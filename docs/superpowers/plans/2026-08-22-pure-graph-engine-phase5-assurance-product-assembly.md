# Pure Graph Engine Phase 5 Assurance Product Assembly Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Assemble the six Phase 4 assurance capability wheels, the OpenCode/Cursor runtime adapters, authenticated deployment bindings, project configuration, a canonical full workflow, `aa-next`, and an isolated legacy comparison harness into one production-usable assurance product without adding assurance semantics to `graph-engine` or cutting over legacy `aa`.

**Architecture:** Extend `graph-engine` only with generic invocation seeding, input projection, secret authorization, and a fixed production task host. Build one independent `assurance-product` distribution with two product entry points that select either the OpenCode or Cursor adapter while sharing the same canonical workflow and six capability wheels; deployment authority comes from a generated, source-authenticated binding wheel, while business configuration remains data-only. Keep legacy and new runtimes isolated, compare them through exported behavioral projections, and leave all cutover and deletion work to Phase 6.

**Tech Stack:** Python 3.11, uv workspace, Pydantic v2 frozen models, YAML/JSON declarative resources, Python entry points, wheel/build isolation, pytest/pytest-asyncio/Hypothesis, Ruff, Pyright, import-linter, POSIX process groups on macOS and process supervision on Linux.

**Spec:** `docs/superpowers/specs/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly-design.md`

## Global Constraints

- Work only in the isolated worktree on branch `codex/pure-graph-engine-phase3-spec`; create a fresh execution worktree with `superpowers:using-git-worktrees` before implementing if this worktree is no longer isolated.
- The Phase 5 spec is authoritative; when this plan and the spec differ, stop and amend the plan before implementing.
- `graph-engine` remains business-agnostic, contains no default graph, and exposes no host plugin, public resource port, business callback, SUT scanning, or project-loaded executable extension point.
- The engine API is exactly `2.0`; the six assurance capability wheels and both runtime adapter wheels are exactly `0.1.0`.
- The product distribution is `assurance-product`, import package `assurance_product`, with exactly two `graph_engine.products` entry points: `assurance-opencode` and `assurance-cursor`.
- Phase 5 adds temporary `aa-next`; it does not replace, redirect, alias, deprecate, or delete legacy `aa`. Phase 6 alone owns cutover and deletion.
- Legacy and new runtimes never import one another and share no invocation directory, ledger, checkpoint, mutable workspace, lock, session store, or state bridge; comparison is external and consumes immutable exports only.
- Deployment authority is one installed, source-authenticated binding wheel with plugin ID/version `assurance.product.bindings==1.0.0`, entry-point group/name `graph_engine.plugins` / `deployment`.
- `aa-next bindings build` accepts only the closed `DeploymentBindingsV1` manifest, emits a digest-named deterministic wheel, embeds no secret values or arbitrary Python, and has no environment, endpoint, executable, permission, model, adapter, or secret fallback.
- Project configuration is exactly the data-only plugin `assurance.product.configuration==1.0.0`; it may contain business policy, knowledge, catalogs, and node configuration, but never runtime authority.
- The canonical binding surface contains exactly 33 `*.prepare` IDs and exactly 99 prepare/execute/finalize aliases; all 33 finalize bindings have JSON `null` binding data.
- The root input is exactly `ProductInputV1`; invocation start captures one stable SUT seed and never rereads ambient source state during replay or resume.
- Runtime event schema v2 bootstraps atomically with exactly `InvocationStarted`, `GraphStarted`, and the entry `TokenOffered`; crashes at every boundary must resume to the same authenticated three-event prefix.
- Secret values are runtime-only. Compositions, locks, events, snapshots, task requests, receipts, logs, exports, and comparison artifacts may contain authorized handles but never resolved bytes.
- Production execution uses the engine-owned fixed task host. Cursor receives a real confined process host. Production is supported on Linux and macOS only; Windows fails closed before dispatch.
- Status JSON is the authoritative workflow projection. Provider conversations remain provider-owned evidence and are not treated as engine history.
- External Eval is not inserted into the canonical workflow; Retro and Improvement remain workflow nodes and must run where the graph requires them.
- Every task follows RED/GREEN TDD, writes `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/task-N-report.md`, requests code review with `superpowers:requesting-code-review`, resolves all P1/P2 findings, and commits only its declared files.
- Preserve these verified pre-implementation baselines: `uv run pytest packages/graph-engine/tests -q` => `1145 passed, 1 skipped`; `uv run pytest packages/agent-runtime-contracts/tests packages/agent-runtime-opencode/tests packages/agent-runtime-cursor/tests tests/phase4 -q` => `585 passed` with the existing `CompiledWorkflow.schema` warning.
- Before Phase 5 acceptance, run `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run lint-imports`, `uv run pytest`, and every packaging/isolation smoke script named in Task 26.

---

## File Responsibility Map

### Phase evidence and conformance

- `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/ownership.yaml`: closed ownership ledger for every Phase 5 requirement and forbidden dependency.
- `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/residuals.yaml`: explicit legacy residuals allowed until Phase 6, with owner and deletion proof.
- `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/graph-inventory.yaml`: exact entrypoint, node, edge, loop, interrupt, STOP, Retro, and Improvement inventory.
- `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/binding-coverage.json`: exact 33 prepare IDs, 99 aliases, providers, targets, resource IDs, secret handles, and binding-data disposition.
- `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/comparison-dispositions.yaml`: exact 25-case comparison matrix and governed mismatch dispositions.
- `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/phase6-handoff.md`: mechanically checked Phase 6 freeze, drain, cutover, and deletion inputs.
- `tests/phase5/conformance.py`: reusable loaders and closed-set assertions for Phase 5 evidence.
- `tests/phase5/ownership.py`: production-path import and distribution ownership checks.

### Generic graph-engine additions

- `packages/graph-engine/graph_engine/runtime/seed.py`: public frozen invocation seed and stable source-file models.
- `packages/graph-engine/graph_engine/runtime/tree_io.py`: source capture and immutable snapshot export with path/symlink safety.
- `packages/graph-engine/graph_engine/runtime/secret_sources.py`: runtime-only secret source and invocation authorization models.
- `packages/graph-engine/graph_engine/runtime/production_host.py`: private fixed production task execution host and public engine factory seam.
- `packages/graph-engine/graph_engine/runtime/production_worker.py`: authenticated subprocess worker protocol used by the fixed host.
- `packages/graph-engine/graph_engine/runtime/events.py`: schema-v2 authenticated bootstrap fields.
- `packages/graph-engine/graph_engine/runtime/invocation_lock.py`: schema-v2 start intent and authorization digest binding.
- `packages/graph-engine/graph_engine/runtime/engine.py`: atomic seeded start/open and production engine construction.
- `packages/graph-engine/graph_engine/runtime/models.py`: projection of root input, seed, and authorization identity.
- `packages/graph-engine/graph_engine/runtime/planner.py`: planned task inputs derived from the closed projection language.
- `packages/graph-engine/graph_engine/runtime/scheduler.py`: task requests carrying projected input, binding data, resource claims, and authorized secret handles.
- `packages/graph-engine/graph_engine/graph/input_projection.py`: closed declarative input-projection AST and evaluator.
- `packages/graph-engine/graph_engine/graph/schema.py`: `NodeDef.input_projection` declaration.
- `packages/graph-engine/graph_engine/graph/compiler.py`: validation and freezing of compiled input projections.
- `packages/graph-engine/graph_engine/plugin_api.py`: `CapabilityBindingContribution.secret_handles` and request-level handle propagation.

### Runtime adapters

- `packages/agent-runtime-opencode/agent_runtime_opencode/config.py`: closed binding-data schema for OpenCode runtime authority.
- `packages/agent-runtime-opencode/agent_runtime_opencode/handler.py`: per-request binding parsing and authorized secret lookup.
- `packages/agent-runtime-cursor/agent_runtime_cursor/config.py`: closed binding-data schema for Cursor runtime authority.
- `packages/agent-runtime-cursor/agent_runtime_cursor/handler.py`: per-request binding parsing and real process-host use.
- `packages/agent-runtime-cursor/agent_runtime_cursor/process.py`: Linux/macOS confined process host and durable terminal evidence.

### Assurance product

- `packages/assurance-product/pyproject.toml`: independent distribution, exact dependencies, scripts, and two product entry points.
- `packages/assurance-product/assurance_product/models.py`: `ProductInputV1`, project configuration, deployment manifest, status, and export models.
- `packages/assurance-product/assurance_product/source_catalog.py`: exact installed provider coordinates and source expectations.
- `packages/assurance-product/assurance_product/product.py`: two product providers and deterministic composition requests.
- `packages/assurance-product/assurance_product/binding_builder.py`: deterministic data-only deployment wheel generation.
- `packages/assurance-product/assurance_product/configuration.py`: data-only project ConfigTree loading and contribution.
- `packages/assurance-product/assurance_product/status.py`: authoritative workflow projection for humans and JSON consumers.
- `packages/assurance-product/assurance_product/export.py`: immutable result export and behavioral projection.
- `packages/assurance-product/assurance_product/cli.py`: all `aa-next` commands and fail-closed argument routing.
- `packages/assurance-product/assurance_product/resources/declarations/*.yaml`: product, deployment, and configuration declarations.
- `packages/assurance-product/assurance_product/resources/workflow/assurance-full.yaml`: canonical full graph.
- `packages/assurance-product/assurance_product/resources/schemas/*.json`: closed product input, deployment, configuration, status, and export schemas.

### Verification, comparison, and benchmarks

- `tests/phase5/`: focused engine, adapter, product, graph, CLI, export, comparison, fault, replay, and security tests.
- `tests/phase5/fixtures/`: exact deployment manifests, project ConfigTrees, source trees, installed-wheel roots, and deterministic provider fixtures.
- `benchmark/assurance-product-phase5/manifest.json`: two provider-live single-item benchmark definitions and model policy.
- `benchmark/assurance-product-phase5/run-opencode.sh`: isolated OpenCode full-workflow benchmark.
- `benchmark/assurance-product-phase5/run-cursor.sh`: isolated Cursor full-workflow benchmark.
- `benchmark/assurance-product-phase5/compare.py`: external immutable-export comparison runner.
- `scripts/assurance_product_wheel_smoke_test.sh`: committed-HEAD build/install/source-authentication matrix.

---

### Task 1: Freeze Phase 5 Ownership and Acceptance Ledgers

**Files:**
- Create: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/ownership.yaml`
- Create: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/residuals.yaml`
- Create: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/graph-inventory.yaml`
- Create: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/binding-coverage.json`
- Create: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/comparison-dispositions.yaml`
- Create: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/phase6-handoff.md`
- Create: `tests/phase5/__init__.py`
- Create: `tests/phase5/conformance.py`
- Create: `tests/phase5/ownership.py`
- Create: `tests/phase5/test_phase5_ledgers.py`

**Interfaces:**
- Consumes: Phase 5 spec Sections 3, 5, 14, 15, 18, 24, and 25.
- Produces: `load_yaml(path: Path) -> dict[str, object]`, `load_json(path: Path) -> dict[str, object]`, `PREPARE_IDS: tuple[str, ...]`, `ALL_BINDING_IDS: tuple[str, ...]`, and closed evidence ledgers used by Tasks 10-27.

- [ ] **Step 1: Write the failing ledger-conformance test**

```python
from tests.phase5.conformance import ALL_BINDING_IDS, PREPARE_IDS, load_json, load_yaml


def test_phase5_ledgers_are_closed_and_exact(evidence_root):
    ownership = load_yaml(evidence_root / "ownership.yaml")
    bindings = load_json(evidence_root / "binding-coverage.json")
    comparisons = load_yaml(evidence_root / "comparison-dispositions.yaml")
    assert ownership["engine_api"] == "2.0"
    assert len(PREPARE_IDS) == 33
    assert len(ALL_BINDING_IDS) == 99
    assert set(bindings) == set(ALL_BINDING_IDS)
    assert all(bindings[item]["data"] is None for item in ALL_BINDING_IDS if item.endswith(".finalize"))
    assert len(comparisons["cases"]) == 25
```

- [ ] **Step 2: Run the test and verify the missing-ledger failure**

Run: `uv run pytest tests/phase5/test_phase5_ledgers.py -q`

Expected: FAIL because `tests.phase5.conformance` and the evidence files do not exist.

- [ ] **Step 3: Add closed loaders, exact IDs, and ledgers**

```python
def load_yaml(path: Path) -> dict[str, object]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain a mapping")
    return value


PREPARE_IDS = (
    "assurance.intake.case-design.prepare",
    "assurance.intake.case-review.prepare",
    "assurance.intake.explore.prepare",
    "assurance.intake.intake.prepare",
    "assurance.generation.api.codegen-fix.prepare",
    "assurance.generation.api.codegen.prepare",
    "assurance.generation.api.plan-review.prepare",
    "assurance.generation.api.plan.prepare",
    "assurance.generation.e2e.codegen-fix.prepare",
    "assurance.generation.e2e.codegen.prepare",
    "assurance.generation.e2e.plan-review.prepare",
    "assurance.generation.e2e.plan.prepare",
    "assurance.generation.fuzz.codegen.prepare",
    "assurance.generation.fuzz.plan-review.prepare",
    "assurance.generation.fuzz.plan.prepare",
    "assurance.generation.performance.codegen.prepare",
    "assurance.generation.performance.plan-review.prepare",
    "assurance.generation.performance.plan.prepare",
    "assurance.execution.execute.prepare",
    "assurance.execution.run.prepare",
    "assurance.healing.coverage-repair.prepare",
    "assurance.healing.fix-proposal.prepare",
    "assurance.quality.fact-baseline.prepare",
    "assurance.quality.inspect.prepare",
    "assurance.quality.issue-analysis.prepare",
    "assurance.quality.issue-triage.prepare",
    "assurance.quality.report.prepare",
    "assurance.improvement.archive.prepare",
    "assurance.improvement.improvement-review.prepare",
    "assurance.improvement.retro-eval-analysis.prepare",
    "assurance.improvement.retro-issue-analysis.prepare",
    "assurance.improvement.retro-workflow-analysis.prepare",
    "assurance.improvement.retro.prepare",
)
ALL_BINDING_IDS = tuple(
    alias
    for prepare_id in PREPARE_IDS
    for alias in (
        "assurance.product.agent."
        + prepare_id.removeprefix("assurance.").removesuffix(".prepare")
        + phase
        for phase in (".prepare", ".execute", ".finalize")
    )
)
```

Write the initial ledgers from these closed projections; no wildcard owner, open-ended node family, unclassified residual, or unassigned comparison case is permitted:

```python
ownership = {
    "engine_api": "2.0",
    "engine_generic": ["invocation_seed", "input_projection", "runtime_authorization", "production_task_host"],
    "adapter_owned": ["opencode_binding", "cursor_binding", "cursor_process_host"],
    "product_owned": ["source_catalog", "composition", "workflow", "cli", "status", "export"],
    "external_harness_owned": ["behavioral_projection", "comparison_matrix", "provider_live_benchmarks"],
    "forbidden_bridges": ["legacy_import", "legacy_state", "sut_code_loading", "host_plugin", "runtime_fallback"],
}
residuals = {
    "allowed_until_phase6": ["legacy_aa_command", "legacy_runtime", "legacy_product", "comparison_baseline"],
    "new_runtime_dependencies_on_residuals": [],
    "compatibility_bridge": False,
}
binding_coverage = {
    alias: {
        "phase": alias.rsplit(".", 1)[1],
        "owner_task": 13,
        "data": None if alias.endswith(".finalize") else {"status": "planned"},
        "secret_handles": [],
    }
    for alias in ALL_BINDING_IDS
}
```

Initialize `graph-inventory.yaml` with exactly `full`, `intake`, `case`, `execute`, `archive`, `retro`, `issue-review`, `issue-analyze`, `issue-reconcile`, `improvement-review`, `improvement-evaluate`, `improvement-export`, `improvement-apply`, and `improvement-rollback`, plus empty closed `nodes`/`edges` arrays owned for incremental replacement by Tasks 14-18. Initialize `comparison-dispositions.yaml` with the exact 25 literal IDs printed in Task 22, each with `status: planned`, `mode: exact`, and no wildcard field rule. Task 13 replaces every non-finalize planned binding record with its authenticated target/data/resource/secret projection.

- [ ] **Step 4: Run focused and ownership tests**

Run: `uv run pytest tests/phase5/test_phase5_ledgers.py tests/phase5/ownership.py -q`

Expected: PASS with 33 prepare IDs, 99 aliases, and 25 comparison cases.

