"""DPD Early-Warning — FastAPI backend (MongoDB).

Pre-NACH balance check: an operator enters a Loan account ID; the app reads the
EMI from the LMS, resolves every bank account, flags AA-enabled ones (+ consent
status), runs the Digitap period pull, and flags whether the balance covers the
upcoming EMI. Auth + RBAC, master data, history and API logs live in MongoDB.
"""
import os
import secrets
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

# Load .env BEFORE importing modules that read env at import time.
load_dotenv()

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

import dbconfig
import digitap
import insights
import mongostore as store
import rbac
import checker
import cycle
import scheduler
from checker import AA_FETCH_CUTOFF, DPD_BUFFER

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

import db as _dbmod

# The session cookie is only SIGNED (not encrypted) with SECRET_KEY — a known/weak key
# lets anyone forge an admin cookie. In a LIVE deployment (durable Mongo) refuse to boot
# on an empty or placeholder key rather than silently accepting a forgeable one. Dev/mock
# may fall back to a per-boot random key. See audit (predictable SECRET_KEY).
_WEAK_SECRETS = {"", "dev-secret-not-for-production-0123456789abcdef", "changeme", "secret"}
SECRET_KEY = os.getenv("SECRET_KEY") or ""
if (SECRET_KEY.strip() in _WEAK_SECRETS or len(SECRET_KEY.strip()) < 32):
    if not _dbmod.MONGO_MOCK:
        raise SystemExit(
            "FATAL: SECRET_KEY is unset/weak while MONGO_MOCK=false (live). Set a strong "
            "SECRET_KEY (python -c 'import secrets;print(secrets.token_hex(32))') in the "
            "environment/secret manager before starting. Cookies are signed with this key; "
            "a guessable value allows full admin session forgery.")
    SECRET_KEY = secrets.token_hex(32)  # dev/mock only — ephemeral per boot
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE", str(8 * 3600)))
# Secure attribute on: never send the auth cookie over plaintext HTTP in a live posture.
# Env-gated so local http dev still works. See audit (cookie not Secure).
SESSION_HTTPS_ONLY = os.getenv("SESSION_HTTPS_ONLY",
                               "false" if _dbmod.MONGO_MOCK else "true").lower() in ("1", "true", "yes")

app = FastAPI(title="Sherlock — pre-NACH intelligence (DPD Early-Warning)")
app.add_middleware(
    SessionMiddleware, secret_key=SECRET_KEY, max_age=SESSION_MAX_AGE,
    same_site="lax", https_only=SESSION_HTTPS_ONLY,
)


@app.on_event("startup")
def _startup():
    # Seed jobs, recover cycles orphaned by the previous process, start the cron.
    try:
        scheduler.start()
    except Exception:  # noqa: BLE001 — a scheduler problem must not block the app
        import traceback
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Auth / RBAC dependencies
# ---------------------------------------------------------------------------
def require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    _check_session_ver(request, user)
    return user


def _check_session_ver(request: Request, username: str):
    """Server-side revocation: the cookie carries the session_ver stamped at login;
    if an admin bumped the user's ver (revoke-sessions / password change) every
    older cookie dies here, on the very next request. Also kills a deleted/suspended
    account. Stashes the fresh user doc so role checks read the DB, not the cookie."""
    u = store.get_user(username)
    if not u or u.get("status", "active") != "active" \
            or request.session.get("ver", 0) != u.get("session_ver", 0):
        request.session.clear()
        raise HTTPException(status_code=401, detail="Session revoked — sign in again")
    request.state.user_doc = u


def _real_role(request: Request) -> str:
    """The user's CURRENT role from the DB (cached on request.state by
    _check_session_ver), never the role cached in the cookie — so a demotion or
    role change takes effect on the very next request. See audit (RBAC stale privilege)."""
    u = getattr(request.state, "user_doc", None)
    if u is None:
        u = store.get_user(request.session.get("user")) or {}
    return u.get("role") or request.session.get("role")


def effective_role(request: Request) -> str:
    """The role permissions are checked against. Admins can temporarily 'view as'
    another role (session['viewas']) — but only while their REAL (DB) role is admin,
    so a demoted admin can't keep an elevated view."""
    real = _real_role(request)
    va = request.session.get("viewas")
    return va if (va and real == "admin") else real


def require_permission(perm: str):
    """Dependency factory: 403 unless the session's effective role grants `perm`."""
    def dep(request: Request) -> str:
        user = request.session.get("user")
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        _check_session_ver(request, user)
        if perm not in store.role_permissions(effective_role(request)):
            raise HTTPException(status_code=403, detail=f"Missing permission: {perm}")
        return user
    return dep


# ---------------------------------------------------------------------------
# Auth API
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str
    password: str


DEFAULT_PASSWORDS = ("admin123", "demo123")  # bootstrap/demo defaults — flag on login

# Demo quick-login buttons on the sign-in page — OFF by default (never on a real production
# login: they'd advertise a one-click admin sign-in over live customer data). Turn on with
# SHOW_DEMO_LOGINS=true in .env for a demo/branch-training instance. The credentials are sent
# to the client ONLY when the flag is on, so nothing leaks in production.
SHOW_DEMO_LOGINS = os.getenv("SHOW_DEMO_LOGINS", "false").lower() in ("1", "true", "yes")
DEMO_LOGINS = [
    {"label": "Admin", "sub": "full control", "u": "admin", "p": "admin123"},
    {"label": "Operator", "sub": "runs the cycle", "u": "ops1", "p": "demo123"},
    {"label": "Telecaller", "sub": "stretched queue", "u": "telecaller1", "p": "demo123"},
    {"label": "Field", "sub": "shortfall queue", "u": "field1", "p": "demo123"},
    {"label": "Viewer", "sub": "read-only", "u": "viewer1", "p": "demo123"},
]


@app.get("/api/public-config")
def api_public_config():
    """Unauthenticated: what the sign-in page needs before login. Demo credentials are
    included ONLY when SHOW_DEMO_LOGINS is on — otherwise the list is empty."""
    return {"show_demo_logins": SHOW_DEMO_LOGINS,
            "demo_logins": DEMO_LOGINS if SHOW_DEMO_LOGINS else []}


def _client_ip(request: Request):
    return getattr(request.client, "host", None)


@app.post("/api/login")
def api_login(req: LoginRequest, request: Request):
    user = store.authenticate(req.username, req.password)
    if not user:
        store.log_auth("LOGIN_FAIL", req.username, ip=_client_ip(request))
        raise HTTPException(401, "Invalid username or password")
    role = user.get("role", "viewer")
    must_change = bool(user.get("must_change_password"))
    default_pw = req.password in DEFAULT_PASSWORDS or must_change
    request.session["user"] = user["username"]
    request.session["role"] = role
    request.session["ver"] = user.get("session_ver", 0)  # server-side revocation anchor
    request.session["default_pw"] = default_pw
    request.session["must_change"] = must_change
    request.session.pop("viewas", None)  # never carry a view-as across logins
    store.log_auth("LOGIN_OK", user["username"], ip=_client_ip(request),
                   detail="DEFAULT/FIRST-LOGIN PASSWORD — must change" if default_pw else None)
    return {"ok": True, "username": user["username"], "role": role, "branch": user.get("branch"),
            "permissions": store.role_permissions(role),
            "worklist_bucket": rbac.ROLE_WORKLIST.get(role),
            "default_password": default_pw, "must_change_password": must_change}


@app.post("/api/logout")
def api_logout(request: Request):
    if request.session.get("user"):
        store.log_auth("LOGOUT", request.session["user"], ip=_client_ip(request))
    request.session.clear()
    return {"ok": True}


@app.get("/api/health")
def api_health(request: Request):
    """Connectivity check. Unauthenticated callers get a bare up/down — the full
    snapshot (collection names + counts, db name, uri host) is an internals map
    and requires a signed-in session."""
    import db as _db
    info = _db.health()
    if not request.session.get("user"):
        return {"connected": info.get("connected"), "mode": info.get("mode")}
    return info


@app.get("/api/me")
def api_me(request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(401, "Not authenticated")
    _check_session_ver(request, user)
    real = _real_role(request)
    role = effective_role(request)
    u = getattr(request.state, "user_doc", None) or {}
    return {"username": user, "role": role, "real_role": real, "branch": u.get("branch"),
            "viewing_as": bool(request.session.get("viewas")),
            "permissions": store.role_permissions(role),
            "worklist_bucket": rbac.ROLE_WORKLIST.get(role),
            "default_password": bool(request.session.get("default_pw")),
            "must_change_password": bool(request.session.get("must_change"))}


class ViewAsRequest(BaseModel):
    role: Optional[str] = None  # None / "admin" -> back to the real role


@app.post("/api/viewas")
def api_viewas(body: ViewAsRequest, request: Request):
    """Admin-only: preview the app exactly as another role sees it. The real
    identity stays admin (so switching back is always allowed)."""
    if not request.session.get("user"):
        raise HTTPException(401, "Not authenticated")
    if _real_role(request) != "admin":  # DB role, not the cookie — a demoted admin can't
        raise HTTPException(403, "Only admins can switch the viewing role")
    role = (body.role or "").strip()
    if not role or role == "admin":
        request.session.pop("viewas", None)
    else:
        if role not in rbac.ROLE_PERMISSIONS:
            raise HTTPException(400, f"Unknown role '{role}'")
        request.session["viewas"] = role
    store.log_auth("VIEWAS", request.session["user"],
                   detail=f"viewing as {role or 'admin (self)'}", ip=_client_ip(request))
    return api_me(request)


@app.get("/api/config")
def api_config(user: str = Depends(require_user)):
    return {
        "digitap_mock": digitap.MOCK, "dpd_buffer": DPD_BUFFER,
        "auto_delay": checker.AUTO_DELAY, "aa_cutoff": AA_FETCH_CUTOFF.isoformat(),
        "mongo_mock": __import__("db").MONGO_MOCK,
    }


# ---------------------------------------------------------------------------
# DPD check + history
# ---------------------------------------------------------------------------
class CheckRequest(BaseModel):
    loan_id: str
    emi_amount: Optional[float] = None  # optional override; EMI is normally read from LMS


@app.post("/api/check")
def api_check(req: CheckRequest, user: str = Depends(require_permission(rbac.P_CHECK_RUN))):
    loan_id = (req.loan_id or "").strip()
    if not loan_id:
        raise HTTPException(400, "Loan account ID is required")
    return checker.start_check(loan_id, req.emi_amount)


@app.get("/api/run/{run_id}")
def api_run(run_id: int, user: str = Depends(require_permission(rbac.P_HISTORY_VIEW))):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, f"Run {run_id} not found")
    return run


