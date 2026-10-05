# Flow 层：用声明式拓扑替换手写 StateGraph

## 背景

六个 capability wheel 和 product 现在直接写 LangGraph `StateGraph`，图层代码约
8300 行（`*/graphs/*.py`）。业务 wheel 有 24 个子图构建函数，product 有 4 种父图
形状（11 个 thin 入口、`intake`、`execute-tail`、`full`）。`ProductState` 声明了
115 个字段，其中 22 个是运行入参、15 个是控制字段、6 个是 ledger 类字段、72 个是
在子图之间搬运的业务数据。

逐图审计后，问题集中在五类：

1. **路由读多个字段。** 很多路由函数组合 publish 里算出来的值、计数器、人工决定和
   `attempt_failure`，比如 `route_case_review`（`case.py:219-236`）、`route_quality`
   （`routes.py:40-71`）、`route_issue_analysis`（`execute.py:419-437`）。
2. **业务逻辑写在图层。** select、publish、普通节点里有哈希、模型校验、领域分类，
   比如 `publish_inspect` 里算 disposition（quality `nodes.py:379-406`）、retro 的
   `assemble_analyses`（improvement `nodes.py:243-347`）、product 的 11 个业务
   adapter。
3. **步骤之间靠 state 传数据。** 只有 intake 的 op 声明了具名写入；其余 wheel 的写入
   都是裸路径，`TaskOp` 和 `TaskAttemptContract` 没有 `input_bindings()`，跨步骤的
   数据只能经 state 搬运。
4. **控制状态散落。** 循环计数由业务节点维护；`rounds_budget` 一个字段先后装着
   healing 预算和 coverage 预算（`execute.py:160`、`216`、`269`）；`attempt_failure`
   成功后不会清除（`node_factory.py:370-381`），只能靠 `case_start`、
   `clear_report_state`、`clear_current_cycle` 三处手工清。
5. **子图结局不统一。** assess 和 issue 子图不写 `status`；execution 的 `status` 是
   证据里的 passed/failed；init 的 `completed` 在父图被叫作 `initialized`；thin 入口
   把空 status 映射成 `completed`。

## 目标

- 业务 wheel 和 product 用 Flow 描述拓扑。业务代码里不再出现 `StateGraph`、state
  `TypedDict`、select、publish、路由函数和普通函数节点。
- Flow 编译成 LangGraph。attempt 节点继续走现有 `add_attempt_node`，Kernel 事务、
  journal 回放、写范围授权、封存提交全部不变。
- 图状态只剩三类：入参、框架控制字段、`artifact_ledger`。
- Flow 定义仍是 wheel 里的 Python 代码。改拓扑照旧需要 code review、仓库门禁、重建
  wheel 和部署；`.aa/` 仍然只放组织配置。

## 编译期强制的规则

1. 业务 Flow 里没有自由函数节点。需要计算的代码放进 op 的 prepare / finalize hook，
   或者写成独立的 `router.task`。
2. 路由只能看两样东西：这次 attempt 成功还是失败（可以按失败种类分），或者 op 输出
   模型上的一个 `Literal` 字段。`routes` 必须覆盖这个 `Literal` 的全部取值，不提供
   通配。
3. 步骤之间只通过 ledger 里的文件和结局传数据。
4. 循环必须声明预算，计数器归框架所有。
5. 每个 op 的每个输入字段都必须能解析，来源只有五种：Flow 入参、循环轮次、gate 决定、
   上游步骤的具名写入、字段默认值。
6. 并行分支写入的 ledger key 不能重名。
7. capability 和 product 包不 import `langgraph`，由 importlinter 合约强制。
   checkpointer 这类基础设施除外。

## 原语

### Flow

```python
Flow(
    name: str,
    *,
    input: type[BaseModel] | type[TypedDict],
    outcomes: tuple[str, ...],
    public: Mapping[str, ProductStatus] | None = None,
)
```

- `outcomes` 由编译器生成终点节点，结局写进框架通道 `outcome`。业务字段里的
  `status`（比如 execution 证据的 passed/failed）留在 op 输出文件里，两者分开。
- 入口按 `input` 模型校验。thin 入口现在的 `validate` 节点
  （`entrypoints.py:72-75`）由它替代。
- `public` 只给 product 根 Flow 用，声明"结局到产品状态"的映射。编译器据此生成
  `ProductPublicOutput`，receipt 由框架从 attempt 回执里收集。
- attempt 的 semantic id 是"op 的 ledger namespace + Flow 内路径"，例如
  `intake.case-review`、`generation.api.codegen`。这和现在的 id 一致，嵌进 product
  也不会改变 id。

### step

```python
flow.step(
    name: str,
    op: AgentOp | TaskOp | TaskAttemptContract,
    *,
    then: str | LoopTarget | None = None,
    route_on: str | None = None,
    routes: Mapping[str, str | LoopTarget] | None = None,
    on_failure: str | Mapping[str, str],
    inputs: Mapping[str, InputSource] | None = None,
)
```

- 一个 step 就是一个完整事务：计算 attempt key、组装输入、资源仲裁、prepare hook、
  Agent 或 task、本地校验、finalize hook、validators、封存提交、登记 ledger。
  before / after 代码挂在 op 声明上，Flow 不提供图层 hook。
- `then` 和 `route_on`+`routes` 二选一。`route_on` 指向 op 输出模型上的 `Literal`
  字段，编译时检查字段存在、类型是 `Literal`、`routes` 覆盖全部取值。
- `on_failure` 可以是一个目标，也可以按失败种类分流。可用的键有：`rejected`、
  `permanent`、`permanent:invalid_output`、`permanent:invalid_input`、
  `committed_effect_failure`、`*`。匹配时越具体越优先，映射里必须带 `*`。
- `inputs` 只做来源绑定，不接受函数。可用的来源：入参字段名、`loop.round`、
  `gate.action`、`gate.field("名字")`、`const(值)`。没写在 `inputs` 里的字段先按
  同名入参取，再按 handle 的 slot 从 ledger 取。
- 编译方式：attempt 节点外面包一层，按这次 attempt 自己的结果返回
  `Command(goto=...)`。路由不再读 `attempt_failure` 通道，这个通道只留作诊断输出，
  上一轮残留的失败也就影响不到下一步。

### loop

```python
with flow.loop(name, *, budget: int | str, on_exhausted: str) as review:
    ...                      # 循环体里的 step / gate / subflow
review.next(target)          # 路由目标：推进一轮，再跳到 target
review.round                 # 输入来源：当前轮次
```

- `budget` 可以写整数，也可以写入参里的路径，例如 `"budgets.review_rounds"`。
- `next(target)` 可以出现在任意路由或 gate 里，可以有多个不同的目标。经过时先检查
  `round < budget`：满足就把轮次加一再跳转，不满足就去 `on_exhausted`。目标可以在
  上游，也可以在下游。
