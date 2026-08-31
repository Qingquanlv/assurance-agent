# LangGraph-first Assurance Boot Runtime

- Status: Draft for review
- Date: 2026-08-31
- Scope: 将 LangGraph 设为唯一 Workflow 推进权威，将现有 Graph Runtime 重构为 Boot/装配层与单 Attempt 可靠提交内核
- Decision: 保留受认证的 YAML Workflow 与 Capability wheel 装配；在启动阶段 lowering 为版本固定的 LangGraph

## Problem Statement

Assurance Agent 当前同时维护两类复杂度：

1. 通用 Workflow 执行复杂度，包括 graph activation、token、route、join、subgraph、interrupt、checkpoint 与 scheduler wave；
2. Assurance 特有的可靠执行复杂度，包括受认证的 Capability binding、隔离 workspace、OpenCode 外部活动恢复、JSON Schema、write-set seal、commit validators、durable prepare、promotion、durable effects、receipt 与 evidence。

第一类复杂度与 LangGraph 的职责高度重叠。继续自行维护完整 Graph Runtime，会让项目长期承担 Workflow 执行器、持久化恢复、并行调度和生态集成成本。

第二类复杂度不能由 LangGraph 或 OpenCode structured output 替代。LangGraph checkpoint 不知道 canonical 项目文件是否已经安全提交，JSON Schema 也不能证明写入路径、workspace baseline、effect receipt 或业务不变量。若简单删除现有 Runtime，系统会失去当前最关键的 assurance 保证。

当前 Workflow 也不是一组简单线性任务。现行模块共包含 64 个 graph、324 个 node、366 条 edge、124 条条件 edge、73 个 subgraph node、12 个 join、13 个 interrupt 和 7 个循环强连通分量；最深调用包含 5 个 graph instance。迁移必须保留实际业务语义，而不能只把 YAML 逐行改成 Python `if/elif`。

同时，`graph_engine` 还承担受认证产品装配、Capability slot binding、schema/validator/effect registry、Workflow digest、CLI 生命周期等职责。这些职责仍然有价值，但更适合类似 Spring Boot 的应用启动与自动装配模块，而不是第二套 Workflow 执行器。

用户需要一个目标架构，使：

- LangGraph 成为唯一 Workflow 推进权威；
- 可靠提交与恢复集中在一个深的 Attempt 模块中；
- `graph_engine` 继续作为可信 Boot/装配框架存在；
- YAML 仍然只替换 Workflow，Python wheel 仍然增加能力，`.aa/` 仍然只保存组织配置；
- 当前 CLI、Entrypoint、插件认证和产品锁定能力保持兼容；
- 旧 planner、token engine 和 scheduler 最终能够真正删除，而不是换名后重写在 LangGraph adapter 中。

本文件是顶层架构规格，锁定权威边界、不可丢失的不变量、迁移顺序与切换门槛；它不是一个可由单个实现 PR 完成的任务规格。Attempt 协议、Effect 协议、Workflow lowering/versioning、Retro 合约修复和最终 cutover 将各自形成可独立验收的子规格或实施计划。

## Solution

将系统重构为一个 Workflow 推进权威、一个 Attempt 事实权威和一个无状态装配入口：

```text
aa CLI / LangGraph Agent Server
              │
              ▼
GraphEngineBoot.assemble(product, project_config)
  ├── 认证 Product 与已安装 Capability wheels
  ├── 装配 Workflow modules、slots、schemas、validators、effects
  ├── 编译并锁定 WorkflowSpec / workflow_digest
  ├── lowering 为 versioned LangGraph StateGraph
  └── 返回 immutable BootArtifact
              │
              ▼
GraphEngineBoot.bootstrap(BootArtifact, runtime_config)
  ├── 注入 checkpointer、Store 与 AssuranceAttemptKernel
  └── 返回 AssuranceApplication
              │
              ▼
AssuranceApplication（生命周期 facade，不另存流程真相）
  ├── 按 workflow_digest 选择已装配 graph
  ├── 将 CLI 调用委托给 LangGraph
  └── 归一化 checkpoint、Attempt receipt 与 evidence 的只读状态
              │
              ▼
LangGraph：唯一 Workflow 推进权威
  ├── route / loop / subgraph / parallel / interrupt
  └── 只有收到终态 CommittedTaskResult 才推进下游
              │
              ▼
AssuranceAttemptKernel：单 Attempt 可靠提交与恢复权威
  ├── ResourceArbiter
  ├── OpenCodeActivityKernel
  ├── WorkspaceCommitKernel
  └── EffectKernel + typed effect adapters
```

权威划分固定如下：

| 模块 | 拥有的事实或职责 | 明确不拥有 |
|---|---|---|
| `GraphEngineBoot` | 一次性认证、装配、验证与 lowering；产出不可变的 `BootArtifact` | Invocation 运行状态、health/status 状态、下一节点 |
| `AssuranceApplication` | 持有 `BootArtifact` 与运行依赖；提供 CLI/health/status facade | 独立流程状态或第二份终态判断 |
| LangGraph | Invocation 的下一节点、循环、并行、Interrupt 与 `InvocationStatus` | canonical 文件是否提交、外部 effect 是否发生 |
| `AssuranceAttemptKernel` | 一个 semantic activation 的 `AttemptResolution` 及其 durable receipt/journal | Invocation 的下一节点或最终状态 |
| OpenCode adapter | 一个受约束 agent activity 的 provider lifecycle | canonical 文件提交事实、Workflow 进度 |

