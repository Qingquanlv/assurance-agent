---
name: aa-api-codegen
description: >-
  Use only after api-plan-review.json has decision == "pass" and codegen_readiness in ["ready", "ready_with_warnings"]. Triggers on: "generate test code from plan", "continue API codegen", "implement api-codegen-plan", "generate /tests/api". User request may trigger this skill but never replaces the JSON gate. Reads Stage 1 plan files and generates pytest code, fixtures, and helpers. Does NOT execute pytest — test execution is Phase 8 aa-run. Never runs before planning is complete.
---

## Purpose

Generate API tests under `tests/api/**`, shared builders under `tests/testdata/**` when authorized, a human summary, and a strict generated-files manifest. Collection and execution are graph-owned later; do not run pytest collect or claim Traceability Verification evidence.

## Inputs

### required

- `change:plans/api-plan.md`
- `change:plans/api-test-data-plan.md`
- `change:plans/api-codegen-plan.md`
- `change:plans/m3-review-summary.md`
- `change:review/api-plan-review.json`
- `change:cases/**/case.yaml`
- `repo:.aa/data-knowledge.yaml`

### optional

- `repo:.aa/config.yaml`
- `repo:tests/api/**`
- `repo:tests/api/adapters/**`
- `repo:tests/testdata/domain/**`

## Outputs

### required

- `change:codegen/api-codegen-summary.md`
- `change:codegen/api-generated-files.json`

### conditional

- `repo:tests/api/**`
- `repo:tests/testdata/domain/**`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Boundaries

Write only authorized test/testdata paths plus the summary and manifest.

Do not modify product source.

Do not run pytest or invent collection evidence.

### Blocked gate output

`STOP` blocks product and test generation; it does not cancel the node's mandatory
evidence contract. On the first failed gate, you must not modify `tests/**`, shared
factories, adapters, helpers, or application code. Always write both of these evidence
artifacts before returning:

- `qa/changes/<change-id>/codegen/api-codegen-summary.md`, with a `Blocked` section
  naming the first failed gate and confirming that no test or support file was changed.
- `qa/changes/<change-id>/codegen/api-generated-files.json`, with an empty `files`
  array:

```json
{
  "schema_version": "1",
  "change_id": "<change-id>",
  "layer": "api",
  "files": []
}
```

These two files record the fail-closed outcome; writing them is not permission to
continue codegen or to report `phases.api_codegen.status = done`.
