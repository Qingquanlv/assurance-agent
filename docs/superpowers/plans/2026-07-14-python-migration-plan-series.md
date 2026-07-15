# Python 迁移计划系列总览

Spec: `docs/superpowers/specs/2026-07-14-python-migration-design.md`

Spec 覆盖 9 个相互依赖但可独立交付的里程碑，每个里程碑一份独立实施计划（每份计划完成后都产出可运行、可测试的软件）。计划按依赖序执行，前序计划的产物（模块、函数签名）是后序计划的输入。

| # | 计划 | 交付物 | Spec 章节 | 状态 |
|---|---|---|---|---|
| 1 | `2026-07-14-m1-scaffolding.md` | pyproject + 包骨架 + 资源分发 + `aa init/doctor/config` | 2, 15 | **已完成并入 main（PR #1）** |
| 2 | `2026-07-15-m2-artifacts.md` | `artifacts/` 全部 pydantic 模型 + 路径注册表 + `aa validate` | 4a | 已写 |
| 3 | `2026-07-15-m3-orchestration.md` | schema 加载 + DSL 解释器 + DAG 引擎 + gate + state/events | 3, 4 | 已写 |
| 4 | `2026-07-15-m4-status-commands.md` | `aa status/gate/state/decide` + risk（Explore） | 5 | 已写 |
| 5 | `2026-07-15-m5-execution-report.md` | 执行层 4 runner + `aa run` + report/inspect + `aa heal` | 6, 7 | 已写 |
| 6 | `2026-07-15-m6-driver.md` | `aa workflow run`：主循环、双 adapter、detached、lock、resume | 5a | 已写 |
| 7 | `2026-07-15-m7-skills-opencode.md` | 33 个 skill 改写 + `.opencode/` 集成 + `aa skill refresh` | 8, 9 | 已写 |
| 8 | `2026-07-15-m8-eval-retro.md` | eval 框架 + retro 模块（含 `aa retro nightly`） | 10 | 已写 |
| 9 | `2026-07-15-m9-docs-ci.md` | README 重写 + examples + CI + benchmark 脚本切换到 `aa` | 15 | 已写 |

约定：

- ~~每份计划完成并验收后，再写下一份计划~~ **（2026-07-15 变更：应用户要求，M2–M9 计划一次性生成。为控制接口失真，所有跨里程碑接口以本文档下方「接口契约」为准；实施某里程碑时若发现前序实际落地与契约不符，以落地代码为准并回改本文档与受影响计划。）**
- 全局约束（工具链、命名、分层）见各计划头部 Global Constraints，源头是 spec 第 1、2 节。
- TS 源仓库 `/Users/lvqingquan/skills/assurance-workflow-skills` 仅作规则参考（spec 决策表），计划中引用它时只提取行为规则，不复制实现。

---

## 接口契约（跨里程碑绑定）

以下签名与常量是后序计划引用前序产物的唯一依据。计划与实现都必须与本节一致；变更需先改本节。

### Import 契约（冻结，2026-07-15）

`.importlinter` 由 **M1 建立并唯一属主**。此后每个里程碑**只做两类改动**：(a) 在既有 `[importlinter:contract:layers]` 的 `layers` 列表里，把本里程碑新建的顶层包插入到**冻结层序**的正确位置；(b) 新增**独立命名**的 `forbidden` 契约。**严禁**：整文件替换、反转既有层序、改名既有 contract（这正是此前 M2/M5/M6 反复翻转、后一个里程碑破坏前一个的根因，Standards P1）。

冻结层序（顶层 import 底层；某层在其包被创建的里程碑才加入）：

```ini
[importlinter:contract:layers]
name = commands depend on domain, never the reverse
type = layers
layers =
    assurance_agent.cli
    assurance_agent.commands
    assurance_agent.eval        # M8 创建时插入
    assurance_agent.retro       # M8 创建时插入
    assurance_agent.workflow
    assurance_agent.artifacts    # M2 插入（workflow → artifacts 允许，反向禁止）
    assurance_agent.config
    assurance_agent.resources

# M3 新增（独立命名，不动 layers）：
[importlinter:contract:core-below-orchestration]
name = workflow.core must not import workflow.orchestration
type = forbidden
source_modules = assurance_agent.workflow.core
forbidden_modules = assurance_agent.workflow.orchestration

[importlinter:contract:artifacts-below-workflow]
name = artifacts must not import workflow
type = forbidden
source_modules = assurance_agent.artifacts
forbidden_modules = assurance_agent.workflow
```

