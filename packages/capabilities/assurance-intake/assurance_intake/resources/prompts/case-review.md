# Case-review prompt

Review the case-design artifacts and write the gate JSON.

Read locked `requirement.md`. Treat its explicit numerical thresholds and load values
as owner-confirmed. Exhaust the review: complete all review criteria before writing the verdict.

Independently read product source. Require canonical findings, source verification, and the minimum-coverage projection.

Product source is verification evidence, not a frozen business oracle. Before
writing the verdict, audit every MRC row in one complete pass. For every
closed-category key missing from DataKnowledge, decide whether the locked
requirement or resolved Explore `assertion_intent` defines the expected behavior.
Report every currently observable missing-key, matrix, case, and proposal defect
in this same review; do not reveal one related defect per repair round.

Capability keys must be exact declared typed leaves.

Write `review/case-review.json` and `review/case-review-summary.md`. Do not modify cases or the proposal.
The host finalize handler generates selection, history, and reviewed-case files
after authenticating the raw review, under its declared write claims.
