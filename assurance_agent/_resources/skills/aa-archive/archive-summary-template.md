# Archive Summary: {change_id}

---
archived_at: {datetime}
archived_by: {agent|human}
archive_status: archived | archived_with_warnings
---

## Change Summary

{Brief description of what this change covered}

## Case Delta Applied

| Action | Count | Case IDs |
|--------|-------|----------|
| ADDED | {n} | {ids} |
| MODIFIED | {n} | {ids} |
| REMOVED | {n} | {ids} |

## Test Files Created or Updated

- `tests/api/{module}/test_{file}_api.py`
- `tests/api/{module}/helpers/{file}_api.py`
- `tests/testdata/domain/{entity}.py`
- `tests/api/adapters/{module}.py`
- `tests/e2e/adapters/{module}.py`
- `tests/e2e/test_{module}_e2e.py`

## Execution Results

| Type | Status | Passed | Failed | Skipped |
|------|--------|--------|--------|---------|
| API  | PASS / PASS_WITH_WARNINGS / FAIL / SKIPPED / not_run | {n} | {n} | {n} |
| E2E  | PASS / PASS_WITH_WARNINGS / FAIL / SKIPPED / not_run | {n} | {n} | {n} |

Workflow execution status: `{phases.execution.status}`

Batch ID: `{batch_id}` (full results: `execution/runs/{batch_id}/`)

## Review Outcome

| Layer | Status |
|---|---|
| Case Review | PASS |
| API Plan Review | PASS / N/A |
| E2E Plan Review | PASS / N/A |
| Inspect | done / partial / not_run |

## Known Product Issues

<!-- Fill in if archive_status == archived_with_warnings, otherwise write "None" -->

| ID | Module | Endpoint | Workaround | Coverage Gap | Status |
|---|---|---|---|---|---|
| {KPI-001} | {module} | {METHOD /path} | {description} | open | open |

Source: `qa/changes/{change_id}/known-product-issues.md` (snapshots in `execution/` and `inspect/`)

## Archived Artifacts

- `qa/archive/{change_id}/cases/`
- `qa/archive/{change_id}/plans/`
- `qa/archive/{change_id}/review/`
- `qa/archive/{change_id}/execution/`
- `qa/archive/{change_id}/inspect/`
- `qa/archive/{change_id}/workflow-state.yaml`
- `qa/archive/{change_id}/events.jsonl`
- `qa/archive/{change_id}/healing/`

## Notes

{Any follow-up items, observations, or next actions to resolve known product issues}
