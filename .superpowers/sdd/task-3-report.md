# Task 3 Report: Codegen writes `qa/tests/` directly

## Status

DONE_WITH_CONCERNS

## TDD Evidence

### RED (Step 2)

Tests and fixtures were rewritten first. Production still exported `staged_generated_path` and `FAMILY_TARGET_ROOTS` still used `tests/api/` (etc.). `durable_test_path` did not exist.

Command:

```bash
uv run pytest packages/capabilities/assurance-generation/tests/test_codegen.py packages/capabilities/assurance-generation/tests/test_codegen_characterization.py -v
```

Output:

```
ERROR collecting packages/capabilities/assurance-generation/tests/test_codegen.py
ImportError: cannot import name 'durable_test_path' from 'assurance_generation.contracts.codegen'

ERROR collecting packages/capabilities/assurance-generation/tests/test_codegen_characterization.py
ImportError: cannot import name 'staged_generated_file' from 'codegen_fixtures'

============================== 2 errors in 0.87s ===============================
```

Exit code: 2

Failure reason: missing `durable_test_path` / leftover `staged_generated_file`, not a typo.

### GREEN (Step 4)

Command:

```bash
uv run pytest packages/capabilities/assurance-generation/tests -v
```

Output:

```
============================= 584 passed in 5.14s ==============================
```

Exit code: 0

Re-run after format:

```
584 passed in 5.05s
```

## What changed

- `durable_test_path(target_path)` requires a path that already starts with `qa/tests/`. It does not rewrite `tests/…` → `qa/tests/…`.
- `FAMILY_TARGET_ROOTS` and `family_allows_target` accept only `qa/tests/{family|testdata}/`.
- `CodegenGeneratedFileAuthoring.repo_path` and `CodegenMappingEntry.target_file` validators require `qa/tests/`. Mapping field names stay `{case_id, symbol, target_file}`; symbol shape stays `test_<case_id_lowercase>__<behavior>`.
- Codegen allowed outputs are `qa/results/codegen/{family}-generated-files.json` (plus the existing summary) and durable `qa/tests/…` targets. Bytes are read from those durable paths, not `qa/changes/…/generated/…`.
- Planning case discovery uses `qa/cases/**/case.yaml`.
- Skill path bullets for `aa-*-codegen` and `aa-*-plan` follow the rewrite table.

## Files Changed

| File | Action |
|------|--------|
| `assurance_generation/contracts/codegen.py` | `durable_test_path`; `FAMILY_TARGET_ROOTS`; `qa/tests/` validators; compatibility `staged_generated_path` |
| `assurance_generation/operations/codegen.py` | write/read `qa/tests/` and `qa/results/codegen/` |
| `assurance_generation/operations/planning.py` | case glob `qa/cases/**/case.yaml` |
| `assurance_generation/operations/cycle.py` | authenticate durable oracles; mapping write via `qa_join("generation/epochs/…")` |
| `assurance_generation/validators/generated_files.py` | family roots under `qa/tests/` |
| `tests/codegen_fixtures.py`, `tests/test_codegen.py` | failing tests first; durable oracle helpers |
| other generation tests/fixtures/skills/schema/declaration | path prefix + descriptor sync so Step 4 passes |

## Commit

`6079b3d3` Write codegen oracles directly under qa/tests.

Only `packages/capabilities/assurance-generation` was staged.

## Concerns

1. **`staged_generated_path` was not fully deleted.** Healing still imports the name (`assurance_healing.operations.application`). Generation tests import the product graph, so deleting the symbol breaks collection. The remaining function is a compatibility wrapper that calls `durable_test_path` and does **not** rewrite `tests/…` → `qa/tests/…`. Later tasks should switch healing and drop the name.
2. **Scope beyond the brief file list** was required for Step 4: validator roots, cycle mapping write (`qa/generation/epochs/…` to match Task 2 claims), result-contract schema, plugin declaration, and additional generation tests/fixtures. `execution_view` and `ChangeWorkspace` were not changed.
3. **Plan packages** still live under `qa/changes/{change_id}/plans/`. Only the case glob moved to `qa/cases/`.
4. **Healing runtime** will raise if it still passes `tests/…` into the compatibility wrapper.

---

# Task 3 Important-review fixes

## Status

DONE

## TDD Evidence

### RED

New assertions failed on leftover alias, old plan/review prefixes, nested generated-tree writes, and skill `target_file` examples.

Command:

```bash
uv run pytest packages/capabilities/assurance-generation/tests/test_codegen.py::test_staged_generated_path_is_removed packages/capabilities/assurance-generation/tests/test_planning.py::test_plan_outputs_use_results_plans_and_review packages/capabilities/assurance-generation/tests/test_generated_files_validator.py::test_generated_files_reject_nested_change_generated_suffix packages/capabilities/assurance-generation/tests/test_resources.py::test_plan_skill_mapping_examples_use_qa_tests packages/capabilities/assurance-generation/tests/test_generation_cycle.py::test_generation_cycle_rejects_invalid_family_evidence -v
```

Output:

```
FAILED test_staged_generated_path_is_removed
  AssertionError: assert not True  (staged_generated_path still exported)

FAILED test_plan_outputs_use_results_plans_and_review
  qa/changes/CH-DEMO-001/plans/... != qa/results/plans/...

FAILED test_generated_files_reject_nested_change_generated_suffix
  assert True is False  (qa/changes/.../generated/.../qa/tests/... accepted)

FAILED test_plan_skill_mapping_examples_use_qa_tests[api/e2e/fuzz/performance]
  target_file examples still used tests/... not qa/tests/...

4 passed (cycle reject-invalid still raised for other reasons), 7 failed
```

Exit code: 1. Failures matched the leftover behaviors, not typos.

### GREEN (covering)

Command:

```bash
uv run pytest packages/capabilities/assurance-generation/tests -v
```

Output:

```
============================= 593 passed in 13.92s =============================
```

Exit code: 0

Healing import-site module:

```bash
uv run pytest packages/capabilities/assurance-healing/tests/test_application.py -v
```

Output:

```
============================== 16 passed in 5.65s ==============================
```

Exit code: 0

## What changed

- Deleted `staged_generated_path` from `assurance_generation.contracts.codegen`. Healing now calls `durable_test_path(selected_test_file(...))` and must already pass `qa/tests/…`. Execution keeps its own merge helper and does not import the generation symbol.
- `plan_outputs` / `plan_review_outputs` / cycle plan-prefix / planning mapping and review reads authorize `qa/results/plans/` and `qa/results/review/`. Plan validator default roots match.
- `aa-*-plan` mapping examples use `qa/tests/…` and leftover `.qa.yaml` / `proposal.md` / `facts/` bullets use the flat table.
- Removed `logical_generated_target` stripping so `qa/changes/…/generated/…` cannot sneak in via a nested suffix.

## Files Changed

Generation package (contracts, planning/cycle/review ops, validators, plan skills, persona, tests/fixtures) plus healing import-site (`operations/application.py`), contract source-ref prefix (`qa/tests/` allowed), and `tests/test_application.py`.

## Concerns

Reviewer-skill markdown still lists `qa/changes/<change-id>/plans/` and `…/review/` in some bullets. Execution still defines its own `staged_generated_path` under `qa/changes/…/generated/`. `plan_review_input_paths` still locks `qa/changes/{id}/proposal.md`.
