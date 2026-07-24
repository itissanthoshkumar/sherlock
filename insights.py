"""Enhanced bank-statement analyser — pattern detection over the AA report.

Runs a battery of detectors on the customer's latest retrieved statement and
returns human-readable insights, each tagged GOOD / INFO / WATCH / RISK, plus a
0–100 statement score. Detectors are independent and defensive: one failing
never hides the others.

Patterns covered: income regularity + salary-day vs the NACH date, inflow
trend, balance-on-the-4th history, low-balance exposure, best presentation
window, debt load & lender count, bounce/penalty charges, counterparty
concentration, circular transfers, cash usage, cash deposits before the due
date, gambling markers, dormant months.
"""
import re
import time
from statistics import mean, median, pstdev

import cycle as cycle_mod
import mongostore as store

GAMBLING_RE = re.compile(r"dream11|rummy|poker|bet(way|365)|casino|teen\s*patti|lottery|1xbet|fantasy", re.I)
CHARGE_RE = re.compile(r"\brtn\b|return|bounce|penal|chrg|charges|ecs\s*rtn|nach\s*rtn|cheque\s*ret", re.I)
CASH_OUT_RE = re.compile(r"\batm\b|cash\s*wd|csh\s*wd|\bcwdr\b|cash\s*withdrawal|self\s*cheque", re.I)
CASH_IN_RE = re.compile(r"cash\s*dep|\bcdm\b|by\s*cash", re.I)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ins(out, severity, title, detail, value=None):
    out.append({"severity": severity, "title": title, "detail": detail, "value": value})


