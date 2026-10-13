"""One collector run = one deterministic state-machine pass. No loops, no sleeps, no LLM.

Order: code-freeze check -> mode/transport coherence -> slot from clock -> lock -> verify full
ledger + every stored artifact (HALT on any mismatch) -> write MISSED rows for ended slots with no
attempts -> idempotency (no-op if slot already captured or attempts exhausted) -> late-start guard ->
capture -> analyse -> single atomic commit (artifacts then ledger, CAS + append-only) -> readback
verify -> notify (success ping only after verified readback).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import VERSION
from . import ledger as L
from .analysis import BOOK_SOURCES, analyse
from .core import PKG_DIR, ConfigRefused, Registration, canonical, parse_utc, sha256, slot_for, slot_id, slot_window, utc_iso, verify_code_frozen
from .sources import LiveTransport, SOURCE_KEYS
from .storage import ConcurrentModification, PersistenceError
from .verify import EvidenceInvalid, verify_artifacts

EXIT = {"CAPTURED_VALID": 0, "NOOP_ALREADY_CAPTURED": 0, "OUTSIDE_WINDOW": 0, "CAPTURED_PARTIAL": 10, "SOURCE_FAILED": 10,
        "LATE_EXCLUDED": 10, "NOT_STARTED_TOO_LATE": 10, "NOOP_ATTEMPTS_EXHAUSTED": 10, "MISSED_ROWS_ONLY": 10,
        "PERSISTENCE_FAILED": 20, "CONFLICT_ABORTED": 20, "PERSISTENCE_READBACK_MISMATCH": 20,
        "HALTED_INTEGRITY": 30, "REFUSED": 40}


@dataclass
class RunResult:
    status: str
    slot: int | None
    rows: list = field(default_factory=list)
    detail: str = ""
    receipt: dict | None = None

    @property
    def exit_code(self) -> int:
        return EXIT[self.status]


def _common(reg, code_sha, slot, run_id):
    return {"study_id": reg.study_id, "registration_sha256": reg.sha256, "code_manifest_sha256": code_sha,
            "collector_version": VERSION, "slot": slot, "slot_id": slot_id(reg, slot), "run_id": run_id}


def run_once(reg: Registration, clock, transport, backend, notifier, run_id: str, trigger: str = "schedule", pkg_dir=PKG_DIR) -> RunResult:
    # 1. configuration gates (fail closed, write nothing)
    try:
        code_sha = verify_code_frozen(reg, pkg_dir)
        if reg.mode == "live" and (reg.approved is not True or not isinstance(transport, LiveTransport)):
            raise ConfigRefused("live mode requires an approved registration and the armed LiveTransport")
        if reg.mode == "fixture" and isinstance(transport, LiveTransport):
            raise ConfigRefused("fixture-mode registration may not use the live transport")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", run_id):
            raise ConfigRefused("run_id must match [A-Za-z0-9._-]{1,64}")
        reg.start  # noqa: B018 — raises if not scheduled
    except ConfigRefused as e:
        return RunResult("REFUSED", None, detail=str(e))

    now = clock.now()
    slot = slot_for(reg, now)
    if slot is None:
        return RunResult("OUTSIDE_WINDOW", None, detail="clock outside registered window; nothing written")
    notifier.start(slot)

    try:
        with backend.lock():
            raw = backend.read(L.LEDGER_PATH) or b""
            try:
                rows = L.parse(raw)
                L.validate(reg, rows)
                verify_artifacts(backend, rows)
            except (L.LedgerInvalid, EvidenceInvalid, ValueError, KeyError) as e:
                notifier.fail(slot, "HALTED_INTEGRITY: " + str(e))
                return RunResult("HALTED_INTEGRITY", slot, detail=str(e))

            work, new, files = list(rows), [], {}
            status = L.slot_status(rows)
            for j in range(1, slot):  # gap rows for ended slots that never had an attempt (no data, no backfill)
                if j not in status:
                    row = L.append(work, {**_common(reg, code_sha, j, run_id), "kind": "MISSED", "outcome": "MISSED",
                                          "written_utc": utc_iso(now), "reason": "no attempt recorded before slot ended; recorded by later run"})
                    work.append(row); new.append(row)

            cur = [r for r in rows if r["slot"] == slot and r["kind"] == "ATTEMPT"]
            if any(r["outcome"] == L.SUCCESS for r in cur):
                result_status = "NOOP_ALREADY_CAPTURED"
            elif len(cur) >= reg.max_attempts_per_slot:
                result_status = "NOOP_ATTEMPTS_EXHAUSTED"
            else:
                attempt = len(cur) + 1
                s0, s1 = slot_window(reg, slot)
                base = {**_common(reg, code_sha, slot, run_id), "kind": "ATTEMPT", "attempt": attempt, "trigger": trigger,
                        "run_start_utc": utc_iso(now)}
                if (s1 - now).total_seconds() < reg.latest_start_margin_s:
                    row = L.append(work, {**base, "outcome": "NOT_STARTED_TOO_LATE", "written_utc": utc_iso(clock.now()),
                                          "artifact_dir": None, "manifest_sha256": None, "sources": {}, "invalid_sources": []})
                else:
                    responses = transport.fetch_all(clock)
                    prior = {}
                    for r in rows:
                        for k in BOOK_SOURCES:
                            h = r.get("sources", {}).get(k, {}).get("sha256")
                            if h:
                                prior.setdefault(k, set()).add(h)
                    derived = analyse(responses, reg.sizes_aud, reg.freshness_limit_s, prior)
                    checks = derived["source_checks"]
                    adir = f"evidence/{reg.study_id}/slot_{slot:04d}/attempt_{attempt}_{run_id}"
                    for k in SOURCE_KEYS:
                        r = responses[k]
                        if r.body is not None:
                            files[f"{adir}/{k}.raw"] = r.body
                        meta = {"source": k, "url": r.url, "status": r.status, "headers": r.headers, "request_utc": r.request_utc,
                                "receipt_utc": r.receipt_utc, "error": r.error, "sha256": checks[k]["sha256"],
                                "bytes": checks[k]["bytes"], "provenance": r.provenance}
                        files[f"{adir}/{k}.meta.json"] = json.dumps(meta, indent=2, sort_keys=True).encode()
                    files[f"{adir}/derived.json"] = canonical(derived)
                    files[f"{adir}/run.json"] = canonical({**base, "slot_window_utc": [utc_iso(s0), utc_iso(s1)]})
                    manifest = canonical({p.rsplit("/", 1)[1]: sha256(b) for p, b in sorted(files.items())})
                    files[f"{adir}/MANIFEST.json"] = manifest
                    late = any(r.receipt_utc and parse_utc(r.receipt_utc) >= s1 for r in responses.values())
                    nvalid = sum(1 for c in checks.values() if c["valid"])
                    outcome = ("LATE_EXCLUDED" if late else "CAPTURED_VALID" if nvalid == len(SOURCE_KEYS)
                               else "CAPTURED_PARTIAL" if nvalid else "SOURCE_FAILED")
                    src = {k: {kk: checks[k].get(kk) for kk in ("http_status", "sha256", "valid", "reason", "duplicate_of_prior", "server_age_s", "http_date_skew_s")}
                           for k in SOURCE_KEYS}
                    row = L.append(work, {**base, "outcome": outcome, "written_utc": utc_iso(clock.now()), "artifact_dir": adir,
                                          "manifest_sha256": sha256(manifest), "sources": src,
                                          "invalid_sources": sorted(k for k in SOURCE_KEYS if not checks[k]["valid"])})
                work.append(row); new.append(row)
                result_status = row["outcome"]

            if not new:
                anchor = (rows[-1]["seq"], rows[-1]["row_sha256"]) if rows else None
                if result_status == "NOOP_ALREADY_CAPTURED":
                    notifier.success(slot, anchor)
                else:
                    notifier.fail(slot, result_status)
                return RunResult(result_status, slot)

            new_ledger = raw + b"".join(canonical(r) + b"\n" for r in new)
            try:
                receipt = backend.commit(files, new_ledger, sha256(raw), msg=f"{reg.study_id} slot {slot} {result_status} run {run_id}")
            except ConcurrentModification as e:
                notifier.fail(slot, "CONFLICT_ABORTED: " + str(e))
                return RunResult("CONFLICT_ABORTED", slot, detail=str(e))
            except (PersistenceError, OSError) as e:
                notifier.fail(slot, "PERSISTENCE_FAILED: " + str(e))
                return RunResult("PERSISTENCE_FAILED", slot, detail=f"{type(e).__name__}: {e}")

            expected = {p: sha256(b) for p, b in files.items()}
            expected[L.LEDGER_PATH] = sha256(new_ledger)
            got = backend.readback(list(expected))
            bad = sorted(p for p in expected if got.get(p) != expected[p])
            if bad:
                notifier.fail(slot, f"PERSISTENCE_READBACK_MISMATCH: {bad[:3]}")
                return RunResult("PERSISTENCE_READBACK_MISMATCH", slot, new, detail=str(bad), receipt=receipt)

            anchor = (new[-1]["seq"], new[-1]["row_sha256"])
            if result_status in ("CAPTURED_VALID", "NOOP_ALREADY_CAPTURED"):
                notifier.success(slot, anchor)
            else:
                notifier.fail(slot, result_status)
            return RunResult(result_status, slot, new, receipt=receipt)
    except PersistenceError as e:  # e.g. lock timeout
        notifier.fail(slot, "PERSISTENCE_FAILED: " + str(e))
        return RunResult("PERSISTENCE_FAILED", slot, detail=str(e))
