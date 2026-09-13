# Task 9 Report: Repo-wide verification

## Status

DONE_WITH_CONCERNS

Scan and ruff stayed green. The focused pytest gate is 2483 passed / 13 skipped / 1 failed. The remaining failure is the Task 8 cursor snapshot message, not rewrite residue from this unlock. No git APIs added. No live OpenCode run.

## Step 1: Forbidden-string scan

Command:

```bash
rg -n "qa/changes|qa/archive" packages tests --glob '!docs/superpowers/**'
```

First run (before remaps) matched:

- `tests/product/goldens/public-closure.json` (`"path": "qa/changes"`)
- `tests/fixtures/retro_v3_golden/qa/changes/*/events.jsonl` (historical `qa/changes/<id>/…` strings)
- Hidden (default `rg` misses `.aa/`): four `tests/fixtures/assurance/*-contract/.aa/config.yaml` (`changes: ./qa/changes`)

Tracked docs outside the scan also still used leftover paths: `README.md` ProductInput examples. `AGENTS.md` and `tests/phase6` were already clean.

After remaps, the same command printed no matches (exit 0). Repeat with `--hidden` was also clean.

`docs/` is gitignored. `docs/usage.md` was rewritten locally (dropped `export` / `archive` command rows; §6 is the flat `qa/` tree; ProductInput examples use `qa/cases` / the four prefixes). That file cannot be committed.

## Step 2: Focused unit + product

The brief’s single invocation was split. Exact commands and outcomes:

### 2a. `qa_paths` + intake

```bash
uv run pytest \
  packages/adapters/agent-runtime-contracts/tests/test_qa_paths.py \
  packages/capabilities/assurance-intake/tests \
  -v --tb=short
```

`4 passed` on `test_qa_paths.py`. Overall: **16 failed, 261 passed** in 3.74s.

Red (pre-existing; Task 7 named “case-design allowlist vs `qa/.qa.yaml`”):

- 14 `test_agent_skills.py` case-design finalize / snapshot tests — `undeclared output file: qa/.qa.yaml` (lock is the four prefixes; root files sit outside them)
- `test_case_family_scope.py::test_finalize_allows_optional_case_outside_frozen_scope[review]` — `undeclared output file: qa/results/review/case-review-summary.md`
- `test_planning_facts.py::test_facts_observe_ignored_files_without_importing_or_disclosing_values` — `KeyError: 'app/schema.py'`

This task did not change intake finalize/allowlist logic.

### 2b. generation

```bash
uv run pytest packages/capabilities/assurance-generation/tests -q --tb=line
```

**9 failed, 584 passed** in 5.03s.

Red (pre-existing rewrite residue / Task 7 skill-doc leftovers):

- `test_durable_test_path_requires_qa_tests_prefix` — DID NOT RAISE
- `test_generated_files_reject_nested_change_generated_suffix` — accepted leftover suffix
- `test_generation_cycle_is_committed_and_passed_to_execution[0,1]` — `allowed_artifact_paths` `[]` rejected by Task 7 lock
- `test_generation_cycle_rejects_change_scoped_plan_prefix` — DID NOT RAISE
- 4× `test_plan_skill_mapping_examples_use_qa_tests` — asserts `qa/.qa.yaml` / `qa/proposal.md` absent from plan skills (those are valid flat root files)

### 2c. execution + quality

```bash
uv run pytest \
  packages/capabilities/assurance-execution/tests \
  packages/capabilities/assurance-quality/tests \
  -q --tb=line
```

**11 failed, 365 passed** in 4.29s.

Red (pre-existing):

- execution validators still expect old reason strings / `tests/` write paths
- quality issue-finalize / prepare: missing `qa/results/execution/api-result.json` or `exactly one current observations.json`

### 2d. healing + improvement

```bash
uv run pytest \
  packages/capabilities/assurance-healing/tests \
  packages/capabilities/assurance-improvement/tests \
  -q --tb=line
```

**5 failed, 377 passed** in 6.97s.

