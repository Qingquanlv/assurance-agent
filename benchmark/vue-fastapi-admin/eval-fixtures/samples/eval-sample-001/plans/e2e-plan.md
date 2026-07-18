# E2E Test Plan — RET-api-management-20260716-192358-cursor

## Source

- `qa/changes/RET-api-management-20260716-192358-cursor/cases/system/api/case.yaml`
- `qa/changes/RET-api-management-20260716-192358-cursor/proposal.md`
- Frontend confirmation: `web/src/views/system/api/index.vue` (`CommonPage` title `API列表`, CRUD modal, refresh dialog)
- Route helpers: `tests/e2e/conftest.py` (`navigate_api_mgmt`, `frontend_path`)
- Auth facts: `qa/changes/RET-api-management-20260716-192358-cursor/facts/fact-baseline.json`
- Explore evidence: `qa/changes/RET-api-management-20260716-192358-cursor/explore/advisory.json` (SC-FE-001)
- Prior E2E reference (different change-id prefix): `tests/e2e/test_api_e2e.py`

## Scope

E2E automation cases from `added` (6 cases). API cases excluded from this plan.

| Case ID | Priority | Title |
|---------|----------|-------|
| TC_APIS_E2E_001 | P0 | 管理员进入 API 管理页面 |
| TC_APIS_E2E_002 | P0 | 管理员新建 API 后列表可见 |
| TC_APIS_E2E_003 | P1 | 管理员编辑 API 元数据 |
| TC_APIS_E2E_004 | P1 | 管理员删除 API 记录 |
| TC_APIS_E2E_005 | P1 | 管理员触发刷新 OpenAPI 注册 |
| TC_APIS_E2E_006 | P1 | 非管理员无法访问 API 管理 |

`modified`: none. `removed`: none.

All selected cases have `type: E2E` and `automation.required: true`. All are P0/P1 (default plan scope).

## User Journey

1. **Admin smoke (001–005):** Admin logs in → navigates to `/system/api` (menu or direct URL) → sees API list with search bar and action buttons → performs CRUD or refresh → observes success toast and updated table.
2. **Permission denial (006):** Limited-role user (empty `api_infos`, no API menu binding) logs in → API management menu hidden → direct URL `/system/api` yields 404 redirect or permission-masked page without operable CRUD/refresh controls.

## Route Mapping

| Item | Value | Source |
|------|-------|--------|
| Frontend base | `settings.frontend_url` (default `http://127.0.0.1:3100`) | `tests/config.py` |
| Login page | `/login` | `tests/e2e/conftest.py` `ui_login` |
| API management page | `/system/api` | `navigate_api_mgmt`, `index.vue` |
| Menu path (001 optional) | Sidebar: `系统管理` → `menuitem` `API管理` | `test_api_e2e.py` pattern |
| Hash mode | `frontend_path()` respects `VITE_USE_HASH=1` → `/#/system/api` | `conftest.frontend_path` |

**Entry pages:**

| Case | Entry |
|------|-------|
| TC_APIS_E2E_001 | Login → menu navigation (preferred per case steps) or direct `goto /system/api` |
| TC_APIS_E2E_002–005 | `api_page` fixture (admin login + `navigate_api_mgmt`) |
| TC_APIS_E2E_006 | `limited_user_page` fixture (limited user login, no navigation preset) |

## Natural Steps

Mapped from `case.yaml:steps`:

| Case ID | Steps |
|---------|-------|
| TC_APIS_E2E_001 | Navigate to API management → confirm title/list visible → confirm 新建API / 刷新API buttons visible |
| TC_APIS_E2E_002 | Click 新建API → fill path/method/summary/tags in modal → save → wait list refresh |
| TC_APIS_E2E_003 | Click row 编辑 → modify summary or tags → save → wait list refresh |
| TC_APIS_E2E_004 | Click row 删除 → confirm in popconfirm → wait list refresh |
| TC_APIS_E2E_005 | Click 刷新API → confirm in dialog → wait list reload |
| TC_APIS_E2E_006 | Attempt menu or direct URL to API management |

## Assertion Strategy

| Case ID | UI Assertions |
|---------|-----------------|
| TC_APIS_E2E_001 | `heading` `API列表` visible; table headers `API路径`/`请求方式`/`API简介`/`Tags`; buttons `新建API`/`刷新API`; search placeholders visible |
| TC_APIS_E2E_002 | Dialog closes after save; row with submitted path/method/tags visible; no error dialog; optional API verify via `page.request` list |
| TC_APIS_E2E_003 | Toast `编辑成功` (or equivalent success feedback); updated summary/tags visible in filtered row |
| TC_APIS_E2E_004 | Popconfirm text `确定删除该API吗?`; row absent after confirm |
| TC_APIS_E2E_005 | Confirm dialog mentions `app.routes`; toast `刷新完成`; page heading still visible; no error dialog |
| TC_APIS_E2E_006 | `menuitem` `API管理` not visible; after direct URL: either `/404` or page without operable `新建API`/`编辑`/`删除`/`刷新API` buttons |

