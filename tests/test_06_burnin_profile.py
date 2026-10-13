"""RC2 — Echo finding B1: burn-in engineering acceptance separated from study coverage gates.
Ledgers here are SYNTHETIC (schema-valid, built through ledger.append) and evaluated TIME-SLICED:
at each check time the watchdog sees only rows written by then, exactly as in production."""
import datetime as dt
import json
import random
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, make_reg, NetworkBlocked  # noqa: F401
from ta1c2 import ledger as L
from ta1c2.core import ConfigRefused, Registration, slot_window, utc_iso
from ta1c2.watchdog import burnin_acceptance, evaluate

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 1, 21, tzinfo=UTC)
END = T0 + dt.timedelta(hours=72, minutes=20)
ACC = {"min_in_slot_captures": 70, "injected_miss_slot": 36}


def ledger(reg, outcome_of, omit_missed=()):
    """outcome_of: slot -> 'CAPTURED_VALID' | 'SOURCE_FAILED' | 'MISSED'. MISSED rows are written by the
    next slot's run (+10 min after the gap slot ends), like the real collector."""
    events = []
    for s in range(1, reg.planned_slots + 1):
        s0, s1 = slot_window(reg, s)
        base = {"study_id": reg.study_id, "registration_sha256": reg.sha256, "code_manifest_sha256": reg.code_manifest_sha256,
                "collector_version": "synthetic", "slot": s, "slot_id": f"{reg.study_id}:{s:04d}", "run_id": f"syn{s}"}
        o = outcome_of.get(s, "CAPTURED_VALID")
        if o == "MISSED":
            if s not in omit_missed and s < reg.planned_slots:
                w = s1 + dt.timedelta(minutes=10)
                events.append((w, {**base, "kind": "MISSED", "outcome": "MISSED", "written_utc": utc_iso(w), "reason": "synthetic"}))
        else:
            w = s0 + dt.timedelta(minutes=9)
            events.append((w, {**base, "kind": "ATTEMPT", "attempt": 1, "trigger": "schedule", "run_start_utc": utc_iso(s0 + dt.timedelta(minutes=8)),
                               "outcome": o, "written_utc": utc_iso(w), "artifact_dir": None, "manifest_sha256": None,
                               "sources": {}, "invalid_sources": []}))
    rows = []
    for _, r in sorted(events, key=lambda e: e[0]):
        rows.append(L.append(rows, r))
    return rows


def at(rows, t):
    return L.serialise([r for r in rows if L.parse_utc(r["written_utc"]) <= t])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.tmp)
        self.reg = make_reg(self.tmp, start=T0, planned=72, watchdog_profile="burnin", acceptance=ACC)

    def timeline(self, rows):
        """Evaluate every 15 min across the window; return list of (t, report)."""
        out, t = [], T0
        while t <= END + dt.timedelta(hours=1):
            out.append((t, evaluate(self.reg, at(rows, t), t)))
            t += dt.timedelta(minutes=15)
        return out


