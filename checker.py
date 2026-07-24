"""Core DPD orchestration.

Flow for a loan account id + EMI:

  1. lookup_accounts()  — read-only SQL -> every bank account on the loan, each
     with bank name, repayment flag, source, fetched date and parent txn id.
  2. AA availability    — an account is AA-enabled when source == 'AA' AND it was
     fetched strictly after the cutoff (default 2026-04-14) AND has a parent txn id.
  3. For every AA-enabled account, Digitap initiate(main_txn_id) -> child txn id;
     a pull row is created in INITIATED state.
  4. ~10s later the pull is auto-processed (statuscheck -> retrieve -> balance ->
     verdict). The UI can also trigger refresh_pull() manually at any time.

Everything (input, resolved accounts, each pull, and every API request/response)
is persisted via store for a complete audit trail.
"""
import os
import re
import threading
from datetime import date, datetime
from pathlib import Path

import pymysql
import pymysql.cursors

import dbconfig
import digitap
import mongostore as store

LOOKUP_SQL_FILE = Path(os.getenv("LOOKUP_SQL_FILE") or (Path(__file__).resolve().parent / "lookup.sql"))
PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

# An AA bank account must have been fetched strictly after this date.
AA_FETCH_CUTOFF = date.fromisoformat(os.getenv("AA_FETCH_CUTOFF", "2026-04-14"))
# A balance >= EMI + buffer is "sufficient".
DPD_BUFFER = float(os.getenv("DPD_BUFFER", "0"))
# Seconds to wait after initiate before the FIRST auto statuscheck + retrieve.
AUTO_DELAY = float(os.getenv("DIGITAP_AUTO_DELAY", "10"))
# A live periodic AA pull is NOT ready instantly — the FIP prepares data (60s+), so the
# first poll almost always finds it PROCESSING. The auto-processor RE-ARMS itself every
# AA_POLL_INTERVAL until every pull is terminal or the budget (AA_MAX_POLLS) is spent, then
# times the stragglers out to a terminal state so the run ALWAYS finalizes and the cycle can
# never wedge at COLLECTING. See flow-audit BLOCKER (single-poll no re-arm).
AA_POLL_INTERVAL = float(os.getenv("AA_POLL_INTERVAL", "20"))     # seconds between re-polls
AA_MAX_POLLS = int(os.getenv("AA_MAX_POLLS", "24"))               # ~8 min total budget

# Which `source` value(s) mark an Account-Aggregator account (comma-separated,
# case-insensitive). The engrow data uses DIGITAL (=AA) / MANUAL / blank.
AA_SOURCE_VALUES = [v.strip().upper() for v in os.getenv("AA_SOURCE_VALUES", "DIGITAL").split(",") if v.strip()]
# Lookup column carrying the Digitap parent txn id (main_txn_id). Blank = the
# query has no such column yet, so AA accounts are listed but cannot be pulled.
LOOKUP_TXN_COLUMN = os.getenv("LOOKUP_TXN_COLUMN", "main_txn_id").strip().lower()

# AA consent: tag as nearing-expiry when within this many days of lapsing.
CONSENT_EXPIRY_WARN_DAYS = int(os.getenv("CONSENT_EXPIRY_WARN_DAYS", "30"))
LOOKUP_CONSENT_ID_COLUMN = os.getenv("LOOKUP_CONSENT_ID_COLUMN", "consent_id").strip().lower()
LOOKUP_CONSENT_EXPIRY_COLUMN = os.getenv("LOOKUP_CONSENT_EXPIRY_COLUMN", "consent_expiry").strip().lower()

LOOKUP_MOCK = os.getenv("LOOKUP_MOCK", "false").lower() in ("1", "true", "yes")

# Where the portfolio + accounts come from: "mock" (deterministic demo),
# "los" (the Engrow Flow-A snapshot synced via the Portfolio Sync tool) or "sql"
# (direct LMS MySQL). Defaults from LOOKUP_MOCK for back-compat; an explicit
# LOOKUP_SOURCE always wins (so the smoke test's LOOKUP_MOCK=true stays on mock).
LOOKUP_SOURCE = os.getenv("LOOKUP_SOURCE", "").strip().lower() or ("mock" if LOOKUP_MOCK else "sql")

# Guardrail: max Digitap initiate attempts per bank account per calendar month
# (statuscheck/retrieve never consume attempts). Enforced across cycles, retries
# and ad-hoc checks via the shared aa_attempts ledger.
MAX_INITIATES_PER_MONTH = int(os.getenv("AA_MAX_INITIATES_PER_MONTH", "4"))

# Mock only: seconds after a consent request before the "customer" completes the
# AA journey (consent turns ACTIVE and the account becomes pullable). <0 disables.
CONSENT_MOCK_COMPLETE_DELAY = float(os.getenv("CONSENT_MOCK_COMPLETE_DELAY", "15"))

# Callbacks invoked (with run_id) after a run reaches DONE — used by the monthly
# cycle to classify its item once all pulls land. Failures are swallowed so a
# classification bug can never break the check pipeline itself.
RUN_DONE_HOOKS = []

# Pull states that will not change again.
TERMINAL_PULL_STATES = ("RETRIEVED", "FAILED", "CAPPED")


class CheckError(Exception):
    """A recoverable, user-facing problem (loan not found, etc.)."""


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------
def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "y", "yes", "true", "t") if v is not None else False


def _as_date(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%d/%m/%y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:len(fmt) + 2], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def is_aa_enabled(acct: dict) -> bool:
    src = str(acct.get("source") or "").strip().upper()
    fetched = _as_date(acct.get("fetched_at"))
    return src in AA_SOURCE_VALUES and fetched is not None and fetched > AA_FETCH_CUTOFF


