# Change-local Assurance Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Use `superpowers:test-driven-development` for every behavior change and `superpowers:verification-before-completion` before claiming completion.

**Goal:** Replace the Phase 5 whole-SUT snapshot/HEAD/export pipeline with a change-local stage/validate/promote workflow in which the original SUT is readable during execution and is mutated only by an explicit, idempotent `aa export` after the change is achieved.

**Architecture:** Graph-engine owns a business-neutral dual-root task workspace: a stable read root and an empty per-attempt write root. It records target baselines, staged file digests, promotion receipts, activities, and ledger state, but never interprets `qa`, a change ID, or a test family. The installed Assurance product supplies `<project>/qa/changes/<id>/.staging` and `.runtime`, validates the staged candidate, promotes ordinary artifacts into the canonical change, stores generated files under family-specific change paths, creates disposable execution views, and publishes only the achieved apply manifest. OpenCode keeps the real project as its session directory while the installed boundary plugin redirects allowed logical writes into the authenticated attempt root; Cursor sees the real project read-only through an OS sandbox and can write only the attempt root. OpenChamber lists changes by scanning `qa/changes/*` directly.

**Tech Stack:** Python 3.11, uv workspace, Pydantic v2 frozen models, pytest/pytest-asyncio/Hypothesis, JSON/YAML schemas, OpenCode JavaScript boundary plugin, macOS `sandbox-exec`, Linux `bwrap`, Node.js/OpenChamber React and server tests.

**Spec:** `docs/superpowers/specs/2026-08-25-change-local-assurance-workspace-design.md`

## Global Constraints

- Implement Assurance changes in `/Users/lvqingquan/agent/assurance-agent/.worktrees/pure-graph-engine-phase3-spec` on `codex/pure-graph-engine-phase3-spec` without rewriting unrelated dirty files. Before each commit, stage only the files declared by that task and inspect `git diff --cached --stat` plus `git diff --cached`.
- Implement OpenChamber changes in `/Users/lvqingquan/agent/assurance-agent/.worktrees/openchamber-qa-latest-results` on `codex/qa-latest-run-results`; it is a separate repository/worktree and receives separate commits.
- The approved spec is authoritative. If a test reveals a conflict with the spec, stop and amend this plan and the spec before changing behavior.
- Do not preserve compatibility with `results/<run>/project`, `workspace/trees`, `workspace/HEAD.json`, whole-tree exports, `benchmarkResultRoot`, latest-run pointers, or `auto_archive`.
- Never mutate original SUT files during workflow execution. Canonical change files are the only promoted workflow outputs; generated repository files remain under `qa/changes/<id>/generated/<family>/files/` until `aa export`.
- Graph-engine must remain business-neutral: no `qa`, `changes`, Assurance family name, default graph, SUT scan, project-loaded handler, validator, hook, or executable extension point.
- Project YAML may replace graph and execution contracts as already documented, but it cannot register Python behavior. All handlers and validators remain installed-wheel contributions.
- OpenCode and Cursor confinement must be mechanical. Prompt instructions alone are not a security boundary.
- Every task follows RED/GREEN/REFACTOR: add the focused failing test, run it and observe the intended failure, implement the minimum behavior, rerun focused tests, then commit.
- Each task writes a short evidence note to `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/task-N-report.md` containing commands, observed results, and any approved deviation.

## Target Interfaces

The implementation converges on these public/deep-module interfaces; do not expose lower-level path construction to capability packages:

```python
# graph_engine.plugin_api
class TaskWorkspaceIdentity(FrozenModel):
    schema_version: Literal["2"] = "2"
    task_id: str
    attempt: int
    attempt_id: str
    read_root_digest: str
    write_root_digest: str
    baseline_digest: str
    identity_digest: str


@dataclass(frozen=True, slots=True)
class TaskContext:
    project_root: Path
    write_root: Path
    workspace_identity: TaskWorkspaceIdentity
    heartbeat: Callable[[], None]
    cancel_requested: Callable[[], bool]
    invocation: InvocationView
    activity: TaskActivityPort | None = None
    secrets: SecretPort | None = None


class StagedFile(FrozenModel):
    path: str                    # project-relative logical target
    baseline_sha256: str | None
    staged_sha256: str
    mode: int


class StagedWriteSet(FrozenModel):
    workspace_identity_digest: str
    files: tuple[StagedFile, ...]
    digest: str
```

```python
# assurance_product.change_workspace
class ChangeWorkspace:
    @classmethod
    def open(cls, project_root: Path, change_id: str) -> "ChangeWorkspace": ...
    def begin_attempt(self, task_id: str, attempt: int, claims: ResourceClaims) -> TaskWorkspaceBinding: ...
    def promote_attempt(self, staged: StagedWriteSet, outcome: TaskOutcome) -> PromotionReceipt: ...
    def discard_attempt(self, identity: TaskWorkspaceIdentity) -> None: ...
    def merge_generated(self, families: tuple[TestFamily, ...]) -> MergedGeneratedSet: ...
    def build_execution_view(self, merged: MergedGeneratedSet) -> ExecutionView: ...
    def publish_achieved(self) -> PublishReceipt: ...
```

`TaskWorkspaceBinding` is process-local and contains canonical `Path` objects; the ledger stores only its authenticated identity and relative file records. `StagedFile.path` is always relative to `project_root`. A staged file physically lives at `write_root / StagedFile.path`, so generic promotion can use the same logical path without understanding product layout.

## Task 1: Freeze Characterization and Change Workspace Paths

**Files:**
- Create: `packages/assurance-product/assurance_product/change_workspace.py`
- Create: `tests/phase5/test_change_workspace_paths.py`
- Create: `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/task-1-report.md`

**Interfaces:** `ChangePaths`, `ChangeWorkspace.open()`, `safe_change_id()`, `safe_relative_path()`.

- [ ] Write tests proving that `ChangeWorkspace.open(project, "BENCH-dept-001")` accepts a canonical absolute project root and resolves `change_root`, `staging_root`, `runtime_root`, `generated_root`, and `apply_manifest` exactly under `project/qa/changes/BENCH-dept-001`; reject a missing/non-directory project, symlinked `qa/changes`, absolute or separator-bearing change IDs, `/`, `..`, NUL, and output paths escaping the project.
- [ ] Run `uv run pytest tests/phase5/test_change_workspace_paths.py -q` and observe import failure for `assurance_product.change_workspace`.
- [ ] Implement frozen `ChangePaths` and canonical path validation. Do not create directories in `open()`; add an explicit `initialize()` that atomically creates `.runtime/{ledger,activities,receipts}` and `.staging`.

