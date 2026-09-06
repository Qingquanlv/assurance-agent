# 业务规格驱动的 API→DB 验证与业务 Trace 检查点

日期：2026-09-06  
状态：Draft — 待评审的实施规格；本文件不表示功能已经实现  
范围：阶段一「规格、机器计划、权威执行、一个 DB oracle」；阶段二「同一流程增加一个必需业务 OTel 检查点」  
基准项目：当前仓库的 `benchmark/vue-fastapi-admin`  
首条业务流程：`POST /api/v1/dept/create` 创建根部门

## 1. 问题与目标

当前项目能生成业务用例、计划和测试，验证生成文件的路径、摘要及 Case ID 映射，但不能确定性保证每项业务预期被实际检查。用例 assertions 数组非空、pytest 通过以及 Agent 提交结构化结果，都不足以证明进行了有效校验。现有 assertion_strength 主要汇总外部统计，现有 trace.v2 是需求到测试结果的追溯投影，不是分布式遥测。

本功能将以下关系形成机器可验证的闭环：

**业务目标/不变量 → API 动作 → 必需业务检查点及关联约束 → 独立状态断言 → 证据完成条件。**

人可读业务用例是预期的来源；机器计划是派生产物。代码证据用于定位路由、数据库字段和插桩位置，不自动决定什么行为正确。最终结果由已安装执行模块采集的证据和确定性裁判计算。

两阶段均须通过实际 benchmark 验收，不能只完成 schema、提示词或 mock 单元测试。第一阶段不要求 OTel；第二阶段沿用同一业务预期和同一 DB oracle，仅新增冻结的 Trace 义务。

## 2. 范围与方案选择

选择扩展现有 intake、generation、execution、quality、healing 模块，复用受限 subprocess runner、产物声明、版本摘要和图调度。只增加这条执行链必须的数据与执行能力。

比较过的方案：

| 方案 | 判断 |
| --- | --- |
| 仅强化 codegen/reviewer 提示词 | 不满足必需检查实际执行的确定性保证，不采用 |
| 现有模块增加结构化执行义务、独立观察与确定性判定 | 本 spec 采用，第一版限定同步 API 与 SQLite |
| 建设通用 Trace 测试平台或重写图引擎 | 超出两阶段验证所需范围，不采用 |

新模式使用两个显式 profile：`api_db.v1` 与 `api_db_trace.v1`。profile 在计划编译前选择并冻结；执行失败后不得自动从后者降级到前者。

第一版不实现 UI 驱动接入、Redis、Kafka、Fuzz、Performance、跨服务消息拓扑、通用 SQL DSL、APM 管理界面或自动业务规则推断。现有其他测试能力继续工作，但不能被标记为已获得新验证保证。

## 3. Benchmark 事实与业务规格来源

### 3.1 已核查的实现事实

当前 benchmark 使用 FastAPI 0.111.0、Tortoise ORM 0.23.0、aiosqlite 0.20.0 与 SQLite。当前 app 和依赖中没有发现 OTel 插桩，第二阶段需要新增接入。

根部门创建路由先等待 controller，再返回成功响应；响应没有新建实体 ID。controller 的 `create_dept` 使用 `@atomic()`，在事务中先创建 Dept，再建立 DeptClosure 关系。第一版按唯一业务名称关联实体，不为获取 ID 修改公开响应契约。

选择创建根部门而非部门重新挂载，是因为创建流程更短且已有事务；当前重新挂载的闭包更新顺序存在额外业务问题，应由其他测试暴露，本功能不顺带修复它。

现有 benchmark 需求要求验证部门创建正确性，但没有定义闭包表行数或原子性细节。本 spec 明确补充下述最小成功创建验收语义，评审后作为规范来源冻结；这些预期不得伪称由当前实现自动证明。DeptClosure 的物理行形状不进入第一版业务 oracle。

### 3.2 首条人可读业务用例