Red (pre-existing):

- 4× `test_safety.py` — validators still reject `tests/api/test_users.py` (want `qa/tests/`)
- `test_retro_slices.py::test_each_plan_bound_source_retains_its_own_authenticated_plan` — `source_refs must be sorted and unique by path`

### 2e. product

```bash
uv run pytest tests/product -q --tb=line
```

**13 failed, 843 passed, 13 skipped** in 397.14s.

Red with Task 7/8 evidence:

- 9× `test_execution_quality_flow.py` / `test_report_flow.py` — `allowed_artifact_paths` not the exact four prefixes (file lists / empty). Same lock Task 7 added.
- `test_success_uses_real_sut_change_and_exports_once` — still expects `qa/publish-receipt.json` after Task 6 dropped export
- `test_all_final_gate_nodes_are_unique_auditable_and_collectable` — `106 == 114` (Task 7: “phase5 gate count”)
- `test_unselected_adapter_source_is_rejected_before_provider_import` — `wheel declaration path is absent from the authenticated snapshot` instead of `runtime.cursor|agent-runtime-cursor` (Task 8 named this exact message)

No failure traces to this task’s golden/README/ruff edits.

## Step 3: Lint

```bash
uv run ruff check packages tests
uv run ruff format --check packages tests
```

First check: F541 leftover f-string in `tests/acg_plan_fixture.py`; format wanted 20 leftover files.

After `ruff format` on those 20 files and dropping the unused `f` prefix:

```
All checks passed!
732 files already formatted
```

Exit 0.

## Step 4: Commit

Brief’s `git add docs` is a no-op (`docs/` is gitignored). Leftover tracked remaps + ruff format are committed separately from this report if the parent asks; this run leaves them in the worktree unless committed with the leftover-path message.

## What this task changed (not in the failing production allowlists)

- `README.md` ProductInput examples → `qa/cases/…`, `qa/results/plan/…`, exact four prefixes
- `docs/usage.md` (local only): 7 commands; no export/archive; flat `qa/` §6
- `tests/product/goldens/public-closure.json` dummy path → `qa/results`
- four contract `.aa/config.yaml` `changes:` rows → `tests` / `fixtures` / `results`
- unused retro golden `events.jsonl` stripped of `qa/changes/<id>/`
- ruff leftovers listed above

## Concerns

- Gate expected PASS; **54** focused failures remain (16+9+11+5+13). All match Tasks 7–8 residue.
- Getting green needs allowlist/root-file policy (`qa/.qa.yaml` vs the four prefixes), healing/execution `qa/tests/` validators, quality evidence paths, and leftover export/phase5/cursor tests — not more string deletion.
- `docs/usage.md` update cannot be committed.
- Retro golden directory is still named `qa/changes/`; content scan is clean.

## Unblock: allowlist expansion + rewrite residue

Controller lock is now the exact sorted 7-tuple (`qa/.qa.yaml`, `qa/cases`, `qa/fixtures`, `qa/proposal.md`, `qa/requirement.md`, `qa/results`, `qa/tests`) on `ProductInputV1`, `run_item.py`, fixtures, and tests that asserted the old 4-tuple.

Combined command:

```bash
uv run pytest packages/adapters/agent-runtime-contracts/tests/test_qa_paths.py packages/capabilities/assurance-intake/tests packages/capabilities/assurance-generation/tests packages/capabilities/assurance-execution/tests packages/capabilities/assurance-quality/tests packages/capabilities/assurance-healing/tests packages/capabilities/assurance-improvement/tests tests/product -q --tb=line
```

**2483 passed, 13 skipped, 1 failed** in 411.53s.

Remaining (pre-existing Task 8; not chased):

- `tests/product/test_product_composition.py::test_unselected_adapter_source_is_rejected_before_provider_import` — message is `wheel declaration path is absent from the authenticated snapshot` instead of `runtime.cursor|agent-runtime-cursor`.

Status: DONE_WITH_CONCERNS.
