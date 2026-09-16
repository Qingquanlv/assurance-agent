# Codegen-Review Audit Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop codegen-review from requiring a model-authored `review_audit` so the reviewer only routes on `route` / `finding_ids`.

**Architecture:** Finalize accepts `PlanReviewAuthoring` without an audit and strips a leftover `review_audit` field. Prepare stops injecting `review_requirements` into the result schema and instruction JSON. Skills drop the coverage-table instructions. Then delete the unused audit modules.

**Tech Stack:** Python 3.11, pydantic, pytest, `uv run`.

## Global Constraints

- Do not move the table into case-design. Do not host-build a replacement ledger.
- Coverage obligations stay on case-design’s `minimum-coverage-matrix.json` and codegen locked outputs.
- `route` + `finding_ids` stay host-validated, no rewrite. Illegal pairs stay retryable `OutputError`.
- Leftover `review_audit` in a model payload is stripped, not invalid output.
- Do not change graph topology, `_ATTEMPT_PATHS`, or codegen locked outputs.
- Do not touch the in-flight live item `BENCH-opencode-ret-dept-management-20260915-124401-e3b28177`.
- Run tests with `uv run pytest`. Do not kill OpenCode on port 4096.

---

## File map

- `packages/capabilities/assurance-generation/assurance_generation/operations/review.py` — strip leftover; drop audit require/repair/validate.
- `packages/capabilities/assurance-generation/assurance_generation/operations/planning.py` — drop `review_requirements` from `result_contract` and `prepare_plan_outcome`.
- `packages/capabilities/assurance-generation/assurance_generation/contracts/reviews.py` — drop `review_audit` from `Review` and `PlanReviewAuthoring`.
- `packages/capabilities/assurance-generation/assurance_generation/resources/result-contracts/plan-review.v1.schema.json` — drop audit `$defs` and the `review_audit` property.
- `packages/capabilities/assurance-generation/assurance_generation/resources/schemas/plan-review.v1.schema.json` — same as the result-contract copy.
- `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-*-codegen-reviewer/SKILL.md` — no table, no plan write-back.
- Delete: `operations/review_audit.py`, `contracts/review_audit.py`, `tests/test_review_audit.py`, `tests/test_review_audit_repair.py`.
- `packages/capabilities/assurance-generation/tests/test_plan_review.py` — no-audit finalize, leftover strip, prepare schema.
- `packages/capabilities/assurance-generation/tests/test_contracts.py` — `valid_plan_review()` is the no-audit payload.

---

### Task 1: Finalize without review_audit

**Files:**
- Modify: `packages/capabilities/assurance-generation/tests/test_plan_review.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/operations/review.py`
- Delete: `packages/capabilities/assurance-generation/tests/test_review_audit.py`
- Delete: `packages/capabilities/assurance-generation/tests/test_review_audit_repair.py`

**Interfaces:**
- Consumes: `review_finalize_handler(family: str) -> TaskHandler`, `valid_plan_review()` from `test_contracts.py`, `fake_agent_result` from `planning_fixtures.py`
- Produces: API finalize succeeds with no `review_audit`; a leftover `review_audit` object is dropped from the output

- [ ] **Step 1: Write the failing tests**

In `test_plan_review.py`, replace `test_api_review_cannot_finish_without_check_coverage` and add the leftover-strip test. Import `valid_plan_review` from `test_contracts`:

```python
from test_contracts import valid_plan_review  # pyright: ignore[reportMissingImports]


@pytest.mark.asyncio
async def test_api_review_finalizes_without_review_audit(tmp_path: Path) -> None:
    outcome = await execute_task(
        review_finalize_handler("api"),
        fake_agent_result(valid_plan_review()),
        tmp_path,
    )
    assert outcome.status == "succeeded", outcome.failure
    output = cast(dict[str, object], outcome.output)
    assert output["route"] == "codegen"
    assert "review_audit" not in output


@pytest.mark.asyncio
async def test_api_review_finalize_strips_leftover_review_audit(tmp_path: Path) -> None:
    leftover = {
        **valid_plan_review(),
        "review_audit": {
            "input_refs": [],
            "planning_facts_digest": "0" * 64,
            "cases": [],
            "helpers": [],
        },
    }
    outcome = await execute_task(
        review_finalize_handler("api"),
        fake_agent_result(leftover),
        tmp_path,
    )
    assert outcome.status == "succeeded", outcome.failure
    output = cast(dict[str, object], outcome.output)
    assert "review_audit" not in output
```

