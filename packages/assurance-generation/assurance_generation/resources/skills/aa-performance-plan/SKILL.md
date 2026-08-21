# Performance plan

Capability-owned performance plan skill. Do not select a provider, model, or
adapter.

Turn the approved Performance portion of a reviewed case document into
reviewable implementation plans. Schema truth is `assurance_generation.contracts`
for the plan result and `assurance_intake.contracts` for reviewed cases.

## Inputs

### required

- `qa/changes/<change-id>/cases/**/case.yaml`
- `qa/changes/<change-id>/proposal.md`

### optional

- `qa/changes/<change-id>/facts/fact-baseline.json`
- `.aa/config.yaml`
- `.aa/data-knowledge.yaml`
- product source under the project source root (read-only)
- `tests/perf/**`
- `tests/testdata/domain/**`

## Outputs

### required

- `qa/changes/<change-id>/plans/performance-plan.md`
- `qa/changes/<change-id>/plans/performance-codegen-plan.md`
- `qa/changes/<change-id>/plans/performance-codegen-mapping.json`
- `qa/changes/<change-id>/plans/performance-review-summary.md`

## Boundaries

Write only the plan artifacts listed in Outputs. Do not write tests or continue
into codegen. The graph owns phase state. Do not write an orchestration state
file.

## Domain Notes

Task Mapping uses `Case ID | Task Method | Target File`.

Task Mapping is a strict one-to-one execution-entry relation:

- Both `performance-plan.md` and `performance-codegen-plan.md` must each contain
  an explicit `## Task Mapping` table with those exact headers.
- Emit exactly one Task Mapping row for each selected Performance Case ID.
- Map that row to the primary executable load-test task method under
  `tests/perf/**`.
- Never add separate Task Mapping rows for setup, cleanup, seed helpers,
  factories, adapters, or support functions.
- A Case ID repeated in Task Mapping is invalid even when the method or target
  file differs.

Every setup lifecycle must document a concrete positive seed and how each field
was validated against the frozen OpenAPI/fact evidence. Never assume a reserved
domain such as `example.test` satisfies an email validator.

Inspect the declared router response construction and existing load-test
support code before specifying measured assertions. Preserve the observed
envelope level.

Valid example:

```markdown
## Task Mapping

| Case ID | Task Method | Target File |
|---|---|---|
| TC_ACCOUNT_PERF_001 | AccountListUser.list_accounts | tests/perf/locustfile_account.py |

## Seed Lifecycle

| Setup | Cleanup | Support Module |
|---|---|---|
| setup_account | cleanup_account | tests/perf/adapters/account_seed.py |
```

Factory Mapping section (required):

```markdown
## Factory Mapping

| Shared Module | Function | Ownership |
|---|---|---|
| tests/testdata/domain/account.py | make_account | reuse |
```

The typed plan result must include scenario identity and numeric thresholds
(`p95_ms`, `error_rate_max`). Every planned case must have operation and risk
coverage. Capability keys must be exact typed leaves.
