# Intake Task Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `assurance_intake.task` the real owner of Intake Agent Task definitions while preserving contracts, runtime behavior, graph topology, and authenticated wheel metadata.

**Architecture:** Move the four lifecycle Task classes into `task.py`; leave request/result models and execution policies in `contracts/attempts.py`. Keep `plugin.py` as the installed registry, split mixed prepare logic under `operations/`, move unrelated processing out of model modules, and keep all StateGraph topology under `graphs/`.

**Tech Stack:** Python 3.11, uv workspace, Pydantic, LangGraph, pytest, ruff, pyright, lint-imports.

**Spec:** `docs/superpowers/specs/2026-09-29-intake-task-module-design.md`

## Global Constraints

- Intake behavior only; direct import sites in other wheels may change to use Intake's read-only operations, but their behavior and the framework lifecycle interfaces do not change.
- `.importlinter` permits only exact, reviewed cross-wheel imports of Intake's read-only operations; all other cross-Feature operation and graph imports remain forbidden.
- Preserve every contract/handler/node ID, canonical digest, retry/timeout value, read/write claim, graph edge, output route, and plugin declaration.
- OpenCode is injected through `RuntimePhase`; Task definitions never construct a client.
- Wheel-local `resources/` and `validators/` remain packaged implementations; `.aa/` remains organization configuration, not executable code.
- Use `uv run` for Python tooling; do not push or create a PR without a later user request.

## Review Focus

1. Import `assurance_intake.task` before and after `assurance_intake.plugin`: both orders must construct the same `FEATURE` and plugin descriptor (Task 1 test).
2. A symlinked or digest-changed plan/evidence ref must still fail in prepare before an OpenCode request is produced (Task 2 existing security tests plus new module-path test).
3. Exploration context must not disclose unreadable or foreign-change source evidence after its I/O moves out of `contracts/` (Task 3 tests).
4. A noncanonical or self-inconsistent sealed plan must still be rejected on readback (Task 4 test).
5. After module moves, Product must still compile the same Intake bundles and preserve contract digests and retry/resource claims (Task 1 and Task 7 tests).

## File map

- Create `packages/capabilities/assurance-intake/assurance_intake/task.py`: Task classes, `IntakeGraphs`, and Product-facing `FEATURE`.
- Delete `assurance_intake/feature.py`, `operations/agent_tasks.py`, and `operations/case_design.py` after their definitions and consumers move; do not add compatibility shims.
- Keep `contracts/attempts.py` authoritative for Agent/Task contracts, retry, timeout, resource claims, output routes, and canonical refs.
- Create `operations/prepare.py`, `operations/prepare_evidence.py`, and `operations/case_design_prepare.py`; remove `operations/agent_skills.py` once imports move. `operations/finalize.py` keeps finalize handler behavior.
- Create `operations/explore_context.py` and `operations/planning_facts.py` for existing project-file readers; `contracts/explore.py` keeps its data models and fixed path constants.
- Create `operations/plan_codec.py` for plan sealing/decoding; retain `plan_bytes` and `plan_artifact_ref` in `contracts/plan.py` because `ResolvePlanOutputV1` uses them for its own model invariant.
- Move `advance_review_round` to `operations/workflow_state.py`, `merge_history_refs` to `operations/history_refs.py`, and loop-history construction to `operations/loop_history.py`; leave their model classes in `contracts/`. The architecture scan permits only the exact graph-to-pure-operation imports this requires.
- Move obligation normalization and external journey-document reading to `operations/obligations.py`, and inventory cross-reference checks to `operations/impact_validation.py`; retain model-local validators in `contracts/`.
- Update the Intake imports in Product, graph factories, tests, and `contracts/__init__.py` as each Task lands.

---

### Task 1: Make `task.py` own Task definitions

**Files:** Create `assurance_intake/task.py`; delete `assurance_intake/feature.py`, `operations/agent_tasks.py`, `operations/case_design.py`; modify `assurance_product/features.py`, `assurance_product/graphs/factory.py`, `assurance_intake/graphs/factory.py`; test `tests/product/test_feature_entrypoints.py`, `tests/product/test_agent_task_lifecycles.py`, and `tests/product/test_semantic_attempt_bindings.py`.

**Interfaces:** Consumes `AGENT_JOB_CONTRACTS`, `TASK_ATTEMPT_CONTRACTS`, `OUTPUT_ROUTE_TEMPLATES`, `IntakePlugin`, and injected `PreparePhase`/`RuntimePhase`/`FinalizePhase`. Produces `assurance_intake.task.FEATURE`, `IntakeGraphs`, and four Task classes with their existing method signatures.

