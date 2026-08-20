# Pure Graph Engine Phase 2 Registry Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the business-neutral Phase 2 registry platform, explicit wheel/config source resolution, frozen `InvocationLock`, plugin conformance, and complete durable-effect recovery lifecycle on top of the Phase 1 engine.

**Architecture:** A single `RegistryPlatform.resolve(ResolutionRequest) -> FrozenComposition` path snapshots every explicit source, validates exact dependencies, builds five fixed immutable registries, compiles the selected workflow, and freezes canonical lock bytes before `Engine.start()`. Runtime effects use an engine-owned intent/apply/reconcile/receipt state machine; plugins only return frozen contributions and never mutate registries or perform untracked effects.

**Tech Stack:** Python 3.11, Pydantic v2, PyYAML, `packaging` PEP 440 parsing, importlib metadata, descriptor-relative POSIX filesystem operations, pytest, Ruff, Pyright, import-linter, uv/hatchling wheels.

## Global Constraints

- Work only in `/Users/lvqingquan/agent/assurance-agent/.worktrees/pure-graph-engine-phase1` on `codex/pure-graph-engine-phase1`; preserve the user's unrelated dirty main checkout.
- `graph-engine` contains no Assurance, OpenCode, Cursor, prompt-provider, default graph, default plugin, or default product semantics.
- Phase 2 migrates no real agent adapter and no real Assurance capability.
- Python wheel and explicit `plugin.yaml` directory sources are both required.
- Product/plugin sources are complete and explicit; no scanning, downloading, fallback, backtracking, or highest-version selection.
- Dependency constraints use PEP 440; the resolver validates exact selected versions only.
- Registry kinds are exactly Source, Capability, Schema, Resource, and Effect. Plugins cannot add a sixth kind.
- Plugins return frozen contributions and never receive mutable registry objects.
- Declarative plugins contain data only: no Python imports, shell commands, command arrays, executable expressions, executable files, symlinks, devices, undeclared files, or out-of-root paths.
- `InvocationLock` is persisted before the first ledger event; resume requires byte-exact canonical lock equality and writes nothing on drift.
- A task with effects remains `effect_pending` until every receipt is durable. Permanent effect failure is typed and non-retryable; the original handler is never rerun.
- Effects settle serially in canonical task/effect order in Phase 2.
- No compatibility aliases for Phase 1 `bind()`, `PluginRuntime`, `ResolvedProduct`, or `product_digest` remain at final acceptance.
- Run commands through `uv run`; Python is pinned to 3.11.
- Use TDD for every task: establish RED, implement the minimum complete behavior, run focused GREEN, then run the task regression gate and commit.
- Before every task commit, run `uv run pytest packages/graph-engine/tests -q`; no task may commit a known graph-engine test failure. Expected RED commands are run before implementation only.
- The known full-suite baseline is exactly four missing ignored benchmark-scaffold fixture failures in `tests/unit/benchmark/test_opencode_openai_loop.py`; Phase 2 may introduce no additional failure.

## File Responsibility Map

```text
packages/graph-engine/graph_engine/
  plugin_api.py                    frozen plugin/task/effect SPI values only
  composition/
    __init__.py                    narrow public composition exports
    models.py                      source refs/snapshots, registries, lock/composition values
    source_fs.py                   authenticated descriptor-relative byte snapshots
    sources.py                     installed/editable wheel and product entry-point sources
    declarative.py                 strict product/plugin YAML and config-tree loader
    dependencies.py                exact PEP 440 closure and canonical topology
    registries.py                  five fixed builders and immutable registry views
    resolver.py                    RegistryPlatform orchestration
    lock.py                        canonical InvocationLock construction/encoding
    conformance.py                 reusable wheel/config conformance runners
  runtime/
    invocation_lock.py             atomic lock install/read/authenticate
    effects.py                     effect settlement and recovery state machine
    events.py                      lock/prepared/effect runtime events
    models.py                      pure fold projection for new events
    planner.py                     non-retryable and effect-pending planning invariants
    scheduler.py                   prepared commit publication
    engine.py                      lock-bound facade and effect recovery loop
```

---

### Task 1: Define additive frozen Phase 2 contribution and effect value contracts

**Files:**
- Modify: `packages/graph-engine/graph_engine/plugin_api.py:21-328`
- Create: `packages/graph-engine/tests/composition/test_plugin_contracts.py`
- Modify: `packages/graph-engine/pyproject.toml:1-8`
- Modify: `pyproject.toml:11-27`
- Modify: `uv.lock`

**Interfaces:**
- Produces: `PluginDependency`, expanded `PluginDescriptor`, `RegistryPorts`, `PluginContribution`, `TaskFailure.retryable`, `TaskContext.effect()`, `EffectIntent`, `EffectPolicy`, `EffectApplyResult`, `EffectReconcileResult`, `DurableEffectHandler`, `EffectRegistration`, `SchemaContribution`, `ResourceContribution`, and `CapabilityBindingContribution`.
- Preserves temporarily: the Phase 1 callable `TaskHandler`, `PluginProvider.bind()`, `PluginRuntime`, and assembler so this additive task leaves every existing test green. Task 6 performs the single breaking SPI cutover and Task 8 removes the old product resolver.
- Consumers: Tasks 4, 5, 6, 7, 9, 11, and 13.

- [ ] **Step 1: Add failing immutable-contract tests**

```python
def test_task_failure_retryability_and_effect_outcome_invariants() -> None:
    failure = TaskFailure(kind="external_effect", message="denied", retryable=False)
    assert failure.retryable is False
    intent = EffectIntent(kind="toy.audit.append", payload={"line": "hello"})
    outcome = TaskOutcome.succeeded(output={"ok": True}, effects=(intent,))
    assert outcome.effects == (intent,)
    with pytest.raises(ValueError, match="effects are allowed only"):
        TaskOutcome(status="failed", failure=failure, effects=(intent,))


def test_plugin_contribution_must_match_descriptor_ids() -> None:
    provider = _Provider(
        descriptor=PluginDescriptor(
            plugin_id="toy.runtime",
            plugin_version="1.0.0",
            engine_api=">=0.2,<0.3",
            dependencies=(),
            task_handlers=("toy.runtime.run",),
            commit_validators=(),
            schemas=(),
            resources=(),
            effects=(),
            bindings=(),
        ),
        contribution=PluginContribution.empty(),
    )
    with pytest.raises(PluginContractError, match="task handler declarations disagree"):
        validate_contribution(provider.descriptor(), provider.contribute(RegistryPorts("0.2")))
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `uv run pytest packages/graph-engine/tests/composition/test_plugin_contracts.py -v`

Expected: collection fails because the Phase 2 contract types do not exist.

- [ ] **Step 3: Implement the closed SPI models and protocols**

Implement these exact public shapes in `plugin_api.py`:

```python
FailureKind = Literal[
    "transient", "timeout", "invalid_input", "invalid_output", "external_effect", "internal"
]


class TaskFailure(FrozenModel):
    kind: FailureKind
    message: str
    retryable: bool = True


class EffectIntent(FrozenModel):
    kind: str
    payload: JSONValue


class TaskOutcome(FrozenModel):
    status: TaskStatus
    output: JSONValue = None
    failure: TaskFailure | None = None
    stop_reason: str | None = None
    effects: tuple[EffectIntent, ...] = ()


class EffectPolicy(FrozenModel):
    max_attempts: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0)
    backoff_seconds: float = Field(ge=0)


class EffectApplyResult(FrozenModel):
    status: Literal["applied", "transient", "permanent"]
    receipt: JSONValue = None
    failure: TaskFailure | None = None

    @classmethod
    def applied(cls, receipt: JSONValue) -> EffectApplyResult:
        return cls(status="applied", receipt=receipt)


class EffectReconcileResult(FrozenModel):
    status: Literal["not_applied", "pending", "applied", "permanently_failed"]
    receipt: JSONValue = None
    failure: TaskFailure | None = None

    @classmethod
    def applied(cls, receipt: JSONValue) -> EffectReconcileResult:
        return cls(status="applied", receipt=receipt)


class DurableEffectHandler(Protocol):
    async def apply(self, intent: EffectIntent, idempotency_key: str) -> EffectApplyResult: ...
    async def reconcile(self, intent: EffectIntent, idempotency_key: str) -> EffectReconcileResult: ...
