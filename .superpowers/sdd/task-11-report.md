# Task 11 Report — Actual Tortoise/SQLite OTel and Reproducible Deps

## Status: DONE_WITH_CONCERNS

**Plan task:** 实际 Tortoise/SQLite OTel 接入与可重现依赖  
**Worktree:** `/Users/lvqingquan/agent/assurance-agent/.worktrees/user-full-workflow-db-oracle-trace`  
**Branch:** `codex/user-full-workflow-db-oracle-trace`  
**HEAD before this task:** `87bac948`  
**Commit:** see git log for `feat(benchmark): instrument the actual User SQLite path`

T12/T13 were not started. No live Agent full runs were faked. No placeholder Collector digests or `latest` tags were used.

## What I implemented

Stage-2 instrumentation on the already-shipped User API+DB host. The same closed-set `start_user_attempt` entry now admits `{"api_db.v1", "api_db_trace.v1"}`. Ordinary `api_db.v1` still starts only SUT+SQLite (no Collector key on the start receipt, no `otel/` directory). Trace starts the locked Collector first, then the SUT with a dynamic OTLP HTTP endpoint.

### Real OTel on the User SUT

- `fixtures/user-oracle/bootstrap.py`
  - `install_otel()` builds the provider from `AA_SUT_OTEL_*` only (endpoint, sampler, protocol). No-op when `AA_SUT_OTEL_ENDPOINT` is unset so `qualify_runtime` / `api_db.v1` stay Collector-free.
  - Official call shape:
    - `TortoiseORMInstrumentor().instrument(tracer_provider=provider, capture_parameters=False)`
    - `FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)`
  - Span processor copies `BoundedAttributes` to a dict, normalizes old (`1.11.0`) / new (`1.24.0`) db semconv onto `aa.db.table` / `aa.db.operation` / `aa.db.semconv`, then deletes SQL text, parameters, password, and auth keys. Mixed/unknown modes fail preflight.
  - `flush_and_shutdown()` writes an optional flush receipt.
- `sut-source/app/__init__.py`: install OTel **before** `create_app()`; instrument after the app exists; flush on lifespan exit when an endpoint is set.
- `sut-source/app/api/v1/users/users.py`: after `create_user` + `update_roles` return and before the Success envelope, emit `user.create.completed` with the real `new_user.username`. Transaction semantics are unchanged; the span is not named committed.

### Locked Collector + hashed SUT deps

- Pinned **otelcol-contrib v0.129.1** (binary self-reports 0.129.0; the **tarball** digest is locked). Cached under `~/.cache/assurance-agent/otelcol-contrib/v0.129.1/`, not in the fixture tree.
  - darwin_arm64: `sha256:bd5c2acb501d525da72a55136894a619e3539026640368ff8249d5829cd51f21`
  - linux_amd64: `sha256:6fe3531656660a8f145872cc1502371cb20e6d7da70fdc9a64bf6ebdaba20403`
- `collector.yaml` is the locked template. Per-attempt instantiate binds loopback ports (`SO_REUSEADDR` reserve-then-close), writes Attempt-fixed `health_check.response_body.healthy` JSON `{"probe_nonce","execution_id"}`, and points the file exporter at `otel/traces.jsonl`.
- `service.telemetry.metrics.level: none` is required. Default Prometheus `:8888` made a second Collector fail with “address already in use”.
- Pipeline: OTLP HTTP → transform delete SQL/params/passwords/auth → file (`format: json`, `flush_interval: 200ms`). Health GET is not OTLP/drain proof. `flush_otel` waits until the export file is parseable and size-stable.
- SUT lock (`requirements.in` / hashed `requirements.lock`, `--python-version 3.11 --exclude-newer 2026-09-06`):
  - Unchanged product pins: FastAPI 0.111.0, tortoise-orm 0.23.0, aiosqlite 0.20.0
  - `opentelemetry-api/sdk/exporter-otlp-proto-http==1.32.1`
  - `opentelemetry-instrumentation-fastapi/tortoiseorm==0.53b1`
- Execution `pyproject.toml` + root `uv.lock` declare SDK + OTLP HTTP exporter (not the instrumentors).
- `runtime-lock.json` records real file digests, collector metadata (version, platform artifacts, sampler, export protocol, health extension, `capture_parameters: false`), and recomputed `source_digest` / `runtime_digest`. No placeholder strings.

### Lifecycle

