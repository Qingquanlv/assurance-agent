# AI Eval Harness (aa)

对 Assurance Agent（**aa**）进行**阶段化、可复现**的质量评估。执行逻辑在 `assurance_agent/eval/`；配置、fixture 与数据在 `eval/`。

---

## 背景

### 要解决什么问题

- **Skills / Workflow 是非确定性的**：同一份 PRD，LLM 每次产出的 case、测试代码可能不同。
- **需要分层评测**：Case Design（E0）→ API Codegen（E2a）→ E2E/Fuzz/Perf Codegen（E2b/E2c/E2d）→ Test Run（E3）→ Full Workflow（E4）各阶段独立 gate。
- **需要可审计证据链**：每次 run 落盘 `stdout.log`、`execution.json`、`raw-output/`、`metrics.json`，便于 CI 与人工复核。

### 设计原则

| 原则 | 说明 |
|------|------|
| **Suite = 测什么 + 怎么跑 + 怎么判** | `eval/suites/*.yaml` 定义 executor、thresholds |
| **Sample = 单条输入** | `eval/datasets/<suite>/*.yaml` 提供 `input` / `expected` |
| **Fixture = 种子数据** | `eval/fixtures/` 在 run 前 seed 到 SUT，不污染 git |
| **PR smoke 用 fake adapter** | CI 验证 harness 通路；真实 LLM 质量在本地 / nightly 评 |
| **Hard / Advisory / Observe** | 见 `eval/contracts/p0-metrics.yaml` |

### Schema v2 / fixture import

- Suite executor 使用 `entrypoint: case|full|execute`（不再使用 `scope`）。
- Mid-graph 回放通过 tier `imports` → `.graph-runtime/import-manifest.yaml` → `import_checkpoint`；
  裸 `workflow-state.yaml` phase 标记不是权威。
- GraphRuntime 真并行 + 资源序列化；retry 与业务 budget 分计；interrupt/resume。

### 阶段与 Suite 对照

| 阶段 | Suite | 样本（模块） | 测什么 |
|------|-------|--------------|--------|
| **E0** | `workflow-case` | WC-001~004 | Case Design + Review（case-only） |
| **E2a** | `workflow-api-codegen` | WAC-001~004 | API Codegen（codegen-only） |
| **E2b** | `workflow-e2e-codegen` | WEEC-001~004 | E2E Codegen |
| **E2c** | `workflow-fuzz-codegen` | WFUZ-001~004 | Fuzz Codegen |
| **E2d** | `workflow-performance-codegen` | WPER-001~004 | Performance Codegen |
| **E3** | `workflow-run` | WR-001~016 | `aa run` 测试执行 |
| **E4** | `workflow-full` | WF-001~004 | 全流程（nightly，observe-only） |

---

## 目录结构

```text
eval/
├── contracts/     # 指标注册表、证据规范
├── suites/        # Suite 定义
├── datasets/      # 样本 YAML
├── fixtures/      # Golden seed
├── baselines/     # 指标基准线（eval compare 用）
├── out/
│   ├── runs/      # 单次 run 产物（gitignore）
│   └── reports/   # trend HTML
└── plans/         # Batch 计划 JSON
```

Python 版 run 产物路径：`eval/out/runs/<run_id>/`（与 TS 版 `eval/runs/` 等价，统一在 `out/` 下）。

---

## 前置条件

```bash
uv sync
```

Workflow 类 suite 还需要：

- 外部 SUT checkout（`eval/suts.yaml` 或 `AA_EVAL_SUT_DIR`）
- 真实 LLM run：本机 agent 可用；**不要**设 `AA_EVAL_FAKE_ADAPTER=1`

---

## CLI 入口

```bash
aa eval <subcommand> ...
aa retro [--since|--change|--retro-id] [--json]
aa retro nightly collect|resume|report ...
```

---

## 环境变量（aa 重命名）

