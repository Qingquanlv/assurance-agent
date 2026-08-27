# Pure Graph Engine Phase 4 Assurance Capability Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract all in-scope Assurance business capabilities into six independently buildable, source-authenticated plugin wheels without assembling the final Assurance product, cutting over `aa`, or coupling the new wheels to the legacy runtime.

**Architecture:** Each wheel is a vertical deep module that owns its contracts, handlers, skills, prompts, personas, schemas, validators, durable effects, and resources. Wheels communicate only through upstream public `contracts` packages and canonical JSON artifacts, register solely through the Phase 2 `PluginProvider`/five-registry seam, and use Phase 3 `AgentRunRequest`/`AgentRunResult` values for provider work. A test-only composition proves dependency closure and adapter rebinding; Phase 5 remains responsible for the production product manifest, graph, bindings, and model configuration.

**Tech Stack:** Python 3.11, Pydantic v2, `graph-engine` plugin SPI and `RegistryPlatform`, `agent-runtime-contracts`, canonical JSON/YAML resources, asyncio task handlers, pytest, Ruff, Pyright, import-linter, uv workspaces, Hatchling wheels.

**Spec:** `docs/superpowers/specs/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction-design.md`

## Global Constraints

- Work only in `/Users/lvqingquan/agent/assurance-agent/.worktrees/pure-graph-engine-phase3-spec` on `codex/pure-graph-engine-phase3-spec`; preserve unrelated worktrees and the untracked `.superpowers/sdd/2026-08-21-pure-graph-engine-phase3-agent-runtime-adapters/` directory.
- The normative source is `docs/superpowers/specs/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction-design.md`; if this plan and the spec differ, stop and amend the plan before implementation.
- Ship exactly six production Assurance wheels: `assurance-intake`, `assurance-generation`, `assurance-execution`, `assurance-healing`, `assurance-quality`, and `assurance-improvement`.
- Do not create `assurance-contracts`, `assurance-common`, `assurance-models`, `assurance-skills`, `assurance-validators`, or a catch-all `assurance-capabilities` production wheel.
- Do not modify the public Phase 2 registry/plugin interfaces or Phase 3 agent-runtime contracts. A discovered interface gap requires a separate design amendment and toy-product proof before implementation resumes.
- Do not add a product manifest, production graph, public graph entrypoint, CLI cutover, benchmark cutover, old-invocation bridge, forwarding import, compatibility alias, or legacy resume adapter.
- New capability wheels must not import `assurance_agent`, `assurance_kernel`, `ProductHooks`, another wheel's `plugin`, `operations`, `validators`, `effects`, or private modules.
- Every created `__init__.py` is a narrow explicit public export surface for types/functions implemented in the same task; do not add empty placeholder packages or wildcard re-exports.
- Legacy packages must not import the new wheels. Legacy and new implementations are exercised independently by characterization tests and share no implementation module.
- Only explicit installed wheel/config sources contribute code or resources. No wheel scans the SUT, `sys.path`, ambient entry points, or arbitrary project directories for executable behavior.
- All public capability IDs use `assurance.<owner>.*`; each ID has one owner and no alias. Schema/resource/effect IDs are versioned with a `.v1`, `.v2`, or later suffix.
- Apply one ID grammar everywhere: schema file `<name>.vN.schema.json` → `assurance.<owner>.schema.<name>.vN`; skill `<name>` → `assurance.<owner>.skill.<name>.v1`; persona `<name>.md` → `assurance.<owner>.persona.<name>.v1`; prompt `<name>.md` → `assurance.<owner>.prompt.<name>.v1`; result contract `<name>.vN.schema.json` → `assurance.<owner>.result.<name>.vN`; policy `<name>.vN.json` → `assurance.<owner>.policy.<name>.vN`. Task, validator, and effect IDs are the exact values named by their owning task. Static declarations, live contributions, the ownership ledger, and tests must use the same strings.
- Cross-wheel Python imports are limited to `assurance_<upstream>.contracts` and its focused submodules. Cross-task authority is canonical JSON plus exact schema/resource digests, never Python object identity.
- Agent-driven work follows prepare → selected Phase 3 adapter → finalize. Capability wheels never import OpenCode/Cursor packages, choose an endpoint, read credentials, or persist provider session history.
- A prepare handler accepts one closed `binding_data` mapping containing `execution`, `request_policy_digest`, and `request_config_digest`; it returns one complete serialized `AgentRunRequest` and rejects fallback/candidate model expressions before dispatch.
- A finalize handler accepts one serialized `AgentRunResult`, authenticates its digest, validates the capability-owned semantic contract and workspace artifacts, then returns canonical business output or typed `invalid_output`.
- Durable mutations use Phase 2 `EffectRegistration`; every effect has an exact intent schema, receipt schema, idempotency key, apply behavior, reconcile behavior, and crash-cut tests.
- A business STOP is `TaskOutcome.stopped()` with an owner-qualified stable reason. Exceptions, missing files, invalid output, and indeterminate provider/effect state are never converted to STOP.
- The current Phase 1–3 focused baseline is `1369 passed, 1 skipped` for `packages/graph-engine/tests`, `packages/agent-runtime-contracts/tests`, `packages/agent-runtime-opencode/tests`, and `packages/agent-runtime-cursor/tests`. Every changed count must be explained in the task report.
- Use TDD for every implementation task: establish RED, implement the smallest complete behavior, run focused GREEN, run the task regression gate, write the task report, and commit one independently reviewable change.
- Run commands through `uv run`; Python is pinned to 3.11. Do not use the system Python.
- Each task report lives in `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/task-<N>-report.md` and records RED evidence, GREEN evidence, full gates, commit SHA, and any non-blocking concern. Reports remain intentionally ignored local evidence; only `ownership.yaml`, `phase5-handoff.md`, and `phase6-deletion.txt` are force-added when their tasks require committed evidence.
- Before a task commit, run `git diff --check`, focused Ruff/format/Pyright for changed files, the wheel's full tests, and the unchanged Phase 1–3 focused baseline when shared configuration or upstream contracts change.
- Before each wheel task commit, run the Task 2 tracer isolation CLI against the current tree: Tasks 3–4 use `--package assurance-intake --expect-entry-point intake`; Tasks 5–7 use `--package assurance-generation --expect-entry-point generation`; Tasks 8–9 use `--package assurance-execution --expect-entry-point execution`; Tasks 10–11 use `--package assurance-healing --expect-entry-point healing`; Tasks 12–14 use `--package assurance-quality --expect-entry-point quality`; Tasks 15–16 use `--package assurance-improvement --expect-entry-point improvement`. Task 19 separately repeats all six from committed `HEAD` and is the release-grade isolation authority.

## File Responsibility Map

```text
.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/
  ownership.yaml                       exact old-item → owner/disposition ledger
  task-<N>-report.md                   per-task RED/GREEN/gate evidence
  phase5-handoff.md                    selected IDs, schemas, resources, bindings, deletion list

tests/phase4/
  ownership.py                         strict ownership-ledger parser and inventory collectors
  conformance.py                       test-only provider/handler/validator/effect assertions
  agent_harness.py                     provider-neutral prepare/fake-adapter/finalize harness
  legacy_fixtures.py                   fixed old/new characterization inputs and canonical outputs
  fixtures/product.yaml                test-only six-wheel declarative product
  fixtures/bindings-opencode/plugin.yaml
  fixtures/bindings-cursor/plugin.yaml test-only logical adapter bindings
  test_ownership_ledger.py
  test_cross_wheel_contracts.py
  test_six_wheel_composition.py
  test_product_hooks_parity.py

packages/assurance-intake/assurance_intake/
  plugin.py                             one PluginProvider and complete contribution
  contracts/                            common primitives, case, intake, review contracts
  operations/                           request preparation and business finalization
  validators/                           case/review candidate invariants
  resources/                            schemas, prompts, personas, skills, result contracts
  plugin-declaration.json               authenticated static descriptor

packages/assurance-generation/assurance_generation/
  plugin.py                             generation PluginProvider
  contracts/                            plan/review/codegen/generated-file contracts
  operations/                           API/E2E/Fuzz/Performance prepare/finalize handlers
  validators/                           plan, mapping, generated-file validators
  resources/                            four family skill/prompt/persona/schema resources
  plugin-declaration.json

packages/assurance-execution/assurance_execution/
  plugin.py                             execution PluginProvider
  contracts/                            selection/manifest/result/evidence contracts
  operations/                           select, run, normalize handlers
  validators/                           closed-mapping and execution-evidence validators
  resources/                            execution/run skill and schema resources
  plugin-declaration.json

packages/assurance-healing/assurance_healing/
  plugin.py                             healing PluginProvider
  contracts/                            proposal, safety, allocation, repair/effect contracts
  operations/                           proposal/finalize, authority, safety, status handlers
  validators/                           test-tree/override/repair candidate validators
  effects/                              allocation, approval, apply handlers and reconciliation
  resources/                            fix/coverage-repair skills, policies, schemas, personas
  plugin-declaration.json

packages/assurance-quality/assurance_quality/
  plugin.py                             quality PluginProvider
  contracts/                            baseline/trace/coverage/issues/metrics/report contracts
  operations/                           deterministic metrics plus agent prepare/finalize handlers
  validators/                           trace, issue, report, coverage reference validators
  resources/                            inspect/report/issue/fact-baseline/dashboard resources
  plugin-declaration.json

packages/assurance-improvement/assurance_improvement/
  plugin.py                             improvement PluginProvider
  contracts/                            retro/improvement/review/delivery/promotion contracts
  operations/                           retro/review/delivery/archive handlers
  validators/                           candidate/review/delivery validators
  effects/                              delivery, promotion, archive, rollback effects
  resources/                            retro/reviewer/archive skills, schemas, personas
  plugin-declaration.json

scripts/
  assurance_capability_wheel_smoke_test.sh  committed-HEAD isolated wheel build/install test
  packaging_smoke_test.sh                  invokes the Phase 4 smoke without legacy fallback
```

## Canonical Wheel Dependency Graph

```text
1 ownership ─> 2 conformance
2 ─> 3 intake contracts ─> 4 intake behavior
4 ─> 5 generation contracts ─> 6 generation planning/review ─> 7 generation codegen
7 ─> 8 execution contracts ─> 9 execution behavior
9 ─> 10 healing contracts/policy ─> 11 healing effects/behavior
11 ─> 12 quality contracts ─> 13 quality metrics/issues ─> 14 quality agent/report behavior
14 ─> 15 improvement contracts ─> 16 improvement behavior/effects
16 ─> 17 cross-wheel closure ─> 18 ProductHooks parity
18 ─> 19 wheel isolation ─> 20 six-wheel rebinding
20 ─> 21 security/fault audit ─> 22 release gates and handoff
```

---

### Task 1: Freeze the ownership ledger, canonical IDs, and allowed dependency DAG

**Files:**
- Create: `tests/phase4/__init__.py`
- Create: `tests/phase4/ownership.py`
- Create: `tests/phase4/test_ownership_ledger.py`
- Create: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: test-only `OwnershipItem`, `OwnershipLedger`, `load_ownership_ledger(path: Path) -> OwnershipLedger`, `legacy_operation_ids()`, `legacy_skill_ids()`, `legacy_persona_ids()`, `legacy_validator_ids()`, `legacy_effect_kinds()`, `legacy_hook_fields()`, `legacy_artifact_types()`, and `legacy_runtime_resource_paths()`.
- Produces: the authoritative migration disposition for every current operation, skill, persona, validator, effect, hook, artifact model, and runtime resource.
- Consumes: the frozen Phase 4 spec and current legacy catalogs only; no production package is created in this task.
- Consumers: every later task updates its own ledger entries from `planned` to `verified` and supplies one verification node ID.

- [ ] **Step 1: Write the failing strict-ledger tests**

```python
def test_ledger_covers_every_legacy_operation_and_skill_exactly_once() -> None:
    ledger = load_ownership_ledger(OWNERSHIP_PATH)
    assert ledger.legacy_ids("operation") == legacy_operation_ids()
    assert ledger.legacy_ids("skill") == legacy_skill_ids()
    assert ledger.legacy_ids("persona") == legacy_persona_ids()
    assert ledger.legacy_ids("validator") == legacy_validator_ids()
    assert ledger.legacy_ids("effect") == legacy_effect_kinds()
    assert ledger.legacy_ids("hook") == legacy_hook_fields()
    assert ledger.legacy_ids("artifact") == legacy_artifact_types()
    assert ledger.legacy_ids("resource") == legacy_runtime_resource_paths()
    assert len(ledger.items) == len({(item.kind, item.legacy_id) for item in ledger.items})


def test_assurance_dependency_edges_are_exact_and_acyclic() -> None:
    ledger = load_ownership_ledger(OWNERSHIP_PATH)
    assert ledger.dependencies == {
        "assurance.intake": (),
        "assurance.generation": ("assurance.intake",),
        "assurance.execution": ("assurance.intake", "assurance.generation"),
        "assurance.healing": (
            "assurance.intake",
            "assurance.generation",
            "assurance.execution",
        ),
        "assurance.quality": (
            "assurance.intake",
            "assurance.generation",
            "assurance.execution",
            "assurance.healing",
        ),
        "assurance.improvement": (
            "assurance.intake",
            "assurance.generation",
            "assurance.execution",
            "assurance.healing",
            "assurance.quality",
        ),
    }
    assert ledger.topological_order() == (
        "assurance.intake",
        "assurance.generation",
        "assurance.execution",
        "assurance.healing",
        "assurance.quality",
        "assurance.improvement",
    )
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest tests/phase4/test_ownership_ledger.py -v`

Expected: collection fails because `tests.phase4.ownership` and `ownership.yaml` do not exist.

- [ ] **Step 3: Implement the strict test-only ledger model**

```python
@dataclass(frozen=True, slots=True)
class OwnershipItem:
    kind: Literal[
        "module", "callable", "operation", "skill", "persona", "schema",
        "artifact", "validator", "effect", "resource", "hook",
    ]
    legacy_id: str
    disposition: Literal["migrate", "replace_phase5", "retain_harness", "delete_phase6"]
    owner: str | None
    new_id: str | None
    status: Literal["planned", "verified"]
    verification: str | None


@dataclass(frozen=True, slots=True)
class OwnershipLedger:
    schema_version: Literal["1"]
    dependencies: Mapping[str, tuple[str, ...]]
    items: tuple[OwnershipItem, ...]

    def legacy_ids(self, kind: str) -> frozenset[str]:
        return frozenset(item.legacy_id for item in self.items if item.kind == kind)

    def topological_order(self) -> tuple[str, ...]:
        return _stable_topological_order(self.dependencies)
```

Parse YAML with `yaml.safe_load`, reject unknown top-level and item keys, reject non-canonical qualified `new_id` values, require one of the six owners exactly when `disposition == "migrate"`, and require `verification` exactly when `status == "verified"`.

- [ ] **Step 4: Populate the exact initial operation dispositions**

Use these owner groups in `ownership.yaml`:

```yaml
assurance.execution:
  - operation:run-tests
  - operation:run-tests-and-collect-pr-metrics
assurance.healing:
  - operation:allocate-healing-attempt
  - operation:fixer-authority-ready
  - operation:record-fixer-approval
  - operation:fixer-dispatch
  - operation:record-codegen-fix-apply
  - operation:combine-fixer-safety
  - operation:record-healing-status
  - operation:compute-coverage-repair-safety
  - operation:allocate-coverage-repair-attempt
  - operation:record-coverage-repair-status
assurance.quality:
  - operation:derive-plan-layer-applicability
  - operation:inspect
  - operation:generate-report
  - operation:materialize-trace-projection
  - operation:build-coverage-gap-signals
  - operation:materialize-trace-and-coverage-gaps
  - operation:materialize-minimum-coverage
  - operation:collect-diff-coverage
  - operation:compute-constraint-coverage
  - operation:compute-auth-matrix
  - operation:compute-journey-coverage
  - operation:compute-threshold-slack
  - operation:materialize-quarantine-projection
  - operation:materialize-c-layer-metrics
  - operation:collect-pr-metrics-batch
  - operation:probe-coverage-repair-need
  - operation:materialize-pr-metrics
  - operation:load-latest-pr-metrics
  - operation:run-mutation-sample
  - operation:compute-assertion-strength
  - operation:compute-baseline-drift
  - operation:collect-adversarial-yield
  - operation:aggregate-nightly-metrics
  - operation:evaluate-retrospective-shortboards
  - operation:run-nightly-metrics-pipeline
  - operation:collect-observations
  - operation:record-empty-issue-analysis
  - operation:record-issue-analysis-failure
  - operation:record-project-sync-pending
  - operation:reconcile-issues
  - operation:load-problem-review-context
  - operation:apply-problem-review
assurance.improvement:
  - operation:retro-collect-v3
  - operation:assemble-retro-context-v3
  - operation:drain-improvement-outbox
  - operation:finalize-retro-status
  - operation:record-retro-pipeline-failure
  - operation:retro-evidence-gap-fallback
  - operation:record-analysis-failed
  - operation:materialize-empty-retro-analysis
  - operation:reconcile-improvements
  - operation:load-review-subject
  - operation:validate-improvement-review-assessment
  - operation:apply-improvement-auto-review
  - operation:record-improvement-auto-review-error
  - operation:record-auto-review-orchestration-error
  - operation:select-current-retro-auto-review-items
  - operation:summarize-auto-review-batch
  - operation:load-improvement-review-context
  - operation:apply-improvement-review
  - operation:load-improvement-delivery
  - operation:evaluate-memory-improvement
  - operation:apply-memory-improvement
  - operation:rollback-memory-improvement
  - operation:export-change-improvement
  - operation:record-change-improvement-applied
  - operation:export-knowledge-improvement
  - operation:record-knowledge-improvement-applied
replace_phase5:
  - operation:no-op
  - operation:stop
  - operation:skill-registry-check
delete_phase6:
  - operation:retro-accept
```

Map skill IDs exactly as the spec: intake owns `aa-intake`, `aa-explore`, `aa-case-design`, `aa-case-reviewer`; generation owns `aa-api-plan`, `aa-api-plan-reviewer`, `aa-api-codegen`, `aa-api-codegen-fixer`, `aa-e2e-plan`, `aa-e2e-plan-reviewer`, `aa-e2e-codegen`, `aa-e2e-codegen-fixer`, `aa-fuzz-plan`, `aa-fuzz-plan-reviewer`, `aa-fuzz-codegen`, `aa-performance-plan`, `aa-performance-plan-reviewer`, and `aa-performance-codegen`; execution owns `aa-execute` and `aa-run`; healing owns `aa-fix-proposal` and `aa-coverage-repair`; quality owns `aa-fact-baseline`, `aa-inspect`, `aa-issue-analyzer`, `aa-issue-triage-advisor`, `aa-report-generator`, `aa-dashboard`; improvement owns `aa-retro`, `aa-retro-eval-analysis`, `aa-retro-issue-analysis`, `aa-retro-workflow-analysis`, `aa-improvement-reviewer`, and `aa-archive`; mark `aa-workflow` as `replace_phase5` and `writing-skills` as `retain_harness`.

