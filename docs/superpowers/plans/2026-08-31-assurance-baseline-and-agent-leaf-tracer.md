# Assurance Baseline and Agent Leaf Tracer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 LangGraph 迁移前可独立落地的 Assurance 基线缺口，并在不引入 LangGraph 的前提下，把一个完全等价的四节点 Agent leaf 收敛为 `AgentLeafSpec` tracer。

**Architecture:** `graph_engine` 继续作为当前 Boot/Runtime，先补齐受认证配置快照、Workflow/Execution Contract 整体覆盖和显式 validator 解析；Feature wheel 继续拥有 task contract 与 validator Python；`assurance-product` 只在 Boot 阶段选择已认证数据、装配 slots、绑定 task contracts、编译并重建 lock。`AgentLeafSpec` 是纯声明到现有 `GraphDef` 的确定性展开，不引入第二套 scheduler，也不改变 Runtime 事件语义。

**Tech Stack:** Python 3.11、uv workspace、Pydantic v2、PyYAML、pytest、现有 graph-engine composition/compiler/planner/scheduler、Hatchling wheel packaging。

**Spec:** `docs/superpowers/specs/2026-08-31-langgraph-assurance-boot-runtime-design.md`

## Global Constraints

- 所有命令从仓库根目录 `/Users/lvqingquan/agent/assurance-agent` 运行；Python 工具命令均以 `uv run` 为前缀，使用 workspace 的 CPython 3.11。
- 实施前必须调用 `superpowers:using-git-worktrees`，从包含本 Spec/Plan 的提交创建隔离 worktree。当前主 worktree 有大量用户未提交修改；不得覆盖、清理、暂存或带入这些修改。
- 每个 Task 先写失败测试，再做最小实现，再跑聚焦测试，并独立提交。`git add` 必须逐个列出本 Task 文件，不能使用 `git add .`、`git add -A` 或通配整个仓库。
- YAML 只改变 Workflow walk 与数据型 execution contract。Python handler、validator、gate function、effect adapter 和 artifact model 只能来自已安装 wheel；项目/SUT 文件不得贡献或导入 Python。
- `.aa/workflow-schema.yaml` 与 `.aa/execution-contracts.yaml` 是两个固定、可选、整体替换文件。它们不需要也不允许写进项目 `plugin.yaml`，不注册 resource/capability，只作为 configuration source snapshot 的受认证附加字节。
- 受认证 optional file 的原始字节必须进入 configuration plugin 的 `SourceSnapshot` 和现有 `InvocationLock`；即使两份 YAML 解析后语义相同，字节变化也必须改变 source/lock digest。
- `AgentExecutionContract` 的 `prepare`、`execute`、`finalize` validator 集合必须全部显式。空集合法，但不能靠默认值或“未注册即为空”推断。
- 不得从 registry 中所有已注册 validator 反推 task 绑定。registry 证明“可用”，immutable task contract 才证明“此 task 应执行”。
- packaged baseline 只绑定已证明与完整 staged write set 兼容的 5 个历史关联：4 个 generation plan-review 和 improvement archive。其他 validator 保持已注册但显式未绑定；项目 override 只能在同一 authenticated Feature owner 内显式选择不同集合。
- `AgentLeafSpec` 只能展开到现有 `GraphDef`。本计划不新增 LangGraph dependency、LangGraph checkpoint、Attempt Kernel、Effect Kernel 编排或通用 backend abstraction。
- 不改变当前 ledger/runtime event schema、planner token/join 行为、effect settlement、CLI 命令、14 个公共 Entrypoint、99 个 agent alias、33 个 agent contract ID 或 Feature export 接口。
- 不以本计划完成作为 migration golden。Retro 与 improvement-evaluate 的真实生产契约仍是独立 spec 的阻断项。

---

## Scope Boundary

本计划包含：

1. 修正 Execution、Healing、Improvement、Quality 四个过期 capability-slot 测试，并冻结 99 alias composition 断言。
2. 为所有 agent phase 和 direct task 建立显式 validator-set 契约；绑定 5 个已验证的非空集合，并证明当前 commit path 会执行它们。
3. 实现 `.aa/workflow-schema.yaml` 和 `.aa/execution-contracts.yaml` 的受认证 whole-file selection、fallback、闭合校验与 digest binding。
4. 提取 `AgentLeafSpec -> GraphDef` 纯 builder，并只转换 `assurance-execution` 的 `execution-execute` 作为 tracer；`execution-run` 保持显式图作为同模块对照。

本计划明确不包含：

- Retro 与 improvement-evaluate 的 handler/public I/O 重设计；它们需要规格中承诺的单独 contract-repair spec。
- 修复 generated-files、codegen-fix、Quality、Improvement review/candidates/delivery validator 的旧路径或缺失语义上下文。
- 其余 29 个 exact four-node leaf 的批量转换。
- 9 个 `join:any`、activation epoch、`current_arrival`、`/tokens/0` 投影重写。
- `AssuranceAttemptKernel`、Effect protocol、LangGraph lowering、版本 registry、shadow/cutover 或旧 Runtime 删除。

## Baseline Evidence to Preserve

- 六个 `test_workflow_module.py` 的已核验基线为 4 failed / 117 passed；四个失败只来自 Execution、Healing、Improvement、Quality 仍断言 prepare/finalize concrete capability。
- 当前 25 个 commit validator 已注册，但所有 task node 解析后的 `validators` 都为空；registration 没有形成 execution binding。
- 当前共有 33 个 agent job contract，展开为 99 个 phase alias；同一 alias 可在多个 graph node occurrence 中出现。
- 历史 12 个 validator 关联中，当前只允许恢复以下 5 个：

| Prepare ID | Execute validator |
|---|---|
| `assurance.generation.api.plan-review.prepare` | `assurance.generation.validator.plan-mechanical.v1` |
| `assurance.generation.e2e.plan-review.prepare` | `assurance.generation.validator.plan-mechanical.v1` |
| `assurance.generation.fuzz.plan-review.prepare` | `assurance.generation.validator.plan-mechanical.v1` |
| `assurance.generation.performance.plan-review.prepare` | `assurance.generation.validator.plan-mechanical.v1` |
| `assurance.improvement.archive.prepare` | `assurance.improvement.validator.archive-integrity.v1` |

- `generated-files` 与 `codegen-fix-candidate` 会拒绝当前 wrapper/summary/manifest 的完整 staged set；Quality 和多数 Improvement validator 仍使用旧 root；`apply-problem-review` 已不在当前 Quality graph。不得在本计划内静默绑定它们。
- 64 个 graph 中 33 个含 prepare/finalize，其中 28 个是完全相同的 `prepare -> execute -> finalize -> done` 形状；本计划只转换其中一个。

## Target Interfaces

### Explicit task contracts

```python
class TaskPhaseContract(FrozenModel):
    validators: tuple[str, ...] = Field()


class AgentExecutionContract(FrozenModel):
    contract_id: str
    skill_id: str
    agent_profile: str
    resources: ResourceClaims | ResourceClaimTemplate
    prepare: TaskPhaseContract
    execute: TaskPhaseContract
    finalize: TaskPhaseContract
```

`TaskPhaseContract.validators` 无默认值，必须是无重复、按字典序排列的 qualified IDs。

Public signature: `bind_task_execution_contracts(workflow: WorkflowDef, *, contracts: Mapping[str, AgentExecutionContract], registries: RegistrySet) -> WorkflowDef`.

`contracts` 的 key 固定为 33 个 prepare capability ID。函数在 compile/lock 之前把 99 个 agent phase 的 validator set 注入 assembled Workflow；direct task 必须已在 Feature YAML 中显式写出 `validators`。

### Authenticated orchestration selection

```python
ORCHESTRATION_OPTIONAL_PATHS = (
    ".aa/execution-contracts.yaml",
    ".aa/workflow-schema.yaml",
)


class OrchestrationSelection(FrozenModel):
    workflow_module: WorkflowModuleDef
    execution_contracts: dict[str, AgentExecutionContract]
```

Public signatures:

- `configuration_tree_with_orchestration_overrides(source: ConfigTreePluginSource) -> ConfigTreePluginSource`
- `load_orchestration_selection(snapshot: SourceSnapshot) -> OrchestrationSelection`

Workflow override 固定保护以下接口字段：`schema_version`、`role`、`name`、`owner_id`、`module_id`、`module_version`、`imports`、`exports`、`capability_slots`、`schemas`、`resources`、`effects`。公共 entrypoint key set 必须相同，但 target graph 可以改变；`graphs`、`retry`、`timeout` 可整体替换并继续接受 assembler/compiler 的闭合校验。

