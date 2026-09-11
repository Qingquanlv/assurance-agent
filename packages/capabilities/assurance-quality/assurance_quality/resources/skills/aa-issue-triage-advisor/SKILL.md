# Issue triage

Capability-owned issue-triage skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Read a Problem projection and its locked evidence, then recommend one declared
human action. Schema truth is `assurance_quality.contracts` for `IssueTriageResultV1`.

## Inputs

### required

- target `problem_id` and `expected_problem_version`
- locked evidence digest map
- declared action set for this interrupt

## Outputs

### required

- structured `IssueTriageResultV1`
- echo `problem_id`, `expected_problem_version`, and `evidence_digests` exactly
- recommend exactly one declared action

## Declared actions

`confirm_assessment`, `mark_not_an_issue`, `accept_risk`, `start_work`, `reopen`, `merge`, `stop`.

## Rules

- Do not emit canonical events or mutate Problems or Ledgers.
- Do not invent Problem state.
- Summarize observable facts; do not include raw logs or secrets.
- Use the locked execution binding from the prepare request.
- Write the typed result to `qa/results/inspect/issue-triage.json`.
- Return the typed result and stop.
