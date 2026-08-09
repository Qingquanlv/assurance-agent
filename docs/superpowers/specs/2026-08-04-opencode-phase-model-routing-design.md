# OpenCode Phase Model Routing Design

日期：2026-08-04
状态：已批准；2026-08-09 更新 benchmark 角色映射

## 1. 目标

在一次 AA OpenCode workflow 内，根据 workflow skill、前一次失败类型和显式 CLI 覆盖，为每次 agent attempt 选择不同模型。当前 benchmark 以 GLM 5.2 承担 Design/Fixer/Coverage Repair，以 DeepSeek V4 Flash 承担五类 Case/Plan Reviewer；Improvement Reviewer 明确保留 GLM 5.2。

2026-08-09 用户确认的映射修订优先于本文早期示例：

- `aa-case-design`、四类 plan、对应 fixer 与 `aa-coverage-repair` → GLM 5.2；
- 五类 Case/Plan Reviewer → DeepSeek V4 Flash；
- `aa-improvement-reviewer` → GLM 5.2；
- 其余 phase 保持原混合策略。

本设计必须同时满足：

- 模型路由不得削弱现有 `aa-*` agent 权限；
- 未启用路由的项目保持当前行为；
- `--model` 继续支持整条 workflow 的单模型 A/B benchmark；
- DeepSeek 出现契约类失败后只允许一次便宜模型尝试，下一次重试升级到 GLM；
- 每个已结算 agent attempt 都能回答“实际用了哪个模型、为什么选择它”。

## 2. 背景与问题

当前 OpenCode adapter 在构造时解析一次 `--model`，随后每个 phase 的 `prompt_async` 请求都复用同一个 `self._model`。`AgentRequest` 只携带权限 persona `agent`，没有 request-local `model`。因此：

- 可以整条 workflow 固定为 GLM 或 DeepSeek；
- 不能让 FactBaseline 使用 GLM、Report 使用 DeepSeek；
- 不能在 DeepSeek 输出不合法后把同一 task 的下一次 attempt 升级为 GLM；
- benchmark 无法从 graph ledger 直接归因各模型的耗时和失败。

OpenCode 原生支持 agent 级模型，但 AA 现有权限 agent 粒度不等于 workflow step 粒度。例如 `aa-doc-author` 同时服务 Case Design、FactBaseline、Plan、Fix Proposal 和 Retro。把模型写进 agent frontmatter 会把权限策略和成本策略耦合，也无法满足 phase 级路由。

已有 benchmark 给出的保守起点是：

- DeepSeek 速度和价格有优势，但历史五项批次存在重复 schema/YAML 失败；
- GLM 在复杂 API 单项中能独立读取源码、修复契约遗漏并纠正测试映射；
- 因此第一版不把语义敏感 Codegen 交给 DeepSeek，只先迁移有 GLM reviewer 或确定性工具兜底的步骤。

## 3. 已确认的设计决策

| 决策 | 结论 |
|---|---|
| 配置位置 | 合并到项目 `.aa/config.yaml` 的 `execution.model_routing` |
| Provider 配置 | 继续由 `opencode.json` 管理；AA 不复制 base URL、header 或密钥 |
| 权限 | 继续由 `.opencode/agents/aa-*.md` 管理；agent 文件不声明模型 |
| 路由接口 | 一个纯 `ModelRouter` module，根据 skill、前次错误和 CLI override 返回解析结果 |
| 路由 key | 只支持精确 skill name；第一版不支持 glob、node id 或 prompt 内容匹配 |
| CLI 兼容 | 显式 `--model` 强制覆盖所有 phase 和 escalation，便于严格单模型 A/B |
| 默认兼容 | 配置段缺失且未传 `--model` 时，继续省略 request model，让 OpenCode 使用其默认值 |
| 自动升级 | 仅 `invalid_output`、`forbidden_write` 触发配置的 escalation model |
| 基础设施错误 | `auth`、`rate_limit`、`transport`、`timeout` 不切模型 |
| Cursor | headless/Cursor adapter 不读取本路由；跨 adapter 路由另立设计 |

### 3.1 评估过的方案

1. **在现有 `aa-*` agent frontmatter 中写 `model`**：改动最少，但只能按权限 persona 路由。`aa-doc-author` 覆盖多个风险差异很大的步骤，且 CLI/request-local model 可能覆盖 agent 默认值，因此不采用。
2. **新增独立 `.aa/model-routing.yaml`**：隔离清晰，但引入第二套项目配置发现、初始化、校验和 detached 传递生命周期；当前配置规模不值得增加新文件，因此不采用。
3. **在 `.aa/config.yaml` 增加 typed routing，并由单一 `ModelRouter` 解析**：同时提供 phase 粒度、配置 locality、CLI 兼容和可测试的纯 interface，采用此方案。

