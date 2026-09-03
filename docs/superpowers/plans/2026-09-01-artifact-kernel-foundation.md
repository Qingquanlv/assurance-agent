# Artifact Foundation and Structured Attempt Kernel Implementation Plan

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

**Goal:** Add the provider-neutral artifact contracts, authenticated registries, deterministic JSON/YAML materializer, durable structured-result boundaries, and recoverable `AssuranceAttemptKernel` phases required to turn one structured Agent result into sealed typed/raw workspace bytes without adding a LangGraph node.

**Architecture:** `graph-engine` owns one deep `graph_engine.artifacts` module and a closed direct-versus-structured executor variant. A structured Agent Attempt persists validated `PreparedT` and `AgentResultT` snapshots, prepares and anchors one immutable materialization manifest, installs its typed bytes idempotently, gives the resulting `MaterializationReceipt` to a read-only deterministic finalizer, and then rejoins the existing seal → validators → durable prepare → promotion → effects → terminal receipt transaction tail. Feature wheels contribute authenticated document models, path resolvers, and projectors; Product selects the runtime binding; the OpenCode adapter implements only `StructuredAgentActivityPort`.

**Tech Stack:** Python 3.11, Pydantic v2, canonical JSON and a checked YAML 1.2-safe subset, SHA-256 content addressing, existing authenticated wheel composition, existing `TaskWorkspaceStore` dirfd/no-symlink primitives, append-only fenced Attempt journal, pytest, Ruff, Pyright, import-linter.

**Spec:** `docs/superpowers/specs/2026-09-01-structured-artifact-pipeline-design.md`

## Global Constraints

- Execute in the clean migration worktree created with `superpowers:using-git-worktrees`; do not stage or overwrite the current user-owned OpenCode/workspace changes.
- This entire child plan is blocked until OpenCode Task 0 closes Checkpoint S0 green for one exact official release. A red S0 authorizes no Artifact/Kernel task or speculative production seam; its non-promotable eligibility report is not an Adapter capability or Checkpoint S certification.
- This plan is the Artifact Foundation + Kernel child plan only. Adapter version/capability gating, the three-mode Capability tracer, all 33 Feature contract migrations, documentation correction, and final Checkpoint S catalog closure are separate child plans.
- Foundation Tasks 1–7 and Semantic Attempt Kernel Tasks 1–2 must be complete before Task 1. Semantic Attempt Kernel Task 8 must have created the base Kernel, Attempt events, durable Attempt journal, workspace transaction, and fence ports before Task 6.
- Task 5 normatively replaces the opaque composite/provider-schema portion of Semantic Attempt Kernel Task 3. OpenCode Structured Output Gate Task 1 must already have created the sole provider-neutral `graph_engine.attempts.structured_activity` seam; this plan imports `StructuredAgentActivityPort.dispatch_or_adopt_activity(...)` / `observe_activity(...)` and does not redeclare it. Use `ResolvedDirectExecutor`, `ResolvedStructuredAgentExecutor`, and generic requirement `requires_structured_output`; do not retain a parallel `CompositeAttemptExecutor` authority or `provider_schema` flag. The concrete `opencode_structured_output` token is mapped only by the OpenCode adapter/Product binding child plan and does not enter generic `agent-runtime-contracts` production code.
- Core `graph-engine` must not import `agent_runtime_contracts`, a concrete runtime adapter, any Capability package, or Product code. The adapter package constructs core resolved executor values.
- `AssuranceAttemptKernel.execute_or_recover(...)` remains the only transaction entrypoint. No structured-result, materialization, or finalization LangGraph node/edge/phase alias is introduced.
- All Agent results are untrusted until the Kernel validates the installed `AgentResultT`. Adapter validation is defense in depth and never authorizes a file.
- Every path-affecting value is closed from validated `InputT` before the Attempt key and resource authorization. Every prepare dependency, including prior file-derived business evidence, is likewise hydrated into canonical validated `InputT`; prepare has no workspace/context read seam. If a Feature needs a read-only preflight, its authenticated snapshot must already be a validated field of `InputT`; the Kernel and the baseline reader cannot add paths or content. All 33 contracts in this migration resolve paths and prepare inputs from `InputT` alone. `PreparedT`, `AgentResultT`, project configuration, and the model cannot expand path authority.
- For every Attempt, `AttemptNodeFactory` validates `InputT`, resolves total and explicit non-artifact `ResourceClaims`, binds `ResolvedArtifactContract`, resolves any provider-neutral structured-toolchain and SUT-network requirements to immutable Product-owned values, and constructs one immutable `BoundAttemptDispatch` before deriving `AttemptKey`. The key binds the input, resource-claim, toolchain, network-access, selected command-secret-set, authorized-secret-handle-union, and bound-artifact digests. The Kernel receives that exact dispatch value and may authenticate it but may never resolve paths, claims, target IDs, DNS, network policy, aliases, or handle selection again.
- Every promotable path has exactly one authority: Kernel-managed typed, Agent-managed raw, or neither. Attempt-internal input/scratch paths are explicit resource claims and never promotable.
- Typed targets are removed from every effective OpenCode raw-write permission, including deny holes beneath broad raw roots. Raw attempts against typed targets leave staging unchanged and remain diagnosable.
- `ArtifactContract`, `ArtifactSlot`, `MaterializationEntryReceipt`, and `MaterializationReceipt` are the normative artifact type names. Do not add a second artifact-spec or receipt hierarchy.
- Canonical schema digests are lowercase SHA-256 of the exact immutable registry schema document encoded with `graph_engine.canonical.canonical_json_bytes`. Agent-result and artifact-document schema digests are separate authorities.
- Serializer identifiers are exactly `canonical-json-v1` and `canonical-yaml-v1`; do not add a separately mutable numeric serializer version.
- Prepare handlers, projectors, path resolvers, and post-materialization finalizers are deterministic installed code. Structured prepare is exactly `prepare(InputT)` and finalization is exactly `finalize(StructuredFinalizeInput)`; neither receives `AttemptExecutionContext`. They cannot read ambient project/workspace files, time, randomness, process identity, provider state, network state, secrets, admission/journal/fence/effect/activity ports, or a mutable write handle.
- The finalizer cannot write staging or alter materialized paths/bytes. It may consume validated typed values, typed document references, `MaterializationReceipt`, and an explicitly read-only raw-artifact view.
- Invalid documents create zero typed files. No partial materialization reaches seal, durable prepare, promotion, effect settlement, or graph publication.
- Prepared/result/manifest/receipt publication and every typed install are fenced. A stale runner may observe an activity but cannot persist, install, finalize, seal, prepare, promote, settle, or publish.
- Direct non-Agent Attempts retain their existing executor path and skip every structured-artifact phase. Existing six-effect ordering and the closed `AttemptResolution` variants do not change.
- No production validator is newly bound. Existing semantic/cross-reference validators, tests, Eval, sealing, promotion, effects, and system-interrupt behavior remain active.
- No project `.aa` or SUT source may register or replace artifact models, schemas, path resolvers, projectors, codecs, serializers, handlers, or validators.
- Do not introduce a custom Schema-writer plugin, private OpenCode fork, in-toto/DSSE/signing feature, or global ACID claim.
- Run Python commands through `uv run`. Each task follows RED → minimal implementation → focused GREEN → static checks → exact-path commit; never use `git add .`, `git add -A`, or broad globs.

## Integration Order

Foundation Tasks 1–10, Semantic Attempt Tasks 1–10, Feature Tasks 1–9, Product Tasks 1–4, and Product T5a are completed prerequisites at the continuation baseline. They are not steps in this child plan and must not be replayed. The remaining Artifact/Kernel delta is:

```text
OpenCode Structured Output Gate Task 0
  → Checkpoint S0 green
  → Agent Artifact Contract Migration Task 10 documentation/test sync
  → Agent Artifact Contract Migration Task 1 inventory
  → OpenCode Structured Output Gate Task 1
  → this plan Tasks 1–4
  → Agent Artifact Contract Migration Task 2 raw baseline + 33-row execution authority
  → this plan Task 5
  → this plan Tasks 6–8
  → OpenCode Structured Output Gate Tasks 2–4 and Task 6
  → this plan Tasks 9–10 extend the existing Kernel/node seam
```

Semantic Attempt Task 3 is completed history; its old `provider_schema`/opaque-executor wording is not replayed. Task 5 below migrates the existing implementation in place to its provider-neutral replacement, while the OpenCode child supplies the concrete activity adapter and qualifying-version gate. The existing `GraphBuildManifest` shape remains unchanged: artifact closure is authenticated through ProductLock registry digests and each `attempt_contract_digest`, not through a compiled graph or a second manifest.

## Target File Map

```text
packages/framework/graph-engine/graph_engine/artifacts/
├── __init__.py       # one public artifact-module interface
├── contracts.py      # ArtifactContract / ArtifactSlot and bound path authority
├── registry.py       # installed model/resolver/projector/serializer closure
├── baseline.py       # immutable workspace-baseline identity and mutation checks
├── codecs.py         # canonical-json-v1 / canonical-yaml-v1
├── materializer.py   # immutable manifest preparation + idempotent typed install
├── receipts.py       # MaterializationEntryReceipt / MaterializationReceipt
└── resources/
    ├── __init__.py
    └── canonical-serialization-v1.json  # shipped normative byte corpus

packages/framework/graph-engine/graph_engine/persistence/
└── attempt_artifacts.py  # Attempt-private content-addressed snapshots/blobs
```

The planned `graph_engine.attempts` and `graph_engine.boot` files remain where the Foundation/Semantic Attempt plans place them. This plan extends those files; it does not create an overlapping Kernel, journal, resolver, or Boot abstraction.

---

### Task 1: Define immutable artifact contracts and close path authority

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/artifacts/__init__.py`
- Create: `packages/framework/graph-engine/graph_engine/artifacts/contracts.py`
- Create: `packages/framework/graph-engine/graph_engine/artifacts/baseline.py`
- Create: `packages/framework/graph-engine/tests/artifacts/test_contracts.py`
- Create: `packages/framework/graph-engine/tests/artifacts/test_baseline.py`

**Interfaces:**

- Consumes: `graph_engine.plugin_api.FrozenModel`, `graph_engine.frozen_json.FrozenJSONValue`, `ResourceClaims`, and the existing canonical digest helpers.
- Produces: immutable `ArtifactSlot`, `ArtifactContract`, `FixedModePolicy`, `PreserveBaselineModePolicy`, `ArtifactBaselineEntry`, `ArtifactBaselineSnapshot`, `BoundArtifactSlot`, `BoundArtifactContract`, `ArtifactProjectionInput`, `ArtifactClassification`, `baseline_from_workspace_binding(...)`, and `bind_artifact_paths(...)`.

- [ ] **Step 1: Write the RED constructor and classification tests.**

Add tests that construct one typed, one raw, and one mixed contract and assert classification is derived, never manually supplied:

```python
def test_artifact_classification_is_derived_from_slots() -> None:
    typed = ArtifactContract.build(contract_id="artifact.typed.v1", slots=(typed_slot("report"),))
    raw = ArtifactContract.build(contract_id="artifact.raw.v1", slots=(raw_slot("markdown"),))
    mixed = ArtifactContract.build(
        contract_id="artifact.mixed.v1",
        slots=(typed_slot("manifest"), raw_slot("source")),
    )

    assert typed.classification == "typed-artifact-only"
    assert raw.classification == "raw-artifact-only"
    assert mixed.classification == "mixed-artifact"
    assert typed.digest == typed.canonical_digest()
```

Assert duplicate `slot_id`, unsorted slots, empty contracts, a hand-entered classification, and a supplied digest that disagrees with canonical projection are rejected.

- [ ] **Step 2: Run the focused test and confirm the missing-module failure.**

Run:

```bash
uv run pytest -q packages/framework/graph-engine/tests/artifacts/test_contracts.py
```

Expected: collection fails with `ModuleNotFoundError: graph_engine.artifacts`.

- [ ] **Step 3: Implement the exact `ArtifactSlot` field contract.**

Use the cross-plan field names and literals exactly; do not create a second slot vocabulary:

```python
ArtifactAuthority = Literal[
    "agent_structured_value",
    "deterministic_derived_value",
    "agent_raw_bytes",
]
ArtifactCardinality = Literal["one", "repeatable"]
ArtifactTargetKind = Literal["exact_file", "tree_root"]
ArtifactMutation = Literal["create", "replace", "bounded_repair"]
ArtifactRepairStrategy = Literal["complete_post_image", "agent_raw_whole_file"]
ArtifactClassification = Literal[
    "typed-artifact-only",
    "raw-artifact-only",
    "mixed-artifact",
]
ParityMode = Literal["exact_bytes", "semantic_migration"]


class FixedModePolicy(FrozenModel):
    kind: Literal["fixed"] = "fixed"
    mode: int


class PreserveBaselineModePolicy(FrozenModel):
    kind: Literal["preserve_baseline"] = "preserve_baseline"


ArtifactMutationPhase = Literal["initial", "repeat", "repair"]


class ArtifactMutationRule(FrozenModel):
    semantic_occurrence_id: str
    phase: ArtifactMutationPhase
    mutation: ArtifactMutation
    repair_strategy: ArtifactRepairStrategy | None


class ArtifactMutationPolicy(FrozenModel):
    rules: tuple[ArtifactMutationRule, ...]


class ArtifactSemanticMigrationRecord(FrozenModel):
    record_id: str
    owner_id: str
    old_representation_id: str
    new_representation_id: str
    rationale: str
    approved_golden_digests: tuple[str, ...]
    downstream_compatibility_test_ids: tuple[str, ...]
    record_digest: str


class ArtifactSlot(FrozenModel):
    slot_id: str
    authority: ArtifactAuthority
    cardinality: ArtifactCardinality
    target_kind: ArtifactTargetKind
    path_resolver_id: str
    mutation_policy: ArtifactMutationPolicy
    mode_policy: FixedModePolicy | PreserveBaselineModePolicy
    document_model_id: str | None
    artifact_document_schema_id: str | None
    artifact_document_schema_digest: str | None
    media_codec: Literal["json", "yaml"] | None
    serializer_id: str | None
    selector_or_projector_id: str | None
    raw_member_selector_id: str | None
    max_items: int
    max_bytes: int
    parity_mode: ParityMode | None
    semantic_migration_record_id: str | None
    semantic_validator_ids: tuple[str, ...]


class ArtifactContract(FrozenModel):
    contract_id: str
    slots: tuple[ArtifactSlot, ...]
    digest: str


class ArtifactProjectionInput(FrozenModel):
    task_input: FrozenJSONValue
    prepared: FrozenJSONValue
    agent_result: FrozenJSONValue
    raw_postimage: RawPostImageProjection


class RawMemberDeclaration(FrozenModel):
    slot_id: str
    logical_path: str


class RawMemberSelectionInput(FrozenModel):
    task_input: FrozenJSONValue
    prepared: FrozenJSONValue
    agent_result: FrozenJSONValue


class RawPostImageEntryMetadata(FrozenModel):
    slot_id: str
    logical_path: str
    content_digest: str
    byte_size: int
    mode: int


class RawPostImageProjection(FrozenModel):
    entries: tuple[RawPostImageEntryMetadata, ...]
    manifest_digest: str
