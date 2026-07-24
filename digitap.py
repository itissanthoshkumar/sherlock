"""Digitap bank-data (Account Aggregator) client — 3-call periodic-fetch flow.

  1. initiate(main_txn_id)  -> child txn_id          [credentials set A: INIT]
  2. status(child_txn_id)   -> readiness payload      [credentials set B: DATA]
  3. retrieve(child_txn_id) -> banking data           [credentials set B: DATA]

Per the supplied curls there are TWO Basic-auth credential pairs: `initiate`
uses one pair, `statuscheck` + `retrievereport` use the other.

Each function returns a *call result* dict so the caller can persist the full
request/response audit trail:

    {"kind", "endpoint", "request", "http_status", "response", "ok", "error"}

Use the helpers (child_id_of / is_ready / extract_balance) to read values out of
a result's response. Set DIGITAP_MOCK=true to run the whole flow with canned
responses (no network, no credentials).
"""
import os

import httpx

BASE_URL = os.getenv("DIGITAP_BASE_URL", "https://svc.digitap.ai").rstrip("/")
INITIATE_PATH = os.getenv("DIGITAP_INITIATE_PATH", "/bank-data/initiate_periodic_fetch")
STATUS_PATH = os.getenv("DIGITAP_STATUS_PATH", "/bank-data/statuscheck")
RETRIEVE_PATH = os.getenv("DIGITAP_RETRIEVE_PATH", "/bank-data/retrievereport")
CONSENT_PATH = os.getenv("DIGITAP_CONSENT_PATH", "/bank-data/consent/request")  # set from the real consent curl

# Two Basic-auth credential pairs — INIT (initiate), DATA (status + retrieve).
INIT_USER = os.getenv("DIGITAP_INIT_USER", "")
INIT_PASS = os.getenv("DIGITAP_INIT_PASS", "")
DATA_USER = os.getenv("DIGITAP_DATA_USER", "")
DATA_PASS = os.getenv("DIGITAP_DATA_PASS", "")

def _mock_allowed() -> bool:
    """Mock replay is permitted ONLY against the in-memory mongomock store, so sample
    data can never be persisted to a real database. DIGITAP_MOCK alone is no longer
    enough — there is no runtime mock/live mode on this platform."""
    import db as _dbmod
    return (os.getenv("DIGITAP_MOCK", "true").lower() in ("1", "true", "yes")
            and bool(_dbmod.MONGO_MOCK))


class _MockFlag:
    """Evaluated on read so `digitap.MOCK` always reflects the rule above (the store is
    resolved at import time otherwise, and tests flip MONGO_MOCK after import)."""
    def __bool__(self):
        return _mock_allowed()


MOCK = _MockFlag()
HTTP_TIMEOUT = float(os.getenv("DIGITAP_HTTP_TIMEOUT", "30"))

# Response field mapping (dotted paths; tweak once live responses are seen).
CHILD_ID_PATH = os.getenv("DIGITAP_CHILD_ID_PATH", "txn_id")
STATUS_FIELD_PATH = os.getenv("DIGITAP_STATUS_FIELD_PATH", "status")
STATUS_CODE_PATH = os.getenv("DIGITAP_STATUS_CODE_PATH", "code")
STATUS_READY_VALUES = [
    v.strip().lower()
    for v in os.getenv("DIGITAP_STATUS_READY_VALUES", "success,completed,ready,data_ready,dataready,aafidatareadysuccess").split(",")
    if v.strip()
]
# Real retrievereport shape: banks[].accounts[].current_balance / .currency
BALANCE_PATH = os.getenv("DIGITAP_BALANCE_PATH", "banks.0.accounts.0.current_balance")
CURRENCY_PATH = os.getenv("DIGITAP_CURRENCY_PATH", "banks.0.accounts.0.currency")


class DigitapError(Exception):
    """Raised for transport-level failures (the per-call dict carries API errors)."""


