"""Read side of the catalog (SQLite, built by ingest.py).

Every served field is {value, source, as_of}. Anything not on record is simply absent (=> "unknown").
Sellability is decided here from plain rules so merchant.py and ingest.py share one truth.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB = Path(os.environ.get("CATALOG_DB", Path(__file__).with_name("catalog.db")))
MAX_PRICE_AGE_DAYS = int(os.environ.get("MAX_PRICE_AGE_DAYS", "30"))

SCHEMA = """
CREATE TABLE products(product_id TEXT PRIMARY KEY, uniq_id TEXT, name TEXT, brand TEXT, category TEXT,
  list_price_paise INTEGER, sell_price_paise INTEGER, price_as_of TEXT, description TEXT, rating REAL,
  price_conflict INTEGER DEFAULT 0, crawl_ts TEXT);
CREATE TABLE specs(product_id TEXT, key TEXT, value TEXT);
CREATE INDEX specs_pid ON specs(product_id);
CREATE TABLE stock(product_id TEXT PRIMARY KEY, qty INTEGER, source TEXT, as_of TEXT);
CREATE TABLE confirmations(id INTEGER PRIMARY KEY AUTOINCREMENT, product_id TEXT, sell_price_paise INTEGER, source TEXT, as_of TEXT);
CREATE INDEX conf_pid ON confirmations(product_id);
CREATE TABLE conflicts(product_id TEXT, field TEXT, values_json TEXT);
"""


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- build (used by ingest.py and tests) ----------

def build(products: list[dict], specs: list[tuple], conflicts: list[tuple]):
    if DB.exists():
        DB.unlink()
    c = _conn()
    c.executescript(SCHEMA)
    cols = ["product_id", "uniq_id", "name", "brand", "category", "list_price_paise", "sell_price_paise",
            "price_as_of", "description", "rating", "price_conflict", "crawl_ts"]
    c.executemany(f"INSERT INTO products({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                  [[p.get(k) for k in cols] for p in products])
    c.executemany("INSERT INTO specs VALUES(?,?,?)", specs)
    c.executemany("INSERT INTO conflicts VALUES(?,?,?)", conflicts)
    c.commit(); c.close()


def add_stock(rows: list[tuple]):
    """rows: (product_id, qty, source, as_of)"""
    c = _conn()
    c.executemany("INSERT OR REPLACE INTO stock VALUES(?,?,?,?)", rows)
    c.commit(); c.close()


def add_confirmations(rows: list[tuple]):
    """rows: (product_id, sell_price_paise, source, as_of)"""
    c = _conn()
    c.executemany("INSERT INTO confirmations(product_id, sell_price_paise, source, as_of) VALUES(?,?,?,?)", rows)
    c.commit(); c.close()


def confirm_price(product_id: str, sell_price_paise: int, stock_qty: int | None, source: str) -> bool:
    c = _conn()
    if c.execute("SELECT 1 FROM products WHERE product_id=?", (product_id,)).fetchone() is None:
        c.close(); return False
    ts = now_iso()
    c.execute("INSERT INTO confirmations(product_id, sell_price_paise, source, as_of) VALUES(?,?,?,?)",
              (product_id, sell_price_paise, source, ts))
    if stock_qty is not None:
        c.execute("INSERT OR REPLACE INTO stock VALUES(?,?,?,?)", (product_id, stock_qty, source, ts))
    c.commit(); c.close()
    return True


# ---------- sellability: one rule set ----------

def _age_days(iso: str | None) -> float | None:
    if not iso:
        return None
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400


def _decide(sell_price, conflict, price_as_of, confirmed, qty):
    """Returns (ok, reason). Order matters: the first failing rule is the reason reported."""
    if sell_price is None:
        return False, "NO_PRICE"
    if conflict and not confirmed:
        return False, "PRICE_CONFLICT"
    age = _age_days(price_as_of)
    if age is None or age > MAX_PRICE_AGE_DAYS:
        return False, "PRICE_STALE"
    if qty is None:
        return False, "NO_STOCK"
    if qty <= 0:
        return False, "OUT_OF_STOCK"
    return True, None


_EFFECTIVE_SQL = """
SELECT p.*, c.sell_price_paise AS c_price, c.source AS c_source, c.as_of AS c_as_of,
       s.qty AS s_qty, s.source AS s_source, s.as_of AS s_as_of
FROM products p
LEFT JOIN (SELECT product_id, sell_price_paise, source, as_of FROM confirmations
           WHERE id IN (SELECT MAX(id) FROM confirmations GROUP BY product_id)) c ON c.product_id = p.product_id
