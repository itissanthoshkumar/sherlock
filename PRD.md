# PRD — Sherlock: Pre-NACH Intelligence & Action System

*Product name: **Sherlock** (by Prayaan) — formerly "DPD Early-Warning". Tagline: "It's elementary: know the bounce before the 4th."*

| | |
|---|---|
| **Product** | Sherlock (pre-NACH cycle / DPD early-warning) |
| **Owner** | Prayaan / PCPL |
| **Version** | v3.0 |
| **Date** | 7 July 2026 |
| **Status** | **Built end-to-end and verified** — demo runs on mock LMS/Digitap data; MongoDB is live |

## Implementation status (v3) — live vs mock

Every functional requirement below (FR-1 … FR-12) is implemented and verified. What varies is the data source behind it:

| Area | Status |
|---|---|
| **MongoDB Atlas** (`PrayaanBiz`) | 🟢 **LIVE** — all 15 collections created, indexed and populated; roles/users seeded on startup |
| Cycle engine, classification, 4-attempt guardrail, overrides, retries, exports, RBAC, Customer 360, Customers directory | 🟢 **Built & verified** (runs on the mock sources below) |
| Scheduler / crons (monthly cycle, outcomes, consent sweep, stuck-cycle recovery) | 🟢 **Built & verified** — in-app, no external deps |
| Team worklists + dispositions (telecaller / field roles) | 🟢 **Built & verified** |
| NACH outcome capture + accuracy dashboard | 🟢 Built — outcomes are a 🟡 **mock simulation** (deterministic per customer) until the live NACH return feed is wired |
| LMS portfolio & account lookup | 🟡 **MOCK** — deterministic 12-loan portfolio; live MySQL hook ready (read-only `lookup.sql`, admin DB-config UI with connection test, `PORTFOLIO_SQL_FILE`) — pending credentials + column mapping |
| Digitap AA pulls | 🟡 **MOCK** — replays a real customer report with deterministic balance variation; live credentials configured, flip `DIGITAP_MOCK=false` to go live |
| Consent delivery (SMS/WhatsApp link) | 🟡 **MOCK** — the "customer" auto-completes the AA journey ~15 s after request; real Digitap consent endpoint still to be confirmed |
| Demo logins (admin / ops1 / telecaller1 / field1 / viewer1) + history seed/reset jobs | 🟡 Demo conveniences, disabled by env flags for production |
| Credential rotation (Atlas + Digitap shared during development) | 🔴 **Pending — do before wider circulation** |

> **Visual edition:** a shareable version of this PRD with the end-to-end flow, classification-gate, consent-sequence and architecture diagrams is published at
> https://claude.ai/code/artifact/9de9f18f-4fa3-4672-955f-9f187a0ac7ba

---

## 1. Overview & Problem

PCPL presents e-NACH mandates for EMI collection on the **4th of every month**. When the customer's repayment account holds less than the EMI on presentation day, the NACH **bounces** — triggering bounce charges to the customer, penal interest, collection follow-up cost, and the first step of the DPD (Days-Past-Due) ladder toward delinquency.

Today PCPL discovers insufficient balances **after** the bounce. This product moves that discovery to **before presentation**: on the 1st/2nd of the month, the system pulls the live balance of every consented customer's bank account via the **Account Aggregator (AA)** framework (Digitap), classifies each customer by their ability to cover the EMI, and hands the tele-calling and field teams a prioritized worklist — two to three days before the NACH hits.

**One line:** predict which NACH presentations will fail due to insufficient balance, and act on them before the 4th.

## 2. Goals & Success Metrics

| Goal | Metric | Target (first 3 cycles) |
|---|---|---|
| Reduce NACH bounce rate | % of presented mandates that bounce, vs trailing 3-month baseline | measurable reduction on the consented cohort |
| Consent coverage | % of eligible customers with a valid periodic-pull consent | grow month-on-month; every cycle reports the number |
| Cycle timeliness | Cycle completed (all pulls + classification) | by end of day on the 2nd |
| Actionability | % of Stretched/Shortfall customers contacted before the 4th | tracked by teams (outside app in v2) |
| Cost control | AA pull attempts per account per month | hard cap: 4 |

## 3. Users & Roles

| Role | Who | What they can do |
|---|---|---|
| **admin** | Product / ops lead | Everything: run cycles, override classification, exports, user management, DB config, data browser |
| **operator** | Central ops team | Run cycles, retry pulls, request consent, view reports/history/master data, export CSVs, data browser |
| **viewer** | Management / branch leadership | View cycle results, buckets, reports and history — read-only, no exports |

