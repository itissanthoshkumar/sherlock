"""Ingest a LOS (Engrow) Flow-A portfolio sync into the app's data model.

los_client.sync_portfolio() returns normalized rows (loan + repayment account +
LOS consent). This module lands them so the monthly cycle + Customer 360 read
real data:

  * los_portfolio  — one snapshot doc per loan (what checker.lookup_* reads in
    LOOKUP_SOURCE=los mode).
  * ppdata masters — loan + repayment bank_account (source LOS), so the
    Customers directory + 360 immediately show real people.
  * consent blend — if the LOS A5 consent is ACTIVE (and unexpired) the account
    is marked DIGITAL/pullable with its main_txn_id and mirrored into the unified
    consent registry (source=LOS); otherwise it stays LOS/not-linked for the
    Mongo-side PCPL overlay (checker._apply_consent_overlay) to fill.

Idempotent: a re-sync replaces the prior snapshot for the same triggered_by.
"""
from datetime import date, datetime

import mongostore as store

MASTER = store.MASTER


def _mask(account_no):
    s = str(account_no or "").strip()
    if len(s) <= 4:
        return s or None
    return "X" * (len(s) - 4) + s[-4:]


def _consent_active(consent: dict) -> bool:
    """ACTIVE status and not past expiry."""
    if not consent or str(consent.get("status") or "").upper() != "ACTIVE":
        return False
    exp = consent.get("consent_expiry")
    if not exp:
        return True
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(exp)[:len(fmt) + 2], fmt).date() >= date.today()
        except ValueError:
            continue
    return True  # unparseable expiry -> treat as live rather than silently drop


def ingest_portfolio(rows, triggered_by="los-sync") -> dict:
    rows = list(rows or [])
    # A failed LIVE sync (login/list error) returns rows=[]; clearing the snapshot on that
    # would wipe the last-good portfolio. Preserve it and report the skip.
    if not rows:
        return {"loans": 0, "aa_linked_via_los": 0, "triggered_by": triggered_by,
                "skipped": "empty portfolio — prior snapshot preserved"}
    db = store._db()
    prior = {d.get("loan_id"): d for d in store.los_portfolio_all()}  # backfill for failed legs
    # INSERT-BEFORE-DELETE (audit #14): upsert the new batch first (tagged), prune stale
    # rows only after every row landed — a mid-loop failure (network blip, bad value) then
    # leaves the old snapshot intact instead of gutting the portfolio to a partial book.
    # Same hardening bulk_replace_lms_presentment already has.
    import uuid
    batch = uuid.uuid4().hex

    n_loans = n_linked = 0
    for row in rows:
        loan_id = row.get("loan_id")
        if not loan_id:
            continue
        # A transient bank/consent-leg failure returns empty repay/consent — don't clobber a
        # previously-synced good value; carry it forward from the pre-clear snapshot.
        pri = prior.get(loan_id) or {}
        if row.get("bank_ok") is False and not (row.get("repayment") or {}):
            row = dict(row, repayment=pri.get("repayment") or {})
        if row.get("consent_ok") is False and not (row.get("los_consent") or {}):
            row = dict(row, los_consent=pri.get("los_consent") or {})
        app_no = row.get("los_application_no")
        repay = row.get("repayment") or {}
        consent = row.get("los_consent") or {}
        active = _consent_active(consent)

        # 1) snapshot (what the cycle reads) — batch-tagged; stale rows pruned after the loop
        store.save_los_portfolio(row, triggered_by=triggered_by, sync_batch=batch)

        # 2) loan master
        db[MASTER].update_one({"type": "loan", "lms_loan_id": loan_id}, {"$set": {
            "type": "loan", "lms_loan_id": loan_id, "loan_id": loan_id,
            "los_application_no": app_no, "customer_name": row.get("customer_name"),
            "emi_amount": row.get("emi"), "product": row.get("product"),
            "tenure": row.get("tenure"), "amount_sanc": row.get("amount_sanc"),
            "source": "LOS", "updated_at": store._now(),
        }}, upsert=True)

        # 3) repayment bank_account master (blend the LOS consent) — only when a real account
        #    exists, and keyed DETERMINISTICALLY per loan (never embedding acc_no, which drifts
        #    raw-vs-masked across syncs and forks duplicate docs — the 94-for-38 state).
        acc_no = repay.get("account_no")
        if acc_no or repay.get("uid"):
            key = repay.get("uid") or (str(loan_id) + ":repayment")
            ba = {
                "type": "bank_account", "bank_account_uid": key, "lms_loan_id": loan_id, "loan_id": loan_id,
                "los_application_no": app_no, "bank_name": repay.get("bank_name"),
                "account_ref": _mask(acc_no), "account_number": acc_no, "ifsc": repay.get("ifsc"),
                "account_holder_name": repay.get("holder_name"), "is_repayment": True,
                "source": "DIGITAL" if active else "LOS", "aa_enabled": bool(active),
                "main_txn_id": consent.get("main_txn_id") if active else None,
                "consent_id": consent.get("consent_id") if active else None,
                "consent_expiry": consent.get("consent_expiry") if active else None,
                "consent_status": "ACTIVE" if active else None,
                "updated_at": store._now(),
            }
            db[MASTER].update_one({"type": "bank_account", "bank_account_uid": key}, {"$set": ba}, upsert=True)

        # 4) mirror an active LOS consent into the unified registry (source=LOS)
        if active and consent.get("consent_id"):
            n_linked += 1
            db[MASTER].update_one({"type": "consent", "handle": consent.get("consent_id")}, {"$set": {
                "type": "consent", "handle": consent.get("consent_id"), "source": "LOS",
                "lms_loan_id": loan_id, "loan_id": loan_id, "los_application_no": app_no,
                "bank_name": repay.get("bank_name"), "account_ref": _mask(acc_no), "url": None,
                "status": "ACTIVE", "expiry": consent.get("consent_expiry"),
                "updated_at": store._now(),
            }}, upsert=True)
        n_loans += 1

    # every new row landed — NOW drop rows the fresh batch didn't write
    pruned = store.prune_los_portfolio(triggered_by, batch)
    return {"loans": n_loans, "aa_linked_via_los": n_linked, "pruned": pruned,
            "triggered_by": triggered_by}