- 预算的含义是"最多推进几轮"。现有四个循环都是这个语义：case review
  （`case.py:162-165` 在推进前检查 `used < budget`）、generation lane
  （`api.py:104-107`）、healing（`failure.py:59-64`）、coverage
  （`loop_state.py:13-16`）。
- 从循环外进入循环体时，计数器归零。计数器放在框架控制通道里，按循环路径区分，所以
  不同循环不会再共用 `rounds_budget` 这样的字段。
- 循环体内 step 的 activation 由框架按"所在循环路径 + 各层轮次 + 分支名"生成，
  通过 `BusinessActivation.for_trigger` 编码，超过 64 个字符时用摘要截断。
  reviewed case 摘要、batch id 这类内容身份已经进了输入 digest，attempt key 里本来
  就有，不需要再放进 activation。

### gate

```python
approval = flow.gate(
    name,
    *,
    decision: type[BaseModel],          # 必须有 Literal 类型的 action 字段
    routes: Mapping[str, str | LoopTarget],
    show: tuple[ArtifactHandle, ...] = (),
    interrupt_id: str | None = None,
)
approval.action                          # 输入来源
approval.field("approval_ref")           # 输入来源：decision 上的其他字段
```

- 编译成 LangGraph `interrupt()`。payload 由框架生成：原因（gate 名）、可选动作
  （decision 里的 `Literal`）、所在循环的轮次、`show` 里各个文件的 ref。不接受自定义
  payload 函数。
- `routes` 必须覆盖全部动作，目标可以是 `loop.next(...)`。case 和 generation 的
  `request_rework` 就是这样写的，预算用完时自动去 `on_exhausted`。
- decision 上 action 以外的字段由框架保存，下一步通过 `gate.field(...)` 取用。healing
  审批带回的 `approval_ref`（healing `nodes.py:284-288`）走这条路。
- `interrupt_id` 默认由挂载路径生成，例如 `generation.api.human-review`。挂到
  `full` 上是 `full.human-review`、`full.approval`、`full.api.human-review`。产品
  不传入旧值。`aa resume` 按 LangGraph snapshot 的 `item.id` 匹配。

### parallel

```python
flow.parallel(
    name,
    *,
    branches: Mapping[str, Flow | AgentOp | TaskOp],
    select: str | None,
    require: str,
    then: str,
    on_failure: str,
)
```

- 编译方式：给每个选中的分支发一个 `Send`（可能的目标在编译期固定），分支是编译好的
  子图，用一个 `defer=True` 的 join 节点汇合。各分支的结局按分支名记进框架通道，
  没选中的分支记为 skipped。join 检查所有选中分支的结局都等于 `require`，然后用
  `Command` 跳到 `then` 或 `on_failure`。
- `select` 指向入参里的分支名列表，编译时检查取值都在 `branches` 里。`select=None`
  表示全部分支都跑，retro 的三个分析步骤会用到。
- 分支直接写一个 op 时，等价于只有一步的 Flow。
- 分支的产出走 ledger，join 只记录结局。编译器检查不同分支写入的 ledger key 不重名。
- 资源仲裁按读写范围排队：读写范围重叠的分支在事务层仍然会串行
  （`resource_arbiter.py:263-284`）。编译器对这种情况给出提示。

### subflow

```python
flow.subflow(
    name,
    child: Flow,
    *,
    routes: Mapping[str, str | LoopTarget],
    inputs: Mapping[str, InputSource] | None = None,
)
```

- `routes` 必须覆盖子 Flow 的全部结局。"空 status 当成 completed"这类问题在编译期就
  会暴露。
- `inputs` 把父图的入参、循环轮次、常量绑定到子 Flow 的入参，只做来源绑定。
- ledger 在父子之间直接贯通（阶段 1 已经让所有子图的输入输出 schema 带上
  `AttemptLedgerState`）。

### 控制输出和未实现的原语

- `flow.control(step, name=path)`：把某一步输出模型上的字段发布成常驻的、有类型的
  状态字段。名字在整个挂载面上唯一。路径中间为 None 时省略该字段；叶子为 None 时
  仍写入。
- `flow.export_from` 和 `legacy()` 已删除。父图需要的少量值由 `flow.control` 发布。
- `map(over=..., body=..., key=...)`（运行时才知道个数的并行）：仓库里没有这种用法，
  原语未实现。

## 示例

### intake prepare

```python
prepare = Flow("prepare", input=PrepareInput, outcomes=("prepared", "failed"))
prepare.step("intake", intake, then="explore", on_failure="failed")
prepare.step("explore", explore, then="resolve-plan", on_failure="failed")
prepare.step("resolve-plan", resolve_plan, then="prepared", on_failure="failed")
prepare.control(
    "resolve-plan",
    plan_digest="plan.plan_digest",
    selected_test_families="plan.selected_test_families",
    preparation_refs="preparation_refs",
)
```

要成立，op 侧需要先改：

- explore 的 finalize 给 `exploration.json` 起名 `exploration`；resolve-plan 用同一个
  名字写回改写后的版本。ledger 按 key 后写覆盖，`publish_plan` 里换 digest 的代码
  （`calls.py:138-143`）就可以删掉。
- resolve-plan 的 handle 改成 `explore.artifact("exploration", ...)` 和
  `explore.artifact("inventory", ...)`。现在写死的 `intake.resolve.*`
  （`resolve_plan/hooks/artifacts.py:71-74`）没有任何步骤写。
- `requirement_digest`、policy 和资源 digest 挪进 resolve-plan 的 `run`
  （现在在 `calls.py:51-97`）。

### intake case

```python
case = Flow("case", input=CaseInput, outcomes=("reviewed", "rejected", "exhausted", "failed"))
with case.loop("review", budget="budgets.review_rounds", on_exhausted="exhausted") as review:
    case.step("case-design", case_design, then="case-review", on_failure="failed")
    case.step(
        "case-review", case_review,
        inputs={"review_round": review.round},
        route_on="public_outcome",
        routes={
            "pass": "reviewed",
            "reject": "rejected",
            "needs_human": "human-review",
            "needs_fix": review.next("case-repair"),
        },
        on_failure="exhausted",
    )
    case.step("case-repair", case_repair, then="case-review", on_failure="exhausted")
    case.gate(
        "human-review", decision=HumanReviewDecision,
        routes={
            "approve": "reviewed",
            "reject": "rejected",
            "request_rework": review.next("case-design"),
        },
    )
```

- 推进一轮之后，自动修复回 `case-repair`，人工要求返工回 `case-design`
  （`case.py:256-268`）。两个不同的 `next` 目标表达了这一点。
- `terminal_reviewed` 里的身份校验（`case.py:106-124`）挪进 case-review 的 finalize。
- `case_start` 里 `preparation_refs` 的回退选择（`case.py:85-102`）挪进 case-design
  的 prepare。