Populate `new_id` deterministically rather than inventing aliases during extraction:

- a migrated legacy `operation:<slug>` becomes `assurance.<owner>.<slug>` with the suffix preserved byte-for-byte;
- a migrated legacy skill directory `<skill>` becomes resource ID `assurance.<owner>.skill.<skill>.v1`;
- `replace_phase5`, `retain_harness`, and `delete_phase6` entries have `new_id: null` until the owning later phase creates a replacement;
- every additional schema, validator, effect, resource, persona, artifact type, and hook row added by later tasks uses the exact ID declared in that task and is rejected if its owner prefix differs.

Seed exact legacy persona ownership: `aa-intake-host`, `aa-explorer`, and `aa-doc-author` → intake; `aa-test-author` and `aa-reviewer` → generation; `aa-reporter` → quality; `aa-archiver` → improvement. A later wheel that needs another reviewer/explorer authors a distinct owner-qualified persona resource; it does not claim or byte-copy the same legacy persona row.

Seed the six closed legacy precommit validators and their direct replacements:

```yaml
generated_files_candidate/v1: assurance.generation.validator.generated-files.v1
codegen_fix_candidate/v1: assurance.generation.validator.codegen-fix-candidate.v1
plan_mechanical_candidate/v1: assurance.generation.validator.plan-mechanical.v1
archive_integrity/v1: assurance.improvement.validator.archive-integrity.v1
problem_apply_candidate/v1: assurance.quality.validator.problem-apply.v1
cross_artifact_invariants/v1: assurance.quality.validator.cross-artifact.v1
```

Seed the three legacy effect kinds: `healing_allocation/v2` → `assurance.healing.effect.allocation.v2`, `fixer_proposal_approved/v1` → `assurance.healing.effect.proposal-approved.v1`, and `heal_record_apply/v2` → `assurance.healing.effect.heal-apply.v2`.

Seed all 18 `ProductHooks` fields listed in Task 18. `semantic_pins` is `delete_phase6` with no replacement ID; every other hook has the Task 18 owner and primary public seam. Where one hook expands into multiple ordinary registrations, the ledger names the primary seam and Task 18 verifies the complete replacement set.

- [ ] **Step 5: Add exact module-family inventory rules**

The test inventory must expand and record each Python file under the workflow roots, and must list each kernel artifact model explicitly. Do not assign all of `assurance_agent.verification` to one owner: planning/generation validation and quality measurement are different capabilities.

```python
MODULE_OWNER_ROOTS = {
    "assurance.intake": (
        "packages/assurance-kernel/assurance_kernel/artifacts/models/common.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/cases.py",
    ),
    "assurance.generation": (
        "packages/assurance-kernel/assurance_kernel/artifacts/models/assurance.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/codegen.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/generated_files.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/plan_checks.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/review.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/discovery.py",
        "assurance_agent/workflow/discovery",
    ),
    "assurance.execution": (
        "packages/assurance-kernel/assurance_kernel/artifacts/models/execution.py",
        "assurance_agent/workflow/execution",
    ),
    "assurance.healing": (
        "packages/assurance-kernel/assurance_kernel/artifacts/models/healing.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/healing_codegen.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/coverage_repair.py",
        "assurance_agent/workflow/healing",
    ),
    "assurance.quality": (
        "packages/assurance-kernel/assurance_kernel/artifacts/models/coverage_gaps.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/c_layer.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/explore.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/inspect.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/issue_events.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/issues.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/metrics.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/minimum_coverage.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/pr_metric_evidence.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/quarantine.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/report.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/sufficiency.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/trace.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/trace_sufficiency.py",
        "assurance_agent/workflow/issues",
        "assurance_agent/workflow/metrics",
        "assurance_agent/workflow/report",
        "assurance_agent/evidence",
    ),
    "assurance.improvement": (
        "packages/assurance-kernel/assurance_kernel/artifacts/models/improvement_outbox.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/improvement_review.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/improvements.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/declarations.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/promotion.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/retro_batch.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/retro_v3.py",
        "assurance_agent/workflow/improvements",
        "assurance_agent/retro",
    ),
}

GENERATION_VERIFICATION_FILES = (
    "assurance_agent/verification/applicability.py",
    "assurance_agent/verification/checks/assert_ideal.py",
    "assurance_agent/verification/checks/base.py",
    "assurance_agent/verification/checks/capability_keys.py",
    "assurance_agent/verification/checks/l1_path.py",
    "assurance_agent/verification/checks/registry.py",
    "assurance_agent/verification/checks/shared_factory.py",
    "assurance_agent/verification/contract_render.py",
    "assurance_agent/verification/generated_entries.py",
    "assurance_agent/verification/generated_files.py",
    "assurance_agent/verification/manifest.py",
    "assurance_agent/verification/oracle.py",
    "assurance_agent/verification/plan_checks.py",
    "assurance_agent/verification/profile_manifest.py",
    "assurance_agent/verification/profiles.py",
    "assurance_agent/verification/property_scan.py",
)

QUALITY_VERIFICATION_FILES = (
    "assurance_agent/verification/assertion_class.py",
    "assurance_agent/verification/baseline_history.py",
    "assurance_agent/verification/gate_state.py",
    "assurance_agent/verification/mutation_cache.py",
    "assurance_agent/verification/mutation_runner.py",
    "assurance_agent/verification/mutation_sampling.py",
    "assurance_agent/verification/promotion_gate.py",
    "assurance_agent/verification/replay.py",
)

NON_PHASE4_MODEL_DISPOSITIONS = {
    "packages/assurance-kernel/assurance_kernel/artifacts/models/data_knowledge.py": "replace_phase5",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/policy.py": "replace_phase5",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/state.py": "replace_phase5",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/eval_projection.py": "retain_harness",
}

CALLABLE_OWNER_OVERRIDES = {
    "assurance_kernel.artifacts.models.review.CaseSourceClaim": "assurance.intake",
    "assurance_kernel.artifacts.models.review.CaseSourceVerification": "assurance.intake",
    "assurance_kernel.artifacts.models.review.CaseMinimumCoverageReview": "assurance.intake",
    "assurance_kernel.artifacts.models.review.CaseReviewAuthoring": "assurance.intake",
}
```

The legacy `review.py` module is generation-owned, while the four explicitly named case-review callables are intake-owned and receive their own ledger rows. Record every `assurance_agent/artifacts/models/*.py` forwarding wrapper as `delete_phase6`, while the corresponding kernel source file receives the semantic owner above. Record the two artifact package `__init__.py` files and the legacy artifact-wide `batch_id.py`, `canonical.py`, `paths.py`, `registry.py`, and `validate.py` modules as `delete_phase6`; record artifact-wide `policy.py`, `policy_obligations.py`, and `repo_registry.py` as `replace_phase5`. Record `assurance_agent/verification/__init__.py` and `checks/__init__.py` as `delete_phase6`.

Reject overlaps after directory expansion. Fail if any current kernel artifact model, forwarding model wrapper, verification module, workflow module under a declared owner root, operation ID, or skill directory is absent. Record exact expanded files in YAML rather than storing globs.

`legacy_runtime_resource_paths()` enumerates every non-primary companion file under the current `skills/<skill-id>/` trees, every schema/example/template path referenced by the artifact registry or an owned operation, and every packaged prompt path referenced by a legacy skill. It excludes the primary `SKILL.md` and `assurance_agent/_resources/opencode/agents/*.md` files because those are already recorded as `skill` and `persona` rows. Assign each companion to its skill owner; assign schema/example/template bytes to the artifact-producing owner; assign `aa-workflow` companions to `replace_phase5` and `writing-skills` companions to `retain_harness`. The collector must resolve constant-based references and reject missing files, unreferenced executable resources, and one physical path assigned to two owners.

`legacy_artifact_types()` reads the current artifact registry's closed canonical type IDs, not Python filenames. Map each type to the owner of its registered model; use `replace_phase5` for data-knowledge/policy/state types and `retain_harness` for evaluation-only projections. Later tasks set each migrated artifact row's `new_id` to the exact owner schema ID and verification test.

- [ ] **Step 6: Run focused and static gates**

Run: `uv run pytest tests/phase4/test_ownership_ledger.py -v`

Run: `uv run ruff check tests/phase4`

Run: `uv run pyright tests/phase4`

Expected: all pass; every current in-scope module/callable, operation, skill, and non-Phase-4 disposition is accounted for and the dependency order is exact.

- [ ] **Step 7: Write the task report and commit**

```bash
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml
git add tests/phase4 pyproject.toml
git commit -m "test(assurance): freeze phase 4 capability ownership"
```

---

### Task 2: Add test-only plugin, resource, handler, validator, and effect conformance

**Files:**
- Create: `tests/phase4/conformance.py`
- Create: `tests/phase4/agent_harness.py`
- Create: `tests/phase4/wheel_isolation.py`
- Create: `tests/phase4/test_conformance_helpers.py`
- Create: `tests/phase4/fixtures/minimal-product.yaml`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml`

**Interfaces:**
- Produces: `PluginExpectation`, `assert_plugin_conforms(provider, expectation)`, `execute_task(handler, request, workspace)`, `assert_validator_rejects(...)`, `assert_effect_idempotent(...)`, `AgentSkillHarness`, `FakeAgentAdapter`, and CLI `python -m tests.phase4.wheel_isolation --package <name> --expect-entry-point <name>`.
- Consumes: only public `graph_engine`, `graph_engine.composition`, `graph_engine.plugin_api`, and `agent_runtime_contracts` interfaces plus Toy A for self-tests.
- Consumers: Tasks 3–21. This module remains test-only and is never packaged by an Assurance wheel.

- [ ] **Step 1: Write failing helper self-tests against Toy A and an invalid provider**

```python
def test_plugin_conformance_accepts_toy_a() -> None:
    assert_plugin_conforms(
        ToyAPlugin(),
        PluginExpectation(plugin_id="toy.a", dependencies=(), id_prefix="toy.a."),
    )


def test_plugin_conformance_rejects_undeclared_resource() -> None:
    with pytest.raises(AssertionError, match="descriptor/contribution mismatch"):
        assert_plugin_conforms(
            _ProviderWithUndeclaredResource(),
            PluginExpectation(plugin_id="test.bad", dependencies=(), id_prefix="test.bad."),
        )
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest tests/phase4/test_conformance_helpers.py -v`

Expected: collection fails because `tests.phase4.conformance` and `agent_harness` do not exist.

- [ ] **Step 3: Implement the public-interface conformance helpers**

```python
@dataclass(frozen=True, slots=True)
class PluginExpectation:
    plugin_id: str
    dependencies: tuple[str, ...]
    id_prefix: str


def assert_plugin_conforms(provider: PluginProvider, expected: PluginExpectation) -> None:
    descriptor = provider.descriptor()
    contribution = provider.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    validate_contribution(descriptor, contribution)
    assert descriptor.plugin_id == expected.plugin_id
    assert tuple(item.plugin_id for item in descriptor.dependencies) == expected.dependencies
    ids = _all_contribution_ids(contribution)
    assert ids
    assert all(item.startswith(expected.id_prefix) for item in ids)
    assert tuple(sorted(ids)) == tuple(ids)
    _assert_canonical_resource_and_schema_bytes(contribution)
```

`execute_task()` creates a real temporary workspace, an `InvocationMetadata`, a `TaskRequest`, and a `TaskContext` with deterministic heartbeat/cancel callbacks. It awaits only the public `TaskHandler.execute()` method and returns the outcome plus final workspace bytes.

- [ ] **Step 4: Implement the provider-neutral agent harness**

```python
@dataclass(frozen=True, slots=True)
class AgentSkillHarness:
    prepare: TaskHandler
    finalize: TaskHandler

    async def run(
        self,
        business_input: JSONValue,
        binding_data: JSONValue,
        structured_result: JSONValue,
    ) -> TaskOutcome:
        prepared = await _execute(self.prepare, business_input, binding_data=binding_data)
        request = AgentRunRequest.model_validate(prepared.output)
        adapter_result = FakeAgentAdapter(structured_result).execute_request(request)
        return await _execute(
            self.finalize,
            {"agent_result": adapter_result.model_dump(mode="json")},
        )
```

`FakeAgentAdapter.execute_request()` must create `AgentRunResult` with canonical `result_digest`, fixed evidence digest, and caller-selected `adapter_id`; it records the exact `AgentRunRequest.canonical_bytes()` for rebinding assertions.

- [ ] **Step 5: Add effect and validator assertions**

`assert_validator_rejects()` constructs a real `CandidateWriteSet` and `ValidationContext`, calls `validate()`, and checks `accepted is False` plus an exact stable reason. `assert_effect_idempotent()` calls `apply()` twice with one key, asserts byte-equal receipts, then calls `reconcile()` and requires the same receipt.

Implement `wheel_isolation.py` as a test-only current-tree tracer gate. It creates a private temp root, builds `graph-engine`, `agent-runtime-contracts`, the requested wheel, and its declared upstream local wheels with `uv build --offline --wheel --no-sources`; installs the local project distributions only from those archives into a new venv with `uv pip install --offline --python <venv-python> --find-links <wheelhouse>` while third-party dependencies resolve only from the pre-populated uv offline cache; then imports the one expected `graph_engine.plugins` entry point, checks static/live declaration equality, and asserts `assurance_agent`/`assurance_kernel` are absent. The package argument is a closed choice of Toy A for the helper self-test plus the six Phase 4 distribution names, not an arbitrary shell string. The installed metadata check rejects editable/direct-path local distributions, so the cache cannot hide a local source import.

- [ ] **Step 6: Run focused and Phase 1–3 regression gates**

Run: `uv run pytest tests/phase4/test_conformance_helpers.py -v`

Run: `uv run python -m tests.phase4.wheel_isolation --package graph-engine-toy-a --expect-entry-point toy-a`

Run: `uv run pytest packages/graph-engine/tests packages/agent-runtime-contracts/tests packages/agent-runtime-opencode/tests packages/agent-runtime-cursor/tests -q`

Expected: helper tests and Toy A isolated load pass; Phase 1–3 remains `1369 passed, 1 skipped`.

- [ ] **Step 7: Write the task report and commit**

```bash
git add tests/phase4
git commit -m "test(assurance): add phase 4 plugin conformance"
```

---

### Task 3: Create `assurance-intake` contracts, schemas, provider, and wheel boundary

**Files:**
- Create: `packages/assurance-intake/pyproject.toml`
- Create: `packages/assurance-intake/assurance_intake/__init__.py`
- Create: `packages/assurance-intake/assurance_intake/plugin.py`
- Create: `packages/assurance-intake/assurance_intake/contracts/__init__.py`
- Create: `packages/assurance-intake/assurance_intake/contracts/common.py`
- Create: `packages/assurance-intake/assurance_intake/contracts/cases.py`
- Create: `packages/assurance-intake/assurance_intake/contracts/review.py`
- Create: `packages/assurance-intake/assurance_intake/resource_loader.py`
- Create: `packages/assurance-intake/assurance_intake/plugin-declaration.json`
- Create: `packages/assurance-intake/assurance_intake/resources/schemas/case.v1.schema.json`
- Create: `packages/assurance-intake/assurance_intake/resources/schemas/case-authoring.v1.schema.json`
- Create: `packages/assurance-intake/assurance_intake/resources/schemas/qa-change.v1.schema.json`
- Create: `packages/assurance-intake/assurance_intake/resources/schemas/case-review.v1.schema.json`
- Create: `packages/assurance-intake/tests/test_contracts.py`
- Create: `packages/assurance-intake/tests/test_plugin.py`
- Modify: `pyproject.toml`
- Modify: `.importlinter`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml`

**Interfaces:**
- Produces: public `assurance_intake.contracts` models `CaseRisk`, `CaseEntry`, `CaseEntryAuthoring`, `CaseYaml`, `CaseYamlAuthoring`, `QaChange`, `QaCaseTarget`, `QaTargets`, `QaWorkflow`, `QaApproval`, `QaYaml`, `CaseReviewFindingV1`, and `CaseReviewResultV1`.
- Produces: `IntakePlugin` with plugin ID `assurance.intake`, version `0.1.0`, engine API `1.0`, and initially schema/resource contributions only.
- Produces schemas: `assurance.intake.schema.case.v1`, `assurance.intake.schema.case-authoring.v1`, `assurance.intake.schema.qa-change.v1`, and `assurance.intake.schema.case-review.v1`.
- Consumers: Task 4 and every downstream wheel's public contract imports.

- [ ] **Step 1: Write failing contract-isolation and canonical-schema tests**

```python
def test_case_authoring_rejects_fake_capability_leaf() -> None:
    raw = load_fixture("case-authoring-invalid-capability.yaml")
    with pytest.raises(ValidationError, match="capability key is not a declared typed leaf"):
        CaseYamlAuthoring.model_validate(raw, context={"capability_leafs": VALID_LEAFS})


def test_intake_schema_bytes_equal_model_schema() -> None:
    assert schema_bytes("assurance.intake.schema.case-authoring.v1") == canonical_json_bytes(
        CaseYamlAuthoring.model_json_schema()
    )


def test_intake_imports_no_legacy_package() -> None:
    assert forbidden_imports("assurance_intake") == set()
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/assurance-intake/tests -v`

Expected: collection fails because the distribution and contracts do not exist.

- [ ] **Step 3: Port the exact intake-owned models without legacy imports**

Copy the implementations, validators, field aliases, and canonical behavior of the named classes from:

```text
packages/assurance-kernel/assurance_kernel/artifacts/models/common.py
packages/assurance-kernel/assurance_kernel/artifacts/models/cases.py
packages/assurance-kernel/assurance_kernel/artifacts/models/review.py
```

Move only case-review classes from `review.py`; generation review classes move in Task 5. Replace imports from `assurance_kernel.artifacts.models.common` with `assurance_intake.contracts.common`. Do not import or re-export a legacy module.

Add exact typed-leaf validation to the authoring contract: the Pydantic validation context must contain `capability_leafs: frozenset[str]`, and every authored `trace` capability key must be an exact member. A prefix match is invalid.

- [ ] **Step 4: Create canonical schemas and resource loader**

```python
def resource_bytes(relative_path: str) -> bytes:
    path = PurePosixPath(relative_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("resource path must be canonical and relative")
    return files("assurance_intake").joinpath("resources", *path.parts).read_bytes()
```

