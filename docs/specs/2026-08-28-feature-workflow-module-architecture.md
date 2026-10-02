# Feature-owned Workflow Modules and Deterministic Product Assembly

- Status: Ready for agent
- Date: 2026-08-28
- Target effort: 30–40 person-days
- Issue title: `refactor: modularize workflow ownership with feature modules and deterministic assembly`
- Scope boundary: 本规格只覆盖已讨论的推荐目标架构；不包含 40–55 PD 档位新增的跨版本恢复、细粒度 assembly provenance 和完整生产认证矩阵。

## Problem Statement

Assurance Agent 已经由多个独立 Python distribution 组成，但源码所有权与 Workflow 所有权没有对齐：六个领域 Capability 的 contracts、operations、validators、skills 和 schemas 位于各自 package，实际 Workflow 却全部集中在 `assurance-product` 的一份单体定义中。

当前单体 Workflow 约 2,313 行，包含 52 个 graph、265 个 node、59 个 subgraph call 和 14 个 Entrypoint。它同时承担以下职责：

- Product 的公共 Entrypoint 与跨 Capability 路由；
- Intake、Generation、Execution、Healing、Quality、Improvement 的内部子流程；
- Feature 内部 task、review loop、gate、interrupt 和 leaf capability wiring；
- 具体 agent capability alias 的选择。

这使当前 package 布局看起来像插件集合，但 Product 仍然知道 Feature 内部 graph 和 task。结果是：

- 修改一个 Capability 的内部 Workflow 必须编辑 Product 的巨型 YAML；
- Feature wheel 不能独立声明、验证和测试自己的流程边界；
- Product 无法只依赖稳定的公共流程接口；
- `subgraph.graph` 只是同一份 Workflow 内的本地 graph 名，不存在显式 imports、exports、owner 或 public/private；
- Feature 跨界调用与 Feature 内部调用在 schema 和 compiler 看来没有区别；
- subgraph 虽声明了 `input_projection`，runtime 启动 child graph 时并未应用它，且没有对称的 output contract/projection；
- 99/105 个 task 使用 Product-owned agent alias，Feature 不能声明自己真正需要的逻辑执行 slot；
- 条件边当前是“所有命中的边都发 token”的过滤器，不是 if/elif/else；无命中时也没有隐式 else，最终可能以“running invocation 无可计划 transition”失败；
- 若干现有 route 已存在可观测语义缺口，包括 reject 仍继续、不可达 status、rerun 绕过重新判定，以及 coverage 达标与预算耗尽共用一个结果。

用户需要的不是增加一层动态插件系统，而是让仓库结构、Workflow 所有权和运行时边界一致：Framework 提供统一语言与执行权威，Client 隔离外部 agent runtime，六个 Feature subproject 各自拥有流程模块，Product 只显式装配公开流程。

## Solution

将 workspace 明确分为四种架构角色，同时保留现有 distribution 名、Python import namespace、CLI 和 Entrypoint 标识：

| Workspace group | 成员 | 唯一职责 |
|---|---|---|
| Framework | `graph-engine` | Workflow language、module schema、Assembler、compiler、composition、ledger runtime、scheduler、lock 与 host protocol |
| Clients | `agent-runtime-contracts`、`agent-runtime-opencode`、`agent-runtime-cursor` | 外部 agent runtime 的 port、transport、lifecycle 与 provider normalization |
| Features | `assurance-intake`、`assurance-generation`、`assurance-execution`、`assurance-healing`、`assurance-quality`、`assurance-improvement` | 对应领域 Capability 的 contracts、handlers、resources、Feature-owned Workflow module 与测试 |
| Products | `assurance-product` | 精确安装集合、Client binding、公共 Entrypoint、跨 Feature route、CLI 与交付体验 |

The physical workspace target is fixed as follows; this is a source-tree classification, not a distribution/import rename:

```text
packages/
  framework/
    graph-engine/
  clients/
    agent-runtime-contracts/
    agent-runtime-opencode/
    agent-runtime-cursor/
  features/
    assurance-intake/
    assurance-generation/
    assurance-execution/
    assurance-healing/
    assurance-quality/
    assurance-improvement/
  products/
    assurance-product/
```

The uv workspace uses those exact nested member paths. Test discovery, static-type include paths, import-linter configuration, scripts and wheel smoke paths move with them; `examples/` remains a peer of `packages/`.

`Feature` 在本规格中是架构/打包角色，不替代领域语言。Intake、Generation、Execution、Healing、Quality 和 Improvement 仍称为六个 Capability；每个 Capability 由一个 Feature subproject 发布。

每个 Feature 发布一个经过认证的 Workflow module resource。模块声明本地 graph、公开 exports、所需 contracts/bindings，以及必要的 imports。Feature graph 默认 private；只有 exports 是 Product 可调用的稳定 subgraph 接口。

Product 发布一个 root Workflow module（下文简称 Product main）。Product main 只包含公共 Entrypoint、跨 Feature 的 structural orchestration、gate、join、interrupt 和 end；它通过显式 import alias 调用 Feature export，不得引用 Feature 内部 graph、node 或 leaf task。

Framework 在 composition 阶段运行确定性 Assembler：

1. 从 ProductManifest 的精确已安装闭包取得已认证的 Product main 和 Feature module resources；
2. 校验 owner、module identity、imports、exports、public/private、I/O contracts、依赖方向和 namespace；
3. 以与发现顺序无关的 canonical order 合并 graph 与 policy references；
4. 生成一份普通、完整的 Workflow definition；
5. 继续交给现有 compiler，得到一个 CompiledWorkflow；
6. 继续使用现有 FrozenComposition、InvocationLock、ledger 和 runtime 执行。

因此 Engine 运行时仍只执行一个已经编译并锁定的 Workflow，不感知 Feature module，也不在运行过程中动态解析 imports。

