"""Monthly pre-NACH cycle orchestration.

start_cycle() enumerates the eligible portfolio from the LMS, fires the
existing checker.start_check() per loan (so accounts, pulls, api_logs, the
report viewer and the consent flow all reuse the single-check machinery),
then classifies each customer once their pulls land:

  bucket by repayment-account balance / due amount
    COMFORT   (Cushioned)   ratio >= 2
    WATCH     (Stretched)   1 <= ratio < 2    -> tele-calling list
    SHORTFALL (Shortfall)   ratio < 1         -> field-team list
    NO_DATA   (No signal)   no repay balance  -> consent CTA

The aggregate ratio across all pulled accounts is stored as a secondary
signal. Derived output (ratios, bucket, analytics, tags) goes to the
processed_data collection — one doc per customer per cycle, no raw reports.
"""
import csv
import io
import json
import os
import re
import threading
import zlib
from datetime import date
from typing import Optional

# Below this delivered-coverage %, a run is flagged red (economically near-blind).
COVERAGE_FLOOR_PCT = float(os.getenv("COVERAGE_FLOOR_PCT", "40"))

import checker
import digitap
import mongostore as store

BUCKET_DISPLAY = {
    "COMFORT": "Cushioned",
    "WATCH": "Stretched",
    "SHORTFALL": "Shortfall",
    "NO_DATA": "No signal",
}


class CycleError(Exception):
    """Recoverable, user-facing cycle problem."""


class CycleBusy(CycleError):
    """A cycle is still executing."""


class CycleNeedsConfirm(CycleError):
    """A cycle for this month already exists; explicit confirmation required."""


# ---------------------------------------------------------------------------
# Cycle lifecycle
# ---------------------------------------------------------------------------
def start_cycle(triggered_by: str, confirm: bool = False, source: str = None) -> dict:
    """source: None -> env default (mock demo); "lms" -> the CRO Sherlock Check
    (presentment population + consent manager + real aa_live periodic pulls)."""
    month = date.today().strftime("%Y-%m")
    # Serialize cycle creation with an atomic month-lock: the guards below are reads, and
    # two concurrent Run-Sherlock-Check clicks used to both pass them and each start a
    # full-book run — double-billing every eligible borrower. See audit #3.
    if not store.acquire_month_lock("cycle_start", month):
        raise CycleBusy("Another Sherlock run is starting right now — try again in a moment.")
    try:
        active = store.active_cycle()
        if active:
            raise CycleBusy(f"Cycle {active['id']} ({active['month']}) is still running.")
        existing = store.cycle_for_month(month)
        if existing and not confirm:
            raise CycleNeedsConfirm(
                f"A cycle for {month} already exists (id {existing['id']}). "
                "Re-running will consume fresh AA attempts for every account.")
        cycle_id = store.create_cycle(month, triggered_by, source=source)
    finally:
        store.release_month_lock("cycle_start", month)
    threading.Thread(target=_run_cycle, args=[cycle_id, source], daemon=True).start()
    return store.get_cycle(cycle_id)


def _run_cycle(cycle_id: int, source: str = None) -> None:
    try:
        portfolio = checker.lookup_portfolio(source=source)
    except Exception as e:  # noqa: BLE001
        store.update_cycle(cycle_id, status="ERROR", error=f"{type(e).__name__}: {e}",
                           finished_at=store._now())
        return
    totals = {"eligible": len(portfolio), "items_created": 0, "repay_consent_ok": 0,
              "repay_consent_expired": 0, "repay_not_linked": 0,
              "pulls_initiated": 0, "repay_pulls": 0, "pulls_blocked": 0}
    for loan in portfolio:
        item_id = store.add_cycle_item(cycle_id, loan)
        totals["items_created"] += 1
        run = checker.start_check(str(loan.get("loan_id")),
                                  cycle_ctx={"cycle_id": cycle_id, "cycle_item_id": item_id},
                                  source=source)
        store.update_cycle_item(item_id, run_id=run["id"], status="PULLING")

        repay = next((a for a in run.get("accounts", []) if a.get("is_repayment")), None)
        cs = (repay or {}).get("consent_status")
        # "consent OK" must mean actually pullable — an ACTIVE ONETIME consent has no
        # main_txn_id and can never be pulled, so requiring the txn keeps coverage_pct
        # honest (matches cm_stats/pre-flight 'eligible'; see bug K4).
        if cs in ("ACTIVE", "NEARING_EXPIRY") and (repay or {}).get("main_txn_id"):
            totals["repay_consent_ok"] += 1
        elif cs == "EXPIRED":
            totals["repay_consent_expired"] += 1
        else:
            totals["repay_not_linked"] += 1
        for p in run.get("pulls", []):
            if p.get("status") == "CAPPED":
                totals["pulls_blocked"] += 1
            else:
                totals["pulls_initiated"] += 1
                if p.get("is_repayment"):
                    totals["repay_pulls"] += 1

        if run.get("status") != "PENDING":
            classify_item(item_id)  # nothing in flight -> classify now (NO_DATA reasons)
        store.update_cycle(cycle_id, totals=dict(totals))
    store.update_cycle(cycle_id, status="COLLECTING", totals=totals)
    _finalize_cycle_if_done(cycle_id)


def on_run_done(run_id: int) -> None:
    """checker RUN_DONE_HOOK — classify the cycle item whose *current* run this is."""
    item = store.find_item_by_run(run_id)
    if item:
        classify_item(item["id"])


def _finalize_cycle_if_done(cycle_id: int) -> None:
    cyc = store.get_cycle(cycle_id)
    if not cyc or cyc.get("status") != "COLLECTING":
        return
    items = store.cycle_items(cycle_id)
    # all([]) is True — an empty portfolio must finalize to DONE, not stay
    # COLLECTING forever (which would raise CycleBusy on every future run). See bug C3.
    if all(i.get("status") in ("DONE", "ERROR") for i in items):
        store.update_cycle(cycle_id, status="DONE", finished_at=store._now())
        snapshot_predictions(cycle_id)


def snapshot_predictions(cycle_id: int) -> int:
    """Freeze each item's prediction-of-record for the cycle. Idempotent
    (write-once per loan), so it safely backfills cycles finalized before this
    existed and is a no-op once taken — later retries never rewrite it."""
    cyc = store.get_cycle(cycle_id) or {}
    n = 0
    for it in store.cycle_items(cycle_id):
        made = store.snapshot_prediction({
            "cycle_id": cycle_id, "loan_id": it["loan_id"], "month": cyc.get("month"),
            "customer_name": it.get("customer_name"),
            "bucket": it.get("override_bucket") or it.get("bucket"),
            "computed_bucket": it.get("bucket"),
            "ratio": it.get("ratio"), "risk_score": it.get("risk_score"),
            "risk_factors": it.get("risk_factors"), "emi_amount": it.get("emi_amount"),
        })
        n += 1 if made else 0
    return n


# ---------------------------------------------------------------------------
# Classification (idempotent — safe to re-run for the same item)
# ---------------------------------------------------------------------------
def _present_day(demand_date, default=4):
    """Day-of-month the mandate actually presents on, from the presentment demand_date —
    used for the funding horizon, nudge copy, scoring and outcome keys. Falls back to the
    4th ONLY when no per-loan date is available (a real book presents across many days)."""
    s = str(demand_date or "").strip()
    if not s:
        return default
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s)  # ISO yyyy-mm-dd
    if m:
        return int(m.group(3))
    m = re.search(r"\b(\d{1,2})\b", s)  # leading day (e.g. '07-Jul-2026' or bare '7')
    if m and 1 <= int(m.group(1)) <= 31:
        return int(m.group(1))
    return default


