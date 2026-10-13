# TA1 collector v2 (2.0.0-rc3): independent reproduction guide

**Gate B deliverable. Fixture-tested only. Nothing here runs on a schedule or touches the network.**
Built by Claude; to be independently validated by Echo or Astra before Kootsy considers Gate C.
Design and rationale: `ARCHITECTURE.md`. The proposed burn-in registration (NOT APPROVED) is in `registration/`.

## Requirements
Python 3.10–3.13 (standard library only; no pip installs). `git` is needed for the 3 GitBackend tests; without it they are reported as *skipped*, which a reviewer must note.

## Reproduce (about 15 s)
```bash
cd ta1-collector-v2
python3 -I tools/check_manifest.py                  # 1. every file matches SHA256SUMS
python3 -m ta1c2.cli freeze                         # 2. code_manifest_sha256 must equal the value in
                                                    #    registration/TA1-C2-BURNIN-72H.PROPOSED.json
cd tests && TA1C2_EVIDENCE_OUT=../evidence_out python3 -m unittest discover -v   # 3. expect: Ran 67 tests … OK
```
Step 3 also writes `evidence_out/replay_14D1_watchdog_timeline.json`. Its `ledger_sha256` must equal `2367ea14ee5cdd460c6c884234645353827f9258e49085039b4386c7f9f3161a` (deterministic).

## What the tests prove (all offline; IP sockets and DNS are blocked inside the test process)
| Area | Tests |
|---|---|
| Fixture provenance: 24 captures × 16 files hash-match the original 14D.1 archive | `test_01` |
| Frozen 14D.1 calculations reproduced bit-exactly (288/288) | `test_01` |
| Idempotency, retry limit, no backfill, MISSED rows, late-start and late-receipt rules | `test_02` |
| Stale (real bytes replayed late), duplicate, HTTP 500, timeout, truncated, FX loss | `test_02` |
| Write error, crash between artifact and ledger, lost write, bit-rot, ledger tamper, blank line, non-append | `test_02` |
| Code-freeze drift, disarmed live transport, unapproved/unscheduled registration, run-id injection | `test_02` |
| HTTP-Date clock-skew audit | `test_02` |
| **RC3:** 8 independent OS processes × 10 trials (subprocess, wall-clock release, hard timeouts): exactly one capture, ledger always valid | `test_03` |
| Git: atomic commit, push, remote readback, second runner no-op, push-race loser aborts cleanly | `test_03` |
| Ledger strictness, truncation via anchor, watchdog grace, consecutive and gate alerts | `test_04` |
| Gate C acceptance arithmetic (72/72 pass, 71/72 pass, 70/72 fail, persistence incident fails) | `test_04` |
| **Counterfactual replay of the real 14D.1 history**: v2 would have alerted at 15:15 Brisbane on 9 Oct | `test_04` |
| CLI end-to-end; shipped PROPOSED registration pins this code and stays inert | `test_05` |
| **RC2:** burn-in watchdog profile: injected and permitted organic misses do not fail; a second organic miss fails at the right time; watchdog/acceptance agreement (300 scenarios); study profile unchanged | `test_06` |

Fixture regeneration (optional, read-only on the original):
`python3 -I tools/extract_fixtures.py <TA1_COST_14D_..._Slot90_2026-10-13.zip> fixtures`

## What these tests CANNOT prove (Gate C items)
Real GitHub cron behaviour, push authentication and branch protection, runner clock trust, Healthchecks delivery to a phone or email, and sustained 72 h reliability.

See `CHANGELOG.md` for rc3 changes (dead-man cron check, 5-min job timeout, subprocess race test) and rc2 changes (Echo finding B1, dead-man timing, `workflow_dispatch` removed).
