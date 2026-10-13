"""Independent watchdog: evaluates a stored ledger against the slot clock. Runs in a separate job
from a separate checkout; it never shares a process or working copy with the collector.

Alerts (all deterministic functions of ledger + clock):
  LEDGER_INVALID            chain/canonical/anchor failure (tamper, truncation, torn write)
  MISSING_SLOT              an ended slot (+grace) has no row at all  (collector silence)
  NO_SUCCESS_SLOT           an ended slot (+grace) has no CAPTURED_VALID attempt
  BLOCKED_TWO_CONSECUTIVE   two consecutive ended slots without success (protocol escalation)
  SOURCE_TWO_CONSECUTIVE    a source invalid/absent in two consecutive ended slots
  GATE_UNREACHABLE_OVERALL  best-case overall coverage < gate  -> study must be closed FAIL
  GATE_UNREACHABLE_HOUR     best-case coverage in some UTC hour < gate
  CLOCK_SKEW                runner clock differs from server HTTP Date by > 120 s
The in-progress slot is never alerted until slot end + grace (no false alarms).

RC2 — watchdog profiles (registration field `watchdog_profile`, default "study"):
  study   exactly the RC1 behaviour above (registered cost-study coverage gates).
  burnin  Gate C ENGINEERING acceptance only; study coverage gates are NOT evaluated.
          The pre-declared injected slot is expected to miss (INFO), and up to the permitted number
          of organic misses is INFO (humans are paged for those by the collector's dead-man and /fail
          pings, not by a sticky watchdog failure). Failing alerts:
            LEDGER_INVALID, CLOCK_SKEW,
            BURNIN_ACCEPTANCE_UNREACHABLE  organic non-success slots > permitted
            BLOCKED_TWO_CONSECUTIVE         two consecutive COUNTED slots without success
            SOURCE_TWO_CONSECUTIVE          a source invalid in two consecutive COUNTED slots
            INJECTED_MISS_NOT_OBSERVED      the injected slot was captured (outage did not happen)
            INJECTED_MISS_ROW_ABSENT        the slot after the injection ended without a MISSED row
                                            for the injected slot (collector did not resume)
          The injected slot is transparent for consecutive-run counting.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from . import ledger as L
from .core import Registration, ended_slots, need, slot_window
from .sources import SOURCE_KEYS


def _clock_skew_alerts(rows) -> list:
    out = []
    for r in rows:
        if r["kind"] == "ATTEMPT":
            skews = [abs(c["http_date_skew_s"]) for c in r.get("sources", {}).values() if c.get("http_date_skew_s") is not None]
            if skews and max(skews) > 120:
                out.append(("CLOCK_SKEW", f"slot {r['slot']} max {max(skews)} s"))
    return out


def burnin_tally(reg: Registration, st: dict, upto: int) -> dict:
    """Single source of truth for burn-in counting, shared by evaluate(burnin) and burnin_acceptance."""
    acc = reg.data["acceptance"]
    inj = acc.get("injected_miss_slot")
    counted = [s for s in range(1, reg.planned_slots + 1) if s != inj]
    allowed = len(counted) - acc["min_in_slot_captures"]
    organic = [s for s in counted if s <= upto and st.get(s) != L.SUCCESS_STATUS]
    return {"injected": inj, "counted": counted, "allowed_organic": allowed, "organic_misses": organic,
            "successes": sum(1 for s in counted if st.get(s) == L.SUCCESS_STATUS)}


def _evaluate_burnin(reg: Registration, rows: list, now) -> dict:
    st = L.slot_status(rows)
    ended = ended_slots(reg, now)
    grace_ended = ended_slots(reg, now - dt.timedelta(seconds=reg.watchdog_grace_s))
    t = burnin_tally(reg, st, grace_ended)
    inj = t["injected"]
    failing, info = _clock_skew_alerts(rows), []
    if inj is not None and inj <= grace_ended:
        if st.get(inj) == L.SUCCESS_STATUS:
            failing.append(("INJECTED_MISS_NOT_OBSERVED", inj))
        else:
            info.append(("INJECTED_MISS_OBSERVED", inj))
            if inj + 1 <= grace_ended and inj not in st:
                failing.append(("INJECTED_MISS_ROW_ABSENT", inj))
    for s in t["organic_misses"]:
        info.append(("ORGANIC_MISS", s))
    if len(t["organic_misses"]) > t["allowed_organic"]:
        failing.append(("BURNIN_ACCEPTANCE_UNREACHABLE", f"organic misses {t['organic_misses']} > permitted {t['allowed_organic']}"))
    seq = [s for s in t["counted"] if s <= grace_ended]
    run = 0
    for s in seq:
        run = run + 1 if st.get(s) != L.SUCCESS_STATUS else 0
        if run == 2:
            failing.append(("BLOCKED_TWO_CONSECUTIVE", s))
    valid = defaultdict(set)
    for r in rows:
        if r["kind"] == "ATTEMPT":
            if r["outcome"] == L.SUCCESS:      # a CAPTURED_VALID attempt has all sources valid by definition
                valid[r["slot"]].update(SOURCE_KEYS)
            for k, c in r.get("sources", {}).items():
                if c.get("valid"):
                    valid[r["slot"]].add(k)
    for k in SOURCE_KEYS:
        run = 0
        for s in seq:
            run = run + 1 if k not in valid[s] else 0
            if run == 2:
                failing.append(("SOURCE_TWO_CONSECUTIVE", f"{k}@{s}"))
    return {"ok": not failing, "profile": "burnin", "alerts": failing, "info": info, "rows": len(rows),
            "ended_slots": ended, "grace_ended_slots": grace_ended, "successes": t["successes"],
            "organic_misses": t["organic_misses"], "allowed_organic": t["allowed_organic"],
            "study_gates": "not evaluated in burnin profile",
            "head": (rows[-1]["seq"], rows[-1]["row_sha256"]) if rows else None}


def evaluate(reg: Registration, raw_ledger: bytes, now, anchor=None) -> dict:
    alerts = []
    try:
        rows = L.parse(raw_ledger)
        L.validate(reg, rows, anchor)
    except L.LedgerInvalid as e:
        return {"ok": False, "alerts": [("LEDGER_INVALID", str(e))], "rows": None}
    if reg.data.get("watchdog_profile", "study") == "burnin":
        return _evaluate_burnin(reg, rows, now)

    st = L.slot_status(rows)
    N = reg.planned_slots
    ended = ended_slots(reg, now)
    grace_ended = ended_slots(reg, now - dt.timedelta(seconds=reg.watchdog_grace_s))
    for s in range(1, grace_ended + 1):
        if s not in st:
            alerts.append(("MISSING_SLOT", s))
        elif st[s] != "SUCCESS":
            alerts.append(("NO_SUCCESS_SLOT", s))
    run = 0
    for s in range(1, grace_ended + 1):
        run = run + 1 if st.get(s) != "SUCCESS" else 0
        if run == 2:
            alerts.append(("BLOCKED_TWO_CONSECUTIVE", s))
    # runner clock audit (slot assignment trusts the runner clock)
    for r in rows:
        if r["kind"] == "ATTEMPT":
            skews = [abs(c["http_date_skew_s"]) for c in r.get("sources", {}).values() if c.get("http_date_skew_s") is not None]
            if skews and max(skews) > 120:
                alerts.append(("CLOCK_SKEW", f"slot {r['slot']} max {max(skews)} s"))
    # per-source consecutive
    valid_by_slot = defaultdict(set)
    for r in rows:
        if r["kind"] == "ATTEMPT":
            for k, c in r.get("sources", {}).items():
                if c.get("valid"):
                    valid_by_slot[r["slot"]].add(k)
    for k in SOURCE_KEYS:
        run = 0
        for s in range(1, grace_ended + 1):
            run = run + 1 if k not in valid_by_slot[s] else 0
            if run == 2:
                alerts.append(("SOURCE_TWO_CONSECUTIVE", f"{k}@{s}"))
    # coverage gates (best case = every not-yet-ended slot succeeds)
    succ = sum(1 for s in range(1, N + 1) if st.get(s) == "SUCCESS")
    # slots still inside their grace period are pending (a late-committing run may still land)
    best = succ + (N - grace_ended) - sum(1 for s in range(grace_ended + 1, N + 1) if st.get(s) == "SUCCESS")
    need_all = need(N, reg.gates["overall"])
    if best < need_all:
        alerts.append(("GATE_UNREACHABLE_OVERALL", f"best {best}/{N} < {need_all}"))
    hours = defaultdict(lambda: [0, 0, 0])  # planned, success, ended-non-success
    for s in range(1, N + 1):
        h = slot_window(reg, s)[0].hour
        hours[h][0] += 1
        if st.get(s) == "SUCCESS":
            hours[h][1] += 1
        elif s <= grace_ended:
            hours[h][2] += 1
    hour_fail = sorted(h for h, (p, ok, bad) in hours.items() if p - bad < need(p, reg.gates["per_utc_hour"]))
    if hour_fail:
        alerts.append(("GATE_UNREACHABLE_HOUR", hour_fail))
    return {"ok": not alerts, "alerts": alerts, "rows": len(rows), "ended_slots": ended, "grace_ended_slots": grace_ended, "successes": succ,
            "best_case": best, "need_overall": need_all, "head": (rows[-1]["seq"], rows[-1]["row_sha256"]) if rows else None}


def burnin_acceptance(reg: Registration, raw_ledger: bytes, now, persistence_incidents: int, anchor=None) -> dict:
    """Gate C acceptance arithmetic (ledger part of A1, A2, A3, A5). Uses burnin_tally, the same
    counting as the burn-in watchdog. The injected slot is excluded from the denominator, must show no
    capture, and must carry a MISSED row (written by the next run). Alert delivery and independent
    reproduction are evidenced outside the ledger."""
    rows = L.parse(raw_ledger)
    L.validate(reg, rows, anchor)
    st = L.slot_status(rows)
    N = reg.planned_slots
    t = burnin_tally(reg, st, N)
    inj = t["injected"]
    crit = {"window_complete": ended_slots(reg, now) >= N, "in_slot_captures": f"{t['successes']}/{len(t['counted'])}",
            "captures_ok": len(t["organic_misses"]) <= t["allowed_organic"],
            "zero_persistence_incidents": persistence_incidents == 0,
            "chain_verified": True, "every_slot_has_row": all(s in st for s in range(1, N + 1))}
    if inj is not None:
        crit["injected_miss_observed"] = st.get(inj) != L.SUCCESS_STATUS
        crit["injected_miss_row_is_MISSED"] = st.get(inj) == "MISSED"
    crit["PASS"] = all(v for k, v in crit.items() if isinstance(v, bool))
    return crit
