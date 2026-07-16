# E2E Codegen Plan — RET-api-management-20260716-192358-cursor

## Target Files

| File | Purpose |
|------|---------|
| `tests/e2e/test_api_management_e2e.py` | 6 test functions for API management E2E cases (regenerate with `TC_APIS_E2E_*` naming) |
| `tests/e2e/conftest.py` | Reuse `api_page`, `editable_api`, `deletable_api`, `limited_user_page`, navigation helpers (reuse) |
| `tests/e2e/adapters/api.py` | Reuse `make_api`, `cleanup_api` via `isolated_worker` (reuse) |
| `tests/e2e/adapters/role.py` | Reuse `compose_limited_role` for limited user (reuse) |
| `tests/e2e/adapters/user.py` | Reuse `make_user`, `cleanup_user` (reuse) |
| `tests/config.py` | Reuse `settings.frontend_url`, `api_prefix`, credentials (reuse) |

**Deprecate traceability in:** `tests/e2e/test_api_e2e.py` — replace or supersede with `test_api_management_e2e.py` using `TC_APIS_E2E_*` case IDs.

## Test Function Mapping

| Case ID | Test Function | Target File |
|---------|---------------|-------------|
| TC_APIS_E2E_001 | `test_tc_apis_e2e_001__admin_enters_api_management` | `tests/e2e/test_api_management_e2e.py` |
| TC_APIS_E2E_002 | `test_tc_apis_e2e_002__admin_creates_api_via_modal` | `tests/e2e/test_api_management_e2e.py` |
| TC_APIS_E2E_003 | `test_tc_apis_e2e_003__admin_edits_api_metadata` | `tests/e2e/test_api_management_e2e.py` |
| TC_APIS_E2E_004 | `test_tc_apis_e2e_004__admin_deletes_api` | `tests/e2e/test_api_management_e2e.py` |
| TC_APIS_E2E_005 | `test_tc_apis_e2e_005__admin_refreshes_openapi_registry` | `tests/e2e/test_api_management_e2e.py` |
| TC_APIS_E2E_006 | `test_tc_apis_e2e_006__non_admin_cannot_access_api_management` | `tests/e2e/test_api_management_e2e.py` |

## Fixture Mapping

| Fixture | Source | Required By |
|---------|--------|-------------|
| `page` | pytest-playwright | 001, 006 |
| `api_page` | `tests/e2e/conftest.py` | 002–005 |
| `admin_token` | `tests/e2e/conftest.py` | 002 (id capture) |
| `editable_api` | `make_api` + verify + teardown | 003 |
| `deletable_api` | `make_api` + verify + teardown | 004 |
| `limited_user_page` | `limited_role_credentials` + `ui_login` | 006 |

## Data Setup Script Mapping

| Script / adapter | Input | Output | Required By |
|------------------|-------|--------|-------------|
| `tests/e2e/adapters/api.py` `make_api` | unique `path`, `method`, `summary`, `tags` | api snapshot dict | 003, 004 fixtures |
| `tests/e2e/adapters/api.py` `cleanup_api` | `api_id: int` | void | 002 finally, fixture teardowns |
| `tests/e2e/adapters/role.py` `compose_limited_role` | `admin_headers` | role dict | 006 via `limited_role_credentials` |
| `tests/e2e/adapters/user.py` `make_user` | `role_ids`, `is_superuser=False` | user dict | 006 |

No new scripts under `qa/changes/.../scripts/`.

## Import Strategy

**`tests/e2e/test_api_management_e2e.py`:**

```python
from __future__ import annotations

import uuid

from playwright.sync_api import Page, expect

from tests.config import settings
from tests.e2e.adapters.api import cleanup_api
from tests.e2e.conftest import (
    api_row,
    confirm_api_delete,
    e2e_login_admin,
    fill_api_modal,
    frontend_path,
    search_apis,
)
```

## Step Mapping