### generation：四个 family 并行

```python
def codegen_lane(family: str) -> Flow:
    lane = Flow(family, input=LaneInput, outcomes=("passed", "rejected", "exhausted", "failed"))
    with lane.loop("review", budget=3, on_exhausted="exhausted") as review:
        lane.step(
            "codegen", CODEGEN[family],
            inputs={"local_round": review.round},
            then="codegen-review", on_failure="failed",
        )
        lane.step(
            "codegen-review", CODEGEN_REVIEW[family],
            inputs={"local_round": review.round},
            route_on="route",
            routes={
                "codegen": "passed",
                "auto_fix": review.next("codegen"),
                "human": "human-review",
                "reject": "rejected",
            },
            on_failure="failed",
        )
        lane.gate(
            "human-review", decision=HumanReviewDecision,
            routes={"approve": "passed", "reject": "rejected", "request_rework": review.next("codegen")},
        )
    return lane


generation = Flow("generation", input=GenerationInput, outcomes=("passed", "failed"))
generation.step("resolve-inputs", resolve_inputs, then="families", on_failure="failed")
generation.parallel(
    "families",
    branches={family: codegen_lane(family) for family in GENERATION_FAMILIES},
    select="selected_test_families",
    require="passed",
    then="publish-cycle",
    on_failure="failed",
)
generation.step("publish-cycle", publish_cycle, then="passed", on_failure="failed")
```

- 预算 3 现在写死在 `route_families` 里（`factory.py:66`），建议改成入参
  `budgets` 里的一项。
- `PlanReview.route` 的类型是 `PlanReviewRoute | None`（`reviews.py:186`），要收紧成
  必填。review 输出里的 `rounds_used`、`rounds_budget`（`reviews.py:309-310`）删掉，
  否则会覆盖框架计数（`nodes.py:211-212`）。
- codegen 的写入要起名，并且名字带 family。publish-cycle 用 handle 读四个 family 的
  产出。`family_results`、`_family_result`、`plan_round_inbox`、`join_selected`、
  `complete_generation_node` 都会删掉。
- 四个 codegen 都写 `qa/tests`，router 读整个 `qa`（`ops/__init__.py:12`），所以现在
  仍然会排队执行。要真正并行，需要每个 family 写自己的目录。

### healing：审批带回文件引用

```python
repair = Flow("repair-failure", input=RepairInput, outcomes=("applied", "needs_review", "failed"))
repair.step("fix-proposal", fix_proposal, then="approval", on_failure="failed")
approval = repair.gate(
    "approval", decision=ProposalApprovalDecision, show=(FIX_PROPOSAL,),
    routes={"approve": "apply", "reject": "needs_review"},
    interrupt_id="fix-proposal-approval",
)
repair.step(
    "apply", apply_test_repair,
    inputs={"approval_ref": approval.field("approval_ref")},
    then="applied",
    on_failure={"permanent:invalid_output": "needs_review", "*": "failed"},
)
```

- `admit` 节点的资格和预算判断（`failure.py:67-76`）上移：资格由 quality 或 issue
  analysis 的结局决定，预算由 product 的 healing 循环管理。healing Flow 因此不再有
  `not_eligible` 和 `exhausted` 两个结局。
- 审批节点在 `approval_ref` 已存在时跳过中断（`nodes.py:269-272`），只有测试夹具会
  走到这条路。建议删除这种跳过，测试改用 `Command(resume=...)`。
- 提案文件起名，`proposal_ref` 由 ledger 提供，`publish_proposal` 里的重算哈希
  （`nodes.py:207-211`）删掉。

### product execute-tail 和 full

```python
tail = Flow(
    "execute-tail", input=TailInput,
    outcomes=("reported", "coverage_insufficient", "diagnostic", "needs_human", "blocked"),
)
tail.subflow("fact-baseline", quality.fact_baseline, routes={"done": "generation", "failed": "blocked"})
tail.subflow("generation", generation, routes={"passed": "execute", "failed": "blocked"})
tail.subflow("execute", execution.execute, routes={"committed": "quality", "failed": "blocked"})
with tail.loop("healing", budget="budgets.healing_rounds", on_exhausted="needs_human") as healing:
    tail.subflow(
        "quality", quality.assess,
        inputs={"repair_round": healing.round},
        routes={
            "satisfied": "report",
            "coverage_insufficient": "coverage_insufficient",
            "repairable_execution_failure": healing.next("repair"),
            "analysis_required": "issue-analyze",
            "needs_human": "needs_human",
            "blocked": "blocked",
            "failed": "blocked",
        },
    )
    tail.subflow(
        "issue-analyze", quality.issue_analyze,
        routes={
            "fix_eligible": healing.next("repair"),
            "report_issue": "diagnostic-report",
            "unclassified": "needs_human",
            "failed": "blocked",
        },
    )
    tail.subflow(
        "repair", healing_wheel.repair_failure,
        inputs={"repair_round": healing.round},
        routes={"applied": "rerun", "needs_review": "needs_human", "failed": "blocked"},
    )
    tail.subflow(
        "rerun", execution.rerun,
        inputs={"repair_round": healing.round},
        routes={"committed": "quality", "failed": "blocked"},
    )
tail.subflow("report", quality.report, inputs={"purpose": const("normal")},
             routes={"reported": "reported", "failed": "blocked"})
tail.subflow("diagnostic-report", quality.report, inputs={"purpose": const("diagnostic")},
             routes={"diagnostic": "issue-reconcile", "failed": "blocked"})
tail.subflow("issue-reconcile", quality.issue_reconcile,
             routes={"ready": "runtime-snapshot", "failed": "blocked"})
tail.step("runtime-snapshot", snapshot_runtime, then="retro", on_failure="blocked")
tail.subflow("retro", improvement.retro, routes={"done": "diagnostic", "failed": "blocked"})


full = Flow(
    "full", input=FullInput, outcomes=("achieved", "not_achieved"),
    public={"achieved": "completed", "not_achieved": "failed"},
)
full.subflow("surface", quality.surface_baseline,
             routes={"ready": "prepare", "not_ready": "not_achieved", "failed": "not_achieved"})
full.subflow("prepare", intake.prepare, routes={"prepared": "init", "failed": "not_achieved"})
full.subflow("init", generation_init, routes={"completed": "case", "failed": "not_achieved"})
with full.loop("coverage", budget="budgets.coverage_rounds", on_exhausted="not_achieved") as coverage:
    full.subflow(
        "case", intake.case, inputs={"coverage_epoch": coverage.round},
        routes={"reviewed": "tail", "rejected": "not_achieved",
                "exhausted": "not_achieved", "failed": "not_achieved"},
    )
    full.subflow(
        "tail", tail, inputs={"coverage_epoch": coverage.round},
        routes={
            "reported": "achieved",
            "coverage_insufficient": coverage.next("coverage-rework"),
            "diagnostic": "not_achieved",
            "needs_human": "not_achieved",
            "blocked": "not_achieved",
        },
    )
    full.step("coverage-rework", coverage_rework, then="case", on_failure="not_achieved")
```