`graph_engine` 的产品定位从“Workflow runtime”改为“Assurance application boot framework”。它继续提供类似 Spring Boot 的 starter discovery、auto-configuration、registry 与 configuration validation。Boot 只负责 wiring；运行期 health/status 和 CLI 生命周期由返回的 `AssuranceApplication` facade 提供。两者都不执行 graph token 或决定下一节点。

首个目标实现选择 YAML-to-LangGraph lowering，而不是立即改成纯 Python graph：

- packaged Workflow module 与项目整体 YAML replacement 保持现状；
- Assembler、Capability slot binding、closed expression language、projection definitions 和 Workflow digest 继续作为 build/start-time contract；
- lowering 只生成 LangGraph nodes、edges、subgraphs、reducers 和 adapters；
- 运行时不再动态解释 module imports 或 slot binding；
- 任意 SUT 文件仍不能提供 Python handler、validator、gate function 或 effect adapter。

## User Stories

1. As a QA operator, I want LangGraph to be the only authority that advances an Invocation, so that status and resume never disagree with a second planner.
2. As a QA operator, I want a downstream node to run only after its predecessor has an authenticated committed receipt, so that partially written evidence is never consumed.
3. As a QA operator, I want `aa start`, `aa run`, `aa status`, `aa resume`, `aa export`, and `aa archive` to keep their current user-facing meaning, so that automation does not need a command rewrite.
4. As a QA operator, I want human Interrupt actions validated against a closed action set, so that a forged resume payload cannot approve work.
5. As a QA operator, I want an effect-pending Invocation to remain resumable without creating a second external side effect, so that waiting does not corrupt delivery state.
6. As a QA operator, I want `InvocationStatus` plus outcome/reason to distinguish achieved, not-achieved, human-interrupted, system-blocked, stopped, committed-effect-failed and indeterminate outcomes, so that remediation is unambiguous.
7. As a Workflow author, I want the promised project-level whole-file YAML replacement implemented and kept declarative and closed, so that changing routes does not authorize arbitrary Python execution.
8. As a Workflow author, I want exclusive routes to fail when multiple predicates match, so that overlapping conditions never silently select the first branch.
9. As a Workflow author, I want fanout `min_matches` to remain enforced, so that an incomplete required fanout fails closed.
10. As a Workflow author, I want `join:all` to preserve predecessor identity and deterministic ordering, so that projections do not depend on completion timing.
11. As a Workflow author, I want versioned `join:any` merge/replay behavior to remain deterministic, and unsupported legacy token-queue dependencies rejected at compile time, so that migration cannot silently change behavior.
12. As a Workflow author, I want per-graph-instance activation limits and explicit business round budgets, so that runaway loops fail predictably.
13. As a Workflow author, I want existing input/output projection declarations reused by LangGraph nodes, so that 126 task inputs do not become hand-written Python mappings.
14. As a Capability maintainer, I want my wheel to continue owning handlers, schemas, validators, resources, effects and Workflow module exports, so that one package owns one coherent Capability.
15. As a Capability maintainer, I want logical slots resolved against exact contract IDs before an Invocation starts, so that Product binding drift fails closed.
16. As a Capability maintainer, I want the 30 exact prepare/execute/finalize leaf graphs exposed through an engine-neutral reusable builder before LangGraph migration, so that nearly half of all graph definitions stop repeating the same topology.
17. As a Product maintainer, I want installed wheel provenance and ProductManifest closure authenticated before graph construction, so that ambient packages cannot alter an Invocation.
18. As a Product maintainer, I want every compiled LangGraph associated with a canonical Workflow digest, so that active Invocations can resume against their original contract.
19. As a Product maintainer, I want old graph versions retained until active and interrupted Invocations drain, so that deployment does not silently change policy mid-Invocation.
20. As a Product maintainer, I want Product main to continue orchestrating only public Capability exports, so that internal subgraphs remain private implementation details.
21. As a Framework maintainer, I want `graph_engine` to bootstrap an `AssuranceApplication`, so that configuration and dependency wiring remain centralized.
22. As a Framework maintainer, I want LangGraph to replace custom activation planning, token scheduling and checkpoint control, so that the project stops maintaining a general Workflow engine.
23. As a Framework maintainer, I want a single high-level Attempt interface, so that OpenCode, workspace, validators, promotion and effects can change internally without changing every graph node.
24. As a Framework maintainer, I want resource conflicts resolved outside LangGraph's generic concurrency limit, so that parallel branches cannot write overlapping project paths.
25. As a Framework maintainer, I want graph state to store receipt references rather than workspace handles or large artifact bytes, so that checkpoints remain stable and serializable.
26. As a Framework maintainer, I want technical effect apply/reconcile hidden behind an internal seam, so that business Workflow topology does not expose commit protocol states.
27. As an effect maintainer, I want one typed adapter per effect kind, so that schema, timeout, apply and reconcile policies remain independently testable.
28. As an effect maintainer, I want multiple effects from one Attempt applied in declared order unless explicitly proven independent, so that delivery behavior remains deterministic.
29. As an effect maintainer, I want a permanent effect failure represented separately after workspace promotion, so that LangGraph never retries an already committed file mutation as if nothing happened.
30. As an OpenCode adapter maintainer, I want provider-native structured output used when supported, so that prompt-only JSON extraction can be reduced.
31. As an OpenCode adapter maintainer, I want local locked-schema validation retained after provider validation, so that provider behavior cannot weaken the product contract.
32. As a security reviewer, I want SUT files prevented from registering handlers, validators, gate functions, effect adapters or artifact models, so that Workflow customization does not become a plugin platform.
33. As an audit consumer, I want Workflow route decisions, Interrupts, Attempt receipts, failures and recoveries projected into stable evidence, so that Retro does not depend on LangGraph's internal checkpoint format.
34. As a Retro maintainer, I want production Retro inputs and authenticated evidence slices connected before migration, so that topology parity is not mistaken for a runnable dataflow.
35. As an Improvement maintainer, I want evaluate, export, apply and rollback effects to complete before their tasks become successful, so that delivery receipts remain authoritative.
36. As a Nightly maintainer, I want a future external scheduler to start a stable keyed Invocation rather than hold a graph node open overnight, so that duplicate schedule delivery is recoverable.
37. As an Eval maintainer, I want offline export comparison to remain a pure deterministic function unless a multi-sample campaign requires orchestration, so that LangGraph is not introduced without leverage.
38. As a test author, I want behavior tested at the `AssuranceApplication`, Workflow lowering harness and `AssuranceAttemptKernel` interfaces, so that tests survive internal refactoring without hiding graph-adapter defects in end-to-end tests.
39. As a release operator, I want old and new runtime paths shadow-compared before cutover, so that observable behavior rather than internal event IDs determines parity.
40. As a release operator, I want the old planner/scheduler removed only after all active old-version Invocations are drained or explicitly migrated, so that rollback and recovery remain controlled.

