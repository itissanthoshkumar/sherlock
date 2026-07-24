"""MongoDB data-access layer for DPD Early-Warning.

Mirrors the function surface the app/checker used with the old SQLite `store`
(create_run, finalize_run, add_account, add_pull, update_pull, log_api, get_run,
recent_runs, get_pull, get_account, update_account, save_consent, set_run_emi)
and adds user/role management + master-data accessors.

Collections (db dpd_early_warning):
  users, roles                 — auth + RBAC
  loans, bank_accounts, consents — master data (upserted on each check)
  checks, accounts, pulls      — history (a check, its resolved accounts, AA pulls)
  api_logs                     — every Digitap request + response
  counters                     — integer id sequences (UI-friendly ids)
"""
from datetime import datetime, timezone, timedelta

import db as _dbmod
import rbac
import userstore

# Project timezone: everything is stored and compared in IST (Asia/Kolkata).
# India observes a fixed +5:30 offset with NO daylight saving, so a constant
# offset is exact and unambiguous — no zone database needed. Storage, the daily
# vendor-cap day buckets, month gates and the frontend all share this one zone.
IST = timezone(timedelta(hours=5, minutes=30))


def _db():
    return _dbmod.get_db()


def _now():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _seq(name):
    """Atomic id sequence. find_one_and_update is a single round-trip, so
    concurrent callers (the cycle thread, per-run Timer threads, request
    handlers) can never read the same value and mint duplicate ids."""
    try:
        from pymongo import ReturnDocument
        doc = _db().counters.find_one_and_update(
            {"_id": name}, {"$inc": {"v": 1}}, upsert=True,
            return_document=ReturnDocument.AFTER)
        return doc["v"]
    except Exception:  # noqa: BLE001 — mongomock/older drivers: fall back
        _db().counters.update_one({"_id": name}, {"$inc": {"v": 1}}, upsert=True)
        return _db().counters.find_one({"_id": name})["v"]


# ---------------------------------------------------------------------------
# Checks (runs) + history
# ---------------------------------------------------------------------------
def create_run(loan_id, emi_amount=None, cycle_id=None, cycle_item_id=None, source=None):
    rid = _seq("checks")
    _db().checks.insert_one({
        "id": rid, "created_at": _now(), "loan_id": loan_id, "emi_amount": emi_amount,
        "aa_available": None, "account_count": None, "aa_count": None,
        "status": "PENDING", "error": None,
        "cycle_id": cycle_id, "cycle_item_id": cycle_item_id, "source": source,
        # join key — app no filled in from the loan master (or DUMMY) now, and
        # re-stamped with the resolved value in finalize_run once accounts land.
        "los_application_no": _loan_keys(loan_id)["los_application_no"],
    })
    return rid


def set_run_emi(run_id, emi_amount):
    _db().checks.update_one({"id": run_id}, {"$set": {"emi_amount": emi_amount}})


def finalize_run(run_id, *, aa_available, account_count, aa_count, status, error=None,
                 los_application_no=None):
    sets = {
        "aa_available": bool(aa_available), "account_count": account_count,
        "aa_count": aa_count, "status": status, "error": error,
    }
    if los_application_no:  # resolved from the run's accounts once they're known
        sets["los_application_no"] = los_application_no
    _db().checks.update_one({"id": run_id}, {"$set": sets})


def add_account(run_id, acct: dict) -> int:
    aid = _seq("accounts")
    loan_id = _loan_id_of_run(run_id)
    doc = {
        "id": aid, "run_id": run_id, **_loan_keys(loan_id, acct.get("los_application_no")),
        "bank_name": acct.get("bank_name"), "account_ref": acct.get("account_ref"),
        "is_repayment": bool(acct.get("is_repayment")), "source": acct.get("source"),
        "fetched_at": str(acct.get("fetched_at")) if acct.get("fetched_at") is not None else None,
        "aa_enabled": bool(acct.get("aa_enabled")), "main_txn_id": acct.get("main_txn_id"),
        "context_uid": acct.get("context_uid"),  # los_application_no set via _loan_keys above
        "bank_account_uid": acct.get("bank_account_uid"), "branch_name": acct.get("branch_name"),
        "ifsc": acct.get("ifsc"), "account_holder_name": acct.get("account_holder_name"),
        "emi_amount": acct.get("emi_amount"),
        "consent_id": acct.get("consent_id"),
        "consent_expiry": str(acct.get("consent_expiry")) if acct.get("consent_expiry") is not None else None,
        "consent_status": acct.get("consent_status"),
        "consent_state": acct.get("consent_state"),  # ELIGIBLE/EXPIRED/NOT_PULLABLE/NO_CONSENT
        "raw_row": acct.get("raw_row"), "created_at": _now(),
    }
    _db().accounts.insert_one(doc)
    _upsert_master(run_id, acct)
    return aid


MASTER = "ppdata"  # master-data collection, type-discriminated (loan / bank_account / consent)


# ---------------------------------------------------------------------------
# Uniform join key — EVERY per-loan document carries both loan_id and
# los_application_no so any record can be joined/searched by either. The app
# number is resolved once from the ppdata loan-master (the same lookup
# customer_360 does); 'DUMMY' fallback keeps the field non-null on mock data —
# real LOS application numbers flow in once a Portfolio Sync populates the
# master. Infra collections (cycles/jobs/users/roles) are NOT per-loan and are
# deliberately excluded.
# ---------------------------------------------------------------------------
def _loan_id_of_run(run_id):
    if run_id is None:
        return None
    run = _db().checks.find_one({"id": run_id}, {"_id": 0, "loan_id": 1})
    return run.get("loan_id") if run else None


def _loan_keys(loan_id, los_application_no=None) -> dict:
    """{loan_id, los_application_no} to spread into any per-loan insert dict."""
    app = los_application_no
    if not app and loan_id is not None:
        m = _db()[MASTER].find_one({"type": "loan", "lms_loan_id": loan_id},
                                   {"_id": 0, "los_application_no": 1})
        app = (m or {}).get("los_application_no")
    return {"loan_id": loan_id, "los_application_no": app or "DUMMY"}


# Per-loan collections that must carry the join key, and how each doc's loan id
# is found. Infra (cycles/jobs/users/roles) is intentionally excluded.
_JOINKEY_DIRECT = {  # loan id already lives on the doc under this field
    "checks": "loan_id", "consents": "lms_loan_id", "cycle_items": "loan_id",
    "processed_data": "loan_id", "aa_attempts": "loan_id", "nach_outcomes": "loan_id",
    "dispositions": "loan_id", "nudges": "loan_id", "customer_notes": "loan_id",
    "predictions": "loan_id",
}
_JOINKEY_VIA_RUN = ["accounts", "pulls", "api_logs"]  # loan id via run_id -> checks


def backfill_join_keys():
    """One-shot, idempotent: stamp {loan_id, los_application_no} on every existing
    per-loan doc that lacks it ('DUMMY' where unresolvable — mock data). Safe to
    re-run. Returns {collection: docs_updated}."""
    app_by_loan = {}
    for m in _db()[MASTER].find({"type": "loan"}, {"_id": 0, "lms_loan_id": 1, "los_application_no": 1}):
        if m.get("lms_loan_id"):
            app_by_loan[m["lms_loan_id"]] = m.get("los_application_no") or "DUMMY"
    loan_by_run = {r["id"]: r.get("loan_id")
                   for r in _db().checks.find({}, {"_id": 0, "id": 1, "loan_id": 1})}
    acct_txn = {}
    for a in _db().accounts.find({"main_txn_id": {"$ne": None}}, {"_id": 0, "main_txn_id": 1, "run_id": 1}):
        acct_txn.setdefault(a.get("main_txn_id"), a.get("run_id"))

    def stamp(coll, loan_id_of):
        n = 0
        missing = {"$or": [{"loan_id": {"$exists": False}}, {"los_application_no": {"$exists": False}}]}
        for d in _db()[coll].find(missing, {"_id": 1, "id": 1, "run_id": 1, "txn_id": 1,
                                            "loan_id": 1, "lms_loan_id": 1}):
            lid = loan_id_of(d)
            _db()[coll].update_one({"_id": d["_id"]},
                                   {"$set": {"loan_id": lid, "los_application_no": app_by_loan.get(lid) or "DUMMY"}})
            n += 1
        return n

    out = {}
    for coll, field in _JOINKEY_DIRECT.items():
        out[coll] = stamp(coll, lambda d, f=field: d.get("loan_id") or d.get(f))
    for coll in _JOINKEY_VIA_RUN:
        out[coll] = stamp(coll, lambda d: d.get("loan_id") or loan_by_run.get(d.get("run_id")))
    out["aa_live_calls"] = stamp("aa_live_calls",
                                 lambda d: d.get("loan_id") or loan_by_run.get(acct_txn.get(d.get("txn_id"))))
    return out


def _upsert_master(run_id, acct: dict):
    run = _db().checks.find_one({"id": run_id}, {"_id": 0, "loan_id": 1})
    loan_id = run.get("loan_id") if run else None
    if loan_id:
        _db()[MASTER].update_one({"type": "loan", "lms_loan_id": loan_id}, {"$set": {
            "type": "loan", "lms_loan_id": loan_id, "loan_id": loan_id,
            "los_application_no": acct.get("los_application_no"),
            "context_uid": acct.get("context_uid"), "emi_amount": acct.get("emi_amount"),
            "updated_at": _now(),
        }}, upsert=True)
    key = acct.get("bank_account_uid") or (str(loan_id) + ":" + str(acct.get("account_ref")))
    _db()[MASTER].update_one({"type": "bank_account", "bank_account_uid": key}, {"$set": {
        "type": "bank_account", "bank_account_uid": key, "lms_loan_id": loan_id, "loan_id": loan_id,
        "los_application_no": acct.get("los_application_no"), "bank_name": acct.get("bank_name"),
        "account_ref": acct.get("account_ref"), "source": acct.get("source"),
        "is_repayment": bool(acct.get("is_repayment")), "aa_enabled": bool(acct.get("aa_enabled")),
        "main_txn_id": acct.get("main_txn_id"), "consent_id": acct.get("consent_id"),
        "consent_expiry": str(acct.get("consent_expiry")) if acct.get("consent_expiry") is not None else None,
        "consent_status": acct.get("consent_status"), "ifsc": acct.get("ifsc"),
        "updated_at": _now(),
    }}, upsert=True)
    # Consents captured at origination live in the LOS and arrive on the LMS row.
    # Mirror them into the unified consent registry with source=LOS (PCPL-assisted
    # consents are written by save_consent with source=PCPL).
    if acct.get("consent_id"):
        _db()[MASTER].update_one({"type": "consent", "handle": acct.get("consent_id")}, {"$set": {
            "type": "consent", "handle": acct.get("consent_id"), "source": "LOS",
            "lms_loan_id": loan_id, "loan_id": loan_id,
            "los_application_no": acct.get("los_application_no"), "bank_name": acct.get("bank_name"),
            "account_ref": acct.get("account_ref"), "url": None,
            "status": acct.get("consent_status"),
            "expiry": str(acct.get("consent_expiry")) if acct.get("consent_expiry") is not None else None,
            "updated_at": _now(),
        }}, upsert=True)


def get_account(account_id):
    a = _db().accounts.find_one({"id": account_id}, {"_id": 0})
    if not a:
        return None
    run = _db().checks.find_one({"id": a.get("run_id")}, {"_id": 0, "loan_id": 1})
    a["loan_id"] = run.get("loan_id") if run else None
    return a


def update_account(account_id, **fields):
    if fields:
        _db().accounts.update_one({"id": account_id}, {"$set": fields})


