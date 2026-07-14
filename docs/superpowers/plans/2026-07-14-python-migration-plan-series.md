# Python 迁移计划系列总览

Spec: `docs/superpowers/specs/2026-07-14-python-migration-design.md`

Spec 覆盖 9 个相互依赖但可独立交付的里程碑，每个里程碑一份独立实施计划（每份计划完成后都产出可运行、可测试的软件）。计划按依赖序执行，前序计划的产物（模块、函数签名）是后序计划的输入。

| # | 计划 | 交付物 | Spec 章节 | 状态 |
|---|---|---|---|---|
| 1 | `2026-07-14-m1-scaffolding.md` | pyproject + 包骨架 + 资源分发 + `aa init/doctor/config` | 2, 15 | 已写 |
| 2 | m2-artifacts | `artifacts/` 全部 pydantic 模型 + 路径注册表 + `aa validate` | 4a | 待写 |
| 3 | m3-orchestration | schema 加载 + DSL 解释器 + DAG 引擎 + gate + state/events | 3, 4 | 待写 |
| 4 | m4-status-commands | `aa status/gate/state/decide` + risk（Explore） | 5 | 待写 |
| 5 | m5-execution-report | 执行层 4 runner + `aa run` + report/inspect + `aa heal` | 6, 7 | 待写 |
| 6 | m6-driver | `aa workflow run`：主循环、双 adapter、detached、lock、resume | 5a | 待写 |
| 7 | m7-skills-opencode | 33 个 skill 改写 + `.opencode/` 集成 + `aa skill refresh` | 8, 9 | 待写 |
| 8 | m8-eval-retro | eval 框架 + retro 模块 | 10 | 待写 |
| 9 | m9-docs-examples | README 重写 + examples + 打包冒烟测试进 CI | 15 | 待写 |

约定：

- 每份计划完成并验收后，再写下一份计划（后序计划需要引用前序实际落地的接口签名，提前写会失真）。
- 全局约束（工具链、命名、分层）见各计划头部 Global Constraints，源头是 spec 第 1、2 节。
- TS 源仓库 `/Users/lvqingquan/skills/assurance-workflow-skills` 仅作规则参考（spec 决策表），计划中引用它时只提取行为规则，不复制实现。