| 项目 | 冻结的业务约束 |
| --- | --- |
| 目标 | 管理员创建一个此前不存在的根部门，并在操作成功后持久保存输入字段 |
| 初态 | 本次唯一部门名称不存在；合法测试管理员可调用创建接口 |
| 输入 | 每个测试尝试唯一的 name、固定 desc、合法 order、parent_id=0；name 不超过当前 20 字符约束 |
| API 预期 | HTTP 200，响应业务 code=200；不依赖成功文案原样匹配 |
| DB 预期 | 在独立连接读取已提交状态：该 name 恰好一行；name、desc、order、parent_id 与本次冻结输入相等，is_deleted=false |
| Trace 预期 | 仅阶段二要求：同一 API 动作下观察到 `dept.create.committed` 检查点，与同一业务名称、SUT 实例关联 |
| 完成 | 同步 API 动作已有终态；独立 DB 查询完成；阶段二还需必需 Trace 证据到达并完成采集封存 |

name 由执行模块生成不超过 20 字符的运行标记。实例绑定冻结前最多尝试分配 3 次以避开碰撞；选定后写入本次实例绑定并冻结。冻结后的初态检查若发现同名记录，必须阻止动作，不能再悄悄换名或复用记录。每个参数实例和显式重试使用新的名称。

DB 的预期是明确的业务字段及存在性，不包括固定主键值、生成时间、ORM 方法名、SQL 文本或 SQL 调用次数。字段与表的对应关系属于实现绑定，可以在保持业务语义的重构后更新。

## 4. 用户故事

1. 作为用例作者，我希望用业务语言表达创建目标和持久化预期，使规格不绑定 ORM 实现。
2. 作为评审者，我希望知道每条预期来自哪条需求和哪个规格版本，避免把现存缺陷当成正确行为。
3. 作为测试生成者，我希望从冻结规格得到明确动作和 oracle 绑定，使必需检查无法被遗漏。
4. 作为执行者，我希望 API 请求与数据库查询属于同一个受控 SUT 实例，避免读错数据库。
5. 作为使用者，我希望成功响应但未落库时得到失败，而不是通过。
6. 作为使用者，我希望 DB 读取失败或必需检查未执行时得到不完整结论，而不是误报业务正确。
7. 作为使用者，我希望 Trace 缺失与业务失败被区分，便于判断应修复业务还是证据采集。
8. 作为维护者，我希望正常重构函数、SQL 或辅助 spans 时测试继续通过，只约束已约定业务语义。
9. 作为恢复执行的操作者，我希望重复运行和重试不会使用上一轮证据，也不会不知情地重复提交创建请求。
10. 作为质量负责人，我希望覆盖统计来自逐项实际证据，且缺失、跳过不能缩小分母。
11. 作为修复 Agent 的使用者，我希望自动修复不能通过删除 oracle 或放宽预期来获得通过。
12. 作为现有项目使用者，我希望旧测试保持可运行，同时清楚区分旧结果与新契约的验证保证。

## 5. 术语与权威

| 术语 | 定义与权威 |
| --- | --- |
| Business Case | 人可读、经评审的业务规格，拥有预期与业务不变量 |
| Obligation | 必须被验证的一项义务；有稳定 ID，可对应动作、状态断言或检查点 |
| Case Execution Plan | 对业务规格的派生执行绑定；不是现有 Agent runtime execution-contracts 配置 |
| Oracle | 已安装的观察/比较能力；实际值来自观察，预期引用冻结业务规格 |
| Run / Case Instance / Test Attempt / Action | 一次实际运行、具体参数组合、真实测试尝试、单个动作；与引擎 task attempt 建立关联但不混用 |
| Evidence | 执行模块采集并保存的 receipt、观察值及原始材料，不等同于测试或 SUT 自报成功 |
| Verdict | 质量模块依据冻结契约与证据计算的结果；Agent 叙述不覆盖它 |

