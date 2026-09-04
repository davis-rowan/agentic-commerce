"""Rule tests on a tiny synthetic catalog. Run: python -m pytest -q"""
import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / ".tmp"
TMP.mkdir(exist_ok=True)
os.environ["CATALOG_DB"] = str(TMP / "catalog.db")
os.environ["LEDGER_DB"] = str(TMP / "ledger.db")
os.environ["QUOTE_SECRET"] = "test-secret"
os.environ["RAZORPAY_KEY_ID"] = "rzp_test_PASTE_ME"  # force stub mode

import catalog  # noqa: E402
import ledger  # noqa: E402

FRESH = datetime.now(timezone.utc).isoformat()
STALE = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()


def _p(pid, **kw):
    base = {"product_id": pid, "uniq_id": "u" + pid, "name": f"Thing {pid}", "brand": "Acme", "category": "Home >> Kitchen",
            "list_price_paise": 150000, "sell_price_paise": 100000, "price_as_of": FRESH, "description": "marketing text",
            "rating": None, "price_conflict": 0, "crawl_ts": FRESH}
    return {**base, **kw}


@pytest.fixture(scope="module")
def client():
    for f in TMP.glob("*.db"):
        try:
            f.unlink()
        except PermissionError:
            pass
    catalog.build([
        _p("ok"), _p("noprice", sell_price_paise=None), _p("stale", price_as_of=STALE),
        _p("conflict", price_conflict=1), _p("nostock"), _p("zero"), _p("blue", name="Blue Bottle"),
    ], [("ok", "color", "red"), ("blue", "color", "blue")], [("conflict", "sell_price_paise", json.dumps([90000, 100000]))])
    catalog.add_stock([("ok", 5, "t", FRESH), ("stale", 5, "t", FRESH), ("conflict", 5, "t", FRESH), ("zero", 0, "t", FRESH), ("blue", 3, "t", FRESH)])
    from fastapi.testclient import TestClient
    import merchant
    return TestClient(merchant.app)


def _mandate(client, agent="a1", txn=200000, day=250000):
    return client.post("/mandate", json={"agent_id": agent, "max_per_txn_paise": txn, "max_per_day_paise": day}).json()["mandate_token"]


def test_sellability_reasons(client):
    assert catalog.sellability("ok") == (True, None)
    assert catalog.sellability("noprice") == (False, "NO_PRICE")
    assert catalog.sellability("stale") == (False, "PRICE_STALE")
    assert catalog.sellability("conflict") == (False, "PRICE_CONFLICT")
    assert catalog.sellability("nostock") == (False, "NO_STOCK")
    assert catalog.sellability("zero") == (False, "OUT_OF_STOCK")
    assert catalog.sellability("ghost") == (False, "UNKNOWN_PRODUCT")


def test_ask_never_guesses(client):
    assert client.post("/ask", json={"product_id": "ok", "field": "spec.color"}).json()["answer"]["value"] == "red"
    assert client.post("/ask", json={"product_id": "nostock", "field": "spec.color"}).json()["answer"] == "unknown"
    assert client.post("/ask", json={"product_id": "ok", "field": "stock"}).json()["answer"]["source"] == "t"


def test_search_is_deterministic(client):
    hits = client.get("/search", params={"q": "blue bottle"}).json()["hits"]
    assert [h["product_id"] for h in hits] == ["blue"] and hits[0]["matched_on"] == ["name"]
    assert client.get("/search", params={"q": "unicorn"}).json()["hits"] == []


def test_quote_gates(client):
    for pid, rule in [("noprice", "REFUSED_NO_PRICE"), ("stale", "REFUSED_PRICE_STALE"), ("conflict", "REFUSED_PRICE_CONFLICT"),
                      ("nostock", "REFUSED_NO_STOCK"), ("zero", "REFUSED_OUT_OF_STOCK")]:
        r = client.post("/quote", json={"product_id": pid, "qty": 1})
        assert r.status_code == 409 and r.json()["detail"]["rule"] == rule, pid
    q = client.post("/quote", json={"product_id": "ok", "qty": 2}).json()["quote"]
    assert q["amount_paise"] == 200000 and q["sell_price_paise"] == 100000 and q["list_price_paise"] == 150000


def test_confirm_price_clears_stale(client):
    r = client.post("/merchant/confirm_price", json={"product_id": "stale", "sell_price_paise": 99000}, headers={"X-Merchant-Key": "demo-merchant"})
    assert r.json()["sellable"] is True
    q = client.post("/quote", json={"product_id": "stale", "qty": 1}).json()["quote"]
    assert q["sell_price_paise"] == 99000 and q["price_source"] == "merchant:confirm_endpoint"
    assert client.post("/merchant/confirm_price", json={"product_id": "ok", "sell_price_paise": 1}).status_code == 401


def test_checkout_tamper_and_mandate(client):
    m = _mandate(client)
    tok = client.post("/quote", json={"product_id": "ok", "qty": 1}).json()["quote_token"]
    ok = client.post("/checkout", json={"quote_token": tok, "mandate_token": m}, headers={"X-Agent-Id": "a1"})
    assert ok.status_code == 200 and ok.json()["razorpay_mode"] == "stub"
    body, sig = tok.split(".", 1)
    p = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))); p["amount_paise"] = 1
    forged = base64.urlsafe_b64encode(json.dumps(p, sort_keys=True).encode()).decode().rstrip("=") + "." + sig
    assert client.post("/checkout", json={"quote_token": forged, "mandate_token": m}, headers={"X-Agent-Id": "a1"}).json()["detail"]["rule"] == "REFUSED_QUOTE_INVALID"
    tok2 = client.post("/quote", json={"product_id": "ok", "qty": 2}).json()["quote_token"]
    r = client.post("/checkout", json={"quote_token": tok2, "mandate_token": m}, headers={"X-Agent-Id": "a1"})
    assert r.json()["detail"]["rule"] == "REFUSED_MANDATE_DAILY" and r.json()["detail"]["escalated_to_human"] is True
    assert client.post("/checkout", json={"quote_token": tok2, "mandate_token": m}).status_code == 401
    assert client.post("/checkout", json={"quote_token": tok2, "mandate_token": m}, headers={"X-Agent-Id": "someone-else"}).json()["detail"]["rule"] == "REFUSED_MANDATE_AGENT_MISMATCH"


def test_ledger_chain(client):
    assert ledger.verify()["ok"] is True
    import sqlite3
    c = sqlite3.connect(ledger.DB); c.execute("UPDATE ledger SET decision='APPROVED' WHERE seq=2"); c.commit(); c.close()
    v = ledger.verify()
    assert v["ok"] is False and v["broken_at_seq"] == 2