## Implementation Decisions

### 1. `graph_engine` becomes the Boot module

`graph_engine` remains the distribution and compatibility namespace during migration. Its public role changes to application bootstrap and assembly.

The Boot API has two explicit phases. `GraphEngineBoot.assemble(product, project_config)` performs no Invocation work and returns an immutable `BootArtifact`; `GraphEngineBoot.bootstrap(artifact, runtime_config)` wires runtime dependencies and returns an `AssuranceApplication`.

`BootArtifact` contains the authenticated registries, canonical Workflow specs/digests, lowered graph factories and compatibility metadata. `AssuranceApplication` holds the versioned graph registry, LangGraph checkpointer/Store, Attempt Kernel and CLI/health/status facade. Boot itself retains no thread, checkpoint, lease, activity or Invocation state after bootstrap.

Boot performs all module loading, provenance checking, import/export resolution, Capability slot lowering, schema closure and Workflow digest calculation before graph execution. No dynamic module lookup occurs after an Invocation starts.

Boot may run embedded in the CLI process or in a LangGraph-compatible deployment process. A separately deployed remote `graph_engine` process is not part of this design.

### 2. LangGraph is the only Workflow progression authority

LangGraph owns node readiness, conditional transitions, subgraph invocation, parallel supersteps, Interrupt persistence and Workflow completion.

The previous Ledger may remain as an append-only Attempt/evidence journal, but it must not decide the next node. No old planner projection and LangGraph checkpoint may independently advance the same Invocation.

One Invocation maps to one LangGraph `thread_id`. Invocation identity, Workflow digest, Product lock digest and input digest are present in the initial checkpoint and cannot change on resume.

状态严格分为两层：

- `AttemptResolution` 是 Kernel 对一个 semantic activation 的 durable 事实；它可以是 committed、pending、failed、stopped 或 indeterminate，但不能直接结束 Invocation；
- `InvocationStatus` 是应用对整个 LangGraph thread 的归一化状态，固定为 `running | blocked | interrupted | stopped | failed | completed`，并可带稳定的 outcome/terminal reason。

只有编译进 LangGraph 的 adapter node 与 conditional edge 可以把 `AttemptResolution` 折叠为 `InvocationStatus`。Kernel 不得直接写 Workflow 终态，Application facade 也不得从 journal 绕过 graph checkpoint 推导第二个下一节点。

### 3. YAML remains the initial Workflow definition interface

The initial migration preserves packaged Workflow modules and implements the intended project whole-file YAML replacement contract. YAML continues to change how a Workflow walks; installed Python wheels continue to add Capability.

This is currently a contract gap, not a working compatibility surface. Product configuration code presently reads only `.aa/config.yaml`, `.aa/policy.yaml`, `.aa/data-knowledge.yaml` and `.aa/capability-catalog.json`; it does not load the promised `.aa/workflow-schema.yaml` or `.aa/execution-contracts.yaml` replacements. Phase 1 must implement both whole-file replacements, authenticate their source and include their bytes in the Product/Workflow digest before the migration golden is captured.

The existing Assembler and compiler responsibilities are retained at Boot time:

- authenticated module loading;
- import/export and public/private checks;
- slot completeness and contract matching;
- closed schema/resource/effect references;
- route shape and fanout validation;
- projection structure validation;
- subgraph dependency DAG validation;
- canonical Workflow digest.

The compiler target changes from the custom planner model to a deterministic LangGraph builder. A future Python-native `GraphProvider` is a separate decision and must preserve the same canonical manifest and binding guarantees.

Lowering helper 的允许边界是：纯投影函数、封闭谓词求值、局部 state reducer、typed adapter，以及从一个已验证声明确定性地产生 node/edge 的 builder。它们只能读取当前 node 输入或当前 LangGraph state，并返回 state update、edge label 或 Interrupt envelope。

Lowering helper 明确禁止：

- 持有独立 token queue、node readiness table、activation wave 或 cross-node scheduler state；
- 在 LangGraph checkpoint 之外保存一份用于推进 Workflow 的持久状态；
- 根据全图运行状态自行选择或执行下一节点；
- 在 helper 内循环轮询 task/effect 直至另一个业务节点可运行；
- 用一个通用解释循环取代显式 LangGraph node、edge、subgraph 和 reducer。

