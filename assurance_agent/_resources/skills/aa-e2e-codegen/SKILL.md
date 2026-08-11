---
name: aa-e2e-codegen
description: >-
  Use only after plan-review.json has decision == "pass" and codegen_readiness in ["ready", "ready_with_warnings"]. Triggers on: "generate E2E test code from plan", "continue E2E codegen", "implement e2e-codegen-plan", "generate /tests/e2e". User request may trigger this skill but never replaces the JSON gate. Reads Stage 1 plan files and generates Python Playwright tests and script-based data setup. Does NOT execute tests — execution is handled by aa-run. Never runs before planning is complete.
---

## Per-Skill Memory

Before producing output, check whether `.aa/memory/aa-e2e-codegen.md` exists in the project root. If it exists, read it before producing output and apply only entries that are not marked `deprecated:`. Treat the file as read-only runtime guidance; do not create, edit, or delete `.aa/memory/**`.

## Test Data Architecture Contract

- Shared business-valid builders live in `tests/testdata/domain/`; E2E-owned setup/cleanup transport and pytest wrappers live in `tests/e2e/adapters/`.
- Sync Playwright code **must not import shared domain factories directly** and **must not call `asyncio.run()`** or another in-thread event-loop bridge.
- Prefer an authenticated admin/test HTTP adapter. When direct project code is unavoidable, use an **isolated subprocess** worker with bounded timeout, plain JSON input/output, checked exit status, no inherited open handles, and cleanup through the same adapter.
- Read `capabilities.domain_factories` and `capabilities.adapters.e2e`. Create a missing shared file only when marked `create-if-missing`; otherwise reuse it without modification.

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/workflow-state.yaml`.
2. Verify `phases.e2e_plan_review.status == pass` (gate must be cleared) — but **never rely on workflow-state alone**. The JSON gate is the source of truth.
3. Read input files from disk: `plans/e2e-plan.md`, `plans/e2e-test-data-plan.md`, `plans/e2e-codegen-plan.md`, `plans/m4-review-summary.md`, `review/plan-review.json`, `.aa/data-knowledge.yaml`, `cases/**/case.yaml`.
4. Verify `review/plan-review.json` gate fields in order — **STOP** on first failure:
   - file exists and is valid JSON
   - `review_type == "e2e-plan"`
   - `change_id == <change-id>`
   - `decision == "pass"`
   - `codegen_readiness in ["ready", "ready_with_warnings"]`
   - `blockers` is empty
   - no `needs_review` item has `blocking == true`
   - `.aa/data-knowledge.yaml` exists on disk
5. Use files as the sole source of truth.

**After completing work:**

1. Write generated test files per `e2e-codegen-plan.md` Target Files and **Generated File Policy**:
   - `tests/e2e/test_<module>_e2e.py` — required when mapped in plan
   - `tests/e2e/scripts/<module>_data_setup.py` — only when plan + data-knowledge authorize new script
   - `tests/e2e/adapters/**/*.py` — only when plan + data-knowledge authorize E2E setup/cleanup adapters
   - `tests/e2e/conftest.py` — only if plan explicitly requires it (see **conftest.py Policy**)
   - `qa/changes/<change-id>/codegen/e2e-codegen-summary.md` (create `codegen/` directory if missing)
2. Report the `workflow-state.yaml` state delta (inline mode: apply it directly; dispatched subagent: never write `workflow-state.yaml` — report the values in your final message and the orchestrator applies them):
   - `phases.e2e_codegen.status = done`
   - `phases.e2e_codegen.review_gate_file = review/plan-review.json`
   - `phases.e2e_codegen.codegen_readiness` = value from review JSON
   - `phases.e2e_codegen.generated_tests.files` = list of generated files
   - `phases.e2e_codegen.data_setup.scripts` = newly generated script paths
   - `phases.e2e_codegen.data_setup.reused` = reused script/capability paths
   - `phases.e2e_codegen.fixtures.generated` = newly generated fixture paths
   - `phases.e2e_codegen.fixtures.reused` = reused fixture paths
   - `phases.e2e_codegen.warnings_carried` = warning IDs or summaries from review JSON
   - `phases.e2e_codegen.workaround_annotations.present` = true|false (explicit test-code workaround comments/xfails emitted for Observation collection)

Example `workflow-state.yaml` fragment (as applied by the state owner):

```yaml
phases:
  e2e_codegen:
    status: done
    review_gate_file: review/plan-review.json
    codegen_readiness: ready_with_warnings
    generated_tests:
      files:
        - tests/e2e/test_<module>_e2e.py
    data_setup:
      scripts:
        - tests/e2e/scripts/<module>_data_setup.py
      reused: []
    fixtures:
      generated: []
      reused: []
    warnings_carried:
      - PLAN-FINDING-001
    workaround_annotations:
      present: true
