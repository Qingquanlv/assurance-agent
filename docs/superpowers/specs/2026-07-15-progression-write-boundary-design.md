# progression 深模块：state-commit 协议下沉设计

日期：2026-07-15
状态：已获用户批准（grilling 全树走完，Q1–Q8 决策见下表）；已纳入两轮架构评审修订（并发隔离、strict writer 收口、guard 校验、crash-prefix reconciliation、重放幂等、rollback 失败分类、write_file 约束、record_decision 收口、退出码冻结）；待实施
关联：修订 `2026-07-14-python-migration-design.md` 第 4 节「M6 driver 是唯一事务写边界」条款（见第 7 节）

## 1. 背景与问题

架构评审（2026-07-15）确认：编排核心（DSL / gates / engine / healing 投影）是真正的深模块，但 **snapshot → strict event → state 写 → 失败 restore** 这套安全关键提交协议存在四份手写副本，错误策略互不一致，且写原语住在 CLI 层：

| 副本 | 位置 | 守卫文件 | 失败策略 |
|---|---|---|---|
| `commit_state_change` | `commands/state_cmd.py:122-131` | events.jsonl + workflow-state.yaml | `SystemExit(EXIT_ERROR)` |
| driver `_allocate` | `workflow/driver/loop.py:235-273` | events.jsonl + healing/entry-baseline.json | 返回 `PhaseResult(ok=False)` |
| run override 路径 | `commands/run_cmd.py:93-123` | override evidence json/diff + events.jsonl | 重抛 |
| `record_apply_summary` | `workflow/healing/safety.py:284-302` | apply-summary json/md + events.jsonl | 重抛 |

伴生缺陷：

- `decide_cmd` 反向 import 兄弟 CLI 模块 `state_cmd` 取写原语（分层倒置）；
- driver 提交 phase outcome 走子进程 `aa state apply`，而 healing allocation 却在进程内直接写 strict 事件（同一进程两种写路径）；
- 三处 strict 事件依赖 side-effect 产物（sha256 等），导致事件构造与文件 IO 纠缠在每个调用点。

目标：**一个接口、一处实现、一组故障测试**——进程内异常下的审计原子性（strict 事件与状态转换要么同现、要么同无），以及进程被 kill 后合法写入前缀的重放修复知识，全部集中到一个深模块及其领域操作；调用方不得自行实现锁、guard、幂等或恢复策略。

## 2. 已确认决策（grilling Q1–Q8）

| # | 决策项 | 结论 |
|---|---|---|
| Q1 | 接口形状 | **事务上下文管理器**（`with transaction(change_dir) as txn:`），非声明式单函数 |
| Q2 | 守卫集合 | 经由 txn 方法的写自动纳入守卫（调用方无法漏保护）；无显式 guard 声明 |
| Q3 | 写入时机 | **全缓冲到 `__exit__` 统一应用**；块内异常时 artifact/event/state 零写入（持久 lock inode 除外） |
| Q4 | 外部 IO helper | **纯化为内容生产者**（返回 bytes + 元数据，不做 IO）；`txn.guard()` 逃生口取消 |
| Q5 | 失败契约 | 基础设施错误统一继承 **`ProgressionError`**：锁超时、提交失败且已恢复、提交与恢复均失败分别为不同子类；块内业务异常原样穿透 |
| Q6 | driver 写路径 | **进程内直调**领域操作，废除 `apply_phase_state` 的子进程 hop（放弃该处 TS 子进程对齐与「CLI 唯一写权威」，记 ADR） |
| Q7 | 领域操作归属 | dispatch / phase-outcome / healing allocation / heal transition / decide 语义下沉为共享领域操作；driver 与 CLI 全部退成薄 adapter；测试假件 `InProcessAa` 删除 |
| Q8 | 落地方式 | **单提交一次到位**；typed event 构造器全量迁移（评审候选 5）不搭车 |

**Q7 落点修正**（实施前事实核查）：grilling 中初定 `workflow/core/phase_outcome.py`，但冻结 import 契约规定 `workflow.core` 不得 import `workflow.orchestration`，而 `apply_phase_outcome` 需要 `WorkflowSchema`（orchestration 层）、`record_decision` 的 stop 快照需要 `compute_status`（engine）。故：

