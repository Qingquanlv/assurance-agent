# E2E plan

Capability-owned E2E plan skill. Do not select a provider, model, or adapter.

Turn the approved E2E portion of a reviewed case document into reviewable
implementation plans. Schema truth is `assurance_generation.contracts` for the
plan result and `assurance_intake.contracts` for reviewed cases.

## Locked Capability Selection

The result contract's `required_capabilities` enum is the sole whitelist for
the top-level result and every coverage row. Copy exact strings from that enum;
never construct a key from a namespace, helper name, or analogous layer. In
particular, a declared `capabilities.domain_factories.*` leaf does not imply a
same-suffix `capabilities.adapters.e2e.*` leaf. If the exact needed leaf is not
in the enum, describe the gap in the plan/review readiness; do not emit a
virtual key. Before returning, reject your own result unless every capability
value is byte-for-byte present in the enum.

## Inputs

Read `proposal.md` first. When its `Product Source Verification` section lists
exact product-source paths, read every listed path directly before any discovery.
A glob result of `No files found` is not evidence that product source is absent;
ignored source files remain exact-readable. Only declare source unavailable after
those exact reads and a path-scoped grep both fail.

### required

- `qa/changes/<change-id>/cases/**/case.yaml`
- `qa/changes/<change-id>/.qa.yaml`
- `qa/changes/<change-id>/proposal.md`

### optional

- `qa/changes/<change-id>/review/e2e-plan-review.json`
- `qa/changes/<change-id>/facts/fact-baseline.json`
- `.aa/config.yaml`
- `.aa/data-knowledge.yaml`
- backend and frontend product source (read-only)
- `tests/testdata/domain/**`
- `tests/e2e/**`
- `tests/config.py`
- `tests/conftest.py`

## Outputs

### required

- `qa/changes/<change-id>/plans/e2e-plan.md`
- `qa/changes/<change-id>/plans/e2e-test-data-plan.md`
- `qa/changes/<change-id>/plans/e2e-codegen-plan.md`
- `qa/changes/<change-id>/plans/e2e-codegen-mapping.json`
- `qa/changes/<change-id>/plans/m4-review-summary.md`

## Closed Codegen Mapping Contract

`e2e-codegen-mapping.json` must use this exact JSON shape:

```json
{"schema_version":"1","layer":"e2e","entries":[{"case_id":"TC_DEPT_E2E_001","symbol":"test_tc_dept_e2e_001__behavior","target_file":"tests/e2e/test_dept.py"}]}
```

Use `schema_version: "1"`, not `"1.0"`. The only top-level keys are
`schema_version`, `layer`, `entries`, and optional `schema_case_ids`. Each entry
has exactly `case_id`, `symbol`, and `target_file`. Do not emit `family`,
`change_id`, `mappings`, or `test_function`. Map every selected E2E Case ID
exactly once, and no other Case ID.

### conditional

- `qa/changes/<change-id>/plans/data-knowledge.proposal.e2e.yaml`

When a proposal is required, write the complete data-knowledge proposal envelope
and validate the whole envelope against the runtime contract.

## Boundaries

Write only the plan artifacts listed in Outputs. Do not write tests or continue
into codegen. The graph owns phase state. Do not write an orchestration state
file.

## Domain Notes

Inspect the declared backend source, frontend DOM source, and existing
E2E/test-data helpers before planning locators or fixture lifecycles. Record
the real request and response shape for every setup/cleanup operation; when
create returns no identifier, plan an exact lookup rather than assuming
`data.id`.

Authoring tables (keep column names exact):

- Scope uses `Case ID | Title`.
- Required Data uses `Entity | State | Capability`.
- Capability Mapping uses `Need | Capability | Source | Status (found/missing/warning)`.
- Target Files uses `File | Purpose`.
- Test Function Mapping uses `Case ID | Test Function | Target File`. Every
  function is named `test_<case_id_lowercase>__<desc>`; use the full case_id.
- Factory Mapping uses `Entity | Shared Module | Function | Ownership | Required By`.
  Ownership is `reuse` for every symbol already declared by L1 knowledge. Use
  `create-if-missing` only when L1 does not declare that shared symbol.
- Adapter Mapping uses `Entity | E2E Adapter | Transport | Cleanup`.
- Fixture Mapping uses `Fixture | Source Factory | Wrapper Only (yes/no) | Required By`.
- Assertion Mapping uses `Case ID | Assertions`.
- Cleanup Mapping uses `Case ID | Cleanup | Capability`.
- Run Guidance uses `Target | Pytest Args | Markers | Environment`.

When `review/e2e-plan-review.json` exists with `decision: needs_fix`, apply only
the findings named in `auto_fix_plan` and only the `locator` targets they point
at. Keep `plans/e2e-codegen-mapping.json` as the closed Case ID → symbol →
target file contract.

Every planned case must have operation and risk coverage. Capability keys must
be exact typed leaves.
