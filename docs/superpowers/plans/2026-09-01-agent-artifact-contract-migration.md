# 33-Agent Artifact Contract Retrofit and Checkpoint S Closure Implementation Plan

> Historical implementation plan: Attempt event/journal and action-runtime steps
> are superseded by the [Attempt checkpoint migration](2026-10-09-attempt-checkpoints.md).


> ## CANCELLED / SUPERSEDED — Historical Record Only
>
> **Effective 2026-09-02:** this plan is permanently cancelled and superseded by
> [Permanent Raw Agent Runtime Cutover](../specs/2026-09-02-raw-agent-runtime-cutover-design.md)
> and its replacement implementation plan,
> [Raw Agent Runtime Closure](./2026-09-02-raw-agent-runtime-closure.md).
>
> The text below is retained only as decision history. **Every checkbox in this file is
> non-authoritative and non-executable**: do not use it to start work, infer current program
> status, define a gate, or make a release claim.

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the Capability-owned part of the Structured Artifact Pipeline by retrofitting all 33 Agent contracts and 34 semantic Agent occurrences already referenced by Python StateGraphs with a machine-readable, fail-closed artifact contract; migrating every Feature's result/skill/projector/finalizer/permission/parity closure; consuming the separately qualified OpenCode and Artifact/Kernel gates; correcting the parent plans; and making the aggregate Checkpoint S gate part of CI.

**Architecture:** This plan contributes Feature-owned `ArtifactContract` and ordered `ArtifactSlot` values, installed result/document models and schemas, projectors, finalizers, skills and resource claims. The authoritative Artifact/Kernel plan supplies registry resolution, serializers, materialization, receipts and `ResolvedStructuredAgentExecutor`; the authoritative OpenCode plan supplies transport certification and runtime deny enforcement. Every inventory row starts as a valid raw row, so incomplete typed work remains raw; a named slot changes to typed only in one atomic Feature commit containing its complete evidence set.

**Tech Stack:** Python 3.11, uv workspace, Pydantic v2, OpenCode Server/SDK structured-result protocol, LangGraph `1.2.11`, canonical JSON, YAML 1.2-safe subset, pytest, Ruff, Pyright, import-linter, existing append-only Attempt journal/workspace/fencing primitives.

**Spec:** `docs/superpowers/specs/2026-09-01-structured-artifact-pipeline-design.md`

**Plan shape:** 12 atomic tasks; Task 11 installs the production runtime foundation before external certification, and Task 12 alone consumes promoted certification/binding artifacts to close Checkpoint S.

## Global Constraints

- Execute this plan inside the clean migration worktree selected by the master migration. The current source worktree contains user-owned OpenCode adapter, workspace-baseline, permission, and boundary edits; land or rebase those explicitly before execution and never overwrite, copy, or stage them implicitly.
- This entire child plan is blocked until OpenCode Task 0 closes Checkpoint S0 green for one exact official release. A red S0 leaves all 12 tasks deferred; no Agent contract, schema, skill, projector, finalizer, permission, Feature factory, or Product runtime binding is changed speculatively, and S0 evidence never satisfies `requires_structured_output`.
- This is a Checkpoint S Capability retrofit after Product T5a. Foundation, Semantic Attempt, Feature Tasks 1–9, Product Tasks 1–4, and T5a already provide `ProductLockV3`, `GraphRevision`, `GraphBuildManifest`, `AttemptKey`, Python StateGraphs, fenced journal/storage, resource authorization, isolated workspace, durable prepare/promotion, effects, and system-interrupt primitives. The two Structured child plans named below provide the Adapter and Artifact/Kernel seams; this plan extends and tests the existing implementation but never reimplements it.
- The exact production set is 33 Agent contracts and 34 occurrences: Execution 2/2, Generation 14/14, Healing 2/2, Improvement 6/6, Intake 4/5, Quality 5/5. `assurance.intake.agent.case-design.v1` is the only twice-occurring Agent contract.
- All 33 contracts declare `requires_structured_output=True`, including the raw-artifact-only Quality report. Raw/typed/mixed classifies artifact ownership, not whether `AgentResultT` is structured.
- Every production `AgentExecutionContract` in the Feature's `contracts/attempts.py` declares its Feature-owned `agent_result_schema_id`, `max_schema_bytes`, and `max_result_bytes`. The installed `AgentResultT` model generates the sole runtime schema document/digest; shipped schema JSON is a canonical-equality golden, and Product/OpenCode cannot override any of these fields.
- Use the spec types `ArtifactContract` and `ArtifactSlot`. Do not add a Feature-local artifact-contract type, another serializer registry, or a suffix-derived production authority.
- The closed structured executor is named `ResolvedStructuredAgentExecutor`. Do not retain or introduce the legacy composite-executor name, legacy provider-schema fields, or a second overlapping phase bundle.
- The adapter-specific advertised capability is exactly `opencode_structured_output`. It means the pinned OpenCode `format.type == "json_schema"` / `format.schema` and terminal `info.structured` path passed qualification; it does not claim provider-native `response_format` enforcement.
- `graph-engine` remains provider-neutral and must not import `agent_runtime_contracts`, OpenCode, a Capability package, or Product. Feature packages do not import another Feature's artifacts/projectors. Product may bind runtime/provider/model but may not replace Feature input/prepared/result/output models, slots, projectors, schemas, serializers, resources, or validators.
- No new LangGraph node, edge, route, join, interrupt, retry loop, or public resolution is added. Structured observation, snapshotting, materialization, and finalization stay inside one semantic Attempt.
- Every path-affecting value is closed before `authorize_resources` and before `AttemptKey` derivation. `PreparedT`, OpenCode, `AgentResultT`, finalizers, project configuration, and SUT files cannot expand path authority.
- The public Product input schema adds four required, closed fields: `sut_network_mode: "offline_source" | "bound_sut"`, sorted unique opaque `sut_network_target_ids`, `sut_auth_mode: "none" | "bound_command_secrets"`, and sorted unique opaque `sut_command_secret_requirement_ids`. Before graph dispatch, the Product root adapter validates those IDs against the frozen target catalog and deterministically builds a private canonical `sut_network_selections` tuple of `(requirement_id, sorted target_ids)` plus digest. Each Feature receives only rows for requirements it declares; it never sees or inherits another Feature's targets. The public IDs, private selection rows, and secret-requirement IDs never contain a host, URL, port, DNS answer, proxy, secret value, or secret handle; Product resolves the authenticated target catalog and requirement-to-handle bindings outside graph/business input. Multiple requirements in one `product-full` invocation, missing/extra/cross-requirement IDs, and replay of the private selection map are exact-set tested.
- Network authority is explicit for all 33 contracts: exactly ten are `input_conditioned`—both Execution contracts plus Generation API/E2E/Fuzz/Performance plan-review and primary codegen—and the other 23 are `none`. The four reviewers already conditionally inspect a live OpenAPI/SUT surface; the two codegen-fix contracts remain bounded staged-file repair and do not gain network authority. Command-secret authority is explicit for all 33 contracts: only the two Execution contracts are `input_conditioned` for installed requirement `sut-admin-credential-v1`; the other 31 are `none`. No default, ambient environment, skill text, or prepared/model value may infer either authority.
- The initial checked inventory contains 69 slots: 61 fixed path templates plus one Intake case set, four primary Generation codegen roots, two API/E2E codegen-fix allowed-path sets, and one Healing coverage-repair exact-file set resolved only from validated `brief.allowed_test_files`. Every row has `disposition="raw"`; there is no `pending`, `unknown`, `auto`, or `unclassified` value. The intended typed candidates are recorded separately and have no authority until their atomic migration lands.
- A raw-to-typed change lands only with all catalog-linked tests green. If any evidence is incomplete, discard that typed change and keep the reviewed raw row with an explicit `raw_reason`; Checkpoint S may never close over a partially migrated or candidate-authoritative row.
- A typed slot using `semantic_migration` must reference one Feature-owned installed `ArtifactSemanticMigrationRecord` that binds distinct old/new representation identities, nonblank rationale, approved golden digests, downstream compatibility-test IDs, and its own digest. The record participates in registry, Attempt-contract, ProductLock, and build digests; a test-time flag or implementer-authored waiver cannot substitute for it.
- After all planned typed promotions, the expected catalog is 34 typed slots and 35 raw slots, deriving 14 typed-artifact-only contracts, 18 mixed-artifact contracts, and one raw-artifact-only contract. Initial raw owner totals are Execution 2, Generation 44, Healing 3, Improvement 6, Intake 9, and Quality 5 (69); final retained-raw owner totals are Execution 0, Generation 30, Healing 1, Improvement 0, Intake 3, and Quality 1 (35); final typed owner totals are Execution 2, Generation 14, Healing 2, Improvement 6, Intake 6, and Quality 4 (34). These counts are an expected result, not a suffix shortcut: live slots and evidence remain the authority.
- Intake case documents use a repeatable typed slot resolved from sorted `CaseDesignInputV1.case_delta_paths`; each path is exactly `qa/changes/{change_id}/cases/{module}/case.yaml`. Primary and repair occurrences share the same contract; repair returns complete post-image documents for the already authenticated paths.
- Generation source trees remain raw. Four primary codegen slots bind a family-closed tree root; API/E2E codegen-fix slots bind only a repeatable exact-file set from authenticated `allowed_paths` and never regain whole-root authority. Current Generation Agents mutate those paths only through bounded native raw-write tools and have no formatter, LSP, test-runner, benchmark, or other command authority. Later Execution Attempts may consume the committed/generated raw sources only through their four declared semantic runner requirements.
- Healing coverage repair remains hybrid. Its typed receipt is Kernel-materialized, while the actual test edits are one repeatable raw exact-file slot resolved strictly from validated `CoverageRepairInputV1.brief.allowed_test_files` before the key. Every member must have an existing immutable input-snapshot baseline; both `initial` and `repeat` use `bounded_repair + agent_raw_whole_file + preserve_baseline`. The raw member selector covers the complete bound allowed set rather than trusting the reported subset. Kernel computes `actual_changed_set` from every bound baseline versus the immutable raw post-image/sealed partition and requires `actual_changed_set == CoverageRepairAgentResultV1.files_modified ⊆ prekey_allowed_test_files`. A false-positive report, an unreported allowed-file change, an out-of-set/new/deleted file, or mode drift fails before promotion. No root, result-expanded member, or undeclared test file is authorized.
- Typed targets are absent from OpenCode `allowed_outputs` and all raw mutation permissions. A broad raw root never overrides a typed deny hole. Internal inputs/scratch are explicit non-promotable claims, not a third artifact authority.
- Every Agent contract declares `non_artifact_resources` for its input/scratch write/exclusive subset. At pre-key binding, typed slots, raw slots/roots, and this explicit subset must account for every total write/exclusive claim; an unowned or multiply owned claim fails before authorization.
- OpenCode and its internal `StructuredOutput` tool remain untrusted producers. Kernel validation into installed `AgentResultT` and document models is authoritative even if OpenCode and the adapter already validated the candidate.
- Every raw slot resolves one Feature-owned authenticated `ArtifactRawMemberSelectorEntry`. It returns only per-slot logical-path declarations from validated input/prepared/result values; Kernel closes those paths against pre-key exact/root authority and derives all byte/digest/size/mode truth from the immutable raw post-image. A raw selector is not a typed projector and cannot authorize bytes or paths outside the bound contract.
- Every one of the 33 production contracts resolves same-owner authenticated `StructuredPrepareHandlerEntry` and `StructuredFinalizeHandlerEntry` values. Prepare is exactly `prepare(InputT)` and finalization is exactly `finalize(StructuredFinalizeInput)`; legacy `TaskHandler` aliases delegate during shadow but are not executor authority. All prior file-derived prepare evidence is hydrated into validated `InputT` before key derivation, and all 33 rows carry linked prepare/finalizer purity tests.
- `canonical-json-v1` and `canonical-yaml-v1` have one version authority: the serializer identifier. A behavior change requires a new identifier and normative-corpus digest; no independently mutable numeric serializer version is allowed.
- Preserve the production validator baseline of 25 registered and zero bound. This migration removes duplicate shape parsing only; semantic/cross-file validators, tests, Eval, seal, promotion, effects, and receipts remain.
- Keep the exact current effect set and its ordering/apply/reconcile behavior unchanged: Healing `allocation.v2`, `heal-apply.v2`, `proposal-approved.v1`; Improvement `archive.v1`, `delivery.v1`, `promotion.v1`, each under its existing fully qualified owner prefix. Do not add a custom OpenCode Schema-writer plugin, a private OpenCode fork, cryptographic attestation, or global ACID semantics for external providers.
- Every task follows RED → focused GREEN → exact staging → commit. Never use `git add .`, `git add -A`, broad globs, destructive Git commands, xfail, skip, or a semantic waiver to close an inventory row.
- Run commands from the root of the clean migration worktree selected in migration-master R0 (capture it once with `git rev-parse --show-toplevel`); never hard-code or return to the dirty source-worktree path. Use `uv run ...` for Python tools. The full existing CI gate and all three wheel smoke scripts remain mandatory.

---

## Interfaces

This plan consumes, without redefining, the exact interfaces exported by the two authoritative child plans:

- From `2026-09-01-artifact-kernel-foundation.md`: `ArtifactSlot`, `ArtifactContract`, `ArtifactSemanticMigrationRecord`, `ArtifactProjectionInput`, the installed artifact registries, `MaterializationReceipt`, `StructuredFinalizeInput`, `ResolvedStructuredAgentExecutor`, and the four-type `AgentExecutionContract[InputT, PreparedT, AgentResultT, OutputT]`.
- From `2026-09-01-opencode-structured-output-gate.md`: `StructuredAgentActivityPort`, `StructuredResultSchema`, `StructuredWorkspaceAccess`, provider capability `opencode_structured_output`, and the exact certification-row key.
- From the Semantic Attempt plan: `AttemptKernelPort.execute_or_recover(...)`, authenticated resource closure, and unchanged graph-visible resolution types. This suite replaces that plan's older three-model/composite/provider-schema wording.

Each Feature produces installed contributions with this common factory surface; concrete model types differ per contract:

```python
def agent_result_models() -> tuple[type[FrozenModel], ...]: ...
def artifact_contracts() -> tuple[ArtifactContract, ...]: ...
def artifact_path_resolvers() -> tuple[ArtifactPathResolverEntry, ...]: ...
def artifact_projectors() -> tuple[ArtifactProjectorEntry, ...]: ...
def artifact_raw_member_selectors() -> tuple[ArtifactRawMemberSelectorEntry, ...]: ...
def artifact_document_models() -> tuple[type[FrozenModel], ...]: ...
def artifact_semantic_migrations() -> tuple[ArtifactSemanticMigrationRecord, ...]: ...
def structured_prepare_handlers() -> tuple[StructuredPrepareHandlerEntry, ...]: ...
def structured_finalize_handlers() -> tuple[StructuredFinalizeHandlerEntry, ...]: ...
```

The Feature factory tuples are sorted by canonical ID and owner-closed by Boot. The prepare/finalize tuples cover every Feature-owned production Agent contract exactly once, and their IDs/provenance exactly match `AgentExecutionContract.prepare_handler_id`/`finalize_handler_id`. They contain data/code contributions only: no provider/model/runtime choice, serializer implementation, materializer, workspace writer, transaction callback, or graph topology.

Every Feature wave below has one non-optional handler-closure substep even when its Files list does not repeat the rule: modify that Feature's `contracts/attempts.py`, `plugin.py`, `plugin-declaration.json`, owning operation modules, and Feature artifact-pipeline test; publish the exact pure prepare/finalize entries; retain legacy TaskHandlers only as delegates during shadow; and assert owner/ID/provenance set equality plus one-argument/no-context purity for every migrated contract. The aggregate architecture test requires exactly 33 prepare and 33 finalize entries, no extras, with 33/33 linked purity rows. Each wave's explicit Files and `git add` list must include those paths.

Exactly these 17 contracts currently have prepare-time ambient file reads and must be closed explicitly, not hidden behind a generic context:

```python
LEGACY_AMBIENT_PREPARE_CONTRACT_IDS = frozenset({
    "assurance.execution.agent.execute.v1",
    "assurance.execution.agent.run.v1",
    "assurance.intake.agent.explore.v1",
    "assurance.intake.agent.case-design.v1",
    "assurance.intake.agent.case-review.v1",
    "assurance.generation.agent.api.plan.v1",
    "assurance.generation.agent.api.plan-review.v1",
    "assurance.generation.agent.api.codegen.v1",
    "assurance.generation.agent.e2e.plan.v1",
    "assurance.generation.agent.e2e.plan-review.v1",
    "assurance.generation.agent.e2e.codegen.v1",
    "assurance.generation.agent.fuzz.plan.v1",
    "assurance.generation.agent.fuzz.plan-review.v1",
    "assurance.generation.agent.fuzz.codegen.v1",
    "assurance.generation.agent.performance.plan.v1",
    "assurance.generation.agent.performance.plan-review.v1",
    "assurance.generation.agent.performance.codegen.v1",
})
```

The separate toolchain/network/command-secret classifications are frozen as exact reviewed sets, not inferred from an Agent profile, artifact kind, skill prose, or a concrete executable. Task 1 uses the following primitive mirrors; Artifact Task 4 supplies the sole production `StructuredToolchainRequirement`, `StructuredCommandSecretRequirement`, and `AgentExecutionAuthority` types. No Feature-local production authority/requirement type is permitted:

```python
class StructuredToolchainRequirementRow(FrozenModel):
    requirement_id: str
    purpose: str
    allowed_logical_recipe_ids: tuple[str, ...]
    immutable_dependency_class_id: str
    environment_policy_id: str
    scratch_output_policy_id: str
    requirement_digest: str


class StructuredCommandSecretRequirementRow(FrozenModel):
    requirement_id: str
    selection_mode: Literal["required", "input_conditioned"]
    environment_alias_keys: tuple[str, ...]
    purpose: str
    delivery_mode: Literal["one_shot_env_v1"]
    value_encoding: Literal["utf8_no_nul_v1"]
    allowed_target_classes: tuple[str, ...]
    max_value_bytes: int
    requirement_digest: str


class AgentExecutionAuthorityRow(FrozenModel):
    contract_id: str
    structured_toolchain_requirement_ids: tuple[str, ...]
    structured_network_mode: Literal["none", "required", "input_conditioned"]
    structured_network_requirement_ids: tuple[str, ...]
    structured_command_secret_mode: Literal["none", "required", "input_conditioned"]
    structured_command_secret_requirements: tuple[StructuredCommandSecretRequirementRow, ...]
    authority_digest: str
```

Each secret requirement row contains the fields above with sorted aliases/target classes, positive bounded bytes, and its canonical digest. Product installs exactly four provider-neutral toolchain semantic rows. These IDs describe business runner recipes; they never name `uv`, Python, pytest, Playwright, Locust, a binary/path, concrete argv, environment value, image, sandbox, or backend:

| Requirement ID | Allowed logical recipe | Immutable dependency class | Environment policy | Scratch/output policy |
| --- | --- | --- | --- | --- |
| `sut-api-test-runner-v1` | execute only API pytest-style node IDs in the authenticated closed mapping; no discovery outside it | `sut-api-test-dependencies-v1` | `hermetic-python-test-env-v1` | `external-batch-test-scratch-v1` |
| `sut-e2e-test-runner-v1` | execute only E2E browser-test node IDs in the authenticated closed mapping | `sut-e2e-test-dependencies-v1` | `hermetic-python-test-env-v1` | `external-batch-browser-scratch-v1` |
| `sut-fuzz-test-runner-v1` | execute only Fuzz property-test node IDs in the authenticated closed mapping | `sut-fuzz-test-dependencies-v1` | `hermetic-python-test-env-v1` | `external-batch-fuzz-scratch-v1` |
| `sut-performance-benchmark-runner-v1` | execute the exact mapped benchmark file with authenticated bounded users/spawn-rate/duration and no test discovery | `sut-performance-test-dependencies-v1` | `hermetic-python-benchmark-env-v1` | `external-batch-benchmark-scratch-v1` |

The following constructor tuple—not the explanatory prose in the table—is the exact value authority. `_requirement(...)` computes `requirement_digest = sha256(canonical_json_bytes(payload)).hexdigest()` over the six fields preceding the digest, with Pydantic JSON-mode strings and tuple order unchanged:

```python
def _requirement(
    *,
    requirement_id: str,
    purpose: str,
    allowed_logical_recipe_ids: tuple[str, ...],
    immutable_dependency_class_id: str,
    environment_policy_id: str,
    scratch_output_policy_id: str,
) -> StructuredToolchainRequirementRow:
    payload = {
        "requirement_id": requirement_id,
        "purpose": purpose,
        "allowed_logical_recipe_ids": allowed_logical_recipe_ids,
        "immutable_dependency_class_id": immutable_dependency_class_id,
        "environment_policy_id": environment_policy_id,
        "scratch_output_policy_id": scratch_output_policy_id,
    }
    return StructuredToolchainRequirementRow(
        **payload,
        requirement_digest=sha256(canonical_json_bytes(payload)).hexdigest(),
    )


TOOLCHAIN_REQUIREMENTS = (
    _requirement(
        requirement_id="sut-api-test-runner-v1",
        purpose="Execute only authenticated closed-mapping API tests.",
        allowed_logical_recipe_ids=("closed-api-tests-v1",),
        immutable_dependency_class_id="sut-api-test-dependencies-v1",
        environment_policy_id="hermetic-python-test-env-v1",
        scratch_output_policy_id="external-batch-test-scratch-v1",
    ),
    _requirement(
        requirement_id="sut-e2e-test-runner-v1",
        purpose="Execute only authenticated closed-mapping E2E browser tests.",
        allowed_logical_recipe_ids=("closed-e2e-tests-v1",),
        immutable_dependency_class_id="sut-e2e-test-dependencies-v1",
        environment_policy_id="hermetic-python-test-env-v1",
        scratch_output_policy_id="external-batch-browser-scratch-v1",
    ),
    _requirement(
        requirement_id="sut-fuzz-test-runner-v1",
        purpose="Execute only authenticated closed-mapping fuzz tests.",
        allowed_logical_recipe_ids=("closed-fuzz-tests-v1",),
        immutable_dependency_class_id="sut-fuzz-test-dependencies-v1",
        environment_policy_id="hermetic-python-test-env-v1",
        scratch_output_policy_id="external-batch-fuzz-scratch-v1",
    ),
    _requirement(
        requirement_id="sut-performance-benchmark-runner-v1",
        purpose="Execute only the authenticated bounded performance benchmark.",
        allowed_logical_recipe_ids=("closed-performance-benchmark-v1",),
        immutable_dependency_class_id="sut-performance-test-dependencies-v1",
        environment_policy_id="hermetic-python-benchmark-env-v1",
        scratch_output_policy_id="external-batch-benchmark-scratch-v1",
    ),
)
```

The canonical rows use Artifact Task 4's fields `(requirement_id, purpose, allowed_logical_recipe_ids, immutable_dependency_class_id, environment_policy_id, scratch_output_policy_id, requirement_digest)`. Their normative recipes require Product-derived argv execution, a cleared allowlisted environment, no install-on-demand, no project-local dependency/cache mutation, exact selected inputs, and batch-scoped nonpromotable scratch. OpenCode Task 6 maps them to qualified concrete Product profiles and command grammars; a future formatter, LSP, code-generator, or different runner is a new requirement plus plan/spec revision, never an inferred extension.

The two Execution `InputT` models also carry the sole per-Attempt `StructuredToolchainInvocationSelection`, using the core-owned Artifact Task 5 value type. It is constructed before `bind_attempt_dispatch(...)` from authenticated case/family/runner data, never by the model or skill. API/E2E/Fuzz rows carry one logical recipe ID plus a sorted exact tuple of selected test node IDs; the Performance row carries `closed-performance-benchmark-v1`, the one authenticated benchmark file, and positive bounded integer `users`, `spawn_rate_millis_per_second`, and `duration_seconds`. Floating-point spawn rate is forbidden. The Feature model requires exact equality to its closed selected-test/benchmark mapping and canonical digest; the binder freezes it with input/requirement/profile digests into `BoundStructuredToolchainInvocationScope`. All other 31 contracts carry the canonical empty selection and no scope. A successful Execution Attempt invokes each selected recipe exactly once. Unknown, duplicate, cross-family/cross-contract, broadened or narrowed node sets, parameter mismatch/overflow, model-supplied argv/executable/environment, or replay drift fails before the key or at the broker without launching a command.

