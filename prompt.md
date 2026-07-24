# Sherlock — Build Prompt (rebuild-from-scratch spec)

> **Purpose of this file.** This is a complete, self‑contained specification for
> an AI coding tool (Lovable, Bolt, v0, Cursor, Claude, …) to **rebuild the
> entire Sherlock application from zero**. It describes the vision, the users,
> the end‑to‑end workflow, every screen, the domain rules, the external
> integrations, the data model, the roles, the skills/stack required, and the
> acceptance criteria that prove the rebuild is correct.
>
> Pair it with [`ARCHITECTURE.md`](ARCHITECTURE.md) for the data‑flow diagrams
> and the full table catalogue. Where this prompt and the code disagree, the
> code wins — but this prompt is the source of truth for *intent*.

---

## 0. One sentence

Build a web app that reads a lender's borrowers' **live bank balances via the
Account Aggregator network, before a NACH auto‑debit is presented**, and sorts
the month's collection book into *will‑bounce* vs *will‑clear* so a collections
team can act in time.

---

## 1. The vision

Lenders collect EMIs by NACH auto‑debit. When the debit fails (a *bounce*), it
costs penalties, damages the lender's bounce ratio with the bank, and wastes
collection effort — and today you only find out **after** it fails.

**Sherlock inverts this.** With the borrower's consent on the Account Aggregator
(AA) network, it fetches their **real bank balance a few days before** the debit,
compares it to the amount due, and tells the floor exactly who will fall short —
**while there is still time** to call them, arrange funds, or re‑time the debit.

The product is a magnifier: *know the bounce before it happens.* It must be
**honest** (never pretend a missing balance is fine), **cost‑disciplined** (every
balance read is a billed vendor call), and **auditable** (it touches money and
consent, so every decision must be reconstructable).

Success = a measurable drop in the monthly bounce rate, and a collections team
that spends its effort on the accounts that actually need it.

---

## 2. Who uses it (personas → roles)

| Persona | Role | What they do |
|---|---|---|
| Collections head / admin | `admin` | Runs the monthly cycle, overrides classifications, manages jobs, users, consents, master data. Full access. |
| Ops / analyst | `operator` | Runs cycles and checks, retries pulls, fetches consent, disposes cases, runs jobs, exports lists. |
| Tele‑calling agent | `telecaller` | Works the **WATCH** (thinly‑covered) queue — calls borrowers, records promise‑to‑pay / dispositions, requests consent. |
| Field agent | `field` | Works the **SHORTFALL** (will‑bounce) queue — field visits, dispositions, requests consent. |
| Management / read‑only | `viewer` | Views cycles, dashboard, reports, history, master data. No writes. |

The floor only sees its own queue. Only elevated users (admin) may override a
bucket. Revoking a role must invalidate that user's live sessions immediately.

---

## 3. The end‑to‑end workflow (what's expected)

This is the heart of the product — one loop, run monthly by the collections team.
Each step is **gated on the one before it**. Build the app so a user can walk
this path with no dead‑ends.

1. **Pull the demand book (Presentment).** Fetch from the LMS every loan due for
   collection this month: borrower contact, demand amount, due date, days
   overdue. Store it as this month's presentment snapshot.
2. **Sync the portfolio.** Fetch from the LOS the origination record — repayment
   account + EMI per loan — and seed any consent captured at disbursal.
3. **Settle consent.** For each borrower, confirm we have valid consent to read
   their balance. Consent arrives three ways: (a) seeded from the LOS, (b)
   captured live through an AA consent journey, (c) **bulk‑uploaded from a CSV**.
   Consent lives in a registry that gates every billed pull.
4. **Pre‑flight gate.** The monthly run must refuse to start on stale inputs:
   the presentment snapshot must belong to *this* month and the consent set must
   be resolved. If not, block with a clear "fix this first" message (HTTP 428).
5. **Run the check.** Open a cycle. For every consented, eligible loan, queue one
   **periodic AA balance pull**. Loans that can't be pulled are carried forward
   with an honest reason (no consent / expired / not pullable), never dropped.
6. **Collect balances (async).** Pulls run asynchronously — a bank's data is
   rarely ready instantly (~60s+). Poll for readiness (a **free** call), harvest
   what lands, and **time out stragglers** so the cycle always reaches a terminal
   state and can never wedge.
7. **Classify.** Compare each loan's balance *at the moment of presentation* to
   its **demand**, and assign one of four buckets (see §5). Freeze the prediction.
8. **Work the floor.** Telecaller and field queues take dispositions
   (promise‑to‑pay, contacted, nudge, re‑time). A supervisor/admin may override a
   bucket; the computed value is preserved alongside the override.
