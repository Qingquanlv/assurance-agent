# Planning facts and review measurements

Case Design/Review and all four family Plan/Review prepares now include
`planning_facts` in their existing JSON instructions. It contains bounded file
observations, content digests, declared helper definitions/signature shapes,
fixture names, and referenced environment-variable names. It does not import
SUT code, read environment values, infer product behavior, or declare an
unobserved symbol absent. Exact source hints bypass ignore-aware discovery;
out-of-family catalog symbols are omitted, and file/size limits remain explicit.

Author and reviewer independently observe their current Attempt input snapshot.
An unchanged snapshot produces identical facts; a source change changes its
digest. The digest authenticates the observation content, not the truth of an
oracle or the runtime availability of a fixture. Required semantic source reads
and owner-defined assertion intent remain part of independent review.

Generation finalize checks complete plan packages, including unchanged files on
repair, for these mechanically provable contradictions:

- Displayed Test Function Mapping differs from the closed JSON mapping.
- A capability table references a key outside the supplied catalog.
- A Factory Mapping marks an observed function definition `create-if-missing`.

Unknown dynamic fixtures/imports are left for Review. These checks supplement
existing Case/MRC/scope checks; they neither evaluate oracle quality nor replace
the review gate. Review must list all affected artifact/section locators, and an
automatic plan repair set must include every finding exactly once. Repairs retain
their existing write boundaries and require another review before passing.

## Baseline (before these changes)

`planning_metrics.py` reads persisted per-epoch review histories and run evidence:

```sh
uv run python benchmark/assurance-product/planning_metrics.py \
  benchmark/assurance-product/results/opencode-20260908-api-only-terra-09/evidence.json
```

| API-only run | Case reviews | API Plan reviews | Full run | Wall seconds |
| --- | --- | --- | --- | --- |
| terra06 | pass | needs_fix → pass | blocked | 1524 |
| terra08 | needs_fix → needs_fix → pass | needs_fix → needs_fix → needs_fix | blocked | 1917 |
| terra09 | needs_fix → needs_fix → pass | needs_fix → pass | blocked | 1951 |

These historical runs used different locks and intervening fixes. They are a
baseline inventory, not a controlled comparison or evidence that facts reduce
repair rounds. The stored token/cost fields are unavailable and remain null.

For an effectiveness comparison, repeat the same item/model/worker with matched
source inputs and budgets on the before/after revisions. Keep failed runs in the
denominator. Compare first-review pass, pass within one repair, wall time, and
full-workflow outcome. Independently adjudicate escaped defects and reviewer
false positives; a test failure can be a correctly detected product defect and
must not automatically count as a planning failure. Do not reduce acceptance
criteria or retry budgets to manufacture fewer review rounds.

## Post-change live smoke: terra10

Run evidence: `results/opencode-20260908-planning-facts-terra-10/evidence.json`.
This run used the built wheels and the local OpenCode server with
`openai/gpt-5.6-terra` / `max`, selecting only API. The actual OpenCode user
request and durable activity receipt both contained `planning_facts`.

- Case: seven cases, first Review passed, zero repairs.
- API Plan: `needs_fix → pass`, one repair. The first review found one semantic
  issue (filtering before tree reconstruction hides nested children) and listed
  all five locators across four artifacts. No fixture-name, invented-adapter or
  helper-existence findings were reported. The original review receipt/history
  is retained even though the latest review file now contains the passing verdict.
- Codegen and Execute ran. Execute recorded 3 passed, 3 failed and 1 skipped.
  Two failed closure assertions read an empty row set; the empty-name test
  observed HTTP 200 where its frozen oracle required 422. The authorization case
  skipped. These outcomes need separate runtime/product adjudication; this smoke
  does not classify all three failures as product defects or as planning errors.
- Inspect committed `status=analyzed`, `classification_performed=true`.
- Full workflow: `failed` / benchmark `blocked`; no `quality.report` or achieved
  result. Wall duration was 1589 seconds. The loop result is not end-to-end success
  and one sample does not establish a causal reduction in review rounds.

The smoke wheel preceded the final heading-case normalization in the mechanical
checker. That small change is covered by regression tests and read-only replay
of this run's complete plan package; the model run was not repeated for it.