```

Enforce these invariants in model validators:

- every slot has exactly one installed `path_resolver_id`; a fixed logical path uses an authenticated framework/Feature fixed resolver rather than a second path field;
- cardinality `one` requires `max_items == 1`; `repeatable` requires `max_items >= 1`;
- every typed slot has `target_kind="exact_file"`; raw fixed files and the two API/E2E codegen-fix repeatable allowed-path sets also use `exact_file`, while only four reviewed primary Generation codegen slots use `tree_root` (API, E2E, Fuzz, Performance). A tree root requires raw authority, repeatable cardinality, explicit item/byte scan bounds, and a canonical root path. A repeatable exact-file resolver returns only its finite pre-key authenticated member set. Target kind is never inferred from `cardinality`, a `/**` suffix, or path punctuation;
- `agent_raw_bytes` forbids all document/schema/codec/serializer/selector-projector/parity fields and requires one installed `raw_member_selector_id`; the selector returns only sorted unique `(slot_id, logical_path)` declarations from validated `InputT`/`PreparedT`/`AgentResultT`, never bytes or trusted digests;
- both typed authorities require every document/schema/codec/serializer/selector-projector/parity field and forbid `raw_member_selector_id`;
- `deterministic_derived_value` and `agent_structured_value` both use an authenticated `selector_or_projector_id`; a direct selector is registered as a projector rather than encoded as an ambient callable;
- `canonical-json-v1` pairs only with codec `json`; `canonical-yaml-v1` pairs only with `yaml`;
- `FixedModePolicy.mode` requires a regular-file permission in `0o400..0o777`; `PreserveBaselineModePolicy` has no caller-supplied mode;
- mutation rules are a sorted, unique, nonempty, exact map over `(semantic_occurrence_id, phase)` with no fallback/default/wildcard. Every slot in one `ArtifactContract` must expose the same rule-key set; `ArtifactContract.occurrence_mutation_phases` is the sole derived phase-domain authority. During Checkpoint S, the already-existing production StateGraphs emit their live selector ownership/provenance catalog, and Boot requires live graph mapping, Artifact domain, and the frozen exact 34-occurrence fixture to be set-equal. The low-level binder receives the authenticated selector result separately from frozen business `InputT`, rejects a missing/ambiguous/unowned phase, and `BoundArtifactSlot` stores the selected phase plus only the concrete mutation/repair strategy;
- `create` requires no repair strategy and forbids `PreserveBaselineModePolicy`; `replace` requires no repair strategy; typed `bounded_repair` requires `complete_post_image`; raw `bounded_repair` requires the explicit coexistence strategy `agent_raw_whole_file`, which may be replaced by `complete_post_image` only in the same atomic commit that moves the slot to typed authority;
- typed slots require a parity mode and raw slots require `parity_mode is None`; `exact_bytes` forbids `semantic_migration_record_id`, while `semantic_migration` requires one installed Feature-owned record;
- an `ArtifactSemanticMigrationRecord` requires nonblank distinct old/new representation identities, a nonblank rationale, sorted unique lowercase SHA-256 approved golden digests, sorted unique downstream compatibility-test IDs, owner closure, and a recomputed `record_digest`;
- validator IDs are sorted unique, sizes are positive and bounded, IDs are canonical nonblank tokens, and every digest is lowercase SHA-256.

`ArtifactMutationPhase` is graph-owned dispatch control, not a Feature business-input field. Public root adapters, models, prompts, providers, project files, prepare handlers, and finalizers cannot supply or revise it. `AttemptNodeFactory.attempt(...)` receives an installed `mutation_phase(state) -> ArtifactMutationPhase` selector beside its existing `activation(state) -> BusinessActivation` and input selectors; the Product/Feature graph source and build digest authenticate both selectors. Activation and phase each execute exactly once against the same anchored checkpoint state and are frozen together before input/path/resource binding; Kernel/recovery never reevaluates them. `initial` means the first distinct business activation of that semantic occurrence in the invocation even when its first legacy call happens at round 1; a later distinct round/recheck/current-trigger activation uses `repeat`; an explicitly modeled bounded repair call uses `repair`. Flow-local inbox/cursor state persists the choice before Attempt dispatch, so a late distinct trigger is `repeat` and exact trigger replay retains its original BusinessActivation/phase pair. The existing BusinessActivation enters `AttemptKey` directly; the selected phase enters `BoundArtifactContract`, `BoundAttemptDispatch`, and the key through their authenticated digests before any journal/workspace/activity call. `semantic_occurrence_id` is exactly `semantic_node_id`, not a second runtime identity. The Capability migration plan freezes the exact 34-occurrence phase matrix and tests 17 reactivatable occurrences: 16 `initial + repeat` occurrences plus repair-only `intake.case-design.repair`; adding a phase is an authenticated contract/graph revision, not a runtime fallback.

`ArtifactContract.build(...)` sorts nothing silently: require stable `slot_id` order, compute `digest` from the exact data-only slot projection, and authenticate it again during model validation. Classification remains a derived property and is not stored in the canonical contract.

- [ ] **Step 4: Add bound-path and authority tests.**

Use a fake installed resolver and validate all three output classes before resource authorization:

```python
def test_bound_contract_subtracts_typed_deny_holes_from_raw_root() -> None:
    bound = bind_artifact_paths(
        contract=mixed_contract_with_raw_root("generated"),
        validated_input={"change_id": "chg-1"},
        semantic_occurrence_id="generation.api.plan",
        mutation_phase="initial",
        path_resolvers={"paths.manifest": lambda _: ("generated/manifest.json",)},
        internal_paths=("generated/.scratch",),
        claimed_raw_roots=("generated",),
    )

    assert bound.typed_paths == ("generated/manifest.json",)
    assert bound.raw_paths == ()
    assert bound.raw_roots == ("generated",)
    assert bound.raw_deny_paths == ("generated/.scratch", "generated/manifest.json")
    assert bound.internal_paths == ("generated/.scratch",)
```

Add negative cases for traversal, absolute/Windows paths, duplicate concrete targets within one bound contract, typed/raw same-path ownership, typed/tree-root, exact-file/root confusion, an unreviewed fifth tree root, a resolver exceeding `max_items`, nondeterministic repeated resolver results, a raw slot missing/using an unowned raw-member selector, a typed slot carrying one, an unsorted/duplicate/missing mutation rule, a missing/unknown/unowned graph phase, an occurrence/phase lookup miss, a project/business input attempting to inject a phase field, parent/child intersection that cannot be represented by a raw deny hole, and a resolver that reads anything except the frozen validated input. `BoundArtifactContract` carries the authenticated semantic occurrence ID and selected mutation phase, frozen `typed_paths`, `raw_paths`, `raw_roots`, raw deny paths, internal paths, slot bindings with selected concrete mutation/repair/mode policies, and `bound_contract_digest`; it does not carry mutable project-baseline facts. `PreparedT` and `AgentResultT` are absent from `bind_artifact_paths(...)`. Cross-contract reuse of the same conventional path is allowed because contracts execute in distinct Attempt/workspace scopes; collision rejection is per bound Attempt.

`bind_artifact_paths(...)` is a pure lower-level constructor, not the graph-facing seam. Artifact Task 5 wraps it in `bind_attempt_dispatch(...)`, proves that every bound typed/raw/internal path is covered by the final input-resolved claims, and freezes the result before key derivation. Kernel code must not call either binder.

Define `ArtifactBaselineEntry` as the canonical `(slot_id, logical_path, exists, before_sha256, before_size, before_mode, entry_digest)` fact and `ArtifactBaselineSnapshot` as the sorted entry/root-tree set plus the authenticated `TaskWorkspaceIdentity.identity_digest`, applied `WorkspaceScanBudget` digest, and its own digest. Presence requires a lowercase SHA-256, nonnegative byte size, and regular-file mode; absence forbids all three. `create` requires absence for every eventual concrete output, `replace` requires presence, and typed `bounded_repair` requires the complete authenticated target set to exist and later receives one complete post-image per path. `preserve_baseline` requires a present entry and resolves to its `before_mode`; fixed mode uses only the contract value but still carries existence/digest/size facts for mutation enforcement. Broad raw roots retain a bounded sorted baseline subtree so the sealed raw write set can enforce mutation per concrete output. Missing/extra entries, size/digest disagreement, symlinks, non-regular files, hardlinks, over-limit trees, and identity drift fail closed.

These baseline facts are deliberately not path authority and do not alter `AttemptKey`: `TaskWorkspaceStore.begin(...)` captures them durably under the already stable Attempt/workspace identity after resource authorization and before prepare or Agent dispatch. Recovery reopens and authenticates that same workspace identity instead of rereading a new baseline. Task 8 owns the descriptor-safe concrete capture; Task 7 consumes only the immutable snapshot value, so the pure materializer never touches the filesystem.

- [ ] **Step 5: Implement binding and rerun the contract tests.**

Run:

```bash
uv run pytest -q packages/framework/graph-engine/tests/artifacts/test_contracts.py \
  packages/framework/graph-engine/tests/artifacts/test_baseline.py
uv run pyright packages/framework/graph-engine/graph_engine/artifacts/contracts.py \
  packages/framework/graph-engine/graph_engine/artifacts/baseline.py
uv run ruff check packages/framework/graph-engine/graph_engine/artifacts/contracts.py \
  packages/framework/graph-engine/graph_engine/artifacts/baseline.py \
  packages/framework/graph-engine/tests/artifacts/test_contracts.py \
  packages/framework/graph-engine/tests/artifacts/test_baseline.py
```

Expected: all commands exit `0`; the tests prove path closure is complete before any workspace or external activity is used.

- [ ] **Step 6: Commit the contract surface.**

```bash
git add \
  packages/framework/graph-engine/graph_engine/artifacts/__init__.py \
  packages/framework/graph-engine/graph_engine/artifacts/contracts.py \
  packages/framework/graph-engine/graph_engine/artifacts/baseline.py \
  packages/framework/graph-engine/tests/artifacts/test_contracts.py \
  packages/framework/graph-engine/tests/artifacts/test_baseline.py
git commit -m "feat: define closed artifact contracts"
```

### Task 2: Implement versioned canonical JSON and YAML codecs

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/artifacts/codecs.py`
- Create: `packages/framework/graph-engine/graph_engine/artifacts/resources/__init__.py`
- Create: `packages/framework/graph-engine/graph_engine/artifacts/resources/canonical-serialization-v1.json`
- Create: `packages/framework/graph-engine/tests/artifacts/test_codecs.py`
- Modify: `packages/framework/graph-engine/graph_engine/artifacts/__init__.py`

**Interfaces:**

- Consumes: `graph_engine.canonical.JSONValue` and `canonical_json_bytes`.
- Produces: `ArtifactSerializer`, `CANONICAL_JSON_V1`, `CANONICAL_YAML_V1`, `serializer_corpus_digest(...)`, and `builtin_artifact_serializers()`.

- [ ] **Step 1: Add the checked normative corpus.**

Create a JSON document with exact `id`, `value`, `json_utf8`, and `yaml_utf8` fields for these cases:

```json
{
  "schema_version": "1",
  "cases": [
    {"id": "null", "value": null, "json_utf8": "null", "yaml_utf8": "null\n"},
    {"id": "true", "value": true, "json_utf8": "true", "yaml_utf8": "true\n"},
    {"id": "false", "value": false, "json_utf8": "false", "yaml_utf8": "false\n"},
    {"id": "integer", "value": -7, "json_utf8": "-7", "yaml_utf8": "-7\n"},
    {"id": "decimal", "value": 1.5, "json_utf8": "1.5", "yaml_utf8": "1.5\n"},
    {"id": "unicode", "value": "雪\n\"x\"", "json_utf8": "\"雪\\n\\\"x\\\"\"", "yaml_utf8": "\"雪\\n\\\"x\\\"\"\n"},
    {"id": "empty-array", "value": [], "json_utf8": "[]", "yaml_utf8": "[]\n"},
    {"id": "empty-object", "value": {}, "json_utf8": "{}", "yaml_utf8": "{}\n"},
    {"id": "ordered-object", "value": {"z": 1, "a": [true, null]}, "json_utf8": "{\"a\":[true,null],\"z\":1}", "yaml_utf8": "\"a\":\n  - true\n  - null\n\"z\": 1\n"},
    {"id": "nested", "value": [{"b": "x", "a": 0}], "json_utf8": "[{\"a\":0,\"b\":\"x\"}]", "yaml_utf8": "-\n  \"a\": 0\n  \"b\": \"x\"\n"}
  ]
}
```

The packaged resource is the one immutable serializer compatibility corpus used by production and tests. Load it with `importlib.resources.files("graph_engine.artifacts.resources")`; do not copy it into the test tree. Its digest is SHA-256 over `canonical_json_bytes` of the complete parsed document, not over raw resource bytes.

- [ ] **Step 2: Write codec RED tests.**

```python
@pytest.mark.parametrize("case", load_serializer_corpus())
def test_builtin_serializers_match_normative_corpus(case: dict[str, object]) -> None:
    assert CANONICAL_JSON_V1.serialize(case["value"]) == case["json_utf8"].encode()
    assert CANONICAL_YAML_V1.serialize(case["value"]) == case["yaml_utf8"].encode()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), {1: "bad"}])
def test_serializers_reject_values_outside_normalized_json_domain(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        CANONICAL_YAML_V1.serialize(value)
```

Also assert JSON has no BOM/trailing newline, YAML has exactly one final LF, YAML contains no directives/tags/anchors/aliases, mapping keys are stable, string scalars are JSON-quoted, and repeated calls return byte-identical results.

- [ ] **Step 3: Run the codec tests and confirm the missing-symbol failure.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/artifacts/test_codecs.py
```

Expected: collection fails because `graph_engine.artifacts.codecs` and the serializer constants do not exist.

- [ ] **Step 4: Implement the closed serializer interface and emitters.**

```python
@dataclass(frozen=True, slots=True)
class ArtifactSerializer:
    serializer_id: Literal["canonical-json-v1", "canonical-yaml-v1"]
    media_codec: Literal["json", "yaml"]
    corpus_digest: str
    _serialize: Callable[[JSONValue], bytes] = field(compare=False, repr=False)

    def serialize(self, value: JSONValue) -> bytes:
        return self._serialize(value)
```

`canonical-json-v1` delegates to the existing `canonical_json_bytes`. Implement YAML with a small recursive emitter over only normalized JSON values: sorted string keys, two-space block indentation, JSON-quoted keys/strings using `ensure_ascii=False`, lowercase null/booleans, finite JSON number spelling, `[]`/`{}` for empty containers, no implicit YAML typing, and one final LF. Reject unsupported objects and nonfinite numbers before producing bytes. Do not use mutable library defaults as the byte contract.

Build both constants using the checked corpus digest and return them in exact serializer-ID order from `builtin_artifact_serializers()`.

- [ ] **Step 5: Prove cross-process byte stability.**

Add a test that serializes the `ordered-object` and `nested` cases in a subprocess launched through the workspace interpreter, compares base64 bytes to the parent process, and varies `PYTHONHASHSEED` between `1` and `999`. The subprocess imports only `graph_engine.artifacts.codecs` and reads the canonical JSON value from stdin. A package-resource test asserts the same corpus is available through `importlib.resources` from the installed workspace wheel and its digest equals both built-in serializer records.

Run:

```bash
uv run pytest -q packages/framework/graph-engine/tests/artifacts/test_codecs.py
uv run pyright packages/framework/graph-engine/graph_engine/artifacts/codecs.py
uv run ruff check packages/framework/graph-engine/graph_engine/artifacts/codecs.py \
  packages/framework/graph-engine/tests/artifacts/test_codecs.py
```

Expected: corpus, rejection, and cross-process tests pass.

- [ ] **Step 6: Commit the serializer contract.**

```bash
git add \
  packages/framework/graph-engine/graph_engine/artifacts/__init__.py \
  packages/framework/graph-engine/graph_engine/artifacts/codecs.py \
  packages/framework/graph-engine/graph_engine/artifacts/resources/__init__.py \
  packages/framework/graph-engine/graph_engine/artifacts/resources/canonical-serialization-v1.json \
  packages/framework/graph-engine/tests/artifacts/test_codecs.py
git commit -m "feat: add canonical artifact serializers"
```

### Task 3: Define self-authenticating materialization receipts

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/artifacts/receipts.py`
- Create: `packages/framework/graph-engine/tests/artifacts/test_receipts.py`
- Modify: `packages/framework/graph-engine/graph_engine/artifacts/__init__.py`

**Interfaces:**

- Consumes: `AttemptKey`, `ArtifactContract.digest`, `ArtifactBaselineEntry`/snapshot identities, canonical digests, and serializer/schema identifiers from Tasks 1–2.
- Produces: `MaterializationEntryReceipt.build(...)`, `MaterializationReceipt.build(...)`, canonical projections, and model-time digest authentication.

- [ ] **Step 1: Write entry-receipt RED tests.**

```python
def test_entry_receipt_binds_object_schema_and_final_bytes() -> None:
    receipt = MaterializationEntryReceipt.build(
        attempt_key=attempt_key.digest,
        artifact_contract_digest="a" * 64,
        structured_result_digest="b" * 64,
        agent_result_schema_digest="c" * 64,
        slot_id="manifest",
        logical_path="qa/manifest.json",
        mutation="replace",
        repair_strategy=None,
        baseline_exists=True,
        baseline_sha256="1" * 64,
        baseline_size=17,
        baseline_mode=0o640,
        baseline_entry_digest="2" * 64,
        document_model_id="assurance.quality.ManifestV1",
        artifact_document_schema_id="assurance.quality.schema.manifest.v1",
        artifact_document_schema_digest="d" * 64,
        projector_id="assurance.quality.projector.manifest.v1",
        media_codec="json",
        serializer_id="canonical-json-v1",
        serializer_corpus_digest="8" * 64,
        parity_mode="semantic_migration",
        semantic_migration_record_digest="f" * 64,
        document_object_digest="e" * 64,
        byte_digest=sha256(b"{}").hexdigest(),
        byte_size=2,
        resolved_mode=0o644,
        file_mode_policy="fixed",
    )

    assert receipt.entry_digest == receipt.canonical_digest()
    assert receipt.logical_path == "qa/manifest.json"
```

Mutate every bound field one at a time while retaining the old `entry_digest` and assert model validation rejects the receipt.

- [ ] **Step 2: Write aggregate ordering and empty-receipt tests.**

```python
def test_aggregate_receipt_has_stable_entry_order_and_digest() -> None:
    receipt = MaterializationReceipt.build(
        attempt_key=attempt_key.digest,
        artifact_contract_digest="a" * 64,
        prepared_digest="f" * 64,
        structured_result_digest="b" * 64,
        manifest_digest="9" * 64,
        baseline_snapshot_digest="7" * 64,
        workspace_identity_digest="6" * 64,
        entries=(entry_for("z", "z.json"), entry_for("a", "a.json")),
    )
    assert tuple((e.slot_id, e.logical_path) for e in receipt.entries) == (
        ("a", "a.json"),
        ("z", "z.json"),
    )
    assert receipt.receipt_digest == receipt.canonical_digest()


def test_raw_only_contract_has_authenticated_empty_materialization_receipt() -> None:
    receipt = MaterializationReceipt.build(
        attempt_key=attempt_key.digest,
        artifact_contract_digest="a" * 64,
        prepared_digest="f" * 64,
        structured_result_digest="b" * 64,
        manifest_digest="9" * 64,
        baseline_snapshot_digest="7" * 64,
        workspace_identity_digest="6" * 64,
        entries=(),
    )
    assert receipt.entries == ()
```

Reject duplicate `(slot_id, logical_path)`, an entry from another Attempt/contract/result/baseline snapshot, unsorted stored entries, mismatched aggregate digest, invalid mutation/repair combinations, create-with-present-baseline, replace/repair-with-absent-baseline, preserve-without-baseline-mode, invalid modes, invalid paths, negative sizes, and byte-size/digest values with invalid formats.

- [ ] **Step 3: Run RED and implement the exact receipt fields.**

Run:

```bash
uv run pytest -q packages/framework/graph-engine/tests/artifacts/test_receipts.py
```

Expected: collection fails because `graph_engine.artifacts.receipts` is absent.

Implement `MaterializationEntryReceipt` with every field named in Step 1 plus `entry_digest`; this explicitly binds mutation/repair strategy, authenticated baseline existence/digest/size/mode, projector, serializer corpus, parity mode, and optional authenticated semantic-migration record rather than only their friendly IDs. `exact_bytes` requires a null migration-record digest and `semantic_migration` requires a resolved record digest. Implement `MaterializationReceipt` with `attempt_key`, `artifact_contract_digest`, `prepared_digest`, `structured_result_digest`, `manifest_digest`, `baseline_snapshot_digest`, `workspace_identity_digest`, ordered `entries`, and `receipt_digest`. Both `build(...)` methods construct the exact canonical projection first; validators recompute it when reading persisted data.

- [ ] **Step 4: Run focused and static checks.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/artifacts/test_receipts.py
uv run pyright packages/framework/graph-engine/graph_engine/artifacts/receipts.py
uv run ruff check packages/framework/graph-engine/graph_engine/artifacts/receipts.py \
  packages/framework/graph-engine/tests/artifacts/test_receipts.py
```

Expected: all commands exit `0`.

- [ ] **Step 5: Commit the receipt surface.**

```bash
git add \
  packages/framework/graph-engine/graph_engine/artifacts/__init__.py \
  packages/framework/graph-engine/graph_engine/artifacts/receipts.py \
  packages/framework/graph-engine/tests/artifacts/test_receipts.py
git commit -m "feat: authenticate materialization receipts"
```

### Task 4: Authenticate artifact dependencies and provider-neutral Agent execution authority at Boot

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/artifacts/registry.py`
- Create: `packages/framework/graph-engine/tests/artifacts/test_registry.py`
- Create: `packages/framework/graph-engine/tests/composition/test_agent_execution_authority_registry.py`
- Modify: `packages/framework/graph-engine/graph_engine/plugin_api.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/models.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/contributions.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/registries.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/lock.py`
- Modify: `packages/framework/graph-engine/graph_engine/boot/graph_revision.py`
- Modify: `packages/framework/graph-engine/graph_engine/boot/boot.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_attempt_contract_registry.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_lock_model.py`
- Modify: `packages/framework/graph-engine/tests/boot/test_boot_manifest_parity.py`
- Modify: `packages/framework/graph-engine/graph_engine/artifacts/__init__.py`

**Interfaces:**

- Consumes: authenticated descriptor/realized contribution machinery from Semantic Attempt Task 2, ProductLock v3 and `GraphBuildManifest.attempt_contract_digests` from Foundation Task 2, Task 1 contracts, and Task 2 built-in serializers.
- Produces: `ArtifactModelEntry`, `ArtifactPathResolverEntry`, `ArtifactProjectorEntry`, `ArtifactRawMemberSelectorEntry`, authenticated `ArtifactSemanticMigrationRecord` entries, `ArtifactRegistry`, `ResolvedArtifactSlot`, `ResolvedArtifactContract`; plus provider-neutral `StructuredToolchainRequirement` and Product-owned installed requirement registry, `StructuredCommandSecretRequirement`, `AgentExecutionAuthority`, its installed same-owner contribution registry, and exact artifact/toolchain/execution-authority dependency projections inside ProductLock and later resolved Attempt contract digests.

- [ ] **Step 1: Write registry resolution RED tests.**

```python
def test_resolve_artifact_contract_authenticates_every_typed_dependency() -> None:
    resolved = registry.resolve(
        owner_id="assurance.quality",
        contract=typed_artifact_contract,
        dependency_order=("assurance.quality", "graph-engine"),
    )
    slot = resolved.slots[0]
    assert slot.model.model_id == typed_artifact_contract.slots[0].document_model_id
    assert slot.model.schema_digest == typed_artifact_contract.slots[0].artifact_document_schema_digest
    assert slot.serializer.serializer_id == "canonical-json-v1"
    assert resolved.resolved_contract_digest == resolved.canonical_digest()
```

Add one test each for missing model, wrong schema digest, missing path resolver, missing projector, missing/cross-owner/drifted raw-member selector, a raw selector attached to a typed slot or absent from a raw slot, missing serializer, a `semantic_migration` slot with a missing/cross-owner/drifted migration record, an `exact_bytes` slot carrying a record, extra realized declaration, cross-owner contribution without dependency authority, project/config-tree contribution, duplicate ID, and callable provenance outside the authenticated owner source.

In `test_agent_execution_authority_registry.py`, construct canonical rows for network/secret `none` and `input_conditioned`. Freeze the exact value contract before Capability Task 2 consumes it:

```python
class StructuredToolchainRequirement(FrozenModel):
    requirement_id: str
    purpose: str
    allowed_logical_recipe_ids: tuple[str, ...]
    immutable_dependency_class_id: str
    environment_policy_id: str
    scratch_output_policy_id: str
    requirement_digest: str


class StructuredCommandSecretRequirement(FrozenModel):
    requirement_id: str
    selection_mode: Literal["required", "input_conditioned"]
    environment_alias_keys: tuple[str, ...]
    purpose: str
    delivery_mode: Literal["one_shot_env_v1"]
    value_encoding: Literal["utf8_no_nul_v1"]
    allowed_target_classes: tuple[str, ...]
    max_value_bytes: int
    requirement_digest: str


class AgentExecutionAuthority(FrozenModel):
    contract_id: str
    structured_toolchain_requirement_ids: tuple[str, ...]
    structured_network_mode: Literal["none", "required", "input_conditioned"]
    structured_network_requirement_ids: tuple[str, ...]
    structured_command_secret_mode: Literal["none", "required", "input_conditioned"]
    structured_command_secret_requirements: tuple[StructuredCommandSecretRequirement, ...]
    authority_digest: str
```

Require canonical sorted/unique IDs, logical recipe IDs, alias keys and target classes; nonblank toolchain purpose/policy/dependency-class IDs; positive bounded `max_value_bytes`; exact digest recomputation; empty requirement tuples for mode `none`; matching nonempty requirement selection modes otherwise; and globally unique secret requirement IDs with byte-identical reuse only when explicitly allowed by the installed Feature owner. Toolchain requirement rows are Product-owned installed semantic data: Feature authorities may reference their IDs but cannot contribute, replace, or self-describe them. Reject a toolchain row from a Feature/project/SUT/config source, an authority row supplied by Product/project/SUT/config, an owner mismatch with its Agent contract, missing/extra contract row, an unknown toolchain ID, duplicate/unsorted recipe or alias values, empty purpose, unknown delivery/encoding, mode/requirement disagreement, alternate value limit, or digest drift. These are provider-neutral declarations only: neither type contains a provider, adapter, executable or executable path, argv, concrete environment value, image/runtime ref, sandbox/backend, target/address/DNS row, secret handle/value/source/generation, or callable. A concrete Product profile may satisfy a semantic requirement only after Task 5 authenticates the complete installed row and the OpenCode gate qualifies its exact implementation.

- [ ] **Step 2: Run RED and confirm the registry is missing.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/artifacts/test_registry.py
```

Expected: collection fails because `graph_engine.artifacts.registry` does not exist.

- [ ] **Step 3: Implement installed entry types and the registry.**

Define these immutable entries:

```python
@dataclass(frozen=True, slots=True)
class ArtifactModelEntry:
    owner_id: str
    model_id: str
    model: type[BaseModel] = field(compare=False, repr=False)
    schema_id: str
    schema_document: FrozenJSONValue
    schema_digest: str
    provenance: ExecutableProvenance


@dataclass(frozen=True, slots=True)
class ArtifactPathResolverEntry:
    owner_id: str
    resolver_id: str
    resolver: Callable[[FrozenJSONValue], tuple[str, ...]] = field(compare=False, repr=False)
    provenance: ExecutableProvenance


@dataclass(frozen=True, slots=True)
class ArtifactProjectorEntry:
    owner_id: str
    projector_id: str
    projector: Callable[[ArtifactProjectionInput], FrozenJSONValue] = field(compare=False, repr=False)
    provenance: ExecutableProvenance


@dataclass(frozen=True, slots=True)
class ArtifactRawMemberSelectorEntry:
    owner_id: str
    selector_id: str
    selector: Callable[[RawMemberSelectionInput], tuple[RawMemberDeclaration, ...]] = field(
        compare=False, repr=False
    )
    provenance: ExecutableProvenance


@dataclass(frozen=True, slots=True)
class StructuredToolchainRequirementEntry:
    owner_id: str
    requirement: StructuredToolchainRequirement
    provenance: ExecutableProvenance
    entry_digest: str
```

`ArtifactRegistry` stores immutable ID maps for models, path resolvers, projectors, raw-member selectors, Feature-owned `ArtifactSemanticMigrationRecord` values, and the two framework-owned serializers. Separately, the composition registry stores Product-owned `StructuredToolchainRequirement` entries and exactly one same-owner `AgentExecutionAuthority` per structured Agent contract. It constructs each model schema document once, freezes that exact value, and hashes the same canonical bytes used later for OpenCode/local validation. Callable identity and Python `repr` never enter canonical projections; source provenance authenticates code. A raw-member selector is distinct from a typed projector: it can name candidate raw members only within already bound exact/root authority and supplies no byte/digest truth. Authority registration checks syntax and ownership here; Task 5 performs exact requirement-ID resolution against the complete Product-owned toolchain registry before it can construct a structured executor.

`ResolvedArtifactContract` retains `ArtifactContract`, exact resolved entries, optional exact semantic-migration record plus digest per slot, and a data-only `resolved_contract_digest`. It resolves dependencies only and exposes no `bind(...)` method: Task 5's `bind_attempt_dispatch(...)` is the sole graph-facing binding seam and the sole caller of Task 1's low-level `bind_artifact_paths(...)`. Downstream manifest and receipt fields named `artifact_contract_digest` carry the value of `ArtifactContract.digest`; they are evidence-field names, not a second property on the contract.

- [ ] **Step 4: Extend authenticated contributions and ProductLock closure.**

Add descriptor/realized projections for `artifact_models`, `artifact_path_resolvers`, `artifact_projectors`, `artifact_raw_member_selectors`, `artifact_semantic_migrations`, `structured_toolchain_requirements`, and `agent_execution_authorities`; serializers remain framework-owned and cannot be contributed by a Feature or project. Only the installed Product owner may contribute toolchain requirements, and only the same installed Feature owner may contribute its execution-authority row. Configuration-tree contributions must reject all seven sets.

Extend `RegistrySet` with one `artifacts: ArtifactRegistry` plus the closed toolchain-requirement and Agent-execution-authority registries and add all installed contribution digests to ProductLock closure. Task 5, after it owns the four-model structured executor, resolves every authority toolchain ID against that exact registry and extends the resolved Attempt-contract projection with the complete requirement values/digest, authority row, prepared/result model and schema facts, `requires_structured_output`, Artifact contract/resolved dependency projections, and semantic-migration records. Do not reference or partially construct the not-yet-defined Task 5 executor types here.

Keep `GraphBuildManifest` fields exactly `revision`, `entrypoint_contract_digests`, and `attempt_contract_digests`. This task proves Artifact registry drift changes ProductLock registry digests; Task 5 proves the same drift changes the owning Attempt digest. No callable, materialized bytes, compiled graph, or runtime port enters the manifest.

- [ ] **Step 5: Test Boot and revision drift fail closed.**

Add tests that change exactly one document schema, projector source digest, path-resolver source digest, serializer corpus digest, artifact path template, codec, serializer ID, toolchain requirement purpose/recipe/dependency/environment/scratch policy/digest, authority toolchain/network requirement ID/mode, or command-secret alias/purpose/delivery/encoding/target-class/byte-limit/digest and assert ProductLock v3 registry closure changes. The owning `attempt_contract_digest` half of this test lands in Task 5. Assert the legacy InvocationLock v2 golden remains byte-identical during coexistence. Assert dry Boot and runtime Boot produce identical registry projections and that `.aa`/SUT attempts to register artifact, toolchain-requirement, or Agent execution-authority code/data are rejected before factory execution.

Run:

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/artifacts/test_registry.py \
  packages/framework/graph-engine/tests/composition/test_agent_execution_authority_registry.py \
  packages/framework/graph-engine/tests/composition/test_attempt_contract_registry.py \
  packages/framework/graph-engine/tests/composition/test_lock_model.py \
  packages/framework/graph-engine/tests/boot/test_boot_manifest_parity.py
uv run pyright packages/framework/graph-engine/graph_engine/artifacts/registry.py \
  packages/framework/graph-engine/graph_engine/plugin_api.py \
  packages/framework/graph-engine/tests/composition/test_agent_execution_authority_registry.py
uv run lint-imports
```

Expected: every command exits `0`; drift and authority violations fail during composition/Boot, before external dispatch.

- [ ] **Step 6: Commit the authenticated artifact registry.**

```bash
git add \
  packages/framework/graph-engine/graph_engine/artifacts/__init__.py \
  packages/framework/graph-engine/graph_engine/artifacts/registry.py \
  packages/framework/graph-engine/graph_engine/plugin_api.py \
  packages/framework/graph-engine/graph_engine/composition/models.py \
  packages/framework/graph-engine/graph_engine/composition/contributions.py \
  packages/framework/graph-engine/graph_engine/composition/registries.py \
  packages/framework/graph-engine/graph_engine/composition/lock.py \
  packages/framework/graph-engine/graph_engine/boot/graph_revision.py \
  packages/framework/graph-engine/graph_engine/boot/boot.py \
  packages/framework/graph-engine/tests/artifacts/test_registry.py \
  packages/framework/graph-engine/tests/composition/test_agent_execution_authority_registry.py \
  packages/framework/graph-engine/tests/composition/test_attempt_contract_registry.py \
  packages/framework/graph-engine/tests/composition/test_lock_model.py \
  packages/framework/graph-engine/tests/boot/test_boot_manifest_parity.py
git commit -m "feat: authenticate artifact registry closure"
```

### Task 5: Replace the opaque composite with a resolved structured executor seam

**Dependency:** Capability Migration Task 2 has already installed the exact 33-row provider-neutral execution-authority table and public-to-private Product input envelope beside the all-raw Artifact baseline. This task consumes and authenticates that table; it may not invent defaults while later Feature waves migrate typed artifacts.

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/attempts/contracts.py`
- Modify: `packages/framework/graph-engine/graph_engine/attempts/context.py`
- Modify: `packages/framework/graph-engine/graph_engine/attempts/keys.py`
- Modify: `packages/framework/graph-engine/graph_engine/attempts/__init__.py`
- Modify: `packages/framework/graph-engine/graph_engine/plugin_api.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/models.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/contributions.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/registries.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/lock.py`
- Rewrite: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/execution_contract.py`
- Create: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/attempt_executor.py`
- Create: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/runtime_binding.py`
- Modify: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/__init__.py`
- Modify: `packages/adapters/agent-runtime-contracts/tests/test_models.py`
- Create: `packages/adapters/agent-runtime-contracts/tests/test_attempt_executor.py`
- Create: `packages/adapters/agent-runtime-contracts/tests/test_runtime_binding.py`
- Modify: `packages/framework/graph-engine/tests/attempts/test_contracts.py`
- Modify: `packages/framework/graph-engine/tests/attempts/test_keys.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_attempt_contract_registry.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_lock_model.py`
- Modify: `packages/framework/graph-engine/tests/boot/test_boot_manifest_parity.py`

**Interfaces:**

- Consumes: base `AttemptExecutor`, `ResolvedAttemptContract`, `AttemptExecutionContext`, `ResolvedArtifactContract`, Capability Task 2's exact 33-row toolchain/network/command-secret authority table, and exact Product runtime binding closure.
- Consumes: the sole `StructuredAgentActivityPort`, ephemeral `StructuredActivityCallContext`, `StructuredAgentActivityRequest`, and closed structured dispatch/observation values from `graph_engine.attempts.structured_activity`, created by OpenCode Structured Output Gate Task 1.
- Consumes: Task 4's sole provider-neutral `StructuredToolchainRequirement`/Product-owned registry, `StructuredCommandSecretRequirement`, and exact installed `AgentExecutionAuthority` row for every contract.
- Produces: authenticated `StructuredPrepareHandlerEntry`/`StructuredFinalizeHandlerEntry` registries; `ResolvedStructuredToolchainProfile`, provider-neutral `StructuredToolchainInvocationSelection`/`BoundStructuredToolchainInvocationScope`, Product-owned shared `StructuredNetworkRequirementEntry`/registry plus `StructuredNetworkRequirement`/`ResolvedStructuredNetworkBackend`/`ResolvedStructuredNetworkTargetCatalog`/`BoundStructuredNetworkAccess`, and value-free `ResolvedStructuredCommandSecretBinding`/`BoundStructuredCommandSecret`; closed `ResolvedDirectExecutor | ResolvedStructuredAgentExecutor`; immutable `BoundAttemptDispatch`; pure deterministic `bind_attempt_dispatch(...)`; `StructuredFinalizeInput`; `ReadOnlyRawArtifactPort`; and four-type `AgentExecutionContract[InputT, PreparedT, AgentResultT, OutputT]` with `requires_structured_output`.

- [ ] **Step 1: Write the closed-variant RED tests in core.**

```python
def test_resolved_executor_is_exactly_direct_or_structured() -> None:
    assert isinstance(direct_contract.executor, ResolvedDirectExecutor)
    assert isinstance(agent_contract.executor, ResolvedStructuredAgentExecutor)
    with pytest.raises(TypeError):
        ResolvedAttemptContract(executor=object(), **base_fields)


def test_structured_executor_digest_excludes_ports_but_binds_models_and_artifacts() -> None:
    first = structured_executor(activity=activity_a, finalize=finalize_a)
    second = structured_executor(activity=activity_b, finalize=finalize_b)
    assert first.canonical_projection() == second.canonical_projection()
    assert first.executor_digest == second.executor_digest
    assert "activity" not in first.canonical_projection()
```

Changing instruction digest, prepared/result model symbols, Agent-result schema ID/document/digest/limits, `requires_structured_output`, artifact contract/resolution digest, prepare/finalize handler IDs or their authenticated entry/provenance digests, runtime-binding/certification digests, or source provenance digests must change `executor_digest`.

Also freeze the network contract here. `StructuredNetworkRequirement` is installed provider-neutral data with canonical `requirement_id`, closed `selection_mode` (`none | required | input_conditioned`), allowed target classes/transports/nonsecret environment-alias keys, closed `allowed_http_methods`, authenticated path-scope policy ID/digest, frontend subresource-origin policy ID/digest, upstream TLS trust/CA-or-pin/hostname-verification policy ID/digest, min/max target cardinality, connection/byte/duration ceilings, redirect policy, and a digest. Install shared rows through one Product-owned `StructuredNetworkRequirementEntry`/registry; the entry binds Product owner ID, requirement value/digest, executable provenance/source digest, and entry digest. Feature contracts reference IDs but cannot contribute or override rows. Feature contracts declare only sorted requirement IDs plus the explicit mode; resolution puts the complete sorted `StructuredNetworkRequirement` tuple and canonical tuple digest in `ResolvedStructuredAgentExecutor`, because the pure binder—not Product or the adapter—enforces class/transport/alias/HTTP-method/path-scope/subresource-origin/upstream-TLS-trust/cardinality/limit/redirect policy. The registry participates in `RegistrySet`, ProductLock, graph revision, and dry/runtime manifest parity. Missing, extra, duplicate, non-Product/project/Feature-owned, or digest-drifted rows fail Boot. Feature declarations never name a host, port, URL, DNS rule, proxy, or backend. `ResolvedStructuredNetworkBackend` is Product-owned and binds backend/policy IDs and digests, immutable gateway/runtime refs, controlled-resolver policy digest, qualification digest, and effective ceilings; it contains no Feature/project-provided executable, host path, or secret. `ResolvedStructuredNetworkTargetCatalog` is an immutable Product-resolved value whose organization-data rows have opaque target ID, target class/purpose, canonical scheme/host/port/TLS-SNI, a controlled-resolver-frozen address set, a sorted value-free `accepted_secret_requirement_ids` tuple, and an allowlisted mapping such as `BASE_URL`/`API_BASE_URL` to Product-issued synthetic gateway origins. Rows contain no userinfo, query, fragment, credential, callable, or model-selected value. Product resolves and authenticates this catalog before executor/Attempt binding; its catalog/version/resolver digest enters ProductLock and the full frozen value enters `ResolvedStructuredAgentExecutor`, so the binder remains pure and performs no DNS/I/O. `BoundStructuredNetworkAccess` binds the exact selected requirement IDs, target catalog digest, sorted target IDs, matched target classes, frozen requirement→target mapping, target/address/alias rows, effective HTTP methods/path-scope/subresource-origin/upstream-TLS-trust policy digests, resolved backend/policy/qualification digests, effective limits, and `access_digest`. Every selected row must match exactly one declared requirement's class/transport/alias/HTTP-method/path-scope/subresource-origin/upstream-TLS-trust/cardinality policy. Recovery reuses this value and never repeats DNS or substitutes a target/environment value.

Consume command-secret authority separately from both network addresses and secret values. Task 4's `StructuredCommandSecretRequirement` is the sole Feature-owned canonical value with requirement ID, closed `selection_mode`, sorted unique allowlisted environment alias keys, secret purpose, delivery mode `one_shot_env_v1`, fixed value encoding `utf8_no_nul_v1`, allowed target classes, max value bytes, and digest. It never contains a handle or value. Product runtime binding maps each requirement ID to one already authorized secret handle through a value-free `ResolvedStructuredCommandSecretBinding(requirement_id, secret_handle_id, binding_digest)`. `ResolvedStructuredAgentExecutor` carries the complete authenticated binding catalog plus its catalog digest and the adapter-only activity-secret handle set; it does not pretend this catalog is the input-conditioned selected set. `BoundStructuredCommandSecret` is the selected Attempt row and embeds the complete authenticated enforcement facts unchanged: requirement ID and requirement digest, `selection_mode`, alias tuple, purpose, delivery mode, value encoding, `max_value_bytes`, allowed target classes, `secret_handle_id` plus binding digest, exact selected target IDs/target-set digest, and bound-row digest. It contains no value/generation and may be selected only from sorted opaque `sut_command_secret_requirement_ids` already present in validated `InputT`. The dispatch also carries an authenticated `authorized_secret_handles` tuple and digest equal to the exact union of the executor's adapter-only activity handles and the selected command handles. The bound secret-set and authorized-handle-union digests enter dispatch/key; actual raw bytes and their opaque generation-set digest are resolved only inside the later `AttemptSecretLifetime` after resource authorization. Before workspace/activity/command work the lifetime factory authenticates the embedded requirement/binding digests and enforces that exact row's byte limit, strict UTF-8 decoding, and no-NUL rule without trimming or changing the raw bytes used by the HMAC generation proof. One selected requirement may install the same value under its complete authenticated alias tuple, but aliases cannot be selected independently. Unknown/extra aliases, handle mismatch, an unauthorized handle, a secret without an exact selected target row whose catalog entry accepts that secret requirement (target class alone is insufficient), or input-conditioned selection not justified by validated mode fails before key/workspace/activity.

Move Task 4's deferred Attempt-digest closure here. The resolved Attempt canonical projection includes prepared model symbol/schema digest; Agent-result model symbol/schema ID/document digest/limits; `requires_structured_output`; the complete Artifact contract including target kind and occurrence/phase mutation rules; every resolved model/path-resolver/projector/serializer owner and schema/corpus digest; and any semantic-migration record identity/golden/test/digest. Parameterized ProductLock/Boot tests change one dependency at a time and require both the owning `attempt_contract_digest` and graph revision to change while dry/runtime manifests remain equal.

Reject a missing/noncanonical Agent-result schema ID, nonpositive/unbounded limits, a resolved schema larger than `max_schema_bytes`, a shipped schema golden that differs from the exact model-generated document, or any Product binding that supplies alternate schema metadata.

- [ ] **Step 2: Freeze the pre-key dispatch binding and extend Attempt identity.**

Define the sole provider-neutral, callable-free command selection and bound-scope values in `attempts/contracts.py`. `StructuredToolchainInvocationSelection` is part of the validated Feature `InputT`; it carries a sorted unique tuple of tagged recipe rows plus a canonical digest. The only initial row shapes are: `selected_tests(logical_recipe_id, sorted exact selected_test_node_ids)` for `closed-api-tests-v1`, `closed-e2e-tests-v1`, and `closed-fuzz-tests-v1`; and `bounded_performance(logical_recipe_id="closed-performance-benchmark-v1", benchmark_file, users, spawn_rate_millis_per_second, duration_seconds)` with positive bounded integers. Milliruns-per-second is the canonical spawn-rate representation; floating point is forbidden. Core types use canonical strings and tagged frozen values rather than importing Feature literals, while the installed Product requirement registry validates the exact recipe-to-requirement relation.

```python
@dataclass(frozen=True, slots=True)
class StructuredToolchainInvocationSelection:
    recipes: tuple[StructuredToolchainRecipeSelection, ...]
    selection_digest: str


@dataclass(frozen=True, slots=True)
class BoundStructuredToolchainInvocationScope:
    task_input_digest: str
    structured_toolchain_requirement_set_digest: str
    structured_toolchain_profile_digest: str
    recipes: tuple[BoundStructuredToolchainRecipeInvocation, ...]
    scope_digest: str
```

Each `BoundStructuredToolchainRecipeInvocation` freezes the semantic requirement ID, logical recipe ID, tagged canonical parameter value, parameter-schema/policy digest, effective per-recipe limits, and invocation digest. It contains no provider, executable, path to an executable, argv, environment value, callable, socket/session token, or secret. The applicable Feature `InputT` model validates the selection against its closed case/family/test mapping: selected test node IDs are exact-set equal to the authenticated selected tests; the performance file is the one authenticated benchmark file and every numeric parameter is within the installed business bounds. A contract with no toolchain requirements must carry an empty selection. Unknown, duplicate, cross-family, cross-contract, broadened, subsetted, unsorted, parameter-mismatched, or over-limit selections fail during `InputT` validation or binding, before the Attempt key.

Define one provider-neutral value in `attempts/contracts.py`:

```python
@dataclass(frozen=True, slots=True)
class BoundAttemptDispatch(Generic[InputT]):
    semantic_occurrence_id: str
    business_activation: BusinessActivation
    business_activation_digest: str
    artifact_mutation_phase: ArtifactMutationPhase | None
    task_input: InputT = field(compare=False, repr=False)
    task_input_snapshot: FrozenJSONValue
    task_input_digest: str
    resources: ResourceClaims
    resource_claims_digest: str
    structured_toolchain_profile: ResolvedStructuredToolchainProfile | None
    structured_toolchain_profile_digest: str | None
    structured_toolchain_invocation_scope: BoundStructuredToolchainInvocationScope | None
    structured_toolchain_invocation_scope_digest: str | None
    structured_network_access: BoundStructuredNetworkAccess | None
    structured_network_access_digest: str | None
    structured_command_secrets: tuple[BoundStructuredCommandSecret, ...]
    structured_command_secret_set_digest: str | None
    authorized_secret_handles: tuple[str, ...]
    authorized_secret_handles_digest: str
    artifacts: BoundArtifactContract | None
    bound_artifact_contract_digest: str | None
    binding_digest: str
```

`TaskAttemptContract` gains one authenticated `non_artifact_resources: ResourceClaims | ResourceClaimTemplate`; its resolved writes/exclusive prefixes are explicit input/scratch claims and are never promotable. `AgentExecutionContract` also carries sorted unique provider-neutral `structured_toolchain_requirement_ids`, `structured_network_requirement_ids`, `structured_command_secret_requirements`, and explicit network/secret modes derived from those installed requirements. Freeze the exact Capability mapping in this task's tests: only `assurance.execution.agent.execute.v1` and `assurance.execution.agent.run.v1` carry the canonical four-row tuple `sut-api-test-runner-v1`, `sut-e2e-test-runner-v1`, `sut-fuzz-test-runner-v1`, `sut-performance-benchmark-runner-v1`; all other 31 Agent contracts carry `()`. Resolve each ID to the complete Product-owned Task 4 value and include the sorted value tuple plus aggregate digest in the structured executor. Product runtime binding then resolves that exact tuple before the key to one installed `ResolvedStructuredToolchainProfile` containing only canonical requirement/profile/backend IDs, logical-recipe mappings, executable argv allowlist, immutable dependency/image ref digests, environment, limits, policy and qualification digests—never a Feature- or project-supplied host path. Missing, extra, reordered, semantically drifted, or profile-self-asserted requirements fail; an empty tuple requires `structured_toolchain_profile is None` and `structured_toolchain_invocation_scope is None`. It supplies one installed `ResolvedStructuredNetworkBackend`, full `ResolvedStructuredNetworkTargetCatalog`, adapter-only activity-secret handles, and a complete requirement-ID→authorized-handle binding catalog. Mode `none` requires empty declarations/input selections and `None`/empty bound values. `required` requires the declared cardinality. `input_conditioned` permits an empty selected set only when the validated Feature `InputT` names its installed offline/unauthenticated/source-only mode; otherwise the Feature validator requires applicable opaque target and secret-requirement IDs. No ambient `BASE_URL`, password/token variable, URI-mode variable, prompt, result, or project config may add a target, alias, handle, or value after validation.

`bind_attempt_dispatch(resolved_contract, validated_input, semantic_occurrence_id, business_activation, mutation_phase)` performs the complete deterministic pre-dispatch closure: validate/authenticate the full existing `BusinessActivation` and its canonical digest; validate/dump the exact installed `InputT`; authenticate that the occurrence belongs to this contract/node site and to the Artifact contract's derived phase-domain; authenticate the separately selected graph-owned phase; resolve total `resources` and `non_artifact_resources` only from that frozen snapshot; require the latter to be a subset of the former; bind the already resolved toolchain profile and require exact requirement-set satisfaction; validate `structured_toolchain_invocation_selection` against the complete resolved requirement tuple, the Feature model's exact closed mapping, and Product profile recipe IDs/parameter policies, then freeze it with `task_input_digest`, requirement-set digest, profile digest, exact parameter rows/limits, and its own scope digest into `BoundStructuredToolchainInvocationScope`; consume only the Product-created canonical `sut_network_selections` rows/digest already present in the frozen Feature input, require every row's requirement ID to resolve in `executor.structured_network_requirements`, and resolve its sorted target IDs from the already frozen target catalog; enforce exact requirement-row set equality plus target class/transport/alias/HTTP-method/path-scope/subresource-origin/redirect/cardinality/limit policy, reject missing/extra/cross-requirement rows or selection-digest replay drift, and freeze the mapping, target rows, policy digests, and limits into `BoundStructuredNetworkAccess` without DNS/I/O; select only declared `sut_command_secret_requirement_ids`, map them to Product-bound authorized handle identities, require each bound secret's exact target IDs to be selected under a requirement-compatible network row whose catalog entry accepts that secret requirement, and build the value-free bound secret tuple/digest; bind Artifact paths and select each occurrence+phase rule into one concrete mutation/repair strategy; derive raw roots from bound `agent_raw_bytes` slots and internal paths from the non-artifact write/exclusive subset; subtract typed/internal deny holes; require every total write/exclusive claim to belong to a typed slot, raw slot/root, or the explicit non-artifact subset; and build/authenticate `binding_digest` from the occurrence ID, BusinessActivation digest, selected phase, input snapshot, both claim projections, resolved toolchain profile and invocation-scope digests, network-access/selection digest, command-secret-set/authorized-handle digests, executor/contract identities, and bound-artifact digest. Direct executors require `artifacts is None`, `artifact_mutation_phase is None`, `structured_toolchain_profile is None`, `structured_toolchain_invocation_scope is None`, `structured_network_access is None`, an empty bound command-secret tuple, and all write/exclusive claims to be non-artifact; structured executors require a non-`None` phase and bound artifact contract, with toolchain/network/command-secret values exactly matching their requirements and selected target set. Unknown/duplicate/cross-contract recipe, broadened test-node selection, recipe-parameter mismatch or overflow, unknown/extra target ID, target-class mismatch, ambiguous requirement match, raw host/URL in prompt/result, late DNS/redirect/target/alias expansion, catalog/backend/qualification drift, or mode/cardinality mismatch fails before key construction. No `PreparedT`, `AgentResultT`, time, mutable workspace, provider value, DNS, or filesystem read enters the projection. A Feature path/network preflight is legal only when its authenticated values are already part of validated `InputT`.

Extend `AttemptKey`/`derive_attempt_key(...)` so the canonical key projection carries `task_input_digest`, `resource_claims_digest`, `structured_toolchain_profile_digest`, `structured_toolchain_invocation_scope_digest`, `structured_network_access_digest`, `structured_command_secret_set_digest`, `authorized_secret_handles_digest`, and `bound_artifact_contract_digest` (nullable only according to the closed executor rules), all copied from `BoundAttemptDispatch`; its existing semantic node and full BusinessActivation/digest must exactly agree with the bound occurrence/activation. Add tests that identical occurrence/activation/input/phase/resource/toolchain recipe selection/network/resolver/authorized-handle closure replays the same key; changing a dynamic Intake/Generation path, BusinessActivation, recipe ID, selected test node, bounded benchmark parameter, resolved profile/image/qualification digest, target catalog/backend/address/limit/access digest, selected command handle, adapter activity handle, or switching `initial -> repeat/repair` changes the dispatch/Attempt identity even when other business `InputT` is equal. Rotating only the secret value keeps the pre-key identity stable but must be detected by the post-authorization generation anchor. A path outside the resolved claims, unsatisfied/extra/project-supplied toolchain or network backend, unknown/cross-contract recipe, unknown target/occurrence/phase, project-supplied phase, raw endpoint, incomplete/extra authorized-handle union, or occurrence/activation/key mismatch is rejected before key construction; a mismatched supplied digest is rejected; and neither `prepare` nor the Kernel is invoked by binding. Baseline existence/content/size/mode is captured later by the durable workspace identity and must not create a second Attempt key for the same business activation.

The later `AttemptNodeFactory` must execute this exact order:

```text
select BusinessActivation + mutation phase once from the same state -> validate InputT -> resolve claims and bind artifact paths/policy
-> bind_attempt_dispatch
-> derive AttemptKey from the bound digests -> Kernel.execute_or_recover
```

It is forbidden to derive a provisional key and replace it after binding.

- [ ] **Step 2a: Install authenticated pure prepare/finalize entries.**

Define core contribution entries with callables excluded from canonical comparison and authenticated provenance included:

```python
@dataclass(frozen=True, slots=True)
class StructuredPrepareHandlerEntry(Generic[InputT]):
    owner_id: str
    handler_id: str
    handler: Callable[[InputT], Awaitable[object]] = field(compare=False, repr=False)
    provenance: ExecutableProvenance
    entry_digest: str


@dataclass(frozen=True, slots=True)
class StructuredFinalizeHandlerEntry(Generic[InputT, PreparedT, AgentResultT]):
    owner_id: str
    handler_id: str
    handler: Callable[
        [StructuredFinalizeInput[InputT, PreparedT, AgentResultT]], Awaitable[object]
    ] = field(compare=False, repr=False)
    provenance: ExecutableProvenance
    entry_digest: str
```

Extend installed descriptor/realized contributions and `RegistrySet` with exact owner-closed prepare/finalize maps. Configuration/SUT contributions are forbidden. `AgentExecutionContract.prepare_handler_id` and `finalize_handler_id` must each resolve exactly one same-owner entry; their IDs, entry digests, and provenance digests enter the Attempt contract, ProductLock, resolved executor, and graph revision. Reject missing/extra/cross-owner/drifted entries, a callable with the legacy `(TaskRequest, TaskContext)` shape, any second parameter, and descriptor/realized mismatch. Existing legacy `TaskHandler` registrations remain only for shadow/YAML aliases and delegate to the same pure callable after constructing validated input; they are not the source of the structured executor and are deleted with the legacy path.

- [ ] **Step 3: Consume the sole activity seam and define only the finalizer ports.**

Import the activity request, binding, dispatch/observation unions, success value, and `StructuredAgentActivityPort` from `graph_engine.attempts.structured_activity`. Add an architecture test that searches `graph_engine.attempts` and fails if any second class or protocol with those names is declared outside that module:

```python
def test_structured_activity_seam_has_one_definition() -> None:
    assert StructuredAgentActivityPort.__module__ == (
        "graph_engine.attempts.structured_activity"
    )
    assert StructuredAgentActivityRequest.__module__ == (
        "graph_engine.attempts.structured_activity"
    )
```

Define only the provider-neutral read-only finalizer input owned by this plan:

```python
class ReadOnlyRawArtifactPort(Protocol):
    def read_bytes(self, logical_path: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class StructuredFinalizeInput(Generic[InputT, PreparedT, AgentResultT]):
    task_input: InputT
    prepared: PreparedT
    agent_result: AgentResultT
    typed_documents: Mapping[str, tuple[BaseModel, ...]]
    materialization_receipt: MaterializationReceipt
    raw_artifacts: ReadOnlyRawArtifactPort
```

The imported `dispatch_or_adopt_activity(...)` owns stable binding/adoption and returns only the closed bound/pending/indeterminate union. The imported `observe_activity(...)` returns the closed running/succeeded/failed/canceled/pending/indeterminate union. Only `StructuredActivitySucceeded.candidate` is eligible for Kernel validation, and it remains untrusted. Neither method returns an `OutputT` or materializes files. No OpenCode protocol type appears in core.

- [ ] **Step 4: Define both executor variants.**

```python
@dataclass(frozen=True, slots=True)
class ResolvedDirectExecutor(Generic[InputT, OutputT]):
    execute: AttemptExecutor[InputT, OutputT] = field(compare=False, repr=False)
    executor_digest: str


@dataclass(frozen=True, slots=True)
class ResolvedStructuredCommandSecretBinding:
    requirement_id: str
    secret_handle_id: str
    binding_digest: str


@dataclass(frozen=True, slots=True)
class ResolvedStructuredAgentExecutor(Generic[InputT, PreparedT, AgentResultT, OutputT]):
    contract_id: str
    prepare_handler_id: str
    finalize_handler_id: str
    instruction: FrozenJSONValue
    prepare: Callable[[InputT], Awaitable[object]] = field(
        compare=False, repr=False
    )
    prepared_model: type[PreparedT] = field(compare=False, repr=False)
    prepared_model_symbol: str
    prepared_schema: FrozenJSONValue
    prepared_schema_digest: str
    activity: StructuredAgentActivityPort = field(compare=False, repr=False)
    agent_result_model: type[AgentResultT] = field(compare=False, repr=False)
    agent_result_model_symbol: str
    agent_result_schema_id: str
    agent_result_schema: FrozenJSONValue
    agent_result_schema_digest: str
    max_schema_bytes: int
    max_result_bytes: int
    requires_structured_output: Literal[True]
    provider_model: str
    certification_row_digest: str
    runtime_binding_digest: str
    structured_toolchain_requirement_ids: tuple[str, ...]
    structured_toolchain_profile: ResolvedStructuredToolchainProfile | None
    structured_toolchain_profile_digest: str | None
    structured_network_requirement_ids: tuple[str, ...]
    structured_network_requirements: tuple[StructuredNetworkRequirement, ...]
    structured_network_requirements_digest: str | None
    structured_network_mode: Literal["none", "required", "input_conditioned"]
    structured_network_backend: ResolvedStructuredNetworkBackend | None
    structured_network_backend_digest: str | None
    structured_network_target_catalog: ResolvedStructuredNetworkTargetCatalog | None
    structured_network_target_catalog_digest: str | None
    structured_command_secret_requirements: tuple[StructuredCommandSecretRequirement, ...]
    structured_command_secret_mode: Literal["none", "required", "input_conditioned"]
    activity_secret_handles: tuple[str, ...]
    activity_secret_handles_digest: str
    structured_command_secret_binding_catalog: tuple[
        ResolvedStructuredCommandSecretBinding, ...
    ]
    structured_command_secret_binding_catalog_digest: str | None
    artifact_contract: ResolvedArtifactContract
    finalize: Callable[
        [StructuredFinalizeInput[InputT, PreparedT, AgentResultT]], Awaitable[object]
    ] = field(compare=False, repr=False)
    executor_digest: str
```

The core resolved contract union is closed and discriminated; direct contracts do not carry unused prepared/result/artifact fields. The structured executor stores the exact frozen schema documents whose digests it authenticates plus only the immutable request metadata needed for the Kernel to construct `StructuredAgentActivityRequest`. `activity`, `prepare`, and `finalize` callable identities stay out of canonical projection; handler/source, schema, runtime-binding, and certification digests stay in it.

- [ ] **Step 5: Rewrite the adapter-owned Agent contract and constructor.**

`AgentExecutionContract[InputT, PreparedT, AgentResultT, OutputT]` contains owner/contract IDs, prepare/finalize handler IDs, skill/profile, four model types, explicit canonical `agent_result_schema_id`, positive bounded `max_schema_bytes` and `max_result_bytes`, `requires_structured_output=True`, sorted provider-neutral `structured_toolchain_requirement_ids`, sorted `structured_network_requirement_ids`, explicit `structured_network_mode`, sorted `structured_command_secret_requirements`, explicit `structured_command_secret_mode`, `ArtifactContract`, total `resources`, explicit `non_artifact_resources`, retry, timeout, and required validators. Every one of the 33 contracts explicitly declares both requirement families and both modes, including empty toolchain/network/secret closure; no default infers shell, network, or secret access. Its Artifact contract derives the one exact occurrence/phase domain from identical slot rule-key sets. Checkpoint S compares that domain with both the frozen 34-occurrence/Feature-contract fixture and the live catalog emitted by the already-existing StateGraphs. Remove `requires_provider_schema` and any `provider_schema` capability field.

The Feature contract is the sole authority for the schema ID and limits. Resolution calls the installed `AgentResultT.model_json_schema()` once, freezes that exact document, rejects it if its canonical bytes exceed `max_schema_bytes`, computes `agent_result_schema_digest` from those same bytes, and copies ID/document/digest/limits unchanged into `ResolvedStructuredAgentExecutor` and later `StructuredResultSchema`. Shipped `resources/schemas/agent-results/*.schema.json` files are generated review/golden artifacts whose tests require canonical equality with that resolved document; they are not a second runtime schema source. Product/provider/model binding may neither override the ID/limits nor regenerate a different document.

`attempt_executor.py` resolves the same-owner authenticated `StructuredPrepareHandlerEntry` and `StructuredFinalizeHandlerEntry`, every declared installed `StructuredNetworkRequirement` row plus the canonical requirement-tuple digest, the Product-selected activity adapter, the exact Agent-result schema ID/document/digest/limits, and `ArtifactRegistry.resolve(...)`, then returns one core `ResolvedStructuredAgentExecutor`. The resolved entry/provenance digests remain in its canonical projection while only the callables are excluded. It does not adapt legacy `TaskContext`, expose an `execute()` method that hides `PreparedT`/`AgentResultT`, or call projectors, materializer, or finalizer itself.

`AgentRuntimeBinding` remains deployment-only: contract ID, runtime handler ID, provider, model, policy, adapter secret handles, command-secret requirement-to-handle bindings, authenticated union of all authorized handles, authenticated adapter-capability-set digest, generic `satisfied_requirements`, selected certification-row digest, selected installed structured-toolchain profile ID/digest, selected structured-network backend ID/digest, and resolved target-catalog digest. `attempt_executor.py` checks only that `requires_structured_output` is in the authenticated generic requirement set and that the selected profile/backend/catalog/handle identities exactly satisfy the Feature's provider-neutral requirement IDs/modes; it does not know which concrete adapter capability/backend satisfied either. Product resolves/qualifies DNS and nonsecret aliases into the immutable catalog before constructing the executor; `attempt_executor.py` copies the full callable-free catalog value, value-free command-secret bindings, and digests into `ResolvedStructuredAgentExecutor`, never a resolver or secret-value port. The later OpenCode/Product child maps `requires_structured_output` to `opencode_structured_output` and installs/qualifies the concrete command/network/secret-injection backends. Before that gate, the exact 33 production binding records may exist with an empty satisfied set/profile/backend, but resolving a required Agent contract fails before dispatch; core/Kernel tests use explicitly test-only satisfied values excluded from Product declarations. Neither layer may change Feature models, result limits, toolchain/network/secret requirements or modes, target/handle identities, artifact slots, resolvers, projectors, serializers, resources, validators, or finalizer. `attempt_executor.py` copies selected provider/model, certification, profile/backend/catalog/handle, and full binding digests into the resolved executor; the later OpenCode child plan defines how those data are qualified.

- [ ] **Step 6: Prove prepare and finalizer have no runtime authority.**

Add a test prepare callable whose only argument is validated `InputT`; assert resolution rejects a callable accepting `AttemptExecutionContext` or a second context parameter. Add a test finalizer that receives validated model instances, typed documents, and a `MaterializationReceipt`; assert its raw port exposes only `read_bytes`. Attempt to access `write_root`, `write_bytes`, workspace binding, runtime adapter, secret/admission/journal/fence/effect/activity ports, host path, or clock and assert those attributes do not exist. A second test returns an unvalidated `OutputT` candidate and proves the Kernel-facing caller must still validate it. Capability child waves separately prove every one of the 33 production handlers has moved all prior ambient file reads into its validated `InputT` projection.

- [ ] **Step 7: Run the core and adapter contract suites.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/attempts/test_contracts.py \
  packages/framework/graph-engine/tests/attempts/test_keys.py \
  packages/framework/graph-engine/tests/composition/test_attempt_contract_registry.py \
  packages/framework/graph-engine/tests/composition/test_lock_model.py \
  packages/framework/graph-engine/tests/boot/test_boot_manifest_parity.py \
  packages/adapters/agent-runtime-contracts/tests/test_models.py \
  packages/adapters/agent-runtime-contracts/tests/test_attempt_executor.py \
  packages/adapters/agent-runtime-contracts/tests/test_runtime_binding.py
uv run pyright \
  packages/framework/graph-engine/graph_engine/attempts \
  packages/adapters/agent-runtime-contracts/agent_runtime_contracts
uv run lint-imports
```

Expected: all commands exit `0`; core imports no adapter package, and the adapter produces exactly one `ResolvedStructuredAgentExecutor` per Agent contract.

- [ ] **Step 8: Commit the structured executor seam.**

```bash
git add \
  packages/framework/graph-engine/graph_engine/attempts/__init__.py \
  packages/framework/graph-engine/graph_engine/attempts/contracts.py \
  packages/framework/graph-engine/graph_engine/attempts/context.py \
  packages/framework/graph-engine/graph_engine/attempts/keys.py \
  packages/framework/graph-engine/graph_engine/plugin_api.py \
  packages/framework/graph-engine/graph_engine/composition/models.py \
  packages/framework/graph-engine/graph_engine/composition/contributions.py \
  packages/framework/graph-engine/graph_engine/composition/registries.py \
  packages/framework/graph-engine/graph_engine/composition/lock.py \
  packages/framework/graph-engine/tests/attempts/test_contracts.py \
  packages/framework/graph-engine/tests/attempts/test_keys.py \
  packages/framework/graph-engine/tests/composition/test_attempt_contract_registry.py \
  packages/framework/graph-engine/tests/composition/test_lock_model.py \
  packages/framework/graph-engine/tests/boot/test_boot_manifest_parity.py \
  packages/adapters/agent-runtime-contracts/agent_runtime_contracts/__init__.py \
  packages/adapters/agent-runtime-contracts/agent_runtime_contracts/execution_contract.py \
  packages/adapters/agent-runtime-contracts/agent_runtime_contracts/attempt_executor.py \
  packages/adapters/agent-runtime-contracts/agent_runtime_contracts/runtime_binding.py \
  packages/adapters/agent-runtime-contracts/tests/test_models.py \
  packages/adapters/agent-runtime-contracts/tests/test_attempt_executor.py \
  packages/adapters/agent-runtime-contracts/tests/test_runtime_binding.py
git commit -m "feat: expose structured Agent phases to the Kernel"
```

### Task 6: Persist prepared values, structured results, manifests, and receipts in an Attempt-private content store

**Dependency:** Semantic Attempt Kernel Task 8 has created `attempts/events.py`, `persistence/attempt_journal.py`, the base journal state machine, and `AttemptExecutionContext` fence/journal ports.

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/plugin_api.py`
- Create: `packages/framework/graph-engine/graph_engine/persistence/attempt_artifacts.py`
- Create: `packages/framework/graph-engine/graph_engine/attempts/snapshot_admission.py`
- Create: `packages/framework/graph-engine/graph_engine/attempts/command_broker.py`
- Create: `packages/framework/graph-engine/tests/persistence/test_attempt_artifact_store.py`
- Create: `packages/framework/graph-engine/tests/attempts/test_snapshot_admission.py`
- Create: `packages/framework/graph-engine/tests/attempts/test_command_secret_injection.py`
- Create: `packages/framework/graph-engine/tests/attempts/test_command_broker_registration.py`
- Modify: `packages/framework/graph-engine/graph_engine/attempts/context.py`
- Modify: `packages/framework/graph-engine/graph_engine/attempts/events.py`
- Modify: `packages/framework/graph-engine/graph_engine/persistence/attempt_journal.py`
- Modify: `packages/framework/graph-engine/graph_engine/persistence/resource_authorization.py`
- Modify: `packages/framework/graph-engine/tests/persistence/test_resource_authorization_store.py`
- Modify: `packages/framework/graph-engine/tests/persistence/test_attempt_journal.py`

**Interfaces:**

- Consumes: `AttemptKey`, the existing fenced append-only storage primitive, `AttemptJournalPort.load/append/ensure_durable`, the existing durable `ResourceAuthorizationRecord`, and canonical bytes/digests.
- Produces: provider-neutral `SecretGenerationSetIdentity`/`ResolvedSecretMaterial`/`ResolvedSecretGeneration`/`SecretMaterialGenerationPort`, exact resource-authorization secret-generation identity anchoring, provider-neutral `StructuredCommandSecretInjectionRequest`/`StructuredCommandSecretSink`/`CommandSecretInjectionEntry`/`CommandSecretInjectionReceipt`/`StructuredCommandSecretInjectionPort`, `StructuredCommandBrokerRegistrationRequest`/`StructuredCommandBrokerRegistrationPort`/`BoundStructuredCommandBrokerSession`, `AttemptArtifactBlobRef`, `AttemptSnapshotAdmissionPort`/`SnapshotAdmissionReceipt`, `AttemptArtifactStorePort`, durable `AttemptArtifactStore`, and journal events `PreparedSnapshotPersisted`, `AgentWritesClosed`, `StructuredResultPersisted`, `RawPostImagePersisted`, `MaterializationPrepared`, and `MaterializationCompleted` folded into the existing Attempt snapshot.

- [ ] **Step 1: Write content-addressed store RED tests.**

```python
async def test_put_if_absent_is_content_addressed_private_and_idempotent(store) -> None:
    content = b'{"change_id":"chg-1"}'
    admitted = await admission.admit(
        attempt_key=attempt_key,
        kind="prepared",
        content=content,
    )
    first = await store.put_if_absent(
        attempt_key=attempt_key,
        kind="prepared",
        content=content,
        admission=admitted,
        fencing_token=4,
    )
    replay = await store.put_if_absent(
        attempt_key=attempt_key,
        kind="prepared",
        content=content,
        admission=admitted,
        fencing_token=4,
    )
    assert replay == first
    assert await store.read_verified(first) == b'{"change_id":"chg-1"}'
    assert stat.S_IMODE(blob_path(first).stat().st_mode) == 0o600
```

Add tests for a same-address existing file with different bytes, symlink/hardlink substitution, truncated bytes, wrong size/digest, a blob from another Attempt, oversized content, stale fence, store restart, and `ensure_durable(ref)` after process reconstruction. Parameterize all twelve blob kinds—including the three command kinds consumed later by OpenCode Task 6—with direct, base64, URL-safe-base64, and hexadecimal authorized-secret canaries and assert admission rejects before any file/event/command-state row exists. Add resource-authorization tests that bind one exact `SecretGenerationSetIdentity(algorithm_id, key_id, generation_set_digest)` exactly once, adopt the same identity under a new fence, and reject drift in any field or an unprovable generation before a blob/event. Simulate rotation between candidate computation and admission, and after blob persistence but before event append; neither path may append the event or reuse the orphan blob under the new generation.

- [ ] **Step 2: Run the store test and confirm the missing-module failure.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/persistence/test_attempt_artifact_store.py
```

Expected: collection fails because `graph_engine.persistence.attempt_artifacts` is absent.

- [ ] **Step 3: Implement the blob reference and store port.**

```python
ArtifactBlobKind = Literal[
    "prepared",
    "raw_write_closure",
    "agent_result",
    "raw_artifact_bytes",
    "raw_postimage_manifest",
    "materialized_document",
    "materialized_bytes",
    "materialization_manifest",
    "materialization_receipt",
    "command_request",
    "command_view_manifest",
    "command_execution_receipt",
]


class AttemptArtifactBlobRef(FrozenModel):
    attempt_key: str
    kind: ArtifactBlobKind
    content_digest: str
    byte_size: int
    admission_policy_digest: str
    secret_generation_set_digest: str
    store_key: str


class AttemptArtifactStorePort(Protocol):
    async def put_if_absent(
        self,
        *,
        attempt_key: AttemptKey,
        kind: ArtifactBlobKind,
        content: bytes,
        admission: SnapshotAdmissionReceipt,
        fencing_token: int,
    ) -> AttemptArtifactBlobRef: ...

    async def read_verified(self, ref: AttemptArtifactBlobRef) -> bytes: ...

    async def ensure_durable(self, ref: AttemptArtifactBlobRef) -> None: ...


@dataclass(frozen=True, slots=True)
class ResolvedSecretMaterial:
    handle: str
    source_provider_id: str
    opaque_generation_id: str
    value: bytes = field(compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class SecretGenerationSetIdentity:
    algorithm_id: str
    key_id: str
    generation_set_digest: str


@dataclass(frozen=True, slots=True)
class ResolvedSecretGeneration:
    identity: SecretGenerationSetIdentity
    materials: tuple[ResolvedSecretMaterial, ...]


class SecretMaterialGenerationPort(Protocol):
    async def resolve_exact_generation(
        self,
        handles: tuple[str, ...],
        *,
        expected_identity: SecretGenerationSetIdentity | None,
    ) -> ResolvedSecretGeneration: ...


@dataclass(frozen=True, slots=True)
class StructuredCommandSecretInjectionRequest:
    attempt_key_digest: str
    activity_reference_digest: str
    workspace_identity_digest: str
    structured_command_broker_binding_digest: str
    broker_registration_receipt_digest: str
    structured_toolchain_invocation_scope_digest: str | None
    command_ordinal: int
    active_fence: int
    command_identity_digest: str
    sink_capability_digest: str
    structured_command_secret_set_digest: str
    command_secret_entries_digest: str
    structured_network_access_digest: str
    allowed_target_classes: tuple[str, ...]
    allowed_target_ids: tuple[str, ...]
    allowed_target_set_digest: str
    secret_generation_set_digest: str
    request_digest: str


class StructuredCommandSecretSink(Protocol):
    sink_capability_digest: str
    broker_registration_receipt_digest: str
    command_ordinal: int

    async def write_once(self, *, alias_key: str, value: bytes) -> None: ...
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class CommandSecretInjectionEntry:
    requirement_id: str
    requirement_digest: str
    selection_mode: Literal["required", "input_conditioned"]
    alias_keys: tuple[str, ...]
    purpose: str
    secret_handle_id: str
    binding_digest: str
    delivery_mode: Literal["one_shot_env_v1"]
    value_encoding: Literal["utf8_no_nul_v1"]
    max_value_bytes: int
    target_classes: tuple[str, ...]
    allowed_target_ids: tuple[str, ...]
    allowed_target_set_digest: str
    bound_secret_digest: str
    entry_digest: str


@dataclass(frozen=True, slots=True)
class CommandSecretInjectionReceipt:
    attempt_key_digest: str
    activity_reference_digest: str
    workspace_identity_digest: str
    structured_command_broker_binding_digest: str
    broker_registration_receipt_digest: str
    structured_toolchain_invocation_scope_digest: str | None
    command_ordinal: int
    active_fence: int
    command_identity_digest: str
    sink_capability_digest: str
    structured_command_secret_set_digest: str
    structured_network_access_digest: str
    entries: tuple[CommandSecretInjectionEntry, ...]
    entries_digest: str
    secret_generation_set_digest: str
    receipt_digest: str


class StructuredCommandSecretInjectionPort(Protocol):
    async def inject_once(
        self,
        *,
        request: StructuredCommandSecretInjectionRequest,
        sink: StructuredCommandSecretSink,
    ) -> CommandSecretInjectionReceipt: ...


@dataclass(frozen=True, slots=True)
class StructuredCommandBrokerRegistrationRequest:
    attempt_key_digest: str
    semantic_occurrence_id: str
    activity_reference_digest: str
    workspace_identity_digest: str
    raw_write_generation: int
    active_fence: int
    structured_toolchain_invocation_scope: BoundStructuredToolchainInvocationScope | None
    structured_toolchain_invocation_scope_digest: str | None
    structured_command_secret_set_digest: str | None
    command_secret_entries_digest: str | None
    structured_network_access_digest: str | None
    allowed_target_ids: tuple[str, ...]
    allowed_target_set_digest: str | None
    secret_generation_set_digest: str
    request_digest: str


@dataclass(frozen=True, slots=True)
class BoundStructuredCommandBrokerSession:
    activity_reference_digest: str
    workspace_identity_digest: str
    structured_toolchain_invocation_scope_digest: str | None
    broker_binding_digest: str
    broker_registration_receipt_digest: str
    close: Callable[[], Awaitable[None]] = field(compare=False, repr=False)


class StructuredCommandBrokerRegistrationPort(Protocol):
    async def register_or_adopt(
        self,
        *,
        request: StructuredCommandBrokerRegistrationRequest,
        snapshot_admission: AttemptSnapshotAdmissionPort,
        command_secret_injection: StructuredCommandSecretInjectionPort,
    ) -> BoundStructuredCommandBrokerSession: ...


class AttemptSecretLifetime(Protocol):
    secret_generation_identity: SecretGenerationSetIdentity
    activity_secrets: SecretPort
    snapshot_admission: AttemptSnapshotAdmissionPort
    command_secret_injection: StructuredCommandSecretInjectionPort

    async def close(self) -> None: ...


class AttemptSecretLifetimeFactoryPort(Protocol):
    async def open(
        self,
        *,
        attempt_key: AttemptKey,
        authorized_handles: tuple[str, ...],
        authorized_handles_digest: str,
        activity_handles: tuple[str, ...],
        command_secrets: tuple[BoundStructuredCommandSecret, ...],
        expected_generation_identity: SecretGenerationSetIdentity | None,
    ) -> AttemptSecretLifetime: ...
```

Before registration or adapter dispatch, Kernel deterministically derives and durably anchors `activity_reference_digest` from the Attempt key, semantic occurrence, structured contract/request identity, and workspace identity. This is not the later adapter `StructuredActivityBinding` digest. Registration, command store, injection request/receipt, raw-write closure, and activity events all bind the stable reference and the nullable `structured_toolchain_invocation_scope_digest`; `dispatch_or_adopt_activity(...)` must return a binding that authenticates the same reference before any tool/session mutation is enabled. Kernel passes the complete `BoundStructuredToolchainInvocationScope` in `StructuredCommandBrokerRegistrationRequest`, directly to the Product broker. The broker authenticates it against dispatch/key/profile and never reconstructs it from an adapter request, model tool payload, or Feature `InputT`. A mismatched reference or scope closes the broker session and fails without command launch. `register_or_adopt(...)` is idempotent for the exact request/scope/generation/fence adoption rules, returns only an opaque binding/registration/scope digest plus a Kernel-held close callable, and excludes both callables/capabilities from canonical projections. The Product implementation may deliver its unforgeable plugin session token directly to the trusted boundary plugin, but neither the core return value nor the adapter request/model exposes that token.

`AttemptExecutionContext` carries only `AttemptSecretLifetimeFactoryPort` plus provider-neutral `StructuredCommandBrokerRegistrationPort`, never a preconstructed scanner, adapter-visible injection port, Product type, or raw secret view. Inside the existing `authorize_resources` stage, after claim acquisition but before workspace/activity/blob work, Kernel calls `open(...)` with `dispatch.authorized_secret_handles`, its authenticated digest, the executor's adapter-only activity handles, and the dispatch's value-free selected command-secret tuple, passing the complete already anchored `SecretGenerationSetIdentity` on adoption. The factory rejects unless the authorized tuple is exactly the sorted union of activity and selected command handles and every bound secret's complete requirement/binding facts and digests authenticate against the executor registry. The Product implementation consumes `SecretMaterialGenerationPort` and returns `ResolvedSecretGeneration`; it must propagate that port-produced opaque identity unchanged into the ephemeral lifetime while validating command materials against each bound row's exact `max_value_bytes` and `utf8_no_nul_v1` policy. The framework never invents or recomputes a second generation-set algorithm from `ResolvedSecretMaterial`. The lifetime additionally contains: an activity `SecretPort` scoped only to adapter handles; snapshot admission over every exact activity/command value; and a write-only `StructuredCommandSecretInjectionPort` scoped to the bound command aliases/handles/target classes. Kernel CAS-binds all three identity fields to the existing durable `ResourceAuthorizationRecord` before using any view. First execution has no expected identity; adoption requires exact algorithm/key equality plus `hmac.compare_digest` for the aggregate digest. A changed/unavailable identity or CAS disagreement closes the lifetime, maps through the existing pending/indeterminate/permanent policy, and permits no workspace creation, activity dispatch, command launch, blob write, or event append. Kernel holds the lifetime locally through the Attempt call and closes/destroys it in `finally`; prepare, finalizer, Feature, and model code never receive any secret value; the adapter receives provider/activity secrets only through its scoped activity view and never receives a command-secret value.

Task 6 owns the durable schema change: extend `ResourceAuthorizationRecord` and its CAS/store tests with `secret_generation_algorithm_id`, `secret_generation_key_id`, and `secret_generation_set_digest`. A newly resource-authorized structured record may carry one explicit unbound identity state only until the lifetime is opened; that state cannot authorize a workspace, activity, blob, event, command, or receipt. One fenced CAS binds all three fields from the exact port-produced `SecretGenerationSetIdentity`; afterward they are non-null and immutable, the record returns the identity on reopen, and supplies it as `expected_generation_identity`. Recovery of the pre-bind crash cut may resolve material and attempt that same one-time CAS; a competing different identity loses and is destroyed. The record stores no per-handle generation row, source locator, raw key, or secret value. Legacy/direct nullable state is distinct and cannot authorize a structured blob. Product Task 11 owns the concrete HMAC algorithm that produces this provider-neutral identity, not its persistence contract.

`StructuredCommandSecretInjectionPort.inject_once(...)` has no read/list/resolve/value-return API and never enters `StructuredActivityCallContext`, an adapter object, request payload, or model/tool-visible value. Kernel registers it directly with the Product-owned command broker for one exact Attempt/activity/workspace binding; that registration creates an unforgeable sink capability per command ordinal. The exact frozen request/receipt models above are the broker's sole canonical internal wire shapes; their digest covers every preceding field in declaration order under the installed canonical JSON projection. The port accepts only the pre-registered Product-owned write-only `StructuredCommandSecretSink` whose `sink_capability_digest` matches the request: the sink permits one write per selected alias followed by `close()`, rejects duplicate/late writes, and deliberately exposes no read, list, enumerate, environment, handle-resolution, or value-return operation. An adapter-created or wrong-ordinal fake sink is rejected before value resolution/write. The request binds Attempt/activity/workspace, toolchain invocation-scope digest, command ordinal and identity, active fence, sink capability, bound command-secret-set plus canonical `CommandSecretInjectionEntry` aggregate digest, bound network access, target classes, exact target IDs/target-set digest, and anchored generation-set digest. The nullable scope digest must exactly equal the dispatch, broker registration/session, and selected command row under the same profile/scope nullable rule; injection replay with a changed/absent/extra scope fails before value resolution or launch. Each entry repeats and authenticates the complete bound enforcement facts—requirement/binding/bound-row digests, selection mode, alias tuple, purpose, delivery, encoding, byte limit, target classes and exact targets—so the port never depends on a lost side lookup. The port rejects any drift, rechecks size/encoding/no-NUL, writes each exact value once into the sandbox launch environment or sealed descriptor according to `one_shot_env_v1`, closes the sink, then returns the metadata-only receipt over the same Attempt/activity/workspace/scope/command identity plus the ordered canonical entry tuple/digest and generation digest; parallel flat tuples are forbidden because they lose the per-requirement alias/handle/target relation. Values never enter argv, command request/view/store, transcript, log, exception, receipt, or durable environment. Command output, raw files, structured results, and all persisted command payloads still pass snapshot admission, so direct/base64/hex/URL-safe secret reflection is rejected. A command may use the secret only while connecting through its exact bound SUT target-ID subset accepted by that requirement; for a command with injected aliases the broker exposes only that network subview, and another same-class target is denied. Gateway/default-deny prevents arbitrary network exfiltration. Recovery may adopt a terminal command only under the exact same scope/generation/receipt and never launches it again under rotated material.

`AttemptSnapshotAdmissionPort.admit(attempt_key, kind, content)` comes only from that local lifetime; the framework scanner implementation lives in `attempts/snapshot_admission.py`. It returns an immutable receipt binding Attempt, kind, content digest, installed admission-policy digest, and the aggregate digest from the anchored `SecretGenerationSetIdentity` only after scanning each exact secret/canary in direct, base64, hexadecimal, and URL-safe-base64 form plus the existing credential-shape rules. Secret values are held only in the ephemeral lifetime, expose no read API, and never enter a receipt, digest, log, exception, or durable store. `AttemptArtifactStore.put_if_absent(...)` authenticates the receipt digest against the full identity in current durable authorization before opening a target, so callers cannot accidentally persist an unadmitted or wrong-generation prepared value, raw-write closure, Agent result, raw byte/post-image, materialized document/bytes, manifest/receipt, command request, command-view manifest, or command-execution receipt. OpenCode Task 6 may use only those three versioned command kinds plus `raw_artifact_bytes` for declared output content; its auxiliary state table stores refs/digests/status/fences only and validates the matching admission receipt on every transition. Admission rejection or generation drift creates no new blob, command-state row, or corresponding journal event, typed install, promotion, or effect; already durable workspace, Attempt, authorization, activity, and earlier-phase facts remain intact and recoverable.

The durable adapter uses an Attempt-private `blobs/sha256/<digest>` namespace under the runtime control root, directory descriptors with no-follow semantics, `0700` directories, `0600` regular files with one link, atomic temporary-file install, file and parent-directory fsync, and byte/digest/size verification before returning. `store_key` is canonical and contains no host absolute path. A blob is immutable; kind and Attempt ownership are authenticated in the reference even when two payloads share bytes.

Retention is conservative and explicit in this phase: the store has no delete/prune API and never garbage-collects a blob, so every reference reachable from an Attempt journal, materialization manifest/receipt, or terminal receipt remains readable across restart indefinitely. Archive/storage compaction is out of scope and may be added only with a proven reference-reachability algorithm and retention tests; it may not be inferred from terminal status.

- [ ] **Step 4: Add journal event RED tests.**

```python
def test_structured_pipeline_events_fold_in_strict_order(journal) -> None:
    journal.append(PreparedSnapshotPersisted(prepared_ref, prepared_schema_digest, prepared_digest))
    journal.append(
        AgentWritesClosed(
            closure_ref,
            closure_digest,
            activity_reference_digest,
            workspace_identity_digest,
            structured_toolchain_invocation_scope_digest,
            structured_command_broker_binding_digest,
            broker_registration_receipt_digest,
            close_generation,
            closing_fence,
            boundary_marker_digest,
            command_transcript_digest,
            command_secret_injection_aggregate_digest,
            structured_network_transcript_digest,
        )
    )
    journal.append(
        StructuredResultPersisted(
            result_ref,
            result_schema_digest,
            result_digest,
            activity_reference_digest,
            candidate_digest,
            terminal_message_id,
            raw_observation_digest,
        )
    )
    journal.append(
        RawPostImagePersisted(
            raw_postimage_manifest_ref,
            raw_postimage_manifest_digest,
            raw_artifact_byte_refs,
            closure_digest,
            result_digest,
        )
    )
    journal.append(
        MaterializationPrepared(
            manifest_ref,
            manifest_digest,
            document_refs,
            byte_refs,
        )
    )
    journal.append(MaterializationCompleted(receipt_ref, receipt_digest))

    snapshot = journal.load(attempt_key)
    assert snapshot.prepared_ref == prepared_ref
    assert snapshot.raw_write_closure_ref == closure_ref
    assert snapshot.structured_result_ref == result_ref
    assert snapshot.raw_postimage_manifest_ref == raw_postimage_manifest_ref
    assert snapshot.materialization_manifest_ref == manifest_ref
    assert snapshot.materialization_receipt_ref == receipt_ref
```

Reject closure-before-prepared, result-before-closure, raw-post-image-before-result, materialization-manifest-before-raw-post-image, completed-before-prepared-manifest, a second event with a different digest/ref, missing blob durability, activity/workspace/toolchain-invocation-scope/broker-binding/broker-registration-receipt/generation/marker mismatch, command-secret injection aggregate mismatch (including noncanonical empty aggregate), raw manifest/member mismatch, Attempt/contract/executor/graph-revision drift, duplicate sequence/gaps, and stale-fence append. An identical append at the same semantic phase is idempotent. A closure produced by fence N and durably marked before crash may be authenticated and appended by current fence N+1 with its historical `closing_fence=N`; the stale fence cannot append or mutate the marker.

- [ ] **Step 5: Implement compact events and fold state.**

Each event carries only refs and bounded identities:

```text
PreparedSnapshotPersisted:
  prepared_schema_digest, prepared_digest, prepared_ref, executor_digest
AgentWritesClosed:
  closure_ref, closure_digest, bound_activity_reference_digest,
  workspace_identity_digest, structured_toolchain_invocation_scope_digest,
  structured_command_broker_binding_digest,
  broker_registration_receipt_digest,
  close_generation, historical_closing_fence,
  boundary_marker_digest, command_transcript_digest,
  command_secret_injection_aggregate_digest,
  nullable structured_network_transcript_digest, executor_digest
StructuredResultPersisted:
  agent_result_schema_digest, structured_result_digest, result_ref,
  bound_activity_reference_digest, candidate_digest,
  terminal_assistant_message_id, raw_observation_digest, executor_digest
RawPostImagePersisted:
  artifact_contract_digest, bound_contract_digest, workspace_identity_digest,
  raw_write_closure_digest, structured_result_digest,
  raw_postimage_manifest_ref, raw_postimage_manifest_digest,
  ordered_raw_artifact_byte_refs, executor_digest
MaterializationPrepared:
  artifact_contract_digest, bound_contract_digest, manifest_digest,
  manifest_ref, ordered materialized_document_refs,
  ordered materialized_byte_refs, executor_digest
MaterializationCompleted:
  manifest_digest, materialization_receipt_digest, receipt_ref
```

Large prepared/closure/result/raw-post-image/materialization/receipt bodies never enter journal events. Before every `put_if_absent(...)`, obtain and verify a content-matched admission receipt; before append, call `ensure_durable(...)` for every referenced blob under the current fence. Task 8 adds bounded streaming admission/storage for raw files so a 512 MiB contract limit never implies one in-memory `bytes` allocation. The fold order is exactly Prepared → WritesClosed → Result → RawPostImage → MaterializationPrepared → MaterializationCompleted. Extend the existing Attempt snapshot rather than creating a second fold or journal.

- [ ] **Step 6: Run persistence and journal GREEN checks.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/attempts/test_snapshot_admission.py \
  packages/framework/graph-engine/tests/attempts/test_command_secret_injection.py \
  packages/framework/graph-engine/tests/attempts/test_command_broker_registration.py \
  packages/framework/graph-engine/tests/persistence/test_attempt_artifact_store.py \
  packages/framework/graph-engine/tests/persistence/test_attempt_journal.py \
  packages/framework/graph-engine/tests/persistence/test_resource_authorization_store.py
uv run pyright \
  packages/framework/graph-engine/graph_engine/plugin_api.py \
  packages/framework/graph-engine/graph_engine/persistence/attempt_artifacts.py \
  packages/framework/graph-engine/graph_engine/persistence/resource_authorization.py \
  packages/framework/graph-engine/graph_engine/attempts/snapshot_admission.py \
  packages/framework/graph-engine/graph_engine/attempts/command_broker.py \
  packages/framework/graph-engine/graph_engine/attempts/events.py
uv run ruff check \
  packages/framework/graph-engine/graph_engine/persistence/attempt_artifacts.py \
  packages/framework/graph-engine/graph_engine/attempts/snapshot_admission.py \
  packages/framework/graph-engine/graph_engine/attempts/command_broker.py \
  packages/framework/graph-engine/tests/attempts/test_snapshot_admission.py \
  packages/framework/graph-engine/tests/attempts/test_command_secret_injection.py \
  packages/framework/graph-engine/tests/attempts/test_command_broker_registration.py \
  packages/framework/graph-engine/graph_engine/attempts/events.py \
  packages/framework/graph-engine/tests/persistence/test_attempt_artifact_store.py
```

Expected: all commands exit `0`; no event contains an unbounded structured body, every durable blob was admitted, and the store exposes no deletion path.

- [ ] **Step 7: Commit the durable structured boundaries.**

```bash
git add \
  packages/framework/graph-engine/graph_engine/plugin_api.py \
  packages/framework/graph-engine/graph_engine/attempts/context.py \
  packages/framework/graph-engine/graph_engine/attempts/events.py \
  packages/framework/graph-engine/graph_engine/attempts/snapshot_admission.py \
  packages/framework/graph-engine/graph_engine/attempts/command_broker.py \
  packages/framework/graph-engine/graph_engine/persistence/attempt_artifacts.py \
  packages/framework/graph-engine/graph_engine/persistence/attempt_journal.py \
  packages/framework/graph-engine/graph_engine/persistence/resource_authorization.py \
  packages/framework/graph-engine/tests/persistence/test_attempt_artifact_store.py \
  packages/framework/graph-engine/tests/attempts/test_snapshot_admission.py \
  packages/framework/graph-engine/tests/attempts/test_command_secret_injection.py \
  packages/framework/graph-engine/tests/attempts/test_command_broker_registration.py \
  packages/framework/graph-engine/tests/persistence/test_attempt_journal.py \
  packages/framework/graph-engine/tests/persistence/test_resource_authorization_store.py
git commit -m "feat: persist structured Attempt snapshots"
```

### Task 7: Prepare one immutable materialization manifest without writing staging

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/artifacts/materializer.py`
- Create: `packages/framework/graph-engine/tests/artifacts/test_materializer_prepare.py`
- Modify: `packages/framework/graph-engine/graph_engine/artifacts/__init__.py`

**Interfaces:**

- Consumes: `ResolvedArtifactContract`, `BoundArtifactContract`, the authoritative `ArtifactBaselineSnapshot` reconstructed from the durable workspace binding, `ArtifactProjectionInput` containing authenticated immutable raw-post-image metadata, installed model/projector/serializer entries, validated `InputT`/`PreparedT`/`AgentResultT`, and Task 3 receipt field identities.
- Produces: self-authenticating `MaterializationManifestEntry`, `MaterializationManifest`, `PreparedMaterialization` carrying canonical document snapshots plus final byte payloads, and pure `ArtifactMaterializer.prepare_materialization(...)`.

- [ ] **Step 1: Write a multi-document preparation RED test.**

```python
def test_prepare_materialization_projects_validates_and_serializes_without_writes() -> None:
    prepared = materializer.prepare_materialization(
        attempt_key=attempt_key,
        resolved_contract=resolved_contract,
        bound_contract=bound_contract,
        baseline_snapshot=baseline_snapshot,
        projection_input=ArtifactProjectionInput(
            task_input=freeze_json(validated_input.model_dump(mode="json")),
            prepared=freeze_json(validated_prepared.model_dump(mode="json")),
            agent_result=freeze_json(validated_result.model_dump(mode="json")),
            raw_postimage=raw_postimage.projection(),
        ),
        prepared_digest="a" * 64,
        structured_result_digest="b" * 64,
        agent_result_schema_digest="c" * 64,
    )

    assert workspace_writes == []
    assert tuple((e.slot_id, e.logical_path) for e in prepared.manifest.entries) == (
        ("cases", "qa/case-a.yaml"),
        ("cases", "qa/case-b.yaml"),
        ("manifest", "qa/manifest.json"),
    )
    assert set(prepared.document_payloads) == {
        e.document_object_digest for e in prepared.manifest.entries
    }
    assert set(prepared.byte_payloads) == {e.byte_digest for e in prepared.manifest.entries}
    assert prepared.manifest.manifest_digest == prepared.manifest.canonical_digest()
```

Verify `typed_documents` contains validated Pydantic instances in the same slot/path order for the later finalizer.

- [ ] **Step 2: Add zero-write failure cases.**

Parameterize projector exception, wrong projector result cardinality, document-model rejection, repeatable path/document count mismatch, duplicate path, document size above slot `max_bytes`, collection above `max_items`, invalid serializer value, schema/model drift, missing/extra/drifted baseline entry, missing/extra/drifted raw-post-image member metadata, create-over-existing, replace-over-missing, bounded-repair target-set mismatch, preserve-mode-over-missing, and a projector that attempts ambient file/time/random/network access through a spy. Add a mixed Generation-style projector case where the untrusted Agent result reports a false raw-file digest: the typed manifest must use only `RawPostImageProjection.content_digest`, or reject an asserted mismatch, and never copy the Agent claim. For each case assert the materializer returns no manifest, creates no byte blob, and performs zero workspace calls.

- [ ] **Step 3: Run RED and confirm the materializer is missing.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/artifacts/test_materializer_prepare.py
```

Expected: collection fails because `graph_engine.artifacts.materializer` does not exist.

- [ ] **Step 4: Implement immutable manifest models.**

```python
class MaterializationManifestEntry(FrozenModel):
    slot_id: str
    logical_path: str
    mutation: Literal["create", "replace", "bounded_repair"]
    repair_strategy: Literal["complete_post_image"] | None
    baseline_exists: bool
    baseline_sha256: str | None
    baseline_size: int | None
    baseline_mode: int | None
    baseline_entry_digest: str
    document_model_id: str
    artifact_document_schema_id: str
    artifact_document_schema_digest: str
    projector_id: str
    media_codec: Literal["json", "yaml"]
    serializer_id: Literal["canonical-json-v1", "canonical-yaml-v1"]
    serializer_corpus_digest: str
    parity_mode: Literal["exact_bytes", "semantic_migration"]
    semantic_migration_record_digest: str | None
    document_object_digest: str
    document_object_size: int
    byte_digest: str
    byte_size: int
    resolved_mode: int
    file_mode_policy: Literal["fixed", "preserve_baseline"]
    entry_digest: str


class MaterializationManifest(FrozenModel):
    attempt_key: str
    artifact_contract_digest: str
    bound_contract_digest: str
    baseline_snapshot_digest: str
    workspace_identity_digest: str
    prepared_digest: str
    structured_result_digest: str
    agent_result_schema_digest: str
    entries: tuple[MaterializationManifestEntry, ...]
    manifest_digest: str


@dataclass(frozen=True, slots=True)
class PreparedMaterialization:
    manifest: MaterializationManifest
    document_payloads: Mapping[str, bytes] = field(compare=False, repr=False)
    byte_payloads: Mapping[str, bytes] = field(compare=False, repr=False)
    typed_documents: Mapping[str, tuple[BaseModel, ...]] = field(compare=False, repr=False)
```

Authenticate entry and manifest digests on construction/read. Manifest entries are in stable `(slot_id, logical_path)` order. A create entry authenticates absence; replace and bounded-repair entries authenticate the exact prior digest/mode; bounded repair also authenticates `complete_post_image`. Raw slots do not produce manifest entries, but the aggregate still binds the baseline/workspace snapshot used later to validate their sealed mutations. A raw-only contract produces an authenticated empty entry set rather than an unauthenticated baseline.

- [ ] **Step 5: Implement the pure preparation pipeline.**

For every bound typed slot in order:

1. authenticate the supplied baseline snapshot against the bound contract and durable workspace identity, then canonical-dump the already validated `InputT`, `PreparedT`, and `AgentResultT`, freeze those three JSON values into `ArtifactProjectionInput`, and call the installed projector once;
2. require one value for fixed cardinality or a bounded sequence matching its resolved path count for repeatable cardinality;
3. validate each exact document model;
4. dump it to normalized JSON mode, encode that normalized value with canonical JSON, calculate `document_object_digest`/`document_object_size`, and retain those canonical document bytes in `document_payloads` for durable recovery;
5. serialize with the resolved versioned serializer;
6. enforce per-entry and aggregate size bounds;
7. bind the slot's parity mode and, for `semantic_migration`, the exact authenticated migration-record digest;
8. require `create` absence or `replace`/`bounded_repair` presence; for bounded repair require one complete post-image for every authenticated resolved target; bind baseline existence/digest/mode and mutation/repair strategy;
9. calculate final byte digest and resolve mode from the fixed contract value or the authenticated baseline mode without a filesystem read;
10. create the immutable entry plus document- and byte-payload maps.

Do not persist blobs, create directories, install files, call a workspace port, or reread a baseline in this method. A repeated invocation with identical validated values, registry closure, and baseline snapshot must return an equal manifest and byte-identical payloads. Changing only baseline existence, digest, or mode changes the manifest—not the already established Attempt key—and is rejected on recovery once a workspace identity is durable.

- [ ] **Step 6: Run deterministic preparation checks.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/artifacts/test_materializer_prepare.py
uv run pyright packages/framework/graph-engine/graph_engine/artifacts/materializer.py
uv run ruff check \
  packages/framework/graph-engine/graph_engine/artifacts/materializer.py \
  packages/framework/graph-engine/tests/artifacts/test_materializer_prepare.py
```

Expected: all tests pass and the workspace spy records zero calls.

- [ ] **Step 7: Commit manifest preparation.**

```bash
git add \
  packages/framework/graph-engine/graph_engine/artifacts/__init__.py \
  packages/framework/graph-engine/graph_engine/artifacts/materializer.py \
  packages/framework/graph-engine/tests/artifacts/test_materializer_prepare.py
git commit -m "feat: prepare immutable artifact manifests"
```

### Task 8: Install anchored typed bytes idempotently and build the aggregate receipt

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/plugin_api.py`
- Modify: `packages/framework/graph-engine/graph_engine/runtime/__init__.py`
- Modify: `packages/framework/graph-engine/graph_engine/persistence/attempt_artifacts.py`
- Modify: `packages/framework/graph-engine/graph_engine/attempts/snapshot_admission.py`
- Modify: `packages/framework/graph-engine/graph_engine/artifacts/baseline.py`
- Modify: `packages/framework/graph-engine/graph_engine/artifacts/materializer.py`
- Modify: `packages/framework/graph-engine/graph_engine/runtime/task_workspace.py`
- Create: `packages/framework/graph-engine/graph_engine/resources/structured-input-snapshot-policy-v1.json`
- Modify: `packages/framework/graph-engine/tests/artifacts/test_baseline.py`
- Create: `packages/framework/graph-engine/tests/artifacts/test_materializer_install.py`
- Create: `packages/framework/graph-engine/tests/artifacts/test_raw_postimage_capture.py`
- Modify: `packages/framework/graph-engine/tests/attempts/test_snapshot_admission.py`
- Modify: `packages/framework/graph-engine/tests/persistence/test_attempt_artifact_store.py`
- Modify: `packages/framework/graph-engine/tests/runtime/test_task_workspace.py`
- Modify: `packages/framework/graph-engine/tests/runtime/test_task_workspace_faults.py`
- Create: `packages/framework/graph-engine/tests/runtime/test_task_workspace_creation_recovery.py`
- Create: `packages/framework/graph-engine/tests/runtime/test_task_workspace_identity_v2.py`
- Create: `packages/framework/graph-engine/tests/runtime/test_input_snapshot_policy.py`
- Modify: `packages/framework/graph-engine/tests/runtime/test_scheduler_workspace_identity.py`
- Modify: `packages/framework/graph-engine/tests/runtime/test_activity_models.py`
- Modify: `packages/framework/graph-engine/tests/runtime/test_staged_promotion_recovery.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_plugin_contracts.py`

**Interfaces:**

- Consumes: `BoundArtifactContract`, the durable/reopenable `TaskWorkspaceBinding`, an already journal-anchored `MaterializationManifest`, durable `materialized_bytes` blob refs, `AttemptArtifactStorePort`, current fence, and Task 3 receipt builders.
- Produces: artifact-agnostic `WorkspaceScanBudget`, bounded immutable `AttemptInputSnapshotManifest`/`AttemptInputSnapshotRef`, crash-reconcilable workspace creation/baseline/input capture, `TypedArtifactWorkspacePort.install_or_verify_typed(...)`, `TypedArtifactWorkspacePort.read_verified_typed(...)`, immutable `RawMemberSelectionReceipt`/`RawPostImageManifest`/streaming capture, blob-backed `ReadOnlyRawArtifactPort`, `validate_raw_mutations(...)`, `SealedAttemptWrites(promotable, internal, digest)`, `ArtifactMaterializer.install_materialization(...) -> MaterializationReceipt`, and `TaskWorkspaceStore.reopen_or_freeze_promotion_sources(...)` for immutable promotion refs.

- [ ] **Step 1: Write idempotent install RED tests.**

```python
async def test_install_materialization_is_atomic_per_file_and_idempotent() -> None:
    first = await materializer.install_materialization(
        manifest=anchored_manifest,
        byte_refs=durable_byte_refs,
        blob_store=blob_store,
        workspace=workspace,
        binding=binding,
        fencing_token=7,
    )
    replay = await materializer.install_materialization(
        manifest=anchored_manifest,
        byte_refs=durable_byte_refs,
        blob_store=blob_store,
        workspace=workspace,
        binding=binding,
        fencing_token=7,
    )
    assert replay == first
    assert tuple(read_stage(e.logical_path) for e in first.entries) == tuple(
        await blob_store.read_verified(durable_byte_refs[e.byte_digest]) for e in first.entries
    )
```

Assert the returned `MaterializationReceipt` is sorted and binds the manifest, prepared/result, schema, codec, serializer, mutation, repair strategy, baseline existence/digest/mode, document, byte, size, and resolved-mode facts from every entry.

- [ ] **Step 2: Write partial-install and drift RED tests.**

Inject a failure after installing the first of three entries. On replay, verify the first exact file and install the remaining two; do not delete/rewrite unrelated raw staging. Add a table-driven mutation/mode matrix: create succeeds only over authenticated absence; replace succeeds only over the exact present baseline; bounded repair requires the exact bound target set and one complete post-image per target; preserve mode equals the authenticated prior mode; fixed mode equals the contract value. For raw outputs, validate each sealed exact path or tree member against the captured baseline using the same mutation and mode rules: raw fixed mode rejects a different `after_mode`; raw preserve rejects a mode different from the exact baseline entry. Reject baseline content/size/mode/existence drift, an existing typed staging target with different bytes/size/mode, symlink/hardlink substitution, traversal, byte blob mismatch, manifest/ref mismatch, stale fence, raw-authority target, or exact-file/root confusion. No failure path returns a completed receipt. The low-level materializer authenticates the manifest value but cannot infer journal durability; Task 9's Kernel trace test proves that no call reaches this port until the matching durable `MaterializationPrepared` event is loaded under the active fence.

- [ ] **Step 3: Run RED and confirm install methods are absent.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/artifacts/test_materializer_install.py \
  packages/framework/graph-engine/tests/artifacts/test_raw_postimage_capture.py \
  packages/framework/graph-engine/tests/artifacts/test_baseline.py \
  packages/framework/graph-engine/tests/attempts/test_snapshot_admission.py \
  packages/framework/graph-engine/tests/persistence/test_attempt_artifact_store.py \
  packages/framework/graph-engine/tests/runtime/test_task_workspace.py \
  packages/framework/graph-engine/tests/runtime/test_task_workspace_faults.py \
  packages/framework/graph-engine/tests/runtime/test_task_workspace_creation_recovery.py \
  packages/framework/graph-engine/tests/runtime/test_input_snapshot_policy.py
```

Expected: tests fail because concrete baseline projection/mutation enforcement, immutable raw post-image capture, and the typed workspace install methods do not exist.

- [ ] **Step 4: Capture one descriptor-safe durable baseline and expose typed-install primitives.**

Keep `TaskWorkspaceStore` artifact-agnostic. Add portable `WorkspaceScanClaim`/`WorkspaceScanBudget` and v2 identity fields in `plugin_api.py`. Freeze one pure `build_workspace_scan_budget(bound_contract)` conversion. After the binder has already rejected overlaps, each nonrepeatable exact slot becomes one exact claim with `max_files=1`, `max_total_bytes=max_file_bytes=slot.max_bytes`; a repeatable exact-file slot becomes one sorted finite-set claim with `max_files=len(bound_paths) <= slot.max_items`, `max_total_bytes=max_file_bytes=slot.max_bytes`; a tree-root slot becomes one root claim with `max_files=slot.max_items`, `max_total_bytes=max_file_bytes=slot.max_bytes`, installed `max_entries_per_root=8192`, and installed `max_depth=64`. When any explicit non-artifact write/exclusive claim exists, add one shared `workspace-internal-v1` claim over that exact union with fixed ceilings `max_files=4096`, `max_entries=8192`, `max_total_bytes=512 MiB`, `max_file_bytes=512 MiB`, and `max_depth=64`; it meters scratch without making it promotable. Multiple disjoint claims combine with checked integer addition for file/byte/entry totals and `max()` for per-file size/depth; overflow or the installed aggregate `max_entries=65536` ceiling fails before `begin(...)`. The canonical budget stores the sorted exact/set/root/internal claims, aggregate values, `scan_policy_id="workspace-scan-v1"`, and `budget_digest`; no target is silently deduplicated. The exact applied algorithm/version/projection and digest enter `TaskWorkspaceIdentity.identity_digest` and `TaskWorkspaceBinding.binding_digest`. Any characterization that exceeds an installed ceiling requires a versioned policy/plan change. Runtime code knows only canonical paths and numeric hard bounds, not Artifact types. Its dirfd/no-follow scan streams hashes in fixed chunks, counts directory entries before allocating or descending (including empty directories), and aborts as soon as any file/entry/aggregate/file-size/depth bound would be exceeded. It never recursively reads an unbounded tree and publishes no identity from a partial scan.

Freeze the input snapshot values:

```python
class AttemptInputSnapshotEntry(FrozenModel):
    logical_path: str
    content_digest: str
    byte_size: int
    mode: int
    entry_digest: str


class AttemptInputSnapshotManifest(FrozenModel):
    attempt_key: str
    resource_claims_digest: str
    policy_id: Literal["attempt-input-snapshot-v1"]
    budget_digest: str
    entries: tuple[AttemptInputSnapshotEntry, ...]
    input_snapshot_digest: str


class AttemptInputSnapshotRef(FrozenModel):
    manifest: AttemptInputSnapshotManifest
    input_root_key: str
    ref_digest: str
```

Entries are sorted by logical path; `input_root_key` is a canonical runtime-relative opaque key and no host absolute path enters either projection. Empty `reads` has one canonical empty manifest/ref with non-null digests. Test every field, duplicate/overlap, cross-Attempt/ref, path/digest/size/mode drift, and empty round-trip.

In the same `begin(...)` publication, capture an immutable read view from exactly `dispatch.resources.reads`. Treat each canonical read prefix as a file-or-root claim; descriptor-scan the canonical project with no-follow/regular-file checks into a runtime-owned `control/input/` tree and the manifest above. The installed, versioned `structured-input-snapshot-policy-v1` denies `.git` and runtime-control ancestry, dot-env variants, credential/key/token files, SSH/GPG/cloud credential roots, sockets/devices/FIFOs, and any path/media/type that is not explicitly permitted; exact Product-installed allow overrides are data-only, digest-bound, and may never come from `.aa`, the SUT, a skill, prompt, or Agent. Root claims are expanded only under that deny policy, so a broad authorized source root cannot accidentally disclose a secret. The policy caps each prefix at 8,192 entries/depth 64 and the combined snapshot at 65,536 entries, 1 GiB total, and 512 MiB per file; checked overlap is deduplicated only by exact file identity and conflicting bytes fail. Missing claimed input, deny-policy match, symlink/hardlink/device, mutation during capture, forbidden ancestry, or any bound fails before activity. The complete input tree/manifest is fsynced and atomically published with workspace identity; `TaskWorkspaceIdentity` v2 explicitly includes `input_snapshot_digest`, `input_snapshot_ref_digest`, input policy ID/digest, deny/allow rule digest, and input budget digest, and `TaskWorkspaceBinding` carries the authenticated ref. ProductLock and resolved toolchain/workspace binding authenticate the same policy digest. Restart reopens and rehashes it and never rereads canonical project to build a replacement. No separate journal event is needed because that identity/binding is already the durable `begin_workspace` anchor.

OpenCode native read operations and the command sandbox receive only a read-only descriptor/mount for this immutable snapshot plus declared raw output/scratch, never the live canonical project. `StructuredWorkspaceAccess` and workspace binding v2 carry `input_snapshot_digest`, input-policy digest, sandbox/toolchain policy and qualification digests, the post-registration `structured_command_broker_binding_digest` **and distinct `broker_registration_receipt_digest`**, nullable `structured_command_secret_set_digest`, and the nullable pre-key `structured_network_access_digest` plus backend/policy/qualification digests, not a host path, raw endpoint, secret handle mapping, or value. Add every sensitive-path/type denial, installed-override provenance, source mutation during capture, over-budget, restart, tampered snapshot, native-read equality, composed-command-view equality, sandbox read-only mount, network-none, broker-registration/adoption/fake-sink, and bound-network equality tests; after Agent activity binding, recovery always reuses the same snapshot and network access.

Introduce `TaskWorkspaceIdentity.layout_schema_version="2"` for structured Attempts. V2 binds sorted `exact_output_paths`, `output_roots`, `baseline_absent_exact_paths`, the complete baseline file set, applied `WorkspaceScanBudget` and digest, plus `input_snapshot_digest`, `input_snapshot_ref_digest`, `input_snapshot_policy_id`, input budget digest, nullable `structured_command_secret_set_digest`, and nullable-together `structured_network_access_digest`, network-backend digest, network-policy digest, and network-qualification digest copied from the pre-key dispatch/Product binding. The durable v2 workspace-access envelope, written only after `register_or_adopt(...)`, additionally binds `structured_command_broker_binding_digest`, sandbox policy/qualification digests, stable activity reference, and registration receipt digest without changing the preexisting `workspace_identity_digest`; recovery requires exact equality and never allocates a second command domain. Extend `StagedFile` with optional `before_size`/`after_size`; v2 baseline and sealed post-image entries require size beside each present digest/mode pair and authenticate it in their canonical projection. Existing persisted v1 identities remain reopenable only by the legacy path, keep their old canonical digest, and are never upgraded in place; a structured Attempt rejects v1. Add v1/v2 coexistence, network-none/network-bound, drift, and round-trip tests in `test_plugin_contracts.py` and `test_scheduler_workspace_identity.py`.

On first creation the runtime captures all existing exact targets and authorized raw-root descendants before prepare/activity and returns a `TaskWorkspaceBinding` whose v2 identity authenticates the complete bounded tree, file sizes, and absence within exact claims. Immediately after `begin(...)`, core artifact code calls pure `baseline_from_workspace_binding(binding, bound_contract)`; that projection requires complete claim/slot coverage and exact agreement with the same limits, then produces the canonical `ArtifactBaselineSnapshot` without filesystem I/O. Do not import `graph_engine.artifacts` from `runtime.task_workspace`. A new workspace is not eligible to continue when projection finds create-over-existing, replace/repair-over-missing, preserve-over-missing, over-limit subtree, size/digest disagreement, or contract/claim mismatch; the scan itself rejects symlink, non-regular file, multi-link file, or descriptor race.

Make workspace creation publish-atomic and crash-reconcilable with an explicit v2 container layout: a runtime-owned parent contains authenticated `control/identity.json` and the declared `write/` subtree. OpenCode native writes receive only brokered `write/` operations, while every executor command runs in OpenCode Task 6's separate UID/container/mount sandbox with only explicit input/scratch/raw-output mounts; the parent/control, canonical project, typed staging, blob store, and Attempt journal are not mounted. POSIX owner modes or undisclosed paths alone are not accepted as isolation. Build the complete parent and authenticated identity/baseline metadata under a fence-owned temporary sibling, fsync files/directories, then atomically publish the parent. A crash before publish leaves only a name containing the exact Attempt key and fence; replay under the current fence validates it contains no unowned path, either completes the identical publish or removes only that runtime-owned temporary parent, and never mistakes it for an adopted workspace. A crash after atomic publish reopens the exact identity. Add named faults before identity write, after identity fsync, before parent publish, and after parent publish/before return; each replays the same stable Attempt key without permanent orphan rejection. Add a malicious indirect-command test for relative/absolute traversal and inherited descriptors; the sandbox must not reach control/blob/journal/canonical/typed paths, and every later host read still rehashes its descriptor against durable refs. Stale-fence reconciliation cannot publish/delete the new owner's state. Legacy v1 keeps its existing sibling layout and is never exposed as a v2 binding.

Reopening never captures a replacement snapshot, but project verification is phase-aware. Before durable promotion starts, `verify_original_baseline(binding)` must prove the canonical project still equals the recorded pre-Attempt baseline before prepare/activity and again at the existing promotion compare-and-swap boundary. After a durable promotion-prepared/promotion receipt exists, recovery must not demand the old project bytes: it delegates to the existing promotion reconciler, which accepts only the receipt-authenticated old image, exact promoted post-image, or its already specified indeterminate recovery state. Effect-tail/terminal recovery authenticates the promoted receipt/post-image and retains the original baseline only as historical evidence. Add a dependency-direction test proving this phase selection lives in Kernel/promotion recovery, not in artifact-agnostic `TaskWorkspaceStore.begin(...)`.

Add race tests that mutate existence/content/size/mode or swap an ancestor after the descriptor opens but before scan completion; no snapshot or workspace binding may be returned. Add early-abort tests with too many files, too many empty directories/entries, one oversized file, aggregate overflow, and excessive depth; instrument entries and bytes read and prove the scan stops at the first violating entry/chunk/bound and publishes no identity. Every explicit internal/scratch write claim is charged against the same installed aggregate `max_files`/`max_entries`/`max_total_bytes` ceilings even though it is excluded from the Artifact baseline and promotion; the post-Agent scan may never traverse an unmetered internal subtree. Add restart tests proving the authenticated applied budget and `baseline_from_workspace_binding(...)` are byte-identical and no second baseline capture is selected, plus a phase table: pre-promotion external drift fails; crash after promotion reopens against the exact receipt-authenticated post-image; crash during effect settlement never tries to restore or compare against the old baseline. This is the production implementation ownership that Task 1's data types intentionally defer to Task 8; Product and `AttemptNodeFactory` inject only the existing `TaskWorkspaceStore`, not a second preflight plugin.

Define the narrow port in `artifacts/materializer.py` and make the target workspace provider implement it:

```python
class TypedArtifactWorkspacePort(Protocol):
    async def install_or_verify_typed(
        self,
        *,
        binding: TaskWorkspaceBinding,
        logical_path: str,
        content: bytes,
        mode: int,
        fencing_token: int,
    ) -> None: ...

    async def read_verified_typed(
        self,
        *,
        binding: TaskWorkspaceBinding,
        logical_path: str,
        fencing_token: int,
    ) -> tuple[bytes, int]: ...
```

Reuse the workspace module's descriptor-pinned containment, no-symlink, regular-file identity, atomic temporary-write/replace, fsync, and fence checks. Do not import or call its private `_atomic_write_at` from `artifacts`; the public workspace adapter owns that implementation. An existing exact typed file is an idempotent success only after bytes and mode verify.

- [ ] **Step 5: Implement ordered installation and receipt construction.**

`install_materialization(...)` authenticates manifest/ref set equality and manifest/baseline/workspace-binding equality, reads every payload from the durable store, rechecks each entry's mutation/baseline rule, installs/verifies entries in manifest order, then reads all entries back and verifies digest/size/mode again. Only after the complete set matches does it call `MaterializationEntryReceipt.build(...)` for each entry and `MaterializationReceipt.build(...)` once. Empty manifests return an authenticated empty receipt that still binds the baseline/workspace identity and perform zero typed workspace calls.

The method never scans, clears, rewrites, or claims raw paths. It receives no projector/model/serializer callable, so recovery from an anchored manifest cannot select a new implementation.

- [ ] **Step 6: Capture an immutable raw post-image, expose its read-only view, then partition sealed writes.**

Freeze the durable values before implementing capture:

```python
class RawMemberSelectionReceipt(FrozenModel):
    slot_id: str
    selector_id: str
    selector_provenance_digest: str
    declaration_digest: str
    declared_paths: tuple[str, ...]
    receipt_digest: str


class RawPostImageEntry(FrozenModel):
    slot_id: str
    logical_path: str
    blob_ref: AttemptArtifactBlobRef
    content_digest: str
    byte_size: int
    mode: int
    entry_digest: str


class RawPostImageManifest(FrozenModel):
    attempt_key: str
    workspace_identity_digest: str
    artifact_contract_digest: str
    bound_contract_digest: str
    structured_result_digest: str
    selection_receipts: tuple[RawMemberSelectionReceipt, ...]
    entries: tuple[RawPostImageEntry, ...]
    member_set_digest: str
    manifest_digest: str
```

Selection receipts are sorted by bound raw-slot order; entries are sorted by `(slot_id, logical_path)`. Each declaration digest binds the complete ordered set returned by that slot's selector. `member_set_digest` binds all selection receipts and entry identities; `manifest_digest` authenticates the entire value. A typed-only Attempt has the canonical empty selection/entry tuples and their deterministic non-null digests. Tests mutate every selector/provenance/declaration/path/blob/content/size/mode/entry/member/manifest field, reject duplicate or cross-slot paths and selection-without-entry/entry-without-selection, and prove the canonical empty value round-trips.

The provider-neutral raw-write admission closure is owned by OpenCode Task 6 and consumed by Kernel Task 9; this task does not pretend a Python marker can revoke filesystem syscalls from a previously detached process. After Kernel has durable `AgentWritesClosed` and `StructuredResultPersisted`, run each bound raw slot's authenticated `ArtifactRawMemberSelectorEntry` exactly once over validated `RawMemberSelectionInput`. Validate sorted unique declarations, exact `slot_id`, required exact-file equality, root containment, typed/internal deny holes, declared empty-tree intent, per-slot and aggregate budgets, and selector provenance before any scan. Then call `capture_raw_postimage(...)` with that closed member set. It descriptor-scans only those result-authenticated members under the same `WorkspaceScanBudget`, stats before/after each chunked read, streams bytes through incremental snapshot admission into an Attempt-private temporary blob, rejects any size/mode/digest/member change during capture, atomically finalizes `raw_artifact_bytes` refs, builds one canonical `RawPostImageManifest`, persists it as `raw_postimage_manifest`, ensures every ref durable, and only then appends `RawPostImagePersisted`. The manifest records `slot_id` for every member and exposes only authenticated path/digest/size/mode metadata to later typed projectors. An empty typed-only raw set produces an authenticated empty manifest. A partial capture has no event; replay may reuse equal content-addressed blobs but must rerun the same authenticated selector, require the exact same complete declaration, and never trusts a mutable partial manifest.

Freeze the streaming seam rather than buffering a contract-sized file. `AttemptSnapshotAdmissionPort.begin(attempt_key, kind, max_bytes) -> SnapshotAdmissionStream` returns an ephemeral scanner with `update(chunk)`, parameterless `finish() -> SnapshotAdmissionReceipt`, and `abort()`; the existing `admit(..., content)` convenience delegates to it. The admission stream independently computes byte count and content digest while carrying an overlap window of `max_encoded_canary_length - 1`, so direct/base64/hex/URL-safe canaries split across arbitrary chunk boundaries are still rejected, and it never emits secret bytes. `AttemptArtifactStorePort.put_stream_if_absent(...)` accepts a bounded chunk iterator plus that stream, independently hashes/counts the same chunks into a fence/Attempt-owned `0600` temporary blob, requires its digest/size to equal the scanner's finished receipt, fsyncs file/parents, then atomically publishes the content address. Neither caller may supply the claimed final digest/size. Oversize, admission rejection, source mutation, cross-computation mismatch, stale fence, or crash removes/reconciles only the owned temp and publishes no ref. Same content replay returns the same ref. Add byte-at-a-time canary, chunk-boundary, forged/mismatched scanner receipt, early-overflow, crash-before/after-fsync, stale-fence cleanup, restart, and 512 MiB simulated-stream tests without allocating the whole payload.

Expose `ReadOnlyRawArtifactPort` exclusively over the durable raw manifest/blob refs, not the workspace. After deterministic finalization returns, seal through one more bounded descriptor-safe scan and call `partition_sealed_writes(binding, bound_contract, raw_postimage, sealed) -> SealedAttemptWrites`. Set and byte/size/mode equality with the immutable raw manifest are mandatory; typed receipt paths plus authenticated raw snapshot entries form `promotable`, explicit non-artifact write/scratch claims form `internal`, and anything else or any late background mutation fails. An internal path cannot masquerade as raw/typed by prefix overlap, is excluded from the finalizer/validator/promoter views, and is deleted only by ordinary Attempt-private workspace cleanup. The aggregate partition digest binds both sets. Each promotable entry carries its immutable typed/raw blob source ref; validators and durable prepare see the sealed projection, while promotion installs from those immutable refs rather than reopening mutable staging. Thus a write after capture can only reject the Attempt and can never alter finalizer input or promoted bytes. Add a delayed/background shell-writer E2E, a positive legal-scratch case, and typed/raw/internal alias and unknown-path negatives.

Freeze the promotion values explicitly:

```python
class FrozenPromotionMember(FrozenModel):
    logical_path: str
    source_ref: AttemptArtifactBlobRef
    content_digest: str
    byte_size: int
    mode: int
    member_digest: str


class FrozenPromotionSet(FrozenModel):
    attempt_key: str
    workspace_identity_digest: str
    sealed_partition_digest: str
    members: tuple[FrozenPromotionMember, ...]
    locator_digest: str
    set_digest: str
```

Members are sorted by logical path and source refs must be exact typed materialization or raw-post-image refs for the same Attempt. `locator_digest` binds only canonical Attempt/workspace/partition identity; no host absolute path enters a projection. Test duplicate paths/refs, cross-Attempt refs, wrong source kind, missing/extra member, digest/size/mode drift, wrong locator/fence, tampered published bytes, canonical empty set, and set-digest drift.

Make that last sentence executable through an artifact-agnostic v2 promotion adapter. Add `TaskWorkspaceStore.reopen_or_freeze_promotion_sources(binding, sealed, blob_reader, fencing_token) -> FrozenPromotionSet`: locate the single set by the canonical locator above; if absent, stream each authenticated typed/raw source ref into a fence-owned temporary promotion root, verify source/path/digest/size/mode, fsync, and atomically publish the set; if present, reopen and authenticate exact set/member/ref equality. The OS command sandbox required by OpenCode Task 6 never mounts the workspace parent/control or Attempt blob/journal roots; nevertheless correctness does not trust path concealment or same-UID mode bits. Every identity/blob/closure/set descriptor is re-read and rehashed against an independently durable Attempt journal/ref immediately before finalizer use, durable prepare, and promotion. A malicious detached command attempting `../control`, blob, marker, journal, or frozen-set mutation must be unable to reach it in the sandbox; any host-side byte drift is an integrity failure, never accepted data. `FrozenPromotionSet` and the durable prepare/promotion receipt bind every source content digest plus the set digest. Structured v2 promotion requires this set and reads only its authenticated descriptors; legacy v1 promotion retains the existing staged path. Crash before/after source copy or set publish reconciles idempotently, target old-image CAS is unchanged, and no process holding an FD in `write/` can affect promoted bytes.

The blob-backed read-only raw view's interface is exactly `ReadOnlyRawArtifactPort.read_bytes(logical_path)`. It authorizes exact equality with manifest members that were already proven equal to `bound.raw_paths` or descendants of `bound.raw_roots`, with typed/internal deny holes applied first; it rejects every other path and has no write or host-path property. `validate_raw_mutations(bound_contract, baseline_snapshot, raw_postimage, promotable_raw_set)` checks every concrete raw exact file, repeatable exact-set member, or root descendant: create requires absence and fixed `after_mode`; replace requires one exact baseline entry and its declared fixed/preserve mode; bounded repair requires an existing baseline, `agent_raw_whole_file`, one complete immutable whole-file post-image at the same path, and exact mode-policy agreement. Required nonrepeatable exact slots and every bound repeatable exact member must have one post-image; omission fails. Tree-root output cardinality is closed by its authenticated raw-member selector plus slot limits, and set equality is required against that declared member list and `RawPostImageManifest`; an empty tree is accepted only when the validated result causes the selector to return an explicit empty set. It never treats raw bytes as a typed patch and rejects missing outputs, extra members, a baseline mutation absent from the snapshot, or an output outside the bound exact/root authority.

- [ ] **Step 7: Run workspace/materializer GREEN checks.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/artifacts/test_materializer_install.py \
  packages/framework/graph-engine/tests/artifacts/test_raw_postimage_capture.py \
  packages/framework/graph-engine/tests/artifacts/test_baseline.py \
  packages/framework/graph-engine/tests/attempts/test_snapshot_admission.py \
  packages/framework/graph-engine/tests/persistence/test_attempt_artifact_store.py \
  packages/framework/graph-engine/tests/runtime/test_task_workspace.py \
  packages/framework/graph-engine/tests/runtime/test_task_workspace_faults.py \
  packages/framework/graph-engine/tests/runtime/test_task_workspace_creation_recovery.py \
  packages/framework/graph-engine/tests/runtime/test_task_workspace_identity_v2.py \
  packages/framework/graph-engine/tests/runtime/test_input_snapshot_policy.py \
  packages/framework/graph-engine/tests/runtime/test_scheduler_workspace_identity.py \
  packages/framework/graph-engine/tests/runtime/test_activity_models.py \
  packages/framework/graph-engine/tests/runtime/test_staged_promotion_recovery.py \
  packages/framework/graph-engine/tests/composition/test_plugin_contracts.py
uv run pyright \
  packages/framework/graph-engine/graph_engine/plugin_api.py \
  packages/framework/graph-engine/graph_engine/persistence/attempt_artifacts.py \
  packages/framework/graph-engine/graph_engine/attempts/snapshot_admission.py \
  packages/framework/graph-engine/graph_engine/artifacts/baseline.py \
  packages/framework/graph-engine/graph_engine/artifacts/materializer.py \
  packages/framework/graph-engine/graph_engine/runtime/task_workspace.py
uv run ruff check \
  packages/framework/graph-engine/graph_engine/plugin_api.py \
  packages/framework/graph-engine/graph_engine/persistence/attempt_artifacts.py \
  packages/framework/graph-engine/graph_engine/attempts/snapshot_admission.py \
  packages/framework/graph-engine/graph_engine/artifacts/baseline.py \
  packages/framework/graph-engine/graph_engine/artifacts/materializer.py \
  packages/framework/graph-engine/tests/artifacts/test_materializer_install.py \
  packages/framework/graph-engine/tests/artifacts/test_raw_postimage_capture.py \
  packages/framework/graph-engine/tests/attempts/test_snapshot_admission.py \
  packages/framework/graph-engine/tests/persistence/test_attempt_artifact_store.py \
  packages/framework/graph-engine/tests/runtime/test_task_workspace_identity_v2.py \
  packages/framework/graph-engine/tests/runtime/test_staged_promotion_recovery.py
```

Expected: all commands exit `0`; baseline capture is bounded, publish-atomic, descriptor-safe, and stable across restart; typed/raw mutation and mode rules are enforced; internal writes cannot promote; partial replay is idempotent; and unrelated raw staging remains untouched.

- [ ] **Step 8: Commit typed installation.**

```bash
git add \
  packages/framework/graph-engine/graph_engine/plugin_api.py \
  packages/framework/graph-engine/graph_engine/runtime/__init__.py \
  packages/framework/graph-engine/graph_engine/persistence/attempt_artifacts.py \
  packages/framework/graph-engine/graph_engine/attempts/snapshot_admission.py \
  packages/framework/graph-engine/graph_engine/artifacts/baseline.py \
  packages/framework/graph-engine/graph_engine/artifacts/materializer.py \
  packages/framework/graph-engine/graph_engine/runtime/task_workspace.py \
  packages/framework/graph-engine/graph_engine/resources/structured-input-snapshot-policy-v1.json \
  packages/framework/graph-engine/tests/artifacts/test_baseline.py \
  packages/framework/graph-engine/tests/artifacts/test_materializer_install.py \
  packages/framework/graph-engine/tests/artifacts/test_raw_postimage_capture.py \
  packages/framework/graph-engine/tests/attempts/test_snapshot_admission.py \
  packages/framework/graph-engine/tests/persistence/test_attempt_artifact_store.py \
  packages/framework/graph-engine/tests/runtime/test_task_workspace.py \
  packages/framework/graph-engine/tests/runtime/test_task_workspace_faults.py \
  packages/framework/graph-engine/tests/runtime/test_task_workspace_creation_recovery.py \
  packages/framework/graph-engine/tests/runtime/test_task_workspace_identity_v2.py \
  packages/framework/graph-engine/tests/runtime/test_input_snapshot_policy.py \
  packages/framework/graph-engine/tests/runtime/test_scheduler_workspace_identity.py \
  packages/framework/graph-engine/tests/runtime/test_activity_models.py \
  packages/framework/graph-engine/tests/runtime/test_staged_promotion_recovery.py \
  packages/framework/graph-engine/tests/composition/test_plugin_contracts.py
git commit -m "feat: install typed artifacts idempotently"
```

### Task 9: Integrate the structured artifact phases into `AssuranceAttemptKernel`

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/attempts/kernel.py`
- Modify: `packages/framework/graph-engine/graph_engine/attempts/events.py`
- Modify: `packages/framework/graph-engine/graph_engine/attempts/context.py`
- Modify: `packages/framework/graph-engine/graph_engine/attempts/resolutions.py`
- Modify: `packages/framework/graph-engine/graph_engine/plugin_api.py`
- Modify: `packages/framework/graph-engine/graph_engine/runtime/task_workspace.py`
- Create: `packages/framework/graph-engine/tests/attempts/test_kernel_structured_artifacts.py`
- Modify: `packages/framework/graph-engine/tests/attempts/test_kernel.py`
- Modify: `packages/framework/graph-engine/tests/attempts/test_validator_context.py`
- Modify: `packages/framework/graph-engine/tests/persistence/test_attempt_journal.py`

**Interfaces:**

- Consumes: base `AssuranceAttemptKernel.execute_or_recover(...)`, authenticated `BoundAttemptDispatch`, the closed executor variants, Attempt artifact store/journal phases, the durable workspace-derived `ArtifactBaselineSnapshot`, `ArtifactMaterializer`, workspace installer/read-only views, existing seal/validator/prepare/promote/effect ports, and current fence.
- Produces: the normative 25-step structured happy path, Kernel-local model validation and recovery boundaries, durable raw-write closure plus immutable raw post-image, partitioned promotable/internal seals, `ValidationContext.materialization_receipt`, and terminal Attempt receipts that bind raw/materialization/partition evidence without changing graph-visible resolutions.

- [ ] **Step 1: Write the exact structured happy-path RED test.**

```python
async def test_structured_agent_attempt_runs_the_exact_internal_trace() -> None:
    result = await kernel.execute_or_recover(
        attempt_key=attempt_key,
        contract=resolved_structured_contract,
        dispatch=bound_dispatch,
        context=context,
    )

    assert trace == [
        "adopt_or_create",
        "authorize_resources",
        "begin_workspace",
        "prepare_or_recover",
        "validate_prepared",
        "persist_prepared",
        "dispatch_or_adopt_agent_activity",
        "observe_structured_agent_result",
        "close_agent_raw_writes",
        "validate_agent_result",
        "persist_structured_result",
        "capture_raw_postimage",
        "persist_raw_postimage",
        "prepare_materialization",
        "persist_materialization_manifest",
        "install_typed_artifacts",
        "persist_materialization_receipt",
        "deterministic_finalize",
        "validate_output",
        "seal_candidate",
        "run_validators",
        "durable_prepare",
        "promote_or_recover",
        "settle_effects",
        "publish_receipt",
    ]
    assert isinstance(result, CommittedTaskResult)
    assert trace.index("authorize_resources") < trace.index("begin_workspace")
    assert workspace.baseline_capture_count == 1
    assert workspace.baseline_capture_finished_before_activity
```

Assert `dispatch_or_adopt_activity(...)` receives a `StructuredAgentActivityRequest` whose `result_schema.schema_id`, schema document, digest, `max_schema_bytes`, and `max_result_bytes` are the exact frozen values from the resolved executor. Make the fake return `StructuredActivityBound`, then `StructuredActivitySucceeded`; assert `PreparedT` was validated only from pure `prepare(InputT)`/recovery. Before model validation, encode the untrusted candidate with the bounded canonical JSON encoder and reject it above `max_result_bytes`; after `agent_result_model.model_validate(...)`, canonicalize the validated model and enforce the same bound again. Add oversized fake-port candidates, normalization-expansion, and oversized replay-blob negatives so adapter checks cannot satisfy the Kernel invariant. Require a durable matching `StructuredRawWriteClosure`, including the invocation-scope and both broker digests, canonical command transcript, command-secret-injection aggregate, and nullable network transcript, before candidate validation, typed install, finalization, or seal.

Before `adopt_or_create`, authenticate all `BoundAttemptDispatch` digests, recompute `business_activation_digest`, require the key's semantic occurrence/full BusinessActivation plus input/resource/toolchain profile/invocation-scope/network/command-secret/authorized-handle/bound-artifact digest fields to equal the dispatch, and require the dispatch's bound artifact contract to match the resolved executor's authenticated artifact contract. Recompute and require `dispatch.authorized_secret_handles` to equal the sorted union of `executor.activity_secret_handles` and the selected `dispatch.structured_command_secrets` handle IDs; the executor's complete command-secret binding catalog is never mistaken for that selected set. `authorize_resources` receives exactly `dispatch.resources`, opens one `AttemptSecretLifetime` with that authenticated union/digest, the adapter-only activity handles, and the selected value-free command rows, validates selected command values for byte bound/strict UTF-8/no-NUL, and anchors/adopts the exact port-produced `SecretGenerationSetIdentity` before returning. After v2 workspace binding, Kernel calls provider-neutral `context.command_broker_registration.register_or_adopt(...)` with the stable activity reference, full bound invocation scope, workspace/fence/access/target/secret/generation request and the same lifetime's admission/injection ports. The Product implementation returns the same opaque broker binding/scope identity on replay and keeps injection capability/sink tokens outside adapter data. Only then does Kernel construct minimal `StructuredActivityCallContext(attempt_key_digest, activity_reference_digest, active_fence, lifetime.activity_secrets, lifetime.secret_generation_identity.generation_set_digest, broker_session.broker_binding_digest)` for dispatch/observe/close. Kernel never gives `AttemptExecutionContext`, snapshot admission, injection, broker registration, journal, workspace, effect, resource, or promotion ports to adapter code; attribute-enumeration and malicious-adapter/fake-sink tests require zero admission/injection/value-resolution calls. The broker uses its registered admission port for durable payload screening and registered injection port for one-shot sandbox injection. No capability/value reaches prompt/model, prepare/finalize/Feature code, canonical request/event data, or adapter-visible protocol. Before returning a pending/running resolution or closing the broker/secret lifetime, Kernel requires the observation's durable tool-production-quiesced receipt; recovery reopens the same full generation identity/broker binding/scope and reattaches the same activity reference before queued tool admission. Unproven quiescence/reattachment is indeterminate, never a reason to reprompt or launch a second command.

The receipt above is the provider-neutral `StructuredActivityToolProductionQuiesced` value from OpenCode Task 1. Kernel authenticates its Attempt/activity/broker/workspace/generation identity, closed-admission generation, and allocated/terminal/queued ordinal watermarks before a running/pending result may escape. `StructuredActivityDispatchPending` without that receipt is legal only when its signed state proves the remote request was never accepted and no tool namespace or ordinal can exist. If acceptance may have occurred—including request accepted followed by response loss—dispatch must recover the stable activity reference and return/adopt the same quiescence receipt; otherwise the resolution is indeterminate and Kernel must not close the broker/secret lifetime under a false “not started” assumption.

Workspace creation and the activity request receive exactly `dispatch.artifacts`, the immutable input snapshot, bound toolchain invocation scope, value-free bound command-secret set, bound network access, and the same generation identity. When constructing `StructuredWorkspaceAccess`, Kernel copies `input_snapshot_digest` and `input_snapshot_policy_digest` from the authenticated v2 workspace identity, `structured_toolchain_profile_digest`, `structured_toolchain_invocation_scope_digest`, nullable `structured_command_secret_set_digest`, and nullable `structured_network_access_digest` from the bound dispatch, sandbox/network backend/policy/qualification digests from the authenticated Product runtime binding/workspace identity, and both `structured_command_broker_binding_digest` and `broker_registration_receipt_digest` from the Kernel-owned `BoundStructuredCommandBrokerSession`; it copies the already bound raw/typed path tuples unchanged. Kernel passes the full authenticated `BoundStructuredToolchainInvocationScope` directly in the broker registration request/session; the adapter and model receive only its digest and the broker never parses Feature `InputT`. Missing, nullable-rule violation, or independent drift of either broker digest or the invocation scope between request, workspace binding, dispatch/key, ProductLock/profile/catalog, plugin, gateway, or broker fails before activity/tool registration. Kernel never resolves a target/DNS row or accepts a URL from activity output. Reject a mismatch or unprovable generation before workspace, blob, journal-phase mutation, or external activity, and close the lifetime in `finally`. During `begin_workspace`, create or reopen the one durable `TaskWorkspaceBinding`, derive/authenticate `ArtifactBaselineSnapshot`, and enforce contract mutation/mode prerequisites before prepare or activity dispatch. The Kernel has no call to `ResolvedArtifactContract.bind(...)`, `bind_artifact_paths(...)`, a `ResourceClaimTemplate`, path resolver, network target resolver, or DNS resolver, and it never accepts a new baseline or network identity for an already-open Attempt.

Add tests that the exact lifetime admission object reaches only Kernel's Product broker registration, never any of the three adapter activity calls; that the registered broker uses it for command payload/output persistence and rejects a changed generation; and that it is absent from every adapter-visible/canonical request/context projection, journal event, log, Feature handler, prepare input, and finalizer input. Parameterize each workspace/profile/sandbox/network access/backend/policy/qualification digest independently and require failure before activity dispatch or tool registration.

- [ ] **Step 2: Run the one test and record the old-trace failure.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/attempts/test_kernel_structured_artifacts.py::test_structured_agent_attempt_runs_the_exact_internal_trace
```

Expected: FAIL because the base Kernel invokes the old opaque executor and has no structured phases.

- [ ] **Step 3: Implement prepared snapshot validation and persistence.**

For `ResolvedStructuredAgentExecutor` only, use `dispatch.task_input` as the already validated `InputT`, `dispatch.artifacts` as the already closed path authority, and the durable workspace binding as the sole baseline source:

1. call pure `prepare(dispatch.task_input)` or reuse the journal's `prepared_ref`; the callable receives no Attempt/workspace context;
2. validate into the installed `PreparedT`;
3. dump JSON mode, encode exact canonical bytes, and calculate `prepared_digest`;
4. reject schema/executor/graph/contract drift against an existing event;
5. call the current fence guard;
6. obtain a content-matched `SnapshotAdmissionReceipt`, then persist and ensure durable the `prepared` blob;
7. CAS-append `PreparedSnapshotPersisted` and ensure journal durability before any activity dispatch.

If the durable event exists, read/verify the exact blob and reconstruct `PreparedT`; do not call prepare. If prepare completed but no event was durable, rerun the deterministic prepare and persist its validated result before dispatch.

- [ ] **Step 4: Implement activity adoption, observation, and structured-result persistence.**

Construct the immutable `StructuredAgentActivityRequest` from the resolved executor, validated prepared snapshot, exact schema document/limits, and bound raw/typed path closure. Call `dispatch_or_adopt_activity(...)` exactly once per bound activity identity and persist/adopt its `StructuredActivityBinding` through the existing activity journal phase. Reduce pending/indeterminate dispatch and running/failed/canceled/pending/indeterminate observations through the existing provider-neutral Attempt resolution policy.

Before reducing either a dispatch-level or observation-level pending result, apply the quiescence contract above. Add the exact request-accepted/response-lost case: dispatch reports pending, a tool call arrives late, recovery adopts the same activity/broker/generation and durable ordinal, and the queued call executes once with no second prompt, namespace, command, or injection. A bare pending with uncertain acceptance or an acknowledgement lost before durable quiescence is indeterminate.

After `StructuredActivitySucceeded`, call the provider-neutral `close_raw_writes(binding, context)`, authenticate its activity/workspace/toolchain-invocation-scope/**broker-binding and broker-registration-receipt**/generation/historical-closing-fence/marker digest, canonical command-transcript digest, mandatory `command_secret_injection_aggregate_digest`, and nullable `structured_network_transcript_digest` against the bound scope/network access. Require exact successful recipe-use cardinality and every allocated command ordinal represented by that transcript to be terminal/imported, admit and persist its canonical bytes as `kind="raw_write_closure"`, ensure the blob durable, and CAS-append/ensure durable `AgentWritesClosed` under the current fence before trusting staging or validating/persisting the result. A network-none Attempt requires `structured_network_transcript_digest is None`; a network-bound Attempt requires the transcript to bind only its frozen targets/limits. The command transcript includes logical recipe/typed-parameter/derived-argv digests plus the ordered aggregate of metadata-only `CommandSecretInjectionReceipt` digests for every allocated ordinal and no value/reusable verifier; Kernel requires the closure's scope and injection aggregate to match the bound dispatch, command-secret-set, workspace, both broker digests, active generation, command receipts, and transcript. Empty secret sets use one specified canonical empty aggregate, not `None`. If fence N wrote the immutable marker and crashed before the event, fence N+1 repeats the idempotent close, authenticates and adopts the same historical closure/transcripts, then appends its ref under fence N+1; fence N cannot mutate or append. If the event already exists, call the same idempotent close to re-authenticate the still-closed boundary and require exact equality with its durable blob/event—this never reopens writes, changes recipe scope, re-resolves targets, or injects a rotated secret. `StructuredRawWriteClosePending` and `StructuredRawWriteCloseIndeterminate` map through the existing pending/indeterminate resolution policy and never dispatch a new activity. Only then bounded-canonicalize the untrusted candidate and enforce `max_result_bytes`, validate it into installed `AgentResultT`, verify its schema/activity/observation digests, canonicalize the validated model, enforce `max_result_bytes` again, run the exact generation-bound snapshot admission port, persist the `agent_result` blob with the returned receipt, and CAS-append `StructuredResultPersisted` before invoking a raw-member selector, projector, or serializer.

