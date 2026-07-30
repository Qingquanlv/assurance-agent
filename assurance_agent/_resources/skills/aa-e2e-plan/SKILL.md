---
name: aa-e2e-plan
description: Use when a QA Case Delta contains E2E automation cases and you need to generate reviewable E2E plan files before writing any Playwright code. Triggers on: "generate E2E plan", "M4 Stage 1", "e2e-plan.md", "review before E2E codegen", "E2E planning". Only processes E2E cases with automation.required = true. Never generates test code.
---

## Per-Skill Memory

Before producing output, check whether `.aa/memory/aa-e2e-plan.md` exists in the project root. If it exists, read it before producing output and apply only entries that are not marked `deprecated:`. Treat the file as read-only runtime guidance; do not create, edit, or delete `.aa/memory/**`.

## Test Data Architecture Contract

- Shared business-valid builders live in `tests/testdata/domain/`; E2E setup/cleanup transport lives in `tests/e2e/adapters/`.
- Default E2E transport is an authenticated admin/test HTTP API. If no safe endpoint exists, plan an isolated subprocess worker with bounded timeout and symmetric cleanup.
- A synchronous Playwright test must never import an async shared domain factory or bridge its event loop. Map capabilities through `capabilities.domain_factories` and `capabilities.adapters.e2e`.
- Shared files are `create-if-missing` for the first active codegen layer and reuse-only thereafter.

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/workflow-state.yaml`.
2. Verify `phases.case_review.status == pass` (gate must be cleared).
3. Read **Required** input files from disk:
   - `qa/changes/<change-id>/.qa.yaml`
   - `qa/changes/<change-id>/proposal.md`
   - `qa/changes/<change-id>/cases/**/case.yaml`
   - If any required file is missing, **STOP** and report.
4. Read **Optional** input files from disk (missing = warning, do not STOP):
   - `.aa/config.yaml`
   - `.aa/data-knowledge.yaml` (if missing, proceed and generate proposal — see Data Knowledge Layer Rules)
   - Frontend source files (for route / selector confirmation)
   > **data-knowledge 分层规则：**
   > - `aa-e2e-plan` 阶段：`.aa/data-knowledge.yaml` 缺失不阻断 planning；生成 `data-knowledge.proposal.e2e.yaml`；**Codegen Readiness 默认为 `not_ready`**，除非 selected E2E cases 完全不需要 auth / data setup / route resolution / cleanup capability（见 Codegen Readiness 判定）。
   > - `aa-e2e-codegen` 阶段：`.aa/data-knowledge.yaml` **必须存在**。`data-knowledge.proposal.e2e.yaml` 仅供参考，不可替代。
5. Use files as the sole source of truth.

**After completing work:**

1. Write output files:
   - `qa/changes/<change-id>/plans/e2e-plan.md`
   - `qa/changes/<change-id>/plans/e2e-test-data-plan.md`
   - `qa/changes/<change-id>/plans/e2e-codegen-plan.md`
   - `qa/changes/<change-id>/plans/m4-review-summary.md`
   - `qa/changes/<change-id>/plans/data-knowledge.proposal.e2e.yaml`（仅当 `.aa/data-knowledge.yaml` 缺失时）
2. Report the `workflow-state.yaml` state delta (inline mode: apply it directly; dispatched subagent: never write `workflow-state.yaml` — report the values in your final message and the orchestrator applies them):
   - `phases.e2e_plan.status = done`
   - `phases.e2e_plan.outputs` = **all actually generated** output files（含 `data-knowledge.proposal.e2e.yaml`，若已生成）

---

# E2E Planning for QA

名称：E2E 测试计划生成

描述："QA 超能力：在生成 Playwright E2E 测试代码之前必须使用。根据 case.yaml、proposal.md 和数据知识库生成可 Review 的 e2e-plan.md、e2e-test-data-plan.md、e2e-codegen-plan.md 和 m4-review-summary.md。此 Skill 不生成测试代码，不执行 Playwright。"

将 QA Case Delta 转化为可审查的 E2E 测试实现计划。

此 Skill 是 AA M4 的 Stage 1。它读取 `qa/changes/<change-id>/cases/**/case.yaml`，筛选 E2E 自动化 Case，生成 E2E 测试计划、前置数据计划和代码生成计划。完成后进入 `aa-e2e-plan-reviewer`；只有 `plan-review.json` 满足 gate 条件后，orchestrator 才可进入 `aa-e2e-codegen`。

## When to Use

当用户要求：

- 生成 M4 E2E plan
- 根据 case 生成 e2e-plan
- 生成 e2e-test-data-plan
- 生成 e2e-codegen-plan
- 为 Playwright 自动化测试做计划

使用此 Skill。

## Do Not Use When

以下情况不得使用此 Skill：

- 用户已经要求根据 plan 生成 Playwright 测试代码
- 用户要求执行 Playwright
- 用户要求直接生成 E2E 测试代码（codegen 阶段的事，不是 plan 阶段）
- 用户要求生成 execution result
- 需求还没有生成 case.yaml
- case.yaml 尚未通过人工 Review
- 用户要做 API-only 测试计划

## Inputs

**Required**（缺失则 STOP）：

- `qa/changes/<change-id>/.qa.yaml`
- `qa/changes/<change-id>/proposal.md`
- `qa/changes/<change-id>/cases/**/case.yaml`

**Optional**（缺失则 warning，不 STOP）：

- `.aa/config.yaml`
- `.aa/data-knowledge.yaml`
- Frontend source files（用于确认 route / selector）

如果 `.aa/config.yaml` 不存在，记录 warning，但不要阻断。

如果 `.aa/data-knowledge.yaml` 不存在，**不阻断 plan 阶段**。不得直接写入项目级知识库。必须生成 proposal：

`qa/changes/<change-id>/plans/data-knowledge.proposal.e2e.yaml`

并在 `m4-review-summary.md` 中记录 warning，将 **Codegen Readiness** 设为 `not_ready` 或 `ready_with_warnings`（见 Codegen Readiness 判定）。

> **Plan vs Codegen gate（data-knowledge）：**
> - **aa-e2e-plan**：`.aa/data-knowledge.yaml` 缺失不阻断 planning；生成 `plans/data-knowledge.proposal.e2e.yaml`；**Codegen Readiness 默认为 `not_ready`**，除非 selected E2E cases 完全不需要 auth / data setup / route resolution / cleanup capability。
> - **aa-e2e-codegen**：`.aa/data-knowledge.yaml` 缺失是 **BLOCKER**；`data-knowledge.proposal.e2e.yaml` 仅供参考，不可替代正式知识库。
> - **Reason**：`aa-e2e-codegen` 启动前均要求正式 `.aa/data-knowledge.yaml` 存在；orchestrator 须在文件补齐并 re-review 后才可进入 codegen。

## Outputs

只能生成：

- `qa/changes/<change-id>/plans/e2e-plan.md`
- `qa/changes/<change-id>/plans/e2e-test-data-plan.md`
- `qa/changes/<change-id>/plans/e2e-codegen-plan.md`
- `qa/changes/<change-id>/plans/m4-review-summary.md`
- `qa/changes/<change-id>/plans/data-knowledge.proposal.e2e.yaml`（仅当 data knowledge 缺失时）

不得生成：

- `/tests/e2e/test_*.py`（测试代码由 aa-e2e-codegen 生成）
- `/tests/e2e/*.spec.ts`（项目不使用 TypeScript Playwright）
- `/tests/e2e/adapters/*.py`
- `/tests/helpers/*`
- `/tests/e2e/pages/*`
- `qa/changes/<change-id>/scripts/*`
- `qa/changes/<change-id>/execution/*`

**E2E framework 约定：Python Playwright，文件命名 `test_<module>_e2e.py`，执行命令 `uv run pytest tests/e2e/ -v --headed`。不使用 TypeScript Playwright（`*.spec.ts` + `npx playwright test`）。**

## Workflow

1. 读取 `.aa/config.yaml`，如果不存在则记录 warning。
2. 读取 `qa/changes/<change-id>/.qa.yaml`。
3. 读取 `qa/changes/<change-id>/proposal.md`。
4. 定位 `qa/changes/<change-id>/cases/**/case.yaml`。
5. 解析 `schema_version`、`added`、`modified`、`removed`。
6. **只从 `added` 和 `modified` 中**筛选 `type = E2E` 且 `automation.required = true` 的 Case。如果没有符合条件的 Case，停止并报告。
   - **不得**为 `removed` 下的 case 生成 E2E plan。
   - `removed` 条目（仅含 `case_id` / `reason`）只用于上下文与 archive / trace 校验，不参与 plan 筛选。
7. 如果是 `type = Mixed`，只提取 E2E 目标并标记来源为 Mixed。
8. 检查 priority：默认只计划 P0 / P1 E2E，P2 / P3 必须进入 Needs Review。
9. 读取 `.aa/data-knowledge.yaml`。
10. 如果 `.aa/data-knowledge.yaml` 不存在，生成 `plans/data-knowledge.proposal.e2e.yaml`，不得写入 `.aa/data-knowledge.yaml`，并在 `m4-review-summary.md` 中记录 warning。
11. 如果没有任何 E2E case（`added` + `modified`），停止并报告。
12. 生成 `plans/e2e-plan.md`（如果 `plans/` 目录不存在则创建）。
13. 生成 `plans/e2e-test-data-plan.md`。
14. 生成 `plans/e2e-codegen-plan.md`。
15. 生成 `plans/m4-review-summary.md`。
16. 停止，不生成测试代码。
17. 提醒下一步为 `aa-e2e-plan-reviewer`；orchestrator 进入 `aa-e2e-codegen` 须同时满足：
    - `plan-review.json` `decision == "pass"`
    - `codegen_readiness in ["ready", "ready_with_warnings"]`
    - `.aa/data-knowledge.yaml` 存在
    用户口头确认不能替代 gate。

## Checklist

必须按顺序完成：

- [ ] 找到 change-id
- [ ] 读取 `.qa.yaml`
- [ ] 读取 `proposal.md`
- [ ] 读取 `cases/**/case.yaml`
- [ ] 确认 `schema_version: "1.0"`
- [ ] 只从 `added` / `modified` 筛选 E2E 自动化 Case（`type: E2E` + `automation.required: true`）；跳过 `removed`
- [ ] 确认只默认处理 P0 / P1 E2E（P2 / P3 进入 Needs Review）
- [ ] 读取 `.aa/data-knowledge.yaml`（不存在则写 `plans/data-knowledge.proposal.e2e.yaml`，不得直接写 `.aa/data-knowledge.yaml`）
- [ ] 生成 `e2e-plan.md`
- [ ] 生成 `e2e-test-data-plan.md`
- [ ] 生成 `e2e-codegen-plan.md`
- [ ] 生成 `m4-review-summary.md`
- [ ] 确认未生成 Playwright 测试代码
- [ ] 更新 `workflow-state.yaml`（`phases.e2e_plan.status = done`）
- [ ] 输出下一步提示（指向 `aa-e2e-plan-reviewer`，而非直接 `aa-e2e-codegen`）

## Output Contract

### e2e-plan.md

路径：`qa/changes/<change-id>/plans/e2e-plan.md`

必须包含：

- **Source** — case 来源文件路径
- **Scope** — Case ID 和标题列表（仅来自 `added` + `modified`；不含 `removed`）；Scope uses `Case ID | Title`，且每个 in-scope case 必须以 full case_id（如 `TC_USER_E2E_001`）出现在 Case ID 列
- **User Journey** — 用户路径说明
- **Route Mapping** — 页面路径、入口页面、目标页面
- **Natural Steps** — 来自 `case.yaml:steps`
- **Assertion Strategy** — 每个 Case 的可观察 UI 断言
- **Selector Strategy** — role / label / text / testid / CSS fallback 的策略说明
- **Network / API Dependency** — 依赖的后端接口
- **Flaky Risk Notes** — 异步、动态数据、第三方依赖、加载状态
- **Output File Candidates** — 建议测试文件路径
- **Needs Review**
- **Blockers**

Selector Strategy 优先级（必须遵守）：

1. role
2. label
3. text
4. testid
5. CSS（只作为 fallback）

禁止：
- 将 locator 写入 case.yaml
- 默认生成 POM

### e2e-test-data-plan.md

路径：`qa/changes/<change-id>/plans/e2e-test-data-plan.md`

必须包含：

- **Scope** — 需要数据准备的 Case 列表；Scope uses `Case ID | Title`
- **Required Data** — 表格：Entity \| State \| Capability；Required Data uses `Entity | State | Capability`
- **Preconditions** — 来自 `case.yaml:preconditions`
- **Data Setup Strategy** — 优先通过脚本或 API 构造；UI 造数只作为最后 fallback
- **Data Setup Script Plan** — 脚本路径、输入、输出、cleanup 规划
- **Capability Mapping** — 表格：Need \| Capability \| Source \| Status（found/missing/warning）；Capability Mapping uses `Need | Capability | Source | Status (found/missing/warning)`；Source 引用正式知识库时只能写 `.aa/data-knowledge.yaml`
- **Runtime Verify Strategy** — UI 步骤前验证数据状态的断言
- **Login / Auth State Strategy** — storageState 或 per-test login 策略
- **Cleanup Strategy** — 测试后数据清理
- **No Data Required Cases** — 不需要数据准备的 Case
- **Blockers / Assumptions** — 未解决的数据依赖
- **Review Checklist**

### e2e-codegen-plan.md

路径：`qa/changes/<change-id>/plans/e2e-codegen-plan.md`

此文件只描述如何生成测试代码，不直接包含测试代码。

必须包含：

- **Target Files** — 表格：File \| Purpose；Target Files uses `File | Purpose`；domain factory 文件在 Purpose 中标注 `create-if-missing` 或 `reuse`，需要 domain 数据的 case 必须列出对应 E2E adapter
- **Test Function Mapping** — 表格：Case ID \| Test Function \| Target File（仅 `added` + `modified`；不含 `removed`）；Test Function Mapping uses `Case ID | Test Function | Target File`
  - **命名（硬规则）：** 每个 Test Function 必须是 `test_<case_id_lowercase>__<desc>` —— 小写 case_id 前缀 + **双下划线**。例：`TC_USER_E2E_001` → `test_tc_user_e2e_001__admin_enters_user_list`。
- **Factory Mapping** — 表格：Entity \| Shared Module \| Function \| Ownership \| Required By；Factory Mapping uses `Entity | Shared Module | Function | Ownership | Required By`。当 `.aa/data-knowledge.yaml` 声明 shared domain factory 时必须出现此表。`Shared Module` 命名 `tests/testdata/domain/<entity>.py`，`Function` 命名 `make_*`/`cleanup_*` capability，`Ownership` 对 L1 已声明 symbol 为 `reuse`，仅 L1 未声明 symbol 可用 `create-if-missing`。`reuse` for every symbol already declared by L1 knowledge；`create-if-missing` only when L1 does not declare that shared symbol。runtime `check_shared_factory` 解析此表，plan 阶段不得复制 gate policy。
- **Adapter Mapping** — 表格：Entity \| E2E Adapter \| Transport \| Cleanup；Adapter Mapping uses `Entity | E2E Adapter | Transport | Cleanup`。E2E Adapter 命名 `tests/e2e/adapters/<module>.py`，Transport 为 `in_process_async`、`http` 或 `isolated_worker`
- **Fixture Mapping** — 表格：Fixture \| Source Factory \| Wrapper Only (yes/no) \| Required By；Fixture Mapping uses `Fixture | Source Factory | Wrapper Only (yes/no) | Required By`；fixture 只包装 lifecycle/transport
- **Data Setup Script Mapping** — 表格：Script \| Input \| Output \| Required By
- **Import Strategy** — 每个文件的 import 模块
- **Step Mapping** — 来自 `case.yaml:steps` 的 Playwright 步骤映射
- **Locator Strategy** — 每个交互点的 locator 选择规则（role > label > text > testid > CSS）
- **Assertion Mapping** — 表格：Case ID \| Assertions；Assertion Mapping uses `Case ID | Assertions`
- **Cleanup Mapping** — 每个 Case 的清理步骤；Cleanup Mapping uses `Case ID | Cleanup | Capability`
- **Run Guidance** — 推荐给 aa-run 的执行命令和参数（此文件本身不执行测试）；Run Guidance uses `Target | Pytest Args | Markers | Environment`
- **Codegen Preconditions** — codegen 开始前必须满足的条件（含 `plan-review.json` `decision == "pass"`；`.aa/data-knowledge.yaml` 必须存在）

### m4-review-summary.md

路径：`qa/changes/<change-id>/plans/m4-review-summary.md`

必须包含：

- **Change** — change-id
- **Loaded Case Files** — 已处理的 case.yaml 路径列表
- **E2E Cases** — 总数（仅 `added` + `modified`）、plan 就绪数、codegen 就绪状态
- **Removed Cases** — `removed` 中的 case_id 列表（仅记录，不纳入 plan Scope / Test Function Mapping / Target Files）
- **Generated Plan Files** — 四个文件的路径
- **Data Knowledge Layer Status** — OK / Warning / Blocked 及 capability 解析结果
- **Data Setup Script Status** — 脚本计划状态
- **Blockers** — 阻塞 codegen 的项（或 None）
- **Needs Review** — 需要人工确认的项
- **Codegen Readiness** — `ready` / `ready_with_warnings` / `not_ready`
- **Next Step** — 指引 reviewer 运行 `aa-e2e-plan-reviewer`；codegen 需等 `plan-review.json` gate 通过

### Codegen Readiness 判定

评估 review pass 后 orchestrator 是否可进入 `aa-e2e-codegen`：

| 值 | 条件 |
|----|------|
| `ready` | 无 Blockers；Needs Review 为空或均为非阻塞项；`.aa/data-knowledge.yaml` 存在且 selected cases 所需 capability 解析 OK |
| `ready_with_warnings` | 无 Blockers；`.aa/data-knowledge.yaml` **存在**；存在非阻塞 Needs Review；或 selected cases 所需 auth / data / route / cleanup capability 已确认或 safely optional |
| `not_ready` | 存在 Blockers；**或** `.aa/data-knowledge.yaml` 缺失（默认）；**或** 缺失 data / auth / route / cleanup capability 且无法 propose 合理候选 |

**`.aa/data-knowledge.yaml` 缺失时的分层（硬规则）：**

- Plan 阶段不阻断 → 可生成 `data-knowledge.proposal.e2e.yaml`。
- **Codegen Readiness 默认为 `not_ready`**。
- **例外**：若 selected E2E cases（`added` + `modified`）**完全不需要** auth、data setup、route resolution、cleanup capability，可设为 `ready_with_warnings`，但必须在 Blockers 或 Needs Review 中注明「`.aa/data-knowledge.yaml` 缺失；codegen 前须补齐正式知识库」。
- **Reason**：`aa-e2e-codegen` 启动前均要求正式 `.aa/data-knowledge.yaml` 存在；`data-knowledge.proposal.e2e.yaml` 仅为 remediation artifact。

## Data Setup Strategy

E2E 前置数据优先通过脚本或 API 构造，不得默认通过 UI 造数。

优先级（必须按顺序评估）：

1. `tests/e2e/scripts/<module>_data_setup.py`
2. API setup script
3. isolated subprocess E2E adapter invoking the shared domain capability
4. Playwright request fixture
5. shared test fixture
6. UI setup（仅作为最后 fallback，必须写入 flaky risk）

规则：

- 前置业务数据必须尽量由脚本构造。
- 脚本必须可重复执行。
- 脚本不得使用生产账号、生产密码、真实 Token。
- 脚本必须从环境变量或测试配置读取 base URL 和凭证。
- 脚本必须输出后续 E2E 需要的实体 ID、URL、状态或 storage state。
- 脚本必须包含 cleanup 方案。
- 如果无法脚本化构造数据，必须记录 blocker 或 needs_review。
- UI 造数只能作为最后 fallback，且必须写入 flaky risk。

## Data Knowledge Layer Rules

必须尝试读取：`.aa/data-knowledge.yaml`（缺失时不阻断 plan 阶段）。

**Stage gate（plan vs codegen）：**

| Stage | `.aa/data-knowledge.yaml` 缺失时 |
|-------|-----------------------------------|
| **aa-e2e-plan** | 不阻断 planning；生成 `plans/data-knowledge.proposal.e2e.yaml`；**Codegen Readiness 默认为 `not_ready`**（见 Codegen Readiness 判定中的例外） |
| **aa-e2e-codegen** | **BLOCKER**；`data-knowledge.proposal.e2e.yaml` 仅供参考，不可替代正式知识库 |

最小建议结构：

```yaml
capabilities:
  setup_scripts:
    receivable_order_setup:
      kind: script
      symbol: tests/e2e/scripts/<module>_data_setup.py
      entity: order
      state: pending_receive
      returns:
        id_attr: order_id
        url_attr: order_url
      verify:
        operation: getOrderById
        asserts:
          - expr: "$.status"
            equals: pending_receive
      cleanup_ref: cleanup.order_delete

  auth:
    e2e_storage_state:
      kind: playwright_storage_state
      acquire:
        operation: login
      output: .auth/e2e-user.json

  cleanup:
    order_delete:
      operation: deleteOrderTestFactory
      retry_safe: true