Store canonical JSON schema bytes in the four exact files and test them against `model_json_schema()`. The provider contributes those bytes with media type `application/schema+json`.

- [ ] **Step 5: Create the strict provider and static declaration**

```python
class IntakePlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=INTAKE_SOURCE,
            plugin_id="assurance.intake",
            plugin_version="0.1.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=(),
            commit_validators=(),
            schemas=INTAKE_SCHEMA_IDS,
            resources=(),
        )

    @staticmethod
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginContribution(schemas=_schema_contributions())
```

The static declaration must exactly equal the live descriptor and identify `assurance-intake`, entry point name `intake`, value `assurance_intake.plugin:IntakePlugin`, declaration path `assurance_intake/plugin-declaration.json`, and import roots `[""]`.

- [ ] **Step 6: Register the wheel and enforce the first import firewall**

Add `packages/assurance-intake` to workspace members, uv sources, dev dependencies, pytest paths, and Pyright includes. Add `assurance_intake` to import-linter roots and a forbidden contract against `assurance_agent` and `assurance_kernel`.

- [ ] **Step 7: Run focused, wheel, and Phase 1–3 gates**

Run: `uv run pytest packages/assurance-intake/tests -q`

Run: `uv run ruff check packages/assurance-intake tests/phase4`

Run: `uv run pyright packages/assurance-intake`

Run: `uv run lint-imports`

Run: `uv run pytest packages/graph-engine/tests packages/agent-runtime-contracts/tests packages/agent-runtime-opencode/tests packages/agent-runtime-cursor/tests -q`

Expected: intake tests pass; Phase 1–3 remains `1369 passed, 1 skipped`.

- [ ] **Step 8: Mark contract entries verified, report, and commit**

```bash
git add packages/assurance-intake pyproject.toml uv.lock .importlinter
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml
git commit -m "feat(assurance-intake): add authenticated intake contracts"
```

---

### Task 4: Add intake skill resources, prepare/finalize handlers, and case validators

**Files:**
- Create: `packages/assurance-intake/assurance_intake/contracts/agent.py`
- Create: `packages/assurance-intake/assurance_intake/operations/__init__.py`
- Create: `packages/assurance-intake/assurance_intake/operations/agent_skills.py`
- Create: `packages/assurance-intake/assurance_intake/operations/finalize.py`
- Create: `packages/assurance-intake/assurance_intake/validators/__init__.py`
- Create: `packages/assurance-intake/assurance_intake/validators/cases.py`
- Create: `packages/assurance-intake/assurance_intake/resources/skills/aa-intake/SKILL.md`
- Create: `packages/assurance-intake/assurance_intake/resources/skills/aa-explore/SKILL.md`
- Create: `packages/assurance-intake/assurance_intake/resources/skills/aa-case-design/SKILL.md`
- Create: `packages/assurance-intake/assurance_intake/resources/skills/aa-case-design/case-delta-reviewer-prompt.md`
- Create: `packages/assurance-intake/assurance_intake/resources/skills/aa-case-design/visual-companion.md`
- Create: `packages/assurance-intake/assurance_intake/resources/skills/aa-case-reviewer/SKILL.md`
- Create: `packages/assurance-intake/assurance_intake/resources/prompts/intake.md`
- Create: `packages/assurance-intake/assurance_intake/resources/prompts/explore.md`
- Create: `packages/assurance-intake/assurance_intake/resources/prompts/case-design.md`
- Create: `packages/assurance-intake/assurance_intake/resources/prompts/case-review.md`
- Create: `packages/assurance-intake/assurance_intake/resources/personas/intake-host.md`
- Create: `packages/assurance-intake/assurance_intake/resources/personas/explorer.md`
- Create: `packages/assurance-intake/assurance_intake/resources/personas/doc-author.md`
- Create: `packages/assurance-intake/assurance_intake/resources/personas/reviewer.md`
- Create: `packages/assurance-intake/assurance_intake/resources/result-contracts/intake.v1.schema.json`
- Create: `packages/assurance-intake/assurance_intake/resources/result-contracts/explore.v1.schema.json`
- Create: `packages/assurance-intake/assurance_intake/resources/result-contracts/case-design.v1.schema.json`
- Create: `packages/assurance-intake/assurance_intake/resources/result-contracts/case-review.v1.schema.json`
- Modify: `packages/assurance-intake/assurance_intake/plugin.py`
- Modify: `packages/assurance-intake/assurance_intake/plugin-declaration.json`
- Create: `packages/assurance-intake/tests/test_agent_skills.py`
- Create: `packages/assurance-intake/tests/test_validators.py`
- Create: `packages/assurance-intake/tests/test_legacy_characterization.py`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml`

**Interfaces:**
- Produces direct task handlers: `assurance.intake.intake.prepare`, `assurance.intake.intake.finalize`, `assurance.intake.explore.prepare`, `assurance.intake.explore.finalize`, `assurance.intake.case-design.prepare`, `assurance.intake.case-design.finalize`, `assurance.intake.case-review.prepare`, and `assurance.intake.case-review.finalize`.
- Produces validators: `assurance.intake.validator.case-candidate.v1` and `assurance.intake.validator.case-references.v1`.
- Produces private request models in `contracts/agent.py`: `AgentBindingDataV1`, `IntakeInputV1`, `ExploreInputV1`, `CaseDesignInputV1`, `CaseReviewInputV1`, and `AgentFinalizeInputV1`.
- Consumes: Task 3 contracts and `agent_runtime_contracts`; no provider adapter package.
- Consumers: Phase 5 graphs and Task 20's test-only bindings.

- [ ] **Step 1: Write failing request-determinism, semantic-finalize, and validator tests**

```python
@pytest.mark.asyncio
async def test_case_design_prepare_is_canonical_and_provider_neutral(tmp_path: Path) -> None:
    first = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, BINDING, tmp_path)
    second = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, BINDING, tmp_path)
    assert AgentRunRequest.model_validate(first.output).canonical_bytes() == (
        AgentRunRequest.model_validate(second.output).canonical_bytes()
    )


@pytest.mark.asyncio
async def test_case_review_finalize_rejects_nonexistent_leaf(tmp_path: Path) -> None:
    result = fake_agent_result({"status": "pass", "required_capabilities": ["entities.fake"]})
    outcome = await run_finalize(CaseReviewFinalizeHandler(), result, tmp_path)
    assert outcome.failure == TaskFailure(
        kind="invalid_output",
        message="case review references unknown capability leaf: entities.fake",
        retryable=True,
    )


def test_case_validator_rejects_tree_outside_case_paths() -> None:
    result = CaseCandidateValidator().validate(candidate_with("src/app.py"), validation_context())
    assert result == ValidationResult(
        accepted=False,
        reason="intake candidate may write only change and cases paths",
    )
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/assurance-intake/tests/test_agent_skills.py packages/assurance-intake/tests/test_validators.py -v`

Expected: failures show the handlers, validators, and resources are absent.

- [ ] **Step 3: Port and normalize the four exact skill resource trees**

Copy the authored bytes from the four legacy skill directories into the wheel. Replace provider-specific front matter, OpenCode agent names, implicit model selection, global skill lookup, and old workflow instructions with capability-owned instruction text. Preserve business obligations, output paths, and review rules.

Port the intake-owned `aa-intake-host`, `aa-explorer`, and `aa-doc-author` persona obligations into the first three resources. Author `reviewer.md` as a new intake case-review persona from the case-review skill's business rules; do not copy or claim generation-owned legacy `aa-reviewer`. Name every resource with an owner-qualified ID. No file path or provider agent name appears in the public task input.

- [ ] **Step 4: Implement deterministic request preparation**

Define the private request models before the handlers. `AgentBindingDataV1` has exactly `execution: FrozenExecutionSelection`, `request_policy_digest: str`, and `request_config_digest: str`. The four prepare inputs have exactly the capability-specific canonical business value, `capability_leafs: tuple[str, ...]`, and the artifact references required by that skill; `AgentFinalizeInputV1` has exactly `agent_result: AgentRunResult`, `capability_leafs: tuple[str, ...]`, and `artifact_paths: tuple[str, ...]`. All models use `extra="forbid"`, sorted unique tuples, canonical relative artifact paths, and bare lowercase SHA-256 validation where applicable.

```python
class CaseDesignPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        business = CaseDesignInputV1.model_validate(request.input)
        binding = AgentBindingDataV1.model_validate(request.binding_data)
        agent_request = AgentRunRequest(
            instructions=(
                InstructionPart.text("text/plain", resource_text(SKILL_RESOURCE)),
                InstructionPart.text("text/plain", resource_text(PERSONA_RESOURCE)),
                InstructionPart.from_json(business.model_dump(mode="json")),
            ),
            result_contract=result_contract(CASE_AUTHORING_SCHEMA_ID),
            execution=binding.execution,
            request_policy_digest=binding.request_policy_digest,
            request_config_digest=binding.request_config_digest,
        )
        return TaskOutcome.succeeded(agent_request.model_dump(mode="json"))
```

Use the same exact three-part ordering for all four prepare handlers: skill, persona, canonical business input. Reject missing/extra binding fields and routing markers through `FrozenExecutionSelection`.

- [ ] **Step 5: Implement semantic finalizers and exact capability-leaf validation**

Each finalize handler validates `AgentRunResult`, then its `structured_result` against the named contract. Case design requires at least one added/modified case and exact leaf membership. Case review requires canonical findings, source verification, minimum coverage review, and exact leaf membership. Intake/explore finalizers authenticate declared output files and return canonical artifact references plus digests.

Return `TaskOutcome.failed("invalid_output", exact_message, retryable=True)` for model-authored semantic invalidity. Return `invalid_input` and `retryable=False` for malformed caller input or missing locked configuration.

- [ ] **Step 6: Implement commit validators over candidate bytes**

`CaseCandidateValidator` permits only owner-declared change/case paths, rejects symlink/absolute/traversal paths, parses candidate YAML, validates `CaseYamlAuthoring` with the exact frozen leaf set, and rejects unlisted files. `CaseReferenceValidator` verifies every related case and target path resolves inside the candidate tree.

- [ ] **Step 7: Update provider/declaration with exact complete contributions**

Register the eight handlers, two validators, four business schemas, all skill/persona/prompt/result resources, and no effects/bindings. Sort IDs canonically and require the static declaration to equal the live descriptor.

- [ ] **Step 8: Add independent legacy characterization**

Run the legacy case model/validation path and the new intake path separately on fixed valid and invalid fixtures. Compare canonical case YAML, gate decision, exact capability-key rejection, and candidate write set. Do not import a legacy function from production intake code.

- [ ] **Step 9: Run focused and wheel gates**

Run: `uv run pytest packages/assurance-intake/tests tests/phase4/test_conformance_helpers.py -q`

Run: `uv run ruff check packages/assurance-intake tests/phase4`

Run: `uv run ruff format --check packages/assurance-intake tests/phase4`

Run: `uv run pyright packages/assurance-intake`

Run: `uv run lint-imports`

Expected: all intake tests and import contracts pass.

- [ ] **Step 10: Mark intake items verified, report, and commit**

```bash
git add packages/assurance-intake tests/phase4
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml
git commit -m "feat(assurance-intake): extract intake capabilities"
```

---
### Task 5: Create `assurance-generation` contracts, schemas, provider, and wheel boundary

**Files:**
- Create: `packages/assurance-generation/pyproject.toml`
- Create: `packages/assurance-generation/assurance_generation/__init__.py`
- Create: `packages/assurance-generation/assurance_generation/plugin.py`
- Create: `packages/assurance-generation/assurance_generation/contracts/__init__.py`
- Create: `packages/assurance-generation/assurance_generation/contracts/families.py`
- Create: `packages/assurance-generation/assurance_generation/contracts/plans.py`
- Create: `packages/assurance-generation/assurance_generation/contracts/reviews.py`
- Create: `packages/assurance-generation/assurance_generation/contracts/codegen.py`
- Create: `packages/assurance-generation/assurance_generation/contracts/generated_files.py`
- Create: `packages/assurance-generation/assurance_generation/contracts/discovery.py`
- Create: `packages/assurance-generation/assurance_generation/resource_loader.py`
- Create: `packages/assurance-generation/assurance_generation/plugin-declaration.json`
- Create: `packages/assurance-generation/assurance_generation/resources/schemas/plan-check.v1.schema.json`
- Create: `packages/assurance-generation/assurance_generation/resources/schemas/plan-review.v1.schema.json`
- Create: `packages/assurance-generation/assurance_generation/resources/schemas/generated-files.v1.schema.json`
- Create: `packages/assurance-generation/assurance_generation/resources/schemas/codegen-mapping.v1.schema.json`
- Create: `packages/assurance-generation/assurance_generation/resources/schemas/discovery-campaign.v1.schema.json`
- Create: `packages/assurance-generation/tests/test_contracts.py`
- Create: `packages/assurance-generation/tests/test_plugin.py`
- Modify: `pyproject.toml`
- Modify: `.importlinter`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml`

**Interfaces:**
- Produces public family primitives `LayerName`, `CaseType`, `PlanCheckId`, and `KNOWN_PLAN_CHECK_IDS`, plus contracts `Finding`, `LayerApplicability`, `CheckEvidence`, `PlanCheckDocument`, `ReviewFinding`, `PlanReview`, `PlanReviewAuthoring`, `CodegenGeneratedFileAuthoring`, `CodegenGeneratedFilesAuthoring`, `CodegenMappingEntry`, `CodegenMapping`, `GeneratedFileEntryV1`, `GeneratedFilesV1`, four family-specific generated-file models, `CampaignSpec`, `Counterexample`, and `CampaignResult`.
- Produces plugin `assurance.generation` version `0.1.0`, dependent on `assurance.intake ==0.1.0`.
- Produces five exact schema IDs matching the five schema filenames.
- Consumes: only `graph-engine`, `agent-runtime-contracts`, and `assurance_intake.contracts`.
- Consumers: Tasks 6–9 and all downstream wheels.

- [ ] **Step 1: Write failing contract, dependency, and exact-key tests**

```python
def test_generation_descriptor_declares_only_intake_dependency() -> None:
    descriptor = GenerationPlugin.descriptor()
    assert descriptor.dependencies == (PluginDependency("assurance.intake", "==0.1.0"),)


def test_plan_review_rejects_prefix_valid_but_unknown_leaf() -> None:
    raw = valid_plan_review(required_capabilities=["capabilities.adapters.missing"])
    with pytest.raises(ValidationError, match="unknown capability leaf"):
        PlanReviewAuthoring.model_validate(raw, context={"capability_leafs": VALID_LEAFS})


def test_generation_contracts_import_only_intake_contracts() -> None:
    assert forbidden_generation_imports() == set()
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/assurance-generation/tests -v`

Expected: collection fails because the generation wheel does not exist.

- [ ] **Step 3: Port the exact generation-owned contracts**

Port named models and validators from these legacy files without importing them:

```text
packages/assurance-kernel/assurance_kernel/artifacts/models/plan_checks.py
packages/assurance-kernel/assurance_kernel/artifacts/models/assurance.py
packages/assurance-kernel/assurance_kernel/artifacts/models/review.py
packages/assurance-kernel/assurance_kernel/artifacts/models/codegen.py
packages/assurance-kernel/assurance_kernel/artifacts/models/generated_files.py
packages/assurance-kernel/assurance_kernel/artifacts/models/discovery.py
```

Import shared IDs/risk/case types only from `assurance_intake.contracts`. Keep case-review-only models in intake. Add exact typed-leaf membership to every `required_capabilities`, mapping capability, and plan leaf field. Preserve canonical file paths and strict family discriminators for API, E2E, Fuzz, and Performance.

- [ ] **Step 4: Create and authenticate canonical schema resources**

Generate the five checked-in canonical schema files from their owning public models. Tests compare exact bytes with `canonical_json_bytes(model.model_json_schema())`; runtime loads checked-in bytes and never generates a schema dynamically.

- [ ] **Step 5: Add provider and static declaration**

`GenerationPlugin.descriptor()` initially declares no handlers/validators/effects/bindings, the five schemas, and the exact intake dependency. Its source is distribution `assurance-generation`, entry point name `generation`, value `assurance_generation.plugin:GenerationPlugin`, declaration path `assurance_generation/plugin-declaration.json`, import roots `[""]`.

- [ ] **Step 6: Register workspace/package/import boundaries**

Add the wheel to root uv workspace, uv sources, dev dependencies, pytest paths, and Pyright includes. Add `assurance_generation` to import-linter roots with:

```text
assurance_generation may import graph_engine, agent_runtime_contracts, assurance_intake.contracts
assurance_generation must not import assurance_agent, assurance_kernel, or non-contract assurance_intake modules
```

- [ ] **Step 7: Run focused and upstream gates**

Run: `uv run pytest packages/assurance-intake/tests packages/assurance-generation/tests -q`

Run: `uv run ruff check packages/assurance-generation`

Run: `uv run pyright packages/assurance-generation`

Run: `uv run lint-imports`

Expected: intake and generation contract/provider tests pass.

- [ ] **Step 8: Update ownership evidence and commit**

```bash
git add packages/assurance-generation pyproject.toml uv.lock .importlinter
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml
git commit -m "feat(assurance-generation): add generation contracts"
```

---

### Task 6: Extract API/E2E/Fuzz/Performance planning and review capabilities

