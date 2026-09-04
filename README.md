# Verifiable Storefront for AI Buyers

A tiny merchant backend that an AI buying agent can transact with safely, on Razorpay test mode.
Built for the "AI Growth & Agentic Commerce" track.

## The problem it answers
Agents fail on merchants because of clarity failures, not payment failures: dirty catalogs,
hallucinated fills, unconfirmed actions, and no way to tell a legitimate agent from a bot.
This merchant only answers in verifiable form.

## Four guarantees
1. **No hallucinated fields.** Every catalog field is `{value, source, as_of}`. A field that is not
   on record is answered `"unknown"`. `POST /ask` never infers or defaults.
2. **Prices are tokens, not numbers.** `POST /quote` returns an HMAC-signed, 120-second quote token.
   `POST /checkout` re-verifies signature and expiry. Tampered or stale means refused.
3. **Spend is bounded by a signed mandate.** `POST /mandate` simulates a human authorising an agent
   with a per-transaction cap, daily cap and expiry. The agent cannot edit its own caps. A breach is
   refused and written with `escalate_to_human: true`. Callers without `X-Agent-Id` get 401 and are
   treated as bot traffic.
4. **Hash-chained audit log.** Every request appends a row with `prev_hash` and `hash` to SQLite.
   `GET /ledger/verify` recomputes the chain and reports the first broken link.

## Run
```
pip install -r requirements.txt
uvicorn merchant:app --reload
python buyer_agent.py
```
Paste your Razorpay **test** keys into `.env`. Until you do, checkout runs in a clearly labelled
`stub` mode and fabricates order ids locally. With real keys, `razorpay_mode` becomes
`razorpay_test` and the order appears in your Razorpay test dashboard.

## Demo script (what buyer_agent.py prints)
| Scene | What happens |
|---|---|
| 1 | Ask a known field, get value + source + as_of |
| 2 | Ask a missing field, get `unknown`; agent escalates instead of guessing |
| 3 | Quote + checkout inside mandate, Razorpay order id returned |
| 4 | Tamper the quote amount, refused: `REFUSED_QUOTE_INVALID` |
| 5 | Second purchase exceeds daily cap, refused: `REFUSED_MANDATE_DAILY`, escalated |
| 5b | No `X-Agent-Id`, 401 `NO_AGENT_ID` |
| 6 | `/ledger/verify` returns `ok: true` |

To show tamper detection on the log itself, edit any row in `ledger.db` and call `/ledger/verify` again.

## Files
| File | Purpose |
|---|---|
| `merchant.py` | FastAPI app: catalog, ask, quote, mandate, checkout, ledger endpoints |
| `ledger.py` | Hash-chained append-only log + verifier + daily spend query |
| `catalog.json` | Six products, some fields deliberately missing |
| `buyer_agent.py` | Scripted buyer running the demo scenes |

## Out of scope
Payment capture and webhooks, multi-merchant, an LLM-driven buyer, auth beyond HMAC mandates.
Delete `ledger.db` to reset the daily spend counters between demos.