```

规则：

- 不得自由发明 setup script、fixture、auth、cleanup。
- 如果 capability 缺失，必须记录为 blocker 或 needs_review。
- 如果 capability 存在但 verify 不完整，必须记录为 needs_review。
- Stage 1 只做静态校验，不执行真实造数。
- 不得将 data knowledge 模板或 proposal 直接写入 `.aa/data-knowledge.yaml`。
- `data-knowledge.proposal.e2e.yaml` 不得被 codegen 当作 `.aa/data-knowledge.yaml` 的替代品。

## Hard Rules

- Always generate E2E plans before codegen.
- This Skill must not generate Playwright test code.
- This Skill must not generate execution results.
- Always read `qa/changes/<change-id>/cases/**/case.yaml`.
- Only process E2E cases under `added` and `modified`.
- Do NOT generate E2E plans for cases under `removed`. `removed` entries are read only for context and archive / trace validation.
- Only process E2E cases with `automation.required = true`.
- Only P0 / P1 E2E cases are planned by default; P2 / P3 enter Needs Review.
- Always generate e2e-plan.md.
- Always generate e2e-test-data-plan.md.
- Always generate e2e-codegen-plan.md.
- Always generate m4-review-summary.md.
- Never generate POM by default.
- Never write locators into case.yaml.
- Never use UI setup as first choice for test data.
- Prefer script-based setup for E2E preconditions.
- Never invent setup scripts, fixtures, auth helpers, or cleanup helpers.
- Missing data capability must be recorded as blocker or needs_review.
- If `.aa/data-knowledge.yaml` is missing, **Codegen Readiness defaults to `not_ready`** unless selected E2E cases need no auth / data setup / route resolution / cleanup capability (see Codegen Readiness 判定).
- Test code generation requires `plan-review.json` `decision == "pass"`. User approval may be input to `aa-e2e-plan-reviewer`, but is not itself a gate artifact.

## Stop Conditions

必须在生成以下四个文件后立即停止：

- `e2e-plan.md`
- `e2e-test-data-plan.md`
- `e2e-codegen-plan.md`
- `m4-review-summary.md`

停止前必须先更新 `workflow-state.yaml`：

- 设置 `phases.e2e_plan.status = done`
- 在 `phases.e2e_plan.outputs` 下列出四个输出文件路径

停止后必须输出：

```
M4 Stage 1 completed. Next: aa-e2e-plan-reviewer.
Only after plan-review.json has decision == "pass" and codegen_readiness in ["ready", "ready_with_warnings"] may the orchestrator invoke aa-e2e-codegen.
User verbal approval cannot substitute plan-review.json.
```

不得继续生成：

- `/tests/e2e/test_*.py`（由 aa-e2e-codegen 生成，不是 plan 阶段）
- `/tests/e2e/*.spec.ts`（不使用 TypeScript Playwright）
- `/tests/e2e/adapters/*.py`
- `/tests/helpers/*`
- `/tests/e2e/pages/*`
- `qa/changes/<change-id>/scripts/*`
- `qa/changes/<change-id>/execution/*`

## Anti-patterns

禁止：

- "这个 E2E 很简单，直接生成 Playwright"
- 未生成 plan 就生成 `/tests/e2e`
- 默认通过 UI 点击来造复杂业务数据
- 自动猜测 route 并不标记 review
- 自由发明 setup script 或 auth helper
- 在 plan 阶段执行 Playwright
- 在 plan 阶段写 execution result
- 生成 POM
- 将 locator 写回 case.yaml
- 将 `removed` 下的 E2E case 纳入 Scope 或 Test Function Mapping
- 因 `.aa/data-knowledge.yaml` 缺失而 STOP planning（应生成 proposal 并继续）

## Next Workflow

**Next workflow:** `aa-e2e-plan-reviewer`

**Codegen gate:** Only after review / `plan-review.json` has `decision == "pass"`, `codegen_readiness in ["ready", "ready_with_warnings"]`, and `.aa/data-knowledge.yaml` exists, the orchestrator may invoke `aa-e2e-codegen`.

流程：
1. 将 plan 文件交由 `aa-e2e-plan-reviewer` 生成 `plan-review.json`。
2. 只有当 `plan-review.json` 满足 codegen gate 条件（含 `.aa/data-knowledge.yaml` 存在），orchestrator 才可以进入 `aa-e2e-codegen`。
3. 用户的自然语言确认（"看起来不错"、"继续"）**不能替代** `plan-review.json` gate；用户 approval 可作为 `aa-e2e-plan-reviewer` 的输入，但不是 gate artifact 本身。
