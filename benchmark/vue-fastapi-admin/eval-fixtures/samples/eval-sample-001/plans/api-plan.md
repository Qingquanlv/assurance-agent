# API Test Plan — RET-api-management-20260716-192358-cursor

## Source

- `qa/changes/RET-api-management-20260716-192358-cursor/cases/system/api/case.yaml`
- `qa/changes/RET-api-management-20260716-192358-cursor/proposal.md`
- Backend confirmation: `app/api/v1/apis/apis.py`, `app/api/v1/__init__.py` (prefix `/api`, `DependPermission`)
- Controller: `app/controllers/api.py` (`refresh_api`, CRUD)
- Schemas: `app/schemas/apis.py` (`ApiCreate`, `ApiUpdate`, `MethodType`)
- Auth facts: `qa/changes/RET-api-management-20260716-192358-cursor/facts/fact-baseline.json`
- Explore evidence: `qa/changes/RET-api-management-20260716-192358-cursor/explore/advisory.json`

## Scope

API automation cases from `added` (12 cases). E2E cases excluded from this plan.

| Case ID | Title |
|---------|-------|
| TC_APIS_API_001 | 管理员成功创建 API 元数据记录 |
| TC_APIS_API_002 | API 列表分页与默认查询 |
| TC_APIS_API_003 | API 列表 path/summary/tags 条件过滤 |
| TC_APIS_API_004 | 按有效 id 查询 API 详情 |
| TC_APIS_API_005 | 更新 API 元数据字段 |
| TC_APIS_API_006 | 删除 API 记录 |
| TC_APIS_API_007 | refresh 同步带鉴权依赖的路由注册表 |
| TC_APIS_API_008 | 未授权访问 API 管理接口被拒绝 |
| TC_APIS_API_009 | 创建 API 时缺少必填字段被拒绝 |
| TC_APIS_API_010 | 创建 API 时无效 MethodType 被拒绝 |
| TC_APIS_API_011 | 对不存在 api id 的查询更新删除返回合理错误 |
| TC_APIS_API_012 | 相同 path+method 重复创建行为探测 |

`modified`: none. `removed`: none.

## API Targets

| Case ID | Scenario | Method | Path | Expected |
|---------|----------|--------|------|----------|
| TC_APIS_API_001 | Create API metadata | POST | `/api/v1/api/create` | HTTP 200, `code=200`; list/get shows path, method, summary, tags |
| TC_APIS_API_002 | Paginated list (default query) | GET | `/api/v1/api/list` | HTTP 200, `code=200`, `data/total/page/page_size`; `data` is array |
| TC_APIS_API_003 | Filter by path, summary, tags | GET | `/api/v1/api/list` | HTTP 200; filtered rows match substring; no-match keyword returns empty |
| TC_APIS_API_004 | Get API by id | GET | `/api/v1/api/get?id={id}` | HTTP 200, `code=200`, `data.id/path/method/summary/tags` match seed |
| TC_APIS_API_005 | Update summary + tags | POST | `/api/v1/api/update` | HTTP 200; get reflects new summary/tags; path/method unchanged |
| TC_APIS_API_006 | Delete API | DELETE | `/api/v1/api/delete?api_id={id}` | HTTP 200; absent from list; get returns 404 |
| TC_APIS_API_007 | Refresh sync registry | POST | `/api/v1/api/refresh` | HTTP 200; auth routes present; orphan rows removed |
| TC_APIS_API_008 | Unauthorized list/create | GET/POST | `/api/v1/api/list`, `/api/v1/api/create` | No token: 401/422; limited user: 403; admin: 200 |
| TC_APIS_API_009 | Missing path/method/tags on create | POST | `/api/v1/api/create` | HTTP 422; no new row |
| TC_APIS_API_010 | Invalid MethodType enum | POST | `/api/v1/api/create` | HTTP 422; no new row |
| TC_APIS_API_011 | Nonexistent id get/update/delete | GET/POST/DELETE | `/api/v1/api/get`, `/api/v1/api/update`, `/api/v1/api/delete` | HTTP 404, `code=404`; non-5xx |
| TC_APIS_API_012 | Duplicate path+method create probe | POST | `/api/v1/api/create` | Non-5xx; record whether reject or allow duplicates |

## Auth Strategy

| Case group | Auth |
|------------|------|
| TC_APIS_API_001–007, 009–012 | `admin_headers` fixture (`{"token": <jwt>}` per `fact-baseline.json`; not `Authorization: Bearer`) |
| TC_APIS_API_008 (no token) | Empty headers |
| TC_APIS_API_008 (limited user) | `limited_role_user_token` — role with `api_infos=[]` via conftest HTTP setup |
| TC_APIS_API_008 (admin control) | `admin_headers` |

Login path: `POST /api/v1/base/access_token` with `settings.admin_username` / `settings.admin_password` (`tests/helpers/user_assertions.login_token`).

RBAC note: `/api/v1/api/*` is mounted with `DependPermission` (`app/api/v1/__init__.py`). Admin superuser bypasses permission checks. `limited_role_user_token` binds a role with zero `/api/v1/api/*` permissions.

## Request Strategy

**Common headers (authenticated):**

```json
{"token": "<access_token>"}
```

**Create body (`ApiCreate`):**

```json
{
  "path": "/api/v1/<qa-api-prefix>/create-<suffix>",
  "method": "GET",
  "summary": "QA test API",
  "tags": "<qa-api-prefix>-module"
}
```

**Update body (`ApiUpdate`):**

```json
{
  "id": <int>,
  "path": "<unchanged>",
  "method": "GET",
  "summary": "<new summary>",
  "tags": "<new tags>"
}
```

**Query params:**

