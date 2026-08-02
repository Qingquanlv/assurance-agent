# API Codegen Plan — CH-CANONICAL

## Target Files

| File | Purpose |
|------|---------|
| `tests/api/test_accounts_api.py` | API-ACC-001 pytest entrypoint (create-if-missing) |
| `tests/api/adapters/account.py` | Account seed/cleanup transport (reuse) |
| `tests/testdata/domain/account.py` | Shared account factory (reuse) |

## Test Function Mapping

| Case ID | Test Function | Target File |
|---------|---------------|-------------|
| API-ACC-001 | `test_api_acc_001__create_account_success` | `tests/api/test_accounts_api.py` |

## Factory Mapping

| Entity | Shared Module | Function | Ownership | Required By |
|--------|---------------|----------|-----------|-------------|
| Account | `tests/testdata/domain/account.py` | `make_account` | reuse | API-ACC-001 setup |
| Account | `tests/testdata/domain/account.py` | `cleanup_account` | reuse | API-ACC-001 cleanup |

## Adapter Mapping

| Entity | API Adapter | Transport | Cleanup |
|--------|-------------|-----------|---------|
| Account | `tests/api/adapters/account.py` | `isolated_worker` | `cleanup_account` |

## Fixture Mapping

| Fixture | Source Factory | Wrapper Only (yes/no) | Required By |
|---------|----------------|-------------------------|-------------|
| `admin_headers` | `auth.api_admin_token` | yes | API-ACC-001 |

## Assertion Mapping

| Case ID | Assertions |
|---------|------------|
| API-ACC-001 | assert_ideal HTTP 201 with created account body |

## Data Setup Mapping

| Case ID | Setup | Capability |
|---------|-------|------------|
| API-ACC-001 | build payload via make_account | `capabilities.domain_factories.account.make_account` |

## Cleanup Mapping

| Case ID | Cleanup | Capability |
|---------|---------|------------|
| API-ACC-001 | cleanup_account | `capabilities.domain_factories.account.cleanup_account` |

## Codegen Preconditions

- `review/api-plan-review.json` `decision == "pass"`
- `.aa/data-knowledge.yaml` exists
