# Task 3 Report: Reviewer skills drop the coverage table

**Status:** DONE_WITH_CONCERNS

**Commit:** `d5ce26da` `docs(generation): drop review_audit from codegen-reviewer skills`

**Branch:** `codex/durable-qa-execute` (in-place, no worktree)

## What you implemented

Four `aa-*-codegen-reviewer` skills no longer tell the model to emit `review_audit`, copy `review_requirements` digests, or write back to a plan package.

- Added `test_codegen_reviewer_skills_do_not_require_review_audit` in `test_plan_review.py` (verbatim from the brief).
- `aa-api-codegen-reviewer/SKILL.md`:
  - Deleted the `## Verifiable review coverage` heading and the helper-row / `unresolved` / `plan_location` / `review_requirements` instructions.
  - Kept the retained semantic paragraphs after that cut (`An upper bound is not an exact length`, first-decision exact-read of locked generated tests and mapping) so existing `test_resources.py` assertions still match.
  - Replaced “every required plan artifact” with “every locked generated test, testdata file, and mapping”.
  - Replaced the Outputs paragraph so it names `route` and `finding_ids` only (no `review_audit`, no audit evidence-path membership).
  - Replaced the Boundaries section with the brief’s generated-test/mapping wording.
- `aa-e2e-codegen-reviewer`, `aa-fuzz-codegen-reviewer`, and `aa-performance-codegen-reviewer`: replaced the Boundaries “edit plan files” / “exact plan artifact” / “planner can revise” sentences with the same generated-test/mapping wording. Did not add `review_audit`. Kept family-prefixed output paths and family-specific checks-file notes.

Did not change graph topology, `_ATTEMPT_PATHS`, codegen locked outputs, or `test_resources.py`.

## What you tested and test results

```
uv run pytest packages/capabilities/assurance-generation/tests/test_plan_review.py::test_codegen_reviewer_skills_do_not_require_review_audit packages/capabilities/assurance-generation/tests/test_plan_review.py::test_plan_reviewer_skills_do_not_instruct_removed_decisions packages/capabilities/assurance-generation/tests/test_resources.py -v
```

**Result:** 64 passed in 1.11s.

Existing `test_resources.py` assertions about `route: auto_fix` and SUT defects still match, including `test_plan_reviews_do_not_block_codegen_on_a_source_proven_sut_defect` and `test_e2e_plan_review_and_repair_are_exhaustive_within_one_round`.

## TDD Evidence

### RED

```
uv run pytest packages/capabilities/assurance-generation/tests/test_plan_review.py::test_codegen_reviewer_skills_do_not_require_review_audit -v
```

**Result:** 4 failed (exit 1), matching the brief.

- `aa-api-codegen-reviewer` failed first on `assert "review_audit" not in skill`.
- `aa-e2e-codegen-reviewer`, `aa-fuzz-codegen-reviewer`, and `aa-performance-codegen-reviewer` failed on `assert "edit plan files" not in skill`.

```
FAILED ...::test_codegen_reviewer_skills_do_not_require_review_audit[aa-api-codegen-reviewer]
FAILED ...::test_codegen_reviewer_skills_do_not_require_review_audit[aa-e2e-codegen-reviewer]
FAILED ...::test_codegen_reviewer_skills_do_not_require_review_audit[aa-fuzz-codegen-reviewer]
FAILED ...::test_codegen_reviewer_skills_do_not_require_review_audit[aa-performance-codegen-reviewer]
============================== 4 failed in 0.90s ===============================
```

### GREEN

Same command as Step 4 above.

**Result:** 64 passed in 1.11s (the four new skill cases plus `test_plan_reviewer_skills_do_not_instruct_removed_decisions` and all of `test_resources.py`).

## Files changed

Committed in `d5ce26da` (5 files, +756):

- `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-api-codegen-reviewer/SKILL.md` (new tracked file)
- `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-e2e-codegen-reviewer/SKILL.md` (new tracked file)
- `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-fuzz-codegen-reviewer/SKILL.md` (new tracked file)
- `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-performance-codegen-reviewer/SKILL.md` (new tracked file)
- `packages/capabilities/assurance-generation/tests/test_plan_review.py` (+17, the new parametrized skill test)

The four skill files were previously untracked on this branch; this commit introduces them as the codegen-reviewer skills.

## Self-review findings

- Forbidden strings are gone from all four skills: `review_audit`, `review_requirements`, `plan_location`, `edit plan files`.
- API Outputs paragraph matches the brief (schema fields are `route` and `finding_ids` only).
- API Boundaries section matches the brief verbatim.
- Non-API skills keep family-prefixed `*-codegen-review.json` / `*-codegen-review-summary.md` paths and do not mention `review_audit`.
- `route: auto_fix` and SUT-defect sentences are unchanged.
- E2E Domain Notes still contain “complete one exhaustive pass across every required plan artifact”, which `test_resources.py` still asserts. Only API was asked to replace that phrase.
- Graph topology, `_ATTEMPT_PATHS`, and codegen locked outputs were not touched.
- Commit scope is only the five named files.

## Issues or concerns

1. API skill still says “The audit makes omissions and source contradictions checkable” in the retained post-cut paragraph. That is not `review_audit` and does not fail the new test, but it is leftover coverage-ledger language.
2. Non-API skills still talk about plan artifacts outside Boundaries (`every required plan artifact`, `fuzz-codegen.md` / `performance-codegen.md`, “route the planner”). The brief only rewrote the Boundaries sentences; later cleanup may still be needed.
3. Non-API Proven Product Defects / Domain Notes still use older decision names (`needs_human_review`, `needs_fix`, `pass` / `ready_with_warnings`). Out of this task’s scope; `test_plan_reviewer_skills_do_not_instruct_removed_decisions` only forbids `"approved"` and `changes_requested`.
4. The four skill files were untracked before this commit, so the commit adds the full skill bodies rather than a small in-place diff against already-tracked files.
