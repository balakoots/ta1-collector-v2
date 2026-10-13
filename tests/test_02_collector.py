"""Collector state machine on DirBackend: idempotency, no-backfill, freshness, faults, fail-closed."""
import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import CAPTURES, ROOT, cap_dir, cap_start, make_reg, NetworkBlocked
from ta1c2 import ledger as L
from ta1c2.collector import run_once
from ta1c2.core import ConfigRefused, VirtualClock
from ta1c2.notify import HealthchecksNotifier, RecordingNotifier
from ta1c2.sources import FaultTransport, FixtureTransport, LiveDisarmed, LiveTransport
from ta1c2.storage import DirBackend, FaultyBackend, SimulatedCrash
from ta1c2.verify import verify_store

C1 = CAPTURES[0]           # 14D.1 slot 1, recorded 2026-10-09T03:18:58Z
C3 = CAPTURES[1]           # 14D.1 slot 3


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.tmp)
        self.reg = make_reg(self.tmp)
        self.be = DirBackend(self.tmp / "store")
        self.n = RecordingNotifier()

    def run_at(self, cap, when=None, transport=None, backend=None, run_id="r1"):
        clock = VirtualClock(when or cap_start(cap))
        return run_once(self.reg, clock, transport or FixtureTransport(cap_dir(cap)), backend or self.be, self.n, run_id)

    def rows(self, be=None):
        return L.parse((be or self.be).read(L.LEDGER_PATH) or b"")


class HappyPathAndIdempotency(Base):
    def test_capture_valid_persisted_verified_and_pinged(self):
        r = self.run_at(C1)
        self.assertEqual(r.status, "CAPTURED_VALID"); self.assertEqual(r.exit_code, 0); self.assertEqual(r.slot, 1)
        rep = verify_store(self.reg, self.be)
        self.assertEqual(rep["derived_recomputed"], 1); self.assertEqual(rep["orphan_files"], [])
        self.assertEqual(self.n.events[-1][0], "success")
        self.assertEqual(self.n.events[-1][2], (1, self.rows()[0]["row_sha256"]))

    def test_second_trigger_same_slot_is_noop(self):
        self.run_at(C1)
        r2 = self.run_at(C1, when=cap_start(C1) + dt.timedelta(minutes=30), run_id="r2")
        self.assertEqual(r2.status, "NOOP_ALREADY_CAPTURED"); self.assertEqual(len(self.rows()), 1)

    def test_retry_after_partial_then_exhausted(self):
        r1 = self.run_at(C1, transport=FaultTransport(FixtureTransport(cap_dir(C1)), {"coinbase_ETH": "HTTP_500"}))
        self.assertEqual(r1.status, "CAPTURED_PARTIAL"); self.assertEqual(r1.exit_code, 10)
        r2 = self.run_at(C1, when=cap_start(C1) + dt.timedelta(seconds=60), run_id="r2",
                         transport=FaultTransport(FixtureTransport(cap_dir(C1)), {"coinbase_ETH": "TIMEOUT"}))
        self.assertEqual(r2.status, "CAPTURED_PARTIAL")
        r3 = self.run_at(C1, when=cap_start(C1) + dt.timedelta(seconds=120), run_id="r3")
        self.assertEqual(r3.status, "NOOP_ATTEMPTS_EXHAUSTED")
        self.assertEqual([x["attempt"] for x in self.rows()], [1, 2])


class NoBackfill(Base):
    def test_missed_slot_gets_row_and_cannot_be_captured_later(self):
        self.run_at(C1)                                       # slot 1
        r = self.run_at(C3, run_id="r3")                      # slot 3 at its recorded time
        self.assertEqual(r.status, "CAPTURED_VALID")
        kinds = [(x["slot"], x["kind"]) for x in self.rows()]
        self.assertEqual(kinds, [(1, "ATTEMPT"), (2, "MISSED"), (3, "ATTEMPT")])
        # Slot is derived only from the clock: there is no API to target slot 2 now.
        self.assertEqual(L.slot_status(self.rows())[2], "MISSED")

    def test_validator_rejects_forged_backfill_row(self):
        self.run_at(C1)
        rows = self.rows()
        forged = dict(rows[0]); forged.update(slot=2, slot_id="TA1-C2-TEST:0002")  # attempt run_start lies in slot 1
        forged = L.append(rows, {k: v for k, v in forged.items() if k not in ("seq", "prev_row_sha256", "row_sha256")})
        with self.assertRaises(L.LedgerInvalid):
            L.validate(self.reg, rows + [forged])

    def test_outside_window_writes_nothing(self):
        r = self.run_at(C1, when=dt.datetime(2026, 10, 9, 2, 59, tzinfo=dt.timezone.utc))
        self.assertEqual(r.status, "OUTSIDE_WINDOW"); self.assertIsNone(self.be.read(L.LEDGER_PATH))


