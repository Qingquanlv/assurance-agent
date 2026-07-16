# API Test Data Plan — RET-api-management-20260716-192358-cursor

## Scope

Cases requiring data setup: TC_APIS_API_003–007 (factory-seeded Api rows), TC_APIS_API_001/012 (HTTP create in test body). TC_APIS_API_002 needs no seed (empty list acceptable). TC_APIS_API_008–011 require auth personas only or invalid payloads with no persistence.

## Required Data

| Entity | State | Capability |
|--------|-------|------------|
| Api | single test row | `make_api` / `factory_make_api` |
| Api | filter bundle (path/summary/tags tokens) | `make_api` × 1 with distinctive tokens |
| Api | ephemeral delete target | `make_api` (`ephemeral_api`) |
| Api | orphan row (refresh negative) | `make_api` with path not in `app.routes` |
| Api | duplicate probe row | HTTP create (TC_APIS_API_012 primary) |
| Auth routes | runtime registry snapshot | `list_auth_routes` / `factory_list_auth_routes` |
| Auth | admin JWT | `login_token` + `settings` |
| Auth | limited-role user (no api permissions) | `limited_role_user_token` |
| Role | limited (empty api_infos) | HTTP role create + `api_infos=[]` (via conftest) |

## Preconditions

From `case.yaml`:

- 具备 API 管理权限的管理员账号 — `admin_token` / `admin_headers` session fixtures
- 目标 path+method 组合尚未被占用 — unique paths via `api_create_payload()` / `settings.api_prefix`
- 存在可匹配与不匹配过滤条件的 API 记录 — `apis_for_filter` fixture
- 目标 API 记录已存在 — `existing_api`, `factory_make_api`
- 专用于删除的 API 记录 — `ephemeral_api` fixture
- 应用已加载带鉴权依赖的 APIRoute — live SUT running
- refresh 前数据库快照 — list query before refresh (TC_APIS_API_007)
- 无 token / 普通用户 token — empty headers / `limited_role_user_token`

## Capability Mapping

| Need | Capability | Source | Status |
|------|------------|--------|--------|
| Create Api (setup) | `make_api` | `tests/testdata/domain/api.py` | found |
| Cleanup Api | `cleanup_api` | `tests/testdata/domain/api.py` | found |
| List auth routes | `list_auth_routes` | `tests/testdata/domain/api.py` | found |
| API adapter seed | `factory_make_api` / `factory_cleanup_api` / `factory_list_auth_routes` | `tests/api/adapters/api.py` | warning — make/cleanup use HTTP not isolated_worker |
| Admin auth | `login_token`, `admin_headers` | `tests/api/conftest.py` | found |
| Limited user auth | `limited_role_user_token` | `tests/api/conftest.py` | found |
| Api payloads / list helpers | `api_create_payload`, `find_api_in_list`, `count_apis_matching`, `count_apis_with_method_path` | `tests/helpers/api_assertions.py` | found |
| Filter bundle fixtures | `existing_api`, `ephemeral_api`, `apis_for_filter` | `tests/api/conftest.py` | found |
| Runtime settings | `settings.api_prefix`, `base_url`, credentials | `tests/config.py` | found |
| Formal `.aa/data-knowledge.yaml` api entity | `capabilities.domain_factories.api`, `capabilities.adapters.api` | `.aa/data-knowledge.yaml` | found |

## Factory / Boundary Strategy

### Per-entity seeding

| Entity | Ring | Preferred method | Notes |
|--------|------|------------------|-------|
| Api | Within domain | `make_api()` via `factory_make_api` | M2M `role_apis`; cleanup clears M2M first |
| Auth routes (read-only) | Within domain | `list_auth_routes()` via `factory_list_auth_routes` | Reads `app.routes` with dependencies; isolated_worker |
| Role (limited user) | Cross domain | role create + `api_infos=[]` + user create | Only for TC_APIS_API_008; via `limited_role_user_token` conftest |

### Factory usage contract

1. Setup uses factory through `tests/api/adapters/api.py`; test body uses HTTP.
2. `make_api` calls `api_controller.create` — no controller copy in tests.
3. Factories return plain dict snapshots (`id`, `path`, `method`, `summary`, `tags`); fixtures yield snapshots only.
4. Shared factories never own sync/async bridging; API adapter should use `run_isolated` per data-knowledge.
5. **Create cases (001, 012)** exercise HTTP `POST /create`; factories used for preconditions (003–007).
6. Cleanup via `cleanup_api` → clears `role_apis` M2M then deletes Api row.
7. Config from `tests.config.settings` only (`api_prefix` default `qa-api`).