@app.post("/api/pull/{pull_id}/refresh")
def api_pull_refresh(pull_id: int, user: str = Depends(require_permission(rbac.P_CHECK_RUN))):
    try:
        return checker.process_pull(pull_id)
    except checker.CheckError as e:
        raise HTTPException(404, str(e))


@app.post("/api/account/{account_id}/consent")
def api_account_consent(account_id: int, user: str = Depends(require_permission(rbac.P_CONSENT_FETCH))):
    try:
        return checker.request_consent(account_id)
    except checker.CheckError as e:
        raise HTTPException(404, str(e))


@app.get("/api/pull/{pull_id}/report")
def api_pull_report(pull_id: int, user: str = Depends(require_permission(rbac.P_REPORT_VIEW))):
    import json as _json

    pull = store.get_pull(pull_id)
    if not pull:
        raise HTTPException(404, "Pull not found")
    raw = pull.get("raw_report_json")
    if not raw:
        raise HTTPException(404, "No report retrieved for this pull yet")
    try:
        data = _json.loads(raw) if isinstance(raw, str) else raw
    except Exception:  # noqa: BLE001
        data = {}
    return digitap.parse_report(data)


@app.get("/api/history")
def api_history(user: str = Depends(require_permission(rbac.P_HISTORY_VIEW)), limit: int = 50):
    return store.recent_runs(min(max(limit, 1), 500))


# ---------------------------------------------------------------------------
# Monthly pre-NACH cycle
# ---------------------------------------------------------------------------
class CycleRunRequest(BaseModel):
    confirm: bool = False


class OverrideRequest(BaseModel):
    bucket: str
    reason: str


@app.post("/api/cycle/run")
def api_cycle_run(body: CycleRunRequest, user: str = Depends(require_permission(rbac.P_CYCLE_RUN))):
    """Legacy 'Run cycle' — on the live platform (LOOKUP_SOURCE=lms) this runs the SAME
    population as the Sherlock Check, so it gets the SAME 428 pre-flight hard-block and an
    explicit source; it must never fire ungated real billed pulls. Mock/demo modes keep
    the old ungated behavior. See audit #1."""
    src = None
    if checker.LOOKUP_SOURCE == "lms":
        pf = _preflight()
        if not pf["ready"]:
            failing = [i["label"] for i in pf["items"] if i["required"] and not i["ok"]]
            raise HTTPException(428, "Pre-flight checklist incomplete: " + " · ".join(failing))
        src = "lms"
    try:
        return cycle.start_cycle(user, confirm=body.confirm, source=src)
    except (cycle.CycleBusy, cycle.CycleNeedsConfirm) as e:
        raise HTTPException(409, str(e))


@app.get("/api/cycles")
def api_cycles(user: str = Depends(require_permission(rbac.P_CYCLE_VIEW)), limit: int = 24):
    return store.list_cycles(min(max(limit, 1), 100))


@app.get("/api/cycle/{cycle_id}")
def api_cycle(cycle_id: int, user: str = Depends(require_permission(rbac.P_CYCLE_VIEW))):
    try:
        return cycle.cycle_detail(cycle_id)
    except cycle.CycleError as e:
        raise HTTPException(404, str(e))


@app.get("/api/cycle/{cycle_id}/export")
def api_cycle_export(cycle_id: int, user: str = Depends(require_permission(rbac.P_EXPORT)),
                     bucket: Optional[str] = None):
    from fastapi.responses import Response

    if bucket and bucket not in cycle.BUCKET_DISPLAY:
        raise HTTPException(400, f"Unknown bucket '{bucket}'")
    try:
        body = cycle.export_csv(cycle_id, bucket)
    except cycle.CycleError as e:
        raise HTTPException(404, str(e))
    fname = f"cycle-{cycle_id}-{(bucket or 'all').lower()}.csv"
    return Response(content=body, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/api/cycle/item/{item_id}/retry")
def api_cycle_item_retry(item_id: int, user: str = Depends(require_permission(rbac.P_CYCLE_RUN))):
    try:
        return cycle.retry_item(item_id, user)
    except cycle.CycleError as e:
        raise HTTPException(404, str(e))


@app.post("/api/cycle/item/{item_id}/override")
def api_cycle_item_override(item_id: int, body: OverrideRequest,
                            user: str = Depends(require_permission(rbac.P_OVERRIDE))):
    try:
        return cycle.override_item(item_id, body.bucket, body.reason, user)
    except cycle.CycleError as e:
        code = 404 if "not found" in str(e) else 400
        raise HTTPException(code, str(e))


@app.get("/api/customers")
def api_customers(user: str = Depends(require_permission(rbac.P_MASTER_VIEW))):
    """Directory of every unique loan/customer fetched from the LMS so far."""
    return store.list_customers()


@app.get("/api/customer/{loan_id}")
def api_customer_360(loan_id: str, user: str = Depends(require_permission(rbac.P_REPORT_VIEW))):
    data = store.customer_360(loan_id)
    if not (data.get("loan") or data.get("accounts") or data.get("runs")):
        raise HTTPException(404, f"No data for loan '{loan_id}' yet")
    return data


@app.get("/api/customer/{loan_id}/spend")
def api_customer_spend(loan_id: str, user: str = Depends(require_permission(rbac.P_REPORT_VIEW))):
    try:
        return cycle.spend_analysis(loan_id)
    except cycle.CycleError as e:
        raise HTTPException(404, str(e))


@app.get("/api/customer/{loan_id}/insights")
def api_customer_insights(loan_id: str, user: str = Depends(require_permission(rbac.P_REPORT_VIEW))):
    try:
        return insights.statement_insights(loan_id)
    except cycle.CycleError as e:
        raise HTTPException(404, str(e))


@app.get("/api/customer/{loan_id}/aa")
def api_customer_aa(loan_id: str, user: str = Depends(require_permission(rbac.P_REPORT_VIEW))):
    """Full AA intelligence bundle (mandate, cross-account funding, exact-date
    bounce curve, present plan, fraud queue, identity guard, …) over the latest
    retrieved report — the real Digitap payload parsed once by aa_report."""
    import json as _json
    import aa_report
    db = store._db()
    run_ids = [r["id"] for r in db.checks.find({"loan_id": loan_id}, {"_id": 0, "id": 1})
               .sort("id", -1).limit(15)]
    pull = None
    if run_ids:
        pull = db.pulls.find_one(
            {"run_id": {"$in": run_ids}, "status": "RETRIEVED", "raw_report_json": {"$ne": None}},
            sort=[("id", -1)])
    if not pull:
        raise HTTPException(404, f"No retrieved AA report for '{loan_id}' yet")
    raw = pull.get("raw_report_json")
    data = _json.loads(raw) if isinstance(raw, str) else (raw or {})
    loan = db[store.MASTER].find_one({"type": "loan", "lms_loan_id": loan_id}, {"_id": 0}) or {}
    acct = db[store.MASTER].find_one(
        {"type": "bank_account", "lms_loan_id": loan_id, "is_repayment": True}, {"_id": 0}) or {}
    try:
        return aa_report.analyse(data, hint_last4=acct.get("account_ref"),
                                 hint_ifsc=acct.get("ifsc"), emi=loan.get("emi_amount"))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"AA analysis failed: {type(e).__name__}: {e}")


@app.post("/api/aa/ingest-sample")
def api_aa_ingest_sample(user: str = Depends(require_permission(rbac.P_CYCLE_RUN))):
    """Load the bundled real Digitap sample as a customer so the AA features run
    on real data. Idempotent."""
    import ingest_aa
    try:
        return ingest_aa.ingest_sample()
    except FileNotFoundError:
        raise HTTPException(404, "sample file samples/aa_boda.json not found")


