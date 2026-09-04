# Verifiable Storefront for AI Buyers

A merchant backend that an AI buying agent can transact with safely, on Razorpay test mode,
built on a real and dirty catalog: the Kaggle Flipkart dataset (20,000 products).
Track: "AI Growth & Agentic Commerce".

## The problem it answers
Agents fail on merchants because of clarity failures, not payment failures: dirty catalogs,
hallucinated fills, unconfirmed actions, and no way to tell a legitimate agent from a bot.
This merchant only answers in verifiable form, and it refuses rather than guesses.

## Four guarantees
1. **No hallucinated fields.** Every catalog field is `{value, source, as_of}`. A field that is not
   on record is answered `"unknown"`. `POST /ask` never infers from name or description.
2. **Prices are tokens, not numbers.** `POST /quote` returns an HMAC-signed, 120-second quote token
   computed from exactly one named price (`sell_price_paise`). `POST /checkout` re-verifies signature
   and expiry. Tampered or stale means refused.
3. **Spend is bounded by a signed mandate.** `POST /mandate` simulates a human authorising an agent
   with a per-transaction cap, daily cap and expiry. The agent cannot edit its own caps. A breach is
   refused and written with `escalate_to_human: true`. Callers without `X-Agent-Id` get 401.
4. **Hash-chained audit log.** Every request appends a row with `prev_hash` and `hash` to SQLite.
   `GET /ledger/verify` recomputes the chain and reports the first broken link.

## What the real data taught us
`ingest.py` profiled `flipkart_com-ecommerce_sample.csv` (PromptCloudHQ/flipkart-products) and wrote `quality_report.json`.

| Problem measured in the raw CSV | Count | Why an agent breaks on it | Rule in this merchant |
|---|---|---|---|
| Rows where `retail_price` and `discounted_price` differ | 17,635 of 20,000 | Agents misread the price next to the one they want | Quote uses one named `sell_price_paise`; `list_price_paise` is informational only |
| Rows with no sell price | 78 | Agent invents a price | `REFUSED_NO_PRICE` |
| Median price age (crawl timestamp) | 3,900 days | A 2015 price quoted as current | `REFUSED_PRICE_STALE` until a human re-confirms via `POST /merchant/confirm_price` |
| Distinct spec keys after normalisation | 2,125 | Inconsistent attributes make specs unreliable to compare | Only parsed keys are served, each tagged `specs_parsed`; nothing derived from description |
| Spec strings unparsable, empty or blank | 53 / 33 / 14 | Agents fill the gap from prose | Those products simply have no spec fields |
| `product_rating` non-numeric ("No rating available") | 18,151 | Type confusion | Typed parse, non-numeric becomes unknown |
| `brand` blank | 5,864 | Agent guesses brand from the name | Unknown, never derived |
| Duplicate `pid` rows / conflicting prices | 2 / 0 | Two truths, one picked silently | Latest crawl kept; a conflict would set `REFUSED_PRICE_CONFLICT` |
| Stock column | none | Agent assumes availability | `REFUSED_NO_STOCK` until a stock source exists |

### Sellable coverage as a merchant adds the sources agents need
| Stage | Sellable products | Blocking reasons |
|---|---|---|
| Crawl data only | 0 of 19,998 (0%) | PRICE_STALE 19,920, NO_PRICE 78 |
| + price confirmations (simulated ERP feed, 25% of catalog) | 0 (0%) | PRICE_STALE 14,940, NO_STOCK 4,980, NO_PRICE 78 |
| + stock snapshot (simulated WMS feed) | 2,878 (14.4%) | PRICE_STALE 14,940, NO_STOCK 1,786, OUT_OF_STOCK 316, NO_PRICE 78 |

The takeaway for a merchant: a scraped or exported catalog is worth nothing to an AI buyer until
fresh prices and a stock source are attached. The two CSVs that lift coverage here
(`price_confirmations.csv`, `stock_snapshot.csv`) are simulated and labelled as such in every field
they touch. The dashboard shows the live number climbing as merchant confirmations arrive.

## Run
```
python -m pip install -r requirements.txt
copy .env.example .env        # paste Razorpay test keys; stub mode until you do
python ingest.py              # needs a Kaggle API token in %USERPROFILE%\.kaggle\access_token
python -m pytest -q           # 7 rule tests
python -m uvicorn merchant:app
python buyer_agent.py         # or open http://127.0.0.1:8000 and press "Run demo"
```

## Endpoints
| Route | Behaviour |
|---|---|
| `GET /search?q=` | Deterministic substring match on name/brand/category, returns real ids with `matched_on` |
| `GET /product/{id}` | All known fields wrapped `{value, source, as_of}`, plus `sellable` and `unsellable_reason` |
| `POST /ask` | One field or `unknown` |
| `GET /quality` | The ingest report plus live coverage |
| `POST /quote` | Signed quote or a refusal naming the sellability rule |
| `POST /merchant/confirm_price` | Human on the merchant side re-confirms price and stock (`X-Merchant-Key`) |
| `POST /mandate` | Signed spend mandate for an agent |
| `POST /checkout` | Verifies quote, mandate, caps; creates a Razorpay test order |
| `GET /ledger`, `GET /ledger/verify` | Chain dump and verification |

## Buyer agent demo
Intent: a water bottle under Rs 1,000 whose colour is on record. The agent searches, drops every
candidate with an explicit reason (unsellable, over budget, required field unknown), picks the first
fully known one, and checks out under a mandate. It then shows a stale-price refusal cleared by a
merchant confirmation, a tampered quote, a daily-cap breach, and an unidentified caller. It ends by
verifying the ledger and printing `assumptive_decisions: 0`.

## Files
| File | Purpose |
|---|---|
| `ingest.py` | Kaggle download, profiling, normalisation, simulated feeds, quality report |
| `catalog.py` | SQLite catalog, sellability rules, deterministic search, merchant confirmations |
| `merchant.py` | FastAPI app |
| `ledger.py` | Hash-chained append-only log |
| `buyer_agent.py` | Scripted intent-driven buyer |
| `dashboard.html` | Served at `/`: data-quality panel, coverage, search, demo runner, ledger |
| `tests/test_rules.py` | Rule tests on a synthetic catalog |

## Out of scope
LLM-driven buyer, fuzzy or semantic search, payment capture and webhooks, multi-merchant, a real WMS or ERP integration.
Delete `ledger.db` to reset daily spend counters between demos; rerun `ingest.py` to reset merchant confirmations.