```

---

# E2E Codegen for QA

名称：E2E 测试代码生成

描述："QA 超能力：仅在 E2E plan 已 Review 后使用。根据 e2e-plan.md、e2e-test-data-plan.md 和 e2e-codegen-plan.md 生成 Python Playwright 测试（test_*_e2e.py）、脚本化前置数据。**不执行 pytest，不生成执行结果** — 测试执行由 aa-run（Phase 8）负责。此 Skill 不生成新的 Case，不重新设计测试范围。"

**E2E Framework: Python Playwright。文件命名 `tests/e2e/test_<module>_e2e.py`。执行命令 `uv run pytest tests/e2e/ -v --headed`。不使用 TypeScript Playwright（`*.spec.ts` + `npx playwright test`）。**

将已 Review 的 E2E 测试计划转化为可执行 Python Playwright 测试代码。

此 Skill 是 AA M4 的 Stage 2。它必须读取 Stage 1 生成的 plan 文件，并基于 `e2e-codegen-plan.md` Target Files 生成测试代码和脚本化前置数据。**不执行 pytest，不产生 execution 结果** — 执行由 Phase 8 `aa-run` 负责。

## When to Use

当用户明确要求：

- 根据 E2E plan 生成测试代码
- 继续 E2E codegen
- 实现 e2e-codegen-plan.md
- 根据 M4 plan 生成 `/tests/e2e`

使用此 Skill。

**不适用于：执行测试（使用 aa-run）、查看执行结果（使用 aa-inspect）。**

## Do Not Use When

以下情况不得使用此 Skill：

- `e2e-plan.md` 尚未生成
- `e2e-test-data-plan.md` 尚未生成
- `e2e-codegen-plan.md` 尚未生成
- `m4-review-summary.md` 尚未生成
- **Standalone mode:** 用户尚未明确要求继续 E2E codegen
- **Orchestrated mode:** `plan-review.json` gate 尚未通过（见 Mandatory Review JSON Gate）
- 用户要求重新设计测试范围
- 用户要求生成 API-only 测试代码
- E2E plan 仍有 blocker

## Inputs

必须读取：

- `qa/changes/<change-id>/plans/e2e-plan.md`
- `qa/changes/<change-id>/plans/e2e-test-data-plan.md`
- `qa/changes/<change-id>/plans/e2e-codegen-plan.md`
- `qa/changes/<change-id>/plans/m4-review-summary.md`
- `qa/changes/<change-id>/cases/**/case.yaml`
- `.aa/data-knowledge.yaml`  ← **必须存在。缺失是 BLOCKER。**
- `qa/changes/<change-id>/review/plan-review.json`

Optional (required when `codegen_readiness == "ready_with_warnings"` due to E2E workaround / coverage gap):

- Review JSON must acknowledge any workaround / coverage gap before codegen if such scope is in the plan

> **data-knowledge 分层规则：**
> - `aa-e2e-plan` 阶段：`.aa/data-knowledge.yaml` 缺失不阻断 planning；生成 `data-knowledge.proposal.yaml`。
> - `aa-e2e-codegen` 阶段：`.aa/data-knowledge.yaml` **必须存在**。`data-knowledge.proposal.yaml` 仅供参考，不可替代。
> - 如果 `.aa/data-knowledge.yaml` 不存在，**STOP**——告知用户需先创建或 promote `data-knowledge.proposal.yaml`。

如果缺少任何 plan 文件，必须停止，并提示先运行 `aa-e2e-plan`。

## Mandatory Review JSON Gate

Same checks as Context Contract step 4. In summary:

- `qa/changes/<change-id>/review/plan-review.json` must exist and be valid JSON
- `review_type == "e2e-plan"` and `change_id == <change-id>`
- `decision == "pass"`
- `codegen_readiness in ["ready", "ready_with_warnings"]`
- `blockers` is empty
- no blocking `needs_review` items
- `.aa/data-knowledge.yaml` exists on disk

If any condition fails, **STOP** — do not generate code.

用户在对话中说 "approved" / "looks good" 不能替代此 JSON gate。如果用户口头批准，必须由 `aa-e2e-plan-reviewer` 写出 `plan-review.json` 后才能继续。

### ready_with_warnings — Handling Rules

If `codegen_readiness == "ready_with_warnings"`:

- Read `findings` and `needs_review` from `plan-review.json`.
- Continue **only** when all warnings are non-blocking and codegen does **not** need to guess:
  - route
  - selector
  - auth / storageState
  - setup script
  - fixture
  - expected UI copy
  - cleanup behavior
  - product semantics
- If any warning requires guessing → **STOP**.
- Carry warning IDs into generated code comments only when helpful, and into `codegen/e2e-codegen-summary.md`.

This aligns with `aa-e2e-plan-reviewer` **Ready With Warnings Boundary**.

If `codegen_readiness == "ready_with_warnings"` due to E2E workaround / coverage gap:

- Review JSON **MUST** already acknowledge the coverage gap (reviewer pass prerequisite)
- Codegen **MUST NOT** create canonical Issue/Problem lifecycle records or legacy known-product issue files
- See **Known Product Issue / Workaround Policy**

## Outputs

可以生成（路径以项目根为基准；以 `e2e-codegen-plan.md` Target Files 为准）：

- `tests/e2e/test_<module>_e2e.py` — test functions (required when mapped)
- `tests/e2e/scripts/<module>_data_setup.py` — **only when** plan + data-knowledge authorize new setup script
- `tests/e2e/adapters/**/*.py` — **only when** plan + data-knowledge authorize E2E setup/cleanup adapters
- `tests/e2e/conftest.py` — **only if** plan explicitly requires conftest changes (see below)
- `qa/changes/<change-id>/codegen/e2e-codegen-summary.md` — always
- `qa/changes/<change-id>/codegen/e2e-generated-files.json` — always; machine-readable generated-file evidence

### Test Data Strategy

**pytest `fixture` vs factory pattern — do not confuse them:**

- **pytest `fixture`** is the injection mechanism. E2E-owned fixtures live with their transport in `tests/e2e/adapters/`.
- **factory pattern** refers to how data is generated *inside* a fixture: return a callable factory rather than a static dict.

Rules:
- When authorized to generate a new fixture, implement it as a factory (callable fixture pattern): each test receives an independent data state.
- Do not hard-code user credentials, entity IDs, or environment-specific URLs in fixture bodies.
- Derive all data capabilities from `.aa/data-knowledge.yaml`.
- Shared mutable state between Playwright tests causes flaky results — always set up and tear down per test.

```python
# Preferred: factory-as-fixture for E2E data setup
# NOTE: Adapt `entity_factory`, `/api/entities`, and field names
#       to the actual module per data-knowledge.yaml and the plan.
@pytest.fixture
def entity_factory(api_client):
    created = []

    def _create(**overrides):
        payload = {"name": "E2E Test Entity", **overrides}
        r = api_client.post("/api/entities", json=payload)
        r.raise_for_status()
        entity = r.json()
        created.append(entity["id"])
        return entity

    yield _create

    for entity_id in created:
        api_client.delete(f"/api/entities/{entity_id}")