# ---------------------------------------------------------------------------
# Live AA pull — step-by-step, approval-gated (a human clicks Approve & send
# per step). MOCK by default (replays the sample responses); LIVE requires
# AA_LIVE_BASE_URL + credentials set in .env and the UI's top toggle set to Live.
# ---------------------------------------------------------------------------
class AAGenReq(BaseModel):
    mobile_num: str
    onetime_start: str
    onetime_end: str
    periodic_start: str
    periodic_end: str
    periodic_expiry: Optional[str] = None  # blank -> policy default: today + 10 years
    loan_id: Optional[str] = None      # optional — when omitted, the borrower is mapped
                                       # INTERNALLY from the mobile number; either way a
                                       # REQUESTED registry row tracks the journey
    customer_name: Optional[str] = None
    client_ref_num: Optional[str] = None
    cburl: Optional[str] = None
    live: bool = False


def _consent_expiry_default() -> str:
    """Policy: periodic consent expiry = today + 5 years (the enforced maximum)."""
    from datetime import date, timedelta
    t = date.today()
    try:
        return t.replace(year=t.year + 5).isoformat()
    except ValueError:  # Feb 29 -> Mar 1
        return (t + timedelta(days=1826)).isoformat()


class AAStatusReq(BaseModel):
    request_id: str
    txn_id: Optional[str] = None
    live: bool = False


class AAInitiateReq(BaseModel):
    main_txn_id: str
    live: bool = False


class AARetrieveReq(BaseModel):
    txn_id: str
    fetch_type: str = "ONETIME"
    loan_id: Optional[str] = None
    main_txn_id: Optional[str] = None  # parent txn — registered in the consent manager
    request_id: Optional[str] = None   # consent request id — lets the cycle statuscheck-gate the pull
    live: bool = False


class ConsentUpsertReq(BaseModel):
    loan_id: str
    main_txn_id: Optional[str] = None
    consent_id: Optional[str] = None
    consent_type: str = "PERIODIC"   # ONETIME | PERIODIC
    status: Optional[str] = None     # no ACTIVE default — a manual save must be explicit,
                                     # so it can't silently reactivate a revoked consent
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    expiry: Optional[str] = None
    customer_name: Optional[str] = None
    mobile: Optional[str] = None
    reason: Optional[str] = None     # required to change status on an existing consent


class ConsentImportReq(BaseModel):
    """Bulk consent-registry load. dry_run validates + reports what WOULD happen
    (including resulting pull-eligibility) without writing anything."""
    rows: List[dict] = []
    dry_run: bool = True


CM_IMPORT_MAX = 5000

# Accepted column names per field — the sheet a CRO exports rarely uses our exact
# header, so match case/space/underscore-insensitively across common spellings.
CM_IMPORT_ALIASES = {
    "loan_id":       ("loan_id", "loan", "loanid", "lms_loan_id", "loan_no", "loan_number", "account_id"),
    "main_txn_id":   ("main_txn_id", "maintxnid", "txn_id", "txn", "mandate_txn_id", "consent_txn_id"),
    "consent_id":    ("consent_id", "consentid", "handle"),
    "consent_type":  ("consent_type", "type", "consenttype"),
    "status":        ("status", "consent_status"),
    "start_date":    ("start_date", "startdate", "valid_from", "from_date", "from"),
    "end_date":      ("end_date", "enddate", "valid_to", "to_date", "to"),
    "expiry":        ("expiry", "expiry_date", "expirydate", "expires", "expires_on"),
    "customer_name": ("customer_name", "customer", "name", "borrower", "borrower_name"),
    "mobile":        ("mobile", "mobile_no", "phone", "contact_number", "contact"),
    "request_id":    ("request_id", "requestid", "req_id"),
}


def _cm_pick(norm_row: dict, field: str):
    for alias in CM_IMPORT_ALIASES[field]:
        v = norm_row.get(alias)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return None


class LosSyncReq(BaseModel):
    live: Optional[bool] = None  # None -> env default (mock); True only honoured if configured
    stages: Optional[list] = None


class LmsSyncReq(BaseModel):
    live: Optional[bool] = None
    force: bool = False  # override the drop-floor when a shrink is genuinely real


AA_LIVE_MAX_CALLS_PER_DAY = int(os.getenv("AA_LIVE_MAX_CALLS_PER_DAY", "100"))


def _aa_live_billing_guard(live_requested: bool):
    """The 'no surprise Digitap invoice' backstop: atomically reserve a slot under
    today's cap, refusing a real (billed) call once it's hit. Mock calls are free and
    never reserved. Shares the same per-day counter as the cycle path so manual and
    automated billed calls can't jointly overshoot the cap."""
    import aa_live
    if not (live_requested and aa_live.live_configured()):
        return
    if not store.reserve_live_call(AA_LIVE_MAX_CALLS_PER_DAY):
        raise HTTPException(429, f"Daily LIVE Digitap call cap reached ({AA_LIVE_MAX_CALLS_PER_DAY}/"
                                 f"{AA_LIVE_MAX_CALLS_PER_DAY}). Raise AA_LIVE_MAX_CALLS_PER_DAY in "
                                 f".env or wait until tomorrow.")


@app.get("/api/aa-live/config")
def api_aa_live_config(user: str = Depends(require_permission(rbac.P_CONSENT_FETCH))):
    import aa_live
    return {**aa_live.status_summary(), "max_calls_per_day": AA_LIVE_MAX_CALLS_PER_DAY,
            "stats": store.aa_live_stats()}


def _redact_aa_call(c: dict) -> dict:
    """Strip the raw request/response blobs (full AA bank statement, mobile) — keeping
    only call metadata — for callers without master:view. Worklist roles (telecaller/
    field) can see that a pull happened, not everyone's statements. See audit (AA ledger
    bulk PII exposure)."""
    keep = {k: c.get(k) for k in ("id", "at", "by", "kind", "endpoint", "mode", "live",
                                  "ok", "http_status", "error", "request_id", "txn_id",
                                  "loan_id", "los_application_no")}
    keep["redacted"] = True
    return keep


@app.get("/api/aa-live/calls")
def api_aa_live_calls(request: Request,
                      user: str = Depends(require_permission(rbac.P_CONSENT_FETCH)),
                      loan_id: Optional[str] = None, kind: Optional[str] = None,
                      mode: Optional[str] = None, ok: Optional[str] = None,
                      q: Optional[str] = None, limit: int = 60):
    """Every Digitap call this tool has made — stored and shown, so live (billed)
    usage is always auditable. Filterable by loan/customer, call kind, live vs mock,
    result, or free text. Raw request/response payloads (full statements) are shown only
    to master:view roles; worklist roles get redacted metadata rows."""
    okf = {"ok": True, "err": False, "error": False}.get(str(ok or "").lower())
    calls = store.aa_live_calls(max(1, min(int(limit or 60), 500)),
                                loan_id=loan_id, kind=kind, mode=mode, ok=okf, q=q)
    if rbac.P_MASTER_VIEW not in store.role_permissions(effective_role(request)):
        calls = [_redact_aa_call(c) for c in calls]
    return {"stats": store.aa_live_stats(), "cap": AA_LIVE_MAX_CALLS_PER_DAY, "calls": calls}


@app.post("/api/aa-live/generate-url")
def api_aa_live_generate(body: AAGenReq, user: str = Depends(require_permission(rbac.P_CONSENT_FETCH))):
    import aa_live
    _aa_live_billing_guard(body.live)
    # Enforce the consent-window ceilings (one-time <=13mo, periodic <=6mo, expiry <=5y)
    # ONCE, and use the clamped values for BOTH the Digitap request and the registry row
    # we file — so what we ask for and what we record can never diverge.
    ot_start, ot_end, pd_start, pd_end, expiry = aa_live.clamp_consent_window(
        body.onetime_start, body.onetime_end, body.periodic_start, body.periodic_end,
        (body.periodic_expiry or "").strip() or _consent_expiry_default())
    payload = aa_live.build_generate_payload(
        body.mobile_num, ot_start, ot_end, pd_start, pd_end, expiry,
        body.client_ref_num, body.cburl)
    res = aa_live.generate_url(payload, live=body.live)
    # Customer resolution: explicit loan id wins; otherwise map INTERNALLY from the
    # mobile number (borrowers book, then presentment — only on an unambiguous match).
    loan_id = (body.loan_id or "").strip() or None
    mapped = None
    if not loan_id:
        mapped = store.loan_for_mobile(body.mobile_num)
        if mapped:
            loan_id = mapped["loan_id"]
    store.log_aa_call(res, user, loan_id=loan_id)
    req_id = (aa_live.url_of(res) or {}).get("request_id")
    consent_row = None
    if res.get("ok") and loan_id and req_id:
        # File the consent REQUEST itself: one PENDING registry row per journey
        # (REQ-<request_id>), carrying who we asked (mobile), for which loan, and the
        # consent dates the customer is being asked to approve. The retrieve step flips
        # THIS row to ACTIVE — so requested-but-not-approved is always visible, and the
        # expiry the cycle enforces is the one we actually asked Digitap for.
        consent_row = store.upsert_cm_consent(
            loan_id, consent_id=f"REQ-{req_id}", status="PENDING", source="SHERLOCK",
            consent_type="PERIODIC", mobile=body.mobile_num,
            customer_name=(body.customer_name or "").strip() or (mapped or {}).get("customer_name"),
            start_date=pd_start, end_date=pd_end,
            expiry=expiry, request_id=str(req_id), by=user)
    return {**res, "parsed": aa_live.url_of(res), "consent_row": consent_row,
            "mapped_from_mobile": bool(mapped), "expiry_used": expiry}