Execution-contract override 的 33 个 key 以及每项 `contract_id`、`skill_id`、`agent_profile`、`resources` 必须与 installed Feature baseline 完全相同；只允许替换三个 phase 的显式 validator tuple。这样 project policy 可以选择已安装 validator，但不能改变 deployment wheel 已认证的 profile、skill、output route 或 resource authority。

`graph-inventory.yaml` 只冻结 packaged fallback，不是 project Workflow 的 graph/node allowlist。Boot 必须从 configuration `SourceSnapshot` 中是否存在受认证的 `.aa/workflow-schema.yaml` 得到 `InventoryPolicy`：packaged 模式严格对照静态 inventory；project-override 模式跳过 `uninventoried_nodes`，但仍执行 compiler closure、unreachable、dead-end、forbidden-target 与 missing-binding 检查。CLI 不接受用户传入或降级这个 policy。

### Engine-neutral leaf template

```python
class AgentLeafSpec(FrozenModel):
    kind: Literal["agent_leaf"]
    max_activations: int = Field(ge=1, le=10_000)
    prepare: NodeDef
    execute: NodeDef
    finalize: NodeDef
```

Builder signature: `build_agent_leaf_graph(spec: AgentLeafSpec) -> GraphDef`.

三个 phase 都必须是 `kind: task`。展开结果固定为 `start="prepare"`、四个 node（第四个为 `done: end`）和三条有序 edge；template 不接受自定义 start、done、edge、branch、join 或 interrupt。

## File Map

| Area | Production files | Focused tests |
|---|---|---|
| Optional source capture | `packages/framework/graph-engine/graph_engine/composition/{source_fs,declarative,resolver}.py` | `packages/framework/graph-engine/tests/composition/test_{source_fs,declarative_sources,registry_platform}.py` |
| Task contract model | `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/{execution_contract,__init__}.py` | `packages/adapters/agent-runtime-contracts/tests/test_models.py` |
| Feature contract ownership | six `packages/capabilities/assurance-*/assurance_*/contracts/workflow.py` and six `resources/workflow/module.yaml` | six Feature `tests/test_{contracts,workflow_module}.py` |
| Product binding | `packages/products/assurance-product/assurance_product/{agent_contracts,product}.py` | `tests/product/{product_runner,runtime_composition}.py`, `test_{agent_execution_contracts,commit_validator_bindings,graph_binding_audit,product_composition}.py` |
| Whole-file overrides | `assurance_product/{configuration,orchestration_overrides,product,cli}.py`, `resources/schemas/{workflow-schema,execution-contracts}.yaml` | `tests/product/test_{project_configuration,project_configuration_security,orchestration_overrides,composition_authority,full_graph_audit,cli_compile}.py` |
| Leaf template | `packages/framework/graph-engine/graph_engine/graph/{agent_leaf,module_schema,__init__}.py` | `packages/framework/graph-engine/tests/graph/test_{agent_leaf,workflow_module_schema}.py` |
| Tracer | `packages/capabilities/assurance-execution/assurance_execution/resources/workflow/module.yaml` | `packages/capabilities/assurance-execution/tests/test_workflow_module.py`, selected product Runtime tests |

### Task 1: Repair the four slot assertions and freeze all 99 phase aliases

**Files:**

- Modify: `packages/capabilities/assurance-execution/tests/test_workflow_module.py`
- Modify: `packages/capabilities/assurance-healing/tests/test_workflow_module.py`
- Modify: `packages/capabilities/assurance-improvement/tests/test_workflow_module.py`
- Modify: `packages/capabilities/assurance-quality/tests/test_workflow_module.py`
- Modify: `tests/product/test_graph_binding_audit.py`
- Modify: `tests/product/test_product_composition.py`

**Interfaces:**

- Consumes: existing six `WorkflowModuleDef` resources, `PREPARE_IDS`, and `tests.product.conformance.ALL_BINDING_IDS`.
- Produces: green slot-shape characterization in which every agent phase is a capability slot and assembled Product graphs reference the exact 99 deployment aliases.

- [ ] **Step 1: Reproduce the stale assertions.**

Run all six module suites before editing:

```bash
uv run pytest -q \
  packages/capabilities/assurance-intake/tests/test_workflow_module.py \
  packages/capabilities/assurance-generation/tests/test_workflow_module.py \
  packages/capabilities/assurance-execution/tests/test_workflow_module.py \
  packages/capabilities/assurance-healing/tests/test_workflow_module.py \
  packages/capabilities/assurance-improvement/tests/test_workflow_module.py \
  packages/capabilities/assurance-quality/tests/test_workflow_module.py
```

Expected baseline: exactly four failures in the Execution, Healing, Improvement and Quality slot-shape tests; prepare/finalize are actually slotted.

- [ ] **Step 2: Replace the four old tests with one three-phase invariant per wheel.**

Execution/Healing 使用 `_BASE_BY_GRAPH`，Improvement/Quality 使用 `_LEAF_GRAPHS`。四个文件分别保留自己的既有 catalog，并把旧测试体替换为：

```python
def test_agent_phase_nodes_use_declared_slots() -> None:
    module = _load_module()
    catalog = _BASE_BY_GRAPH  # Improvement/Quality 此行使用 _LEAF_GRAPHS
    for graph_id, base in catalog.items():
        nodes = module.graphs[graph_id].nodes
        for phase in ("prepare", "execute", "finalize"):
            node = nodes[phase]
            assert node.capability is None
            assert node.capability_slot == f"{base}.{phase}"
```

Do not change production YAML back to concrete Feature capability IDs.

- [ ] **Step 3: Tighten Product composition assertions.**

In `test_graph_binding_audit.py`, require the full alias set:

```python
agent_ids = {
    capability
    for capability in graph_capabilities
    if capability.startswith("assurance.product.agent.")
}
assert agent_ids == set(ALL_BINDING_IDS)
```

In `test_product_composition.py`, add:

```python
old_phase_ids = set(PREPARE_IDS) | {
    value.removesuffix(".prepare") + ".finalize" for value in PREPARE_IDS
}
assert graph_capabilities.isdisjoint(old_phase_ids)
```

- [ ] **Step 4: Run the repaired module and composition tests.**

```bash
uv run pytest -q \
  packages/capabilities/assurance-intake/tests/test_workflow_module.py \
  packages/capabilities/assurance-generation/tests/test_workflow_module.py \
  packages/capabilities/assurance-execution/tests/test_workflow_module.py \
  packages/capabilities/assurance-healing/tests/test_workflow_module.py \
  packages/capabilities/assurance-improvement/tests/test_workflow_module.py \
  packages/capabilities/assurance-quality/tests/test_workflow_module.py \
  tests/product/test_graph_binding_audit.py \
  tests/product/test_product_composition.py
```

Expected: all six module suites agree that all agent phases are slots; Product graph references exactly 99 agent aliases and no old direct prepare/finalize capability.

- [ ] **Step 5: Commit only these tests.**

```bash
git add \
  packages/capabilities/assurance-execution/tests/test_workflow_module.py \
  packages/capabilities/assurance-healing/tests/test_workflow_module.py \
  packages/capabilities/assurance-improvement/tests/test_workflow_module.py \
  packages/capabilities/assurance-quality/tests/test_workflow_module.py \
  tests/product/test_graph_binding_audit.py \
  tests/product/test_product_composition.py
git commit -m "test: align agent phase slot expectations"
```

### Task 2: Introduce the immutable phase-validator value object

**Files:**

- Modify: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/execution_contract.py`
- Modify: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/__init__.py`
- Modify: `packages/adapters/agent-runtime-contracts/tests/test_models.py`

**Interfaces:**

- Consumes: `graph_engine.plugin_api.FrozenModel`, `ResourceClaims`, `ResourceClaimTemplate`, and `graph_engine.identifiers.validate_qualified_id`.
- Produces: exported `TaskPhaseContract` only. `AgentExecutionContract` deliberately keeps its current four fields until Task 3 so this commit leaves all six Feature wheels importable and green.

- [ ] **Step 1: Write RED model tests.**

Add tests proving:

1. `TaskPhaseContract()` fails because `validators` is required.
2. duplicate, unsorted or unqualified validator IDs fail.
3. explicit `TaskPhaseContract(validators=())` succeeds and is frozen.

Start with these concrete RED cases:

```python
def test_task_phase_contract_requires_explicit_validators() -> None:
    with pytest.raises(ValidationError, match="validators"):
        TaskPhaseContract.model_validate({})


@pytest.mark.parametrize(
    "validators",
    [
        ("test.validator.z", "test.validator.a"),
        ("test.validator.a", "test.validator.a"),
        ("not-qualified",),
    ],
)
def test_task_phase_contract_rejects_noncanonical_ids(validators: tuple[str, ...]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        TaskPhaseContract(validators=validators)
```

Run:

```bash
uv run pytest -q packages/adapters/agent-runtime-contracts/tests/test_models.py -k "phase_contract or execution_contract"
```

Expected: the new tests fail because `TaskPhaseContract` does not exist.

- [ ] **Step 2: Implement the closed phase model.**

Use `graph_engine.identifiers.IdentifierError`、`validate_qualified_id` and this validator shape:

```python
class TaskPhaseContract(FrozenModel):
    validators: tuple[str, ...] = Field()

    @field_validator("validators")
    @classmethod
    def _validate_validators(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        try:
            normalized = tuple(validate_qualified_id(value) for value in values)
        except IdentifierError as error:
            raise ValueError(str(error)) from error
        if normalized != tuple(sorted(normalized)):
            raise ValueError("task phase validators must use canonical order")
        if len(set(normalized)) != len(normalized):
            raise ValueError("task phase validators must be unique")
        return normalized
```

Export `TaskPhaseContract` from package `__init__.py`. Do **not** add fields to `AgentExecutionContract` in this Task.

- [ ] **Step 3: Run adapter tests and static checks for the package.**

```bash
uv run pytest -q packages/adapters/agent-runtime-contracts/tests/test_models.py
uv run pyright packages/adapters/agent-runtime-contracts
uv run ruff check packages/adapters/agent-runtime-contracts
```

- [ ] **Step 4: Commit the shared contract.**

```bash
git add \
  packages/adapters/agent-runtime-contracts/agent_runtime_contracts/execution_contract.py \
  packages/adapters/agent-runtime-contracts/agent_runtime_contracts/__init__.py \
  packages/adapters/agent-runtime-contracts/tests/test_models.py
git commit -m "feat: add task phase validator contract"
```

### Task 3: Atomically require phases and declare all Feature validator policy

**Files:**

- Modify: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/execution_contract.py`
- Modify: `packages/adapters/agent-runtime-contracts/tests/test_models.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/contracts/workflow.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/contracts/workflow.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/contracts/workflow.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/contracts/workflow.py`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/contracts/workflow.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/contracts/workflow.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/resources/workflow/module.yaml`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/workflow/module.yaml`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/resources/workflow/module.yaml`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/resources/workflow/module.yaml`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/resources/workflow/module.yaml`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/resources/workflow/module.yaml`
- Modify: `packages/capabilities/assurance-intake/tests/test_contracts.py`
- Modify: `packages/capabilities/assurance-generation/tests/test_contracts.py`
- Modify: `packages/capabilities/assurance-execution/tests/test_contracts.py`
- Modify: `packages/capabilities/assurance-quality/tests/test_contracts.py`
- Modify: `packages/capabilities/assurance-healing/tests/test_contracts.py`
- Modify: `packages/capabilities/assurance-improvement/tests/test_contracts.py`
- Modify: `packages/capabilities/assurance-intake/tests/test_workflow_module.py`
- Modify: `packages/capabilities/assurance-generation/tests/test_workflow_module.py`
- Modify: `packages/capabilities/assurance-execution/tests/test_workflow_module.py`
- Modify: `packages/capabilities/assurance-quality/tests/test_workflow_module.py`
- Modify: `packages/capabilities/assurance-healing/tests/test_workflow_module.py`
- Modify: `packages/capabilities/assurance-improvement/tests/test_workflow_module.py`

**Interfaces:**

- Consumes: `TaskPhaseContract` from Task 2 and the current four-field `AgentExecutionContract`.
- Produces atomically: `AgentExecutionContract` with required `prepare`、`execute`、`finalize` fields; 33 Feature-owned contracts with explicit phase sets; the exact five non-empty packaged bindings; and explicit `validators: []` on every current direct task occurrence. No commit may expose the required fields before all 33 callers are updated.

- [ ] **Step 1: Add RED shared-model and catalog tests.**

In `tests/test_models.py`, require `AgentExecutionContract` construction to fail when any one of `prepare`、`execute`、`finalize` is absent, and add a successful case containing three explicit `TaskPhaseContract` values. In the six Feature contract suites, freeze this exact map and require every other agent phase to be explicitly empty:

```python
EXPECTED_EXECUTE_VALIDATORS = {
    "assurance.generation.api.plan-review.prepare": (
        "assurance.generation.validator.plan-mechanical.v1",
    ),
    "assurance.generation.e2e.plan-review.prepare": (
        "assurance.generation.validator.plan-mechanical.v1",
    ),
    "assurance.generation.fuzz.plan-review.prepare": (
        "assurance.generation.validator.plan-mechanical.v1",
    ),
    "assurance.generation.performance.plan-review.prepare": (
        "assurance.generation.validator.plan-mechanical.v1",
    ),
    "assurance.improvement.archive.prepare": (
        "assurance.improvement.validator.archive-integrity.v1",
    ),
}
```

For each of the 33 contracts assert `prepare.validators == ()`, `finalize.validators == ()`, and `execute.validators == EXPECTED_EXECUTE_VALIDATORS.get(prepare_id, ())`.

Run the shared model and six contract suites:

```bash
uv run pytest -q \
  packages/adapters/agent-runtime-contracts/tests/test_models.py \
  packages/capabilities/assurance-intake/tests/test_contracts.py \
  packages/capabilities/assurance-generation/tests/test_contracts.py \
  packages/capabilities/assurance-execution/tests/test_contracts.py \
  packages/capabilities/assurance-quality/tests/test_contracts.py \
  packages/capabilities/assurance-healing/tests/test_contracts.py \
  packages/capabilities/assurance-improvement/tests/test_contracts.py
```

Expected: new assertions fail because `AgentExecutionContract` has no phase fields. Existing imports must still succeed at this RED point.

- [ ] **Step 2: Add the three required fields and update all 33 callers in the same working change.**

Add these fields to `AgentExecutionContract`:

```python
prepare: TaskPhaseContract
execute: TaskPhaseContract
finalize: TaskPhaseContract
```

Each Feature 的既有 `_job` helper 必须构造全部三个 phase。新增下面的精确 helper，并只从两个 owning Feature 传入非空 execute validators：

```python
_NO_VALIDATORS = TaskPhaseContract(validators=())


def _phase_contracts(
    execute_validators: tuple[str, ...] = (),
) -> tuple[TaskPhaseContract, TaskPhaseContract, TaskPhaseContract]:
    return (
        _NO_VALIDATORS,
        TaskPhaseContract(validators=execute_validators),
        _NO_VALIDATORS,
    )
```

在每个既有 `_job` 内执行 `prepare, execute, finalize = _phase_contracts(execute_validators)`，并把三个命名值传给 `AgentExecutionContract`；其余既有 ID、skill、profile 与 resource 构造保持不变。

Generation assigns plan-mechanical only when `base.endswith(".plan-review")`; Improvement assigns archive-integrity only for `base == "archive"`. Intake, Execution, Healing, Quality and all other jobs explicitly use empty tuples.

- [ ] **Step 3: Make every direct task occurrence explicit in Feature YAML.**

Add `validators: []` to exactly these current direct task nodes; do not add a non-empty set:

```yaml
kind: task
capability: assurance.generation.complete
validators: []
```

| Module | Graph/node occurrences |
|---|---|
| Generation | `generation/complete`; four `*/plan-review-round-advance`; four `*/plan-review-round-advance-retry`; `generation-api/codegen-round-advance`; `generation-e2e/codegen-round-advance` |
| Healing | `healing-fix-proposal/repair-round-advance`; `healing-coverage-repair/repair-round-advance` |
| Intake | `entry/review-round-advance`; `entry/review-round-advance-retry`; `entry/review-round-advance-rework-retry` |
| Improvement | `retro/collect`; `retro/reconcile`; `improvement-evaluate/evaluate`; `improvement-export/export`; `improvement-apply/apply-auto-review`; `improvement-apply/apply-human-review`; `improvement-apply/evaluate`; `improvement-apply/apply`; `improvement-rollback/rollback` |

Execution and Quality currently have no direct task occurrence, so only their agent contract catalogs change.

- [ ] **Step 4: Add module tests for direct-task explicitness.**

Load raw YAML as well as the parsed module. For every raw `kind: task` without `capability_slot`, assert the key `validators` exists; for every parsed direct task, assert its tuple is empty in this baseline. Do not require slot nodes to duplicate agent-contract validators in YAML.

- [ ] **Step 5: Run all Feature contract/module suites.**

```bash
uv run pytest -q \
  packages/adapters/agent-runtime-contracts/tests/test_models.py \
  packages/capabilities/assurance-intake/tests/test_contracts.py \
  packages/capabilities/assurance-generation/tests/test_contracts.py \
  packages/capabilities/assurance-execution/tests/test_contracts.py \
  packages/capabilities/assurance-quality/tests/test_contracts.py \
  packages/capabilities/assurance-healing/tests/test_contracts.py \
  packages/capabilities/assurance-improvement/tests/test_contracts.py \
  packages/capabilities/assurance-intake/tests/test_workflow_module.py \
  packages/capabilities/assurance-generation/tests/test_workflow_module.py \
  packages/capabilities/assurance-execution/tests/test_workflow_module.py \
  packages/capabilities/assurance-quality/tests/test_workflow_module.py \
  packages/capabilities/assurance-healing/tests/test_workflow_module.py \
  packages/capabilities/assurance-improvement/tests/test_workflow_module.py
