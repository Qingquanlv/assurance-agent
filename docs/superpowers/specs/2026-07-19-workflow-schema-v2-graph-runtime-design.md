# Workflow Schema v2 与 GraphRuntime 设计

日期：2026-07-19
状态：待用户评审
方案：声明式 `schema_version: "2"` + AA 自研最小 GraphRuntime；参考 LangGraph 执行模型，不引入 LangGraph 依赖

## 1. 背景

当前 workflow 由静态 `phases:` DAG、`loops:` kind 特判和 driver 主循环共同执行。执行路径实际分成四类：

1. skill phase 由 agent adapter 执行；
2. CLI phase 由 `CliPhaseExecutor` 执行；
3. orchestrator phase 在 driver 内直接完成；
4. healing control action 由 `HealingActionExecutor` 执行。

普通 phase 由 `status.next_dispatch` 驱动，healing 则由 `status.healing_episode.next_actions` 驱动。`CliPhaseExecutor` 同时负责 CLI 调用和所有普通 phase 的 outcome commit，因此其名字和职责都跨越了两个不同的 seam。新增 loop kind、动态 fan-out、持久 retry 或并行执行时，driver 必须继续识别更多 action 类型，无法形成通用图运行时。

当前 benchmark 使用仓库打包的 [`workflow-schema.yaml`](../../../assurance_agent/_resources/schemas/workflow-schema.yaml)，并通过 benchmark runner 注入 `run_mode`、`test_types`、`max_healing_attempts` 等参数。真实运行已经暴露出以下恢复需求：

- API、E2E、Fuzz、Performance 等独立分支需要并行执行；
- review→fix→review 和 healing 都需要有界业务循环；
- driver 或 agent 被 hard timeout 杀死后必须恢复；
- 已成功的并行 sibling 不能在 resume 时重复执行；
- 当前只有 `dispatch_signed`、没有失败/abandoned 生命周期，benchmark 需要手工删除 orphan dispatch；
- eval fixture 从中间阶段启动，但预置 artifacts 不一定带完整 task ledger；
- human review 必须绑定 audited artifact，并从原 checkpoint 恢复。

## 2. 已确认的决策

| 决策 | 结论 |
|---|---|
| 兼容性 | 升级为不兼容的 `schema_version: "2"`；不透明兼容 v1 |
| Runtime | 参考 LangGraph 的 graph/task/super-step/subgraph/interrupt 模型，但不引入 `langgraph`、LangChain 或 LangSmith 依赖 |
| Graph 布局 | 顶层 `graphs:`；父图通过 `uses: graph:<id>` 调用 named subgraph |
| 并行 | 真正并行执行同一 super-step 内资源不冲突的 ready/fan-out tasks |
| sibling 失败 | 不启动新节点；允许在途 sibling 完成；成功结果持久化为 pending writes |
| resume | 只重跑失败、abandoned 或尚未开始的 task；不重复已成功 sibling |
| 业务预算 | 由指定业务节点成功提交时消耗，不按 graph 总 step 数消耗 |
| 预算权威 | 只以严格 ledger 的 `budget_consumed` 事件为权威；checkpoint 中的计数只是投影 |
| retry 条件 | schema 声明稳定 error kinds；handler 把底层异常归一化，不在 YAML 中引用 Python exception 类型 |
| human review | 建模为可恢复 graph interrupt，不是不可恢复终局 |
| 资源声明 | node 不强制重复声明资源；execution contract registry 提供默认资源，node 可选补充或覆盖 |
| 资源冲突 | ready tasks 自动拆分为确定性串行 wave，不因冲突报错 |
| 未知资源 | 无法推导资源范围的 task 获取保守的全局 exclusive lock |
| Fixture | 通过显式 `import-checkpoint` 校验并导入，不因文件存在而静默视为完成 |
| Attempt 生命周期 | started/succeeded/failed/abandoned 严格入 ledger；高频 heartbeat 写可重建 lease 文件 |

## 3. 目标与非目标

### 3.1 目标

1. schema 成为 graph 拓扑、条件路由、subgraph、fan-out、retry、timeout、interrupt 和业务预算的唯一声明来源。
2. driver 不再识别 skill/CLI/healing 等业务类型，只调用一个 GraphRuntime interface。
3. 同一 super-step 的独立 task 真正并行；资源冲突时确定性串行。
4. 每个物理 attempt、业务预算和 human decision 都可审计、可恢复、不可通过编辑 `driver.json` 重置。
5. hard kill 后不需要删除历史事件；成功 sibling 和动态 fan-out expansion 可以准确重放。
6. 完整覆盖当前 canonical workflow、benchmark 和 eval fixture 的运行模式。

### 3.2 非目标

1. 不引入 LangGraph runtime、checkpointer、Runnable 或 Channel 类型体系。
2. v2 首版不提供透明 v1→v2 in-flight graph migration；旧 fixture 只能显式导入。
3. 不承诺 exactly-once 外部副作用。外部 agent/CLI 采用 at-least-once 执行，progression commit 提供幂等 reconcile。
4. 不在 v2 首版提供 graph macro/template 语言；三个 review-fix graph 可以有少量声明重复。
5. 不因并行需求引入分布式 scheduler；首版在单机进程内并行，ledger 和 lease 支持进程恢复。

## 4. 核心不变量

### 4.1 单一权威

- `events.jsonl` 的严格 graph/task/budget/decision 事件是执行历史的唯一权威。
- `workflow-state.yaml`、checkpoint snapshot、`driver.json` 和 `running-tasks.json` 都是可从严格 ledger 与 artifacts 重建的投影或 liveness 数据。
- graph params、schema digest 和 execution contract digests 在 graph invocation 开始时冻结；普通 resume 不得静默替换。

### 4.2 Plan → Execute → Update

GraphRuntime 按离散 super-step 执行：

1. **Plan**：从 checkpoint 投影 ready nodes、冻结条件分支、展开 fan-out、分配 task IDs，并按资源冲突划分当前并行 wave；
2. **Execute**：基于同一 committed workspace snapshot，在 task 私有写层中并行执行 wave；每个 task 独立 retry，成功结果立即固化为内容寻址 write-set；
3. **Update**：当本 super-step 所需 tasks 都成功、skipped 或 interrupt 后，确定性合并 write-sets，提交 canonical workspace、graph state 和下一个 checkpoint。

同一 super-step 中，一个 task 的 graph-state update 和文件写入对 sibling 都不可见；存在数据依赖时必须用 edge 进入下一 super-step。资源冲突的 ready task 不进入当前 wave，在当前 wave 成功 Update 后重新 Plan，因此后一个 task 能看到前一个 wave 的提交结果。

### 4.3 两类次数不可混淆

- **Task retry budget**：处理 timeout、transport、rate limit 等技术失败；按物理 attempt 计数。
- **Business loop budget**：处理 case fix、plan fix、healing 等语义循环；只在声明的业务节点成功提交时计数。
- **`max_supersteps`**：最终结构安全阀；不替代业务预算。

一次 fixer 因 transport error 重试三次，仍只消耗一次 `fix_attempts`；一次 attempt 被 hard kill 并在 lease 到期后标为 abandoned，则消耗一次 retry attempt，但不消耗业务预算。

## 5. GraphRuntime 深模块

### 5.1 对外 interface

```python
class GraphRuntime:
    def run(
        self,
        schema: CompiledWorkflow,
        entrypoint: str,
        context: RuntimeContext,
    ) -> RunResult: ...

    def resume(
        self,
        invocation_id: str,
        command: ResumeCommand | None = None,
    ) -> RunResult: ...

    def status(self, invocation_id: str) -> GraphStatus: ...

    def import_checkpoint(
        self,
        schema: CompiledWorkflow,
        entrypoint: str,
        manifest: ImportManifest,
        context: RuntimeContext,
    ) -> ImportResult: ...
```