def consent_status(acct: dict) -> str:
    """NOT_LINKED (no AA consent) / EXPIRED / NEARING_EXPIRY / ACTIVE."""
    if not acct.get("aa_enabled"):
        return "NOT_LINKED"
    exp = _as_date(acct.get("consent_expiry"))
    if exp is None:
        return "ACTIVE"
    days = (exp - date.today()).days
    if days < 0:
        return "EXPIRED"
    if days <= CONSENT_EXPIRY_WARN_DAYS:
        return "NEARING_EXPIRY"
    return "ACTIVE"


# ---------------------------------------------------------------------------
# SQL lookup
# ---------------------------------------------------------------------------
def _build_query(sql: str, values: dict):
    params: list = []

    def repl(m):
        params.append(values.get(m.group(1)))
        return "%s"

    return PLACEHOLDER_RE.sub(repl, sql), params


def _map_row(row: dict) -> dict:
    """Map one engrow SQL row to the normalized account dict the app uses."""
    r = {(k or "").lower(): v for k, v in row.items()}
    repay = str(r.get("is_repayment_account") or r.get("is_repayment") or "").strip().upper()
    return {
        "bank_name": r.get("bank_name"),
        "account_ref": r.get("masked_account_number") or r.get("account_number"),
        "is_repayment": repay in ("YES", "Y", "1", "TRUE", "T"),
        "source": r.get("source"),
        "fetched_at": r.get("fetched_at") or r.get("created_date"),
        "main_txn_id": (r.get(LOOKUP_TXN_COLUMN) if LOOKUP_TXN_COLUMN else None),
        "los_application_no": r.get("los_application_no") or r.get("application_no"),
        "context_uid": r.get("context_uid") or r.get("application_uid") or r.get("uid"),
        "bank_account_uid": r.get("bank_account_uid"),
        "branch_name": r.get("branch_name"),
        "account_type": r.get("account_type"),
        "ifsc": r.get("ifsc"),
        "account_holder_name": r.get("account_holder_name"),
        "emi_amount": r.get("emi_amount"),  # loan-level EMI, fetched from LMS
        "consent_id": (r.get(LOOKUP_CONSENT_ID_COLUMN) if LOOKUP_CONSENT_ID_COLUMN else None),
        "consent_expiry": (r.get(LOOKUP_CONSENT_EXPIRY_COLUMN) if LOOKUP_CONSENT_EXPIRY_COLUMN else None),
        "raw_row": r,
    }


# ---------------------------------------------------------------------------
# Mock portfolio (12 loans) — drives the monthly-cycle demo. Repayment-account
# balances come from mock_report._variant_balance("CHILD-<txn>") and were chosen
# together with the EMIs so buckets are guaranteed:
#   Cushioned  PP-2001 (23,027/4,440)  PP-2003 (21,277/6,500)  PP-2008 (22,076/8,600)
#   Stretched  PP-2002 (6,122/3,900)   PP-2004 (19,416/12,500)
#   Shortfall  PP-2005 (20,975/24,000) PP-2006 (6,134/7,400)   PP-2007 (15,297/21,500)
#   No signal  PP-2009 consent expired · PP-2010 not linked ·
#              PP-2011 AA but no parent txn id · PP-2012 repay account not AA
# ---------------------------------------------------------------------------
MOCK_PORTFOLIO = [
    {"loan_id": "PP-2001", "customer_name": "D Madhu",          "emi_amount": 4440,  "npa_parked": False, "branch": "Dharmavaram", "state": "Andhra Pradesh"},
    {"loan_id": "PP-2002", "customer_name": "K Lakshmi Devi",   "emi_amount": 3900,  "npa_parked": False, "branch": "Anantapur",   "state": "Andhra Pradesh"},
    {"loan_id": "PP-2003", "customer_name": "S Ramesh Babu",    "emi_amount": 6500,  "npa_parked": False, "branch": "Dharmavaram", "state": "Andhra Pradesh"},
    {"loan_id": "PP-2004", "customer_name": "P Anitha",         "emi_amount": 12500, "npa_parked": False, "branch": "Kurnool",     "state": "Andhra Pradesh"},
    {"loan_id": "PP-2005", "customer_name": "M Venkatesh",      "emi_amount": 24000, "npa_parked": False, "branch": "Hyderabad",   "state": "Telangana"},
    {"loan_id": "PP-2006", "customer_name": "B Srinivasulu",    "emi_amount": 7400,  "npa_parked": True,  "branch": "Kurnool",     "state": "Andhra Pradesh"},
    {"loan_id": "PP-2007", "customer_name": "G Nagaraju",       "emi_amount": 21500, "npa_parked": False, "branch": "Hyderabad",   "state": "Telangana"},
    {"loan_id": "PP-2008", "customer_name": "T Swapna",         "emi_amount": 8600,  "npa_parked": False, "branch": "Kadapa",      "state": "Andhra Pradesh"},
    {"loan_id": "PP-2009", "customer_name": "V Prasad Rao",     "emi_amount": 4440,  "npa_parked": False, "branch": "Anantapur",   "state": "Andhra Pradesh"},
    {"loan_id": "PP-2010", "customer_name": "N Chandra Sekhar", "emi_amount": 2400,  "npa_parked": False, "branch": "Kurnool",     "state": "Andhra Pradesh"},
    {"loan_id": "PP-2011", "customer_name": "R Padmavathi",     "emi_amount": 5600,  "npa_parked": False, "branch": "Anantapur",   "state": "Andhra Pradesh"},
    {"loan_id": "PP-2012", "customer_name": "Y Obulesu",        "emi_amount": 3100,  "npa_parked": False, "branch": "Hyderabad",   "state": "Telangana"},
]

