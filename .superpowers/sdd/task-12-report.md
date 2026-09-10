# Task 12 Report — Trace 关联、完成与缺失判定

## Status: DONE_WITH_CONCERNS

**Plan task:** Trace 关联、完成与缺失判定  
**Worktree:** `/Users/lvqingquan/agent/assurance-agent/.worktrees/user-full-workflow-db-oracle-trace`  
**Branch:** `codex/user-full-workflow-db-oracle-trace`  
**Commit:** `86c7a781` `feat(quality): require correlated and complete User traces`  
**Did not start T13.** Did not fake live Agent full runs. Did not invent a second evidence store.

## What Was Implemented

Sealed T11 Collector output into the existing journal / `EvidenceArtifactRef` / kernel receipt path, then judged correlated and complete User traces on both the producer and authenticated assessment replay.

### Shared contracts (`assurance_execution.contracts.telemetry`)

- `parse_otlp_records(raw)` flattens OTLP JSON/JSONL, de-dupes `(trace_id, span_id)`, rejects conflicting duplicates and truncated export.
- `check_trace_requirements(plan, manifest, spans, completion)` is the single matcher used by producer and quality replay.
- Matching rules:
  - HTTP: FastAPI SERVER `/api/v1/user/create`, descendant of driver CLIENT `assurance.execution.http-driver`.
  - Write: Tortoise CLIENT, `aa.db.table == user`, `aa.db.operation == INSERT`, status UNSET or OK, descendant of SERVER (helpers allowed; no fixed direct parent).
  - Completed: name `user.create.completed`, `user.username == plan.inputs["username"]`, descendant of SERVER.
  - Drain: completion `state == complete` plus drain/archive complete.
  - Same `aa.execution_id` and `service.instance.id == manifest.sut.instance_id`.
  - Oracle exclusion is exact: `aa.role == oracle` OR instrumentation `assurance.execution.oracle` OR `service.name == "oracle"` (not a substring; `user-oracle-sut` is not excluded).
- `TelemetryCompletionV1` / `StageCompletionV1` / `TelemetryArchiveV1` keep driver flush, SUT flush, Collector drain, and archive state/reason in one sealed document.
- `apply_sut_request_identity` copies only `aa-execution-id` → `aa.execution_id`.
- Names: `telemetry.otlp.jsonl`, `telemetry-completion.json`.

### Producer (`assurance_execution.operations.telemetry` + `verified_execution`)

- `load_otlp_records(path, execution_id)` bounded 12MB regular-file read, then parse + filter.
- Parent HTTP CLIENT span + W3C inject + `aa-execution-id` on `api_db_trace.v1` posts.
- After host run / before terminal outcome: flush driver, flush SUT (`POST /internal/otel/flush`, no provider shutdown), drain owned Collector, copy Collector `otel/traces.jsonl` to evidence-root `telemetry.otlp.jsonl`, then write `telemetry-completion.json`. Complete is written only after every stage succeeds.
- `_journal_refs` includes both sealed files. `collector_completion` is `not_required` for `api_db.v1`.
- Oracle SELECT spans are marked `aa.role=oracle`, scope `assurance.execution.oracle`, service `oracle`.

### Assessment replay (`assurance_quality.operations.assessment`)

- Authenticates OTLP and completion by digest into the existing raw-evidence closed set.
- Replays via **contracts only** (`parse_otlp_records` + `check_trace_requirements`). No `assurance_execution.operations` import. No second matcher.
- Keeps the four action/process journal authentications.
- Rejects extra `.jsonl` except `telemetry.otlp.jsonl`; allows `telemetry-completion.json`.
- `api_db.v1` must not carry sealed telemetry.
- Conflict → `AssessmentInputError`; truncate → missing `otlp_truncated` observations.
- Tampered sealed OTLP cannot reuse an old PASSED publish.

### Fixture / SUT hook

- FastAPI `server_request_hook` copies only `aa-execution-id`.
- Added `POST /internal/otel/flush` (force_flush, no shutdown).
- Recomputed `runtime-lock.json` `files.bootstrap.py` and `runtime_digest`. `source_digest` unchanged (`sut-source/` only).

## Tests + Results

Named command:

```text
uv run pytest packages/capabilities/assurance-execution/tests/test_telemetry.py \
  packages/capabilities/assurance-quality/tests/test_trace_verification.py \
  packages/capabilities/assurance-quality/tests/test_verification.py \
  tests/product/test_execution_quality_flow.py \
  tests/product/test_verified_delivery.py -q
uv run lint-imports
uv run python benchmark/assurance-product/user_oracle_harness.py verify-otel-compatibility
```

| Gate | Result |
| --- | --- |
| Named pytest | **110 passed** |
| `uv run lint-imports` | **13 kept, 0 broken** |
| T11 `verify-otel-compatibility` | **14 passed**; runtime_digest `sha256:e0478bbe3fa8d6cbceb2a38ebdd812b4f2efd0551b07cc6509793fcc2e2b579c`; source_digest unchanged |

Coverage in the named tests:

- Real OTLP-shaped fixtures: missing HTTP, missing write, stale `execution_id`, wrong instance, broken parent chain, oracle SELECT only, role/audit table write, helper parent chain, equivalent INSERT + UNSET.
- Seal writes `telemetry.otlp.jsonl` + `telemetry-completion.json`; drain timeout stays incomplete.
- Persist-without-write → INCOMPLETE; rollback + completed → FAILED; business error + missing telemetry → FAILED + missing evidence.
- Authenticated materializer admits sealed traces; tampered OTLP / rewritten digest fails closed.
- Installed `materialize-assessment-inputs` path admits a legal sealed User trace and rejects missing/rewritten traces.
- Export rejects tampered sealed traces.

`test_trace_verification.py` is synthetic parse/match only and is not counted as T13 live admission.

## TDD Evidence

1. **Red:** first named-test run failed with `ModuleNotFoundError: assurance_execution.contracts.telemetry` before the contract module existed.
2. **Green:** after matcher + seal + assessment wiring, named pytest reached 110 passed. A first installed-assessment pass failed because `assessment_composition` was still frozen on `api_db.v1`; `trace_assessment_composition` (`api_db_trace.v1`) fixed that without changing `api_db.v1`.
3. **Oracle false-negative:** an early matcher treated `service.name` containing `"oracle"` as oracle (`user-oracle-sut`). Fixed to exact `== "oracle"`.
4. Implementation was not written before the failing import; no live Agent full run was invented to go green.

## Files Changed

**New**

- `packages/capabilities/assurance-execution/assurance_execution/contracts/telemetry.py`
- `packages/capabilities/assurance-execution/assurance_execution/operations/telemetry.py`
- `packages/capabilities/assurance-execution/assurance_execution/resources/schemas/telemetry-completion.v1.schema.json`
- `packages/capabilities/assurance-execution/tests/test_telemetry.py`
- `packages/capabilities/assurance-quality/tests/test_trace_verification.py`

**Modified**

- `packages/capabilities/assurance-execution/assurance_execution/contracts/__init__.py`
- `packages/capabilities/assurance-execution/assurance_execution/operations/verified_execution.py`
- `packages/capabilities/assurance-execution/assurance_execution/operations/sqlite_oracle.py`
- `packages/capabilities/assurance-execution/assurance_execution/plugin.py`
- `packages/capabilities/assurance-execution/assurance_execution/plugin-declaration.json`
- `packages/capabilities/assurance-quality/assurance_quality/operations/assessment.py`
- `packages/capabilities/assurance-quality/tests/test_verification.py`
- `tests/verified_assessment_fixture.py`
- `tests/product/test_execution_quality_flow.py`
- `tests/product/test_verified_delivery.py`
- `benchmark/assurance-product/fixtures/user-oracle/bootstrap.py`
- `benchmark/assurance-product/fixtures/user-oracle/runtime-lock.json`

Quality `operations/verification.py` was not rewritten: the evaluator already consumes replayed `ObservationV1` values. The new quality test asserts it does not re-implement the matcher. `contracts/{verification,workflow,attempts}.py` already carried `collector_completion` from T11.

## Self-Review

- Quality imports execution **contracts** only; `lint-imports` kept the layer rule.
- `api_db.v1` stays `not_required` and is rejected if sealed telemetry appears.
- Journal kinds (action/process/cleanup) are still authenticated; telemetry is an addition, not a replacement.
- Complete is not written before export; drain timeout seals `incomplete`.
- Duplicate span identity de-dupes; conflicting content is reject.
- Oracle observer cannot satisfy `trace.user_write`.
- Installed assessment, not only synthetic fixtures, is the admission door.
- No second evidence store. No T13 live run. No amend of prior commits.

