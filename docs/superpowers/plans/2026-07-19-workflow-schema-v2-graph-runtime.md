# Workflow Schema v2 GraphRuntime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the v1 phase/loop driver with the incompatible `schema_version: "2"` graph runtime described in [`2026-07-19-workflow-schema-v2-graph-runtime-design.md`](../specs/2026-07-19-workflow-schema-v2-graph-runtime-design.md), including durable retries, resumable interrupts, dynamic fan-out, true parallel execution, and ledger-authoritative recovery.

**Architecture:** Build v2 beside the current v1 implementation until the canonical-schema cutover. The public seam is `GraphRuntime`; schema compilation, planning, scheduling, task workspaces, handlers, ledger projection, and checkpoints remain inside `assurance_agent.workflow.graph`. Agent and CLI operations become handler-registry entries, while every write-capable task executes in a private snapshot and returns a content-addressed write-set for deterministic Update.

**Tech Stack:** Python 3.11+, pydantic v2, PyYAML, Click, `concurrent.futures`, existing AA DSL/gate/progression modules, pytest, ruff, pyright, import-linter, uv. No LangGraph, LangChain, LangSmith, database, or distributed-scheduler dependency.

## Global Constraints

- `schema_version` must be exactly the string `"2"`; v1 schemas and v1 in-flight checkpoints are rejected after cutover.
- `graphs:` is the only topology declaration. `phases:`, `loops:`, `kind: healing`, and `kind: review_fix` do not exist in v2.
- The runtime borrows only LangGraph's Plan → Execute → Update, task, subgraph, pending-write, Send/fan-out, and interrupt concepts; it does not import LangGraph.
- Strict `events.jsonl` graph/task/budget/decision events are authoritative. `driver.json`, checkpoint JSON, workflow state, and `running-tasks.json` are projections or liveness data.
- Same-step sibling state and filesystem writes remain invisible until Update. A successful sibling is never re-executed when another sibling fails.
- All current agent skills and operations are write-capable unless their execution contract explicitly declares `side_effect_free: true`.
- Nodes do not have to declare `resources`; execution contracts supply defaults. Read/read may run together; write/read, write/write, and equal exclusive tokens serialize automatically.
- Unknown resource scope receives `global:exclusive`.
- Task retry attempts and business-loop budget consumption are separate ledger counters. `max_attempts` includes the first attempt; business budget is consumed only by the declared successful task.
- Human review is a resumable graph interrupt bound to audited artifact hashes.
- Eval and benchmark fixtures enter mid-graph only through validated `import-checkpoint`; bare artifact presence never fabricates completed tasks.
- No task may write directly to coordinator-owned ledger, checkpoint, object-store, lease, or canonical-workspace paths.
- Each task below ends with its focused pytest command plus `uv run ruff check .` and `uv run pyright`. Tasks changing imports also run `uv run lint-imports`.
- Use conventional commits and stage only files listed by the task. Preserve user-owned `.history/` and unrelated worktree changes.

---

## File and Module Map

### New runtime package

| File | Responsibility |
|---|---|
| `assurance_agent/workflow/graph/__init__.py` | Export only `GraphRuntime` and public command/result models |
| `assurance_agent/workflow/graph/agent_api.py` | Graph-owned agent request/result/invoker protocol used by handlers and driver adapters |
| `assurance_agent/workflow/graph/schema_v2.py` | Pydantic v2 schema models and YAML loading |
| `assurance_agent/workflow/graph/models.py` | Compiled graph, task, result, status, checkpoint, and runtime-context models |
| `assurance_agent/workflow/graph/compiler.py` | Static validation, SCC checks, route analysis, resource summaries, canonical digests |
| `assurance_agent/workflow/graph/contracts.py` | Execution-contract registry, resource-claim normalization and conflict checks |
| `assurance_agent/workflow/graph/checkpoint.py` | Ledger projection, checkpoint snapshots, schema/contract pinning, import projection |
| `assurance_agent/workflow/graph/workspace.py` | Private task snapshots, Merkle objects, write-set validation/merge/materialization |
| `assurance_agent/workflow/graph/planner.py` | Node activation, route freezing, joins, reducers, fan-out, ready-task planning |
| `assurance_agent/workflow/graph/leases.py` | Atomic `running-tasks.json`, heartbeat, adopt/abandon decisions |
| `assurance_agent/workflow/graph/scheduler.py` | Deterministic waves, thread-based parallelism, retry/backoff, sibling settling |
| `assurance_agent/workflow/graph/task_runner.py` | Handler registry and the internal `NodeRunner` seam |
| `assurance_agent/workflow/graph/runtime.py` | Single public GraphRuntime, recursive subgraph orchestration, run/resume/status/import |
| `assurance_agent/workflow/graph/handlers/agent.py` | Agent adapter bridge target handler |
| `assurance_agent/workflow/graph/handlers/operation.py` | AA domain-operation target handler |
| `assurance_agent/workflow/graph/handlers/gate.py` | Attached and builtin gate handlers |
| `assurance_agent/workflow/graph/handlers/join.py` | Builtin join result handler |
| `assurance_agent/workflow/graph/handlers/interrupt.py` | Interrupt publication and audited resume validation |
| `assurance_agent/workflow/graph/handlers/subgraph.py` | Named-subgraph child invocation handler |

### New core support

| File | Responsibility |
|---|---|
| `assurance_agent/workflow/core/graph_types.py` | Hold shared graph error literals without creating an upward core → graph import |
| `assurance_agent/workflow/core/graph_events.py` | Hold strict v2 graph event models below the graph package |

### New packaged resources and tests

| File | Responsibility |
|---|---|
| `assurance_agent/_resources/schemas/execution-contracts.yaml` | Default reads/writes/exclusive/retry capabilities for every canonical target |
| `tests/fixtures/workflow-v2-minimal.yaml` | Small valid graph used across compiler/runtime tests |
| `tests/unit/workflow/graph/` | Focused unit tests mirroring each new runtime module |
| `tests/integration/test_graph_runtime.py` | Real filesystem/ledger run, failure, resume, interrupt and subgraph tests |
| `tests/integration/test_cli_workflow_v2.py` | v2 CLI entrypoint/status/resume/import tests |

### Existing files changed at cutover

| File | Change |
|---|---|
| `assurance_agent/_resources/schemas/workflow-schema.yaml` | Replace v1 phases/loops with the canonical v2 graphs |
| `assurance_agent/workflow/core/events.py` | Add graph audit-event union members |
| `assurance_agent/workflow/core/progression.py` | Add coordinator-only root-qualified pointer/file staging needed by Update |
| `assurance_agent/workflow/orchestration/dsl.py` | Add deterministic `node()` lookup and preserve three-valued semantics |
| `assurance_agent/workflow/orchestration/schema.py` | Retain shared gate models; remove v1 phase/loop loader after cutover |
| `assurance_agent/workflow/orchestration/gates.py` | Evaluate against explicit artifact views and frozen graph node outcomes |
| `assurance_agent/workflow/driver/adapter.py` | Temporarily re-export graph-owned agent invocation types for v1 callers |
| `assurance_agent/workflow/driver/loop.py` | Reduce to a compatibility wrapper around GraphRuntime, then remove executor seams |
| `assurance_agent/workflow/driver/driver_state.py` | Store invocation/checkpoint pointers instead of authoritative iteration/current phase |
| `assurance_agent/workflow/driver/workflow_start.py` | Replace `scope` with `entrypoint` in detached launch |
| `assurance_agent/commands/workflow_cmd.py` | Expose run/status/resume/import-checkpoint commands |
| `assurance_agent/commands/status_cmd.py` | Project `GraphStatus`; remove v1 phase computation |
| `assurance_agent/commands/gate_cmd.py` | Resolve v2 structural node paths and frozen gate results |
| `assurance_agent/commands/state_cmd.py` | Remove v1 phase/healing progression commands after skill migration |
| `assurance_agent/commands/decide_cmd.py` | Keep non-graph policy decisions; route graph human actions through resume |
| `assurance_agent/eval/executor.py`, `assurance_agent/eval/fixtures.py` | Use GraphRuntime and explicit import manifests |
| `benchmark/vue-fastapi-admin/benchmark/*.sh` | Use entrypoints/resume and remove stale-dispatch pruning |
| `assurance_agent/_resources/skills/**`, `README.md`, `docs/eval.md` | Replace v1 status/apply/gate/decision instructions |

---

### Task 1: Add strict v2 schema models and parser

**Files:**
- Create: `assurance_agent/workflow/core/graph_types.py`
- Create: `assurance_agent/workflow/graph/__init__.py`
- Create: `assurance_agent/workflow/graph/schema_v2.py`
- Create: `tests/unit/workflow/graph/__init__.py`
- Create: `tests/unit/workflow/graph/test_schema_v2.py`
- Create: `tests/fixtures/workflow-v2-minimal.yaml`
- Modify: `assurance_agent/workflow/orchestration/schema.py`

**Interfaces:**
- Consumes: `GateDef`, `ReadEntry`, `Verdict`, and gate normalization from `workflow.orchestration.schema`.
- Produces: `parse_workflow_v2(text: str) -> WorkflowSchemaV2`; `load_workflow_v2(project_root: Path, explicit: Path | None = None) -> WorkflowSchemaV2`; immutable schema models named in this task.

- [ ] **Step 1: Expose shared gate normalization without changing v1 behavior**

Add this public helper around the existing `_normalize_gate` implementation:

```python
def normalize_gates(raw: object) -> dict[str, GateDef]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SchemaError("gates must be a mapping")
    normalized: dict[str, GateDef] = {}
    for gate_id, gate_raw in raw.items():
        if gate_raw is not None and not isinstance(gate_raw, dict):
            raise SchemaError(f"gate {gate_id!r} must be a mapping")
        normalized[str(gate_id)] = _normalize_gate(str(gate_id), gate_raw or {})
    return normalized
```

Change `parse_schema` to call `normalize_gates(doc.get("gates"))`. Run:

```bash
uv run pytest tests/unit/test_schema.py tests/unit/test_gates.py -v
```

Expected: all existing v1 schema and gate tests pass unchanged.

- [ ] **Step 2: Write parser tests for the exact v2 top-level shape**

Create `test_schema_v2.py` with these first assertions:

```python
from pathlib import Path

import pytest

from assurance_agent.workflow.graph.schema_v2 import (
    SchemaV2Error,
    load_workflow_v2,
    parse_workflow_v2,
)


def test_minimal_v2_schema_loads() -> None:
    schema = parse_workflow_v2(
        Path("tests/fixtures/workflow-v2-minimal.yaml").read_text(encoding="utf-8")
    )
    assert schema.schema_version == "2"
    assert schema.entrypoints["full"].graph == "main"
    assert list(schema.graphs["main"].nodes) == ["first"]
    assert schema.policies.retry["never"].max_attempts == 1


@pytest.mark.parametrize("version", ["1", "2.0", 2, ""])
def test_only_exact_string_version_two_is_accepted(version: object) -> None:
    text = f"schema_version: {version!r}\nname: bad\nentrypoints: {{}}\ngraphs: {{}}\n"
    with pytest.raises(SchemaV2Error, match='schema_version must be exactly "2"'):
        parse_workflow_v2(text)


def test_v1_phase_and_loop_keys_are_rejected() -> None:
    with pytest.raises(SchemaV2Error, match="phases|loops"):
        parse_workflow_v2(
            'schema_version: "2"\nname: bad\nphases: []\nloops: {}\nentrypoints: {}\ngraphs: {}\n'
        )
```

Run: `uv run pytest tests/unit/workflow/graph/test_schema_v2.py -v`

Expected: collection fails because `workflow.graph.schema_v2` does not exist.

- [ ] **Step 3: Create the complete schema model vocabulary**

Create `workflow/core/graph_types.py` first, so both strict core events and graph models import the same downward-safe type:

```python
ErrorKind = Literal[
    "timeout", "transport", "rate_limit", "auth", "invalid_input",
    "invalid_output", "forbidden_write", "contract", "internal",
]
```

Implement the following pydantic models with `ConfigDict(extra="forbid", frozen=True)` on every schema model, importing `ErrorKind` from `workflow.core.graph_types`:

```python

class ParamDef(_FrozenModel):
    type: Literal["enum", "list", "bool", "int", "str"]
    values: list[object] | None = None
    min_items: int | None = Field(default=None, ge=0)
    unique: bool = False
    default: object = None

class EntrypointDef(_FrozenModel):
    graph: str
    allow: str | None = None
    with_: dict[str, object] = Field(default_factory=dict, alias="with")

class BackoffDef(_FrozenModel):
    initial_seconds: float = Field(default=0, ge=0)
    multiplier: float = Field(default=1, ge=1)
    max_seconds: float = Field(default=0, ge=0)
    jitter: bool = False

class RetryPolicyDef(_FrozenModel):
    max_attempts: int = Field(ge=1, le=10)
    retry_on: list[ErrorKind] = Field(default_factory=list)
    backoff: BackoffDef = Field(default_factory=BackoffDef)

class TimeoutPolicyDef(_FrozenModel):
    run_seconds: float = Field(gt=0)
    heartbeat_seconds: float = Field(gt=0)

class SchedulerPolicyDef(_FrozenModel):
    max_parallel_tasks: int = Field(default=4, ge=1, le=64)
    conflict_order: list[Literal["topology", "declaration", "task_id"]] = Field(
        default_factory=lambda: ["topology", "declaration", "task_id"]
    )

class PoliciesDef(_FrozenModel):
    retry: dict[str, RetryPolicyDef] = Field(default_factory=dict)
    timeout: dict[str, TimeoutPolicyDef] = Field(default_factory=dict)
    scheduler: SchedulerPolicyDef = Field(default_factory=SchedulerPolicyDef)

class StateDef(_FrozenModel):
    type: Literal["list", "object", "str", "int", "bool"]
    default: object
    reducer: Literal["replace", "append", "merge_disjoint", "set_union"] = "replace"

class ResourceDef(_FrozenModel):
    reads: list[str] = Field(default_factory=list)
    writes: list[str] = Field(default_factory=list)
    exclusive: list[str] = Field(default_factory=list)

class JoinDef(_FrozenModel):
    sources: list[str]
    mode: Literal["all", "all_active", "any"]
    cancel_remaining: bool = False

class ReduceDef(_FrozenModel):
    into: str
    using: Literal["replace", "append", "merge_disjoint", "set_union"]

class FanOutDef(_FrozenModel):
    items: str
    item_as: str
    key: str
    max_items: int = Field(default=32, ge=1, le=128)
    completion: Literal["all"] = "all"
    reduce: ReduceDef | None = None

class BudgetUseDef(_FrozenModel):
    consume: str
    on: Literal["committed"]
    exhausted_to: str

class InterruptDef(_FrozenModel):
    reason: str
    checkpoint: str
    bind: Literal["audited_gate_read"]
    actions: list[Literal["fix_and_proceed", "accept_risk", "stop"]]

class NodeDef(_FrozenModel):
    uses: str
    agent: str | None = None
    when: str | None = None
    outputs: list[str] = Field(default_factory=list)
    gate: str | None = None
    retry: str | None = None
    timeout: str | None = None
    with_: dict[str, object] = Field(default_factory=dict, alias="with")
    resources: ResourceDef | None = None
    state_writes: dict[str, str] = Field(default_factory=dict)
    join: JoinDef | None = None
    fan_out: FanOutDef | None = None
    budget: BudgetUseDef | None = None
    interrupt: InterruptDef | None = None

class EdgeDef(_FrozenModel):
    from_: str = Field(alias="from")
    to: str
    when: str | None = None

class RouteDef(_FrozenModel):
    from_: str = Field(alias="from")
    select: str
    cases: dict[str, str]
    default: str | None = None

class BudgetDef(_FrozenModel):
    limit: str | int

class GraphDef(_FrozenModel):
    max_supersteps: int = Field(gt=0)
    state: dict[str, StateDef] = Field(default_factory=dict)
    budgets: dict[str, BudgetDef] = Field(default_factory=dict)
    nodes: dict[str, NodeDef]
    edges: list[EdgeDef] = Field(default_factory=list)
    routes: list[RouteDef] = Field(default_factory=list)

class WorkflowSchemaV2(_FrozenModel):
    schema_version: Literal["2"]
    name: str
    params: dict[str, ParamDef] = Field(default_factory=dict)
    entrypoints: dict[str, EntrypointDef]
    policies: PoliciesDef = Field(default_factory=PoliciesDef)
    graphs: dict[str, GraphDef]
    gates: dict[str, GateDef] = Field(default_factory=dict)
```