方向裁定要点：`artifacts` 在 `workflow` **之下**（`workflow/core/state.py` import `artifacts.WorkflowState`、M5 execution/report import artifacts 模型）；`workflow.core` 允许 import `artifacts`，但不得 import `workflow.orchestration`。M4 的 `risk` 层、M8 的 `eval`/`retro` 层按各自里程碑在冻结位置插入，不得改动上述已冻结部分。

### M1 已落地（以代码为准）

```python
# assurance_agent/resources.py
def read_text(*relpath: str) -> str: ...
def exists(*relpath: str) -> bool: ...
def iter_children(*relpath: str) -> list[str]: ...

# assurance_agent/config.py
CONFIG_RELPATH = ".aa/config.yaml"
class AaConfig(BaseModel): ...   # extra="allow"; sources/qa/tests/frameworks/generation/execution
def load_config(root: Path) -> AaConfig  # raises ConfigNotFoundError | ConfigInvalidError

# assurance_agent/exceptions.py
class AaError(Exception): ...

# assurance_agent/workflow/core/generator.py
def generate_project(root: Path, answers: InitAnswers) -> GenerateResult
def repair_project(root: Path) -> GenerateResult

# assurance_agent/workflow/core/checks.py
def run_doctor_checks(root: Path) -> DoctorResult

# cli.py: click Group `main`; 命令用 main.add_command() 挂载
```

### M2 产物契约（artifacts/）

```python
# assurance_agent/artifacts/registry.py
Compat = Literal["must_compat", "versioned", "free"]

class ArtifactSpec(BaseModel):
    artifact_type: str            # 如 "review", "failure_analysis"
    pattern: str                  # change 相对路径 glob，如 "review/*-review.json"
    model: type[BaseModel]
    compat: Compat

REGISTRY: list[ArtifactSpec]
def match_artifact(relpath: str) -> ArtifactSpec | None

# assurance_agent/artifacts/validate.py
class ArtifactResult(BaseModel):
    path: str; artifact_type: str; ok: bool; errors: list[str]
class ValidationReport(BaseModel):
    ok: bool; results: list[ArtifactResult]
class WorkflowSchemaLike(Protocol):    # M2 落地：解耦 M3，避免循环依赖
    def phase_produces(self, phase_id: str) -> list[str] | None: ...
def validate_change(change_dir: Path, phase: str | None = None,
                    artifact: str | None = None,
                    schema: WorkflowSchemaLike | None = None) -> ValidationReport

# CLI: aa validate --change <id> [--phase <phase>] [--artifact <relpath>] [--json]
# 退出码: 0 全部通过 / 1 校验失败、缺失、显式未注册 artifact 或零注册产物 / 2 用法错误（未知 phase 等）
```

> M2 落地补充：注册表实为 13 条（对齐 TS `src/schema/index.ts`）外加 `safety_check` 增补条目；`aa validate --phase` 过滤在 M2 阶段用轻量 YAML 读取 `_resources/schemas/workflow-schema.yaml` 的 `produces` 声明，M3 的 `WorkflowSchema` 须实现 `phase_produces()` 以满足上面的 `WorkflowSchemaLike` 协议。

公共路径安全合同：`assurance_agent.identifiers.assert_path_segment_safe()` / `assert_change_id_safe()` 由 M2 建立；所有 M2+ 接受外部 `change_id`、`retro_id`、run/baseline id 并拼接文件路径的 CLI、driver、eval/retro 入口必须先调用。ID 是单一路径段：首字符字母/数字，其余仅 `[A-Za-z0-9._-]`；空串、`.`、`..`、斜杠和绝对路径一律拒绝。

