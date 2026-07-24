"""Canonical Account Aggregator report engine.

Digitap's retrievereport payload already pre-computes almost everything the old
insights.py reconstructed by hand — salary detection, per-anchor-day EOD
balances, a recommended NACH window, prior bounce counts, full daily open/close
balances and 23 behaviour flags. This module parses that report ONCE into a
normalised model (report -> banks -> accounts -> {holder, monthly, daily,
transactions, recurrent, fraud}) and derives the pre-NACH features on top of it:

  Tier A  parse()                     -> canonical model, every field a column
  Tier B  resolve_mandate()          -> which account/day/amount the NACH hits
          cross_account_funding()    -> liquidity + FOIR across all accounts
          exact_date_bounce()        -> empirical funded/bounce curve on the day
          prior_bounce_score()       -> uses the report's own bounce counts
          present_plan()             -> present/re-present dates + timing clash
          live_balance()             -> fetch-time balance + fund-THIS-account
          kyc_block()                -> dial-ready contact block
  Tier C  new_borrowing() income_stability() fraud_queue() cash_drain()
          call_script() anchor_trend() identity_guard() pin_route()

  analyse(raw, ...) runs the whole battery and returns one bundle for the 360;
  bounce_model(...) returns an explainable 0-100 grounded in real fields.

Everything is defensive: a missing field or a malformed account degrades to
None/[] and never raises, so one bad account can't blank the report.
"""
import re
from datetime import date, datetime

# --- narration signatures -------------------------------------------------
ACH_RE = re.compile(r"ach\s*dr|ach\s+debit|\bnach\b|\becs\b|e-?mandate|mandate|si\s+debit|auto\s*debit", re.I)
DISBURSAL_RE = re.compile(r"disburs|loan\s*cr|\bneft\b.*fin|fin.*\bneft\b", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}


def _f(v):
    try:
        if v is None or v == "":
            return None
        f = float(v)
        # Reject NaN/Inf: json.loads parses a bare NaN literal, which then defeats every
        # `is not None` NO_DATA guard downstream and (via nan comparisons always False)
        # falls through to the most-confident LOW bounce score. None restores the guards.
        # See audit (NaN balance -> confident safe score).
        import math
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _i(v):
    f = _f(v)
    return int(f) if f is not None else None


def month_key(label):
    """Sortable (year, month) for 'August 2025', '2025-08', 'August 2025*'."""
    if not label:
        return (9999, 99)
    s = str(label).replace("*", "").strip()
    m = re.match(r"([A-Za-z]+)\s+(\d{4})", s)
    if m and m.group(1).lower() in _MONTHS:
        return (int(m.group(2)), _MONTHS[m.group(1).lower()])
    m = re.match(r"(\d{4})[-/](\d{1,2})", s)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (9999, 99)


def _day_of(datestr):
    s = str(datestr or "")
    m = re.search(r"\d{4}-\d{2}-(\d{2})", s)
    if m:
        return int(m.group(1))
    m = re.match(r"(\d{1,2})[-/]", s)
    return int(m.group(1)) if m else None


# ==========================================================================
# Tier A — parse
# ==========================================================================
def _holder(acct):
    h = ((acct.get("customer_info") or {}).get("holders") or [{}])[0]
    addr = (h.get("address") or "").strip()
    pin = None
    mp = re.search(r"\b(\d{6})\b", addr)
    if mp:
        pin = mp.group(1)
    age = None
    dob = (h.get("dob") or "").strip()
    md = re.match(r"(\d{4})-(\d{2})-(\d{2})", dob)
    if md:
        try:
            b = date(int(md.group(1)), int(md.group(2)), int(md.group(3)))
            today = date.today()
            age = today.year - b.year - ((today.month, today.day) < (b.month, b.day))
        except ValueError:
            age = None
    # locality: token before the PIN, best-effort
    locality = None
    if addr:
        head = re.split(r"pin\s*:", addr, flags=re.I)[0]
        parts = [p.strip() for p in re.split(r"[,\n]", head) if p.strip()]
        if parts:
            locality = parts[-1][:40]
    return {
        "name": (h.get("name") or "").strip() or None,
        "phone": (h.get("contact_number") or "").strip() or None,
        "email": (h.get("email") or "").strip() or None,
        "dob": dob or None, "age": age,
        "pan": (h.get("pan") or "").strip() or None,
        "ckyc": str(h.get("ckyc_compliance")).lower() == "true",
        "nominee": (h.get("nominee") or "").strip() or None,
        "address": addr or None, "pin": pin, "locality": locality,
        "holding_type": (acct.get("customer_info") or {}).get("holding_type"),
    }


def _recurrent(block):
    """Normalise recurrent_cr / recurrent_dr into a list of income/mandate
    streams. Field names differ (_cr_ vs _dr_) so read sum/count defensively."""
    out = []
    narr = (block or {}).get("recurrent_narration") or {}
    for name, d in narr.items():
        total = count = None
        for k, v in d.items():
            if k.endswith("_sum") or "sum" in k:
                total = _f(v) if total is None else total
            elif k.endswith("_count") or "count" in k:
                count = _i(v) if count is None else count
        months = {}
        for mk, mv in (d.get("individual_month") or {}).items():
            s = c = None
            for k, v in mv.items():
                if "sum" in k:
                    s = _f(v)
                elif "count" in k:
                    c = _i(v)
            months[mk] = {"sum": s, "count": c}
        out.append({"narration": name.strip(), "total": total,
                    "count": count, "months": months})
    return sorted(out, key=lambda x: -(x["total"] or 0))