Branch staff (BM/RO/BCM) assist customers with consent completion; in v2 they act through an operator/admin user who initiates the consent request from the app. Dedicated tele-calling/field roles with in-app case dispositions are on the roadmap (§8).

## 4. End-to-End Monthly Flow

```
1st–2nd of month                                  3rd            4th
────────────────────────────────────────────────  ─────────────  ────────────
[Run cycle]                                       Teams work     e-NACH
  1. Fetch eligible loans from LMS                the lists:     presentation
     (non-closed + selected NPA-parked)           - reminders
  2. For each loan: resolve bank accounts,        - field visits
     consent status (LOS + PCPL registry)         - consent
  3. Initiate AA period-pull for every              completion
     consented account (cap: 4/account/month)
  4. Retrieve balances + full AA report
  5. Intelligence layer: classify by
     repayment-account balance ÷ due amount
  6. Route: Stretched → tele-calling,
     Shortfall → field, No signal → consent drive
```

## 5. Functional Requirements

### FR-1 — Eligibility & portfolio fetch
The cycle takes **all eligible customers** from the LMS: non-closed loans plus designated NPA-parked cases, each with loan id, customer name, **due amount (EMI)** and the **NACH-mandate repayment bank account**. No manual data entry. (v2 runs on a mocked LMS query; live MySQL wiring is an open item, §9.)

### FR-2 — AA pull orchestration
For every AA-consented bank account of an eligible customer, the system automatically runs the Digitap period-pull sequence (initiate → status check → retrieve report). Pulls run for **all** the customer's consented accounts, not only the repayment account. Each pull auto-completes ~10s after initiation, with a manual "check now" fallback.

### FR-3 — Classification (intelligence layer)
Each customer is classified by the ratio **repayment-account balance ÷ due amount**:

| Bucket | Display name | Rule | Meaning |
|---|---|---|---|
| COMFORT | **Cushioned** | ratio ≥ 2× | Comfortable cover; no action |
| WATCH | **Stretched** | 1× ≤ ratio < 2× | Covers the EMI but thin; reminder call |
| SHORTFALL | **Shortfall** | ratio < 1× | Will bounce as of today; field action |
| NO_DATA | **No signal** | no repayment-account balance available | Consent gap / pull failure; fix the pipe |

The bucket is decided **only by the repayment (mandate) account** — that is the account the NACH will hit. An **aggregate ratio** across all the customer's consented accounts is computed and shown as a secondary signal ("has funds elsewhere"). Every No-signal case carries a machine-readable reason (consent expired, not linked, no parent txn id, pull failed, attempt cap, EMI missing).

### FR-4 — Routing
Stretched → **tele-calling list**; Shortfall → **field-team list**; No signal → **consent-acquisition list**. Lists are viewable in-app (filterable by bucket) and exportable as CSV per bucket (FR-11). Case dispositions inside the app are out of scope for v2 (§8).

### FR-5 — Consent program
1. **Coverage stats**: every cycle reports "of X eligible customers, Y have a valid periodic-pull consent" as a funnel (eligible → consented → pulled → classified).
2. **Unified registry**: consents captured at origination live in the **LOS**; consents acquired later with PCPL staff assistance live in **our database**. The registry merges both, and every consent shows its **source: LOS | PCPL**, status, and expiry.
3. **Re-acquisition**: for non-consented/expired customers, staff initiate an AA consent request from the app; the customer receives an SMS/WhatsApp link; a PCPL employee (BM/RO/BCM) guides completion; the resulting status is reflected in the app.
4. **Expiry hygiene**: consents nearing expiry (≤30 days) are tagged so renewal happens before the data pipe breaks.

### FR-6 — Attempt guardrail
Hard cap of **4 AA initiate attempts per bank account per calendar month**, enforced across cycle runs, retries, and ad-hoc checks alike. Blocked attempts are recorded in an attempt ledger and surfaced in the UI (`n/4 used`). Status checks and report retrievals do not consume attempts.

### FR-7 — Full request/response logging
Every AA API call — initiate, status, retrieve, consent request; every attempt, allowed or blocked — is logged with full request and response payloads and its cycle context, viewable in the data browser.

### FR-8 — Processed-data store
Derived output only (no raw reports) is stored in a dedicated collection, one document per customer per cycle: ratios, bucket + reason, per-account balances, and analytics — 3-month credits/debits, top spend categories (narration tagging), other-lender EMI outflows, EMI bounce history, average/min balances, salary/employment signals, fraud flags, and derived tags (e.g. OTHER_EMIS, LOW_INFLOWS).

