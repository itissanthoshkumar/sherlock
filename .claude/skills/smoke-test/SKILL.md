---
name: smoke-test
description: Offline regression test of the DPD Early-Warning cycle engine — runs a full mock monthly cycle on in-memory mongomock (no Atlas, no server, no Digitap spend) and asserts the deterministic funnel (12/9/8), bucket spread (3/2/3/4), the 4-attempt guardrail, override audit, and CSV export. Run this after ANY edit to checker.py, cycle.py, mongostore.py, mock_report.py, digitap.py or rbac.py, and before syncing the preview or demoing — it catches engine regressions in ~30 seconds without touching live data.
---

# Cycle-engine smoke test

One command from the project root:

```bash
python3 .claude/skills/smoke-test/scripts/smoke_cycle.py
```

It forces `MONGO_MOCK=true`, `LOOKUP_MOCK=true`, `DIGITAP_MOCK=true` and a
0.2 s pull delay, so it is fully offline and consumes **no** live AA attempts.

## What it asserts

| Check | Expected |
|---|---|
| Funnel | 12 eligible · 9 repay-consent OK · 8 repay pulls |
| Buckets | COMFORT 3 · WATCH 2 · SHORTFALL 3 · NO_DATA 4 |
| Known ratios | PP-2001 ≈ 5.19× · PP-2005 ≈ 0.87× |
| No-signal reasons | PP-2009 CONSENT_EXPIRED · PP-2010 REPAY_NOT_AA · PP-2011 NO_TXN_ID |
| Guardrail | after retries exhaust 4 attempts, the next initiate is CAPPED and ledgered `allowed: false` |
| Override | computed bucket preserved; effective bucket changes; recount updates |
| Export | CSV contains both computed and effective bucket columns |

Exit code 0 with `PASS` on success; non-zero with the first failing assertion
printed. On failure, fix the engine before syncing the preview — the same
deterministic portfolio backs the live demo, so a failing smoke means a broken
demo.

If the expected numbers themselves changed intentionally (new mock loans,
changed EMIs, new balance formula), update the constants at the top of
`scripts/smoke_cycle.py` in the same commit.