9. **Confirmation sweep.** A day before each loan's own due date, re‑pull the
   at‑risk and promised accounts so the decision rests on a *fresh* balance.
10. **Record the outcome.** After the debit, record CLEARED / BOUNCED per loan. A
    real bounce automatically produces a re‑presentation date.
11. **Learn.** Because predictions were frozen at step 7, score them honestly
    against the outcomes — accuracy and calibration feed next month's read.

---

## 4. Screens to build

The reference app is a single‑page console with a top nav. Build these screens
(equivalent functionality; exact layout is yours):

| Screen | Purpose |
|---|---|
| **Dashboard** | Cycle‑over‑cycle health: buckets, coverage, bounce rate, prediction accuracy, scheduled jobs. Landing page for admin/ops/viewer. |
| **Cycle** | The active monthly cycle: funnel, buckets, the **worklist** (per‑loan cases with dispositions, PTP, nudges, overrides), re‑presentation planner, confirmation sweep, outcome recording, CSV export. |
| **Presentment** (LMS) | Pull + browse the LMS demand book (loans due, contact, demand, due date, OD days). |
| **Portfolio Sync** (LOS) | Pull the LOS portfolio (repayment account + EMI) and blend consent. |
| **Consents** | The consent registry: per‑loan consent rows, status, validity, pull‑eligibility; add/edit a consent; **bulk CSV upload** with a dry‑run validator; change history. |
| **Borrowers** | The book of all disbursed borrowers (searchable), origin‑tagged. |
| **Live Pull** | Step‑by‑step AA consent + pull journey against the real vendor, approval‑gated, showing each request/response (and a copy‑pasteable cURL, auth redacted). Also a one‑action "periodic pull" from an existing consent id. |
| **AA Analyser** | Paste/ingest an AA report and view the parsed intelligence (balances, recurring flows, fraud/obligation signals). |
| **Checks** | History of ad‑hoc balance checks/runs with the resolved accounts and reports. |
| **Customer 360** | Per‑borrower deep view: master data, consent journey + Digitap call trail, banking pulls (with type + full JSON), spend signals, timeline, notes. |
| **Data** | A raw database browser (admin): pick any collection, search, page, view a document's full JSON. Shows ground truth. |
| **Users** | Admin user management: create branch staff (temp password + must‑change), edit role/branch, suspend/revoke/reset/delete, embedded auth log. |

---

## 5. Domain rules (non‑negotiable — these are load‑bearing)

**The four buckets** (classification = balance at presentation vs the demand):

| Bucket | Condition | Action |
|---|---|---|
| `COMFORT` | balance comfortably ≥ demand | leave alone |
| `WATCH` | clears, but thinly | nudge (telecaller queue) |
| `SHORTFALL` | balance < demand → will bounce | work it (field queue) |
| `NO_DATA` | no usable read — no consent, expired, or pull failed | **blind exposure**, never counted as safe; carries the specific reason |