def _aa_risk_read(raw, hint_last4=None, hint_ifsc=None, emi=None, present_day=None):
    """Distill the full AA report into the risk signals the floor actually needs but the
    bare balance-vs-EMI bucket hides: empirical mandate-day coverage, the cross-holder
    cushion trap, prior bounces, competing recurring obligations, cash-drain, salary timing
    and a ready call script. Returns (aa_risk dict, tags, bounce_prob) — or (None, [], None)
    when the report isn't a parseable AA payload (e.g. the mock-cycle digitap shape)."""
    try:
        import aa_report
        an = aa_report.analyse(raw, hint_last4=hint_last4, hint_ifsc=hint_ifsc, emi=emi,
                               present_day=present_day)
        if not an or not an.get("mandate"):
            return None, [], None
        mand = an.get("mandate") or {}
        fund = an.get("funding") or {}
        bnc = an.get("bounce") or {}
        exact = an.get("exact_date") or {}
        pb = an.get("prior_bounce") or {}
        cd = an.get("cash_drain") or {}
        inc = an.get("income") or {}
        plan = an.get("present_plan") or {}
        cs = an.get("call_script") or {}
        biq = an.get("banking_iq") or {}
        # Biggest competing recurring outflow on the BORROWER'S OWN mandate account only —
        # a cross-holder (relative's) account's spending must not be charged against this
        # borrower while its balance is (correctly) excluded from cover. See audit
        # (COMPETING_EMI from other holders).
        mand_last4 = mand.get("last4")
        own_outs = [x for x in (biq.get("recurring_out") or [])
                    if not mand_last4 or str(x.get("acct") or "") == str(mand_last4)]
        outs = sorted(own_outs, key=lambda x: -(x.get("monthly") or 0))
        top = outs[0] if outs else None
        prior = (pb.get("iw_bounced") or 0) + (pb.get("ecs_bounced") or 0)
        cov_pct = exact.get("coverage_pct")
        # Behaviour/fraud flags that actually fired (result != not_applicable), most-hit first.
        fq = an.get("fraud_queue") or []
        fraud = [{"code": f.get("code"), "type": f.get("type"), "hits": f.get("hits")}
                 for f in sorted(fq, key=lambda x: -(x.get("hits") or 0)) if f.get("type")][:3]
        emp_mismatch = bool(inc.get("employment_mismatch"))
        risk = {
            "bounce_prob": bnc.get("probability"),
            "factors": (bnc.get("factors") or [])[:4],
            "mandate_last4": mand.get("last4"), "mandate_day": mand.get("debit_day"),
            "lender": mand.get("lender"), "foir_pct": fund.get("foir_pct"),
            "coverage": (f"{exact.get('covered')}/{exact.get('months')}"
                         if exact.get("months") else None),
            "coverage_pct": cov_pct,
            "cross_holder": bool(fund.get("cross_holder_warning")),
            "cross_holder_note": fund.get("cross_holder_warning"),
            "prior_bounces": prior,
            "cash_drain": cd.get("urgency"),
            "competing": ({"name": top.get("name"), "monthly": top.get("monthly")}
                          if top and (top.get("monthly") or 0) >= 0.5 * (emi or 1e12) else None),
            "salary_day": plan.get("salary_day"), "recommended_nach": plan.get("recommended_nach"),
            "salary_conflict": bool(plan.get("salary_conflict")),
            "call_script": cs.get("script"), "fund_account": cs.get("fund_account"),
            "income_flags": (inc.get("flags") or [])[:2],
            "fraud": fraud, "employment_mismatch": emp_mismatch,
        }
        tags = []
        if risk["cross_holder"]:
            tags.append("CROSS_HOLDER")
        if prior >= 1:
            tags.append("REPEAT_BOUNCER")
        if cov_pct is not None and cov_pct < 50:
            tags.append("LOW_MANDATE_COVERAGE")
        # Tag on the SIGNAL, not the urgency wording: cash_drain returns None for benign
        # accounts, and the MOST urgent case ('same-day / pre-salary contact', instant
        # debit after salary) used a string outside ('elevated','high') and so never
        # tagged. See audit (CASH_DRAIN tag never fires for instant-drain).
        if cd and (cd.get("instant_debit_after_salary")
                   or str(cd.get("urgency") or "").lower() in ("elevated", "high", "same-day / pre-salary contact")):
            tags.append("CASH_DRAIN")
        if risk["competing"]:
            tags.append("COMPETING_EMI")
        if inc.get("flags"):
            tags.append("INCOME_UNSTABLE")
        if plan.get("salary_conflict"):
            tags.append("SALARY_AFTER_MANDATE")
        if fraud:
            tags.append("FRAUD_REVIEW")
        if emp_mismatch:
            tags.append("EMPLOYMENT_MISMATCH")
        return risk, tags, bnc.get("probability")
    except Exception:  # noqa: BLE001 — enrichment only, never break classification
        return None, [], None


def classify_item(item_id: int) -> dict:
    item = store.get_cycle_item(item_id)
    if not item:
        raise CycleError(f"Cycle item {item_id} not found")
    run = store.get_run(item["run_id"]) if item.get("run_id") else None

    emi = None
    for candidate in ((run or {}).get("emi_amount"), item.get("emi_amount")):
        try:
            emi = float(candidate)
            break
        except (TypeError, ValueError):
            continue

    # NACH presents the DEMAND (EMI + arrears for overdue loans), not the bare EMI — so the
    # cover ratio must be measured against demand. Fall back to EMI only when demand is absent.
    demand = None
    try:
        demand = float(item.get("demand_amount"))
    except (TypeError, ValueError):
        demand = None
    denom = demand if (demand and demand > 0) else emi
    denom_basis = "demand" if (demand and demand > 0) else "emi"
    present_day = _present_day(item.get("demand_date"))

    accounts = (run or {}).get("accounts", [])
    pulls = (run or {}).get("pulls", [])
    acct_by_id = {a.get("id"): a for a in accounts}
    repay_acct = next((a for a in accounts if a.get("is_repayment")), None)
    repay_pull = next((p for p in reversed(pulls) if p.get("is_repayment")), None)

    repay_balance = None
    if repay_pull and repay_pull.get("status") == "RETRIEVED":
        repay_balance = repay_pull.get("available_balance")

    bucket, reason, ratio = "NO_DATA", "PULL_FAILED", None
    if run is None or run.get("status") == "ERROR":
        reason = "LOOKUP_ERROR"
    elif not denom:
        # 0/None EMI-and-demand would ZeroDivisionError at the ratio line below and, because
        # the RUN_DONE hook swallows exceptions, wedge the item at PULLING and the whole cycle
        # at COLLECTING (CycleBusy forever). See bug C2.
        reason = "EMI_MISSING"
    elif repay_acct is None:
        reason = "LOOKUP_ERROR"
    elif not repay_acct.get("aa_enabled"):
        # Prefer the precise consent-gap reason (NO_CONSENT / EXPIRED / ONETIME-only) over the
        # generic REPAY_NOT_AA so the CRO knows exactly why a borrower is blind. See flow-audit.
        cst = repay_acct.get("consent_state")
        reason = {"NO_CONSENT": "NO_CONSENT", "EXPIRED": "CONSENT_EXPIRED",
                  "NOT_PULLABLE": "CONSENT_NOT_PULLABLE"}.get(cst, "REPAY_NOT_AA")
    elif repay_acct.get("consent_status") == "EXPIRED":
        reason = "CONSENT_EXPIRED"
    elif not repay_acct.get("main_txn_id"):
        reason = "NO_TXN_ID"
    elif repay_pull and repay_pull.get("status") == "CAPPED":
        reason = "CAPPED"
    elif repay_balance is not None:
        ratio = float(repay_balance) / denom  # cover vs the amount that actually presents
        bucket = "COMFORT" if ratio >= 2.0 else ("WATCH" if ratio >= 1.0 else "SHORTFALL")
        reason = "OK"

    # Aggregate across every retrieved pull — the "has funds elsewhere" signal.
    retrieved = [p for p in pulls if p.get("status") == "RETRIEVED" and p.get("available_balance") is not None]
    agg_balance = round(sum(float(p["available_balance"]) for p in retrieved), 2) if retrieved else None
    agg_ratio = (agg_balance / denom) if (agg_balance is not None and denom) else None

    # A CAPPED re-pull carries NO new information. If retry_item stashed a real last-known
    # classification, RESTORE it instead of degrading SHORTFALL/WATCH to NO_DATA on
    # presentation eve — reason stays CAPPED so the guardrail state remains visible. Audit #5.
    restored_lk = None
    if reason == "CAPPED" and (item.get("last_known") or {}).get("bucket"):
        restored_lk = item["last_known"]
        bucket = restored_lk["bucket"]
        ratio = restored_lk.get("ratio")
        repay_balance = restored_lk.get("repay_balance")
        agg_balance = restored_lk.get("agg_balance")
        agg_ratio = restored_lk.get("agg_ratio")

    # Analytics from the repayment report (fallback: first retrieved account).
    analytics, tags, aa_risk = None, [], None
    src_pull = repay_pull if (repay_pull and repay_pull.get("status") == "RETRIEVED") else \
        (retrieved[0] if retrieved else None)
    aa_bounce_prob = None
    if src_pull:
        full = store.get_pull(src_pull["id"])  # includes raw_report_json
        raw = (full or {}).get("raw_report_json")
        try:
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
            analytics = build_analytics(digitap.parse_report(data), emi)
        except Exception:  # noqa: BLE001
            analytics = None
        # Rich AA risk read (empirical mandate-day coverage, cross-holder trap, prior bounces,
        # competing obligations, cash-drain, salary timing, call script) — the signal the bare
        # balance-vs-EMI bucket hides. ONLY for real AA payloads (top-level 'banks'); the legacy
        # mock-cycle report shape is skipped so it adds no latency there. Enrichment only.
        try:
            data2 = json.loads(raw) if isinstance(raw, str) else (raw or {})
            if isinstance(data2, dict) and data2.get("banks"):
                aa_risk, aa_tags, aa_bounce_prob = _aa_risk_read(
                    data2, hint_last4=(repay_acct or {}).get("account_ref"),
                    hint_ifsc=(repay_acct or {}).get("ifsc"), emi=denom,
                    present_day=present_day)  # score the loan's REAL presentation day
                tags.extend(aa_tags)
        except Exception:  # noqa: BLE001
            pass
    if analytics:
        if (analytics.get("emi_txn_count_3m") or 0) > 0:
            tags.append("OTHER_EMIS")
        if emi and analytics.get("inflow_months_covering_emi") == 0:
            tags.append("LOW_INFLOWS")
        if analytics.get("fraud_flags"):
            tags.append("FRAUD_FLAGS")
    if repay_acct and repay_acct.get("consent_status") == "NEARING_EXPIRY":
        tags.append("NEARING_EXPIRY")

    # AA-HONEST DOWNGRADE (F1): when the empirical AA read is severe, the balance-today bucket
    # is a lie (flush today, near-certain bounce) — move the borrower one queue toward action
    # and record the raw balance bucket so the adjustment is auditable.
    bucket_by_balance = bucket
    aa_downgrade = None
    if aa_risk and bucket in ("COMFORT", "WATCH"):
        prob = aa_risk.get("bounce_prob") or 0
        covp = aa_risk.get("coverage_pct")
        prior_b = aa_risk.get("prior_bounces") or 0
        low_cov = covp is not None and covp < 50
        severe = (prob >= 80) or (aa_risk.get("cross_holder") and low_cov) or (prior_b >= 2 and low_cov)
        if severe:
            _order = ["COMFORT", "WATCH", "SHORTFALL"]
            bucket = _order[min(_order.index(bucket) + 1, 2)]
            why = ([f"{prob}% AA bounce"] if prob else []) \
                + (["cross-holder cushion"] if aa_risk.get("cross_holder") else []) \
                + ([f"covers only {aa_risk.get('coverage')}"] if low_cov and aa_risk.get("coverage") else []) \
                + ([f"{prior_b} prior bounces"] if prior_b >= 2 else [])
            aa_downgrade = f"{bucket_by_balance}→{bucket}: " + ", ".join(why)
            tags.append("AA_DOWNGRADE")
    if restored_lk:
        tags.append("CAPPED_KEPT_LAST_KNOWN")
    tags = list(dict.fromkeys(tags))  # de-dupe, preserve order

    # Bounce probability: prefer the empirical AA model (mandate-day coverage + prior bounces +
    # timing) when we have a real AA report; else the statement-pattern estimate keyed to the
    # loan's REAL presentation day (not a hardcoded 4th).
    risk_score, risk_factors = None, None
    if aa_bounce_prob is not None:
        risk_score = aa_bounce_prob
        risk_factors = (aa_risk or {}).get("factors")
    else:
        try:
            import insights as _insights  # lazy: insights imports this module
            risk_score, risk_factors = _insights.bounce_probability(
                item["loan_id"], bucket=bucket, ratio=ratio, present_day=present_day)
        except Exception:  # noqa: BLE001
            pass

    # Timing risk: looks affordable on the run-day snapshot (COMFORT/WATCH) yet the statement
    # pattern flags elevated bounce odds — i.e. flush today, likely drained by the due date.
    # Tag it so the pre-presentation confirmation sweep re-checks it instead of dropping it.
    timing_risk = bucket in ("COMFORT", "WATCH") and (risk_score or 0) >= 55
    if timing_risk:
        tags.append("TIMING_RISK")

    restore_fields = {}
    if restored_lk:  # a CAPPED retry also resurrects the override it wiped
        restore_fields = {k: restored_lk.get(k) for k in
                          ("override_bucket", "override_reason", "override_by", "override_at")
                          if restored_lk.get(k)}
    store.update_cycle_item(
        item_id, status="DONE", bucket=bucket, bucket_reason=reason,
        ratio=round(ratio, 4) if ratio is not None else None,
        repay_balance=repay_balance,
        repay_bank=(repay_acct or {}).get("bank_name"),
        repay_account_ref=(repay_acct or {}).get("account_ref"),
        agg_balance=agg_balance,
        agg_ratio=round(agg_ratio, 4) if agg_ratio is not None else None,
        risk_score=risk_score, risk_factors=risk_factors,
        denominator=denom, denom_basis=denom_basis, present_day=present_day,
        timing_risk=timing_risk, tags=tags, aa_risk=aa_risk,
        bucket_by_balance=bucket_by_balance, aa_downgrade=aa_downgrade,
        last_known=None, **restore_fields,
    )
    item = store.get_cycle_item(item_id)

    store.save_processed({
        "cycle_id": item["cycle_id"], "cycle_item_id": item_id, "loan_id": item["loan_id"],
        "month": (store.get_cycle(item["cycle_id"]) or {}).get("month"),
        "run_id": item.get("run_id"), "emi_amount": emi,
        "bucket": bucket, "bucket_reason": reason,
        "effective_bucket": item.get("override_bucket") or bucket,
        "repay": {
            "bank_name": (repay_acct or {}).get("bank_name"),
            "account_ref": (repay_acct or {}).get("account_ref"),
            "balance": repay_balance, "currency": (repay_pull or {}).get("currency"),
            "ratio": round(ratio, 4) if ratio is not None else None,
        },
        "aggregate": {
            "total_balance": agg_balance,
            "ratio": round(agg_ratio, 4) if agg_ratio is not None else None,
            "accounts": [{
                "bank_name": p.get("bank_name"),
                "account_ref": acct_by_id.get(p.get("account_id"), {}).get("account_ref"),
                "is_repayment": bool(p.get("is_repayment")),
                "balance": p.get("available_balance"), "pull_status": p.get("status"),
            } for p in pulls],
        },
        "analytics": analytics, "tags": tags,
    })

    store.recount_buckets(item["cycle_id"])
    _finalize_cycle_if_done(item["cycle_id"])
    return item