# Per-loan bank accounts: (bank, ifsc, is_repayment, source, main_txn_id, consent_id, consent_expiry)
_PORTFOLIO_ACCOUNTS = {
    "PP-2001": [("Canara Bank", "CNRB0013802", True,  "DIGITAL", "MT-PP2001-R", "CONSENT-PP2001",  "2027-03-15"),
                ("Union Bank",  "UBIN0530450", False, "DIGITAL", "MT-PP2001-S", "CONSENT-PP2001B", "2027-01-10")],
    "PP-2002": [("State Bank Of India", "SBIN0006108", True, "DIGITAL", "MT-PP2002-R", "CONSENT-PP2002", "2027-04-20"),
                ("HDFC Bank", "HDFC0000123", False, "MANUAL", None, None, None)],
    "PP-2003": [("Canara Bank", "CNRB0013392", True, "DIGITAL", "MT-PP2003-R", "CONSENT-PP2003", "2026-08-02")],
    "PP-2004": [("Union Bank", "UBIN0559105", True, "DIGITAL", "MT-PP2004-R", "CONSENT-PP2004", "2027-02-11")],
    "PP-2005": [("HDFC Bank", "HDFC0001987", True,  "DIGITAL", "MT-PP2005-R", "CONSENT-PP2005",  "2027-05-30"),
                ("Canara Bank", "CNRB0011240", False, "DIGITAL", "MT-PP2005-S", "CONSENT-PP2005B", "2027-05-30")],
    "PP-2006": [("State Bank Of India", "SBIN0001537", True, "DIGITAL", "MT-PP2006-R", "CONSENT-PP2006", "2026-12-01")],
    "PP-2007": [("Canara Bank", "CNRB0002214", True, "DIGITAL", "MT-PP2007-R", "CONSENT-PP2007", "2027-03-09")],
    "PP-2008": [("Union Bank", "UBIN0810355", True, "DIGITAL", "MT-PP2008-R", "CONSENT-PP2008", "2026-11-18")],
    "PP-2009": [("Canara Bank", "CNRB0008830", True, "DIGITAL", "MT-PP2009-R", "CONSENT-PP2009", "2026-05-30")],
    "PP-2010": [("State Bank Of India", "SBIN0020734", True, "MANUAL", None, None, None)],
    "PP-2011": [("HDFC Bank", "HDFC0004452", True,  "DIGITAL", None,           "CONSENT-PP2011",  "2027-01-25"),
                ("Canara Bank", "CNRB0016675", False, "DIGITAL", "MT-PP2011-S", "CONSENT-PP2011B", "2027-01-25")],
    "PP-2012": [("Union Bank", "UBIN0532101", True, "MANUAL", None, None, None),
                ("State Bank Of India", "SBIN0013309", False, "DIGITAL", "MT-PP2012-S", "CONSENT-PP2012", "2027-06-14")],
}


def _portfolio_account(loan_id, seq, spec, emi):
    import zlib
    bank, ifsc, repay, source, txn, consent, expiry = spec
    tail = 1000 + (zlib.crc32(f"{loan_id}:{seq}".encode()) % 9000)
    return {
        "los_application_no": str(16000 + (zlib.crc32(loan_id.encode()) % 900)),
        "context_uid": f"CTX-{loan_id}", "emi_amount": emi,
        "bank_name": bank, "account_ref": f"XXXXXX{tail}", "is_repayment": repay,
        "source": source, "fetched_at": "2026-06-20 09:15:00", "main_txn_id": txn,
        "bank_account_uid": f"{loan_id}-BA{seq}", "branch_name": None, "ifsc": ifsc,
        "consent_id": consent, "consent_expiry": expiry,
    }


def lookup_portfolio(source=None) -> list:
    """All cycle-eligible loans (non-closed + designated NPA-parked cases) with
    customer name and due amount. `source` overrides the env default — the CRO's
    monthly Sherlock Check always passes source="lms" (the presentment report)."""
    src = (source or LOOKUP_SOURCE).lower()
    if src == "lms":
        # This month's presentment snapshot = the borrowers due for collection.
        # Dedup by account_id (keep last) so a duplicated presentment row can't create two
        # cycle items -> two billed pulls + double attempt burn for the same loan.
        by_acct = {}
        for d in store.lms_presentment_all():
            if d.get("account_id"):
                by_acct[d["account_id"]] = d
        return [{"loan_id": d.get("account_id"), "customer_name": d.get("customer_name"),
                 "emi_amount": d.get("emi_amount"), "npa_parked": False,
                 "branch": d.get("branch_name"),
                 "demand_amount": d.get("demand_amount"), "demand_date": d.get("demand_date"),
                 "od_days_num": d.get("od_days_num")}
                for d in by_acct.values()]
    if src == "mock":
        # The demo loan book is a TEST fixture. Serving it against a real store would
        # seed fabricated borrowers into live data, so it is refused outright rather
        # than silently returned. There is no runtime mock mode on this platform.
        import db as _dbmod
        if not _dbmod.MONGO_MOCK:
            raise RuntimeError(
                "LOOKUP_SOURCE=mock is a test-only fixture and cannot be used against a "
                "real database. Set LOOKUP_SOURCE=lms (or los) in .env.")
        return [dict(l) for l in MOCK_PORTFOLIO]
    if src == "los":
        # Read the synced Engrow snapshot (no per-loan HTTP inside the cycle).
        return [{"loan_id": d.get("loan_id"), "customer_name": d.get("customer_name"),
                 "emi_amount": d.get("emi"), "npa_parked": bool(d.get("npa_parked")),
                 "los_application_no": d.get("los_application_no")}
                for d in store.los_portfolio_all()]
    sql_file = os.getenv("PORTFOLIO_SQL_FILE")
    if not sql_file:
        raise CheckError("Portfolio lookup not configured for live LMS (set PORTFOLIO_SQL_FILE)")
    sql = Path(sql_file).read_text()
    conn = pymysql.connect(**dbconfig.mysql_kwargs())
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        r = {(k or "").lower(): v for k, v in row.items()}
        out.append({"loan_id": r.get("loan_id") or r.get("account_id"),
                    "customer_name": r.get("customer_name") or r.get("applicant_name"),
                    "emi_amount": r.get("emi_amount"),
                    "npa_parked": _truthy(r.get("npa_parked"))})
    return out


