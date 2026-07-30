# E2E Plan — CH-E2E-CONTRACT-001

## Source

- `cases/TC_E2E_AUTH_REJECT/case.yaml`

## Scope

| Case ID | Title |
|---------|-------|
| TC_E2E_AUTH_REJECT | Non-admin denied API management |

## User Journey

Limited-role user attempts direct navigation to API management and must be denied without server errors.

## Route Mapping

| Case ID | Entry | Target Route |
|---------|-------|--------------|
| TC_E2E_AUTH_REJECT | login page | `/system/api` |

## Natural Steps

From `case.yaml:steps` for TC_E2E_AUTH_REJECT.

## Assertion Strategy

| Case ID | Assertions |
|---------|------------|
| TC_E2E_AUTH_REJECT | assert_ideal HTTP 4xx; no operable CRUD |

## Selector Strategy

Use role and text locators; avoid brittle CSS unless documented as fallback.

## Network / API Dependency

Limited-user seeding uses the E2E auth adapter declared in `.aa/data-knowledge.yaml`.

## Flaky Risk Notes

Permission-denial UX may branch between redirect and masked shell; both remain 4xx-class outcomes.

## Output File Candidates

| Case ID | Target File |
|---------|-------------|
| TC_E2E_AUTH_REJECT | `tests/e2e/test_api_management_e2e.py` |

## Needs Review

None.

## Blockers

None.
