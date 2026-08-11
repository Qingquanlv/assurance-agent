# Improvement Candidate Contract Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reject semantically invalid Improvement candidates at the shared artifact boundary, while preserving historical knowledge formats and making Auto-review fail closed for legacy invalid memory targets.

**Architecture:** Put the workspace-independent memory-target rule beside the shared Improvement models so v2 and inherited v3 candidates use one contract. Keep the apply-time filesystem/symlink guard as defense in depth, and reuse the pure rule in Auto-review for legacy subjects. Keep `EntityLeaf.constraints` open-ended, but recursively validate the recognized `max_length` semantic key without invalidating existing flattened boolean flags.

**Tech Stack:** Python 3.11, Pydantic v2, pytest, Ruff, Pyright, uv.

## Global Constraints

- Source design: `docs/superpowers/specs/2026-08-04-improvement-candidate-contract-hardening-design.md`.
- Run Python tooling through `uv run ...`.
- Use test-driven development: add the failing assertion, run it and observe the expected failure, then make the smallest production change.
- Preserve the dirty worktree. Stage only files named by the current task; never use `git add -A` or `git add .`.
- Do not rewrite existing benchmark artifacts, ledger events, or `.aa/data-knowledge.yaml`.
- Do not tighten `ImprovementProjection` or `ImprovementReviewSubject`; historical projections/subjects must remain readable.
- Do not replace `EntityLeaf.constraints: dict[str, Any] | None` with a closed schema. Existing flattened keys such as `name_has_max_length: true` remain valid.
- `max_length` means the exact mapping key `"max_length"` at any nesting depth. Its value must be an `int > 0`, with `bool` explicitly rejected even though Python treats `bool` as an `int` subclass.
- A `memory_patch` target is valid only when raw `/`-separated components start with `.aa`, `memory`, contain at least one child component, and contain no empty, `.`, or `..` component. Absolute paths and all backslashes are invalid.
- `change_draft` and `knowledge_delta` target semantics are unchanged.
- Keep `resolve_memory_target()` unchanged as the final filesystem-aware containment and symlink guard.

---

## Task 1: Enforce the shared `memory_patch` target contract

**Files:**

- Modify: `tests/unit/artifacts/test_models_improvements.py`
- Modify: `tests/unit/retro/test_candidates.py`
- Modify: `assurance_agent/artifacts/models/improvements.py`

**Interface:** Add `is_valid_memory_patch_target(target: str) -> bool` in `artifacts.models.improvements`. `ImprovementCandidate.validate_delivery()` calls it only when `delivery is DeliveryKind.MEMORY_PATCH`. `ImprovementCandidateV3` inherits the same check.

- [ ] **Step 1: Add model-seam RED tests.** In `tests/unit/artifacts/test_models_improvements.py`, add a passing case for `.aa/memory/aa-api-plan.md`; parameterize rejection of:

  ```python
  "skills/awe-api-plan:required-field-summary-probe"
  ".aa/memory"
  "/.aa/memory/aa-api-plan.md"
  ".aa/memory/../escape.md"
  ".aa/memory/./aa-api-plan.md"
  ".aa/memory//aa-api-plan.md"
  r".aa\memory\aa-api-plan.md"
  ```

  Construct each as `kind="prompt_improvement", delivery="memory_patch"` and assert `ValidationError` matches `memory_patch target must be a child path under .aa/memory/`.

  Update the existing allowed kind/delivery matrix so its three `MEMORY_PATCH` rows use `.aa/memory/candidate-{index}.md`; keep its logical targets unchanged for other delivery kinds. Make the same adjustment in the allowed matrix in `tests/unit/retro/test_candidates.py`. These are existing valid fixtures whose placeholder targets become invalid under the new contract.

- [ ] **Step 2: Add a candidate-document RED test.** In `tests/unit/retro/test_candidates.py`, start from `_valid_candidate(...).model_dump(mode="json")`, set its target to the original invalid value, add `signal_ids=["issue-pattern:x"]`, and write it inside a schema-v3 `proposal-candidates.json`. Call `read_candidate_document(retro_dir, expected_schema="3")` and assert a `CandidateBatchInvalid` with code `invalid_candidate`. This pins rejection at the zero-write document boundary rather than only exercising the helper.

- [ ] **Step 3: Run RED tests and confirm the current bug.** Run:

  ```bash
  uv run pytest tests/unit/artifacts/test_models_improvements.py tests/unit/retro/test_candidates.py -q
  ```

  Expected: the invalid memory targets are accepted and the new assertions fail.