本功能的信任范围是受控本地 benchmark 与已安装的执行/观察代码，不提供针对恶意 SUT 的密码学执行证明。文件摘要证明材料身份和版本一致性，不能证明业务行为正确。

## 6. 规格、来源与机器计划契约

### 6.1 业务规格与来源

新模式将业务断言升级为带稳定 assertion/obligation ID 的明确结构。每条断言包含可读 statement、有限类型的预期及比较语义；不得以空字符串、空对象、任意散文或代码表达式替代执行义务。

保留 case.yaml 的业务内容。新增正式的预期来源记录，按 case/assertion ID 关联需求引用、规范版本、决策状态和评审引用；不把 Explore 的全部 advisory 元数据复制到 case。来源记录和 case 内容由运行时计算摘要并冻结。

规范来源与 implementation references 分开。源码观察、正常运行录制或 `source_verification` 不得单独将候选预期升级为 ready。未确定的预期使该用例计划 `NOT_READY`，不能由 codegen 猜测 expected。

### 6.2 机器计划

`CaseExecutionPlanV1` 是新增的机器契约，使用 JSON、闭合 schema 和运行时确定性序列化。至少包含：

| 字段组 | 必需内容 |
| --- | --- |
| 规格绑定 | case ID、规格引用/摘要、预期来源摘要、选定 profile、义务目录摘要 |
| 实现绑定 | SUT 版本、支持的动作/observer/checkpoint ID 与版本、配置摘要 |
| 实例输入 | 参数声明和来源、运行期允许绑定的唯一业务名称；禁止绑定时改写业务预期 |
| 动作 | action ID、已安装的 HTTP driver、method/path、请求参数、响应预期 ID |
| DB oracle | oracle ID、初态检查、已安装 SQLite observer、参数化只读查询绑定、结果比较和预期 ID |
| Trace | 阶段二的必需检查点 ID、语义版本、关联约束；阶段一明确 not_required |
| 完成 | 同步动作语义、各等待上限、证据完成要求、缺失处理 |
| 闭集映射 | 每项必需义务到一个受支持执行绑定；关联现有 Case ID → test symbol/file mapping |

第一版支持的比较能力限于 exact row cardinality、字段相等、HTTP 状态与 envelope code；不提供任意 Python/CEL 插件执行。SQL 绑定必须为经校验的参数化只读查询，表/列来自固定适配，不允许在参数里注入 SQL。

机械校验必须拒绝：缺失或重复义务、悬空 expected ID、规格摘要不匹配、空必需集合、未知 observer、缺少必需 binding、profile 与 Trace required 冲突。计划遗漏在执行前得到 `NOT_READY`，不启动业务动作。

codegen 输出仍是 raw tests 及映射；执行契约、来源记录等机器产物遵循现有 typed artifact 声明和 materialization 机制。生成器不得修改冻结的业务预期。源码静态检查可补充明显错误提示，但 assert 个数不是有效性判据。

## 7. 权威执行、身份与环境

### 7.1 测试与执行模块的职责

复用现有受限 subprocess runner，在新 profile 的产品执行链上将它接为权威执行路径。Agent 负责生成与解释，不能通过 structured_result、pytest exit=0 或手写 receipt 宣告义务已完成。

生成的 pytest 入口通过已安装的执行桥接调用指定计划。桥接是通往父级 execution host 的窄 IPC，不是在同一 pytest 进程中直接运行 observer 并把返回字典当权威证据。父级 host 持有冻结计划、实际 HTTP driver、DB observer 和证据目录；子进程只能请求执行已分配的计划/实例，不能传入新的 SQL、expected、完成状态或 evidence 路径。IPC 请求绑定当前子进程会话与 attempt，并拒绝重复动作请求。若测试入口没有调用桥接，所选义务缺少记录，结果为 `INCOMPLETE`。

允许一个很薄、没有显式 Python assert 的入口在执行模块实际完成全部义务后通过。禁止将 `pass`/`assert True` 的语法形状直接等同于失败，也禁止只因 pytest 通过而补写 oracle 完成记录。