```

- [ ] **Step 6: Commit Feature-owned policy.**

```bash
git add \
  packages/adapters/agent-runtime-contracts/agent_runtime_contracts/execution_contract.py \
  packages/adapters/agent-runtime-contracts/tests/test_models.py \
  packages/capabilities/assurance-intake/assurance_intake/contracts/workflow.py \
  packages/capabilities/assurance-generation/assurance_generation/contracts/workflow.py \
  packages/capabilities/assurance-execution/assurance_execution/contracts/workflow.py \
  packages/capabilities/assurance-quality/assurance_quality/contracts/workflow.py \
  packages/capabilities/assurance-healing/assurance_healing/contracts/workflow.py \
  packages/capabilities/assurance-improvement/assurance_improvement/contracts/workflow.py \
  packages/capabilities/assurance-intake/assurance_intake/resources/workflow/module.yaml \
  packages/capabilities/assurance-generation/assurance_generation/resources/workflow/module.yaml \
  packages/capabilities/assurance-execution/assurance_execution/resources/workflow/module.yaml \
  packages/capabilities/assurance-quality/assurance_quality/resources/workflow/module.yaml \
  packages/capabilities/assurance-healing/assurance_healing/resources/workflow/module.yaml \
  packages/capabilities/assurance-improvement/assurance_improvement/resources/workflow/module.yaml \
  packages/capabilities/assurance-intake/tests/test_contracts.py \
  packages/capabilities/assurance-generation/tests/test_contracts.py \
  packages/capabilities/assurance-execution/tests/test_contracts.py \
  packages/capabilities/assurance-quality/tests/test_contracts.py \
  packages/capabilities/assurance-healing/tests/test_contracts.py \
  packages/capabilities/assurance-improvement/tests/test_contracts.py \
  packages/capabilities/assurance-intake/tests/test_workflow_module.py \
  packages/capabilities/assurance-generation/tests/test_workflow_module.py \
  packages/capabilities/assurance-execution/tests/test_workflow_module.py \
  packages/capabilities/assurance-quality/tests/test_workflow_module.py \
  packages/capabilities/assurance-healing/tests/test_workflow_module.py \
  packages/capabilities/assurance-improvement/tests/test_workflow_module.py
git commit -m "feat: require explicit feature task validators"
```

### Task 4: Bind task contracts before compile and prove commit validation

**Files:**

- Modify: `packages/products/assurance-product/assurance_product/agent_contracts.py`
- Modify: `packages/products/assurance-product/assurance_product/product.py`
- Modify: `tests/product/product_runner.py`
- Modify: `tests/product/runtime_composition.py`
- Modify: `tests/product/test_agent_execution_contracts.py`
- Create: `tests/product/test_commit_validator_bindings.py`

**Interfaces:**

- Consumes: `Mapping[str, AgentExecutionContract]` from Task 3, assembled `WorkflowDef`, and authenticated `RegistrySet`.
- Produces: `bind_task_execution_contracts(workflow: WorkflowDef, *, contracts: Mapping[str, AgentExecutionContract], registries: RegistrySet) -> WorkflowDef` and a current-scheduler commit receipt proof.

- [ ] **Step 1: Write RED binding-closure tests.**

Cover all of these failures before `compile_workflow()`:

- missing or extra prepare contract;
- missing or extra agent phase alias in the assembled Workflow;
- pre-populated `validators` on an agent alias, which would create two authorities;
- direct task with no explicit `validators` field;
- unknown validator ID;
- validator owned by a different Feature;
- unqualified, duplicate or non-canonical validator tuple on a direct task or project-selected Workflow. `NodeDef.validators` does not currently enforce this, so the binder must.

Also assert the successful resolved Workflow has exactly the five non-empty execute bindings from Task 3 and explicit empty tuples everywhere else.

For each of those five bindings, obtain the owning `AgentExecutionContract`, resolve its `ResourceClaimTemplate` with `{"workspace": {"scope_id": "CH-VALIDATOR-001"}}`, build a staged write set containing **every** resolved write path, and call the actual installed registry validator. Assert the complete declared staged set is accepted. This is the compatibility characterization that permits the five bindings; Step 5 separately proves one configured ID travels through Scheduler commit.

The default-closure test begins from the unbound assembled Workflow, not the already compiled result:

```python
def test_task_contract_binding_resolves_the_exact_default_validator_set(installed_sources) -> None:
    composition = resolve_assurance_composition(
        request_for("opencode", installed_sources)
    )
    assembled = assemble_product_workflow(
        manifest=composition.manifest,
        descriptors={item.plugin_id: item for item in composition.descriptors},
        registries=composition.registries,
    )
    bound = bind_task_execution_contracts(
        assembled,
        contracts=AGENT_EXECUTION_CONTRACTS,
        registries=composition.registries,
    )
    nonempty = {
        node.capability: node.validators
        for graph in bound.graphs.values()
        for node in graph.nodes.values()
        if node.capability is not None and node.validators
    }
    assert nonempty == EXPECTED_BOUND_VALIDATORS
```

Define the expected value exactly as:

```python
EXPECTED_BOUND_VALIDATORS = {
    "assurance.product.agent.generation.api.plan-review.execute": (
        "assurance.generation.validator.plan-mechanical.v1",
    ),
    "assurance.product.agent.generation.e2e.plan-review.execute": (
        "assurance.generation.validator.plan-mechanical.v1",
    ),
    "assurance.product.agent.generation.fuzz.plan-review.execute": (
        "assurance.generation.validator.plan-mechanical.v1",
    ),
    "assurance.product.agent.generation.performance.plan-review.execute": (
        "assurance.generation.validator.plan-mechanical.v1",
    ),
    "assurance.product.agent.improvement.archive.execute": (
        "assurance.improvement.validator.archive-integrity.v1",
    ),
}
```

- [ ] **Step 2: Replace the old binder with `bind_task_execution_contracts`.**

Preserve current execute-phase resource/retry injection, and add phase validator injection. Resolve aliases by `assurance.product.agent.<feature>.<base>.<phase>`, map them back to prepare IDs, and select `getattr(contract, phase)`.

For every validator ID, require:

```python
entry = registries.capabilities.entries.get(validator_id)
if not isinstance(entry, CommitValidatorEntry):
    raise ValueError(f"unknown task validator: {validator_id}")
if entry.owner_id != feature_owner:
    raise ValueError(f"task validator owner drifted: {validator_id}")
