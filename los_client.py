"""Engrow **LOS** (Flow A) client — pulls the real loan portfolio + repayment
(NACH-mandate) bank account + loan economics, driven from an approval-gated
console tab (the "Portfolio Sync" view) exactly like the Digitap Live Pull.

Flow A (from PresentmentAPIs.md), executed per Portfolio Sync:

  A1 login(...)                 POST /resources/j_spring_security_check -> X-Auth-Token
  A2 list_applications(...)     POST /api/application/list              -> content[] {uid, applicationNo}
  A3 get_application(uid)       GET  /api/getApplication?...fullChain   -> emi, amount, product, tenure, repay uid, personUid
  A4 fetch_bank_account(uid)    GET  /api/fetchApplicantBankAccount     -> {accountNo, ifsc, bankName, holderName}
  A5 fetch_consent(uid)         **user-supplied endpoint (STUB)**       -> {consent_id, main_txn_id, consent_expiry, status}

Design mirrors aa_live.py:
  * MOCK is the default and is decided PER CALL (the UI toggle passes `live=`),
    so nothing hits the real LOS until a human flips to LIVE and clicks Approve.
  * LIVE requires LOS_BASE_URL + LOS_USER + LOS_PASS in .env (never hard-coded).
  * Every call returns the same 8-key result dict the Digitap ledger persists.
  * NO loop mode: A3/A4/A5 fan out over a FINITE uid list on a bounded
    ThreadPoolExecutor (<=10); a 401/403 triggers a SINGLE re-login, never a
    retry storm; there is no polling/scheduler here.

Both LOS logins return HTTP 200 even on failed auth, so success is decided by
`responseCode == "SUCCESS"`, never the status code. Numeric loan fields default
to 0 when unset, so amount/emi/tenure use `||`-style fallback chains (0 == missing).
"""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import httpx

_SAMPLES = Path(__file__).resolve().parent / "samples"


def _env(name, default=""):
    return (os.getenv(name) or default).strip()


BASE_URL = _env("LOS_BASE_URL").rstrip("/")
LOGIN_PATH = _env("LOS_LOGIN_PATH", "/resources/j_spring_security_check")
LIST_PATH = _env("LOS_LIST_PATH", "/api/application/list")
DETAIL_PATH = _env("LOS_DETAIL_PATH", "/api/getApplication")
BANK_PATH = _env("LOS_BANK_PATH", "/api/fetchApplicantBankAccount")
CONSENT_PATH = _env("LOS_CONSENT_PATH", "")  # A5 — supplied by the user, blank until then

LOS_USER = _env("LOS_USER")
LOS_PASS = _env("LOS_PASS")
STAGES = [s.strip() for s in _env("LOS_STAGES", "DOCUMENT EXECUTION").split(",") if s.strip()]

MOCK_DEFAULT = _env("LOS_MOCK", "true").lower() in ("1", "true", "yes")
HTTP_TIMEOUT = float(_env("LOS_HTTP_TIMEOUT", "60"))
CONNECT_TIMEOUT = float(_env("LOS_CONNECT_TIMEOUT", "15"))
# Bounded fan-out for A3/A4/A5. Hard-capped at 10 (the doc's recommendation) so a
# large portfolio can never open an unbounded number of sockets.
MAX_CONCURRENCY = min(10, max(1, int(_env("LOS_MAX_CONCURRENCY", "10"))))


def _default_origin():
    if not BASE_URL:
        return ""
    p = urlparse(BASE_URL)
    return f"{p.scheme}://{p.netloc}"


ORIGIN = _env("LOS_ORIGIN") or _default_origin()
REFERER = _env("LOS_REFERER") or (ORIGIN + "/" if ORIGIN else "")


def live_configured() -> bool:
    """True only when a real LOS call could actually be made."""
    return bool(BASE_URL and LOS_USER and LOS_PASS)


def status_summary() -> dict:
    return {"mock_default": MOCK_DEFAULT, "live_configured": live_configured(),
            "base_url_set": bool(BASE_URL), "stages": STAGES,
            "consent_query_configured": bool(CONSENT_PATH), "max_concurrency": MAX_CONCURRENCY}


def _use_mock(live=None) -> bool:
    """No runtime mock/live mode — mock replay is permitted ONLY against the in-memory
    mongomock store, so sample data can never land in a real database. `live` is
    accepted and ignored. See aa_live._use_mock for the full rationale."""
    import db as _dbmod
    return bool(_dbmod.MONGO_MOCK)