pytest 原始报告先作为子进程输出接纳，只证明测试收集与 runner outcome；它不是业务 oracle 的权威结论。动作 receipt、DB 观察、Trace 原始材料的接纳和最终证据清单由父级执行模块控制。权威存储在子进程可写集合之外，测试/Agent 自写的同名文件不进入接纳路径。执行后端必须验证并实施此进程/写集隔离；无法提供时新 profile 为 NOT_READY，不能以目录命名约定冒充隔离。重复或冲突证据按身份与摘要检测，不能采用“最后写入覆盖”。

### 7.2 身份

每次真实运行生成新的 run ID；保留具体参数化 nodeid，不只聚合到函数。身份链为 run → case instance → test attempt → action，并记录引擎 invocation/task/attempt。

当前 batch ID 是输入内容摘要，继续用于内容身份；不得充当唯一运行 ID。每条动作、DB 观察、Trace 关联都绑定本次实例、尝试和业务名称，旧尝试证据不能补齐新尝试。

第一版不自动重试业务 POST。动作已开始但终态 receipt 丢失时，不根据缺失结果直接重新发送请求；先记录不完整。用户或调度策略发起的新尝试必须使用新的身份、名称和受控环境。恢复仅可复用同一身份下已落盘且匹配摘要的证据。

### 7.3 SUT 与 SQLite 隔离

复用 benchmark 的 run-scoped runtime 副本思路，每个验证尝试启动独占 managed SUT，并绑定监听地址、进程/实例 ID、实际 SQLite 绝对路径、代码/配置摘要。不得仅因为某个端口的服务 ready 就复用未知实例。

执行者和 SQLite observer 必须确认同一个环境绑定。复制初始数据库应使用 SQLite 一致性备份或已停止的种子库，不能忽略 WAL 后直接复制活跃主文件。不得读取或修改 benchmark 原始数据库来完成业务动作。

初始准备只提供管理员和必要基础数据，不预先创建目标部门。独立只读连接先确认本次 name 不存在，API 动作后重新读取已提交状态；不得复用 SUT ORM session、未提交事务或响应缓存作为 DB oracle。

环境不匹配、初态不满足、数据库不可读均不得运行或宣称通过；归为执行环境证据错误，不自动生成业务缺陷。证据先封存再清理；失败/不完整尝试保留诊断材料，清理不得篡改判定。

## 8. 阶段一：独立 DB oracle

第一版实现一个 SQLite observer 能力，承担本用例初态与后态读取。初态须确认目标 name 为零行；后态查询必须返回原始有限 rowset，再由固定比较器检查恰好一行与字段值。

后态读取发生在同步 HTTP 动作已有终态之后。成功响应承诺已提交，因此目标记录不存在或字段错误是业务失败；不通过反复等待把同步提交错误掩盖成最终成功。

HTTP 超时且执行是否完成未知时，不把当时查不到行直接解释为业务未创建；记录动作未收敛及相关观察，最终 `INCOMPLETE`。查询抛错或超时也与“查询成功且返回零行”区分。

每项观察保存：oracle/obligation ID、执行身份、环境绑定、读取时刻、查询绑定摘要、实际 rowset/受控引用、执行结果与错误原因。预期从冻结规格读取，不从 DB 当前值、Trace 属性或模型输出回填。

阶段一通过的声明仅为「API 动作与 DB 业务契约已验证」，Trace 状态为 not_required，不显示“完整链路已验证”。

## 9. 阶段二：一个业务 OTel 检查点

### 9.1 接入方式

按 benchmark 的 Python/FastAPI 技术栈接入 OTel Python SDK、FastAPI instrumentation 与 OTLP HTTP exporter。第一版不依赖 Tortoise/aiosqlite 自动 SQL 插桩；DB 正确性仍由阶段一 observer 验证。