Normalize gates before `WorkflowSchemaV2.model_validate`, and reject any root key outside `schema_version`, `name`, `params`, `entrypoints`, `policies`, `graphs`, and `gates`. Before pydantic validation, require `isinstance(doc.get("schema_version"), str)` and `doc["schema_version"] == "2"`; this prevents YAML integer coercion from weakening the incompatible-version boundary.

- [ ] **Step 4: Add the minimal fixture and loader precedence**

Write `tests/fixtures/workflow-v2-minimal.yaml` exactly as:

```yaml
schema_version: "2"
name: minimal
params:
  run_mode: {type: enum, values: [full], default: full}
entrypoints:
  full: {graph: main, allow: "params.run_mode == 'full'"}
policies:
  retry:
    never: {max_attempts: 1, retry_on: []}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 10}
  scheduler: {max_parallel_tasks: 2}
graphs:
  main:
    max_supersteps: 5
    nodes:
      first:
        uses: operation:no-op
        retry: never
        timeout: local
    edges:
      - {from: START, to: first}
      - {from: first, to: END}
gates: {}
```

`load_workflow_v2` must use explicit path, then `.aa/workflow-schema.yaml`, then `schemas/workflow-schema.yaml`, then the packaged resource, matching current precedence. Add a temp-project test proving explicit path wins.

- [ ] **Step 5: Verify and commit Task 1**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_schema_v2.py tests/unit/test_schema.py tests/unit/test_gates.py -v
uv run ruff check .
uv run pyright
```

Expected: all commands exit 0.

Commit:

```bash
git add assurance_agent/workflow/core/graph_types.py assurance_agent/workflow/graph/__init__.py assurance_agent/workflow/graph/schema_v2.py assurance_agent/workflow/orchestration/schema.py tests/unit/workflow/graph tests/fixtures/workflow-v2-minimal.yaml
git commit -m "feat(workflow): add strict schema v2 models"
```

---

### Task 2: Compile graph topology, expressions, cycles, routes, and digests

**Files:**
- Create: `assurance_agent/workflow/graph/models.py`
- Create: `assurance_agent/workflow/graph/compiler.py`
- Create: `tests/unit/workflow/graph/test_compiler.py`
- Modify: `assurance_agent/workflow/orchestration/dsl.py`
- Modify: `tests/unit/test_dsl_parse.py`
- Modify: `tests/unit/test_dsl_eval.py`

**Interfaces:**
- Consumes: `WorkflowSchemaV2`, `GraphDef`, current AA DSL parser/evaluator.
- Produces: `compile_workflow(schema: WorkflowSchemaV2) -> CompiledWorkflow`; `canonical_digest(value: BaseModel | Mapping[str, object]) -> str`; `Scope.node_result: NodeResolver | None` support.

- [ ] **Step 1: Add failing DSL tests for frozen node outcomes**

Add:

```python
def test_node_result_builtin() -> None:
    assert (
        ev(
            "node('review').gate.verdict == 'pass'",
            {},
            node_result=lambda node_id: {"gate": {"verdict": "pass"}} if node_id == "review" else {},
        )
        is True
    )


def test_node_result_without_resolver_fails_closed() -> None:
    with pytest.raises(DslError, match="no resolver"):
        ev("node('review').value == true", {})
```

Also assert `parse_expression("node('x').gate.verdict")` produces a `Call` below two `Member` nodes and `BUILTIN_ARITY["node"] == 1`.

Run: `uv run pytest tests/unit/test_dsl_parse.py tests/unit/test_dsl_eval.py -v`

Expected: FAIL because `node` is not in the function allowlist.

- [ ] **Step 2: Implement `node()` without weakening the DSL**

Add `NodeResolver = Callable[[str], object]`, a `node_result` keyword to `Scope`, preserve it in `Scope.child`, and add this evaluator branch:

```python
if callee == "node":
    node_id = evaluate(expr.args[0], scope)
    if not isinstance(node_id, str):
        return MISSING
    if scope.node_result is None:
        raise DslError("node() called but no resolver was provided")
    return scope.node_result(node_id)
```

Do not add attribute calls, arithmetic, comprehensions, lambdas, or dynamic subscripts.

- [ ] **Step 3: Write compiler tests before compiler models**

Create tests covering all structural invariants:

```python
def compile_text(text: str):
    return compile_workflow(parse_workflow_v2(text))


def test_compiles_minimal_fixture_with_stable_digest() -> None:
    text = Path("tests/fixtures/workflow-v2-minimal.yaml").read_text(encoding="utf-8")
    first = compile_text(text)
    second = compile_text(text)
    assert first.digest == second.digest
    assert first.entrypoints["full"].graph_id == "main"
    assert first.graphs["main"].declaration_order == ("first",)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda d: d["entrypoints"].update({"bad": {"graph": "missing"}}), "unknown graph"),
        (lambda d: d["graphs"]["main"]["edges"].append({"from": "missing", "to": "END"}), "unknown node"),
        (lambda d: d["graphs"]["main"]["nodes"].update({"dead": {"uses": "operation:no-op"}}), "unreachable"),
    ],
)
def test_rejects_invalid_references(mutation, message: str) -> None:
    raw = yaml.safe_load(Path("tests/fixtures/workflow-v2-minimal.yaml").read_text())
    mutation(raw)
    with pytest.raises(CompileError, match=message):
        compile_text(yaml.safe_dump(raw, sort_keys=False))
```

Add dedicated tests for subgraph recursion, a cyclic SCC without a finite budget consumer, missing `exhausted_to`, non-exhaustive interrupt action route, bad retry/timeout refs, bad gate refs, `replace` multi-writer, unsafe node/path identifiers, and an expression referencing an unknown `node('id')`.

Run: `uv run pytest tests/unit/workflow/graph/test_compiler.py -v`

Expected: FAIL because compiler symbols do not exist.

- [ ] **Step 4: Add immutable compiled models**

Define these exact public shapes in `models.py`:

```python
class CompiledNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    graph_id: str
    node_id: str
    declaration_index: int
    topology_rank: int
    definition: NodeDef
    incoming: tuple[EdgeDef, ...]
    outgoing: tuple[EdgeDef, ...]
    routes: tuple[RouteDef, ...]