def _apply_consent_overlay(loan_id: str, rows: list, source=None) -> list:
    """Mock-mode: a PCPL-acquired ACTIVE consent upgrades the account it was
    requested for — the real-world equivalent of the customer completing the AA
    journey, after which the account is linked and pullable. Without this, the
    static mock rows would keep showing EXPIRED/NOT_LINKED forever.

    NEVER on a real-vendor path: when _via_aa_live(source) the pull goes to the REAL
    Digitap API, and a mock-minted consent + fabricated MT-<loan> txn would fire a
    billed initiate for a borrower with NO valid registry consent — the consent
    manager is the ONLY eligibility source in the CRO flow. See audit #2."""
    if _via_aa_live(source):
        return rows
    for i, row in enumerate(rows):
        try:
            c = store.pcpl_consent_for(loan_id, row.get("account_ref"))
        except Exception:  # noqa: BLE001
            c = None
        if not c or c.get("status") != "ACTIVE":
            continue
        exp = _as_date(c.get("expiry"))
        if exp is not None and exp < date.today():
            continue
        row["consent_id"] = c.get("handle")
        row["consent_expiry"] = c.get("expiry")
        row["source"] = "DIGITAL"
        row["fetched_at"] = store._now()
        if not row.get("main_txn_id"):
            row["main_txn_id"] = f"MT-{loan_id}-NEW{i + 1}"
    return rows


def _mask_acc(n):
    s = str(n or "").strip()
    return ("X" * (len(s) - 4) + s[-4:]) if len(s) > 4 else (s or None)


def _los_consent_active(consent) -> bool:
    """LOS A5 consent counts as live when ACTIVE and not past its expiry."""
    if str((consent or {}).get("status") or "").upper() != "ACTIVE":
        return False
    exp = _as_date((consent or {}).get("consent_expiry"))
    return exp is None or exp >= date.today()


def _los_account_row(loan_id, doc):
    """Normalize a los_portfolio snapshot doc into the account dict the app uses.
    The blended LOS consent (A5) decides AA-linkage: an active LOS consent makes
    the account DIGITAL + pullable; otherwise it stays LOS/not-linked and the
    Mongo-side PCPL overlay (below) may still upgrade it."""
    repay = doc.get("repayment") or {}
    consent = doc.get("los_consent") or {}
    active = _los_consent_active(consent)
    acc_no = repay.get("account_no")
    return {
        "bank_name": repay.get("bank_name"), "account_ref": _mask_acc(acc_no),
        "account_number": acc_no, "is_repayment": True,
        "source": "DIGITAL" if active else "LOS",
        "fetched_at": store._now(),
        "main_txn_id": consent.get("main_txn_id") if active else None,
        "los_application_no": doc.get("los_application_no"), "context_uid": doc.get("uid"),
        "bank_account_uid": repay.get("uid") or (str(loan_id) + ":" + str(acc_no)),
        "branch_name": None, "ifsc": repay.get("ifsc"),
        "account_holder_name": repay.get("holder_name"), "emi_amount": doc.get("emi"),
        "consent_id": consent.get("consent_id") if active else None,
        "consent_expiry": consent.get("consent_expiry") if active else None,
    }


def cm_state(cm):
    """Review state of a loan's effective consent -> (state, reason).
    ELIGIBLE          — ACTIVE PERIODIC + mandate txn, covering today
    NOT_PULLABLE      — a consent EXISTS but can't drive a periodic pull
                        (ONETIME only / mandate txn missing / non-ACTIVE status)
    EXPIRED           — consent lapsed
    NO_CONSENT        — nothing in the registry"""
    if not cm:
        return "NO_CONSENT", "no consent in the registry"
    for f in ("expiry", "end_date"):
        v = cm.get(f)
        d = _as_date(v)
        if d is None and str(v or "").strip():
            # FAIL CLOSED: a date is present but unparseable (e.g. '31-12-2025') — treating
            # it as "no expiry" let lapsed consents stay ELIGIBLE forever. See audit #8.
            return "NOT_PULLABLE", f"unparseable {f} '{str(v)[:12]}' — fix the date format"
        if d is not None and d < date.today():
            return "EXPIRED", f"lapsed {str(v)[:10]}"
    sd = _as_date(cm.get("start_date"))
    if sd is not None and sd > date.today():
        # A post-dated PERIODIC consent is not yet in force — pulling it now would
        # bill Digitap for a mandate that isn't legally effective. See bug (_cm_effective_of).
        return "NOT_PULLABLE", f"not yet effective — starts {str(cm.get('start_date'))[:10]}"
    st = str(cm.get("status") or "").upper()
    if st == "EXPIRED":
        return "EXPIRED", "status EXPIRED"
    if st != "ACTIVE":
        return "NOT_PULLABLE", f"status {st or 'unknown'}"
    if str(cm.get("consent_type") or "PERIODIC").upper() != "PERIODIC":
        return "NOT_PULLABLE", "ONETIME consent only — PERIODIC needed for monthly pulls"
    if not cm.get("main_txn_id"):
        return "NOT_PULLABLE", "mandate txn id missing"
    return "ELIGIBLE", None


def _cm_active(cm) -> bool:
    """Pullable = ELIGIBLE per cm_state (ACTIVE PERIODIC + txn, covering today)."""
    return cm_state(cm)[0] == "ELIGIBLE"


