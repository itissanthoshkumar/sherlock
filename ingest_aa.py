"""Ingest a real Digitap retrievereport payload into the app's data model.

In production the monthly cycle's AA pull already lands this exact multi-account
JSON as pulls.raw_report_json; this module lets us load a real sample the same
way so every downstream reader (latest_parsed_report, Customer 360, the AA
intelligence panel, classification) runs on real data instead of the mock.

ingest_report() creates: a loan master, one check run, one accounts+pulls row
per bank account (RETRIEVED, balance = current_balance, is_repayment = the
mandate account resolved by aa_report), and stamps the resolved EMI + geo on the
loan master. Idempotent per loan_id (a re-ingest replaces the prior run).
"""
import json

import aa_report
import mongostore as store


def ingest_report(loan_id, raw, customer_name=None, branch=None, state=None,
                  triggered_by="aa-ingest", replace=None, fetch_type=None):
    # replace=True wipes the loan's prior runs for this source (idempotent re-load of a
    # FIXED sample); on the LIVE aa-live path each retrieve is DISTINCT evidence under the
    # customer's consent, so it must NOT delete the previous month's run — keep history.
    # See audit (aa-live retrieve destroys prior runs). Default: replace only for the
    # sample loader (aa-ingest), keep history for everything else (aa-live, cycle).
    if replace is None:
        replace = (triggered_by == "aa-ingest")
    model = aa_report.parse(raw)
    mandate = aa_report.resolve_mandate(model)
    mkey = (mandate or {}).get("account_key")
    emi = (mandate or {}).get("emi_amount")
    # borrower-of-record = holder of the mandate account, not just the first holder
    macct = next((a for a in model["accounts"] if a["key"] == mkey), None)
    borrower = ((macct or {}).get("holder") or {}).get("name") if macct else None
    holder0 = (model.get("holders") or [{}])[0]
    name = customer_name or borrower or holder0.get("name") or loan_id

    db = store._db()
    # wipe prior runs ONLY when replacing (sample re-load) — never on the live aa-live path
    if replace:
        old = [r["id"] for r in db.checks.find({"loan_id": loan_id, "triggered_by": triggered_by}, {"_id": 0, "id": 1})]
        if old:
            db.pulls.delete_many({"run_id": {"$in": old}})
            db.accounts.delete_many({"run_id": {"$in": old}})
            db.checks.delete_many({"id": {"$in": old}})

    run_id = store.create_run(loan_id, emi_amount=emi)
    db.checks.update_one({"id": run_id}, {"$set": {"triggered_by": triggered_by}})

    # Stamp each pull with the kind of fetch that produced it, so PERIODIC pulls are a
    # queryable set. Precedence: what the caller actually REQUESTED wins, falling back to
    # what the payload claims. The request is the reliable signal — the bundled mock
    # samples are ONETIME captures, so trusting the payload alone would mislabel every
    # mock periodic pull.
    ftype = fetch_type or (raw.get("report_fetch_type") if isinstance(raw, dict) else None)

    n_acc = 0
    for a in model["accounts"]:
        is_repay = a["key"] == mkey
        acct_id = store.add_account(run_id, {
            "bank_name": a["bank"], "account_ref": a["last4"], "is_repayment": is_repay,
            "source": "AA", "aa_enabled": True, "main_txn_id": model["meta"].get("txn_id"),
            "bank_account_uid": a["key"], "ifsc": a["ifsc"], "branch_name": None,
            "account_holder_name": (a["holder"] or {}).get("name"), "emi_amount": emi,
            "consent_status": "ACTIVE", "fetched_at": a.get("balance_as_of"),
        })
        pid = store.add_pull(run_id, acct_id, a["bank"], is_repay,
                             model["meta"].get("txn_id"), model["meta"].get("txn_id"),
                             "RETRIEVED", account_key=a["key"], fetch_type=ftype)
        store.update_pull(pid, available_balance=a.get("current_balance"), currency="INR",
                          raw_report_json=json.dumps(raw))
        n_acc += 1

    store.finalize_run(run_id, aa_available=True, account_count=n_acc, aa_count=n_acc, status="DONE")
    db[store.MASTER].update_one({"type": "loan", "lms_loan_id": loan_id}, {"$set": {
        "customer_name": name, "branch": branch, "state": state, "emi_amount": emi,
        "updated_at": store._now()}}, upsert=True)
    return {"loan_id": loan_id, "run_id": run_id, "accounts": n_acc,
            "mandate_account": (mandate or {}).get("last4"), "emi": emi,
            "customer_name": name}


SAMPLE_LOAN_ID = "PP-2013"


def ingest_sample(path="samples/aa_boda.json", loan_id=SAMPLE_LOAN_ID):
    with open(path, encoding="utf-8", errors="replace") as fh:
        raw = json.load(fh)
    return ingest_report(loan_id, raw, branch="Khammam", state="Telangana")