```python
EXECUTION_RUNNER_REQUIREMENT_IDS = (
    "sut-api-test-runner-v1",
    "sut-e2e-test-runner-v1",
    "sut-fuzz-test-runner-v1",
    "sut-performance-benchmark-runner-v1",
)

EXPECTED_TOOLCHAIN_REQUIREMENT_IDS_BY_CONTRACT = {
    "assurance.execution.agent.execute.v1": EXECUTION_RUNNER_REQUIREMENT_IDS,
    "assurance.execution.agent.run.v1": EXECUTION_RUNNER_REQUIREMENT_IDS,
    "assurance.generation.agent.api.codegen-fix.v1": (),
    "assurance.generation.agent.api.codegen.v1": (),
    "assurance.generation.agent.api.plan-review.v1": (),
    "assurance.generation.agent.api.plan.v1": (),
    "assurance.generation.agent.e2e.codegen-fix.v1": (),
    "assurance.generation.agent.e2e.codegen.v1": (),
    "assurance.generation.agent.e2e.plan-review.v1": (),
    "assurance.generation.agent.e2e.plan.v1": (),
    "assurance.generation.agent.fuzz.codegen.v1": (),
    "assurance.generation.agent.fuzz.plan-review.v1": (),
    "assurance.generation.agent.fuzz.plan.v1": (),
    "assurance.generation.agent.performance.codegen.v1": (),
    "assurance.generation.agent.performance.plan-review.v1": (),
    "assurance.generation.agent.performance.plan.v1": (),
    "assurance.healing.agent.coverage-repair.v1": (),
    "assurance.healing.agent.fix-proposal.v1": (),
    "assurance.improvement.agent.archive.v1": (),
    "assurance.improvement.agent.improvement-review.v1": (),
    "assurance.improvement.agent.retro-eval-analysis.v1": (),
    "assurance.improvement.agent.retro-issue-analysis.v1": (),
    "assurance.improvement.agent.retro-workflow-analysis.v1": (),
    "assurance.improvement.agent.retro.v1": (),
    "assurance.intake.agent.case-design.v1": (),
    "assurance.intake.agent.case-review.v1": (),
    "assurance.intake.agent.explore.v1": (),
    "assurance.intake.agent.intake.v1": (),
    "assurance.quality.agent.fact-baseline.v1": (),
    "assurance.quality.agent.inspect.v1": (),
    "assurance.quality.agent.issue-analysis.v1": (),
    "assurance.quality.agent.issue-triage.v1": (),
    "assurance.quality.agent.report.v1": (),
}
```

The mapping has exactly `2` rows with all four runner requirements and `31` empty rows. In particular all 14 Generation rows are empty: their current skills use bounded native read/write only, and API/E2E codegen explicitly must not run the product test runner.

```python
INPUT_CONDITIONED_NETWORK_CONTRACT_IDS = frozenset({
    "assurance.execution.agent.execute.v1",
    "assurance.execution.agent.run.v1",
    "assurance.generation.agent.api.plan-review.v1",
    "assurance.generation.agent.api.codegen.v1",
    "assurance.generation.agent.e2e.plan-review.v1",
    "assurance.generation.agent.e2e.codegen.v1",
    "assurance.generation.agent.fuzz.plan-review.v1",
    "assurance.generation.agent.fuzz.codegen.v1",
    "assurance.generation.agent.performance.plan-review.v1",
    "assurance.generation.agent.performance.codegen.v1",
})

INPUT_CONDITIONED_COMMAND_SECRET_CONTRACT_IDS = frozenset({
    "assurance.execution.agent.execute.v1",
    "assurance.execution.agent.run.v1",
})
```

Every other exact catalog ID declares `structured_network_mode="none"` and `structured_command_secret_mode="none"` with empty requirement sets. Do not use one broad shared runtime-target requirement. Product installs exactly three provider-neutral network requirement rows: `sut-openapi-read-v1` admits target class `sut-openapi` with GET/HEAD/OPTIONS only, Product-bound read-only OpenAPI path scope, and no redirect expansion; `sut-test-backend-v1` admits only target class `sut-test-backend` and bounded test traffic; `sut-test-frontend-v1` admits only target class `sut-test-frontend` plus Product-declared subresource origins. All eight Generation network-capable contracts—the four plan-review and four primary codegen rows—declare only `sut-openapi-read-v1`; their current live access is OpenAPI/route inspection, not test execution. Both Execution contracts declare backend plus frontend and derive their exact required subset from frozen selected-case/family data. A Feature `InputT` carries only Product-created `sut_network_selections` rows for that declared subset. The OpenAPI requirement is implemented by a Product L7 synthetic-origin proxy that terminates the sandbox side, canonicalizes method/path, rejects CONNECT/upgrade/non-read methods/non-OpenAPI paths/redirect escape, and rebuilds upstream TLS under an authenticated CA/pin/hostname-verification policy digest carried in ProductLock/access/qualification/receipt. The backend/frontend requirements use separately qualified target-class policies. The gateway enforces target class, transport, aliases, HTTP methods/path scope, subresource origins, redirect policy, cardinality, connections, bytes, and duration; therefore Generation cannot write to or select an Execution target.

The two credential-capable Execution rows additionally declare the exact canonical value below. `_secret_requirement(...)` hashes the canonical JSON payload over every field except `requirement_digest`, identically to the toolchain helper; there is no implementation-chosen purpose, target class, or byte limit:

```python
def _secret_requirement(
    *,
    requirement_id: str,
    selection_mode: Literal["required", "input_conditioned"],
    environment_alias_keys: tuple[str, ...],
    purpose: str,
    delivery_mode: Literal["one_shot_env_v1"],
    value_encoding: Literal["utf8_no_nul_v1"],
    allowed_target_classes: tuple[str, ...],
    max_value_bytes: int,
) -> StructuredCommandSecretRequirementRow:
    payload = {
        "requirement_id": requirement_id,
        "selection_mode": selection_mode,
        "environment_alias_keys": environment_alias_keys,
        "purpose": purpose,
        "delivery_mode": delivery_mode,
        "value_encoding": value_encoding,
        "allowed_target_classes": allowed_target_classes,
        "max_value_bytes": max_value_bytes,
    }
    return StructuredCommandSecretRequirementRow(
        **payload,
        requirement_digest=sha256(canonical_json_bytes(payload)).hexdigest(),
    )


SUT_ADMIN_CREDENTIAL_REQUIREMENT = _secret_requirement(
    requirement_id="sut-admin-credential-v1",
    selection_mode="input_conditioned",
    environment_alias_keys=("AA_ADMIN_PASSWORD", "QA_ADMIN_PASSWORD"),
    purpose="Authenticate selected administrative SUT test traffic.",
    delivery_mode="one_shot_env_v1",
    value_encoding="utf8_no_nul_v1",
    allowed_target_classes=("sut-test-backend", "sut-test-frontend"),
    max_value_bytes=16_384,
)
```

These are provider-neutral requirement identifiers and alias contracts, never endpoints or values. Execution `InputT` includes an authenticated `requires_admin_auth` predicate derived from the frozen typed case/fixture contract, not from `bound_sut`, node identity, a skill, or the model. The legal combinations are: `offline_source + none`; `bound_sut + none` when that predicate is false and injection count is zero; and `bound_sut + bound_command_secrets` with exactly `sut-admin-credential-v1` when it is true. `offline_source + bound_command_secrets`, a missing credential for an authenticated case, or an extra credential for an unauthenticated case fails before key/workspace creation. Both Generation codegen-fix contracts remain offline/network-none; live SUT/OpenAPI access stays only in the four reviewed plan-review and four primary codegen contracts. Architecture tests assert the exact per-contract requirement mapping, `10/23` and `2/31` partitions, all four Execution mode combinations, and reject every unresolved or multiply classified row.

For those IDs the graph projection hydrates every bounded prior evidence value needed by prepare into the exact validated `InputT` before `bind_attempt_dispatch`; prepare receives only that `InputT` and performs no path lookup or file read. Separately, any source/test/config bytes the Agent itself must inspect are represented by sorted, closed `sut_read_paths`/root claims derived from validated `InputT` and captured by Kernel into the immutable Attempt input snapshot. Task 2 adds required, sorted `sut_read_paths` plus the four required network/auth selector fields to the public Product input schema, checkpoints the internal `ResolvedProductInputV1`, propagates each exact tuple through coexistence graph inputs, and atomically installs the complete 33-row provider-neutral execution-authority table before OpenCode Task 6 or Artifact Task 9. Feature code may narrow but never enumerate/expand it after the root adapter validates it. Only the two Execution contracts declare `EXECUTION_RUNNER_REQUIREMENT_IDS`; all other 31 rows declare an empty tuple. Their authority rows also explicitly declare network/secret modes and requirements. Artifact Task 5 resolves the semantic toolchain IDs to one Product-installed `ResolvedStructuredToolchainProfile`; OpenCode Task 6 owns the concrete qualified sandbox profile, default-deny network backend, Product target catalog, and write-only command-secret injection backend. The resolved values authenticate executable allowlists, immutable dependency/image roots, environment, target/address/alias closure, requirement-to-handle identities, qualification digests, and limits in ProductLock, dispatch/key, workspace/activity binding, and receipt. Neither `reads=("qa",)` alone nor an undeclared live-project/venv/cache mount, endpoint, or credential is sufficient; missing input, toolchain, target, or command-secret closure fails before activity dispatch. Architecture tests require exact 17-ID ambient-read closure, exact `2/31` toolchain, `10/23` network, and `2/31` command-secret classifications, plus no Feature-loaded command/network/secret backend.

The machine-readable inventory freezes mutation per slot, semantic occurrence, and graph-owned business phase; it never stores one contract-wide scalar:

```python
class AgentArtifactMutationRuleRow(FrozenModel):
    semantic_occurrence_id: str
    phase: Literal["initial", "repeat", "repair"]
    mutation: Literal["create", "replace", "bounded_repair"]
    repair_strategy: Literal["complete_post_image", "agent_raw_whole_file"] | None


class AgentArtifactInventoryRow(FrozenModel):
    feature_id: str
    contract_id: str
    occurrence_ids: tuple[str, ...]
    skill_id: str
    finalizer_id: str
    slot_id: str
    path_template_or_resolver: str
    cardinality: Literal["one", "repeatable"]
    target_kind: Literal["exact_file", "tree_root"]
    candidate_kind: Literal["structured", "raw"]
    content_authority: Literal[
        "agent_structured_value",
        "deterministic_derived_value",
        "agent_raw_bytes",
    ]
    disposition: Literal["typed", "raw"]
    raw_reason: str | None
    document_model_id: str | None
    schema_id: str | None
    schema_digest: str | None
    media_codec: Literal["json", "yaml"] | None
    serializer_id: str | None
    projector_id: str | None
    raw_member_selector_id: str | None
    mutation_rules: tuple[AgentArtifactMutationRuleRow, ...]
    mode_policy: Literal["fixed", "preserve_baseline"]
    fixed_mode: int | None
    max_items: int
    max_bytes: int
    semantic_validator_ids: tuple[str, ...]
    parity_mode: Literal["exact_bytes", "semantic_migration"] | None
    semantic_migration_record_id: str | None
    parity_test_ids: tuple[str, ...]
    permission_test_ids: tuple[str, ...]
    recovery_test_ids: tuple[str, ...]
    prepare_purity_test_ids: tuple[str, ...]
    finalizer_purity_test_ids: tuple[str, ...]
```

For raw rows, `content_authority="agent_raw_bytes"`, all typed-only fields (including `semantic_migration_record_id`) are `None`, `raw_member_selector_id` is present, and `raw_reason` is nonempty. Exactly four reviewed primary generated-source rows have `target_kind="tree_root"`; the other 65 rows are `exact_file`, including the two repeatable Generation fix allowed-path sets and the repeatable Healing coverage-repair set, and every typed row is exact-file. For typed rows, `content_authority` is explicitly either `agent_structured_value` or `deterministic_derived_value`, `raw_reason` and `raw_member_selector_id` are `None`, and every model/schema/codec/serializer/projector/parity field is present. The named `projector_id` is the source of the document value for both typed authorities; inventory equality tests authenticate it against the live Feature contribution. `exact_bytes` requires a null migration-record ID; `semantic_migration` requires an ID that resolves to the same Feature owner's authenticated record. `mutation_rules` is sorted/unique and exactly equals the live slot policy: every owned occurrence has precisely the phases derived by its Artifact contract, with no fallback or wildcard. Raw bounded repair requires `agent_raw_whole_file`; after the same row becomes typed, its rule changes atomically to `complete_post_image`. Every row has nonempty purity-test IDs for its owning contract, and the generator requires exact 33-contract coverage. The generator rejects every other shape.

## Exact 33-contract catalog and intended slot classification

`Q` below means the exact prefix `qa/changes/{change_id}/`. `T` is an intended typed slot and `R` is a retained raw slot. The implementation begins with all rows disposition `raw`; the `T` annotation is only the named promotion target for the Feature task.

| Feature | Exact contract ID | Exact slots after the full planned migration | Derived mode |
| --- | --- | --- | --- |
| Execution | `assurance.execution.agent.execute.v1` | `T execution/execute-result.json` | typed-only |
| Execution | `assurance.execution.agent.run.v1` | `T execution/run-result.json` | typed-only |
| Generation | `assurance.generation.agent.api.codegen-fix.v1` | `R codegen/api-codegen-fix-summary.md`; `T codegen/api-generated-files.json`; repeatable `R exact files from allowed_paths` | mixed |
| Generation | `assurance.generation.agent.api.codegen.v1` | `R codegen/api-codegen-summary.md`; `T codegen/api-generated-files.json`; `R generated/api/files/**` | mixed |
| Generation | `assurance.generation.agent.api.plan-review.v1` | `T review/api-plan-review.json`; `R review/api-plan-review-summary.md` | mixed |
| Generation | `assurance.generation.agent.api.plan.v1` | `R plans/api-plan.md`; `R plans/api-test-data-plan.md`; `R plans/api-codegen-plan.md`; `T plans/api-codegen-mapping.json`; `R plans/m3-review-summary.md` | mixed |
| Generation | `assurance.generation.agent.e2e.codegen-fix.v1` | `R codegen/e2e-codegen-fix-summary.md`; `T codegen/e2e-generated-files.json`; repeatable `R exact files from allowed_paths` | mixed |
| Generation | `assurance.generation.agent.e2e.codegen.v1` | `R codegen/e2e-codegen-summary.md`; `T codegen/e2e-generated-files.json`; `R generated/e2e/files/**` | mixed |
| Generation | `assurance.generation.agent.e2e.plan-review.v1` | `T review/e2e-plan-review.json`; `R review/e2e-plan-review-summary.md` | mixed |
| Generation | `assurance.generation.agent.e2e.plan.v1` | `R plans/e2e-plan.md`; `R plans/e2e-test-data-plan.md`; `R plans/e2e-codegen-plan.md`; `T plans/e2e-codegen-mapping.json`; `R plans/m4-review-summary.md` | mixed |
| Generation | `assurance.generation.agent.fuzz.codegen.v1` | `R codegen/fuzz-codegen-summary.md`; `T codegen/fuzz-generated-files.json`; `R generated/fuzz/files/**` | mixed |
| Generation | `assurance.generation.agent.fuzz.plan-review.v1` | `T review/fuzz-plan-review.json`; `R review/fuzz-plan-review-summary.md` | mixed |
| Generation | `assurance.generation.agent.fuzz.plan.v1` | `R plans/fuzz-plan.md`; `R plans/fuzz-codegen-plan.md`; `T plans/fuzz-codegen-mapping.json`; `R plans/fuzz-review-summary.md` | mixed |
| Generation | `assurance.generation.agent.performance.codegen.v1` | `R codegen/performance-codegen-summary.md`; `T codegen/performance-generated-files.json`; `R generated/performance/files/**` | mixed |
| Generation | `assurance.generation.agent.performance.plan-review.v1` | `T review/performance-plan-review.json`; `R review/performance-plan-review-summary.md` | mixed |
| Generation | `assurance.generation.agent.performance.plan.v1` | `R plans/performance-plan.md`; `R plans/performance-codegen-plan.md`; `T plans/performance-codegen-mapping.json`; `R plans/performance-review-summary.md` | mixed |
| Healing | `assurance.healing.agent.coverage-repair.v1` | `T healing/coverage-repair.json`; repeatable `R exact files from brief.allowed_test_files` | mixed |
| Healing | `assurance.healing.agent.fix-proposal.v1` | `T healing/fix-proposal.json` | typed-only |
| Improvement | `assurance.improvement.agent.archive.v1` | `T archive/archive-receipt.json` | typed-only |
| Improvement | `assurance.improvement.agent.improvement-review.v1` | `T review/improvement-review.json` | typed-only |
| Improvement | `assurance.improvement.agent.retro-eval-analysis.v1` | `T retro/retro-eval-analysis.json` | typed-only |
| Improvement | `assurance.improvement.agent.retro-issue-analysis.v1` | `T retro/retro-issue-analysis.json` | typed-only |
| Improvement | `assurance.improvement.agent.retro-workflow-analysis.v1` | `T retro/retro-workflow-analysis.json` | typed-only |
| Improvement | `assurance.improvement.agent.retro.v1` | `T retro/retro.json` | typed-only |
| Intake | `assurance.intake.agent.case-design.v1` | `T .qa.yaml`; `R proposal.md`; `T trace/minimum-coverage-matrix.json`; repeatable `T cases/{module}/case.yaml` from `case_delta_paths` | mixed |
| Intake | `assurance.intake.agent.case-review.v1` | `T review/case-review.json`; `R review/case-review-summary.md` | mixed |
| Intake | `assurance.intake.agent.explore.v1` | `T explore/exploration.json` | typed-only |
| Intake | `assurance.intake.agent.intake.v1` | `T .qa.yaml`; `R requirement.md` | mixed |
| Quality | `assurance.quality.agent.fact-baseline.v1` | `T facts/fact-baseline.json` | typed-only |
| Quality | `assurance.quality.agent.inspect.v1` | `T inspect/inspection.json` | typed-only |
| Quality | `assurance.quality.agent.issue-analysis.v1` | `T inspect/issue-analysis.json` | typed-only |
| Quality | `assurance.quality.agent.issue-triage.v1` | `T inspect/issue-triage.json` | typed-only |
| Quality | `assurance.quality.agent.report.v1` | `R report/report.md` | raw-only |

The table expands to 69 slots: 34 named typed candidates and 35 retained raw slots. Each suffix is prefixed by `Q`; root/set rows have repeatable cardinality and fixed size/item limits in the Feature contract. The Healing coverage-repair set is not a root: it contains only canonical exact files strictly resolved from validated `CoverageRepairInputV1.brief.allowed_test_files`.

## Exact mutation-phase matrix

The following is the deletion-safe mutation authority. It is expanded into explicit `AgentArtifactMutationRuleRow` values for every slot; “all other” is only shorthand in this plan and never becomes a runtime default or wildcard.

- Exactly 17 of the 34 semantic occurrences are reactivatable: the eight Generation `plan`/`plan-review` occurrences; Intake `case-design.primary`, `case-design.repair`, and `case-review`; Execution `run`; Quality `fact-baseline`, `inspect`, and `issue-analysis`; Healing `fix-proposal` and `coverage-repair`.
- Sixteen of those expose exactly `initial + repeat`. For every slot in Generation plan/plan-review, Execution run, Intake case-review, the three Quality occurrences, Healing fix-proposal, and the typed coverage-repair receipt, `initial=create` and `repeat=replace`; the coverage-repair raw file-set exception is defined below.
- `intake.case-design.primary` is also `initial + repeat`, but policy is per slot: `.qa.yaml` is `initial=replace`; `proposal.md`, `trace/minimum-coverage-matrix.json`, and repeatable `cases/{module}/case.yaml` are `initial=create`; every `repeat` rule is `bounded_repair`, using `complete_post_image` for typed slots and `agent_raw_whole_file` for the retained raw proposal.
- `intake.case-design.repair` exposes only `repair`; every slot is `bounded_repair` with the same typed/raw strategy split. It can recur in later outer rounds without acquiring an `initial` rule.
- `generation.api.codegen-fix` and `generation.e2e.codegen-fix` expose only `repair`: each distinct `*-codegen-fix-summary.md` is `create`; the shared `*-generated-files.json` is `bounded_repair + complete_post_image` after typed promotion (and `agent_raw_whole_file` in the raw baseline); the shared generated members are a repeatable exact-file set with `bounded_repair + agent_raw_whole_file` over only pre-key authenticated `allowed_paths`, never a root claim. The route must prove the corresponding primary codegen committed before fix, so the shared targets exist.
- `healing.coverage-repair` exposes `initial + repeat`, but its two slots intentionally differ: `healing/coverage-repair.json` is `initial=create`, `repeat=replace`; the repeatable raw exact-file set from `brief.allowed_test_files` is `bounded_repair + agent_raw_whole_file + preserve_baseline` in both phases. Every bound member must exist in the immutable input snapshot before dispatch, and its raw selector covers that complete set. Kernel compares all baselines with the immutable raw post-image/sealed partition and requires `actual_changed_set == CoverageRepairAgentResultV1.files_modified ⊆ prekey_allowed_test_files`; false-positive reports, unreported changes, adds/deletes, and mode drift fail at close/seal.
- Every other semantic occurrence exposes only `initial`; every remaining slot is `create`. Cross-contract shared paths are not silently upgraded to replace: the two codegen-fix sets, Intake `.qa.yaml`, and Healing coverage-repair set above are the complete reviewed exceptions.

Mode and size values are equally closed. Every slot uses `fixed` mode `0o644` except Intake case-design `.qa.yaml`, the API/E2E codegen-fix shared manifest/allowed-file-set slots, and the Healing coverage-repair exact-file set, which use `preserve_baseline` and therefore have `fixed_mode=None`; every fixed row has `fixed_mode=0o644`. Nonrepeatable exact files have `max_items=1` and `max_bytes=8 * 1024 * 1024`. The repeatable Intake case-document slot has `max_items=512`, `max_bytes=64 * 1024 * 1024` aggregate. Each primary generated-source tree root, each repeatable codegen-fix exact-file set, and the coverage-repair exact-file set has `max_items=4096`, `max_bytes=512 * 1024 * 1024` aggregate. These limits are checked before buffering/scanning past the bound and are part of the contract/inventory digest. A characterization exceeding one of them is a migration stop requiring an explicit spec/plan revision, not permission to infer a larger production value.

Mutation phase is a separate graph-owned `AttemptNodeFactory` selector, not a field in public or Feature business `InputT`. During Checkpoint S, retrofit the already-installed selectors so they persist phase alongside seven private inbox families (four Generation plan-round, one Intake advance, Product failed, and Product coverage): first distinct dispatch of one of the 16 dual-phase semantic occurrences uses `initial`, a distinct later/late trigger uses `repeat`, exact arrival replay preserves the phase, case-design's internal validation repair uses `repair`, and API/E2E codegen-fix use constant `repair`. The existing `BusinessActivation` and phase selectors each run once against the same anchored state, are frozen together before binding, and are never inferred from round numbers or InputT. `bind_attempt_dispatch` binds phase before key derivation; BusinessActivation already enters the key directly. A prepare handler may not infer or overwrite either value from the filesystem. Ten Agent-dependent roots remain on legacy during this retrofit, while four direct/non-Agent T5a roots remain on LangGraph.

## Exact 34-occurrence mapping

The test-owned occurrence inventory stores `(target_semantic_node_id, legacy_anchor, contract_id)` exactly as follows. The target semantic node ID is stable installed code; the legacy anchor is retained only for coexistence parity and can survive YAML deletion as checked evidence.