def _lms_account_row(loan_id):
    """Build the repayment-account row for an LMS borrower from the consent
    manager (the ONLY eligibility source in the CRO flow) + the presentment row
    (EMI, names)."""
    pres = store.lms_for_account(loan_id) or {}
    cm = store.cm_for(loan_id)
    active = _cm_active(cm)
    consent_state = cm_state(cm)[0]  # ELIGIBLE / EXPIRED / NOT_PULLABLE / NO_CONSENT — carried
                                     # onto the row so classify_item can give an honest reason
    exp_state = bool(cm) and str(cm.get("status") or "").upper() in ("ACTIVE", "EXPIRED") \
        and not active and (cm.get("main_txn_id") or str(cm.get("status")).upper() == "EXPIRED")
    return {
        "bank_name": (cm or {}).get("bank_name") or "Repayment A/C (NACH)",
        "account_ref": loan_id, "is_repayment": True,
        # DIGITAL + fresh fetched_at => aa_enabled; EXPIRED consent surfaces as
        # DIGITAL+expired (consent CTA); no consent row at all => MANUAL/not linked.
        "source": "DIGITAL" if (active or exp_state) else "MANUAL",
        "fetched_at": store._now(),
        "main_txn_id": cm.get("main_txn_id") if active else None,
        "los_application_no": (cm or {}).get("los_application_no"),
        "context_uid": None, "bank_account_uid": "CM-" + str(loan_id),
        "branch_name": pres.get("branch_name"), "ifsc": None,
        "account_holder_name": pres.get("customer_name"),
        "emi_amount": pres.get("emi_amount"),
        "consent_id": (cm or {}).get("consent_id") if cm else None,
        "consent_expiry": (cm or {}).get("expiry") if cm else None,
        "consent_state": consent_state,
    }


def lookup_accounts(loan_id: str, source=None) -> list:
    """Return every bank account on the loan (one normalized dict per account)."""
    src = (source or LOOKUP_SOURCE).lower()
    if src == "lms":
        return _apply_consent_overlay(loan_id, [_lms_account_row(loan_id)], source=src)
    if src == "los":
        doc = store.los_account_for(loan_id)
        if not doc:
            return []
        # Union with any PCPL consent we chased ourselves (via the Live Pull tool).
        return _apply_consent_overlay(loan_id, [_los_account_row(loan_id, doc)], source=src)
    if src == "mock":
        spec = _PORTFOLIO_ACCOUNTS.get(loan_id)
        if spec:
            emi = next(l["emi_amount"] for l in MOCK_PORTFOLIO if l["loan_id"] == loan_id)
            rows = [_portfolio_account(loan_id, i + 1, s, emi) for i, s in enumerate(spec)]
            return _apply_consent_overlay(loan_id, rows)
        # Any other id: engrow-shaped sample, 2 DIGITAL (=AA, with parent txn id) + 1 MANUAL.
        base = {"los_application_no": "16419", "context_uid": "853570393230533120", "emi_amount": 4440}
        # consent_expiry: one AA consent nearing expiry, one comfortably active;
        # the MANUAL account has no AA consent (Fetch consent applies there).
        return [
            {**base, "bank_name": "Canara Bank", "account_ref": "XXXXXXXXX9618", "is_repayment": True,
             "source": "DIGITAL", "fetched_at": "2026-06-13 10:33:44", "main_txn_id": "da47c4ccc",
             "bank_account_uid": "853574222365843968", "branch_name": "Dharmavaram", "ifsc": "CNRB0013802",
             "consent_id": "CONSENT-AAA111", "consent_expiry": "2026-07-12"},
            {**base, "bank_name": "Canara Bank", "account_ref": "XXXXXXXXX9618", "is_repayment": False,
             "source": "DIGITAL", "fetched_at": "2026-06-13 10:33:45", "main_txn_id": "da47c4ddd",
             "bank_account_uid": "853576749291397632", "branch_name": "Dharmavaram", "ifsc": "CNRB0013802",
             "consent_id": "CONSENT-BBB222", "consent_expiry": "2027-05-01"},
            {**base, "bank_name": "State Bank Of India", "account_ref": "XXXXX6072", "is_repayment": False,
             "source": "MANUAL", "fetched_at": "2026-06-12 10:42:22", "main_txn_id": None,
             "bank_account_uid": "853198042592778240", "branch_name": "Bukkarayasamudram", "ifsc": "SBIN0006108",
             "consent_id": None, "consent_expiry": None},
        ]
    sql = LOOKUP_SQL_FILE.read_text()
    query, params = _build_query(sql, {"loan_id": loan_id})
    conn = pymysql.connect(**dbconfig.mysql_kwargs())
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    return [_map_row(row) for row in rows]


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------
def decide(balance, emi):
    if balance is None or emi is None:
        return "INDETERMINATE"
    return "SUFFICIENT" if float(balance) >= (float(emi) + DPD_BUFFER) else "INSUFFICIENT"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _current_month() -> str:
    return date.today().strftime("%Y-%m")


def account_key_of(loan_id, acct: dict) -> str:
    """Ledger/master key for a physical bank account (same key _upsert_master uses)."""
    return acct.get("bank_account_uid") or (str(loan_id) + ":" + str(acct.get("account_ref")))


def _via_aa_live(source) -> bool:
    """LMS-source (CRO Sherlock Check) runs pull through the REAL Digitap
    periodic API (aa_live: initiate_periodic -> retrieve); everything else stays
    on the legacy digitap client (mock cycle demo + smoke test)."""
    return (source or "").lower() == "lms" or os.getenv("AA_PULL_VIA", "").lower() == "aa_live"


def _aa_live_initiate(main_txn_id, loan_id):
    """initiate via aa_live with the daily billing cap enforced. Returns the
    digitap-shaped (res, child) pair so the pull bookkeeping is unchanged."""
    import aa_live
    if not aa_live._use_mock(None):  # a real (billed) call — atomically reserve a cap slot
        cap = int(os.getenv("AA_LIVE_MAX_CALLS_PER_DAY", "100"))
        if not store.reserve_live_call(cap):  # atomic check-and-increment (no check-then-act race)
            return {"kind": "aa_initiate", "ok": False, "mode": "live",
                    "error": f"Daily LIVE Digitap call cap reached ({cap}/{cap})"}, None
    res = aa_live.initiate_periodic(main_txn_id, live=None)  # None -> env default
    store.log_aa_call(res, by="sherlock-check", loan_id=loan_id)
    child = aa_live.initiate_of(res).get("txn_id")
    return res, child


