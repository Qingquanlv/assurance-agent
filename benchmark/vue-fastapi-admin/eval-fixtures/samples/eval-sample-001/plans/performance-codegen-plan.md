# Performance Codegen Plan — eval-sample-001

## Target Files

| File | Purpose |
|------|---------|
| `tests/perf/locustfile_api.py` | PERF-001 Locust tasks (create-if-missing) |
| `tests/perf/adapters/api_seed.py` | Bulk API seed/cleanup via L1 capabilities (reuse) |

## Auth Strategy

Reuse `auth.api_admin_token` from `.aa/data-knowledge.yaml`; Locust user on_start acquires token through performance adapter only.

## Factory Mapping

| Shared Module | Function | Ownership |
|---|---|---|
| tests/testdata/domain/api.py | make_api | reuse |

## Seed Mapping

| Case ID | Batch Size | Domain Factory | Cleanup |
|---------|------------|----------------|---------|
| PERF-001 | 50 | `capabilities.domain_factories.api.make_api` | manifest cleanup via adapter |

## Task Mapping

| Case ID | Task Method | Target File |
|---------|-------------|-------------|
| PERF-001 | `list_apis` | `tests/perf/locustfile_api.py` |

## Generated File Policy

Append new scenarios; do not overwrite existing Locust files.

## Codegen Preconditions

- `review/performance-plan-review.json` `decision == "pass"`
- `.aa/data-knowledge.yaml` exists