- 通用事务 → `workflow/core/progression.py`（只依赖 core 的 events / state / snapshot）；
- 领域操作 → `workflow/orchestration/operations.py`（orchestration 可 import core，driver / commands 可 import orchestration——完全在既有分层内，不改 `.importlinter`）。

## 3. `workflow/core/progression.py` — 事务接口（唯一测试面）

```python
class ProgressionError(AaError):
    """progression 基础设施失败；所有 CLI adapter 统一映射 EXIT_ERROR (40)。"""

class ProgressionCommitError(ProgressionError):
    """事务应用失败，但守卫文件已全部恢复；原始原因经 __cause__ 保留。"""

class ProgressionRollbackError(ProgressionError):
    """应用失败且恢复也失败——磁盘可能处于部分提交态，需要人工介入。
    .commit_cause = 触发回滚的原始异常；.rollback_cause = 恢复期异常。"""

class ProgressionLockTimeout(ProgressionError):
    """同一 change 的 advisory lock 在期限内未取得。"""

@contextmanager
def transaction(change_dir: Path) -> Iterator[ProgressionTxn]: ...
    # __enter__ 获取 per-change commit lock；__exit__ finally 释放

class ProgressionTxn:
    def read_state(self) -> WorkflowState: ...                            # 锁内读（见并发语义）
    def read_events(self) -> list[dict]: ...                              # 锁内读（幂等键查重用）
    def current_state_guard(self) -> str: ...                              # 锁内读取磁盘 state sha256
    def write_file(self, rel: str, content: bytes | str) -> None: ...     # 缓冲；相对 change_dir
    def append_strict(self, event: dict | BaseModel) -> None: ...         # 缓冲；入参契约同 append_event_strict
    def set_state(self, state: WorkflowState) -> None: ...                # 缓冲；每事务至多一次，重复调用抛 ValueError
```

### 并发隔离（冻结）

- **per-change composite lock**：`__enter__` 先竞争进程内按 canonical change path 索引的 `threading.Lock`，再打开 `qa/changes/<id>/.progression.lock` 并用 POSIX `fcntl.flock(fd, LOCK_EX | LOCK_NB)` 竞争内核锁；前者覆盖同进程多线程，后者覆盖多进程，支持 macOS/Linux（本项目冻结的 CLI/CI 运行平台）。advisory lock 由打开的 fd 持有，进程退出时由内核自动释放，**不使用 O_EXCL、不删除 lock file、不做 PID stale-reclaim**，从结构上消除双回收者 unlink/create 的 split-brain。两层竞争共享一个总 deadline（默认 500ms），均不得无限阻塞；超时则按已取得资源的逆序释放并抛 `ProgressionLockTimeout`。`__exit__` 在 `finally` 中 unlock + close + 释放线程锁。
- **读取必须入锁**：operations 的 read-modify-write 一律通过 `txn.read_state()` / `txn.read_events()` 在锁内读取；`transaction()` 块外读到的 state 不得作为 `set_state` 的构造基础（代码评审级纪律 + operations 层结构保证——operations 内部先开事务再读）。
- **为何锁而非 CAS**：`state_guard` CAS 只能保护 state 覆盖，不能防止「恢复快照擦掉并发提交者已追加的事件」——快照恢复要求快照与恢复之间无其他写者，只有互斥能给出该保证。锁在手时 CAS 冗余，故不引入。
- **所有 strict writer 必须入锁**：运行时代码不得在 `progression.py` 外直接调用 `append_event_strict`。包括独立的 `dispatch_signed`；它虽不伴随 state 写，也会修改其他事务 snapshot/restore 的 `events.jsonl`，故必须通过第 4 节 `record_dispatch` 进入同一把锁。
- **best-effort 遥测是唯一例外**：telemetry append 不经锁。恢复窗口内（snapshot 后、restore 前）落入的遥测行会被一并回滚——契约允许（best-effort 本就可静默丢失），在模块 docstring 中明示；任何 projection 不得依赖 best-effort 事件的 seq 唯一性或持久存在。

### 应用语义（冻结）

