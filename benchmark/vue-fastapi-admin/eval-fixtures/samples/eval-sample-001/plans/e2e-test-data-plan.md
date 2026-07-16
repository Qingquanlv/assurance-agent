# E2E Test Data Plan — RET-api-management-20260716-192358-cursor

## Scope

Cases requiring data preparation:

| Case ID | Data need |
|---------|-----------|
| TC_APIS_E2E_001 | Admin login only |
| TC_APIS_E2E_002 | Unique path/method payload (generated in test); cleanup after UI create |
| TC_APIS_E2E_003 | Pre-seeded editable API row |
| TC_APIS_E2E_004 | Pre-seeded deletable API row |
| TC_APIS_E2E_005 | None (admin session + empty/minimal list acceptable) |
| TC_APIS_E2E_006 | Limited-role user with empty API permissions |

## Required Data

| Entity | State | Capability |
|--------|-------|------------|
| Auth (admin) | logged-in session | `auth.e2e_admin_login` / `ui_login` |
| Auth (limited) | non-superuser, `api_infos=[]` | `compose_limited_role` + `make_user` |
| Api | editable row | `adapters.e2e.api.make_api` → `editable_api` fixture |
| Api | deletable row | `adapters.e2e.api.make_api` → `deletable_api` fixture |
| Api | UI-created row (002) | Created via UI; id captured via `page.request` list; cleaned via `cleanup_api` |

## Preconditions

From `case.yaml`:

| Case | Preconditions | Resolution |
|------|---------------|------------|
| TC_APIS_E2E_001 | 前端可访问；管理员可登录 | `page` + `e2e_login_admin` |
| TC_APIS_E2E_002 | 管理员已登录并在 API 管理页 | `api_page` fixture |
| TC_APIS_E2E_003 | 管理员已登录；存在可编辑记录 | `api_page` + `editable_api` |
| TC_APIS_E2E_004 | 管理员已登录；存在待删除记录 | `api_page` + `deletable_api` |
| TC_APIS_E2E_005 | 管理员已登录并在 API 管理页 | `api_page` fixture |
| TC_APIS_E2E_006 | 无 API 管理权限的普通用户 | `limited_user_page` via `limited_role_credentials` |

## Data Setup Strategy

Priority (per skill contract):

1. `tests/e2e/adapters/api.py` — **selected** (`make_api` / `cleanup_api` via `isolated_worker`)
2. API setup via httpx in fixture verify helpers — used for pre-UI list verification only
3. Playwright `page.request` — id capture after UI create (002)
4. Shared fixtures in `tests/e2e/conftest.py` — **selected**
5. UI setup — **only for TC_APIS_E2E_002 primary test action** (create via modal); not used as setup for 003/004

**Forbidden:** UI-click create as precondition for edit/delete cases (003/004 use factory seed).

## Data Setup Script Plan

No new standalone script under `qa/changes/.../scripts/`. Setup transport:

| Script / module | Path | Input | Output | Cleanup |
|-----------------|------|-------|--------|---------|
| E2E api adapter | `tests/e2e/adapters/api.py` | `path`, `method`, `summary`, `tags` kwargs | `{id, path, method, summary, tags}` snapshot | `cleanup_api(api_id)` |
| Role limited seed | `tests/e2e/adapters/role.py` `compose_limited_role` | `admin_headers` | `{id, name, ...}` role with `api_infos=[]` | `cleanup_role` in fixture finalizer |
| User for limited role | `tests/e2e/adapters/user.py` `make_user` | `role_ids`, `is_superuser=False` | `{id, username, password}` | `cleanup_user` in fixture finalizer |

Unique path pattern: `/api/v1/{settings.api_prefix}/<suffix>` with `uuid.uuid4().hex[:8]`.

## Capability Mapping

