# Fuzz Plan — eval-sample-001

## Source

- `cases/system/fuzz/case.yaml`

## Scope

| Case ID | Title |
|---------|-------|
| FUZZ-001 | API create endpoint input robustness |

## Schema Source

| Case ID | Acquisition | Notes |
|---------|-------------|-------|
| FUZZ-001 | `from_asgi` against in-process FastAPI app | Prefer in-process OpenAPI; avoids live-service drift |

## Related Functional Case

| Fuzz Case ID | Related API Case | Purpose |
|--------------|------------------|---------|
| FUZZ-001 | TC_APIS_API_001 | Functional happy-path coverage remains authoritative; fuzz hardens input robustness only |

## Authentication Semantics

| Case ID | Auth Capability | Transport |
|---------|-----------------|-----------|
| FUZZ-001 | `auth.api_admin_token` from `.aa/data-knowledge.yaml` | Bearer token injected by fuzz adapter; no hardcoded secrets |

## Seed / Corpus Adequacy

| Case ID | Seed Corpus | Coverage |
|---------|-------------|----------|
| FUZZ-001 | `tests/fuzz/corpus/api_create.json` | Valid baseline payload plus boundary variants for required fields |

## Expectations

| Case ID | Checks |
|---------|--------|
| FUZZ-001 | no_5xx; valid_input_not_rejected; response_conforms_to_schema |
