# Schema 契约

本页是打包机器契约的权威人类说明。`assurance_agent/_resources/schemas/` 下的文件是机器可读的事实源，本页只解释它们的角色与用户可见行为，不复制字段约束。当本页与机器契约冲突时，以机器契约为准。

## 打包文件

| 机器契约 | 适用对象 | 用途 |
|---|---|---|
| `schemas/workflow-schema.yaml` | 工作流 entrypoints / graphs / gates / params / policies（schema v2） | 随 CLI 分发的运行期编排契约，可被项目 schema 覆盖 |
| `schemas/execution-contracts.yaml` | 节点 target（`skill:*` / `operation:*` / `builtin:*`） | 资源 claim、写授权与可重试错误的执行契约目录，可被项目 `.aa/execution-contracts.yaml` 整体覆盖 |
| `schemas/explore-advisory.schema.json` | `qa/changes/<id>/explore/advisory.json` | Explore advisory 产物的 JSON Schema 参考 |
| `schemas/explore-context.schema.json` | `qa/changes/<id>/explore/context.json` | 聚合 explore context 产物的 JSON Schema 参考 |

JSON Schema 文件是给非 Python 消费者的参考。运行期产物校验由 `assurance_agent/artifacts/` 下的 pydantic 模型 + 路径注册表实现，`aa validate` 与 risk 语义检查都消费同一注册表（唯一契约来源；任何模块禁止私开字典解析同一产物）。

## Workflow schema 解析顺序

无显式覆盖时，CLI 按此顺序解析 workflow schema：

1. 项目 `.aa/workflow-schema.yaml`；
2. 项目 `schemas/workflow-schema.yaml`；
3. 包内默认 `schemas/workflow-schema.yaml`。

显式 `--schema` 覆盖是**排他**的：路径缺失即报错，不回退到隐式候选。

## Workflow schema v2 编排词汇

打包 schema 为 `schema_version: "2"`。根级键仅允许 `schema_version` / `name` / `params` / `entrypoints` / `policies` / `graphs` / `gates`；v1 的 `phases:` / `loops:` 在加载期即被拒绝。语义由 `assurance_agent/workflow/graph/`（GraphRuntime）实现，此处只描述用户可见行为；字段约束以 `workflow/graph/schema_v2.py` 的 pydantic 模型为准。

- **params**——类型化运行参数（`enum` / `list` / `bool` / `int` / `str` + `default`），invocation 级可覆盖，未声明的 param 直接拒绝。
- **entrypoints**——图的入口：`graph` 指定目标图；`allow` DSL 在启动前裁决 params；`with` 固化 param 覆盖；`restart: once|repeatable` 决定完成后能否再跑。
- **policies**——命名 `retry`（`max_attempts` / `retry_on` / `backoff`）与 `timeout`（`run_seconds` / `heartbeat_seconds`）策略，node 按名引用；`scheduler.max_parallel_tasks` 控制同一 superstep 的并行上限。
- **graphs**——拓扑主体：`nodes` + `edges`（可带 `when`）+ `routes`（DSL `select` 多路分派 + `cases`/`default`）+ `state`（带 reducer）/ `budgets` / `exports`；图可嵌套（node `uses: graph:<id>`）。
- **gates**——声明式裁决定义；node 用 `gate:` 在成功提交后裁决，或用 `builtin:gate` 节点做前置裁决。

节点（NodeDef）词汇速查：

| 字段 | 作用 |
|---|---|
| `uses` | 执行体：`skill:<name>`（agent 推理）、`operation:<name>`（进程内确定性操作）、`builtin:join\|gate\|interrupt`、`graph:<id>`（子图） |
| `agent` | skill 节点使用的 subagent 角色 |
| `when` | DSL 条件，不满足则跳过该节点 |
| `outputs` | 声明产物路径（`change:` / `project:` / `repo:` 前缀） |
| `gate` | 节点成功提交后求值的 gate |
| `retry` / `timeout` | 引用 policies 中的命名策略 |
| `with` | 静态参数，原样进 task input（operation 唯一的参数化通道） |
| `resources` | `reads` / `writes` / `synchronized` / `exclusive` 资源声明，供并行冲突判定、当前 run 快照收窄与写授权收窄 |
| `evidence` | 引用上游节点已冻结输出作为本节点输入证据 |
| `state_writes` | 把 task value 写入图 state（按声明的 reducer 合并） |
| `join` | `builtin:join` 的汇聚声明（`all` / `all_active` / `any`） |
| `fan_out` | 运行期按 DSL 求值 `items` 动态展开 child task（map），可配 `reduce` 汇聚；展开即冻结，源漂移 fail closed |
| `budget` | 业务预算消耗（如 fix 次数上限），耗尽路由到 `exhausted_to` 节点 |
| `interrupt` | `builtin:interrupt` 的人工中断声明：`actions` 列表，`resume` 时由人选择并续跑 |
| `recover` | retry 耗尽后的降级路径：经 `via` operation 记录后 `continue_to` 指定节点 |

