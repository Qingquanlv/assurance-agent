# Task 4 Report: Delete unused audit modules

**Status:** DONE_WITH_CONCERNS

**Commit:** `858f3973` `refactor(generation): remove unused codegen-review audit modules`

**Branch:** `codex/durable-qa-execute` (in-place, no worktree)

## What you implemented

Deleted the unused codegen-review audit modules and dropped the leftover `review_audit` field from contracts and both plan-review schemas.

- Added `test_review_audit_modules_are_gone` and `test_plan_review_schema_has_no_review_audit` to `test_plan_review.py` (verbatim from the brief).
- `git rm` of `assurance_generation/operations/review_audit.py` and `assurance_generation/contracts/review_audit.py`.
- In `contracts/reviews.py`, removed `from assurance_generation.contracts.review_audit import PlanReviewAudit` and both `review_audit: PlanReviewAudit | None = None` fields on `Review` and `PlanReviewAuthoring`.
- Regenerated both `plan-review.v1.schema.json` copies from `PlanReviewAuthoring.model_json_schema()` so they stay canonical with the model. Deleted `review_audit`, `$defs` `PlanReviewAudit` / `CaseReviewChecks` / `CaseReviewCoverage` / `HelperReviewEvidence` / `HelperPlanLocation`, and unused `EvidenceArtifactRefV1`.
- Updated the locked `assurance.generation.schema.plan-review.v1` digest in `test_contracts.py` to `7dc96dc5ee821aaf8f6e1e0adbd19ec563b9f8594ca20f96af5dae003a993ca8`.
- Left `tests/review_audit_fixtures.py` named as-is. It already only exports the still-imported helpers (`write_codegen_artifacts`, `review_prepare_input`, `write_review`).

Did not change graph topology, `_ATTEMPT_PATHS`, codegen locked outputs, `planning.py`, or `review.py`. Did not touch the in-flight live item or OpenCode on 4096.

## What you tested and test results

```
uv run pytest packages/capabilities/assurance-generation/tests/test_plan_review.py packages/capabilities/assurance-generation/tests/test_contracts.py packages/capabilities/assurance-generation/tests/test_resources.py packages/capabilities/assurance-generation/tests/test_plan_review_policy.py packages/capabilities/assurance-generation/tests/test_plan_review_routing.py -v
```

**Result:** 139 passed in 1.37s.

## TDD Evidence

### RED

```
uv run pytest packages/capabilities/assurance-generation/tests/test_plan_review.py::test_review_audit_modules_are_gone packages/capabilities/assurance-generation/tests/test_plan_review.py::test_plan_review_schema_has_no_review_audit -v
```

**Result:** 2 failed (exit 1), matching the brief.

- `test_review_audit_modules_are_gone` failed because `ModuleNotFoundError` was not raised (`DID NOT RAISE ModuleNotFoundError`).
- `test_plan_review_schema_has_no_review_audit` failed because `review_audit` was still in `schema["properties"]`.

```
FAILED ...::test_review_audit_modules_are_gone
FAILED ...::test_plan_review_schema_has_no_review_audit
============================== 2 failed in 0.89s ===============================
```

### GREEN

Same command as the named test set above.

**Result:** 139 passed in 1.37s, including both new tests.

## Files changed

Committed in `858f3973` (7 files, +164 / −469):

- `packages/capabilities/assurance-generation/assurance_generation/operations/review_audit.py` (deleted)
- `packages/capabilities/assurance-generation/assurance_generation/contracts/review_audit.py` (deleted)
- `packages/capabilities/assurance-generation/assurance_generation/contracts/reviews.py`
- `packages/capabilities/assurance-generation/assurance_generation/resources/result-contracts/plan-review.v1.schema.json`
- `packages/capabilities/assurance-generation/assurance_generation/resources/schemas/plan-review.v1.schema.json`
- `packages/capabilities/assurance-generation/tests/test_plan_review.py` (+30, the two new tests)
- `packages/capabilities/assurance-generation/tests/test_contracts.py` (schema digest + pre-existing dirty routing assertions; see concerns)

`tests/review_audit_fixtures.py` was listed but unchanged, so it is not in the commit.

## Self-review findings

| Spec requirement | Result |
|---|---|
| Finalize succeeds without `review_audit` | Covered by existing Task 1 tests; still passing |
| Leftover `review_audit` is stripped | Covered by existing Task 1 tests; still passing |
| Prepare schema does not require `review_audit` | Covered by existing Task 2 test; still passing |
| Prepare does not inject `review_requirements` | Covered by existing Task 2 test; still passing |
| Four skills do not tell the model to emit the table | Covered by existing Task 3 tests; still passing |
| `review_audit` modules have no production importers | Modules deleted; importer tests fail closed |
| `Review.review_audit` / `PlanReviewAuthoring.review_audit` gone | Removed |
| Schema has no `PlanReviewAudit` `$defs` | Both copies regenerated; `$defs` empty |
| `route` + `finding_ids` unchanged | Existing routing tests still pass |
| Graph / locked outputs / live item untouched | No graph / `_ATTEMPT_PATHS` / locked-output edits |

Production grep after the delete:

- `api_review_requirements`, `PlanReviewAudit`, `review_requirements`: zero production hits.
- `review_audit`: two leftover-strip strings remain in `operations/review.py` (`raw.pop("review_audit", None)` and `exclude={..., "review_audit"}`). Those are Task 1 leftover handling, not unused modules, and `review.py` was not a named file for this task.

Tests mention the strings only in “must be absent” assertions, leftover-strip fixtures, and the fixture filename `review_audit_fixtures`.

## Issues or concerns

1. **Pre-existing dirty hunks in named files.** `reviews.py` and `test_contracts.py` were already modified on the working tree when this task started (routing rewrite from `decision` / `auto_fix_*` to `route` / `finding_ids`). Staging the named files included those earlier uncommitted hunks along with the Task 4 audit-field deletion. I did not amend. The extra hunks are not graph / `_ATTEMPT_PATHS` / locked-output edits, but they are larger than this task’s intended field drop.

2. **Production still mentions `review_audit` in `review.py`.** Task 1 leftover-strip / exclude strings remain. Removing them would break leftover-payload stripping because `PlanReviewAuthoring` still has `extra="allow"`. Left untouched because that file is outside this task’s named set.

3. **Unused `review_input_images` in `planning.py`.** Leftover from Task 2. Did not expand into `planning.py`; it does not import `review_audit`.
