"""LOS consent sync — the pre-flight step of the CRO monthly cycle.

Fresh disbursals capture the AA consent in the LOS; this module copies it into
Sherlock's consent registry (the single source of truth) via a DIRECT LOS DB
query supplied by the user: pass the LMS loan/account id -> consent rows.

  * Query file: los_consent.sql ({{loan_id}} placeholder, same convention as
    lookup.sql). PENDING from the user — until it exists (or DB creds are set),
    live sync reports "not configured" and MOCK replays samples/los_consents.json.
  * Normalization is defensive (_map_consent_row): column names vary until the
    real query lands, so every field tries a list of aliases.
  * Rows land via store.upsert_cm_consent(source="LOS") — one row per consent,
    idempotent per (loan_id, consent_id).
"""
import json
import os
from pathlib import Path

import dbconfig
import mongostore as store

SQL_FILE = Path(os.getenv("LOS_CONSENT_SQL_FILE") or
                (Path(__file__).resolve().parent / "los_consent.sql"))
MOCK_DEFAULT = (os.getenv("LOS_CONSENT_MOCK", "true").lower() in ("1", "true", "yes"))
_SAMPLES = Path(__file__).resolve().parent / "samples"

import re

_PLACEHOLDER_RE = re.compile(r"\{\{\s*loan_id\s*\}\}")


def _sql_ready() -> bool:
    try:
        text = SQL_FILE.read_text()
    except OSError:
        return False
    live_lines = [l for l in text.splitlines() if l.strip() and not l.strip().startswith("--")]
    # Match the placeholder in the ACTUAL query (non-comment lines only) — else a {{loan_id}}
    # sitting in the SQL file's comment header falsely passes readiness and an unfiltered query
    # cross-attributes every consent to every loan. See bug (_sql_ready comment header).
    return bool(live_lines) and bool(_PLACEHOLDER_RE.search("\n".join(live_lines)))


def live_configured() -> bool:
    """A real sync needs the consent query AND a real LOS MySQL target (explicit DB_HOST /
    saved db_config.yaml). Credentials reuse the shared LOS creds via dbconfig, so only the
    DB host/port/name + the query are outstanding."""
    return _sql_ready() and dbconfig.has_connection()


def status_summary() -> dict:
    return {"mock_default": MOCK_DEFAULT, "live_configured": live_configured(),
            "sql_ready": _sql_ready(), "sql_file": str(SQL_FILE)}


def _use_mock(live) -> bool:
    # An EXPLICIT live=True must never silently fall back to replaying mock samples into the
    # real registry — when LOS isn't configured, sync_consents hits its SKIP branch instead.
    if live is None:
        return MOCK_DEFAULT
    return not live


def _first(r, keys):
    for k in keys:
        v = r.get(k)
        if v not in (None, ""):
            return v
    return None


def _iso(v):
    """Normalize a date to ISO yyyy-mm-dd so downstream string comparisons enforce expiry.
    Handles date/datetime objects, dd/mm/yyyy or dd-mm-yyyy (Indian format), and Excel serials
    — an unparsed serial/dd-mm expiry would otherwise evade both expiry gates."""
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()[:10]
    s = str(v).strip()
    if not s:
        return None
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)  # dd/mm/yyyy (Indian)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    if s.isdigit() and 20000 <= int(s) <= 80000:  # Excel date serial (1900 epoch)
        from datetime import date as _date, timedelta as _td
        return (_date(1899, 12, 30) + _td(days=int(s))).isoformat()
    return s[:10] if len(s) >= 10 else s


# mutable/date aliases the mapper understands; _present tracks which were ACTUALLY in the row
_ALIAS = {
    "consent_id": ["consent_id", "consent_handle", "handle", "consent_ref"],
    "consent_type": ["consent_type", "type", "fetch_type"],
    "main_txn_id": ["main_txn_id", "txn_id", "parent_txn_id", "mandate_txn_id"],
    "start_date": ["start_date", "from_date", "fi_start", "consent_start"],
    "end_date": ["end_date", "to_date", "fi_end", "consent_end"],
    "expiry": ["expiry", "consent_expiry", "expiry_date", "valid_till"],
    "status": ["status", "consent_status"],
}