def add_pull(run_id, account_id, bank_name, is_repayment, main_txn_id, child_txn_id, status,
             error=None, account_key=None, loan_id=None, los_application_no=None,
             fetch_type=None) -> int:
    """fetch_type records WHICH Digitap fetch produced this pull — 'PERIODIC' (the
    monthly Sherlock Check, via initiate_periodic -> retrieve) or 'ONETIME' (an ad-hoc
    verification from the Live Pull tab). Stored on the row so periodic pulls are a
    queryable set instead of something you have to infer from cycle_id/txn prefix."""
    pid = _seq("pulls")
    if loan_id is None:
        loan_id = _loan_id_of_run(run_id)
    ftype = (str(fetch_type).upper() if fetch_type else None)
    if ftype not in (None, "ONETIME", "PERIODIC"):
        ftype = None
    _db().pulls.insert_one({
        "id": pid, "run_id": run_id, "account_id": account_id,
        **_loan_keys(loan_id, los_application_no), "bank_name": bank_name,
        "is_repayment": bool(is_repayment), "main_txn_id": main_txn_id, "child_txn_id": child_txn_id,
        "fetch_type": ftype,
        "status": status, "available_balance": None, "currency": None, "decision": None,
        "error": error, "raw_report_json": None, "account_key": account_key,
        "created_at": _now(), "updated_at": _now(),
    })
    return pid


def update_pull(pull_id, **fields):
    if not fields:
        return
    fields["updated_at"] = _now()
    _db().pulls.update_one({"id": pull_id}, {"$set": fields})


def get_pull(pull_id):
    return _db().pulls.find_one({"id": pull_id}, {"_id": 0})


def log_api(run_id, pull_id, result: dict, cycle_id=None):
    _db().api_logs.insert_one({
        "id": _seq("api_logs"), "run_id": run_id, "pull_id": pull_id, "cycle_id": cycle_id,
        **_loan_keys(_loan_id_of_run(run_id)),
        "kind": result.get("kind"), "endpoint": result.get("endpoint"),
        "request": result.get("request"), "response": result.get("response"),
        "http_status": result.get("http_status"), "ok": bool(result.get("ok")),
        "error": result.get("error"), "created_at": _now(),
    })


def save_consent(run_id, account_id, consent: dict) -> int:
    cid = _seq("consents")
    acct = _db().accounts.find_one({"id": account_id},
                                   {"_id": 0, "account_ref": 1, "bank_name": 1, "los_application_no": 1})
    run = _db().checks.find_one({"id": run_id}, {"_id": 0, "loan_id": 1})
    loan_id = run.get("loan_id") if run else None
    doc = {
        "id": cid, "run_id": run_id, "account_id": account_id, "lms_loan_id": loan_id,
        **_loan_keys(loan_id, (acct or {}).get("los_application_no")),
        "bank_name": acct.get("bank_name") if acct else None,
        "account_ref": acct.get("account_ref") if acct else None,
        "handle": consent.get("handle"), "url": consent.get("url"),
        "status": consent.get("status"), "expiry": consent.get("expiry"),
        "source": "PCPL",  # staff-assisted consent, acquired through this app
        "created_at": _now(),
    }
    _db().consents.insert_one(doc)  # per-request history
    # master consent record in ppdata (keyed by handle)
    if consent.get("handle"):
        _db()[MASTER].update_one({"type": "consent", "handle": consent.get("handle")},
                                 {"$set": {"type": "consent", **{k: v for k, v in doc.items() if k != "id"}, "updated_at": _now()}},
                                 upsert=True)
    return cid


def activate_consent(handle):
    """Customer completed the AA journey: mark the consent live everywhere."""
    _db().consents.update_many({"handle": handle}, {"$set": {"status": "ACTIVE"}})
    _db()[MASTER].update_one({"type": "consent", "handle": handle},
                             {"$set": {"status": "ACTIVE", "updated_at": _now()}})


def pcpl_consent_for(loan_id, account_ref):
    """Latest PCPL-acquired consent for one bank account (mock overlay uses this)."""
    return _db()[MASTER].find_one(
        {"type": "consent", "lms_loan_id": loan_id, "account_ref": account_ref, "source": "PCPL"},
        {"_id": 0}, sort=[("updated_at", -1)])


def get_run(run_id):
    run = _db().checks.find_one({"id": run_id}, {"_id": 0})
    if not run:
        return None
    run["accounts"] = list(_db().accounts.find({"run_id": run_id}, {"_id": 0, "raw_row": 0}).sort("id", 1))
    run["pulls"] = list(_db().pulls.find({"run_id": run_id}, {"_id": 0, "raw_report_json": 0}).sort("id", 1))
    run["api_calls"] = list(_db().api_logs.find(
        {"run_id": run_id}, {"_id": 0, "id": 1, "pull_id": 1, "kind": 1, "http_status": 1, "ok": 1, "created_at": 1}
    ).sort("id", 1))
    run["consents"] = list(_db().consents.find({"run_id": run_id}, {"_id": 0}).sort("id", 1))
    return run


def recent_runs(limit: int = 50):
    return list(_db().checks.find(
        {}, {"_id": 0, "id": 1, "created_at": 1, "loan_id": 1, "emi_amount": 1,
             "aa_available": 1, "account_count": 1, "aa_count": 1, "status": 1}
    ).sort("id", -1).limit(limit))


# ---------------------------------------------------------------------------
# Users + roles (RBAC)
# ---------------------------------------------------------------------------
def authenticate(username, password):
    u = _db().users.find_one({"username": (username or "").strip()})
    if u and u.get("status", "active") == "active" and userstore.verify_password(password, u.get("password_hash", "")):
        _db().users.update_one({"username": u["username"]}, {"$set": {"last_login_at": _now()}})
        return u
    return None


def role_permissions(role_name):
    r = _db().roles.find_one({"name": role_name}, {"_id": 0, "permissions": 1})
    return r.get("permissions", []) if r else []


def list_roles():
    return list(_db().roles.find({}, {"_id": 0}))


def list_users():
    return list(_db().users.find({}, {"_id": 0, "password_hash": 0}))


def _role_exists(role):
    return _db().roles.find_one({"name": role}) is not None


def add_user(username, password, role="viewer", branch=None, must_change=True, by=None):
    username = (username or "").strip()
    if not username:
        raise ValueError("Username is required")
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    if not _role_exists(role):
        raise ValueError(f"Unknown role '{role}'")
    if _db().users.find_one({"username": username}):
        raise ValueError(f"User '{username}' already exists")
    _db().users.insert_one({
        "username": username, "password_hash": userstore.hash_password(password),
        "role": role, "branch": (branch or "").strip() or None, "status": "active",
        "session_ver": 0, "must_change_password": bool(must_change),
        "created_by": by, "created_at": _now(),
    })


def set_branch(username, branch):
    if _db().users.update_one({"username": username},
                              {"$set": {"branch": (branch or "").strip() or None}}
                              ).matched_count == 0:
        raise ValueError(f"User '{username}' not found")


def set_status(username, status):
    """Suspend/activate a user. Suspending bumps session_ver so their live sessions die
    immediately (offboarding without deleting the audit trail of their account)."""
    status = (status or "").strip().lower()
    if status not in ("active", "suspended"):
        raise ValueError("status must be 'active' or 'suspended'")
    target = _db().users.find_one({"username": username}, {"_id": 0, "role": 1})
    if not target:
        raise ValueError(f"User '{username}' not found")
    if status == "suspended" and target.get("role") == "admin" and _admin_count() <= 1:
        raise ValueError("Can't suspend the last admin — promote another admin first")
    upd = {"$set": {"status": status}}
    if status == "suspended":
        upd["$inc"] = {"session_ver": 1}  # kill active sessions on suspend
    _db().users.update_one({"username": username}, upd)


def set_password(username, password):
    if not password:
        raise ValueError("Password is required")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    # Bump session_ver too: a password change must invalidate every existing cookie —
    # that's usually WHY the password is being changed (compromise / offboarding). Also
    # clears the must-change flag (the user has now set their own password).
    if _db().users.update_one({"username": username},
                              {"$set": {"password_hash": userstore.hash_password(password),
                                        "must_change_password": False},
                               "$inc": {"session_ver": 1}}).matched_count == 0:
        raise ValueError(f"User '{username}' not found")


def get_user(username):
    return _db().users.find_one({"username": (username or "").strip()},
                                {"_id": 0, "password_hash": 0})


def bump_session_ver(username) -> int:
    """Server-side session revocation: every login stamps the user's current
    session_ver into the cookie session; bumping it invalidates ALL existing
    sessions for that user immediately (checked per request). Default anchor is 0
    (absent field) so the first $inc -> 1 DIVERGES from every already-issued cookie."""
    username = (username or "").strip()
    if _db().users.update_one({"username": username},
                              {"$inc": {"session_ver": 1}}).matched_count == 0:
        raise ValueError(f"User '{username}' not found")
    u = _db().users.find_one({"username": username}, {"_id": 0, "session_ver": 1})
    return (u or {}).get("session_ver", 0)


# ---------------------------------------------------------------------------
# Auth audit trail — every login/logout/role/password/session event, immutable
# append-only, viewable in the data browser + /api/auth-log.
# ---------------------------------------------------------------------------
def log_auth(event, username, by=None, detail=None, ip=None) -> int:
    aid = _seq("auth_log")
    _db().auth_log.insert_one({
        "id": aid, "at": _now(), "event": str(event or "").upper(),
        "username": username, "by": by or username, "detail": detail, "ip": ip,
    })
    return aid


def auth_log_list(limit=200) -> list:
    return list(_db().auth_log.find({}, {"_id": 0}).sort("id", -1).limit(limit))


def _admin_count():
    # Only ACTIVE admins keep the platform administrable — a suspended admin can't log in,
    # so it must not count toward the last-admin guard.
    return _db().users.count_documents({"role": "admin", "status": {"$ne": "suspended"}})


def set_role(username, role):
    if not _role_exists(role):
        raise ValueError(f"Unknown role '{role}'")
    target = _db().users.find_one({"username": username}, {"_id": 0, "role": 1})
    if not target:
        raise ValueError(f"User '{username}' not found")
    # Never let the last admin be demoted — that locks every admin function out of
    # the platform with no in-app recovery. Mirrors userstore.set_role.
    if target.get("role") == "admin" and role != "admin" and _admin_count() <= 1:
        raise ValueError("Can't demote the last admin — promote another admin first")
    # Bump session_ver so the role change lands immediately even on an in-flight session
    # (belt-and-suspenders with the DB-role read in effective_role). See audit (RBAC stale).
    if _db().users.update_one({"username": username},
                              {"$set": {"role": role}, "$inc": {"session_ver": 1}}
                              ).matched_count == 0:
        raise ValueError(f"User '{username}' not found")


def delete_user(username):
    target = _db().users.find_one({"username": username}, {"_id": 0, "role": 1})
    if not target:
        raise ValueError(f"User '{username}' not found")
    if target.get("role") == "admin" and _admin_count() <= 1:
        raise ValueError("Can't delete the last admin — promote another admin first")
    if _db().users.delete_one({"username": username}).deleted_count == 0:
        raise ValueError(f"User '{username}' not found")


# ---------------------------------------------------------------------------
# Master data accessors
# ---------------------------------------------------------------------------
def list_loans(limit=200):
    return list(_db()[MASTER].find({"type": "loan"}, {"_id": 0}).sort("updated_at", -1).limit(limit))


def list_bank_accounts(limit=500):
    return list(_db()[MASTER].find({"type": "bank_account"}, {"_id": 0}).sort("updated_at", -1).limit(limit))


def list_consents(limit=200):
    return list(_db()[MASTER].find({"type": "consent"}, {"_id": 0}).sort("updated_at", -1).limit(limit))


# ---------------------------------------------------------------------------
# Monthly pre-NACH cycle: cycles + items
# ---------------------------------------------------------------------------
def create_cycle(month, triggered_by, source=None):
    cid = _seq("cycles")
    _db().cycles.insert_one({
        "id": cid, "month": month, "status": "RUNNING", "triggered_by": triggered_by,
        "source": source,
        "created_at": _now(), "finished_at": None, "error": None,
        "totals": {"eligible": 0, "items_created": 0, "repay_consent_ok": 0,
                   "repay_consent_expired": 0, "repay_not_linked": 0,
                   "pulls_initiated": 0, "repay_pulls": 0, "pulls_blocked": 0},
        "bucket_counts": {"COMFORT": 0, "WATCH": 0, "SHORTFALL": 0, "NO_DATA": 0},
    })
    return cid


def get_cycle(cycle_id):
    return _db().cycles.find_one({"id": cycle_id}, {"_id": 0})


def update_cycle(cycle_id, **fields):
    if fields:
        _db().cycles.update_one({"id": cycle_id}, {"$set": fields})