If the result event already exists, read/verify and validate its blob without dispatching or observing the activity, but still idempotently authenticate the durable raw-write closure marker against `AgentWritesClosed`. If the activity is bound/terminal but the closure/result events are absent, adopt and re-observe that same activity, then close the same binding; never dispatch a replacement to hide uncertainty. Existing pending/indeterminate exceptions map to existing `PendingTaskResult`/`IndeterminateTaskResult` handling.

- [ ] **Step 4a: Capture and anchor the immutable raw post-image.** After the result event is durable, invoke each authenticated raw-member selector once over validated input/prepared/result values, close the exact per-slot member set against pre-key bound exact/root authority, and call Task 8's bounded streaming `capture_raw_postimage(...)`. Admit/persist every raw byte blob and manifest under the anchored secret generation, ensure all refs durable, then CAS-append/ensure durable `RawPostImagePersisted`. Reject a selector drift, missing required exact member, unlisted root member, internal/typed path, member/size/mode drift during capture, secret-generation/canary admission failure, or budget overflow before projection/materialization/finalization. If the event exists, rerun/authenticate selector identity and declaration equality, then load and authenticate its manifest/blobs; never reread mutable workspace bytes as finalizer input. If capture crashed before the event, recapture/verify the current complete set without redispatch; the boundary must already have killed/reconciled every known sandbox command before close, but the plan does not claim to date an arbitrary out-of-band syscall that completed before the first successful capture. Mutation during capture or after a successful capture is rejected before promotion, and never changes durable finalizer input.