上面是设计草图。真实实现在
`packages/products/assurance-product/assurance_product/graphs/`：
`execute_tail.py` 的 `build_execute_tail_flow`，`full.py` 的 `build_full_flow`。

草图和实现的差异：

- quality assess 的 `blocked` 去 `issue-analyze`。
- `report` 和 `diagnostic-report` 共用同一套结局路由：`reported`、`diagnostic`、
  `failed`。普通 report 把 `issue_analysis_ref` 绑成 `const(None)`。
- `runtime-snapshot` 是 improvement 的 subflow，结局 `done` / `failed`。
- `coverage-rework` 是 intake 的 subflow，结局 `done` / `failed`；入参绑 inspect
  写的 `CoverageReworkHandoffV1`、reviewed case 和 inspect 回执。task 打开这两份
  文件，写出 `qa/results/cases/case-rework-context.json`（`CaseReworkContextV1`）。
- 子 Flow 入参在挂载处显式绑定。full 的 case 绑 prepare 发布的 `plan_digest`、
  ledger 里的 `plan_ref` 和 surface 的 exploration refs；tail 再绑
  `reviewed_refs`、`source_artifacts`、`timeout_seconds`。

两个 healing 入口都用 `healing.next("repair")`，共享一个计数器和耗尽出口
`needs_human`。提案拒绝也去 `needs_human`。

覆盖轮次由 `coverage.next("coverage-rework")` 推进。intake 的 `coverage-rework`
task 读 inspect 的交接文件和 reviewed case，写出下一轮的 `CaseReworkContextV1`。
activation 带着轮次，同一轮不会推进两次。

## 覆盖审计

### 按 wheel 汇总

| wheel | 子图 | 现在就能直接写成 Flow | 需要 op 先改 | 用到的原语能力 |
|---|---|---|---|---|
| intake | prepare、case | prepare 的三步直线 | resolve-plan 的派生输入和 handle；case-review 改按 `public_outcome` 路由；`terminal_reviewed` 的校验进 finalize | loop 多个 `next` 目标；gate 目标用 `loop.next`；轮次作为输入 |
| generation | init、resolve-inputs（product 未挂载）、root、4 个 lane | init、publish-cycle | codegen 写入起名；`PlanReview.route` 收紧为必填；review 输出删掉轮次字段；`complete_generation` 校验进 publish-cycle | parallel；lane 循环；gate |
| execution | execute、rerun | 两个都是单步 | publish 里的拼装和哈希（`nodes.py:72-131`）进 handler，结果文件起名 | 无 |
| healing | repair_failure、repair_coverage（未挂载） | fix-proposal 到 apply 这一段 | 提案文件起名；apply 失败按 `invalid_output` 分流 | gate 带字段；`on_failure` 按失败种类分；资格和计数上移 |
| quality | assess、issue ×3、report、fact-baseline、surface | report、fact-baseline、surface、issue-reconcile | inspect 的 finalize 产出 `disposition`；issue-triage 和 issue-analysis 产出单一 `Literal`；surface 产出 readiness；report 的守卫进 finalize | 无 |
| improvement | 5 个单步、apply、retro | 5 个单步 | apply 的两个 task 产出顶层 `Literal`；retro 的 assemble 改成 task 并产出 `Literal` | gate 的动作作为输入；`parallel(select=None)` |
| product | 11 个 thin、intake、execute-tail、full | thin 入口、intake 入口 | 11 个业务 adapter 进 op；`advance_coverage` 的返工包变成 task；`snapshot-retro-runtime` 变成 task | subflow 覆盖子结局；两个跨子图循环；`public` 映射 |

所有子图里没有找到这套原语表达不了的控制流。剩下的差距都可以归到两类：op 输出里
缺一个可路由的 `Literal`，或者数据还没有变成具名文件。

### op 侧要补的路由字段

| op | 现在路由读的是什么 | 改成 |
|---|---|---|
| intake case-review | `decision`、两个布尔值和计数器（`case.py:219-228`） | `public_outcome`，已经存在于 `CaseReviewOutputV1` |
| generation `*.codegen-review` | 拷进 state 的 `route` 加计数器（`api.py:138-143`） | `route`，类型收紧为必填 |
| quality inspect | publish 里算出的 disposition（`nodes.py:379-406`） | finalize 产出 `disposition: InspectionDisposition` |
| quality issue-triage / issue-analysis | `classification` 加 `fix_eligible`（`issues.py:61-72`）。triage 的输出模型里没有这两个字段（`contracts/issues.py:452-466`） | 一个 `Literal`：fix_eligible / report_issue / unclassified |
| improvement apply-auto-review / apply-review | 嵌套的 `projection.state`，有 7 个取值没被覆盖，落到 otherwise（`delivery.py:82-87`） | 顶层 `Literal`，取值恰好等于路由分支 |
| improvement retro 的汇总 | 整数 `signal_count`（`retro.py:56-59`） | 新 task 输出 synthesize / empty |
| quality surface-baseline | product 用两个 source 加 family 列表判断（`full.py:74-101`） | task 接收 family 列表，输出 ready / not_ready |
| execution execute / rerun | product 校验 `execution_result` 的字段（`routes.py:16-33`） | 结局 committed / failed，身份校验进 op |
| healing coverage-repair | publish 改写后的 `status`，输出模型里没有这个字段（`coverage_repair.py:118-126`） | 输出模型加 `status`；这张图未挂载，也可以直接删 |

### 要迁出图层的业务逻辑

| 位置 | 内容 | 去处 |
|---|---|---|
| intake `prepare_start`、`select_resolve_plan`、`publish_plan` | 路径过滤、需求哈希、按路径找文件、换 digest | case-design prepare；resolve-plan `run`；具名写入 |
| intake `terminal_reviewed`、`case_start` | 身份校验、ref 回退 | case-review finalize；case-design prepare |
| generation `complete_generation_node`、`generation_done`、`publish_codegen` | 领域校验、重复校验、合成 `plan_files` 路径 | publish-cycle task；codegen finalize |
| execution `publish_execution` | 拼 `ExecutionCycleResultV1`、重算文件哈希 | run-tests handler |
| healing `publish_proposal`、`publish_applied_repair` | 重算哈希、硬编码 `status="applied"` | 具名写入；apply 输出 |
| quality `publish_inspect`、`select_report`、`select_reconcile_issues`、`clear_report_state` | 分类、摘要遍历、默认值填充、清空通道 | inspect finalize；report prepare；reconcile prepare；框架的失败局部化 |
| improvement `assemble_analyses`、`select_apply_memory` | 哈希、信号合并、拼回执 | 新 task；evaluate 输出文件 |
| product `validate`、`adapt_*`、`bind_issue_analysis`、`advance_coverage`、`snapshot-retro-runtime` | 输入校验、周期校验、修复授权、改写 generation、拼证据集、返工包、宿主回调 | Flow 输入模型；对应 op 的 hook；`coverage-rework` task；`snapshot-runtime` task（宿主回调走已有 runtime port） |

