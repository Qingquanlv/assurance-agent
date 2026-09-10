# Stage-2 User trace delivery record

Three delivery classes are recorded separately. A class that was not run is
**未验收** and is not replaced by the other two. A test filename containing
`full` is not an acceptance of live Agent full. OCI isolation is a fourth
optional safety experiment; its absence does not block delivery and is not
described as verified isolation.

Item: `opencode-user-api-db-trace` / `api_db_trace.v1`. Same User
requirement as Stage-1 `opencode-user-api-db` / `api_db.v1`. Shared DB
observer, comparator, and business assertion IDs.

## Class 1 — Deterministic unit and installed product graph

**Status: installed `refactor` reaches report / achieved / export.**
Deterministic unit/manifest/processor tests passed in this worktree.
Installed INCOMPLETE/FAILED faults reach their product boundaries.
Installed `refactor` verification is PASSED and the graph now continues
through `quality.report` to achieved; export is allowed. Snapshots were
not relaxed. Ordinary CI remains uv-workspace only. Class 3 is still
未验收.

Matrix stop stages and expected verdicts (bound before full start):

| Fault | Stop / prefix | Expected verdict |
| --- | --- | --- |
| no-bridge | generation admission | NOT_READY (not runtime A03) |
| no-action / skip-oracle / unknown-http | quality.report | INCOMPLETE |
| drop-business-span / drop-write-span / broken-context / stale-trace / drain-timeout | quality.report | INCOMPLETE |
| wrong-value / rollback / rollback-success / missing-write | quality.report | FAILED |
| early-completed | quality.inspect then needs_human (FAILED does not open report) | FAILED |
| business violation plus missing telemetry | quality.report | FAILED + missing telemetry detail; required set not reduced |
| refactor | quality.report | PASSED, achieved/export |

`refactor` freezes real new source (helper extract, equivalent ORM, extra
span) before intake/explore. Runtime uses the new files and lock.

## Class 2 — Real OTel compatibility experiment

**Status:** previously executed by Task 11 via
`uv run python benchmark/assurance-product/user_oracle_harness.py verify-otel-compatibility`.
This Task 13 commit does not re-declare that experiment as a substitute for
Class 3. Re-run the named command when certifying a new Collector or SUT lock.

## Class 3 — Real Agent full matrix

**Status: 未验收 / BLOCKED.** `http://127.0.0.1:4096` was down on 2026-09-10
(connection refused). The five live commands were **not run** and outcomes
were **not invented**.

```bash
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db-trace --adapter opencode
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db-trace --adapter opencode --fault drop-business-span
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db-trace --adapter opencode --fault drop-write-span
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db-trace --adapter opencode --fault early-completed
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db-trace --adapter opencode --fault refactor
```

When the OpenCode agent server is available, run each command from an empty
change, record outcome, wall time, environment start cost, integration
changes, false positives, and limits. Do not assume a performance gain.
Happy path must reach achieved and export. Archive remains optional.

## Class 4 — Optional OCI isolation (does not block delivery)

**Status: 未验收 / not run.** `scripts/build_verification_runner.py` and real
OCI qualification are explicit optional commands. They are not part of
ordinary CI, business verdict, or achieved/export. Missing OCI evidence is
not verified isolation.