- [ ] **Step 5: Review and record evidence**

Use `superpowers:requesting-code-review`; record the exact commands, counts, and resolved findings in `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/task-1-report.md`.

- [ ] **Step 6: Commit**

```bash
git add tests/phase5
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/ownership.yaml .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/residuals.yaml .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/graph-inventory.yaml .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/binding-coverage.json .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/comparison-dispositions.yaml .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/phase6-handoff.md
git commit -m "test(phase5): freeze product assembly ledgers"
```

### Task 2: Add Invocation Seed, Root Input, and Atomic Bootstrap v2

**Files:**
- Create: `packages/graph-engine/graph_engine/runtime/seed.py`
- Modify: `packages/graph-engine/graph_engine/runtime/events.py`
- Modify: `packages/graph-engine/graph_engine/runtime/invocation_lock.py`
- Modify: `packages/graph-engine/graph_engine/runtime/engine.py`
- Modify: `packages/graph-engine/graph_engine/runtime/models.py`
- Modify: `packages/graph-engine/graph_engine/runtime/__init__.py`
- Create: `packages/graph-engine/tests/runtime/test_invocation_seed.py`
- Create: `packages/graph-engine/tests/runtime/test_bootstrap_v2.py`

**Interfaces:**
- Consumes: `FrozenJSONValue`, `FrozenComposition`, `SnapshotStore`, and the existing authenticated event ledger.
- Produces: `SeedFile`, `WorkspaceSeed`, `InvocationSeed`, `Engine.start(composition, *, entrypoint: str, invocation_id: str, seed: InvocationSeed) -> InvocationHandle`, and schema-v2 `InvocationStarted`/`InvocationStartIntent` fields.

- [ ] **Step 1: Write failing frozen-model and three-event crash tests**

```python
def test_seed_models_reject_duplicate_and_unsafe_paths():
    with pytest.raises(ValidationError, match="relative canonical path"):
        SeedFile(path="../secret", sha256="0" * 64, content=b"x")


@pytest.mark.parametrize("boundary", range(4))
def test_start_recovers_to_one_authenticated_bootstrap_prefix(engine_factory, composition, seed, boundary):
    engine = engine_factory(fail_after_bootstrap_append=boundary)
    with contextlib.suppress(InjectedCrash):
        engine.start(composition, entrypoint="full", invocation_id="inv-1", seed=seed)
    recovered = engine_factory().open("inv-1", composition)
    events = recovered.ledger.read_all()
    assert [item.event.type for item in events[:3]] == ["invocation_started", "graph_started", "token_offered"]
    assert len([item for item in events if item.event.type == "invocation_started"]) == 1
```

- [ ] **Step 2: Run tests and verify missing-model/signature failures**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_invocation_seed.py packages/graph-engine/tests/runtime/test_bootstrap_v2.py -q`

Expected: FAIL because seed models and the required `seed=` start contract do not exist.

- [ ] **Step 3: Implement public frozen seed models and schema-v2 identities**

```python
@dataclass(frozen=True)
class SeedFile:
    path: str
    sha256: str
    content: bytes


@dataclass(frozen=True)
class WorkspaceSeed:
    schema_version: Literal["1"]
    tree_id: str
    files: tuple[SeedFile, ...]


@dataclass(frozen=True)
class InvocationSeed:
    schema_version: Literal["1"]
    root_input: JSONValue
    root_input_digest: str
    workspace: WorkspaceSeed
```

Recompute every file SHA-256, `tree_id`, and root-input digest during construction; reject caller-supplied mismatches. Extend `InvocationStarted` and `InvocationStartIntent` with exact `runtime_authorization_digest`, `root_input_digest`, `initial_tree_id`, and `event_schema_version: Literal["2"]` fields (temporarily use the canonical empty-authorization digest until Task 5). Put `seed.root_input` in `GraphStarted.input` and the initial token payload; set authoritative `head_tree_id` from `initial_tree_id` before later planning; write all three bootstrap events in one authenticated append batch and make retry authenticate the exact batch. Reject schema-v1 prototype invocation directories instead of adding an upgrade reader.

- [ ] **Step 4: Run focused and existing runtime tests**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_invocation_seed.py packages/graph-engine/tests/runtime/test_bootstrap_v2.py packages/graph-engine/tests/runtime/test_engine.py packages/graph-engine/tests/runtime/test_invocation_lock.py packages/graph-engine/tests/runtime/test_activity_fold.py -q`

Expected: PASS; existing callers use an explicit deterministic empty seed fixture rather than an implicit default.

- [ ] **Step 5: Review, report, and commit**

```bash
git add packages/graph-engine/graph_engine/runtime packages/graph-engine/tests/runtime/test_invocation_seed.py packages/graph-engine/tests/runtime/test_bootstrap_v2.py
git commit -m "feat(engine): authenticate invocation seeds at bootstrap"
```

Record review output in `task-2-report.md` before committing.

### Task 3: Capture Stable SUT Seeds and Export Immutable Result Trees

**Files:**
- Create: `packages/graph-engine/graph_engine/runtime/tree_io.py`
- Modify: `packages/graph-engine/graph_engine/runtime/__init__.py`
- Create: `packages/graph-engine/tests/runtime/test_tree_io.py`
- Create: `packages/graph-engine/tests/runtime/test_tree_io_faults.py`

**Interfaces:**
- Consumes: `SeedFile`, `WorkspaceSeed`, `SnapshotStore`, canonical SHA-256 helpers, and descriptor-relative path operations.
- Produces: `SeedCapturePolicy`, `ExportedFile`, `ExportManifest`, `capture_workspace_seed(source: Path, *, policy: SeedCapturePolicy) -> WorkspaceSeed`, and `materialize_snapshot(store: SnapshotStore, tree_id: str, destination: Path) -> ExportManifest`.

- [ ] **Step 1: Write failing capture, mutation, symlink, and export tests**

```python
def test_capture_is_stable_after_source_mutation(tmp_path):
    source = tmp_path / "sut"
    source.mkdir()
    (source / "app.py").write_text("before", encoding="utf-8")
    seed = capture_workspace_seed(source, policy=SeedCapturePolicy())
    (source / "app.py").write_text("after", encoding="utf-8")
    assert seed.files[0].content == b"before"


def test_capture_rejects_symlink(tmp_path):
    source = tmp_path / "sut"
    source.mkdir()
    (source / "escape").symlink_to(tmp_path)
    with pytest.raises(SeedCaptureError, match="symlink"):
        capture_workspace_seed(source, policy=SeedCapturePolicy())
```

- [ ] **Step 2: Run tests and verify the module is absent**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_tree_io.py packages/graph-engine/tests/runtime/test_tree_io_faults.py -q`

Expected: FAIL on import of `graph_engine.runtime.tree_io`.

- [ ] **Step 3: Implement descriptor-authenticated capture and atomic export**

```python
class SeedCapturePolicy(FrozenModel):
    excluded_names: tuple[str, ...] = (
        ".git", ".aa-runtime", ".venv", "__pycache__", ".pytest_cache",
        ".mypy_cache", ".ruff_cache", "benchmark-results", ".env",
    )
    maximum_file_count: int = 100_000
    maximum_file_bytes: int = 16 * 1024 * 1024
    maximum_total_bytes: int = 512 * 1024 * 1024


def capture_workspace_seed(source: Path, *, policy: SeedCapturePolicy) -> WorkspaceSeed:
    files = tuple(_capture_regular_files_beneath(source, policy))
    return WorkspaceSeed(schema_version="1", tree_id=_seed_tree_id(files), files=files)
```

Open the source root once, walk with directory descriptors, reject symlinks/special files/path drift, sort canonical POSIX paths, verify digest and size before closing each descriptor, and fsync the atomic export before rename. `materialize_snapshot` must fail if the destination exists or if any snapshot path is non-canonical.

- [ ] **Step 4: Run focused tests and property checks**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_tree_io.py packages/graph-engine/tests/runtime/test_tree_io_faults.py -q`

Expected: PASS, including mutation, symlink, duplicate-path, digest, interrupted-export, and deterministic-order cases.

- [ ] **Step 5: Review, report, and commit**

```bash
git add packages/graph-engine/graph_engine/runtime/tree_io.py packages/graph-engine/graph_engine/runtime/__init__.py packages/graph-engine/tests/runtime/test_tree_io.py packages/graph-engine/tests/runtime/test_tree_io_faults.py
git commit -m "feat(engine): capture and export immutable workspace trees"
```

### Task 4: Add the Closed Declarative Input Projection Language

**Files:**
- Create: `packages/graph-engine/graph_engine/graph/input_projection.py`
- Modify: `packages/graph-engine/graph_engine/graph/schema.py`
- Modify: `packages/graph-engine/graph_engine/graph/compiler.py`
- Modify: `packages/graph-engine/graph_engine/runtime/planner.py`
- Modify: `packages/graph-engine/graph_engine/runtime/models.py`
- Create: `packages/graph-engine/tests/graph/test_input_projection.py`
- Create: `packages/graph-engine/tests/runtime/test_planner_input_projection.py`

**Interfaces:**
- Consumes: invocation `root_input`, compiled node configuration, and predecessor token payloads.
- Produces: closed union `InputProjectionDef`, `NodeDef.input_projection: InputProjectionDef | None`, and `project_task_input(projection: InputProjectionDef, *, root_input: FrozenJSONValue, node_config: FrozenJSONValue, predecessor_tokens: Mapping[str, FrozenJSONValue]) -> FrozenJSONValue`.

- [ ] **Step 1: Write failing parse, evaluation, and rejection tests**

```python
def test_object_projection_combines_only_declared_sources():
    projection = ObjectProjection(fields={
        "change_id": RootPointerProjection(pointer="/change_id"),
        "policy": ConfigPointerProjection(pointer="/policy"),
        "inputs": AllPredecessorTokensProjection(),
    })
    assert project_task_input(
        projection,
        root_input={"change_id": "CH-1", "ignored": "x"},
        node_config={"policy": "strict"},
        predecessor_tokens={"left": {"a": 1}, "right": {"b": 2}},
    ) == {"change_id": "CH-1", "policy": "strict", "inputs": ({"a": 1}, {"b": 2})}


def test_projection_rejects_unknown_operator():
    with pytest.raises(ValidationError):
        NodeDef.model_validate({"id": "n", "kind": "task", "input_projection": {"type": "python", "callable": "x:y"}})
```

- [ ] **Step 2: Run tests and verify unknown-field/module failures**

Run: `uv run pytest packages/graph-engine/tests/graph/test_input_projection.py packages/graph-engine/tests/runtime/test_planner_input_projection.py -q`

Expected: FAIL because the projection AST and `NodeDef.input_projection` are absent.

- [ ] **Step 3: Implement the closed AST and total evaluator**

```python
class LiteralProjection(FrozenModel):
    type: Literal["literal"]
    value: FrozenJSONValue


class RootPointerProjection(FrozenModel):
    type: Literal["root_pointer"]
    pointer: JSONPointer


class ObjectProjection(FrozenModel):
    type: Literal["object"]
    fields: dict[str, "InputProjectionDef"]


InputProjectionDef = Annotated[
    LiteralProjection | RootPointerProjection | ConfigPointerProjection |
    PredecessorPointerProjection | PredecessorValueProjection |
    AllPredecessorTokensProjection | ObjectProjection | TupleProjection,
    Field(discriminator="type"),
]
```

The committed `NodeDef` edit adds this field beside all existing fields rather than replacing the class. Add `input_projection` to the allowed field set for task, subgraph, join, gate, and interrupt nodes; reject it on `end`. Define `PredecessorPointerProjection(type="predecessor_pointer", predecessor: str, pointer: JSONPointer)`, `PredecessorValueProjection(type="predecessor", predecessor: str)`, and `AllPredecessorTokensProjection(type="all_predecessor_tokens")`. Use RFC 6901 pointer semantics; reject missing pointers, duplicate object keys after normalization, non-direct predecessor names, invalid join cardinality, cycles, extra fields, and non-JSON values. The all-token form returns values sorted by predecessor node ID. Compile the AST into `CompiledNodeDefinition`; planner evaluation is deterministic and never imports or invokes product code. When `input_projection` is absent, preserve the existing `{config, tokens}` behavior only for toy/low-level graphs.

- [ ] **Step 4: Run graph/compiler/planner suites**

Run: `uv run pytest packages/graph-engine/tests/graph/test_input_projection.py packages/graph-engine/tests/runtime/test_planner_input_projection.py packages/graph-engine/tests/graph/test_schema_and_compiler.py packages/graph-engine/tests/runtime/test_planner.py -q`

Expected: PASS with every union variant and rejection class covered.

- [ ] **Step 5: Review, report, and commit**

```bash
git add packages/graph-engine/graph_engine/graph packages/graph-engine/graph_engine/runtime/planner.py packages/graph-engine/graph_engine/runtime/models.py packages/graph-engine/tests/graph packages/graph-engine/tests/runtime/test_planner_input_projection.py
git commit -m "feat(engine): add closed task input projections"
```

### Task 5: Bind Secret Handles to Invocation Runtime Authorization

**Files:**
- Create: `packages/graph-engine/graph_engine/runtime/secret_sources.py`
- Modify: `packages/graph-engine/graph_engine/plugin_api.py`
- Modify: `packages/graph-engine/graph_engine/composition/contributions.py`
- Modify: `packages/graph-engine/graph_engine/composition/models.py`
- Modify: `packages/graph-engine/graph_engine/composition/registries.py`
- Modify: `packages/graph-engine/graph_engine/composition/lock.py`
- Modify: `packages/graph-engine/graph_engine/runtime/events.py`
- Modify: `packages/graph-engine/graph_engine/runtime/invocation_lock.py`
- Modify: `packages/graph-engine/graph_engine/runtime/engine.py`
- Modify: `packages/graph-engine/graph_engine/runtime/scheduler.py`
- Create: `packages/graph-engine/tests/runtime/test_secret_authorization.py`
- Modify: `packages/graph-engine/tests/composition/test_plugin_contracts.py`

**Interfaces:**
- Consumes: Task 2 bootstrap v2, `CapabilityBindingContribution`, and the existing `SecretPort.resolve(handle: str) -> bytes`.
- Produces: `CapabilityBindingContribution.secret_handles: tuple[str, ...]`, `SecretSourceBinding`, `InvocationRuntimeAuthorization`, `Engine.start(composition, *, entrypoint: str, invocation_id: str, seed: InvocationSeed, authorization: InvocationRuntimeAuthorization) -> InvocationHandle`, and `Engine.open(invocation_id: str, composition: FrozenComposition, *, authorization: InvocationRuntimeAuthorization) -> InvocationHandle`.

- [ ] **Step 1: Write failing authorization and redaction tests**

```python
def test_task_receives_only_binding_authorized_handles(scheduler_fixture):
    binding = CapabilityBindingContribution(
        capability_id="runtime.opencode.execute",
        target="runtime.opencode",
        secret_handles=("opencode.token",),
    )
    call = scheduler_fixture(binding=binding).planned_host_call()
    assert call.authorized_secret_handles == ("opencode.token",)


def test_secret_bytes_never_serialize(tmp_path, authorization):
    engine = make_engine(tmp_path)
    engine.start(composition(), entrypoint="full", invocation_id="inv", seed=seed(), authorization=authorization)
    assert b"actual-secret" not in b"".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file() and not path.is_symlink())
```

- [ ] **Step 2: Run tests and verify missing-field/signature failures**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_secret_authorization.py packages/graph-engine/tests/composition/test_plugin_contracts.py -q`

Expected: FAIL because bindings lack `secret_handles` and engine start/open lack authorization.

- [ ] **Step 3: Implement runtime-only authorization**

```python
@dataclass(frozen=True)
class SecretSourceBinding:
    handle: str
    source_kind: Literal["environment", "file"]
    source_locator: str


@dataclass(frozen=True)
class InvocationRuntimeAuthorization:
    schema_version: Literal["1"]
    secret_sources: tuple[SecretSourceBinding, ...]
    digest: str

    def __post_init__(self) -> None:
        expected = canonical_digest(tuple((item.handle, item.source_kind, item.source_locator) for item in self.secret_sources))
        if self.digest != expected:
            raise ValueError("runtime authorization digest mismatch")
```

Validate unique canonical handles; authorize only handles declared by the selected binding; resolve values at dispatch inside the production host; store only the authorization digest in start intent/events. `open` requires an authorization whose digest matches the authenticated start, and any missing/extra/drifted handle fails before handler execution.

- [ ] **Step 4: Run plugin, composition, lock, scheduler, and security tests**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_secret_authorization.py packages/graph-engine/tests/composition/test_plugin_contracts.py packages/graph-engine/tests/composition packages/graph-engine/tests/runtime/test_scheduler.py -q`