**DSL**——`allow` / `when` / `select` / gate 表达式共用一套白名单 AST 解释器（非 `eval`，有长度/深度上限）：`len` / `defined` / `file_exists` / `gate(id)` / `node(id)` / `capabilities_present(review, dk)` / `plan_review_route(node)` / `any` / `all` / `count`；比较运算仅 `==` `!=` `<` `<=` `>` `>=` `in` `not in`。

**执行体目录与自定义**——`schemas/execution-contracts.yaml` 是全部 `skill:*` / `operation:*` / `builtin:*` target 的唯一目录：每条声明 handler、资源 claim、写授权（`authorization_writes`）与可重试错误；未登记的 target 编译期报错，写范围不可推导的保守为 `global:exclusive`。自定义路径：

- **换图/改图（零引擎代码）**：项目放 `.aa/workflow-schema.yaml`（或 `schemas/workflow-schema.yaml`）即可整体替换拓扑；运行中改图会产生新 digest，旧 invocation 拒绝普通 resume（fail closed）。
- **新增 skill 节点（零引擎代码）**：skill 文件 + contracts 加条目，node 声明 `uses: skill:<name>` 与 `agent:`。
- **新增 operation（需改引擎，三步）**：`workflow/graph/handlers/operation.py` 的 `default_operations()` 注册 callable → contracts 加条目 → schema 中 `uses: operation:<name>`。operation 的参数经 node `with:` 传入，由函数自行校验。

## Checkpoint 与恢复语义（schema v2）

权威状态只有 `events.jsonl`（append-only ledger）；`workflow-state.yaml` 是从 ledger 投影的人工/报告视图，运行时决策从不读它；`driver.json` 只是非权威进程指针（锁 + 最近已知的 `invocation_id` / `checkpoint_id` / `event_seq`）。主循环按 superstep（Plan → Execute → Update）推进：每次提交的 `checkpoint_id` 是 canonical digest，恢复不读快照——重跑 `aa workflow run --change <id>` 即从 ledger 重投影、跳过已成功 task 继续。运行中途修改 schema YAML 会触发 digest 漂移并在下一 superstep 中止（`GraphDefinitionChanged`，exit 40），已提交的 ledger 不受影响。`aa status` 从 ledger 投影 GraphStatus。

## 校验 change 产物

`aa validate` 不调用 LLM，确定性校验结构化 change 产物：

```text
aa validate --change <id> [--phase <phase>] [--artifact <relpath>] [--json]
```

- 默认校验 `qa/changes/<id>/` 下每个被识别的产物，用 `assurance_agent/artifacts/registry.py` 的路径注册表匹配。
- `--phase <phase>` 限定到该阶段 `produces` 声明的产物。
- `--artifact <relpath>` 校验单个 change 相对文件。
- `--json` 输出机器可读的 `{ ok, results }`；每个 result 为 `{ path, artifact_type, ok, errors[] }`。

退出码：全部通过 `0`；校验失败（含 change、所请求 artifact 缺失、显式请求未注册 artifact，或扫描不到任何注册产物）`1`；用法错误（如未知 phase）`2`。人类可读输出逐产物报告及其错误；“零个注册产物”必须 fail closed，不能形成 CI 假绿。

产物兼容性分级（见注册表 `compat` 字段）：

- `must_compat`——skill 直接读写、gate 表达式直接引用的字段（如 review 的 `decision`/`auto_fix_allowed`/`codegen_readiness`，failure-analysis 的 `fix_proposal_eligible`）：字段名与枚举取值不得改。
- `versioned`——带 `schema_version`、允许结构演进的 CLI 产物（如 `workflow-state.yaml`、execution manifest）。
- `free`——纯 CLI 内部产物（报告 markdown 排版、events 扩展字段），不进注册表、不校验。

## Data knowledge（L1 / L2）

**L1（repo 级）**：`.aa/data-knowledge.yaml` — 正式领域知识库，注册于 `assurance_agent/artifacts/repo_registry.py`，由 `aa knowledge validate`（无 `--change`）与 `aa knowledge promote` 消费。

**L2（change 级）**：`plans/data-knowledge.proposal.<layer>.yaml`（`layer ∈ {api,e2e}`）— 规划/评审阶段的增量提案，注册于 change-relative `artifacts/registry.py`。`mode: bootstrap` 表示 L1 缺失首生；`mode: delta` 表示 L1 存在但缺 leaf。

```text
aa knowledge validate [--project-dir] [--change <id>] [--proposal <path>]
aa knowledge promote [--project-dir] (--change <id> | --from <proposal-path>) [--yes] [--force]
```

- `validate` 从 registry 解析 pydantic 模型，不维护平行映射。
- `promote` 将 L2 leaf merge 进 L1（剥离 proposal-only metadata）；冲突写入 `promote-conflicts.json`，需 `--force` 才覆盖。
- API/E2E plan-review gate 通过 `required_capabilities[]`（review JSON 中的 leaf dotted keys）与 L1 做 pre-codegen 能力校验；缺 leaf → `needs_human_review` + **knowledge-remediation** checkpoint（人工 promote 后 `fix_and_proceed` 重跑 review）。
- Fuzz/Performance plan-review gate 读 review JSON 的 `layer_applicable`：被 proposal 选中但无对应 `type:Fuzz`/`type:Performance` case（空 scope）时 reviewer 置 `layer_applicable: false` → gate 走 `skip`（分支结束、codegen 跳过），而非硬 `reject` 拖垮整条并行链。缺失该字段时按原 `pass`/`reject` 语义处理。

