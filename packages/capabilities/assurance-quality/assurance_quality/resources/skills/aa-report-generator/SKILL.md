# Report generator

Capability-owned report-generator skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Authenticate every referenced quality projection, then return a typed report result.
Schema truth is `assurance_quality.contracts` for `ReportResultV1`. The deterministic
generate-report handler owns `quality_score` and `final_status`.

## Inputs

### required

- the current Inspect outcome, its receipt, Reviewed Case, generation mapping, and assessment refs
- locked case, plan, mapping, execution, healing, trace, coverage, issue, and metrics digests
- an explicit `normal` or `diagnostic` purpose

## Outputs

### required

- a Markdown report at `qa/results/report/report.md`
- structured `ReportResultV1` declaring that path in `report_files`
- every source digest equals the locked projection digest

## Rules

- Never recompute or change `quality_score` or `final_status`.
- Do not invoke a browser, session, or delegation tool; the report is a bounded
  artifact projection, not a new orchestration run.
- Never copy `issue_risk` into `final_status`.
- Wording may refine `risk_rationale` or `recommendation` but must not claim a
  safe release when `final_status` is `FAIL` or an unresolved product defect exists.
- Do not write minimum-coverage JSON; that projection is owned by a deterministic handler.
- Do not emit provider session transcripts or secret-bearing diagnostics.
- Use the locked execution binding from the prepare request.
- A normal report may describe only a satisfied Inspect outcome. A diagnostic report must
  retain the `diagnostic` purpose and must not claim normal success.
- Return the typed result after writing the declared report file, then stop.