_MONTH_NAMES = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}


def _month_key(m):
    """Sortable key for month labels: 'October 2025' and '2025-10' both work.
    Plain string sort puts 'April 2026' before 'August 2025' — chronology matters."""
    s = str(m or "").strip()
    parts = s.split()
    if len(parts) == 2 and parts[0] in _MONTH_NAMES and parts[1].isdigit():
        return (int(parts[1]), _MONTH_NAMES[parts[0]])
    if len(s) >= 7 and s[:4].isdigit() and s[5:7].isdigit():
        return (int(s[:4]), int(s[5:7]))
    return (0, 0)


def build_analytics(parsed: dict, emi=None) -> dict:
    """Pure derivation over digitap.parse_report() output: spends, other-EMI
    outflows, narration tagging, inflow cover and headline AA signals."""
    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    monthly = sorted(parsed.get("monthly") or [], key=lambda m: _month_key(m.get("month")))
    txns = parsed.get("transactions") or []
    loans = parsed.get("loans") or []
    signals = parsed.get("signals") or {}
    last3 = monthly[-3:]
    last3_keys = {_month_key(m.get("month")) for m in last3}

    credits_3m = sum(_f(m.get("credits")) or 0 for m in last3)
    debits_3m = sum(_f(m.get("debits")) or 0 for m in last3)

    dcat, ccat = {}, {}
    for t in txns:
        amt = _f(t.get("amount"))
        if amt is None:
            continue
        c = (str(t.get("category") or "").strip() or "Uncategorised")
        if amt < 0:
            e = dcat.setdefault(c, {"category": c, "total": 0.0, "count": 0})
            e["total"] += abs(amt)
            e["count"] += 1
        elif amt > 0:
            ccat[c] = ccat.get(c, 0.0) + amt
    top_debit = sorted(dcat.values(), key=lambda x: -x["total"])[:5]
    for e in top_debit:
        e["total"] = round(e["total"], 2)

    loan_debits = [l for l in loans if (_f(l.get("amount")) or 0) < 0]
    loan_months = sorted({str(l.get("date") or "")[:7] for l in loan_debits if l.get("date")})
    latest = loan_months[-1] if loan_months else None
    emi_outflow_latest = round(sum(abs(_f(l.get("amount")) or 0) for l in loan_debits
                                   if str(l.get("date") or "").startswith(latest)), 2) if latest else 0.0

    hints = {}
    for l in loans:
        n = re.sub(r"[\d/:\-_.]+", " ", str(l.get("narration") or ""))
        n = re.sub(r"\s+", " ", n).strip()[:48]
        if n:
            hints[n] = hints.get(n, 0) + 1
    lender_hints = [k for k, _ in sorted(hints.items(), key=lambda kv: -kv[1])[:5]]

    def _sig(*needles):
        for k, v in signals.items():
            kl = str(k).lower()
            if all(nd in kl for nd in needles):
                return v
        return None

    return {
        "credits_3m": round(credits_3m, 2), "debits_3m": round(debits_3m, 2),
        "net_3m": round(credits_3m - debits_3m, 2),
        "top_debit_categories": top_debit,
        "top_credit_category": max(ccat, key=ccat.get) if ccat else None,
        "emi_outflow_latest_month": emi_outflow_latest,
        "emi_txn_count_3m": sum(1 for l in loan_debits if _month_key(str(l.get("date") or "")[:7]) in last3_keys),
        "other_lender_hints": lender_hints,
        "emi_bounces_3m": _sig("emi bounce"),
        "avg_eod_3m": _sig("avg eod"),
        "min_balance_1m": _sig("min balance"),
        "credits_6m": _sig("credits", "6m"),
        "employment_type": _sig("employment"),
        "nach_date_recommendation": _sig("recommended", "nach"),
        "inflow_months_covering_emi": (sum(1 for m in last3 if (_f(m.get("credits")) or 0) >= float(emi))
                                       if emi else None),
        "fraud_flags": parsed.get("fraud") or [],
    }


# ---------------------------------------------------------------------------
# Spend analyser — where the customer's money actually goes
# ---------------------------------------------------------------------------
_PAYEE_SKIP = {"UPI", "DR", "CR", "NACH", "ACH", "POS", "IMPS", "NEFT", "INET", "CCPS",
               "DDF", "TO", "TRANSFER", "PAYMENT", "PMT", "REF", "TXN", "BY", "MOB"}