def _initiate_for_account(run_id, account_id, acct, loan_id, cycle_ctx=None, source=None) -> str:
    """Initiate the Digitap pull for one AA-enabled account, enforcing the
    monthly attempt cap. Returns 'PULL' | 'CAPPED' | 'EXPIRED' | 'NO_TXN'."""
    cycle_id = (cycle_ctx or {}).get("cycle_id")
    if acct.get("consent_status") == "EXPIRED":
        return "EXPIRED"  # no live consent -> cannot pull; no attempt consumed
    if not acct.get("main_txn_id"):
        return "NO_TXN"   # AA account but no parent txn id yet -> cannot pull
    month = _current_month()
    key = account_key_of(loan_id, acct)
    # Atomically RESERVE the monthly slot before dispatching (no check-then-act race:
    # two concurrent initiates for the same account can no longer both read 3<4 and
    # both bill the vendor — the sibling of reserve_live_call). See audit #17.
    if not store.reserve_monthly_attempt(key, month, MAX_INITIATES_PER_MONTH):
        used = store.attempts_used(key, month)
        pull_id = store.add_pull(run_id, account_id, acct.get("bank_name"), acct.get("is_repayment"),
                                 acct.get("main_txn_id"), None, "CAPPED",
                                 f"Monthly AA attempt cap reached ({used}/{MAX_INITIATES_PER_MONTH})",
                                 account_key=key, loan_id=loan_id,
                                 los_application_no=acct.get("los_application_no"),
                                 fetch_type="PERIODIC")
        store.record_attempt(month, key, loan_id, acct.get("bank_name"), acct.get("account_ref"),
                             run_id=run_id, pull_id=pull_id, cycle_id=cycle_id,
                             allowed=False, reason="CAP_REACHED")
        return "CAPPED"
    if _via_aa_live(source):
        res, child = _aa_live_initiate(acct["main_txn_id"], loan_id)
    else:
        res = digitap.initiate(acct["main_txn_id"])
        child = digitap.child_id_of(res)
    dispatched = bool(res.get("ok") and child)  # a real initiate that produced a child txn
    pull_status = "INITIATED" if dispatched else "FAILED"
    err = None if dispatched else (res.get("error") or "initiate returned no child id")
    pull_id = store.add_pull(run_id, account_id, acct.get("bank_name"), acct.get("is_repayment"),
                             acct.get("main_txn_id"), child, pull_status, err, account_key=key,
                             loan_id=loan_id, los_application_no=acct.get("los_application_no"),
                             # the cycle always pulls periodically (initiate_periodic -> retrieve PERIODIC)
                             fetch_type="PERIODIC")
    # Only a dispatched initiate consumes one of the 4 monthly attempts. A daily-cap
    # block never reached the vendor, and a transient failure produced no usable pull —
    # charging either would let a borrower burn the month with zero real pulls. See bug K1.
    if dispatched:
        store.record_attempt(month, key, loan_id, acct.get("bank_name"), acct.get("account_ref"),
                             run_id=run_id, pull_id=pull_id, cycle_id=cycle_id, allowed=True)
    else:
        store.release_monthly_attempt(key, month)  # never reached the vendor — give the slot back
        cap_hit = "cap reached" in str(res.get("error") or "").lower()
        store.record_attempt(month, key, loan_id, acct.get("bank_name"), acct.get("account_ref"),
                             run_id=run_id, pull_id=pull_id, cycle_id=cycle_id, allowed=False,
                             reason="DAILY_CAP" if cap_hit else "INITIATE_FAILED")
    store.log_api(run_id, pull_id, res, cycle_id=cycle_id)
    return "PULL"


def start_check(loan_id: str, emi_override=None, cycle_ctx=None, source=None) -> dict:
    """Resolve accounts, read the loan's EMI from the LMS lookup (or an optional
    override), determine AA, initiate pulls for all AA accounts, and schedule the
    auto statuscheck+retrieve. Returns the run snapshot immediately.
    cycle_ctx = {"cycle_id", "cycle_item_id"} when run as part of a monthly cycle.
    source: mock | los | lms — lms is the CRO flow (presentment + consent manager
    + real aa_live periodic pulls); stored on the run so refresh/auto keep it."""
    cycle_ctx = cycle_ctx or {}
    # Resolve the source ONCE — the run's stored source (which routes the retrieve) and
    # the initiate path must never diverge: a None param with LOOKUP_SOURCE=lms used to
    # initiate via the mock digitap client but retrieve via the real billed aa_live,
    # wedging the pull at PROCESSING forever. See audit #1 (seam).
    source = (source or LOOKUP_SOURCE)
    run_id = store.create_run(loan_id, cycle_id=cycle_ctx.get("cycle_id"),
                              cycle_item_id=cycle_ctx.get("cycle_item_id"),
                              source=source)
    try:
        accounts = lookup_accounts(loan_id, source=source)
    except Exception as e:  # noqa: BLE001
        store.finalize_run(run_id, aa_available=False, account_count=0, aa_count=0,
                           status="ERROR", error=f"{type(e).__name__}: {e}")
        return store.get_run(run_id)

    if not accounts:
        store.finalize_run(run_id, aa_available=False, account_count=0, aa_count=0,
                           status="ERROR", error=f"No bank accounts found for loan '{loan_id}'")
        return store.get_run(run_id)

    # EMI from LMS (same on every account row) unless an explicit override is given.
    emi = emi_override
    if emi in (None, ""):
        try:
            emi = float(accounts[0].get("emi_amount"))
        except (TypeError, ValueError):
            emi = None
    store.set_run_emi(run_id, emi)

    aa_count = 0
    pulls_created = 0
    expired = 0
    for acct in accounts:
        acct["aa_enabled"] = is_aa_enabled(acct)
        acct["consent_status"] = consent_status(acct)
        account_id = store.add_account(run_id, acct)
        if not acct["aa_enabled"]:
            continue
        aa_count += 1
        outcome = _initiate_for_account(run_id, account_id, acct, loan_id, cycle_ctx, source=source)
        if outcome in ("PULL", "CAPPED"):
            pulls_created += 1  # a pull row exists either way (CAPPED is terminal)
        elif outcome == "EXPIRED":
            expired += 1

    aa_available = aa_count > 0
    if aa_count == 0:
        status = "AA_NOT_AVAILABLE"
    elif pulls_created == 0:
        status = "CONSENT_EXPIRED" if expired else "NO_TXN_ID"
    else:
        status = "PENDING"
    store.finalize_run(run_id, aa_available=aa_available, account_count=len(accounts),
                       aa_count=aa_count, status=status,
                       los_application_no=accounts[0].get("los_application_no"))

    if pulls_created and AUTO_DELAY >= 0:
        threading.Timer(AUTO_DELAY, _auto_process_run, args=[run_id]).start()

    return store.get_run(run_id)