def dig(obj, path: str):
    """Read a nested value by dotted path. None if any segment is absent."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _safe_json(resp: httpx.Response):
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {"_raw_text": resp.text}


def _call(kind: str, path: str, body: dict, auth) -> dict:
    result = {"kind": kind, "endpoint": path, "request": body,
              "http_status": None, "response": None, "ok": False, "error": None}
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            resp = client.post(
                BASE_URL + path, json=body, auth=auth,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        result["http_status"] = resp.status_code
        result["response"] = _safe_json(resp)
        result["ok"] = resp.status_code < 400
        if not result["ok"]:
            result["error"] = f"HTTP {resp.status_code}"
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
    return result


# ---------------------------------------------------------------------------
# The three calls
# ---------------------------------------------------------------------------
def initiate(main_txn_id: str) -> dict:
    if MOCK:
        return {"kind": "initiate", "endpoint": INITIATE_PATH,
                "request": {"main_txn_id": main_txn_id}, "http_status": 200, "ok": True, "error": None,
                "response": {"status": "success", "code": "AAFIPullRequestSuccess",
                             "msg": "We have requested FI to prepare data.",
                             "txn_id": f"CHILD-{main_txn_id}", "main_txn_id": main_txn_id}}
    return _call("initiate", INITIATE_PATH, {"main_txn_id": main_txn_id}, (INIT_USER, INIT_PASS))


def status(child_txn_id: str) -> dict:
    if MOCK:
        return {"kind": "status", "endpoint": STATUS_PATH,
                "request": {"request_id": child_txn_id}, "http_status": 200, "ok": True, "error": None,
                "response": {"status": "success", "code": "DataReady", "request_id": child_txn_id}}
    return _call("status", STATUS_PATH, {"request_id": child_txn_id}, (DATA_USER, DATA_PASS))


def _mock_report_response(child_txn_id: str) -> dict:
    """Mock retrievereport payload. Prefers the real-data report in
    mock_report.py (built from an actual AA report); falls back to a synthetic
    multi-month generator if that module isn't present."""
    try:
        import mock_report
        return mock_report.build(child_txn_id)
    except Exception:  # noqa: BLE001 — fall back to the synthetic generator
        pass
    from datetime import date, timedelta

    dr = [("Auto Loan", "NACH TVSCreditServicesLtd AP3108CP0003030 CNRB7031001261013122"),
          ("Bills & Utilities", "UPI/DR/Jio Recharge/YESB/IRECT@ybl"),
          ("Transfer to 434-3@axl", "UPI/DR/NAGA LAKS/CNRB/434-3@axl/Payment"),
          ("Medical", "UPI/DR/MEDARI NA/YESB/79778@ybl/Payment")]
    cr = [("Transfer in", "UPI/CR/NAGA LAKS/CNRB/434-3@ybl/Payment"),
          ("Loan Disbursed", "INET-IMPS-CR/MUTHOOTFIN/AXIS BANK/IMPS"),
          ("Investment Income", "NACH IOCL LPG SUBSIDY")]
    txns = []
    base = date(2026, 6, 24)
    bal = 32000.0
    for i in range(36):
        d = base - timedelta(days=i * 5)
        credit = (i % 3 == 0)
        amt = round(1500 + (i * 317) % 8000, 2) if credit else -round(150 + (i * 223) % 5000, 2)
        cat, narr = (cr[i % len(cr)] if credit else dr[i % len(dr)])
        bal = round(bal - amt, 2)
        txns.append({"date": d.isoformat(), "amount": amt, "balance": f"{abs(bal):.2f}",
                     "category": cat, "narration": f"{narr} //{1000000 + i}",
                     "transaction_timestamp": d.isoformat() + "T10:00:00"})

    overall = {
        "Avg EOD Balance last 3 months": 6111.48, "Min Balance Last 1 month": 1.95,
        "Total No. of EMI bounces in last 3 months": 0.0, "Recommended Date Range for NACH": "31-2",
        "Employment Type": "Self-Employed", "Amount of Credit Transactions in last 6 months": 184063.58,
    }
    months = {
        "April 2026": {"Closing Balance": 15001.16, "Total Amount of Credit Transactions": 12271.0,
                       "Total Amount of Debit Transactions": 25500.0, "Total No. of EMI / loan payments": 0.0, "Min EOD Balance": 4000.16},
        "May 2026": {"Closing Balance": 10017.16, "Total Amount of Credit Transactions": 14284.0,
                     "Total Amount of Debit Transactions": 19268.0, "Total No. of EMI / loan payments": 0.0, "Min EOD Balance": 17.16},
        "June 2026": {"Closing Balance": 2.95, "Total Amount of Credit Transactions": 3440.79,
                      "Total Amount of Debit Transactions": 13455.0, "Total No. of EMI / loan payments": 1.0, "Min EOD Balance": 1.95},
    }
    loans = [
        {"amount": 95369.0, "balance": "99369.16", "date": "2026-01-11", "category": "Loan Disbursed", "narration": "INET-IMPS-CR/TVSCREDITS/AXIS BANK"},
        {"amount": -4440.0, "balance": "8561.16", "date": "2026-05-04", "category": "Auto Loan", "narration": "NACH TVSCreditServicesLtd"},
        {"amount": -2500.0, "balance": "7536.16", "date": "2026-06-02", "category": "Loan & EMI Repayment", "narration": "UPI/DR/BAJAJ FIN/topay@hdfcbank"},
        {"amount": -4440.0, "balance": "3092.16", "date": "2026-06-03", "category": "Auto Loan", "narration": "NACH TVSCreditServicesLtd"},
    ]
    fraud = [
        {"type": "Equal Credit Debit", "dg_bdtin_code": "BDTIN_0002", "result": "applicable", "transactions": [{}, {}, {}, {}]},
        {"type": "Discontinuity in Credits", "dg_bdtin_code": "BDTIN_0023", "result": "applicable", "transactions": [{}, {}]},
        {"type": "Suspicious ATM Withdrawals", "dg_bdtin_code": "BDTIN_0010", "result": "not_applicable", "transactions": []},
    ]
    return {
        "status": "success", "txn_id": child_txn_id, "source_of_data": "accountaggregator",
        "multiple_accounts_found": "no",
        "banks": [{
            "bank": "Canara Bank",
            "accounts": [{
                "customer_info": {"holders": [{"name": "D MADHU"}]},
                "current_balance": "1500.00", "currency": "INR",
                "balance_date_time": "2026-06-27T09:53:31",
                "account_status": "ACTIVE", "ifsc_code": "CNRB0013392",
                "account_number": "XXXXXXXXX5020", "account_type": "SAVINGS",
                "transactions": txns,
                "analysis_data": {"Overall": overall, **months},
                "loan_analysis": loans,
                "fraud_analysis": fraud,
            }],
        }],
    }