def list_cycles(limit=24):
    # Month first: backfilled history cycles have higher ids than the live month.
    return list(_db().cycles.find({}, {"_id": 0}).sort([("month", -1), ("id", -1)]).limit(limit))


def active_cycle():
    """The cycle still executing, if any (RUNNING = firing checks, COLLECTING = waiting on pulls)."""
    return _db().cycles.find_one({"status": {"$in": ["RUNNING", "COLLECTING"]}}, {"_id": 0})


def cycle_for_month(month):
    return _db().cycles.find_one({"month": month}, {"_id": 0}, sort=[("id", -1)])


def add_cycle_item(cycle_id, loan: dict) -> int:
    iid = _seq("cycle_items")
    _db().cycle_items.insert_one({
        "id": iid, "cycle_id": cycle_id,
        **_loan_keys(loan.get("loan_id"), loan.get("los_application_no")),
        "customer_name": loan.get("customer_name"), "emi_amount": loan.get("emi_amount"),
        # demand_amount = what NACH actually presents this cycle (EMI + arrears); demand_date =
        # the real per-loan presentation day. Both drive the right denominator/horizon downstream.
        "demand_amount": loan.get("demand_amount"), "demand_date": loan.get("demand_date"),
        "od_days_num": loan.get("od_days_num"),
        "npa_parked": bool(loan.get("npa_parked")), "run_id": None,
        "branch": loan.get("branch"), "state": loan.get("state"),
        "status": "PENDING", "bucket": None, "bucket_reason": None,
        "ratio": None, "repay_balance": None, "repay_bank": None, "repay_account_ref": None,
        "agg_balance": None, "agg_ratio": None, "risk_score": None,
        "override_bucket": None, "override_reason": None, "override_by": None, "override_at": None,
        "created_at": _now(), "updated_at": _now(),
    })
    # Keep the loan master enriched with the portfolio-level fields.
    if loan.get("loan_id"):
        loan_set = {
            "type": "loan", "lms_loan_id": loan.get("loan_id"), "loan_id": loan.get("loan_id"),
            "customer_name": loan.get("customer_name"), "emi_amount": loan.get("emi_amount"),
            "branch": loan.get("branch"), "state": loan.get("state"),
            "updated_at": _now(),
        }
        if loan.get("los_application_no"):  # present when the portfolio came from LOS
            loan_set["los_application_no"] = loan.get("los_application_no")
        _db()[MASTER].update_one({"type": "loan", "lms_loan_id": loan.get("loan_id")},
                                 {"$set": loan_set}, upsert=True)
    return iid


def get_cycle_item(item_id):
    return _db().cycle_items.find_one({"id": item_id}, {"_id": 0})


def update_cycle_item(item_id, **fields):
    if fields:
        fields["updated_at"] = _now()
        _db().cycle_items.update_one({"id": item_id}, {"$set": fields})


def cycle_items(cycle_id):
    return list(_db().cycle_items.find({"cycle_id": cycle_id}, {"_id": 0}).sort("id", 1))


def find_item_by_run(run_id):
    return _db().cycle_items.find_one({"run_id": run_id}, {"_id": 0})


def run_accounts(run_id):
    return list(_db().accounts.find({"run_id": run_id}, {"_id": 0, "raw_row": 0}).sort("id", 1))


def run_pulls(run_id):
    return list(_db().pulls.find({"run_id": run_id}, {"_id": 0, "raw_report_json": 0}).sort("id", 1))


def recount_buckets(cycle_id):
    """Recompute bucket counts + rupee exposure from items (idempotent)."""
    counts = {"COMFORT": 0, "WATCH": 0, "SHORTFALL": 0, "NO_DATA": 0}
    at_risk_value = shortfall_value = 0.0
    for it in _db().cycle_items.find({"cycle_id": cycle_id},
                                     {"_id": 0, "bucket": 1, "override_bucket": 1,
                                      "emi_amount": 1, "demand_amount": 1}):
        b = it.get("override_bucket") or it.get("bucket")
        if b in counts:
            counts[b] += 1
        # NACH presents the DEMAND (EMI + arrears) — rupee exposure must use it, same
        # denominator classification already uses. EMI is only the no-demand fallback.
        try:
            amt = float(it.get("demand_amount"))
        except (TypeError, ValueError):
            amt = 0.0
        amt = amt if amt > 0 else (it.get("emi_amount") or 0)
        if b in ("WATCH", "SHORTFALL"):
            at_risk_value += amt
        if b == "SHORTFALL":
            shortfall_value += amt
    update_cycle(cycle_id, bucket_counts=counts,
                 exposure={"at_risk_value": round(at_risk_value, 2),
                           "shortfall_value": round(shortfall_value, 2)})
    return counts


# ---------------------------------------------------------------------------
# AA attempt ledger — the 4-initiates-per-account-per-month guardrail
# ---------------------------------------------------------------------------
def record_attempt(month, account_key, loan_id, bank_name, account_ref,
                   run_id=None, pull_id=None, cycle_id=None, allowed=True, reason=None):
    _db().aa_attempts.insert_one({
        "id": _seq("aa_attempts"), "month": month, "account_key": account_key,
        **_loan_keys(loan_id), "bank_name": bank_name, "account_ref": account_ref,
        "run_id": run_id, "pull_id": pull_id, "cycle_id": cycle_id,
        "allowed": bool(allowed), "reason": reason, "created_at": _now(),
    })


def attempts_used(account_key, month) -> int:
    return _db().aa_attempts.count_documents({"account_key": account_key, "month": month, "allowed": True})


def attempts_summary(account_keys, month) -> dict:
    """{account_key: allowed-attempt count} for the given month."""
    out = {k: 0 for k in account_keys if k}
    if not out:
        return out
    for doc in _db().aa_attempts.aggregate([
        {"$match": {"account_key": {"$in": list(out.keys())}, "month": month, "allowed": True}},
        {"$group": {"_id": "$account_key", "n": {"$sum": 1}}},
    ]):
        out[doc["_id"]] = doc["n"]
    return out


# ---------------------------------------------------------------------------
# Processed data — derived output only, one doc per customer per cycle
# ---------------------------------------------------------------------------
def save_processed(doc: dict):
    key = {"cycle_id": doc.get("cycle_id"), "loan_id": doc.get("loan_id")}
    doc.update(_loan_keys(doc.get("loan_id"), doc.get("los_application_no")))
    existing = _db().processed_data.find_one(key, {"_id": 0, "id": 1})
    if existing:
        doc["id"] = existing["id"]
    else:
        doc["id"] = _seq("processed_data")
        doc["created_at"] = _now()
    doc["updated_at"] = _now()
    _db().processed_data.update_one(key, {"$set": doc}, upsert=True)
    return doc["id"]


def update_processed(cycle_id, loan_id, **fields):
    if fields:
        fields["updated_at"] = _now()
        _db().processed_data.update_one({"cycle_id": cycle_id, "loan_id": loan_id}, {"$set": fields})


def processed_for_loan(loan_id, limit=24):
    return list(_db().processed_data.find({"loan_id": loan_id}, {"_id": 0})
                .sort([("month", -1), ("id", -1)]).limit(limit))


# ---------------------------------------------------------------------------
# Worklist dispositions (current state on the item + append-only audit log)
# ---------------------------------------------------------------------------
def add_disposition(cycle_id, item_id, loan_id, bucket, status, remarks, ptp_date, by):
    doc = {
        "id": _seq("dispositions"), "cycle_id": cycle_id, "item_id": item_id,
        **_loan_keys(loan_id), "bucket": bucket, "status": status,
        "remarks": remarks, "ptp_date": ptp_date, "by": by, "created_at": _now(),
    }
    _db().dispositions.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


def dispositions_for_item(item_id, limit=20):
    return list(_db().dispositions.find({"item_id": item_id}, {"_id": 0}).sort("id", -1).limit(limit))


def disposition_summary(cycle_id):
    """{status: count} across a cycle's disposition *current* states."""
    out = {}
    for it in _db().cycle_items.find({"cycle_id": cycle_id}, {"_id": 0, "disposition": 1}):
        s = (it.get("disposition") or {}).get("status")
        if s:
            out[s] = out.get(s, 0) + 1
    return out


# ---------------------------------------------------------------------------
# NACH outcomes (presentation results — mock-simulated until live feed exists)
# ---------------------------------------------------------------------------
def save_outcome(doc: dict):
    key = {"cycle_id": doc.get("cycle_id"), "loan_id": doc.get("loan_id")}
    doc.update(_loan_keys(doc.get("loan_id"), doc.get("los_application_no")))
    existing = _db().nach_outcomes.find_one(key, {"_id": 0, "id": 1, "source": 1})
    # Ground truth is one-way: a hand-entered NACH_ACTUAL row must NEVER be overwritten
    # by a mock simulation re-run — the real return data would be unrecoverable and the
    # accuracy dashboard would silently flip to 'unvalidated'. See audit #4.
    if existing and existing.get("source") == "NACH_ACTUAL" \
            and doc.get("source") != "NACH_ACTUAL":
        return existing["id"]
    if existing:
        doc["id"] = existing["id"]
    else:
        doc["id"] = _seq("nach_outcomes")
        doc["created_at"] = _now()  # stamp first-entry time so a correction can't erase it
    doc["updated_at"] = _now()
    _db().nach_outcomes.update_one(key, {"$set": doc}, upsert=True)
    return doc["id"]


def outcomes_for_cycle(cycle_id):
    return list(_db().nach_outcomes.find({"cycle_id": cycle_id}, {"_id": 0}).sort("id", 1))


def outcomes_by_cycle(source=None):
    """cycle_id -> {presented, bounced}. Pass source='NACH_ACTUAL' to count only REAL
    (validated) outcomes — mock rows must never feed accuracy KPIs."""
    match = {"source": source} if source else {}
    out = {}
    for doc in _db().nach_outcomes.aggregate([
        {"$match": match},
        {"$group": {"_id": "$cycle_id", "presented": {"$sum": 1},
                    "bounced": {"$sum": {"$cond": [{"$eq": ["$outcome", "BOUNCED"]}, 1, 0]}}}},
    ]):
        out[doc["_id"]] = {"presented": doc["presented"], "bounced": doc["bounced"]}
    return out


# ---------------------------------------------------------------------------
# Immutable prediction snapshots — the prediction of record a cycle is scored
# against. Write-once per (cycle_id, loan_id); retries/sentinel/backfill mutate
# the live cycle_item but NEVER this, so predicted-vs-actual stays honest.
# ---------------------------------------------------------------------------
def snapshot_prediction(doc: dict) -> bool:
    key = {"cycle_id": doc.get("cycle_id"), "loan_id": doc.get("loan_id")}
    if _db().predictions.find_one(key, {"_id": 1}):
        return False
    doc = dict(doc)
    doc.update(_loan_keys(doc.get("loan_id"), doc.get("los_application_no")))
    doc["id"] = _seq("predictions")
    doc["snapped_at"] = _now()
    _db().predictions.insert_one(doc)
    return True


def prediction_for(cycle_id, loan_id):
    return _db().predictions.find_one({"cycle_id": cycle_id, "loan_id": loan_id}, {"_id": 0})


def predictions_for_cycle(cycle_id):
    return list(_db().predictions.find({"cycle_id": cycle_id}, {"_id": 0}).sort("id", 1))


