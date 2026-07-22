---
name: aa-fuzz-codegen
description: "AA M3 Fuzz Stage 2: generate schemathesis fuzz tests from a reviewed fuzz plan. Use only after fuzz-plan-review.json has decision == pass and codegen_readiness in [ready, ready_with_warnings]. Reads fuzz plan files and writes tests/fuzz/test_<module>_fuzz.py. Does NOT execute pytest — execution is Phase 8 aa-run."
---

## Per-Skill Memory

Before producing output, check whether `.aa/memory/aa-fuzz-codegen.md` exists in the project root. If it exists, read it before producing output and apply only entries that are not marked `deprecated:`. Treat the file as read-only runtime guidance; do not create, edit, or delete `.aa/memory/**`.

## Test Data Architecture Contract

- Shared business-valid builders live in `tests/testdata/domain/`; fuzz state adapters live in `tests/fuzz/adapters/`; Hypothesis/Schemathesis value strategies live in `tests/fuzz/strategies/`.
- Strategies must be pure value generation. Persistent setup/cleanup is owned by the fuzz adapter, which may call a shared factory through a project-confirmed async or process boundary.
- Read `capabilities.domain_factories` and `capabilities.adapters.fuzz`; never import API/E2E adapters or their pytest fixtures. Shared files are create-if-missing, then immutable to later layers.

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/workflow-state.yaml`.
2. Verify `phases.fuzz_plan_review.status == pass`.
3. Read input files: `plans/fuzz-plan.md`, `plans/fuzz-codegen-plan.md`, `plans/fuzz-review-summary.md`, `review/fuzz-plan-review.json`, selected `cases/**/case.yaml` (type == Fuzz).
4. Verify `review/fuzz-plan-review.json` gate fields in order — **STOP** on first failure:
   - valid JSON
   - `review_type == "fuzz-plan"`
   - `change_id == <change-id>`
   - `decision == "pass"`
   - `codegen_readiness in ["ready", "ready_with_warnings"]`
   - `blockers` is empty
   - no `needs_review` item has `blocking == true`
5. Use files as the sole source of truth.

**After completing work:**

1. Write generated fuzz test files per `fuzz-codegen-plan.md` Target Files:
   - `tests/fuzz/test_<module>_fuzz.py` (required path — runner discovers `tests/fuzz/`)
   - `tests/fuzz/strategies/<module>.py` (when the plan maps reusable value strategies)
   - `tests/fuzz/adapters/<module>.py` (only for mapped stateful setup/cleanup)
   - `tests/testdata/domain/<entity>.py` (only when absent and marked `create-if-missing`)
   - `qa/changes/<change-id>/codegen/fuzz-codegen-summary.md`
2. Report the `workflow-state.yaml` state delta (inline mode: apply it directly; dispatched subagent: never write `workflow-state.yaml` — report the values in your final message and the orchestrator applies them):
   - `phases.fuzz_codegen.status = done`
   - `phases.fuzz_codegen.review_gate_file = review/fuzz-plan-review.json`
   - `phases.fuzz_codegen.generated_tests.files` = list of generated files

---

# AA Fuzz Codegen

## Purpose

Translate a reviewed fuzz plan into executable schemathesis tests. This skill does **not** execute pytest — execution is Phase 8 `aa-run`.

## What to Generate

Fuzz tests use schemathesis's pytest integration so they run on the existing pytest infrastructure and emit JUnit XML:

```python
import schemathesis
from tests.config import settings

# Default: acquire the schema from the LIVE SUT over HTTP. Do NOT import the app.
schema = schemathesis.openapi.from_url(f"{settings.base_url}/openapi.json")

# case_id TC_MENU_FUZZ_001
@schema.parametrize(endpoint="/api/v1/menu/create")
def test_tc_menu_fuzz_001__menu_create(case):
    response = case.call()
    case.validate_response(response)   # asserts: no 5xx, response conforms to declared schema