class CompiledGraph(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    graph_id: str
    max_supersteps: int
    declaration_order: tuple[str, ...]
    nodes: dict[str, CompiledNode]
    sccs: tuple[tuple[str, ...], ...]
    artifact_symbols: dict[str, str]


class CompiledEntrypoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    graph_id: str
    allow_expr: Expr | None
    param_overrides: dict[str, object]


class CompiledWorkflow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    schema: WorkflowSchemaV2
    digest: str
    entrypoints: dict[str, CompiledEntrypoint]
    graphs: dict[str, CompiledGraph]
    contract_digests: dict[str, str] = Field(default_factory=dict)
```

Use tuples in compiled topology so callers cannot mutate execution order after digesting.

For every JSON output, derive its expression symbol from the filename stem (`change:explore/advisory.json` → `advisory`). Reject duplicate stems that could be visible in the same graph/subgraph scope. Store the resulting symbol → logical-path map in `CompiledGraph.artifact_symbols`; expression validation permits only params, state keys, these artifact symbols, and declared `node('id')` results.

- [ ] **Step 5: Implement compiler validation as independent collectors**

Implement `compile_workflow` as:

```python
def compile_workflow(
    schema: WorkflowSchemaV2,
) -> CompiledWorkflow:
    errors: list[str] = []
    errors.extend(_validate_params_and_entrypoints(schema))
    errors.extend(_validate_graph_refs(schema))
    errors.extend(_validate_expressions(schema))
    errors.extend(_validate_routes_and_interrupts(schema))
    errors.extend(_validate_state_writers(schema))
    errors.extend(_validate_subgraph_recursion(schema))
    errors.extend(_validate_bounded_sccs(schema))
    if errors:
        raise CompileError("workflow v2 compile failed:\n  - " + "\n  - ".join(errors))
    graphs = {graph_id: _compile_graph(graph_id, graph) for graph_id, graph in schema.graphs.items()}
    canonical = schema.model_dump(mode="json", by_alias=True, exclude_none=True)
    return CompiledWorkflow(
        schema=schema,
        digest=canonical_digest(canonical),
        entrypoints=_compile_entrypoints(schema),
        graphs=graphs,
        contract_digests={},
    )
```

Use Tarjan's algorithm for SCCs. A cyclic SCC is valid only when it contains a node with `budget.consume`, the referenced graph budget exists and has a finite int/int-param limit, and `exhausted_to` leaves the SCC. Compute `topology_rank` on the SCC condensation DAG, then declaration index, then node ID.

- [ ] **Step 6: Canonicalize digests and cross-parameter validation**

Serialize with `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` and SHA-256. Validate default params at compile time and invocation overrides later. Add a reusable:

```python
def resolve_params(schema: WorkflowSchemaV2, overrides: Mapping[str, object]) -> dict[str, object]:
    unknown = sorted(set(overrides) - set(schema.params))
    if unknown:
        raise CompileError(f"unknown params: {', '.join(unknown)}")
    resolved = {name: definition.default for name, definition in schema.params.items()}
    resolved.update(overrides)
    _validate_param_values(schema.params, resolved)
    mode = resolved.get("run_mode")
    test_types = resolved.get("test_types")
    if mode == "api-only" and isinstance(test_types, list) and "api" not in test_types:
        raise CompileError("api-only requires test_types to contain api")
    if mode == "e2e-only" and isinstance(test_types, list) and "e2e" not in test_types:
        raise CompileError("e2e-only requires test_types to contain e2e")
    return resolved
```

- [ ] **Step 7: Verify and commit Task 2**

Run:

```bash
uv run pytest tests/unit/test_dsl_parse.py tests/unit/test_dsl_eval.py tests/unit/workflow/graph/test_compiler.py -v
uv run ruff check .
uv run pyright
```

Expected: all commands exit 0.

Commit:

```bash
git add assurance_agent/workflow/graph/models.py assurance_agent/workflow/graph/compiler.py assurance_agent/workflow/orchestration/dsl.py tests/unit/test_dsl_parse.py tests/unit/test_dsl_eval.py tests/unit/workflow/graph/test_compiler.py
git commit -m "feat(workflow): compile schema v2 graphs"
```

---

### Task 3: Add execution contracts and deterministic resource conflicts

**Files:**
- Create: `assurance_agent/workflow/graph/contracts.py`
- Create: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Create: `tests/unit/workflow/graph/test_contracts.py`
- Modify: `assurance_agent/workflow/graph/compiler.py`
- Modify: `assurance_agent/workflow/graph/models.py`

**Interfaces:**
- Consumes: compiled nodes and packaged resources.
- Produces: `load_execution_contracts(project_root: Path, explicit: Path | None = None) -> ExecutionContractCatalog`; `claims_conflict(left: ResourceClaims, right: ResourceClaims) -> bool`; `compile_workflow(schema: WorkflowSchemaV2, contracts: ExecutionContractCatalog | None = None) -> CompiledWorkflow`; compiled transitive subgraph footprints.

- [ ] **Step 1: Write resource normalization and conflict tests**

Create:

```python
def test_read_read_does_not_conflict() -> None:
    left = ResourceClaims(reads=(ResourcePath.parse("repo:tests/api/**"),))
    right = ResourceClaims(reads=(ResourcePath.parse("repo:tests/api/test_users.py"),))
    assert claims_conflict(left, right) is False


def test_write_read_and_write_write_conflict() -> None:
    writer = ResourceClaims(writes=(ResourcePath.parse("repo:tests/api/**"),))
    reader = ResourceClaims(reads=(ResourcePath.parse("repo:tests/api/test_users.py"),))
    other_writer = ResourceClaims(writes=(ResourcePath.parse("repo:tests/api/test_roles.py"),))
    assert claims_conflict(writer, reader) is True
    assert claims_conflict(writer, other_writer) is True


def test_disjoint_api_and_e2e_writes_do_not_conflict() -> None:
    api = ResourceClaims(writes=(ResourcePath.parse("repo:tests/api/**"),))
    e2e = ResourceClaims(writes=(ResourcePath.parse("repo:tests/e2e/**"),))
    assert claims_conflict(api, e2e) is False


def test_equal_exclusive_token_conflicts() -> None:
    left = ResourceClaims(exclusive=("repo:test-runtime",))
    right = ResourceClaims(exclusive=("repo:test-runtime",))
    assert claims_conflict(left, right) is True
```

Add path tests rejecting absolute paths, backslashes, `..`, unknown roots, and unsafe glob segments.

Run: `uv run pytest tests/unit/workflow/graph/test_contracts.py -v`

Expected: FAIL because contract types do not exist.

- [ ] **Step 2: Implement exact contract models and composition**

```python
RootName = Literal["change", "project", "repo", "global"]

@dataclass(frozen=True)
class ResourcePath:
    root: RootName
    pattern: str

    @classmethod
    def parse(cls, value: str) -> "ResourcePath":
        root, separator, pattern = value.partition(":")
        if not separator or root not in {"change", "project", "repo", "global"}:
            raise ContractError(f"invalid resource root: {value}")
        _assert_safe_pattern(pattern)
        return cls(root=cast(RootName, root), pattern=pattern)


@dataclass(frozen=True)
class ResourceClaims:
    reads: tuple[ResourcePath, ...] = ()
    writes: tuple[ResourcePath, ...] = ()
    exclusive: tuple[str, ...] = ()
    authorization_writes: tuple[ResourcePath, ...] = ()


class ExecutionContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target: str
    handler: Literal["agent", "operation", "builtin"]
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    exclusive: tuple[str, ...] = ()
    authorization_writes: tuple[str, ...] = ()
    retryable_errors: tuple[ErrorKind, ...] = ()
    side_effect_free: bool = False
    reconnect: bool = False
```

`ExecutionContractCatalog.claims_for(node)` composes registry defaults, output-derived writes, node-added concurrency claims, and narrowed authorization. If the target is missing or the resulting scope is incomplete, return `exclusive=("global:exclusive",)` rather than guessing.

- [ ] **Step 3: Implement path intersection conservatively**

Split normalized patterns into path segments. Literal segments must match exactly; `*` intersects one segment; `**` intersects any remaining suffix. Different roots never intersect except `global`, which intersects everything. A write conflicts with the other side's reads or writes; reads never conflict with reads; equal exclusive tokens conflict.

Add property-style parametrized cases for parent directories, file-vs-glob, `**`, and `global:exclusive`. Do not use `fnmatch` alone because it does not provide a sound glob-intersection test.

- [ ] **Step 4: Add a minimal packaged registry and compiler target checks**

Start the packaged YAML with builtins and the minimal fixture operation:

```yaml
schema_version: "1"
contracts:
  operation:no-op:
    handler: operation
    side_effect_free: true
  builtin:join:
    handler: builtin
    side_effect_free: true
  builtin:gate:
    handler: builtin
    side_effect_free: true
  builtin:interrupt:
    handler: builtin
    side_effect_free: true
```

The loader accepts project-local `.aa/execution-contracts.yaml` before the packaged resource. Compiler validation must reject an unknown `uses`, an agent node whose target is not `skill:*`, a `graph:*` target missing from `graphs`, a builtin with the wrong detail block, node resources that expand `authorization_writes`, and a node retry policy containing an error kind absent from its target contract's `retryable_errors`.

In this task, extend Task 2's compiler signature to `compile_workflow(schema, contracts: ExecutionContractCatalog | None = None)`. Contract-aware target validation and `contract_digests` population occur only when a catalog is supplied; topology-only unit tests may continue compiling without one.

- [ ] **Step 5: Compute conservative subgraph footprints**

For each `graph:<id>` node, union every reachable child node's reads, writes, and exclusive tokens after recursively expanding named subgraphs. Mutually exclusive runtime conditions are not used to remove claims in v2's first implementation; conservative serialization is correct. Store the result in `CompiledGraph.resource_footprint` and include each referenced contract digest in `CompiledWorkflow.contract_digests`.

- [ ] **Step 6: Verify and commit Task 3**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_contracts.py tests/unit/workflow/graph/test_compiler.py -v
uv run ruff check .
uv run pyright
uv run lint-imports
```

Expected: all commands exit 0.

Commit:

```bash
git add assurance_agent/workflow/graph/contracts.py assurance_agent/workflow/graph/compiler.py assurance_agent/workflow/graph/models.py assurance_agent/_resources/schemas/execution-contracts.yaml tests/unit/workflow/graph/test_contracts.py
git commit -m "feat(workflow): add execution contract registry"
```

---

### Task 4: Add strict graph events, ledger projection, and checkpoint snapshots

**Files:**
- Create: `assurance_agent/workflow/core/graph_events.py`
- Create: `assurance_agent/workflow/graph/checkpoint.py`
- Create: `tests/unit/workflow/graph/test_checkpoint.py`
- Modify: `assurance_agent/workflow/core/events.py`
- Modify: `assurance_agent/workflow/core/progression.py`
- Modify: `tests/unit/test_events.py`
- Modify: `tests/unit/core/test_progression.py`

**Interfaces:**
- Consumes: `Ledger`, `transaction`, `CompiledWorkflow` digests.
- Produces: strict graph event models; `read_events_strict(change_dir: Path) -> list[dict[str, object]]`; `project_invocation(change_dir: Path, invocation_id: str) -> GraphProjection`; `project_workflow_state(projection: GraphProjection) -> WorkflowStateProjection`; `ProgressionTxn.set_workflow_state_projection(content: bytes | str) -> None`; `CheckpointStore.read_latest(invocation_id: str) -> GraphProjection`; `CheckpointStore.write(projection: GraphProjection) -> Path`.

- [ ] **Step 1: Write strict-ledger tests before adding graph events**

Add to `tests/unit/test_events.py`:

```python
def test_read_events_strict_rejects_bad_json_and_sequence_gap(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    (change / "events.jsonl").write_text(
        '{"seq":1,"ts":"x","source":"graph","type":"graph_invocation_started",'
        '"invocation_id":"i","entrypoint":"full","graph_id":"main",'
        '"graph_digest":"d","contract_digests":{},"params":{},'
        '"params_sha256":"p","root_tree_id":"t","max_parallel_tasks":2,'
        '"checkpoint_ns":"i","structural_path":"main"}\n'
        '{bad}\n',
        encoding="utf-8",
    )
    with pytest.raises(LedgerIntegrityError, match="line 2"):
        read_events_strict(change)


def test_graph_event_requires_declared_fields(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    with pytest.raises(EventWriteError):
        append_event_strict(
            change,
            {"source": "graph", "type": "task_attempt_started", "task_id": "missing-fields"},
        )
```

Run: `uv run pytest tests/unit/test_events.py -v`

Expected: FAIL because `LedgerIntegrityError`, `read_events_strict`, and graph audit types do not exist.

- [ ] **Step 2: Define every strict v2 event as a discriminated pydantic model**

Create `graph_events.py` below the graph package so core does not import upward. Use one frozen, `extra="forbid"` base and these required payloads:

```python
class GraphInvocationStartedEvent(_GraphEvent):
    type: Literal["graph_invocation_started"]
    invocation_id: str
    entrypoint: str
    graph_id: str
    graph_digest: str
    contract_digests: dict[str, str]
    params: dict[str, object]
    params_sha256: str
    root_tree_id: str
    max_parallel_tasks: int
    checkpoint_ns: str
    parent_invocation_id: str | None = None
    parent_task_id: str | None = None
    structural_path: str

class NodeActivatedEvent(_GraphEvent):
    type: Literal["node_activated"]
    invocation_id: str
    checkpoint_ns: str
    graph_id: str
    node_id: str
    activation_id: str
    input_sha256: str
    source_reads_sha256: dict[str, str]

class NodeSkippedEvent(_GraphEvent):
    type: Literal["node_skipped"]
    invocation_id: str
    checkpoint_ns: str
    graph_id: str
    node_id: str
    expression: str
    input_sha256: str
    source_reads_sha256: dict[str, str]

class FanOutExpandedEvent(_GraphEvent):
    type: Literal["fan_out_expanded"]
    invocation_id: str
    checkpoint_ns: str
    graph_id: str
    node_id: str
    source_reads_sha256: dict[str, str]
    items: list[object]
    task_keys: list[str]
    task_ids: list[str]

class SuperstepPlannedEvent(_GraphEvent):
    type: Literal["superstep_planned"]
    invocation_id: str
    checkpoint_ns: str
    superstep_id: str
    checkpoint_id: str
    task_ids: list[str]

class TaskAttemptStartedEvent(_GraphEvent):
    type: Literal["task_attempt_started"]
    invocation_id: str
    checkpoint_ns: str
    superstep_id: str
    task_id: str
    attempt_id: str
    node_id: str
    input_sha256: str
    graph_digest: str
    contract_digest: str
    attempt_number: int
    lease_expires_at: str
    started_at: str

class TaskAttemptSucceededEvent(_GraphEvent):
    type: Literal["task_attempt_succeeded"]
    invocation_id: str
    checkpoint_ns: str
    superstep_id: str
    task_id: str
    attempt_id: str
    write_set_id: str | None
    outputs_sha256: dict[str, str]
    gate_report: dict[str, object] | None
    state_updates: dict[str, object]
    value: object = None

class TaskAttemptFailedEvent(_GraphEvent):
    type: Literal["task_attempt_failed"]
    invocation_id: str
    checkpoint_ns: str
    superstep_id: str
    task_id: str
    attempt_id: str
    error_kind: ErrorKind
    message: str
    next_retry_at: str | None

class TaskAttemptAbandonedEvent(_GraphEvent):
    type: Literal["task_attempt_abandoned"]
    invocation_id: str
    checkpoint_ns: str
    task_id: str
    attempt_id: str
    reason: str
    abandoned_at: str

class BudgetConsumedEvent(_GraphEvent):
    type: Literal["budget_consumed"]
    invocation_id: str
    checkpoint_ns: str
    graph_id: str
    budget_id: str
    consumption_id: str
    task_id: str

class GraphInterruptedEvent(_GraphEvent):
    type: Literal["graph_interrupted"]
    invocation_id: str
    checkpoint_ns: str
    interrupt_id: str
    node_id: str
    checkpoint: str
    actions: list[str]
    audited_reads_sha256: dict[str, str]
    artifact_view: str | None

class GraphResumedEvent(_GraphEvent):
    type: Literal["graph_resumed"]
    invocation_id: str
    checkpoint_ns: str
    interrupt_id: str
    action: str
    reason: str
    who: str
    audited_reads_sha256: dict[str, str]

class SuperstepCommittedEvent(_GraphEvent):
    type: Literal["superstep_committed"]
    invocation_id: str
    checkpoint_ns: str
    superstep_id: str
    checkpoint_id: str
    parent_checkpoint_id: str | None
    write_set_ids: list[str]
    target_tree_id: str
    state_values: dict[str, object]

class GraphTerminalEvent(_GraphEvent):
    type: Literal["graph_completed", "graph_stopped", "graph_failed"]
    invocation_id: str
    checkpoint_ns: str
    reason: str

class TaskImportedEvent(_GraphEvent):
    type: Literal["task_imported"]
    invocation_id: str
    checkpoint_ns: str
    graph_id: str
    node_id: str
    structural_path: str
    task_key: str | None
    outputs_sha256: dict[str, str]
    gate_report: dict[str, object] | None

class CheckpointImportedEvent(_GraphEvent):
    type: Literal["checkpoint_imported"]
    invocation_id: str
    checkpoint_ns: str
    fixture_id: str
    fixture_digest: str
    manifest_sha256: str
    input_sha256: dict[str, str]
```

All models set `source: Literal["graph"] = "graph"`. Add their union to `AuditEvent` in `core/events.py`.

Define `GraphEvent = Annotated[GraphInvocationStartedEvent | NodeActivatedEvent | NodeSkippedEvent | FanOutExpandedEvent | SuperstepPlannedEvent | TaskAttemptStartedEvent | TaskAttemptSucceededEvent | TaskAttemptFailedEvent | TaskAttemptAbandonedEvent | BudgetConsumedEvent | GraphInterruptedEvent | GraphResumedEvent | SuperstepCommittedEvent | GraphTerminalEvent | TaskImportedEvent | CheckpointImportedEvent, Field(discriminator="type")]`, and validate through one `TypeAdapter(GraphEvent)`. Import `ErrorKind` from `core.graph_types`; `core.graph_events` must never import `workflow.graph`.

- [ ] **Step 3: Add fail-closed strict reading without changing tolerant telemetry reading**

`read_events` remains tolerant for old query telemetry. `read_events_strict` must parse every nonblank line, require a dict, require integer sequence numbers exactly `1..N`, remove only ledger envelope keys `seq` and `ts`, and validate every `source == "graph"` payload through the graph-event adapter. It returns the original validated dictionaries with envelopes intact, so projections retain event sequence. It raises `LedgerIntegrityError` with line/sequence context on the first violation; unknown graph event types and extra payload fields are integrity failures.

- [ ] **Step 4: Write projector and checkpoint snapshot tests**

Create tests that append invocation → plan → two starts → one success → one failure and assert:

```python
projection = project_invocation(change, "inv-1")
assert projection.tasks["task-a"].status == "succeeded"
assert projection.tasks["task-a"].write_set_id == "ws-a"
assert projection.tasks["task-b"].status == "failed"
assert projection.tasks["task-b"].attempts_used == 1
assert projection.latest_checkpoint_id is None
```

Then append `superstep_committed` and assert both the checkpoint pointer and target tree project. Add duplicate `budget_consumed` with the same `(invocation_id, budget_id, consumption_id)` and require `LedgerIntegrityError`, not double counting.

For `CheckpointStore`, write a valid snapshot, corrupt its JSON, and assert `read_latest` returns the ledger-rebuilt projection and rewrites a valid cache.

Also write `workflow-state.yaml` from a projection, delete it, rebuild it from the ledger, and assert the rebuilt bytes are semantically identical. Corrupting this YAML must not change task completion, budgets, interrupts, retry attempts, or the next plan.

- [ ] **Step 5: Implement immutable projection models and a pure event fold**

Add to `models.py`:

```python
TaskStatus = Literal["pending", "running", "succeeded", "failed", "abandoned", "interrupted"]

class TaskProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str
    node_id: str
    status: TaskStatus
    attempts_used: int = 0
    latest_attempt_id: str | None = None
    write_set_id: str | None = None
    outputs_sha256: dict[str, str] = Field(default_factory=dict)
    gate_report: dict[str, object] | None = None
    state_updates: dict[str, object] = Field(default_factory=dict)
    error_kind: ErrorKind | None = None
    next_retry_at: str | None = None

class InterruptProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    interrupt_id: str
    checkpoint_ns: str
    node_id: str
    checkpoint: str
    actions: tuple[str, ...]
    audited_reads_sha256: dict[str, str]
    artifact_view: str | None = None
    resolved_action: str | None = None

class GraphProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: str
    entrypoint: str
    checkpoint_ns: str
    parent_invocation_id: str | None = None
    parent_task_id: str | None = None
    structural_path: str
    graph_digest: str
    contract_digests: dict[str, str]
    params: dict[str, object]
    root_tree_id: str
    current_tree_id: str
    latest_checkpoint_id: str | None = None
    event_seq: int = 0
    supersteps: int = 0
    state_values: dict[str, object] = Field(default_factory=dict)
    tasks: dict[str, TaskProjection] = Field(default_factory=dict)
    budgets: dict[str, int] = Field(default_factory=dict)
    fan_out_expansions: dict[str, dict[str, object]] = Field(default_factory=dict)
    interrupts: dict[str, InterruptProjection] = Field(default_factory=dict)
    terminal: Literal["completed", "stopped", "failed"] | None = None
    terminal_reason: str | None = None
```

Implement the fold as a pure function over strict events. Projected JSON never overrides a later ledger event.

Define `WorkflowStateProjection` as the human/reporting compatibility view containing invocation ID, entrypoint, terminal status/reason, latest checkpoint, event sequence, node/task summaries, pending interrupts, and budgets. `project_workflow_state` derives every field solely from `GraphProjection`; no runtime decision may read this YAML as authority.

- [ ] **Step 6: Add atomic compatibility-projection staging and checkpoint cache validation**

Add `ProgressionTxn.set_workflow_state_projection(content)` as the only graph path allowed to stage reserved `workflow-state.yaml`. It stores bytes separately from general `write_file`, rejects a second call or mixing with v1 `set_state`, and includes the file in the existing capture/apply/rollback transaction. Preserve v1 `set_state` behavior during side-by-side Tasks 4–14.

Store snapshots under `.graph-runtime/checkpoints/<checkpoint-id>.json`, including `event_seq`, graph/contract/params digests, task projections, pending write-set IDs, fan-out expansions, interrupts and budgets. Write via `ProgressionTxn.write_file`. On read, accept only when the snapshot event sequence and digest tuple exactly match the ledger projection; otherwise rebuild and overwrite. Stage `workflow-state.yaml` through the new projection method whenever a root checkpoint or terminal event is committed; regenerate it from strict events when absent or malformed.

- [ ] **Step 7: Verify and commit Task 4**

Run:

```bash
uv run pytest tests/unit/test_events.py tests/unit/workflow/graph/test_checkpoint.py tests/unit/core/test_progression.py -v
uv run ruff check .
uv run pyright
uv run lint-imports
```

Expected: all commands exit 0.

Commit:

```bash
git add assurance_agent/workflow/core/graph_events.py assurance_agent/workflow/core/events.py assurance_agent/workflow/core/progression.py assurance_agent/workflow/graph/checkpoint.py assurance_agent/workflow/graph/models.py tests/unit/core/test_progression.py tests/unit/test_events.py tests/unit/workflow/graph/test_checkpoint.py
git commit -m "feat(workflow): project graph state from strict ledger"
```

---

### Task 5: Add private task workspaces and content-addressed write-sets

**Files:**
- Create: `assurance_agent/workflow/graph/workspace.py`
- Create: `tests/unit/workflow/graph/test_workspace.py`
- Modify: `assurance_agent/workflow/core/progression.py`
- Modify: `tests/unit/core/test_progression.py`

**Interfaces:**
- Consumes: `ResourceClaims`, runtime logical roots, progression lock.
- Produces: `TreeStore.capture/materialize/freeze_write_set/load_write_set/merge_write_sets`; `TaskWorkspace`; idempotent canonical materialization.

- [ ] **Step 1: Write tree/write-set safety tests**

Create tests for a base project containing `qa/changes/CH-1/input.txt`, `tests/api/test_a.py`, and `app/source.py`. Required assertions:

```python
base_tree = store.capture(project_root)
workspace = backend.create(task_id="task-a", base_tree_id=base_tree, store=store)
(workspace.project_root / "tests/api/test_a.py").write_text("changed\n")
write_set = store.freeze_write_set(
    workspace,
    claims=ResourceClaims(
        writes=(ResourcePath.parse("repo:tests/api/**"),),
        authorization_writes=(ResourcePath.parse("repo:tests/api/**"),),
    ),
    outputs=("repo:tests/api/test_a.py",),
)
assert store.load_write_set(write_set.write_set_id) == write_set
assert (project_root / "tests/api/test_a.py").read_text() == "base\n"
```

Add tests rejecting a changed `app/source.py`, missing declared output, a symlink escape, overlapping sibling write-sets, base-tree drift, and tampered object bytes. Add a test that deletes the temporary task directory after freeze and successfully rematerializes from the object store.

Run: `uv run pytest tests/unit/workflow/graph/test_workspace.py -v`

Expected: FAIL because workspace symbols do not exist.

- [ ] **Step 2: Implement content-addressed objects and deterministic tree capture**

Use `.graph-runtime/objects/sha256/<first-two>/<digest>` under the change directory. Store blobs with `O_EXCL`, fsync, and verify an existing object's hash before reuse. A tree manifest is canonical JSON sorted by logical path and records `kind`, `sha256`, and executable mode.

Capture the project root while excluding `.git/`, `.graph-runtime/`, `.worktrees/`, `.venv/`, `node_modules/`, `__pycache__/`, `.pytest_cache/`, and `.ruff_cache/`. Preserve all other tracked, untracked, ignored change artifacts, tests, source files, `.aa/config.yaml`, and installed `skills/`. Reject changed symlinks and any path that resolves outside its logical root.

- [ ] **Step 3: Implement the exact workspace/write-set models**

```python
class WriteEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    logical_path: str
    operation: Literal["add", "modify", "delete"]
    before_sha256: str | None
    after_sha256: str | None
    blob_sha256: str | None
    executable: bool = False

class WriteSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    write_set_id: str
    task_id: str
    base_tree_id: str
    entries: tuple[WriteEntry, ...]
    outputs_sha256: dict[str, str]

@dataclass(frozen=True)
class TaskWorkspace:
    task_id: str
    root: Path
    project_root: Path
    repo_root: Path
    change_dir: Path
    base_tree_id: str

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

class WorkspaceBackend:
    def __init__(self, change_dir: Path) -> None:
        self._tasks_root = change_dir / ".graph-runtime" / "tasks"

    def create(
        self,
        *,
        task_id: str,
        base_tree_id: str,
        store: TreeStore,
    ) -> TaskWorkspace:
        root = self._tasks_root / task_id
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        store.materialize(base_tree_id, root)
        return TaskWorkspace.from_materialized_root(task_id, root, base_tree_id)
```

`TaskWorkspace.from_materialized_root` resolves the materialized logical `project`, `repo`, and `change` roots from the tree manifest and rejects a missing or escaping root. `freeze_write_set` computes a complete before/after diff, validates each actual path against `authorization_writes`, validates outputs, writes all new blobs, canonicalizes the manifest, and derives `write_set_id` from the manifest bytes.

- [ ] **Step 4: Initialize a task-local Git index without sharing canonical `.git`**

After materializing a workspace, run `git init -q` and `git add -f -A` when Git exists. This gives phase agents a local `git status`/`git diff` baseline without exposing the canonical repository metadata. Failure to initialize the convenience index is a contract error for agent targets and is ignored only for explicitly side-effect-free builtin targets.

- [ ] **Step 5: Extend progression with a coordinator pointer commit**

Add `ProgressionTxn.write_runtime_file(rel: str, content: bytes | str)` restricted to `.graph-runtime/` and still rejecting `events.jsonl`, `workflow-state.yaml`, locks, absolute paths, and traversal. Add:

```python
def commit_tree_pointer(
    change_dir: Path,
    *,
    event: SuperstepCommittedEvent,
    checkpoint_rel: str,
    checkpoint_bytes: bytes,
) -> None:
    with transaction(change_dir) as txn:
        txn.write_runtime_file(checkpoint_rel, checkpoint_bytes)
        txn.append_strict(event)
```

The strict event/tree pointer commits before canonical filesystem materialization. If materialization is interrupted, the next runtime invocation reads `target_tree_id` from the ledger and idempotently repairs the canonical files without re-running a task.

- [ ] **Step 6: Merge and materialize write-sets**

Sort write-sets by structural task ID. Reject two entries for the same logical path, an entry whose `before_sha256` disagrees with the base tree, and any current canonical root digest that differs from the projected base. Apply target entries with temp-file + `os.replace`; apply deletes last. After a partial failure, reapplying the same target tree must converge.

- [ ] **Step 7: Verify and commit Task 5**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_workspace.py tests/unit/core/test_progression.py -v
uv run ruff check .
uv run pyright
```

Expected: all commands exit 0.

Commit:

```bash
git add assurance_agent/workflow/graph/workspace.py assurance_agent/workflow/core/progression.py tests/unit/workflow/graph/test_workspace.py tests/unit/core/test_progression.py
git commit -m "feat(workflow): isolate task writes in content store"
```

---

### Task 6: Implement pure Plan semantics, routes, joins, and typed reducers

**Files:**
- Create: `assurance_agent/workflow/graph/planner.py`
- Create: `tests/unit/workflow/graph/test_planner.py`
- Modify: `assurance_agent/workflow/graph/models.py`

**Interfaces:**
- Consumes: `CompiledWorkflow`, `GraphProjection`, AA DSL, resource claims.
- Produces: `ArtifactReader.read_json(tree_id: str, logical_path: str) -> ResolvedArtifact`; `plan_superstep(compiled: CompiledWorkflow, projection: GraphProjection, context: RuntimeContext, artifacts: ArtifactReader) -> PlanResult`; deterministic `apply_state_updates`.

- [ ] **Step 1: Write activation and same-step visibility tests**

Use a graph `START -> left`, `START -> right`, `left/right -> join`, `join -> END`. Assert the initial plan returns `left` and `right` in declaration order, with stable task IDs. Feed a projection where only `left` succeeded and assert no downstream task is ready. Feed both successes and assert only `join` is ready.

Add mutually exclusive START edges to the same node and assert it activates once. Add two non-mutually-exclusive incoming paths and assert compiler rejection, not duplicate activation.

- [ ] **Step 2: Add exact plan/result models**

```python
class RuntimeContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    project_root: Path
    repo_root: Path
    change_dir: Path
    change_id: str
    params: dict[str, object] = Field(default_factory=dict)
    parent_session_id: str | None = None


class ResolvedArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    value: object
    reads_sha256: dict[str, str]


class ArtifactReader(Protocol):
    def read_json(self, tree_id: str, logical_path: str) -> ResolvedArtifact:
        raise NotImplementedError


class ExecutableTask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str
    invocation_id: str
    checkpoint_ns: str
    graph_id: str
    node_id: str
    structural_path: str
    input: object
    input_sha256: str
    contract_digest: str
    retryable_errors: tuple[ErrorKind, ...]
    retry_policy: RetryPolicyDef
    timeout_policy: TimeoutPolicyDef
    target: str
    resources: ResourceClaims
    task_key: str | None = None

class PlanResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    superstep_id: str
    checkpoint_id: str
    tasks: tuple[ExecutableTask, ...]
    strict_events: tuple[BaseModel, ...] = ()
    terminal: Literal["end", "stop", "fail", "interrupt"] | None = None
    reason: str | None = None
```

Derive structural task IDs from invocation ID, checkpoint namespace, graph/node path, activation ordinal, and fan-out key using canonical SHA-256. Wall-clock time and thread completion order must never enter the ID.

- [ ] **Step 3: Implement frozen activation and routing**

An ordinary node activates when at least one incoming edge token is selected. Evaluate node `when` only after predecessor results are stable, and emit exactly one `node_activated` or `node_skipped` event. A skipped ordinary node does not traverse its outgoing edges. Routes read only frozen task outcomes; missing/MISSING selection chooses explicit default, otherwise fail closed to `STOP`.

Persist activation/skip events before returning tasks so resume never re-evaluates a previously frozen condition.

Build the DSL scope by loading only `CompiledGraph.artifact_symbols` from `projection.current_tree_id` through `ArtifactReader`; JSON parse failure or hash mismatch fails closed. Carry the union of `ResolvedArtifact.reads_sha256` into activation and fan-out events so the exact inputs can be rechecked after restart.

- [ ] **Step 4: Implement join modes**

`all` waits for every source to be succeeded or skipped. `all_active` waits for every activated source and ignores skipped sources. `any` becomes ready after the first success; when `cancel_remaining` is false, already-running siblings settle, while canonical graphs never request cancellation. Empty `all_active` is a compiler/runtime error rather than an implicit pass.

- [ ] **Step 5: Implement typed reducers with stable ordering**

```python
def apply_state_updates(
    state_defs: Mapping[str, StateDef],
    current: Mapping[str, object],
    updates: Sequence[tuple[str, Mapping[str, object]]],
) -> dict[str, object]:
    result = dict(current)
    for task_id, task_updates in sorted(updates, key=lambda pair: pair[0]):
        for key, value in task_updates.items():
            definition = state_defs[key]
            result[key] = _reduce(definition.reducer, result.get(key, definition.default), value)
    return result
```

`merge_disjoint` rejects duplicate object keys; `set_union` returns a list sorted by canonical JSON; `append` requires lists; `replace` rejects more than one writer in a superstep. Add direct tests for all four.

- [ ] **Step 6: Enforce max-superstep and terminal priority**

If projected committed steps equal `graph.max_supersteps`, return FAIL before planning another task. Resolve settled results in priority order: integrity/runtime FAIL, STOP/REJECT, interrupt, retry pending, completed. STOP commits validated settled write-sets; technical fail and interrupt leave the incomplete wave pending.

- [ ] **Step 7: Verify and commit Task 6**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_planner.py -v
uv run ruff check .
uv run pyright
```

Expected: all commands exit 0.

Commit:

```bash
git add assurance_agent/workflow/graph/planner.py assurance_agent/workflow/graph/models.py tests/unit/workflow/graph/test_planner.py
git commit -m "feat(workflow): plan deterministic graph supersteps"
```

---

### Task 7: Add frozen fan-out and ledger-authoritative business budgets

**Files:**
- Create: `tests/unit/workflow/graph/test_fanout_budget.py`
- Modify: `assurance_agent/workflow/graph/planner.py`
- Modify: `assurance_agent/workflow/graph/checkpoint.py`
- Modify: `assurance_agent/workflow/graph/models.py`

**Interfaces:**
- Consumes: planner activation, strict fan-out/budget events, typed reducers.
- Produces: deterministic dynamic child tasks and budget exhaustion routing.

- [ ] **Step 1: Write fan-out freeze tests**

Build a node with `items: advisory.modules`, `item_as: module`, `key: ${module}`, and two modules. Assert one `fan_out_expanded` event freezes items, keys, source hashes, and child IDs; two child tasks are returned in source-list order. Change the advisory after the event and assert `fan_out_source_drift`. Resume with unchanged source and assert no second expansion event.

Add failures for duplicate keys, more than `max_items`, non-JSON items, unsafe path keys, and reducer target/type mismatch.

- [ ] **Step 2: Implement template expansion and stable dynamic IDs**

Only `${<item_as>}` and `${context.change_id}` are valid templates. Require the fan-out key expression to resolve to `str | int | float | bool`, canonicalize it without lossy path normalization, and require uniqueness. Expand each child's `with`, outputs, resource claims, and authorization paths before scheduling; reject an unsafe path segment or any expansion outside the target contract. Structural child IDs use the canonical key/item hash; display values never become workflow-state keys.

Freeze the expansion in a strict transaction before any child starts. On replay, compare current source-read hashes to `source_reads_sha256`; never recompute a different item list.

- [ ] **Step 3: Write business-budget tests**

For limit 2, project two distinct `budget_consumed` events and assert the third consumer routes to `exhausted_to` without producing an `ExecutableTask`. Assert duplicate `consumption_id` is idempotent, a failed/abandoned task consumes zero, and three technical retries followed by one task success consume exactly one unit.

- [ ] **Step 4: Implement atomic success + budget consumption contract**

Planner marks a task as a budget consumer; scheduler must stage `task_attempt_succeeded`, `budget_consumed`, output hashes, gate report, state updates and write-set ID in one progression transaction. Derive `consumption_id = sha256(invocation_id + checkpoint_ns + budget_id + task_id)`. Project counts only unique strict events. Check exhaustion before creating another task.

- [ ] **Step 5: Implement deterministic fan-out reduction**

Wait for every frozen child because v2 supports `completion: all` only. Feed child values to the declared reducer in frozen item order, not completion order. If any child is failed/retrying/interrupted, keep successful child writes pending and do not publish the reduced state.

- [ ] **Step 6: Verify and commit Task 7**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_fanout_budget.py tests/unit/workflow/graph/test_planner.py tests/unit/workflow/graph/test_checkpoint.py -v
uv run ruff check .
uv run pyright
```

Expected: all commands exit 0.

Commit:

```bash
git add assurance_agent/workflow/graph/planner.py assurance_agent/workflow/graph/checkpoint.py assurance_agent/workflow/graph/models.py tests/unit/workflow/graph/test_fanout_budget.py
git commit -m "feat(workflow): persist fan-out and business budgets"
```

---

### Task 8: Persist retry attempts, backoff, heartbeats, and abandonment

**Files:**
- Create: `assurance_agent/workflow/graph/leases.py`
- Create: `tests/unit/workflow/graph/test_leases_retry.py`
- Modify: `assurance_agent/workflow/graph/models.py`
- Modify: `assurance_agent/workflow/graph/checkpoint.py`

**Interfaces:**
- Consumes: strict attempt events and `RetryPolicyDef`/`TimeoutPolicyDef`.
- Produces: `Clock`/`SystemClock`; `LeaseRegistry`; `next_attempt_decision`; stable backoff calculation; orphan adoption/abandonment events.

- [ ] **Step 1: Write retry-decision tests with an injected clock**

Use a `FakeClock` returning controlled UTC and monotonic values. Assert:

```python
decision = next_attempt_decision(
    task=task,
    projection=failed_once,
    now=fake_clock.now(),
)
assert decision.kind == "wait"
assert decision.next_retry_at == failed_once.tasks[task.task_id].next_retry_at

fake_clock.advance(2)
decision = next_attempt_decision(task=task, projection=failed_once, now=fake_clock.now())
assert decision.kind == "start"
assert decision.attempt_number == 2
```

Add tests that an error retries only when it appears in both `task.retry_policy.retry_on` and `task.retryable_errors`; `auth`, `invalid_output`, `forbidden_write`, and `contract` failures stop immediately for canonical contracts; `max_attempts` includes the first attempt; abandoned counts as an attempt; normal resume never resets attempts.

- [ ] **Step 2: Implement deterministic backoff and attempt decisions**

```python
class Clock(Protocol):
    def now(self) -> datetime:
        raise NotImplementedError

    def monotonic(self) -> float:
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class AttemptDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["start", "wait", "exhausted", "failed"]
    attempt_number: int | None = None
    next_retry_at: str | None = None
    reason: str | None = None


def retry_delay_seconds(policy: RetryPolicyDef, task_id: str, attempt_number: int) -> float:
    raw = policy.backoff.initial_seconds * (policy.backoff.multiplier ** max(0, attempt_number - 1))
    capped = min(raw, policy.backoff.max_seconds)
    if not policy.backoff.jitter or capped == 0:
        return capped
    digest = hashlib.sha256(f"{task_id}:{attempt_number}".encode()).digest()
    fraction = int.from_bytes(digest[:8], "big") / float(2**64 - 1)
    return capped * (0.5 + 0.5 * fraction)
```

Jitter is derived from structural identity, never process randomness, so restart does not move `next_retry_at`.

- [ ] **Step 3: Write atomic lease-file tests**

Assert two concurrent `LeaseRegistry.upsert` calls retain both records; `heartbeat` only advances the matching attempt; a stale attempt cannot overwrite a newer attempt; a truncated temp file never replaces the prior valid `running-tasks.json`.

Use this model:

```python
class RunningTaskLease(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str
    attempt_id: str
    pid: int
    host: str
    session_id: str | None
    started_at: str
    last_heartbeat_at: str
    lease_expires_at: str
```

- [ ] **Step 4: Implement coordinator-owned lease registry**

Store `running-tasks.json` at the change root. Use a process-local lock plus `.progression.lock`, read-modify-write to a temporary sibling, fsync, and `os.replace`. This file is not appended to strict ledger and cannot change budget or task-success projection.

The scheduler starts one heartbeat thread per running future. It updates only liveness; it cannot turn a failed task into success.

- [ ] **Step 5: Implement recovery classification**

For each projected `running` task:

1. If its contract supports reconnect and the session is alive, return `adopt`.
2. If same-host PID/session is provably dead, append `task_attempt_abandoned` immediately.
3. If liveness is unknown and lease is unexpired, return `wait`.
4. If lease expired, append `task_attempt_abandoned`.
5. Feed the updated attempt count into `next_attempt_decision`.

Write abandonment through `transaction` and deduplicate by `attempt_id`.

- [ ] **Step 6: Verify and commit Task 8**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_leases_retry.py tests/unit/workflow/graph/test_checkpoint.py -v
uv run ruff check .
uv run pyright
```

Expected: all commands exit 0.

Commit:

```bash
git add assurance_agent/workflow/graph/leases.py assurance_agent/workflow/graph/models.py assurance_agent/workflow/graph/checkpoint.py tests/unit/workflow/graph/test_leases_retry.py
git commit -m "feat(workflow): persist retries and task leases"
```

---

### Task 9: Add the internal NodeRunner and canonical target handlers

**Files:**
- Create: `assurance_agent/workflow/graph/agent_api.py`
- Create: `assurance_agent/workflow/graph/task_runner.py`
- Create: `assurance_agent/workflow/graph/handlers/__init__.py`
- Create: `assurance_agent/workflow/graph/handlers/agent.py`
- Create: `assurance_agent/workflow/graph/handlers/operation.py`
- Create: `assurance_agent/workflow/graph/handlers/gate.py`
- Create: `assurance_agent/workflow/graph/handlers/join.py`
- Create: `assurance_agent/workflow/graph/handlers/interrupt.py`
- Create: `tests/unit/workflow/graph/test_task_runner.py`
- Modify: `assurance_agent/workflow/graph/models.py`
- Modify: `assurance_agent/workflow/driver/adapter.py`
- Modify: `assurance_agent/workflow/driver/headless_adapter.py`
- Modify: `assurance_agent/workflow/driver/opencode_adapter.py`
- Modify: `assurance_agent/workflow/driver/phase_prompt.py`
- Modify: `assurance_agent/workflow/orchestration/gates.py`

**Interfaces:**
- Consumes: `ExecutableTask`, `TaskWorkspace`, existing agent adapters, gates, execution/report/healing domain functions.
- Produces: graph-owned `AgentRequest`/`AgentResult`/`AgentInvoker`; `NodeRunner.execute(task, workspace, context) -> TaskResult`; handler registry for `skill:`, `operation:`, and `builtin:` targets.

- [ ] **Step 1: Define task result and handler protocols with failing dispatch tests**

Add tests registering one handler per namespace and asserting exact dispatch, unknown-target `contract` failure, and handler exceptions normalized to `internal` without escaping the runner.

Define:

```python
class TaskResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["succeeded", "failed", "interrupted", "stopped"]
    value: object = None
    state_updates: dict[str, object] = Field(default_factory=dict)
    outputs_sha256: dict[str, str] = Field(default_factory=dict)
    gate_report: dict[str, object] | None = None
    write_set_id: str | None = None
    error_kind: ErrorKind | None = None
    error: str | None = None
    interrupt: InterruptProjection | None = None

class TaskHandler(Protocol):
    def execute(
        self,
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        raise NotImplementedError

class NodeRunner(Protocol):
    def execute(
        self,
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        raise NotImplementedError
```

In the implementation, Protocol bodies use `raise NotImplementedError`; no runtime handler returns an unimplemented result.

- [ ] **Step 2: Add graph-owned workspace-aware adapter requests and typed errors**

Create the dependency-neutral seam in `graph/agent_api.py`:

```python
class AgentRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target: str
    node_id: str
    change_id: str
    workspace_root: Path
    allowed_writes: tuple[str, ...]
    prompt: str
    timeout_seconds: float
    reconnect_session_id: str | None = None


class AgentResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ok: bool
    error_kind: ErrorKind | None = None
    error: str | None = None
    session_id: str | None = None


class AgentInvoker(Protocol):
    def invoke(self, request: AgentRequest) -> AgentResult:
        raise NotImplementedError
```

Make `driver.adapter` a compatibility re-export of these types plus a v1 `PhaseRequest`/`PhaseResult` translation shim until Task 17. The graph package imports only `graph.agent_api`; driver implementations import and implement `AgentInvoker`, preserving `driver → graph` rather than creating `graph → driver`.

`HeadlessAdapter` runs the process at `Path(request.workspace_root)`; timeout maps to `timeout`, nonzero process exit maps to `internal`, and spawn/transport errors map to `transport`. `OpenCodeAdapter._request` accepts a directory argument and every request uses `request.workspace_root`; HTTP 401/403 → `auth`, 429 → `rate_limit`, network error → `transport`, poll deadline → `timeout`. Return the created OpenCode session ID for reconnect metadata.

Update prompt construction to list the contract-authorized write roots instead of claiming every phase is change-directory-only:

```python
def build_phase_prompt(
    skill: str,
    phase: str,
    change_id: str,
    *,
    allowed_writes: Sequence[str],
    item: str | None = None,
) -> str:
    allowed = ", ".join(sorted(allowed_writes)) or "(none)"
    return (
        f"Call skill(name='{skill}'). Operate strictly on change_id='{change_id}'. "
        f"Authorized write paths: {allowed}. Produce only node {phase}'s declared outputs. "
        "Do not run aa gate/status, edit workflow-state.yaml, or access coordinator runtime files."
        + (f" Fan-out item: {item}." if item is not None else "")
    )
```

- [ ] **Step 3: Implement agent handler and output freezing**

`AgentHandler` resolves the `skill:` target, builds the request with workspace paths, calls the injected adapter, validates the declared result fields, then asks `TreeStore.freeze_write_set` to verify actual writes and outputs. Adapter failure returns `TaskResult(status="failed", error_kind=adapter_result.error_kind or "internal", error=adapter_result.error)`; it never writes strict events itself.

- [ ] **Step 4: Implement operation handler registry without subprocess recursion**

Register exact callables:

```python
OperationFn = Callable[[ExecutableTask, TaskWorkspace, RuntimeContext], OperationResult]

operations = {
    "operation:no-op": no_op,
    "operation:skill-registry-check": skill_registry_check,
    "operation:run-tests": run_tests,
    "operation:allocate-healing-attempt": operation_allocate_healing_attempt,
    "operation:record-healing-status": operation_record_healing_status,
    "operation:stop": stop_operation,
}
```

`run_tests` calls `workflow.execution.runner.run_change` against the remapped workspace project/change paths; it does not spawn `aa run`. Allocation writes the entry-baseline artifact in the task workspace and returns state updates; strict `budget_consumed` remains scheduler-owned. Stop returns `status="stopped"` with its declared reason.

- [ ] **Step 5: Generalize gate evaluation to an explicit artifact view**

Add:

```python
@dataclass(frozen=True)
class GateEvaluationContext:
    project_root: Path
    repo_root: Path
    change_dir: Path
    change_id: str
    params: Mapping[str, object]
    state_values: Mapping[str, object]
    node_results: Mapping[str, object]


class FrozenGateReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    gate_id: str
    verdict: Verdict
    matched_rule: str | None
    reason: str
    reads_sha256: dict[str, str]

def check_gate_in_view(
    gates: Mapping[str, GateDef],
    gate_id: str,
    context: GateEvaluationContext,
) -> FrozenGateReport:
    gate = gates.get(gate_id)
    if gate is None:
        raise GateError(f"unknown gate: {gate_id}")
    verdict, matched_rule, reason, reads_sha256 = _evaluate_gate_def(gate, context)
    return FrozenGateReport(
        gate_id=gate_id,
        verdict=verdict,
        matched_rule=matched_rule,
        reason=reason,
        reads_sha256=reads_sha256,
    )
```

Extract the current ordered rule loop into `_evaluate_gate_def`. It resolves `change:`, `project:`, and `repo:` roots from `GateEvaluationContext`, returns the first matching rule, and hashes every audited read before returning. Attached gates run against base snapshot + current task workspace; builtin gates run against committed graph workspace. Keep the current `check_gate` wrapper working until Task 15.

- [ ] **Step 6: Implement builtin gate/join/interrupt handlers**

Gate returns `value`, `gate_report`, and a business status of succeeded regardless of verdict. Join returns succeeded only after planner satisfaction. Interrupt builds an `InterruptProjection` with structural interrupt ID, actions, audited hashes, and no human decision. It does not write the ledger; GraphRuntime publishes it after sibling settling.

- [ ] **Step 7: Verify and commit Task 9**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_task_runner.py tests/unit/driver/test_headless_adapter.py tests/unit/driver/test_opencode_adapter.py tests/unit/test_gates.py -v
uv run ruff check .
uv run pyright
uv run lint-imports
```

Expected: all commands exit 0.

Commit:

```bash
git add assurance_agent/workflow/graph/agent_api.py assurance_agent/workflow/graph/task_runner.py assurance_agent/workflow/graph/handlers assurance_agent/workflow/graph/models.py assurance_agent/workflow/driver/adapter.py assurance_agent/workflow/driver/headless_adapter.py assurance_agent/workflow/driver/opencode_adapter.py assurance_agent/workflow/driver/phase_prompt.py assurance_agent/workflow/orchestration/gates.py tests/unit/workflow/graph/test_task_runner.py
git commit -m "feat(workflow): execute graph nodes through handlers"
```

---

### Task 10: Execute deterministic resource waves in true parallel

**Files:**
- Create: `assurance_agent/workflow/graph/scheduler.py`
- Create: `tests/unit/workflow/graph/test_scheduler.py`
- Modify: `assurance_agent/workflow/graph/models.py`
- Modify: `assurance_agent/workflow/graph/leases.py`

**Interfaces:**
- Consumes: `PlanResult`, `NodeRunner`, workspace store/backend, retry/lease decisions, strict transactions.
- Produces: `Scheduler.execute(plan, projection, context) -> WaveResult`; deterministic greedy wave selection.

- [ ] **Step 1: Write deterministic wave-selection tests**

Create four ready tasks in declaration order: API writer, API reader, E2E writer, global-exclusive task. Assert wave 1 is API writer + E2E writer, wave 2 is API reader, wave 3 is global-exclusive. Reorder input tasks and assert the same structural ordering after scheduler sort.

Define:

```python
class WaveResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    superstep_id: str
    succeeded: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    interrupted: tuple[str, ...] = ()
    stopped: tuple[str, ...] = ()
    pending_write_set_ids: tuple[str, ...] = ()
    retry_at: str | None = None
```

- [ ] **Step 2: Implement greedy maximal non-conflicting selection**

Sort by `(topology_rank, declaration_index, task_id)`. Walk the sorted ready set and add a task when it conflicts with none already selected and the wave has fewer than `max_parallel_tasks`. Leave conflicts ready for a later superstep after the current wave commits.

- [ ] **Step 3: Prove execution is concurrent rather than interleaved**

Use two handlers blocked on a `threading.Barrier(2)`. The test must finish both tasks under a timeout that a serial scheduler cannot meet, and record overlapping monotonic intervals. Do not assert only on call order.

Run: `uv run pytest tests/unit/workflow/graph/test_scheduler.py::test_non_conflicting_tasks_overlap -v`

Expected before implementation: FAIL or timeout because scheduler does not exist.

- [ ] **Step 4: Implement structured thread execution and sibling settling**

Use `ThreadPoolExecutor(max_workers=policy.max_parallel_tasks)` inside a context manager. For every selected task:

1. Reconcile an existing running/succeeded/failed projection.
2. Append `task_attempt_started` before submitting a new physical attempt.
3. Create a private workspace from the projected base tree.
4. Register the lease and heartbeat.
5. Execute `NodeRunner`.
6. Freeze write-set and append succeeded/failed event.
7. Remove the live lease.

After one future fails or interrupts, submit no new node, but wait for all already-submitted siblings. Never cancel an agent process merely because its sibling failed.

- [ ] **Step 5: Make success and budget events atomic**

For a successful task, open one progression transaction and append `task_attempt_succeeded`; when applicable append one deduplicated `budget_consumed`; include output hashes, frozen gate report, state updates and write-set ID. On transaction failure, retain the write-set object but do not infer success from its existence.

- [ ] **Step 6: Commit or retain pending writes correctly**

If every required task succeeds, merge write-sets, compute next state values, commit checkpoint/tree pointer and `superstep_committed`, then materialize. If a sibling is retrying, failed or interrupted, retain successful write-set IDs in projection and do not materialize. On resume, a projected success bypasses handler execution and rejoins the pending wave.

Add tests for A success/B transient fail then resume only B, A success/B permanent fail, Update failure after both successes, and actual overlapping writes despite non-conflicting declarations.

- [ ] **Step 7: Verify and commit Task 10**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_scheduler.py tests/unit/workflow/graph/test_leases_retry.py tests/unit/workflow/graph/test_workspace.py -v
uv run ruff check .
uv run pyright
```

Expected: all commands exit 0.

Commit:

```bash
git add assurance_agent/workflow/graph/scheduler.py assurance_agent/workflow/graph/models.py assurance_agent/workflow/graph/leases.py tests/unit/workflow/graph/test_scheduler.py
git commit -m "feat(workflow): run safe graph tasks in parallel"
```

---

### Task 11: Build the single-graph GraphRuntime run/status/recovery loop

**Files:**
- Create: `assurance_agent/workflow/graph/runtime.py`
- Create: `tests/integration/test_graph_runtime.py`
- Modify: `assurance_agent/workflow/graph/__init__.py`
- Modify: `assurance_agent/workflow/graph/models.py`
- Modify: `assurance_agent/workflow/graph/checkpoint.py`

**Interfaces:**
- Consumes: compiler, projector, planner, scheduler, handler registry, workspace store.
- Produces: the public `GraphRuntime.run`, `resume`, `status`, and `latest_root_invocation` methods; `RunResult`, `RuntimeContext`, `GraphStatus`, `ResumeCommand`.

- [ ] **Step 1: Define the remaining public command/result models exactly once**

Reuse `RuntimeContext` from Task 6 and add the remaining models to `models.py`:

```python
class ResumeCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    interrupt_id: str
    action: Literal["fix_and_proceed", "accept_risk", "stop"]
    reason: str
    who: str

class GraphStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: str
    entrypoint: str
    status: Literal["running", "interrupted", "completed", "stopped", "failed"]
    checkpoint_id: str | None
    event_seq: int
    superstep: int
    running_tasks: tuple[str, ...]
    pending_tasks: tuple[str, ...]
    pending_write_sets: tuple[str, ...]
    pending_interrupts: tuple[InterruptProjection, ...]
    next_retry_at: str | None
    budgets: dict[str, int]
    terminal_reason: str | None

class RunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: str
    status: GraphStatus
    exit_code: Literal[0, 20, 30, 40]
    reason: str

class ImportResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: str
    checkpoint_id: str
    imported_tasks: tuple[str, ...]
```

Export only `GraphRuntime`, these public models, and `CompiledWorkflow` from `graph/__init__.py`.

Define `GraphRuntimeError(AaError)`, `GraphDefinitionChanged(GraphRuntimeError)`, and `GraphIntegrityError(GraphRuntimeError)` in `runtime.py`. The CLI maps them to exit 40; checkpoint/ledger corruption must use `GraphIntegrityError`, while pinned digest drift must use `GraphDefinitionChanged`.

- [ ] **Step 2: Write an end-to-end minimal run test**

Compile the minimal v2 fixture, register `operation:no-op`, initialize a temp project/change, and call `runtime.run`. Assert exit 0, one physical attempt, one committed superstep, `graph_completed`, and status reconstructed from a fresh runtime instance.

```python
result = runtime.run(compiled, "full", context)
assert result.exit_code == 0
assert result.status.status == "completed"
events = read_events_strict(context.change_dir)
assert [event["type"] for event in events].count("task_attempt_started") == 1
assert [event["type"] for event in events].count("task_attempt_succeeded") == 1
assert events[-1]["type"] == "graph_completed"
assert fresh_runtime.status(result.invocation_id).model_dump() == result.status.model_dump()
```

Run: `uv run pytest tests/integration/test_graph_runtime.py::test_minimal_graph_run_and_fresh_status -v`

Expected: FAIL because GraphRuntime does not exist.

- [ ] **Step 3: Implement dependency construction and the public interface**

```python
class GraphRuntime:
    def __init__(
        self,
        *,
        checkpoint_store: CheckpointStore,
        object_store: TreeStore,
        workspace_backend: WorkspaceBackend,
        contracts: ExecutionContractCatalog,
        node_runner: NodeRunner,
        scheduler: Scheduler,
        schema_resolver: Callable[[str], CompiledWorkflow],
        clock: Clock,
    ) -> None:
        self._checkpoints = checkpoint_store
        self._objects = object_store
        self._workspaces = workspace_backend
        self._contracts = contracts
        self._node_runner = node_runner
        self._scheduler = scheduler
        self._schema_resolver = schema_resolver
        self._clock = clock

    def run(
        self,
        schema: CompiledWorkflow,
        entrypoint: str,
        context: RuntimeContext,
    ) -> RunResult:
        return self._start_and_drive(schema, entrypoint, context)

    def resume(
        self,
        invocation_id: str,
        command: ResumeCommand | None = None,
    ) -> RunResult:
        return self._recover_and_drive(invocation_id, command)

    def status(self, invocation_id: str) -> GraphStatus:
        projection = self._checkpoints.project(invocation_id)
        return graph_status_from_projection(projection)

    def latest_root_invocation(self) -> str | None:
        return self._checkpoints.latest_root_invocation()
```

`import_checkpoint` is added in Task 13. The runtime is synchronous externally; concurrency is internal to Scheduler.

- [ ] **Step 4: Start an invocation in one strict transaction**

Resolve/freeze params, validate entrypoint allow, capture initial tree, generate invocation/checkpoint namespace IDs, and append `graph_invocation_started`. Save the canonical schema and referenced contract JSON objects by digest so `schema_resolver` can compare current definitions after restart. A duplicate start with the same invocation ID is rejected; a completed change is not silently restarted.

- [ ] **Step 5: Drive Plan → Execute → Update until a stable return boundary**

Implement one loop that:

1. Strictly projects the invocation and repairs any committed-but-unmaterialized tree.
2. Rejects graph/contract digest drift.
3. Reconciles running attempts and retry time.
4. Calls `plan_superstep`.
5. Commits plan events before task execution.
6. Executes one maximal non-conflicting wave.
7. Reprojects from ledger rather than mutating an in-memory authoritative counter.
8. Returns on completed, stopped, failed, or interrupt. For retry pending, publish `next_retry_at`, sleep through the injected clock while process ownership is retained, then reproject; killing that wait is safe because a later plain resume observes the same ledger time.

No branch switches on skill/CLI/healing kinds.

After each root `superstep_committed` and graph terminal event, stage the `workflow-state.yaml` compatibility projection in the same coordinator transaction. At startup/status, repair a missing or malformed projection from strict events; never infer ledger completion from that file.

- [ ] **Step 6: Add crash/recovery and Update-only retry tests**

Inject failures after `task_attempt_started`, after write-set freeze, after `task_attempt_succeeded`, after pointer commit, and during materialization. Assert a fresh runtime either abandons/retries the physical attempt or retries only Update; it never re-executes a task with strict success.

- [ ] **Step 7: Verify and commit Task 11**

Run:

```bash
uv run pytest tests/integration/test_graph_runtime.py tests/unit/workflow/graph -v
uv run ruff check .
uv run pyright
uv run lint-imports
```

Expected: all commands exit 0.

Commit:

```bash
git add assurance_agent/workflow/graph/runtime.py assurance_agent/workflow/graph/__init__.py assurance_agent/workflow/graph/models.py assurance_agent/workflow/graph/checkpoint.py tests/integration/test_graph_runtime.py
git commit -m "feat(workflow): add ledger-driven graph runtime"
```

---

### Task 12: Add nested subgraphs and audited resumable interrupts

**Files:**
- Create: `assurance_agent/workflow/graph/handlers/subgraph.py`
- Create: `tests/unit/workflow/graph/test_subgraph_interrupt.py`
- Modify: `assurance_agent/workflow/graph/runtime.py`
- Modify: `assurance_agent/workflow/graph/checkpoint.py`
- Modify: `assurance_agent/workflow/graph/task_runner.py`
- Modify: `assurance_agent/workflow/graph/handlers/interrupt.py`
- Modify: `assurance_agent/workflow/core/graph_events.py`

**Interfaces:**
- Consumes: named compiled graphs, checkpoint namespaces, pending write-sets, `ResumeCommand`.
- Produces: per-invocation child graph execution, interrupt bubbling/publication, audited resume.

- [ ] **Step 1: Use the parent/child identity already present in invocation events**

Project and validate the Task 4 fields:

```python
checkpoint_ns: str
parent_invocation_id: str | None = None
parent_task_id: str | None = None
structural_path: str
```

Root namespace is `<root-invocation-id>`; child namespace is `<parent-ns>/<parent-node-id>/<child-invocation-id>`. Derive child invocation ID from parent task ID and graph ID so resume cannot create a second child.

- [ ] **Step 2: Write nested completion and namespace tests**

Use a parent whose two START children invoke the same named subgraph with different node IDs. Assert distinct namespaces, no checkpoint collision, true parallel child work, and one parent pending write-set per child. A fresh runtime must project both child states.

- [ ] **Step 3: Implement child runtime over the parent task workspace**

`SubgraphHandler` calls an injected internal `run_child(parent_task, graph_id, workspace, context)`. Child supersteps commit to the child's private tree lineage and ledger namespace, not the root canonical workspace. On child completion, freeze the accumulated child tree delta as the parent task write-set. On child failure/interrupt, return the corresponding TaskResult without flattening partial files into the parent graph.

- [ ] **Step 4: Write interrupt publication tests**

Create a review → interrupt graph. Assert exit 30, `graph_interrupted` includes actions and audited hashes, `GraphStatus.pending_interrupts` contains one item, and a read-only artifact view contains the exact reviewed bytes even though the parent write-set is pending.

Add two parallel subgraphs that interrupt and assert both interrupt IDs are returned; resolving one leaves the other pending.

- [ ] **Step 5: Publish immutable artifact views and bubble interrupts**

Materialize the checkpoint namespace tree under `.graph-runtime/views/<interrupt-id>/`, mark files read-only, and store its relative path in the event. Nested interrupts propagate to root status while retaining their child namespace. Once any interrupt exists, Plan starts no new task, but Scheduler settles already-running siblings and persists their write-sets.

- [ ] **Step 6: Validate and commit audited resume decisions**

`resume` requires a pending interrupt ID, allowed action, nonblank reason and who. Rehash every audited path in the saved namespace view; reject drift. Append `graph_resumed` with the same hashes in one transaction, then route by `resume.action` from the original namespace.

Tests must prove:

- `accept_risk` at `healing.safety` routes to rerun, not back to the same gate;
- `fix_and_proceed` routes to the declared fixer/proposal path;
- `stop` produces exit 20;
- an action for a different checkpoint or stale hash is rejected without `graph_resumed`;
- resume runs neither a successful sibling nor a completed child node again.

- [ ] **Step 7: Verify and commit Task 12**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_subgraph_interrupt.py tests/integration/test_graph_runtime.py -v
uv run ruff check .
uv run pyright
```

Expected: all commands exit 0.

Commit:

```bash
git add assurance_agent/workflow/graph/handlers/subgraph.py assurance_agent/workflow/graph/handlers/interrupt.py assurance_agent/workflow/graph/runtime.py assurance_agent/workflow/graph/checkpoint.py assurance_agent/workflow/graph/task_runner.py assurance_agent/workflow/core/graph_events.py tests/unit/workflow/graph/test_subgraph_interrupt.py tests/integration/test_graph_runtime.py
git commit -m "feat(workflow): resume nested graph interrupts"
```

---

### Task 13: Add explicit validated checkpoint import

**Files:**
- Create: `tests/unit/workflow/graph/test_import_checkpoint.py`
- Modify: `assurance_agent/workflow/graph/checkpoint.py`
- Modify: `assurance_agent/workflow/graph/runtime.py`
- Modify: `assurance_agent/workflow/graph/models.py`

**Interfaces:**
- Consumes: compiled entrypoint topology, fixture locks, tree hashing, gate evaluator.
- Produces: `ImportManifest`; `GraphRuntime.import_checkpoint(schema: CompiledWorkflow, manifest: ImportManifest, context: RuntimeContext) -> ImportResult`.

- [ ] **Step 1: Define strict import models and parser tests**

```python
class ImportedGate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    verdict: str
    reads_sha256: dict[str, str]

class ImportedTask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    path: str
    graph: str
    node: str
    task_key: str | None = None
    outputs: dict[str, str] = Field(default_factory=dict)
    gate: ImportedGate | None = None

class ImportedBudget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    path: str
    budget_id: str
    consumption_id: str
    task_path: str

class ImportManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["2"]
    entrypoint: str
    source_kind: Literal["eval-fixture", "benchmark-seed", "v1-artifact-import"]
    fixture_id: str
    fixture_digest: str
    inputs: dict[str, str] = Field(default_factory=dict)
    completed: tuple[ImportedTask, ...] = ()
    budgets: tuple[ImportedBudget, ...] = ()
```

YAML normalization maps `source.kind`, `source.fixture_id`, and `source.fixture_digest` to the flat frozen model. Reject extra fields and ambiguous graph/node-only task references.

- [ ] **Step 2: Write import validation tests**

Cover valid input-only import, valid completed review/gate import, wrong fixture digest, output hash mismatch, unsafe path, impossible structural path, missing predecessor closure, imported budget consumer without matching budget event, gate verdict/hash mismatch, and fan-out child missing `task_key`.

Run: `uv run pytest tests/unit/workflow/graph/test_import_checkpoint.py -v`

Expected: FAIL because import models/runtime method do not exist.

- [ ] **Step 3: Implement structural-path resolution and closure validation**

Resolve from entrypoint root one node segment at a time. Each segment must be a real `graph:*` invocation edge; repeated named graphs remain distinct by path. For fan-out, require a task key present in a frozen expansion. A completed node is legal only when every active predecessor is imported, START-reachable for this run mode, or already strict-ledger complete.

- [ ] **Step 4: Re-evaluate gates and freeze provenance**

Verify fixture lock/digest and every input/output file hash. Input entries establish provenance only. For each imported gated task, evaluate the gate against the imported artifact view and require exact verdict/read hashes. Do not trust the manifest's verdict alone.

- [ ] **Step 5: Commit import atomically without fabricating attempts**

Start a new invocation, append `task_imported` entries, matching unique `budget_consumed` entries, and one `checkpoint_imported` in a single progression transaction. Do not append `task_attempt_started` or `task_attempt_succeeded`. Build the first checkpoint from the resulting strict ledger, then continue with `resume`.

- [ ] **Step 6: Verify and commit Task 13**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_import_checkpoint.py tests/integration/test_graph_runtime.py -v
uv run ruff check .
uv run pyright
```

Expected: all commands exit 0.

Commit:

```bash
git add assurance_agent/workflow/graph/checkpoint.py assurance_agent/workflow/graph/runtime.py assurance_agent/workflow/graph/models.py tests/unit/workflow/graph/test_import_checkpoint.py
git commit -m "feat(workflow): import explicit graph checkpoints"
```

---

### Task 14: Encode the complete canonical workflow and execution contracts

**Files:**
- Create: `assurance_agent/_resources/schemas/workflow-schema-v2.yaml`
- Create: `tests/unit/workflow/graph/test_canonical_schema_v2.py`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`

**Interfaces:**
- Consumes: the compiler and handler target names delivered by Tasks 1–13.
- Produces: one fully compiling canonical v2 workflow resource and a complete target contract catalog, still not selected by default until Task 15.

- [ ] **Step 1: Write the canonical inventory test before the YAML**

Assert exact graph IDs:

```python
EXPECTED_GRAPHS = {
    "bootstrap", "workflow", "intake-workflow", "execute-workflow", "intake",
    "case-review-cycle", "assurance", "api-branch", "api-plan-cycle",
    "e2e-branch", "e2e-plan-cycle", "fuzz-branch", "fuzz-plan-review-cycle",
    "performance-branch", "performance-plan-review-cycle", "healing",
}

def test_canonical_v2_compiles_with_all_targets() -> None:
    schema = load_workflow_v2(Path.cwd(), Path("assurance_agent/_resources/schemas/workflow-schema-v2.yaml"))
    contracts = load_execution_contracts(Path.cwd())
    compiled = compile_workflow(schema, contracts)
    assert set(compiled.graphs) == EXPECTED_GRAPHS
    assert set(compiled.entrypoints) == {"full", "intake", "execute", "case"}
```

Run: `uv run pytest tests/unit/workflow/graph/test_canonical_schema_v2.py -v`

Expected: FAIL because the canonical v2 resource does not exist and contracts are incomplete.

- [ ] **Step 2: Encode wrappers, intake, and bounded case repair**

Use the design spec's exact params/policies. `workflow`, `intake-workflow`, and `execute-workflow` all call `graph:bootstrap` first. Intake routes full/case-only through explore → case-design → case-review-cycle and review-case directly to case-review-cycle. Case review routes pass to END, needs_fix to fixer, needs_human_review to interrupt, reject/stop to STOP; only successful fixer consumes `max_case_fix_attempts`.

- [ ] **Step 3: Encode all four assurance branches and explicit all-active join**

The assurance graph activates API/E2E/Fuzz/Performance branches from frozen `test_types` and run mode. API and E2E each contain plan → bounded review/fix cycle → codegen. Fuzz and Performance contain plan → review/interrupt → codegen without an automatic fixer. `generation-join` uses `all_active`; execution never starts after only one active codegen sibling.

Add parametrized tests for full, api-only, e2e-only, plan-only, codegen-only and review-plan that assert the first two planned supersteps and active branch set.

- [ ] **Step 4: Encode healing as an ordinary bounded graph**

Use nodes entry, proposal, proposal-eligible, allocate, parallel fix-api/fix-e2e, all-active fixer-join, safety, safety-interrupt, rerun, reinspect, decide and four completion operations. Only allocate consumes `max_healing_attempts`. `accept_risk` routes safety-interrupt to rerun; `fix_and_proceed` routes to proposal; no path returns immediately to the same unresolved interrupt.

Add a compiler/runtime test for zero eligible proposals, API-only fixer, both fixers, safety interrupt accept-risk, resolved, exhausted and failed outcomes.

- [ ] **Step 5: Complete every target contract**

Contracts must cover these exact targets:

```text
skill:aa-explore
skill:aa-case-design
skill:aa-case-reviewer
skill:aa-case-fixer
skill:aa-fact-baseline
skill:aa-api-plan
skill:aa-api-plan-reviewer
skill:aa-api-plan-fixer
skill:aa-api-codegen
skill:aa-e2e-plan
skill:aa-e2e-plan-reviewer
skill:aa-e2e-plan-fixer
skill:aa-e2e-codegen
skill:aa-fuzz-plan
skill:aa-fuzz-plan-reviewer
skill:aa-fuzz-codegen
skill:aa-performance-plan
skill:aa-performance-plan-reviewer
skill:aa-performance-codegen
skill:aa-inspect
skill:aa-fix-proposal
skill:aa-api-codegen-fixer
skill:aa-e2e-codegen-fixer
skill:aa-report-generator
skill:aa-archive
operation:no-op
operation:skill-registry-check
operation:run-tests
operation:allocate-healing-attempt
operation:record-healing-status
operation:stop
builtin:join
builtin:gate
builtin:interrupt
```

Give every skill/operation concrete reads, writes, authorization writes, exclusive tokens, retryable error kinds and side-effect-free flag. API/E2E/Fuzz/Performance plan/codegen paths must be disjoint where their real outputs are disjoint. Run-tests holds `repo:test-runtime` exclusive and reads every selected test root.

- [ ] **Step 6: Assert canonical safety properties**

Tests must prove every cyclic SCC has a finite consumer/exhausted route, every gate verdict route is exhaustive/fail-closed, every interrupt action has a route, all node targets resolve, `test_types` branches can actually overlap, unknown resources do not occur in the packaged schema, and the schema/contract digest is stable across two wheel-resource loads.

- [ ] **Step 7: Verify and commit Task 14**

Run:

```bash
uv run pytest tests/unit/workflow/graph/test_canonical_schema_v2.py tests/unit/workflow/graph/test_compiler.py tests/unit/workflow/graph/test_contracts.py -v
uv run ruff check .
uv run pyright
```

Expected: all commands exit 0.

Commit:

```bash
git add assurance_agent/_resources/schemas/workflow-schema-v2.yaml assurance_agent/_resources/schemas/execution-contracts.yaml tests/unit/workflow/graph/test_canonical_schema_v2.py
git commit -m "feat(workflow): encode canonical graph workflow"
```

---

### Task 15: Cut CLI and driver ownership over to GraphRuntime

**Files:**
- Create: `assurance_agent/workflow/driver/runtime_factory.py`
- Create: `tests/integration/test_cli_workflow_v2.py`
- Modify: `assurance_agent/_resources/schemas/workflow-schema.yaml`
- Delete: `assurance_agent/_resources/schemas/workflow-schema-v2.yaml`
- Modify: `assurance_agent/commands/workflow_cmd.py`
- Modify: `assurance_agent/commands/status_cmd.py`
- Modify: `assurance_agent/commands/gate_cmd.py`
- Modify: `assurance_agent/commands/decide_cmd.py`
- Modify: `assurance_agent/workflow/driver/loop.py`
- Modify: `assurance_agent/workflow/driver/driver_state.py`
- Modify: `assurance_agent/workflow/driver/workflow_start.py`
- Modify: `tests/integration/test_cli_workflow.py`
- Modify: `tests/unit/driver/test_loop.py`
- Modify: `tests/unit/driver/test_driver_state.py`
- Modify: `tests/unit/driver/test_workflow_start.py`

**Interfaces:**
- Consumes: canonical compiled workflow, contracts, adapter implementations, GraphRuntime.
- Produces: `aa workflow run|status|resume|import-checkpoint`; top-level `aa status` graph projection; detached entrypoint launch.

- [ ] **Step 1: Create one runtime factory used by foreground, detached, eval, and tests**

```python
@dataclass(frozen=True)
class RuntimeBundle:
    runtime: GraphRuntime
    compiled: CompiledWorkflow


def build_graph_runtime(
    *,
    project_root: Path,
    change_id: str,
    adapter: AgentInvoker,
    explicit_schema: Path | None = None,
    clock: Clock | None = None,
) -> RuntimeBundle:
    loc = resolve_change(project_root, change_id)
    schema = load_workflow_v2(project_root, explicit_schema)
    contracts = load_execution_contracts(project_root)
    compiled = compile_workflow(schema, contracts)
    runtime_clock = clock or SystemClock()

    def resolve_pinned(digest: str) -> CompiledWorkflow:
        if digest != compiled.digest:
            raise GraphDefinitionChanged(
                f"requested graph digest {digest} does not match {compiled.digest}"
            )
        return compiled

    object_store = TreeStore(loc.path)
    checkpoints = CheckpointStore(loc.path)
    runner = build_default_node_runner(adapter, object_store, contracts)
    runtime = GraphRuntime(
        checkpoint_store=checkpoints,
        object_store=object_store,
        workspace_backend=WorkspaceBackend(loc.path),
        contracts=contracts,
        node_runner=runner,
        scheduler=Scheduler(
            checkpoints=checkpoints,
            object_store=object_store,
            clock=runtime_clock,
        ),
        schema_resolver=resolve_pinned,
        clock=runtime_clock,
    )
    return RuntimeBundle(runtime=runtime, compiled=compiled)
```

The factory passes the same clock instance to runtime and scheduler, and the resolver fails rather than returning a mismatched compiled schema.

- [ ] **Step 2: Replace the canonical resource atomically**

Move the fully tested v2 resource content into `workflow-schema.yaml` and remove the temporary v2 filename. `load_workflow_v2` now loads the packaged default. Add an integration assertion that `load_workflow_v2` from an installed-resource context returns version 2 and that `parse_schema` rejects the same file as incompatible v1 during the transition.

- [ ] **Step 3: Write new CLI surface tests**

Required commands and assertions:

```text
aa workflow run --change CH-1 --entrypoint full --params '{"run_mode":"full"}'
aa workflow status --change CH-1 --json
aa workflow resume --change CH-1
aa workflow resume --change CH-1 --interrupt INT --action accept_risk --reason approved
aa workflow import-checkpoint --change CH-1 --manifest import.yaml
```

Test exit 0 completed, 20 stopped, 30 interrupted, 40 schema/runtime error. Reject removed `--scope`, missing reason/who, invalid params, interrupt action on plain resume, and import without a manifest. Keep `workflow start` as the detached alias with `--entrypoint`.

- [ ] **Step 4: Implement workflow commands over the runtime factory**

`run` starts a new invocation only when none exists; otherwise it calls plain resume. `resume` with no command advances retry/abandoned work; with interrupt flags it builds `ResumeCommand`. `status` finds the latest root invocation from strict ledger, not `driver.json`. `import-checkpoint` parses YAML, invokes runtime import, and prints the new invocation/checkpoint IDs.

- [ ] **Step 5: Reduce the old loop to a temporary compatibility wrapper**

Retain `LoopResult(exit_code, reason)` for eval callers until Task 16, but change the function to:

```python
def run_workflow_loop(
    *,
    project_root: Path,
    change_id: str,
    entrypoint: str,
    adapter: AgentInvoker,
    params: dict[str, object] | None = None,
    explicit_schema: Path | None = None,
    parent_session_id: str | None = None,
) -> LoopResult:
    bundle = build_graph_runtime(
        project_root=project_root,
        change_id=change_id,
        adapter=adapter,
        explicit_schema=explicit_schema,
    )
    context = runtime_context_for(project_root, change_id, params or {}, parent_session_id)
    latest = bundle.runtime.latest_root_invocation()
    result = (
        bundle.runtime.run(bundle.compiled, entrypoint, context)
        if latest is None
        else bundle.runtime.resume(latest)
    )
    return LoopResult(exit_code=result.exit_code, reason=result.reason)
```

Delete `CliPhaseExecutor`, `DefaultCliPhaseExecutor`, `HealingActionExecutor`, `DefaultHealingActionExecutor`, `_DefaultStatusProvider`, and `_dispatch_entry`. No caller may inject either executor.

- [ ] **Step 6: Make driver.json a non-authoritative process pointer**

Replace `current_phase`, `current_attempt_id`, `paused_on`, `iteration`, and `last_checkpoint_at` with `invocation_id`, `checkpoint_id`, and `event_seq`. Start guard uses the latest root graph terminal plus process liveness. A stale/absent driver file never causes successful tasks to re-run.

- [ ] **Step 7: Project top-level status and frozen gates**

Top-level `aa status` prints `GraphStatus`; `--next --json` emits pending structural task IDs and interrupt metadata. It does not call v1 `compute_status`. `aa gate check --node-path <path>` returns the latest frozen gate report for that node task and refuses to re-adjudicate mutable files. `aa decide` rejects graph gate actions with guidance to `aa workflow resume`; it continues supporting `allow_test_changes` and any other non-graph policy decision still consumed outside GraphRuntime.

- [ ] **Step 8: Update detached launch and lock adoption**

Replace all `scope` fields/argv with `entrypoint`. The detached child adopts the same lock token, projects the invocation ID after start, and writes it to driver.json. A hard-killed child releases the OS lock; the next run reconciles leases from ledger.

- [ ] **Step 9: Verify and commit Task 15**

Run:

```bash
uv run pytest tests/integration/test_cli_workflow_v2.py tests/integration/test_cli_workflow.py tests/unit/driver -v
uv run ruff check .
uv run pyright
uv run lint-imports
```

Expected: all commands exit 0.

Commit:

```bash
git add -A assurance_agent/_resources/schemas/workflow-schema.yaml assurance_agent/_resources/schemas/workflow-schema-v2.yaml assurance_agent/commands/workflow_cmd.py assurance_agent/commands/status_cmd.py assurance_agent/commands/gate_cmd.py assurance_agent/commands/decide_cmd.py assurance_agent/workflow/driver tests/integration/test_cli_workflow_v2.py tests/integration/test_cli_workflow.py tests/unit/driver
git commit -m "feat(workflow): switch cli to graph runtime"
```

---

### Task 16: Migrate eval fixtures, benchmark resume, skills, and operator documentation

**Files:**
- Modify: `assurance_agent/eval/executor.py`
- Modify: `assurance_agent/eval/fixtures.py`
- Modify: `assurance_agent/eval/types.py`
- Modify: `tests/unit/eval/test_executor.py`
- Modify: `tests/unit/eval/test_fixtures.py`
- Modify: `tests/integration/test_eval_workflow_run_synth.py`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/*.yaml`
- Modify: `benchmark/vue-fastapi-admin/benchmark/run-workflow-loop.sh`
- Modify: `benchmark/vue-fastapi-admin/benchmark/run-workflow-loop-cursor.sh`
- Modify: `benchmark/vue-fastapi-admin/benchmark/run_with_hard_timeout.py`
- Modify: `assurance_agent/_resources/opencode/tools/workflow_start.ts`
- Modify: `assurance_agent/_resources/opencode/plugins/aa.mjs`
- Modify: `assurance_agent/_resources/skills/aa-workflow/SKILL.md`
- Replace: `assurance_agent/_resources/skills/aa-workflow/FALLBACK-RUNBOOK.md`
- Modify: `assurance_agent/_resources/skills/aa-intake/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-execute/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-archive/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-run/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-fix-proposal/SKILL.md`
- Modify: `README.md`
- Modify: `docs/eval.md`

**Interfaces:**
- Consumes: runtime factory, import manifest, workflow CLI.
- Produces: fixture-provenance imports, retry-safe benchmark loops, v2-only user/agent instructions.

- [ ] **Step 1: Extend fixture tiers with structural import declarations**

Add an optional model:

```python
class FixtureImportTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    graph: str
    node: str
    task_key: str | None = None
    outputs: list[str] = Field(default_factory=list)
    gate: str | None = None

class FixtureImportDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entrypoint: str
    inputs: list[str] = Field(default_factory=list)
    completed: list[FixtureImportTask] = Field(default_factory=list)

class TierManifest(BaseModel):
    name: str
    extends: str | None = None
    description: str = ""
    paths: list[str] = Field(default_factory=list)
    resets: FixtureResets = Field(default_factory=FixtureResets)
    source_prefix: str | None = None
    imports: dict[str, FixtureImportDef] = Field(default_factory=dict)
```

Merge imports through tier inheritance by entrypoint and structural task identity.

- [ ] **Step 2: Generate hash-complete import manifests during seeding**

`seed_change` returns `SeedResult(change_dir, import_manifest_path)`. For the requested entrypoint, hash fixture lock, input paths, completed outputs and gate reads after copy/reset, then write `.graph-runtime/import-manifest.yaml`. Missing declared source files fail seeding.

Tier declarations must cover:

- `case`: case design/review paths needed by L0 case replay;
- `execute`: case/fact inputs, API/E2E reviewed plans for L1, per-target codegen for L2, and execution/inspect evidence for L3;
- each fan-out import includes its task key;
- no tier imports a v1 `workflow-state.yaml` phase marker as authority.

- [ ] **Step 3: Replace eval loop injection with runtime invocation**

Remove `LoopRunner`, `CliPhaseExecutor`, `scope`, and `status_provider` from `execute_attempt`. Inject a `RuntimeFactory` for tests. After seeding, call `import_checkpoint` when a manifest exists, otherwise `run` the suite entrypoint. Preserve before/after write scan and scorer raw-output copying.

Map suite configuration `scope: case|full|execute` to `entrypoint: case|full|execute` while accepting only the new key after dataset migration. Set `params.run_mode`, `params.test_types`, and `params.run_tests` explicitly.

- [ ] **Step 4: Prove eval replay uses imported ledger events**

Update tests to assert `checkpoint_imported` and `task_imported` exist, no imported task has a physical attempt event, codegen-only starts only its unimported task, and forbidden-write evidence still reports actual project diffs. Run all ten workflow suites with synthetic adapters.

- [ ] **Step 5: Remove benchmark ledger surgery and use graph resume**

In both benchmark scripts:

- rename `DRIVER_SCOPE` to `DRIVER_ENTRYPOINT`;
- pass `--entrypoint`;
- delete `prune_stale_dispatches` and every direct rewrite of `events.jsonl`;
- read `pending_interrupts[]` from `aa workflow status --json`;
- auto-decision calls `aa workflow resume --interrupt <id> --action <action> --reason <text>`;
- hard-timeout retry calls plain `aa workflow resume`/`run`, relying on lease abandonment;
- retain `CURSOR_MAX_WORKFLOW_ATTEMPTS` only as a process-restart ceiling;
- assert a successful task ID appears exactly once across restarts.

- [ ] **Step 6: Update OpenCode plugin/tool inputs**

`workflow_start.ts` accepts `entrypoint: full | intake | execute | case` and emits `--entrypoint`; remove `scope`. Plugin help lists run, status, resume and import-checkpoint and no longer instructs phase agents to apply state.

- [ ] **Step 7: Replace fallback orchestration with a fail-closed v2 operator runbook**

The replacement `FALLBACK-RUNBOOK.md` must state:

1. GraphRuntime is the only progression writer; there is no hand-edited phase fallback.
2. Operators may inspect `aa workflow status`, wait for retry time, resume an expired attempt, resolve a listed interrupt, or run validated checkpoint import.
3. Operators never delete events, reset budgets, edit checkpoint JSON, mark a task complete from files, or run removed `aa state apply/heal` commands.
4. Recovery procedures cover live lease, expired lease, digest drift, artifact hash drift, workspace drift, retry exhaustion and corrupted checkpoint cache.
5. `allow_test_changes` remains a separate one-use `aa decide` policy path.

Update aa-workflow/intake/execute/archive/run/fix-proposal skills to the same command surface. Archive status is committed by the archive graph node. Healing status is a graph terminal operation. Human safety approval uses interrupt resume.

- [ ] **Step 8: Update README/eval docs and command-corpus tests**

Document schema v2, entrypoints, true parallelism, resource serialization, retry vs business budget, interrupt/resume, strict ledger authority, and fixture imports. Update command examples and integration command allowlists so no live instruction uses `--scope`, `aa state apply`, `aa state heal`, or graph-gate `aa decide`.

- [ ] **Step 9: Verify and commit Task 16**

Run:

```bash
uv run pytest tests/unit/eval tests/integration/test_eval_cli.py tests/integration/test_eval_workflow_run_synth.py tests/integration/test_readme_commands.py tests/unit/test_skills_content.py -v
uv run ruff check .
uv run pyright
uv run lint-imports
bash -n benchmark/vue-fastapi-admin/benchmark/run-workflow-loop.sh
bash -n benchmark/vue-fastapi-admin/benchmark/run-workflow-loop-cursor.sh
```

Expected: all commands exit 0.

Commit:

```bash
git add assurance_agent/eval tests/unit/eval tests/integration/test_eval_workflow_run_synth.py benchmark/vue-fastapi-admin/eval-fixtures/tiers benchmark/vue-fastapi-admin/benchmark assurance_agent/_resources/opencode assurance_agent/_resources/skills README.md docs/eval.md tests/integration/test_readme_commands.py tests/unit/test_skills_content.py
git commit -m "feat(workflow): migrate fixtures and runbooks to graphs"
```

---

### Task 17: Delete v1 orchestration and run the fault-injection acceptance gate

**Files:**
- Delete: `assurance_agent/workflow/orchestration/engine.py`
- Delete: `assurance_agent/workflow/orchestration/loop_registry.py`
- Delete: `assurance_agent/workflow/orchestration/healing_episode.py`
- Delete: `assurance_agent/workflow/orchestration/review_fix_episode.py`
- Delete: `tests/unit/test_engine.py`
- Delete: `tests/unit/orchestration/test_loop_registry.py`
- Delete: `tests/unit/test_healing_episode.py`
- Delete: `tests/unit/test_review_fix_episode.py`
- Delete: `tests/fixtures/healing-episode-schema.yaml`
- Create: `tests/integration/test_graph_runtime_faults.py`
- Modify: `assurance_agent/workflow/orchestration/schema.py`
- Modify: `assurance_agent/workflow/orchestration/operations.py`
- Modify: `assurance_agent/commands/state_cmd.py`
- Modify: `assurance_agent/workflow/driver/loop.py`
- Modify: `.importlinter`
- Modify: remaining tests importing v1 phase/loop symbols

**Interfaces:**
- Consumes: fully migrated GraphRuntime callers.
- Produces: no production v1 phase/loop scheduler or dual-executor seam; complete fault-injection and CI evidence.

- [ ] **Step 1: Prove there are no production consumers before deletion**

Run:

```bash
rg -n "compute_status|LoopRegistry|HealingEpisode|ReviewFix|CliPhaseExecutor|HealingActionExecutor|dispatch_signed|phase_outcome_committed" assurance_agent --glob '*.py'
```

Expected: only compatibility definitions scheduled for deletion, legacy event import/migration code explicitly named `v1`, or zero matches. Any live command/runtime consumer must be migrated before continuing.

- [ ] **Step 2: Remove v1 scheduler, loop projectors, executor seams, and state progression commands**

Delete the files listed above. Keep shared `Verdict`, `GateDef`, `ReadEntry`, gate normalization, and any non-graph decision/test-change policy operations. Remove `state apply` and `state heal`; keep `state configure` only if a non-running change still needs pre-run convenience, and make it refuse once `graph_invocation_started` exists because params are frozen.

Strip `parse_schema`, `WorkflowSchema`, `PhaseDef`, `LoopDef`, fan-out child phase helpers, and v1 validations from `orchestration/schema.py`. Remove old dispatch/outcome/healing-allocation event models only after v1-artifact import tests prove legacy files can be read as opaque source data without accepting them as new strict graph events.

- [ ] **Step 3: Update import-layer contracts**

Set workflow internal layers to:

```ini
[importlinter:contract:graph-runtime-layer]
name = driver depends on graph, graph depends on orchestration and core
type = layers
layers =
    assurance_agent.workflow.driver
    assurance_agent.workflow.graph
    assurance_agent.workflow.orchestration
    assurance_agent.workflow.core
```

`workflow.core` remains forbidden from importing `workflow.graph` or `workflow.orchestration`. Driver adapters implement the graph-owned `AgentInvoker` protocol; graph code does not import driver.

- [ ] **Step 4: Add process-kill fault tests at every persistence boundary**

Use subprocesses and synchronization files to kill at:

1. before/after `task_attempt_started`;
2. handler output before success event;
3. success/write-set transaction after one sibling;
4. budget success transaction;
5. interrupt event;
6. tree pointer/superstep commit;
7. canonical materialization;
8. checkpoint snapshot write;
9. heartbeat replacement.

For every case, start a fresh process and assert final terminal, exact attempt count, exact business budget count, no repeated successful task, no event deletion, and canonical tree matching the committed target tree.

- [ ] **Step 5: Add full acceptance assertions**

Tests must cover:

- API/E2E handlers overlap in real time;
- resource-conflicting tasks serialize in stable order without node resource declarations;
- unknown-resource tasks run alone;
- sibling A success/B failure resumes only B;
- graph interrupt with sibling success resumes from original namespace;
- healing safety accept-risk reaches rerun;
- review/healing budgets reconstruct from ledger after deleting all checkpoint/driver projections;
- fan-out IDs/expansion survive restart and source drift fails closed;
- explicit import works and bare artifacts do not complete a node;
- corrupted checkpoint/driver files rebuild from strict ledger;
- write-policy hard gate detects a forbidden task write;
- schema/contract digest drift refuses resume.

- [ ] **Step 6: Run focused and full repository verification**

Run:

```bash
uv run pytest tests/integration/test_graph_runtime.py tests/integration/test_graph_runtime_faults.py tests/integration/test_cli_workflow_v2.py -v
uv run pytest -v
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
bash scripts/packaging_smoke_test.sh
```

Expected: every command exits 0; pytest reports zero failures; packaging loads the packaged v2 schema and contract resources.

- [ ] **Step 7: Verify and commit Task 17 after v1 removal**

Run:

```bash
rg -n "schema_version: ['\"]1|kind: healing|kind: review_fix|--scope|aa state apply|aa state heal|prune_stale_dispatches" assurance_agent benchmark README.md docs/eval.md --glob '!**/migration/**'
```

Expected: zero live-runtime/instruction matches; any historical migration fixture match is explicitly allowlisted in its test.

Commit:

```bash
git add -A assurance_agent tests .importlinter benchmark README.md docs/eval.md
git commit -m "refactor(workflow): remove v1 phase loop runtime"
```

---

## Spec Coverage Checklist

| Design requirement | Implementation task(s) |
|---|---|
| Incompatible v2/top-level graphs | 1, 2, 14, 15, 17 |
| One GraphRuntime seam/no dual executor | 9, 11, 15, 17 |
| Typed state and deterministic reducers | 2, 6 |
| Named subgraphs and bounded cycles | 2, 7, 12, 14 |
| Dynamic frozen fan-out/map-reduce | 7 |
| True parallel ready tasks | 3, 5, 10 |
| Automatic resource serialization/no mandatory node claims | 3, 10, 14 |
| Private writes/pending successful siblings | 5, 10, 12 |
| Resume only failed/abandoned nodes | 4, 8, 10, 11 |
| Typed retry/backoff/timeout/lease | 1, 4, 8, 10 |
| Ledger-authoritative business budgets | 4, 7, 10, 14 |
| Checkpoint snapshots/schema pinning/rebuild | 2, 4, 5, 11 |
| Resumable audited interrupts | 4, 9, 12 |
| Explicit fixture import | 13, 16 |
| Canonical workflow/healing/review migration | 14 |
| CLI/status/detached process migration | 15 |
| Eval/benchmark/runbook migration | 16 |
| v1 deletion/fault injection/full acceptance | 17 |

## Execution Notes

- Tasks 1–14 are side-by-side additions and must keep the existing v1 default tests passing. Task 15 is the deliberate incompatible cutover.
- Do not combine Task 5 (write isolation), Task 10 (parallel scheduling), and Task 12 (subgraph interrupt) into one commit; each has a distinct recovery invariant and reviewer gate.
- Do not optimize the initial private workspace by weakening its tree/hash coverage. Measure after the benchmark passes; correctness and recoverability are the v2 acceptance boundary.
- At the end of each task, inspect `git status --short` before staging so `.history/` and unrelated user files remain untouched.
