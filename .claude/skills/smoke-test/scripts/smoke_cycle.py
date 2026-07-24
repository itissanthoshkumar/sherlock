"""Offline regression test for the DPD Early-Warning cycle engine.

Runs entirely on mongomock + mock Digitap/LMS: no Atlas, no server, no AA spend.
Asserts the deterministic mock portfolio produces the expected funnel, buckets,
guardrail behaviour, override audit and CSV export. Exit 0 = PASS.
"""
import os
import sys
import time

os.environ["MONGO_MOCK"] = "true"
os.environ["LOOKUP_MOCK"] = "true"
os.environ["DIGITAP_MOCK"] = "true"
os.environ["DIGITAP_AUTO_DELAY"] = "0.2"

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)

import checker  # noqa: E402
import cycle    # noqa: E402
import mongostore as store  # noqa: E402

# repay_consent_ok now requires a mandate txn (bug K4 fix): PP-2011 has an ACTIVE consent
# but no main_txn_id, so it's "not linked", not "consent OK" — 8 now matches repay_pulls (8).
EXPECTED_FUNNEL = {"eligible": 12, "repay_consent_ok": 8, "repay_pulls": 8}
EXPECTED_BUCKETS = {"COMFORT": 3, "WATCH": 2, "SHORTFALL": 3, "NO_DATA": 4}
EXPECTED_REASONS = {"PP-2009": "CONSENT_EXPIRED", "PP-2010": "REPAY_NOT_AA", "PP-2011": "NO_TXN_ID"}
EXPECTED_RATIOS = {"PP-2001": 5.19, "PP-2005": 0.87}  # ±0.01

_failures = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + (f" — {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


def main():
    c = cycle.start_cycle("smoke-test")
    cid = c["id"]
    for _ in range(300):
        time.sleep(0.2)
        if store.get_cycle(cid)["status"] == "DONE":
            break
    d = cycle.cycle_detail(cid)
    items = {i["loan_id"]: i for i in d["items"]}

    print("cycle:", d["status"])
    check("cycle reaches DONE", d["status"] == "DONE")
    t = d["totals"]
    for k, v in EXPECTED_FUNNEL.items():
        check(f"funnel {k} == {v}", t.get(k) == v, f"got {t.get(k)}")
    for k, v in EXPECTED_BUCKETS.items():
        check(f"bucket {k} == {v}", d["bucket_counts"].get(k) == v, f"got {d['bucket_counts'].get(k)}")
    for loan, reason in EXPECTED_REASONS.items():
        check(f"{loan} reason {reason}", items[loan]["bucket_reason"] == reason,
              f"got {items[loan]['bucket_reason']}")
    for loan, ratio in EXPECTED_RATIOS.items():
        got = items[loan]["ratio"]
        check(f"{loan} ratio ≈ {ratio}", got is not None and abs(got - ratio) < 0.01, f"got {got}")

    # Guardrail: PP-2001 used 1 attempt; 3 retries exhaust the cap of 4,
    # the 4th retry must come back CAPPED with a blocked-ledger entry.
    it = items["PP-2001"]
    for _ in range(3):
        cycle.retry_item(it["id"], "smoke-test")
        time.sleep(0.8)
    cycle.retry_item(it["id"], "smoke-test")
    time.sleep(0.8)
    d2 = cycle.cycle_detail(cid)
    it2 = {i["loan_id"]: i for i in d2["items"]}["PP-2001"]
    blocked = store._db().aa_attempts.count_documents({"allowed": False})
    check("cap: PP-2001 attempts == 4", it2["attempts_used"] == 4, f"got {it2['attempts_used']}")
    check("cap: 5th initiate CAPPED", it2["bucket_reason"] == "CAPPED", f"got {it2['bucket_reason']}")
    check("cap: blocked attempts ledgered", blocked >= 1, f"got {blocked}")

    # Override audit + export
    it4 = it2 = {i["loan_id"]: i for i in d2["items"]}["PP-2004"]
    cycle.override_item(it4["id"], "SHORTFALL", "smoke: override audit check", "smoke-admin")
    d3 = cycle.cycle_detail(cid)
    it4b = {i["loan_id"]: i for i in d3["items"]}["PP-2004"]
    check("override: computed preserved", it4b["bucket"] == "WATCH", f"got {it4b['bucket']}")
    check("override: effective changed", it4b["effective_bucket"] == "SHORTFALL")
    check("override: actor recorded", it4b["override_by"] == "smoke-admin")
    csv_out = cycle.export_csv(cid, "SHORTFALL")
    row = next((l for l in csv_out.splitlines() if "PP-2004" in l), "")
    check("export: computed+effective in CSV", "WATCH" in row and "SHORTFALL" in row, row[:80])

    print()
    if _failures:
        print(f"FAIL — {len(_failures)} assertion(s): {', '.join(_failures)}")
        sys.exit(1)
    print("PASS — cycle engine behaves as specified.")


if __name__ == "__main__":
    main()
