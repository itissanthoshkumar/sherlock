# Sherlock — Project Context

> The "read this first" for anyone joining this codebase — human or AI. It
> explains **what** we're building, **why** it's shaped the way it is, and the
> **conventions** you're expected to keep. For the technical map (data flow,
> every table), see [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## What Sherlock is

Sherlock is a **pre‑NACH early‑warning system** for a lending business
(Prayaan Capital). When a loan repayment is collected by NACH auto‑debit, a
*bounce* (failed debit) is expensive: penalties, a hit to the lender's bounce
ratio, and wasted collection effort. The classic problem is that you only learn
about a bounce **after** it happens.

Sherlock flips that. Using the borrower's **consent** on the Account Aggregator
(AA) network, it reads their **live bank balance shortly before** the debit is
presented, compares it to the amount due, and tells the collections team who is
going to be short — **while there is still time to act** (call the borrower, move
money, re‑time the debit).

The name is the magnifier: *know the bounce before it happens.*

---

## The core loop, in plain English

Once a month, a collections officer:

1. **Pulls the demand book** from the LMS (which loans are due, how much).
2. **Confirms consent** for each borrower (who has agreed to let us read their
   balance). Consent comes from origination, from a live AA journey, or from a
   bulk upload.
3. **Runs the check** — Sherlock pulls a fresh balance for every consented loan.
4. **Sorts the book** into four buckets: comfortably covered, thinly covered,
   **going to bounce**, and *no usable data*.
5. **Works the shortfall list** — calls, promises‑to‑pay, nudges, re‑timing.
6. **Records the outcome** after the debit, and scores its own prediction so it
   gets better each month.

Every balance read costs money (it's a real, billed vendor call), so the system
is careful: it only pulls with valid consent, caps the calls, and never guesses
when it has no data.

---

## Principles we hold (please keep these)

These aren't style preferences — they're load‑bearing. Breaking one usually
breaks correctness or trust.

- **Honesty over optimism.** A missing or failed read is **never** treated as
  "probably fine." It goes to `NO_DATA` and is surfaced as *blind exposure* with
  the reason. We would rather say "we don't know" than be confidently wrong.
- **Live‑only, no fake data in real tables.** There is no mock/live toggle at
  runtime. Sample fixtures may only ever be written to the in‑memory test
  database. A misconfigured vendor **fails loudly** — it must never silently
  serve sample data into production.
- **Money is guarded.** Vendor calls are billed. Guardrails (≤4 initiates per
  mandate/month, ≤100 billed calls/day, reserved atomically) are not optional.
  If you add a new outbound call, account for its cost and make failures
  refundable where the call never reached the vendor.
- **Everything is auditable.** Consent changes, bucket overrides, outcomes,
  scheduler toggles, and sign‑ins are **append‑only** history. The current
  state is a single row; the story of how it got there lives beside it. Don't
  mutate history.
- **One timezone: IST.** Timestamps are stored *and* compared in `Asia/Kolkata`
  (fixed +5:30, no DST). Never introduce UTC — the daily caps and month gates
  compare stored values against "today" and assume one zone.
- **One data layer.** All database access goes through `mongostore.py`. Don't
  reach into Mongo from elsewhere.
- **Secrets never enter the repo.** Config is in `.env` (gitignored); the repo
  ships `.env.example` with the keys only. Rotate anything that leaks.

---

## Stack

- **Backend:** Python + FastAPI, session auth, role‑based access control.
- **Store:** MongoDB (Atlas in production; in‑memory `mongomock` for tests).
- **Frontend:** a single‑file vanilla‑JS console (`static/index.html`) — no build
  step, no framework. It is scanned and operated, not read top‑to‑bottom.
- **Integrations (all live):** Digitap (Account Aggregator — consent + balances),
  Engrow (LOS — origination/portfolio), Encore (LMS — collections demand).

No heavy dependencies, no build pipeline. You can run the whole thing from a
checkout plus a `.env`.

---

## Running it

```bash
# 1. config
cp .env.example .env      # then fill in Mongo + vendor credentials

# 2. offline regression test (no DB, no vendor spend) — run this after ANY
#    change to the engine (checker/cycle/mongostore/aa_report/rbac)
python3 .claude/skills/smoke-test/scripts/smoke_cycle.py

# 3. the app (see .claude/skills for the preview/run helpers)
```

The `.claude/skills/` folder holds the repeatable operations we actually use:
`smoke-test` (offline engine check), `db-health` (Atlas connectivity/state),
`sync-preview` (mirror + run the app), `run-cycle` (drive a full monthly cycle).
Start there before reinventing a workflow.

---

## Where to look

| If you're touching… | Start in |
|---|---|
| classification / buckets / outcomes | `cycle.py` |
| consent eligibility, AA pull loop, guardrails | `checker.py` |
| a Digitap/LOS/LMS call | `aa_live.py` / `los_client.py` / `lms_client.py` |
| parsing an AA report | `aa_report.py` |
| anything that writes to the DB | `mongostore.py` |
| an HTTP endpoint or permission | `app.py` + `rbac.py` |
| the UI | `static/index.html` |
| the tables themselves | [`ARCHITECTURE.md`](ARCHITECTURE.md) §4 |

---

## Honest state of things

Transparency for collaborators means naming the rough edges, not just the wins:

- **The console is one big file.** `static/index.html` is a single ~4k‑line SPA.
  It works and is fast to change, but it's dense and has grown organically. A
  restructure into a **Work / Setup / Records** layout has been discussed but not
  done. Be careful adding to it — function‑name collisions are a real hazard.
- **Vendor endpoints can be behind tunnels in non‑prod.** If a live call
  intermittently returns gateway/offline errors, suspect the tunnel/endpoint
  before the code — the request shape is inspectable as a copy‑pasteable cURL in
  the Live Pull view (auth redacted).
- **`samples/` is not in the repo.** The bundled AA fixtures contain
  real‑looking PII and are test‑only; they're gitignored. The offline smoke test
  needs them locally.
- **Production hardening is ongoing.** RBAC, audit trails, and the money‑path
  guardrails are in place; change the default admin password, keep
  `SHOW_DEMO_LOGINS` off in production, and rotate credentials on deploy.

---

## Contributing

- Keep the principles above intact — if a change appears to violate one, say so
  and explain why it's still correct.
- Run the offline smoke test before proposing engine changes.
- Prefer editing `mongostore.py` for data concerns; prefer small, honest UI
  additions over new top‑level surfaces.
- Write timestamps with `mongostore._now()` (IST). Never `datetime.utcnow()`.
- No secrets, no real PII, in commits.