def statement_insights(loan_id: str, present_day: int = 4) -> dict:
    chosen, parsed = cycle_mod.latest_parsed_report(loan_id)
    loan = store._db()[store.MASTER].find_one({"type": "loan", "lms_loan_id": loan_id}, {"_id": 0}) or {}
    emi = _f(loan.get("emi_amount"))

    txns = sorted((parsed.get("transactions") or []),
                  key=lambda t: str(t.get("date") or ""))  # oldest first
    monthly = sorted((parsed.get("monthly") or []),
                     key=lambda m: cycle_mod._month_key(m.get("month")))
    loans_tx = parsed.get("loans") or []
    dates = [str(t.get("date"))[:10] for t in txns if t.get("date")]

    credits = [t for t in txns if (_f(t.get("amount")) or 0) > 0]
    debits = [t for t in txns if (_f(t.get("amount")) or 0) < 0]
    total_in = sum(_f(t["amount"]) for t in credits) or 0.0
    total_out = sum(-_f(t["amount"]) for t in debits) or 0.0

    insights = []
    metrics = {}

    # -- 1. income regularity + salary day vs the NACH date -----------------
    try:
        by_month_max = {}
        for t in credits:
            m = str(t.get("date") or "")[:7]
            if m and (m not in by_month_max or _f(t["amount"]) > _f(by_month_max[m]["amount"])):
                by_month_max[m] = t
        days = [int(str(t["date"])[8:10]) for t in by_month_max.values() if len(str(t.get("date") or "")) >= 10]
        if len(days) >= 4:
            spread = pstdev(days)
            day = int(median(days))
            metrics["income_day"] = day
            if spread <= 4.5:
                if day < present_day:
                    _ins(insights, "GOOD", "Income lands before the NACH date",
                         f"The month's biggest credit arrives around day {day} — money is in the account "
                         f"before presentation on the {present_day}th.", f"day ~{day}")
                else:
                    _ins(insights, "RISK", "Income lands AFTER the NACH date",
                         f"The month's biggest credit arrives around day {day}, but the mandate presents on "
                         f"the {present_day}th — a structural timing mismatch. A later presentation window would help.",
                         f"day ~{day}")
            else:
                _ins(insights, "WATCH", "Irregular income timing",
                     f"The main monthly credit moves around (median day {day}, spread ±{spread:.0f} days) — "
                     "no stable salary pattern to anchor the NACH date on.", f"±{spread:.0f}d")
    except Exception:  # noqa: BLE001
        pass

    # -- 2. inflow stability + trend ----------------------------------------
    try:
        flows = [_f(m.get("credits")) or 0 for m in monthly]
        if len(flows) >= 6:
            last3, prev3 = sum(flows[-3:]), sum(flows[-6:-3])
            if prev3 > 0:
                chg = 100.0 * (last3 - prev3) / prev3
                metrics["inflow_trend_pct"] = round(chg, 1)
                if chg <= -20:
                    _ins(insights, "RISK", "Inflows are shrinking",
                         f"Credits fell {abs(chg):.0f}% in the last quarter vs the one before "
                         f"(₹{last3:,.0f} vs ₹{prev3:,.0f}) — early income-shock signal.", f"{chg:+.0f}%")
                elif chg >= 20:
                    _ins(insights, "GOOD", "Inflows are growing",
                         f"Credits up {chg:.0f}% quarter-over-quarter (₹{last3:,.0f} vs ₹{prev3:,.0f}).",
                         f"{chg:+.0f}%")
        nz = [f for f in flows if f > 0]
        if len(nz) >= 4 and mean(nz) > 0:
            cv = pstdev(nz) / mean(nz)
            _ins(insights, "GOOD" if cv < 0.4 else "WATCH",
                 "Stable monthly inflows" if cv < 0.4 else "Volatile monthly inflows",
                 f"Month-to-month credit variation is {'low' if cv < 0.4 else 'high'} "
                 f"(coefficient {cv:.2f}). {'Dependable for NACH.' if cv < 0.4 else 'Balance checks matter every single month.'}",
                 f"cv {cv:.2f}")
    except Exception:  # noqa: BLE001
        pass

    # -- 3. balance on the 4th, low-balance exposure, best window -----------
    try:
        bal_series = [(str(t["date"])[:10], _f(t.get("balance")))
                      for t in txns if t.get("date") and _f(t.get("balance")) is not None]
        if bal_series and emi:
            months = sorted({d[:7] for d, _ in bal_series})
            covered = total_m = 0
            for m in months:
                asof = [b for d, b in bal_series if d <= f"{m}-{present_day:02d}"]
                if asof:
                    total_m += 1
                    covered += 1 if asof[-1] >= emi else 0
            if total_m >= 4:
                metrics["months_covered_on_4th"] = f"{covered}/{total_m}"  # key kept; now keyed to the real due day
                sev = "GOOD" if covered / total_m >= 0.75 else ("WATCH" if covered / total_m >= 0.5 else "RISK")
                _ins(insights, sev, f"History of covering the EMI on the {present_day}th",
                     f"Reconstructing balances: in {covered} of {total_m} months the balance on day {present_day} "
                     f"covered the EMI of ₹{emi:,.0f}. {'Strong track record.' if sev == 'GOOD' else 'Presentation would have bounced in ' + str(total_m - covered) + ' month(s).'}",
                     f"{covered}/{total_m}")
            low_days = sum(1 for _, b in bal_series if b < emi)
            pct_low = 100.0 * low_days / len(bal_series)
            metrics["days_below_emi_pct"] = round(pct_low, 0)
            if pct_low >= 60:
                _ins(insights, "RISK", "Balance lives below the EMI",
                     f"On {pct_low:.0f}% of transaction days the running balance was under the EMI — "
                     "cover exists only briefly after inflows. Timing is everything for this customer.",
                     f"{pct_low:.0f}% days")
        if bal_series:
            by_day = {}
            for d, b in bal_series:
                day = int(d[8:10])
                if day <= 28:
                    by_day.setdefault(day, []).append(b)
            avg_day = {d: mean(v) for d, v in by_day.items() if v}
            if len(avg_day) >= 10:
                best = max((d for d in avg_day if d <= 26),
                           key=lambda d: mean([avg_day.get(d, 0), avg_day.get(d + 1, avg_day.get(d, 0)),
                                               avg_day.get(d + 2, avg_day.get(d, 0))]))
                metrics["best_window"] = f"{best}–{best + 2}"
                _ins(insights, "INFO", "Best presentation window",
                     f"Average balances peak around days {best}–{best + 2} of the month"
                     + (f" (Digitap suggests {parsed.get('signals', {}).get('Recommended NACH range')})"
                        if parsed.get('signals', {}).get('Recommended NACH range') else "")
                     + ". Worth considering if bounces repeat on the 4th.", f"days {best}–{best + 2}")
    except Exception:  # noqa: BLE001
        pass

    # -- 4. debt load & other lenders ----------------------------------------
    try:
        loan_debits = [l for l in loans_tx if (_f(l.get("amount")) or 0) < 0]
        if loan_debits and monthly:
            last3_keys = {cycle_mod._month_key(m.get("month")) for m in monthly[-3:]}
            recent = [l for l in loan_debits
                      if cycle_mod._month_key(str(l.get("date") or "")[:7]) in last3_keys]
            emi_out = sum(-_f(l["amount"]) for l in recent)
            in3 = sum(_f(m.get("credits")) or 0 for m in monthly[-3:])
            lenders = {cycle_mod._payee_of(l) for l in loan_debits}
            metrics["lender_count"] = len(lenders)
            if in3 > 0 and emi_out > 0:
                foir = 100.0 * emi_out / in3
                metrics["debt_load_pct"] = round(foir, 0)
                sev = "RISK" if foir >= 40 else ("WATCH" if foir >= 25 else "INFO")
                _ins(insights, sev, "Debt servicing load",
                     f"₹{emi_out:,.0f} of the last quarter's ₹{in3:,.0f} inflows went to loan payments "
                     f"({foir:.0f}%) across {len(lenders)} lender(s): {', '.join(sorted(lenders)[:4])}.",
                     f"{foir:.0f}% of inflows")
            elif len(lenders) >= 2:
                _ins(insights, "WATCH", "Multiple lender relationships",
                     f"{len(lenders)} distinct lenders appear in the statement: {', '.join(sorted(lenders)[:5])}.",
                     f"{len(lenders)} lenders")
    except Exception:  # noqa: BLE001
        pass

    # -- 5. bounce & penalty charges -----------------------------------------
    try:
        bounces = _f(parsed.get("signals", {}).get("EMI bounces (3m)"))
        if bounces:
            metrics["emi_bounces_3m"] = int(bounces)
        charges = [t for t in debits if CHARGE_RE.search(str(t.get("narration") or ""))]
        if bounces:
            _ins(insights, "RISK", "Recent EMI bounces on record",
                 f"The AA analysis reports {bounces:.0f} EMI bounce(s) in the last 3 months.",
                 f"{bounces:.0f} in 3m")
        if charges:
            amt = sum(-_f(t["amount"]) for t in charges)
            _ins(insights, "WATCH", "Bank return/penalty charges",
                 f"{len(charges)} charge-like debit(s) totalling ₹{amt:,.0f} "
                 "(returns, penalties, cheque/NACH return fees) found in the narrations.",
                 f"{len(charges)} charges")
    except Exception:  # noqa: BLE001
        pass

    # -- 6. counterparty concentration & circular flows ----------------------
    try:
        out_by, in_by = {}, {}
        for t in debits:
            out_by[cycle_mod._payee_of(t)] = out_by.get(cycle_mod._payee_of(t), 0) + (-_f(t["amount"]))
        for t in credits:
            in_by[cycle_mod._payee_of(t)] = in_by.get(cycle_mod._payee_of(t), 0) + _f(t["amount"])
        if out_by and total_out:
            top_p, top_v = max(out_by.items(), key=lambda kv: kv[1])
            share = 100.0 * top_v / total_out
            metrics["top_payee_share_pct"] = round(share, 0)
            if share >= 35:
                _ins(insights, "WATCH", "Outflows concentrated on one counterparty",
                     f"{share:.0f}% of all spending (₹{top_v:,.0f}) goes to “{top_p}”. "
                     "Worth asking who this is — family, supplier, or an informal lender.",
                     f"{share:.0f}% → {top_p}")
        circ = [p for p in out_by if p in in_by
                and out_by[p] >= 0.08 * total_out and in_by[p] >= 0.08 * total_in
                and min(out_by[p], in_by[p]) >= 10000]
        if circ:
            p = circ[0]
            _ins(insights, "WATCH", "Circular money movement",
                 f"“{p}” both receives (₹{out_by[p]:,.0f}) and sends (₹{in_by[p]:,.0f}) large amounts — "
                 "possible self-transfers or rotation; inflows may overstate real income.",
                 f"{len(circ)} counterparty(ies)")
    except Exception:  # noqa: BLE001
        pass

    # -- 7. cash behaviour ----------------------------------------------------
    try:
        cash_out = sum(-_f(t["amount"]) for t in debits
                       if CASH_OUT_RE.search(str(t.get("narration") or "") + " " + str(t.get("category") or "")))
        if total_out and cash_out / total_out >= 0.3:
            _ins(insights, "WATCH", "Heavy cash withdrawals",
                 f"₹{cash_out:,.0f} ({100 * cash_out / total_out:.0f}% of outflow) leaves as cash — "
                 "spending beyond this point is invisible to the statement.",
                 f"{100 * cash_out / total_out:.0f}% cash")
        dep_before_due = [t for t in credits
                          if CASH_IN_RE.search(str(t.get("narration") or ""))
                          and 1 <= int(str(t.get("date") or "1970-01-05")[8:10]) <= 4]
        if len(dep_before_due) >= 3:
            _ins(insights, "INFO", "Cash top-ups before the due date",
                 f"{len(dep_before_due)} cash deposit(s) land in the 1st–4th window — the customer actively "
                 "funds the account for the EMI. A reminder call is likely to work well here.",
                 f"{len(dep_before_due)} deposits")
    except Exception:  # noqa: BLE001
        pass

    # -- 8. gambling / risky merchants ---------------------------------------
    try:
        bets = [t for t in debits if GAMBLING_RE.search(str(t.get("narration") or ""))]
        if bets:
            amt = sum(-_f(t["amount"]) for t in bets)
            _ins(insights, "RISK", "Gaming/betting outflows",
                 f"{len(bets)} transaction(s) totalling ₹{amt:,.0f} match gaming/betting merchants.",
                 f"₹{amt:,.0f}")
    except Exception:  # noqa: BLE001
        pass

    # -- 9. dormant months ------------------------------------------------------
    try:
        dead = [m.get("month") for m in monthly
                if (_f(m.get("credits")) or 0) < 1000 and (_f(m.get("debits")) or 0) < 1000]
        if dead:
            _ins(insights, "WATCH", "Dormant months in the statement",
                 f"{len(dead)} month(s) with almost no activity ({', '.join(str(x) for x in dead[:3])}"
                 f"{'…' if len(dead) > 3 else ''}) — the account may not be the customer's primary one.",
                 f"{len(dead)} month(s)")
    except Exception:  # noqa: BLE001
        pass

    # -- 10. shared counterparties with other borrowers ----------------------
    try:
        net = payee_network()
        mine = {cycle_mod._payee_of(t) for t in debits if -_f(t["amount"]) >= 5000}
        shared = [(p, [x for x in net.get(p, []) if x["loan_id"] != loan_id]) for p in mine if p in net]
        shared = [(p, others) for p, others in shared if others]
        if shared:
            p, others = max(shared, key=lambda x: len(x[1]))
            names = ", ".join(o.get("customer_name") or o["loan_id"] for o in others[:3])
            _ins(insights, "WATCH", "Counterparty shared with other borrowers",
                 f"“{p}” also receives large sums from {len(others)} other customer(s) on the book "
                 f"({names}{'…' if len(others) > 3 else ''}) — possible informal lending ring or common "
                 "middleman; worth a field question.", f"{len(others)} borrower(s)")
    except Exception:  # noqa: BLE001
        pass

    # -- score ------------------------------------------------------------------
    order = {"RISK": 0, "WATCH": 1, "INFO": 2, "GOOD": 3}
    insights.sort(key=lambda i: order.get(i["severity"], 9))
    score = 70
    for i in insights:
        score += {"GOOD": 6, "INFO": 0, "WATCH": -7, "RISK": -14}[i["severity"]]
    score = max(5, min(95, score))
    metrics["score"] = score

    return {"loan_id": loan_id, "bank": chosen.get("bank_name"),
            "is_repayment": bool(chosen.get("is_repayment")), "pull_id": chosen["id"],
            "emi": emi, "score": score, "metrics": metrics, "insights": insights}


