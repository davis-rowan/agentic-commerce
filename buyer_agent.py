"""Scripted buyer agent v2. No LLM: the point is the merchant's guarantees, not the buyer's cleverness.

Given a shopping intent, it searches, filters ONLY on fields the merchant actually has, escalates on
anything unknown instead of assuming, and checks out under a mandate. Then it runs the negative scenes.
Counts its own assumptive decisions (must stay 0).
"""
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000"
AGENT = "agent-davis-02"
INTENT = {"want": "water bottle", "max_paise": 100000, "must": {"spec.color": None}}  # None = any value, but it must be KNOWN
STATS = {"assumptive_decisions": 0, "escalations": 0}


def call(method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def scene(n, title):
    print(f"\n=== Scene {n}: {title} ===")


def escalate(msg):
    STATS["escalations"] += 1
    print(f"  ESCALATE: {msg}")


# ---- Scene 1: intent -> search -> filter on known fields only ----
scene(1, f"intent {json.dumps(INTENT)}")
_, s = call("GET", "/search?" + urllib.parse.urlencode({"q": INTENT["want"], "limit": 40}))
print(f"  search returned {len(s['hits'])} real product ids (matched_on reported per hit)")
chosen = None
dropped = {}
for h in s["hits"]:
    _, p = call("GET", f"/product/{h['product_id']}")
    f = p["fields"]
    if not p["sellable"]:
        dropped[p["unsellable_reason"]] = dropped.get(p["unsellable_reason"], 0) + 1
        continue
    if f["sell_price_paise"]["value"] > INTENT["max_paise"]:
        dropped["OVER_BUDGET"] = dropped.get("OVER_BUDGET", 0) + 1
        continue
    missing = [k for k in INTENT["must"] if k not in f]
    if missing:
        dropped["MUST_FIELD_UNKNOWN"] = dropped.get("MUST_FIELD_UNKNOWN", 0) + 1
        continue  # NOT assumed: an agent that guessed here would count an assumptive decision
    chosen = (h["product_id"], f)
    break
print(f"  dropped: {dropped}")
if not chosen:
    escalate("no candidate has every required field on record; asking the human instead of guessing")
    print(f"\nSTATS {STATS}")
    sys.exit(0)
pid, f = chosen
print(f"  chosen {pid}: {f['name']['value'][:60]}")
print(f"    sell price ₹{f['sell_price_paise']['value']/100:.2f}  source={f['sell_price_paise']['source']}  as_of={f['sell_price_paise']['as_of'][:10]}")
print(f"    list price {'₹%.2f' % (f['list_price_paise']['value']/100) if 'list_price_paise' in f else 'unknown'} (informational only, never used for amount)")
print(f"    color={f['spec.color']['value']}  stock={f['stock']['value']} ({f['stock']['source']})")

# ---- Scene 2: mandate -> quote -> checkout ----
scene(2, "mandate + signed quote + checkout")
_, m = call("POST", "/mandate", {"agent_id": AGENT, "max_per_txn_paise": 150000, "max_per_day_paise": 200000})
mandate_tok = m["mandate_token"]
print(f"  mandate {m['mandate']['mandate_id']}: per-txn 1500.00, daily 2000.00")
st, q = call("POST", "/quote", {"product_id": pid, "qty": 1})
print(f"  quote {q['quote']['quote_id']}: amount ₹{q['quote']['amount_paise']/100:.2f} from {q['quote']['price_source']}, 120 s TTL")
st, b = call("POST", "/checkout", {"quote_token": q["quote_token"], "mandate_token": mandate_tok}, {"X-Agent-Id": AGENT})
print(f"  HTTP {st}: {json.dumps(b)[:200]}")

# ---- Scene 3: stale price -> refused -> merchant confirms -> quote ok ----
scene(3, "stale-price product: refused until a human on the merchant side confirms")
_, cov = call("GET", "/quality")
stale_pid = None
for h in s["hits"]:
    _, p = call("GET", f"/product/{h['product_id']}")
    if p["unsellable_reason"] == "PRICE_STALE":
        stale_pid = h["product_id"]; stale_price = p["fields"]["sell_price_paise"]["value"]; break
if stale_pid:
    st, b = call("POST", "/quote", {"product_id": stale_pid, "qty": 1})
    print(f"  HTTP {st} {b['detail']['rule']}: {b['detail']['reason'][:90]}")
    escalate(f"asked merchant to confirm price for {stale_pid}")
    st, b = call("POST", "/merchant/confirm_price", {"product_id": stale_pid, "sell_price_paise": stale_price, "stock_qty": 10}, {"X-Merchant-Key": "demo-merchant"})
    print(f"  merchant confirmed -> sellable={b['sellable']}")
    st, b = call("POST", "/quote", {"product_id": stale_pid, "qty": 1})
    print(f"  HTTP {st}: quote now issued from {b['quote']['price_source']}")
else:
    print("  (no stale candidate in this search; skipped)")

# ---- Scene 4: tamper ----
scene(4, "tamper the quote amount -> refused")
body_b64, sig = q["quote_token"].split(".", 1)
payload = json.loads(base64.urlsafe_b64decode(body_b64 + "=" * (-len(body_b64) % 4))); payload["amount_paise"] = 1
forged = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode().rstrip("=") + "." + sig
st, b = call("POST", "/checkout", {"quote_token": forged, "mandate_token": mandate_tok}, {"X-Agent-Id": AGENT})
print(f"  HTTP {st} {b['detail']['rule']}")

# ---- Scene 5: daily cap ----
scene(5, "second purchase over daily cap -> refused + escalated")
_, q2 = call("POST", "/quote", {"product_id": pid, "qty": 1})
st, b = call("POST", "/checkout", {"quote_token": q2["quote_token"], "mandate_token": mandate_tok}, {"X-Agent-Id": AGENT})
print(f"  HTTP {st} {b['detail']['rule']} escalated={b['detail']['escalated_to_human']}")
if b["detail"]["escalated_to_human"]:
    STATS["escalations"] += 1

# ---- Scene 6: no agent id, ledger ----
scene(6, "no X-Agent-Id -> 401; verify ledger")
st, b = call("POST", "/checkout", {"quote_token": q2["quote_token"], "mandate_token": mandate_tok})
print(f"  HTTP {st} {b['detail']['rule']}")
st, v = call("GET", "/ledger/verify")
print(f"  ledger: {v}")
print(f"\nSTATS {STATS}")
sys.exit(0 if v.get("ok") and STATS["assumptive_decisions"] == 0 else 1)
