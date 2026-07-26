# Issue Lifecycle and Problem Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Markdown known-product-issue side channel with immutable Change observations/occurrences, a project-level Problem lifecycle Ledger, embedded issue analysis after every authoritative execution batch, and a non-blocking human review workflow.

**Architecture:** Deterministic operations collect evidence and reconcile canonical state; LLM nodes write only non-canonical candidates/advice. Two strict append-only Ledgers rebuild Change and project projections. The graph runtime gains typed post-retry recovery and synchronized project resources so analyzer failures can continue visibly and concurrent Changes cannot create duplicate Problems.

**Tech Stack:** Python 3.11+, Pydantic v2, Click, YAML workflow schema v2, append-only JSONL, `fcntl` advisory locks, the existing GraphRuntime/TreeStore/write-set engine, pytest, and scripted fake agent adapters.

## Global Constraints

- The approved design is [2026-07-25-issue-lifecycle-design.md](/Users/lvqingquan/agent/assurance-agent/docs/superpowers/specs/2026-07-25-issue-lifecycle-design.md). If code and this plan disagree with it, the design wins unless a new decision is recorded first.
- Work test-first. Each checklist item that adds behavior begins with the named failing test, runs that exact test to observe the expected failure, implements the minimum production change, and reruns it.
- Change Issue events and Project Problem events are canonical. `inspect/issue-candidates.json`, analyzer advice, projections, reports, and review queues are never canonical lifecycle state.
- A single candidate batch is semantically validated before any event is appended. No partial occurrence/problem acceptance is permitted.
- An Issue never changes execution `final_status` and never blocks archive. Incomplete analysis or open Problems affect only Issue risk, archive status wording, and report content.
- Only exact versioned fingerprints auto-link. Semantic similarity creates review suggestions and never mutates Problem identity.
- `known-product-issues.md` and `known-product-issues.json` are neither generated nor read on the new path. Existing archived copies are ignored. The legacy `known_product_issue` execution-classifier enum may remain as an Observation input label; it must have no filesystem or lifecycle authority.
- `.aa/data-knowledge.yaml` remains behind the existing validated retro proposal/acceptance boundary. Issue collection, reconciliation, review, report, archive, and risk code must not write it.
- Preserve unrelated worktree changes. Stage and commit only the files named by the current task.
- Use canonical JSON (`sort_keys=True`, compact separators for digest inputs, one trailing newline for materialized documents) and UTC RFC 3339 timestamps supplied by an injectable clock.
- Run focused tests after each task. Run the complete verification matrix only in Task 17.

---

## Task 1: Add typed recovery syntax and compiler validation

**Files:**

- Modify: `assurance_agent/workflow/graph/schema_v2.py`
- Modify: `assurance_agent/workflow/graph/compiler.py`
- Modify: `assurance_agent/workflow/graph/models.py`
- Test: `tests/unit/workflow/graph/test_schema_v2.py`
- Test: `tests/unit/workflow/graph/test_compiler.py`

- [ ] **Step 1: Write schema parsing tests for `recover`**

Add cases proving that a node accepts a typed recovery declaration and that actions/errors are non-empty and unique:

```python
def test_node_accepts_typed_recovery_route() -> None:
    node = NodeDef.model_validate(
        {
            "uses": "skill:aa-issue-analyzer",
            "recover": {
                "errors": ["timeout", "transport", "rate_limit", "invalid_output"],
                "via": "record-analysis-failure",
                "continue_to": "inspect-complete",
            },
        }
    )
    assert node.recover is not None
    assert node.recover.errors == ["timeout", "transport", "rate_limit", "invalid_output"]
```

Also reject an empty error list, duplicate errors, and unsupported error kinds.

- [ ] **Step 2: Run the schema tests and observe the missing-field failure**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_schema_v2.py -q
```

Expected: FAIL because `NodeDef` forbids the undeclared `recover` field.

- [ ] **Step 3: Add the frozen recovery model**

Implement in `schema_v2.py`:

```python
class RecoverDef(_FrozenModel):
    errors: list[ErrorKind] = Field(min_length=1)
    via: str
    continue_to: str

    @model_validator(mode="after")
    def unique_errors(self) -> "RecoverDef":
        if len(set(self.errors)) != len(self.errors):
            raise ValueError("recover.errors must be unique")
        return self


class NodeDef(_FrozenModel):
    # existing fields remain unchanged
    recover: RecoverDef | None = None
```

Import `ErrorKind` from `workflow.core.graph_types`; do not duplicate the literal list.

- [ ] **Step 4: Write compiler tests for recovery topology**

Cover all of these rules in `test_compiler.py`:

- `via` names a node in the same graph;
- `continue_to` names a node or `END`/`STOP`/`FAIL`;
- `via` differs from the recovering node;
- the dedicated recovery node has no ordinary incoming edge/route and no ordinary outgoing edge/route;
- a recovery error is also allowed by the node target's execution contract `retryable_errors`;
- recovery adjacency makes the fallback and continuation reachable for compiler reachability checks;
- a subgraph containing recovery compiles to the same digest twice.

- [ ] **Step 5: Run compiler tests and observe unknown references/unreachable nodes**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_compiler.py -q
```

Expected: FAIL because compiler adjacency and reference validation do not inspect recovery routes.

- [ ] **Step 6: Compile recovery metadata**

Extend `_validate_graph_refs()`, `_graph_adjacency()`, `_check_reachability()`, and contract validation. Store the validated declaration on `CompiledNode` through its existing `definition` field; do not add a second mutable representation.

The compiler must reject `forbidden_write`, `contract`, and `internal` recovery on `skill:aa-issue-analyzer` when those kinds are absent from its contract whitelist. Recovery is evaluated only after the normal retry policy is exhausted.

- [ ] **Step 7: Run the focused graph schema/compiler suite**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_schema_v2.py tests/unit/workflow/graph/test_compiler.py tests/unit/workflow/graph/test_canonical_schema_v2.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit the syntax/compiler slice**

```bash
git add assurance_agent/workflow/graph/schema_v2.py assurance_agent/workflow/graph/compiler.py assurance_agent/workflow/graph/models.py tests/unit/workflow/graph/test_schema_v2.py tests/unit/workflow/graph/test_compiler.py
git commit -m "feat(graph): compile typed recovery routes"
```

---

## Task 2: Make recovery durable in graph events, projection, and planning

**Files:**

- Modify: `assurance_agent/workflow/core/graph_events.py`
- Modify: `assurance_agent/workflow/graph/models.py`
- Modify: `assurance_agent/workflow/graph/checkpoint.py`
- Modify: `assurance_agent/workflow/graph/planner.py`
- Test: `tests/unit/workflow/graph/test_checkpoint.py`
- Test: `tests/unit/workflow/graph/test_planner.py`
- Test: `tests/integration/test_graph_runtime_faults.py`

- [ ] **Step 1: Write strict-event and fold tests**

Add a `task_recovery_routed` event test with these invariants:

- it references an existing failed task and the same node/generation;
- only one recovery route may be recorded for that task failure;
- duplicate identical ledger bytes remain an integrity error under the existing strict progression rules;
- projection records the recovery route without rewriting the failed attempt.

Use this model shape:

```python
class TaskRecoveryRoutedEvent(_GraphEvent):
    type: Literal["task_recovery_routed"]
    invocation_id: str
    checkpoint_ns: str
    graph_id: str
    node_id: str
    generation_ordinal: int
    task_id: str
    error_kind: ErrorKind
    message: str
    via: str
    continue_to: str
```

- [ ] **Step 2: Run the checkpoint test and observe adapter rejection**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_checkpoint.py -q
```

Expected: FAIL because `GRAPH_EVENT_ADAPTER` does not know `task_recovery_routed`.

- [ ] **Step 3: Add the event and projection**

Add the event to `GraphEvent`, `__all__`, and `fold_invocation_events()`. Add a frozen projection:

```python
class RecoveryProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str
    node_id: str
    generation_ordinal: int
    error_kind: ErrorKind
    message: str
    via: str
    continue_to: str


class GraphProjection(BaseModel):
    # existing fields
    recoveries: dict[str, RecoveryProjection] = Field(default_factory=dict)
