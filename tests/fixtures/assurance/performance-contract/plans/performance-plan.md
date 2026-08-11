# Performance Plan — CH-CANONICAL

## Source

- `cases/PERF-001/case.yaml`

## Scope

| Case ID | Title |
|---------|-------|
| PERF-001 | Account list endpoint load threshold |

## Absolute Thresholds

| Case ID | p95_ms | error_rate_max | Interpretation |
|---------|--------|----------------|----------------|
| PERF-001 | 500 | 0.01 | Pass/fail against fixed user-confirmed absolutes; no historical baseline comparison |

## Load Shape

| Case ID | users | spawn_rate | run_time_s |
|---------|-------|------------|------------|
| PERF-001 | 50 | 5 | 120 |

## Scenario Coverage

| Case ID | Capability / Endpoint | Weight |
|---------|----------------------|--------|
| PERF-001 | GET /accounts | 100 |

## Statistical Interpretation

| Case ID | Metric | Rule |
|---------|--------|------|
| PERF-001 | p95 latency | Locust aggregated p95 over full run must be <= `p95_ms` |
| PERF-001 | error rate | failed requests / total requests must be <= `error_rate_max` |

## Target Environment

Base URL comes from `.aa/config.yaml`; auth uses `auth.api_admin_token` declared in `.aa/data-knowledge.yaml`.

## Out of Scope

Performance does not replace functional API/E2E coverage and does not compare against prior baselines.
