"""Ledger strictness, watchdog semantics, Gate C acceptance arithmetic, and a counterfactual replay of
the real TA1-COST-14D.1 run history through collector v2 + watchdog."""
import datetime as dt
import json
import os
import shutil
import socket
import tempfile
import unittest
from pathlib import Path

from helpers import CAPTURES, START_14D1, cap_dir, cap_start, make_reg, NetworkBlocked
from ta1c2 import ledger as L
from ta1c2.collector import run_once
from ta1c2.core import VirtualClock, canonical, slot_window, utc_iso
from ta1c2.notify import RecordingNotifier
from ta1c2.sources import FixtureTransport
from ta1c2.storage import DirBackend
from ta1c2.verify import verify_store
from ta1c2.watchdog import burnin_acceptance, evaluate

UTC = dt.timezone.utc


def synth_ledger(reg, outcomes: dict[int, str]) -> bytes:
    """SYNTHETIC schema-valid ledger (no artifacts) for pure gate arithmetic. outcomes: slot -> outcome|'MISSED'."""
    rows = []
    for s in sorted(outcomes):
        s0, s1 = slot_window(reg, s)
        base = {"study_id": reg.study_id, "registration_sha256": reg.sha256, "code_manifest_sha256": reg.code_manifest_sha256,
                "collector_version": "synthetic", "slot": s, "slot_id": f"{reg.study_id}:{s:04d}", "run_id": "synthetic"}
        if outcomes[s] == "MISSED":
            row = {**base, "kind": "MISSED", "outcome": "MISSED", "written_utc": utc_iso(s1 + dt.timedelta(minutes=10)), "reason": "synthetic"}
        else:
            row = {**base, "kind": "ATTEMPT", "attempt": 1, "trigger": "synthetic", "run_start_utc": utc_iso(s0 + dt.timedelta(minutes=8)),
                   "outcome": outcomes[s], "written_utc": utc_iso(s0 + dt.timedelta(minutes=9)), "artifact_dir": None,
                   "manifest_sha256": None, "sources": {}, "invalid_sources": []}
        rows.append(L.append(rows, row))
    return L.serialise(rows)


class NetworkGuard(unittest.TestCase):
    def test_ip_sockets_and_dns_blocked(self):
        with self.assertRaises(NetworkBlocked):
            socket.create_connection(("api.coinbase.com", 443))
        with self.assertRaises(NetworkBlocked):
            socket.getaddrinfo("www.coinspot.com.au", 443)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(NetworkBlocked):
                s.connect(("127.0.0.1", 9))
        finally:
            s.close()


class LedgerStrictness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.tmp)
        self.reg = make_reg(self.tmp, planned=10, start=dt.datetime(2026, 11, 1, tzinfo=UTC))
        self.raw = synth_ledger(self.reg, {1: "CAPTURED_VALID", 2: "MISSED", 3: "CAPTURED_VALID"})

    def test_valid(self):
        L.validate(self.reg, L.parse(self.raw))

    def test_non_canonical_rejected(self):
        bad = self.raw.replace(b'","', b'", "', 1)
        with self.assertRaises(L.LedgerInvalid):
            L.parse(bad)

    def test_reorder_rejected(self):
        lines = self.raw.splitlines(True); lines[0], lines[1] = lines[1], lines[0]
        with self.assertRaises(L.LedgerInvalid):
            L.validate(self.reg, L.parse(b"".join(lines)))

    def test_torn_write_rejected(self):
        with self.assertRaises(L.LedgerInvalid):
            L.parse(self.raw[:-5])

    def test_truncation_detected_only_with_anchor(self):
        rows = L.parse(self.raw); anchor = (3, rows[2]["row_sha256"])
        cut = L.serialise(rows[:2])
        L.validate(self.reg, L.parse(cut))  # undetectable without an external anchor (by design, documented)
        with self.assertRaises(L.LedgerInvalid):
            L.validate(self.reg, L.parse(cut), anchor)

    def test_foreign_registration_rejected(self):
        other = make_reg(self.tmp / "o" if (self.tmp / "o").mkdir() is None else None, planned=10, start=dt.datetime(2026, 11, 1, tzinfo=UTC), study_id="OTHER")
        with self.assertRaises(L.LedgerInvalid):
            L.validate(other, L.parse(self.raw))


