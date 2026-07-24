---
name: db-health
description: Check MongoDB Atlas connectivity and data state for the DPD Early-Warning app (collection counts, master-data types, connection mode). Use whenever login fails, data looks missing or stale, the app shows connection errors, before a demo, after editing MONGO_URI/MONGO_DB/MONGO_MOCK in .env, or when the user asks "is the DB up / connected / what's in Mongo".
---

# Database health check

Two ways, cheapest first:

1. **Running app** — unauthenticated health endpoint:

   ```bash
   curl -s localhost:8001/api/health
   ```

   Expect `"connected": true`, `"db": "PrayaanBiz"`, per-collection counts, and
   `ppdata_by_type` (loan / bank_account / consent). `mode: "mock"` means the
   app is on in-memory mongomock (`MONGO_MOCK=true`) — nothing persists.

2. **No server needed** — standalone validator from the project root:

   ```bash
   cd <project-root> && python3 check_db.py
   ```

   It loads `.env`, pings the cluster, and prints collection counts. If imports
   fail, prepend the vendored deps: `PYTHONPATH=vendor python3 check_db.py`.

## Interpreting failures

- `DBUnavailable` / DNS errors → check `MONGO_URI` in `.env`; `mongodb+srv`
  URIs need the `dnspython` package (vendored).
- Counts all zero on a fresh DB is normal — `db._ensure()` seeds roles and the
  bootstrap admin on first connect.
- Collections to expect after v2: users, roles, ppdata, checks, accounts,
  pulls, api_logs, consents, cycles, cycle_items, processed_data, aa_attempts.
- Never print the connection string with credentials; `health()` masks it.