def _payee_of(txn) -> str:
    """Best-effort counterparty from category/narration. Bank narrations are
    noisy (UPI/DR/…/NAME/…, 'NACH TVSCreditServicesLtd', 'Transfer to x@ybl'),
    so: prefer an explicit 'Transfer to …' category, else take the first non-
    boilerplate, non-numeric tokens of the narration."""
    cat = str(txn.get("category") or "").strip()
    if cat.lower().startswith("transfer to "):
        return cat[12:].strip()[:36] or "Unknown"
    narr = str(txn.get("narration") or "").strip()
    picked = []
    for t in re.split(r"[\s/|:_-]+", narr):
        if not t:
            continue
        digits = sum(ch.isdigit() for ch in t)
        if t.upper() in _PAYEE_SKIP or t.isdigit() or (len(t) > 3 and digits > len(t) // 2):
            if picked:
                break
            continue
        picked.append(t)
        if len(picked) == 3:
            break
    if picked:
        return " ".join(picked)[:36]
    return (cat or "Other")[:36]


def latest_parsed_report(loan_id: str):
    """(pull_meta, parsed_report) for the customer's latest retrieved AA pull,
    repayment account preferred. Shared by the spend and insights analysers."""
    db = store._db()
    run_ids = [r["id"] for r in db.checks.find({"loan_id": loan_id},
                                               {"_id": 0, "id": 1}).sort("id", -1).limit(15)]
    pull_meta = list(db.pulls.find({"run_id": {"$in": run_ids}, "status": "RETRIEVED"},
                                   {"_id": 0, "id": 1, "is_repayment": 1, "bank_name": 1})
                     .sort("id", -1)) if run_ids else []
    if not pull_meta:
        raise CycleError(f"No retrieved AA report for '{loan_id}' yet — run a check or a cycle first")
    chosen = next((p for p in pull_meta if p.get("is_repayment")), pull_meta[0])
    raw = (store.get_pull(chosen["id"]) or {}).get("raw_report_json")
    data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    return chosen, digitap.parse_report(data)


def spend_analysis(loan_id: str) -> dict:
    """Analyse the customer's latest retrieved AA report (repayment account
    preferred): top payees by outflow, top single spends, category mix,
    monthly in/out."""
    chosen, parsed = latest_parsed_report(loan_id)
    txns = parsed.get("transactions") or []

    def f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    debits = [t for t in txns if (f(t.get("amount")) or 0) < 0]
    credits = [t for t in txns if (f(t.get("amount")) or 0) > 0]
    total_out = round(sum(-f(t["amount"]) for t in debits), 2)
    total_in = round(sum(f(t["amount"]) for t in credits), 2)
    dates = sorted(t.get("date") for t in txns if t.get("date"))

    top_spends = [{"date": t.get("date"), "narration": t.get("narration"),
                   "category": t.get("category"), "payee": _payee_of(t),
                   "amount": round(-f(t["amount"]), 2)}
                  for t in sorted(debits, key=lambda t: f(t["amount"]))[:5]]

    payees = {}
    for t in debits:
        p = _payee_of(t)
        e = payees.setdefault(p, {"payee": p, "total": 0.0, "count": 0})
        e["total"] += -f(t["amount"])
        e["count"] += 1
    top_payees = sorted(payees.values(), key=lambda x: -x["total"])[:8]
    for e in top_payees:
        e["total"] = round(e["total"], 2)
        e["share_pct"] = round(100 * e["total"] / total_out, 1) if total_out else 0

    cats = {}
    for t in debits:
        c = str(t.get("category") or "Uncategorised").strip() or "Uncategorised"
        e = cats.setdefault(c, {"category": c, "total": 0.0, "count": 0})
        e["total"] += -f(t["amount"])
        e["count"] += 1
    categories = sorted(cats.values(), key=lambda x: -x["total"])[:10]
    for e in categories:
        e["total"] = round(e["total"], 2)
        e["share_pct"] = round(100 * e["total"] / total_out, 1) if total_out else 0

    monthly = [{"month": m.get("month"), "credits": m.get("credits"), "debits": m.get("debits")}
               for m in sorted(parsed.get("monthly") or [],
                               key=lambda m: _month_key(m.get("month")))][-6:]

    return {
        "loan_id": loan_id, "bank": chosen.get("bank_name"),
        "is_repayment": bool(chosen.get("is_repayment")), "pull_id": chosen["id"],
        "period": {"from": dates[0] if dates else None, "to": dates[-1] if dates else None},
        "summary": {"total_out": total_out, "total_in": total_in,
                    "net": round(total_in - total_out, 2),
                    "debit_count": len(debits), "credit_count": len(credits),
                    "avg_spend": round(total_out / len(debits), 2) if debits else None,
                    "largest_spend": top_spends[0]["amount"] if top_spends else None},
        "top_spends": top_spends, "top_payees": top_payees,
        "categories": categories, "monthly": monthly,
    }


# ---------------------------------------------------------------------------
# Actions: override / retry / export
# ---------------------------------------------------------------------------
OP_GRACE_DAYS = 3  # actions/engines stay live until max demand_date + this many days


def _cycle_max_demand(cycle_id):
    """Latest demand_date (ISO) across a cycle's items, or None."""
    dds = [str(it.get("demand_date") or "")[:10] for it in store.cycle_items(cycle_id)]
    dds = [d for d in dds if len(d) == 10]
    return max(dds) if dds else None


def _cycle_covers_today(cyc) -> bool:
    """True while the cycle's presentment book is still presenting (+grace)."""
    from datetime import timedelta
    mx = _cycle_max_demand(cyc["id"])
    if not mx:
        return False
    try:
        return date.fromisoformat(mx) + timedelta(days=OP_GRACE_DAYS) >= date.today()
    except ValueError:
        return False


def _operational_cycle():
    """The cycle whose book covers TODAY. A run can happen late in month M for a book
    presenting in M+1 (demand_date next month) — keying strictly to the calendar month
    made the sweep/PTP/sentinel engines skip during the actual pre-NACH window and froze
    the cycle read-only exactly when the floor needs it. Prefer this month's cycle; else
    the newest non-ERROR cycle still presenting. See audit #6."""
    cyc = store.cycle_for_month(date.today().strftime("%Y-%m"))
    if cyc:
        return cyc
    for c in store.list_cycles(6):
        if c.get("status") == "ERROR":
            continue
        if _cycle_covers_today(c):
            return c
    return None


def _require_live_month(item, action: str):
    """History cycles are read-only — the past already happened. 'Live' = the current
    calendar month OR a cycle whose presentment book is still presenting (+grace):
    a July run over an Aug-4 book must stay actionable through the Aug window. Audit #6."""
    cyc = store.get_cycle(item["cycle_id"]) or {}
    if cyc.get("month") == date.today().strftime("%Y-%m"):
        return
    if cyc and _cycle_covers_today(cyc):
        return
    raise CycleError(f"{action} is only allowed while the cycle's book is presenting — history is read-only")


def override_item(item_id: int, bucket: str, reason: str, username: str) -> dict:
    if bucket not in BUCKET_DISPLAY:
        raise CycleError(f"Unknown bucket '{bucket}'")
    if not (reason or "").strip():
        raise CycleError("An override reason is required")
    item = store.get_cycle_item(item_id)
    if not item:
        raise CycleError(f"Cycle item {item_id} not found")
    _require_live_month(item, "Override")
    prev_eff = item.get("override_bucket") or item.get("bucket")
    store.update_cycle_item(item_id, override_bucket=bucket, override_reason=reason.strip(),
                            override_by=username, override_at=store._now())
    store.update_processed(item["cycle_id"], item["loan_id"], effective_bucket=bucket)
    store.log_bucket_event(item["cycle_id"], item_id, item["loan_id"], "OVERRIDE",
                           prev_eff, bucket, reason=reason.strip(), by=username)
    store.recount_buckets(item["cycle_id"])
    return store.get_cycle_item(item_id)


def retry_item(item_id: int, username: str) -> dict:
    """Re-run the AA pull for one customer (a new run through start_check, so
    the monthly attempt cap applies). A fresh pull invalidates any override."""
    item = store.get_cycle_item(item_id)
    if not item:
        raise CycleError(f"Cycle item {item_id} not found")
    _require_live_month(item, "Retry")
    cyc = store.get_cycle(item["cycle_id"]) or {}
    # Stash the last-known classification BEFORE wiping: if this retry comes back CAPPED
    # (attempt cap already exhausted — start_check records a terminal CAPPED pull instead
    # of raising), classify_item restores it rather than destroying a real SHORTFALL/WATCH
    # reading into NO_DATA on presentation eve. See audit #5.
    last_known = None
    if item.get("bucket") and item.get("bucket") != "NO_DATA":
        last_known = {"bucket": item.get("bucket"), "ratio": item.get("ratio"),
                      "repay_balance": item.get("repay_balance"),
                      "agg_balance": item.get("agg_balance"), "agg_ratio": item.get("agg_ratio"),
                      "override_bucket": item.get("override_bucket"),
                      "override_reason": item.get("override_reason"),
                      "override_by": item.get("override_by"), "override_at": item.get("override_at"),
                      "as_of": item.get("updated_at") or store._now()}
    # A live override about to be wiped by this re-pull is recorded as OVERRIDE_CLEARED so
    # the supervisor decision survives in history even if the fresh pull replaces the bucket.
    if item.get("override_bucket"):
        store.log_bucket_event(item["cycle_id"], item_id, item["loan_id"], "OVERRIDE_CLEARED",
                               item.get("override_bucket"), None,
                               reason=f"retry by {username}", by=username)
    run = checker.start_check(str(item["loan_id"]),
                              cycle_ctx={"cycle_id": item["cycle_id"], "cycle_item_id": item_id},
                              source=cyc.get("source"))
    store.update_cycle_item(item_id, run_id=run["id"], status="PULLING",
                            bucket=None, bucket_reason=None, ratio=None, repay_balance=None,
                            agg_balance=None, agg_ratio=None,
                            override_bucket=None, override_reason=None,
                            override_by=None, override_at=None,
                            last_known=last_known)
    if run.get("status") != "PENDING":
        classify_item(item_id)
    else:
        store.recount_buckets(item["cycle_id"])
        cyc = store.get_cycle(item["cycle_id"])
        if cyc and cyc.get("status") == "DONE":
            store.update_cycle(item["cycle_id"], status="COLLECTING", finished_at=None)
    return store.get_cycle_item(item_id)


def export_csv(cycle_id: int, bucket: Optional[str] = None) -> str:
    cyc = store.get_cycle(cycle_id)
    if not cyc:
        raise CycleError(f"Cycle {cycle_id} not found")
    items = store.cycle_items(cycle_id)
    if bucket:
        items = [i for i in items if (i.get("override_bucket") or i.get("bucket")) == bucket]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["cycle_month", "loan_id", "customer_name", "emi_amount", "repay_bank",
                "repay_account_ref", "repay_balance", "ratio", "computed_bucket",
                "effective_bucket", "override_reason", "bucket_reason",
                "agg_balance", "agg_ratio", "status", "run_id"])
    for i in items:
        w.writerow([cyc.get("month"), i.get("loan_id"), i.get("customer_name"), i.get("emi_amount"),
                    i.get("repay_bank"), i.get("repay_account_ref"), i.get("repay_balance"),
                    i.get("ratio"), i.get("bucket"), i.get("override_bucket") or i.get("bucket"),
                    i.get("override_reason"), i.get("bucket_reason"), i.get("agg_balance"),
                    i.get("agg_ratio"), i.get("status"), i.get("run_id")])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Read model for the UI
