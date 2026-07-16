# API Codegen Plan — RET-api-management-20260716-192358-cursor

## Target Files

| File | Purpose |
|------|---------|
| `tests/api/test_api_management_api.py` | 12 test functions for api registry API cases (regenerate with `TC_APIS_API_*` naming) |
| `tests/helpers/api_assertions.py` | Reuse `api_create_payload`, `find_api_in_list`, count helpers (reuse) |
| `tests/testdata/domain/api.py` | Reuse `make_api` / `cleanup_api` / `list_auth_routes` (reuse) |
| `tests/api/adapters/api.py` | Refactor to `factory_make_api` / `factory_cleanup_api` via `isolated_worker` (reuse shape) |
| `tests/api/conftest.py` | Reuse `existing_api`, `ephemeral_api`, `apis_for_filter`; api `_LOCAL_SCHEMAS` (reuse) |
| `tests/schema_validation.py` | Reuse `assert_matches_schema` (reuse) |

Live-server transport: all test bodies use `api_client` (`httpx.Client`, `settings.base_url`). Setup/cleanup and `list_auth_routes` use `tests/api/adapters/api.py` → `isolated_worker` subprocess boundary per `.aa/data-knowledge.yaml`.

## Test Function Mapping

| Case ID | Test Function | Target File |
|---------|---------------|-------------|
| TC_APIS_API_001 | `test_tc_apis_api_001__create_api_metadata_success` | `tests/api/test_api_management_api.py` |
| TC_APIS_API_002 | `test_tc_apis_api_002__list_apis_pagination` | `tests/api/test_api_management_api.py` |
| TC_APIS_API_003 | `test_tc_apis_api_003__list_filter_by_path_summary_tags` | `tests/api/test_api_management_api.py` |
| TC_APIS_API_004 | `test_tc_apis_api_004__get_api_detail_by_id` | `tests/api/test_api_management_api.py` |
| TC_APIS_API_005 | `test_tc_apis_api_005__update_api_metadata` | `tests/api/test_api_management_api.py` |
| TC_APIS_API_006 | `test_tc_apis_api_006__delete_api_record` | `tests/api/test_api_management_api.py` |
| TC_APIS_API_007 | `test_tc_apis_api_007__refresh_sync_route_registry` | `tests/api/test_api_management_api.py` |
| TC_APIS_API_008 | `test_tc_apis_api_008__reject_unauthorized_api_access` | `tests/api/test_api_management_api.py` |
| TC_APIS_API_009 | `test_tc_apis_api_009__reject_create_missing_required_fields` | `tests/api/test_api_management_api.py` |
| TC_APIS_API_010 | `test_tc_apis_api_010__reject_invalid_method_enum` | `tests/api/test_api_management_api.py` |
| TC_APIS_API_011 | `test_tc_apis_api_011__reject_invalid_api_id_operations` | `tests/api/test_api_management_api.py` |
| TC_APIS_API_012 | `test_tc_apis_api_012__duplicate_path_method_create_behavior` | `tests/api/test_api_management_api.py` |

## Factory Mapping

| Entity | Shared Module | Function | Ownership | Required By |
|--------|---------------|----------|-----------|-------------|
| Api | `tests/testdata/domain/api.py` | `make_api` | reuse | 003–007 setup |
| Api | `tests/testdata/domain/api.py` | `cleanup_api` | reuse | all cleanup paths |
| Api (routes) | `tests/testdata/domain/api.py` | `list_auth_routes` | reuse | 007 refresh assertions |
| Role | `tests/testdata/domain/role.py` | `make_role` | reuse | 008 (via conftest) |
| User | `tests/testdata/domain/user.py` | `make_user` | reuse | 008 (via conftest) |

## Adapter Mapping

| Entity | API Adapter | Transport | Cleanup |
|--------|-------------|-----------|---------|
| Api | `tests/api/adapters/api.py` | `isolated_worker` | `factory_cleanup_api` |
| Auth routes | `tests/api/adapters/api.py` | `isolated_worker` | n/a (read-only) |
| Role/User | `tests/api/adapters/role.py`, `user.py` | `isolated_worker` | conftest finalizers |

## Fixture Mapping

| Fixture | Source Factory | Wrapper Only | Required By |
|---------|----------------|--------------|-------------|
| `api_client` | n/a (httpx) | no | all |
| `admin_headers` | `login_token` | yes | 001–007, 009–012 |
| `existing_api` | `factory_make_api` | yes | 004, 005 |
| `ephemeral_api` | `factory_make_api` | yes | 006 |
| `apis_for_filter` | `factory_make_api` (tokens bundle) | yes | 003 |
| `limited_role_user_token` | role+user HTTP setup | yes | 008 |

## Helper Mapping

