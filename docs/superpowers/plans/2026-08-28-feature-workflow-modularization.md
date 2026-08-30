# Feature Workflow Modularization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan.

**Goal:** 在 30–40 PD 范围内，把当前单体 Assurance Workflow 重构为 Framework + Clients + 六个 Feature subprojects + Product main，并在 composition 时确定性装配为现有 runtime 执行的一份普通 `CompiledWorkflow`。

**Architecture:** `graph-engine` 继续拥有唯一 Workflow 语言、Assembler、compiler、planner、ledger 与 lock；三个 agent runtime wheel 归入 Clients；六个 Capability wheel 各自发布一个经过认证的 Workflow module resource；`assurance-product` 在 `ProductManifest` 内发布 Product root module、精确模块闭包和 99 个 slot bindings。Assembler 只消费已认证 Registry 内容，在 compiler 前消除 import/export/slot 元数据。Runtime 只看到 generic subgraph I/O 与 routing 字段，不感知 Feature/module。

**Tech Stack:** Python 3.11、uv workspace、Pydantic v2、PyYAML、pytest、ruff、pyright、import-linter、现有 graph-engine ledger/runtime。

**Spec:** `docs/specs/2026-08-28-feature-workflow-module-architecture.md`

## Global Constraints

- 所有命令从仓库根目录运行，Python 工具命令通过 `uv run` 使用 workspace Python；shell smoke 脚本按原入口运行。
- 当前 `.gitignore` 忽略整个 `docs/`。开始实施前，必须先在源分支用 `git add -f docs/specs/2026-08-28-feature-workflow-module-architecture.md docs/superpowers/plans/2026-08-28-feature-workflow-modularization.md` 提交这两份设计文档；否则隔离 worktree 看不到实施依据。确认 `git ls-files` 能列出两者后，才可继续。
- 实施必须使用 `superpowers:using-git-worktrees` 建立隔离 worktree，并从已包含 Spec/Plan 的提交创建。三个 wheel smoke 脚本都从 committed `HEAD` 打包，其中 capability smoke 还要求 tracked worktree clean，因此它们只能在对应 Task 提交后、隔离 worktree 无未提交改动时运行。
- 保留 distribution 名、Python import namespace、Python entry-point group/name、`aa` CLI 命令和 14 个公共 Workflow Entrypoint 名。
- 目录移动使用 `git mv`；不得顺手重命名 `PluginContribution`、`PluginDescriptor` 或既有 capability ID。
- Framework 不得导入 Clients、Features 或 Products。Feature-to-Feature graph import 禁止；规格允许的 public Python `contracts` 依赖继续由 import-linter 约束。
- Product root module 使用 `ProductManifest.workflow_module` 内联发布；Feature module 使用既有 `ResourceContribution`。不得新增 ProductContribution、ModuleRegistry 或第六种 Registry。
- Product main 不含 `task` node，不直接引用 Feature capability 或 private graph，只调用已声明 import alias。
- Runtime event type/schema、ledger/checkpoint 持久化格式和 `InvocationLock.schema_version == "2"` 不变。复用 `GraphStarted.input`、`GraphCompleted.output`、parent `NodeCompleted.output` 和 `TokenOffered.payload`；只允许收紧既有 `NodeFailed` 的 fold/causal-proof 语义以承载无 task-attempt 的结构契约失败。
- Legacy `workflow` / `workflow_resource_id` 两种 manifest form 继续可读；其未设置的新字段不得改变现有 canonical lock golden。
- `.aa/` 只保存组织配置。不得从 SUT、cwd、URL、glob、Python import path 或 ambient entry points 发现 Workflow module。
- 每项行为改动先写失败测试，再写最小实现，再跑聚焦测试；每个 Task 独立提交。不得把机械拆分和路由语义修复放在同一提交。
- 保留用户已有未提交文件；任何 Task 的 `git add` 只能列出本 Task 文件。
- 本计划不实现历史 wheel 自动选择、跨版本 resume、assembly provenance graph、lock schema 升级或 40–55 PD 档位的生产认证矩阵。

### Frozen public catalog

Product root module ID 固定为 `assurance.product.workflow`。Feature public exports 固定如下：

| Module | Exports |
|---|---|
| `assurance.intake.workflow` | `prepare`, `case` |
| `assurance.generation.workflow` | `generate` |
| `assurance.execution.workflow` | `execute`, `rerun` |
| `assurance.quality.workflow` | `assess`, `issue-review`, `issue-analyze`, `issue-reconcile`, `report` |
| `assurance.healing.workflow` | `repair-failure`, `repair-coverage` |
| `assurance.improvement.workflow` | `archive`, `retro`, `review`, `evaluate`, `export`, `apply`, `rollback` |

每个 export 的 schema ID 固定为 `assurance.<feature>.workflow.<export>.input.v1` 与 `assurance.<feature>.workflow.<export>.output.v1`。Product import alias 固定为 `<feature>.<export>`。

每份 public I/O schema 必须是 Framework closed subset 内的 `additionalProperties: false` schema；对应 Pydantic boundary model 使用 frozen + `extra="forbid"`。输入至少显式声明 `change_id` 以及 Task 1 inventory 中该 export 实际需要的 typed artifact/budget/policy 字段，输出只含 normalized outcome、已用 budget 和已认证 artifact/effect references，且不含具体 Client/provider 类型。

Entrypoint 映射固定为：`intake → intake.prepare`、`case → intake.case`、`archive → improvement.archive`、`retro → improvement.retro`、三个 issue Entrypoint → 对应 Quality export、五个 improvement Entrypoint → 对应 Improvement export；`full` 与 `execute` 由 Product 自己编排跨 Feature 主流程。

Product root 的 14 个本地 graph ID 固定为 `product-<entrypoint>`，其中 `<entrypoint>` 保留原 Entrypoint 拼写；这避免与 Feature 旧 graph ID 冲突，并让 Product structural-only 审计只有一个明确前缀。

33 个 base agent contract 各展开 `prepare`、`execute`、`finalize` 三个 slot。slot 名是 `<base>.<phase>`，contract ID 是 `assurance.<feature>.agent.<base>.v1`，concrete alias 是 `assurance.product.agent.<feature>.<base>.<phase>`。最终恰好 99 个 slot binding。

### Frozen Framework interfaces

- Module resource media type：`application/vnd.graph-engine.workflow-module+yaml`。
- Product module 必须声明最终 ordinary Workflow 的 `name`（Assurance 固定为 `assurance`）；Feature module 禁止声明 `name`。Assembler 从 Product root 复制该值，不从 module ID 猜测。
- ProductManifest modular fields：`workflow_module`、`workflow_module_resources`、`workflow_slot_bindings`。
- Assembler 唯一公开入口：`assemble_product_workflow(*, manifest: ProductManifest, descriptors: Mapping[str, PluginDescriptor], registries: RegistrySet) -> WorkflowDef`。
- Module node unresolved fields：task 使用 `capability_slot`；cross-module subgraph 使用 `graph_import`。两者在 `compile_workflow()` 前必须被消除。
- Generic subgraph fields：`input_schema`、`input_projection`、`output_projection`、`output_schema`。
- Input projection 新 source：`graph_input_pointer`，只读取当前 `GraphInstanceRecord.input`；既有 `root_pointer` 仍读取 Invocation root。
- Subgraph 调用不会隐式继承 parent current graph input；默认仍只有静态 `node.definition.input`。任何 private child 需要 `change_id`、预算或 artifact refs 时，caller 必须显式声明 `input_projection`（传整个 closed input 或逐字段投影）。
- Output projection 唯一动态 source：`child_output_pointer`，只读取 child `GraphCompleted.output`。output projection 只允许 literal、object、tuple 和 child-output source。
- Planner 的 `plan_next`、`plan_running_tasks`、`validate_projection`、`validate_event_history` 增加 keyword-only `schemas: SchemaRegistry | None = None`；Engine 总是传 `composition.registries.schemas`。
- `schemas=None` 只兼容整份 compiled Workflow 完全没有 `input_schema`/`output_schema` 的 legacy 路径；只要任一 contracted subgraph 存在就立即 `PlanningError`。`validate_event_history()` 的所有递归规划调用必须复用同一份 registry。
- Subgraph contract violation 使用现有 `NodeFailed(TaskFailure(kind="invalid_input" | "invalid_output"))`、`GraphFailed`、`InvocationFinished` 形成可重放的 structural terminal proof；fold 允许 active structural activation 在零 task attempt 下以这两种 kind 失败，最终 causal proof 再根据 compiled node 与冻结 schemas 认证确切违规。不得只抛出未持久化异常后把 Invocation 留在 running。
- Routing：`routing.mode` 为 `exclusive | fanout`；fanout 必须显式给出 `min_matches`，包括允许零匹配时的 `0`；exclusive 禁止 `min_matches` 且恰好一条 `otherwise: true` edge。
- Legacy node 省略 `routing` 时保留当前“发出所有命中/无条件 edge，零命中允许”的行为。所有最终 modular graph 的多出边 node 必须显式 routing。
- Explicit route 的 canonical key 是 `(to, condition or "", otherwise)`；完全重复的 key 编译失败。explicit route token ID 使用 source 内 canonical rank，legacy route 继续使用旧 global edge index。source node 已经完成后发生的 overlap/min-match 失败属于 graph-level failure，只追加 `GraphFailed`、祖先传播与 `InvocationFinished`，不得再为已完成 source 追加 `NodeFailed`，且不得发出任何 edge token。
- Contract/routing fatal cause 使用同一 deterministic fatal barrier：按 `(graph_instance_id, activation-or-node-id, reason)` 选择 canonical first cause。只要 Invocation 任意位置仍有 running/promotion/effect-pending attempt，planner 不追加 terminal chain、不发 edge token、也不创建新 task attempt；它只允许既有 attempt 被重建/结算。cause 可由现有 tokens/completed output/compiled contracts 重算，因此无需新事件。全部 live attempts 结算后，一次追加完整失败链。
- 不排序或重写整个 `GraphDef.edges` / `CompiledGraph.edges`；canonical rank 只用于 explicit source node 的选择和 token ID，既有 join predecessor/aggregation 顺序保持不变。
- `CapabilityBindingContribution.contract_id` 与 `CapabilityBindingEntry.contract_id` 为向后兼容的可选字段；legacy `None` 在 canonical projection 中省略。被 slot 选中的 binding 必须有完全相同的 contract ID。

## Execution Preflight (not included in the 30–40 PD estimate)

```bash
git diff --cached --name-only
git check-ignore -v docs/specs/2026-08-28-feature-workflow-module-architecture.md docs/superpowers/plans/2026-08-28-feature-workflow-modularization.md
git add -f docs/specs/2026-08-28-feature-workflow-module-architecture.md docs/superpowers/plans/2026-08-28-feature-workflow-modularization.md
git diff --cached --name-only
git commit --only docs/specs/2026-08-28-feature-workflow-module-architecture.md docs/superpowers/plans/2026-08-28-feature-workflow-modularization.md -m "docs: specify feature workflow modularization"
git ls-files docs/specs/2026-08-28-feature-workflow-module-architecture.md docs/superpowers/plans/2026-08-28-feature-workflow-modularization.md
```

Expected: 第一个命令没有输出；force-add 后的 staged list 精确包含两份文档；最后一个命令精确输出两个路径。若开始时已有 staged 用户改动则停止并先让用户处理，不得把它们带入文档提交。随后按 `superpowers:using-git-worktrees` 从该提交创建隔离 worktree；后续所有 Task、测试与提交都在隔离 worktree 内完成。

## File Map

Task 2–5 完成后，以下均使用目标路径：

```text
packages/framework/graph-engine
packages/clients/agent-runtime-contracts
packages/clients/agent-runtime-opencode
packages/clients/agent-runtime-cursor
packages/features/assurance-intake
packages/features/assurance-generation
packages/features/assurance-execution
packages/features/assurance-healing
packages/features/assurance-quality
packages/features/assurance-improvement
packages/products/assurance-product
```

| Area | Production files | Focused tests |
|---|---|---|
| Workflow language | `graph_engine/graph/{schema,input_projection,output_projection,module_schema,compiler}.py` | `tests/graph/test_{schema_and_compiler,input_projection,output_projection,workflow_module_schema}.py` |
| Composition | `graph_engine/composition/{models,declarative,contributions,registries,workflow_assembler,resolver,lock}.py` | `tests/composition/test_{manifest_projection,workflow_assembler,registry_platform,registries,lock_model,declarative_sources}.py` |
| Runtime | `graph_engine/runtime/{planner,engine}.py` | `tests/runtime/test_{planner_input_projection,planner_subgraph_contracts,planner_routing,engine}.py` |
| Feature modules | each `assurance_<feature>/{plugin.py,plugin-declaration.json,contracts/workflow.py,resources/workflow/module.yaml,resources/schemas/workflow/*}` | each Feature `tests/test_workflow_module.py` plus existing contract/operation tests |
| Product | `assurance_product/{product,agent_contracts,binding_builder,models,output_routes}.py`, product declarations and `resources/workflow/main.yaml` | `tests/product/test_{product_composition,product_entrypoints,agent_execution_contracts,full_graph_audit}.py` and route suites |
| Packaging | root `pyproject.toml`, `uv.lock`, `.importlinter`, `.github/workflows/ci.yml`, `scripts/*.sh` | `tests/architecture/*`, product packaging/wheel smoke tests |

---

## Phase A — Freeze behavior and establish the four workspace roles (3–4 PD)

### Task 1: Freeze the legacy Workflow inventory and behavioral golden

**Files:**

- Create: `scripts/update_assurance_workflow_golden.py`
- Create: `tests/product/goldens/assurance-full-pre-modular.json`
- Create: `tests/product/fixtures/workflow-module-ownership.yaml`
- Modify: `tests/product/graph_inventory.py`
- Create: `tests/product/test_workflow_modularization_golden.py`
- Read only: `packages/assurance-product/assurance_product/resources/workflow/assurance-full.yaml`