| Case ID | Playwright steps |
|---------|------------------|
| TC_APIS_E2E_001 | `e2e_login_admin(page)` → click `系统管理` → click `menuitem` `API管理` → assert heading/table/buttons/search fields |
| TC_APIS_E2E_002 | `api_page.get_by_role("button", name="新建API").click()` → `fill_api_modal(...)` → `dialog.get_by_role("button", name="保存").click()` with `expect_response(api/create)` → `search_apis` → `api_row` assertions |
| TC_APIS_E2E_003 | `search_apis(api_page, path=...)` → row `编辑` → fill summary/tags → save with `expect_response(api/update)` → assert `编辑成功` → re-search → assert updated text |
| TC_APIS_E2E_004 | `search_apis` → row `删除` → assert popconfirm text → `confirm_api_delete` with `expect_response(api/list)` → row not visible |
| TC_APIS_E2E_005 | click `刷新API` → assert confirm copy → click `确定` with `expect_response(api/refresh)` → assert `刷新完成` |
| TC_APIS_E2E_006 | assert `menuitem` `API管理` hidden → `goto frontend_path("/system/api")` → branch 404 vs masked → assert CRUD/refresh buttons not visible |

## Locator Strategy

| Interaction | Locator (priority order) |
|-------------|--------------------------|
| Login button | role `button`, name `/登录\|Login/` |
| Sidebar API menu | role `menuitem`, name `API管理` |
| Page title | role `heading`, name `API列表` |
| Action buttons | role `button`, names `新建API`, `刷新API`, `编辑`, `删除`, `保存`, `确定` |
| Modal | role `dialog` |
| Form fields | placeholder `请输入API路径`, `请输入请求方式`, `请输入API简介`, `请输入Tags` |
| Table row filter | role `row` + `filter(has_text=path)` |
| Delete popconfirm | text `确定删除该API吗?` → CSS `.n-popconfirm` fallback |
| Success feedback | text `编辑成功`, `刷新完成` |

## Assertion Mapping

| Case ID | Assertions |
|---------|------------|
| TC_APIS_E2E_001 | Heading `API列表`; table column texts; buttons `新建API`/`刷新API`; search placeholders |
| TC_APIS_E2E_002 | Dialog hidden; row visible with path/method/tags; `created_api_id` captured; no error dialog |
| TC_APIS_E2E_003 | Toast `编辑成功`; updated summary/tags in row |
| TC_APIS_E2E_004 | Popconfirm visible; row not visible after delete |
| TC_APIS_E2E_005 | Confirm dialog text; toast `刷新完成`; heading still visible |
| TC_APIS_E2E_006 | Menu hidden; direct URL denied — no operable CRUD/refresh buttons |

## Cleanup Mapping

| Case ID | Cleanup |
|---------|---------|
| TC_APIS_E2E_001 | none |
| TC_APIS_E2E_002 | `cleanup_api(created_api_id)` in `finally` |
| TC_APIS_E2E_003 | `editable_api` fixture finalizer |
| TC_APIS_E2E_004 | UI delete primary; `deletable_api` finalizer fallback |
| TC_APIS_E2E_005 | none |
| TC_APIS_E2E_006 | `limited_role_credentials` finalizer |

## Run Guidance

For `aa-run` (Phase 8):

```bash
uv run pytest tests/e2e/test_api_management_e2e.py -v --headed
uv run pytest tests/e2e/test_api_management_e2e.py -v --headed -k "tc_apis_e2e"
uv run pytest tests/e2e/test_api_management_e2e.py -v --headed -k "tc_apis_e2e_001"
```

Env: `E2E_FRONTEND_URL` (default `http://127.0.0.1:3100`), `BASE_URL`, `QA_ADMIN_USERNAME`, `QA_ADMIN_PASSWORD`, `QA_API_PREFIX`, optional `VITE_USE_HASH`, `QA_SKIP_SUT_READINESS=1` to bypass frontend probe.

## Codegen Preconditions

- `review/e2e-plan-review.json` (or `plan-review.json` per orchestrator) `decision == "pass"`
- `codegen_readiness in ["ready", "ready_with_warnings"]` (from reviewer)
- `.aa/data-knowledge.yaml` exists (present)
- Frontend SUT reachable at `settings.frontend_url`
- Backend SUT reachable at `settings.base_url`
- Existing `tests/e2e/conftest.py` API helpers present (`navigate_api_mgmt`, `fill_api_modal`, etc.)