Expected: PASS; serialized artifacts contain handles and digests only.

- [ ] **Step 5: Review, report, and commit**

```bash
git add packages/graph-engine/graph_engine packages/graph-engine/tests
git commit -m "feat(engine): authorize runtime-only secret handles"
```

### Task 6: Add the Fixed Engine-Owned Production Task Host

**Files:**
- Create: `packages/graph-engine/graph_engine/runtime/production_host.py`
- Create: `packages/graph-engine/graph_engine/runtime/production_worker.py`
- Modify: `packages/graph-engine/graph_engine/composition/lock.py`
- Modify: `packages/graph-engine/graph_engine/runtime/engine.py`
- Modify: `packages/graph-engine/graph_engine/runtime/host_protocol.py`
- Modify: `packages/graph-engine/graph_engine/runtime/host_receipts.py`
- Create: `packages/graph-engine/tests/runtime/test_production_host.py`
- Create: `packages/graph-engine/tests/runtime/test_production_host_faults.py`
- Create: `packages/graph-engine/tests/runtime/test_production_host_security.py`

**Interfaces:**
- Consumes: `FrozenComposition`, `InvocationRuntimeAuthorization`, authenticated task-host calls, attempt workspace descriptors, and installed handler registry.
- Produces: `Engine.production(root: Path, *, authorization: InvocationRuntimeAuthorization, clock: Clock | None = None) -> Engine`; `_ProductionTaskExecutionHost` remains private.

- [ ] **Step 1: Write failing production, crash, platform, and leakage tests**

```python
@pytest.mark.asyncio
async def test_production_engine_executes_installed_handler(tmp_path, installed_composition, authorization):
    engine = Engine.production(tmp_path, authorization=authorization)
    handle = engine.start(installed_composition, entrypoint="full", invocation_id="inv", seed=seed(), authorization=authorization)
    result = engine.run_until_blocked(handle)
    assert result.status == "completed"


def test_production_engine_rejects_windows(monkeypatch, tmp_path, authorization):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(UnsupportedProductionPlatform, match="Linux and macOS"):
        Engine.production(tmp_path, authorization=authorization)
```

- [ ] **Step 2: Run tests and verify the factory is absent**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_production_host.py packages/graph-engine/tests/runtime/test_production_host_faults.py packages/graph-engine/tests/runtime/test_production_host_security.py -q`

Expected: FAIL because `Engine.production` and the fixed host do not exist.

- [ ] **Step 3: Implement the private host and authenticated worker protocol**

```python
@classmethod
def production(
    cls,
    root: Path,
    *,
    authorization: InvocationRuntimeAuthorization,
    clock: Clock | None = None,
) -> "Engine":
    host = _ProductionTaskExecutionHost(root=root, authorization=authorization)
    return cls(root, task_host=host, clock=clock)
```

The worker request contains canonical handler identity, immutable task request, attempt-workspace identity, and only authorized secret descriptors. Use an authenticated length-prefixed protocol, process group isolation, durable terminal receipt written/fsynced before response, bounded stdout/stderr, cancellation escalation, and reconcile-from-receipt. Update `pinned_execution_host_lock()` so its implementation digest authenticates `production_host.py`, `production_worker.py`, `host_protocol.py`, and `host_receipts.py`, while retaining the explicit wire-schema version. Do not expose host construction through product/plugin declarations.

- [ ] **Step 4: Run production-host and existing recovery tests**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_production_host.py packages/graph-engine/tests/runtime/test_production_host_faults.py packages/graph-engine/tests/runtime/test_production_host_security.py packages/graph-engine/tests/runtime/test_activity_recovery.py packages/graph-engine/tests/runtime/test_host_receipts.py -q`

Expected: PASS for execute, reconcile, cancel, host crash before/after receipt, forged receipt, wrong workspace, secret redaction, and unsupported platform.

- [ ] **Step 5: Review, report, and commit**

```bash
git add packages/graph-engine/graph_engine/composition/lock.py packages/graph-engine/graph_engine/runtime packages/graph-engine/tests/runtime/test_production_host.py packages/graph-engine/tests/runtime/test_production_host_faults.py packages/graph-engine/tests/runtime/test_production_host_security.py
git commit -m "feat(engine): add fixed production task host"
```

### Task 7: Implement the Real Cursor Confined Process Host

**Files:**
- Modify: `packages/agent-runtime-cursor/agent_runtime_cursor/process.py`
- Modify: `packages/agent-runtime-cursor/agent_runtime_cursor/__init__.py`
- Create: `packages/agent-runtime-cursor/tests/test_process_host.py`
- Create: `packages/agent-runtime-cursor/tests/test_process_host_faults.py`
- Create: `packages/agent-runtime-cursor/tests/test_process_host_security.py`

**Interfaces:**
- Consumes: existing `ConfinedProcessHost`, `ProcessLaunchRequest`, `CursorProcessReceipt`, `HostTerminalResult`, and authenticated attempt workspace identity.
- Produces: `MacOSProcessGroupHost`, `LinuxProcessSupervisorHost`, and `production_process_host(root: Path) -> ConfinedProcessHost`.

- [ ] **Step 1: Write failing real-process lifecycle tests**

```python
@pytest.mark.asyncio
async def test_real_host_records_terminal_before_return(tmp_path):
    host = production_process_host(tmp_path)
    request = launch_request(argv=(sys.executable, "-c", "print('ok')"))
    process = await host.spawn(request)
    terminal = await host.wait(process.receipt)
    assert terminal.exit_code == 0
    assert host.read_durable_terminal(process.receipt) == terminal


@pytest.mark.asyncio
async def test_cancel_terminates_the_entire_process_group(tmp_path):
    host = production_process_host(tmp_path)
    process = await host.spawn(spawning_child_request())
    await host.terminate(process.receipt, CancelPolicy(grace_seconds=0.1))
    assert await no_recorded_pid_survives(process.receipt)
```

- [ ] **Step 2: Run tests and verify factory/class failures**

Run: `uv run pytest packages/agent-runtime-cursor/tests/test_process_host.py packages/agent-runtime-cursor/tests/test_process_host_faults.py packages/agent-runtime-cursor/tests/test_process_host_security.py -q`

Expected: FAIL because only the protocol and fake test implementations exist.

- [ ] **Step 3: Implement platform hosts with durable identity**

```python
def production_process_host(root: Path) -> ConfinedProcessHost:
    if sys.platform == "darwin":
        return MacOSProcessGroupHost(root)
    if sys.platform.startswith("linux"):
        return LinuxProcessSupervisorHost(root)
    raise UnsupportedCursorPlatform("Cursor execution supports Linux and macOS only")
```

Authenticate executable digest, argv policy, environment-name allowlist, workspace descriptor, process start identity, and receipt MAC before observe/wait/terminate. On macOS use a new process group; on Linux use a supervised session and `/proc` start identity. Never trust PID alone, inherit ambient environment, follow symlinks, or return terminal state before durable receipt.

- [ ] **Step 4: Run Cursor process and existing adapter suites**

Run: `uv run pytest packages/agent-runtime-cursor/tests/test_process_host.py packages/agent-runtime-cursor/tests/test_process_host_faults.py packages/agent-runtime-cursor/tests/test_process_host_security.py packages/agent-runtime-cursor/tests -q`

Expected: PASS on the current supported platform; platform-specific tests for the other OS use deterministic syscall fakes.

- [ ] **Step 5: Review, report, and commit**

```bash
git add packages/agent-runtime-cursor/agent_runtime_cursor/process.py packages/agent-runtime-cursor/agent_runtime_cursor/__init__.py packages/agent-runtime-cursor/tests
git commit -m "feat(cursor): add confined production process host"
```

### Task 8: Make Runtime Adapters Consume Locked Binding Data

**Files:**
- Modify: `packages/agent-runtime-opencode/agent_runtime_opencode/config.py`
- Modify: `packages/agent-runtime-opencode/agent_runtime_opencode/handler.py`
- Modify: `packages/agent-runtime-opencode/agent_runtime_opencode/plugin.py`
- Modify: `packages/agent-runtime-opencode/tests/test_config.py`
- Modify: `packages/agent-runtime-opencode/tests/test_credentials.py`
- Modify: `packages/agent-runtime-opencode/tests/test_preflight.py`
- Modify: `packages/agent-runtime-opencode/tests/test_create_recovery.py`
- Modify: `packages/agent-runtime-opencode/tests/test_cancel.py`
- Create: `packages/agent-runtime-opencode/tests/test_binding_authority.py`
- Modify: `packages/agent-runtime-cursor/agent_runtime_cursor/config.py`
- Modify: `packages/agent-runtime-cursor/agent_runtime_cursor/handler.py`
- Modify: `packages/agent-runtime-cursor/agent_runtime_cursor/plugin.py`
- Modify: `packages/agent-runtime-cursor/tests/test_config.py`
- Modify: `packages/agent-runtime-cursor/tests/test_credentials.py`
- Modify: `packages/agent-runtime-cursor/tests/test_process_launch.py`
- Modify: `packages/agent-runtime-cursor/tests/test_recovery.py`
- Modify: `packages/agent-runtime-cursor/tests/test_cancel.py`
- Create: `packages/agent-runtime-cursor/tests/test_binding_authority.py`

**Interfaces:**
- Consumes: `TaskRequest.binding_data`, the host-authorized `TaskContext.secrets` port, and Task 7 `production_process_host`.
- Produces: `OpenCodeAdapterConfig.from_request(request: TaskRequest) -> OpenCodeAdapterConfig` and `CursorAdapterConfig.from_request(request: TaskRequest) -> CursorAdapterConfig`; production handlers have no constructor-only authority.

- [ ] **Step 1: Write failing per-request authority and no-fallback tests**

```python
@pytest.mark.asyncio
async def test_opencode_rejects_missing_locked_endpoint(request, context):
    request = request.model_copy(update={"binding_data": {"model": "gpt-5.6-terra"}})
    outcome = await OpenCodeHandler().execute(request, context)
    assert outcome.failure.kind == "configuration"
    assert "endpoint" in outcome.failure.message


@pytest.mark.asyncio
async def test_cursor_uses_request_binding_not_constructor(tmp_path, request, context):
    handler = CursorHandler(process_host=production_process_host(tmp_path))
    configured = request.model_copy(update={"binding_data": valid_cursor_binding()})
    outcome = await handler.execute(configured, context)
    assert outcome.status in {"succeeded", "stopped"}
```

- [ ] **Step 2: Run tests and verify current `_require_config` failures**

Run: `uv run pytest packages/agent-runtime-opencode/tests/test_binding_authority.py packages/agent-runtime-cursor/tests/test_binding_authority.py -q`

Expected: FAIL because both handlers still require constructor configuration.

- [ ] **Step 3: Parse every operation from locked request data**

```python
@classmethod
def from_request(cls, request: TaskRequest) -> Self:
    try:
        return cls.model_validate(request.binding_data)
    except ValidationError as error:
        raise AdapterConfigurationError(redact_validation_error(error)) from error
```

Call `from_request` independently in preflight, execute, reconcile, and cancel; resolve each config-named handle through `context.secrets` before network/process work so an undeclared handle raises `SecretHandleUnauthorized`. Delete the optional production config constructor path and environment fallbacks. Keep injectable HTTP/process clients only as explicit test seams that cannot carry runtime authority.

- [ ] **Step 4: Run all adapter contract suites**

Run: `uv run pytest packages/agent-runtime-contracts/tests packages/agent-runtime-opencode/tests packages/agent-runtime-cursor/tests -q`

Expected: PASS; tests prove binding drift, model/endpoint/executable/permission omission, extra fields, and unauthorized handles fail before dispatch.

- [ ] **Step 5: Review, report, and commit**

```bash
git add packages/agent-runtime-opencode packages/agent-runtime-cursor
git commit -m "feat(adapters): derive authority from locked bindings"
```

### Task 9: Close Phase 3 Provider-Live Adapter Fixture Runs

**Files:**
- Modify: `benchmark/agent-runtime-phase3/manifest.json`
- Modify: `benchmark/agent-runtime-phase3/run_item.py`
- Modify: `benchmark/agent-runtime-phase3/run-opencode.sh`
- Modify: `benchmark/agent-runtime-phase3/run-cursor.sh`
- Create: `tests/phase5/test_phase3_live_fixture_contract.py`
- Create: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/phase3-live-closeout.md`

**Interfaces:**
- Consumes: Task 8 request-bound adapter configuration and installed provider credentials supplied only as runtime secret sources.
- Produces: one reproducible OpenCode fixture result, one reproducible Cursor fixture result, and a closeout record containing command, item ID, model, adapter version, lock digest, exit status, and artifact paths.

- [ ] **Step 1: Write the failing benchmark-contract test**

```python
def test_phase3_live_manifest_has_one_locked_fixture_per_adapter(repo_root):
    manifest = json.loads((repo_root / "benchmark/agent-runtime-phase3/manifest.json").read_text())
    assert {item["adapter"] for item in manifest["items"]} == {"opencode", "cursor"}
    for item in manifest["items"]:
        assert item["adapter_version"] == "0.1.0"
        assert "binding_manifest" in item
        assert "expected_artifacts" in item
```

- [ ] **Step 2: Run the contract test and verify manifest mismatch**

Run: `uv run pytest tests/phase5/test_phase3_live_fixture_contract.py -q`

Expected: FAIL until both fixture definitions use locked binding manifests and exact result contracts.

- [ ] **Step 3: Update the runner to fail closed and emit authenticated evidence**

```python
result = {
    "adapter": item["adapter"],
    "adapter_version": item["adapter_version"],
    "item_id": item["id"],
    "model": locked_binding["model"],
    "lock_digest": composition.lock.digest,
    "terminal_status": run_result.status,
    "artifacts": collect_expected_artifacts(item),
}
write_json_atomic(output / "result.json", result)
```

Reject unknown items, missing expected artifacts, ambient endpoint/model/executable overrides, and any credential value in output. Run each script in a fresh result directory and follow it to terminal completion; do not start both provider-live fixtures concurrently.

- [ ] **Step 4: Execute the two fixture runs and record closeout**

Run: `bash benchmark/agent-runtime-phase3/run-opencode.sh`

Expected: terminal success with every declared OpenCode artifact present.

Run: `bash benchmark/agent-runtime-phase3/run-cursor.sh`

Expected: terminal success with every declared Cursor artifact present.

Write the exact observed evidence to `phase3-live-closeout.md`; if either external provider is unavailable, keep Task 9 incomplete and record the blocking external condition without fabricating a pass.

- [ ] **Step 5: Re-run deterministic contract tests**

Run: `uv run pytest tests/phase5/test_phase3_live_fixture_contract.py packages/agent-runtime-opencode/tests packages/agent-runtime-cursor/tests -q`

Expected: PASS.

- [ ] **Step 6: Review, report, and commit**

```bash
git add benchmark/agent-runtime-phase3 tests/phase5/test_phase3_live_fixture_contract.py
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/phase3-live-closeout.md
git commit -m "test(adapters): close provider-live fixture evidence"
```

### Task 10: Package the Product, Source Catalog, and Two Providers

**Files:**
- Create: `packages/assurance-product/pyproject.toml`
- Create: `packages/assurance-product/README.md`
- Create: `packages/assurance-product/assurance_product/__init__.py`
- Create: `packages/assurance-product/assurance_product/models.py`
- Create: `packages/assurance-product/assurance_product/source_catalog.py`
- Create: `packages/assurance-product/assurance_product/product.py`
- Create: `packages/assurance-product/assurance_product/resources/declarations/product.yaml`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/phase5/test_product_packaging.py`
- Create: `tests/phase5/test_product_providers.py`

**Interfaces:**
- Consumes: `graph_engine.products` provider protocol, six Phase 4 capability distributions, both `0.1.0` adapter distributions, and source-authenticated provider coordinates.
- Produces: `AssuranceOpenCodeProductProvider`, `AssuranceCursorProductProvider`, `PRODUCT_ID = "assurance"`, `ENGINE_API = "2.0"`, and `product_source_catalog(adapter: Literal["opencode", "cursor"]) -> tuple[ProviderSource, ...]`.

- [ ] **Step 1: Write failing distribution and entry-point tests**