```python
EXPECTED_AGENT_OCCURRENCES = (
    ("execution.execute", "assurance.execution.workflow:execution-execute/execute", "assurance.execution.agent.execute.v1"),
    ("execution.run", "assurance.execution.workflow:execution-run/execute", "assurance.execution.agent.run.v1"),
    ("generation.api.plan", "assurance.generation.workflow:generation-api-plan/execute", "assurance.generation.agent.api.plan.v1"),
    ("generation.api.plan-review", "assurance.generation.workflow:generation-api-plan-review/execute", "assurance.generation.agent.api.plan-review.v1"),
    ("generation.api.codegen", "assurance.generation.workflow:generation-api-codegen/execute", "assurance.generation.agent.api.codegen.v1"),
    ("generation.api.codegen-fix", "assurance.generation.workflow:generation-api-codegen-fix/execute", "assurance.generation.agent.api.codegen-fix.v1"),
    ("generation.e2e.plan", "assurance.generation.workflow:generation-e2e-plan/execute", "assurance.generation.agent.e2e.plan.v1"),
    ("generation.e2e.plan-review", "assurance.generation.workflow:generation-e2e-plan-review/execute", "assurance.generation.agent.e2e.plan-review.v1"),
    ("generation.e2e.codegen", "assurance.generation.workflow:generation-e2e-codegen/execute", "assurance.generation.agent.e2e.codegen.v1"),
    ("generation.e2e.codegen-fix", "assurance.generation.workflow:generation-e2e-codegen-fix/execute", "assurance.generation.agent.e2e.codegen-fix.v1"),
    ("generation.fuzz.plan", "assurance.generation.workflow:generation-fuzz-plan/execute", "assurance.generation.agent.fuzz.plan.v1"),
    ("generation.fuzz.plan-review", "assurance.generation.workflow:generation-fuzz-plan-review/execute", "assurance.generation.agent.fuzz.plan-review.v1"),
    ("generation.fuzz.codegen", "assurance.generation.workflow:generation-fuzz-codegen/execute", "assurance.generation.agent.fuzz.codegen.v1"),
    ("generation.performance.plan", "assurance.generation.workflow:generation-performance-plan/execute", "assurance.generation.agent.performance.plan.v1"),
    ("generation.performance.plan-review", "assurance.generation.workflow:generation-performance-plan-review/execute", "assurance.generation.agent.performance.plan-review.v1"),
    ("generation.performance.codegen", "assurance.generation.workflow:generation-performance-codegen/execute", "assurance.generation.agent.performance.codegen.v1"),
    ("healing.fix-proposal", "assurance.healing.workflow:healing-fix-proposal/execute", "assurance.healing.agent.fix-proposal.v1"),
    ("healing.coverage-repair", "assurance.healing.workflow:healing-coverage-repair/execute", "assurance.healing.agent.coverage-repair.v1"),
    ("improvement.archive", "assurance.improvement.workflow:improvement-archive/execute", "assurance.improvement.agent.archive.v1"),
    ("improvement.retro", "assurance.improvement.workflow:improvement-retro/execute", "assurance.improvement.agent.retro.v1"),
    ("improvement.retro-eval-analysis", "assurance.improvement.workflow:improvement-retro-eval-analysis/execute", "assurance.improvement.agent.retro-eval-analysis.v1"),
    ("improvement.retro-issue-analysis", "assurance.improvement.workflow:improvement-retro-issue-analysis/execute", "assurance.improvement.agent.retro-issue-analysis.v1"),
    ("improvement.retro-workflow-analysis", "assurance.improvement.workflow:improvement-retro-workflow-analysis/execute", "assurance.improvement.agent.retro-workflow-analysis.v1"),
    ("improvement.improvement-review", "assurance.improvement.workflow:improvement-review/execute", "assurance.improvement.agent.improvement-review.v1"),
    ("intake.intake", "assurance.intake.workflow:intake/execute", "assurance.intake.agent.intake.v1"),
    ("intake.explore", "assurance.intake.workflow:explore/execute", "assurance.intake.agent.explore.v1"),
    ("intake.case-design.primary", "assurance.intake.workflow:case-design/execute", "assurance.intake.agent.case-design.v1"),
    ("intake.case-design.repair", "assurance.intake.workflow:case-design/repair-execute", "assurance.intake.agent.case-design.v1"),
    ("intake.case-review", "assurance.intake.workflow:case-review/execute", "assurance.intake.agent.case-review.v1"),
    ("quality.fact-baseline", "assurance.quality.workflow:quality-fact-baseline/execute", "assurance.quality.agent.fact-baseline.v1"),
    ("quality.inspect", "assurance.quality.workflow:quality-inspect/execute", "assurance.quality.agent.inspect.v1"),
    ("quality.issue-triage", "assurance.quality.workflow:quality-issue-triage/execute", "assurance.quality.agent.issue-triage.v1"),
    ("quality.issue-analysis", "assurance.quality.workflow:quality-issue-analysis/execute", "assurance.quality.agent.issue-analysis.v1"),
    ("quality.report", "assurance.quality.workflow:quality-report/execute", "assurance.quality.agent.report.v1"),
)
```

The inventory test asserts `len(EXPECTED_AGENT_OCCURRENCES) == 34`, exactly 33 distinct contract IDs, only the case-design ID has multiplicity two, and each target Feature factory later binds the same `(semantic_node_id, contract_id)` pair.

## Feature waves

1. **Tracer wave:** `assurance.execution.agent.execute.v1` (typed-only), `assurance.quality.agent.report.v1` (raw-only), and `assurance.generation.agent.api.plan.v1` (mixed).
2. **Execution wave:** close `assurance.execution.agent.run.v1` and re-run both Execution contracts through the real Kernel.
3. **Intake wave:** close all four IDs/five occurrences, including dynamic case paths and bounded repair.
4. **Generation wave:** close the remaining 13 IDs, four dynamic codegen roots, and two fix allowed-path sets; re-run all 14 together.
5. **Quality wave:** close the four typed IDs and re-run the raw report tracer with them.
6. **Healing wave:** close both typed IDs without changing the three Healing effects.
7. **Improvement wave:** close all six typed IDs without changing the eight direct Improvement Attempt contracts or three Improvement effects.

### Task 1: Freeze the machine-readable source inventory with a safe raw baseline

**Files:**

- Create: `scripts/build_agent_artifact_inventory.py`
- Create: `tests/architecture/agent_artifact_inventory.py`
- Create: `tests/architecture/fixtures/agent-artifact-inventory.v1.json`
- Create: `tests/architecture/test_agent_artifact_inventory.py`

**Interfaces:** `build_inventory() -> tuple[AgentArtifactInventoryRow, ...]`, `EXPECTED_AGENT_OCCURRENCES`, primitive exact per-contract `AgentExecutionAuthorityRow` mirror of the Artifact-owned full `AgentExecutionAuthority` (including complete command-secret requirement values/digests), canonical JSON fixture writer/checker, exact 33/34/61+8/69 counts, every initial artifact row final-safe raw.

- [ ] **Step 1: Add the exact ID, occurrence, phase, mutation, and execution-authority constants.** Copy the catalog, 34-occurrence tuple, exact mutation-phase matrix, `EXPECTED_TOOLCHAIN_REQUIREMENT_IDS_BY_CONTRACT`, `INPUT_CONDITIONED_NETWORK_CONTRACT_IDS`, and `INPUT_CONDITIONED_COMMAND_SECRET_CONTRACT_IDS` above into `tests/architecture/agent_artifact_inventory.py`; expose `EXPECTED_AGENT_CONTRACT_IDS`, `EXPECTED_AGENT_OCCURRENCES`, `EXPECTED_OCCURRENCE_PHASES`, and one 33-row `EXPECTED_EXECUTION_AUTHORITIES` fixture. Each primitive authority row carries its explicit sorted provider-neutral toolchain tuple, network mode/requirement IDs, and secret mode plus the complete sorted secret-requirement tuples `(requirement_id, selection_mode, environment_alias_keys, purpose, one_shot_env_v1, utf8_no_nul_v1, allowed_target_classes, max_value_bytes, digest)`; exact-set assert the two Execution rows carry all four `EXECUTION_RUNNER_REQUIREMENT_IDS` and the other 31 rows carry `()`. Concrete profiles, executables, providers, adapter capabilities, network backends/targets, secret handles/values, and Feature-local authority/secret types are forbidden. Assert exact alias order/set, target-class set, value limit, and requirement digest; drift in any one is a failing exact-set comparison. Assert only case-design maps two semantic occurrences, exactly 16 occurrences have `initial + repeat`, `intake.case-design.repair` and both codegen-fix occurrences are repair-only, the remaining 15 are initial-only, every one of the 69 slot rows expands to explicit rules with no default, and the authority partitions are exactly `2 toolchain + 31 none`, `10 network input_conditioned + 23 none`, and `2 secret input_conditioned + 31 none`. Freeze the Healing coverage-repair row as repeatable exact-file raw authority resolved only from validated `brief.allowed_test_files`, with existing-baseline precondition, `bounded_repair + agent_raw_whole_file` for both phases, `preserve_baseline`, and the limits above.

- [ ] **Step 2: Write the RED for catalog closure.** The test treats the exact constants as the deletion-safe migration authority. While legacy catalogs still exist, the generator cross-checks all six `AGENT_JOB_CONTRACTS`, 61 `OUTPUT_ROUTE_TEMPLATES`, and six Workflow modules against them; after the Product cutover deletes those sources, the checked fixture remains valid and live Python Feature factories are compared to the same constants instead of reopening YAML.

```python
def test_agent_contract_and_occurrence_inventory_is_exact() -> None:
    inventory = build_inventory()
    assert len({row.contract_id for row in inventory}) == 33
    assert len({occ for row in inventory for occ in row.occurrence_ids}) == 34
    assert len(inventory) == 69
    structured_candidates = {
        row.contract_id for row in inventory if row.candidate_kind == "structured"
    }
    assert len(structured_candidates) == 32
    assert set(EXPECTED_AGENT_CONTRACT_IDS) - structured_candidates == {
        "assurance.quality.agent.report.v1"
    }
    assert sum(row.path_template_or_resolver.endswith((".json", ".yaml", ".yml")) for row in inventory) == 33
    assert all(row.disposition == "raw" for row in inventory)
```

- [ ] **Step 3: Run RED.**

```bash
uv run pytest -q tests/architecture/test_agent_artifact_inventory.py
```

Expected: FAIL because the inventory module and checked fixture do not exist.

- [ ] **Step 4: Implement the deterministic collector.** Encode the reviewed 61 fixed templates, repeatable Intake case resolver, four generated-tree resolvers, two codegen-fix exact-member resolvers, one Healing coverage-repair exact-member resolver, target kind, mode policy, item/byte limits, and exact per-slot occurrence/phase mutation rules as typed constants in fixed owner order; use legacy catalogs only as an optional one-time cross-check. The Healing resolver consumes only validated `CoverageRepairInputV1.brief.allowed_test_files`, canonicalizes a sorted unique finite exact-file tuple, and cannot consume result `files_modified` or filesystem discovery as authority. Encode a separate exact 33-row provider-neutral execution-authority table so toolchain requirement IDs and network/secret modes plus requirement IDs are contract-scoped rather than duplicated across slots; prohibit concrete backend/profile/adapter/deployment values. Attach the exact occurrence tuple and explicit content authority/projector source, sort artifact rows by `(feature_id, contract_id, slot_id)` and authority rows by `contract_id`, and reject duplicate slot IDs, missing/extra authority rows, bound-path collisions within one contract/Attempt, every unmapped write claim, any raw endpoint/handle/value field, or any `none` row with a requirement. Reviewed reuse of a conventional path across different contracts is legal because each executes in an isolated Attempt workspace; record it, but do not treat it as a global collision. Task 2 installs every authority row atomically; later Feature waves only assert exact equality while changing artifacts.

- [ ] **Step 5: Emit only final-safe rows.** Initialize all 69 rows as `disposition="raw"`; use `candidate_kind="structured"` for the 33 JSON/YAML fixed paths and the repeatable case slot, otherwise `candidate_kind="raw"`; give every raw row a concrete reason (`"typed evidence has not landed"`, `"human-authored markdown"`, `"formatter/test-dependent generated tree"`, or `"bounded repair of existing test files"`). No row contains `pending` or an empty reason.

- [ ] **Step 6: Generate and verify the fixture.**

```bash
uv run python scripts/build_agent_artifact_inventory.py --write tests/architecture/fixtures/agent-artifact-inventory.v1.json
uv run python scripts/build_agent_artifact_inventory.py --check tests/architecture/fixtures/agent-artifact-inventory.v1.json
uv run pytest -q tests/architecture/test_agent_artifact_inventory.py
```

Expected: all exit `0`; the checker reports `33 contracts, 34 occurrences, 69 slots, 69 raw, 0 typed, network 10/23, command-secret 2/31`.

- [ ] **Step 7: Commit the baseline.**

```bash
git add scripts/build_agent_artifact_inventory.py tests/architecture/agent_artifact_inventory.py tests/architecture/fixtures/agent-artifact-inventory.v1.json tests/architecture/test_agent_artifact_inventory.py
git commit -m "test: freeze Agent artifact inventory"
```

## Authoritative prerequisite gates

This plan does not implement the adapter protocol, artifact foundation, registries, serializers, materializer, receipts, snapshots, or Kernel recovery. Those have one authority each:

- `docs/superpowers/plans/2026-09-01-artifact-kernel-foundation.md` owns `ArtifactContract`/`ArtifactSlot`, authenticated artifact registries, canonical codecs, materialization receipts, `ResolvedStructuredAgentExecutor`, Attempt integration, and the seven crash cuts. Its **Artifact Foundation + Kernel Exit Gate** must be green before the tracer wave. Its Task 5 consumes, rather than redeclares, the activity seam from OpenCode Task 1.
- `docs/superpowers/plans/2026-09-01-opencode-structured-output-gate.md` owns the sole `StructuredAgentActivityPort` definition, the real `format.schema`/`info.structured` protocol, restart/error reduction, pinned certification, the exact 33-row provider/model/schema matrix, and typed-path denial at every OpenCode write seam. Its Task 1 precedes Artifact/Kernel Task 5; its Tasks 2–4 and generic deny-hole Task 6 must be green before the tracer wave; its Task 5 and final Task 7 consume/recheck the frozen 33 Feature schemas and final typed/raw paths after this plan's seven Feature waves.
- This plan owns the exact inventory, Feature result/document models and schemas, Feature `ArtifactContract`/`ArtifactSlot` values, skills, projectors, finalizers, Feature-side resource claims, per-slot parity/recovery evidence, installed Feature contributions, documentation supersession, and aggregate Checkpoint S/CI closure.

The integration order is fixed:

```text
completed Foundation 1–10 / Semantic Attempt 1–10 / Feature 1–9 / Product T5a
→ OpenCode gate Task 0
→ Checkpoint S0 green
→ this plan Task 10 normative documentation sync (first post-S0 retrofit commit)
→ this plan Task 1 inventory
→ OpenCode gate Task 1
→ Artifact Foundation Tasks 1–4
→ this plan Task 2 raw production and provider-neutral authority baseline
→ Artifact Foundation Task 5 consumes the frozen 33-row authority table
→ Artifact Foundation Tasks 6–8
→ OpenCode gate Tasks 2–4 and generic deny-hole/command-boundary Task 6
→ Artifact Foundation Tasks 9–10 extend the existing Semantic Attempt Kernel
→ this plan Tasks 3–9 Feature waves
→ this plan Task 11 pre-certification production Structured Runtime foundation
→ OpenCode gate Task 5 and Task 7 qualification/promotion using the frozen 33-schema/path catalog
→ binding build, wheel install, and Boot proof
→ rerun this plan Task 10 wording gate (no second documentation commit)
→ this plan Task 12 post-certification Product aggregation and Checkpoint S/CI closure
```

A missing prerequisite gate is a stop condition, not authority to reimplement its code here.

Until OpenCode Tasks 5 and 7 have produced a real positive 33-row record, Feature Tasks 3–9 run through an installed test-only `TestStructuredAgentActivityPort` and test Product binding whose candidate/replay stream is deterministic and schema-checked. That fixture may exercise the actual Kernel, Boot, permissions, materializer, finalizer, and recovery code, but it is excluded from production declarations and cannot satisfy the OpenCode transport or provider/model certification gates.

### Task 2: Install the final-safe raw ArtifactContract and execution-authority baseline

**Dependency:** Artifact Foundation Tasks 1–4 and OpenCode Task 1 are green. Artifact Task 5, OpenCode Task 6, and Semantic Attempt Task 4 have not run; all three consume the Feature-owned raw/authority mappings produced here and therefore cannot precede this commit.

**Files:**

- Create: `packages/capabilities/assurance-execution/assurance_execution/contracts/artifacts.py`
- Create: `packages/capabilities/assurance-execution/assurance_execution/artifacts/__init__.py`
- Create: `packages/capabilities/assurance-execution/assurance_execution/artifacts/path_resolvers.py`
- Create: `packages/capabilities/assurance-execution/assurance_execution/artifacts/raw_member_selectors.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/plugin.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/plugin-declaration.json`
- Create: `packages/capabilities/assurance-generation/assurance_generation/contracts/artifacts.py`
- Create: `packages/capabilities/assurance-generation/assurance_generation/artifacts/__init__.py`
- Create: `packages/capabilities/assurance-generation/assurance_generation/artifacts/path_resolvers.py`
- Create: `packages/capabilities/assurance-generation/assurance_generation/artifacts/raw_member_selectors.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/plugin.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/plugin-declaration.json`
- Create: `packages/capabilities/assurance-healing/assurance_healing/contracts/artifacts.py`
- Create: `packages/capabilities/assurance-healing/assurance_healing/artifacts/__init__.py`
- Create: `packages/capabilities/assurance-healing/assurance_healing/artifacts/path_resolvers.py`
- Create: `packages/capabilities/assurance-healing/assurance_healing/artifacts/raw_member_selectors.py`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/plugin.py`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/plugin-declaration.json`
- Create: `packages/capabilities/assurance-improvement/assurance_improvement/contracts/artifacts.py`
- Create: `packages/capabilities/assurance-improvement/assurance_improvement/artifacts/__init__.py`
- Create: `packages/capabilities/assurance-improvement/assurance_improvement/artifacts/path_resolvers.py`
- Create: `packages/capabilities/assurance-improvement/assurance_improvement/artifacts/raw_member_selectors.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/plugin.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/plugin-declaration.json`
- Create: `packages/capabilities/assurance-intake/assurance_intake/contracts/artifacts.py`
- Create: `packages/capabilities/assurance-intake/assurance_intake/artifacts/__init__.py`
- Create: `packages/capabilities/assurance-intake/assurance_intake/artifacts/path_resolvers.py`
- Create: `packages/capabilities/assurance-intake/assurance_intake/artifacts/raw_member_selectors.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/plugin.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/plugin-declaration.json`
- Create: `packages/capabilities/assurance-quality/assurance_quality/contracts/artifacts.py`
- Create: `packages/capabilities/assurance-quality/assurance_quality/artifacts/__init__.py`
- Create: `packages/capabilities/assurance-quality/assurance_quality/artifacts/path_resolvers.py`
- Create: `packages/capabilities/assurance-quality/assurance_quality/artifacts/raw_member_selectors.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/plugin.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/plugin-declaration.json`
- Modify: `scripts/build_agent_artifact_inventory.py`
- Modify: `tests/architecture/fixtures/agent-artifact-inventory.v1.json`
- Create: `tests/architecture/test_agent_artifact_raw_baseline.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/contracts/workflow.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/resources/workflow/module.yaml`
- Modify: `packages/capabilities/assurance-execution/tests/test_workflow_module.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/contracts/workflow.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/workflow/module.yaml`
- Modify: `packages/capabilities/assurance-generation/tests/test_workflow_module.py`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/contracts/workflow.py`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/resources/workflow/module.yaml`
- Modify: `packages/capabilities/assurance-healing/tests/test_workflow_module.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/contracts/workflow.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/resources/workflow/module.yaml`
- Modify: `packages/capabilities/assurance-improvement/tests/test_workflow_module.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/contracts/workflow.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/resources/workflow/module.yaml`
- Modify: `packages/capabilities/assurance-intake/tests/test_workflow_module.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/contracts/workflow.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/resources/workflow/module.yaml`
- Modify: `packages/capabilities/assurance-quality/tests/test_workflow_module.py`
- Modify: `packages/products/assurance-product/assurance_product/resources/schemas/product-input-v1.json`
- Create: `packages/products/assurance-product/assurance_product/toolchain_requirements.py`
- Modify: `packages/products/assurance-product/assurance_product/models.py`
- Modify: `packages/products/assurance-product/assurance_product/cli.py`
- Modify: `packages/products/assurance-product/assurance_product/product.py`
- Modify: `packages/products/assurance-product/assurance_product/resources/workflow/main.yaml`
- Modify by declaration regeneration: `packages/products/assurance-product/assurance_product/product-declaration-opencode.json`
- Modify by declaration regeneration: `packages/products/assurance-product/assurance_product/product-declaration-cursor.json`
- Create: `tests/architecture/test_agent_execution_authority_baseline.py`
- Create: `tests/product/test_structured_toolchain_requirements.py`
- Modify: `tests/product/test_product_input.py`
- Modify: `tests/product/test_workflow_modularization_golden.py`

**Interfaces:** each Feature exports `RAW_AGENT_ARTIFACT_CONTRACTS: Mapping[str, ArtifactContract]`, `artifact_contracts()`, `artifact_path_resolvers()`, `artifact_raw_member_selectors()`, and a complete provider-neutral 33-row mapping of the sole Artifact-owned `AgentExecutionAuthority` values consumed later by Artifact Task 5 and Semantic Attempt Task 4. The union is exact-set equal to the 33 contract IDs and contains 69 explicit `agent_raw_bytes` slots with one same-owner selector each: Execution 2, Generation 44, Healing 3, Improvement 6, Intake 9, and Quality 5. Product installs the exact four provider-neutral `StructuredToolchainRequirement` semantic rows from Task 1, but no concrete profile. Product exposes only public `ProductInputV1`; its root adapter checkpoints internal `ResolvedProductInputV1` with private selection rows/digests. The adapter consumes already authenticated Product-owned target/binding values through the core seams; focused network/secret tests install explicit test-only values. This task does not create a concrete toolchain profile, network target catalog/backend, command-secret binding/source, or production satisfied capability, so production executor resolution remains fail-closed until OpenCode Task 6 and the later binding build.

- [ ] **Step 1: Write the production-catalog RED.** Import all six Feature mappings and assert exact 33-ID equality, owner slot counts `2/44/3/6/9/5`, total 69, stable slot order, unique `(contract_id, slot_id)`, and a digest-valid `ArtifactContract` for every ID. Assert every slot has `authority="agent_raw_bytes"`, no document/schema/codec/serializer/projector/parity/migration-record field, an explicit `exact_file | tree_root` target kind, exact mutation-phase rules/mode/cardinality/size bounds from Task 1, one installed path-resolver ID, and one installed same-owner raw-member-selector ID. Require every contract's slots to derive one identical occurrence/phase domain and compare it to `EXPECTED_OCCURRENCE_PHASES`.

- [ ] **Step 2: Write the four semantic requirement, 33-row execution-authority, and root-envelope REDs.** In `test_structured_toolchain_requirements.py`, exact-set compare the Product contribution to Task 1's four canonical `StructuredToolchainRequirement` rows and mutate purpose, logical recipe, dependency class, environment policy, scratch policy, owner/provenance, and digest independently. Reject an executable/path/argv/environment value/image/backend field, an extra generic-shell/formatter/LSP row, a Feature/project contribution, or a concrete profile in this module. In `test_agent_execution_authority_baseline.py`, compare the six live Feature declaration mappings by exact contract ID to Task 1's 33-row table. Every row is the sole Artifact-owned `AgentExecutionAuthority` and explicitly carries sorted provider-neutral `structured_toolchain_requirement_ids`, exact `structured_network_mode` plus requirement IDs, and exact `structured_command_secret_mode` plus the complete `StructuredCommandSecretRequirement` tuple, including explicit empty tuples for `none`; no row contains a concrete sandbox/profile/backend, executable path, host, URL, target descriptor, DNS value, secret handle/value, adapter capability, provider, or model. Assert exact alias tuples, purpose, `one_shot_env_v1`, `utf8_no_nul_v1`, allowed target classes, `max_value_bytes`, and digest; mutate each independently and require Boot/exact-set failure. Assert the exact `2/31` toolchain, `10/23` network, and `2/31` command-secret partitions: only `execution.execute` and `execution.run` carry the four semantic runner IDs, and every one of the other 31 rows is empty. Artifact Task 5 and Semantic Attempt Task 4 must later attach these exact full values to their 33 `AgentExecutionContract` values and prove equality; Tasks 3–9 may only retain/assert them while migrating artifact shape. OpenCode compatibility/certification matrices derive and carry toolchain/network/command-secret requirement IDs, not a second copy of the full requirement values.

  In `test_product_input.py`, require public `ProductInputV1` to contain sorted/unique bounded `sut_read_paths`, `sut_network_mode`, opaque `sut_network_target_ids`, `sut_auth_mode`, and opaque `sut_command_secret_requirement_ids`, with extra-forbid behavior for every private field. `_load_product_input(...)` is the sole public parse boundary. The Product root adapter resolves public IDs against authenticated Product-owned target/requirement/binding values supplied by the composition/runtime seam and constructs internal-only `ResolvedProductInputV1 = (public_input, sut_network_selections, sut_network_selection_digest, sut_command_secret_selections, sut_command_secret_selection_digest)` before graph dispatch. Use explicit test-only catalog/binding fixtures excluded from Product declarations in this task; prove missing production values fail before graph dispatch and cannot be replaced by project config or empty defaults. `_plan_start(...)` checkpoints that envelope as anchored root input; restart authenticates and reuses it byte-for-byte without re-reading a changed catalog or rerunning selection. Six coexistence Feature modules receive only their declared private rows; reject missing/extra/cross-requirement/duplicate/late-expanded selectors and every raw host/URL/port/DNS/secret/handle business field.