- [ ] **Step 5: Implement materialization manifest anchoring and install.**

Call `prepare_materialization(...)` only when no `MaterializationPrepared` event exists, passing the authenticated `ArtifactBaselineSnapshot` derived from the durable workspace binding and `RawPostImageProjection` derived only from the durable raw manifest. Run snapshot admission independently for every canonical typed-document snapshot, every `materialized_bytes` payload, and the canonical manifest document; persist each with its matching generation-bound receipt, ensure all refs durable, fence, CAS-append `MaterializationPrepared` with both ordered ref sets, and ensure journal durability before the first typed workspace call.

If the event exists, read/verify its manifest, typed-document blobs, and byte blobs; authenticate Attempt, executor, graph revision, artifact contract, bound paths, workspace/baseline identity, mutation/repair strategy, prepared/result/schema, projector, model, codec, serializer/corpus, document object/size, mode, and manifest digests. Revalidate each canonical document snapshot into the installed document model and require its normalized object digest to match the manifest. Do not rerun a projector, serializer, or baseline capture from an anchored manifest.

Install or verify the complete manifest. Admit and persist the canonical `MaterializationReceipt`, ensure it durable, fence, CAS-append `MaterializationCompleted`, and ensure journal durability before finalization. If the completed event exists, load/authenticate its receipt and verify receipt/manifest/staging agreement instead of reinstalling unless a missing/partial target requires idempotent completion.

