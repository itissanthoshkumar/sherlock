"""Ingest an LMS (Encore) Flow-B presentment sync into the app's data model.

Kept deliberately isolated in the `lms_presentment` collection (its own dataset)
— these are DISBURSED loans due for collection, a different population than the
LOS DOCUMENT-EXECUTION apps, so we don't merge them into the LOS masters here.
Wiring LMS into Customer 360 / the monthly cycle is a follow-up once an
LOS<->LMS reconciliation exists.

Each row is keyed by the LMS `account_id` (the real loan id) and carries the
uniform join key (loan_id = account_id). Idempotent: a re-sync replaces the
prior snapshot for the same triggered_by.
"""
import mongostore as store


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def ingest_presentment(rows, triggered_by="lms-sync", mode=None, force=False) -> dict:
    # Dedup by account_id keep-last FIRST so the summary stats match what the store
    # actually persists — accumulating over raw duplicate rows overstated demand/overdue
    # vs the deduped book. See audit (sync stats pre-dedup).
    seen = {}
    for row in rows:
        if row.get("account_id"):
            seen[row["account_id"]] = row
    overdue = 0
    total_demand = 0.0
    by_status = {}
    prepared = []
    for row in seen.values():
        row = dict(row)
        # parsed numerics alongside the raw CSV strings (for aggregation/UI)
        row["demand_amount"] = _num(row.get("demand"))
        row["emi_amount"] = _num(row.get("emi"))
        row["pos_amount"] = _num(row.get("pos"))
        row["total_amount"] = _num(row.get("total"))
        row["od_days_num"] = _num(row.get("od_days"))
        prepared.append(row)
        if row["demand_amount"]:
            total_demand += row["demand_amount"]
        if (row.get("od_days_num") or 0) > 0:
            overdue += 1
        st = (row.get("account_status") or "?").upper()
        by_status[st] = by_status.get(st, 0) + 1
    # ONE bulk insert (thousands of per-row Atlas round-trips would time out).
    n = store.bulk_replace_lms_presentment(prepared, triggered_by=triggered_by, mode=mode,
                                           force=force)
    return {"rows": n, "total_demand": round(total_demand, 2), "overdue": overdue,
            "by_status": by_status, "triggered_by": triggered_by, "mode": mode}