| Endpoint | Params |
|----------|--------|
| GET `/api/v1/api/list` | `page`, `page_size`, optional `path`, `summary`, `tags` (substring `__contains`) |
| GET `/api/v1/api/get` | required `id` |
| DELETE `/api/v1/api/delete` | required `api_id` |

**Uniqueness:** use `api_create_payload()` from `tests/helpers/api_assertions.py` (prefix from `settings.api_prefix`).

**Create response note:** `POST /create` returns `Success(msg=...)` without `data.id`; resolve id via `GET /list` filtered by `path` + `method` (`find_api_in_list`).

**MethodType enum:** `GET`, `POST`, `PUT`, `DELETE`, `PATCH` (`app/models/enums.py`). Invalid values (e.g. `INVALID`) → Pydantic 422.

**List ordering:** backend orders by `["tags", "id"]` (`app/api/v1/apis/apis.py`).

## Assertion Strategy

| Case ID | Assertions |
|---------|------------|
| TC_APIS_API_001 | HTTP 200 + `code=200`; list/get finds path+method; summary/tags match payload; schema validation |
| TC_APIS_API_002 | pagination fields present; each row has path/method/summary/tags; schema validation |
| TC_APIS_API_003 | path filter rows contain keyword; summary filter rows contain keyword; tags filter rows contain keyword; no-match keyword → `data=[]` or `total=0` |
| TC_APIS_API_004 | `data.id/path/method/summary/tags` match seeded snapshot; schema validation |
| TC_APIS_API_005 | update success; get shows new summary/tags; path/method unchanged; list reflects update |
| TC_APIS_API_006 | delete success; list has no matching id; get returns 404/`code=404` |
| TC_APIS_API_007 | refresh success; orphan path absent after refresh; each `list_auth_routes()` entry present with matching summary/tags |
| TC_APIS_API_008 | no token: 401 or 422 on list/create; limited: 403 on list + create; admin list: 200 |
| TC_APIS_API_009 | HTTP 422 for omit path, omit method, omit tags; `count_apis_matching` unchanged |
| TC_APIS_API_010 | HTTP 422 for invalid method; list count unchanged |
| TC_APIS_API_011 | get/update/delete each HTTP 404 + `code=404` for id `999999999`; status < 500 |
| TC_APIS_API_012 | second create non-5xx; record count of rows with same path+method (neutral probe) |

Schema validation (happy paths): api endpoint schemas registered in `tests/api/conftest.py` `_LOCAL_SCHEMAS`; call `assert_matches_schema` on create/list/get/update/delete/refresh responses.

## Mock Strategy

None. Live SUT over HTTP (`httpx.Client` + `settings.base_url`). Domain seeding via `tests/api/adapters/api.py` for non-create setup paths.

## Cleanup Strategy

| Pattern | Cleanup |
|---------|---------|
| TC_APIS_API_001 | HTTP create primary → `factory_cleanup_api(id)` in `finally` |
| TC_APIS_API_002 | no persistent seed required (read-only list) |
| TC_APIS_API_003 | `apis_for_filter` fixture teardown → `factory_cleanup_api` |
| TC_APIS_API_004, 005 | `existing_api` fixture teardown → `factory_cleanup_api` |
| TC_APIS_API_006 | delete assertion primary; `ephemeral_api` fixture attempts cleanup if row remains |
| TC_APIS_API_007 | `factory_cleanup_api(orphan_id)` if orphan pre-seeded; refresh is idempotent |
| TC_APIS_API_008, 009, 010, 011 | no cleanup (no rows created) |
| TC_APIS_API_012 | `factory_cleanup_api` for all created ids in `finally` |

Always use `cleanup_api` → clears `role_apis` M2M then deletes Api row — never raw-insert for setup.

## Output File Candidates

| File | Purpose |
|------|---------|
| `tests/api/test_api_management_api.py` | All 12 API cases (regenerate with `test_tc_apis_api_*` naming) |
| `tests/helpers/api_assertions.py` | Reuse payloads, list helpers, count helpers |
| `tests/api/conftest.py` | Reuse `existing_api`, `ephemeral_api`, `apis_for_filter`; api schemas in `_LOCAL_SCHEMAS` |
| `tests/testdata/domain/api.py` | Reuse `make_api` / `cleanup_api` / `list_auth_routes` |
| `tests/api/adapters/api.py` | Refactor `factory_make_api` / `factory_cleanup_api` to `isolated_worker` per data-knowledge |

## Needs Review

1. **TC_APIS_API_012** — `ApiController.create` has no path+method uniqueness guard; case delta uses neutral/exploratory probe. Codegen must record observed behavior (reject vs allow duplicates).
2. **TC_APIS_API_008** — unauthenticated list may return HTTP 422 (missing `token` header) vs 401; accept `(401, 422)` per FastAPI header validation contract.
3. **TC_APIS_API_009** — `ApiCreate.summary` defaults to `""` in schema; omitting `summary` may not produce 422. Plan tests path/method/tags omission; document if summary omission is skipped.
4. **`tests/api/adapters/api.py`** — currently uses HTTP create for `factory_make_api`/`factory_cleanup_api`; `.aa/data-knowledge.yaml` declares `isolated_worker` transport. Refactor to `run_isolated(MAKE_API/CLEANUP_API)` before codegen execution.
5. **Legacy test modules** — `tests/api/test_api_api.py` (`TC_API_API_*`) and `tests/api/test_api_management_api.py` (`TC_APIM_API_*`) use prior change prefixes; codegen remaps to `test_tc_apis_api_*` per current case.yaml.
6. **`workflow-state.yaml`** records `phases.case_review.status: done` while `review/case-review.json` has `decision: pass` (gate cleared).

## Blockers

None for plan review. Endpoint Method/Path confirmed from backend source (`app/api/v1/apis/apis.py`, router prefix `/api`).
