# Graph 数据面摄入与控制流语法糖设计

- 日期：2026-07-24
- 状态：待评审
- 范围：`assurance_agent/workflow/graph/`、`assurance_agent/workflow/orchestration/`、`assurance_agent/artifacts/`、`assurance_agent/_resources/schemas/`

## 1. 背景与问题

当前引擎是刻意的「编排层 + 产物层」分离：

- **Layer A（graph 编排状态）**：`events.jsonl` ledger → `GraphProjection`，管 task 生命周期、gate verdict、`current_tree_id`、budget、interrupt。节点之间不传业务数据——`TaskResult` 只带 `write_set_id` / `outputs_sha256`。
- **Layer B（artifact 文件状态）**：业务真相全在 `qa/changes/<id>/` 下的文件里。skill 写文件，下游 skill 与 gate 从盘上重新读文件。

由此产生三个已确认的痛点：

1. **契约隐式**：skill 输入输出是路径 glob，散落在 `execution-contracts.yaml`、`workflow-schema.yaml`、各 SKILL.md 三处；graph 编译期看不出数据如何流动。
2. **路由盲**：gate 裁决靠 `_load_view_doc` 从盘上裸 `json.loads` 再对 dict 求 DSL，完全不经过 registry 的 pydantic 模型；「这个 JSON 长什么样」有两套认知（registry 模型管写入校验，gate DSL 里手工堆 `defined(...)` / `== null` / `missing_field_is` 防御）。
3. **双层维护成本**：`reads_sha256` 防漂移、audited view 重 hash、import-checkpoint gate 重求值等机制都在给「gate 二次读盘」补一致性。

另一个独立问题是**控制流语法的人机工学**：nodes / edges / routes 三段分离导致同一条件写多处（节点 `when` 与入边 `when` 重复）、读一条流程要跨 section 跳转、一个 review-fix 循环需要 4 个节点 + budget + 回边 + route 约 55 行样板。

对照 Dagu（`/Users/lvqingquan/golandProject/dagu`）调研结论：其数据面（`output:` 变量经引擎流动、下游插值引用、sub-DAG 输出冒泡、human task 表单值成为节点输出）优于本引擎；其控制流条件语言（shell exit + 正则）、循环能力（无多节点环）、持久化（全量快照 JSONL）弱于本引擎。故本设计**只吸收其数据面思想与「编译期脱糖」技巧**，不改变现有运行时模型。

## 2. 目标与非目标

### 目标

- G1 业务语义的路由证据在 skill 任务收尾时**一次性摄入**为类型化值并冻结进 ledger；此后 gate / route / edge 条件不再回盘读文件。
- G2 skill 的输入输出契约收敛到**单一 manifest**，`execution-contracts.yaml` / `workflow-schema.yaml` / SKILL.md 引用符号名而非重复路径 glob。
- G3 子图向父图**冒泡声明的输出**；人机 interrupt 的 resume 可携带结构化 payload 并成为节点输出。
- G4 控制流表面语法通过**编译期糖**降低样板：线性链、节点 `when` 单一来源、`cycle:` 循环结构。脱糖结果与现行底层形式逐字节等价。

### 非目标

- 不把 plan 等文档型产物结构化为 pydantic 对象（「方案二」已否决：plan 本质是给下游 agent 读的 markdown 推理文档，强行建模丢表达力且与 agent 文件系统 I/O 模型冲突）。
- 不替换引擎为 Dagu，不引入其字符串/正则条件语言。
- 不改变 tree store / write-set 隔离 / ledger event-sourcing / 确定性重放架构——它们继续负责「文件的版本化传输」。
- 文件产物照常写盘：codegen 等下游 agent 仍读 `plans/*.md` 原文；摄入值是文件的冻结登记，不是替代品。

## 3. 设计 A：数据面——artifact 摄入与节点输出值

### A1 契约收敛：artifact manifest

新增单一 manifest（建议 `assurance_agent/_resources/schemas/artifact-manifest.yaml`），每个产物一条：

```yaml
artifacts:
  api_plan_review:
    path: change:review/api-plan-review.json
    schema: Review            # artifacts.models 中的 pydantic 模型名
    producer: skill:aa-api-plan-reviewer
    ingest: true              # 收尾时解析并冻结进 ledger
  api_plan:
    path: change:plans/api-plan.md
    schema: null              # 文档型，只校验存在性，不摄入
    producer: skill:aa-api-plan
```

- `artifacts/registry.py` 的 glob→model 映射改由 manifest 生成（或校验二者一致），消除双份声明。
- `execution-contracts.yaml` 的 `reads` / `writes` / `outputs` 与 `workflow-schema.yaml` 的 node `outputs` 允许写符号名（`artifact:api_plan_review`），编译期展开为路径；过渡期路径写法继续有效。
- SKILL.md 的 Inputs/Outputs 段由 manifest 生成或 CI 校验一致（复用现有 skill 文档校验管线）。
- 编译期即可导出完整数据流图：谁产出哪个符号、谁消费它（`compiler.py` 现有 `_collect_artifact_symbols` 的「文件名 stem 派生」升级为 manifest 驱动，消除 stem 冲突的隐式规则）。

