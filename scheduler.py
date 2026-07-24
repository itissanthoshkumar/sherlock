"""In-app job scheduler (no external dependencies).

A single daemon thread ticks every SCHEDULER_TICK_SECONDS and runs whatever is
due. Job state (enabled, last run, result) is persisted in the `jobs`
collection so it survives restarts; next-run times are computed on read.
Schedule decisions use the server's local clock.

Jobs:
  monthly_cycle        day CYCLE_RUN_DAY at CYCLE_RUN_HOUR — start the pre-NACH
                       cycle (skips if one already exists for the month).
  nach_outcomes        day NACH_OUTCOME_DAY — record presentation results for
                       this month's cycle (mock simulation until the live NACH
                       return feed is wired).
  consent_expiry_sweep daily at 06:00 — recompute consent statuses in the
                       master registry (ACTIVE / NEARING_EXPIRY / EXPIRED).
  stuck_cycle_sweep    hourly — recover cycles orphaned by a crash/restart:
                       classify whatever data landed and finalize.

On startup, stuck-cycle recovery runs once unconditionally: no thread survives
a restart, so any non-terminal cycle at boot is a zombie by definition.
"""
import os
import threading
import traceback
from datetime import date, datetime, timedelta, timezone

import checker
import cycle
import demo_seed
import mongostore as store

TICK_SECONDS = int(os.getenv("SCHEDULER_TICK_SECONDS", "20"))
ENABLED = os.getenv("SCHEDULER_ENABLED", "true").lower() in ("1", "true", "yes")
CYCLE_DAY = int(os.getenv("CYCLE_RUN_DAY", "1"))
CYCLE_HOUR = int(os.getenv("CYCLE_RUN_HOUR", "7"))
OUTCOME_DAY = int(os.getenv("NACH_OUTCOME_DAY", "5"))
SWEEP_HOUR = int(os.getenv("CONSENT_SWEEP_HOUR", "6"))
CONFIRM_HOUR = int(os.getenv("CONFIRM_SWEEP_HOUR", "6"))

_started = False


# ---------------------------------------------------------------------------
# Job implementations — each returns a one-line human-readable result
# ---------------------------------------------------------------------------
def _job_monthly_cycle() -> str:
    month = store._now()[:7]
    existing = store.cycle_for_month(month)
    if existing:
        return f"skipped — cycle {existing['id']} already exists for {month}"
    # Run the LMS/CRO population (source='lms'), never the env default — and never fire
    # real billed pulls ungated: enforce the same hard pre-flight the CRO console does.
    # ALL of it (audit #11): the earlier duplicate here missed the LIVE-ONLY mock-snapshot
    # gate and the Digitap-readiness/cap-headroom gate, so the scheduled path could
    # auto-run a live cycle over 5 demo rows or with zero budget. Use app._preflight —
    # the single checklist — imported lazily (app imports this module at startup).
    try:
        import app as _app
        pf = _app._preflight()
    except Exception as e:  # noqa: BLE001 — a preflight crash must not start an ungated run
        return f"skipped — pre-flight check failed: {type(e).__name__}: {e}"
    if not pf.get("ready"):
        failing = [i["label"] for i in pf.get("items", []) if i.get("required") and not i.get("ok")]
        return (f"skipped — pre-flight not satisfied for {month}: "
                + (" · ".join(failing) or "checklist incomplete") + "; run it from the CRO console")
    c = cycle.start_cycle("scheduler", source="lms")
    return f"started cycle {c['id']} for {month}"


def _job_nach_outcomes() -> str:
    month = date.today().strftime("%Y-%m")
    cyc = store.cycle_for_month(month)
    if not cyc or cyc.get("status") != "DONE":
        return f"skipped — no finished cycle for {month}"
    if store.outcomes_for_cycle(cyc["id"]):
        return f"skipped — outcomes already recorded for cycle {cyc['id']}"
    s = cycle.simulate_outcomes(cyc["id"])
    return f"cycle {cyc['id']}: {s['bounced']}/{s['presented']} bounced ({s['bounce_rate']}%)"