```

Key `recoveries` by source `task_id`. Fold validates that the referenced latest attempt is `failed` with the same `error_kind` and message; it never marks that attempt succeeded. Add a frozen `RecoveryContext` to `ExecutableTask` so the dedicated fallback operation receives source task/node, error kind/message, attempt count, and pinned recovery event without scraping Ledger JSON.

- [ ] **Step 4: Write planner tests for retry exhaustion and hard failures**

Test the sequence precisely:

1. retryable analyzer error with remaining attempts schedules the same task ID again;
2. after exhaustion, an allowed error emits `task_recovery_routed` and schedules `via`;
3. `via` success routes only to `continue_to`;
4. normal success routes through normal outgoing edges and never runs `via`;
5. `forbidden_write`, `contract`, and corrupt-ledger failures remain terminal;
6. crash after persisting recovery event but before starting `via` replays one fallback task;
7. fallback task input contains the frozen typed error kind/message;
8. retry/crash never commits the failed analyzer write-set.

- [ ] **Step 5: Run planner tests and observe terminal failure**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_planner.py -q
```

Expected: FAIL because `_seed_outcomes()` currently turns exhausted failure directly into graph failure.

- [ ] **Step 6: Implement recovery delivery in the pure planner**

Refactor `_seed_outcomes()` to return recovery events/deliveries alongside normal outcomes. Keep these rules explicit:

```python
if exhausted and definition.recover and error_kind in definition.recover.errors:
    recovery = projection.recoveries.get(latest.task_id)
    if recovery is None:
        strict_events.append(
            TaskRecoveryRoutedEvent(
                type="task_recovery_routed",
                invocation_id=projection.invocation_id,
                checkpoint_ns=projection.checkpoint_ns,
                graph_id=graph.graph_id,
                node_id=nid,
                generation_ordinal=latest.generation_ordinal or 0,
                task_id=latest.task_id,
                error_kind=error_kind,
                message=latest.error or "task failed",
                via=definition.recover.via,
                continue_to=definition.recover.continue_to,
            )
        )
    recovery_deliveries[definition.recover.via] = definition.recover.continue_to
    outcomes[nid] = _Outcome(status="unresolved", task=latest)
    continue
```

The failed source never traverses its normal outgoing edges. The dedicated `via` node is activated with `ExecutableTask.recovery` populated from the frozen event; once it succeeds, its frozen `continue_to` is delivered. Event IDs and task IDs remain structural and deterministic.

- [ ] **Step 7: Add an end-to-end crash/resume fault test**

Use the scripted failing handler in `test_graph_runtime_faults.py`: fail analyzer twice with `timeout`, persist the recovery event, simulate restart, then assert exactly one fallback execution and a completed graph. A `forbidden_write` variant must exit failed.

- [ ] **Step 8: Run recovery tests**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_checkpoint.py tests/unit/workflow/graph/test_planner.py tests/integration/test_graph_runtime_faults.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit durable recovery**

```bash
git add assurance_agent/workflow/core/graph_events.py assurance_agent/workflow/graph/models.py assurance_agent/workflow/graph/checkpoint.py assurance_agent/workflow/graph/planner.py tests/unit/workflow/graph/test_checkpoint.py tests/unit/workflow/graph/test_planner.py tests/integration/test_graph_runtime_faults.py
git commit -m "feat(graph): route exhausted typed failures through recovery"
```

---

## Task 3: Generalize interrupt actions and carry audited review payloads

**Files:**

- Modify: `assurance_agent/workflow/graph/schema_v2.py`
- Modify: `assurance_agent/workflow/graph/models.py`
- Modify: `assurance_agent/commands/workflow_cmd.py`
- Test: `tests/unit/workflow/graph/test_schema_v2.py`
- Test: `tests/unit/workflow/graph/test_resume_v3.py`
- Test: `tests/integration/test_cli_workflow_v2.py`

- [ ] **Step 1: Add failing tests for arbitrary declared actions**

Prove that `InterruptDef.actions` accepts issue actions such as `confirm_assessment`, `mark_not_an_issue`, `accept_risk`, `start_work`, `confirm_link`, `merge`, `reopen`, and `submit_resolution`, while rejecting empty/duplicate/blank values. Preserve compiler enforcement that every action has a route.

- [ ] **Step 2: Add failing CLI tests for structured resume payload**

Exercise:

```bash
aa workflow resume --change CH-1 \
  --interrupt INT-1 \
  --action confirm_assessment \
  --reason "triaged from execution evidence" \
  --payload '{"expected_problem_version":2,"classification":"product_bug","severity":"high","evidence_refs":["OCC-1"]}'
```

Assert invalid JSON/non-object payload exits with `EXIT_ERROR`; valid JSON is copied into `GraphResumedEvent.payload` unchanged.