Feature 公共流程使用显式 I/O contract。Product call site 将 parent token 投影为 Feature input；Feature 结束时生成并校验 Feature output，Product 只能基于该 output 路由。跨 Feature 不共享隐式 root payload，也不能借 `root_pointer` 读取未声明的 parent state。

迁移分为两个有独立证据的阶段：

1. **结构等价阶段**：拆分单体 Workflow 并装配回来，canonical Workflow/compiled behavior 与迁移前 golden 等价；
2. **边界深化阶段**：启用 public/private、Feature I/O、binding seam 和显式 exclusive/fan-out route，并对已确认的 route 缺口提交逐项行为变更测试。

## User Stories

1. As a Framework maintainer, I want `graph-engine` to contain no Assurance-specific Workflow, so that the engine remains reusable and structurally testable.
2. As a Framework maintainer, I want one authoritative Workflow/module schema, so that Feature packages cannot drift by copying schema files.
3. As a Framework maintainer, I want module assembly to complete before compilation, so that the runtime keeps one execution model.
4. As a Framework maintainer, I want assembly to be deterministic for any input registration order, so that the same installed product always yields the same lock and graph digest.
5. As a Framework maintainer, I want every cross-module reference declared as an import, so that hidden coupling is rejected before an Invocation starts.
6. As a Framework maintainer, I want graphs private by default, so that adding an internal subflow does not accidentally expand the Product API.
7. As a Framework maintainer, I want duplicate exports, missing imports, cycles and namespace collisions to fail closed, so that composition cannot depend on last-write-wins behavior.
8. As a Framework maintainer, I want the existing compiler and runtime to receive one assembled Workflow, so that ledger, scheduling, recovery and replay do not need a second module-aware execution path.
9. As a Client maintainer, I want external runtime protocol and lifecycle logic isolated in Client packages, so that a Feature never depends on OpenCode- or Cursor-specific types.
10. As a Client maintainer, I want `agent-runtime-contracts` to remain the common port, so that another runtime can be added without changing Feature Workflow definitions.
11. As a Feature maintainer, I want my Capability's public flows and private subflows packaged beside its handlers and contracts, so that one subproject owns one coherent change surface.
12. As a Feature maintainer, I want all six Feature subprojects to share the same structural roles, so that contributors and coding agents can navigate them consistently.
13. As a Feature maintainer, I want to export one or more stable public subgraphs, so that the Product can support both the full route and specialized Entrypoints without reaching into internals.
14. As a Feature maintainer, I want internal graphs private and freely refactorable, so that Product definitions do not break when an internal plan/review/fix loop changes.
15. As a Feature maintainer, I want each public export to declare input and output contracts, so that I can test the Feature independently of the Product main.
16. As a Feature maintainer, I want undeclared parent fields unavailable at runtime, so that a passing test proves the declared interface is sufficient.
17. As a Feature maintainer, I want logical capability slots declared by my Feature, so that the Product selects an implementation without owning my internal node topology.
18. As a Feature maintainer, I want module errors to name the importer, owner and symbol, so that composition failures are actionable.
19. As a Product maintainer, I want Product main to call Feature exports rather than Feature nodes, so that the root route stays small and reviewable.
20. As a Product maintainer, I want Product main to own all cross-Feature routing, so that no Feature becomes coupled directly to another Feature package.
21. As a Product maintainer, I want all existing public Entrypoint names preserved, so that existing CLI automation does not need command rewrites.
22. As a Product maintainer, I want Product main to contain structural nodes only, so that leaf operations cannot leak back into the composition root.
23. As a Product maintainer, I want every required Feature and Client version in the exact ProductManifest closure, so that installed ambient packages cannot alter assembly.
24. As a Product maintainer, I want missing or extra Feature contributions rejected before Invocation creation, so that the shipped product is reproducible.
25. As a workflow author, I want exclusive decisions to behave as an exhaustive if/elif/else, so that at most one branch continues.
26. As a workflow author, I want intentional fan-out declared separately, so that multiple Generation families can run without being mistaken for overlapping conditions.
27. As a workflow author, I want a no-match policy on every route, so that a running Invocation cannot become stuck merely because no condition emitted a token.
28. As a workflow author, I want interrupt actions routed independently, so that `reject` can never have the same effect as `approve` by accident.
29. As a QA operator, I want a healed test rerun to pass through the same execution verdict again, so that a still-failing rerun cannot be reported as quality-ready.
30. As a QA operator, I want coverage achieved and repair budget exhausted to be distinct outcomes, so that the Product never labels exhausted work as achieved.
31. As a QA operator, I want impossible contract values rejected during compile/contract checks, so that dead routes cannot masquerade as recovery handling.
32. As a release operator, I want existing source authentication, resource hashing and compiled Workflow locking reused, so that modularization does not weaken provenance.
33. As a release operator, I want old active Invocations to remain fail-closed under a new composition, so that an upgrade cannot silently resume with different code or graph semantics.
34. As a release operator, I want a documented drain-and-pin rollout, so that legacy Invocations can finish in their retained old environment without building migration machinery in this release.
35. As a test author, I want one high-level assembly seam for most architecture tests, so that tests verify public behavior instead of internal helper layout.
36. As a test author, I want a golden proof for the mechanical split before semantic route changes, so that assembly regressions are distinguishable from intentional behavior changes.
37. As a security reviewer, I want modules loaded only from authenticated installed wheel resources, so that this architecture does not become a project-loadable plugin platform.
38. As an organization administrator, I want `.aa/` to remain organization configuration, so that adding Feature modules does not authorize SUT-provided executable operations, validators, gates or artifact shapes.
39. As a contributor, I want distribution names and Python import namespaces preserved, so that the architecture change does not create an unrelated ecosystem-wide rename.
40. As a contributor, I want CI and wheel smoke tests to install the reorganized workspace from built artifacts, so that local editable imports cannot conceal packaging errors.

## Implementation Decisions

### 1. Package and dependency boundaries