def _account(acct, bank):
    ad = acct.get("analysis_data") or {}
    overall = ad.get("Overall") or {}
    monthly = []
    for k, v in ad.items():
        if k.startswith("Overall") or not isinstance(v, dict):
            continue
        monthly.append({
            "month": k.replace("*", "").strip(), "key": month_key(k),
            "opening": _f(v.get("Opening")), "closing": _f(v.get("Closing Balance")),
            "min_eod": _f(v.get("Min EOD Balance")), "max_eod": _f(v.get("Max EOD Balance")),
            "avg_eod": _f(v.get("Average EOD Balance")),
            "anchor_eod": _f(v.get("Average EOD Balance on 1st 5th 10th 15th 25th")),
            "credits": _f(v.get("Total Amount of Credit Transactions")),
            "debits": _f(v.get("Total Amount of Debit Transactions")),
            "salary_amt": _f(v.get("Total Amount of Salary Credits")),
            "salary_cnt": _i(v.get("Total No of Salary Credits")),
            "emi_cnt": _i(v.get("Total No. of EMI / loan payments")),
            "iw_bounced": _i(v.get("Total No.of I / W Bounced")),
            "recommended_nach": v.get("Recommended Date Range for NACH"),
        })
    monthly.sort(key=lambda m: m["key"])

    daily = []
    for mo in (acct.get("daily_open_close_balances") or []):
        for db in (mo.get("dailyBalance") or []):
            daily.append({"date": db.get("transaction_date"),
                          "open": _f(db.get("opening_balance")),
                          "close": _f(db.get("closing_balance"))})
    daily.sort(key=lambda d: str(d["date"] or ""))

    txns = []
    for t in (acct.get("transactions") or []):
        amt = _f(t.get("amount"))
        txns.append({"date": t.get("date"), "ts": t.get("transaction_timestamp"),
                     "amount": amt, "type": "CREDIT" if (amt or 0) > 0 else "DEBIT",
                     "balance": _f(t.get("balance")), "narration": t.get("narration"),
                     "category": t.get("category"), "sub_category": t.get("sub_category")})
    txns.sort(key=lambda t: str(t.get("date") or ""))

    loans = []
    for l in (acct.get("loan_analysis") or []):
        loans.append({"date": l.get("date"), "amount": _f(l.get("amount")),
                      "balance": _f(l.get("balance")), "narration": l.get("narration"),
                      "category": l.get("category")})

    fraud = [{"code": f.get("dg_bdtin_code"), "type": f.get("type"),
              "result": f.get("result"), "hits": len(f.get("transactions") or []),
              "sample": (f.get("transactions") or [])[:3]}
             for f in (acct.get("fraud_analysis") or [])]

    num = str(acct.get("account_number") or "")
    last4 = re.sub(r"\D", "", num)[-4:] if num else None
    holder = _holder(acct)
    return {
        "bank": bank.get("bank") or acct.get("bank"),
        "institution_id": bank.get("digitap_institution_id"),
        "account_number": num or None, "last4": last4,
        "ifsc": acct.get("ifsc_code") or acct.get("ifscCode"),
        "micr": acct.get("micr_code"), "account_type": acct.get("account_type"),
        "status": acct.get("account_status"),
        "current_balance": _f(acct.get("current_balance")),
        "balance_as_of": acct.get("balance_date_time"),
        "drawing_limit": _f(acct.get("drawing_limit")),
        "od_limit": _f(acct.get("current_od_Limit")),
        "key": "|".join([x for x in (last4, acct.get("ifsc_code"), holder.get("pan")) if x]) or (num or "?"),
        "holder": holder,
        "employment": overall.get("Employment Type"),
        "business_income": _f(overall.get("Business Income")),
        "salary_flag": _i(overall.get("Salary Flag")),
        "recommended_nach": overall.get("Recommended Date Range for NACH"),
        "avg_eod": _f(overall.get("Average EOD Balance")),
        "anchor_eod": _f(overall.get("Average EOD Balance on 1st 5th 10th 15th 25th")),
        "avg_salary_1m": _f(overall.get("Avg Salary Credited in last 1 month")),
        "avg_salary_3m": _f(overall.get("Avg Salary Credited in last 3 months")),
        "avg_salary_6m": _f(overall.get("Avg Salary Credited in last 6 months")),
        "iw_bounced": _i(overall.get("Total No.of I / W Bounced")) or 0,
        "ecs_bounced": _i(overall.get("Total No. of I / W ECS Bounced")) or 0,
        "bounce_charges": _i(overall.get("Total No of Payment Bounce Charges")) or 0,
        "below_min_bal": _i(overall.get("Total No. of Below Minimum Balance Transaction")) or 0,
        "cash_wd_amt": _f(overall.get("Total Amount of Cash Withdrawals")) or 0.0,
        "cash_wd_cnt": _i(overall.get("Total No. of Cash Withdrawals")) or 0,
        "loan_disbursal_amt": _f(overall.get("Total Amount of Loan Disbursal")) or 0.0,
        "loan_credited_3m": _f(overall.get("Total Loan Credited in last 3 months")) or 0.0,
        "loan_credited_cnt_3m": _i(overall.get("No. of Loan Credited Last 3 Months")) or 0,
        "total_emi_paid": _f(overall.get("Total Amount of EMI / loan Payments")) or 0.0,
        "emi_payment_cnt": _i(overall.get("No. of EMI / loan payments")) or 0,
        "gaming_amt": _f(overall.get("Total Amount of Gaming")) or 0.0,
        "monthly": monthly, "daily": daily, "transactions": txns,
        "loans": loans, "fraud": fraud,
        "recurrent_cr": _recurrent(acct.get("recurrent_cr")),
        "recurrent_dr": _recurrent(acct.get("recurrent_dr")),
        # full per-month analysis_data dicts (label -> 138 fields), for the explorer
        "analysis_raw": {k.replace("*", "").strip(): v for k, v in ad.items()
                         if not k.startswith("Overall") and isinstance(v, dict)},
        "tampered": bool(acct.get("tamper_detection_details")),
    }