def retrieve(child_txn_id: str) -> dict:
    if MOCK:
        return {"kind": "retrieve", "endpoint": RETRIEVE_PATH,
                "request": {"txn_id": child_txn_id}, "http_status": 200, "ok": True, "error": None,
                "response": _mock_report_response(child_txn_id)}
    return _call("retrieve", RETRIEVE_PATH, {"txn_id": child_txn_id}, (DATA_USER, DATA_PASS))


def request_consent(ref: dict) -> dict:
    """Request a new AA consent from the customer. `ref` carries the loan/account
    identifiers. Returns a call-result whose response has the consent handle/URL.
    Mapping is config-driven; swap in the real consent curl when available."""
    if MOCK:
        from datetime import date, timedelta
        tail = str(ref.get("account_ref") or "XXXX")[-4:]
        handle = f"CH-{tail}-{abs(hash(str(ref))) % 100000:05d}"
        return {"kind": "consent", "endpoint": CONSENT_PATH, "request": ref,
                "http_status": 200, "ok": True, "error": None,
                "response": {"status": "success", "consent_handle": handle,
                             "consent_url": f"https://aa.digitap.ai/consent/{handle}",
                             "consent_status": "PENDING",
                             "consent_expiry": (date.today() + timedelta(days=365)).isoformat()}}
    return _call("consent", CONSENT_PATH, ref, (INIT_USER, INIT_PASS))


def consent_of(consent_result: dict) -> dict:
    """Read the consent handle / URL / status / expiry out of a consent result."""
    resp = consent_result.get("response") or {}
    return {
        "handle": dig(resp, "consent_handle") or dig(resp, "consent_id"),
        "url": dig(resp, "consent_url") or dig(resp, "redirect_url"),
        "status": dig(resp, "consent_status") or dig(resp, "status"),
        "expiry": dig(resp, "consent_expiry"),
        "error": consent_result.get("error"),
    }