1. **块内业务数据零落盘**：write/append/set 只缓冲意图。除持锁所需的持久 `.progression.lock` inode 外，块内抛出的任何异常原样穿透（`__exit__` 返回 False 前仅释放锁），此时磁盘无本事务的 artifact/event/state 写入，无需回滚。
2. **`__exit__` 应用顺序**（结构强制，调用方不可改变）：
   1. snapshot 全部目标文件（缓冲文件 ∪ events.jsonl ∪（有 `set_state` 时）workflow-state.yaml）——沿用 `workflow/core/snapshot.py`；
   2. 按缓冲顺序落文件（父目录按需创建；str 按 UTF-8 编码；**一律临时文件 + `os.replace` 原子替换**，对齐现状 baseline 写入的 crash-atomic 语义）；
   3. 按缓冲顺序 `append_event_strict`（seq 在此刻分配）；
   4. 最后 `write_state`（原子替换 + 完整性哈希，沿用 `workflow/core/state.py`）。
3. **失败恢复与错误分类**：应用中途异常 → `restore_files` 恢复全部快照 → 抛 `ProgressionCommitError`（原因在 `__cause__`）。**恢复自身失败**（restore 期间再异常）→ 抛 `ProgressionRollbackError`，同时携带 `commit_cause` 与 `rollback_cause`，消息指明磁盘可能部分提交、列出受影响文件；「已回滚」语义只属于 `ProgressionCommitError`。
4. **`write_file` 约束**：入参为相对 change_dir 的路径；解析后必须落在 change_dir 子树内（`resolve` + `relative_to` 校验，拒绝 `..`/绝对路径）；**保留文件 `events.jsonl`、`workflow-state.yaml`、`.progression.lock` 拒绝**——它们分别只能经 `append_strict`、`set_state`、transaction 内部锁实现操作。违反即块内 `ValueError`（零业务数据落盘）。`run_cmd` override evidence 位于 `execution/runs/<batch-id>/`，不受影响。
5. **进程内审计不变量**：strict 事件先于 state 写由第 2 条结构保证；「state 已推进但审计事件缺失」的中间序在可捕获的进程内异常下不可构造。
6. **kill-prefix 契约**：多文件 snapshot 不是 WAL；进程被 SIGKILL/断电时允许磁盘停在应用顺序的合法前缀（例如 file 已 replace、event 已 append、state 尚未写）。模块不宣称跨进程崩溃原子性。第 3a/4 节为每个带幂等键的领域操作冻结 prefix detection + reconciliation，使重放补齐缺失后缀而不是仅凭已有事件 no-op；无幂等键的人工 decision 以 strict event ledger 为事实源，state 中的 decisions/terminal 是可恢复投影视图，不得反向覆盖 ledger。
7. 空事务（未调用任何缓冲方法）的 `__exit__` 是 no-op（仍会取锁/放锁）。

### 依赖

只 import 标准库 `fcntl` 以及 `workflow.core.events` / `workflow.core.state` / `workflow.core.snapshot` / `artifacts.WorkflowState` / `exceptions`。不 import orchestration / driver / commands。

## 3a. 重放、guard 与 crash-prefix reconciliation（冻结；承接原总 spec:89）

幂等键查重在 operations 层、**锁内**（`txn.read_events()` 之后、staging 之前）执行，使 check-then-append 原子：

