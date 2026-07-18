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
| 黄金样本来源 | 双源、内容寻址捕获:已归档的 `RET-api-management-20260716-192358-cursor`提供 change 产物、执行证据与 tests/product tree 哈希;SUT 工作树仅在逐文件哈希与 archive manifest 完全匹配时提供生成的 API/E2E 测试文件 |
| nightly 触发 | benchmark loop 自动做 `collect`并停在人工审批;`aa retro promote --decision promoted` 记录审批后立即调用真实 `eval_runner`,基础设施失败可用 `aa retro nightly resume` 幂等重试;`aa eval` 同时可手动跑 |
| 实施方法 | 方案 A:四个里程碑垂直切片,每个独立可验收;种子机制不逐行翻译 TS,用 Pydantic + 现有 state 原语重写 |

## 2. 目录布局(混合模式)

```
assurance-agent/                        # 引擎仓库
├── eval/                               # 版本化的"引擎契约"数据(新增)
│   ├── suites/*.yaml                   # 11 个 suite 定义(scorer / 阈值 / executor 配置)
│   ├── datasets/<suite>/*.yaml         # 样本指针(id / change_id / fixture_id / fixture_tier / tags)
│   ├── baselines/main.json             # 人工批准的指标基线
│   └── suts.yaml                       # SUT 注册表(pinned rev + local_dir)
└── benchmark/vue-fastapi-admin/        # SUT
    ├── eval-fixtures/                  # 黄金样本(与 SUT 同处,版本化,新增)
    │   ├── fixture-lock.yaml            # archive id / source tree hashes / fixture digest
    │   ├── samples/eval-sample-001/    # 从 api-management 归档捕获的冻结快照
    │   └── tiers/L0~L3-*.yaml          # 种子分层 manifest
    └── eval/out/                       # 运行产物(gitignore,新增)
        └── runs/<run_id>/              # manifest.json / report.json / samples/<id>/attempt-N/
```

布局原则:

- **引擎契约跟引擎走**:suite 阈值、样本 schema、baseline 与引擎代码同仓同版本,契约变更同一 PR 更新;
- **黄金样本跟 SUT 走**:fixture 内容(测试代码、change 产物)天然 SUT 特定,放 SUT 侧,`suts.yaml` 用 tests/product tree 哈希钉住有效性;
- **产物写 SUT 根**:`eval/out/` 统一落 SUT 根,顺带修复 retro 读趋势的路径错位——`read_eval_trend` 现有实现零改动即可吃到数据。

archive id 单独记为 `source_archive_id`,不再冒充 revision。当前同仓 benchmark SUT 未独立版本化,因此用 archive manifest 已有的 `tests_tree_sha256`、`test_files_sha256` 和 `product_tree_sha256` 作为可执行的 pinned 契约;`pinned_rev` 仅对将来可 checkout 的外部 SUT 可选启用。`fixture-lock.yaml` 记录上述源哈希、每个捕获文件的 SHA-256 和整体 `fixture_digest`;`fixture_digest` 是对 `samples/` 与 `tiers/` 下按相对路径排序的 `{path: sha256}` 映射做 canonical JSON SHA-256,不包含 lock 文件自身,避免循环哈希。加载 fixture 时校验 digest,任一文件不匹配则硬失败。

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

每个 dataset sample 必须同时声明:

```yaml
id: WR-001
suite: workflow-run
input:
  change_id: RET-api-management-eval-001
  fixture_id: eval-sample-001
  fixture_tier: L3-run-seed
```

`fixture_id` 只允许安全的单路径段,对应 `eval-fixtures/samples/<fixture_id>/`;不允许由 `change_id` 或 sample `id` 隐式推导,避免 dataset 命名与 fixture 布局耦合。

### seed_change 函数

`seed_change(sut_sandbox: Path, change_id: str, fixture_id: str, tier: str, fixtures_root: Path) -> None`:

1. 清空沙箱内 `qa/changes/<change_id>/`;
2. 先校验 `fixture-lock.yaml`,再按展开后的 tier `paths` 从 `eval-fixtures/samples/<fixture_id>/` 拷入;
3. 按 `resets.workflow_state` 重置 phase 状态;
4. 按 `resets.qa_yaml` 重置 `.qa.yaml` 字段。