CLI 和测试只跨这个 interface。scheduler、handler registry、retry、resource claims、pending writes、checkpoint projection 和 ledger recovery 都是 GraphRuntime 的内部实现。

### 5.2 内部 task interface

```python
@dataclass(frozen=True)
class ExecutableTask:
    task_id: str
    invocation_id: str
    checkpoint_ns: str
    graph_id: str
    node_id: str
    input: object
    input_sha256: str
    retry_policy: RetryPolicy
    timeout_policy: TimeoutPolicy
    target: NodeTarget
    resources: ResourceClaims


class NodeRunner(Protocol):
    def execute(self, task: ExecutableTask, context: RuntimeContext) -> TaskResult: ...
```

`NodeRunner` 内部按 target namespace 选择 handler：

| Target | Handler |
|---|---|
| `skill:<name>` | agent adapter handler |
| `operation:<name>` | AA domain operation/CLI handler |
| `graph:<name>` | nested GraphRuntime handler |
| `builtin:join` | graph join handler |
| `builtin:gate` | frozen gate evaluation handler |
| `builtin:interrupt` | interrupt projection handler |

handler registry 同时提供：

- error kind 归一化；
- 默认 resource claims；
- write policy；
- result serializer；
- idempotency/reconcile 策略；
- 是否支持重新连接仍在运行的 remote task。

GraphRuntime 还向 handler 注入 `TaskWorkspace`，将 `change:`、`project:`、`repo:` 映射到同一 committed snapshot 上的 task 私有写层。handler 不持有 canonical workspace 的可写路径。具体后端可以是 Git worktree、copy-on-write clone 或 overlay，但 `TaskWorkspace.diff()` 和 `freeze_write_set()` 的语义固定。

因此 v2 删除 `CliPhaseExecutor` 和 `HealingActionExecutor` 这两个外部 seam。CLI、healing allocation 和 healing completion 只是不同的内部 handler。

## 6. Workflow Schema v2

### 6.1 顶层结构

```yaml
schema_version: "2"
name: aa-full

params: {}
entrypoints: {}
policies:
  retry: {}
  timeout: {}
  scheduler: {}
graphs: {}
gates: {}
```

顶层不再包含 `phases:` 和 `loops:`。所有可执行行为都是 graph node；所有 loop 都是 graph 中的有界 cycle。

### 6.2 参数、entrypoint 和 policy

```yaml
schema_version: "2"
name: aa-full

params:
  run_mode:
    type: enum
    values:
      - full
      - case-only
      - api-only
      - e2e-only
      - plan-only
      - codegen-only
      - review-case
      - review-plan
    default: full
  test_types:
    type: list
    values: [api, e2e, fuzz, performance]
    min_items: 1
    unique: true
    default: [api, e2e]
  run_tests: {type: bool, default: true}
  max_case_fix_attempts: {type: int, default: 3}
  max_plan_fix_attempts: {type: int, default: 3}
  max_healing_attempts: {type: int, default: 3}
  auto_archive: {type: bool, default: false}
  force_continue: {type: bool, default: false}

entrypoints:
  full:
    graph: workflow
    allow: "params.run_mode == 'full'"
  intake:
    graph: intake-workflow
    allow: "params.run_mode in ['full', 'case-only', 'review-case']"
  execute:
    graph: execute-workflow
    allow: >
      params.run_mode in
      ['full','api-only','e2e-only','plan-only','codegen-only','review-plan']
  case:
    graph: intake-workflow
    with:
      run_mode: case-only

policies:
  retry:
    agent-transient:
      max_attempts: 3
      retry_on: [timeout, transport, rate_limit]
      backoff:
        initial_seconds: 2
        multiplier: 2
        max_seconds: 30
        jitter: true
    cli-transient:
      max_attempts: 2
      retry_on: [timeout, transport]
      backoff:
        initial_seconds: 1
        multiplier: 2
        max_seconds: 10
        jitter: true
    never:
      max_attempts: 1
      retry_on: []

  timeout:
    agent:
      run_seconds: 5400
      heartbeat_seconds: 120
    cli:
      run_seconds: 900
      heartbeat_seconds: 30
    local-operation:
      run_seconds: 60
      heartbeat_seconds: 10

  scheduler:
    max_parallel_tasks: 4
    conflict_order: [topology, declaration, task_id]
```

Entrypoint 选择 graph，而不是在 driver 中解释 scope。`entrypoints.*.allow` 是 run-mode allowlist；不满足时在创建 invocation 前拒绝。`entrypoints.*.with` 只能覆盖 schema 声明的 params，并在 invocation 开始时冻结。

`test_types` 必须至少包含一个元素且不能重复；`api-only` 必须包含 `api`，`e2e-only` 必须包含 `e2e`。这些 cross-param constraints 在创建 invocation 前校验。`max_parallel_tasks` 是单进程的结构化并发上限，不改变 ready 语义。部署配置可以进一步降低该值，但不能在 resume 时提高已冻结 invocation 的上限。

### 6.3 NodeDef

普通 node 支持以下字段：

```yaml
nodes:
  explore:
    uses: skill:aa-explore
    agent: aa-doc-author
    when: "params.run_mode in ['full', 'case-only']"
    outputs:
      - change:explore/advisory.json
    gate: null
    retry: agent-transient
    timeout: agent
    with: {}
    resources: null
```

语义：

- `uses` 必填，必须能在 execution contract registry 或 `graphs:` 中解析；
- `outputs` 是 artifact contract，也是 change-dir 默认 write claim；
- `gate` 在 task handler 成功并验证 outputs 后求值，结果冻结进 `task_attempt_succeeded`；
- `retry`、`timeout` 引用顶层 policy；
- `when` 只在 node 的前驱满足后求值，首次 activation/skipped 决定写入 ledger，resume 不重新求值；
- `resources` 可选，只补充或收窄 registry 默认 claims；node 不必重复声明每个资源。

逻辑路径有三个 root namespace：`change:` 指当前 `qa/changes/<change-id>/`，`project:` 指 project root，`repo:` 指被测仓库 root。普通 output 只允许插值冻结的 `context.change_id`；fan-out output 另可使用其 `item_as` 变量。handler 只接收解析后的安全路径，不自行拼接绝对路径。

### 6.4 Graph state 与 reducer

graph 可以声明少量 typed state，用于路由、fan-out input 和 map-reduce；artifact 正文不复制进 state，只保存逻辑引用与 hash：

```yaml
state:
  generated_cases:
    type: list
    default: []
    reducer: append
  quality_summary:
    type: object
    default: {}
    reducer: merge_disjoint
```

node 只能通过声明的 `state_writes` 返回 update：

```yaml
state_writes:
  quality_summary: "result.quality_summary"
```

v2 只提供可重放的内建 reducer：`replace`、`append`、`merge_disjoint`、`set_union`。`replace` 在同一 super-step 只允许一个 writer；其他 reducer 按 structural task ID 而非完成时间合并，并要求输入为 JSON 可序列化值。自定义 Python reducer 不进入 v2 首版，避免 schema digest 相同但代码行为漂移。

未声明 reducer 的 key 默认 `replace`。state update 与 task write-set 一起成为 pending write，只有 `superstep_committed` 后才对下一个 Plan 可见。

### 6.5 Edge、route 与 terminal

无条件或布尔条件使用 edges：

```yaml
edges:
  - {from: START, to: explore}
  - {from: inspect, to: healing, when: "params.run_tests == true"}
  - {from: report, to: END}
```

枚举/gate 路由使用 exhaustive route：

```yaml
routes:
  - from: review
    select: "node('review').gate.verdict"
    cases:
      pass: END
      needs_fix: fix
      needs_human_review: human-review
      reject: STOP
      stop: STOP
    default: STOP
```

