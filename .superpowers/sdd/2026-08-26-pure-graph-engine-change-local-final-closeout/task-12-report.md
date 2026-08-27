# Task 12 Report — Rewrite Final Documentation and Lock Export Acceptance

## Status: DONE_WITH_CONCERNS

Current docs now state the installed product workflow and plugin rules.
Black-box export acceptance locks the already-shipped Change-local publish
and archive APIs. Task 3 OpenCode live admission was not fabricated. Cursor
live stays absent from current required docs.

## Baseline

- Branch: `codex/pure-graph-engine-phase3-spec`
- Baseline HEAD: `267ecba9cea503d1bba17258105dc7b51d2f326c`

## What I implemented

- `tests/phase6/test_current_documentation.py` requires:
  - product/engine ownership (`aa` owned by `assurance-product`; graph-engine
    remains business-neutral with no default product)
  - installed-product plugin rules (YAML replaces graph/contracts, Python
    wheels add capability, `.aa/` is organization configuration only, no SUT
    executable plugin loading)
  - delivery flow `aa run` to achieved, then `aa export`, then optional
    `aa archive`
  - installed commands `aa compile`, `aa start`, `aa run`, `aa status`,
    `aa resume`, `aa export`, `aa archive`, `aa bindings build`, `aa lock show`
  - final paths `tests/product/` and `benchmark/assurance-product/`
  - absence of deleted-package, retired-console, whole-tree, result-registry,
    and `aa workflow` language
- `tests/phase6/test_export_delivery_acceptance.py` consumes
  `assurance_product.export.publish_achieved` and
  `assurance_product.status.archive_published` for non-achieved rejection,
  target drift rejection, authenticated manifest file set, interruption
  recovery, idempotency, and archive-after-receipt.
- Current docs (`README.md`, `AGENTS.md`, both package READMEs) keep Task 10
  plugin wording and add only the missing workflow/ownership sentences.
  graph-engine README gained the plugin-rule paragraph and still does not
  claim `aa` ownership or the product delivery flow.

No export/archive production code was added, rewritten, or patched. No
`workflow` command was invented.

## What I tested and test results

Focused docs + export (GREEN):

```text
uv run pytest tests/phase6/test_current_documentation.py tests/phase6/test_export_delivery_acceptance.py -q
12 passed in 0.36s
```

Required no-legacy repository gate:

```text
uv run python scripts/check_no_legacy.py --scope repository
no-legacy repository: 4 violation(s)
```

All four hits are the pre-existing gitignored
`.superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/task-10-review.md`.
That review is outside this task's file list and was not edited. Current docs
and this report do not add those tokens.

## TDD Evidence

**RED** — docs lacked the final delivery/ownership sentences and graph-engine
plugin rules. Export acceptance already passed against the shipped API.

```bash
uv run pytest tests/phase6/test_current_documentation.py tests/phase6/test_export_delivery_acceptance.py -q
```

First run (before folding whitespace / Task 10 wording variants):

```text
4 failed, 8 passed
```

Expected: plugin-rule and delivery-flow assertions failed on current README /
AGENTS / package READMEs; the six export cases passed because they lock
`publish_achieved` / `archive_published`.

After correcting the docs tests to fold wrapping and accept Task 10 phrasing:

```text
3 failed, 3 passed
```

Expected remaining RED: graph-engine README missing YAML/wheels/`.aa/`/no-SUT
plugin sentences; product docs missing the delivery flow and command list;
workspace README missing `tests/product/` and `benchmark/assurance-product/`.

**GREEN** — after adding only the missing current-doc sentences:

```bash
uv run pytest tests/phase6/test_current_documentation.py tests/phase6/test_export_delivery_acceptance.py -q
```

```text
12 passed in 0.36s
```

## Files changed

Created:

- `tests/phase6/test_current_documentation.py`
- `tests/phase6/test_export_delivery_acceptance.py`
- `.superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/task-12-report.md`

Modified:

- `README.md`
- `AGENTS.md`
- `packages/graph-engine/README.md`
- `packages/assurance-product/README.md`

Not staged: leftover dirty Phase 4/5/package hunks, `benchmark/**/results/`,
`tmp/`, provider sessions, frozen Phase 5 `acceptance.json` /
`phase6-handoff.json`, and other-task SDD files.

## Self-review

- Docs tests read real current-doc files and require the final workflow,
  installed-product plugin rules, and absence of old package/command/result
  language. graph-engine is kept product-neutral.
- Export tests call the shipped publish/archive APIs with the existing
  `write_achieved` fixture. They reject non-achieved and baseline drift,
  write only authenticated manifest files, recover after a prepared-phase
  crash via the existing journal cut, stay idempotent, and archive only after
  a publish receipt. Product tests were not deleted or rewritten.
- No production export/CLI rewrite. No Cursor live required path. No Task 3
  admission fabricated.
- Did not `git reset` / `git clean` or stage directories wholesale.

## Issues or concerns

- The repository no-legacy gate still fails on the pre-existing Task 10 review
  file. Adding that path to the allowlist or rewriting the review was out of
  scope.
- Export interruption recovery uses the existing `_journal_cut` test hook.
  That is the same seam the product security tests already use; production
  export was not changed to add a new hook.
- Task 3 live OpenCode admission remains incomplete and was not invented.