def parse(raw):
    """Raw retrievereport dict -> canonical model. Handles N banks x N accounts."""
    resp = raw or {}
    banks, accounts = [], []
    for bank in (resp.get("banks") or []):
        bmodel = {"bank": bank.get("bank"),
                  "institution_id": bank.get("digitap_institution_id"),
                  "source_bank_id": bank.get("source_bank_id"), "accounts": []}
        for acct in (bank.get("accounts") or []):
            am = _account(acct, bank)
            bmodel["accounts"].append(am)
            accounts.append(am)
        banks.append(bmodel)
    # customer identity: distinct holders across accounts
    holders, seen = [], set()
    for a in accounts:
        h = a["holder"]
        sig = (h.get("name"), h.get("pan"), h.get("dob"))
        if sig not in seen:
            seen.add(sig)
            holders.append(h)
    meta = {
        "client_ref_num": resp.get("client_ref_num"), "request_id": resp.get("request_id"),
        "txn_id": resp.get("txn_id"), "fetch_time": resp.get("report_fetch_time"),
        "fetch_type": resp.get("report_fetch_type"),
        "statement_start": resp.get("statement_start_date"),
        "statement_end": resp.get("statement_end_date"),
        "duration_months": resp.get("duration_in_month"),
        "version": resp.get("dg_report_version"),
        "source_report_url": resp.get("source_report"),
        "multiple_accounts": resp.get("multiple_accounts_found"),
    }
    return {"meta": meta, "banks": banks, "accounts": accounts, "holders": holders}


# ==========================================================================
# Tier B — mandate, funding, bounce, timing, live balance, KYC
# ==========================================================================
def resolve_mandate(model, hint_last4=None, hint_ifsc=None):
    """Which pulled account carries the loan NACH, its debit day & rupee amount.
    Priority: LMS hint (last4/ifsc) match, else the account with ACH/NACH
    recurrent debits and a populated loan_analysis. Reads the amount straight
    from the bank feed so it no longer depends on the pending LMS EMI column."""
    accounts = model.get("accounts") or []
    scored = []
    for a in accounts:
        ach = [r for r in a.get("recurrent_dr", []) if ACH_RE.search(r["narration"])]
        loan_debits = [l for l in a.get("loans", []) if (l.get("amount") or 0) < 0]
        score = 0
        if hint_last4 and a.get("last4") == str(hint_last4)[-4:]:
            score += 100
        if hint_ifsc and a.get("ifsc") == hint_ifsc:
            score += 50
        # Cap the activity term so a deterministic LMS hint (last4=100 / ifsc=50) always
        # wins over a salary account that merely has many NACH debits for OTHER lenders.
        # See bug (resolve_mandate).
        score += min(20, 10 * len(ach)) + min(5, len(loan_debits))
        scored.append((score, a, ach, loan_debits))
    scored.sort(key=lambda x: -x[0])
    if not scored or scored[0][0] == 0:
        return None
    _, acct, ach, loan_debits = scored[0]
    # debit day-of-month: mode across loan_analysis debit dates
    days = [_day_of(l["date"]) for l in loan_debits if _day_of(l["date"])]
    debit_day = max(set(days), key=days.count) if days else None
    # amount: most common abs() among ACH debits (the mandate EMI, not the sum)
    amts = [round(abs(l["amount"])) for l in loan_debits if l.get("amount")]
    emi_amt = max(set(amts), key=amts.count) if amts else None
    lender = None
    if ach:
        toks = [t for t in re.split(r"[\s]+", ach[0]["narration"]) if t.isalpha() and len(t) > 3]
        lender = " ".join(toks[-2:]) if toks else ach[0]["narration"][:24]
    return {
        "account_key": acct["key"], "last4": acct["last4"], "ifsc": acct["ifsc"],
        "bank": acct["bank"], "holder": acct["holder"].get("name"),
        "debit_day": debit_day, "emi_amount": emi_amt, "lender": lender,
        "ach_narration": ach[0]["narration"] if ach else None,
        "debit_count": len(loan_debits),
        "iw_bounced": acct.get("iw_bounced", 0) + acct.get("ecs_bounced", 0),
        "matched_by_hint": bool(hint_last4 and acct.get("last4") == str(hint_last4)[-4:]),
    }


def _holder_sig(account):
    h = account.get("holder") or {}
    return (h.get("name"), h.get("pan"), h.get("dob"))


def cross_account_funding(model, emi=None, mandate=None):
    """Liquidity and EMI obligation across the borrower's accounts — aggregating
    the single-account blind spot away WITHOUT falling into the multi-holder trap:
    only same-holder-as-the-mandate accounts count toward the borrower's cover, so
    a relative's balance can't mask the risk. A sibling holder is reported
    separately with a cross-holder warning."""
    accounts = model.get("accounts") or []
    # borrower-of-record = holder of the mandate account (else the first account)
    mkey = (mandate or {}).get("account_key")
    macct = next((a for a in accounts if a["key"] == mkey), (accounts or [None])[0])
    borrower_sig = _holder_sig(macct) if macct else None

    own = [a for a in accounts if _holder_sig(a) == borrower_sig]
    other = [a for a in accounts if _holder_sig(a) != borrower_sig]

    # A MISSING balance (None) is NO_DATA, not ₹0 cover — scoring it as zero would report
    # a full-shortfall (fundable_ratio 0.0, bounce 85) for a report that has no balance. See bug.
    own_bals = [a.get("current_balance") for a in own if a.get("current_balance") is not None]
    total_bal = round(sum(own_bals), 2) if own_bals else None
    total_salary = sum(a.get("avg_salary_3m") or 0 for a in own)
    total_emi_obl = sum(a.get("total_emi_paid") or 0 for a in own)
    # duration_months comes straight off the raw feed and can be a string ("6") — coerce
    # before max(), which raises TypeError on max(1, "6"). See bug.
    dur = _i((model.get("meta") or {}).get("duration_months")) or 12
    monthly_emi = total_emi_obl / max(1, dur)
    foir = round(100.0 * (monthly_emi / total_salary), 1) if total_salary else None
    ratio = (total_bal / emi) if (total_bal is not None and emi and emi > 0) else None

    def _row(a):
        return {"last4": a.get("last4"), "bank": a.get("bank"),
                "balance": a.get("current_balance"), "salary_3m": a.get("avg_salary_3m"),
                "holder": (a.get("holder") or {}).get("name"),
                "is_mandate": a["key"] == mkey}

    warn = None
    if other:
        warn = (f"{len(other)} account(s) under this consent belong to a different holder "
                f"({', '.join(sorted({(a.get('holder') or {}).get('name') or '?' for a in other}))}) — "
                f"their ₹{sum(a.get('current_balance') or 0 for a in other):,.0f} is NOT counted toward the "
                f"borrower's cover. Confirm co-borrower/guarantor before pooling.")
    return {"total_balance": total_bal, "total_salary_3m": round(total_salary, 2),
            "monthly_emi_obligation": round(monthly_emi, 2), "foir_pct": foir,
            "fundable_ratio": round(ratio, 3) if ratio is not None else None,
            "borrower": (macct.get("holder") or {}).get("name") if macct else None,
            "accounts": [_row(a) for a in own], "n_accounts": len(own),
            "other_holder_accounts": [_row(a) for a in other], "cross_holder_warning": warn}


