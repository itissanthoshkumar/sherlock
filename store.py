"""SQLite audit store for DPD checks (dpd_checks.db, git-ignored).

Four tables capture the whole flow end to end:

  runs       — one per /api/check: the input (loan id, EMI) + AA-availability verdict
  accounts   — every bank account resolved by the SQL lookup for that run
  pulls      — one per AA-enabled account: the Digitap initiate->status->retrieve
               flow, its child txn id, retrieved balance and the EMI decision
  api_calls  — every Digitap request/response (initiate, statuscheck, retrieve)
               with bodies, HTTP status and timing, linked to its run + pull

`get_run()` reassembles a run with its nested accounts, pulls and api_calls.
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_FILE = Path(os.getenv("DPD_STORE_FILE") or (Path(__file__).resolve().parent / "dpd_checks.db"))
IST = timezone(timedelta(hours=5, minutes=30))  # project timezone (fixed +5:30, no DST)
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at    TEXT NOT NULL,
  loan_id       TEXT NOT NULL,
  emi_amount    REAL,
  aa_available  INTEGER,          -- 1/0
  account_count INTEGER,
  aa_count      INTEGER,
  status        TEXT,             -- PENDING / AA_NOT_AVAILABLE / DONE / ERROR
  error         TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id             INTEGER NOT NULL,
  bank_name          TEXT,
  account_ref        TEXT,
  is_repayment       INTEGER,          -- 1/0
  source             TEXT,
  fetched_at         TEXT,
  aa_enabled         INTEGER,          -- 1/0
  main_txn_id        TEXT,
  los_application_no TEXT,
  context_uid        TEXT,
  bank_account_uid   TEXT,
  branch_name        TEXT,
  ifsc               TEXT,
  account_holder_name TEXT,
  consent_id         TEXT,
  consent_expiry     TEXT,
  consent_status     TEXT,        -- NOT_LINKED / NEARING_EXPIRY / EXPIRED / ACTIVE / CONSENT_REQUESTED
  raw_row_json       TEXT
);
CREATE TABLE IF NOT EXISTS consents (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      INTEGER,
  account_id  INTEGER,
  handle      TEXT,
  url         TEXT,
  status      TEXT,
  expiry      TEXT,
  created_at  TEXT
);
CREATE TABLE IF NOT EXISTS pulls (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id            INTEGER NOT NULL,
  account_id        INTEGER,
  bank_name         TEXT,
  is_repayment      INTEGER,
  main_txn_id       TEXT,
  child_txn_id      TEXT,
  status            TEXT,         -- INITIATED / PROCESSING / RETRIEVED / FAILED
  available_balance REAL,
  currency          TEXT,
  decision          TEXT,         -- SUFFICIENT / INSUFFICIENT / INDETERMINATE
  error             TEXT,
  raw_report_json   TEXT,
  created_at        TEXT,
  updated_at        TEXT
);
CREATE TABLE IF NOT EXISTS api_calls (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        INTEGER,
  pull_id       INTEGER,
  kind          TEXT,             -- initiate / status / retrieve
  endpoint      TEXT,
  request_json  TEXT,
  response_json TEXT,
  http_status   INTEGER,
  ok            INTEGER,
  error         TEXT,
  created_at    TEXT
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _lock, _conn() as conn:
        conn.executescript(_SCHEMA)


def _now() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _j(v):
    return None if v is None else (v if isinstance(v, str) else json.dumps(v, default=str))


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------
def create_run(loan_id, emi_amount=None) -> int:
    init()
    with _lock, _conn() as conn:
        cur = conn.execute(
            "INSERT INTO runs (created_at, loan_id, emi_amount, status) VALUES (?,?,?,?)",
            (_now(), loan_id, emi_amount, "PENDING"),
        )
        return cur.lastrowid


def set_run_emi(run_id, emi_amount):
    with _lock, _conn() as conn:
        conn.execute("UPDATE runs SET emi_amount=? WHERE id=?", (emi_amount, run_id))


def finalize_run(run_id, *, aa_available, account_count, aa_count, status, error=None):
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE runs SET aa_available=?, account_count=?, aa_count=?, status=?, error=? WHERE id=?",
            (1 if aa_available else 0, account_count, aa_count, status, error, run_id),
        )


def add_account(run_id, acct: dict) -> int:
    with _lock, _conn() as conn:
        cur = conn.execute(
            "INSERT INTO accounts (run_id, bank_name, account_ref, is_repayment, source, fetched_at, "
            "aa_enabled, main_txn_id, los_application_no, context_uid, bank_account_uid, branch_name, "
            "ifsc, account_holder_name, consent_id, consent_expiry, consent_status, raw_row_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, acct.get("bank_name"), acct.get("account_ref"),
             1 if acct.get("is_repayment") else 0, acct.get("source"),
             str(acct.get("fetched_at")) if acct.get("fetched_at") is not None else None,
             1 if acct.get("aa_enabled") else 0, acct.get("main_txn_id"),
             acct.get("los_application_no"), acct.get("context_uid"), acct.get("bank_account_uid"),
             acct.get("branch_name"), acct.get("ifsc"), acct.get("account_holder_name"),
             acct.get("consent_id"),
             str(acct.get("consent_expiry")) if acct.get("consent_expiry") is not None else None,
             acct.get("consent_status"), _j(acct.get("raw_row"))),
        )
        return cur.lastrowid


def get_account(account_id):
    init()
    with _lock, _conn() as conn:
        r = conn.execute(
            "SELECT a.*, r.loan_id AS loan_id FROM accounts a "
            "LEFT JOIN runs r ON r.id = a.run_id WHERE a.id=?", (account_id,)
        ).fetchone()
    return dict(r) if r else None


def update_account(account_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock, _conn() as conn:
        conn.execute(f"UPDATE accounts SET {cols} WHERE id=?", (*fields.values(), account_id))


def save_consent(run_id, account_id, consent: dict) -> int:
    init()
    with _lock, _conn() as conn:
        cur = conn.execute(
            "INSERT INTO consents (run_id, account_id, handle, url, status, expiry, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, account_id, consent.get("handle"), consent.get("url"),
             consent.get("status"), consent.get("expiry"), _now()),
        )
        return cur.lastrowid


def add_pull(run_id, account_id, bank_name, is_repayment, main_txn_id, child_txn_id, status, error=None) -> int:
    with _lock, _conn() as conn:
        cur = conn.execute(
            "INSERT INTO pulls (run_id, account_id, bank_name, is_repayment, main_txn_id, child_txn_id, status, error, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, account_id, bank_name, 1 if is_repayment else 0, main_txn_id, child_txn_id, status, error, _now(), _now()),
        )
        return cur.lastrowid


def update_pull(pull_id, **fields):
    if not fields:
        return
    fields["updated_at"] = _now()
    if "raw_report_json" in fields:
        fields["raw_report_json"] = _j(fields["raw_report_json"])
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock, _conn() as conn:
        conn.execute(f"UPDATE pulls SET {cols} WHERE id=?", (*fields.values(), pull_id))


def log_api(run_id, pull_id, result: dict):
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO api_calls (run_id, pull_id, kind, endpoint, request_json, response_json, http_status, ok, error, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, pull_id, result.get("kind"), result.get("endpoint"),
             _j(result.get("request")), _j(result.get("response")),
             result.get("http_status"), 1 if result.get("ok") else 0,
             result.get("error"), _now()),
        )


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------
def get_pull(pull_id):
    init()
    with _lock, _conn() as conn:
        r = conn.execute("SELECT * FROM pulls WHERE id=?", (pull_id,)).fetchone()
    return dict(r) if r else None


def get_run(run_id):
    init()
    with _lock, _conn() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            return None
        accounts = conn.execute("SELECT * FROM accounts WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        pulls = conn.execute("SELECT * FROM pulls WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        calls = conn.execute("SELECT id, pull_id, kind, http_status, ok, created_at FROM api_calls WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        consents = conn.execute("SELECT * FROM consents WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    out = dict(run)
    # Account rows carry the bulky raw SQL row — drop it from the polled snapshot.
    out["accounts"] = [{k: v for k, v in dict(a).items() if k != "raw_row_json"} for a in accounts]
    # Drop the bulky raw report from the polled snapshot (fetch via /api/pull/{id}/report).
    out["pulls"] = [{k: v for k, v in dict(p).items() if k != "raw_report_json"} for p in pulls]
    out["api_calls"] = [dict(c) for c in calls]
    out["consents"] = [dict(c) for c in consents]
    return out


def recent_runs(limit: int = 50):
    init()
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT id, created_at, loan_id, emi_amount, aa_available, account_count, aa_count, status "
            "FROM runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