权威 HTTP driver 创建本次动作的 client span 并通过 W3C trace context 注入请求。FastAPI 创建 server span；受控请求 hook 仅传播允许的测试身份字段。业务检查点在同一请求上下文中产生。测试身份不是业务预期，不能把 expected 值或通过结论注入 SUT。

第一版采用运行期独占的上游 OpenTelemetry Collector，接收 OTLP HTTP 并通过 file exporter 保存原始 OTLP JSONL。执行模块读取并归一化；不自建 APM 查询服务，不以 debug 日志文本作为 Trace 数据接口。

该 exporter 当前为 alpha，因此 SDK/instrumentation/Collector 必须锁定具体版本及 Collector 制品摘要，以样本兼容测试验证 JSONL 解析；运行时版本不支持则预检失败，不静默跳过。file exporter 的原始格式不是本产品的业务契约，产品只公开自己的版本化归一模型。每次尝试使用新文件，不复用旧内容。

### 9.2 检查点语义

唯一新增的业务检查点类型为 `dept.create.committed`，语义版本为 1。它表达当前创建事务已成功返回给请求处理层，不声称所有业务字段正确。

它必须在路由等待 `dept_controller.create_dept(...)` 正常返回之后产生，或在重构后的等价提交后位置产生。不能放在 `@atomic()` 函数体尾部就标记 committed，因为此时事务可能尚未退出提交。

记录稳定检查点 ID/版本、实际业务 name、parent_id、动作关联及 SUT instance。不改变公开创建响应，不依赖新增实体 ID，也不把 Python 函数名作为稳定检查点身份。

匹配条件为：来自本次绑定的 SUT 实例、同一业务名称与 action，且通过实际父子祖先关系关联到该 HTTP 动作。仅业务名称相同、仅时间相近或仅拥有某个旧 trace ID 都不够。允许中间增加辅助 spans；不要求完整树形状、固定直接父节点、SQL 文本或 span 总数。

要求至少一个去重后的匹配检查点，去重按 trace ID/span ID；第一版不以 span 次数证明 exactly-once 业务效果。SpanStatus.OK 或 committed 属性不能覆盖 DB oracle 的失败。

### 9.3 采集完成

测试流量配置全量采样，仍需检测导出/读取错误。原始证据保留 resource attributes、spans、events、links 与丢弃计数等可用信息；第一版仅断言本同步流程的必要关系，未实现的消息 links 裁判不得宣称支持。

API 终态、DB 观察与 telemetry drain 是不同检查点。第一版在动作终态结束 HTTP driver 的 client span；随后在同一完成预算内排空 driver 所属 TracerProvider，并通过 managed SUT 的受控关闭生命周期排空 SUT 的 TracerProvider，两侧都执行适用的 flush/shutdown。两侧完成或得到明确失败/超时后，才有序停止 Collector 并封存文件。执行模块分别记录 driver、SUT、Collector 的完成/失败 receipt；任一必需排空步骤失败不得宣称证据完整。仅等待固定 sleep 或只看“最近无新 span”不能代替该过程。

封存前归一化必须排除旧尝试/其他动作的证据。缺少 server/业务检查点关联、必需字段丢失、文件截断或 drain 超时均不得补成完整。晚到证据不能直接覆盖已封存结果；重算必须显式产生同一原始材料集合对应的新评价记录，或启动新尝试。

“完整”仅指冻结契约所需的正向证据齐全且收集过程成功，不是所有真实操作均被无损观测的全局承诺。

## 10. 完成、状态和产品门禁

默认配置为 HTTP 动作上限 10 秒、独立 DB 读取上限 2 秒、动作与 DB 观察结束后的 telemetry 完成预算 10 秒。它们是本地验证等待边界，不是业务性能 SLO。实施可通过已声明配置改变预算，但编译后冻结并进入计划摘要，healing 不可临时放宽。