def _daily_asof(account, target_date):
    """Carry-forward closing balance on-or-before target_date (YYYY-MM-DD)."""
    bal = None
    for d in account.get("daily", []):
        if str(d["date"] or "") <= target_date:
            bal = d["close"] if d["close"] is not None else bal
        else:
            break
    return bal


def _balance_at_presentation(account, target_date):
    """Funds AVAILABLE when the NACH presents on target_date — i.e. BEFORE that day's own
    outflows (crucially the EMI debit itself). Prefer the day's opening_balance; else the
    carry-forward CLOSING balance of the latest day strictly BEFORE it. Scoring against the
    post-debit close made a 6/6 on-time payer read as a near-certain bouncer. See audit
    (exact_date post-debit balance)."""
    prev_close = None
    for d in account.get("daily", []):
        ds = str(d.get("date") or "")
        if ds < target_date:
            if d.get("close") is not None:
                prev_close = d["close"]
        elif ds == target_date:
            return d["open"] if d.get("open") is not None else prev_close
        else:
            break
    return prev_close


def exact_date_bounce(account, present_day, emi):
    """Empirical funded/bounce curve: for every month in the daily series, the
    balance available on the mandate day vs the EMI. Replaces the hand-
    reconstructed balance-on-the-4th."""
    if not account.get("daily") or not emi or not present_day:
        return None
    months = sorted({str(d["date"])[:7] for d in account["daily"] if d.get("date")})
    rows, covered = [], 0
    for m in months:
        tgt = f"{m}-{present_day:02d}"
        bal = _balance_at_presentation(account, tgt)  # funds BEFORE that day's EMI debit
        if bal is None:
            continue
        ok = bal >= emi
        covered += 1 if ok else 0
        rows.append({"month": m, "balance_on_day": round(bal, 2),
                     "residual": round(bal - emi, 2), "covered": ok})
    if not rows:
        return None
    rate = covered / len(rows)
    return {"present_day": present_day, "emi": emi, "months": len(rows),
            "covered": covered, "coverage_pct": round(100 * rate, 0),
            "curve": rows, "residuals": [r["residual"] for r in rows[-6:]]}


def prior_bounce_score(account):
    """Prior bounces are the strongest predictor. Uses the report's own counts."""
    iw = account.get("iw_bounced", 0)
    ecs = account.get("ecs_bounced", 0)
    charges = account.get("bounce_charges", 0)
    below = account.get("below_min_bal", 0)
    total = iw + ecs
    factors = []
    add = 0
    if total:
        add += min(30, 12 * total)
        factors.append(f"{total} prior I/W bounce(s) (+{min(30, 12*total)})")
    if charges:
        add += 6
        factors.append(f"{charges} bounce-charge debit(s) (+6)")
    if below >= 3:
        add += 5
        factors.append(f"below-min-balance {below}x (+5)")
    return {"iw_bounced": iw, "ecs_bounced": ecs, "charges": charges,
            "below_min": below, "score_add": add, "factors": factors}


def _salary_days(account):
    days = [_day_of(t["date"]) for t in account.get("transactions", [])
            if t.get("type") == "CREDIT" and "salary" in str(t.get("category") or "").lower()
            and _day_of(t["date"])]
    if not days:
        # fall back to recurrent_cr months' representative day via biggest credit
        days = [_day_of(t["date"]) for t in account.get("transactions", [])
                if t.get("type") == "CREDIT" and (t.get("amount") or 0) >= 3000 and _day_of(t["date"])]
    return days


def present_plan(account, mandate):
    """Optimal present / re-present dates + the salary-timing conflict flag that
    explains structural bounces (mandate presents before salary lands)."""
    sal_days = _salary_days(account)
    salary_day = None
    if sal_days:
        salary_day = max(set(sal_days), key=sal_days.count)
    debit_day = (mandate or {}).get("debit_day")
    rec = account.get("recommended_nach")
    # Salary timing is CYCLICAL, not linear on day-of-month. A conflict is: the mandate
    # presents only SHORTLY BEFORE salary lands (funds not in yet). Measure the forward
    # gap from debit to the NEXT salary credit mod ~30. The common 'salary 28th, present
    # 5th' pattern has salary ~7 days BEFORE presentation (prev month) -> NO conflict.
    # A plain debit_day < salary_day wrongly flagged every month-end-salaried borrower.
    # See audit (salary_conflict inverted).
    LEAD = 7  # salary within this many days AFTER the debit -> funds arrive too late
    conflict = False
    days_to_salary = None
    if salary_day and debit_day:
        days_to_salary = (salary_day - debit_day) % 30  # forward distance debit -> salary
        conflict = 0 < days_to_salary <= LEAD
    represent = None
    if salary_day:
        represent = min(28, salary_day + 1)
    return {"recommended_nach": rec, "salary_day": salary_day, "mandate_day": debit_day,
            "present_on": rec, "represent_on": represent, "salary_conflict": conflict,
            "conflict_note": (f"Mandate debits on the {debit_day}th but salary lands ~{days_to_salary} day(s) "
                              f"later (~{salary_day}th) — funds arrive AFTER presentation, the structural "
                              f"bounce cause. Re-present after the {salary_day}th.") if conflict else None}