- [ ] **Step 4: Implement the pure target predicate.** In `assurance_agent/artifacts/models/improvements.py`, add:

  ```python
  def is_valid_memory_patch_target(target: str) -> bool:
      if target.startswith("/") or "\\" in target:
          return False
      parts = target.split("/")
      if any(part in {"", ".", ".."} for part in parts):
          return False
      return len(parts) > 2 and parts[:2] == [".aa", "memory"]
  ```

  Extend `ImprovementCandidate.validate_delivery()` after the kind/delivery matrix check:

  ```python
  if self.delivery is DeliveryKind.MEMORY_PATCH and not is_valid_memory_patch_target(self.target):
      raise ValueError("memory_patch target must be a child path under .aa/memory/")
  ```

- [ ] **Step 5: Run GREEN tests.** Run the Step 3 command. Expected: all tests pass, including the inherited v3 document case.

- [ ] **Step 6: Lint and commit only Task 1 files.** Run:

  ```bash
  uv run ruff check assurance_agent/artifacts/models/improvements.py tests/unit/artifacts/test_models_improvements.py tests/unit/retro/test_candidates.py
  uv run ruff format --check assurance_agent/artifacts/models/improvements.py tests/unit/artifacts/test_models_improvements.py tests/unit/retro/test_candidates.py
  git add assurance_agent/artifacts/models/improvements.py tests/unit/artifacts/test_models_improvements.py tests/unit/retro/test_candidates.py
  git commit -m "fix(improvements): reject unsafe memory patch targets"
  ```

---

## Task 2: Make Auto-review reject legacy invalid memory targets

**Files:**

- Modify: `tests/unit/workflow/improvements/test_auto_review.py`
- Modify: `assurance_agent/workflow/improvements/auto_review.py`

**Interface:** Add `_delivery_allowed(delivery: DeliveryKind, target: str) -> bool` in `auto_review.py`. It preserves the existing ban on automatic knowledge delivery and additionally applies `is_valid_memory_patch_target()` to memory delivery. `build_auto_review_gate_input()` must use this helper.

- [ ] **Step 1: Add RED policy tests.** In `tests/unit/workflow/improvements/test_auto_review.py`, import `_delivery_allowed` and add:

  ```python
  def test_auto_review_rejects_legacy_invalid_memory_target() -> None:
      assert not _delivery_allowed(
          DeliveryKind.MEMORY_PATCH,
          "skills/awe-api-plan:required-field-summary-probe",
      )


  def test_auto_review_allows_valid_memory_target_and_existing_change_draft() -> None:
      assert _delivery_allowed(DeliveryKind.MEMORY_PATCH, ".aa/memory/aa-api-plan.md")
      assert _delivery_allowed(DeliveryKind.CHANGE_DRAFT, "qa/planning:api-rule")
      assert not _delivery_allowed(DeliveryKind.KNOWLEDGE_DELTA, "qa/knowledge:entities.dept")
  ```

  Add a gate-wiring test that proves the helper is used by `build_auto_review_gate_input()` rather than inspecting source text:

  - use `_candidate()` and `_context()` from `tests.unit.workflow.improvements.test_reconcile_v3` with `build_review_subject()` to obtain a structurally valid subject;
  - use `model_copy(update=...)` to represent a readable legacy subject with `kind=PROMPT`, `delivery=MEMORY_PATCH`, and target `skills/awe-api-plan:required-field-summary-probe`;
  - write its canonical bytes under `qa/improvements/review-subjects/<digest>.json` and write a passing `ImprovementAutoReviewAssessment` under `reviews/AUTO-1/assessment.json`;
  - monkeypatch `read_improvement_events` to return `()` and `project_improvements` to return a projection whose `review_subject_sha256` is that digest;
  - call `build_auto_review_gate_input(...)` and assert `gate.delivery_allowed is False` and `gate.auto_approve is False`.

  This exercises the real policy gate while leaving `ImprovementReviewSubject` permissive for historical compatibility.

- [ ] **Step 2: Run the RED test.** Run:

  ```bash
  uv run pytest tests/unit/workflow/improvements/test_auto_review.py -q
  ```

  Expected: import or assertion failure because the shared predicate is not yet consumed by Auto-review.