特殊 target：

- `START`：graph invocation 输入；
- `END`：graph/subgraph 正常完成；
- `STOP`：业务安全终止，CLI exit 20；
- `FAIL`：runtime/contract 失败，CLI exit 40。

`needs_human_review` 不直接映射终局，而是进入一个 `builtin:interrupt` node。

普通 node 在至少一条入边被选择后激活；多条互斥入边不会形成隐式 AND。需要等待多个 source 时必须使用显式 `builtin:join`。node 被 `when` 判为 false 后记为 skipped，不执行其普通出边；join 可以观察其声明 source 的 skipped 状态。编译器拒绝同一 checkpoint 中可能被两条非互斥路径重复激活的普通 node。

### 6.6 Join

```yaml
nodes:
  branch-join:
    uses: builtin:join
    join:
      sources: [api, e2e, fuzz, performance]
      mode: all_active
```

支持：

- `all`：所有 source 必须成功或显式 skipped；
- `all_active`：所有已激活 source 必须成功；条件为 false、因此从未激活的 source 不参与；
- `any`：首个成功 source 即满足，可选 `cancel_remaining: true|false`；v2 canonical workflow 不使用 `any`。

当前 execution 和 healing fixer join 都迁移为 `all_active`。旧 `any_active` 不能表达“两个都激活时必须等两个”，因此不直接保留。

### 6.7 Fan-out

```yaml
state:
  generated_cases:
    type: list
    default: []
    reducer: append

nodes:
  per-module-case-generation:
    uses: skill:aa-case-design
    agent: aa-doc-author
    fan_out:
      items: "advisory.modules"
      item_as: module
      key: "${module}"
      max_items: 32
      completion: all
      reduce:
        into: generated_cases
        using: append
    with:
      module: "${module}"
    outputs:
      - "change:cases/${module}/case.yaml"
    retry: agent-transient
    timeout: agent
```

首次 Plan 成功求值 `items` 后，runtime 在严格 transaction 中写入 `fan_out_expanded`：

```json
{
  "type": "fan_out_expanded",
  "graph_invocation_id": "...",
  "node": "per-module-case-generation",
  "source_reads_sha256": {"explore/advisory.json": "..."},
  "items": ["menu", "order"],
  "task_ids": ["...", "..."]
}
```

约束：

- item 必须是 JSON 可序列化值；作为路径模板时还必须是安全 path segment；
- `key` 必须求值得到唯一稳定 scalar；重复 key 在 expansion transaction 前失败；
- task ID 由 graph invocation、node ID 和 canonical key/item hash 构成，不把显示字符串正规化为 state key；
- expansion 一旦写入，resume 只重放冻结的 items；
- source artifact hash 发生漂移时 fail closed，错误为 `fan_out_source_drift`；
- downstream 默认等待全部 child；显式 reducer 按声明顺序或稳定 task ID 顺序合并，不能依赖完成先后顺序。

### 6.8 业务预算

Graph 在 invocation namespace 内声明预算：

```yaml
budgets:
  fix_attempts:
    limit: "params.max_case_fix_attempts"
```

只有指定 node 消耗：

```yaml
nodes:
  fix:
    uses: skill:aa-case-fixer
    budget:
      consume: fix_attempts
      on: committed
      exhausted_to: exhausted
```

`committed` 指 handler 成功、outputs/写策略校验通过，并在同一个 progression transaction 中追加：

- `task_attempt_succeeded`；
- `budget_consumed`；
- output hashes 和 pending graph writes。

计数通过 `(graph_invocation_id, budget_id, consumption_id)` 去重；`consumption_id` 从 task ID 派生。checkpoint 里的 `used/remaining` 只是 ledger 投影。

每个 cyclic strongly connected component 必须至少包含一个有限预算消费点和明确的 `exhausted_to`，同时 graph 必须声明 `max_supersteps`。编译器拒绝仅依赖全局 iteration guard 的业务 loop。

### 6.9 Interrupt

```yaml
nodes:
  human-review:
    uses: builtin:interrupt
    interrupt:
      reason: "case review requires a human decision"
      checkpoint: case-review-gate
      bind: audited_gate_read
      actions: [fix_and_proceed, accept_risk, stop]

routes:
  - from: human-review
    select: "resume.action"
    cases:
      fix_and_proceed: fix
      accept_risk: END
      stop: STOP
    default: STOP
```

进入 interrupt 时：

1. 不再计划新 task；
2. 允许同一 wave 已运行 sibling 完成；
3. 保存 sibling pending writes；
4. 为尚未进入 canonical workspace 的 audited artifacts 发布只读 `artifact_view`；
5. 严格写 `graph_interrupted`，包含 interrupt ID、checkpoint namespace、允许 action、artifact view 和 audited read hashes；
6. CLI 返回 30。

resume 时必须指定 interrupt ID 和 action。`bind: audited_gate_read` 要求当前 artifact hash 与 interrupt 记录一致；不一致则拒绝旧决定并要求重新投影。有效决定和 `graph_resumed` 在同一个 transaction 中写入。

`artifact_view` 是 write-set 的只读 materialization，用户和 reviewer 可以检查与 gate 完全相同的文件，但不能借此绕过 resume command 修改 pending task workspace。恢复后的 handler 继续使用原 checkpoint namespace 和私有 workspace。

并行分支可同时产生多个 interrupt。GraphStatus 返回 `pending_interrupts[]`；只有所有未被终局 preempt 的 interrupt 都被解决后 graph 才继续。

## 7. Canonical workflow v2 映射

### 7.1 Root graph

```yaml
graphs:
  bootstrap:
    max_supersteps: 5
    nodes:
      registry:
        uses: operation:skill-registry-check
        outputs: [change:workflow-state.yaml]
        gate: registry-gate
        retry: never
        timeout: local-operation
    edges:
      - {from: START, to: registry}
    routes:
      - from: registry
        select: "node('registry').gate.verdict"
        cases: {pass: END, stop: STOP}
        default: STOP

  workflow:
    max_supersteps: 100
    nodes:
      bootstrap:
        uses: graph:bootstrap

      intake:
        uses: graph:intake

      assurance:
        uses: graph:assurance

      archive:
        uses: skill:aa-archive
        agent: aa-archiver
        when: "params.auto_archive == true"
        outputs: ["project:qa/archive/${context.change_id}/"]
        gate: archive-gate
        retry: agent-transient
        timeout: agent

    edges:
      - {from: START, to: bootstrap}
      - {from: bootstrap, to: intake}
      - {from: intake, to: assurance}
      - {from: assurance, to: archive, when: "params.auto_archive == true"}
      - {from: assurance, to: END, when: "params.auto_archive == false"}
      - {from: archive, to: END}

  intake-workflow:
    max_supersteps: 50
    nodes:
      bootstrap: {uses: graph:bootstrap}
      intake: {uses: graph:intake}
    edges:
      - {from: START, to: bootstrap}
      - {from: bootstrap, to: intake}
      - {from: intake, to: END}

  execute-workflow:
    max_supersteps: 150
    nodes:
      bootstrap: {uses: graph:bootstrap}
      assurance: {uses: graph:assurance}
    edges:
      - {from: START, to: bootstrap}
      - {from: bootstrap, to: assurance}
      - {from: assurance, to: END}
```

三个公开 entrypoint 都先经过同一个 `bootstrap` graph，因此 registry gate 不会在 `intake`/`execute` 模式被绕过。业务 subgraph 本身保持可组合，不需要各自复制 bootstrap node。

### 7.2 Intake graph 与 case review loop

