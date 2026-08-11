# Task 6 Resolution — Narrow to generated-files validator

**Choice:** B  
**Date:** 2026-08-01  
**Controller tip at decision:** 23cecbb

## In scope for Task 6
- `PrecommitValidationContext`, `CandidateValidationReceiptV1`
- Closed registry with `generated_files_candidate/v1` implemented
- `codegen_fix_candidate/v1` may be registered as a known closed ID but must load-reject / remain unimplemented until Task 8 (do not invent `FixerAuthorityV1`)
- Mapping extraction (`generated_entries.py`), receipt CAS store/verify, scheduler hook between freeze and success, fold/validate receipt identity
- Scheduler no-commit tests for `generated_files_candidate/v1`
- Dark-ship: `precommit_validator` defaults None; packaged contracts select neither validator

## Deferred to Task 8
- `FixerAuthorityV1` and proposal path-subset surface
- Full `codegen_fix_candidate/v1` algorithm + Step 3 mutation suite
- Scheduler no-commit cases specific to codegen_fix
- Diff-safety predicates that depend on authority/proposal paths

## Secondary clarifications (for Task 6 generated-files path)
1. Proposal path authority: N/A for generated-files validator; wait for Task 8.
2. `PrecommitValidationContext` fields: populate from available scheduler/projection/CAS identities in dark-ship tests; use fixture/test-contract values for policy/definition_semantics when live bindings are Task 9/10/15.
3. Diff-safety helpers for codegen_fix: Task 8 / later; not required in Task 6.

## Policy
Do not invent `FixerAuthorityV1` or stub empty authority classes in Task 6.