- [ ] **Step 6: Implement deterministic finalization and final `OutputT` validation.**

Construct `StructuredFinalizeInput` from validated `InputT`, `PreparedT`, `AgentResultT`, typed documents reconstructed by validating the durable `materialized_document` snapshots against the authenticated manifest/model entries, loaded `MaterializationReceipt`, and the blob-backed read-only raw artifact port reconstructed from `RawPostImagePersisted`. Invoke `finalize(finalize_input)` with no `AttemptExecutionContext`; check the fence immediately before and after the async pure call. Validate its untrusted candidate into `OutputT`.

The finalizer receives no write binding, typed installer, activity port, secret port, model/provider object, or host path. It may read only declared raw paths. Do not add a separate optional finalizer-output journal event; a crash before seal reruns the deterministic finalizer from the durable prepared/result/materialization closure.

- [ ] **Step 7: Bind materialization receipts into seal and validation.**

After finalization, seal once and partition through Task 8's `partition_sealed_writes(...)`. Before validators, require every materialized entry's actual promotable file path, byte digest, size, and mode to equal its receipt; require every raw entry to be set/byte/size/mode equal to the durable `RawPostImageManifest`; and run `validate_raw_mutations(...)` against only that immutable raw post-image, the promotable raw partition, and durable baseline snapshot. A typed path missing from seal, a different byte/mode, an extra Kernel-managed typed path, create/replace/repair/mode disagreement, a missing/drifted raw post-image, an undeclared raw mutation, or any internal/unknown path in the promotable partition fails before durable prepare. Validators receive only `SealedAttemptWrites.promotable`. Inside the existing `durable_prepare` trace stage, call `reopen_or_freeze_promotion_sources(...)`, reconcile/persist the exact `FrozenPromotionSet`, then bind its set/member/source-ref digests into the durable commit-prepare event. `promote_or_recover` requires that same set and its receipt installs every typed/raw path from the authenticated frozen control-root descriptors, never mutable staging. Terminal evidence additionally binds raw-post-image, frozen-promotion-set, and aggregate partition digests so internal authenticated scratch cannot disappear from audit or enter the project.