class Timing(Base):
    def test_late_start_guard(self):
        late = dt.datetime(2026, 10, 9, 3, 59, 30, tzinfo=dt.timezone.utc)
        r = self.run_at(C1, when=late)
        self.assertEqual(r.status, "NOT_STARTED_TOO_LATE"); self.assertEqual(self.rows()[0]["artifact_dir"], None)

    def test_receipt_after_slot_end_excluded_but_preserved(self):
        r = self.run_at(C1, transport=FaultTransport(FixtureTransport(cap_dir(C1)), {"ecb_fx": "LATE_RECEIPT"}))
        self.assertEqual(r.status, "LATE_EXCLUDED")
        self.assertEqual(verify_store(self.reg, self.be)["artifacts_verified"], 16)

    def test_stale_recorded_book_rejected(self):
        """Real recorded bytes replayed 10 minutes late: Coinbase server age > 60 s."""
        r = self.run_at(C1, when=cap_start(C1) + dt.timedelta(minutes=10))
        self.assertEqual(r.status, "CAPTURED_PARTIAL")
        row = self.rows()[0]
        self.assertEqual(row["invalid_sources"], ["coinbase_BTC", "coinbase_ETH"])
        self.assertIn("stale", row["sources"]["coinbase_BTC"]["reason"])

    def test_duplicate_book_bodies_flagged(self):
        self.run_at(C1)
        # replay slot-1 bytes inside slot 2 with fresh-looking timing offsets: books are byte-identical
        r = self.run_at(C1, when=cap_start(C1) + dt.timedelta(hours=1), run_id="dup")
        row = self.rows()[-1]
        for k in ("coinspot_depth_BTC", "coinspot_depth_ETH"):
            self.assertTrue(row["sources"][k]["duplicate_of_prior"]); self.assertFalse(row["sources"][k]["valid"])
        self.assertNotEqual(r.status, "CAPTURED_VALID")


class ClockAudit(Base):
    def test_recorded_http_date_skew_small(self):
        self.run_at(C1)
        skews = [c["http_date_skew_s"] for c in self.rows()[0]["sources"].values() if c["http_date_skew_s"] is not None]
        self.assertGreaterEqual(len(skews), 5); self.assertTrue(all(abs(x) < 5 for x in skews), skews)

    def test_skewed_runner_clock_alerts(self):
        from ta1c2.watchdog import evaluate
        self.run_at(C1, when=cap_start(C1) + dt.timedelta(minutes=5))   # runner clock 5 min fast vs servers
        rep = evaluate(self.reg, self.be.read(L.LEDGER_PATH), cap_start(C1) + dt.timedelta(minutes=10))
        self.assertIn("CLOCK_SKEW", [a[0] for a in rep["alerts"]])


class SourceFaults(Base):
    def test_all_sources_failed_recorded_not_crash(self):
        faults = {k: "TIMEOUT" for k in ["coinbase_BTC", "coinbase_ETH", "coinspot_quote_BTC", "coinspot_quote_ETH", "coinspot_depth_BTC", "coinspot_depth_ETH", "ecb_fx"]}
        r = self.run_at(C1, transport=FaultTransport(FixtureTransport(cap_dir(C1)), faults))
        self.assertEqual(r.status, "SOURCE_FAILED"); self.assertEqual(self.n.events[-1][0], "fail")
        self.assertEqual(verify_store(self.reg, self.be)["ledger_rows"], 1)

    def test_truncated_body_and_fx_loss(self):
        r = self.run_at(C1, transport=FaultTransport(FixtureTransport(cap_dir(C1)), {"ecb_fx": "HTTP_500", "coinspot_depth_ETH": "TRUNCATED"}))
        row = self.rows()[0]
        self.assertEqual(r.status, "CAPTURED_PARTIAL")
        # without FX the Coinbase AUD sizing is UNKNOWN, so Coinbase books are not valid at all sizes
        self.assertIn("coinbase_BTC", row["invalid_sources"]); self.assertIn("coinspot_depth_ETH", row["invalid_sources"])
        self.assertTrue(row["sources"]["coinspot_depth_BTC"]["valid"])


