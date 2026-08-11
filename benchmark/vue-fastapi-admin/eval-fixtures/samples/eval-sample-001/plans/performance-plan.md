# Performance Plan — eval-sample-001

## Source

- `cases/system/performance/case.yaml`

## Scope

| Case ID | Title |
|---------|-------|
| PERF_001 | API list endpoint load threshold |

## Absolute Thresholds

| Case ID | p95_ms | error_rate_max | Interpretation |
|---------|--------|----------------|----------------|
| PERF_001 | 500 | 0.01 | Pass/fail against fixed user-confirmed absolutes; no historical baseline comparison |

## Load Shape

| Case ID | users | spawn_rate | run_time_s |
|---------|-------|------------|------------|
| PERF_001 | 50 | 5 | 120 |

## Scenario Coverage

| Case ID | Capability / Endpoint | Weight |
|---------|----------------------|--------|
| PERF_001 | GET /api/v1/api/list | 100 |

## Statistical Interpretation

| Case ID | Metric | Rule |
|---------|--------|------|
| PERF_001 | p95 latency | Locust aggregated p95 over full run must be <= `p95_ms` |
| PERF_001 | error rate | failed requests / total requests must be <= `error_rate_max` |

## Target Environment

Base URL comes from `.aa/config.yaml`; auth uses `auth.api_admin_token` declared in `.aa/data-knowledge.yaml`.
