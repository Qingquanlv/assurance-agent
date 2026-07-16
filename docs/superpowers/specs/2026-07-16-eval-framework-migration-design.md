# eval 框架补齐设计:数据层、种子机制与 nightly 闸门

日期:2026-07-16
状态:已获用户批准(方案 A:垂直切片、里程碑推进)

## 1. 背景与目标

2026-07-14 的 Python 净室重写迁移了 `eval/` 的**代码层**(runner / executor / scorers / gate / baseline / dataset_loader / report / judge / metrics / plan,以及 `aa eval` CLI),但**数据层与机制层完全缺失**,导致 eval 框架"空转":

1. **无数据集**:原版 `eval/` 数据树(11 个 suite 定义、样本 YAML、黄金 fixture、baseline)一个文件都没迁;
2. **无种子机制**:原版 `seed_change.ts` + `fixture_utils.ts`(按 tier manifest 把 change 目录重置到钉死的中间态)没有 Python 对应物,executor 只会从 change 目录当前状态起跑,确定性基石缺失;
3. **nightly 不点火**:`resume_nightly(options, *, eval_runner=None)` 的 `eval_runner` 从未被接线,eval 分支永远走不到;原版"提案 apply→staging→eval 闸门→落地/回滚"的 promotion 安全链在 Python 版退化为无验证直接落地;
4. **路径错位**:`aa eval` 把产物写到 `Path.cwd()/eval/out/`,而 retro 的 `read_eval_trend(project_root)` 从 SUT 根读 `eval/out/runs/`,两边永远对不上。

目标:按原版语义补齐以上四项,恢复"引擎回归自动检测"与"retro 提案落地前 eval 验证"两条防线。

### 已确认的关键决策

| 决策项 | 结论 |
|---|---|
| 范围 | 完整对齐原版:最小回归环 + nightly 接线 + retro promotion 闸门全做 |
| suite 覆盖 | 尽量全迁 11 个 suite(原 datasets/ 下 15 个子目录中 4 个无 suite 定义,属遗留,不迁) |
| 目录布局 | 混合:引擎契约数据(suites/datasets/baselines/suts.yaml)放引擎仓库根 `eval/`;黄金样本与种子层放 `benchmark/vue-fastapi-admin/eval-fixtures/`;运行产物统一写 SUT 根 `eval/out/` |
| agent 执行器 | cursor-agent(复用 benchmark 已验证的 dispatch 机制);确定性 suite 用 CLI 直跑不经 agent |
| 黄金样本来源 | 从已归档的 `RET-api-management-20260716-192358-cursor`(12/12 API + 6/6 E2E 全绿)按阶段截断捕获 |
| nightly 触发 | `resume_nightly` 接真实 `eval_runner`,benchmark loop 的 retro 环节自动带出;`aa eval` 同时可手动跑 |
| 实施方法 | 方案 A:四个里程碑垂直切片,每个独立可验收;种子机制不逐行翻译 TS,用 Pydantic + 现有 state 原语重写 |

## 2. 目录布局(混合模式)

```
assurance-agent/                        # 引擎仓库
├── eval/                               # 版本化的"引擎契约"数据(新增)
│   ├── suites/*.yaml                   # 11 个 suite 定义(scorer / 阈值 / executor 配置)
│   ├── datasets/<suite>/*.yaml         # 样本指针(几行 YAML:id / input.change_id / fixture_tier / tags)
│   ├── baselines/main.json             # 人工批准的指标基线
│   └── suts.yaml                       # SUT 注册表(pinned rev + local_dir)
└── benchmark/vue-fastapi-admin/        # SUT
    ├── eval-fixtures/                  # 黄金样本(与 SUT 同处,版本化,新增)
    │   ├── samples/eval-sample-001/    # 从 api-management 归档捕获的冻结快照
    │   └── tiers/L0~L3-*.yaml          # 种子分层 manifest
    └── eval/out/                       # 运行产物(gitignore,新增)
        └── runs/<run_id>/              # manifest.json / report.json / samples/<id>/attempt-N/
```

布局原则:

- **引擎契约跟引擎走**:suite 阈值、样本 schema、baseline 与引擎代码同仓同版本,契约变更同一 PR 更新;
- **黄金样本跟 SUT 走**:fixture 内容(测试代码、change 产物)天然 SUT 特定,放 SUT 侧,`suts.yaml` 用 pinned rev 钉住有效性;
- **产物写 SUT 根**:`eval/out/` 统一落 SUT 根,顺带修复 retro 读趋势的路径错位——`read_eval_trend` 现有实现零改动即可吃到数据。