## Plan check 证据（`review/*-plan-checks.json`，schema v2）

四层机械 check（`l1_path` / `shared_factory` / `assert_ideal` / `capability_keys`，运行顺序由 `assurance_agent/artifacts/models/assurance.py` 的 `PLAN_CHECK_IDS` 声明顺序决定）产出的 `PlanCheckDocument`（`assurance_agent/artifacts/models/plan_checks.py`）是版本化产物：

- **version 1**——历史只读格式：无 `layer` / `applicability` 字段，文档级与逐 check 的 `status` 只允许 `pass`/`fail`。可被解析，但生产路径不再写出。
- **version 2**——`api` / `e2e` / `fuzz` / `performance` 四层统一生产格式：新增 `layer`（`LayerName`）与 `applicability`（`LayerApplicability`：`applicable` + `reason_code` + 排序去重的 `case_ids`）。`checks` 必须**恰好**包含 `PLAN_CHECK_IDS` 中每个已知 check 各一次——多、少、重复或未知 `check_id` 均在模型校验期拒绝（fail closed），不会静默丢弃或吞并。

**check 状态语义**：

- `pass` / `fail`——check 在本层适用且已求值；`fail` 必须携带非空 `findings`，`pass`/`not_applicable` 禁止携带 `findings`。
- `not_applicable`——check 未求值，`applicability_reason` 二选一，含义不同：
  - `layer_not_applicable`——**整层**因该 change 在该层无自动化 case 而不适用（`applicability.applicable=False`，运行期空 scope）；此时四个 check 全部 `not_applicable`，下次该层出现自动化 case 时会重新变为适用；
  - `check_not_in_profile`——层本身适用，但该 check 按**静态 profile**（`assurance_agent/verification/profiles.py` 的 `applicable_check_ids`）在此层被永久排除，与本次 case 集合无关（目前仅 Fuzz/Performance 排除 `assert_ideal`，因为二者无 ideal-result 断言语义）。

区分这两者是关键：前者是运行期事实（会随 case 变化），后者是层的固有能力边界（不会随 case 变化）。`validate_plan_check_document`（`assurance_agent/verification/checks/registry.py`）在 Pydantic 结构校验之外，额外用运行期 profile catalog 校验每个 check 的 `not_applicable` 理由与静态排除表一致——例如把 Fuzz 层的 `assert_ideal` 标成 `pass` 能通过 Pydantic（单看文档结构合法），但会被这一步拒绝，因为「该 check 在该层被静态排除」是 profile catalog 知识，文档自身无法单独表达或验证。

`PlanCheckDocument.status` 由 `applicability`/`checks` 机械推导（层不适用 → `not_applicable`；任一 check `fail` → `fail`；否则 `pass`），文档级校验器拒绝与推导值不一致的手写 `status`，以及 `layer` 与 `applicability.layer` 不一致的文档。全套校验对畸形输入 fail closed：缺失必需字段、check 集合不完整/含未知项、layer 不匹配均在模型构造/解析期抛出，不产出部分有效的文档。

**与 `Review.layer_applicable` 的关系**：Fuzz/Performance 的层级适用性目前仍经由 `review/*-plan-review.json` 的 `layer_applicable` 字段供 plan-review gate 读取（见上文）。本次改动只落地机械 check 证据自身的版本化与 profile 化执行，尚未把 gate/graph 的跨层消费迁移到 `PlanCheckDocument.applicability`；因此 `Review.layer_applicable` 字段**本次不删除**，其消费迁移与运行期只读该字段的移除属于后续依赖计划。

**v2 `LayerApplicability` 字段**（`PlanCheckDocument.applicability`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `layer` | `api` \| `e2e` \| `fuzz` \| `performance` | 必须与文档级 `layer` 一致 |
| `applicable` | bool | 该 change 在该层是否存在自动化 case |
| `reason_code` | `automated_cases_present` \| `no_automated_cases` | 适用时为前者且 `case_ids` 非空；不适用时为后者且 `case_ids` 为空 |
| `case_ids` | string[] | 排序去重后的自动化 case ID 列表 |

## Graph invocation 事件（`graph_invocation_started` v5）

新 root invocation 写入 `event_schema_version: 5`，并**要求**已落盘的 assurance-profile 快照（见下）。v4 事件保持可读，**从不**被升级，也**从不**被补造（fabricated）profile 快照。v1–v3 事件仍可由 `migrate_graph_event_stream` 解析，缺失的绑定字段回填为空字符串，不会被静默升级为可 replay 的 specialty 证据。

