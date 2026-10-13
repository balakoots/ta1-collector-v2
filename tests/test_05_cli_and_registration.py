"""CLI plumbing (in-process, network guard active) and the shipped PROPOSED registration."""
import contextlib
import datetime as dt
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import CAPTURES, ROOT, cap_dir, make_reg, NetworkBlocked  # noqa: F401
from ta1c2 import cli
from ta1c2.core import Registration, code_manifest_sha256
from ta1c2.sources import LiveDisarmed, LiveTransport


def run_cli(args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(args)
    return code, json.loads(buf.getvalue())


class CLI(unittest.TestCase):
    def test_freeze_matches(self):
        code, out = run_cli(["freeze"])
        self.assertEqual(code, 0); self.assertEqual(out["code_manifest_sha256"], code_manifest_sha256())

    def test_run_verify_watchdog_fixture_mode(self):
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp)
        now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
        make_reg(tmp, start=now - dt.timedelta(hours=1), planned=3)
        reg, store = str(tmp / "registration.json"), "dir:" + str(tmp / "store")
        code, out = run_cli(["run", "--registration", reg, "--backend", store, "--fixture-dir", str(cap_dir(CAPTURES[0])), "--run-id", "cli1"])
        # recorded (old) Coinbase books replayed now are stale -> partial, exit 10, still persisted
        # (NOT_STARTED_TOO_LATE only if the wall clock happens to be in the last 45 s of the hour)
        self.assertIn(out["status"], ("CAPTURED_PARTIAL", "NOT_STARTED_TOO_LATE")); self.assertEqual(code, 10)
        code, rep = run_cli(["verify", "--registration", reg, "--backend", store])
        self.assertEqual(code, 0); self.assertEqual(rep["ledger_rows"], 2)  # MISSED slot 1 + attempt slot 2
        code, wd = run_cli(["watchdog", "--registration", reg, "--backend", store])
        self.assertIn(code, (0, 3)); self.assertIn("alerts", wd)


class ProposedRegistration(unittest.TestCase):
    P = ROOT / "registration" / "TA1-C2-BURNIN-72H.PROPOSED.json"

    def test_proposed_registration_is_inert(self):
        reg = Registration.load(self.P)
        self.assertFalse(reg.approved); self.assertIsNone(reg.start_utc); self.assertEqual(reg.mode, "live")
        self.assertEqual(reg.planned_slots, 72)
        self.assertEqual(reg.code_manifest_sha256, code_manifest_sha256(), "registration must pin the shipped code")
        with self.assertRaises(LiveDisarmed):
            LiveTransport(reg, reg.sha256)


if __name__ == "__main__":
    unittest.main()
