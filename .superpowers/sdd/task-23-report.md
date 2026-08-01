# Task 23 Report — Publish Compatibility Semantics and Release Gate

## Status

**DONE**

No git add/commit from this agent (controller landed `2e20802` / `fd2326e`). Cursor-loop left alone.

Tip at verification close: `fd2326e` (docs close) on top of `2e20802` (Task 23 docs + gate fixes) and Task 22 `9ae1095`.

## What landed

### Docs / contracts
- `tests/unit/test_docs_contract.py` — schema IDs, six v6 fields, validators, effect kinds, legacy reason, supersede, layers/default, evidence-export algorithm, three hard metrics; rejects stale single-layer / summary-authority / auto-heal wording
- `docs/schemas.md`, `docs/eval.md`, `README.md`, `docs/release-notes/2026-08-four-layer-assurance.md`

### Release-gate blockers fixed
| Issue | Fix |
|-------|-----|
| Hang: `test_prepared_publication_blocks_later_change_after_apply_before_ack_crash` | Unacknowledged prepared publications and non-retryable conflicts fail closed via attempt start+fail (`scheduler._fail_closed_project_resource_conflict`); retryable lock timeouts keep deferral |
| DSL corpus missing fixer-proposal-approval gates + `capabilities_present` | Corpus pairs + resolver stubs in `tests/unit/test_dsl_schema_corpus.py` |
| `test_uncommitted_predecessor_blocks_preview` | Fail-closed: empty plan when predecessor succeeded-but-uncommitted |
| Nested resume forbidden delete | `TreeStore.freeze_write_set` skips out-of-claim deletes |
| Step 6 `-k` names missing | Added `test_declared_only_exact_count`, `test_exact_validator_contract_set`, `test_exact_effect_contract_set`, `test_exact_validator_and_effect_consumer_set` |

## Gate evidence (real command output)

### Docs contract

```text
uv run pytest -q tests/unit/test_docs_contract.py
..............                                                           [100%]
14 passed in 0.02s
```

### Focused feature suites (brief Step 4 + CLI codegen)

```text
# focused list from brief (plus related CLI)
508 passed, 1 warning in 205.80s   # task-23-gate-focused.log

uv run pytest -q tests/integration/test_eval_cli.py -k codegen
7 passed, 8 deselected, 1 warning in 21.28s   # task-23-gate-cli-codegen.log
```

### Hang-fix proof

```text
uv run pytest -q tests/integration/test_graph_runtime.py::test_prepared_publication_blocks_later_change_after_apply_before_ack_crash
1 passed, 1 warning in 2.94s   # task-23-hang-fix.log
```

### Full release gate (brief Step 5)

```text
uv run ruff check .
All checks passed!
EXIT:0

uv run ruff format --check .
622 files already formatted
EXIT:0

uv run pyright
0 errors, 0 warnings, 0 informations
EXIT:0

uv run lint-imports
Contracts: 6 kept, 0 broken.
EXIT:0

uv run pytest -q
4672 passed, 2 skipped, 1 warning in 524.41s (0:08:44)
EXIT:0   # task-23-gate-pytest.log

bash scripts/packaging_smoke_test.sh
packaging smoke test: OK
EXIT:0   # task-23-gate-packaging.log
```

### Mechanical scans (brief Step 6)

```text
# ! rg workflow-state.yaml|phases.|aa heal record-apply on aa-{api,e2e,fuzz,performance}-*
NO_MATCH EXIT:0 (brief ! rg success)

# ! rg events.jsonl on aa-api-plan-fixer / aa-e2e-plan-fixer
NO_MATCH EXIT:0 (brief ! rg success)

uv run pytest -q \
  tests/unit/verification/test_assurance_contract_round_trip.py \
  tests/unit/workflow/graph/test_contracts.py \
  tests/unit/workflow/graph/test_runtime_commit_safety.py \
  -k 'declared_only_exact_count or exact_validator_contract_set or exact_effect_contract_set or consumer_set'
....                                                                     [100%]
4 passed, 181 deselected, 1 warning in 0.58s
EXIT:0
```

### History / diff inspect (brief Step 7; no commit)

```text
git log --oneline --decorate -3
fd2326e (HEAD -> codex/capability-traceability-integration) docs(sdd): mark Task 23 complete and close four-layer plan
2e20802 docs(assurance): publish v6 runtime evidence semantics
9ae1095 feat(eval): activate current-chain scorers and pending hard gates

# Task 15 activation remains 14f023d; Task 22 activation 9ae1095; Task 23 docs/gate 2e20802
```

`git diff origin/main...HEAD --check` reports trailing whitespace only in older `.superpowers/sdd/task-*-report.md` / resolution notes (pre-Task-23), plus one EOF blank-line note — not release-gate failures.

## Log artifacts

- `.superpowers/sdd/task-23-gate-static.log`
- `.superpowers/sdd/task-23-gate-pytest.log`
- `.superpowers/sdd/task-23-gate-packaging.log`
- `.superpowers/sdd/task-23-gate-mechanical.log`
- `.superpowers/sdd/task-23-gate-focused.log`
- `.superpowers/sdd/task-23-gate-cli-codegen.log`
- `.superpowers/sdd/task-23-hang-fix.log`

## Verdict

**DONE** — full six-command release gate green with recorded output; docs contract + mechanical scans green; prepared-publication hang fail-closed.