```

**Workaround annotations:** Codegen may emit explicit workaround comments, `xfail` markers, or alternate-flow calls in generated tests when review JSON acknowledges the gap. The Observation collector records these during `inspect-with-issues`. Codegen **MUST NOT** create legacy known-product issue files.

不生成 execution 结果文件 — 测试执行由 `aa-run` (Phase 8) 负责。

不得生成：

- `/tests/e2e/*.spec.ts`（不使用 TypeScript Playwright）
- `/tests/e2e/adapters/*.ts`
- `/tests/helpers/*`
- `/tests/e2e/pages/*`
- POM / Page Object Model
- Test files with naming other than `tests/e2e/test_<module>_e2e.py` unless `e2e-codegen-plan.md` Target Files explicitly specifies another path

不得生成或修改：

- `proposal.md`
- `case.yaml`
- `e2e-plan.md`
- `e2e-test-data-plan.md`
- `e2e-codegen-plan.md`

除非用户明确要求修订 plan。

## Generated File Policy

- Only create or modify files listed in `e2e-codegen-plan.md` **Target Files**.
- If a required output file is not listed in Target Files, **STOP** and report codegen precondition failure.
- If a target test file already exists, **append** new test functions only — do not overwrite unrelated tests.
- Do not delete existing tests.
- Preserve existing imports, fixtures, and project style.
- Do not generate empty fixture, script, or conftest files "just in case".

## conftest.py Policy

Update `tests/e2e/conftest.py` **only if** `e2e-codegen-plan.md` explicitly requires it.

- Prefer existing pytest fixture discovery and existing conftest hierarchy.
- If `tests/e2e/conftest.py` exists:
  - append only the minimal fixture/import required by the plan
  - never rewrite existing content
  - never duplicate existing fixtures/imports
- If not required by the plan, do **not** create or modify `conftest.py`.

## Workflow

1. 读取 `e2e-plan.md`。
2. 读取 `e2e-test-data-plan.md`。
3. 读取 `e2e-codegen-plan.md`。
4. 读取 `m4-review-summary.md`。
5. 读取 `case.yaml`。
6. 读取 `.aa/data-knowledge.yaml`。
7. 检查 Codegen Preconditions（见 Output Contract 中的 Codegen Preconditions）。
8. 检查 Mandatory Review JSON Gate（Context Contract step 4 + **ready_with_warnings — Handling Rules**）。
9. 校验 route、natural steps、selector strategy（含 Source / Confidence）、assertion、fixture、auth、cleanup、data setup script 映射完整性。
10. 根据 `e2e-codegen-plan.md` **Target Files** 生成或追加 `tests/e2e/test_<module>_e2e.py`。
11. 仅当 plan + data-knowledge 授权且 Target Files 列出时，生成 `tests/e2e/scripts/<module>_data_setup.py`；若 capability 已存在，复用现有脚本/fixture，不新建文件。
12. 仅当 plan + data-knowledge 授权且 Target Files 列出时，生成 `tests/e2e/adapters/**/*.py`。
12b. 仅当 plan 明确要求时，按 **conftest.py Policy** 更新 `tests/e2e/conftest.py`。
13. **不执行 pytest** — 测试执行由 `aa-run` 负责（Phase 8）。
14. 创建 `qa/changes/<change-id>/codegen/`（若不存在），写入 `codegen/e2e-codegen-summary.md`。
15. 输出 E2E Codegen Summary 摘要（文件列表 + 下一步提示）。

## Checklist

必须按顺序完成：

- [ ] 确认触发条件（standalone: 用户明确要求；orchestrated: JSON gate 已通过）
- [ ] 读取 `e2e-plan.md`
- [ ] 读取 `e2e-test-data-plan.md`
- [ ] 读取 `e2e-codegen-plan.md`
- [ ] 读取 `m4-review-summary.md`
- [ ] 读取 `case.yaml`
- [ ] 读取 `.aa/data-knowledge.yaml`
- [ ] 检查 Codegen Preconditions
- [ ] 检查 Mandatory Review JSON Gate（完整 gate + ready_with_warnings 规则）
- [ ] 校验 route、selector（Source / Confidence）、data setup capability
- [ ] 生成或追加 `tests/e2e/test_<module>_e2e.py`（仅 Target Files 授权）
- [ ] 生成 `tests/e2e/scripts/<module>_data_setup.py`（仅 plan + data-knowledge 授权）
- [ ] 生成 `tests/e2e/adapters/**/*.py`（仅 plan + data-knowledge 授权）
- [ ] 更新 `tests/e2e/conftest.py`（仅 plan 明确要求时）
- [ ] 更新 `workflow-state.yaml`（`phases.e2e_codegen` 含 review_gate_file、codegen_readiness、data_setup、fixtures、warnings_carried、known_product_issues）
- [ ] 创建 `codegen/` 目录（若不存在），写入 `e2e-codegen-summary.md`
- [ ] 所有测试文件定稿后写入 `e2e-generated-files.json`
- [ ] **机械命名校验（强制，需留证据）：** 运行 `uv run pytest --collect-only -q <生成的测试文件>`，确认收集到的每个测试名都匹配 `test_<case_id 小写>__<description>` 且覆盖 `Case → Test Function Mapping` 全部 case_id（无视大小写）。将 collect-only 输出粘贴到 `e2e-codegen-summary.md` 的 **Traceability Verification** 段落。若 plan 的 Test Function Mapping 本身缺 case_id 前缀，**不得照抄** —— 本 skill 的命名规则优先于 plan；改名并在 summary 中注明修正。
- [ ] 输出 E2E Codegen Summary（文件列表 + 下一步）

## Output Contract

### `codegen/e2e-generated-files.json`

此机器可读证据为必需输出。必须在测试文件定稿后写入；只声明文件身份与追溯元数据，runtime 会排序条目并在 freeze 前根据隔离 task workspace 的精确 bytes 补齐 digest。不要调用 shell、browser、MCP 或其他 skill 计算 hash，也不要列出 manifest 或 summary 自身。

```json
{
  "schema_version": "1",
  "change_id": "<change-id>",
  "layer": "e2e",
  "files": [
    {
      "repo_path": "tests/e2e/test_<module>_e2e.py",
      "disposition": "generated | modified | reused",
      "role": "test_entry | support",
      "case_ids": ["<case-id>"]
    }
  ]
}
```

列出 codegen plan 中每个实际生成、修改或明确复用的 E2E/support 文件。`change_id`、`layer` 必须匹配当前任务；`repo_path` 使用项目相对路径。`content_sha256` 归 runtime 所有：旧格式即使携带该字段，runtime 也会在 freeze 前忽略并按隔离 workspace 中的文件重新计算。

### tests/e2e/test_<module>_e2e.py

必须满足：

- 使用 Python Playwright（`playwright` + `pytest-playwright`）。
- **测试函数名必须以归一化 case_id 为前缀**，并用**双下划线** `__` 与描述分隔：`test_<case_id 小写>__<description>`。归一化 = 把 case.yaml 的 case_id 转小写（不含连字符）。例如 case_id `TC_ROLE_E2E_001` → `def test_tc_role_e2e_001__admin_create_edit_flow(page):`。这是**唯一强制的可追溯标记**，不依赖注释/docstring；`aa run` 的结果解析器据此从函数名回填 case_id（匹配无视大小写）。一个 case 拆成多个函数时，各函数共用同一 case_id 前缀。
- 每个断言必须来自 `case.yaml` 或 `e2e-codegen-plan.md`，不得凭空添加。
- 默认不生成 POM。
- 默认不生成 `/tests/e2e/pages/*`。
- 不得硬编码生产账号、密码、Token。
- 不得使用固定 `time.sleep()` 作为默认等待策略；必须使用 Playwright 条件等待（`expect(locator).to_be_visible()`、`page.wait_for_*`）。
- locator 选择优先级：role > label > text > testid > CSS fallback。
- **Locator generation rule:** Do not generate a locator unless the locator strategy is confirmed in `e2e-codegen-plan.md` with **Source** and **Confidence**. If key locator confidence is `unknown` or requires guessing → **STOP**. Do not invent `data-testid`, button text, label text, or CSS selector.
- API setup / script setup 必须在 UI 操作前完成。
- 前置数据 verify 失败时，不得继续 UI 步骤，必须 fail fast。
- E2E 只覆盖用户路径，不把造数流程扩展成 UI 点击流程。

### tests/e2e/conftest.py

Apply **conftest.py Policy** above.

必须满足：

- 仅追加新 fixture，不覆盖已有 fixture。
- 可封装 `browser_context`、`page`、`base_url`、登录 storageState、测试数据读取。
- fixture 来源必须映射到 `.aa/data-knowledge.yaml`。
- fixture 不得改变断言语义。
- fixture 不得隐藏业务失败。
- fixture 不得硬编码真实账号、密码、Token。

### tests/e2e/adapters/**/*.py

