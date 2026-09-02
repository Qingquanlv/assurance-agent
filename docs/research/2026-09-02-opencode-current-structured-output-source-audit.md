# OpenCode 当前 Structured Output 源码审计

日期：2026-09-02

## 审计基线

本报告只使用 OpenCode 官方仓库和官方仓库内文档。审计开始时，GitHub 仓库默认分支是 `dev`，`HEAD` 为：

- commit [`8e0f1c253b6b7292b419505af849d06747c0e049`](https://github.com/anomalyco/opencode/commit/8e0f1c253b6b7292b419505af849d06747c0e049)，提交时间 2026-09-01 21:52:12 UTC；
- `packages/opencode/package.json` 的包版本为 [`1.18.26`](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/package.json#L1-L4)。
- 对应官方 release tag 是 [`v1.18.26 / 774cc7c1914e4329eefde5a669f938b0cf566661`](https://github.com/anomalyco/opencode/releases/tag/v1.18.26)；本报告涉及的 Structured Output、V1 message route、storage 和 V2 prompt 文件在该 tag 与随后同步版本号的 `dev` HEAD 之间没有差异。

下文所有源码链接都固定到该 commit，不使用会漂移的 `dev` 链接。

## 结论

**OpenCode 现在已经有 JSON Schema Structured Output，不需要等待上游新增这个功能。** 它的工作方式是：调用方在 V1 Session prompt 中传入 `format.type=json_schema` 和 `format.schema`；OpenCode 把 schema 动态注册成一个名为 `StructuredOutput` 的函数工具，要求模型调用这个工具，再把通过工具参数校验的对象写入 Assistant message 的 `info.structured`。

**但官方 v1.18.26 release 仍不能通过本项目最前置的无模型 message round-trip Gate。** 对 release binary 的协议探测复现了官方仍为 open 的 [#26929](https://github.com/anomalyco/opencode/issues/26929)：`prompt_async + noReply + format.json_schema` 返回 204，随后 V1 message list 和 single-message 都返回 400 `Expected OutputFormatJsonSchema`。V2 message API 不能绕行：V2 list 为空，按同一 message ID 查询为 404，而且 V2 prompt 本来就不接受 `format`。

但它不是一个可以直接宣称“事务级可恢复”的完整边界：

1. 它不是 Provider 原生 `response_format`，而是 OpenCode 的工具调用协议；选定的 Provider/模型必须支持 function tool 和 `toolChoice=required`。
2. `retryCount` 虽然出现在请求 Schema、生成 SDK 和官方文档里，当前生产逻辑没有读取它；缺少结构化结果时错误仍固定记录 `retries=0`。
3. 工具参数先作为 completed `StructuredOutput` ToolPart 持久化，之后才复制到 `AssistantMessage.structured`，两者之间有真实崩溃窗口。
4. V1 Session 的 active runner 和 status 是进程内状态，进程重启不会自动续跑 in-flight structured prompt。
5. 已持久化的 `info.structured` 可以通过 message API 重读；稳定 `messageID` 只是相关性标识和行 upsert 键，不是 prompt dispatch 幂等键。
6. OpenCode 的官方文档示例仍读取不存在于当前生成类型中的 `info.structured_output`；正确字段是 `info.structured`。
7. 因为 v1.18.26 无法从正式 message API 重读包含 format 的 V1 message，当前部署资格必须保持 `schema-capability-red`；这不是 waiver，也不是 Provider 集成测试可以掩盖的问题。

因此，本项目应该实施和认证 `opencode_structured_output`，而不是等待 `provider_schema=True`；同时必须保留 Kernel 的独立 Schema 验证、one-POST dispatch journal、崩溃窗口分类和版本/Provider/Schema 矩阵 Gate。

## 1. 请求类型和 JSON Schema 定义

### 1.1 V1 请求格式

OpenCode 定义了两个输出格式：

```json
{ "type": "text" }
```

或：

```json
{
  "type": "json_schema",
  "schema": { "type": "object" },
  "retryCount": 2
}
```

`schema` 的运行时输入类型只是 `Record<string, any>`；`retryCount` 是非负整数、解码默认值为 2。[V1 Format 定义](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/schema/src/v1/session.ts#L65-L79) 生成 SDK 对应类型也是 `{type: "json_schema", schema: JsonSchema, retryCount?: number}`，其中 `JsonSchema` 仍是任意键值对象。[生成 SDK 类型](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/sdk/js/src/v2/gen/types.gen.ts#L223-L237)

这意味着 OpenCode 的 HTTP 解码层只证明 `schema` 是一个 JSON object，并不对它运行 JSON Schema meta-schema 校验。实现还会移除 schema 顶层的 `$schema`，再把剩余对象按 TypeScript `JSONSchema7` 交给 AI SDK；源码没有做 Draft 规范化、关键字白名单、引用闭包检查或 schema 大小限制。[StructuredOutput 工具构造](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L1564-L1591)

所以调用方仍应在发送前：

- 验证并摘要 exact schema document；
- 限制 schema 字节数、深度和项目允许的 JSON Schema 子集；
- 把 schema digest 和 exact OpenCode 版本纳入认证记录。

### 1.2 `format` 的存储位置

`format` 是 User message 的一部分。[User message Schema](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/schema/src/v1/session.ts#L327-L355) 创建 prompt 时，OpenCode 把 `input.format` 原样放入 User message 并持久化。[User message 创建](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L635-L670)

结果不写回 User message。Assistant message 有一个独立的可选字段 `structured?: any`。[Assistant message Schema](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/schema/src/v1/session.ts#L453-L488) 该字段本身是 `Schema.Any`，所以从数据库重读时不会再次按原始业务 schema 校验。

### 1.3 v1.18.26 release 的无模型协议探测

为区分“源码表面存在”与“发布物可被 Adapter 使用”，本次对官方 `v1.18.26 / 774cc7c` 运行了不调用模型或 Provider 的最小探测：

1. 创建 V1 session；
2. 给调用方指定稳定 message ID；
3. `POST /session/{id}/prompt_async`，body 包含 `noReply: true`、一个最小 object schema 和 `format.type=json_schema`；
4. admission 返回 HTTP 204；
5. `GET /session/{id}/message` 返回 HTTP 400；
6. `GET /session/{id}/message/{messageID}` 同样返回 HTTP 400；
7. 两个错误的核心都是 `Expected OutputFormatJsonSchema, got {...}`；
8. 尝试读取 `/api` V2 message list 得到空列表，按 V1 message ID 读取 V2 single-message 得到 404。

`noReply: true` 在源码中会在持久化 User message 后直接返回，不启动 LLM loop。[noReply 分支](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L1052-L1071) 因此该失败与模型、Provider tool calling、Schema 复杂度或最终 Assistant output 无关；它直接证明 v1.18.26 的正式 V1 admission→durable message→read 协议不闭合。现象与 OpenCode 官方仍为 open 的 [#26929](https://github.com/anomalyco/opencode/issues/26929) 完全一致。

这条探测应成为项目 Gate 的第一个 fail-fast 测试。它未通过时，不应继续消耗 Provider 配额运行 33-contract matrix，也不能发布 `opencode_structured_output=True`。

## 2. StructuredOutput 工具如何创建和执行

### 2.1 动态注入，而不是普通已安装插件

每轮 Session loop 先解析普通工具；如果最近的 User message 使用 `json_schema`，OpenCode 随后动态追加 `tools["StructuredOutput"]`，并把成功回调绑定到该次 loop 的局部变量 `structured`。[动态注入](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L1221-L1250)

同时，OpenCode：

- 给 system prompt 加入“必须使用 StructuredOutput、不得用普通文本回答”的指令；
- 把 `toolChoice` 设成 `required`；
- 工具说明要求在完成其他调用后、最终只调用一次。[工具说明与系统提示](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L74-L82) [调用设置](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L1264-L1286)

“只调用一次”目前是提示词要求，不是服务端计数约束。`onSuccess` 只是给局部变量重新赋值；代码没有拒绝同一流中的第二个成功 `StructuredOutput` 调用。[工具成功回调和执行](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L1243-L1249) [工具 execute](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L1572-L1589) 因此调用方仍应把多个成功候选视为冲突，而不能默默采用最后一个。

### 2.2 Schema 验证在哪里发生

工具的 `inputSchema` 是 AI SDK 的 `jsonSchema(toolSchema)`。只有 AI SDK 接受工具参数后，OpenCode 的 `execute(args)` 才运行，并调用 `onSuccess(args)`。[工具实现](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L1564-L1591)

OpenCode 自己没有在 `execute` 里再次运行独立 validator；它只返回通用成功文本和 `metadata: {valid: true}`。官方单元测试也明确把“execute 前由 AI SDK 校验”作为预期，但所谓缺字段/类型错误测试只检查生成的 `inputSchema`，并没有实际执行一个无效工具调用。[上游单元测试](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/test/session/structured-output.test.ts#L165-L278)

当 AI SDK 发现工具调用无法按 schema 解析时，OpenCode 的 `experimental_repairToolCall` 会把失败调用改写成 `invalid` 工具及错误说明。[repair hook](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/llm.ts#L294-L312) `invalid` 工具把错误反馈给模型。[invalid tool](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/tool/invalid.ts#L4-L20) Tool error/result 则由 processor 持久化到 ToolPart。[processor tool result/error](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/processor.ts#L383-L418)

这可以让模型在普通会话循环里看到错误并尝试另一次调用，但不是由 `format.retryCount` 控制的、可审计的结构化重试协议。

### 2.3 权限过滤会影响 StructuredOutput

StructuredOutput 虽然在普通工具解析之后动态追加，但进入 LLM 前，`LLMRequestPrep.prepare` 会再次对完整工具集合执行 agent/session permission 过滤。[二次工具过滤](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/llm/request.ts#L148-L158) [过滤实现](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/llm/request.ts#L208-L214)

因此，类似 `"*": deny` 的规则可以把动态工具删掉，除非在合并顺序靠后的位置显式允许 `StructuredOutput`。此外，V1 prompt 的 deprecated `tools` 布尔 map 一旦非空，会被转换为一组 permission rules 并整体替换 Session permission。[prompt tools 到 permission 的转换](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L1052-L1067) Session create 本身支持完整 permission rules。[Session CreateInput](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/session.ts#L260-L271)

本项目的 Adapter 不应只在 prompt body 里随手追加一个不完整的 `tools` map；认证测试必须使用生产一致的完整 permission 配置，并证明 `StructuredOutput` 在 broad-deny 下仍被明确允许。

## 3. Session prompt 和 processor 如何形成终态

`runLoop` 为一次执行初始化局部变量 `let structured: unknown`。[loop 初始化](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L1081-L1096) StructuredOutput 工具成功时只先更新这个内存变量。processor 继续消费完整模型流、结算 ToolPart、记录 step finish，并在 cleanup 中给 Assistant message 写 `time.completed`。[step finish 持久化](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/processor.ts#L435-L470) [cleanup 完成时间](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/processor.ts#L553-L610)

`handle.process(...)` 返回后，prompt loop 才执行：

```text
handle.message.structured = structured
updateMessage(handle.message)
break
```

[最终 structured 写入](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L1288-L1293)

所以可靠成功条件应至少包括：

- 是与目标 User message 对应的 Assistant message；
- `time.completed` 已存在；
- `error` 不存在；
- `structured` 存在且是预期 JSON 类型；
- 本地重新按锁定 schema 验证通过；
- 候选摘要与 durable observation 记录一致。

普通文本、text part 中的 JSON、文档所写的 `structured_output`、任意普通 tool output 都不能作为正常成功路径。

## 4. 失败语义和 `retryCount`

### 4.1 `retryCount` 当前没有生产语义

在固定 commit 中，`retryCount` 出现在 V1 Schema、生成 SDK、文档和测试里，但 Session 生产执行只读取 `format.schema`，没有读取 `format.retryCount`。[Format 定义](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/schema/src/v1/session.ts#L69-L73) [实际工具创建](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L1243-L1249)

如果模型以普通终止原因结束且没有产生 StructuredOutput，OpenCode 写入：

```json
{
  "name": "StructuredOutputError",
  "data": {
    "message": "Model did not produce structured output",
    "retries": 0
  }
}
```

[缺少 structured 的失败分支](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L1295-L1316) `StructuredOutputError` 的类型也是 `{name, data:{message,retries}}`，不是文档示例中的扁平 `.error.message`/`.error.retries`。[错误 Schema](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/schema/src/v1/session.ts#L43-L47) [生成 SDK 类型](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/sdk/js/src/v2/gen/types.gen.ts#L287-L300)

因此当前 Adapter 省略 `retryCount` 是正确的。项目 Gate 必须自己覆盖 invalid arguments、没有调用工具、重复调用、大小写修复、output limit、content filter 和不支持 required tool choice 的 Provider/模型，不能把文档中的默认 2 次重试当作保证。

### 4.2 其他失败

Assistant error 联合还包括 Provider auth、unknown、output length、abort、context overflow、content filter 和 API error。[Assistant error 联合](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/schema/src/v1/session.ts#L385-L395) 内容过滤会被显式转为 `ContentFilterError`；缺 structured 的分支则只更新 Assistant message，没有像内容过滤分支那样额外发布 `session.error`。[终态分支](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L1295-L1316)

所以异步观察器不能只监听 `session.error`；必须观察/轮询目标 Assistant message 的 `info.error` 与 `info.structured`。

Provider transport 的一般重试由独立 `SessionRetry` policy 处理，最多 5 次，并不读取 Structured Output 的 `retryCount`。[processor retry policy 调用](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/processor.ts#L641-L695) [SessionRetry policy](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/retry.ts#L26-L40) [重试上限和调度](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/retry.ts#L183-L206)

## 5. HTTP 和 SDK 返回形状

### 5.1 同步调用

同步入口是 `POST /session/{sessionID}/message`。HTTP handler 等待 `promptSvc.prompt(...)` 完成并以 JSON stream 返回整个 message envelope。[同步 handler](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts#L295-L309)

生成 SDK 的 200 类型是：

```text
{
  info: AssistantMessage,
  parts: Part[]
}
```

[同步请求/响应类型](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/sdk/js/src/v2/gen/types.gen.ts#L9794-L9844) `AssistantMessage.structured?: unknown` 是唯一正式 structured 字段。[Assistant SDK 类型](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/sdk/js/src/v2/gen/types.gen.ts#L333-L374)

### 5.2 异步调用

异步入口是 `POST /session/{sessionID}/prompt_async`，请求使用同一个 `PromptPayload`，成功只返回 HTTP 204；实际 prompt 在后台 fiber 中执行。[路由定义](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/server/routes/instance/httpapi/groups/session.ts#L316-L341) [异步 handler](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts#L311-L329) [异步 SDK 类型](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/sdk/js/src/v2/gen/types.gen.ts#L10141-L10188)

204 只表示 admission，不代表结构化结果成功。调用方必须通过：

- `GET /session/{sessionID}/message`；或
- `GET /session/{sessionID}/message/{messageID}`；或
- `message.updated`/`message.part.updated` 事件后再进行权威重读

观察结果。message list 和 single-message 都返回 `{info, parts}`。[message routes](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/server/routes/instance/httpapi/groups/session.ts#L179-L201) [SDK message list/single 类型](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/sdk/js/src/v2/gen/types.gen.ts#L9755-L9792) [single-message 类型](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/sdk/js/src/v2/gen/types.gen.ts#L9885-L9921)

上述是源码和生成 API 的目标契约；官方 v1.18.26 release 的实际 round-trip 探测却在两个 V1 read endpoint 都返回 400。因此计划不能仅靠生成 SDK 类型判定能力存在，必须以 release binary 的读写回归测试为资格事实。

### 5.3 官方文档与当前类型不一致

官方仓库的 SDK 文档仍示例：

```typescript
result.data.info.structured_output
```

并声称 `retryCount` 会执行结构化验证重试。[官方文档源码](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/web/src/content/docs/sdk.mdx#L120-L188) 当前生成 SDK 和运行时代码实际字段都是 `structured`，当前失败实现也固定 `retries=0`。项目集成应以固定 commit 的生成类型和实际 payload 为准，不应兼容猜测 `structured_output` 后继续执行。

## 6. 持久化、resume 和崩溃恢复

### 6.1 已写入的 `info.structured` 是持久的

`Session.updateMessage` 发布 durable `MessageUpdated` event。[updateMessage](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/session.ts#L629-L643) projector 把 message data upsert 到 SQLite `MessageTable`。[Message projector](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/core/src/session/projector.ts#L260-L272) durable event、projector 和 event row 位于同一数据库事务中。[event durable transaction](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/core/src/event.ts#L236-L353)

message API 从该 SQLite projection 读取并还原 `info` 与 `parts`。[message hydration](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/message-v2.ts#L80-L123) [single-message read](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/message-v2.ts#L506-L519)

因此，若 `handle.message.structured` 的最终 `updateMessage` 已经返回，即使同步 HTTP response 丢失或 Kernel 尚未保存自己的 result，也可以通过目标 session/message 重读同一 structured candidate。

这里的“可以重读”是 storage/projector 层的设计结论。v1.18.26 的 HTTP 反序列化 bug 使这条能力目前无法通过正式 V1 message API可靠消费；所以在 #26929 对应行为修复并通过 exact release Gate 之前，不能据此给生产 Adapter 标绿。

### 6.2 `info.structured` 写入前存在真实 crash seam

ToolPart 的 completed state 包含模型工具参数 `input`、字符串 output、title、metadata 和结束时间。[ToolStateCompleted Schema](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/schema/src/v1/session.ts#L277-L301) processor 在收到 tool result 时先把 StructuredOutput ToolPart 更新为 completed；该工具的通用 metadata 是 `{valid:true}`。[ToolPart 完成](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/processor.ts#L160-L184) [StructuredOutput 返回值](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L1572-L1589)

随后 processor 还会写 step finish 和 completed Assistant；最后 prompt loop 才把局部 `structured` 复制进 Assistant message。[processor step finish](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/processor.ts#L435-L470) [structured 后置写入](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L1288-L1293)

所以存在以下顺序：

```text
StructuredOutput args 通过 AI SDK tool schema
→ completed ToolPart(state.input, metadata.valid=true) durable
→ Assistant finish/completed durable
→ [可崩溃]
→ Assistant.info.structured durable
```

如果项目坚持“正常成功只接受终态 `info.structured`”，该窗口必须返回 `indeterminate`，不能声称 OpenCode 会自动恢复。如果希望从这个窗口恢复，必须单独批准一个 recovery-only 规则：只接受唯一 completed `StructuredOutput` ToolPart，绑定 exact session、User/Assistant parent、schema digest 和 message ID，再由 Kernel 重新校验其 `state.input`；零个、多个或身份不一致都 fail closed。这个规则属于本项目 Adapter/Kernel 的恢复协议，不是 OpenCode 已提供的事务保证。

### 6.3 V1 没有 durable auto-resume

V1 `SessionRunState` 把 active runner 保存在 InstanceState 内的 `Map`；scope 结束时取消 runner 并清空 Map。[V1 runner 状态](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/run-state.ts#L35-L68) `SessionStatus` 同样只是进程内 Map，不存在条目时直接返回 `idle`。[V1 status](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/status.ts#L21-L48)

进程重启后，上游不会扫描未完成的 V1 structured prompt 自动续跑，且旧 busy 状态会表现为 idle。Adapter 不能把“重启后 status=idle”当成终态成功或安全失败。

`runLoop` 每次重新启动还会把局部 `structured` 初始化为 `undefined`，不会从 persisted ToolPart 或 Assistant message恢复该变量。[runLoop 初始化](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L1081-L1096) 它的早退逻辑也没有把 `AssistantMessage.structured` 当成独立终态标记，而是按 finish reason 和 tool parts 判断是否继续。[loop 早退逻辑](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L1100-L1130) 因此恢复观察器应只读既有 message；不能通过再次启动 loop 来“观察”结果。

### 6.4 稳定 `messageID` 不是 dispatch 幂等键

PromptInput 允许调用方指定 `messageID`。[PromptInput](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L1499-L1521) 它被用于 User message ID，SQLite projector 对相同 message row 做 upsert。[User message ID](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L656-L670) [projector upsert](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/core/src/session/projector.ts#L260-L272)

但每次 `prompt(...)` 仍会在保存 User message 后调用 `loop(...)`。[prompt dispatch](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/prompt.ts#L1052-L1071) 再次 POST 相同 messageID 并不是“只返回第一次结果”，仍可能再次执行模型和工具。

本项目必须拥有自己的 one-POST dispatch journal：

- POST 明确未被接受，可以按受控规则重试；
- POST 已返回 204，只能观察已绑定 session/message；
- request accepted/response lost 时，先通过稳定标识发现既有 User message，不能直接重新 POST；
- 无法证明是否接受、且无法发现同一 activity 时返回 indeterminate。

### 6.5 不要混淆 V1 Structured Output 与 V2 resume

当前新的 V2 `/api/session/{sessionID}/prompt` 确实有 `resume?: boolean`，并把 prompt admission 做成 durable session input。[V2 prompt API 类型](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/sdk/js/src/v2/gen/types.gen.ts#L11553-L11597) [V2 prompt admission/resume](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/core/src/session.ts#L360-L385)

但 V2 `PromptInput` 只有 `text`、`files` 和 `agents`，没有 `format` 或 JSON Schema。[V2 PromptInput](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/sdk/js/src/v2/gen/types.gen.ts#L2708-L2712) 当前上游不存在“V2 durable resume + V1 JSON Schema Structured Output”的统一 API。不能把 V2 的恢复保证套到 V1 structured prompt 上。

## 7. 是否依赖 Provider 原生 `response_format`

不依赖。

默认 LLM 路径调用 AI SDK：

```text
streamText({
  tools: prepared.tools,
  toolChoice: input.toolChoice,
  ...
})
```

没有给该 structured path 传 `response_format`、`generateObject` 或 `streamObject`。[默认 LLM 调用](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/llm.ts#L271-L354)

实验性 native runtime 也只把 `tools` 和 `toolChoice` 下沉到统一 LLM request。[native runtime 请求](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/llm/native-runtime.ts#L74-L113) 对 OpenAI、Azure 和 Mantle，request preparation 甚至把每个函数工具的 `strict` 显式设置成 `false`。[strict=false](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/src/session/llm/request.ts#L148-L158)

正确的能力关系是：

```text
Agent contract requires_structured_output
    → Adapter/runtime 具备并经认证的 opencode_structured_output
    → exact Provider/model 能接受 function tools + required tool choice
    → AI SDK/OpenCode tool 参数校验成功
    → Kernel 再按锁定业务 schema 独立验证
```

不能把它命名回 `provider_schema`，也不能通过设置 `provider_schema=True` 来解锁生产入口。

## 8. 上游测试覆盖不足以替代项目认证

OpenCode 自带的 live structured integration 只在设置 `ANTHROPIC_API_KEY` 时运行，否则 skip；它直接调用 Session service，而不是 HTTP async 协议。[live test gate](https://github.com/anomalyco/opencode/blob/8e0f1c253b6b7292b419505af849d06747c0e049/packages/opencode/test/session/structured-output-integration.test.ts#L12-L66)

当前上游测试没有同时证明：

- `prompt_async` 204 后的 list/single-message 往返；
- server process restart；
- completed ToolPart 与 `info.structured` 之间的 crash cut；
- stable messageID 的 one-POST 恢复；
- invalid tool args 的确定性失败/重试上限；
- 多个 StructuredOutput 调用的冲突规则；
- broad-deny permission 下 StructuredOutput 仍可用；
- 项目 33 个 Agent schema、exact Provider/model 和结果大小矩阵。

此外，官方仓库文档与生成 SDK 的字段名、错误形状和 `retryCount` 行为已经不一致。因此项目自己的 certification Gate 仍是生产切流前置条件，而不是对上游已有功能的重复实现。

## 9. 对当前计划的直接修正建议

### 必须修正

1. 把“等待 OpenCode 提供 Structured Output”改成“实现并认证现有 V1 OpenCode Structured Output”。
2. 把源码审计基线钉为 `dev 8e0f1c253b6b7292b419505af849d06747c0e049`，把实际发布物基线钉为 `v1.18.26 / 774cc7c1914e4329eefde5a669f938b0cf566661`；记录后者为已实测的 `message-roundtrip-red`，不能自动标绿。
3. 保持新能力名 `opencode_structured_output`，不要恢复 `provider_schema`。
4. 请求发送 `format: {type:"json_schema", schema}`；继续省略 `retryCount`。
5. 正常成功只读 terminal `info.structured`，并由 Kernel 独立验证；不接受 `structured_output`、文本 JSON 或普通 tool output。
6. 明确 one-POST dispatch journal；stable messageID 只作 correlation，不作 OpenCode 幂等保证。
7. 把 V1 in-flight server restart 定义为不具备上游自动恢复；无法证明终态时返回 pending/indeterminate，不得重新 POST。
8. 在崩溃矩阵中新增 `completed StructuredOutput ToolPart durable → info.structured 尚未 durable`。
9. 选择其一并写死：
   - 保守方案：该 crash seam 一律 indeterminate；或
   - recovery-only 方案：唯一 completed StructuredOutput ToolPart + `metadata.valid=true` + exact identity + Kernel schema revalidation，任何歧义 fail closed。
10. Gate 增加 production-equivalent permission 测试，证明 StructuredOutput 不会被 wildcard deny 二次过滤。
11. 明确区分 V1 Structured Output 和 V2 durable resume；不得借用 V2 保证。
12. 把 `noReply + minimal json_schema → V1 list read → V1 single read` 放在所有 Provider/model matrix 之前；任一步不是 204/200/200 即整版 qualification 失败。
13. 明确 `/api` V2 message API 不是 V1 structured message 的 fallback；禁止在 V1 400 时切到 V2 猜测结果。
14. 如果项目仍不授权修改或 fork OpenCode 源码，则 Gate 只能等待一个修复 message round-trip 的上游 release，再对 exact binary 重跑；不能通过 Adapter 宽松解析、跳过 message read 或 waiver 解锁剩余入口。

### 可以保留

- Adapter 发送真实 schema、读取 `info.structured`；
- schema/result 大小限制；
- exact server/provider/model/schema digest 的资格矩阵；
- Kernel 本地验证与确定性 artifact materialization；
- 204 不是 completion；
- terminal error matrix；
- server/message 重读与 response-lost 恢复；
- capability 未通过前剩余 Agent 入口保持 legacy；
- 不伪造 `provider_schema=True`。

## 最终判断

OpenCode 上游源码功能已经足以让本项目实现 Structured Artifact Pipeline 的 Adapter/Kernel 代码；阻塞点不是“上游还没做 JSON Schema”。但是，官方 v1.18.26 发布物仍存在确定的 V1 message round-trip 阻断，因此它不能成为生产切流版本。若不修改 OpenCode 源码，必须等待并认证修复该缺陷的后续 release。

即使后续 release 修复 message read，当前实现也不能支撑“任意崩溃后都自动续跑并得到 `info.structured`”的表述。计划只有在明确 one-POST、V1/V2 边界、ToolPart→Assistant crash seam、indeterminate 策略以及 exact release binary Gate 后，才是闭合的。