**Step 1: Write the failing characterization test**

```python
def test_pre_modular_inventory_is_frozen() -> None:
    workflow = load_canonical_workflow()
    assert len(workflow.entrypoints) == 14
    assert len(workflow.graphs) == 52
    assert sum(len(graph.nodes) for graph in workflow.graphs.values()) == 265
    assert sum(
        node.kind == "subgraph"
        for graph in workflow.graphs.values()
        for node in graph.nodes.values()
    ) == 59
    assert canonical_json_bytes(workflow.model_dump(mode="json", by_alias=True, exclude_unset=True)) == (
        GOLDEN.read_bytes()
    )
```

Add a second assertion that the ownership fixture assigns every one of the 52 graph IDs exactly once with counts `{intake: 6, generation: 19, execution: 2, healing: 2, quality: 9, improvement: 12, product: 2}`. The fixture must record the public export target, every local subgraph call, every `root_pointer` used by each exported closure, and the legacy `full` prefix nodes that will relocate behind `intake.prepare`; it becomes the exact input-boundary/relocation inventory for Tasks 18–25.

**Step 2: Run the test and verify RED**

Run: `uv run pytest tests/product/test_workflow_modularization_golden.py -v`  
Expected: FAIL because the golden and ownership fixture do not exist.

**Step 3: Add the deterministic golden writer and reviewed ownership fixture**

The writer imports `load_canonical_workflow()`, serializes with `canonical_json_bytes(workflow.model_dump(mode="json", by_alias=True, exclude_unset=True))`, and refuses to write anywhere except the two named repository fixtures. Freeze the full exact ownership table, not only examples: Intake owns `entry`, `case`, `intake`, `explore`, `case-design`, `case-review`; Generation owns its 19 graphs; Execution owns two; Healing owns two; Quality owns its six internal graphs plus `issue-review`, `issue-analyze`, `issue-reconcile`; Improvement owns 12 including both top-level wrappers `archive` **and `retro`**; Product retains only `full` and `execute`.

In the same fixture, name the mechanically relocated Intake prefix from legacy `full`: `intake`, `explore`, `case-design`, `case-review`, `review-pass-gate`, `review-fix-gate`, `review-human-gate`, `human-review` and their internal edges. Record that legacy continuation edges into `generation` become the `intake.prepare` public terminal boundary; no semantic expression/action change is allowed before Task 26.

**Step 4: Generate and verify GREEN**

Run: `uv run python scripts/update_assurance_workflow_golden.py`  
Run: `uv run pytest tests/product/test_workflow_modularization_golden.py tests/product/test_full_graph_audit.py -v`  
Expected: PASS with 14 Entrypoints, 52 graphs, 265 nodes, 59 subgraph calls, exact owner counts `6/19/2/2/9/12/2`, and the reviewed Product→Intake relocation map.

**Step 5: Commit**

```bash
git add scripts/update_assurance_workflow_golden.py tests/product/goldens/assurance-full-pre-modular.json tests/product/fixtures/workflow-module-ownership.yaml tests/product/graph_inventory.py tests/product/test_workflow_modularization_golden.py
git commit -m "test(product): freeze pre-modular workflow behavior"
```

### Task 2: Move graph-engine under Framework without renaming its public package

**Files:**

- Move: `packages/graph-engine` → `packages/framework/graph-engine`
- Modify: `pyproject.toml`, `uv.lock`, `.importlinter`
- Modify path assertions in: `tests/architecture/test_graph_engine_boundaries.py`, `tests/phase6/conformance.py`, `tests/phase6/test_phase2_invariant_reconciliation.py`
- Modify path-bearing gates: `tests/product/conformance.py`, `tests/product/test_phase5_final_fault_gate.py`, `tests/product/test_phase5_final_security_gate.py`
- Modify: `scripts/graph_engine_smoke_test.sh`

**Step 1: Add a failing layout assertion**

```python
def test_framework_has_the_only_graph_engine_source_tree(repo_root: Path) -> None:
    assert (repo_root / "packages/framework/graph-engine/graph_engine").is_dir()
    assert not (repo_root / "packages/graph-engine").exists()
```

**Step 2: Run RED**

Run: `uv run pytest tests/architecture/test_graph_engine_boundaries.py -v`  
Expected: FAIL because the Framework path does not exist.

**Step 3: Perform only the Framework move**

Use `mkdir -p packages/framework` and `git mv packages/graph-engine packages/framework/graph-engine`. Update pytest, pyright and uv workspace paths plus every exact path found by:

```bash
rg -n 'packages/graph-engine' pyproject.toml scripts tests packages README.md .github
```

Regenerate `uv.lock`; do not change distribution `graph-engine` or import `graph_engine`. In `.importlinter`, make Framework's forbidden set explicit: all three Client namespaces, all six Feature namespaces and `assurance_product` (in addition to test fixtures). Extend the AST architecture test with the same role matrix so a missing import-linter root cannot hide a downward import.

**Step 4: Verify**

Run: `uv run pytest packages/framework/graph-engine/tests tests/architecture/test_graph_engine_boundaries.py tests/product/test_phase5_final_fault_gate.py tests/product/test_phase5_final_security_gate.py -q`
Run: `uv run pyright packages/framework/graph-engine/graph_engine`
Expected: PASS and `rg -n 'packages/graph-engine' pyproject.toml scripts tests packages README.md .github` returns no stale source path.

**Step 5: Commit**

```bash
git add packages/framework pyproject.toml uv.lock .importlinter scripts/graph_engine_smoke_test.sh tests/architecture tests/phase6 tests/product/conformance.py tests/product/test_phase5_final_fault_gate.py tests/product/test_phase5_final_security_gate.py
git commit -m "refactor(workspace): classify graph-engine as framework"
```

**Step 6: Verify the committed wheel**

Run: `bash scripts/graph_engine_smoke_test.sh`
Expected: PASS against the new committed Framework path.

### Task 3: Move provider-neutral contracts and concrete adapters under Clients

**Files:**

- Move: `packages/agent-runtime-contracts` → `packages/clients/agent-runtime-contracts`
- Move: `packages/agent-runtime-opencode` → `packages/clients/agent-runtime-opencode`
- Move: `packages/agent-runtime-cursor` → `packages/clients/agent-runtime-cursor`
- Modify: `pyproject.toml`, `uv.lock`, `.importlinter`, path-based architecture/phase6 tests and benchmark manifest paths
- Modify path-bearing gates: `tests/product/composition_harness.py`, `tests/product/conformance.py`, `tests/product/test_phase5_final_{fault,repository,security}_gate.py`

**Step 1: Add failing target-layout assertions**

Assert all three target directories exist, all three old directories are absent, and their `project.name` values remain unchanged.

**Step 2: Run RED**

Run: `uv run pytest tests/architecture/test_graph_engine_boundaries.py -v`  
Expected: FAIL on the new Client paths.

**Step 3: Move and update exact paths**

Use `git mv` for all three packages. Update root pytest/pyright/workspace paths, `benchmark/agent-runtime-phase3/run_item.py`, `benchmark/agent-runtime-phase3/manifest.json`, and **every** path assertion found with `rg -n 'packages/agent-runtime-' pyproject.toml scripts tests packages README.md .github benchmark`; do not defer Product gate fixtures.

Add `assurance_product` to import-linter `root_packages`. Add a Client-downward forbidden contract and matching AST test: `agent_runtime_contracts`, `agent_runtime_opencode` and `agent_runtime_cursor` may not import any of the six Feature namespaces or `assurance_product`. Adapters may depend on `agent_runtime_contracts` and Framework public host/runtime contracts, but remain independent of one another.

**Step 4: Verify Clients and dependency direction**

Run: `uv run pytest packages/clients/agent-runtime-contracts/tests packages/clients/agent-runtime-opencode/tests packages/clients/agent-runtime-cursor/tests tests/architecture tests/product/test_phase5_final_fault_gate.py tests/product/test_phase5_final_repository_gate.py tests/product/test_phase5_final_security_gate.py -q`  
Run: `uv run lint-imports`  
Expected: PASS; adapters remain independent, `agent_runtime_contracts` imports neither adapter, and no Client imports a Feature or Product.

**Step 5: Commit**

```bash
git add packages/clients pyproject.toml uv.lock .importlinter benchmark/agent-runtime-phase3 tests/architecture tests/phase6 tests/product/composition_harness.py tests/product/conformance.py tests/product/test_phase5_final_fault_gate.py tests/product/test_phase5_final_repository_gate.py tests/product/test_phase5_final_security_gate.py
git commit -m "refactor(workspace): classify agent runtimes as clients"
```

### Task 4: Move the six Capability wheels under Features

**Files:**

- Move all six: `packages/assurance-{intake,generation,execution,healing,quality,improvement}` → `packages/features/*`
- Modify: `pyproject.toml`, `uv.lock`, `.importlinter`
- Modify: `scripts/assurance_capability_wheel_smoke_test.sh`
- Modify hardcoded paths in `tests/product/composition_harness.py`, `tests/product/conformance.py`, `tests/phase6/*`
- Modify executable ownership node IDs in: `tests/phase4/test_ownership_ledger.py`

**Step 1: Add the failing six-member shape test**

```python
def test_all_capability_wheels_are_feature_subprojects(repo_root: Path) -> None:
    expected = {
        "assurance-intake",
        "assurance-generation",
        "assurance-execution",
        "assurance-healing",
        "assurance-quality",
        "assurance-improvement",
    }
    assert {path.name for path in (repo_root / "packages/features").iterdir()} == expected
```

**Step 2: Run RED**

Run: `uv run pytest tests/architecture/test_graph_engine_boundaries.py -v`  
Expected: FAIL because `packages/features` is absent.

**Step 3: Move packages and keep contract-only dependencies explicit**

Update all path consumers, including the executable pytest node IDs in `tests/phase4/test_ownership_ledger.py`. Keep the existing allowed public `contracts` dependencies; strengthen `.importlinter` so a Feature cannot import another Feature's `plugin`, `operations`, `validators`, `effects`, `resource_loader`, `resources` or future `workflow` implementation. Add a six-Feature downward contract and AST test forbidding `agent_runtime_opencode`, `agent_runtime_cursor` and `assurance_product`; provider-neutral `agent_runtime_contracts` remains allowed.

**Step 4: Verify all Feature wheels**

Run: `uv run pytest packages/features/assurance-intake/tests packages/features/assurance-generation/tests packages/features/assurance-execution/tests packages/features/assurance-healing/tests packages/features/assurance-quality/tests packages/features/assurance-improvement/tests tests/phase4/test_ownership_ledger.py tests/architecture -q`
Run: `uv run lint-imports`
Expected: PASS with unchanged distribution/import names; Features import neither concrete Client adapters nor Product implementation.

**Step 5: Commit**

```bash
git add packages/features pyproject.toml uv.lock .importlinter scripts/assurance_capability_wheel_smoke_test.sh tests/architecture tests/product tests/phase4/test_ownership_ledger.py tests/phase6
git commit -m "refactor(workspace): classify capabilities as features"
```

**Step 6: Verify committed Feature wheels**

Run: `bash scripts/assurance_capability_wheel_smoke_test.sh`
Expected: PASS from committed `HEAD` with no editable-source leakage.

### Task 5: Move assurance-product under Products and close all stale paths

**Files:**

- Move: `packages/assurance-product` → `packages/products/assurance-product`
- Modify: `pyproject.toml`, `uv.lock`, `.importlinter`
- Modify: `scripts/assurance_product_wheel_smoke_test.sh`
- Modify path users in `tests/product`, `tests/phase6`, `benchmark/agent-runtime-phase3`, README files
- Modify: `tests/architecture/test_graph_engine_boundaries.py`

**Step 1: Add a failing Product role assertion**

Assert `packages/products/assurance-product/assurance_product` exists, the old path does not, and only that distribution owns the `aa` console script.

**Step 2: Run RED**

Run: `uv run pytest tests/architecture/test_graph_engine_boundaries.py tests/product/test_product_packaging.py -v`  
Expected: FAIL on the target Product path.

**Step 3: Move, update, and regenerate lock**

Apply the move and regenerate `uv.lock`. Search only the seven active distributions with a PCRE text boundary so quoted/bare package roots are both found:

```bash
rg -n -P 'packages/(assurance-product|assurance-intake|assurance-generation|assurance-execution|assurance-healing|assurance-quality|assurance-improvement)(?=/|[^A-Za-z0-9_-]|$)' pyproject.toml scripts tests packages README.md .github benchmark
```

Update every active source/test/build path and require that exact command to return no matches after Tasks 4–5. Do **not** mechanically rewrite `packages/assurance-kernel/` strings or the legacy ownership/baseline map in `tests/phase6/conformance.py`; those are historical provenance and the regex intentionally excludes `assurance-kernel`. Do not alter `assurance-product`, `assurance_product`, `graph_engine.products` or either product entrypoint name.

Complete the four-role architecture test here: import-linter recognizes `assurance_product` as a root; Framework cannot import Clients/Features/Product; Clients cannot import Features/Product; Features cannot import concrete adapters/Product and can cross Feature boundaries only through allowed public `contracts`; Product is the only top-level composition layer allowed to import all lower public surfaces.

**Step 4: Verify the complete physical classification**

Run: `uv run pytest tests/architecture tests/product/test_product_packaging.py tests/product/test_wheel_smoke_contract.py -q`
Run: `uv run pyright`
Run: `uv run lint-imports`
Expected: PASS, with no stale exact active-distribution path from the bounded `rg`, while the historical `packages/assurance-kernel/` provenance assertions remain byte-identical.

**Step 5: Commit**

```bash
git add packages/products pyproject.toml uv.lock .importlinter scripts/assurance_product_wheel_smoke_test.sh tests/architecture tests/product tests/phase6 benchmark README.md
git commit -m "refactor(workspace): classify assurance product"
```