```python
def test_product_metadata_exposes_only_two_product_entry_points(built_product_wheel):
    metadata = read_wheel_metadata(built_product_wheel)
    assert metadata.name == "assurance-product"
    assert metadata.entry_points["graph_engine.products"] == {
        "assurance-opencode": "assurance_product.product:AssuranceOpenCodeProductProvider",
        "assurance-cursor": "assurance_product.product:AssuranceCursorProductProvider",
    }
    assert "assurance-agent" not in metadata.requires_dist


def test_provider_catalogs_differ_only_by_runtime_adapter():
    opencode = product_source_catalog("opencode")
    cursor = product_source_catalog("cursor")
    assert six_capability_coordinates(opencode) == six_capability_coordinates(cursor)
    assert runtime_coordinates(opencode) == {"agent-runtime-opencode==0.1.0"}
    assert runtime_coordinates(cursor) == {"agent-runtime-cursor==0.1.0"}
```

- [ ] **Step 2: Run tests and verify package/entry points are absent**

Run: `uv run pytest tests/phase5/test_product_packaging.py tests/phase5/test_product_providers.py -q`

Expected: FAIL because `packages/assurance-product` does not exist.

- [ ] **Step 3: Add the independent package and exact provider catalogs**

```toml
[project]
name = "assurance-product"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "graph-engine==0.1.0",
  "assurance-intake==0.1.0",
  "assurance-generation==0.1.0",
  "assurance-execution==0.1.0",
  "assurance-healing==0.1.0",
  "assurance-quality==0.1.0",
  "assurance-improvement==0.1.0",
]

[project.optional-dependencies]
opencode = ["agent-runtime-opencode==0.1.0"]
cursor = ["agent-runtime-cursor==0.1.0"]

[project.entry-points."graph_engine.products"]
assurance-opencode = "assurance_product.product:AssuranceOpenCodeProductProvider"
assurance-cursor = "assurance_product.product:AssuranceCursorProductProvider"
```

Each provider returns one exact product declaration and deterministic source catalog. It must not scan the SUT, import legacy packages, select a deployment/config provider, or embed runtime binding values. Add `packages/assurance-product` as a uv workspace member.

- [ ] **Step 4: Build the wheel and run packaging/provider tests**

Run: `uv build --package assurance-product`

Expected: one sdist and one wheel whose metadata contains only the declared dependencies and entry points.

Run: `uv run pytest tests/phase5/test_product_packaging.py tests/phase5/test_product_providers.py -q`

Expected: PASS.

- [ ] **Step 5: Review, report, and commit**

```bash
git add pyproject.toml uv.lock packages/assurance-product tests/phase5/test_product_packaging.py tests/phase5/test_product_providers.py
git commit -m "feat(product): package assurance product providers"
```

### Task 11: Build Deterministic Deployment Binding Wheels

**Files:**
- Create: `packages/assurance-product/assurance_product/binding_builder.py`
- Create: `packages/assurance-product/assurance_product/resources/declarations/deployment.schema.json`
- Create: `packages/assurance-product/assurance_product/resources/declarations/deployment-plugin.yaml`
- Modify: `packages/assurance-product/assurance_product/models.py`
- Create: `tests/phase5/test_binding_builder.py`
- Create: `tests/phase5/test_binding_builder_security.py`
- Create: `tests/phase5/fixtures/deployment/opencode.yaml`
- Create: `tests/phase5/fixtures/deployment/cursor.yaml`

**Interfaces:**
- Consumes: Task 1 exact prepare-ID list, exact source coordinates from Task 10, `AgentBindingDataV1`, and closed adapter binding schemas.
- Produces: `DeploymentBindingsV1`, `BuiltDeploymentWheel`, and `build_deployment_wheel(manifest_path: Path, output_dir: Path) -> BuiltDeploymentWheel`.

- [ ] **Step 1: Write failing determinism and rejection tests**

```python
def test_binding_build_is_byte_deterministic(tmp_path, opencode_manifest):
    first = build_deployment_wheel(opencode_manifest, tmp_path / "first")
    second = build_deployment_wheel(opencode_manifest, tmp_path / "second")
    assert first.manifest_digest == second.manifest_digest
    assert first.wheel_digest == second.wheel_digest
    assert first.wheel.read_bytes() == second.wheel.read_bytes()
    prefix = first.manifest_digest.removeprefix("sha256:")[:16]
    assert first.distribution == f"assurance-product-bindings-{prefix}"
    assert first.import_package == f"assurance_product_bindings_{prefix}"
    assert first.wheel.name == canonical_wheel_filename(first.distribution, "1.0.0")


@pytest.mark.parametrize("field", ["python", "secret_value", "default_model", "fallback_endpoint"])
def test_manifest_rejects_executable_or_fallback_fields(opencode_document, field):
    opencode_document[field] = "forbidden"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        DeploymentBindingsV1.model_validate(opencode_document)
```

- [ ] **Step 2: Run tests and verify the builder is absent**

Run: `uv run pytest tests/phase5/test_binding_builder.py tests/phase5/test_binding_builder_security.py -q`

Expected: FAIL because `DeploymentBindingsV1` and `build_deployment_wheel` do not exist.

- [ ] **Step 3: Implement a closed manifest and data-only wheel renderer**

```python
class DeploymentBindingsV1(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"]
    runtime_plugin_id: Literal["runtime.opencode", "runtime.cursor"]
    adapter_binding: OpenCodeBindingV1 | CursorBindingV1
    routes: Mapping[str, RouteAssignmentV1]
    permission_profiles: Mapping[str, PermissionProfileV1]
    request_policies: Mapping[str, RequestPolicyV1]
    secret_handles: tuple[str, ...]


class BuiltDeploymentWheel(FrozenModel):
    wheel: Path
    manifest_digest: str
    wheel_digest: str
    distribution: str
    import_package: str
    entry_point_value: str
    declaration_path: str
    plugin_id: Literal["assurance.product.bindings"]
    plugin_version: Literal["1.0.0"]
```

Validate exactly one assignment for each of the 33 prepare IDs and no unknown ID. Render a fixed audited provider module plus canonical JSON resources; normalize ZIP timestamps, member order, permissions, metadata, and RECORD. The only entry point is `graph_engine.plugins` / `deployment`; the generated provider contributes exactly 99 aliases and no secret bytes.

- [ ] **Step 4: Run builder, wheel-inspection, and installation tests**

Run: `uv run pytest tests/phase5/test_binding_builder.py tests/phase5/test_binding_builder_security.py -q`

Expected: PASS for repeated builds, reordered YAML mappings, missing/extra assignments, malicious strings, ZIP path escape, source drift, and no-secret scans.

- [ ] **Step 5: Review, report, and commit**

```bash
git add packages/assurance-product/assurance_product/binding_builder.py packages/assurance-product/assurance_product/models.py packages/assurance-product/assurance_product/resources/declarations tests/phase5/test_binding_builder.py tests/phase5/test_binding_builder_security.py tests/phase5/fixtures/deployment
git commit -m "feat(product): build authenticated deployment binding wheels"
```

### Task 12: Load the Data-Only Project Configuration Tree

**Files:**
- Create: `packages/assurance-product/assurance_product/configuration.py`
- Create: `packages/assurance-product/assurance_product/resources/declarations/configuration-plugin.yaml`
- Create: `packages/assurance-product/assurance_product/resources/schemas/project-config-v1.json`
- Modify: `packages/assurance-product/assurance_product/models.py`
- Create: `tests/phase5/test_project_configuration.py`
- Create: `tests/phase5/test_project_configuration_security.py`
- Create: `tests/phase5/fixtures/project-config/.aa/config.yaml`
- Create: `tests/phase5/fixtures/project-config/.aa/policy.yaml`
- Create: `tests/phase5/fixtures/project-config/.aa/data-knowledge.yaml`

**Interfaces:**
- Consumes: Phase 2 authenticated data-only ConfigTree loading and the six capability wheels' owned policy/knowledge schemas.
- Produces: `ProjectConfigV1`, `load_project_configuration(tree: ConfigTree) -> PluginContribution`, plugin ID/version `assurance.product.configuration==1.0.0`.

- [ ] **Step 1: Write failing authority-separation tests**

```python
def test_project_config_contributes_business_data_only(config_tree):
    contribution = load_project_configuration(config_tree)
    assert contribution.bindings == ()
    assert contribution.task_handlers == {}
    assert contribution.commit_validators == {}
    assert contribution.effects == ()
    assert {resource.id for resource in contribution.resources} == {
        "assurance.config.product-policy",
        "assurance.config.data-knowledge",
        "assurance.config.capability-catalog",
        "assurance.config.node-policy-values",
    }


@pytest.mark.parametrize("key", ["model", "endpoint", "executable", "permission_profile", "secret", "adapter"])
def test_project_config_rejects_runtime_authority(config_document, key):
    config_document[key] = "forbidden"
    with pytest.raises(ProjectConfigurationError):
        parse_project_config(config_document)
```

- [ ] **Step 2: Run tests and verify configuration module failures**

Run: `uv run pytest tests/phase5/test_project_configuration.py tests/phase5/test_project_configuration_security.py -q`

Expected: FAIL because the configuration plugin and schema do not exist.

- [ ] **Step 3: Implement schema-owned data contribution**

```python
class ConfiguredResourceV1(FrozenModel):
    resource_id: str
    schema_id: str
    media_type: Literal["application/json", "application/yaml", "text/markdown"]
    content: FrozenJSONValue | str


class ProjectConfigV1(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"]
    product_policy: FrozenJSONValue
    data_knowledge: FrozenJSONValue
    capability_catalog: FrozenJSONValue
    node_policy_values: Mapping[str, FrozenJSONValue] = Field(default_factory=dict)
    resources: tuple[ConfiguredResourceV1, ...] = ()
```

Authenticate the complete ConfigTree before parsing; delegate policy/knowledge payload validation to the owning capability schemas; reject Python files, entry-point declarations, bindings, handlers, host/resource ports, runtime authority, unknown files, symlinks, and SUT path escapes. Contribution source identity and full resource bytes must enter the composition lock.

- [ ] **Step 4: Run configuration and Phase 2 ConfigTree tests**

Run: `uv run pytest tests/phase5/test_project_configuration.py tests/phase5/test_project_configuration_security.py packages/graph-engine/tests/composition/test_declarative_sources.py packages/graph-engine/tests/composition/test_source_fs.py -q`

Expected: PASS with source mutation, same ID/version different bytes, schema drift, extra-file, and authority-injection coverage.

- [ ] **Step 5: Review, report, and commit**

```bash
git add packages/assurance-product/assurance_product/configuration.py packages/assurance-product/assurance_product/models.py packages/assurance-product/assurance_product/resources tests/phase5/test_project_configuration.py tests/phase5/test_project_configuration_security.py tests/phase5/fixtures/project-config
git commit -m "feat(product): load data-only project configuration"
```

### Task 13: Resolve the Exact Product Composition and 99 Bindings

**Files:**
- Modify: `packages/assurance-product/assurance_product/product.py`
- Modify: `packages/assurance-product/assurance_product/source_catalog.py`
- Modify: `packages/assurance-product/assurance_product/models.py`
- Create: `tests/phase5/test_product_composition.py`
- Create: `tests/phase5/test_binding_coverage.py`
- Create: `tests/phase5/test_composition_authority.py`

**Interfaces:**
- Consumes: one product provider, one generated deployment provider, one project configuration tree, six capability providers, one selected adapter provider, `CompositionRequest`, and `FrozenComposition` authentication.
- Produces: `AssuranceCompositionRequest` and `resolve_assurance_composition(request: AssuranceCompositionRequest) -> FrozenComposition`.

- [ ] **Step 1: Write failing exact-closure tests**

```python
@pytest.mark.parametrize("adapter", ["opencode", "cursor"])
def test_composition_has_exact_provider_and_binding_closure(adapter, installed_sources):
    composition = resolve_assurance_composition(request_for(adapter, installed_sources))
    assert composition.lock.engine_api == "2.0"
    entries = composition.registries.capabilities.entries
    bindings = {key: value for key, value in entries.items() if isinstance(value, CapabilityBindingEntry)}
    assert set(bindings) == set(ALL_BINDING_IDS)
    assert len(bindings) == 99
    for binding_id in finalize_aliases():
        assert bindings[binding_id].data is None
        assert bindings[binding_id].secret_handles == ()
```

- [ ] **Step 2: Run tests and verify incomplete composition failure**

Run: `uv run pytest tests/phase5/test_product_composition.py tests/phase5/test_binding_coverage.py tests/phase5/test_composition_authority.py -q`

Expected: FAIL because product resolution does not yet select and authenticate deployment/configuration providers.

- [ ] **Step 3: Implement one-shot fail-closed resolution**

```python
class AssuranceCompositionRequest(FrozenModel):
    product_entrypoint: Literal["assurance-opencode", "assurance-cursor"]
    deployment_source: ProviderSource
    configuration_tree: ConfigTree


def resolve_assurance_composition(request: AssuranceCompositionRequest) -> FrozenComposition:
    sources = product_source_catalog(adapter_for(request.product_entrypoint))
    return resolve_composition(
        CompositionRequest(
            product=source_for_product(request.product_entrypoint),
            plugins=(*sources, request.deployment_source, source_for_configuration(request.configuration_tree)),
        )
    )
```

Before returning, compare descriptor, declaration, contribution, registry, lock, and graph binding sets to the exact expected sets. Authenticate distribution name/version, entry-point coordinate, module root, declaration bytes, contribution projection, adapter match, 33 routing assignments, 99 aliases, all resource IDs/digests, and secret-handle names. There is no provider discovery by wildcard and no fallback product/adapter.

- [ ] **Step 4: Run composition and lock suites**

Run: `uv run pytest tests/phase5/test_product_composition.py tests/phase5/test_binding_coverage.py tests/phase5/test_composition_authority.py packages/graph-engine/tests/composition -q`

Expected: PASS for both adapters and every missing/extra/duplicate/drift/forgery case.

- [ ] **Step 5: Update the binding evidence and commit**

Regenerate `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/binding-coverage.json` from the authenticated composition projection, verify it is byte-identical for repeated resolution, then:

```bash
git add packages/assurance-product tests/phase5/test_product_composition.py tests/phase5/test_binding_coverage.py tests/phase5/test_composition_authority.py
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/binding-coverage.json
git commit -m "feat(product): resolve exact assurance compositions"
```

### Task 14: Assemble Intake, Case Design, and Agent Triplet Subgraphs

**Files:**
- Create: `packages/assurance-product/assurance_product/resources/workflow/assurance-full.yaml`
- Create: `packages/assurance-product/assurance_product/resources/schemas/product-input-v1.json`
- Modify: `packages/assurance-product/assurance_product/models.py`
- Create: `tests/phase5/graph_inventory.py`
- Create: `tests/phase5/test_product_input.py`
- Create: `tests/phase5/test_graph_intake_and_triplets.py`

**Interfaces:**
- Consumes: Task 4 `input_projection`, Task 13 exact alias registry, configured policy/knowledge resources, and Phase 4 intake capabilities.
- Produces: `ResourceRefV1`, `BusinessBudgetsV1`, exact `ProductInputV1`, `load_canonical_workflow() -> WorkflowDef`, and reusable YAML triplets whose alias is derived as `assurance.product.agent.` plus the Phase 4 prepare ID without its leading `assurance.` and terminal `.prepare`, followed by `.prepare`, `.execute`, or `.finalize`.

- [ ] **Step 1: Write failing root-input and triplet-shape tests**

```python
def test_product_input_is_closed_and_stable():
    value = ProductInputV1.model_validate(valid_product_input())
    assert value.schema_version == 1
    with pytest.raises(ValidationError):
        ProductInputV1.model_validate({**valid_product_input(), "model": "ambient"})


def test_every_agent_node_is_one_closed_triplet(compiled_product_workflow):
    triplets = collect_agent_triplets(compiled_product_workflow)
    assert triplets
    for triplet in triplets:
        assert triplet.aliases == expected_triplet_aliases(triplet.prepare_id)
        assert triplet.execute_input_from == triplet.prepare_node
        assert triplet.finalize_input_from == triplet.execute_node
```

- [ ] **Step 2: Run tests and verify missing workflow/model failures**

Run: `uv run pytest tests/phase5/test_product_input.py tests/phase5/test_graph_intake_and_triplets.py -q`

Expected: FAIL because the canonical workflow and `ProductInputV1` do not exist.

- [ ] **Step 3: Add the exact input model and initial graph slice**