| 写入 | 幂等键 | 完整重放（同键同义） | kill-prefix / 冲突 |
|---|---|---|---|
| `record_dispatch` | `attempt_id` 对 `dispatch_signed` | 相同 phase/kind/target/guard → no-op，返回既有 receipt | 同 `attempt_id` 不同 payload → `AaError`；driver 仍为每次物理 dispatch 生成新 UUID |
| `apply_phase_outcome` | `(phase_id, attempt_id)` 对 `phase_outcome_committed` | event 已存在且 state phase entry 的 `attempt_id` 相同 → **no-op**，返回 `replayed`；state marker 指向同 phase 的更晚合法 outcome → 保留新 state，返回 `superseded` | event 已存在、state marker 缺失且它仍是该 phase 最新 outcome → 不重复 event，仅补 phase entry，返回 `reconciled`；同 `attempt_id` 不同 phase、marker 指向不存在/非该 phase outcome、已标记 payload 冲突 → `AaError` |
| healing allocation | `operation_id` 对 `healing_attempt_allocated` | allocation event 已存在，说明按 file→baseline-event→allocation-event 顺序整束完成 → no-op | 仅 file 或 baseline event 存在 → 校验已有 prefix 后只补缺失后缀；同 `operation_id` 不同 payload、baseline hash/identity 不一致 → `AaError` |
| `record_heal_transition` | 最新 `heal_transition.to` + state healing status | event 与 state 都是目标 status → no-op | 最新 event 已是目标但 state 仍旧 → 只补 state；下一目标不同则以最新 strict event 的 `to` 为 `from`，不得用滞后 state 伪造 prior |
| `record_decision` | 无 | 人工命令重复执行如实追加；重复决定本身是审计事实 | stop 的运行时真相始终来自 `human_decision` ledger；operations 每次在锁内先把可由 ledger 完整重建的 decision fields 投影回 state，再追加新决定。`state_at_stop` 是诊断快照，kill 后缺失不影响 stop 语义，且不得以重算值冒充原快照 |

### dispatch guard（首次 outcome 提交必须校验）

- `record_dispatch` 在同一 transaction 内先取 `txn.current_state_guard()`，再追加 `dispatch_signed`，从而签入 dispatch 当刻的 state hash。
- `apply_phase_outcome(attempt_id=...)` 若传入非空 attempt id，视为 driver 提交：先在锁内查唯一同 phase/attempt 的 `dispatch_signed`。若 outcome 尚不存在，当前 `txn.current_state_guard()` 必须等于 signed guard，否则抛 `StaleDispatchError(AaError)`，不追加 outcome、不写 state。
- 若 matching outcome 已存在，先走上表 replay/reconciliation，再返回；不能因后续 state 合法变化而用旧 guard 拒绝已经完成的重放。
- `attempt_id is None` 才是人工提交：operation 生成 `manual:<phase>:<uuid>`，明确跳过 dispatch lookup/guard。调用方显式传入任意非空 id 却找不到 matching dispatch 时 fail closed。

测试中的 “crash-retry” 必须预置真实合法前缀（尤其 outcome event 已有但 state marker 缺失，以及 baseline file/event 只完成前 N 步），禁止只预置 event+state 均完成后把普通 replay 冒充 crash recovery。

## 3b. CLI 错误映射（冻结）

所有事务基础设施错误共享 `ProgressionError` 基类，adapter **必须先捕获该基类**；映射冻结为：

| 异常 | CLI 退出码 | 现状对应 |
|---|---|---|
| `ProgressionError`（含 commit / rollback / lock timeout） | `EXIT_ERROR (40)` | commit rollback 沿用现状；lock timeout 同属基础设施失败 |
| 其它校验类 `AaError`（unknown phase、missing produces、stale dispatch、非法 status/action 等） | `1` | `state_cmd`/`decide_cmd` 现有 `SystemExit(1)` |

所有 CLI adapter（state / decide / run override / heal record-apply）统一先 `except ProgressionError` 退 40，再 `except AaError` 退 1；不得各自重建映射。driver adapter 对两类异常均转 `PhaseResult(ok=False, error=...)`，其中 `ProgressionRollbackError` 必须把「部分提交态」写入 error 文本并 fatal 结束本轮。

## 4. `workflow/orchestration/operations.py` — 共享领域操作

driver 与 CLI 穿同一接口；全部经 `transaction()` 提交。

