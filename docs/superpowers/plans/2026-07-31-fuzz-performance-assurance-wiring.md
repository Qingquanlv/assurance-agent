# Fuzz/Performance Assurance Wiring and Frozen Policy Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote Fuzz and Performance from declared-but-unwired profiles to fully enforced capability-contract assurance layers, including deterministic applicability, reviewed mechanical evidence, fail-closed codegen gates, recoverable manual plan revision, and topology-driven frozen replay.

**Architecture:** Keep profiles as immutable metadata and keep the workflow schema as the explicit control plane. Current compilation validates all four packaged layer contracts, while historical compilation binds only pinned definitions and classifies each pinned layer as wired, legacy-unwired, or partial before evidence recovery. Manual `fix_and_proceed` is a version-5 leaf-tree transition with a durable bounded revision view and a ledger-prefix recovery protocol; it does not mutate ancestor trees or treat mutable files as evidence.

**Tech Stack:** Python 3.11, Pydantic v2, frozen dataclasses, YAML workflow DSL, content-addressed `TreeStore`, append-only graph events, pytest, Ruff, Pyright, import-linter, uv.

## Global Constraints

- The authoritative design is `docs/superpowers/specs/2026-07-31-fuzz-performance-assurance-wiring-design.md`; preserve every invariant and error code defined there.
- Profiles declare metadata and applicability inputs. They do not generate graph topology, return routes, or override gate verdicts.
- The workflow schema remains the explicit control plane. Do not add prompt-owned phase transitions, workflow-state mutation, or implicit graph generation from profiles.
- `PlanReview` is the runtime model for all four plan-review layers. The generic `Review.layer_applicable` field remains readable for unrelated legacy artifacts but is never consumed by Fuzz/Performance applicability, gates, codegen, or replay.
- Fuzz/Performance have no automatic plan fixer in this increment. `needs_fix` must route to the audited human interrupt, and `fix_and_proceed` must return to review.
- A checks file is authoritative only when its successful producer attempt is committed, its tree bytes match the output digest, and the later gate read the same digest.
- Current packaged compilation must require complete four-layer activation. Historical pinned compilation must never load current execution contracts, the current ingest catalog, or current model-schema digests.
- A recognized legacy Fuzz/Performance graph has none of the three activation markers: applicability producer, reviewed mechanical producer, or explicit cycle gate. Any marker without the complete contract is partial wiring, never `not_wired`.
- Manual plan revision may replace only exact `change:plans/<declared-file>.md` files declared on the interrupt. It may not ingest review, checks, L1, product code, tests, configuration, additions, deletions, or symlinks.
- `manual_plan_revision` advances only the interrupt-producing leaf invocation tree. Existing child write-set propagation remains the only path by which the revised result reaches branch and root trees.
- `ProgressionTxn` is not power-loss atomic. Treat target-object publication, the revision event, and every root-to-leaf resume event as legal durable prefixes and recover them under the progression lock.
- Once `manual_plan_revision` is committed, recovery uses only committed event fields and immutable tree objects; it never rereads the mutable revision view.
- Version-5 gate decisions bind the source gate attempt and source committed tree. A decision cannot authorize a later gate evidence epoch after a manual tree revision.
- The deferred Fuzz auth-policy field remains parseable but must occur in neither gate expressions nor claims of runtime enforcement.
- Preserve API/E2E behavior. Do not retroactively require their parent graphs to add the Fuzz/Performance cases-only preflight.
- Preserve schema-version-2 specialty reports with replay semantics v1 and legacy specialty schema-version 1. New reports emit replay semantics v2 and do not rewrite old files.
- Do not read active change/repo/project files as a replay fallback when committed object-tree evidence is missing.
- Write every behavior test first, run it, and observe the expected failure before production changes.
- Do not assume the worktree or index is clean. Complete the execution preflight below and stage only the files explicitly listed by each task.
- Agents sharing one worktree also share one Git index. A single integrator must serialize every `git add` and `git commit`; subagents may perform read-only analysis or edit disjoint files, but they never stage or commit concurrently.
- Run all Python commands through `uv run`. The final gate is Ruff, format check, Pyright, import-linter, full pytest, and packaging smoke.

---

## File Structure

### New production files

- `assurance_agent/artifacts/policy_obligations.py` — typed registry for intentionally deferred policy leaves.
- `assurance_agent/workflow/graph/manual_revision.py` — manual-revision view validation, deterministic transition construction, and legal-prefix recovery planning.

### New canonical fixtures and tests

- `tests/fixtures/assurance/fuzz-contract/` — applicable Fuzz case, plans, strong review, and L1 fixture.
- `tests/fixtures/assurance/performance-contract/` — applicable Performance case, plans, strong review, and L1 fixture.
- `tests/unit/test_fuzz_performance_skills.py` — prompt/output/contract parity guards.
- `tests/unit/workflow/graph/test_manual_revision.py` — exact tree replacement, transition identity, and prefix validation.
- `tests/integration/test_fuzz_performance_assurance_flow.py` — real full, inapplicable, codegen-only, remediation, and nested revision flows.

### Modified production areas

- `assurance_agent/artifacts/models/review.py`, `assurance_agent/artifacts/registry.py`, `assurance_agent/verification/profiles.py` — strong four-layer review contracts and exact artifact ownership.
- `assurance_agent/_resources/skills/aa-{fuzz,performance}-{plan,plan-reviewer,codegen}/SKILL.md` — capability/evidence authoring instructions without workflow-state control.
- `assurance_agent/_resources/schemas/execution-contracts.yaml` — exact Fuzz/Performance reads, writes, locks, and authorization prefixes.
- `assurance_agent/_resources/schemas/workflow-schema.yaml` — Fuzz/Performance preflight, explicit review/check/gate cycles, remediation, and hard codegen preconditions.
- `assurance_agent/workflow/graph/schema_v2.py`, `compiler.py`, `replay_schema.py` — manual-revision schema, current activation validation, historical validation, and pinned topology classification.
- `assurance_agent/workflow/graph/workspace.py`, `handlers/interrupt.py`, `resume_wire.py` — bounded revision views and immutable leaf-tree replacement.
- `assurance_agent/workflow/core/graph_events.py`, `migrate_events.py`, `workflow/graph/models.py`, `checkpoint.py` — version-5 events and projections.
- `assurance_agent/workflow/graph/definition_pinning.py`, `verification/profile_manifest.py` — immutable assurance-profile snapshots.
- `assurance_agent/workflow/graph/runtime.py`, `workflow/core/progression.py`, `workflow/graph/status.py`, `commands/status_cmd.py` — manual revision ingestion and crash-prefix repair.
- `assurance_agent/workflow/graph/handlers/gate.py`, `workflow/orchestration/gates.py` — source gate attempt/tree decision epochs.
- `assurance_agent/workflow/graph/ingest_catalog.py`, `contracts.py`, `replay_binding.py` — pinned-only historical definition reconstruction and evidence binding.
- `assurance_agent/eval/specialty_models.py`, `specialty_replay.py`, `specialty_render.py` — topology-driven replay semantics v2.
- `benchmark/vue-fastapi-admin/benchmark/benchmark_specialty_report.py`, `cursor-loop-helpers.sh`, `run-workflow-loop-cursor.sh` — v2 replay invocation without a current schema-root escape hatch.
- `docs/schemas.md` — event v5, manual revision, profile snapshot, and replay v2 contracts.

---

## Execution Preflight

- [ ] Record `git rev-parse HEAD`, then inspect `git status --short` and `git diff --cached --name-only`.
- [ ] Require a clean tracked worktree and empty index before Task 1. If any tracked path is modified or staged by another session—or any untracked path can be imported, packaged, or discovered by tests—stop and let that owner finish, create a reviewed baseline commit, or use an isolated worktree. Do not stash, discard, or absorb it into this work; a green result from contaminated source bytes is not reproducible from the task commit.
- [ ] Before every task commit, inspect all three outputs: `git diff --cached --name-only`, full `git diff --cached`, and `git diff --cached --check`. The staged path set and hunks must equal that task's declared work.
- [ ] Before every task commit that changes Python, run Ruff on the changed Python paths and run the full `uv run pyright`; focused pytest alone is not an intermediate green gate.
- [ ] Run tasks in the numbered order below. No task that publishes a prompt, registry/profile, packaged graph, or event-version switch may be moved ahead of Task 13.

---

### Task 1: Extend Strong Review Models Without Activating Fuzz/Performance Yet

**Files:**
- Modify: `assurance_agent/artifacts/models/review.py` — `PlanReview`, `PlanReviewAuthoring`, separate broad/strong type sets.
- Test: `tests/unit/artifacts/test_models_review_explore.py`

**Interfaces:**
- Produces: `PlanReview` and `PlanReviewAuthoring` accepting exactly `api-plan`, `e2e-plan`, `fuzz-plan`, and `performance-plan` as capability-gated review types.
- Enforces: non-empty fully qualified `required_capabilities`, non-blank `findings[].id`, and the Fuzz/Performance human-only invariant.
- Preserves: the current Fuzz/Performance wildcard registry/profile binding until the atomic activation in Task 13, so old prompts and gates are not split from their consumers.

- [ ] **Step 1: Add failing model tests for all four review types and the human-only invariant**

Add these tests:

```python
@pytest.mark.parametrize("review_type", ["fuzz-plan", "performance-plan"])
def test_human_only_plan_review_rejects_automatic_fix(review_type: str) -> None:
    payload = valid_plan_review(review_type=review_type)
    payload["auto_fix_allowed"] = True
    payload["auto_fix_plan"] = ["run a fixer"]

    with pytest.raises(ValidationError, match="human-only plan review"):
        PlanReview.model_validate(payload)


@pytest.mark.parametrize("review_type", ["fuzz-plan", "performance-plan"])
def test_human_only_authoring_requires_empty_auto_fix_plan(review_type: str) -> None:
    payload = valid_plan_review_authoring(review_type=review_type)
    payload["auto_fix_plan"] = ["rewrite plan"]

    with pytest.raises(ValidationError, match="auto_fix_plan must be empty"):
        PlanReviewAuthoring.model_validate(payload)
```

Also mutate `required_capabilities` to `[]`, `[""]`, and `["capability"]`, and mutate a finding ID to whitespace. Each mutation must fail authoring and runtime validation.

- [ ] **Step 2: Run the focused tests and observe Fuzz/Performance rejection or weak validation**

Run:

```bash
uv run pytest -q \
  tests/unit/artifacts/test_models_review_explore.py
```

Expected: the new types are rejected or the new conditional invariants are not enforced.

- [ ] **Step 3: Expand and document the strong review models**

Implement this public shape in `review.py` without changing broad `Review` behavior:

```python
_CAPABILITY_GATED_REVIEW_TYPES = frozenset({"api-plan", "e2e-plan"})
_PLAN_REVIEW_TYPES = frozenset(
    {"api-plan", "e2e-plan", "fuzz-plan", "performance-plan"}
)
_HUMAN_ONLY_PLAN_REVIEW_TYPES = frozenset({"fuzz-plan", "performance-plan"})


class PlanReview(Review):
    """Strong cross-skill contract for API, E2E, Fuzz, and Performance plans."""

    @model_validator(mode="after")
    def _require_cross_skill_fields(self) -> "PlanReview":
        if self.review_type not in _PLAN_REVIEW_TYPES:
            raise ValueError("unsupported plan review type")
        self._validate_required_cross_skill_fields()
        self._validate_nonblank_finding_ids()
        self._validate_fully_qualified_capabilities()
        if self.review_type in _HUMAN_ONLY_PLAN_REVIEW_TYPES:
            if self.auto_fix_allowed or self.auto_fix_plan:
                raise ValueError("human-only plan review cannot authorize automatic fixes")
        return self


class PlanReviewAuthoring(BaseModel):
    review_type: Literal[
        "api-plan", "e2e-plan", "fuzz-plan", "performance-plan"
    ]
```

The broad `Review` validator continues to use the unchanged `_CAPABILITY_GATED_REVIEW_TYPES`; only `PlanReview`/`PlanReviewAuthoring` use `_PLAN_REVIEW_TYPES`. Use shared helpers for non-blank finding IDs and fully qualified L1 leaf keys so authoring and runtime models cannot drift. Fully qualified keys must contain at least one dot and resolve through one of the L1 root mappings accepted by `capabilities_present`. Do not remove `layer_applicable` from the broad `Review` model.

- [ ] **Step 4: Prove this preparatory commit does not activate the new producer/consumer edge**

Add a regression assertion against the unchanged runtime registry/profile:

```python
assert match_artifact("review/fuzz-plan-review.json").model is Review
assert get_layer_assurance_profile("fuzz").review_model is Review
```

Keep this assertion in a Task 13 activation test and change its expected model there; do not change registry/profile production files in Task 1.

- [ ] **Step 5: Run the model seam**

Run:

```bash
uv run pytest -q \
  tests/unit/artifacts/test_models_review_explore.py
uv run pyright
```

Expected: all tests pass and Pyright reports zero errors.

- [ ] **Step 6: Commit the typed review boundary**

```bash
git add assurance_agent/artifacts/models/review.py tests/unit/artifacts/test_models_review_explore.py
git diff --cached --check
git commit -m "feat(review): enforce fuzz and performance plan contracts"
```

---

### Task 2: Add Canonical Fuzz/Performance Contract Fixtures Without Publishing Skills

**Files:**
- Create: `tests/fixtures/assurance/fuzz-contract/cases/FUZZ-001/case.yaml`
- Create: `tests/fixtures/assurance/fuzz-contract/plans/fuzz-plan.md`
- Create: `tests/fixtures/assurance/fuzz-contract/plans/fuzz-codegen-plan.md`
- Create: `tests/fixtures/assurance/fuzz-contract/review/fuzz-plan-review.json`
- Create: `tests/fixtures/assurance/fuzz-contract/.aa/data-knowledge.yaml`
- Create: `tests/fixtures/assurance/performance-contract/cases/PERF-001/case.yaml`
- Create: `tests/fixtures/assurance/performance-contract/plans/performance-plan.md`
- Create: `tests/fixtures/assurance/performance-contract/plans/performance-codegen-plan.md`
- Create: `tests/fixtures/assurance/performance-contract/review/performance-plan-review.json`
- Create: `tests/fixtures/assurance/performance-contract/.aa/data-knowledge.yaml`
- Create: `tests/unit/verification/test_fuzz_performance_contract_fixtures.py`

**Interfaces:**
- Produces: canonical `## Factory Mapping` tables consumed by `shared_factory`.
- Produces: applicable reviews whose `required_capabilities` resolve against L1 and whose remediation fields match human-only topology.
- Preserves: the currently published six skills until Task 13 can switch prompts, registry/profile, graph gates, replay, and v5 emission together.

- [ ] **Step 1: Add failing canonical fixture tests**

Create `test_fuzz_performance_contract_fixtures.py` with exact assertions:

```python
@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_is_a_strong_review_and_canonical_plan(layer: str) -> None:
    fixture = load_contract_fixture(layer)
    authored = PlanReviewAuthoring.model_validate_json(fixture.review_bytes)
    review = PlanReview.model_validate(authored.model_dump(mode="json"))
    assert review.review_type == f"{layer}-plan"
    assert review.required_capabilities
    assert review.auto_fix_allowed is False
    assert review.auto_fix_plan == []
    assert "## Factory Mapping" in fixture.codegen_plan
    assert "| Shared Module | Function | Ownership |" in fixture.codegen_plan
```

- [ ] **Step 2: Add lower-level canonical round trips independent of registry activation**

For each fixture, call the public seams in this order:

```python
authored = PlanReviewAuthoring.model_validate_json(review_bytes)
review = PlanReview.model_validate(authored.model_dump(mode="json"))
checks = run_plan_checks(context, applicability=applicability)
state = plan_assurance_state(
    checks.model_dump(mode="json"),
    review.model_dump(mode="json"),
    l1_mapping,
    profile.layer,
    change_id="CH-CANONICAL",
)

assert state == "applicable"
assert [item.status for item in checks.checks] == [
    "pass",
    "pass",
    "not_applicable",
    "pass",
]
```