- Collector starts before SUT. Collector failure → no SUT, no login/business POST.
- `ManagedUserSutHost.start(..., validation_profile=, execution_id=)`; timeout 180s.
- `RetainedUserAttempt` optionally carries `collector_handle`, `collector_receipt`, `collector_export`.
- Recover overlays the same Collector receipt and never rebuilds Collector/SUT.
- `verified_attempt` authenticates collector readiness for `api_db_trace.v1` (still raises if the receipt is missing).
- `UserAttempt.stop` / `stop_owned` also SIGTERM the Collector when the start receipt has a collector pid.

### Harness command

`verify-otel-compatibility` ensures the pinned Collector artifact and runtime lock, then runs `benchmark/assurance-product/tests/test_user_otel_compatibility.py`. The locked SUT environment produces the spans and OTLP file; the workspace interpreter runs pytest because the locked SUT venv does not ship pytest or execution wheels.

## What I tested and test results

| Command | Result |
|---|---|
| `uv run pytest benchmark/assurance-product/tests/test_user_otel_compatibility.py tests/product/test_behavioral_projection.py::test_harness_modules_do_not_import_runtime_packages -q` | **12 passed** in ~70s |
| `uv run python benchmark/assurance-product/user_oracle_harness.py verify-otel-compatibility` | **11 passed** then `{"state":"verified",...}` |
| `uv run ruff check` on T11 Python files | All checks passed |

Compatibility coverage (real SUT + real Collector, no hand-made in-memory spans):

- Command is declared; runtime lock records real Collector + hashed OTel deps
- `api_db.v1` start does not launch Collector
- FastAPI SERVER + Tortoise CLIENT `user`/`INSERT` spans, `user.create.completed` with live username, canary password absent from OTLP file and start receipt, no `INSERT INTO` / `db.statement` / `db.query.text` in the export
- Transaction variant `rollback-success` still emits real CLIENT spans (no commit/rollback span claim)
- Collector start failure does not execute business POST / does not write `owned-process.json`
- Incomplete drain keeps `otel/diagnostics.json`, OTLP file, and collector log
- Same-attempt recover does not rebuild Collector/SUT
- New attempt uses new files and dynamic endpoints; sampler/protocol come from the lock
- Unknown semconv fails preflight
- Live health GET body equals `{"probe_nonce","execution_id"}` and passes `authenticate_collector_readiness`

A full-repo `pytest` pass was run once during implementation (~4983 passed). Failures that remained were **not** T11-owned:

- `tests/product/test_behavioral_projection.py::test_harness_modules_do_not_import_runtime_packages` **did** fail when the new test imported `graph_engine`; that import was removed (duck-typed `AttemptKey`) and the isolation test now passes.
- `packages/capabilities/assurance-execution/tests/test_agent_skills.py` verified-prepare cases fail with `verified execution requires the accepted generation result`. T11 did not edit `agent_skills.py`. `_verified_prepare_input` on this branch already omits `generation_result`; this is pre-existing relative to T11.
- `test_expected_verified_fault_is_a_successful_benchmark_without_export` and `test_finalize_allows_optional_case_outside_frozen_scope[review]` (`src/menu.py` missing) look unrelated to the T11 file set.

## TDD Evidence

### RED (before Collector lock + command wiring)

```text
uv run pytest \
  benchmark/assurance-product/tests/test_user_otel_compatibility.py::test_verify_otel_compatibility_command_is_declared \
  benchmark/assurance-product/tests/test_user_otel_compatibility.py::test_runtime_lock_records_real_collector_and_hashed_otel_deps \
  -q
FF
```

Failures: parser did not expose `verify-otel-compatibility`; `collector.yaml` / collector lock metadata were absent.

### GREEN (after implementation; re-verified this session)

```text
uv run pytest benchmark/assurance-product/tests/test_user_otel_compatibility.py \
  tests/product/test_behavioral_projection.py::test_harness_modules_do_not_import_runtime_packages -q
............                                                             [100%]
12 passed in 70.43s

uv run python benchmark/assurance-product/user_oracle_harness.py verify-otel-compatibility
...........                                                              [100%]
11 passed in 68.92s
{"command": "verify-otel-compatibility", "runtime_digest": "sha256:77f8a9dffc5a9ef375a782e1e09278e2aac454b673b5e550c07324f2625cef0f", "schema_version": "1", "source_digest": "sha256:84c5905d0df6d45e2ce8a5a4f50a36b877e00d8f5e2c38f19fd12b47307b9558", "state": "verified"}
```