LEFT JOIN stock s ON s.product_id = p.product_id
"""


def _effective(r: sqlite3.Row) -> dict:
    """Pick the price that counts: latest merchant confirmation beats the crawl."""
    confirmed = r["c_price"] is not None
    sell = r["c_price"] if confirmed else r["sell_price_paise"]
    src = r["c_source"] if confirmed else f"flipkart_crawl:{r['uniq_id']}"
    as_of = r["c_as_of"] if confirmed else r["price_as_of"]
    ok, reason = _decide(sell, r["price_conflict"], as_of, confirmed, r["s_qty"])
    return {"sell": sell, "src": src, "as_of": as_of, "confirmed": confirmed, "ok": ok, "reason": reason}


def sellability(product_id: str) -> tuple[bool, str | None]:
    c = _conn()
    r = c.execute(_EFFECTIVE_SQL + " WHERE p.product_id=?", (product_id,)).fetchone()
    c.close()
    if r is None:
        return False, "UNKNOWN_PRODUCT"
    e = _effective(r)
    return e["ok"], e["reason"]


def coverage() -> dict:
    c = _conn()
    reasons: dict[str, int] = {}
    ok = total = 0
    for r in c.execute(_EFFECTIVE_SQL):
        total += 1
        e = _effective(r)
        if e["ok"]:
            ok += 1
        else:
            reasons[e["reason"]] = reasons.get(e["reason"], 0) + 1
    c.close()
    return {"total": total, "sellable": ok, "sellable_pct": round(100 * ok / total, 2) if total else 0, "unsellable_by_reason": reasons}


# ---------- read ----------

def get(product_id: str) -> dict | None:
    c = _conn()
    r = c.execute(_EFFECTIVE_SQL + " WHERE p.product_id=?", (product_id,)).fetchone()
    if r is None:
        c.close(); return None
    crawl_src, crawl_ts = f"flipkart_crawl:{r['uniq_id']}", r["crawl_ts"]
    f = lambda v: None if v is None else {"value": v, "source": crawl_src, "as_of": crawl_ts}
    out = {k: f(r[k]) for k in ("name", "brand", "category", "description", "rating", "list_price_paise")}
    e = _effective(r)
    if e["sell"] is not None:
        out["sell_price_paise"] = {"value": e["sell"], "source": e["src"], "as_of": e["as_of"]}
    if r["s_qty"] is not None:
        out["stock"] = {"value": r["s_qty"], "source": r["s_source"], "as_of": r["s_as_of"]}
    for s in c.execute("SELECT key, value FROM specs WHERE product_id=?", (product_id,)):
        out.setdefault(f"spec.{s['key']}", {"value": s["value"], "source": f"{crawl_src}:specs_parsed", "as_of": crawl_ts})
    conf = [dict(x) for x in c.execute("SELECT field, values_json FROM conflicts WHERE product_id=?", (product_id,))]
    c.close()
    fields = {k: v for k, v in out.items() if v is not None}
    return {"fields": fields, "sellable": e["ok"], "unsellable_reason": e["reason"],
            "conflicts": [{"field": x["field"], "values": json.loads(x["values_json"])} for x in conf]}


def search(q: str, limit: int = 20) -> list[dict]:
    """Deterministic: every token must appear in name, brand or category (case-insensitive substring)."""
    tokens = [t.lower() for t in q.split() if t.strip()]
    if not tokens:
        return []
    where = " AND ".join("(lower(coalesce(name,'')) LIKE ? OR lower(coalesce(brand,'')) LIKE ? OR lower(coalesce(category,'')) LIKE ?)" for _ in tokens)
    params = [f"%{t}%" for t in tokens for _ in range(3)]
    c = _conn()
    rows = c.execute(f"SELECT product_id, name, brand, category FROM products WHERE {where} ORDER BY product_id LIMIT ?", [*params, limit]).fetchall()
    c.close()
    out = []
    for r in rows:
        matched = sorted({fld for t in tokens for fld in ("name", "brand", "category") if t in (r[fld] or "").lower()})
        out.append({"product_id": r["product_id"], "name": r["name"], "matched_on": matched})
    return out


def page(offset: int = 0, limit: int = 50) -> list[dict]:
    c = _conn()
    ids = [r["product_id"] for r in c.execute("SELECT product_id FROM products ORDER BY product_id LIMIT ? OFFSET ?", (limit, offset))]
    c.close()
    return [{"product_id": i, **get(i)} for i in ids]