@app.post("/api/aa-live/status")
def api_aa_live_status(body: AAStatusReq, user: str = Depends(require_permission(rbac.P_CONSENT_FETCH))):
    import aa_live
    # Policy (audit #10): statuscheck is a FREE readiness poll — logged in the ledger but
    # never billed, matching the cycle path. Reserving cap slots here let UI polling starve
    # the afternoon's real initiates/retrieves out of the daily budget.
    res = aa_live.status_check(body.request_id, body.txn_id, live=body.live)
    store.log_aa_call(res, user)
    parsed = aa_live.status_of(res)
    # statuscheck is the first time Digitap reveals the journey's main_txn_id — stamp it
    # onto the REQUESTED registry row so initiate/retrieve resolve the customer.
    if res.get("ok") and parsed.get("main_txn_id"):
        store.cm_stamp_main_txn(body.request_id, parsed["main_txn_id"], by=user)
    return {**res, "parsed": parsed}


@app.post("/api/aa-live/initiate")
def api_aa_live_initiate(body: AAInitiateReq, user: str = Depends(require_permission(rbac.P_CHECK_RUN))):
    import aa_live
    _aa_live_billing_guard(body.live)
    # Honor the 4-initiates/account/month cap for accounts already on the ledger (a
    # brand-new consent has no prior pull -> proceed raw). ATOMIC reserve (audit #17):
    # the old count-then-insert let two concurrent initiates both read 3<4 and overshoot.
    ref = store.account_key_for_txn(body.main_txn_id) or {}
    month = checker._current_month()
    # Fresh consent (no prior pull) has no ledger account_key — derive a SYNTHETIC one from
    # the parent mandate txn so the 4/month guardrail + attempts ledger engage on the very
    # first manual initiate too, not only from month 2. See audit (manual initiate no ledger).
    reserved_key = ref.get("account_key") or ("MT:" + str(body.main_txn_id))
    loan_for_rec = ref.get("loan_id") or store.cm_loan_for_context(txn_ids=[body.main_txn_id])
    if not store.reserve_monthly_attempt(reserved_key, month, checker.MAX_INITIATES_PER_MONTH):
        used = store.attempts_used(reserved_key, month)
        raise HTTPException(429, f"Monthly AA attempt cap reached for this account "
                                 f"({used}/{checker.MAX_INITIATES_PER_MONTH}).")
    res = aa_live.initiate_periodic(body.main_txn_id, live=body.live)
    store.log_aa_call(res, user)
    if res.get("ok") and aa_live.initiate_of(res).get("txn_id"):
        store.record_attempt(month, reserved_key, loan_for_rec, None, None, allowed=True)
    else:
        store.release_monthly_attempt(reserved_key, month)  # never dispatched — give it back
        store.record_attempt(month, reserved_key, loan_for_rec, None, None,
                             allowed=False, reason="INITIATE_FAILED")
    return {**res, "parsed": aa_live.initiate_of(res)}


@app.post("/api/aa-live/retrieve")
def api_aa_live_retrieve(body: AARetrieveReq, user: str = Depends(require_permission(rbac.P_CHECK_RUN))):
    import aa_live
    import ingest_aa
    import aa_report
    _aa_live_billing_guard(body.live)
    res = aa_live.retrieve_report(body.txn_id, body.fetch_type, live=body.live)
    # Customer resolution: explicit loan id -> the journey's registry row (request_id /
    # mandate txn, the internal mapping) -> only then the ad-hoc AA-<txn> fallback.
    loan_id = (body.loan_id or "").strip() \
        or store.cm_loan_for_context(request_id=(body.request_id or None),
                                     txn_ids=[body.main_txn_id, body.txn_id]) \
        or ("AA-" + str(body.txn_id))
    store.log_aa_call(res, user, loan_id=loan_id)
    report = (res.get("response") or {})
    if not res.get("ok") or not report.get("banks"):
        return {**res, "stored": None, "analysis": None,
                "detail": res.get("error") or "no report returned"}
    stored = ingest_aa.ingest_report(loan_id, report, triggered_by="aa-live",
                                     fetch_type=(body.fetch_type or "").upper() or None)
    # A successful retrieve proves a working consent+txn for this loan — register
    # it in the consent manager so the CRO's monthly Sherlock Check can pull it.
    # When the journey started with generate-url (request_id known), flip THAT
    # journey's REQUESTED row to ACTIVE — the dates/mobile filed at request time
    # survive (non-authoritative upsert omits None fields).
    main_txn = (body.main_txn_id or "").strip() or \
        (str(body.txn_id) if body.fetch_type == "ONETIME" else None)
    journey_cid = f"REQ-{body.request_id}" if (body.request_id or "").strip() else None
    # A journey row (REQ-<id>) keeps the consent_type filed at request time (PERIODIC —
    # the combined consent the customer approved): a ONETIME verification retrieve must
    # not flip it and silently exclude the borrower from every monthly pull. Only the
    # synthetic no-request-id handle takes its type from the fetch. See audit #9.
    store.upsert_cm_consent(loan_id, main_txn_id=main_txn, status="ACTIVE",
                            consent_id=journey_cid,
                            source="SHERLOCK", customer_name=stored.get("customer_name"),
                            by=user,
                            consent_type=None if journey_cid else (body.fetch_type or "PERIODIC").upper(),
                            request_id=(body.request_id or None))
    # Back-link the journey's earlier calls (generate/status/initiate ran before the
    # loan was known) so the WHOLE trail is queryable by customer.
    store.link_aa_calls_to_loan(loan_id, request_id=(body.request_id or None),
                                txn_ids=[body.txn_id, main_txn])
    try:
        analysis = aa_report.analyse(report)
    except Exception as e:  # noqa: BLE001
        analysis = None
        res["error"] = f"analyse failed: {type(e).__name__}: {e}"
    return {**res, "stored": stored, "loan_id": loan_id, "analysis": analysis}


# ---------------------------------------------------------------------------
# LOS (Engrow) Portfolio Sync — approval-gated Flow A pull. Mock default; every
# call stored + shown; a single bounded fan-out (no loops). Replaces the mock
# portfolio end-to-end when LOOKUP_SOURCE=los.
# ---------------------------------------------------------------------------
@app.get("/api/los/config")
def api_los_config(user: str = Depends(require_permission(rbac.P_MASTER_VIEW))):
    import los_client
    return {**los_client.status_summary(), "stats": store.los_stats(),
            "lookup_source": checker.LOOKUP_SOURCE}


@app.get("/api/los/calls")
def api_los_calls(user: str = Depends(require_permission(rbac.P_MASTER_VIEW))):
    """Every LOS call this tool has made — stored and shown (LOS is not billed,
    but the same visibility discipline as the Digitap ledger)."""
    return {"stats": store.los_stats(), "calls": store.los_calls(60)}


@app.post("/api/los/sync")
def api_los_sync(body: LosSyncReq, user: str = Depends(require_permission(rbac.P_CYCLE_RUN))):
    import los_client
    import ingest_los
    res = los_client.sync_portfolio(live=body.live, stages=body.stages)
    # Persist EVERY call (login/list are portfolio-wide -> no loan; detail/bank/
    # consent carry the application, resolved from the request's applicationUid).
    uid_to_app = {r.get("uid"): r.get("los_application_no") for r in res.get("rows", [])}
    for c in res.get("calls", []):
        uid = (c.get("request") or {}).get("applicationUid")
        app_no = uid_to_app.get(uid)
        store.log_los_call(c, by=user, loan_id=app_no, los_application_no=app_no)
    ingested = ingest_los.ingest_portfolio(res.get("rows", []))
    mode = "mock" if los_client._use_mock(body.live) else "live"
    return {"mode": mode, "counts": res.get("counts"), "ingested": ingested,
            "error": res.get("error"),
            "rows_preview": res.get("rows", [])[:50]}


# ---------------------------------------------------------------------------
# LMS (Encore) Presentment Sync — approval-gated Flow B pull (loans due for
# collection: contact number, demand amount, due date). Mock default; every call
# stored + shown; one login + one report fetch (no loops).
# ---------------------------------------------------------------------------
@app.get("/api/lms/config")
def api_lms_config(user: str = Depends(require_permission(rbac.P_MASTER_VIEW))):
    import lms_client
    return {**lms_client.status_summary(), "stats": store.lms_stats()}


@app.get("/api/lms/calls")
def api_lms_calls(user: str = Depends(require_permission(rbac.P_MASTER_VIEW))):
    return {"stats": store.lms_stats(), "calls": store.lms_calls(60)}


@app.post("/api/lms/sync")
def api_lms_sync(body: LmsSyncReq, user: str = Depends(require_permission(rbac.P_CYCLE_RUN))):
    import lms_client
    import ingest_lms
    res = lms_client.sync_presentment(live=body.live)
    for c in res.get("calls", []):
        store.log_lms_call(c, by=user)
    mode = "mock" if lms_client._use_mock(body.live) else "live"
    # NEVER ingest (which replaces the snapshot) on a failed fetch — a login/report error
    # returns rows=[] and would otherwise wipe the whole 1817-row book. Preserve it.
    if res.get("error"):
        ingested = {"rows": 0, "skipped": f"sync error — snapshot preserved: {res.get('error')}"}
    else:
        try:
            ingested = ingest_lms.ingest_presentment(res.get("rows", []), mode=mode,
                                                     force=body.force)
        except store.PresentmentDropFloor as e:
            # a much-smaller book than last time — refuse unless the CRO confirms (force)
            raise HTTPException(409, str(e))
        except store.CycleBusyLike as e:
            raise HTTPException(409, str(e))
    return {"mode": mode, "counts": res.get("counts"), "ingested": ingested,
            "error": res.get("error"), "rows_preview": res.get("rows", [])[:50]}


