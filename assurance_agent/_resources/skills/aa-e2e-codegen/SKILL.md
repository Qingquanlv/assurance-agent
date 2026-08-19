---
name: aa-e2e-codegen
description: Generate E2E tests and a strict generated-files manifest after the E2E plan gate passes.
---

## Purpose

Generate E2E tests under `tests/e2e/**`, shared builders under `tests/testdata/**` when authorized, a human summary, and a strict generated-files manifest. Do not run pytest collect or claim Traceability Verification evidence.

## Inputs

### required

- `change:plans/e2e-plan.md`
- `change:plans/e2e-test-data-plan.md`
- `change:plans/e2e-codegen-plan.md`
- `change:plans/e2e-codegen-mapping.json`
- `change:plans/m4-review-summary.md`
- `change:review/plan-review.json`
- `change:cases/**/case.yaml`
- `repo:.aa/data-knowledge.yaml`

### optional

- `repo:.aa/config.yaml`
- `repo:tests/e2e/**`
- `repo:tests/testdata/domain/**`

## Outputs

### required

- `change:codegen/e2e-codegen-summary.md`
- `change:codegen/e2e-generated-files.json`

### conditional

- `repo:tests/e2e/**`
- `repo:tests/testdata/domain/**`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Boundaries

Write only authorized test/testdata paths plus the summary and manifest. Do not modify product source. Do not run pytest.

## Fixture Closure

Before writing the manifest, resolve every mapped test parameter to a fixture
defined or imported by the test module, an ancestor `conftest.py`, or the
installed Playwright pytest plugin. Generate any missing project fixture under
authorized `tests/e2e/**`, record changed fixture files as `support` with
`case_ids: []`, and finish only when the unresolved fixture set is empty. The
Graph precommit validator checks this candidate-tree contract without executing
the fixtures.

## Generated-files Manifest Rules

- Only `test_entry` entries may claim mapped Case IDs, and their `case_ids` must
  exactly match the codegen mapping (`plans/e2e-codegen-mapping.json`) for that path.
- Every `support` and `shared_builder` entry must use `case_ids: []`.
- Use `generated` only for a newly added file and `updated` only for a file whose
  content this invocation changed.
- `reused` is legal only for an unchanged, selected private-root `test_entry`
  that is itself a codegen-mapping target. Never list an unchanged adapter, helper,
  fixture, or shared builder as `reused`; omit unchanged support dependencies
  from the manifest.