- [ ] **Step 3: Write path/member-closure REDs.** Resolve all 61 fixed path templates from validated `InputT`; resolve the Intake repeatable case slot only from sorted authenticated `case_delta_paths`; resolve four Generation primary raw tree roots from the finite family and two codegen-fix repeatable exact-file sets from authenticated `allowed_paths`; resolve Healing's coverage-repair repeatable exact-file set only from sorted unique validated `brief.allowed_test_files`. Require every Healing member to exist as a regular file in the immutable input snapshot, expose those original bytes through the read-only Agent view, bind its original mode, and reject a missing baseline before activity dispatch. For each nonrepeatable raw exact slot, its selector declares that one bound path; repeatable exact selectors are set-equal to the pre-key bound set; the four Generation tree selectors consume only the validated Agent result member list and emit canonical per-slot paths under the bound root; Intake's repeatable selector is likewise set-equal. Healing's raw selector covers the complete pre-key `brief.allowed_test_files` set, never only the result-reported subset. Kernel computes `actual_changed_set` across that complete set from immutable baselines versus raw post-image/sealed partition and requires `actual_changed_set == CoverageRepairAgentResultV1.files_modified ⊆ prekey_allowed_test_files`. Reject selector-supplied bytes/digest/size/mode, traversal, absolute/Windows paths, wrong change IDs, duplicates, false-positive reports, unreported changes, a result-expanded/created/deleted/unlisted Healing file, mode drift, unlisted same-root fix files, unbounded roots/items, selector/resolver owner/provenance drift, later path expansion, and SUT/project-loaded contributions.

- [ ] **Step 4: Run RED.**

```bash
uv run pytest -q tests/architecture/test_agent_artifact_raw_baseline.py tests/architecture/test_agent_execution_authority_baseline.py tests/product/test_structured_toolchain_requirements.py tests/product/test_product_input.py tests/product/test_workflow_modularization_golden.py packages/capabilities/assurance-execution/tests/test_workflow_module.py packages/capabilities/assurance-generation/tests/test_workflow_module.py packages/capabilities/assurance-healing/tests/test_workflow_module.py packages/capabilities/assurance-improvement/tests/test_workflow_module.py packages/capabilities/assurance-intake/tests/test_workflow_module.py packages/capabilities/assurance-quality/tests/test_workflow_module.py
```

Expected: FAIL because the raw catalogs, complete provider-neutral 33-row authority table, closed Product root envelope, and private graph projections do not exist.

- [ ] **Step 5: Implement the six Feature raw catalogs.** Copy the exact checked route/path/target-kind/mode/limit/mutation-rule inventory from Task 1 into Feature-owned installed Python values, not a runtime YAML/parser. Fixed resolvers return one exact path from validated `change_id`; dynamic resolvers return only the pre-authorized finite set. Healing coverage repair binds its repeatable exact-file set from validated `brief.allowed_test_files`, requires immutable existing baselines, and gives those bytes to the Agent read view before mutation; it is never a root or a create permission. Implement the explicit selectors described in Step 3; selectors declare members only and Kernel later derives byte truth. Use raw authority for all 69 rows, `agent_raw_whole_file` for every raw bounded-repair rule, `preserve_baseline` for Healing's set, and no fallback mutation. Do not create a typed document model, typed projector, serializer contribution, or semantic-migration record in this task.

- [ ] **Step 6: Install the semantic requirement and authority spine and authenticate all baseline contributions.** In Product `toolchain_requirements.py`, encode the exact seven-field payloads frozen in Task 1 and construct the sole Artifact Task 4 production `StructuredToolchainRequirement` values. Tests validate each primitive Row dump into the production type and require byte-identical canonical dumps/digests; production code never imports the test-only Row class. Publish exactly those four values through Artifact Task 4's Product-owned contribution seam; `product.py` and both declarations expose their canonical values/provenance/digests. This module contains no executable, path, argv, environment value, image/runtime ref, sandbox/backend, resolved profile, or callable. Beside—not inside—the legacy four-field `AGENT_JOB_CONTRACTS`, add one Feature-owned immutable `AGENT_EXECUTION_AUTHORITY_DECLARATIONS: Mapping[str, AgentExecutionAuthority]` in each `contracts/workflow.py`, using the sole Artifact Task 4 type and the exact Task 1 full values for all 33 IDs. Encode the exact secret payload once per owning Feature and apply the same primitive-dump/production-validate/equality test. It carries toolchain IDs, network mode/IDs, and secret mode/full requirement tuples, and does not select or instantiate a concrete toolchain/network/secret backend. Artifact Task 5 consumes these mappings when it extends the canonical `AgentExecutionContract`; Semantic Attempt Task 4 later attaches the same values to the final Attempt contracts. Add the five public Product input fields and strict extra-forbid model/schema, construct/checkpoint internal `ResolvedProductInputV1` in `models.py`/`cli.py`, project only declared private selection rows through Product `main.yaml` and six Feature modules, and regenerate all Feature/Product declarations. Register the Product requirement rows plus six Feature contract/resolver/raw-member-selector/authority sets through the Artifact Task 4 contribution seams. ProductLock/build digests must change on any requirement/contract/resolver/selector/authority/alias/limit/source drift. Boot rejects missing, extra, duplicate, wrong-owner, project-loaded, concrete-backend-bearing, or digest-drifted values. Keep test-only network/secret catalog/binding fixtures in test modules only; do not write a provisional production profile/network catalog/secret binding or advertise `opencode_structured_output` in this commit.

- [ ] **Step 7: Switch the inventory checker to the live raw catalogs and authority declarations.** Keep the legacy 61-route/34-occurrence comparison while coexistence sources exist and record Healing's newly explicit dynamic repair set as a reviewed authority addition, but make the six Feature `RAW_AGENT_ARTIFACT_CONTRACTS` and provider-neutral authority mappings the production authority. Regenerate the fixture and require exactly `33 contracts, 34 occurrences, 69 slots, 69 raw, 0 typed`; exact-set compare all 33 toolchain/network/secret declarations and require the anchored Product root-envelope replay tests.

- [ ] **Step 8: Run GREEN and composition checks.**

```bash
uv run pytest -q tests/architecture/test_agent_artifact_inventory.py \
  tests/architecture/test_agent_artifact_raw_baseline.py \
  tests/architecture/test_agent_execution_authority_baseline.py \
  tests/product/test_structured_toolchain_requirements.py \
  tests/product/test_product_input.py \
  tests/product/test_workflow_modularization_golden.py \
  packages/capabilities/assurance-execution/tests/test_workflow_module.py \
  packages/capabilities/assurance-generation/tests/test_workflow_module.py \
  packages/capabilities/assurance-healing/tests/test_workflow_module.py \
  packages/capabilities/assurance-improvement/tests/test_workflow_module.py \
  packages/capabilities/assurance-intake/tests/test_workflow_module.py \
  packages/capabilities/assurance-quality/tests/test_workflow_module.py \
  packages/framework/graph-engine/tests/artifacts/test_registry.py \
  packages/framework/graph-engine/tests/composition/test_attempt_contract_registry.py
uv run pyright packages/capabilities/assurance-execution packages/capabilities/assurance-generation \
  packages/capabilities/assurance-healing packages/capabilities/assurance-improvement \
  packages/capabilities/assurance-intake packages/capabilities/assurance-quality
uv run lint-imports
```

Expected: all commands exit `0`; the production catalog is complete and still grants no typed authority.

- [ ] **Step 9: Commit the raw and execution-authority baseline.**

```bash
git add \
  packages/capabilities/assurance-execution/assurance_execution/contracts/artifacts.py \
  packages/capabilities/assurance-execution/assurance_execution/artifacts/__init__.py \
  packages/capabilities/assurance-execution/assurance_execution/artifacts/path_resolvers.py \
  packages/capabilities/assurance-execution/assurance_execution/artifacts/raw_member_selectors.py \
  packages/capabilities/assurance-execution/assurance_execution/plugin.py \
  packages/capabilities/assurance-execution/assurance_execution/plugin-declaration.json \
  packages/capabilities/assurance-generation/assurance_generation/contracts/artifacts.py \
  packages/capabilities/assurance-generation/assurance_generation/artifacts/__init__.py \
  packages/capabilities/assurance-generation/assurance_generation/artifacts/path_resolvers.py \
  packages/capabilities/assurance-generation/assurance_generation/artifacts/raw_member_selectors.py \
  packages/capabilities/assurance-generation/assurance_generation/plugin.py \
  packages/capabilities/assurance-generation/assurance_generation/plugin-declaration.json \
  packages/capabilities/assurance-healing/assurance_healing/contracts/artifacts.py \
  packages/capabilities/assurance-healing/assurance_healing/artifacts/__init__.py \
  packages/capabilities/assurance-healing/assurance_healing/artifacts/path_resolvers.py \
  packages/capabilities/assurance-healing/assurance_healing/artifacts/raw_member_selectors.py \
  packages/capabilities/assurance-healing/assurance_healing/plugin.py \
  packages/capabilities/assurance-healing/assurance_healing/plugin-declaration.json \
  packages/capabilities/assurance-improvement/assurance_improvement/contracts/artifacts.py \
  packages/capabilities/assurance-improvement/assurance_improvement/artifacts/__init__.py \
  packages/capabilities/assurance-improvement/assurance_improvement/artifacts/path_resolvers.py \
  packages/capabilities/assurance-improvement/assurance_improvement/artifacts/raw_member_selectors.py \
  packages/capabilities/assurance-improvement/assurance_improvement/plugin.py \
  packages/capabilities/assurance-improvement/assurance_improvement/plugin-declaration.json \
  packages/capabilities/assurance-intake/assurance_intake/contracts/artifacts.py \
  packages/capabilities/assurance-intake/assurance_intake/artifacts/__init__.py \
  packages/capabilities/assurance-intake/assurance_intake/artifacts/path_resolvers.py \
  packages/capabilities/assurance-intake/assurance_intake/artifacts/raw_member_selectors.py \
  packages/capabilities/assurance-intake/assurance_intake/plugin.py \
  packages/capabilities/assurance-intake/assurance_intake/plugin-declaration.json \
  packages/capabilities/assurance-quality/assurance_quality/contracts/artifacts.py \
  packages/capabilities/assurance-quality/assurance_quality/artifacts/__init__.py \
  packages/capabilities/assurance-quality/assurance_quality/artifacts/path_resolvers.py \
  packages/capabilities/assurance-quality/assurance_quality/artifacts/raw_member_selectors.py \
  packages/capabilities/assurance-quality/assurance_quality/plugin.py \
  packages/capabilities/assurance-quality/assurance_quality/plugin-declaration.json \
  scripts/build_agent_artifact_inventory.py \
  tests/architecture/fixtures/agent-artifact-inventory.v1.json \
  tests/architecture/test_agent_artifact_raw_baseline.py
git add \
  packages/capabilities/assurance-execution/assurance_execution/contracts/workflow.py \
  packages/capabilities/assurance-execution/assurance_execution/resources/workflow/module.yaml \
  packages/capabilities/assurance-execution/tests/test_workflow_module.py \
  packages/capabilities/assurance-generation/assurance_generation/contracts/workflow.py \
  packages/capabilities/assurance-generation/assurance_generation/resources/workflow/module.yaml \
  packages/capabilities/assurance-generation/tests/test_workflow_module.py \
  packages/capabilities/assurance-healing/assurance_healing/contracts/workflow.py \
  packages/capabilities/assurance-healing/assurance_healing/resources/workflow/module.yaml \
  packages/capabilities/assurance-healing/tests/test_workflow_module.py \
  packages/capabilities/assurance-improvement/assurance_improvement/contracts/workflow.py \
  packages/capabilities/assurance-improvement/assurance_improvement/resources/workflow/module.yaml \
  packages/capabilities/assurance-improvement/tests/test_workflow_module.py \
  packages/capabilities/assurance-intake/assurance_intake/contracts/workflow.py \
  packages/capabilities/assurance-intake/assurance_intake/resources/workflow/module.yaml \
  packages/capabilities/assurance-intake/tests/test_workflow_module.py \
  packages/capabilities/assurance-quality/assurance_quality/contracts/workflow.py \
  packages/capabilities/assurance-quality/assurance_quality/resources/workflow/module.yaml \
  packages/capabilities/assurance-quality/tests/test_workflow_module.py \
  packages/products/assurance-product/assurance_product/resources/schemas/product-input-v1.json \
  packages/products/assurance-product/assurance_product/toolchain_requirements.py \
  packages/products/assurance-product/assurance_product/models.py \
  packages/products/assurance-product/assurance_product/cli.py \
  packages/products/assurance-product/assurance_product/product.py \
  packages/products/assurance-product/assurance_product/resources/workflow/main.yaml \
  packages/products/assurance-product/assurance_product/product-declaration-opencode.json \
  packages/products/assurance-product/assurance_product/product-declaration-cursor.json \
  tests/architecture/test_agent_execution_authority_baseline.py \
  tests/product/test_structured_toolchain_requirements.py \
  tests/product/test_product_input.py \
  tests/product/test_workflow_modularization_golden.py
git commit -m "feat: install Agent artifact and authority baseline"
```

Semantic Attempt Task 4 must now attach `RAW_AGENT_ARTIFACT_CONTRACTS[contract_id]`, the exact Feature-owned provider-neutral toolchain/network/command-secret authority declaration, and the Feature-declared `non_artifact_resources` subset to each of the 33 new `AgentExecutionContract` values and assert exact set equality/claim partition. It may not synthesize a fallback contract, infer authority from a skill/resource/suffix, select a concrete backend, or leave a write/exclusive claim unclassified. OpenCode Task 6 and Artifact Task 9 consume this already frozen table; neither may be scheduled before this atomic baseline commit.

**Tasks 3–9 authority invariant:** these seven waves migrate result/artifact shape only. Every touched `contracts/attempts.py`, plugin declaration, workflow module, and generated Product declaration must exact-set equal its already installed Task 2 `AgentExecutionAuthority` before and after the commit, including toolchain IDs, network mode/IDs, secret mode/full requirement values, aliases, limits, and digests. Wording such as “bind explicit none” means retain and assert the frozen row; it never authorizes first definition, mutation, or a concrete backend in a Feature wave.

### Task 3: Prove the typed-only, raw-only, and mixed tracer through the real Kernel

**Files:**

- Modify: `packages/capabilities/assurance-execution/assurance_execution/contracts/artifacts.py`
- Create: `packages/capabilities/assurance-execution/assurance_execution/contracts/agent_results.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/artifacts/__init__.py`
- Create: `packages/capabilities/assurance-execution/assurance_execution/artifacts/projectors.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/artifacts/raw_member_selectors.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/contracts/agent.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/contracts/attempts.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/plugin.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/plugin-declaration.json`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/operations/agent_skills.py`
- Create: `packages/capabilities/assurance-execution/assurance_execution/resources/schemas/agent-results/execute-result.v1.schema.json`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/resources/skills/aa-execute/SKILL.md`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/resources/workflow/module.yaml`
- Modify: `packages/capabilities/assurance-execution/tests/test_workflow_module.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/contracts/artifacts.py`
- Create: `packages/capabilities/assurance-quality/assurance_quality/contracts/agent_results.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/contracts/agent.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/contracts/attempts.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/plugin.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/plugin-declaration.json`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/operations/agent_skills.py`
- Create: `packages/capabilities/assurance-quality/assurance_quality/resources/schemas/agent-results/report-result.v1.schema.json`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/resources/skills/aa-report-generator/SKILL.md`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/contracts/artifacts.py`
- Create: `packages/capabilities/assurance-generation/assurance_generation/contracts/agent_results.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/artifacts/__init__.py`
- Create: `packages/capabilities/assurance-generation/assurance_generation/artifacts/projectors.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/artifacts/raw_member_selectors.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/contracts/agent.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/contracts/attempts.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/plugin.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/plugin-declaration.json`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/operations/planning.py`
- Create: `packages/capabilities/assurance-generation/assurance_generation/resources/schemas/agent-results/api-plan-result.v1.schema.json`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-api-plan/SKILL.md`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/workflow/module.yaml`
- Modify: `packages/capabilities/assurance-generation/tests/test_workflow_module.py`
- Modify by declaration regeneration: `packages/products/assurance-product/assurance_product/product-declaration-opencode.json`
- Modify by declaration regeneration: `packages/products/assurance-product/assurance_product/product-declaration-cursor.json`
- Modify: `tests/product/test_workflow_modularization_golden.py`
- Create: `tests/product/test_structured_artifact_tracer.py`
- Modify: `tests/architecture/fixtures/agent-artifact-inventory.v1.json`

**Interfaces:** `ExecuteAgentResultV1`, `QualityReportAgentResultV1`, `ApiPlanAgentResultV1`; the three exact `ArtifactContract` values; consumes the already frozen Product root envelope and 33-row provider-neutral execution-authority baseline without redefining either; `requires_structured_output=True`; result-schema digests; catalog-linked tracer IDs.

- [ ] **Step 1: Write the three-mode RED.** Build a test Product composition with the installed test-only `TestStructuredAgentActivityPort`, then run `execution.execute`, `quality.report`, and `generation.api.plan` through the actual Kernel. Assert respectively: only canonical typed bytes, only authenticated raw bytes plus a structured receipt value, and both authorities with disjoint paths. Feed the already installed Task 2 Product root envelope and prove `quality.report` and `generation.api.plan` retain network/command-secret `none` plus an empty toolchain invocation selection/scope, while an offline Execution tracer carries one exact API selected-tests recipe row and retains an empty input-conditioned target/secret set without consulting ambient endpoint/credential variables. Exact-set compare all three live toolchain/network/secret declarations to the Task 2 table before and after their artifact migrations; bind the Execution selection into `task_input_digest`, `BoundStructuredToolchainInvocationScope`, `BoundAttemptDispatch`, and `AttemptKey`; this task may not first introduce, alter, or concretize execution authority. Assert the test port/binding cannot appear in either production Product declaration or an OpenCode certification report.

- [ ] **Step 2: Add permission and finalizer REDs.** Assert OpenCode cannot write/read-back `execution/execute-result.json` or `plans/api-codegen-mapping.json`; it can write the declared Markdown paths; finalizers receive typed values/receipt and never reopen typed files.

- [ ] **Step 3: Run RED.**

```bash
uv run pytest -q tests/product/test_structured_artifact_tracer.py packages/capabilities/assurance-execution/tests/test_agent_skills.py packages/capabilities/assurance-execution/tests/test_workflow_module.py packages/capabilities/assurance-quality/tests/test_agent_skills.py packages/capabilities/assurance-generation/tests/test_planning.py packages/capabilities/assurance-generation/tests/test_workflow_module.py
```

Expected: contracts lack structured result models/artifact slots and the old skills still instruct raw JSON writes.

- [ ] **Step 4: Implement the three structured results and artifact migrations without changing execution authority.** Return complete typed document values for the two typed candidates, keep every listed Markdown slot raw, register schemas/projectors/raw-member selectors and pure prepare/finalize entries as installed Feature contributions, regenerate the three declarations, set `requires_structured_output=True` on all three contracts, and update each Feature's production `contracts/attempts.py` so its `AgentExecutionContract` references the new result model/schema ID/limits and exact live `ArtifactContract`. Copy, do not recompute, its exact provider-neutral toolchain/network/command-secret fields from the Task 2 declaration and assert equality. Hydrate API-plan's reviewed-case/family-constraint/source-plan inputs into its validated `InputT`; its Agent-readable SUT bytes remain exactly the intersection of the already anchored root `sut_read_paths` and Feature-declared read policy and become immutable-snapshot read claims. API plan retains an empty toolchain tuple/selection/scope and network/secret `none`; Execution execute retains all four semantic runner requirements, adds the core-owned tagged invocation selection to its `InputT`, validates exact selected-test/benchmark rows against authenticated runner inputs, and retains its input-conditioned backend/frontend requirements, `sut-admin-credential-v1`, and authenticated `requires_admin_auth`, while the tracer selects one API recipe in validated offline/unauthenticated mode with an empty target/secret selection and zero injection. Rewrite `aa-execute` to request only that already bound logical recipe ID and typed parameters from the command broker; remove Feature-visible `uv`, concrete executable/argv-prefix, dependency/image, cache path, and environment implementation instructions. Updating only legacy `contracts/agent.py`, changing the authority table, or introducing a concrete backend is insufficient.

- [ ] **Step 5: Split byte determination from finalization.** Move byte-affecting mapping/result construction into authenticated projectors; make post-materialization finalizers consume typed values and the aggregate receipt. Keep raw report authentication and final output semantics unchanged.

- [ ] **Step 6: Flip only the proven tracer rows.** Atomically change the execute-result and API mapping inventory rows from raw to typed, fill every evidence ID, retain the report and all Markdown rows as reviewed raw, regenerate the fixture, and assert there is no authority overlap.

- [ ] **Step 7: Verify real recovery, parity, and baseline authority equality.** Reuse Task 2's anchored-envelope restart fixture and prove the artifact migration changes neither its bytes/digests nor any of the three contracts' provider-neutral authority rows.

```bash
uv run pytest -q tests/product/test_structured_artifact_tracer.py packages/framework/graph-engine/tests/attempts/test_kernel_artifact_recovery.py packages/capabilities/assurance-execution/tests packages/capabilities/assurance-quality/tests packages/capabilities/assurance-generation/tests/test_planning.py packages/capabilities/assurance-generation/tests/test_planning_characterization.py tests/architecture/test_agent_artifact_inventory.py
```

Expected: all exit `0`; exact-byte rows match legacy accepted bytes or an explicitly recorded semantic-migration golden; recovery after result persistence dispatches zero new activities.

- [ ] **Step 8: Commit.**

```bash
git add packages/capabilities/assurance-execution/assurance_execution/contracts/artifacts.py packages/capabilities/assurance-execution/assurance_execution/contracts/agent_results.py packages/capabilities/assurance-execution/assurance_execution/artifacts/__init__.py packages/capabilities/assurance-execution/assurance_execution/artifacts/projectors.py packages/capabilities/assurance-execution/assurance_execution/artifacts/raw_member_selectors.py packages/capabilities/assurance-execution/assurance_execution/contracts/agent.py packages/capabilities/assurance-execution/assurance_execution/contracts/attempts.py packages/capabilities/assurance-execution/assurance_execution/plugin.py packages/capabilities/assurance-execution/assurance_execution/plugin-declaration.json packages/capabilities/assurance-execution/assurance_execution/operations/agent_skills.py packages/capabilities/assurance-execution/assurance_execution/resources/schemas/agent-results/execute-result.v1.schema.json packages/capabilities/assurance-execution/assurance_execution/resources/skills/aa-execute/SKILL.md packages/capabilities/assurance-execution/assurance_execution/resources/workflow/module.yaml packages/capabilities/assurance-execution/tests/test_workflow_module.py packages/capabilities/assurance-quality/assurance_quality/contracts/artifacts.py packages/capabilities/assurance-quality/assurance_quality/contracts/agent_results.py packages/capabilities/assurance-quality/assurance_quality/contracts/agent.py packages/capabilities/assurance-quality/assurance_quality/contracts/attempts.py packages/capabilities/assurance-quality/assurance_quality/plugin.py packages/capabilities/assurance-quality/assurance_quality/plugin-declaration.json packages/capabilities/assurance-quality/assurance_quality/operations/agent_skills.py packages/capabilities/assurance-quality/assurance_quality/resources/schemas/agent-results/report-result.v1.schema.json packages/capabilities/assurance-quality/assurance_quality/resources/skills/aa-report-generator/SKILL.md packages/capabilities/assurance-generation/assurance_generation/contracts/artifacts.py packages/capabilities/assurance-generation/assurance_generation/contracts/agent_results.py packages/capabilities/assurance-generation/assurance_generation/artifacts/__init__.py packages/capabilities/assurance-generation/assurance_generation/artifacts/projectors.py packages/capabilities/assurance-generation/assurance_generation/artifacts/raw_member_selectors.py packages/capabilities/assurance-generation/assurance_generation/contracts/agent.py packages/capabilities/assurance-generation/assurance_generation/contracts/attempts.py packages/capabilities/assurance-generation/assurance_generation/plugin.py packages/capabilities/assurance-generation/assurance_generation/plugin-declaration.json packages/capabilities/assurance-generation/assurance_generation/operations/planning.py packages/capabilities/assurance-generation/assurance_generation/resources/schemas/agent-results/api-plan-result.v1.schema.json packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-api-plan/SKILL.md packages/capabilities/assurance-generation/assurance_generation/resources/workflow/module.yaml packages/capabilities/assurance-generation/tests/test_workflow_module.py packages/products/assurance-product/assurance_product/product-declaration-opencode.json packages/products/assurance-product/assurance_product/product-declaration-cursor.json tests/product/test_workflow_modularization_golden.py tests/product/test_structured_artifact_tracer.py tests/architecture/fixtures/agent-artifact-inventory.v1.json
git commit -m "feat: prove structured artifact tracer modes"
```

### Task 4: Close both Execution Agent contracts

**Files:**

- Modify: `packages/capabilities/assurance-execution/assurance_execution/contracts/artifacts.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/contracts/agent_results.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/artifacts/projectors.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/artifacts/raw_member_selectors.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/contracts/agent.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/contracts/attempts.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/plugin.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/plugin-declaration.json`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/resources/workflow/module.yaml`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/operations/agent_skills.py`
- Create: `packages/capabilities/assurance-execution/assurance_execution/resources/schemas/agent-results/run-result.v1.schema.json`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/resources/skills/aa-run/SKILL.md`
- Create: `packages/capabilities/assurance-execution/tests/test_artifact_pipeline.py`
- Modify: `tests/architecture/fixtures/agent-artifact-inventory.v1.json`

