"""MongoDB connection + bootstrap for DPD Early-Warning.

Connection modes (env):
  MONGO_MOCK=true   -> in-memory mongomock (dev / sandbox; no server, not durable)
  MONGO_MOCK=false  -> real MongoDB via pymongo at MONGO_URI (durable)

On first connection it pings the server (fails fast with a clear error), creates
indexes, seeds the roles catalog, and seeds a bootstrap admin
(BOOTSTRAP_ADMIN_USER / BOOTSTRAP_ADMIN_PASSWORD, default admin/admin123) when no
users exist.

Connection string examples:
  Local server : mongodb://127.0.0.1:27017
  Atlas (SRV)  : mongodb+srv://USER:PASS@cluster0.xxxx.mongodb.net/?retryWrites=true&w=majority
"""
import os

# Default OFF: a deploy that loses its .env must fail loud (can't reach Mongo)
# rather than silently boot on non-durable in-memory storage. Dev/tests set it
# to true explicitly.
MONGO_MOCK = os.getenv("MONGO_MOCK", "false").lower() in ("1", "true", "yes")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017")
MONGO_DB = os.getenv("MONGO_DB", "dpd_early_warning")
CONNECT_TIMEOUT_MS = int(os.getenv("MONGO_TIMEOUT_MS", "6000"))

_client = None
_db = None
_last_error = None


class DBUnavailable(RuntimeError):
    """Raised when a real MongoDB cannot be reached."""


def _connect():
    """Create the client (mongomock or pymongo) and verify connectivity."""
    global _last_error
    if MONGO_MOCK:
        import mongomock
        print("⚠  MONGO_MOCK is ON — using in-memory mongomock; data is NOT durable. "
              "Set MONGO_MOCK=false + MONGO_URI for real persistence.")
        return mongomock.MongoClient()
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=CONNECT_TIMEOUT_MS, appname="dpd-early-warning")
    try:
        client.admin.command("ping")  # fail fast if the server is unreachable
    except PyMongoError as e:  # noqa: BLE001
        _last_error = str(e)
        raise DBUnavailable(f"Cannot reach MongoDB at {_safe_uri()}: {e}")
    return client


def get_db():
    global _client, _db
    if _db is not None:
        return _db
    _client = _connect()
    _db = _client[MONGO_DB]
    _ensure(_db)
    return _db


def _safe_uri() -> str:
    """MONGO_URI with any credentials masked, for logs/health."""
    uri = MONGO_URI
    if "@" in uri and "//" in uri:
        scheme, rest = uri.split("//", 1)
        creds, host = rest.split("@", 1)
        return f"{scheme}//***@{host}"
    return uri


def health() -> dict:
    """Connectivity snapshot for /api/health — never raises."""
    info = {"mode": "mock" if MONGO_MOCK else "mongodb", "uri": "in-memory" if MONGO_MOCK else _safe_uri(),
            "db": MONGO_DB, "connected": False, "collections": {}, "error": None}
    try:
        db = get_db()
        if not MONGO_MOCK:
            db.client.admin.command("ping")
        info["connected"] = True
        # Report EVERY collection that exists, not a hardcoded legacy subset — otherwise the
        # newer collections (lms_presentment, consent_manager, borrowers, los_portfolio,
        # los_calls/lms_calls/aa_live_calls, predictions) are invisible to the health check.
        try:
            names = sorted(db.list_collection_names())
        except Exception:  # noqa: BLE001 — mongomock/edge fallback to the known set
            names = ["users", "roles", "ppdata", "consents", "checks", "accounts", "pulls",
                     "api_logs", "cycles", "cycle_items", "processed_data", "aa_attempts",
                     "dispositions", "nach_outcomes", "jobs", "nudges", "customer_notes",
                     "lms_presentment", "consent_manager", "borrowers", "los_portfolio",
                     "los_calls", "lms_calls", "aa_live_calls", "predictions"]
        for c in names:
            info["collections"][c] = db[c].estimated_document_count()
        info["ppdata_by_type"] = {
            t: db.ppdata.count_documents({"type": t}) for t in ("loan", "bank_account", "consent")
        }
    except Exception as e:  # noqa: BLE001
        info["error"] = str(e)
    return info