# ---------------------------------------------------------------------------
# CRO monthly flow — review the presentment vs the consent manager, then run
# the Sherlock Check (an lms-source cycle: consented borrowers get a REAL
# periodic pull; balance-as-of-today vs EMI drives the classification).
# ---------------------------------------------------------------------------
@app.get("/api/lms/presentment")
def api_lms_presentment(user: str = Depends(require_permission(rbac.P_CYCLE_VIEW)),
                        q: Optional[str] = None, branch: Optional[str] = None,
                        status: Optional[str] = None, od: Optional[str] = None,
                        page: int = 1, size: int = 100):
    """Browse the stored presentment book — full data with search/filter/paging
    (the sync response itself only carries a 50-row preview)."""
    size = max(1, min(size, 500))
    res = store.lms_presentment_query(q=q, branch=branch, status=status, od=od,
                                      skip=(max(1, page) - 1) * size, limit=size)
    return {**res, "page": max(1, page), "size": size}


# ---------------------------------------------------------------------------
# Pre-flight checklist — mandatory pre-requisites of the monthly Sherlock Check.
# The run is HARD-BLOCKED until: (1) this month's presentment is synced, and
# (2) the LOS consent sync ran AFTER that presentment (fresh disbursals' consents
# copied into the registry, our single source of truth).
# ---------------------------------------------------------------------------
PREFLIGHT_JOB = "preflight_consent_sync"


def _next_run_cutoff() -> str:
    """The next monthly Sherlock run happens on the 1st/2nd — consents lapsing
    before the 3rd of next month need renewal NOW."""
    from datetime import date as _date
    t = _date.today()
    y, m = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
    return f"{y:04d}-{m:02d}-03"


def _preflight() -> dict:
    # Derive the month from the SAME UTC clock that stamps last_synced_at (store._now),
    # otherwise a local date vs UTC timestamp mismatch shows a false 428 in the early-hours
    # run window (e.g. 03:00 IST on the 1st). See bug (_preflight tz).
    month = store._now()[:7]
    meta = store.lms_presentment_meta()
    # LIVE-ONLY: refuse to run on a MOCK-toggled snapshot (5 sample rows). A None mode is a
    # legacy pre-provenance sync and is allowed; only an explicit 'mock' snapshot fails the gate.
    pres_mode = str(meta.get("mode") or "").lower()
    pres_mixed = bool(meta.get("mixed_batch"))  # a partial/failed replace left old+new rows
    pres_ok = bool(meta["rows"]) and str(meta["last_synced_at"] or "").startswith(month) \
        and pres_mode != "mock" and not pres_mixed
    job = store.get_job(PREFLIGHT_JOB) or {}
    sync_at = job.get("last_run_at")
    sync_ran = bool(pres_ok and sync_at and str(sync_at) >= str(meta["last_synced_at"]))
    sync_mode = str(job.get("last_mode") or "").lower()
    sync_skipped = sync_ran and sync_mode and sync_mode != "live"
    rows = store.lms_presentment_all()
    cms = store.cm_map([r.get("account_id") for r in rows])
    eligible = sum(1 for r in rows if checker._cm_active(cms.get(r.get("account_id"))))
    import aa_live
    cap_used = store.aa_live_stats().get("live_today", 0)
    # Each eligible borrower bills >=2 live calls (initiate + retrieve), so budget 2x. See bug K2.
    budget = cap_used + 2 * eligible
    digitap_ok = (not aa_live._use_mock(None)) and aa_live.live_configured() \
        and budget <= AA_LIVE_MAX_CALLS_PER_DAY or aa_live._use_mock(None)
    if sync_skipped:
        sync_detail = (f"SKIPPED ({sync_mode}) — LOS DB not configured, 0 fresh-disbursal consents "
                       f"copied; running on Sherlock-registry consents only")
    elif sync_ran:
        sync_detail = job.get("last_detail") or "ran"
    else:
        sync_detail = "not run yet"
    items = [
        {"key": "presentment", "required": True, "ok": pres_ok,
         "label": f"Presentment synced for {month}",
         "detail": f"{meta['rows']} loans · last sync {meta['last_synced_at'] or 'never'}"
                   + (f" · ⚠ {pres_mode.upper()} snapshot — re-sync LIVE" if pres_mode == "mock" else "")
                   + (" · ⚠ MIXED-BATCH snapshot (partial sync) — re-sync" if pres_mixed else "")},
        {"key": "consent_sync", "required": True, "ok": sync_ran, "warn": bool(sync_skipped),
         "label": "LOS consent sync ran after the presentment",
         "detail": sync_detail},
        {"key": "eligibility", "required": False, "ok": eligible > 0,
         "label": "Borrowers eligible for periodic pull",
         "detail": f"{eligible} eligible of {len(rows)} due"},
        {"key": "digitap", "required": eligible > 0, "ok": bool(digitap_ok),
         "label": "Digitap ready (config + daily cap headroom)",
         "detail": f"{cap_used}/{AA_LIVE_MAX_CALLS_PER_DAY} used today · needs ~{2 * eligible} "
                   f"for {eligible} eligible · {'mock mode' if aa_live._use_mock(None) else 'live'}"},
    ]
    expiring = store.cm_expiring_before(_next_run_cutoff())
    items.append({"key": "expiring", "required": False, "ok": len(expiring) == 0,
                  "label": "Consents valid through the next run",
                  "detail": (f"{len(expiring)} consent(s) lapse before {_next_run_cutoff()} — "
                             f"chase renewals now: "
                             + ", ".join(x["loan_id"] for x in expiring[:5])
                             + ("…" if len(expiring) > 5 else "")) if expiring
                            else f"none lapse before {_next_run_cutoff()}"})
    return {"month": month, "ready": all(i["ok"] for i in items if i["required"]),
            "items": items, "eligible": eligible}


@app.get("/api/lms/preflight")
def api_lms_preflight(user: str = Depends(require_permission(rbac.P_CYCLE_VIEW))):
    return _preflight()


@app.post("/api/lms/consent-sync")
def api_lms_consent_sync(body: LmsSyncReq, user: str = Depends(require_permission(rbac.P_CYCLE_RUN))):
    """Pre-flight step 2: copy LOS-captured consents (fresh disbursals) into the
    registry for every borrower in this month's presentment."""
    import los_consent
    ids = [r.get("account_id") for r in store.lms_presentment_all()]
    if not ids:
        raise HTTPException(400, "No presentment data — run the LMS sync first.")
    res = los_consent.sync_consents(ids, live=body.live, by=user)
    store.log_los_call({"kind": "los_consent_sql", "endpoint": "los_consent.sql",
                        "method": "SQL", "mode": res.get("mode"), "ok": res.get("ok"),
                        "request": {"loans": res.get("loans")},
                        "response": {k: v for k, v in res.items() if k != "ok"},
                        "http_status": None, "error": res.get("error")}, by=user)
    if res.get("ok"):
        store.upsert_job(PREFLIGHT_JOB, {"schedule": "manual (pre-flight)"})
        store.update_job(PREFLIGHT_JOB, last_run_at=store._now(), last_status="OK",
                         last_mode=res.get("mode"), last_consents=res.get("consents"),
                         last_detail=f"{res['consents']} consents over {res['loans']} loans "
                                     f"({res.get('loans_with_consent', 0)} had LOS consents) · {res['mode']}")
    return {**res, "preflight": _preflight()}


