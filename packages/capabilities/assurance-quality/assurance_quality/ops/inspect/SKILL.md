# Inspect

Use only locked evidence and projection digests; never invent failure
categories or write a runtime ledger or orchestration state file.

Capability-owned inspect skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Authenticate closed execution, trace, coverage, metrics, sufficiency, and fact-baseline projections,
then return a typed inspection result. Schema truth is `assurance_quality.contracts`
for `InspectionResultV1`. Classification of failures is owned by the deterministic
inspect handler; this skill verifies closure and does not invent categories.

## Inputs

### required

- locked change and batch identity
- exact execution, trace, coverage, metrics, and optional healing digests
- the committed fact-baseline ref for this assessment
- closed execution evidence already materialized by the graph

## Outputs

### required

- structured `InspectionResultV1`
- `inspect_mode` is `primary`
- projection digests echo the locked values exactly
- absent healing evidence is echoed as `null`

## Rules

- Do not classify failures yourself. The deterministic inspect handler is the classifier.
- Execution test failures do not make the Inspect operation itself failed.
  When the authenticated execution evidence contains failures and the locked
  evidence and all projection digests can be analyzed, return
  `status="analyzed"` and `classification_performed=true`; the deterministic
  handler will classify those failures.
- When the authenticated execution evidence contains no failures, return
  `status="no_failures"` and `classification_performed=true`.
- Use `status="failed"` only when the locked evidence cannot be authenticated or analyzed.
- Do not fabricate a quality gate or rewrite `final_status`.
- Do not emit `coverage_state`, `disposition`, `route`, or another workflow action.
- Inspect labels such as `known_product_issue` and `coverage_gap` are classification
  hints only. They do not create Problems or mutate Ledgers.
- Do not write product trees, tests, cases, plans, or healing files.
- Do not emit provider session transcripts or secret-bearing diagnostics.
- Use the locked execution binding from the prepare request.
- Write the typed result to `qa/results/inspect/inspection.json`.
- Return the typed result and stop.