class B1InjectedAndOrganic(Base):
    def test_injected_plus_one_organic_never_fails_and_accepts(self):
        rows = ledger(self.reg, {36: "MISSED", 50: "SOURCE_FAILED"})
        tl = self.timeline(rows)
        bad = [(utc_iso(t), r["alerts"]) for t, r in tl if not r["ok"]]
        self.assertEqual(bad, [], "watchdog must not fail on the declared injection + one permitted organic miss")
        final = tl[-1][1]
        self.assertIn(("INJECTED_MISS_OBSERVED", 36), final["info"])
        self.assertIn(("ORGANIC_MISS", 50), final["info"])
        self.assertEqual(final["study_gates"], "not evaluated in burnin profile")
        self.assertFalse(any(a[0].startswith("GATE_UNREACHABLE") for _, r in tl for a in r["alerts"]))
        acc = burnin_acceptance(self.reg, L.serialise(rows), END, 0)
        self.assertTrue(acc["PASS"], acc)
        self.assertTrue(acc["injected_miss_row_is_MISSED"])

    def test_organic_miss_fully_missed_slot_also_tolerated(self):
        rows = ledger(self.reg, {36: "MISSED", 10: "MISSED"})
        self.assertTrue(all(r["ok"] for _, r in self.timeline(rows)))
        self.assertTrue(burnin_acceptance(self.reg, L.serialise(rows), END, 0)["PASS"])

    def test_injection_adjacent_organic_is_transparent(self):
        rows = ledger(self.reg, {35: "SOURCE_FAILED", 36: "MISSED"})   # 35 organic, 36 injected: not "two consecutive"
        self.assertTrue(all(r["ok"] for _, r in self.timeline(rows)))

    def test_second_organic_miss_fails_at_the_right_time(self):
        rows = ledger(self.reg, {36: "MISSED", 20: "SOURCE_FAILED", 60: "SOURCE_FAILED"})
        tl = self.timeline(rows)
        first_fail = next(t for t, r in tl if not r["ok"])
        # slot 60 ends at T0+60h; grace 15 min -> first failing evaluation at the next 15-min tick
        self.assertEqual(first_fail, T0 + dt.timedelta(hours=60, minutes=15))
        self.assertIn("BURNIN_ACCEPTANCE_UNREACHABLE", [a[0] for a in tl[-1][1]["alerts"]])
        self.assertFalse(burnin_acceptance(self.reg, L.serialise(rows), END, 0)["PASS"])

    def test_two_consecutive_counted_misses_across_injection(self):
        rows = ledger(self.reg, {35: "SOURCE_FAILED", 36: "MISSED", 37: "SOURCE_FAILED"})
        kinds = [a[0] for a in self.timeline(rows)[-1][1]["alerts"]]
        self.assertIn("BLOCKED_TWO_CONSECUTIVE", kinds)


class B1InjectionIntegrity(Base):
    def test_injection_not_performed_is_failing(self):
        rows = ledger(self.reg, {})   # slot 36 captured: the outage never happened
        self.assertIn(("INJECTED_MISS_NOT_OBSERVED", 36), self.timeline(rows)[-1][1]["alerts"])
        self.assertFalse(burnin_acceptance(self.reg, L.serialise(rows), END, 0)["PASS"])

    def test_injection_without_missed_row_is_failing(self):
        rows = ledger(self.reg, {36: "MISSED"}, omit_missed={36})   # collector never resumed to record it
        kinds = [a[0] for a in self.timeline(rows)[-1][1]["alerts"]]
        self.assertIn("INJECTED_MISS_ROW_ABSENT", kinds)
        self.assertFalse(burnin_acceptance(self.reg, L.serialise(rows), END, 0)["PASS"])

    def test_watchdog_and_acceptance_agree_on_random_scenarios(self):
        """Property check: final watchdog verdict == acceptance (ledger criteria) on 300 random scenarios."""
        rnd = random.Random(20261013)
        for _ in range(300):
            o = {}
            for s in rnd.sample(range(1, 73), rnd.randint(0, 3)):
                o[s] = rnd.choice(["SOURCE_FAILED", "MISSED"])
            if rnd.random() < 0.9:
                o[36] = "MISSED"
            elif 36 in o:
                del o[36]
            rows = ledger(self.reg, o)
            wd = evaluate(self.reg, L.serialise(rows), END)
            acc = burnin_acceptance(self.reg, L.serialise(rows), END, 0)
            # every_slot_has_row is acceptance-only (final slot 72 has no later run to record a miss)
            ledger_criteria = acc["captures_ok"] and acc.get("injected_miss_observed", True) and acc.get("injected_miss_row_is_MISSED", True)
            consecutive = any(a[0] in ("BLOCKED_TWO_CONSECUTIVE", "SOURCE_TWO_CONSECUTIVE") for a in wd["alerts"])
            self.assertEqual(wd["ok"] or consecutive, ledger_criteria or consecutive, (o, wd["alerts"], acc))
            if ledger_criteria and not consecutive:
                self.assertTrue(wd["ok"], (o, wd["alerts"]))