## 框架要补的能力

1. `TaskOp` 和 `TaskAttemptContract` 补上 `ledger_writes()` 和 `input_bindings()`；
   或者把剩下的 `TaskAttemptContract` 迁到 `router.task`。
2. handle 只能从生产方获得：`producer_op.artifact(name)`。capability 代码里禁止手写
   ledger key 字符串，同一个 op 里两个 handle 用同一个 slot 时报错（现在
   `handoff.py:152-190` 有多个 handle 共用 `plan_ref`）。
3. 累积型 ledger 条目：`Dir(path, name=..., accumulate=True)` 按路径合并，供
   `history_refs` 这类跨轮累积的目录使用。现在的后写覆盖会丢掉前几轮的文件。
4. 失败局部化：attempt 节点按自己的结果返回 `Command(goto)`，`attempt_failure` 只做
   诊断输出。
5. 框架控制通道：`outcome`、循环计数、分支结局、gate 决定。
6. 按 Flow 路径生成 activation。
7. 编译期检查：输入可解析、路由覆盖、ledger key 不冲突、slot 不重复、读取方的上游
   存在写入方。第一版只检查"上游某条路径上有写入方"，第二版再做支配关系检查：按子
   Flow 每个结局保证写出的文件集合，确认读取方的每条上游路径都写过。循环体内的读取
   只要满足支配关系，就一定读到本轮写的文件，所以不需要运行时清空状态。
8. `Flow.compile(context)`，产出 `CompiledStateGraph`，内部复用 `AttemptGraph` 和
   `add_attempt_node`。
9. importlinter 合约：capability 和 product 不得 import `langgraph`。

## op hook 收口

before / after 应该只放映射：拷字段、改名、组装 Agent 请求、把结果写成输出。对六个
wheel 的 `ops/`、`operations/`、`validators/`、`effects/` 做过一次审计（快照
`c1aa542`，约 26600 行，本节行号都按这个快照）。映射约占 26%，领域规则约占 52%，和
领域无关的通用逻辑约占 16%（约 4400 行）。通用逻辑里约 1100 行是框架已有能力的重写，
约 1400 行要等框架补上接口才能收掉，其余是只出现一次的工具代码，或者和领域规则缠在
一起的片段，留在 wheel。

### 框架已有、wheel 改调用即可

| 重复实现 | 改用 | 出现在 |
|---|---|---|
| 手写读文件、核 sha256、拒 symlink、查 `st_nlink` | `open_artifact` / `read_workspace_file`；依赖改声明成 `ArtifactHandle` | 六个 wheel。例：generation `codegen.py:76-91`、quality `agent_skills.py:39-113`、healing `application.py:88-107`、intake `obligations.py:247-255` |
| 再写一遍规范相对路径 | `is_canonical_relative` | 各 wheel 的 `validators/paths.py`、`contracts/agent.py` 等 |
| 旧式 `TaskHandler`：`validate_model` 加 `try/except` 转 `failed_input` | `router.task(...)`；要接 `OSError` 的用 `errors=` | quality 约 12 处；generation、execution、healing、improvement 若干 |
| after 里再读暂存文件、和 typed result 比较、再算 digest | `Agent(strict_files=True)` 加 `Out(path, model=..., format=...)`，读 `ctx.file` / `ctx.ref` | generation、quality 五个 Agent op、healing fix-proposal 和 coverage-repair、improvement 四个 retro op。现在只有 intake 打开了 `strict_files` |
| 手写 canonical JSON 落盘再算 digest | `stage_json_artifact` / `ctx.stage` | quality `assessment.py:510-516` 等；improvement `retro_persistence.py:78-85`；intake `resolve_plan/hooks/artifacts.py:176-192` |
| 暂存缺失时回退读项目树 | `Out(input_source="stage_first")` | generation `planning.py:648-665` |

替换 `TaskHandler` 时注意 `failed_input` 是可重试的，execution `runner.py:549-552`
现在不可重试，不能原样替换。canonical JSON 只替换字节不变的调用点，见下面的"digest
编码"。

### 框架要补的接口

| 接口 | 包 | 收掉的重复 |
|---|---|---|
| 按字段名核对身份：`ArtifactHandle(..., same=("change_id", "plan_digest"))`；`Finalize(same=...)` 比较业务输入和 Agent 结果的同名字段；`equals=` 比较字段和常量 | `agent_runtime_contracts` | 六个 wheel 约 450 行。例：quality `inspect/hooks.py:34-54` 逐个比较五个 digest 字段；intake `handoff.py:52-91` |
| `Finalize(errors=...)`，和 `Prepare.errors` 对称，并指定转成输入失败还是输出失败 | `agent_runtime_contracts` | intake 约 55 行，以及 quality、healing 里手写的 `try/except ValueError` |
| `Finalize(artifacts="auto")`：hook 返回后用 `ctx.refs()` 填 `artifacts` | `agent_runtime_contracts` | intake 五个 after，以及输出模型上"artifacts 排序且唯一"的校验 |
| `Agent(receipt_field=...)`：`capture` 核对回执时读的字段名，默认 `output_files` | `agent_runtime_contracts` | quality report 的 `report_files` |
| `under_root(path, roots)`：公开现在私有的 `_claimed` | `graph_engine.artifacts` | intake `allowed_by_lock`；quality、healing、improvement 的 `validators/paths.py`；generation `contracts/agent.py` |
| `merge_refs_by_path(..., on_conflict="error")`，默认仍是后写覆盖 | `graph_engine.stategraph.ledger` | intake `history_refs.py`；execution `generated_merge.py:142-152`；quality `assessment.py:599-604` |
| `stage_bytes(workspace, relative, data) -> ArtifactRef`，和 `stage_json_artifact` 并列 | `graph_engine.artifacts` | generation、execution 里手写的 mkdir、写入、sha256 |
| `walk_regular_files(...)`：带文件数和字节上限的目录扫描，拒 symlink | `graph_engine.artifacts` | execution `agent_skills.py:277-350`、`execution_view.py:465-500` |
| `check_sealed_documents(...)`：提交校验器的前半段（路径合法、必需文件在写集里、digest 对上） | `graph_engine`，放在 `run_validators` 旁边 | improvement 四个 validator、healing 三个、quality `validators/*`、intake `validators.py:13-94` |
| `apply_idempotent_effect` / `reconcile_idempotent_effect` | `graph_engine.effects` | healing 和 improvement 的两份 `effects/common.py`，各 154 行，只差首行注释 |