**Interfaces:** `RunAgentResultV1`; typed `execution/run-result.json`; two-contract Execution catalog closure; both contracts carry a core-owned `StructuredToolchainInvocationSelection` in validated `InputT`, bound to one `BoundStructuredToolchainInvocationScope` before the key; both contracts `structured_network_mode="input_conditioned"` with backend/frontend test requirements selected from frozen case/family data; both contracts `structured_command_secret_mode="input_conditioned"` with `sut-admin-credential-v1` selected only by authenticated `requires_admin_auth`.

- [ ] **Step 1: Add RED catalog cases for `assurance.execution.agent.execute.v1` and `assurance.execution.agent.run.v1`.** Parameterize result-schema, skill, permission, materialization, finalizer, recovery and parity evidence for each exact ID. Assert both rows explicitly declare installed backend/frontend network and command-secret requirements; their `InputT` carries only the exact Product-created requirement→target selection rows, the core-owned tagged toolchain invocation selection derived from authenticated case/family/runner data, plus an authenticated case/fixture-derived `requires_admin_auth`. Cover API/E2E/Fuzz selected-test rows with exact sorted node IDs and Performance with the exact benchmark path plus bounded integer users/spawn-rate-millis/duration. Require selection digest → bound scope digest → dispatch/key/workspace/broker/store/receipt continuity and exactly one command per selected recipe. Cover all four network/auth combinations: `offline_source + none` selects empty access; `bound_sut + none` is valid only when the predicate is false and performs zero injection; `bound_sut + bound_command_secrets` requires the exact applicable target class and `sut-admin-credential-v1` when the predicate is true; `offline_source + bound_command_secrets` is rejected. Also reject unknown/duplicate/cross-family recipe, broadened or narrowed test nodes, mismatched benchmark file, noninteger/overflow parameters, a second recipe invocation, model-supplied argv/executable/environment, missing auth for a true predicate, extra auth for a false predicate, ambient `BASE_URL`, host/URL input, direct password/token, unknown/cross-requirement target, alias/handle expansion, credential without matching target class, and any pre-key catalog/handle drift.

- [ ] **Step 2: Run RED.**

```bash
uv run pytest -q packages/capabilities/assurance-execution/tests/test_artifact_pipeline.py
```

Expected: the run contract still returns a file claim and its typed candidate remains raw in the fixture.

- [ ] **Step 3: Implement `RunAgentResultV1`, its slot, and both Execution artifact contracts.** Carry the complete run result, remove raw-write/read-back instructions and permissions for the typed target, register its schema/projector and both pure handler entries, make finalization receipt-driven, and update the production `contracts/attempts.py` binding to the live result/schema/limit/artifact contract. Replace both Execution ambient prepare loaders with validated `InputT` fields for selected targets, reviewed cases/constraints, runner inputs, the tagged `StructuredToolchainInvocationSelection`, Product-created backend/frontend requirement→target rows, `requires_admin_auth`, and command-secret requirement IDs. The Feature validator derives and exact-set checks selected test node IDs or the benchmark path/bounds from the same authenticated case/family/runner mapping; it computes the canonical selection digest before `bind_attempt_dispatch(...)`. Resolve exact source/test/tool-config read claims from that frozen input; retain and exact-set assert all four Task 2 semantic runner IDs plus its network/full secret requirements while Product/runtime resolution supplies the concrete qualified profile, pre-resolved target, and value-free secret binding; keep raw endpoint/credential values outside Feature state. Rewrite `aa-run` consistently with `aa-execute`: each skill requests only an applicable, already bound logical API/E2E/Fuzz/Performance recipe and sends exactly its bound typed parameters. The skill must not select a second recipe or mention/construct `uv`, Python/pytest/Playwright/Locust executables, argv prefixes, environment/cache paths, plugins, dependency roots, or images; those are Product profile policy. Product-issued synthetic target aliases and, only when `requires_admin_auth` is true, one-shot `QA_ADMIN_PASSWORD`/`AA_ADMIN_PASSWORD` injection remain the only runtime inputs. An unauthenticated bound case launches with no injected aliases. `reads=("qa",)`, a live project/venv mount, host network, inherited environment, or model-selected endpoint is a failing test.

- [ ] **Step 4: Promote the named inventory row and verify both contracts.**

```bash
uv run pytest -q packages/capabilities/assurance-execution/tests tests/product/test_structured_artifact_tracer.py tests/architecture/test_agent_artifact_inventory.py
```

Expected: both Execution contracts are typed-artifact-only; their two typed bytes and terminal outputs satisfy parity and crash recovery. Bound-SUT fixtures select each of the four semantic recipes and Product resolves each bound recipe/parameter row to the exact deterministic qualified argv against only selected backend/frontend targets: one public/no-secret case proves zero injection and one authenticated case proves the complete one-shot alias tuple. Same input/scope yields the same derived argv and one durable command; unknown/cross-family/duplicate/broadened/mismatched requests launch nothing. Registry/package and an unbound/cross-requirement target are denied, stdout/stderr/raw/structured/log reflection is rejected, exact-generation replay adopts the command without re-execution, and rotation blocks adoption rather than reinjecting.

- [ ] **Step 5: Commit.**

```bash
git add packages/capabilities/assurance-execution/assurance_execution/contracts/artifacts.py packages/capabilities/assurance-execution/assurance_execution/contracts/agent_results.py packages/capabilities/assurance-execution/assurance_execution/artifacts/projectors.py packages/capabilities/assurance-execution/assurance_execution/artifacts/raw_member_selectors.py packages/capabilities/assurance-execution/assurance_execution/contracts/agent.py packages/capabilities/assurance-execution/assurance_execution/contracts/attempts.py packages/capabilities/assurance-execution/assurance_execution/plugin.py packages/capabilities/assurance-execution/assurance_execution/plugin-declaration.json packages/capabilities/assurance-execution/assurance_execution/resources/workflow/module.yaml packages/capabilities/assurance-execution/assurance_execution/operations/agent_skills.py packages/capabilities/assurance-execution/assurance_execution/resources/schemas/agent-results/run-result.v1.schema.json packages/capabilities/assurance-execution/assurance_execution/resources/skills/aa-run/SKILL.md packages/capabilities/assurance-execution/tests/test_artifact_pipeline.py tests/architecture/fixtures/agent-artifact-inventory.v1.json
git commit -m "feat: close Execution artifact contracts"
```

### Task 5: Close Intake, including dynamic case paths and bounded repair

**Files:**

- Modify: `packages/capabilities/assurance-intake/assurance_intake/contracts/artifacts.py`
- Create: `packages/capabilities/assurance-intake/assurance_intake/contracts/agent_results.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/artifacts/__init__.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/artifacts/path_resolvers.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/artifacts/raw_member_selectors.py`
- Create: `packages/capabilities/assurance-intake/assurance_intake/artifacts/projectors.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/contracts/agent.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/contracts/explore.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/contracts/attempts.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/plugin.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/plugin-declaration.json`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/operations/agent_skills.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/operations/finalize.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/resources/workflow/module.yaml`
- Create: `packages/capabilities/assurance-intake/assurance_intake/resources/policies/explore-source-read-v1.json`
- Create: `packages/capabilities/assurance-intake/assurance_intake/resources/schemas/agent-results/intake-result.v1.schema.json`
- Create: `packages/capabilities/assurance-intake/assurance_intake/resources/schemas/agent-results/explore-result.v1.schema.json`
- Create: `packages/capabilities/assurance-intake/assurance_intake/resources/schemas/agent-results/case-design-result.v1.schema.json`
- Create: `packages/capabilities/assurance-intake/assurance_intake/resources/schemas/agent-results/case-review-result.v1.schema.json`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/resources/skills/aa-intake/SKILL.md`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/resources/skills/aa-explore/SKILL.md`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/resources/skills/aa-case-design/SKILL.md`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/resources/skills/aa-case-repair/SKILL.md`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/resources/skills/aa-case-reviewer/SKILL.md`
- Create: `packages/capabilities/assurance-intake/tests/test_artifact_pipeline.py`
- Modify: `packages/capabilities/assurance-intake/tests/test_contracts.py`
- Modify: `packages/capabilities/assurance-intake/tests/test_workflow_module.py`
- Modify: `tests/architecture/fixtures/agent-artifact-inventory.v1.json`

**Interfaces:** four installed result models; `CaseDesignInputV1.case_delta_paths`; `case-document-v1` repeatable slot; complete post-image bounded repair; five exact semantic occurrences; all four Intake contracts explicitly network-none and command-secret-none.

- [ ] **Step 1: Write RED for all four IDs and five occurrences.** Assert primary and repair resolve the same case-design contract, while their semantic occurrence IDs remain distinct and both bind `requires_structured_output=True`.

- [ ] **Step 2: Write dynamic-authority, native-read, and pre-key dataflow RED.** Reject duplicate, unsorted, escaping, symlinked, over-count, over-size, wrong-change, and undeclared module paths before `authorize_resources` and Attempt-key derivation. Assert `PreparedT` and result payloads cannot add a case path. Require root `sut_read_paths` to enter each Intake InputT unchanged; Explore derives its read claims only by intersecting that tuple with the installed/digest-bound `explore-source-read-v1` policy. The policy permits shallow structural source views for route/controller, domain/model, RBAC/auth, and frontend page/component code; denies tests, generated/build/vendor trees, QA artifacts, business data, environment/credential/config secrets, and unsupported media/types; and is further constrained by the Kernel sensitive-input policy. A missing source view, forbidden path, Agent/prepare enumeration, late expansion, or snapshot/member mismatch fails before dispatch. Require the old-runtime coexistence graph projection to place the authenticated Explore current-change/baseline evidence, `ExploreAdvisoryV1` snapshot, and nullable typed `CaseReview`-derived `ReviewRepairContractV1` into the exact Explore/CaseDesign/CaseReview `InputT` values before `bind_attempt_dispatch`. First primary activation has `review_repair=None`; an internal validation repair inherits that exact nullable value plus the parent's full immutable Artifact evidence, `validation_error`, and exploration (so `None` remains valid before any CaseReview); an outer repeat requires the non-null contract produced by CaseReview finalization. The prior CaseDesign OutputT/private graph state carries one authenticated evidence value combining typed materialization entries/receipt with raw-post-image/sealed evidence for `proposal.md`. CaseReview constructs `baseline_file_digests` and `baseline_case_documents` deterministically from that complete immutable evidence, then the graph projects the contract into the next CaseDesign InputT—none of these three prepare handlers reads baseline/current-change files to synthesize it. All canonical values/digests enter `task_input_digest`. Mutating source, `exploration.json`, review files, proposal, or `.qa.yaml` after that snapshot cannot change `PreparedT`, authority, snapshot, mutation phase, or key.

- [ ] **Step 3: Write repair and finalizer RED.** Require complete post-images for exactly the authenticated case paths; assert `.qa.yaml`, matrix and case bytes come from typed values/projectors, Markdown stays raw, and finalizers do not parse typed files. The retained `proposal.md` uses complete `agent_raw_whole_file` bounded repair: missing baseline, missing/incomplete post-image, extra target, content/size/mode drift, or a non-preserved baseline mode fails. Typed case documents likewise require the complete authenticated set.

- [ ] **Step 4: Run RED.**

```bash
uv run pytest -q packages/capabilities/assurance-intake/tests/test_artifact_pipeline.py packages/capabilities/assurance-intake/tests/test_contracts.py packages/capabilities/assurance-intake/tests/test_workflow_module.py
```

Expected: no dynamic typed resolver exists, repair authority is not closed, and typed paths remain in raw output permissions.

- [ ] **Step 5: Implement result models, slots, projectors, graph projection, read policy, and skill semantics.** Make `CaseDesignInputV1.case_delta_paths` the sole sorted resolver input, carry the root `sut_read_paths`/policy digest through all Intake inputs, resolve Explore's exact bounded source claims before the key, and explicitly bind empty/network-none and empty/command-secret-none requirement sets for all four installed contracts. Return complete case documents/post-images, retain `proposal.md`, `requirement.md`, and case-review summary as raw, and move every byte-affecting transform before materialization. Update the coexistence `module.yaml` input projection so Explore current-change/baseline evidence, exploration, parent validation context, prior CaseDesign typed-document/materialization evidence, raw proposal post-image/seal evidence, and nullable review/repair data arrive as authenticated predecessor/current-trigger values in exact `InputT` values before key derivation; never reread them by project path. Make CaseReview finalization produce the next non-null `ReviewRepairContractV1` from that full immutable evidence. Remove all three ambient loaders (Explore, CaseDesign, CaseReview), including `_review_repair_contract(...)`; every pure prepare entry validates only its frozen `InputT` and is deterministic on crash replay. Native Explore reads are served only from the resulting immutable input snapshot and equality-tested against its manifest; an ambient endpoint or credential never upgrades Intake authority.

- [ ] **Step 6: Restrict permissions and finalizers.** Remove all typed paths and the dynamic typed root from OpenCode writes; preserve raw paths as explicit claims; consume models and aggregate receipt after materialization.

- [ ] **Step 7: Atomically promote every intended Intake typed row and verify parity/recovery.**

```bash
uv run pytest -q packages/capabilities/assurance-intake/tests packages/framework/graph-engine/tests/attempts/test_kernel_artifact_recovery.py tests/architecture/test_agent_artifact_inventory.py
```

Expected: four IDs/five occurrences close; dynamic cardinality is bounded; primary and repair exact-byte or approved semantic-migration parity is catalog-linked.

- [ ] **Step 8: Commit.**

```bash
git add packages/capabilities/assurance-intake/assurance_intake/contracts/artifacts.py packages/capabilities/assurance-intake/assurance_intake/contracts/agent_results.py packages/capabilities/assurance-intake/assurance_intake/artifacts/__init__.py packages/capabilities/assurance-intake/assurance_intake/artifacts/path_resolvers.py packages/capabilities/assurance-intake/assurance_intake/artifacts/raw_member_selectors.py packages/capabilities/assurance-intake/assurance_intake/artifacts/projectors.py packages/capabilities/assurance-intake/assurance_intake/contracts/agent.py packages/capabilities/assurance-intake/assurance_intake/contracts/explore.py packages/capabilities/assurance-intake/assurance_intake/contracts/attempts.py packages/capabilities/assurance-intake/assurance_intake/plugin.py packages/capabilities/assurance-intake/assurance_intake/plugin-declaration.json packages/capabilities/assurance-intake/assurance_intake/operations/agent_skills.py packages/capabilities/assurance-intake/assurance_intake/operations/finalize.py packages/capabilities/assurance-intake/assurance_intake/resources/workflow/module.yaml packages/capabilities/assurance-intake/assurance_intake/resources/policies/explore-source-read-v1.json packages/capabilities/assurance-intake/assurance_intake/resources/schemas/agent-results/intake-result.v1.schema.json packages/capabilities/assurance-intake/assurance_intake/resources/schemas/agent-results/explore-result.v1.schema.json packages/capabilities/assurance-intake/assurance_intake/resources/schemas/agent-results/case-design-result.v1.schema.json packages/capabilities/assurance-intake/assurance_intake/resources/schemas/agent-results/case-review-result.v1.schema.json packages/capabilities/assurance-intake/assurance_intake/resources/skills/aa-intake/SKILL.md packages/capabilities/assurance-intake/assurance_intake/resources/skills/aa-explore/SKILL.md packages/capabilities/assurance-intake/assurance_intake/resources/skills/aa-case-design/SKILL.md packages/capabilities/assurance-intake/assurance_intake/resources/skills/aa-case-repair/SKILL.md packages/capabilities/assurance-intake/assurance_intake/resources/skills/aa-case-reviewer/SKILL.md packages/capabilities/assurance-intake/tests/test_artifact_pipeline.py packages/capabilities/assurance-intake/tests/test_contracts.py packages/capabilities/assurance-intake/tests/test_workflow_module.py tests/architecture/fixtures/agent-artifact-inventory.v1.json
git commit -m "feat: close Intake artifact contracts"
```

### Task 6: Close all Generation contracts, four raw roots, and two fix allow-sets

**Files:**