**Files:**
- Create: `packages/assurance-generation/assurance_generation/contracts/agent.py`
- Create: `packages/assurance-generation/assurance_generation/operations/__init__.py`
- Create: `packages/assurance-generation/assurance_generation/operations/planning.py`
- Create: `packages/assurance-generation/assurance_generation/operations/review.py`
- Create: `packages/assurance-generation/assurance_generation/validators/__init__.py`
- Create: `packages/assurance-generation/assurance_generation/validators/plans.py`
- Create: `packages/assurance-generation/assurance_generation/resources/skills/aa-api-plan/SKILL.md`
- Create: `packages/assurance-generation/assurance_generation/resources/skills/aa-api-plan-reviewer/SKILL.md`
- Create: `packages/assurance-generation/assurance_generation/resources/skills/aa-e2e-plan/SKILL.md`
- Create: `packages/assurance-generation/assurance_generation/resources/skills/aa-e2e-plan-reviewer/SKILL.md`
- Create: `packages/assurance-generation/assurance_generation/resources/skills/aa-fuzz-plan/SKILL.md`
- Create: `packages/assurance-generation/assurance_generation/resources/skills/aa-fuzz-plan-reviewer/SKILL.md`
- Create: `packages/assurance-generation/assurance_generation/resources/skills/aa-performance-plan/SKILL.md`
- Create: `packages/assurance-generation/assurance_generation/resources/skills/aa-performance-plan-reviewer/SKILL.md`
- Create: `packages/assurance-generation/assurance_generation/resources/personas/test-author.md`
- Create: `packages/assurance-generation/assurance_generation/resources/personas/reviewer.md`
- Create: `packages/assurance-generation/assurance_generation/resources/result-contracts/plan.v1.schema.json`
- Create: `packages/assurance-generation/assurance_generation/resources/result-contracts/plan-review.v1.schema.json`
- Modify: `packages/assurance-generation/assurance_generation/plugin.py`
- Modify: `packages/assurance-generation/assurance_generation/plugin-declaration.json`
- Create: `packages/assurance-generation/tests/test_planning.py`
- Create: `packages/assurance-generation/tests/test_plan_review.py`
- Create: `packages/assurance-generation/tests/test_plan_validator.py`
- Create: `packages/assurance-generation/tests/test_planning_characterization.py`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml`

**Interfaces:**
- Produces 16 direct handlers: `assurance.generation.<family>.plan.prepare`, `.plan.finalize`, `.plan-review.prepare`, and `.plan-review.finalize` for `api`, `e2e`, `fuzz`, and `performance`.
- Produces validators `assurance.generation.validator.api-plan.v1`, `e2e-plan.v1`, `fuzz-plan.v1`, `performance-plan.v1`, and `plan-mechanical.v1`.
- Consumes exact reviewed cases and capability leaf sets from intake contracts/resources.
- Consumers: Task 7 codegen and Phase 5 graph assembly.

- [ ] **Step 1: Write failing four-family parametrized RED tests**

```python
@pytest.mark.parametrize("family", ("api", "e2e", "fuzz", "performance"))
@pytest.mark.asyncio
async def test_plan_prepare_is_deterministic_for_every_family(family: str, tmp_path: Path) -> None:
    handler = planning_handler(family, "prepare")
    first = await execute_task(handler, plan_input(family), tmp_path, binding_data=BINDING)
    second = await execute_task(handler, plan_input(family), tmp_path, binding_data=BINDING)
    assert canonical_json_bytes(first.output) == canonical_json_bytes(second.output)


@pytest.mark.parametrize("family", ("api", "e2e", "fuzz", "performance"))
@pytest.mark.asyncio
async def test_plan_review_finalize_rejects_unknown_leaf(family: str, tmp_path: Path) -> None:
    result = fake_agent_result(review_result(family, leaf="auth.fake"))
    outcome = await execute_task(review_finalize_handler(family), result, tmp_path)
    assert outcome.status == "failed"
    assert outcome.failure is not None and outcome.failure.kind == "invalid_output"
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/assurance-generation/tests/test_planning.py packages/assurance-generation/tests/test_plan_review.py -v`

Expected: failures show the family handlers and skill resources are absent.

- [ ] **Step 3: Port and normalize all eight planning/review skills**

Copy the exact legacy skill content into the named wheel resources. Port generation-owned legacy `aa-test-author` and `aa-reviewer` business persona obligations into `test-author.md` and `reviewer.md`. Remove OpenCode-specific agent declarations, model defaults, ambient skill paths, and whole-workflow navigation. Keep family-specific business obligations and output contracts. Planning uses `test-author.md`; review uses `reviewer.md`.

- [ ] **Step 4: Implement closed family factories without dynamic IDs**

```python
Family = Literal["api", "e2e", "fuzz", "performance"]
FAMILIES: tuple[Family, ...] = ("api", "e2e", "fuzz", "performance")


def planning_handlers() -> Mapping[str, TaskHandler]:
    handlers: dict[str, TaskHandler] = {}
    for family in FAMILIES:
        handlers[f"assurance.generation.{family}.plan.prepare"] = PlanPrepareHandler(family)
        handlers[f"assurance.generation.{family}.plan.finalize"] = PlanFinalizeHandler(family)
        handlers[f"assurance.generation.{family}.plan-review.prepare"] = PlanReviewPrepareHandler(family)
        handlers[f"assurance.generation.{family}.plan-review.finalize"] = PlanReviewFinalizeHandler(family)
    return MappingProxyType(handlers)
```

The family is constructor-closed, not supplied by task input. Prepare ordering is skill, persona, reviewed-case JSON, then family constraints JSON. Finalizers require exact family match, exact leaf membership, operation/risk coverage, and canonical file references.

- [ ] **Step 5: Implement family validators**

Each family validator parses only its family plan files, rejects wrong-family entries, unresolved case IDs, unresolved capability leaves, missing operation/risk partitions, path traversal, and plan output outside declared write roots. Fuzz requires endpoint/property strategy; Performance requires scenario identity and numeric thresholds. `PlanMechanicalValidator` dispatches only to the closed four-family table, authenticates the family discriminator, and replaces the legacy `plan_mechanical_candidate/v1` without a product-global registry.

- [ ] **Step 6: Register exact contributions and add characterization tests**

Update the descriptor and declaration with 16 handler IDs, five validator IDs, two result-contract resources, eight skill resources, and two persona resources. Compare old/new accepted plans and review decisions on one valid and two invalid fixtures per family without production cross-imports.

- [ ] **Step 7: Run focused and generation regression gates**

Run: `uv run pytest packages/assurance-generation/tests -q`

Run: `uv run ruff check packages/assurance-generation`

Run: `uv run ruff format --check packages/assurance-generation`

Run: `uv run pyright packages/assurance-generation`

Expected: all generation tests pass.

- [ ] **Step 8: Update ownership evidence and commit**

```bash
git add packages/assurance-generation
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml
git commit -m "feat(assurance-generation): extract planning and review"
```

---

### Task 7: Extract code generation, generated-file mapping, and codegen-fixer capabilities

**Files:**
- Create: `packages/assurance-generation/assurance_generation/operations/codegen.py`
- Create: `packages/assurance-generation/assurance_generation/validators/generated_files.py`
- Create: `packages/assurance-generation/assurance_generation/resources/skills/aa-api-codegen/SKILL.md`
- Create: `packages/assurance-generation/assurance_generation/resources/skills/aa-api-codegen-fixer/SKILL.md`
- Create: `packages/assurance-generation/assurance_generation/resources/skills/aa-e2e-codegen/SKILL.md`
- Create: `packages/assurance-generation/assurance_generation/resources/skills/aa-e2e-codegen-fixer/SKILL.md`
- Create: `packages/assurance-generation/assurance_generation/resources/skills/aa-fuzz-codegen/SKILL.md`
- Create: `packages/assurance-generation/assurance_generation/resources/skills/aa-performance-codegen/SKILL.md`
- Create: `packages/assurance-generation/assurance_generation/resources/result-contracts/codegen.v1.schema.json`
- Create: `packages/assurance-generation/assurance_generation/resources/result-contracts/codegen-fix.v1.schema.json`
- Modify: `packages/assurance-generation/assurance_generation/plugin.py`
- Modify: `packages/assurance-generation/assurance_generation/plugin-declaration.json`
- Create: `packages/assurance-generation/tests/test_codegen.py`
- Create: `packages/assurance-generation/tests/test_generated_files_validator.py`
- Create: `packages/assurance-generation/tests/test_codegen_characterization.py`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml`

**Interfaces:**
- Produces prepare/finalize handlers for `api.codegen`, `api.codegen-fix`, `e2e.codegen`, `e2e.codegen-fix`, `fuzz.codegen`, and `performance.codegen` under `assurance.generation.*`.
- Produces validators `assurance.generation.validator.generated-files.v1`, `assurance.generation.validator.codegen-mapping.v1`, and `assurance.generation.validator.codegen-fix-candidate.v1`.
- Produces exact generated-file and mapping artifacts consumed by execution.
- Consumes: Task 5 contracts and Task 6 reviewed plans.
- Consumers: Tasks 8–11 and Phase 5 graph assembly.

- [ ] **Step 1: Write failing generated-file closure tests**

```python
@pytest.mark.parametrize("family", ("api", "e2e", "fuzz", "performance"))
def test_generated_files_require_exact_closed_mapping(family: str) -> None:
    candidate = generated_candidate(family, extra_file="tests/unmapped_test.py")
    result = GeneratedFilesValidator().validate(candidate, validation_context())
    assert result == ValidationResult(
        accepted=False,
        reason="generated test file is absent from the closed mapping: tests/unmapped_test.py",
    )


@pytest.mark.asyncio
async def test_codegen_finalize_rejects_claimed_but_missing_file(tmp_path: Path) -> None:
    outcome = await execute_task(
        CodegenFinalizeHandler("api"),
        fake_agent_result(codegen_result(files=["tests/api/test_users.py"])),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/assurance-generation/tests/test_codegen.py packages/assurance-generation/tests/test_generated_files_validator.py -v`

Expected: failures show codegen handlers and validators are absent.

- [ ] **Step 3: Port six exact skill resources and implement prepare handlers**

Port the six named legacy skills. Keep generated-file obligations, framework selection, case/plan traceability, and safety restrictions. Remove provider and whole-workflow instructions. Prepare input is the exact reviewed family plan plus frozen case references and baseline tree identity.

- [ ] **Step 4: Implement finalizers with workspace-byte authentication**

For each result, require every declared generated file to exist as a regular file in the attempt workspace, reject undeclared generated/modified test files, compute each file digest from bytes, and build `GeneratedFilesV1` plus `CodegenMapping`. API/E2E fixers accept only the files named in the fix input; fuzz/performance have no fixer handler in Phase 4.

- [ ] **Step 5: Implement candidate validators and exact path policy**

The generated-files validator requires canonical relative POSIX paths, family-specific test roots, exact mapping equality, unique case IDs, exact capability leaves, matching before/after digests, and no writes outside declared test paths. The mapping validator rejects stale, missing, extra, or duplicate mappings. `CodegenFixCandidateValidator` authenticates the approved fix proposal, baseline tree, allowed file set, and resulting mapping and directly replaces `codegen_fix_candidate/v1`.

- [ ] **Step 6: Update provider/declaration and characterization evidence**

Register 12 handlers, three validators, six skills, two result contracts, and retain the exact five Task 5 schema contributions (`plan-check.v1`, `plan-review.v1`, `generated-files.v1`, `codegen-mapping.v1`, and `discovery-campaign.v1`). Compare new artifacts with legacy codegen fixtures for each family, including missing-file, extra-file, and nonexistent capability-key cases.

- [ ] **Step 7: Run generation and upstream gates**

Run: `uv run pytest packages/assurance-intake/tests packages/assurance-generation/tests -q`

Run: `uv run ruff check packages/assurance-generation`

Run: `uv run pyright packages/assurance-generation`

Run: `uv run lint-imports`

Expected: all pass.

- [ ] **Step 8: Update ownership evidence and commit**

```bash
git add packages/assurance-generation
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml
git commit -m "feat(assurance-generation): extract code generation"
```

---

### Task 8: Create `assurance-execution` contracts, schemas, provider, and wheel boundary

**Files:**
- Create: `packages/assurance-execution/pyproject.toml`
- Create: `packages/assurance-execution/assurance_execution/__init__.py`
- Create: `packages/assurance-execution/assurance_execution/plugin.py`
- Create: `packages/assurance-execution/assurance_execution/contracts/__init__.py`
- Create: `packages/assurance-execution/assurance_execution/contracts/selection.py`
- Create: `packages/assurance-execution/assurance_execution/contracts/execution.py`
- Create: `packages/assurance-execution/assurance_execution/contracts/evidence.py`
- Create: `packages/assurance-execution/assurance_execution/resource_loader.py`
- Create: `packages/assurance-execution/assurance_execution/plugin-declaration.json`
- Create: `packages/assurance-execution/assurance_execution/resources/schemas/selected-targets.v1.schema.json`
- Create: `packages/assurance-execution/assurance_execution/resources/schemas/execution-manifest.v1.schema.json`
- Create: `packages/assurance-execution/assurance_execution/resources/schemas/closed-mapping.v1.schema.json`
- Create: `packages/assurance-execution/assurance_execution/resources/schemas/execution-evidence.v1.schema.json`
- Create: `packages/assurance-execution/tests/test_contracts.py`
- Create: `packages/assurance-execution/tests/test_plugin.py`
- Modify: `pyproject.toml`
- Modify: `.importlinter`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml`

**Interfaces:**
- Produces `SelectedTargets`, `ExecutionManifest`, `ClosedMappingEntryV1`, `ClosedMappingV1`, `RawTestResultV1`, `ExecutionReceiptV1`, and `ExecutionEvidenceV1`.
- Produces plugin `assurance.execution` version `0.1.0`, dependent on intake and generation `==0.1.0`.
- Produces schema IDs `assurance.execution.schema.selected-targets.v1`, `assurance.execution.schema.execution-manifest.v1`, `assurance.execution.schema.closed-mapping.v1`, and `assurance.execution.schema.execution-evidence.v1`.
- Consumes only public intake/generation contracts.
- Consumers: Task 9, healing, quality, improvement.

- [ ] **Step 1: Write failing closed-mapping and source-isolation tests**

```python
def test_closed_mapping_rejects_duplicate_or_unselected_test() -> None:
    with pytest.raises(ValidationError, match="mapping must equal selected tests"):
        ClosedMappingV1.model_validate(
            {"selected": ["tests/a.py"], "mappings": [mapping("tests/b.py")]}
        )


def test_execution_descriptor_has_exact_dependencies() -> None:
    assert ExecutionPlugin.descriptor().dependencies == (
        PluginDependency("assurance.intake", "==0.1.0"),
        PluginDependency("assurance.generation", "==0.1.0"),
    )
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/assurance-execution/tests -v`

Expected: collection fails because the execution wheel does not exist.

- [ ] **Step 3: Port and tighten execution contracts**

Port `SelectedTargets` and `ExecutionManifest` from `packages/assurance-kernel/assurance_kernel/artifacts/models/execution.py`, then port normalized result semantics from `assurance_agent/workflow/execution/results.py` and evidence semantics from `assurance_agent/workflow/execution/evidence.py`. Replace legacy imports with intake/generation public contracts. Add strict closed-mapping equality: every selected test appears exactly once, every mapped case/capability exists, and no unselected old test may enter execution evidence.

- [ ] **Step 4: Add canonical schemas, provider, and declaration**

Create checked-in canonical schema bytes, contribute them through `ExecutionPlugin`, and require static/live descriptor equality. Initial contribution has no handlers/validators/effects/bindings.

- [ ] **Step 5: Register workspace/import boundaries**

Add package metadata and root workspace configuration. Import-linter allows only `assurance_intake.contracts` and `assurance_generation.contracts` among Assurance imports and forbids both legacy packages.

- [ ] **Step 6: Run focused and upstream gates**

Run: `uv run pytest packages/assurance-intake/tests packages/assurance-generation/tests packages/assurance-execution/tests -q`

Run: `uv run ruff check packages/assurance-execution`

Run: `uv run pyright packages/assurance-execution`

Run: `uv run lint-imports`

Expected: all pass.

- [ ] **Step 7: Update evidence and commit**

```bash
git add packages/assurance-execution pyproject.toml uv.lock .importlinter
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml
git commit -m "feat(assurance-execution): add execution contracts"
```

---

### Task 9: Extract test selection, execution, normalization, and raw evidence

**Files:**
- Create: `packages/assurance-execution/assurance_execution/contracts/agent.py`
- Create: `packages/assurance-execution/assurance_execution/operations/__init__.py`
- Create: `packages/assurance-execution/assurance_execution/operations/selection.py`
- Create: `packages/assurance-execution/assurance_execution/operations/runner.py`
- Create: `packages/assurance-execution/assurance_execution/operations/normalize.py`
- Create: `packages/assurance-execution/assurance_execution/operations/agent_skills.py`
- Create: `packages/assurance-execution/assurance_execution/validators/__init__.py`
- Create: `packages/assurance-execution/assurance_execution/validators/mapping.py`
- Create: `packages/assurance-execution/assurance_execution/validators/evidence.py`
- Create: `packages/assurance-execution/assurance_execution/resources/skills/aa-execute/SKILL.md`
- Create: `packages/assurance-execution/assurance_execution/resources/skills/aa-run/SKILL.md`
- Create: `packages/assurance-execution/assurance_execution/resources/personas/executor.md`
- Create: `packages/assurance-execution/assurance_execution/resources/result-contracts/execution.v1.schema.json`
- Modify: `packages/assurance-execution/assurance_execution/plugin.py`
- Modify: `packages/assurance-execution/assurance_execution/plugin-declaration.json`
- Create: `packages/assurance-execution/tests/test_selection.py`
- Create: `packages/assurance-execution/tests/test_runner.py`
- Create: `packages/assurance-execution/tests/test_normalize.py`
- Create: `packages/assurance-execution/tests/test_agent_skills.py`
- Create: `packages/assurance-execution/tests/test_validators.py`
- Create: `packages/assurance-execution/tests/test_execution_characterization.py`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml`

**Interfaces:**
- Produces direct handlers `assurance.execution.select`, `assurance.execution.run-tests`, `assurance.execution.run-tests-and-collect-pr-metrics`, and `assurance.execution.normalize`.
- Produces agent handlers `assurance.execution.execute.prepare`, `.execute.finalize`, `.run.prepare`, and `.run.finalize`.
- Produces validators `assurance.execution.validator.closed-mapping.v1` and `assurance.execution.validator.evidence.v1`.
- Consumes exact reviewed plans/generated-file mappings; emits raw and normalized evidence.
- Consumers: healing and quality contracts/handlers.

- [ ] **Step 1: Write failing execution-scope and old-test-exclusion tests**

```python
@pytest.mark.asyncio
async def test_run_tests_executes_only_closed_mapping(tmp_path: Path) -> None:
    write_test(tmp_path / "tests/generated_test.py")
    write_test(tmp_path / "tests/legacy_test.py")
    outcome = await execute_task(
        RunTestsHandler(process_host=fake_pytest_host()),
        run_request(selected=["tests/generated_test.py"]),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    assert executed_paths(outcome) == ("tests/generated_test.py",)


def test_evidence_validator_rejects_result_outside_mapping() -> None:
    result = ExecutionEvidenceValidator().validate(
        candidate_with_result("tests/legacy_test.py"), validation_context()
    )
    assert result.accepted is False
    assert result.reason == "execution evidence contains a test outside the closed mapping"
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/assurance-execution/tests/test_runner.py packages/assurance-execution/tests/test_validators.py -v`

Expected: failures show handlers and validators are absent.

- [ ] **Step 3: Port selection and process execution behind narrow internal seams**

