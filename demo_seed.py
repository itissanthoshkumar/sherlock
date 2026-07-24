"""Demo data seeder — makes the app a complete end-to-end story.

seed_history() backfills three finished cycles (2026-04/05/06) for the mock
portfolio so every PRD surface has data: coverage growing month over month
(58% -> 67% -> 75%) as the consent program lands, bucket trends, worklist
dispositions by the demo team users, and NACH outcomes per month (via the
same deterministic simulator the live cycle uses). Idempotent — months that
already have a cycle are skipped.

reset_current_month() clears the CURRENT month's cycles, items, outcomes,
dispositions and attempt ledger so the live demo can be replayed from a
clean slate (audit trails in checks/pulls/api_logs are kept).

Both are exposed as manual "Run now" jobs on the dashboard.
"""
import zlib
from datetime import date

import checker
import cycle as cycle_mod
import mongostore as store

# Baseline repayment-cover ratios (the live July numbers); jittered per month.
BASE_RATIO = {"PP-2001": 5.19, "PP-2002": 1.57, "PP-2003": 3.27, "PP-2004": 1.55,
              "PP-2005": 0.87, "PP-2006": 0.83, "PP-2007": 0.71, "PP-2008": 2.57}

# Consent-program narrative: reasons for the no-signal loans, per month.
# April: 5 gaps (58% coverage) -> May: 4 (67%) -> June: 4 but 2011's consent
# is captured (75%) -> July live: 75%.
NO_SIGNAL = {
    "2026-04": {"PP-2008": ("REPAY_NOT_AA", "NOT_LINKED"), "PP-2009": ("CONSENT_EXPIRED", "EXPIRED"),
                "PP-2010": ("REPAY_NOT_AA", "NOT_LINKED"), "PP-2011": ("REPAY_NOT_AA", "NOT_LINKED"),
                "PP-2012": ("REPAY_NOT_AA", "NOT_LINKED")},
    "2026-05": {"PP-2009": ("CONSENT_EXPIRED", "EXPIRED"), "PP-2010": ("REPAY_NOT_AA", "NOT_LINKED"),
                "PP-2011": ("REPAY_NOT_AA", "NOT_LINKED"), "PP-2012": ("REPAY_NOT_AA", "NOT_LINKED")},
    "2026-06": {"PP-2009": ("CONSENT_EXPIRED", "EXPIRED"), "PP-2010": ("REPAY_NOT_AA", "NOT_LINKED"),
                "PP-2011": ("NO_TXN_ID", "ACTIVE"), "PP-2012": ("REPAY_NOT_AA", "NOT_LINKED")},
}
MONTHS = ["2026-04", "2026-05", "2026-06"]
DISPO_MONTHS = ("2026-05", "2026-06")  # worklists "launched" in May
DISPO_CHOICES = ["CONTACTED", "PTP", "PAID", "NO_RESPONSE"]


def _jitter_ratio(loan_id, month):
    base = BASE_RATIO.get(loan_id)
    if base is None:
        return None
    factor = 0.78 + (zlib.crc32(f"{loan_id}:{month}:ratio".encode()) % 45) / 100.0
    return round(base * factor, 4)


def _bucket_for(ratio):
    return "COMFORT" if ratio >= 2.0 else ("WATCH" if ratio >= 1.0 else "SHORTFALL")