def live_balance(model, mandate=None, emi=None):
    """Fetch-time balance per account + which account the NACH will hit + how much
    still to collect right now."""
    accts = []
    mkey = (mandate or {}).get("account_key")
    shortfall = None
    for a in model.get("accounts", []):
        is_mandate = a["key"] == mkey
        if is_mandate and emi:
            gap = emi - (a.get("current_balance") or 0)
            shortfall = round(gap, 2) if gap > 0 else 0.0
        accts.append({"last4": a.get("last4"), "bank": a.get("bank"),
                      "balance": a.get("current_balance"), "as_of": a.get("balance_as_of"),
                      "status": a.get("status"), "is_mandate": is_mandate})
    return {"accounts": accts, "shortfall_now": shortfall,
            "fund_account": (mandate or {}).get("last4")}


def kyc_block(model, borrower_account=None):
    """Dial-ready contact block for the worklist / 360 header. Prefers the
    borrower-of-record (mandate account holder) over the first holder."""
    h = (borrower_account or {}).get("holder") or (model.get("holders") or [{}])[0]
    return {"name": h.get("name"), "phone": h.get("phone"), "age": h.get("age"),
            "dob": h.get("dob"), "pan": h.get("pan"), "ckyc": h.get("ckyc"),
            "address": h.get("address"), "pin": h.get("pin"),
            "locality": h.get("locality"), "email": h.get("email"),
            "holding_type": h.get("holding_type")}


# ==========================================================================
# Tier C — risk & collections signals
# ==========================================================================
def new_borrowing(account):
    if (account.get("loan_credited_3m") or 0) <= 0 and (account.get("loan_disbursal_amt") or 0) <= 0:
        return None
    disb = [l for l in account.get("loans", []) if (l.get("amount") or 0) > 0]
    return {"disbursed_3m": account.get("loan_credited_3m"),
            "disbursed_total": account.get("loan_disbursal_amt"),
            "count_3m": account.get("loan_credited_cnt_3m"),
            "events": disb[:5],
            "note": "Fresh borrowing just before the cycle — a leading distress signal."}


def income_stability(account):
    streams = account.get("recurrent_cr", [])
    a1, a3, a6 = account.get("avg_salary_1m"), account.get("avg_salary_3m"), account.get("avg_salary_6m")
    frauds = {f["type"] for f in account.get("fraud", []) if str(f.get("result")).lower() == "applicable"}
    flags = []
    if "Irregular Salary Credit" in frauds or "Infrequent Salary Transfers" in frauds:
        flags.append("irregular/infrequent salary")
    if "Discontinuity in Credits" in frauds:
        flags.append("discontinuity in credits")
    trend = None
    if a3 and a1 is not None:
        trend = "falling" if a1 < 0.6 * a3 else ("rising" if a1 > 1.4 * a3 else "steady")
    emp = account.get("employment")
    mismatch = bool(emp == "Salaried" and (account.get("business_income") or 0) > 5 * (a3 or 1) and (account.get("business_income") or 0) > 100000)
    return {"employment": emp, "salary_flag": account.get("salary_flag"),
            "avg_salary_1m": a1, "avg_salary_3m": a3, "avg_salary_6m": a6, "trend": trend,
            "streams": [{"narration": s["narration"], "total": s["total"], "count": s["count"]} for s in streams[:4]],
            "flags": flags, "business_income": account.get("business_income"),
            "employment_mismatch": mismatch}


def fraud_queue(model):
    out = []
    for a in model.get("accounts", []):
        for f in a.get("fraud", []):
            if str(f.get("result")).lower() == "applicable":
                out.append({"account": a.get("last4"), "code": f["code"], "type": f["type"],
                            "hits": f["hits"], "sample": f.get("sample")})
    return out


def cash_drain(account):
    frauds = {f["type"] for f in account.get("fraud", []) if str(f.get("result")).lower() == "applicable"}
    instant = "Instant big debit after Salary Credit" in frauds
    amt = account.get("cash_wd_amt") or 0
    cnt = account.get("cash_wd_cnt") or 0
    if not instant and amt < 20000:
        return None
    return {"instant_debit_after_salary": instant, "cash_withdrawn": amt,
            "cash_count": cnt, "urgency": "same-day / pre-salary contact" if instant else "elevated",
            "note": ("Balance empties right after inflow — the money will be cash before the NACH presents."
                     if instant else "Heavy cash usage reduces the collectible balance.")}


CATEGORY_TALK = {
    "Cash Withdrawal": "withdrew {amt} in cash",
    "Shopping & Purchase": "spent {amt} on shopping",
    "Food": "spent {amt} on food & dining",
    "Travel": "spent {amt} on travel",
    "Entertainment & Lifestyle": "spent {amt} on lifestyle",
    "Gaming": "spent {amt} on gaming",
}


def call_script(model, mandate, emi):
    """Personalised, non-accusatory talking points from structured category totals
    over the last ~30 days on the mandate account."""
    acct = next((a for a in model.get("accounts", []) if a["key"] == (mandate or {}).get("account_key")),
                (model.get("accounts") or [None])[0])
    if not acct:
        return None
    recent = sorted([t for t in acct.get("transactions", []) if t.get("date")], key=lambda t: t["date"])[-60:]
    by_cat = {}
    for t in recent:
        if (t.get("amount") or 0) < 0:
            c = t.get("category") or "Other"
            by_cat[c] = by_cat.get(c, 0) + (-t["amount"])
    lines = []
    for c, v in sorted(by_cat.items(), key=lambda kv: -kv[1])[:2]:
        if c in CATEGORY_TALK and v >= 1000:
            lines.append(CATEGORY_TALK[c].format(amt=f"₹{v:,.0f}"))
    ask = f"please keep ₹{emi:,.0f} aside for the EMI on the {mandate.get('debit_day')}th" if (emi and mandate and mandate.get("debit_day")) else "please keep the EMI aside"
    opener = f"In the last month you {' and '.join(lines)}. " if lines else ""
    return {"points": lines, "script": f"{opener}{ask.capitalize()}.",
            "fund_account": (mandate or {}).get("last4")}


