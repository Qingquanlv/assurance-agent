---
name: aa-coverage-repair
description: "Close briefed coverage gaps by editing tests only. Reads coverage-repair/brief.json and writes tests plus coverage-repair/apply-summary.json. Never edits case.yaml, .aa/data-knowledge.yaml, or product code."
---

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/coverage-repair/brief.json` — stop if missing.
2. Read `qa/changes/<change-id>/coverage-repair/entry-baseline.json` — stop if missing (attempt not allocated).
3. Work **only** on `brief.repair_items` and files named by `brief.allowed_test_files`.
   Do not create a new test file or invent locators outside the brief.
4. Read `cases/**` only to understand already-declared obligations; never write them.

**After completing work (mandatory — declared output):**

1. **Always rewrite** `qa/changes/<change-id>/coverage-repair/apply-summary.json`, including when nothing changed (`applied: false`). Omitting it fails the task.
2. Copy `change_id`, `attempt`, and `attempt_token` **verbatim** from `coverage-repair/entry-baseline.json`. Do not compute or reformat the token. The previous attempt's summary may still be on disk and would otherwise be mistaken for this attempt's report.
3. Report `files_modified` completely and honestly. Safety computes the real change set from the pre-repair tree snapshot; omitting a file does not hide it — it only adds `summary_mismatch` and routes the run to human review.
4. Include per-item references back to `repair_items` (what was addressed vs left alone).

---

# Skill: aa-coverage-repair

## Purpose

Close eligible coverage gaps listed in `coverage-repair/brief.json` by adding or refreshing **tests only**.

## Imperative rules

1. Read `coverage-repair/brief.json` and work only on its `repair_items`.
2. For each item by kind:
   - `constraint_without_property` → add a property test asserting the named constraint key on the named entity.
   - `matrix_cell_unasserted` → add the parameterized authorization-matrix case for the named cell.
   - `uncovered_required_case` / `stale_required_case` → add or refresh the test bound to the named case id, honoring the `test_<case_id>__` naming contract.
3. Modify only existing paths in `brief.allowed_test_files`; those paths are the exact
   codegen-plan scope that the following rerun will execute. Put `case_id` in the
   function name (`test_<case_id>__…`) when adding a case to a shared file.
4. **Never** create or edit `case.yaml`, `.aa/data-knowledge.yaml`, or product code. If an item appears to need a new declaration, leave it and record that in `apply-summary.json`.
5. **Never** satisfy a locator with an assertion that cannot fail (e.g. asserting a response is truthy). Coverage bought with a vacuous assertion is a lie the safety check cannot see and assertion-strength will later expose.
6. **Never** add `skip` / `xfail` markers; doing so routes the run to human review.
7. Touch only files in `brief.allowed_test_files`; every other test edit routes to human review.
8. **Always** write `coverage-repair/apply-summary.json` with `applied`, `files_modified`, and per-item references back to `repair_items` — including when nothing was changed (`applied: false`).
9. **Rewrite the summary on every attempt**, copying `change_id`, `attempt`, and `attempt_token` verbatim from `coverage-repair/entry-baseline.json`.
10. Report `files_modified` completely and honestly.
11. When adding Hypothesis properties to a function that also accepts pytest
    fixtures, use keyword strategies (`@given(value=...)`), never positional
    strategies. Positional strategies bind from the right and can leave the
    intended generated parameter visible to pytest as a missing fixture.
12. Do not hide an unresolved local import by moving it into a fixture or test
    body. If the missing module is outside `brief.allowed_test_files`, leave the
    item unaddressed and report it in `apply-summary.json`.

## Role boundary

This skill:

- **May** modify only `repo:tests/**` paths listed in `brief.allowed_test_files`
- **May** (must) write `coverage-repair/apply-summary.json`
- **Must NOT** write `change:cases/**` or `project:.aa/**`
- **Must NOT** modify product code
- **Must NOT** leave a stale apply-summary from a prior attempt

## apply-summary.json shape

```json
{
  "schema_version": "1",
  "change_id": "<from entry-baseline>",
  "attempt": 1,
  "attempt_token": "<verbatim from entry-baseline>",
  "applied": true,
  "files_modified": ["tests/api/test_<case_id>__constraint_foo.py"],
  "addressed_items": ["<repair_item_id>"],
  "notes": ""
}
```

When no repair was applied, still write the file with `applied: false`, empty `files_modified` / `addressed_items` as appropriate, and the same attempt binding fields copied from the baseline.