```python
class ProductInputV1(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"]
    change_id: str
    requirement: str
    run_mode: Literal["case", "implement", "verify"]
    selected_test_families: tuple[Literal["api", "e2e", "fuzz", "performance"], ...]
    auto_archive: bool
    capability_catalog: ResourceRefV1
    product_policy: ResourceRefV1
    data_knowledge: ResourceRefV1
    allowed_artifact_paths: tuple[str, ...]
    budgets: BusinessBudgetsV1


class ResourceRefV1(FrozenModel):
    resource_id: str
    sha256: str


class BusinessBudgetsV1(FrozenModel):
    review_rounds: int = Field(ge=0)
    coverage_rounds: int = Field(ge=0)
    healing_rounds: int = Field(ge=0)
    execution_retries: int = Field(ge=0)
```

Normalize the non-empty canonical `change_id` and UTF-8 `requirement`; require canonical family order without duplicates; require sorted canonical relative POSIX artifact prefixes; resolve and authenticate all three resource references before bootstrap. Enforce the entrypoint family table exactly: `full` and `execute` require a non-empty tuple, while `intake`, `case`, `archive`, `retro`, `issue-review`, `issue-analyze`, `issue-reconcile`, `improvement-review`, `improvement-evaluate`, `improvement-export`, `improvement-apply`, and `improvement-rollback` require an empty tuple. Add entry, intake, explore, case-design, and case-review nodes with closed input projections. Expand each agent-backed logical step as a prepare/execute/finalize triplet and reject direct runtime or Phase 4 capability references.

- [ ] **Step 4: Run product graph and compiler tests**

Run: `uv run pytest tests/phase5/test_product_input.py tests/phase5/test_graph_intake_and_triplets.py packages/graph-engine/tests/graph -q`

Expected: PASS; the workflow slice compiles under both product providers and every referenced alias exists in the frozen registry.

- [ ] **Step 5: Update graph inventory and commit**

Record every added node/edge/alias in `graph-inventory.yaml`, then:

```bash
git add packages/assurance-product/assurance_product/models.py packages/assurance-product/assurance_product/resources tests/phase5/graph_inventory.py tests/phase5/test_product_input.py tests/phase5/test_graph_intake_and_triplets.py
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/graph-inventory.yaml
git commit -m "feat(product): assemble intake and agent triplets"
```

### Task 15: Add Four Generation Families and the Exact Selected-Family Join

**Files:**
- Modify: `packages/assurance-product/assurance_product/resources/workflow/assurance-full.yaml`
- Modify: `tests/phase5/graph_inventory.py`
- Create: `tests/phase5/test_generation_branches.py`
- Create: `tests/phase5/test_selected_family_join.py`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/graph-inventory.yaml`

**Interfaces:**
- Consumes: validated `ProductInputV1.selected_test_families`, intake/case outputs, API/E2E/Fuzz/Performance Phase 4 capabilities, and closed gate expressions.
- Produces: four independently selectable generation branches and one deterministic join that waits for exactly the selected family terminals.

- [ ] **Step 1: Write failing branch coverage and join tests**

```python
@pytest.mark.parametrize("family", ["api", "e2e", "fuzz", "performance"])
def test_single_family_runs_only_its_generation_branch(product_runner, family):
    trace = product_runner(selected_test_families=(family,)).run_to_generation_join()
    assert trace.completed_generation_families == {family}
    assert trace.join_expected == {family}


def test_all_family_join_is_order_independent(product_runner):
    forward = product_runner(selected_test_families=("api", "e2e", "fuzz", "performance"), completion_order="forward").run_to_generation_join()
    reverse = product_runner(selected_test_families=("api", "e2e", "fuzz", "performance"), completion_order="reverse").run_to_generation_join()
    assert forward.join_output == reverse.join_output
```

- [ ] **Step 2: Run tests and verify absent branches/join**

Run: `uv run pytest tests/phase5/test_generation_branches.py tests/phase5/test_selected_family_join.py -q`

Expected: FAIL because the workflow stops after the intake/case slice.

- [ ] **Step 3: Add exact family subgraphs and deterministic fan-in**

```yaml
nodes:
  generation.join-selected:
    kind: join
    join: all
    input_projection:
      type: object
      fields:
        selected_families: {type: root_pointer, pointer: /selected_test_families}
        completed: {type: all_predecessor_tokens}
```

API and E2E include plan, plan-review, codegen, and codegen-fix where specified; Fuzz and Performance include their exact Phase 4 plan/review/codegen sequences. Guards derive only from the validated selected-family tuple. Zero selected families is rejected by `ProductInputV1`, unknown families fail validation, non-selected branches receive no token, and the join neither waits for nor accepts non-selected terminals.

- [ ] **Step 4: Run branch permutations and graph audits**

Run: `uv run pytest tests/phase5/test_generation_branches.py tests/phase5/test_selected_family_join.py tests/phase5/test_graph_intake_and_triplets.py -q`

Expected: PASS for all 15 non-empty family subsets and forward/reverse completion order.

- [ ] **Step 5: Update inventory, review, and commit**

```bash
git add packages/assurance-product/assurance_product/resources/workflow/assurance-full.yaml tests/phase5/graph_inventory.py tests/phase5/test_generation_branches.py tests/phase5/test_selected_family_join.py
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/graph-inventory.yaml
git commit -m "feat(product): add selected generation branches"
```

### Task 16: Add Execution, Quality, Issue, Healing, Coverage, and Report Flow

**Files:**
- Modify: `packages/assurance-product/assurance_product/resources/workflow/assurance-full.yaml`
- Modify: `tests/phase5/graph_inventory.py`
- Create: `tests/phase5/test_execution_quality_flow.py`
- Create: `tests/phase5/test_issue_healing_flow.py`
- Create: `tests/phase5/test_coverage_loop.py`
- Create: `tests/phase5/test_report_flow.py`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/graph-inventory.yaml`

**Interfaces:**
- Consumes: joined generated cases, execution/quality/healing capability outputs, configured coverage policy, immutable attempt trees, and closed gates.
- Produces: execution/quality path, issue triage/analysis/healing path, bounded coverage-repair loop, and mandatory report artifact path.

- [ ] **Step 1: Write failing success, issue, loop, and report tests**

```python
def test_low_coverage_reenters_generation_until_policy_passes(product_runner):
    trace = product_runner(coverage_sequence=(0.40, 0.72, 0.91), threshold=0.90).run_to_report()
    assert trace.activations("assurance.healing.coverage-repair") == 2
    assert trace.report.coverage == 0.91


def test_issue_path_runs_triage_analysis_and_fix_before_rerun(product_runner):
    trace = product_runner(execution_sequence=("failed", "passed")).run_to_report()
    assert trace.logical_steps_between("execution", "execution") == (
        "quality.issue-triage", "quality.issue-analysis", "healing.fix-proposal"
    )
    assert trace.report.exists
```

- [ ] **Step 2: Run tests and verify missing downstream nodes**

Run: `uv run pytest tests/phase5/test_execution_quality_flow.py tests/phase5/test_issue_healing_flow.py tests/phase5/test_coverage_loop.py tests/phase5/test_report_flow.py -q`

Expected: FAIL because the canonical graph ends at the selected-family join.

- [ ] **Step 3: Add closed downstream paths and loop budget**

```yaml
nodes:
  coverage.policy-gate:
    kind: gate
    expression: "input.decision"
    input_projection:
      type: object
      fields:
        decision: {type: predecessor_pointer, predecessor: quality.inspect.finalize, pointer: /coverage/decision}
        measured: {type: predecessor_pointer, predecessor: quality.inspect.finalize, pointer: /coverage/measured}
        policy: {type: config_pointer, pointer: /coverage}
```

Wire execute/run, fact-baseline/inspect, issue-triage/issue-analysis, fix-proposal, coverage-repair, rerun, and report triplets exactly as inventoried. Coverage decisions use structured measured coverage and configured threshold, not model prose. Enforce the spec's explicit loop budget and terminal semantics; budget exhaustion routes to the governed STOP/report outcome rather than silently succeeding or looping forever.

- [ ] **Step 4: Run downstream flow and replay tests**

Run: `uv run pytest tests/phase5/test_execution_quality_flow.py tests/phase5/test_issue_healing_flow.py tests/phase5/test_coverage_loop.py tests/phase5/test_report_flow.py packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py packages/graph-engine/tests/runtime/test_engine.py -q`

Expected: PASS for success, test failure, product issue, infrastructure failure, low coverage, repair success, repair exhaustion, and report generation.

- [ ] **Step 5: Update inventory, review, and commit**

```bash
git add packages/assurance-product/assurance_product/resources/workflow/assurance-full.yaml tests/phase5/graph_inventory.py tests/phase5/test_execution_quality_flow.py tests/phase5/test_issue_healing_flow.py tests/phase5/test_coverage_loop.py tests/phase5/test_report_flow.py
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/graph-inventory.yaml
git commit -m "feat(product): add assurance execution and quality loops"
```

### Task 17: Add Archive, Retro, Improvement, STOP, and Interrupt Paths

**Files:**
- Modify: `packages/assurance-product/assurance_product/resources/workflow/assurance-full.yaml`
- Modify: `tests/phase5/graph_inventory.py`
- Create: `tests/phase5/test_archive_retro_improvement.py`
- Create: `tests/phase5/test_stop_and_interrupts.py`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/graph-inventory.yaml`

**Interfaces:**
- Consumes: mandatory report output, archive/Retro/Improvement capability triplets, engine interrupt/resume primitives, and governed STOP reasons.
- Produces: the canonical `full` terminal tail with optional archive, independent Retro and Improvement entrypoint paths, exact resumable interrupts, structured STOP projection, and successful `InvocationFinished` only after the selected entrypoint's required work.

- [ ] **Step 1: Write failing tail-order and STOP/resume tests**

```python
def test_full_archives_only_when_requested(product_runner):
    archived = product_runner(entrypoint="full", auto_archive=True).run_to_terminal()
    unarchived = product_runner(entrypoint="full", auto_archive=False).run_to_terminal()
    assert archived.terminal_tail == ("quality.report", "improvement.archive")
    assert unarchived.terminal_tail == ("quality.report",)


def test_retro_and_improvement_are_independent_entrypoints(product_runner):
    retro = product_runner(entrypoint="retro").run_to_terminal()
    improvement = product_runner(entrypoint="improvement-apply").run_to_terminal()
    assert retro.logical_steps == (
        "improvement.retro", "improvement.retro-eval-analysis",
        "improvement.retro-issue-analysis", "improvement.retro-workflow-analysis",
    )
    assert improvement.logical_steps == ("improvement.improvement-review", "improvement.apply")


def test_business_stop_is_resumable_only_at_declared_interrupt(product_runner):
    stopped = product_runner(review_decision="needs-human").run_to_terminal()
    assert stopped.status == "interrupted"
    resumed = stopped.resume({"decision": "approve"})
    assert resumed.status == "completed"
```

- [ ] **Step 2: Run tests and verify incomplete terminal tail**

Run: `uv run pytest tests/phase5/test_archive_retro_improvement.py tests/phase5/test_stop_and_interrupts.py -q`

Expected: FAIL because archive, Retro, Improvement, and declared interrupt routes are absent.

- [ ] **Step 3: Complete the graph with explicit terminal semantics**

```yaml
nodes:
  healing.fix-proposal.finalize:
    kind: task
    capability: assurance.product.agent.healing.fix-proposal.finalize
    retry: finalize-business-output
    timeout: local-finalize
    input_projection:
      type: object
      fields:
        adapter_result: {type: predecessor, predecessor: healing.fix-proposal.execute}
        business_context: {type: root_pointer, pointer: ""}
```

The finalize handler returns `TaskOutcome.stopped(reason=...)` for the typed healing-disallowed decision; the graph does not invent a `stop` node kind. Add archive after its precheck and durable effect authority, gated only by `auto_archive`. Build Retro collect/analyze/propose/reconcile and the five Improvement review/evaluate/export/apply/rollback entrypoints as independent paths using their exact Phase 4 capabilities and triplet aliases; do not append them to `full`. Add only the interrupts enumerated in the spec, each with a closed resume-input schema. Separate business STOP, interrupt, graph failure, and reported terminal success; no nested subgraph may translate a STOP into normal completion.

- [ ] **Step 4: Run terminal, recovery, and resume tests**

Run: `uv run pytest tests/phase5/test_archive_retro_improvement.py tests/phase5/test_stop_and_interrupts.py packages/graph-engine/tests/runtime/test_engine.py packages/graph-engine/tests/runtime/test_activity_recovery.py -q`

Expected: PASS for successful tail, every declared STOP, interrupt/resume, invalid resume input, crash at tail boundaries, and nested STOP propagation.

- [ ] **Step 5: Update inventory, review, and commit**

```bash
git add packages/assurance-product/assurance_product/resources/workflow/assurance-full.yaml tests/phase5/graph_inventory.py tests/phase5/test_archive_retro_improvement.py tests/phase5/test_stop_and_interrupts.py
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/graph-inventory.yaml
git commit -m "feat(product): complete retro and terminal workflow"
```

### Task 18: Publish Public Entrypoints and Audit the Full Graph

**Files:**
- Modify: `packages/assurance-product/assurance_product/resources/workflow/assurance-full.yaml`
- Modify: `packages/assurance-product/assurance_product/product.py`
- Modify: `tests/phase5/graph_inventory.py`
- Create: `tests/phase5/test_product_entrypoints.py`
- Create: `tests/phase5/test_full_graph_audit.py`
- Create: `tests/phase5/test_graph_binding_audit.py`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/graph-inventory.yaml`

**Interfaces:**
- Consumes: completed canonical workflow, exact 99 aliases, graph inventory, and both authenticated product compositions.
- Produces: all public entrypoints named in spec Section 15.2 and `audit_full_graph(workflow: CompiledWorkflow, composition: FrozenComposition) -> GraphAuditResult`.

- [ ] **Step 1: Write failing closed-entrypoint and reachability audits**

```python
def test_public_entrypoints_are_exact(compiled_product_workflow):
    assert set(compiled_product_workflow.entrypoints) == {
        "full", "intake", "case", "execute", "archive", "retro",
        "issue-review", "issue-analyze", "issue-reconcile",
        "improvement-review", "improvement-evaluate", "improvement-export",
        "improvement-apply", "improvement-rollback",
    }


@pytest.mark.parametrize("adapter", ["opencode", "cursor"])
def test_full_graph_has_no_orphans_or_forbidden_targets(adapter, compiled_for):
    audit = audit_full_graph(compiled_for(adapter).workflow, compiled_for(adapter).composition)
    assert audit.unreachable_nodes == ()
    assert audit.dead_ends == ()
    assert audit.forbidden_direct_targets == ()
    assert audit.missing_bindings == ()
    assert audit.uninventoried_nodes == ()
```

- [ ] **Step 2: Run tests and verify entrypoint/inventory mismatch**

Run: `uv run pytest tests/phase5/test_product_entrypoints.py tests/phase5/test_full_graph_audit.py tests/phase5/test_graph_binding_audit.py -q`

Expected: FAIL until every public entrypoint and exact graph inventory record is present.

- [ ] **Step 3: Add exact entrypoint projections and total audit implementation**

```python
class GraphAuditResult(FrozenModel):
    unreachable_nodes: tuple[str, ...]
    dead_ends: tuple[str, ...]
    forbidden_direct_targets: tuple[str, ...]
    missing_bindings: tuple[str, ...]
    uninventoried_nodes: tuple[str, ...]
```

Each public entrypoint starts at a declared node with a typed root-input projection and reaches only its intended subgraph. Audit graph/reference closure in both adapter compositions, all family subsets, success/STOP/interrupt terminals, loop back-edges, exact triplet adjacency, exact selected-family join, and absence of direct `runtime.*`, direct Phase 4 agent capabilities, and test-only aliases.

- [ ] **Step 4: Run the complete deterministic product graph suite**

Run: `uv run pytest tests/phase5/test_product_entrypoints.py tests/phase5/test_full_graph_audit.py tests/phase5/test_graph_binding_audit.py tests/phase5/test_graph_intake_and_triplets.py tests/phase5/test_generation_branches.py tests/phase5/test_selected_family_join.py tests/phase5/test_execution_quality_flow.py tests/phase5/test_issue_healing_flow.py tests/phase5/test_coverage_loop.py tests/phase5/test_report_flow.py tests/phase5/test_archive_retro_improvement.py tests/phase5/test_stop_and_interrupts.py -q`

Expected: PASS under both adapters.

- [ ] **Step 5: Review, report, and commit**

```bash
git add packages/assurance-product tests/phase5/test_product_entrypoints.py tests/phase5/test_full_graph_audit.py tests/phase5/test_graph_binding_audit.py tests/phase5/graph_inventory.py
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/graph-inventory.yaml
git commit -m "feat(product): publish and audit full workflow entrypoints"
```