| 情况 | 最终处理 |
| --- | --- |
| 计划缺预期、必需 binding 或支持能力 | `NOT_READY`，不启动动作 |
| 全部必需业务义务实际完成并满足，必要证据齐全 | `PASSED` |
| 可靠观察证明响应/已提交状态/必需业务条件违反规格 | `FAILED`；允许同时记录其他证据不完整 |
| 未执行必需动作/oracle，或证据缺失、采集/读取失败、终态未知 | `INCOMPLETE`，不能转为通过 |
| 明确跳过且没有执行 | `SKIPPED`，不贡献验证覆盖 |

判定保留业务维度 satisfied/violated/unknown，以及证据维度 complete/missing/error/timeout；已确认业务违反优先于 unknown，不被遥测故障掩盖。单纯 checkpoint 未收到不能据此断定业务未执行。

quality 从冻结计划与执行证据纯计算结果，策略只决定后续路由。INCOMPLETE 是有效的执行事实，不应因为它不是业务通过而拒绝保存。格式/身份错误拒绝证据接纳，并报告明确完整性原因。

产品的质量门禁、状态页、问题分类、导出和 achieved 判定都消费新结果。选中新模式用例存在 NOT_READY/INCOMPLETE/SKIPPED 或 FAILED 时，不得宣称该验证目标 achieved；诊断材料可以导出，但须保留未验证/失败状态。

## 11. 产物、模块归属与修复约束

最少新增三组正式契约：预期来源记录、机器执行计划、逐项 oracle 执行证据。现有执行结果/报告按版本演进引用这些材料，不另建第二套调度状态机。

| 模块 | 本次职责 |
| --- | --- |
| intake | 业务断言 ID、规范来源与评审状态；冻结可读业务预期 |
| generation | 派生机器计划、闭集义务绑定与 readiness；生成 pytest 桥接入口 |
| execution | 权威 subprocess/HTTP driver、SQLite observer、实例身份、Collector 生命周期和证据封存 |
| quality | 确定性业务与证据判定、义务覆盖及新状态投影 |
| healing | 检测并拒绝验收义务降级，允许不改变语义的执行绑定修复 |
| assurance-product | 安装声明、profile 配置、工作流接线、状态/导出/achieved 集成 |
| benchmark | 明确的业务样例、独占 SUT 环境、业务插桩和验证故障变体 |

`.aa/` 仅保存声明式绑定与策略，扩展闭合配置 schema；observer、validator、gate 等能力由已安装 wheels 提供。不得扫描 SUT 加载任意 Python 插件。

业务插桩是独立受控的 benchmark/SUT 接入改动，不能藏在禁止修改 product source 的 test codegen 中。normal 与故障变体使用显式不同的运行绑定并保存制品/配置摘要。

healing 必须直接对照冻结契约，拒绝修改 expected、比较器、初态、required、profile、身份关联、完成条件或规格摘要来获得通过。修改业务规格必须新 revision 并重新评审/编译。函数定位、SQL 字段绑定等技术修复可进行，但仍须重新验证同一业务语义。

trace.v2 保持需求—用例—测试—问题追溯用途，新增材料通过 evidence refs 关联。覆盖统计从冻结的义务集合和实际记录计算，区分已执行、已判定、已满足；已判定失败仍是“已验证但违反”，不是“未验证”。重试/缺失/跳过不得缩小分母，零分母不产生 100%。

## 12. 验收测试

优先通过最高层的「冻结计划 → managed benchmark 执行 → 权威证据 → 最终 verdict」验证行为。复用已有 execution host seam 和结果映射测试；对裁判使用同一公共评价入口。内部函数调用次数不作为本功能验收。

### 12.1 必需验收矩阵

