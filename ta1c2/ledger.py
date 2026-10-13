"""Append-only, hash-chained run ledger (canonical JSON Lines).

Every row: seq (1..n, contiguous), prev_row_sha256, row_sha256 = SHA-256(canonical(row - row_sha256)).
Kinds:
  ATTEMPT — a collector run for its *current* slot (request time inside the slot window).
  MISSED  — written by a later run for an ended slot that had no attempt. Never contains data.
Rules enforced by `validate`: strict canonical bytes, no blank lines, contiguous seq, chain,
unique (slot, attempt), no ATTEMPT after a MISSED row for the same slot, attempts <= max,
attempt request_start inside its slot window, MISSED only for slots that had ended when written.
Tail truncation is undetectable from the file alone; `validate(anchor=…)` checks an external
(seq,row_sha256) anchor (e.g. the dead-man ping log or the watchdog's last-seen head).
"""
from __future__ import annotations

from .core import GENESIS, Registration, canonical, parse_utc, sha256, slot_window

LEDGER_PATH = "ledger/ledger.jsonl"
SUCCESS = "CAPTURED_VALID"
SUCCESS_STATUS = "SUCCESS"  # value used by slot_status()
ATTEMPT_OUTCOMES = {"CAPTURED_VALID", "CAPTURED_PARTIAL", "SOURCE_FAILED", "LATE_EXCLUDED", "NOT_STARTED_TOO_LATE"}


class LedgerInvalid(Exception):
    pass


def parse(raw: bytes) -> list[dict]:
    import json
    if raw == b"":
        return []
    if not raw.endswith(b"\n"):
        raise LedgerInvalid("ledger must end with newline (possible torn write)")
    rows = []
    for i, line in enumerate(raw[:-1].split(b"\n"), 1):
        if not line:
            raise LedgerInvalid(f"blank line at {i}")
        try:
            row = json.loads(line)
        except ValueError as e:
            raise LedgerInvalid(f"unparseable line {i}") from e
        if canonical(row) != line:
            raise LedgerInvalid(f"non-canonical encoding at line {i}")
        rows.append(row)
    return rows


def seal(row: dict) -> dict:
    body = {k: v for k, v in row.items() if k != "row_sha256"}
    return {**body, "row_sha256": sha256(canonical(body))}


def append(rows: list[dict], new: dict) -> dict:
    prev = rows[-1] if rows else None
    new = {**new, "seq": (prev["seq"] + 1) if prev else 1, "prev_row_sha256": prev["row_sha256"] if prev else GENESIS}
    return seal(new)


def serialise(rows: list[dict]) -> bytes:
    return b"".join(canonical(r) + b"\n" for r in rows)


def validate(reg: Registration, rows: list[dict], anchor: tuple[int, str] | None = None) -> None:
    prev = GENESIS
    seen, missed, attempts = set(), set(), {}
    for i, r in enumerate(rows, 1):
        if r.get("seq") != i:
            raise LedgerInvalid(f"seq discontinuity at row {i}")
        if r.get("prev_row_sha256") != prev:
            raise LedgerInvalid(f"chain break at seq {i}")
        if seal(r)["row_sha256"] != r.get("row_sha256"):
            raise LedgerInvalid(f"row hash mismatch at seq {i}")
        if r.get("study_id") != reg.study_id or r.get("registration_sha256") != reg.sha256:
            raise LedgerInvalid(f"row {i} belongs to another study/registration")
        slot, kind = r["slot"], r["kind"]
        s0, s1 = slot_window(reg, slot)
        if kind == "MISSED":
            if slot in missed or slot in attempts:
                raise LedgerInvalid(f"MISSED row for slot {slot} conflicts with existing rows")
            if parse_utc(r["written_utc"]) < s1:
                raise LedgerInvalid(f"MISSED row for slot {slot} written before slot ended")
            missed.add(slot)
        elif kind == "ATTEMPT":
            key = (slot, r["attempt"])
            if key in seen or slot in missed:
                raise LedgerInvalid(f"duplicate or post-MISSED attempt for slot {slot}")
            if r["outcome"] not in ATTEMPT_OUTCOMES:
                raise LedgerInvalid(f"unknown outcome at seq {i}")
            if not (s0 <= parse_utc(r["run_start_utc"]) < s1):
                raise LedgerInvalid(f"attempt at seq {i} started outside its slot window (backfill)")
            attempts[slot] = attempts.get(slot, 0) + 1
            if attempts[slot] > reg.max_attempts_per_slot or r["attempt"] != attempts[slot]:
                raise LedgerInvalid(f"attempt numbering invalid for slot {slot}")
            seen.add(key)
        else:
            raise LedgerInvalid(f"unknown row kind at seq {i}")
        prev = r["row_sha256"]
    if anchor is not None:
        aseq, ahash = anchor
        if aseq > len(rows) or rows[aseq - 1]["row_sha256"] != ahash:
            raise LedgerInvalid(f"external anchor (seq {aseq}) not matched: truncation or rewrite")


def slot_status(rows: list[dict]) -> dict[int, str]:
    """slot -> 'SUCCESS' | 'MISSED' | 'FAILED' (attempted, no valid capture)."""
    st = {}
    for r in rows:
        s = r["slot"]
        if r["kind"] == "MISSED":
            st[s] = "MISSED"
        elif r["outcome"] == SUCCESS:
            st[s] = "SUCCESS"
        elif st.get(s) != "SUCCESS":
            st[s] = "FAILED"
    return st