# ---------------------------------------------------------------------------
def cycle_detail(cycle_id: int) -> dict:
    cyc = store.get_cycle(cycle_id)
    if not cyc:
        raise CycleError(f"Cycle {cycle_id} not found")
    items = store.cycle_items(cycle_id)
    keys = []
    for it in items:
        accounts = store.run_accounts(it["run_id"]) if it.get("run_id") else []
        pulls = store.run_pulls(it["run_id"]) if it.get("run_id") else []
        repay = next((a for a in accounts if a.get("is_repayment")), None)
        repay_pull = next((p for p in reversed(pulls) if p.get("is_repayment")), None)
        it["repay_account_id"] = (repay or {}).get("id")
        # Backfilled history items carry consent_status directly (no run behind them).
        it["consent_status"] = (repay or {}).get("consent_status") or it.get("consent_status")
        it["consent_expiry"] = (repay or {}).get("consent_expiry") or it.get("consent_expiry")
        it["repay_pull_status"] = (repay_pull or {}).get("status")
        it["repay_pull_id"] = (repay_pull or {}).get("id") \
            if (repay_pull or {}).get("status") == "RETRIEVED" else None
        it["account_key"] = checker.account_key_of(it["loan_id"], repay) if repay else None
        it["effective_bucket"] = it.get("override_bucket") or it.get("bucket")
        keys.append(it["account_key"])
    used = store.attempts_summary([k for k in keys if k], cyc.get("month"))
    flags = store.flags_by_loan([it["loan_id"] for it in items])
    nudged = store.nudges_by_item(cycle_id)
    prev_cycle = store._db().cycles.find_one(
        {"month": {"$lt": cyc.get("month")}, "status": "DONE"},
        {"_id": 0, "id": 1, "month": 1}, sort=[("month", -1), ("id", -1)])
    prev_items = {i["loan_id"]: i for i in store.cycle_items(prev_cycle["id"])} if prev_cycle else {}
    for it in items:
        k = it.get("account_key")
        it["attempts_used"] = used.get(k, 0) if k else None
        it["attempts_left"] = max(0, checker.MAX_INITIATES_PER_MONTH - used.get(k, 0)) if k else None
        it["flags"] = flags.get(it["loan_id"], [])
        it["nudges"] = nudged.get(it["id"])
        pv = prev_items.get(it["loan_id"])
        if pv:
            it["prev"] = {"month": prev_cycle["month"],
                          "bucket": pv.get("override_bucket") or pv.get("bucket"),
                          "ratio": pv.get("ratio"), "risk_score": pv.get("risk_score")}
    # Call order: rank each worklist by DEMAND (what actually presents — EMI + arrears;
    # EMI only as fallback) × bounce probability; broken PTPs first. Audit #12.
    for b in ("SHORTFALL", "WATCH", "NO_DATA"):
        queue = [it for it in items if it.get("effective_bucket") == b]
        queue.sort(key=lambda it: (
            0 if (it.get("disposition") or {}).get("status") == "PTP_BROKEN" else 1,
            -((it.get("demand_amount") or it.get("emi_amount") or 0) * (it.get("risk_score") or 50)),
        ))
        for n, it in enumerate(queue, 1):
            it["call_order"] = n
    cyc["items"] = items
    cyc["branches"] = sorted({it.get("branch") for it in items if it.get("branch")})
    cyc["states"] = sorted({it.get("state") for it in items if it.get("state")})
    cyc["max_attempts"] = checker.MAX_INITIATES_PER_MONTH
    cyc["bucket_display"] = BUCKET_DISPLAY
    return cyc


# ---------------------------------------------------------------------------
# Worklist dispositions — the tele-calling / field trail on a case
# ---------------------------------------------------------------------------
DISPOSITION_STATUSES = {
    "CONTACTED": "Contacted",
    "PTP": "Promise to pay",
    "NO_RESPONSE": "No response",
    "PAID": "Paid / topped up",
    "WILL_BOUNCE": "Will bounce",
}


def dispose_item(item_id: int, status: str, remarks: str, ptp_date, username: str) -> dict:
    if status not in DISPOSITION_STATUSES:
        raise CycleError(f"Unknown disposition status '{status}'")
    if status == "PTP" and not ptp_date:
        raise CycleError("A promise-to-pay needs a date")
    item = store.get_cycle_item(item_id)
    if not item:
        raise CycleError(f"Cycle item {item_id} not found")
    if status == "PTP":
        # A promise dated on/after the presentation day confirms nothing for this cycle and
        # would burn a billed re-pull AFTER the money already bounced. Force it earlier.
        dd = str(item.get("demand_date") or "")[:10]
        pd = str(ptp_date or "")[:10]
        if dd and pd and pd >= dd:
            raise CycleError(f"Promise date {pd} is on/after the presentation date {dd} — a "
                             f"promise must land BEFORE the auto-debit. Pick an earlier date.")
    _require_live_month(item, "Logging a disposition")
    entry = store.add_disposition(item["cycle_id"], item_id, item["loan_id"],
                                  item.get("override_bucket") or item.get("bucket"),
                                  status, (remarks or "").strip(), ptp_date, username)
    store.update_cycle_item(item_id, disposition={
        "status": status, "remarks": (remarks or "").strip(),
        "ptp_date": ptp_date, "by": username, "at": entry["created_at"],
    })
    return store.get_cycle_item(item_id)


def recommend_retime(item_id: int, date=None, username: str = "cro") -> dict:
    """Record a 'move the NACH to the borrower's income date' recommendation for ops to action
    in the mandate system (F4). When salary lands AFTER the mandate day, re-timing the debit is
    the cleanest prevention. Date defaults to the AA-recommended window."""
    item = store.get_cycle_item(item_id)
    if not item:
        raise CycleError(f"Cycle item {item_id} not found")
    aa = item.get("aa_risk") or {}
    d = (str(date or "").strip()) or aa.get("recommended_nach") or None
    store.update_cycle_item(item_id, retime={
        "date": d, "salary_day": aa.get("salary_day"), "mandate_day": aa.get("mandate_day"),
        "by": username, "at": store._now()})
    return store.get_cycle_item(item_id)


# ---------------------------------------------------------------------------
# Nudges — one-tap WhatsApp/SMS drafts (mock send; every nudge is a touch)
# ---------------------------------------------------------------------------
NUDGE_CHANNELS = ("WHATSAPP", "SMS")


def _ord(n):
    return "th" if 11 <= (n % 100) <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def compose_nudge(item: dict) -> str:
    demand = item.get("demand_amount") or item.get("emi_amount") or 0
    short = max(0.0, demand - (item.get("repay_balance") or 0))
    day = _present_day(item.get("demand_date"))
    return (f"Dear {item.get('customer_name') or 'customer'}, your Prayaan auto-debit of ₹{demand:,.0f} "
            f"is due on the {day}{_ord(day)}. Your {item.get('repay_bank') or 'bank'} account is short by "
            f"₹{short:,.0f}. Please top up before the {day}{_ord(day)} to avoid bounce charges. "
            f"Pay now: upi://pay?pa=prayaan@icici&am={short:.0f} — Team Prayaan")


def send_nudge(item_id: int, channel: str, message: str, username: str) -> dict:
    if channel not in NUDGE_CHANNELS:
        raise CycleError(f"Unknown channel '{channel}'")
    item = store.get_cycle_item(item_id)
    if not item:
        raise CycleError(f"Cycle item {item_id} not found")
    _require_live_month(item, "Nudging")
    if "DO_NOT_CALL" in store.loan_flags(item["loan_id"]):
        raise CycleError("Customer is flagged DO_NOT_CALL — nudges are blocked")
    demand = item.get("demand_amount") or item.get("emi_amount") or 0
    short = round(max(0.0, demand - (item.get("repay_balance") or 0)), 2)
    return store.add_nudge(item["cycle_id"], item_id, item["loan_id"], channel,
                           (message or "").strip() or compose_nudge(item), short, username)


