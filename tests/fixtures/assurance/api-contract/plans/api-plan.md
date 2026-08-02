# API Plan — CH-CANONICAL

## Source

- `cases/API-ACC-001/case.yaml`

## Scope

| Case ID | Title |
|---------|-------|
| API-ACC-001 | Create account succeeds for admin |

## API Targets

| Case ID | Scenario | Method | Path | Expected |
|---------|----------|--------|------|----------|
| API-ACC-001 | create account | POST | /accounts | assert_ideal HTTP 201 |

## Auth Strategy

| Case ID | Auth |
|---------|------|
| API-ACC-001 | `auth.api_admin_token` from `.aa/data-knowledge.yaml` |

## Request Strategy

| Case ID | Headers | Body | Params |
|---------|---------|------|--------|
| API-ACC-001 | Bearer admin | make_account payload | none |

## Assertion Strategy

| Case ID | Assertions |
|---------|------------|
| API-ACC-001 | assert_ideal HTTP 201 with created account body |

## Mock Strategy

| Case ID | Dependency | Approach |
|---------|------------|----------|
| API-ACC-001 | none | live backend |

## Cleanup Strategy

| Case ID | Cleanup |
|---------|---------|
| API-ACC-001 | `capabilities.domain_factories.account.cleanup_account` |

## Output File Candidates

| Case ID | Target File |
|---------|-------------|
| API-ACC-001 | `tests/api/test_accounts_api.py` |

## Needs Review

None.

## Blockers

None.