```yaml
graphs:
  intake:
    max_supersteps: 30
    nodes:
      explore:
        uses: skill:aa-explore
        agent: aa-doc-author
        when: "params.run_mode in ['full', 'case-only']"
        outputs: [change:explore/advisory.json]
        retry: agent-transient
        timeout: agent

      case-design:
        uses: skill:aa-case-design
        agent: aa-doc-author
        when: "params.run_mode in ['full', 'case-only']"
        outputs: [change:.qa.yaml, change:proposal.md, change:cases/]
        gate: case-design-gate
        retry: agent-transient
        timeout: agent

      case-review-cycle:
        uses: graph:case-review-cycle

    edges:
      - {from: START, to: explore, when: "params.run_mode in ['full', 'case-only']"}
      - {from: START, to: case-review-cycle, when: "params.run_mode == 'review-case'"}
      - {from: explore, to: case-design}
      - {from: case-review-cycle, to: END}

    routes:
      - from: case-design
        select: "node('case-design').gate.verdict"
        cases: {pass: case-review-cycle, stop: STOP}
        default: STOP

  case-review-cycle:
    max_supersteps: 20
    budgets:
      fix_attempts:
        limit: "params.max_case_fix_attempts"
    nodes:
      review:
        uses: skill:aa-case-reviewer
        agent: aa-reviewer
        outputs: [change:review/case-review.json]
        gate: case-review-gate
        retry: agent-transient
        timeout: agent

      fix:
        uses: skill:aa-case-fixer
        agent: aa-doc-author
        outputs: [change:review/case-review-apply-summary.md]
        retry: agent-transient
        timeout: agent
        budget:
          consume: fix_attempts
          on: committed
          exhausted_to: exhausted

      human-review:
        uses: builtin:interrupt
        interrupt:
          reason: "case review requires human review"
          checkpoint: case-review-gate
          bind: audited_gate_read
          actions: [fix_and_proceed, accept_risk, stop]

      exhausted:
        uses: operation:stop
        with: {reason: "case fix attempts exhausted"}

    edges:
      - {from: START, to: review}
      - {from: fix, to: review}
      - {from: exhausted, to: STOP}

    routes:
      - from: review
        select: "node('review').gate.verdict"
        cases:
          pass: END
          needs_fix: fix
          needs_human_review: human-review
          reject: STOP
          stop: STOP
        default: STOP
      - from: human-review
        select: "resume.action"
        cases:
          fix_and_proceed: fix
          accept_risk: END
          stop: STOP
        default: STOP
```

API plan 和 E2E plan 分别声明 `api-plan-cycle`、`e2e-plan-cycle`，结构与 `case-review-cycle` 相同，但引用各自 skill、gate、outputs 和 `params.max_plan_fix_attempts`。v2 首版允许这三个 named graph 存在少量重复，不增加 schema macro 语言。

Fuzz/Performance 当前没有自动 fixer；其 review gate 的 `needs_fix` 与 `needs_human_review` 都进入 audited interrupt，不能形成无 consumer 的 awaiting gate。

### 7.3 Assurance graph 的并行分支

```yaml
graphs:
  assurance:
    max_supersteps: 100
    nodes:
      fact-baseline:
        uses: skill:aa-fact-baseline
        agent: aa-doc-author
        when: >
          params.run_mode in
          ['full','api-only','e2e-only','plan-only','review-plan']
        outputs: [change:facts/fact-baseline.json]
        retry: agent-transient
        timeout: agent

      api:
        uses: graph:api-branch
        when: >
          'api' in params.test_types and params.run_mode in
          ['full','api-only','plan-only','review-plan','codegen-only']

      e2e:
        uses: graph:e2e-branch
        when: >
          'e2e' in params.test_types and params.run_mode in
          ['full','e2e-only','plan-only','review-plan','codegen-only']

      fuzz:
        uses: graph:fuzz-branch
        when: >
          'fuzz' in params.test_types and params.run_mode in
          ['full','plan-only','review-plan','codegen-only']

      performance:
        uses: graph:performance-branch
        when: >
          'performance' in params.test_types and params.run_mode in
          ['full','plan-only','review-plan','codegen-only']

      generation-join:
        uses: builtin:join
        join:
          sources: [api, e2e, fuzz, performance]
          mode: all_active

      execution:
        uses: operation:run-tests
        when: >
          params.run_tests == true and
          params.run_mode in ['full','api-only','e2e-only','codegen-only']
        outputs: [change:execution/execution-manifest.yaml]
        retry: cli-transient
        timeout: cli

      inspect:
        uses: skill:aa-inspect
        agent: aa-reviewer
        outputs:
          - change:inspect/failure-analysis.json
          - change:inspect/quality-gate-result.json
        retry: agent-transient
        timeout: agent

      healing:
        uses: graph:healing

      report:
        uses: skill:aa-report-generator
        agent: aa-reporter
        outputs:
          - change:report/quality-report.json
          - change:report/executive-summary.md
        retry: agent-transient
        timeout: agent

    edges:
      - {from: START, to: fact-baseline, when: "params.run_mode != 'codegen-only'"}
      - {from: START, to: api, when: "params.run_mode == 'codegen-only'"}
      - {from: START, to: e2e, when: "params.run_mode == 'codegen-only'"}
      - {from: START, to: fuzz, when: "params.run_mode == 'codegen-only'"}
      - {from: START, to: performance, when: "params.run_mode == 'codegen-only'"}
      - {from: fact-baseline, to: api}
      - {from: fact-baseline, to: e2e}
      - {from: fact-baseline, to: fuzz}
      - {from: fact-baseline, to: performance}
      - {from: api, to: generation-join}
      - {from: e2e, to: generation-join}
      - {from: fuzz, to: generation-join}
      - {from: performance, to: generation-join}
      - {from: generation-join, to: execution, when: "params.run_tests == true"}
      - {from: generation-join, to: END, when: "params.run_tests == false"}
      - {from: execution, to: inspect}
      - {from: inspect, to: healing}
      - {from: healing, to: report}
      - {from: report, to: END}
```

Plan/codegen branch 的 node 由同一 Plan 同时激活。scheduler 根据 execution contract registry 的资源范围选择最大无冲突集合并行执行；资源冲突的 ready node 按拓扑序、声明顺序、node ID 的稳定组合排序，进入后续 wave。

`codegen-only` 等从中间阶段启动的 eval 必须先 `import-checkpoint`，不能仅依赖已有 plan 文件存在。

### 7.4 API branch 示例

```yaml
graphs:
  api-branch:
    max_supersteps: 30
    nodes:
      plan:
        uses: skill:aa-api-plan
        agent: aa-doc-author
        when: "params.run_mode in ['full','api-only','plan-only','review-plan']"
        outputs:
          - change:plans/api-plan.md
          - change:plans/api-test-data-plan.md
          - change:plans/api-codegen-plan.md
          - change:plans/m3-review-summary.md
        retry: agent-transient
        timeout: agent

      review-cycle:
        uses: graph:api-plan-cycle

      codegen:
        uses: skill:aa-api-codegen
        agent: aa-test-author
        when: "params.run_mode in ['full','api-only','codegen-only']"
        outputs: [change:codegen/api-codegen-summary.md]
        gate: api-codegen-precondition-gate
        retry: agent-transient
        timeout: agent

    edges:
      - {from: START, to: plan, when: "params.run_mode != 'codegen-only'"}
      - {from: START, to: review-cycle, when: "params.run_mode == 'codegen-only'"}
      - {from: plan, to: review-cycle}
      - {from: review-cycle, to: codegen, when: "params.run_mode in ['full','api-only','codegen-only']"}
      - {from: review-cycle, to: END, when: "params.run_mode in ['plan-only','review-plan']"}
      - {from: codegen, to: END}
```

E2E、Fuzz、Performance branch 使用相同 graph vocabulary。Fuzz/Performance 的 review 没有自动 fixer，非 pass verdict 必须路由到 interrupt 或 STOP。

