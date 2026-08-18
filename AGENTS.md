# AGENTS.md

## Cursor Cloud specific instructions

This repo is a single Python 3.11 CLI project (`aa`, the "assurance-agent") managed by
[`uv`](https://docs.astral.sh/uv/). There is no long-running service, database, web server, or
frontend to start — the "application" is the `aa` CLI. Standard dev/lint/test/build commands live in
`README.md` ("开发与测试") and `.github/workflows/ci.yml`; use those as the source of truth.

Non-obvious notes:

- `uv` is not part of the base image. The startup update script installs it (to `~/.local/bin`) and
  runs `uv sync --dev`. Interactive shells pick it up via the `. "$HOME/.local/bin/env"` line the
  installer added to `~/.bashrc`. If `uv` is ever missing in a shell, run
  `export PATH="$HOME/.local/bin:$PATH"`.
- `uv sync` provisions its own pinned CPython 3.11 (from `.python-version`); do not rely on the
  system `python3` (which is 3.12).
- Run everything through `uv run ...` (e.g. `uv run aa --version`, `uv run pytest -v`,
  `uv run ruff check .`, `uv run pyright`, `uv run lint-imports`).
- The full CI gate is: `ruff check .`, `ruff format --check .`, `pyright`, `lint-imports` (import
  layering contracts in `.importlinter`), `pytest`, and `bash scripts/packaging_smoke_test.sh`.
- To exercise the CLI end-to-end without an OpenCode server, use the deterministic scheduler on the
  bundled example: `cp -R examples/minimal-sut /tmp/aa-demo && cd /tmp/aa-demo && uv run --project
  <repo> aa init --yes && uv run --project <repo> aa status --change CH-DEMO-001 --next --json`.
  `aa doctor` reporting `warning` for missing `frontend`/`backend`/`playwright` is expected in a bare
  demo SUT and exits 0.
- Full workflow driving (`aa workflow run ... --adapter opencode`) needs an external OpenCode agent
  server (default `http://127.0.0.1:4096`); it is not required for building, testing, or the
  deterministic scheduler.

## 自定义编排不是插件平台

YAML 只换「图怎么走」。不要把技能、操作、提交前校验、门控函数做成项目外可加载插件。扩能力改 Python；组织配置放被测项目的 `.aa/`。

**项目可以替换的（内部编排文件，不是对外扩展接口）**

- 流程图：打包的 `schemas/workflow-schema.yaml`，可被项目 `.aa/workflow-schema.yaml` 或 `schemas/workflow-schema.yaml` 整份替换。
- 执行合同：打包的 `schemas/execution-contracts.yaml`，同样可被项目 `.aa/` 或 `schemas/` 下同名文件整份替换（给**已有**技能节点、操作节点改读写范围）。

这两份用来编译、恢复、重放。运行中改图会产生新编排摘要，旧调用缺省拒绝普通续跑。

**不要从项目外加载、也不要做成 YAML 插件的**

- 操作节点（`operation:`）：登记在 `workflow/driver/operations_catalog.py`，确定性副作用、账本、写集冻结都在引擎里。
- 提交前校验（如计划机械候选 `/v1`）：闭集在 `workflow/graph/precommit.py`。合同里写一个新校验器名字，运行仍会缺省拒绝。
- 门控内置函数（能力是否存在、计划评审怎么走）：白名单在 `workflow/orchestration/dsl.py`。开口等于任意代码执行。
- 新产物形状：只有 `artifacts/registry.py` 与对应模型。校验命令和门控字段不认登记表外的结构。
- 摄入产物目录 `schemas/ingest-artifact-catalog.yaml`：跟着上面的模型走，不是项目清单。

合同里声明一个新的 `skill:` / `operation:` 名字，编译也许过，没有对应处理函数仍会缺省拒绝。这是对的。

**技能**

现有 `aa-*` 技能的提示词可以在项目 `skills/` 里改。新技能要进冻结、门控、角色表，还得有执行合同、产物模型和（通常）提交前校验。文档里「新增技能零引擎代码」只对「通用智能体节点、复用已有产物类型」成立，对自定义保障能力不成立。

**打包 `schemas/` 不必整目录产品化**

| 文件 | 怎么对待 |
|---|---|
| 工作流描述、执行合同 | 允许项目整份替换；不当插件市场 |
| 默认策略 `policy-default.yaml` | 不要做项目替换。组织策略是项目 `.aa/policy.yaml` |
| 摄入产物目录 | 引擎内核 |
| 探索建议 / 探索上下文的 JSON 样例 | 给非 Python 消费者的参考；运行时以产物模型为准 |

被测项目要调的是 `.aa/config.yaml`、`.aa/policy.yaml`、`.aa/data-knowledge.yaml`，不是再复制一份包内清单。

**若要「自定义保障产品」**

另发一版引擎（加 Python 模块、扩闭集），不要让被测项目加载外部技能、操作或校验器。恢复、写集冻结、源码核验都不得被项目代码绕开。

一句话：YAML 换图；Python 扩能力；`.aa/` 放组织配置。
