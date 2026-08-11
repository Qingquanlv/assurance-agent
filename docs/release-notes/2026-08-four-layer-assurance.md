# Release notes — Four-layer assurance verification (2026-08)

## Summary

This release activates v6 runtime bindings, declared-only assurance inputs, generated-file authority, durable healing effects, legacy resume narrowing, audited supersede exits, multi-layer `codegen-only`, D17/D18 evidence export, and hard benchmark gates on current-chain metrics.

## Schema IDs

Operators and integrators should recognize these exact semantics identifiers:

| ID | Role |
|---|---|
| `plan_gate_semantics/v1` | Plan-gate semantics object |
| `historical_topology_safety/v1` | Topology-safety semantics object |
| `runtime_commit_safety/v1` | Commit-safety / validator / effect inventory |
| `selection_normalizer/v1` | Eval layer-selection normalizer |
| `write_policy/v1` | Content-bound write policy for eval attempts |

## Six v6 semantic fields

Every v6 root/child projection binds these six fields (digests + object IDs):

- `gate_semantics_digest`
- `gate_semantics_object_id`
- `topology_safety_semantics_digest`
- `topology_safety_semantics_object_id`
- `commit_safety_semantics_digest`
- `commit_safety_semantics_object_id`

v1–v5 roots remain parseable/displayable. Topology receipts prove topology safety only; they are **not** commit-safety proof. Remaining commit-safety-bearing work on unbound legacy roots blocks with `legacy_commit_safety_semantics_unbound`.

## Validator IDs

Precommit / healing candidate authority uses exactly these validator IDs:

- `generated_files_candidate/v1`
- `codegen_fix_candidate/v1`

Generated-file manifests (not summaries) are authoritative for selected-test credit. Summary-only artifacts earn zero selected-write credit.

## Durable effect kinds

Exactly three durable effect kinds are registered:

- `healing_allocation/v2`
- `fixer_proposal_approved/v1`
- `heal_record_apply/v2`

Effects survive success/commit/domain/ack cuts and use independent due-time retry state.

## Legacy resume narrowing and supersede

- Report-only / terminal-only legacy work may continue.
- Pending assurance commit work on unbound legacy roots stops with `legacy_commit_safety_semantics_unbound`.
- `aa workflow supersede` is the sole audited operator exit, with actions `rerun-v6` (one replacement root) and `stop` (terminal disposition only).
- Imported-codegen healing is intentionally narrowed: imported codegen roots are not auto-healed (`unverified_imported_codegen`). Non-imported API/E2E codegen can bind ready fixer authority from committed `generated_files_candidate/v1` write sets on the packaged allocate path; a full packaged GraphRuntime E2E healing drive (approval → intent → record) is covered by unit/integration operation proofs, not a single end-to-end healing matrix run.

## Multi-layer codegen-only and selection

- Selected layers are the four values `api`, `e2e`, `fuzz`, `performance`.
- Default selection (when suite keys are absent) is `api` + `e2e`, normalized by `selection_normalizer/v1`.
- Every non-empty subset is supported; write policy allows only the current change, selected private roots, shared testdata, and graph locks/publications.
- Multi-layer `codegen-only` is first-class (not single-layer-only).

## Declared-only inputs

Assurance agents use declared-only isolation: attempt-bound `input_snapshot` / `runtime_context` supply exact reads. Ambient host/coordinator paths are not part of the agent input surface.

## Evidence export (D17/D18)

Eval attempts persist an `ExecutionEvidenceV1` envelope plus an `evidence-export/` closure:

- Root event slice lists events with `export_seq` exactly `1..N` and may gap on `source_seq`.
- Manifest closure includes definition bindings, gate/topology/commit-safety semantics, input snapshots, validation receipts, write sets, and blobs needed for replay.
- Scorers replay `write_policy/v1` and D17 change-location evidence before awarding current-chain credit.

## Hard benchmark metrics

Workflow-codegen suites (`workflow-api-codegen`, `workflow-e2e-codegen`, `workflow-fuzz-codegen`, `workflow-performance-codegen`) consume only `L2-*-codegen-pending` fixtures and hard-gate:

| Metric | Gate |
|---|---|
| `current_assurance_chain_rate` | hard `gte` `1.0` |
| `current_codegen_attempt_rate` | hard `gte` `1.0` |
| `selected_test_write_rate` | hard `gte` `1.0` |

Regression policy is `higher_is_better` with `max_regression: 0.0`.

## Out of CI proof

Third-party sandbox enforcement beyond tested OpenCode configuration/request binding is outside CI proof. Documentation does not promise stronger isolation than the tested adapter surface.
