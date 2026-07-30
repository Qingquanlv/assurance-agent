# E2E Codegen Plan — CH-E2E-CONTRACT-001

## Target Files

| File | Purpose |
|------|---------|
| `tests/e2e/test_api_management_e2e.py` | TC_E2E_AUTH_REJECT Playwright test (create-if-missing) |
| `tests/e2e/adapters/auth.py` | Reuse limited-user seed/cleanup transport (reuse) |

## Test Function Mapping

| Case ID | Test Function | Target File |
|---------|---------------|-------------|
| TC_E2E_AUTH_REJECT | `test_tc_e2e_auth_reject__limited_user_denied` | `tests/e2e/test_api_management_e2e.py` |

## Factory Mapping

| Entity | Shared Module | Function | Ownership | Required By |
|--------|---------------|----------|-----------|-------------|
| Role | `tests/testdata/domain/role.py` | `make_role` | reuse | limited-user seed |
| Role | `tests/testdata/domain/role.py` | `cleanup_role` | reuse | limited-user cleanup |
| User | `tests/testdata/domain/user.py` | `make_user` | reuse | limited-user seed |
| User | `tests/testdata/domain/user.py` | `cleanup_user` | reuse | limited-user cleanup |

## Adapter Mapping

| Entity | E2E Adapter | Transport | Cleanup |
|--------|-------------|-----------|---------|
| Auth | `tests/e2e/adapters/auth.py` | `isolated_worker` | `e2e_cleanup_limited_user` |

## Fixture Mapping

| Fixture | Source Factory | Wrapper Only (yes/no) | Required By |
|---------|----------------|-------------------------|-------------|
| `limited_user_page` | `capabilities.adapters.e2e.auth.seed_limited_user` | yes | TC_E2E_AUTH_REJECT |

## Data Setup Script Mapping

| Script | Input | Output | Required By |
|--------|-------|--------|-------------|
| `tests/e2e/adapters/auth.py` | limited-role recipe | user credentials | TC_E2E_AUTH_REJECT |

## Import Strategy

Import Playwright helpers and the E2E auth adapter from their declared modules.

## Step Mapping

| Case ID | Playwright steps |
|---------|------------------|
| TC_E2E_AUTH_REJECT | login as limited user → goto `/system/api` → assert denial |

## Locator Strategy

Prefer role and text locators for denial messaging and hidden CRUD controls.

## Assertion Mapping

| Case ID | Assertions |
|---------|------------|
| TC_E2E_AUTH_REJECT | assert_ideal HTTP 4xx or permission-masked shell without operable CRUD |

## Cleanup Mapping

| Case ID | Cleanup |
|---------|---------|
| TC_E2E_AUTH_REJECT | `capabilities.adapters.e2e.auth.cleanup_limited_user` |

## Run Guidance

| Target | Pytest Args | Markers | Environment |
|--------|-------------|---------|-------------|
| `tests/e2e/test_api_management_e2e.py` | `-v --headed -k tc_e2e_auth_reject` | none | `E2E_FRONTEND_URL`, admin credentials from `tests.config.settings` |

## Codegen Preconditions

- `review/plan-review.json` `decision == "pass"`
- `.aa/data-knowledge.yaml` exists