def anchor_trend(account):
    """Per-month anchor-day EOD + closing trend — catches a customer sliding
    toward a shortfall a cycle early."""
    series = [{"month": m["month"], "anchor_eod": m["anchor_eod"], "closing": m["closing"],
               "min_eod": m["min_eod"]} for m in account.get("monthly", []) if m.get("anchor_eod") is not None]
    trend = None
    if len(series) >= 4:
        first = [s["anchor_eod"] for s in series[:2] if s["anchor_eod"] is not None]
        last = [s["anchor_eod"] for s in series[-2:] if s["anchor_eod"] is not None]
        if first and last:
            fa, la = sum(first) / len(first), sum(last) / len(last)
            trend = "deteriorating" if la < 0.7 * fa else ("improving" if la > 1.3 * fa else "flat")
    return {"series": series[-8:], "trend": trend}


def identity_guard(model):
    """Flags when a pulled account belongs to a DIFFERENT person than the
    borrower (name/PAN/DOB differ) — a relative's balance must not be scored as
    the borrower's."""
    holders = model.get("holders") or []
    if len(holders) < 2:
        return None
    phones = {h.get("phone") for h in holders if h.get("phone")}
    return {"distinct_holders": len(holders),
            "shared_phone": len(phones) == 1 and bool(phones),
            "holders": [{"name": h.get("name"), "pan": h.get("pan"), "dob": h.get("dob"),
                         "pin": h.get("pin")} for h in holders],
            "note": "Accounts under one consent belong to different people — confirm co-borrower/guarantor "
                    "before counting a sibling account's balance toward the borrower."}


def pin_route(model):
    pins = {}
    for h in model.get("holders", []):
        if h.get("pin"):
            pins.setdefault(h["pin"], []).append(h.get("locality") or h.get("name"))
    if not pins:
        return None
    return {"pins": [{"pin": p, "localities": sorted(set(v))} for p, v in pins.items()]}


# ==========================================================================
# Bounce model — explainable 0-100 from real fields
# ==========================================================================
def bounce_model(model, mandate=None, emi=None, present_day=None):
    """Deterministic, explainable probability grounded in the report's own daily
    balances, prior bounces and timing — not a heuristic guess."""
    accts = model.get("accounts") or []
    if not accts:
        return None
    mkey = (mandate or {}).get("account_key")
    macct = next((a for a in accts if a["key"] == mkey), accts[0])
    emi = emi or (mandate or {}).get("emi_amount")
    day = present_day or (mandate or {}).get("debit_day") or 5
    score, factors = 50, []

    edb = exact_date_bounce(macct, day, emi) if emi else None
    if edb:
        rate = edb["coverage_pct"] / 100.0
        base = int(round(90 - 80 * rate))          # 0% covered -> 90, 100% -> 10
        score = base
        factors.append(f"covered the {day}th in {edb['covered']}/{edb['months']} months → base {base}")
    else:
        cf = cross_account_funding(model, emi, mandate)
        if cf.get("fundable_ratio") is not None:
            r = cf["fundable_ratio"]
            score = 85 if r < 0.5 else (60 if r < 1 else (30 if r < 2 else 12))
            factors.append(f"fundable cover {r:.2f}× today → base {score}")

    pb = prior_bounce_score(macct)
    score += pb["score_add"]
    factors += pb["factors"]

    plan = present_plan(macct, mandate)
    if plan.get("salary_conflict"):
        score += 8
        factors.append(f"mandate day {plan['mandate_day']} before salary day {plan['salary_day']} (+8)")

    if new_borrowing(macct):
        score += 6
        factors.append("fresh borrowing in last 3 months (+6)")

    dr = cash_drain(macct)
    if dr and dr.get("instant_debit_after_salary"):
        score += 6
        factors.append("balance drains right after salary (+6)")

    at = anchor_trend(macct)
    if at.get("trend") == "deteriorating":
        score += 6
        factors.append("anchor-day balance deteriorating (+6)")

    return {"probability": max(2, min(98, int(round(score)))), "factors": factors,
            "mandate_account": macct.get("last4"), "present_day": day, "emi": emi}


# ==========================================================================
# Report data — the underlying figures, segregated per account for browsing
# ==========================================================================
# Analysis fields shown first (always, even if zero); the rest of the month's
# non-zero fields follow. Everything else in analysis_data is a zero/duplicate.
_ANALYSIS_ORDER = [
    "Opening", "Closing Balance", "Min EOD Balance", "Max EOD Balance", "Average EOD Balance",
    "Monthly Average Balance", "Average EOD Balance on 1st 5th 10th 15th 25th",
    "Recommended Date Range for NACH",
    "Total No. of Credit Transactions", "Total Amount of Credit Transactions",
    "Total No. of Debit Transactions", "Total Amount of Debit Transactions",
    "Total No of Salary Credits", "Total Amount of Salary Credits",
    "Total No. of EMI / loan payments", "Total Amount of EMI / loan Payments",
    "Total No.of I / W Bounced", "Total No. of I / W ECS Bounced",
    "Total No. of Cash Withdrawals", "Total Amount of Cash Withdrawals",
    "Total Amount of Loan Disbursal",
]
_ANALYSIS_LABELS = {
    "Opening": "Opening balance", "Closing Balance": "Closing balance",
    "Min EOD Balance": "Min EOD balance", "Max EOD Balance": "Max EOD balance",
    "Average EOD Balance": "Avg EOD balance", "Monthly Average Balance": "Monthly avg balance",
    "Average EOD Balance on 1st 5th 10th 15th 25th": "Avg EOD on anchor days",
    "Recommended Date Range for NACH": "Recommended NACH window",
    "Total No. of Credit Transactions": "Credit txns (count)",
    "Total Amount of Credit Transactions": "Credit amount",
    "Total No. of Debit Transactions": "Debit txns (count)",
    "Total Amount of Debit Transactions": "Debit amount",
    "Total No of Salary Credits": "Salary credits (count)",
    "Total Amount of Salary Credits": "Salary amount",
    "Total No. of EMI / loan payments": "EMI / loan payments (count)",
    "Total Amount of EMI / loan Payments": "EMI / loan amount",
    "Total No.of I / W Bounced": "Inward bounces", "Total No. of I / W ECS Bounced": "ECS bounces",
    "Total No. of Cash Withdrawals": "Cash withdrawals (count)",
    "Total Amount of Cash Withdrawals": "Cash withdrawn",
    "Total Amount of Loan Disbursal": "Loan disbursed",
}