def _common_headers():
    h = {
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache", "Pragma": "no-cache", "DNT": "1",
        "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
        "User-Agent": ("Mozilla/5.0 (Linux; Android 10; Pixel 3) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/84.0.4076.0 Mobile Safari/537.36"),
    }
    if ORIGIN:
        h["Origin"] = ORIGIN
    if REFERER:
        h["Referer"] = REFERER
    return h


def _result(kind, method, path, request, http_status=None, response=None, ok=False,
            error=None, mode="live"):
    # 8-key shape mongostore.log_los_call/log_aa_call persist, + method for clarity.
    return {"kind": kind, "endpoint": path, "method": method, "request": request,
            "http_status": http_status, "response": response, "ok": bool(ok),
            "error": error, "mode": mode}


# ---------------------------------------------------------------------------
# Low-level HTTP (live) — auth handled by the caller passing X-Auth-Token
# ---------------------------------------------------------------------------
def _redact(body):
    """Never store the password in the ledger."""
    if isinstance(body, dict):
        return {k: ("***" if k in ("j_password", "password") else v) for k, v in body.items()}
    return body


def _http(method, path, *, token=None, params=None, data=None, json_body=None):
    url = BASE_URL + path
    headers = _common_headers()
    if token:
        headers["X-Auth-Token"] = token
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    headers.setdefault("Accept", "application/json, text/plain, */*")
    timeout = httpx.Timeout(HTTP_TIMEOUT, connect=CONNECT_TIMEOUT)
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        resp = client.request(method, url, params=params, data=data, json=json_body, headers=headers)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — CSV/HTML/empty
        body = {"_raw_text": resp.text}
    return resp.status_code, body, resp.headers


def _succeeded(body) -> bool:
    """LOS returns HTTP 200 on failed auth; the real signal is responseCode."""
    if isinstance(body, dict) and "responseCode" in body:
        return str(body.get("responseCode")).upper() == "SUCCESS"
    return True  # detail endpoints don't carry responseCode; rely on http status


# ---------------------------------------------------------------------------
# A1 — login
# ---------------------------------------------------------------------------
def login(live=None):
    """Returns (token_or_None, result). Never raises."""
    body = {"j_username": LOS_USER, "j_password": LOS_PASS, "remember-me": "false"}
    if _use_mock(live):
        resp = _mock("login", None)
        token = resp.get("jSessionId")
        return token, _result("los_login", "POST", LOGIN_PATH, _redact(body), 200, resp, True, None, "mock")
    if not live_configured():
        return None, _result("los_login", "POST", LOGIN_PATH, _redact(body), None, None, False,
                             "Live not configured — set LOS_BASE_URL + LOS_USER + LOS_PASS in .env", "live")
    try:
        status, resp, headers = _http("POST", LOGIN_PATH, data=body)
    except Exception as e:  # noqa: BLE001
        return None, _result("los_login", "POST", LOGIN_PATH, _redact(body), None, None, False,
                             f"{type(e).__name__}: {e}", "live")
    # Engrow returns the session token in the X-Auth-Token *response header* on a
    # 302 (the doc's JSON {responseCode, jSessionId} is only a fallback shape).
    token = headers.get("x-auth-token") or headers.get("authorization")
    if not token and isinstance(resp, dict):
        token = resp.get("jSessionId")
    ok = bool(token)
    if ok:
        err = None
    elif isinstance(resp, dict) and resp.get("detail"):
        err = resp.get("detail")
    else:
        err = f"login failed (HTTP {status}) — no X-Auth-Token returned"
    return (token if ok else None), _result("los_login", "POST", LOGIN_PATH, _redact(body),
                                            status, resp, ok, err, "live")


# ---------------------------------------------------------------------------
# A2 — list applications
# ---------------------------------------------------------------------------
def _list_body(stages):
    return {"applicationDateFrom": None, "applicationDateTo": None, "productTypes": None,
            "stages": stages or None, "branches": None, "show": "ALL",
            "applicantName": "", "applicationNo": "", "leadSource": None, "salesPersonId": ""}