# ---------------------------------------------------------------------------
# Response readers
# ---------------------------------------------------------------------------
def child_id_of(initiate_result: dict):
    return dig(initiate_result.get("response") or {}, CHILD_ID_PATH)


def is_ready(status_result: dict) -> bool:
    resp = status_result.get("response") or {}
    val = str(dig(resp, STATUS_FIELD_PATH) or "").lower()
    code = str(dig(resp, STATUS_CODE_PATH) or "").lower()
    return val in STATUS_READY_VALUES or code in STATUS_READY_VALUES


def extract_balance(retrieve_result: dict):
    """Return (balance: float|None, currency: str|None) from a retrieve result."""
    resp = retrieve_result.get("response") or {}
    bal = dig(resp, BALANCE_PATH)
    cur = dig(resp, CURRENCY_PATH)
    try:
        bal = float(bal) if bal is not None else None
    except (TypeError, ValueError):
        bal = None
    return bal, cur


def parse_report(raw_response: dict) -> dict:
    """Flatten a retrievereport payload into account info + DPD signals + recent
    transactions for the UI report viewer. Reads the first bank/account."""
    resp = raw_response or {}
    bank = dig(resp, "banks.0") or {}
    acct = dig(resp, "banks.0.accounts.0") or {}
    overall = dig(acct, "analysis_data.Overall") or {}

    txns = []
    for t in (acct.get("transactions") or []):
        amt = t.get("amount")
        try:
            amt = float(amt)
        except (TypeError, ValueError):
            amt = None
        txns.append({
            "date": t.get("date"),
            "narration": t.get("narration"),
            "category": t.get("category"),
            "amount": amt,
            "type": "CREDIT" if (amt or 0) > 0 else "DEBIT",
            "balance": t.get("balance"),
        })
    txns.sort(key=lambda x: x.get("date") or "", reverse=True)  # newest first

    # Monthly summary (every analysis_data entry except the Overall* aggregates)
    monthly = []
    for k, v in (acct.get("analysis_data") or {}).items():
        if k.startswith("Overall") or not isinstance(v, dict):
            continue
        monthly.append({
            "month": k,
            "closing": v.get("Closing Balance"),
            "credits": v.get("Total Amount of Credit Transactions"),
            "debits": v.get("Total Amount of Debit Transactions"),
            "emi": v.get("Total No. of EMI / loan payments"),
            "min_eod": v.get("Min EOD Balance"),
        })

    # Loan activity
    loans = []
    for l in (acct.get("loan_analysis") or []):
        amt = l.get("amount")
        try:
            amt = float(amt)
        except (TypeError, ValueError):
            amt = None
        loans.append({"date": l.get("date"), "narration": l.get("narration"),
                      "category": l.get("category"), "amount": amt, "balance": l.get("balance")})
    loans.sort(key=lambda x: x.get("date") or "", reverse=True)

    # Fraud flags that actually triggered
    fraud = [{"type": f.get("type"), "code": f.get("dg_bdtin_code"),
              "hits": len(f.get("transactions") or [])}
             for f in (acct.get("fraud_analysis") or [])
             if str(f.get("result")).lower() == "applicable"]

    return {
        "account": {
            "bank": bank.get("bank") or acct.get("bank"),
            "account_number": acct.get("account_number"),
            "account_type": acct.get("account_type"),
            "ifsc": acct.get("ifsc_code"),
            "holder": dig(acct, "customer_info.holders.0.name"),
            "current_balance": acct.get("current_balance"),
            "currency": acct.get("currency"),
            "balance_as_of": acct.get("balance_date_time"),
            "status": acct.get("account_status"),
        },
        "signals": {
            "Avg EOD balance (3m)": overall.get("Avg EOD Balance last 3 months"),
            "Min balance (1m)": overall.get("Min Balance Last 1 month"),
            "EMI bounces (3m)": overall.get("Total No. of EMI bounces in last 3 months"),
            "Recommended NACH range": overall.get("Recommended Date Range for NACH"),
            "Employment type": overall.get("Employment Type"),
            "Credits (6m)": overall.get("Amount of Credit Transactions in last 6 months"),
        },
        "transactions": txns,
        "txn_count": len(txns),
        "monthly": monthly,
        "loans": loans,
        "fraud": fraud,
        "raw": resp,
    }
