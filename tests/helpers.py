"""Shared test helpers. Importing this module installs a hard network block: any socket connect,
DNS lookup or urlopen raises NetworkBlocked. Every test module imports it first."""
import datetime as dt
import json
import socket
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class NetworkBlocked(RuntimeError):
    pass


def _blocked(*a, **k):
    raise NetworkBlocked("network access is forbidden in fixture tests")


_orig_connect = socket.socket.connect


def _connect(self, addr):
    if self.family == getattr(socket, "AF_UNIX", None):  # local IPC (multiprocessing) only
        return _orig_connect(self, addr)
    raise NetworkBlocked("IP network access is forbidden in fixture tests")


socket.socket.connect = _connect
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
urllib.request.urlopen = _blocked

from ta1c2.core import Registration, code_manifest_sha256, parse_utc  # noqa: E402

FIX = ROOT / "fixtures"
PROV = json.loads((FIX / "PROVENANCE.json").read_text())
CAPTURES = sorted(PROV["captures"], key=lambda c: c["slot_14d1"])  # 24 recorded runs
START_14D1 = dt.datetime(2026, 10, 9, 3, tzinfo=dt.timezone.utc)


def cap_dir(c) -> Path:
    return FIX / "recorded" / c["capture_id"]


def cap_start(c) -> dt.datetime:
    """Recorded run start = earliest recorded request time."""
    d = cap_dir(c)
    return min(parse_utc(json.loads(p.read_text())["request_utc"]) for p in d.glob("*.meta.json"))


def make_reg(tmp: Path, start=START_14D1, planned=336, mode="fixture", approved=False, **over) -> Registration:
    data = {"study_id": "TA1-C2-TEST", "approved": approved, "mode": mode, "start_utc": start.isoformat().replace("+00:00", "Z") if start else None,
            "planned_slots": planned, "slot_seconds": 3600, "latest_start_margin_s": 45, "max_attempts_per_slot": 2,
            "freshness_limit_s": 60, "request_timeout_s": 20, "watchdog_grace_s": 900, "sizes_aud": [500, 1000, 2500],
            "gates": {"overall": 0.90, "per_utc_hour": 0.80, "depth": 0.95}, "code_manifest_sha256": code_manifest_sha256(),
            "acceptance": {"min_in_slot_captures": planned - 1}}
    data.update(over)
    p = Path(tmp) / "registration.json"
    p.write_text(json.dumps(data, indent=1, sort_keys=True))
    return Registration.load(p)
