# TA1 collector v2 — Architecture (2.0.0-rc3)

**Status:** Gate B build, fixture-tested only. **Not deployed. No live call has ever been made by this code.**
Governance: R16 PAUSE · R15 NO NEXT · 2025 SEALED · PAPER PAUSED · LIVE PROHIBITED. Gates C, D, E, F not approved.

## 1. Design principle
**Take the LLM out of the clock and out of the write path.** TA1-COST-14D.1 failed because a ChatGPT scheduled task fired irregularly (24/48 slots). Each run also re-implemented the packaging and ledger code, and nothing outside the collector noticed when it went silent. v2 is one frozen, hash-pinned program, run by a deterministic external scheduler and watched by an independent dead-man service.

```
 GitHub Actions cron (:07, :37)          Healthchecks.io (independent SaaS)
        │                                   ▲  start / success / fail pings
        ▼                                   │  (success ONLY after verified readback)
 ┌───────────────────────────── ta1c2 run (one pass, no loops) ─────────────────────────────┐
 │ code-freeze check → slot from clock → lock → verify ledger + ALL stored artifacts (HALT) │
 │ → MISSED rows for ended slots → idempotency → late-start guard → 7 GETs → analyse        │
 │ → ONE atomic commit (artifacts, then ledger; CAS + append-only) → readback → notify      │
 └──────────────────────────────────────────────┬───────────────────────────────────────────┘
                                                ▼
                       git `evidence` branch (protected: no force-push / delete)
                                                ▲
 GitHub Actions cron (:52) → ta1c2 watchdog (separate checkout) → Healthchecks watchdog check
```

## 2. Components (`ta1c2/`, standard library only, Python ≥ 3.10)
| Module | Responsibility |
|---|---|
| `core.py` | Canonical JSON, SHA-256, registration schema, **slot derived only from the clock**, code-manifest freeze |
| `sources.py` | The same 7 public GET endpoints as 14D.1. `LiveTransport` refuses to construct unless the registration is approved, in live mode, and the arm token equals the registration SHA-256. `FixtureTransport` replays recorded bytes; `FaultTransport` injects labelled synthetic faults |
| `analysis.py` | Line-for-line port of frozen 14D.1 calculations (bit-exact on 288/288 figures), plus validity, freshness, duplicate and HTTP-Date clock-skew fields |
| `ledger.py` | Strict canonical JSONL with a hash chain. Rules: contiguous seq, unique (slot, attempt), no attempt after MISSED, attempt start inside its slot, MISSED only after slot end, optional external anchor |
| `storage.py` | `DirBackend` (flock, CAS, create-only artifacts, atomic ledger replace, fsync). `GitBackend` (one atomic commit per run; non-forced push = cross-runner CAS; readback from the remote). `FaultyBackend` for tests |
| `collector.py` | The state machine (§3) |
| `verify.py` | Independent verification: chain, every artifact hash, **full recomputation of derived.json from raw bytes**, orphan report |
| `watchdog.py` | Deterministic alerts from ledger + clock (§5); Gate C acceptance arithmetic |
| `notify.py` | Healthchecks dead-man pings (inert unless armed) |
| `cli.py` | `freeze`, `run`, `verify`, `watchdog` |

## 3. Run outcomes and exit codes
| Outcome | Meaning | Exit |
|---|---|---|
| CAPTURED_VALID | All 7 sources valid, fresh, non-duplicate, in-slot | 0 |
| NOOP_ALREADY_CAPTURED / OUTSIDE_WINDOW | Idempotent second trigger / clock outside window (nothing written) | 0 |
| CAPTURED_PARTIAL / SOURCE_FAILED | Some / none valid. Raw and error bodies preserved; retry allowed once in the same slot | 10 |
| LATE_EXCLUDED | A receipt landed after slot end. Preserved, excluded | 10 |
| NOT_STARTED_TOO_LATE | Under 45 s left in slot; attempt recorded, no fetch | 10 |
| NOOP_ATTEMPTS_EXHAUSTED | Two attempts used | 10 |
| PERSISTENCE_FAILED / CONFLICT_ABORTED / PERSISTENCE_READBACK_MISMATCH | Write error / lost race / stored bytes differ | 20 |
| HALTED_INTEGRITY | Ledger or any stored artifact fails verification. **Writes nothing until a human resolves it** | 30 |
| REFUSED | Code-freeze mismatch, unapproved/unscheduled registration, wrong transport for mode, bad run id | 40 |

A non-zero exit fails the GitHub job, which emails the repository owner. That is a secondary channel; the primary channel is Healthchecks.

## 4. Ledger row (canonical JSON; `row_sha256` = SHA-256 of the row without it)
`seq, prev_row_sha256, row_sha256, kind (ATTEMPT|MISSED), study_id, registration_sha256, code_manifest_sha256, collector_version, slot, slot_id (STUDY:NNNN), run_id, written_utc` plus, for ATTEMPT: `attempt, trigger, run_start_utc, outcome, artifact_dir, manifest_sha256, sources{key:{http_status, sha256, valid, reason, duplicate_of_prior, server_age_s, http_date_skew_s}}, invalid_sources`.
Every slot ends with a row: an attempt, or a MISSED row written by the next run. There is never data in a MISSED row, and there is no API to target a past slot.

## 5. Watchdog alerts
**RC2:** the registration's `watchdog_profile` selects the rule set. `study` (default) applies the list below, unchanged from rc1. `burnin` applies Gate C engineering acceptance only. The declared injected slot and up to the permitted organic misses are INFO; failing alerts are BURNIN_ACCEPTANCE_UNREACHABLE, BLOCKED_TWO_CONSECUTIVE and SOURCE_TWO_CONSECUTIVE (the injected slot is transparent), INJECTED_MISS_NOT_OBSERVED, INJECTED_MISS_ROW_ABSENT, CLOCK_SKEW and LEDGER_INVALID. Humans are paged for organic misses by the collector dead-man (silence) and `/fail` pings, not by a sticky watchdog failure.