def seed_history() -> str:
    made = []
    for month in MONTHS:
        if store.cycle_for_month(month):
            continue
        cid = store.create_cycle(month, "demo-seed")
        gaps = NO_SIGNAL[month]
        counts = {"COMFORT": 0, "WATCH": 0, "SHORTFALL": 0, "NO_DATA": 0}
        expired = sum(1 for r, cs in gaps.values() if cs == "EXPIRED")
        not_linked = sum(1 for r, cs in gaps.values() if cs == "NOT_LINKED")

        for loan in checker.MOCK_PORTFOLIO:
            lid = loan["loan_id"]
            item_id = store.add_cycle_item(cid, loan)
            repay_bank = checker._PORTFOLIO_ACCOUNTS[lid][0][0]
            if lid in gaps:
                reason, consent = gaps[lid]
                store.update_cycle_item(item_id, status="DONE", bucket="NO_DATA",
                                        bucket_reason=reason, consent_status=consent,
                                        repay_bank=repay_bank)
                counts["NO_DATA"] += 1
                bucket, ratio, balance = "NO_DATA", None, None
            else:
                ratio = _jitter_ratio(lid, month)
                bucket = _bucket_for(ratio)
                balance = round(ratio * loan["emi_amount"], 2)
                store.update_cycle_item(item_id, status="DONE", bucket=bucket, bucket_reason="OK",
                                        ratio=ratio, repay_balance=balance, repay_bank=repay_bank,
                                        agg_balance=balance, agg_ratio=ratio, consent_status="ACTIVE")
                counts[bucket] += 1

            store.save_processed({
                "cycle_id": cid, "cycle_item_id": item_id, "loan_id": lid, "month": month,
                "run_id": None, "emi_amount": loan["emi_amount"],
                "bucket": bucket, "bucket_reason": "OK" if bucket != "NO_DATA" else gaps[lid][0],
                "effective_bucket": bucket,
                "repay": {"bank_name": repay_bank, "account_ref": None,
                          "balance": balance, "currency": "INR" if balance else None, "ratio": ratio},
                "aggregate": {"total_balance": balance, "ratio": ratio, "accounts": []},
                "analytics": None, "tags": [],
            })

            # Worklist dispositions once the teams "launched" (May onward).
            if month in DISPO_MONTHS and bucket in ("WATCH", "SHORTFALL"):
                status = DISPO_CHOICES[zlib.crc32(f"{lid}:{month}:disp".encode()) % len(DISPO_CHOICES)]
                by = "telecaller1" if bucket == "WATCH" else "field1"
                ptp = f"{month}-03" if status == "PTP" else None
                remarks = {"CONTACTED": "Spoke to customer, aware of the due date",
                           "PTP": "Will deposit before the 3rd",
                           "PAID": "Topped up at the branch",
                           "NO_RESPONSE": "Two attempts, no answer"}[status]
                entry = store.add_disposition(cid, item_id, lid, bucket, status, remarks, ptp, by)
                ts = f"{month}-02 11:{10 + zlib.crc32(lid.encode()) % 45:02d}:00"
                store._db().dispositions.update_one({"id": entry["id"]}, {"$set": {"created_at": ts}})
                store.update_cycle_item(item_id, disposition={
                    "status": status, "remarks": remarks, "ptp_date": ptp, "by": by, "at": ts})

        ok = 12 - expired - not_linked
        store.update_cycle(cid, status="DONE",
                           created_at=f"{month}-01 07:00:12", finished_at=f"{month}-01 07:24:40",
                           totals={"eligible": 12, "items_created": 12,
                                   "repay_consent_ok": ok, "repay_consent_expired": expired,
                                   "repay_not_linked": not_linked,
                                   "pulls_initiated": (12 - len(gaps)) + 2,
                                   "repay_pulls": 12 - len(gaps), "pulls_blocked": 0},
                           bucket_counts=counts)
        # timestamps on items, then NACH results via the live simulator (day 4)
        store._db().cycle_items.update_many(
            {"cycle_id": cid, "disposition": None},
            {"$set": {"created_at": f"{month}-01 07:01:00", "updated_at": f"{month}-01 07:20:00"}})
        s = cycle_mod.simulate_outcomes(cid)
        made.append(f"{month}: cycle {cid}, buckets {counts['COMFORT']}/{counts['WATCH']}"
                    f"/{counts['SHORTFALL']}/{counts['NO_DATA']}, bounce {s['bounce_rate']}%")
    return " · ".join(made) if made else "skipped — history months already seeded"


def sync_portfolio_masters() -> str:
    """Backfill branch/state (and names) from MOCK_PORTFOLIO onto existing loan
    masters and cycle items — keeps data seeded before those fields existed
    filterable. Idempotent; runs at every startup."""
    db = store._db()
    n = 0
    for loan in checker.MOCK_PORTFOLIO:
        lid = loan["loan_id"]
        db[store.MASTER].update_one(
            {"type": "loan", "lms_loan_id": lid},
            {"$set": {"branch": loan.get("branch"), "state": loan.get("state"),
                      "customer_name": loan.get("customer_name")}})
        n += db.cycle_items.update_many(
            {"loan_id": lid, "branch": None},
            {"$set": {"branch": loan.get("branch"), "state": loan.get("state")}}).modified_count
        n += db.cycle_items.update_many(
            {"loan_id": lid, "branch": {"$exists": False}},
            {"$set": {"branch": loan.get("branch"), "state": loan.get("state")}}).modified_count
    return f"portfolio masters synced ({n} item(s) backfilled)"


def backfill_scores() -> str:
    """Score any DONE items in the latest cycle that predate the scoring engine
    (classify_item is idempotent and now attaches risk_score). Startup-safe."""
    latest = store._db().cycles.find_one({"status": "DONE"}, {"_id": 0, "id": 1},
                                         sort=[("month", -1), ("id", -1)])
    if not latest:
        return "no finished cycle"
    n = 0
    for it in store.cycle_items(latest["id"]):
        if it.get("status") == "DONE" and it.get("risk_score") is None:
            try:
                cycle_mod.classify_item(it["id"])
                n += 1
            except Exception:  # noqa: BLE001
                pass
    return f"backfilled scores on {n} item(s) in cycle {latest['id']}"


def reset_current_month() -> str:
    """Clear the live month's demo artifacts so the cycle can be replayed.
    Keeps checks/pulls/api_logs (audit) and all history months."""
    month = date.today().strftime("%Y-%m")
    db = store._db()
    cycles = list(db.cycles.find({"month": month}, {"_id": 0, "id": 1}))
    ids = [c["id"] for c in cycles]
    n_items = db.cycle_items.delete_many({"cycle_id": {"$in": ids}}).deleted_count
    n_proc = db.processed_data.delete_many({"cycle_id": {"$in": ids}}).deleted_count
    n_out = db.nach_outcomes.delete_many({"cycle_id": {"$in": ids}}).deleted_count
    n_disp = db.dispositions.delete_many({"cycle_id": {"$in": ids}}).deleted_count
    db.cycles.delete_many({"month": month})
    n_att = db.aa_attempts.delete_many({"month": month}).deleted_count
    return (f"cleared {len(ids)} cycle(s) for {month}: {n_items} items, {n_proc} processed, "
            f"{n_out} outcomes, {n_disp} dispositions, {n_att} attempt entries — run a fresh cycle now")