def _map_consent_row(loan_id, row) -> dict:
    """Normalize one LOS consent row (defensive aliases until the real query fixes the
    column names). `_present` = the mutable fields whose source column actually existed, so
    an authoritative writer only clears columns that were really in the query.

    Missing/unrecognized status and type columns map to None — NOT to ACTIVE/PERIODIC.
    Defaulting them force-reactivated REVOKED consents and force-PERIODIC'd ONETIME ones
    whenever the live query's column names missed the alias list (the exact condition this
    mapper anticipates); upsert_cm_consent's omit-None behavior preserves the existing
    values instead. See audit #7."""
    r = {(k or "").strip().lower(): v for k, v in row.items()}
    present = {f for f, ks in _ALIAS.items() if any(k in r for k in ks)}
    ctype = str(_first(r, _ALIAS["consent_type"]) or "").upper() or None
    if ctype not in (None, "ONETIME", "PERIODIC"):
        ctype = None  # unknown vocabulary -> don't guess, preserve what the registry has
    status = str(_first(r, _ALIAS["status"]) or "").upper() or None
    return {
        "loan_id": str(loan_id),
        "consent_id": _first(r, _ALIAS["consent_id"]),
        "consent_type": ctype,
        "main_txn_id": _first(r, _ALIAS["main_txn_id"]),
        "start_date": _iso(_first(r, _ALIAS["start_date"])),
        "end_date": _iso(_first(r, _ALIAS["end_date"])),
        "expiry": _iso(_first(r, _ALIAS["expiry"])),
        "status": status,
        "customer_name": _first(r, ["customer_name", "applicant_name", "name"]),
        "mobile": _first(r, ["mobile", "contact_number", "phone"]),
        "_present": present,
    }


def _fetch_live(loan_ids):
    import re
    import pymysql
    import pymysql.cursors
    sql = SQL_FILE.read_text()
    out = {}
    conn = pymysql.connect(**dbconfig.mysql_kwargs())
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            for lid in loan_ids:
                params = []

                def repl(m):  # noqa: B023 — same convention as checker._build_query
                    params.append(lid)
                    return "%s"

                query = re.sub(r"\{\{\s*loan_id\s*\}\}", repl, sql)
                cur.execute(query, params)
                out[lid] = cur.fetchall() or []
    finally:
        conn.close()
    return out


def _fetch_mock(loan_ids):
    try:
        with open(_SAMPLES / "los_consents.json", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        data = {}
    return {lid: data.get(str(lid), []) for lid in loan_ids}


def sync_consents(loan_ids, live=None, by="preflight") -> dict:
    """Pre-flight: pull LOS consents for every loan due this month and upsert
    them into the registry (source=LOS). Returns counts + a ledger result."""
    loan_ids = [str(x).strip() for x in loan_ids if x]
    mock = _use_mock(live)
    if not mock and not live_configured():
        # Query/creds still pending: the step completes as a SKIP (legacy cases
        # rely on Sherlock-procured consents already in the registry) — never
        # silently replay mock data into the live registry.
        return {"ok": True, "mode": "skipped", "loans": len(loan_ids), "consents": 0,
                "loans_with_consent": 0,
                "note": "LOS consent query not configured yet (los_consent.sql + DB creds) — "
                        "0 copied; registry runs on Sherlock-procured consents"}
    try:
        raw = _fetch_mock(loan_ids) if mock else _fetch_live(loan_ids)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "mode": "mock" if mock else "live", "loans": len(loan_ids),
                "consents": 0, "error": f"{type(e).__name__}: {e}"}
    n = loans_hit = revoked = 0
    seen_by_loan = {}
    unknown_id_loans = set()  # a row with no consent handle -> identity unknown, can't reconcile
    for lid, rows in raw.items():
        if rows:
            loans_hit += 1
        seen = []
        for row in rows:
            c = _map_consent_row(lid, row)
            # Only treat as AUTHORITATIVE (able to clear fields / be reconciled) when a real
            # consent handle matched — a handle-less row can't safely null or revoke anything.
            saved = store.upsert_cm_consent(
                lid, main_txn_id=c["main_txn_id"], consent_id=c["consent_id"],
                status=c["status"], expiry=c["expiry"], source="LOS",
                customer_name=c["customer_name"], mobile=c["mobile"], by=by,
                consent_type=c["consent_type"], start_date=c["start_date"],
                end_date=c["end_date"], authoritative=bool(c["consent_id"]),
                clear_fields=c.get("_present"))
            if saved and saved.get("consent_id"):
                seen.append(saved["consent_id"])
            if not c["consent_id"]:
                unknown_id_loans.add(lid)
            n += 1
        seen_by_loan[lid] = seen
    # Reconcile (revoke vanished LOS consents) ONLY per-loan where THIS pull returned rows.
    # An empty per-loan result is indistinguishable from a JOIN/id-format miss — revoking on
    # it would mass-revoke (and null the mandate txns of) every loan the query failed to key,
    # e.g. all Excel-imported consents. A loan with rows CAN be reconciled: consents absent
    # from its non-empty result really are gone. Skip handle-less rows. Live sync only. Audit #13.
    if not mock and n:
        for lid, rows in raw.items():
            if lid in unknown_id_loans or not rows:
                continue
            revoked += store.cm_revoke_missing_los(lid, seen_by_loan.get(lid) or [], by=by)
    return {"ok": True, "mode": "mock" if mock else "live", "revoked": revoked,
            "loans": len(loan_ids), "loans_with_consent": loans_hit, "consents": n}