```python
@dataclass(frozen=True, slots=True)
class ChangePaths:
    project_root: Path
    change_root: Path
    staging_root: Path
    runtime_root: Path
    generated_root: Path
    apply_manifest: Path


class ChangeWorkspace:
    @classmethod
    def open(cls, project_root: Path, change_id: str) -> "ChangeWorkspace":
        project = require_real_directory(project_root)
        change = require_descendant(project, project / "qa" / "changes" / safe_change_id(change_id))
        return cls(ChangePaths(project, change, change / ".staging", change / ".runtime", change / "generated", change / "apply-manifest.json"))
```

- [ ] Add a historical characterization test using a fixture shaped like pre-`51f385c`: canonical change artifacts are under `qa/changes/<id>`, while `tests/**` remains outside the active change. The test documents structure only and does not import legacy runtime code.
- [ ] Run `uv run pytest tests/phase5/test_change_workspace_paths.py -q` and commit only Task 1 files with `git commit -m "feat(product): define change-local workspace paths"`.

## Task 2: Introduce the Generic Dual-root Task Workspace

**Files:**
- Modify: `packages/graph-engine/graph_engine/plugin_api.py`
- Create: `packages/graph-engine/graph_engine/runtime/task_workspace.py`
- Modify: `packages/graph-engine/graph_engine/runtime/__init__.py`
- Create: `packages/graph-engine/tests/runtime/test_task_workspace.py`
- Create: `packages/graph-engine/tests/runtime/test_task_workspace_faults.py`
- Create: `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/task-2-report.md`

**Interfaces:** `TaskWorkspaceIdentity`, `TaskWorkspaceBinding`, `StagedFile`, `StagedWriteSet`, `PromotionReceipt`, `TaskWorkspaceStore`.

- [ ] Add failing tests for an empty attempt root, stable identity, declared-output baseline capture, stage sealing, undeclared-file rejection, symlink/hard-link/special-file rejection, target drift rejection, atomic promotion, idempotent repeated promotion, and different-bytes replay rejection.
- [ ] Run `uv run pytest packages/graph-engine/tests/runtime/test_task_workspace.py packages/graph-engine/tests/runtime/test_task_workspace_faults.py -q`; verify missing type/store failures.
- [ ] Implement `TaskWorkspaceStore(project_root, attempts_root, receipts_root)` with no tree, HEAD, or full project copy. `begin()` receives `task_id`, `attempt`, and the exact normalized output claims. It creates `<attempts_root>/<safe-task-id>/<attempt-id>` and captures only claimed target baselines.

```python
binding = store.begin(task_id=request.task_id, attempt=request.attempt, output_paths=claims.writes)
assert binding.project_root == project_root.resolve()
assert binding.write_root == attempts_root / safe_task_id / binding.identity.attempt_id

staged = store.seal(binding.identity)
receipt = store.promote(binding.identity, staged)
```

- [ ] Promotion must write and fsync every authenticated adjacent temporary and rollback file, publish each target with `os.replace` plus parent fsync, and durably publish the digest-bound completed receipt before removing any rollback evidence or pending intent. The direct `qa/changes/<id>` layout cannot provide one OS-level atomic visibility point for files spanning directories: batch terminal-failure atomicity therefore comes from durable intent/receipt, fail-closed `PromotionPublicationIndeterminate`, resource locking, and authenticated replay. Engine-managed readers and successors may consume the batch only after the completed receipt and terminal event. A raw directory observer that ignores the receipt can see a short intermediate set during successful replaces. Do not add generation/pointer, SnapshotStore, tree, or HEAD indirection to hide that direct-layout tradeoff.
- [ ] Run focused tests plus `uv run pytest packages/graph-engine/tests/runtime/test_attempt_workspace_identity.py -q`; commit with `git commit -m "feat(engine): add dual-root task workspace"`.

## Task 3: Migrate Scheduler Commit Semantics off Snapshot Trees

**Execution note:** Tasks 3 and 4 form one atomic implementation/review batch. The scheduler event/activity identity cannot switch to `TaskWorkspaceIdentity` while engine construction, host protocol, and worker receipts still require `AttemptWorkspaceIdentity`/`SnapshotStore`; do not add a transitional compatibility mode merely to split the commits. The combined batch uses both tasks' declared files and may update `runtime/activity.py`, `runtime/host_receipts.py`, `runtime/__init__.py`, and their directly corresponding graph-engine tests when required to keep one coherent schema.

Promotion publication is a prepared/recoverable state until the completed
receipt is durable. An uncertain replacement, rollback, or completed-receipt
publication raises `PromotionPublicationIndeterminate`; scheduler and engine
must not turn it into an ordinary failed attempt. Resource locks remain held,
and no successor may consume canonical outputs, until authenticated replay
completes the receipt and terminal event.

**Files:**
- Modify: `packages/graph-engine/graph_engine/runtime/scheduler.py`
- Modify: `packages/graph-engine/graph_engine/runtime/engine.py`
- Modify: `packages/graph-engine/graph_engine/runtime/models.py`
- Modify: `packages/graph-engine/graph_engine/runtime/events.py`
- Modify: `packages/graph-engine/graph_engine/runtime/checkpoint.py`
- Modify: `packages/graph-engine/graph_engine/graph/schema.py`
- Modify: `packages/graph-engine/graph_engine/graph/compiler.py`
- Modify: `packages/graph-engine/graph_engine/runtime/planner.py`
- Modify: `packages/graph-engine/graph_engine/plugin_api.py`
- Modify: `packages/graph-engine/tests/graph/test_schema_and_compiler.py`
- Modify: `packages/graph-engine/tests/runtime/test_planner.py`
- Modify: `packages/graph-engine/tests/runtime/test_scheduler.py`
- Create: `packages/graph-engine/tests/runtime/test_scheduler_faults.py`
- Modify: `packages/graph-engine/tests/runtime/test_engine.py`
- Create: `packages/graph-engine/tests/runtime/test_staged_promotion_recovery.py`
- Create: `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/task-3-report.md`

**Interfaces:** scheduler accepts `TaskWorkspaceStore`; terminal events carry `staged_write_set_digest` and `promotion_receipt_digest`, never `candidate_tree_id` or `current_head_tree_id`.