核心模型（M3+ 消费，全部在 `assurance_agent/artifacts/models/`）：
`WorkflowState`、`QaYaml`、`CaseYaml`、`Review`（含 `decision`/`auto_fix_allowed`/`human_review_required`/`codegen_readiness`/`risk_level`/`findings`）、`Advisory`、`FactBaseline`、`ExecutionManifest`（含 `final_status`/`batch_id`/`selected_targets`）、`FailureAnalysis`（含 `source_batch_id`/`compat_fallback_reason`/`failures[].classification`/`failures[].fix_proposal_eligible`）、`FixProposal`（含 `summary.eligible_count`/`proposals[].target`/`proposals[].eligible`）、`QualityGateResult`、`QualityReport`、`ApplySummary`、`SafetyCheck`（fixer/inspect）。

### M3 编排核心（orchestration + core）

```python
# assurance_agent/workflow/orchestration/schema.py
class WorkflowSchema(BaseModel): ...   # phases/loops/gates/params，加载期校验全部 DSL 表达式
    def phase_produces(self, phase_id: str) -> list[str] | None: ...  # 满足 M2 WorkflowSchemaLike
    def has_phase(self, phase_id: str) -> bool: ...                    # M4 gate check / state apply 需要
    def gate_for_phase(self, phase_id: str) -> str | None: ...         # phase → 其 exit gate 名称
def load_workflow_schema(project_root: Path, explicit: Path | None = None) -> WorkflowSchema
# 解析顺序: explicit(排他) → .aa/workflow-schema.yaml → schemas/workflow-schema.yaml → 包内默认

# assurance_agent/workflow/orchestration/dsl.py
MISSING: object                        # 三值逻辑的 missing 哨兵
def parse_expression(text: str) -> Expr        # ast.parse + 白名单; 加载期报错
def evaluate(expr: Expr, scope: Scope) -> object   # True/False/MISSING/值
class Scope: ...                       # params/state/evidence 别名 + gate()/file_exists()/any()/all()/count()/len()/defined()

# assurance_agent/workflow/orchestration/engine.py
class DispatchEntry(BaseModel):
    phase_id: str; skill: str | None; agent: str | None
    kind: Literal["skill", "cli", "orchestrator"]
class Terminal(BaseModel):
    kind: Literal["completed", "stopped", "needs_human_review"]; reason: str | None = None
class PhaseView(BaseModel):
    id: str; status: str               # pruned|out_of_scope|blocked|ready|awaiting_gate|done|stopped (对齐源版)
    gate: str | None; gate_verdict: str | None; produces_present: bool
class WorkflowStatus(BaseModel):
    phases: list[PhaseView]
    next_dispatch: list[DispatchEntry]
    terminal: Terminal | None          # None = running
    healing_episode: HealingEpisodeSnapshot  # direct construction defaults to an inactive snapshot
class HealingStateSnapshot(BaseModel):
    status: str; attempts_used: int; all_fixers_no_op: bool
    episode_id: str | None = None; attempt_id: str | None = None
class HealingStateProvider(Protocol):
    def __call__(self, change_dir: Path) -> HealingStateSnapshot: ...
def derive_healing_state(change_dir: Path) -> HealingStateSnapshot
# attempts_used 的唯一事实源是当前 episode 的 healing_attempt_allocated，按 operation_id 去重；
# heal_record_apply 只记录 target apply 结果，不消耗/返还 attempt budget；持久化 state 不得覆盖该计数。
def compute_status(schema: WorkflowSchema, change_dir: Path,
                   state: WorkflowState, params: dict,
                   *, scope: str = "full",
                   healing_provider: HealingStateProvider | None = None) -> WorkflowStatus  # 纯函数(不写 state)
#   进度模型: produces 文件【存在性】+ gate 裁决驱动(忠实转录 engine.ts), 不读 state 状态串作进度
#   scope: 由 M6 从 --scope 透传; 仅 produces 未生成且不在 active scope → out_of_scope(已产出者不因 scope 死锁)
#   gate 时机: exit gate 只在 produces_present 时裁决, 绝不对未运行 phase 裁决
#   needs_fix → awaiting_gate(不终止); 其 repair_of phase 经 repair 路由变 ready；healing counter 每次由 typed ledger 覆盖
#   when/ready_when: 在 build_evidence_scope(全局反向别名 produces_alias_map, hoist_primary=False)下求值; 【无 phase.reads】
#   human decision: latest action=stop overrides terminal/dispatch; a later non-stop decision resumes normal projection

# assurance_agent/workflow/orchestration/healing_episode.py
class HealingAttemptIntent(BaseModel):
    episode_id: str; attempt_id: str; attempt_number: int; operation_id: str
    source_batch_id: str; pin_entry_baseline: bool
class HealingEpisodeAction(BaseModel):
    kind: Literal["dispatch_phase", "allocate_attempt", "await_human", "complete"]
    phase: str | None = None; allocation: HealingAttemptIntent | None = None
    outcome: str | None = None
class HealingEpisodeSnapshot(BaseModel):
    state: Literal["inactive", "active", "awaiting_human", "terminal"]
    stage: Literal["entry", "proposal", "allocate", "apply", "safety", "rerun", "reinspect", "decide"] | None
    attempt_number: int; next_actions: list[HealingEpisodeAction]
    terminal_kind: Literal["stopped"] | None = None; reason: str | None = None
def project_healing_episode(schema, change_dir, state, params,
                            healing: HealingStateSnapshot) -> HealingEpisodeSnapshot
# 纯 projection 读取 typed events；共享 produces 的 healing-rerun/healing-reinspect 是否完成，
# 由最新 healing_attempt_allocated 之后的 phase_outcome_committed 判定，不由旧文件存在性判定。
# allocate_attempt 携带完整 typed intent；M6 只执行 baseline/snapshot + strict event 写边界，不补业务字段。

# assurance_agent/workflow/orchestration/gates.py
class Verdict(StrEnum): ...            # 唯一 verdict 词汇源；schema 与 GateVerdict 共用
class GateVerdict(BaseModel):
    gate: str
    verdict: Verdict                   # 加载期拒绝未知 YAML verdict
    matched_rule: str | None = None; reason: str | None = None
# 规则按【声明顺序 first-true-wins】裁决(对齐源版); 安全序 needs_fix→human_review→reject→pass 由加载期 canonical 声明序强制(Spec §80)
def check_gate(schema: WorkflowSchema, gate_name: str, change_dir: Path,
               state: WorkflowState, params: dict) -> GateVerdict
def resolve_gate_verdict(schema, gate_name, change_dir, state, params, memo=None, stack=()) -> Verdict
def build_evidence_scope(schema, change_dir, state, params, reads, *, hoist_primary=True, memo=None, stack=()) -> Scope
#   ↑ gate 规则(hoist_primary=True) 与 phase when/ready_when(hoist_primary=False, 全局别名) 共用
def resolve_change_path(change_dir: Path, rel: str) -> Path   # 先替换 <change-id>，再解析 repo:/qa/；gate reads 与 produces 共用

# assurance_agent/workflow/core/state.py  —— WorkflowState 是唯一交换类型(非 dict); 【无 WAL/_txn】
def read_state(change_dir: Path) -> WorkflowState        # 含 state hash 校验; 缺失→WorkflowState()
def write_state(change_dir: Path, state: WorkflowState) -> None    # 原子写 + 完整性 hash
def state_guard(change_dir: Path) -> str                 # state 文件 SHA256; dispatch guard/幂等
# 冻结约定: state.phases.healing 为显式 HealingPhaseState；driver 可持久化展示态，
# 但 gate 使用的 attempts_used 每次由 typed allocation events 投影覆盖，禁止手写 state 作为事实源。

# assurance_agent/workflow/core/events.py  —— 【无 WAL】; strict 事件是 discriminated pydantic union
AuditEvent = Annotated[DispatchSigned | PhaseOutcomeCommitted | HealingAttemptAllocated | HealRecordApply | HealTransition | HumanDecision | HealingEntryBaselinePinned, Field(discriminator="type")]
def append_event_best_effort(change_dir: Path, event: Mapping[str, JsonValue]) -> None  # 捕获全部序列化/IO 异常
def append_event_strict(change_dir: Path, event: AuditEvent) -> None   # 先校验形状，失败抛 EventWriteError
def read_events(change_dir: Path) -> list[dict[str, JsonValue]]        # 仅保留 JSON object，跳过 scalar/array/坏行

# assurance_agent/workflow/core/snapshot.py —— M6 progression 直接消费，不另立签名
@dataclass(frozen=True)
class FileSnapshot:
    path: Path; existed: bool; content: bytes | None
def capture_files(paths: Sequence[Path]) -> tuple[FileSnapshot, ...]
def restore_files(snapshots: Sequence[FileSnapshot]) -> None
```