### 7.5 Healing subgraph

```yaml
graphs:
  healing:
    max_supersteps: 50
    budgets:
      healing_attempts:
        limit: "params.max_healing_attempts"

    nodes:
      entry:
        uses: builtin:gate
        with: {gate: healing-entry-gate}

      proposal:
        uses: skill:aa-fix-proposal
        agent: aa-doc-author
        outputs: [change:healing/fix-proposal.json]
        retry: agent-transient
        timeout: agent

      proposal-eligible:
        uses: builtin:gate
        with:
          expression: >
            file_exists('healing/fix-proposal.json') and
            any(fix_proposal.proposals,
                eligible == true and target in ['api', 'e2e'])

      allocate:
        uses: operation:allocate-healing-attempt
        retry: never
        timeout: local-operation
        budget:
          consume: healing_attempts
          on: committed
          exhausted_to: complete-exhausted

      fix-api:
        uses: skill:aa-api-codegen-fixer
        agent: aa-test-author
        when: "any(fix_proposal.proposals, target == 'api' and eligible == true)"
        outputs: [change:healing/api-apply-summary.json]
        retry: agent-transient
        timeout: agent

      fix-e2e:
        uses: skill:aa-e2e-codegen-fixer
        agent: aa-test-author
        when: "any(fix_proposal.proposals, target == 'e2e' and eligible == true)"
        outputs: [change:healing/e2e-apply-summary.json]
        retry: agent-transient
        timeout: agent

      fixer-join:
        uses: builtin:join
        join:
          sources: [fix-api, fix-e2e]
          mode: all_active

      safety:
        uses: builtin:gate
        with: {gate: fixer-safety-gate}

      safety-interrupt:
        uses: builtin:interrupt
        interrupt:
          reason: "fixer safety requires human review"
          checkpoint: healing.safety
          bind: audited_gate_read
          actions: [fix_and_proceed, accept_risk, stop]

      rerun:
        uses: operation:run-tests
        outputs: [change:execution/execution-manifest.yaml]
        retry: cli-transient
        timeout: cli

      reinspect:
        uses: skill:aa-inspect
        agent: aa-reviewer
        outputs:
          - change:inspect/failure-analysis.json
          - change:inspect/quality-gate-result.json
        retry: agent-transient
        timeout: agent

      decide:
        uses: builtin:gate
        with: {gate: healing-loop-gate}

      complete-not-needed:
        uses: operation:record-healing-status
        with: {status: not_needed}
        retry: never

      complete-resolved:
        uses: operation:record-healing-status
        with: {status: resolved}
        retry: never

      complete-exhausted:
        uses: operation:record-healing-status
        with: {status: exhausted}
        retry: never

      complete-failed:
        uses: operation:record-healing-status
        with: {status: failed}
        retry: never

    edges:
      - {from: START, to: entry}
      - {from: proposal, to: proposal-eligible}
      - {from: proposal-eligible, to: allocate, when: "node('proposal-eligible').value == true"}
      - {from: proposal-eligible, to: complete-failed, when: "node('proposal-eligible').value == false"}
      - {from: allocate, to: fix-api}
      - {from: allocate, to: fix-e2e}
      - {from: fix-api, to: fixer-join}
      - {from: fix-e2e, to: fixer-join}
      - {from: fixer-join, to: safety}
      - {from: rerun, to: reinspect}
      - {from: reinspect, to: decide}
      - {from: complete-not-needed, to: END}
      - {from: complete-resolved, to: END}
      - {from: complete-exhausted, to: END}
      - {from: complete-failed, to: END}

    routes:
      - from: entry
        select: "node('entry').gate.verdict"
        cases:
          enter: proposal
          skip: complete-not-needed
          stop: complete-failed
          reject: complete-failed
        default: complete-failed

      - from: safety
        select: "node('safety').gate.verdict"
        cases:
          pass: rerun
          needs_human_review: safety-interrupt
          stop: complete-failed
          reject: complete-failed
        default: complete-failed

      - from: safety-interrupt
        select: "resume.action"
        cases:
          accept_risk: rerun
          fix_and_proceed: proposal
          stop: complete-failed
        default: complete-failed

      - from: decide
        select: "node('decide').gate.verdict"
        cases:
          exit: complete-resolved
          continue: proposal
          stop: complete-exhausted
          reject: complete-failed
        default: complete-failed
```

这个 graph 完整替代 `kind: healing`、`HealingEpisodeSnapshot`、`HealingEpisodeAction` 和 `HealingActionExecutor`。`allocate` 是唯一消耗 healing business budget 的节点；fixer retry、rerun retry 和 reinspect retry 都不增加 healing attempts。

## 8. 条件激活、并行与资源冲突

### 8.1 条件冻结

node 的 `when` 只在所有 graph predecessor 已有稳定结果后求值。runtime 严格记录：

- `node_activated`；或
- `node_skipped`，包括 expression、input hashes 和 params digest。

resume 不重新求值已经冻结的 activation。尚未到达的 node 可以在未来 checkpoint 使用新的上游结果求值，但 invocation params 始终不可变。

### 8.2 Resource claims 来源

claims 按以下顺序合成：

1. execution contract registry 提供 skill/operation 的默认 reads、writes、exclusive 和授权范围；
2. `outputs` 自动补充对应逻辑 root 的 write claims；
3. node 显式 `resources` 只能增加 concurrency claim 或收窄授权范围；
4. 任一步仍无法推导完整范围时使用 `global:exclusive`。

示例 registry contract：

```yaml
contracts:
  skill:aa-api-codegen:
    handler: agent
    reads:
      - change:plans/api-*.md
      - change:review/api-plan-review.json
    writes:
      - change:codegen/api-codegen-summary.md
      - repo:tests/api/**
    exclusive: [repo:test-infra]
    retryable_errors: [timeout, transport, rate_limit]

  operation:run-tests:
    handler: local-operation
    reads:
      - repo:tests/**
    writes:
      - change:execution/**
    exclusive: [repo:test-runtime]
    retryable_errors: [timeout, transport]
```

workflow schema 只引用 `uses`，不重复这份信息。

resource conflict 的判定是 path/token 级别：read/read 可并行；write/write、write/read 或相同 exclusive token 必须串行。glob 先正规化到逻辑 root 再判包含/相交，不能以字符串前缀代替路径语义。

`graph:<id>` 的 resource footprint 由 compiler 对所有可达 child contract 做保守 union；确定无法同时激活的条件分支可以排除。这样父 scheduler 在启动 subgraph 前就能判断两个 branch 是否可并行，而不需要 driver 理解 branch 内部 node。

### 8.3 并行选择

Plan 将所有 ready tasks 按以下稳定键排序：

1. graph 拓扑层；
2. schema 声明顺序；
3. structural task ID。

scheduler 依次选择与当前 wave 无资源冲突的 task。冲突 task 留在 ready 集合，当前 wave Update 后进入下一 super-step。未知资源 task 因持有 `global:exclusive` 自动单独运行。

### 8.4 私有写层与实际写入验证

每个可能写文件的 task 都在私有 `TaskWorkspace` 中执行，base tree digest 固定为 Plan 使用的 committed checkpoint。task 完成后，runtime 独立计算其 diff；每个变化路径必须符合该 task contract：

当前 canonical agent skill 和 operation 一律按 write-capable 处理，即使某个 node 没有显式 `outputs`；只有 registry 明确标记 `side_effect_free: true` 的 builtin/read-only handler 才能跳过私有写层。

- 无 claim 覆盖 → `forbidden_write`，非重试失败；
- 与 sibling claim 重叠 → compiler 或 Plan 阶段视为资源冲突，不允许同 wave；
- symlink/path escape → `forbidden_write`；
- outputs 缺失或 hash 无法冻结 → `invalid_output`，默认非重试。