```python
class StaleDispatchError(AaError):
    """首次 outcome 提交时，dispatch 后的 state guard 已变化。"""

@dataclass(frozen=True)
class DispatchReceipt:
    phase_id: str
    attempt_id: str
    state_guard: str
    disposition: Literal["committed", "replayed"]

@dataclass(frozen=True)
class AppliedOutcome:
    phase_id: str
    attempt_id: str
    applied_status: str          # "pass"（ORCHESTRATOR_INTERNAL）或 "done"
    disposition: Literal["committed", "replayed", "reconciled", "superseded"]

@dataclass(frozen=True)
class HealingAllocationResult:
    operation_id: str
    attempt_id: str
    disposition: Literal["committed", "replayed", "reconciled"]

@dataclass(frozen=True)
class HealTransition:
    from_status: str
    to_status: str
    disposition: Literal["committed", "replayed", "reconciled"]

def record_dispatch(
    change_dir: Path, *, phase_id: str, kind: Literal["dispatch_phase", "heal"],
    attempt_id: str, target: Literal["api", "e2e"] | None = None,
    dispatched_at: int | None = None,
) -> DispatchReceipt:
    # with transaction: guard = txn.current_state_guard(); 查 attempt_id 重放/冲突；
    # append dispatch_signed。dispatched_at 缺省用当前 epoch ms；同键 replay 返回原 receipt。

def apply_phase_outcome(
    project_root: Path, change_dir: Path, schema: WorkflowSchema,
    phase_id: str, *, attempt_id: str | None = None,
    skill: str | None = None, skill_md_path: str | None = None,
) -> AppliedOutcome:
    # 1. schema.has_phase 校验（未知 phase → AaError）
    # 2. produces 存在性校验（resolve_change_path；缺失 → AaError，列出缺失项）
    # 3. with transaction(change_dir) as txn:                 # 读改写全程在锁内
    #        events/state = txn.read_events()/txn.read_state()
    #        attempt_id is None → 生成 manual:*；否则查 matching dispatch
    #        outcome 已存在 → 检查 state phase entry.attempt_id：相同 replay，缺失且仍最新则
    #          reconcile，更晚合法 marker 则 superseded；不得用旧 replay 覆盖新 state
    #        outcome 不存在且为 driver attempt → 当前 guard 必须等于 signed guard
    #        构造 phase entry：status（ORCHESTRATOR_INTERNAL → pass，否则 done）、attempt_id、
    #          skill_loaded / skill_md_path / skill_loaded_at（Skill Load Gate 字段）
    #        首次提交 append phase_outcome_committed + set_state；reconcile 只 set_state

def allocate_healing_attempt(
    change_dir: Path, allocation: HealingAttemptIntent,
) -> HealingAllocationResult:
    # driver 不再持有 baseline/event/幂等知识。锁内按 3a 检查 operation_id 与合法 prefix；
    # 需要 pin 时 txn.write_file("healing/entry-baseline.json", data) + baseline event，
    # 最后 append healing_attempt_allocated；只补缺失后缀。

def record_heal_transition(change_dir: Path, status: str) -> HealTransition:
    # HEAL_STATUSES 校验（迁自 state_cmd）→
    # with transaction(...): prior 以最新 strict heal_transition.to 为准，state 只作展示
    #   event/state 都等于 status → replay；event 已有但 state 落后 → reconcile state；
    #   否则 append heal_transition(from,to) + set_state，返回冻结的 from/to/disposition

def record_decision(
    project_root: Path, change_dir: Path, *, checkpoint: str, action: str,
    reason: str, who: str, evidence: str | None = None,
) -> None:
    # 动作白名单 / reason 非空 / evidence 路径安全 + sha256（迁自 decide_cmd）→
    # with transaction(...):
    #     events/state = txn.read_events()/txn.read_state()；先以 ledger 修复可重建的 decisions 字段
    #     action == "stop" 时：内部经 compute_status 校验非 terminal（已 terminal → AaError，
    #       冻结现状 decide_cmd 行为）并生成 state_at_stop 快照——快照不作为公共入参，
    #       调用方无法传入伪造或过期快照
    #     txn.append_strict(human_decision) ; txn.set_state(decisions 追加；stop 时写 terminal)
```

注：`record_decision` 的 stop 分支需要 `compute_status`（engine），这正是 operations 落在 `workflow.orchestration` 而非 `workflow.core` 的原因之一（第 2 节 Q7 落点修正）。

state 变换所需的 `model_dump → 改 dict → model_validate` 细节（含连字符→下划线键规则）随迁至本模块私有 helper（`_with_phase` / `_with_healing_status` 从 `state_cmd` 迁入并私有化）。整层 overlay 接口统一是评审候选 2 的范围，本次不做。

## 5. 纯化的内容生产者（Q4）

三个直接落盘的 helper 改为纯函数，IO 归 txn：