class StudyProfileUnchanged(Base):
    def test_study_profile_keeps_rc1_semantics(self):
        reg = make_reg(self.tmp / "s" if (self.tmp / "s").mkdir() is None else None, start=T0, planned=72, acceptance=ACC)
        rows = ledger(reg, {36: "MISSED", 50: "SOURCE_FAILED"})
        kinds = [a[0] for a in evaluate(reg, L.serialise(rows), END)["alerts"]]
        self.assertIn("GATE_UNREACHABLE_HOUR", kinds)          # study gates still enforced for studies
        self.assertIn("NO_SUCCESS_SLOT", kinds)


class RegistrationValidation(unittest.TestCase):
    def reg(self, **over):
        d = {"study_id": "X", "approved": False, "mode": "fixture", "start_utc": "2026-11-01T21:00:00Z", "planned_slots": 72,
             "slot_seconds": 3600, "latest_start_margin_s": 45, "max_attempts_per_slot": 2, "freshness_limit_s": 60,
             "request_timeout_s": 20, "watchdog_grace_s": 900, "sizes_aud": [500], "gates": {"overall": .9, "per_utc_hour": .8, "depth": .95},
             "code_manifest_sha256": "0" * 64, "watchdog_profile": "burnin", "acceptance": dict(ACC)}
        d.update(over)
        return Registration.from_bytes(json.dumps(d).encode())

    def test_valid(self):
        self.reg()

    def test_rejections(self):
        for bad in ({"watchdog_profile": "lenient"}, {"acceptance": {}}, {"acceptance": {"min_in_slot_captures": 72, "injected_miss_slot": 36}},
                    {"acceptance": {"min_in_slot_captures": 70, "injected_miss_slot": 72}}, {"acceptance": {"min_in_slot_captures": 70, "injected_miss_slot": "36"}}):
            with self.assertRaises(ConfigRefused, msg=str(bad)):
                self.reg(**bad)

    def test_shipped_proposed_registration_is_burnin(self):
        reg = Registration.load(ROOT / "registration" / "TA1-C2-BURNIN-72H.PROPOSED.json")
        self.assertEqual(reg.data["watchdog_profile"], "burnin"); self.assertEqual(reg.data["release"], "2.0.0-rc3")

    def test_rc3_dead_man_config_is_coherent(self):
        """B4a/B4b arithmetic, from the shipped registration + workflow, not from prose."""
        import re
        reg = Registration.load(ROOT / "registration" / "TA1-C2-BURNIN-72H.PROPOSED.json").data
        cc = reg["dead_man"]["collector_check"]
        self.assertEqual((cc["kind"], cc["tz"], cc["manual_resume"]), ("cron", "UTC", True))
        minute = int(re.fullmatch(r"(\d+) \* \* \* \*", cc["schedule"]).group(1))
        wf = (ROOT / "deploy/github-actions/collector.yml").read_text()
        timeout = int(re.search(r"timeout-minutes: (\d+)", wf).group(1))
        latest_ping_after_slot_end_min = timeout - reg["latest_start_margin_s"] / 60   # start <= :59:15, job <= timeout
        self.assertLess(latest_ping_after_slot_end_min, minute, "an in-slot run could ping after the next cron boundary")
        self.assertNotIn("workflow_dispatch:", wf)
        alert_after_slot_start = minute + cc["grace_minutes"]
        self.assertEqual(alert_after_slot_start, reg["acceptance"]["injected_miss_alert_expected_at_minutes"])
        self.assertLessEqual(alert_after_slot_start + 10, reg["acceptance"]["injected_miss_alert_delivered_within_minutes"],
                             "need >= 10 min delivery margin under the 75-min A2 limit")
        pw = reg["dead_man"]["post_window"]
        self.assertLess(pw["window_minutes_after_end"][1], minute + cc["grace_minutes"], "pause must complete before the earliest post-window alert")
        self.assertGreaterEqual(pw["window_minutes_after_end"][0], minute, "pause only after the slot-72 outcome is final")


if __name__ == "__main__":
    unittest.main()