| ID | 场景 | 必须观察到的结果 |
| --- | --- | --- |
| A01 | 阶段一正常创建 | API 与 DB oracle 均满足，PASSED，Trace 为 not_required |
| A02 | 从计划遗漏必需 DB binding 或 expected 引用 | 编译 NOT_READY，没有 HTTP 动作 receipt |
| A03 | pytest 通过但没有触发计划执行 | INCOMPLETE，必需动作/oracle 未执行，不补写完成记录 |
| A04 | 动作执行后跳过必需 DB observer | INCOMPLETE，即使 HTTP 200/pytest exit 0 |
| A05 | HTTP 正常成功但持久字段错误 | DB 比较给出实际/预期差异，FAILED |
| A06 | 事务真实写入后回滚 | 同步正向创建规格不成立，FAILED；独立 DB 连接确认目标记录未提交 |
| A07 | 真实回滚后故障变体仍返回成功 envelope | HTTP 检查满足，DB oracle 仍 FAILED；不能只依赖 HTTP 错误发现问题 |
| A08 | DB 不可读或 observer 超时 | INCOMPLETE，不当成零行成功读取 |
| A09 | 冻结实例的目标名称已存在或服务/数据库绑定不一致 | 动作前阻止执行，记录环境原因，不能读取旧行获得通过；冻结前的有限碰撞重分配另作准备逻辑测试 |
| A10 | 阶段二正常创建 | 同一 DB oracle 满足、匹配提交后检查点存在且证据封存成功，PASSED |
| A11 | 阶段二丢弃业务 span 或导出链路失败 | DB 可满足，但 Trace 证据不完整，INCOMPLETE |
| A12 | 使用旧 attempt 的 span 或中断 HTTP context 传播 | 不满足当前关联约束，INCOMPLETE |
| A13 | 在实际回滚前错误发出 committed span | 即使 span 存在，DB oracle 仍使正向创建 FAILED |
| A14 | 改函数名、提取 helper、改变等价 ORM 写法、增加辅助 spans | 保持业务规格摘要与检查点语义不变，针对新 SUT 制品重新校验绑定并编译新计划后仍 PASSED；旧制品摘要绑定不得直接复用 |
| A15 | 删除 oracle、required 改 optional、放宽预期/等待条件 | 修复提交或重用计划被拒绝，不能以此获得通过 |
| A16 | 同版本再次执行 | 新 run/attempt/业务名称；旧证据不可补齐新尝试 |
| A17 | HTTP 终态未知或执行中断 | INCOMPLETE；恢复不得隐式重复 POST |
| A18 | Agent 手写全通过结果、测试写入伪造证据、权威材料缺失/被更改 | 验证 IPC/写集隔离与父级接纳路径，不接受自报结果；不能仅凭可同时改写的文件及摘要通过 |
| A19 | 薄 pytest 入口没有显式 assert，但权威执行完成全部义务 | 合法 PASSED；证明不以 assert 语法计数代替有效性 |
| A20 | 第二阶段失败后尝试降级或套用 legacy 结果 | 不能满足原 profile，也不能显示原目标 achieved |

### 12.2 故障变体与回滚的准确含义

回滚变体在真实 `@atomic` 内、首次 Dept 写入之后、关系写入完成之前注入指定异常，必须确实执行数据库写入后再回滚，不能在发请求前抛错替代。

故障注入由版本化、固定的 benchmark 验证 harness 在运行副本中装配，不给普通 HTTP 请求增加任意 failpoint 参数，也不允许项目配置加载 Python 插件。A07 在事务退出已回滚后，由验证变体返回原成功 envelope；它不修改正常产品行为。

单独验证故障 harness 的事务回滚事实，和执行正向业务规格是两个判定：harness 能正确制造回滚，其自身验证通过；正向创建目标未持久化，业务用例仍失败。本 spec 不规定所有 rollback 都失败；未来若规格要求拒绝且不写入，回滚可以满足该负向规格。

### 12.3 阶段交付门槛

阶段一完成 A01–A09、A15–A19 的适用部分，并通过正常产品执行链验证新状态不会被 exit code 覆盖。阶段二在同一业务用例上完成全部矩阵，包含真实 OTLP/Collector 文件采集和故障变体，不能用人工构造 span JSON 替代端到端验收。