### Task 19: Implement the `aa-next` Command Surface

**Files:**
- Create: `packages/assurance-product/assurance_product/cli.py`
- Create: `packages/assurance-product/assurance_product/status.py`
- Create: `packages/assurance-product/assurance_product/resources/schemas/status-v1.json`
- Modify: `packages/assurance-product/assurance_product/models.py`
- Modify: `packages/assurance-product/pyproject.toml`
- Create: `tests/phase5/test_cli_compile.py`
- Create: `tests/phase5/test_cli_bindings_build.py`
- Create: `tests/phase5/test_cli_lifecycle.py`
- Create: `tests/phase5/test_cli_status_and_lock.py`
- Create: `tests/phase5/test_cli_fail_closed.py`

**Interfaces:**
- Consumes: authenticated product/deployment/configuration sources, `ProductInputV1`, stable seed capture, `Engine.production`, workflow entrypoints, and invocation projection.
- Produces: console script `aa-next` with exactly `compile`, `bindings build`, `start`, `run`, `status`, `resume`, `export`, and `lock show`; `StatusV1`; `render_status(projection: InvocationProjection) -> StatusV1`.

The public command names are exactly:

```text
aa-next compile
aa-next bindings build
aa-next start
aa-next run
aa-next status
aa-next resume
aa-next export
aa-next lock show
```

- [ ] **Step 1: Write failing command-surface and lifecycle tests**

```python
def test_help_exposes_exact_command_tree(cli_runner):
    result = cli_runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert command_names(result.stdout) == {"compile", "bindings", "start", "run", "status", "resume", "export", "lock"}
    assert nested_command_names(cli_runner, app, "bindings") == {"build"}
    assert nested_command_names(cli_runner, app, "lock") == {"show"}


def test_start_requires_explicit_product_deployment_config_and_input(cli_runner, tmp_path):
    result = cli_runner.invoke(app, ["start", "--engine-root", str(tmp_path)])
    assert result.exit_code == 2
    assert "--project-dir" in result.output
    assert "--invocation-id" in result.output
    assert "--product" in result.output
    assert "--binding-dist" in result.output
    assert "--binding-entrypoint" in result.output
    assert "--binding-declaration" in result.output
    assert "--config-tree" in result.output
    assert "--entrypoint" in result.output
    assert "--input" in result.output
```

- [ ] **Step 2: Run tests and verify `aa-next` is absent**

Run: `uv run pytest tests/phase5/test_cli_compile.py tests/phase5/test_cli_bindings_build.py tests/phase5/test_cli_lifecycle.py tests/phase5/test_cli_status_and_lock.py tests/phase5/test_cli_fail_closed.py -q`

Expected: FAIL because the console script and command handlers do not exist.

- [ ] **Step 3: Implement explicit command routing and status projection**

```python
class GraphStatusV1(FrozenModel):
    graph_instance_id: str
    graph_id: str
    parent_graph_instance_id: str | None
    state: Literal["inactive", "running", "failed", "stopped", "interrupted", "completed"]


class NodeStatusV1(FrozenModel):
    graph_instance_id: str
    node_id: str
    state: Literal["inactive", "ready", "running", "retrying", "succeeded", "failed", "stopped", "interrupted", "skipped"]
    attempt: int | None
    lease_state: str | None
    failure_category: str | None
    activity_reference_digest: str | None


class CoverageProgressV1(FrozenModel):
    round: int
    maximum_rounds: int
    measured: FrozenJSONValue
    decision: str


class EffectStatusV1(FrozenModel):
    effect_id: str
    kind: str
    state: str
    receipt_digest: str | None


class AdapterEvidenceRefV1(FrozenModel):
    activation_id: str
    activity_id: str
    reference_digest: str
    terminal_receipt_digest: str | None


class PendingInterruptStatusV1(FrozenModel):
    node_id: str
    actions: tuple[str, ...]
    reason_category: str


class StatusV1(FrozenModel):
    schema_version: Literal["1"]
    invocation_id: str
    lock_digest: str
    root_input_digest: str
    initial_tree_id: str
    current_head_tree_id: str
    status: Literal["running", "blocked", "interrupted", "stopped", "failed", "completed"]
    entrypoint: str
    graph_hierarchy: tuple[GraphStatusV1, ...]
    node_states: tuple[NodeStatusV1, ...]
    selected_test_families: tuple[str, ...]
    coverage_progress: CoverageProgressV1 | None
    durable_effects: tuple[EffectStatusV1, ...]
    adapter_evidence: tuple[AdapterEvidenceRefV1, ...]
    pending_interrupt: PendingInterruptStatusV1 | None
    terminal_reason: str | None


def render_status(projection: InvocationProjection) -> StatusV1:
    return StatusV1.model_validate(project_status_fields(projection))
```

`compile` authenticates sources and prints lock/workflow identity without starting. `bindings build` calls Task 11 and accepts only a closed manifest plus absent/empty output directory; it accepts no engine root, project directory, provider, or secret argument and never installs its output. `start` requires `--project-dir`, `--engine-root`, `--invocation-id`, exact `--product`, `--binding-dist`, `--binding-entrypoint deployment`, `--binding-declaration`, `--config-tree`, `--entrypoint`, canonical `--input`, and one `--secret HANDLE=env:NAME|file:/absolute/path` per required handle; it captures the seed, atomically creates an invocation without driving it, and prints invocation ID, lock digest, composition digest, seed tree ID, and root-input digest. No run command builds or installs code. `run` opens or starts and drives to block/terminal with exit codes 0/20/30/40. `status` reads only authenticated engine state. `resume` accepts only the pending interrupt's declared action and reason, never new product/config/input. `lock show` prints the authenticated closed projection. Repeated `run`, `status`, `resume`, `export`, and `lock show` require the same explicit source coordinates and authenticate them against the lock. Every applicable command has `--json`, stable exit codes, atomic output, and no ambient provider/config/runtime fallback.

- [ ] **Step 4: Run CLI and product composition tests**

Run: `uv run pytest tests/phase5/test_cli_compile.py tests/phase5/test_cli_bindings_build.py tests/phase5/test_cli_lifecycle.py tests/phase5/test_cli_status_and_lock.py tests/phase5/test_cli_fail_closed.py tests/phase5/test_product_composition.py -q`

Expected: PASS for happy paths, invalid sources/input, lock drift, wrong authorization, missing invocation, active-run conflict, invalid resume, and every JSON schema.

- [ ] **Step 5: Build and invoke the installed script**

Run: `uv build --package assurance-product`

Expected: PASS.

Run: `uv run aa-next --help`

Expected: exit 0 and the exact command tree above; `aa --help` remains unchanged.

- [ ] **Step 6: Review, report, and commit**

```bash
git add packages/assurance-product tests/phase5/test_cli_compile.py tests/phase5/test_cli_bindings_build.py tests/phase5/test_cli_lifecycle.py tests/phase5/test_cli_status_and_lock.py tests/phase5/test_cli_fail_closed.py
git commit -m "feat(product): add explicit aa-next command surface"
```

### Task 20: Wire Result Export Through `aa-next`

**Files:**
- Create: `packages/assurance-product/assurance_product/export.py`
- Create: `packages/assurance-product/assurance_product/resources/schemas/result-export-v1.json`
- Modify: `packages/assurance-product/assurance_product/cli.py`
- Modify: `packages/assurance-product/assurance_product/models.py`
- Create: `tests/phase5/test_result_export.py`
- Create: `tests/phase5/test_cli_export.py`
- Create: `tests/phase5/test_export_security.py`

**Interfaces:**
- Consumes: Task 3 immutable tree export, authenticated invocation events/lock, final head tree, status projection, report/archive/Retro/Improvement artifact references.
- Produces: `ResultExportV1`, `export_invocation(engine: Engine, invocation_id: str, destination: Path, *, authorization: InvocationRuntimeAuthorization) -> ResultExportV1`, and functional `aa-next export`.

- [ ] **Step 1: Write failing deterministic export tests**

```python
def test_export_contains_authenticated_projection_and_result_tree(completed_invocation, tmp_path):
    exported = export_invocation(
        completed_invocation.engine,
        completed_invocation.id,
        tmp_path / "export",
        authorization=completed_invocation.authorization,
    )
    assert exported.lock_digest == completed_invocation.lock_digest
    assert exported.status.status == "completed"
    assert exported.result_tree_digest == digest_directory(tmp_path / "export/result-tree")
    assert (tmp_path / "export/manifest.json").exists()


def test_repeated_export_is_byte_identical(completed_invocation, tmp_path):
    export_invocation(completed_invocation.engine, completed_invocation.id, tmp_path / "a", authorization=completed_invocation.authorization)
    export_invocation(completed_invocation.engine, completed_invocation.id, tmp_path / "b", authorization=completed_invocation.authorization)
    assert digest_directory(tmp_path / "a") == digest_directory(tmp_path / "b")
```

- [ ] **Step 2: Run tests and verify export implementation is absent**

Run: `uv run pytest tests/phase5/test_result_export.py tests/phase5/test_cli_export.py tests/phase5/test_export_security.py -q`

Expected: FAIL because `ResultExportV1` and `export_invocation` do not exist.

- [ ] **Step 3: Implement authenticated immutable export**

```python
class ExportedArtifactV1(FrozenModel):
    artifact_id: str
    relative_path: str
    media_type: str
    sha256: str


class ResultExportV1(FrozenModel):
    schema_version: Literal["1"]
    invocation_id: str
    lock_digest: str
    event_stream_digest: str
    result_tree_digest: str
    status: StatusV1
    artifact_index: tuple[ExportedArtifactV1, ...]
```

Authenticate invocation root, authorization digest, ledger chain, projection, final head, and artifact paths before writing. Export into a fresh staging directory, materialize the immutable result tree, write canonical JSON manifest/status/artifact index, fsync, and atomically rename. Reject live indeterminate activity, missing terminal receipt, path escape, symlink, hardlink, non-empty destination, an in-place original-SUT destination, event corruption, lock drift, and any resolved secret bytes.

- [ ] **Step 4: Run export, tree I/O, and CLI tests**

Run: `uv run pytest tests/phase5/test_result_export.py tests/phase5/test_cli_export.py tests/phase5/test_export_security.py packages/graph-engine/tests/runtime/test_tree_io.py packages/graph-engine/tests/runtime/test_tree_io_faults.py -q`

Expected: PASS for completed invocations with identical repeated bytes; running, interrupted, stopped, drifted, indeterminate, and failed invocations all refuse result-tree export.

- [ ] **Step 5: Review, report, and commit**

```bash
git add packages/assurance-product/assurance_product/export.py packages/assurance-product/assurance_product/cli.py packages/assurance-product/assurance_product/models.py packages/assurance-product/assurance_product/resources/schemas tests/phase5/test_result_export.py tests/phase5/test_cli_export.py tests/phase5/test_export_security.py
git commit -m "feat(product): export authenticated invocation results"
```

### Task 21: Define Behavioral Projections and the Isolated Comparison Harness

**Files:**
- Create: `benchmark/assurance-product-phase5/compare.py`
- Create: `benchmark/assurance-product-phase5/projection.py`
- Create: `benchmark/assurance-product-phase5/eval.py`
- Create: `benchmark/assurance-product-phase5/schemas/behavioral-projection-v1.json`
- Create: `tests/phase5/test_behavioral_projection.py`
- Create: `tests/phase5/test_comparison_isolation.py`
- Create: `tests/phase5/test_comparison_dispositions.py`

**Interfaces:**
- Consumes: immutable legacy export, immutable new-runtime `ResultExportV1`, and Task 1 governed comparison dispositions.
- Produces: `BehavioralProjectionV1`, `ComparisonResultV1`, `ExternalEvalV1`, `project_legacy_export(path: Path) -> BehavioralProjectionV1`, `project_new_export(path: Path) -> BehavioralProjectionV1`, `compare_case(case_id: str, legacy_export: Path, new_export: Path, dispositions: Mapping[str, DispositionV1]) -> ComparisonResultV1`, and `evaluate_complete_runs(opencode_export: Path, cursor_export: Path) -> ExternalEvalV1`.

- [ ] **Step 1: Write failing normalization and isolation tests**

```python
def test_projections_ignore_provider_conversation_noise(legacy_export, new_export):
    legacy = project_legacy_export(legacy_export)
    new = project_new_export(new_export)
    assert legacy.artifact_contract == new.artifact_contract
    assert "session_id" not in legacy.model_dump_json()
    assert "conversation" not in new.model_dump_json()


def test_comparison_modules_do_not_import_runtime_packages():
    imports = imported_top_level_modules(Path("benchmark/assurance-product-phase5"))
    assert "assurance_agent" not in imports
    assert "graph_engine" not in imports
    assert "assurance_product" not in imports
```

- [ ] **Step 2: Run tests and verify harness modules are absent**

Run: `uv run pytest tests/phase5/test_behavioral_projection.py tests/phase5/test_comparison_isolation.py tests/phase5/test_comparison_dispositions.py -q`

Expected: FAIL because projection and comparison modules do not exist.

- [ ] **Step 3: Implement data-only projection and governed comparison**

```python
JSONValue: TypeAlias = None | bool | int | float | str | tuple["JSONValue", ...] | Mapping[str, "JSONValue"]


class ProjectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class GateDecisionV1(ProjectionModel):
    semantic_role: str
    decision: str
    input_digest: str


class ArtifactObservationV1(ProjectionModel):
    artifact_id: str
    media_type: str
    sha256: str
    semantic_projection: JSONValue


class ChangedFileV1(ProjectionModel):
    path: str
    sha256: str


class ExecutionEvidenceV1(ProjectionModel):
    summary: JSONValue
    digest: str


class QualityMetricsV1(ProjectionModel):
    coverage: JSONValue
    trace: JSONValue
    quality: JSONValue


class IssueHealingDecisionV1(ProjectionModel):
    issue_class: str
    decision: str
    evidence_digest: str


class EffectProjectionV1(ProjectionModel):
    effect_id: str
    idempotency_key: str
    status: str
    receipt_digest: str | None
    observed_external_projection: JSONValue


class SemanticArtifactProjectionV1(ProjectionModel):
    present: bool
    digest: str | None
    semantic_fields: JSONValue


ReportProjectionV1 = SemanticArtifactProjectionV1
RetroProjectionV1 = SemanticArtifactProjectionV1
ImprovementProjectionV1 = SemanticArtifactProjectionV1
ArchiveProjectionV1 = SemanticArtifactProjectionV1


class SemanticCountsV1(ProjectionModel):
    retries: Mapping[str, int]
    interrupts: Mapping[str, int]
    stops: Mapping[str, int]


class RedactedDiagnosticV1(ProjectionModel):
    category: str
    message: str


class BehavioralProjectionV1(ProjectionModel):
    schema_version: Literal["1"]
    case_id: str
    input_digest: str
    runtime_identity: str
    terminal_class: str
    terminal_reason_category: str | None
    selected_families: frozenset[str]
    activated_families: frozenset[str]
    completed_families: frozenset[str]
    skipped_families: frozenset[str]
    gate_decisions: tuple[GateDecisionV1, ...]
    artifact_contract: tuple[ArtifactObservationV1, ...]
    changed_files: tuple[ChangedFileV1, ...]
    execution_evidence: ExecutionEvidenceV1
    quality_metrics: QualityMetricsV1
    issue_healing_decisions: tuple[IssueHealingDecisionV1, ...]
    durable_effects: tuple[EffectProjectionV1, ...]
    report: ReportProjectionV1
    retro: RetroProjectionV1 | None
    improvement: ImprovementProjectionV1 | None
    archive: ArchiveProjectionV1 | None
    semantic_counts: SemanticCountsV1
    diagnostics: tuple[RedactedDiagnosticV1, ...]


class DispositionV1(ProjectionModel):
    case_id: str
    field: str
    mode: Literal["exact", "set", "predicate", "intentionally-different"]
    classification: Literal["required", "legacy-bug", "unspecified", "observational-noise"]
    predicate: str | None
    governing_contract: str


class ComparisonResultV1(ProjectionModel):
    case_id: str
    passed: bool
    governed_differences: tuple[JSONValue, ...]
    undisposed_differences: tuple[JSONValue, ...]


def compare_case(case_id: str, legacy_export: Path, new_export: Path, dispositions: Mapping[str, DispositionV1]) -> ComparisonResultV1:
    legacy = project_legacy_export(legacy_export)
    current = project_new_export(new_export)
    return apply_governed_dispositions(case_id, diff_projection(legacy, current), dispositions)


class EvalFindingV1(ProjectionModel):
    field: str
    mode: Literal["exact", "set", "predicate", "intentionally-different"]
    outcome: Literal["pass", "fail", "intentionally-different"]
    evidence_digest: str


class ExternalEvalV1(ProjectionModel):
    schema_version: Literal["1"]
    opencode_export_digest: str
    cursor_export_digest: str
    findings: tuple[EvalFindingV1, ...]
    retro_input_digest: str
```