## 4. 配置契约

项目配置示例：

```yaml
execution:
  entry: cli
  self_healing:
    mode: proposal-only

  model_routing:
    default: anthropic/deepseek-v4-flash
    strict_routes: true

    routes:
      aa-explore: anthropic/glm-5.2
      aa-fact-baseline: anthropic/glm-5.2
      aa-case-design: anthropic/glm-5.2
      aa-case-fixer: anthropic/glm-5.2
      aa-case-reviewer: anthropic/deepseek-v4-flash

      aa-api-plan: anthropic/glm-5.2
      aa-api-plan-fixer: anthropic/glm-5.2
      aa-api-plan-reviewer: anthropic/deepseek-v4-flash
      aa-e2e-plan: anthropic/glm-5.2
      aa-e2e-plan-fixer: anthropic/glm-5.2
      aa-e2e-plan-reviewer: anthropic/deepseek-v4-flash
      aa-fuzz-plan: anthropic/glm-5.2
      aa-fuzz-plan-reviewer: anthropic/deepseek-v4-flash
      aa-performance-plan: anthropic/glm-5.2
      aa-performance-plan-reviewer: anthropic/deepseek-v4-flash
      aa-coverage-repair: anthropic/glm-5.2

      aa-api-codegen: anthropic/glm-5.2
      aa-api-codegen-fixer: anthropic/glm-5.2
      aa-e2e-codegen: anthropic/glm-5.2
      aa-e2e-codegen-fixer: anthropic/glm-5.2
      aa-fuzz-codegen: anthropic/glm-5.2
      aa-performance-codegen: anthropic/glm-5.2

      aa-fix-proposal: anthropic/glm-5.2
      aa-report-generator: anthropic/deepseek-v4-flash
      aa-inspect: anthropic/glm-5.2
      aa-issue-analyzer: anthropic/glm-5.2
      aa-issue-triage-advisor: anthropic/glm-5.2
      aa-archive: anthropic/deepseek-v4-flash

      aa-retro-issue-analysis: anthropic/deepseek-v4-flash
      aa-retro-workflow-analysis: anthropic/deepseek-v4-flash
      aa-retro-eval-analysis: anthropic/deepseek-v4-flash
      aa-retro: anthropic/glm-5.2
      aa-improvement-reviewer: anthropic/glm-5.2

    escalation:
      model: anthropic/glm-5.2
      on_error_kinds:
        - invalid_output
        - forbidden_write
```

### 4.1 字段语义

- `default`：`strict_routes=false` 时，未命中 route 的 agent skill 使用该模型。
- `strict_routes`：为 `true` 时，compiled workflow 中每个 `skill:*` agent node 必须有精确 route；遗漏在启动 agent 调用前失败。operation、gate、subgraph 等非 agent node 不参与覆盖检查。显式 `--model` 会使 routes 整体失效，因此单模型覆盖模式跳过 route coverage 检查，但仍校验配置文件本身的字段与 model 格式。
- `routes`：key 必须是精确 skill name，value 必须是非空 `provider/model`。
- `escalation.model`：前一次 attempt 命中 `on_error_kinds` 时使用的模型。
- `escalation.on_error_kinds`：只接受 graph `ErrorKind`，第一版只允许 `invalid_output` 和 `forbidden_write`。

`model_routing` 是可选配置。`aa init` 不写入 Ark 专属模型 ID；通用初始化模板保持 provider-neutral。benchmark 项目和明确选择混合模型的项目自行添加该段。

配置加载必须使用 typed model。未知字段、空 model、缺少 `/`、重复/非字符串 route、非法 escalation error kind 均在 workflow 启动时以 `ConfigInvalidError` 失败，不能等到第一个 agent 请求才发现。

## 5. 路由解析接口

新增一个纯 module，外部 interface 为：

```python
class ModelRouteContext(BaseModel):
    adapter: str
    skill: str
    prior_error_kind: ErrorKind | None
    cli_override: str | None

class ModelResolution(BaseModel):
    model: str | None
    source: Literal[
        "cli_override",
        "escalation",
        "skill_route",
        "default",
        "opencode_default",
    ]
    policy_sha256: str | None

class ModelRouter:
    def resolve(self, context: ModelRouteContext) -> ModelResolution: ...
```

解析优先级固定为：

1. 非空 `--model` → `cli_override`；整条 workflow 不再发生自动升级。
2. `prior_error_kind` 命中 escalation → `escalation`。
3. 精确 `routes[skill]` → `skill_route`。
4. `strict_routes=false` 且存在 `default` → `default`。
5. 配置段缺失 → `model=None`、`opencode_default`。
6. 其余情况 fail closed，不创建 OpenCode session。

路由不读取 artifact 内容、风险文本或 prompt，不允许模型自行选择模型。相同输入必须产生相同结果。

