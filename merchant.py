"""Verifiable storefront for AI buyers, v3.

Four guarantees:
  1. Answers only from catalog rows, each field with source + as_of. Missing -> "unknown".
  2. Prices are issued as signed, time-limited quote tokens, re-verified and REVALIDATED at checkout.
  3. Spend is bounded by a signed mandate (per-txn cap, daily cap, expiry). Breach -> refused + escalated.
  4. Every step appended to a hash-chained ledger, tagged with its process stage.

Research applied (see README):
  - decision envelopes with reasons, freshness horizon and evidence hashes on every money action
  - payment non-escalation: actor -> mandate -> quote -> revalidation -> caps -> Razorpay, in that order
  - claim verification endpoint so a buyer grounds attributes instead of inferring them from titles
  - AP2 roles: mandate = intent mandate, quote = cart mandate, human-completed Razorpay Checkout = payment mandate
"""
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

import catalog
import ledger

load_dotenv(Path(__file__).with_name(".env"))
SECRET = os.environ["QUOTE_SECRET"].encode()
RZP_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RZP_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
STUB = "PASTE_ME" in RZP_ID or "PASTE_ME" in RZP_SECRET or not RZP_ID
MERCHANT_KEY = os.environ.get("MERCHANT_KEY", "demo-merchant")
QUOTE_TTL_S = 120
HERE = Path(__file__).parent

# Process stages (ACWorld: evaluate the trajectory, not just the final state)
STAGE = {"catalog": "observe", "search": "observe", "product": "observe", "ask": "observe", "quality": "observe",
         "verify": "ground", "mandate": "authorize", "quote": "decide", "checkout": "submit", "payment": "execute",
         "merchant_confirm": "merchant"}

app = FastAPI(title="Verifiable Storefront v3")


# ---------- helpers ----------

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(payload: dict) -> str:
    body = _b64(json.dumps(payload, sort_keys=True).encode())
    return f"{body}.{hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()}"


def verify_token(token: str) -> dict | None:
    try:
        body, mac = token.split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(mac, hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()):
        return None
    return json.loads(_unb64(body))


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def log(step: str, request: dict, rule: str, decision: str, ref: str | None = None) -> dict:
    return ledger.append(step, {"stage": STAGE.get(step, "other"), **request}, rule, decision, ref)


def envelope(decision: str, reasons: list[str], evidence: dict, freshness_horizon: int | None = None) -> dict:
    """Decision envelope: what was decided, why, on which evidence (pinned by hash), valid until when."""
    ev = {k: v for k, v in evidence.items() if v is not None}
    body = {"decision": decision, "reasons": reasons, "evidence": ev, "evidence_hash": _sha(ev),
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "freshness_horizon": datetime.fromtimestamp(freshness_horizon, timezone.utc).isoformat() if freshness_horizon else None}
    body["decision_hash"] = _sha(body)
    return body


def _refuse(step: str, req: dict, rule: str, status: int, msg: str, escalate: bool = False, evidence: dict | None = None):
    env = envelope("REFUSED", [rule, msg], evidence or {})
    log(step, {**req, "escalate_to_human": escalate, "decision_hash": env["decision_hash"]}, rule, "REFUSED")
    raise HTTPException(status, {"decision": "REFUSED", "rule": rule, "reason": msg,
                                 "escalated_to_human": escalate, "envelope": env})


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(HERE / "dashboard.html")


# ---------- observe: answer only what we know ----------

@app.get("/catalog")
def catalog_page(offset: int = 0, limit: int = Query(50, le=200)):
    log("catalog", {"offset": offset, "limit": limit}, "SERVE_PAGE", "OK")
    return {"products": catalog.page(offset, limit), "field_format": "{value, source, as_of} or absent => unknown"}


@app.get("/search")
def search(q: str, limit: int = Query(20, le=100)):
    hits = catalog.search(q, limit)
    log("search", {"q": q}, "TOKEN_MATCH", f"{len(hits)}_HITS")
    return {"q": q, "hits": hits, "note": "substring match on name, brand, category only; nothing inferred"}


@app.get("/product/{product_id}")
def product(product_id: str):
    p = catalog.get(product_id)
    if p is None:
        log("product", {"product_id": product_id}, "UNKNOWN_PRODUCT", "404")
        raise HTTPException(404, {"answer": "unknown", "reason": "no such product_id in catalog"})
    log("product", {"product_id": product_id}, "SERVE_ROW", "OK")
    return {"product_id": product_id, **p}


class Ask(BaseModel):
    product_id: str
    field: str