- [ ] Change scheduler tests first: two disjoint tasks in one wave receive the same project read root and different empty write roots; a failed/indeterminate task is discarded; validators see the sealed staged set; only a successful validated task is promoted; replay consumes the durable promotion receipt without repeating adapter execution.
- [ ] Run `uv run pytest packages/graph-engine/tests/runtime/test_scheduler.py packages/graph-engine/tests/runtime/test_staged_promotion_recovery.py -q` and confirm failures mention current `SnapshotStore`/tree fields.
- [ ] Add a business-neutral `ResourceClaimTemplate` whose parameters are closed input projections. The compiler validates templates and JSON pointers; the planner resolves them before scheduling. Assurance contracts use it to produce concrete change/family prefixes such as `qa/changes/<resolved-id>/generated/api`, so API/E2E/Fuzz/Performance tasks are disjoint and may occupy one wave. Do not add Assurance names or special parameters to graph-engine.
- [ ] Replace the scheduler sequence with `resolve claims -> begin -> dispatch/reconcile -> seal -> validator -> promote -> terminal event`. Record a pre-promotion intent before filesystem mutation and a terminal receipt after mutation so recovery can distinguish not-started, resumable, and completed promotion.
- [ ] Remove snapshot-head advancement from the reducer and invocation projection. Preserve task activity binding, heartbeat, cancellation, effect receipts, interrupt, STOP, replay, and deterministic token semantics.
- [ ] Run all scheduler/engine/recovery tests: `uv run pytest packages/graph-engine/tests/runtime/test_scheduler.py packages/graph-engine/tests/runtime/test_scheduler_faults.py packages/graph-engine/tests/runtime/test_engine.py packages/graph-engine/tests/runtime/test_staged_promotion_recovery.py -q`.
- [ ] Commit with `git commit -m "refactor(engine): promote staged task outputs"`.

## Task 4: Replace Workspace Seed and Host Protocol with Authenticated Roots

**Execution note:** Implement and review this task together with Task 3 as stated above. Produce one combined RED/GREEN report for the schema cutover and record both task numbers in the commit/review evidence. Installed `TaskHandler`/product Python is inside the trusted engine boundary: the host-v2 descriptor authenticates protocol inputs and rejects persistent root/attempt replacement or worker path substitution, but it is not a sandbox against a same-permission malicious installed handler that swaps and restores a path during its own call. Do not claim that pre/post checks close that unsupported threat or add polling as a substitute for confinement. Provider/model code is untrusted and becomes mechanically confined in Tasks 5 and 6.

**Files:**
- Modify: `packages/graph-engine/graph_engine/runtime/seed.py`
- Modify: `packages/graph-engine/graph_engine/runtime/engine.py`
- Modify: `packages/graph-engine/graph_engine/runtime/invocation_lock.py`
- Modify: `packages/graph-engine/graph_engine/runtime/host_protocol.py`
- Modify: `packages/graph-engine/graph_engine/runtime/production_host.py`
- Modify: `packages/graph-engine/graph_engine/runtime/production_worker.py`
- Modify: `packages/graph-engine/graph_engine/plugin_api.py`
- Modify: `packages/graph-engine/tests/runtime/test_host_protocol.py`
- Modify: `packages/graph-engine/tests/runtime/test_production_host.py`
- Modify: `packages/graph-engine/tests/runtime/test_production_host_faults.py`
- Create: `packages/graph-engine/tests/runtime/test_invocation_workspace_binding.py`
- Create: `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/task-4-report.md`

**Interfaces:** `InvocationSeed(root_input, root_input_digest)` no longer embeds a captured project tree; `Engine.start()` and `Engine.open()` require a process-local `InvocationWorkspaceBinding(project_root, attempts_root, receipts_root)` whose canonical path identities are locked by digest.

- [ ] Write failing tests proving start does not read every project file, resume rejects a different project root or attempts root, host calls contain authenticated project/write-root descriptors, and a worker cannot substitute another attempt directory.
- [ ] Run the focused engine/host tests and observe failures from `WorkspaceSeed`, `initial_tree_id`, and `AttemptRootDescriptor(schema_version="1")`.
- [ ] Change `TaskContext.workspace_root` to the target interface fields `project_root`, `write_root`, and `workspace_identity`; make host protocol schema version `2`; bind both roots and the staged baseline digest into dispatch and terminal receipts.
- [ ] Keep absolute paths out of event payloads. The invocation lock stores canonical path identity digests; the CLI re-supplies paths on open/resume, and engine authentication recomputes digests before reading the ledger.
- [ ] Run `uv run pytest packages/graph-engine/tests/runtime/test_invocation_workspace_binding.py packages/graph-engine/tests/runtime/test_host_protocol.py packages/graph-engine/tests/runtime/test_production_host.py packages/graph-engine/tests/runtime/test_production_host_faults.py -q` and commit with `git commit -m "refactor(engine): bind invocations to read and write roots"`.

## Task 5: Make Agent Runtime Contracts Dual-root

**Execution note:** Adapters receive the real project path because providers
need it as readable context, but they must treat the authenticated request as
the only path authority. This contract migration does not by itself make the
project path read-only; Task 6 must place provider shell/tool execution behind
the OpenCode boundary or OS sandbox and allow writes only below the authenticated
attempt `write_root`.

**Files:**
- Modify: `packages/agent-runtime-contracts/agent_runtime_contracts/models.py`
- Modify: `packages/agent-runtime-contracts/agent_runtime_contracts/__init__.py`
- Modify: `packages/agent-runtime-contracts/tests/test_models.py`
- Modify: `packages/agent-runtime-opencode/agent_runtime_opencode/handler.py`
- Delete: `packages/agent-runtime-opencode/agent_runtime_opencode/runtime_cache.py`
- Delete: `packages/agent-runtime-opencode/tests/test_runtime_cache.py`
- Modify: `packages/agent-runtime-opencode/tests/test_binding_authority.py`
- Modify: `packages/agent-runtime-opencode/tests/test_fault_matrix.py`
- Modify: `packages/agent-runtime-cursor/agent_runtime_cursor/handler.py`
- Modify: `packages/agent-runtime-cursor/agent_runtime_cursor/process.py`
- Modify: `packages/agent-runtime-cursor/tests/test_process_launch.py`
- Modify: `packages/agent-runtime-cursor/tests/test_process_receipt.py`
- Create: `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/task-5-report.md`

**Interfaces:** `AgentRunRequest.workspace: AgentWorkspaceV1` with project-relative `write_root`, exact allowed logical outputs, and workspace identity digest; provider prompts expose project and stage coordinates but cannot change them.

- [ ] Add contract tests for canonical relative write roots, sorted exact output paths, no absolute/parent paths, and stable digest. Add adapter tests proving OpenCode session directory and Cursor cwd are the real project root while the request carries the separate stage root.
- [ ] Run contract and adapter-focused tests; verify they fail because both adapters still use `context.workspace_root` as a copied attempt directory.
- [ ] Add the closed model:

```python
class AgentWorkspaceV1(FrozenModel):
    schema_version: Literal["1"] = "1"
    write_root: str
    allowed_outputs: tuple[str, ...]
    identity_digest: str
```