Generate **only when**:

- `e2e-codegen-plan.md` Target Files lists it
- `e2e-codegen-plan.md` defines fixture mapping
- `.aa/data-knowledge.yaml` explicitly authorizes new fixture wrapper/factory

If existing fixture already satisfies the need, **reuse it** — do not generate a new fixture file.

Do not invent fixture names, factories, auth helpers, or cleanup helpers.

必须满足：

- fixture 来源必须映射到 `.aa/data-knowledge.yaml` 中已定义的 capability。
- 不得自由发明业务状态或新建 business data factory，除非 data-knowledge 明确定义该 capability。
- 不得硬编码真实账号、密码、Token。
- 必须保留 cleanup 入口或说明 cleanup 由现有 fixture 负责。

### tests/e2e/scripts/<module>_data_setup.py

Generate **only when**:

- `e2e-codegen-plan.md` Target Files lists it, and
- `.aa/data-knowledge.yaml` explicitly authorizes the setup capability or wrapper, and
- `e2e-test-data-plan.md` requires a new script rather than reusing an existing script/fixture

If required capability already exists, **reuse it** instead of generating a new script.

Do not create new business data factories or setup endpoints unless `.aa/data-knowledge.yaml` defines the capability.

必须满足：

- 用于构造 E2E 前置业务数据。
- 优先通过 E2E HTTP adapter 构造数据；必须调用共享 domain factory 时，只能由有超时和 JSON I/O 的 isolated subprocess adapter 执行。
- 不得通过 UI 点击构造复杂业务数据，除非 plan 已明确批准。
- 必须可重复执行。
- 必须从环境变量或测试配置读取 baseURL、账号、密码、token。
- 必须输出 E2E test 需要的实体 ID、URL、状态。
- 必须包含 runtime verify。
- 必须包含 cleanup 或 cleanup reference。
- 不得使用生产环境地址、生产账号或真实 Token。