## Files changed

- `benchmark/assurance-product/fixtures/user-oracle/bootstrap.py`
- `benchmark/assurance-product/fixtures/user-oracle/collector.yaml` (new)
- `benchmark/assurance-product/fixtures/user-oracle/requirements.in`
- `benchmark/assurance-product/fixtures/user-oracle/requirements.lock`
- `benchmark/assurance-product/fixtures/user-oracle/runner-lock.json`
- `benchmark/assurance-product/fixtures/user-oracle/runtime-lock.json`
- `benchmark/assurance-product/fixtures/user-oracle/sut-source/app/__init__.py`
- `benchmark/assurance-product/fixtures/user-oracle/sut-source/app/api/v1/users/users.py`
- `benchmark/assurance-product/user_oracle_harness.py`
- `benchmark/assurance-product/tests/test_user_otel_compatibility.py` (new)
- `packages/capabilities/assurance-execution/assurance_execution/operations/managed_sut.py`
- `packages/capabilities/assurance-execution/assurance_execution/operations/user_attempt.py`
- `packages/capabilities/assurance-execution/assurance_execution/operations/verified_attempt.py`
- `packages/capabilities/assurance-execution/pyproject.toml`
- `tests/verified_generation_fixture.py` (additive `validation_profile=` defaulting to `api_db.v1`)
- `uv.lock`
- `.superpowers/sdd/task-11-report.md` (this file)

Not committed: `.superpowers/sdd/progress.md`, leftover `task-10-report.md` edits. `original-source-lock.json` was left alone because `sut-source` already diverged before T11.

Harness / runner byte pin after format: `8f337f1feaf2045d226bbc8ad084d35de4fdaccaf966926476f090a839149be3`.

## Self-review findings

- Completeness: closed-set entry, Collector-before-SUT, recover-without-rebuild, hashed deps, digest-locked Collector, official instrumentors, create-user checkpoint span, scrub + semconv normalize, health_check body verified live.
- Quality: spans come from FastAPI + Tortoise against a real SQLite User INSERT, then a real file exporter. No in-memory fake spans.
- Discipline: no second User runtime; no T12/T13; no invented “latest” Collector; no fake Agent runs.
- Testing: one import-isolation leak (`graph_engine` in the new test file) was found on the full-suite pass and fixed before commit.
- YAGNI: Collector metrics disabled only because a second instance collided on `:8888`; that is a real bind failure, not speculative hardening.

## Issues or concerns

1. `verify-otel-compatibility` uses the workspace `pytest` after checking the locked Collector + runtime lock. The locked SUT venv produces the database writes and OTLP file but does not contain pytest. This matches how the rest of the User host tests run.
2. Full-suite leftovers (`test_agent_skills` verified-prepare, one phase5 fault layout, one intake finalize missing `src/menu.py`) were not fixed. T11 did not change `agent_skills.py`. Do not treat those as T11 regressions unless a later bisect shows otherwise.
3. Official `otelcol-contrib` v0.129.1 tarball extracts a binary that reports `0.129.0`. The lock is the tarball digest + release tag `v0.129.1`.
4. Create-user HTTP path is `POST /api/v1/user/create`. Emails must be `@example.com` (`@*.invalid` → 422).

## Actual Collector / SDK / instrumentor versions verified

| Component | Version | How verified |
|---|---|---|
| otelcol-contrib | release **v0.129.1** (binary reports 0.129.0) | downloaded pinned tarballs; darwin_arm64 / linux_amd64 sha256 as above; live health_check custom body; live OTLP HTTP → file |
| opentelemetry-api | **1.32.1** | `requirements.in` + hashed lock; create-user path |
| opentelemetry-sdk | **1.32.1** | same; provider + SimpleSpanProcessor |
| opentelemetry-exporter-otlp-proto-http | **1.32.1** | same; SUT export to Collector |
| opentelemetry-instrumentation-fastapi | **0.53b1** | official `instrument_app`; SERVER span for `/api/v1/user/create` |
| opentelemetry-instrumentation-tortoiseorm | **0.53b1** | official `instrument(..., capture_parameters=False)`; CLIENT span for `user`/`INSERT` |
| FastAPI / tortoise-orm / aiosqlite | 0.111.0 / 0.23.0 / 0.20.0 | unchanged pins; live create + sqlite row |