# ---------------------------------------------------------------------------
# PTP follow-up engine — a promise is checked, not filed
# ---------------------------------------------------------------------------
def run_ptp_followup() -> str:
    """For every PTP whose date has arrived: re-pull the balance (attempt-cap
    aware — a capped account is judged on its last known data), mark the
    promise KEPT or BROKEN, and put broken ones back on top of the queue."""
    cyc = _operational_cycle()  # the cycle whose book covers today, not the calendar month
    if not cyc:
        return "skipped — no cycle covering today's presentations"
    today = date.today().strftime("%Y-%m-%d")
    due = [it for it in store.cycle_items(cyc["id"])
           if (it.get("disposition") or {}).get("status") == "PTP"
           and str((it.get("disposition") or {}).get("ptp_date") or "9999") <= today]
    if not due:
        return "no PTPs due today"
    retried = 0
    for it in due:
        try:
            retry_item(it["id"], "ptp-engine")
            retried += 1
        except CycleError:
            pass  # read-only/capped — evaluate on the freshest data we have
    if retried:
        import time as _time
        _time.sleep(checker.AUTO_DELAY + 5)  # let the pulls land + classify
    kept = broken = 0
    for it in due:
        fresh = store.get_cycle_item(it["id"]) or {}
        # retry_item cleared ratio to None before the re-pull; if the re-pull was
        # CAPPED/failed (no fresh RETRIEVED balance) judge on the last-known ratio
        # instead of treating None as 0 and wrongly breaking a promise. See bug (retry_item/PTP).
        r = fresh.get("ratio")
        if r is None:
            r = it.get("ratio")  # pre-retry snapshot from the due list
        ok = (r or 0) >= 1.0
        kept += 1 if ok else 0
        broken += 0 if ok else 1
        status = "PTP_KEPT" if ok else "PTP_BROKEN"
        store.add_disposition(cyc["id"], it["id"], it["loan_id"],
                              fresh.get("override_bucket") or fresh.get("bucket"), status,
                              "Auto-check on the promise date: balance "
                              + ("now covers the EMI." if ok else "is still short — recalled to the queue."),
                              None, "sherlock")
        store.update_cycle_item(it["id"], needs_recall=(not ok), disposition={
            "status": status, "remarks": "auto-checked by Sherlock on the promise date",
            "ptp_date": (it.get("disposition") or {}).get("ptp_date"),
            "by": "sherlock", "at": store._now()})
    return f"{len(due)} PTP(s) due: {kept} kept ✓ · {broken} broken (recalled)"


# ---------------------------------------------------------------------------
# Pre-presentation confirmation sweep — the 'before the 4th' lever
# ---------------------------------------------------------------------------
def run_confirmation_sweep(window_days: int = 1) -> str:
    """The day before each loan's OWN presentation date, re-pull the accounts that
    matter — WATCH/SHORTFALL, open promises, and COMFORT flagged TIMING_RISK (flush on
    run-day but likely drained by the due date). This gives the floor a FRESH reading in
    the narrow pre-NACH window instead of debiting on a run-day snapshot, and it is the
    preventive counterpart to the post-presentation sentinel. Attempt-cap aware;
    idempotent per day (won't re-pull an account already swept today)."""
    from datetime import timedelta
    cyc = _operational_cycle()  # the cycle whose book covers today, not the calendar month
    if not cyc:
        return "skipped — no cycle covering today's presentations"
    todaystr = date.today().strftime("%Y-%m-%d")
    horizon = (date.today() + timedelta(days=max(0, window_days))).strftime("%Y-%m-%d")
    swept = skipped_done = 0
    due = []
    for it in store.cycle_items(cyc["id"]):
        dd = str(it.get("demand_date") or "")[:10]
        if not dd or dd < todaystr or dd > horizon:   # no date / already presented / not imminent
            continue
        eff = it.get("override_bucket") or it.get("bucket")
        disp = (it.get("disposition") or {}).get("status")
        at_risk = eff in ("WATCH", "SHORTFALL") or disp == "PTP" \
            or (it.get("timing_risk") and eff == "COMFORT")
        if at_risk:
            due.append(it)
    for it in due:
        if str(it.get("last_swept") or "")[:10] == todaystr:
            skipped_done += 1
            continue  # already re-checked today — don't double-bill
        try:
            retry_item(it["id"], "confirmation-sweep")
            store.update_cycle_item(it["id"], last_swept=todaystr)
            swept += 1
        except CycleError:
            pass  # read-only/capped — floor acts on the freshest reading available
    if not due:
        return f"no at-risk accounts presenting by {horizon}"
    return (f"confirmation sweep: re-checked {swept} of {len(due)} at-risk account(s) presenting by "
            f"{horizon}" + (f" ({skipped_done} already fresh today)" if skipped_done else ""))


# ---------------------------------------------------------------------------
# Mid-month sentinel — POST-presentation recovery pass (did the debit bounce; is
# re-presentation viable?). The pre-4th prevention lever is run_confirmation_sweep.
# ---------------------------------------------------------------------------
def run_midmonth_sentinel() -> str:
    """Post-presentation recovery: re-check every Shortfall customer after the debit
    window (attempt-cap aware): did the account recover, did a promise get funded, is
    re-presentation viable? For the PRE-presentation confirmation, see run_confirmation_sweep."""
    cyc = _operational_cycle()  # the cycle whose book covers today, not the calendar month
    if not cyc or cyc.get("status") not in ("DONE", "COLLECTING"):
        return "skipped — no finished cycle covering today"
    targets = [it for it in store.cycle_items(cyc["id"])
               if (it.get("override_bucket") or it.get("bucket")) == "SHORTFALL"]
    n = 0
    for it in targets:
        try:
            retry_item(it["id"], "sentinel")
            n += 1
        except CycleError:
            pass
    return f"re-checked {n} of {len(targets)} Shortfall customer(s); buckets refresh as pulls land"


# ---------------------------------------------------------------------------
# NACH outcomes — presentation results. Mock-simulated (deterministic per loan)
# until the live NACH return feed is wired; dispositions influence the odds.
# ---------------------------------------------------------------------------
BOUNCE_PROB = {"COMFORT": 5, "WATCH": 35, "SHORTFALL": 85, "NO_DATA": 50}


def _retry_plan(loan_id, month, present_day):
    """Best re-presentation date for a bounced mandate — from the customer's income
    pattern (statement) if we have it, else a default T+8 window. Shared by the mock
    simulation AND real NACH-actual bounces so the re-presentation planner works on
    real returns, not only fabricated ones. See flow-audit (re-present dead-end)."""
    try:
        import insights as _insights
        m = _insights.statement_insights(loan_id, present_day=present_day).get("metrics", {})
        d = m.get("income_day")
        if d and present_day < int(d) <= 26:
            return f"{month}-{int(d) + 1:02d}", f"income lands ~day {d}"
        if m.get("best_window"):
            start = max(6, min(26, int(str(m["best_window"]).split("–")[0])))
            return f"{month}-{start:02d}", f"balances peak days {m['best_window']}"
    except Exception:  # noqa: BLE001
        pass
    return f"{month}-{min(28, present_day + 8):02d}", "default T+8 window"


def simulate_outcomes(cycle_id: int) -> dict:
    cyc = store.get_cycle(cycle_id)
    if not cyc:
        raise CycleError(f"Cycle {cycle_id} not found")
    if cyc.get("status") != "DONE":
        raise CycleError(f"Cycle {cycle_id} is {cyc.get('status')} — outcomes need a finished cycle")
    month = cyc.get("month")
    snapshot_predictions(cycle_id)  # ensure a frozen prediction exists to score against
    # Hand-entered NACH returns are ground truth — never roll mock outcomes over them
    # (save_outcome also refuses store-side; this keeps the summary honest). Audit #4.
    real = {o.get("loan_id") for o in store.outcomes_for_cycle(cycle_id)
            if o.get("source") == "NACH_ACTUAL"}
    summary = {"presented": 0, "bounced": 0, "by_bucket": {}, "skipped_actual": len(real)}
    for item in store.cycle_items(cycle_id):
        if item["loan_id"] in real:
            continue
        pred = store.prediction_for(cycle_id, item["loan_id"]) or {}
        bucket = item.get("override_bucket") or item.get("bucket") or "NO_DATA"
        disp = (item.get("disposition") or {}).get("status")
        prob = BOUNCE_PROB.get(bucket, 50)
        if disp == "PTP":
            prob = max(0, prob - 25)   # honoured promises reduce bounce odds
        elif disp == "WILL_BOUNCE":
            prob = min(100, prob + 15)
        present_day = _present_day(item.get("demand_date"))
        roll = zlib.crc32(f"{item['loan_id']}:{month}:outcome".encode()) % 100
        outcome = "CLEARED" if disp == "PAID" else ("BOUNCED" if roll < prob else "CLEARED")
        # Re-presentation planner: pick THIS customer's best retry date from
        # their income pattern (statement), falling back to the balance window.
        retry_date = retry_reason = None
        if outcome == "BOUNCED":
            retry_date, retry_reason = _retry_plan(item["loan_id"], month, present_day)
        dd = str(item.get("demand_date") or "")[:10]
        store.save_outcome({
            "cycle_id": cycle_id, "cycle_item_id": item["id"], "loan_id": item["loan_id"],
            "customer_name": item.get("customer_name"), "month": month,
            # the REAL presentation date — the book can present in the month AFTER the run
            "presented_on": dd if len(dd) == 10 else f"{month}-{present_day:02d}",
            "amount": item.get("demand_amount") or item.get("emi_amount"),
            "bucket": bucket, "disposition_status": disp,
            # frozen prediction-of-record — what we said on cycle day, immutable
            "predicted_bucket": pred.get("bucket") or bucket,
            "predicted_score": pred.get("risk_score"),
            "bounce_prob": prob, "roll": roll, "outcome": outcome,
            "retry_date": retry_date, "retry_reason": retry_reason,
            "source": "MOCK_SIMULATION",
        })
        summary["presented"] += 1
        summary["bounced"] += 1 if outcome == "BOUNCED" else 0
        b = summary["by_bucket"].setdefault(bucket, {"presented": 0, "bounced": 0})
        b["presented"] += 1
        b["bounced"] += 1 if outcome == "BOUNCED" else 0
    summary["bounce_rate"] = round(100.0 * summary["bounced"] / summary["presented"], 1) \
        if summary["presented"] else None
    return {"cycle_id": cycle_id, "month": month, **summary}