@app.post("/ask")
def ask(q: Ask):
    p = catalog.get(q.product_id)
    if p is None or q.field not in p["fields"]:
        conflict = next((c for c in (p or {}).get("conflicts", []) if c["field"] == q.field), None)
        log("ask", q.model_dump(), "FIELD_CONFLICT" if conflict else "FIELD_ABSENT", "UNKNOWN")
        return {"product_id": q.product_id, "field": q.field, "answer": "unknown",
                "reason": f"sources disagree {conflict['values']}; merchant serves neither" if conflict
                else "field not present on record; merchant does not guess"}
    log("ask", q.model_dump(), "FIELD_PRESENT", "ANSWERED")
    return {"product_id": q.product_id, "field": q.field, "answer": p["fields"][q.field]}


@app.get("/quality")
def quality():
    rep_path = HERE / "quality_report.json"
    rep = json.loads(rep_path.read_text()) if rep_path.exists() else {"error": "run ingest.py first"}
    return {"report": rep, "live_coverage": catalog.coverage(), "max_price_age_days": catalog.MAX_PRICE_AGE_DAYS}


# ---------- ground: verify a claim before acting on it ----------

class Claim(BaseModel):
    product_id: str
    field: str
    expected: str | int | float | bool


@app.post("/verify")
def verify(c: Claim):
    """Buyer says 'I believe field X of product P is V'. Merchant answers MATCH / MISMATCH / UNKNOWN / CONFLICT
    with evidence. This is how an agent grounds an attribute instead of inferring it from a title."""
    v = catalog.verify_claim(c.product_id, c.field, c.expected)
    log("verify", c.model_dump(), f"CLAIM_{v['verdict']}", v["verdict"])
    return {**c.model_dump(), **v}


# ---------- authorize: the human sets the caps (AP2 intent mandate) ----------

class MandateReq(BaseModel):
    agent_id: str
    max_per_txn_paise: int
    max_per_day_paise: int
    valid_for_s: int = 3600
    human_present: bool = True


@app.post("/mandate")
def mandate(m: MandateReq):
    payload = {**m.model_dump(), "mandate_id": uuid.uuid4().hex[:12], "currency": "INR",
               "merchant_scope": "verifiable-storefront", "expires_at": int(time.time()) + m.valid_for_s}
    log("mandate", payload, "HUMAN_AUTHORISED", "ISSUED")
    return {"mandate_token": sign(payload), "mandate": payload, "ap2_role": "intent_mandate"}


# ---------- decide: a signed cart, not a number (AP2 cart mandate) ----------

class QuoteReq(BaseModel):
    product_id: str
    qty: int = 1


def _explain(p: dict) -> str:
    return {
        "NO_PRICE": "no sell price on record",
        "PRICE_CONFLICT": f"sources hold conflicting prices {p['conflicts']}; merchant has not resolved",
        "PRICE_STALE": f"price as_of {p['fields'].get('sell_price_paise', {}).get('as_of')} is older than {catalog.MAX_PRICE_AGE_DAYS} days",
        "NO_STOCK": "no stock source on record; merchant does not assume availability",
        "OUT_OF_STOCK": "stock snapshot says 0",
    }.get(p["unsellable_reason"], p["unsellable_reason"])


@app.post("/quote")
def quote(r: QuoteReq):
    p = catalog.get(r.product_id)
    if p is None:
        _refuse("quote", r.model_dump(), "UNKNOWN_PRODUCT", 404, "no such product_id; nothing to quote")
    if not p["sellable"]:
        fixable = p["unsellable_reason"] in ("PRICE_STALE", "PRICE_CONFLICT", "NO_STOCK")
        _refuse("quote", r.model_dump(), f"REFUSED_{p['unsellable_reason']}", 409, _explain(p), escalate=fixable,
                evidence={"sell_price_paise": p["fields"].get("sell_price_paise"), "stock": p["fields"].get("stock")})
    f = p["fields"]
    if r.qty < 1 or r.qty > f["stock"]["value"]:
        _refuse("quote", r.model_dump(), "REFUSED_INSUFFICIENT_STOCK", 409, f"qty {r.qty} exceeds stock on record",
                evidence={"stock": f["stock"]})
    now = int(time.time())
    sell = f["sell_price_paise"]
    payload = {
        "quote_id": uuid.uuid4().hex[:12], "product_id": r.product_id, "qty": r.qty, "currency": "INR",
        "sell_price_paise": sell["value"], "amount_paise": sell["value"] * r.qty,
        "list_price_paise": f.get("list_price_paise", {}).get("value"),
        "price_source": sell["source"], "price_as_of": sell["as_of"], "stock_as_of": f["stock"]["as_of"],
        "issued_at": now, "expires_at": now + QUOTE_TTL_S,
    }
    env = envelope("ISSUED", ["SELLABLE_PRICE_ON_RECORD"],
                   {"sell_price_paise": sell, "stock": f["stock"], "list_price_paise": f.get("list_price_paise")}, payload["expires_at"])
    log("quote", {**r.model_dump(), "quote_id": payload["quote_id"], "amount_paise": payload["amount_paise"],
                  "decision_hash": env["decision_hash"]}, "SELLABLE_PRICE_ON_RECORD", "ISSUED")
    return {"quote_token": sign(payload), "quote": payload, "ap2_role": "cart_mandate", "envelope": env,
            "note": "amount is computed from sell_price_paise only; list_price_paise is informational"}