Delete the old `test_api_review_cannot_finish_without_check_coverage` function.

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest packages/capabilities/assurance-generation/tests/test_plan_review.py::test_api_review_finalizes_without_review_audit packages/capabilities/assurance-generation/tests/test_plan_review.py::test_api_review_finalize_strips_leftover_review_audit -v`

Expected: FAIL. The first test fails with `review_audit is required for API codegen review in every round`. The leftover payload may fail the same way or on audit validation.

- [ ] **Step 3: Write the minimal finalize change**

In `PlanReviewFinalizeHandler.execute`, pop `review_audit` before `PlanReviewAuthoring.model_validate`, delete the API audit block, and seal the API review JSON whenever the family is `api` (do not gate on `document.review_audit`).

Replace the validate + API-audit region with:

```python
            raw = thaw_json(payload.agent_result.result_payload)
            if isinstance(raw, dict):
                raw.pop("review_audit", None)
            try:
                document = PlanReviewAuthoring.model_validate(
                    raw,
                    context={"capability_leafs": leafs_of(payload.capability_leafs)},
                )
            except ValidationError as error:
                raise OutputError(str(error)) from error
            expected = f"{family}-codegen"
            if document.review_type != expected:
                raise OutputError(f"review_type {document.review_type!r} does not match {expected}")
            document = PlanReviewAuthoring.model_validate(
                apply_plan_review_policy(document.model_dump(mode="json"), previous=None),
                context={"capability_leafs": leafs_of(payload.capability_leafs)},
            )
            if family == "api":
                raw_path = "qa/results/review/api-codegen-review.json"
                sealed = context.write_root.joinpath(*PurePosixPath(raw_path).parts)
                sealed.parent.mkdir(parents=True, exist_ok=True)
                sealed.write_text(
                    json.dumps(document.model_dump(mode="json"), indent=2) + "\n",
                    encoding="utf-8",
                )
```

Remove these imports from `review.py`:

```python
from assurance_generation.operations.review_audit import (
    api_review_requirements,
    repair_api_review_audit,
    validate_api_review_audit,
)
```

Also drop now-unused imports that existed only for that block: `logging` if nothing else uses it, plus `load_family_cases`, `planning_facts_for`, `review_input_images` if they are no longer referenced in this file.

Delete `tests/test_review_audit.py` and `tests/test_review_audit_repair.py`. Those files encode the old finalize gate and will fail after this change. Keep `review_audit_fixtures.py` for Task 2.

- [ ] **Step 4: Run finalize tests**

Run: `uv run pytest packages/capabilities/assurance-generation/tests/test_plan_review.py -v`

Expected: PASS. `route` / `finding_ids` tests still fail illegal pairs.

- [ ] **Step 5: Commit**

```bash
git add packages/capabilities/assurance-generation/assurance_generation/operations/review.py packages/capabilities/assurance-generation/tests/test_plan_review.py
git rm packages/capabilities/assurance-generation/tests/test_review_audit.py packages/capabilities/assurance-generation/tests/test_review_audit_repair.py
git commit -m "$(cat <<'EOF'
fix(generation): finalize codegen-review without review_audit

A missing or leftover coverage table is no longer retryable invalid
output. Reviewer routing stays on route and finding_ids.
EOF
)"
```

---

### Task 2: Prepare stops requiring the table

**Files:**
- Modify: `packages/capabilities/assurance-generation/tests/test_plan_review.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/operations/planning.py`
- Modify: `packages/capabilities/assurance-generation/tests/test_plan_review.py` callers that still use `audited_review`
- Modify: `packages/capabilities/assurance-generation/tests/review_audit_fixtures.py` only if a remaining caller still needs `write_codegen_artifacts` / `review_prepare_input`

**Interfaces:**
- Consumes: `result_contract(schema_id: str, *, capability_leafs: tuple[str, ...] | None = None) -> ResultContract` (no `review_requirements`)
- Produces: prepare instruction JSON has `review_input_paths` and no `review_requirements`; result schema `required` does not contain `review_audit`

- [ ] **Step 1: Write the failing prepare test**

Append to `test_plan_review.py` (same workspace setup as `test_plan_review_prepare_uses_reviewer_persona`):

```python
@pytest.mark.asyncio
async def test_api_review_prepare_does_not_require_review_audit(tmp_path: Path) -> None:
    proposal_path = tmp_path / "qa/proposal.md"
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    proposal_path.write_text("# Proposal\n", encoding="utf-8")
    write_codegen_artifacts(tmp_path, "api")
    case_path = tmp_path / "qa/cases/items/case.yaml"
    case_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.write_text("schema_version: '1.0'\nadded: []\nmodified: []\nremoved: []\n", encoding="utf-8")

    prepared = await execute_task(
        review_prepare_handler("api"),
        review_prepare_input("api", plan_input("api")),
        tmp_path,
        binding_data=BINDING,
    )
    assert prepared.status == "succeeded", prepared.failure
    request = AgentRunRequest.model_validate(prepared.output)
    schema = cast(dict[str, object], request.result_contract.schema_document)
    assert "review_audit" not in cast(list[object], schema["required"])
    parts = [thaw_json(part.json_content) for part in request.instructions if part.json_content is not None]
    assert all(
        not (isinstance(part, dict) and "review_requirements" in part) for part in parts
    )