字段名和常量都由 wheel 以字符串传入。需要同时看到业务输入和 Agent 结果的接口放
`agent_runtime_contracts`；只涉及文件、digest、effect、validator 的放 `graph_engine`。
`same=` 只比较顶层字段，`qa.change.change_id` 这种嵌套字段和
`coverage_epoch == previous + 1` 这种规则留在 wheel。

### wheel 内部收口

- generation 的四个 codegen hook 之间只差 `FAMILY`，四个 codegen-review hook 也一样。
  在 wheel 里用一个按 family 参数化的工厂生成这八个 op。
- intake case-review 和 case-repair 的 auto-fix 校验合成一套。
- case YAML 定位器（generation `selected_cases.py:52-75`、execution
  `agent_skills.py:172-235`）在两个 wheel 之间共享。

### 留在 wheel 的

family 集合运算、覆盖矩阵和 MRC 不变量、修复安全旗标、AST 断言保护、复盘信号和候选
规则、问题指纹的字段组合、知识库合并、报告正文。它们用到集合、路径和 digest，规则本
身只在 assurance 领域成立。

### digest 编码

现有 digest 至少有四种写法：`canonical_digest`（无前缀、无尾换行）；
`stage_json_artifact`（带尾换行）；quality `identity.py` 和 improvement
`contracts/delivery.py:19-32`（尾换行加 `sha256:` 前缀）；`indent=2` 的 JSON（quality
`obligations.py:473-486`、execution `runner.py:334-345`）。intake `planning_facts.py:272`
用的 `json.dumps(sort_keys=True)` 在 `ensure_ascii` 上也和 `canonical_digest` 不同。
替换会改变已经存进账本和证据里的 digest，所以 F2、F3 不动这些调用点，统一编码另外
立项迁移。

### 落地结果

hook 收口在 F3 之后单独做完。规则是生产调用点不到两处的接口不加。

| 接口 | 生产调用点 |
|---|---|
| `ArtifactHandle(same=)` | 7 |
| `Finalize(same=)` | 8 |
| `Finalize(errors=, error_failure=)` | 3 |
| `Finalize(artifacts="auto")` | 5 |
| `under_root` | 3 |
| `merge_refs_by_path(on_conflict="error")` | 2 |
| `apply_idempotent_effect` / `reconcile_idempotent_effect` | 6，两份 `effects/common.py` 已删 |

没有加的接口：

- `Agent(receipt_field=)` 和 `Finalize(equals=)` 各只有一处。
- `check_sealed_documents` 的调用方是下面删掉的 validator。
- `walk_regular_files`：execution 的两处都还要过滤、记 mode、改写路径。
- `stage_bytes`：wheel 里的原始字节写入都允许用不同字节覆盖。

收口时发现，插件登记的 139 个 task handler 里有 67 个没有被任何 attempt 契约引用，全部删掉，只有它们在用的代码也一并删除。登记的 18 个 commit validator 里有 17 个从未被契约的 `validators` 引用，一起删掉。`tests/architecture/test_registered_handlers_are_referenced.py` 要求登记的 handler 和 validator 都有契约引用。

现存问题第 9、10 条已修。

没有收完的：

- `strict_files` 只打开了 intake 5 个、quality 3 个、improvement 4 个 Agent op。其余 op 的 `artifact_paths` 默认是空元组，或者现在没写出文件也能成功，打开会把这些成功变成失败。
- 旧式 `TaskHandler` 还剩 22 个：generation 8、execution 1、quality 3、improvement 10。换成 `TaskOp` 会改变已发布契约。
- generation `planning.py` 的暂存回退在 `PlanFinalizeHandler` 里，没有对应 op，没有改成 `stage_first`。

## 审计中发现的现存问题

这些问题和 Flow 无关，建议单独处理：

1. **issue 系列 thin 入口失败时发布成 `completed`。** 已修。`failed` 路由到 `failed`
   （`entrypoints.py` 的 `_ISSUE_ROUTES`）。
   `test_issue_review_thin_entry_publishes_failed_when_the_attempt_fails` 覆盖
   issue-review；issue-analyze 用同一套路由。
2. **healing 的 `needs-human` 分支走不到。** 已修。两个入口都用
   `healing.next("repair")`，耗尽和提案拒绝都去 `needs_human`
   （`execute_tail.py`，`test_execute_tail_flow.py`）。
3. **`rounds_budget` 一个字段装两种预算。** 已修。手写 `ProductState` 已删。循环计数
   在 `flow_control.loops`，按循环路径区分。
4. **healing 预算在两处检查，耗尽后去向不同。** 已修。预算只由 `flow.loop` 检查，两
   个入口共享计数器和 `on_exhausted="needs_human"`。
5. **`attempt_failure` 成功后不清除。** 已修。step 按这次 attempt 的结果返回
   `Command(goto=...)`，路由读本次结果里的 `attempt_failure`
   （`graph_engine/flow/compile.py`）。
6. **未挂载的图。** 已修（已定决策第 2 条）。`HealingGraphs` 只剩 `repair_failure`；
   `GenerationGraphs` 只剩 `generation` 和 `init_runtime`。没有图绑定的 healing
   `coverage-repair` Agent 契约和它的 op、schema、skill 一起删除。
   `test_feature_graph_bundles.py` 要求每个 Agent 契约都被某张图绑定。
7. **`issue_analysis` 字段被 quality 和 retro 共用。** 已修。手写 `ProductState` 已
   删。quality 和 retro 各自用自己的 ref。普通 report 把 `issue_analysis_ref` 绑成
   `const(None)`。
8. **resolve-plan 的 handle 从未自动绑定。** 已修。explore 写出 `exploration` 和
   `inventory`；resolve-plan 是 `router.task`，`depends=RESOLVE_DEPENDS` 经
   `input_bindings()` 绑定 `intake.exploration` / `intake.inventory`。
9. **graph_engine 写死了领域的 effect kind。** 已修（见"op hook 收口 / 落地结果"）。
   `graph_engine/effects/contracts.py:7-14`
   的 `EXPECTED_EFFECT_KINDS` 列了 healing 和 improvement 的 6 个 kind，
   `attempts/kernel.py:728` 拒绝不在列表里的 kind。import-linter 只查 import，查不出
   字符串常量。Kernel 应该只认插件声明的 `effects.entries`。F1 门禁通过后修。