Extend the immutable validator context with:

```python
materialization_receipt: MaterializationReceipt | None
```

Structured Agent Attempts provide the authenticated receipt, including an empty receipt for raw-only contracts; direct Attempts provide `None`. Validator order/rejection behavior is unchanged.

- [ ] **Step 8: Bind the aggregate digest into the terminal Attempt receipt.**

Add `input_snapshot_digest: str | None`, `structured_toolchain_profile_digest: str | None`, `structured_toolchain_invocation_scope_digest: str | None`, `structured_command_secret_set_digest: str | None`, `structured_command_broker_binding_digest: str | None`, `broker_registration_receipt_digest: str | None`, `sandbox_policy_digest: str | None`, `sandbox_qualification_digest: str | None`, `command_secret_injection_aggregate_digest: str | None`, `structured_network_access_digest: str | None`, `structured_network_backend_digest: str | None`, `structured_network_policy_digest: str | None`, `structured_network_qualification_digest: str | None`, `command_transcript_digest: str | None`, `structured_network_transcript_digest: str | None`, `secret_generation_set_digest: str | None`, `raw_postimage_manifest_digest: str | None`, `materialization_receipt_digest: str | None`, `sealed_partition_digest: str | None`, and `frozen_promotion_set_digest: str | None` to the terminal Attempt receipt's canonical projection. Require the broker binding and registration-receipt digests independently—do not assume one recursively commits to the other—plus sandbox binding fields and all other structured pipeline values for every `ResolvedStructuredAgentExecutor`; require the invocation-scope digest whenever the contract has a nonempty toolchain requirement set/profile, including the legal zero-recipe scope, and authenticate it against the key/workspace/broker registration, command payload/view/receipt, and transcript; require it to be `None` only for an empty requirement set. Require `structured_command_secret_set_digest` exactly when aliases were selected, authenticate it against the key/workspace/broker registration/command injection receipts and require the ordered injection-entry aggregate digest, require the four network binding digests plus network transcript exactly when nonempty network access is selected, require all corresponding nullable fields to be `None` otherwise, and require a command transcript for every structured activity even when canonically empty. Forbid every structured-only field for `ResolvedDirectExecutor`, and authenticate them against ProductLock, the bound dispatch/key, durable workspace/resource authorization, broker-registration/command-view/network/secret-injection receipts when present, `AgentWritesClosed`, `RawPostImagePersisted`, `MaterializationCompleted`, final sealed partition, durable commit-prepare, and promotion receipt. Retain existing promotable sealed-write, validation, promotion, and effect receipt fields/order.