### M4 命令面（status/gate/state/decide/risk）

```
aa status --change <id> [--next] [--json]
  退出码（对齐源版 exit_codes.ts 的 terminal 映射）:
    0 = running 或 terminal.kind==completed / 20 = terminal.kind==stopped / 30 = needs_human_review / 40 = command/data error
  --json 输出 WorkflowStatus.model_dump()（含 terminal.kind/terminal.reason/next_dispatch[].agent/skill/kind）
aa gate check --change <id> --phase <phase> [--json]
  退出码（verdict 映射）: pass|enter|exit|skip=0 / needs_fix|needs_human_review|continue=30 / reject|stop=40
aa state apply --change <id> --phase <phase> [--attempt-id <id>]  # M6 原样传 dispatch attempt；只校验 produces 存在性，不二次裁决 exit gate
aa state heal --change <id> --status <s>           # healing 决策记录（strict event）
aa decide --change <id> ...                        # 人工决定（strict event + 回滚）
aa risk context --change <id> [--project-dir <root>]   # 写 explore/context.json
aa risk validate-advisory --change <id>
```

退出码常量统一定义在 `assurance_agent/workflow/core/exit_codes.py`（与源版 `CliExitCodes` 数值一致，benchmark 脚本依赖这些语义）：

```python
EXIT_COMPLETED = 0
EXIT_STOPPED = 20
EXIT_HUMAN_REVIEW = 30
EXIT_ERROR = 40          # command/data error；不是业务 terminal
EXIT_USAGE = 2           # click 用法错误默认值
def exit_code_for_gate_verdict(verdict: str) -> int: ...
def exit_code_for_terminal(terminal: TerminalLike | None) -> int: ...  # Protocol，不 import orchestration.Terminal
```

