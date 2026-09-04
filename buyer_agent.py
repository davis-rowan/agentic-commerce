"""Scripted buyer agent. No LLM: the point is to show the merchant's guarantees, not the buyer's cleverness.

Runs five scenes against http://127.0.0.1:8000 and prints each decision.
"""
import base64
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
AGENT = "agent-davis-01"


def call(method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def scene(n, title):
    print(f"\n=== Scene {n}: {title} ===")


def show(status, body):
    print(f"  HTTP {status}: {json.dumps(body, indent=None)[:400]}")


# Scene 1: ask for a field the merchant knows
scene(1, "ask a known field -> value with source + timestamp")
s, b = call("POST", "/ask", {"product_id": "sku-001", "field": "warranty_months"})
show(s, b)

# Scene 2: ask for a field the merchant does NOT know
scene(2, "ask a missing field -> 'unknown', agent escalates instead of guessing")
s, b = call("POST", "/ask", {"product_id": "sku-002", "field": "warranty_months"})
show(s, b)
if b["answer"] == "unknown":
    print("  AGENT: cannot compare warranties; escalating to human rather than assuming a value.")

# Get a mandate: Rs 1500 per txn, Rs 2000 per day, valid 1 hour
scene(3, "quote + checkout inside mandate -> Razorpay order created")
s, m = call("POST", "/mandate", {"agent_id": AGENT, "max_per_txn_paise": 150000, "max_per_day_paise": 200000})
print(f"  mandate {m['mandate']['mandate_id']}: per-txn {m['mandate']['max_per_txn_paise']}, daily {m['mandate']['max_per_day_paise']}")
mandate_tok = m["mandate_token"]

s, q = call("POST", "/quote", {"product_id": "sku-004", "qty": 1})  # Rs 1299
print(f"  quote {q['quote']['quote_id']}: {q['quote']['amount_paise']} paise, price from {q['quote']['price_source']}")
s, b = call("POST", "/checkout", {"quote_token": q["quote_token"], "mandate_token": mandate_tok}, {"X-Agent-Id": AGENT})
show(s, b)

# Scene 4: tamper with the quote amount, keep the old signature
scene(4, "tamper the quote amount -> refused (signature mismatch)")
body_b64, sig = q["quote_token"].split(".", 1)
payload = json.loads(base64.urlsafe_b64decode(body_b64 + "=" * (-len(body_b64) % 4)))
payload["amount_paise"] = 1
forged = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode().rstrip("=") + "." + sig
s, b = call("POST", "/checkout", {"quote_token": forged, "mandate_token": mandate_tok}, {"X-Agent-Id": AGENT})
show(s, b)

# Scene 5: a second purchase pushes the day total over the cap
scene(5, "second purchase exceeds daily cap -> refused + escalated to human")
s, q2 = call("POST", "/quote", {"product_id": "sku-004", "qty": 1})
s, b = call("POST", "/checkout", {"quote_token": q2["quote_token"], "mandate_token": mandate_tok}, {"X-Agent-Id": AGENT})
show(s, b)

# Bonus: no agent id => treated as a bot
scene("5b", "no X-Agent-Id header -> 401, treated as bot traffic")
s, b = call("POST", "/checkout", {"quote_token": q2["quote_token"], "mandate_token": mandate_tok})
show(s, b)

# Verify the chain
scene(6, "verify the hash-chained ledger")
s, b = call("GET", "/ledger/verify")
show(s, b)
sys.exit(0 if b.get("ok") else 1)
