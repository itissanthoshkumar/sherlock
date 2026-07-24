"""Encore **LMS** (Flow B) client — pulls the Presentment Report (loans due for
collection: contact number, demand amount, due date, EMI, POS, OD days), driven
from an approval-gated console tab exactly like the LOS Portfolio Sync.

Flow B (from PresentmentAPIs.md + verified live 2026-07-11):

  B1 login(...)              POST /resources/j_spring_security_check -> X-Auth-Token header
  B2 fetch_presentment(...)  PUT  /api/reports/createReportDirect    -> raw CSV (1 row / loan due)

Reality vs the doc (same as LOS): the session token comes back in the
**X-Auth-Token RESPONSE HEADER** on a 302, not a JSON `jSessionId`. The report is
a 22-column CSV whose header we parse dynamically (Account ID, Customer Name,
Contact Number, Demand, Demand Date, OD Days, Account Status, ...).

MOCK is the default and per-call (the UI toggle passes `live=`); LIVE needs
LMS_BASE_URL + LMS_USER + LMS_PASS in .env. Every call returns the same 8-key
result dict the ledger persists. No loop mode: one login + one report fetch.
"""
import csv
import io
import os
from pathlib import Path

import httpx

_SAMPLES = Path(__file__).resolve().parent / "samples"


def _env(name, default=""):
    return (os.getenv(name) or default).strip()


BASE_URL = _env("LMS_BASE_URL").rstrip("/")
LOGIN_PATH = _env("LMS_LOGIN_PATH", "/resources/j_spring_security_check")
REPORT_PATH = _env("LMS_REPORT_PATH", "/api/reports/createReportDirect")
REPORT_NAME = _env("LMS_REPORT_NAME", "PresentationReport")

LMS_USER = _env("LMS_USER")
LMS_PASS = _env("LMS_PASS")

MOCK_DEFAULT = _env("LMS_MOCK", "true").lower() in ("1", "true", "yes")
HTTP_TIMEOUT = float(_env("LMS_HTTP_TIMEOUT", "180"))
CONNECT_TIMEOUT = float(_env("LMS_CONNECT_TIMEOUT", "15"))


def _default_origin():
    if not BASE_URL:
        return ""
    from urllib.parse import urlparse
    p = urlparse(BASE_URL)
    return f"{p.scheme}://{p.netloc}"


ORIGIN = _env("LMS_ORIGIN") or _default_origin()
REFERER = _env("LMS_REFERER") or (ORIGIN + "/" if ORIGIN else "")

# The report body is fixed (the "PresentationReport" template, FileName Extension
# input). Kept as a module constant so every call is identical + auditable.
REPORT_BODY = {
    "id": 0, "version": 0, "tenantCode": None, "sortOrder": None, "reportGroup": None,
    "reportName": REPORT_NAME, "reportDescription": None, "reportTemplate": None,
    "reportOutputFileName": None, "reportOutputType": None, "eodTrigger": None, "bodTrigger": None,
    "eodDiscriminator": None, "bodDiscriminator": None, "reportClassifier": None, "preScript": None,
    "writerScript": None, "scope": None, "passwordProtectionPattern": None, "compressArtifact": None,
    "removeInputSheets": None, "useReadReplica": None, "bulkParameters": [], "bulkParameterSeparator": ",",
    "inputParameters": [{"param": "input_text", "type": None, "description": "FileName Extension",
                         "date": None, "inputField": "User", "selectField1": None, "selectField2": None,
                         "selectField3": None, "selectField4": None, "selectField5": None,
                         "selectField6": None, "selectField7": None, "selectField8": None,
                         "selectField9": None, "selectField10": None, "selectField11": None,
                         "selectField12": None, "defaultValue": "User"}],
    "reportQueries": [], "reportLinkParameters": [],
}


def live_configured() -> bool:
    return bool(BASE_URL and LMS_USER and LMS_PASS)


def status_summary() -> dict:
    return {"mock_default": MOCK_DEFAULT, "live_configured": live_configured(),
            "base_url_set": bool(BASE_URL), "report_name": REPORT_NAME}


def _use_mock(live=None) -> bool:
    """No runtime mock/live mode — mock replay is permitted ONLY against the in-memory
    mongomock store, so sample data can never land in a real database. `live` is
    accepted and ignored. See aa_live._use_mock for the full rationale."""
    import db as _dbmod
    return bool(_dbmod.MONGO_MOCK)


def _common_headers():
    h = {
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8", "Cache-Control": "no-cache",
        "Pragma": "no-cache", "DNT": "1", "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": ("Mozilla/5.0 (Linux; Android 10; Pixel 3) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/84.0.4076.0 Mobile Safari/537.36"),
    }
    if ORIGIN:
        h["Origin"] = ORIGIN
    if REFERER:
        h["Referer"] = REFERER
    return h


def _redact(body):
    if isinstance(body, dict):
        return {k: ("***" if k in ("j_password", "password") else v) for k, v in body.items()}
    return body


def _result(kind, method, path, request, http_status=None, response=None, ok=False, error=None, mode="live"):
    return {"kind": kind, "endpoint": path, "method": method, "request": request,
            "http_status": http_status, "response": response, "ok": bool(ok), "error": error, "mode": mode}


def _http(method, path, *, token=None, data=None, json_body=None):
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
        resp = client.request(method, url, data=data, json=json_body, headers=headers)
    return resp.status_code, resp.text, resp.headers