### M5 执行与报告

```
aa run --change <id> [--allow-test-changes --rerun-reason <t>]  # 执行 + healing 安全守卫（tree-hash/变更守卫/override 留痕）
aa report inspect --change <id>   # 失败分类 → inspect/failure-analysis.json + quality-gate-result.json
aa report generate --change <id>  # Quality Score → report/ 三件套
aa report reclassify --change <id> # 对既有批次重跑失败分类（不重执行）
aa heal validate-proposal|eligibility-summary|safety-check|record-apply --change <id>  # healing 支持命令 + apply 记录
```

```python
# assurance_agent/workflow/execution/runner.py
def run_change(project_root: Path, change_dir: Path, config: AaConfig) -> ExecutionManifest
# execution-manifest.yaml 顶层字段 final_status: PASS|PASS_WITH_WARNINGS|FAIL|SKIPPED
# execution manifest 是 canonical evidence；run 生命周期事件均为 best-effort telemetry，不扩充 strict AuditEvent
# load_execution_evidence(execution_dir, *, batch_id=None) 读取 batch manifest，校验 batch/result identity；
# result 路径相对 execution_dir 且必须保持在该目录内，拒绝绝对路径与 `..` 逃逸；
# 未显式 batch 时才允许 compat fallback，并写 FailureAnalysis.compat_fallback_reason。
# assurance_agent/workflow/report/failure_classifier.py — 规则表数据文件:
#   assurance_agent/_resources/rules/failure-classification.yaml
```

### M6 工作流 Driver

```
aa workflow run --change <id> --scope full|execute --adapter opencode|headless
                [--params <json>] [--server <url>] [--directory <dir>]
                [--model provider/model] [--parent-session <id>] [--agent-cmd <cmd>]
  退出码: 0 completed / 20 stopped / 30 needs_human_review / 40 error（复用 exit_codes.py）
aa workflow status --change <id>       # 读 driver 状态文件
aa workflow start ...                  # detached 启动（供 OpenCode 插件 workflow_start 调用）
```