**Step 6: Verify the committed Product wheel**

Run: `bash scripts/assurance_product_wheel_smoke_test.sh`
Expected: PASS from the nested Product source path.

---

## Phase B — Add generic Workflow I/O and routing semantics (6–8 PD)

### Task 6: Add current-graph input and child-output projection languages

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/graph/input_projection.py`
- Create: `packages/framework/graph-engine/graph_engine/graph/output_projection.py`
- Modify: `packages/framework/graph-engine/graph_engine/graph/__init__.py`
- Modify: `packages/framework/graph-engine/graph_engine/runtime/planner.py`
- Modify: `packages/framework/graph-engine/tests/graph/test_input_projection.py`
- Create: `packages/framework/graph-engine/tests/graph/test_output_projection.py`
- Modify: `tests/product/product_runner.py`, `tests/product/test_phase3_live_fixture_contract.py`

**Step 1: Write RED projection tests**

```python
def test_graph_input_pointer_does_not_change_root_pointer() -> None:
    graph_projection = parse_input_projection({"type": "graph_input_pointer", "pointer": "/change_id"})
    root_projection = parse_input_projection({"type": "root_pointer", "pointer": "/change_id"})
    context = {
        "root_input": {"change_id": "ROOT"},
        "graph_input": {"change_id": "CHILD"},
        "node_config": {},
        "predecessor_tokens": {},
    }
    assert project_task_input(graph_projection, **context) == "CHILD"
    assert project_task_input(root_projection, **context) == "ROOT"


def test_child_output_projection_exposes_only_selected_fields() -> None:
    projection = parse_output_projection(
        {
            "type": "object",
            "fields": {
                "status": {"type": "child_output_pointer", "pointer": "/internal/status"}
            },
        }
    )
    assert project_subgraph_output(projection, child_output={"internal": {"status": "passed", "secret": 1}}) == {
        "status": "passed"
    }
```

Also test that input parsing rejects `child_output_pointer`, output parsing rejects root/config/predecessor/graph-input sources, pointer misses fail deterministically, and object keys are NFC-unique.

**Step 2: Run RED**

Run: `uv run pytest packages/framework/graph-engine/tests/graph/test_input_projection.py packages/framework/graph-engine/tests/graph/test_output_projection.py -v`  
Expected: FAIL because the new types/module do not exist.

**Step 3: Implement the two closed evaluators**

Add and export the symmetric public parsers `parse_input_projection(value: object) -> InputProjectionDef` and `parse_output_projection(value: object) -> OutputProjectionDef`; both use the closed discriminated union rather than making callers construct a private `TypeAdapter`. Add `graph_input` as a required keyword to `project_task_input`. Define `OutputProjectionDef` as the discriminated union of literal, `child_output_pointer`, recursively nested object and tuple. Add `project_subgraph_output` and `validate_output_projection_compile`; do not reuse `root_pointer` for child output.

Migrate every existing call in the same commit: run `rg -n 'project_task_input\(' packages tests` and pass the current graph input explicitly in planner, Product runner and live-fixture helpers. At this stage the value may equal invocation root for legacy top-level tests, but no caller may rely on a default or be deferred to Task 8.

**Step 4: Run GREEN and typecheck**

Run: `uv run pytest packages/framework/graph-engine/tests/graph/test_input_projection.py packages/framework/graph-engine/tests/graph/test_output_projection.py -q`  
Run: `uv run pytest tests/product/test_phase3_live_fixture_contract.py tests/product/test_agent_execution_contracts.py -q`  
Run: `uv run pyright packages/framework/graph-engine/graph_engine/graph`  
Expected: PASS and `rg -n 'project_task_input\(' packages tests` shows every call passing `graph_input=` or forwarding a context that contains it.

**Step 5: Commit**

```bash
git add packages/framework/graph-engine/graph_engine/graph packages/framework/graph-engine/graph_engine/runtime/planner.py packages/framework/graph-engine/tests/graph tests/product/product_runner.py tests/product/test_phase3_live_fixture_contract.py
git commit -m "feat(graph): add graph input and child output projections"
```

### Task 7: Preserve and compile generic subgraph I/O contracts

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/graph/schema.py`
- Modify: `packages/framework/graph-engine/graph_engine/graph/compiler.py`
- Modify: `packages/framework/graph-engine/tests/graph/test_schema_and_compiler.py`

**Step 1: Write RED schema/compiler tests**

```python
def test_compiled_subgraph_preserves_public_io_contract() -> None:
    compiled = compile_workflow(contracted_subgraph_workflow(), contracted_registry())
    definition = compiled.graphs["parent"].nodes["child"].definition
    assert definition.input_schema == "toy.feature.workflow.run.input.v1"
    assert definition.output_schema == "toy.feature.workflow.run.output.v1"
    assert definition.output_projection is not None
```

Add matrix cases proving these fields are subgraph-only, schema IDs are qualified, both IDs occur in `WorkflowDef.schemas`, both registry entries exist, and compiled JSON round-trip/digest includes set values while omitting unset legacy defaults.

**Step 2: Run RED**

Run: `uv run pytest packages/framework/graph-engine/tests/graph/test_schema_and_compiler.py -k 'subgraph and contract' -v`  
Expected: FAIL because `NodeDef` and `CompiledNodeDefinition` lack the fields.

**Step 3: Extend the generic schema and compiler**

Add `input_schema`, `output_projection`, and `output_schema` to subgraph allowed fields. Require output projection and output schema to appear together; allow a legacy subgraph to omit all three. Validate referenced schemas against both `WorkflowDef.schemas` and the existing SchemaRegistry. Preserve them in `CompiledNodeDefinition` without adding module metadata.

**Step 4: Verify**

Run: `uv run pytest packages/framework/graph-engine/tests/graph/test_schema_and_compiler.py -q`  
Expected: PASS, including existing legacy compiled serialization tests.

**Step 5: Commit**

```bash
git add packages/framework/graph-engine/graph_engine/graph/schema.py packages/framework/graph-engine/graph_engine/graph/compiler.py packages/framework/graph-engine/tests/graph/test_schema_and_compiler.py
git commit -m "feat(graph): compile generic subgraph io contracts"
```

### Task 8: Execute subgraph input projection before GraphStarted

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/runtime/planner.py`
- Modify: `packages/framework/graph-engine/graph_engine/runtime/engine.py`
- Modify: `packages/framework/graph-engine/graph_engine/runtime/models.py`
- Modify: `packages/framework/graph-engine/tests/runtime/test_planner_input_projection.py`
- Create: `packages/framework/graph-engine/tests/runtime/test_planner_subgraph_contracts.py`
- Create: `packages/framework/graph-engine/tests/runtime/test_structural_failure_fold.py`
- Create: `packages/framework/graph-engine/tests/runtime/test_planner_fatal_barrier.py`

**Step 1: Write RED runtime tests**

Create a parent graph whose subgraph call projects one root field, one predecessor field and static config. Assert the next `GraphStarted.input` equals the projected object and the child start-token payload is byte-equivalent. Parameterize missing field, extra field and wrong type; each must emit an existing `NodeFailed` whose `TaskFailure.kind == "invalid_input"`, followed by deterministic graph/invocation failure, with no child `GraphStarted` or task dispatch. Add fold tests proving this intrinsic structural failure is accepted only for an active structural activation with zero task attempts and one of `invalid_input | invalid_output`; every other no-attempt `NodeFailed` remains invalid.

Add a parallel barrier fixture: one ready contracted subgraph has invalid input while a sibling task attempt is running. Assert repeated planning emits no `NodeFailed`/`GraphFailed`/`InvocationFinished`, no edge token and no **new** task attempt; `plan_running_tasks()` may reconstruct only the already persisted sibling. After its success/failure/effect settlement, the next plan emits the same canonical invalid-input failure chain exactly once. Replay both sibling outcome variants and reject a forged chain inserted before settlement.

```python
assert graph_started.input == {
    "change_id": "C-1",
    "evidence": {"status": "passed"},
    "policy": "strict",
}
```

**Step 2: Run RED**

Run: `uv run pytest packages/framework/graph-engine/tests/runtime/test_planner_subgraph_contracts.py -k input -v`  
Expected: FAIL because `_start_subgraph()` currently uses only `node.definition.input`.

**Step 3: Add the schema-registry planner seam**

Add keyword-only `schemas` to planner public APIs and store the immutable registry view in `_PlannerState`. If any compiled node has an input/output schema and `schemas is None`, fail immediately with `PlanningError`; permit `None` only for a wholly uncontracted legacy Workflow. Add `_subgraph_input()`: preserve static-only behavior when projection is absent; otherwise call `project_task_input` with invocation root, current graph input, node config and predecessor tokens, then validate `input_schema`. Update `_expected_activation_input()` to pass `graphs[activation.graph_instance_id].input`.

Update `_validate_projection()` in this Task—not Task 9—to recompute the expected child input from the parent activation, predecessor tokens, current parent `GraphStarted.input`, node config, compiled projection and frozen schemas. Remove the old equality check against only `node.definition.input`. Ensure every recursive `validate_event_history()`/`plan_next()` call receives the same schema registry.

Introduce an internal immutable `_FatalCandidate` and `_pending_fatal_violation()` seam shared by Tasks 8, 9 and 11. Detect invalid input from ready tokens **before** appending `NodeActivated` or scheduling work; also scan already active/settling structural activations. Split each planner wave into (1) settle already persisted attempt/child outcomes, (2) globally collect all now-derivable contract/route fatal candidates without emitting routes, and only if none exist (3) emit routes or create attempts. This prevents activation iteration order from leaking a sibling token before a later candidate in the same wave is seen.

Sort simultaneous candidates by `(graph_instance_id, activation-or-node-id, reason)` and choose the first on every replayable scan. If `_has_running_attempt(state)` anywhere in the Invocation, return only causally necessary settlement events, with no new tasks/routes/terminal events, and deterministically rediscover the cause on the next call; existing running attempts may only be reconstructed and settle. When none remain, materialize the invalid-input candidate's normal deterministic `NodeActivated` and atomically follow it with `NodeFailed`/`GraphFailed`/ancestor propagation/`InvocationFinished`; an already active invalid-output candidate needs no second activation. This fatal preflight precedes normal terminal-task settlement and every scheduling/route-emission pass so a sibling outcome cannot escape downstream after a cause is derivable.

In `runtime/models.py`, extend the existing compiled-graph-agnostic fold without changing event models or persisted ledger format: syntactically admit an active zero-attempt activation only when failure kind is `invalid_input | invalid_output`, and mark it `structural_failure=True`; invalid input has no child graph, while invalid output may have a completed child. The fold cannot identify node kind by itself, so `validate_event_history()` terminal causal proof must then require that the compiled activation is the corresponding contracted subgraph and authenticate the exact violation before replay accepts it. Wire every Engine planning/replay call with `composition.registries.schemas`.

**Step 4: Verify legacy and contracted behavior**

Run: `uv run pytest packages/framework/graph-engine/tests/runtime/test_planner_input_projection.py packages/framework/graph-engine/tests/runtime/test_planner_subgraph_contracts.py packages/framework/graph-engine/tests/runtime/test_structural_failure_fold.py packages/framework/graph-engine/tests/runtime/test_planner_fatal_barrier.py -q`  
Run: `uv run pytest packages/framework/graph-engine/tests/runtime/test_engine.py -q`  
Expected: PASS; a legacy subgraph with no projection still receives only static node input, a contracted Workflow rejects `schemas=None`, and forged projected child input fails replay.

**Step 5: Commit**

```bash
git add packages/framework/graph-engine/graph_engine/runtime/planner.py packages/framework/graph-engine/graph_engine/runtime/engine.py packages/framework/graph-engine/graph_engine/runtime/models.py packages/framework/graph-engine/tests/runtime
git commit -m "feat(runtime): validate projected subgraph input"
```

### Task 9: Project and validate public subgraph output with replay authentication

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/runtime/planner.py`
- Modify: `packages/framework/graph-engine/tests/runtime/test_planner_subgraph_contracts.py`
- Modify: `packages/framework/graph-engine/tests/runtime/test_planner_fatal_barrier.py`
- Modify: `packages/framework/graph-engine/tests/runtime/test_engine.py`

**Step 1: Write RED completion and forgery tests**

Assert child `GraphCompleted.output` retains the raw terminal value, parent `NodeCompleted.output` contains only the output projection, and downstream `TokenOffered.payload` equals that public value. Add invalid-output cases proving an `invalid_output` `NodeFailed` plus graph/invocation failure is emitted, with no parent completion/token. Add replay cases that tamper separately with `GraphStarted.input`, parent `NodeCompleted.output`, and downstream token payload; all must fail even when adjacent forged events are made mutually consistent. Repeat invalid output with a parallel sibling attempt still live: Task 8's barrier must suppress the terminal chain and all new work until settlement, then emit the same cause once.

**Step 2: Run RED**

Run: `uv run pytest packages/framework/graph-engine/tests/runtime/test_planner_subgraph_contracts.py -k 'output or replay or forged' -v`  
Expected: FAIL because `_settle_subgraph_activation()` forwards raw child output.

**Step 3: Implement public output settlement**

Add `_subgraph_public_output()` to apply `project_subgraph_output` and validate `output_schema` before constructing parent `NodeCompleted`. On failure, register Task 8's `_FatalCandidate` instead of immediately routing/completing the parent. The child graph is already complete, but the parent structural activation is not; after the barrier clears, use `TaskFailure(kind="invalid_output", retryable=False)` and the intrinsic structural-failure fold path. Extend terminal causal-proof validation so the exact compiled projection/schema violation authenticates the `NodeFailed`/`GraphFailed`/ancestor propagation/`InvocationFinished` chain. Recompute expected child input and expected public parent output from the compiled node plus frozen schemas, and pass that registry through every recursive history validation call. Do not change `events.py` or the ledger/checkpoint persistence schema.