```

Use frozen Pydantic models for JSON-bearing values and frozen dataclasses plus `MappingProxyType` for implementation mappings. `TaskOutcome.succeeded(output=None, *, effects=())` freezes the ordered intent tuple; `TaskOutcome.failed(kind, message, *, retryable=True)` constructs the typed failure. `TaskContext.effect(kind, payload)` is a pure value constructor and never invokes a handler. Validate the mutually exclusive result fields for every apply/reconcile status.

Add `packaging>=24` and validate all versions/specifiers with `Version` and `SpecifierSet`. New `PluginDescriptor` dependency/schema/resource/effect/binding tuples default to `()` so the additive task preserves Phase 1 callers. `validate_contribution()` compares each descriptor tuple with contribution IDs and rejects duplicate/cross-kind IDs. Do not change handler invocation or provider binding in this task.

- [ ] **Step 4: Run focused GREEN and static checks**

Run:

```bash
uv run pytest packages/graph-engine/tests/composition/test_plugin_contracts.py -v
uv run ruff check packages/graph-engine/graph_engine/plugin_api.py packages/graph-engine/tests/composition/test_plugin_contracts.py
uv run pyright packages/graph-engine/graph_engine/plugin_api.py
```

Expected: all pass.

- [ ] **Step 5: Run the complete existing graph-engine regression gate**

Run: `uv run pytest packages/graph-engine/tests/test_plugin_registry.py packages/graph-engine/tests/test_product_resolution.py -q`

Expected: all graph-engine tests pass; the new value contracts are additive and do not break Phase 1 execution.

- [ ] **Step 6: Commit the SPI contract**

```bash
git add packages/graph-engine/graph_engine/plugin_api.py packages/graph-engine/tests/composition/test_plugin_contracts.py packages/graph-engine/pyproject.toml pyproject.toml uv.lock
git commit -m "feat(graph-engine): define phase two plugin contracts"
```

### Task 2: Build authenticated declared-tree snapshot primitives

**Files:**
- Create: `packages/graph-engine/graph_engine/composition/__init__.py`
- Create: `packages/graph-engine/graph_engine/composition/models.py`
- Create: `packages/graph-engine/graph_engine/composition/source_fs.py`
- Create: `packages/graph-engine/tests/composition/test_source_fs.py`

**Interfaces:**
- Produces: `SourceKind`, `SourceIdentity`, `SourceFile`, `SourceSnapshot`, `capture_declared_tree(root, files, policy) -> SourceSnapshot`, and `DeclaredTreePolicy`.
- Consumes: canonical hashing from `graph_engine.canonical`.
- Consumers: Tasks 3, 4, 7, and 8.

- [ ] **Step 1: Add deterministic snapshot and mutation RED tests**

```python
def test_declared_tree_digest_is_path_order_independent(tmp_path: Path) -> None:
    (tmp_path / "plugin.yaml").write_text("plugin_id: toy.flow\n", encoding="utf-8")
    (tmp_path / "workflow.yaml").write_text("name: flow\n", encoding="utf-8")
    first = capture_declared_tree(
        tmp_path,
        ("workflow.yaml", "plugin.yaml"),
        DeclaredTreePolicy.config_tree(),
    )
    second = capture_declared_tree(
        tmp_path,
        ("plugin.yaml", "workflow.yaml"),
        DeclaredTreePolicy.config_tree(),
    )
    assert first.digest == second.digest
    assert tuple(item.path for item in first.files) == ("plugin.yaml", "workflow.yaml")


@pytest.mark.parametrize("bad_path", ("../escape", "/absolute", "a/../b", "a\\b"))
def test_declared_tree_rejects_unsafe_paths(tmp_path: Path, bad_path: str) -> None:
    with pytest.raises(SourceSnapshotError):
        capture_declared_tree(tmp_path, (bad_path,), DeclaredTreePolicy.config_tree())
```

Add deterministic fault seams around enumeration, component open, byte read, rescan, and final stat. Tests must replace a file, add/remove a name, swap a path to a symlink, mutate bytes after the first stat, and retain the original exception when cleanup also fails.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `uv run pytest packages/graph-engine/tests/composition/test_source_fs.py -v`

Expected: import fails because `composition.source_fs` does not exist.

- [ ] **Step 3: Implement descriptor-relative immutable snapshots**

Implement `capture_declared_tree()` with this order:

```python
def capture_declared_tree(
    root: Path,
    files: tuple[str, ...],
    policy: DeclaredTreePolicy,
) -> SourceSnapshot:
    root_fd = _open_physical_directory(root)
    try:
        normalized = _validate_closed_file_list(files)
        before_names = _enumerate_regular_tree(root_fd, policy)
        if before_names != set(normalized):
            raise SourceSnapshotError("declared source file set does not match the physical tree")
        captured = tuple(_read_stable_file_at(root_fd, path, policy) for path in normalized)
        after_names = _enumerate_regular_tree(root_fd, policy)
        if after_names != before_names:
            raise SourceSnapshotError("source tree changed while it was captured")
        return SourceSnapshot.from_files(policy.kind, root.resolve(), captured)
    finally:
        os.close(root_fd)