# ---------------------------------------------------------------------------
# Live AA (Digitap) call ledger — EVERY request this tool makes is recorded,
# with mode (mock/live). `live` == billed; the UI surfaces the count so nobody
# is surprised by a Digitap invoice.
# ---------------------------------------------------------------------------
def log_aa_call(res: dict, by=None, loan_id=None) -> int:
    req = res.get("request") or {}
    resp = res.get("response") or {}
    txn = req.get("txn_id") or req.get("main_txn_id") or resp.get("txn_id")
    if loan_id is None and txn:  # resolve the loan from the Digitap txn when the caller didn't
        a = _db().accounts.find_one({"main_txn_id": txn}, {"_id": 0, "run_id": 1})
        if a:
            loan_id = _loan_id_of_run(a.get("run_id"))
    if loan_id is None:
        # Fresh-consent journey: no accounts row exists yet, but the generate step filed a
        # REQUESTED registry row carrying the request_id (and, once statuscheck reveals it,
        # the main_txn_id) — resolve through it so EVERY call in the journey is customer-linked.
        rid = req.get("request_id") or resp.get("request_id")
        q = []
        if rid:
            q.append({"request_id": str(rid)})
        if txn:
            q.append({"main_txn_id": txn})
        if q:
            row = _db().consent_manager.find_one({"$or": q}, {"_id": 0, "loan_id": 1},
                                                 sort=[("id", -1)])
            if row:
                loan_id = row.get("loan_id")
    cid = _seq("aa_live_calls")
    at = _now()
    _db().aa_live_calls.insert_one({
        "id": cid, "at": at, "by": by, **_loan_keys(loan_id),
        "kind": res.get("kind"), "endpoint": res.get("endpoint"),
        "mode": res.get("mode"), "live": res.get("mode") == "live",
        "ok": bool(res.get("ok")), "http_status": res.get("http_status"),
        "error": res.get("error"),
        "request_id": req.get("request_id") or resp.get("request_id"),
        "txn_id": txn,
        "request": req, "response": resp,
        "url": res.get("url"), "curl": res.get("curl"),  # curl has the auth token redacted
    })
    # Dedicated payload archive: the ENTIRE request and ENTIRE response of every
    # attempt, in its own collection (one row per call, back-linked by call_id to
    # the ledger row above). Keeps the full JSON queryable on its own without
    # wading through the ledger, and gives payload retention its own home.
    _db().aa_call_payloads.insert_one({
        "id": _seq("aa_call_payloads"), "call_id": cid, "at": at, "by": by,
        **_loan_keys(loan_id),
        "kind": res.get("kind"), "endpoint": res.get("endpoint"),
        "mode": res.get("mode"), "live": res.get("mode") == "live",
        "ok": bool(res.get("ok")), "http_status": res.get("http_status"),
        "request_id": req.get("request_id") or resp.get("request_id"), "txn_id": txn,
        "request": req, "response": resp,
        "url": res.get("url"), "curl": res.get("curl"),  # curl has the auth token redacted
    })
    return cid


def aa_live_calls(limit: int = 60, loan_id=None, kind=None, mode=None, ok=None, q=None) -> list:
    """Digitap call ledger, filterable — so every journey is viewable from the UI
    (per loan, per call kind, live vs mock, ok vs error, or free-text on the ids)."""
    f = {}
    if loan_id:
        f["loan_id"] = str(loan_id)
    if kind:
        f["kind"] = kind if str(kind).startswith("aa_") else "aa_" + str(kind)
    if mode in ("live", "mock"):
        f["live"] = (mode == "live")
    if ok in (True, False):
        f["ok"] = ok
    if q:
        import re as _re
        rx = _re.compile(_re.escape(str(q).strip()), _re.IGNORECASE)
        f["$or"] = [{"loan_id": rx}, {"txn_id": rx}, {"by": rx},
                    # request_id may be stored as int — match its string form too
                    {"request_id": rx}, {"request_id": str(q).strip()}]
        try:
            f["$or"].append({"request_id": int(str(q).strip())})
        except ValueError:
            pass
    return list(_db().aa_live_calls.find(f, {"_id": 0}).sort("id", -1).limit(limit))


def link_aa_calls_to_loan(loan_id, request_id=None, txn_ids=None) -> int:
    """Back-link a consent journey's earlier calls (generate/status/initiate ran before
    the loan was known) to the customer once the retrieve associates txn -> loan. Only
    fills empty loan_id — never overwrites an existing link."""
    loan_id = str(loan_id or "").strip()
    if not loan_id:
        return 0
    ors = []
    if request_id:
        ors += [{"request_id": str(request_id)}, {"request.request_id": str(request_id)}]
        try:
            ors += [{"request_id": int(str(request_id))}, {"request.request_id": int(str(request_id))}]
        except ValueError:
            pass
    for t in (txn_ids or []):
        if t:
            ors += [{"txn_id": t}, {"request.main_txn_id": t}]
    if not ors:
        return 0
    keys = _loan_keys(loan_id)
    r = _db().aa_live_calls.update_many(
        {"$and": [{"$or": ors}, {"$or": [{"loan_id": None}, {"loan_id": ""}]}]},
        {"$set": {"loan_id": loan_id, "los_application_no": keys.get("los_application_no")}})
    return getattr(r, "modified_count", 0) or 0


def account_key_for_txn(main_txn_id):
    """The ledger key + loan for a Digitap parent txn, from its latest pull — so the
    manual Live-Pull initiate can honor the per-account monthly cap for accounts already
    on the ledger (a brand-new consent has no prior pull and returns None)."""
    if not main_txn_id:
        return None
    return _db().pulls.find_one({"main_txn_id": main_txn_id},
                                {"_id": 0, "account_key": 1, "loan_id": 1}, sort=[("id", -1)])


def aa_live_stats() -> dict:
    """Totals by mode + today's LIVE (billed) count — the billing guard rail.
    Policy: statuscheck (aa_status) is a free readiness poll — logged in the ledger
    for completeness but NOT billed, so it is excluded from live_today (which seeds
    the daily cap counter). Initiate/retrieve/generate are the billed calls. Audit #10."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    total = live = mock = live_today = 0
    for c in _db().aa_live_calls.find({}, {"_id": 0, "live": 1, "at": 1, "kind": 1}):
        total += 1
        if c.get("live"):
            live += 1
            if str(c.get("at") or "").startswith(today) and c.get("kind") != "aa_status":
                live_today += 1
        else:
            mock += 1
    return {"total": total, "live": live, "mock": mock, "live_today": live_today}


def reserve_live_call(cap, day=None) -> bool:
    """Atomically reserve one slot under the daily billed-call cap. Returns True if a
    slot was reserved (caller may make the billed Digitap call), False if the cap is
    full. A per-UTC-day counter closes the check-then-act race that let concurrent
    initiate/retrieve calls (e.g. many cycle retrieve Timers firing together) overshoot
    AA_LIVE_MAX_CALLS_PER_DAY. Seeded once/day from the audit count so a restart mid-day
    doesn't lose the tally. Callers still log_aa_call for the audit trail."""
    from pymongo import ReturnDocument
    day = day or datetime.now(IST).strftime("%Y-%m-%d")
    key = "aa_live_reserved:" + day
    if _db().counters.find_one({"_id": key}, {"_id": 1}) is None:
        # first reservation of the day — seed from calls already logged today (idempotent:
        # $setOnInsert only applies if this thread actually inserts the doc).
        _db().counters.update_one(
            {"_id": key}, {"$setOnInsert": {"v": aa_live_stats().get("live_today", 0)}}, upsert=True)
    doc = _db().counters.find_one_and_update(
        {"_id": key, "v": {"$lt": int(cap)}}, {"$inc": {"v": 1}},
        return_document=ReturnDocument.AFTER)
    return doc is not None


def reserve_monthly_attempt(account_key, month, cap) -> bool:
    """Atomically reserve one of the account's monthly AA attempts BEFORE dispatching —
    the sibling of reserve_live_call. The old attempts_used()-then-record_attempt()
    pattern was check-then-act: two concurrent initiates for the same account both read
    3<4 and both dispatched a billed pull past the guardrail. Seeded once per
    (account, month) from the ledger so history is preserved. Audit #17.
    On a genuine non-dispatch (vendor never reached), release with release_monthly_attempt."""
    from pymongo import ReturnDocument
    key = f"aa_month:{account_key}:{month}"
    if _db().counters.find_one({"_id": key}, {"_id": 1}) is None:
        _db().counters.update_one(
            {"_id": key}, {"$setOnInsert": {"v": attempts_used(account_key, month)}}, upsert=True)
    doc = _db().counters.find_one_and_update(
        {"_id": key, "v": {"$lt": int(cap)}}, {"$inc": {"v": 1}},
        return_document=ReturnDocument.AFTER)
    return doc is not None


def release_monthly_attempt(account_key, month) -> None:
    """Give back a reserved monthly slot when the initiate never reached the vendor
    (daily-cap block / transient failure) — mirrors record_attempt(allowed=False)."""
    _db().counters.update_one({"_id": f"aa_month:{account_key}:{month}", "v": {"$gt": 0}},
                              {"$inc": {"v": -1}})


def acquire_month_lock(name, month) -> bool:
    """One-shot mutex per (name, month) — closes the check-then-act race where two
    concurrent start_cycle calls both pass the active/existing guards and each start
    a full-book run (double-billing every borrower). Audit #3."""
    r = _db().counters.update_one({"_id": f"{name}:{month}"},
                                  {"$setOnInsert": {"locked_at": _now()}}, upsert=True)
    return bool(getattr(r, "upserted_id", None))


def release_month_lock(name, month) -> None:
    _db().counters.delete_one({"_id": f"{name}:{month}"})


# ---------------------------------------------------------------------------
# LOS (Engrow) call ledger + portfolio snapshot — EVERY Flow-A request is
# recorded (mock/live) and shown in the Portfolio Sync tab, same discipline as
# the Digitap ledger. LOS is not vendor-billed, so no daily cap, just visibility.
# ---------------------------------------------------------------------------
def log_los_call(res: dict, by=None, loan_id=None, los_application_no=None) -> int:
    req = res.get("request") or {}
    resp = res.get("response") or {}
    cid = _seq("los_calls")
    _db().los_calls.insert_one({
        "id": cid, "at": _now(), "by": by,
        **_loan_keys(loan_id, los_application_no),
        "kind": res.get("kind"), "endpoint": res.get("endpoint"), "method": res.get("method"),
        "mode": res.get("mode"), "live": res.get("mode") == "live",
        "ok": bool(res.get("ok")), "http_status": res.get("http_status"),
        "error": res.get("error"), "request": req, "response": resp,
    })
    return cid


def los_calls(limit: int = 60) -> list:
    return list(_db().los_calls.find({}, {"_id": 0}).sort("id", -1).limit(limit))