## Concerns

1. ~~**Host driver span is not exported to the locked Collector.**~~ Fixed in the follow-up commit below. `api_db_trace.v1` now attaches an OTLP HTTP exporter to the Attempt Collector endpoint recorded in `collector-process.json` / `collector_export.otlp_endpoint`. A real Collector test seals that CLIENT span into `telemetry.otlp.jsonl`. `api_db.v1` still does not start or require Collector/OTel.
2. If Collector `otel/traces.jsonl` is missing after flush/drain, `_complete_trace_evidence` returns without writing an incomplete completion document. Assessment then fail-closes on missing sealed telemetry. That is conservative, but T13 should confirm the live drain path always leaves a file or an explicit incomplete receipt.
3. `flush_driver_provider` shuts down the process-global tracer provider. Safe for a single-shot verified attempt; do not reuse that process for a later traced action without re-installing the provider.

These are follow-through items for T13 live, not gaps in the named T12 gates.

---

## Pre-review correctness fix — host driver CLIENT span export

**Did not start T13.** Did not fake live Agent full runs. Did not amend `86c7a781`.

### Change

- `_ensure_driver_provider(otlp_endpoint=)` attaches `OTLPSpanExporter` + `SimpleSpanProcessor` to `{endpoint}/v1/traces` (same shape as the SUT).
- `collector_otlp_endpoint(run_root)` reads the T11 Attempt Collector endpoint from `otel/collector-process.json`.
- `execute_frozen_action` / `_post` pass that endpoint on `api_db_trace.v1` only.
- Covering test starts a real locked Collector, exports a driver CLIENT span, flushes, drains, seals, and asserts `assurance.execution.http-driver` is in `telemetry.otlp.jsonl`.

### TDD

1. **Red:** `test_driver_client_span_is_sealed_from_attempt_collector` first failed with `ImportError: cannot import name 'collector_otlp_endpoint'`.
2. **Red (behavior):** after adding the lookup API without an exporter, the same test failed with `ValueError: OTel flush did not produce an OTLP file` (17.57s) — the driver span never reached the Collector.
3. **Green:** after attaching the OTLP exporter and threading the Attempt endpoint, the covering test passed (7.56s). Named suite then reached 111 passed.

### Commands + output

```text
$ uv run pytest packages/capabilities/assurance-execution/tests/test_telemetry.py::test_driver_client_span_is_sealed_from_attempt_collector -q
# RED (API missing)
F
ImportError: cannot import name 'collector_otlp_endpoint' from 'assurance_execution.operations.telemetry'
1 failed in 0.99s

# RED (exporter missing)
F
ValueError: OTel flush did not produce an OTLP file
1 failed in 17.57s

# GREEN
.
1 passed in 7.56s
```

```text
$ uv run pytest packages/capabilities/assurance-execution/tests/test_telemetry.py \
  packages/capabilities/assurance-quality/tests/test_trace_verification.py \
  packages/capabilities/assurance-quality/tests/test_verification.py \
  tests/product/test_execution_quality_flow.py \
  tests/product/test_verified_delivery.py -q
uv run lint-imports
```

```text
........................................................................ [ 64%]
.......................................                                  [100%]
111 passed in 61.84s (0:01:01)

Contracts: 13 kept, 0 broken.
```

`uv run ruff check` on the three Python files: all checks passed. `ruff format` reformatted `telemetry.py`.

---

## Review-fix commit — Critical and Important findings

**Did not start T13.** Did not fake live Agent full runs. Did not amend `3a90048e` or `86c7a781`.

### Fixes

