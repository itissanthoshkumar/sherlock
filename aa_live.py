"""Live Account Aggregator client — the UPDATED Digitap API (single Basic-auth
pair, combined ONE-TIME + PERIODIC consent) driven step-by-step from the UI with
a human approval before every real call.

Deliberately separate from digitap.py (which is load-bearing for the mock cycle
and the smoke test). The lifecycle:

  1. generate_url(...)          POST /bank-data/generateurl        -> {url, request_id}
  2. status_check(request_id)   POST /bank-data/statuscheck        -> txn_status[] (one-time -> main_txn_id)
  3. initiate_periodic(main)    POST /bank-data/initiate_periodic_fetch -> periodic txn_id
  4. status_check(req, txn)     POST /bank-data/statuscheck        -> txn_status[] (periodic)
  5. retrieve_report(txn, kind) POST /bank-data/retrievereport     -> full AA report JSON

Every function returns the same 7-key call-result dict digitap.py uses
(kind/endpoint/request/http_status/response/ok/error) so mongostore.log_api()
persists the audit trail unchanged.

MOCK vs LIVE is decided PER CALL (the UI's top toggle passes `live=`), defaulting
to AA_LIVE_MOCK. In MOCK it replays the exact sample responses under samples/;
LIVE requires AA_LIVE_BASE_URL + credentials to be set (never hard-coded).
"""
import calendar
import json
import os
import random
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

BASE_URL = os.getenv("AA_LIVE_BASE_URL", "").rstrip("/")
GENERATE_PATH = os.getenv("AA_LIVE_GENERATE_PATH", "/bank-data/generateurl")
INITIATE_PATH = os.getenv("AA_LIVE_INITIATE_PATH", "/bank-data/initiate_periodic_fetch")
STATUS_PATH = os.getenv("AA_LIVE_STATUS_PATH", "/bank-data/statuscheck")
RETRIEVE_PATH = os.getenv("AA_LIVE_RETRIEVE_PATH", "/bank-data/retrievereport")

# Auth: either a user/pass pair, or the pre-encoded Basic token. Never committed.
AUTH_USER = os.getenv("AA_LIVE_AUTH_USER", "")
AUTH_PASS = os.getenv("AA_LIVE_AUTH_PASS", "")
AUTH_TOKEN = os.getenv("AA_LIVE_AUTH_TOKEN", "")  # base64 of user:pass (the "Basic xxx" value)

MOCK_DEFAULT = os.getenv("AA_LIVE_MOCK", "true").lower() in ("1", "true", "yes")
# generateurl / retrievereport can take 60s+ (the FIP prepares data). Read window
# must comfortably exceed that; connect stays short so a dead ngrok fails fast.
HTTP_TIMEOUT = float(os.getenv("AA_LIVE_HTTP_TIMEOUT", "180"))
CONNECT_TIMEOUT = float(os.getenv("AA_LIVE_CONNECT_TIMEOUT", "15"))
CBURL = os.getenv("AA_LIVE_CBURL", "https://webhook.site/00000000-0000-0000-0000-000000000000")

_SAMPLES = Path(__file__).resolve().parent / "samples"


def live_configured() -> bool:
    """True only when a real call could actually be made (URL + some auth set)."""
    return bool(BASE_URL and (AUTH_TOKEN or (AUTH_USER and AUTH_PASS)))


def status_summary() -> dict:
    return {"mock_default": MOCK_DEFAULT, "live_configured": live_configured(),
            "base_url_set": bool(BASE_URL)}


def _use_mock(live=None) -> bool:
    """THERE IS NO RUNTIME MOCK/LIVE MODE. Mock replay is a test-harness facility and
    is permitted ONLY when the store is the in-memory mongomock database — so a sample
    payload can never be persisted to a real database under a real consent/mandate id.
    The caller's `live` argument is accepted and ignored (callers are being cleaned up).

    Why this is a hard rule: a mock retrieve once stored the bundled BODA MOHAN sample
    against a real borrower's mandate txn in live Atlas, because a page-level toggle
    silently decided the mode. Tying mock to MONGO_MOCK makes that impossible rather
    than merely discouraged. With a real store, an unconfigured vendor now fails loudly
    ("Live not configured") instead of quietly serving fake data."""
    import db as _dbmod
    return bool(_dbmod.MONGO_MOCK)


def _auth():
    if AUTH_USER and AUTH_PASS:
        return (AUTH_USER, AUTH_PASS), {}
    if AUTH_TOKEN:
        return None, {"Authorization": "Basic " + AUTH_TOKEN}
    return None, {}


def _result(kind, path, request, http_status=None, response=None, ok=False, error=None, mode="live"):
    return {"kind": kind, "endpoint": path, "request": request, "http_status": http_status,
            "response": response, "ok": ok, "error": error, "mode": mode}