# ---------- merchant actions ----------

class ConfirmReq(BaseModel):
    product_id: str
    sell_price_paise: int
    stock_qty: int | None = None


@app.post("/merchant/confirm_price")
def confirm_price(c: ConfirmReq, x_merchant_key: str | None = Header(default=None)):
    if x_merchant_key != MERCHANT_KEY:
        raise HTTPException(401, {"reason": "X-Merchant-Key required"})
    if not catalog.confirm_price(c.product_id, c.sell_price_paise, c.stock_qty, "merchant:confirm_endpoint"):
        raise HTTPException(404, {"reason": "no such product_id"})
    log("merchant_confirm", c.model_dump(), "HUMAN_CONFIRMED_PRICE", "APPLIED")
    sellable, reason = catalog.sellability(c.product_id)
    return {"product_id": c.product_id, "sellable": sellable, "unsellable_reason": reason}


# ---------- submit: verify everything in a fixed order, then create the Razorpay order ----------

class CheckoutReq(BaseModel):
    quote_token: str
    mandate_token: str


def _razorpay_order(amount_paise: int, quote_id: str, agent_id: str) -> tuple[str, str]:
    if STUB:
        return "order_STUB" + uuid.uuid4().hex[:10], "stub"
    import razorpay
    order = razorpay.Client(auth=(RZP_ID, RZP_SECRET)).order.create(
        {"amount": amount_paise, "currency": "INR", "receipt": quote_id, "notes": {"agent_id": agent_id, "quote_id": quote_id}})
    return order["id"], "razorpay_test"


@app.post("/checkout")
def checkout(c: CheckoutReq, x_agent_id: str | None = Header(default=None)):
    """Order of gates is the point (payment non-escalation):
    actor -> intent mandate -> cart -> revalidation against live state -> caps -> payment rail."""
    req = {"agent_id": x_agent_id}
    # 1. actor
    if not x_agent_id:
        _refuse("checkout", req, "NO_AGENT_ID", 401, "X-Agent-Id header required; unidentified callers are treated as bots")
    # 2. intent mandate
    m = verify_token(c.mandate_token)
    if m is None:
        _refuse("checkout", req, "REFUSED_MANDATE_INVALID", 403, "mandate signature invalid or tampered")
    req["mandate_id"] = m["mandate_id"]
    if m["agent_id"] != x_agent_id:
        _refuse("checkout", req, "REFUSED_MANDATE_AGENT_MISMATCH", 403, "mandate was issued to a different agent")
    now = int(time.time())
    if now > m["expires_at"]:
        _refuse("checkout", req, "REFUSED_MANDATE_EXPIRED", 403, "mandate expired")
    # 3. cart mandate
    q = verify_token(c.quote_token)
    if q is None:
        _refuse("checkout", req, "REFUSED_QUOTE_INVALID", 400, "quote signature invalid or tampered")
    req.update(quote_id=q["quote_id"], product_id=q["product_id"], qty=q["qty"], amount_paise=q["amount_paise"])
    if now > q["expires_at"]:
        _refuse("checkout", req, "REFUSED_QUOTE_EXPIRED", 410, f"quote expired at {q['expires_at']}; request a fresh quote")
    # 4. revalidation: has the world moved since the quote?
    p = catalog.get(q["product_id"])
    live = p["fields"] if p else {}
    if p is None or not p["sellable"]:
        _refuse("checkout", req, "REFUSED_REQUIRES_REVALIDATION", 409,
                f"product no longer sellable ({p['unsellable_reason'] if p else 'gone'}); request a fresh quote", evidence=live)
    if live["sell_price_paise"]["value"] != q["sell_price_paise"]:
        _refuse("checkout", req, "REFUSED_REQUIRES_REVALIDATION", 409,
                f"price changed {q['sell_price_paise']} -> {live['sell_price_paise']['value']} since quote", evidence=live)
    if live["stock"]["value"] < q["qty"]:
        _refuse("checkout", req, "REFUSED_REQUIRES_REVALIDATION", 409, "stock dropped below quoted qty", evidence=live)
    # 5. caps
    if q["amount_paise"] > m["max_per_txn_paise"]:
        _refuse("checkout", req, "REFUSED_MANDATE_PER_TXN", 403,
                f"amount {q['amount_paise']} exceeds per-transaction cap {m['max_per_txn_paise']}", escalate=True)
    spent = ledger.approved_total_today(x_agent_id)
    if spent + q["amount_paise"] > m["max_per_day_paise"]:
        _refuse("checkout", req, "REFUSED_MANDATE_DAILY", 403,
                f"spent today {spent} + {q['amount_paise']} exceeds daily cap {m['max_per_day_paise']}", escalate=True)
    # 6. payment rail
    order_id, mode = _razorpay_order(q["amount_paise"], q["quote_id"], x_agent_id)
    env = envelope("APPROVED", ["ALL_GATES_PASSED"],
                   {"mandate_id": m["mandate_id"], "quote_id": q["quote_id"], "sell_price_paise": live["sell_price_paise"],
                    "stock": live["stock"], "spent_today_before": spent}, q["expires_at"])
    log("checkout", {**req, "razorpay_mode": mode, "decision_hash": env["decision_hash"]}, "ALL_GATES_PASSED", "APPROVED", ref=order_id)
    return {"decision": "APPROVED", "razorpay_order_id": order_id, "razorpay_mode": mode, "amount_paise": q["amount_paise"],
            "currency": "INR", "spent_today_after": spent + q["amount_paise"], "envelope": env,
            "next": "human completes payment in Razorpay Checkout (AP2 payment mandate, human present); POST /payment/verify"}