def _ensure(db):
    db.users.create_index("username", unique=True)
    db.roles.create_index("name", unique=True)
    # ppdata = master data (type-discriminated): loan / bank_account / consent
    db.ppdata.create_index([("type", 1), ("lms_loan_id", 1)])
    db.ppdata.create_index([("type", 1), ("bank_account_uid", 1)])
    db.ppdata.create_index([("type", 1), ("handle", 1)])
    db.consents.create_index("handle")
    db.checks.create_index("id", unique=True)
    db.checks.create_index("cycle_id")
    db.pulls.create_index("id", unique=True)
    db.accounts.create_index("id", unique=True)
    db.api_logs.create_index([("run_id", 1), ("id", 1)])
    # Monthly pre-NACH cycle
    db.cycles.create_index("id", unique=True)
    db.cycle_items.create_index("id", unique=True)
    db.cycle_items.create_index([("cycle_id", 1), ("id", 1)])
    db.cycle_items.create_index("run_id")
    db.processed_data.create_index("id", unique=True)
    db.processed_data.create_index([("cycle_id", 1), ("loan_id", 1)], unique=True)
    db.processed_data.create_index("loan_id")
    db.aa_attempts.create_index([("account_key", 1), ("month", 1)])
    # Worklist dispositions, NACH outcomes, scheduled jobs
    db.dispositions.create_index("id", unique=True)
    db.dispositions.create_index([("cycle_id", 1), ("item_id", 1)])
    db.customer_notes.create_index("loan_id")
    db.nudges.create_index([("cycle_id", 1), ("item_id", 1)])
    db.nudges.create_index("loan_id")
    db.nach_outcomes.create_index("id", unique=True)
    db.nach_outcomes.create_index([("cycle_id", 1), ("loan_id", 1)], unique=True)
    db.jobs.create_index("name", unique=True)
    # Consent-journey joins: the Digitap ledger and the registry are queried by
    # loan (customer view), request_id (journey), and main_txn_id (mandate).
    db.aa_live_calls.create_index("id", unique=True)
    db.aa_live_calls.create_index("loan_id")
    db.aa_live_calls.create_index("request_id")
    db.aa_live_calls.create_index("txn_id")
    # UNIQUE: the registry upsert keys on (loan_id, consent_id) — without uniqueness two
    # concurrent first writes could fork duplicate rows (and a revoke then miss one). If a
    # legacy deployment already holds duplicates, fall back to non-unique rather than
    # failing boot; the atomic upsert still prevents new forks. Audit #16.
    try:
        db.consent_manager.create_index([("loan_id", 1), ("consent_id", 1)], unique=True)
    except Exception:  # noqa: BLE001 — duplicate legacy data
        print("⚠  consent_manager (loan_id, consent_id) has duplicates — unique index skipped; "
              "dedup the collection to enforce uniqueness.")
        db.consent_manager.create_index([("loan_id", 1), ("consent_id", 1)])
    db.consent_manager.create_index("request_id")
    db.consent_manager.create_index("main_txn_id")
    db.borrowers.create_index("contact_number")
    db.auth_log.create_index("id", unique=True)
    db.auth_log.create_index([("username", 1), ("id", -1)])
    # Append-only audit histories (consent / bucket-override / NACH-outcome / job)
    for _c in ("consent_events", "bucket_events", "outcome_events", "job_events"):
        db[_c].create_index("id", unique=True)
    db.consent_events.create_index("loan_id")
    db.bucket_events.create_index("loan_id")
    db.outcome_events.create_index([("cycle_id", 1), ("id", -1)])
    db.job_events.create_index([("job", 1), ("id", -1)])

    import rbac
    for name, perms in rbac.ROLE_PERMISSIONS.items():
        # Roles are authoritative from code — keep permissions in sync on startup.
        db.roles.update_one(
            {"name": name},
            {"$set": {"name": name, "permissions": perms, "description": rbac.ROLE_DESC.get(name, "")}},
            upsert=True,
        )

    if db.users.count_documents({}) == 0:
        import userstore
        from datetime import datetime, timezone
        u = os.getenv("BOOTSTRAP_ADMIN_USER", "admin")
        p = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "admin123")
        db.users.insert_one({
            "username": u, "password_hash": userstore.hash_password(p), "role": "admin",
            "status": "active", "created_at": userstore._now(),  # IST (project timezone)
        })

    # Demo team users for the worklist roles (mock/demo installs only).
    if os.getenv("BOOTSTRAP_DEMO_USERS", "false").lower() in ("1", "true", "yes"):
        import userstore
        from datetime import datetime, timezone
        pw = os.getenv("BOOTSTRAP_DEMO_PASSWORD", "demo123")
        for uname, role in (("telecaller1", "telecaller"), ("field1", "field"),
                            ("ops1", "operator"), ("viewer1", "viewer")):
            if not db.users.find_one({"username": uname}):
                db.users.insert_one({
                    "username": uname, "password_hash": userstore.hash_password(pw),
                    "role": role, "status": "active",
                    "created_at": userstore._now(),  # IST (project timezone)
                })