- [ ] **Step 1: Add a failing ownership/import-order test** in `tests/product/test_feature_entrypoints.py`:

```python
def test_intake_task_module_owns_lifecycle_classes() -> None:
    import subprocess
    import sys

    from assurance_intake.task import FEATURE, IntakeTask, ExploreTask, CaseDesignTask, CaseReviewTask
    from assurance_intake.plugin import IntakePlugin

    assert FEATURE.plugin is IntakePlugin
    assert FEATURE.agent_task_types == (IntakeTask, ExploreTask, CaseDesignTask, CaseReviewTask)
    assert all(task.__module__ == "assurance_intake.task" for task in FEATURE.agent_task_types)
    assert IntakePlugin.descriptor().attempt_contracts
    for first, second in (("task", "plugin"), ("plugin", "task")):
        subprocess.run(
            [sys.executable, "-c", f"import assurance_intake.{first}; import assurance_intake.{second}"],
            check=True,
        )
```

- [ ] **Step 2: Run red:** `uv run pytest tests/product/test_feature_entrypoints.py::test_intake_task_module_owns_lifecycle_classes -q`; expect `ModuleNotFoundError: assurance_intake.task`.
- [ ] **Step 3: Move exact class bodies without changing phase methods**, and update all imports from the three removed paths. In both parameterized tests in `test_feature_entrypoints.py`, use `import_module(f"{module}.{'task' if module == 'assurance_intake' else 'feature'}")`; the other five wheels keep `.feature`. `task.py` must assemble the same `FEATURE` value:

```python
@dataclass(frozen=True, slots=True)
class IntakeGraphs:
    prepare: CompiledStateGraph
    load_plan: CompiledStateGraph
    case: CompiledStateGraph

FEATURE = FeatureSpec(
    plugin=IntakePlugin,
    agent_contracts=AGENT_JOB_CONTRACTS,
    task_contracts=TASK_ATTEMPT_CONTRACTS,
    output_route_templates=OUTPUT_ROUTE_TEMPLATES,
    graph_factory=FeatureFactoryRef("assurance.intake", "assurance_intake.graphs.factory:build_intake_graphs"),
    agent_task_types=(IntakeTask, ExploreTask, CaseDesignTask, CaseReviewTask),
)
```

- [ ] **Step 4: Run green:** `uv run pytest tests/product/test_feature_entrypoints.py tests/product/test_agent_task_lifecycles.py tests/product/test_semantic_attempt_bindings.py packages/capabilities/assurance-intake/tests/test_intake_graph_factory.py -q --tb=short`; expect all pass. Also run `rg -n 'assurance_intake\.feature|operations\.(agent_tasks|case_design)' packages tests`; expect no Intake import paths.
- [ ] **Step 5: Commit:** `git add -A packages/capabilities/assurance-intake/assurance_intake packages/products/assurance-product/assurance_product tests/product` then `git commit -m "refactor: make intake task module own lifecycle classes"`.

### Task 2: Split prepare work by responsibility

**Files:** Create `operations/prepare.py`, `operations/prepare_evidence.py`, `operations/case_design_prepare.py`; delete `operations/agent_skills.py`; modify `operations/__init__.py`, `operations/finalize.py`, `operations/workflow_state.py`, and Intake tests.

**Interfaces:** `prepare.py` exports `IntakePrepareHandler`, `ExplorePrepareHandler`, `CaseReviewPrepareHandler`, `InputError`, `failed_input`, `validate_input`, and `case_review_outputs`; `case_design_prepare.py` exports `CaseDesignPrepareHandler`; `prepare_evidence.py` provides existing regular-file/ref/plan authentication to those handlers. The four plugin handler IDs stay unchanged.

- [ ] **Step 1: Add a red module-layout assertion** to `packages/capabilities/assurance-intake/tests/test_agent_skills.py`:

```python
def test_prepare_handlers_live_in_operations() -> None:
    from assurance_intake.operations.prepare import IntakePrepareHandler, ExplorePrepareHandler, CaseReviewPrepareHandler
    from assurance_intake.operations.case_design_prepare import CaseDesignPrepareHandler

    assert all(cls.__module__.startswith("assurance_intake.operations.") for cls in (
        IntakePrepareHandler, ExplorePrepareHandler, CaseDesignPrepareHandler, CaseReviewPrepareHandler,
    ))
```

