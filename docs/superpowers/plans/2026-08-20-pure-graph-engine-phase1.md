# Pure Graph Engine Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an independently installable, business-neutral `graph-engine` wheel that compiles and runs explicit product graphs through invocation-scoped plugins, then prove the seam with two materially different toy products.

**Architecture:** Add a new `graph_engine` package beside the old runtime and do not import or shim `assurance_kernel`. The package owns only structural graph compilation, event-sourced planning, deterministic scheduling, snapshot workspaces, retry/lease/checkpoint recovery, and generic product/plugin dispatch. Phase 1 uses exact, explicitly supplied wheel plugins; declarative plugin loading, dependency solving, full source digests, and `InvocationLock` belong to Phase 2.

**Tech Stack:** Python 3.11, Pydantic 2, PyYAML, asyncio, hatchling, uv workspace, pytest, Ruff, Pyright, import-linter.

**Spec:** `docs/superpowers/specs/2026-08-20-pure-graph-engine-plugin-architecture-design.md`

## Global Constraints

- `graph_engine` must not import `assurance_agent`, `assurance_kernel`, either agent runtime, or any product/plugin package.
- The engine wheel contains no default graph, default product, default operation, skill, prompt, Assurance schema, or business model.
- No `assurance_kernel.*` or `assurance_agent.workflow.*` compatibility shim is created.
- No old-runtime invocation is resumed by the new engine.
- Product and plugin registration is explicit, invocation-scoped, deterministic, and free of import-time registry mutation.
- Python wheel plugins are trusted code; configuration is data. Phase 1 does not implement a hostile-code sandbox.
- Task input, output, event payloads, and product/plugin descriptors are JSON-compatible and validated before persistence.
- Graph compilation and planning are pure and deterministic. Wall-clock time and concurrent completion order cannot affect task IDs, event order, or terminal state.
- Candidate workspace changes become visible only after invariant checks and explicitly selected plugin validators pass.
- Every task follows red-green-refactor and ends with an independently reviewable commit.
- Existing dirty files outside a task's explicit file list belong to the user and must not be staged.

## Phase 1 boundaries

Phase 1 includes the minimal complete fixed engine: task, gate, join, subgraph, interrupt, and end nodes; routing; bounded activation count; retry; timeout; resource-aware waves; leases; immutable snapshots; atomic ledger batches; checkpoints; explicit product/plugin loading; and two toy products.

Phase 1 deliberately excludes declarative `plugin.yaml`, semantic-version dependency solving, wheel/source hashing, `InvocationLock`, durable external effects, fan-out, and graph budgets. Those capabilities are owned by the separately specified Phase 2 project. Phase 1 must not add provisional APIs for them.

## Locked file structure

```text
packages/graph-engine/
  pyproject.toml
  README.md
  graph_engine/
    __init__.py
    __main__.py
    errors.py
    canonical.py
    identifiers.py
    plugin_api.py
    product.py
    graph/
      __init__.py
      schema.py
      expressions.py
      compiler.py
    runtime/
      __init__.py
      models.py
      events.py
      ledger.py
      checkpoint.py
      workspace.py
      planner.py
      scheduler.py
      engine.py
  tests/
    test_primitives.py
    test_plugin_registry.py
    test_product_resolution.py
    test_cli.py
    graph/test_schema_and_compiler.py
    runtime/test_ledger_and_checkpoint.py
    runtime/test_workspace.py
    runtime/test_planner.py
    runtime/test_scheduler.py
    runtime/test_engine.py
    integration/test_toy_a.py
    integration/test_toy_b.py
examples/graph-engine-toy-a/
  pyproject.toml
  graph_engine_toy_a/{__init__.py,plugin.py,product.py}
examples/graph-engine-toy-b/
  pyproject.toml
  graph_engine_toy_b/{__init__.py,plugin.py,product.py}
tests/architecture/test_graph_engine_boundaries.py
scripts/graph_engine_smoke_test.sh
```

Do not create a shared `graph_engine` namespace in any other distribution. Toy packages use their own import package and depend only on `graph-engine`.

---

### Task 1: Independent wheel and neutral primitives

**Files:**
- Create: `packages/graph-engine/pyproject.toml`
- Create: `packages/graph-engine/README.md`
- Create: `packages/graph-engine/graph_engine/__init__.py`
- Create: `packages/graph-engine/graph_engine/errors.py`
- Create: `packages/graph-engine/graph_engine/canonical.py`
- Create: `packages/graph-engine/graph_engine/identifiers.py`
- Create: `packages/graph-engine/tests/test_primitives.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: Python 3.11, Pydantic 2, PyYAML from the workspace toolchain.
- Produces: `ENGINE_API_VERSION`, `GraphEngineError`, `canonical_json_bytes()`, `canonical_digest()`, and `validate_qualified_id()` for every subsequent task.

- [ ] **Step 1: Write the failing primitive tests**

```python
import json

import pytest

from graph_engine import ENGINE_API_VERSION
from graph_engine.canonical import canonical_digest, canonical_json_bytes
from graph_engine.identifiers import IdentifierError, validate_qualified_id


def test_public_engine_api_version_is_explicit() -> None:
    assert ENGINE_API_VERSION == "1.0"


def test_canonical_json_is_order_independent() -> None:
    left = canonical_json_bytes({"b": 2, "a": [1, True, None]})
    right = canonical_json_bytes({"a": [1, True, None], "b": 2})
    assert left == right == b'{"a":[1,true,null],"b":2}'
    assert canonical_digest(json.loads(left)) == canonical_digest(json.loads(right))


@pytest.mark.parametrize("value", ["assurance.execution", "agent.opencode.execute"])
def test_qualified_ids_require_a_namespace(value: str) -> None:
    assert validate_qualified_id(value) == value


@pytest.mark.parametrize("value", ["run", "Operation:run", "a..b", "a/b", "a_b.c"])
def test_invalid_qualified_ids_fail(value: str) -> None:
    with pytest.raises(IdentifierError):
        validate_qualified_id(value)