Set OpenCode `directory=context.project_root`, Cursor `cwd=context.project_root`, and compute dispatch identity from both `project_root` and `write_root`. Delete runtime-cache hydration because OpenCode configuration is no longer copied into each attempt.
- [ ] Update all cancellation/reconcile paths to recompute the same dual-root identity. A bound provider session/process whose identity differs must remain indeterminate/fail-closed.
- [ ] Run `uv run pytest packages/agent-runtime-contracts/tests packages/agent-runtime-opencode/tests packages/agent-runtime-cursor/tests -q` and commit with `git commit -m "refactor(adapters): execute against dual-root workspaces"`.

## Task 6: Enforce OpenCode and Cursor Write Confinement

**Execution note:** This is the mechanical security boundary for untrusted
provider/model behavior. Acceptance must include provider shell/tool attempts
to rename, replace, or swap-and-restore the project root and its ancestors;
all such attempts must fail, while only the authenticated attempt `write_root`
is writable. The trusted installed-handler limitation recorded in Task 4 does
not relax this provider acceptance criterion.

**Files:**
- Modify: `packages/assurance-product/assurance_product/opencode_agents.py`
- Modify: `packages/assurance-product/assurance_product/resources/opencode/assurance-boundary.mjs`
- Create: `packages/assurance-product/assurance_product/resources/opencode/workspace-binding-v1.schema.json`
- Modify: `tests/phase5/test_agent_execution_contracts.py`
- Create: `tests/phase5/test_opencode_staging_boundary.py`
- Modify: `packages/agent-runtime-cursor/agent_runtime_cursor/process.py`
- Create: `packages/agent-runtime-cursor/agent_runtime_cursor/filesystem_sandbox.py`
- Create: `packages/agent-runtime-cursor/tests/test_filesystem_sandbox.py`
- Create: `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/task-6-report.md`

**Interfaces:** OpenCode session title carries a canonical digest-bound workspace-binding envelope consumed by the source-authenticated installed boundary plugin; Cursor launch uses `FilesystemSandbox.wrap(request)`, supporting macOS Seatbelt and Linux bubblewrap and failing closed when unavailable.

- [ ] Add a Node-driven plugin test matrix: native `write`, `edit`, and every `apply_patch` header are redirected from an allowed logical path to `write_root/<logical-path>`; reads prefer a staged overlay; writes outside exact allowed outputs, different attempts, canonical change state owned by another node, `tests/**`, symlinks, shell escapes, and missing/invalid bindings are denied.
- [ ] Run `uv run pytest tests/phase5/test_opencode_staging_boundary.py -q` and observe direct canonical writes in the current plugin.
- [ ] Implement an authenticated session binding created after OpenCode session creation and before prompt admission. The binding contains session ID, agent profile, project-root digest, write-root relative path, allowed output paths, task/attempt identity, and digest. The plugin validates it, mutates tool arguments to the staged physical path, and never accepts ambient glob permission as authority.
- [ ] Remove archiver mutation permissions from the live `full` profiles. Keep shell disabled for all author/reviewer profiles. Executor shell commands must point at the explicit execution view and retain cache-disabled environment flags.
- [ ] Add Cursor sandbox tests. On macOS wrap the pinned executable with a generated `sandbox-exec` profile that permits project reads and writes only under the attempt root plus an adapter-owned temp directory. On Linux use authenticated `bwrap` with a read-only `/` bind and writable binds only for the attempt/temp roots. Validate sandbox executable identity and record its profile digest in the process receipt.
- [ ] Add adversarial provider tests proving shell/tool attempts cannot rename, replace, or swap-use-restore the project root or a canonical output parent; only the exact authenticated attempt `write_root` is writable. Run the same acceptance against OpenCode tool mediation and the Cursor OS sandbox.
- [ ] Run `uv run pytest tests/phase5/test_agent_execution_contracts.py tests/phase5/test_opencode_staging_boundary.py packages/agent-runtime-cursor/tests/test_filesystem_sandbox.py packages/agent-runtime-cursor/tests/test_process_launch.py -q` and commit with `git commit -m "feat(adapters): confine writes to task staging roots"`.

## Task 7: Route Capability Outputs through ChangeWorkspace

**Files:**
- Modify: `packages/assurance-product/assurance_product/change_workspace.py`
- Create: `packages/assurance-product/assurance_product/output_routes.py`
- Modify: `packages/assurance-product/assurance_product/agent_contracts.py`
- Modify: `packages/assurance-intake/assurance_intake/operations/agent_skills.py`
- Modify: `packages/assurance-intake/assurance_intake/operations/finalize.py`
- Modify: `packages/assurance-generation/assurance_generation/operations/planning.py`
- Modify: `packages/assurance-generation/assurance_generation/operations/review.py`
- Modify: `packages/assurance-quality/assurance_quality/operations/agent_skills.py`
- Modify: `packages/assurance-healing/assurance_healing/operations/agent.py`
- Modify: `packages/assurance-improvement/assurance_improvement/operations/agent.py`
- Modify: `packages/assurance-intake/assurance_intake/resources/skills/aa-intake/SKILL.md`
- Modify: `packages/assurance-intake/assurance_intake/resources/skills/aa-explore/SKILL.md`
- Modify: `packages/assurance-intake/assurance_intake/resources/skills/aa-case-design/SKILL.md`
- Modify: `packages/assurance-intake/assurance_intake/resources/skills/aa-case-reviewer/SKILL.md`
- Modify: `packages/assurance-generation/assurance_generation/resources/skills/aa-api-plan/SKILL.md`
- Modify: `packages/assurance-generation/assurance_generation/resources/skills/aa-api-plan-reviewer/SKILL.md`
- Modify: `packages/assurance-generation/assurance_generation/resources/skills/aa-e2e-plan/SKILL.md`
- Modify: `packages/assurance-generation/assurance_generation/resources/skills/aa-e2e-plan-reviewer/SKILL.md`
- Modify: `packages/assurance-generation/assurance_generation/resources/skills/aa-fuzz-plan/SKILL.md`
- Modify: `packages/assurance-generation/assurance_generation/resources/skills/aa-fuzz-plan-reviewer/SKILL.md`
- Modify: `packages/assurance-generation/assurance_generation/resources/skills/aa-performance-plan/SKILL.md`
- Modify: `packages/assurance-generation/assurance_generation/resources/skills/aa-performance-plan-reviewer/SKILL.md`
- Modify: `packages/assurance-quality/assurance_quality/resources/skills/aa-fact-baseline/SKILL.md`
- Modify: `packages/assurance-quality/assurance_quality/resources/skills/aa-inspect/SKILL.md`
- Modify: `packages/assurance-quality/assurance_quality/resources/skills/aa-issue-analyzer/SKILL.md`
- Modify: `packages/assurance-quality/assurance_quality/resources/skills/aa-issue-triage-advisor/SKILL.md`
- Modify: `packages/assurance-quality/assurance_quality/resources/skills/aa-report-generator/SKILL.md`
- Modify: `packages/assurance-healing/assurance_healing/resources/skills/aa-coverage-repair/SKILL.md`
- Modify: `packages/assurance-healing/assurance_healing/resources/skills/aa-fix-proposal/SKILL.md`
- Modify: `packages/assurance-improvement/assurance_improvement/resources/skills/aa-improvement-reviewer/SKILL.md`
- Modify: `packages/assurance-improvement/assurance_improvement/resources/skills/aa-retro/SKILL.md`
- Modify: `packages/assurance-improvement/assurance_improvement/resources/skills/aa-retro-eval-analysis/SKILL.md`
- Modify: `packages/assurance-improvement/assurance_improvement/resources/skills/aa-retro-issue-analysis/SKILL.md`
- Modify: `packages/assurance-improvement/assurance_improvement/resources/skills/aa-retro-workflow-analysis/SKILL.md`
- Modify: `packages/assurance-intake/tests/test_agent_skills.py`
- Modify: `packages/assurance-intake/tests/test_contracts.py`
- Modify: `packages/assurance-generation/tests/test_planning.py`
- Modify: `packages/assurance-generation/tests/test_plan_review.py`
- Modify: `packages/assurance-quality/tests/test_agent_skills.py`
- Modify: `packages/assurance-quality/tests/test_report.py`
- Modify: `packages/assurance-healing/tests/test_proposal.py`
- Modify: `packages/assurance-improvement/tests/test_retro.py`
- Modify: `packages/assurance-improvement/tests/test_review.py`
- Create: `tests/phase5/test_change_local_output_routing.py`
- Create: `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/task-7-report.md`

