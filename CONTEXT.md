# Assurance Agent

Deterministic QA workflow automation for a target project: changes, evidence, gates, healing, and archive.

## Language

**Change**:
A named unit of QA work identified by a filesystem-safe `change_id`. While active it lives under the configured changes directory; after archive it lives under the configured archive directory.
_Avoid_: ticket, request, run (a run is an execution batch inside a change)

**ChangeLocation**:
The resolved absolute directory of a Change plus its source role (`changes` or `archive`).
_Avoid_: ChangeContext (reserved if we later mean schema+state+params), path tuple

**Active change**:
A Change whose directory exists under the configured `qa.changes` path and is eligible for write commands and the driver.
_Avoid_: unarchived (retro evidence-lifecycle wording; map at the retro adapter)

**Archived change**:
A Change whose directory exists under the configured `qa.archive` path; read-oriented evidence source for retro and history sampling.
_Avoid_: completed change (completion is a workflow status, not a directory role)

**Retro evidence slice**:
An immutable, typed, run-scoped projection of Issue, Workflow, or Eval evidence for one resolved Retro window. It is analysis input, not a new Ledger and not historical Retro output.

**Retro signal**:
A domain analyzer's validated pattern claim whose references resolve only inside its evidence slice. Runtime code supplies the slice digest; analyzers do not author integrity fields.

**Retro context**:
The mechanically assembled v3 envelope containing the three domain statuses, source manifest, integrity result, and all validated signals for one Retro run. Improvement Candidates must reference both context signals and immutable source evidence.

**Ledger**:
The append-only event log for a Change (`events.jsonl`), queried through one interface for sequence-aware lookups (filter / latest by type and attributes).
_Avoid_: event store, event bus, audit log (when meaning the query seam over `events.jsonl`)

**Fan-out item**:
One element of a phase-level `fan_out.each` list; the engine expands the base phase into a child phase `<base>[<item>]` per item at projection time, with `{item}` templating in `produces`.
_Avoid_: shard, batch (a run batch is an execution concept)

**Loop kind**:
A registered projector for a `loops:` entry (`healing`, `review_fix`), consumed by the engine only through the unified `LoopSnapshot` protocol.
_Avoid_: hardcoded loop, special-case loop

**Checkpoint**:
A driver main-loop boundary (one committed phase outcome or control action); the checkpoint payload is `workflow-state.yaml` + `events.jsonl`, `driver.json` is only the non-authoritative process pointer (`invocation_id` / `checkpoint_id` / `event_seq`).
_Avoid_: snapshot, savepoint (recovery is re-projection, not snapshot restore)

**Runtime assembly**:
The single composition root (`assemble_graph_runtime`) that wires TreeStore, CheckpointStore, WorkspaceBackend, NodeRunner, Scheduler, and GraphRuntime for one Change. Production loads and compiles the schema before calling it; tests pass an inline CompiledWorkflow. The only substitution point is `build_node_runner` (or the default path via `adapter` + `operations`).
_Avoid_: test fixture, builder helper (implies a per-caller copy is acceptable)

## Evidence coverage gate (Task 9)

`QualityGateResult.dimensions.coverage.status` is adjudicated solely from `EvidenceCoverageEvaluation` (trace projection × `policy.evidence_sufficiency`), not line/branch thresholds. Legacy `CoverageDimension` numeric fields remain for artifact compatibility but do not affect status. `exec_config.coverage.gate_mode` is deprecated (D3).

**Golden attribution (vue-fastapi-admin eval-sample-001, default `on_insufficient: require_human`):**

| Scenario | Old verdict | New verdict | Reason |
|---|---|---|---|
| Line coverage PASS, required API case never executed | PASS_WITH_WARNINGS (`gate_mode: warn`) | FAIL | `never_run` / `execution_recent` insufficient under `require_human` |
| Line coverage PASS, required case uncovered in tests tree | PASS | FAIL | `uncovered` / `covered` kind missing |

Migration: set project `.aa/policy.yaml` `evidence_sufficiency.on_insufficient: warn` to soften evidence gaps to `PASS_WITH_WARNINGS` (cannot soften missing projection or policy errors).

## `aa verify` (Task 12)

Read-only reconciled-phase verdict: `fold_trace(..., phase="reconciled")` then `evaluate_sufficiency(..., as_of=aware UTC now)`. Verdict order: blocking gaps or `integrity == incomplete` → fail; non-empty `open_problem_ids` → fail; insufficient × `on_insufficient` (`block`→fail, `require_human`→needs_human, `warn`→pass+warnings); all sufficient → pass with scope (`cases`, `batch`, `policy_digest`, `projection_digest`). `VERIFY_BLOCKING_GAP_CODES` fail-closed even when `on_insufficient: warn`; `mapped_test_missing_from_tree` is sufficiency-only. Exit codes: pass=0, needs_human=30, fail=40.