| 字段 | v4+ 要求 | 说明 |
|---|---|---|
| `policy_digest` | 必填 | 归一化 policy 快照的 SHA-256；快照位于 `.graph-runtime/policies/<digest>.json` |
| `policy_origin` | 必填，闭枚举 | `project`（来自 root tree 的项目 `.aa/policy.yaml`）或 `packaged_default`（无项目 policy 时使用打包默认）；相同归一化内容共享同一快照文件，origin 单独记录 |
| `gate_semantics_digest` | 必填 | 代码拥有的 gate/DSL/校验语义 manifest 聚合 digest；实现变更但未 bump 语义版本也会改变 digest |
| `assurance_profile_digest` | 必填 | 四层 assurance profile 与 check catalog 的归一化 digest |

v5 额外要求：digest 对应的不可变字节必须已写入 `.graph-runtime/assurance-profiles/<digest>.json`，且在 `graph_invocation_started` 引用该 digest 之前通过解析校验。重复写入相同字节幂等；同 digest 不同字节为完整性错误。子 invocation 继承 digest 与「需要 snapshot」的 epoch，不重新从当前 registry 取材。

### Assurance-profile 快照

Profile 快照是元数据与审计证据，不是归档的 Python 实现。Replay 对该文件做 hash/parse，**不会**静默用当前 profile registry 替换它。可执行 gate/check 语义仍须与记录的 `gate_semantics_digest` 兼容，wired 行才能 `complete`。

对无 profile 快照的历史 v4 invocation：

- 在 pinned 定义完整性成立后，legacy-unwired 的 Fuzz/Performance 仍可报告 `not_wired`；
- API/E2E wired 行仅在记录的 profile digest 与可用 manifest 可证明兼容时才能 `complete`；
- pre-v4 或定义绑定损坏的 invocation **不能**升格为 `not_wired`，保持 report 级 `incomplete`（或走 legacy renderer）。

### 编译入口三分（无启发式降级）

工作流编译是三条显式入口，**禁止**按 schema 内容启发式选择更弱的校验器：

| 入口 | API | 用途 |
|---|---|---|
| core | `compile_workflow` | 合成/自定义 schema；不做 packaged 四层 activation 门禁 |
| packaged-current | `compile_packaged_workflow` | 打包 assurance schema；要求完整四层 activation，并校验 live ingest catalog |
| pinned-historical | `compile_historical_workflow` | 仅消费已校验的 pinned schema / ingest-catalog / execution-contract 快照；不调用 `validate_catalog_runtime` |

当前路径继续用 live catalog；历史路径只使用 reconstructed catalog。`WorkflowSchemaOrigin`（`packaged` / `project` / `explicit`）记录加载来源，不替代上述编译入口选择。

### Manual revision、revision view 与 prefix recovery

Fuzz/Performance 的 `fix_and_proceed` interrupt 声明 `manual_revision` allowlist（仅 plan-node 输出）。v5 interrupt 在 ephemeral `TaskWorkspace` 清理前物化可写 revision view：

```text
.graph-runtime/revision-views/<interrupt-id>/
```

**Revision view 是可变 transport**，不是证据。证据是 `manual_plan_revision.target_tree_id`（以及事件上的 base/target 树、路径 digest、`revision_transition_id` 与 resume-anchor 链）和 ledger lineage。一旦 `manual_plan_revision` 已提交，恢复只读已提交事件字段与不可变 tree object，**不再**重读可变 view。

该协议是显式的 **crash-recoverable prefix recovery**（目标对象 → `manual_plan_revision` → 有序 `graph_resumed` 后缀），**不得**描述为 power-loss-atomic 的事务回滚。合法前缀可单独落盘；重启后只补缺失后缀。缺口、乱序、重复 ordinal 或 payload 漂移 → `manual_plan_revision_prefix_conflict`。字节相同的 `fix_and_proceed` → `manual_plan_revision_noop`，interrupt 保持未解决。

### v5 resume 的 source-gate 绑定

v5 resume 仅在 interrupt 存在真实已提交的 gate evidence epoch 时绑定 `source_gate_attempt_id` + `source_gate_tree_id` 对；该对必须与当前 gate 求值一致才可作为 gate override。Improvement/issue 等无 gate checkpoint 的 interrupt 保持 **pairless**：仍可正常 resume，但**不能**充当 gate override。v4 保持仅 hash 的兼容路径。

## Counterfactual plan-check policy replay（v2）

Specialty Report **当前 writer** 为 schema `"3"`（见下）。`"2"` 报告仍可读，其 `capability_contract_policy.semantics` 为 `counterfactual_plan_check_actions/v2`（拓扑驱动分类）。`counterfactual_plan_check_actions/v1` 与 legacy specialty `schema_version: "1"` **保持可读**，旧文件从不被改写。v1 的历史解释不变：API/E2E 视为 wired，Fuzz/Performance 不能为 `complete`。

每个 **complete** 层行携带恰好三个 scenario，action 顺序固定为 `warn` → `block` → `require_human`：