**Step 4: Verify no new event type**

Run: `uv run pytest packages/framework/graph-engine/tests/runtime/test_planner_subgraph_contracts.py packages/framework/graph-engine/tests/runtime/test_ledger_and_checkpoint.py tests/product/test_replay_properties.py -q`  
Expected: PASS and the runtime event-kind set remains unchanged.

**Step 5: Commit**

```bash
git add packages/framework/graph-engine/graph_engine/runtime/planner.py packages/framework/graph-engine/tests/runtime tests/product/test_replay_properties.py
git commit -m "feat(runtime): validate public subgraph output"
```

### Task 10: Add explicit exclusive/fanout declarations to the generic compiler

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/graph/schema.py`
- Modify: `packages/framework/graph-engine/graph_engine/graph/compiler.py`
- Modify: `packages/framework/graph-engine/tests/graph/test_schema_and_compiler.py`

**Step 1: Write the RED routing matrix**

Test: valid exclusive with one otherwise; exclusive missing/multiple otherwise; condition plus otherwise; unconditional non-otherwise edge; exclusive with min; valid fanout with `min_matches=0/1`; fanout missing min, fanout otherwise, min greater than outgoing count; duplicate canonical route; and a legacy node with omitted routing.

```python
with pytest.raises(CompileError, match="exclusive route .* requires exactly one otherwise"):
    compile_workflow(exclusive_without_otherwise(), registry)
```

Add direct compatibility assertions around a frozen legacy fixture: `EdgeDef.model_dump()`, the containing `CompiledWorkflow.model_dump()`, workflow digest and lock canonical bytes must all remain byte-identical and must not contain `"otherwise": false`, even when the old YAML is parsed after the field exists.

**Step 2: Run RED**

Run: `uv run pytest packages/framework/graph-engine/tests/graph/test_schema_and_compiler.py -k routing -v`  
Expected: FAIL because routing fields do not exist.

**Step 3: Implement closed routing validation**

Add `RoutingDef(mode, min_matches)`, `NodeDef.routing`, and `EdgeDef.otherwise=False`. Freeze sparse serialization at the `EdgeDef` boundary with a Pydantic wrap `@model_serializer`: call the normal handler, remove the `otherwise` key whenever its value is false, and retain `otherwise: true`. Because `CompiledWorkflow` nests the same `EdgeDef`, this rule applies to direct model dumps, compiler canonical projection, workflow digest and lock projection without a second representation. Do not rely only on `exclude_unset`, because parsing legacy input may explicitly materialize the default.

Compiler validates each source node as one route set and rejects duplicate `(to, condition or "", otherwise)` keys. Do not require routing on every generic multi-edge node; that final modular-only rule belongs to module validation/Assembler.

**Step 4: Verify legacy compiler compatibility**

Run: `uv run pytest packages/framework/graph-engine/tests/graph/test_schema_and_compiler.py -q`  
Run: `uv run pytest packages/framework/graph-engine/tests/composition/test_lock_model.py -q`  
Expected: PASS; pre-existing `CompiledWorkflow` dump, workflow digest and lock golden remain byte-identical, and only explicit `otherwise: true` appears in new compiled bytes.

**Step 5: Commit**

```bash
git add packages/framework/graph-engine/graph_engine/graph/schema.py packages/framework/graph-engine/graph_engine/graph/compiler.py packages/framework/graph-engine/tests/graph/test_schema_and_compiler.py
git commit -m "feat(graph): add explicit workflow routing modes"
```

### Task 11: Make explicit route selection atomic, canonical and replayable

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/runtime/planner.py`
- Create: `packages/framework/graph-engine/tests/runtime/test_planner_routing.py`
- Modify: `packages/framework/graph-engine/tests/runtime/test_planner_fatal_barrier.py`
- Modify: `packages/framework/graph-engine/tests/runtime/test_planner.py`

**Step 1: Write RED exclusive/fanout runtime tests**

Cover one-match exclusive, all-false otherwise, overlap, fanout 0/1 minimum, multiple matches, and explicit edge declaration reorder. For overlap and insufficient matches, assert the source already has exactly one `NodeCompleted`, the next `PlanResult` contains `GraphFailed` → ancestor propagation → `InvocationFinished(failed)`, contains no later `NodeFailed` for that source, and emits no `TokenOffered`. Repeat both failures while an unrelated parallel task attempt is live: the fatal barrier emits neither terminal chain nor new route/work until settlement, then emits the canonical graph-level cause once. For reordered explicit routes, assert identical token IDs, token order, payload and terminal projection.

**Step 2: Run RED**

Run: `uv run pytest packages/framework/graph-engine/tests/runtime/test_planner_routing.py -v`  
Expected: FAIL because `_route_completion()` independently emits every true/unconditional edge using global declaration index.

**Step 3: Implement `_select_routes()` and dual token indexing**

Evaluate every condition against one immutable `{input, output}` scope before emitting. Exclusive returns the sole match or reports `ambiguous_route:<graph>/<node>:<canonical-keys>` on overlap. Fanout returns all matches or reports `insufficient_route_matches:<graph>/<node>:<actual>/<minimum>`. Catch the internal selection error before token creation and register a Task 8 `_FatalCandidate`. Because route selection happens after the source `NodeCompleted`, do **not** append a contradictory `NodeFailed`; after the shared barrier has no live attempts, atomically append graph-level `GraphFailed`, propagate failure through active ancestor subgraph calls, and finish with `InvocationFinished(failed)`. Extend graph-terminal causal proof so replay recomputes and authenticates the exact canonical route failure and rejects a forged reason, premature failure chain or any edge token.

For explicit routing, sort the source node's route keys and use canonical rank in `_edge_token_id`; for legacy routing, retain the old global edge index.

**Step 4: Add replay forgery cases and run GREEN**

Replay must reject a missing selected branch, an extra branch and forged otherwise token. Run:

`uv run pytest packages/framework/graph-engine/tests/runtime/test_planner_routing.py packages/framework/graph-engine/tests/runtime/test_planner.py -q`  
Expected: PASS; existing `join: all|any` tests remain unchanged.

**Step 5: Commit**

```bash
git add packages/framework/graph-engine/graph_engine/runtime/planner.py packages/framework/graph-engine/tests/runtime
git commit -m "feat(runtime): select explicit routes deterministically"
```

---

## Phase C — Add Workflow modules, deterministic assembly and slot lowering (6–8 PD)

### Task 12: Add the closed Workflow module schema

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/graph/schema.py`
- Create: `packages/framework/graph-engine/graph_engine/graph/module_schema.py`
- Modify: `packages/framework/graph-engine/graph_engine/graph/__init__.py`
- Create: `packages/framework/graph-engine/tests/graph/test_workflow_module_schema.py`

**Step 1: Write RED role and reference tests**

Use one minimal Product root module and one Feature module. Test qualified module/owner IDs, normalized versions, duplicate YAML keys, private-by-default graphs, export target existence, declared import aliases, declared slots, Product-only Entrypoints, Feature-no-Entrypoints, and extra fields forbidden.

```yaml
schema_version: "1"
role: feature
owner_id: toy.feature
module_id: toy.feature.workflow
module_version: 1.0.0
entrypoints: {}
imports: {}
exports:
  run:
    graph: run
    input_schema: toy.feature.workflow.run.input.v1
    output_schema: toy.feature.workflow.run.output.v1
    output_projection:
      type: child_output_pointer
      pointer: ""
capability_slots:
  worker.execute:
    contract_id: toy.feature.agent.worker.v1
schemas:
  - toy.feature.workflow.run.input.v1
  - toy.feature.workflow.run.output.v1
resources: []
effects: []
retry:
  once: {max_attempts: 1}
timeout:
  short: {run_seconds: 30}
graphs:
  run:
    max_activations: 2
    start: work
    nodes:
      work:
        kind: task
        capability_slot: worker.execute
        retry: once
        timeout: short
      done: {kind: end}
    edges:
      - {from: work, to: done}
```

**Step 2: Run RED**

Run: `uv run pytest packages/framework/graph-engine/tests/graph/test_workflow_module_schema.py -v`  
Expected: FAIL because module parsing and unresolved node fields do not exist.

**Step 3: Implement one authoritative node/module language**

Add optional `capability_slot` and `graph_import` to `NodeDef`; task requires exactly one of `capability`/`capability_slot`, subgraph exactly one of `graph`/`graph_import`. Generic compiler rejects either unresolved field. Define `WorkflowImportDef(owner_id,module_id,export)`, `WorkflowExportDef(graph,input_schema,output_schema,output_projection)`, `CapabilitySlotDef(contract_id)`, and `WorkflowModuleDef` with optional `name`. Product role requires a non-empty `name`; Feature role forbids it. `parse_workflow_module(bytes | str)` must use a duplicate-key-rejecting safe YAML loader.

Feature modules require at least one export and no Entrypoints. Product modules require Entrypoints and may import; Assurance-specific Feature-to-Feature and structural-only rules stay in Assembler.

**Step 4: Verify**

Run: `uv run pytest packages/framework/graph-engine/tests/graph/test_workflow_module_schema.py packages/framework/graph-engine/tests/graph/test_schema_and_compiler.py -q`  
Expected: PASS; compiler rejects an unresolved slot/import with a location-rich `CompileError`.

**Step 5: Commit**

```bash
git add packages/framework/graph-engine/graph_engine/graph packages/framework/graph-engine/tests/graph
git commit -m "feat(graph): add closed workflow module schema"
```

### Task 13: Add the third, modular ProductManifest form

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/composition/models.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/declarative.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/lock.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/__init__.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_manifest_projection.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_declarative_sources.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_registry_platform.py`

**Step 1: Write RED XOR and canonical projection tests**

Define `WorkflowModuleRequirement(module_id, owner_id, resource_id)` and `WorkflowSlotBinding(module_id, slot, capability_id, contract_id)`. Test exactly one of `workflow`, `workflow_resource_id`, `workflow_module`; modular-only auxiliary fields; unique requirement module/resource IDs; unique `(module_id,slot)`; Product root owner/version/entrypoints; and rejection of auxiliary fields on legacy forms.

```python
assert sum(
    value is not None
    for value in (manifest.workflow, manifest.workflow_resource_id, manifest.workflow_module)
) == 1
```

Also serialize an unchanged legacy fixture and compare it byte-for-byte with the existing lock/manifest golden.
Add path-sensitive declarative security cases: the data map at exactly `DeclarativeProductDocument` path `("workflow_module", "imports")` is accepted and parsed as `WorkflowImportDef`; `imports` at the document root, inside arbitrary node input/config, inside a Plugin document/resource, or at any other nesting remains rejected as an executable key.

**Step 2: Run RED**

Run: `uv run pytest packages/framework/graph-engine/tests/composition/test_manifest_projection.py packages/framework/graph-engine/tests/composition/test_declarative_sources.py -v`  
Expected: FAIL because modular fields are unknown.

**Step 3: Add the modular manifest model and declarative mirror**

Add the three modular fields with empty defaults. Modular root `owner_id` must equal `product_id`, role must be `product`, module version must equal product version, and root Entrypoints must equal manifest Entrypoints. Update canonical manifest projection so modular values are authenticated, but omit default empty fields for legacy manifests. Do not change lock version or add a lock field.

Replace the current context-free recursive executable-key rejection with a path-aware walker whose default allowed-path set is empty. Only `_parse_product_document()` passes `{("workflow_module", "imports")}` into `_safe_yaml_mapping()`/`_strict_yaml_value()`; Plugin/config-tree/resource parsers retain the empty set. Permit the literal key `imports` only when the walker's current key path equals that tuple, continue scanning its children for every other executable key, then validate the value through the closed `WorkflowModuleDef`/`WorkflowImportDef` models. Keep `imports` on `_EXECUTABLE_KEYS` and do not relax `_SAFE_MEDIA_TYPES`.

**Step 4: Add the config-tree negative assertion and verify**

Attempting to use module MIME through config-tree or `.aa` must remain rejected; do not add module MIME to declarative `_SAFE_MEDIA_TYPES`.

Run: `uv run pytest packages/framework/graph-engine/tests/composition/test_manifest_projection.py packages/framework/graph-engine/tests/composition/test_declarative_sources.py packages/framework/graph-engine/tests/composition/test_lock_model.py -q`  
Expected: PASS with unchanged legacy lock golden.

**Step 5: Commit**

```bash
git add packages/framework/graph-engine/graph_engine/composition packages/framework/graph-engine/tests/composition
git commit -m "feat(composition): add modular product manifest form"
```

### Task 14: Authenticate capability contract IDs on existing bindings

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/plugin_api.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/models.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/contributions.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/registries.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/lock.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_plugin_contracts.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_registries.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_lock_model.py`

**Step 1: Write RED contribution/registry tests**

Test a qualified `contract_id`, an invalid ID, propagation into `CapabilityBindingEntry`, contribution/registry projection, and the existing `None` compatibility case. Assert the old lock golden remains exact when all legacy bindings omit the field.

```python
binding = CapabilityBindingContribution(
    capability_id="toy.product.agent.worker.execute",
    target_capability_id="toy.runtime.execute",
    contract_id="toy.feature.agent.worker.v1",
)
assert resolved.bindings[binding.capability_id].contract_id == "toy.feature.agent.worker.v1"
```

**Step 2: Run RED**

Run: `uv run pytest packages/framework/graph-engine/tests/composition/test_plugin_contracts.py packages/framework/graph-engine/tests/composition/test_registries.py -k contract_id -v`  
Expected: FAIL because bindings have no contract field.

**Step 3: Thread the optional field through existing deep interfaces**

Add `contract_id: str | None = None` to contribution, registry entry, bound handler consistency checks and canonical projections. Validate qualified IDs. Omit `None` from projections so legacy lock bytes do not change; include non-None values so source/lock authentication covers modular slot contracts.

**Step 4: Verify all five Registry invariants**

Run: `uv run pytest packages/framework/graph-engine/tests/composition/test_plugin_contracts.py packages/framework/graph-engine/tests/composition/test_registries.py packages/framework/graph-engine/tests/composition/test_lock_model.py -q`  
Expected: PASS, including the assertion that RegistrySet still has only sources/capabilities/schemas/resources/effects.

**Step 5: Commit**

```bash
git add packages/framework/graph-engine/graph_engine/plugin_api.py packages/framework/graph-engine/graph_engine/composition packages/framework/graph-engine/tests
git commit -m "feat(composition): authenticate binding contract ids"
```

### Task 15: Authenticate and freeze the exact module resource closure

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/composition/workflow_assembler.py`
- Create: `packages/framework/graph-engine/tests/composition/test_workflow_assembler.py`