| TS / 旧名 | Python / aa |
|-----------|-------------|
| `EVAL_USE_FAKE_OPENCODE=1` | `AA_EVAL_FAKE_ADAPTER=1` |
| `EVAL_SUT_DIR` | `AA_EVAL_SUT_DIR` |
| `EVAL_JUDGE_API_URL` | `AA_JUDGE_API_URL` |
| `EVAL_JUDGE_API_KEY` | `AA_JUDGE_API_KEY` |
| `EVAL_JUDGE_MOCK` | `AA_JUDGE_MOCK` |
| — | `AA_EVAL_AGENT_CMD`（默认 `cursor-agent`，headless adapter） |
| — | `AA_JUDGE_MODEL`（judge 模型名） |

---

## 命令详解：`aa eval run`

```bash
aa eval run \
  [--suite <name> | --plan <path>] \
  [--sample <id>] \
  [--repeat <n>] \
  [--calibrate] \
  [--output id] \
  [--json] \
  [--fail-on-verdict] \
  [--sut-dir <dir>]
```

| 参数 | 含义 |
|------|------|
| `--suite` / `--plan` | 互斥，至少一个 |
| `--sample` | 只跑单条样本 |
| `--fail-on-verdict` | verdict 为 fail/inconclusive/needs_human_review 时 exit 1 |
| `--json` | 输出 `{ run_id, verdict }` |
| `--output id` | 只打印 run_id |
| `--sut-dir` | 覆盖 SUT 根目录 |

CI smoke 示例：

```bash
AA_EVAL_FAKE_ADAPTER=1 aa eval run --suite workflow-case --sample WC-001 --fail-on-verdict
```

Executor 复用 M6 `run_workflow_loop`（`skip_lock=True`），不另起循环；fake 模式注入桩 adapter + 立即 terminal 的 status provider。

---

## 其它 eval 子命令

### `aa eval gate`

```bash
aa eval gate --run <run-id>
```

只读 `gate-result.json`，**不重新跑**。退出码：`pass`/`pass_with_warnings` → 0；`fail`/`inconclusive` → 1；`needs_human_review` → 30。

> **deferred**：`aa eval gate --batch <batch-id>`（批次 gate）未迁移。

### `aa eval report`

```bash
aa eval report --run <run-id> [--json|--html]
aa eval report --trend --suite workflow-case [--from ISO] [--to ISO] [--html] [--output path]
```

### `aa eval compare`

```bash
aa eval compare --baseline main --run <run-id>
```

读取 `eval/baselines/main.json`，对双方共有的 metric 输出 `run - baseline` delta。

> **deferred**：`aa eval compare --batch <id>` 未迁移。

### `aa eval baseline update`

```bash
aa eval baseline update --suite workflow-case --run <run-id> --approved-by XX [--yes]
```

交互确认后写入 `eval/baselines/main.json`；不参与 gate。

### `aa eval plan`

```bash
aa eval plan --event pull_request --changed-files changed.txt --out eval-plan.json
aa eval plan --event manual --suite workflow-case --out eval-plan.json
```

---

## Run 产物路径

```text
eval/out/runs/<run_id>/
├── manifest.json
├── metrics.json
├── gate-result.json
├── report.json / report.html / report.md
└── samples/<sample-id>/attempt-0/
    ├── stdout.log / stderr.log / execution.json
    └── raw-output/       # 拷贝的 change 产物
```

---

## 指标与 Gate

Gate 三档：

| 档 | 行为 |
|----|------|
| **hard** | 不达标 → suite **fail** |
| **advisory** | 不达标 → **pass_with_warnings** |
| **observe** | 只写入 metrics，**不阻断** |

### E0 — `workflow-case`

| 指标 | Gate | 阈值 |
|------|------|------|
| `schema_valid_rate` | hard | ≥ 0.99 |
| `layer_scan_valid_rate` | hard | == 1.0 |
| `case_review_gate_pass_rate` | hard | ≥ 0.99 |
| `secret_leak_count` | hard | == 0 |
| `forbidden_write_executed_count` | hard | == 0 |
| `evidence_integrity` | hard | pass |

### E2a–E2d / E3 / E4