- Modify: `packages/capabilities/assurance-generation/assurance_generation/contracts/artifacts.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/contracts/agent_results.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/contracts/agent.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/contracts/attempts.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/plugin.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/plugin-declaration.json`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/artifacts/__init__.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/artifacts/raw_member_selectors.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/artifacts/projectors.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/operations/planning.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/operations/review.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/operations/codegen.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/workflow/module.yaml`
- Create: `packages/capabilities/assurance-generation/assurance_generation/resources/schemas/agent-results/plan-result.v1.schema.json`
- Create: `packages/capabilities/assurance-generation/assurance_generation/resources/schemas/agent-results/plan-review-result.v1.schema.json`
- Create: `packages/capabilities/assurance-generation/assurance_generation/resources/schemas/agent-results/codegen-result.v1.schema.json`
- Create: `packages/capabilities/assurance-generation/assurance_generation/resources/schemas/agent-results/codegen-fix-result.v1.schema.json`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-api-plan/SKILL.md`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-api-plan-reviewer/SKILL.md`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-api-codegen/SKILL.md`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-api-codegen-fixer/SKILL.md`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-e2e-plan/SKILL.md`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-e2e-plan-reviewer/SKILL.md`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-e2e-codegen/SKILL.md`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-e2e-codegen-fixer/SKILL.md`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-fuzz-plan/SKILL.md`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-fuzz-plan-reviewer/SKILL.md`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-fuzz-codegen/SKILL.md`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-performance-plan/SKILL.md`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-performance-plan-reviewer/SKILL.md`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-performance-codegen/SKILL.md`
- Create: `packages/capabilities/assurance-generation/tests/test_artifact_pipeline.py`
- Modify: `packages/capabilities/assurance-generation/tests/test_workflow_module.py`
- Modify: `tests/architecture/fixtures/agent-artifact-inventory.v1.json`

**Interfaces:** 14 exact contract IDs; family enum `{api,e2e,fuzz,performance}`; 14 mapping/review/generated-files typed slots; four explicit primary raw roots (api/e2e/fuzz/performance codegen); two repeatable exact-file codegen-fix sets resolved from authenticated `allowed_paths`; exact eight-row input-conditioned network set and six-row network-none set; all 14 command-secret-none.

- [ ] **Step 1: Add one parameter row for each of the 14 exact Generation IDs.** The test must name every ID from the 33-contract table and link its skill, result-schema, projector/finalizer, permission, recovery and parity evidence; a loop over the live catalog is allowed only after equality with the frozen 14-ID set. The same table must assert that API/E2E/Fuzz/Performance plan-review and primary codegen are exactly the eight `input_conditioned` network rows and each declares only `sut-openapi-read-v1`, both fixers plus all four plans are the six `none` rows, and all 14 have empty command-secret requirements. Generation may carry a credential requirement identifier/alias contract forward as data for later Execution, but cannot resolve, inject, or observe its value.

- [ ] **Step 2: Add root/member, repair-input, input-snapshot, no-command, and network RED.** Assert exactly four primary roots are raw and family-bounded. Assert API/E2E fix contracts have no root authority: their repeatable exact-file resolver returns set-equal canonical paths from preclosed `CodegenFixInputV1.allowed_paths`, and a same-root unlisted sibling is denied. The prior primary `CodegenResultV2.repair.allowed_paths` must be sorted/unique and an exact set-subset of that same committed primary generated-files manifest's `files[*].repo_path`; deterministically map only that subset through `staged_generated_path`, and require fix InputT/bound members to be set-equal to the mapped subset. Replace prefix-style `under_write_root` acceptance with exact member equality; reject descendants, `.bak` siblings, family-allowed extras, and a full-manifest substitution when only a subset was approved. The coexistence workflow projection derives this finite set from the prior committed primary typed result/receipt before `bind_attempt_dispatch`; it may not enumerate the project or infer a root. Its canonical set enters `task_input_digest`, resource closure, bound Artifact digest, and Attempt key. Mutation after snapshot cannot expand it. Require complete whole-file raw repair for every exact member and complete typed post-image for the shared manifest; summary remains create, while shared manifest/members are bounded-repair with preserved baseline mode. Reject missing baseline/member, incomplete set, unlisted sibling, content/size/mode drift, or non-preserved mode. Typed JSON paths are deny holes beneath every broad primary claim. For all four plan/review/codegen families, require validated InputT to carry reviewed cases, family constraints, plan/mapping/target-set and baseline/tree identities needed by prepare, plus explicit sorted SUT source/test/tool-config read claims. Exact-set compare every Generation authority row to Task 1 and require all 14 toolchain tuples empty; a formatter/LSP/test/benchmark/code-generator command request, executable, concrete profile, live project/venv/cache, or install-on-demand path is rejected before dispatch. For the eight network-capable rows, test both explicit `offline_source` and exact `bound_sut` selection; the latter can reach only Product-issued aliases for selected frozen target rows, while registry/package/unbound/direct-IP/DNS-rebound/redirect targets are denied. The four plan-review skills preserve their current conditional live OpenAPI/SUT inspection through this bounded path; the two fixers remain source-only. A network-none row opening a connection, any raw URL/host field, a `qa`-only read set, or live-project enumeration is a RED failure.

- [ ] **Step 3: Add same-session RED.** Generation uses bounded native raw writes only and does not invoke a formatter, LSP, test runner, benchmark, or code generator. No skill writes or rereads mapping/review/generated-files JSON; every typed result carries the complete document/projector input. Prove that a later Execution Attempt can consume the committed generated sources only through its authenticated input snapshot and selected semantic runner recipe, never through the earlier Generation workspace or authority.

- [ ] **Step 4: Run RED.**

```bash
uv run pytest -q packages/capabilities/assurance-generation/tests/test_artifact_pipeline.py packages/capabilities/assurance-generation/tests/test_planning.py packages/capabilities/assurance-generation/tests/test_codegen.py packages/capabilities/assurance-generation/tests/test_workflow_module.py
```

Expected: only the API plan tracer is migrated; the remaining 13 result contracts and closed root claims are absent.

- [ ] **Step 5: Implement the four result families and all 14 bindings.** Reuse generic installed result/document models only where schemas are truly identical; bind a distinct immutable `ArtifactContract` per exact ID; retain every Markdown and generated-source slot as explicit raw. Declare the exact `8 input_conditioned / 6 none` network partition and `14 none` command-secret partition in production `contracts/attempts.py`; no constructor default may supply them.

- [ ] **Step 6: Move byte-determining logic into projectors and close permissions/dataflow.** Project mapping, review and generated-files documents before materialization; make finalizers receipt-driven; derive four primary raw roots plus two exact fix allowed-path sets before Attempt-key derivation. Validate the primary repair subset against the same primary manifest, apply `staged_generated_path` once, and persist the exact mapped set—never a root or prefix predicate. Update coexistence `module.yaml` so API/E2E fix receives that set from the authenticated committed primary typed result/receipt, not ambient files; the later StateGraph migration must preserve the same value in its private trigger envelope. Remove all 12 Generation ambient prepare reads: graph hydration supplies exact reviewed-case/family/plan/mapping/target/baseline plus the validated opaque network selectors to `InputT`, while complete Agent-readable source/test/tool-config claims are captured into the immutable input snapshot. Register the exact raw-member selectors and pure handler entries; retain and assert Task 2's empty toolchain tuples on all 14 rows. Update only the eight applicable plan-review/primary-codegen skills to use Product-issued SUT aliases for authenticated read-only OpenAPI/route-inspection branches and explicit offline source fixtures otherwise; test-backend/frontend access, inherited endpoint/proxy variables, command execution, and command-secret injection are forbidden for all Generation skills. Amend `aa-performance-codegen` so validation is described as preparation for **later Execution** to start the benchmark; the Generation Agent itself must not start Locust.

- [ ] **Step 7: Promote only the 14 named typed candidates and run the entire Feature.**

```bash
uv run pytest -q packages/capabilities/assurance-generation/tests tests/product/test_structured_artifact_tracer.py tests/architecture/test_agent_artifact_inventory.py
```

Expected: all 14 contracts are mixed, four primary roots and two exact fix sets remain raw, every typed JSON target is disjoint, an unlisted fix sibling is denied, and all catalog-linked parity/recovery cases pass.

- [ ] **Step 8: Commit.**

```bash
git add packages/capabilities/assurance-generation/assurance_generation/contracts/artifacts.py packages/capabilities/assurance-generation/assurance_generation/contracts/agent_results.py packages/capabilities/assurance-generation/assurance_generation/contracts/agent.py packages/capabilities/assurance-generation/assurance_generation/contracts/attempts.py packages/capabilities/assurance-generation/assurance_generation/plugin.py packages/capabilities/assurance-generation/assurance_generation/plugin-declaration.json packages/capabilities/assurance-generation/assurance_generation/artifacts/__init__.py packages/capabilities/assurance-generation/assurance_generation/artifacts/projectors.py packages/capabilities/assurance-generation/assurance_generation/artifacts/raw_member_selectors.py packages/capabilities/assurance-generation/assurance_generation/operations/planning.py packages/capabilities/assurance-generation/assurance_generation/operations/review.py packages/capabilities/assurance-generation/assurance_generation/operations/codegen.py packages/capabilities/assurance-generation/assurance_generation/resources/workflow/module.yaml packages/capabilities/assurance-generation/assurance_generation/resources/schemas/agent-results/plan-result.v1.schema.json packages/capabilities/assurance-generation/assurance_generation/resources/schemas/agent-results/plan-review-result.v1.schema.json packages/capabilities/assurance-generation/assurance_generation/resources/schemas/agent-results/codegen-result.v1.schema.json packages/capabilities/assurance-generation/assurance_generation/resources/schemas/agent-results/codegen-fix-result.v1.schema.json packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-api-plan/SKILL.md packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-api-plan-reviewer/SKILL.md packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-api-codegen/SKILL.md packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-api-codegen-fixer/SKILL.md packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-e2e-plan/SKILL.md packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-e2e-plan-reviewer/SKILL.md packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-e2e-codegen/SKILL.md packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-e2e-codegen-fixer/SKILL.md packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-fuzz-plan/SKILL.md packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-fuzz-plan-reviewer/SKILL.md packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-fuzz-codegen/SKILL.md packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-performance-plan/SKILL.md packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-performance-plan-reviewer/SKILL.md packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-performance-codegen/SKILL.md packages/capabilities/assurance-generation/tests/test_artifact_pipeline.py packages/capabilities/assurance-generation/tests/test_workflow_module.py tests/architecture/fixtures/agent-artifact-inventory.v1.json
git commit -m "feat: close Generation artifact contracts"
```

### Task 7: Close all five Quality Agent contracts

**Files:**

- Modify: `packages/capabilities/assurance-quality/assurance_quality/contracts/artifacts.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/artifacts/raw_member_selectors.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/contracts/agent_results.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/contracts/agent.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/contracts/attempts.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/plugin.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/plugin-declaration.json`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/artifacts/__init__.py`
- Create: `packages/capabilities/assurance-quality/assurance_quality/artifacts/projectors.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/operations/agent_skills.py`
- Create: `packages/capabilities/assurance-quality/assurance_quality/resources/schemas/agent-results/fact-baseline-result.v1.schema.json`
- Create: `packages/capabilities/assurance-quality/assurance_quality/resources/schemas/agent-results/inspect-result.v1.schema.json`
- Create: `packages/capabilities/assurance-quality/assurance_quality/resources/schemas/agent-results/issue-triage-result.v1.schema.json`
- Create: `packages/capabilities/assurance-quality/assurance_quality/resources/schemas/agent-results/issue-analysis-result.v1.schema.json`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/resources/skills/aa-fact-baseline/SKILL.md`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/resources/skills/aa-inspect/SKILL.md`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/resources/skills/aa-issue-triage-advisor/SKILL.md`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/resources/skills/aa-issue-analyzer/SKILL.md`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/resources/skills/aa-report-generator/SKILL.md`
- Create: `packages/capabilities/assurance-quality/tests/test_artifact_pipeline.py`
- Modify: `tests/architecture/fixtures/agent-artifact-inventory.v1.json`

**Interfaces:** four complete typed result documents, one structured raw-report result, five exact contracts, unchanged Quality resolution semantics; all five explicitly network-none and command-secret-none.

- [ ] **Step 1: Write five-ID RED.** Require catalog-linked result/skill/projector/finalizer/permission/recovery/parity evidence for fact-baseline, inspect, issue-triage, issue-analysis and report; assert the report remains raw-artifact-only but structured-result-required.

- [ ] **Step 2: Run RED.**

```bash
uv run pytest -q packages/capabilities/assurance-quality/tests/test_artifact_pipeline.py packages/capabilities/assurance-quality/tests/test_agent_skills.py
```

Expected: four typed result models/contracts are absent and report is not yet part of a complete five-row Feature matrix.

- [ ] **Step 3: Implement the four typed results and finish all five artifact bindings.** Move authoritative JSON values/projectors before materialization, keep report Markdown raw, remove typed permissions, consume typed values/receipts in finalization, and retain/exact-set assert Task 2's empty network/command-secret rows for every Quality contract. Ambient endpoint/credential variables cannot upgrade report or analysis authority.

- [ ] **Step 4: Promote four typed rows and verify the full Feature.**

```bash
uv run pytest -q packages/capabilities/assurance-quality/tests tests/product/test_structured_artifact_tracer.py tests/architecture/test_agent_artifact_inventory.py
```

Expected: four typed-only plus one raw-only contract pass exact permission, crash recovery, and parity tests.

- [ ] **Step 5: Commit.**

```bash
git add packages/capabilities/assurance-quality/assurance_quality/contracts/artifacts.py packages/capabilities/assurance-quality/assurance_quality/artifacts/raw_member_selectors.py packages/capabilities/assurance-quality/assurance_quality/contracts/agent_results.py packages/capabilities/assurance-quality/assurance_quality/contracts/agent.py packages/capabilities/assurance-quality/assurance_quality/contracts/attempts.py packages/capabilities/assurance-quality/assurance_quality/plugin.py packages/capabilities/assurance-quality/assurance_quality/plugin-declaration.json packages/capabilities/assurance-quality/assurance_quality/artifacts/__init__.py packages/capabilities/assurance-quality/assurance_quality/artifacts/projectors.py packages/capabilities/assurance-quality/assurance_quality/operations/agent_skills.py packages/capabilities/assurance-quality/assurance_quality/resources/schemas/agent-results/fact-baseline-result.v1.schema.json packages/capabilities/assurance-quality/assurance_quality/resources/schemas/agent-results/inspect-result.v1.schema.json packages/capabilities/assurance-quality/assurance_quality/resources/schemas/agent-results/issue-triage-result.v1.schema.json packages/capabilities/assurance-quality/assurance_quality/resources/schemas/agent-results/issue-analysis-result.v1.schema.json packages/capabilities/assurance-quality/assurance_quality/resources/skills/aa-fact-baseline/SKILL.md packages/capabilities/assurance-quality/assurance_quality/resources/skills/aa-inspect/SKILL.md packages/capabilities/assurance-quality/assurance_quality/resources/skills/aa-issue-triage-advisor/SKILL.md packages/capabilities/assurance-quality/assurance_quality/resources/skills/aa-issue-analyzer/SKILL.md packages/capabilities/assurance-quality/assurance_quality/resources/skills/aa-report-generator/SKILL.md packages/capabilities/assurance-quality/tests/test_artifact_pipeline.py tests/architecture/fixtures/agent-artifact-inventory.v1.json
git commit -m "feat: close Quality artifact contracts"
```

### Task 8: Close both Healing Agent contracts without changing effects

**Files:**

- Modify: `packages/capabilities/assurance-healing/assurance_healing/contracts/artifacts.py`
- Create: `packages/capabilities/assurance-healing/assurance_healing/contracts/agent_results.py`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/artifacts/__init__.py`
- Create: `packages/capabilities/assurance-healing/assurance_healing/artifacts/projectors.py`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/artifacts/path_resolvers.py`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/artifacts/raw_member_selectors.py`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/contracts/agent.py`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/contracts/attempts.py`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/plugin.py`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/plugin-declaration.json`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/operations/agent.py`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/operations/proposal.py`
- Create: `packages/capabilities/assurance-healing/assurance_healing/resources/schemas/agent-results/fix-proposal-result.v1.schema.json`
- Create: `packages/capabilities/assurance-healing/assurance_healing/resources/schemas/agent-results/coverage-repair-result.v1.schema.json`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/resources/skills/aa-fix-proposal/SKILL.md`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/resources/skills/aa-coverage-repair/SKILL.md`
- Create: `packages/capabilities/assurance-healing/tests/test_artifact_pipeline.py`
- Modify: `tests/architecture/fixtures/agent-artifact-inventory.v1.json`

**Interfaces:** `FixProposalAgentResultV1`, `CoverageRepairAgentResultV1`; fix-proposal typed-only; coverage-repair mixed with one typed receipt and one repeatable raw exact-file bounded-repair slot; both explicitly network-none and command-secret-none; existing allocation, heal-apply and proposal-approved effects unchanged.

- [ ] **Step 1: Write two-ID RED.** For each exact Healing ID, require complete typed value, skill/result-schema agreement, typed-path denial, receipt-driven finalizer, crash recovery and parity evidence. Keep fix-proposal typed-only. For coverage-repair, require one repeatable raw exact-file slot strictly set-equal at bind time to canonical validated `CoverageRepairInputV1.brief.allowed_test_files`; every path must already exist in the immutable input snapshot, expose its original bytes/mode in the Agent's read-only view, and bind `initial|repeat -> bounded_repair + agent_raw_whole_file + preserve_baseline`. Deny roots, creates, missing baselines, result-expanded paths, and files outside the set before launch. Its raw member selector covers the complete bound allowed set. Validate `CoverageRepairAgentResultV1.files_modified` as a sorted unique subset declaration, then at raw close/seal derive `actual_changed_set` across the complete bound set from immutable baseline versus raw post-image/sealed partition and require `actual_changed_set == files_modified ⊆ prekey_allowed_test_files`, with no typed/raw authority overlap. Test false-positive reports, unreported allowed changes, out-of-set/new/deleted files, and mode drift.

- [ ] **Step 2: Add effect-invariance RED.** Snapshot the three existing effect kinds, intent/receipt schemas, ordering and reconcile behavior; assert structured materialization adds no effect and cannot run after effect settlement.

- [ ] **Step 3: Run RED.**

```bash
uv run pytest -q packages/capabilities/assurance-healing/tests/test_artifact_pipeline.py packages/capabilities/assurance-healing/tests/test_effects.py packages/capabilities/assurance-healing/tests/test_recovery_faults.py
```

Expected: result models/artifact contracts are absent; existing effect tests remain green and become the invariant baseline.

- [ ] **Step 4: Implement both structured results, the typed receipts, and coverage raw repair authority.** Remove raw-write/read-back semantics and permissions only for the two typed receipt paths; move their byte-affecting construction before materialization. Preserve coverage-repair's exact-file write permission by installing the `brief.allowed_test_files` resolver and complete-set raw member selector from the baseline, snapshot each existing original file before dispatch, and expose those immutable bytes through the bounded Agent view. The result's `files_modified` cannot add authority; Kernel scans the complete bound set and authenticates exact equality with actual baseline/post-image differences before seal. Preserve baseline modes and reject false-positive reports, unreported changes, created, deleted, symlinked, mode-drifted, or out-of-set members according to the closed bounded-repair contract. Retain and exact-set assert Task 2's empty network/command-secret rows and keep approval/effect production in deterministic finalization.

- [ ] **Step 5: Promote both rows and verify.**

```bash
uv run pytest -q packages/capabilities/assurance-healing/tests tests/architecture/test_agent_artifact_inventory.py
```

Expected: fix-proposal is typed-only, coverage-repair is mixed, the final inventory has Healing `2 typed + 1 raw`, exact or documented semantic parity passes, raw test edits are limited to existing preauthorized files, and the three effect kinds and recovery traces are unchanged.

- [ ] **Step 6: Commit.**

```bash
git add packages/capabilities/assurance-healing/assurance_healing/contracts/artifacts.py packages/capabilities/assurance-healing/assurance_healing/contracts/agent_results.py packages/capabilities/assurance-healing/assurance_healing/artifacts/__init__.py packages/capabilities/assurance-healing/assurance_healing/artifacts/projectors.py packages/capabilities/assurance-healing/assurance_healing/artifacts/path_resolvers.py packages/capabilities/assurance-healing/assurance_healing/artifacts/raw_member_selectors.py packages/capabilities/assurance-healing/assurance_healing/contracts/agent.py packages/capabilities/assurance-healing/assurance_healing/contracts/attempts.py packages/capabilities/assurance-healing/assurance_healing/plugin.py packages/capabilities/assurance-healing/assurance_healing/plugin-declaration.json packages/capabilities/assurance-healing/assurance_healing/operations/agent.py packages/capabilities/assurance-healing/assurance_healing/operations/proposal.py packages/capabilities/assurance-healing/assurance_healing/resources/schemas/agent-results/fix-proposal-result.v1.schema.json packages/capabilities/assurance-healing/assurance_healing/resources/schemas/agent-results/coverage-repair-result.v1.schema.json packages/capabilities/assurance-healing/assurance_healing/resources/skills/aa-fix-proposal/SKILL.md packages/capabilities/assurance-healing/assurance_healing/resources/skills/aa-coverage-repair/SKILL.md packages/capabilities/assurance-healing/tests/test_artifact_pipeline.py tests/architecture/fixtures/agent-artifact-inventory.v1.json
git commit -m "feat: close Healing artifact contracts"
```

### Task 9: Close all six Improvement Agent contracts

**Files:**

- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/contracts/artifacts.py`
- Create: `packages/capabilities/assurance-improvement/assurance_improvement/contracts/agent_results.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/artifacts/__init__.py`
- Create: `packages/capabilities/assurance-improvement/assurance_improvement/artifacts/projectors.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/artifacts/raw_member_selectors.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/contracts/agent.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/contracts/attempts.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/plugin.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/plugin-declaration.json`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/operations/agent.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/operations/archive.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/operations/retro.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/operations/review.py`
- Create: `packages/capabilities/assurance-improvement/assurance_improvement/resources/schemas/agent-results/archive-result.v1.schema.json`
- Create: `packages/capabilities/assurance-improvement/assurance_improvement/resources/schemas/agent-results/retro-result.v1.schema.json`
- Create: `packages/capabilities/assurance-improvement/assurance_improvement/resources/schemas/agent-results/retro-eval-analysis-result.v1.schema.json`
- Create: `packages/capabilities/assurance-improvement/assurance_improvement/resources/schemas/agent-results/retro-issue-analysis-result.v1.schema.json`
- Create: `packages/capabilities/assurance-improvement/assurance_improvement/resources/schemas/agent-results/retro-workflow-analysis-result.v1.schema.json`
- Create: `packages/capabilities/assurance-improvement/assurance_improvement/resources/schemas/agent-results/improvement-review-result.v1.schema.json`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/resources/skills/aa-archive/SKILL.md`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/resources/skills/aa-retro/SKILL.md`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/resources/skills/aa-retro-eval-analysis/SKILL.md`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/resources/skills/aa-retro-issue-analysis/SKILL.md`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/resources/skills/aa-retro-workflow-analysis/SKILL.md`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/resources/skills/aa-improvement-reviewer/SKILL.md`
- Create: `packages/capabilities/assurance-improvement/tests/test_artifact_pipeline.py`
- Modify: `tests/architecture/fixtures/agent-artifact-inventory.v1.json`

**Interfaces:** six distinct structured result contracts and typed-only artifact contracts; all six explicitly network-none and command-secret-none; unchanged eight direct Attempt IDs/nine occurrences; unchanged Improvement effect set.

- [ ] **Step 1: Write six-ID RED.** Name archive, retro, three analysis IDs and improvement-review explicitly; for each require result schema, skill, projector/finalizer, permission, recovery and parity evidence.

- [ ] **Step 2: Add topology/effect RED.** Assert the six Agent migrations add no semantic node and do not change the eight direct Attempt contracts, nine direct occurrences, or existing Improvement effect kinds.

- [ ] **Step 3: Run RED.**

```bash
uv run pytest -q packages/capabilities/assurance-improvement/tests/test_artifact_pipeline.py packages/capabilities/assurance-improvement/tests/test_effects.py packages/capabilities/assurance-improvement/tests/test_retro.py packages/capabilities/assurance-improvement/tests/test_review.py
```

Expected: all six typed result/slot bindings are absent while direct Attempt/effect characterization remains green.

- [ ] **Step 4: Implement six complete results and byte projectors.** Remove raw typed-file protocols and permissions, move archive/retro/review byte construction before materialization, retain and exact-set assert Task 2's empty network/command-secret rows for all six contracts, and leave post-materialization finalizers deterministic and receipt-driven.

- [ ] **Step 5: Promote all six rows and verify.**

```bash
uv run pytest -q packages/capabilities/assurance-improvement/tests tests/architecture/test_agent_artifact_inventory.py
```

Expected: six typed-only Agent contracts close with catalog-linked parity/recovery; direct Attempt and effect inventories remain exact.

- [ ] **Step 6: Commit.**

```bash
git add packages/capabilities/assurance-improvement/assurance_improvement/contracts/artifacts.py packages/capabilities/assurance-improvement/assurance_improvement/contracts/agent_results.py packages/capabilities/assurance-improvement/assurance_improvement/artifacts/__init__.py packages/capabilities/assurance-improvement/assurance_improvement/artifacts/projectors.py packages/capabilities/assurance-improvement/assurance_improvement/artifacts/raw_member_selectors.py packages/capabilities/assurance-improvement/assurance_improvement/contracts/agent.py packages/capabilities/assurance-improvement/assurance_improvement/contracts/attempts.py packages/capabilities/assurance-improvement/assurance_improvement/plugin.py packages/capabilities/assurance-improvement/assurance_improvement/plugin-declaration.json packages/capabilities/assurance-improvement/assurance_improvement/operations/agent.py packages/capabilities/assurance-improvement/assurance_improvement/operations/archive.py packages/capabilities/assurance-improvement/assurance_improvement/operations/retro.py packages/capabilities/assurance-improvement/assurance_improvement/operations/review.py packages/capabilities/assurance-improvement/assurance_improvement/resources/schemas/agent-results/archive-result.v1.schema.json packages/capabilities/assurance-improvement/assurance_improvement/resources/schemas/agent-results/retro-result.v1.schema.json packages/capabilities/assurance-improvement/assurance_improvement/resources/schemas/agent-results/retro-eval-analysis-result.v1.schema.json packages/capabilities/assurance-improvement/assurance_improvement/resources/schemas/agent-results/retro-issue-analysis-result.v1.schema.json packages/capabilities/assurance-improvement/assurance_improvement/resources/schemas/agent-results/retro-workflow-analysis-result.v1.schema.json packages/capabilities/assurance-improvement/assurance_improvement/resources/schemas/agent-results/improvement-review-result.v1.schema.json packages/capabilities/assurance-improvement/assurance_improvement/resources/skills/aa-archive/SKILL.md packages/capabilities/assurance-improvement/assurance_improvement/resources/skills/aa-retro/SKILL.md packages/capabilities/assurance-improvement/assurance_improvement/resources/skills/aa-retro-eval-analysis/SKILL.md packages/capabilities/assurance-improvement/assurance_improvement/resources/skills/aa-retro-issue-analysis/SKILL.md packages/capabilities/assurance-improvement/assurance_improvement/resources/skills/aa-retro-workflow-analysis/SKILL.md packages/capabilities/assurance-improvement/assurance_improvement/resources/skills/aa-improvement-reviewer/SKILL.md packages/capabilities/assurance-improvement/tests/test_artifact_pipeline.py tests/architecture/fixtures/agent-artifact-inventory.v1.json
git commit -m "feat: close Improvement artifact contracts"
```


### Task 10: Correct research, design, Feature, Product, and master-plan wording

**Files:**

- Modify: `docs/superpowers/specs/2026-09-01-structured-artifact-pipeline-design.md`
- Modify: `docs/research/2026-08-31-opencode-structured-output-json-schema.md`
- Modify: `docs/superpowers/specs/2026-08-31-python-native-langgraph-assurance-design.md`
- Modify: `docs/superpowers/specs/2026-08-31-langgraph-assurance-boot-runtime-design.md`
- Modify: `docs/superpowers/plans/2026-08-31-semantic-attempt-kernel.md`
- Modify: `docs/superpowers/plans/2026-08-31-feature-stategraph-migration.md`
- Modify: `docs/superpowers/plans/2026-08-31-langgraph-product-cutover.md`
- Modify: `docs/superpowers/plans/2026-08-31-python-native-langgraph-migration.md`
- Modify: `docs/superpowers/plans/2026-09-01-structured-artifact-pipeline.md`
- Modify: `docs/superpowers/plans/2026-09-01-artifact-kernel-foundation.md`
- Modify: `docs/superpowers/plans/2026-09-01-opencode-structured-output-gate.md`
- Modify: `docs/superpowers/plans/2026-09-01-agent-artifact-contract-migration.md`
- Create: `tests/architecture/test_structured_artifact_wording.py`

**Interfaces:** one closed executor name (`ResolvedStructuredAgentExecutor`), one Feature flag (`requires_structured_output`), one concrete adapter capability (`opencode_structured_output`), `ArtifactContract`/`ArtifactSlot`, exact 25-stage trace, pure `prepare(InputT)`, blob-backed raw finalization, provider-neutral structured network/command-secret contracts, explicit Structured Artifact Pipeline subphase before Checkpoint S closure.

- [ ] **Step 1: Write the wording RED.** Scope the test to the all twelve files above, including this plan, the current master, and both authoritative current child plans. Reject stale executor/flag/schema-writer recommendations, require the frozen names, and require the master Checkpoint S to link this plan and enumerate its gate. In the current Structured Artifact spec require the exact 25-stage trace (including close/capture/persist raw post-image), `prepare(InputT)` with no workspace/context read, and mixed/raw finalization through only the durable blob-backed raw post-image. Also require the exact 69-slot, final `34 typed/35 raw`, `14/18/1` classification, initial raw owner totals `2/44/3/6/9/5`, final retained-raw totals `0/30/1/0/3/1`, and final typed totals `2/14/2/6/6/4`; coverage-repair's repeatable exact-file `brief.allowed_test_files` authority, existing immutable baselines, raw whole-file bounded repair, preserve mode, complete-set raw selector, and `actual_changed_set == files_modified ⊆ prekey_allowed_test_files`; the exact mutation-phase selector wiring, live graph/domain/fixture three-way equality at StateGraph cutover, Product snapshot-admission assembly, immutable raw-post-image/frozen-promotion ownership, zero new semantic Agent occurrence, the exact four Product-owned semantic runner requirements with `2 Execution/31 none`, required public `sut_network_*`/`sut_auth_*` selector fields, exact `10/23` network and `2/31` command-secret classifications, and matching requirement-registry/executor/dispatch/key/workspace/minimal-context/broker-registration/closure/terminal-receipt/quiesce/recovery digest fields. The wording guard fails if any plan uses a raw endpoint/value/handle as business input, lets Generation or another non-Execution contract infer command authority, conflates the full command-secret binding catalog with an Attempt-selected set, or omits toolchain/network/secret replay evidence.

- [ ] **Step 2: Run RED.**

```bash
uv run pytest -q tests/architecture/test_structured_artifact_wording.py
```

Expected: the older design/plans still name an overlapping composite executor/provider-schema flag, and the research note still recommends a custom Schema writer.