**Interfaces:** one installed-product `OutputRouteCatalog` maps each closed capability alias to exact project-relative canonical outputs; prepare injects the route into `AgentRunRequest`; finalize reads prior inputs from `context.project_root` and candidate outputs only from `context.write_root`.

- [ ] Add a closed-set test that every one of the 33 agent triplets has an output route and no route targets `.runtime`, `.staging`, `qa/archive`, or original `tests/**`. Add per-wheel tests proving failed parsing/validation leaves canonical outputs unchanged.
- [ ] Run the new test and observe that finalizers currently read one copied `workspace_root` and agents target canonical files directly.
- [ ] Implement exact routes. Examples:

```python
"assurance.intake.intake.execute": (f"qa/changes/{change_id}/proposal.md",),
"assurance.intake.explore.execute": (f"qa/changes/{change_id}/explore/exploration.json",),
"assurance.quality.report.execute": (f"qa/changes/{change_id}/report/report.md",),
```

Do not make routes project-configurable. The installed product owns this closed catalog.
- [ ] Update the listed prepare/finalize operations and skill files so logical output paths match the route. Validation must combine canonical project inputs with staged outputs explicitly; it must not glob `.staging` or infer output files from model prose.
- [ ] Run the changed wheel tests plus `uv run pytest tests/phase5/test_change_local_output_routing.py -q`; commit with `git commit -m "refactor(product): route capability outputs through change staging"`.

## Task 8: Store and Merge Generated Test Families inside the Change

**Files:**
- Modify: `packages/assurance-generation/assurance_generation/operations/codegen.py`
- Modify: `packages/assurance-generation/assurance_generation/validators/generated_files.py`
- Modify: `packages/assurance-generation/assurance_generation/contracts/codegen.py`
- Modify: `packages/assurance-generation/assurance_generation/resources/skills/aa-api-codegen/SKILL.md`
- Modify: `packages/assurance-generation/assurance_generation/resources/skills/aa-e2e-codegen/SKILL.md`
- Modify: `packages/assurance-generation/assurance_generation/resources/skills/aa-fuzz-codegen/SKILL.md`
- Modify: `packages/assurance-generation/assurance_generation/resources/skills/aa-performance-codegen/SKILL.md`
- Modify: `packages/assurance-generation/assurance_generation/resources/result-contracts/codegen.v1.schema.json`
- Modify: `packages/assurance-generation/tests/test_codegen.py`
- Create: `packages/assurance-product/assurance_product/generated_merge.py`
- Create: `tests/phase5/test_generated_merge.py`
- Create: `tests/phase5/test_parallel_generation_isolation.py`
- Create: `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/task-8-report.md`

**Interfaces:** `GeneratedFileV2(target_path, staged_path, sha256, mode, operation, family)`; physical canonical source is `qa/changes/<id>/generated/<family>/files/<target_path>`; `merge_generated()` returns a closed `MergedGeneratedSet`.

- [ ] Add tests for all four physical namespaces, closed manifest membership, extra-on-disk rejection, duplicate identical ownership acceptance, differing bytes/mode/operation conflict rejection, path traversal rejection, and deterministic ordering independent of branch completion order.
- [ ] Run generation and merge tests and observe current direct `tests/**` expectations.
- [ ] Change codegen instructions and contracts so the model writes logical staged outputs under the family change prefix while the manifest preserves `target_path="tests/..."`. Finalizers verify both the staged physical route and target-family policy before promotion.
- [ ] Implement `merge_generated()` as a pure collection/validation step. It must not write into the SUT and must return the exact target-to-source mapping and digest used by execution and export.
- [ ] Run `uv run pytest packages/assurance-generation/tests tests/phase5/test_generated_merge.py tests/phase5/test_parallel_generation_isolation.py -q` and commit with `git commit -m "feat(product): isolate and merge generated test families"`.

## Task 9: Execute Candidate Tests without Publishing Them

**Files:**
- Create: `packages/assurance-product/assurance_product/execution_view.py`
- Create: `tests/phase5/test_execution_view.py`
- Modify: `packages/assurance-execution/assurance_execution/operations/paths.py`
- Modify: `packages/assurance-execution/assurance_execution/operations/selection.py`
- Modify: `packages/assurance-execution/assurance_execution/operations/runner.py`
- Modify: `packages/assurance-execution/assurance_execution/operations/agent_skills.py`
- Modify: `packages/assurance-execution/tests/test_selection.py`
- Modify: `packages/assurance-execution/tests/test_execution_characterization.py`
- Create: `tests/phase5/test_candidate_execution_isolation.py`
- Create: `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/task-9-report.md`

**Interfaces:** `ExecutionView(batch_id, root, selected_targets, digest)`; project application imports remain rooted at original SUT, while test discovery roots point at `.staging/execution/<batch-id>`.