## 6. 执行数据流

```text
.aa/config.yaml
      │ load + validate + canonical digest
      ▼
 ModelRouter.resolve(skill, prior_error_kind, cli_override)
      │ ModelResolution
      ▼
 AgentRequest(model, model_route_source, policy_sha256)
      │
      ▼
 OpenCodeAdapter prompt_async(model + agent)
      │
      ▼
 AgentResult(effective_model, session_id, usage?)
      │
      ▼
 task attempt settled event / benchmark metrics
```

### 6.1 Config 和 runtime 构造

- `ExecutionCfg` 增加可选 `model_routing` typed 字段。
- CLI 和 detached child 都从项目根加载一次配置，并构造只读 `ModelRouter`。
- `build_graph_runtime` 把 router 注入 agent handler；graph planner、contracts 和权限 claims 不读取模型配置。
- normalized policy 使用 canonical JSON 计算 SHA-256。新进程 resume 可以读取更新后的项目配置，但每次 attempt 记录实际 policy digest，因此配置变更可审计而不会伪装成同一策略。

### 6.2 Agent request

`AgentRequest` 增加可选字段：

- `model: str | None`；
- `model_route_source`；
- `model_policy_sha256`。

默认值必须保持现有 fake adapter、eval 和第三方 `AgentInvoker` 兼容。`agent` 仍表示权限 persona，`model` 只表示认知执行模型，两者互不替代。

### 6.3 OpenCode adapter

- 删除“构造器 model 是唯一模型来源”的假设。
- graph `invoke()` 始终使用 `request.model`；显式 `--model` 由 router 解析进 request，不在 adapter 内再次决策。adapter 构造器的 fallback model 只为没有 request-local model 的 legacy `run_phase()` 保留。
- model 继续以 `{providerID, modelID}` 发送给当前 legacy OpenCode HTTP 面。
- agent 与 model 必须在同一个请求中显式发送，不能依赖 OpenCode 最近使用模型。
- server 明确以 400/404 拒绝未知或不可用模型时归一化为 `invalid_input`；401/403 仍为 `auth`，429 仍为 `rate_limit`。任何情况下都不得静默回落到 server default。

## 7. 自动升级与重试

升级是 attempt 级策略，不修改 graph retry 次数和 retryable error 集合：

- DeepSeek 首次返回合法 artifact → 正常继续；
- DeepSeek 返回 `invalid_output` 或触发 `forbidden_write`，graph 按原 retry policy 决定是否有下一 attempt；
- 若有下一 attempt，router 看到 `prior_error_kind` 后选择 GLM；
- GLM 再失败时继续由原 retry policy 处理，不回退 DeepSeek，不产生模型振荡；
- `auth`、`rate_limit`、`transport`、`timeout` 是环境或服务问题，切换模型可能掩盖根因，必须保持原模型；
- 测试执行失败不是 agent attempt `ErrorKind`。相应 Codegen Fixer 在静态 routes 中直接绑定 GLM，不把 `test_failure` 伪造为 graph error kind。

## 8. 审计与指标

每个已结算 agent attempt 的 strict event 增加可选执行元数据：

```json
{
  "agent_execution": {
    "agent": "aa-api-plan-reviewer",
    "model": "anthropic/glm-5.2",
    "route_source": "skill_route",
    "model_policy_sha256": "sha256:...",
    "session_id": "ses_...",
    "usage": {
      "input_tokens": 0,
      "output_tokens": 0
    }
  }
}
```

- `usage` 只有 OpenCode 响应提供可信值时才写；未知时为 `null`，不能写假 `0`。
- 成功、失败、STOP 都记录相同结构；历史事件字段缺失仍可 replay。
- token、耗时、attempt 数和最终状态按 model/skill 聚合进 benchmark 汇总。
- 不记录 provider header、API key、完整 prompt 或其他凭据。

第一版不要求把 policy 本体复制进 change artifact；digest 与实际 resolution 已足以区分 resume 前后的配置变化。若未来要求完全可复现的模型策略 replay，再单独设计 policy snapshot。

## 9. 权限与安全不变量

模型切换不得改变以下行为：

- `agent_for_skill` / workflow schema 仍决定 `aa-*` persona；
- agent frontmatter 的 edit/bash/external-directory floor 不变；
- contract `authorization_writes`、task workspace、freeze write set 不变；
- 路由失败发生在 OpenCode session 创建前；
- 模型无法从 prompt、skill 输出或 artifact 内容提升自己到另一个模型；
- escalation 只能改变 model，不能改变 agent、allowed writes、timeout、retry budget 或 gate verdict。

## 10. 兼容性

### 10.1 旧项目

没有 `execution.model_routing` 且没有 `--model`：行为与当前完全一致，由 OpenCode 默认模型执行。