| 现状 | 纯化后 |
|---|---|
| `write_test_changes_override_evidence(...)` 写 json + diff 并返回 sha | `build_test_changes_override_evidence(...) -> OverrideEvidenceContent`（json_bytes / diff_bytes / sha256 / rel_path），调用方 `txn.write_file` ×2 + `txn.append_strict(HumanDecisionEvent(review_sha256=...))` |
| `record_apply_summary(...)` 写 summary json + md 并 append | `build_apply_summary(...) -> ApplySummaryContent`（json_text / md_text / summary_sha256），提交仍由 `record_apply_summary` 包装（签名不变，内部改 txn），保持 heal CLI 调用点稳定 |
| driver `_allocate` 内联写 entry-baseline.json | baseline payload 构造为 `operations.py` 内部纯内容 producer；`allocate_healing_attempt` 统一执行 `txn.write_file("healing/entry-baseline.json", data)` + 1–2 条 strict event，driver 不接触写细节 |

纯函数可脱离 tmp_path 直接单测。时间戳、`git diff` 输出、TreeHash、proposal/context 等全部由调用方先取得并作为值传入；builder 内禁止 `Path.read/write`、`datetime.now()`、`subprocess` 和环境读取，sha256 必须基于它实际返回并由 txn 原样写入的 bytes/text 计算。

## 6. 迁移清单（单提交）

| 位置 | 动作 |
|---|---|
| `workflow/core/progression.py` | 新增 composite advisory lock、`transaction` / `ProgressionTxn` / `ProgressionError` 全错误层级；只有本模块可直调 `append_event_strict` |
| `artifacts/models/state.py` | `PhaseState` 新增可选 `attempt_id`，作为 outcome event→state reconciliation marker；兼容性加字段，不提升 `schema_version` |
| `workflow/orchestration/operations.py` | 新增 `record_dispatch` / `apply_phase_outcome` / `allocate_healing_attempt` / `record_heal_transition` / `record_decision`、冻结结果类型与私有 state 变换 helper；集中 guard、幂等与合法 prefix 修复 |
| `commands/state_cmd.py` | `commit_state_change` / `_with_phase` / `_with_healing_status` 删除；`state apply` / `state heal` 退成薄 Click adapter（参数解析 → operations → **先 `except ProgressionError` 退 40，再 `except AaError` 退 1**，按 3b 表） |
| `commands/decide_cmd.py` | 改调 `record_decision`；删除对 `state_cmd` 的 import；统一消费 3b 错误映射 |
| `commands/run_cmd.py` | override 路径改 txn + 纯化 evidence builder；统一消费 3b 错误映射 |
| `workflow/healing/safety.py` | `record_apply_summary` 内部改 txn；内容构造纯化 |
| `workflow/healing/override_evidence.py` | `write_test_changes_override_evidence` → `build_test_changes_override_evidence` 纯函数 |
| `commands/heal_cmd.py` | `record-apply` 统一消费 3b 错误映射：先 `ProgressionError`→40，再其它领域/校验错误→1 |
| `workflow/driver/loop.py` | 每次物理 dispatch 的直接 `append_event_strict` 改调 `record_dispatch`；`_allocate` 写逻辑删除改调 `allocate_healing_attempt`；`DefaultCliPhaseExecutor.apply_phase_state` 改**进程内**调 `apply_phase_outcome`（`ProgressionError` / `AaError` → `PhaseResult(ok=False)`）；`SubprocessRunner` 仅保留 cli-kind phase（`aa run` / `aa report …`） |
| `CliPhaseExecutor` 协议 | `apply_phase_state` 语义不变（签名维持，便于测试注入），默认实现不再 shell |
| tests | 新增 `tests/unit/core/test_progression.py`（提交顺序、块内异常零业务数据落盘、应用中途失败恢复、`__cause__` 链、set_state 重复调用、空事务、**write_file 路径逃逸/三个保留文件拒绝、restore 自身失败 → ProgressionRollbackError 双因、线程+进程互斥、持锁进程被 kill 后内核自动释放、竞争超时 → ProgressionLockTimeout**）；operations 测试补 **guard、完整 replay、同键冲突、合法 kill-prefix reconciliation**（3a 全表）；`InProcessAa`（`test_loop_real_provider.py` / `test_packaged_happy_path.py`）删除改直调；`test_state_cmd` / `test_decide` / `test_run_guard` 断言面从内部函数移到 operations 接口，退出码按 3b 冻结 |
| `docs/adr/0001-driver-in-process-phase-outcome.md` | 新增：记录放弃子进程 hop 与「CLI 唯一写权威」的理由（评审依据、性能非动机、审计不变量由 progression 承接），防未来评审反复重提 |