```

- [ ] **Step 2: Run the test to verify package absence**

Run: `uv run pytest packages/graph-engine/tests/test_primitives.py -v`

Expected: collection fails with `ModuleNotFoundError: No module named 'graph_engine'`.

- [ ] **Step 3: Add the workspace member and package metadata**

Add `packages/graph-engine` to `[tool.uv.workspace].members`, add
`graph-engine = { workspace = true }` to `[tool.uv.sources]`, add
`"packages/graph-engine/graph_engine"` to Pyright's include list, and add
`"graph-engine"` to the root dev dependency group.

Use this package metadata:

```toml
[project]
name = "graph-engine"
version = "0.1.0"
description = "Business-neutral deterministic graph runtime"
requires-python = ">=3.11,<4.0"
dependencies = ["pydantic>=2.7", "pyyaml>=6.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["graph_engine"]
```

- [ ] **Step 4: Implement the neutral primitives**

`graph_engine/__init__.py` exports only:

```python
ENGINE_API_VERSION = "1.0"

__all__ = ["ENGINE_API_VERSION"]
```

`canonical.py` must reject non-JSON values before hashing:

```python
import hashlib
import json
from typing import TypeAlias

JSONScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


def canonical_json_bytes(value: JSONValue) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_digest(value: JSONValue) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
```

`identifiers.py` uses `^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+$` and raises an
`IdentifierError(GraphEngineError)` containing the rejected value.

- [ ] **Step 5: Run focused quality checks**

Run:

```bash
uv sync --dev
uv run pytest packages/graph-engine/tests/test_primitives.py -v
uv run ruff check packages/graph-engine
uv run pyright packages/graph-engine/graph_engine
```

Expected: all commands pass.

- [ ] **Step 6: Commit the wheel foundation**

```bash
git add pyproject.toml uv.lock packages/graph-engine
git commit -m "feat(graph-engine): add neutral wheel foundation"
```

---

### Task 2: Generic plugin execution contracts and invocation-scoped registry

**Files:**
- Create: `packages/graph-engine/graph_engine/plugin_api.py`
- Create: `packages/graph-engine/tests/test_plugin_registry.py`
- Modify: `packages/graph-engine/graph_engine/__init__.py`

**Interfaces:**
- Consumes: `ENGINE_API_VERSION`, `JSONValue`, `validate_qualified_id()`.
- Produces: `ResourceClaims`, `TaskRequest`, `TaskContext`, `TaskOutcome`, `TaskFailure`, `TaskHandler`, `CandidateWriteSet`, `ValidationContext`, `ValidationResult`, `CommitValidator`, `PluginDescriptor`, `PluginRuntime`, `PluginProvider`, `EnginePorts`, `CapabilityRegistry`, and `assemble_registry()`.

- [ ] **Step 1: Write failing registry tests**

```python
from dataclasses import dataclass

import pytest

from graph_engine.plugin_api import (
    CapabilityRegistryError,
    EnginePorts,
    PluginDescriptor,
    PluginRuntime,
    TaskContext,
    TaskOutcome,
    TaskRequest,
    assemble_registry,
)


async def _ping(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
    return TaskOutcome.succeeded({"pong": True})


@dataclass(frozen=True)
class Provider:
    plugin_id: str

    def descriptor(self) -> PluginDescriptor:
        return PluginDescriptor(
            plugin_id=self.plugin_id,
            plugin_version="1.0.0",
            engine_api="1.0",
            task_handlers=(f"{self.plugin_id}.ping",),
            commit_validators=(),
        )

    def bind(self, _ports: EnginePorts) -> PluginRuntime:
        return PluginRuntime(task_handlers={f"{self.plugin_id}.ping": _ping}, commit_validators={})


def test_registry_is_local_and_exact() -> None:
    first = assemble_registry((Provider("toy.one"),))
    second = assemble_registry((Provider("toy.two"),))
    assert set(first.task_handlers) == {"toy.one.ping"}
    assert set(second.task_handlers) == {"toy.two.ping"}


def test_descriptor_and_runtime_must_match() -> None:
    class Broken(Provider):
        def bind(self, _ports: EnginePorts) -> PluginRuntime:
            return PluginRuntime(task_handlers={}, commit_validators={})

    with pytest.raises(CapabilityRegistryError, match="declared and bound task handlers differ"):
        assemble_registry((Broken("toy.broken"),))


def test_duplicate_capability_fails_without_last_wins() -> None:
    duplicate = Provider("toy.one")
    with pytest.raises(CapabilityRegistryError, match="duplicate plugin id"):
        assemble_registry((duplicate, duplicate))
```

- [ ] **Step 2: Run tests to verify the contracts are missing**

Run: `uv run pytest packages/graph-engine/tests/test_plugin_registry.py -v`

Expected: collection fails because `graph_engine.plugin_api` does not exist.

- [ ] **Step 3: Implement frozen task contracts**

Use frozen Pydantic models with `extra="forbid"`:

```python
FailureKind = Literal["transient", "timeout", "invalid_input", "invalid_output", "internal"]
TaskStatus = Literal["succeeded", "failed", "stopped"]


class TaskFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: FailureKind
    message: str


class TaskRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: str
    task_id: str
    graph_instance_id: str
    node_id: str
    capability_id: str
    attempt: int = Field(ge=1)
    input: JSONValue
    prior_failure: TaskFailure | None = None


class TaskOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: TaskStatus
    output: JSONValue = None
    failure: TaskFailure | None = None
    stop_reason: str | None = None

    @classmethod
    def succeeded(cls, output: JSONValue = None) -> "TaskOutcome":
        return cls(status="succeeded", output=output)

    @classmethod
    def failed(cls, kind: FailureKind, message: str) -> "TaskOutcome":
        return cls(status="failed", failure=TaskFailure(kind=kind, message=message))

    @classmethod
    def stopped(cls, reason: str, output: JSONValue = None) -> "TaskOutcome":
        return cls(status="stopped", output=output, stop_reason=reason)
```

`TaskOutcome` validation requires `failure` exactly when status is `failed` and
requires a non-empty `stop_reason` exactly when status is `stopped`. A task
handler cannot return `interrupted`; only the structural interrupt node creates
an interrupt checkpoint.
`TaskContext` is a frozen dataclass containing `workspace_root: Path` and a
callable `heartbeat: Callable[[], None]`. `TaskHandler` is an async callable
protocol. Catching arbitrary plugin exceptions belongs to Task 8's scheduler,
not this registry.

Define the generic resource and validation contracts in the same module so the
plugin SPI never points at a type introduced by a later task:

```python
class ResourceClaims(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    exclusive: tuple[str, ...] = ()


class CandidateFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    path: str
    before_sha256: str | None
    after_sha256: str | None


class CandidateWriteSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    baseline_tree_id: str
    candidate_tree_id: str
    files: tuple[CandidateFile, ...]


class ValidationContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: str
    task_id: str
    graph_instance_id: str
    node_id: str
    resources: ResourceClaims


class ValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    accepted: bool
    reason: str | None = None


class CommitValidator(Protocol):
    def validate(self, candidate: CandidateWriteSet, context: ValidationContext) -> ValidationResult: ...
```

Require non-empty segment-aware relative resource prefixes, prohibit `..` and
absolute paths, and require a rejection reason exactly when `accepted` is
false.

- [ ] **Step 4: Implement exact descriptor binding**

```python
@dataclass(frozen=True, slots=True)
class EnginePorts:
    engine_api: str = ENGINE_API_VERSION


@dataclass(frozen=True, slots=True)
class PluginDescriptor:
    plugin_id: str
    plugin_version: str
    engine_api: str
    task_handlers: tuple[str, ...]
    commit_validators: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PluginRuntime:
    task_handlers: Mapping[str, TaskHandler]
    commit_validators: Mapping[str, "CommitValidator"]
```

`assemble_registry()` validates plugin IDs and every provided capability ID,
rejects duplicate plugin IDs/capabilities, requires `descriptor.engine_api ==
ENGINE_API_VERSION`, calls `bind()` exactly once, requires descriptor/runtime
key equality, and returns a new immutable `CapabilityRegistry` without writing
module globals. `CapabilityRegistry.empty()` returns a registry backed by empty
immutable mappings and is the only zero-capability constructor used by tests.

- [ ] **Step 5: Run focused tests and static checks**

Run:

```bash
uv run pytest packages/graph-engine/tests/test_plugin_registry.py -v
uv run ruff check packages/graph-engine/graph_engine/plugin_api.py
uv run pyright packages/graph-engine/graph_engine/plugin_api.py
```

Expected: all pass.

- [ ] **Step 6: Commit the plugin contracts**

```bash
git add packages/graph-engine/graph_engine packages/graph-engine/tests/test_plugin_registry.py
git commit -m "feat(graph-engine): define invocation scoped plugin registry"
```

---

### Task 3: Structural graph schema and deterministic compiler

**Files:**
- Create: `packages/graph-engine/graph_engine/graph/__init__.py`
- Create: `packages/graph-engine/graph_engine/graph/schema.py`
- Create: `packages/graph-engine/graph_engine/graph/expressions.py`
- Create: `packages/graph-engine/graph_engine/graph/compiler.py`
- Create: `packages/graph-engine/tests/graph/test_schema_and_compiler.py`

**Interfaces:**
- Consumes: `JSONValue`, `canonical_digest()`, `CapabilityRegistry`, `ResourceClaims`.
- Produces: `WorkflowDef`, `GraphDef`, `NodeDef`, `EdgeDef`, `CompiledWorkflow`, `parse_workflow()`, `compile_workflow()`, and `evaluate_expression()`.

- [ ] **Step 1: Write failing compile tests**

```python
import pytest

from graph_engine.graph.compiler import CompileError, compile_workflow
from graph_engine.graph.schema import parse_workflow
from graph_engine.plugin_api import CapabilityRegistry, EnginePorts, PluginDescriptor, PluginRuntime, assemble_registry


async def _ping(_request, _context):
    from graph_engine.plugin_api import TaskOutcome

    return TaskOutcome.succeeded({"pong": True})


class _Provider:
    def descriptor(self) -> PluginDescriptor:
        return PluginDescriptor(
            plugin_id="toy.one",
            plugin_version="1.0.0",
            engine_api="1.0",
            task_handlers=("toy.one.ping",),
            commit_validators=(),
        )

    def bind(self, _ports: EnginePorts) -> PluginRuntime:
        return PluginRuntime(task_handlers={"toy.one.ping": _ping}, commit_validators={})


@pytest.fixture
def registry():
    return assemble_registry((_Provider(),))


VALID = """
name: toy
entrypoints: {main: root}
retry: {once: {max_attempts: 1, retry_on: []}}
timeout: {short: {run_seconds: 5}}
graphs:
  root:
    max_activations: 20
    start: ping
    nodes:
      ping: {kind: task, capability: toy.one.ping, retry: once, timeout: short}
      done: {kind: end}
    edges:
      - {from: ping, to: done}
"""


def test_compile_is_deterministic(registry: CapabilityRegistry) -> None:
    workflow = parse_workflow(VALID)
    first = compile_workflow(workflow, registry)
    second = compile_workflow(workflow, registry)
    assert first.digest == second.digest
    assert first.graphs["root"].declaration_order == ("ping", "done")


def test_unknown_capability_fails_before_runtime() -> None:
    workflow = parse_workflow(VALID)
    with pytest.raises(CompileError, match="unknown capability toy.one.ping"):
        compile_workflow(workflow, CapabilityRegistry.empty())


def test_unreachable_node_is_rejected(registry: CapabilityRegistry) -> None:
    workflow = parse_workflow(VALID.replace("      done: {kind: end}", "      done: {kind: end}\n      lost: {kind: end}"))
    with pytest.raises(CompileError, match="unreachable node root/lost"):
        compile_workflow(workflow, registry)
```

- [ ] **Step 2: Run tests to verify schema modules are absent**

Run: `uv run pytest packages/graph-engine/tests/graph/test_schema_and_compiler.py -v`

Expected: collection fails for `graph_engine.graph`.

- [ ] **Step 3: Implement the closed structural schema**

Use frozen Pydantic models and reject unknown YAML keys. The exact node shape is:

```python
class NodeDef(FrozenModel):
    kind: Literal["task", "gate", "join", "subgraph", "interrupt", "end"]
    capability: str | None = None
    graph: str | None = None
    join: Literal["all", "any"] | None = None
    expression: str | None = None
    reason: str | None = None
    actions: tuple[str, ...] = ()
    input: dict[str, JSONValue] = Field(default_factory=dict)
    retry: str | None = None
    timeout: str | None = None
    resources: ResourceClaims = Field(default_factory=ResourceClaims)
    validators: tuple[str, ...] = ()
```

Import `ResourceClaims` from `graph_engine.plugin_api`; do not define a second
claims model in the graph package.

Validation rules are exact: task requires `capability`, `retry`, and `timeout`;
subgraph requires `graph`; join requires `join`; gate requires `expression`;
interrupt requires non-empty `reason` and unique non-empty `actions`; end accepts
none of those fields. `GraphDef` has `max_activations` in `1..10000`, `start`,
ordered `nodes`, and `edges`. `WorkflowDef` has `name`, `entrypoints`, named retry
and timeout policies, and graphs. There is no resource fallback and no packaged
schema loader.

- [ ] **Step 4: Implement a bounded expression evaluator**

`evaluate_expression(expression, scope)` parses Python expression syntax but
accepts only constants, dictionary-style names, `and`, `or`, `not`, `==`,
`!=`, `<`, `<=`, `>`, `>=`, and `in`. Reject calls, attributes beginning with
`_`, comprehensions, arithmetic, and input longer than 2,048 characters. Convert
`true`, `false`, and `null` names to JSON literals. Tests must cover a valid
`input.route == "left"` expression and rejection of `__import__("os")`.

- [ ] **Step 5: Implement deterministic compilation**

`compile_workflow()` must:

1. validate every entrypoint, edge endpoint, retry, timeout, subgraph, task
   capability, and validator reference;
2. require end nodes to have no outgoing edges;
3. require an `all` join to have at least two distinct incoming sources;
4. reject unreachable nodes from each graph's declared start;
5. assign declaration index and stable topology rank, using declaration order
   to break ranks in cyclic graphs;
6. preserve tuples instead of mutable lists in compiled models; and
7. hash `model_dump(mode="json", by_alias=True)` with `digest` excluded.

Cycles are legal because runtime activation count is bounded; the compiler must
not reject a reachable cycle merely for being cyclic.

- [ ] **Step 6: Run compiler and expression tests**

Run:

```bash
uv run pytest packages/graph-engine/tests/graph/test_schema_and_compiler.py -v
uv run ruff check packages/graph-engine/graph_engine/graph
uv run pyright packages/graph-engine/graph_engine/graph
```

Expected: all pass.

- [ ] **Step 7: Commit the structural language**

```bash
git add packages/graph-engine/graph_engine/graph packages/graph-engine/tests/graph
git commit -m "feat(graph-engine): compile structural product graphs"
```

---

### Task 4: Explicit product provider and resolved bundle

**Files:**
- Create: `packages/graph-engine/graph_engine/product.py`
- Create: `packages/graph-engine/tests/test_product_resolution.py`
- Modify: `packages/graph-engine/graph_engine/__init__.py`

**Interfaces:**
- Consumes: `WorkflowDef`, `compile_workflow()`, `PluginProvider`, `assemble_registry()`.
- Produces: `PluginRequirement`, `ProductManifest`, `ProductProvider`, `ResolvedProduct`, `resolve_product()`, and `load_product_entrypoint()`.

- [ ] **Step 1: Write failing product-resolution tests**

```python
import pytest

from graph_engine.product import (
    PluginRequirement,
    ProductManifest,
    ProductResolutionError,
    resolve_product,
)


def test_only_manifest_plugins_are_enabled(product_provider, plugin_one, plugin_two) -> None:
    resolved = resolve_product(product_provider, {"toy.one": plugin_one, "toy.two": plugin_two})
    assert set(resolved.registry.task_handlers) == {"toy.one.ping"}


def test_installed_but_unlisted_plugin_cannot_change_product(product_provider, plugin_one, plugin_two) -> None:
    without_extra = resolve_product(product_provider, {"toy.one": plugin_one})
    with_extra = resolve_product(product_provider, {"toy.one": plugin_one, "toy.two": plugin_two})
    assert without_extra.digest == with_extra.digest


def test_missing_explicit_plugin_fails(product_provider) -> None:
    with pytest.raises(ProductResolutionError, match="missing plugin toy.one==1.0.0"):
        resolve_product(product_provider, {})
```

- [ ] **Step 2: Run tests to verify product contracts are absent**

Run: `uv run pytest packages/graph-engine/tests/test_product_resolution.py -v`

Expected: collection fails because `graph_engine.product` does not exist.

- [ ] **Step 3: Implement immutable product models**

```python
class PluginRequirement(FrozenModel):
    plugin_id: str
    version: str


class ProductManifest(FrozenModel):
    product_id: str
    product_version: str
    engine_api: str
    plugins: tuple[PluginRequirement, ...]
    workflow: WorkflowDef


class ProductProvider(Protocol):
    def manifest(self) -> ProductManifest: ...
```

Product IDs use the same qualified-ID syntax as plugins. Phase 1 requires exact
plugin versions and exact engine API equality. A manifest cannot repeat a plugin
ID and cannot be empty.

- [ ] **Step 4: Implement explicit resolution and entrypoint loading**

`resolve_product(provider, available_plugins)` reads the manifest exactly once,
selects only `manifest.plugins`, validates exact versions, assembles a fresh
registry, compiles the workflow, and returns `ResolvedProduct`. Its digest covers
the manifest JSON, selected descriptors, registry IDs, and compiled digest; it
does not include unselected installed plugins.

`load_product_entrypoint(product_id)` searches only group
`graph_engine.products`, rejects zero or multiple matching entry points, loads
the selected object, and validates it has callable `manifest`. It does not load
plugin entry points; the caller supplies the explicit provider map in Phase 1.

- [ ] **Step 5: Run product tests and static checks**

Run:

```bash
uv run pytest packages/graph-engine/tests/test_product_resolution.py -v
uv run ruff check packages/graph-engine/graph_engine/product.py
uv run pyright packages/graph-engine/graph_engine/product.py
```

Expected: all pass.

- [ ] **Step 6: Commit product resolution**

```bash
git add packages/graph-engine/graph_engine packages/graph-engine/tests/test_product_resolution.py
git commit -m "feat(graph-engine): resolve explicit product bundles"
```

---

### Task 5: Atomic event ledger, pure fold, and checkpoint cache

**Files:**
- Create: `packages/graph-engine/graph_engine/runtime/__init__.py`
- Create: `packages/graph-engine/graph_engine/runtime/models.py`
- Create: `packages/graph-engine/graph_engine/runtime/events.py`
- Create: `packages/graph-engine/graph_engine/runtime/ledger.py`
- Create: `packages/graph-engine/graph_engine/runtime/checkpoint.py`
- Create: `packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py`

**Interfaces:**
- Consumes: `JSONValue`, `canonical_digest()`.
- Produces: strict event models, `Ledger`, `fold_events()`, `InvocationProjection`, `write_checkpoint()`, and `load_checkpoint()`.

- [ ] **Step 1: Write failing ledger tests**

```python
from pathlib import Path

import pytest

from graph_engine.runtime.events import InvocationStarted, TokenOffered
from graph_engine.runtime.ledger import Ledger, LedgerIntegrityError
from graph_engine.runtime.models import fold_events


def test_atomic_batches_have_contiguous_sequences(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger")
    ledger.append_batch(
        (
            InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
            TokenOffered(token_id="tok-1", graph_instance_id="root", source=None, target="start", payload=None),
        ),
        expected_next_seq=1,
    )
    events = ledger.read_all()
    assert [item.seq for item in events] == [1, 2]
    assert fold_events(events).status == "running"


def test_temporary_batch_is_never_visible(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger")
    ledger.root.mkdir(parents=True)
    (ledger.root / ".pending-1.json").write_text("{broken", encoding="utf-8")
    assert ledger.read_all() == ()


def test_sequence_gap_is_integrity_failure(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger")
    ledger.root.mkdir(parents=True)
    (ledger.root / "0000000002-0000000002.json").write_text("[]", encoding="utf-8")
    with pytest.raises(LedgerIntegrityError, match="expected batch starting at 1"):
        ledger.read_all()
```

- [ ] **Step 2: Run tests to verify runtime persistence is absent**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py -v`

Expected: collection fails for `graph_engine.runtime`.

- [ ] **Step 3: Define strict structural events**

Use a discriminated union with these event kinds and no business fields:

```text
invocation_started
graph_started
token_offered
token_consumed
node_activated
task_attempt_started
task_attempt_succeeded
task_attempt_failed
task_attempt_stopped
node_completed
node_interrupted
interrupt_resumed
graph_completed
invocation_finished
```

Every event is frozen and `extra="forbid"`. IDs, graph/node references, JSON
payloads, attempt numbers, timestamps used only for leases, and typed failures
are explicit fields. `EventEnvelope` contains `seq`, `event`, and
`event_sha256`; its digest excludes `event_sha256` itself.

- [ ] **Step 4: Implement atomic immutable ledger batches**

`Ledger.append_batch(events, expected_next_seq)` serializes a complete tuple to
`.pending-<uuid>.json`, flushes and `fsync()`s the file, then `os.replace()`s it
to `<first-seq>-<last-seq>.json` and `fsync()`s the directory. Existing batch
files are immutable. `read_all()` ignores `.pending-*`, sorts final names,
requires contiguous sequence numbers and valid envelope hashes, and rejects a
batch whose filename does not match its envelope range.

- [ ] **Step 5: Implement the pure projection fold**

`InvocationProjection` tracks invocation status, product digest, entrypoint,
graph instances, offered/consumed tokens, activation records, attempt histories,
pending interrupt, and terminal reason. `fold_events(envelopes)` starts from an
empty projection, applies each event exactly once, and rejects impossible
transitions such as success without an attempt or consuming a token twice.

Do not read files, time, plugins, or graphs while folding.

- [ ] **Step 6: Implement checkpoint as a disposable cache**

`write_checkpoint(path, projection, last_seq)` writes one atomic JSON document
containing `last_seq`, the serialized projection, and a digest. `load_checkpoint`
returns `None` for missing, malformed, digest-mismatched, or ahead-of-ledger
checkpoints. The runtime always falls back to folding authoritative ledger
events; it never repairs the ledger from a checkpoint.

- [ ] **Step 7: Run persistence tests**

Run:

```bash
uv run pytest packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py -v
uv run ruff check packages/graph-engine/graph_engine/runtime
uv run pyright packages/graph-engine/graph_engine/runtime
```

Expected: all pass.

- [ ] **Step 8: Commit ledger and checkpoint support**

```bash
git add packages/graph-engine/graph_engine/runtime packages/graph-engine/tests/runtime
git commit -m "feat(graph-engine): persist strict runtime ledger"
```

---

### Task 6: Immutable snapshot workspace and explicit commit validators

**Files:**
- Create: `packages/graph-engine/graph_engine/runtime/workspace.py`
- Create: `packages/graph-engine/tests/runtime/test_workspace.py`
- Modify: `packages/graph-engine/graph_engine/runtime/models.py`

**Interfaces:**
- Consumes: `ResourceClaims`, `CandidateWriteSet`, `CommitValidator`, `ValidationContext`, `ValidationResult`, and `canonical_digest()`.
- Produces: `SnapshotStore`, `AttemptWorkspace`, and `commit_candidate()`.

- [ ] **Step 1: Write failing workspace tests**

```python
from pathlib import Path

import pytest

from graph_engine.plugin_api import ResourceClaims
from graph_engine.runtime.workspace import SnapshotStore, WorkspaceViolation


def test_failed_attempt_never_moves_head(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {"seed.txt": b"old"})
    before = store.head_tree_id()
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "seed.txt").write_text("new", encoding="utf-8")
    attempt.discard()
    assert store.head_tree_id() == before
    assert store.read_head("seed.txt") == b"old"


def test_undeclared_write_is_rejected(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "outside.txt").write_text("x", encoding="utf-8")
    candidate = attempt.seal()
    with pytest.raises(WorkspaceViolation, match="outside.txt"):
        store.commit_candidate(candidate, ResourceClaims(writes=("allowed/",)))
```

- [ ] **Step 2: Run tests to verify the workspace module is absent**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_workspace.py -v`

Expected: collection fails for `runtime.workspace`.

- [ ] **Step 3: Extend exact plugin binding tests for validators**

Add one accepting validator to the Task 2 test provider and assert the
descriptor/runtime validator key sets must match exactly, just like handlers.
Use the already-defined `CandidateWriteSet`, `ValidationContext`, and
`ValidationResult` contracts; do not redefine them under `runtime`.

- [ ] **Step 4: Implement content-addressed snapshots**

Store immutable tree directories under `trees/<sha256>/`. Hash a tree from
sorted `(relative_posix_path, file_sha256)` pairs. Reject symlinks, absolute
paths, `..`, device files, and filenames that cannot round-trip as UTF-8.
`create_attempt()` copies the current head to `attempts/<attempt_id>`.
`seal()` writes a new immutable tree and returns a complete diff against its
baseline. `discard()` removes only that exact attempt directory.

Publishing a candidate writes a temporary `HEAD.json`, flushes it, and atomically
replaces `HEAD.json`. Tree directories are immutable, so a crash before head
replacement leaves the old head authoritative.

- [ ] **Step 5: Enforce engine invariants and selected validators**

`commit_candidate(candidate, claims, validators, context)` always checks:

- candidate baseline equals current head;
- every changed path is covered by a declared write prefix;
- no read-only/exclusive claim is being used as an undeclared write; and
- the candidate tree exists and matches its ID.

Then run validators in the exact order declared by the compiled task node. Any
exception becomes a rejected validation receipt naming that validator. Only an
all-accepted result moves head.

- [ ] **Step 6: Run workspace and registry tests**

Run:

```bash
uv run pytest packages/graph-engine/tests/runtime/test_workspace.py packages/graph-engine/tests/test_plugin_registry.py -v
uv run ruff check packages/graph-engine/graph_engine/runtime/workspace.py packages/graph-engine/graph_engine/plugin_api.py
uv run pyright packages/graph-engine/graph_engine/runtime/workspace.py packages/graph-engine/graph_engine/plugin_api.py
```

Expected: all pass.

- [ ] **Step 7: Commit workspace isolation**

```bash
git add packages/graph-engine/graph_engine packages/graph-engine/tests
git commit -m "feat(graph-engine): commit validated snapshot workspaces"
```

---

### Task 7: Pure token planner for tasks, gates, joins, cycles, and terminal nodes

**Files:**
- Create: `packages/graph-engine/graph_engine/runtime/planner.py`
- Create: `packages/graph-engine/tests/runtime/test_planner.py`
- Modify: `packages/graph-engine/graph_engine/runtime/models.py`
- Modify: `packages/graph-engine/graph_engine/runtime/events.py`

**Interfaces:**
- Consumes: `CompiledWorkflow`, `InvocationProjection`, `evaluate_expression()`.
- Produces: `PlannedTask`, `PlanResult`, `plan_next()`, deterministic token/activation/task IDs, and structural events.

- [ ] **Step 1: Write failing planner tests**

```python
from graph_engine.runtime.planner import plan_next


def test_start_token_plans_the_first_task(compiled, started_projection) -> None:
    plan = plan_next(compiled, started_projection)
    assert [task.node_id for task in plan.tasks] == ["seed"]
    assert plan.terminal is None


def test_all_join_waits_for_each_predecessor(compiled_join, projection_with_left_only) -> None:
    assert plan_next(compiled_join, projection_with_left_only).tasks == ()


def test_all_join_activates_after_both_tokens(compiled_join, projection_with_both) -> None:
    plan = plan_next(compiled_join, projection_with_both)
    assert any(event.kind == "node_activated" and event.node_id == "joined" for event in plan.events)


def test_cycle_is_bounded_by_graph_activation_limit(compiled_cycle, exhausted_projection) -> None:
    plan = plan_next(compiled_cycle, exhausted_projection)
    assert plan.terminal == "failed"
    assert plan.reason == "max_activations_exceeded:root"
```

- [ ] **Step 2: Run tests to verify planner absence**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_planner.py -v`

Expected: collection fails for `runtime.planner`.

- [ ] **Step 3: Implement deterministic token identities**

Derive IDs exclusively from canonical inputs:

```python
def activation_id(graph_instance_id: str, node_id: str, generation: int, token_ids: tuple[str, ...]) -> str:
    return canonical_digest({
        "graph_instance_id": graph_instance_id,
        "node_id": node_id,
        "generation": generation,
        "token_ids": list(token_ids),
    })


def task_id(activation: str) -> str:
    return canonical_digest({"activation_id": activation, "kind": "task"})
```

Never include timestamps, process IDs, random UUIDs, or completion order.

- [ ] **Step 4: Implement pure activation planning**

`plan_next()` performs no I/O and follows these rules:

- a graph-start event offers one synthetic token to the graph's start node;
- a non-join node consumes the earliest unconsumed token ordered by token ID;
- an `any` join consumes the earliest token from any predecessor;
- an `all` join consumes exactly one earliest token from every distinct incoming
  predecessor, ordered by compiled predecessor order;
- every activation input is the JSON object `{"config": node.input, "tokens":
  [consumed_token_payloads...]}`; handlers never receive an order-dependent
  dictionary merge of predecessor outputs;
- generation is the count of prior activations for the same graph instance and
  node;
- each successful node completion evaluates outgoing edge conditions against
  `{"input": activation.input, "output": completion.output}` and offers one
  token per matching edge in compiled edge order;
- gate nodes evaluate their expression and complete structurally with
  `{"value": bool}`;
- join nodes complete structurally with `{"tokens": [...]}` in compiled
  predecessor order;
- a stopped task attempt terminates the root invocation as `stopped` with its
  exact `stop_reason` and emits no outgoing token;
- an end node completes its graph with its sole consumed payload, or with
  `{"tokens": [...]}` when it consumed more than one payload; root graph
  completion sets invocation success;
  and
- exceeding `max_activations` fails the invocation before creating another task.

Planner events are returned in deterministic order and become authoritative only
when the caller appends the complete batch.

- [ ] **Step 5: Implement retry planning**

A failed task attempt is planned again only when its failure kind is present in
the node's retry policy and attempts used are below `max_attempts`. The new
`PlannedTask` preserves task ID and activation ID, increments `attempt`, and
includes the previous `TaskFailure`. Exhaustion completes the node as failed and
fails the graph. Timeout is represented by failure kind `timeout` and follows
the same rule.

- [ ] **Step 6: Run planner tests and determinism replay**

Run:

```bash
uv run pytest packages/graph-engine/tests/runtime/test_planner.py -v
uv run pytest packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py -v
uv run ruff check packages/graph-engine/graph_engine/runtime/planner.py
uv run pyright packages/graph-engine/graph_engine/runtime/planner.py
```

Expected: all pass.

- [ ] **Step 7: Commit deterministic planning**

```bash
git add packages/graph-engine/graph_engine/runtime packages/graph-engine/tests/runtime
git commit -m "feat(graph-engine): plan deterministic token activations"
```

---

### Task 8: Resource-aware async scheduler, leases, timeout, and deterministic commits

**Files:**
- Create: `packages/graph-engine/graph_engine/runtime/scheduler.py`
- Create: `packages/graph-engine/tests/runtime/test_scheduler.py`
- Modify: `packages/graph-engine/graph_engine/runtime/models.py`
- Modify: `packages/graph-engine/graph_engine/runtime/events.py`

**Interfaces:**
- Consumes: `PlannedTask`, `CapabilityRegistry`, `SnapshotStore`, `Ledger`, compiled resource/retry/timeout policies.
- Produces: `Clock`, `SystemClock`, `FakeClock`, `Lease`, `select_wave()`, and `Scheduler.run_wave()`.

- [ ] **Step 1: Write failing wave and timeout tests**

```python
import asyncio

from graph_engine.runtime.scheduler import FakeClock, select_wave


def test_wave_selects_disjoint_tasks_in_topology_order(task_a, task_b, conflicting_task) -> None:
    selected = select_wave((conflicting_task, task_b, task_a), max_parallel=4)
    assert [task.task_id for task in selected] == [task_a.task_id, task_b.task_id]


def test_timeout_is_typed_failure(scheduler, timed_task) -> None:
    result = asyncio.run(scheduler.run_wave((timed_task,)))
    assert result[0].outcome.failure is not None
    assert result[0].outcome.failure.kind == "timeout"


def test_expired_running_attempt_is_reclaimable(scheduler_factory, running_lease) -> None:
    scheduler = scheduler_factory(clock=FakeClock(running_lease.expires_at + 1))
    assert scheduler.reclaim_expired((running_lease,)) == (running_lease.task_id,)
```

- [ ] **Step 2: Run tests to verify scheduler absence**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_scheduler.py -v`

Expected: collection fails for `runtime.scheduler`.

- [ ] **Step 3: Implement conservative resource conflict selection**

Sort tasks by `(topology_rank, declaration_index, task_id)`. Two tasks conflict
when either has `exclusive`, when their write prefixes overlap, or when one's
write prefix overlaps the other's read prefix. Select the first maximal
non-conflicting prefix up to `max_parallel`. Path-prefix matching is segment
aware: `a/b` covers `a/b/c` but not `a/bb`.

- [ ] **Step 4: Implement leases with injected time**

`Clock.now()` is the only time source. A lease contains task ID, attempt,
owner ID, acquired time, heartbeat time, and expiry. Starting an attempt and its
lease is one ledger batch. Heartbeat appends a lease-heartbeat event. Recovery
may abandon and re-plan only attempts whose persisted expiry is strictly before
the injected current time.

- [ ] **Step 5: Execute handlers concurrently and commit deterministically**

For each selected task:

1. create an attempt workspace from the same baseline head;
2. invoke its handler inside `asyncio.timeout(node.timeout.run_seconds)`;
3. convert `TimeoutError` to `TaskFailure(kind="timeout")`;
4. convert any other uncaught exception to `TaskFailure(kind="internal")` with
   exception class and message but no unserializable traceback object;
5. seal successful candidate workspaces; and
6. return an in-memory `AttemptResult` without touching head or success ledger.

After all selected handlers finish, process `AttemptResult`s in the same stable
task order used for selection. Disjoint successful candidates are rebased onto
the newly advanced head by applying their changed files to a fresh attempt,
then validated and committed. Append each task result and the new head tree ID
in one ledger batch. Completion order must not affect file tree ID or event
order.

- [ ] **Step 6: Run scheduler, workspace, and lease recovery tests**

Run:

```bash
uv run pytest packages/graph-engine/tests/runtime/test_scheduler.py packages/graph-engine/tests/runtime/test_workspace.py -v
uv run ruff check packages/graph-engine/graph_engine/runtime/scheduler.py
uv run pyright packages/graph-engine/graph_engine/runtime/scheduler.py
```

Expected: all pass, including a test that runs two handlers with reversed sleep
durations and obtains identical event and tree digests.

- [ ] **Step 7: Commit scheduling and leases**

```bash
git add packages/graph-engine/graph_engine/runtime packages/graph-engine/tests/runtime
git commit -m "feat(graph-engine): schedule leased resource safe waves"
```

---

### Task 9: Subgraphs, interrupts, and the Engine facade

**Files:**
- Create: `packages/graph-engine/graph_engine/runtime/engine.py`
- Create: `packages/graph-engine/tests/runtime/test_engine.py`
- Modify: `packages/graph-engine/graph_engine/runtime/planner.py`
- Modify: `packages/graph-engine/graph_engine/runtime/events.py`
- Modify: `packages/graph-engine/graph_engine/runtime/models.py`
- Modify: `packages/graph-engine/graph_engine/__init__.py`

**Interfaces:**
- Consumes: `ResolvedProduct`, `Ledger`, `SnapshotStore`, `plan_next()`, `Scheduler`.
- Produces: `Engine`, `InvocationHandle`, `RunResult`, nested graph lifecycle, `Engine.start()`, `Engine.run_until_blocked()`, and `Engine.resume()`.

- [ ] **Step 1: Write failing facade tests**

```python
import pytest

from graph_engine.runtime.engine import Engine


def test_engine_has_no_default_product(tmp_path) -> None:
    engine = Engine(tmp_path)
    with pytest.raises(TypeError, match="product"):
        engine.start(entrypoint="main", invocation_id="missing-product")  # type: ignore[call-arg]


def test_subgraph_completion_returns_to_parent(engine, resolved_subgraph_product) -> None:
    handle = engine.start(resolved_subgraph_product, entrypoint="main", invocation_id="inv-sub")
    result = engine.run_until_blocked(handle)
    assert result.status == "succeeded"
    assert result.output == {"child": "done"}


def test_interrupt_requires_explicit_resume(engine, resolved_interrupt_product) -> None:
    handle = engine.start(resolved_interrupt_product, entrypoint="main", invocation_id="inv-int")
    blocked = engine.run_until_blocked(handle)
    assert blocked.status == "interrupted"
    assert blocked.actions == ("approve", "reject")
    resumed = engine.resume(handle, action="approve", payload={"reviewer": "human"})
    assert engine.run_until_blocked(resumed).status == "succeeded"
```

Build the two resolved-product fixtures in the same file from frozen
`ProductManifest` objects and static plugin providers:
the subgraph fixture has `parent -> child-subgraph -> end`; the interrupt fixture
has `review -> end`. Do not load either fixture from an Assurance resource.

- [ ] **Step 2: Run tests to verify facade absence**

Run: `uv run pytest packages/graph-engine/tests/runtime/test_engine.py -v`

Expected: collection fails for `runtime.engine`.

- [ ] **Step 3: Extend planner with nested graph instances**

A subgraph activation creates `graph_instance_id = canonical_digest({"parent_activation_id":
activation_id, "graph_id": child_graph_id})`, appends `graph_started`, and offers
the child's start token. Child end completion appends `graph_completed`, then
completes the parent subgraph node with the child output and routes its outgoing
edges. The parent cannot complete before the child. Nested interrupts block the
root invocation and retain the exact child graph instance in the event.

- [ ] **Step 4: Implement interrupt and resume semantics**

Activating an interrupt node appends `node_interrupted` containing reason,
actions, activation ID, and JSON input, then returns terminal-for-now status
`interrupted`. `resume()` requires the invocation's one pending interrupt,
requires an action from its frozen action tuple, appends `interrupt_resumed`,
completes that node with `{"action": action, "payload": payload}`, and returns a
new handle. Repeated or mismatched resume commands fail without appending events.

- [ ] **Step 5: Implement the deep Engine interface**

```python
class Engine:
    def __init__(self, root: Path, *, clock: Clock | None = None) -> None: ...
    def start(self, product: ResolvedProduct, *, entrypoint: str, invocation_id: str) -> InvocationHandle: ...
    def open(self, invocation_id: str, product: ResolvedProduct) -> InvocationHandle: ...
    def run_until_blocked(self, handle: InvocationHandle) -> RunResult: ...
    def resume(self, handle: InvocationHandle, *, action: str, payload: JSONValue) -> InvocationHandle: ...
```

`InvocationHandle` is a frozen value containing `invocation_id`,
`invocation_root`, and the resolved-product digest; its `workspace` property
opens the invocation's `SnapshotStore`. `RunResult` contains status, root output,
terminal reason, interrupt actions, and the freshly folded projection.

`start()` creates an invocation directory, snapshot store, and initial atomic
ledger batch containing product digest, root graph start, and start token.
`open()` requires the exact resolved-product digest persisted at start, folds
the authoritative ledger, uses a valid checkpoint only as an optimization, and
reclaims expired leases. `run_until_blocked()` repeatedly commits pure planner
events and scheduler waves until succeeded, failed, stopped, or interrupted.
It enforces each graph's activation bound and never reads a packaged graph.

Keep `Engine.start()` statically unable to omit `product`; do not add a nullable
product or a `start_without_product()` escape hatch.

- [ ] **Step 6: Test crash reopen and nested behavior**

Add a parametrized test that stops after each committed event batch, creates a
fresh `Engine`, calls `open()`, and reaches the same final ledger digest and tree
ID as an uninterrupted run. Include cuts before/after child graph start,
task-attempt start, task success, and interrupt resume.

Run:

```bash
uv run pytest packages/graph-engine/tests/runtime/test_engine.py -v
uv run pytest packages/graph-engine/tests/runtime -v
uv run ruff check packages/graph-engine/graph_engine/runtime
uv run pyright packages/graph-engine/graph_engine/runtime
```

Expected: all pass.

- [ ] **Step 7: Commit the runtime facade**

```bash
git add packages/graph-engine/graph_engine packages/graph-engine/tests/runtime
git commit -m "feat(graph-engine): run nested resumable graph invocations"
```

---

### Task 10: Toy product A — sequential product and isolated wheel entry points

**Files:**
- Create: `examples/graph-engine-toy-a/pyproject.toml`
- Create: `examples/graph-engine-toy-a/graph_engine_toy_a/__init__.py`
- Create: `examples/graph-engine-toy-a/graph_engine_toy_a/plugin.py`
- Create: `examples/graph-engine-toy-a/graph_engine_toy_a/product.py`
- Create: `packages/graph-engine/tests/integration/test_toy_a.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `ProductProvider`, `PluginProvider`, `TaskHandler`, `Engine`.
- Produces: wheel entry points `toy-a` in both `graph_engine.products` and `graph_engine.plugins` and one executable sequential graph.

- [ ] **Step 1: Write the failing toy A integration test**

```python
from graph_engine.product import load_product_entrypoint, resolve_product
from graph_engine.runtime.engine import Engine


def test_toy_a_runs_without_assurance_packages(tmp_path, load_plugin_entrypoint) -> None:
    product = load_product_entrypoint("toy-a")
    plugin = load_plugin_entrypoint("toy-a")
    resolved = resolve_product(product, {"toy.a": plugin})
    engine = Engine(tmp_path / "engine")
    handle = engine.start(resolved, entrypoint="hello", invocation_id="toy-a-1")
    result = engine.run_until_blocked(handle)
    assert result.status == "succeeded"
    assert result.output == {"message": "hello Ada"}
    assert handle.workspace.read_head("greeting.txt") == b"hello Ada\n"
```

- [ ] **Step 2: Run test to verify entrypoint absence**

Run: `uv run pytest packages/graph-engine/tests/integration/test_toy_a.py -v`

Expected: product lookup fails with unknown entrypoint `toy-a`.

- [ ] **Step 3: Create the toy A wheel**

Its `pyproject.toml` depends only on `graph-engine` and contains:

```toml
[project.entry-points."graph_engine.products"]
toy-a = "graph_engine_toy_a.product:ToyAProduct"

[project.entry-points."graph_engine.plugins"]
toy-a = "graph_engine_toy_a.plugin:ToyAPlugin"
```

Add it as a uv workspace member, root dev dependency, and workspace source.

- [ ] **Step 4: Implement the sequential capability and product**

`ToyAPlugin` declares `plugin_id="toy.a"`, exact version `1.0.0`, and handler
`toy.a.greet`. The handler reads `request.input["config"]["name"]`, writes
`greeting.txt`, and returns `{"message": f"hello {name}"}`.

`ToyAProduct.manifest()` returns a workflow with `greet -> done`, one retry,
five-second timeout, write claim `greeting.txt`, and public entrypoint `hello`.
It lists only `PluginRequirement(plugin_id="toy.a", version="1.0.0")`.

- [ ] **Step 5: Add explicit plugin entrypoint loading**

Add `load_plugin_entrypoint(entrypoint_name)` beside product loading. It searches
only `graph_engine.plugins`, rejects duplicates, and does not build or mutate a
global registry. Add unit tests mirroring product duplicate/unknown behavior.

- [ ] **Step 6: Run toy A and isolation checks**

Run:

```bash
uv sync --dev
uv run pytest packages/graph-engine/tests/integration/test_toy_a.py -v
uv run python -c "import graph_engine_toy_a; import graph_engine; assert 'assurance_agent' not in __import__('sys').modules"
```

Expected: all pass.

- [ ] **Step 7: Commit toy A**

```bash
git add pyproject.toml uv.lock examples/graph-engine-toy-a packages/graph-engine
git commit -m "test(graph-engine): add sequential toy product"
```

---

### Task 11: Toy product B — branch, retry, subgraph, join, and interrupt

**Files:**
- Create: `examples/graph-engine-toy-b/pyproject.toml`
- Create: `examples/graph-engine-toy-b/graph_engine_toy_b/__init__.py`
- Create: `examples/graph-engine-toy-b/graph_engine_toy_b/plugin.py`
- Create: `examples/graph-engine-toy-b/graph_engine_toy_b/product.py`
- Create: `packages/graph-engine/tests/integration/test_toy_b.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: the same stable product/plugin/engine interfaces used by toy A.
- Produces: an independently packaged product proving branching, transient retry, disjoint parallel writes, child graphs, all-join, interrupt, resume, and replay.

- [ ] **Step 1: Write the failing toy B integration test**

```python
from graph_engine.product import load_plugin_entrypoint, load_product_entrypoint, resolve_product
from graph_engine.runtime.engine import Engine


def test_toy_b_recovers_then_interrupts_and_resumes(tmp_path) -> None:
    product = load_product_entrypoint("toy-b")
    plugin = load_plugin_entrypoint("toy-b")
    resolved = resolve_product(product, {"toy.b": plugin})
    engine = Engine(tmp_path / "engine")
    handle = engine.start(resolved, entrypoint="review", invocation_id="toy-b-1")
    blocked = engine.run_until_blocked(handle)
    assert blocked.status == "interrupted"
    assert blocked.actions == ("approve", "reject")
    assert blocked.projection.attempts_for("root", "left") == 2
    engine.resume(handle, action="approve", payload={"reviewer": "Ada"})
    completed = engine.run_until_blocked(handle)
    assert completed.status == "succeeded"
    assert handle.workspace.read_head("left.txt") == b"left\n"
    assert handle.workspace.read_head("child.txt") == b"child\n"
```

- [ ] **Step 2: Run test to verify toy B is absent**

Run: `uv run pytest packages/graph-engine/tests/integration/test_toy_b.py -v`

Expected: product lookup fails with unknown entrypoint `toy-b`.

- [ ] **Step 3: Create the independent toy B wheel**

Use the same packaging pattern as toy A but entrypoint name `toy-b`, import
package `graph_engine_toy_b`, and plugin ID `toy.b`. Add it explicitly to the uv
workspace/dev dependencies/sources.

- [ ] **Step 4: Implement materially different handlers and graph**

Handlers:

- `toy.b.seed` returns `{"route": "both"}`;
- `toy.b.left` returns transient failure on attempt 1, then writes `left.txt`;
- `toy.b.child` writes `child.txt`; and
- `toy.b.combine` returns `{"combined": True}`.

Root graph: `seed` routes to `left` and `child-subgraph`; both flow to an
`all` join; join flows to `combine`, then interrupt `review`, then end. Child
graph: `child` then end. Left and child declare disjoint write claims. Left uses
`max_attempts=2` and `retry_on=["transient"]`.

- [ ] **Step 5: Prove deterministic replay and product separation**

Run toy B twice with the same invocation inputs in separate engine roots and
assert identical compiled digest, event-kind/ID sequence, final tree ID, and
terminal output. Assert toy A's resolved registry has no `toy.b.*` IDs and toy
B's has no `toy.a.*` IDs.

- [ ] **Step 6: Run both toy integration suites**

Run:

```bash
uv sync --dev
uv run pytest packages/graph-engine/tests/integration -v
uv run pytest packages/graph-engine/tests/runtime -q
```

Expected: all pass.

- [ ] **Step 7: Commit toy B**

```bash
git add pyproject.toml uv.lock examples/graph-engine-toy-b packages/graph-engine
git commit -m "test(graph-engine): prove independent complex toy product"
```

---

### Task 12: Import firewall, wheel isolation, CLI, and Phase 1 acceptance gate

**Files:**
- Create: `packages/graph-engine/graph_engine/__main__.py`
- Create: `packages/graph-engine/tests/test_cli.py`
- Create: `tests/architecture/test_graph_engine_boundaries.py`
- Create: `scripts/graph_engine_smoke_test.sh`
- Modify: `.importlinter`
- Modify: `.github/workflows/ci.yml`
- Modify: `packages/graph-engine/README.md`

**Interfaces:**
- Consumes: all Phase 1 public interfaces and toy entrypoints.
- Produces: a product-required module CLI, mechanical dependency firewall, isolated-wheel smoke test, and documented Phase 1 acceptance command.

- [ ] **Step 1: Write failing CLI and architecture tests**

```python
import ast
import sys
from pathlib import Path


ALLOWED_ROOTS = set(sys.stdlib_module_names) | {"graph_engine", "pydantic", "yaml"}


def test_graph_engine_imports_no_product_packages() -> None:
    root = Path("packages/graph-engine/graph_engine")
    violations: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".", 1)[0]]
            else:
                continue
            for name in names:
                if name not in ALLOWED_ROOTS:
                    violations.append(f"{path}:{node.lineno}:{name}")
    assert violations == []
```

CLI tests invoke `python -m graph_engine run` without `--product` and require
exit code 2 plus `product is required`; `compile --product toy-a` prints the
product and compiled digests; `run --product toy-a --plugin toy-a --entrypoint
hello --invocation-id smoke --root <tmp>` completes successfully.

- [ ] **Step 2: Run tests to verify CLI and firewall are absent**

Run:

```bash
uv run pytest packages/graph-engine/tests/test_cli.py tests/architecture/test_graph_engine_boundaries.py -v
```

Expected: CLI tests fail because `graph_engine.__main__` is absent.

- [ ] **Step 3: Implement the product-required module CLI**

Use stdlib `argparse`; commands are `compile` and `run`. Both require
`--product`; `run` additionally requires one or more explicit `--plugin`,
`--entrypoint`, `--invocation-id`, and `--root`. The CLI loads only named entry
points, resolves the exact manifest, and emits JSON. It has no default product,
graph, plugin, workspace, or entrypoint.

- [ ] **Step 4: Add the import-linter firewall**

Add `graph_engine`, `graph_engine_toy_a`, and `graph_engine_toy_b` to
`root_packages` and add this contract:

```ini
[importlinter:contract:graph-engine-independent]
name = graph engine must not import products or the old runtime
type = forbidden
source_modules =
    graph_engine
forbidden_modules =
    assurance_agent
    assurance_kernel
    graph_engine_toy_a
    graph_engine_toy_b
```

Keep the AST test because import-linter only sees installed import graphs and
the wheel must remain independent even when a toy is absent.

- [ ] **Step 5: Implement isolated-wheel smoke testing**

`scripts/graph_engine_smoke_test.sh` must:

1. archive committed `HEAD` to a temporary source tree;
2. build `graph-engine`, toy A, and toy B wheels;
3. inspect the engine wheel and assert it contains only `graph_engine/**` plus
   distribution metadata, contains no YAML/JSON/Markdown resources, and contains
   no import package matching Assurance or either toy;
4. create venv A with only `graph-engine`, verify import succeeds and CLI without
   product fails closed;
5. create venv B with engine + toy A, compile and run toy A;
6. create venv C with engine + toy B, run through interrupt/resume using the
   Python interface; and
7. verify neither toy wheel depends on `assurance-agent` or `assurance-kernel`.

Use explicit wheel paths and local `--find-links`; do not access the network.

- [ ] **Step 6: Add CI and focused documentation**

Add `bash scripts/graph_engine_smoke_test.sh` after the existing packaging smoke
test in CI. `packages/graph-engine/README.md` documents the public Engine,
ProductProvider, PluginProvider, and TaskHandler interfaces; lists Phase 1 node
kinds; states there is no default product; and links the architecture spec. It
must not document Phase 2 configuration syntax as if implemented.

- [ ] **Step 7: Run the complete Phase 1 gate**

Run:

```bash
uv run ruff check packages/graph-engine examples/graph-engine-toy-a examples/graph-engine-toy-b tests/architecture/test_graph_engine_boundaries.py
uv run ruff format --check packages/graph-engine examples/graph-engine-toy-a examples/graph-engine-toy-b tests/architecture/test_graph_engine_boundaries.py
uv run pyright packages/graph-engine/graph_engine examples/graph-engine-toy-a examples/graph-engine-toy-b
uv run lint-imports
uv run pytest packages/graph-engine/tests tests/architecture/test_graph_engine_boundaries.py -q
bash scripts/graph_engine_smoke_test.sh
```

Expected: every command passes. Then run the repository-wide gates to detect
workspace regressions:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest -q
bash scripts/packaging_smoke_test.sh
```

Expected: every command passes; unrelated pre-existing dirty files remain
unstaged.

- [ ] **Step 8: Commit Phase 1 acceptance tooling**

```bash
git add .importlinter .github/workflows/ci.yml packages/graph-engine scripts/graph_engine_smoke_test.sh tests/architecture/test_graph_engine_boundaries.py
git commit -m "build(graph-engine): enforce phase one isolation gates"
```

## Phase 1 completion evidence

Before declaring Phase 1 complete, attach these exact outputs to the handoff:

- the twelve task commit IDs in order;
- `uv run pytest packages/graph-engine/tests tests/architecture/test_graph_engine_boundaries.py -q` summary;
- `uv run lint-imports` contract summary;
- `bash scripts/graph_engine_smoke_test.sh` final line;
- repository-wide CI command summaries;
- built engine wheel file list; and
- toy A and toy B terminal status, compiled digest, ledger digest, and final tree ID.

Phase 2 cannot begin until this evidence is present and reviewed.
