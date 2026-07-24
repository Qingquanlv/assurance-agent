# AA 接入 OpenCode v2 API 设计

日期：2026-07-23
状态：待用户评审
方案：彻底迁移 driver adapter 到 OpenCode v2 `/api/*` 面；手写 httpx 客户端；SSE 判定 phase 完成；`location.directory` 映射 task workspace（预留 workspace 扩展 seam）；权限以现有 per-agent floor 为准、残留 “ask” 一律 reject。

## 1. 背景

`assurance_agent/workflow/driver/opencode_adapter.py` 当前对接 OpenCode 的 **legacy（非 `/api/`）端点**：

- `POST /session {title, parentID?}` 建会话；
- `POST /session/{id}/prompt_async {parts, model?, agent?}` 异步发提示；
- 轮询 `GET /session/status`，用「持续 idle N 次」（idle-streak 启发式）判定 phase 完成；
- `directory` 通过 query 参数传递。

本地 OpenCode 仓库（`/Users/lvqingquan/jsProject/opencode`，commit `62e4641`，含 `feat(app): add dual-server compatibility`）已提供独立的 v2 `/api/*` 面，语义有实质升级：

| 能力 | legacy（AA 现用） | v2 (`/api/*`) |
|------|------------------|---------------|
| 建会话 | `POST /session {title,parentID}` | `POST /api/session {id?,agent?,model?,location?}` |
| 发提示 | `POST /session/{id}/prompt_async {parts,model,agent}` | `POST /api/session/{id}/prompt {prompt:{text,files,agents},delivery?,resume?}` |
| 等完成 | 轮询 `/session/status` idle-streak | SSE `GET /api/session/{id}/event` 上的 `session.idle` 事件 |
| 目录/工作区 | `?directory=` query | `location.directory` + `x-opencode-directory` header（GET 会被重写成 query） |
| 中断 | `POST /session/{id}/abort` | `POST /api/session/{id}/interrupt` |
| 权限/追问 | 无对应交互 | `/api/session/{id}/permission`、`/api/session/{id}/question` + reply/reject |
| 事件流 | 无（靠轮询） | SSE：`GET /api/event`、`GET /api/session/{id}/event` |
| 模型/agent | prompt body 里带 | 建会话时定，或 `POST .../model`、`.../agent` 单独切 |

约束事实：

- OpenCode 仓库**没有 Python SDK**（只有 JS SDK + VSCode 扩展），AA 是 Python，因此 v2 客户端只能手写 HTTP 或从 `openapi.json` 生成——本设计选择手写。
- legacy 端点在当前版本仍保留（dual-server），迁移可平滑进行，但本设计**不做运行时 v1/v2 协商**，一次性切到 v2。
- graph 侧的 agent seam（`AgentRequest`/`AgentResult`/`AgentInvoker`，见 `assurance_agent/workflow/graph/agent_api.py`）与 `driver → graph` 单向依赖保持不变。

## 2. 已确认的决策

| 决策点 | 结论 |
|--------|------|
| 目标 | 彻底迁移到 v2 `/api/*`，legacy 端点不再维护 |
| 客户端实现 | 手写 httpx，仅实现 AA 需要的约 6 个端点，沿用现有 adapter 风格 |
| 完成检测 | 订阅 per-session SSE（`GET /api/session/{id}/event`），收到 `session.idle` 即返回，`session.error` 即失败 |
| workspace 映射 | 现在用 `location.directory = workspace_root`；在 adapter 内预留 workspace 扩展 seam，未来可升级到 experimental workspace（远程/容器执行） |
| 权限 / 追问 | 现有 `.opencode/agents/*.md` per-agent permission floor 为权威；SSE 对残留走到 “ask” 的 permission/question 一律 `reject`，绝不 auto-allow |

### 2.1 关于 experimental workspace 的调研结论（为何不现在采用）

`packages/opencode/src/control-plane/workspace.ts` 与 `types.ts` 显示：

