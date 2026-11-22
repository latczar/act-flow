"""Case database and the append-only, hash-chained audit log (SQLite)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager

from . import config

LOCK = threading.RLock()   # one case-processing transaction at a time

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases(
    ref TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, status TEXT, reason TEXT,
    sender TEXT, subject TEXT, client_id TEXT, instruction_type TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS messages(
    message_id TEXT PRIMARY KEY, case_ref TEXT, direction TEXT, path TEXT,
    received_at TEXT, sha256 TEXT, headers TEXT);
CREATE TABLE IF NOT EXISTS documents(
    id INTEGER PRIMARY KEY AUTOINCREMENT, case_ref TEXT, message_id TEXT, filename TEXT,
    path TEXT, sha256 TEXT, size INTEGER, doc_type TEXT, pages TEXT, received_at TEXT);
CREATE TABLE IF NOT EXISTS steps(
    case_ref TEXT, seq INTEGER, system TEXT, op TEXT, status TEXT, attempts INTEGER,
    idem_key TEXT, result TEXT, error TEXT, PRIMARY KEY(case_ref, seq));
CREATE TABLE IF NOT EXISTS writes(
    idem_key TEXT PRIMARY KEY, case_ref TEXT, system TEXT, collection TEXT, record_id TEXT,
    before TEXT, after TEXT, at TEXT, undone INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER);
CREATE TABLE IF NOT EXISTS audit(
    seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, case_ref TEXT, actor TEXT,
    event TEXT, detail TEXT, prev_hash TEXT, hash TEXT);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
    BEGIN SELECT RAISE(ABORT, 'The audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
    BEGIN SELECT RAISE(ABORT, 'The audit log is append-only'); END;
"""


def connect() -> sqlite3.Connection:
    path = config.data_dir() / "act.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


@contextmanager
def session():
    with LOCK:
        con = connect()
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()


def dumps(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


# ---- cases -------------------------------------------------------------------

def next_ref(con) -> str:
    year = config.today().year
    name = f"case-{year}"
    row = con.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
    n = (row["value"] if row else 0) + 1
    con.execute("INSERT INTO counters(name, value) VALUES(?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, n))
    return f"ACT-{year}-{n:06d}"


def create_case(con, sender: str, subject: str) -> dict:
    ref, at = next_ref(con), config.now_iso()
    con.execute("INSERT INTO cases(ref, created_at, updated_at, status, sender, subject, data) "
                "VALUES(?, ?, ?, 'received', ?, ?, '{}')", (ref, at, at, sender, subject))
    audit(con, ref, "system", "case_opened", sender=sender, subject=subject)
    return get_case(con, ref)


def get_case(con, ref: str) -> dict | None:
    row = con.execute("SELECT * FROM cases WHERE ref=?", (ref,)).fetchone()
    if not row:
        return None
    case = dict(row)
    case["data"] = json.loads(case["data"] or "{}")
    return case


def list_cases(con) -> list[dict]:
    rows = con.execute("SELECT ref FROM cases ORDER BY created_at DESC, ref DESC").fetchall()
    return [get_case(con, r["ref"]) for r in rows]


def save_case(con, case: dict) -> None:
    case["updated_at"] = config.now_iso()
    con.execute("UPDATE cases SET updated_at=?, status=?, reason=?, sender=?, subject=?, client_id=?, "
                "instruction_type=?, data=? WHERE ref=?",
                (case["updated_at"], case["status"], case.get("reason"), case.get("sender"),
                 case.get("subject"), case.get("client_id"), case.get("instruction_type"),
                 dumps(case["data"]), case["ref"]))


def set_status(con, case: dict, status: str, actor: str = "system", reason: str | None = None) -> None:
    old = case["status"]
    case["status"], case["reason"] = status, reason
    save_case(con, case)
    audit(con, case["ref"], actor, "status_changed", **{"from": old, "to": status, "reason": reason})


def case_messages(con, ref: str, direction: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM messages WHERE case_ref=?", [ref]
    if direction:
        sql += " AND direction=?"
        args.append(direction)
    rows = con.execute(sql + " ORDER BY received_at, rowid", args).fetchall()
    out = []
    for r in rows:
        m = dict(r)
        m["headers"] = json.loads(m["headers"] or "{}")
        out.append(m)
    return out


def case_documents(con, ref: str) -> list[dict]:
    rows = con.execute("SELECT * FROM documents WHERE case_ref=? ORDER BY id", (ref,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["pages"] = json.loads(d["pages"]) if d["pages"] else None
        out.append(d)
    return out


def case_steps(con, ref: str) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM steps WHERE case_ref=? ORDER BY seq", (ref,))]


def case_writes(con, ref: str) -> list[dict]:
    out = []
    for r in con.execute("SELECT * FROM writes WHERE case_ref=? ORDER BY at, rowid", (ref,)):
        w = dict(r)
        w["before"], w["after"] = json.loads(w["before"]), json.loads(w["after"])
        out.append(w)
    return out


# ---- audit log -----------------------------------------------------------------

GENESIS = "0" * 64


def _hash(prev: str, at: str, case_ref: str, actor: str, event: str, detail: str) -> str:
    body = "|".join([prev, at, case_ref, actor, event, detail])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def audit(con, case_ref: str | None, actor: str, event: str, **detail) -> None:
    """Append an event. Each event carries the hash of the one before, so edits break the chain."""
    last = con.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
    prev = last["hash"] if last else GENESIS
    at, ref, body = config.now_iso(), case_ref or "", dumps(detail)
    con.execute("INSERT INTO audit(at, case_ref, actor, event, detail, prev_hash, hash) VALUES(?,?,?,?,?,?,?)",
                (at, ref, actor, event, body, prev, _hash(prev, at, ref, actor, event, body)))


def verify_audit(con) -> dict:
    prev, count = GENESIS, 0
    for row in con.execute("SELECT * FROM audit ORDER BY seq"):
        count += 1
        expected = _hash(prev, row["at"], row["case_ref"], row["actor"], row["event"], row["detail"])
        if row["prev_hash"] != prev or row["hash"] != expected:
            return {"ok": False, "events": count, "broken_at": row["seq"]}
        prev = row["hash"]
    return {"ok": True, "events": count, "broken_at": None}


def timeline(con, ref: str) -> list[dict]:
    rows = con.execute("SELECT * FROM audit WHERE case_ref=? ORDER BY seq", (ref,)).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]