| scenario 字段 | 说明 |
|---|---|
| `action` | `warn` \| `block` \| `require_human` |
| `policy_digest` | 该 counterfactual 分支所用 policy 快照 digest |
| `verdict` / `route` / `matched_rule` / `reason` | 冻结 gate 求值结果 |
| `missing_capabilities` | reviewer 能力缺口（可为空） |
| `policy_effect` | 闭枚举，见下表 |

**`policy_effect` 闭枚举**：

| 值 | 含义 |
|---|---|
| `applied` | 该 action 的 check 失败规则（或同等 reviewer 裁决）决定了 gate 结果 |
| `no_failed_checks` | 无失败 check；层不适用时三个 scenario 均为 `skip` 且通常为此值 |
| `shadowed_by_gate_precondition` | 存在失败 check，但更早的 reviewer needs-fix / human / explicit-reject 规则已决定结果 |
| `shadowed_by_capability_precondition` | 能力前置条件本身是最先决定结果的规则 |

Counterfactual replay 仅在 baseline gate/route 校准通过后运行：冻结 baseline policy 对绑定 raw bytes 的 gate 报告字段必须一致；route 由 `plan_review_route` 推导并与 ledger 激活/跳过事件交叉校验。

## 四层 replay 矩阵（SpecialtyReport v2，semantics v2）

`SpecialtyReportV2.capability_contract_policy` 始终输出 **恰好四行**，layer 顺序固定为 `api` → `e2e` → `fuzz` → `performance`（与 `LAYER_NAMES` / `CASE_TYPES` 一致）。顶层 `integrity` 闭枚举：

| `integrity` | 条件 |
|---|---|
| `complete` | `definition_binding` 存在且无任何 `incomplete` 行 |
| `incomplete` | 缺失/模糊定义绑定，或任一行 `status == incomplete` |

**行级 `status` 闭枚举**（由 pinned 拓扑分类驱动，不是硬编码 wired 集合）：

| status | 含义 |
|---|---|
| `complete` | 选中且 fully wired；含 applicability、mechanical checks、evidence digests、三 scenario |
| `not_selected` | pinned params 下 assurance 分支未选中；不 fabricated scenario |
| `not_wired` | 选中且 pinned 拓扑为 legacy-unwired；仅在 pinned 定义完整性成功后可用于 legacy v4 Fuzz/Performance；不 fabricated scenario |
| `incomplete` | 带 `reason_code`（如 `partial_assurance_wiring`、`root_invocation_unbound`、`gate_evidence_drift`、`profile_snapshot_missing`）；无借用 artifact |

层选择来自 pinned assurance graph 的 params-only `when` 谓词（可含 `run_mode` 与 `test_types` 合取），**不是** `test_types` 单独推断。Fully activated 的 v5 拓扑上，被选中的 Fuzz/Performance 只能是 `complete` 或 `incomplete`，**绝不是** `not_wired`。部分接线（任一 activation marker 出现但不完整）→ `incomplete` / `partial_assurance_wiring`，永不降级为 `not_wired`。当前源文件从不改写历史分类；迁移不补造缺失的 ingest/contract/profile 快照或 gate 证据。

## Specialty report v1（仅展示）

`schema_version: "1"` 的 legacy report 可被 `load_specialty_report` 读取并参与 benchmark evidence-row 导出，但：

- **不能**通过 `SpecialtyReportV2` / `SpecialtyReportV3` 校验（无四层矩阵、无 definition binding、无 typed traceability）；
- Capability/Policy Markdown 仍用 `legacy_api_only` 行展示历史 policy replay；Traceability 区标记 `legacy_unlayered`，**不**生成四个零值 complete layer row；
- 不得被静默升级为 v2/v3 证据；当前 writer 只写 Specialty Report `"3"`。

## Trace / reconcile / quality / specialty 线缆兼容

本节记录当前 writer 与兼容 reader 的精确版本边界。Python 名 `TraceProjection` / `IssueReconcileStatus` / `QualityGateResult` 仍是 **V1 别名**（只读历史），不是当前 writer 类型。磁盘上的 `inspect/trace-projection.json` 是 **point-in-time** 产物：通过 registry 形状校验不等于 current authority；声称“当前 reconciled projection”的路径必须经 `load_current_reconciled_projection`，对 legacy V1 / digest 漂移返回 typed stale，不得把陈旧文件当 live authority。

### Trace Projection（`inspect/trace-projection.json`，execution batch 同模型）

| | Reader | Current writer |
|---|---|---|
| `"1"` | `TraceProjectionV1`（别名 `TraceProjection`） | 否 |
| `"2"` | `TraceProjectionV2` | 是（`fold_trace` / materializer） |

- Registry 面：`TraceProjectionDocument`（`schema_version` discriminator）；compat=`versioned`。
- **Legacy missing-version**：顶层 mapping **完全缺少** `schema_version` key 时，loader 仅注入 `"1"` 再走 discriminator。显式 `null`、空字符串、未知版本一律 fail closed。IssueReconcile / QualityGate **不做**同类缺失注入。
- **V2-only gap codes**（不得出现在 V1）：`failure_analysis_identity_mismatch`、`issues_snapshot_identity_mismatch`、`issue_analysis_failed`、`project_sync_pending`、`issue_reconcile_failed`、`issue_reconciliation_unavailable`。
- **Freshness**：authoritative loader 要求 concrete V2 且与当前 live reconciled fold 的 change/batch/canonical digest 完全相等；V1 → `legacy_version` stale；source 变化 → typed stale，不自动重写磁盘。