Port the logic from `assurance_agent/workflow/execution/selection.py`, `runner.py`, `runners.py`, `pytest_parser.py`, `results.py`, and `scope.py` in that same directory. The public handler accepts only canonical data. Keep `ExecutionProcessHost` as a private protocol injected into `RunTestsHandler`; production constructs a confined implementation, tests inject a deterministic fake. Do not invoke a shell and do not read ambient runner configuration.

- [ ] **Step 4: Enforce the exact closed mapping before spawn**

The run handler resolves every selected path against the attempt workspace, rejects symlinks/traversal/non-regular files, verifies the selected set equals the closed mapping, builds argv without shell expansion, and spawns only after all checks pass. Unmapped existing tests are neither discovered nor run.

- [ ] **Step 5: Normalize raw results and produce evidence**

Normalize exit code, collected/passed/failed/skipped counts, test IDs, duration, failure details, and command receipt into `ExecutionEvidenceV1`. Bind evidence to selected mapping digest, baseline tree ID, runner profile digest, and raw receipt digest. Reject results for tests absent from the mapping.

- [ ] **Step 6: Add provider-neutral execute/run skill handlers**

Port `aa-execute` and `aa-run` resources, remove provider-specific behavior, and implement prepare/finalize using the same closed `AgentRunRequest` pattern as intake. Finalization accepts only results consistent with the selected mapping and authenticated workspace evidence.

- [ ] **Step 7: Register contributions and characterization evidence**

Register eight handlers, two validators, skill/persona/result resources, and execution schemas. Compare old/new selected tests, normalized evidence, exit classification, and PR metric input on fixed pass/fail/empty/unmapped fixtures.

- [ ] **Step 8: Run execution and upstream gates**

Run: `uv run pytest packages/assurance-intake/tests packages/assurance-generation/tests packages/assurance-execution/tests -q`

Run: `uv run ruff check packages/assurance-execution`

Run: `uv run ruff format --check packages/assurance-execution`

Run: `uv run pyright packages/assurance-execution`

Run: `uv run lint-imports`

Expected: all pass.

- [ ] **Step 9: Update ownership evidence and commit**

```bash
git add packages/assurance-execution
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml
git commit -m "feat(assurance-execution): extract execution capabilities"
```

---

### Task 10: Create `assurance-healing` contracts, policy resources, provider, and wheel boundary

**Files:**
- Create: `packages/assurance-healing/pyproject.toml`
- Create: `packages/assurance-healing/assurance_healing/__init__.py`
- Create: `packages/assurance-healing/assurance_healing/plugin.py`
- Create: `packages/assurance-healing/assurance_healing/contracts/__init__.py`
- Create: `packages/assurance-healing/assurance_healing/contracts/proposal.py`
- Create: `packages/assurance-healing/assurance_healing/contracts/safety.py`
- Create: `packages/assurance-healing/assurance_healing/contracts/coverage_repair.py`
- Create: `packages/assurance-healing/assurance_healing/contracts/effects.py`
- Create: `packages/assurance-healing/assurance_healing/contracts/status.py`
- Create: `packages/assurance-healing/assurance_healing/resource_loader.py`
- Create: `packages/assurance-healing/assurance_healing/plugin-declaration.json`
- Create: `packages/assurance-healing/assurance_healing/resources/schemas/fix-proposal.v1.schema.json`
- Create: `packages/assurance-healing/assurance_healing/resources/schemas/healing-safety.v1.schema.json`
- Create: `packages/assurance-healing/assurance_healing/resources/schemas/coverage-repair.v1.schema.json`
- Create: `packages/assurance-healing/assurance_healing/resources/schemas/healing-status.v1.schema.json`
- Create: `packages/assurance-healing/assurance_healing/resources/schemas/allocation-intent.v2.schema.json`
- Create: `packages/assurance-healing/assurance_healing/resources/schemas/allocation-receipt.v2.schema.json`
- Create: `packages/assurance-healing/assurance_healing/resources/schemas/proposal-approved-intent.v1.schema.json`
- Create: `packages/assurance-healing/assurance_healing/resources/schemas/proposal-approved-receipt.v1.schema.json`
- Create: `packages/assurance-healing/assurance_healing/resources/schemas/heal-apply-intent.v2.schema.json`
- Create: `packages/assurance-healing/assurance_healing/resources/schemas/heal-apply-receipt.v2.schema.json`
- Create: `packages/assurance-healing/assurance_healing/resources/policy/test-change-policy.v1.json`
- Create: `packages/assurance-healing/tests/test_contracts.py`
- Create: `packages/assurance-healing/tests/test_plugin.py`
- Modify: `pyproject.toml`
- Modify: `.importlinter`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml`

**Interfaces:**
- Produces public `FixProposalSummary`, `FixProposalItem`, `FixProposal`, `ApplySummary`, `SafetyCheck`, `CodegenFixApplyIntentV1`, `ApiCodegenFixApplyIntentV1`, `E2eCodegenFixApplyIntentV1`, `FixerProposalApprovalReceiptV1`, `FixerAuthorityV1`, `CodegenFixApplySummaryV1`, `CodegenFixerSafetyCheckV1`, `RepairItem`, `DeferredItem`, `CoverageRepairBrief`, `CoverageRepairStatus`, `CoverageRepairApplySummary`, `CoverageRepairBaseline`, `CoverageRepairSafetyCheck`, `TestChangePolicyV1`, `HealingOverrideTokenV1`, `HealingAllocationIntentV2`, `HealingAllocationReceiptV2`, `ProposalApprovedIntentV1`, `ProposalApprovedReceiptV1`, `HealApplyIntentV2`, `HealApplyReceiptV2`, and `HealingStatusV1`.
- Produces plugin `assurance.healing` version `0.1.0`, dependent on intake, generation, and execution `==0.1.0`.
- Produces ten schema resources and one non-executable policy resource.
- Consumes only upstream public contracts and normalized execution evidence.
- Consumers: Task 11, quality, improvement, Phase 5.

- [ ] **Step 1: Write failing policy and effect-contract tests**

```python
def test_healing_descriptor_declares_exact_upstream_dependencies() -> None:
    assert tuple(item.plugin_id for item in HealingPlugin.descriptor().dependencies) == (
        "assurance.intake",
        "assurance.generation",
        "assurance.execution",
    )


def test_override_token_is_bound_to_policy_and_candidate() -> None:
    with pytest.raises(ValidationError, match="override token digest"):
        HealingOverrideTokenV1.model_validate(
            forged_override_token(policy_digest="0" * 64, candidate_digest="1" * 64)
        )


def test_effect_receipt_rejects_wrong_idempotency_key() -> None:
    with pytest.raises(ValidationError, match="idempotency key"):
        HealApplyReceiptV2.model_validate(forged_receipt())
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/assurance-healing/tests -v`

Expected: collection fails because the healing wheel does not exist.

- [ ] **Step 3: Port exact healing-owned contracts without legacy imports**

Port named contracts and validators from:

```text
packages/assurance-kernel/assurance_kernel/artifacts/models/healing.py
packages/assurance-kernel/assurance_kernel/artifacts/models/healing_codegen.py
packages/assurance-kernel/assurance_kernel/artifacts/models/coverage_repair.py
assurance_agent/workflow/healing/effects.py
assurance_agent/workflow/healing/override_policy.py
assurance_agent/workflow/healing/safety.py
```

Replace shared imports with upstream public contracts. Separate data-only intent/receipt models from effect implementations. Bind every override/allocation/apply value to canonical candidate, baseline, policy, and owner IDs. Do not preserve `.aa` ambient policy loading; the policy arrives as a registered, digest-authenticated resource or Phase 5 binding value.

- [ ] **Step 4: Create checked-in canonical schemas and closed policy resource**

Each schema byte file must equal the owning model's canonical JSON schema. `test-change-policy.v1.json` has exact keys `allowed_test_roots`, `forbidden_product_roots`, `max_files`, and `require_approval`; reject extra keys, absolute paths, traversal, and empty roots.

- [ ] **Step 5: Add provider, declaration, workspace, and import boundaries**

The initial provider contributes schemas and policy only. Its exact dependency tuple matches Section 8 of the spec. Add root workspace and import-linter rules allowing only `assurance_intake.contracts`, `assurance_generation.contracts`, and `assurance_execution.contracts` among Assurance packages.

- [ ] **Step 6: Run focused and upstream gates**

Run: `uv run pytest packages/assurance-intake/tests packages/assurance-generation/tests packages/assurance-execution/tests packages/assurance-healing/tests -q`

Run: `uv run ruff check packages/assurance-healing`

Run: `uv run pyright packages/assurance-healing`

Run: `uv run lint-imports`

Expected: all pass.

- [ ] **Step 7: Update evidence and commit**

```bash
git add packages/assurance-healing pyproject.toml uv.lock .importlinter
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml
git commit -m "feat(assurance-healing): add healing contracts"
```

---

### Task 11: Extract healing preparation, safety, approval, status, and durable effects

**Files:**
- Create: `packages/assurance-healing/assurance_healing/contracts/agent.py`
- Create: `packages/assurance-healing/assurance_healing/operations/__init__.py`
- Create: `packages/assurance-healing/assurance_healing/operations/proposal.py`
- Create: `packages/assurance-healing/assurance_healing/operations/authority.py`
- Create: `packages/assurance-healing/assurance_healing/operations/safety.py`
- Create: `packages/assurance-healing/assurance_healing/operations/status.py`
- Create: `packages/assurance-healing/assurance_healing/validators/__init__.py`
- Create: `packages/assurance-healing/assurance_healing/validators/test_tree.py`
- Create: `packages/assurance-healing/assurance_healing/validators/override.py`
- Create: `packages/assurance-healing/assurance_healing/validators/repair.py`
- Create: `packages/assurance-healing/assurance_healing/effects/__init__.py`
- Create: `packages/assurance-healing/assurance_healing/effects/allocation.py`
- Create: `packages/assurance-healing/assurance_healing/effects/approval.py`
- Create: `packages/assurance-healing/assurance_healing/effects/apply.py`
- Create: `packages/assurance-healing/assurance_healing/resources/skills/aa-fix-proposal/SKILL.md`
- Create: `packages/assurance-healing/assurance_healing/resources/skills/aa-coverage-repair/SKILL.md`
- Create: `packages/assurance-healing/assurance_healing/resources/personas/fix-proposer.md`
- Create: `packages/assurance-healing/assurance_healing/resources/result-contracts/fix-proposal.v1.schema.json`
- Create: `packages/assurance-healing/assurance_healing/resources/result-contracts/coverage-repair.v1.schema.json`
- Modify: `packages/assurance-healing/assurance_healing/plugin.py`
- Modify: `packages/assurance-healing/assurance_healing/plugin-declaration.json`
- Create: `packages/assurance-healing/tests/test_proposal.py`
- Create: `packages/assurance-healing/tests/test_safety.py`
- Create: `packages/assurance-healing/tests/test_effects.py`
- Create: `packages/assurance-healing/tests/test_recovery_faults.py`
- Create: `packages/assurance-healing/tests/test_healing_characterization.py`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml`

**Interfaces:**
- Produces agent handlers `assurance.healing.fix-proposal.prepare/finalize` and `assurance.healing.coverage-repair.prepare/finalize`.
- Produces deterministic handlers `assurance.healing.allocate-healing-attempt`, `.fixer-authority-ready`, `.record-fixer-approval`, `.fixer-dispatch`, `.record-codegen-fix-apply`, `.combine-fixer-safety`, `.record-healing-status`, `.compute-coverage-repair-safety`, `.allocate-coverage-repair-attempt`, `.record-coverage-repair-status`, and `.project-episode`.
- Produces validators `assurance.healing.validator.test-tree.v1`, `.override.v1`, and `.repair-candidate.v1`.
- Produces effects `assurance.healing.effect.allocation.v2`, `.proposal-approved.v1`, and `.heal-apply.v2`.
- Replaces every healing-related `ProductHooks` behavior on the new path.
- Consumes upstream generated mappings and execution evidence. Healing owns `CoverageRepairBrief`; quality later imports that public healing contract when it converts a quality gap into a repair request, so healing never imports quality.

- [ ] **Step 1: Write failing hook-replacement and effect crash tests**

```python
def test_healing_plugin_has_no_product_hooks_import() -> None:
    assert "ProductHooks" not in imported_symbols("assurance_healing")


@pytest.mark.asyncio
async def test_allocation_apply_is_idempotent_and_reconcilable() -> None:
    handler = HealingAllocationEffect(store=FakeAllocationStore())
    intent = allocation_intent()
    first = await handler.apply(intent, "allocation-key")
    second = await handler.apply(intent, "allocation-key")
    reconciled = await handler.reconcile(intent, "allocation-key")
    assert first == second
    assert reconciled.status == "applied"
    assert reconciled.receipt == first.receipt


@pytest.mark.parametrize("cut", ("before_mutation", "after_mutation", "before_receipt"))
@pytest.mark.asyncio
async def test_heal_apply_crash_cuts_never_duplicate(cut: str) -> None:
    store = FaultingHealStore(cut=cut)
    handler = HealApplyEffect(store=store)
    await _drive_apply_then_reconcile(handler, heal_intent())
    assert store.external_mutation_count <= 1
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/assurance-healing/tests/test_effects.py packages/assurance-healing/tests/test_recovery_faults.py -v`

Expected: failures show effect handlers and registrations are absent.

- [ ] **Step 3: Port proposal/safety/status logic behind task handlers**

Port behavior from `assurance_agent/workflow/healing/allocation.py`, `graph_ops.py`, `operations.py`, `projection.py`, `override_policy.py`, and `safety.py` in that same directory. Convert legacy positional/filesystem callables into strict input models plus `TaskRequest`/`TaskContext`. Read only the attempt workspace and declared resources. Return canonical output and effect intents; do not commit external state directly from a task handler.

- [ ] **Step 4: Implement provider-neutral proposal and repair skills**

Port both skill resources and the fix-proposer persona. Prepare handlers assemble one exact `AgentRunRequest`. Finalizers validate proposal/repair semantics, exact target files, baseline digests, generated mapping, approval requirements, and allowed roots. A model claiming a nonexistent file or capability is `invalid_output`.

- [ ] **Step 5: Implement commit validators**

`TestTreeValidator` compares baseline and candidate test/product roots and permits only approved test changes. `OverrideValidator` verifies the exact policy/candidate token. `RepairCandidateValidator` requires every modified file to be named by an approved proposal, rejects product changes without approval, and authenticates apply summaries.

- [ ] **Step 6: Implement three durable effect handlers**

Port the exact domain keys: allocation derives `operation_id`, proposal approval derives `approval_id`, and heal apply derives `record_key`. Implement them in `HealingAllocationEffect`, `ProposalApprovedEffect`, and `HealApplyEffect`. Each receives an injected private store seam, authenticates intent kind/schema, requires the caller-supplied key to equal the derived value, returns canonical receipts, and reconciles absent/pending/applied/permanent states without guessing.

- [ ] **Step 7: Register complete handlers, validators, effects, and resources**

Declare the three effects with their exact intent/receipt schema IDs. Freeze these initial policies in both static and live contributions: allocation `EffectPolicy(max_attempts=3, timeout_seconds=30.0, backoff_seconds=1.0)`, proposal approval `EffectPolicy(max_attempts=3, timeout_seconds=30.0, backoff_seconds=1.0)`, and heal apply `EffectPolicy(max_attempts=5, timeout_seconds=120.0, backoff_seconds=2.0)`. Tests assert the complete values and that changing any policy field changes composition/lock identity. Add all handler/validator/resource IDs to static and live declarations. Do not register a generic healing callback or semantic pin map.

- [ ] **Step 8: Add independent characterization evidence**

Compare old/new allocation key, approval key, record key, safety verdict, override decision, proposal completion, episode projection, and repair status on fixed fixtures. Exercise each path separately; do not make new code call legacy functions.

- [ ] **Step 9: Run focused, fault, and upstream gates**

Run: `uv run pytest packages/assurance-healing/tests -q`

Run: `uv run ruff check packages/assurance-healing`

Run: `uv run ruff format --check packages/assurance-healing`

Run: `uv run pyright packages/assurance-healing`

Run: `uv run lint-imports`

Expected: all pass, including every crash cut.

- [ ] **Step 10: Update ownership evidence and commit**

```bash
git add packages/assurance-healing
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml
git commit -m "feat(assurance-healing): extract healing capabilities"
```

---

### Task 12: Create `assurance-quality` contracts, schemas, provider, and wheel boundary

**Files:**
- Create: `packages/assurance-quality/pyproject.toml`
- Create: `packages/assurance-quality/assurance_quality/__init__.py`
- Create: `packages/assurance-quality/assurance_quality/plugin.py`
- Create: `packages/assurance-quality/assurance_quality/contracts/__init__.py`
- Create: `packages/assurance-quality/assurance_quality/contracts/baseline.py`
- Create: `packages/assurance-quality/assurance_quality/contracts/trace.py`
- Create: `packages/assurance-quality/assurance_quality/contracts/coverage.py`
- Create: `packages/assurance-quality/assurance_quality/contracts/issues.py`
- Create: `packages/assurance-quality/assurance_quality/contracts/issue_events.py`
- Create: `packages/assurance-quality/assurance_quality/contracts/metrics.py`
- Create: `packages/assurance-quality/assurance_quality/contracts/pr_metrics.py`
- Create: `packages/assurance-quality/assurance_quality/contracts/c_layer.py`
- Create: `packages/assurance-quality/assurance_quality/contracts/quarantine.py`
- Create: `packages/assurance-quality/assurance_quality/contracts/sufficiency.py`
- Create: `packages/assurance-quality/assurance_quality/contracts/inspect.py`
- Create: `packages/assurance-quality/assurance_quality/contracts/report.py`
- Create: `packages/assurance-quality/assurance_quality/resource_loader.py`
- Create: `packages/assurance-quality/assurance_quality/plugin-declaration.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/fact-baseline.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/trace.v2.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/coverage-gaps.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/minimum-coverage.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/issues.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/issue-events.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/metrics.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/coverage-diff.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/constraint-coverage.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/auth-matrix.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/journey-coverage.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/perf-slack.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/mutation.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/assertion-strength.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/baseline-drift.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/adversarial-yield.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/c-layer.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/quarantine.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/sufficiency.v2.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/trace-sufficiency.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/quality-gate.v2.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/schemas/report.v1.schema.json`
- Create: `packages/assurance-quality/tests/test_contracts.py`
- Create: `packages/assurance-quality/tests/test_plugin.py`
- Modify: `pyproject.toml`
- Modify: `.importlinter`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml`

**Interfaces:**
- Produces public fact-baseline, trace, coverage-gap, minimum-coverage, issue, issue-event, PR metric evidence, C-layer, quarantine, sufficiency, inspect/quality-gate, and report models.
- Produces plugin `assurance.quality` version `0.1.0`, dependent on all four upstream wheels `==0.1.0`.
- Produces 22 exact schema resources: the nine base schema files plus coverage diff, constraint coverage, auth matrix, journey coverage, performance slack, mutation, assertion strength, baseline drift, adversarial yield, C-layer, quarantine, sufficiency v2, and trace-sufficiency v1.
- Consumes reviewed cases/plans, mappings, execution evidence, and healing status through public contracts.
- Consumers: Tasks 13–16 and Phase 5.

- [ ] **Step 1: Write failing strict-contract and dependency tests**

```python
def test_quality_descriptor_declares_exact_dependency_order() -> None:
    assert tuple(item.plugin_id for item in QualityPlugin.descriptor().dependencies) == (
        "assurance.intake",
        "assurance.generation",
        "assurance.execution",
        "assurance.healing",
    )