def record_actual_outcome(cycle_id, loan_id, outcome, reason=None, username="cro") -> dict:
    """Manual NACH-return entry — the REAL ground truth the calibration loop needs when no
    automated return feed exists. Writes source='NACH_ACTUAL' (which replaces any mock row
    for this loan/cycle) joined to the frozen prediction, so predicted-vs-actual is honest.
    outcome: CLEARED | BOUNCED."""
    outcome = str(outcome or "").upper()
    if outcome not in ("CLEARED", "BOUNCED"):
        raise CycleError("outcome must be CLEARED or BOUNCED")
    cyc = store.get_cycle(cycle_id)
    if not cyc:
        raise CycleError(f"Cycle {cycle_id} not found")
    item = next((it for it in store.cycle_items(cycle_id) if str(it["loan_id"]) == str(loan_id)), None)
    if not item:
        raise CycleError(f"Loan {loan_id} is not in cycle {cycle_id}")
    pred = store.prediction_for(cycle_id, loan_id) or {}
    bucket = item.get("override_bucket") or item.get("bucket") or "NO_DATA"
    present_day = _present_day(item.get("demand_date"))
    dd = str(item.get("demand_date") or "")[:10]
    # A REAL bounce feeds the re-presentation planner too — compute the best retry date so
    # simulate_representation acts on it (it used to skip every NACH_ACTUAL bounce because
    # no retry_date was written). See flow-audit (re-present dead-end).
    retry_date = retry_reason = None
    if outcome == "BOUNCED":
        retry_date, retry_reason = _retry_plan(loan_id, cyc.get("month"), present_day)
    prior = next((o for o in store.outcomes_for_cycle(cycle_id)
                  if str(o.get("loan_id")) == str(loan_id)
                  and o.get("source") == "NACH_ACTUAL"), None)
    # Append every real-outcome entry/correction to the immutable history BEFORE the
    # latest-wins row is updated — so a re-entry can never erase what it said before.
    store.log_outcome_event(cycle_id, loan_id, outcome, reason=(reason or "").strip() or None,
                            by=username, prior=(prior or {}).get("outcome"))
    store.save_outcome({
        "cycle_id": cycle_id, "cycle_item_id": item["id"], "loan_id": loan_id,
        "customer_name": item.get("customer_name"), "month": cyc.get("month"),
        # full demand_date, not cycle-month + day: an Aug-4 presentation recorded from a
        # July cycle must not be stamped 2026-07-04. See audit #6.
        "presented_on": dd if len(dd) == 10 else f"{cyc.get('month')}-{present_day:02d}",
        "amount": item.get("demand_amount") or item.get("emi_amount"),
        "bucket": bucket, "disposition_status": (item.get("disposition") or {}).get("status"),
        "predicted_bucket": pred.get("bucket") or bucket, "predicted_score": pred.get("risk_score"),
        "outcome": outcome, "return_reason": (reason or "").strip() or None,
        "retry_date": retry_date, "retry_reason": retry_reason,
        "source": "NACH_ACTUAL", "recorded_by": username,
    })
    return {"cycle_id": cycle_id, "loan_id": loan_id, "outcome": outcome,
            "retry_date": retry_date, "source": "NACH_ACTUAL"}


def outcomes_worklist(cycle_id: int) -> dict:
    """Every presented loan in the cycle with its NACH result status — the CRO's
    outcome-entry worklist. Awaiting-first so the unrecorded ones are on top."""
    cyc = store.get_cycle(cycle_id)
    if not cyc:
        raise CycleError(f"Cycle {cycle_id} not found")
    by_loan = {str(o.get("loan_id")): o for o in store.outcomes_for_cycle(cycle_id)}
    rows = []
    for it in store.cycle_items(cycle_id):
        lid = str(it["loan_id"])
        o = by_loan.get(lid) or {}
        bucket = it.get("override_bucket") or it.get("bucket")
        rows.append({
            "loan_id": lid, "customer_name": it.get("customer_name"),
            "branch": it.get("branch"), "bucket": bucket,
            "demand_amount": it.get("demand_amount") or it.get("emi_amount"),
            "demand_date": it.get("demand_date"),
            "outcome": o.get("outcome"), "source": o.get("source"),
            "return_reason": o.get("return_reason"), "retry_date": o.get("retry_date"),
            "recorded": bool(o.get("outcome") and o.get("source") == "NACH_ACTUAL"),
        })
    rows.sort(key=lambda r: (r["recorded"], str(r["loan_id"])))  # awaiting first
    recorded = sum(1 for r in rows if r["recorded"])
    return {"cycle_id": cycle_id, "month": cyc.get("month"), "rows": rows,
            "total": len(rows), "recorded": recorded, "awaiting": len(rows) - recorded}


def import_outcomes(cycle_id: int, rows, username="cro") -> dict:
    """Bulk NACH-return entry — feed a whole month's presented results at once (the return
    file the bank/NPCI hands back) instead of one prompt per loan. Each row {loan_id,
    outcome, reason} routes through record_actual_outcome, so history + re-presentation
    planning fire per row. Returns per-row recorded/skipped so the CRO sees exactly what
    landed. See flow-audit (single-loan outcome entry)."""
    recorded, skipped = 0, []
    for r in (rows or []):
        lid = str((r or {}).get("loan_id") or "").strip()
        oc = str((r or {}).get("outcome") or "").strip().upper()
        if not lid:
            continue
        # tolerate common return-file vocab
        oc = {"BOUNCE": "BOUNCED", "RETURN": "BOUNCED", "RETURNED": "BOUNCED", "FAIL": "BOUNCED",
              "FAILED": "BOUNCED", "SUCCESS": "CLEARED", "CLEAR": "CLEARED", "PAID": "CLEARED",
              "PRESENTED": "CLEARED"}.get(oc, oc)
        try:
            record_actual_outcome(cycle_id, lid, oc, (r or {}).get("reason"), username)
            recorded += 1
        except CycleError as e:
            skipped.append({"loan_id": lid, "reason": str(e)})
    return {"cycle_id": cycle_id, "recorded": recorded, "skipped": skipped,
            "skipped_count": len(skipped)}


def simulate_representation(cycle_id: int) -> dict:
    """Run the planned re-presentations for a cycle's bounced mandates (mock
    result until the live return feed exists). Income-day-timed retries clear
    at higher odds than default-window ones — that's the planner's edge."""
    cyc = store.get_cycle(cycle_id)
    if not cyc:
        raise CycleError(f"Cycle {cycle_id} not found")
    month = cyc.get("month")
    ran = recovered = 0
    value = 0.0
    for o in store.outcomes_for_cycle(cycle_id):
        if o.get("outcome") != "BOUNCED" or o.get("rep_outcome") or not o.get("retry_date"):
            continue
        roll = zlib.crc32(f"{o['loan_id']}:{month}:rep".encode()) % 100
        prob_clear = 68 if str(o.get("retry_reason") or "").startswith("income") else 50
        rep = "CLEARED" if roll < prob_clear else "BOUNCED"
        store.save_outcome({"cycle_id": cycle_id, "loan_id": o["loan_id"],
                            "rep_outcome": rep, "rep_prob_clear": prob_clear, "rep_roll": roll})
        ran += 1
        if rep == "CLEARED":
            recovered += 1
            value += o.get("amount") or 0
    if not ran:
        return {"cycle_id": cycle_id, "ran": 0, "recovered": 0, "recovered_value": 0,
                "detail": "nothing to re-present (no planned retries pending)"}
    return {"cycle_id": cycle_id, "ran": ran, "recovered": recovered,
            "recovered_value": round(value, 2),
            "detail": f"re-presented {ran}: {recovered} cleared (₹{value:,.0f} recovered)"}


