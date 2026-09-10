# Task 13 Report — 第二阶段 full 故障矩阵与最终交付

## Status

**DONE_WITH_CONCERNS**

T1–T12 remain as committed. This task added `opencode-user-api-db-trace` /
`api_db_trace.v1`, the Trace fault closed set, deterministic matrix wiring,
CI/smoke comments, README live commands, and a three-class delivery record.
Live Agent full (Class 3) was not run: `http://127.0.0.1:4096` was down
(connection refused) on 2026-09-10 and was rechecked before delivery.
Installed `refactor` now gets verification **PASSED** but does not yet reach
`quality.report` / achieved because inspect coverage stays `repair_required`.

## What was implemented

### Manifest and profile

- New benchmark item `opencode-user-api-db-trace` selects only
  `api_db_trace.v1`.
- Same User requirement, DB observer/comparator, and business assertion IDs
  as Stage-1 `opencode-user-api-db` / `api_db.v1`. No second expected set.

### Fault closed set (bound before full start)

Added: `drop-business-span`, `drop-write-span`, `broken-context`,
`stale-trace`, `drain-timeout`, `early-completed`, `refactor`.

Expected terminals:

| Fault | Verdict | Product boundary |
| --- | --- | --- |
| Telemetry-loss family | INCOMPLETE | `quality.report` |
| `early-completed` (rollback after completed) | FAILED | `quality.inspect` / `needs_human` (FAILED does not open report) |
| `refactor` | PASSED | `quality.report` then achieved/export |
| `no-bridge` | generation admission | not runtime A03 |

`refactor` freezes real new source before intake: helper extract
(`app/controllers/user_persist.py` / `persist_created_user`), equivalent ORM
(`User.create` + `filter`), and extra `persist_user` span. Runtime lock and
reviewed sources follow the new files. Traces are not patched onto old
source digests.

### Host / wheel binding (T13 wiring already present)

- Wheel `secret_handles` stay the adapter binding handle only.
- Host writes CollectorQualification under the output collector-host tree
  (not OCI qualification).
- Attempt start overlays a live `CollectorReadinessReceiptV1` on
  `sut.collector`.

### T12 leftover that blocked the happy-path traces

Assessment `load_otlp_records` / `_bound_spans` require `aa.execution_id` on
every span. The SUT request hook stamped only the FastAPI SERVER span.
Write and `user.create.completed` spans were present in raw Collector OTLP
(T11 already saw them) but were filtered out, so installed `refactor`
looked INCOMPLETE.

Fix (TDD):

- Red: `test_processor_stamps_request_execution_id_on_child_spans`
- Green: `bootstrap.bind_request_execution_id` + processor inherit + request
  hook bind. `broken-context` clears the identity. `stale-trace` still
  overwrites. Drop faults still omit the dropped spans.
- Repinned `fixtures/user-oracle/runtime-lock.json` (`bootstrap.py` digest
  and `runtime_digest`). Harness pin unchanged
  (`52d8a98c19472b8ad49692f1c6cd1c023da32e499e2f2168a52e5ca7b32f1580`).

After this fix, installed `refactor` verification is **PASSED** with empty
open obligations and HTTP 200.

## Three-class delivery

Recorded in `benchmark/assurance-product/stage2-delivery.md`. Missing class
= 未验收. Filenames containing `full` are not live Agent acceptance.

### Class 1 — Deterministic unit / installed product graph

**Partial.**

- Unit/manifest/processor: 78 passed
  (`test_user_oracle_full_workflow`, phase5/phase6 manifest,
  `validate_live_run`, child-span identity).
- Earlier this session: installed `drop-business-span`, `drop-write-span`,
  and `early-completed` reached their expected product boundaries.
- Installed `refactor`: verification PASSED; graph then looped
  case-design → inspect (`coverage_state=repair_required`,
  `coverage_rounds=2`) and stopped without `quality.report` / achieved.
  Snapshots and source checks were not relaxed.

T10 leftover (blocks achieved only): deterministic installed happy path has
not been certified through coverage-satisfied → report → achieved. Stage-1
`test_installed_full_assembles_empty_change_through_codegen` runs the full
graph but only asserts assembly through codegen.

### Class 2 — Real OTel compatibility experiment

**Previously run by Task 11**
(`user_oracle_harness.py verify-otel-compatibility`). Not re-run here. Not
a substitute for Class 3.

### Class 3 — Real Agent full matrix

**未验收 / BLOCKED.** `http://127.0.0.1:4096` connection refused
(rechecked 2026-09-10). These five commands were **not executed** and
outcomes were **not invented**:

```bash
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db-trace --adapter opencode
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db-trace --adapter opencode --fault drop-business-span
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db-trace --adapter opencode --fault drop-write-span
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db-trace --adapter opencode --fault early-completed
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db-trace --adapter opencode --fault refactor
```

### Class 4 — Optional OCI isolation

**未验收 / not run.** Does not block delivery. Not described as verified
isolation. Ordinary CI has no Docker/Colima/qualification steps.

## Named CI / smoke in this environment

| Command | Result |
| --- | --- |
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | failed on two **pre-existing** HEAD-dirty files not owned by T13: `packages/products/assurance-product/assurance_product/status.py`, `tests/product/test_verified_attempt_recovery.py` |
| `uv run pyright` | 80 errors, pre-existing T11/T12 (telemetry typing, verified_execution tuple size, `_InstalledFullRun` dynamic attrs) |
| `uv run lint-imports` | 13 kept, 0 broken |
| T13 unit/manifest set | 78 passed |
| `uv run pytest` (full) | not completed in this session |
| three smoke scripts | run after this commit (`git archive HEAD`); see follow-up if a second commit is required |

No Docker/OCI added to ordinary CI.

## Self-review

- Profile split is `api_db.v1` vs `api_db_trace.v1` only.
- FAILED still routes `needs_human` and does not open report.
- Host secrets are not placed on the wheel handle union.
- Child-span identity is required by the existing assessment contract; the
  SUT processor now matches the synthetic OTLP fixtures.
- Remaining achieved gap is coverage sufficiency after a PASSED
  verification, not missing traces. Do not treat installed `*full*`
  filenames as Class 3.
- Did not amend earlier commits. Did not invent live outcomes.

## Concerns

1. Installed `refactor` verification PASSED but did not reach achieved /
   export (`coverage.repair_required`).
2. Class 3 live Agent matrix blocked (4096 down).
3. Named `ruff format --check` and `pyright` fail on pre-existing files.
4. Full `pytest` and wheel smoke were not finished before the first
   delivery commit; smoke must use archived HEAD.