def list_applications(token, stages=None, live=None):
    """Returns (content_list, result). Pages through the LOS list until exhausted —
    a single page-0/size-500 request silently truncates portfolios over 500. See bug."""
    stages = stages if stages is not None else STAGES
    body = _list_body(stages)
    if _use_mock(live):
        resp = _mock("list", None)
        return resp.get("content") or [], _result("los_list", "POST", LIST_PATH, body, 200, resp, True, None, "mock")
    size = 500
    all_content, page, last = [], 0, None
    while True:
        params = {"page": page, "size": size, "sort": "submissionDate,desc"}
        try:
            status, resp, _ = _http("POST", LIST_PATH, token=token, params=params, json_body=body)
        except Exception as e:  # noqa: BLE001
            return all_content, _result("los_list", "POST", LIST_PATH, body, None, None, False,
                                        f"{type(e).__name__}: {e} (page {page})", "live")
        if not (status < 400 and _succeeded(resp)):
            return all_content, _result("los_list", "POST", LIST_PATH, body, status, resp, False,
                                        f"list failed (HTTP {status}) on page {page}", "live")
        content = (resp.get("content") if isinstance(resp, dict) else None) or []
        all_content.extend(content)
        last = resp
        total_pages = resp.get("totalPages") if isinstance(resp, dict) else None
        try:
            tp = int(total_pages) if total_pages is not None else None
        except (TypeError, ValueError):
            tp = None
        if tp is not None:
            if page + 1 >= tp:
                break
        elif len(content) < size:
            break
        page += 1
        if page > 500:  # safety backstop (250k rows) — never loop unbounded
            break
    return all_content, _result("los_list", "POST", LIST_PATH, body, 200, last, True, None, "live")


# ---------------------------------------------------------------------------
# A3 — application detail (with the doc's fallback chains)
# ---------------------------------------------------------------------------
def _first(dto, keys, zero_missing=False):
    for k in keys:
        v = dto.get(k)
        if v in (None, ""):
            continue
        if zero_missing and isinstance(v, (int, float)) and v == 0:
            continue  # 0 is a placeholder, not a real value
        return v
    return None


def _resolve_person(resp):
    """(person_uid, applicant_name). In the real Engrow payload `personDTO` is a
    TOP-LEVEL sibling of `applicationDTO` (not nested), and the applicant entry
    has partyPlay == 'applicant' (lowercase). Falls back to applicationDTO uid
    fields, then customerUid."""
    dto = (resp.get("applicationDTO") or {}) if isinstance(resp, dict) else {}
    persons = (resp.get("personDTO") or []) if isinstance(resp, dict) else []
    applicant = next((p for p in persons if str(p.get("partyPlay")).lower() == "applicant"), None) \
        or (persons[0] if persons else None)
    puid = _first(dto, ["applicantPersonUid", "primaryPersonUid", "personUid"])
    if not puid and applicant:
        puid = applicant.get("uid") or applicant.get("personUid")
    if not puid:
        puid = _first(dto, ["customerUid"])
    name = None
    if applicant:
        name = (applicant.get("name") or applicant.get("personName") or applicant.get("fullName")
                or (" ".join(x for x in [applicant.get("firstName"), applicant.get("middleName"),
                                         applicant.get("lastName")] if x) or None))
    return puid, name


def _parse_detail(uid, resp):
    dto = (resp.get("applicationDTO") if isinstance(resp, dict) else None) or (resp if isinstance(resp, dict) else {})
    person_uid, applicant_name = _resolve_person(resp)
    return {
        "uid": uid,
        "application_no": _first(dto, ["applicationNo", "applicationNumber"]),
        "customer_name": _first(dto, ["applicantName", "customerName", "primaryApplicantName"]) or applicant_name,
        "emi": _first(dto, ["emi"], zero_missing=True),
        "amount_sanc": _first(dto, ["amountSanc", "finalApprovedAmount", "provApprovedAmount", "amountProp", "amountReq"], zero_missing=True),
        "product": _first(dto, ["productName", "productTypeSanc", "productTypeProp", "productCodeSanc", "productCodeProp", "productNameProp"]),
        "tenure": _first(dto, ["tenureSanc", "finalApprovalTenure", "tenureProp", "tenureReq"], zero_missing=True),
        "repay_uid": _first(dto, ["repaymentBankaccountUid", "finalApprovedRepaymentAccount"]),
        "person_uid": person_uid,
    }


def get_application(token, uid, live=None):
    params = {"applicationUid": uid, "filter": "fullChain"}
    if _use_mock(live):
        resp = _mock("detail", uid)
        return _parse_detail(uid, resp), _result("los_detail", "GET", DETAIL_PATH, params, 200, resp, True, None, "mock")
    try:
        status, resp, _ = _http("GET", DETAIL_PATH, token=token, params=params)
    except Exception as e:  # noqa: BLE001
        return {"uid": uid}, _result("los_detail", "GET", DETAIL_PATH, params, None, None, False, f"{type(e).__name__}: {e}", "live")
    ok = status < 400
    parsed = _parse_detail(uid, resp) if ok else {"uid": uid}
    return parsed, _result("los_detail", "GET", DETAIL_PATH, params, status, resp, ok,
                           None if ok else f"getApplication failed (HTTP {status})", "live")