### 10.2 显式单模型执行

`--model anthropic/glm-5.2` 或 `--model anthropic/deepseek-v4-flash` 强制所有步骤使用指定模型，忽略 routes 和 escalation。这是单模型 benchmark 和故障复现的权威入口。

### 10.3 Detached / resume / import-checkpoint

- detached child 继续透传显式 `--model`；没有 override 时自行读取 `.aa/config.yaml`。
- resume/import-checkpoint 使用启动该进程时加载的 routing policy；新的 attempt 记录新的 policy digest。
- 已成功 task 不重跑，模型路由只作用于新 attempt。

### 10.4 Headless / Cursor

headless adapter 保持现有 Cursor model 解析和默认 `cursor-grok-4.5-high-fast`。项目配置仍经过通用 schema 校验，但 `execution.model_routing` 不参与 headless 的模型解析，避免把 OpenCode provider ID 传给 `cursor-agent`。

## 11. 分阶段启用

### 阶段 A：保守混合路由

- DeepSeek：四类 Plan 首写、Report、Archive、Retro 三类信号分析；
- GLM：Explore、FactBaseline、Case、所有 Reviewer/Fixer、所有 Codegen、Inspect、Issue、Retro Proposal 和 Improvement Review；
- DeepSeek 契约失败后自动升级 GLM。

### 阶段 B：扩大 DeepSeek 范围

只有阶段 A 在同一 commit、同一 fixture、同一五项 batch 上满足质量基线后，才允许考虑把低风险 Codegen 首次 attempt 路由给 DeepSeek。此前必须先具备可执行的 case-id/测试函数语义映射 gate；仅靠 YAML/Pydantic 合法性和 pytest collect 不能防止静默语义错位。

扩大范围必须通过配置完成，不修改 agent 权限或 workflow topology。

## 12. 验证要求

### 12.1 单元测试

- 配置段缺失保持 OpenCode default；
- 合法配置解析与 canonical digest 稳定；
- 非法 model、未知字段、非法 error kind fail closed；
- `strict_routes=true` 检出 compiled agent skill 漏配；
- 五级解析优先级逐项覆盖；
- DeepSeek `invalid_output` / `forbidden_write` 下一 attempt 升级 GLM；
- auth/rate-limit/transport/timeout 不升级；
- `--model` 覆盖 routes 和 escalation；
- OpenCode 请求携带 request-local model 和原 agent；
- 不可用模型 4xx 不回落默认模型；
- settled event 写入 model、route source、policy digest；历史 event 可 replay；
- agent permission 文件在模型切换前后字节不变。

### 12.2 集成测试

- 两个连续 node 分别解析为 DeepSeek 和 GLM，fake OpenCode transport 捕获不同 model body；
- 同一 task 第一次 DeepSeek 输出非法、第二次 GLM 输出合法，最终成功且 ledger 记录两次不同模型；
- detached 与非 detached 对同一配置产生相同 resolution；
- resume 配置变化时，新旧 attempt policy digest 可区分；
- headless workflow 不应用 OpenCode routing，仍按原有 headless/CLI 模型优先级执行。

### 12.3 Benchmark 验收

在同一代码 commit 和同一五 item fixture 上至少比较：

1. DeepSeek 全程；
2. GLM 全程；
3. 阶段 A 混合路由。

记录每个方案的：workflow 完成率、artifact invalid rate、forbidden-write rate、重试次数、总墙钟时间、按模型 token/费用（可获得时）、case/plan/codegen traceability finding、Improvement 质量。混合路由只有在不降低完成率和语义质量的前提下才能成为 benchmark 默认。

## 13. 成功标准

- 同一 OpenCode workflow 的不同 phase 可以使用不同模型；
- 权限 persona 与模型路由是两个独立 interface；
- DeepSeek 契约失败后下一次有效 retry 自动使用 GLM；
- 配置错误和未知模型 fail closed，不静默降为 OpenCode default；
- 旧项目、单模型 CLI、detached、resume 和 headless 行为兼容；
- 每个 settled agent attempt 的模型选择可审计；
- 五项 benchmark 能比较单模型与混合模型的质量、速度和成本。

## 14. 非目标

- 在同一 phase 内同时调用两个模型投票或合并答案；
- 自动根据 token 价格、余额或实时限额选模型；
- 在模型失败时修改 agent 权限或扩大 write set；
- 将 OpenCode provider 凭据写入 `.aa/config.yaml`；
- 在本设计中把 Cursor Grok 接入 OpenCode，或实现 phase 级跨 adapter 路由；
- 在缺少语义映射 gate 前把 API/E2E/Fuzz/Performance Codegen 默认切给 DeepSeek；
- 改变现有 gate verdict、review topology 或产品代码写策略。
