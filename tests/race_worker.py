"""RC3 race worker: one independent OS process (launched by test_03 via subprocess).

usage: python race_worker.py REGISTRATION STORE RUN_ID GO_EPOCH
Waits until wall-clock GO_EPOCH (all workers released together, no shared barrier objects), runs the
collector once against the recorded slot-1 fixture, and prints one JSON line with the outcome.
Imports `helpers`, so the IP/DNS network block is active inside every worker too."""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import helpers  # noqa: E402  (installs network guard; exposes fixtures)
from ta1c2.collector import run_once  # noqa: E402
from ta1c2.core import Registration, VirtualClock  # noqa: E402
from ta1c2.notify import RecordingNotifier  # noqa: E402
from ta1c2.sources import FixtureTransport  # noqa: E402
from ta1c2.storage import DirBackend  # noqa: E402


def main(reg_path, store, run_id, go_epoch):
    reg = Registration.load(reg_path)
    c1 = helpers.CAPTURES[0]
    transport = FixtureTransport(helpers.cap_dir(c1))
    delay = float(go_epoch) - time.time()
    if delay > 0:
        time.sleep(delay)
    r = run_once(reg, VirtualClock(helpers.cap_start(c1)), transport, DirBackend(store), RecordingNotifier(), run_id)
    print(json.dumps({"run_id": run_id, "status": r.status, "t_start": time.time()}))


if __name__ == "__main__":
    main(*sys.argv[1:5])