def _analysis_kv(dic):
    """Month analysis_data -> ordered [{k,label,v}] for display: priority metrics
    first (even if zero), then every other non-zero field (zeros are noise)."""
    if not dic:
        return []
    out, seen = [], set()
    for k in _ANALYSIS_ORDER:
        if k in dic and dic[k] not in (None, ""):
            out.append({"k": k, "label": _ANALYSIS_LABELS.get(k, k), "v": dic[k]})
            seen.add(k)
    for k, v in dic.items():
        if k in seen or v in (None, "", 0, 0.0) or k[0].islower():
            continue
        out.append({"k": k, "label": k, "v": v})
        seen.add(k)
    return out


def report_data(model):
    """Account-first, drill-down view: every bank account, and under each the
    month-by-month RAW data (transactions + daily balances) and ANALYSIS data
    (Digitap's per-month metrics). Plus account-level recurrent streams and the
    23 behaviour checks."""
    accounts = []
    for a in model.get("accounts", []):
        # bucket transactions & daily balances by YYYY-MM
        txn_by, daily_by = {}, {}
        for t in a.get("transactions", []):
            txn_by.setdefault(str(t.get("date") or "")[:7], []).append(t)
        for db in a.get("daily", []):
            daily_by.setdefault(str(db.get("date") or "")[:7], []).append(db)
        araw = {month_key(lbl): (lbl, d) for lbl, d in a.get("analysis_raw", {}).items()}

        months = []
        for mo in a.get("monthly", []):
            ym = "%04d-%02d" % (mo["key"][0], mo["key"][1])
            txs = sorted(txn_by.get(ym, []), key=lambda t: str(t.get("date") or ""), reverse=True)
            credits = [t for t in txs if (t.get("amount") or 0) > 0]
            debits = [t for t in txs if (t.get("amount") or 0) < 0]
            _, adic = araw.get(mo["key"], (mo["month"], {}))

            # per-day in/out from transactions, keyed by date
            day_flow = {}
            for t in txs:
                d = str(t.get("date") or "")[:10]
                e = day_flow.setdefault(d, {"in": 0.0, "out": 0.0, "n": 0})
                amt = t.get("amount") or 0
                e["in" if amt >= 0 else "out"] += amt if amt >= 0 else -amt
                e["n"] += 1
            # daily ledger: opening/closing (every day) merged with that day's flow
            days = []
            for db in sorted(daily_by.get(ym, []), key=lambda x: str(x.get("date") or "")):
                d = str(db.get("date") or "")[:10]
                fl = day_flow.get(d, {"in": 0.0, "out": 0.0, "n": 0})
                days.append({"date": d, "opening": db.get("open"), "closing": db.get("close"),
                             "credit_in": round(fl["in"], 2), "debit_out": round(fl["out"], 2),
                             "txn_count": fl["n"]})

            first_open = days[0]["opening"] if days else mo.get("opening")
            last_close = days[-1]["closing"] if days else mo.get("closing")
            months.append({
                "month": mo["month"], "ym": ym,
                "summary": {"opening": first_open, "closing": last_close,
                            "credit_amt": round(sum(t["amount"] for t in credits), 2) if credits else 0,
                            "debit_amt": round(-sum(t["amount"] for t in debits), 2) if debits else 0,
                            "credit_count": len(credits), "debit_count": len(debits),
                            "txn_count": len(txs), "min_eod": mo.get("min_eod"),
                            "day_count": len(days)},
                "days": days,
                "raw_txns": [{"date": t["date"], "narration": t["narration"], "category": t["category"],
                              "amount": t["amount"], "type": t["type"], "balance": t["balance"]}
                             for t in txs[:300]],
                "analysis": _analysis_kv(adic),
            })

        fa = a.get("fraud", [])
        accounts.append({
            "last4": a["last4"], "bank": a["bank"], "type": a["account_type"], "status": a["status"],
            "holder": (a["holder"] or {}).get("name"), "balance": a["current_balance"],
            "ifsc": a["ifsc"], "micr": a["micr"], "account_number": a["account_number"],
            "txn_count": len(a.get("transactions", [])), "month_count": len(months),
            "months": months,
            "recurrent_cr": [{"narration": s["narration"], "total": s["total"], "count": s["count"]}
                             for s in a.get("recurrent_cr", [])[:8]],
            "recurrent_dr": [{"narration": s["narration"], "total": s["total"], "count": s["count"]}
                             for s in a.get("recurrent_dr", [])[:8]],
            "fraud_all": [{"code": f["code"], "type": f["type"], "result": f["result"], "hits": f["hits"]}
                          for f in fa],
            "fraud_hits": sum(1 for f in fa if str(f.get("result")).lower() == "applicable"),
        })
    return {"accounts": accounts}


# ==========================================================================
# BankingIQ — recurring patterns: payments out, money in, subscriptions
# ==========================================================================
_SUB_PATTERNS = [
    ("OTT / Streaming", re.compile(r"netflix|hotstar|disney|prime\s*video|primevideo|sony\s*liv|sonyliv|zee5|\bvoot\b|jio\s*cinema|jiocinema|\baha\b|sun\s*nxt|sunnxt|altbalaji|\bullu\b|eros\s*now", re.I)),
    ("Music", re.compile(r"spotify|gaana|\bwynk\b|jio\s*saavn|jiosaavn|\bsaavn\b|apple\s*music|hungama|amazon\s*music", re.I)),
    ("Software / Cloud", re.compile(r"youtube\s*prem|google\s*one|\bicloud\b|microsoft|office\s*365|\badobe\b|\bcanva\b|\bgithub\b|openai|chatgpt|linkedin\s*prem", re.I)),
    ("Games", re.compile(r"playstation|\bxbox\b|steam\s*games|google\s*play|app\s*store", re.I)),
]
_CP_SKIP = {"UPI", "TFR", "WDL", "DEP", "NEFT", "IMPS", "RTGS", "ACH", "ACHDR", "DR", "CR", "PAYMENT",
            "TRANSFER", "TO", "FROM", "SELF", "CASH", "ATM", "BY", "THE", "AND", "REF", "TXN"}