`paths.py` 相应拆分为两个根:**数据根**(引擎仓库:suites/datasets/baselines)与**输出根**(SUT:out)。`aa eval` 的 `project_root = Path.cwd()` 语义改为"引擎数据根",输出路径全部改挂 SUT 根。

## 3. 种子机制(M1 核心)

新模块 `assurance_agent/eval/fixtures.py`:

### TierManifest(Pydantic 模型)

```yaml
# eval-fixtures/tiers/L3-run-seed.yaml 示意(语义对齐原版,非逐行翻译)
name: L3-run-seed
extends: L2-api-codegen-seed        # 继承链:L3 → L2 → L1 → L0
description: Run eval seed — API tests generated, ready for execution
paths:                              # 相对黄金样本根的文件清单(累加继承)
  - "tests/api/test_users_api.py"
resets:                             # 种子后对 workflow-state 的显式重置
  workflow_state:
    phases.api_codegen.status: done
    phases.execution.status: pending
  qa_yaml:
    test_types: [api]
```

- `extends` 链在加载时展开:`paths` 累加,`resets` 深合并(子层覆盖父层);
- `tests/` 前缀的路径拷到 SUT 测试目录,其余拷到 change 目录(对齐原版 `splitTestPaths` 语义)。

### seed_change 函数

`seed_change(sut_sandbox: Path, change_id: str, tier: str, fixtures_root: Path) -> None`:

1. 清空沙箱内 `qa/changes/<change_id>/`;
2. 按展开后的 tier `paths` 从 `eval-fixtures/samples/<sample>/` 拷入;
3. 按 `resets.workflow_state` 重置 phase 状态;
4. 按 `resets.qa_yaml` 重置 `.qa.yaml` 字段。

**硬约束(与审计层的唯一交叉点)**:第 3 步必须走 `read_state` / `write_state` 原语重算 `_integrity.state_sha256`。直接改 YAML 文件会被 2026-07-16 上线的读侧审计判为 `STATE-INTEGRITY-TAMPERED`,导致 eval 运行自带审计告警。此约束必须有专门测试覆盖(种子后 `aa status` 无 audit issue)。

### executor 接入

`execute_attempt` 在跑 loop 前:样本带 `input.fixture_tier` 则先 `seed_change`;不带则保持现行为(向后兼容,现有单测不受影响)。种子发生在 `_copy_attempt_workspace` 生成的沙箱内,真实 SUT 全程只读。

## 4. suite / executor / scorer 全迁(M2)

### executor 类型映射

| 原版类型 | suite | Python 落地 |
|---|---|---|
| `aws-run` | workflow-run(样本数以本次捕获为准;原版 16 个) | **CLI 直跑,不用 agent**,复用 `CliPhaseExecutor`;确定性引擎回归主力,可高频 |
| `workflow-run` | workflow-full、workflow-case、4 个 codegen 族 | cursor-agent,复用 benchmark 已验证的 dispatch 机制;非确定,nightly 低频 |
| `in_process` | _test、classification-unit、safety-lite | 新增轻量分支:直接调用 Python 函数评测,无沙箱无 agent |
| `subprocess` | case-generation | cursor-agent 单发调用 |

executor 分发按 suite YAML 的 `executor.type` 路由;`in_process` 不走 `_copy_attempt_workspace`(无 SUT 依赖)。

### scorer 缺口

现有注册表已覆盖 7 个(workflow-run / case / full + 4 codegen 族)。需新增 4 个:`classification_unit`、`safety_lite`、`case_generation`、`_test`(自测桩)。指标语义对照原版 TS scorer(原项目 `src/eval/scorers/`)迁移,以原项目 `eval/contracts/metric-spec.md` 为规格(仅作参考读物,不迁入本仓库)。

### 黄金样本捕获

一次性捕获脚本(`scripts/capture_eval_fixture.py`,产物进版本库,脚本保留供未来重新捕获):

- 源:`qa/archive/RET-api-management-20260716-192358-cursor/`;
- 按阶段截断生成:L0(case 种子)→ L1(plan 完成)→ L2(codegen 完成)→ L3(执行就绪)四层 tier + `samples/eval-sample-001/` 快照;
- 捕获后人工审定,冻结入库;重新捕获仅发生在引擎契约升级或 SUT pinned rev 变更时,走 review。

### suts.yaml

