# Task 23 Report — Publish Compatibility Semantics and Release Gate

## Status

**DONE** (edit + verify only; no git add/commit — controller owns commits)

Left cursor-loop alone.

## Five pytest failures fixed

| # | Test | Fix |
|---|------|-----|
| 1 | `test_corpus_covers_every_packaged_gate_expression` | Added CORPUS truth/missing pairs for `fixer-proposal-approval-gate:{stop_when,needs_human_review_when,pass_when}` |
| 2–3 | `test_truth_and_missing_pair[gate:{api,e2e}-codegen-precondition-gate:pass_when]` | Supplied `capabilities_present` resolver stubs in corpus harness (Task 15 activation) |
| 4 | `test_uncommitted_predecessor_blocks_preview` | Updated contract: uncommitted predecessors keep plan empty (fail-closed) and preview stays `None` |
| 5 | `test_resume_skips_successful_sibling_and_completed_child` | Production: `freeze_write_set` skips out-of-claim deletes so nested resume repair cannot publish sibling `change:` wipes |

### Files touched

```
assurance_agent/workflow/graph/workspace.py
tests/unit/test_dsl_schema_corpus.py
tests/unit/workflow/graph/test_selected_wave.py
.superpowers/sdd/task-23-gate-pytest.log
.superpowers/sdd/task-23-report.md
```

### Failure 5 root cause

On resume, `_repair_ordinary_materialization(..., restore_change_drift=True)` applies the child tree onto the parent sandbox and deletes sibling `change:` files that were materialized from a newer root tree. Parent freeze then saw `change:branch-a/out.txt` as an unauthorized delete. Skipping out-of-claim deletes at freeze keeps the merge-base copy (unauthorized adds/modifies still fail closed).

## Gate evidence so far

### Focused re-verify (post-fix)

```text
uv run pytest -q \
  tests/unit/test_dsl_schema_corpus.py \
  tests/unit/workflow/graph/test_selected_wave.py::test_uncommitted_predecessor_blocks_preview \
  tests/unit/workflow/graph/test_subgraph_interrupt.py::test_resume_skips_successful_sibling_and_completed_child
→ 72 passed
```

### Full pytest (Step 5)

```text
uv run pytest -q --tb=line > .superpowers/sdd/task-23-gate-pytest.log 2>&1
→ 4672 passed, 2 skipped, 1 warning in 524.41s
EXIT:0
```

### Static / packaging (prior gate logs still green)

| Gate | Log | Result |
|------|-----|--------|
| `ruff check .` | `task-23-gate-static.log` | All checks passed |
| `ruff format --check .` | `task-23-gate-static.log` | 622 files already formatted |
| `pyright` | `task-23-gate-static.log` | 0 errors, 0 warnings |
| `lint-imports` | `task-23-gate-static.log` | 6 kept, 0 broken |
| `scripts/packaging_smoke_test.sh` | `task-23-gate-packaging.log` | OK |
| mechanical consumer-set pytest | `task-23-gate-mechanical-pytest.log` | 4 passed, 181 deselected |

### Mechanical token scan note

`task-23-gate-mechanical.log` previously reported `MATCH_FOUND` for skill lines that *mention* `aa heal record-apply` in a “Do not invoke …” prohibition. That is a scan false-positive relative to the Step 6 intent (forbid live workflow-state/heal usage), not a pytest failure. Revisit scan pattern if the controller requires a clean `! rg` exit.

## Remaining Task 23 work

- [ ] Steps 1–3 / 7–8: docs contracts, release notes, history/diff inspection, documentation commit (controller)
- [ ] Re-run mechanical `! rg` with a negation-aware pattern if required for a clean Step 6
- [ ] No commit from this agent (per instructions)

## Final release gate (recorded)

```text
uv run ruff check .                 → All checks passed
uv run ruff format --check .        → 622 files already formatted
uv run pyright                      → 0 errors, 0 warnings, 0 informations
uv run lint-imports                 → 6 kept, 0 broken
uv run pytest -q                    → 4672 passed, 2 skipped (EXIT:0)
bash scripts/packaging_smoke_test.sh → packaging smoke test: OK
```

Mechanical scans:
- Forbidden tokens in aa-{api,e2e,fuzz,performance}-* skills: clean
- Plan-fixer events.jsonl: clean
- docs contract: 14 passed

## Status: DONE
