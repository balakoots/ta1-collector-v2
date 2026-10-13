# Changelog

## 2.0.0-rc3 — 13 Oct 2026 (narrow; supersedes rc2 for Gate C; rc2 archive preserved unchanged)
Response to Echo's final readiness review (findings **B4a**, **B4b**, and the spawn-test reproducibility limit). **No collector logic changed:** the only `ta1c2/` diff is `VERSION`.
- **B4a — dead-man false positive.** The collector check moves from a *simple* 30/35 check to a **cron check `5 * * * *` UTC, grace 60 min**.
  - Healthchecks' cron semantics (`hc/api/models.py`): down at the next cron occurrence after the last ping, plus grace. The deadline is therefore slot-aligned.
  - Any in-slot success, including a backup :37 run delayed up to the :59:15 late-start guard, never alerts.
  - A fully missed slot alerts at **slot start + 65 min**, inside A2's 75-min limit.
- **Collector workflow `timeout-minutes` 8 → 5.** Every ping, including the run before an outage, lands before the next :05 boundary, so the 65-min deadline holds regardless of how late the previous slot ran.
- **Watchdog check:** cron `0 * * * *` UTC, grace 120 min. One dropped watchdog run is tolerated; two alert.
- **B4b — post-window alert.** The earliest possible post-window alert is now deterministic: **window end + 65 min**.
  - The registration's `dead_man.post_window` specifies the owner (Kootsy), the action (pause both checks with sticky `manual_resume`, disable both workflows), the window (end + 5 to end + 60 min; target + 15) and the verification (an API export).
- **Registration:** `release 2.0.0-rc3`; structured `dead_man` block; `injected_miss_alert_expected_at_minutes: 65`.
- **Tests (67):** `test_03` race test rewritten.
  - It uses independent OS subprocesses released by a wall-clock go-time, with no Pool, Manager or Barrier, so a slow or failed worker start cannot deadlock the others.
  - Per-worker 60 s timeouts give a clear failure message. Defaults are 10 trials × 8 processes; scale with `TA1C2_RACE_TRIALS` / `TA1C2_RACE_PROCS`.
  - New `test_rc3_dead_man_config_is_coherent` derives the B4a/B4b arithmetic from the shipped registration and workflow.
- The Healthchecks timeline simulations (an independent port of the Healthchecks deadline logic) live in ops kit **TA1-C2-OPS-3**.

## 2.0.0-rc2 — 13 Oct 2026 (supersedes rc1 for Gate C; rc1 archive preserved unchanged)
Response to Echo's independent Gate C ops review (finding **B1**) and its operational notes.

**Code (`ta1c2/`):**
- `watchdog.py`: new **`watchdog_profile`**.
  - `study` (the default) keeps the rc1 behaviour byte-for-byte in its logic.
  - `burnin` evaluates **Gate C engineering acceptance only**; study coverage gates (overall and per-UTC-hour) are not evaluated.
  - The declared injected slot is expected (`INJECTED_MISS_OBSERVED`, info).
  - Organic misses up to the permitted number are info.
  - Failing alerts: `BURNIN_ACCEPTANCE_UNREACHABLE`, `BLOCKED_TWO_CONSECUTIVE` and `SOURCE_TWO_CONSECUTIVE` (the injected slot is transparent for both), `INJECTED_MISS_NOT_OBSERVED`, `INJECTED_MISS_ROW_ABSENT`, `CLOCK_SKEW`, `LEDGER_INVALID`.
  - `burnin_tally()` is the single counting function shared by the watchdog and `burnin_acceptance()`, so the two cannot disagree.
  - `burnin_acceptance()` additionally requires the injected slot's row to be `MISSED`.
- `core.py`: validates `watchdog_profile`. The `burnin` profile requires a coherent `acceptance` block: an integer minimum, and an injected slot that is null or lies in [1, planned_slots).
- `ledger.py`: adds the `SUCCESS_STATUS` constant (no behaviour change).
- `__init__.py`: `VERSION = "2.0.0-rc2"`.

**Operational files:**
- `deploy/github-actions/*.yml`: **`workflow_dispatch` removed**; no manual runs are possible.
- `registration/TA1-C2-BURNIN-72H.PROPOSED.json` changes:
  - `watchdog_profile: "burnin"` and `release: "2.0.0-rc2"`;
  - a precise injection procedure;
  - **dead-man period 30 min / grace 35 min.** Under rc1's 60/35, the :37 no-op success ping put the injected-slot alert at about 75 min, i.e. on the A2 limit (Echo's note). It now lands at about 45 min.

**Tests:** 66, up from 54.
- New `test_06_burnin_profile.py` (12 tests): time-sliced watchdog evaluation across the whole 72 h.
- Cases covered:
  - injected miss plus one permitted organic miss, which never fails and is accepted;
  - a second organic miss, which fails at exactly slot end + grace;
  - an injection adjacent to an organic miss, which is transparent;
  - the injection not performed, and the injection with no MISSED row, both failing;
  - a 300-scenario property check that the watchdog and the acceptance check agree;
  - the study profile unchanged;
  - registration validation.
- The concurrency test now uses the `spawn` start method, so no `fork()` deprecation warning appears (verified with `-W error::DeprecationWarning`).

## 2.0.0-rc1 — 13 Oct 2026
Initial fixture-tested release. Independent Gate B PASS (Echo, OS-053).
