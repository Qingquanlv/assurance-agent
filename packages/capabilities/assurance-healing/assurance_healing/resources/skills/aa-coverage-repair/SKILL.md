# Coverage repair

Capability-owned coverage-repair skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Close briefed coverage gaps by editing tests named in the brief. Schema truth is
`assurance_healing.contracts` for `CoverageRepairBrief` and
`CoverageRepairApplySummary`.

## Inputs

### required

- `CoverageRepairBrief` with `repair_items` and `allowed_test_files`
- attempt baseline identity
- allowed test roots

## Outputs

### required

- structured `CoverageRepairApplySummary`
- `files_modified` is an honest subset of `brief.allowed_test_files`
- `addressed_items` refer back to brief locators

## Rules

- Work only on `brief.repair_items` and files named by `brief.allowed_test_files`.
- Do not create undeclared test files or invent locators.
- Do not edit product code or declaration trees.
- Do not add skip or xfail markers.
- Do not hide an unresolved import by moving it outside the allowed file set.
- Prefer the typed summary over any remembered conversation state.
- Write the typed result to `qa/results/healing/coverage-repair.json`.
- Return the typed result and stop.
