# E2E Test Data Plan — CH-CANONICAL

## Scope

| Case ID | Title |
|---------|-------|
| TC_E2E_AUTH_REJECT | Limited-role user with no API permissions |

## Required Data

| Entity | State | Capability |
|--------|-------|------------|
| Role | limited permissions | `capabilities.domain_factories.role.make_role` |
| User | limited-role account | `capabilities.domain_factories.user.make_user` |
| Auth | limited login | `auth.e2e_limited_user_login` |

## Preconditions

| Case ID | Preconditions | Resolution |
|---------|---------------|------------|
| TC_E2E_AUTH_REJECT | Limited-role user exists | `capabilities.adapters.e2e.auth.seed_limited_user` |

## Data Setup Strategy

Seed the limited user through the shared domain factories and E2E auth adapter declared in `.aa/data-knowledge.yaml`.

## Capability Mapping

| Need | Capability | Source | Status |
|------|------------|--------|--------|
| Limited user seed | `capabilities.adapters.e2e.auth.seed_limited_user` | `.aa/data-knowledge.yaml` | found |
| Limited login | `auth.e2e_limited_user_login` | `.aa/data-knowledge.yaml` | found |
| Role factory | `capabilities.domain_factories.role.make_role` | `.aa/data-knowledge.yaml` | found |
| User factory | `capabilities.domain_factories.user.make_user` | `.aa/data-knowledge.yaml` | found |

## Runtime Verify Strategy

Confirm the limited user can authenticate before navigation.

## Login / Auth State Strategy

Per-test limited-user login via `auth.e2e_limited_user_login`.

## Cleanup Strategy

| Case ID | Cleanup |
|---------|---------|
| TC_E2E_AUTH_REJECT | `capabilities.adapters.e2e.auth.cleanup_limited_user` |

## No Data Required Cases

None.

## Blockers / Assumptions

None.

## Review Checklist

- [ ] Capability leaves resolve in `.aa/data-knowledge.yaml`