```

Open every component with `dir_fd` plus `O_NOFOLLOW`, require regular files, reject `st_nlink != 1` for editable/config policies, compare device/inode/size/mtime before and after reading, and canonical-hash `[{"path", "sha256"}]`. Never return mutable bytearrays or a partially built snapshot.

- [ ] **Step 4: Run RED/GREEN fault matrix**

Run:

```bash
uv run pytest packages/graph-engine/tests/composition/test_source_fs.py -v
uv run ruff check packages/graph-engine/graph_engine/composition packages/graph-engine/tests/composition/test_source_fs.py
uv run pyright packages/graph-engine/graph_engine/composition
```

Expected: all path, symlink, name-set, byte-swap, hard-link, and cleanup-primary-error tests pass.

- [ ] **Step 5: Commit snapshot primitives**

```bash
git add packages/graph-engine/graph_engine/composition packages/graph-engine/tests/composition/test_source_fs.py
git commit -m "feat(graph-engine): snapshot explicit plugin sources"
```

### Task 3: Resolve and hash installed and editable wheel sources

**Files:**
- Create: `packages/graph-engine/graph_engine/composition/sources.py`
- Create: `packages/graph-engine/tests/composition/test_wheel_sources.py`
- Modify: `packages/graph-engine/graph_engine/composition/models.py`
- Modify: `packages/graph-engine/graph_engine/composition/__init__.py`

**Interfaces:**
- Produces: `WheelProductSource`, `WheelPluginSource`, `EditableWheelPluginSource`, `snapshot_wheel_source()`, and `load_snapshotted_entrypoint()`.
- Consumes: `SourceSnapshot`, `capture_declared_tree()`, `ProductProvider`, and `PluginProvider`.
- Consumers: Task 7.

- [ ] **Step 1: Add fake-distribution RED tests**

Create an installed distribution fixture containing `METADATA`, `entry_points.txt`, `RECORD`, and `toy_plugin/__init__.py`. Test exact distribution/group/name selection, actual-byte hashing, `RECORD` mismatch, duplicate matching entry points, foreign distribution entry points, missing files, and editable closed-file capture.

```python
def test_wheel_snapshot_binds_distribution_and_entrypoint(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    distribution = _installed_distribution(tmp_path, name="toy-runtime", version="1.2.3")
    monkeypatch.setattr(metadata, "distribution", lambda name: distribution)
    source = WheelPluginSource(
        distribution="toy-runtime",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="toy.runtime",
    )
    snapshot = snapshot_wheel_source(source)
    assert snapshot.identity.version == "1.2.3"
    assert snapshot.identity.entrypoint_name == "toy.runtime"
    assert snapshot.digest == snapshot_wheel_source(source).digest
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `uv run pytest packages/graph-engine/tests/composition/test_wheel_sources.py -v`

Expected: imports fail because wheel source adapters do not exist.

- [ ] **Step 3: Implement wheel source adapters**

For normal wheels, read the selected distribution's `RECORD`, validate listed hashes when present, capture actual bytes for every listed source/metadata file, and include normalized distribution name/version/group/name in the aggregate digest. Reject ambiguous or missing entry points before `.load()`.

For editable wheels, require:

```python
class EditableWheelPluginSource(FrozenModel):
    kind: Literal["editable_plugin"] = "editable_plugin"
    distribution: str
    entrypoint_group: Literal["graph_engine.plugins"] = "graph_engine.plugins"
    entrypoint_name: str
    source_root: Path
    source_files: tuple[str, ...]
```

Capture its exact file tuple with `DeclaredTreePolicy.editable()`. Load the provider only after the snapshot completes, then verify `descriptor().plugin_id` and version against the source identity.

- [ ] **Step 4: Verify source mutation and entry-point loading behavior**

Run:

```bash
uv run pytest packages/graph-engine/tests/composition/test_wheel_sources.py -v
uv run pytest packages/graph-engine/tests/composition/test_source_fs.py -q
uv run ruff check packages/graph-engine/graph_engine/composition packages/graph-engine/tests/composition
uv run pyright packages/graph-engine/graph_engine/composition
```

Expected: all pass; changing one installed/editable byte changes the digest, and provider code is never loaded on a failed snapshot.

- [ ] **Step 5: Commit wheel resolution**

```bash
git add packages/graph-engine/graph_engine/composition packages/graph-engine/tests/composition/test_wheel_sources.py
git commit -m "feat(graph-engine): resolve explicit wheel plugin sources"
```

### Task 4: Parse strict declarative product and plugin sources

**Files:**
- Create: `packages/graph-engine/graph_engine/composition/declarative.py`
- Create: `packages/graph-engine/tests/composition/test_declarative_sources.py`
- Modify: `packages/graph-engine/graph_engine/composition/models.py`
- Modify: `packages/graph-engine/graph_engine/composition/__init__.py`

**Interfaces:**
- Produces: `ProductFileSource`, `ConfigTreePluginSource`, `DeclarativeProduct`, `DeclarativePlugin`, `load_product_file()`, and `load_config_tree()`.
- Consumes: Task 1 contribution models and Task 2 snapshots.
- Consumers: Tasks 6, 7, and 13.

- [ ] **Step 1: Add strict data-only RED tests**

```python
@pytest.mark.parametrize(
    "payload",
    (
        {"python": "pkg.module:function"},
        {"command": ["sh", "-c", "echo bad"]},
        {"shell": "echo bad"},
        {"template": "{{ __import__('os') }}"},
    ),
)
def test_config_plugin_rejects_executable_forms(tmp_path: Path, payload: dict[str, object]) -> None:
    _write_plugin_tree(tmp_path, extra=payload)
    with pytest.raises(DeclarativePluginRejected):
        load_config_tree(ConfigTreePluginSource(path=tmp_path))


def test_config_plugin_binds_data_to_selected_wheel_capability(tmp_path: Path) -> None:
    _write_plugin_tree(
        tmp_path,
        bindings={
            "toy.flow.greet": {
                "target": "toy.runtime.execute",
                "data": {"skill": "toy.flow.greeting-skill"},
            }
        },
    )
    loaded = load_config_tree(ConfigTreePluginSource(path=tmp_path))
    assert loaded.contribution.bindings[0].target_capability_id == "toy.runtime.execute"
```

Also test unknown keys, undeclared/extra files, unsafe media types, executable POSIX mode, symlink, resource digest changes, invalid qualified IDs, and product-relative config paths.

- [ ] **Step 2: Run tests and confirm RED**

Run: `uv run pytest packages/graph-engine/tests/composition/test_declarative_sources.py -v`

Expected: imports fail because declarative source models/loaders do not exist.

- [ ] **Step 3: Implement closed Pydantic YAML models and loaders**

Define strict models with `extra="forbid"`:

```python
class DeclarativeBinding(FrozenModel):
    capability_id: str
    target_capability_id: str
    data: JSONValue = None
    resource_ids: tuple[str, ...] = ()


class DeclarativePluginDocument(FrozenModel):
    schema_version: Literal["1"]
    plugin_id: str
    plugin_version: str
    engine_api: str
    dependencies: tuple[PluginDependency, ...] = ()
    files: tuple[DeclaredResourceFile, ...]
    bindings: tuple[DeclarativeBinding, ...] = ()
```

Parse YAML with `yaml.safe_load`, immediately validate into closed models, require `plugin.yaml` plus exactly the declared resource files, enforce the safe media-type table, and build frozen Schema/Resource/Binding contributions from snapshot bytes. A product file produces a strict manifest with explicit plugin requirements, entrypoints, configuration, inline workflow or one workflow resource ID, and manifest-relative config paths.

- [ ] **Step 4: Run focused GREEN and snapshot regressions**

Run:

```bash
uv run pytest packages/graph-engine/tests/composition/test_declarative_sources.py -v
uv run pytest packages/graph-engine/tests/composition/test_source_fs.py -q
uv run ruff check packages/graph-engine/graph_engine/composition packages/graph-engine/tests/composition
uv run pyright packages/graph-engine/graph_engine/composition
```

Expected: all pass.

- [ ] **Step 5: Commit declarative loading**

```bash
git add packages/graph-engine/graph_engine/composition packages/graph-engine/tests/composition/test_declarative_sources.py
git commit -m "feat(graph-engine): load data-only plugin trees"
```

### Task 5: Validate exact dependency closure and canonical topology

**Files:**
- Create: `packages/graph-engine/graph_engine/composition/dependencies.py`
- Create: `packages/graph-engine/tests/composition/test_dependencies.py`
- Modify: `packages/graph-engine/graph_engine/composition/__init__.py`

**Interfaces:**
- Produces: `DependencyConflict` and `resolve_dependency_order(descriptors, required_plugin_ids) -> tuple[str, ...]`.
- Consumes: `PluginDescriptor` and `PluginDependency` from Task 1.
- Consumers: Task 7.

- [ ] **Step 1: Add exact-version/no-selection RED tests**

```python
def test_resolver_validates_selected_versions_without_choosing() -> None:
    descriptors = {
        "toy.flow": _descriptor("toy.flow", "2.0.0", requires=(("toy.runtime", ">=1,<2"),)),
        "toy.runtime": _descriptor("toy.runtime", "1.4.0"),
    }
    assert resolve_dependency_order(descriptors, ("toy.flow", "toy.runtime")) == (
        "toy.runtime",
        "toy.flow",
    )


def test_resolver_rejects_incompatible_explicit_version() -> None:
    descriptors = {
        "toy.flow": _descriptor("toy.flow", "2.0.0", requires=(("toy.runtime", ">=1,<2"),)),
        "toy.runtime": _descriptor("toy.runtime", "2.1.0"),
    }
    with pytest.raises(DependencyConflict, match="toy.runtime==2.1.0"):
        resolve_dependency_order(descriptors, tuple(descriptors))
```

Add missing source, extra unreferenced source, self-cycle, multi-node cycle, invalid PEP 440, duplicate dependency, engine API mismatch, and input-order permutation tests.

- [ ] **Step 2: Run tests and confirm RED**

Run: `uv run pytest packages/graph-engine/tests/composition/test_dependencies.py -v`

Expected: import fails because the dependency module does not exist.

- [ ] **Step 3: Implement deterministic validation-only resolution**

Normalize versions with `Version`, constraints with `SpecifierSet`, reject any selected ID outside the exact closure, and use Kahn topological sorting with qualified plugin ID as the ready-queue tie-breaker. Cycle errors must name the remaining sorted IDs. The function accepts one descriptor per explicit source and never accepts a candidate list.

```python
def resolve_dependency_order(
    descriptors: Mapping[str, PluginDescriptor],
    required_plugin_ids: tuple[str, ...],
) -> tuple[str, ...]:
    selected = _validate_exact_source_set(descriptors, required_plugin_ids)
    _validate_selected_versions(selected)
    return _canonical_topological_order(selected)
```

- [ ] **Step 4: Run permutation property checks and static gates**

Run:

```bash
uv run pytest packages/graph-engine/tests/composition/test_dependencies.py -v
uv run ruff check packages/graph-engine/graph_engine/composition/dependencies.py packages/graph-engine/tests/composition/test_dependencies.py
uv run pyright packages/graph-engine/graph_engine/composition/dependencies.py
```

Expected: all pass; every permutation yields identical topology/error text.

- [ ] **Step 5: Commit dependency validation**

```bash
git add packages/graph-engine/graph_engine/composition/dependencies.py packages/graph-engine/graph_engine/composition/__init__.py packages/graph-engine/tests/composition/test_dependencies.py
git commit -m "feat(graph-engine): validate exact plugin dependencies"
```

### Task 6: Construct the five fixed immutable registries

**Files:**
- Create: `packages/graph-engine/graph_engine/composition/registries.py`
- Create: `packages/graph-engine/tests/composition/test_registries.py`
- Modify: `packages/graph-engine/graph_engine/composition/models.py`
- Modify: `packages/graph-engine/graph_engine/composition/__init__.py`
- Modify: `packages/graph-engine/graph_engine/plugin_api.py`
- Modify: `packages/graph-engine/graph_engine/product.py:71-180`
- Modify: `packages/graph-engine/graph_engine/runtime/scheduler.py:51-260`
- Modify: `examples/graph-engine-toy-a/graph_engine_toy_a/plugin.py`
- Modify: `examples/graph-engine-toy-b/graph_engine_toy_b/plugin.py`
- Modify: `packages/graph-engine/tests/runtime/test_scheduler.py`
- Modify: `packages/graph-engine/tests/runtime/test_engine.py`
- Modify: `packages/graph-engine/tests/integration/test_toy_a.py`
- Modify: `packages/graph-engine/tests/integration/test_toy_b.py`
- Delete after call-site migration in this task: `packages/graph-engine/tests/test_plugin_registry.py`

**Interfaces:**
- Produces: `SourceRegistry`, `CapabilityRegistry`, `SchemaRegistry`, `ResourceRegistry`, `EffectRegistry`, `RegistrySet`, and `build_registries(sources, contributions, dependency_order) -> RegistrySet`.
- Consumes: Tasks 1-5 models and contributions.
- Consumers: Tasks 7, 9, 10, 11, and 13.

- [ ] **Step 1: Add collision, alias, and immutability RED tests**

```python
def test_registry_builder_freezes_all_five_views() -> None:
    registries = build_registries(
        sources=(_source("toy.runtime"),),
        contributions=(_runtime_contribution(),),
        dependency_order=("toy.runtime",),
    )
    assert tuple(registries.capabilities.task_handlers) == ("toy.runtime.execute",)
    with pytest.raises(TypeError):
        registries.capabilities.task_handlers["toy.other"] = _handler()  # type: ignore[index]


def test_binding_must_be_owned_and_target_selected_handler() -> None:
    with pytest.raises(RegistryConflict, match="unknown target capability"):
        build_registries(
            sources=(_source("toy.flow"),),
            contributions=(_binding_contribution("toy.flow.run", "missing.execute"),),
            dependency_order=("toy.flow",),
        )
```

Add duplicate IDs within/across kinds, descriptor/contribution mismatch, alias owner mismatch, alias cycle, schema/resource dangling references, effect schema mismatch, canonical ordering, and attempted sixth-kind contribution tests.

- [ ] **Step 2: Run tests and confirm RED**

Run: `uv run pytest packages/graph-engine/tests/composition/test_registries.py -v`

Expected: imports fail because fixed registry builders do not exist.

- [ ] **Step 3: Implement typed builders and immutable views**

Use separate entry models and one `RegistrySet`:

```python
@dataclass(frozen=True, slots=True)
class RegistrySet:
    sources: SourceRegistry
    capabilities: CapabilityRegistry
    schemas: SchemaRegistry
    resources: ResourceRegistry
    effects: EffectRegistry


def build_registries(
    sources: tuple[SourceSnapshot, ...],
    contributions: tuple[PluginContribution, ...],
    dependency_order: tuple[str, ...],
) -> RegistrySet:
    source_view = _build_source_registry(sources, dependency_order)
    schema_view = _build_schema_registry(contributions, dependency_order)
    resource_view = _build_resource_registry(contributions, dependency_order)
    capability_view = _build_capability_registry(contributions, dependency_order)
    effect_view = _build_effect_registry(contributions, schema_view, dependency_order)
    _validate_bindings(capability_view, resource_view)
    return RegistrySet(source_view, capability_view, schema_view, resource_view, effect_view)
```

Every view copies and sorts inputs before wrapping mappings with `MappingProxyType`. Config bindings create generic immutable bound-handler adapters; they preserve alias and target IDs plus frozen binding data in `TaskRequest`.

- [ ] **Step 4: Remove the mutable Phase 1 assembler**

Replace `PluginProvider.bind()` with `contribute()` and callable `TaskHandler` with the `execute()` method defined by the approved SPI. Delete `PluginRuntime` and the Phase 1 `CapabilityRegistry` implementation from `plugin_api.py`. Migrate Toy providers, scheduler host/test hosts, runtime fixtures, and product-resolution fixtures in this same task. `product.py` may retain one private `_assemble_selected_contributions()` bridge that returns the new immutable capability view for the still-Phase-1 `ResolvedProduct`; it is not exported and Task 8 deletes the complete old product resolver. Replace the retired `test_plugin_registry.py` with the composition contract/registry suites; do not leave public import aliases.

The final public protocols introduced by this cut are exactly:

```python
class TaskHandler(Protocol):
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome: ...


class PluginProvider(Protocol):
    def descriptor(self) -> PluginDescriptor: ...
    def contribute(self, ports: RegistryPorts) -> PluginContribution: ...
```

`TaskExecutionHost.execute()` continues to receive a `TaskHandler`; every production/test host invokes `handler.execute(request, TaskContext(...))` inside its confinement seam. No callable fallback remains after this task.

- [ ] **Step 5: Run focused and compiler regressions**

Run:

```bash
uv run pytest packages/graph-engine/tests/composition/test_plugin_contracts.py packages/graph-engine/tests/composition/test_registries.py -v
uv run pytest packages/graph-engine/tests/graph/test_schema_and_compiler.py -q
uv run pytest packages/graph-engine/tests -q
uv run ruff check packages/graph-engine/graph_engine/plugin_api.py packages/graph-engine/graph_engine/composition packages/graph-engine/tests/composition
uv run pyright packages/graph-engine/graph_engine/plugin_api.py packages/graph-engine/graph_engine/composition
```

Expected: all pass.

- [ ] **Step 6: Commit fixed registries**

```bash
git add packages/graph-engine/graph_engine/plugin_api.py packages/graph-engine/graph_engine/product.py packages/graph-engine/graph_engine/runtime/scheduler.py packages/graph-engine/graph_engine/composition packages/graph-engine/tests examples/graph-engine-toy-a/graph_engine_toy_a/plugin.py examples/graph-engine-toy-b/graph_engine_toy_b/plugin.py
git commit -m "feat(graph-engine): build fixed typed plugin registries"
```

### Task 7: Resolve a complete FrozenComposition and canonical InvocationLock

**Files:**
- Create: `packages/graph-engine/graph_engine/composition/lock.py`
- Create: `packages/graph-engine/graph_engine/composition/resolver.py`
- Create: `packages/graph-engine/tests/composition/test_registry_platform.py`
- Create: `packages/graph-engine/tests/composition/test_lock_model.py`
- Modify: `packages/graph-engine/graph_engine/composition/models.py`
- Modify: `packages/graph-engine/graph_engine/composition/__init__.py`
- Modify: `packages/graph-engine/graph_engine/graph/compiler.py:174-430`
- Modify: `packages/graph-engine/graph_engine/graph/schema.py:1-135`

**Interfaces:**
- Produces: `ResolutionRequest`, `ProductManifest`, `PluginRequirement`, `InvocationLock`, `FrozenComposition`, `RegistryPlatform.resolve()`.
- Consumes: Tasks 1-6 plus `compile_workflow()`.
- Consumers: Tasks 8-14.

- [ ] **Step 1: Add wheel-only and mixed-source resolution RED tests**

```python
def test_registry_platform_resolves_one_frozen_composition() -> None:
    request = _wheel_only_request()
    composition = RegistryPlatform(metadata_provider=_fake_metadata()).resolve(request)
    assert composition.manifest.product_id == "toy.a"
    assert composition.workflow.entrypoints == {"hello": "root"}
    assert composition.lock.digest == composition.lock_digest
    assert composition.registries.capabilities.task_handlers["toy.a.greet"]


def test_resolution_is_identical_for_permuted_explicit_sources() -> None:
    first = RegistryPlatform(metadata_provider=_fake_metadata()).resolve(_mixed_request(order="flow-first"))
    second = RegistryPlatform(metadata_provider=_fake_metadata()).resolve(_mixed_request(order="runtime-first"))
    assert first.lock.canonical_bytes == second.lock.canonical_bytes
    assert first.digest == second.digest
```

Add missing/extra source, drifted provider descriptor, unknown graph capability, unknown schema/resource/effect, invalid product config, inline-vs-resource workflow exclusivity, and product entrypoint closure tests.

- [ ] **Step 2: Run tests and confirm RED**

Run:

```bash
uv run pytest packages/graph-engine/tests/composition/test_registry_platform.py packages/graph-engine/tests/composition/test_lock_model.py -v
```

Expected: imports fail because the resolver and lock do not exist.

- [ ] **Step 3: Implement canonical lock construction**

Define strict frozen lock models and serialize only through `canonical_json_bytes()`:

```python
class InvocationLock(FrozenModel):
    schema_version: Literal["1"] = "1"
    digest_algorithm: Literal["graph-engine-source-v1"] = "graph-engine-source-v1"
    engine_api: str
    engine_digest: str
    product: LockedProduct
    plugins: tuple[LockedPlugin, ...]
    dependency_order: tuple[str, ...]
    registry_digests: RegistryDigests
    configuration_digest: str
    compiled_workflow_digest: str
    canonical_bytes: bytes = Field(exclude=True, repr=False)
    digest: str
```

Construct bytes from a JSON projection that excludes `canonical_bytes` and `digest`, then set `digest = sha256(canonical_bytes)`. Golden tests pin Unicode, float rejection, map order, tuple/list projection, and source-order independence.

- [ ] **Step 4: Implement RegistryPlatform orchestration**

Implement the exact 15-step order from the design: parse product, capture all sources, validate exact source set, dependency order, load snapshotted providers, validate contributions, parse config contributions, build registries, validate aliases/references/config, compile graphs, compute digests, construct lock, return composition.

```python
class RegistryPlatform:
    def resolve(self, request: ResolutionRequest) -> FrozenComposition:
        product = self._load_product(request.product)
        snapshots = self._snapshot_sources(request, product)
        descriptors, contributions = self._load_contributions(snapshots)
        order = resolve_dependency_order(descriptors, product.required_plugin_ids)
        registries = build_registries(snapshots, contributions, order)
        workflow = self._compile_product_workflow(product, registries)
        lock = build_invocation_lock(product, snapshots, order, registries, workflow)
        return FrozenComposition.freeze(product, registries, workflow, lock)
```

- [ ] **Step 5: Run composition, graph, and canonical tests**

Run:

```bash
uv run pytest packages/graph-engine/tests/composition -q
uv run pytest packages/graph-engine/tests/graph -q
uv run ruff check packages/graph-engine/graph_engine/composition packages/graph-engine/graph_engine/graph packages/graph-engine/tests/composition
uv run pyright packages/graph-engine/graph_engine/composition packages/graph-engine/graph_engine/graph
```

Expected: all pass.

- [ ] **Step 6: Commit frozen composition**

```bash
git add packages/graph-engine/graph_engine/composition packages/graph-engine/graph_engine/graph packages/graph-engine/tests/composition
git commit -m "feat(graph-engine): freeze resolved plugin compositions"
```

### Task 8: Bind Engine start/open to an atomically persisted InvocationLock

**Files:**
- Create: `packages/graph-engine/graph_engine/runtime/invocation_lock.py`
- Create: `packages/graph-engine/tests/runtime/test_invocation_lock.py`
- Modify: `packages/graph-engine/graph_engine/runtime/events.py:22-310`
- Modify: `packages/graph-engine/graph_engine/runtime/models.py:43-951`
- Modify: `packages/graph-engine/graph_engine/runtime/engine.py:72-570`
- Modify: `packages/graph-engine/graph_engine/runtime/checkpoint.py:1-218`
- Modify: `packages/graph-engine/graph_engine/runtime/planner.py:232-380`
- Modify: `packages/graph-engine/graph_engine/__init__.py:1-80`
- Modify: `packages/graph-engine/graph_engine/__main__.py:1-215`
- Modify: `packages/graph-engine/graph_engine/runtime/__init__.py:1-136`
- Modify: `examples/graph-engine-toy-a/graph_engine_toy_a/product.py`
- Modify: `examples/graph-engine-toy-b/graph_engine_toy_b/product.py`
- Delete after all imports move: `packages/graph-engine/graph_engine/product.py`
- Modify: `packages/graph-engine/tests/test_cli.py`
- Modify: `packages/graph-engine/tests/integration/test_toy_a.py`
- Modify: `packages/graph-engine/tests/integration/test_toy_b.py`
- Modify: `packages/graph-engine/tests/runtime/test_engine.py`
- Modify: `packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py`
- Modify: `packages/graph-engine/tests/runtime/test_planner.py`
- Delete after coverage moves: `packages/graph-engine/tests/test_product_resolution.py`

**Interfaces:**
- Produces: `install_invocation_lock_at()`, `read_invocation_lock_at()`, `authenticate_invocation_lock()`, `InvocationDrift`, lock-bound `Engine.start(FrozenComposition, ...)`, and `Engine.open(invocation_id, FrozenComposition)`.
- Consumes: Task 7 `FrozenComposition` and canonical lock bytes.
- Consumers: Tasks 9-14.

- [ ] **Step 1: Add lock lifecycle and drift RED tests**

```python
def test_engine_persists_lock_before_bootstrap(tmp_path: Path) -> None:
    composition = _composition()
    with Engine(tmp_path) as engine:
        with engine.start(composition, entrypoint="hello", invocation_id="run-1") as handle:
            assert handle.lock_digest == composition.lock.digest
    lock_path = tmp_path / "invocations" / "run-1" / "invocation.lock.json"
    assert lock_path.read_bytes() == composition.lock.canonical_bytes
    first = Ledger(tmp_path / "invocations" / "run-1" / "ledger").read_all()[0].event
    assert isinstance(first, InvocationStarted)
    assert first.lock_digest == composition.lock.digest


def test_engine_open_rejects_each_lock_drift_without_append(tmp_path: Path) -> None:
    original = _composition()
    drifted = _composition(resource_bytes=b"changed")
    _start_and_close(tmp_path, original)
    before = _ledger_bytes(tmp_path, "run-1")
    with Engine(tmp_path) as engine, pytest.raises(InvocationDrift):
        engine.open("run-1", drifted)
    assert _ledger_bytes(tmp_path, "run-1") == before
```

Add fault cuts before/after lock file fsync, directory fsync, invocation rename, and ledger bootstrap; exact repeated-start initialization recovery; corrupt/missing lock with ledger; lock/ledger digest mismatch; descriptor leak/error masking tests.

- [ ] **Step 2: Run tests and confirm RED**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_invocation_lock.py -v`

Expected: tests fail because no lock file is installed and `InvocationStarted` still stores `product_digest`.

- [ ] **Step 3: Implement atomic immutable lock storage**

Use invocation-directory descriptor-relative create/write/fsync/rename/root-fsync operations. Require a single regular, non-symlinked, link-count-one `invocation.lock.json`. Existing lock bytes are immutable: exact equality is accepted only for idempotent initialization; inequality raises `InvocationDrift`.

```python
def authenticate_invocation_lock(
    invocation_fd: int,
    expected: InvocationLock,
) -> None:
    actual = read_invocation_lock_at(invocation_fd)
    if actual != expected.canonical_bytes:
        raise InvocationDrift("invocation lock differs from the resolved composition")
```

- [ ] **Step 4: Cut Engine and events to FrozenComposition/lock_digest**

Change `InvocationHandle._product` to `_composition`, `product_digest` to `lock_digest`, and all workflow/registry accesses to `composition.workflow`/`composition.registries`. Replace `InvocationStarted.product_digest` with `lock_digest`; update fold and history validation. Start installs the lock before ledger creation. Open authenticates lock bytes and bootstrap digest before acquiring a runner claim or reclaiming work.

Migrate the module CLI, Toy product providers, integration fixtures, and every runtime test factory to construct a `ResolutionRequest`, call `RegistryPlatform.resolve()`, and pass `FrozenComposition` to Engine. Preserve the current explicit Phase 1 CLI flags only as renamed exact distribution/entry-point options; Task 13 adds the mixed config-tree commands and documentation. Delete `product.py`, `ResolvedProduct`, and Phase 1 product-resolution tests only after all call sites are updated. Do not create re-export aliases.

- [ ] **Step 5: Run runtime lock and complete existing runtime regressions**

Run:

```bash
uv run pytest packages/graph-engine/tests/runtime/test_invocation_lock.py -v
uv run pytest packages/graph-engine/tests/runtime -q
uv run pytest packages/graph-engine/tests/composition -q
uv run pytest packages/graph-engine/tests/test_cli.py packages/graph-engine/tests/integration -q
uv run pytest packages/graph-engine/tests -q
uv run ruff check packages/graph-engine/graph_engine packages/graph-engine/tests
uv run pyright packages/graph-engine/graph_engine
```

Expected: all pass with `lock_digest`; no `ResolvedProduct`, `PluginRuntime`, `.bind(`, or runtime `product_digest` reference remains.

- [ ] **Step 6: Commit the lock-bound facade**

```bash
git add packages/graph-engine/graph_engine packages/graph-engine/tests examples/graph-engine-toy-a/graph_engine_toy_a/product.py examples/graph-engine-toy-b/graph_engine_toy_b/product.py
git commit -m "feat(graph-engine): pin invocation compositions before bootstrap"
```

### Task 9: Add durable-effect events and pure projection semantics

**Files:**
- Modify: `packages/graph-engine/graph_engine/runtime/events.py:22-310`
- Modify: `packages/graph-engine/graph_engine/runtime/models.py:43-951`
- Modify: `packages/graph-engine/graph_engine/runtime/planner.py:232-1658`
- Modify: `packages/graph-engine/graph_engine/runtime/checkpoint.py:1-218`
- Modify: `packages/graph-engine/graph_engine/runtime/__init__.py:1-136`
- Modify: `packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py`
- Modify: `packages/graph-engine/tests/runtime/test_planner.py`

**Interfaces:**
- Produces events `TaskCommitPrepared`, `EffectIntentCommitted`, `EffectApplyStarted`, `EffectReceiptRecorded`; projections `PreparedTaskCommit` and `EffectRecord`; attempt status `effect_pending`; non-retryable planner behavior.
- Consumes: Task 1 effect contracts and Task 8 lock-bound bootstrap.
- Consumers: Tasks 10-12.

- [ ] **Step 1: Add fold RED tests for the complete legal effect history**

```python
def test_fold_keeps_task_pending_until_all_effect_receipts() -> None:
    envelopes = _envelopes(
        *_running_task_prefix(),
        TaskCommitPrepared(
            task_id="task-1",
            activation_id="a1",
            attempt=1,
            output={"ok": True},
            previous_tree_id=_EMPTY,
            tree_id=_TREE,
            effect_ids=("effect-1", "effect-2"),
        ),
        HeadAdvanced(task_id="task-1", activation_id="a1", attempt=1, previous_tree_id=_EMPTY, tree_id=_TREE),
        EffectIntentCommitted(effect_id="effect-1", activation_id="a1", attempt=1, index=0, kind="toy.audit", payload={"n": 1}, idempotency_key=_KEY1),
        EffectIntentCommitted(effect_id="effect-2", activation_id="a1", attempt=1, index=1, kind="toy.audit", payload={"n": 2}, idempotency_key=_KEY2),
    )
    projection = fold_events(envelopes)
    assert projection.activations[-1].attempts[-1].status == "effect_pending"
    assert tuple(effect.status for effect in projection.effects) == ("committed", "committed")
```

Add forged histories: intent without prepared commit, wrong index/order/key, duplicate intent/receipt, apply before prior receipt, receipt without apply, success before all receipts, non-retryable failure followed by retry, HEAD mismatch, and graph terminal while effects remain pending.

- [ ] **Step 2: Run fold/planner tests and confirm RED**

Run:

```bash
uv run pytest packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py -k effect -v
uv run pytest packages/graph-engine/tests/runtime/test_planner.py -k "effect or non_retryable" -v
```

Expected: event models/statuses do not exist.

- [ ] **Step 3: Implement strict events and projection records**

Define exact event payloads with frozen JSON and SHA-256 fields. `TaskCommitPrepared` stores output, previous/current tree IDs, and ordered effect IDs. `EffectIntentCommitted` stores canonical payload and stable key. `EffectApplyStarted` stores monotonically contiguous apply attempt. `EffectReceiptRecorded` stores schema-validated receipt data as frozen JSON.

Extend attempt state:

```python
AttemptStatus = Literal["running", "effect_pending", "succeeded", "failed", "stopped"]


class EffectRecord(ProjectionModel):
    effect_id: str
    task_id: str
    activation_id: str
    task_attempt: int
    index: int
    kind: str
    payload: FrozenJSONValue
    idempotency_key: str
    status: Literal["committed", "applying", "applied", "permanently_failed"]
    apply_attempts: int = 0
    receipt: FrozenJSONValue = None
    failure: TaskFailure | None = None
```

Keep all validation in the single `_advance_fold()` state machine so `fold_events()` and `FoldCursor` remain equivalent.

- [ ] **Step 4: Make planner/history validation effect-aware**

`plan_next()` emits no downstream or task retry while an activation is `effect_pending`. A failure with `retryable=False` never creates another `PlannedTask` even if numeric attempts remain. `validate_event_history()` recognizes the prepared atomic batch, single apply-start events, single receipt/failure events, and final task success as exact external transitions without prefix refolding.

- [ ] **Step 5: Run fold, planner, checkpoint, and history gates**

Run:

```bash
uv run pytest packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py -v
uv run pytest packages/graph-engine/tests/runtime/test_planner.py -v
uv run pytest packages/graph-engine/tests/runtime/test_engine.py -k "history or replay" -q
uv run ruff check packages/graph-engine/graph_engine/runtime packages/graph-engine/tests/runtime
uv run pyright packages/graph-engine/graph_engine/runtime
```

Expected: all pass; adversarial effect histories fail before Engine recovery.

- [ ] **Step 6: Commit effect projection semantics**

```bash
git add packages/graph-engine/graph_engine/runtime packages/graph-engine/tests/runtime
git commit -m "feat(graph-engine): project durable effect histories"
```

### Task 10: Publish prepared workspace commits and effect intents atomically

**Files:**
- Modify: `packages/graph-engine/graph_engine/runtime/scheduler.py:119-697`
- Modify: `packages/graph-engine/graph_engine/runtime/workspace.py:1160-1550`
- Modify: `packages/graph-engine/tests/runtime/test_scheduler.py`
- Modify: `packages/graph-engine/tests/runtime/test_workspace.py`

**Interfaces:**
- Produces: `Scheduler.prepare_success()`, prepared publication batches, `AttemptResult.effect_ids`, and a generalized authenticated HEAD transaction journal.
- Consumes: Task 9 events/projection and Task 6 Effect Registry validation data.
- Consumers: Tasks 11-12.

- [ ] **Step 1: Add prepared-commit RED tests**

```python
def test_effectful_success_publishes_head_and_intents_without_task_success(tmp_path: Path) -> None:
    scheduler, ledger, store = _scheduler(tmp_path, outcome=_effectful_outcome())
    result = asyncio.run(scheduler.run_wave((_planned_task(),)))[0]
    events = tuple(envelope.event for envelope in ledger.read_all())
    assert isinstance(events[-4], TaskCommitPrepared)
    assert isinstance(events[-3], HeadAdvanced)
    assert isinstance(events[-2], EffectIntentCommitted)
    assert isinstance(events[-1], EffectIntentCommitted)
    assert not any(isinstance(event, TaskAttemptSucceeded) for event in events)
    assert result.outcome.effects
    assert store.head_tree_id() == result.head_tree_id


def test_effect_free_success_keeps_one_atomic_publication(tmp_path: Path) -> None:
    scheduler, ledger, _store = _scheduler(tmp_path, outcome=TaskOutcome.succeeded({"ok": True}))
    asyncio.run(scheduler.run_wave((_planned_task(),)))
    tail = tuple(envelope.event.kind for envelope in ledger.read_all()[-3:])
    assert tail == ("task_commit_prepared", "head_advanced", "task_attempt_succeeded")
```

Add intent schema/kind failure before HEAD, stable ID/key derivation, max-order preservation, lease expiry before publication, concurrent reclaim, CAS conflict rollback, ambiguous ledger installed/absent/unreadable outcomes, and no newer-HEAD overwrite tests.

- [ ] **Step 2: Run scheduler/workspace tests and confirm RED**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_scheduler.py packages/graph-engine/tests/runtime/test_workspace.py -k "prepared or effect" -v`

Expected: current scheduler publishes `TaskAttemptSucceeded + HeadAdvanced` and has no prepared path.

- [ ] **Step 3: Validate intents and derive stable identities before commit**

Validate every intent kind against `RegistrySet.effects`, validate payload against its registered schema, and assign:

```python
effect_id = canonical_id(
    "effect",
    task.invocation_id,
    task.activation_id,
    str(task.attempt),
    str(index),
)
idempotency_key = canonical_digest(
    {
        "lock_digest": self._lock_digest,
        "effect_id": effect_id,
        "kind": intent.kind,
        "payload_digest": canonical_digest(intent.payload),
    }
)
```

Any invalid intent becomes `TaskAttemptFailed(kind="invalid_output")` before candidate publication.

- [ ] **Step 4: Generalize candidate finalization to publish the exact prepared batch**

Change the workspace callback from `publish_success(previous, tree)` to `publish_prepared(previous, tree)`. The scheduler builds the complete exact event tuple before acquiring the store commit lock. `SnapshotStore.finalize_candidate()` holds the authenticated store lock through validators, lease recheck, HEAD publish, and ledger CAS exactly as Phase 1, but its journal now records the expected arbitrary prepared event-range digest.

On post-append errors, reread/authenticate the exact range: exact presence completes; proven absence conditionally rolls back only this HEAD; ambiguity preserves HEAD and raises `HeadPublicationIndeterminate`.

- [ ] **Step 5: Run focused race matrix and full scheduler/workspace suites**

Run:

```bash
uv run pytest packages/graph-engine/tests/runtime/test_scheduler.py -v
uv run pytest packages/graph-engine/tests/runtime/test_workspace.py -v
uv run ruff check packages/graph-engine/graph_engine/runtime/scheduler.py packages/graph-engine/graph_engine/runtime/workspace.py packages/graph-engine/tests/runtime
uv run pyright packages/graph-engine/graph_engine/runtime/scheduler.py packages/graph-engine/graph_engine/runtime/workspace.py
```

Expected: all pass.

- [ ] **Step 6: Commit prepared publication**

```bash
git add packages/graph-engine/graph_engine/runtime/scheduler.py packages/graph-engine/graph_engine/runtime/workspace.py packages/graph-engine/tests/runtime/test_scheduler.py packages/graph-engine/tests/runtime/test_workspace.py
git commit -m "feat(graph-engine): commit effect intents before execution"
```

### Task 11: Implement the serial durable-effect executor

**Files:**
- Create: `packages/graph-engine/graph_engine/runtime/effects.py`
- Create: `packages/graph-engine/tests/runtime/test_effects.py`
- Modify: `packages/graph-engine/graph_engine/runtime/engine.py:300-570`
- Modify: `packages/graph-engine/graph_engine/runtime/__init__.py`
- Modify: `packages/graph-engine/graph_engine/plugin_api.py`

**Interfaces:**
- Produces: `EffectExecutor.settle_next(projection) -> EffectSettlement`, canonical pending-effect selection, apply/reconcile dispatch, receipt publication, permanent failure publication.
- Consumes: Task 6 Effect Registry, Task 9 projection, Task 10 prepared commits.
- Consumers: Task 12 and engine integration tests.

- [ ] **Step 1: Add apply/reconcile state-machine RED tests**

```python
def test_executor_applies_committed_effect_and_records_receipt(tmp_path: Path) -> None:
    handler = RecordingEffectHandler(apply_result=EffectApplyResult.applied({"remote_id": "r1"}))
    executor, ledger, projection = _effect_executor(tmp_path, handler=handler, state="committed")
    settlement = asyncio.run(executor.settle_next(projection))
    assert settlement.progressed is True
    assert handler.apply_keys == (_KEY,)
    assert handler.reconcile_keys == ()
    assert tuple(event.event.kind for event in ledger.read_all()[-2:]) == (
        "effect_apply_started",
        "effect_receipt_recorded",
    )


def test_executor_reconciles_after_apply_started_without_blind_reapply(tmp_path: Path) -> None:
    handler = RecordingEffectHandler(reconcile_result=EffectReconcileResult.applied({"remote_id": "r1"}))
    executor, _ledger, projection = _effect_executor(tmp_path, handler=handler, state="applying")
    asyncio.run(executor.settle_next(projection))
    assert handler.apply_keys == ()
    assert handler.reconcile_keys == (_KEY,)
```

Add `not_applied -> apply`, `pending -> no apply`, transient retry/backoff, invalid receipt, handler exception ambiguity, permanent result, exhausted policy, multiple effects serial order, multiple tasks canonical order, and all-receipts-to-task-success tests.

- [ ] **Step 2: Run tests and confirm RED**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_effects.py -v`

Expected: import fails because `runtime.effects` does not exist.

- [ ] **Step 3: Implement canonical selection and handler dispatch**

```python
class EffectExecutor:
    async def settle_next(self, projection: InvocationProjection) -> EffectSettlement:
        effect = _next_effect(projection)
        if effect is None:
            return self._publish_ready_task_success(projection)
        registration = self._effects.require(effect.kind)
        if effect.status == "committed":
            self._append_apply_started(effect)
            return await self._apply(registration, effect)
        if effect.status == "applying":
            return await self._reconcile(registration, effect)
        raise EffectStateError(f"effect {effect.effect_id} is not settleable")
```

Select the lowest canonical `(task_id, task_attempt, effect_index, effect_id)` whose predecessors have receipts. Persist `EffectApplyStarted` before calling `apply`. Validate every receipt through the locked schema registry before publication.

- [ ] **Step 4: Integrate effect settlement ahead of normal planning**

In `Engine._run_until_blocked_with_store()`, after replay/reclaim and before `plan_running_tasks()`/`plan_next()`, call the effect executor when the projection contains pending effects. A progressed settlement restarts the loop from a fresh ledger read. A `pending` reconcile result returns an interrupted/blocked engine result only through an explicit generic effect-pending result, never as success.

- [ ] **Step 5: Run effect, engine, planner, and scheduler suites**

Run:

```bash
uv run pytest packages/graph-engine/tests/runtime/test_effects.py -v
uv run pytest packages/graph-engine/tests/runtime/test_engine.py -q
uv run pytest packages/graph-engine/tests/runtime/test_planner.py packages/graph-engine/tests/runtime/test_scheduler.py -q
uv run ruff check packages/graph-engine/graph_engine/runtime packages/graph-engine/tests/runtime/test_effects.py
uv run pyright packages/graph-engine/graph_engine/runtime
```

Expected: all pass; no task/node/downstream success precedes all receipts.

- [ ] **Step 6: Commit effect execution**

```bash
git add packages/graph-engine/graph_engine/plugin_api.py packages/graph-engine/graph_engine/runtime packages/graph-engine/tests/runtime/test_effects.py packages/graph-engine/tests/runtime/test_engine.py
git commit -m "feat(graph-engine): reconcile durable plugin effects"
```

### Task 12: Close crash, ambiguity, concurrency, and descriptor-lifecycle gaps

**Files:**
- Modify: `packages/graph-engine/graph_engine/runtime/effects.py`
- Modify: `packages/graph-engine/graph_engine/runtime/engine.py`
- Modify: `packages/graph-engine/graph_engine/runtime/ledger.py:1-384`
- Modify: `packages/graph-engine/graph_engine/runtime/workspace.py`
- Modify: `packages/graph-engine/tests/runtime/test_effects.py`
- Modify: `packages/graph-engine/tests/runtime/test_engine.py`
- Modify: `packages/graph-engine/tests/runtime/test_workspace.py`

**Interfaces:**
- Produces: authoritative reopen/reconcile behavior across every section 13.5 crash cut and explicit `EffectPublicationIndeterminate` outcomes.
- Consumes: Tasks 8-11 complete runtime.
- Consumers: Task 13 Toy fault integration and Task 14 acceptance.

- [ ] **Step 1: Add the exact process-crash matrix RED harness**

Parameterize real fresh `Engine.open()` cuts:

```python
@pytest.mark.parametrize(
    "cut",
    (
        "before_prepared_batch",
        "after_head_before_prepared_batch",
        "after_intents_before_apply",
        "after_apply_started_before_apply",
        "after_external_apply_before_receipt",
        "during_receipt_append",
        "after_receipt_before_next_effect",
        "after_all_receipts_before_task_success",
        "after_task_success",
    ),
)
def test_effect_crash_reopen_is_exactly_once(tmp_path: Path, cut: str) -> None:
    evidence = run_effect_crash_cut(tmp_path, cut=cut)
    assert evidence.final_status == "succeeded"
    assert evidence.observable_applications_by_key == {_KEY1: 1, _KEY2: 1}
    assert evidence.reopened_ledger_digest == evidence.clean_ledger_digest
    assert evidence.reopened_tree_id == evidence.clean_tree_id
```

Add two-engine runner-claim contention, reclaim vs settlement, exact receipt append installed/absent/unreadable, handler/schema drift rejection before reconcile, stale handle, retained traceback FD bounds, lock/ledger/workspace namespace swaps, and close-error-primary-identity tests.

- [ ] **Step 2: Run the crash matrix and confirm concrete RED cuts**

Run:

```bash
uv run pytest packages/graph-engine/tests/runtime/test_effects.py -k crash -v
uv run pytest packages/graph-engine/tests/runtime/test_engine.py -k "effect and reopen" -v
```

Expected: each newly exposed unhandled cut fails with its exact expected duplicate, lost receipt, wrong digest, raw OSError, or leaked-descriptor symptom; record RED evidence in the task report.

- [ ] **Step 3: Reconcile ambiguous append outcomes from authoritative ranges**

For every prepared/apply/receipt/success append error, reread the ledger and authenticate the exact expected `EventEnvelope` range. Exact presence returns success; proven absence performs only the safe next action; unreadable or conflicting ranges raise `EffectPublicationIndeterminate` without applying again or overwriting later state.

- [ ] **Step 4: Serialize open-time recovery under the runner claim**

Authenticate lock, ledger, HEAD journal, effect projection, and pending effect state under the invocation runner claim before any reconcile. Preserve the lock ordering `runner claim -> workspace store lock -> ledger CAS`; effect handlers never receive a store/ledger descriptor. Close every Engine-owned duplicated descriptor on all `BaseException` paths without masking the primary error.

- [ ] **Step 5: Run complete runtime and static gates**

Run:

```bash
uv run pytest packages/graph-engine/tests/runtime -q
uv run ruff check packages/graph-engine/graph_engine/runtime packages/graph-engine/tests/runtime
uv run ruff format --check packages/graph-engine/graph_engine/runtime packages/graph-engine/tests/runtime
uv run pyright packages/graph-engine/graph_engine/runtime
git diff --check
```

Expected: all pass; the exact crash matrix has identical clean/reopened ledger and tree digests.

- [ ] **Step 6: Commit recovery hardening**

```bash
git add packages/graph-engine/graph_engine/runtime packages/graph-engine/tests/runtime
git commit -m "fix(graph-engine): harden durable effect recovery"
```

### Task 13: Extend Toy products and CLI with mixed sources, effects, and conformance

**Files:**
- Create: `packages/graph-engine/graph_engine/composition/conformance.py`
- Create: `packages/graph-engine/tests/composition/test_conformance.py`
- Modify: `examples/graph-engine-toy-a/graph_engine_toy_a/plugin.py`
- Modify: `examples/graph-engine-toy-a/graph_engine_toy_a/product.py`
- Create: `examples/graph-engine-toy-a/graph_engine_toy_a/effects.py`
- Modify: `examples/graph-engine-toy-a/pyproject.toml`
- Modify: `examples/graph-engine-toy-b/graph_engine_toy_b/plugin.py`
- Modify: `examples/graph-engine-toy-b/graph_engine_toy_b/product.py`
- Create: `examples/graph-engine-toy-b/config/plugin.yaml`
- Create: `examples/graph-engine-toy-b/config/workflow.yaml`
- Create: `examples/graph-engine-toy-b/config/role.md`
- Modify: `examples/graph-engine-toy-b/pyproject.toml`
- Modify: `packages/graph-engine/graph_engine/__main__.py:1-215`
- Modify: `packages/graph-engine/tests/test_cli.py`
- Modify: `packages/graph-engine/tests/integration/test_toy_a.py`
- Modify: `packages/graph-engine/tests/integration/test_toy_b.py`

**Interfaces:**
- Produces: reusable `assert_wheel_plugin_conforms()` and `assert_config_plugin_conforms()` helpers; wheel-only effectful Toy A; mixed wheel+config Toy B; explicit Phase 2 CLI source arguments.
- Consumes: Tasks 1-12 complete composition/runtime.
- Consumers: Task 14 smoke and acceptance.

- [ ] **Step 1: Add conformance RED tests**

```python
def test_wheel_conformance_accepts_toy_a() -> None:
    report = assert_wheel_plugin_conforms(_toy_a_source())
    assert report.plugin_id == "toy.a"
    assert report.checks == tuple(sorted(report.checks))


def test_config_conformance_accepts_toy_b_flow() -> None:
    report = assert_config_plugin_conforms(
        ConfigTreePluginSource(path=TOY_B_ROOT / "config"),
        selected_wheel_capabilities=("toy.b.runtime.seed", "toy.b.runtime.left", "toy.b.runtime.child", "toy.b.runtime.combine"),
    )
    assert report.plugin_id == "toy.b.flow"
```

Add deliberately nondeterministic descriptor/contribution, import-time registration sentinel, missing implementation, invalid JSON output, missing apply/reconcile, executable config, undeclared config file, unstable digest, and unknown binding fixtures.

- [ ] **Step 2: Run conformance tests and confirm RED**

Run: `uv run pytest packages/graph-engine/tests/composition/test_conformance.py -v`

Expected: conformance helpers do not exist.

- [ ] **Step 3: Implement reusable subprocess-safe conformance runners**

Run wheel descriptor/contribution checks twice in-process and once through a fresh Python subprocess that emits canonical JSON. Import the wheel in an isolated process before any registry platform construction and assert that no engine global registry exists or changes. For config trees, run the strict loader twice, compare canonical snapshots, and execute every negative closed-schema/path/media test through shared helper functions.

Return a frozen report:

```python
class ConformanceReport(FrozenModel):
    source_digest: str
    plugin_id: str
    plugin_version: str
    checks: tuple[str, ...]
```

- [ ] **Step 4: Extend the migrated Toy A contribution with ordered durable effects**

Extend the Task 6 `ToyAPlugin.contribute()` implementation with effect kind `toy.a.audit.append`. The existing `execute()` greet handler writes `greeting.txt` and returns two ordered intents:

```python
return TaskOutcome.succeeded(
    {"message": message},
    effects=(
        EffectIntent(kind="toy.a.audit.append", payload={"line": "greet-started"}),
        EffectIntent(kind="toy.a.audit.append", payload={"line": message}),
    ),
)
```

The Toy effect handler stores idempotency-keyed observable evidence outside the attempt workspace only in the integration fixture supplied port, supports fault injection, and reconciles by key. Unit plugin code itself remains business-neutral Toy code and never receives engine store paths.

- [ ] **Step 5: Migrate Toy B to a mixed wheel + config composition**

Rename the executable wheel plugin to `toy.b.runtime` and its handler IDs to `toy.b.runtime.*`. Create config plugin `toy.b.flow` depending exactly on `toy.b.runtime>=1,<2`; it owns aliases `toy.b.flow.seed/left/child/combine`, the workflow YAML, and role resource. The product requires both explicit sources and selects the config workflow resource. No Python graph remains duplicated in `product.py`.

- [ ] **Step 6: Replace CLI product/plugin flags with explicit Phase 2 sources**

Keep commands explicit:

```text
python -m graph_engine compile \
  --product-dist graph-engine-toy-a --product-entrypoint toy-a \
  --plugin-dist graph-engine-toy-a --plugin-entrypoint toy-a

python -m graph_engine run \
  --product-dist graph-engine-toy-b --product-entrypoint toy-b \
  --plugin-dist graph-engine-toy-b --plugin-entrypoint toy-b-runtime \
  --config-plugin /absolute/path/to/examples/graph-engine-toy-b/config \
  --entrypoint review --invocation-id smoke --root /tmp/graph-engine-smoke
```

Reject missing distribution names, ambient entry-point-only lookup, extra source, implicit config path, and composition mismatch. CLI output includes lock/composition/source/registry digests plus terminal evidence.

- [ ] **Step 7: Run Toy, CLI, and conformance GREEN gates**

Run:

```bash
uv run pytest packages/graph-engine/tests/composition/test_conformance.py -v
uv run pytest packages/graph-engine/tests/integration/test_toy_a.py packages/graph-engine/tests/integration/test_toy_b.py -v
uv run pytest packages/graph-engine/tests/test_cli.py -v
uv run ruff check packages/graph-engine/graph_engine examples/graph-engine-toy-a examples/graph-engine-toy-b packages/graph-engine/tests
uv run pyright packages/graph-engine/graph_engine examples/graph-engine-toy-a examples/graph-engine-toy-b
```

Expected: all pass; Toy A proves ordered exactly-once effects and Toy B proves mixed data-only binding.

- [ ] **Step 8: Commit Toy and conformance migration**

```bash
git add packages/graph-engine/graph_engine packages/graph-engine/tests examples/graph-engine-toy-a examples/graph-engine-toy-b
git commit -m "test(graph-engine): prove frozen plugin compositions"
```

### Task 14: Enforce Phase 2 wheel isolation, drift, crash, and repository gates

**Files:**
- Modify: `packages/graph-engine/README.md`
- Modify: `scripts/graph_engine_smoke_test.sh`
- Modify: `tests/architecture/test_graph_engine_boundaries.py`
- Modify: `.importlinter`
- Modify: `.github/workflows/ci.yml`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `.superpowers/sdd/2026-08-20-pure-graph-engine-phase2/acceptance-evidence.md` (ignored execution evidence; do not stage unless repository policy explicitly tracks it)

**Interfaces:**
- Produces: committed-HEAD offline wheel smoke, import firewall, full Phase 2 acceptance evidence, and documented public composition/effect interfaces.
- Consumes: every prior task.

- [ ] **Step 1: Add architecture and wheel-content RED assertions**

```python
def test_graph_engine_has_no_product_or_agent_dependency() -> None:
    forbidden = ("assurance_agent", "assurance_kernel", "opencode", "cursor")
    for module in _graph_engine_modules():
        source = module.read_text(encoding="utf-8").lower()
        assert not any(name in source for name in forbidden)


def test_phase_two_public_surface_has_no_phase_one_aliases() -> None:
    import graph_engine
    for retired in ("ResolvedProduct", "PluginRuntime", "resolve_product"):
        assert not hasattr(graph_engine, retired)
```

Add engine wheel inspection for no Toy/default graph/config files; Toy A wheel exact dependency on `graph-engine`; Toy B wheel plus external explicit config; and config tree containing no executable file.

- [ ] **Step 2: Extend the committed-HEAD offline smoke script**

Archive committed HEAD, build `graph-engine`, Toy A, and Toy B wheels offline, then create four isolated venvs:

1. engine only: import/CLI succeeds; run without explicit product fails closed;
2. engine + Toy A: wheel-only resolve/run, two effects, crash/reopen, exact lock evidence;
3. engine + Toy B: explicit mixed config resolve/run/interrupt/resume;
4. engine + mutated copy of one selected source: open fails with `InvocationDrift` and ledger digest is unchanged.

Print machine-readable lines `ENGINE_WHEEL_FILES=`, `TOY_A_PHASE2_EVIDENCE=`, `TOY_B_PHASE2_EVIDENCE=`, `DRIFT_EVIDENCE=`, and final `graph-engine phase2 smoke test: OK`.

- [ ] **Step 3: Strengthen import-linter and CI**

Keep `graph_engine` forbidden from importing `assurance_agent`, `assurance_kernel`, and Toy packages. Add an internal layers contract:

```ini
[importlinter:contract:graph-engine-phase2-layers]
name = graph engine runtime depends on composition interfaces, never source adapters on runtime
type = layers
layers =
    graph_engine.runtime
    graph_engine.composition.resolver : graph_engine.composition.conformance
    graph_engine.composition.registries : graph_engine.composition.dependencies : graph_engine.composition.declarative : graph_engine.composition.sources
    graph_engine.plugin_api : graph_engine.graph : graph_engine.canonical : graph_engine.identifiers : graph_engine.errors
```

Adjust only if import-linter requires grouping syntax, without adding ignore edges that invert the intended dependency. Add the Phase 2 smoke and focused graph-engine suite to CI.

- [ ] **Step 4: Document exact public interfaces and non-cutover status**

Update README examples for `RegistryPlatform`, explicit source refs, `FrozenComposition`, lock drift, config-tree rules, effect lifecycle, conformance commands, and the Phase 2 CLI. State prominently that `aa` still uses the old runtime and that OpenCode/Cursor/Assurance migration begins only in later phases.

- [ ] **Step 5: Run focused acceptance gates**

Run:

```bash
uv run pytest packages/graph-engine/tests tests/architecture/test_graph_engine_boundaries.py -q
uv run ruff check packages/graph-engine examples/graph-engine-toy-a examples/graph-engine-toy-b tests/architecture/test_graph_engine_boundaries.py
uv run ruff format --check packages/graph-engine examples/graph-engine-toy-a examples/graph-engine-toy-b tests/architecture/test_graph_engine_boundaries.py
uv run pyright packages/graph-engine/graph_engine examples/graph-engine-toy-a examples/graph-engine-toy-b
uv run lint-imports
bash scripts/graph_engine_smoke_test.sh
git diff --check
```

Expected: every command exits 0; graph-engine test summary has no failure and smoke ends with the exact OK marker.

- [ ] **Step 6: Run full repository verification and triage only the fixed baseline**

Run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest -q
bash scripts/packaging_smoke_test.sh
```

Expected: all static and packaging commands exit 0. Full pytest may report only these four pre-existing failures and no others:

```text
test_benchmark_test_scaffold_is_an_importable_package
test_shared_e2e_login_uses_locators_present_in_the_real_dom
test_shared_fuzz_fixtures_use_the_live_sut_without_invented_app_imports
test_generated_http_scaffold_matches_observed_response_shapes
```

Record the exact passed/skipped/failure counts and causal `git diff` proof in `acceptance-evidence.md`; never describe the full suite as green while those fixtures remain absent.

- [ ] **Step 7: Commit Phase 2 acceptance gates**

```bash
git add packages/graph-engine/README.md scripts/graph_engine_smoke_test.sh tests/architecture/test_graph_engine_boundaries.py .importlinter .github/workflows/ci.yml pyproject.toml uv.lock
git commit -m "build(graph-engine): enforce phase two isolation gates"
```

## Final Review and Handoff

- [ ] Generate one whole-range review package from the Phase 2 base commit to final HEAD.
- [ ] Run an independent Standards review against repository instructions and a Spec review against `docs/superpowers/specs/2026-08-20-pure-graph-engine-phase2-registry-platform-design.md`.
- [ ] Fix every Critical and Important finding with focused RED/GREEN evidence; rerun the affected task gate after each fix.
- [ ] Rerun the Task 14 focused acceptance commands from a clean committed HEAD.
- [ ] Record final commit SHAs, exact gates, known baseline failures, residual trust assumptions, and the explicit statement that `aa` has not cut over.
- [ ] Do not begin Phase 3 until Phase 2 review reports 0 Critical and 0 Important findings.

## Suggested Task Dependency Graph

```text
1 SPI contracts
└─ 2 source filesystem
   ├─ 3 wheel sources
   └─ 4 declarative sources
      └─ 5 dependency graph
         └─ 6 five registries
            └─ 7 resolver + frozen lock model
               └─ 8 Engine lock persistence
                  └─ 9 effect events/fold/planner
                     └─ 10 prepared commit
                        └─ 11 effect executor
                           └─ 12 crash/recovery hardening
                              └─ 13 Toy + conformance + CLI
                                 └─ 14 isolation + final gates
```

Tasks are intentionally sequential because each changes shared public/runtime interfaces. Parallel implementation against one worktree would create conflicting edits in `plugin_api.py`, composition models, runtime events, scheduler, and Engine. Parallel read-only review is safe only after each task's implementation commit.

## Spec Coverage Matrix

| Phase 2 design requirement | Owning tasks |
|---|---|
| Frozen plugin SPI and no mutable/global registration | 1, 6, 13 |
| Authenticated explicit source snapshots and implementation hashing | 2, 3, 4 |
| Installed wheel, editable wheel, product file, and config-tree sources | 3, 4, 7 |
| Exact PEP 440 dependency validation without version selection | 5, 7 |
| Five fixed immutable registries and data-only aliases | 6, 7 |
| Canonical FrozenComposition and complete InvocationLock | 7, 8 |
| Lock-before-ledger bootstrap and byte-exact drift rejection | 8, 12 |
| Effect contracts, intent validation, stable IDs, and prepared publication | 1, 9, 10 |
| Apply/reconcile/receipt ordering and non-retryable permanent failure | 9, 11 |
| Complete crash/ambiguity/reopen matrix | 10, 11, 12 |
| Wheel/config conformance and mixed-source Toy proof | 13 |
| Wheel isolation, import firewalls, static/full tests, and honest baseline triage | 14 |
| No real agent/Assurance migration and explicit non-cutover status | Global Constraints, 14 |