- experimental workspace 受运行时开关 `OPENCODE_EXPERIMENTAL_WORKSPACES=true` / `flags.experimentalWorkspaces` 控制，默认关闭；
- adapter 的 `target` 只有 `local` 与 `remote` 两类；创建 workspace 会写 DB（`WorkspaceTable`）、绑定 `projectID`、拉起子实例、跑 SSE 事件同步 fiber、事件 replay、断线重连退避；
- 其核心价值是**远程/分布式**：`session warp`（会话迁移）、`/sync/history`、`/sync/replay`、`/sync/steal`、跨机 VCS `diff/apply`；
- 对 `local` 类型，`target` 最终仍是 `{type:"local", directory}`，本质等价于直接传 `directory`；
- `LocationRef` 中 `directory` 为必填，`workspaceID` 可选。

对本机、单实例、无人值守的 AA（已自行物化 task 私有目录），experimental workspace 绕一圈仍落到同一 `directory`，而其杀手锏 AA 用不上。故现在用 `directory`，仅预留升级 seam。

## 3. 架构与范围

**不变**：graph 侧 `AgentInvoker.invoke(AgentRequest) -> AgentResult` 契约完全不动；迁移仅发生在 driver adapter 内部。

**改动面**：

- 重写 `assurance_agent/workflow/driver/opencode_adapter.py`：全部端点切到 v2 `/api/*`，完成检测改为 SSE，删除 idle-streak 常量与 legacy 端点调用。
- `assurance_agent/commands/workflow_cmd.py` 的 `_build_adapter`：`OpenCodeAdapter(...)` 构造参数基本不变（`server/directory/model/parent_session/auth_headers`），内部实现换 v2。
- `run_phase()`（v1 driver 循环路径）与 `invoke()`（graph `AgentInvoker` 路径）都保留，共用新的 v2 内部方法（`_create_session`、`_dispatch_prompt`、`_await_via_sse`、`_interrupt`）。

## 4. 会话生命周期与端点映射

单次 `invoke` 时序：

```
1. POST /api/session                 body: {agent, model?, location:{directory}}   -> {id}
   （若已有 reconnect_session_id，则复用该 id，跳过 create）
2. POST /api/session/{id}/prompt      body: {prompt:{text:<prompt>}, resume?:true}  -> 2xx
3. GET  /api/session/{id}/event (SSE) 阻塞读取，直到 session.idle / session.error
4. 失败或超时                          POST /api/session/{id}/interrupt
```

v2 语义变化要点：

- **model/agent 在建会话时定**：`create` body 的 `agent`（字符串，如 `aa-test-author`）、`model`（`ModelRef{providerID,id}`）。不再放 prompt body。
- prompt body 是 `PromptInput{text, files?, agents?}`，AA 只用 `text`。
- 目录：`create` body 的 `location.directory`；对 POST 类请求同时带 `x-opencode-directory` header（v2 client 对非 GET 用 header 传目录，GET 才重写成 query）。

## 5. 完成检测（SSE）

- prompt 发出后立即打开 `GET /api/session/{id}/event`（`Accept: text/event-stream`），用 httpx `stream()` 逐行解析 SSE 帧（`id:` / `event:` / `data:`）。
- **终止条件**：`data.type == "session.idle"` → 成功返回；`data.type == "session.error"` → typed error（`internal`）。中间 `session.next.*` 流式增量忽略（可选降级为 debug 日志）。
- **断线重连**：SSE 帧带 `id`；重连时用 `?after=<last_event_id>` 续读，避免漏掉终止事件。对应执行契约中的 `reconnect: true`。
- **超时**：以 `request.timeout_seconds` 作为整体 deadline；到点调用 `interrupt` 并返回 `timeout`。
- 删除旧的 idle-streak 猜测逻辑：SSE 有确定性的 `session.idle`，无需「持续 idle N 次」启发式与相关常量（`DEFAULT_IDLE_DONE_STREAK` 等）。

## 6. 权限 / 追问处理

**权威来源 = 现有 per-agent floor**：`.opencode/agents/*.md` frontmatter 的 `permission:` 块（deny-by-default + allow globs + `bash: deny` + `external_directory: deny`），保持不动。示例（`aa-test-author`）：

```yaml
permission:
  edit:
    "**": deny
    "**tests/**": allow
    "**qa/changes/**/codegen/**": allow
    "**qa/changes/**/healing/**": allow
    "**qa/changes/**/workflow-state.yaml": deny
  bash: { "*": deny }
  external_directory: deny
```