# ---------------------------------------------------------------------------
# B1 — login
# ---------------------------------------------------------------------------
def login(live=None):
    body = {"j_username": LMS_USER, "j_password": LMS_PASS, "otp": "undefined",
            "remember-me": "false", "submit": "Login"}
    if _use_mock(live):
        return "MOCK-LMS-TOKEN", _result("lms_login", "POST", LOGIN_PATH, _redact(body), 200,
                                         {"mock": True}, True, None, "mock")
    if not live_configured():
        return None, _result("lms_login", "POST", LOGIN_PATH, _redact(body), None, None, False,
                             "Live not configured — set LMS_BASE_URL + LMS_USER + LMS_PASS in .env", "live")
    try:
        status, text, headers = _http("POST", LOGIN_PATH, data=body)
    except Exception as e:  # noqa: BLE001
        return None, _result("lms_login", "POST", LOGIN_PATH, _redact(body), None, None, False,
                             f"{type(e).__name__}: {e}", "live")
    token = headers.get("x-auth-token") or headers.get("authorization")
    ok = bool(token)
    err = None if ok else f"login failed (HTTP {status}) — no X-Auth-Token returned"
    return (token if ok else None), _result("lms_login", "POST", LOGIN_PATH, _redact(body),
                                            status, {"token": "set" if token else None}, ok, err, "live")


# ---------------------------------------------------------------------------
# B2 — presentment report (CSV)
# ---------------------------------------------------------------------------
def _norm_key(col):
    k = "".join(c if c.isalnum() else "_" for c in str(col).strip().lower())
    while "__" in k:
        k = k.replace("__", "_")
    return k.strip("_")


def parse_presentment_csv(text):
    """CSV text -> list of dicts with normalized snake_case keys (+ raw header kept)."""
    rows = list(csv.reader(io.StringIO(text or "")))
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return [], []
    header = [h.strip() for h in rows[0]]
    keys = [_norm_key(h) for h in header]
    out = []
    for r in rows[1:]:
        d = {keys[i]: (r[i].strip() if i < len(r) else None) for i in range(len(keys))}
        out.append(d)
    return out, header


def fetch_presentment(token, live=None):
    """Returns (rows, result). rows = list of normalized dicts."""
    if _use_mock(live):
        text = _mock_csv()
        rows, header = parse_presentment_csv(text)
        return rows, _result("lms_presentment", "PUT", REPORT_PATH, {"reportName": REPORT_NAME},
                             200, {"rows": len(rows), "columns": header}, True, None, "mock")
    params = "?_no_loader_required=true&_no_global_error=true"
    try:
        status, text, _ = _http("PUT", REPORT_PATH + params, token=token, json_body=REPORT_BODY)
    except Exception as e:  # noqa: BLE001
        return [], _result("lms_presentment", "PUT", REPORT_PATH, {"reportName": REPORT_NAME},
                          None, None, False, f"{type(e).__name__}: {e}", "live")
    # Only a real 200 report is valid. A 302 (expired/invalid X-Auth-Token bounces to the
    # login page — follow_redirects=False) or any non-200 must NOT read as "no presentment".
    if status != 200:
        return [], _result("lms_presentment", "PUT", REPORT_PATH, {"reportName": REPORT_NAME},
                          status, {"body": (text or "")[:300]}, False, f"report failed (HTTP {status})", "live")
    rows, header = parse_presentment_csv(text)
    # A 200 HTML/JSON error page or a renamed column parses to 0 account_ids and would wipe
    # the book under a green tick — require the Account ID column before accepting the report.
    if "account_id" not in [_norm_key(h) for h in header]:
        return [], _result("lms_presentment", "PUT", REPORT_PATH, {"reportName": REPORT_NAME},
                          status, {"body": (text or "")[:300]}, False,
                          "unexpected report format — 'Account ID' column missing", "live")
    # header WITH account_id but no data rows = genuinely no presentment due — success (the
    # empty-fetch guard in bulk_replace then preserves the prior snapshot).
    return rows, _result("lms_presentment", "PUT", REPORT_PATH, {"reportName": REPORT_NAME},
                         status, {"rows": len(rows), "columns": header}, True, None, "live")


# ---------------------------------------------------------------------------
# Orchestrator — one login + one report fetch (no loops)
# ---------------------------------------------------------------------------
def sync_presentment(live=None):
    calls = []
    token, r = login(live)
    calls.append(r)
    if not r.get("ok") or not token:
        return {"rows": [], "calls": calls, "counts": {"rows": 0, "errors": 1},
                "error": r.get("error") or "login failed"}
    rows, r = fetch_presentment(token, live)
    calls.append(r)
    if not r.get("ok"):
        return {"rows": rows, "calls": calls, "counts": {"rows": len(rows), "errors": 1},
                "error": r.get("error")}
    return {"rows": rows, "calls": calls, "counts": {"rows": len(rows), "errors": 0}}


# ---------------------------------------------------------------------------
# MOCK replay
# ---------------------------------------------------------------------------
def _mock_csv():
    try:
        with open(_SAMPLES / "lms_presentment.csv", encoding="utf-8") as fh:
            return fh.read()
    except Exception:  # noqa: BLE001
        return "Account ID,Customer Name,Contact Number,Demand,Demand Date,Account Status\n"
