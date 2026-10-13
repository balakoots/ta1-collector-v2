"""Regenerate registration/TA1-C2-BURNIN-72H.PROPOSED.json pinned to the current code manifest.
usage: python -I tools/make_proposed_registration.py   (run from repo root)"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ta1c2.core import code_manifest, code_manifest_sha256  # noqa: E402
from ta1c2.sources import SOURCES  # noqa: E402

reg = {
    "study_id": "TA1-C2-BURNIN-72H",
    "purpose": "ENGINEERING BURN-IN ONLY (Gate C). Never registered cost-study data; never pooled into TA1-COST-14D.2.",
    "status": "PROPOSED — NOT APPROVED. Collector refuses to run until approved=true, start_utc set, and arm token supplied.",
    "release": "2.0.0-rc3",
    "watchdog_profile": "burnin",
    "watchdog_profile_note": "Engineering acceptance only; registered study coverage gates are not evaluated during the burn-in (Echo finding B1).",
    "approved": False,
    "approval": {"gate": "C", "decision_ref": None, "approved_by": None, "approved_utc": None},
    "mode": "live",
    "start_utc": None,
    "planned_slots": 72,
    "slot_seconds": 3600,
    "latest_start_margin_s": 45,
    "max_attempts_per_slot": 2,
    "freshness_limit_s": 60,
    "request_timeout_s": 20,
    "watchdog_grace_s": 900,
    "sizes_aud": [500, 1000, 2500],
    "gates": {"overall": 0.90, "per_utc_hour": 0.80, "depth": 0.95},
    "acceptance": {
        "injected_miss_slot": 36,
        "injected_miss_method": "Kootsy disables workflow ta1-c2-collector at slot-36 start minus 5 min (before the :07 trigger) and re-enables it after slot 36 ends (before slot 37 :07). Watchdog stays enabled. Both times recorded.",
        "min_in_slot_captures": 70,
        "min_in_slot_captures_note": "of the 71 non-injected slots, i.e. at most one organic miss (equivalent to the 71/72 tolerance)",
        "zero_persistence_incidents": True,
        "ledger_chain_and_all_artifacts_verified": True,
        "every_slot_has_ledger_row": True,
        "injected_miss_alert_delivered_within_minutes": 75,
        "injected_miss_alert_expected_at_minutes": 65,
        "independent_reproduction_by": "Echo or Astra (not the builder)",
    },
    "sources": [{"key": k, "url": u, "method": "GET", "auth": "none"} for k, u in SOURCES],
    "scheduler": {"primary": "GitHub Actions cron :07 and :37 (best-effort, two triggers, idempotent); collector job timeout 5 min"},
    "dead_man": {
        "basis": "Healthchecks cron-check semantics (open source hc/api/models.py): down at next cron occurrence after the last ping + grace. Slot-aligned, independent of when the previous ping landed (Echo finding B4a).",
        "collector_check": {"name": "ta1-c2-collector", "kind": "cron", "schedule": "5 * * * *", "tz": "UTC", "grace_minutes": 60, "manual_resume": True,
                            "guarantees": "any in-slot success (runner start <= :59:15, job <= 5 min, so ping < next :05) never alerts; a fully missed slot alerts at slot start + 65 min (A2 limit 75)"},
        "watchdog_check": {"name": "ta1-c2-watchdog", "kind": "cron", "schedule": "0 * * * *", "tz": "UTC", "grace_minutes": 120, "manual_resume": True,
                           "guarantees": "one dropped :52 watchdog run never alerts; two consecutive dropped runs alert"},
        "post_window": {"owner": "Kootsy (Claude may assist under C-1b)", "action": "pause BOTH checks (manual_resume = sticky) and disable both workflows",
                        "window_minutes_after_end": [5, 60], "target_minutes_after_end": 15,
                        "reason": "earliest possible post-window dead-man alert is window end + 65 min (Echo finding B4b)",
                        "verification": "Healthchecks API read-only export: both checks status 'paused'; flips show no 'down' after window end except a genuine slot-72 miss (alert at end + 5 min)"},
    },
    "code_manifest": code_manifest(),
    "code_manifest_sha256": code_manifest_sha256(),
    "governance": "R16 PAUSE; R15 NO NEXT; 2025 SEALED; PAPER PAUSED; LIVE PROHIBITED. Public unauthenticated GET only. No accounts, credentials for exchanges, orders, transfers, backfill or signal research.",
}
out = ROOT / "registration" / "TA1-C2-BURNIN-72H.PROPOSED.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")
print(out, reg["code_manifest_sha256"])
