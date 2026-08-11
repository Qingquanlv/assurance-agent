---
name: aa-api-plan
description: Use when a QA Case Delta contains API automation cases and you need reviewable API plan files before test code is written. Processes added or modified API cases with automation.required = true and never generates tests.
---

## Purpose

Turn the approved API portion of a QA Case Delta into reviewable implementation plans for `aa-api-plan-reviewer` and, after that gate passes, `aa-api-codegen`.

Read the change from disk; do not rely on conversation history. Apply non-deprecated guidance from `.aa/memory/aa-api-plan.md` when that read-only file exists. Start only when `workflow-state.yaml` says the case-review phase passed. Select only `added` and `modified` entries whose type is API and whose automation is required; retain `removed` entries only as context.

Planning establishes case coverage, endpoint and assertion intent, data setup and cleanup, factory/adapter ownership, and separate Plan Readiness and Codegen Readiness. Unknown product facts remain explicit review items or blockers; they are never guessed.

## Inputs

Required; stop if any is missing:

- `qa/changes/<change-id>/workflow-state.yaml`
- `qa/changes/<change-id>/.qa.yaml`
- `qa/changes/<change-id>/proposal.md`
- `qa/changes/<change-id>/cases/**/case.yaml`

Optional; warn if absent:

- `.aa/config.yaml`
- `.aa/data-knowledge.yaml`
- backend source files used to confirm methods, paths, request schemas, and response schemas
- existing `tests/testdata/domain/**`, `tests/api/adapters/**`, `tests/config.py`, and `tests/conftest.py`

If `.aa/data-knowledge.yaml` is absent, planning still completes with a bootstrap `plans/data-knowledge.proposal.api.yaml`; Plan Readiness may be `ready_with_warnings`, but Codegen Readiness is `not_ready` until the formal knowledge file exists. If the formal file exists but required leaves are missing, write a delta proposal and keep codegen blocked until promotion.

If `tests/config.py` lacks a required runtime fact, ask the user to confirm the backend/frontend URL, QA credential, or collision-safe data prefix from project evidence. An unconfirmed value remains in Needs Review and makes Codegen Readiness `not_ready`.

## Outputs

Write only below `qa/changes/<change-id>/plans/`:

| Path | Required structure |
|---|---|
| `api-plan.md` | Source paths; scope; API Targets; auth, request, assertion, mock, and cleanup strategies; output candidates; Needs Review; Blockers |
| `api-test-data-plan.md` | data scope; Required Data; preconditions; Capability Mapping; Factory / Boundary Strategy; setup, runtime verification, and cleanup; no-data cases; Blockers / Assumptions; review checklist |
| `api-codegen-plan.md` | Target Files; Test Function Mapping; Factory Mapping; Adapter Mapping; Fixture Mapping; Helper Mapping; Import Strategy; Assertion Mapping; Data Setup Mapping; Cleanup Mapping; Run Guidance; Codegen Preconditions |
| `m3-review-summary.md` | change and loaded cases; API and removed-case counts; generated files; knowledge status; Blockers; Needs Review; Plan Readiness; Codegen Readiness; next reviewer |
| `data-knowledge.proposal.api.yaml` | only when the formal knowledge file is absent or lacks required leaves; candidate evidence and promotion needs, never invented implementation facts |

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

The Markdown tables below are authoring interfaces; the runtime checks parse a subset of them. Keep every column name and order exact.

