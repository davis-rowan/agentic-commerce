"""Kaggle Flipkart catalog -> profile -> normalize -> catalog.db + quality_report.json.

    python ingest.py                 # downloads via the kaggle API (needs ~/.kaggle/kaggle.json)
    python ingest.py --csv path.csv  # use a local copy

Prints a one-screen summary of every data problem found and how much of the catalog is
sellable to an AI buyer before and after the two extra sources a merchant must supply
(price confirmations, stock snapshot). Both extra sources are SIMULATED and labelled as such.
"""
import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import catalog

HERE = Path(__file__).parent
DATA = HERE / "data"
DATASET = "PromptCloudHQ/flipkart-products"
KEY_SYNONYMS = {"colour": "color", "warranty_summary": "warranty", "model_name": "model", "net_quantity": "quantity"}
CONF_SOURCE, CONF_AS_OF = "erp:price_confirmation_sim_2026-09", "2026-09-01T09:00:00+00:00"
STOCK_SOURCE = "wms:stock_snapshot_sim"


def download() -> Path:
    DATA.mkdir(exist_ok=True)
    existing = list(DATA.glob("*.csv"))
    if existing:
        return existing[0]
    try:
        from kaggle.api.kaggle_api_extended import KaggleApi
        api = KaggleApi(); api.authenticate()
        api.dataset_download_files(DATASET, path=str(DATA), unzip=True)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Kaggle download failed: {e}\nPut kaggle.json in ~/.kaggle (Kaggle > Settings > API) or pass --csv <file>.")
    found = list(DATA.glob("*.csv"))
    if not found:
        sys.exit("download finished but no CSV found in data/")
    return found[0]


def norm_key(k: str) -> str:
    k = "_".join(str(k).strip().lower().split())
    return KEY_SYNONYMS.get(k, k)


def parse_specs(s):
    if not isinstance(s, str) or not s.strip():
        return {}, "blank"
    try:
        obj = json.loads(s.replace("=>", ":"))
    except Exception:  # noqa: BLE001
        return {}, "unparsable"
    out = {}
    for it in obj.get("product_specification", []) or []:
        if isinstance(it, dict) and "key" in it and "value" in it:
            out[norm_key(it["key"])] = str(it["value"]).strip()
    return out, ("ok" if out else "empty")


def to_paise(x):
    return None if pd.isna(x) else int(round(float(x) * 100))


def to_iso(ts: str) -> str:
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S %z").isoformat()