- [ ] **Step 3: Correct the current normative spec before retrofit implementation.** Only after OpenCode Task 0 closes Checkpoint S0 green, and before any other Structured R1 source task, change the spec's stale 68-slot/final `34/34`/`15/17/1` statements to exact 69-slot/final `34 typed/35 raw`/`14 typed-only, 18 mixed, 1 raw-only`, with initial raw owner totals `2/44/3/6/9/5`, final retained-raw totals `0/30/1/0/3/1`, and final typed totals `2/14/2/6/6/4`. Add coverage-repair's repeatable raw exact-file slot resolved only from validated `brief.allowed_test_files`: existing immutable input-snapshot baselines and read-only original Agent view are mandatory; both phases are `bounded_repair + agent_raw_whole_file + preserve_baseline`; the raw selector covers the complete bound set; and seal computes `actual_changed_set` from all baselines/post-images and requires `actual_changed_set == files_modified ⊆ prekey_allowed_test_files`. Freeze the four semantic runner requirement rows and exact mapping—both Execution contracts carry all four, the other 31 contracts carry none—and explicitly forbid current Generation formatter/LSP/test/benchmark/command authority. Then expand Decision 10 and its acceptance/coverage tables from the old exact 21-step trace to the exact 25-stage trace in this suite. Replace the remaining workspace-capable prepare seam with strict `prepare(InputT)` and require all prior evidence hydration before key derivation. Replace mixed/raw finalizer access to mutable workspace with `ReadOnlyRawArtifactPort` over the durable authenticated `RawPostImagePersisted` blob view. Record these as refinements of the same design, not optional implementation deviations. This documentation/test commit is the first post-S0 retrofit commit in master R1; Task 0 is the only source task allowed before it.

- [ ] **Step 4: Correct the research conclusion.** Preserve dated investigation evidence, but mark the custom Schema-writer proposal superseded by pinned built-in `format.schema` plus terminal `info.structured`; retain OpenCode 1.18.4 as a historical negative and v1.18.26/`774cc7c` as the current `message-roundtrip-red` release until a later exact version passes the qualification matrix.

- [ ] **Step 5: Correct the two older designs and three child plans.** Replace the stale phase-bundle name with `ResolvedStructuredAgentExecutor`, replace provider-schema Feature vocabulary with `requires_structured_output`, keep provider capability selection in Product, and insert the artifact/materialization/recovery interfaces without adding graph nodes.

  Amend Semantic Attempt Task 10's exact future files `packages/framework/graph-engine/graph_engine/attempts/node_factory.py` and `packages/framework/graph-engine/tests/attempts/test_node_factory.py`. `AttemptNodeFactory.attempt(...)` keeps the installed `activation(state) -> BusinessActivation` selector, adds required graph-owned `mutation_phase(state) -> ArtifactMutationPhase` for structured contracts, and evaluates each exactly once against the same anchored checkpoint state. It freezes both values in one internal pre-key selection before input/path/resource binding; the existing BusinessActivation already enters `AttemptKey` directly, while the phase enters `BoundArtifactContract`, `BoundAttemptDispatch`, and the key through their authenticated digests. `semantic_occurrence_id` is exactly the existing `semantic_node_id`, never a second runtime identity. Kernel/recovery execute zero activation/phase/input/path selectors. Tests prove a first business call that happens at legacy round 1 can still be `initial`, a distinct late/current-trigger activation is `repeat`, exact arrival replay retains the same pair, and InputT/prompt/result attempts to inject phase are ignored/rejected.

  Amend the already-existing Feature StateGraph implementations in all six `assurance_*/graphs/state.py`, `graphs/nodes.py`, and `graphs/factory.py` packages plus each `packages/capabilities/assurance-*/tests/test_graph_factory.py`; also amend Intake/Generation `test_graph_join_any.py`. Persist phase in a private inbox/current-trigger/dispatch-cursor envelope before Attempt dispatch, never in public/Feature business `InputT`. The exact 17 cross-round sites are: eight Generation plan/plan-review sites driven by four plan-round inboxes; Intake case-design primary/repair/case-review driven by the advance inbox; Execution run, Quality issue-analysis, and Healing fix-proposal driven by Product's failed inbox; Quality fact-baseline/inspect and Healing coverage-repair driven by Product's coverage inbox. Exactly 16 sites select `initial` for the first distinct activation and `repeat` for later distinct activations; `intake.case-design.repair` is constant `repair`. An internal case-design repair inherits its parent's exact nullable review-repair value, typed document/receipt refs, validation error, and exploration; only an outer repeat requires the non-null CaseReview-finalized contract. API/E2E codegen-fix sites are also constant `repair` and currently have no back edge; the remaining 15 sites are constant `initial`. Do not infer phase from `rounds_used`, child-subgraph generation, `validation_attempt`, filesystem state, or any current Agent InputT. Each network-capable Feature state accepts only its Product-projected canonical `sut_network_selections` rows/digest for installed requirement IDs, carries them unchanged into the affected `InputT`, and rejects root-global, missing, extra, or cross-requirement rows; Feature nodes never inspect the target catalog or repartition IDs.

  Amend the already-existing Product StateGraphs in `packages/products/assurance-product/assurance_product/graphs/state.py`, `graphs/factory.py`, `graphs/execute.py`, and `graphs/full.py`, covered by `tests/product/test_product_stategraph_flow.py` and `tests/product/test_product_join_any.py`. The four Generation, one Intake, and two Product inbox families (seven total) persist/replay the private selection before invoking nested Feature graphs. Each dry/runtime factory emits an internal canonical `AttemptSiteCatalog` containing `(semantic_node_id, contract_id, mutation-phase selector provenance/domain)` and source digest. Boot requires live catalog = `ArtifactContract.occurrence_mutation_phases` = frozen exact 34-occurrence fixture and folds that catalog digest into the existing per-entrypoint contract digest; `GraphBuildManifest` keeps exactly `revision`, `entrypoint_contract_digests`, and `attempt_contract_digests`. This adds zero node, edge, Agent contract, or semantic occurrence. The Product root adapter validates public opaque target IDs once against the frozen catalog, builds and checkpoints the private requirement→target selection map/digest before nested dispatch, and replays it without re-resolving DNS/catalog state. Product-full tests select concurrent OpenAPI/backend/frontend requirements, route only declared rows to each nested Feature, resume from checkpoint with identical digests, and reject cross-requirement tampering. This private authority spine adds no public resolution or graph node.

  Extend, rather than recreate, Product Task 3's existing `packages/products/assurance-product/assurance_product/runtime_ports.py` and its `application.py`/`cli.py` wiring, with `tests/product/test_product_runtime_ports.py` and `tests/product/test_cli_sqlite_system_interrupt.py`. Capability Task 11 adds the durable Attempt-artifact store, v2 workspace/input-snapshot/frozen-promotion adapter, `AttemptSecretLifetimeFactoryPort`, snapshot admission, and structured command sandbox/broker to the same Product lifetime without depending on promoted certification. The secret lifetime resolves exactly the selected executor's authorized handles, proves the anchored opaque generation set, and gives Kernel the generation-scoped `SecretPort` plus admission scanner; it never puts a scanner or raw secret view in durable state or a general `AttemptExecutionContext`. Restart reconstructs the same policy from durable authorized handle identities and expected generation-set digest; a changed/unprovable generation or a payload containing another Attempt's secret fails before blob/event publication. Post-certification Task 12's real Adapter→Kernel E2E covers this production assembly, restart, encoded/chunk-split canaries, immutable raw post-image, delayed background staging drift, and cross-handle rejection.

- [ ] **Step 6: Correct the master plan.** Add this resume tranche after completed Product T5a and before T5b; make the 33/34/69 inventory and final `34 typed/35 raw`, `14/18/1` classification, Healing coverage-repair exact-file bounded-repair closure, exact four semantic runner rows with `2 Execution/31 none`, exact `10/23` network and `2/31` command-secret partitions, public opaque selector spine, Product target-catalog and requirement-to-handle carrier, authorized-handle union, default-deny command gateway, write-only one-shot injection, qualification, all-Feature evidence, dynamic root tests, snapshot-admission production wiring, 25-stage Kernel trace, command/network transcript and injection receipt closure, raw closure/capture/frozen-promotion recovery, phase-selector StateGraph bridge, documentation correction and CI gate explicit Checkpoint S prerequisites. The master must show the same fields in resolved executor, dispatch, Attempt key, workspace/access, ephemeral call context, raw-write closure, terminal receipt, drift checks, and replay paths as the two Structured child plans. It schedules Capability Task 11 before OpenCode Tasks 5/7 and binding build, then closes only with Task 12's protected producer→wheel/digest artifact→fresh downstream gate; ordinary PR CI is explicitly non-closing.

- [ ] **Step 7: Verify exact wording.**

```bash
uv run pytest -q tests/architecture/test_structured_artifact_wording.py
rg -n 'ResolvedStructuredAgentExecutor|requires_structured_output|opencode_structured_output|ArtifactContract|Checkpoint S|69 slots|34 typed/35 raw|brief.allowed_test_files|close_agent_raw_writes|RawPostImagePersisted|sut_network|structured_network|command_secret' docs/superpowers/specs/2026-09-01-structured-artifact-pipeline-design.md docs/superpowers/specs/2026-08-31-python-native-langgraph-assurance-design.md docs/superpowers/specs/2026-08-31-langgraph-assurance-boot-runtime-design.md docs/superpowers/plans/2026-08-31-semantic-attempt-kernel.md docs/superpowers/plans/2026-08-31-feature-stategraph-migration.md docs/superpowers/plans/2026-08-31-langgraph-product-cutover.md docs/superpowers/plans/2026-08-31-python-native-langgraph-migration.md docs/superpowers/plans/2026-09-01-structured-artifact-pipeline.md docs/superpowers/plans/2026-09-01-artifact-kernel-foundation.md docs/superpowers/plans/2026-09-01-opencode-structured-output-gate.md docs/superpowers/plans/2026-09-01-agent-artifact-contract-migration.md
```

Expected: test exits `0`; every plan layer uses the same ownership and naming, and no plan tells Feature code to select an adapter-specific mechanism.

- [ ] **Step 8: Commit the ignored documentation explicitly.**

```bash
git add -f docs/superpowers/specs/2026-09-01-structured-artifact-pipeline-design.md docs/research/2026-08-31-opencode-structured-output-json-schema.md docs/superpowers/specs/2026-08-31-python-native-langgraph-assurance-design.md docs/superpowers/specs/2026-08-31-langgraph-assurance-boot-runtime-design.md docs/superpowers/plans/2026-08-31-semantic-attempt-kernel.md docs/superpowers/plans/2026-08-31-feature-stategraph-migration.md docs/superpowers/plans/2026-08-31-langgraph-product-cutover.md docs/superpowers/plans/2026-08-31-python-native-langgraph-migration.md docs/superpowers/plans/2026-09-01-structured-artifact-pipeline.md docs/superpowers/plans/2026-09-01-artifact-kernel-foundation.md docs/superpowers/plans/2026-09-01-opencode-structured-output-gate.md docs/superpowers/plans/2026-09-01-agent-artifact-contract-migration.md
git add tests/architecture/test_structured_artifact_wording.py
git commit -m "docs: align structured artifact migration plans"
```

### Task 11: Install the pre-certification production Structured Runtime foundation

**Files:**

- Create: `packages/products/assurance-product/assurance_product/runtime_ports.py`
- Create: `packages/products/assurance-product/assurance_product/secret_generation_key.py`
- Modify: `packages/products/assurance-product/assurance_product/cli.py`
- Modify: `packages/products/assurance-product/assurance_product/product.py`
- Modify: `packages/products/assurance-product/assurance_product/network_targets.py`
- Modify: `packages/products/assurance-product/assurance_product/command_network.py`
- Modify: `packages/products/assurance-product/assurance_product/command_secrets.py`
- Modify: `packages/framework/graph-engine/graph_engine/runtime/secret_sources.py`
- Modify: `packages/framework/graph-engine/tests/runtime/test_secret_authorization.py`
- Create: `tests/product/test_product_runtime_ports.py`
- Create: `tests/product/test_secret_generation_key.py`
- Create: `tests/product/test_cli_structured_secret_authorization.py`

**Interfaces:** production `ProductRuntimePorts`; protected `SecretGenerationKeyPort`; Product-owned owner-only absolute-file implementation; `InvocationRuntimeAuthorization` source bridge to HMAC-backed `SecretMaterialGenerationPort` returning the framework-owned provider-neutral `ResolvedSecretGeneration`/`SecretGenerationSetIdentity`; `AttemptSecretLifetimeFactoryPort`; durable `AttemptArtifactStore`; v2 task-workspace/input-snapshot/frozen-promotion adapter; snapshot-admission port; resolved Product network-target catalog/default-deny command gateway; write-only command-secret injection sink/port; immutable Kernel-bound `BoundStructuredToolchainInvocationScope` handoff through broker registration; command sandbox/broker plus durable `StructuredCommandExecutionStore`. This task proves the production composition with test-owned qualified dependencies and does not read, require, synthesize, or promote an OpenCode certification record or binding wheel.

- [ ] **Step 1: Write the production-runtime RED without a certification dependency.** In `test_product_runtime_ports.py` require one Product-owned composition that opens/closes the durable `AttemptArtifactStore`, `StructuredCommandExecutionStore`, v2 workspace/input-snapshot/frozen-promotion adapter, exact-handle secret lifetime/admission scanner, resolved target catalog, default-deny command gateway, write-only injection sink, and command sandbox/broker under the same Attempt fence and ordered lifetime. Broker registration receives the immutable full `BoundStructuredToolchainInvocationScope` directly from Kernel, persists/authenticates its digest through workspace/store/receipt, accepts only exact unused logical-recipe/typed-parameter rows, derives deterministic argv under the test-owned qualified profile, and never parses Feature `InputT` or accepts model argv. Cover a legal zero-recipe Execution scope, one exact selected-tests row, one bounded-performance row, unknown/cross-family/broadened/parameter-drift/duplicate denials, and restart adoption without re-execution. Broker/gateway/sink close and command/network reconciliation precede secret-lifetime/store close. Use explicit test-owned qualified dependency values excluded from Product declarations; assert no test reads a promoted OpenCode record or installed binding wheel. In `test_secret_generation_key.py`, require the production generation-key source to accept only an explicit key ID plus an absolute, owner-owned, owner-only, no-follow regular key file outside the worktree and runtime database; reject relative/worktree/database/env-provided paths, symlinks, hard links/replacement races, group/other permission bits, wrong owner, missing/empty/oversize keys, and unstable reads. In `test_cli_structured_secret_authorization.py`, freeze CLI startup/dispatch authorization: startup validates the supplied source catalog and requires only adapter-always handles; it neither requires nor resolves every handle in the complete command-secret binding catalog. `bind_attempt_dispatch(...)` is the sole writer of the immutable `authorized_secret_handles` tuple/digest and computes it as adapter activity handles union selected value-free command handles before the key. After anchored root/input selection and immutable dispatch binding, the CLI bridge may only recompute that expected union, compare it to the frozen dispatch, and prove the selected source rows exist; it cannot assign, replace, reorder, or expand either field. Cover `offline_source + none` without an admin source, `bound_sut + none` without an admin source, `bound_sut + bound_command_secrets` with the selected admin source, and pre-key rejection of `offline_source + bound_command_secrets`; restart adopts the same authorized set and missing/rotated sources fail before publication.

- [ ] **Step 2: Add the pre-certification fail-closed invariant.** Compose `ProductRuntimePorts` against explicit test-owned toolchain/network/broker dependencies and the frozen Task 2 authority declarations. Prove input-selection → bound invocation scope → dispatch/key → workspace/broker/store/receipt continuity, selection-time secret authorization, key generation, lifetime/store/fence ordering, snapshot admission, broker registration, quiescence adoption, and close/reconcile behavior without claiming the production OpenCode capability. A missing test dependency fails locally; it cannot be replaced with an empty certification record, an advertised production capability, a hand-written matrix row, or a synthetic binding wheel.

  Run the RED before implementing production ports:

  ```bash
  uv run pytest -q tests/product/test_product_runtime_ports.py tests/product/test_secret_generation_key.py tests/product/test_cli_structured_secret_authorization.py packages/framework/graph-engine/tests/runtime/test_secret_authorization.py
  ```

  Expected: FAIL because `ProductRuntimePorts`, the production generation-key port/file implementation, and selection-time authorization bridge do not exist; no test may pass by consulting a certification record or binding wheel.

- [ ] **Step 3: Implement the real Product port composition and secret-generation proof.** `runtime_ports.py` owns lifecycle and close ordering for the durable Attempt-artifact store, durable command execution store, v2 task workspace, immutable input snapshot, frozen promotion set, snapshot admission, resolved target catalog, default-deny network gateway, write-only injection sink, and the Task 6 command sandbox/broker. Kernel's registration request is the only route by which the complete bound invocation scope reaches the Product broker; runtime composition authenticates it against dispatch/key/profile, while adapter/model/plugin requests carry only scope digest plus logical recipe/typed parameters. It opens both stores before activities, shares the same current Attempt fence, requires command/injection/network close and transcript reconciliation before raw-write closure, and closes broker/sandbox/gateway/injection sink/secret lifetimes before stores. It obtains a host-owned generation key and key ID through `SecretGenerationKeyPort`; neither may come from the worktree, runtime database, Agent environment, prompt, or durable Attempt data.

  Implement the production port in `secret_generation_key.py`. The executing CLI receives an explicit `--secret-generation-key-id` and absolute `--secret-generation-key-file`; no environment-variable fallback or generated/default key is permitted. Canonically resolve and reject any path inside the SUT worktree or runtime database tree, open from an already opened parent dirfd with `O_NOFOLLOW`, require one owner-owned regular file with no group/other permission bits and exactly one stable inode, stream a bounded non-empty key while checking device/inode/size/mtime before and after, and retain key bytes only in the host-owned in-memory lifetime. The returned port exposes only `(key_id, keyed-HMAC operation)`, not raw key bytes. Missing/wrong permissions, symlink/replacement, invalid key ID, or unreadable key fails before stores, workspace, source resolution, or activity dispatch. A changed key ID or key bytes is a generation rotation: new Attempts may use it, but adoption of an existing Attempt whose durable `(algorithm_id, key_id, generation_set_digest)` cannot be reproduced is rejected before blob/event publication.

  Change `cli.py` rather than preserving the current eager `_required_handles(composition)` behavior. `_authorize_secrets(...)` authenticates the syntax and stable identities of only the source rows supplied by the operator and requires the adapter-always handle subset at startup; complete command-secret binding-catalog handles are authorization possibilities, not globally mandatory sources and their values are not resolved at startup. `_plan_start(...)` freezes the internal `ResolvedProductInputV1`; the graph/node factory freezes BusinessActivation/phase and calls `bind_attempt_dispatch(...)`, which alone computes immutable `authorized_secret_handles = adapter_activity_handles ∪ selected_command_handles` plus its digest before `derive_attempt_key(...)`. The CLI/runtime authorization bridge recomputes the expected union from the resolved executor and selected value-free bound secrets, requires exact equality with the frozen dispatch, and proves every selected handle has an authenticated source row before key handoff/Kernel entry; it never mutates the dispatch. Resource authorization then resolves values before creating any workspace. Resume/open repeats the comparison against the durable authorized-handle tuple/digest and complete expected `SecretGenerationSetIdentity`. Thus offline and bound-but-unauthenticated runs may omit `sut-admin-credential-v1`'s backing source, the authenticated bound mode must supply it, an unselected supplied source remains unresolved/invisible, and offline-plus-secret mode fails at pre-key selection regardless of supplied sources.

  Bridge the existing `InvocationRuntimeAuthorization` source model into this composition explicitly: its exact authorized `HANDLE=env:NAME | file:ABS_PATH` rows are the only production source catalog; a Product adapter implements `SecretMaterialGenerationPort` by resolving only the authenticated `dispatch.authorized_secret_handles` subset after resource authorization and returning one framework-owned `ResolvedSecretGeneration(identity, materials)`, then passes it to `AttemptSecretLifetimeFactoryPort`. The factory must prove that this subset equals adapter activity handles union selected command handles, propagate `identity` unchanged, and reject a missing selected source, an extra resolved source, duplicate handle, unsupported source kind, changed env/file identity, or a value not admitted by the current authorization. Same-generation restart resolves the same authorized sources and adopts against the complete durable identity; no unselected authorization row is exposed to the lifetime, adapter, broker, or scanner.

  Freeze the Product implementation algorithm as `assurance-secret-generation-hmac-sha256-v1`. For each exact authorized handle, the Product source adapter resolves one value and canonical source identity. HMAC input is unambiguous and streaming: the ASCII domain tag plus NUL; unsigned 64-bit big-endian length + canonical UTF-8 metadata JSON bytes for `(algorithm_id, key_id, handle, source_provider_id, source_kind, source_locator, value_encoding)`; then unsigned 64-bit big-endian raw-value length followed by the exact UTF-8 or binary value chunks. There is no concatenated tuple or canonical-JSON encoding of secret bytes. Reject more than 256 handles, a single value over 16 MiB, aggregate values over 64 MiB, length overflow, malformed UTF-8 for text sources, duplicate handles, or a source without stable identity. File sources use dirfd-relative no-follow regular-file reads, bind device/inode/size/mtime before and after streaming, and reject replacement/torn reads. The per-handle `opaque_generation_id` is the lowercase HMAC digest; the aggregate generation-set digest is canonical SHA-256 over the sorted nonsecret `(algorithm_id, key_id, handle, source_provider_id, source_kind, source_locator, opaque_generation_id)` rows. Return exactly `SecretGenerationSetIdentity(algorithm_id, key_id, generation_set_digest)` with the materials; Kernel does not recompute it from the narrower material projection.

  Artifact Task 6's `ResourceAuthorizationRecord` persists only that exact identity's algorithm ID, key ID, and aggregate opaque generation-set digest; Product does not add another durable record or alternate digest. Never persist/log a key, value, plaintext digest, per-handle row, source locator, or reusable unkeyed per-secret verifier. Adoption requires exact algorithm/key equality and uses `hmac.compare_digest` against the expected aggregate. The same resolved values back both the generation-scoped existing `SecretPort` and `AttemptSnapshotAdmissionPort`, and are destroyed together at lifetime close. Missing/wrong key, changed value/source generation, source drift between views, or unavailable proof fails before workspace/blob/event work. Test one cross-plan golden identity through Product generation → lifetime → ResourceAuthorizationRecord reopen, plus framing vectors and chunk-boundary invariance; same key/value restart; same locator with rotated value; key/algorithm rotation; missing/wrong key; limits; file replacement during read; another handle; and rotation after blob publication but before event adoption.

- [ ] **Step 4: Run the pre-certification production-foundation suite.**

```bash
uv run pytest -q tests/product/test_product_runtime_ports.py tests/product/test_secret_generation_key.py tests/product/test_cli_structured_secret_authorization.py packages/framework/graph-engine/tests/runtime/test_secret_authorization.py
```

Expected: all exit `0` without opening a promoted qualification record or installed binding wheel.

- [ ] **Step 5: Commit the production foundation before OpenCode Tasks 5/7 and binding build.**

```bash
git add packages/products/assurance-product/assurance_product/runtime_ports.py packages/products/assurance-product/assurance_product/secret_generation_key.py packages/products/assurance-product/assurance_product/cli.py packages/products/assurance-product/assurance_product/product.py packages/products/assurance-product/assurance_product/network_targets.py packages/products/assurance-product/assurance_product/command_network.py packages/products/assurance-product/assurance_product/command_secrets.py packages/framework/graph-engine/graph_engine/runtime/secret_sources.py packages/framework/graph-engine/tests/runtime/test_secret_authorization.py tests/product/test_product_runtime_ports.py tests/product/test_secret_generation_key.py tests/product/test_cli_structured_secret_authorization.py
git commit -m "feat: install production structured runtime foundation"
```

### Task 12: Consume certification and close Checkpoint S in CI

**Dependency:** Task 11 and OpenCode Task 7 are committed, including the exact promoted transport/matrix records. Task 12 first implements and locally verifies the release workflow/gate, then commits that implementation as one candidate revision and publishes that exact commit through the protected branch/tag process. Only the workflow definition contained in that already-published candidate revision may run; it is authorized to rerun only Task 7's binding-build/install verification, not qualification or promotion. This task may not rely on a wheel installed by a prior local process: the protected run of the candidate revision must produce the external wheel, `binding-wheel-build-v1.json`, and independent SHA-256 file as one immutable workflow artifact and pass all three explicitly to its downstream Checkpoint S job. External qualification inputs and generated wheel/build-record bytes are never committed to Git. Checkpoint S remains open until that exact candidate revision's downstream job is green; any post-run source change creates a new candidate and requires the whole protected run again.

**Files:**

