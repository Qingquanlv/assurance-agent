# Codegen-review drops review_audit

Date: 2026-09-15

## Problem

API codegen-review still requires a model-authored `review_audit`: every
selected case × six checks, every helper row, exact `input_refs` digests,
and `planning_facts_digest`. Host prepare injects `review_requirements`
into the result schema as required. Finalize `repair`s some bookkeeping
then `validate`s the table; a missing or illegal table is retryable
`OutputError` and the whole node reruns.

That table is leftover plan-review. Case flow does not do this:
case-design writes `qa/results/trace/minimum-coverage-matrix.json`,
case-reviewer does not emit `review_audit`. Generation already has the
same split: case-design owns coverage obligations; codegen finalize
already locks exact files and requires mapping `case_ids` to match
`scope.case_ids`.

The live cost is a 20+ minute review that can pass semantically and still
retry because the coverage ledger does not copy.

## Decision

**A.** Delete `review_audit` from codegen-review. The model does not write
a coverage table. Case-design’s matrix stays the coverage obligation.
Codegen finalize keeps the existing path lock. Reviewer keeps only
semantic routing: `route`, `finding_ids`, `findings`.

Do not move the table into case-design. Case-design cannot cite
generated-file evidence. Do not host-build a replacement ledger.

## Reviewer contract

All four families (`api`, `e2e`, `fuzz`, `performance`) stop emitting
`review_audit`.

`PlanReviewAuthoring` / `Review` drop the field. The result schema
required list stays:

- `schema_version`
- `review_type`
- `change_id`
- `route`
- `findings`
- `finding_ids`
- `next_action`
- `risk_level`
- `required_capabilities`

`route` + `finding_ids` stay host-validated, no rewrite. Illegal pairs
remain retryable `OutputError`.

`review_audit` is removed, not left optional. A leftover field in a
model payload is dropped at finalize and is not invalid output.

## Prepare

`result_contract` no longer adds `review_audit` to `required` when
`review_requirements` is present.

Prepare no longer injects `review_requirements` into the result schema
or the instruction JSON. The skill may still name selected `case_ids`
and the locked test / testdata / mapping paths as read-only context,
the way case-reviewer reads cases. It must not ask for a table back.

`api_review_requirements` has no remaining caller after this change.

## Finalize

`PlanReviewFinalizeHandler` validates `PlanReviewAuthoring` without an
audit. It does not call `repair_api_review_audit` or
`validate_api_review_audit`. A missing table is success, not
`OutputError`.

If the payload still contains `review_audit`, strip it before
validation. Do not fail the attempt for that leftover.

Sealing `qa/results/review/<family>-codegen-review.json` stays. The
sealed document is the semantic review, not a repaired coverage ledger.

Finding-scope write, history ref, `public_outcome`, and
`route` / `finding_ids` policy stay.

## Skills

`aa-api-codegen-reviewer` drops the “Verifiable review coverage” section
and every “return `review_audit` / copy digests / one row per helper”
instruction. It still reviews frozen-case request, auth, setup,
assertion, and cleanup against the locked generated files, and still
emits `route` + `finding_ids`.

Plan leftovers go with it: `plan_location`, planner re-entry, “do not
edit plan files” as the repair target. Bounded `auto_fix` findings
point at generated tests / mapping, not a deleted plan package.

`aa-e2e-codegen-reviewer`, `aa-fuzz-codegen-reviewer`, and
`aa-performance-codegen-reviewer` already omit `review_audit`. Do not
add it. Strip any plan-era repair wording that still names plan
artifacts as the write-back target.

Outputs stay the review JSON and summary Markdown only.

## Dead code

Remove unused audit machinery once nothing imports it:

- `assurance_generation/operations/review_audit.py`
- `assurance_generation/contracts/review_audit.py`
- `PlanReviewAudit` and related models
- `$defs` for `PlanReviewAudit` / case-check / helper-evidence rows in
  `plan-review.v1.schema.json`
- `review_requirements` wiring in `planning.result_contract` and
  `prepare_plan_outcome`

## Tests

Delete tests whose only job is the coverage table:

- `test_review_audit.py`
- `test_review_audit_repair.py`
- fixtures that exist only to attach a valid `review_audit`

Update `test_plan_review.py` and contract tests:

- drop “`review_audit` is required”
- add: a passing API review with no `review_audit` finalizes
- add: a payload that still includes `review_audit` finalizes after
  the field is stripped
- keep existing `route` / `finding_ids` tests

Do not change locked-outputs tests, graph topology tests, or
`_ATTEMPT_PATHS`.

## Out of scope

- Graph topology (`codegen → codegen-review → done`)
- Codegen locked outputs and mapping `case_ids` equality
- Case-design / `minimum-coverage-matrix.json`
- The in-flight live item
  `opencode-ret-dept-management` /
  `BENCH-opencode-ret-dept-management-20260915-124401-e3b28177`

## Success

1. API (and every other family) codegen-review finalize succeeds
   without `review_audit`.
2. Prepare result schema does not list `review_audit` as required.
3. Four reviewer skills do not tell the model to emit the table.
4. `review_audit` modules have no remaining production importers.
5. Reviewer still routes on `route` + `finding_ids` only.