| Need | Capability | Source | Status |
|------|------------|--------|--------|
| Admin UI login | `auth.e2e_admin_login` | `tests/e2e/conftest.py` | found |
| Navigate API page | `navigate_api_mgmt`, `api_page` | `tests/e2e/conftest.py` | found |
| Seed Api (edit/delete) | `adapters.e2e.api.make_api` | `tests/e2e/adapters/api.py` | found |
| Cleanup Api | `adapters.e2e.api.cleanup_api` | `tests/e2e/adapters/api.py` | found |
| Editable/deletable fixtures | `editable_api`, `deletable_api` | `tests/e2e/conftest.py` | found |
| Limited user session | `limited_user_page`, `limited_role_credentials` | `tests/e2e/conftest.py` | found |
| Limited role compose | `compose_limited_role` | `tests/e2e/adapters/role.py` | found |
| Pre-UI list verify | `verify_api_list_contains` | `tests/e2e/conftest.py` | found |
| Runtime settings | `settings.frontend_url`, `api_prefix`, credentials | `tests/config.py` | found |
| Formal data-knowledge | `capabilities.adapters.e2e.api`, `fixtures.e2e_*` | `.aa/data-knowledge.yaml` | found |
| Dedicated `e2e_seed_limited_user` adapter | `tests/e2e/adapters/auth.py` | `.aa/data-knowledge.yaml` | warning — exists but conftest uses inline `compose_limited_role`; functionally equivalent |

## Runtime Verify Strategy

Before UI steps in seeded cases:

| Fixture / step | Verify |
|----------------|--------|
| `editable_api`, `deletable_api` | `verify_api_list_contains(admin_headers, path)` after `make_api` |
| TC_APIS_E2E_002 post-create | `_capture_api_id_by_path(page, admin_token, path)` via `GET /api/v1/api/list` |
| TC_APIS_E2E_004 post-delete | `expect(api_row(page, path)).not_to_be_visible` |

## Login / Auth State Strategy

| Persona | Strategy |
|---------|----------|
| Admin (001–005) | Per-test `ui_login` (001) or `api_page` fixture (002–005); session-scoped `admin_token` for API verify |
| Limited user (006) | `limited_user_page`: login via `ui_login(credentials)` from `limited_role_credentials` fixture |

No `storageState` file required; inline login per existing dept/user/role E2E patterns.

Credentials from `settings.admin_username` / `settings.admin_password` (`fact-baseline.json`: admin / 123456).

## Cleanup Strategy

| Case ID | Cleanup |
|---------|---------|
| TC_APIS_E2E_001 | none |
| TC_APIS_E2E_002 | `cleanup_api(created_api_id)` in `finally` if UI create succeeded |
| TC_APIS_E2E_003 | `editable_api` fixture finalizer → `cleanup_api` |
| TC_APIS_E2E_004 | Delete via UI is primary assertion; `deletable_api` finalizer as fallback if delete fails |
| TC_APIS_E2E_005 | none (refresh side effects accepted; no row teardown) |
| TC_APIS_E2E_006 | `limited_role_credentials` finalizer → `cleanup_user` + `cleanup_role` |

## No Data Required Cases

- **TC_APIS_E2E_001** — auth + navigation only
- **TC_APIS_E2E_005** — admin session only; refresh operates on live registry

## Blockers / Assumptions

**Blockers:** None

**Assumptions:**

- Admin superuser bypasses `DependPermission` for all API management UI actions.
- `compose_limited_role` with empty `api_infos` and `menu_ids` denies API management menu and operable controls (006).
- Frontend and backend SUT are running at `settings.frontend_url` / `settings.base_url`.
- No pre-seeded non-admin user in DB; limited user created per test via factories.

## Review Checklist

- [ ] Factory seed used for 003/004 (not UI create)
- [ ] UI create in 002 has API-level cleanup in `finally`
- [ ] Limited user role has zero API permissions (not merely missing menu)
- [ ] Refresh case (005) documents global DB side effects
- [ ] Unique paths use `settings.api_prefix` to avoid collision
- [ ] No production credentials in scripts or plans