def request_consent(account_id: int) -> dict:
    """Request a fresh AA consent from the customer for an account that isn't
    linked (or whose consent is lapsing). Returns the consent handle + URL."""
    acct = store.get_account(account_id)
    if not acct:
        raise CheckError(f"Account {account_id} not found")
    ref = {
        "loan_id": acct.get("loan_id"),
        "context_uid": acct.get("context_uid"),
        "bank_name": acct.get("bank_name"),
        "account_ref": acct.get("account_ref"),
        "ifsc": acct.get("ifsc"),
    }
    res = digitap.request_consent(ref)
    store.log_api(acct.get("run_id"), None, res)
    consent = digitap.consent_of(res)
    store.save_consent(acct.get("run_id"), account_id, consent)
    if consent.get("handle"):
        store.update_account(account_id, consent_status="CONSENT_REQUESTED",
                             consent_id=consent.get("handle"), consent_expiry=consent.get("expiry"))
        # Mock: simulate the customer completing the AA journey a little later.
        if digitap.MOCK and CONSENT_MOCK_COMPLETE_DELAY >= 0:
            threading.Timer(CONSENT_MOCK_COMPLETE_DELAY, _mock_complete_consent,
                            args=[consent.get("handle"), account_id]).start()
    return {"account_id": account_id, "ok": bool(res.get("ok")), **consent}


def request_consent_for_loan(loan_id: str) -> dict:
    """Loan-level consent CTA (Customers directory / 360): pick the account
    that needs consent — repayment first — from the loan's latest resolved
    accounts, and request it."""
    run = store._db().checks.find_one({"loan_id": loan_id}, {"_id": 0, "id": 1}, sort=[("id", -1)])
    if not run:
        raise CheckError(f"No check or cycle has resolved '{loan_id}' yet — run one first")
    accounts = store.run_accounts(run["id"])

    def needs(a):
        return a.get("consent_status") in (None, "NOT_LINKED", "EXPIRED", "NEARING_EXPIRY")

    target = next((a for a in accounts if a.get("is_repayment") and needs(a)), None) \
        or next((a for a in accounts if needs(a)), None)
    if not target:
        raise CheckError("Every account on this loan already has an active consent")
    return request_consent(target["id"])


def _mock_complete_consent(handle, account_id):
    """Mock stand-in for the customer approving the consent via the SMS/WhatsApp
    link. Marks the consent ACTIVE in the registry; the next lookup/pull for the
    loan sees the account as linked (via _apply_consent_overlay)."""
    try:
        store.activate_consent(handle)
        store.update_account(account_id, consent_status="ACTIVE")
    except Exception:  # noqa: BLE001
        pass


def _auto_process_run(run_id: int, attempt: int = 1):
    run = store.get_run(run_id)
    if not run:
        return
    for pull in run["pulls"]:
        if pull["status"] in ("INITIATED", "PROCESSING"):
            process_pull(pull["id"])  # statuscheck + (if ready) retrieve; else stays PROCESSING
    run = store.get_run(run_id) or {}
    pending = [p for p in (run.get("pulls") or []) if p["status"] in ("INITIATED", "PROCESSING")]
    if not pending:
        _maybe_mark_done(run_id)
        return
    # Still waiting on the FIP to prepare data. RE-ARM the poll (bounded) so a live pull that
    # becomes ready at minute 3 is actually harvested — the old single-shot Timer left it
    # PROCESSING forever and wedged the whole cycle at COLLECTING. When the budget is spent,
    # time the stragglers out to a terminal FAILED so the run finalizes (borrower -> NO_DATA,
    # honest) instead of hanging. Mock is ready on the first pass, so this never re-arms there.
    if attempt < AA_MAX_POLLS and AA_POLL_INTERVAL > 0:
        threading.Timer(AA_POLL_INTERVAL, _auto_process_run, args=[run_id, attempt + 1]).start()
    else:
        waited = int(AA_MAX_POLLS * AA_POLL_INTERVAL)
        for p in pending:
            store.update_pull(p["id"], status="FAILED",
                              error=f"AA report not ready after ~{waited}s — timed out")
        _maybe_mark_done(run_id)


def _balance_today(raw, hint_last4=None, hint_ifsc=None):
    """Balance as on today from a retrieved AA report: the mandate (repayment)
    account's current balance. The pull knows exactly which account it initiated
    for, so pass its last4/ifsc as the resolver hint. Only fall back to the first
    account when the report is genuinely single-account — never guess accounts[0]
    on a multi-account report (that reads the wrong balance). See bug K3."""
    import aa_report
    model = aa_report.parse(raw)
    accounts = model.get("accounts") or []
    if not accounts:
        return None
    mandate = aa_report.resolve_mandate(model, hint_last4=hint_last4, hint_ifsc=hint_ifsc)
    key = (mandate or {}).get("account_key")
    acct = next((a for a in accounts if a.get("key") == key), None)
    if acct is None:
        acct = accounts[0] if len(accounts) == 1 else None
    if acct is None:
        return None  # multi-account report we couldn't resolve -> NO_DATA, not a wrong guess
    return acct.get("current_balance")


