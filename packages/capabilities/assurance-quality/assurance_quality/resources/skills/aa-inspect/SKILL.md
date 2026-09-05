# Inspect

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
- Do not fabricate a quality gate or rewrite `final_status`.
- Do not emit `coverage_state`, `disposition`, `route`, or another workflow action.
- Inspect labels such as `known_product_issue` and `coverage_gap` are classification
  hints only. They do not create Problems or mutate Ledgers.
- Do not write product trees, tests, cases, plans, or healing files.
- Do not emit provider session transcripts or secret-bearing diagnostics.
- Use the locked execution binding from the prepare request.
- Write the typed result to `qa/changes/<change-id>/inspect/inspection.json`.
- Return the typed result and stop.