- The workspace will expose four top-level package groups: Framework, Clients, Features and Products.
- Existing distribution names, import namespaces, Python entry-point groups, CLI command names and public Workflow Entrypoint names remain stable.
- `graph-engine` is the only Framework package in this scope. It owns structural definitions and execution authority, not Assurance domain flow.
- `agent-runtime-contracts` is grouped with Clients as the provider-neutral port; concrete runtime packages implement that port.
- The six `assurance-*` Capability packages are independent Feature subprojects and remain independently buildable Python wheels.
- `assurance-product` remains a Product subproject. It is not a seventh Feature because it owns composition, CLI and cross-Feature policy.
- Framework may not import Clients, Features or Products.
- Concrete Clients may depend on the provider-neutral contracts and Framework public ports, but not on Features or Products.
- Features may depend on Framework public contracts, provider-neutral runtime contracts, and another Feature's explicitly public `contracts` namespace when declared as a package dependency. They may not import another Feature's plugin/contribution implementation, operations, validators, effects, resource loader, packaged resources or Workflow module.
- Feature-to-Feature **graph** imports remain prohibited; public Python contract reuse does not transfer Workflow ownership.
- Product may depend on the exact selected Framework API, Clients and Features.
- Existing import-linter rules and wheel smoke coverage will be updated to enforce these dependency directions after the workspace move.

### 2. Standard Feature subproject shape

Each Feature subproject will contain the same architectural roles, although the number of concrete files may differ:

- public Python contribution/descriptor surface;
- domain contracts and artifact schemas;
- operations, validators and effects owned by that Capability;
- personas, skills and other packaged resources;
- one canonical Workflow module contribution;
- Feature-level contract, workflow and wheel tests.

The canonical Workflow module may internally reference multiple packaged graph documents if size requires it, but it appears to composition as one immutable module contribution owned by that Feature.

Each Feature must have at least one public export. A Feature may expose multiple exports when the Product has a legitimate specialized Entrypoint, but it must not export every internal graph for convenience.

### 3. One Workflow language schema

- Framework owns one authoritative schema/model for Workflow modules and assembled Workflows.
- Feature packages own Workflow **instances**, not copies of a `workflow-schema.yaml`.
- “主流程和子流程” is represented by ownership and visibility:
  - Product main declares Product Entrypoints and root orchestration;
  - a Feature export is a public callable subgraph;
  - a non-exported Feature graph is a private subflow.
- Main/subflow is not a new node kind and does not create a separate runtime Invocation.
- The schema remains closed: installed Python distributions add capabilities; arbitrary SUT files do not add executable node kinds, operations, validators, gates or artifact models.

### 4. Workflow module contract

The module model will include, at minimum:

- a format/schema version;
- a module role of Product root or Feature;
- a stable module identifier;
- a module version tied to, or authenticated against, the contributing distribution version;
- an authenticated owner derived from the contributing Product or Feature, not trusted from free text alone;
- local retry and timeout policy definitions or qualified references;
- local graph definitions;
- an explicit export table;
- an explicit import table;
- required schema/resource/effect and logical binding references.

Each export declares:

- a stable export name;
- the owned local graph it exposes;
- an input contract identifier;
- an output contract identifier.

All local graphs are private unless listed in exports. Export aliases are public API; local graph identifiers are implementation details.

Only Product root modules may declare CLI-facing Entrypoints. Feature modules expose exports, not product Entrypoints.

Product root and Feature modules share one envelope and parser, but role-specific validation differs: Product roots may declare Entrypoints and cross-module imports; Feature modules declare public exports and private graphs, and the Assurance Product policy requires their cross-Feature import table to be empty.

### 5. Imports, exports and ownership rules

- Every cross-module graph reference must go through a declared import alias.
- An import identifies an exact module owner and public export; it may not reference a local graph name in another module.
- Product main may import Feature exports.
- Feature-to-Feature imports are rejected for `assurance-product`; all cross-Feature sequencing belongs in Product main.
- A module may call its own private subgraphs without exporting them.
- The compiler/assembler rejects undeclared imports, imports of private graphs, duplicate exports, duplicate module identities, ambiguous providers, missing owners, extra undeclared modules, namespace collisions and dependency cycles.
- There is no discovery-order override and no last contribution wins behavior.
- Diagnostics include the importer, requested alias/export and authenticated owner while avoiding filesystem-dependent identities in the canonical result.

### 6. Deterministic Assembler

- Assembler is part of Framework composition, after source and registry authentication and before Workflow compilation.
- It consumes only the frozen ProductManifest closure and authenticated ResourceContribution bytes. It never re-reads ambient module files after source authentication.
- ProductManifest evolves to select a Product root module plus the exact required Feature/Client closure.
- The existing inline/single-resource Workflow form remains available as a legacy composition path during the migration; one Product manifest cannot select both forms at once.
- Module traversal, import resolution and output ordering are canonical and independent of registration, filesystem or mapping iteration order.
- Local graph and policy symbols are qualified or rewritten deterministically so two modules cannot silently collide.
- Assembler emits one ordinary Workflow definition. The existing compiler remains the only path to CompiledWorkflow.
- All assembly errors occur before Invocation claim, lock creation, ledger append or task execution.
- Runtime Engine, scheduler, planner, ledger fold, workspace transaction, effect recovery and replay are not made module-aware.

### 7. Feature input and output contracts

- Every public Feature export is a StateGraph. Product adapters map Product state into that graph state, and the feature publishes a Pydantic public result. Those boundaries are not registered JSON Schema documents.
- A Product subgraph call must declare how its predecessor/root values map to the Feature input. The runtime must actually apply subgraph `input_projection`; static node input alone is not sufficient.
- A Feature receives only its declared input, namespaced Product configuration explicitly allowed by contract, Framework invocation context, and declared bound resources.
- A cross-module Feature call may not use an unrestricted root pointer to inspect arbitrary parent input.
- Existing invocation-root projection semantics remain compatible for legacy Workflows. Feature graphs use an explicit current-graph-input projection source to read the child input they were given; the migration does not silently redefine `root_pointer`.
- A Feature graph publishes its public result on graph state. Product reads that result. The runtime does not validate it against a registered workflow JSON Schema.
- Product downstream gates and projections read the public output token, not the Feature's internal node outputs.
- Output contract/projection failure is deterministic and fail-closed, with no downstream route emitted.
- Calls between private graphs inside one Feature may continue using internal contracts. Crossing the module boundary uses the public graph state and the published Pydantic result.