Do not add a seventh `AttemptResolution` variant. A structured configuration/model/materialization failure maps through the existing rejected/permanent/pending/indeterminate policy; no incomplete materialization can yield `CommittedTaskResult`.

- [ ] **Step 9: Prove direct and raw-only paths remain closed.**

Add tests that a direct contract retains the original base trace and performs zero baseline/prepare-snapshot/activity/closure/raw-capture/materializer/finalizer calls. Add a raw-only structured contract test that persists/validates its structured Agent result, durably closes write admissions, captures an immutable raw post-image, produces an empty-entry typed manifest/receipt that still binds workspace/baseline identity, validates raw create/replace/whole-file-bounded-repair mutations and fixed/preserve modes, preserves unrelated raw staging, partitions legal scratch away, finalizes from the blob-backed raw view, then promotes only immutable artifact refs. Add a delayed/background writer that mutates staging after capture and prove finalizer bytes remain unchanged, equality verification rejects before promotion, and no redispatch occurs. Add negative raw repair cases for missing baseline, missing/incomplete post-image, extra/out-of-bound target, mode drift, and baseline drift. Add typed create, replace, fixed-mode, preserve-mode, and complete-post-image bounded-repair cases plus negative existence/digest/mode/target-set cases; all fail before promotion and none redispatches an already bound activity.