# ---------------------------------------------------------------------------
# Bounce probability — one explainable 0–100 per customer
# ---------------------------------------------------------------------------
def bounce_probability(loan_id, bucket=None, ratio=None, present_day=4):
    """Blend the bucket verdict with statement patterns into a bounce
    probability, keyed to the loan's real presentation day. Returns (score, factor
    strings) — always explainable."""
    base = {"COMFORT": 8, "WATCH": 35, "SHORTFALL": 80, "NO_DATA": 50}.get(bucket or "NO_DATA", 50)
    score, factors = base, [f"{bucket or 'NO_DATA'} baseline {base}"]
    try:
        ins = statement_insights(loan_id, present_day=present_day)
        m = ins.get("metrics", {})
        cov = m.get("months_covered_on_4th")
        if cov:
            c, t = cov.split("/")
            r = int(c) / max(1, int(t))
            if r < 0.3:
                score += 10; factors.append(f"covered the 4th only {cov} months (+10)")
            elif r > 0.7:
                score -= 10; factors.append(f"covered the 4th {cov} months (−10)")
        b3 = m.get("emi_bounces_3m")
        if b3:  # prior bounces are the single strongest predictor of the next one
            add = min(20, 10 * int(b3))
            score += add; factors.append(f"{int(b3)} EMI bounce(s) in last 3m (+{add})")
        d = m.get("income_day")
        if d is not None:
            if d > present_day:
                score += 8; factors.append(f"income lands ~day {d}, after the day-{present_day} debit (+8)")
            elif d < present_day:
                score -= 5; factors.append(f"income lands ~day {d}, before the day-{present_day} debit (−5)")
        if (m.get("debt_load_pct") or 0) >= 40:
            score += 7; factors.append("debt load ≥40% of inflows (+7)")
        if (m.get("inflow_trend_pct") or 0) <= -20:
            score += 8; factors.append("inflows shrinking ≥20% (+8)")
        if (m.get("days_below_emi_pct") or 0) >= 60:
            score += 6; factors.append("balance under EMI on 60%+ of days (+6)")
        if any(i["title"].startswith("Gaming") for i in ins.get("insights", [])):
            score += 6; factors.append("gaming/betting outflows (+6)")
    except Exception:  # noqa: BLE001
        factors.append("no statement — bucket baseline only")
    if ratio is not None:
        if ratio >= 3:
            score -= 6; factors.append("cover ≥3× today (−6)")
        elif ratio < 0.5:
            score += 6; factors.append("cover <0.5× today (+6)")
    return max(2, min(98, int(round(score)))), factors