任何跨节点推进决定必须在生成的 LangGraph topology 中可见。若某个 legacy YAML 语义只能通过上述禁用机制实现，该 graph 不得 lowering；它必须先显式升级 Workflow 语言版本或继续留在 legacy Runtime。

### 4. Closed route semantics are preserved

Project route expressions continue through the closed expression evaluator; arbitrary project Python functions are not accepted.

An exclusive route evaluates all declared predicates. More than one match is `ambiguous_route`; zero matches selects the unique `otherwise`; exactly one match selects that branch.

A fanout evaluates all declared predicates and fails when matches are below `min_matches`.

These semantics live in small pure lowering/runtime helpers invoked by LangGraph route nodes. They do not form a second graph planner.

### 5. Join and reducer semantics are explicit

Fixed parallel lanes write results to maps keyed by stable predecessor or family IDs. Reducers reject conflicting duplicate keys and never use completion order as business order.

`join:all` waits for each declared predecessor and assembles outputs in declaration order.

`join:any` is a first-class migration primitive, not an exceptional compatibility path. All 9 current `join:any` nodes feed a `/tokens/0` projection; 7 are reactivated by a loop, and the Intake merge has 3 predecessors. All 9 must be rewritten and pass characterization before `product-full`, `product-execute`, Intake or any Generation family can leave the legacy Runtime.

Target WorkflowSpec defines a consume-once `merge:any` envelope generated by lowering:

```text
AnyMergeArrival:
  join_id
  activation_epoch
  source_activation_id
  predecessor_id
  payload
```

Each incoming edge creates one stable arrival ID. The LangGraph reducer de-duplicates an exact replay, retains the current arrival under `(join_id, activation_epoch)`, and exposes it to the merge activation as `current_arrival`. Existing `/tokens/0/...` projections are deterministically rewritten to `current_arrival.payload/...`; they never read a predecessor-keyed map without an epoch.

Re-entering a merge through a business loop increments `activation_epoch`; replaying the same source activation does not. The 9 current joins are mutually exclusive-path merges, so target compatibility requires at most one distinct arrival per logical epoch. A second distinct arrival in the same epoch fails closed as `ambiguous_merge_epoch`; wall-clock or completion-order “last arrival wins” is forbidden.

This envelope lives in ordinary typed LangGraph state and does not authorize an independent residual-token queue, readiness table or scheduler. A future or external graph that depends on the old generic behavior—multiple same-epoch arrivals consumed one by one—fails lowering with `legacy_join_any_requires_rewrite` and remains on the legacy Runtime until explicitly rewritten. The language change is recorded in the WorkflowSpec version and compatibility manifest.

### 6. Loop budgets remain business state

Each migrated subgraph keeps its existing business counters, including `rounds_used` and `rounds_budget`, in typed state.

Per-graph-instance `max_activations` remains an independent safety counter. LangGraph recursion limits are an additional last-resort guard and do not replace either counter.

Loop epoch and fanout branch identity contribute to semantic Attempt identity, preventing a later repair/recheck round from replaying an earlier receipt.

### 7. Subgraph communication uses typed adapters

Parent and child graphs with different schemas communicate through explicit projection wrappers. The wrapper evaluates the existing input projection, validates the child input schema, invokes the subgraph, applies the output projection and validates the public output schema.

Only schemas intentionally shared by parent and child may use direct shared-state subgraphs.

Subgraphs use per-invocation persistence by default. Cross-call per-thread subgraph memory is not enabled unless a separate domain requirement justifies it.

### 8. Repeated leaf graphs collapse behind one builder

Of 64 current graphs, 33 contain prepare/finalize stages and 30 have the exact four-node `prepare → execute → finalize → done` shape. The exact shape therefore represents 47% of all graph definitions and is an early, independent simplification target.

The repeated topology is first represented by an engine-neutral `AgentLeafSpec`/builder that can target the current compiled model and later LangGraph. It is extracted behind the current Runtime before Workflow cutover. The LangGraph target may lower the spec to a reusable subgraph or one Attempt node, depending on whether prepare/finalize are separately observable business steps.

Capability-specific prepare and finalize implementations remain typed hooks. The shared builder owns projection, OpenCode dispatch, retry integration, result validation and receipt publication wiring.

This decision removes repeated topology without merging domain-specific validators or finalization rules, and it must preserve whole-file YAML replacement rather than introducing project-loadable Python factories.

### 9. `AssuranceAttemptKernel` is the public transaction interface

The previous proposed `AssuranceTaskKernel` is renamed `AssuranceAttemptKernel` because its interface resolves one semantic node activation across process crashes and internal retries, including durable effects.

The decision-rich result shape established during design is:

```text
AttemptResolution =
    CommittedTaskResult
  | PendingTaskResult
  | CommittedEffectFailure
  | FailedTaskResult
  | StoppedTaskResult
  | IndeterminateTaskResult
```

`AttemptResolution` is the result of one `execute_or_recover` call; `PendingTaskResult` and `IndeterminateTaskResult` are intentionally nonterminal observations. The generated adapter applies this mandatory mapping:

| `AttemptResolution` | LangGraph adapter action | Observable `InvocationStatus` / reason | Resume or retry | Normal downstream | Invocation terminal |
|---|---|---|---|---|---|
| `CommittedTaskResult` | Publish validated output plus receipt/evidence refs, then take the declared success edge | `running`; a later graph end may produce `completed` | Continues normally | Yes | No |
| `PendingTaskResult` | Invoke `interrupt(SystemWakeEnvelope)` from the adapter node; on resume rerun that node with the same Attempt key | `blocked / effect_pending` or typed activity reason | Same-thread `run/resume` or deployment wake | No | No |
| `CommittedEffectFailure` | Publish the promoted-write failure fact and take an explicit generated edge to the `committed_effect_failed` terminal node | `failed / committed_effect_failed` | Never automatic; compensation is a separate Invocation | No | Yes |
| `FailedTaskResult` | Publish a failure envelope; the generated conditional edge either increments logical attempt and retries, or enters the failed terminal node | `running / retrying` or `failed / task_failed:<kind>` | Only when the compiled retry contract permits | No | Only when retry is exhausted or forbidden |
| `StoppedTaskResult` | Take the explicit stopped terminal edge | `stopped / <stop_reason>` | No | No | Yes |
| `IndeterminateTaskResult` | Invoke `interrupt(SystemBlockEnvelope)` containing the reconciliation reference; never dispatch a replacement activity/effect | `blocked / indeterminate` | Only same-key reconciliation after operator/provider resolution | No | No |

Every row appends stable Attempt/evidence facts before the adapter action. Only `CommittedTaskResult` publishes normal business output. Human approval Interrupts are Workflow nodes, not Kernel resolutions, and normalize to `interrupted` rather than `blocked`.

The external interface remains small: stable Attempt key plus an immutable resolved task request in, one typed resolution out. Workspace, OpenCode activity, validators, prepare, promotion and effects remain implementation details.

### 10. Attempt identity is stable across LangGraph replay

The stable Attempt key is derived from:

- Invocation/thread identity;
- Workflow digest;
- semantic graph/subgraph path;
- node ID;
- loop epoch or activation ordinal;
- fanout item/branch identity;
- logical task attempt.

The key is persisted before external dispatch. Reusing a key with a different input/task digest fails closed as an identity conflict.

The Kernel resolves the same key as follows:

- no record: create the first activity and isolated workspace;
- running: adopt or reconcile the existing OpenCode activity;
- prepared: resume the same promotion;
- effect pending: apply/reconcile the same ordered effects;
- committed: return the original immutable receipt;
- indeterminate: block rather than dispatch again.

### 11. Attempt transaction ordering is fixed

The Kernel preserves the following order:

```text
durable activity prepare
→ isolated workspace
→ OpenCode or deterministic handler
→ structured result validation
→ deterministic finalizer
→ seal write set
→ commit validators
→ durable commit prepare, including EffectIntents
→ atomic promote/recover
→ ordered effect apply/reconcile
→ final authenticated Attempt receipt
```

The final receipt binds task contract/schema version, input digest, workspace baseline, sealed paths and content digests, validation receipts, promotion receipt, ordered effect receipts, Workflow digest and receipt digest.

### 12. Resource arbitration remains Assurance-specific

LangGraph concurrency limits do not replace read/write/exclusive resource claims.

Before external activity starts, `ResourceArbiter` obtains a durable authorization for the Attempt's resolved claims. Parallel LangGraph branches may execute concurrently only when their claims do not conflict.

Each parallel branch receives an isolated workspace. Reducer merge never substitutes for filesystem conflict detection or canonical baseline comparison.

### 13. Effect implementation is typed but normally hidden

`EffectKernel` is an internal module owned by `AssuranceAttemptKernel`. It dispatches each registered effect kind to a typed adapter that owns payload schema, receipt schema, timeout, apply and reconcile behavior.

The current registry contains exactly six effect kinds in two wheels:

- Healing: `assurance.healing.effect.allocation.v2`, `assurance.healing.effect.heal-apply.v2`, `assurance.healing.effect.proposal-approved.v1`;
- Improvement: `assurance.improvement.effect.archive.v1`, `assurance.improvement.effect.delivery.v1`, `assurance.improvement.effect.promotion.v1`.

`improvement-evaluate`, `improvement-export`, `improvement-apply` and `improvement-rollback` are graph names, not effect kinds. Their reachable handlers emit variants of the single `assurance.improvement.effect.delivery.v1` kind. The other five registered kinds are not reachable from current Workflow YAML; discovery alone must not make them reachable. They remain typed internal commit protocol adapters and require explicit Workflow/task wiring before use.

Multiple effects from one Attempt settle in declared index order. Parallel settlement requires an explicit future contract proving independence and commutativity.

`PendingTaskResult` is mapped exactly as defined in Decision 9: the generated adapter invokes `interrupt(SystemWakeEnvelope)`. Resuming the same thread restarts the adapter node and calls the Kernel with the same key; no alternative polling path is authoritative.

The initial migration preserves the current Invocation-wide pending barrier. When any parallel branch returns `PendingTaskResult`, siblings already started in the same LangGraph superstep may settle and checkpoint, but no new sibling or downstream superstep may begin until the same Attempt resolves. Branch-local continuation while an effect is pending is a future behavior change and requires a separate spec.

If workspace promotion succeeded but an effect permanently fails, the Kernel returns `CommittedEffectFailure` with `writes_promoted=true`. This result is nonretryable; the generated LangGraph failure edge, rather than the Kernel, records the failed Invocation terminal and its evidence outcome.

An effect may become a dedicated LangGraph subgraph only when it has business-visible routing, human approval, compensation or independent scheduling requirements. That promotion requires a separate spec; technical apply/reconcile states alone are insufficient justification.

### 14. OpenCode structured output narrows only the adapter