Mutate the Factory Mapping header, remove one plan, use an unknown capability, and remove its L1 leaf. Each mutation must fail or produce a finding; none may silently become `not_applicable`.

- [ ] **Step 3: Run the new test and observe missing fixture failures**

```bash
uv run pytest -q \
  tests/unit/verification/test_fuzz_performance_contract_fixtures.py
```

Expected: fixture paths or their canonical contents are missing.

- [ ] **Step 4: Encode the final authoring contract in fixture bytes**

The Fuzz fixture covers schema source, related API case, authentication semantics, and seed/corpus adequacy. The Performance fixture covers absolute thresholds, load shape, scenario coverage, and statistical interpretation. Both review documents use:

```yaml
review_type: fuzz-plan  # performance-plan in the Performance skill
decision: pass | needs_fix | changes_requested | needs_human_review | reject
auto_fix_allowed: false
auto_fix_plan: []
required_capabilities:
  - capabilities.domain_factories.account
next_action: continue | manual_fix | human_review | stop
```

Do not add `layer_applicable`; empty scope is represented only by mechanical applicability in later activation tests.
The matching `.aa/data-knowledge.yaml` fixture must contain the concrete leaf `capabilities.domain_factories.account`, so the example proves real L1 resolution rather than only string-shape validation.

- [ ] **Step 5: Complete the canonical fixtures and rerun the round trip**

Ensure both codegen plans contain this exact parser-visible table:

```markdown
## Factory Mapping

| Shared Module | Function | Ownership |
|---|---|---|
| tests/factories/account.py | make_account | reuse |
```

Run the test from Step 3. Expected: strong-model and lower-level mechanical round trips pass for both layers.

- [ ] **Step 6: Commit canonical contract fixtures**

```bash
git add tests/fixtures/assurance/fuzz-contract tests/fixtures/assurance/performance-contract tests/unit/verification/test_fuzz_performance_contract_fixtures.py
git diff --cached --check
git commit -m "test(assurance): add fuzz performance contract fixtures"
```

---

### Task 3: Prepare Exact Execution Contracts Without Switching the Graph

**Files:**
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml` — six skills and mechanical operation ownership.
- Modify: `tests/unit/workflow/graph/test_contracts.py`
- Modify: `tests/unit/workflow/graph/test_read_isolation.py`
- Modify: `tests/unit/verification/test_applicability.py`

**Interfaces:**
- Consumes: the exact final skill contract stated in the design spec and represented by Task 2 fixtures.
- Produces: exact authorization for the six future skill contracts before those prompts are published.
- Makes: the generic mechanical operation a write/lock shell with `read_isolation: declared_only`; its sole static read is the project L1 synchronization bound, while planner resource narrowing makes each graph node's declared reads the complete materialized read view.

- [ ] **Step 1: Add failing exact contract tests**

For each of the six targets, assert the final mandatory logical inputs are covered by reads, every output by writes, and every write by `authorization_writes`. Plan targets read exact cases, proposal, config, L1, fact baseline, their optional per-skill memory file, and bounded same-layer style paths. Reviewers read exact plans/summary, selected cases, L1, and evaluated config. Codegen reads exact plans/summary, exact review, exact checks, selected cases, config, L1, its optional memory file, and bounded existing-test/style paths. Add explicit negative guards:

```python
fuzz_reviewer = catalog.contracts["skill:aa-fuzz-plan-reviewer"]
assert not any(
    path_covers(
        ResourcePath.parse(pattern),
        ResourcePath.parse("change:review/fuzz-plan-checks.json"),
    )
    for pattern in fuzz_reviewer.authorization_writes
)
assert fuzz_reviewer.authorization_writes == (
    "change:review/fuzz-plan-review.json",
    "change:review/fuzz-plan-review-summary.md",
)
```

Add the equivalent Performance assertion and reject broad `repo:**`, `change:plans/**`, or `change:review/**` ownership for these six targets.

Load the complete execution-contract catalog and assert the mechanical contract remains valid with its synchronized project L1 bound. A mutation that removes the sole covering `project:.aa/data-knowledge.yaml` static read while retaining the synchronized path must fail catalog loading with `synchronized path must be covered by reads or writes`; this prevents the preparatory contract from relying on node declarations that do not exist during catalog validation.

- [ ] **Step 2: Pin the exact target matrix and mechanical isolation**

Assert the exact three outputs for each plan target, exact two outputs for each reviewer, and exact codegen summary/test prefixes. Reviewer authorization must exclude checks. Codegen reads must include `change:review/<layer>-plan-checks.json`; plan/reviewer/codegen reads must include `repo:.aa/data-knowledge.yaml` where their final prompt consumes L1.

For `operation:verify-plan-mechanical`, assert `reads == ("project:.aa/data-knowledge.yaml",)` and `read_isolation == "declared_only"`; that static read exists only to bound the synchronized path and is not a materialized task input after planner narrowing. Its static `writes` and `authorization_writes` must each be the same exact four-path tuple: API, E2E, Fuzz, and Performance checks artifacts. Do not use `change:review/*-plan-checks.json`: current `path_covers()` intentionally cannot prove partial-segment glob coverage, so exact node writes would fail compilation. Build synthetic API, E2E, Fuzz, and Performance nodes with exact `resources.reads`, `resources.writes`, and exact outputs, then exercise the real compile/plan resource-narrowing path—not bare `claims_for()`—and prove final task reads equal the node declaration exactly and final `authorization_writes` equals only that layer's checks artifact. Materialize a tree containing another layer's plan/review/checks, assert `test_read_isolation.py` cannot see the cross-layer inputs, and assert freezing a cross-layer checks write is rejected.

Load the existing packaged API/E2E cycles and assert both current mechanical nodes already have non-empty exact `resources.reads`; plan each node through `_narrow_task_resources()` and prove the static synchronization-only read is absent from the final materialized reads while repo L1, cases, plans, and review remain present. This is the compatibility proof that permits the contract preparation to land before Task 13 publishes Fuzz/Performance nodes.

- [ ] **Step 3: Run the focused contract tests and observe failures**

```bash
uv run pytest -q \
  tests/unit/workflow/graph/test_contracts.py \
  tests/unit/workflow/graph/test_read_isolation.py \
  tests/unit/verification/test_applicability.py \
  tests/integration/test_api_e2e_assurance_flow.py
```

Expected: old contract patterns are too broad or omit required future skill inputs.

- [ ] **Step 4: Encode exact ownership**

For each layer, use exact plan/review/check paths. The generic mechanical operation declares no artifact read glob; the canonical nodes published in Task 13 declare exact cases/plans/review/L1 reads and the exact checks write. Its only static read is the project path needed to validate the synchronization boundary. Its static write/authorization upper bound enumerates the four exact checks files, filters the planned workspace to node-declared reads, and retains these synchronized paths and lock:

```yaml
reads:
  - project:.aa/data-knowledge.yaml  # static synchronization bound; planner narrows it away
read_isolation: declared_only
writes:
  - change:review/api-plan-checks.json
  - change:review/e2e-plan-checks.json
  - change:review/fuzz-plan-checks.json
  - change:review/performance-plan-checks.json
authorization_writes:
  - change:review/api-plan-checks.json
  - change:review/e2e-plan-checks.json
  - change:review/fuzz-plan-checks.json
  - change:review/performance-plan-checks.json
synchronized:
  - project:.aa/data-knowledge.yaml
exclusive:
  - project:data-knowledge
```

Bound optional style reads to declared locations such as `repo:tests/fuzz/**`, `repo:tests/perf/**`, `repo:tests/testdata/**`, and `repo:tests/config.py`. Remove optional-source prompt text when no matching declared read is justified.

Use this exact Fuzz matrix and substitute `performance`, `tests/perf/**`, and the Performance filenames for the other layer:

```yaml
skill:aa-fuzz-plan:
  reads:
    - change:facts/fact-baseline.json
    - change:cases/**/case.yaml
    - change:proposal.md
    - repo:.aa/config.yaml
    - repo:.aa/data-knowledge.yaml
    - project:.aa/memory/aa-fuzz-plan.md
    - repo:tests/fuzz/**
  writes:
    - change:plans/fuzz-plan.md
    - change:plans/fuzz-codegen-plan.md
    - change:plans/fuzz-review-summary.md
skill:aa-fuzz-plan-reviewer:
  reads:
    - change:plans/fuzz-plan.md
    - change:plans/fuzz-codegen-plan.md
    - change:plans/fuzz-review-summary.md
    - change:cases/**/case.yaml
    - repo:.aa/config.yaml
    - repo:.aa/data-knowledge.yaml
  writes:
    - change:review/fuzz-plan-review.json
    - change:review/fuzz-plan-review-summary.md
