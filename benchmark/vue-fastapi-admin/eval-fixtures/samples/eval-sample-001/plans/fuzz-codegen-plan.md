# Fuzz Codegen Plan — eval-sample-001

## Target Files

| File | Purpose |
|------|---------|
| `tests/fuzz/test_api_fuzz.py` | FUZZ-001 schemathesis entrypoint (create-if-missing) |
| `tests/fuzz/strategies/api.py` | Reusable API payload generation (create-if-missing) |
| `tests/fuzz/adapters/api.py` | Stateful setup/cleanup transport via L1 capabilities (reuse) |

## Test Function Mapping

| Case ID | Test Function | Target File |
|---------|---------------|-------------|
| FUZZ-001 | `test_fuzz_001__api_create_schema` | `tests/fuzz/test_api_fuzz.py` |

## Schema Acquisition

| Case ID | Strategy | Import Path |
|---------|----------|-------------|
| FUZZ-001 | `from_asgi` | `app.main:app` |

## Auth Strategy

Reuse `auth.api_admin_token` declared in `.aa/data-knowledge.yaml`; fuzz adapter wraps token acquisition only.

## Factory Mapping

| Shared Module | Function | Ownership |
|---|---|---|
| tests/testdata/domain/api.py | make_api | reuse |

## Capability Mapping

| Case ID | Domain Factory | Fuzz Adapter | Cleanup |
|---------|----------------|--------------|---------|
| FUZZ-001 | `capabilities.domain_factories.api.make_api` | isolated_worker seed | manifest cleanup |

## Generated File Policy

Append new cases to `tests/fuzz/test_api_fuzz.py`; do not overwrite existing fuzz tests.

## Codegen Preconditions

- `review/fuzz-plan-review.json` `decision == "pass"`
- `.aa/data-knowledge.yaml` exists