# ---------------------------------------------------------------------------
# A4 — repayment bank account (array-pick + alt field names)
# ---------------------------------------------------------------------------
def _map_bank(entry):
    return {
        "account_no": _first(entry, ["accountNo", "accountNumber", "bankAccountNo", "accNo"]),
        "ifsc": _first(entry, ["ifsc", "ifscCode", "ifsc_code"]),
        "bank_name": _first(entry, ["bankName", "bank", "bankNameCode"]),
        "holder_name": _first(entry, ["holderName", "accountHolderName", "nameAsPerBankRecords"]),
        "mandate_person_uid": _first(entry, ["linkToUid", "personUid"]),
        "uid": entry.get("uid"),
    }


def _pick_bank(resp, repay_uid):
    rows = resp if isinstance(resp, list) else [resp] if isinstance(resp, dict) else []
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return {}
    if repay_uid:
        match = next((r for r in rows if r.get("uid") == repay_uid), None)
        if match:
            return _map_bank(match)
    return _map_bank(rows[0])


def fetch_bank_account(token, uid, person_uid, repay_uid, live=None):
    params = {"applicationUid": uid, "personUid": person_uid}
    if _use_mock(live):
        resp = _mock("bank", uid)
        return _pick_bank(resp, repay_uid), _result("los_bank", "GET", BANK_PATH, params, 200, resp, True, None, "mock")
    try:
        status, resp, _ = _http("GET", BANK_PATH, token=token, params=params)
    except Exception as e:  # noqa: BLE001
        return {}, _result("los_bank", "GET", BANK_PATH, params, None, None, False, f"{type(e).__name__}: {e}", "live")
    ok = status < 400
    return (_pick_bank(resp, repay_uid) if ok else {}), _result("los_bank", "GET", BANK_PATH, params, status, resp, ok,
                                                                None if ok else f"fetchBankAccount failed (HTTP {status})", "live")


# ---------------------------------------------------------------------------
# A5 — AA consent status  ***STUB*** (endpoint supplied later by the user)
# ---------------------------------------------------------------------------
def _map_consent(resp):
    if not isinstance(resp, dict):
        return {}
    return {"consent_id": _first(resp, ["consent_id", "consentId", "handle"]),
            "main_txn_id": _first(resp, ["main_txn_id", "mainTxnId", "txn_id"]),
            "consent_expiry": _first(resp, ["consent_expiry", "expiry", "consentExpiry"]),
            "status": _first(resp, ["status", "consentStatus"])}


def fetch_consent(token, uid, live=None):
    """A5 — returns ({consent_id, main_txn_id, consent_expiry, status}, result).

    The real endpoint + response shape are still to be supplied by the user; in
    MOCK it replays samples/los_consent.json, in LIVE it is a no-op that returns
    an empty consent (so the Mongo-side PCPL overlay decides pullability) and
    flags itself in the ledger, rather than calling an unknown URL."""
    params = {"applicationUid": uid}
    if _use_mock(live):
        resp = _mock("consent", uid)
        return _map_consent(resp), _result("los_consent", "GET", CONSENT_PATH or "(A5 mock)", params, 200, resp, True, None, "mock")
    if not CONSENT_PATH:
        return {}, _result("los_consent", "GET", "(A5 not configured)", params, None, None, True,
                          "A5 consent query not configured — blend falls back to Mongo consent registry", "live")
    try:
        status, resp, _ = _http("GET", CONSENT_PATH, token=token, params=params)
    except Exception as e:  # noqa: BLE001
        return {}, _result("los_consent", "GET", CONSENT_PATH, params, None, None, False, f"{type(e).__name__}: {e}", "live")
    ok = status < 400
    return (_map_consent(resp) if ok else {}), _result("los_consent", "GET", CONSENT_PATH, params, status, resp, ok,
                                                       None if ok else f"fetchConsent failed (HTTP {status})", "live")


# ---------------------------------------------------------------------------
# Orchestrator — A1..A5, bounded fan-out, single re-login on 401/403, no loops
# ---------------------------------------------------------------------------
def _is_auth_expiry(res):
    return res.get("http_status") in (401, 403)