### HTTP API seeding boundary

| Case | HTTP create in test body | Factory for setup |
|------|--------------------------|-------------------|
| TC_APIS_API_001 | Yes (primary happy path) | None |
| TC_APIS_API_002 | No | None |
| TC_APIS_API_003 | No | `apis_for_filter` |
| TC_APIS_API_004, 005 | No | `existing_api` |
| TC_APIS_API_006 | No | `ephemeral_api` |
| TC_APIS_API_007 | No | HTTP create orphan row in test (path not in routes) |
| TC_APIS_API_008 | Yes (create leg for limited user negative) | None for negative legs |
| TC_APIS_API_009, 010 | Yes (invalid payloads) | None |
| TC_APIS_API_011 | No (invalid id only) | None |
| TC_APIS_API_012 | Yes (duplicate create probe) | First row HTTP create |

**Forbidden:** using `POST /api/create` in fixtures for non-create cases (003–006).

## Setup Strategy

| Case ID | Ordered setup |
|---------|---------------|
| TC_APIS_API_001 | `api_create_payload()` → HTTP create → list/get verify → `factory_cleanup_api` |
| TC_APIS_API_002 | `admin_headers` only; `page=1`, `page_size=10` |
| TC_APIS_API_003 | `apis_for_filter` fixture → list with path/summary/tags params → no-match keyword probe |
| TC_APIS_API_004 | `existing_api` fixture → HTTP get by id |
| TC_APIS_API_005 | `existing_api` → HTTP update summary/tags → get verify |
| TC_APIS_API_006 | `ephemeral_api` fixture → HTTP delete → list/get verify absent |
| TC_APIS_API_007 | HTTP create orphan row (`orphan_api_path()`) → record before state → HTTP refresh → verify orphan gone + auth routes synced |
| TC_APIS_API_008 | no headers / `limited_role_user_token` / `admin_headers` on list + create |
| TC_APIS_API_009 | `admin_headers`; invalid JSON via `api_create_payload(omit=...)` for path/method/tags |
| TC_APIS_API_010 | `admin_headers`; payload with `method="INVALID"` |
| TC_APIS_API_011 | `admin_headers`; id `999999999` for get, update, delete |
| TC_APIS_API_012 | HTTP create first row → HTTP duplicate create same path+method → `count_apis_with_method_path` probe |

## Runtime Verify Strategy

Before API calls in seeded cases:

- `existing_api` / `apis_for_filter` / `ephemeral_api` fixtures verify factory returned `id`, `path`, `method` snapshot.
- TC_APIS_API_007: confirm orphan row visible in list before refresh; confirm orphan path not in `factory_list_auth_routes()`.
- TC_APIS_API_009/010: `count_apis_matching` before/after to prove no row created.

## Cleanup Strategy

| Case ID | Cleanup |
|---------|---------|
| TC_APIS_API_001 | `factory_cleanup_api(api_id)` in `finally` |
| TC_APIS_API_002 | none |
| TC_APIS_API_003 | `apis_for_filter` fixture finalizer |
| TC_APIS_API_004, 005 | `existing_api` fixture finalizer |
| TC_APIS_API_006 | delete assertion; fixture finalizer as fallback |
| TC_APIS_API_007 | delete orphan if still present in `finally` |
| TC_APIS_API_008–011 | none |
| TC_APIS_API_012 | `factory_cleanup_api` for all probe ids in `finally` |

## No Data Required Cases

- TC_APIS_API_002 — list pagination (empty list acceptable)
- TC_APIS_API_008 — auth-only negative probes
- TC_APIS_API_009, 010 — invalid payload rejection
- TC_APIS_API_011 — nonexistent id operations

## Blockers / Assumptions

- Assumes live SUT at `settings.base_url` with admin superuser seeded (`admin` / `123456` per fact-baseline).
- Assumes `QA_SQLITE_FILE` alignment for isolated_worker when adapter is refactored.
- No blockers for plan stage.

## Review Checklist

- [ ] Confirm `factory_make_api` refactor to isolated_worker before codegen
- [ ] Confirm TC_APIS_API_009 field omission set (summary default may skip validation)
- [ ] Confirm TC_APIS_API_012 neutral probe documents product duplicate behavior
- [ ] Confirm TC_APIS_API_008 accepts 401 or 422 for missing token
- [ ] Confirm TC_APIS_API_011 includes update leg for nonexistent id