**Step 1: Write RED resource-closure tests through the internal loading seam**

Cover missing required resource, wrong MIME, resource owner mismatch, module ID mismatch, module version versus selected descriptor mismatch, duplicate module identity, undeclared extra module resource owned by a selected descriptor, ambient module owned by an unselected descriptor, frozen resource bytes versus a later filesystem replacement, and every permutation of requirement/registry construction order.

```python
left = _load_authenticated_modules(manifest=manifest_ab, descriptors=descriptors, registries=registry_ab)
right = _load_authenticated_modules(manifest=manifest_ba, descriptors=descriptors, registries=registry_ba)
assert canonical_json_bytes(left.canonical_projection()) == (
    canonical_json_bytes(right.canonical_projection())
)
```

**Step 2: Run RED**

Run: `uv run pytest packages/framework/graph-engine/tests/composition/test_workflow_assembler.py -k 'resource or order or owner' -v`  
Expected: FAIL because authenticated module loading does not exist.

**Step 3: Implement authenticated loading only**

Define `WorkflowAssemblyError`, an immutable internal `LoadedModule`/`LoadedModules` value, and private `_load_authenticated_modules(...) -> LoadedModules`. Do **not** expose or test `assemble_product_workflow()` in this loading-only commit: it cannot truthfully return a `WorkflowDef` until symbol lowering exists in Task 16. The first stage enumerates module-MIME ResourceEntries already frozen in Registry: a module owned by a selected descriptor must appear exactly once in `manifest.workflow_module_resources`, while modules owned outside the selected descriptor closure are ignored. Read required bytes only from `registries.resources`, verify exact MIME/owner/module/version against `PluginDescriptor`, and sort by `(module_id, owner_id, resource_id)`. It must not reopen a file, query entry points or scan the working directory.

**Step 4: Verify fail-closed resource selection**

Run: `uv run pytest packages/framework/graph-engine/tests/composition/test_workflow_assembler.py -k 'resource or order or owner' -q`  
Expected: PASS; an ambient extra ResourceEntry cannot change the immutable loaded closure. The public Assembler interface is introduced only after both symbol and slot lowering are complete in Task 17.

**Step 5: Commit**

```bash
git add packages/framework/graph-engine/graph_engine/composition/workflow_assembler.py packages/framework/graph-engine/tests/composition/test_workflow_assembler.py
git commit -m "feat(composition): authenticate workflow module resources"
```

### Task 16: Resolve imports/exports, private graphs and namespaces deterministically

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/composition/workflow_assembler.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_workflow_assembler.py`

**Step 1: Write RED symbol and ownership tests through the internal lowering seam**

Cover undeclared import alias, missing export, attempted private graph reference, duplicate local/export/import names, module cycle, invalid local symbol, Feature-to-Feature graph import, Product direct graph/capability reference, Product task node, Feature Entrypoint, modular multi-out node without routing, and a valid Product → Feature import. Add two Features that both define local graph `run`, retry `once` and timeout `short` with different policy bodies; assembly must succeed and produce distinct deterministic qualified symbols. Add a same-module dangling policy reference that fails closed.

**Step 2: Run RED**

Run: `uv run pytest packages/framework/graph-engine/tests/composition/test_workflow_assembler.py -k 'import or export or private or collision or product' -v`  
Expected: FAIL because the loading-only implementation does not lower symbols.

**Step 3: Implement one deterministic lowering pass**

- Add private `_lower_module_symbols(loaded: LoadedModules) -> WorkflowDef`; it is an internal intermediate that may still contain authenticated `capability_slot` values and therefore must not be passed to `compile_workflow()`. Do not expose `assemble_product_workflow()` until Task 17 can eliminate slots as well.
- Build `(module_id, export)` and import-alias tables from sorted modules.
- Namespace every module-local symbol before merging with exact, reversible names: graph `<module_id>.graph.<local-id>`, retry `<module_id>.retry.<local-id>`, timeout `<module_id>.timeout.<local-id>`. Rewrite local graph calls, Entrypoints, export targets and node retry/timeout references through the owner module's tables. Never deduplicate same-named policies across modules and never conflict merely because two modules both call a policy `once` or `short`.
- Merge each module's declared schemas/resources/effects as sorted exact unions and reject a reference not registered in the authenticated Registry closure.
- Rewrite a private local `graph` reference only through its owner module's qualified table.
- Rewrite Product `graph_import` to the resolved exported graph and copy `input_schema`, export `output_projection`, and `output_schema` onto the ordinary subgraph node.
- Reject all unresolved `graph_import` and all module metadata before returning the internal `WorkflowDef`; retain only the exact declared `capability_slot` set for Task 17.
- Require explicit routing on every modular node with more than one outgoing edge.
- Apply the Assurance Product policy: Feature import tables are empty; Product root has structural nodes only.

**Step 4: Verify the symbol-lowered intermediate is closed except for slots**

Run: `uv run pytest packages/framework/graph-engine/tests/composition/test_workflow_assembler.py -q`  
Expected: PASS; serializing the internal result contains neither `module_id`, `imports`, `exports` nor `graph_import`, contains only the exact authenticated `capability_slot` set, and changing one Feature's private `once` policy cannot collide with another Feature's policy namespace. The generic compiler still rejects this intermediate.

**Step 5: Commit**

```bash
git add packages/framework/graph-engine/graph_engine/composition/workflow_assembler.py packages/framework/graph-engine/tests/composition/test_workflow_assembler.py
git commit -m "feat(composition): lower public workflow module symbols"
```

### Task 17: Lower exactly matched capability slots and connect the existing resolver

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/composition/workflow_assembler.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/resolver.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_workflow_assembler.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_registry_platform.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_lock_model.py`

**Step 1: Write RED slot and resolve tests**

Cover missing, extra, duplicate and unknown slot; contract mismatch; selected capability is not a `CapabilityBindingEntry`; binding contract missing; target handler outside the exact descriptor closure; and a valid slot that lowers to a concrete capability. Resolve both a legacy and modular product and assert both return the same `CompiledWorkflow` class.

```python
assembled = assemble_product_workflow(manifest=manifest, descriptors=descriptors, registries=registries)
task = assembled.graphs["toy.feature.workflow.graph.feature-run"].nodes["work"]
assert task.capability == "toy.product.agent.worker.execute"
assert task.capability_slot is None
```

**Step 2: Run RED**

Run: `uv run pytest packages/framework/graph-engine/tests/composition/test_workflow_assembler.py -k slot -v`  
Expected: FAIL because slots remain unresolved.

**Step 3: Implement exact-set slot lowering, expose the complete Assembler, and connect the resolver**

Compare the union of selected module slots against ProductManifest slot bindings as exact sets. Verify contract ID on module requirement, manifest binding and Registry binding are identical; capability owner is Product-owned; target provenance belongs to a selected descriptor. Rewrite to concrete `capability` and remove `capability_slot` before compile.

Now expose the one frozen public `assemble_product_workflow(...) -> WorkflowDef`: call `_load_authenticated_modules()`, `_lower_module_symbols()`, then exact slot lowering, assert no module/import/export/slot metadata remains, and only then return. No earlier Task exposes a partial public Assembler.

In `_compile_product_workflow`, branch on `manifest.workflow_module`; call Assembler with descriptors and registries, then call the same existing `compile_workflow(workflow, registries)` used by legacy forms. All Assembler errors must occur before lock creation, Invocation creation or ledger append.

**Step 4: Prove lock-v2 integrity**

Test module byte drift changes authenticated resource/contribution/final workflow/lock digest; registration permutations produce identical assembled/lock bytes; modular composition cannot resume a legacy lock; same-composition resume still passes. Run:

`uv run pytest packages/framework/graph-engine/tests/composition/test_workflow_assembler.py packages/framework/graph-engine/tests/composition/test_registry_platform.py packages/framework/graph-engine/tests/composition/test_lock_model.py -q`  
Expected: PASS without changing lock schema/version.

**Step 5: Commit**

```bash
git add packages/framework/graph-engine/graph_engine/composition packages/framework/graph-engine/tests/composition
git commit -m "feat(composition): assemble modular products before compile"
```

---

## Phase D — Move Workflow ownership into six Features and Product main (9–11 PD)

Phase D 的每个 Feature 提取 Task 都必须消费 Task 1 的完整 root-pointer 与 subgraph-call inventory。对每条 local subgraph call，记录 child 实际读取字段并显式添加 caller `input_projection`；不得假设 child 自动继承 export input。凡存在 nested subgraph 的 Feature，`test_workflow_module.py` 至少驱动一条两层以上路径，断言 `change_id`、artifact refs 和 budget 从 export → private child → grandchild 保持一致，同时断言未声明字段不会穿透边界。Execution 的两个 export 没有 local subgraph relation，必须分别驱动并断言两者保持独立。

### Task 18: Move the 33 logical agent-job contracts to Feature-owned catalogs

**Files:**

- Create: `packages/clients/agent-runtime-contracts/agent_runtime_contracts/execution_contract.py`
- Modify: `packages/clients/agent-runtime-contracts/agent_runtime_contracts/__init__.py`
- Create: each `packages/features/assurance-<feature>/assurance_<feature>/contracts/workflow.py`
- Modify: `packages/products/assurance-product/assurance_product/{agent_contracts,models,output_routes,binding_builder,opencode_agents}.py`
- Modify: `tests/product/test_agent_execution_contracts.py`, `tests/product/test_binding_builder.py`, `tests/product/test_binding_coverage.py`
- Modify: each Feature `tests/test_contracts.py`

**Step 1: Write RED ownership/count tests**

Define the provider-neutral value model with `contract_id`, `skill_id`, `agent_profile`, and `ResourceClaims | ResourceClaimTemplate`. Each Feature exports an immutable `AGENT_JOB_CONTRACTS` keyed by its exact base names. Test counts per Feature are 4, 14, 2, 5, 2, 6; union is 33; phase expansion is exactly 99; aliases still match existing IDs.

```python
assert sum(len(catalog) for catalog in FEATURE_AGENT_JOB_CATALOGS) == 33
assert len(expand_agent_job_slots(FEATURE_AGENT_JOB_CATALOGS)) == 99
assert all("opencode" not in contract.model_dump_json().lower() for contract in all_contracts)
assert all("cursor" not in contract.model_dump_json().lower() for contract in all_contracts)
```

**Step 2: Run RED**

Run: `uv run pytest tests/product/test_agent_execution_contracts.py packages/features/assurance-intake/tests/test_contracts.py -v`  
Expected: FAIL because Feature catalogs and shared model do not exist.

**Step 3: Move data ownership while keeping a Product compatibility facade**

Move the current Product `_AGENT_SKILL_PROFILES` and resource-claim/output-route data into the responsible Feature `contracts/workflow.py`. `assurance_product.agent_contracts` imports and combines only those public catalogs, preserving its current exported names temporarily. Derive `PREPARE_IDS` from the union rather than maintaining a second tuple. The Product keeps provider assignment, permission/request policy and secret selection.

Update `binding_builder` so prepare/execute/finalize `CapabilityBindingContribution` entries all receive the base job's contract ID. Keep all 99 concrete aliases and targets unchanged.

**Step 4: Verify ownership and compatibility**

Run: `uv run pytest packages/clients/agent-runtime-contracts/tests packages/features/assurance-intake/tests packages/features/assurance-generation/tests packages/features/assurance-execution/tests packages/features/assurance-quality/tests packages/features/assurance-healing/tests packages/features/assurance-improvement/tests tests/product/test_agent_execution_contracts.py tests/product/test_binding_builder.py tests/product/test_binding_coverage.py -q`  
Run: `uv run lint-imports`  
Expected: PASS with 33 jobs/99 aliases and no Feature import from `assurance_product`.

**Step 5: Commit**

```bash
git add packages/clients/agent-runtime-contracts packages/features packages/products/assurance-product/assurance_product tests/product/test_agent_execution_contracts.py tests/product/test_binding_builder.py tests/product/test_binding_coverage.py
git commit -m "refactor(agents): move logical execution contracts to features"
```

### Task 19: Publish the Intake Workflow module and its two public contracts

**Files:**

- Create: `packages/features/assurance-intake/assurance_intake/resources/workflow/module.yaml`
- Create: `packages/features/assurance-intake/assurance_intake/resources/schemas/workflow/{prepare-input,prepare-output,case-input,case-output}.v1.schema.json`
- Modify: `packages/features/assurance-intake/assurance_intake/{plugin.py,plugin-declaration.json}`
- Modify: `packages/features/assurance-intake/assurance_intake/contracts/workflow.py`
- Create: `packages/features/assurance-intake/tests/test_workflow_module.py`

**Step 1: Write RED module-resource tests**

Assert resource ID `assurance.intake.workflow.module.v1`, module ID `assurance.intake.workflow`, owner, two exports, four base jobs × three slots, registered I/O schemas, empty imports, no `assurance.product.agent.*`, no Product or concrete Client-adapter import, and no `root_pointer` anywhere in Feature graphs. Assert the six owned legacy graph IDs remain present and `prepare → entry`, but `entry` now owns the exact reviewed relocation map from Task 1. Audit every Intake local subgraph edge and drive `prepare → intake → explore` (or another two-level path) to prove current graph input is explicitly projected at each hop. Importing the provider-neutral `agent_runtime_contracts` value model remains allowed.