class PersistenceFaults(Base):
    def test_commit_oserror_leaves_ledger_untouched(self):
        r = self.run_at(C1, backend=FaultyBackend(self.be, "COMMIT_OSERROR"))
        self.assertEqual(r.status, "PERSISTENCE_FAILED"); self.assertEqual(r.exit_code, 20)
        self.assertIsNone(self.be.read(L.LEDGER_PATH)); self.assertEqual(self.n.events[-1][0], "fail")

    def test_crash_between_artifacts_and_ledger_is_recoverable(self):
        with self.assertRaises(SimulatedCrash):
            self.run_at(C1, backend=FaultyBackend(self.be, "CRASH_AFTER_ARTIFACTS"))
        self.assertIsNone(self.be.read(L.LEDGER_PATH))
        r = self.run_at(C1, when=cap_start(C1) + dt.timedelta(seconds=30), run_id="r2")   # second trigger recovers
        self.assertEqual(r.status, "CAPTURED_VALID")
        rep = verify_store(self.reg, self.be)
        self.assertEqual(len(rep["orphan_files"]), 17)  # crashed attempt's 16 files + MANIFEST: unreferenced, reported, harmless

    def test_lost_write_detected_by_readback_then_halts(self):
        r = self.run_at(C1, backend=FaultyBackend(self.be, "LOST_WRITE"))
        self.assertEqual(r.status, "PERSISTENCE_READBACK_MISMATCH"); self.assertEqual(r.exit_code, 20)
        r2 = self.run_at(C3, run_id="r2")
        self.assertEqual(r2.status, "HALTED_INTEGRITY"); self.assertEqual(r2.exit_code, 30)

    def test_bitrot_after_commit_halts_next_run(self):
        self.run_at(C1, backend=FaultyBackend(self.be, "BITROT_AFTER_COMMIT"))
        r2 = self.run_at(C3, run_id="r2")
        self.assertEqual(r2.status, "HALTED_INTEGRITY")
        self.assertEqual(len(self.rows()), 1)  # nothing appended while halted

    def test_ledger_tamper_halts(self):
        self.run_at(C1)
        p = self.be.root / L.LEDGER_PATH
        p.write_bytes(p.read_bytes().replace(b"CAPTURED_VALID", b"CAPTURED_PARTIAL"))
        self.assertEqual(self.run_at(C3, run_id="r2").status, "HALTED_INTEGRITY")

    def test_blank_line_ledger_halts(self):
        self.run_at(C1)
        p = self.be.root / L.LEDGER_PATH
        p.write_bytes(p.read_bytes() + b"\n")
        self.assertEqual(self.run_at(C3, run_id="r2").status, "HALTED_INTEGRITY")

    def test_storage_refuses_non_append(self):
        self.run_at(C1)
        from ta1c2.core import sha256
        cur = self.be.read(L.LEDGER_PATH)
        from ta1c2.storage import AppendOnlyViolation
        with self.assertRaises(AppendOnlyViolation):
            self.be.commit({}, b"", sha256(cur))


class Governance(Base):
    def test_code_freeze_mismatch_refuses(self):
        pkg = self.tmp / "pkgcopy"; shutil.copytree(ROOT / "ta1c2", pkg)
        (pkg / "analysis.py").write_text((pkg / "analysis.py").read_text() + "\n# drift\n")
        clock = VirtualClock(cap_start(C1))
        r = run_once(self.reg, clock, FixtureTransport(cap_dir(C1)), self.be, self.n, "r1", pkg_dir=pkg)
        self.assertEqual(r.status, "REFUSED"); self.assertIn("code freeze", r.detail); self.assertIsNone(self.be.read(L.LEDGER_PATH))

    def test_live_transport_disarmed_by_default(self):
        with self.assertRaises(LiveDisarmed):
            LiveTransport(self.reg, None)
        live_unapproved = make_reg(self.tmp, mode="live", approved=False)
        with self.assertRaises(LiveDisarmed):
            LiveTransport(live_unapproved, live_unapproved.sha256)
        with self.assertRaises(PermissionError):
            HealthchecksNotifier("https://example.invalid/ping", live_unapproved, live_unapproved.sha256)

    def test_armed_live_transport_still_cannot_reach_network_in_tests(self):
        live = make_reg(self.tmp, mode="live", approved=True)
        t = LiveTransport(live, live.sha256)
        res = t.fetch_all(VirtualClock(cap_start(C1)))
        self.assertTrue(all(r.status is None and "NetworkBlocked" in r.error for r in res.values()))

    def test_live_registration_rejects_fixture_transport(self):
        live = make_reg(self.tmp, mode="live", approved=True)
        r = run_once(live, VirtualClock(cap_start(C1)), FixtureTransport(cap_dir(C1)), self.be, self.n, "r1")
        self.assertEqual(r.status, "REFUSED")

    def test_unscheduled_registration_refuses(self):
        reg = make_reg(self.tmp, start=None)
        r = run_once(reg, VirtualClock(cap_start(C1)), FixtureTransport(cap_dir(C1)), self.be, self.n, "r1")
        self.assertEqual(r.status, "REFUSED")

    def test_bad_run_id_refused(self):
        r = self.run_at(C1, run_id="../../etc")
        self.assertEqual(r.status, "REFUSED")


if __name__ == "__main__":
    unittest.main()
