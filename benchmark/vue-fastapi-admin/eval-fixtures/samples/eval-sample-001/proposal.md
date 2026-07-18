# Proposal: RET-api-management-20260716-192358-cursor

generation_mode: autonomous

## Why

验证 vue-fastapi-admin API 管理（apis）模块的 CRUD、分页/条件查询、refresh 路由同步与鉴权行为，确保后端 `/api/v1/api/*` 与前端 `web/src/views/system/api/index.vue` 管理流程可用。

## Test Basis

- Requirement ID: RET-api-management
- Feature Name: api-management
- Source: benchmark seed requirement / explore advisory
- Target Module: system.api
- Target Case File: `qa/cases/system/api/case.yaml`

## What to Test

- 后端 6 个 API 管理端点：list、get、create、update、delete、refresh
- Api/ApiCreate/ApiUpdate 元数据字段与 MethodType 枚举校验
- DependPermission 鉴权与 unauthorized 负向
- refresh 与 app.routes（带鉴权依赖）同步行为
- 前端 API 管理页：列表、搜索、新建/编辑、删除、刷新 OpenAPI 注册

## Out of Scope

- 角色-API 授权绑定（由角色管理模块覆盖）
- 非 admin 角色的 E2E 细粒度权限矩阵（API 层 unauthorized 即可）
- Fuzz 输入健壮性（benchmark scope declined）
- Performance 性能阈值（benchmark scope declined）

## Confirmed Coverage Approach

> 选择 API + E2E 冒烟覆盖：12 条 API 用例覆盖 CRUD、分页/过滤、refresh、鉴权与负向；6 条 E2E 用例覆盖管理员 CRUD/refresh 冒烟与非管理员访问拒绝。负向采用保守 4xx/非 5xx 断言；重复 path+method 创建采用中性探测（模型无唯一约束）。

## Test Conditions

| Condition ID | Condition | Source | Priority | Risk Level | Design Technique |
|---|---|---|---|---|---|
| COND-APIS-001 | 验证管理员可成功创建 API 元数据记录 | RET-api-management | P0 | high | use_case |
| COND-APIS-002 | 验证 API 列表支持分页与默认查询 | RET-api-management | P0 | high | use_case |
| COND-APIS-003 | 验证 API 列表支持 path/summary/tags 条件过滤 | RET-api-management | P1 | medium | boundary_value_analysis |
| COND-APIS-004 | 验证按有效 id 可查询 API 详情 | RET-api-management | P1 | medium | use_case |
| COND-APIS-005 | 验证可更新 API 元数据字段 | RET-api-management | P1 | high | state_transition |
| COND-APIS-006 | 验证可删除 API 记录 | RET-api-management | P1 | high | use_case |
| COND-APIS-007 | 验证 refresh 可同步带鉴权依赖的路由注册表 | RET-api-management | P0 | critical | state_transition |
| COND-APIS-008 | 验证未携带有效 token 的请求被拒绝 | RET-api-management | P0 | critical | negative |
| COND-APIS-009 | 验证缺少必填字段时创建请求被拒绝 | RET-api-management | P1 | medium | negative |
| COND-APIS-010 | 验证无效 MethodType 枚举值创建被拒绝 | RET-api-management | P1 | medium | equivalence_partitioning |
| COND-APIS-011 | 验证对不存在 api id 的操作返回合理错误 | RET-api-management | P1 | medium | negative |
| COND-APIS-012 | 验证相同 path+method 重复创建行为可观测 | RET-api-management | P2 | low | exploratory |
| COND-APIS-E2E-001 | 验证管理员可进入 API 管理页面 | RET-api-management | P0 | high | use_case |
| COND-APIS-E2E-002 | 验证管理员新建 API 后列表可见 | RET-api-management | P0 | high | use_case |
| COND-APIS-E2E-003 | 验证管理员可编辑 API 元数据 | RET-api-management | P1 | medium | state_transition |
| COND-APIS-E2E-004 | 验证管理员可删除 API | RET-api-management | P1 | high | use_case |
| COND-APIS-E2E-005 | 验证管理员可触发刷新 OpenAPI 注册 | RET-api-management | P1 | high | use_case |
| COND-APIS-E2E-006 | 验证非管理员无法访问 API 管理 | RET-api-management | P1 | high | negative |

## Quality Risks

| Risk | Likelihood | Impact | Level | Mitigation |
|---|---|---|---|---|
| refresh 误删或不同步导致权限绑定失效 | 4 | 5 | critical | TC_APIS_API_007 + TC_APIS_E2E_005 覆盖同步一致性 |
| 未授权访问暴露 API 元数据管理 | 4 | 5 | critical | TC_APIS_API_008 + TC_APIS_E2E_006 |
| 无 path+method 唯一约束导致重复脏数据 | 2 | 3 | medium | TC_APIS_API_012 中性探测建立基线 |
| 缺字段/非法枚举校验不足 | 3 | 3 | medium | TC_APIS_API_009/010 负向覆盖 |