```python
# assurance_agent/workflow/driver/adapter.py
class PhaseRequest(BaseModel):
    change_id: str; phase_id: str; skill: str | None; agent: str | None; prompt: str
class PhaseResult(BaseModel):
    ok: bool; output: str; error: str | None = None
class Adapter(Protocol):
    def run_phase(self, request: PhaseRequest) -> PhaseResult: ...
# headless_adapter: subprocess 驱动任意 agent CLI（--agent-cmd）
# opencode_adapter: OpenCode server HTTP API
# driver_state.py: driver.lock（pid 死亡自动回收）+ driver-state 文件 + breakpoint/resume
# DefaultStatusProvider 必须把 --scope 原样传给 compute_status。
# HealingActionExecutor 执行 M3 action：allocate 在同一 snapshot 边界写 baseline+allocation frozen events，
# await_human 退出 30，complete 经 aa state heal；普通 dispatch 仅严格写 dispatch_signed，成功后
# aa state apply --attempt-id 原样提交 phase_outcome_committed。driver 生命周期事件只能 best-effort。
```

### M7 Skills + OpenCode

- `assurance_agent/_resources/skills/<name>/SKILL.md`（33 个，aa-* 前缀 + writing-skills）
- `assurance_agent/_resources/opencode/agents/*.md`（6 个 aa-* agent）、`plugins/aa.mjs`、`tools/`
- `aa skill refresh [--build-link 删除][--sync-agents]`——把 skills/agents/tools 从包资源同步到目标项目
- `aa init` 追加 OpenCode 注册（opencode.json plugin 条目 + assets 复制）

### M8 Eval + Retro

```
aa eval run|plan|report ...            # docs/eval.md 为规格
aa retro --retro-id <id> --change <id>... --json    # 聚合; stdout JSON 含 retro_id/signal_count/change_count
aa retro nightly collect --sut <dir> --agent <cmd>  # 退出码 0 成功 / 10 no-op / 其他失败
# 产物: qa/retro/<retro-id>/{context.json,proposals.json,retro-summary.md,review-queue.md}
```

M8 落地补充：`context.json` 顶层增 `signal_count`；nightly phase B 进程内直调 `build_retro_context`（不 shell out `aa retro`）；run 产物在 `eval/out/runs/`。

Eval 重复执行的每个 attempt 必须使用隔离的 SUT 副本；scorer 对 workflow-state / execution-manifest 使用 M2 typed model 校验。`calibrate` 必须实际调用 judge 并写 judge evidence。Retro archive 对注册产物按 M2 model fail-closed 解析，历史坏文件只能降为缺失，不能把 raw dict 注入聚合器。

### M9 文档 / CI / benchmark

- README 重写（Python/uv 安装与使用）；examples/
- CI：pytest + ruff + pyright + lint-imports + `scripts/packaging_smoke_test.sh`
- `benchmark/vue-fastapi-admin/benchmark/run-workflow-loop.sh` 与 `run-workflow-loop-cursor.sh`
  切换到 `aa`（`AWS_BIN`→`AA_BIN` 等），依赖 M4 status 退出码、M6 driver、M7 skills、M8 retro nightly

M9 落地补充：README 命令表与 click group 双向对拍（`tests/integration/test_readme_commands.py`）；`docs/schemas.md` 与 `examples/minimal-sut/` 端到端冒烟（`tests/integration/test_example_minimal.py`）；`.github/workflows/ci.yml` 五 job（ruff、pyright、lint-imports、pytest、packaging smoke）。benchmark 脚本已切换：`AWS_BIN`→`AA_BIN`（默认 `aa`）、`AWS_SKILLS_ROOT`→`AA_SKILLS_ROOT`（指向 `$PROJECT_ROOT/skills`，由 `aa skill refresh` 物化）、`.aws/`→`.aa/`；npm bootstrap（实为 `command -v` 预检）替换为 uv 安装 + `aa skill refresh`。benchmark 使用侧修正：(1) `aa status` 为 0 running/completed、20 stopped、30 needs_human_review、40 error，并让 40 fail closed；(2) 删除 cursor 版自动 `state heal failed` / `kind=exhausted` 恢复，needs-human-review 只提示 `aa decide`。这些不改变 `aa` 契约。benchmark harness 已提交（`.gitignore` 忽略 vendored SUT 与 `runs/`/`resume-logs/` 生成物）。