### Codegen Output Summary

codegen 完成后必须写入（create `qa/changes/<change-id>/codegen/` if it does not exist）：

```text
qa/changes/<change-id>/codegen/e2e-codegen-summary.md
```

必须包含：

- **Generated Files** — 实际生成或修改的文件列表（仅列出真实写入的文件；optional 文件若未生成则标注 N/A）
- **Case → Test Function Mapping** — 表格：Case ID \| Test Function \| File
- **Traceability Verification** — 粘贴 `pytest --collect-only -q` 输出，证明每个收集到的测试名都带 `test_<case_id 小写>__` 前缀（强制；缺少此段落的 summary 视为不完整）
- **Data Setup Scripts Generated/Reused** — 脚本路径及来源
- **Fixtures Generated/Reused** — fixture 名称和来源（或 "Reused existing — no new file"）
- **Conftest Changes** — 变更摘要（如无则 None）
- **Warnings Carried from Review** — 来自 `plan-review.json` 的警告（如 `codegen_readiness == "ready_with_warnings"`）
- **Workaround annotations** — 列出测试代码中为 Observation 采集显式标注的 workaround
- **Next Step** — `aa run --change <change-id>`

同时在聊天中输出摘要，但磁盘文件是 aa-run、workflow UI 的唯一可信来源，不依赖聊天上下文。