**Step 2: Run RED**

Run: `uv run pytest packages/features/assurance-intake/tests/test_workflow_module.py -v`  
Expected: FAIL because the module resource is missing.

**Step 3: Extract the owned graphs and close the public boundary**

Move copies of legacy `entry`, `case`, `intake`, `explore`, `case-design`, `case-review` into the module. Export `prepare → entry` and `case → case`. Mechanically relocate the frozen legacy `full` Intake prefix into `entry`: preserve the four subgraph calls, three review gates, human interrupt, expressions, actions and loop edges byte-for-byte after projection normalization; replace only the former continuation into Product `generation` with an Intake public `done`/outcome boundary. Remove the old direct `case-review → done` shortcut. Do not normalize decisions, add reject/rework behavior or introduce a business counter yet—those are Task 26's intentional diff.

Replace internal Product aliases with slots for `intake`, `explore`, `case-design`, `case-review`; preserve deterministic Feature capability IDs. Build input schemas from Task 1's exact root-pointer inventory and replace internal reads with `graph_input_pointer`. For every private subgraph caller, add an explicit `input_projection` containing precisely the child-required current-input fields; do this recursively rather than only on the public wrapper. Output projections expose only the reviewed Intake/case outcome and authenticated artifact references.

Declare explicit routing on every multi-out node, initially matching existing behavior; semantic review corrections stay for Task 26.

**Step 4: Verify the Feature in isolation**

Run: `uv run pytest packages/features/assurance-intake/tests -q`
Expected: PASS using fake provider-neutral slot bindings and no external agent server.

**Step 5: Commit**

```bash
git add packages/features/assurance-intake
git commit -m "feat(intake): publish feature workflow module"
```

**Step 6: Verify packaged module bytes from committed HEAD**

Run: `bash scripts/assurance_capability_wheel_smoke_test.sh`
Expected: PASS and the Intake wheel contains its module plus four public I/O schema resources.

### Task 20: Publish the Generation Workflow module and generate export

**Files:**

- Create: `packages/features/assurance-generation/assurance_generation/resources/workflow/module.yaml`
- Create: `packages/features/assurance-generation/assurance_generation/resources/schemas/workflow/{generate-input,generate-output}.v1.schema.json`
- Modify: `packages/features/assurance-generation/assurance_generation/{plugin.py,plugin-declaration.json}`
- Modify: `packages/features/assurance-generation/assurance_generation/contracts/workflow.py`
- Create: `packages/features/assurance-generation/tests/test_workflow_module.py`

**Step 1: Write RED ownership tests**

Assert module/export IDs, the exact 14 base jobs × three slots, no imports, no Product alias/root pointer, and ownership of 19 graphs: `generation`; the API and E2E lane roots plus plan/review/codegen/fix graphs; the Fuzz and Performance roots plus their plan/review/codegen graphs. Drive a selected lane through root → plan → review/codegen and prove each private call explicitly propagates the same family, budget and artifact refs.

**Step 2: Run RED**

Run: `uv run pytest packages/features/assurance-generation/tests/test_workflow_module.py -v`  
Expected: FAIL because the module is absent.

**Step 3: Extract all Generation graphs behind one export**

Export `generate → generation`. Add the closed non-empty family-selection input and normalized per-family aggregate output. Carry all Task 1 root dependencies through the input schema and use `graph_input_pointer` internally. Inventory and add explicit current-input projection on every root → lane and lane → plan/review/codegen/fix private call. Replace the 42 agent aliases with slots; preserve Feature deterministic handlers/validators. Use four fixed lanes and structural skip paths feeding the existing all-join; do not add a dynamic join.

Keep current review/codegen outcomes for the mechanical extraction. Explicit semantic fixes are Task 27.

**Step 4: Verify**

Run: `uv run pytest packages/features/assurance-generation/tests -q`  
Run: `uv run pytest tests/product/test_selected_family_join.py tests/product/test_parallel_generation_isolation.py -q`  
Expected: PASS with no external runtime.

**Step 5: Commit**

```bash
git add packages/features/assurance-generation
git commit -m "feat(generation): publish feature workflow module"
```

### Task 21: Publish Execution execute/rerun Workflow exports

**Files:**

- Create: `packages/features/assurance-execution/assurance_execution/resources/workflow/module.yaml`
- Create: `packages/features/assurance-execution/assurance_execution/resources/schemas/workflow/{execute-input,execute-output,rerun-input,rerun-output}.v1.schema.json`
- Modify: `packages/features/assurance-execution/assurance_execution/{plugin.py,plugin-declaration.json}`
- Modify: `packages/features/assurance-execution/assurance_execution/contracts/workflow.py`
- Create: `packages/features/assurance-execution/tests/test_workflow_module.py`

**Step 1: Write RED module tests**

Assert exports `execute → execution-execute` and `rerun → execution-run`, two base jobs × three slots, empty imports, closed passed/failed public verdict schema, and no invented Product status values in the public contract. Assert neither graph calls the other: each is an independent task triplet/public export. Drive both separately and prove execution policy/artifact refs arrive from each export's own current graph input.

**Step 2: Run RED**

Run: `uv run pytest packages/features/assurance-execution/tests/test_workflow_module.py -v`  
Expected: FAIL because no Execution module exists.

**Step 3: Extract both graphs and boundary schemas**

Move `execution-execute` and `execution-run`; replace six agent aliases with slots; convert each graph's task projections from invocation-root reads to its own explicit export input plus `graph_input_pointer`. Do not add an `execution-execute → execution-run` edge: Product alone calls public `execution.rerun` after Healing in Task 28. The mechanical output adapter may normalize current values, but its closed public status vocabulary is exactly `passed | failed`.

**Step 4: Verify**

Run: `uv run pytest packages/features/assurance-execution/tests tests/product/test_execution_quality_flow.py -q`  
Expected: PASS.

**Step 5: Commit**

```bash
git add packages/features/assurance-execution
git commit -m "feat(execution): publish execute and rerun workflows"
```

### Task 22: Publish Healing failure/coverage repair Workflow exports

**Files:**

- Create: `packages/features/assurance-healing/assurance_healing/resources/workflow/module.yaml`
- Create: `packages/features/assurance-healing/assurance_healing/resources/schemas/workflow/{repair-failure-input,repair-failure-output,repair-coverage-input,repair-coverage-output}.v1.schema.json`
- Modify: `packages/features/assurance-healing/assurance_healing/{plugin.py,plugin-declaration.json}`
- Modify: `packages/features/assurance-healing/assurance_healing/contracts/workflow.py`
- Create: `packages/features/assurance-healing/tests/test_workflow_module.py`

**Step 1: Write RED module and safety-boundary tests**

Assert exports `repair-failure → healing-fix-proposal`, `repair-coverage → healing-coverage-repair`, two base jobs × three slots, no Quality graph import, and output vocabularies `repaired | not_eligible | exhausted | failed | needs_review` where applicable. Drive every nested repair call and prove classification, budget and change refs cross each private boundary only through explicit projection.

**Step 2: Run RED**

Run: `uv run pytest packages/features/assurance-healing/tests/test_workflow_module.py -v`  
Expected: FAIL because the module resource is missing.

**Step 3: Extract graphs without moving classification policy into Healing**

Move the two graphs, replace six Product agent aliases with slots, and expose only normalized repair outcomes plus authenticated change/effect references. Inputs must receive Quality classification/fix eligibility explicitly; every nested private call projects these fields from current graph input, and the module must not import a Quality Workflow or inspect arbitrary root state.

**Step 4: Verify**

Run: `uv run pytest packages/features/assurance-healing/tests tests/product/test_issue_healing_flow.py tests/product/test_coverage_loop.py -q`  
Expected: PASS.

**Step 5: Commit**

```bash
git add packages/features/assurance-healing
git commit -m "feat(healing): publish repair workflow module"
```

### Task 23: Publish the five Quality Workflow exports

**Files:**

- Create: `packages/features/assurance-quality/assurance_quality/resources/workflow/module.yaml`
- Create ten schemas under: `packages/features/assurance-quality/assurance_quality/resources/schemas/workflow/`
- Modify: `packages/features/assurance-quality/assurance_quality/{plugin.py,plugin-declaration.json}`
- Modify: `packages/features/assurance-quality/assurance_quality/contracts/workflow.py`
- Create: `packages/features/assurance-quality/tests/test_workflow_module.py`

**Step 1: Write RED exact-surface tests**

Assert exports and targets: `assess → quality`, `issue-review → issue-review`, `issue-analyze → issue-analyze`, `issue-reconcile → issue-reconcile`, `report → quality-report`. Assert five base jobs × three slots, ownership of the six Quality internal graphs plus the three issue wrapper graphs, no Healing graph import, and report output does not contain an `achieved` decision. Drive `assess → fact-baseline/inspect → issue triage/analysis` and prove evidence/budget refs are explicitly projected at each private boundary.

**Step 2: Run RED**

Run: `uv run pytest packages/features/assurance-quality/tests/test_workflow_module.py -v`  
Expected: FAIL because the Quality module is absent.

**Step 3: Extract graphs and define normalized public evidence**

Move `quality`, `quality-fact-baseline`, `quality-inspect`, `quality-issue-triage`, `quality-issue-analysis`, `quality-report`, `issue-review`, `issue-analyze`, `issue-reconcile`. Replace 15 Product agent aliases with slots. Inputs explicitly carry Execution evidence/artifact references, and every local subgraph caller explicitly projects its child-required subset from current graph input. Outputs expose normalized assessment, failure classification/fix eligibility, coverage state and report references without private tokens.

**Step 4: Verify**

Run: `uv run pytest packages/features/assurance-quality/tests tests/product/test_execution_quality_flow.py tests/product/test_report_flow.py -q`  
Expected: PASS.

**Step 5: Commit**

```bash
git add packages/features/assurance-quality
git commit -m "feat(quality): publish assessment workflow module"
```

### Task 24: Publish the seven Improvement Workflow exports

**Files:**

- Create: `packages/features/assurance-improvement/assurance_improvement/resources/workflow/module.yaml`
- Create fourteen schemas under: `packages/features/assurance-improvement/assurance_improvement/resources/schemas/workflow/`
- Modify: `packages/features/assurance-improvement/assurance_improvement/{plugin.py,plugin-declaration.json}`
- Modify: `packages/features/assurance-improvement/assurance_improvement/contracts/workflow.py`
- Create: `packages/features/assurance-improvement/tests/test_workflow_module.py`

**Step 1: Write RED surface/ownership tests**

Assert exports `archive`, `retro`, `review`, `evaluate`, `export`, `apply`, `rollback` and their exact schema IDs. Assert six base jobs × three slots and ownership of `archive`, `improvement-archive`, `retro`, the retro/three analysis graphs, `improvement-review`, `improvement-evaluate`, `improvement-export`, `improvement-apply`, `improvement-rollback`. Drive archive/retro through at least two private levels and prove lifecycle/evidence refs arrive through explicit current-input projections.

**Step 2: Run RED**

Run: `uv run pytest packages/features/assurance-improvement/tests/test_workflow_module.py -v`  
Expected: FAIL because the module resource does not exist.

**Step 3: Extract all Improvement graphs and contracts**

Replace 18 Product agent aliases with slots, keep deterministic Improvement operations/effects concrete, and replace invocation-root access with declared public inputs. Add explicit caller projections for every nested archive/retro/analysis/review/delivery subgraph. Outputs expose authenticated lifecycle state, evaluation/delivery receipts and terminal outcome, not internal review tokens. Preserve current apply behavior only for mechanical comparison; Task 30 tightens approval semantics.

**Step 4: Verify**

Run: `uv run pytest packages/features/assurance-improvement/tests tests/product/test_archive_retro_improvement.py -q`  
Expected: PASS.

**Step 5: Commit**

```bash
git add packages/features/assurance-improvement
git commit -m "feat(improvement): publish lifecycle workflow module"
```

### Task 25: Publish Product main, switch new Invocations to modular assembly, and prove mechanical equivalence

**Files:**

- Create: `packages/products/assurance-product/assurance_product/resources/workflow/main.yaml`
- Modify: `packages/products/assurance-product/assurance_product/{product.py,models.py,agent_contracts.py}`
- Modify: `packages/products/assurance-product/assurance_product/product-declaration-{opencode,cursor}.json`
- Modify: `tests/product/test_product_composition.py`, `tests/product/test_product_entrypoints.py`, `tests/product/test_full_graph_audit.py`, `tests/product/test_product_providers.py`
- Modify: `tests/product/test_workflow_modularization_golden.py`

**Step 1: Write RED Product-boundary tests**

Assert root module ID/owner/version; exact six `WorkflowModuleRequirement` entries; 19 imports/exports; 99 exact slot bindings; 14 preserved Entrypoints; no task node; no direct `assurance.<feature>.*` capability; and every subgraph call either Product-local or `graph_import` of a public alias.

```python
assert set(resolved.workflow.entrypoints) == {
    "intake", "case", "full", "execute", "archive", "retro",
    "issue-review", "issue-analyze", "issue-reconcile",
    "improvement-review", "improvement-evaluate", "improvement-export",
    "improvement-apply", "improvement-rollback",
}
root = product_manifest.workflow_module
assert set(root.graphs) == {f"product-{name}" for name in resolved.workflow.entrypoints}
assert all(
    node.kind != "task"
    for graph in root.graphs.values()
    for node in graph.nodes.values()
)
assert all(
    f"assurance.product.workflow.graph.product-{name}" in resolved.workflow.graphs
    for name in resolved.workflow.entrypoints
)
```

**Step 2: Run RED**

Run: `uv run pytest tests/product/test_product_composition.py tests/product/test_product_entrypoints.py -v`  
Expected: FAIL because Product still embeds the monolithic Workflow.