### Issue Reconcile Status（`inspect/issue-reconcile-status.json`）

| | Reader | Current writer |
|---|---|---|
| `"1.0"` | `IssueReconcileStatusV1`（别名 `IssueReconcileStatus`）；仅 `completed` \| `failed` | 否 |
| `"2.0"` | `IssueReconcileStatusV2` | 是 |

V2 形状（三种状态均要求非空 `candidate_digest`）：

| status | `occurrence_count` | `error` |
|---|---|---|
| `completed` | 必填且 `>= 0` | 必须 `null` |
| `failed` | 必须 `null` | 非空 |
| `pending` | 必须 `null` | 必须 `null` |

V1 可读但只作 legacy fact，不能建立本设计的 current completed/failed/pending authority。

### Quality Gate Result（execution / inspect quality-gate 路径）

| | Reader | Current writer |
|---|---|---|
| `"1.0"` | `QualityGateResultV1`（别名 `QualityGateResult`）；无 typed evidence | 否 |
| `"2.0"` | `QualityGateResultV2` | 是 |

V2 coverage `evidence` 是 typed 联合：

- `kind: "sufficiency"` → 嵌入 `SufficiencyReportV2`（见下）；
- `kind: "error"` → `error_code ∈ {evidence_projection_missing, policy_error}`。

`aa report generate` 经 concrete document dispatch 读取 V1-only / V2-only 门禁产物，输出 QualityReport 仍为 `"1.1"`；不把磁盘上的 V1 quality 升级为 V2。

### Sufficiency Report `"2.0"`（reporting-only）

`SufficiencyReportV2`（`artifacts/models/sufficiency.py`）绑定：

- `schema_version: "2.0"`、`semantics: "evidence_sufficiency/v2"`；
- `source_projection_digest` / `source_policy_digest`；
- Quality success 路径要求 `require_current_batch: true`；
- 四层 sufficiency join 是 **reporting-only view**：按 `case_id` 关联 verdict，不回写 `TraceProjection`，不携带 policy/clock 进 projection。

### execution / reconciled phase-pair

仅两相：`execution`（执行期事实）与 `reconciled`（在其上增加当前有效的 failure/problem enrichment）。共享 row 身份字段必须一致；允许差异的只有声明的 enrichment 字段。Phase-pair 违反 → specialty collect `projection_phase_pair_mismatch`（incomplete），integrity 不得因 enrichment 而“变好”。

### 三图 settled-path 与 recovery-as-incomplete

`inspect-with-issues` / `issue-analyze` / `issue-reconcile` 凡将 issue 状态视为 settled 的路径，都必须先经 `operation:materialize-trace-projection` 再完成。Typed recovery（analysis failure、project sync pending 等）发布 **当前 batch** 的合法 incomplete V2 projection（稳定 blocking gap，无陈旧 problem links），不得跳过 materialize，也不得把旧 batch 磁盘文件冒充当前 authority。

### Specialty Report `"1"` / `"2"` / `"3"`

| Version | Role |
|---|---|
| `"1"` / `"2"` | Legacy readers；evidence-row / render 标 `legacy_unlayered`；无 typed 四层 complete matrix |
| `"3"` | Current writer：`status` 判别的 complete / incomplete `TraceabilityEvidenceV3` 联合 |

Incomplete V3 使用闭集 `TraceCollectionFailureReason`（含 `reconciled_projection_missing` / `reconciled_projection_stale` 等）。旧 pinned run 无当前 reconciled V2 → 原子写出 incomplete V3 + `reconciled_projection_missing` 并非零退出。旧 recovery-barrier 修出的 V1 projection 经 current loader 判 `legacy_version` → collect 为 `reconciled_projection_stale` incomplete，**从不**升格为 complete v3 matrix。

### Specialty V3 发布收据（Clarification 11）

独立原子文件，非单事务：

1. fresh attempt：先把 sibling receipt 写成 `state="pending"` + 新 `attempt_id`；
2. 再原子写出 report 字节；
3. 再把 receipt 换成 `state="committed"`（绑定 change_id、report_sha256、trace_status、capability_integrity）。

- Fresh validate 还要求调用方 expected attempt ID；reuse 校验已记录的非空 attempt 与全部 binding。
- 同 digest 的旧 committed receipt 不能在 pending 窗口后 ABA 假提交。
- V3 **必须**有匹配的 committed receipt 才可 reuse；仅有 report、无 receipt / pending / 错配一律拒绝。
- V1/V2 仅在 sibling receipt 路径 **不存在** 时允许 receiptless 读取；任何 pending/committed/畸形/错配 receipt 都阻断 legacy bypass。