10. **healing finalize 的 prepare 锁按产品调用格式必然失败。** 已修（见"op hook 收口 /
    落地结果"）。`ops/coverage_repair/hooks.py:15-24`、`ops/fix_proposal/hooks.py:34-37`、
    `operations/agent.py:47-49` 要求 `ctx.prepared["prepare"]` 存在。产品的
    `runtime_bindings.py:587-620` 发给 finalize 的是只有 `prepare` 和 `agent_result` 两
    个键的信封，router 拆开后（`router.py:985-990`）没有 `prepare` 键。按这个格式调用
    coverage-repair finalize，结果是可重试的
    `invalid_input: finalize digests do not match the locked prepare payload`；测试用的
    是平铺格式，所以一直通过。router 已经把 prepare 阶段的业务输入当作 finalize 的业务
    输入，这把锁比较的是同一份数据，倾向直接删掉，并补一条按产品格式调用的测试。F1
    门禁通过后修。

## 迁移顺序

每个阶段结束都跑完整门禁：ruff、format、pyright、lint-imports、
`build_wheels --check`、全量 pytest、三个 smoke 脚本。

| 阶段 | 内容 |
|---|---|
| F0 | 在 graph_engine 里实现 Flow 编译器、检查器和上面"框架要补的能力"的第 1 到 6 条，用玩具 op 写测试。`legacy()` 适配器一起做，保证新旧图可以混用。 |
| F1 | intake 试点：prepare 和 case 用 Flow 重写；保持 semantic id 不变；用 `export_from` 继续给父图导出过渡字段。 |
| F2 | 直线型子图：improvement 的 5 个单步、quality 的 issue / report / fact-baseline / surface、execution 的 2 个、generation init。同时给这些 op 补上路由 `Literal` 和具名写入。开头先补"op hook 收口"里框架要补的全部接口；然后 intake 和本阶段涉及的 op 按"框架已有"和"要补的接口"两张表清理 hook，打开 `strict_files`；两份 `effects/common.py` 一起换成框架的 effect 循环。 |
| F3 | 有循环、gate、并行的子图：generation（parallel 加 lane 循环）、retro（并行分析加汇总 task）、improvement apply（gate 动作作为输入）、healing（gate 带字段，资格和计数上移）、quality assess（disposition 进 finalize）。本阶段涉及的 op 同样清理 hook；generation 的八个 codegen op 改用工厂生成。 |
| F4 | product：用 subflow 组装、healing 和 coverage 两个循环、`public` 映射；删掉所有 adapter；手写 `ProductState` 和 product `graphs/state.py` 整个删除；纯度守卫改成硬性检查；importlinter 禁止 import langgraph。 |

## F4 落地结果

产品入口、状态和父子传值已经按 Flow 收完。

**产品入口**

产品只挂 7 个根：`full`、`intake`、`init`、`retro`、`issue-review`、`issue-analyze`、
`issue-reconcile`（`assurance_product/models.py` 的 `PRODUCT_ENTRYPOINTS`，
`graphs/entrypoints.py` 的 `thin_root_flows`，`graphs/factory.py` 的
`product_root_flows` / `declared_root_flows`）。全部是 Flow 编译出来的。手写父图和
adapter 已删除。

6 个 improvement 交付入口 `archive`、`improvement-review`、`improvement-evaluate`、
`improvement-export`、`improvement-apply`、`improvement-rollback` 不再挂载。
`aa start` 只按 `ProductInputV1`（`extra=forbid`）校验入参，这些入口的子 Flow 需要
的字段只能由调用方提供；apply 的第一步 `apply-auto-review` 还要读 `assessment` 和
`current`。improvement capability 自己的 Flow、op、契约和测试保留。

根入参就是入口模型（`_entrypoint_model`）。`_covering_input` 已删。子 Flow 的字段
在挂载处显式绑定，例如 case 绑定 prepare 发布的 `plan_digest` 和 ledger 里的
`plan_ref`。

契约数：26 个 Agent 契约、47 个 Attempt 契约（Agent 加 Task；
`scripts/assurance_product_wheel_smoke_test.sh`，
`tests/product/test_wheel_smoke_contract.py`）。

**状态和入口契约**

手写 `ProductState` 已删除。Flow 状态只有入参、控制字段和 ledger。
`STATE_SCHEMA_VERSION = "6"`。

`graph_engine.flow.root_schemas(flow, *, outcome_field=None)`
（`graph_engine/flow/check.py`）同时供根编译和入口契约使用。入口契约 digest
（`assurance_product/graphs/revisions.py` 的 `contract_for_root`）：输入是根入参
模型的 JSON schema；输出是 `ProductPublicOutput` 的 JSON schema；状态按字段取去掉
reducer 后类型的 `TypeAdapter(...).json_schema()`，加上 reducer 的
`module.qualname`。

`factory.entrypoint_contracts()` 带缓存，基于 `declared_root_flows()`。
`build_product_graphs` 的契约来自它实际编译的 Flow。
`test_declared_and_compiled_production_contracts_match` 保证两者相等。

**父子之间的数据传递**

文档类入参改成 ref，由 op 的 prepare 通过生产方 handle 打开。task digest 和
attempt key 因此变过一次。

ledger 回执：ledger 在同一次更新里把提交回执 `ReceiptRef` 记在同一个 key 上（新值
覆盖旧值）。入参来源 `ledger_receipt(handle)` 把回执绑定到入参。文件本身不再内嵌
自己的回执。

控制输出 `flow.control`：常驻的、有类型的少量值。白名单是 prepare 的
`plan_digest`、`selected_test_families`、`preparation_refs`，以及 case 的
`reviewed_refs`（`reviewed_case.preparation_refs`）。名字在整个挂载面上唯一。路径
中间为 None 时省略该字段；叶子为 None 时仍写入。

反向交接按 import 顺序（intake < generation < execution < healing < quality <
improvement）：生产方按消费方拥有的契约模型写文件，product 用
`ledger(生产方 handle)` 绑定 ref。实例：`AppliedRepairHandoffV1`（execution 契约，
healing apply → execution rerun）、`CoverageReworkHandoffV1`（intake 契约，quality
inspect → intake coverage-rework）、`IssueAnalysisHandoffV1`（healing 契约，
issue-analyze → fix-proposal，带 `coverage_epoch`，其他轮次的视为不存在）。

execute 和 rerun 共用 ledger key `execution.cycle`（新值覆盖旧值）。

轮次路径：materialize-assessment-inputs 的写入路径带
`epochs/{coverage_epoch}/rounds/{repair_round}`；`batch_id` 仍是内容 digest
（execution `operations/agent_skills.py` 的 `canonical_digest`），quality 在
prepare 里从 execution cycle 文件读出并校验；`repair_round` 绑定 `healing.round`。

执行证据路径常量：execution `contracts/workflow.py` 的 `EXECUTION_EVIDENCE_PATHS`
（`execution.execute` ↔ `qa/results/execution/execute-result.json`，
`execution.run` ↔ `run-result.json`）。`seal_execution`、runner、attempts、
`paths.resolve_canonical_evidence` 和 product status 共用。

