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

- `change:review/plan-review.json`
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
- `change:plans/e2e-codegen-mapping.yaml`
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

When a proposal is required, write the complete `DataKnowledgeProposal` envelope. For a
delta, `based_on_l1_version` is the current L1 `version`; for a bootstrap proposal it is
`null` and `mode` is `bootstrap`. Top-level `version`, `change_id`, `proposal_kind`,
`target`, `proposed_leaves`, and layer-specific wrapper objects are forbidden.

### Canonical delta proposal

```yaml
schema_version: "1"
based_on_l1_version: 1
mode: delta
accounts: {}
auth: {}
entities: {}
capabilities:
  domain_factories: {}
  adapters:
    api: {}
    e2e:
      role:
        make_role:
          kind: isolated_worker
          symbol: tests.e2e.adapters.role.make_role
          entity: role
          create-if-missing: false
          cleanup_ref: cleanup.role_delete
    fuzz: {}
    performance: {}
  cleanup:
    role_delete:
      symbol: tests.testdata.domain.role.cleanup_role
      retry_safe: true
discovered_candidates:
  - id: DK-E2E-ROLE-001
    capability: capabilities.adapters.e2e.role.make_role
    evidence: tests/e2e/adapters/role.py
needs_review:
  - Confirm the on-disk role adapter is safe to promote into L1.
promotion_checklist:
  - Run `aa knowledge validate --change <change-id>` before promotion.
```

After writing the proposal, validate the whole envelope against the runtime
`DataKnowledgeProposal` contract, including field types and extra-forbid behavior. Do
not validate only an inner entity or capability leaf.

When `change:review/plan-review.json` exists with `decision: needs_fix`, apply only
the findings named in `auto_fix_plan` and only the `locator` targets they point at.
Repair Factory Mapping rows the findings authorize. Do not edit
`review/e2e-plan-checks.json` or `.aa/data-knowledge.yaml`. Never promote proposal content into L1.
Keep `plans/e2e-codegen-mapping.yaml` as the closed Case ID → symbol → target file
contract; the markdown plan is narrative only.