**Step 3: Build Product main and modular declarations**

Create a Product root module with `name: assurance`, Product-owned `product-full` and `product-execute` cross-Feature graphs, plus `product-<entrypoint>` thin wrappers for the other 12 Entrypoints. In `product-full`, replace the entire Task 1 legacy Intake prefix (including review gates/interrupt) with one `intake.prepare` import call; Task 19 now owns that closure. Continue from its validated public terminal output into `generation.generate`. Do not duplicate an Intake decision or reference a private Intake node in Product.

Use only the frozen import aliases. All cross-Feature decisions consume validated public outputs. ProductManifest selects six exact module resource IDs and 99 exact slot bindings. Regenerate both declaration JSON files from the same canonical builder; do not hand-edit one variant.

**Step 4: Prove the two migration stages separately**

Prove three disjoint migration facts rather than requiring impossible per-graph identity:

1. For 49 of the 50 Feature-owned legacy graphs (all except Intake `entry`), compare a normalized mechanical projection that (a) reverses only Task 16's exact `<module-id>.graph|retry|timeout.<local-id>` qualification and (b) removes only declared boundary fields; assert node kinds, task dispatch IDs after slot lowering, edges, retry/timeout definitions and terminal outputs remain equal.
2. For Intake `entry`, compare against the Task 1 canonical extraction of the legacy `full` Intake prefix. Permit only the recorded Product-continuation → Feature-terminal remap; all relocated subgraphs, gates, interrupt actions, expressions and loop edges must match. The normalizer rejects unknown qualified prefixes instead of fuzzy suffix matching.
3. For the two legacy Product roots `full` and `execute`, compare public-closure behavior rather than internal topology: with deterministic fake handlers, the pre-modular and modular roots must produce the same ordered task dispatches, interrupts, external effects and terminal output on every existing characterization scenario. Product internal node/edge equality is intentionally not asserted because private calls became public Feature calls.

Separately assert the 12 new Product wrappers contain one public subgraph call and one end. Drive all 14 Entrypoints, but do not claim impossible byte-for-byte behavior equality for standalone `intake`: the frozen two-export surface makes `intake.prepare` own the review loop that previously existed only inside `full`. At this checkpoint:

- 13 Entrypoints other than `intake` must preserve all characterized public behavior;
- `intake` pass-path task/effect/terminal behavior must remain equal;
- its non-pass branch may differ **only** by following the exact legacy-`full` review prefix relocated in Task 19—same gates, expressions and interrupt actions, with no Task 26 normalization/budget/reject/rework changes yet.

Record this unavoidable ownership move in a separate `ARCHITECTURAL_RELOCATION_DIFF` fixture. Keep the Task 26–30 `INTENTIONAL_SEMANTIC_DIFF` set empty at this checkpoint so relocation and later business corrections cannot be conflated.

Run: `uv run pytest tests/product/test_workflow_modularization_golden.py tests/product/test_product_composition.py tests/product/test_product_entrypoints.py tests/product/test_full_graph_audit.py tests/product/test_product_providers.py -q`  
Expected: PASS; 49 exact Feature graph comparisons, one exact relocation comparison, two Product closure comparisons, 12 thin-wrapper shape comparisons and the narrowly frozen standalone-Intake relocation exception all pass; new Product manifests use only the modular form.

**Step 5: Commit**

```bash
git add packages/products/assurance-product/assurance_product tests/product
git commit -m "feat(product): assemble public feature workflows"
```

---

## Phase E — Apply the intentional routing corrections (3–4 PD)

The pre-modular golden remains immutable. From this phase onward, `test_workflow_modularization_golden.py` must identify the exact changed owned graphs and continue asserting every unaffected graph's normalized projection is equal. Each Task updates only its own expected changed-graph set.

### Task 26: Make Intake review exhaustive, bounded and reject-safe

**Files:**

- Modify: `packages/features/assurance-intake/assurance_intake/contracts/review.py`
- Create: `packages/features/assurance-intake/assurance_intake/operations/workflow_state.py`
- Modify: `packages/features/assurance-intake/assurance_intake/{plugin.py,plugin-declaration.json}`
- Modify: `packages/features/assurance-intake/assurance_intake/resources/result-contracts/case-review.v1.schema.json`
- Modify: `packages/features/assurance-intake/assurance_intake/resources/workflow/module.yaml`
- Modify: `packages/features/assurance-intake/assurance_intake/resources/schemas/workflow/{prepare-output,case-output}.v1.schema.json`
- Modify: `packages/features/assurance-intake/tests/test_contracts.py`
- Create: `packages/features/assurance-intake/tests/test_workflow_state.py`
- Modify: `packages/features/assurance-intake/tests/test_workflow_module.py`
- Modify: `tests/product/test_graph_intake_and_triplets.py`, `tests/product/test_stop_and_interrupts.py`, `tests/product/test_workflow_modularization_golden.py`

**Step 1: Write RED outcome-matrix tests**

Parameterize raw compatibility decisions into public outcomes: `pass/approved → pass`, auto-fixable `needs_fix/changes_requested → needs_fix`, human-required variants → `needs_human`, explicit reject → `reject`. Contradictory combinations fail validation. Drive pass, automatic fix, human approve, human reject, request-rework and budget exhaustion through fresh Invocations.

```python
@pytest.mark.parametrize(
    ("action", "expected"),
    [("approve", "passed"), ("reject", "rejected"), ("request_rework", "rework")],
)
def test_case_review_action_has_one_successor(action: str, expected: str) -> None:
    result = drive_case_review_interrupt(action)
    assert result.public_outcome == expected
    assert result.successor_count == 1
```

Add operation tests for `assurance.intake.review-round.advance`: `0/2 → 1/2`, `1/2 → 2/2`, and attempts at `2/2`, negative or inconsistent counters fail without output. Drive both automatic-fix and request-rework paths and assert exactly one capability execution per loop, with monotonic counters that never exceed the input budget.

**Step 2: Run RED**

Run: `uv run pytest packages/features/assurance-intake/tests/test_workflow_module.py tests/product/test_graph_intake_and_triplets.py tests/product/test_stop_and_interrupts.py -k 'review or reject or rework or budget' -v`  
Expected: FAIL on reject/rework/budget distinctions.

**Step 3: Normalize the contract and replace accidental parallel gates**

Add a closed public review outcome model with consistent decision, `auto_fix_allowed`, `human_review_required`, repair payload and budget counters. Implement a frozen, deterministic `ReviewRoundAdvanceHandler` with concrete Feature capability ID `assurance.intake.review-round.advance`; register it in `plugin.py` and regenerate `plugin-declaration.json`. Its closed input carries `rounds_used`/`rounds_budget`, it accepts only `0 <= used < budget`, and its only output is the validated counter pair with `rounds_used = used + 1`.

Route one exclusive decision: pass completes; needs_fix with remaining budget passes through a concrete `task` node bound to that capability, then loops to case design using the task output counter; needs_human interrupts; reject terminates rejected. Resume output uses an exclusive action route; approve completes, reject terminates, request_rework uses the same advance task before looping. Exhausted paths bypass the task and terminate explicit non-achieved. Routing/projection never claims to mutate a counter; `max_activations` remains only a backstop.

**Step 4: Verify no stuck state**

Run: `uv run pytest packages/features/assurance-intake/tests tests/product/test_graph_intake_and_triplets.py tests/product/test_stop_and_interrupts.py tests/product/test_workflow_modularization_golden.py -q`  
Expected: PASS; only Intake graphs appear in the intentional semantic diff, and every looping trace contains one authenticated `assurance.intake.review-round.advance` result per consumed round.

**Step 5: Commit**

```bash
git add packages/features/assurance-intake tests/product/test_graph_intake_and_triplets.py tests/product/test_stop_and_interrupts.py tests/product/test_workflow_modularization_golden.py
git commit -m "fix(intake): make case review routing exhaustive"
```

### Task 27: Make Generation selection/review/fix loops explicit and complete

**Files:**

- Modify: `packages/features/assurance-generation/assurance_generation/contracts/{reviews,codegen,workflow}.py`
- Create: `packages/features/assurance-generation/assurance_generation/operations/workflow_state.py`
- Modify: `packages/features/assurance-generation/assurance_generation/{plugin.py,plugin-declaration.json}`
- Modify: corresponding Generation result-contract/schema resources
- Modify: `packages/features/assurance-generation/assurance_generation/resources/workflow/module.yaml`
- Modify: `packages/features/assurance-generation/tests/test_{contracts,plan_review,codegen,workflow_module}.py`
- Create: `packages/features/assurance-generation/tests/test_workflow_state.py`
- Modify: `tests/product/test_generation_branches.py`, `tests/product/test_selected_family_join.py`, `tests/product/test_parallel_generation_isolation.py`, `tests/product/test_workflow_modularization_golden.py`

**Step 1: Write RED selection and loop matrices**

Generate all 15 non-empty subsets of `(api,e2e,fuzz,performance)` and assert each selected family dispatches once, each unselected lane emits one structural skip token and no task, and the all-join receives four lane results independent of completion order. Empty, duplicate and unknown values fail at Feature input before work.

For each family, cover plan review pass, auto-fix loop, human approve/reject/rework and review-budget exhaustion when supported. Add an API/E2E codegen result with reachable fix verdict and repair payload.

Add table tests for `assurance.generation.review-round.advance` over all four families and stages `plan | codegen`: one call increments exactly once, preserves family/stage/budget, rejects `used >= budget`, and two lane completions in different scheduler order produce the same per-lane counters.

**Step 2: Run RED**

Run: `uv run pytest packages/features/assurance-generation/tests tests/product/test_generation_branches.py tests/product/test_selected_family_join.py tests/product/test_parallel_generation_isolation.py -k 'family or review or fix or subset' -v`  
Expected: FAIL on empty/duplicate input, unreachable codegen fix, and incomplete review action routes.

**Step 3: Implement explicit fanout and bounded exclusive reviews**

- Validate the canonical non-empty duplicate-free family tuple at the export boundary.
- From the selection node, use four unconditional edges under `routing: {mode: fanout, min_matches: 4}` so all four fixed lane selectors activate. Each selector then uses `exclusive` routing: one conditioned `selected` edge and one `otherwise` skip edge. Thus every lane contributes exactly one token to the four-way `join: all`; the aggregate filters structural skip values and returns only selected family results.
- Implement and register deterministic `GenerationReviewRoundAdvanceHandler` under concrete Feature capability ID `assurance.generation.review-round.advance`. Its closed input/output contains `family`, `stage`, `rounds_used`, `rounds_budget`; it accepts only a known family/stage with `0 <= used < budget` and returns exactly `used + 1`.
- Normalize plan review into pass/needs_fix/needs_human/reject and use exclusive + otherwise routes. Every plan/codegen re-entry passes through a concrete task node using the advance capability, and downstream projections read its output counter. Exhausted branches terminate before dispatch; routing/projection never mutates budget state.
- Add a versioned internal `CodegenResultV2` with `verdict: accepted | needs_fix`; needs_fix requires repair payload. API/E2E enter their existing bounded fixer graphs. Fuzz/Performance contracts do not advertise a fixer outcome because the frozen slot catalog has no corresponding fixer jobs.
- Resume actions approve/reject/request_rework each have one explicit successor.

**Step 4: Verify all subsets and deterministic join output**

Run: `uv run pytest packages/features/assurance-generation/tests tests/product/test_generation_branches.py tests/product/test_selected_family_join.py tests/product/test_parallel_generation_isolation.py tests/product/test_workflow_modularization_golden.py -q`  
Expected: PASS for 15 subsets plus all invalid inputs and both lane completion orders; the initial fanout always emits four selector tokens and every consumed review round has exactly one authenticated advance result.

**Step 5: Commit**

```bash
git add packages/features/assurance-generation tests/product/test_generation_branches.py tests/product/test_selected_family_join.py tests/product/test_parallel_generation_isolation.py tests/product/test_workflow_modularization_golden.py
git commit -m "fix(generation): make family and review routes explicit"
```

### Task 28: Reclassify failed execution and recheck every healed rerun

**Files:**

- Modify: `packages/features/assurance-execution/assurance_execution/contracts/{execution,workflow}.py`
- Modify: `packages/features/assurance-quality/assurance_quality/contracts/{issues,workflow}.py`
- Modify: `packages/features/assurance-healing/assurance_healing/contracts/{proposal,status,workflow}.py`
- Create: `packages/features/assurance-healing/assurance_healing/operations/workflow_state.py`
- Modify: `packages/features/assurance-healing/assurance_healing/{plugin.py,plugin-declaration.json}`
- Modify: the three Feature module YAML files and affected public schemas
- Modify: `packages/products/assurance-product/assurance_product/resources/workflow/main.yaml`
- Modify: `tests/product/test_execution_quality_flow.py`, `tests/product/test_issue_healing_flow.py`, `tests/product/test_achieved_terminal.py`, `tests/product/test_workflow_modularization_golden.py`
- Create: `packages/features/assurance-healing/tests/test_workflow_state.py`

**Step 1: Write RED end-to-end route cases**

Cover: passed; fix-eligible test failure; fix-eligible test-data failure; product bug; environment/infrastructure classification; unknown/pending/failed analysis; healed rerun passed; healed rerun still failed; Healing budget exhausted. Assert product/environment/infrastructure failures never dispatch `healing.repair-failure`.

```python
@pytest.mark.parametrize("classification", ["product_bug", "environment_failure", "infrastructure_failure"])
def test_non_test_failure_never_dispatches_test_fix(classification: str) -> None:
    trace = drive_failed_execution(classification=classification, fix_eligible=False)
    assert "healing.repair-failure" not in trace.public_exports
    assert trace.terminal != "achieved"
```

Add operation tests for `assurance.healing.repair-round.advance` with `kind: failure | coverage`: it increments exactly once while `used < budget`, preserves kind/budget, rejects exhausted/negative counters, and produces no effect intent itself. In the failure E2E trace, assert each actual repair is preceded by exactly one advance task and a failed rerun does not reset the counter.