- [ ] **Step 2: Run red:** `uv run pytest packages/capabilities/assurance-intake/tests/test_agent_skills.py::test_prepare_handlers_live_in_operations -q`; expect missing `operations.prepare`.
- [ ] **Step 3: Move the existing functions and class bodies verbatim**, update imports, and remove the old file. Keep the common request assembly in `prepare.py` and call evidence authentication from `prepare_evidence.py`:

```python
from assurance_intake.operations.prepare_evidence import _authenticate_evidence_refs, _authenticate_plan
from assurance_intake.operations.case_design_prepare import CaseDesignPrepareHandler

# operations/__init__.py still exports the same four PrepareHandler names.
```

The original `_review_repair_contract` and `CaseDesignPrepareHandler` move together to `case_design_prepare.py`; retain their existing validation and failure conversion. Do not broaden write roots or change retryability.
- [ ] **Step 4: Run green:** `uv run pytest packages/capabilities/assurance-intake/tests/test_agent_skills.py tests/product/test_agent_execution_contracts.py tests/product/test_change_local_output_routing.py -q --tb=short`; expect all pass. Run `rg -n 'operations\.agent_skills' packages tests`; expect no imports.
- [ ] **Step 5: Commit:** `git add -A packages/capabilities/assurance-intake tests/product` then `git commit -m "refactor: separate intake prepare operations"`.

### Task 3: Move project-file readers out of data models

**Files:** Create `operations/planning_facts.py` from `contracts/planning_facts.py` and `operations/explore_context.py` from the non-model section of `contracts/explore.py`; delete the former contracts module; modify Intake operations and tests.

**Interfaces:** `operations.planning_facts.build_planning_facts(workspace: Path, *, change_id: str, capability_leafs: tuple[str, ...], families: tuple[str, ...], target_files: tuple[str, ...] = ()) -> dict[str, Any]` and `source_path_hints(text: str) -> tuple[str, ...]`. `operations.explore_context.build_explore_context(workspace: Path, *, change_id: str, capability_leafs: tuple[str, ...]) -> ExploreContextV1` and `load_exploration_document(data: bytes) -> ExploreAdvisoryV1 | PreparedExploreV1`; `contracts.explore` keeps those three models and fixed path constants.

- [ ] **Step 1: Change the imports first** in `test_explore_context.py` and `test_planning_facts.py`:

```python
from assurance_intake.operations.explore_context import build_explore_context
from assurance_intake.operations.planning_facts import build_planning_facts
```

- [ ] **Step 2: Run red:** `uv run pytest packages/capabilities/assurance-intake/tests/test_explore_context.py packages/capabilities/assurance-intake/tests/test_planning_facts.py -q --tb=short`; expect missing new modules.
- [ ] **Step 3: Relocate the existing reader/decoder functions and their private helpers**, then update `operations/prepare.py`, `operations/case_design_prepare.py`, `operations/plan_artifacts.py`, and `operations/finalize.py` to import from `operations`. Keep model constants under `contracts.explore`; `contracts/` must not import `operations/`:

```python
from assurance_intake.contracts.explore import ExploreContextV1, ExploreAdvisoryV1, PreparedExploreV1
from assurance_intake.operations.planning_facts import source_path_hints
```

- [ ] **Step 4: Run green:** repeat the Step 2 tests plus `test_obligation_sources.py` and `test_agent_skills.py`; expect all pass, including foreign-change, malformed-evidence, symlink, and bounded-file cases.
- [ ] **Step 5: Commit:** `git add -A packages/capabilities/assurance-intake` then `git commit -m "refactor: move intake project readers to operations"`.

### Task 4: Separate plan sealing from plan models

**Files:** Create `operations/plan_codec.py`; modify `contracts/plan.py`, `operations/resolve_plan.py`, `operations/plan_artifacts.py`, `operations/finalize.py`, `operations/prepare_evidence.py`, exact cross-wheel import sites, `.importlinter`, and plan tests.

**Interfaces:** `operations.plan_codec.seal_plan(payload: dict[str, object]) -> ResolvedAssurancePlan` and `decode_plan(data: bytes, ref: EvidenceArtifactRefV1) -> ResolvedAssurancePlan`. `contracts.plan.plan_bytes` and `plan_artifact_ref` remain because the plan output model validates its own sealed reference with them.