```

For direct task nodes, derive `feature_owner` from the concrete capability prefix and validate their explicit tuple against the same authenticated registry. After binding, every task `NodeDef` must have `"validators" in node.model_fields_set`.

Because `NodeDef` itself accepts arbitrary validator tuples, perform the same canonical checks here as `TaskPhaseContract`: every ID passes `validate_qualified_id`, the tuple equals `tuple(sorted(values))`, and `len(set(values)) == len(values)`. This validation applies to direct tasks and selected project Workflow data before registry lookup.

- [ ] **Step 3: Pass registries and contracts at the existing Boot seam.**

Change `_apply_agent_execution_contracts()` to call:

```python
bound = bind_task_execution_contracts(
    assembled,
    contracts=AGENT_EXECUTION_CONTRACTS,
    registries=composition.registries,
)
workflow = compile_workflow(bound, composition.registries)
```

Keep ordering exactly `assemble -> bind -> compile -> build_invocation_lock -> FrozenComposition.freeze`.

- [ ] **Step 4: Keep the Product Runtime harness closed under the new validator IDs.**

Update `tests/product/product_runner.py::assemble_bound_product_workflow()` to call the new binder with `AGENT_EXECUTION_CONTRACTS` and the registries from the real `resolve_assurance_composition(request_for("opencode", installed_sources))`; remove the current `try/except ValueError` fallback, because a binding failure must fail the test. Do not use `modular_product_composition()` as assembler input: its manifest is the synthetic inline-workflow form.

The synthetic `tests/product/runtime_composition.py::resolve_workflow_composition()` currently declares `commit_validators=()` for every generated plugin. Extend this test harness as follows:

1. collect the exact validator IDs referenced by parsed Workflow task nodes;
2. group those IDs with `_owner_for()` and include them in each generated `PluginDescriptor.commit_validators`;
3. generate an `_AcceptingValidator` whose `validate()` returns `ValidationResult(accepted=True)` and contribute one instance for every declared ID;
4. include validator owners when computing the generated plugin owner set;
5. assert in a Product test that the synthetic registry contains exactly the referenced validator IDs.

These pass-through validators exist only in the Runtime test composition, so branch/projection tests remain focused on graph behavior. Production Boot continues using installed Feature validators, and Step 5 proves one of those real validators reaches Scheduler commit.

- [ ] **Step 5: Add a current-Runtime commit-path proof.**

In `test_commit_validator_bindings.py`:

1. resolve the real Product composition;
2. obtain one compiled generation plan-review execute node and assert its `definition.validators` contains plan-mechanical;
3. use that resolved node’s `resources` and validator tuple to construct the `PlannedTask` passed to the existing `Scheduler` test seam;
4. use the actual authenticated `CommitValidatorEntry.validator` from the composition registry;
5. stage both declared outputs, `qa/changes/CH-VALIDATOR-001/review/api-plan-review.json` and `qa/changes/CH-VALIDATOR-001/review/api-plan-review-summary.md`, and assert `AttemptResult.commit.receipts` contains one accepted receipt with the exact validator ID;
6. retain the existing scheduler rejection test as the proof that rejection never advances head or emits success.

The new test must observe the configured ID on the planned task and the same installed validator instance at commit; a direct unit call to `validator.validate()` alone is insufficient.

- [ ] **Step 6: Run product binding and scheduler tests.**

```bash
uv run pytest -q \
  tests/product/test_agent_execution_contracts.py \
  tests/product/test_commit_validator_bindings.py \
  tests/product/test_graph_binding_audit.py \
  tests/product/test_product_composition.py \
  tests/product/test_generation_branches.py \
  tests/product/test_replay_properties.py \
  packages/framework/graph-engine/tests/runtime/test_scheduler.py::test_validator_rejection_or_exception_never_moves_head_or_emits_success
```

- [ ] **Step 7: Commit Boot binding and test-harness closure.**

```bash
git add \
  packages/products/assurance-product/assurance_product/agent_contracts.py \
  packages/products/assurance-product/assurance_product/product.py \
  tests/product/product_runner.py \
  tests/product/runtime_composition.py \
  tests/product/test_agent_execution_contracts.py \
  tests/product/test_commit_validator_bindings.py
git commit -m "feat: bind authenticated task validators"
```

### Task 5: Capture fixed optional files inside the authenticated config snapshot

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/composition/source_fs.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/declarative.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/resolver.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_source_fs.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_declarative_sources.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_registry_platform.py`

**Interfaces:**

- Consumes: existing descriptor-relative `capture_declared_tree`, `ConfigTreePluginSource`, `SourceSnapshot`, and resolver source-reference identity.
- Produces: `ConfigTreePluginSource.optional_file_paths` plus secure optional capture in the same immutable source snapshot, without contributing those files to registries.

- [ ] **Step 1: Write RED source-capture tests.**

Test these exact cases:

- both optional files absent: required declared tree still loads;
- either or both optional files present: snapshot contains their exact bytes in canonical path order;
- an ambient third file still fails closed;
- optional path colliding with `plugin.yaml` or a declared resource fails;
- unsafe, duplicate or non-canonical optional paths fail model validation;
- changing only optional bytes changes `SourceSnapshot.digest`;
- two source references with different `optional_file_paths` are not deduplicated as the same resolver input.

The present-file RED case is:

```python
def test_config_tree_snapshot_includes_allowlisted_optional_bytes(tmp_path: Path) -> None:
    _write_plugin_tree(tmp_path)
    override = tmp_path / ".aa" / "workflow-schema.yaml"
    override.parent.mkdir()
    override.write_text("schema_version: '1'\n", encoding="utf-8")
    loaded = load_config_tree(
        ConfigTreePluginSource(
            path=tmp_path,
            optional_file_paths=(".aa/workflow-schema.yaml",),
        )
    )
    files = {item.path: item.content for item in loaded.snapshot.files}
    assert files[".aa/workflow-schema.yaml"] == b"schema_version: '1'\n"
```

- [ ] **Step 2: Extend physical tree capture without a TOCTOU pre-check.**

Change the public signature to `capture_declared_tree(root: Path, files: tuple[str, ...], policy: DeclaredTreePolicy, *, optional_files: tuple[str, ...] = ()) -> SourceSnapshot`.

Inside the already-open root descriptor, enumerate once, require all required files, allow only the required/optional union, capture exactly the optional paths that were present in that enumeration, then rescan and require the tree state to be unchanged. Do not call `Path.exists()` or `Path.is_file()` before secure descriptor-relative capture.

The set decision occurs against the descriptor-relative enumeration:

```python
required = frozenset(_validate_closed_file_list(files))
optional = frozenset(_validate_closed_file_list(optional_files))
if required & optional:
    raise SourceSnapshotError("required and optional source files overlap")
before_tree = _enumerate_regular_tree(root_fd, policy)
present = before_tree.file_names
if not required.issubset(present) or not present.issubset(required | optional):
    raise SourceSnapshotError("declared source file set does not match the physical tree")
captured_paths = tuple(sorted(present))
captured = tuple(_read_stable_file_at(root_fd, path, policy) for path in captured_paths)
```

- [ ] **Step 3: Extend `ConfigTreePluginSource`.**

Add `optional_file_paths: tuple[str, ...] = ()` with canonical-relative, sorted, unique validation. In `load_config_tree()`:

- reject collision with `plugin.yaml` and `probe_document.files`;
- call `capture_declared_tree(source.path, (_PLUGIN_MANIFEST, *declared_paths), DeclaredTreePolicy.config_tree(), optional_files=source.optional_file_paths)`;
- keep optional files out of `DeclarativePluginDocument.files` and therefore out of contributed resources/capabilities.

Update resolver `_source_reference_key()` to append `*source.optional_file_paths`.

- [ ] **Step 4: Run framework composition tests.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/composition/test_source_fs.py \
  packages/framework/graph-engine/tests/composition/test_declarative_sources.py \
  packages/framework/graph-engine/tests/composition/test_registry_platform.py
uv run pyright packages/framework/graph-engine/graph_engine/composition
```

- [ ] **Step 5: Commit optional snapshot support.**

```bash
git add \
  packages/framework/graph-engine/graph_engine/composition/source_fs.py \
  packages/framework/graph-engine/graph_engine/composition/declarative.py \
  packages/framework/graph-engine/graph_engine/composition/resolver.py \
  packages/framework/graph-engine/tests/composition/test_source_fs.py \
  packages/framework/graph-engine/tests/composition/test_declarative_sources.py \
  packages/framework/graph-engine/tests/composition/test_registry_platform.py
git commit -m "feat: authenticate optional config tree files"
```

### Task 6: Add closed packaged fallbacks and override parsers

**Files:**

- Modify: `packages/products/assurance-product/assurance_product/configuration.py`
- Modify: `packages/products/assurance-product/assurance_product/product.py`
- Create: `packages/products/assurance-product/assurance_product/orchestration_overrides.py`
- Move: `packages/products/assurance-product/assurance_product/resources/workflow/main.yaml` to `packages/products/assurance-product/assurance_product/resources/schemas/workflow-schema.yaml`
- Create: `packages/products/assurance-product/assurance_product/resources/schemas/execution-contracts.yaml`
- Modify: `docs/runbooks/assurance-modular-workflow-rollout.md`
- Create: `tests/product/test_orchestration_overrides.py`
- Modify: `tests/product/test_project_configuration.py`
- Modify: `tests/product/test_project_configuration_security.py`
- Modify: `tests/product/test_workflow_module_security.py`

**Interfaces:**

- Consumes: optional source bytes from Task 5, task contract models from Task 2, and installed `AGENT_EXECUTION_CONTRACTS` from Task 3.
- Produces: `ORCHESTRATION_OPTIONAL_PATHS`, `OrchestrationSelection`, `configuration_tree_with_orchestration_overrides()`, and `load_orchestration_selection()` with packaged fallback parity.

- [ ] **Step 1: Write RED parser/fallback tests.**

Require:

- no optional files selects both packaged fallbacks;
- a project Workflow file may change graph topology/policies while keeping the protected interface and entrypoint key set;
- changing any protected field, adding/removing an entrypoint name, using duplicate YAML keys, unsafe tags, executable declaration keys or non-JSON cyclic data fails closed;
- a project execution-contract file must contain exactly 33 prepare keys and all three phase validator tuples;
- contract key, ID, skill, profile or resources drift fails;
- validator tuple changes parse successfully here and are resolved against the authenticated registry in Task 7;
- optional files do not increase the four business `ResourceContribution`s and do not create handlers, validators, effects or bindings.

The fallback RED test is:

```python
def test_absent_project_overrides_select_packaged_fallbacks(installed_sources) -> None:
    source = configuration_tree_with_orchestration_overrides(
        installed_sources.configuration_tree
    )
    snapshot = load_config_tree(source).snapshot
    selection = load_orchestration_selection(snapshot)
    assert selection.workflow_module == load_product_workflow_module()
    assert selection.execution_contracts == dict(AGENT_EXECUTION_CONTRACTS)