class Watchdog(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.tmp)
        self.t0 = dt.datetime(2026, 11, 1, tzinfo=UTC)
        self.reg = make_reg(self.tmp, planned=72, start=self.t0)

    def kinds(self, rep):
        return [a[0] for a in rep["alerts"]]

    def test_no_false_alarm_for_in_progress_slot_or_within_grace(self):
        raw = synth_ledger(self.reg, {1: "CAPTURED_VALID"})
        self.assertTrue(evaluate(self.reg, raw, self.t0 + dt.timedelta(minutes=110))["ok"])   # slot 2 in progress
        self.assertTrue(evaluate(self.reg, raw, self.t0 + dt.timedelta(minutes=134))["ok"])   # slot 2 ended 14 min ago
        rep = evaluate(self.reg, raw, self.t0 + dt.timedelta(minutes=136))                   # grace (15 min) elapsed
        self.assertIn(("MISSING_SLOT", 2), rep["alerts"])

    def test_two_consecutive_and_source_escalation(self):
        raw = synth_ledger(self.reg, {1: "CAPTURED_VALID", 2: "SOURCE_FAILED", 3: "MISSED"})
        rep = evaluate(self.reg, raw, self.t0 + dt.timedelta(hours=3, minutes=16))
        self.assertIn("BLOCKED_TWO_CONSECUTIVE", self.kinds(rep)); self.assertIn("SOURCE_TWO_CONSECUTIVE", self.kinds(rep))

    def test_gate_ceiling(self):
        # 72 slots, need ceil(0.9*72)=65 -> 8 misses make the gate unreachable
        out = {s: ("MISSED" if s <= 8 else "CAPTURED_VALID") for s in range(1, 9)}
        rep = evaluate(self.reg, synth_ledger(self.reg, out), self.t0 + dt.timedelta(hours=8, minutes=16))
        self.assertIn("GATE_UNREACHABLE_OVERALL", self.kinds(rep))
        out7 = {s: ("MISSED" if s <= 7 else "CAPTURED_VALID") for s in range(1, 9)}
        rep7 = evaluate(self.reg, synth_ledger(self.reg, out7), self.t0 + dt.timedelta(hours=8, minutes=16))
        self.assertNotIn("GATE_UNREACHABLE_OVERALL", self.kinds(rep7))

    def test_tamper_reported(self):
        raw = synth_ledger(self.reg, {1: "CAPTURED_VALID"}).replace(b"CAPTURED_VALID", b"SOURCE_FAILED")
        self.assertEqual(self.kinds(evaluate(self.reg, raw, self.t0)), ["LEDGER_INVALID"])


class BurnInAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.tmp)
        self.t0 = dt.datetime(2026, 11, 1, tzinfo=UTC)
        self.reg = make_reg(self.tmp, planned=72, start=self.t0, acceptance={"min_in_slot_captures": 71})
        self.end = self.t0 + dt.timedelta(hours=72, minutes=20)

    def test_72_of_72_pass(self):
        raw = synth_ledger(self.reg, {s: "CAPTURED_VALID" for s in range(1, 73)})
        self.assertTrue(burnin_acceptance(self.reg, raw, self.end, 0)["PASS"])

    def test_71_of_72_pass_70_fail(self):
        o = {s: "CAPTURED_VALID" for s in range(1, 73)}; o[30] = "SOURCE_FAILED"
        self.assertTrue(burnin_acceptance(self.reg, synth_ledger(self.reg, o), self.end, 0)["PASS"])
        o[31] = "MISSED"
        self.assertFalse(burnin_acceptance(self.reg, synth_ledger(self.reg, o), self.end, 0)["PASS"])

    def test_injected_miss_excluded_and_must_be_real(self):
        reg = make_reg(self.tmp / "inj" if (self.tmp / "inj").mkdir() is None else None, planned=72, start=self.t0,
                       acceptance={"min_in_slot_captures": 70, "injected_miss_slot": 36})
        o = {s: "CAPTURED_VALID" for s in range(1, 73)}; o[36] = "MISSED"; o[50] = "SOURCE_FAILED"   # injected + 1 organic
        self.assertTrue(burnin_acceptance(reg, synth_ledger(reg, o), self.end, 0)["PASS"])
        o[51] = "MISSED"                                                                           # 2 organic -> fail
        self.assertFalse(burnin_acceptance(reg, synth_ledger(reg, o), self.end, 0)["PASS"])
        o = {s: "CAPTURED_VALID" for s in range(1, 73)}                                            # injection did not happen
        self.assertFalse(burnin_acceptance(reg, synth_ledger(reg, o), self.end, 0)["PASS"])

    def test_any_persistence_incident_fails(self):
        raw = synth_ledger(self.reg, {s: "CAPTURED_VALID" for s in range(1, 73)})
        self.assertFalse(burnin_acceptance(self.reg, raw, self.end, 1)["PASS"])

    def test_incomplete_window_fails(self):
        raw = synth_ledger(self.reg, {s: "CAPTURED_VALID" for s in range(1, 40)})
        self.assertFalse(burnin_acceptance(self.reg, raw, self.t0 + dt.timedelta(hours=40), 0)["PASS"])