The module boundary is lowered into generic graph fields before runtime; this is how runtime remains module-unaware:

| Generic field/change | Required behavior |
|---|---|
| subgraph-node `input_schema` | not a workflow JSON Schema id; feature input is graph state filled by the product adapter |
| existing subgraph-node `input_projection` | call-site parent/predecessor → child input mapping; evaluated at child start |
| new `graph_input_pointer` projection source | reads the current GraphInstance input inside the child without changing legacy invocation `root_pointer` semantics |
| subgraph-node `output_projection` | resolved export child-terminal-output → public output mapping |
| subgraph-node `output_schema` | not a workflow JSON Schema id; the public result is the feature Pydantic model |

Assembler copies the subgraph input and output projections onto the ordinary subgraph node. The generic compiled node preserves those fields; it does not preserve module/import/export metadata, and it does not attach a workflow JSON Schema id.

Planner validation points are fixed:

- before `GraphStarted`, the product adapter maps predecessor state into the feature graph;
- when the child graph completes but before parent `NodeCompleted`/`TokenOffered`, Product reads the feature's public Pydantic result;
- the child input and public result are carried on graph state;
- feature input and output are not validated against workflow JSON Schema ids in the schema registry.

No new module-aware runtime event or dynamic export lookup is introduced.

### 8. Product main constraints

- Product main is the only composition root for `assurance-product`.
- It owns the existing public Entrypoints: `intake`, `case`, `full`, `execute`, `archive`, `retro`, `issue-review`, `issue-analyze`, `issue-reconcile`, `improvement-review`, `improvement-evaluate`, `improvement-export`, `improvement-apply`, and `improvement-rollback`.
- Each Entrypoint resolves to a Product-owned root graph or a thin Product wrapper around a Feature export.
- Product root graphs may contain subgraph calls, gates, joins, interrupts and ends.
- Product root graphs may not contain task nodes or direct operation/agent capability references. Existing Product-owned leaf tasks move to the responsible Feature module.
- Product main calls only imported Feature exports. It cannot reference Feature-private graph IDs or internal nodes.
- Product gates may make cross-Feature decisions only from declared Feature outputs and Product policy/configuration.
- Product main selects concrete Client bindings and verifies that every Feature logical slot is satisfied exactly once.

### 9. Initial Feature ownership and public surface

The Product root module ID is `assurance.product.workflow`. The first modular split freezes the following Feature module and export surface; existing internal graph names are not automatically public.

| Module ID | Product import alias / export | Public result | Owns internally |
|---|---|---|---|
| `assurance.intake.workflow` | `intake.prepare` / `prepare` | graph state, terminal status `prepared` | intake, exploration, case design/review and the bounded review loop |
| `assurance.intake.workflow` | `intake.case` / `case` | `CaseFlowResultV1` | case-only design/review flow |
| `assurance.generation.workflow` | `generation.generate` / `generate` | `GenerationCycleResultV1` | family selection and all plan/review/codegen/fix loops |
| `assurance.execution.workflow` | `execution.execute` / `execute` | `ExecutionCycleResultV1` | initial execution and evidence normalization |
| `assurance.execution.workflow` | `execution.rerun` / `rerun` | `ExecutionCycleResultV1` | post-Healing rerun using the same public result |
| `assurance.quality.workflow` | `quality.assess` / `assess` | `QualityAssessPublicV1` | fact baseline, inspect, quality and normalized coverage outcome |
| `assurance.quality.workflow` | `quality.issue-review` / `issue-review` | `QualityIssuePublicV1` | issue triage/review |
| `assurance.quality.workflow` | `quality.issue-analyze` / `issue-analyze` | `QualityIssuePublicV1` | issue analysis and normalized fix eligibility/classification |
| `assurance.quality.workflow` | `quality.issue-reconcile` / `issue-reconcile` | `QualityIssuePublicV1` | issue reconciliation |
| `assurance.quality.workflow` | `quality.report` / `report` | `QualityReportPublicV1` | report materialization without deciding achieved by itself |
| `assurance.healing.workflow` | `healing.repair-failure` / `repair-failure` | `HealingRepairPublicV1` | eligible test/test-data fix proposal and application boundary |
| `assurance.healing.workflow` | `healing.repair-coverage` / `repair-coverage` | `HealingRepairPublicV1` | coverage repair and repair outcome normalization |
| `assurance.improvement.workflow` | `improvement.archive` / `archive` | improvement graph state | archive |
| `assurance.improvement.workflow` | `improvement.retro` / `retro` | improvement graph state | Retro collection, three analyses and reconciliation |
| `assurance.improvement.workflow` | `improvement.review` / `review` | improvement graph state | Improvement review and authenticated lifecycle transition |
| `assurance.improvement.workflow` | `improvement.evaluate` / `evaluate` | improvement graph state | evaluate |
| `assurance.improvement.workflow` | `improvement.export` / `export` | improvement graph state | export |
| `assurance.improvement.workflow` | `improvement.apply` / `apply` | improvement graph state | approved-only apply |
| `assurance.improvement.workflow` | `improvement.rollback` / `rollback` | improvement graph state | rollback |

Feature export boundaries are the feature graph state and the Pydantic public result each graph publishes. Product adapters map Product state into that graph state. Those boundaries are not registered JSON Schema documents.

The Product Entrypoint mapping is also fixed:

| Entrypoint | Product root behavior |
|---|---|
| `intake` | thin root calling `intake.prepare` |
| `case` | thin root calling `intake.case` |
| `full` | Product-owned cross-Feature root using Intake → Generation → Execution → Quality/Healing loops → report → Improvement |
| `execute` | Product-owned cross-Feature root beginning at `generation.generate`, then Execution → Quality/Healing loops → report |
| `archive` | thin root calling `improvement.archive` |
| `retro` | thin root calling `improvement.retro` |
| `issue-review` | thin root calling `quality.issue-review` |
| `issue-analyze` | thin root calling `quality.issue-analyze` |
| `issue-reconcile` | thin root calling `quality.issue-reconcile` |
| `improvement-review` | thin root calling `improvement.review` |
| `improvement-evaluate` | thin root calling `improvement.evaluate` |
| `improvement-export` | thin root calling `improvement.export` |
| `improvement-apply` | thin root calling `improvement.apply` |
| `improvement-rollback` | thin root calling `improvement.rollback` |

### 10. Capability binding seam

Feature-owned deterministic operations continue to use their existing concrete Feature capability IDs. Agent-mediated prepare/execute/finalize nodes use module-local capability slots so Feature-authored Workflow does not contain `assurance.product.agent.*`.

The module schema adds a capability-slot declaration and a task-node `capability_slot` reference, mutually exclusive with concrete `capability`. A slot declares a local name and a versioned capability contract ID. ProductManifest adds an exact slot-binding table keyed by module ID plus local slot.

During assembly:

1. The union of required slots is calculated from the selected modules.
2. Every required slot must have exactly one Product binding and every Product slot binding must name a required slot.
3. The binding names one authenticated, Product-owned concrete capability alias and the same capability contract ID.
4. The concrete alias must exist in the existing CapabilityRegistry as a valid binding whose target handler is in the exact selected closure.
5. Assembler rewrites `capability_slot` to that concrete capability ID in the ordinary Workflow definition before compilation.
6. The compiled Workflow and runtime therefore keep the existing concrete capability lookup; no runtime slot lookup or fallback is added.

The new slot-binding declaration lives in ProductManifest, whose canonical bytes are already locked. Existing `CapabilityBindingContribution` and CapabilityRegistry ownership remain unchanged: Feature modules own the symbolic requirement and its execution contract, while the Product provider owns aliases in the `assurance.product.agent` namespace. Product resolution proves that the declaration's contract ID equals the Feature slot contract and that the selected concrete alias was constructed from that contract before assembly. A Feature never attempts to register a Product-owned registry ID, and this change does not require an InvocationLock schema upgrade.

The initial slot catalog is exact. Every base slot below expands to three local task slots named `<base>.prepare`, `<base>.execute` and `<base>.finalize`; its contract ID is `assurance.<feature>.agent.<base>.v1`, and the Product concrete alias is `assurance.product.agent.<feature>.<base>.<phase>`.

| Feature module | Base slots |
|---|---|
| Intake | `intake`, `explore`, `case-design`, `case-review` |
| Generation | `api.plan`, `api.plan-review`, `api.codegen`, `api.codegen-fix`, `e2e.plan`, `e2e.plan-review`, `e2e.codegen`, `e2e.codegen-fix`, `fuzz.plan`, `fuzz.plan-review`, `fuzz.codegen`, `performance.plan`, `performance.plan-review`, `performance.codegen` |
| Execution | `execute`, `run` |
| Quality | `fact-baseline`, `inspect`, `issue-triage`, `issue-analysis`, `report` |
| Healing | `coverage-repair`, `fix-proposal` |
| Improvement | `archive`, `improvement-review`, `retro`, `retro-eval-analysis`, `retro-issue-analysis`, `retro-workflow-analysis` |

This catalog contains 33 base agent contracts and 99 phase slots, matching the current 99 Product-owned agent task aliases. Adding/removing a slot later is an explicit Feature/Product interface change. Concrete Client/provider types do not appear in Feature Workflow I/O schemas. Existing non-agent task handler IDs and contribution ABI names remain supported; this scope does not rename the entire `Plugin*` Python API to `Feature*`.

### 11. Routing semantics

Workflow routing must distinguish two explicit modes:

- **Exclusive decision**: logical if/elif/else. Exactly one route continues. Conditioned branches are mutually checked; an explicit otherwise branch handles no matches. Multiple matches are an overlap error rather than silently choosing by declaration order.
- **Fan-out decision**: every matching route continues. The declaration states whether zero matches is allowed; Feature family selection requires at least one match.

The generic Workflow schema change is fixed and intentionally small:

| Schema element | Shape/semantics |
|---|---|
| node `routing.mode` | `exclusive` or `fanout`; required on every new modular node with more than one outgoing edge |
| node `routing.min_matches` | non-negative integer allowed only for fan-out; routing fails before token emission if fewer edges match |
| edge `otherwise` | boolean allowed only under exclusive routing, mutually exclusive with `condition`, with exactly one otherwise edge required |

For backward compatibility, a legacy node with omitted `routing` retains today's “emit every matching/unconditional edge, zero allowed” behavior. Modular Product/Feature modules may omit `routing` only when the node has zero or one outgoing edge.

Exclusive routing evaluates all conditioned edges against the same immutable input/output scope before emitting any token. More than one true condition is `ambiguous_route`; no true condition selects the sole otherwise edge. Fan-out routing evaluates all edges first, rejects a `min_matches` violation, then emits all selected tokens in canonical edge order. `otherwise` is invalid for fan-out.

Existing `join: all|any` semantics and existing token events remain unchanged in this scope. Generation continues to use four fixed lanes: each lane emits either a completed token or a structural skipped token into the existing all-join; a skipped lane dispatches no Feature task. This scope does not add a dynamic selected-predecessor join.

Additional rules:

- An unconditional edge is not evaluated in parallel with conditioned edges as an accidental else.
- Every exclusive decision has an otherwise outcome, even when that outcome is an explicit failure or human interrupt.
- Decision input is one immutable value that has already passed the owning Feature's I/O contract; missing, contradictory or unknown fields fail closed instead of becoming a false predicate.
- Route conditions are evaluated against declared predecessor/public output contracts.
- Module contract tests enumerate every declared public outcome and prove one route; the generic expression compiler is not expanded into a full static theorem prover.
- A no-match or overlap error reports the graph, node and evaluated route set before the generic no-progress state.
- Condition declaration order does not affect the selected result.
- Joins name the fan-out they synchronize, define a deterministic aggregation rule and cannot accidentally wait for a branch that was not selected.
- Every interrupt action has exactly one explicit successor; unavailable or tampered actions are rejected.
- Review, Healing and coverage loops consume their declared business budgets. `max_activations` remains a safety backstop, not the normal loop budget.
- Generating a report and reaching `achieved` are different decisions. A report may describe failure or exhaustion without turning it into success.
- Route selection is represented by the existing canonical `TokenOffered` events; replay requires no new route-decision event type.

### 12. Required route behavior corrections

These corrections occur only after the structure-equivalence golden is established, and each produces an explicit intentional diff:

| Area | Required target behavior |
|---|---|
| Case review | This loop is owned by Intake. The public review result normalizes compatibility values into `pass`, `needs_fix`, `needs_human` or `reject`, and validates that decision, auto-fix and human-review fields agree. `pass` completes the Intake flow; auto-fixable `needs_fix` returns to case design and consumes review budget; `needs_human` interrupts. Human `approve` completes the flow, `reject` reaches an explicit rejected terminal, and an optional declared `request_rework` returns to design under budget. Product sees only the final public Intake outcome. |
| Generation family selection | This route is owned by Generation. Input is a normalized, non-empty, duplicate-free subset of API, E2E, Fuzz and Performance. The four fixed lanes are intentional fan-out: every selected family runs exactly once; an unselected lane emits only its structural skipped token and runs no task; the existing all-join receives four lane outcomes and aggregates only selected results independent of completion order. Empty, unknown or duplicate selections fail at the Feature boundary. |
| Generation plan review | This route is owned by Generation. `pass` proceeds to codegen; auto-fixable `needs_fix` returns to the matching plan and consumes review budget; human review interrupts. Human `approve`, `reject` and `request_rework`, when exposed, respectively proceed, terminate, or re-enter the plan loop. Contradictory review fields fail the contract. |
| Generation codegen review | This route is owned by Generation. The output contract is versioned so the existing fixer loop has an explicit reachable `needs_fix` verdict with repair payload; accepted output completes the family, and fix output enters the bounded fixer loop. The current contract-impossible `needs_fix=true` branch is not retained unchanged. |
| Initial execution verdict | Execution emits only contract values `passed` or `failed`. `passed` proceeds to Quality; `failed` proceeds to Quality triage/classification. Scheduler/host infrastructure failure uses retry policy and, after exhaustion, an explicit non-achieved outcome. Product never compares raw Execution status with invented `product_issue` or `infrastructure_failure` values. |
| Failure classification | Quality emits a normalized classification. Test or test-data failures marked fix-eligible may enter Healing while budget remains. Product bugs are reported/tracked and must not trigger a test fix. Environment/infrastructure failures follow infrastructure policy and must not trigger a test fix. Unknown, pending or failed analysis interrupts or terminates explicitly. |
| Rerun after healing | Healing followed by Execution rerun returns to the same execution verdict and classification route. Only a passed rerun reaches normal Quality assessment; a failed rerun is analyzed again while Healing budget remains, otherwise it reaches an explicit exhausted/non-achieved result. `run → quality` may not be unconditional. |
| Coverage | Quality owns a normalized outcome: `satisfied`, `repair_required`, `exhausted`, `needs_human` or `inconclusive`. `satisfied` includes equality with the threshold and may continue toward achieved; repair-required with budget enters coverage repair; exhausted reports but cannot achieve; human interrupts; inconclusive fails closed. Each repair atomically increments rounds, never exceeds budget, and its `repaired`, `not_eligible`, `exhausted`, `failed` or `needs_review` result is routed separately. Only fresh Quality evidence marked satisfied permits achieved. |
| Improvement apply | This decision is owned by Improvement. Auto-review pass first materializes an authenticated approved lifecycle projection; changes requested, needs-human and reject advice cannot apply directly. Human `approve` transitions to approved and may apply after required evaluation passes; `reject`, `request_rework` and `supersede` transition to distinct non-apply outcomes. Apply requires proof of approved state and successful evaluation, not merely an interrupt payload. |

### 13. Existing integrity mechanisms are reused

- Feature Workflow module declarations are registered as existing authenticated resources owned by their Feature distribution.
- Existing source snapshots, provider identity, resource SHA-256, dependency closure and registry projections authenticate module source and bytes.
- Assembler implementation is covered by the existing Framework source digest.
- The final assembled CompiledWorkflow and digest remain stored in InvocationLock v2.
- Existing byte-exact lock authentication, resume drift rejection, atomic lock persistence, ledger canonical hashes and deterministic replay remain authoritative.
- This scope does not create a second module lock or weaken exact-composition resume.
- A changed Feature Workflow resource must change authenticated composition and/or final Workflow digest and be rejected when reopening an Invocation created with different bytes.

### 14. This is not a project plugin platform

- ProductManifest statically selects exact Product, Feature and Client providers and versions. Merely installing another distribution does not make its module available to assembly.
- Assembler does not scan the SUT, current working directory, generic `plugins/` folders, URLs or arbitrary Python import paths.
- `.aa/` remains namespaced organization configuration. It cannot nominate module sources, distributions, Python entry points, operations, validators, effects, gate callables, node kinds or artifact schema implementations.
- Workflow YAML stays inside the closed graph language. It cannot embed Python callables, shell commands, dynamic imports, URL fetches or custom expression functions.
- Skills, personas, prompts and executable handlers come only from the exact authenticated wheel closure.
- Assembly occurs once during resolve/compile. Runtime never re-discovers or hot-loads modules.