def los_stats() -> dict:
    """Totals by mode + today's LIVE count (visibility, LOS is not billed)."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    total = live = mock = live_today = 0
    for c in _db().los_calls.find({}, {"_id": 0, "live": 1, "at": 1}):
        total += 1
        if c.get("live"):
            live += 1
            if str(c.get("at") or "").startswith(today):
                live_today += 1
        else:
            mock += 1
    return {"total": total, "live": live, "mock": mock, "live_today": live_today}


def save_los_portfolio(row: dict, triggered_by="los-sync", sync_batch=None) -> int:
    """Upsert one LOS portfolio snapshot doc, keyed by loan_id (idempotent per sync).
    sync_batch tags the doc so the ingest can delete stale rows AFTER the new batch
    landed (insert-before-delete — a mid-loop failure never guts the snapshot)."""
    key = {"loan_id": row.get("loan_id")}
    existing = _db().los_portfolio.find_one(key, {"_id": 0, "id": 1})
    doc = dict(row)
    doc.update(_loan_keys(row.get("loan_id"), row.get("los_application_no")))
    doc["id"] = existing["id"] if existing else _seq("los_portfolio")
    doc["triggered_by"] = triggered_by
    if sync_batch:
        doc["sync_batch"] = sync_batch
    doc["updated_at"] = _now()
    _db().los_portfolio.update_one(key, {"$set": doc}, upsert=True)
    return doc["id"]


def prune_los_portfolio(triggered_by, keep_batch) -> int:
    """Drop snapshot rows NOT written by this batch — called only after the new batch
    fully landed (the delete-last half of insert-before-delete)."""
    r = _db().los_portfolio.delete_many(
        {"triggered_by": triggered_by, "sync_batch": {"$ne": keep_batch}})
    return getattr(r, "deleted_count", 0) or 0


def los_portfolio_all() -> list:
    return list(_db().los_portfolio.find({}, {"_id": 0}).sort("id", 1))


def los_account_for(loan_id):
    return _db().los_portfolio.find_one({"loan_id": loan_id}, {"_id": 0})


def clear_los_portfolio(triggered_by="los-sync"):
    _db().los_portfolio.delete_many({"triggered_by": triggered_by})


# ---------------------------------------------------------------------------
# LMS (Encore) call ledger + presentment snapshot — Flow B (loans due for
# collection: contact number, demand amount, due date). Same visibility as LOS.
# ---------------------------------------------------------------------------
def log_lms_call(res: dict, by=None, loan_id=None) -> int:
    req = res.get("request") or {}
    resp = res.get("response") or {}
    cid = _seq("lms_calls")
    _db().lms_calls.insert_one({
        "id": cid, "at": _now(), "by": by, **_loan_keys(loan_id),
        "kind": res.get("kind"), "endpoint": res.get("endpoint"), "method": res.get("method"),
        "mode": res.get("mode"), "live": res.get("mode") == "live",
        "ok": bool(res.get("ok")), "http_status": res.get("http_status"),
        "error": res.get("error"), "request": req, "response": resp,
    })
    return cid


def lms_calls(limit: int = 60) -> list:
    return list(_db().lms_calls.find({}, {"_id": 0}).sort("id", -1).limit(limit))


def lms_stats() -> dict:
    today = datetime.now(IST).strftime("%Y-%m-%d")
    total = live = mock = live_today = 0
    for c in _db().lms_calls.find({}, {"_id": 0, "live": 1, "at": 1}):
        total += 1
        if c.get("live"):
            live += 1
            if str(c.get("at") or "").startswith(today):
                live_today += 1
        else:
            mock += 1
    return {"total": total, "live": live, "mock": mock, "live_today": live_today}


def save_lms_presentment(row: dict, triggered_by="lms-sync") -> int:
    """Upsert one presentment row keyed by LMS account_id (idempotent per sync)."""
    acct = row.get("account_id")
    key = {"account_id": acct}
    existing = _db().lms_presentment.find_one(key, {"_id": 0, "id": 1})
    doc = dict(row)
    # loan_id = the real LMS account id; los_application_no stays DUMMY until an
    # LOS<->LMS reconciliation exists.
    doc.update(_loan_keys(acct))
    doc["id"] = existing["id"] if existing else _seq("lms_presentment")
    doc["triggered_by"] = triggered_by
    doc["updated_at"] = _now()
    _db().lms_presentment.update_one(key, {"$set": doc}, upsert=True)
    return doc["id"]


def lms_presentment_all(limit=0) -> list:
    """The WHOLE presentment book (default) — this feeds the cycle portfolio, consent
    sync, pre-flight eligibility and borrowers refresh, so a silent cap here would drop
    real due loans from the monthly run once the book grows past it. Pass a positive
    limit only for explicit previews. See audit (silent 5000-row cap)."""
    cur = _db().lms_presentment.find({}, {"_id": 0}).sort("id", 1)
    if limit and limit > 0:
        cur = cur.limit(limit)
    return list(cur)


def lms_for_account(account_id):
    return _db().lms_presentment.find_one({"account_id": account_id}, {"_id": 0})


def lms_presentment_meta() -> dict:
    """{rows, last_synced_at, mode, mixed_batch} for the current snapshot (pre-flight gates
    on mode + mixed_batch). mixed_batch=True means >1 sync_batch coexist — a partial/failed
    replace left old+new rows together; the book is untrustworthy. See audit (partial insert)."""
    latest = _db().lms_presentment.find_one({}, {"_id": 0, "updated_at": 1, "mode": 1},
                                            sort=[("updated_at", -1)])
    try:
        n_batches = len(_db().lms_presentment.distinct("sync_batch"))
    except Exception:  # noqa: BLE001
        n_batches = 1
    return {"rows": _db().lms_presentment.estimated_document_count(),
            "last_synced_at": (latest or {}).get("updated_at"),
            "mode": (latest or {}).get("mode"),
            "mixed_batch": n_batches > 1}


def lms_presentment_query(q=None, branch=None, status=None, od=None, skip=0, limit=100):
    """Search/filter/page the presentment book. od: '0' | '1-7' | '8-30' | '30+'.
    Returns {total, rows, facets:{branches, statuses}}."""
    import re
    flt = {}
    q = (q or "").strip()
    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        flt["$or"] = [{"account_id": rx}, {"customer_name": rx},
                      {"contact_number": rx}, {"branch_name": rx}, {"customer_id": rx}]
    if branch:
        flt["branch_name"] = branch
    if status:
        flt["account_status"] = status
    if od == "0":
        flt["od_days_num"] = {"$in": [0, None]}
    elif od == "1-7":
        flt["od_days_num"] = {"$gte": 1, "$lte": 7}
    elif od == "8-30":
        flt["od_days_num"] = {"$gt": 7, "$lte": 30}
    elif od == "30+":
        flt["od_days_num"] = {"$gt": 30}
    coll = _db().lms_presentment
    total = coll.count_documents(flt)
    rows = list(coll.find(flt, {"_id": 0}).sort("account_id", 1)
                .skip(max(0, int(skip))).limit(max(1, min(int(limit), 500))))
    facets = {"branches": sorted(b for b in coll.distinct("branch_name") if b),
              "statuses": sorted(s for s in coll.distinct("account_status") if s)}
    return {"total": total, "rows": rows, "facets": facets}


def clear_lms_presentment(triggered_by="lms-sync"):
    _db().lms_presentment.delete_many({"triggered_by": triggered_by})


def _seq_block(name, n):
    """Reserve n sequential ids in ONE round-trip (for bulk inserts of 1000s of
    rows). Returns the first reserved id."""
    if n <= 0:
        return 1
    try:
        from pymongo import ReturnDocument
        doc = _db().counters.find_one_and_update(
            {"_id": name}, {"$inc": {"v": n}}, upsert=True, return_document=ReturnDocument.AFTER)
        return doc["v"] - n + 1
    except Exception:  # noqa: BLE001
        _db().counters.update_one({"_id": name}, {"$inc": {"v": n}}, upsert=True)
        return _db().counters.find_one({"_id": name})["v"] - n + 1


# ---------------------------------------------------------------------------
# Consent registry — the SINGLE SOURCE OF TRUTH for AA consents, one row PER
# CONSENT (a loan can hold several: ONETIME + PERIODIC from one journey,
# renewals over time). Each row: type ONETIME|PERIODIC, start/end date (the
# consented FI range), expiry (when the consent lapses), source LOS (fresh
# disbursals, copied from the LOS DB by the pre-flight sync) or SHERLOCK
# (procured via the Live Pull journey / manual entry). The monthly Sherlock
# Check consults ONLY this registry: eligible = an effective ACTIVE PERIODIC
# consent with a main_txn_id covering today.
# ---------------------------------------------------------------------------
def _iso_date(v):
    """Normalize any writer's date to ISO yyyy-mm-dd so the string-compare expiry gates
    hold for EVERY row, not just LOS-synced ones. Handles date/datetime objects,
    dd/mm/yyyy and dd-mm-yyyy (Indian formats), and Excel serials. See audit #8."""
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()[:10]
    s = str(v).strip()
    if not s:
        return None
    import re as _re
    m = _re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    if s.isdigit() and 20000 <= int(s) <= 80000:  # Excel serial (1900 epoch)
        from datetime import date as _date, timedelta as _td
        return (_date(1899, 12, 30) + _td(days=int(s))).isoformat()
    return s[:10] if len(s) >= 10 else s


def upsert_cm_consent(loan_id, main_txn_id=None, consent_id=None, status=None,
                      expiry=None, source=None, customer_name=None, mobile=None, by=None,
                      consent_type=None, start_date=None, end_date=None,
                      request_id=None, authoritative=False, clear_fields=None, reason=None):
    loan_id = str(loan_id or "").strip()
    if not loan_id:
        return None
    # consent_type: None means "the writer doesn't know" — PRESERVE the existing value
    # (a one-time verification retrieve must not flip a PERIODIC journey row; a source
    # with no type column must not force-PERIODIC a ONETIME row). See audit #7/#9.
    ctype = (consent_type or "").upper() or None
    if ctype not in (None, "ONETIME", "PERIODIC"):
        ctype = None
    # key: one row per (loan, consent). Manual/legacy rows without a consent id
    # get a stable synthetic handle per (loan, type) so re-saves update in place.
    handle = (consent_id or "").strip() or f"CM-{loan_id}-{ctype or 'PERIODIC'}"
    # Non-authoritative writers (e.g. the SHERLOCK auto-register) omit None fields so a
    # partial save never clobbers existing data. An authoritative writer (LOS system of
    # record) may CLEAR a mutable field to None — but ONLY when that column was actually
    # present in the source row (clear_fields). A missing/renamed column must never null a
    # real main_txn_id/date. See bug (unmatched column NULLs main_txn_id).
    mutable = {"main_txn_id": main_txn_id, "status": (status or "").upper() or None,
               "start_date": _iso_date(start_date), "end_date": _iso_date(end_date),
               "expiry": _iso_date(expiry)}
    sets = {k: v for k, v in {
        "loan_id": loan_id, "consent_id": handle, "consent_type": ctype,
        "source": (source or "").upper() or None, "request_id": request_id,
        "customer_name": customer_name, "mobile": mobile, "updated_by": by,
        **mutable,
    }.items() if v is not None}
    if authoritative:
        for k, v in mutable.items():
            if k in (clear_fields or ()):  # source carried this column -> write it (None clears)
                sets[k] = v
    sets["los_application_no"] = _loan_keys(loan_id)["los_application_no"]
    sets["updated_at"] = _now()
    key = {"loan_id": loan_id, "consent_id": handle}
    # Snapshot the prior mutable state so we can append an immutable diff (who changed
    # what) — the registry gates real billed pulls, so its mutations must be reconstructable.
    prior = _db().consent_manager.find_one(key, {"_id": 0}) or {}
    # Single atomic upsert ($setOnInsert for id/created_at/type-default) — the old
    # find-then-update pair could fork duplicate rows for the same key under two
    # concurrent first writes. See audit #16.
    from pymongo import ReturnDocument
    on_insert = {"created_at": _now(), "id": _seq("consent_manager")}
    if "consent_type" not in sets:
        on_insert["consent_type"] = "PERIODIC"  # default only on INSERT, never on update
    row = _db().consent_manager.find_one_and_update(
        key, {"$set": sets, "$setOnInsert": on_insert},
        upsert=True, return_document=ReturnDocument.AFTER, projection={"_id": 0})
    _log_consent_event(loan_id, handle, prior, row, by=by,
                       source=(source or "").upper() or None, reason=reason)
    return row


def _log_consent_event(loan_id, consent_id, prior, now_row, by=None, source=None, reason=None):
    """Append an immutable consent_events row for any mutable-field change (or creation) —
    so a resurrection/expiry/txn change always has a who/when/old->new record. The registry
    doc stays single (for gating); the history lives here. See audit (consent resurrection)."""
    tracked = ("status", "main_txn_id", "consent_type", "expiry", "start_date", "end_date")
    diff = {f: [prior.get(f), (now_row or {}).get(f)]
            for f in tracked if prior.get(f) != (now_row or {}).get(f)}
    if not prior:
        event = "CREATE"
    elif not diff:
        return  # a no-op re-save (touch) — nothing worth a history row
    else:
        event = "UPDATE"
    _db().consent_events.insert_one({
        "id": _seq("consent_events"), "at": _now(), **_loan_keys(loan_id),
        "consent_id": consent_id, "event": event, "by": by, "source": source,
        "reason": reason, "changes": diff,
    })


def consent_events_for(loan_id, limit=100):
    return list(_db().consent_events.find({"loan_id": str(loan_id or "").strip()},
                                          {"_id": 0}).sort("id", -1).limit(limit))


def log_bucket_event(cycle_id, item_id, loan_id, event, from_bucket, to_bucket,
                     reason=None, by=None, run_id=None) -> int:
    """Append-only override/override-cleared history — a supervisor override changes who
    gets called before the debit, and a retry/sweep used to wipe it with no trace. See
    audit (override no history)."""
    eid = _seq("bucket_events")
    _db().bucket_events.insert_one({
        "id": eid, "at": _now(), "cycle_id": cycle_id, "item_id": item_id,
        **_loan_keys(loan_id), "event": event, "from_bucket": from_bucket,
        "to_bucket": to_bucket, "reason": reason, "by": by, "cleared_by_run_id": run_id,
    })
    return eid


def bucket_events_for(loan_id, limit=100):
    return list(_db().bucket_events.find({"loan_id": str(loan_id or "").strip()},
                                         {"_id": 0}).sort("id", -1).limit(limit))


