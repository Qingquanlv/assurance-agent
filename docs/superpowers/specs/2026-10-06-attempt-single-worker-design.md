# Attempt Runtime Spike 修订：单 Worker 与中断后重新生成

日期：2026-10-06。

Status: implemented. Current concurrency-correction validation is recorded in
[MR67](https://github.com/Qingquanlv/assurance-agent/pull/67). Historical
pre-correction baseline evidence (2026-10-07): 4732 tests passed, 15 skipped;
static/import checks and all three exact-source wheel smokes passed. That
historical gate does not establish verification of the subsequent corrections.


## Implemented behavior and verification limits (2026-10-07)

`worker_lifecycle.py` holds a canonical-workspace lifetime flock across admission,
startup, execution and cleanup; a short control flock keeps status/stop available.
Application, direct CLI, bootstrap, managed operator and background worker paths
share admission. Different Invocations also exclude one another in the same
workspace. Background launch hands off a reserved FD and nonce through a bounded
handshake. A target-derived preparation reservation precedes directory creation,
`git worktree add`, and all runtime seeding. The same canonical-path reservation
also guards direct/reuse admission to a worktree already visible during seeding;
it transfers to the lifetime flock without a gap and releases before authenticated
background FD handoff. Distinct changes in the same source repository use distinct
reservations and can prepare independently. The reservation inode is a stable
file at `target.parent/.aa-preparation-locks/target.name` on the same filesystem,
with no host/UID partition. The target basename is used unchanged, so native case
and Unicode filename equivalence determine aliases without hashing normalized
path text or extending maximum-length names. The lock directory is reserved
infrastructure, including casefold spelling variants and targets nested inside
it; those targets are rejected before creation. Symlink/non-directory namespace
shapes and non-regular/symlink lock files fail closed. Only lock-required ancestor
directories are created before reservation; permission failure closes admission,
and files are not deleted on release. Nested borrowing checks canonical
path, OS PID, thread and async task.

Linux signals use `os.pidfd_open` handles acquired before creation-identity
validation and `signal.pidfd_send_signal` for TERM, KILL, STOP and failure-recovery
CONT. There is no numeric-PID or `killpg` fallback when pidfds are unsupported or
denied. Dedicated groups are frozen with confirmed stopped state and bounded
rescans, including children born before freeze; every discovered member is bound
to a verified handle. Confirmed frozen groups receive KILL directly and remain
frozen so no member can spawn during exit verification. Ordinary processes
retain TERM followed by KILL after the termination timeout. An absent
leader at handle acquisition uses the existing natural-exit confirmation without
signaling; an unproved remaining group, failed enumeration, or unconfirmed freeze
fails closed for this signaling path. Vanished stat entries trigger fresh scans
within the same freeze deadline before accepting quiescence: an unknown vanished
parent may have spawned a child after the preceding directory snapshot. Permission,
parse and proc-root failures, persistent churn and unknown owned zombies fail
closed. After confirmed whole-group freeze and all bound handles exiting, vanished
entries can be skipped because no authenticated member can spawn. Only pauses introduced by this stop controller
are undone on its verified handles. Handle closure is unconditional. An empty
snapshot or leader exit alone is not the active termination proof. Naturally
exited owners retain the existing owner/children, lifetime flock and authenticated
external terminal-proof protocol. This covers owned process groups; it does not
introduce supervision of processes that escaped their recorded group.

macOS retains repeated native creation-identity checks and numeric signals. The
check/signal TOCTOU can signal a replacement PID; unconditional PID-reuse
protection is therefore not claimed on macOS. A terminal business status or closed
Invocation lock never authorizes takeover on either platform.

Python 3.11 documents Linux pidfd requirements:
[`os.pidfd_open`](https://docs.python.org/3.11/library/os.html#os.pidfd_open)
(Linux 5.3+) and
[`signal.pidfd_send_signal`](https://docs.python.org/3.11/library/signal.html#signal.pidfd_send_signal)
(Linux 5.1+); the implementation uses flags=0.

```sh
uv run aa operator stop --json --project-dir PATH --run-id ID --force
uv run aa bootstrap stop --run-dir RUN_DIR --force
```

Without `--force`, stop remains a durable checkpoint request. Force stop exits 0
only after confirmation; exit 40 keeps replacement blocked. `stop_run` captures the
original nonce before the control mutex, checks Invocation/directory/process
identity, confirms owned children and authenticated external calls ended, then
releases only owned grants and marks generations abandoned. Acknowledged or
indeterminate cancellation is insufficient. Shared OpenCode services are never
signaled; only proven private services can be stopped. Resume clears the stop
request under the guard. Abnormal return after generation registration retains
stopping ownership; normal release requires no unresolved activity.

Existing SQLite generation records retain Invocation, graph revision/entrypoint,
semantic node, business activation and contract scope, immutable input, increasing
ordinal, Attempt key, owner nonce and abandonment. Registration commits before
dispatch; registration-only crashes consume budget and technical feedback does not
reset it. System/resource waits retain the same Attempt and original input. Human
waits keep graph checkpoints. Explicit abandonment maps interrupted work to a fresh
Attempt; completed nodes still follow graph checkpoints. Generation/phase journals
are counter/dispatch records, not another graph state machine.

Production enables regeneration; low-level Kernel `execute_or_recover` and default
node-factory recovery compatibility remain distinct. Promotion-before-receipt
regressions regenerate real bytes while retaining historical promotion evidence.
No Effect/Intent execution or healing approval stage is restored.

Missing registered persistence, an empty replacement database or missing owned rows
cannot prove cleanup or reset budget, including after ownership was released.
Stop validates real workspace/runtime ancestors and a regular database before any
SQLite access. A missing database is accepted only with no registered generation,
call or child dispatch evidence. Ambiguous legacy ownership/budgets, unverifiable
identity and lost authenticated envelopes refuse execution with a diagnostic;
historical read-only status remains available. Use a fresh isolated diagnostic run
instead of guessing takeover.

Focused evidence covers real process exclusion and termination, native identity
mismatch, delayed old-stop nonce, retained external calls, scoped cleanup, durable
restart/registration-crash budget, system waits and actual filesystem promotion.
Process-stop and promotion regressions are separate experiments; no combined
end-to-end kill experiment across every commit window is claimed. Installed wheel
handlers retain the existing host containment model; no cgroups or generic detached
process tracking was introduced. The acceptance targets below remain requirements,
not a claim that the complete experiment in item 8 has been performed.


## 决定

同一运行状态只能由一个 Worker 执行。同一任务工作目录也不允许不同 Run 的 Worker 同时写入。旧 Worker 尚未退出时，不得启动替代 Worker。

恢复顺序固定为：阻止新启动 → 停止旧执行 → 确认退出 → 清理旧执行的资源占用 → 登记下一次尝试 → 重新生成中断节点的产物。启动互斥覆盖整个过程；尝试次数已用完时，结束并报告失败。

取消“旧 Worker 仍执行，新 Worker 已接管”的正常运行模式。不能依靠租约超时、心跳失联或提高 fencing token，绕过旧进程退出确认。

本修订中的互斥范围是共享运行状态或任务工作目录的 Worker；不同独立工作目录不增加机器级全局锁。状态查询和停止命令可以执行，但不得推进业务图或派发新业务调用。

## 启动与恢复

1. 所有会推进业务的入口，包括前台执行、后台 Worker、bootstrap、resume，使用同一套启动互斥规则。
2. 进入业务执行、创建 Agent 会话或修改工作区前，必须取得执行权。
3. 已有 Worker 存活时，普通启动或恢复返回明确的运行冲突。不得自动杀进程、自动接管或一边等待一边执行业务。
4. 启动检查、启动预留与 Worker 接收执行权必须连成一个互斥过程。不能只读取状态文件后就启动进程。
5. 执行权覆盖 Worker 的整个执行与清理过程。状态写成 terminal 或释放一次 Invocation 调用的锁，都不等于旧进程已经退出。
6. 替代 Worker 启动前，必须确认旧进程身份已退出，并取得互斥锁。两项条件缺一不可。
7. 从图最后一次持久化的完成记录继续。对中断或结果未知的节点，创建新的 Attempt 重新执行，不回到旧 Attempt 内部补收尾。停止进程不会使已写入文件自动回滚。

## 停止旧进程的方法

Product Worker management now exposes `stop_run`; Operator and bootstrap
`stop --force` share it. Ordinary start/resume never invokes force stop implicitly.

该方法执行以下顺序：

1. 在启动互斥保护下标记停止中，阻止新的 Worker 启动。
2. 核对目标进程身份及其 Run／Invocation／工作目录。记录需要包含主机身份、PID、进程创建身份以及本次启动的唯一标识；不得仅按 PID 或进程名称结束进程。
3. 请求旧 Worker 停止派发新活动，取消所属活动，完成可完成的持久化清理，然后退出。
4. 在有界等待时间内未退出时，对身份仍可核实的旧 Worker 及其专属进程组执行终止；必要时升级为强制杀进程。
5. 等待并确认目标进程已退出。发出终止信号不算停止成功。身份不明、没有终止权限或等待超时，都保持新启动被阻止。
6. 确认旧执行不再拥有可以继续写该工作区的子进程或外部活动，再清理它的资源占用。
7. 清理成功后允许后续恢复；清理失败时报告原因，保持业务启动被阻止。

停止入口的结果必须区分“已停止”“仍在停止”“无法确认”。不得用业务 terminal 状态伪造进程退出证明。对历史运行缺少进程身份记录的情况，不猜测目标 PID；保持阻止启动，先核对旧执行是否结束。

## OpenCode 与外部活动

强制结束 AA Worker 后，控制入口仍须核对 journal 中所属活动，必要时取消对应 OpenCode 会话并确认它已结束。这个过程只完成停止与核对，不执行新业务。

共享 OpenCode 服务只能取消本 Run 所属的活动，不能杀掉整个共享服务。独占服务可以随该 Run 停止，但仍需确认相关工作区写入已停止。

仅收到取消请求的确认、网络断开、AA PID 消失，均不能作为外部活动结束的依据。无法确认停止时保持启动门关闭；控制入口只需继续确认活动是否停止。停止已确认后，不要求取回旧调用的业务结果，直接重新生成。

## 旧执行的资源清理

进程退出不会自动删除数据库中的资源占用。统一停止与重启入口负责这一步，不要求恢复旧 Attempt 的完整收尾流程。

1. 确认旧 Worker 及其所属写入活动已停止，并继续持有启动互斥保护。
2. 只释放旧执行所属 Attempt 的资源占用。正常退出时已经释放的占用无需重复处理。
3. 复用现有资源释放记录与数据库事务。收窄现有清理方法的范围；只有已确认数据库全部占用都属于该旧执行时，才能整体清理。
4. 清理可重复调用。如果控制进程在清理后、新生成前退出，下次启动再次检查并继续，不影响其他执行的占用。
5. 清理成功后，新 Attempt 正常申请自己的资源。不得通过删锁文件或清空全部授权来绕过互斥。

## Attempt Runtime 的边界

进程启动、停止与退出确认属于产品 Worker 管理层，不放进业务节点、AgentOp `after` 或 Attempt 的业务 Phase。

Attempt Runtime 继续保留 journal 的 CAS、身份校验、fencing token 和关键操作前的权限检查。这些检查用于拒绝失效上下文和错误写入，不用于允许两个 Worker 同时执行。

本次不增加第二份 Attempt checkpoint，也不恢复已删除的 Effect／Intent 层。业务 Repair 仍由图控制，重试仍受现有预算限制；中断后的重新执行使用新 Attempt，避免命中旧 Attempt 的缓存结果或恢复分支。

## 中断后直接重新生成

1. 外部调用结果未知：停止旧调用，确认不再执行，然后重新调用生成。无需取回原结果。
2. 文件已经写入，但节点完成记录未保存：同样重新生成。无需恢复原 Attempt 只补写完成记录。
3. 新一轮重新生成节点声明范围内的产物，允许覆盖或更新上一轮已经写入的产物。继续执行现有输出校验和写入范围限制；不承诺两次生成的内容完全相同。
4. 已完成节点沿用图的持久化记录；只重跑尚未记录完成的节点。重新生成使用新执行身份和本轮 staging，不把旧的半成品当成本轮成功结果。
5. 接受重复调用、重复生成及相应耗时。验收不再要求中断节点只生成一次。达到现有重试上限后停止，不无限自动重跑。

重新生成不等于撤销旧文件，也不需要将旧执行伪装成 `writes_promoted=False` 的失败。保留已有日志作为历史，新一轮按正常节点路径生成、校验、提交并记录完成。

这项决定替代“必须按提交前／提交后分别恢复同一个 Attempt”的设计要求。进程停止与禁止并行仍是重新生成的前提。

## 跨进程重启的尝试次数

同一个 Invocation、节点和业务激活共用一份已使用次数。技术重试附带的错误反馈不重置次数；图进入新的业务修复轮次仍按原有业务激活规则处理。

每次开始新一轮生成前，在现有持久化存储中登记递增的尝试编号，再用该编号创建新的 Attempt 和本轮 staging。编号同时用于计数和区分新旧执行，不另外建立一套重试状态机。登记失败时不调用 Agent。

重启后读取已登记的次数，使用下一个编号，不能重新从 1 开始，也不能重用旧 Attempt 的结果。登记后发生崩溃，该次仍计入已使用次数。等待当前调用、查询状态或等待人工输入不登记新的尝试。

例如 `max_attempts=3` 表示包括首次执行在内最多 3 次。已经登记 2 次后重启，只剩 1 次；第 3 次也中断后，再次启动直接报告次数已用完，不再调用 Agent。

## 普通顺序执行

范围补充（2026-10-07）：本节简化的是中断后的恢复策略和图调度，不取消 Attempt 内部的职责解耦。原 Spike 要求的“状态推进与领域处理分离”继续有效，详见 [Attempt Runtime 补充设计](2026-10-07-attempt-runtime-separation-design.md)。内部执行器可以选择当前 Attempt 的下一步动作；它不增加图调度器，也不改变以下重新生成和等待规则。

Driver 按当前节点的执行结果处理，不依靠 Phase 是否变化或 journal revision 是否增长来反复调度 handler。

图的节点选择与路由仍由现有 Flow／LangGraph 负责，不增加第二个图调度器。

| 当前情况 | 行为 |
| --- | --- |
| 当前调用尚未返回 | 等待这一次调用，不发起重复生成 |
| 节点成功 | 持久化完成结果，按图的边继续执行 |
| 可重试失败或执行中断 | 停止旧执行、清理资源，检查剩余次数后重新生成 |
| 需要人工输入 | 暂停；收到输入后按图的规则继续 |
| 不可重试失败或次数已用完 | 停止并报告失败 |

现有 Attempt 执行代码负责校验、文件提交和必要记录；Driver 不把它们改成统一的 `execute → persist(result)`。Phase 可以保留为现有记录的派生状态，不额外保存另一份 checkpoint，也不把 `prepared`、`dispatch_started`、`bound` 拆成新的 Driver 调度协议。

## 验收要求

1. 两个真实进程同时申请同一运行状态或同一任务工作目录时，只有一个能进入业务执行；另一个不能派发请求或修改工作区。
2. 旧 Worker 运行期间，所有执行和恢复入口均返回冲突；只读查询及停止入口仍可用。
3. 停止请求发出后至退出确认完成前，新 Worker 始终被阻止。覆盖暂停响应慢、清理耗时和进程忽略温和终止的情况。
4. 强制终止后，只有在旧进程、所属写入活动已结束且互斥锁可获得时，才允许启动替代 Worker。
5. 两个启动或恢复请求同时到达、停止命令重复到达，均不会启动两个 Worker，也不会结束后来启动的新 Worker。
6. Linux 发信号使用核验后的 pidfd，PID 复用不会把信号发送到替代进程；记录指向别的进程、主机不匹配、权限不足、退出未确认或旧记录缺失时，保持闭锁。macOS 重复核验创建身份，但核验与数字 PID 信号之间仍有 TOCTOU 平台限制，不能承诺绝不误杀被复用的 PID。
7. AA Worker 已退出而 OpenCode 会话仍运行、取消仅被确认或取消结果未知时，新 Worker 仍被阻止。
8. 在生成、文件提交和节点完成记录附近强制结束真实旧进程，再串行启动新执行；未记录完成的节点可以重复生成，最终使用新一轮通过校验的产物。旧资源占用不得使新一轮永久阻塞，也不得在旧执行仍存活时提前放行。
9. 旧上下文写入拒绝测试继续保留；删除将两个 Worker 重叠执行业务作为正常接管路径的要求。
10. 最多 3 次的节点在已登记 2 次后重启，只能再生成 1 次；在登记编号后立即崩溃，也不会回退次数或复用旧 Attempt。
11. 强杀后旧资源占用可以被清理，新 Attempt 能正常申请；重复清理无影响，其他执行的占用保持不变，清理失败时不开始新生成。
12. 等待 Agent 或人工输入期间，不重复调用 handler、不增加尝试次数；成功后按图继续，不要求 Phase 变化或增加通用进展事件。

## 替换后的评审第 3 条

**[P1] 明确单 Worker 所有权与先停后恢复协议。**

同一运行状态和任务工作目录不允许并行 Worker。旧进程未退出时，新的执行或恢复请求必须被拒绝。产品层增加显式停止旧进程的方法；需要强制终止时，核对进程身份，终止并确认退出，同时确认所属外部活动不再执行，再允许新的 Worker 重新执行中断节点。现有 CAS 和 fencing 校验保留，但不再承担重叠 Worker 接管的职责。验收使用真实进程，覆盖启动竞争、停止超时、强杀、PID 复用、外部活动未停及串行重新生成。

## 替换后的评审第 4 条

**决定：中断或结果未知时直接重新生成。**

在旧执行停止后，对尚未记录完成的节点创建新的 Attempt，重新生成产物。即使上一轮已写入文件，也允许重新生成并覆盖或更新这些产物。取消“必须取回旧业务结果”和“必须恢复同一个 Attempt 补收尾”的要求，接受重复生成成本。保留单 Worker、节点输出校验、写入范围和重试预算。

## 替换后的评审第 6 条

**决定：使用普通顺序执行，不增加通用推进协议。**

当前调用未结束就等待；成功后按图继续；可重试失败或中断后，确认旧执行停止、清理资源，再按剩余次数重新生成；需要人工输入就暂停。尝试编号跨进程重启保存，不用 Phase 或 journal revision 判断是否应当再次调用 handler。