# ---------- execute: the human pays, the merchant verifies the signature and the API ----------

@app.get("/payment/config")
def payment_config():
    return {"key_id": None if STUB else RZP_ID, "mode": "stub" if STUB else "razorpay_test"}


class PaymentVerify(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


@app.post("/payment/verify")
def payment_verify(p: PaymentVerify):
    req = p.model_dump()
    approved = next((r for r in reversed(ledger.rows()) if r["step"] == "checkout" and r["razorpay_ref"] == p.razorpay_order_id), None)
    if approved is None:
        _refuse("payment", req, "REFUSED_UNKNOWN_ORDER", 404, "order id was never approved by this merchant")
    expected_amount = approved["request"]["amount_paise"]
    if STUB:
        if not p.razorpay_payment_id.startswith("pay_STUB"):
            _refuse("payment", req, "REFUSED_PAYMENT_SIGNATURE", 400, "stub mode accepts only simulated pay_STUB ids")
        mode, status, amount = "stub", "captured", expected_amount
    else:
        expected = hmac.new(RZP_SECRET.encode(), f"{p.razorpay_order_id}|{p.razorpay_payment_id}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, p.razorpay_signature):
            _refuse("payment", req, "REFUSED_PAYMENT_SIGNATURE", 400, "signature does not match; payment not from Razorpay or tampered")
        import razorpay
        pay = razorpay.Client(auth=(RZP_ID, RZP_SECRET)).payment.fetch(p.razorpay_payment_id)
        mode, status, amount = "razorpay_test", pay["status"], pay["amount"]
        if pay.get("order_id") != p.razorpay_order_id:
            _refuse("payment", req, "REFUSED_PAYMENT_ORDER_MISMATCH", 400, "payment belongs to a different order")
    if amount != expected_amount:
        _refuse("payment", req, "REFUSED_PAYMENT_AMOUNT_MISMATCH", 400, f"paid {amount} but approved {expected_amount}")
    if status not in ("captured", "authorized"):
        _refuse("payment", req, "REFUSED_PAYMENT_NOT_CAPTURED", 402, f"payment status {status}")
    env = envelope("PAID", ["SIGNATURE_VERIFIED", "API_CONFIRMED", "AMOUNT_MATCHES_APPROVAL"],
                   {"order_id": p.razorpay_order_id, "payment_id": p.razorpay_payment_id, "amount_paise": amount, "status": status})
    log("payment", {**req, "agent_id": approved["request"].get("agent_id"), "amount_paise": amount, "razorpay_mode": mode,
                    "decision_hash": env["decision_hash"]}, "SIGNATURE_AND_API_VERIFIED", "PAID", ref=p.razorpay_payment_id)
    return {"decision": "PAID", "payment_id": p.razorpay_payment_id, "status": status, "amount_paise": amount,
            "razorpay_mode": mode, "ap2_role": "payment_mandate", "envelope": env}


# ---------- ledger ----------

@app.get("/ledger")
def ledger_dump(limit: int = Query(200, le=2000)):
    rows = ledger.rows()
    return {"rows": rows[-limit:], "total": len(rows)}


@app.get("/ledger/verify")
def ledger_verify():
    return ledger.verify()