### Codegen Preconditions

进入 codegen 前必须满足全部条件：

- `e2e-plan.md` 已存在且 Blockers 为空
- `e2e-test-data-plan.md` 已存在
- `e2e-codegen-plan.md` 已存在且 Target Files 非空
- Mandatory Review JSON Gate 全部通过（见上文章节）— **不接受**用 `m4-review-summary.md` 或对话批准替代
- `.aa/data-knowledge.yaml` 已存在，且所有 required capability 状态为 found
- Route Mapping 无 blocking needs_review 项；关键 route 有 Source 且 Confidence ≠ unknown
- Selector Strategy 无 blocking needs_review 项；关键 locator 有 Source 且 Confidence ≠ unknown
- Data Setup Script Plan 无 blocking needs_review 项

如果任何条件未满足，**STOP and report** — do not ask for chat confirmation to bypass the gate.

## Data Setup Strategy

E2E 前置数据必须优先通过脚本构造。

推荐实现顺序：

1. `tests/e2e/adapters/<module>.py` authenticated admin/test HTTP transport
2. `tests/e2e/adapters/<module>_worker.py` isolated subprocess transport when no safe HTTP setup exists
3. Playwright request fixture wrapping the E2E adapter
4. UI setup（仅作为最后 fallback，必须写入 flaky risk）