def log_outcome_event(cycle_id, loan_id, outcome, reason=None, by=None, prior=None) -> int:
    """Append-only NACH-outcome history — the nach_outcomes row is latest-wins (for KPIs),
    but every entry/correction is preserved here with who/when/old->new. See audit
    (NACH_ACTUAL rewritable)."""
    eid = _seq("outcome_events")
    _db().outcome_events.insert_one({
        "id": eid, "at": _now(), "cycle_id": cycle_id, **_loan_keys(loan_id),
        "outcome": outcome, "prior_outcome": prior, "reason": reason, "by": by,
    })
    return eid


def outcome_events_for(cycle_id, loan_id=None, limit=200):
    f = {"cycle_id": cycle_id}
    if loan_id:
        f["loan_id"] = str(loan_id)
    return list(_db().outcome_events.find(f, {"_id": 0}).sort("id", -1).limit(limit))


def log_job_event(name, event, by=None, detail=None) -> int:
    """Scheduler toggle/manual-run attribution — who enabled/disabled/ran a risk-control
    job and when. See audit (scheduler toggles no actor)."""
    eid = _seq("job_events")
    _db().job_events.insert_one({
        "id": eid, "at": _now(), "job": name, "event": str(event or "").upper(),
        "by": by, "detail": detail,
    })
    return eid


def _cm_effective_of(rows, today=None):
    """Pick the effective consent from a loan's rows: ACTIVE, unexpired,
    PERIODIC preferred over ONETIME, then the one valid the longest."""
    from datetime import date as _date
    today = today or _date.today().isoformat()

    def live(r):
        if str(r.get("status") or "").upper() != "ACTIVE":
            return False
        sd = str(r.get("start_date") or "")[:10]
        if sd and sd > today:
            return False  # post-dated consent — not yet in force, don't pull/bill it
        for f in ("expiry", "end_date"):
            v = str(r.get(f) or "")[:10]
            if v and v < today:
                return False
        return True

    cands = [r for r in rows if live(r)]
    if not cands:
        return None
    cands.sort(key=lambda r: (
        0 if str(r.get("consent_type") or "").upper() == "PERIODIC" else 1,
        0 if r.get("main_txn_id") else 1,
        # longest-lived last key (desc): sort ascending on negative-ish via reverse trick
        str(r.get("expiry") or r.get("end_date") or "")), )
    # prefer PERIODIC + has-txn, and among those the LATEST expiry
    best_bucket = [r for r in cands if (str(r.get("consent_type") or "").upper() ==
                                        str(cands[0].get("consent_type") or "").upper()
                                        and bool(r.get("main_txn_id")) == bool(cands[0].get("main_txn_id")))]
    best_bucket.sort(key=lambda r: str(r.get("expiry") or r.get("end_date") or ""), reverse=True)
    return best_bucket[0]


def cm_revoke_missing_los(loan_id, keep_consent_ids, by=None):
    """Mark LOS-source consents for a loan REVOKED (and clear their mandate txn) when
    they're absent from the latest authoritative LOS pull — a withdrawn mandate must not
    stay pullable. Never touches SHERLOCK-procured rows. Returns the count revoked."""
    loan_id = str(loan_id or "").strip()
    keep = set(keep_consent_ids or [])
    n = 0
    for r in _db().consent_manager.find(
            {"loan_id": loan_id, "source": "LOS", "status": {"$ne": "REVOKED"}},
            {"_id": 0, "consent_id": 1}):
        if r.get("consent_id") in keep:
            continue
        before = _db().consent_manager.find_one(
            {"loan_id": loan_id, "consent_id": r.get("consent_id")}, {"_id": 0})
        _db().consent_manager.update_many(  # update_many: covers any legacy duplicate rows
            {"loan_id": loan_id, "consent_id": r.get("consent_id")},
            {"$set": {"status": "REVOKED", "main_txn_id": None,
                      "updated_by": by, "updated_at": _now()}})
        after = _db().consent_manager.find_one(
            {"loan_id": loan_id, "consent_id": r.get("consent_id")}, {"_id": 0})
        _log_consent_event(loan_id, r.get("consent_id"), before or {}, after or {}, by=by,
                           source="LOS", reason="absent from latest LOS pull")
        n += 1
    return n


def cm_rows_for(loan_id):
    return list(_db().consent_manager.find({"loan_id": str(loan_id or "").strip()},
                                           {"_id": 0}).sort("id", -1))


def loan_for_mobile(mobile):
    """Map a mobile number to the borrower internally (last-10-digit match against the
    borrowers book, then the presentment snapshot) — so a consent journey started with
    just the customer's mobile is still tracked against the right loan. Returns
    {loan_id, customer_name, matched} only when the match is UNAMBIGUOUS (exactly one
    loan); several loans on one number -> None (never guess a customer)."""
    digits = "".join(ch for ch in str(mobile or "") if ch.isdigit())[-10:]
    if len(digits) < 10:
        return None
    hits = {}
    for coll, id_field in (("borrowers", "loan_id"), ("lms_presentment", "account_id")):
        for r in _db()[coll].find({"contact_number": {"$ne": None}},
                                  {"_id": 0, id_field: 1, "account_id": 1,
                                   "contact_number": 1, "customer_name": 1}):
            c = "".join(ch for ch in str(r.get("contact_number") or "") if ch.isdigit())[-10:]
            if c == digits:
                lid = str(r.get(id_field) or r.get("account_id") or "").strip()
                if lid:
                    hits[lid] = r.get("customer_name")
        if hits:
            break  # borrowers book is authoritative; fall to presentment only when empty
    if len(hits) != 1:
        return None
    lid, name = next(iter(hits.items()))
    return {"loan_id": lid, "customer_name": name, "matched": "mobile"}


def cm_loan_for_context(request_id=None, txn_ids=None):
    """Resolve the customer of an in-flight journey from the registry: by the consent
    request_id first, then by the mandate txn. Newest row wins."""
    ors = []
    if request_id:
        ors.append({"request_id": str(request_id)})
    for t in (txn_ids or []):
        if t:
            ors.append({"main_txn_id": t})
    if not ors:
        return None
    row = _db().consent_manager.find_one({"$or": ors}, {"_id": 0, "loan_id": 1},
                                         sort=[("id", -1)])
    return (row or {}).get("loan_id") or None


def cm_context_for_txn(main_txn_id):
    """Resolve an existing consent's journey context from its mandate (parent) txn.
    The request_id matters most: the FREE readiness statuscheck needs one, so a periodic
    pull started from nothing but the parent consent id can still poll instead of
    blind-firing the BILLED retrieve before the bank has prepared data. Newest row wins."""
    txn = str(main_txn_id or "").strip()
    if not txn:
        return None
    return _db().consent_manager.find_one(
        {"main_txn_id": txn},
        {"_id": 0, "loan_id": 1, "request_id": 1, "consent_id": 1,
         "customer_name": 1, "consent_type": 1, "status": 1, "expiry": 1},
        sort=[("id", -1)]) or None


def cm_stamp_main_txn(request_id, main_txn_id, by=None) -> int:
    """Once statuscheck reveals the journey's main_txn_id, stamp it onto the REQUESTED
    registry row (matched by request_id) so the initiate/retrieve steps can resolve the
    customer. Only fills an empty main_txn_id — never overwrites."""
    if not request_id or not main_txn_id:
        return 0
    r = _db().consent_manager.update_many(
        {"request_id": str(request_id),
         "$or": [{"main_txn_id": None}, {"main_txn_id": ""}]},
        {"$set": {"main_txn_id": main_txn_id, "updated_by": by, "updated_at": _now()}})
    return getattr(r, "modified_count", 0) or 0


def cm_for(loan_id):
    """The loan's EFFECTIVE consent (best ACTIVE PERIODIC covering today), or
    the newest row when none is live (so callers can surface EXPIRED etc.)."""
    rows = cm_rows_for(loan_id)
    if not rows:
        return None
    return _cm_effective_of(rows) or rows[0]


def cm_map(loan_ids):
    """{loan_id: effective consent doc} for a set of loans in one query."""
    ids = [str(x) for x in loan_ids if x]
    if not ids:
        return {}
    by_loan = {}
    for d in _db().consent_manager.find({"loan_id": {"$in": ids}}, {"_id": 0}).sort("id", -1):
        by_loan.setdefault(d["loan_id"], []).append(d)
    return {lid: (_cm_effective_of(rows) or rows[0]) for lid, rows in by_loan.items()}


def cm_all(limit=2000):
    return list(_db().consent_manager.find({}, {"_id": 0}).sort("id", -1).limit(limit))


def cm_expiring_before(cutoff_iso):
    """Loans whose effective consent is pullable TODAY but lapses before the
    cutoff (the next monthly run) — renewals to chase now, not next month."""
    by_loan = {}
    for d in _db().consent_manager.find({}, {"_id": 0}).sort("id", -1):
        by_loan.setdefault(d["loan_id"], []).append(d)
    out = []
    for lid, rows in by_loan.items():
        eff = _cm_effective_of(rows)
        if not eff or not eff.get("main_txn_id"):
            continue
        if str(eff.get("consent_type") or "").upper() != "PERIODIC":
            continue
        lapses = [str(eff.get(f) or "")[:10] for f in ("expiry", "end_date")]
        lapses = [x for x in lapses if x]
        lapse = min(lapses) if lapses else None
        if lapse and lapse < cutoff_iso:
            out.append({"loan_id": lid, "lapses_on": lapse,
                        "consent_id": eff.get("consent_id"),
                        "customer_name": eff.get("customer_name")})
    return sorted(out, key=lambda x: x["lapses_on"])


def cm_stats():
    from datetime import date as _date
    today = _date.today().isoformat()
    out = {"total": 0, "loans": 0, "pullable": 0, "expired": 0, "pending": 0,
           "source_los": 0, "source_sherlock": 0}
    by_loan = {}
    for d in _db().consent_manager.find({}, {"_id": 0}):
        out["total"] += 1
        if str(d.get("status") or "").upper() == "PENDING":
            out["pending"] += 1  # requested, customer hasn't approved yet
        src = str(d.get("source") or "").upper()
        if src == "LOS":
            out["source_los"] += 1
        elif src:
            out["source_sherlock"] += 1
        by_loan.setdefault(d["loan_id"], []).append(d)
    out["loans"] = len(by_loan)
    for rows in by_loan.values():
        eff = _cm_effective_of(rows)
        if eff and eff.get("main_txn_id") and str(eff.get("consent_type") or "").upper() == "PERIODIC":
            out["pullable"] += 1
            continue
        # Not pullable — count it as expired (a renewal to chase) when a PERIODIC consent
        # has lapsed by STATUS or by DATE. Date-lapsed ACTIVE rows were previously invisible.
        for r in rows:
            if str(r.get("consent_type") or "").upper() != "PERIODIC":
                continue
            ds = [str(r.get(f) or "")[:10] for f in ("expiry", "end_date") if str(r.get(f) or "")]
            if str(r.get("status") or "").upper() == "EXPIRED" or (ds and max(ds) < today):
                out["expired"] += 1
                break
    return out


# ---------------------------------------------------------------------------
# Borrowers book — every customer we ever disbursed to, irrespective of loan
# status. Upsert-only (never deleted). Source: the LMS allData report once its
# reportName is provided; until then refreshed from presentment snapshots.
# ---------------------------------------------------------------------------
def bulk_upsert_borrowers(rows, source="lms-presentment") -> int:
    docs = [r for r in rows if r.get("account_id")]
    if not docs:
        return 0
    from pymongo import UpdateOne
    now = _now()
    ops = []
    for r in docs:
        acct = str(r.get("account_id"))
        keep = {k: r.get(k) for k in
                ("customer_id", "customer_name", "contact_number", "branch_code", "branch_name",
                 "loan_amount", "emi", "emi_amount", "pos_amount", "demand_amount",
                 "disbursement_date", "maturity_date", "account_status", "od_days_num")
                if r.get(k) is not None}
        keep.update({"account_id": acct, "loan_id": acct, "los_application_no": "DUMMY",
                     "source": source, "updated_at": now})
        ops.append(UpdateOne({"account_id": acct},
                             {"$set": keep, "$setOnInsert": {"created_at": now}}, upsert=True))
    try:
        _db().borrowers.bulk_write(ops, ordered=False)
    except TypeError:  # mongomock's older bulk API — fall back to per-doc upserts
        for op in ops:
            _db().borrowers.update_one(op._filter, op._doc, upsert=True)
    # integer ids for the data browser (new docs only)
    missing = list(_db().borrowers.find({"id": {"$exists": False}}, {"_id": 1}))
    if missing:
        start = _seq_block("borrowers", len(missing))
        for i, d in enumerate(missing):
            _db().borrowers.update_one({"_id": d["_id"]}, {"$set": {"id": start + i}})
    return len(docs)