class Replay14D1(unittest.TestCase):
    """Drive v2 through the REAL 14D.1 calendar: run only where 14D.1 actually ran (recorded bytes,
    recorded times); nothing after slot 48 (task dead). Then ask when the watchdog would have alerted."""

    def test_counterfactual(self):
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp)
        reg = make_reg(tmp, start=START_14D1, planned=336)
        be = DirBackend(tmp / "store")
        for c in CAPTURES:
            r = run_once(reg, VirtualClock(cap_start(c)), FixtureTransport(cap_dir(c)), be, RecordingNotifier(), f"replay-{c['slot_14d1']}")
            self.assertEqual((c["slot_14d1"], r.status), (c["slot_14d1"], "CAPTURED_VALID"))
        rep = verify_store(reg, be)
        self.assertEqual(rep["derived_recomputed"], 24)
        st = rep["slot_status"]
        self.assertEqual(sum(v == "SUCCESS" for v in st.values()), 24)
        self.assertEqual(sum(v == "MISSED" for v in st.values()), 24)  # every slot 1-48 accounted (14D.1 had 9 without rows)
        self.assertEqual(sorted(int(k) for k, v in st.items() if v == "MISSED"),
                         [2, 5, 6, 7, 8, 9, 11, 13, 16, 19, 20, 22, 23, 24, 25, 30, 36, 37, 38, 39, 40, 41, 45, 46])

        all_rows = L.parse(be.read(L.LEDGER_PATH))
        first = {}
        t = START_14D1
        while t <= dt.datetime(2026, 10, 12, 21, 0, tzinfo=UTC):  # through slot 90
            # the ledger exactly as it existed at time t (chronological chain => valid prefix)
            raw = L.serialise([r for r in all_rows if L.parse_utc(r["written_utc"]) <= t])
            for kind, _ in evaluate(reg, raw, t)["alerts"]:
                first.setdefault(kind, t)
            t += dt.timedelta(minutes=5)
        # The replayed ledger stops at slot 48, so slots 49+ have no row: watchdog sees silence.
        self.assertEqual(first["MISSING_SLOT"], dt.datetime(2026, 10, 9, 5, 15, tzinfo=UTC))       # slot 2 + 15 min
        self.assertEqual(first["BLOCKED_TWO_CONSECUTIVE"], dt.datetime(2026, 10, 9, 9, 15, tzinfo=UTC))  # slots 5-6
        self.assertEqual(first["GATE_UNREACHABLE_OVERALL"], dt.datetime(2026, 10, 11, 13, 15, tzinfo=UTC))  # 34th miss = slot 58, +15 min grace
        timeline = {k: {"utc": utc_iso(v), "brisbane": v.astimezone(dt.timezone(dt.timedelta(hours=10))).isoformat()} for k, v in sorted(first.items(), key=lambda x: x[1])}
        out = os.environ.get("TA1C2_EVIDENCE_OUT")
        if out:
            Path(out).mkdir(parents=True, exist_ok=True)
            (Path(out) / "replay_14D1_watchdog_timeline.json").write_text(json.dumps(
                {"note": "Counterfactual: v2 collector+watchdog over the real 14D.1 run history (recorded bytes/times)",
                 "first_alert_times": timeline, "ledger_rows": rep["ledger_rows"], "ledger_sha256": rep["ledger_sha256"]}, indent=1))


if __name__ == "__main__":
    unittest.main()