def category_path(tree) -> str | None:
    if not isinstance(tree, str):
        return None
    t = tree.strip().strip("[]").strip().strip('"')
    return t or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    csv = Path(args.csv) if args.csv else download()
    df = pd.read_csv(csv)
    n = len(df)
    print(f"raw rows: {n}  columns: {list(df.columns)}")

    report = {"dataset": DATASET, "file": csv.name, "rows": n,
              "null_pct": {c: round(100 * float(df[c].isna().mean()), 2) for c in df.columns}}

    # --- prices: two columns, misread trap ---
    rp, dp = df["retail_price"], df["discounted_price"]
    report["price"] = {
        "missing_list_price": int(rp.isna().sum()), "missing_sell_price": int(dp.isna().sum()),
        "sell_price_gt_list_price": int((dp > rp).sum()),
        "rows_where_two_prices_differ": int(((dp != rp) & dp.notna() & rp.notna()).sum()),
    }
    ages = (datetime.now(timezone.utc) - pd.to_datetime(df["crawl_timestamp"], utc=True)).dt.days
    report["price"]["age_days"] = {"min": int(ages.min()), "median": int(ages.median()), "max": int(ages.max())}

    # --- specs, ratings, brands ---
    parsed = df["product_specifications"].map(parse_specs)
    status = parsed.map(lambda t: t[1]).value_counts().to_dict()
    report["specs"] = {k: int(v) for k, v in status.items()}
    keys_raw = {}
    for d in parsed.map(lambda t: t[0]):
        for k in d:
            keys_raw[k] = keys_raw.get(k, 0) + 1
    report["specs"]["distinct_keys_after_normalisation"] = len(keys_raw)
    report["specs"]["top_keys"] = sorted(keys_raw.items(), key=lambda kv: -kv[1])[:12]
    rating_num = pd.to_numeric(df["product_rating"], errors="coerce")
    report["rating_non_numeric"] = int(rating_num.isna().sum())
    report["brand_blank"] = int(df["brand"].isna().sum())

    # --- duplicates and conflicts ---
    df["_ts"] = pd.to_datetime(df["crawl_timestamp"], utc=True)
    dup_rows = int(df.duplicated("pid").sum())
    conflicts, conflict_pids = [], set()
    for pid, g in df.groupby("pid"):
        prices = sorted(set(g["discounted_price"].dropna().tolist()))
        if len(prices) > 1:
            conflict_pids.add(pid)
            conflicts.append((pid, "sell_price_paise", json.dumps([int(round(p * 100)) for p in prices])))
    report["duplicates"] = {"duplicate_rows": dup_rows, "unique_pids": int(df["pid"].nunique()), "pids_with_conflicting_price": len(conflict_pids)}

    # --- normalise: keep latest crawl per pid ---
    latest = df.sort_values("_ts", ascending=False).drop_duplicates("pid")
    products, specs = [], []
    for row in latest.itertuples(index=False):
        sp, _ = parse_specs(row.product_specifications)
        pid = row.pid
        products.append({
            "product_id": pid, "uniq_id": row.uniq_id, "name": row.product_name,
            "brand": None if pd.isna(row.brand) else row.brand, "category": category_path(row.product_category_tree),
            "list_price_paise": to_paise(row.retail_price), "sell_price_paise": to_paise(row.discounted_price),
            "price_as_of": to_iso(row.crawl_timestamp), "description": None if pd.isna(row.description) else row.description,
            "rating": None if pd.isna(rn := pd.to_numeric(row.product_rating, errors="coerce")) else float(rn),
            "price_conflict": 1 if pid in conflict_pids else 0, "crawl_ts": to_iso(row.crawl_timestamp),
        })
        specs.extend((pid, k, v) for k, v in sp.items())
    catalog.build(products, specs, conflicts)
    cov_raw = catalog.coverage()

    # --- simulated merchant sources (labelled as such) ---
    rnd = random.Random(args.seed)
    priced = [p for p in products if p["sell_price_paise"] is not None and not p["price_conflict"]]
    confirmed = rnd.sample(priced, k=len(priced) // 4)
    catalog.add_confirmations([(p["product_id"], p["sell_price_paise"], CONF_SOURCE, CONF_AS_OF) for p in confirmed])
    pd.DataFrame([{"product_id": p["product_id"], "sell_price_paise": p["sell_price_paise"], "source": CONF_SOURCE, "as_of": CONF_AS_OF} for p in confirmed]) \
        .to_csv(HERE / "price_confirmations.csv", index=False)
    cov_conf = catalog.coverage()

    stock_as_of = datetime.now(timezone.utc).isoformat()
    stock_rows = []
    for p in rnd.sample(confirmed, k=int(len(confirmed) * 0.6)) + rnd.sample(products, k=len(products) // 10):
        qty = 0 if rnd.random() < 0.1 else rnd.randint(1, 40)
        stock_rows.append((p["product_id"], qty, STOCK_SOURCE, stock_as_of))
    catalog.add_stock(stock_rows)
    pd.DataFrame(stock_rows, columns=["product_id", "qty", "source", "as_of"]).to_csv(HERE / "stock_snapshot.csv", index=False)
    cov_stock = catalog.coverage()

    report["coverage"] = {"raw_crawl_only": cov_raw, "after_price_confirmations": cov_conf, "after_stock_snapshot": cov_stock,
                          "note": "price_confirmations.csv and stock_snapshot.csv are simulated stand-ins for the ERP/WMS feeds a real merchant must supply"}
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    (HERE / "quality_report.json").write_text(json.dumps(report, indent=2))

    print(f"\n== quality summary ({n} rows, {report['duplicates']['unique_pids']} unique pids) ==")
    print(f"missing sell price: {report['price']['missing_sell_price']}   two prices differ: {report['price']['rows_where_two_prices_differ']}   "
          f"price age days median: {report['price']['age_days']['median']}")
    print(f"specs: {report['specs']}")
    print(f"rating non-numeric: {report['rating_non_numeric']}   brand blank: {report['brand_blank']}   "
          f"dup rows: {dup_rows}   conflicting-price pids: {len(conflict_pids)}")
    for k in ("raw_crawl_only", "after_price_confirmations", "after_stock_snapshot"):
        c = report["coverage"][k]
        print(f"sellable {k:28s}: {c['sellable']:5d} / {c['total']} ({c['sellable_pct']}%)  {c['unsellable_by_reason']}")
    print("\nwrote catalog.db, quality_report.json, price_confirmations.csv, stock_snapshot.csv")


if __name__ == "__main__":
    main()