```

- [ ] **Step 2: Make the packaged Workflow fallback canonical.**

Move the current Product root module to `resources/schemas/workflow-schema.yaml`; update `load_product_workflow_module()` to read that path. Do not keep a second `main.yaml` copy. Parsed Product declaration content must remain identical before any project override.

Update `test_workflow_module_security.py` so its source and wheel inventory assertions require exactly one Product Workflow YAML at `assurance_product/resources/schemas/workflow-schema.yaml`, while continuing to reject the deleted monolith. Do not weaken the assertion to accept any YAML under `resources/schemas/`.

Update the tracked rollout runbook from `resources/workflow/main.yaml` to `resources/schemas/workflow-schema.yaml`. Do not force-add ignored local documentation that is absent from the Git base; historical specs, plans, and research remain immutable records and are not rewritten.

- [ ] **Step 3: Define the execution-contract document.**

Use this exact YAML shape for all 33 prepare IDs:

```yaml
schema_version: "1"
contracts:
  assurance.generation.api.plan-review.prepare:
    contract_id: assurance.generation.agent.api.plan-review.v1
    skill_id: aa-api-plan-reviewer
    agent_profile: assurance-v1-reviewer
    resources:
      parameters: {change_id: /workspace/scope_id}
      reads: [qa]
      writes:
        - qa/changes/{change_id}/review/api-plan-review-summary.md
        - qa/changes/{change_id}/review/api-plan-review.json
    prepare: {validators: []}
    execute:
      validators: [assurance.generation.validator.plan-mechanical.v1]
    finalize: {validators: []}
```

Serialize the remaining contracts from the installed Feature catalog with the same fields and canonical ordering. Add a parity test that compares the parsed packaged document to `AGENT_EXECUTION_CONTRACTS`; this makes any future catalog/file drift a test failure rather than choosing one silently.

- [ ] **Step 4: Implement orchestration selection.**

`configuration_tree_with_orchestration_overrides()` must replace an empty optional list with exactly `ORCHESTRATION_OPTIONAL_PATHS` and reject any caller-supplied different list.

`load_orchestration_selection(snapshot)` must read optional bytes only through `snapshot.files`, never reopen the project path. If absent, read the installed package fallback. Parse Workflow with `parse_workflow_module`; parse execution contracts into the frozen shared models; enforce the protected-field and installed-baseline comparisons described in Target Interfaces.

Before typed parsing, validate each project YAML as closed data: compose with `yaml.SafeLoader`, reuse duplicate-key rejection, allow shared anchors but reject recursion cycles with an active recursion stack, require string-keyed JSON-compatible mappings, and recursively reject executable keys `python`、`callable`、`command`、`commands`、`install`、`installer`、`module`、`shell`、`template`、`templates`。Workflow 只允许顶层 structural `imports` 字段；execution contracts 不允许任何 `imports` 字段。不得通过 project path、Python import 或动态 callable 解释这些值。

Use a recursion-stack validator so a shared anchor is data but a cycle is rejected:

```python
_EXECUTABLE_KEYS = frozenset(
    {
        "callable", "command", "commands", "import", "imports", "install",
        "installer", "module", "python", "shell", "template", "templates",
    }
)


def _validate_closed_value(
    value: object,
    *,
    label: str,
    allow_top_level_imports: bool,
    path: tuple[str, ...] = (),
    active: set[int] | None = None,
) -> None:
    active = set() if active is None else active
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number")
        return
    if isinstance(value, (dict, list)):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{label} contains a YAML cycle")
        active.add(identity)
        children = value.items() if isinstance(value, dict) else enumerate(value)
        for key, child in children:
            if isinstance(value, dict) and not isinstance(key, str):
                raise ValueError(f"{label} must use string mapping keys")
            child_path = (*path, key) if isinstance(key, str) else path
            if isinstance(key, str) and key.casefold() in _EXECUTABLE_KEYS:
                allowed = allow_top_level_imports and child_path == ("imports",)
                if not allowed:
                    raise ValueError(f"{label} contains executable declaration: {key}")
            _validate_closed_value(
                child,
                label=label,
                allow_top_level_imports=allow_top_level_imports,
                path=child_path,
                active=active,
            )
        active.remove(identity)
        return
    raise ValueError(f"{label} contains a non-JSON data value")
```

- [ ] **Step 5: Normalize config loading and preserve business contribution closure.**

`load_project_configuration()` must call `configuration_tree_with_orchestration_overrides()` before `load_config_tree()`. Replace its exact file-set check with:

```python
required = _REQUIRED_FILES
allowed = required | set(ORCHESTRATION_OPTIONAL_PATHS)
if not required.issubset(files) or not set(files).issubset(allowed):
    raise ProjectConfigurationError("unknown or missing project configuration files")
```

Continue constructing exactly the existing four `ResourceContribution`s; orchestration files are source authority inputs, not registry resources.

- [ ] **Step 6: Run override/security tests and package checks.**

```bash
uv run pytest -q \
  tests/product/test_orchestration_overrides.py \
  tests/product/test_project_configuration.py \
  tests/product/test_project_configuration_security.py \
  tests/product/test_workflow_module_security.py \
  tests/product/test_product_providers.py
uv run pyright packages/products/assurance-product/assurance_product
```

- [ ] **Step 7: Commit fallback and parser work.**

```bash
git add \
  packages/products/assurance-product/assurance_product/configuration.py \
  packages/products/assurance-product/assurance_product/product.py \
  packages/products/assurance-product/assurance_product/orchestration_overrides.py \
  packages/products/assurance-product/assurance_product/resources/workflow/main.yaml \
  packages/products/assurance-product/assurance_product/resources/schemas/workflow-schema.yaml \
  packages/products/assurance-product/assurance_product/resources/schemas/execution-contracts.yaml \
  docs/runbooks/assurance-modular-workflow-rollout.md \
  tests/product/test_orchestration_overrides.py \
  tests/product/test_project_configuration.py \
  tests/product/test_project_configuration_security.py \
  tests/product/test_workflow_module_security.py
git commit -m "feat: parse authenticated orchestration overrides"
```

### Task 7: Apply selected Workflow and execution contracts during Boot

**Files:**

- Modify: `packages/products/assurance-product/assurance_product/product.py`
- Modify: `packages/products/assurance-product/assurance_product/agent_contracts.py`
- Modify: `packages/products/assurance-product/assurance_product/cli.py`
- Modify: `tests/product/test_orchestration_overrides.py`
- Modify: `tests/product/test_composition_authority.py`
- Modify: `tests/product/test_product_composition.py`
- Modify: `tests/product/test_agent_execution_contracts.py`
- Modify: `tests/product/test_full_graph_audit.py`
- Modify: `tests/product/test_graph_binding_audit.py`
- Modify: `tests/product/test_cli_compile.py`

**Interfaces:**

- Consumes: `OrchestrationSelection` from Task 6 and `bind_task_execution_contracts()` from Task 4.
- Produces: final `FrozenComposition` whose selected manifest, compiled Workflow, config source and `InvocationLock` authenticate the same override generation.

- [ ] **Step 1: Write RED end-to-end selection tests.**

Using a copied project-config fixture:

1. add `.aa/workflow-schema.yaml`, copy `product-intake` to a new graph ID, change the `intake` entrypoint target to that graph, alter its `max_activations`, resolve composition, and assert both the manifest and compiled Workflow reflect the new target;
2. add `.aa/execution-contracts.yaml`, change one allowed validator tuple, resolve composition, and assert the corresponding compiled node reflects it;
3. assert each change alters configuration source digest, lock digest and composition digest;
4. change only YAML whitespace and assert parsed semantics are equal but source/lock digest still changes;
5. assert absent optional files produce the packaged Workflow and packaged execution-contract baseline;
6. assert malformed or interface-drifting overrides fail before `Engine.start()` for both adapters.

Use this concrete Workflow selection test first:

Add `from copy import deepcopy` to the test module, then use:

```python
def test_project_workflow_override_changes_compiled_graph_and_lock(
    installed_sources,
    tmp_path: Path,
) -> None:
    source = copy_config_tree(tmp_path / "project-config")
    module = load_product_workflow_module()
    document = module.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
        exclude_unset=True,
    )
    graph = deepcopy(document["graphs"].pop("product-intake"))
    graph["max_activations"] = int(graph["max_activations"]) + 1
    document["graphs"]["project-intake"] = graph
    document["entrypoints"]["intake"] = "project-intake"
    override = source.path / ".aa" / "workflow-schema.yaml"
    override.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    baseline = resolve_assurance_composition(request_for("opencode", installed_sources))
    request = request_for("opencode", installed_sources).model_copy(
        update={"configuration_tree": source}
    )
    selected = resolve_assurance_composition(request)

    graph_id = "assurance.product.workflow.graph.project-intake"
    assert selected.manifest.entrypoints["intake"] == "project-intake"
    assert selected.workflow.entrypoints["intake"] == graph_id
    assert selected.workflow.graphs[graph_id].max_activations == graph["max_activations"]
    assert selected.lock.digest != baseline.lock.digest
