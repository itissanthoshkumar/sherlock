# DPD Early-Warning

A **web application** that gives an early Days-Past-Due signal before EMI NACH.

EMI NACH is presented on the **4th** of every month. On the **3rd**, an operator
enters a loan + EMI; the app resolves **every bank account** on the loan, flags
which are **Account-Aggregator (AA) enabled**, and for each AA account runs a
**Digitap period fetch** to read the balance and flag whether it covers the EMI.

```
Loan account ID + EMI
   └─ read-only SQL ─▶ all bank accounts (bank, repayment?, source, fetched, parent txn id)
        └─ AA rule: source='AA' AND fetched > cutoff(2026-04-14) AND has parent txn id
             └─ for EACH AA account:  Digitap initiate(main_txn_id) ─▶ child txn id
                  └─ ~10s later (or "Check now"):  statuscheck(child) ─▶ retrievereport(child)
                       └─ balance vs EMI ─▶ SUFFICIENT / INSUFFICIENT / INDETERMINATE
   └─ everything persisted: input, accounts, pulls, and every API request/response
```

If no account qualifies, the result says **AA not available** for the loan.

## Digitap: the 3-call flow (two credential pairs)

| Call | Endpoint | Body | Credentials |
|------|----------|------|-------------|
| initiate | `/bank-data/initiate_periodic_fetch` | `{"main_txn_id": <parent>}` | INIT pair |
| statuscheck | `/bank-data/statuscheck` | `{"request_id": <child>}` | DATA pair |
| retrievereport | `/bank-data/retrievereport` | `{"txn_id": <child>}` | DATA pair |

`initiate` returns `txn_id` (the **child id**), reused for statuscheck + retrieve.

## Layout

| File | Purpose |
|------|---------|
| `app.py` | FastAPI: auth, `/api/check`, `/api/run/{id}`, `/api/pull/{id}/refresh`, history, DB/user admin |
| `checker.py` | `start_check()` / `process_pull()` — AA detection + the Digitap orchestration |
| `digitap.py` | initiate / statuscheck / retrieve, two Basic-auth pairs, config-driven + mock |
| `store.py` | SQLite audit: `runs`, `accounts`, `pulls`, `api_calls` |
| `dbconfig.py` | Read-only MySQL connection (in-app configurable) |
| `userstore.py` / `manage_users.py` | Login / roles |
| `lookup.sql` | Loan → all bank accounts query (`{{loan_id}}` bound) |
| `static/` | Clay-free CRED-themed console + login |

## Storage (full audit trail)

- `runs` — input (loan, EMI) + AA-availability verdict + status
- `accounts` — every bank account the SQL returned (bank, repayment, source, fetched, AA, parent txn id)
- `pulls` — one per AA account: child txn id, status, balance, currency, decision, raw report
- `api_calls` — every Digitap request + response (bodies, HTTP status, ok), linked to run + pull

## Setup

```bash
cd dpd-early-warning
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill DB + Digitap creds (or keep mocks for a dry run)
python manage_users.py add admin --admin
uvicorn app:app --reload --port 8001
```

Open <http://localhost:8001>, sign in, run a check.

## Going live

1. Set the two credential pairs and flip the mock off in `.env`:
   `DIGITAP_INIT_USER/PASS`, `DIGITAP_DATA_USER/PASS`, `DIGITAP_MOCK=false`.
2. Point `lookup.sql` at your real schema — it must return one row per bank
   account with aliases `bank_name, account_ref, is_repayment, source,
   fetched_at, main_txn_id`; set DB creds and `LOOKUP_MOCK=false`.
3. After the first live call, confirm the response field paths in `.env`
   (`DIGITAP_CHILD_ID_PATH`, `DIGITAP_STATUS_READY_VALUES`,
   `DIGITAP_BALANCE_PATH`, `DIGITAP_CURRENCY_PATH`) match the real payloads.

## Dev / demo without a database

`LOOKUP_MOCK=true` + `DIGITAP_MOCK=true` runs the whole flow with canned data
(4 accounts, 2 AA-enabled). `DIGITAP_AUTO_DELAY` controls the auto statuscheck delay.

## Config knobs (`.env`)

- `AA_FETCH_CUTOFF` (default 2026-04-14) — AA accounts must be fetched after this.
- `DIGITAP_AUTO_DELAY` (default 10s) — auto statuscheck+retrieve after initiate.
- `DPD_BUFFER` — balance must clear EMI + buffer to be SUFFICIENT.

## Roadmap

- **Batch + scheduler** — run for all loans due on the 4th, on the 3rd, via cron;
  reuses `start_check` / `process_pull`. Dashboard over the audit tables.