### A2 finalize 摄入（改动最小的杠杆点）

`finalize.py::_validate_registry_outputs` 现在已经解析 + pydantic 校验了 `must_compat` 产物，然后**把解析结果丢弃**。改为校验通过后返回结构化值，由 `finalize_task_result` 写入 `TaskResult.value`（字段已存在）：

```python
# TaskResult.value 载荷形态（按 manifest 符号名组织）
value = {
    "outputs": {
        "api_plan_review": {  # 摄入的完整解析值或 routing 字段投影
            "decision": "pass",
            "codegen_readiness": "ready",
            "required_capabilities": [...],
            ...
        }
    }
}
```

- ledger 侧无 schema 变更：`task_succeeded` 事件与 `TaskProjection.value` 本就携带 `value`。
- 摄入值大小护栏：单符号摄入值序列化后上限 64KB（对齐 Dagu 的 maxOutputSize 思路），超限则任务以 `invalid_output` 失败——路由证据本就应是小 JSON，plan 文档不在摄入范围。
- interrupt / import-checkpoint 语义不变：摄入值随 task 结局冻结，重放即恢复。

### A3 gate 与 route 只读摄入值

1. **attached gate**（`finalize.py::_attach_gate_report` → `gates.py::check_gate_in_view`）：gate `reads` 中凡是 manifest 声明 `ingest: true` 的条目，求值改用本 task 刚摄入的值（同一 workspace、同一瞬间，语义等价），不再 `_load_view_doc` 读盘。非摄入条目（如 `repo:.aa/data-knowledge.yaml`）过渡期保留读盘。
2. **DSL scope**（`planner.py::_build_scope`）：`node('id')` payload 增加 `outputs` 键，暴露该节点冻结的摄入值；route `select` 可写 `node('review').outputs.api_plan_review.decision`。现有 artifact symbol（从 `current_tree_id` 读 tree）保留作过渡，目标态是条件表达式全部走 `node().outputs` 与 `state`。
3. **随之简化**：gate 对摄入条目的 `reads_sha256` 防漂移、interrupt resume 的 audited view 重 hash、import-checkpoint 的 gate 重求值，在证据已冻结进 ledger 后逐步退役（保留 audit 记录字段，去掉运行时重读）。
4. gate 规则里的 `defined(...)` / `== null` 防御性写法可在 schema 校验前移后删减——摄入值已过 pydantic，字段缺失在 finalize 阶段就变成可重试的 `invalid_output`，不再流到 gate 才 fail closed。

### A4 输入显式注入

- planner 物化 task workspace 时，`change:` 域只放入该节点 contract `reads` 声明的 artifact（tree store 选子集物化；`repo:` 域不受限）。
- `agent_api.py` 的 prompt 契约段列出确切输入文件清单，替代「工作区里有什么读什么」。
- 收益：skill 行为不再受工作区无关文件影响；contract `reads` 从文档性声明变成强制边界。

### A5 子图输出冒泡

子图（`graph:api-branch` 等）当前对父图完全不透明。增加：

```yaml
api-branch:
  exports:
    api_plan_review: node('review-cycle').outputs.api_plan_review
```

subgraph handler 在子图终局后按 `exports` 求值并写入父 task 的 `TaskResult.value.outputs`，父图 DSL 以同一 `node('api').outputs.*` 形式引用。对齐 Dagu 的 sub-DAG outputs 汇总，但保持显式声明（不做全量自动冒泡，避免父图耦合子图内部结构）。

### A6 人机输入结构化

`builtin:interrupt` 的 resume 命令当前只带 `resume.action` 字符串。扩展：

- interrupt 声明可选 `form`（JSON Schema 片段）；
- resume 命令携带 `payload`，经 form 校验后写入该节点 `TaskResult.value.outputs.human_input`；
- DSL 里 `resume.action` 保持不变，新增 `node('human-review').outputs.human_input.*` 可供下游引用。

对齐 Dagu human task 的「人的输入也是节点输出」模型，统一数据面。

## 4. 设计 B：语法面——三个编译期糖

原则（借 Dagu router 的实现技巧）：**表面语法在 schema 加载期脱糖为现行底层形式**（nodes/edges/routes/budget），`compiler.py` 之后的 planner / scheduler / ledger 零改动；`aa workflow explain`（或等价命令）可打印脱糖结果供审查。

### B1 线性链推断

```yaml
# 糖
chain: [START, explore, case-design]
# 脱糖
edges:
  - {from: START, to: explore}
  - {from: explore, to: case-design}
```