```

- [ ] **Step 2: Normalize the request before Registry resolution.**

In `resolve_assurance_composition()`:

```python
configuration_tree = configuration_tree_with_orchestration_overrides(
    request.configuration_tree
)
```

Pass only this normalized source to `RegistryPlatform.resolve()`. After resolution, obtain the selected snapshot from:

```python
snapshot = composition.declarative_sources[CONFIGURATION_PLUGIN_ID]
selection = load_orchestration_selection(snapshot)
```

- [ ] **Step 3: Apply one selected manifest and one selected contract map.**

Build:

```python
selected_manifest = ProductManifest.model_validate(
    {
        **composition.manifest.model_dump(mode="python"),
        "entrypoints": dict(selection.workflow_module.entrypoints),
        "workflow_module": selection.workflow_module,
    }
)
```

Replacing both fields is mandatory because `ProductManifest` requires its top-level `entrypoints` to equal `workflow_module.entrypoints`; `model_copy(update=...)` would bypass validation and leave an invalid authority object when a target changes. Assemble using the revalidated `selected_manifest`, bind using `selection.execution_contracts`, compile, build the lock with `selected_manifest`, and freeze the final composition with `selected_manifest`. Do not mutate registries or configuration contribution.

- [ ] **Step 4: Make graph inventory policy explicit and keep CLI fail-closed.**

Add:

```python
InventoryPolicy = Literal["packaged_baseline", "authenticated_project_override"]


def graph_inventory_policy(composition: FrozenComposition) -> InventoryPolicy:
    snapshot = composition.declarative_sources[CONFIGURATION_PLUGIN_ID]
    paths = {item.path for item in snapshot.files}
    return (
        "authenticated_project_override"
        if ".aa/workflow-schema.yaml" in paths
        else "packaged_baseline"
    )
```

Change the audit signature to `audit_full_graph(workflow: CompiledWorkflow, composition: FrozenComposition, *, inventory_policy: InventoryPolicy) -> GraphAuditResult`. `packaged_baseline` preserves the exact current `graph-inventory.yaml` check. `authenticated_project_override` sets `uninventoried_nodes=()` instead of comparing project graph/node IDs to the packaged golden; it does **not** suppress `unreachable_nodes`、`dead_ends`、`forbidden_direct_targets` or `missing_bindings`. The policy is derived only from frozen source bytes already authenticated by Registry resolution—there is no CLI flag and no project-supplied inventory file.

Update every call site to pass the policy explicitly. In `cli.py::_resolve_and_audit()` use:

```python
audit = audit_full_graph(
    composition.workflow,
    composition,
    inventory_policy=graph_inventory_policy(composition),
)
```

Add audit tests proving: packaged fallback still rejects an uninventoried node; an authenticated project rename has no uninventoried error; the same override still fails on an unreachable node, dead end, forbidden target, or missing binding; and adding a file after snapshot capture cannot change the policy.

- [ ] **Step 5: Prove lock/source authentication.**

The final lock’s configuration plugin `LockedSource.files` must list present optional files and their SHA-256 values. No lock schema change is needed: the existing locked plugin source digest already authenticates the extended snapshot, while the compiled Workflow projection authenticates the selected graph and validator tuples.

- [ ] **Step 6: Prove the public CLI accepts the authenticated replacement.**

In `test_cli_compile.py`, copy the real config fixture, write the same `product-intake -> project-intake` whole-file override, and invoke `aa compile --json` with the real installed source arguments. Assert exit code 0, `audit.uninventoried_nodes == []`, unchanged public entrypoint names, a changed workflow/lock digest, and the selected lock contains `.aa/workflow-schema.yaml`. Also retain/add a parameterized seam assertion that `compile`、`start` and `run` all enter the same `_resolve_and_audit()` path, so no lifecycle command can bypass or re-enable the packaged inventory check.

- [ ] **Step 7: Run both-adapter composition, audit and CLI tests.**

```bash
uv run pytest -q \
  tests/product/test_orchestration_overrides.py \
  tests/product/test_composition_authority.py \
  tests/product/test_product_composition.py \
  tests/product/test_agent_execution_contracts.py \
  tests/product/test_full_graph_audit.py \
  tests/product/test_graph_binding_audit.py \
  tests/product/test_cli_compile.py \
  tests/product/test_product_providers.py
```

- [ ] **Step 8: Commit Boot selection and override-aware auditing.**

```bash
git add \
  packages/products/assurance-product/assurance_product/product.py \
  packages/products/assurance-product/assurance_product/agent_contracts.py \
  packages/products/assurance-product/assurance_product/cli.py \
  tests/product/test_orchestration_overrides.py \
  tests/product/test_composition_authority.py \
  tests/product/test_product_composition.py \
  tests/product/test_agent_execution_contracts.py \
  tests/product/test_full_graph_audit.py \
  tests/product/test_graph_binding_audit.py \
  tests/product/test_cli_compile.py
git commit -m "feat: select authenticated orchestration at boot"
```

### Task 8: Add the pure `AgentLeafSpec -> GraphDef` expansion

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/graph/agent_leaf.py`
- Modify: `packages/framework/graph-engine/graph_engine/graph/module_schema.py`
- Modify: `packages/framework/graph-engine/graph_engine/graph/__init__.py`
- Create: `packages/framework/graph-engine/tests/graph/test_agent_leaf.py`
- Modify: `packages/framework/graph-engine/tests/graph/test_workflow_module_schema.py`

**Interfaces:**

- Consumes: existing `NodeDef`, `EdgeDef`, `GraphDef`, `WorkflowModuleDef`, and `parse_workflow_module()`.
- Produces: exported `AgentLeafSpec`, `build_agent_leaf_graph(spec: AgentLeafSpec) -> GraphDef`, and module-parser expansion of `kind: agent_leaf` only.

- [ ] **Step 1: Write RED builder and parser tests.**

Cover:

- exact expansion to `prepare`, `execute`, `finalize`, `done` and the three ordered edges;
- preservation of all phase NodeDef fields, including `model_fields_set` for explicit validators;
- rejection when any phase is not a task;
- rejection of extra `start`, `nodes`, `edges`, `done`, branch/join fields on an agent leaf;
- rejection of unknown graph `kind`;
- existing explicit `GraphDef` parsing remains byte-for-behavior compatible.

The first builder RED test uses fully valid phase nodes:

```python
def test_agent_leaf_expands_to_the_exact_current_graph_shape() -> None:
    phase = {
        "kind": "task",
        "capability_slot": "worker.execute",
        "retry": "once",
        "timeout": "short",
    }
    spec = AgentLeafSpec.model_validate(
        {
            "kind": "agent_leaf",
            "max_activations": 8,
            "prepare": phase,
            "execute": phase,
            "finalize": phase,
        }
    )
    graph = build_agent_leaf_graph(spec)
    assert graph.start == "prepare"
    assert tuple(graph.nodes) == ("prepare", "execute", "finalize", "done")
    assert tuple((edge.from_, edge.to) for edge in graph.edges) == (
        ("prepare", "execute"),
        ("execute", "finalize"),
        ("finalize", "done"),
    )
```

- [ ] **Step 2: Implement the frozen template and pure builder.**

```python
class AgentLeafSpec(FrozenModel):
    kind: Literal["agent_leaf"]
    max_activations: int = Field(ge=1, le=10_000)
    prepare: NodeDef
    execute: NodeDef
    finalize: NodeDef

    @model_validator(mode="after")
    def _require_task_phases(self) -> Self:
        for phase in ("prepare", "execute", "finalize"):
            if getattr(self, phase).kind != "task":
                raise ValueError(f"agent leaf {phase} phase must be a task")
        return self


def build_agent_leaf_graph(spec: AgentLeafSpec) -> GraphDef:
    return GraphDef(
        max_activations=spec.max_activations,
        start="prepare",
        nodes={
            "prepare": spec.prepare,
            "execute": spec.execute,
            "finalize": spec.finalize,
            "done": NodeDef(kind="end"),
        },
        edges=(
            EdgeDef(from_="prepare", to="execute"),
            EdgeDef(from_="execute", to="finalize"),
            EdgeDef(from_="finalize", to="done"),
        ),
    )
```