MISSING_SLOT (an ended slot plus 15 min grace has no row) · NO_SUCCESS_SLOT · BLOCKED_TWO_CONSECUTIVE · SOURCE_TWO_CONSECUTIVE · GATE_UNREACHABLE_OVERALL · GATE_UNREACHABLE_HOUR · CLOCK_SKEW (> 120 s vs server `Date`) · LEDGER_INVALID.
The in-progress slot and slots still inside grace are never alerted (a run that started at :59 may commit after the hour).

## 6. 14D.1 failure → v2 control (each proven by a named test)
| 14D.1 failure | v2 control | Test |
|---|---|---|
| ChatGPT task fired about 50% of slots, irregularly | External cron, two triggers per hour, idempotent | `test_second_trigger_same_slot_is_noop`, concurrency, git tests |
| Disabled task went unnoticed for 42+ h | Healthchecks dead-man (needs no collector cooperation) plus semantic watchdog | `test_counterfactual` (alert on day 1 at 15:15 Brisbane) |
| Misses were not counted as failures | MISSED rows; consecutive-miss escalation counts misses | `test_missed_slot_gets_row…`, `test_two_consecutive…` |
| 9 misses had no ledger rows | Every ended slot gets a row | `test_counterfactual` (48/48 slots accounted) |
| Unfrozen, drifting wrapper | One program; code-manifest SHA pinned in registration; refuses on drift | `test_code_freeze_mismatch_refuses` |
| Slot 51 ran but persisted nothing | Readback before success ping; failure ping; HALT on later mismatch | `test_lost_write…`, `test_bitrot…`, `test_commit_oserror…` |
| Blank lines and format drift in ledger | Strict canonical parse | `test_blank_line_ledger_halts`, `test_non_canonical_rejected` |
| Alert delivery never evidenced | Gate C criterion: injected miss must reach the phone/email within 75 min | Gate C only (cannot be fixture-proven) |

## 7. Echo scaffold defects and how v2 closes them
| # | Echo scaffold behaviour (probed 13 Oct) | v2 |
|---|---|---|
| D1 | Caller chooses the slot; slot 1 accepted 30 days late (backfill) | Slot from clock only; validator rejects out-of-window attempts |
| D2 | 9 Oct bytes accepted as CAPTURED on 13 Oct (no freshness parse) | Coinbase server-age ≤ 60 s parsed from the bytes |
| D3 | Real OS write error → unhandled crash, no row | Caught; PERSISTENCE_FAILED; failure ping; ledger untouched |
| D4 | Orphan artifact makes every retry crash (FileExistsError) | Unique attempt directories; create-only with identical-bytes tolerance; orphans reported |
| D5 | Tail truncation undetected | External anchor (Healthchecks success-ping log; protected git history) |
| D6 | In-progress slot alerted as missing; `grace_seconds` unused | Grace applied to liveness and gates |
| D7 | Rows do not bind registration or code | `registration_sha256`, `code_manifest_sha256` in every row |
| D8 | Concurrent writers corrupted the ledger in **25/30** trials | Lock + CAS + append-only: 0/20 trials × 6 processes; git push race aborts cleanly |
| D9 | "Stale" and "HTTP 500" tests assert a label, not parsed bytes | Faults go through the real analysis path |

## 8. Scheduler choice and limits (facts, checked 13 Oct 2026)
- **GitHub Actions cron is best-effort.** Runs can be delayed or silently dropped under load, most often near :00. Hence :07 and :37, a 45 s late-start guard and an independent dead-man. In **public** repositories, scheduled workflows are disabled after 60 days without repository activity (irrelevant for 72 h or 7 days).
- **Cost:** Actions on standard runners are free for public repositories. Private repositories get 2,000 included minutes a month on GitHub Free. Burn-in needs about 3 × 72 = 216 job-minutes (two collector triggers plus one watchdog per hour, at about 1 min each).
- **Healthchecks.io Hobbyist:** $0, 20 checks, 100 log entries per check (covers about 50 h at two pings an hour). The external anchor for a 72 h window therefore also relies on protected git history.
- **Fallback if burn-in shows GitHub drops above 1/72:** Google Cloud Scheduler → Cloud Run job → GCS with `ifGenerationMatch` preconditions as the CAS. A backend stub is NOT implemented; the storage interface is ready. Cost has not been verified; no paid service has been initiated.

## 9. Operational requirements (Gate C pre-flight, all Kootsy-owned accounts)
1. GitHub repository (public recommended: the data is public market data, minutes are free and auditing is easy). A `main` branch holds code and the registration. An `evidence` branch is protected by a ruleset: block force-pushes and deletion, and do not allow bypass.
2. Two Healthchecks **cron** checks (RC3), both with "ignore pings while paused": `ta1-c2-collector` (`5 * * * *` UTC, grace 60 min) and `ta1-c2-watchdog` (`0 * * * *` UTC, grace 120 min), each routed to email plus a phone channel. Deadline = next cron occurrence after the last ping + grace, so it is slot-aligned. Pause both between window end + 5 and + 60 min.
3. Repository secrets: `TA1_C2_ARM` (SHA-256 of the approved registration), `TA1_C2_PING_URL`, `TA1_C2_WD_PING_URL`.
4. Copy `deploy/github-actions/*.yml` to `.github/workflows/` **only** at Gate C start.