```

Add `from graph_engine.frozen_json import thaw_json` if the file does not already import it.

- [ ] **Step 2: Run the prepare test to verify it fails**

Run: `uv run pytest packages/capabilities/assurance-generation/tests/test_plan_review.py::test_api_review_prepare_does_not_require_review_audit -v`

Expected: FAIL because `review_audit` is still appended to `required` and `review_requirements` is still in an instruction part.

- [ ] **Step 3: Remove review_requirements wiring**

In `planning.py`:

1. Delete `from assurance_generation.operations.review_audit import api_review_requirements`.
2. Change `result_contract` to:

```python
def result_contract(
    schema_id: str,
    *,
    capability_leafs: tuple[str, ...] | None = None,
) -> ResultContract:
    payload = json.loads(resource_bytes(_RESULT_FILES[schema_id]))
    if capability_leafs is not None:
```

Delete the `if review_requirements is not None:` block that added `review_audit` to `required`.

3. In `prepare_plan_outcome`, replace the API requirements block with paths only:

```python
    if review_input_paths:
        instructions = (
            *instructions,
            InstructionPart.from_json({"review_input_paths": list(review_input_paths)}),
        )
```

4. Call `result_contract` without `review_requirements`:

```python
        result_contract=result_contract(
            result_schema_id,
            capability_leafs=business.capability_leafs if close_result_capabilities else None,
        ),
```

5. In `test_plan_review.py`, stop calling `audited_review` for success-path finalize tests. Use `valid_plan_review()` (and `_as_auto_fix(...)` where needed) plus `write_review` when the test stages `qa/results/review/api-codegen-review.json`. Example for `test_plan_review_finalize_accepts_typed_review`:

```python
    review = valid_plan_review() if family == "api" else {**valid_plan_review(), "review_type": f"{family}-codegen"}
```

For history / auto_fix tests that currently do `review, _, _ = await audited_review(tmp_path)`, use:

```python
    review = valid_plan_review()
    write_review(tmp_path, review)
```

Keep importing `write_review` / `write_codegen_artifacts` / `review_prepare_input` from `review_audit_fixtures` if those helpers are still used. If `audited_review` has no remaining callers, delete that function from the fixture file.

- [ ] **Step 4: Run prepare and review tests**

Run: `uv run pytest packages/capabilities/assurance-generation/tests/test_plan_review.py packages/capabilities/assurance-generation/tests/test_contracts.py -v`

Expected: PASS. No test looks up `review_requirements`.

- [ ] **Step 5: Commit**

```bash
git add packages/capabilities/assurance-generation/assurance_generation/operations/planning.py packages/capabilities/assurance-generation/tests/test_plan_review.py packages/capabilities/assurance-generation/tests/review_audit_fixtures.py
git commit -m "$(cat <<'EOF'
fix(generation): stop injecting review_requirements into codegen-review

Prepare no longer makes review_audit required on the result schema or
instruction JSON.
EOF
)"
```

---

### Task 3: Reviewer skills drop the coverage table

**Files:**
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-api-codegen-reviewer/SKILL.md`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-e2e-codegen-reviewer/SKILL.md`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-fuzz-codegen-reviewer/SKILL.md`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-performance-codegen-reviewer/SKILL.md`
- Modify: `packages/capabilities/assurance-generation/tests/test_plan_review.py`

**Interfaces:**
- Consumes: existing `resource_text(f"skills/{skill_id}/SKILL.md")`
- Produces: four reviewer skills contain no `review_audit`, `review_requirements`, or `plan_location`

- [ ] **Step 1: Write the failing skill test**

Add to `test_plan_review.py`:

```python
@pytest.mark.parametrize(
    "skill_id",
    (
        "aa-api-codegen-reviewer",
        "aa-e2e-codegen-reviewer",
        "aa-fuzz-codegen-reviewer",
        "aa-performance-codegen-reviewer",
    ),
)
def test_codegen_reviewer_skills_do_not_require_review_audit(skill_id: str) -> None:
    skill = resource_text(f"skills/{skill_id}/SKILL.md")
    assert "review_audit" not in skill
    assert "review_requirements" not in skill
    assert "plan_location" not in skill
    assert "edit plan files" not in skill