---

## 生成记录与已登记的接口增补（2026-07-15）

M2–M9 计划一次性生成，各计划自审中登记了如下对本契约的增补/取舍（均已回填到上文对应段，实施时以上文为准）：

- **M2**：注册表实为 13 条（对齐 TS `src/schema/index.ts`）+ `safety_check` 增补；`validate_change` 的 `schema` 形参改为 `WorkflowSchemaLike` 协议（`phase_produces`），解耦 M3；显式未注册 artifact 或一次扫描零注册产物均返回失败，禁止空校验假绿；新增全命令面复用的 `assert_change_id_safe`，阻断 `--change ../...` 路径逃逸。
- **M3**：`WorkflowSchema` 增 `has_phase()` / `gate_for_phase()`（供 M4）；`Terminal.kind ∈ {completed,stopped,needs_human_review}`（无 exhausted，耗尽经 gate `stop` 表达）；`exit_code_for_terminal` 把 `needs_human_review→30`（M3 为 `exit_codes.py` 唯一属主，M4 只消费不重建）；state 完整性哈希（`versioned` 级）。~~`commit_state_transition`/`effective_events`（WAL）~~ **已被下方「忠实重基线」删除——改为 M6 progression 的 snapshot+幂等标记。**
- **M3（2026-07-15 P0/P1 重写，冻结）**：`compute_status` 增 `*, scope` 形参（`owned_by` 过滤，`--scope execute` 生效）；`when`/`ready_when` 改用 `build_evidence_scope`；**needs_fix 路由进 healing**（不终止/不失败）；`read_state`/`write_state` 以 `WorkflowState` 交换（**不再 dict**）。**⚠️ 本条部分做法已被下一条「忠实重基线」取代。**
- **M3（2026-07-15 忠实重基线到 TS 源，冻结——最终以本条为准）**：对齐 `engine.ts`/`schema.ts`/`events.ts`/`progression.ts` 净室转录，取代上一条中与源分叉的做法：
  1. **进度模型**：`compute_status` 由 **produces 文件存在性 + gate 裁决** 驱动，**不再**读 `state.phases.<id>.status` 作进度（gate DSL 仍按字面读 state 作证据）；`PhaseView.status ∈ {pruned,out_of_scope,blocked,ready,awaiting_gate,done,stopped}`。
  2. **gate 时机**：exit gate 只在 `produces_present` 时裁决（绝不对未运行 phase 裁决）；needs_fix → `awaiting_gate`，其 `repair_of` phase 经 repair 路由变 `ready`。
  3. **when 作用域**：删除 `PhaseDef.reads`；`when`/`ready_when` 用 `WorkflowSchema.produces_alias_map()`（对所有 produces 的**全局反向别名**，对齐源版 `reverseAlias`，`hoist_primary=False`），打包 schema **无需改**即可解析 `fix_proposal` 等别名。
  4. **gate 裁决**：按**声明顺序 first-true-wins**（对齐源版），安全序由加载期 canonical 声明序强制（Task 4 `_SAFETY_ORDER`），**不再运行期固定优先级排序**；schema 与 `GateVerdict` 共用唯一闭集 `Verdict(StrEnum)`，未知 YAML verdict 加载期失败。打包 `fixer-safety-gate` 同步重排为 human-review 在 pass 之前。
  5. **无 WAL**：删除 `_txn`/`commit_state_transition`/`effective_events`/`current_committed_txn`；`state.py` 仅原子写 + 完整性 hash + `state_guard()`；`events.py` 为 best-effort/strict append + `read_events`；事务写边界（snapshot 回滚 + 幂等标记 `attempt_id`/`operation_id`/`state_guard`）下移到 **M6 progression**。
  6. **healing seam**：M3 定义 typed `HealingStateSnapshot`/provider，并提供默认 typed-event projection；`attempts_used` 只数当前 episode 内按 `operation_id` 去重的 `healing_attempt_allocated`，不再数 `heal_record_apply`、不回退手写 state。M3 的纯 `project_healing_episode` 读取 allocation/phase-outcome 事件，负责复用产物的 rerun/reinspect、continue 与 STOP；M6 只负责 strict event + snapshot 写边界。
  7. **打包 schema 自洽**：`loops.healing.counter` 由 `state.phases.healing.attempts` 改为 `attempts_used`（与 gate 读取一致）。
  8. 新增导出 `resolve_change_path`、`produces_alias_map`、`HealingStateProvider`。
  9. **state 类型**：M2 canonical `WorkflowState` 显式建模 `phases.healing`、execution/inspect、gates、run_context；`extra="allow"` 只兼容动态 phase/扩展键。DSL dump 使用 `exclude_none=True`。
  10. **路径与图**：`resolve_change_path` 必须替换 `<change-id>`；schema 加载期拒绝重复 phase id、非法 `requires_mode` 与 phase DAG cycle。
