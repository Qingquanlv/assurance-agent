# Inspect

Capability-owned inspect skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Authenticate closed execution, healing, trace, coverage, and metrics projections,
then return a typed inspection result. Schema truth is `assurance_quality.contracts`
for `InspectionResultV1`. Classification of failures is owned by the deterministic
inspect handler; this skill verifies closure and does not invent categories.

## Inputs

### required

- locked change and batch identity
- exact execution, healing, trace, coverage, and metrics digests
- closed execution evidence already materialized by the graph

## Outputs

### required

- structured `InspectionResultV1`
- `inspect_mode` is `primary`
- projection digests echo the locked values exactly

## Rules

- Do not classify failures yourself. The deterministic inspect handler is the classifier.
- Do not fabricate a quality gate or rewrite `final_status`.
- Inspect labels such as `known_product_issue` and `coverage_gap` are classification
  hints only. They do not create Problems or mutate Ledgers.
- Do not write product trees, tests, cases, plans, or healing files.
- Do not emit provider session transcripts or secret-bearing diagnostics.
- Use the locked execution binding from the prepare request.
- Return the typed result and stop.
