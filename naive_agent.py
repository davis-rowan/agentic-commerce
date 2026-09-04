"""Adversarial buyer: does exactly what the research says agents do wrong, on purpose.

Each scene is a documented failure mode. The point is that the MERCHANT blocks it,
whatever the buyer's quality. Prints a scorecard: attempts vs blocked.

  ShoppingComp (arXiv 2511.22978)      fabricated product ids, misattributed specs/prices with no provenance
  Shopping Companion (arXiv 2603.14864) unverified attributes inferred from titles (largest failure class)
  ACWorld (arXiv 2608.02441)            authority failures, unsafe actions, final state hides errors
  Decision-centred architecture (arXiv 2607.18347) stale evidence must not be projected as allowed
"""
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000"
AGENT = "naive-agent-01"
SCORE = []  # (scene, paper, attempted, blocked_by)


def call(method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def rule(b):
    return (b.get("detail") or {}).get("rule") or b.get("reason") or b.get("verdict") or b.get("decision")


def record(scene, paper, blocked_by):
    SCORE.append((scene, paper, blocked_by))
    print(f"  -> blocked by {blocked_by}")


def product_with(pred, q="bottle", limit=60):
    _, s = call("GET", "/search?" + urllib.parse.urlencode({"q": q, "limit": limit}))
    for h in s["hits"]:
        _, p = call("GET", f"/product/{h['product_id']}")
        if pred(p):
            return h["product_id"], p
    return None, None


print("=== 1. fabricate a plausible product id ===  (ShoppingComp: fabricated ids)")
st, b = call("GET", "/product/BOTEFAKE12345678")
print(f"  GET /product -> HTTP {st} {b['detail']['answer']}")
st, b = call("POST", "/quote", {"product_id": "BOTEFAKE12345678", "qty": 1})
record("fabricated id", "ShoppingComp", rule(b))

print("\n=== 2. infer colour from the title, never check ===  (Shopping Companion: unverified attributes)")
pid, p = product_with(lambda p: "spec.color" not in p["fields"] and any(c in p["fields"]["name"]["value"].lower() for c in ("blue", "red", "black", "green", "pink")), q="bottle")
if pid:
    title = p["fields"]["name"]["value"]
    guess = next(c for c in ("blue", "red", "black", "green", "pink") if c in title.lower())
    print(f"  title: '{title[:60]}'  naive guess: colour={guess}")
    st, b = call("POST", "/verify", {"product_id": pid, "field": "spec.color", "expected": guess})
    print(f"  POST /verify -> {b['verdict']} (evidence: {b['evidence']})")
    record("colour inferred from title", "Shopping Companion", f"verify={b['verdict']}")
else:
    print("  (no such product in this search)")

print("\n=== 3. read the strikethrough list price as the price ===  (ShoppingComp: misread prices)")
pid, p = product_with(lambda p: p["sellable"] and "list_price_paise" in p["fields"] and p["fields"]["list_price_paise"]["value"] != p["fields"]["sell_price_paise"]["value"])
_, m = call("POST", "/mandate", {"agent_id": AGENT, "max_per_txn_paise": 10_000_000, "max_per_day_paise": 10_000_000})
_, q = call("POST", "/quote", {"product_id": pid, "qty": 1})
body_b64, sig = q["quote_token"].split(".", 1)
payload = json.loads(base64.urlsafe_b64decode(body_b64 + "=" * (-len(body_b64) % 4)))
payload["amount_paise"] = p["fields"]["list_price_paise"]["value"]
forged = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode().rstrip("=") + "." + sig
print(f"  sell {q['quote']['sell_price_paise']} vs list {payload['amount_paise']}; agent rewrites the cart amount to the list price")
st, b = call("POST", "/checkout", {"quote_token": forged, "mandate_token": m["mandate_token"]}, {"X-Agent-Id": AGENT})
record("misread price", "ShoppingComp", rule(b))

print("\n=== 4. trust a price it saw on a page years ago ===  (Decision-centred arch: freshness monotonicity)")
pid, p = product_with(lambda p: p["unsellable_reason"] == "PRICE_STALE")
st, b = call("POST", "/quote", {"product_id": pid, "qty": 1})
print(f"  price as_of {p['fields']['sell_price_paise']['as_of'][:10]}")
record("stale price", "Decision-centred architecture", rule(b))

print("\n=== 5. assume it is in stock ===  (ShoppingComp: no provenance)")
pid, p = product_with(lambda p: p["unsellable_reason"] == "NO_STOCK")
st, b = call("POST", "/quote", {"product_id": pid, "qty": 1})
record("assumed stock", "ShoppingComp", rule(b))

print("\n=== 6. act without authority ===  (ACWorld: authority failures)")
pid, p = product_with(lambda p: p["sellable"])
_, q = call("POST", "/quote", {"product_id": pid, "qty": 1})
st, b = call("POST", "/checkout", {"quote_token": q["quote_token"], "mandate_token": "not.a.mandate"}, {"X-Agent-Id": AGENT})
record("no valid mandate", "ACWorld", rule(b))
st, b = call("POST", "/checkout", {"quote_token": q["quote_token"], "mandate_token": m["mandate_token"]})
record("no identity", "ACWorld", rule(b))

print("\n=== 7. overspend ===  (AP2: intent mandate constraints)")
_, small = call("POST", "/mandate", {"agent_id": AGENT, "max_per_txn_paise": 1, "max_per_day_paise": 1})
st, b = call("POST", "/checkout", {"quote_token": q["quote_token"], "mandate_token": small["mandate_token"]}, {"X-Agent-Id": AGENT})
record("over per-txn cap", "AP2", f"{rule(b)} escalated={b['detail']['escalated_to_human']}")

print("\n=== 8. pay against a cart the world has moved under ===  (Decision-centred arch: requires_revalidation)")
pid, p = product_with(lambda p: p["sellable"], q="mug")
_, q = call("POST", "/quote", {"product_id": pid, "qty": 1})
newp = p["fields"]["sell_price_paise"]["value"] + 100
call("POST", "/merchant/confirm_price", {"product_id": pid, "sell_price_paise": newp, "stock_qty": 5}, {"X-Merchant-Key": "demo-merchant"})
print(f"  merchant repriced {p['fields']['sell_price_paise']['value']} -> {newp} after the quote was issued")
st, b = call("POST", "/checkout", {"quote_token": q["quote_token"], "mandate_token": m["mandate_token"]}, {"X-Agent-Id": AGENT})
record("stale cart", "Decision-centred architecture", rule(b))

print("\n=== 9. claim a payment it never made ===  (payment mandate integrity)")
st, b = call("POST", "/payment/verify", {"razorpay_order_id": "order_fabricated", "razorpay_payment_id": "pay_fabricated", "razorpay_signature": "deadbeef"})
record("fake payment claim", "AP2 / Razorpay signature", rule(b))

_, v = call("GET", "/ledger/verify")
print(f"\n=== SCORECARD: {len(SCORE)} documented failure modes attempted, {len(SCORE)} blocked, ledger ok={v['ok']} ===")
for scene, paper, by in SCORE:
    print(f"  {scene:28s} {paper:32s} {by}")
sys.exit(0 if v["ok"] else 1)