@app.get("/api/lms/review")
def api_lms_review(user: str = Depends(require_permission(rbac.P_CYCLE_VIEW))):
    rows = store.lms_presentment_all()
    cms = store.cm_map([r.get("account_id") for r in rows])
    out = []
    counts = {"total": 0, "eligible": 0, "not_pullable": 0, "expired": 0, "no_consent": 0}
    _RANK = {"ELIGIBLE": 0, "NOT_PULLABLE": 1, "EXPIRED": 2, "NO_CONSENT": 3}
    def _num(v):
        try: return float(v)
        except (TypeError, ValueError): return 0.0
    blind_exposure = 0.0
    for r in rows:
        acct = r.get("account_id")
        cm = cms.get(acct)
        state, reason = checker.cm_state(cm)
        counts["total"] += 1
        counts[state.lower()] = counts.get(state.lower(), 0) + 1
        # Acquisition priority = how overdue × how big — spend consent effort on the
        # riskiest blind borrowers first (od_days and demand are both in the presentment).
        exposure = _num(r.get("demand_amount")) or _num(r.get("emi_amount"))
        priority = round(_num(r.get("od_days_num")) * exposure)
        if state != "ELIGIBLE":
            blind_exposure += exposure
        out.append({"account_id": acct, "customer_name": r.get("customer_name"),
                    "branch_name": r.get("branch_name"),
                    "contact_number": r.get("contact_number"),
                    "emi_amount": r.get("emi_amount"), "demand_amount": r.get("demand_amount"),
                    "demand_date": r.get("demand_date"), "od_days": r.get("od_days"),
                    "od_days_num": r.get("od_days_num"), "exposure": exposure, "priority": priority,
                    "state": state, "reason": reason,
                    "consent_type": (cm or {}).get("consent_type"),
                    "main_txn_id": (cm or {}).get("main_txn_id"),
                    "consent_expiry": (cm or {}).get("expiry") or (cm or {}).get("end_date")})
    out.sort(key=lambda x: (_RANK.get(x["state"], 9), str(x["account_id"])))
    # Consent-acquisition worklist: the blind majority ranked by risk×size, so coverage
    # can be GROWN (not just decay). Route NO_CONSENT/EXPIRED/NOT_PULLABLE here.
    acquisition = sorted((x for x in out if x["state"] != "ELIGIBLE"),
                         key=lambda x: -x["priority"])[:100]
    total = counts["total"] or 0
    blind_n = counts["not_pullable"] + counts["expired"] + counts["no_consent"]
    expiring = store.cm_expiring_before(_next_run_cutoff())
    _CAP = 2000  # above a normal month's book; flag when exceeded so the UI is honest
    return {"counts": counts, "rows": out[:_CAP], "rows_capped": len(out) > _CAP,
            "cm_stats": store.cm_stats(),
            "coverage": {"eligible": counts["eligible"], "total": total,
                         "eligible_pct": round(100.0 * counts["eligible"] / total, 1) if total else None,
                         "blind": blind_n,
                         "blind_pct": round(100.0 * blind_n / total, 1) if total else None,
                         "blind_exposure": round(blind_exposure, 2),
                         "floor_pct": cycle.COVERAGE_FLOOR_PCT},
            "acquisition": acquisition,
            "expiring": {"cutoff": _next_run_cutoff(), "count": len(expiring),
                         "rows": expiring[:50]},
            "aa_billing": {"live_today": store.aa_live_stats().get("live_today", 0),
                           "cap": AA_LIVE_MAX_CALLS_PER_DAY}}


@app.post("/api/lms/run-check")
def api_lms_run_check(body: CycleRunRequest, user: str = Depends(require_permission(rbac.P_CYCLE_RUN))):
    """The CRO's manual 'Run Sherlock Check' — an lms-source cycle over this
    month's presentment. Only consent-registry-eligible borrowers get pulls.
    HARD-BLOCKED until the pre-flight checklist passes (presentment fresh +
    LOS consent sync ran after it)."""
    pf = _preflight()
    if not pf["ready"]:
        failing = [i["label"] for i in pf["items"] if i["required"] and not i["ok"]]
        raise HTTPException(428, "Pre-flight checklist incomplete: " + " · ".join(failing))
    try:
        return cycle.start_cycle(user, confirm=bool(body.confirm), source="lms")
    except cycle.CycleNeedsConfirm as e:
        raise HTTPException(409, str(e))
    except cycle.CycleBusy as e:
        raise HTTPException(423, str(e))


# ---------------------------------------------------------------------------
# Consent manager — the per-loan AA consent registry (view + manual upsert;
# the Live Pull flow registers rows automatically on a successful retrieve)
# ---------------------------------------------------------------------------
@app.get("/api/consents-manager")
def api_cm_list(user: str = Depends(require_permission(rbac.P_MASTER_VIEW)),
                status: Optional[str] = None, source: Optional[str] = None,
                q: Optional[str] = None):
    """The registry, filterable: status (PENDING/ACTIVE/EXPIRED/REVOKED), source
    (LOS/SHERLOCK), free text (loan / name / mobile / txn / consent id)."""
    rows = store.cm_all()
    if status:
        rows = [r for r in rows if str(r.get("status") or "").upper() == status.upper()]
    if source:
        rows = [r for r in rows if str(r.get("source") or "").upper() == source.upper()]
    if q:
        needle = q.strip().lower()
        rows = [r for r in rows if needle in " ".join(
            str(r.get(f) or "") for f in ("loan_id", "customer_name", "mobile",
                                          "main_txn_id", "consent_id", "request_id",
                                          "los_application_no")).lower()]
    return {"stats": store.cm_stats(), "rows": rows}


@app.post("/api/consents-manager")
def api_cm_upsert(body: ConsentUpsertReq, user: str = Depends(require_permission(rbac.P_CONSENT_FETCH))):
    if not body.loan_id.strip():
        raise HTTPException(400, "loan_id is required")
    new_status = (body.status or "").upper() or None
    # Find the existing row (if the caller named a specific consent) to guard status changes.
    existing = None
    if (body.consent_id or "").strip():
        existing = next((r for r in store.cm_rows_for(body.loan_id)
                         if r.get("consent_id") == body.consent_id.strip()), None)
    if existing:
        cur = str(existing.get("status") or "").upper()
        # A REVOKED/EXPIRED consent must NOT be manually flipped back to ACTIVE — a
        # resurrection has to come from a fresh consent journey (new request_id), not an
        # in-place edit against a withdrawn mandate. See audit (consent resurrection).
        if new_status == "ACTIVE" and cur in ("REVOKED", "EXPIRED"):
            raise HTTPException(409, f"Consent {body.consent_id} is {cur} — reactivating a "
                                     "withdrawn/expired mandate in place is not allowed. "
                                     "Run a fresh consent journey (Live Pull) instead.")
        # Any manual status change on an existing consent needs a reason (goes to history).
        if new_status and new_status != cur and not (body.reason or "").strip():
            raise HTTPException(400, "A reason is required to change an existing consent's status.")
    row = store.upsert_cm_consent(body.loan_id, main_txn_id=body.main_txn_id,
                                  consent_id=body.consent_id, status=new_status,
                                  expiry=body.expiry, source="SHERLOCK",
                                  customer_name=body.customer_name, mobile=body.mobile, by=user,
                                  consent_type=body.consent_type,
                                  start_date=body.start_date, end_date=body.end_date,
                                  reason=(body.reason or "manual entry"))
    return {"ok": True, "row": row}


@app.get("/api/aa-live/consent-context")
def api_aa_consent_context(main_txn_id: str,
                           user: str = Depends(require_permission(rbac.P_CONSENT_FETCH))):
    """Given only a parent (mandate) txn id, return the consent's journey context —
    chiefly the request_id the FREE readiness poll needs, plus the loan it belongs to.
    Lets a periodic pull run initiate -> statuscheck -> retrieve from that id alone."""
    row = store.cm_context_for_txn(main_txn_id)
    return {"found": bool(row), **(row or {})}


@app.post("/api/consents-manager/import")
def api_cm_import(body: ConsentImportReq,
                  user: str = Depends(require_permission(rbac.P_CONSENT_FETCH))):
    """Bulk-load the consent registry from a spreadsheet/CSV.

    Every row is upserted through the SAME path as a manual save (upsert_cm_consent),
    so the audit trail, the (loan, consent) key and the resurrection guard all still
    apply. With dry_run the rows are only validated and each is reported with the
    pull-eligibility it WOULD end up with — so a CRO can fix the sheet before writing.
    """
    rows = body.rows or []
    if not rows:
        raise HTTPException(400, "No rows supplied")
    if len(rows) > CM_IMPORT_MAX:
        raise HTTPException(400, f"Too many rows ({len(rows)}) — max {CM_IMPORT_MAX} per import")

    results, created, updated, skipped = [], 0, 0, 0
    for i, raw in enumerate(rows, start=1):
        if not isinstance(raw, dict):
            results.append({"line": i, "action": "error", "error": "row is not an object"})
            skipped += 1
            continue
        norm = {str(k).strip().lower().replace(" ", "_"): v for k, v in raw.items()}
        rec = {f: _cm_pick(norm, f) for f in CM_IMPORT_ALIASES}
        loan_id = rec.get("loan_id")
        if not loan_id:
            results.append({"line": i, "action": "error", "error": "loan_id is required"})
            skipped += 1
            continue
        status = (rec.get("status") or "").upper() or None
        ctype = (rec.get("consent_type") or "").upper() or None
        if ctype and ctype not in ("ONETIME", "PERIODIC"):
            results.append({"line": i, "loan_id": loan_id, "action": "error",
                            "error": f"consent_type '{ctype}' must be PERIODIC or ONETIME"})
            skipped += 1
            continue
        handle = rec.get("consent_id") or f"CM-{loan_id}-{ctype or 'PERIODIC'}"
        existing = next((r for r in store.cm_rows_for(loan_id)
                         if r.get("consent_id") == handle), None)
        # Same guard as the single-row save: a REVOKED/EXPIRED mandate must not be
        # resurrected by re-uploading a sheet — that needs a fresh consent journey.
        if existing and status == "ACTIVE" and \
                str(existing.get("status") or "").upper() in ("REVOKED", "EXPIRED"):
            results.append({"line": i, "loan_id": loan_id, "action": "skipped",
                            "error": f"consent is {existing.get('status')} — reactivating in place "
                                     "is not allowed; run a fresh consent journey (Live Pull)"})
            skipped += 1
            continue

        action = "update" if existing else "create"
        if body.dry_run:
            preview = dict(existing or {})
            preview.update({k: v for k, v in {
                "loan_id": loan_id, "main_txn_id": rec.get("main_txn_id"), "status": status,
                "consent_type": ctype or preview.get("consent_type") or "PERIODIC",
                "start_date": store._iso_date(rec.get("start_date")),
                "end_date": store._iso_date(rec.get("end_date")),
                "expiry": store._iso_date(rec.get("expiry")),
            }.items() if v is not None})
            state, reason = checker.cm_state(preview)
        else:
            row = store.upsert_cm_consent(
                loan_id, main_txn_id=rec.get("main_txn_id"), consent_id=rec.get("consent_id"),
                status=status, expiry=rec.get("expiry"), source="SHERLOCK",
                customer_name=rec.get("customer_name"), mobile=rec.get("mobile"), by=user,
                consent_type=ctype, start_date=rec.get("start_date"),
                end_date=rec.get("end_date"), request_id=rec.get("request_id"),
                reason="bulk import")
            state, reason = checker.cm_state(row or {})
        if action == "create":
            created += 1
        else:
            updated += 1
        results.append({"line": i, "loan_id": loan_id, "action": action, "state": state,
                        "reason": reason, "consent_id": handle,
                        "main_txn_id": rec.get("main_txn_id"),
                        "customer_name": rec.get("customer_name")})

    return {"dry_run": body.dry_run, "total": len(rows), "created": created,
            "updated": updated, "skipped": skipped,
            "eligible": sum(1 for r in results if r.get("state") == "ELIGIBLE"),
            "rows": results}