Run legacy and new cases in separate subprocesses with distinct source copies, invocation roots, ledgers, checkpoints, effect stores, secret material, invocation namespaces, and mutable external targets. Never double-apply one real durable effect merely for comparison. Parse only files beneath the export roots; never open runtime state. Normalize timestamps, event sequence numbers, provider session IDs, token counts, log prose, and ordering noise. Compare governed terminal class, family execution, required artifact schemas/content digests, issue classes, coverage outcome, STOP/interrupt class, and Retro/Improvement execution. Every mismatch requires an exact case/field disposition with `required`, `legacy-bug`, `unspecified`, or `observational-noise` classification and a governing contract citation. `evaluate_complete_runs` is an external data-only evaluator, not a graph node or registry; it authenticates both completed export digests and emits canonical findings/Retro input without importing either runtime.

- [ ] **Step 4: Run projection, mutation, and isolation tests**

Run: `uv run pytest tests/phase5/test_behavioral_projection.py tests/phase5/test_comparison_isolation.py tests/phase5/test_comparison_dispositions.py -q`

Expected: PASS; changing any governed field fails, while only enumerated observational noise normalizes away.

- [ ] **Step 5: Review, report, and commit**

```bash
git add benchmark/assurance-product-phase5 tests/phase5/test_behavioral_projection.py tests/phase5/test_comparison_isolation.py tests/phase5/test_comparison_dispositions.py
git commit -m "test(phase5): add isolated behavioral comparison harness"
```

### Task 22: Implement the Deterministic 25-Case Comparison Matrix

**Files:**
- Create: `benchmark/assurance-product-phase5/comparison-manifest.json`
- Create: `benchmark/assurance-product-phase5/run-comparison.sh`
- Create: `tests/phase5/fixtures/comparison/legacy/`
- Create: `tests/phase5/fixtures/comparison/current/`
- Create: `tests/phase5/test_comparison_matrix.py`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/comparison-dispositions.yaml`

**Interfaces:**
- Consumes: Task 21 comparison harness and the exact 25 cases from spec Section 18.
- Produces: `ComparisonManifestV1`, 25 authenticated fixture pairs, and one machine-readable aggregate result with zero undisposed governed mismatches.

- [ ] **Step 1: Write the failing exact-matrix test**

```python
def test_comparison_manifest_is_the_exact_spec_matrix(comparison_manifest):
    expected = (
        "full-api-only-success",
        "full-e2e-only-success",
        "full-fuzz-only-success",
        "full-performance-only-success",
        "full-all-four-family-success",
        "intake-review-needs-fix-then-pass",
        "plan-review-invalid-output-bounded-retry",
        "codegen-validation-failure-bounded-fix",
        "execution-closed-mapping-no-stale-test",
        "coverage-insufficient-repair-reexecution-pass",
        "coverage-repair-no-progress-exhausted",
        "healing-disallowed-business-stop",
        "report-generation-required-outputs",
        "issue-analysis-reconcile-path",
        "archive-durable-effect-replay",
        "retro-collect-analyze-propose-reconcile",
        "improvement-review-evaluate-export-apply",
        "improvement-rollback",
        "human-interrupt-exact-resume",
        "transient-local-retry",
        "opencode-ambiguous-create-recovery",
        "cursor-unknown-process-indeterminate",
        "engine-crash-after-provider-terminal-receipt",
        "config-model-graph-source-drift-rejection",
        "replay-after-provider-state-removal",
    )
    assert tuple(item.id for item in comparison_manifest.cases) == expected
    assert len(comparison_manifest.cases) == 25
    assert len(set(item.id for item in comparison_manifest.cases)) == 25
    assert all(item.legacy_export_digest.startswith("sha256:") for item in comparison_manifest.cases)
    assert all(item.current_export_digest.startswith("sha256:") for item in comparison_manifest.cases)
```

Export this exact literal as `EXPECTED_25_CASE_IDS` from `tests/phase5/conformance.py` so the ledger and benchmark checks consume one repository-owned expected set rather than deriving it from the manifest under test.

- [ ] **Step 2: Run tests and verify manifest/fixture absence**

Run: `uv run pytest tests/phase5/test_comparison_matrix.py -q`

Expected: FAIL because the exact matrix and authenticated fixtures do not exist.

- [ ] **Step 3: Add all 25 cases and deterministic fixture generation**

```python
manifest = {
    "schema_version": "1",
    "cases": [
        {
            "id": case_id,
            "legacy_export": f"fixtures/comparison/legacy/{case_id}",
            "current_export": f"fixtures/comparison/current/{case_id}",
            "legacy_export_digest": digest_directory(legacy_root / case_id),
            "current_export_digest": digest_directory(current_root / case_id),
        }
        for case_id in EXPECTED_25_CASE_IDS
    ],
}
write_canonical_json(manifest_path, manifest)
```

Generate every case in the literal order above. Generate legacy and current fixtures in separate subprocesses and copy only immutable exports into the matrix tree; `digest_directory` returns the canonical `sha256:` plus 64-lowercase-hex identity written directly into the committed manifest.

- [ ] **Step 4: Run all 25 comparisons**

Run: `bash benchmark/assurance-product-phase5/run-comparison.sh`

Expected: exactly 25 executed, 25 governed passes, 0 missing, 0 extra, 0 undisposed mismatches, and no runtime-state access.

- [ ] **Step 5: Run mutation and disposition checks**

Run: `uv run pytest tests/phase5/test_comparison_matrix.py tests/phase5/test_comparison_dispositions.py tests/phase5/test_comparison_isolation.py -q`

Expected: PASS, including one mutation per governed projection field and rejection of stale/wildcard dispositions.

- [ ] **Step 6: Review, report, and commit**

```bash
git add benchmark/assurance-product-phase5/comparison-manifest.json benchmark/assurance-product-phase5/run-comparison.sh tests/phase5/fixtures/comparison tests/phase5/test_comparison_matrix.py
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/comparison-dispositions.yaml
git commit -m "test(phase5): cover the 25-case comparison matrix"
```

### Task 23: Verify Committed-HEAD Wheel Isolation and Source Authentication

**Files:**
- Create: `scripts/assurance_product_wheel_smoke_test.sh`
- Create: `tests/phase5/test_wheel_smoke_contract.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: committed HEAD, product wheel, six capability wheels, both adapter wheels, generated deployment wheels, project configuration fixture, and graph-engine wheel.
- Produces: a four-environment isolation matrix proving wheel metadata, source authentication, dependency closure, CLI installation, and fail-closed missing-provider behavior.

- [ ] **Step 1: Write the failing smoke-script contract test**

```python
def test_wheel_smoke_covers_exact_install_matrix(repo_root):
    script = (repo_root / "scripts/assurance_product_wheel_smoke_test.sh").read_text()
    assert "git archive HEAD" in script
    assert "base-no-adapter" in script
    assert "opencode-product" in script
    assert "cursor-product" in script
    assert "source-drift" in script
    assert "aa-next compile" in script
```

- [ ] **Step 2: Run the test and verify script absence**

Run: `uv run pytest tests/phase5/test_wheel_smoke_contract.py -q`

Expected: FAIL because the smoke script does not exist.

- [ ] **Step 3: Implement the committed-HEAD isolation matrix**

```bash
#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
archive_root="$(mktemp -d)"
trap 'rm -rf "${archive_root}"' EXIT
git -C "${repo_root}" archive HEAD | tar -x -C "${archive_root}"
```

The script builds only from the archive and creates fresh venvs for: base product without adapter (inspection succeeds, composition fails), OpenCode product plus generated OpenCode deployment wheel/config tree (compile succeeds), Cursor equivalent (compile succeeds), and source-drift tampering (compile fails before graph start). Inspect installed distributions/import roots/entry points and scan wheels/exports for legacy imports and secret fixture bytes. Use explicit temporary paths and delete them through the trap only.

- [ ] **Step 4: Commit the smoke implementation so HEAD is the test input**

```bash
git add scripts/assurance_product_wheel_smoke_test.sh tests/phase5/test_wheel_smoke_contract.py .github/workflows/ci.yml
git commit -m "test(packaging): verify assurance product wheel isolation"
```

- [ ] **Step 5: Run the smoke test from the committed snapshot**

Run: `bash scripts/assurance_product_wheel_smoke_test.sh`

Expected: all four environments pass their declared success/failure outcome and leave legacy `aa` untouched.

- [ ] **Step 6: Re-run contract checks and record review**

Run: `uv run pytest tests/phase5/test_wheel_smoke_contract.py tests/phase5/test_product_packaging.py -q`

Expected: PASS.

Use `superpowers:requesting-code-review` and record the clean committed-HEAD smoke output in `task-23-report.md`. If review changes code, commit the focused fix and rerun the entire smoke matrix from the new committed HEAD.

### Task 24: Run One Full OpenCode Provider-Live Benchmark

**Files:**
- Create: `benchmark/assurance-product-phase5/manifest.json`
- Create: `benchmark/assurance-product-phase5/run_item.py`
- Create: `benchmark/assurance-product-phase5/run-opencode.sh`
- Create: `tests/phase5/test_phase5_benchmark_manifest.py`
- Create: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/opencode-benchmark.md`

**Interfaces:**
- Consumes: installed `assurance-opencode` product, generated OpenCode deployment wheel, one data-only project configuration tree, one benchmark item, runtime secret authorization, and `aa-next run/export`.
- Produces: one terminal full-workflow OpenCode invocation export and an evidence record covering intake, all manifest-selected families, execution, issue/healing/coverage behavior, report, and the manifest-selected optional archive behavior.

- [ ] **Step 1: Write the failing OpenCode benchmark contract test**

```python
FULL_WORKFLOW_REQUIRED_STEPS = (
    "intake.intake", "intake.explore", "intake.case-design", "intake.case-review",
    "generation.api.plan", "generation.api.plan-review", "generation.api.codegen",
    "generation.e2e.plan", "generation.e2e.plan-review", "generation.e2e.codegen",
    "generation.fuzz.plan", "generation.fuzz.plan-review", "generation.fuzz.codegen",
    "generation.performance.plan", "generation.performance.plan-review", "generation.performance.codegen",
    "execution.execute", "execution.run", "quality.fact-baseline", "quality.inspect", "quality.report",
)


def test_opencode_benchmark_is_one_full_locked_item(phase5_manifest):
    item = phase5_manifest.item("opencode-ret-dept-management")
    assert item.sut_item_id == "RET-dept-management"
    assert item.product == "assurance-opencode"
    assert item.entrypoint == "full"
    assert item.selected_test_families == ("api", "e2e", "fuzz", "performance")
    assert item.auto_archive is True
    assert item.adapter_version == "0.1.0"
    assert item.expected_terminal == "completed"
    assert item.required_steps == FULL_WORKFLOW_REQUIRED_STEPS
    assert set(item.routing_assignments) == set(PREPARE_IDS)
    assert item.routing_assignments == item.deployment_binding_routes
    assert all(route.provider_model == "openai/gpt-5.6-terra" for route in item.routing_assignments.values())
    assert all(route.worker_profile == "max" for route in item.routing_assignments.values())
```

- [ ] **Step 2: Run the contract test and verify manifest/runner absence**

Run: `uv run pytest tests/phase5/test_phase5_benchmark_manifest.py -q`

Expected: FAIL because the Phase 5 live manifest is absent.

- [ ] **Step 3: Add a fail-closed single-item OpenCode runner**

```bash
#!/usr/bin/env bash
set -euo pipefail
exec uv run python benchmark/assurance-product-phase5/run_item.py \
  --item opencode-ret-dept-management \
  --adapter opencode
```

The Python runner builds/installs the exact deployment wheel, starts one fresh invocation, follows `aa-next status --json` until terminal, exports results, validates required artifacts and workflow steps, and writes a redacted evidence summary. It rejects an adapter/model mismatch, pre-existing result directory, unselected item tests, any absent manifest-selected API/E2E/Fuzz/Performance branch, missing report, archive behavior inconsistent with `auto_archive`, coverage-loop contract violation, or provider session text used as status.

- [ ] **Step 4: Execute and follow the OpenCode benchmark to terminal**

Run: `bash benchmark/assurance-product-phase5/run-opencode.sh`

Expected: one item reaches the declared successful terminal state; its export passes schema/digest checks and all required workflow-step assertions. If the provider is unavailable, leave Task 24 incomplete and preserve the redacted failure evidence.

- [ ] **Step 5: Review the complete trace and record evidence**

Inspect status transitions, event projection, generated case counts, selected-family execution, coverage loop, issue analysis, healing, report, and configured archive behavior. Record exact per-capability model routing, effort/worker profiles, start/end time, lock digest, terminal status, artifact digests, and any governed deviation in `opencode-benchmark.md`.

- [ ] **Step 6: Run deterministic checks and commit**

Run: `uv run pytest tests/phase5/test_phase5_benchmark_manifest.py tests/phase5/test_full_graph_audit.py tests/phase5/test_result_export.py -q`

Expected: PASS.

```bash
git add benchmark/assurance-product-phase5/manifest.json benchmark/assurance-product-phase5/run_item.py benchmark/assurance-product-phase5/run-opencode.sh tests/phase5/test_phase5_benchmark_manifest.py
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/opencode-benchmark.md
git commit -m "test(phase5): verify full OpenCode product workflow"
```

### Task 25: Run One Full Cursor Provider-Live Benchmark

**Files:**
- Modify: `benchmark/assurance-product-phase5/manifest.json`
- Modify: `benchmark/assurance-product-phase5/run_item.py`
- Create: `benchmark/assurance-product-phase5/run-cursor.sh`
- Create: `benchmark/assurance-product-phase5/run-eval-and-retro.sh`
- Modify: `tests/phase5/test_phase5_benchmark_manifest.py`
- Create: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/cursor-benchmark.md`

**Interfaces:**
- Consumes: installed `assurance-cursor` product, generated Cursor deployment wheel, real confined process host, one data-only project configuration tree, one benchmark item, runtime secret authorization, and `aa-next run/export`.
- Produces: one terminal full-workflow Cursor invocation export, equivalent workflow evidence to Task 24, one external Eval over both complete runs, and authenticated `retro` plus Improvement entrypoint executions consuming that Eval result.

- [ ] **Step 1: Extend the failing manifest test for Cursor**

```python
def test_cursor_benchmark_is_one_full_locked_item(phase5_manifest):
    item = phase5_manifest.item("cursor-ret-dept-management")
    assert item.sut_item_id == "RET-dept-management"
    assert item.product == "assurance-cursor"
    assert item.entrypoint == "full"
    assert item.selected_test_families == ("api", "e2e", "fuzz", "performance")
    assert item.auto_archive is True
    assert item.adapter_version == "0.1.0"
    assert item.expected_terminal == "completed"
    assert item.required_steps == FULL_WORKFLOW_REQUIRED_STEPS
    assert set(item.routing_assignments) == set(PREPARE_IDS)
    assert item.routing_assignments == item.deployment_binding_routes
    assert {route.provider_model for route in item.routing_assignments.values()} == {"cursor-grok-4.6"}
    assert all(route.worker_profile for route in item.routing_assignments.values())
    assert item.requires_real_confined_process_host is True


def test_live_release_runs_external_eval_then_retro_and_improvement(phase5_manifest):
    release = phase5_manifest.release_evaluation
    assert release.inputs == ("opencode-ret-dept-management", "cursor-ret-dept-management")
    assert release.entrypoints == (
        "retro", "improvement-review", "improvement-evaluate",
        "improvement-export", "improvement-apply", "improvement-rollback",
    )
```

- [ ] **Step 2: Run the test and verify Cursor item/runner absence**

Run: `uv run pytest tests/phase5/test_phase5_benchmark_manifest.py::test_cursor_benchmark_is_one_full_locked_item -q`

Expected: FAIL because the Cursor item and script do not exist.

- [ ] **Step 3: Add the Cursor item and platform-authenticated runner**

```bash
#!/usr/bin/env bash
set -euo pipefail
exec uv run python benchmark/assurance-product-phase5/run_item.py \
  --item cursor-ret-dept-management \
  --adapter cursor
```