**status**

`aa status` 从 `artifact_ledger` 和 `open_artifact` 读取
（`render_status_from_langgraph` 的 `project_root` 为必填）。当前覆盖轮次读 Flow
循环的控制字段 `flow_control.loops.coverage`；轮次不同的 execution / quality /
coverage 值视为不存在。quality gate 使用算好的 execution gate。

普通 report 把 `issue_analysis_ref` 绑成 `const(None)`。模型拒绝 purpose 为
normal 且带 issue 引用的 report。

**框架和边界**

`graph_engine.flow.CompiledFlow` 是 LangGraph `CompiledStateGraph` 的别名，
`Flow.compile` / `BoundFlow.compile` 返回它。

import-linter 契约 `capability-product-no-langgraph`：capability 和 product 禁止
import langgraph，唯一例外 `assurance_product.sqlite_checkpointer`
（`.importlinter`）。

图纯度守卫是硬性检查，豁免清单已删（`tests/architecture/test_graph_purity.py`）。

**CLI**

`aa run` 在进入工作区之前拒绝软链接的 `--project-dir`（退出码 40）：
`assurance_product/cli.py` 的 `_project_for_run` 先调用 `require_real_directory`
（`change_workspace.py`）。

没有收完的：

- 重新开放 6 个 improvement 入口需要单独设计：子 Flow 要能从工作区文件读取 retro
  结果和投影。
- 历史文档仍提到已移除的入口，作为历史记录保留：
  `docs/specs/2026-08-28-feature-workflow-module-architecture.md`，
  `docs/superpowers/specs/2026-08-31-langgraph-assurance-boot-runtime-design.md`，
  `docs/superpowers/specs/2026-08-31-python-native-langgraph-assurance-design.md`，
  `docs/superpowers/plans/2026-08-28-feature-workflow-modularization.md`，
  `docs/superpowers/plans/2026-08-31-assurance-baseline-and-agent-leaf-tracer.md`，
  `docs/superpowers/plans/2026-08-31-feature-stategraph-migration.md`，
  `docs/superpowers/plans/2026-08-31-langgraph-product-cutover.md`，
  `docs/superpowers/plans/2026-08-31-python-native-langgraph-migration.md`，
  `docs/superpowers/plans/2026-08-31-semantic-attempt-kernel.md`，
  `docs/superpowers/plans/2026-09-01-agent-artifact-contract-migration.md`，
  `docs/superpowers/plans/2026-09-01-structured-artifact-pipeline.md`。
- `map` 原语未实现。
- `bootstrap/driver.py` 和 `operator.py` 的项目目录处理没有加软链接检查。driver
  在 `task_directory` 分支用 `selected_task.resolve()`（`driver.py:315`）；operator
  的 `start` 用 `project_dir.resolve()`（`operator.py:280`）。driver 可能收到
  macOS 下 `/tmp` 这类系统软链接路径。
- digest 编码统一见"digest 编码"一节。hook 收口遗留见"op hook 收口 / 落地结果"。

## 兼容性和代价

- **attempt key 会变。** activation 改成框架生成，codegen、execution、assess、issue
  analysis 这些现在用组合 trigger 的步骤 key 都会变；输入契约变化的 op（比如
  resolve-plan）也一样。需要一次性更新 golden，部署前未完成的运行不能跨版本续跑。
- **semantic id 尽量不变。** 按"ledger namespace + Flow 内路径"的规则，现有 id 都能
  保留。
- **interrupt id 换成挂载路径。** 产品不保留旧值（已定决策第 4 条）。Flow payload
  的 `interrupt_id` 挂到 `full` 上是 `full.human-review`、`full.approval`、
  `full.api.human-review`。`aa resume` 按 LangGraph snapshot 的 `item.id` 匹配
  （`application.py` 的 `_pending_graph_interrupt_ids`）。
- **契约 digest 会变。** 凡是输出模型新增路由 `Literal`、或输入模型删掉派生字段的 op
  都会变，`tests/product/goldens/contract-digests.json` 要随之更新。
- **并行不等于更快。** 资源仲裁按读写范围排队，generation 和 retro 的分支要等写范围
  拆开后才能真正同时执行。
- **`revisions.py` 保留。** 其中的递归上限是运行时的保护上限，和业务预算无关。

## 已定决策

1. generation 选择哪些 family，来源是入参字段 `selected_test_families`。
2. "审计中发现的现存问题"第 6 条列出的未挂载图直接删除。
3. 两个 healing 入口耗尽时统一去 `needs_human`。
4. attempt key 和 interrupt id 一次性换成新值，迁移期不保留旧的 interrupt id。
5. case review 判 `needs_fix` 且要求人工复核时，人工 approve 后结局是 `reviewed`。
   case-review 的 finalize 对所有决定都会产出 `reviewed_case`。`reviewed_refs` 经
   `flow.control` 发布。
6. 失败路由进入 gate 时，interrupt payload 带上失败步骤的 semantic id、失败种类和
   失败信息。从成功路由进入时不带。
7. Flow 框架通道统一用 `flow_` 前缀（`flow_control`、`flow_outcome`），避免和业务
   字段重名：improvement apply 的入参 `ApplyFlowInput` 有一个表示评测结果的
   `outcome`。
8. 产品根全部由 Flow 编译。`public` 把结局映射到 `status`。根 Flow 的输出不回显入参。
9. generation 的 family 选择和 `plan_digest` 通过 prepare 的 `flow.control` 发布。
   第 1 条继续成立。
10. 文档类入参改成 ref，在 op prepare 里经生产方 handle 读取。
11. 删除手写 `ProductState`，入口契约 digest 取编译后的根 schema，状态版本升到 6。
12. status 的新鲜度按 Flow 循环轮次判断。
13. issue-analyze 给 repair 的引用走带 `coverage_epoch` 的交接文件；普通 report 把
    `issue_analysis_ref` 绑成 `const(None)`。
14. 回执记在 ledger 上（`ledger_receipt`）。
15. quality 的输入路径按轮次分目录，`batch_id` 保持内容 digest。
16. retro 需要的 preparation refs 走控制输出。`retro_id` 仍由 window、source refs
    和 report receipt digest 派生。
17. execution 的 semantic node 由证据路径常量推导。cycle 仍写
    `qa/results/execution/execution-cycle.json`。
18. execute 和 rerun 共用一个 ledger key。
19. 反向交接由生产方按消费方的契约模型写文件。
20. 6 个 improvement 交付入口不再挂载，capability 内部保留。
21. `aa run` 拒绝软链接项目目录。
22. 删除没有图绑定的 healing `coverage-repair` Agent 契约。覆盖不足由 full 的 coverage
    循环回到 case design 处理。