### Cursor evidence-row 十列（Clarification 9）

```text
change_id|collection_status|reason_code|trace_exit|integrity|gap_count|verify_exit|verdict|blocking|insufficient
```

`collection_status` ∈ `{raw, complete, incomplete, legacy_unlayered}`。V3 incomplete 上不可用字段写字面量 `unknown`，**从不**用 `0` / `-1` 顶替。无 `--schema-root` 逃生舱。

### 历史 pinned 续跑边界（Clarification 10，修正设计 D7）

- graph / contract / **ingest-catalog** identity 漂移：解析已校验的 pinned execution bundle，**可继续旧拓扑**；本身不抛 `GraphDefinitionChanged`。
- gate-semantics / profile 不兼容，或 pinned-model 与 current-class schema 不匹配：在依赖定义的 pending-write recovery / planning **之前** fail closed（`GraphDefinitionChanged`）。
- Recovery barrier 仍是 invocation-local；已成功 task 只 replay 冻结 write-set，不重调 handler。
- **不**归档历史 Python handler：后续旧拓扑任务只能经与 pinned catalog 证明兼容的 **当前** 代码执行。修出的 V1 artifact 仍是 legacy/stale，不是 current complete 证据。

## Retro v3 signal analysis 与 Improvement lifecycle

Retro 是**独立入口**（`aa workflow run --entrypoint retro` / `aa retro`），不挂在 full workflow 上。当前 run 只读写 `qa/retro/<retro-id>/`；生产路径不扫描、不迁移、不消费历史 Retro 目录。

数据流固定为：确定性 reader 解析窗口并物化三份 typed evidence slice → 三个领域 skill 并行分析 issue / workflow / eval → runtime 校验引用并回填 slice digest → operation 机械装配 context v3 → proposer 生成 Candidate draft → runtime 回填 context digest → reconciler 校验完整追溯链并写入 Improvement Ledger。分析 skill 只能看到本域 slice；proposer 只能看到当前 run 的 context。

### 当前 run 产物

| 文件 | 写入方 | 说明 |
|---|---|---|
| `qa/retro/<id>/window.json` | `operation:retro-collect-v3` | 本轮互斥窗口选择的冻结结果 |
| `qa/retro/<id>/evidence/{issue,workflow,eval}-slice.json` | `operation:retro-collect-v3` | schema_version `"3"` 的 immutable typed evidence slice |
| `qa/retro/<id>/signals/{issue,workflow,eval}.json` | 对应领域 skill；失败时由 recovery operation 写入 | schema_version `"3"`；runtime 校验 refs、回填并冻结 `slice_sha256`；失败域显式记录 `analysis_status=failed` |
| `qa/retro/<id>/context.json` | `operation:assemble-retro-context-v3` | schema_version `"3"`：汇总 source manifest、逐域状态、integrity 与全部合法 signals；不做语义过滤 |
| `qa/retro/<id>/proposal-candidates.json` | `skill:aa-retro` 或零信号 receipt | schema_version `"3"` Candidate 批（非权威）；每项引用 `signal_ids` 与 immutable `source_refs`，runtime 回填 `context_sha256` |
| `qa/retro/<id>/retro-summary.md` | `skill:aa-retro` | 人类摘要 |
| `qa/retro/<id>/accept-status.json` | `operation:reconcile-improvements` | 批级 receipt（accepted/failed + digests/event ids） |
| `qa/retro/<id>/review-queue.md` | reconcile | 指向本批 canonical Improvement IDs |
| `qa/retro/<id>/pipeline-failure.json` | Graph recovery / Supervisor | 结构化 stage + error kind；自由错误文本只保留摘要指纹 |
| `qa/retro/<id>/retro-status.json` | finalizer / Supervisor | `completed` / `completed_with_gaps` / `pending_reconcile` |
| `qa/improvements/outbox/pending/<id>.json` | reconcile | 自包含 context + Candidate 的 durable pending work；下一轮 Retro collect 前幂等 drain |

### Window 选择

互斥：`--batch-manifest <json>`（自动批次的权威 Change 集）/ `--change`（人工显式 Change 集）/ `--since`+`--until`（时间窗）/ `--last N`。入口不再隐式选择 last 10；调用方必须明确窗口。Batch manifest 的 members 必须已排序且唯一，`change_ids` 与 members 顺序逐项相同；缺失、运行中或损坏成员不会被丢弃，而是生成 typed evidence-gap signal。显式 Change 与 last-N 模式只接纳 `source_change_ids` 有交集的 `qa/eval/runs/*/report.json` compact projection；Retro 不读取 raw `eval/out/**`。`--dry-run` 会完成 collect、三域分析和 assemble，只跳过 propose/reconcile。

三个分析域经各自 `*-settled` 节点进入 `all_active` join。某域在 retry 耗尽后由 recovery 写出 typed failed signal document，因此不会把“分析失败”伪装成“零信号”。所有成员的三域证据均 absent 时不调用 analyzer/proposer，而是直接产生 deterministic workflow Improvement。collect / assemble / propose / reconcile 失败由同一 typed pipeline-failure fallback 收口，并最终写 `retro-status.json`；Graph 外 compile/dispatch/freeze/finalize 失败由同一 Supervisor 补偿。已有 final Retro status 后的 Auto Review 失败不得回写这些 Retro 文件。