```

- **The test function name MUST be prefixed with the normalized case_id** (lowercase case_id + `__` + description): `test_<case_id lowercase>__<description>`. e.g. case_id `TC_MENU_FUZZ_001` → `def test_tc_menu_fuzz_001__menu_create(case)`. This is the **only mandatory traceability marker** (not a comment/docstring); `aa run` recovers the case_id from the function name (case-insensitive).
- **Default to `from_url` against the live SUT** (`settings.base_url`) with `case.call()`. This fuzzes the same running SUT + real DB that the seed/cleanup adapters target, so results are consistent. **Do NOT default to `from_asgi`/`from app import app`**: importing the app boots its lifespan in-process (e.g. aerich `init_db → migrate`, which writes `migrations/**` and would trip the execution write-set guard) and binds the fuzzed app to the sandbox DB instead of the seeded real DB. Only use `from_asgi` when the plan *explicitly* requires in-process transport; if you gate on an env var, use exactly `QA_FUZZ_SCHEMA_MODE` (default = `uri`).
- Pass auth via the plan's strategy (reuse fixtures / data-knowledge). Never hardcode real tokens.
- Output exactly to `tests/fuzz/test_<module>_fuzz.py` so the CLI runner discovers it.
- Put reusable pure value generation in `tests/fuzz/strategies/`. Put persistent setup/cleanup in `tests/fuzz/adapters/`; never create state inside a Hypothesis strategy.
- Never import `tests/api/adapters/` or `tests/e2e/adapters/`. Reuse shared domain code only through the mapped fuzz adapter.
- When a schemathesis-parametrized test consumes **function-scoped** pytest fixtures for fixed setup context (seeded role/dept/user, cleanup trackers), Hypothesis raises `FailedHealthCheck: function_scoped_fixture` because the fixture is not reset per generated input. That is expected here — the seed is stable context while the body is fuzzed — so suppress **only** that one health check:

```python
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings  # alias: tests.config already exports `settings`

@_create_schema.parametrize()
@hypothesis_settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_tc_user_fuzz_001__user_create_schema_robustness(case, fuzz_admin_headers, fuzz_role_dept_seed):
    ...
```

  This is a fixture-scope compatibility shim, NOT a generation-disabling setting — it does not reduce examples, extend deadlines, or hide 5xx. Never suppress other health checks or pass `max_examples`/`deadline`/`phases` to force green (see Test Failure Integrity).

## Test Failure Integrity

Generated fuzz tests MUST fail for the right reason. Rules:

- Every assertion MUST map to a plan expectation (no 5xx / schema-valid input not wrongly rejected / response schema-conformant).
- Do not use `assert True`, placeholder assertions, or empty expectations.
- Do not swallow failures with empty `try/except` or `except Exception: pass`.
- Do not add fallback logic that hides product failures (a real 5xx surfaced by fuzzing is a product defect, not something to suppress).
- Do not use `skip`, `xfail`, early return, or hypothesis settings that disable generation to make fuzz tests green.
- Do not loosen `validate_response` or narrow the endpoint set after observing failures.
- Do not mock the behavior under test.
- Setup may be flexible; assertions must be strict.

Self-check before reporting complete:
1. Would this test fail if the endpoint returns a 5xx or violates its schema?
2. Does every assertion trace back to the plan's expectations?
3. Are setup failures reported instead of hidden?

If any answer is unsafe, fix the test before reporting codegen complete.

## Hard Rules

- STOP if the gate (`fuzz-plan-review.json`) is not `pass` with empty blockers.
- Do not run pytest or produce execution results — that is Phase 8 `aa-run`.
- Do not design new cases or change test scope.
- Do not write to `tests/api/` or `tests/e2e/` — fuzz output goes to `tests/fuzz/` only.
- Do not weaken assertions to force green; a fuzz-discovered product crash is a defect to report.