- Create: `scripts/structured_artifact_pipeline_gate.sh`
- Modify: `.github/workflows/ci.yml`
- Create: `.github/workflows/structured-artifact-release.yml`
- Create: `tests/product/test_checkpoint_b_structured_artifact_gate.py`
- Create: `tests/product/test_structured_artifact_release_workflow.py`
- Create: `tests/product/test_opencode_structured_artifact_e2e.py`
- Modify: `tests/architecture/test_agent_artifact_inventory.py`
- Consume without modifying: `tests/product/test_opencode_structured_binding_release.py` (owned by OpenCode Task 7)

**Interfaces:** the candidate revision contains protected `structured-artifact-release.yml`, the aggregate gate, and their tests before any protected run is requested. Its manual/tag entry accepts an explicit immutable `candidate_sha`, requires `github.workflow_sha == inputs.candidate_sha`, proves the protected dispatch ref is the authorized source of that workflow, and requires each checkout's `git rev-parse HEAD` to equal the same commit; a moving/mismatched ref is rejected. The producer job then reruns the exact release `aa bindings build`/install proof from that commit's OpenCode Task 7 promoted records without rerunning qualification or promotion. It uploads one immutable workflow artifact containing the exact external binding wheel, `binding-wheel-build-v1.json`, and an independently computed SHA-256 file, and a downstream job downloads/verifies all three. The two protected jobs independently resolve the same immutable deployment-input revision for manifest, network-target catalog, and command-secret bindings; producer exposes only their value-free source/resolved digests as authenticated job outputs, while downstream recomputes those digests from its own protected copy and rejects rotation/drift. Raw deployment inputs never enter the Actions artifact. `AA_OPENCODE_BINDINGS_WHEEL_SHA256` is frozen as the absolute path of that canonical `sha256sum` file—never the digest string. Its one line is exactly `lowercase_hex + "  " + wheel_basename + LF`; it never contains a producer absolute path. The SHA-file basename and build-record path are distinct siblings of the wheel in the artifact/output directory. The downstream job sets all four absolute paths and invokes the non-waivable gate plus the complete repository gate against the same `candidate_sha`. The gate consumes that explicit wheel/build record, the same committed promoted OpenCode identity and exact 33-row certified matrix, production `ProductRuntimePorts`, and all Feature/Kernel evidence; it provides exact 33/34/69/final-count, `2/31` toolchain, `10/23` network, and `2/31` command-secret assertions, all-Feature evidence, dynamic-root/exact-repair-set proofs, recovery cuts, and no topology/effect/validator drift. Ordinary PR CI runs only the deterministic fail-closed precursor and cannot claim Checkpoint S closure.

- [ ] **Step 1: Write the aggregate Product, release-workflow, and certification invariant RED.** In `test_structured_artifact_release_workflow.py`, require a manually dispatched/tag-scoped producer job bound to a reviewer-protected `structured-artifact-release` GitHub Environment with least-privilege permissions. Its required `candidate_sha` input is a full commit ID already reachable from the protected workflow ref; the workflow requires `github.workflow_sha == inputs.candidate_sha`, validates that `github.workflow_ref` names the authorized protected ref, and requires the producer and downstream checkouts' `git rev-parse HEAD` to equal exactly that commit before reading records or secrets. Both jobs independently fetch/read the same immutable protected deployment-input revision; producer publishes only the manifest, target-catalog, command-secret-binding source/resolved digests and revision identity as metadata-only job outputs, and downstream recomputes them from its own copy before trusting either the build record or wheel. Rotation, a mutable locator, a digest available only from the producer's record, or upload of raw catalogs/bindings into the three-file artifact fails. The producer then runs only the exact ordered command block below from that commit's Task 7 promoted transport/matrix records and frozen target/secret catalogs. Assert the workflow has no conformance `run`/`promote` step and cannot substitute newly generated certification bytes. Before invoking the build, the producer explicitly sets `AA_OPENCODE_BINDINGS_BUILD_RECORD` and `AA_OPENCODE_BINDINGS_WHEEL_SHA256` to distinct new absolute owner-only/no-follow sibling paths of the wheel inside its external output/artifact staging directory, outside the checkout/SUT and distinct from every input/install path. The one block orders build → wheel/record existence → relative-name SHA generation/check → no-deps install → release test. Require SHA content exactly `lowercase_hex + "  " + wheel_basename + LF`, with no absolute path, traversal, second line, or alternate filename. Upload a single immutable artifact preserving the common directory and containing only the wheel, `binding-wheel-build-v1.json`, and SHA file. Require a dependent job to download it into a fresh checkout of the same `candidate_sha` and validate the closed build-record schema before install/import: expected wheel filename must equal both the actual wheel basename and the SHA line basename; recorded wheel digest and manifest/resolved-input/ProductLock/promoted-certification/target-catalog/command-secret-binding digests must equal producer metadata outputs, the downstream's independent recomputation, and the checked-in promoted records. Run `sha256sum --check "$sha_basename"` from the downloaded artifact directory and independently hash the actual wheel; both must equal the recorded digest. Separately authenticate the Actions artifact's producer job, protected Environment, candidate revision, workflow run ID/attempt, immutable artifact ID and downstream dependency from workflow metadata—these are not fields invented in `binding-wheel-build-v1.json`. Then set explicit `AA_OPENCODE_BINDINGS_INSTALL_DIR`, `AA_OPENCODE_BINDINGS_WHEEL`, `AA_OPENCODE_BINDINGS_WHEEL_SHA256`, and the downloaded `AA_OPENCODE_BINDINGS_BUILD_RECORD` absolute paths and run the focused and complete gates. No pull-request secret exposure, cache, previous process environment, preinstalled wheel, mutable “latest”, unpublished workflow definition, newly generated/uncommitted certification, unauthenticated artifact provenance, or hand-authored satisfied record may close it. Assert `.github/workflows/ci.yml` invokes only the deterministic precursor and labels it non-closing, while Checkpoint S requires the protected downstream job for the same `candidate_sha` to be green.

  ```bash
  uv run aa bindings build --json \
    --manifest "$AA_OPENCODE_BINDINGS_MANIFEST" \
    --output-dir "$AA_OPENCODE_BINDINGS_OUTPUT_DIR" \
    --structured-output-certification packages/adapters/agent-runtime-opencode/agent_runtime_opencode/resources/structured-output-matrix-v1.json \
    --structured-network-target-catalog "$AA_STRUCTURED_NETWORK_TARGET_CATALOG" \
    --structured-command-secret-bindings "$AA_STRUCTURED_COMMAND_SECRET_BINDINGS" \
    --build-record "$AA_OPENCODE_BINDINGS_BUILD_RECORD"
  test -f "$AA_OPENCODE_BINDINGS_WHEEL"
  test -f "$AA_OPENCODE_BINDINGS_BUILD_RECORD"
  wheel_basename="${AA_OPENCODE_BINDINGS_WHEEL##*/}"
  sha_basename="${AA_OPENCODE_BINDINGS_WHEEL_SHA256##*/}"
  (
    cd "$AA_OPENCODE_BINDINGS_OUTPUT_DIR"
    sha256sum "$wheel_basename" > "$sha_basename"
    sha256sum --check "$sha_basename"
  )
  uv pip install --no-deps --target "$AA_OPENCODE_BINDINGS_INSTALL_DIR" "$AA_OPENCODE_BINDINGS_WHEEL"
  AA_OPENCODE_BINDINGS_INSTALL_DIR="$AA_OPENCODE_BINDINGS_INSTALL_DIR" \
    AA_OPENCODE_BINDINGS_WHEEL="$AA_OPENCODE_BINDINGS_WHEEL" \
    AA_OPENCODE_BINDINGS_WHEEL_SHA256="$AA_OPENCODE_BINDINGS_WHEEL_SHA256" \
    AA_OPENCODE_BINDINGS_BUILD_RECORD="$AA_OPENCODE_BINDINGS_BUILD_RECORD" \
    uv run pytest -q tests/product/test_opencode_structured_binding_release.py
  ```

  Compose the installed Product from the downloaded verified wheel and assert its lock authenticates all six Feature contribution sets; 33 contracts/34 occurrences/69 slots; final 34 typed/35 raw and 14/18/1 modes; initial raw owner totals `2/44/3/6/9/5`, final retained-raw totals `0/30/1/0/3/1`, and final typed totals `2/14/2/6/6/4`; exact toolchain `2 Execution/31 none`, network `10 input_conditioned/23 none`, and command-secret `2 input_conditioned/31 none` partitions with no unresolved/default row; every contract has `requires_structured_output=True`; and the wheel closes over exact promoted records, frozen catalogs, and full Feature-owned `AgentExecutionAuthority` values. Exact-set compare the four Product-owned semantic runner requirement values/digests and both Execution mappings, plus complete secret requirement aliases/purpose/delivery/encoding/target classes/value limits/digests, against ProductLock and reject any drift. The certified OpenCode matrix derives only the 33 toolchain/network/command-secret requirement-ID tuples and must match those IDs alongside contract/schema-digest, provider/model, and schema/result limits; it is not a second authority for full semantic requirement values. Require coverage repair's complete bound raw set and `actual_changed_set == files_modified ⊆ prekey_allowed_test_files`. Also assert no typed row lacks evidence; every semantic migration is authenticated; no raw row lacks a reason; 25 validators remain registered/zero bound; and six effect kinds plus semantic graph cardinality are unchanged. A missing/drifted record, wheel, digest, or workflow handoff is a hard failure, not a skip.

- [ ] **Step 2: Prove the real production Adapter -> Kernel seam in all artifact and execution-authority modes.** In `test_opencode_structured_artifact_e2e.py`, import `OpenCodeFakeServer` and `OpenCodeFakeState` from `tests.helpers.opencode_fake_server`, load the exact positive transport record and selected 33-row matrix promoted by OpenCode Task 7, then start it with `server_version` set to that record's exact server version. Instantiate the production `OpenCode` activity adapter, installed certified runtime binding, `ProductRuntimePorts`, `AttemptNodeFactory`, and `AssuranceAttemptKernel`—never `TestStructuredAgentActivityPort`, direct Kernel assembly, or a synthetic satisfied-capability/admission override. Parameterize `execution.execute` three times—one `offline_source + none` typed-only case, one `bound_sut + none` public-target case with zero injection, and one `bound_sut + bound_command_secrets` credentialed typed-only case—plus `quality.report` (raw-only), `generation.api.plan` (mixed/network-none), and `healing.coverage-repair` (mixed/network-none with existing exact-file bounded repairs). The Healing case snapshots the complete validated `brief.allowed_test_files` set, exposes original bytes read-only, changes exactly one and reports exactly that member, preserves modes, and rejects false-positive reports, unreported changes, new/deleted/out-of-set files, and mode drift before promotion. The two bound Execution cases use local synthetic SUT targets; the credentialed case uses one Product-issued alias and one synthetic authorization handle backing the complete `("AA_ADMIN_PASSWORD", "QA_ADMIN_PASSWORD")` tuple. Assert the promoted health identity, exact `format.schema`, terminal `info.structured`, v2 workspace boundary, request/binding/key/workspace digests, immutable input/raw post-image, command/network/injection transcripts, closure, canonical materialization, blob-backed finalization, frozen promotion, receipt, and graph-visible result. Run `offline_source + bound_command_secrets` as a pre-key negative and reject unbound network/secret paths.

  Re-run terminal/recovery after adapter and Product-port reconstruction and assert the same activity and terminal command are adopted with no second prompt, launch, or injection; the secret lifetime is rebuilt only from its durable authorized handle set plus the exact expected `SecretGenerationSetIdentity`. Add the asynchronous pending cut explicitly: arrange a tool call to race with the adapter's running/pending observation, require an authenticated durable `StructuredActivityToolProductionQuiesced` receipt before the observation can escape the Kernel, and prove a late call is queued under the same activity reference/broker binding and adopted exactly once after reconstruction. If the remote side acknowledges quiescence but dies before the Kernel can durably prove or reattach that state, return indeterminate—never ordinary pending—and perform no reprompt, duplicate command, or duplicate injection. Rotation or an unprovable source blocks replay before blob/event work. Test direct/base64/hex/URL-safe canaries split across stream chunks, and prove another Attempt's secret is rejected without leaking either value. Add a delayed/background shell writer after raw capture: finalizer still sees immutable bytes, seal/prepare rejects staging drift, and no promotion/effect occurs. Add a negative row in which the fixture advertises any other version and prove failure occurs before session creation. The local scripted server proves the full software seam against the already promoted identity; it does not establish that identity's real-world qualification, and the separate Task 7 real-version evidence remains a mandatory release prerequisite.

- [ ] **Step 3: Run the post-certification aggregate RED.**

```bash
uv run pytest -q tests/product/test_checkpoint_b_structured_artifact_gate.py tests/product/test_structured_artifact_release_workflow.py tests/product/test_opencode_structured_binding_release.py tests/product/test_opencode_structured_artifact_e2e.py tests/architecture/test_agent_artifact_inventory.py packages/capabilities/assurance-healing/tests/test_artifact_pipeline.py
```

Expected: CI has no Structured Artifact Pipeline gate and the aggregate closure test cannot find its script invocation.

- [ ] **Step 4: Implement the protected release handoff and fail-fast aggregate gate.** Add `structured-artifact-release.yml` with manual/tag scope, a required full-length `candidate_sha`, one immutable deployment-input revision, least-privilege permissions, reviewer-protected `structured-artifact-release` Environment, separate producer/downstream jobs, and explicit artifact dependency; never expose release secrets to pull-request jobs. Both jobs check out `candidate_sha` directly and fail unless the protected dispatch ref/workflow source and both resulting `HEAD` values identify that same immutable commit. Each job independently resolves the pinned deployment input; producer's metadata-only digest outputs and downstream's recomputed digests must match, while raw inputs remain private and outside the uploaded artifact. The producer verifies the checked-in Task 7 records there, runs only the full Step 1 build/install/release-test command block without removing or substituting a flag, writes/verifies the relative-basename SHA file, then uploads wheel, build record, and SHA file as one immutable artifact. The workflow contains no real-version qualification/promotion command. The downstream job preserves/downloads the three siblings into one fresh artifact directory, performs the schema/identity/digest checks from Step 1 before install, and exports only its fresh absolute install directory, wheel path, SHA-file path, and build-record path. `structured_artifact_pipeline_gate.sh` requires all four inputs, treats `AA_OPENCODE_BINDINGS_WHEEL_SHA256` only as an absolute regular one-line SHA-file path, verifies its basename from the artifact directory (never a producer path), reruns `sha256sum --check` from that directory, revalidates build-record schema/identity/digest against the checked-in promoted records and independently resolved deployment inputs, and rejects missing/nonabsolute/out-of-tree/wrong-digest/non-wheel/mismatched-record values before Boot. It always runs `tests/product/test_opencode_structured_binding_release.py` before the remaining aggregate suites. Compose Product through its existing provider/declaration and verified wheel path; do not create a second manifest or hand-edit a digest. The script then runs Artifact/Kernel exit, OpenCode matrix/staging/network/secret/E2E, Boot resolution, exact catalog/authority partitions, all six Feature matrices, dynamic Intake/Generation closure, Healing exact-repair closure, recovery cuts, and wording guard. The downstream repeats the complete repository gate after this script. Ordinary `ci.yml` runs a separately named deterministic precursor that proves code closure but cannot set a Checkpoint S success status. Use `set -euo pipefail`, explicit paths, and exact inventory/version/toolchain checks; never retry, skip, xfail, count-substitute, or fall back to a previously installed wheel/build record.

- [ ] **Step 5: Run the local deterministic precursor and complete non-closing gate.** Run the workflow-structure/fail-closed tests, the fake-server production seam, and the complete repository gate before committing. The focused protected script must be tested here for rejection of absent, relative, stale, or mismatched wheel/record/SHA inputs, but a locally built or Stage 6 wheel must not turn this step into Checkpoint S success.

```bash
uv run pytest -q tests/product/test_checkpoint_b_structured_artifact_gate.py tests/product/test_structured_artifact_release_workflow.py tests/product/test_opencode_structured_artifact_e2e.py tests/architecture/test_agent_artifact_inventory.py packages/capabilities/assurance-healing/tests/test_artifact_pipeline.py
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest
bash scripts/assurance_product_wheel_smoke_test.sh
bash scripts/graph_engine_smoke_test.sh
bash scripts/assurance_capability_wheel_smoke_test.sh
```

Expected: all deterministic checks exit `0`, and the focused gate demonstrably refuses to report Checkpoint S closure without the protected workflow artifact and authenticated Actions provenance.

- [ ] **Step 6: Commit and publish the exact candidate revision before requesting protected execution.** This is the task's only implementation commit. Record its full `candidate_sha`, publish it through the repository's protected PR/branch or immutable protected-tag process, and verify the remote ref resolves to exactly that SHA and contains this workflow. Do not trigger the protected workflow from an uncommitted worktree, an unpushed commit, a moving `latest` ref, or a prior revision.

```bash
git add scripts/structured_artifact_pipeline_gate.sh .github/workflows/ci.yml .github/workflows/structured-artifact-release.yml tests/product/test_checkpoint_b_structured_artifact_gate.py tests/product/test_structured_artifact_release_workflow.py tests/product/test_opencode_structured_artifact_e2e.py tests/architecture/test_agent_artifact_inventory.py
git commit -m "ci: gate Checkpoint S artifact closure"
candidate_sha="$(git rev-parse HEAD)"
test "$(git rev-parse "$candidate_sha^{commit}")" = "$candidate_sha"
# Publish this exact commit through the repository's authorized protected-ref process.
# Verify the resulting remote protected ref resolves to $candidate_sha before dispatch.
```

Expected: the protected ref exposes the workflow and resolves to the recorded full SHA; the working tree contains no post-commit Task 12 change.

- [ ] **Step 7: Trigger and wait for the protected producer/downstream run of that exact revision.** Dispatch the workflow from the protected ref with `candidate_sha` set to Step 6's full SHA. The producer creates the only eligible wheel/build-record/SHA bundle. In a fresh checkout of the same SHA, the downstream authenticates workflow/artifact provenance, verifies the bundle, sets the four explicit paths, runs the focused gate below, and then repeats every complete repository command from Step 5. Checkpoint S closes only when that exact downstream job is green. Do not amend, merge another change into, or create a follow-up commit on the candidate and still attribute the old run to it; any source change requires a new candidate SHA and a complete rerun from Step 5.

```bash
test -n "${AA_OPENCODE_BINDINGS_INSTALL_DIR:?}"
test -n "${AA_OPENCODE_BINDINGS_WHEEL:?}"
test -n "${AA_OPENCODE_BINDINGS_WHEEL_SHA256:?}"
test -n "${AA_OPENCODE_BINDINGS_BUILD_RECORD:?}"
bash scripts/structured_artifact_pipeline_gate.sh
```

Expected: the protected downstream for the recorded `candidate_sha` exits `0`; the checked OpenCode version is proven by the committed qualification records, all 33 contracts are revalidated from the verified installed wheel, every typed row has linked permission/recovery/parity evidence, and the complete gate is green on the same revision. There is no later Task 12 commit.

## Checkpoint S Acceptance Gate

Checkpoint S is closed only when all of the following are true in the same revision:

- The machine-readable inventory equals the exact 33-ID catalog and exact 34-occurrence tuple above, proves exactly 32 contracts have at least one structured candidate with Quality report as the sole exception, expands to 69 owned slots, contains no undecided disposition, and reports the reviewed final 34 typed/35 raw and 14/18/1 classification counts, initial raw owner totals `2/44/3/6/9/5`, final retained-raw totals `0/30/1/0/3/1`, and final typed totals `2/14/2/6/6/4`.
- The Product lock authenticates every Feature-owned result model/schema, `ArtifactContract`, `ArtifactSlot`, document model/schema, projector and Kernel serializer; Boot rejects missing, extra, project-loaded, cross-owner and drifted contributions.
- All 33 contracts set `requires_structured_output=True`; Product resolves that against the qualified `opencode_structured_output` capability and an exact pinned OpenCode version. An unproven version, including the recorded 1.18.4 historical negative and v1.18.26 `message-roundtrip-red` fixture, fails Agent dispatch/cutover and Checkpoint S while direct/non-Agent Product roots remain bootable.
- The typed-only/raw-only/mixed tracer and every catalog row pass their linked skill, result, projector, finalizer, permission, recovery and parity tests; uncompleted candidates remain final raw and cannot affect runtime authority.
- Every `semantic_migration` row resolves one Feature-owned `ArtifactSemanticMigrationRecord` with old/new identities, rationale, approved golden digests, downstream compatibility tests, and a digest authenticated by the Product build; every `exact_bytes` row has no migration record.
- Intake dynamic case paths and repair post-images, all four Generation codegen roots/two fix allowed-path sets, and Healing coverage-repair's repeatable exact-file set are closed before resource authorization and Attempt-key derivation and cannot be expanded later. Coverage repair uses only validated `brief.allowed_test_files`, requires immutable existing baselines and preserve mode, exposes original bytes read-only, covers the complete bound set in its raw selector, and proves `actual_changed_set == CoverageRepairAgentResultV1.files_modified ⊆ prekey_allowed_test_files` from the immutable raw post-image/sealed partition.
- The Kernel persists/reopens the immutable input snapshot, prepared/result snapshots, raw-write closure plus durable command transcript, immutable raw post-image, complete materialization manifest/receipt, sealed partition, and frozen promotion set; it survives all seven required cuts plus workspace-begin/command/closure/raw-capture/frozen-source subcuts with no duplicate OpenCode dispatch or command execution, and verifies typed/raw immutable refs again at seal/promotion.
- Typed and raw permissions are disjoint, typed deny holes dominate broad raw roots, internal claims cannot promote, and neither skills nor finalizers raw-write/read-back/reparse typed artifacts.
- No LangGraph topology, public resolution, validator binding, effect kind/order, or external-provider transaction claim changes; the production baseline remains 25 validators registered/zero bound and six effect kinds.
- Research, both target designs, all three Structured child plans and the migration master use the frozen interfaces, record T5a as completed history, and explicitly run `scripts/structured_artifact_pipeline_gate.sh` before Product T5b.
- Checkpoint S is closed only by a green downstream job in protected `.github/workflows/structured-artifact-release.yml`: its producer consumes the exact Task 7 records already committed at the protected source revision, reruns only build/install proof, and uploads one immutable artifact containing the exact wheel, `binding-wheel-build-v1.json`, and independent SHA file. The fresh downstream checks out the same revision, separately authenticates Actions producer/source/run/artifact provenance, validates the record's manifest/input/ProductLock/certification/catalog/binding/wheel identities against those checked-in records, proves the two SHA checks against actual bytes, and only then sets all four explicit gate inputs. Rerunning qualification/promotion in this workflow is forbidden. Ordinary PR CI's deterministic precursor is necessary but cannot claim closure.
- The focused script, full Ruff/format/Pyright/import-linter/pytest gate and all three installed-wheel smoke scripts pass on the final revision.

## Self-Review Checklist

- [ ] Header, Global Constraints, Interfaces, exact files, RED/GREEN commands, expected failures, exact staging and one commit are present for every task.
- [ ] The catalog contains all 33 exact IDs, the mapping contains all 34 exact occurrences, and only case-design has multiplicity two.
- [ ] The initial inventory is fail-closed raw; every row has a final raw or typed disposition, and a typed flip is atomic with all named evidence.
- [ ] `ArtifactContract`, `ArtifactSlot`, `requires_structured_output`, `opencode_structured_output`, and `ResolvedStructuredAgentExecutor` are the only corresponding abstractions/names.
- [ ] Every Feature wave explicitly covers result models, skills, projectors, finalizers, permissions, recovery and parity; Intake and Generation dynamic roots have dedicated tests.
- [ ] Healing coverage repair remains mixed: its typed receipt is Kernel-materialized and its repeatable raw exact-file set is baseline-backed, bounded-repair-only, preserve-mode, and subset-checked at result and seal.
- [ ] Every semantic parity change is represented by an authenticated Feature-owned migration record in contract/build closure; no task-time waiver can authorize it.
- [ ] No task introduces project-loadable code/schema capability, new graph topology, a second phase bundle, private OpenCode fork, new effect, cryptographic attestation, or global ACID claim.
- [ ] Task dependencies follow the authoritative prerequisite sequence: completed Foundation/Semantic Attempt/Feature/Product T5a history; OpenCode Task 0; Checkpoint S0 green; Task 10 normative sync as the first post-S0 retrofit commit; Task 1 inventory; Artifact Tasks 1–4; Task 2 raw/authority baseline; Artifact Task 5 consumes that table; Artifact Tasks 6–8 before OpenCode Tasks 2–4/6 and Artifact Tasks 9–10 afterward; Tasks 3–9 Feature waves; Task 11 pre-certification production runtime; external OpenCode Tasks 5/7 plus binding build/install; Task 10 wording-gate rerun without a second commit; Task 12 post-certification Product/CI release gate.