验证通过后，runtime 把 patch/tree objects、output hashes 和 base digest 固化为内容寻址 `write_set_id`，再写 `task_attempt_succeeded`。object store 由 coordinator 独占写入且不暴露给 handler。临时 workspace 可以删除；resume 从 write-set 重建，不依赖残留进程目录。

Update 按 structural task ID 排序合并同一 wave 的 write-sets。已声明不冲突但实际触碰同一路径时，以 `resource_contract_violation` fail closed；canonical workspace 的 base digest 漂移时，以 `workspace_drift` 停止，不覆盖 operator 的外部改动。只有所有必需 sibling 成功后才原子推进 canonical tree pointer 与 `superstep_committed`；物理文件 materialization 可由该 pointer 幂等恢复。

subgraph 也运行在父 task 的私有写层中。child super-step 只更新该 subgraph workspace；subgraph 完成后冻结一个 write-set 交给父 graph。child interrupt 时保留 namespace 和 write-set objects，恢复时不重跑已成功 child。这样 API 与 E2E subgraph 可以真正并行，同时不会提前把半完成分支暴露给父图或 sibling。

无法重定向到 `TaskWorkspace` 的外部副作用必须在 contract 中声明 exclusive resource 和 reconcile/idempotency 策略；否则 compiler 不允许该 handler 参与并行 wave。

## 9. Task attempt、retry 与 lease

### 9.1 标识

- `graph_invocation_id`：一次 root/subgraph 调用；
- `checkpoint_ns`：父 namespace + 调用 node + invocation UUID；
- `task_id`：一次逻辑 node invocation，retry 时不变；
- `attempt_id`：一次物理执行，retry 时变化；
- `superstep_id`：一次 Plan/Execute/Update 边界。

### 9.2 严格生命周期事件

| 事件 | 含义 |
|---|---|
| `task_attempt_started` | attempt 获得执行权；替代 `dispatch_signed` |
| `task_attempt_succeeded` | handler 成功、outputs 与写策略验证通过，pending write 已持久化 |
| `task_attempt_failed` | handler 或 contract 失败，包含 typed error kind |
| `task_attempt_abandoned` | started attempt 的 lease 到期且无法重新连接 |

`task_attempt_started` 至少包含：

```json
{
  "task_id": "...",
  "attempt_id": "...",
  "graph_invocation_id": "...",
  "checkpoint_ns": "...",
  "superstep_id": "...",
  "node": "api.codegen",
  "input_sha256": "...",
  "graph_digest": "...",
  "contract_digest": "...",
  "attempt_number": 1,
  "lease_expires_at": "...",
  "started_at": "..."
}
```

### 9.3 Heartbeat

高频 heartbeat 原子覆盖：

```text
qa/changes/<id>/running-tasks.json
```

文件保存所有并行 task 的 PID/session、host、last heartbeat 和 lease expiry。它不是预算或成功状态权威；丢失后可退回 `task_attempt_started.lease_expires_at` 判断。

进程恢复时：

1. execution contract 支持 reconnect 且 task/session 仍活跃 → adopt；
2. lease 未到期但不能 reconnect → 等待或显式 operator cancel，不重复执行；
3. lease 到期 → 严格追加 `task_attempt_abandoned`；
4. remaining retry budget > 0 → 创建新 attempt；
5. budget 耗尽 → task/graph failed。

`abandoned` 消耗一次 task retry attempt，不消耗业务 loop budget。

### 9.4 Retry 规则

`max_attempts` 包含第一次。handler 必须返回稳定 error kind：

```text
timeout | transport | rate_limit | auth | invalid_input |
invalid_output | forbidden_write | contract | internal
```

只有 policy `retry_on` 中的 kind 可自动重试。gate 的 `needs_fix`、`needs_human_review`、`reject`、`stop` 都是成功 task 的业务 verdict，不是技术失败，不进入 retry。

失败事件同时保存 `next_retry_at`。resume 必须尊重已持久化 backoff，不能因进程重启立即重试。

### 9.5 Update commit 失败

handler 已成功、write-set 已冻结且 `task_attempt_succeeded` 已记录时，后续 super-step Update 失败不得重跑 handler。runtime 只根据 write-set 和 ledger 重试 progression Update/materialization。

如果 write-set 已经存入 object store、但进程在写 `task_attempt_succeeded` 前崩溃，则 object 只是未引用垃圾，不能单独证明 attempt 成功；恢复按 lease/retry 重新执行并由 GC 清理。对于无法进入私有写层的外部副作用，execution contract 的 reconcile 逻辑用原 attempt ID 和领域幂等键尝试补记成功。无法证明同一 attempt 结果时，按 at-least-once 语义创建新 attempt；相关 handler 必须幂等或能检测已有结果。

## 10. Pending writes 与 sibling failure

同一 wave 的成功 task 不等到所有 sibling 完成才落审计记录。每个成功 task 立即通过 progression transaction 写入：

- `task_attempt_succeeded`；
- content-addressed `write_set_id` 与 base tree digest；
- output/artifact hashes；
- frozen gate report；
- graph-state pending writes；
- 可选的 `budget_consumed`。

如果 sibling 失败：

1. 不计划下一批 node；
2. 允许已运行 sibling 完成；
3. 成功 sibling 的 write-set 维持 pending，不 materialize 到 canonical workspace；
4. 失败 sibling 独立 retry；
5. resume 根据 task ID 找到 pending success，不再执行；
6. 当本 super-step 全部必需 task 成功后，runtime 合并 write-sets，并由 `superstep_committed` 一次性使结果对后续 Plan 可见。

retry 耗尽时 graph 状态为 `failed`，pending successes 仍保留用于诊断或显式人工 retry；普通 `resume` 不会无上限重置 retry budget。

`STOP/REJECT` 是成功求值的业务终局：settled wave 中已验证的 write-sets 先提交，再记录 `graph_stopped`，保证 review/gate 证据可见。技术失败、retry pending 或 interrupt 不提交不完整 wave；它们通过 pending write-sets 和只读 terminal/interrupt view 暴露诊断信息。integrity failure 永不 materialize offending task 的 write-set。

## 11. Checkpoint 与恢复

### 11.1 Checkpoint 内容

每个 committed super-step 产生 checkpoint：

```json
{
  "checkpoint_id": "...",
  "parent_checkpoint_id": "...",
  "checkpoint_ns": "...",
  "event_seq": 123,
  "graph_digest": "...",
  "contract_digests": {"skill:aa-api-codegen": "..."},
  "params_sha256": "...",
  "values": {},
  "next_tasks": [],
  "pending_tasks": [],
  "pending_write_sets": {},
  "pending_interrupts": [],
  "fan_out_expansions": {},
  "budget_projection": {}
}
```

严格 ledger 是权威；checkpoint JSON 是带 `event_seq` 和 digest 的性能快照。hash 不匹配、文件缺失或落后时从 ledger 重建。

`driver.json` 只保存当前 GraphRuntime 进程状态和 latest checkpoint pointer。pointer 落后不影响恢复，status 命令应以 ledger/projected checkpoint 为准。

### 11.2 Schema 与 contract pinning

invocation 开始时 canonicalize v2 schema 和所有已引用 execution contracts，记录 digest。resume 时 digest 不一致则拒绝执行，错误为 `graph_definition_changed`。

拓扑或 contract 变化必须通过未来独立的显式 checkpoint migration 机制；v2 首版不自动猜测迁移。

### 11.3 Terminal 优先级

同一 wave 可能同时产生多种结果。优先级为：

1. integrity/runtime `FAIL`；
2. gate `STOP/REJECT`；
3. pending interrupt；
4. retry pending；
5. completed。

一旦出现高优先级结果，不再 Plan 新 task；已运行 sibling 仍按第 10 节完成或持久化失败。