明确**不做**（范围外）：typed event 构造器全量迁移（候选 5）、WorkflowState overlay 统一接口（候选 2）、事件账本投影 facade（候选 3）、schema 声明 executor kind（候选 4）。

## 7. 对已冻结契约的修订

- 原 spec 第 4 节「M3 只提供 typed event、snapshot 和纯 projection 原语，**M6 driver 是唯一事务写边界**」修订为：「**`workflow/core/progression.transaction` 是唯一事务写边界实现**；所有 strict runtime writer 必须经 `workflow/orchestration/operations` 或持有 txn 的既有领域 wrapper 进入该实现。写入顺序（快照 → 文件 → strict 事件 → state → 进程内失败恢复）由 progression 结构强制；SIGKILL/断电只保证留下合法前缀，由领域操作重放补齐可恢复后缀。」
- `.importlinter` 不变：progression 在 `workflow.core`（既有层），operations 在 `workflow.orchestration`（既有层），无新增层或反向依赖。
- frozen audit event payload 形状、driver/status 退出码（0/20/30/40）、`workflow-state.yaml` 完整性哈希均不变；普通 CLI 参数/校验错误继续为 1，事务基础设施错误为 40。`PhaseState.attempt_id` 是向后兼容的可选展示/reconciliation 字段。

## 8. 验收标准

1. `rg "capture_files|restore_files" assurance_agent --files-with-matches` 仅命中 `workflow/core/snapshot.py` 与 `workflow/core/progression.py`；
2. `rg "append_event_strict" assurance_agent --files-with-matches` 仅命中 `workflow/core/events.py` 与 `workflow/core/progression.py`；`commands/`、driver、healing 均不得直写 strict event 或 state（best-effort 遥测不受限）；
3. `decide_cmd` 不 import `state_cmd`；
4. driver 正常路径零 `aa state apply` 子进程调用（`test_loop` 的 runner 调用记录断言）；
5. 全量 pytest / ruff / pyright / lint-imports 通过；`test_packaged_happy_path`、`test_loop_real_provider`（改造后）、healing golden 全绿；
6. progression 单测覆盖：提交顺序（文件→事件→state）、块内异常零业务数据落盘、应用第 N 步失败时前 N-1 步已恢复、`ProgressionCommitError.__cause__` 保留原因；
7. 并发：同线程/双线程/双进程提交者对同一 change 并发 read-modify-write 时互斥，后到者等待或超时，**无状态丢失、无 strict event 被恢复擦除**；预置持锁子进程后 kill，下一提交者无需 stale-delete 即可取得 advisory lock；
8. strict writer 收口：真实 driver dispatch 与 healing allocation 均通过 operations；构造「事务 snapshot 后，另一个 dispatch 尝试写入，前者失败 restore」的并发测试，dispatch 只能等待且最终不会被擦除；
9. 幂等/reconciliation：3a 表逐行测试——完整重放事件数不增、同键异义抛错；预置 outcome-event-only/state-marker-missing、baseline file-only、baseline-event-only、heal-transition-event-only 等合法前缀后，只补缺失后缀；
10. guard：首次 driver outcome 有 matching dispatch 且 guard 未变才成功；guard 改变抛 `StaleDispatchError` 且零 outcome/state 写；已提交 outcome 在后续 state 改变后仍可 replay/reconcile；manual outcome 明确跳过 guard；
11. 回滚失败：monkeypatch `restore_files` 抛错 → `ProgressionRollbackError`，`commit_cause` / `rollback_cause` 均非空；
12. 退出码回归：所有 CLI adapter 的 `ProgressionError`=40、其它 `AaError`=1；`decide` 对已 terminal workflow 执行 stop=1（行为冻结自现状）。