## Test Types

- API
- E2E

## Test Types Considered

| Layer | Decision | Reason |
|---|---|---|
| API | selected | 6 个 REST 端点 + RBAC，适合请求/响应级断言（SC-ROUTE-001..006, SC-RBAC-001..003） |
| E2E | selected | 存在 system/api 管理页，需求要求 E2E 冒烟（SC-FE-001） |
| Fuzz | declined | benchmark seed 与 test_strategy 明确 declined；无历史 fuzz 失败证据 |
| Performance | declined | 未识别高频/重查询路径；benchmark seed declined Performance |

## Layer Rationale

- TC_APIS_API_001: API — 单请求验证创建成功与持久化
- TC_APIS_API_002: API — 分页列表直接断言响应体字段
- TC_APIS_API_003: API — 过滤参数为 HTTP 查询，API 层足够
- TC_APIS_API_004: API — get 详情为单请求断言
- TC_APIS_API_005: API — update 状态变更通过 API 验证
- TC_APIS_API_006: API — delete 通过 API 验证记录移除
- TC_APIS_API_007: API — refresh 同步行为需验证后端状态
- TC_APIS_API_008: API — 鉴权矩阵适合 API 负向
- TC_APIS_API_009: API — schema 校验为 API 层职责
- TC_APIS_API_010: API — 枚举校验为 API 层职责
- TC_APIS_API_011: API — 无效 id 错误码矩阵
- TC_APIS_API_012: API — 重复创建探测无需 UI
- TC_APIS_E2E_001: E2E — 验证页面入口与操作按钮可见
- TC_APIS_E2E_002: E2E — 验证 UI 新建与列表刷新
- TC_APIS_E2E_003: E2E — 验证编辑弹窗与列表更新
- TC_APIS_E2E_004: E2E — 验证删除确认与列表移除
- TC_APIS_E2E_005: E2E — 验证刷新确认对话框与成功反馈
- TC_APIS_E2E_006: E2E — 验证非管理员 UI 权限拦截

## Explore Input

- advisory: `explore/advisory.json` (status: done, degraded)
- source_code_read: true
- test_strategy: adopted as-is
  - proposal: API + E2E 冒烟覆盖 CRUD、分页/条件查询、refresh 同步与 unauthorized 鉴权；负向采用保守 4xx/非 5xx 断言
  - override: []
- open_questions (assertion-intent, per pitfall): none (explore open_questions_for_case_design 为空)
- adopted: [test_strategy.scope.in_scope, test_strategy.layer_recommendation API+E2E]
- override: []
- gap: [无 qa/cases 基线 — 全部 18 条用例置于 added；role_api_relation_consistency 明确排除]

## Data Needs

- 具备 API 管理权限的管理员账号（superuser 或已授权角色）
- 无 API 管理权限的普通用户账号（E2E 权限拒绝用例）
- 唯一 path + MethodType 组合的 ApiCreate/ApiUpdate 测试载荷
- 专用于删除/编辑的 API 记录（通过 create 或 factory 准备）
- refresh 前后数据库 API 表快照（用于同步一致性断言）

## Success Assertions

- CRUD 操作返回 HTTP 200 且业务码为 200，列表/详情反映预期元数据
- 分页列表包含 data、total、page、page_size
- refresh 后注册路由与数据库记录一致，不在注册表中的记录被清理
- 未授权请求返回 401 或 403，不得 5xx
- E2E 管理页可见列表、新建、刷新入口；操作后出现成功提示且列表更新

## Exception Scenarios

- 缺字段创建、非法 MethodType、无效 api id、未授权访问
- 相同 path+method 重复创建（中性探测，记录实际行为）
- 非管理员 UI 访问拒绝

## Entry Criteria

- Explore advisory 已完成（phases.explore.status == done）
- 目标模块 system.api 已确认
- 无既有 qa/cases 基线，全部用例为新增

## Exit Criteria for This Phase

- `proposal.md` 已写入
- `cases/system/api/case.yaml` 已生成并自审（18 added / 0 modified / 0 removed）
- `trace/minimum-coverage-matrix.yaml` 覆盖全部 MRC（21 covered + 1 skipped_by_scope）
- 每条用例可追溯到 requirement_id 与 test_condition_id
- 准备进入 `aa-case-reviewer`

## Downstream Exit Criteria

- case review 通过后由 `aa-api-plan` / `aa-e2e-plan` 生成 plans
- codegen 与执行后由 `aa-run` / `aa-archive` 更新 traceability
