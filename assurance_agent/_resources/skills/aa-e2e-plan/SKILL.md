---
name: aa-e2e-plan
description: Use when a QA Case Delta contains E2E automation cases and you need reviewable E2E plan files before test code is written.
---

## Purpose

Turn the approved E2E portion of a QA Case Delta into reviewable implementation plans for `aa-e2e-plan-reviewer` and, after that gate passes, `aa-e2e-codegen`.

## Inputs

### required

- `change:cases/**/case.yaml`
- `change:.qa.yaml`
- `change:proposal.md`

### optional

- `change:facts/fact-baseline.json`
- `repo:.aa/config.yaml`
- `repo:.aa/data-knowledge.yaml`
- `repo:tests/testdata/domain/**`
- `repo:tests/e2e/**`
- `repo:tests/config.py`
- `repo:tests/conftest.py`

## Outputs

### required

- `change:plans/e2e-plan.md`
- `change:plans/e2e-test-data-plan.md`
- `change:plans/e2e-codegen-plan.md`
- `change:plans/m4-review-summary.md`

### conditional

- `change:plans/data-knowledge.proposal.e2e.yaml`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Boundaries

Write only the plan artifacts listed in Outputs. Do not write tests or continue into codegen.

## Domain Notes

Authoring tables (keep column names exact):

- Scope uses `Case ID | Title`.
- Required Data uses `Entity | State | Capability`.
- Capability Mapping uses `Need | Capability | Source | Status (found/missing/warning)`.
- Target Files uses `File | Purpose`.
- Test Function Mapping uses `Case ID | Test Function | Target File`. Every function is named `test_<case_id_lowercase>__<desc>`; use the full case_id.
- Factory Mapping uses `Entity | Shared Module | Function | Ownership | Required By`. Ownership is `reuse` for every symbol already declared by L1 knowledge. Use `create-if-missing` only when L1 does not declare that shared symbol.
- Adapter Mapping uses `Entity | E2E Adapter | Transport | Cleanup`.
- Fixture Mapping uses `Fixture | Source Factory | Wrapper Only (yes/no) | Required By`.
- Assertion Mapping uses `Case ID | Assertions`.
- Cleanup Mapping uses `Case ID | Cleanup | Capability`.
- Run Guidance uses `Target | Pytest Args | Markers | Environment`.