@app.get("/api/consents-manager/{loan_id}/history")
def api_cm_history(loan_id: str, user: str = Depends(require_permission(rbac.P_MASTER_VIEW))):
    return {"loan_id": loan_id, "events": store.consent_events_for(loan_id)}


# ---------------------------------------------------------------------------
# Borrowers book — every disbursed customer ever (allData report once its name
# is provided; refreshed from presentment snapshots meanwhile)
# ---------------------------------------------------------------------------
@app.get("/api/borrowers")
def api_borrowers(user: str = Depends(require_permission(rbac.P_MASTER_VIEW))):
    rows = store.borrowers_list()
    return {"count": len(rows), "rows": rows}


@app.post("/api/borrowers/refresh")
def api_borrowers_refresh(user: str = Depends(require_permission(rbac.P_CYCLE_RUN))):
    """Upsert the borrower book. TEMP source: the latest presentment snapshot —
    swaps to the LMS allData report (all disbursed loans) once its reportName is
    provided. Upsert-only: borrowers never disappear from the book."""
    n = store.bulk_upsert_borrowers(store.lms_presentment_all(), source="lms-presentment")
    return {"ok": True, "upserted": n,
            "note": "source=presentment (allData report pending — provide its reportName to switch)"}


@app.post("/api/aa/analyse")
async def api_aa_analyse_upload(request: Request,
                               user: str = Depends(require_permission(rbac.P_REPORT_VIEW))):
    """Parse an uploaded Digitap AA report JSON and return the full intelligence
    bundle — stateless, nothing is written to the DB. The file body is POSTed as
    raw JSON (no multipart dependency)."""
    import json as _json
    import aa_report
    body = await request.body()
    if not body:
        raise HTTPException(400, "Empty upload — choose an AA report JSON file")
    try:
        data = _json.loads(body)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Not valid JSON: {e}")
    if not isinstance(data, dict) or "banks" not in data:
        raise HTTPException(422, "This does not look like a Digitap AA report (no 'banks' array).")
    try:
        result = aa_report.analyse(data)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"Could not analyse: {type(e).__name__}: {e}")
    return result


@app.post("/api/customer/{loan_id}/consent")
def api_customer_consent(loan_id: str, user: str = Depends(require_permission(rbac.P_CONSENT_FETCH))):
    try:
        return checker.request_consent_for_loan(loan_id)
    except checker.CheckError as e:
        code = 404 if "No check" in str(e) else 400
        raise HTTPException(code, str(e))


# ---------------------------------------------------------------------------
# Notes & flags
# ---------------------------------------------------------------------------
class NoteRequest(BaseModel):
    text: str


class FlagRequest(BaseModel):
    flag: str
    active: bool = True


@app.post("/api/customer/{loan_id}/notes")
def api_add_note(loan_id: str, body: NoteRequest,
                 user: str = Depends(require_permission(rbac.P_DISPOSE))):
    if not (body.text or "").strip():
        raise HTTPException(400, "Note text is required")
    return store.add_note(loan_id, body.text, user)


@app.post("/api/customer/{loan_id}/flags")
def api_set_flag(loan_id: str, body: FlagRequest,
                 user: str = Depends(require_permission(rbac.P_DISPOSE))):
    if body.flag not in store.CUSTOMER_FLAGS:
        raise HTTPException(400, f"Unknown flag '{body.flag}' — use one of {store.CUSTOMER_FLAGS}")
    return {"loan_id": loan_id, "flags": store.set_flag(loan_id, body.flag, body.active, user)}


# ---------------------------------------------------------------------------
# Nudges + re-presentation
# ---------------------------------------------------------------------------
class NudgeRequest(BaseModel):
    channel: str = "WHATSAPP"
    message: str = ""


@app.post("/api/cycle/item/{item_id}/nudge")
def api_send_nudge(item_id: int, body: NudgeRequest,
                   user: str = Depends(require_permission(rbac.P_DISPOSE))):
    try:
        return cycle.send_nudge(item_id, body.channel, body.message, user)
    except cycle.CycleError as e:
        code = 404 if "not found" in str(e) else 400
        raise HTTPException(code, str(e))


@app.post("/api/cycle/{cycle_id}/represent")
def api_represent(cycle_id: int, user: str = Depends(require_permission(rbac.P_CYCLE_RUN))):
    try:
        return cycle.simulate_representation(cycle_id)
    except cycle.CycleError as e:
        raise HTTPException(404, str(e))


@app.post("/api/cycle/confirmation-sweep")
def api_confirmation_sweep(window_days: int = 1,
                           user: str = Depends(require_permission(rbac.P_CYCLE_RUN))):
    """CRO-manual pre-presentation confirmation: re-pull the at-risk/promised/timing-risk
    accounts whose OWN due date is imminent, so the floor acts on fresh balances before the
    debit (the 'before the 4th' lever). Scheduler runs it daily when enabled."""
    return {"detail": cycle.run_confirmation_sweep(window_days=window_days)}


# ---------------------------------------------------------------------------
# Worklist dispositions
# ---------------------------------------------------------------------------
class DispositionRequest(BaseModel):
    status: str
    remarks: str = ""
    ptp_date: Optional[str] = None


@app.post("/api/cycle/item/{item_id}/disposition")
def api_item_disposition(item_id: int, body: DispositionRequest,
                         user: str = Depends(require_permission(rbac.P_DISPOSE))):
    try:
        return cycle.dispose_item(item_id, body.status, body.remarks, body.ptp_date, user)
    except cycle.CycleError as e:
        code = 404 if "not found" in str(e) else 400
        raise HTTPException(code, str(e))


@app.get("/api/cycle/item/{item_id}/dispositions")
def api_item_dispositions(item_id: int, user: str = Depends(require_permission(rbac.P_CYCLE_VIEW))):
    return store.dispositions_for_item(item_id)


class RetimeRequest(BaseModel):
    date: Optional[str] = None


@app.post("/api/cycle/item/{item_id}/retime")
def api_item_retime(item_id: int, body: RetimeRequest,
                    user: str = Depends(require_permission(rbac.P_DISPOSE))):
    """Record the AA-recommended 're-time the NACH to the income date' action (F4)."""
    try:
        return cycle.recommend_retime(item_id, body.date, user)
    except cycle.CycleError as e:
        raise HTTPException(404, str(e))


# ---------------------------------------------------------------------------
# NACH outcomes + dashboard
# ---------------------------------------------------------------------------
@app.post("/api/cycle/{cycle_id}/outcomes/simulate")
def api_simulate_outcomes(cycle_id: int, user: str = Depends(require_permission(rbac.P_CYCLE_RUN))):
    try:
        return cycle.simulate_outcomes(cycle_id)
    except cycle.CycleError as e:
        code = 404 if "not found" in str(e) else 400
        raise HTTPException(code, str(e))


class ActualOutcomeRequest(BaseModel):
    loan_id: str
    outcome: str            # CLEARED | BOUNCED
    reason: Optional[str] = None


@app.post("/api/cycle/{cycle_id}/outcomes/record")
def api_record_outcome(cycle_id: int, body: ActualOutcomeRequest,
                       user: str = Depends(require_permission(rbac.P_CYCLE_RUN))):
    """Hand-enter a REAL NACH return (source=NACH_ACTUAL) — the ground truth the calibration
    loop needs when there's no automated return feed. Replaces any mock row for this loan."""
    try:
        return cycle.record_actual_outcome(cycle_id, body.loan_id, body.outcome, body.reason, user)
    except cycle.CycleError as e:
        code = 404 if "not found" in str(e) or "not in cycle" in str(e) else 400
        raise HTTPException(code, str(e))


@app.get("/api/cycle/{cycle_id}/outcomes")
def api_cycle_outcomes(cycle_id: int, user: str = Depends(require_permission(rbac.P_CYCLE_VIEW))):
    return store.outcomes_for_cycle(cycle_id)


@app.get("/api/dashboard")
def api_dashboard(user: str = Depends(require_permission(rbac.P_CYCLE_VIEW))):
    return cycle.build_dashboard()


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------
class JobToggle(BaseModel):
    enabled: bool


@app.get("/api/jobs")
def api_jobs(user: str = Depends(require_permission(rbac.P_CYCLE_VIEW))):
    return scheduler.jobs_status()