# ---------------------------------------------------------------------------
# Portfolio dashboard — KPIs and trends across cycles
# ---------------------------------------------------------------------------
def build_dashboard() -> dict:
    cycles = store.list_cycles(12)
    by_outcome = store.outcomes_by_cycle(source="NACH_ACTUAL")  # only REAL returns feed accuracy
    trend = []
    for c in reversed(cycles):  # oldest first for trends
        t = c.get("totals") or {}
        o = by_outcome.get(c["id"]) or {}
        coverage = round(100.0 * (t.get("repay_consent_ok") or 0) / t["eligible"], 1) \
            if t.get("eligible") else None
        bounce_rate = round(100.0 * o["bounced"] / o["presented"], 1) if o.get("presented") else None
        trend.append({"cycle_id": c["id"], "month": c.get("month"), "status": c.get("status"),
                      "eligible": t.get("eligible"), "coverage_pct": coverage,
                      "buckets": c.get("bucket_counts"), "presented": o.get("presented"),
                      "bounced": o.get("bounced"), "bounce_rate_pct": bounce_rate})

    latest = next((c for c in cycles if c.get("status") == "DONE"), None)
    latest_block = None
    if latest:
        t = latest.get("totals") or {}
        bc = latest.get("bucket_counts") or {}
        items = store.cycle_items(latest["id"])
        all_outcomes = store.outcomes_for_cycle(latest["id"])
        # ACCURACY IS MEASURED ON REAL RETURNS ONLY. Mock rows never feed the matrix,
        # calibration or hit-rates — and NO_DATA (no prediction was made) is excluded so a
        # blind slice can't be scored as if predicted. See workflow-gap fixes (feedback loop).
        real = [o for o in all_outcomes if str(o.get("source")) == "NACH_ACTUAL"]
        n_simulated = sum(1 for o in all_outcomes if str(o.get("source")) == "MOCK_SIMULATION")
        scorable = [o for o in real if (o.get("bucket") or "NO_DATA") != "NO_DATA"]
        matrix = {}
        for o in scorable:
            m = matrix.setdefault(o.get("bucket"), {"presented": 0, "bounced": 0})
            m["presented"] += 1
            m["bounced"] += 1 if o.get("outcome") == "BOUNCED" else 0
        for m in matrix.values():
            m["bounce_rate_pct"] = round(100.0 * m["bounced"] / m["presented"], 1) if m["presented"] else None
        at_risk = (bc.get("WATCH") or 0) + (bc.get("SHORTFALL") or 0)
        risk_bounced = sum(m["bounced"] for k, m in matrix.items() if k in ("WATCH", "SHORTFALL"))
        risk_presented = sum(m["presented"] for k, m in matrix.items() if k in ("WATCH", "SHORTFALL"))
        safe = matrix.get("COMFORT") or {}

        # -- Coverage HONESTY: consent presence != delivered predictions, and the blind
        #    majority must be visible (not fabricated). See workflow-gap fixes (coverage).
        eligible_due = len(items)
        BLIND_REASONS = ("REPAY_NOT_AA", "CONSENT_EXPIRED", "NO_TXN_ID")
        def _eff(it): return it.get("override_bucket") or it.get("bucket")
        def _dem(it):
            try: return float(it.get("demand_amount") or it.get("emi_amount") or 0)
            except (TypeError, ValueError): return 0.0
        delivered = [it for it in items if _eff(it) in ("COMFORT", "WATCH", "SHORTFALL")]
        blind = [it for it in items if _eff(it) == "NO_DATA"
                 and (it.get("bucket_reason") in BLIND_REASONS)]
        pulled = [it for it in items if it.get("run_id")]
        coverage_delivered_pct = round(100.0 * len(delivered) / eligible_due, 1) if eligible_due else None
        blind_pct = round(100.0 * len(blind) / eligible_due, 1) if eligible_due else None
        blind_exposure = round(sum(_dem(it) for it in blind), 2)
        floor = COVERAGE_FLOOR_PCT

        latest_block = {
            "cycle_id": latest["id"], "month": latest.get("month"),
            "eligible": t.get("eligible"),
            # consent presence (legacy KPI) kept but no longer the headline
            "coverage_pct": round(100.0 * (t.get("repay_consent_ok") or 0) / t["eligible"], 1)
                            if t.get("eligible") else None,
            # what we ACTUALLY saw: a delivered balance, and how blind we were
            "coverage_delivered_pct": coverage_delivered_pct,
            "delivered_count": len(delivered), "pulled_count": len(pulled),
            "eligible_due": eligible_due,
            "blind_pct": blind_pct, "blind_count": len(blind), "blind_exposure": blind_exposure,
            "coverage_floor_pct": floor,
            "coverage_ok": (coverage_delivered_pct is not None and coverage_delivered_pct >= floor),
            "buckets": bc, "at_risk": at_risk,
            "consent_gap": (t.get("repay_consent_expired") or 0) + (t.get("repay_not_linked") or 0),
            "pulls_blocked": t.get("pulls_blocked"),
            "dispositions": store.disposition_summary(latest["id"]),
            "worked": sum(store.disposition_summary(latest["id"]).values()),
            # accuracy provenance so the UI never presents mock as validated truth
            "accuracy": {"validated": bool(real),
                         "source": "NACH_ACTUAL" if real else ("SIMULATED" if n_simulated else "none"),
                         "n_real": len(real), "n_simulated": n_simulated},
            "has_real_outcomes": bool(real),
            "has_outcomes": bool(real),  # legacy alias — now means REAL (validated) outcomes only
            "outcome_matrix": matrix or None,
            # of the customers we flagged at-risk, how many actually bounced? (real returns)
            "risk_hit_rate_pct": round(100.0 * risk_bounced / risk_presented, 1) if risk_presented else None,
            # of the customers we called safe, how many bounced anyway?
            "safe_miss_rate_pct": round(100.0 * safe.get("bounced", 0) / safe["presented"], 1)
                                  if safe.get("presented") else None,
        }
        latest_block["exposure"] = latest.get("exposure") or {}

        # Calibration — frozen predicted-score band vs ACTUAL bounce rate, REAL returns only,
        # NO_DATA excluded (never a prediction). Empty until real outcomes are recorded.
        bands = [(0, 20, "0–19"), (20, 40, "20–39"), (40, 60, "40–59"),
                 (60, 80, "60–79"), (80, 101, "80–100")]
        scored = [o for o in scorable if o.get("predicted_score") is not None]
        calib = []
        for lo, hi, label in bands:
            grp = [o for o in scored if lo <= o["predicted_score"] < hi]
            if not grp:
                continue
            bnc = sum(1 for o in grp if o.get("outcome") == "BOUNCED")
            calib.append({"band": label, "n": len(grp),
                          "avg_score": round(sum(o["predicted_score"] for o in grp) / len(grp)),
                          "bounced": bnc, "actual_bounce_pct": round(100.0 * bnc / len(grp))})
        latest_block["calibration"] = {"bands": calib, "scored": len(scored),
                                       "validated": bool(real), "n_simulated": n_simulated}

        # Newly at-risk vs the previous month — the early warning inside the warning.
        prev_cycle = store._db().cycles.find_one(
            {"month": {"$lt": latest.get("month")}, "status": "DONE"},
            {"_id": 0, "id": 1, "month": 1}, sort=[("month", -1), ("id", -1)])
        newly = []
        if prev_cycle:
            prevmap = {i["loan_id"]: (i.get("override_bucket") or i.get("bucket"))
                       for i in store.cycle_items(prev_cycle["id"])}
            sev = {"COMFORT": 0, "NO_DATA": 1, "WATCH": 2, "SHORTFALL": 3}
            for it in items:
                b = it.get("override_bucket") or it.get("bucket")
                pb = prevmap.get(it["loan_id"])
                if b in ("WATCH", "SHORTFALL") and pb and sev.get(b, 0) > sev.get(pb, 0):
                    newly.append({"loan_id": it["loan_id"], "customer_name": it.get("customer_name"),
                                  "from": pb, "to": b, "emi": it.get("emi_amount"),
                                  "branch": it.get("branch")})
        latest_block["newly_at_risk"] = newly
        latest_block["prev_month"] = (prev_cycle or {}).get("month")

        # Branch breakdown.
        br = {}
        for it in items:
            b = it.get("branch") or "—"
            e = br.setdefault(b, {"branch": b, "state": it.get("state"), "customers": 0,
                                  "at_risk": 0, "exposure": 0.0, "worked": 0})
            e["customers"] += 1
            eb = it.get("override_bucket") or it.get("bucket")
            if eb in ("WATCH", "SHORTFALL"):
                e["at_risk"] += 1
                # demand (EMI + arrears) is what presents — EMI understates overdue loans 2-3x
                e["exposure"] += it.get("demand_amount") or it.get("emi_amount") or 0
            if it.get("disposition"):
                e["worked"] += 1
        latest_block["branches"] = sorted(br.values(), key=lambda x: -x["exposure"])

        # Supervisor console: agent effectiveness + untouched at-risk cases.
        untouched = sorted(
            [{"loan_id": it["loan_id"], "customer_name": it.get("customer_name"),
              "bucket": it.get("override_bucket") or it.get("bucket"), "branch": it.get("branch"),
              "emi": it.get("emi_amount"), "risk_score": it.get("risk_score"),
              "since": it.get("updated_at")}
             for it in items
             if (it.get("override_bucket") or it.get("bucket")) in ("WATCH", "SHORTFALL")
             and not it.get("disposition")],
            key=lambda x: -(x.get("risk_score") or 0))[:8]
        latest_block["supervisor"] = {
            "agents": store.agent_stats(latest["id"]),
            "untouched": untouched,
            "nudges_sent": sum(v["count"] for v in store.nudges_by_item(latest["id"]).values()),
        }

        # Re-presentation planner state (works on all recorded returns, real or mock demo).
        planned = [o for o in all_outcomes if o.get("outcome") == "BOUNCED"]
        repd = [o for o in planned if o.get("rep_outcome")]
        latest_block["representation"] = {
            "bounced": len(planned),
            "pending": len(planned) - len(repd),
            "recovered": sum(1 for o in repd if o["rep_outcome"] == "CLEARED"),
            "recovered_value": round(sum(o.get("amount") or 0 for o in repd
                                         if o["rep_outcome"] == "CLEARED"), 2),
            "planned": [{"loan_id": o["loan_id"], "customer_name": o.get("customer_name"),
                         "amount": o.get("amount"), "retry_date": o.get("retry_date"),
                         "retry_reason": o.get("retry_reason"), "rep_outcome": o.get("rep_outcome")}
                        for o in planned],
        }

        # Shared counterparties across the book (cached network).
        try:
            import insights as _insights
            latest_block["shared_counterparties"] = _insights.shared_counterparties()
        except Exception:  # noqa: BLE001
            latest_block["shared_counterparties"] = []
    return {"latest": latest_block, "trend": trend,
            "bucket_display": BUCKET_DISPLAY, "disposition_display": DISPOSITION_STATUSES}


# Classify cycle items as their runs complete.
checker.RUN_DONE_HOOKS.append(on_run_done)