```

- [ ] **Step 2: Run the skill test to verify it fails**

Run: `uv run pytest packages/capabilities/assurance-generation/tests/test_plan_review.py::test_codegen_reviewer_skills_do_not_require_review_audit -v`

Expected: FAIL on `aa-api-codegen-reviewer` (`review_audit` still present). Other families fail on `edit plan files`.

- [ ] **Step 3: Edit the skills**

In `aa-api-codegen-reviewer/SKILL.md`:

1. Delete the entire `## Verifiable review coverage` section (from that heading through the helper-row / `unresolved` instructions, up to but not including `## Inputs` or the next remaining heading).
2. Replace the sentence that says “every required plan artifact” with “every locked generated test, testdata file, and mapping”.
3. Replace the Outputs paragraph so it no longer names `review_audit` or “audit evidence-path membership”:

```markdown
The JSON file and final assistant JSON are two deliveries of the same complete
`PlanReviewAuthoring` object. Build that object once using every required field
in the supplied result schema, including `route` and `finding_ids`.
Write the entire object to `api-codegen-review.json`, then read and parse that exact
file. After any correction, rewrite and re-read the file first; a corrected final
response alone does not repair the staged artifact. The Markdown summary is the
human-readable summary, not a replacement or reduced shape for the JSON file.
```

4. Replace the Boundaries section with:

```markdown
## Boundaries

Write only the review outputs listed above. Authorizing bounded codegen re-entry
does not permit the reviewer to edit tests, testdata, mapping, cases, or
knowledge files. Each automatic finding must point at a locked generated test
or mapping path and a bounded key/section that codegen can revise.

A finding locator anchors the observed defect. Do not split one conceptual
defect into a new finding solely for each occurrence.

The graph owns phase state. Do not write an orchestration state file.
```

In `aa-e2e-codegen-reviewer/SKILL.md`, `aa-fuzz-codegen-reviewer/SKILL.md`, and `aa-performance-codegen-reviewer/SKILL.md`, replace every “edit plan files” / “exact plan artifact” / “planner can revise” Boundaries sentence with the same generated-test/mapping wording. Do not add `review_audit`. Keep family-prefixed output paths.

- [ ] **Step 4: Run skill and resource tests**

Run: `uv run pytest packages/capabilities/assurance-generation/tests/test_plan_review.py::test_codegen_reviewer_skills_do_not_require_review_audit packages/capabilities/assurance-generation/tests/test_plan_review.py::test_plan_reviewer_skills_do_not_instruct_removed_decisions packages/capabilities/assurance-generation/tests/test_resources.py -v`

Expected: PASS. Existing `test_resources.py` assertions about `route: auto_fix` and SUT defects still match.

- [ ] **Step 5: Commit**

```bash
git add packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-api-codegen-reviewer/SKILL.md packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-e2e-codegen-reviewer/SKILL.md packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-fuzz-codegen-reviewer/SKILL.md packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-performance-codegen-reviewer/SKILL.md packages/capabilities/assurance-generation/tests/test_plan_review.py
git commit -m "$(cat <<'EOF'
docs(generation): drop review_audit from codegen-reviewer skills

Reviewers keep semantic routing and stop asking the model to copy a
coverage ledger or write back to a plan package.
EOF
)"
```

---

### Task 4: Delete unused audit modules

