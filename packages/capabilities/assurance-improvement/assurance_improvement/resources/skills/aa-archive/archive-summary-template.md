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
- `tests/e2e/test_{module}_e2e.py`

## Execution Results

| Type | Status | Passed | Failed | Skipped |
|------|--------|--------|--------|---------|
| API  | PASS / PASS_WITH_WARNINGS / FAIL / SKIPPED / not_run | {n} | {n} | {n} |
| E2E  | PASS / PASS_WITH_WARNINGS / FAIL / SKIPPED / not_run | {n} | {n} | {n} |

Batch ID: `{batch_id}`

## Review Outcome

| Layer | Status |
|---|---|
| Case Review | PASS |
| API Plan Review | PASS / N/A |
| E2E Plan Review | PASS / N/A |
| Inspect | done / partial / not_run |

## Issue Risk

Fill from the authenticated quality report issues section. Write "None" when
issues is absent or `issue_risk` is `clear`.

issue_risk: {unknown|critical|high|medium|low|clear}
issue_risk_rationale: {rationale from the quality report}
open_problem_count: {count of active Problems linked to this Change}

## Archived Artifacts

- `qa/archive/{change_id}/cases/`
- `qa/archive/{change_id}/plans/`
- `qa/archive/{change_id}/review/`
- `qa/archive/{change_id}/execution/`
- `qa/archive/{change_id}/inspect/`
- `qa/archive/{change_id}/issues/` (if present)
- `qa/archive/{change_id}/report/` (if present)
- `qa/archive/{change_id}/events.jsonl`
- `qa/archive/{change_id}/healing/`

## Notes

{Any follow-up items or next actions}