- [ ] **Step 3: Run tests and observe Click choice/model rejection**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_schema_v2.py tests/unit/workflow/graph/test_resume_v3.py tests/integration/test_cli_workflow_v2.py -q
```

Expected: FAIL because actions are fixed Literals/Click choices and the CLI has no `--payload` option.

- [ ] **Step 4: Generalize the models without weakening runtime validation**

Change `InterruptDef.actions` to validated `list[str]` and `ResumeCommand.action` to a validated non-empty `str`. Keep `GraphRuntime.resume()` as the authority that rejects actions not declared by the pending interrupt.

Reuse `_parse_params()` through a generic `_parse_json_object(raw, option_name)` helper, add `--payload`, and construct:

```python
ResumeCommand(
    interrupt_id=interrupt_id,
    action=action,
    reason=reason.strip(),
    who=resolved_who,
    payload=_parse_json_object(payload, "--payload"),
)
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_schema_v2.py tests/unit/workflow/graph/test_resume_v3.py tests/integration/test_cli_workflow_v2.py -q
```

Expected: PASS, including rejection of an action absent from `pending.actions`.

- [ ] **Step 6: Commit generic interrupts**

```bash
git add assurance_agent/workflow/graph/schema_v2.py assurance_agent/workflow/graph/models.py assurance_agent/commands/workflow_cmd.py tests/unit/workflow/graph/test_schema_v2.py tests/unit/workflow/graph/test_resume_v3.py tests/integration/test_cli_workflow_v2.py
git commit -m "feat(graph): support audited domain interrupt actions"
```

---

## Task 4: Make synchronized project resources safe across Change processes

**Files:**

- Modify: `assurance_agent/workflow/core/graph_types.py`
- Modify: `assurance_agent/workflow/graph/contracts.py`
- Create: `assurance_agent/workflow/graph/project_locks.py`
- Modify: `assurance_agent/workflow/graph/workspace.py`
- Modify: `assurance_agent/workflow/graph/scheduler.py`
- Modify: `assurance_agent/workflow/driver/runtime_factory.py`
- Test: `tests/unit/workflow/graph/test_contracts.py`
- Test: `tests/unit/workflow/graph/test_workspace.py`
- Test: `tests/unit/workflow/graph/test_scheduler.py`
- Create: `tests/integration/test_synchronized_project_resources.py`

- [ ] **Step 1: Add contract tests for synchronized paths**

Add `synchronized: tuple[str, ...]` to `ExecutionContract` and `ResourceClaims`. Validate that every synchronized claim:

- uses the `project:` root;
- is covered by the contract's reads; when the target can write that path, it is also covered by writes and `authorization_writes`;
- is a concrete file or directory-prefix pattern ending in `/**`;
- is paired with at least one `project:*` exclusive token.

This contract is valid:

```yaml
reads: [project:qa/issues/**]
writes: [project:qa/issues/**]
authorization_writes: [project:qa/issues/**]
synchronized: [project:qa/issues/**]
exclusive: [project:issue-registry]
```

- [ ] **Step 2: Run contract tests and observe unknown-field failure**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_contracts.py -q
```

Expected: FAIL because execution contracts currently forbid `synchronized`.

- [ ] **Step 3: Implement contract parsing and conflict behavior**

Propagate synchronized paths through `claims_for()`. Treat synchronized paths as normal read/write conflicts inside one scheduler and preserve them separately for cross-process handling.

- [ ] **Step 4: Write advisory-lock tests**

Implement `ProjectResourceLockManager` under `qa/.graph-runtime/locks/`, a tree-capture-excluded directory. Lock file names are SHA-256 digests of `(resolved_project_root, exclusive_token)`. Use non-blocking `fcntl` attempts with an injectable clock and bounded timeout; add `conflict` to `ErrorKind` for an exhausted resource acquisition. Tests must prove sorted multi-lock acquisition, timeout classification, release on exception, and mutual exclusion between two processes.

Core interface:

```python
class ProjectResourceLockManager:
    @contextmanager
    def acquire(self, tokens: Sequence[str], timeout_seconds: float) -> Iterator[None]:
        """Acquire sorted project:* tokens with fcntl.LOCK_EX; always release."""
```

- [ ] **Step 5: Write workspace overlay and targeted-apply tests**

Create tests where an invocation tree contains Problem version 1 while the canonical root has version 2. Under a synchronized claim, workspace creation must overlay only `qa/issues/**` from the live root. Freeze/commit must:

- preserve the invocation snapshot for every non-synchronized path;
- use live version 2 as `before_sha256`;
- apply every changed write-set entry (including Change-local outputs) rather than validating/materializing the whole stale tree; synchronized entries validate against the locked live overlay and ordinary entries validate against the invocation base;
- remain idempotent after a partial/replayed apply;
- reject any synchronized write outside the declared prefixes.

Add focused `TreeStore.overlay_synchronized_paths(base_tree_id, project_root, paths) -> str` and `TreeStore.apply_write_sets_to_synchronized_paths(project_root, write_sets, paths) -> None` methods rather than weakening `apply_tree()` globally.

- [ ] **Step 6: Run workspace tests and observe stale-base failure**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_workspace.py -q
```

Expected: FAIL because every task currently materializes `projection.current_tree_id` and `apply_tree()` checks the complete stale tree.

- [ ] **Step 7: Hold locks across refresh, handler execution, and Update**

Integrate the lock manager in `Scheduler.execute()`:

1. derive sorted `project:*` exclusive tokens for the selected wave;
2. acquire them before any synchronized workspace is created;
3. overlay live synchronized paths after lock acquisition;
4. run/freeze tasks against the overlay tree;
5. commit the strict graph checkpoint and targeted write-set materialization while still holding the lock;
6. release in `finally` on success, failure, retry, interrupt, or crash recovery.

Do not acquire filesystem locks for ordinary Change-local claims. Do not release the project lock immediately after the handler; that recreates the stale-write race before `_commit_wave()`. A lock timeout is persisted as the current task attempt's `conflict` failure so the declared retry policy and eventual recovery route, rather than an out-of-band scheduler exception, control progress.

- [ ] **Step 8: Add the two-Change exact-update infrastructure test**

Spawn two processes, each with a different `qa/changes/<id>` object store and the same project root. Both increment the same synchronized projection version. Assert both events survive, final version is 2, and neither process reports canonical drift.

- [ ] **Step 9: Run synchronized resource tests**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_contracts.py tests/unit/workflow/graph/test_workspace.py tests/unit/workflow/graph/test_scheduler.py tests/integration/test_synchronized_project_resources.py -q
```

Expected: PASS.

- [ ] **Step 10: Commit the cross-process resource foundation**

```bash
git add assurance_agent/workflow/core/graph_types.py assurance_agent/workflow/graph/contracts.py assurance_agent/workflow/graph/project_locks.py assurance_agent/workflow/graph/workspace.py assurance_agent/workflow/graph/scheduler.py assurance_agent/workflow/driver/runtime_factory.py tests/unit/workflow/graph/test_contracts.py tests/unit/workflow/graph/test_workspace.py tests/unit/workflow/graph/test_scheduler.py tests/integration/test_synchronized_project_resources.py
git commit -m "feat(graph): synchronize live project resources across changes"
```

---

## Task 5: Define strict Issue artifact and lifecycle models

**Files:**

- Create: `assurance_agent/artifacts/models/issues.py`
- Modify: `assurance_agent/artifacts/models/__init__.py`
- Modify: `assurance_agent/artifacts/registry.py`
- Create: `tests/unit/artifacts/test_models_issues.py`
- Modify: `tests/unit/artifacts/test_registry.py`

- [ ] **Step 1: Write strict model tests from the approved examples**

Cover `Observation`, `IssueEvidenceManifest`, `IssueCandidate`, `IssueCandidateDocument`, `IssueAnalysisStatus`, `IssueOccurrence`, `ProblemFingerprint`, `ProblemAssessment`, `Problem`, `ChangeIssueSnapshot`, `ProblemProjection`, and `ProblemReviewQueue`.

All canonical models use `ConfigDict(frozen=True, extra="forbid")`. Define shared literals once:

```python
ObservationKind = Literal[
    "test_failure", "warning", "anomaly", "workaround", "coverage_gap",
    "performance_signal", "environment_signal", "review_finding",
]
IssueClassification = Literal[
    "product_bug", "test_bug", "test_data_issue", "environment_issue",
    "coverage_gap", "performance_issue", "workflow_issue", "unknown",
]
IssueSeverity = Literal["critical", "high", "medium", "low"]
ProblemStatus = Literal[
    "detected", "triaged", "in_progress", "verification_pending",
    "resolved", "not_an_issue", "accepted_risk",
]
AssessmentAuthority = Literal["llm_provisional", "human_confirmed"]
```

Tests must reject an occurrence without observations, a candidate referencing no observations, unknown enum values, mutable lifecycle fields on `IssueOccurrence`, and a resolved Problem without a resolution record.

- [ ] **Step 2: Run model tests and observe import failure**

Run:

```bash
uv run pytest tests/unit/artifacts/test_models_issues.py -q
```

Expected: FAIL because `artifacts.models.issues` does not exist.

- [ ] **Step 3: Implement the artifact models**

Use versioned documents. `IssueCandidateDocument` contains the pinned `evidence_bundle_digest`; `IssueAnalysisStatus` records `completed|pending|failed`; `ChangeIssueSnapshot` retains all batches and exposes the latest authoritative batch without deleting prior observations/occurrences. `Problem.version` starts at 1 and changes only through projected events.

- [ ] **Step 4: Register Change-relative artifacts**

Register these exact patterns:

```text
inspect/observations.json
inspect/issue-evidence-manifest.json
inspect/issue-candidates.json
inspect/issue-analysis-status.json
inspect/issue-reconcile-status.json
issues/snapshot.json
```

Do not register `issues/events.jsonl` as a free-form artifact; its dedicated strict adapter owns validation. Project-level `qa/issues/*.json` is outside the Change-relative artifact registry and will be validated by the Issue store.

- [ ] **Step 5: Run artifact tests**

Run:

```bash
uv run pytest tests/unit/artifacts/test_models_issues.py tests/unit/artifacts/test_registry.py tests/unit/artifacts/test_validate.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the artifact contracts**

```bash
git add assurance_agent/artifacts/models/issues.py assurance_agent/artifacts/models/__init__.py assurance_agent/artifacts/registry.py tests/unit/artifacts/test_models_issues.py tests/unit/artifacts/test_registry.py
git commit -m "feat(issues): define strict lifecycle artifacts"
```

---

## Task 6: Implement deterministic IDs, fingerprints, and Problem transitions

**Files:**

- Create: `assurance_agent/workflow/issues/__init__.py`
- Create: `assurance_agent/workflow/issues/identity.py`
- Create: `assurance_agent/workflow/issues/transitions.py`
- Create: `tests/unit/workflow/issues/test_identity.py`
- Create: `tests/unit/workflow/issues/test_transitions.py`

- [ ] **Step 1: Write deterministic identity tests**

Prove that canonical key order, whitespace, title, root-cause prose, confidence, severity, and Change ID cannot alter a Problem fingerprint. Prove that surface kind/value, normalized symptom, behavior qualifiers, or fingerprint version do alter it.

Required public functions are `observation_id(observation_input) -> str`, `occurrence_id(change_id, batch_id, candidate_digest) -> str`, `problem_fingerprint(inputs) -> ProblemFingerprint`, `problem_id(fingerprint) -> str`, and `reconciliation_idempotency_key(change_id, batch_id, candidate_digest) -> str`.

Use `OBS-`, `OCC-`, and `PROB-` plus a stable lowercase digest prefix of a documented length; collision tests use the full stored SHA-256 digest.

- [ ] **Step 2: Run identity tests and observe import failure**

Run:

```bash
uv run pytest tests/unit/workflow/issues/test_identity.py -q
```

Expected: FAIL because the identity module does not exist.

- [ ] **Step 3: Implement canonical normalization and IDs**

Normalize endpoint methods to uppercase, path whitespace/trailing slash deterministically, module names to lowercase tokens, and symptom signatures to lowercase underscore tokens. Reject empty normalized values instead of hashing them.

- [ ] **Step 4: Write the complete transition table tests**

Test every legal edge and representative illegal edges. Include authority rules, required reason/evidence, linked fix, required verification scope, selected authoritative batch, every verification case executed/passed, absence of the fingerprint, regression, merge aliasing, and stale `expected_problem_version`.

Centralize the table:

```python
HUMAN_TRANSITIONS = {
    "confirm_assessment": {"detected": "triaged"},
    "mark_not_an_issue": {"detected": "not_an_issue", "triaged": "not_an_issue"},
    "accept_risk": {
        "triaged": "accepted_risk",
        "in_progress": "accepted_risk",
        "verification_pending": "accepted_risk",
    },
    "start_work": {"triaged": "in_progress", "accepted_risk": "in_progress"},
    "reopen": {"not_an_issue": "detected"},
}
```

Resolution and regression remain dedicated deterministic functions, not entries that a human payload can force.

- [ ] **Step 5: Run transition tests and observe failures**

Run:

```bash
uv run pytest tests/unit/workflow/issues/test_transitions.py -q
```

Expected: FAIL because the transition authority does not exist.

- [ ] **Step 6: Implement transition validation**

Return immutable domain decisions/events. Reject stale versions before evaluating action payload. Never accept `status`, `version`, or `authority` directly from the LLM advice/payload.

- [ ] **Step 7: Run identity/transition tests**

Run:

```bash
uv run pytest tests/unit/workflow/issues/test_identity.py tests/unit/workflow/issues/test_transitions.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit identity and state machine**

```bash
git add assurance_agent/workflow/issues assurance_agent/artifacts/models/issues.py tests/unit/workflow/issues/test_identity.py tests/unit/workflow/issues/test_transitions.py
git commit -m "feat(issues): add deterministic identity and lifecycle transitions"
```

---

## Task 7: Add strict Issue Ledgers and byte-stable projections

**Files:**

- Create: `assurance_agent/workflow/issues/events.py`
- Create: `assurance_agent/workflow/issues/ledger.py`
- Create: `assurance_agent/workflow/issues/projection.py`
- Create: `tests/unit/workflow/issues/test_events.py`
- Create: `tests/unit/workflow/issues/test_ledger.py`
- Create: `tests/unit/workflow/issues/test_projection.py`

- [ ] **Step 1: Write strict event adapter tests**

Define discriminated, `extra="forbid"` unions for the exact event vocabulary:

```text
Change: observation_recorded, issue_analysis_completed, issue_analysis_failed,
        occurrence_detected, occurrence_linked, project_sync_pending
Project: problem_detected, problem_occurrence_linked,
         problem_assessment_confirmed, problem_work_started,
         problem_verification_requested, problem_resolved,
         problem_marked_not_an_issue, problem_risk_accepted,
         problem_reopened, problem_regressed, problem_merge_suggested,
         problem_merged
```

Every envelope includes schema version, contiguous `seq`, event ID, idempotency key, UTC timestamp, evidence digest, and typed payload. Every Project mutation includes `problem_id` and `expected_problem_version` (`0` only for `problem_detected`). Unknown event types or fields must fail the whole read.

- [ ] **Step 2: Run event tests and observe import failure**

Run:

```bash
uv run pytest tests/unit/workflow/issues/test_events.py -q
```

Expected: FAIL because the strict event adapters do not exist.

- [ ] **Step 3: Implement event models and canonical JSONL parsing**

Expose only `CHANGE_ISSUE_EVENT_ADAPTER`, `PROBLEM_EVENT_ADAPTER`, `read_change_issue_events(path)`, and `read_problem_events(path)` from the event module.

Readers reject malformed JSON, blank holes, non-contiguous sequence numbers, duplicate event IDs, and duplicate idempotency keys.

- [ ] **Step 4: Write projection replay tests**

Cover observation/occurrence retention across multiple batches, exact links, semantic suggestions, merge aliases, all lifecycle transitions, stale versions, resolution, accepted risk, not-an-issue, regression, and project sync pending. Replay the same event bytes twice and assert byte-identical `snapshot.json`, `problems.json`, and `review-queue.json`.

- [ ] **Step 5: Run projection tests and observe missing replay functions**

Run:

```bash
uv run pytest tests/unit/workflow/issues/test_projection.py -q
```

Expected: FAIL because projections are not implemented.

- [ ] **Step 6: Implement pure replay**

Keep `project_change_issues(events)`, `project_problems(events)`, `project_review_queue(events)`, and `dump_projection(model)` free of filesystem and clock access.

`problem_merged` leaves source history intact and resolves its canonical alias to the target. An exact new occurrence against a resolved fingerprint yields `problem_regressed` and status `detected`; no replay code invents events.

- [ ] **Step 7: Write append/rebuild tests**

Test append against existing JSONL, idempotent no-op on an already committed idempotency key, atomic all-or-nothing batch serialization, and rebuild from Ledger after projection deletion/corruption. The task workspace is the transaction boundary; the store must not lock or write the host root directly.

- [ ] **Step 8: Implement the store API**

Implement `ChangeIssueStore.append_and_rebuild(events) -> ChangeIssueSnapshot` and `ProjectProblemStore.append_and_rebuild(events) -> tuple[ProblemProjection, ProblemReviewQueue]` as the only mutating store entrypoints.

Write JSONL/projection temp files inside the task workspace and use `os.replace`. Cross-process serialization is supplied by Task 4's synchronized graph resource; do not add a second lock with a shorter lifetime here.

- [ ] **Step 9: Run Ledger/projection tests**

Run:

```bash
uv run pytest tests/unit/workflow/issues/test_events.py tests/unit/workflow/issues/test_ledger.py tests/unit/workflow/issues/test_projection.py -q
```

Expected: PASS.

- [ ] **Step 10: Commit Ledgers and projections**

```bash
git add assurance_agent/workflow/issues/events.py assurance_agent/workflow/issues/ledger.py assurance_agent/workflow/issues/projection.py tests/unit/workflow/issues/test_events.py tests/unit/workflow/issues/test_ledger.py tests/unit/workflow/issues/test_projection.py
git commit -m "feat(issues): add strict ledgers and deterministic projections"
```

---

## Task 8: Collect immutable Observations before any LLM call

**Files:**

- Create: `assurance_agent/workflow/issues/collector.py`
- Create: `assurance_agent/workflow/issues/operations.py`
- Modify: `assurance_agent/workflow/graph/handlers/operation.py`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Create: `tests/unit/workflow/issues/test_collector.py`
- Create: `tests/unit/workflow/issues/test_operations.py`

- [ ] **Step 1: Build fixture-driven collector tests**

Create small in-test execution trees covering:

- failed API/E2E results and raw log evidence;
- warnings, skipped/xfail cases, and runner anomalies;
- coverage gaps and performance signals;
- trace/screenshot/video references;
- workaround declarations and `fact-baseline` anomalies;
- plan/review warnings;
- linked healing apply summaries and their declared Problem verification scope;
- a clean authoritative batch.

Assert every abnormal signal has at least one allowlisted evidence reference and digest, and replay produces the same `OBS-` ID.

- [ ] **Step 2: Run collector tests and observe import failure**

Run:

```bash
uv run pytest tests/unit/workflow/issues/test_collector.py -q
```

Expected: FAIL because the collector module does not exist.

- [ ] **Step 3: Implement deterministic collection**

Use `execution/execution-manifest.yaml` to select the authoritative batch; never pick by directory mtime. Normalize source references, linked healing metadata, and verification-scope evidence, then apply the existing secret-redaction policy before hashing/writing the evidence bundle. Return a value object; do not call an agent.

- [ ] **Step 4: Write operation tests**

`operation:collect-observations` must write, in one task write-set:

```text
change:inspect/observations.json
change:inspect/issue-evidence-manifest.json
change:issues/events.jsonl
change:issues/snapshot.json
```

It appends `observation_recorded` events before analysis and returns `batch_id`, `evidence_bundle_digest`, and `abnormal_count`. Replaying the operation is idempotent. Missing/corrupt authoritative execution evidence remains a hard `invalid_input`/`invalid_output` failure.

- [ ] **Step 5: Run operation tests and observe unregistered target**

Run:

```bash
uv run pytest tests/unit/workflow/issues/test_operations.py -q
```

Expected: FAIL because `default_operations()` has no collector target.

- [ ] **Step 6: Register the operation and contract**

Add a narrow contract that reads selected execution/case/generated-test/fact/review/healing artifacts and writes only the four Change paths above. It must not read or write `qa/issues/**`.

- [ ] **Step 7: Run collector/operation/contract tests**

Run:

```bash
uv run pytest tests/unit/workflow/issues/test_collector.py tests/unit/workflow/issues/test_operations.py tests/unit/workflow/graph/test_contracts.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Observation collection**

```bash
git add assurance_agent/workflow/issues/collector.py assurance_agent/workflow/issues/operations.py assurance_agent/workflow/graph/handlers/operation.py assurance_agent/_resources/schemas/execution-contracts.yaml tests/unit/workflow/issues/test_collector.py tests/unit/workflow/issues/test_operations.py
git commit -m "feat(issues): collect immutable execution observations"
```

---

## Task 9: Add the constrained Issue Analyzer and visible failure operations

**Files:**

- Create: `assurance_agent/_resources/skills/aa-issue-analyzer/SKILL.md`
- Create: `assurance_agent/_resources/skills/aa-issue-triage-advisor/SKILL.md`
- Modify: `assurance_agent/workflow/issues/operations.py`
- Modify: `assurance_agent/workflow/graph/handlers/operation.py`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Test: `tests/unit/workflow/graph/test_task_runner.py`
- Modify: `tests/unit/workflow/issues/test_operations.py`

- [ ] **Step 1: Write analyzer contract tests**

Assert `skill:aa-issue-analyzer`:

- reads only observations, evidence manifest, allowlisted evidence, and project Problem projection/review queue;
- writes only `inspect/issue-candidates.json` and `inspect/issue-analysis-status.json`;
- cannot write either Ledger or either canonical projection;
- retries only `timeout`, `transport`, `rate_limit`, and `invalid_output`.

Assert the triage advisor can write only its review advice artifact and cannot mutate Problems.

- [ ] **Step 2: Run contract tests and observe missing targets**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_contracts.py tests/unit/workflow/graph/test_task_runner.py -q
```

Expected: FAIL because neither packaged skill/contract exists.

- [ ] **Step 3: Write the packaged analyzer skill**

Its output schema must require each candidate to cite one or more Observation IDs. State explicitly that classification, severity, root cause, fingerprint inputs, confidence, recommendations, and possible matches are proposals; status/merge/resolution/human authority fields are forbidden.

- [ ] **Step 4: Write the packaged triage-advisor skill**

It summarizes evidence and recommends one declared human action but does not emit canonical events. It must echo the pinned Problem ID/version and audited evidence digests.

- [ ] **Step 5: Add deterministic empty/failure operations**

Implement and register:

```text
operation:record-empty-issue-analysis
operation:record-issue-analysis-failure
operation:record-project-sync-pending
```

Empty analysis writes a completed zero-candidate document. Exhausted analyzer recovery writes an empty candidate document plus `status: failed` analysis status and appends `issue_analysis_failed` to the Change Issue Ledger; an explicit standalone retry may later replace the noncanonical status for the same evidence digest. Project-sync recovery appends `project_sync_pending`. Each output pins the evidence/candidate digest and is idempotent.

- [ ] **Step 6: Add operation tests for typed failure visibility**

Test `timeout`, `transport`, `rate_limit`, and `invalid_output` mappings. A provider/model-unavailable transport failure materializes analysis reason `unavailable` while retaining graph `ErrorKind: transport`. Assert no fabricated candidates/Problems and no loss of existing Observations.

- [ ] **Step 7: Run analyzer and failure-operation tests**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_contracts.py tests/unit/workflow/graph/test_task_runner.py tests/unit/workflow/issues/test_operations.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit analyzer boundaries**

```bash
git add assurance_agent/_resources/skills/aa-issue-analyzer/SKILL.md assurance_agent/_resources/skills/aa-issue-triage-advisor/SKILL.md assurance_agent/workflow/issues/operations.py assurance_agent/workflow/graph/handlers/operation.py assurance_agent/_resources/schemas/execution-contracts.yaml tests/unit/workflow/graph/test_task_runner.py tests/unit/workflow/issues/test_operations.py
git commit -m "feat(issues): add constrained analysis and failure visibility"
```

---

## Task 10: Reconcile complete candidate batches into Occurrences and Problems

**Files:**

- Create: `assurance_agent/workflow/issues/reconciler.py`
- Modify: `assurance_agent/workflow/issues/operations.py`
- Modify: `assurance_agent/workflow/graph/handlers/operation.py`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Create: `tests/unit/workflow/issues/test_reconciler.py`
- Create: `tests/integration/test_issue_reconcile_concurrency.py`

- [ ] **Step 1: Write all-or-nothing semantic validation tests**

Reject the complete list without writes when any candidate has an unknown Observation, duplicate candidate ID, duplicate deterministic occurrence ID, mismatched evidence digest, incomplete fingerprint inputs, unknown claimed Problem, unsupported enum, or attempted lifecycle authority. Include a valid candidate before the invalid one and assert zero events from both.

- [ ] **Step 2: Run reconciler tests and observe import failure**

Run:

```bash
uv run pytest tests/unit/workflow/issues/test_reconciler.py -q
```

Expected: FAIL because the reconciler does not exist.

- [ ] **Step 3: Implement a pure reconciliation plan**

Validate first, then derive immutable events:

```python
@dataclass(frozen=True)
class ReconciliationPlan:
    change_events: tuple[ChangeIssueEvent, ...]
    problem_events: tuple[ProblemEvent, ...]
    candidate_digest: str


def plan_reconciliation(
    candidates: IssueCandidateDocument,
    observations: ObservationDocument,
    change_snapshot: ChangeIssueSnapshot,
    problems: ProblemProjection,
) -> ReconciliationPlan:
    validated = validate_candidate_batch(candidates, observations, problems)
    return derive_reconciliation_events(validated, change_snapshot, problems)
```

Every validated candidate creates one Occurrence. Exact fingerprint matches link; no exact match creates one `detected` Problem. Semantic possible matches append only `problem_merge_suggested`. Exact recurrence of a resolved Problem emits `problem_regressed`.

The same pure plan evaluates existing `verification_pending` Problems against the later authoritative batch. It emits `problem_resolved` only when a linked fix/disposition exists, every required target/case ran and passed, and the fingerprint is absent. Missing scope keeps the Problem pending. A linked successful healing apply or audited `submit_resolution` review action emits `problem_verification_requested`; it never emits `problem_resolved` directly.

- [ ] **Step 4: Write operation/contract tests**

`operation:reconcile-issues` reads the complete candidate/evidence/change/project state and writes:

```text
change:inspect/issue-reconcile-status.json
change:issues/events.jsonl
change:issues/snapshot.json
project:qa/issues/events.jsonl
project:qa/issues/problems.json
project:qa/issues/review-queue.json
```

Its contract declares `synchronized: [project:qa/issues/**]` and `exclusive: [project:issue-registry]`. It reloads the project projection from the synchronized workspace after lock acquisition.

- [ ] **Step 5: Implement the operation**

Run full semantic validation and construct both event batches before touching any output file. Then append/rebuild both stores inside the same task workspace and return counts/IDs. A successful batch appends `issue_analysis_completed` before its Occurrence events. A semantic batch error writes only a failed `issue-reconcile-status.json` and returns success so the Issue dimension is visibly failed but the workflow may continue; it appends no Occurrence/Problem event. Infrastructure/forbidden-write/corrupt-ledger errors remain typed task failures.

- [ ] **Step 6: Add concurrency and replay tests**

Run two Changes concurrently with the same exact fingerprint. Assert one `problem_detected`, two unique `problem_occurrence_linked` events, two Change Occurrences, correct Problem version, no duplicates after retry/crash, and byte-stable rebuilds.

- [ ] **Step 7: Run reconciliation tests**

Run:

```bash
uv run pytest tests/unit/workflow/issues/test_reconciler.py tests/unit/workflow/issues/test_operations.py tests/integration/test_issue_reconcile_concurrency.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit reconciliation**

```bash
git add assurance_agent/workflow/issues/reconciler.py assurance_agent/workflow/issues/operations.py assurance_agent/workflow/graph/handlers/operation.py assurance_agent/_resources/schemas/execution-contracts.yaml tests/unit/workflow/issues/test_reconciler.py tests/unit/workflow/issues/test_operations.py tests/integration/test_issue_reconcile_concurrency.py
git commit -m "feat(issues): reconcile occurrences into project problems"
```

---

## Task 11: Embed `inspect-with-issues` after every authoritative execution

**Files:**

- Modify: `assurance_agent/_resources/schemas/workflow-schema.yaml`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Modify: `tests/unit/workflow/graph/test_canonical_schema_v2.py`
- Create: `tests/integration/test_issue_lifecycle_workflow.py`

- [ ] **Step 1: Add canonical graph shape tests**

Assert the full workflow topology is:

```text
execution -> inspect-with-issues -> healing -> report
healing.rerun -> inspect-with-issues -> decide
```

Assert `run_tests: false` skips both execution and the Issue subgraph. Assert initial execution and every healing rerun use the same subgraph, not a reduced re-inspect path.

- [ ] **Step 2: Add analyzer success/empty/recovery graph tests**

Use this graph behavior:

```yaml
collect-observations:
  uses: operation:collect-observations
analyze-issues:
  uses: skill:aa-issue-analyzer
  retry: llm-default
  recover:
    errors: [timeout, transport, rate_limit, invalid_output]
    via: record-analysis-failure
    continue_to: inspect-complete
record-empty-analysis:
  uses: operation:record-empty-issue-analysis
reconcile-issues:
  uses: operation:reconcile-issues
  retry: project-sync
  recover:
    errors: [conflict, transport]
    via: record-project-sync-pending
    continue_to: inspect-complete
```

Conditional edges send `abnormal_count == 0` to empty analysis and nonzero counts to the agent. Analyzer success and empty analysis reach reconciliation; exhausted allowed analyzer failure reaches the analysis-failure operation, while exhausted project synchronization reaches the sync-pending operation. Both recovery paths then reach `inspect-complete` visibly.

- [ ] **Step 3: Run workflow tests and observe missing graph**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_canonical_schema_v2.py tests/integration/test_issue_lifecycle_workflow.py -q
```

Expected: FAIL because the assurance/healing graphs still call `operation:inspect` directly.

- [ ] **Step 4: Add the subgraph and wire both call sites**

Keep `operation:inspect` first so current failure analysis/quality gate artifacts remain authoritative. Add an `inspect-complete` side-effect-free join/no-op. Increase `max_supersteps` only by the measured number required by the new nodes; do not use an arbitrary large bound.

- [ ] **Step 5: Prove fail-open and integrity boundaries**

Scripted integration cases must assert:

- observations commit before the analyzer starts;
- analyzer timeout after retries completes full workflow with failed analysis status;
- analyzer `forbidden_write` fails the graph;
- corrupt execution evidence fails before Issue analysis;
- one healing rerun creates a second batch's observations/occurrences;
- Issue outcomes never alter execution manifest/quality-gate `final_status`.

- [ ] **Step 6: Run graph integration tests**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_canonical_schema_v2.py tests/unit/workflow/graph/test_contracts.py tests/integration/test_issue_lifecycle_workflow.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit full-workflow integration**

```bash
git add assurance_agent/_resources/schemas/workflow-schema.yaml assurance_agent/_resources/schemas/execution-contracts.yaml tests/unit/workflow/graph/test_canonical_schema_v2.py tests/integration/test_issue_lifecycle_workflow.py
git commit -m "feat(workflow): analyze issues after every execution batch"
```

---

## Task 12: Add repeatable, non-blocking human Problem review

**Files:**

- Create: `assurance_agent/workflow/issues/review.py`
- Modify: `assurance_agent/workflow/issues/operations.py`
- Modify: `assurance_agent/workflow/graph/handlers/operation.py`
- Modify: `assurance_agent/workflow/graph/schema_v2.py`
- Modify: `assurance_agent/workflow/graph/models.py`
- Modify: `assurance_agent/workflow/graph/runtime.py`
- Modify: `assurance_agent/workflow/driver/driver_state.py`
- Modify: `assurance_agent/workflow/driver/loop.py`
- Modify: `assurance_agent/_resources/schemas/workflow-schema.yaml`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Modify: `assurance_agent/commands/workflow_cmd.py`
- Create: `tests/unit/workflow/issues/test_review.py`
- Modify: `tests/unit/workflow/graph/test_resume_v3.py`
- Create: `tests/integration/test_issue_review_workflow.py`

- [ ] **Step 1: Write pure review validation tests**

Cover every supported action, missing reasons/evidence, stale Problem versions, illegal transitions, merge cycles, unknown occurrence links, missing verification scope, and attempts to set canonical fields directly. Advice is ignored by the apply validator except as display evidence.

- [ ] **Step 2: Run review tests and observe import failure**

Run:

```bash
uv run pytest tests/unit/workflow/issues/test_review.py -q
```

Expected: FAIL because the review module does not exist.

- [ ] **Step 3: Implement deterministic review planning/apply**

Expose `build_problem_review_context(problem_id, projection) -> ProblemReviewContext` and `validate_review_action(context, action, payload, reason, who) -> tuple[ProblemEvent, ...]` as the pure review API.

Bind context to `problem_id`, `expected_problem_version`, audited occurrence/problem event digests, and the current canonical alias. Apply reloads the synchronized Problem projection and rejects stale context.

- [ ] **Step 4: Add review operations and contracts**

Register:

```text
operation:load-problem-review-context
operation:apply-problem-review
```

The load operation writes only noncanonical review context under `change:issue-review/${params.review_id}/`. The advisor writes only advice there. Apply holds `project:issue-registry`, declares `synchronized: [project:qa/issues/**]`, appends typed events, rebuilds Problems/review queue, and writes a Change-local apply receipt.

- [ ] **Step 5: Add a repeatable entrypoint policy**

Extend `EntrypointDef`/`CompiledEntrypoint` with:

```python
restart: Literal["once", "repeatable"] = "once"
```

Move completed-invocation refusal from `evaluate_start_guard()` into `GraphRuntime._start_and_drive()`, where compiled entrypoint policy is available. The driver guard continues to refuse a live PID. `full`, `execute`, `archive`, and `retro` retain `once`; `issue-review`, `issue-analyze`, and `issue-reconcile` declare `repeatable`. A new invocation is still refused while any root invocation for the Change is active.

- [ ] **Step 6: Add the workflow entrypoint**

Add `problem_id` and `review_id` string params with a non-empty allow expression. The review graph is:

```text
load-problem -> triage-advisor -> human-interrupt -> apply-review -> END
```

Every declared interrupt action routes to `apply-review`; `stop` routes to `STOP`. The full workflow has no edge to this graph. `ProblemReviewQueue` deterministically derives `problem_review_requested` items from provisional detected Problems and `problem_merge_suggested` events; the queue item is not a new canonical event type.

Also add two retry entrypoints:

- `issue-analyze` reruns the analyzer against the latest existing Observation/evidence digest and then reconciles on success; it never recollects evidence.
- `issue-reconcile` reruns only `operation:reconcile-issues` for the latest pinned candidate/evidence digest; it is the idempotent retry path for `project_sync_pending` and never calls the LLM.

Add all three Issue entrypoints to `_ENTRYPOINT_CHOICE`.

- [ ] **Step 7: Add CLI/integration tests**

Run two review invocations on the same Change and different Problems. Test stale-version rejection after the first decision, successful post-archive review, unchanged archived `issues/**` digest, and changed project Problem projection only. Test `--payload` reaches apply.

- [ ] **Step 8: Run review tests**

Run:

```bash
uv run pytest tests/unit/workflow/issues/test_review.py tests/unit/workflow/graph/test_resume_v3.py tests/integration/test_issue_review_workflow.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit human review workflow**

```bash
git add assurance_agent/workflow/issues/review.py assurance_agent/workflow/issues/operations.py assurance_agent/workflow/graph/handlers/operation.py assurance_agent/workflow/graph/schema_v2.py assurance_agent/workflow/graph/models.py assurance_agent/workflow/graph/runtime.py assurance_agent/workflow/driver/driver_state.py assurance_agent/workflow/driver/loop.py assurance_agent/_resources/schemas/workflow-schema.yaml assurance_agent/_resources/schemas/execution-contracts.yaml assurance_agent/commands/workflow_cmd.py tests/unit/workflow/issues/test_review.py tests/unit/workflow/graph/test_resume_v3.py tests/integration/test_issue_review_workflow.py
git commit -m "feat(issues): add repeatable human problem review workflow"
```

---

## Task 13: Surface Issue risk in reports and archive without changing execution status

**Files:**

- Modify: `assurance_agent/artifacts/models/report.py`
- Modify: `assurance_agent/artifacts/models/__init__.py`
- Modify: `assurance_agent/workflow/report/report_builder.py`
- Modify: `assurance_agent/_resources/skills/aa-report-generator/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-archive/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-archive/archive-summary-template.md`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Modify: `tests/unit/report/test_report_builder.py`
- Modify: `tests/unit/workflow/graph/test_archive_workflow.py`
- Modify: `tests/unit/artifacts/test_models_inspect_report.py`

- [ ] **Step 1: Write Issue report model and risk tests**

Add a versioned Issue section containing analysis/project-sync status, counts by status/classification/severity, new/repeated/regressed/resolved/accepted-risk/not-an-issue summaries, and `unknown|critical|high|medium|low|clear` Issue risk.

Keep execution risk and Issue risk separate:

```python
class QualityReport(BaseModel):
    schema_version: Literal["1.0", "1.1"]
    final_status: GateStatus
    # existing execution fields
    issues: IssueReport | None = None
```

The writer emits `1.1`. Reading a historical `1.0` report is allowed, but it does not trigger any legacy Issue-file lookup.

- [ ] **Step 2: Run report tests and observe missing field/risk logic**

Run:

```bash
uv run pytest tests/unit/artifacts/test_models_inspect_report.py tests/unit/report/test_report_builder.py -q
```

Expected: FAIL because reports do not consume Issue projections.

- [ ] **Step 3: Implement Issue report derivation**

Load final Change `issues/snapshot.json` and current `qa/issues/problems.json`. Compute:

- `unknown` when analysis or sync is incomplete;
- highest active severity for active statuses;
- `accepted_risk` as active;
- `clear` for no Issues or only resolved/not-an-issue Problems.

Copy `gate.final_status` unchanged into `report.final_status`. Add markdown/executive-summary wording that separately states execution outcome and Issue risk.

- [ ] **Step 4: Write archive behavior tests**

Assert archive copies the entire Change `issues/**` directory, preserves byte digests, and uses `archived_with_warnings` for unknown/open/accepted-risk Issues. A critical open Problem must not stop archive. Clear Issues use the normal archived status. Existing non-Issue precheck gates continue to stop as before.

- [ ] **Step 5: Update report/archive skills and contracts**

The report skill reads structured snapshots/projections. The archive skill reads the Issue report/projection and copies `issues/**`; remove all Markdown-known-issue checks and copy rules. Give report read-only project Problem access; archive may write only `qa/archive/**` and never the Problem Ledger.

- [ ] **Step 6: Run report/archive tests**

Run:

```bash
uv run pytest tests/unit/artifacts/test_models_inspect_report.py tests/unit/report/test_report_builder.py tests/unit/workflow/graph/test_archive_workflow.py -q
```

Expected: PASS, including unchanged execution `final_status`.

- [ ] **Step 7: Commit reporting/archive consumption**

```bash
git add assurance_agent/artifacts/models/report.py assurance_agent/artifacts/models/__init__.py assurance_agent/workflow/report/report_builder.py assurance_agent/_resources/skills/aa-report-generator/SKILL.md assurance_agent/_resources/skills/aa-archive/SKILL.md assurance_agent/_resources/skills/aa-archive/archive-summary-template.md assurance_agent/_resources/schemas/execution-contracts.yaml tests/unit/report/test_report_builder.py tests/unit/workflow/graph/test_archive_workflow.py tests/unit/artifacts/test_models_inspect_report.py
git commit -m "feat(report): separate issue risk from execution outcome"
```

---

## Task 14: Replace archive Markdown history with structured Problems in risk context

**Files:**

- Modify: `assurance_agent/risk/context.py`
- Modify: `tests/unit/risk/test_context.py`
- Modify: `tests/unit/risk/test_advisory.py`

- [ ] **Step 1: Replace Markdown fixture tests with Problem projection tests**

Cover active, resolved, accepted-risk, and not-an-issue Problems. Preserve their distinct semantics:

- active/accepted-risk Problems increase current risk context;
- resolved Problems remain regression signals;
- not-an-issue Problems provide negative classification context;
- merged aliases appear once under the canonical Problem.

- [ ] **Step 2: Run risk tests and observe no structured reader**

Run:

```bash
uv run pytest tests/unit/risk/test_context.py tests/unit/risk/test_advisory.py -q
```

Expected: FAIL because `_merge_historical_issues()` still samples archive-root known-product files.

- [ ] **Step 3: Implement the structured reader**

Delete `_SOURCE_RANK`, `_cap_for_source`, `_parse_known_product_issues_md`, `_collect_issues_from_archive`, and `_merge_historical_issues`. Load/validate `qa/issues/problems.json` once and create evidence entries pinned to its digest and relevant occurrence IDs.

Evolve `HistoricalIssue` to carry `problem_id`, classification, status, severity, affected surface, first/last seen, occurrence count, and evidence ID. Keep `module`/`endpoint` optional only as derived affected-surface conveniences; never parse prose to recover them.

- [ ] **Step 4: Update degraded-mode semantics**

`no_history` means the structured Problem projection is absent/empty. A corrupt projection must be a visible degraded reason, not a silent fallback to archives. Archive sampling remains for pass-rate history only.

- [ ] **Step 5: Run risk tests**

Run:

```bash
uv run pytest tests/unit/risk/test_context.py tests/unit/risk/test_advisory.py tests/integration/test_cli_risk_context.py tests/integration/test_cli_risk_advisory.py -q
```

Expected: PASS with no known-product file fixture.

- [ ] **Step 6: Commit structured risk context**

```bash
git add assurance_agent/risk/context.py tests/unit/risk/test_context.py tests/unit/risk/test_advisory.py
git commit -m "feat(risk): consume project problem lifecycle"
```

---

## Task 15: Feed Issue lifecycle evidence into retro without granting mutation authority

**Files:**

- Modify: `assurance_agent/retro/types.py`
- Modify: `assurance_agent/retro/archive_reader.py`
- Modify: `assurance_agent/retro/aggregator.py`
- Modify: `assurance_agent/retro/collect_stage.py`
- Modify: `assurance_agent/retro/nightly/phase_a.py`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Modify: `assurance_agent/_resources/skills/aa-retro/SKILL.md`
- Modify: `tests/unit/retro/test_aggregator.py`
- Modify: `tests/unit/retro/test_collect_stage.py`
- Modify: `tests/unit/retro/nightly/test_phases.py`
- Modify: `tests/unit/retro/test_promotions.py`

- [ ] **Step 1: Write archive-reader and snapshot tests**

Add `issues` to `_ARCHIVED_EVIDENCE_RELS`; copy active `issues/**` into retro evidence snapshots; load strict Change Issue events/snapshot into `ArchivedChange`. Malformed Issue JSONL must be visible as an incomplete/error condition rather than silently skipped line-by-line.

- [ ] **Step 2: Run retro reader tests and observe missing Issue evidence**

Run:

```bash
uv run pytest tests/unit/retro/nightly/test_phases.py tests/unit/retro/test_collect_stage.py -q
```

Expected: FAIL because retro ignores the Change Issue Ledger.

- [ ] **Step 3: Add lifecycle signal models and aggregation**

Add typed signals for occurrence trends, regressions, human Problem decisions, resolution/verification outcomes, and repeated not-an-issue classifications. Evidence IDs must point to immutable Change/Project event IDs. Filter Project Problem events to Problems/Occurrences connected to the selected retro window.

- [ ] **Step 4: Expand retro collect reads, not writes**

Allow `operation:retro-collect` and `skill:aa-retro` to read `project:qa/issues/**`. Keep every retro contract's writes restricted to `project:qa/retro/**`; add a contract test that retro cannot write `qa/issues/**`.

- [ ] **Step 5: Prove data-knowledge separation**

Add tests where a resolved Problem produces a `domain_knowledge` proposal, but `.aa/data-knowledge.yaml` remains unchanged until the existing semantic validation and acceptance/promotion flow runs. Invalid/duplicate proposals still fail the complete batch as already enforced by `run_retro_accept()`.

- [ ] **Step 6: Run retro tests**

Run:

```bash
uv run pytest tests/unit/retro/test_aggregator.py tests/unit/retro/test_collect_stage.py tests/unit/retro/nightly/test_phases.py tests/unit/retro/test_promotions.py tests/unit/workflow/graph/test_retro_ops.py tests/unit/workflow/graph/test_retro_workflow.py -q
```

Expected: PASS; no retro test observes a Problem mutation.

- [ ] **Step 7: Commit retro integration**

```bash
git add assurance_agent/retro/types.py assurance_agent/retro/archive_reader.py assurance_agent/retro/aggregator.py assurance_agent/retro/collect_stage.py assurance_agent/retro/nightly/phase_a.py assurance_agent/_resources/schemas/execution-contracts.yaml assurance_agent/_resources/skills/aa-retro/SKILL.md tests/unit/retro/test_aggregator.py tests/unit/retro/test_collect_stage.py tests/unit/retro/nightly/test_phases.py tests/unit/retro/test_promotions.py
git commit -m "feat(retro): consume immutable issue lifecycle evidence"
```

---

## Task 16: Remove every production dependency on known-product Issue files

**Files:**

- Modify: `assurance_agent/_resources/skills/aa-inspect/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-run/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-api-codegen/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-e2e-codegen/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-api-plan-reviewer/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-api-plan-fixer/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-api-codegen-fixer/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-e2e-codegen-fixer/SKILL.md`
- Modify: `docs/schemas.md`
- Modify: `tests/unit/report/test_failure_classifier.py`
- Modify: `tests/unit/report/test_report_builder.py`
- Modify: `tests/unit/risk/test_context.py`
- Create: `tests/unit/workflow/issues/test_no_legacy_issue_files.py`

- [ ] **Step 1: Add a repository guard test**

Scan production Python, packaged skills/templates, current docs, and active test fixtures. Reject runtime references to `known-product-issues.md` or `known-product-issues.json`. Exclude historical specs/plans from the guard because they are immutable design records.

Also test that the legacy `known_product_issue` classifier, if retained, only becomes an Observation/classification hint and never causes file IO, `PASS_WITH_WARNINGS`, Problem auto-confirmation, or archive blocking by itself.

- [ ] **Step 2: Run the guard and observe current references**

Run:

```bash
uv run pytest tests/unit/workflow/issues/test_no_legacy_issue_files.py -q
```

Expected: FAIL and enumerate the existing packaged skill/risk/docs references.

- [ ] **Step 3: Rewrite packaged skill policy**

Replace file-presence prerequisites and append/copy instructions with these boundaries:

- codegen/test code may emit explicit workaround annotations that the Observation collector records;
- codegen/reviewers do not create or mutate Problems;
- inspect/classifier artifacts remain execution evidence, not Problem lifecycle authority;
- no skill changes execution status merely because an Issue/Problem exists;
- only the deterministic reconciler/review apply operations write Issue Ledgers.

Delete the archive template's known-product file source section. Update `docs/schemas.md` to document the two Ledgers and projections.

- [ ] **Step 4: Update obsolete tests/fixtures**

Replace filesystem-known-issue fixtures with Observation/Problem fixtures. Retain classifier token tests only to prove the legacy execution label maps into structured evidence without lifecycle authority.

- [ ] **Step 5: Run the guard and affected suites**

Run:

```bash
uv run pytest tests/unit/workflow/issues/test_no_legacy_issue_files.py tests/unit/report/test_failure_classifier.py tests/unit/report/test_report_builder.py tests/unit/risk/test_context.py -q
```

Expected: PASS.

Run a direct audit:

```bash
rg -n "known-product-issues\.(md|json)" assurance_agent docs/schemas.md tests
```

Expected: no output except the guard's own forbidden token fixture if it is constructed dynamically; historical `docs/superpowers/specs/**` and `docs/superpowers/plans/**` were intentionally not searched.

- [ ] **Step 6: Commit the clean cut**

```bash
git add assurance_agent/_resources/skills/aa-inspect/SKILL.md assurance_agent/_resources/skills/aa-run/SKILL.md assurance_agent/_resources/skills/aa-api-codegen/SKILL.md assurance_agent/_resources/skills/aa-e2e-codegen/SKILL.md assurance_agent/_resources/skills/aa-api-plan-reviewer/SKILL.md assurance_agent/_resources/skills/aa-api-plan-fixer/SKILL.md assurance_agent/_resources/skills/aa-api-codegen-fixer/SKILL.md assurance_agent/_resources/skills/aa-e2e-codegen-fixer/SKILL.md docs/schemas.md tests/unit/report/test_failure_classifier.py tests/unit/report/test_report_builder.py tests/unit/risk/test_context.py tests/unit/workflow/issues/test_no_legacy_issue_files.py
git commit -m "refactor(issues): remove known-product file workflow"
```

---

## Task 17: Prove full workflow, benchmark, and acceptance invariants

**Files:**

- Create: `tests/fixtures/issues/vue_fastapi_admin/`
- Create: `tests/integration/test_issue_lifecycle_acceptance.py`
- Modify: `tests/integration/test_packaged_happy_path.py`
- Modify: `tests/integration/test_cli_workflow_v2.py`
- Modify: `docs/schemas.md`

- [ ] **Step 1: Add a deterministic vue-fastapi-admin acceptance fixture**

Extract a minimal, non-secret execution/evidence fixture representing:

- an ordinary HTTP 500 becoming a `product_bug` candidate without a pre-existing marker;
- a passing workaround test still producing an Observation/Occurrence;
- repeated API/fuzz evidence sharing one exact fingerprint;
- a test-code failure remaining distinct from a product Problem;
- analyzer failure retaining every Observation and permitting idempotent later retry.

Use scripted analyzer responses; do not call a live model in protocol tests and do not modify existing benchmark run outputs.

- [ ] **Step 2: Write the hard acceptance tests**

Assert exactly:

1. every abnormal Observation has valid evidence;
2. exact-fingerprint duplicate Problem count is zero;
3. Ledger replay bytes are identical;
4. Observation loss through failure/retry/crash is zero;
5. archive blocks caused by Issue state are zero;
6. unauthorized resolved/not-an-issue/accepted-risk transitions are zero;
7. initial and healing batches both reconcile before report;
8. linked fix plus complete authoritative verification resolves;
9. missing verification scope remains `verification_pending`;
10. post-archive review leaves archive bytes unchanged;
11. execution `final_status` is identical before and after Issue processing;
12. `run_tests: false` creates no Issue subgraph artifacts.

- [ ] **Step 3: Run acceptance tests and fix only exposed gaps**

Run:

```bash
uv run pytest tests/integration/test_issue_lifecycle_acceptance.py tests/integration/test_packaged_happy_path.py tests/integration/test_cli_workflow_v2.py -q
```

Expected: PASS. If a failure reveals a production defect, add the smallest regression test to the owning earlier test module before changing production code.

- [ ] **Step 4: Run the complete focused Issue/graph matrix**

Run:

```bash
uv run pytest tests/unit/workflow/issues tests/unit/workflow/graph tests/unit/artifacts tests/unit/report tests/unit/risk tests/unit/retro tests/integration/test_graph_runtime.py tests/integration/test_graph_runtime_faults.py tests/integration/test_issue_lifecycle_workflow.py tests/integration/test_issue_reconcile_concurrency.py tests/integration/test_issue_review_workflow.py tests/integration/test_issue_lifecycle_acceptance.py -q
```

Expected: PASS.

- [ ] **Step 5: Run project-wide quality checks**

Run:

```bash
uv run pytest -q
uv run ruff check assurance_agent tests
uv run pyright
```

Expected: all commands exit 0 using the pytest, Ruff, and Pyright configuration declared by `pyproject.toml`.

- [ ] **Step 6: Perform invariant audits**

Run:

```bash
rg -n "known-product-issues\.(md|json)" assurance_agent docs/schemas.md tests
rg -n "qa/issues|project:issue-registry|synchronized" assurance_agent/_resources/schemas assurance_agent/workflow
rg -n "data-knowledge\.yaml" assurance_agent/workflow/issues assurance_agent/_resources/skills/aa-issue-analyzer assurance_agent/_resources/skills/aa-issue-triage-advisor
```

Expected:

- first command has only the deliberate guard-test token, if any;
- second command shows writes only in reconciler/review contracts and synchronized runtime support;
- third command has no output.

- [ ] **Step 7: Update current schema documentation**

Document storage paths, canonical ownership, recovery semantics, synchronized project writes, Issue risk vs execution status, review commands/payloads, risk consumption, retro read-only authority, and the no-migration policy. Link the approved design and this plan.

- [ ] **Step 8: Commit acceptance coverage and documentation**

```bash
git add tests/fixtures/issues/vue_fastapi_admin tests/integration/test_issue_lifecycle_acceptance.py tests/integration/test_packaged_happy_path.py tests/integration/test_cli_workflow_v2.py docs/schemas.md
git commit -m "test(issues): verify lifecycle workflow acceptance"
```

---

## Final Verification Checklist

- [ ] Typed recovery activates only after retry exhaustion and only for the declared error allowlist.
- [ ] Failed analyzer write-sets never commit; Observations already committed remain intact.
- [ ] Cross-process Changes refresh and serialize `qa/issues/**` under one project lock held through Update.
- [ ] Candidate semantic validation is complete-list/all-or-nothing.
- [ ] Every validated candidate creates an immutable Occurrence.
- [ ] Exact fingerprints create one Problem identity; semantic matches require human confirmation.
- [ ] Problem versions, transitions, merge aliases, verification, and regression replay deterministically.
- [ ] Initial execution and every healing rerun invoke the same Issue subgraph.
- [ ] Human review is independent, repeatable, audited, stale-version-safe, and non-blocking.
- [ ] Report/archive clearly separate execution status from Issue risk.
- [ ] Open or unknown Issues never block archive.
- [ ] Risk reads structured Problems; retro reads lifecycle events but cannot mutate Problems.
- [ ] Data knowledge still changes only through validated retro acceptance/promotion.
- [ ] No production path reads or generates known-product Issue Markdown/JSON files.
- [ ] All focused, integration, full pytest, lint, and type-check commands pass.