When the pinned OpenCode version supports provider-native JSON Schema output, the adapter sends the locked schema through the supported structured-output channel.

The returned value is still validated locally against the locked schema and schema digest. Domain finalizers still authenticate Change identity, source manifest, evidence references, expected versions and allowed output paths.

Structured output may remove prompt-only JSON formatting instructions, message-text scanning and formatting retries after integration parity is demonstrated. It does not remove workspace isolation, write-set sealing, validators, promotion or effect receipts.

### 15. Human and system Interrupts are distinct

Human Interrupts remain independent pure Workflow nodes. Their adapters validate the declared action enum and payload schema before state is updated. Invalid resumes leave the pending Interrupt unchanged, and no Kernel/OpenCode work occurs in that node.

System suspension is deliberately different: `PendingTaskResult` and `IndeterminateTaskResult` cause the already effectful Attempt adapter node to invoke the typed Interrupt defined in Decision 9. That node is not pure. Safe restart comes from replaying the same stable Attempt key through the idempotent Kernel, not from separating the system Interrupt into another node.

System suspension carries no human action. It includes a typed wake/reconciliation reference bound to Invocation, Attempt key and Workflow digest. A later `aa run`/`aa resume` or deployment wake resumes the same LangGraph thread from the beginning of the adapter node, where the Kernel adopts, reconciles or returns the prior durable resolution.

### 16. Checkpoint and evidence formats remain separate

LangGraph checkpoints are control-plane persistence and may change with LangGraph implementation details.

Stable business evidence is projected explicitly from:

- Workflow and Product version identity;
- route decision and normalized predicate outcome;
- Attempt receipt and recovery result;
- failure classification and healing round;
- Interrupt/resume action;
- Workflow terminal classification.

Retro consumes authenticated evidence documents and receipt references, not raw LangGraph checkpoint internals.

### 17. Workflow version routing is mandatory

Boot registers compiled LangGraphs by canonical Workflow digest. A new Invocation selects the current approved digest; an existing Invocation always resumes the digest recorded at start.

Old graph builds and compatible schemas remain available until all active/interrupted Invocations using them drain. Removing an in-use version is a release error.

Changing graph code for an active digest is prohibited. Explicit state/graph migration, if ever needed, is separately versioned and auditable.

### 18. Current contract gaps are migration prerequisites

Retro control topology is representable in LangGraph, but its current dataflow must first be closed against production handler contracts. The repaired flow authenticates execution receipts/evidence, constructs the Retro window and Issue/Workflow/Eval slices, performs analyses, assembles authenticated context and reconciles candidates.

Improvement evaluation input projection must likewise match its production handler contract before it becomes a parity target.

Each executable task must resolve an explicit validator set through its immutable task contract. Registration alone does not imply execution. Empty validator sets are allowed only when explicitly declared and tested.

The promised `.aa/workflow-schema.yaml` and `.aa/execution-contracts.yaml` replacement paths must be implemented and authenticated as described in Decision 3.

The current capability-slot drift is four failing module tests, not one isolated mismatch. A verified six-wheel run on this working tree produced 4 failures and 118 passes; Execution, Healing, Improvement and Quality tests still expect concrete prepare/finalize capability IDs where YAML now declares capability slots. These four failures must be resolved and the full six-wheel result recaptured from a stable tree before freezing the migration golden.

### 19. Eval, Retro and Nightly keep distinct semantics

Improvement evaluation remains an in-Workflow gate backed by committed receipts.

Retro Eval analysis consumes an authenticated Eval evidence slice; it does not run a general Eval suite.

The offline benchmark export comparator remains a pure deterministic operation. A future multi-sample Eval campaign may use a separate LangGraph Invocation.

Nightly remains future wiring rather than migration parity. An external scheduler or deployment cron starts a new thread using a stable schedule-slot key. The graph does not sleep until the next schedule.

### 20. CLI and application lifecycle remain compatible

The Product continues to own `aa`. Existing Entrypoint names and public commands remain stable during migration.

`compile` runs Boot assembly, static validation, lowering and digest creation without starting OpenCode.

`start` creates the initial LangGraph checkpoint and Kernel Invocation journal.

`run` and `resume` invoke the pinned graph and return normalized application status.

`status`, `export` and `archive` use LangGraph control state plus authenticated Kernel/evidence receipts; they do not depend on deleted planner projections.

### 21. Migration is staged and reversible

Migration phases are:

1. Repair baseline contracts, validator bindings, the two missing `.aa/` whole-file replacements and the four current capability-slot test failures.
2. Extract the engine-neutral `AgentLeafSpec` under the current Runtime; convert and characterize the 30 exact four-node leaf graphs without adding LangGraph.
3. Define WorkflowSpec vNext `merge:any`, activation epochs and `current_arrival`; rewrite all 9 current `join:any` nodes and their `/tokens/0` projections in the lowering target and pass the lowering harness before any dependent Entrypoint cutover.
4. Extract `AssuranceAttemptKernel` behind the current Runtime without adding LangGraph.
5. Add Boot lowering and migrate one contract-closed leaf end to end.
6. Migrate remaining low-topology execution, quality and delivery wrappers.
7. Migrate Healing and Intake using the already-proven merge/projection primitive.
8. Migrate Generation fixed fanout, its four merge/retry loops, nested review loops and Interrupts.
9. Migrate `product-execute`, resource-aware parallelism and effect-heavy Improvement flows.
10. Migrate the redesigned Retro flow; add separate Eval/Nightly entrypoints only if required.
11. Shadow old/new execution, drain old graph versions, then remove the old planner/scheduler/token engine.

