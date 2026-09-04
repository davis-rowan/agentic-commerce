"""Hash-chained, append-only audit log in SQLite.

Each row carries prev_hash (the previous row's hash) and hash = sha256(row minus hash).
verify() recomputes the chain and reports the first broken link, if any.
"""
import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DB = Path(os.environ.get("LEDGER_DB", Path(__file__).with_name("ledger.db")))
GENESIS = "0" * 64
COLS = ["ts", "step", "request", "rule_fired", "decision", "razorpay_ref", "prev_hash", "hash"]


def _conn():
    c = sqlite3.connect(DB)
    c.execute(
        "CREATE TABLE IF NOT EXISTS ledger("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, step TEXT, request TEXT, "
        "rule_fired TEXT, decision TEXT, razorpay_ref TEXT, prev_hash TEXT, hash TEXT)"
    )
    return c


def _digest(row: dict) -> str:
    body = {k: row[k] for k in COLS if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


_LOCK = threading.Lock()  # appends must be serial: read head -> hash -> insert is one atomic step


def append(step: str, request: dict, rule_fired: str, decision: str, razorpay_ref: str | None = None) -> dict:
    with _LOCK:
        c = _conn()
        c.isolation_level = None  # manual transaction control
        c.execute("BEGIN IMMEDIATE")  # also serialises against other processes
        last = c.execute("SELECT hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "step": step,
            "request": json.dumps(request, sort_keys=True),
            "rule_fired": rule_fired,
            "decision": decision,
            "razorpay_ref": razorpay_ref,
            "prev_hash": last[0] if last else GENESIS,
        }
        row["hash"] = _digest(row)
        c.execute(f"INSERT INTO ledger({','.join(COLS)}) VALUES({','.join('?' * len(COLS))})", [row[k] for k in COLS])
        seq = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("COMMIT")
        c.close()
        return {"seq": seq, **row}


def rows() -> list[dict]:
    c = _conn()
    out = [dict(zip(["seq", *COLS], r)) for r in c.execute(f"SELECT seq,{','.join(COLS)} FROM ledger ORDER BY seq")]
    c.close()
    for r in out:
        r["request"] = json.loads(r["request"])
    return out


def verify() -> dict:
    c = _conn()
    prev = GENESIS
    for r in c.execute(f"SELECT seq,{','.join(COLS)} FROM ledger ORDER BY seq"):
        row = dict(zip(["seq", *COLS], r))
        if row["prev_hash"] != prev:
            return {"ok": False, "broken_at_seq": row["seq"], "reason": "prev_hash mismatch"}
        if _digest(row) != row["hash"]:
            return {"ok": False, "broken_at_seq": row["seq"], "reason": "row hash mismatch (content altered)"}
        prev = row["hash"]
    n = c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    c.close()
    return {"ok": True, "rows": n, "head": prev}


def approved_total_today(agent_id: str) -> int:
    """Sum of amount_paise approved for this agent since 00:00 UTC today (read from the chain, not a cache)."""
    today = datetime.now(timezone.utc).date().isoformat()
    total = 0
    for r in rows():
        if r["step"] == "checkout" and r["decision"] == "APPROVED" and r["ts"].startswith(today):
            if r["request"].get("agent_id") == agent_id:
                total += int(r["request"].get("amount_paise", 0))
    return total