def _counterparty(t):
    """Best-effort counterparty name. Digitap's category 'Transfer to/from X' is
    cleanest; else pull a name token out of the UPI/NEFT narration."""
    cat = str(t.get("category") or "")
    m = re.match(r"\s*transfer\s+(?:to|from)\s+(.+)", cat, re.I)
    if m:
        return m.group(1).strip().title()[:34] or None
    for p in re.split(r"[/\\|:_-]+", str(t.get("narration") or "")):
        p = p.strip()
        if p and p.isalpha() and len(p) > 2 and p.upper() not in _CP_SKIP:
            return p.title()[:34]
    return None


def _sub_kind(t):
    s = str(t.get("narration") or "") + " " + str(t.get("category") or "")
    for kind, rx in _SUB_PATTERNS:
        if rx.search(s):
            return kind
    return None


def banking_iq(model, min_months=3, min_amount=200):
    """Recurring behaviour across all accounts: payments that repeat to the same
    counterparty in >= min_months distinct months, recurring inflows, and any
    subscription/OTT merchants. Amounts small and one-off transfers are filtered."""
    out_g, in_g, subs = {}, {}, {}
    for a in model.get("accounts", []):
        last4 = a.get("last4")
        for t in a.get("transactions", []):
            amt = t.get("amount")
            if amt is None:
                continue
            ym = str(t.get("date") or "")[:7]
            d = str(t.get("date") or "")[:10]
            kind = _sub_kind(t)
            if kind and amt < 0:
                key = (kind, (_counterparty(t) or kind))
                s = subs.setdefault(key, {"merchant": _counterparty(t) or kind, "kind": kind,
                                          "acct": last4, "amounts": [], "months": set(), "dates": []})
                s["amounts"].append(-amt); s["months"].add(ym); s["dates"].append(d)
            cp = _counterparty(t)
            if not cp:
                continue
            g = (out_g if amt < 0 else in_g).setdefault(
                (cp, last4), {"name": cp, "acct": last4, "amounts": [], "months": set(), "dates": []})
            g["amounts"].append(abs(amt)); g["months"].add(ym); g["dates"].append(d)

    def _pack(groups, recurring_only=True):
        rows = []
        for g in groups.values():
            n_months = len(g["months"])
            amts = sorted(g["amounts"])
            typical = amts[len(amts) // 2] if amts else 0
            total = round(sum(amts), 2)
            if recurring_only and (n_months < min_months or (total / max(1, len(amts))) < min_amount):
                continue
            rows.append({"name": g.get("name") or g.get("merchant"), "acct": g["acct"],
                         "occurrences": len(amts), "months": n_months, "typical": round(typical, 2),
                         "monthly": round(total / max(1, n_months), 2), "total": total,
                         "last_date": max(g["dates"]) if g["dates"] else None,
                         "kind": g.get("kind")})
        rows.sort(key=lambda r: (-r["months"], -r["total"]))
        return rows

    recurring_out = _pack(out_g)[:12]
    recurring_in = _pack(in_g)[:12]
    sub_rows = _pack(subs, recurring_only=False)
    for r in sub_rows:
        r["recurring"] = r["months"] >= 2
    sub_rows.sort(key=lambda r: (-r["months"], -r["total"]))
    return {
        "recurring_out": recurring_out, "recurring_in": recurring_in,
        "subscriptions": sub_rows[:12],
        "summary": {"out_count": len(recurring_out), "in_count": len(recurring_in),
                    "sub_count": len(sub_rows),
                    "out_monthly_total": round(sum(r["monthly"] for r in recurring_out), 2),
                    "in_monthly_total": round(sum(r["monthly"] for r in recurring_in), 2)},
    }


# ==========================================================================
# One-call bundle for the Customer 360
# ==========================================================================
def analyse(raw, hint_last4=None, hint_ifsc=None, emi=None, present_day=None):
    model = parse(raw)
    mandate = resolve_mandate(model, hint_last4, hint_ifsc)
    macct = None
    if mandate:
        macct = next((a for a in model["accounts"] if a["key"] == mandate["account_key"]), None)
    emi_eff = emi or (mandate or {}).get("emi_amount")
    # Score against THIS loan's real NACH presentation day (from the LMS demand_date) when
    # the caller knows it — not the mode of every lender's historical debit dates, which
    # can measure balances after mid-month salary and hide the real pre-salary risk.
    # See audit (AA bounce keyed to wrong day).
    day = present_day or (mandate or {}).get("debit_day") or 5
    return {
        "meta": model["meta"],
        "kyc": kyc_block(model, macct),
        "mandate": mandate,
        "funding": cross_account_funding(model, emi_eff, mandate),
        "live_balance": live_balance(model, mandate, emi_eff),
        "bounce": bounce_model(model, mandate, emi_eff, day),
        "exact_date": exact_date_bounce(macct, day, emi_eff) if macct else None,
        "present_plan": present_plan(macct, mandate) if macct else None,
        "prior_bounce": prior_bounce_score(macct) if macct else None,
        "new_borrowing": new_borrowing(macct) if macct else None,
        "income": income_stability(macct) if macct else None,
        "cash_drain": cash_drain(macct) if macct else None,
        "call_script": call_script(model, mandate, emi_eff),
        "anchor_trend": anchor_trend(macct) if macct else None,
        "fraud_queue": fraud_queue(model),
        "identity_guard": identity_guard(model),
        "pin_route": pin_route(model),
        "accounts": [{"last4": a["last4"], "bank": a["bank"], "type": a["account_type"],
                      "balance": a["current_balance"], "holder": a["holder"].get("name"),
                      "ifsc": a["ifsc"], "status": a["status"]} for a in model["accounts"]],
        "banking_iq": banking_iq(model),
        "report_data": report_data(model),
    }