**硬约束(与审计层的唯一交叉点)**:第 3 步必须走 `read_state` / `write_state` 原语重算 `_integrity.state_sha256`。直接改 YAML 文件会被 2026-07-16 上线的读侧审计判为 `STATE-INTEGRITY-TAMPERED`,导致 eval 运行自带审计告警。此约束必须有专门测试覆盖(种子后 `aa status` 无 audit issue)。

### executor 接入

`execute_attempt` 在跑 loop 前:样本带 `input.fixture_tier` 则必须同时带 `input.fixture_id`,并用两者调用 `seed_change`;不带 `fixture_tier` 则保持现行为(向后兼容,现有单测不受影响)。种子发生在 `_copy_attempt_workspace` 生成的沙箱内,真实 SUT 全程只读。

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

- 双源:`qa/archive/RET-api-management-20260716-192358-cursor/` 供 change 产物、执行证据与源哈帏;`suts.yaml.local_dir` 指向的 SUT 工作树供 `tests/` 下的生成测试文件;
- 捕获前读 archive `execution/execution-manifest.yaml`,硬校验其 `final_status=PASS`、`tests_tree_sha256=c27e78e057f7603ffa0c74852ffaf114486ca121cdc6a4977458c25e84996439`、`product_tree_sha256=037862330f93db0461429c747c97082f2450dd120565b7c8f3e75588abef9637`,并对 `test_files_sha256` 列出的每个 SUT 文件重算哈希;缺文件、symlink、非普通文件或任一哈希不符均拒绝捕获;
- `product_tree_sha256` 用现有 tree-hash 原语按 manifest 的同一排除规则重算;不要求整个工作树 clean,但凡纳入 tests/product tree 的内容必须与 manifest 完全一致;
- 按阶段截断生成:L0(case 种子)→ L1(plan 完成)→ L2(codegen 完成)→ L3(执行就绪)四层 tier + `samples/eval-sample-001/` 快照;
- 写入 `fixture-lock.yaml` 后重读并校验整体 digest;捕获后人工审定,冻结入库;重新捕获仅发生在引擎契约升级或 SUT pinned rev 变更时,走 review。

### suts.yaml

```yaml
suts:
  vue-fastapi-admin:
    local_dir: "benchmark/vue-fastapi-admin"   # 同仓,相对引擎根
    source_archive_id: "RET-api-management-20260716-192358-cursor"
    tests_tree_sha256: "c27e78e057f7603ffa0c74852ffaf114486ca121cdc6a4977458c25e84996439"
    product_tree_sha256: "037862330f93db0461429c747c97082f2450dd120565b7c8f3e75588abef9637"
    pinned_rev: null                  # 仅外部、可 checkout SUT 使用
```

eval 启动时重算并校验 tests/product tree 哈希;不一致则拒绝运行并提示恢复 SUT 快照或重新捕获 fixture。若未来设置 `pinned_rev`,还必须同时校验该 revision 可 checkout 且与当前 SUT revision 一致。

## 5. nightly 接线(M3)

- `retro_cmd` 提供唯一的 `_build_eval_runner(data_root, sut_root)` 工厂;`promote` 与 `nightly resume` 都用它构造真实 `eval_runner`(闭包内调 `run_suite`),避免两条入口分叉;
- `resume_nightly` 的 `skip_eval or eval_runner is None → PENDING_REVIEW` 早退路径保留(显式 `--skip-eval` 仍可跳过);
- benchmark loop 的 `collect` 只产生 review queue,因为此时尚无人工批准提案,不调用 eval。人工执行 `aa retro promote ... --decision promoted` 时,命令写入审批事件后立即调用 `resume_nightly`;若进程中断,操作者或外部调度器用 `aa retro nightly resume --retro-id <id>` 幂等续跑。不再声称现有 benchmark 脚本会自动调用 resume;
- `read_eval_trend` 因第 2 节的输出根统一自动吃到 `report.json`,retro 上下文的 `eval_trend` 信号从恒空变为真实数据。

## 6. retro promotion 闸门(M4)

对齐原版 `resumeNightly` 完整语义:

### 审批入口与状态机

1. 新增 `aa retro promote --retro <id> --proposal <id> --decision promoted|rejected|needs_rework --decided-by <actor> [--rework-note <text>]`;
2. 命令确认 proposal 存在、仍可审批;`promoted` 只接受 `apply_kind=memory_append`、非空且可加载的 `eval_suite`、位于 `.aa/memory/` 下的安全 `target`;
3. `promotions.json` 改为追加式事件流,通过临时文件 + `os.replace` 原子写入。事件至少包含 `proposal_id`、`type`、`at`、`actor`与对应证据:

```json
{
  "schema_version": "2",
  "events": [
    {"proposal_id": "P-1", "type": "review_decision", "decision": "promoted", "actor": "LQ", "at": "..."},
    {"proposal_id": "P-1", "type": "eval_completed", "result": "pass", "run_ids": ["eval-..."], "actor": "aa", "at": "..."},
    {"proposal_id": "P-1", "type": "application", "result": "applied", "target": ".aa/memory/aa-run.md", "content_sha256": "...", "actor": "aa", "at": "..."}
  ]
}
```

4. 法定转移为 `proposed → rejected|needs_rework|promoted_pending_eval → applied|rolled_back|awaiting_baseline|eval_error`。`applied`、`rejected` 是终态;`rolled_back` 后提案状态视为 `needs_rework`;`awaiting_baseline` 在补齐 baseline 后可由 resume 重跑,`eval_error` 在基础设施恢复后可由 resume 重试。终态提案重复操作只返回原结果,不重复 eval 或 append;
5. 写入 `promoted` 事件后,`promote` 命令立即用真实 runner 调用 `resume_nightly`;人工审批是授权点,eval 和 apply 是审批后的自动流程。

### staging 与 memory overlay

1. 新增 `aa retro apply --retro <id> --proposal <id> --stage-dir <tmp>` 作为可测的内部 interface:从真实 `.aa/memory/` 复制基础内容到 staging,再把已批准 `memory_append` 提案应用到 staging,不动真实 memory;
2. append 内容使用 `proposal_id` 标记,同一 proposal 在 staging 和真实 target 中最多出现一次;target 必须 resolve 在 `.aa/memory/` 内,拒绝绝对路径、`..`、symlink 和非普通文件;
3. `resume_nightly` 按 `eval_suite` 分组所有 `promoted_pending_eval` 提案,为每组构造独立 staging,然后调用 `run_suite(..., extra_memory_dir=stage)`;
4. `--extra-memory-dir` 从“只声明未使用的 CLI 参数”升级为真实契约:`eval_cmd` 解析后传给 `run_suite`;runner 在 `_copy_attempt_workspace` 之后、`seed_change` 之前把 staging 内的 memory 覆盖到 `attempt_sut/.aa/memory/`。staging 优先,拒绝 symlink/越界路径,不写真实 SUT;
5. run manifest 记录 `memory_overlay_sha256`,不记录主机 staging 绝对路径,便于重现与避免泄漏环境信息。

### baseline regression policy

用 suite YAML 声明基线回归策略,不从 metric 名称猜方向:

```yaml
version: "1"
regression:
  repeat: 1                       # agent suite 可设为 3
  metrics:
    completion_rate:
      direction: higher_is_better
      max_regression: 0.0
    secret_leak_count:
      direction: lower_is_better
      max_regression: 0.0
```

所有用于 promotion 的 suite 都必须先通过自身 gate,该规则不可由 suite 关闭。`RunManifest` 新增 `suite_version`、`repeat` 和 `regression_policy_sha256`;`regression_policy_sha256` 是对 suite `regression` 块 model dump 后的 canonical JSON SHA-256。`baseline update` 把三者与 metrics 一起写入 baseline entry。

判定顺序固定为:

1. baseline 不存在 → `inconclusive`,不 apply;提示用现有命令 `aa eval baseline update --suite <suite> --run <run> --approved-by <actor>` 批准首条基线;
2. candidate gate 为 `fail|inconclusive|needs_human_review`,或存在 `hard_gate_failures` → `regression`;
3. baseline 与 candidate 的 suite `version`、`repeat` 或 `regression_policy_sha256` 不同,或 policy 声明的指标在任一侧缺失 → `inconclusive`,不 apply;
4. 对每个 policy metric 计算方向化退化量;`higher_is_better` 为 `baseline-current`,`lower_is_better` 为 `current-baseline`。任一退化量超过 `max_regression` → `regression`,否则 `pass`;
5. `repeat` 传给 `run_suite`,使用当前 aggregate metrics 与 baseline 比较;
6. `pass` → 用与 staging 相同的幂等 append 逻辑落地真实 memory,写 `application: applied`;`regression` → 删除 staging,写 `eval_completed: regression` + `application: rolled_back` 并附 run id、gate 和 metric delta;`inconclusive` → 删除 staging,写 `eval_completed: inconclusive` 并派生 `awaiting_baseline`,保持真实 memory 不变。

## 7. 里程碑与验收标准

| 里程碑 | 内容 | 验收 |
|---|---|---|
| M1 地基 | fixtures.py 种子机制;paths.py 双根拆分;suts.yaml;workflow-run suite(aws-run 型)打通 | tmp 目录 fake-SUT(合成 fixture)种子→跑→评分→gate 单测全绿;dataset 缺 `fixture_id` 或 lock digest 不符时硬失败;种子后 `aa status` 无 audit issue |
| M2 数据全迁 | 黄金样本双源捕获(L0~L3);11 个 suite + datasets 迁移;in_process/subprocess executor;4 个新 scorer | capture 在 archive/SUT 任一文件或 tree digest 不匹配时拒绝,且 lock digest 可重现;所有 suite 可加载、scorer 注册表全覆盖;真 SUT 上 `aa eval run --suite workflow-run` 出 report.json;产出并人工批准首条 baseline |
| M3 nightly 接线 | 共用 eval_runner 工厂接入 promote/resume;趋势信号点亮 | 集成测试先写入 promoted 事件再调 resume,断言真实 runner 被调用;`--skip-eval` 仍返回 pending review;`read_eval_trend` 非空;retro context.json 含 eval_trend |
| M4 promotion 闸门 | aa retro promote/apply;memory overlay;regression policy;staging→eval→对比→落地/回滚 | 集成测试覆盖"未批准不跑 eval"、"批准后通过闸门且只落地一次"、"提案回归被回滚"、"baseline/指标/version 缺失时 inconclusive"、"overlay 仅写 attempt 沙箱";promotions.json 事件可完整重放 |

每个里程碑 TDD:先写失败测试,再实现;里程碑测试全绿才进入下一个。

## 8. 错误处理与风险

- 种子失败(tier 不存在、样本文件缺失)→ `AaError` 硬失败,不产生半种子状态(先种到临时目录再原子移入);
- eval 永不写真实 SUT:agent 类 suite 在 `_copy_attempt_workspace` 沙箱内运行;`in_process` 类无文件系统副作用;
- baseline、policy metric 或 suite version 缺失 → `inconclusive` 而非 crash,且真实 memory 不变;
- candidate 产生可判定的回归 → 对应提案 `rolled_back` 并视为 `needs_rework`;调用 runner、agent 或读写产物等基础设施失败 → `eval_error`,不落地且可 resume 重试;两者都不阻塞其余 suite 分组;
- agent 类 suite 单次成本高且非确定 → 阈值从宽(完成率 / 证据完整性为主),nightly 低频;确定性回归主力靠 workflow-run(无 agent,可高频);
- 黄金样本随 SUT/引擎演进"腐烂" → 显式走"重新捕获 + review"流程,capture 脚本常备;原版 `baselines/main.json` 至今为空的教训:M2 验收即要求产出并批准首条真实 baseline,避免闸门长期空转。

## 9. 非目标

- 不迁移原 datasets/ 下无 suite 定义的 4 个遗留子目录(classification-cli-parity、codegen、failure-classification、safety);
- 不实现多 SUT 并行 eval(保留 suts.yaml 结构即可);
- 不做 judge(LLM 评审)校准数据迁移——`judge/` 相关能力保持现状,待有需求再启用;
- 不改 benchmark loop 的现有语义(它保持"真实 agent 全流程演练场"角色)。