### FR-9 — Classification override
A supervisor (admin) can manually move a customer to a different bucket with a mandatory reason. The computed bucket is never lost; the override records who/when/why and is visible in the UI (marked) and in exports (computed vs effective columns). A fresh re-pull clears the override.

### FR-10 — Retry
Staff can re-run the AA pull for a customer within the attempt cap (a retry consumes one initiate attempt per consented account).

### FR-11 — Export
Bucket lists download as CSV (loan, customer, due, repayment balance, ratio, computed & effective bucket, override reason, aggregate signal, consent status). Full AA reports download as JSON per pull.

### FR-12 — Customer 360
Clicking any customer opens a single view across cycles: consent status + full consent request/response payload + source (LOS/PCPL), all linked bank accounts, latest pull results with drill-down into the full transaction-level AA report, spending analytics, and bucket history.

## 6. Non-Functional Requirements

- **Auditability** — every cycle, pull, attempt, override, and consent action is persisted with actor and timestamp; nothing is silently overwritten.
- **Access control** — permission-gated API and UI (RBAC); least privilege per role in §3.
- **PII handling** — masked account numbers in lists; password hashes never leave the server; data browser is allow-listed and read-only.
- **Cost control** — the attempt cap (FR-6) bounds Digitap spend to a known ceiling per account per month.
- **Reliability** — a classification failure must never break the underlying pull/check pipeline; cycle re-runs in the same month require explicit confirmation.
- **LMS safety** — the system only ever issues read-only queries against the LMS.

## 7. Data Model Summary (MongoDB, plain English)

| Collection | Holds |
|---|---|
| `cycles` | One doc per monthly run: status, funnel totals, bucket counts |
| `cycle_items` | One doc per customer per cycle: bucket, ratios, override, link to the underlying run |
| `processed_data` | Derived intelligence only, per customer per cycle (FR-8) |
| `aa_attempts` | The guardrail ledger — every initiate attempt, allowed or blocked |
| `checks` / `accounts` / `pulls` | The underlying pull runs (shared with ad-hoc single-loan checks) |
| `api_logs` | Full request/response of every Digitap call |
| `consents` + `ppdata` (master) | Unified consent registry (LOS + PCPL sourced), loan & bank-account masters |
| `users` / `roles` | RBAC |

## 8. Roadmap — updated for v3

Shipped since this PRD was first approved (formerly "out of scope"):

1. ~~**Scheduler**~~ — ✅ **SHIPPED v3**: in-app cron (cycle day-1 07:00, outcomes day-5, daily consent sweep, hourly stuck-cycle recovery + startup auto-recovery), with a dashboard jobs panel (Run now / Disable).
2. ~~**Team dispositions**~~ — ✅ **SHIPPED v3**: telecaller/field roles with focused queues, disposition trail (Contacted / PTP with date / No response / Paid / Will bounce) and append-only audit.
3. ~~**Portfolio dashboards**~~ — ✅ **SHIPPED v3**: cycle-over-cycle coverage & bucket trends, prediction-vs-outcome accuracy matrix, worklist progress.

Still ahead:

4. **Live LMS (MySQL/engrow) wiring** — plumbing complete (read-only SQL, config UI, connection test); pending credentials and column confirmation. Until then the portfolio is a deterministic 12-loan mock.
5. **Live Digitap consent API + WhatsApp/SMS delivery** — consent flow works end-to-end today with a mock customer completion (~15 s); real endpoint + channel integration to follow.
6. **Live NACH return feed** — outcomes are currently a deterministic mock simulation; wire the presentation-results feed to replace it.
7. **Bounce-probability scoring (v4)** — beyond point-in-time balance: salary-date timing, min-balance patterns, historical bounces, competing EMIs; dynamic NACH presentation-date recommendation.

## 9. Open Items (blockers to go-live)

| # | Item | Owner |
|---|---|---|
| 1 | LMS MySQL credentials + read-only user | PCPL IT |
| 2 | Confirm LMS columns: parent (main) txn id, EMI amount, consent id & expiry in the bank-account/loan query | PCPL IT |
| 3 | Real Digitap consent-request endpoint (curl) + a sample status-check response | Digitap / integration |
| 4 | **Rotate the Digitap and MongoDB Atlas credentials** shared during development, and move to least-privilege users | PCPL IT |
| 5 | Decision: which NPA-parked cases are cycle-eligible | Business |
| 6 | AA consent capture made mandatory in origination journey (new loans) | Product/LOS |