## Selector Strategy

Priority: **role > label > text > testid > CSS** (per `.aa/config.yaml`).

| Interaction | Locator rule |
|-------------|--------------|
| Page title | `get_by_role("heading", name="API列表")` |
| Table | `get_by_role("table")` + column header text |
| 新建API / 刷新API | `get_by_role("button", name="新建API")` / `name="刷新API"` |
| Search fields | `get_by_placeholder("请输入API路径")`, `请输入API简介`, `请输入API模块` |
| Modal fields | Dialog-scoped placeholders from `index.vue` form |
| Save | `dialog.get_by_role("button", name="保存")` |
| Row actions | `row.get_by_role("button", name="编辑")` / `name="删除"` |
| Delete confirm | Popconfirm text `确定删除该API吗?` → button `确认` or `确定` |
| Refresh confirm | Dialog text contains `app.routes` → button `确定` |
| Sidebar menu | `get_by_text("系统管理")` → `get_by_role("menuitem", name="API管理")` |
| Success toasts | `get_by_text("刷新完成")`, `get_by_text("编辑成功")` |

**CSS fallback (last resort):** `.n-popconfirm` filtered by delete text (`confirm_api_delete` helper).

No locators written back to `case.yaml`. No POM generated by default.

## Network / API Dependency

| UI action | Backend endpoint | Notes |
|-----------|------------------|-------|
| Page load / search | `GET /api/v1/api/list` | `navigate_api_mgmt` waits for `api/list` response |
| Create | `POST /api/v1/api/create` | Modal save |
| Update | `POST /api/v1/api/update` | Edit modal save |
| Delete | `DELETE /api/v1/api/delete?api_id={id}` | Popconfirm |
| Refresh | `POST /api/v1/api/refresh` | Confirm dialog |
| Login | `POST /api/v1/base/access_token` | `ui_login` |
| Pre/post verify (002–004) | `GET /api/v1/api/list` via `page.request` or httpx | Optional hardening |

All endpoints require `DependPermission`; admin superuser bypasses RBAC.

## Flaky Risk Notes

| Risk | Cases | Mitigation |
|------|-------|------------|
| Async list reload after CRUD | 002–004 | `expect_response` on `api/list`; `search_apis` before row assertion |
| Modal animation / overlay | 002–004 | Wait `dialog` visible/hidden with timeout |
| Popconfirm button label variance | 004 | `confirm_api_delete` tries `确认` then `确定` |
| Refresh mutates DB globally | 005 | Accept side effects; no row-count assertion; document in teardown notes |
| Limited user 404 vs masked page | 006 | Branch on URL/text; assert absence of operable controls in both branches |
| Hash vs history routing | all | Use `frontend_path()` helper |
| UI create for setup | 002 only (primary path) | Prefer API/isolated_worker cleanup in `finally`; UI create is the test action, not setup for other cases |
| Frontend SUT readiness | all | Session autouse `_verify_frontend_ready` in conftest |

## Output File Candidates

| File | Cases |
|------|-------|
| `tests/e2e/test_api_management_e2e.py` | TC_APIS_E2E_001 – TC_APIS_E2E_006 (regenerate with `TC_APIS_*` naming; retire `TC_APIM_*` prefix in traceability header) |

Reuse (no new files required for plan):

- `tests/e2e/conftest.py` — `api_page`, `editable_api`, `deletable_api`, `limited_user_page`, navigation helpers
- `tests/e2e/adapters/api.py` — `make_api`, `cleanup_api`
- `tests/e2e/adapters/role.py` — `compose_limited_role` for 006

## Needs Review

1. **TC_APIS_E2E_001 navigation** — Case steps say "导航至 API 管理页面"; plan supports menu flow (preferred) or direct URL. Reviewer should confirm menu labels match seeded frontend i18n.
2. **TC_APIS_E2E_006 denial UX** — Product may redirect to `/404` or render permission-masked shell; plan accepts both (pattern from prior `test_api_e2e.py`).
3. **TC_APIS_E2E_005 teardown** — Refresh syncs all auth routes and may delete orphan DB rows; no deterministic row assertion planned.
4. **Legacy module naming** — Existing `tests/e2e/test_api_e2e.py` uses `TC_APIM_E2E_*`; codegen remaps to `test_tc_apis_e2e_*`.
5. **`limited_role_credentials` vs `e2e_seed_limited_user`** — Conftest uses `compose_limited_role` with `api_infos=[]`; confirm acceptable for API menu denial (not dept-specific limited user).

## Blockers

None for E2E planning.