- At least one table in `api-plan.md` must contain `Case ID`, and every in-scope case must appear in a `Case ID` cell as its full case_id such as `TC_USER_API_001`, never a short number such as `008`.
- Scope uses `Case ID | Title`.
- API Targets uses `Case ID | Scenario | Method | Path | Expected`. Unknown Method or Path is `TBD` in that row and is also explained under Needs Review or Blockers.
- Auth Strategy uses `Case ID | Auth`.
- Request Strategy uses `Case ID | Headers | Body | Params`; method, path, headers, body, and params stay in the plan rather than being written back to the case.
- Assertion Strategy uses `Case ID | Assertions`.
- Mock Strategy uses `Case ID | Dependency | Approach`.
- Cleanup Strategy uses `Case ID | Cleanup`.
- Output File Candidates uses `Case ID | Target File`.
- Required Data uses `Entity | State | Capability`.
- Capability Mapping uses `Need | Capability | Source | Status (found/missing/warning)`.
- Factory / Boundary Strategy uses `Entity | Ring | Preferred method | Notes` and covers every entity under test.
- No Data Required Cases uses `Case ID | Rationale`.
- Target Files uses `File | Purpose`; include each domain factory as `create-if-missing` or `reuse` in its purpose and the corresponding API adapter whenever setup or cleanup needs domain data.
- Test Function Mapping uses `Case ID | Test Function | Target File`. Every function is named `test_<case_id_lowercase>__<desc>`; the full lowercase case ID and double underscore are mandatory.
- Factory Mapping uses `Entity | Shared Module | Function | Ownership | Required By`. `Shared Module` names `tests/testdata/domain/<entity>.py`, `Function` names a `make_*` capability, and `Ownership` is `reuse` for every symbol already declared by L1 knowledge. Use `create-if-missing` only when L1 does not declare that shared symbol.
- Adapter Mapping uses `Entity | API Adapter | Transport | Cleanup`; API Adapter names `tests/api/adapters/<module>.py`, and Transport is `in_process_async`, `http`, or `isolated_worker`.
- Fixture Mapping uses `Fixture | Source Factory | Wrapper Only (yes/no) | Required By`; fixtures only wrap lifecycle or transport.
- Helper Mapping uses `Helper | Purpose | Required By`.
- Import Strategy uses `Target File | Imports`.
- Assertion Mapping uses `Case ID | Assertions` and preserves every case assertion semantically.
- Data Setup Mapping uses `Case ID | Setup | Capability`.
- Cleanup Mapping uses `Case ID | Cleanup | Capability`.
- Run Guidance uses `Target | Pytest Args | Markers | Environment`; it is execution-layer input and is not run during planning.

The plan summary reports all files actually written and the state delta `phases.api_plan.status = done` with those paths in `phases.api_plan.outputs`. The next action is `aa-api-plan-reviewer`; codegen waits for its gate and for the formal knowledge file.

## Boundaries

Write only the plan artifacts listed in Outputs.

Do not write `.aa/data-knowledge.yaml` or `.aa/memory/**`.

Do not modify case files, proposal files, application source, or backend source.

Do not write tests, factories, adapters, helpers, or execution results.

Do not run pytest or execute data setup during planning.

Do not set the API-plan-review phase or release the codegen gate.

Do not silently guess endpoints, methods, auth, schemas, fixtures, cleanup, or product behavior.

Do not use `removed` cases as plan scope.

Do not continue past planning into codegen.

## Domain Notes

Shared business-valid factories belong in `tests/testdata/domain/`. They own domain defaults and invariant-preserving create/cleanup behavior, return plain snapshots, and contain no pytest, HTTP client, Playwright, Hypothesis, Locust, subprocess, or event-loop bridge code.

API lifecycle and transport glue belongs in `tests/api/adapters/`. A plan maps each data need to a shared domain factory and an API adapter. The first active codegen layer may own a shared module that is absent from L1 as `create-if-missing`; every L1-declared symbol and every later-layer reference is `reuse`.

Choose setup by aggregate boundary:

- Within an aggregate, use its shared `make_*` factory through the API adapter.
- Across aggregates, create a valid referenced entity first, then pass its ID to the dependent factory.
- For an external system, use a mock or contract stub.

M2M, closure-table, password-hashing, soft-delete, and similar invariants stay behind the domain factory. Setup and cleanup must preserve the same invariants. A create-focused API case exercises HTTP create; other cases normally seed through the shared capability rather than duplicating business creation through HTTP.

An adapter may await an in-process async factory. A live-server adapter must use a confirmed HTTP transport or a bounded isolated worker; it must not plan a shared ORM runtime or an in-thread event-loop bridge. If no safe live-server boundary is known, Codegen Readiness is `not_ready`.

When no safe factory or service path exists, prefer proposing a shared capability, then compose real application operations or reuse invariant-preserving helpers. Test-side invariant copies, guarded seed entry points, HTTP setup, and direct leaf-table insertion are progressively weaker options and require the plan to expose the tradeoff for review. Direct insert is acceptable only for a true leaf with no derived invariants.

Reference runtime settings through `tests.config.settings`; do not hardcode URLs, credentials, or data prefixes. Planning may inspect existing test code for naming and capability evidence, but discovered candidates are not confirmed capabilities until the knowledge layer authorizes them.

Plan Readiness describes whether the documents are complete and reviewable. Codegen Readiness is stricter: it also requires confirmed endpoint/auth/schema behavior, feasible setup and cleanup, a formal knowledge file, and resolvable capability leaves. Human chat approval does not replace the review artifact or capability gate.