- [ ] **Step 3: Implement and wire the policy helper.** In `auto_review.py`, import `is_valid_memory_patch_target` and add:

  ```python
  def _delivery_allowed(delivery: DeliveryKind, target: str) -> bool:
      if delivery is DeliveryKind.KNOWLEDGE_DELTA:
          return False
      if delivery is DeliveryKind.MEMORY_PATCH:
          return is_valid_memory_patch_target(target)
      return True
  ```

  Replace `delivery_allowed=subject.delivery is not DeliveryKind.KNOWLEDGE_DELTA` with:

  ```python
  delivery_allowed=_delivery_allowed(subject.delivery, subject.target),
  ```

- [ ] **Step 4: Run GREEN and adjacent Auto-review tests.** Run:

  ```bash
  uv run pytest tests/unit/workflow/improvements/test_auto_review.py tests/unit/workflow/improvements/test_review_subject.py -q
  ```

- [ ] **Step 5: Lint and commit only Task 2 files.** Run:

  ```bash
  uv run ruff check assurance_agent/workflow/improvements/auto_review.py tests/unit/workflow/improvements/test_auto_review.py
  uv run ruff format --check assurance_agent/workflow/improvements/auto_review.py tests/unit/workflow/improvements/test_auto_review.py
  git add assurance_agent/workflow/improvements/auto_review.py tests/unit/workflow/improvements/test_auto_review.py
  git commit -m "fix(improvements): fail closed on legacy memory targets"
  ```

---

## Task 3: Validate semantic `max_length` values without breaking historical flags

**Files:**

- Modify: `tests/unit/artifacts/test_data_knowledge_models.py`
- Modify: `assurance_agent/artifacts/models/data_knowledge.py`

**Interface:** Add an `EntityLeaf.constraints` field validator that recursively walks mappings and sequences. Only an exact key named `max_length` receives semantic validation; every other open-ended constraint remains unchanged.

- [ ] **Step 1: Add compatibility and RED tests.** Import `EntityLeaf`. Add acceptance tests for:

  ```python
  {"constraints": {"name_non_empty": True, "name_has_max_length": True, "name_unique": True}}
  {"constraints": {"name": {"max_length": 20, "unique": True}}}
  ```

  Parameterize rejected values as `True`, `False`, `0`, `-1`, and `"20"`, each nested at `constraints.name.max_length`. Assert `ValidationError` matches `max_length must be a positive integer`.

- [ ] **Step 2: Add the original knowledge candidate regression.** In `tests/unit/artifacts/test_models_improvements.py`, construct the original `domain_knowledge` / `knowledge_delta` candidate shape with `entities.dept.constraints.name.max_length=True` and assert candidate parsing rejects it. Add the corrected sibling with `max_length=20` and assert it parses. This proves the shared leaf validation reaches the Improvement document boundary.

- [ ] **Step 3: Run RED tests.** Run:

  ```bash
  uv run pytest tests/unit/artifacts/test_data_knowledge_models.py tests/unit/artifacts/test_models_improvements.py -q
  ```

  Expected: all invalid `max_length` values are currently accepted, so the rejection assertions fail; historical flattened flags continue to pass.

- [ ] **Step 4: Implement recursive semantic-key validation.** Import `field_validator` and add these members to `EntityLeaf` before `_at_least_one_constraint`:

  ```python
  @staticmethod
  def _validate_constraint_values(value: Any) -> None:
      if isinstance(value, dict):
          for key, child in value.items():
              if key == "max_length" and (
                  isinstance(child, bool) or not isinstance(child, int) or child <= 0
              ):
                  raise ValueError("max_length must be a positive integer")
              EntityLeaf._validate_constraint_values(child)
      elif isinstance(value, list | tuple):
          for child in value:
              EntityLeaf._validate_constraint_values(child)

  @field_validator("constraints")
  @classmethod
  def _known_constraint_values_are_typed(
      cls, constraints: dict[str, Any] | None
  ) -> dict[str, Any] | None:
      cls._validate_constraint_values(constraints)
      return constraints
  ```

  If Pyright rejects `isinstance(value, list | tuple)`, use `isinstance(value, (list, tuple))`; do not loosen the accepted `max_length` type.

- [ ] **Step 5: Run GREEN and validate the historical L1 file.** Run:

  ```bash
  uv run pytest tests/unit/artifacts/test_data_knowledge_models.py tests/unit/artifacts/test_models_improvements.py -q
  uv run python -c 'from pathlib import Path; import yaml; from assurance_agent.artifacts.models.data_knowledge import DataKnowledge; DataKnowledge.model_validate(yaml.safe_load(Path("benchmark/vue-fastapi-admin/.aa/data-knowledge.yaml").read_text())); print("historical_l1=PASS")'
  ```

  Expected: tests pass and the command prints `historical_l1=PASS`.

