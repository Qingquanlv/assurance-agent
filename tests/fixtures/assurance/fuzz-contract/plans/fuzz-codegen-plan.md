# Fuzz Codegen Plan — CH-CANONICAL

## Target Files

| File | Purpose |
|------|---------|
| `tests/fuzz/test_accounts_fuzz.py` | FUZZ-001 schemathesis entrypoint (create-if-missing) |
| `tests/fuzz/strategies/account.py` | Reusable account payload generation (create-if-missing) |
| `tests/fuzz/adapters/account.py` | Stateful setup/cleanup transport via L1 capabilities (create-if-missing) |

## Schema Acquisition Strategy

| Case ID | Strategy | Import Path |
|---------|----------|-------------|
| FUZZ-001 | `from_asgi` | `app.main:app` |

## Auth Strategy

Reuse `auth.api_admin_token` declared in `.aa/data-knowledge.yaml`; fuzz adapter wraps token acquisition only.

## Factory Mapping

| Shared Module | Function | Ownership |
|---|---|---|
| tests/factories/account.py | make_account | reuse |

## Capability Mapping

| Case ID | Domain Factory | Fuzz Adapter | Cleanup |
|---------|----------------|--------------|---------|
| FUZZ-001 | `capabilities.domain_factories.account.make_account` | isolated_worker seed | manifest cleanup |

## Generated File Policy

Append new cases to `tests/fuzz/test_accounts_fuzz.py`; do not overwrite existing fuzz tests.

## Codegen Preconditions

- `review/fuzz-plan-review.json` `decision == "pass"`
- `.aa/data-knowledge.yaml` exists