def test_trace_rejects_unmapped_or_unknown_capability() -> None:
    with pytest.raises(ValidationError, match="trace capability is not a frozen typed leaf"):
        TraceProjectionV2.model_validate(
            trace_fixture(capability="entities.fake"),
            context={"capability_leafs": VALID_LEAFS},
        )
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/assurance-quality/tests -v`

Expected: collection fails because the quality wheel does not exist.

- [ ] **Step 3: Port the exact quality-owned contracts**

Port the named models from `packages/assurance-kernel/assurance_kernel/artifacts/models/`: `explore.py`, `trace.py`, `coverage_gaps.py`, `minimum_coverage.py`, `issues.py`, `issue_events.py`, `metrics.py`, `pr_metric_evidence.py`, `c_layer.py`, `quarantine.py`, `sufficiency.py`, `trace_sufficiency.py`, `inspect.py`, and `report.py`. Replace legacy imports with upstream public contracts. Preserve discriminated versions and compatibility reads only where the new schema explicitly names them; authoring/final output remains strict.

Add exact frozen-catalog validation for every capability, case, plan, test, schema, issue, and evidence reference. Prefix-only checks are forbidden.

- [ ] **Step 4: Create canonical schemas, provider, and declaration**

Store exact canonical schema bytes, contribute them with owner-qualified IDs, and create a source-authenticated provider/declaration with the exact dependency order. Initial contribution has no handlers/validators/effects/bindings.

- [ ] **Step 5: Register workspace and import boundaries**

Add root workspace metadata and import-linter rules permitting only public contracts from intake, generation, execution, and healing. Forbid legacy imports and cross-wheel implementation imports.

- [ ] **Step 6: Run focused and upstream gates**

Run: `uv run pytest packages/assurance-intake/tests packages/assurance-generation/tests packages/assurance-execution/tests packages/assurance-healing/tests packages/assurance-quality/tests -q`

Run: `uv run ruff check packages/assurance-quality`

Run: `uv run pyright packages/assurance-quality`

Run: `uv run lint-imports`

Expected: all pass.

- [ ] **Step 7: Update evidence and commit**

```bash
git add packages/assurance-quality pyproject.toml uv.lock .importlinter
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml
git commit -m "feat(assurance-quality): add quality contracts"
```

---

### Task 13: Extract trace, coverage, metrics, issue, and nightly deterministic operations

**Files:**
- Create: `packages/assurance-quality/assurance_quality/operations/__init__.py`
- Create: `packages/assurance-quality/assurance_quality/operations/trace.py`
- Create: `packages/assurance-quality/assurance_quality/operations/coverage.py`
- Create: `packages/assurance-quality/assurance_quality/operations/metrics.py`
- Create: `packages/assurance-quality/assurance_quality/operations/issues.py`
- Create: `packages/assurance-quality/assurance_quality/operations/nightly.py`
- Create: `packages/assurance-quality/assurance_quality/validators/__init__.py`
- Create: `packages/assurance-quality/assurance_quality/validators/trace.py`
- Create: `packages/assurance-quality/assurance_quality/validators/issues.py`
- Create: `packages/assurance-quality/assurance_quality/validators/metrics.py`
- Modify: `packages/assurance-quality/assurance_quality/plugin.py`
- Modify: `packages/assurance-quality/assurance_quality/plugin-declaration.json`
- Create: `packages/assurance-quality/tests/test_trace.py`
- Create: `packages/assurance-quality/tests/test_coverage.py`
- Create: `packages/assurance-quality/tests/test_metrics.py`
- Create: `packages/assurance-quality/tests/test_issues.py`
- Create: `packages/assurance-quality/tests/test_nightly.py`
- Create: `packages/assurance-quality/tests/test_quality_characterization.py`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml`

**Interfaces:**
- Produces owner-qualified replacements for all quality operation IDs listed in Task 1, excluding `inspect` and `generate-report`, which arrive in Task 14.
- Produces validators `assurance.quality.validator.trace.v2`, `.issues.v1`, `.metrics.v1`, `.problem-apply.v1`, and `.cross-artifact.v1`.
- Consumes canonical upstream artifacts; emits quality-owned projections only.
- Consumers: Task 14, improvement, Phase 5.

- [ ] **Step 1: Write failing representative operation and reference-closure tests**

```python
@pytest.mark.asyncio
async def test_trace_operation_excludes_unmapped_old_tests(tmp_path: Path) -> None:
    outcome = await execute_task(
        MaterializeTraceHandler(),
        trace_input(closed_mapping=["tests/generated.py"], observed=["tests/generated.py", "tests/old.py"]),
        tmp_path,
    )
    trace = TraceProjectionV2.model_validate(outcome.output)
    assert tuple(row.test_path for row in trace.rows) == ("tests/generated.py",)


@pytest.mark.asyncio
async def test_issue_candidate_digest_changes_with_canonical_evidence() -> None:
    first = await execute_task(CollectObservationsHandler(), issue_input(message="a"))
    second = await execute_task(CollectObservationsHandler(), issue_input(message="b"))
    assert canonical_digest(first.output) != canonical_digest(second.output)
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/assurance-quality/tests/test_trace.py packages/assurance-quality/tests/test_issues.py -v`

Expected: failures show the operations are absent.

- [ ] **Step 3: Port deterministic trace and coverage behavior**

Port trace projection, coverage gaps, minimum coverage, diff/constraint/auth/journey coverage, threshold slack, quarantine, C-layer, and coverage-repair-need logic from legacy graph handlers, metrics modules, and evidence modules. Convert each to one focused `TaskHandler` with closed Pydantic input/output. Read no global state and discover no tests outside the closed mapping.

- [ ] **Step 4: Port deterministic metrics and nightly behavior**

Port PR metrics, mutation sampling, assertion strength, baseline drift, adversarial yield, aggregation, shortboards, and nightly pipeline logic. Inject process/data sources through private constructor seams. Bind every metric to exact scope, evidence, baseline, and source digests.

- [ ] **Step 5: Port issue lifecycle behavior**

Port observation collection, empty/failure status, sync pending, reconcile, review context, and review application. Replace file-global stores with canonical task inputs/outputs or durable effects only where an external mutation exists. Candidate identity is computed from canonical evidence, never document formatting.

- [ ] **Step 6: Implement three validators and exact operation registration**

Trace validator checks exact case/plan/test/capability closure. Issue validator checks event transitions, evidence membership, and canonical fingerprint. Metrics validator checks numeric domains, scope identity, evidence completeness, and no NaN/Infinity. `ProblemApplyValidator` authenticates the reviewed issue application candidate; `CrossArtifactValidator` checks exact schema/digest/reference closure across quality-owned inputs. Register every owner-qualified handler in sorted order and update the static declaration.

- [ ] **Step 7: Add legacy characterization matrix**

Compare old/new canonical projections and gate inputs for trace, coverage, PR metrics, nightly metrics, issue candidate, issue reconcile, and issue review fixtures. Record intentional differences only when the new spec requires stricter exact-key/closed-mapping behavior.

- [ ] **Step 8: Run quality deterministic gates**

Run: `uv run pytest packages/assurance-quality/tests/test_trace.py packages/assurance-quality/tests/test_coverage.py packages/assurance-quality/tests/test_metrics.py packages/assurance-quality/tests/test_issues.py packages/assurance-quality/tests/test_nightly.py packages/assurance-quality/tests/test_quality_characterization.py -q`

Run: `uv run ruff check packages/assurance-quality`

Run: `uv run pyright packages/assurance-quality`

Expected: all pass.

- [ ] **Step 9: Update ownership evidence and commit**

```bash
git add packages/assurance-quality
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml
git commit -m "feat(assurance-quality): extract quality operations"
```

---

### Task 14: Extract fact baseline, inspect, issue-agent, report, and dashboard capabilities