- [ ] **Step 6: Lint and commit only Task 3 files.** Run:

  ```bash
  uv run ruff check assurance_agent/artifacts/models/data_knowledge.py tests/unit/artifacts/test_data_knowledge_models.py tests/unit/artifacts/test_models_improvements.py
  uv run ruff format --check assurance_agent/artifacts/models/data_knowledge.py tests/unit/artifacts/test_data_knowledge_models.py tests/unit/artifacts/test_models_improvements.py
  git add assurance_agent/artifacts/models/data_knowledge.py tests/unit/artifacts/test_data_knowledge_models.py tests/unit/artifacts/test_models_improvements.py
  git commit -m "fix(knowledge): require concrete max length constraints"
  ```

---

## Task 4: Regression replay and final verification

**Files:** No production changes expected. If a verification failure reveals a real defect, return to the responsible task and add a failing regression test before changing code.

- [ ] **Step 1: Replay the original malformed proposal artifact.** Run:

  ```bash
  uv run python -c 'from pathlib import Path; from assurance_agent.retro.candidates import CandidateBatchInvalid, read_candidate_document; p=Path("benchmark/vue-fastapi-admin/qa/retro/retro-20260803-204602-cursor");
  try: read_candidate_document(p, expected_schema="3")
  except CandidateBatchInvalid as e: print("original_artifact=REJECTED", sorted({x.code for x in e.errors}))
  else: raise SystemExit("original malformed artifact was accepted")'
  ```

  Expected: `original_artifact=REJECTED` and at least `invalid_candidate`. The file contains both original failure shapes; no benchmark artifact is modified.

- [ ] **Step 2: Run the complete related regression slice.** Run:

  ```bash
  uv run pytest tests/unit/artifacts/test_models_improvements.py tests/unit/artifacts/test_data_knowledge_models.py tests/unit/artifacts/test_retro_v3_models.py tests/unit/retro/test_candidates.py tests/unit/workflow/improvements/test_auto_review.py tests/unit/workflow/improvements/test_review_subject.py tests/unit/workflow/improvements/test_memory_delivery.py tests/integration/test_retro_improvement_workflow.py tests/integration/test_improvement_review_workflow.py -q
  ```

- [ ] **Step 3: Run repository quality gates.** Run separately so each failure is attributable:

  ```bash
  uv run ruff check .
  uv run ruff format --check .
  uv run pyright
  uv run lint-imports
  uv run pytest -q
  bash scripts/packaging_smoke_test.sh
  ```

- [ ] **Step 4: Inspect the exact diff and placeholder scan.** Run:

  ```bash
  git diff --check
  git diff -- assurance_agent/artifacts/models/improvements.py assurance_agent/artifacts/models/data_knowledge.py assurance_agent/workflow/improvements/auto_review.py tests/unit/artifacts/test_models_improvements.py tests/unit/artifacts/test_data_knowledge_models.py tests/unit/retro/test_candidates.py tests/unit/workflow/improvements/test_auto_review.py
  rg -n "TODO|FIXME|placeholder|pass$|NotImplemented" assurance_agent/artifacts/models/improvements.py assurance_agent/artifacts/models/data_knowledge.py assurance_agent/workflow/improvements/auto_review.py tests/unit/artifacts/test_models_improvements.py tests/unit/artifacts/test_data_knowledge_models.py tests/unit/retro/test_candidates.py tests/unit/workflow/improvements/test_auto_review.py
  ```

  Confirm no unrelated dirty-worktree file appears in the staged diff.

- [ ] **Step 5: Commit any test-only verification adjustment narrowly.** If no adjustment was needed, do not create an empty commit. Otherwise stage only the named regression test and use:

  ```bash
  git commit -m "test(improvements): cover candidate contract regressions"
  ```

## Acceptance Criteria

- The original invalid `memory_patch` target is rejected while parsing both v2 and inherited v3 candidates.
- Valid memory targets below `.aa/memory/` remain accepted; `change_draft` and `knowledge_delta` target formats are unaffected.
- Auto-review reports `delivery_allowed=false` for a legacy invalid memory target and can no longer auto-approve it.
- `max_length: true`, false, zero, negative, and string values are rejected; `max_length: 20` is accepted.
- Existing flattened constraint flags in the benchmark L1 validate unchanged.
- Existing apply-time containment/symlink protection remains intact.
- Related tests, full pytest, Ruff, Pyright, import-linter, and packaging smoke test all pass.