def _process_pull_aa_live(pull, run, emi, cycle_id, child):
    """CRO-flow pull processing. Gate the *billed* retrieve behind a cheap
    statuscheck when the consent's request_id is on record (avoids paying for a
    full periodic report just to learn it isn't ready); otherwise fall back to
    retrieve-as-probe. Either way the retrieve honours the daily billing cap so a
    run can't blow AA_LIVE_MAX_CALLS_PER_DAY. See bug (_process_pull_aa_live) + K2."""
    import aa_live
    pull_id = pull["id"]
    loan_id = run.get("loan_id") if run else None

    # Cheap readiness gate (logged to the audit trail, NOT the billing counter, so it
    # doesn't consume the retrieve budget — mirrors the legacy digitap.status() gate).
    req_id = (store.cm_for(loan_id) or {}).get("request_id") if loan_id else None
    if req_id:
        st = aa_live.status_check(req_id, child, live=None)
        # Ledger EVERY Digitap request (audit #10) — aa_status is excluded from the
        # billed live_today count by policy (free readiness poll), so this keeps the
        # "every request is recorded" invariant without consuming cap slots.
        store.log_aa_call(st, by="sherlock-check", loan_id=loan_id)
        store.log_api(pull["run_id"], pull_id, st, cycle_id=cycle_id)
        if not (st.get("ok") and aa_live.status_of(st).get("ready")):
            store.update_pull(pull_id, status="PROCESSING",
                              error=st.get("error") or "report not ready yet")
            return store.get_run(pull["run_id"])

    # The retrieve is billed — atomically reserve a cap slot; if full, defer (stay PROCESSING).
    if not aa_live._use_mock(None):
        cap = int(os.getenv("AA_LIVE_MAX_CALLS_PER_DAY", "100"))
        if not store.reserve_live_call(cap):
            store.update_pull(pull_id, status="PROCESSING",
                              error="daily LIVE Digitap call cap reached — retrieve deferred")
            return store.get_run(pull["run_id"])

    rep = aa_live.retrieve_report(child, "PERIODIC", live=None)
    store.log_aa_call(rep, by="sherlock-check", loan_id=loan_id)
    store.log_api(pull["run_id"], pull_id, rep, cycle_id=cycle_id)
    report = rep.get("response") or {}
    if not rep.get("ok") or not report.get("banks"):
        store.update_pull(pull_id, status="PROCESSING",
                          error=rep.get("error") or "report not ready yet")
        return store.get_run(pull["run_id"])
    hint = next((a for a in (run.get("accounts") or [])
                 if a.get("id") == pull.get("account_id")), {}) if run else {}
    try:
        balance = _balance_today(report, hint_last4=hint.get("account_ref"),
                                 hint_ifsc=hint.get("ifsc"))
    except Exception as e:  # noqa: BLE001
        store.update_pull(pull_id, status="PROCESSING", error=f"parse failed: {type(e).__name__}: {e}")
        return store.get_run(pull["run_id"])
    import json as _json
    store.update_pull(pull_id, status="RETRIEVED", available_balance=balance, currency="INR",
                      decision=decide(balance, emi), error=None,
                      raw_report_json=_json.dumps(report))
    _maybe_mark_done(pull["run_id"])
    return store.get_run(pull["run_id"])


def process_pull(pull_id: int) -> dict:
    """statuscheck the pull's child txn; if ready, retrieve + score it."""
    pull = store.get_pull(pull_id)
    if not pull:
        raise CheckError(f"Pull {pull_id} not found")
    if pull["status"] in TERMINAL_PULL_STATES:
        return store.get_run(pull["run_id"])

    run = store.get_run(pull["run_id"])
    emi = run.get("emi_amount") if run else None
    cycle_id = run.get("cycle_id") if run else None
    child = pull["child_txn_id"]

    if _via_aa_live(run.get("source") if run else None):
        return _process_pull_aa_live(pull, run, emi, cycle_id, child)

    st = digitap.status(child)
    store.log_api(pull["run_id"], pull_id, st, cycle_id=cycle_id)
    if not st.get("ok"):
        store.update_pull(pull_id, status="PROCESSING", error=st.get("error"))
        return store.get_run(pull["run_id"])
    if not digitap.is_ready(st):
        store.update_pull(pull_id, status="PROCESSING", error=None)
        return store.get_run(pull["run_id"])

    rep = digitap.retrieve(child)
    store.log_api(pull["run_id"], pull_id, rep, cycle_id=cycle_id)
    if not rep.get("ok"):
        store.update_pull(pull_id, status="PROCESSING", error=rep.get("error"))
        return store.get_run(pull["run_id"])

    balance, currency = digitap.extract_balance(rep)
    store.update_pull(pull_id, status="RETRIEVED", available_balance=balance, currency=currency,
                      decision=decide(balance, emi), error=None, raw_report_json=rep.get("response"))
    _maybe_mark_done(pull["run_id"])
    return store.get_run(pull["run_id"])


def _maybe_mark_done(run_id: int):
    run = store.get_run(run_id)
    if run and run["pulls"] and all(p["status"] in TERMINAL_PULL_STATES for p in run["pulls"]):
        store.finalize_run(run_id, aa_available=bool(run["aa_available"]),
                           account_count=run["account_count"], aa_count=run["aa_count"],
                           status="DONE", error=run.get("error"))
        for hook in RUN_DONE_HOOKS:
            try:
                hook(run_id)
            except Exception:  # noqa: BLE001
                pass  # cycle classification must never break the check pipeline