- [ ] Add tests proving candidate files shadow existing tests in the disposable view, unchanged existing tests remain selected, application code is not copied, unselected/closed-mapping tests are not executed, pytest caches and Hypothesis storage stay outside the SUT, conflicts block view creation, and cleanup does not delete canonical evidence.
- [ ] Run focused tests and confirm current runners assume generated files already exist under original `tests/**`.
- [ ] Implement the execution view by copying/linking only selected existing test/support files and materializing validated candidate files. Never copy application source. Pass explicit `--rootdir`, config, import path, selected node IDs, cache disablement, and Hypothesis storage to runners.
- [ ] Make execution evidence canonical only after runner output parses and validates. A runner crash leaves the view disposable and no new canonical evidence.
- [ ] Run `uv run pytest packages/assurance-execution/tests tests/phase5/test_execution_view.py tests/phase5/test_candidate_execution_isolation.py -q`; commit with `git commit -m "feat(execution): run tests from candidate execution views"`.

## Task 10: Make Achieved the Terminal Full-workflow State

**Files:**
- Modify: `packages/assurance-product/assurance_product/models.py`
- Modify: `packages/assurance-product/assurance_product/status.py`
- Modify: `packages/assurance-product/assurance_product/resources/workflow/assurance-full.yaml`
- Modify: `packages/assurance-product/assurance_product/resources/schemas/product-input-v1.json`
- Modify: `packages/assurance-product/assurance_product/resources/schemas/status-v1.json`
- Modify: `tests/phase5/test_product_input.py`
- Modify: `tests/phase5/test_archive_retro_improvement.py`
- Modify: `tests/phase5/test_full_graph_audit.py`
- Create: `tests/phase5/test_achieved_terminal.py`
- Create: `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/task-10-report.md`

**Interfaces:** `ProductInputV1` has no `auto_archive`; `StatusV1` has change/apply/publish projections and no tree IDs; `full` ends at `achieved` after required Eval/Retro/Improvement nodes.

- [ ] Change tests first to reject `auto_archive`, reject tree fields, require `publication.status` in `not_ready|ready|published|drifted`, and assert the full graph contains no archive node/branch but still contains the required Retro and Improvement paths.
- [ ] Run focused Phase 5 tests and observe current archive gate and tree status failures.
- [ ] Remove `auto_archive`, `archive-gate`, `skip-archive-gate`, and the archive subgraph edge from `full`. Add the deterministic achieved finalizer that writes `status.json` and `apply-manifest.json` only after merged generation and execution/quality gates validate.
- [ ] Keep archive as a separately compiled entrypoint/command; it is not reachable from `full`.
- [ ] Run `uv run pytest tests/phase5/test_product_input.py tests/phase5/test_archive_retro_improvement.py tests/phase5/test_full_graph_audit.py tests/phase5/test_achieved_terminal.py -q`; commit with `git commit -m "refactor(product): end full workflow at achieved"`.

## Task 11: Replace Whole-tree Export with Achieved File Publication

**Files:**
- Rewrite: `packages/assurance-product/assurance_product/export.py`
- Modify: `packages/assurance-product/assurance_product/models.py`
- Delete: `packages/assurance-product/assurance_product/resources/schemas/result-export-v1.json`
- Create: `packages/assurance-product/assurance_product/resources/schemas/publish-receipt-v1.json`
- Modify: `packages/assurance-product/assurance_product/cli.py`
- Rewrite: `tests/phase5/test_result_export.py`
- Rewrite: `tests/phase5/test_cli_export.py`
- Rewrite: `tests/phase5/test_export_security.py`
- Create: `tests/phase5/test_publish_recovery.py`
- Create: `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/task-11-report.md`

**Interfaces:** `aa export --project-dir PROJECT [--change CHANGE]`; `PublishReceiptV1`; `PublishJournalV1`. There is no destination argument and no result-tree directory.

- [ ] Add failing tests for non-achieved rejection, exact manifest publication, no unlisted writes/deletes, source digest mismatch, target-baseline drift, symlink/hard-link/special-file rejection, adjacent-temp atomic replace, crash after every journal transition, resume/rollback, repeated idempotent export, and already-matching target acceptance.
- [ ] Run export tests and observe current `SnapshotStore`/`materialize_snapshot` behavior.
- [ ] Implement `publish_achieved()` around the closed apply manifest. Journal phases are exactly `prepared`, `replacing`, `committed`, `rolled_back`; each record binds change ID, manifest digest, source digest, target baseline, temp/backup identity, and final digest. Do not capture or materialize any other project file.
- [ ] Change CLI selection: explicit `--change` wins; otherwise exactly one active achieved unpublished change is required. Zero or multiple candidates fail with a concise list and no mutation.
- [ ] Run `uv run pytest tests/phase5/test_result_export.py tests/phase5/test_cli_export.py tests/phase5/test_export_security.py tests/phase5/test_publish_recovery.py -q`; commit with `git commit -m "feat(product): publish achieved change file sets"`.

## Task 12: Require Publication before Explicit Archive

**Files:**
- Modify: `packages/assurance-improvement/assurance_improvement/operations/archive.py`
- Modify: `packages/assurance-product/assurance_product/cli.py`
- Modify: `packages/assurance-product/assurance_product/status.py`
- Modify: `packages/assurance-improvement/tests/test_delivery.py`
- Create: `tests/phase5/test_archive_after_publish.py`
- Create: `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/task-12-report.md`

**Interfaces:** `aa archive --project-dir PROJECT --change CHANGE`; archive authenticates `publish-receipt.json` and never writes generated files to the SUT.

- [ ] Add tests that archive before publish fails unchanged, invalid receipt fails, successful archive preserves the complete change record under `qa/archive/<id>`, and archive never performs publication.
- [ ] Run the focused tests and observe current archive operation can run inside `full` without publication proof.
- [ ] Implement receipt authentication and explicit CLI routing. Use atomic directory movement only on the same filesystem; otherwise copy to a temporary archive directory, fsync/verify all canonical files, atomically rename, then remove the active change.
- [ ] Run `uv run pytest packages/assurance-improvement/tests/test_delivery.py tests/phase5/test_archive_after_publish.py -q`; commit with `git commit -m "feat(product): gate archive on publish receipt"`.

## Task 13: Re-root Product CLI and Runtime State into the Change

**Files:**
- Modify: `packages/assurance-product/assurance_product/cli.py`
- Modify: `packages/assurance-product/assurance_product/product.py`
- Modify: `packages/assurance-product/assurance_product/status.py`
- Modify: `tests/phase5/cli_support.py`
- Modify: `tests/phase5/product_runner.py`
- Modify: `tests/phase5/test_cli_status_and_lock.py`
- Modify: `tests/phase5/test_stop_and_interrupts.py`
- Create: `tests/phase5/test_change_runtime_layout.py`
- Create: `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/task-13-report.md`

**Interfaces:** start/run/status/resume receive project + change; engine runtime is always `qa/changes/<id>/.runtime`; task attempts are always `.staging/<node>/<attempt>`; no user-facing `--engine-root` for Assurance.