脚本生成规则：

- 仅当 `e2e-codegen-plan.md` Target Files 列出且 data-knowledge 授权时才生成新脚本。
- 如果项目已有 data setup script 或 capability，优先复用。
- 如果缺少 setup capability，停止 codegen，不得继续。
- 如果脚本无法运行，E2E 执行必须 skipped，不得继续 UI 步骤。
- 如果 verify 失败，E2E 执行必须 failed 或 skipped，并写明原因。
- cleanup 必须在脚本或已有 conftest/fixture 中明确。

## Known Product Issue / Workaround Policy

Do not silently change tests to avoid product bugs or skip flows.

**Prerequisites (all required before codegen with workaround):**

- `plan-review.json` `decision == "pass"`
- `codegen_readiness == "ready_with_warnings"`
- Review JSON **already acknowledges** the coverage gap
- The issue was acknowledged by `aa-e2e-plan-reviewer` (not deferred to codegen)

**Codegen MUST NOT** create legacy known-product issue files. If workaround is required but review JSON lacks acknowledgment → **STOP**.

**During codegen, the skill must:**

1. Add an explicit test docstring referencing the known issue ID (e.g. `KPI-001`) and explaining the workaround.
2. Add a local comment in the test function body explaining why the alternate flow is used.
3. **May emit** explicit workaround annotations in generated test code tied to an acknowledged review finding.
4. Mark skipped or worked-around flow as a coverage gap in test comments — never claim direct E2E coverage for a skipped or worked-around flow.
5. Do **not** set execution status — that is `aa-run`'s responsibility.

## Execution

此 Skill 不执行测试。测试执行由 `aa-run` 负责（Phase 8）。

参考执行命令（由 aa-run 调用）：`uv run pytest tests/e2e/ -v --headed`

## Hard Rules

- This Skill must only run after E2E planning is complete.
- Do not generate or modify Case YAML.
- Do not generate or modify `proposal.md`.
- Do not redesign test scope.
- Do not change assertions unless the plan explicitly says so.
- Do not generate API-only tests.
- Do not generate POM.
- Do not generate `/tests/e2e/pages/*`.
- Do not generate `/tests/helpers/*`.
- Do not generate `*.spec.ts` files (TypeScript Playwright is not used in this project).
- Do not generate test files except `tests/e2e/test_<module>_e2e.py` unless `e2e-codegen-plan.md` Target Files explicitly specifies another path.
- Do not hardcode real credentials, tokens, secrets, or environment-specific URLs.
- Do not invent setup scripts, fixtures, auth helpers, or cleanup helpers.
- Do not bypass `e2e-codegen-plan.md` Target Files.
- Do not execute pytest. Test execution belongs to aa-run (Phase 8).
- Do not write any execution result files (e2e-result.json, summary.md, etc.).
- If data setup cannot run, do not continue UI steps.
- Do not create legacy known-product issue files — coverage-gap acknowledgment belongs to human + plan reviewer before pass.
- Do not generate locators without confirmed Source and Confidence in `e2e-codegen-plan.md`.

