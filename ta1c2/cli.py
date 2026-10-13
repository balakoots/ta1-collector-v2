"""Command line: python -m ta1c2.cli {freeze,run,verify,watchdog}

  freeze                               print the code manifest SHA-256 to register
  run      --registration R --backend dir:PATH|git:WORKDIR [--fixture-dir D] [--run-id ID]
  verify   --registration R --backend ... [--anchor SEQ:SHA]
  watchdog --registration R --backend ... [--now ISO] [--anchor SEQ:SHA]

Live mode reads TA1_C2_ARM (must equal the registration SHA-256) and TA1_C2_PING_URL. Without
both, and without an APPROVED live registration, live transport/notifier refuse to construct.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

from .collector import run_once
from .core import Registration, SystemClock, code_manifest, code_manifest_sha256, parse_utc
from .notify import HealthchecksNotifier, RecordingNotifier
from .sources import FixtureTransport, LiveTransport
from .storage import DirBackend, GitBackend
from .verify import verify_store
from .watchdog import evaluate
from . import ledger as L


def _backend(spec: str):
    kind, _, path = spec.partition(":")
    if kind == "dir":
        return DirBackend(path)
    if kind == "git":
        return GitBackend(path, branch=os.environ.get("TA1_C2_BRANCH", "evidence"))
    raise SystemExit("backend must be dir:PATH or git:WORKDIR")


def _anchor(s):
    if not s:
        return None
    a, b = s.split(":")
    return int(a), b


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ta1c2")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("freeze")
    for name in ("run", "verify", "watchdog"):
        p = sub.add_parser(name)
        p.add_argument("--registration", required=True)
        p.add_argument("--backend", required=True)
        p.add_argument("--anchor")
        if name == "run":
            p.add_argument("--fixture-dir")
            p.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", "") + ("-" + os.environ.get("GITHUB_RUN_ATTEMPT", "") if os.environ.get("GITHUB_RUN_ATTEMPT") else "") or uuid.uuid4().hex[:16])
            p.add_argument("--trigger", default=os.environ.get("GITHUB_EVENT_NAME", "manual"))
        if name == "watchdog":
            p.add_argument("--now")
    a = ap.parse_args(argv)

    if a.cmd == "freeze":
        print(json.dumps({"code_manifest": code_manifest(), "code_manifest_sha256": code_manifest_sha256()}, indent=2))
        return 0
    reg = Registration.load(a.registration)
    be = _backend(a.backend)
    if a.cmd == "run":
        if reg.mode == "fixture":
            if not a.fixture_dir:
                raise SystemExit("fixture mode requires --fixture-dir")
            transport, notifier = FixtureTransport(a.fixture_dir), RecordingNotifier()
        else:
            arm = os.environ.get("TA1_C2_ARM")
            transport = LiveTransport(reg, arm)
            notifier = HealthchecksNotifier(os.environ.get("TA1_C2_PING_URL", ""), reg, arm)
        res = run_once(reg, SystemClock(), transport, be, notifier, a.run_id, a.trigger)
        print(json.dumps({"status": res.status, "slot": res.slot, "exit_code": res.exit_code, "detail": res.detail,
                          "rows_written": [r["seq"] for r in res.rows], "receipt": res.receipt}, indent=2))
        return res.exit_code
    if a.cmd == "verify":
        with be.lock():  # git: syncs to the remote head first
            rep = verify_store(reg, be, _anchor(a.anchor))
        print(json.dumps(rep, indent=2))
        return 0
    now = parse_utc(a.now) if a.now else SystemClock().now()
    with be.lock():
        raw = be.read(L.LEDGER_PATH) or b""
    rep = evaluate(reg, raw, now, _anchor(a.anchor))
    print(json.dumps(rep, indent=2, default=list))
    return 0 if rep["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
