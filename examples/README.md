# assurance-agent 最小示例

`minimal-sut/` 是一个极小的被测项目（SUT）骨架，预置了一个自主模式 intake 种子的变更 `CH-DEMO-001`（只含 `.qa.yaml` + `proposal.md`）。它演示：初始化一个 QA 项目，然后对一个已存在的变更计算确定性工作流状态。

## 走查

假设已安装 `aa`（见根 README「安装」）。把示例拷到一个可写目录再操作（`aa init` 会写文件）：

```bash
cp -R examples/minimal-sut /tmp/aa-demo
cd /tmp/aa-demo

# 1) 初始化：生成 .aa/ 配置、qa/tests 脚手架与 OpenCode 集成。
#    已存在的 qa/changes/CH-DEMO-001/ 种子不会被覆盖。
aa init --yes

# 2) 环境自检（frontend/backend 为占位空目录，可能是 warning，正常）。
aa doctor

# 3) 对预置变更投影确定性工作流状态（GraphStatus）。
#    种子变更尚未启动任何图，此处输出 {"status": null}。
aa status --change CH-DEMO-001 --next --json
```

`aa status --json` 输出 `GraphStatus`：`status`（`null`＝未启动）、`pending_tasks`、`pending_interrupts`、`terminal_reason` 等。退出码：`0` running/completed、`20` stopped、`30` interrupted（待人工）、`40` failed；命令或数据错误退出 `1`。

## 下一步

把 `proposal.md` 换成真实需求，接一个 OpenCode server，即可用 driver 跑完整流水线：

```bash
aa workflow run --change CH-DEMO-001 --entrypoint full --adapter opencode --server http://127.0.0.1:4096
```