## 12. 显式 import-checkpoint

### 12.1 用途

只用于：

- eval fixture；
- benchmark seed；
- 明确授权的一次性 v1 artifact 导入。

生产 workflow 不因 artifacts 存在而隐式完成 node。

### 12.2 Manifest

```yaml
schema_version: "2"
entrypoint: execute
source:
  kind: eval-fixture
  fixture_id: eval-sample-001
  fixture_digest: "sha256:..."
inputs:
  change:plans/api-plan.md: "sha256:..."
  change:plans/api-test-data-plan.md: "sha256:..."
completed:
  - path: execute-workflow/assurance/api/review-cycle
    graph: api-plan-cycle
    node: review
    outputs:
      change:review/api-plan-review.json: "sha256:..."
    gate:
      id: api-plan-review-gate
      verdict: pass
      reads_sha256: "sha256:..."
budgets: []
```

Import 流程：

1. 校验 fixture digest、schema digest、entrypoint 和安全路径；
2. 校验每个 input/output hash；input 只建立来源证明，不伪造 node completion；
3. 校验 completed nodes 构成合法 predecessor closure；
4. 重新求值并冻结 gate report；
5. 对导入的 budget-consuming node 要求 manifest 明确对应 consumption；
6. 在一个 progression transaction 中写 `task_imported[]`、可选 `budget_consumed[]` 和 `checkpoint_imported`；
7. 从未完成 task 继续。

导入不会伪造物理 attempt；`task_imported` 与 `task_attempt_succeeded` 是不同事件类型。

`path` 是从 entrypoint root 开始的 structural invocation path，用于区分同一 named graph 的多次调用；fan-out child 还必须附带冻结的 `task_key`。import 不接受仅靠 graph/node 名称的模糊匹配。

## 13. Gate 与 audited decision

v2 保留现有 gate DSL、缺失/非法文件 fail-closed 策略和 verdict canonical order。变化是：

- gate 只能由 `gate:` node outcome 或 `builtin:gate` 执行；
- attached `gate:` 在该 task 的 base snapshot + 私有 write-set 视图上执行；`builtin:gate` 在当前 graph committed workspace 上执行；
- gate result 必须冻结 reads hashes；
- edge route 读取冻结的 node gate result，不在每次 status 时对可变文件重新裁决；
- human override 只能通过 interrupt resume command；
- audited gate 的 `accept_risk/fix_and_proceed` 必须绑定 interrupt 时的 read hash；
- 同一 review node 重跑后产生新的 task invocation 和 gate report，旧 decision 不跨 invocation 生效。

当前 gates 的 rule bodies 可以机械迁移到 v2 顶层 `gates:`；本设计不改变其业务表达式语义。

## 14. Static compiler 校验

加载 schema v2 后必须先编译。所有错误在执行前一次性报告：

1. schema version 必须精确为 `"2"`；
2. entrypoint、graph、node、gate、policy、execution contract 引用必须存在；
3. graph node ID 和 graph ID 必须是安全、唯一标识；
4. 所有 node 必须从 START 可达，并能到 END/STOP/FAIL/interrupt；
5. subgraph 引用图不能形成递归调用环；
6. graph 内每个 cyclic SCC 必须有有限业务预算消费点和 exhausted route；
7. 每个 graph 必须有正数 `max_supersteps`；
8. route 对 gate verdict 必须 exhaustive，或显式声明 fail-closed default；
9. join sources 必须位于同一 graph，且不能依赖 join 自身；
10. fan-out output 模板引用必须来自 `item_as`，路径必须保持在声明 root 内；
11. retry error kinds 必须来自稳定词表，`max_attempts` 范围为 1–10；
12. timeout 与 heartbeat 必须为正数，heartbeat 小于 run timeout；
13. budget limit 必须是非负 int param/expression，消费 node 必须在同一 graph；
14. interrupt action 必须有完整 resume route；
15. 静态已知的 resource conflict 不报 schema 错误，但 compiler 记录 serialization constraint；
16. 显式 node resources 不能扩大 execution contract 的授权 write policy，只能补充 concurrency claim 或收窄授权；
17. entrypoint allow、param min/unique/cross-param constraints 和 scheduler bounds 必须在 invocation 前通过；
18. state type、reducer、`state_writes` 引用必须有效；同 step 的 `replace` 多 writer 必须拒绝；
19. condition、route 和 gate DSL 在加载期完成 parse、arity 和 reference 校验。

compiled graph 保存 canonical digest，GraphRuntime 只接受 compiled model，不直接解释原始 YAML。

## 15. Benchmark 与 eval 覆盖

| 当前需求 | v2 落点 |
|---|---|
| `run_mode`/`test_types` | frozen params + node `when` |
| full/intake/execute/case | `entrypoints` |
| API/E2E/Fuzz/Performance 并行 | assurance graph 同层 subgraph tasks |
| execution 等全部 active codegen | `generation-join.mode: all_active` |
| case/API/E2E review fix | 三个 named cyclic graphs + business budget |
| healing | named healing graph + allocate budget + parallel fixers |
| hard timeout | timeout policy + lease + abandoned attempt |
| 外围 workflow attempts | 可重启进程，但不能重置 task retry ledger |
| orphan `dispatch_signed` | attempt lifecycle 自动收敛，不再删除事件 |
| human gate recovery | interrupt/resume command |
| eval L0–L3 fixture | explicit import-checkpoint |
| codegen-only 中途运行 | imported predecessor closure + entrypoint params |
| archive | root conditional node；benchmark 也可继续显式独立调用 |
| forbidden write | contract registry + wave union diff |

benchmark 迁移完成后应删除 `prune_stale_dispatches`。外层 `CURSOR_MAX_WORKFLOW_ATTEMPTS` 可以保留为“重启 driver 进程”的基础设施保护，但重复启动不能扩大任何 task 或 business budget。

## 16. 事件模型

新增严格事件：

```text
graph_invocation_started
node_activated
node_skipped
fan_out_expanded
superstep_planned
task_attempt_started
task_attempt_succeeded
task_attempt_failed
task_attempt_abandoned
budget_consumed
graph_interrupted
graph_resumed
superstep_committed
graph_completed
graph_stopped
graph_failed
task_imported
checkpoint_imported
```

旧事件迁移：

| v1 | v2 |
|---|---|
| `dispatch_signed` | `task_attempt_started` |
| `phase_outcome_committed` | `task_attempt_succeeded` + pending writes |
| healing allocation event | operation task success + `budget_consumed` |
| driver checkpoint counter | `superstep_committed` / checkpoint event seq |
| best-effort `phase_retry` | strict failed/abandoned attempt + next retry time |

driver started/finished、heartbeat 和 UI progress 仍可作为 best-effort telemetry，但不得参与业务判断。

## 17. 状态与 CLI

GraphStatus 至少返回：

```json
{
  "invocation_id": "...",
  "entrypoint": "full",
  "status": "running|interrupted|completed|stopped|failed",
  "checkpoint_id": "...",
  "event_seq": 123,
  "superstep": 7,
  "running_tasks": [],
  "pending_tasks": [],
  "pending_interrupts": [],
  "budgets": {},
  "terminal": null
}
```

CLI：

```text
aa workflow run --change <id> --entrypoint full --params <json>
aa workflow status --change <id> --json
aa workflow resume --change <id>
aa workflow resume --change <id> --interrupt <id> --action accept_risk --reason <text>
aa workflow import-checkpoint --change <id> --manifest <path>
```

保留现有四类退出码：

- 0 completed；
- 20 stopped/rejected；
- 30 interrupted/needs human review；
- 40 runtime、schema、contract 或 integrity failure。

## 18. 模块布局

建议实现集中在一个深模块内：