def sync_portfolio(live=None, stages=None):
    """Run Flow A once and return {rows, calls, counts}. Approval-gated by the
    caller (the POST /api/los/sync endpoint fires only on the tab's Approve)."""
    calls = []
    sess = {"token": None}
    lock = threading.Lock()

    token, res = login(live)
    calls.append(res)
    if not res.get("ok") or not token:
        return {"rows": [], "calls": calls, "counts": {"applications": 0, "rows": 0, "errors": 1},
                "error": res.get("error") or "login failed"}
    sess["token"] = token

    apps, res = list_applications(token, stages, live)
    calls.append(res)
    if not res.get("ok"):
        return {"rows": [], "calls": calls, "counts": {"applications": 0, "rows": 0, "errors": 1},
                "error": res.get("error") or "list failed"}

    relogged = {"done": False}

    def _relogin_once():
        with lock:
            if relogged["done"]:
                return sess["token"]
            relogged["done"] = True
            tok, r = login(live)
            calls.append(r)
            if r.get("ok") and tok:
                sess["token"] = tok
            return sess["token"]

    def _one(app):
        uid = app.get("uid")
        app_no = app.get("applicationNo") or app.get("applicationNumber")
        local = []
        detail, r = get_application(sess["token"], uid, live)
        if _is_auth_expiry(r):  # single bounded re-login, then one retry
            _relogin_once()
            detail, r = get_application(sess["token"], uid, live)
        local.append(r)
        # Auth can expire on ANY leg — a token that lapsed after the detail call would
        # otherwise zero the bank/consent and silently drop the repayment account. See bug.
        bank, r = fetch_bank_account(sess["token"], uid, detail.get("person_uid"), detail.get("repay_uid"), live)
        if _is_auth_expiry(r):
            _relogin_once()
            bank, r = fetch_bank_account(sess["token"], uid, detail.get("person_uid"), detail.get("repay_uid"), live)
        bank_ok = bool(r.get("ok"))
        local.append(r)
        consent, r = fetch_consent(sess["token"], uid, live)
        if _is_auth_expiry(r):
            _relogin_once()
            consent, r = fetch_consent(sess["token"], uid, live)
        consent_ok = bool(r.get("ok"))
        local.append(r)
        row = {
            "los_application_no": detail.get("application_no") or app_no,
            # Flow A yields only the LOS application no; the true LMS loan_id
            # arrives with Flow B — seed loan_id = applicationNo for now (never null).
            "loan_id": detail.get("application_no") or app_no,
            "uid": uid,
            "customer_name": detail.get("customer_name") or app.get("applicantName"),
            "emi": detail.get("emi"), "amount_sanc": detail.get("amount_sanc"),
            "product": detail.get("product"), "tenure": detail.get("tenure"),
            "npa_parked": False,
            "repayment": bank,
            "los_consent": consent,
            # leg-success flags so ingest doesn't clobber a good value on a transient failure
            "bank_ok": bank_ok, "consent_ok": consent_ok,
        }
        return row, local

    def _one_safe(app):
        # Contain a per-row parse crash so one weird LOS payload can't kill the whole sync
        # (which would lose the entire call ledger for the run). See bug.
        try:
            return _one(app)
        except Exception as e:  # noqa: BLE001
            u = app.get("uid") if isinstance(app, dict) else None
            return None, [_result("los_detail", "GET", DETAIL_PATH, {"applicationUid": u},
                                  None, None, False, f"row parse crashed: {type(e).__name__}: {e}",
                                  "mock" if _use_mock(live) else "live")]

    rows = []
    errors = 0
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as ex:
        for row, local in ex.map(_one_safe, apps):
            if row is not None:
                rows.append(row)
            calls.extend(local)
            errors += sum(1 for c in local if not c.get("ok"))

    return {"rows": rows, "calls": calls,
            "counts": {"applications": len(apps), "rows": len(rows), "errors": errors,
                       "relogins": 1 if relogged["done"] else 0}}


# ---------------------------------------------------------------------------
# MOCK replay — a small synthetic portfolio under samples/los_*.json
# ---------------------------------------------------------------------------
def _load(name):
    with open(_SAMPLES / name, encoding="utf-8") as fh:
        return json.load(fh)


def _mock(kind, uid):
    try:
        if kind == "login":
            return _load("los_login.json")
        if kind == "list":
            return _load("los_applications.json")
        if kind == "detail":
            return (_load("los_application_detail.json") or {}).get(uid, {})
        if kind == "bank":
            return (_load("los_bank_account.json") or {}).get(uid, {})
        if kind == "consent":
            return (_load("los_consent.json") or {}).get(uid, {})
    except Exception:  # noqa: BLE001
        return {}
    return {}