- [ ] **Step 3: Expand templates only during module parsing.**

Before `WorkflowModuleDef.model_validate`, copy the raw top-level/graphs mappings. For each graph:

- no `kind`: leave as explicit `GraphDef` input;
- `kind == "agent_leaf"`: validate `AgentLeafSpec`, call builder, pass the resulting `GraphDef` to module validation;
- any other `kind`: raise `ValueError(f"unknown workflow graph kind: {kind!r}")`.

Do not add template awareness to compiler, planner or runtime.

- [ ] **Step 4: Run graph language tests.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/graph/test_agent_leaf.py \
  packages/framework/graph-engine/tests/graph/test_workflow_module_schema.py \
  packages/framework/graph-engine/tests/graph/test_schema_and_compiler.py
uv run pyright packages/framework/graph-engine/graph_engine/graph
```

- [ ] **Step 5: Commit the engine-neutral builder.**

```bash
git add \
  packages/framework/graph-engine/graph_engine/graph/agent_leaf.py \
  packages/framework/graph-engine/graph_engine/graph/module_schema.py \
  packages/framework/graph-engine/graph_engine/graph/__init__.py \
  packages/framework/graph-engine/tests/graph/test_agent_leaf.py \
  packages/framework/graph-engine/tests/graph/test_workflow_module_schema.py
git commit -m "feat: add agent leaf graph template"
```

### Task 9: Convert `execution-execute` as the single current-Runtime tracer

**Files:**

- Modify: `packages/capabilities/assurance-execution/assurance_execution/resources/workflow/module.yaml`
- Modify: `packages/capabilities/assurance-execution/tests/test_workflow_module.py`
- Test: `tests/product/test_workflow_modularization_golden.py`

**Interfaces:**

- Consumes: `kind: agent_leaf` parsing from Task 8 and task validator binding from Tasks 3–4.
- Produces: one template-authored `execution-execute` graph that compiles to the existing `GraphDef`; explicit `execution-run` remains the control graph.

- [ ] **Step 1: Write RED tracer-shape and parity tests.**

Raw YAML assertions:

```python
assert raw_graphs["execution-execute"]["kind"] == "agent_leaf"
assert "nodes" not in raw_graphs["execution-execute"]
assert "edges" not in raw_graphs["execution-execute"]
assert "kind" not in raw_graphs["execution-run"]
```

Build an explicit four-node equivalent inside the test and assert its parsed `GraphDef` equals the expanded `execution-execute`. Also assert `execution-run` stays explicit and both exports/slots are unchanged.

- [ ] **Step 2: Perform the mechanical YAML conversion.**

For `execution-execute` only:

1. add `kind: agent_leaf`;
2. retain `max_activations: 8`;
3. promote the existing `nodes.prepare`, `nodes.execute`, and `nodes.finalize` mappings to top-level `prepare`, `execute`, and `finalize` under the graph;
4. remove `start`, the `nodes` wrapper, the explicit `done` node and all three explicit edges;
5. do not change any phase capability slot, input, projection, retry, timeout or validator field.

Leave `execution-run` byte-for-structure explicit as the control case.

The exact structural transformation is:

```python
graph = raw["graphs"]["execution-execute"]
assert graph["start"] == "prepare"
assert tuple(graph["nodes"]) == ("prepare", "execute", "finalize", "done")
assert graph["nodes"]["done"] == {"kind": "end"}
assert graph["edges"] == [
    {"from": "prepare", "to": "execute"},
    {"from": "execute", "to": "finalize"},
    {"from": "finalize", "to": "done"},
]
raw["graphs"]["execution-execute"] = {
    "kind": "agent_leaf",
    "max_activations": graph["max_activations"],
    "prepare": graph["nodes"]["prepare"],
    "execute": graph["nodes"]["execute"],
    "finalize": graph["nodes"]["finalize"],
}
```

- [ ] **Step 3: Run module/compile parity tests.**

```bash
uv run pytest -q \
  packages/capabilities/assurance-execution/tests/test_workflow_module.py \
  tests/product/test_workflow_modularization_golden.py \
  tests/product/test_product_composition.py \
  tests/product/test_product_entrypoints.py
```

No graph inventory change is expected because expanded node IDs and edges are identical.

- [ ] **Step 4: Run one current-Runtime execute path.**

```bash
uv run pytest -q 'tests/product/test_generation_branches.py::test_single_family_runs_only_its_generation_branch[api]'
```

Expected: the existing Engine reaches `completed`; the tracer remains an ordinary compiled `GraphDef` to planner/scheduler.

- [ ] **Step 5: Commit the tracer.**

```bash
git add \
  packages/capabilities/assurance-execution/assurance_execution/resources/workflow/module.yaml \
  packages/capabilities/assurance-execution/tests/test_workflow_module.py
git commit -m "refactor: express execution leaf with agent template"
```

### Task 10: Run repository gates and record the remaining migration blocker

**Files:** none unless a gate exposes a defect in a preceding Task; fix such a defect in the owning Task’s files and commit it separately.

**Interfaces:**

- Consumes: committed outputs of Tasks 1–9.
- Produces: fresh repository-gate and wheel-smoke evidence plus a handoff that explicitly keeps Retro/improvement-evaluate outside migration-golden readiness.

- [ ] **Step 1: Run focused aggregate suites.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/graph \
  packages/framework/graph-engine/tests/composition \
  packages/adapters/agent-runtime-contracts/tests \
  packages/capabilities/assurance-intake/tests/test_contracts.py \
  packages/capabilities/assurance-generation/tests/test_contracts.py \
  packages/capabilities/assurance-execution/tests/test_contracts.py \
  packages/capabilities/assurance-quality/tests/test_contracts.py \
  packages/capabilities/assurance-healing/tests/test_contracts.py \
  packages/capabilities/assurance-improvement/tests/test_contracts.py \
  tests/product/test_orchestration_overrides.py \
  tests/product/test_commit_validator_bindings.py \
  tests/product/test_product_composition.py \
  tests/product/test_product_entrypoints.py
```

- [ ] **Step 2: Run the complete repository gate.**

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest
bash scripts/assurance_product_wheel_smoke_test.sh
```

- [ ] **Step 3: Verify package artifacts include both fallbacks.**

Inspect the wheel built by the smoke test and require these entries:

```text
assurance_product/resources/schemas/workflow-schema.yaml
assurance_product/resources/schemas/execution-contracts.yaml
```

Resolve a composition using a copied config tree with both project override files and prove the resulting lock lists `.aa/execution-contracts.yaml` and `.aa/workflow-schema.yaml`.

- [ ] **Step 4: Check scope and repository cleanliness.**

```bash
git status --short
git log --oneline --decorate -10
git diff HEAD~9..HEAD --stat
```

Expected: only planned files changed; no LangGraph dependency, Attempt Kernel, join rewrite, effect rewrite, Retro contract rewrite or unrelated user file appears.

- [ ] **Step 5: Stop before claiming migration-golden readiness.**

Report this exact remaining blocker in the handoff: Retro and improvement-evaluate still require their separate production-contract spec and implementation before the architecture specification’s migration golden can be captured. The successful result of this plan is “baseline foundations + one current-Runtime leaf tracer green,” not “Phase 1 migration baseline complete.”

## Completion Criteria

- Six Feature module suites are green and agree that all agent phases use slots.
- Every executable task resolves a validator tuple from exactly one authority: agent phase contract or explicit direct-node YAML.
- Packaged baseline 中恰好五个已 characterize 的历史绑定非空；其他当前 task set 显式为空，项目 override 的差异则由 source/lock digest 固定。
- A resolved plan-review task produces an accepted validation receipt through the current scheduler commit path, and the existing rejection test still proves no promotion on rejection.
- Both `.aa` override files are optional, fixed-name, whole-file, source-authenticated and lock-bound; neither contributes executable code or registry entries.
- `aa compile` accepts an authenticated project graph rename without consulting the packaged node inventory, while packaged fallback still fails on inventory drift and both modes keep reachability/dead-end/capability/binding audits.
- Packaged fallback and absent-project behavior are identical.
- `execution-execute` uses `AgentLeafSpec`; `execution-run` remains explicit; both compile to the same four-node topology and the current Runtime execute test passes.
- Full repository CI and product wheel smoke pass.
- No migration golden is declared until the separate Retro/improvement-evaluate contract repair is complete.