- [ ] **Step 1: Change test imports first** in `test_resolve_plan.py` and Product plan-flow/recovery tests:

```python
from assurance_intake.operations.plan_codec import decode_plan, seal_plan
from assurance_intake.contracts.plan import plan_artifact_ref, plan_bytes
```

- [ ] **Step 2: Run red:** `uv run pytest packages/capabilities/assurance-intake/tests/test_resolve_plan.py::test_plan_decoder_rejects_noncanonical_or_self_inconsistent_bytes -q`; expect missing `operations.plan_codec`.
- [ ] **Step 3: Move `seal_plan` and `decode_plan` bodies verbatim**, importing `ResolvedAssurancePlan`, `plan_artifact_ref`, and `plan_bytes` from `contracts.plan`. Remove their old exports and update callers:

```python
from assurance_intake.contracts.plan import ResolvedAssurancePlan, plan_artifact_ref, plan_bytes

def decode_plan(data: bytes, ref: EvidenceArtifactRefV1) -> ResolvedAssurancePlan:
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("plan is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("plan document must be an object")
    if canonical_json_bytes(cast(JSONValue, payload)) != data:
        raise ValueError("plan bytes must be canonical JSON")
    if hashlib.sha256(data).hexdigest() != ref.digest:
        raise ValueError("plan_ref digest does not match plan bytes")
    plan = ResolvedAssurancePlan.model_validate(payload)
    if plan_artifact_ref(plan) != ref:
        raise ValueError("plan_ref path does not match plan_digest")
    return plan
```

- [ ] **Step 4: Run green:** `uv run pytest packages/capabilities/assurance-intake/tests/test_resolve_plan.py tests/product/test_acg_plan_flow.py tests/product/test_acg_plan_loading.py tests/product/test_acg_plan_recovery.py -q --tb=short`; expect all pass.
- [ ] **Step 5: Commit:** `git add -A packages/capabilities/assurance-intake tests/product` then `git commit -m "refactor: separate intake plan codec from models"`.

### Task 5: Put workflow computation beside its consumers

**Files:** Modify `contracts/decisions.py`, `contracts/workflow.py`, `contracts/loop_history.py`, `contracts/__init__.py`, `operations/workflow_state.py`, `operations/case_review_seal.py`, `graphs/state.py`, `graphs/nodes.py`, and their tests; add `operations/history_refs.py` and `operations/loop_history.py`; update cross-wheel callers and `.importlinter`.

**Interfaces:** Keep `ReviewRoundAdvanceInput/Output`, `EvidenceArtifactRefV1`, and `LoopRoundHistoryV1` in `contracts/`; move `advance_review_round` to `operations/workflow_state.py`, `merge_history_refs` to `operations/history_refs.py`, and `build_loop_round_history` to `operations/loop_history.py` with current signatures and behavior. Shared operations avoid cross-Feature `graphs` imports and case-review-specific coupling.

- [ ] **Step 1: Update import assertions first** in `test_attempt_contracts.py`, `test_graph_interrupts.py`, and `test_product_input.py`:

```python
from assurance_intake.operations.workflow_state import advance_review_round
from assurance_intake.operations.history_refs import merge_history_refs
from assurance_intake.operations.loop_history import build_loop_round_history
```

- [ ] **Step 2: Run red:** `uv run pytest packages/capabilities/assurance-intake/tests/test_graph_interrupts.py::test_advance_review_round_node_is_the_moved_pure_function tests/product/test_product_input.py -q --tb=short`; expect import errors for the moved names.
- [ ] **Step 3: Move the existing function bodies without altering validation or reducer order**, update `graphs/nodes.py` and old exports, and keep model modules independent from operations:

```python
# graphs/state.py imports from operations.history_refs
history_refs: Annotated[list[dict[str, str]], merge_history_refs]

# operations/workflow_state.py
def advance_review_round(data: object) -> ReviewRoundAdvanceOutput:
    try:
        payload = ReviewRoundAdvanceInput.model_validate(data)
    except ValidationError as error:
        raise error
    if payload.rounds_used >= payload.rounds_budget:
        raise ValueError("rounds_used must be below rounds_budget")
    return ReviewRoundAdvanceOutput(
        rounds_used=payload.rounds_used + 1,
        rounds_budget=payload.rounds_budget,
    )
```