指标表与 TS 版 `docs/eval.md` 一致（见 `eval/contracts/p0-metrics.yaml`）。Python scorer 已实现 E0/E2a-d/E3/E4 核心 hard/advisory 指标。

### OpenCode 过程可观测性（7 项 observe）

> **deferred（M8）**：Python 版未迁移 OpenCode NDJSON 事件解析器；scorer **不产出** `process_observability_*` / `tool_*` / `write_bypass_*` 等指标；报告**不展示** Process Observability 区块。不得将缺失解读为「无过程问题」。

### Judge 校准

`--calibrate` 会调用 judge 并将结果写入 sample notes；**deferred**：不会据此更新 gate 阈值或 baseline 数值。

---

## Retro Loop

### 手动路径（`aa retro`）

```bash
aa retro --since 2026-07-01T00:00:00Z --json
aa retro --retro-id retro-x --change CH-1 --change CH-2 --json
```

stdout JSON 含 `retro_id`、`change_count`、`signal_count`（benchmark nightly 分支消费）。

写入 `qa/retro/<retro-id>/context.json`。`context.json` **顶层含 `signal_count`**（= `count_signals(context)`，TS 版无此字段，Python 有意增补）。

`--since` 与 `--change` 互斥。目录已有 `promotions.json` 时不可再写 context（immutable）。

### Nightly 驱动（`aa retro nightly`）

```bash
# PHASE A–D
aa retro nightly collect \
  --sut /path/to/sut \
  [--retro-id <id>] [--dry-run] [--agent cursor-agent] \
  [--history 5] [--min-evidence 2] [--rework-alert 3]

# PHASE E–F（resume）
aa retro nightly resume --sut /path/to/sut --retro-id <id> [--skip-eval]

# 跨 run 汇总
aa retro nightly report --sut /path/to/sut [--last 10]
```

| 阶段 | 做什么 |
|------|--------|
| **A** | 枚举未消费 / unarchived 且 terminal 的 change；必要时 snapshot 证据 |
| **B** | 进程内直调 `build_retro_context` 写 `context.json`；`signal_count=0` → exit 10 |
| **C** | 调 `--agent` 生成 `proposals.json`；schema 校验剔除非法提案 |
| **D** | 分流提案；写 `review-queue.md`；`complete_retro_stage` |
| **E** | resume：对已 promote 的 `memory_append` stage apply |
| **F** | eval baseline/candidate 回归；有 hard_gates 且未回归才 auto-apply |

**退出码**

| 码 | 含义 |
|----|------|
| 0 | 成功 |
| 10 | no-op（无候选 / 零信号 / 校验后无提案） |
| 30 | 仍待人工审阅（resume，`skip_eval` 或缺 eval runner） |
| 40 | 基础设施 / agent 失败 |

产物（SUT 的 `qa/retro/`）：

```text
qa/retro/
├── _state.json
├── cross-run-report.json
└── <retro-id>/
    ├── context.json      # 含顶层 signal_count
    ├── proposals.json
    ├── promotions.json
    ├── review-queue.md
    └── retro-summary.md
```

---

## 与 TS 版差异（Python M8 落地）

1. **CLI**：`node dist/cli.js eval` → `aa eval`；`aws retro` → `aa retro`；`aws run` → `aa run`。
2. **Nightly phase B**：进程内直调聚合，**不** shell out `aa retro`。
3. **`context.json`**：顶层新增 `signal_count`（benchmark 依赖）。
4. **Judge**：经 `httpx` 直连 Anthropic 兼容端点；`AA_JUDGE_MOCK` 走确定性 stub。
5. **Run 根目录**：`eval/out/runs/`（非 `eval/runs/`）。
6. **deferred**：OpenCode 过程可观测性 7 项；`eval gate|compare --batch`；judge 校准写基准。

---

## 延伸阅读

- `eval/contracts/p0-metrics.yaml` — 指标注册表
- `assurance_agent/eval/scorers/` — Python scorer 实现
- `assurance_agent/retro/` — retro 聚合与 nightly 驱动