Require executable digest, confinement identity, permission profile, exact model, and runtime handles from the locked deployment binding. Assert the real host is active, terminal receipt is durable, no child process survives, and provider session data is absent from engine status/export. Apply the same full-workflow checks as Task 24 without sharing its invocation or result directory. `run-eval-and-retro.sh` authenticates both complete exports, calls Task 21 `evaluate_complete_runs`, supplies its canonical result as the declared Retro input, and runs the listed Retro/Improvement entrypoints in fresh invocations, including rollback against the just-applied isolated Improvement target; Eval itself never appears in the workflow.

- [ ] **Step 4: Execute and follow the Cursor benchmark to terminal**

Run: `bash benchmark/assurance-product-phase5/run-cursor.sh`

Expected: one item reaches declared successful terminal state with valid export, complete workflow coverage, durable Cursor receipt, and no surviving process. If the provider is unavailable, leave Task 25 incomplete and preserve the redacted failure evidence.

Run: `bash benchmark/assurance-product-phase5/run-eval-and-retro.sh`

Expected: both full-run export digests authenticate, external Eval completes, and the `retro`, `improvement-review`, `improvement-evaluate`, `improvement-export`, `improvement-apply`, and `improvement-rollback` entrypoints each reach their declared terminal outcome with fresh ledger evidence.

- [ ] **Step 5: Review the complete trace and record evidence**

Record exact model ID, effort/profile, executable digest, confinement identity digest, start/end time, lock digest, terminal status, artifact digests, child-process cleanup, and governed deviations in `cursor-benchmark.md`.

- [ ] **Step 6: Run deterministic checks and commit**

Run: `uv run pytest tests/phase5/test_phase5_benchmark_manifest.py tests/phase5/test_full_graph_audit.py tests/phase5/test_result_export.py packages/agent-runtime-cursor/tests/test_process_host.py -q`

Expected: PASS.

```bash
git add benchmark/assurance-product-phase5/manifest.json benchmark/assurance-product-phase5/run_item.py benchmark/assurance-product-phase5/run-cursor.sh benchmark/assurance-product-phase5/run-eval-and-retro.sh tests/phase5/test_phase5_benchmark_manifest.py
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/cursor-benchmark.md
git commit -m "test(phase5): verify full Cursor product workflow"
```

### Task 26: Close Security, Fault, Replay, Property, and Repository Gates

**Files:**
- Create: `tests/phase5/test_fault_matrix.py`
- Create: `tests/phase5/test_security_invariants.py`
- Create: `tests/phase5/test_replay_properties.py`
- Create: `tests/phase5/test_no_legacy_bridge.py`
- Modify: `tests/phase5/ownership.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/ownership.yaml`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/residuals.yaml`

**Interfaces:**
- Consumes: all Phase 5 engine, adapter, product, CLI, export, comparison, packaging, and benchmark outputs.
- Produces: executable coverage of every spec Section 22 fault, property-based replay invariants, import/state isolation proof, and green full-repository gates.

- [ ] **Step 1: Write the fault-matrix parametrization before filling any uncovered row**

```python
PHASE5_FAULT_IDS = (
    "deployment-route-invalid", "deployment-route-unknown", "deployment-route-missing",
    "deployment-route-duplicate", "deployment-route-fallback", "deployment-raw-secret",
    "deployment-arbitrary-code", "builder-before-file-publication", "builder-after-file-publication",
    "builder-before-wheel-publication", "builder-after-wheel-publication",
    "generated-declaration-contribution-mismatch", "wrong-adapter-dependency",
    "unselected-adapter-source", "deployment-wheel-drift", "binding-through-project-config",
    "project-config-executable-shape", "project-config-runtime-authority",
    "alias-missing", "alias-extra", "alias-forged",
    "seed-capture-cut", "seed-captured-before-start", "initial-tree-publication-cut",
    "initial-tree-durable-before-intent", "start-intent-publication-cut",
    "bootstrap-before-append", "bootstrap-after-append", "bootstrap-before-directory-fsync",
    "repeated-start-root-input-drift", "repeated-start-seed-drift",
    "projection-missing-root-pointer", "projection-missing-predecessor",
    "projection-duplicate-predecessor-token", "projection-wrong-join-cardinality",
    "projection-token-schema-mismatch", "projection-noncanonical-pointer", "projection-replay-drift",
    "host-before-worker-spawn", "host-after-spawn-before-dispatch", "host-during-activity-rpc",
    "host-after-reference-bind", "host-response-before-quiescence", "host-terminal-receipt-publication",
    "host-receipt-durable-before-ack", "host-parent-crash-live-descendants",
    "host-secret-channel-disconnect", "host-secret-channel-revocation", "host-worker-source-drift",
    "host-duplicate-terminal-receipt", "host-foreign-terminal-receipt",
    "opencode-ambiguous-session-create", "opencode-empty-ambiguous-discovery",
    "opencode-duplicate-metadata", "opencode-prompt-admission-lost-response",
    "opencode-sse-disconnect", "opencode-transient-idle", "opencode-cancel-result-race",
    "opencode-terminal-before-restart", "opencode-provider-state-deleted-after-receipt",
    "cursor-confinement-unavailable", "cursor-executable-drift", "cursor-version-drift",
    "cursor-before-spawn", "cursor-after-spawn", "cursor-partial-ndjson", "cursor-output-overflow",
    "cursor-terminal-exit-mismatch", "cursor-unknown-process-ownership", "cursor-host-boot-change",
    "cursor-cancel-race", "cursor-descendant-cleanup-failure",
    "effect-before-intent", "effect-after-intent", "effect-receipt-publication",
    "effect-reconcile-lost-ack", "export-file-write", "export-rename", "export-directory-fsync",
    "export-destination-race", "export-destination-symlink", "export-destination-hardlink",
    "comparison-input-drift", "comparison-one-side-running", "comparison-partial-report",
)


def test_every_phase5_fault_fails_closed(phase5_system):
    assert tuple(phase5_system.fault_cases) == PHASE5_FAULT_IDS
    for fault_id, fault in phase5_system.fault_cases.items():
        observed = phase5_system.inject_and_observe(fault)
        assert observed.error_class == fault.expected_error_class, fault_id
        assert observed.dispatch_count == fault.expected_dispatch_count, fault_id
        assert observed.authoritative_state == fault.expected_authoritative_state, fault_id
        assert observed.legal_next_action == fault.expected_legal_next_action, fault_id
        assert observed.outcome == fault.expected_outcome, fault_id
        assert observed.secret_leak_count == 0, fault_id
        assert observed.replay_outcome == fault.expected_replay_outcome, fault_id
```

```python
@given(crash_points=st.sets(st.integers(min_value=0, max_value=80)))
def test_crash_replay_matches_uninterrupted_run(crash_points, deterministic_phase5_scenario):
    uninterrupted = deterministic_phase5_scenario.run()
    recovered = deterministic_phase5_scenario.run_with_crashes(crash_points)
    assert recovered.behavioral_projection == uninterrupted.behavioral_projection
    assert recovered.effect_ids == uninterrupted.effect_ids
```

- [ ] **Step 2: Run Phase 5 tests and enumerate every failing/missing fault row**

Run: `uv run pytest tests/phase5 -q`

Expected: FAIL until every Section 22 row is represented and all security/replay/isolation assertions pass.

- [ ] **Step 3: Close only high-confidence gaps within their owning modules**

For each failure, add one minimal fix in the owner file already named by Tasks 2-25 and add its exact regression assertion to the relevant Phase 5 test. The invariants are executable and closed:

```python
FORBIDDEN_PRODUCT_IMPORTS = {"assurance_agent", "assurance_kernel"}
FORBIDDEN_ENGINE_IMPORT_PREFIXES = {"assurance_", "agent_runtime_"}
FORBIDDEN_SERIALIZED_KEYS = {"secret_value", "credential", "provider_conversation", "legacy_checkpoint"}
SUPPORTED_PRODUCTION_PLATFORMS = {"linux", "darwin"}
```

Do not weaken an assertion, add a fallback, or mark a fault expected merely to make the suite green. Update ownership/residual ledgers with exact proof paths after each resolved gap.

- [ ] **Step 4: Run focused package suites**

Run: `uv run pytest packages/graph-engine/tests -q`

Expected: PASS with the existing single intentional skip only.

Run: `uv run pytest packages/agent-runtime-contracts/tests packages/agent-runtime-opencode/tests packages/agent-runtime-cursor/tests tests/phase4 tests/phase5 -q`

Expected: PASS; no unexpected skip or xfail.

- [ ] **Step 5: Run static and architectural gates**

Run: `uv run ruff check .`

Expected: PASS.

Run: `uv run ruff format --check .`

Expected: PASS.

Run: `uv run pyright`

Expected: PASS.

Run: `uv run lint-imports`

Expected: PASS, including no assurance/adapter imports from `graph-engine` and no legacy imports from `assurance-product`.

- [ ] **Step 6: Run full tests and all packaging smokes**

Run: `uv run pytest`

Expected: PASS.

Run: `bash scripts/packaging_smoke_test.sh`

Expected: PASS.

Run: `bash scripts/graph_engine_smoke_test.sh`

Expected: PASS.

Run: `bash scripts/agent_runtime_wheel_smoke_test.sh`

Expected: PASS.

Run: `bash scripts/assurance_capability_wheel_smoke_test.sh`

Expected: PASS.

Run: `bash scripts/assurance_product_wheel_smoke_test.sh`

Expected: PASS.

- [ ] **Step 7: Review, report, and commit**

Write exact counts, skips, warnings, property examples, and smoke results to `task-26-report.md`, request code review, resolve every P1/P2, then:

```bash
git add tests/phase5 .github/workflows/ci.yml
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/ownership.yaml .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/residuals.yaml
git commit -m "test(phase5): close product assembly release gates"
```

### Task 27: Publish Phase 5 Acceptance and the Exact Phase 6 Handoff

**Files:**
- Create: `docs/superpowers/specs/2026-08-22-pure-graph-engine-phase5-acceptance.md`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/phase6-handoff.md`
- Create: `tests/phase5/test_phase5_acceptance.py`
- Create: `tests/phase5/test_phase6_handoff.py`

**Interfaces:**
- Consumes: all task reports, final ownership/residual/graph/binding/comparison ledgers, wheel smoke evidence, 25-case result, and both provider-live benchmark records.
- Produces: signed-off Phase 5 acceptance mapping all 27 spec criteria to evidence and a machine-checkable Phase 6 handoff containing the exact freeze/drain/cutover/delete obligations.

- [ ] **Step 1: Write failing acceptance and handoff completeness tests**

```python
def test_acceptance_maps_every_spec_criterion_to_passing_evidence(acceptance):
    assert tuple(item.number for item in acceptance.criteria) == tuple(range(1, 28))
    assert all(item.status == "passed" for item in acceptance.criteria)
    assert all(item.evidence_paths for item in acceptance.criteria)
    assert all(item.commit for item in acceptance.criteria)


def test_phase6_handoff_has_exact_obligations(handoff):
    assert handoff.required_actions == (
        "freeze_new_legacy_starts",
        "drain_or_audit_terminate_legacy_invocations",
        "switch_aa_to_assurance_product",
        "remove_aa_next_name",
        "decide_and_implement_safe_in_place_result_application_if_required",
        "delete_legacy_runtime_and_product_implementation",
        "delete_product_hooks_catalogs_old_entrypoints_resources_and_obsolete_tests",
        "remove_comparison_only_compatibility_baseline_where_unneeded",
        "add_no_old_invocation_resume_adapter_or_forwarding_import",
    )
    assert handoff.compatibility_bridge_allowed is False
```

- [ ] **Step 2: Run tests and verify acceptance is absent/incomplete**

Run: `uv run pytest tests/phase5/test_phase5_acceptance.py tests/phase5/test_phase6_handoff.py -q`

Expected: FAIL until all 27 criteria have fresh passing evidence and the handoff is exact.

- [ ] **Step 3: Write evidence-backed acceptance and handoff documents**

```python
class AcceptanceCriterionV1(FrozenModel):
    criterion: int
    requirement: str
    status: Literal["passed"]
    evidence_paths: tuple[str, ...]
    commit: str


def resolve_full_commit_for_task(task: int) -> str:
    report = EVIDENCE_ROOT / f"task-{task}-report.md"
    matches = re.findall(r"(?m)^commit: ([0-9a-f]{40})$", report.read_text(encoding="utf-8"))
    if len(matches) != 1:
        raise AssertionError(f"{report} must contain exactly one full commit")
    subprocess.run(["git", "cat-file", "-e", f"{matches[0]}^{{commit}}"], check=True)
    return matches[0]


criterion = AcceptanceCriterionV1(
    criterion=1,
    requirement="Phase 4's six wheels remain independently buildable and isolated",
    status="passed",
    evidence_paths=(
        "scripts/assurance_capability_wheel_smoke_test.sh",
        "tests/phase5/test_phase5_acceptance.py",
    ),
    commit=resolve_full_commit_for_task(26),
)
assert re.fullmatch(r"[0-9a-f]{40}", criterion.commit)
```

Render the resolved full commit into each committed acceptance row. Include exact commands/counts/digests, OpenCode and Cursor terminal evidence, all 25 comparisons, known non-blocking warnings, allowed Phase 6 residuals, and zero P1/P2 findings. The handoff must publish all items listed in spec Section 25: product/source digests, entry-point coordinates, wheel constraints, builder/template identity, representative binding wheels, workflow/config/schema identities, 99-binding report, host/wire identity, comparison/benchmark evidence, old-invocation disposition mechanism, command mapping, deletion inventory, no-import proof, and rollback boundary. It must name every legacy path/distribution/entry point/script/test to freeze or delete and the evidence required before each destructive Phase 6 action; it must not perform those actions.

- [ ] **Step 4: Re-run acceptance, handoff, and evidence-conformance tests**

Run: `uv run pytest tests/phase5/test_phase5_acceptance.py tests/phase5/test_phase6_handoff.py tests/phase5/test_phase5_ledgers.py -q`

Expected: PASS with all 27 criteria, no missing evidence file, no abbreviated commit, no unresolved residual, and no compatibility bridge.

- [ ] **Step 5: Run the final release gate from a clean tree**

Run: `git status --short`

Expected: only the Task 27 acceptance/handoff/test files are uncommitted.

Run: `uv run ruff check .`

Run: `uv run ruff format --check .`

Run: `uv run pyright`

Run: `uv run lint-imports`

Run: `uv run pytest`

Expected: every command exits 0 with no unexpected skip or xfail.

Run: `bash scripts/packaging_smoke_test.sh`

Run: `bash scripts/graph_engine_smoke_test.sh`

Run: `bash scripts/agent_runtime_wheel_smoke_test.sh`

Run: `bash scripts/assurance_capability_wheel_smoke_test.sh`

Run: `bash scripts/assurance_product_wheel_smoke_test.sh`

Expected: every script exits 0 from committed-HEAD artifacts.

- [ ] **Step 6: Request final code review and commit**

Use `superpowers:requesting-code-review` against the Phase 5 merge base. Resolve every P1/P2, rerun the affected focused tests and the final release gate, record the final review in `task-27-report.md`, then:

```bash
git add tests/phase5/test_phase5_acceptance.py tests/phase5/test_phase6_handoff.py
git add -f docs/superpowers/specs/2026-08-22-pure-graph-engine-phase5-acceptance.md .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/phase6-handoff.md
git commit -m "docs(phase5): publish acceptance and phase 6 handoff"
```

---

## Final Evidence Checklist

- [ ] All 27 tasks have a local task report with RED/GREEN commands, exact test counts, review findings, and commit SHA; durable acceptance evidence is copied into committed tests, ledgers, benchmark records, and the acceptance document.
- [ ] Both product entry points compile the byte-identical canonical workflow with the exact 33/99 binding closure.
- [ ] `graph-engine` contains only generic mechanisms and imports no assurance product or runtime adapter package.
- [ ] Deployment/configuration authority is split exactly as specified, source-authenticated, and contains no fallback or secret value.
- [ ] Atomic bootstrap, replay, fixed host, Cursor confinement, exports, and comparison isolation pass fault/property tests.
- [ ] The exact 25-case matrix has zero undisposed governed mismatch.
- [ ] One OpenCode and one Cursor single-item full benchmark reach their declared terminal outcome with report evidence; external Eval plus separate Retro and Improvement entrypoint runs also complete.
- [ ] Full CI and all five packaging/isolation smoke scripts pass from committed HEAD.
- [ ] Legacy `aa` remains unchanged and operational; Phase 6 handoff contains all remaining cutover/deletion work and no compatibility bridge.