- [ ] **Step 4: Run green:** `uv run pytest packages/capabilities/assurance-intake/tests/test_attempt_contracts.py packages/capabilities/assurance-intake/tests/test_graph_interrupts.py packages/capabilities/assurance-intake/tests/test_workflow_state.py tests/product/test_product_input.py -q --tb=short`; expect all pass.
- [ ] **Step 5: Commit:** `git add -A packages/capabilities/assurance-intake tests/product` then `git commit -m "refactor: localize intake workflow computation"`.

### Task 6: Move cross-document normalization to operations

**Files:** Modify `contracts/quality_goals.py`, `contracts/impact.py`, `operations/obligations.py`, `operations/resolve_plan.py`, `operations/plan_artifacts.py`, `operations/finalize.py`; create `operations/impact_validation.py`; update Intake, Generation, Quality, and Product callers, their tests, and `.importlinter`.

**Interfaces:** `operations.obligations` owns `normalize_obligation_drafts`, `normalize_goal_obligations`, `required_goal_families`, and the external-document reader `journey_keys_from_document`; `operations.impact_validation` owns `validate_inventory_references`, `validate_inventory_closed_keys`, and `impact_required_families`. The underlying model types and model-local policy remain under `contracts/`.

- [ ] **Step 1: Change the relevant test imports first** in `test_prepared_quality_goals.py`, `test_obligation_normalization.py`, and `test_impact_contracts.py`:

```python
from assurance_intake.operations.obligations import normalize_obligation_drafts, normalize_goal_obligations, required_goal_families
from assurance_intake.operations.impact_validation import validate_inventory_references, validate_inventory_closed_keys, impact_required_families
```

- [ ] **Step 2: Run red:** `uv run pytest packages/capabilities/assurance-intake/tests/test_prepared_quality_goals.py packages/capabilities/assurance-intake/tests/test_impact_contracts.py -q --tb=short`; expect missing new exports.
- [ ] **Step 3: Move the named function bodies and private helpers**, update their callers and `__all__`, and leave only model-local validators in `contracts/`:

```python
from assurance_intake.contracts.quality_goals import PreparedQualityGoalV1
from assurance_intake.contracts.impact import ChangeImpactInventoryV1

# Cross-document transformations live here, not in the model modules.
```

- [ ] **Step 4: Run green:** repeat Step 2, plus `test_obligation_normalization.py`, `test_agent_skills.py`, `test_resolve_plan.py`, and `tests/product/test_acg_plan_recovery.py`; expect all pass.
- [ ] **Step 5: Commit:** `git add -A packages/capabilities/assurance-intake tests/product` then `git commit -m "refactor: move intake normalization to operations"`.

### Task 7: Final installed-wheel and repository verification

**Files:** Update any remaining Intake import sites found by `rg`; no new runtime interface. Check `contracts/__init__.py`, `plugin.py`, `plugin-declaration.json`, and Product import paths.

**Interfaces:** `assurance_intake.task` is the only Product-facing Task entrance; `contracts/attempts.py` remains the unchanged policy catalog and plugin refs. Existing output types, execution semantics, and graph roots remain identical.

- [ ] **Step 1: Add an import/closure regression test** to `tests/product/test_feature_entrypoints.py`:

```python
def test_intake_plugin_contract_refs_match_task_catalog() -> None:
    from assurance_intake.plugin import IntakePlugin
    from assurance_intake.task import FEATURE
    from assurance_intake.contracts.attempts import attempt_contract_refs

    assert IntakePlugin.descriptor().attempt_contracts == attempt_contract_refs()
    assert set(FEATURE.agent_contracts) == {"intake", "explore", "case-design", "case-review"}
```

- [ ] **Step 2: Run red/green around the final test:** `uv run pytest tests/product/test_feature_entrypoints.py -q`; if red, repair the relevant import/metadata path only; then require green.
- [ ] **Step 3: Run static and complete tests:** `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run lint-imports`, and `uv run pytest -q --tb=short`; require exit 0 for each, with pyright warnings reported separately.
- [ ] **Step 4: Run installed-wheel smokes:** `scripts/graph_engine_smoke_test.sh`, `scripts/assurance_capability_wheel_smoke_test.sh`, `scripts/assurance_product_wheel_smoke_test.sh`. Compare any capability smoke failure to the unchanged `assurance_execution/resources/personas/executor.md` baseline before attributing it to Intake.
- [ ] **Step 5: Check `git diff --check`, `git status --short`, and `git diff origin/main..HEAD`**, then commit only any necessary final import/test fixes with `git commit -m "test: verify intake task-module migration"`. Do not push or open a PR unless requested.