| Helper | Module | Purpose |
|--------|--------|---------|
| `api_create_payload` | `tests/helpers/api_assertions.py` | Build `ApiCreate`-compatible JSON with unique path |
| `find_api_in_list` | `tests/helpers/api_assertions.py` | Locate row by id/path/method |
| `count_apis_matching` | `tests/helpers/api_assertions.py` | List total with filters |
| `count_apis_with_method_path` | `tests/helpers/api_assertions.py` | Duplicate path+method count |
| `orphan_api_path` | `tests/helpers/api_assertions.py` | Path not in live routes for refresh test |
| `auth_route_lookup_key` | `tests/helpers/api_assertions.py` | Normalize method+path key |
| `login_token` | `tests/helpers/user_assertions.py` | Auth |
| `assert_matches_schema` | `tests/schema_validation.py` | Response schema |

## Import Strategy

**`tests/api/test_api_management_api.py`:**

```python
from tests.api.adapters.api import (
    factory_cleanup_api,
    factory_list_auth_routes,
)
from tests.config import settings
from tests.helpers.api_assertions import (
    api_create_payload,
    auth_route_lookup_key,
    count_apis_matching,
    count_apis_with_method_path,
    find_api_in_list,
    orphan_api_path,
)
from tests.schema_validation import assert_matches_schema
```

**`tests/api/adapters/api.py` (target shape):**

```python
from tests.api.adapters.isolated_worker import run_isolated
from tests.testdata.domain.api import CLEANUP_API, LIST_AUTH_ROUTES, MAKE_API

def factory_make_api(**kwargs):
    return run_isolated(MAKE_API, **kwargs)

def factory_cleanup_api(api_id: int):
    return run_isolated(CLEANUP_API, api_id=api_id)

def factory_list_auth_routes():
    return run_isolated(LIST_AUTH_ROUTES)
```

## Assertion Mapping

| Case ID | Assertions |
|---------|------------|
| TC_APIS_API_001 | HTTP 200, `code=200`; list/get field match; `assert_matches_schema` |
| TC_APIS_API_002 | pagination fields; row shape; schema |
| TC_APIS_API_003 | filter substring match; no-match empty; non-5xx |
| TC_APIS_API_004 | detail fields match snapshot; schema |
| TC_APIS_API_005 | updated summary/tags; path/method unchanged |
| TC_APIS_API_006 | delete success; get 404; list absent |
| TC_APIS_API_007 | refresh success; orphan removed; auth routes synced |
| TC_APIS_API_008 | 401/422 no token; 403 limited; 200 admin |
| TC_APIS_API_009 | 422 per omitted field; count unchanged |
| TC_APIS_API_010 | 422 invalid method; count unchanged |
| TC_APIS_API_011 | 404 get/update/delete; non-5xx |
| TC_APIS_API_012 | non-5xx duplicate create; document row count |

## Data Setup Mapping

| Case ID | Setup |
|---------|-------|
| TC_APIS_API_001 | `api_create_payload()` only |
| TC_APIS_API_002 | none |
| TC_APIS_API_003 | `apis_for_filter` |
| TC_APIS_API_004, 005 | `existing_api` |
| TC_APIS_API_006 | `ephemeral_api` |
| TC_APIS_API_007 | HTTP orphan create in test |
| TC_APIS_API_008 | auth personas only |
| TC_APIS_API_009, 010 | invalid payloads |
| TC_APIS_API_011 | `NONEXISTENT_API_ID = 999999999` |
| TC_APIS_API_012 | HTTP create × 2 same path+method |

## Cleanup Mapping

| Case ID | Cleanup |
|---------|---------|
| TC_APIS_API_001 | `factory_cleanup_api` in `finally` |
| TC_APIS_API_002 | none |
| TC_APIS_API_003 | fixture finalizer |
| TC_APIS_API_004, 005 | fixture finalizer |
| TC_APIS_API_006 | delete + fixture finalizer |
| TC_APIS_API_007 | orphan delete in `finally` if needed |
| TC_APIS_API_008–011 | none |
| TC_APIS_API_012 | cleanup all probe ids |

## Run Guidance

For `aa-run` (Phase 8):

```bash
pytest tests/api/test_api_management_api.py -v
pytest tests/api/test_api_management_api.py -k "tc_apis_api" -v
pytest tests/api/test_api_management_api.py -m smoke  # if markers added
```

Env: `BASE_URL` (default `http://127.0.0.1:9999`), `QA_ADMIN_USERNAME`, `QA_ADMIN_PASSWORD`, `QA_API_PREFIX`.

## Codegen Preconditions

- `api-plan-review.json` `decision == "pass"`
- `codegen_readiness in ["ready", "ready_with_warnings"]` (from reviewer)
- `.aa/data-knowledge.yaml` exists (present)
- `tests/api/adapters/api.py` refactored to isolated_worker before execution