Guardrail: YAML may compose only public graphs authorized by the installed Product; Python wheels add capability; `.aa/` provides organization configuration, not code or new modules.

### 15. Compatibility and rollout

- Existing public CLI commands and the 14 Workflow Entrypoint names remain stable for new Invocations.
- Distribution names, Python import namespaces and entry-point identifiers remain stable; only workspace grouping and internal ownership change.
- The legacy monolithic Workflow composition form remains readable during the migration and can serve as the golden reference.
- Module migration increments affected Product/Feature versions; wheel bytes are never replaced under an existing version.
- Automatic cross-version resume is not provided. The rollout uses drain-and-pin:
  - stop creating long-lived legacy Invocations before cutover;
  - retain an immutable legacy environment containing the old Framework, Product, Features, Clients and configuration;
  - resume legacy Invocations only with that pinned environment until terminal and exported/archived;
  - start new Invocations with the modular release;
  - a modular runner opening a legacy Invocation continues to fail closed before ledger mutation and directs the operator to the legacy runner.
- The retained legacy environment may be selected operationally; Framework does not add automatic historical wheel selection in this scope.
- Producing or storing a legacy wheelhouse/container/runner is a release-operations responsibility, not a repository deliverable of this specification. Repository acceptance covers fail-closed mismatch behavior and the rollout runbook, not executing an unspecified historical environment in CI.

### 16. Delivery slices

1. Establish nested workspace groups, dependency rules and build/wheel-smoke support while preserving public package identities.
2. Add Workflow module schema/contribution models and deterministic Assembler behind the existing legacy Workflow path.
3. Register each Feature's module resource and mechanically extract graphs from the monolith.
4. Add Product main and prove canonical structure/behavior equivalence for all existing Entrypoints.
5. Enforce imports/exports, graph ownership, private-by-default visibility and Product structural-only rules.
6. Implement subgraph input projection, Feature output contracts/projection and logical capability bindings.
7. Convert routes to explicit exclusive/fan-out semantics and apply the listed intentional behavior corrections.
8. Complete full CI, wheel installation smoke, rollout documentation and legacy drain-and-pin tests.

Each slice remains compilable and testable. The old monolith is removed from the active Product only after the assembled golden and Entrypoint parity checks pass.

## Testing Decisions

Tests prefer the highest stable seam: given a ProductManifest, authenticated contribution registries and module resources, resolve composition and observe either one canonical CompiledWorkflow or a typed composition failure. Tests should assert public graph/lock/runtime behavior, not helper calls, internal class layout or physical source paths.

### Architecture and assembly tests

- The same module set in every tested permutation produces byte-identical assembled Workflow and lock projection.
- A mechanical modular split produces the approved canonical golden before intentional semantic changes.
- Every Product Entrypoint resolves and compiles after assembly.
- Missing Feature, extra undeclared Feature, missing import, duplicate export, private graph access, owner mismatch, namespace collision and dependency cycle fail before Invocation creation.
- A Feature-to-Feature import is rejected for this Product.
- Product main containing a task node or direct Feature capability reference is rejected.
- Product main can call every declared public export and cannot call internal graph names.
- Feature module resource byte drift changes authenticated composition/final lock.
- Assembly consumes authenticated frozen resource bytes and does not observe a later ambient filesystem replacement.
- Legacy single-Workflow and modular paths both use the same compiler and emit the same compiled model shape.
- An additional installed but ProductManifest-undeclared Python entry point/module is ignored and cannot change assembled bytes.
- Fake module YAML, Python packages or `plugins/` trees placed in the SUT/current working directory are never scanned or imported.
- `.aa/` keys attempting to select a module, distribution, entry point, path, export, capability, schema implementation or source closure are rejected as unknown configuration.
- Product/organization configuration cannot add a provider or extend the authenticated module closure.
- Module imports expressed as filesystem paths, globs, URLs or Python import paths are rejected; only declared module/export symbols resolve.

### Feature I/O tests

- A Product call maps declared parent fields into the Feature input exactly.
- Subgraph input projection is exercised at runtime, not merely preserved in a manifest projection.
- A Feature cannot read an undeclared parent/root field at a public boundary.
- Missing input fields, extra forbidden fields and wrong types fail before the Feature starts work.
- Feature output is validated before a downstream Product gate receives it.
- Output projection exposes only the declared public result and not internal node tokens.
- Product gates operate on the public output contract and remain valid after a Feature-private graph rename.
- Logical capability slots fail composition when missing, duplicated or contract-incompatible.
- The same Feature tests run with fake provider-neutral bindings; no concrete OpenCode server is required.

### Routing behavior tests

- Exclusive route: one true condition selects one branch; no true condition selects otherwise; two true conditions fail with overlap diagnostics.
- Reordering condition declarations does not change decision output, tokens or terminal state; replay produces the same route.
- Fan-out route: all selected Generation families run; every fixed lane reaches the all-join with either a completed or structural skipped token; empty selection fails explicitly and no unselected Feature task is dispatched or awaited.
- Every exposed interrupt action has exactly one successor; unknown or tampered actions are rejected.
- Case review covers pass, auto-fix, human approve, human reject, contradictory fields and review-budget exhaustion as distinct cases.
- Generation selection covers all 15 non-empty family subsets plus empty, duplicate and unknown inputs; branch completion order does not change aggregate output.
- Each Generation family covers pass, fix loop, human approve/reject/rework and review-budget exhaustion outcomes allowed by its contract.
- Codegen fix is reachable when the verdict requests it and bounded by activation/retry limits.
- Execution covers passed, fix-eligible test failure, product bug, environment/infrastructure failure and unknown classification without comparing against impossible status values; non-test failures never dispatch a test fix.
- A failed post-healing rerun re-enters failure classification; only a passed rerun reaches normal Quality assessment, and Healing budget exhaustion is explicitly non-achieved.
- Coverage tests distinguish above/equal threshold, repair-needed, zero/exhausted budget, human and inconclusive outcomes; rounds are monotonic and never exceed budget; every repair result variant has a route.
- Improvement apply tests prove approved state plus successful evaluation is required; reject, request-rework and supersede cause no apply effect or write authorization.
- Report-producing infrastructure/coverage failure cases do not reach the achieved terminal.
- No supported route ends in a running Invocation with no planned transition.

