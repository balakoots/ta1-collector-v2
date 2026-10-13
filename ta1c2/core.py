"""Shared primitives: hashing, canonical JSON, UTC handling, registration, slot clock, code freeze."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

GENESIS = "0" * 64
PKG_DIR = Path(__file__).resolve().parent


class ConfigRefused(Exception):
    """Configuration, registration or code-freeze check failed: refuse to run (fail closed)."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def utc_iso(t: dt.datetime) -> str:
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return t.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(s: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError("timezone-aware timestamp required: " + s)
    return t.astimezone(dt.timezone.utc)


# ---------------------------------------------------------------- clocks
class SystemClock:
    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)


class VirtualClock:
    """Deterministic clock for fixture tests. Never advances on its own."""

    def __init__(self, t: dt.datetime):
        self.t = t.astimezone(dt.timezone.utc)

    def now(self) -> dt.datetime:
        return self.t

    def set(self, t: dt.datetime) -> None:
        self.t = t.astimezone(dt.timezone.utc)

    def advance(self, seconds: float) -> None:
        self.t = self.t + dt.timedelta(seconds=seconds)


# ---------------------------------------------------------------- code freeze
def code_manifest(pkg_dir: Path = PKG_DIR) -> dict:
    files = sorted(p for p in Path(pkg_dir).glob("*.py"))
    return {p.name: sha256(p.read_bytes()) for p in files}


def code_manifest_sha256(pkg_dir: Path = PKG_DIR) -> str:
    return sha256(canonical(code_manifest(pkg_dir)))


# ---------------------------------------------------------------- registration
REQUIRED = {
    "study_id": str, "approved": bool, "mode": str, "start_utc": (str, type(None)),
    "planned_slots": int, "slot_seconds": int, "latest_start_margin_s": int,
    "max_attempts_per_slot": int, "freshness_limit_s": int, "request_timeout_s": int,
    "watchdog_grace_s": int, "sizes_aud": list, "gates": dict, "code_manifest_sha256": str,
}


@dataclass(frozen=True)
class Registration:
    raw: bytes
    data: dict

    @property
    def sha256(self) -> str:
        return sha256(self.raw)

    def __getattr__(self, k):
        try:
            return self.data[k]
        except KeyError as e:
            raise AttributeError(k) from e

    @property
    def start(self) -> dt.datetime:
        if not self.data["start_utc"]:
            raise ConfigRefused("registration has no start_utc (not yet approved/scheduled)")
        return parse_utc(self.data["start_utc"])

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Registration":
        data = json.loads(raw)
        for k, t in REQUIRED.items():
            if k not in data or not isinstance(data[k], t):
                raise ConfigRefused(f"registration field invalid or missing: {k}")
        if data["mode"] not in ("fixture", "live"):
            raise ConfigRefused("mode must be 'fixture' or 'live'")
        if data["planned_slots"] < 1 or data["slot_seconds"] != 3600:
            raise ConfigRefused("hourly slots (3600 s) and >=1 planned slot required")
        if not 1 <= data["max_attempts_per_slot"] <= 2:
            raise ConfigRefused("max_attempts_per_slot must be 1 or 2")
        for g in ("overall", "per_utc_hour", "depth"):
            if not 0 < float(data["gates"].get(g, 0)) <= 1:
                raise ConfigRefused("gates.overall/per_utc_hour/depth required in (0,1]")
        # RC2: the watchdog profile separates engineering burn-in acceptance from study coverage gates
        profile = data.get("watchdog_profile", "study")
        if profile not in ("study", "burnin"):
            raise ConfigRefused("watchdog_profile must be 'study' or 'burnin'")
        if profile == "burnin":
            acc = data.get("acceptance")
            if not isinstance(acc, dict) or not isinstance(acc.get("min_in_slot_captures"), int):
                raise ConfigRefused("burnin profile requires acceptance.min_in_slot_captures (int)")
            inj = acc.get("injected_miss_slot")
            if inj is not None and not (isinstance(inj, int) and 1 <= inj < data["planned_slots"]):
                raise ConfigRefused("acceptance.injected_miss_slot must be null or an int in [1, planned_slots)")
            counted = data["planned_slots"] - (1 if inj is not None else 0)
            if not 0 < acc["min_in_slot_captures"] <= counted:
                raise ConfigRefused("acceptance.min_in_slot_captures out of range")
        return cls(raw, data)

    @classmethod
    def load(cls, path) -> "Registration":
        return cls.from_bytes(Path(path).read_bytes())


def verify_code_frozen(reg: Registration, pkg_dir: Path = PKG_DIR) -> str:
    actual = code_manifest_sha256(pkg_dir)
    if actual != reg.data["code_manifest_sha256"]:
        raise ConfigRefused(f"code freeze mismatch: running {actual}, registered {reg.data['code_manifest_sha256']}")
    return actual


# ---------------------------------------------------------------- slot clock
def slot_for(reg: Registration, t: dt.datetime) -> int | None:
    """1-based slot index containing t, or None if outside the registered window. Derived only from the clock."""
    secs = (t - reg.start).total_seconds()
    if secs < 0:
        return None
    idx = int(secs // reg.slot_seconds) + 1
    return idx if idx <= reg.planned_slots else None


def slot_window(reg: Registration, idx: int) -> tuple[dt.datetime, dt.datetime]:
    if not 1 <= idx <= reg.planned_slots:
        raise ValueError("slot out of range")
    s = reg.start + dt.timedelta(seconds=(idx - 1) * reg.slot_seconds)
    return s, s + dt.timedelta(seconds=reg.slot_seconds)


def slot_id(reg: Registration, idx: int) -> str:
    return f"{reg.study_id}:{idx:04d}"


def ended_slots(reg: Registration, now: dt.datetime) -> int:
    """Number of slots whose window has fully ended at `now`."""
    secs = (now - reg.start).total_seconds()
    if secs <= 0:
        return 0
    return min(reg.planned_slots, int(secs // reg.slot_seconds))


def need(count: int, frac: float) -> int:
    return math.ceil(round(frac * count, 9))
