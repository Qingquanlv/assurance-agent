# Claude Code Skill 加载模型与 Effect 绑定简化

日期：2026-09-03

## 结论

Claude Code Skills 值得借鉴的是它的**单一静态定义 + 调用时上下文**，不是它的安全或持久化语义：

```text
installed plugin / skills directory
        │
        ├── 自动发现一个 SKILL.md
        ├── 常驻 name + description
        └── 触发时加载同一份 body，并注入 arguments / tools / environment
```

Claude Code 没有为一个 Skill 再建立“定义 registry → factory registry → runtime registry”三套对象。Agent SDK 更明确说明，Skill 是文件系统工件，并不存在 programmatic registration API。[Claude Agent SDK Skills](https://code.claude.com/docs/en/agent-sdk/skills#how-skills-work-with-the-agent-sdk)

对应到本项目，建议保留一个受安装来源和 ProductLock 认证的 `EffectRegistration`，让其静态 `handler` 在执行时接收 Product 提供的 `EffectRuntimeContext`。不要引入 `EffectRuntimeFactory`、`EFFECT_FACTORY`、`effect_factory_registry` 和第二套 `runtime_effect_registry`。

这只是装配形状的借鉴。Claude Code Skills 不提供 durable store、幂等、reconcile、receipt、崩溃恢复或事务提交，不能替代 `AttemptEffectSettler` 和 Kernel 的 Effect 协议。

## 调研范围

只使用以下一手资料：

- Claude Code 官方 Skills 文档；
- Claude Agent SDK 官方 Skills 文档；
- Claude Code 官方 Plugin 文档和 reference；
- Anthropic `claude-code` 官方仓库中的 `plugin-dev` Skill 编写说明。

## 1. Claude Code 如何发现、注册、加载和调用 Skill

### 1.1 发现

Skill 的入口固定为 `<skill>/SKILL.md`。Claude Code 从 enterprise、personal、project 和已启用 plugin 的约定目录发现 Skill；plugin Skill 使用 `plugin-name:skill-name` 命名空间，普通同名 Skill 则按 enterprise、personal、project 的优先级解析。[Claude Code Skills：Where skills live](https://code.claude.com/docs/en/slash-commands#where-skills-live)

Plugin 安装或启用后，标准 `skills/<name>/SKILL.md` 会自动发现。只有非标准路径才需要 manifest 的 `skills` 字段；plugin manifest 主要提供 plugin identity、namespace、version 和展示元数据。[Claude Code Plugins](https://code.claude.com/docs/en/plugins) [Plugins reference：Skills](https://code.claude.com/docs/en/plugins-reference#skills)

这意味着标准路径本身就是注册协议，不需要每个 Skill 再向一个 factory registry 注册。

### 1.2 “注册”

常规 Claude Code 会把发现到的 Skill 暴露给 Skill tool；Agent SDK 的 `skills` option 只是按名称筛选本次 session 可以调用哪些已发现 Skill。官方文档明确写明，SDK 没有 programmatic Skill registration API。[Claude Agent SDK Skills](https://code.claude.com/docs/en/agent-sdk/skills#how-skills-work-with-the-agent-sdk)

因此，Claude Code 的 registration 是：

```text
enabled source + conventional path + stable name
```

而不是：

```text
descriptor -> abstract factory -> returned runtime implementation -> second registry
```

### 1.3 加载

常规 session 只先把 Skill 的 name/description 暴露给模型；完整 `SKILL.md` 在 Skill 被调用时才进入上下文。引用资料和脚本继续按需读取或执行。Anthropic 官方 `plugin-dev` Skill 将其概括为 metadata、SKILL body、bundled resources 三层 progressive disclosure。[Claude Code Skills：Skill content lifecycle](https://code.claude.com/docs/en/slash-commands#skill-content-lifecycle) [Anthropic plugin-dev Skill source](https://github.com/anthropics/claude-code/blob/main/plugins/plugin-dev/skills/skill-development/SKILL.md)

加载后仍然是同一份 Skill 定义，没有生成一个新的“runtime Skill type”。变化的是调用上下文，例如 arguments、动态命令结果、当前目录和工具权限。

### 1.4 调用

默认情况下，用户可以用 `/name` 显式调用，Claude 也可以按 description 自动调用。`disable-model-invocation: true` 可以把有副作用的工作流改为只能由用户触发。[Claude Code Skills：Control who invokes a skill](https://code.claude.com/docs/en/slash-commands#control-who-invokes-a-skill)

`allowed-tools` 只是当前 turn 的预授权，不会限制其余工具；后续 turn 还会清除该 grant。官方文档还指出，若必须确定性地约束行为，应使用 hooks，而不能依赖模型持续遵循 Skill 内容。[Claude Code Skills：Pre-approve tools](https://code.claude.com/docs/en/slash-commands#pre-approve-tools-for-a-skill) [Claude Code Skills：Skill content lifecycle](https://code.claude.com/docs/en/slash-commands#skill-content-lifecycle)

## 2. Skills 真正保证什么

| 能力 | Claude Code Skills 的保证 |
| --- | --- |
| 定义位置 | 约定目录中的 `SKILL.md` |
| identity | directory/frontmatter name；plugin Skill 带 namespace |
| 可见性 | source scope、plugin enablement 和 session `skills` filter |
| 加载时机 | metadata 先加载，body 触发后加载 |
| 调用入口 | 用户 `/name` 或模型 Skill tool |
| 简单权限控制 | 可禁止模型自动调用；可做单 turn tool 预授权 |
| 资源组织 | references、scripts、assets 按需使用 |

这些保证解决的是“哪份说明在什么时候进入 agent 上下文”，不是 Effect transaction。

## 3. Skills 不保证什么

Claude Code 公开的 Skill contract 没有定义以下内容：

- durable state store 或 store schema；
- handler/factory 的密码学 provenance；
- effect admission、settlement key 或 business key；
- exactly-once / at-most-once external mutation；
- apply 与 reconcile 状态机；
- crash cut、fencing 或 replay；
- atomic receipt publication；
- Skill 成功等同于业务 side effect 已提交。

另外，`allowed-tools` 是临时 grant，并非 deny-based capability boundary；Skill body 是模型指令，并非确定性 validator。官方把确定性 enforcement 指向 hooks，也印证了这一边界。[Claude Code Skills](https://code.claude.com/docs/en/slash-commands)

所以不能把 `EffectRegistration` 直接做成 Markdown Skill，也不能因为 plugin 已启用就删除当前 ProductLock、Kernel journal、Effect store 和 receipt 校验。

## 4. 对当前 Effect seam 的直接借鉴

### 4.1 保留一个静态 registry

保持当前单一贡献路径：

```python
@dataclass(frozen=True, slots=True)
class EffectRegistration:
    kind: str
    intent_schema_id: str
    receipt_schema_id: str
    policy: EffectPolicy
    handler: DurableEffectHandler
```

`PluginContribution.effects`、`FrozenComposition.effect_registry` 和 ProductLock 继续认证这一份 registration 及其 `apply` / `reconcile` executable。不要把它改名为 factory registry，也不要让 factory 返回第二个需要重新认证的 handler。

这对应 Claude Code 的 `SKILL.md`：安装时确定 identity 和静态内容，调用时仍然使用同一份定义。

### 4.2 把 durable store 作为运行时上下文传入

把当前 handler 捕获的 `InMemoryHealingStore` / `InMemoryImprovementStore` 改为调用参数：

```python
class DurableEffectHandler(Protocol):
    async def apply(
        self,
        intent: EffectIntent,
        settlement_key: str,
        runtime: EffectRuntimeContext,
    ) -> EffectApplyResult: ...

    async def reconcile(
        self,
        intent: EffectIntent,
        settlement_key: str,
        runtime: EffectRuntimeContext,
    ) -> EffectReconcileResult: ...
```

Product 在启动时只构造一次 `EffectRuntimeContext`，Kernel 把它交给现有 `AttemptEffectSettler`。静态 executable 不变，变化的只是运行时依赖，类似 Skill 调用时注入 arguments、tools 和 environment。

首选把两套当前形状相同的 Capability store protocol 收敛为一个窄的 framework port：

```python
class DurableEffectStore(Protocol):
    async def get(self, kind: str, settlement_key: str) -> EffectStoreRecord | None: ...
    async def commit(self, record: EffectStoreRecord) -> None: ...


@dataclass(frozen=True, slots=True)
class EffectRuntimeContext:
    store: DurableEffectStore
```

业务 payload 校验、business-key 推导、receipt 构造仍留在六个 Capability handler；公共 store 只保存 Kernel 已定义的通用 envelope。若后续证明 Healing 与 Improvement 确实需要不同的存储操作，可把 context 改成两个显式 typed port，但仍不增加 handler factory 或第二套 registry。

### 4.3 ProductLock 应锁什么

继续锁：

- effect kind、intent/receipt schema、policy；
- 静态 handler 的 apply/reconcile provenance；
- durable store schema/version 以及 Product runtime revision。

不再锁：

- 不存在的 `EFFECT_FACTORY` executable kind；
- factory 产生的临时 handler identity；
- `effect_factory_registry -> runtime_effect_registry` 之间的二次投影。

Product 自己是运行时依赖的 owner。它负责创建 SQLite store、验证 schema/version、构造 context，并在任何 Attempt 开始前 fail closed。SUT 仍然没有注入 store 或替换 handler 的入口。

## 5. 建议从主 Spec 删除和替换的内容

删除：

- `EffectRuntimeFactory[StoreT]`；
- `EffectStoreContract[StoreT]` 作为每个 registration 的泛型对象；
- `ExecutableKind.EFFECT_FACTORY`；
- `FrozenComposition.effect_factory_registry`；
- invocation-scoped `runtime_effect_registry`；
- 对 factory 返回 handler 再验证 apply/reconcile projection 的第二轮认证。

替换为：

```text
installed EffectRegistration + authenticated static handler
                         │
                         ▼
AttemptEffectSettler ── EffectRuntimeContext(Product-owned SQLite store)
                         │
                         ▼
             apply / reconcile / durable receipt
```

这会把原链条：

```text
StoreContract -> Factory -> factory registry -> runtime registry
```

收敛成：

```text
EffectRegistration -> runtime context
```

同时不减少 Effect transaction 的任何保障：intent 先持久化、两种 key、apply/reconcile、幂等冲突、receipt、崩溃恢复和 Kernel 独占提交权都继续保留。

## 6. 最终判断

Claude Code Skills 支持的是一个**装配简化论据**：定义和可执行内容共置、标准目录自动发现、触发时注入上下文，没有 factory-of-factory。

它不支持“Effect 可以像 Skill 一样弱化安全”的结论。对本项目最合适的落点是：

> 保留一个锁定的静态 Effect registry；把 durable state 作为 Product-owned runtime context 注入现有静态 handler；让 Kernel/Settler 继续承担全部事务语义。
