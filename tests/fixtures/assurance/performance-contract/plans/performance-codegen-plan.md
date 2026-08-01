# Performance Codegen Plan — CH-CANONICAL

## Target Files

| File | Purpose |
|------|---------|
| `tests/perf/locustfile_accounts.py` | PERF-001 Locust tasks (create-if-missing) |
| `tests/perf/adapters/account_seed.py` | Bulk account seed/cleanup via L1 capabilities (reuse) |

## Auth Strategy

Reuse `auth.api_admin_token` from `.aa/data-knowledge.yaml`; Locust user on_start acquires token through performance adapter only.

## Factory Mapping

| Shared Module | Function | Ownership |
|---|---|---|
| tests/testdata/domain/account.py | make_account | reuse |

## Seed Mapping

| Case ID | Batch Size | Domain Factory | Cleanup |
|---------|------------|----------------|---------|
| PERF-001 | 50 | `capabilities.domain_factories.account.make_account` | manifest cleanup via adapter |

## Task Mapping

| Case ID | Task Method | Target File |
|---------|-------------|-------------|
| PERF-001 | `get_accounts` | `tests/perf/locustfile_accounts.py` |

## Generated File Policy

Append new scenarios; do not overwrite existing Locust files.

## Codegen Preconditions

- `review/performance-plan-review.json` `decision == "pass"`
- `.aa/data-knowledge.yaml` exists