### Compatibility, integrity and end-to-end tests

- All existing public CLI commands continue to resolve the same Entrypoint names for new Invocations.
- New composition opening a legacy Invocation fails before claim/append and leaves ledger bytes unchanged.
- Existing same-composition resume/replay tests remain passing; CI does not construct a separate historical release environment.
- Existing source tamper, dependency ordering, registry collision, lock authentication, crash-cut, symlink/hardlink and replay suites remain passing.
- A built-wheel clean-environment smoke test installs Framework, Clients, six Features and Product, then compiles all Entrypoints without editable-source leakage.
- Deterministic full-route tests use fake handlers to cover intake → generation → execution → quality/healing loops → report → improvement terminal outcomes.
- The repository's complete quality gate passes: lint, format check, static typing, import boundaries, tests and Product wheel smoke.

### Acceptance checklist

- [ ] Workspace packages occupy the exact `packages/framework`, `packages/clients`, `packages/features` and `packages/products` tree declared in Solution, with nested uv/test/type/import/wheel paths updated and public distributions/imports unchanged.
- [ ] All six Feature subprojects package one authenticated Workflow module and have the same structural roles.
- [ ] Product main contains no Feature-internal graph reference and no task node.
- [ ] Every cross-module call resolves through a public export with validated I/O.
- [ ] Assembler produces one deterministic Workflow before the existing compiler runs.
- [ ] Engine runtime remains module-unaware and uses the existing CompiledWorkflow/InvocationLock path.
- [ ] All 14 existing Entrypoints compile from the modular Product.
- [ ] Mechanical extraction has an approved equivalence golden.
- [ ] Listed route corrections have explicit behavioral tests and intentional golden diffs.
- [ ] Existing integrity/replay suites and full CI pass.
- [ ] Drain-and-pin rollout instructions exist and legacy/new composition mismatch remains fail-closed.
- [ ] Ambient entry points, SUT-local fake modules and `.aa/` source-extension attempts are covered by negative tests and cannot alter assembly.

## Out of Scope

- No `InvocationLock` schema upgrade solely to add module-level provenance.
- No persisted assembly plan, graph/node-to-Feature origin map, export/import provenance graph or assembler algorithm version field in the lock.
- No automatic reconstruction of executable providers from a historical lock.
- No automatic installation or side-by-side selection of old Product/Feature/Client wheels.
- No repository-built legacy wheelhouse, container image or historical runner harness.
- No cross-version Invocation migration, graph/node/activation ID mapping, ledger state migration or relaxed compatibility resume.
- No rolling mixed-version runner protocol or online upgrade coordinator.
- No exhaustive production-certification matrix for every assembler crash cut, TOCTOU/path-swap combination, property fuzz campaign or independent security audit; essential deterministic and fail-closed tests listed above remain in scope.
- No remote signatures, SBOM transparency service or new ledger signature scheme.
- No dynamic Feature discovery from the SUT, organization project tree or arbitrary `.aa/` executable definitions.
- No project-loadable operations, validators, gate builtins, artifact shapes or Client implementations.
- No replacement of the existing graph runtime with LangGraph, Temporal, Dagster, Prefect or Argo.
- No standalone deployment/service/process per Feature; a Feature subproject is a package/module boundary, not an independently running Workflow service.
- No wholesale rename of `PluginContribution`, `PluginDescriptor`, Python entry-point groups or all existing capability identifiers to Feature terminology.
- No unrelated domain behavior redesign beyond the route corrections explicitly listed in this specification.

## Further Notes

### Effort allocation

| Workstream | Estimate |
|---|---:|
| Workspace grouping, dependency rules and packaging updates | 3–4 PD |
| Workflow module schema, contribution API and deterministic Assembler | 6–8 PD |
| Subgraph input execution, Feature output contract/projection | 5–7 PD |
| Six-Feature Workflow extraction, Product main and binding seam | 9–11 PD |
| Explicit route semantics and listed behavior corrections | 3–4 PD |
| Integration tests, full CI, wheel smoke, docs and rollout | 4–6 PD |
| **Total** | **30–40 PD** |

The estimate assumes existing distribution/import names and the current `Plugin*` contribution ABI are preserved, existing lock/provenance/runtime machinery is reused, and legacy Invocations use drain-and-pin. Expanding any of those assumptions moves the work toward or beyond the excluded 40–55 PD scope.

### Primary risks

- The mechanical split and semantic route repair can become indistinguishable unless the equivalence golden is completed first.
- Feature I/O may expose hidden dependence on Invocation root data because current subgraph calls frequently omit input projections.
- Product-owned agent aliases may require staged compatibility bindings while Feature-owned slots are introduced.
- Graph/retry/timeout namespacing can change canonical identifiers; migration must either preserve them in the equivalence stage or record the expected golden diff.
- Existing active Invocations cannot safely cross the composition change; rollout inventory and retention of the old executable environment are release prerequisites.

### Definition of done

The architecture change is complete when a clean wheel installation can resolve `assurance-product`, authenticate six Feature Workflow modules and selected Clients, assemble one deterministic Workflow, compile all existing Entrypoints, execute deterministic fake-handler scenarios through every corrected route, and persist/reopen new Invocations through the unchanged lock/ledger runtime—with no Product reference to Feature-private graphs or leaf tasks and no ability to load executable modules from the SUT.