skill:aa-fuzz-codegen:
  reads:
    - change:plans/fuzz-plan.md
    - change:plans/fuzz-codegen-plan.md
    - change:plans/fuzz-review-summary.md
    - change:review/fuzz-plan-review.json
    - change:review/fuzz-plan-checks.json
    - change:cases/**/case.yaml
    - repo:.aa/config.yaml
    - repo:.aa/data-knowledge.yaml
    - project:.aa/memory/aa-fuzz-codegen.md
    - repo:tests/fuzz/**
    - repo:tests/testdata/**
    - repo:tests/config.py
  writes:
    - change:codegen/fuzz-codegen-summary.md
    - repo:tests/fuzz/**
    - repo:tests/testdata/**
```

For each target, set `authorization_writes` to its exact `writes` tuple. Preserve `repo:test-infra` exclusivity for codegen. A mandatory plan/reviewer/codegen input omitted from this matrix must be removed from the final skill instead of silently read from the workspace.

Do not change catalog validation to consult graph nodes: contracts load independently of a workflow schema. The four static mechanical checks paths are a multi-layer upper bound only. `narrow_claims()` must remain the runtime boundary that replaces them with the exact node-declared write/output; tests must fail if a final planned task retains authorization for another layer's checks.

- [ ] **Step 5: Keep the graph and profile consumers inactive**

Do not change `workflow-schema.yaml`, Fuzz/Performance profile review models, artifact registry resolution, or the mechanical handler in this task. The extra read authorization is safe preparation; Task 13 switches prompts and graph consumers in one activation commit.

- [ ] **Step 6: Run tests, import contracts, and commit**

```bash
uv run pytest -q \
  tests/unit/workflow/graph/test_contracts.py \
  tests/unit/workflow/graph/test_read_isolation.py \
  tests/unit/verification/test_applicability.py \
  tests/integration/test_api_e2e_assurance_flow.py
uv run lint-imports
uv run pyright
git add assurance_agent/_resources/schemas/execution-contracts.yaml tests/unit/workflow/graph/test_contracts.py tests/unit/workflow/graph/test_read_isolation.py tests/unit/verification/test_applicability.py
git diff --cached --check
git commit -m "feat(contracts): narrow fuzz and performance assurance ownership"
```

---

### Task 4: Define Manual-Revision Schema and Split Current Activation from Historical Classification

**Files:**
- Modify: `assurance_agent/workflow/graph/schema_v2.py` — `ManualRevisionDef`, `InterruptDef` validation.
- Modify: `assurance_agent/workflow/graph/replay_schema.py` — topology specification, current validator, historical classifier, codegen AST guard.
- Modify: `tests/unit/workflow/graph/test_schema_v2.py`
- Modify: `tests/unit/workflow/graph/test_replay_schema.py`
- Modify: `tests/unit/workflow/graph/test_packaged_schema_compiles.py`

**Interfaces:**
- Produces: a schema-owned exact revision allowlist attached to an audited interrupt.
- Produces: `validate_current_assurance_activation(schema)` and `classify_pinned_layer_topology(schema, topology_spec)` as separate authorities.
- Produces: parsed-AST validation for hard Fuzz/Performance codegen predicates.
- Does not yet connect the new all-four-layer validator to `compile_workflow`; Task 13 changes the YAML, current release gate, skills, registry/profile, and v5 emission atomically after runtime and replay support exist.

- [ ] **Step 1: Write failing schema tests for exact manual revision paths**

Add this frozen model contract:

```python
class ManualRevisionDef(_FrozenModel):
    action: Literal["fix_and_proceed"]
    paths: list[str] = Field(min_length=1)


class InterruptDef(_FrozenModel):
    reason: str
    checkpoint: str
    bind: Literal["audited_gate_read"]
    actions: list[str] = Field(min_length=1)
    manual_revision: ManualRevisionDef | None = None
```

Tests must reject duplicate paths, globs, templates, directories, non-`change:` paths, paths outside `change:plans/`, and a manual-revision action absent from `interrupt.actions`. This is valid:

```yaml
interrupt:
  reason: fuzz plan requires a manual revision
  checkpoint: fuzz-plan-gate
  bind: audited_gate_read
  actions: [fix_and_proceed, accept_risk, stop]
  manual_revision:
    action: fix_and_proceed
    paths:
      - change:plans/fuzz-plan.md
      - change:plans/fuzz-codegen-plan.md
```

- [ ] **Step 2: Add synthetic topology tests for all three classifications**

Define the shared immutable input/output records:

```python
WiringStatus = Literal["wired", "legacy_unwired", "partial"]


@dataclass(frozen=True, slots=True)
class LayerTopologySpec:
    layer: str
    plan_artifacts: tuple[str, ...]
    review_artifact: str
    review_alias: str
    checks_artifact: str
    gate_id: str


@dataclass(frozen=True, slots=True)
class PinnedLayerTopology:
    layer: str
    status: WiringStatus
    assurance_node_id: str | None
    branch_graph_id: str | None
    cycle_call_node_id: str | None
    cycle_graph_id: str | None
    applicability_node_id: str | None
    reviewer_node_id: str | None
    mechanical_node_id: str | None
    gate_node_id: str | None
    human_review_node_id: str | None
    knowledge_remediation_node_id: str | None
    codegen_precondition_node_id: str | None
    codegen_node_id: str | None
    diagnostics: tuple[str, ...]
```

Build synthetic pinned graphs and assert:

```python
assert classify_pinned_layer_topology(legacy_fuzz_schema, fuzz_spec).status == "legacy_unwired"
assert classify_pinned_layer_topology(wired_fuzz_schema, fuzz_spec).status == "wired"
assert classify_pinned_layer_topology(partial_fuzz_schema, fuzz_spec).status == "partial"
```

Exactly zero applicability/mechanical/explicit-gate activation markers is legacy. The presence of any marker makes every missing or malformed requirement partial.

- [ ] **Step 3: Add codegen-precondition AST mutations**

For Fuzz and Performance, mutate each required predicate independently: delete it, negate it, change an operand, or place it below a permissive `or`. Every mutation must yield a topology diagnostic. Required normalized predicates are:

```text
skip: plan_assurance_state(<checks-alias>, <review-alias>, data_knowledge, '<layer>') == 'not_applicable'
stop: node('<cycle>').status != 'succeeded'
stop: plan_assurance_state(<checks-alias>, <review-alias>, data_knowledge, '<layer>') == 'invalid'
stop: not file_exists('repo:.aa/data-knowledge.yaml')
pass: node('<cycle>').status == 'succeeded'
pass: plan_assurance_state(<checks-alias>, <review-alias>, data_knowledge, '<layer>') == 'applicable'
pass: gate('<profile-gate>').verdict == 'pass'
pass: capabilities_present(<review-alias>, data_knowledge)
pass: file_exists('repo:.aa/data-knowledge.yaml')
```

The pass expression must expose all five predicates as top-level `and` conjuncts. Extra top-level conjuncts may narrow it. Flatten the stop expression as top-level `or`; do not accept logically unrelated text matching.

Also mutate both halves of knowledge remediation independently: remove or retarget `review-gate.knowledge_remediation -> knowledge-remediation`, then remove or retarget `knowledge-remediation.fix_and_proceed -> mechanical-plan-checks`. Either mutation is partial wiring.

- [ ] **Step 4: Run the new tests and observe missing models/classifier failures**

```bash
uv run pytest -q \
  tests/unit/workflow/graph/test_schema_v2.py \
  tests/unit/workflow/graph/test_replay_schema.py \
  tests/unit/workflow/graph/test_compiler.py \
  tests/unit/workflow/graph/test_packaged_schema_compiles.py
```

- [ ] **Step 5: Implement pure structural discovery and validation**

The classifier must follow pinned `uses: graph:<graph-id>` references rather than construct names. Require exactly one assurance node, branch, cycle-call node, and cycle binding; applicability; one reviewer; one `require_review: true` mechanical producer; a separate explicit gate owner; mechanical on both applicable and inapplicable paths; review before mechanical on the applicable path; the gate's `knowledge_remediation` route into the knowledge interrupt and that interrupt's `fix_and_proceed` route back to mechanical; the discovered codegen-precondition/codegen nodes; and either the existing automatic-fixer shape or the human-only shape.

For the human-only shape, require:

```python
interrupt.manual_revision.action == "fix_and_proceed"
tuple(interrupt.manual_revision.paths) == tuple(
    f"change:{path}" for path in topology_spec.plan_artifacts
)
```

Fuzz/Performance additionally require their cases-only parent preflight. API/E2E continue to validate without that parent guard.

- [ ] **Step 6: Separate validation entry points without weakening the release gate**

Export:

```python
def validate_current_assurance_activation(
    schema: WorkflowSchemaV2,
) -> tuple[str, ...]:
    """Require every current registry profile to classify as fully wired."""


def validate_historical_replay_surface(
    schema: WorkflowSchemaV2,
) -> tuple[str, ...]:
    """Validate syntax, graph integrity, and replay-safe dependencies only."""
```

Leave the existing packaged compiler call site unchanged in this task. Add direct tests for `validate_current_assurance_activation`: it must report the still-unwired packaged Fuzz/Performance graphs, while the existing compatibility validator keeps the intermediate commit green. Task 13 updates the YAML and switches the compiler to the new validator in one commit. Task 10 later adds a separate historical compile entry point; do not add a permissive boolean flag.

- [ ] **Step 7: Run tests and commit structural contracts**

```bash
uv run pytest -q \
  tests/unit/workflow/graph/test_schema_v2.py \
  tests/unit/workflow/graph/test_replay_schema.py \
  tests/unit/workflow/graph/test_compiler.py \
  tests/unit/workflow/graph/test_packaged_schema_compiles.py
uv run ruff check assurance_agent/workflow/graph/schema_v2.py assurance_agent/workflow/graph/replay_schema.py tests/unit/workflow/graph/test_schema_v2.py tests/unit/workflow/graph/test_replay_schema.py tests/unit/workflow/graph/test_packaged_schema_compiles.py
uv run pyright
git add assurance_agent/workflow/graph/schema_v2.py assurance_agent/workflow/graph/replay_schema.py tests/unit/workflow/graph/test_schema_v2.py tests/unit/workflow/graph/test_replay_schema.py tests/unit/workflow/graph/test_packaged_schema_compiles.py
git diff --cached --check
git commit -m "feat(graph): define assurance topology contracts"
```

---

### Task 5: Add Exact Immutable Tree Replacement and Durable Revision Views

**Files:**
- Modify: `assurance_agent/workflow/graph/workspace.py` — exact file replacement primitive.
- Create: `assurance_agent/workflow/graph/manual_revision.py` — revision view and candidate models/helpers.
- Modify: `tests/unit/workflow/graph/test_workspace.py`
- Create: `tests/unit/workflow/graph/test_manual_revision.py`

**Interfaces:**
- Produces: `TreeStore.replace_tree_files(base_tree_id, replacements)` without capturing unrelated live drift.
- Produces: create-once `.graph-runtime/revision-views/<interrupt-id>/` transport containing only exact allowlisted plan files.
- Rejects: an all-path no-op candidate, additions, removals, symlinks, non-files, and paths outside the exact allowlist; a candidate that changes one declared plan while leaving another declared plan byte-identical is valid.

- [ ] **Step 1: Write failing TreeStore replacement tests**

Use this result shape:

```python
@dataclass(frozen=True, slots=True)
class TreePathRevision:
    logical_path: str
    before_sha256: str
    after_sha256: str


@dataclass(frozen=True, slots=True)
class TreeFileRevision:
    target_tree_id: str
    paths: tuple[TreePathRevision, ...]
```

Test this public method:

```python
revision = store.replace_tree_files(
    base_tree_id,
    {"change:plans/fuzz-plan.md": b"# revised\n"},
)
assert revision.target_tree_id != base_tree_id
assert store.read_bytes(
    revision.target_tree_id, "change:plans/fuzz-plan.md"
) == b"# revised\n"
assert store.read_bytes(
    revision.target_tree_id, "change:review/fuzz-plan-review.json"
) == store.read_bytes(
    base_tree_id, "change:review/fuzz-plan-review.json"
)
```

Mutations for a missing path, symlink manifest entry, an all-path same-bytes mapping, and duplicate canonical path must raise a stable workspace error. Add a two-path case in which one replacement is unchanged and one changes: it must produce a new target tree, retain both paths in canonical order in `TreeFileRevision.paths`, and record equal before/after digests for the unchanged path. The generic TreeStore primitive may replace any exact existing regular logical file; the schema and manual-revision capture layer separately enforce `change:plans/` scope.

- [ ] **Step 2: Write failing bounded-view tests**

Define:

```python
@dataclass(frozen=True, slots=True)
class RevisionPathBaseline:
    logical_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class RevisionViewBinding:
    interrupt_id: str
    owner_invocation_id: str
    base_tree_id: str
    view_relpath: str
    logical_paths: tuple[str, ...]
    baseline: tuple[RevisionPathBaseline, ...]
```

Test the `materialize_revision_view` and `capture_revision_candidate` helpers for:

- exact files only under `.graph-runtime/revision-views/<interrupt-id>/`;
- an orphan view being recreated from the leaf base tree;
- an existing view for a committed unresolved interrupt preserving user edits;
- missing files, extra files/directories, symlinks at any path component, and non-files failing before tree creation;
- a deterministic race mutation that replaces an already-inventoried regular file with a symlink before content open; capture must fail without reading the symlink target;
- mixed unchanged/changed allowlisted files succeeding, while a wholly byte-identical view raises `manual_plan_revision_noop` and leaves the base tree unreferenced by any revision event;

- [ ] **Step 3: Run the focused tests and observe missing interfaces**

```bash
uv run pytest -q \
  tests/unit/workflow/graph/test_workspace.py \
  tests/unit/workflow/graph/test_manual_revision.py
```

- [ ] **Step 4: Implement content-addressed exact replacement**

Implement:

```python
def replace_tree_files(
    self,
    base_tree_id: str,
    replacements: Mapping[str, bytes],
) -> TreeFileRevision:
    """Return a new tree by replacing existing regular files only."""
```

Read the base manifest directly, validate each exact entry, publish replacement blobs only for changed bytes, and publish one new immutable manifest when at least one path changed. Preserve every allowlisted path in the returned before/after lineage, including unchanged paths; reject only when all replacements are byte-identical and the resulting target would equal the base. Never call `capture(project_root)` and never scan active workspace paths.

- [ ] **Step 5: Implement bounded view materialization and capture**

Expose from `manual_revision.py`:

```python
def materialize_revision_view(
    *,
    change_dir: Path,
    store: TreeStore,
    interrupt_id: str,
    owner_invocation_id: str,
    base_tree_id: str,
    logical_paths: tuple[str, ...],
    committed_binding: RevisionViewBinding | None,
) -> RevisionViewBinding:
    """Materialize or validate the exact bounded transport view."""
    return _write_or_validate_revision_view(
        change_dir=change_dir,
        store=store,
        interrupt_id=interrupt_id,
        owner_invocation_id=owner_invocation_id,
        base_tree_id=base_tree_id,
        logical_paths=logical_paths,
        committed_binding=committed_binding,
    )


def capture_revision_candidate(
    *,
    change_dir: Path,
    store: TreeStore,
    binding: RevisionViewBinding,
) -> TreeFileRevision:
    """Validate the complete view inventory and publish exact replacements."""
    replacements = _read_validated_revision_files(change_dir, binding)
    return store.replace_tree_files(binding.base_tree_id, replacements)
```

Do not use a path-based `lstat()`-then-`read_bytes()` sequence. Open the revision-view root once, traverse every directory component relative to that descriptor with `os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)`, open each leaf with `os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)`, verify the opened descriptor is a regular file with `fstat()`, and read from that same descriptor. Compare the complete relative inventory with the exact allowlist before and after the descriptor-bound reads. The race test must swap a file after inventory and prove the leaf open fails closed rather than following it outside the view. When `committed_binding is None`, always rebuild any orphan directory from the base tree. When it is present, validate interrupt/owner/base/path/baseline metadata and preserve the existing edits. Keep the view as transport only; the returned immutable target tree is the accepted artifact.

- [ ] **Step 6: Run tests, type checks, and commit**

```bash
uv run pytest -q \
  tests/unit/workflow/graph/test_workspace.py \
  tests/unit/workflow/graph/test_manual_revision.py
uv run ruff check assurance_agent/workflow/graph/workspace.py assurance_agent/workflow/graph/manual_revision.py tests/unit/workflow/graph/test_workspace.py tests/unit/workflow/graph/test_manual_revision.py
uv run pyright
git add assurance_agent/workflow/graph/workspace.py assurance_agent/workflow/graph/manual_revision.py tests/unit/workflow/graph/test_workspace.py tests/unit/workflow/graph/test_manual_revision.py
git diff --cached --check
git commit -m "feat(runtime): add bounded manual revision views"
```

---

### Task 6: Introduce Event Schema v5, Leaf Revision Folding, and Profile Snapshots

**Files:**
- Modify: `assurance_agent/workflow/core/graph_events.py`
- Modify: `assurance_agent/workflow/core/migrate_events.py`
- Modify: `assurance_agent/workflow/graph/models.py`
- Modify: `assurance_agent/workflow/graph/checkpoint.py`
- Modify: `assurance_agent/workflow/graph/resume_wire.py`
- Modify: `assurance_agent/workflow/graph/runtime.py`
- Modify: `assurance_agent/verification/profile_manifest.py`
- Modify: `assurance_agent/workflow/graph/definition_pinning.py`
- Modify: `tests/helpers_graph_v3.py`
- Modify: `tests/unit/test_events.py`
- Modify: `tests/unit/workflow/graph/test_checkpoint.py`
- Modify: `tests/unit/workflow/graph/test_resume_v3.py`
- Modify: `tests/unit/workflow/graph/test_subgraph_interrupt.py`
- Modify: `tests/unit/workflow/graph/test_policy_digest_event.py`
- Modify: `tests/unit/workflow/graph/test_policy_snapshot_runtime.py`
- Modify: `tests/unit/verification/test_profile_manifest.py`

**Interfaces:**
- Produces: version-5 `manual_plan_revision` and revision-aware resume/interrupted events.
- Produces: typed, canonical assurance-profile snapshot parsing and immutable snapshot paths.
- Enforces: only the leaf fold advances to the revision target tree; ancestors remain unchanged until normal child propagation.

- [ ] **Step 1: Add failing event validation tests**

Define the revision event with all lineage fields:

```python
class ManualPlanRevisionEvent(_GraphEvent):
    type: Literal["manual_plan_revision"] = "manual_plan_revision"
    invocation_id: str
    checkpoint_ns: str
    revision_transition_id: str
    interrupt_id: str
    action: Literal["fix_and_proceed"]
    who: str
    reason: str
    audited_reads_sha256: dict[str, str]
    source_gate_attempt_id: str
    source_gate_tree_id: str
    base_tree_id: str
    target_tree_id: str
    logical_paths: list[str]
    before_sha256: dict[str, str]
    after_sha256: dict[str, str]
    resume_anchors: list[ResumeAnchor]
```

Extend `GraphInterruptedEvent`/`InterruptProjection` with the exact optional v4-compatible fields `revision_owner_invocation_id`, `revision_base_tree_id`, `revision_view`, `revision_paths`, `revision_before_sha256`, `source_gate_attempt_id`, and `source_gate_tree_id`. Extend `GraphResumedEvent` with optional `revision_transition_id`, zero-based `revision_ordinal`, `revision_chain_length`, `source_gate_attempt_id`, and `source_gate_tree_id`; enforce the revision triple all-or-none, the source pair all-or-none, and valid ordinal bounds. A v5 fold requires the resume source pair to equal the unresolved interrupt's recorded pair whenever that interrupt resolved a real committed gate epoch, including `accept_risk` and `stop`. A v5 interrupt with no resolvable gate attempt records no pair, its resume must remain pairless, and it can never create a gate-decision override. v4 retains its legacy optional shape.

- [ ] **Step 2: Add failing fold tests for leaf-only ownership**

Build a root → branch → cycle event stream. Assert:

```python
leaf = fold_invocation_events(leaf_id, events_with_manual_revision)
branch = fold_invocation_events(branch_id, events_with_manual_revision)
root = fold_invocation_events(root_id, events_with_manual_revision)

assert leaf.current_tree_id == target_tree_id
assert branch.current_tree_id == branch_base_tree_id
assert root.current_tree_id == root_base_tree_id
```

Reject a missing unresolved interrupt, wrong owner, wrong base tree, equal target tree, path/digest mismatch, a leaf resume without the prior unconsumed transition, and a transition reused by a second leaf resume.
Also inject `manual_plan_revision` and revision-tagged `graph_resumed` events beneath an owning v4 root/parent epoch. Both must fail closed during fold/migration; the new event and revision triple are v5-only and may never upgrade a historical v4 projection implicitly.

- [ ] **Step 3: Add failing profile snapshot tests**

Introduce typed models in `profile_manifest.py` and require canonical bytes:

```python
class AssuranceProfileManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    layer: LayerName
    case_type: CaseType
    plan_artifacts: tuple[str, ...]
    review_artifact: str
    review_alias: str
    checks_artifact: str
    gate_id: str
    applicable_check_ids: tuple[PlanCheckId, ...]
    capability_contract_enabled: bool
    review_model: str


class AssuranceCheckManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    check_id: PlanCheckId
    callable: str


class AssuranceProfileManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"]
    layer_names: tuple[LayerName, ...]
    plan_check_ids: tuple[PlanCheckId, ...]
    profiles: tuple[AssuranceProfileManifestEntry, ...]
    check_catalog: tuple[AssuranceCheckManifestEntry, ...]


PROFILE_SNAPSHOT_DIRECTORY = ".graph-runtime/assurance-profiles"
```

Implement `parse_assurance_profile_snapshot(data: bytes) -> AssuranceProfileManifest` by validating JSON with the typed model, enforcing canonical layer/check order and uniqueness, and requiring `assurance_profile_bytes(parsed.model_dump(mode="json")) == data`. Implement `assurance_profile_snapshot_relpath(digest: str) -> str` as `f"{PROFILE_SNAPSHOT_DIRECTORY}/{digest}.json"` after validating a lowercase SHA-256 digest.

Test missing, malformed, non-canonical, and digest-mismatched snapshots. Identical create-once writes must be idempotent; different bytes at the same digest must fail.

- [ ] **Step 4: Run focused tests and observe the v4-only failures**

```bash
uv run pytest -q \
  tests/unit/test_events.py \
  tests/unit/workflow/graph/test_checkpoint.py \
  tests/unit/workflow/graph/test_resume_v3.py \
  tests/unit/workflow/graph/test_subgraph_interrupt.py \
  tests/unit/workflow/graph/test_policy_digest_event.py \
  tests/unit/workflow/graph/test_policy_snapshot_runtime.py \
  tests/unit/verification/test_profile_manifest.py
```

- [ ] **Step 5: Implement v5 models and pure fold invariants**

Add `ManualPlanRevisionEvent` to the event union/parser and permit migration through version 5 without fabricating v5 fields for old events. `fold_invocation_events()` must verify the owning root/parent event epoch is v5 before accepting either a manual event or revision-tagged resume, consume the manual event before the matching leaf `graph_resumed`, and reject these events under v4. A revision-tagged v5 ancestor resume validates the ordered chain but does not update the ancestor tree.

- [ ] **Step 6: Pin profile bytes before event references**

Extend the binding:

```python
@dataclass(frozen=True, slots=True)
class InvocationDefinitionBinding:
    event_schema_version: int
    policy_digest: str
    policy_origin: PolicyOrigin
    policy_bytes: bytes
    gate_semantics_digest: str
    assurance_profile_digest: str
    assurance_profile_bytes: bytes | None
```

Add `event_schema_version` as an explicit root-binding input: `bind_root_definitions(store=store, root_tree_id=root_tree_id, event_schema_version=4)` is the temporary default in this task, while `inherit_child_definitions()` always copies `parent.event_schema_version`. Make `_build_invocation_started()` emit `binding.event_schema_version` rather than a global constant. `stage_pinned_definitions()` can write `.graph-runtime/assurance-profiles/<digest>.json` in the same progression transaction before a v5 start event refers to it. Add direct v5 binding/inheritance tests: a v5 child requires the same verified snapshot, while a v4 parent whose first child starts after a code upgrade still produces a v4 child without a fabricated snapshot. Task 13 changes only fresh root binding to version 5 after runtime decision semantics and replay snapshot verification are installed.

- [ ] **Step 7: Run tests, type checks, and commit**

```bash
uv run pytest -q \
  tests/unit/test_events.py \
  tests/unit/workflow/graph/test_checkpoint.py \
  tests/unit/workflow/graph/test_resume_v3.py \
  tests/unit/workflow/graph/test_subgraph_interrupt.py \
  tests/unit/workflow/graph/test_policy_digest_event.py \
  tests/unit/workflow/graph/test_policy_snapshot_runtime.py \
  tests/unit/verification/test_profile_manifest.py
uv run pyright
git add assurance_agent/workflow/core/graph_events.py assurance_agent/workflow/core/migrate_events.py assurance_agent/workflow/graph/models.py assurance_agent/workflow/graph/checkpoint.py assurance_agent/workflow/graph/resume_wire.py assurance_agent/workflow/graph/runtime.py assurance_agent/verification/profile_manifest.py assurance_agent/workflow/graph/definition_pinning.py tests/helpers_graph_v3.py tests/unit/test_events.py tests/unit/workflow/graph/test_checkpoint.py tests/unit/workflow/graph/test_resume_v3.py tests/unit/workflow/graph/test_subgraph_interrupt.py tests/unit/workflow/graph/test_policy_digest_event.py tests/unit/workflow/graph/test_policy_snapshot_runtime.py tests/unit/verification/test_profile_manifest.py
git diff --cached --check
git commit -m "feat(events): add revision lineage and profile snapshot epoch"
```

---

### Task 7: Implement Crash-Recoverable Manual Resume and Decision Epochs

**Files:**
- Modify: `assurance_agent/workflow/graph/manual_revision.py`
- Modify: `assurance_agent/workflow/core/progression.py`
- Modify: `assurance_agent/workflow/graph/runtime.py`
- Modify: `assurance_agent/workflow/graph/resume_wire.py`
- Modify: `assurance_agent/workflow/graph/task_runner.py`
- Modify: `assurance_agent/workflow/graph/handlers/interrupt.py`
- Modify: `assurance_agent/workflow/graph/status.py`
- Modify: `assurance_agent/commands/status_cmd.py`
- Modify: `assurance_agent/workflow/orchestration/gates.py`
- Modify: `assurance_agent/workflow/orchestration/gate_semantics.py`
- Modify: `assurance_agent/workflow/graph/handlers/gate.py`
- Modify: `tests/unit/workflow/graph/test_manual_revision.py`
- Modify: `tests/unit/workflow/graph/test_task_runner.py`
- Modify: `tests/unit/workflow/graph/test_subgraph_interrupt.py`
- Modify: `tests/unit/workflow/graph/test_resume_v3.py`
- Modify: `tests/unit/workflow/graph/test_status_read.py`
- Modify: `tests/unit/workflow/graph/test_gate_reference_resolution.py`
- Modify: `tests/unit/workflow/orchestration/test_gate_semantics_manifest.py`
- Modify: `tests/integration/test_graph_interrupt_v3.py`
- Modify: `tests/integration/test_cli_status.py`
- Modify: `tests/integration/test_cli_workflow_v2.py`

**Interfaces:**
- Produces: deterministic `ManualRevisionTransition` and strict legal-prefix repair.
- Produces: `GraphStatus.recovery_state == "revision_resume_recovery_pending"`; the durable view path remains on its owning pending interrupt.
- Enforces: v5 decision overrides match both the source gate attempt and source committed tree.

- [ ] **Step 1: Add deterministic transition and prefix mutation tests**

Use this pure aggregate:

```python
@dataclass(frozen=True, slots=True)
class ManualRevisionTransition:
    revision: ManualPlanRevisionEvent
    resumes: tuple[GraphResumedEvent, ...]

def validate_resume_prefix(
    events: Sequence[dict[str, object]],
    transition: ManualRevisionTransition,
) -> int:
    """Return the first missing resume ordinal or raise a prefix conflict."""
```

`build_manual_revision_transition` takes the committed `GraphInterruptedEvent`, `ResumeCommand`, `TreeFileRevision`, pinned definition digests, and ordered `ResumeAnchor` tuple, and returns the aggregate above. It must not accept a filesystem path or mutable view object, which keeps transition identity independent of later edits.

Assert identical committed inputs produce the same `revision_transition_id`. Validate all legal prefixes from no event through the complete root-to-leaf chain. Reject a gap, reordering, duplicate ordinal, different transition ID, changed anchor, wrong parent anchor, and changed chain length with `manual_plan_revision_prefix_conflict`.

- [ ] **Step 2: Add runtime tests for action-specific ingestion**

Assert `fix_and_proceed` captures the exact view and rejects a byte-identical candidate as `manual_plan_revision_noop`, leaving the interrupt unresolved. Assert `accept_risk` and `stop` use the old audited path and ignore all view edits. A non-identical retry after a committed manual event must be an integrity conflict; an identical retry repairs only the missing resume suffix.

Add a `GateEvidenceEpoch` resolver test that starts from strict committed events and returns the interrupt checkpoint's exact successful gate attempt ID and committed target tree ID. Resolve both direct gate IDs and the existing approved checkpoint aliases, including `healing.safety -> fixer-safety-gate`; do not create a second alias table. The leaf `graph_interrupted` event must record that pair, and every bubbled parent event must preserve the leaf owner, revision metadata, and source pair byte-for-byte. Also exercise the packaged improvement-review and issue-review interrupts, whose checkpoint names do not resolve directly or through an alias to an actual gate attempt: their v5 interrupt/resume events stay pairless and never enter the gate-decision override store.

- [ ] **Step 3: Add status and decision-epoch tests**

Add:

```python
class GraphStatus(BaseModel):
    # existing lifecycle status remains unchanged
    recovery_state: Literal["revision_resume_recovery_pending"] | None = None
```

Derive `recovery_state` from the strict global event stream, not only the root projection: ordinal zero may already have resolved the root interrupt while branch/leaf suffix events are still missing. Pass the derived state into `graph_status_from_projection`; keep each view path on its owning `InterruptProjection` so multiple interrupts remain unambiguous. Test that an open revision event with an incomplete resume chain exposes the recovery state, and an unresolved human interrupt exposes its view path. For v5, a gate-backed decision whose `source_gate_attempt_id` or `source_gate_tree_id` differs from the current gate evaluation must not apply even when audited bytes are identical. A pairless non-gate decision remains a normal resume action but is ineligible as a gate override. Keep v4 hash-only compatibility.

Extend the gate-semantics manifest with `_latest_graph_gate_decision`, `_apply_gate_decision`, the shared checkpoint-to-gate resolver/alias constant, every pure epoch-matching helper they call, and the strong-review `_PLAN_REVIEW_TYPES`/`_HUMAN_ONLY_PLAN_REVIEW_TYPES` constants. AST-mutate each function and value-mutate each constant/alias table in `test_gate_semantics_manifest.py`; every mutation must change the digest. This prevents a runtime upgrade from reinterpreting pinned reviews or human decisions under an unchanged definition identity.

- [ ] **Step 4: Run focused tests and observe missing protocol behavior**

```bash
uv run pytest -q \
  tests/unit/workflow/graph/test_manual_revision.py \
  tests/unit/workflow/graph/test_task_runner.py \
  tests/unit/workflow/graph/test_subgraph_interrupt.py \
  tests/unit/workflow/graph/test_resume_v3.py \
  tests/unit/workflow/graph/test_status_read.py \
  tests/unit/workflow/graph/test_gate_reference_resolution.py \
  tests/unit/workflow/orchestration/test_gate_semantics_manifest.py \
  tests/integration/test_graph_interrupt_v3.py \
  tests/integration/test_cli_status.py \
  tests/integration/test_cli_workflow_v2.py
```

- [ ] **Step 5: Implement transition construction from committed inputs**

Canonicalize and hash exactly: pinned invocation definition digests, interrupt/action, who/reason, audited reads, source gate attempt/tree, base/target trees, ordered paths and before/after digests, and the ordered root-to-leaf resume anchors. Every resume event carries the same ID plus ordinal and chain length.

Expose:

```python
def stage_missing_resume_suffix(
    *,
    txn: ProgressionTxn,
    transition: ManualRevisionTransition,
) -> int:
    """Validate the committed prefix and append only its missing suffix."""
```

Read strict ledger events while holding the progression lock. Do not use a lenient event reader at this boundary.

Add `ProgressionTxn.read_events_strict() -> list[dict[str, object]]`; it must use the already-held progression lock and the strict ledger parser. Both initial commit and suffix repair call this interface.

- [ ] **Step 6: Connect recovery before ordinary repair and planning**

At the start of drive and resume, before materialization repair or planner selection:

1. open the progression transaction;
2. scan for an open manual-revision transition;
3. reconstruct it only from committed event fields;
4. validate the exact existing resume prefix;
5. append the missing suffix; and
6. re-project before continuing.

For a new `fix_and_proceed`, publish target objects, append `manual_plan_revision`, then append the ordered resume chain. Do not describe this sequence as transactionally power-loss atomic.

Construct `InterruptHandler(compiled, object_store)` in `build_default_node_runner`. For a schema interrupt with `manual_revision`, the leaf handler resolves `GateEvidenceEpoch`, materializes the revision view before `TaskWorkspace` cleanup, and returns the exact metadata consumed by `build_graph_interrupted_event`. For any other interrupt, resolve the epoch only when its checkpoint maps directly or through the existing checkpoint-alias resolver to a real compiled gate with committed evidence; no-gate checkpoints remain pairless. Pass `committed_binding=None` for an orphan/uncommitted view and the recorded binding for an already committed unresolved interrupt.

- [ ] **Step 7: Bind decision overrides to source gate epoch**

Pass the current committed tree ID into gate evaluation. For v5, `_latest_graph_gate_decision()` and `_apply_gate_decision()` require matching source gate attempt and tree. Every v5 resume of a gate-backed interrupt, including Fuzz/Performance and the aliased `healing.safety` interrupt, records that pair; resumes for checkpoints without a directly or alias-resolved gate attempt record neither field and cannot be applied as gate decisions. A manual revision advances the leaf epoch, invalidating the old override; the regenerated reviewer/mechanical/gate sequence must create a new verdict or interrupt. Keep fresh-root emission at v4 until the atomic activation in Task 13; these tests seed explicit v5 starts to exercise the prepared reader/runtime paths.

- [ ] **Step 8: Run tests, type checks, and commit**

```bash
uv run pytest -q \
  tests/unit/workflow/graph/test_manual_revision.py \
  tests/unit/workflow/graph/test_task_runner.py \
  tests/unit/workflow/graph/test_subgraph_interrupt.py \
  tests/unit/workflow/graph/test_resume_v3.py \
  tests/unit/workflow/graph/test_status_read.py \
  tests/unit/workflow/graph/test_gate_reference_resolution.py \
  tests/unit/workflow/orchestration/test_gate_semantics_manifest.py \
  tests/integration/test_graph_interrupt_v3.py \
  tests/integration/test_cli_status.py \
  tests/integration/test_cli_workflow_v2.py
uv run pyright
git add assurance_agent/workflow/graph/manual_revision.py assurance_agent/workflow/core/progression.py assurance_agent/workflow/graph/runtime.py assurance_agent/workflow/graph/resume_wire.py assurance_agent/workflow/graph/task_runner.py assurance_agent/workflow/graph/handlers/interrupt.py assurance_agent/workflow/graph/status.py assurance_agent/commands/status_cmd.py assurance_agent/workflow/orchestration/gates.py assurance_agent/workflow/orchestration/gate_semantics.py assurance_agent/workflow/graph/handlers/gate.py tests/unit/workflow/graph/test_manual_revision.py tests/unit/workflow/graph/test_task_runner.py tests/unit/workflow/graph/test_subgraph_interrupt.py tests/unit/workflow/graph/test_resume_v3.py tests/unit/workflow/graph/test_status_read.py tests/unit/workflow/graph/test_gate_reference_resolution.py tests/unit/workflow/orchestration/test_gate_semantics_manifest.py tests/integration/test_graph_interrupt_v3.py tests/integration/test_cli_status.py tests/integration/test_cli_workflow_v2.py
git diff --cached --check
git commit -m "feat(runtime): recover manual revision resume prefixes"
```

---

### Task 8: Define Canonical Gate Fixtures Without Publishing the Graph

**Files:**
- Create: `tests/fixtures/assurance/fuzz-performance-gates.yaml`
- Modify: `tests/unit/workflow/orchestration/test_plan_check_gate.py`
- Modify: `tests/unit/test_dsl_schema_corpus.py`

**Interfaces:**
- Produces: canonical fail-closed plan-gate and codegen-precondition definitions as test fixtures consumed verbatim by Task 13 activation checks.
- Preserves: the published workflow schema, its currently referenced policy fields, and current compiler behavior until Task 13 can remove the old policy expression and register its deferred obligation atomically.

- [ ] **Step 1: Write failing canonical gate-fixture tests**

Load `tests/fixtures/assurance/fuzz-performance-gates.yaml` through the real gate normalizer. Assert it defines exactly the Fuzz/Performance plan gates and codegen preconditions, uses profile-canonical aliases, and reads exact review/checks/L1 paths. Task 13 later asserts the packaged definitions have identical canonical dumps before activation can pass.

- [ ] **Step 2: Add complete plan-gate truth tables**

Parameterize both layers across:

- invalid assurance state → `stop`;
- valid inapplicable state → `skip`;
- `needs_fix` with `auto_fix_allowed=False` → `needs_fix`;
- missing capability → `needs_human_review`/knowledge remediation;
- `reject` plus high risk → `reject`, never human review;
- `changes_requested`, explicit human review, configured risk, and `require_human` failed checks → `needs_human_review`;
- `not_ready`, explicit reject, and `block` failed checks → `reject`;
- `pass`, ready readiness, present capabilities, and no blocking condition → `pass`;
- `force_continue` combinations preserve the approved existing policy semantics but never override explicit reject or missing capability;
- missing capability routes to knowledge remediation under each of `warn`, `block`, and `require_human` plan-check action sets;
- `approved` → not pass;
- malformed review/checks/L1 → `stop`;
- an unmatched valid-looking combination reaches the default `stop`.

Assert `assert_ideal` never triggers a Fuzz/Performance action because it is N/A, while the other three applicable checks honor `warn`, `block`, and `require_human`.

- [ ] **Step 3: Add fixture-only DSL and policy-reference guards**

Parse the fixture's codegen skip/stop/pass expressions with the real DSL AST helpers. Assert every hard predicate from Task 4 is present without a permissive disjunction. Assert the fixture contains neither `policy.fuzz.required_when_endpoint_has_auth` nor `layer_applicable`. Do not yet assert disjointness against the packaged workflow: that workflow still contains the old policy reference until Task 13's atomic activation.

- [ ] **Step 4: Run the new fixture tests and observe missing definitions**

```bash
uv run pytest -q \
  tests/unit/workflow/orchestration/test_plan_check_gate.py \
  tests/unit/test_dsl_schema_corpus.py
```

- [ ] **Step 5: Write the four canonical gate definitions**

Write both plan gates and both codegen preconditions to the fixture. The Fuzz plan gate includes exact reads:

```yaml
fuzz-plan-review-gate:
  reads:
    - {path: review/fuzz-plan-review.json, as: fuzz_plan_review}
    - {path: review/fuzz-plan-checks.json, as: fuzz_plan_checks}
    - {path: repo:.aa/data-knowledge.yaml, as: data_knowledge}
  invalid_json: stop
  missing_field_is: stop
```

Use the exact Performance equivalents. Each plan gate encodes the spec's first-true precedence; `needs_fix_when` depends only on decision, every ordinary human branch excludes `decision == 'reject'`, and successful review is exactly `pass`. Do not edit `workflow-schema.yaml` in this task.

- [ ] **Step 6: Encode hard codegen-precondition expressions**

Use this exact normalized Fuzz pass expression:

```yaml
pass_when: >
  node('review-cycle').status == 'succeeded'
  and plan_assurance_state(fuzz_plan_checks, fuzz_plan_review, data_knowledge, 'fuzz') == 'applicable'
  and gate('fuzz-plan-review-gate').verdict == 'pass'
  and capabilities_present(fuzz_plan_review, data_knowledge)
  and file_exists('repo:.aa/data-knowledge.yaml')
```

Use exact skip and stop predicates from Task 4 and the equivalent Performance aliases. The gate reads all three artifacts; no prior on-disk pass may replace the cycle producers.

- [ ] **Step 7: Run fixture truth tables and commit**

```bash
uv run pytest -q \
  tests/unit/workflow/orchestration/test_plan_check_gate.py \
  tests/unit/test_dsl_schema_corpus.py
uv run pyright
git add tests/fixtures/assurance/fuzz-performance-gates.yaml tests/unit/workflow/orchestration/test_plan_check_gate.py tests/unit/test_dsl_schema_corpus.py
git diff --cached --check
git commit -m "test(policy): define fuzz performance gate fixtures"
```

---

### Task 9: Prove v5 Manual-Revision Power-Loss Recovery on a Synthetic Graph

**Files:**
- Modify: `tests/integration/_graph_fault_worker.py`
- Modify: `tests/integration/test_graph_runtime_faults.py`
- Modify: `tests/integration/test_cli_workflow_v2.py`

**Interfaces:**
- Exercises: an explicit v5 synthetic root → branch → leaf graph, content-addressed trees, nested interrupts, and deterministic handlers without publishing Fuzz/Performance.
- Proves: every durable revision/resume prefix is repaired without duplicate evidence.

- [ ] **Step 1: Write a synthetic nested v5 revision flow**

Use the real `GraphRuntime` signature and an explicit v5 started-event fixture:

```python
def test_v5_manual_revision_resumes_root_to_leaf(tmp_path: Path) -> None:
    runtime, compiled, context, root_id = prepare_interrupted_v5_graph(tmp_path)
    edit_recorded_revision_view(root_id, b"# revised plan\n")
    result = runtime.resume(root_id, fix_and_proceed_command(root_id))
    assert result.status.status == "completed"
    assert_revision_resume_chain(root_id, expected_ordinals=(0, 1, 2))
```

The helper may use `runtime.run(compiled, "root", context)` only to create v4 compatibility cases; the v5 path seeds the explicit version-5 root event and verified profile snapshot before calling `resume`.

- [ ] **Step 2: Write real nested manual-revision integration cases**

Interrupt root → layer branch → review cycle, edit the durable view, and resume. Assert:

- the view survives leaf `TaskWorkspace` cleanup;
- the manual event belongs to the cycle invocation;
- only the leaf tree changes at the event;
- the resume chain is root-to-branch-to-leaf with correct parent anchors;
- review, mechanical, and gate run again after the target tree;
- normal child write-set propagation later carries results into branch/root;
- no-op, extra file, missing file, symlink, and outside-path edits leave the interrupt unresolved;
- `accept_risk` and `stop` do not ingest the view;
- a source decision cannot cross the revised tree epoch.

- [ ] **Step 3: Extend the subprocess fault harness**

Add deterministic worker fault points after:

1. target object publication;
2. `manual_plan_revision` append; and
3. each root-to-leaf `graph_resumed` ordinal append.

Use the existing real `SIGKILL` harness rather than monkeypatch exceptions. For every legal prefix, restart the CLI/runtime and assert it appends only the missing suffix. Then inject a gap, reordering, duplicate ordinal, or altered payload and assert `manual_plan_revision_prefix_conflict` before the planner runs.

- [ ] **Step 4: Run integration tests**

```bash
uv run pytest -q \
  tests/integration/test_graph_runtime_faults.py \
  tests/integration/test_cli_workflow_v2.py
uv run ruff check tests/integration/_graph_fault_worker.py tests/integration/test_graph_runtime_faults.py tests/integration/test_cli_workflow_v2.py
uv run pyright
```

Expected: all real runtime and power-loss cases pass.

If this task exposes a production defect, return to the owning Task 5–8 invariant, add the failing case to that task's focused suite, and commit the production correction there before recording the Task 9 acceptance commit.

- [ ] **Step 5: Commit runtime acceptance coverage**

```bash
git add tests/integration/_graph_fault_worker.py tests/integration/test_graph_runtime_faults.py tests/integration/test_cli_workflow_v2.py
git diff --cached --check
git commit -m "test(runtime): cover fuzz performance assurance recovery"
```

---

### Task 10: Reconstruct Historical Compilation Exclusively from Pinned Definitions

**Files:**
- Modify: `assurance_agent/workflow/graph/ingest_catalog.py`
- Modify: `assurance_agent/workflow/graph/contracts.py`
- Modify: `assurance_agent/workflow/graph/compiler.py`
- Modify: `assurance_agent/workflow/graph/definition_pinning.py`
- Modify: `assurance_agent/workflow/graph/replay_binding.py` — pinned definition loader only; evidence recovery lands in Task 11.
- Modify: `tests/unit/workflow/graph/test_ingest_catalog.py`
- Modify: `tests/unit/workflow/graph/test_contracts.py`
- Modify: `tests/unit/workflow/graph/test_compiler.py`
- Modify: `tests/unit/workflow/graph/test_replay_binding.py`

**Interfaces:**
- Produces: a separate `compile_historical_workflow()` entry point whose context contains the verified pinned ingest catalog, execution-contract catalog, and both recorded identities.
- Produces: typed pinned ingest/contract parsers that perform canonical integrity checks without consulting current model registries.
- Produces: one reusable `load_pinned_execution_definition()` seam used by replay now and by restarted runtime resolution in Task 13; replay must not own a private second copy.
- Makes current catalog loading unrepresentable from the replay definition-loading path.

- [ ] **Step 1: Add failing pinned-ingest and pinned-contract mutation tests**

Cover exact stable failures: `pinned_ingest_catalog_missing`, `pinned_ingest_catalog_invalid`, and `pinned_ingest_catalog_digest_mismatch`; `pinned_contract_snapshot_missing`, `pinned_contract_digest_mismatch`, and `pinned_contract_target_mismatch`; plus unrecorded referenced targets and extra conflicting bindings.

Add two authority tests:

```python
monkeypatch.setattr(ingest_catalog, "resolve_model", explode)
monkeypatch.setattr(ingest_catalog, "validate_catalog_runtime", explode)
binding = bind_replay_definitions(
    change_dir=change_dir,
    change_id="CH-PINNED-001",
    root_invocation_id=root_invocation_id,
    expected_entrypoint="assurance",
    store=store,
)
assert binding.compiled.ingest_catalog_digest == started.ingest_catalog_digest
```

Modify current project/packaged execution contracts after creating the pinned fixture and prove the historical compiled digest map remains unchanged.

- [ ] **Step 2: Add failing current-versus-historical compiler tests**

Define the explicit context:

```python
@dataclass(frozen=True, slots=True)
class HistoricalCompileContext:
    ingest_catalog: IngestArtifactCatalog
    ingest_catalog_digest: str
    contracts: ExecutionContractCatalog
    contract_digests: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class PinnedDefinitionRequest:
    graph_digest: str
    ingest_catalog_digest: str
    contract_digests: tuple[tuple[str, str], ...]
    event_schema_version: int
    gate_semantics_digest: str
    assurance_profile_digest: str


@dataclass(frozen=True, slots=True)
class ResolvedPinnedDefinition:
    compiled: CompiledWorkflow
    contracts: ExecutionContractCatalog
    ingest_catalog: IngestArtifactCatalog


def compile_historical_workflow(
    schema: WorkflowSchemaV2,
    *,
    context: HistoricalCompileContext,
) -> CompiledWorkflow:
    context.validate_identities()
    return _compile_with_catalog(
        schema,
        contracts=context.contracts,
        ingest_catalog=context.ingest_catalog,
        activation_errors=validate_historical_replay_surface(schema),
    )
```

`HistoricalCompileContext.validate_identities()` requires `self.ingest_catalog.digest == self.ingest_catalog_digest` and `canonical_digest(self.contracts.contracts[target]) == self.contract_digests[target]` for the exact target set. `_compile_with_catalog()` receives the catalog object and derives the compiled digest from it; it contains no call to `validate_catalog_runtime()`. Assert current `compile_workflow(schema)` remains the live-catalog/current path and historical compilation accepts core-valid legacy topology, calls `validate_historical_replay_surface`, and leaves per-layer classification to the caller. Keep the existing intermediate packaged-compatibility validator on the current path until Task 13 atomically activates the four-layer schema.

`PinnedDefinitionRequest` is constructed only from a committed invocation projection, with `contract_digests=tuple(sorted(projection.contract_digests.items()))`. The tuple makes the full request an immutable cache key. It carries all identities needed to select exact snapshots and to enforce live executable compatibility; a graph digest alone is insufficient because the same schema may have been invoked with different contract/catalog or semantics epochs. Historical replay may inspect mismatched semantic identities without executing them; after definition-independent manual-prefix repair, Task 13's live runtime resolver validates them before definition-dependent recovery, materialization, or planning.

- [ ] **Step 3: Run tests and observe current-catalog coupling**

```bash
uv run pytest -q \
  tests/unit/workflow/graph/test_ingest_catalog.py \
  tests/unit/workflow/graph/test_contracts.py \
  tests/unit/workflow/graph/test_compiler.py \
  tests/unit/workflow/graph/test_replay_binding.py
```

- [ ] **Step 4: Implement versioned snapshot parsing**

Add:

```python
def parse_ingest_catalog_snapshot(data: bytes) -> IngestArtifactCatalog:
    """Parse schema version 1 and canonical JSON bytes without resolving models."""


def catalog_from_pinned_contracts(
    contracts: Sequence[ExecutionContract],
) -> ExecutionContractCatalog:
    """Validate target uniqueness and resource path safety."""
```

Reuse the existing resource-path validators. Snapshot files are canonical model JSON plus one newline, while recorded identities are canonical model digests; validate typed parse → canonical file bytes equality → model canonical digest equality rather than hashing file bytes directly. Accept only ingest snapshot `schema_version == 1`. Do not recompute model schema digests or execute a historical ingest model.

- [ ] **Step 5: Load and verify every root-recorded snapshot**

Split pinned definition loading into three exact helpers: `_load_pinned_ingest_catalog(change_dir: Path, digest: str) -> IngestArtifactCatalog`, `_load_pinned_execution_contracts(change_dir: Path, recorded: Mapping[str, str]) -> ExecutionContractCatalog`, and `_load_pinned_schema(change_dir: Path, digest: str) -> WorkflowSchemaV2`. Put those helpers and the public composition seam in `definition_pinning.py` (or a new graph-level definition-loader module imported by both runtime and replay), not under a replay-only namespace:

```python
def load_pinned_execution_definition(
    change_dir: Path,
    request: PinnedDefinitionRequest,
) -> ResolvedPinnedDefinition:
    recorded_contracts = dict(request.contract_digests)
    schema = _load_pinned_schema(change_dir, request.graph_digest)
    ingest = _load_pinned_ingest_catalog(change_dir, request.ingest_catalog_digest)
    contracts = _load_pinned_execution_contracts(change_dir, recorded_contracts)
    compiled = compile_historical_workflow(
        schema,
        context=HistoricalCompileContext(
            ingest_catalog=ingest,
            ingest_catalog_digest=request.ingest_catalog_digest,
            contracts=contracts,
            contract_digests=recorded_contracts,
        ),
    )
    return ResolvedPinnedDefinition(
        compiled=compiled,
        contracts=contracts,
        ingest_catalog=ingest,
    )
```

For every root `target -> digest`, read `.graph-runtime/contracts/<digest>.json`, verify canonical bytes, model digest, and declared target, reconstruct the catalog, and call `compile_historical_workflow(schema, context=context)`. Require exact equality of compiled and recorded contract maps and ingest digests. Monkeypatch both current ingest and current execution-contract loaders to raise; the historical entry point must still succeed.

Test that requests differing only in one contract digest cannot collide in a cache keyed by graph digest. Missing, malformed, wrong-target, or digest-mismatched runtime snapshots fail before returning a compiled object. `bind_replay_definitions()` must call this same public loader.

- [ ] **Step 6: Run tests, type checks, and commit**

```bash
uv run pytest -q \
  tests/unit/workflow/graph/test_ingest_catalog.py \
  tests/unit/workflow/graph/test_contracts.py \
  tests/unit/workflow/graph/test_compiler.py \
  tests/unit/workflow/graph/test_replay_binding.py
uv run pyright
git add assurance_agent/workflow/graph/ingest_catalog.py assurance_agent/workflow/graph/contracts.py assurance_agent/workflow/graph/compiler.py assurance_agent/workflow/graph/definition_pinning.py assurance_agent/workflow/graph/replay_binding.py tests/unit/workflow/graph/test_ingest_catalog.py tests/unit/workflow/graph/test_contracts.py tests/unit/workflow/graph/test_compiler.py tests/unit/workflow/graph/test_replay_binding.py
git diff --cached --check
git commit -m "feat(replay): compile from pinned historical definitions"
```

---

### Task 11: Bind Definitions First and Recover Evidence Only for Classified Wired Layers

**Files:**
- Modify: `assurance_agent/workflow/graph/replay_binding.py`
- Modify: `assurance_agent/workflow/orchestration/plan_check_replay.py`
- Modify: `tests/unit/workflow/graph/test_replay_binding.py`
- Modify: `tests/unit/workflow/graph/test_four_layer_replay.py`
- Modify: `tests/unit/workflow/orchestration/test_plan_check_replay.py`

**Interfaces:**
- Changes `bind_replay_definitions()` to stop requiring hard-coded API/E2E child invocations during definition anchoring.
- Makes `FrozenDefinitionBinding.layer_topology_specs` immutable descriptors derived from pinned definition evidence, never the current profile registry.
- Makes `FrozenDefinitionBinding.layer_topologies` the per-layer authority for recovery.
- Makes `recover_layer_inputs(binding, layer="fuzz")` dynamically bind the branch/cycle/node IDs discovered from the pinned graph.
- Separates pinned structural authority from executable compatibility: classification/evidence selection use pinned metadata only; the existing current gate evaluator runs only after exact profile and gate-semantics compatibility succeeds.

- [ ] **Step 1: Add two-stage binding tests**

Require this binding shape:

```python
@dataclass(frozen=True, slots=True)
class FrozenDefinitionBinding:
    change_id: str
    root_invocation_id: str
    assurance_invocation_id: str
    event_schema_version: int
    graph_digest: str
    gate_definition_source: Literal["pinned_schema"]
    baseline_policy_digest: str
    policy_source: Literal["pinned_runtime_snapshot"]
    policy_origin: str
    gate_semantics_digest: str
    assurance_profile_digest: str
    compiled: CompiledWorkflow
    policy: Policy
    assurance_params: dict[str, object]
    selected_layers: frozenset[str]
    profile_manifest: AssuranceProfileManifest | None
    layer_topology_specs: tuple[LayerTopologySpec, ...]
    profile_compatibility: dict[str, bool]
    gate_semantics_compatible: bool
    layer_topologies: dict[str, PinnedLayerTopology]
    sequenced_events: tuple[SequencedEvent, ...]
```

Assert definition binding succeeds for a definition-valid v4 graph with legacy-unwired Fuzz/Performance even when current Fuzz/Performance profile or gate semantics differ. Assert v5 missing/tampered profile snapshots fail with `profile_snapshot_missing` or `profile_snapshot_digest_mismatch`; a typed but incompatible entry yields `profile_definition_incompatible`. Assert a v4 fully activated Fuzz/Performance graph without a profile snapshot classifies partial/incomplete rather than complete.

For v5, mutate/monkeypatch every current Fuzz/Performance profile path, alias, gate ID, and check set after the snapshot is pinned. Rebinding the same history must produce byte-for-byte identical `layer_topology_specs` and classifications. Spy on `get_layer_assurance_profile()` and fail the test if the v5 topology classifier or structural child/evidence selector calls it. Then test the compatibility boundary separately: a mismatched normalized current profile yields `profile_definition_incompatible` before baseline/counterfactual evaluation; an exactly matching current profile plus compatible gate-semantics digest permits the real current evaluator to resolve that profile and produces stable calibration results.

- [ ] **Step 2: Add frozen-evidence authority tests**

Change the current active review, checks, L1, and policy files after creating an object-tree-backed fixture; replay must remain byte-for-byte unchanged. Remove a required tree object while keeping an active file with the same logical path; replay must return `missing_evidence`, never use that file.

Add committed-attempt selection tests:

- ignore failed, abandoned, uncommitted, and post-terminal attempts;
- select the last committed gate at or before the cycle terminal;
- select the last earlier committed mechanical attempt whose output digest equals the gate read digest;
- require the attempt contract digest to equal the pinned compiled node contract digest;
- verify raw tree bytes against every recorded digest before parsing.

When dynamically binding assurance, branch, and cycle child invocations, require every `graph_invocation_started` event to match the root's event epoch, graph/ingest/contract/policy/gate-semantics/profile definition binding and its expected parent task/structural path. A structurally matching child from another definition epoch is `ambiguous_graph_wiring`, never evidence.

- [ ] **Step 3: Add gate/report/route calibration tests**

Compare only real report fields: `gate_id`, `verdict`, `matched_rule`, `reason`, `details`, and `reads_sha256`. Independently derive recorded and recomputed routes via `plan_review_route`, then validate the pinned downstream activation/skip/terminal evidence. Add separate `baseline_gate_mismatch` and `baseline_route_mismatch` mutations.

- [ ] **Step 4: Run focused tests and observe hard-coded layer/fallback failures**

```bash
uv run pytest -q \
  tests/unit/workflow/graph/test_replay_binding.py \
  tests/unit/workflow/graph/test_four_layer_replay.py \
  tests/unit/workflow/orchestration/test_plan_check_replay.py
```

- [ ] **Step 5: Refactor definition binding into the required order**

Perform these stages without per-layer evidence parsing:

1. bind the requested terminal root;
2. verify pinned schema, ingest catalog, execution contracts, and policy;
3. compile historically;
4. bind frozen root/assurance params and selection facts;
5. parse/verify the v5 profile snapshot or record the permitted v4 compatibility state;
6. derive immutable `LayerTopologySpec` values from that pinned manifest and classify all four pinned layer topologies; and
7. retain sequenced committed events for later recovery.

For v5, `LayerTopologySpec` construction consumes only `AssuranceProfileManifest` entries and the pinned schema. For v4 API/E2E, it may use the available normalized manifest only after proving its digest compatible. For legacy v4 Fuzz/Performance, run the three-marker absence/partial probe directly against the pinned schema before any current-profile compatibility lookup; it can return `legacy_unwired` or `partial`, but never `wired/complete` without a pinned profile snapshot. Delete `_WIRED_LAYERS`, `_LAYER_CYCLE_GRAPH`, and definition-time assumptions that API/E2E child invocations must exist.

- [ ] **Step 6: Recover only a wired layer from discovered structural IDs**

Keep the public seam:

```python
def recover_layer_inputs(
    binding: FrozenDefinitionBinding,
    *,
    layer: str,
    change_dir: Path,
    store: TreeStore | None = None,
) -> BoundLayerReplayInputs:
    topology = binding.layer_topologies[layer]
    if topology.status == "partial":
        raise ReplayBindingError(
            "partial_assurance_wiring", "selected layer has partial pinned topology"
        )
    if topology.status != "wired":
        raise ReplayBindingError(
            "ambiguous_graph_wiring", "evidence recovery requires wired topology"
        )
    if not binding.profile_compatibility[layer]:
        raise ReplayBindingError(
            "profile_definition_incompatible", "pinned profile cannot be executed"
        )
    if not binding.gate_semantics_compatible:
        raise ReplayBindingError(
            "gate_semantics_mismatch", "pinned gate evaluator is incompatible"
        )
    return _recover_wired_layer(binding, topology, change_dir=change_dir, store=store)
```

Remove `_read_fallback_bytes()`. Applicable state recovers review, checks, and L1; inapplicable state recovers checks only and explicitly returns null review/L1/capability summaries.

After structural evidence is bound, compare the selected pinned manifest entry byte-for-byte with the same entry in the normalized current manifest and require the recorded gate-semantics digest to match. On either mismatch return `profile_definition_incompatible` or `gate_semantics_mismatch` before invoking the evaluator. Only after both checks succeed may `plan_check_replay.py` call the existing `check_gate_in_view()`/`plan_assurance_state()` path, which may resolve the now-proven-equivalent current profile. The pinned manifest remains metadata and audit evidence, not executable archived Python; do not fork or copy the gate DSL, rule ordering, route logic, or check-document validation.

- [ ] **Step 7: Implement version-specific profile compatibility order**

- v5: verify and parse the snapshot before any row can be complete.
- v5: derive topology specs, classify wiring, and select structural evidence from the parsed snapshot with no current-profile access; consult the normalized current manifest/evaluator only at the later explicit compatibility/calibration boundary.
- v4 legacy Fuzz/Performance: classify `legacy_unwired` before current profile/gate compatibility checks.
- v4 wired API/E2E: permit completion only when the recorded digest is provably compatible with the available normalized manifest.
- v4 graphs with complete Fuzz/Performance activation markers but no snapshot: return incomplete.
- pre-v4 or broken root definition bindings: remain report-level incomplete and cannot be upgraded to `not_wired`.

- [ ] **Step 8: Run tests, type checks, and commit**

```bash
uv run pytest -q \
  tests/unit/workflow/graph/test_replay_binding.py \
  tests/unit/workflow/graph/test_four_layer_replay.py \
  tests/unit/workflow/orchestration/test_plan_check_replay.py
uv run pyright
git add assurance_agent/workflow/graph/replay_binding.py assurance_agent/workflow/orchestration/plan_check_replay.py tests/unit/workflow/graph/test_replay_binding.py tests/unit/workflow/graph/test_four_layer_replay.py tests/unit/workflow/orchestration/test_plan_check_replay.py
git diff --cached --check
git commit -m "feat(replay): bind evidence by pinned layer topology"
```

---

### Task 12: Emit Topology-Driven Replay Semantics v2

**Files:**
- Modify: `assurance_agent/eval/specialty_models.py`
- Modify: `assurance_agent/eval/specialty_replay.py`
- Modify: `assurance_agent/eval/specialty_render.py`
- Modify: `benchmark/vue-fastapi-admin/benchmark/benchmark_specialty_report.py`
- Modify: `benchmark/vue-fastapi-admin/benchmark/cursor-loop-helpers.sh`
- Modify: `benchmark/vue-fastapi-admin/benchmark/run-workflow-loop-cursor.sh`
- Modify: `tests/unit/eval/test_specialty_models.py`
- Modify: `tests/unit/workflow/graph/test_four_layer_replay.py`
- Modify: `tests/unit/benchmark/test_specialty_report.py`
- Modify: `tests/unit/benchmark/test_cursor_loop_helpers.py`

**Interfaces:**
- New output semantics: `counterfactual_plan_check_actions/v2` with report `schema_version: "2"` unchanged.
- Read compatibility: semantics v1 and legacy specialty schema-version 1.
- Layer row status derives from pinned selection and topology, never static wired/unwired sets.

- [ ] **Step 1: Add failing model compatibility tests**

Introduce:

```python
ReplaySemantics = Literal[
    "counterfactual_plan_check_actions/v1",
    "counterfactual_plan_check_actions/v2",
]


class CapabilityPolicyReplayV2(BaseModel):
    semantics: ReplaySemantics = "counterfactual_plan_check_actions/v2"
```

Assert the loader and renderer accept frozen semantics-v1 reports unchanged. Assert new builders emit v2. Make row topology validation conditional: semantics v1 continues to reject a forged Fuzz/Performance `complete` row and retains its historical API/E2E-wired interpretation; semantics v2 permits a complete row only after the collector's pinned topology/evidence path. Retain exact four-layer order, layer/case-type matching, complete row shape, four ordered checks, and exactly three scenarios in `warn`, `block`, `require_human` order.

- [ ] **Step 2: Add the full topology/evidence result matrix**

Parameterize:

| Pinned definition | Selected | Evidence | Expected row |
|---|---:|---|---|
| valid v4 legacy Fuzz/Performance | yes | none | `not_wired` |
| valid v4 legacy Fuzz/Performance | yes | stray active F/P files | `not_wired` |
| valid v4 legacy Fuzz/Performance | no | any | `not_selected` |
| v5 fully wired applicable | yes | complete review/checks/L1/gate/mechanical | `complete` |
| v5 fully wired inapplicable | yes | complete checks/gate/mechanical | `complete` |
| v5 fully wired | yes | missing or drifted item | `incomplete` |
| v5 partial/duplicate wiring | yes | pass-shaped active files | `incomplete/partial_assurance_wiring` |
| forged v4 fully wired Fuzz/Performance | yes | no profile snapshot | `incomplete` |
| v5 missing/tampered profile snapshot | any | any | definition-level `incomplete` |
| pre-v4 or broken pinned binding | yes | any | `incomplete`, never upgraded |
| wired topology with ambiguous structural child invocation | yes | otherwise complete | `incomplete/ambiguous_graph_wiring` |
| frozen semantics-v1 report with F/P complete | any | forged row | model rejection |

For complete applicable rows assert review/checks/L1 digests, mechanical contract digest, four check summaries with N/A `assert_ideal`, capability summaries, and three counterfactuals. For complete inapplicable rows assert only checks/mechanical digests, null review/L1/capabilities, four layer-N/A checks, and three skip scenarios.

Before Task 13, exercise Fuzz/Performance complete-row construction with an explicit synthetic `FrozenDefinitionBinding` whose pinned topology and compatibility fields are set by the fixture; do not switch the current profile registry early. Task 13 adds the public end-to-end `bind_replay_definitions` complete-row proof after the strong profiles are active.

- [ ] **Step 3: Run the model/collector tests and observe static-layer failures**

```bash
uv run pytest -q \
  tests/unit/eval/test_specialty_models.py \
  tests/unit/workflow/graph/test_four_layer_replay.py \
  tests/unit/benchmark/test_specialty_report.py \
  tests/unit/benchmark/test_cursor_loop_helpers.py
```

- [ ] **Step 4: Make the collector topology-driven**

Use this precedence for each profile in canonical order:

```python
if layer not in binding.selected_layers:
    return not_selected_row(layer)
if topology.status == "legacy_unwired":
    return not_wired_row(layer)
if topology.status == "partial":
    return incomplete_row(layer, reason="partial_assurance_wiring")
return replay_wired_layer(binding, layer=layer)
```

Delete `_WIRED_LAYERS` and `_UNWIRED_LAYERS` from the model, collector, and binding modules. Counterfactuals clone the pinned baseline policy, set all plan-check actions to one scenario value, and call the real pinned gate evaluator only when gate semantics are compatible.

- [ ] **Step 5: Remove the current-schema replay escape hatch from benchmark helpers**

Delete the replay API's `schema_root` argument and any benchmark `--schema-root` flag/forwarding. A specialty replay must be constructible from `change_dir`, change ID, root invocation, immutable object store, and pinned ledger definitions only.

- [ ] **Step 6: Run tests and commit replay v2 output**

```bash
uv run pytest -q \
  tests/unit/eval/test_specialty_models.py \
  tests/unit/workflow/graph/test_four_layer_replay.py \
  tests/unit/benchmark/test_specialty_report.py \
  tests/unit/benchmark/test_cursor_loop_helpers.py
uv run pyright
git add assurance_agent/eval/specialty_models.py assurance_agent/eval/specialty_replay.py assurance_agent/eval/specialty_render.py benchmark/vue-fastapi-admin/benchmark/benchmark_specialty_report.py benchmark/vue-fastapi-admin/benchmark/cursor-loop-helpers.sh benchmark/vue-fastapi-admin/benchmark/run-workflow-loop-cursor.sh tests/unit/eval/test_specialty_models.py tests/unit/workflow/graph/test_four_layer_replay.py tests/unit/benchmark/test_specialty_report.py tests/unit/benchmark/test_cursor_loop_helpers.py
git diff --cached --check
git commit -m "feat(eval): emit topology-driven policy replay v2"
```

---

### Task 13: Atomically Activate Skills, Registry/Profile, Packaged Graph, and Event v5

**Files:**
- Modify: `assurance_agent/_resources/skills/aa-fuzz-plan/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-fuzz-plan-reviewer/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-fuzz-codegen/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-performance-plan/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-performance-plan-reviewer/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-performance-codegen/SKILL.md`
- Modify: `assurance_agent/_resources/schemas/workflow-schema.yaml`
- Create: `assurance_agent/artifacts/policy_obligations.py`
- Modify: `assurance_agent/artifacts/registry.py`
- Modify: `assurance_agent/eval/fixtures.py`
- Modify: `assurance_agent/verification/profiles.py`
- Modify: `assurance_agent/workflow/graph/compiler.py`
- Modify: `assurance_agent/workflow/graph/checkpoint.py`
- Modify: `assurance_agent/workflow/graph/definition_pinning.py`
- Modify: `assurance_agent/workflow/graph/finalize.py`
- Modify: `assurance_agent/workflow/graph/ingest.py`
- Modify: `assurance_agent/workflow/graph/ingest_catalog.py`
- Modify: `assurance_agent/workflow/graph/schema_v2.py`
- Modify: `assurance_agent/workflow/graph/runtime.py`
- Modify: `assurance_agent/workflow/graph/task_runner.py`
- Modify: `assurance_agent/workflow/driver/runtime_factory.py`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-fuzz-codegen-seed.yaml`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-performance-codegen-seed.yaml`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/cases/system/fuzz/case.yaml`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/cases/system/performance/case.yaml`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/fuzz-plan.md`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/fuzz-codegen-plan.md`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/fuzz-review-summary.md`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/performance-plan.md`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/performance-codegen-plan.md`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/performance-review-summary.md`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/fixture-lock.json`
- Modify: `tests/helpers_graph_v3.py`
- Create: `tests/unit/test_fuzz_performance_skills.py`
- Modify: `tests/unit/test_gates.py`
- Modify: `tests/unit/eval/test_fixtures.py`
- Modify: `tests/unit/artifacts/test_models_review_explore.py`
- Modify: `tests/unit/artifacts/test_registry.py`
- Modify: `tests/unit/verification/test_contract_render.py`
- Modify: `tests/unit/verification/test_profiles.py`
- Modify: `tests/unit/verification/test_profile_manifest.py`
- Modify: `tests/unit/verification/test_gate_state.py`
- Modify: `tests/unit/workflow/orchestration/test_plan_check_gate.py`
- Modify: `tests/unit/workflow/orchestration/test_policy_scope.py`
- Modify: `tests/unit/verification/test_contract_round_trip.py`
- Modify: `tests/unit/verification/test_layer_assurance_round_trip.py`
- Modify: `tests/unit/workflow/graph/handlers/test_plan_checks_operation.py`
- Modify: `tests/unit/workflow/graph/test_canonical_schema_v2.py`
- Modify: `tests/unit/workflow/graph/test_compiler.py`
- Modify: `tests/unit/workflow/graph/test_schema_v2.py`
- Create: `tests/unit/workflow/driver/test_runtime_factory_compilation.py`
- Modify: `tests/unit/workflow/graph/test_packaged_schema_compiles.py`
- Modify: `tests/unit/workflow/graph/test_replay_schema.py`
- Modify: `tests/unit/workflow/graph/test_replay_binding.py`
- Modify: `tests/unit/workflow/graph/test_four_layer_replay.py`
- Create: `tests/integration/test_fuzz_performance_assurance_flow.py`
- Modify: `tests/integration/test_api_e2e_assurance_flow.py`
- Modify: `tests/unit/workflow/graph/test_import_checkpoint.py`
- Modify: `tests/unit/eval/test_eval_import_replay.py`
- Modify: `tests/unit/workflow/graph/test_archive_workflow.py`
- Modify: `tests/unit/workflow/graph/test_finalize_and_child_stop.py`
- Modify: `tests/unit/workflow/graph/test_ingest.py`
- Modify: `tests/unit/workflow/graph/test_ingest_catalog.py`
- Modify: `tests/unit/workflow/graph/test_policy_snapshot_runtime.py`
- Modify: `tests/unit/workflow/graph/test_retro_workflow.py`
- Modify: `tests/unit/workflow/graph/test_status_read.py`
- Modify: `tests/unit/workflow/graph/test_subgraph_interrupt.py`
- Modify: `tests/unit/workflow/graph/test_task_runner.py`
- Modify: `tests/integration/_graph_fault_worker.py`
- Modify: `tests/integration/test_graph_runtime_faults.py`
- Modify: `tests/integration/test_graph_runtime.py`
- Modify: `tests/integration/test_graph_interrupt_v3.py`
- Modify: `tests/integration/test_improvement_review_workflow.py`
- Modify: `tests/integration/test_issue_lifecycle_acceptance.py`
- Modify: `tests/integration/test_issue_lifecycle_workflow.py`
- Modify: `tests/integration/test_cli_workflow_v2.py`
- Modify: `tests/unit/test_dsl_schema_corpus.py`

**Interfaces:**
- Atomically publishes: aligned prompts, exact artifact/profile models, complete graph/gates, current four-layer release validation, and new-root event schema v5.
- Crosses: skill output → authoring model → runtime artifact model → applicability → mechanical document → gate → codegen precondition → committed evidence → replay.
- Mutates: every structural activation marker, ordering edge, gate read, hard predicate, revision allowlist, contract binding, and frozen evidence digest.
- Proves: full, plan-only, codegen-only, human resume, knowledge resume, and crash resume all preserve freshness.
- Proves: a process restart resolves an existing invocation's schema, ingest catalog, contracts, runner, and scheduler from its committed definition request rather than the newly installed package.

- [ ] **Step 1: Add failing atomic-activation and deferred-policy guards**

Before changing production resources, add assertions that currently fail together:

- exact Fuzz/Performance registry entries resolve to `PlanReview`/`PlanReviewAuthoring`, and both profiles use `PlanReview`;
- normalized profile manifest records the strong model and changes digest;
- all six skills match their execution contracts and contain no workflow-state, phase, `layer_applicable`, state-delta, removed TypeScript schema, or nonexistent fixer instructions;
- the packaged Fuzz/Performance gate canonical dumps equal `tests/fixtures/assurance/fuzz-performance-gates.yaml`;
- both parent branches and cycles classify fully wired, including the parent cases-only preflight, both knowledge-remediation edges, exact manual revision allowlists, and hard codegen AST predicates;
- explicit `compile_packaged_workflow` requires all four layers wired, core `compile_workflow` still accepts non-assurance synthetic graphs, and `compile_historical_workflow` still accepts the legacy fixture;
- a fresh root binding requests version 5, `_build_invocation_started()` emits the binding's version, and start staging writes/verifies the profile snapshot before the event;
- the existing gate-consumer inventory and policy-scope tests share one authority and prove `all_policy_leaf_paths == runtime_policy_consumers | deferred_policy_fields`, the sets are disjoint, and no packaged gate references a deferred field; delete the old runtime-only equality instead of leaving two contradictory inventories;
- the Task 2 fixtures pass artifact authoring/runtime validation, reviewed mechanical execution, the real plan gate, and codegen precondition.

- [ ] **Step 2: Add failing contract mutations at every producer/consumer edge**

For each layer independently mutate:

- review type, change ID, required capability, finding ID, and human-only auto-fix fields;
- missing plan, malformed/duplicate Factory Mapping, or incorrect Factory Mapping ownership;
- malformed case bucket, type, automation block, case ID, or strict boolean `automation.required` changed to string `"true"`;
- missing/malformed L1 or unknown capability;
- missing check entry, wrong layer, wrong applicability state, or reordered/duplicated check IDs;
- mechanical output digest different from the later gate read digest.

Every mutation must fail closed with an explicit validation/finding/incomplete reason; none may silently pass or become N/A.

- [ ] **Step 3: Add failing topology mutations**

Load the real packaged schema and first assert that its unmutated form satisfies `compile_packaged_workflow`. Then delete or duplicate one item at a time: branch binding, cycle binding, parent preflight, applicability node, review node, mechanical node, explicit gate owner, applicable-path edge, inapplicable-path edge, human route, manual allowlist item, the gate-to-knowledge-remediation route, the knowledge-remediation-to-mechanical return, codegen read, and each hard predicate. Before production changes, the base assertion or discovery of the missing new component is red; after activation, the base compiles and every mutation fails packaged compilation. Assert the pinned classifier returns partial, not legacy-unwired, whenever any activation marker remains.

Create a separate true legacy fixture by removing all three activation markers and assert historical classification returns `legacy_unwired` while packaged compilation rejects it. Assert core compilation still accepts a minimal non-assurance graph, so synthetic tests do not need a bypass flag.

- [ ] **Step 4: Add failing mode, epoch, import, join, and resume tests**

For both layers cover:

- full: all producers precede codegen;
- plan-only/review-plan: cycle completes and codegen is absent;
- codegen-only applicable: no plan generation, but review/mechanical/gate/precondition rerun;
- codegen-only inapplicable: no reviewer, four N/A checks, precondition skip;
- human `fix_and_proceed`: new leaf tree, new review/mechanical/gate epoch;
- knowledge `fix_and_proceed`: L1 synchronization then mechanical/gate rerun, with direct capability guard still required;
- `accept_risk`: cannot bypass missing capability;
- ordinary accepted risk remains valid only while its audited gate bytes and source committed tree are unchanged;
- `import-checkpoint` and ordinary resume continue the pinned invocation/definition epoch rather than compiling current definitions;
- an assurance run with Fuzz/Performance plus another active layer reaches `generation-join` only after all active branches finish;
- crash resume: legal prefixes repair exactly once and conflicts stop before planning.

Also add global epoch regressions before changing the root default: resume a pre-existing v4 root whose first child starts after a pinned resource-definition upgrade while its executable gate/profile digests remain compatible, and assert that child inherits v4 without a profile snapshot; run fresh v5 improvement-review and issue-review interrupts and assert their no-gate actions resume pairless; assert a Fuzz/Performance gate-backed v5 action carries the exact committed source pair; and prove fresh-v5 `healing.safety` resolves its `fixer-safety-gate` alias, records the source pair, and still applies an accepted-risk override only to that epoch. Audit `rg -n 'event_schema_version.*4|== 4' tests` before implementation: update every fresh `run()`/`import_checkpoint()` assertion and shared fresh-start helper to v5 plus staged profile bytes, while preserving explicit historical/migration v4 fixtures. A v5 live-compatibility mutation must complete a legal definition-independent manual-prefix repair and then stop before definition-dependent recovery, materialization, or planning. A v4 mutation has no revision prefix—Task 6 forbids one—and must fail at the same compatibility boundary without fabricating revision events.

Add a real restart regression, not an in-memory resolver stub: start and interrupt an explicitly version-4 invocation under the current executable-semantics digest, destroy the `RuntimeBundle`, modify the current packaged/project schema, ingest catalog, and execution contracts, build a new runtime, and resume the old invocation. Resolution must use the committed `PinnedDefinitionRequest`, return the pinned historical compiled graph, pinned contract catalog, and pinned ingest catalog, build execution services from those objects, and preserve the v4 child epoch. Remove or corrupt each pinned schema/ingest/contract snapshot and assert restart fails closed before pending write-set repair, ordinary materialization repair, or planning. Change a current ingest model implementation so its schema digest no longer matches the pinned catalog and assert live resume fails before planning. Separately mutate the current gate-semantics implementation digest and normalized assurance-profile digest for both v4 and v5 roots; live resume must fail closed rather than reinterpret either epoch. For v5 incompatibility cases, first seed a legal partial manual-revision resume chain and prove the definition-independent missing suffix is repaired before definition resolution fails. For v4, assert revision events are rejected and compatibility fails without them. Replay's v4 legacy classification remains independent and must still return `not_wired`. Also start a fresh invocation after the resource upgrade and prove it uses the newly origin-selected packaged definition/catalog rather than the old pinned ones.

Audit every direct constructor with `rg -l 'GraphRuntime\(' assurance_agent tests`. Convert synthetic callers to an explicit one-definition bundle resolver and production callers to the exact request resolver; do not keep the old constructor as a graph-digest-only compatibility path. Update `_graph_fault_worker.py` to retain the concrete scheduler it places in its synthetic resolved bundle and pass that object into `_install_hooks`; it must not recover a scheduler through `runtime._scheduler` or another singleton compatibility attribute.

Replace the old `test_gates.py` reviewer-authored `layer_applicable` cases with complete checks/L1 fixtures proving mechanical inapplicability skips and applicable reject/pass behavior remains fail-closed. Update both L2 Fuzz/Performance codegen import-tier YAML files and `test_fixtures.py` to seed the full new predecessor chain—cycle applicability, review when applicable, reviewed mechanical checks/output, explicit cycle gate, completed review cycle, then branch `codegen-precheck` with the layer codegen-precondition gate—before codegen. A tier pinned to the removed shallow `codegen-gate` or reviewer-attached plan gate must fail explicitly rather than silently bypassing new evidence.

Add the Task 2 canonical Fuzz/Performance case and plan bytes to the locked benchmark sample. Replace `_STUB_REVIEW_DOCS` with a typed `_ensure_assurance_seed_artifacts(change_dir, project_root, change_id, import_def)` path: it writes a change-ID-correct `PlanReview`, canonical L1 leaves, and a `PlanCheckDocument` produced by the real `run_plan_checks` from those cases/plans; it must not hand-author pass-shaped checks. Pass `change_id` from `_write_import_manifest`, include both review summary/check outputs in the tier, and test real `seed_change()` for both tiers through import-manifest hashing and gate evaluation. Regenerate and review `fixture-lock.json`; a structural-only tier test is insufficient.

The import projection must expose the same node-result shape as a committed execution. Maintain `node_results_by_structural_path`, so same-named nodes in nested/sibling graphs cannot overwrite one another; pass only the current graph instance's local mapping to gate/edge/route evaluation. In both `_write_import_manifest()` and `validate_import()`, record `local_results[task.node]["status"] = "succeeded"` for every completed task and merge—not replace—the optional `gate` report. Enhance predecessor validation with the planner's existing edge/route DSL semantics: the imported successor must be the actual target selected by an already validated predecessor result under frozen params/state. In particular, a `codegen-precheck` whose recomputed verdict is `stop` or `skip` cannot satisfy predecessor closure for codegen even if both task IDs appear in the manifest. Add real-seed assertions that `review-cycle.status == "succeeded"` makes the precheck pass, a missing ordinary-node status fails closed, and negative Fuzz/Performance manifests that include the full structural chain but force `stop` are rejected before ledger writes.

- [ ] **Step 5: Run the pre-activation suite and observe the intended failures**

Run the exact focused command from Step 9 before changing production resources. The new registry/profile, packaged topology, policy-disjointness, fresh-root-v5, and public end-to-end assertions must fail for the old implementation. Record the failing assertion names; do not weaken them to make the red phase pass.

- [ ] **Step 6: Publish the six aligned skill contracts**

Keep Fuzz reviewer obligations for schema source, related API cases, authentication semantics, and seed/corpus adequacy. Keep Performance obligations for absolute thresholds, load shape, scenario coverage, and statistical interpretation. Both plan outputs add canonical `## Factory Mapping`. Remove prompt-owned workflow progression. Applicable reviewers always emit non-empty fully qualified capabilities, `auto_fix_allowed: false`, `auto_fix_plan: []`, and a manual/human next action for `needs_fix`. Codegen inputs name the exact plan/summary, strong review, checks, cases, config, L1, and bounded style inputs; the graph gate is the progression authority.

- [ ] **Step 7: Activate exact artifact registration, strong profiles, and the deferred obligation**

Insert exact `review/fuzz-plan-review.json` and `review/performance-plan-review.json` specs before `review/*.json`, both with runtime `PlanReview` and authoring `PlanReviewAuthoring`. Switch the two profile `review_model` values to `PlanReview`. Update Task 1's temporary compatibility assertion to require the exact strong model.

Create this code-owned non-consumer in the same activation commit:

```python
@dataclass(frozen=True, slots=True)
class DeferredPolicyObligation:
    field: str
    owner: str
    status: Literal["deferred"]
    reason: str


DEFERRED_POLICY_OBLIGATIONS = {
    "fuzz.required_when_endpoint_has_auth": DeferredPolicyObligation(
        field="fuzz.required_when_endpoint_has_auth",
        owner="fuzz-auth-applicability",
        status="deferred",
        reason="no_typed_endpoint_auth_fact",
    )
}
```

Keep this registry out of gate verdict expressions and the executable gate-semantics digest. Its exact-set AST test is the change detector; it must not claim runtime enforcement.

- [ ] **Step 8: Publish the complete graph and event epoch atomically**

Replace both parent branches with the cases-only preflight and both cycles with explicit applicability, reviewer, reviewed mechanical producer, separate gate, human revision interrupt, and knowledge-remediation interrupt. Route:

```text
START -> applicability-preflight
applicability-preflight[true, non-codegen-only] -> plan -> review-cycle
applicability-preflight[true, codegen-only] -> review-cycle
applicability-preflight[false] -> review-cycle
review-gate.pass -> END
review-gate.skip -> END
review-gate.needs_fix -> human-review
review-gate.knowledge_remediation -> knowledge-remediation
review-gate.needs_human_review -> human-review
review-gate.reject|stop|default -> STOP
human-review.fix_and_proceed -> review
knowledge-remediation.fix_and_proceed -> mechanical-plan-checks
```

For full and codegen-only entrypoints, the parent continues `review-cycle -> codegen-precheck -> codegen -> END`; plan-only/review-plan ends after the cycle and never schedules codegen.

Copy the canonical gate definitions from Task 8, remove `policy.fuzz.required_when_endpoint_has_auth` and every `layer_applicable` reference, add exact mechanical node reads/writes/synchronization/locks for all four layers, and remove attached reviewer/shallow branch gates. Every mechanical node declares its exact checks path in both `resources.writes` and `outputs`; final planned authorization must contain no wildcard. Add three non-overlapping compiler entry points: core `compile_workflow()` for explicit synthetic/custom schemas, `compile_packaged_workflow()` which always calls `validate_current_assurance_activation`, and the existing pinned-only `compile_historical_workflow()`. Delete `_has_packaged_assurance_surface`; no graph-name/content heuristic may select validation. Extend schema loading to return an explicit `packaged | project | explicit` origin, make runtime/eval default-resource call sites choose `compile_packaged_workflow` only from the packaged origin, and add a runtime-factory test that a malformed packaged surface cannot downgrade itself to core compilation by deleting a sentinel graph. Fresh root call sites request `bind_root_definitions(store=store, root_tree_id=root_tree_id, event_schema_version=5)`, while `_build_invocation_started()` continues to emit the binding value and children continue to inherit their parent epoch. Stage and verify the required profile snapshot before appending a v5 start event.

Replace the graph-digest-only resolver with an exact committed-definition resolver. Define a resolved execution bundle containing the immutable `PinnedDefinitionRequest`, `compiled`, its matching `ExecutionContractCatalog`, its matching `IngestArtifactCatalog`, a prevalidated model-ID-to-current-class map, and execution services built from those objects. `GraphRuntime` constructs the request from the projection's graph, ingest-catalog, contract, event-version, gate-semantics, and profile identities; `runtime_factory` returns the current bundle only when every identity matches and otherwise calls Task 10's `load_pinned_execution_definition()`. Cache by the complete request, never graph digest alone.

Runtime startup/resume has two ordered recovery phases. First, under the progression lock, repair any open manual-revision resume prefix solely from committed transition/resume events and reproject; this phase must not load schema, contracts, catalogs, profiles, or current semantic code. Then construct the exact definition request. Before pending write-set recovery, publication/materialization repair, or planning, live resolution must require `request.gate_semantics_digest == gate_semantics_digest()` and `request.assurance_profile_digest == assurance_profile_digest()` for both v4 and v5. A mismatch raises `GraphDefinitionChanged` with the existing graph-definition-changed boundary; it is not the replay compatibility path and does not alter legacy replay classification. Validate every executable model referenced by the resolved ingest catalog against that catalog's pinned `model_schema_digest`; snapshots archive metadata, not Python, so a mismatch also fails before definition-dependent recovery or planning.

Refactor `ingest_from_write_set()` to accept the resolved catalog and validated model map. Its catalog lookup must use those supplied objects and must not call `validate_catalog_runtime()`, `load_ingest_catalog()`, or a current-catalog lookup during pinned execution. Thread this ingest runtime through `build_default_node_runner()` → `HandlerNodeRunner` → `finalize_task_result()` → `_ingest_frozen_outputs()`. Build/cache a `NodeRunner` and `Scheduler` per resolved definition so handler finalization, ingestion, read isolation, reconnect behavior, retries, locks, and write authorization never use current contracts/catalog while executing a pinned graph. Thread that resolved scheduler through recovery barriers, pending write-set commit/repair, ordinary drive, and child start; thread the resolved contracts/catalog through definition staging and task finalization. A child resolves from its parent projection and inherits the parent's exact definition/epoch. Do not retain singleton `self._contracts`, `self._node_runner`, or `self._scheduler` as hidden current-definition fallbacks.

Use explicit public shapes rather than a boolean escape hatch:

```python
WorkflowSchemaOrigin = Literal["packaged", "project", "explicit"]


@dataclass(frozen=True, slots=True)
class LoadedWorkflowV2:
    schema: WorkflowSchemaV2
    origin: WorkflowSchemaOrigin


def compile_packaged_workflow(
    schema: WorkflowSchemaV2,
    contracts: ExecutionContractCatalog | None = None,
) -> CompiledWorkflow:
    errors = validate_current_assurance_activation(schema)
    if errors:
        details = "\n  - ".join(errors)
        raise CompileError(f"packaged assurance activation failed:\n  - {details}")
    return compile_workflow(schema, contracts)
```

Expose `load_workflow_v2_with_origin(project_root: Path, explicit: Path | None = None) -> LoadedWorkflowV2`; its implementation must preserve whether resolution selected an explicit path, a project override, or the packaged resource. Keep the existing `load_workflow_v2()` as a compatibility wrapper returning `.schema`; it must not guess compilation purpose.

After adding the canonical benchmark case/plan files and typed seed generation, regenerate the lock with the repository helper and inspect its diff:

```bash
uv run python -c "from pathlib import Path; from assurance_agent.eval.fixtures import write_fixture_lock; root = Path('benchmark/vue-fastapi-admin/eval-fixtures'); write_fixture_lock(root, {'eval-sample-001': 'samples/eval-sample-001'})"
```

- [ ] **Step 9: Run the complete activation, mutation, and regression suite**

```bash
uv run pytest -q \
  tests/unit/test_fuzz_performance_skills.py \
  tests/unit/test_gates.py \
  tests/unit/eval/test_fixtures.py \
  tests/unit/artifacts/test_models_review_explore.py \
  tests/unit/artifacts/test_registry.py \
  tests/unit/verification/test_contract_render.py \
  tests/unit/verification/test_profiles.py \
  tests/unit/verification/test_profile_manifest.py \
  tests/unit/verification/test_gate_state.py \
  tests/unit/workflow/orchestration/test_plan_check_gate.py \
  tests/unit/workflow/orchestration/test_policy_scope.py \
  tests/unit/verification/test_contract_round_trip.py \
  tests/unit/verification/test_layer_assurance_round_trip.py \
  tests/unit/workflow/graph/handlers/test_plan_checks_operation.py \
  tests/unit/workflow/graph/test_canonical_schema_v2.py \
  tests/unit/workflow/graph/test_compiler.py \
  tests/unit/workflow/graph/test_schema_v2.py \
  tests/unit/workflow/driver/test_runtime_factory_compilation.py \
  tests/unit/workflow/graph/test_packaged_schema_compiles.py \
  tests/unit/workflow/graph/test_replay_schema.py \
  tests/unit/workflow/graph/test_replay_binding.py \
  tests/unit/workflow/graph/test_four_layer_replay.py \
  tests/unit/workflow/graph/test_import_checkpoint.py \
  tests/unit/eval/test_eval_import_replay.py \
  tests/unit/workflow/graph/test_archive_workflow.py \
  tests/unit/workflow/graph/test_finalize_and_child_stop.py \
  tests/unit/workflow/graph/test_ingest.py \
  tests/unit/workflow/graph/test_ingest_catalog.py \
  tests/unit/workflow/graph/test_policy_snapshot_runtime.py \
  tests/unit/workflow/graph/test_retro_workflow.py \
  tests/unit/workflow/graph/test_status_read.py \
  tests/unit/workflow/graph/test_subgraph_interrupt.py \
  tests/unit/workflow/graph/test_task_runner.py \
  tests/unit/test_dsl_schema_corpus.py \
  tests/integration/test_api_e2e_assurance_flow.py \
  tests/integration/test_fuzz_performance_assurance_flow.py \
  tests/integration/test_graph_runtime.py \
  tests/integration/test_graph_runtime_faults.py \
  tests/integration/test_graph_interrupt_v3.py \
  tests/integration/test_improvement_review_workflow.py \
  tests/integration/test_issue_lifecycle_acceptance.py \
  tests/integration/test_issue_lifecycle_workflow.py \
  tests/integration/test_cli_workflow_v2.py
uv run ruff check .
uv run pyright
uv run lint-imports
```

- [ ] **Step 10: Commit the atomic activation**

```bash
git add assurance_agent/_resources/skills/aa-fuzz-plan/SKILL.md assurance_agent/_resources/skills/aa-fuzz-plan-reviewer/SKILL.md assurance_agent/_resources/skills/aa-fuzz-codegen/SKILL.md assurance_agent/_resources/skills/aa-performance-plan/SKILL.md assurance_agent/_resources/skills/aa-performance-plan-reviewer/SKILL.md assurance_agent/_resources/skills/aa-performance-codegen/SKILL.md assurance_agent/_resources/schemas/workflow-schema.yaml assurance_agent/artifacts/policy_obligations.py assurance_agent/artifacts/registry.py assurance_agent/eval/fixtures.py assurance_agent/verification/profiles.py assurance_agent/workflow/driver/runtime_factory.py assurance_agent/workflow/graph/checkpoint.py assurance_agent/workflow/graph/compiler.py assurance_agent/workflow/graph/definition_pinning.py assurance_agent/workflow/graph/finalize.py assurance_agent/workflow/graph/ingest.py assurance_agent/workflow/graph/ingest_catalog.py assurance_agent/workflow/graph/schema_v2.py assurance_agent/workflow/graph/runtime.py assurance_agent/workflow/graph/task_runner.py benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-fuzz-codegen-seed.yaml benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-performance-codegen-seed.yaml benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/cases/system/fuzz/case.yaml benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/cases/system/performance/case.yaml benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/fuzz-plan.md benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/fuzz-codegen-plan.md benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/fuzz-review-summary.md benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/performance-plan.md benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/performance-codegen-plan.md benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/performance-review-summary.md benchmark/vue-fastapi-admin/eval-fixtures/fixture-lock.json tests/helpers_graph_v3.py tests/unit/test_fuzz_performance_skills.py tests/unit/test_gates.py tests/unit/eval/test_eval_import_replay.py tests/unit/eval/test_fixtures.py tests/unit/artifacts/test_models_review_explore.py tests/unit/artifacts/test_registry.py tests/unit/verification/test_contract_render.py tests/unit/verification/test_profiles.py tests/unit/verification/test_profile_manifest.py tests/unit/verification/test_gate_state.py tests/unit/verification/test_contract_round_trip.py tests/unit/verification/test_layer_assurance_round_trip.py tests/unit/workflow/orchestration/test_plan_check_gate.py tests/unit/workflow/orchestration/test_policy_scope.py tests/unit/workflow/graph/handlers/test_plan_checks_operation.py tests/unit/workflow/graph/test_archive_workflow.py tests/unit/workflow/graph/test_canonical_schema_v2.py tests/unit/workflow/graph/test_compiler.py tests/unit/workflow/graph/test_finalize_and_child_stop.py tests/unit/workflow/graph/test_ingest.py tests/unit/workflow/graph/test_ingest_catalog.py tests/unit/workflow/graph/test_schema_v2.py tests/unit/workflow/driver/test_runtime_factory_compilation.py tests/unit/workflow/graph/test_packaged_schema_compiles.py tests/unit/workflow/graph/test_replay_schema.py tests/unit/workflow/graph/test_replay_binding.py tests/unit/workflow/graph/test_four_layer_replay.py tests/unit/workflow/graph/test_import_checkpoint.py tests/unit/workflow/graph/test_policy_snapshot_runtime.py tests/unit/workflow/graph/test_retro_workflow.py tests/unit/workflow/graph/test_status_read.py tests/unit/workflow/graph/test_subgraph_interrupt.py tests/unit/workflow/graph/test_task_runner.py tests/unit/test_dsl_schema_corpus.py tests/integration/_graph_fault_worker.py tests/integration/test_api_e2e_assurance_flow.py tests/integration/test_fuzz_performance_assurance_flow.py tests/integration/test_graph_runtime.py tests/integration/test_graph_runtime_faults.py tests/integration/test_graph_interrupt_v3.py tests/integration/test_improvement_review_workflow.py tests/integration/test_issue_lifecycle_acceptance.py tests/integration/test_issue_lifecycle_workflow.py tests/integration/test_cli_workflow_v2.py
git diff --cached --name-only
git diff --cached --check
git commit -m "feat(assurance): activate fuzz and performance contracts"
```

---

### Task 14: Document the Contracts and Run the Full Release Gate

**Files:**
- Modify: `docs/schemas.md`
- Modify: `scripts/packaging_smoke_test.sh`
- Verify: every production/test/fixture file from Tasks 1–13.

**Interfaces:**
- Documents: review contract, applicability/check matrix, graph topology, event v5, revision view, prefix recovery, profile snapshots, historical compile split, topology classification, and replay semantics v2.
- Verifies: packaging includes the updated workflow schema, execution contracts, six skills, and any new production module.

- [ ] **Step 1: Update schema and compatibility documentation**

Document these exact compatibility rules:

- new invocations are event schema v5 and require an assurance-profile snapshot;
- v4 is never upgraded or supplied a fabricated snapshot;
- legacy v4 Fuzz/Performance can report `not_wired` only after pinned definition integrity succeeds;
- core, packaged-current, and pinned-historical compilation are explicit entry points; no schema-content heuristic selects a weaker validator;
- new replay reports remain schema version `"2"` but use `counterfactual_plan_check_actions/v2`;
- replay semantics v1 and legacy specialty schema v1 remain readable;
- a revision view is mutable transport, while `manual_plan_revision.target_tree_id` and ledger lineage are evidence;
- runtime prefix recovery is explicit and must not be described as power-loss-atomic transaction rollback.
- v5 resumes bind a source-gate pair only when the interrupt has a real committed gate epoch; pairless non-gate improvement/issue resumes remain supported and cannot act as gate overrides.

- [ ] **Step 2: Run focused four-layer acceptance once more**

```bash
uv run pytest -q \
  tests/unit/artifacts/test_models_review_explore.py \
  tests/unit/artifacts/test_registry.py \
  tests/unit/verification/test_contract_round_trip.py \
  tests/unit/verification/test_layer_assurance_round_trip.py \
  tests/unit/workflow/graph/test_packaged_schema_compiles.py \
  tests/unit/workflow/graph/test_replay_schema.py \
  tests/unit/workflow/graph/test_replay_binding.py \
  tests/unit/workflow/graph/test_four_layer_replay.py \
  tests/integration/test_fuzz_performance_assurance_flow.py \
  tests/integration/test_graph_runtime_faults.py
```

- [ ] **Step 3: Run the complete CI gate**

Before running it, extend `scripts/packaging_smoke_test.sh` to import `assurance_agent.artifacts.policy_obligations` and `assurance_agent.workflow.graph.manual_revision` from the built wheel, load packaged `execution-contracts.yaml`, and assert all six Fuzz/Performance skills plus the updated workflow schema are present. The smoke test—not a manual statement—owns those packaging assertions.

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest
bash scripts/packaging_smoke_test.sh
```

Expected: all six commands exit zero. Do not claim completion from focused tests alone.

- [ ] **Step 4: Inspect packaged resources and staged scope**

```bash
uv run aa --version
git status --short
git diff --check
git diff --cached --name-only
```

Confirm no unrelated pre-existing worktree changes are staged. Confirm the wheel/smoke test resolves the new module, workflow schema, execution contracts, and all six modified skills.

- [ ] **Step 5: Commit documentation**

```bash
git add -f docs/schemas.md scripts/packaging_smoke_test.sh
git diff --cached --name-only
git diff --cached --check
git commit -m "docs: describe four-layer assurance replay contracts"
```

---

## Dependency and Parallelization Map

```text
Task 1 review-model capability (not active)
  -> Task 2 canonical fixtures (not active)
  -> Task 3 exact execution-contract preparation
  -> Task 4 schema/topology interfaces (current compiler unchanged)
  -> Task 5 immutable tree/revision-view primitives
  -> Task 6 v5 event/fold/profile-snapshot readers (new roots still v4)
  -> Task 7 manual-resume/decision-epoch runtime support (new roots still v4)
  -> Task 8 canonical gate fixtures only (packaged graph/policy consumers unchanged)
  -> Task 9 synthetic SIGKILL prefix recovery
  -> Task 10 pinned-only historical compiler
  -> Task 11 two-stage replay binding + evidence recovery
  -> Task 12 topology-driven replay semantics v2
  -> Task 13 atomic publication: skills + registry/profile + deferred registry + graph + current gate + v5
  -> Task 14 packaging/docs/full CI
```

The dependency chain is intentionally serial. With one shared worktree, even disjoint implementation tasks must not stage or commit in parallel. Read-only reviews may run concurrently; the integrator applies and commits one numbered task at a time.

## Plan Self-Review Checklist

- [x] Every section 6–14 acceptance criterion in the design spec maps to at least one task and one executable test.
- [x] Public type names are consistent across tasks: `ManualRevisionDef`, `TreeFileRevision`, `RevisionViewBinding`, `ManualRevisionTransition`, `HistoricalCompileContext`, `PinnedDefinitionRequest`, `ResolvedPinnedDefinition`, `LayerTopologySpec`, `PinnedLayerTopology`, `WorkflowSchemaOrigin`, and `LoadedWorkflowV2`.
- [x] Current compile and historical compile have different explicit entry paths; there is no `skip_validation=True` or equivalent escape hatch.
- [x] No replay path accepts current schema/catalog/contract roots.
- [x] Every new error condition uses the stable reason names from the spec or the existing `GraphDefinitionChanged` live-execution boundary.
- [x] No task stages unrelated dirty files.
- [x] Search the completed plan for unresolved authoring placeholders before execution:

```bash
rg -n 'T(O)DO|T(B)D|F(I)XME|same[ ]as|similar[ ]to|fill[ ]in[ ]later' docs/superpowers/plans/2026-07-31-fuzz-performance-assurance-wiring.md
```

Expected: no matches.