**Consent eligibility** — a loan may be pulled this month only if **all** hold
(else it is `NOT_PULLABLE` / `EXPIRED` / `NO_CONSENT`, fail‑closed on bad dates):
1. status `ACTIVE`
2. type `PERIODIC` (a one‑time consent can't drive monthly pulls)
3. a mandate/parent txn id is present
4. expiry / end date is **not** in the past
5. start date is **not** in the future

**Consent‑window ceilings** (enforced server‑side on every consent request;
clamp — never reject — so the request always goes out compliant):
- one‑time fetch range ≤ **13 months** back from today
- periodic fetch range ≤ **6 months** back from today
- consent expiry ≤ **5 years** from today
- all ranges end no later than today

**Cost guardrails** (money discipline — every balance read is billed):
- ≤ **4** billed initiates per mandate per **month**
- ≤ **100** billed vendor calls per **day**
- both reserved **atomically** (no check‑then‑act race)
- readiness polling is **free** and excluded from the counters
- a call that never dispatched is **refunded**

**Honesty.** A missing/failed read is never rounded to *safe*. It lands in
`NO_DATA` and is surfaced as blind exposure with the reason.

**Money path.** Classification measures balance against the **demand**, keyed to
the **presentation month**, and predictions are **frozen at decision time** so
later data can't flatter the score. Outcome records are **immutable**.

**Time = IST.** Store *and* compare every timestamp in `Asia/Kolkata`
(fixed +5:30, no DST). The daily caps and month gates compare stored values
against "today" — so storage and comparison must share one zone. **No UTC.**

**Live‑only.** No runtime mock/live toggle. Sample/fixture replay is allowed
**only** when the store is an in‑memory test database, so fake data can never be
written to a real one. A misconfigured vendor **fails loudly** — never silently
returns sample data.

**Auditable.** Consent changes, bucket overrides, outcomes, scheduler toggles,
and sign‑ins are **append‑only** history. Current state is one row; the story of
how it got there lives beside it. Never mutate history.

---

## 6. External integrations

Three live vendors. All credentials come from environment config; nothing is
hard‑coded. Every outbound call is logged (request + response) for audit.

### 6a. Digitap — Account Aggregator (consent + live balances) — *the billed one*

The whole balance read is a five‑call lifecycle over `POST /bank-data/*`
(HTTP Basic auth, JSON). A first‑time consent runs all five; a repeat pull on an
existing mandate runs the last three.

| # | Call | Cost | Purpose |
|---|---|---|---|
| 1 | `generateurl` | **BILLED** | create consent request; returns an approval URL + `request_id`; sends the borrower an SMS |
| 2 | `statuscheck` | free | poll until the borrower approves; the success row reveals `main_txn_id` (the mandate) |
| 3 | `initiate_periodic_fetch` | **BILLED** | ask the bank to prepare a fresh read; returns a child `txn_id` |
| 4 | `statuscheck` | free | poll the child txn until the data is ready (~60s+) |
| 5 | `retrievereport` | **BILLED** | fetch the full report JSON (balances + transaction intelligence) |

Details a rebuild must get right:
- `client_ref_num` must be a unique **28‑digit numeric** value per request (an
  18‑digit random prefix + a 10‑digit unix epoch). Not a prefixed timestamp.
- Send only `Content-Type` + `Authorization` headers (no `Accept`).
- The consent request carries two blocks: a `ONETIME` and a `PERIODIC`
  `fi_date_range`, plus the periodic `consent.expiry` — all clamped per §5.
- The readiness poll needs the `request_id`; a pull started from only the parent
  mandate id must resolve the `request_id` from the consent registry first, or it
  can't poll and will blind‑fire the billed retrieve before data is ready.
- Persist the **entire raw payload** for every call.

### 6b. Engrow — LOS (origination / portfolio)
Login → list applications → per‑loan detail → repayment account + EMI + any
disbursal consent. A bounded fan‑out (no unbounded loops). Stored as a portfolio
snapshot + a call ledger.

### 6c. Encore — LMS (collections demand)
Login → presentment report → every loan due for collection (contact, demand
amount, due date, OD days). Stored as the monthly presentment snapshot + a call
ledger. This is the entry point of the whole monthly loop.

---

## 7. Data model

Single database. Integer id sequences. IST timestamps. All writes through one
data‑access layer. The full catalogue — every collection, its fields, and what
writes it — is in **[`ARCHITECTURE.md`](ARCHITECTURE.md) §4**. The essential
groups:

- **Master:** borrower/loan/bank‑account master, borrowers book.
- **Integration ledgers + snapshots:** LMS presentment, LOS portfolio, Digitap
  call ledger + a dedicated full‑payload archive, plus a call log per vendor.
- **AA evidence:** runs (`checks`), resolved `accounts`, `pulls`
  (status + balance + `fetch_type` PERIODIC/ONETIME + raw report), and the
  monthly‑attempt guardrail ledger.
- **Consent registry:** one row per (loan, consent) that gates billed pulls,
  plus append‑only change history.
- **Cycle:** the monthly cycle, per‑loan items, **frozen predictions**.
- **Action & outcomes:** dispositions, nudges, notes, immutable NACH outcomes.
- **Append‑only audit:** consent / bucket / outcome / job events, auth log.
- **Ops & access:** scheduled jobs, users, roles.

---

## 8. Roles & permissions (RBAC)

Permissions (gate both routes and UI controls):
`check:run`, `consent:fetch`, `report:view`, `history:view`, `master:view`,
`data:view`, `cycle:run`, `cycle:view`, `classify:override`, `export:data`,
`case:dispose`, `jobs:manage`, `user:manage`, `role:manage`, `dbconfig:manage`.

Role → permission mapping is §2. `classify:override` (move a case between
buckets) is elevated (admin). `telecaller`→WATCH queue, `field`→SHORTFALL queue.
Session auth; revoking a role kills live sessions.

---

## 9. Skills & stack needed to build this

A team/tool rebuilding Sherlock needs competence in:

- **Web app + REST API** with **session auth and role‑based access control**
  (per‑route + per‑control gating; session revocation).
- **A database** and a disciplined single data‑access layer, with **atomic
  counters/reservations** (for the billed‑call caps — must be race‑free).
- **Third‑party API integration** against three vendors: HTTP Basic auth,
  multi‑step **consent + polling** lifecycles, callback/webhook URLs, and
  resilient handling of slow/flaky upstreams (timeouts, retries on transient
  gateway errors, honest failure surfaces).
- **Asynchronous job processing + scheduling** (an in‑app scheduler/cron): a
  bounded readiness‑poll loop that always finalizes; monthly cycle, sweeps, and
  stale‑run recovery.
- **Timezone‑correct date handling** (single‑zone IST discipline; month/day
  boundary math for caps and gates).
- **CSV import (with a dry‑run validator) and CSV export.**
- **An admin/data console**: searchable, paginated data tables; a raw‑document
  JSON viewer; forms with validation.
- **Audit logging** (append‑only event stores) and **secrets management**
  (config in env, never in the repo, rotate on deploy).
- **Financial/data‑integrity discipline**: idempotent upserts, immutable
  outcome records, frozen‑prediction snapshots, fail‑closed validation.
- **Product judgement** for a collections‑ops workflow: the UI is *operated*,
  not read — surface state (buckets, exposure) at a glance, one primary action
  per screen, no dead‑ends in the monthly loop.

**Reference implementation stack** (you may rebuild in any stack, but this is
what the original uses): Python + FastAPI, MongoDB (Atlas in prod; in‑memory
mongomock for tests), a single‑file vanilla‑JS SPA, no build step.

---

## 10. Non‑functional requirements

- **Security:** RBAC everywhere; hashed passwords; must‑change temp passwords;
  session revocation on role change; `Secure` cookies; a strong app secret
  required at boot; output escaping to prevent XSS; sensitive ledgers gated.
- **Cost control:** the billed‑call guardrails of §5 are mandatory and atomic.
- **Reliability:** the monthly cycle must always reach a terminal state (harvest
  or time‑out); a crashed/interrupted run must be recoverable.
- **Transparency:** every vendor call's full request/response is stored and
  viewable; every consequential state change is in an append‑only log.
- **Privacy:** borrower PII and bank data are sensitive — access‑gated, never in
  logs‑as‑URLs, never committed to the repo.

---

## 11. Build order (suggested milestones)

1. **Foundations:** app + auth + RBAC + the single data layer + IST clock.
2. **Ingest:** LMS presentment + LOS portfolio pulls → snapshots + master data.
3. **Consent:** the registry + eligibility rules + manual add + **CSV bulk load
   (dry‑run)**; the consent‑window clamps.
4. **AA pull engine:** the Digitap 5‑call lifecycle, the async readiness loop,
   the **atomic guardrails**, full‑payload persistence.
5. **Cycle engine:** pre‑flight gate → run → classify into the four buckets →
   frozen predictions → coverage/exposure.
6. **Floor:** worklist + dispositions + PTP + nudges + overrides + confirmation
   sweep + re‑presentation.
7. **Outcomes + learning:** record CLEARED/BOUNCED (immutable) → score frozen
   predictions → dashboard accuracy/calibration.
8. **Console polish:** Customer 360, raw Data browser, Users admin, exports,
   the Live Pull cURL/JSON views.

---

## 12. Definition of done (acceptance criteria)

The rebuild is correct when:

- An **offline end‑to‑end test** runs a full mock monthly cycle on an in‑memory
  DB (no vendor spend) and reproduces a **deterministic funnel and bucket
  spread** — the original asserts 12 eligible → 8 repayment pulls → buckets
  `COMFORT 3 / WATCH 2 / SHORTFALL 3 / NO_DATA 4`, with the 4‑attempt guardrail
  blocking a 5th initiate, an override preserving the computed bucket, and CSV
  export carrying both computed and effective buckets.
- The Digitap lifecycle completes generate→status→initiate→status→retrieve with
  the billed/free split honoured and the daily/monthly caps enforced atomically.
- Consent eligibility, the consent‑window clamps, and the four‑bucket
  classification behave exactly per §5.
- Every timestamp is IST; a grep for UTC in the codebase returns nothing.
- No secrets or real PII in the repo; a misconfigured vendor fails loudly.
- The monthly loop is walkable end‑to‑end with no dead‑ends, per §3.

---

## 13. Out of scope / do not

- Do **not** build a runtime mock/live switch. Live‑only; fixtures only in tests.
- Do **not** treat missing data as safe. `NO_DATA` is exposure.
- Do **not** mutate audit history or outcome records.
- Do **not** commit secrets or borrower PII.
- Do **not** introduce UTC anywhere.
