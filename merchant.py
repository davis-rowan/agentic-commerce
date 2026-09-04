"""Verifiable storefront for AI buyers.

Four guarantees:
  1. Answers only from catalog rows, each field with source + as_of. Missing -> "unknown".
  2. Prices are issued as signed, time-limited quote tokens, re-verified at checkout.
  3. Spend is bounded by a signed mandate (per-txn cap, daily cap, expiry). Breach -> refused + escalated.
  4. Every step appended to a hash-chained ledger.
"""
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

import ledger

load_dotenv(Path(__file__).with_name(".env"))
SECRET = os.environ["QUOTE_SECRET"].encode()
RZP_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RZP_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
STUB = "PASTE_ME" in RZP_ID or "PASTE_ME" in RZP_SECRET or not RZP_ID
QUOTE_TTL_S = 120

CATALOG = json.loads(Path(__file__).with_name("catalog.json").read_text())["products"]
app = FastAPI(title="Verifiable Storefront")

# ---------- signing helpers ----------

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(payload: dict) -> str:
    body = _b64(json.dumps(payload, sort_keys=True).encode())
    mac = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def verify_token(token: str) -> dict | None:
    """Return payload if signature is valid, else None. Never trust the payload before this passes."""
    try:
        body, mac = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None
    return json.loads(_unb64(body))


# ---------- catalog: answer only what we know ----------

@app.get("/catalog")
def catalog():
    ledger.append("catalog", {}, "SERVE_ALL", "OK")
    return {"products": CATALOG, "field_format": "{value, source, as_of} or absent => unknown"}


@app.get("/product/{product_id}")
def product(product_id: str):
    p = CATALOG.get(product_id)
    if p is None:
        ledger.append("product", {"product_id": product_id}, "UNKNOWN_PRODUCT", "404")
        raise HTTPException(404, {"answer": "unknown", "reason": "no such product_id in catalog"})
    ledger.append("product", {"product_id": product_id}, "SERVE_ROW", "OK")
    return {"product_id": product_id, "fields": p}


class Ask(BaseModel):
    product_id: str
    field: str


@app.post("/ask")
def ask(q: Ask):
    """The anti-hallucination endpoint: exact field or 'unknown'. Never inferred, never defaulted."""
    p = CATALOG.get(q.product_id)
    if p is None or q.field not in p:
        ledger.append("ask", q.model_dump(), "FIELD_ABSENT", "UNKNOWN")
        return {"product_id": q.product_id, "field": q.field, "answer": "unknown",
                "reason": "field not present in catalog row; merchant does not guess"}
    ledger.append("ask", q.model_dump(), "FIELD_PRESENT", "ANSWERED")
    return {"product_id": q.product_id, "field": q.field, "answer": p[q.field]}


# ---------- quotes: a token, not a number ----------

class QuoteReq(BaseModel):
    product_id: str
    qty: int = 1


@app.post("/quote")
def quote(r: QuoteReq):
    p = CATALOG.get(r.product_id)
    if p is None or "price_paise" not in p:
        ledger.append("quote", r.model_dump(), "NO_PRICE_ON_RECORD", "REFUSED")
        raise HTTPException(409, {"answer": "unknown", "reason": "no price on record; cannot quote"})
    if r.qty < 1 or r.qty > p.get("stock", {"value": 0})["value"]:
        ledger.append("quote", r.model_dump(), "INSUFFICIENT_STOCK", "REFUSED")
        raise HTTPException(409, {"reason": "qty exceeds stock on record", "stock": p.get("stock")})
    now = int(time.time())
    payload = {
        "quote_id": uuid.uuid4().hex[:12],
        "product_id": r.product_id,
        "qty": r.qty,
        "unit_price_paise": p["price_paise"]["value"],
        "amount_paise": p["price_paise"]["value"] * r.qty,
        "price_source": p["price_paise"]["source"],
        "price_as_of": p["price_paise"]["as_of"],
        "issued_at": now,
        "expires_at": now + QUOTE_TTL_S,
    }
    tok = sign(payload)
    ledger.append("quote", {**r.model_dump(), "quote_id": payload["quote_id"], "amount_paise": payload["amount_paise"]},
                  "PRICE_ON_RECORD", "ISSUED")
    return {"quote_token": tok, "quote": payload}