1. **Critical 1 — live driver CLIENT spans satisfy the matcher.** Driver provider now stamps `service.instance.id` from the SUT instance. `_bound_spans` also keeps `assurance.execution.http-driver` spans that match `execution_id`, so a live sealed CLIENT span is not dropped. Covering test exports a real driver span through the Collector, then runs `check_trace_requirements` against that span plus a SERVER child (presence-only is no longer the gate).
2. **Critical 2 — oracle does not steal the process-global provider.** `_oracle_select` uses a local `TracerProvider(service.name=oracle)` and never calls `set_tracer_provider`. Driver remains `assurance.execution.http-driver` / `assurance-execution-driver`. Covered through real `execute_frozen_action` order (oracle SELECT before HTTP).
3. **Important 3 — completion order.** `execute_frozen_action` owns the CLIENT span: HTTP terminal → post-action oracle → `span.end()` → later flush/drain/seal.
4. **Important 4 — missing Collector file seals INCOMPLETE.** `_complete_trace_evidence` writes `telemetry-completion.json` with archive/export reason when `otel/traces.jsonl` is absent.
5. **Important 5 — truncated OTLP is INCOMPLETE on both sides.** Shared `truncated_trace_observations` emits `otlp_truncated` for producer `collect_facts` and assessment replay.
6. **Important 6 — named combination tests** now go through matcher + authenticated assessment (not evaluator fiction):
   - persist-without-write → INCOMPLETE (kept; also installed assessment path)
   - rollback + early `user.create.completed` → FAILED
   - business error and missing write span → FAILED + missing evidence
   - helper INSERT rebound via matcher+assessment → PASSED. **This is not a T13 live new-artifact refactor.**
7. **Important 7 — stage receipt_refs.** Driver flush, SUT flush, and Collector drain write controlled receipt files beside the completion and retain `receipt_ref` in `telemetry-completion.json`. Archive keeps path/digest/size.
8. **Important 8 — narrowed `_oracle_select` catch.** Setup uses `ImportError` / `AttributeError` only. Query errors propagate. No bare `except Exception`. Global provider is not mutated.

### TDD

1. **Red:** new covering tests failed for the named bugs (`service.name=oracle`; `span_end` before post-action oracle; missing completion file; `receipt_ref is None`; `runtime_fact_unavailable` vs `otlp_truncated`; `except Exception` present; live `sut_instance_id` TypeError then matcher).
2. **Green:** after the producer/matcher/oracle fixes, the same tests passed. Named suite 121 passed.

### Commands + output

```text
$ uv run pytest \
    packages/capabilities/assurance-execution/tests/test_telemetry.py::test_execute_frozen_action_keeps_driver_distinct_from_oracle \
    packages/capabilities/assurance-execution/tests/test_telemetry.py::test_client_span_ends_after_post_action_oracle \
    packages/capabilities/assurance-execution/tests/test_telemetry.py::test_missing_collector_file_seals_incomplete_completion \
    packages/capabilities/assurance-execution/tests/test_telemetry.py::test_complete_trace_evidence_retains_stage_receipt_refs \
    packages/capabilities/assurance-execution/tests/test_telemetry.py::test_truncated_otlp_is_incomplete_on_producer \
    packages/capabilities/assurance-execution/tests/test_telemetry.py::test_oracle_select_does_not_steal_global_provider_on_setup_failure \
    packages/capabilities/assurance-quality/tests/test_verification.py::test_rollback_and_early_completed_is_failed_via_assessment \
    packages/capabilities/assurance-quality/tests/test_verification.py::test_business_error_and_missing_telemetry_is_failed_with_missing_evidence \
    packages/capabilities/assurance-quality/tests/test_verification.py::test_helper_insert_rebound_still_passed_via_assessment \
    packages/capabilities/assurance-quality/tests/test_verification.py::test_truncated_otlp_is_incomplete_on_assessment \
    -q --tb=line
# RED
FFFFFF...F
7 failed, 3 passed in 0.92s
# (oracle provider steal; span ended before post-action oracle; no incomplete
#  completion; no receipt_refs; runtime_fact_unavailable; except Exception;
#  assessment observation mismatch on truncated OTLP)
```

```text
$ uv run pytest packages/capabilities/assurance-execution/tests/test_telemetry.py::test_driver_client_span_is_sealed_from_attempt_collector -q --tb=short
# RED
TypeError: start_driver_client_span() got an unexpected keyword argument 'sut_instance_id'
```

```text
$ uv run pytest packages/capabilities/assurance-execution/tests/test_telemetry.py \
  packages/capabilities/assurance-quality/tests/test_trace_verification.py \
  packages/capabilities/assurance-quality/tests/test_verification.py \
  tests/product/test_execution_quality_flow.py \
  tests/product/test_verified_delivery.py -q
uv run lint-imports
```

```text
........................................................................ [ 59%]
.................................................                        [100%]
121 passed in 63.64s (0:01:03)

Contracts: 13 kept, 0 broken.
```

`uv run ruff check` on the touched Python files: all checks passed.