Each phase has an independent rollback to the prior execution path. The old and new runtimes never advance the same production Invocation.

This architecture is implemented through separately reviewable protocol/design slices:

1. engine-neutral leaf-template schema and current-Runtime lowering;
2. WorkflowSpec vNext `merge:any`, epoch/provenance and projection rewrite;
3. `AssuranceAttemptKernel` state machine, Attempt key and crash-recovery protocol;
4. Effect intent/receipt/apply/reconcile protocol;
5. general Workflow lowering and `AttemptResolution` adapter rules;
6. graph version registry, checkpoint pinning and resume/retirement protocol;
7. Retro and improvement-evaluate contract repair;
8. shadow comparison, drain, rollback and deletion cutover.

The first implementation plan is limited to Phase 1 and one contract-closed `AgentLeafSpec` tracer from Phase 2. Attempt Kernel extraction and each later protocol slice receive separate plans; no plan may bundle the full eleven-phase migration into one change set.

### 22. Deletion target is explicit

After cutover, the following old responsibilities no longer exist as independent Runtime implementations:

- node activation planner;
- generic token scheduling loop;
- scheduler wave as Workflow progression;
- custom subgraph execution loop;
- custom human Interrupt progression;
- checkpoint-as-second-workflow-authority;
- engine-level effect settlement loop.

The following code is retained or refactored into named modules:

- authenticated product/module assembly;
- Workflow compiler/lowering and digest;
- projection and closed-expression helpers;
- workspace store and promotion recovery;
- OpenCode activity recovery;
- validators and resolved task contracts;
- EffectKernel and effect adapters;
- receipt/evidence journal;
- Workflow version registry;
- CLI/application lifecycle.

The migration is not accepted if equivalent token/planner/scheduler logic merely reappears as a large generic LangGraph adapter.

## Testing Decisions

### Primary test seams

The highest primary seam is `AssuranceApplication`: tests start or resume a named Entrypoint and assert normalized status, public output and authenticated evidence/receipts.

The second seam is `AssuranceAttemptKernel`: fault-injection tests submit one immutable task request repeatedly and assert exactly-once observable outcomes across every crash cut.

The required middle seam is `WorkflowLoweringHarness`: given a fixed `WorkflowSpec`/digest, typed initial state, an in-memory checkpoint and scripted `AttemptResolution` values, it drives bounded LangGraph steps and exposes only semantic transition labels, reducer output, Interrupt envelopes and Attempt calls. It never invokes real OpenCode, filesystem promotion or effects. This seam is the primary home for route, join, subgraph adapter, retry, Interrupt encoding and pinned-version replay parity.

Lower modules receive focused contract tests only where these three seams cannot efficiently isolate a failure, especially typed effect adapters, projection helpers and resource arbitration.

Tests assert external behavior and durable facts, not LangGraph internal task IDs, checkpoint serialization details or private helper layout.

### Workflow parity tests

- All 14 public Entrypoints compile and start through Boot.
- Existing public input/output schemas and terminal classifications remain stable.
- All declared conditional routes receive characterization cases for each branch.
- Exclusive overlap produces `ambiguous_route`; zero match uses the unique `otherwise`.
- Fanout below `min_matches` fails closed.
- `join:all` is independent of branch completion order and preserves predecessor identity.
- All 9 current `join:any` nodes pass target WorkflowSpec characterization; none may remain on the legacy Runtime at cutover.
- Loop re-entry increments `activation_epoch`, exact arrival replay is de-duplicated, `/tokens/0` reads only `current_arrival.payload`, and two distinct same-epoch arrivals fail as `ambiguous_merge_epoch`.
- A non-current graph that needs residual-token behavior fails lowering with `legacy_join_any_requires_rewrite`; no residual-token scheduler is introduced.
- Business round budgets and graph activation limits fail at the same observable point.
- Five-level nested subgraphs preserve input/output projection and failure propagation.
- Human Interrupt action whitelists reject invalid and forged payloads.
- Stopped, interrupted, completed, failed and committed-effect-failed remain distinct.

### Attempt and crash tests

Fault injection covers at least:

- before durable activity prepare;
- after activity prepare but before OpenCode dispatch;
- OpenCode dispatched but external reference publication uncertain;
- OpenCode completed but terminal result not journaled;
- after write-set seal but before durable commit prepare;
- after durable prepare but before promotion;
- during promotion;
- after promotion but before promotion receipt publication;
- after Kernel commit but before LangGraph checkpoint;
- effect apply completed but receipt publication uncertain;
- effect reconcile pending;
- permanent effect failure after workspace promotion.

For every cut, replay with the same Attempt key either returns the same receipt, reconciles the same external activity/effect, or returns a typed indeterminate result. It never creates an unauthorized second OpenCode activity or repeats a committed effect.

### Resource and parallel tests

- Nonconflicting branches run concurrently.
- Overlapping write/write, write/read and exclusive claims serialize or fail according to policy.
- Each branch sees the same authenticated baseline and its own workspace.
- Promotion detects canonical baseline drift.
- Reducers reject two different receipts for the same semantic branch key.
- Generation selected/skip combinations always produce the complete fixed-family join contract.
- If one branch becomes effect-pending, already-started work in the same superstep may settle, but no new sibling or downstream superstep starts until that Attempt resolves.

### Effect tests