**Files:**
- Delete: `packages/capabilities/assurance-generation/assurance_generation/operations/review_audit.py`
- Delete: `packages/capabilities/assurance-generation/assurance_generation/contracts/review_audit.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/contracts/reviews.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/result-contracts/plan-review.v1.schema.json`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/schemas/plan-review.v1.schema.json`
- Modify: `packages/capabilities/assurance-generation/tests/review_audit_fixtures.py` — keep only helpers still imported (`write_codegen_artifacts`, `review_prepare_input`, `write_review`); if the file is then only those three, leave it named as-is to avoid a drive-by rename
- Test: `packages/capabilities/assurance-generation/tests/test_plan_review.py`
- Test: `packages/capabilities/assurance-generation/tests/test_contracts.py`

**Interfaces:**
- Consumes: no production importer of `assurance_generation.operations.review_audit` or `assurance_generation.contracts.review_audit`
- Produces: `Review.review_audit` and `PlanReviewAuthoring.review_audit` gone; schema has no `PlanReviewAudit` `$defs`

- [ ] **Step 1: Write the failing importer / schema tests**

Add to `test_plan_review.py`:

```python
def test_review_audit_modules_are_gone() -> None:
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("assurance_generation.operations.review_audit")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("assurance_generation.contracts.review_audit")
```

Add to `test_contracts.py` next to `valid_plan_review` tests, or in `test_plan_review.py`:

```python
def test_plan_review_schema_has_no_review_audit() -> None:
    from assurance_generation.resource_loader import resource_bytes
    import json

    for relative in (
        "result-contracts/plan-review.v1.schema.json",
        "schemas/plan-review.v1.schema.json",
    ):
        schema = json.loads(resource_bytes(relative))
        assert "review_audit" not in schema["properties"]
        assert "review_audit" not in schema["required"]
        defs = schema.get("$defs", {})
        for name in (
            "PlanReviewAudit",
            "CaseReviewChecks",
            "CaseReviewCoverage",
            "HelperReviewEvidence",
            "HelperPlanLocation",
        ):
            assert name not in defs
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest packages/capabilities/assurance-generation/tests/test_plan_review.py::test_review_audit_modules_are_gone packages/capabilities/assurance-generation/tests/test_plan_review.py::test_plan_review_schema_has_no_review_audit -v`

Expected: FAIL (`ModuleNotFoundError` not raised; schema still has `review_audit`).

- [ ] **Step 3: Delete modules and drop the field**

1. `git rm` `operations/review_audit.py` and `contracts/review_audit.py`.
2. In `contracts/reviews.py`, delete `from assurance_generation.contracts.review_audit import PlanReviewAudit` and both `review_audit: PlanReviewAudit | None = None` fields.
3. In both `plan-review.v1.schema.json` files, delete the `review_audit` property and the `$defs` entries `PlanReviewAudit`, `CaseReviewChecks`, `CaseReviewCoverage`, `HelperReviewEvidence`, `HelperPlanLocation`. Keep `EvidenceArtifactRefV1` only if some other `$ref` still uses it; after this deletion it is unused, so delete it too.
4. Grep the wheel for `review_audit`, `api_review_requirements`, `PlanReviewAudit`, `review_requirements`. Production code must have zero hits. Tests may mention the strings only in the “must be absent” assertions.

- [ ] **Step 4: Run the generation review test set**

Run: `uv run pytest packages/capabilities/assurance-generation/tests/test_plan_review.py packages/capabilities/assurance-generation/tests/test_contracts.py packages/capabilities/assurance-generation/tests/test_resources.py packages/capabilities/assurance-generation/tests/test_plan_review_policy.py packages/capabilities/assurance-generation/tests/test_plan_review_routing.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git rm packages/capabilities/assurance-generation/assurance_generation/operations/review_audit.py packages/capabilities/assurance-generation/assurance_generation/contracts/review_audit.py
git add packages/capabilities/assurance-generation/assurance_generation/contracts/reviews.py packages/capabilities/assurance-generation/assurance_generation/resources/result-contracts/plan-review.v1.schema.json packages/capabilities/assurance-generation/assurance_generation/resources/schemas/plan-review.v1.schema.json packages/capabilities/assurance-generation/tests/test_plan_review.py packages/capabilities/assurance-generation/tests/test_contracts.py packages/capabilities/assurance-generation/tests/review_audit_fixtures.py
git commit -m "$(cat <<'EOF'
refactor(generation): remove unused codegen-review audit modules

The coverage ledger is gone from contracts and schemas. Nothing in
production imports review_audit.
EOF
)"
```

---

## Self-review

| Spec requirement | Task |
|---|---|
| Finalize succeeds without `review_audit` | Task 1 |
| Leftover `review_audit` is stripped | Task 1 |
| Prepare schema does not require `review_audit` | Task 2 |
| Prepare does not inject `review_requirements` | Task 2 |
| Four skills do not tell the model to emit the table | Task 3 |
| Plan leftovers / plan write-back stripped | Task 3 |
| `review_audit` modules have no production importers | Task 4 |
| `route` + `finding_ids` unchanged | Task 1 keeps existing tests |
| Graph / locked outputs / live item untouched | Global constraints |