**Files:**
- Create: `packages/assurance-quality/assurance_quality/contracts/agent.py`
- Create: `packages/assurance-quality/assurance_quality/operations/agent_skills.py`
- Create: `packages/assurance-quality/assurance_quality/operations/inspect.py`
- Create: `packages/assurance-quality/assurance_quality/operations/report.py`
- Create: `packages/assurance-quality/assurance_quality/validators/report.py`
- Create: `packages/assurance-quality/assurance_quality/resources/skills/aa-fact-baseline/SKILL.md`
- Create: `packages/assurance-quality/assurance_quality/resources/skills/aa-inspect/SKILL.md`
- Create: `packages/assurance-quality/assurance_quality/resources/skills/aa-issue-analyzer/SKILL.md`
- Create: `packages/assurance-quality/assurance_quality/resources/skills/aa-issue-triage-advisor/SKILL.md`
- Create: `packages/assurance-quality/assurance_quality/resources/skills/aa-report-generator/SKILL.md`
- Create: `packages/assurance-quality/assurance_quality/resources/skills/aa-dashboard/SKILL.md`
- Create: `packages/assurance-quality/assurance_quality/resources/personas/explorer.md`
- Create: `packages/assurance-quality/assurance_quality/resources/personas/reviewer.md`
- Create: `packages/assurance-quality/assurance_quality/resources/personas/reporter.md`
- Create: `packages/assurance-quality/assurance_quality/resources/result-contracts/fact-baseline.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/result-contracts/inspection.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/result-contracts/issue-analysis.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/result-contracts/issue-triage.v1.schema.json`
- Create: `packages/assurance-quality/assurance_quality/resources/result-contracts/report.v1.schema.json`
- Modify: `packages/assurance-quality/assurance_quality/plugin.py`
- Modify: `packages/assurance-quality/assurance_quality/plugin-declaration.json`
- Create: `packages/assurance-quality/tests/test_agent_skills.py`
- Create: `packages/assurance-quality/tests/test_inspect.py`
- Create: `packages/assurance-quality/tests/test_report.py`
- Create: `packages/assurance-quality/tests/test_report_validator.py`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml`

**Interfaces:**
- Produces prepare/finalize pairs for fact-baseline, inspect, issue-analysis, issue-triage, and report under `assurance.quality.*`.
- Produces deterministic handlers `assurance.quality.inspect`, `assurance.quality.generate-report`, and `assurance.quality.dashboard`.
- Produces validator `assurance.quality.validator.report.v1`.
- Replaces issue-output completion and candidate digest `ProductHooks` behavior on the new path.
- Consumers: improvement and Phase 5.

- [ ] **Step 1: Write failing agent semantic and report-closure tests**

```python
@pytest.mark.asyncio
async def test_issue_finalize_rejects_candidate_without_owned_evidence(tmp_path: Path) -> None:
    outcome = await execute_task(
        IssueAnalysisFinalizeHandler(),
        fake_agent_result(issue_candidate(evidence_ids=["missing"])),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"


def test_report_validator_requires_exact_quality_inputs() -> None:
    result = ReportValidator().validate(candidate_report(missing="metrics"), validation_context())
    assert result == ValidationResult(
        accepted=False,
        reason="quality report is missing the authenticated metrics projection",
    )
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/assurance-quality/tests/test_agent_skills.py packages/assurance-quality/tests/test_report.py -v`

Expected: failures show agent/report handlers are absent.

- [ ] **Step 3: Port six exact skill resources and three personas**

Move business obligations from the six legacy skills into provider-neutral resources. Port only quality-owned legacy `aa-reporter`; author quality-specific `explorer.md` and `reviewer.md` from the fact/issue skill obligations without copying the intake/generation persona bytes. Remove model defaults, OpenCode agent names, provider commands, and workflow navigation. `aa-dashboard` becomes a quality projection skill/resource; this task does not implement frontend UI.

- [ ] **Step 4: Implement prepare/finalize pairs**

All prepare handlers use skill/persona/canonical-input ordering and exact locked execution selection. Fact-baseline finalization verifies source evidence; inspect verifies trace/coverage/metrics/execution/healing closure; issue handlers verify evidence/fingerprint/status; report verifies all referenced projections and produces canonical report data.

- [ ] **Step 5: Port inspect/report/dashboard deterministic behavior**

Port quality gate, failure classification, quality score, inspector, report builder, and dashboard projection logic into focused handlers. Inputs are exact canonical upstream artifacts; output contains no provider session transcript or raw secret-bearing diagnostic.

- [ ] **Step 6: Implement report validator and register contributions**

The report validator requires exact source digests for case, plan, mapping, execution, healing, trace, coverage, issue, and metrics inputs. Register all handlers/resources/validator and update the static declaration.

- [ ] **Step 7: Run complete quality gates**

Run: `uv run pytest packages/assurance-quality/tests -q`

Run: `uv run ruff check packages/assurance-quality`

Run: `uv run ruff format --check packages/assurance-quality`

Run: `uv run pyright packages/assurance-quality`

Run: `uv run lint-imports`

Expected: all pass.

- [ ] **Step 8: Update ownership evidence and commit**

```bash
git add packages/assurance-quality
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml
git commit -m "feat(assurance-quality): extract quality agent and report capabilities"
```

---

### Task 15: Create `assurance-improvement` contracts, schemas, provider, and wheel boundary

**Files:**
- Create: `packages/assurance-improvement/pyproject.toml`
- Create: `packages/assurance-improvement/assurance_improvement/__init__.py`
- Create: `packages/assurance-improvement/assurance_improvement/plugin.py`
- Create: `packages/assurance-improvement/assurance_improvement/contracts/__init__.py`
- Create: `packages/assurance-improvement/assurance_improvement/contracts/retro.py`
- Create: `packages/assurance-improvement/assurance_improvement/contracts/improvements.py`
- Create: `packages/assurance-improvement/assurance_improvement/contracts/review.py`
- Create: `packages/assurance-improvement/assurance_improvement/contracts/delivery.py`
- Create: `packages/assurance-improvement/assurance_improvement/contracts/promotion.py`
- Create: `packages/assurance-improvement/assurance_improvement/contracts/declarations.py`
- Create: `packages/assurance-improvement/assurance_improvement/contracts/effects.py`
- Create: `packages/assurance-improvement/assurance_improvement/resource_loader.py`
- Create: `packages/assurance-improvement/assurance_improvement/plugin-declaration.json`
- Create: `packages/assurance-improvement/assurance_improvement/resources/schemas/retro-context.v3.schema.json`
- Create: `packages/assurance-improvement/assurance_improvement/resources/schemas/retro-signals.v3.schema.json`
- Create: `packages/assurance-improvement/assurance_improvement/resources/schemas/improvement-candidates.v3.schema.json`
- Create: `packages/assurance-improvement/assurance_improvement/resources/schemas/improvement-review.v1.schema.json`
- Create: `packages/assurance-improvement/assurance_improvement/resources/schemas/improvement-delivery.v1.schema.json`
- Create: `packages/assurance-improvement/assurance_improvement/resources/schemas/promotion.v1.schema.json`
- Create: `packages/assurance-improvement/assurance_improvement/resources/schemas/declaration-proposal.v1.schema.json`
- Create: `packages/assurance-improvement/assurance_improvement/resources/schemas/improvement-effect-intent.v1.schema.json`
- Create: `packages/assurance-improvement/assurance_improvement/resources/schemas/improvement-effect-receipt.v1.schema.json`
- Create: `packages/assurance-improvement/tests/test_contracts.py`
- Create: `packages/assurance-improvement/tests/test_plugin.py`
- Modify: `pyproject.toml`
- Modify: `.importlinter`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml`

**Interfaces:**
- Produces public retro context/signal/candidate, improvement lifecycle/review/outbox, declaration proposal, delivery, promotion, and effect intent/receipt contracts.
- Produces plugin `assurance.improvement` version `0.1.0`, dependent on all five upstream wheels `==0.1.0`.
- Produces nine exact schema resources.
- Consumes all upstream public contracts and quality summaries.
- Consumers: Task 16 and Phase 5.

- [ ] **Step 1: Write failing dependency and candidate-integrity tests**

```python
def test_improvement_descriptor_declares_all_upstream_in_order() -> None:
    assert tuple(item.plugin_id for item in ImprovementPlugin.descriptor().dependencies) == (
        "assurance.intake",
        "assurance.generation",
        "assurance.execution",
        "assurance.healing",
        "assurance.quality",
    )


def test_retro_candidate_requires_authenticated_source_membership() -> None:
    with pytest.raises(ValidationError, match="candidate source is outside the retro manifest"):
        ImprovementCandidateDocumentV3.model_validate(candidate_with_unknown_source())
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/assurance-improvement/tests -v`

Expected: collection fails because the improvement wheel does not exist.

- [ ] **Step 3: Port exact improvement-owned contracts**

Port the named models from `packages/assurance-kernel/assurance_kernel/artifacts/models/`: `retro_batch.py`, `retro_v3.py`, `improvements.py`, `improvement_review.py`, `improvement_outbox.py`, `declarations.py`, and `promotion.py`, plus data-only receipts from `assurance_agent/workflow/improvements/` delivery modules. Replace legacy imports with upstream public contracts. Preserve exact event/lifecycle invariants and versioned retro semantics.

- [ ] **Step 4: Create schemas, provider, declaration, and import contracts**

Store and authenticate all nine schemas. Add a provider with exact dependency order and initial schema-only contribution. Register the wheel in uv/pytest/Pyright. Import-linter permits only public contracts from the five upstream wheels and forbids legacy/private imports.

- [ ] **Step 5: Run focused and upstream gates**

Run: `uv run pytest packages/assurance-intake/tests packages/assurance-generation/tests packages/assurance-execution/tests packages/assurance-healing/tests packages/assurance-quality/tests packages/assurance-improvement/tests -q`

Run: `uv run ruff check packages/assurance-improvement`

Run: `uv run pyright packages/assurance-improvement`

Run: `uv run lint-imports`

Expected: all pass.

- [ ] **Step 6: Update evidence and commit**

```bash
git add packages/assurance-improvement pyproject.toml uv.lock .importlinter
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml
git commit -m "feat(assurance-improvement): add improvement contracts"
```

---

### Task 16: Extract retro, improvement review, delivery, archive, rollback, and effects

**Files:**
- Create: `packages/assurance-improvement/assurance_improvement/contracts/agent.py`
- Create: `packages/assurance-improvement/assurance_improvement/operations/__init__.py`
- Create: `packages/assurance-improvement/assurance_improvement/operations/retro.py`
- Create: `packages/assurance-improvement/assurance_improvement/operations/review.py`
- Create: `packages/assurance-improvement/assurance_improvement/operations/delivery.py`
- Create: `packages/assurance-improvement/assurance_improvement/operations/archive.py`
- Create: `packages/assurance-improvement/assurance_improvement/validators/__init__.py`
- Create: `packages/assurance-improvement/assurance_improvement/validators/candidates.py`
- Create: `packages/assurance-improvement/assurance_improvement/validators/review.py`
- Create: `packages/assurance-improvement/assurance_improvement/validators/delivery.py`
- Create: `packages/assurance-improvement/assurance_improvement/effects/__init__.py`
- Create: `packages/assurance-improvement/assurance_improvement/effects/delivery.py`
- Create: `packages/assurance-improvement/assurance_improvement/effects/promotion.py`
- Create: `packages/assurance-improvement/assurance_improvement/effects/archive.py`
- Create: `packages/assurance-improvement/assurance_improvement/resources/skills/aa-retro/SKILL.md`
- Create: `packages/assurance-improvement/assurance_improvement/resources/skills/aa-retro-eval-analysis/SKILL.md`
- Create: `packages/assurance-improvement/assurance_improvement/resources/skills/aa-retro-issue-analysis/SKILL.md`
- Create: `packages/assurance-improvement/assurance_improvement/resources/skills/aa-retro-workflow-analysis/SKILL.md`
- Create: `packages/assurance-improvement/assurance_improvement/resources/skills/aa-improvement-reviewer/SKILL.md`
- Create: `packages/assurance-improvement/assurance_improvement/resources/skills/aa-archive/SKILL.md`
- Create: `packages/assurance-improvement/assurance_improvement/resources/personas/reviewer.md`
- Create: `packages/assurance-improvement/assurance_improvement/resources/personas/archiver.md`
- Create: `packages/assurance-improvement/assurance_improvement/resources/result-contracts/retro-analysis.v3.schema.json`
- Create: `packages/assurance-improvement/assurance_improvement/resources/result-contracts/improvement-review.v1.schema.json`
- Create: `packages/assurance-improvement/assurance_improvement/resources/result-contracts/archive.v1.schema.json`
- Modify: `packages/assurance-improvement/assurance_improvement/plugin.py`
- Modify: `packages/assurance-improvement/assurance_improvement/plugin-declaration.json`
- Create: `packages/assurance-improvement/tests/test_retro.py`
- Create: `packages/assurance-improvement/tests/test_review.py`
- Create: `packages/assurance-improvement/tests/test_delivery.py`
- Create: `packages/assurance-improvement/tests/test_effects.py`
- Create: `packages/assurance-improvement/tests/test_improvement_characterization.py`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml`

**Interfaces:**
- Produces prepare/finalize pairs for retro, retro-eval-analysis, retro-issue-analysis, retro-workflow-analysis, improvement-review, and archive.
- Produces owner-qualified deterministic replacements for all improvement operations listed in Task 1.
- Produces validators `assurance.improvement.validator.candidates.v3`, `.review.v1`, `.delivery.v1`, and `.archive-integrity.v1`.
- Produces effects `assurance.improvement.effect.delivery.v1`, `.promotion.v1`, and `.archive.v1`; rollback is an explicit delivery effect action with its own intent discriminator.
- Replaces retro/improvement output-completion hooks on the new path.

- [ ] **Step 1: Write failing source-closure, lifecycle, and effect tests**

```python
@pytest.mark.asyncio
async def test_retro_finalize_rejects_signal_outside_manifest(tmp_path: Path) -> None:
    outcome = await execute_task(
        RetroFinalizeHandler(),
        fake_agent_result(retro_result(source_id="unknown")),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"


@pytest.mark.asyncio
async def test_delivery_effect_reconciles_after_mutation_before_receipt() -> None:
    store = FaultingDeliveryStore(cut="after_mutation")
    handler = ImprovementDeliveryEffect(store=store)
    await _attempt_and_reconcile(handler, delivery_intent())
    assert store.delivery_count == 1
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/assurance-improvement/tests/test_retro.py packages/assurance-improvement/tests/test_effects.py -v`

Expected: failures show operations/effects/resources are absent.

- [ ] **Step 3: Port six exact skill resources and two personas**

Move the six named skill trees into the wheel, including the archive summary template. Port improvement-owned legacy `aa-archiver`; author improvement-specific `reviewer.md` from the improvement-review skill obligations without copying generation's legacy reviewer persona. Remove provider/model/whole-workflow assumptions. Preserve exact retro source closure, domain analysis, candidate, review, and archive output obligations.

- [ ] **Step 4: Port deterministic retro and improvement lifecycle behavior**

Port retro collection/assembly/fallback/status, outbox drain, candidate reconcile, review context/application, delivery load/evaluate, auto review, export/apply/rollback records, and archive projection into focused task handlers. Inputs are canonical upstream artifacts; no handler reads the graph ledger or legacy stores directly.

- [ ] **Step 5: Implement agent prepare/finalize handlers**

Prepare handlers assemble exact provider-neutral requests. Finalizers validate source manifest membership, domain status closure, candidate identity, review evidence, delivery target, and archive summary. Complete-signal/candidate/reviewer behavior becomes these finalizers, not hooks.

- [ ] **Step 6: Implement validators and durable effects**

Candidate validator checks source/evidence/candidate identity. Review validator checks subject/version/assessment. Delivery validator checks approved state and exact target. `ArchiveIntegrityValidator` authenticates the archive subject, selected artifact manifest, summary bytes, and pre-archive tree and directly replaces `archive_integrity/v1`. Effect handlers use injected stores, typed receipts, and apply/reconcile crash semantics. Their domain keys are exact: delivery uses `improvement_id:version:target_kind:target_digest`, promotion uses `improvement_id:version:promotion_digest`, and archive uses `invocation_id:archive_digest`; the handler verifies that the caller-supplied key equals the value derived from its validated intent. Freeze delivery at `EffectPolicy(max_attempts=5, timeout_seconds=120.0, backoff_seconds=2.0)`, promotion at `EffectPolicy(max_attempts=3, timeout_seconds=60.0, backoff_seconds=2.0)`, and archive at `EffectPolicy(max_attempts=3, timeout_seconds=60.0, backoff_seconds=2.0)`. Tests assert these exact values and lock-digest sensitivity.

- [ ] **Step 7: Register complete contributions and characterize legacy parity**

Update provider/declaration with every handler, validator, effect, schema, skill, persona, prompt, and result resource. Compare old/new retro context, signals, candidates, review decisions, effect keys/receipts, promotion output, rollback output, and archive summary on fixed fixtures.

- [ ] **Step 8: Run complete improvement and upstream gates**

Run: `uv run pytest packages/assurance-intake/tests packages/assurance-generation/tests packages/assurance-execution/tests packages/assurance-healing/tests packages/assurance-quality/tests packages/assurance-improvement/tests -q`

Run: `uv run ruff check packages/assurance-improvement`

Run: `uv run ruff format --check packages/assurance-improvement`

Run: `uv run pyright packages/assurance-improvement`

Run: `uv run lint-imports`

Expected: all pass.

- [ ] **Step 9: Update ownership evidence and commit**

```bash
git add packages/assurance-improvement
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml
git commit -m "feat(assurance-improvement): extract improvement capabilities"
```

---

### Task 17: Close cross-wheel artifact handoffs, capability keys, and registry references

**Files:**
- Create: `tests/phase4/fixtures/capability-catalog.v1.json`
- Create: `tests/phase4/test_cross_wheel_contracts.py`
- Create: `tests/phase4/test_registry_closure.py`
- Modify: `packages/assurance-quality/assurance_quality/operations/coverage.py`
- Modify: `packages/assurance-quality/tests/test_coverage.py`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml`

**Interfaces:**
- Produces: one test authority for the exact capability-leaf catalog and one black-box closure matrix over all six providers.
- Produces: quality conversion from `CoverageGap` to healing-owned `CoverageRepairBrief` without a reverse dependency.
- Consumes: only public provider interfaces, public contracts, registered schema/resource bytes, and the ownership ledger.
- Consumers: parity, isolation, and six-wheel composition tasks.

- [ ] **Step 1: Write failing exact-catalog and handoff tests**

```python
@pytest.mark.parametrize(
    "value",
    (
        "auth.fake",
        "entities.user.fake",
        "capabilities.adapters.nonexistent",
    ),
)
def test_every_contract_rejects_prefix_valid_unknown_leaf(value: str) -> None:
    for validator in all_capability_leaf_validators():
        with pytest.raises((ValidationError, ValueError), match="unknown|typed leaf"):
            validator(value, catalog=CAPABILITY_CATALOG)


def test_every_registry_reference_resolves_exactly_once() -> None:
    descriptors, contributions = all_six_provider_values()
    ids = contribution_ids(contributions)
    assert len(ids) == len(set(ids))
    assert every_schema_resource_and_effect_reference_resolves(descriptors, contributions)


def test_quality_gap_converts_to_healing_contract_without_reverse_import() -> None:
    brief = coverage_gap_to_repair_brief(quality_gap_fixture())
    assert isinstance(brief, CoverageRepairBrief)
    assert forbidden_imports("assurance_healing", prefix="assurance_quality") == set()
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest tests/phase4/test_cross_wheel_contracts.py tests/phase4/test_registry_closure.py -v`

Expected: failures identify unresolved/duplicated references, prefix-only validation paths, or missing handoff conversion.

- [ ] **Step 3: Freeze the exact capability catalog fixture**

The fixture is canonical JSON with schema version, catalog digest input, and the complete typed leaf set used by characterization tests. Tests reject duplicates, prefix nodes, non-leaf nodes, and any leaf absent from the exact set. The fixture is test evidence, not a runtime default catalog.

- [ ] **Step 4: Build a six-provider closure matrix through public contributions**

For each provider, call `descriptor()`, call `contribute()`, and run `validate_contribution()`. Build global maps for task handlers, validators, schemas, resources, effects, and bindings. Require global ID uniqueness, owner prefix, exact descriptor membership, exact schema/resource references, exact effect intent/receipt references, and no binding target in Phase 4 wheels.

- [ ] **Step 5: Test canonical artifact handoffs**

Exercise these exact handoffs with serialized bytes between producer and consumer models:

```text
intake reviewed case -> generation planning input
generation reviewed plan/generated mapping -> execution selection
execution evidence -> healing proposal and quality trace
healing status -> quality inspection/report
quality coverage gap -> healing CoverageRepairBrief
quality report/issues/metrics -> improvement retro context
```

Round-trip through canonical JSON at every seam. A consumer must reject changed schema ID, changed digest, missing field, extra field, wrong family, and unknown capability key.

- [ ] **Step 6: Run all six wheel tests and import contracts**

Run: `uv run pytest packages/assurance-intake/tests packages/assurance-generation/tests packages/assurance-execution/tests packages/assurance-healing/tests packages/assurance-quality/tests packages/assurance-improvement/tests tests/phase4/test_cross_wheel_contracts.py tests/phase4/test_registry_closure.py -q`

Run: `uv run lint-imports`

Run: `uv run pyright packages/assurance-intake packages/assurance-generation packages/assurance-execution packages/assurance-healing packages/assurance-quality packages/assurance-improvement tests/phase4`

Expected: all pass and the approved dependency DAG has no reverse import.

- [ ] **Step 7: Update ownership evidence and commit**

```bash
git add packages/assurance-quality tests/phase4
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml
git commit -m "test(assurance): close cross-wheel contracts"
```

---

### Task 18: Close every former `ProductHooks` behavior with independent parity evidence

**Files:**
- Create: `tests/phase4/fixtures/product-hooks-cases.yaml`
- Create: `tests/phase4/test_product_hooks_parity.py`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml`

**Interfaces:**
- Produces: complete old-hook → new contribution parity evidence; no production compatibility code.
- Consumes: legacy hooks only from tests, new public plugin contributions, fixed fixtures, and canonical encoders.
- Consumers: wheel isolation/release audit and Phase 6 deletion list.

- [ ] **Step 1: Write the failing completeness test against the real dataclass fields**

```python
def test_every_product_hook_has_one_verified_replacement() -> None:
    legacy_fields = {field.name for field in dataclasses.fields(ProductHooks)}
    ledger = load_ownership_ledger(OWNERSHIP_PATH)
    mapped = {
        item.legacy_id
        for item in ledger.items
        if item.kind == "hook" and item.status == "verified"
    }
    assert mapped == legacy_fields


def test_no_new_wheel_imports_or_recreates_product_hooks() -> None:
    assert forbidden_symbol_scan(
        NEW_WHEEL_ROOTS,
        symbols=("ProductHooks", "install_product_hooks", "current_product_hooks", "semantic_pins"),
    ) == set()
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest tests/phase4/test_product_hooks_parity.py -v`

Expected: failures list unverified hook mappings.

- [ ] **Step 3: Add exact fixture rows for all legacy hook fields**

The YAML fixture must contain one row for each field:

```text
load_product_code_roots
candidate_document_digest
commit_healing_allocation_ledger
complete_issue_analyzer_outputs
complete_improvement_reviewer_outputs
complete_signal_outputs
complete_candidate_outputs
register_healing_effects
project_healing_episode
assert_test_tree_unchanged_or_healing
assert_test_changes_override_allowed
build_test_changes_override_token
load_test_changes_override_policy
token_json_bytes
reconcile_healing_allocation
reconcile_fixer_proposal_approved
reconcile_heal_record_apply
semantic_pins
```

Each row names the exact new owner, handler/validator/effect/private function ID, input fixture, and comparison fields.

- [ ] **Step 4: Compare legacy and new paths independently**

For functional hooks, invoke the legacy callable and new public contribution separately, canonicalize their externally meaningful result, and compare. For `register_healing_effects`, compare the exact three effect registrations. For `semantic_pins`, assert no replacement callback exists and verify that changing the owning wheel's implementation/resource bytes changes the Phase 2 source/composition/lock digest instead.

- [ ] **Step 5: Enforce no hidden catch-all**

AST-scan the six wheels and reject any dataclass/mapping/protocol that stores more than one registry-kind callable or is named with `Hooks`, `HookRegistry`, `ProductRuntime`, or `SemanticPins`. Private focused store/host seams used by one handler/effect remain allowed.

- [ ] **Step 6: Run parity and full six-wheel gates**

Run: `uv run pytest tests/phase4/test_product_hooks_parity.py packages/assurance-healing/tests packages/assurance-quality/tests packages/assurance-improvement/tests -q`

Run: `uv run ruff check tests/phase4`

Run: `uv run pyright tests/phase4`

Expected: every hook is verified exactly once and no catch-all exists.

- [ ] **Step 7: Mark hook entries verified and commit**

```bash
git add tests/phase4
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml
git commit -m "test(assurance): close product hook parity"
```

---

### Task 19: Build and install every capability wheel from committed source in isolation

**Files:**
- Create: `scripts/assurance_capability_wheel_smoke_test.sh`
- Create: `tests/phase4/test_wheel_metadata.py`
- Modify: `scripts/packaging_smoke_test.sh`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: one committed-HEAD offline isolation command covering each legal dependency prefix and the full six-wheel set.
- Consumes: wheel metadata, static declarations, public entry points, and test-only conformance script.
- Consumers: Tasks 20–22 and CI.

- [ ] **Step 1: Write failing metadata and smoke entry tests**

```python
@pytest.mark.parametrize(
    "distribution, dependencies",
    (
        ("assurance-intake", ("graph-engine", "agent-runtime-contracts")),
        ("assurance-generation", ("graph-engine", "agent-runtime-contracts", "assurance-intake")),
        ("assurance-execution", ("graph-engine", "agent-runtime-contracts", "assurance-intake", "assurance-generation")),
        ("assurance-healing", ("graph-engine", "agent-runtime-contracts", "assurance-intake", "assurance-generation", "assurance-execution")),
        ("assurance-quality", ("graph-engine", "agent-runtime-contracts", "assurance-intake", "assurance-generation", "assurance-execution", "assurance-healing")),
        ("assurance-improvement", ("graph-engine", "agent-runtime-contracts", "assurance-intake", "assurance-generation", "assurance-execution", "assurance-healing", "assurance-quality")),
    ),
)
def test_wheel_metadata_has_exact_assurance_dependencies(
    distribution: str, dependencies: tuple[str, ...]
) -> None:
    assert phase4_runtime_dependency_names(distribution) == dependencies
```

`phase4_runtime_dependency_names()` canonicalizes PEP 503 names and filters installed metadata to `graph-engine`, `agent-runtime-contracts`, and `assurance-*`. This keeps the assertion exact for architectural edges while allowing declared third-party implementation dependencies such as Pydantic and PyYAML. A separate assertion requires every dependency to be present in the wheel metadata and forbids `assurance-agent`, `assurance-kernel`, undeclared local paths, and direct dependencies on downstream Assurance wheels.

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest tests/phase4/test_wheel_metadata.py -v`

Expected: failure because the committed-HEAD isolation script and exact metadata assertions are absent or incomplete.

- [ ] **Step 3: Implement committed-HEAD archive/build flow**

The shell script must:

1. reject a dirty tracked worktree;
2. create a private temporary directory;
3. `git archive HEAD` into a source tree;
4. build `graph-engine`, `agent-runtime-contracts`, and all six Assurance wheels with no network;
5. create a clean venv for each legal dependency prefix;
6. install local project distributions only from private wheel files with `uv pip install --offline --find-links`, allowing third-party requirements only from the pre-populated uv cache, then reject editable/direct-path local distributions in installed metadata;
7. enumerate exact `graph_engine.plugins` entry points;
8. load each provider, validate static/live declarations, and exercise at least one public contract/contribution path; and
9. scan installed files to prove `assurance_agent` and `assurance_kernel` are absent.

Use `set -euo pipefail`, quoted paths, and `mktemp -d`; cleanup through one trap that preserves the primary exit status.

- [ ] **Step 4: Add resource inclusion checks**

For every wheel, open the built archive and require its declaration plus every contributed schema/resource path. Compare installed resource bytes with source bytes and reject undeclared executable/config files.

- [ ] **Step 5: Wire the smoke into packaging and CI**

Call `scripts/assurance_capability_wheel_smoke_test.sh` from the existing packaging smoke after Phase 1–3 wheel checks. Add a named CI step without weakening existing gates.

- [ ] **Step 6: Run focused metadata and shell checks**

Run: `uv run pytest tests/phase4/test_wheel_metadata.py -q`

Run: `bash -n scripts/assurance_capability_wheel_smoke_test.sh`

Run: `bash -n scripts/packaging_smoke_test.sh`

Expected: all pass.

- [ ] **Step 7: Commit, then run the committed-HEAD smoke**

```bash
git add scripts/assurance_capability_wheel_smoke_test.sh scripts/packaging_smoke_test.sh tests/phase4/test_wheel_metadata.py .github/workflows/ci.yml
git commit -m "test(assurance): isolate capability wheels"
bash scripts/assurance_capability_wheel_smoke_test.sh
```

Expected: every clean environment passes and imports no legacy package. Record exact wheel filenames and entry points in `task-19-report.md`.

---

### Task 20: Resolve all six wheels in a test-only product and prove OpenCode/Cursor rebinding

**Files:**
- Create: `tests/phase4/fixtures/six-wheel-product/pyproject.toml`
- Create: `tests/phase4/fixtures/six-wheel-product/test_assurance_phase4_product/__init__.py`
- Create: `tests/phase4/fixtures/six-wheel-product/test_assurance_phase4_product/product.py`
- Create: `tests/phase4/fixtures/six-wheel-product/test_assurance_phase4_product/product-opencode-declaration.json`
- Create: `tests/phase4/fixtures/six-wheel-product/test_assurance_phase4_product/product-cursor-declaration.json`
- Create: `tests/phase4/fixtures/six-wheel-product/test_assurance_phase4_product/workflow.yaml`
- Create: `tests/phase4/fixtures/bindings-opencode/plugin.yaml`
- Create: `tests/phase4/fixtures/bindings-cursor/plugin.yaml`
- Create: `tests/phase4/fixtures/bindings-opencode/model-policy.json`
- Create: `tests/phase4/fixtures/bindings-cursor/model-policy.json`
- Create: `tests/phase4/test_six_wheel_composition.py`
- Create: `tests/phase4/test_adapter_rebinding.py`

**Interfaces:**
- Produces: one non-workspace test-only wheel with `phase4-opencode` and `phase4-cursor` `graph_engine.products` entry points plus config-tree binding sources; it is never selected by `aa`, added to workspace members, included in production packaging, or published.
- Produces: one representative prepare/adapter/finalize execution for both `runtime.opencode.execute` and `runtime.cursor.execute` using deterministic adapter fakes.
- Consumes: public `RegistryPlatform`, `FrozenComposition`, `Engine`, six wheel providers, Phase 3 adapters, and config-tree binding sources.
- Consumers: Task 21 security/fault audit and Phase 5 handoff evidence.

- [ ] **Step 1: Write failing six-wheel resolution and rebinding tests**

```python
def test_six_wheel_product_resolves_exact_dependency_order() -> None:
    composition = resolve_fixture("phase4-opencode")
    assert composition.dependency_order == (
        "assurance.intake",
        "assurance.generation",
        "assurance.execution",
        "assurance.healing",
        "assurance.quality",
        "assurance.improvement",
        "runtime.opencode",
        "test.assurance.bindings",
    )


@pytest.mark.asyncio
async def test_rebinding_preserves_business_request_and_result() -> None:
    opencode = await run_fixture("phase4-opencode")
    cursor = await run_fixture("phase4-cursor")
    assert opencode.agent_request_bytes == cursor.agent_request_bytes
    assert opencode.business_output_bytes == cursor.business_output_bytes
    assert opencode.adapter_id != cursor.adapter_id
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest tests/phase4/test_six_wheel_composition.py tests/phase4/test_adapter_rebinding.py -v`

Expected: failures show the product/config/workflow fixtures are absent.

- [ ] **Step 3: Create two closed test-only product providers in one fixture wheel**

Build the fixture distribution only inside the test temporary directory. `Phase4OpenCodeProduct.manifest()` and `Phase4CursorProduct.manifest()` each select the six wheels, exactly one runtime adapter, and exactly one config binding plugin. They differ only in runtime/binding source and exact worker profile. They use the same authenticated test-only workflow resource and expose one `fixture` graph entrypoint. Their declarations exactly bind their distinct entry-point names/values and the shared source snapshot.

The test distribution necessarily declares its two `graph_engine.products` entry points so `RegistryPlatform` can authenticate it; tests prove the directory is absent from root workspace members, root dependencies, release archives, and production entry-point enumeration.

- [ ] **Step 4: Create exact config-tree adapter bindings**

Each `plugin.yaml` declares binding aliases for the representative intake prepare, runtime execute, and intake finalize path. `binding_data` contains exactly:

```json
{
  "execution": {
    "provider_model": "fixture/model-v1",
    "worker_profile": "fixture-profile",
    "permission_profile_digest": "<64 lowercase hex from fixture bytes>",
    "limits": {"max_seconds": 30}
  },
  "request_policy_digest": "<64 lowercase hex from model policy bytes>",
  "request_config_digest": "<64 lowercase hex from canonical binding data>"
}
```

Write literal computed digests into the checked-in files and add tests that recompute them. There is no fallback, candidate list, or ambient default.

- [ ] **Step 5: Run through real composition and engine interfaces**

Resolve explicit sources with `RegistryPlatform`, freeze composition, compile the fixture graph, start an invocation, and execute through a test `TaskExecutionHost` that substitutes deterministic OpenCode/Cursor adapter fakes at the host seam. Do not bypass registry lookup or call capability handlers by private import in the integration assertion.

- [ ] **Step 6: Verify lock and provider-session independence**

Require different composition/lock digests for the two adapter selections, byte-equal pre-adapter `AgentRunRequest`, byte-equal finalized business output, and successful replay after deleting fake provider state. Assert no `SessionEvent` or provider message transcript exists in invocation files.

- [ ] **Step 7: Run composition, engine, and wheel gates**

Run: `uv run pytest tests/phase4/test_six_wheel_composition.py tests/phase4/test_adapter_rebinding.py -q`

Run: `uv run pytest packages/graph-engine/tests packages/agent-runtime-contracts/tests packages/agent-runtime-opencode/tests packages/agent-runtime-cursor/tests -q`

Run: `uv run lint-imports`

Expected: both fixtures pass; Phase 1–3 has no regression.

- [ ] **Step 8: Commit**

```bash
git add tests/phase4
git commit -m "test(assurance): prove six-wheel adapter rebinding"
```

---

### Task 21: Close source/resource drift, secret, indeterminate, effect, and path security matrices

**Files:**
- Create: `tests/phase4/test_security_matrix.py`
- Create: `tests/phase4/test_agent_indeterminate.py`
- Create: `tests/phase4/test_effect_fault_matrix.py`
- Create: `tests/phase4/test_path_confinement.py`
- Create: `tests/phase4/test_drift_matrix.py`
- Modify: affected wheel tests/implementation only when a matrix case establishes a real RED

**Interfaces:**
- Produces: Phase 4 adversarial evidence across all six public plugin seams.
- Consumes: explicit resolution/composition, test-only products, handler/effect fakes, and canonical invocation files.
- Consumers: final release gate and Phase 5 handoff.

- [ ] **Step 1: Write source/resource/config drift matrix**

```python
@pytest.mark.parametrize(
    "facet",
    (
        "plugin-code",
        "plugin-version",
        "schema-bytes",
        "skill-bytes",
        "persona-bytes",
        "result-contract-bytes",
        "policy-bytes",
        "binding-data",
        "capability-catalog",
    ),
)
def test_every_authority_facet_changes_composition_and_blocks_old_open(facet: str) -> None:
    original, drifted = resolve_with_one_mutation(facet)
    assert original.invocation_lock.canonical_bytes() != drifted.invocation_lock.canonical_bytes()
    assert_open_rejects_drift_without_ledger_append(original, drifted)
```

- [ ] **Step 2: Write credential-canary and diagnostic tests**

Inject unique canaries through the Phase 3 secret port and provider error paths. Scan every lock, ledger, checkpoint, workspace artifact, effect intent/receipt, task result, captured log, and diagnostic. Require zero canary occurrences and bounded redacted diagnostics.

- [ ] **Step 3: Write agent indeterminate tests**

For prepare-complete/dispatch-unknown/bound-running/result-truncated/terminal-observed cuts, prove a capability finalize handler runs only after an authenticated `AgentRunResult`. Indeterminate state appends no business artifact, effect intent, STOP, or new attempt.

- [ ] **Step 4: Write cross-wheel effect crash matrix**

Parametrize all six healing/improvement effects over cuts before mutation, after mutation, before receipt, after receipt, and reconcile error. Require at-most-once external mutation, stable idempotency key, exact receipt, and typed pending/indeterminate behavior.

- [ ] **Step 5: Write path and symlink confinement matrix**

Exercise absolute path, `..`, Windows drive, symlink file, symlink parent, hard-link candidate, path swap, and undeclared write-root cases against each wheel's validators/handlers. Require failure before external spawn/effect and no write outside the attempt workspace.

- [ ] **Step 6: Confirm RED**

Run: `uv run pytest tests/phase4/test_security_matrix.py tests/phase4/test_agent_indeterminate.py tests/phase4/test_effect_fault_matrix.py tests/phase4/test_path_confinement.py tests/phase4/test_drift_matrix.py -q`

Expected: the newly added matrix fails on absent test helpers or at least one explicitly recorded uncovered boundary. If every behavior assertion is already GREEN, preserve the coverage-only evidence, verify the test fails when its target guard is locally disabled, restore the guard, and record the RED/GREEN pair without manufacturing a production defect.

- [ ] **Step 7: Run the adversarial matrix and fix only reproduced gaps**

Run: `uv run pytest tests/phase4/test_security_matrix.py tests/phase4/test_agent_indeterminate.py tests/phase4/test_effect_fault_matrix.py tests/phase4/test_path_confinement.py tests/phase4/test_drift_matrix.py -q`

For every RED, record the exact failing assertion in `task-21-report.md`, implement the smallest owner-local fix, rerun the exact case, then rerun the complete matrix. Do not change Graph Engine or Phase 3 interfaces to silence a capability bug.

- [ ] **Step 8: Run static and all-wheel regression gates**

Run: `uv run ruff check packages/assurance-intake packages/assurance-generation packages/assurance-execution packages/assurance-healing packages/assurance-quality packages/assurance-improvement tests/phase4`

Run: `uv run ruff format --check packages/assurance-intake packages/assurance-generation packages/assurance-execution packages/assurance-healing packages/assurance-quality packages/assurance-improvement tests/phase4`

Run: `uv run pyright packages/assurance-intake packages/assurance-generation packages/assurance-execution packages/assurance-healing packages/assurance-quality packages/assurance-improvement tests/phase4`

Run: `uv run lint-imports`

Expected: all pass.

- [ ] **Step 9: Commit the matrix and owner-local fixes**

```bash
git add tests/phase4 packages/assurance-intake packages/assurance-generation packages/assurance-execution packages/assurance-healing packages/assurance-quality packages/assurance-improvement
git commit -m "test(assurance): close capability security matrix"
```

---

### Task 22: Complete release gates, ownership closure, Phase 5 handoff, and Phase 6 deletion inventory

**Files:**
- Create: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/phase5-handoff.md`
- Create: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/phase6-deletion.txt`
- Create: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/final-report.md`
- Modify: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-20-pure-graph-engine-plugin-architecture-design.md`

**Interfaces:**
- Produces: final Phase 4 acceptance evidence and exact Phase 5/6 handoff; no runtime interface.
- Consumes: all task reports, ownership ledger, built wheel metadata, test-only composition, and repository gates.
- Consumer: the Phase 5 design/implementation plan.

- [ ] **Step 1: Write the failing final ownership-closure test**

```python
def test_phase4_ownership_is_fully_verified() -> None:
    ledger = load_ownership_ledger(OWNERSHIP_PATH)
    planned = [item for item in ledger.items if item.disposition == "migrate" and item.status != "verified"]
    assert planned == []
    assert all(item.verification for item in ledger.items if item.status == "verified")
```

Run: `uv run pytest tests/phase4/test_ownership_ledger.py::test_phase4_ownership_is_fully_verified -v`

Expected: FAIL listing any remaining planned item; resolve each through its owning task evidence before proceeding.

- [ ] **Step 2: Freeze the Phase 5 handoff**

`phase5-handoff.md` must include:

- six wheel filenames, versions, entry-point coordinates, source/declaration paths, and dependency constraints;
- every public handler, validator, schema, resource, effect, and logical binding ID;
- exact artifact handoff table and schema digests;
- exact prepare binding-data contract;
- exact logical agent capability IDs requiring Phase 5 adapter bindings;
- product graph/entrypoint responsibilities deliberately absent from Phase 4;
- organization model/endpoint/secret/policy choices deliberately absent from Phase 4;
- committed-HEAD wheel smoke evidence; and
- known non-blocking concerns with owner and target phase.

- [ ] **Step 3: Freeze the Phase 6 deletion inventory**

Generate a sorted file list from every ledger item with a migrated new owner plus explicit legacy catalogs/hooks/resources. Require the list to include `ProductHooks`, `operations_catalog.py`, old skill/persona resources now owned by wheels, old artifact-model wrappers, and duplicate domain implementations. Exclude benchmark datasets/scorers retained as external Phase 5 comparison harnesses.

- [ ] **Step 4: Update documentation without claiming cutover**

README and umbrella architecture must state:

- Phase 4 wheels exist and pass isolation;
- `aa` still defaults to the legacy product;
- no production Assurance product manifest/full graph exists yet;
- Phase 5 is the next step; and
- Phase 6 performs the hard cut and deletion.

- [ ] **Step 5: Run the complete Phase 4 focused gate**

Run: `uv run pytest packages/graph-engine/tests packages/agent-runtime-contracts/tests packages/agent-runtime-opencode/tests packages/agent-runtime-cursor/tests packages/assurance-intake/tests packages/assurance-generation/tests packages/assurance-execution/tests packages/assurance-healing/tests packages/assurance-quality/tests packages/assurance-improvement/tests tests/phase4 -q`

Expected: zero failures; record exact passed/skipped counts. The original Phase 1–3 subset must still be `1369 passed, 1 skipped` unless a prior separately approved Phase 1–3 change changed that baseline and its report explains why.

- [ ] **Step 6: Run full static and architecture gates**

Run: `uv run ruff check .`

Run: `uv run ruff format --check .`

Run: `uv run pyright`

Run: `uv run lint-imports`

Run: `git diff --check`

Expected: all commands exit 0.

- [ ] **Step 7: Run full repository tests**

Run: `uv run pytest -q`

Expected: zero unexplained failures. Any pre-existing baseline failure must be reproduced on the Phase 4 merge base and documented by exact node ID; Phase 4-owned failures block completion.

- [ ] **Step 8: Commit documentation/evidence, then run committed-HEAD packaging**

```bash
git add README.md docs/superpowers/specs/2026-08-20-pure-graph-engine-plugin-architecture-design.md
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/ownership.yaml
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/phase5-handoff.md
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/phase6-deletion.txt
git commit -m "docs: close phase 4 capability extraction"
bash scripts/assurance_capability_wheel_smoke_test.sh
bash scripts/packaging_smoke_test.sh
```

Expected: both committed-HEAD scripts exit 0. Append exact results and final SHA to the intentionally ignored final report; do not amend the verified implementation commit solely to embed its own SHA.

- [ ] **Step 9: Verify hard scope exclusions**

Run:

```bash
rg -n "graph_engine\.products|ProductManifest" packages/assurance-{intake,generation,execution,healing,quality,improvement}
rg -n "assurance_agent|assurance_kernel|ProductHooks|current_product_hooks" packages/assurance-{intake,generation,execution,healing,quality,improvement}
rg -n "SessionEvent|opencode|cursor" packages/assurance-{intake,generation,execution,healing,quality,improvement}
```

Expected: all three scans return no production-code matches; documentation/test fixtures may mention forbidden terms only in negative assertions.

## Spec Coverage Matrix

| Phase 4 specification obligation | Implementation-plan evidence |
|---|---|
| Exactly six vertical wheels; no shared Assurance production wheel | Global Constraints; Tasks 3, 5, 8, 10, 12, 15 |
| Exact acyclic dependency graph in metadata, descriptors, declarations, imports, and ledger | Tasks 1, 3/5/8/10/12/15, 17, 19 |
| Complete ownership of operations, skills, models, artifacts, validators, effects, resources, personas, and hooks | Tasks 1, 3–18, 22 |
| Five Phase 2 registries only; authenticated static/live provider declarations | Tasks 2–17, 19 |
| Provider-neutral prepare → Phase 3 adapter → finalize | Tasks 2, 4, 6–7, 9, 11, 14, 16, 20 |
| Exact model routing, no fallback, no provider session duplication | Tasks 4, 20, 21–22 |
| Canonical owner schemas and exact capability/reference closure | Tasks 3–17, 21 |
| Candidate-byte validators and path confinement | Tasks 4, 7, 9, 11, 13–16, 21 |
| Durable effects with idempotency, receipt, reconcile, and crash cuts | Tasks 11, 16, 21 |
| Every `ProductHooks` concern removed from the new path with independent parity evidence | Tasks 11, 14, 16, 18 |
| Legacy/new coexist without cross-imports or persisted-state bridge | Tasks 3–19, 21–22 |
| Committed-HEAD isolated builds and installed resource/source authentication | Task 19, Task 22 |
| Six-wheel test-only composition and OpenCode/Cursor rebinding | Task 20 |
| Security, drift, secret, indeterminate, effect, and filesystem fault matrices | Task 21 |
| No Phase 5 product/graph/CLI cutover and no Phase 6 deletion | Global Constraints; Task 22 handoff only |

## Execution Notes

- Tasks are dependency ordered. Do not begin a downstream wheel before the upstream public contract task is committed and reviewed.
- A task may be split into multiple RED/GREEN commits only if a reviewer can approve each commit independently; never combine changes from two wheel owners in one implementation commit except Tasks 17, 20, 21, and 22, whose purpose is cross-wheel closure.
- Use a fresh implementer context per task under Subagent-Driven execution, followed by separate Spec and Standards reviews. Review findings are fixed in a separate commit and re-reviewed before advancing.
- Treat the ownership ledger as an acceptance oracle, not runtime configuration. Runtime code must never open it.
- Preserve the old runtime exactly enough for Phase 5 behavioral comparison. Do not reduce temporary duplication by adding an import bridge.
- If a new business contract does not fit one owner in the approved DAG, stop and amend the Phase 4 spec rather than creating a shared production package.
- If a capability needs a new engine registry or an adapter needs business fields, stop and amend the relevant Phase 2/3 spec rather than extending the engine in a Phase 4 task.