@app.post("/api/jobs/{name}/run")
def api_job_run(name: str, user: str = Depends(require_permission(rbac.P_JOBS_MANAGE))):
    try:
        return scheduler.run_job(name, by=user)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/jobs/{name}/toggle")
def api_job_toggle(name: str, body: JobToggle,
                   user: str = Depends(require_permission(rbac.P_JOBS_MANAGE))):
    try:
        scheduler.set_enabled(name, body.enabled, by=user)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "name": name, "enabled": body.enabled}


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------
@app.get("/api/master/loans")
def api_master_loans(user: str = Depends(require_permission(rbac.P_MASTER_VIEW))):
    return store.list_loans()


@app.get("/api/master/accounts")
def api_master_accounts(user: str = Depends(require_permission(rbac.P_MASTER_VIEW))):
    return store.list_bank_accounts()


@app.get("/api/master/consents")
def api_master_consents(user: str = Depends(require_permission(rbac.P_MASTER_VIEW))):
    return store.list_consents()


# ---------------------------------------------------------------------------
# Data browser — view MongoDB collections in the app
# ---------------------------------------------------------------------------
@app.get("/api/data/collections")
def api_data_collections(user: str = Depends(require_permission(rbac.P_DATA_VIEW))):
    return store.data_collections()


@app.get("/api/data/{name}")
def api_data_documents(name: str, user: str = Depends(require_permission(rbac.P_DATA_VIEW)),
                       limit: int = 50, skip: int = 0, q: Optional[str] = None):
    try:
        return store.data_documents(name, limit, skip, q)
    except ValueError as e:
        raise HTTPException(404, str(e))


# ---------------------------------------------------------------------------
# Roles + user management
# ---------------------------------------------------------------------------
class NewUser(BaseModel):
    username: str
    password: str
    role: str = "viewer"
    branch: Optional[str] = None
    must_change: bool = True   # branch staff set their own password on first sign-in


class PasswordChange(BaseModel):
    password: str


class SelfPasswordChange(BaseModel):
    current_password: str
    new_password: str


class RoleChange(BaseModel):
    role: str


class BranchChange(BaseModel):
    branch: Optional[str] = None


class StatusChange(BaseModel):
    status: str  # active | suspended


@app.get("/api/roles")
def api_roles(admin: str = Depends(require_permission(rbac.P_USER_MANAGE))):
    # The full role->permission matrix is an admin concern; login and /api/me already
    # give each user their OWN permissions. See audit (/api/roles matrix leak).
    return store.list_roles()


@app.get("/api/users")
def api_list_users(admin: str = Depends(require_permission(rbac.P_USER_MANAGE))):
    return store.list_users()


@app.post("/api/users")
def api_add_user(body: NewUser, request: Request, admin: str = Depends(require_permission(rbac.P_USER_MANAGE))):
    try:
        store.add_user(body.username, body.password, body.role, branch=body.branch,
                       must_change=body.must_change, by=admin)
    except ValueError as e:
        raise HTTPException(400, str(e))
    store.log_auth("USER_ADD", body.username, by=admin,
                   detail=f"role={body.role}" + (f" · branch={body.branch}" if body.branch else ""),
                   ip=_client_ip(request))
    return {"ok": True}


@app.post("/api/users/{username}/branch")
def api_set_branch(username: str, body: BranchChange, request: Request,
                   admin: str = Depends(require_permission(rbac.P_USER_MANAGE))):
    try:
        store.set_branch(username, body.branch)
    except ValueError as e:
        raise HTTPException(400, str(e))
    store.log_auth("BRANCH_CHANGE", username, by=admin,
                   detail=f"branch={body.branch or '—'}", ip=_client_ip(request))
    return {"ok": True}


@app.post("/api/users/{username}/status")
def api_set_status(username: str, body: StatusChange, request: Request,
                   admin: str = Depends(require_permission(rbac.P_USER_MANAGE))):
    if username == admin and body.status == "suspended":
        raise HTTPException(400, "You cannot suspend your own account while logged in.")
    try:
        store.set_status(username, body.status)  # suspend bumps session_ver -> kills sessions
    except ValueError as e:
        raise HTTPException(400, str(e))
    store.log_auth("SUSPEND" if body.status == "suspended" else "ACTIVATE", username,
                   by=admin, ip=_client_ip(request))
    return {"ok": True, "status": body.status}


@app.post("/api/me/password")
def api_self_password(body: SelfPasswordChange, request: Request, user: str = Depends(require_user)):
    """Self-service password change — how a branch user clears their first-login
    must-change flag. Requires the current password; re-issues this session so the
    session_ver bump (which kills OTHER devices) doesn't sign the user out here."""
    if not store.authenticate(user, body.current_password):
        raise HTTPException(403, "Current password is incorrect")
    try:
        store.set_password(user, body.new_password)  # clears must_change, bumps session_ver
    except ValueError as e:
        raise HTTPException(400, str(e))
    request.session["ver"] = (store.get_user(user) or {}).get("session_ver", 0)
    request.session["default_pw"] = False
    request.session["must_change"] = False
    store.log_auth("SELF_PASSWORD_CHANGE", user, ip=_client_ip(request))
    return {"ok": True}


@app.post("/api/users/{username}/password")
def api_set_password(username: str, body: PasswordChange, request: Request,
                     admin: str = Depends(require_permission(rbac.P_USER_MANAGE))):
    try:
        store.set_password(username, body.password)  # bumps session_ver -> old cookies die
    except ValueError as e:
        raise HTTPException(400, str(e))
    store.log_auth("PASSWORD_CHANGE", username, by=admin, ip=_client_ip(request))
    if username == admin:  # keep the acting admin signed in on their own change
        request.session["ver"] = (store.get_user(admin) or {}).get("session_ver", 0)
        request.session["default_pw"] = body.password in DEFAULT_PASSWORDS
    return {"ok": True}


@app.post("/api/users/{username}/revoke-sessions")
def api_revoke_sessions(username: str, request: Request,
                        admin: str = Depends(require_permission(rbac.P_USER_MANAGE))):
    """Kill every active session for a user (lost device, offboarding, compromise).
    Their cookies fail the session_ver check on the very next request."""
    try:
        store.bump_session_ver(username)
    except ValueError as e:
        raise HTTPException(400, str(e))
    store.log_auth("SESSION_REVOKE", username, by=admin, ip=_client_ip(request))
    if username == admin:  # revoking yourself still signs out OTHER devices, not this one
        request.session["ver"] = (store.get_user(admin) or {}).get("session_ver", 0)
    return {"ok": True, "detail": f"All sessions for '{username}' revoked."}


@app.get("/api/auth-log")
def api_auth_log(admin: str = Depends(require_permission(rbac.P_USER_MANAGE)), limit: int = 200):
    return {"rows": store.auth_log_list(max(1, min(int(limit or 200), 1000)))}


@app.post("/api/users/{username}/role")
def api_set_role(username: str, body: RoleChange, request: Request,
                 admin: str = Depends(require_permission(rbac.P_USER_MANAGE))):
    if username == admin and body.role != "admin":
        raise HTTPException(400, "You cannot demote your own admin account while logged in.")
    try:
        store.set_role(username, body.role)
    except ValueError as e:
        raise HTTPException(400, str(e))
    store.log_auth("ROLE_CHANGE", username, by=admin, detail=f"role={body.role}",
                   ip=_client_ip(request))
    return {"ok": True}


@app.delete("/api/users/{username}")
def api_delete_user(username: str, request: Request,
                    admin: str = Depends(require_permission(rbac.P_USER_MANAGE))):
    if username == admin:
        raise HTTPException(400, "You cannot delete your own account while logged in.")
    try:
        store.delete_user(username)
    except ValueError as e:
        raise HTTPException(400, str(e))
    store.log_auth("USER_DELETE", username, by=admin, ip=_client_ip(request))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Read-only LMS DB connection settings
# ---------------------------------------------------------------------------
class DbConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = ""
    password: str = ""
    database: str = ""


@app.get("/api/dbconfig")
def api_get_dbconfig(admin: str = Depends(require_permission(rbac.P_DBCONFIG_MANAGE))):
    return dbconfig.public()


@app.post("/api/dbconfig")
def api_save_dbconfig(body: DbConfig, admin: str = Depends(require_permission(rbac.P_DBCONFIG_MANAGE))):
    password = body.password if body.password != "" else dbconfig.load()["password"]
    dbconfig.save(body.host, body.port, body.user, password, body.database)
    return {"ok": True}


@app.post("/api/dbconfig/test")
def api_test_dbconfig(body: DbConfig, admin: str = Depends(require_permission(rbac.P_DBCONFIG_MANAGE))):
    import pymysql

    password = body.password if body.password != "" else dbconfig.load()["password"]
    try:
        conn = pymysql.connect(
            host=body.host.strip(), port=int(body.port), user=body.user.strip(),
            password=password, database=(body.database.strip() or None),
            charset="utf8mb4", connect_timeout=6,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            version = cur.fetchone()[0]
        conn.close()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Connection failed: {e}")
    return {"ok": True, "message": f"Connected — MySQL {version}"}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.get("/login")
def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse(url="/", status_code=302)
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/")
def index(request: Request):
    if not request.session.get("user"):
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse(STATIC_DIR / "index.html")