- [ ] Add CLI tests proving two changes are independently discoverable, status/resume authenticate the change-local ledger, failed runs expose status/events but not staged files, and no `workspace`, `trees`, `attempts`, or `HEAD.json` directory is created.
- [ ] Run focused CLI tests and observe current explicit engine root and snapshot expectations.
- [ ] Make `ChangeWorkspace` the only constructor of runtime paths. `aa workflow run` initializes the change and engine binding; `status`, `resume`, `export`, and `archive` reopen and authenticate the same paths.
- [ ] Ensure `events.jsonl` and `status.json` are projections written from ledger state and never used to advance execution.
- [ ] Run `uv run pytest tests/phase5/test_cli_status_and_lock.py tests/phase5/test_stop_and_interrupts.py tests/phase5/test_change_runtime_layout.py -q`; commit with `git commit -m "refactor(cli): store assurance runtime inside each change"`.

## Task 14: Make the Benchmark Use the Real SUT and Change Result

**Files:**
- Rewrite: `benchmark/assurance-product-phase5/run_item.py`
- Modify: `benchmark/assurance-product-phase5/manifest.json`
- Modify: `benchmark/assurance-product-phase5/run-opencode.sh`
- Create: `benchmark/assurance-product-phase5/run-cursor.sh`
- Modify: `tests/phase5/test_phase5_benchmark_manifest.py`
- Create: `tests/phase5/test_phase5_benchmark_change_layout.py`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/opencode-benchmark.md`
- Create: `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/task-14-report.md`

**Interfaces:** a benchmark run derives a unique deterministic change ID from item ID + run timestamp/nonce, invokes the real SUT path, stores only harness logs/timing under `results/<run>`, and calls `aa export` only after achieved success.

- [ ] Add tests using a temporary SUT to assert: no `_copy_sut`, `results/<run>/project`, `results/<run>/export`, workspace tree, HEAD, latest pointer, or result registry; the result is `sut/qa/changes/<id>`; failure leaves original tests unchanged; success invokes export once.
- [ ] Run benchmark tests and observe current project-copy/export-root behavior.
- [ ] Remove `_SUT_COPY_IGNORE`, `_copy_sut`, seed/export-tree validation, and `auto_archive` input. Do not install OpenCode configuration during a live run: preflight the product-installed locked profiles and fail if they are absent or drifted.
- [ ] Update run evidence to record `sut_root`, `change_id`, `change_root`, terminal status, publish receipt, provider session/process reference, and logs. These diagnostics are not an alternate result source.
- [ ] Run `uv run pytest tests/phase5/test_phase5_benchmark_manifest.py tests/phase5/test_phase5_benchmark_change_layout.py -q`; commit with `git commit -m "refactor(benchmark): use change-local live results"`.

## Task 15: Simplify OpenChamber to Direct Change Discovery

**Repository:** `/Users/lvqingquan/agent/assurance-agent/.worktrees/openchamber-qa-latest-results`

**Files:**
- Delete: `packages/web/server/lib/testing/latest-run-bridge.js`
- Delete: `packages/web/server/lib/testing/latest-run-bridge.test.js`
- Modify: `packages/web/server/lib/opencode/openchamber-routes.js`
- Modify: `packages/web/server/lib/testing/qa-bridge.js`
- Modify: `packages/web/server/lib/testing/tests-bridge.js`
- Modify: `packages/web/server/lib/testing/report-bridge.js`
- Modify: `packages/web/server/lib/testing/execution-results.js`
- Modify: `packages/web/server/lib/testing/DOCUMENTATION.md`
- Modify: `packages/ui/src/qa/QAPanel.tsx`
- Modify: `packages/ui/src/qa/QAChangeToolbar.tsx`
- Modify: `packages/ui/src/qa/QAChangeToolbar.test.ts`
- Modify: `packages/ui/src/qa/lib/qaApi.ts`
- Modify: `packages/ui/src/qa/lib/qaApi.test.ts`
- Delete: `packages/ui/src/qa/sections/LatestRunSummaryCard.tsx`
- Delete: `packages/ui/src/qa/sections/LatestRunSummaryCard.test.tsx`
- Modify: `packages/ui/src/qa/sections/OverviewSection.tsx`
- Modify: `packages/ui/src/qa/stores/useQAPanelStore.ts`
- Modify: `packages/ui/src/qa/stores/useQAPanelStore.test.ts`
- Modify: `packages/ui/src/lib/i18n/messages/en.ts`
- Modify: `packages/ui/src/lib/i18n/messages/es.ts`
- Modify: `packages/ui/src/lib/i18n/messages/fr.ts`
- Modify: `packages/ui/src/lib/i18n/messages/ja.ts`
- Modify: `packages/ui/src/lib/i18n/messages/ko.ts`
- Modify: `packages/ui/src/lib/i18n/messages/pl.ts`
- Modify: `packages/ui/src/lib/i18n/messages/pt-BR.ts`
- Modify: `packages/ui/src/lib/i18n/messages/uk.ts`
- Modify: `packages/ui/src/lib/i18n/messages/zh-CN.ts`
- Modify: `packages/ui/src/lib/i18n/messages/zh-TW.ts`

**Interfaces:** `GET /api/qa/changes?dir=<project>` scans exactly `<project>/qa/changes/*`; selected change endpoints receive project root + change ID and ignore `.runtime`/`.staging` unless a dedicated diagnostics view asks for ledger evidence.

- [ ] Add server tests with two changes (one achieved, one failed with dirty staging) proving both appear, sorting uses typed status/event time, canonical cases/report/execution load, staging is hidden, malformed artifacts surface an error on that change, and no latest/result-root fallback occurs.
- [ ] Run the focused server tests and observe the feature branch still exposes `/api/qa/latest-run` and a latest benchmark card.
- [ ] Delete the latest-run bridge/route/card/query/i18n strings. Reuse and harden the existing `listQaChanges(dir)` scan; do not add a fixed list file, pointer file, benchmark root config, or historical compatibility reader.
- [ ] Run `bunx vitest run packages/web/server/lib/testing/qa-bridge.test.js packages/web/server/lib/testing/tests-bridge.test.js packages/web/server/lib/testing/report-bridge.test.js` plus the focused UI QA tests.
- [ ] Run OpenChamber `bun run lint`, `bun run typecheck`, and `bun run build`; stage only OpenChamber Task 15 files and commit there with `git commit -m "refactor(qa): read assurance changes directly"`.

## Task 16: Delete Snapshot/HEAD and Whole-tree Export Residuals

**Files:**
- Delete: `packages/graph-engine/graph_engine/runtime/tree_io.py`
- Delete: `packages/graph-engine/graph_engine/runtime/workspace.py`
- Delete: `packages/graph-engine/tests/runtime/test_tree_io.py`
- Delete: `packages/graph-engine/tests/runtime/test_tree_io_faults.py`
- Delete: `packages/graph-engine/tests/runtime/test_workspace.py`
- Modify: `packages/graph-engine/graph_engine/__init__.py`
- Modify: `packages/graph-engine/graph_engine/runtime/__init__.py`
- Modify: `packages/graph-engine/graph_engine/__main__.py`
- Modify: `packages/graph-engine/tests/composition/test_plugin_contracts.py`
- Modify: `packages/graph-engine/tests/integration/test_toy_b.py`
- Modify: `packages/graph-engine/tests/runtime/activity-events-v2.golden.json`
- Modify: `packages/graph-engine/tests/runtime/bootstrap_fixtures.py`
- Modify: `packages/graph-engine/tests/runtime/test_activity_cancellation.py`
- Modify: `packages/graph-engine/tests/runtime/test_activity_fold.py`
- Modify: `packages/graph-engine/tests/runtime/test_activity_models.py`
- Modify: `packages/graph-engine/tests/runtime/test_activity_recovery.py`
- Delete: `packages/graph-engine/tests/runtime/test_attempt_workspace_identity.py`
- Modify: `packages/graph-engine/tests/runtime/test_host_receipts.py`
- Modify: `packages/graph-engine/tests/runtime/test_invocation_lock.py`
- Modify: `packages/graph-engine/tests/runtime/test_invocation_seed.py`
- Modify: `packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py`
- Modify: `packages/graph-engine/tests/runtime/test_planner.py`
- Modify: `packages/graph-engine/tests/runtime/test_production_host_security.py`
- Modify: `packages/assurance-product/assurance_product/cli.py`
- Modify: `packages/assurance-product/assurance_product/export.py`
- Modify: `packages/assurance-product/assurance_product/models.py`
- Modify: `packages/assurance-product/assurance_product/status.py`
- Modify: `tests/phase5/cli_support.py`
- Modify: `tests/phase5/test_cli_export.py`
- Modify: `tests/phase5/test_cli_status_and_lock.py`
- Modify: `tests/phase5/test_export_security.py`
- Modify: `tests/phase5/test_result_export.py`
- Modify: `tests/phase5/test_product_packaging.py`
- Modify: `scripts/assurance_product_wheel_smoke_test.sh`
- Create: `tests/phase5/test_no_whole_tree_residuals.py`
- Create: `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/task-16-report.md`

**Interfaces:** no exported `SnapshotStore`, `WorkspaceSeed`, `capture_workspace_seed`, `materialize_snapshot`, `ResultExportV1`, tree ID, or HEAD result field remains unless a separately installed non-Assurance product has a committed test proving real use.

- [ ] Add behavioral/import/package tests proving removed public imports fail, built wheels omit the deleted tree/workspace modules and result-export schema, status/event schemas expose no tree IDs, and a real toy plus Assurance invocation creates no `workspace/trees`, `HEAD.json`, or whole-tree export. Do not grep source text or assert human documentation wording.
- [ ] Run the residual test and record every current failure in the task report.
- [ ] Delete obsolete implementation and tests rather than wrapping them in compatibility aliases. Update toy engine examples to use the generic dual-root workspace with an empty write attempt.
- [ ] Run `uv run pytest packages/graph-engine/tests tests/phase5/test_no_whole_tree_residuals.py tests/phase5/test_product_packaging.py -q` and `bash scripts/assurance_product_wheel_smoke_test.sh`.
- [ ] Commit with `git commit -m "refactor(engine): remove whole-tree workspace residuals"`.

## Task 17: Full Verification and Live Single-item Acceptance

**Files:**
- Create: `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/acceptance.md`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/opencode-benchmark.md`

- [ ] Run the focused acceptance matrix:

```bash
uv run pytest packages/graph-engine/tests -q
uv run pytest packages/agent-runtime-contracts/tests packages/agent-runtime-opencode/tests packages/agent-runtime-cursor/tests -q
uv run pytest packages/assurance-intake/tests packages/assurance-generation/tests packages/assurance-execution/tests packages/assurance-quality/tests packages/assurance-healing/tests packages/assurance-improvement/tests tests/phase5 -q
uv run pytest tests/phase4 tests/phase5 -q
```

- [ ] Run the repository gates in the Assurance worktree:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest
bash scripts/packaging_smoke_test.sh
bash scripts/assurance_product_wheel_smoke_test.sh
```

- [ ] Before live execution, record `git status --short`, the selected SUT, item ID, generated change ID, expected provider/model, original SUT test-tree digest, and empty/new change path. Do not clean or reset user changes.
- [ ] Run one single-item OpenCode benchmark from `benchmark/assurance-product-phase5/run-opencode.sh`, follow it through terminal state, and record the provider session link/reference. If the provider is blocked by quota/auth/network, report it as an external blocker; do not substitute credentials or claim workflow acceptance.
- [ ] On achieved success, verify before export that original SUT test-tree digest is unchanged, `.staging` contains no canonical evidence, all selected families have closed manifests, execution evidence passed, Eval/Retro/Improvement required by the graph ran, and `apply-manifest.json` authenticates.
- [ ] Invoke `aa export`, verify only manifest targets changed, verify the publish receipt, invoke export again to prove idempotency, and confirm OpenChamber lists the change directly from `qa/changes/*` with workflow/cases/execution/report views.
- [ ] Run OpenChamber server/UI focused tests and its lint/type/build gates in its own worktree. Capture exact commands and results in `acceptance.md`.
- [ ] Use `superpowers:requesting-code-review`, resolve every P1/P2 finding, rerun affected tests, then use `superpowers:verification-before-completion` before the final completion claim.
- [ ] Commit acceptance evidence only after all non-external gates pass: `git commit -m "test(workspace): verify change-local assurance flow"`.

## Completion Definition

Implementation is complete only when all of the following are simultaneously true:

1. Workflow execution reads the real SUT and cannot write outside the authenticated attempt stage.
2. Failed or retried tasks do not change canonical change artifacts or original SUT files.
3. Successful task outputs use per-file atomic replacement and batch-level durable intent/receipt with fail-closed replay; engine-managed readers see the batch only after its completed receipt and terminal event. Generated test families remain physically isolated.
4. Candidate tests execute before publication from a disposable, closed-mapping view.
5. `full` terminates at achieved and contains no automatic archive path.
6. `aa export` publishes exactly the achieved apply manifest, detects drift, recovers from interruption, and is idempotent.
7. The benchmark has no project copy, tree/HEAD workspace, or whole-tree export and uses its change directory as the result.
8. OpenChamber scans `qa/changes/*` directly with no latest-run/result-root bridge.
9. Graph-engine contains no Assurance business meaning and no unused whole-tree result machinery.
10. Focused tests, full CI gates, packaging gates, one live OpenCode single-item run, export verification, and OpenChamber verification are recorded with observed evidence.