- **M3（human-decision 收口）**：`compute_status` 对最新 `human_decision` 投影；最新 action=`stop` 时覆盖 terminal/清空 dispatch，之后出现非 stop decision 时恢复普通投影。`WorkflowStatus` 的 direct-construction 默认 healing snapshot 为 inactive，兼容 M4/M6 测试构造。
- **M4**：`exit_code_for_terminal` 取结构化 `TerminalLike`（不 import engine.Terminal）；`risk` 作为新分层插入冻结层序 `commands → risk → workflow → artifacts`（risk 在 workflow 之上、artifacts 仍在 workflow 之下）；risk archive 采样直接读 `execution/runs/<batch>/*-result.json`，不依赖 M5 evidence loader。state apply/heal/decide 已统一为 canonical `WorkflowState` 属性读写；`state apply --attempt-id` 原样提交 frozen outcome、只校验产物且不二次裁决 exit gate，event+state 失败按文件快照回滚。
- **M5**：API/E2E/Fuzz 统一走 pytest `--json-report`（per-target 中间结果为 `free` 级、不入注册表）；失败分类规则表外置为 `_resources/rules/failure-classification.yaml`。
- **M6**：`gate` 裁决在 `compute_status` 内间接完成，driver 不在边界二次调 `check_gate`；`kind==cli` 阶段经 `ProcessRunner` 调 pinned `aa run`/`aa report`/`aa state apply --attempt-id`；`DefaultStatusProvider` 透传 scope；`HealingActionExecutor` 执行 M3 allocate/await-human/complete action，driver 只严格写冻结审计事件，生命周期事件全部 best-effort。
- **M7**：plugin 条目用本地文件路径 `./.opencode/plugins/aa.mjs`（净室包不发 npm）；`--build-link` 删除；`workflow_start` 调 `aa workflow start`（M6 detached 入口）。
- **M5（P1 修订）**：healing 安全边界（test/product tree-hash、healing 变更守卫、`--allow-test-changes` override evidence、`compat_fallback` 读取模式）与 `aa heal record-apply`/`aa report reclassify` 属**安全边界 + 一一对应迁移**，已回归 M5 绑定范围（Task 8b/8c，对齐 TS `healing_state.ts`/`override_evidence.ts`/`heal.ts`/`report.ts`）；仅 coverage `server-process` 模式与报告富化字段仍 out-of-scope。
- **M8（P1 修订）**：`context.json` 顶层增 `signal_count`；nightly phase B 进程内直调 `build_retro_context`；`eval gate|compare|baseline update`（`--run` 作用域）已回归绑定范围；repeat attempt 使用隔离 SUT，scorer/archive 消费 typed artifacts，judge calibrate 写 evidence。仅 `--batch` 作用域与 OpenCode 过程可观测性 deferred。
- **M9**：benchmark 侧按冻结契约处理 `aa status` 0/20/30（业务态）与 40（错误，fail closed）；Cursor `recover_dead_end` 只诊断、不自动伪造 `state heal failed`，needs-human-review 交给 `aa decide`。内嵌 status 调用读取 `AA_BIN` 且显式接受 0/20/30。
