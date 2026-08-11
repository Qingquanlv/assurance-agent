# API Test Data Plan — CH-CANONICAL

## Scope

| Case ID | Title |
|---------|-------|
| API-ACC-001 | Create account succeeds for admin |

## Required Data

| Entity | State | Capability |
|--------|-------|------------|
| Account | creatable | `capabilities.domain_factories.account.make_account` |

## Preconditions

| Case ID | Preconditions | Resolution |
|---------|---------------|------------|
| API-ACC-001 | Admin token available | `auth.api_admin_token` in `.aa/data-knowledge.yaml` |

## Capability Mapping

| Need | Capability | Source | Status |
|------|------------|--------|--------|
| Account factory | `capabilities.domain_factories.account.make_account` | `.aa/data-knowledge.yaml` | found |
| Admin auth | `auth.api_admin_token` | `.aa/data-knowledge.yaml` | found |

## Factory / Boundary Strategy

| Entity | Ring | Preferred method | Notes |
|--------|------|------------------|-------|
| Account | domain | make_account | reuse L1 shared module |

## No Data Required Cases

None.

## Blockers / Assumptions

None.
