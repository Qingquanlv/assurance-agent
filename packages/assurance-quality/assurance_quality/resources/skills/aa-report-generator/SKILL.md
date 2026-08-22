# Report generator

Capability-owned report-generator skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Authenticate every referenced quality projection, then return a typed report result.
Schema truth is `assurance_quality.contracts` for `ReportResultV1`. The deterministic
generate-report handler owns `quality_score` and `final_status`.

## Inputs

### required

- locked case, plan, mapping, execution, healing, trace, coverage, issue, and metrics digests
- quality-gate and failure-analysis projections already materialized by the graph

## Outputs

### required

- structured `ReportResultV1`
- every source digest equals the locked projection digest

## Rules

- Never recompute or change `quality_score` or `final_status`.
- Never copy `issue_risk` into `final_status`.
- Wording may refine `risk_rationale` or `recommendation` but must not claim a
  safe release when `final_status` is `FAIL` or an unresolved product defect exists.
- Do not write minimum-coverage JSON; that projection is owned by a deterministic handler.
- Do not emit provider session transcripts or secret-bearing diagnostics.
- Use the locked execution binding from the prepare request.
- Return the typed result and stop.
