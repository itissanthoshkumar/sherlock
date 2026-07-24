---
name: run-cycle
description: Run a DPD Early-Warning monthly pre-NACH cycle end-to-end against the running app and report the funnel and buckets. Use whenever the user asks to run/demo/trigger the cycle, re-run this month, test the classification funnel, check the buckets, or when a cycle is stuck in RUNNING/COLLECTING (e.g. after the server restarted mid-run) and new runs return 409 "still running" — this skill includes the recovery procedure.
---

# Run a monthly cycle

The cycle runs against the app on `http://localhost:8001` (start it first — see
the `sync-preview` skill if it isn't running). Login uses the bootstrap admin
(`admin` / `admin123` unless `BOOTSTRAP_ADMIN_*` env was changed). With
`LOOKUP_MOCK=true` the portfolio is the 12 PP-20xx loans and the expected result
is deterministic: funnel **12 eligible → 9 consent-OK → 8 repay pulls**, buckets
**3 Cushioned / 2 Stretched / 3 Shortfall / 4 No signal**.

## Steps

1. Login and start the cycle:

   ```bash
   C=$(mktemp)
   curl -s -c "$C" -X POST localhost:8001/api/login \
     -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"admin123"}'
   curl -s -b "$C" -X POST localhost:8001/api/cycle/run \
     -H 'Content-Type: application/json' -d '{"confirm":false}'
   ```

2. Interpret the response:
   - **200** → note the cycle `id`, go to step 3.
   - **409 "already exists"** → a cycle already ran this month. Re-running
     consumes fresh AA attempts (cap: 4/account/month), so confirm with the
     user before re-posting with `{"confirm":true}`.
   - **409 "still running"** → either a cycle is genuinely in flight (wait) or
     it's a zombie from a server restart → run the recovery script:
     `python3 scripts/recover_cycle.py` (next to this SKILL.md; marks stale
     RUNNING/COLLECTING cycles as ERROR), then retry.

3. Poll `GET localhost:8001/api/cycle/<id>` (cookie jar `-b "$C"`) every ~3 s
   until `status` is `DONE` (mock completes in ~15–40 s; `DIGITAP_AUTO_DELAY`
   drives the pull delay).

4. Report to the user: the funnel (`totals`: eligible / repay_consent_ok /
   repay_pulls / consent gap), `bucket_counts`, and any items whose
   `bucket_reason` is not OK. If attempts are near the cap (attempts_left 0–1),
   say so — further retries will be blocked this month.

## Notes

- Every pull initiate consumes one monthly attempt per account — don't loop
  cycle runs for fun on the live Atlas DB.
- The Cycle tab in the UI shows the same run live; prefer pointing the user
  there for demos (login admin/admin123 → Cycle).