def _curl_of(url, headers, auth, body):
    """The exact request as a copy-pasteable curl, for debugging against Postman. The
    Basic credential is REDACTED to a placeholder — the value is a shared vendor secret,
    it's proven working (any 200 confirms it), and it must not land in the UI, ledger or
    a screenshot. Everything else — method, url, headers, body — is shown verbatim."""
    lines = [f"curl --location '{url}'"]
    if auth:  # (user, pass) basic-auth tuple
        lines.append(f"--user '{auth[0]}:<AA_LIVE_AUTH_PASS>'")
    for k, v in headers.items():
        if k.lower() == "authorization":
            v = "Basic <AA_LIVE_AUTH_TOKEN — redacted; set in .env>"
        lines.append(f"--header '{k}: {v}'")
    lines.append("--data '" + json.dumps(body, indent=2) + "'")
    return " \\\n".join(lines)


def _call(kind, path, body, live):
    auth, hdrs = _auth()
    # Only the headers Digitap needs — Content-Type + auth. No Accept header (matches the
    # working Postman request; an unexpected Accept can trip strict upstreams).
    headers = {"Content-Type": "application/json", **hdrs}
    url = (BASE_URL or "<AA_LIVE_BASE_URL — not set>") + path
    # attached to every return so the request is inspectable whether it succeeds, fails,
    # is mocked, or short-circuits on missing config.
    meta = {"url": url, "method": "POST", "curl": _curl_of(url, headers, auth, body)}
    if _use_mock(live):
        return {**_result(kind, path, body, 200, _mock_response(kind, body), True, None, "mock"), **meta}
    if not live_configured():
        return {**_result(kind, path, body, None, None, False,
                          "Live not configured — set AA_LIVE_BASE_URL + credentials in .env", "live"), **meta}
    timeout = httpx.Timeout(HTTP_TIMEOUT, connect=CONNECT_TIMEOUT)
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(BASE_URL + path, json=body, auth=auth, headers=headers)
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = {"_raw_text": resp.text}
        ok = resp.status_code < 400
        return {**_result(kind, path, body, resp.status_code, data, ok,
                          None if ok else f"HTTP {resp.status_code}", "live"), **meta}
    except Exception as e:  # noqa: BLE001
        return {**_result(kind, path, body, None, None, False, f"{type(e).__name__}: {e}", "live"), **meta}


# ---------------------------------------------------------------------------
# The five lifecycle calls
# ---------------------------------------------------------------------------
# Consent-window ceilings (Digitap / AA-framework limits). Enforced on every
# generate-url so an out-of-range request can never be sent:
#   one-time FI range  <= 13 months back from today
#   periodic FI range  <=  6 months back from today
#   periodic consent expiry <= 5 years from today
# All ranges end no later than today; the expiry is never in the past.
ONETIME_MAX_MONTHS = 13
PERIODIC_MAX_MONTHS = 6
EXPIRY_MAX_YEARS = 5


def _shift_months(d, months):
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _parse_date(s, fallback):
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return fallback


def clamp_consent_window(onetime_start, onetime_end, periodic_start, periodic_end, periodic_expiry):
    """Force the consent request inside the allowed ceilings. Values already within
    range pass through unchanged; anything out of range is pulled to the boundary
    (never rejected — the request always goes out compliant). Returns 5 ISO strings."""
    today = date.today()

    def _window(start, end, max_months):
        e = min(_parse_date(end, today), today)            # end never in the future
        floor = _shift_months(today, -max_months)          # earliest allowed start
        s = _parse_date(start, floor)
        s = min(max(s, floor), e)                          # start within [floor, end]
        return s.isoformat(), e.isoformat()

    ot_s, ot_e = _window(onetime_start, onetime_end, ONETIME_MAX_MONTHS)
    pd_s, pd_e = _window(periodic_start, periodic_end, PERIODIC_MAX_MONTHS)
    exp_cap = _shift_months(today, EXPIRY_MAX_YEARS * 12)   # today + 5 years
    exp = _parse_date(periodic_expiry, exp_cap)
    if exp > exp_cap or exp <= today:                      # cap at 5y; never already-expired
        exp = exp_cap
    return ot_s, ot_e, pd_s, pd_e, exp.isoformat()