def _job_consent_sweep() -> str:
    """Recompute consent status on the bank-account masters from their expiry."""
    changed = 0
    scanned = 0
    for acct in store.list_bank_accounts(limit=2000):
        scanned += 1
        status = checker.consent_status({
            "aa_enabled": acct.get("aa_enabled"),
            "consent_expiry": acct.get("consent_expiry"),
        })
        if status != acct.get("consent_status"):
            store._db()[store.MASTER].update_one(
                {"type": "bank_account", "bank_account_uid": acct.get("bank_account_uid")},
                {"$set": {"consent_status": status, "updated_at": store._now()}})
            changed += 1
    return f"scanned {scanned} accounts, {changed} status change(s)"


def _job_confirmation_sweep() -> str:
    """Pre-presentation confirmation: re-pull at-risk/promised/timing-risk accounts whose
    OWN due date is imminent, so the floor acts on fresh balances before the debit."""
    return cycle.run_confirmation_sweep(window_days=1)


def recover_stale_cycles(max_age_minutes: int = 30, force: bool = False) -> str:
    """RUNNING cycles whose thread died -> ERROR. COLLECTING cycles that never
    finished -> classify whatever landed (classify_item degrades gracefully to
    NO_DATA/PULL_FAILED) and finalize. `force` ignores age — used at startup,
    where any non-terminal cycle is a zombie."""
    fixed = []
    now = datetime.now(store.IST).replace(tzinfo=None)  # IST wall-clock, naive (compared to stored IST strings)
    for cyc in store._db().cycles.find({"status": {"$in": ["RUNNING", "COLLECTING"]}}, {"_id": 0}):
        if not force:
            try:
                created = datetime.strptime(cyc.get("created_at"), "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                created = now
            if (now - created) < timedelta(minutes=max_age_minutes):
                continue
        if cyc["status"] == "RUNNING":
            store.update_cycle(cyc["id"], status="ERROR",
                               error="Interrupted: cycle thread did not survive a restart",
                               finished_at=store._now())
            fixed.append(f"cycle {cyc['id']} RUNNING→ERROR")
        else:  # COLLECTING — classify what we have, then finalize
            for item in store.cycle_items(cyc["id"]):
                if item.get("status") in ("PENDING", "PULLING"):
                    try:
                        cycle.classify_item(item["id"])
                    except Exception:  # noqa: BLE001
                        store.update_cycle_item(item["id"], status="ERROR")
            cycle._finalize_cycle_if_done(cyc["id"])
            fixed.append(f"cycle {cyc['id']} force-classified")
    return "; ".join(fixed) if fixed else "nothing stale"


def _job_stuck_sweep() -> str:
    return recover_stale_cycles(max_age_minutes=30)


def _job_representation() -> str:
    month = date.today().strftime("%Y-%m")
    cyc = store.cycle_for_month(month)
    if not cyc:
        return f"skipped — no cycle for {month}"
    return cycle.simulate_representation(cyc["id"])["detail"]


# ---------------------------------------------------------------------------
# Schedule wiring
# ---------------------------------------------------------------------------
def _parse(ts):
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _monthly_next(day, hour, now):
    nxt = now.replace(day=min(day, 28), hour=hour, minute=0, second=0, microsecond=0)
    if nxt <= now:
        y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        nxt = nxt.replace(year=y, month=m)
    return nxt


JOBS = {
    "monthly_cycle": {
        "schedule": f"monthly · day {CYCLE_DAY} at {CYCLE_HOUR:02d}:00",
        "run": _job_monthly_cycle,
        # Fire on-or-after the target day when it hasn't run this month, so a process that
        # was down for all of day CYCLE_DAY still runs the cycle later. See bug (due).
        "due": lambda now, last: ((now.day > CYCLE_DAY or (now.day == CYCLE_DAY and now.hour >= CYCLE_HOUR))
                                  and (last is None or last.strftime("%Y-%m") != now.strftime("%Y-%m"))),
        "next": lambda now: _monthly_next(CYCLE_DAY, CYCLE_HOUR, now),
    },
    "nach_outcomes": {
        "schedule": f"monthly · day {OUTCOME_DAY} at {CYCLE_HOUR:02d}:00",
        "run": _job_nach_outcomes,
        "due": lambda now, last: ((now.day > OUTCOME_DAY or (now.day == OUTCOME_DAY and now.hour >= CYCLE_HOUR))
                                  and (last is None or last.strftime("%Y-%m") != now.strftime("%Y-%m"))),
        "next": lambda now: _monthly_next(OUTCOME_DAY, CYCLE_HOUR, now),
    },
    "consent_expiry_sweep": {
        "schedule": f"daily at {SWEEP_HOUR:02d}:00",
        "run": _job_consent_sweep,
        "due": lambda now, last: (now.hour >= SWEEP_HOUR
                                  and (last is None or last.date() < now.date())),
        "next": lambda now: (now.replace(hour=SWEEP_HOUR, minute=0, second=0, microsecond=0)
                             + (timedelta(days=1) if now.hour >= SWEEP_HOUR else timedelta())),
    },
    "confirmation_sweep": {
        "schedule": f"daily at {CONFIRM_HOUR:02d}:00 (pre-presentation re-pull)",
        "run": _job_confirmation_sweep,
        "due": lambda now, last: (now.hour >= CONFIRM_HOUR
                                  and (last is None or last.date() < now.date())),
        "next": lambda now: (now.replace(hour=CONFIRM_HOUR, minute=0, second=0, microsecond=0)
                             + (timedelta(days=1) if now.hour >= CONFIRM_HOUR else timedelta())),
    },
    "stuck_cycle_sweep": {
        "schedule": "hourly",
        "run": _job_stuck_sweep,
        "due": lambda now, last: last is None or (now - last) >= timedelta(hours=1),
        "next": lambda now: now + timedelta(hours=1),
    },
    "ptp_followup": {
        "schedule": "daily at 09:00",
        "run": cycle.run_ptp_followup,
        "due": lambda now, last: (now.hour >= 9 and (last is None or last.date() < now.date())),
        "next": lambda now: (now.replace(hour=9, minute=0, second=0, microsecond=0)
                             + (timedelta(days=1) if now.hour >= 9 else timedelta())),
    },
    "midmonth_sentinel": {
        "schedule": f"monthly · day 10 at {CYCLE_HOUR:02d}:00",
        "run": cycle.run_midmonth_sentinel,
        "due": lambda now, last: ((now.day > 10 or (now.day == 10 and now.hour >= CYCLE_HOUR))
                                  and (last is None or last.strftime("%Y-%m") != now.strftime("%Y-%m"))),
        "next": lambda now: _monthly_next(10, CYCLE_HOUR, now),
    },
    "representation_run": {
        "schedule": f"monthly · day 12 at {CYCLE_HOUR:02d}:00",
        "run": lambda: _job_representation(),
        "due": lambda now, last: ((now.day > 12 or (now.day == 12 and now.hour >= CYCLE_HOUR))
                                  and (last is None or last.strftime("%Y-%m") != now.strftime("%Y-%m"))),
        "next": lambda now: _monthly_next(12, CYCLE_HOUR, now),
    },
}

# Demo helpers (seed history, reset month) are DESTRUCTIVE and must never be
# runnable in production. Registered only when explicitly enabled — defaults to
# the demo-users flag, so demo boxes keep them and real deployments do not.
DEMO_JOBS_ENABLED = os.getenv(
    "DEMO_JOBS_ENABLED", os.getenv("BOOTSTRAP_DEMO_USERS", "false")).lower() in ("1", "true", "yes")
if DEMO_JOBS_ENABLED:
    JOBS["demo_seed_history"] = {
        "schedule": "manual · Run now only", "run": demo_seed.seed_history,
        "due": lambda now, last: False, "next": lambda now: None,
    }
    JOBS["demo_reset_month"] = {
        "schedule": "manual · Run now only", "run": demo_seed.reset_current_month,
        "due": lambda now, last: False, "next": lambda now: None,
    }


def run_job(name: str, by: str = None) -> dict:
    """Run one job immediately (manual trigger or scheduler tick). `by` is the actor for a
    MANUAL run (None = the scheduler tick) — recorded so a manual trigger is distinguishable
    and attributable. See audit (scheduler runs no actor)."""
    if name not in JOBS:
        raise KeyError(f"Unknown job '{name}'")
    try:
        detail = JOBS[name]["run"]()
        status = "OK"
    except Exception as e:  # noqa: BLE001
        detail = f"{type(e).__name__}: {e}"
        status = "ERROR"
        traceback.print_exc()
    store.update_job(name, last_run_at=store._now(), last_status=status, last_detail=detail,
                     last_run_by=by or "scheduler")
    if by:  # only manual triggers get an actor-attributed history row
        store.log_job_event(name, "RUN", by=by, detail=f"{status}: {detail}")
    return {"name": name, "status": status, "detail": detail}


def jobs_status() -> list:
    # next-run is display-only, computed in IST (project timezone) so it matches
    # last_run_at (store._now(), IST) and everything else the UI shows.
    now = datetime.now(store.IST).replace(tzinfo=None)
    out = []
    for name, spec in JOBS.items():
        doc = store.get_job(name) or {"name": name, "enabled": True}
        doc["schedule"] = spec["schedule"]
        nxt = spec["next"](now) if doc.get("enabled") else None
        doc["next_run_at"] = nxt.strftime("%Y-%m-%d %H:%M") if nxt else None
        out.append(doc)
    return out


def set_enabled(name: str, enabled: bool, by: str = None):
    if name not in JOBS:
        raise KeyError(f"Unknown job '{name}'")
    prev = (store.get_job(name) or {}).get("enabled", True)
    store.update_job(name, enabled=bool(enabled), toggled_by=by, toggled_at=store._now())
    # Disabling a risk-control job (confirmation_sweep, monthly_cycle...) is a material act
    # — record who/when/prior-state so it's reconstructable. See audit (toggles no actor).
    store.log_job_event(name, "ENABLE" if enabled else "DISABLE", by=by,
                        detail=f"{prev} -> {bool(enabled)}")


def _loop():
    import time
    while True:
        time.sleep(TICK_SECONDS)
        now = datetime.now(store.IST).replace(tzinfo=None)  # IST wall-clock (project timezone)
        for name, spec in JOBS.items():
            try:
                doc = store.get_job(name)
                if not doc or not doc.get("enabled"):
                    continue
                if spec["due"](now, _parse(doc.get("last_run_at"))):
                    run_job(name)
            except Exception:  # noqa: BLE001
                traceback.print_exc()


def start():
    """Idempotent: seed job docs, recover zombies from before the restart, and
    start the tick thread (unless SCHEDULER_ENABLED=false)."""
    global _started
    if _started:
        return
    _started = True
    for name, spec in JOBS.items():
        store.upsert_job(name, {"schedule": spec["schedule"]})
    try:
        demo_seed.sync_portfolio_masters()  # backfill branch/state onto older data
        demo_seed.backfill_scores()         # score items that predate the engine
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    try:
        result = recover_stale_cycles(force=True)
        if result != "nothing stale":
            store.update_job("stuck_cycle_sweep", last_run_at=store._now(),
                             last_status="OK", last_detail=f"startup recovery: {result}")
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    if ENABLED:
        threading.Thread(target=_loop, daemon=True, name="dpd-scheduler").start()