### Test Failure Integrity

Generated tests MUST fail for the right reason.

Rules:
- Every assertion MUST map to a case assertion or plan assertion.
- Do not use `assert True`, placeholder assertions, or empty expectations.
- Do not swallow failures with empty `try/except` or `except Exception: pass`.
- Do not add fallback logic that hides product failures.
- Do not use `skip`, `xfail`, `pytest.mark.skip`, or `test.fixme` to make generated tests green.
  - **Only exception for `xfail`:** a known product issue explicitly decided at intake/case-design AND acknowledged in review JSON; the xfail `reason` MUST reference the review finding ID. An xfail without documented acknowledgment is a violation.
- Do not use conditional early `return` to bypass assertions (e.g. `if "/404" in page.url: return`) — assert the expected denial/behavior explicitly instead.
- Do not loosen expected values after observing failures.
- Do not mock the behavior under test unless the plan explicitly authorizes it.
- Do not fall back to a generic locator if the primary locator fails — report the failure instead.
- Setup may be flexible; assertions must be strict.

Self-check before finishing codegen:
1. Would this test fail if the product behavior is wrong?
2. Does every assertion trace back to the case or plan?
3. Are setup failures reported instead of hidden?

## Stop Conditions

**STOP and report** to orchestrator/user. In orchestrated mode, do **not** ask for chat confirmation to bypass the gate.

必须停止并报告（不得靠用户一句「继续」绕过）：

- 缺少 `e2e-plan.md`
- 缺少 `e2e-test-data-plan.md`
- 缺少 `e2e-codegen-plan.md`
- 缺少 `m4-review-summary.md`
- 缺少 `.aa/data-knowledge.yaml`
- `plan-review.json` 缺失、非法 JSON、`review_type` / `change_id` 不匹配、`decision != pass`、或 `codegen_readiness == not_ready`
- `blockers` 非空或存在 `blocking == true` 的 `needs_review` 项
- Codegen Preconditions 未满足
- Required output file 不在 `e2e-codegen-plan.md` Target Files 中
- Data setup capability 缺失
- Route / selector 仍处于 blocking needs_review 或 Confidence = unknown
- Data setup script plan 仍处于 blocking needs_review
- Workaround required but review JSON lacks coverage-gap acknowledgment
- `ready_with_warnings` 但任何 warning 需要 guessing route / selector / auth / setup / fixture / UI copy / cleanup / product semantics
- **Standalone mode:** 用户未明确要求继续 codegen

## Anti-patterns

禁止：

- "Plan 文件不完整，但我先生成测试"
- "我帮你补一个 setup script 名"
- "先用 UI 点一遍把数据造出来"
- "先写固定 time.sleep() 等页面"
- "Workaround 写了，但没关联已确认的 review finding"
- "codegen 首次创建 legacy known-product issue 文件来承认 coverage gap"
- "生成空 fixture / script / conftest 文件"
- "未经 plan 授权就改 conftest.py"
- "未经 Source / Confidence 确认就发明 locator"
- "顺手修改 case.yaml"
- "顺手补充断言"
- "把 E2E 测试扩展成 API 测试"
- "生成 Page Object Model"
- "生成 *.spec.ts 文件"
- "用 test_<module>.py 代替 test_<module>_e2e.py（除非 Target Files 明确指定）"

## Next Workflow

完成后，下一步是：

**Phase 8 — 运行 `aa-run`** 执行测试：

```bash
aa run --change <change-id>
```

如果测试失败，由 `aa-inspect` 分析原因，不得在此 skill 内自动修改断言或合并修复。