```text
assurance_agent/workflow/graph/
├── schema_v2.py          # YAML models + parse
├── compiler.py           # refs、SCC、route、resource constraints、digest
├── models.py             # executable graph/task/result/checkpoint
├── runtime.py            # 唯一对外 GraphRuntime interface
├── planner.py            # activation、fan-out、ready、wave plan
├── scheduler.py          # 并行执行、资源冲突、retry/backoff
├── task_runner.py        # handler registry 内部 seam
├── checkpoint.py         # ledger projection、snapshot、import
├── leases.py             # running-tasks liveness
├── resources.py          # execution contracts/resource claims/write attribution
├── workspace.py          # task 私有写层、write-set freeze/merge/materialize
└── handlers/
    ├── agent.py
    ├── operation.py
    ├── subgraph.py
    ├── gate.py
    ├── join.py
    └── interrupt.py
```

迁移后删除或吸收：

- `workflow/orchestration/loop_registry.py`；
- `healing_episode.py` 的 projector/control action；
- `review_fix_episode.py`；
- driver 内 `_dispatch_entry` kind 分支；
- `CliPhaseExecutor`、`HealingActionExecutor`；
- v1 dynamic fan-out projection；
- `DriverState.iteration` 作为 checkpoint 权威的语义。

现有 gate DSL、progression transaction、domain operations、agent adapters 和 artifact validators 作为 GraphRuntime 内部依赖继续复用。

## 19. 错误处理

| 类别 | 处理 |
|---|---|
| schema/compiler error | 执行前 FAIL，零 task 写入 |
| transient task error | 按 policy 严格重试，跨 resume 保持剩余次数 |
| non-retryable task error | sibling settle 后 graph failed |
| retry exhausted | graph failed，不因普通 resume 重置 |
| gate stop/reject | graph stopped |
| gate needs human | graph interrupted |
| missing/invalid output | 默认 non-retryable `invalid_output` |
| forbidden write/path escape | integrity FAIL，尽力取消尚未开始的 task |
| fan-out source drift | FAIL，不重算 expansion |
| schema/contract digest drift | 拒绝 resume |
| lease 未过期 | 不重复 task |
| lease 到期 | abandoned，按剩余 retry budget 决定是否重试 |
| checkpoint snapshot 损坏 | 从 ledger 重建 |
| ledger integrity 失败 | FAIL，不用 snapshot 掩盖 |

## 20. 测试策略

### 20.1 Compiler

- v2 canonical schema 编译成功；
- v1 schema 给出明确不兼容错误；
- unknown refs、unreachable nodes、subgraph recursion、non-exhaustive routes 失败；
- 无预算 cycle、无 exhausted route cycle 失败；
- fan-out 路径、retry kinds、timeout、interrupt actions 静态校验；
- graph/contract digest 稳定。

### 20.2 Runtime 单元与集成

- 同层无冲突 tasks 实际重叠运行；
- 资源冲突 tasks 自动串行且顺序稳定；
- 未知资源 task 独占运行；
- parallel task 看不到 sibling 未提交文件，write-set 合并顺序确定；
- sibling 实际越界/重叠写入 fail closed，canonical workspace 不出现半提交；
- sibling A 成功、B 失败，resume 只重跑 B；
- B 在下一进程重试时保留 attempt count/backoff；
- hard kill → lease expiry → abandoned → retry；
- retry exhausted 后普通 resume 不扩容；
- fixer 三次 transport retry 只消耗一次 business budget；
- budget 事件与 success 事件原子、幂等；
- interrupt 允许 sibling settle，resume 从原 namespace 继续；
- audited hash 漂移拒绝旧 human decision；
- parallel multiple interrupts 可分别恢复；
- fan-out expansion 冻结、child ID 无碰撞、source drift fail closed；
- superstep Update 失败不重跑已成功外部 task；
- checkpoint snapshot 删除后可从 ledger 重建。

### 20.3 Fault injection

在以下边界逐一 kill 进程并验证恢复：

1. `task_attempt_started` 之前/之后；
2. handler 写输出后、success event 前；
3. success/pending write transaction 后；
4. sibling 部分成功时；
5. budget consumed transaction 后；
6. interrupt event 前/后；
7. `superstep_committed` 前/后；
8. checkpoint snapshot 写入前/后；
9. heartbeat 更新过程中。

### 20.4 Benchmark/Eval

- 当前 full benchmark 五个 requirement items；
- API/E2E/Fuzz/Performance 四分支真实并行；
- `workflow-full`、`workflow-case`、四个 codegen suite、`workflow-run`；
- L0–L3 fixture 通过 import-checkpoint 启动；
- resume benchmark 不再调用 stale dispatch pruning；
- forbidden write、secret leak、schema validity hard gates 不回退；
- 同一 fixture 和 params 的 structural task keys、fan-out expansion 与事件因果顺序在去除 invocation namespace 后可确定性比较。

## 21. 验收标准

1. 打包的 canonical workflow 已完全改写为 schema v2，源码中没有 `kind: healing/review_fix`。
2. driver 只调用 GraphRuntime，不再注入 `CliPhaseExecutor` 或 `HealingActionExecutor`。
3. API/E2E 等无冲突 ready tasks 在测试中被证明真实并行。
4. 冲突 tasks 被稳定串行，不要求每个 node 声明 resources。
5. sibling 成功后另一个失败/崩溃，成功 write-set 不提前 materialize，resume 不重复成功 sibling。
6. retry attempt 总数跨进程严格不超过 policy，普通 resume 不能重置。
7. review-fix 与 healing budget 只由声明的成功业务节点消耗，ledger 可独立重建次数。
8. healing 和 review-fix 都是普通 named graphs；新增 subgraph 不修改 runtime 分支。
9. human review 以 interrupt 保存并能通过 audited resume command 恢复。
10. fan-out expansion 被严格持久化，task ID 无碰撞，source drift fail closed。
11. hard kill 后通过 lease/abandoned 自动恢复，无需删除 ledger 事件。
12. eval fixture 通过显式 import-checkpoint 运行，生产 workflow 不隐式采纳裸 artifacts。
13. checkpoint snapshot 损坏或 `driver.json` 落后时，ledger 重建得到相同 GraphStatus。
14. task 私有写层能从 write-set 恢复；并行 task 的越界写和实际路径冲突都 fail closed。
15. 当前 workflow/eval/benchmark 回归套件全部通过，且 write-policy hard gate 不回退。

## 22. 实施边界与顺序建议

本设计是一个完整目标，但实现应按可回滚切片推进：

1. v2 models/compiler/digest；
2. task lifecycle、ledger projection 和 checkpoint；
3. 单线程 GraphRuntime 兼容切片；
4. retry/lease/reconcile；
5. 真并行 scheduler、resource registry、pending writes；
6. review-fix named graphs；
7. healing named graph 与 interrupt；
8. fan-out freeze/reduce；
9. import-checkpoint 与 eval migration；
10. canonical schema、benchmark 和旧 runtime 删除。

具体 commit 拆分和测试顺序不在本设计中展开；用户批准本 spec 后由 implementation plan 定义。

## 23. 参考模型

本设计只借鉴以下 LangGraph 概念：

- [Graph API：State、Nodes、Edges 与 compile](https://docs.langchain.com/oss/python/langgraph/graph-api)
- [Pregel runtime：Plan、Execution、Update super-step](https://docs.langchain.com/oss/python/langgraph/pregel)
- [Persistence：super-step checkpoint 与 pending writes](https://docs.langchain.com/oss/python/langgraph/persistence)
- [Subgraphs：作为父图 node 与 checkpoint namespace](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)
- [RetryPolicy、PregelExecutableTask 与 Send](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py)

AA 不依赖这些实现。GraphRuntime 必须继续服从本项目自己的 progression、gate、artifact、audit 和 ledger 契约。