def borrowers_list(limit=3000):
    """Borrower book joined with the consent manager + AA attempt counts +
    latest classification — the single Borrowers tab dataset (Customers tab
    merged into this). LMS-book rows first; any other loan the platform knows
    (e.g. LOS pipeline apps not yet disbursed) is unioned in with its origin."""
    rows = list(_db().borrowers.find({}, {"_id": 0}).sort("account_id", 1).limit(limit))
    for r in rows:
        r["origin"] = "LMS"
    seen = {r["account_id"] for r in rows}
    # union: platform loan masters not in the LMS book (ex-Customers directory)
    for m in _db()[MASTER].find({"type": "loan"}, {"_id": 0}).sort("lms_loan_id", 1):
        lid = m.get("lms_loan_id") or m.get("loan_id")
        if not lid or lid in seen:
            continue
        seen.add(lid)
        rows.append({"account_id": lid, "loan_id": lid,
                     "los_application_no": m.get("los_application_no"),
                     "customer_name": m.get("customer_name"),
                     "emi_amount": m.get("emi_amount"), "branch_name": m.get("branch"),
                     "account_status": m.get("product") or "—",
                     "origin": (m.get("source") or "PLATFORM").upper(),
                     "updated_at": m.get("updated_at")})
    rows = rows[:limit]
    ids = [r["account_id"] for r in rows]
    consents = cm_map(ids)
    attempts = {}
    for d in _db().aa_attempts.aggregate([
        {"$match": {"loan_id": {"$in": ids}}},
        {"$group": {"_id": "$loan_id", "n": {"$sum": 1},
                    "allowed": {"$sum": {"$cond": ["$allowed", 1, 0]}}}},
    ]):
        attempts[d["_id"]] = {"attempts": d["n"], "allowed": d["allowed"]}
    latest = {}
    for d in _db().processed_data.aggregate([
        {"$match": {"loan_id": {"$in": ids}}},
        {"$sort": {"id": -1}},
        {"$group": {"_id": "$loan_id", "bucket": {"$first": "$effective_bucket"},
                    "month": {"$first": "$month"}}},
    ]):
        latest[d["_id"]] = d
    for r in rows:
        cm = consents.get(r["account_id"]) or {}
        r["consent_status"] = cm.get("status") or "NOT_LINKED"
        r["main_txn_id"] = cm.get("main_txn_id")
        r["consent_expiry"] = cm.get("expiry")
        r["aa_attempts"] = (attempts.get(r["account_id"]) or {}).get("attempts", 0)
        lt = latest.get(r["account_id"]) or {}
        r["latest_bucket"] = lt.get("bucket")
        r["latest_month"] = lt.get("month")
    return rows


class PresentmentDropFloor(RuntimeError):
    """Raised when a replace would shrink the book below the drop-floor without force."""


def bulk_replace_lms_presentment(rows, triggered_by="lms-sync", mode=None, force=False) -> int:
    """Replace the presentment snapshot. loan_id = the LMS account id; los_application_no
    stays DUMMY until an LOS<->LMS reconciliation exists. Safe: dedups account_id
    (keep last), NEVER wipes on an empty fetch, inserts the new batch BEFORE deleting the
    old, and:
      - SERIALIZES with an atomic sync lock so two concurrent replaces can't each delete
        the other's batch and leave an empty book (audit: double-sync delete);
      - refuses (PresentmentDropFloor) when the new book is <50% of the prior one unless
        force=True — a server-side-truncated but well-formed report can't silently replace
        the full book (audit: truncated report);
      - on an insert failure, deletes ONLY its own partial batch so a mixed old+new book
        can't survive and pass pre-flight (audit: partial insert)."""
    seen = {}
    for r in rows:
        if r.get("account_id"):
            seen[r["account_id"]] = r  # keep-last dedup -> no double pulls/attempt burn
    docs = list(seen.values())
    if not docs:
        return 0  # empty/failed fetch -> preserve the prior snapshot (no delete)
    prior = _db().lms_presentment.estimated_document_count()
    if not force and prior and len(docs) < 0.5 * prior:
        raise PresentmentDropFloor(
            f"Refusing to replace {prior} rows with only {len(docs)} (<50%). A truncated "
            f"report should not silently shrink the book — re-run the sync, or pass force "
            f"if this shrink is real.")
    if not acquire_month_lock("lms_sync", triggered_by):
        raise CycleBusyLike("Another presentment sync is in progress — try again shortly.")
    try:
        import uuid
        batch = uuid.uuid4().hex
        start = _seq_block("lms_presentment", len(docs))
        now = _now()
        payload = []
        for i, row in enumerate(docs):
            d = dict(row)
            d["loan_id"] = row.get("account_id")
            d["los_application_no"] = "DUMMY"
            d["id"] = start + i
            d["triggered_by"] = triggered_by
            d["sync_batch"] = batch
            d["mode"] = mode
            d["updated_at"] = now
            payload.append(d)
        try:
            _db().lms_presentment.insert_many(payload, ordered=False)  # new batch first
        except Exception:  # noqa: BLE001 — insert failed mid-way: remove our partial batch
            _db().lms_presentment.delete_many({"sync_batch": batch})
            raise
        _db().lms_presentment.delete_many(  # then drop the old rows (this batch is kept)
            {"triggered_by": triggered_by, "sync_batch": {"$ne": batch}})
        return len(payload)
    finally:
        release_month_lock("lms_sync", triggered_by)


class CycleBusyLike(RuntimeError):
    """A concurrent-sync guard (kept local so mongostore has no app import)."""


# ---------------------------------------------------------------------------
# Scheduled jobs (in-app cron) — one doc per job, state persisted across restarts
# ---------------------------------------------------------------------------
def upsert_job(name, defaults: dict):
    if not _db().jobs.find_one({"name": name}):
        _db().jobs.insert_one({"name": name, "enabled": True, "last_run_at": None,
                               "last_status": None, "last_detail": None, **defaults})


def get_job(name):
    return _db().jobs.find_one({"name": name}, {"_id": 0})


def list_jobs():
    return list(_db().jobs.find({}, {"_id": 0}).sort("name", 1))


def update_job(name, **fields):
    if fields:
        _db().jobs.update_one({"name": name}, {"$set": fields})


# ---------------------------------------------------------------------------
# Notes & flags (persist across cycles) + nudges (mock WhatsApp/SMS touches)
# ---------------------------------------------------------------------------
CUSTOMER_FLAGS = ["DISPUTE", "HARDSHIP", "RESTRUCTURE", "DO_NOT_CALL"]


def add_note(loan_id, text, by):
    doc = {"id": _seq("customer_notes"), **_loan_keys(loan_id),
           "text": (text or "").strip(), "by": by, "created_at": _now()}
    _db().customer_notes.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


def notes_for_loan(loan_id, limit=30):
    return list(_db().customer_notes.find({"loan_id": loan_id}, {"_id": 0})
                .sort("id", -1).limit(limit))


def set_flag(loan_id, flag, active, by):
    op = "$addToSet" if active else "$pull"
    _db()[MASTER].update_one({"type": "loan", "lms_loan_id": loan_id},
                             {op: {"flags": flag}, "$set": {"updated_at": _now()}}, upsert=True)
    add_note(loan_id, f"Flag {'set' if active else 'cleared'}: {flag}", by)
    return loan_flags(loan_id)


def loan_flags(loan_id):
    doc = _db()[MASTER].find_one({"type": "loan", "lms_loan_id": loan_id}, {"_id": 0, "flags": 1})
    return (doc or {}).get("flags") or []


def flags_by_loan(loan_ids):
    out = {}
    for d in _db()[MASTER].find({"type": "loan", "lms_loan_id": {"$in": list(loan_ids)},
                                 "flags.0": {"$exists": True}}, {"_id": 0, "lms_loan_id": 1, "flags": 1}):
        out[d["lms_loan_id"]] = d.get("flags") or []
    return out


def add_nudge(cycle_id, item_id, loan_id, channel, message, shortfall, by):
    doc = {"id": _seq("nudges"), "cycle_id": cycle_id, "item_id": item_id, **_loan_keys(loan_id),
           "channel": channel, "message": message, "shortfall": shortfall,
           "by": by, "status": "MOCK_SENT", "created_at": _now()}
    _db().nudges.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


def nudges_by_item(cycle_id):
    out = {}
    for d in _db().nudges.aggregate([
        {"$match": {"cycle_id": cycle_id}},
        {"$group": {"_id": "$item_id", "n": {"$sum": 1}, "last": {"$max": "$created_at"}}},
    ]):
        out[d["_id"]] = {"count": d["n"], "last_at": d["last"]}
    return out


def nudges_for_loan(loan_id, limit=20):
    return list(_db().nudges.find({"loan_id": loan_id}, {"_id": 0}).sort("id", -1).limit(limit))


# ---------------------------------------------------------------------------
# Supervisor stats
# ---------------------------------------------------------------------------
def agent_stats(cycle_id):
    """Per-agent activity for one cycle: touches, PTPs made and their fate."""
    agents = {}
    for d in _db().dispositions.find({"cycle_id": cycle_id}, {"_id": 0}).sort("id", 1):
        by = d.get("by") or "?"
        if by == "sherlock":  # engine verdicts attribute to the PTP's agent below
            continue
        a = agents.setdefault(by, {"agent": by, "touches": 0, "ptp": 0, "kept": 0,
                                   "broken": 0, "last_at": None})
        a["touches"] += 1
        a["last_at"] = d.get("created_at")
        if d.get("status") == "PTP":
            a["ptp"] += 1
    # PTP outcomes: engine writes PTP_KEPT/PTP_BROKEN on the item; credit the agent who made the PTP
    for it in _db().cycle_items.find({"cycle_id": cycle_id, "disposition.status":
                                      {"$in": ["PTP_KEPT", "PTP_BROKEN"]}}, {"_id": 0}):
        maker = next((d.get("by") for d in
                      _db().dispositions.find({"item_id": it["id"], "status": "PTP"},
                                              {"_id": 0, "by": 1}).sort("id", -1)), None)
        if maker and maker in agents:
            agents[maker]["kept" if it["disposition"]["status"] == "PTP_KEPT" else "broken"] += 1
    for a in agents.values():
        done = a["kept"] + a["broken"]
        a["kept_rate_pct"] = round(100.0 * a["kept"] / done, 0) if done else None
    return sorted(agents.values(), key=lambda x: -x["touches"])