# ---------------------------------------------------------------------------
# Payee-overlap network — shared counterparties across the whole book
# ---------------------------------------------------------------------------
_NET_CACHE = {"at": 0.0, "data": None}


def payee_network(min_total=15000, ttl=300):
    """payee -> [{loan_id, customer_name, total}] across every customer with a
    retrieved report. Cached (parsing every statement is the expensive bit)."""
    now = time.time()
    if _NET_CACHE["data"] is not None and now - _NET_CACHE["at"] < ttl:
        return _NET_CACHE["data"]
    net = {}
    for loan in store._db()[store.MASTER].find({"type": "loan"}, {"_id": 0}):
        lid = loan.get("lms_loan_id")
        try:
            _, parsed = cycle_mod.latest_parsed_report(lid)
        except Exception:  # noqa: BLE001
            continue
        totals = {}
        for t in (parsed.get("transactions") or []):
            amt = _f(t.get("amount"))
            if amt is None or amt >= 0:
                continue
            p = cycle_mod._payee_of(t)
            totals[p] = totals.get(p, 0) - amt
        for p, v in totals.items():
            if v >= min_total and p not in ("Other", "Unknown"):
                net.setdefault(p, []).append(
                    {"loan_id": lid, "customer_name": loan.get("customer_name"),
                     "branch": loan.get("branch"), "total": round(v, 2)})
    _NET_CACHE.update(at=now, data=net)
    return net


def shared_counterparties(min_borrowers=2):
    """Ranked list of counterparties that receive large sums from 2+ borrowers."""
    net = payee_network()
    out = [{"payee": p, "borrowers": len(v), "total": round(sum(x["total"] for x in v), 2),
            "customers": v}
           for p, v in net.items() if len(v) >= min_borrowers]
    out.sort(key=lambda x: (-x["borrowers"], -x["total"]))
    return out[:12]