**兜底策略 = 配置没放行即拒绝，绝不 auto-allow**：

- adapter 的 SSE 读循环同时监听 `permission.v2.asked` 与 question 相关事件。
- 命中即调用 `POST /api/session/{id}/permission/{requestID}/reply {reply:"reject"}`（`PermissionV2Reply` 枚举为 `once|always|reject`，取 `reject`）；question 走 `POST /api/session/{id}/question/{requestID}/reject`。
- 语义：凡走到 “ask”（即 floor 未确定性放行的动作）一律拒绝，既不死锁也不越权。
- 每次拒绝记结构化日志（action / resource / sessionID），便于事后发现 floor 配置缺口。

## 7. workspace 映射（directory now + 预留 seam）

- 现在：`create` body 传 `location:{directory: str(request.workspace_root)}`；POST 请求带 `x-opencode-directory` header。语义等同现状，隔离由 AA 自行物化的 task 私有目录保证。
- **预留扩展点**：adapter 内抽 `_location_ref(request) -> dict`，默认返回 `{"directory": ...}`。未来接远程/容器执行时，仅需让它先 `POST /experimental/workspace` 建 workspace 并返回 `{directory, workspaceID}`，上层调用不变。
- 不引入实验开关、不建 DB workspace、不碰 sync/warp。

## 8. model / agent / reconnect / 错误分类

- **agent**：`request.agent`（如 `aa-test-author`）→ `create` body 的 `agent`，一会话一 agent。
- **model**：显式 `--model "provider/model"` → `ModelRef{providerID,id}`；未指定则省略，让 server 用默认（保持现有行为）。沿用现有 `parse_model` 校验，输出结构从 `{providerID,modelID}` 调整为 v2 的 `ModelRef{providerID,id}`。
- **reconnect**：`request.reconnect_session_id` 存在 → 跳过 create、复用该会话，prompt 带 `resume:true`；返回 `AgentResult.session_id` 供下次 reconnect。
- **错误分类**（沿用 `ErrorKind`）：401/403 → `auth`，429 → `rate_limit`，`session.error` 事件 → `internal`，httpx 传输错误 → `transport`，deadline → `timeout`。响应 `content-type: text/html` → 视为「server 不支持 v2」的显式错误（参考 v2 client 的同款检查）。

## 9. 配置引导、测试、迁移

**benchmark / 文档**：

- `benchmark/vue-fastapi-admin/benchmark/run-workflow-loop.sh`、`benchmark.env`、`assurance_agent/_resources/opencode/INSTALL.md`、`README.md` 中 opencode server 的启动/版本说明更新为 v2（注明支持 `/api/*` 的 server 版本下限）。
- server 启动命令不变（`opencode serve --port 4096`），仅补充最低版本要求。

**测试**（`tests/unit/driver/test_opencode_adapter.py` 重写）：

- 用假的 httpx transport 模拟：`POST /api/session`、`POST prompt`、SSE 流。
- 覆盖：`session.idle` 成功、`session.error` 失败、`permission.v2.asked` → reject、断线 `after` 续读、timeout → interrupt、401/403/429 分类、`text/html` → 显式错误。
- `tests/unit/workflow/graph/test_task_runner.py` 中的 v1→v2 shim 路径同步更新。

**迁移**：一次性替换，无双轨；删除 legacy 端点调用、idle-streak 常量。

## 10. 成功标准

- opencode adapter 全部走 `/api/*`；
- phase 完成由 `session.idle` 判定，失败由 `session.error` 判定；
- 无人值守跑不因权限死锁（未放行即 reject）；
- directory 隔离行为与现状一致；
- 单测覆盖成功/错误/权限/断线/超时/版本不符；
- benchmark 端到端跑通。

## 11. 待评审 / 开放问题

- 是否也需要同步迁移 `run_phase()`（v1 driver 循环）路径，还是仅保留 `invoke()`（graph）路径为一等公民？（本设计假定两者共用 v2 内部方法一并迁移。）
- benchmark 的 `headless` adapter 不受本设计影响，确认无需改动。
- server 版本下限的具体号（依部署的 OpenCode 版本确定，如 ≥ 1.18.x）。