每次验收保存规格/计划摘要、环境绑定、原始动作与 DB 证据、阶段二原始 OTLP、收集关闭 receipt 和最终评价。测试执行耗时、接入改动和误报记录进入结果摘要，不预设未经测量的性能提升。

## 13. 兼容、非目标与交付物

旧 case/plan/result 继续按既有版本读取；新 profile 必须经过显式迁移补齐断言来源与义务，不从旧 `passed`、`covered` 或 strong tally 自动补全。旧结果标识 legacy/unverified，不贡献新契约的验证覆盖。

不包含：修复部门 reparent 已知问题、重写整个 Explore、构造任意业务 DSL、全量 SQL/函数覆盖、生产部署、远程 APM 支持、跨 DB 支持、恶意 SUT 证明，以及 UI/Redis/Kafka/Fuzz/Performance 扩展。

实施交付物为：版本化规格/计划/证据契约，现有模块内的执行与裁判接线，一个 SQLite oracle，一个受控 benchmark OTel 检查点，正常/故障验证 harness，产品门禁与导出兼容，以及可复现实验材料。任何一项只有文档或提示词而没有实际执行验收，都不能视为本功能完成。

## 14. 依据与既有约束

以下是制定本 spec 时核对的资料；本地源码只支持实现位置和现状判断。外部 OTel 文档只支持接入可行性，不为部门业务预期提供依据。

- [当前缺口分析](/Users/lvqingquan/agent/assurance-agent/docs/research/2026-09-06-business-spec-execution-contract-gaps.md)、[空校验审查](/Users/lvqingquan/agent/assurance-agent/docs/research/2026-09-06-generated-tests-oracle-audit.md)。
- [部门业务需求](/Users/lvqingquan/agent/assurance-agent/benchmark/vue-fastapi-admin/benchmark/requirements/dept-management.md:10)、[创建路由](/Users/lvqingquan/agent/assurance-agent/benchmark/vue-fastapi-admin/app/api/v1/depts/depts.py:27)、[事务实现](/Users/lvqingquan/agent/assurance-agent/benchmark/vue-fastapi-admin/app/controllers/dept.py:56)、[Dept 模型](/Users/lvqingquan/agent/assurance-agent/benchmark/vue-fastapi-admin/app/models/admin.py:62)。
- [benchmark 环境准备](/Users/lvqingquan/agent/assurance-agent/benchmark/vue-fastapi-admin/benchmark/run-workflow-loop.sh:166)、[当前 ready 服务复用](/Users/lvqingquan/agent/assurance-agent/benchmark/vue-fastapi-admin/benchmark/run-workflow-loop.sh:206)、[依赖版本](/Users/lvqingquan/agent/assurance-agent/benchmark/vue-fastapi-admin/pyproject.toml:10)。
- [代码生成写入边界](/Users/lvqingquan/agent/assurance-agent/packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-api-codegen/SKILL.md:55)、[当前执行 finalize](/Users/lvqingquan/agent/assurance-agent/packages/capabilities/assurance-execution/assurance_execution/operations/agent_skills.py:369)、[安装式扩展规则](/Users/lvqingquan/agent/assurance-agent/AGENTS.md:33)。
- [结构化产物流水线规格](/Users/lvqingquan/agent/assurance-agent/docs/superpowers/specs/2026-09-01-structured-artifact-pipeline-design.md)：新机器材料遵循 typed artifact 与 runtime materialization，业务语义检查不被形状检查替代。
- [OTel FastAPI instrumentation](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/fastapi/fastapi.html)：支持请求插桩与 hooks。
- [OTel Python exporters](https://opentelemetry.io/docs/languages/python/exporters/)：提供 OTLP 导出与 Collector 接入。
- [Collector file exporter](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/fileexporter/README.md)：支持逐行 JSON 导出；当前为 alpha，格式兼容需绑定版本验证。