```yaml
suts:
  vue-fastapi-admin:
    local_dir: "benchmark/vue-fastapi-admin"   # 同仓,相对引擎根
    pinned_rev: "RET-api-management-20260716-192358-cursor"  # 捕获脚本写入:黄金样本的归档源 id
```

同仓场景下 pinned_rev 弱化为记录性字段;保留结构为将来外部 SUT 留路。

## 5. nightly 接线(M3)

- `retro_cmd` 的 `resume` 子命令构造真实 `eval_runner`(闭包内调 `run_suite`),传入 `resume_nightly`;
- `resume_nightly` 的 `skip_eval or eval_runner is None → PENDING_REVIEW` 早退路径保留(显式 `--skip-eval` 仍可跳过);
- `run-workflow-loop-cursor.sh` 的 retro 环节零改动自动获得 eval;
- `read_eval_trend` 因第 2 节的输出根统一自动吃到 `report.json`,retro 上下文的 `eval_trend` 信号从恒空变为真实数据。

## 6. retro promotion 闸门(M4)

对齐原版 `resumeNightly` 完整语义:

1. 新增 `aa retro apply --retro <id> --proposal <id> --stage-dir <tmp>`:把已批准(promoted)的 `memory_append` 提案应用到 staging 目录(不动真实 memory);
2. `resume_nightly` 按 suite 分组已批准提案 → 逐组 staging → 用 `aa eval run --extra-memory-dir <stage>`(现有参数,即为此预留的钩子)跑候选 eval;
3. 与 `eval/baselines/main.json` 对比:通过 → 提案落地并写 `promotions.json` applied 记录;回归 → 回滚 staging,写 `promotions.json` rolled_back 记录含证据;
4. baseline 缺失 → 判 `inconclusive`,要求人工批准首条基线(`aa eval baseline approve`),不硬失败。

## 7. 里程碑与验收标准

| 里程碑 | 内容 | 验收 |
|---|---|---|
| M1 地基 | fixtures.py 种子机制;paths.py 双根拆分;suts.yaml;workflow-run suite(aws-run 型)打通 | tmp 目录 fake-SUT(合成 fixture)种子→跑→评分→gate 单测全绿;种子后 `aa status` 无 audit issue |
| M2 数据全迁 | 黄金样本捕获(L0~L3);11 个 suite + datasets 迁移;in_process/subprocess executor;4 个新 scorer | 所有 suite 可加载、scorer 注册表全覆盖;真 SUT 上 `aa eval run --suite workflow-run` 出 report.json;产出并人工批准首条 baseline |
| M3 nightly 接线 | eval_runner 接入 resume_nightly;趋势信号点亮 | nightly collect→propose→resume 全程跑通;`read_eval_trend` 非空;retro context.json 含 eval_trend |
| M4 promotion 闸门 | aa retro apply;staging→eval→对比→落地/回滚 | 集成测试覆盖"提案通过闸门落地"与"提案回归被回滚"两条路径;promotions.json 记录完整 |

每个里程碑 TDD:先写失败测试,再实现;里程碑测试全绿才进入下一个。

## 8. 错误处理与风险

- 种子失败(tier 不存在、样本文件缺失)→ `AaError` 硬失败,不产生半种子状态(先种到临时目录再原子移入);
- eval 永不写真实 SUT:agent 类 suite 在 `_copy_attempt_workspace` 沙箱内运行;`in_process` 类无文件系统副作用;
- baseline 缺失 → `inconclusive` 而非 crash;
- nightly 中 eval 失败 → 对应提案标记 `needs_rework`,不落地、不阻塞其余提案;
- agent 类 suite 单次成本高且非确定 → 阈值从宽(完成率 / 证据完整性为主),nightly 低频;确定性回归主力靠 workflow-run(无 agent,可高频);
- 黄金样本随 SUT/引擎演进"腐烂" → 显式走"重新捕获 + review"流程,capture 脚本常备;原版 `baselines/main.json` 至今为空的教训:M2 验收即要求产出并批准首条真实 baseline,避免闸门长期空转。

## 9. 非目标

- 不迁移原 datasets/ 下无 suite 定义的 4 个遗留子目录(classification-cli-parity、codegen、failure-classification、safety);
- 不实现多 SUT 并行 eval(保留 suts.yaml 结构即可);
- 不做 judge(LLM 评审)校准数据迁移——`judge/` 相关能力保持现状,待有需求再启用;
- 不改 benchmark loop 的现有语义(它保持"真实 agent 全流程演练场"角色)。