def build_generate_payload(mobile_num, onetime_start, onetime_end,
                           periodic_start, periodic_end, periodic_expiry,
                           client_ref_num=None, cburl=None):
    # Defence in depth: clamp here too, so no caller can ever assemble an
    # out-of-range consent request even if it bypasses the endpoint's clamp.
    onetime_start, onetime_end, periodic_start, periodic_end, periodic_expiry = \
        clamp_consent_window(onetime_start, onetime_end, periodic_start, periodic_end, periodic_expiry)
    return {
        "txn_completed_cburl": cburl or CBURL,
        "consent_request": [
            {"fi_types": ["DEPOSIT"], "fetch_type": "ONETIME",
             "fi_date_range": {"end_date": onetime_end, "start_date": onetime_start}},
            {"fi_types": ["DEPOSIT"], "fetch_type": "PERIODIC",
             "fi_date_range": {"end_date": periodic_end, "start_date": periodic_start},
             "consent": {"expiry": periodic_expiry}},
        ],
        "mobile_num": mobile_num,
        "destination": "accountaggregator",
        "acceptance_policy": "atLeastOneTransactionInRange",
        "client_ref_num": client_ref_num or gen_client_ref(),
    }


def generate_url(payload, live=None):
    return _call("aa_generate_url", GENERATE_PATH, payload, live)


def status_check(request_id, txn_id=None, live=None):
    body = {"request_id": str(request_id)}
    if txn_id:
        body["txn_id"] = txn_id
    return _call("aa_status", STATUS_PATH, body, live)


def initiate_periodic(main_txn_id, live=None):
    return _call("aa_initiate", INITIATE_PATH, {"main_txn_id": main_txn_id}, live)


def retrieve_report(txn_id, fetch_type="ONETIME", live=None):
    body = {"txn_id": txn_id, "report_subtype": "type3", "report_type": "json"}
    res = _call("aa_retrieve", RETRIEVE_PATH, body, live)
    res["fetch_type"] = fetch_type
    return res


# ---------------------------------------------------------------------------
# Response readers
# ---------------------------------------------------------------------------
def gen_client_ref() -> str:
    """Digitap client_ref_num — a unique 28-digit NUMERIC reference, one per request,
    matching the exact shape Digitap uses (e.g. 8629235539633377291783574156):
    an 18-digit random prefix + the 10-digit unix epoch (seconds).

    The previous value was "PP" + a timestamp — non-numeric and only 22 chars, which
    Digitap rejects. client_ref_num is an opaque idempotency/reference token (not a
    displayed timestamp), so epoch seconds here is correct and zone-independent."""
    prefix = random.randint(10 ** 17, 10 ** 18 - 1)   # exactly 18 digits, never leading-zero
    return f"{prefix}{int(time.time())}"               # + 10-digit epoch = 28 digits total


def url_of(res):
    r = res.get("response") or {}
    return {"url": r.get("url"), "request_id": r.get("request_id"), "expires": r.get("expires"),
            "status": r.get("status")}


def status_of(res):
    """Pull the first txn_status row -> {code, status, txn_id, main_txn_id, msg}."""
    r = res.get("response") or {}
    rows = r.get("txn_status") or []
    first = rows[0] if rows else {}
    return {"request_id": r.get("request_id"), "code": first.get("code"), "state": first.get("status"),
            "txn_id": first.get("txn_id"), "main_txn_id": first.get("main_txn_id"), "msg": first.get("msg"),
            "ready": str(first.get("code") or "").lower() in ("reportgenerated", "reportsuccess")}


def initiate_of(res):
    r = res.get("response") or {}
    return {"txn_id": r.get("txn_id"), "main_txn_id": r.get("main_txn_id"), "code": r.get("code")}


# ---------------------------------------------------------------------------
# MOCK replay — the exact sample responses supplied for this integration
# ---------------------------------------------------------------------------
def _load(name):
    with open(_SAMPLES / name, encoding="utf-8") as fh:
        return json.load(fh)


def _mock_response(kind, body):
    if kind == "aa_generate_url":
        exp = (date.today() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        return {"url": "https://bsuiprod.digitap.work/bank-data?gl=MOCK_CONSENT_URL&destination=accountaggregator&theme_id=dt",
                "expires": exp, "status": "success", "request_id": 82644925}
    if kind == "aa_status":
        row = {"code": "ReportGenerated", "status": "Success",
               "msg": "Your account has been successfully analyzed. Please click on below button to continue further.",
               "txn_id": body.get("txn_id") or "da4b9b8f8"}
        if body.get("txn_id"):
            row["main_txn_id"] = "da4b9b8f8"
        return {"status": "success", "request_id": body.get("request_id"), "txn_status": [row]}
    if kind == "aa_initiate":
        return {"status": "success", "code": "AAFIPullRequestSuccess",
                "msg": "We have requested FI to prepare data.",
                "txn_id": "ap66fd50", "main_txn_id": body.get("main_txn_id")}
    if kind == "aa_retrieve":
        txn = body.get("txn_id") or ""
        name = "aa_live_periodic.json" if txn.startswith("ap") else "aa_live_onetime.json"
        try:
            return _load(name)
        except Exception:  # noqa: BLE001
            return {"banks": [], "error": "sample not found"}
    return {"status": "success"}