Add pre-key seam tests that pass a dispatch whose resource digest, bound-artifact digest, or binding digest differs from `AttemptKey` and assert zero journal/workspace/activity calls. In this task's harness, invoke `bind_attempt_dispatch(...)` once before key derivation, then instrument every resolver and assert zero resolver calls in Kernel execution or recovery. Replay with the same bound dispatch reuses the key; changing only a dynamic resolved target creates a different key and a different resource authorization record. The real `AttemptNodeFactory` ordering/count test lands later in Semantic Attempt Task 10, as required by the master interleave.

- [ ] **Step 10: Run the structured Kernel and validator suites.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/attempts/test_kernel_structured_artifacts.py \
  packages/framework/graph-engine/tests/attempts/test_kernel.py \
  packages/framework/graph-engine/tests/attempts/test_validator_context.py \
  packages/framework/graph-engine/tests/persistence/test_attempt_journal.py
uv run pyright packages/framework/graph-engine/graph_engine/attempts
uv run lint-imports
```

Expected: exact structured trace, direct regression, raw-only, seal, validator, receipt, and journal tests all pass.

- [ ] **Step 11: Commit Kernel integration.**

```bash
git add \
  packages/framework/graph-engine/graph_engine/attempts/kernel.py \
  packages/framework/graph-engine/graph_engine/attempts/events.py \
  packages/framework/graph-engine/graph_engine/attempts/context.py \
  packages/framework/graph-engine/graph_engine/attempts/resolutions.py \
  packages/framework/graph-engine/graph_engine/plugin_api.py \
  packages/framework/graph-engine/graph_engine/runtime/task_workspace.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_structured_artifacts.py \
  packages/framework/graph-engine/tests/attempts/test_kernel.py \
  packages/framework/graph-engine/tests/attempts/test_validator_context.py \
  packages/framework/graph-engine/tests/persistence/test_attempt_journal.py
git commit -m "feat: materialize structured Agent Attempts"
```

### Task 10: Close all seven required structured cuts plus raw-closure, fencing, and replay drift cases

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/attempts/kernel.py`
- Modify: `packages/framework/graph-engine/graph_engine/artifacts/materializer.py`
- Modify: `packages/framework/graph-engine/graph_engine/persistence/attempt_artifacts.py`
- Modify: `packages/framework/graph-engine/graph_engine/plugin_api.py`
- Modify: `packages/framework/graph-engine/graph_engine/runtime/task_workspace.py`
- Create: `packages/framework/graph-engine/tests/attempts/test_kernel_artifact_recovery.py`
- Create: `packages/framework/graph-engine/tests/attempts/test_kernel_artifact_fencing.py`
- Create: `packages/framework/graph-engine/tests/attempts/test_kernel_artifact_drift.py`
- Modify: `packages/framework/graph-engine/tests/attempts/test_kernel_recovery.py`
- Modify: `packages/framework/graph-engine/tests/runtime/test_staged_promotion_recovery.py`

**Interfaces:**

- Consumes: this plan's Task 9 Kernel state machine plus Semantic Attempt Task 9's real six-effect apply/reconcile tail and all existing terminal recovery behavior; it must not fake that tail locally.
- Produces: proven no-duplicate-dispatch recovery for seven required structured-pipeline cuts, the raw-write-closure subcuts, phase-complete fence checks, closed replay drift rejection, and unchanged seal/promotion/effect recovery.

- [ ] **Step 1: Add one named fault for each required crash cut.**

The test fault injector uses exactly these names and triggers after the producer phase but before the named durable/next boundary:

```python
STRUCTURED_CRASH_CUTS = (
    "prepared_computed_before_snapshot_durable",
    "activity_terminal_before_structured_result_durable",
    "structured_result_durable_before_manifest_durable",
    "manifest_durable_before_first_typed_install",
    "during_multi_file_materialization",
    "materialization_complete_before_finalizer",
    "finalizer_complete_before_seal",
)
```

Assert set equality against this tuple so no cut can be silently dropped or renamed.

Broker registration has its own pre-dispatch submatrix:

```python
BROKER_REGISTRATION_CRASH_CUTS = (
    "broker_registered_before_activity_dispatch",
    "activity_bound_before_first_tool_admission",
)
```

At the first cut, replay derives the same `activity_reference_digest`, reopens the same workspace and secret generation, and `register_or_adopt(...)` returns the same `broker_binding_digest` without allocating a second command namespace or dispatching a prompt. At the second, the adapter binding must authenticate the same reference/broker digest before the boundary plugin admits a tool. Rotation, fence/access/secret-set/workspace drift, an unregistered/fake sink, or a second binding is permanent integrity failure with zero command launch/value resolution. Kernel closes the adopted broker session before destroying the secret lifetime on every terminal/pending/error path only after authenticating durable tool-production quiescence; a crash during close is reconciled by the raw-write closure rules below. Add both asynchronous lifetime cases: (1) `activity running -> durable tool-production quiesce -> Kernel returns pending/process exits -> remote late tool call queues without execution -> exact generation/broker/activity reattach -> queued ordinal executes once`; and (2) `request accepted -> dispatch response lost -> dispatch pending -> late tool request -> recover stable activity + quiescence -> exact broker/generation reattach -> queued ordinal executes once`. Assert no second prompt, command, injection, or command namespace. A dispatch-pending result that cannot prove either “not accepted/no namespace” or the same durable quiescence, and an acknowledgement lost before quiescence became durable, are indeterminate; such a runtime cannot pass Checkpoint S.

The closure boundary has its own required submatrix and does not rename or dilute the seven spec cuts:

```python
RAW_WRITE_CLOSURE_CRASH_CUTS = (
    "activity_terminal_before_raw_closure_marker",
    "raw_closure_marker_before_event",
    "raw_closure_event_before_structured_result_durable",
)
```

Assert set equality here too. The middle case covers marker publication, closure-blob durability, and a crash before `AgentWritesClosed`; fence N+1 independently authenticates both the broker-binding and broker-registration-receipt digests plus the injection aggregate/transcripts, adopts the immutable marker/receipt, and appends the event under its current fence, while fence N cannot mutate or append. The final case restarts from the event, idempotently re-authenticates the same complete boundary marker/closure identity, and performs zero activity dispatch/observation before result persistence.

The immutable raw capture also has a required submatrix:

```python
RAW_POSTIMAGE_CRASH_CUTS = (
    "structured_result_durable_before_raw_postimage_durable",
    "during_multi_file_raw_postimage_capture",
)
```

Assert set equality. Before the event, replay recaptures the complete bounded set and may reuse equal content-addressed blobs; after `RawPostImagePersisted`, no finalizer or promotion path reads mutable raw staging. A changed file during/after capture causes failure or later seal disagreement, never redispatch or best-effort acceptance.

The immutable promotion-source adapter has a final required submatrix, folded into the existing `durable_prepare` stage rather than adding graph or trace stages:

```python
FROZEN_PROMOTION_CRASH_CUTS = (
    "seal_complete_before_frozen_promotion_set_durable",
    "frozen_promotion_set_durable_before_commit_prepare",
)
```

Replay calls `reopen_or_freeze_promotion_sources(...)` using the fixed Attempt/workspace locator, `sealed.partition_digest`, and the immutable typed/raw source refs reconstructed from durable materialization/raw-post-image records. It verifies/copies those refs and atomically adopts the same control-root set record; it never reopens Agent staging. Test absent, duplicate, wrong-locator, wrong-fence, and member/ref/digest drift. An immutable set record/publication or durable prepare with a different member/ref/digest is integrity failure; the set is a durable auxiliary record folded into the existing `durable_prepare` stage, not a seventh structured journal event.

- [ ] **Step 2: Write recovery expectations for the first three cuts.**

Parameterize and assert:

| Cut | Required source | Replay behavior |
| --- | --- | --- |
| prepared computed, snapshot not durable | validated `InputT` + authenticated inputs | prepare may rerun; dispatch count remains one and occurs only after snapshot anchor |
| activity terminal, result not durable | same bound activity/session | adopt/re-observe same activity; dispatch count exactly one |
| result durable, manifest not durable | prepared/result/closure blobs + events | complete raw snapshot first, then recompute/persist materialization manifest; zero dispatch/observation calls after restart |

For cut one, if the fault harness retained the pre-crash prepared digest, require the rerun digest to equal it. For cut two, execute all three closure subcuts: an unreconcilable same session or close returns pending/indeterminate; it never dispatches another activity, and no result is trusted before the closure event. For cut three, execute both raw-post-image subcuts before materialization; projectors/serializers may rerun because no materialization manifest was anchored, but their output must equal the same canonical manifest.

- [ ] **Step 3: Write recovery expectations for the final four cuts.**

Parameterize and assert:

| Cut | Required source | Replay behavior |
| --- | --- | --- |
| manifest durable, no install | anchored raw/materialization manifests + document/byte blobs | validate raw/typed snapshots and install exact typed manifest; no activity/projector/serializer call |
| mid multi-file install | anchored manifest + byte blobs | verify installed prefix and complete suffix; raw staging unchanged; no completed receipt until all match |
| materialization complete, no finalizer | prepared/result/raw/materialization/document/byte/receipt blobs | authenticate document snapshots and immutable raw view, then run finalizer; no activity/materializer preparation call |
| finalizer complete, no seal | same durable closure | reconstruct documents from snapshots, rerun finalizer, require byte-identical canonical `OutputT`, then seal once |

At every cut, promotion count is zero until complete seal/validation/prepare; after a successful replay it is exactly one. Once the activity is bound, external dispatch count is exactly one for the entire Attempt history.

- [ ] **Step 4: Run the crash matrix RED.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/attempts/test_kernel_artifact_recovery.py
```

Expected: at least the seven parameterized cases fail because phase-specific recovery is incomplete.

- [ ] **Step 5: Implement recovery from the latest durable phase only.**

Refactor Kernel recovery into one forward-only phase decision over the existing Attempt snapshot:

```text
terminal receipt
→ promotion/effect tail recovery
→ durable commit-prepare + authenticated frozen promotion set
→ published frozen promotion set before commit-prepare
→ materialization completed
→ materialization prepared
→ raw post-image persisted
→ structured result persisted
→ raw writes closed
→ prepared snapshot persisted
→ activity bound/terminal
→ fresh structured execution
```

The order above is precedence, not a reverse execution order. Probe the fixed promotion-set locator before any path that might rerun finalizer/seal. If a valid set exists without commit-prepare, reconstruct `SealedAttemptWrites.promotable` from its authenticated source refs, reconstruct/rerun the pure finalizer from durable values, and rerun validators over that immutable projection before writing the same commit-prepare; do not scan `write/`. If both set and commit-prepare exist, require exact digest equality and continue promotion recovery. Select the furthest authenticated phase first, reopen the original durable workspace/baseline identity without recapturing it, then choose project verification by phase: before promotion, verify the recorded baseline; from promotion-prepared onward, use only promotion receipt/reconciler old-image-versus-post-image rules; during effect/terminal recovery, verify the promoted post-image and receipt. Every `raw writes closed` or later phase idempotently authenticates the immutable external marker against the closure blob/event before reading raw staging. Load and validate only that phase's durable closure and continue forward. Never require pre-Attempt baseline equality after this Attempt has promoted, infer completion from mutable staging alone, or clear unrelated raw staging. A partial typed file without `MaterializationPrepared` is a violation; with the event it is verified/completed from its byte blob.

- [ ] **Step 6: Add a fence test at every irreversible or externally visible boundary.**

Parameterize fence loss immediately before and after:

```text
prepared blob persistence
PreparedSnapshotPersisted append
activity dispatch/adoption
raw-write marker publication/adoption
raw-write-closure blob persistence
AgentWritesClosed append
structured-result blob persistence
StructuredResultPersisted append
each raw post-image blob persistence
raw post-image manifest persistence
RawPostImagePersisted append
materialization byte/manifest persistence
MaterializationPrepared append
each typed file install
MaterializationCompleted append
finalizer call
seal
sealed promotable/internal partition publication
each immutable promotion-source copy
frozen promotion-set publication/adoption
durable prepare
promotion/recovery
effect apply/reconcile
terminal receipt publication
```

The stale runner may finish an already-started observation but cannot write the blob/event/file/checkpoint/receipt after fence loss. The new fence adopts the same Attempt, durable authorization, workspace, activity, blobs, and journal phase; old-fence cleanup cannot release or delete new-owner state.

- [ ] **Step 7: Run fencing RED and implement missing guards.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/attempts/test_kernel_artifact_fencing.py
```

Expected before the fix: at least one parameter allows a stale phase write. Add the minimal guard immediately before every mutation/publication and after any awaited pure/finalizer call whose result will be committed.

- [ ] **Step 8: Add the exact replay drift matrix.**

Starting from each durable phase, change one item and require a typed integrity/permanent failure before further work:

```python
DRIFT_FIELDS = (
    "graph_revision",
    "resolved_attempt_contract_digest",
    "structured_executor_digest",
    "prepared_schema_digest",
    "prepared_digest",
    "bound_activity_reference_digest",
    "raw_write_closure_digest",
    "raw_write_close_generation",
    "raw_write_boundary_marker_digest",
    "raw_write_command_transcript_digest",
    "structured_command_secret_set_digest",
    "authorized_secret_handles_digest",
    "structured_command_broker_binding_digest",
    "broker_registration_receipt_digest",
    "sandbox_policy_digest",
    "sandbox_qualification_digest",
    "command_secret_injection_aggregate_digest",
    "command_secret_injection_receipt_digest",
    "structured_network_access_digest",
    "structured_network_policy_digest",
    "structured_network_transcript_digest",
    "raw_write_historical_closing_fence",
    "candidate_digest",
    "terminal_assistant_message_id",
    "raw_observation_digest",
    "secret_generation_set_digest",
    "agent_result_schema_digest",
    "structured_result_digest",
    "raw_postimage_manifest_digest",
    "raw_postimage_member_set_digest",
    "raw_postimage_byte_digest",
    "raw_postimage_size",
    "raw_postimage_mode",
    "raw_member_selector_id",
    "raw_member_selector_provenance_digest",
    "raw_member_declaration_digest",
    "artifact_contract_digest",
    "bound_contract_digest",
    "workspace_identity_digest",
    "input_snapshot_digest",
    "input_snapshot_policy_digest",
    "structured_network_access_digest",
    "structured_network_backend_digest",
    "structured_network_policy_digest",
    "structured_network_qualification_digest",
    "input_snapshot_member_digest",
    "workspace_scan_budget_digest",
    "baseline_snapshot_digest",
    "baseline_entry_digest",
    "baseline_exists",
    "baseline_sha256",
    "baseline_size",
    "baseline_mode",
    "mutation",
    "repair_strategy",
    "logical_path",
    "artifact_document_schema_digest",
    "projector_id",
    "media_codec",
    "serializer_id",
    "serializer_corpus_digest",
    "document_object_digest",
    "byte_digest",
    "resolved_mode",
    "manifest_digest",
    "materialization_receipt_digest",
    "frozen_promotion_set_digest",
    "frozen_promotion_member_digest",
    "frozen_promotion_source_ref_digest",
)
```

Also test corrupt/missing blobs, event/blob mismatch, receipt/manifest mismatch, sealed-byte disagreement, secret rotation before the first snapshot, rotation after blob-before-event, and restart with an unavailable generation-proof provider. Generation drift never adopts an orphan blob or appends a new phase event. Any drift never causes redispatch, re-projection under new code, best-effort overwrite, promotion, or effect settlement.

- [ ] **Step 9: Run drift RED and implement exact phase authentication.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/attempts/test_kernel_artifact_drift.py
```

Expected before the fix: the earliest unbound identity is accepted. Compare the current resolved closure to the durable event/manifest/receipt before continuing and fail closed on the first disagreement.

- [ ] **Step 10: Run the complete recovery regression set.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/attempts/test_kernel_artifact_recovery.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_artifact_fencing.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_artifact_drift.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_recovery.py \
  packages/framework/graph-engine/tests/runtime/test_staged_promotion_recovery.py
```

Expected: all seven required cuts, all three raw-closure subcuts, both immutable-raw-capture cuts, both frozen-promotion cuts, all fence boundaries, every drift field, base Attempt recovery, six-effect recovery, and promotion recovery pass.

- [ ] **Step 11: Run static/import checks.**

```bash
uv run pyright \
  packages/framework/graph-engine/graph_engine/plugin_api.py \
  packages/framework/graph-engine/graph_engine/runtime/task_workspace.py \
  packages/framework/graph-engine/graph_engine/artifacts \
  packages/framework/graph-engine/graph_engine/attempts \
  packages/framework/graph-engine/graph_engine/persistence/attempt_artifacts.py
uv run ruff check \
  packages/framework/graph-engine/graph_engine/plugin_api.py \
  packages/framework/graph-engine/graph_engine/runtime/task_workspace.py \
  packages/framework/graph-engine/graph_engine/artifacts \
  packages/framework/graph-engine/graph_engine/attempts \
  packages/framework/graph-engine/graph_engine/persistence/attempt_artifacts.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_artifact_recovery.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_artifact_fencing.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_artifact_drift.py
uv run lint-imports
```

Expected: all commands exit `0`; core remains provider-neutral.

- [ ] **Step 12: Commit the recovery closure.**

```bash
git add \
  packages/framework/graph-engine/graph_engine/attempts/kernel.py \
  packages/framework/graph-engine/graph_engine/artifacts/materializer.py \
  packages/framework/graph-engine/graph_engine/persistence/attempt_artifacts.py \
  packages/framework/graph-engine/graph_engine/plugin_api.py \
  packages/framework/graph-engine/graph_engine/runtime/task_workspace.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_artifact_recovery.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_artifact_fencing.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_artifact_drift.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_recovery.py \
  packages/framework/graph-engine/tests/runtime/test_staged_promotion_recovery.py
git commit -m "test: close structured Attempt crash recovery"
```

## Artifact Foundation + Kernel Exit Gate

- [ ] Run `uv run pytest -q packages/framework/graph-engine/tests/artifacts packages/framework/graph-engine/tests/persistence/test_attempt_artifact_store.py packages/framework/graph-engine/tests/persistence/test_attempt_journal.py`.
- [ ] Run `uv run pytest -q packages/framework/graph-engine/tests/attempts packages/adapters/agent-runtime-contracts/tests`.
- [ ] Run the unchanged existing runtime regressions: `uv run pytest -q packages/framework/graph-engine/tests/runtime/test_task_workspace.py packages/framework/graph-engine/tests/runtime/test_task_workspace_faults.py packages/framework/graph-engine/tests/runtime/test_staged_promotion_recovery.py packages/framework/graph-engine/tests/runtime/test_activity_recovery.py packages/framework/graph-engine/tests/runtime/test_effects.py`.
- [ ] Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, and `uv run lint-imports`.
- [ ] Review a generated core fixture showing one typed-only, one raw-only, and one mixed resolved contract; this proves framework modes only and does not substitute for the later 33-contract/34-occurrence migration catalog.
- [ ] The fixture includes one `exact_bytes` slot with no migration record and one `semantic_migration` slot whose owner-matched `ArtifactSemanticMigrationRecord` digest changes resolved Attempt-contract/ProductLock/build closure and appears in its manifest/receipt.
- [ ] Review the exact 25-stage structured trace, six durable structured events, authenticated empty raw/typed manifests where applicable, terminal receipt digest binding, seven required crash-cut set plus closure/raw-capture subcuts, fence matrix, and drift matrix.
- [ ] Confirm `rg -n "provider_schema|requires_provider_schema|CompositeAttemptExecutor" packages/framework/graph-engine/graph_engine/attempts packages/adapters/agent-runtime-contracts/agent_runtime_contracts` returns no production match.
- [ ] Confirm no LangGraph graph factory, node, edge, route, join, interrupt, or Feature topology file changed in this child plan.
- [ ] Request code review focused on schema digest authority, path closure before resources, registry ownership, serializer corpus stability, snapshot/event ordering, manifest-before-install ordering, finalizer write isolation, seal/receipt agreement, duplicate-dispatch prevention, fencing, and drift rejection.

This exit gate makes the Artifact Foundation and Kernel mechanics available to the separate OpenCode capability/version and Capability migration plans. It does not by itself close Checkpoint S: the qualifying adapter version, 33-contract classification, every typed-slot skill/finalizer/parity row, dynamic Intake/codegen proofs, documentation amendments, and Product integration remain required by the parent spec.
