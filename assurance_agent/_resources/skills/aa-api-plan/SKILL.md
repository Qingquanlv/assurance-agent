---
name: aa-api-plan
description: Use when a QA Case Delta contains API automation cases and you need reviewable API plan files before test code is written. Processes added or modified API cases with automation.required = true and never generates tests.
---

## Purpose

Turn the approved API portion of a QA Case Delta into reviewable implementation plans for `aa-api-plan-reviewer` and, after that gate passes, `aa-api-codegen`.

Read the change from disk; do not rely on conversation history. Apply non-deprecated guidance from `.aa/memory/aa-api-plan.md` when that read-only file exists. Select only `added` and `modified` entries whose type is API and whose automation is required; retain `removed` entries only as context.

Planning establishes case coverage, endpoint and assertion intent, data setup and cleanup, factory/adapter ownership, and separate Plan Readiness and Codegen Readiness. Unknown product facts remain explicit review items or blockers; they are never guessed.

## Inputs

### required

- `change:cases/**/case.yaml`
- `change:.qa.yaml`
- `change:proposal.md`

### optional

- `change:review/api-plan-review.json`
- `change:facts/fact-baseline.json`
- `repo:.aa/config.yaml`
- `repo:.aa/data-knowledge.yaml`
- `repo:app/**` (read-only SUT contract evidence)
- `repo:tests/testdata/domain/**`
- `repo:tests/api/adapters/**`
- `repo:tests/config.py`
- `repo:tests/conftest.py`

## Outputs

### required

- `change:plans/api-plan.md`
- `change:plans/api-test-data-plan.md`
- `change:plans/api-codegen-plan.md`
- `change:plans/api-codegen-mapping.json`
- `change:plans/m3-review-summary.md`

### conditional

- `change:plans/data-knowledge.proposal.api.yaml`

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
entities:
  user:
    required_fields:
      - username
    constraints:
      username_unique: true
capabilities:
  domain_factories: {}
  adapters:
    api: {}
    e2e: {}
    fuzz: {}
    performance: {}
  cleanup: {}
discovered_candidates:
  - id: DK-API-USER-001
    knowledge_key: entities.user.constraints.username_unique
    evidence: app/api/v1/user/user.py
needs_review:
  - Confirm the source-backed user constraint before promotion into L1.
promotion_checklist:
  - Run `aa knowledge validate --change <change-id>` before promotion.
```

After writing the proposal, validate the whole envelope against the runtime
`DataKnowledgeProposal` contract, including field types and extra-forbid behavior. Do
not validate only an inner entity or capability leaf.

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Boundaries

Write only the plan artifacts listed in Outputs.

Do not write `.aa/data-knowledge.yaml` or `.aa/memory/**`.

Do not modify case files, proposal files, application source, or backend source.

Do not write tests, factories, adapters, helpers, or execution results.

Do not run pytest or execute data setup during planning.

Do not silently guess endpoints, methods, auth, schemas, fixtures, cleanup, or product behavior.

Before naming an endpoint, payload, response field, or reusable helper, inspect
the declared `repo:app/**` and test inputs. A plan must describe the observed
contract, including create operations whose success response has no identifier
and therefore requires a supported follow-up lookup.

Do not use `removed` cases as plan scope.

Do not continue past planning into codegen.

## Domain Notes

When `change:review/api-plan-review.json` exists with `decision: needs_fix`, apply
only the findings' `locator` targets. Do not rewrite unmentioned plan sections or
mapping rows. Keep `plans/api-codegen-mapping.json` as the closed Case ID → symbol →
target file contract; the markdown plan is narrative only.

Shared business-valid factories belong in `tests/testdata/domain/`. They own domain defaults and invariant-preserving create/cleanup behavior, return plain snapshots, and contain no pytest, HTTP client, Playwright, Hypothesis, Locust, subprocess, or event-loop bridge code.

API lifecycle and transport glue belongs in `tests/api/adapters/`. A plan maps each data need to a shared domain factory and an API adapter. The first active codegen layer may own a shared module that is absent from L1 as `create-if-missing`; every L1-declared symbol and every later-layer reference is `reuse`.

Authoring tables (keep column names exact):

- Scope uses `Case ID | Title`.
- API Targets uses `Case ID | Scenario | Method | Path | Expected`.
- Auth Strategy uses `Case ID | Auth`.
- Request Strategy uses `Case ID | Headers | Body | Params`.
- Assertion Strategy uses `Case ID | Assertions`.
- Mock Strategy uses `Case ID | Dependency | Approach`.
- Cleanup Strategy uses `Case ID | Cleanup`.
- Output File Candidates uses `Case ID | Target File`.
- Required Data uses `Entity | State | Capability`.
- Capability Mapping uses `Need | Capability | Source | Status (found/missing/warning)`.
- Factory / Boundary Strategy uses `Entity | Ring | Preferred method | Notes`.
- No Data Required Cases uses `Case ID | Rationale`.
- Target Files uses `File | Purpose`.
- Test Function Mapping uses `Case ID | Test Function | Target File`. Every function is named `test_<case_id_lowercase>__<desc>`; the full case_id and double underscore are mandatory.
- Factory Mapping uses `Entity | Shared Module | Function | Ownership | Required By`. `Shared Module` names `tests/testdata/domain/<entity>.py`, and Ownership is `reuse` for every symbol already declared by L1 knowledge. Use `create-if-missing` only when L1 does not declare that shared symbol.
- Adapter Mapping uses `Entity | API Adapter | Transport | Cleanup`.
- Fixture Mapping uses `Fixture | Source Factory | Wrapper Only (yes/no) | Required By`.
- Helper Mapping uses `Helper | Purpose | Required By`.
- Import Strategy uses `Target File | Imports`.
- Assertion Mapping uses `Case ID | Assertions`.
- Data Setup Mapping uses `Case ID | Setup | Capability`.
- Cleanup Mapping uses `Case ID | Cleanup | Capability`.
- Run Guidance uses `Target | Pytest Args | Markers | Environment`.