- Each effect adapter validates both intent and receipt schemas.
- Registry contract tests cover all six exact kinds: Healing allocation/heal-apply/proposal-approved and Improvement archive/delivery/promotion.
- Apply/reconcile uses the stable idempotency key.
- Multiple effects settle in declared order.
- Pending effects suspend without marking the Attempt successful.
- All receipts are required before `CommittedTaskResult`.
- Permanent failure after promotion returns `CommittedEffectFailure` and is not automatically retried.
- The four current Improvement graph paths—evaluate, export, apply and rollback—have end-to-end tests proving the payload variants of `assurance.improvement.effect.delivery.v1` settle through one typed adapter.
- The other five registered but unwired kinds have apply/reconcile/fault contract tests and remain unreachable merely through Boot discovery; adding Workflow reachability requires a new end-to-end test.

### Version and upgrade tests

- An interrupted Invocation created with graph digest A resumes graph A after digest B is deployed.
- Renaming or removing a node in B does not affect A.
- Starting with a missing pinned graph version fails before Kernel execution.
- Reusing an Attempt key under a different Workflow or input digest fails closed.
- Draining the last Invocation allows an old version to be retired explicitly.

### Structured-output tests

- Provider-native JSON Schema success produces the same typed result as the locked local validator.
- Malformed, extra-field, wrong-schema and wrong-digest responses fail before workspace commit.
- Domain identity and source-manifest violations fail even when the JSON Schema is valid.
- Provider fallback does not silently weaken the schema contract.

### Current baseline prerequisites

- Retro and improvement-evaluate run end to end with production handlers rather than universal success fakes.
- Task validator binding is observed in a real Kernel commit test.
- The four current capability-slot test failures are resolved and all six wheel module suites agree with Workflow declarations.
- `.aa/workflow-schema.yaml` and `.aa/execution-contracts.yaml` whole-file replacement, authentication and digest binding are exercised end to end.
- The migration golden is captured only after the selected current Workflow suite is green.

### Cutover acceptance

Cutover requires all of the following:

1. Topology, route, projection, join, loop, Interrupt and terminal parity for current Entrypoints.
2. Crash-matrix proof for Attempt, promotion and effects.
3. Resource-claim parity under parallel execution.
4. Pinned-version resume proof across a deployment upgrade.
5. Behavioral export parity, excluding explicitly documented runtime identity fields.
6. `status`, `resume`, `export` and `archive` reconstructed without the old planner projection.
7. Old planner, Workflow scheduler and token execution loop removed from the production path.
8. No project/SUT Python loading introduced.
9. All 9 current consume-once merges and `/tokens/0` projections run on the epoch-aware target contract; none retains a legacy Runtime dependency.

## Out of Scope

- Loading handlers, validators, gate functions, effect adapters or artifact models from the SUT.
- Turning `.aa/` into an executable plugin directory.
- Replacing local/domain validation with OpenCode JSON Schema alone.
- Letting an LLM apply, reconcile or authenticate durable effects.
- Replacing all YAML Workflow definitions with arbitrary Python graphs in the initial migration.
- Building a second generic YAML Workflow engine on top of LangGraph.
- Reimplementing the legacy residual-token queue solely to preserve unused generic `join:any` behavior; dependent graphs must be rewritten or remain on the legacy Runtime.
- Deploying `graph_engine` as a separate remote microservice.
- Implementing a LangGraph UI or graph editor.
- Replacing the existing CLI command surface.
- Automatically migrating active Invocation state between unrelated Workflow digests.
- Implementing a new general Eval campaign or Nightly scheduler as part of Runtime migration.
- Parallelizing ordered effects without an explicit independence contract.
- Treating LangSmith tracing or evaluation feedback as authoritative Assurance evidence.
- Rewriting Retro domain semantics, Improvement fingerprinting or evidence authentication beyond the contract repairs required to make current flows runnable.

## Further Notes

### Terminology

- **Workflow** is the product-owned route a Change follows; LangGraph is its execution implementation.
- **Invocation** is one start of a Workflow at an Entrypoint and maps to one LangGraph thread.
- **Attempt** is one semantic task activation resolved by `AssuranceAttemptKernel`; it is not the same as a LangGraph retry counter.
- **Receipt** is an authenticated durable fact, not a reference to a LangGraph checkpoint.
- **Effect** is an ordered durable external mutation requested by a successful handler and completed before the Attempt is successful.

### Architectural analogy

The intended analogy is:

| Role | Target |
|---|---|
| Spring Boot | `graph_engine` Boot and auto-configuration |
| Embedded execution runtime | LangGraph |
| Transaction manager | `AssuranceAttemptKernel` |
| Internal transaction resources | Workspace, activity and Effect kernels |
| Starters | Installed Capability wheels |
| Application configuration | `.aa/` and Product configuration |

The analogy does not imply a web server or long-running daemon. It describes module responsibility and startup composition.

### Known baseline caveat

The design research was performed against a dirty working tree. The latest explicit run of all six `test_workflow_module.py` files produced 4 failures and 118 passes. The four failures are capability-slot expectation drift in Execution, Healing, Improvement and Quality. Those failures, the missing `.aa/` whole-file loaders, Retro/improvement-evaluate production contract gaps and validator binding audit must be resolved before a golden parity baseline is declared.

### Follow-up after approval

After this specification is approved, the next Superpowers step is a separate implementation plan. It begins with baseline repair and one engine-neutral leaf-template tracer rather than LangGraph graph translation.

The first plan covers only Phases 1 and the tracer portion of Phase 2 in Decision 21; the `merge:any` protocol, Attempt Kernel and later slices receive their own review and plan before implementation.