# ---------- mandates: the human sets the caps, the agent cannot edit them ----------

class MandateReq(BaseModel):
    agent_id: str
    max_per_txn_paise: int
    max_per_day_paise: int
    valid_for_s: int = 3600


@app.post("/mandate")
def mandate(m: MandateReq):
    """Simulates the human principal authorising an agent. Returns a signed mandate token."""
    payload = {**m.model_dump(), "mandate_id": uuid.uuid4().hex[:12], "expires_at": int(time.time()) + m.valid_for_s}
    ledger.append("mandate", payload, "HUMAN_AUTHORISED", "ISSUED")
    return {"mandate_token": sign(payload), "mandate": payload}


# ---------- checkout: verify everything, then call Razorpay ----------

class CheckoutReq(BaseModel):
    quote_token: str
    mandate_token: str


def _refuse(step_req: dict, rule: str, status: int, msg: str, escalate: bool = False):
    ledger.append("checkout", {**step_req, "escalate_to_human": escalate}, rule, "REFUSED")
    raise HTTPException(status, {"decision": "REFUSED", "rule": rule, "reason": msg, "escalated_to_human": escalate})


def _razorpay_order(amount_paise: int, quote_id: str, agent_id: str) -> tuple[str, str]:
    if STUB:
        return "order_STUB" + uuid.uuid4().hex[:10], "stub"
    import razorpay
    client = razorpay.Client(auth=(RZP_ID, RZP_SECRET))
    order = client.order.create({
        "amount": amount_paise, "currency": "INR", "receipt": quote_id,
        "notes": {"agent_id": agent_id, "quote_id": quote_id},
    })
    return order["id"], "razorpay_test"


@app.post("/checkout")
def checkout(c: CheckoutReq, x_agent_id: str | None = Header(default=None)):
    req = {"agent_id": x_agent_id}
    if not x_agent_id:
        _refuse(req, "NO_AGENT_ID", 401, "X-Agent-Id header required; unidentified callers are treated as bots")

    m = verify_token(c.mandate_token)
    if m is None:
        _refuse(req, "REFUSED_MANDATE_INVALID", 403, "mandate signature invalid or tampered")
    req["mandate_id"] = m["mandate_id"]
    if m["agent_id"] != x_agent_id:
        _refuse(req, "REFUSED_MANDATE_AGENT_MISMATCH", 403, "mandate was issued to a different agent")
    now = int(time.time())
    if now > m["expires_at"]:
        _refuse(req, "REFUSED_MANDATE_EXPIRED", 403, "mandate expired")

    q = verify_token(c.quote_token)
    if q is None:
        _refuse(req, "REFUSED_QUOTE_INVALID", 400, "quote signature invalid or tampered")
    req.update(quote_id=q["quote_id"], product_id=q["product_id"], qty=q["qty"], amount_paise=q["amount_paise"])
    if now > q["expires_at"]:
        _refuse(req, "REFUSED_QUOTE_EXPIRED", 410, f"quote expired at {q['expires_at']}; request a fresh quote")

    if q["amount_paise"] > m["max_per_txn_paise"]:
        _refuse(req, "REFUSED_MANDATE_PER_TXN", 403,
                f"amount {q['amount_paise']} exceeds per-transaction cap {m['max_per_txn_paise']}", escalate=True)
    spent = ledger.approved_total_today(x_agent_id)
    if spent + q["amount_paise"] > m["max_per_day_paise"]:
        _refuse(req, "REFUSED_MANDATE_DAILY", 403,
                f"spent today {spent} + {q['amount_paise']} exceeds daily cap {m['max_per_day_paise']}", escalate=True)

    order_id, mode = _razorpay_order(q["amount_paise"], q["quote_id"], x_agent_id)
    ledger.append("checkout", {**req, "razorpay_mode": mode}, "ALL_CHECKS_PASSED", "APPROVED", razorpay_ref=order_id)
    return {"decision": "APPROVED", "razorpay_order_id": order_id, "razorpay_mode": mode,
            "amount_paise": q["amount_paise"], "spent_today_after": spent + q["amount_paise"]}


# ---------- ledger ----------

@app.get("/ledger")
def ledger_dump():
    return {"rows": ledger.rows()}


@app.get("/ledger/verify")
def ledger_verify():
    return ledger.verify()