### Improvement kinds 与 deliveries

| kind | 允许的 delivery |
|---|---|
| `prompt_improvement` | `memory_patch` |
| `fixture_improvement` / `test_improvement` | `memory_patch` 或 `change_draft` |
| `workflow_improvement` | `change_draft` |
| `domain_knowledge` | `knowledge_delta` |

Project Improvement Ledger（唯一权威）：

| 路径 | 角色 |
|---|---|
| `qa/improvements/events.jsonl` | 追加事件 |
| `qa/improvements/improvements.json` | 确定性投影 |
| `qa/improvements/review-queue.json` | 待审队列 |

独立入口：`improvement-review` / `improvement-evaluate` / `improvement-export` / `improvement-apply` / `improvement-rollback`。CLI 读模型：`aa improvement list|show`（只读投影，不扫 `qa/retro/`）。

Delivery 落点：

- `memory_patch` → `.aa/memory/<skill>.md`（evaluate → apply/rollback；运行时 `load_skill_memory` 注入，8 KiB 上限，过滤 `deprecated:`）
- `change_draft` → `qa/improvements/drafts/<id>.md`（人工落地 Change 后 `record-*-applied`）
- `knowledge_delta` → `qa/improvements/knowledge-delta/<id>.proposal.yaml`（**不**写 L1；合入 L1 仍走 `aa knowledge promote`，且要求引用的 Problem 已 `resolved` + `human_confirmed` + 有 verification scope）

已删除的旧路径（无生产读/写）：`aa retro nightly`、`export-issues` / `export-knowledge`、`qa/retro/_state.json`、`cross-run-report.json`、per-run `promotions.json` / `issue-drafts/**`、枚举 `workflow_bug` / `issue_export`。

## Issue lifecycle (Change Issue Ledger + Project Problem Ledger)

Issue lifecycle replaces the retired Markdown/JSON known-product side channel. Canonical state lives in two append-only Ledgers and their deterministic projections:

| Scope | Ledger | Projection | Role |
|---|---|---|---|
| Change | `qa/changes/<id>/issues/events.jsonl` | `qa/changes/<id>/issues/snapshot.json` | Immutable Observations and Occurrences for one Change |
| Project | `qa/issues/events.jsonl` | `qa/issues/problems.json`, `qa/issues/review-queue.json` | Cross-Change Problem identity, lifecycle, and human review queue |

Only deterministic reconciler/review apply operations append Ledger events. Inspect/classifier artifacts (`inspect/failure-analysis.json`) and the legacy `known_product_issue` execution label are **classification hints only** — they do not read or write legacy known-product issue files and do not mutate Problems by themselves.

Risk context reads structured Problems from `qa/issues/problems.json` (not archived Markdown/JSON). Reports separate execution `final_status` from Issue risk (`report/quality-report.json` schema 1.1 `issues` section). Open or unknown Issues never block archive; they affect archive status wording only.

## Quality Score 与 Quality Gate

Quality Score 由 CLI **确定性**计算，LLM 不参与。Quality Gate 四态：`PASS` / `PASS_WITH_WARNINGS` / `FAIL` / `SKIPPED`，跨维度 worst-wins 合并。

维度与权重（从 TS 源提取，落地在 report 模块与打包规则数据；权重与阈值以代码/数据文件为准，本表为说明）：

| 维度 | 计入 | 说明 |
|---|---|---|
| functional | 权重最高 | API / E2E / Fuzz 用例通过率（含 unmapped_tests 惩罚） |
| coverage | 阈值门禁 | 行 / 分支 / 模块 / diff 覆盖率对 `.aa/config.yaml` 阈值 |
| non_functional | 独立维度 | Performance（Locust 绝对阈值：p95、error_rate） |

`score_breakdown` 各维度取值为浮点分数或 `"N/A"`（该维度未启用）。报告三件套：`quality-report.json` / `quality-report.md` / `executive-summary.md`。

## 失败分类

`aa report inspect` 把执行失败归入固定分类（规则表数据从 TS 源提取为打包 YAML 规则数据 `assurance_agent/_resources/rules/failure-classification.yaml`，随包分发并有单测对拍）。分类决定该失败是否 `fix_proposal_eligible`（进入 Healing Loop）。可自愈类（如 `locator_failure`、`wait_strategy_failure`、`test_code_error`、条件性 `test_data_failure`）与不可自愈类（如 `assertion_failure`、`business_logic_failure`、`known_product_issue`、`coverage_gap`、`fuzz_*`、`perf_*`）的完整清单见规则数据文件与 README「失败分类速查」。

## 维护规则

先改机器 schema / 运行期 pydantic 模型的字段约束，再更新本页——且只在契约的**用途、产物映射、解析顺序或用户可见校验行为**变化时更新。不要把完整字段清单复制到本页。
