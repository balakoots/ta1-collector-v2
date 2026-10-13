"""Concurrency (real processes racing one slot) and GitBackend against a real local bare remote."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from helpers import CAPTURES, cap_dir, cap_start, make_reg, NetworkBlocked  # noqa: F401
from ta1c2 import ledger as L
from ta1c2.collector import run_once
from ta1c2.core import VirtualClock
from ta1c2.notify import RecordingNotifier
from ta1c2.sources import FixtureTransport
from ta1c2.storage import DirBackend, GitBackend
from ta1c2.verify import verify_store

C1, C3 = CAPTURES[0], CAPTURES[1]


WORKER = Path(__file__).resolve().with_name("race_worker.py")


class Concurrency(unittest.TestCase):
    """RC3: independent OS processes (subprocess), released together by a wall-clock go-time.
    No multiprocessing Pool/Manager/Barrier, so a slow or failed worker start can never deadlock the
    others: every worker and every trial has a hard timeout and fails with a clear message.
    Trials and processes can be scaled with TA1C2_RACE_TRIALS / TA1C2_RACE_PROCS."""

    def test_racing_triggers_never_corrupt_or_duplicate(self):
        trials = int(os.environ.get("TA1C2_RACE_TRIALS", "10"))
        procs = int(os.environ.get("TA1C2_RACE_PROCS", "8"))
        for t in range(trials):
            tmp = Path(tempfile.mkdtemp())
            try:
                reg = make_reg(tmp)
                go = time.time() + 1.5   # generous lead time so every worker is waiting before release
                ps = [subprocess.Popen([sys.executable, str(WORKER), str(tmp / "registration.json"), str(tmp / "store"), f"t{t}p{i}", repr(go)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(procs)]
                res = []
                for p in ps:
                    try:
                        out, err = p.communicate(timeout=60)
                    except subprocess.TimeoutExpired:
                        for q in ps:
                            q.kill()
                        self.fail(f"trial {t}: worker timed out after 60 s (no deadlock possible; investigate environment)")
                    self.assertEqual(p.returncode, 0, f"trial {t}: worker failed: {err[-2000:]}")
                    res.append(json.loads(out.strip().splitlines()[-1])["status"])
                self.assertEqual(res.count("CAPTURED_VALID"), 1, res)
                self.assertEqual(res.count("NOOP_ALREADY_CAPTURED"), procs - 1, res)
                rep = verify_store(reg, DirBackend(tmp / "store"))
                self.assertEqual(rep["ledger_rows"], 1)
                self.assertEqual(rep["orphan_files"], [])
            finally:
                shutil.rmtree(tmp)


def git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


@unittest.skipIf(shutil.which("git") is None, "git not installed")
class GitBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.tmp)
        self.reg = make_reg(self.tmp)
        origin = self.tmp / "origin.git"
        git("init", "--quiet", "--bare", str(origin))
        seed = self.tmp / "seed"
        git("init", "--quiet", "-b", "evidence", str(seed))
        (seed / "README.md").write_text("TA1 collector v2 evidence branch (test)\n")
        git("-c", "user.name=t", "-c", "user.email=t@invalid", "add", "-A", cwd=seed)
        git("-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "--quiet", "-m", "seed", cwd=seed)
        git("push", "--quiet", str(origin), "evidence", cwd=seed)
        self.a = self.tmp / "runnerA"; self.b = self.tmp / "runnerB"
        git("clone", "--quiet", "--branch", "evidence", str(origin), str(self.a))
        git("clone", "--quiet", "--branch", "evidence", str(origin), str(self.b))
        self.origin = origin

    def run_in(self, wd, cap, run_id, transport=None, when=None):
        return run_once(self.reg, VirtualClock(when or cap_start(cap)), transport or FixtureTransport(cap_dir(cap)), GitBackend(wd), RecordingNotifier(), run_id)

    def test_commit_push_readback_and_second_runner_noop(self):
        r = self.run_in(self.a, C1, "A1")
        self.assertEqual(r.status, "CAPTURED_VALID"); self.assertEqual(len(r.receipt["commit"]), 40)
        r2 = self.run_in(self.b, C1, "B1")
        self.assertEqual(r2.status, "NOOP_ALREADY_CAPTURED")
        be = GitBackend(self.b)
        with be.lock():
            rep = verify_store(self.reg, be)
        self.assertEqual(rep["ledger_rows"], 1); self.assertEqual(rep["derived_recomputed"], 1)

    def test_push_race_loser_aborts_cleanly(self):
        outer = self

        class Racing(FixtureTransport):
            def fetch_all(self, clock):
                # while runner B is mid-capture, runner A completes the same slot and pushes first
                res = outer.run_in(outer.a, C1, "A-wins")
                outer.assertEqual(res.status, "CAPTURED_VALID")
                return super().fetch_all(clock)

        r = self.run_in(self.b, C1, "B-loses", transport=Racing(cap_dir(C1)))
        self.assertEqual(r.status, "CONFLICT_ABORTED"); self.assertEqual(r.exit_code, 20)
        be = GitBackend(self.b)
        with be.lock():
            rep = verify_store(self.reg, be)
        self.assertEqual(rep["ledger_rows"], 1)
        self.assertEqual(rep["orphan_files"], [])  # loser's partial state never reached the remote

    def test_missed_rows_and_history_across_runners(self):
        self.assertEqual(self.run_in(self.a, C1, "A1").status, "CAPTURED_VALID")
        self.assertEqual(self.run_in(self.b, C3, "B3").status, "CAPTURED_VALID")
        be = GitBackend(self.a)
        with be.lock():
            rows = L.parse(be.read(L.LEDGER_PATH))
        self.assertEqual([(x["slot"], x["kind"]) for x in rows], [(1, "ATTEMPT"), (2, "MISSED"), (3, "ATTEMPT")])
        log = subprocess.run(["git", "--git-dir", str(self.origin), "log", "--oneline", "evidence"], capture_output=True, check=True).stdout.decode()
        self.assertEqual(len(log.strip().splitlines()), 3)  # seed + one atomic commit per run


if __name__ == "__main__":
    unittest.main()