仅允许无守卫直链；与显式 `edges` 可混用，重复边编译期报错。

### B2 节点 `when` 单一来源

现状：`run_mode` 条件在节点 `when` 与其入边 `when` 重复（如 intake-workflow 的 explore/case-design）。规则改为：

- 节点声明 `when` 后，编译器自动为其每条入边合成守卫 `edge.when AND node.when`；
- 手写入边守卫若与节点 `when` 语义重复，编译期 lint 告警；
- skip 语义不变：`when` 为假记 `node_skipped`，不遍历出边。

迁移：现有 schema 中重复守卫逐个删除入边侧副本，行为经 `test_dsl_schema_corpus` 快照对比验证等价。

### B3 `cycle:` 循环结构

```yaml
# 糖（替代现行 review/fix/human-review/exhausted 四节点 + budget + 回边 + route）
cycles:
  case-review:
    body:
      review: {uses: skill:aa-case-reviewer, agent: aa-reviewer,
               outputs: [artifact:case_review], gate: case-review-gate}
      fix:    {uses: skill:aa-case-fixer, agent: aa-doc-author,
               outputs: [change:review/case-review-apply-summary.md]}
    entry: review
    dispatch:                       # 按 review 的 gate verdict 分派
      pass: EXIT
      needs_fix: fix
      needs_human_review: HUMAN     # 自动生成 builtin:interrupt 节点
      reject: STOP
      stop: STOP
    human_actions: {fix_and_proceed: fix, accept_risk: EXIT, stop: STOP}
    max_attempts: "params.max_case_fix_attempts"
    on_exhausted: stop("case fix attempts exhausted")
```

脱糖产物与现行 `case-review-cycle`（workflow-schema.yaml L193-251）逐字节等价：body 节点 + `fix → review` 回边 + budget（`consume` / `exhausted_to`）+ interrupt 节点 + exhausted 节点 + 两组 route + default STOP。安全语义（fail-closed default、budget 记账、interrupt audited bind）由脱糖模板固化，不再依赖每处手写。

## 5. 分阶段实施

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| P1 | A1 manifest + registry/contract 双向校验；符号名引用编译期展开 | manifest 单测；`aa validate` 对三处契约一致性校验通过；现有 schema corpus 测试不变 |
| P2 | A2 finalize 摄入 + A3.2 `node().outputs` 进 DSL scope | `test_finalize_review_validation` 扩展断言摄入值；planner 单测覆盖 `node().outputs` 路由；一次 benchmark 全流程跑通且 route 决策与旧版一致 |
| P3 | A3.1/3.3 gate 读摄入值、退役摄入条目的二次读盘与重 hash | `test_gates` 双跑（读盘 vs 摄入）verdict 等价；import-checkpoint 测试更新 |
| P4 | A5 子图 exports + A6 interrupt payload | 子图冒泡单测；resume payload 端到端测试 |
| P5 | B1/B2/B3 语法糖（纯编译期，可与 P2-P4 并行） | 脱糖快照测试：糖写法编译产物与手写底层形式逐字节相等；现行 schema 迁移后 `test_dsl_schema_corpus` 全绿 |
| P6 | A4 输入显式注入（行为收紧，放最后） | agent workspace 物化白名单测试；benchmark 回归确认无 skill 因缺输入失败 |

每阶段独立可合并、可回滚；P2 落地即解决「路由盲」，P1+P2 合计解决三个痛点中的两个。

## 6. 风险与兼容性

- **摄入值与文件漂移**：摄入发生在 freeze 同一事务窗口内，之后文件即使被后续节点改写，路由证据仍以冻结值为准——这是特性不是缺陷（对齐「route 只读冻结结局」的既有原则）；人工审查仍看文件。
- **过渡期双轨**：P2-P3 期间 gate 可能部分读摄入值、部分读盘；以 manifest `ingest` 标记为准，双跑测试保证 verdict 等价后才切换。
- **语法糖漂移**：糖与底层形式并存期间，`explain` 输出 + 快照测试防止脱糖模板与手写体走样。
- **摄入值膨胀**：64KB 上限 + 只摄入 `ingest: true` 的路由型 JSON；plan/summary 类文档永不摄入。

## 7. 未采纳的替代方案

- **方案二（全结构化数据流，文件仅渲染）**：与 LLM agent 的文件系统 I/O 模型冲突，plan 类文档强行建模丢表达力，改动巨大收益存疑。
- **迁移到 Dagu**：无多节点环（review→fix→review 写不出）、分支靠隐式 skip 传播无 fail-closed 语义、条件语言为字符串/正则匹配、无写隔离与确定性重放——等于丢弃本引擎最有价值的部分。
- **仅做减法（砍 workflow-state.yaml 投影等）**：只缓解维护成本，契约与路由痛点原样保留；其收益已并入 P3 的机制退役。
