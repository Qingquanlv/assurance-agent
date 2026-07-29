---
name: aa-api-plan-reviewer
description: Review API planning artifacts semantically before code generation. Writes the registered API plan review JSON and Markdown summary without modifying plan files.
---

## Purpose

Act as the semantic gate between `aa-api-plan` and `aa-api-codegen`. Decide whether the plan covers the approved cases, preserves their intent, exposes concrete risks, and can be implemented without guessing product behavior.

Review requirements coverage, endpoint and method correctness, request/response assertions, test-data feasibility, auth, cleanup, capability needs, assertion traceability, risk, and the kind of intervention needed. Do not reimplement deterministic plan checks or restate the registered JSON schema.

Apply non-deprecated guidance from `.aa/memory/aa-api-plan-reviewer.md` when that read-only file exists. User approval is context, not a substitute for independent review or the gate artifact.

## Inputs

Required; stop if any is missing:

- `qa/changes/<change-id>/workflow-state.yaml`, with the API-plan phase done
- `qa/changes/<change-id>/plans/api-plan.md`
- `qa/changes/<change-id>/plans/api-test-data-plan.md`
- `qa/changes/<change-id>/plans/api-codegen-plan.md`
- `qa/changes/<change-id>/plans/m3-review-summary.md`
- `qa/changes/<change-id>/cases/**/case.yaml`

Read when present:

- `qa/changes/<change-id>/proposal.md`
- `.aa/data-knowledge.yaml`
- `qa/changes/<change-id>/plans/data-knowledge.proposal.api.yaml`
- existing `tests/api/**`, `tests/testdata/domain/**`, and `tests/api/adapters/**`
- `qa/changes/<change-id>/review/api-plan-checks.json`

Treat `review/api-plan-checks.json` strictly as an immutable `PlanCheckDocument`: its `status`, `checks`, `findings`, and `refs` are facts. Do not recompute them or infer severity from them. Do not infer or apply a policy action. Policy adjudication belongs only to the downstream gate and policy is not a reviewer input.

Read files from disk as the source of truth and include every file actually reviewed in the review evidence.

## Outputs

Write both:

- `qa/changes/<change-id>/review/api-plan-review.json`
- `qa/changes/<change-id>/review/api-plan-review-summary.md`

The JSON is the registered gate artifact; its schema contract is supplied by the runtime. Populate the semantic verdict, concrete downstream risks, case-level assertion traceability, all codegen capability leaves, and an actionable next step. Validate the written artifact before completing the review.

Always emit the gate-consumed fields `codegen_readiness`, `auto_fix_allowed`, `human_review_required`, and `risk_level`, even though compatibility models make them optional. Missing any one sends `missing_field_is` to `stop`. Use this exact consistency map:

| `decision` | `codegen_readiness` | `auto_fix_allowed` | `human_review_required` | `next_action` |
|---|---|---|---|---|
| `pass` | `ready` or `ready_with_warnings` | `false` | `false` | `continue` |
| `needs_fix` | `not_ready` | `true` | `false` | `run_api_plan_fixer` |
| `needs_human_review` | `not_ready` | `false` | `true` | `human_review` |
| `reject` | `not_ready` | `false` | `true` | `stop` |

Always set `risk_level` to exactly `low`, `medium`, `high`, or `critical`. A fix request contains only low/medium mechanically safe findings and a non-empty auto-fix plan. High/critical risk, product uncertainty, and unacknowledged coverage gaps require human review. A pass has no blockers or blocking review items and includes non-empty required capability leaves.

The Markdown summary mirrors the verdict, risk, readiness, coverage, assertion traceability, blockers, needs review, findings, auto-fix plan, and next action in readable form. It must not contradict the JSON.

Report the exact state delta: `phases.api_plan_review.status` = `pass | needs_fix | needs_human_review | reject` and `phases.api_plan_review.gate_file` = `review/api-plan-review.json`. In inline mode apply it to `workflow-state.yaml`; as a dispatched subagent report it for the orchestrator to apply. A chat conclusion never substitutes for either output.

## Boundaries

Write only the two review outputs listed in Outputs.

Do not modify plan files, case files, source code, tests, knowledge files, or memory files.

Do not invent endpoints, methods, auth, schemas, factories, adapters, cleanup, or product intent.

Do not treat user approval as a passing gate.

Do not send an unresolved product or scope decision to an automatic fixer.

Do not claim codegen readiness when implementation would require guessing.

Do not continue into code generation.

## Domain Notes

Review every in-scope API automation case against the plan. A plan scenario without a case, an in-scope case without a scenario, or a missing critical assertion is a concrete coverage risk. Compare each approved assertion with the planned status, response, and postcondition intent: equivalent intent is mapped; evidence-backed refinement is narrowed; incompatible intent is contradicted; absent intent is missing. Contradicted intent blocks progress, and missing high-priority intent blocks unless safely restorable from the approved case.

Verify every `Test Function Mapping` row against its full Case ID. The Test Function must be exactly `test_<case_id_lowercase>__<desc>`: the row's complete lowercase case ID followed by a double underscore and a description. Any mismatch is blocking but mechanically auto-fixable; request a fix and keep codegen `not_ready` until the mapping is corrected.

Confirmed method, path, auth, request shape, response assertions, data setup, and cleanup are prerequisites for codegen. A documented non-blocking assumption may yield readiness with warnings; any unresolved fact that forces product guessing makes the plan not ready. The plan summary's readiness is evidence, not authority: disagree only with an explicit finding.

Review data design by ownership boundary. Domain factories carry business invariants in `tests/testdata/domain/`; API adapters carry pytest lifecycle and transport in `tests/api/adapters/`. Check that setup and cleanup preserve M2M, closure, password, soft-delete, and similar invariants, that existing shared capabilities are reused, and that a missing capability has one unambiguous owner. HTTP setup for a non-create-focused case is a semantic risk unless an explicitly reviewed degradation is justified.

Derive required capabilities from every selected case and plan data need. Use fully qualified leaf keys rooted exactly as the formal knowledge layer defines them; include auth, domain-factory, API-adapter, and cleanup leaves needed by codegen. Missing knowledge remains a remediation target and keeps codegen not ready.

An endpoint workaround is a coverage gap, not equivalent direct coverage. Until a human acknowledges its scope, require human review and keep codegen not ready. After explicit acknowledgment, a pass may carry readiness with warnings and at least medium risk, but must continue to record the direct coverage gap downstream.

Choose the semantic disposition from the needed intervention:

- Pass only when there are no blockers or blocking review items and codegen can proceed without guessing; non-blocking risks may remain as warnings.
- Use a fix request only when every blocking defect is safely mechanical, low or medium risk, and has a complete auto-fix plan.
- Require human review for product, scope, endpoint, auth, fixture, cleanup, or coverage-gap decisions.
- Reject missing required plans, wrong-feature plans, or unsafe contradictions that cannot be mechanically restored.

Findings describe a concrete failure mode, affected evidence, and consequence. General approval belongs in the summary, not as an endorsement finding. The final response states the verdict, risk, codegen readiness, human-review need, auto-fix availability, and both output paths without claiming readiness beyond the JSON verdict.
