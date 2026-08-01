### Task 16: Unify Eval Layer Selection and Define Replayable Change Location

**Files:**
- Create: `assurance_agent/eval/selection.py`
- Create: `assurance_agent/eval/change_location_evidence.py`
- Modify: `assurance_agent/change_location.py`
- Modify: `assurance_agent/eval/runner.py`
- Modify: `assurance_agent/eval/executor.py`
- Modify: `assurance_agent/eval/types.py`
- Create: `tests/unit/eval/test_selection.py`
- Create: `tests/unit/eval/test_change_location_evidence.py`
- Modify: `tests/unit/test_change_location.py`
- Modify: `tests/unit/eval/test_runner.py`
- Modify: `tests/unit/eval/test_executor.py`
- Modify: `tests/integration/test_eval_cli.py`

**Interfaces:**
- Produces: `normalize_selected_layers(...)`, pure change-root parsing/probing/decision, `ChangeLocationCandidateV1`, `ChangeLocationEvidenceV1`, and canonical selection fields in `ExecutionResult`/`execution.json`.
- Consumes: raw suite `test_type`/`test_types` by key presence, `.aa/config.yaml` bytes, caller-supplied before-manifest leaf facts, `preference="active"`, and safe repository containment.
- Preserves: generic workflow CLI params remain validated by the workflow schema; only the eval suite boundary accepts scalar/comma/list forms.

- [ ] **Step 1: Add the complete selection normalization table**

  Cover omitted, single scalar, comma scalar, YAML list, reverse order, all fifteen non-empty subsets, empty string/list, duplicate, unknown, and both keys present. Assert the exact canonical tuple and that a falsey explicit value never receives the default.
- [ ] **Step 2: Add runner/executor/CLI single-resolution tests**

  Pass a list-form suite through both `run_suite` and a real `aa eval run`; assert fixture validation, runtime params, executor result, and `execution.json` all receive the same canonical tuple value. Remove every downstream stringification/reparse path and the `parse_single_test_type` assumption; Task 17 makes policy consume this tuple.
- [ ] **Step 3: Add pure change-location decision tests**

  Cover default and non-default configured roots, changes/archive coexistence selecting changes, archive-only rejection, missing active leaf, absolute/parent roots, symlink candidates, path escape, another change, missing/extra candidates, and tampered config digest. Candidate probes use `lstat` kind/mode and before-manifest leaf presence.
- [ ] **Step 4: Add strict decision-model replay tests over supplied leaf facts**

  Construct `ChangeLocationEvidenceV1` from exact config bytes, safe candidate probes, and an explicit set of before-manifest leaves. Reparse the bytes, recompute the decision, and require byte-identical evidence. Do not wire executor persistence until Task 17 owns the actual content manifest.
- [ ] **Step 5: Run tests and observe truthiness/string/symlink failures**

  ```bash
  uv run pytest -q \
    tests/unit/eval/test_selection.py \
    tests/unit/eval/test_change_location_evidence.py \
    tests/unit/test_change_location.py \
    tests/unit/eval/test_runner.py \
    tests/unit/eval/test_executor.py \
    tests/integration/test_eval_cli.py -k list_form_selection
  ```

  Expected: current runner stringifies lists, defaults falsey values, and live `is_dir()` cannot produce replayable candidate evidence.
- [ ] **Step 6: Implement one normalization boundary and typed executor input**

  Resolve before fixture seeding. Change `execute_attempt` to accept only `selected_layers`; construct graph `params.test_types` as a list in canonical order. Reject any call that tries to pass unresolved raw selection downstream.
- [ ] **Step 7: Split pure change resolution from the existing wrapper**

  Keep `resolve_change(...)` public behavior by delegating to parsed roots, safe probes, and decision. Expose the strict evidence builder/replayer as pure functions that require caller-supplied before-manifest facts. Task 17 wires copied config and evidence before policy/runtime.
- [ ] **Step 8: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/eval/test_selection.py \
    tests/unit/eval/test_change_location_evidence.py \
    tests/unit/test_change_location.py \
    tests/unit/eval/test_runner.py \
    tests/unit/eval/test_executor.py \
    tests/integration/test_eval_cli.py -k list_form_selection
  uv run ruff check assurance_agent/eval/selection.py assurance_agent/eval/change_location_evidence.py assurance_agent/change_location.py assurance_agent/eval/runner.py assurance_agent/eval/executor.py tests/unit/eval
  uv run pyright
  ```

  Expected: all selection forms converge once, and pure change-location evidence replays exactly from explicit leaf facts.
- [ ] **Step 9: Commit the eval selection/location boundary**

  ```bash
  git add assurance_agent/eval/selection.py \
    assurance_agent/eval/change_location_evidence.py \
    assurance_agent/change_location.py \
    assurance_agent/eval/runner.py \
    assurance_agent/eval/executor.py \
    assurance_agent/eval/types.py \
    tests/unit/eval/test_selection.py \
    tests/unit/eval/test_change_location_evidence.py \
    tests/unit/test_change_location.py \
    tests/unit/eval/test_runner.py \
    tests/unit/eval/test_executor.py \
    tests/integration/test_eval_cli.py
  git commit -m "feat(eval): normalize layers and define change evidence"
  ```