**Step 2: Run RED**

Run: `uv run pytest tests/product/test_execution_quality_flow.py tests/product/test_issue_healing_flow.py tests/product/test_achieved_terminal.py -v`  
Expected: FAIL because current Product compares Execution status with impossible values and rerun can bypass classification.

**Step 3: Implement the normalized verdict/classification loop**

Execution public output is exactly passed/failed. Host/scheduler failure remains a typed task failure handled by retry and explicit non-achieved terminal after exhaustion; it is not forged into an Execution status. A failed verdict always calls Quality issue analysis. Quality classification is a closed value with a consistency rule for `fix_eligible`; only test/test-data + eligible + remaining budget enters Healing. After repair, `execution.rerun` returns to the same verdict node; a failed rerun is classified again and may heal only while budget remains.

Implement and register deterministic `HealingRepairRoundAdvanceHandler` at concrete Feature capability ID `assurance.healing.repair-round.advance`. Its closed input/output is `{kind, rounds_used, rounds_budget}` and only accepts `kind in {failure, coverage}` with `0 <= used < budget`, returning `used + 1`; it performs no repair/effect itself. Inside both Healing exports, the eligible route first executes this concrete task and only its authenticated output can feed the repair task. Product receives the updated counter in the public Healing output and passes it into rerun/reclassification.

Use exclusive routes with otherwise fail-closed for unknown/pending analysis. Exhausted paths terminate before the advance/repair nodes. Every loop therefore has one real atomic counter transition; routing/projection never mutates counters and `max_activations` remains a backstop.

**Step 4: Verify no unconditional `run → quality` edge remains**

Run: `uv run pytest packages/features/assurance-execution/tests packages/features/assurance-quality/tests/test_issues.py packages/features/assurance-healing/tests tests/product/test_execution_quality_flow.py tests/product/test_issue_healing_flow.py tests/product/test_achieved_terminal.py tests/product/test_workflow_modularization_golden.py -q`  
Expected: PASS; only passed initial/rerun verdict reaches normal Quality assessment, and each failure repair consumes exactly one authenticated Healing round.

**Step 5: Commit**

```bash
git add packages/features/assurance-execution packages/features/assurance-quality packages/features/assurance-healing packages/products/assurance-product/assurance_product/resources/workflow/main.yaml tests/product
git commit -m "fix(product): reclassify every failed execution rerun"
```

### Task 29: Distinguish coverage success, repair, exhaustion, human and inconclusive outcomes

**Files:**

- Modify: `packages/features/assurance-quality/assurance_quality/contracts/{coverage,workflow}.py`
- Modify: `packages/features/assurance-quality/assurance_quality/resources/workflow/module.yaml` and assess output schema
- Modify: `packages/features/assurance-healing/assurance_healing/contracts/{coverage_repair,workflow}.py`
- Modify: `packages/features/assurance-healing/assurance_healing/resources/workflow/module.yaml` and repair-coverage output schema
- Modify: `packages/products/assurance-product/assurance_product/resources/workflow/main.yaml`
- Modify: `packages/features/assurance-quality/tests/test_coverage.py`
- Modify: `tests/product/test_coverage_loop.py`, `tests/product/test_report_flow.py`, `tests/product/test_achieved_terminal.py`, `tests/product/test_workflow_modularization_golden.py`

**Step 1: Write RED coverage state-table tests**

Cover measured above and equal threshold; below threshold with budget; zero budget; exhausted budget; needs human; inconclusive. Cover every repair result `repaired`, `not_eligible`, `exhausted`, `failed`, `needs_review`. Assert rounds are monotonic, increment once per repair and never exceed the declared budget. Assert a coverage repair dispatch is always preceded by exactly one Task 28 `assurance.healing.repair-round.advance(kind="coverage")`; exhausted/non-repair outcomes execute neither node.

**Step 2: Run RED**

Run: `uv run pytest packages/features/assurance-quality/tests/test_coverage.py tests/product/test_coverage_loop.py tests/product/test_report_flow.py tests/product/test_achieved_terminal.py -v`  
Expected: FAIL because current threshold/budget routing can collapse exhaustion into success.

**Step 3: Implement the closed coverage outcome**

Quality emits exactly `satisfied | repair_required | exhausted | needs_human | inconclusive`; equality is satisfied. Product calls coverage repair only for repair_required with budget. The Healing `repair-coverage` export uses Task 28's concrete advance task before its repair task and returns the incremented counter; no Product route or projection fabricates the increment. Healing emits its five closed repair outcomes. Repaired loops back to fresh Quality evidence with the returned counter; not-eligible/exhausted/failed are explicit non-achieved outcomes (a report may still be produced); needs-review interrupts. Only a fresh Quality output marked satisfied can cross the achieved gate.

**Step 4: Verify reporting is not success**

Run: `uv run pytest packages/features/assurance-quality/tests/test_coverage.py packages/features/assurance-healing/tests tests/product/test_coverage_loop.py tests/product/test_report_flow.py tests/product/test_achieved_terminal.py tests/product/test_workflow_modularization_golden.py -q`  
Expected: PASS; exhausted/inconclusive traces can contain a report but cannot reach achieved, and every actual coverage repair consumes exactly one authenticated Healing round.

**Step 5: Commit**

```bash
git add packages/features/assurance-quality packages/features/assurance-healing packages/products/assurance-product/assurance_product/resources/workflow/main.yaml tests/product
git commit -m "fix(quality): separate coverage exhaustion from success"
```

### Task 30: Require authenticated approval and successful evaluation before Improvement apply

**Files:**

- Modify: `packages/features/assurance-improvement/assurance_improvement/contracts/{review,delivery,workflow}.py`
- Modify: `packages/features/assurance-improvement/assurance_improvement/operations/{review,delivery}.py`
- Modify: `packages/features/assurance-improvement/assurance_improvement/resources/workflow/module.yaml` and affected public schemas
- Modify: `packages/products/assurance-product/assurance_product/resources/workflow/main.yaml`
- Modify: `packages/features/assurance-improvement/tests/test_{review,delivery,workflow_module}.py`
- Modify: `tests/product/test_archive_retro_improvement.py`, `tests/product/test_stop_and_interrupts.py`, `tests/product/test_report_flow.py`, `tests/product/test_achieved_terminal.py`, `tests/product/test_workflow_modularization_golden.py`

**Step 1: Write RED lifecycle/action tests**

Cover auto-review pass, changes requested, needs human, reject advice; human approve/reject/request_rework/supersede; evaluation passed/failed/missing/stale; forged interrupt approval payload; and direct apply attempt from every non-approved state. Assert reject/rework/supersede produce no apply effect or write authorization.

```python
@pytest.mark.parametrize("state", ["proposed", "changes_requested", "rejected", "superseded"])
def test_apply_requires_authenticated_approved_state(state: str) -> None:
    result = attempt_apply(state=state, evaluation="passed")
    assert result.applied is False
    assert result.effect_intents == ()
```

**Step 2: Run RED**

Run: `uv run pytest packages/features/assurance-improvement/tests/test_review.py packages/features/assurance-improvement/tests/test_delivery.py tests/product/test_archive_retro_improvement.py tests/product/test_stop_and_interrupts.py -v`  
Expected: FAIL where advice or interrupt payload can bypass an authenticated approved projection.

**Step 3: Make lifecycle transitions authoritative**

Auto-review pass first materializes an authenticated approved lifecycle projection. Other auto outcomes cannot call apply. Human approve produces the same approved projection; reject, request_rework and supersede persist distinct non-apply states. Apply input requires approved state digest/version plus a successful, current evaluation receipt; validators reject forged/stale/missing proof before effect intent or workspace write. Use exclusive action/evaluation routes with otherwise fail-closed.

Finally, make Product achieved depend on successful report + fresh Quality satisfied + no failed/exhausted state; Improvement artifacts alone never turn a failed QA run into success.

**Step 4: Verify effects, terminal state and replay**

Run: `uv run pytest packages/features/assurance-improvement/tests tests/product/test_archive_retro_improvement.py tests/product/test_stop_and_interrupts.py tests/product/test_report_flow.py tests/product/test_achieved_terminal.py tests/product/test_replay_properties.py tests/product/test_workflow_modularization_golden.py -q`  
Expected: PASS; only authenticated approved+evaluated paths apply.

**Step 5: Commit**

```bash
git add packages/features/assurance-improvement packages/products/assurance-product/assurance_product/resources/workflow/main.yaml tests/product
git commit -m "fix(improvement): gate apply on approved evaluated state"
```

---

## Phase F — Security closure, rollout and release verification (3–5 PD)

### Task 31: Remove the active monolith, prove non-plugin security, bump release versions and run the full gate

**Files:**

- Move: `packages/products/assurance-product/assurance_product/resources/workflow/assurance-full.yaml` → `tests/product/fixtures/assurance-full-pre-modular.yaml`
- Modify: `tests/product/test_workflow_modularization_golden.py`
- Modify/Create: `tests/product/test_workflow_module_security.py`
- Modify: `tests/product/test_project_configuration_security.py`, `tests/product/test_composition_authority.py`, `tests/product/test_cli_fail_closed.py`, `tests/product/test_replay_properties.py`
- Create: `docs/runbooks/assurance-modular-workflow-rollout.md`
- Modify affected package `pyproject.toml`, provider source/version constants, plugin/product declarations, module versions, root `pyproject.toml`, `uv.lock`, smoke scripts and CI path checks

**Step 1: Write RED negative-source and rollout tests**

Create temporary SUT-local `plugins/`, fake Python package, module YAML, URL/path/glob import strings and ambient entry point. Assert none is scanned/imported and assembly bytes are unchanged. Add `.aa` documents trying to select module/distribution/entrypoint/path/export/capability/schema implementation; each must be rejected as unknown configuration. Add a modular-runner/legacy-lock test that snapshots ledger bytes, attempts resume, and asserts byte-exact unchanged ledger after fail-closed mismatch. Add a final public-outcome audit that drives every declared boundary enum/action and proves it reaches a next transition, interrupt or terminal result rather than a running Invocation with no planned transition.

**Step 2: Run RED**

Run: `uv run pytest tests/product/test_workflow_module_security.py tests/product/test_project_configuration_security.py tests/product/test_cli_fail_closed.py -v`  
Expected: FAIL until the active monolith path and all negative cases/runbook assertions are handled.

**Step 3: Finalize packaging and immutable release identities**

- Move the legacy YAML to the test fixture and keep it only for parser/golden compatibility; Product source/package data must include only `resources/workflow/main.yaml` as active Workflow source.
- Bump affected release identities together: `graph-engine`, `agent-runtime-contracts`, six Features and `assurance-product` from `0.1.0` to `0.2.0`. Bump the generated `assurance.product.agent` deployment plugin from `1.0.0` to `1.1.0` because its authenticated binding contribution now contains `contract_id`; keep the configuration plugin and concrete Client versions unchanged unless their wheel content/API changed during implementation.
- Update every ProviderSource/PluginDescriptor/ProductManifest requirement/module version and regenerate all plugin/product declarations from canonical builders. Never replace old wheel bytes under `0.1.0`.
- Regenerate `uv.lock` and ensure wheel package-data includes every module and I/O schema.
- Document drain-and-pin exactly: stop new legacy Invocations, retain immutable old environment/config, finish/export/archive there, start new Invocations on modular release, and direct composition mismatch back to the pinned old runner. Do not promise automatic historical wheel selection.

**Step 4: Run focused security and compatibility gates**

Run:

```bash
uv run pytest packages/framework/graph-engine/tests/composition packages/framework/graph-engine/tests/runtime -q
uv run pytest tests/product/test_workflow_module_security.py tests/product/test_project_configuration_security.py tests/product/test_composition_authority.py tests/product/test_cli_fail_closed.py tests/product/test_replay_properties.py -q
```

Expected: PASS; an undeclared installed module and all SUT-local fake sources have no effect.

**Step 5: Run the complete repository quality gate**

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest -v
```

Expected: every command exits 0. Do not claim completion from a partial suite.

**Step 6: Commit the release closure**

```bash
git add packages/framework packages/clients/agent-runtime-contracts packages/features packages/products tests pyproject.toml uv.lock scripts .github/workflows/ci.yml
git add -f docs/runbooks/assurance-modular-workflow-rollout.md
git commit -m "release: complete modular assurance workflow architecture"
```

**Step 7: Verify all committed wheels in clean environments**

```bash
bash scripts/graph_engine_smoke_test.sh
bash scripts/assurance_capability_wheel_smoke_test.sh
bash scripts/assurance_product_wheel_smoke_test.sh
```

Expected: all three commands exit 0 from committed `HEAD`. If one fails, make a narrowly scoped follow-up commit and repeat Steps 5 and 7 before reporting completion.

## Completion Evidence

Before handing the branch back, record these exact facts in the final implementation summary:

- physical tree has one Framework, three Clients, six Features and one Product at the target paths;
- six authenticated Feature module resources expose exactly 19 public exports;
- Product root contains 14 Entrypoints, zero task nodes and only public Feature imports;
- 33 base agent contracts lower through exactly 99 contract-authenticated concrete aliases;
- Assembler output contains no module/import/export/slot metadata and compiles through the existing compiler;
- contracted subgraph input/output validation and explicit routing replay from the existing event types;
- the legacy pre-modular golden is preserved; `ARCHITECTURAL_RELOCATION_DIFF` names only the standalone-Intake ownership move, while `INTENTIONAL_SEMANTIC_DIFF` names only Tasks 26–30 graphs;
- legacy composition mismatch leaves ledger bytes unchanged and same-composition resume passes;
- ambient entry points, SUT files and `.aa` source-extension attempts cannot affect assembly;
- all five code-quality commands and all three committed-wheel smoke commands in Task 31 pass.