# ---------------------------------------------------------------------------
# Customer timeline — every event on one stream
# ---------------------------------------------------------------------------
def customer_timeline(loan_id, limit=80):
    ev = []

    def add(at, typ, title, detail=None, by=None):
        if at and len(str(at)) == 10:
            at = str(at) + " 10:00:00"
        ev.append({"at": str(at or ""), "type": typ, "title": title, "detail": detail, "by": by})

    runs = list(_db().checks.find({"loan_id": loan_id}, {"_id": 0}).sort("id", -1).limit(15))
    run_ids = [r["id"] for r in runs]
    for r in runs:
        add(r.get("created_at"), "check",
            f"AA check run #{r['id']}" + (f" (cycle #{r['cycle_id']})" if r.get("cycle_id") else " (ad-hoc)"),
            f"status {r.get('status')}")
    for p in _db().pulls.find({"run_id": {"$in": run_ids}}, {"_id": 0, "raw_report_json": 0}):
        if p.get("status") == "RETRIEVED":
            add(p.get("updated_at"), "pull", f"{p.get('bank_name')} balance retrieved",
                f"₹{(p.get('available_balance') or 0):,.2f} · {p.get('decision') or ''}")
        elif p.get("status") == "CAPPED":
            add(p.get("created_at"), "capped", f"{p.get('bank_name')} pull blocked",
                "monthly attempt cap reached")
    for c in _db().consents.find({"lms_loan_id": loan_id}, {"_id": 0}):
        add(c.get("created_at"), "consent", "Consent requested",
            f"{c.get('handle')} · {c.get('bank_name') or ''}")
    for d in _db().dispositions.find({"loan_id": loan_id}, {"_id": 0}):
        add(d.get("created_at"), "call",
            {"CONTACTED": "Customer contacted", "PTP": "Promise to pay",
             "NO_RESPONSE": "No response", "PAID": "Paid / topped up",
             "WILL_BOUNCE": "Marked will-bounce", "PTP_KEPT": "Promise KEPT ✓",
             "PTP_BROKEN": "Promise BROKEN"}.get(d.get("status"), d.get("status")),
            (d.get("remarks") or "") + (f" · by {d.get('ptp_date')}" if d.get("ptp_date") else ""),
            d.get("by"))
    for n in _db().nudges.find({"loan_id": loan_id}, {"_id": 0}):
        add(n.get("created_at"), "nudge", f"{n.get('channel')} nudge sent",
            f"short by ₹{(n.get('shortfall') or 0):,.0f}", n.get("by"))
    for it in _db().cycle_items.find({"loan_id": loan_id, "override_at": {"$ne": None}}, {"_id": 0}):
        add(it.get("override_at"), "override", f"Bucket overridden → {it.get('override_bucket')}",
            it.get("override_reason"), it.get("override_by"))
    for o in _db().nach_outcomes.find({"loan_id": loan_id}, {"_id": 0}):
        add(o.get("presented_on"), "nach",
            f"NACH presented — {o.get('outcome')}", f"₹{(o.get('amount') or 0):,.0f}")
        if o.get("rep_outcome"):
            add(o.get("retry_date"), "nach", f"Re-presented — {o.get('rep_outcome')}",
                o.get("retry_reason"))
    for nt in _db().customer_notes.find({"loan_id": loan_id}, {"_id": 0}):
        add(nt.get("created_at"), "note", "Note", nt.get("text"), nt.get("by"))
    ev.sort(key=lambda e: e["at"], reverse=True)
    return ev[:limit]


# ---------------------------------------------------------------------------
# Customers directory — every unique loan fetched from the LMS so far
# ---------------------------------------------------------------------------
def list_customers():
    loans = list(_db()[MASTER].find({"type": "loan"}, {"_id": 0}))
    accts = list(_db()[MASTER].find({"type": "bank_account"}, {"_id": 0}))
    by_loan = {}
    for a in accts:
        by_loan.setdefault(a.get("lms_loan_id"), []).append(a)
    out = []
    for l in loans:
        lid = l.get("lms_loan_id")
        rows = by_loan.get(lid, [])
        repay = next((a for a in rows if a.get("is_repayment")), None)
        latest = _db().processed_data.find_one(
            {"loan_id": lid}, {"_id": 0, "month": 1, "effective_bucket": 1, "repay": 1},
            sort=[("month", -1), ("id", -1)])
        out.append({
            "loan_id": lid, "customer_name": l.get("customer_name"),
            "emi_amount": l.get("emi_amount"), "los_application_no": l.get("los_application_no"),
            "branch": l.get("branch"), "state": l.get("state"), "flags": l.get("flags") or [],
            "accounts": len(rows), "aa_accounts": sum(1 for a in rows if a.get("aa_enabled")),
            "repay_bank": (repay or {}).get("bank_name"),
            "consent_status": (repay or {}).get("consent_status"),
            "latest_bucket": (latest or {}).get("effective_bucket"),
            "latest_month": (latest or {}).get("month"),
            "latest_ratio": ((latest or {}).get("repay") or {}).get("ratio"),
            "updated_at": l.get("updated_at"),
        })
    out.sort(key=lambda x: str(x.get("loan_id") or ""))
    return out


# ---------------------------------------------------------------------------
# Customer 360 — everything the app knows about one loan/customer
# ---------------------------------------------------------------------------
def customer_360(loan_id):
    loan = _db()[MASTER].find_one({"type": "loan", "lms_loan_id": loan_id}, {"_id": 0})
    accounts = list(_db()[MASTER].find({"type": "bank_account", "lms_loan_id": loan_id}, {"_id": 0})
                    .sort("updated_at", -1))
    consents = list(_db()[MASTER].find({"type": "consent", "lms_loan_id": loan_id}, {"_id": 0})
                    .sort("updated_at", -1))
    runs = list(_db().checks.find({"loan_id": loan_id}, {"_id": 0}).sort("id", -1).limit(10))
    run_ids = [r["id"] for r in runs]
    consent_logs = list(_db().api_logs.find(
        {"run_id": {"$in": run_ids}, "kind": "consent"}, {"_id": 0}).sort("id", -1))
    latest_pulls = []
    all_pulls = []
    if run_ids:
        latest_pulls = list(_db().pulls.find(
            {"run_id": run_ids[0]}, {"_id": 0, "raw_report_json": 0}).sort("id", 1))
        # Full pull history across this loan's runs (newest first), with run context.
        run_cycle = {r["id"]: r.get("cycle_id") for r in runs}
        all_pulls = list(_db().pulls.find(
            {"run_id": {"$in": run_ids}}, {"_id": 0, "raw_report_json": 0}).sort("id", -1).limit(60))
        for p in all_pulls:
            p["cycle_id"] = run_cycle.get(p.get("run_id"))
    # Latest cycle item's AA read — so the 360 shows the same risk signals as the worklist.
    li = _db().cycle_items.find_one(
        {"loan_id": loan_id}, {"_id": 0, "aa_risk": 1, "aa_downgrade": 1, "retime": 1,
                               "tags": 1, "bucket": 1, "bucket_by_balance": 1, "risk_score": 1},
        sort=[("id", -1)]) or {}
    # Consent journey: the registry rows + the loan's Digitap call trail (light
    # projection — the full request/response JSON stays in the Live Pull ledger).
    registry = cm_rows_for(loan_id)
    aa_calls = list(_db().aa_live_calls.find(
        {"loan_id": loan_id},
        {"_id": 0, "id": 1, "at": 1, "by": 1, "kind": 1, "mode": 1, "live": 1,
         "ok": 1, "error": 1, "request_id": 1, "txn_id": 1}).sort("id", -1).limit(30))
    return {
        "loan_id": loan_id, "loan": loan, "accounts": accounts, "consents": consents,
        "consent_logs": consent_logs, "runs": runs, "latest_pulls": latest_pulls,
        "pulls": all_pulls, "processed": processed_for_loan(loan_id),
        "registry": registry, "aa_calls": aa_calls,
        "flags": (loan or {}).get("flags") or [], "notes": notes_for_loan(loan_id),
        "timeline": customer_timeline(loan_id),
        "aa_risk": li.get("aa_risk"), "aa_downgrade": li.get("aa_downgrade"),
        "retime": li.get("retime"), "aa_tags": li.get("tags"), "risk_score": li.get("risk_score"),
    }


# ---------------------------------------------------------------------------
# Data browser — read-only, allow-listed collections (view Mongo in the app)
# ---------------------------------------------------------------------------
# collection -> {label, sort field, fields hidden from the browser}
DATA_COLLECTIONS = {
    "ppdata":         {"label": "Master data (ppdata)", "sort": "updated_at", "hide": []},
    "cycles":         {"label": "Cycles", "sort": "id", "hide": []},
    "cycle_items":    {"label": "Cycle items", "sort": "id", "hide": []},
    "processed_data": {"label": "Processed data", "sort": "id", "hide": []},
    "predictions":    {"label": "Prediction snapshots (frozen)", "sort": "id", "hide": []},
    "aa_live_calls":  {"label": "Live AA calls (Digitap)", "sort": "id", "hide": []},
    "aa_call_payloads":{"label": "AA call payloads (full request + response)", "sort": "id", "hide": []},
    "los_portfolio":  {"label": "LOS portfolio (Engrow sync)", "sort": "id", "hide": []},
    "los_calls":      {"label": "LOS API calls (Engrow)", "sort": "id", "hide": []},
    "lms_presentment":{"label": "LMS presentment (Encore)", "sort": "id", "hide": []},
    "lms_calls":      {"label": "LMS API calls (Encore)", "sort": "id", "hide": []},
    "consent_manager":{"label": "Consent manager (per loan)", "sort": "id", "hide": []},
    "consent_events": {"label": "Consent change history (append-only)", "sort": "id", "hide": []},
    "bucket_events":  {"label": "Bucket override history (append-only)", "sort": "id", "hide": []},
    "outcome_events": {"label": "NACH outcome history (append-only)", "sort": "id", "hide": []},
    "job_events":     {"label": "Scheduler toggle/run history", "sort": "id", "hide": []},
    "auth_log":       {"label": "Auth audit trail (logins, roles, sessions)", "sort": "id", "hide": []},
    "borrowers":      {"label": "Borrowers book (all disbursed)", "sort": "id", "hide": []},
    "aa_attempts":    {"label": "AA attempt ledger", "sort": "id", "hide": []},
    "checks":         {"label": "Checks (history)", "sort": "id", "hide": []},
    "accounts":       {"label": "Resolved accounts", "sort": "id", "hide": ["raw_row"]},
    "pulls":          {"label": "AA pulls", "sort": "id", "hide": ["raw_report_json"]},
    "consents":       {"label": "Consent requests", "sort": "id", "hide": []},
    "dispositions":   {"label": "Dispositions (worklist)", "sort": "id", "hide": []},
    "nudges":         {"label": "Nudges (WhatsApp/SMS)", "sort": "id", "hide": []},
    "customer_notes": {"label": "Customer notes", "sort": "id", "hide": []},
    "nach_outcomes":  {"label": "NACH outcomes", "sort": "id", "hide": []},
    "jobs":           {"label": "Scheduled jobs", "sort": "name", "hide": []},
    "api_logs":       {"label": "API logs (req/resp)", "sort": "id", "hide": []},
    "users":          {"label": "Users", "sort": "username", "hide": ["password_hash"]},
    "roles":          {"label": "Roles", "sort": "name", "hide": []},
}


def data_collections():
    out = []
    for name, cfg in DATA_COLLECTIONS.items():
        out.append({"name": name, "label": cfg["label"], "count": _db()[name].estimated_document_count()})
    return out


# Free-text search across a collection: regex-match q against the id/name/txn
# fields that actually carry meaning (works on any collection — clauses on absent
# fields simply don't match), plus exact match on numeric id-ish fields.
_SEARCH_FIELDS = ["loan_id", "lms_loan_id", "los_application_no", "application_no",
                  "account_id", "customer_id", "contact_number",
                  "customer_name", "bank_name", "account_ref",
                  "account_key", "request_id", "txn_id", "main_txn_id", "child_txn_id",
                  "fetch_type",
                  "handle", "kind", "endpoint", "mode", "status", "bucket", "by", "month",
                  "triggered_by", "account_number", "ifsc", "source", "error", "name"]
_SEARCH_NUMERIC = ["id", "run_id", "cycle_id", "pull_id", "account_id", "request_id", "cycle_item_id"]


def _search_query(q):
    q = (q or "").strip()
    if not q:
        return {}
    import re
    ors = [{f: {"$regex": re.escape(q), "$options": "i"}} for f in _SEARCH_FIELDS]
    if q.lstrip("-").isdigit():
        n = int(q)
        ors += [{f: n} for f in _SEARCH_NUMERIC]
    return {"$or": ors}


def data_documents(name, limit=50, skip=0, q=None):
    if name not in DATA_COLLECTIONS:
        raise ValueError(f"Unknown collection '{name}'")
    cfg = DATA_COLLECTIONS[name]
    proj = {"_id": 0}
    for f in cfg["hide"]:
        proj[f] = 0
    limit = min(max(int(limit), 1), 200)
    skip = max(int(skip), 0)
    query = _search_query(q)
    coll = _db()[name]
    count = coll.count_documents(query) if query else coll.estimated_document_count()
    cur = coll.find(query, proj).sort(cfg["sort"], -1).skip(skip).limit(limit)
    return {
        "collection": name, "label": cfg["label"], "count": count,
        "limit": limit, "skip": skip, "q": (q or "").strip(),
        "documents": list(cur),
    }
