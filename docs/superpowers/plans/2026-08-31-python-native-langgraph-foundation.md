# Python-native LangGraph Foundation Implementation Plan

> Historical implementation plan: Attempt event/journal and action-runtime steps
> are superseded by the [Attempt checkpoint migration](2026-10-09-attempt-checkpoints.md).


> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish the pinned LangGraph dependency boundary, authenticated graph revision/manifest model, owner-scoped graph factory loading, journal-anchored checkpoint persistence, runner lease/fencing, and engine-neutral Boot/Application contracts required by every later graph.

**Architecture:** Offline Boot authenticates the Product and six Capability sources, resolves immutable contracts, imports only Product-allowlisted factory symbols, dry-builds every root without a saver, and emits a serializable `GraphBuildManifest`. Runtime Boot reconstructs the same manifest and compiles roots with an anchored saver into `BootArtifact`. One Invocation maps to one LangGraph thread, one graph revision, and one fenced runner. Checkpoint bytes/pending writes become resumable only after the store/outbox/journal anchoring handshake closes.

**Tech Stack:** Python 3.11, uv, LangGraph `1.2.11`, LangGraph Checkpoint `4.2.0`, LangGraph SQLite Checkpoint `3.1.1`, Pydantic v2, pytest, SQLite, `fcntl` on the local CLI backend, existing canonical JSON/source snapshot/Product lock primitives.

**Spec:** `docs/superpowers/specs/2026-08-31-python-native-langgraph-assurance-design.md`, especially sections 7, 20, 23–25, 27 Phase 1, and 28.

---

## Global Constraints

This plan owns framework foundation only. It introduces no production Feature topology and does not switch `aa run` away from the legacy Runtime. Follow the master plan's interleave: run Foundation Task 1, then Semantic Attempt Task 1, then Foundation Tasks 2–7; after Semantic Attempt Tasks 2–6, finish Foundation Tasks 8–10. Feature migration waits until both child-plan exit gates are green.

Implementation must begin in a clean worktree created with `superpowers:using-git-worktrees` from an integration-base commit containing the accepted spec and this plan suite. Preserve the current dirty worktree untouched. Use explicit path staging only.

Target package layout introduced here:

```text
packages/framework/graph-engine/graph_engine/
├── boot/
│   ├── __init__.py
│   ├── graph_revision.py
│   ├── source_authentication.py
│   └── boot.py
├── application/
│   ├── __init__.py
│   ├── runtime_context.py
│   ├── revision_guard.py
│   ├── status.py
│   └── application.py
└── persistence/
    ├── __init__.py
    ├── journal.py
    ├── anchored_checkpointer.py
    └── runner_lease.py
```

Tests use official in-memory saver semantics through the same anchoring wrapper; the local Product backend uses SQLite. No service object is written into graph state.

### Task 1: Pin direct dependencies and freeze import boundaries

**Files:**

- Modify: `packages/framework/graph-engine/pyproject.toml`
- Modify: `packages/capabilities/assurance-intake/pyproject.toml`
- Modify: `packages/capabilities/assurance-generation/pyproject.toml`
- Modify: `packages/capabilities/assurance-execution/pyproject.toml`
- Modify: `packages/capabilities/assurance-quality/pyproject.toml`
- Modify: `packages/capabilities/assurance-healing/pyproject.toml`
- Modify: `packages/capabilities/assurance-improvement/pyproject.toml`
- Modify: `packages/products/assurance-product/pyproject.toml`
- Modify: `uv.lock`
- Modify: `.importlinter`
- Modify: `tests/architecture/test_graph_engine_boundaries.py`
- Create: `tests/architecture/test_langgraph_dependency_boundaries.py`

**Interfaces:** `graph-engine` directly owns LangGraph/Checkpoint base APIs; every Feature directly owns its `StateGraph` import; Product directly owns root graph composition and SQLite saver construction.

- [ ] **Step 1: Add a failing dependency-boundary test.**

Create `tests/architecture/test_langgraph_dependency_boundaries.py` with exact TOML assertions:

```python
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[2]
FEATURES = (
    "assurance-intake",
    "assurance-generation",
    "assurance-execution",
    "assurance-quality",
    "assurance-healing",
    "assurance-improvement",
)


def dependencies(path: Path) -> set[str]:
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    return set(document["project"]["dependencies"])


def test_langgraph_dependencies_are_direct_and_exact() -> None:
    engine = dependencies(ROOT / "packages/framework/graph-engine/pyproject.toml")
    assert "langgraph==1.2.11" in engine
    assert "langgraph-checkpoint==4.2.0" in engine
    for feature in FEATURES:
        feature_deps = dependencies(ROOT / f"packages/capabilities/{feature}/pyproject.toml")
        assert "langgraph==1.2.11" in feature_deps
    product = dependencies(ROOT / "packages/products/assurance-product/pyproject.toml")
    assert "langgraph==1.2.11" in product
    assert "langgraph-checkpoint-sqlite==3.1.1" in product
```

- [ ] **Step 2: Run the new test and confirm the intended failure.**

```bash
uv run pytest -q tests/architecture/test_langgraph_dependency_boundaries.py
```

Expected: fail because the exact LangGraph dependencies are absent.

- [ ] **Step 3: Add the direct exact dependencies and update import rules.**

Add `langgraph==1.2.11` and `langgraph-checkpoint==4.2.0` to `graph-engine`; add `langgraph==1.2.11` to each Feature and Product; add `langgraph-checkpoint-sqlite==3.1.1` to Product. During coexistence, add `.graphs` beside every existing cross-Feature forbidden `.workflow` suffix; retain both plus all handler/validator/effect/resource prohibitions until legacy deletion. Add `langgraph` to framework/Product allowed roots, not to forbidden lower-layer imports.

- [ ] **Step 4: Regenerate the lock and verify imports.**

```bash
uv lock
uv sync --dev
uv run pytest -q \
  tests/architecture/test_langgraph_dependency_boundaries.py \
  tests/architecture/test_graph_engine_boundaries.py
uv run lint-imports
```

Expected: all exit `0`; lock contains the three exact package versions.

- [ ] **Step 5: Commit only dependency/boundary files.**

```bash
git add \
  packages/framework/graph-engine/pyproject.toml \
  packages/capabilities/assurance-intake/pyproject.toml \
  packages/capabilities/assurance-generation/pyproject.toml \
  packages/capabilities/assurance-execution/pyproject.toml \
  packages/capabilities/assurance-quality/pyproject.toml \
  packages/capabilities/assurance-healing/pyproject.toml \
  packages/capabilities/assurance-improvement/pyproject.toml \
  packages/products/assurance-product/pyproject.toml \
  uv.lock .importlinter \
  tests/architecture/test_graph_engine_boundaries.py \
  tests/architecture/test_langgraph_dependency_boundaries.py
git commit -m "build: pin Python-native LangGraph dependencies"
```

### Task 2: Define canonical graph revisions and build manifests

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/boot/__init__.py`
- Create: `packages/framework/graph-engine/graph_engine/boot/graph_revision.py`
- Create: `packages/framework/graph-engine/tests/boot/test_graph_revision.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/lock.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_lock_model.py`
- Modify: `packages/framework/graph-engine/graph_engine/__init__.py`

**Interfaces:** Immutable `ProductLock` v3, `FeatureFactoryRef`, `EntrypointGraphContract`, `GraphRevision`, `GraphBuildManifest`, and `BootArtifact`; data-only canonical digest functions. `BootArtifact` is runtime-only and never serialized. Legacy `InvocationLock` v2 remains byte-for-byte stable for old Invocation resume.

- [ ] **Step 1: Write canonicalization tests first.**

```python
def test_revision_digest_is_order_independent_and_source_sensitive() -> None:
    left = GraphRevision.build(
        product_lock_digest="a" * 64,
        wheel_source_digests={"assurance.intake": "b" * 64, "assurance.product": "c" * 64},
        factory_symbols=("assurance_intake.graphs.factory:build_intake_graphs",),
        state_schema_versions={"intake": "1"},
        langgraph_version="1.2.11",
        checkpoint_contract_version="1",
    )
    reordered = GraphRevision.build(
        product_lock_digest="a" * 64,
        wheel_source_digests={"assurance.product": "c" * 64, "assurance.intake": "b" * 64},
        factory_symbols=("assurance_intake.graphs.factory:build_intake_graphs",),
        state_schema_versions={"intake": "1"},
        langgraph_version="1.2.11",
        checkpoint_contract_version="1",
    )
    changed = replace(left, langgraph_version="1.2.12", revision_id="0" * 64)
    assert left == reordered
    assert changed.canonical_revision_id() != left.revision_id


def test_manifest_json_excludes_compiled_graphs_and_runtime_ports(
    manifest: GraphBuildManifest,
) -> None:
    encoded = manifest.model_dump(mode="json")
    assert set(encoded) == {"revision", "entrypoint_contract_digests", "attempt_contract_digests"}
    assert "checkpointer" not in repr(encoded)
    assert "kernel" not in repr(encoded)


def test_product_lock_v3_has_no_compiled_workflow_or_execution_host(
    product_lock: ProductLock,
) -> None:
    document = product_lock.model_dump(mode="json")
    assert document["schema_version"] == "3"
    assert "compiled_workflow" not in document
    assert "execution_host" not in document


def test_legacy_invocation_lock_v2_golden_is_unchanged(
    invocation_lock_v2: InvocationLock,
    legacy_golden_path: Path,
) -> None:
    assert invocation_lock_v2.model_dump_json() == legacy_golden_path.read_text(encoding="utf-8")
```

- [ ] **Step 2: Confirm import/test failure.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/boot/test_graph_revision.py
```

Expected: collection fails because `graph_engine.boot.graph_revision` does not exist.

- [ ] **Step 3: Implement frozen validated models and projections.**

Use tuples/sorted mappings in canonical projections and `graph_engine.canonical.canonical_digest`; never hash `repr`, a callable, or a compiled graph:

```python
@dataclass(frozen=True, slots=True)
class GraphRevision:
    revision_id: str
    product_lock_digest: str
    wheel_source_digests: Mapping[str, str]
    factory_symbols: tuple[str, ...]
    state_schema_versions: Mapping[str, str]
    langgraph_version: str
    checkpoint_contract_version: str


@dataclass(frozen=True, slots=True)
class GraphBuildManifest:
    revision: GraphRevision
    entrypoint_contract_digests: Mapping[str, str]
    attempt_contract_digests: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class BootArtifact:
    manifest: GraphBuildManifest
    entrypoints: Mapping[str, CompiledStateGraph]
    attempt_contracts: Mapping[str, ResolvedAttemptContract[object, object]]
    checkpointer_backend_id: str
```

Create `ProductLock` v3 alongside, not by mutating, `InvocationLock` v2. Its canonical fields retain authenticated Product/plugin/config/source/registry closure and remove only `compiled_workflow`, its digest, and the legacy execution-host lock. Keep LangGraph/Attempt imports under `TYPE_CHECKING` where runtime cycles would otherwise arise. Validate lowercase SHA-256 values, stable sorted unique factory symbols, nonempty schema versions, and exact recomputation of `revision_id`.

- [ ] **Step 4: Run focused tests and static checks.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/boot/test_graph_revision.py
uv run pytest -q packages/framework/graph-engine/tests/composition/test_lock_model.py
uv run pyright packages/framework/graph-engine/graph_engine/boot
uv run ruff check packages/framework/graph-engine/graph_engine/boot
```

Expected: all exit `0`.

- [ ] **Step 5: Commit the revision contract.**

```bash
git add \
  packages/framework/graph-engine/graph_engine/__init__.py \
  packages/framework/graph-engine/graph_engine/boot/__init__.py \
  packages/framework/graph-engine/graph_engine/boot/graph_revision.py \
  packages/framework/graph-engine/graph_engine/composition/lock.py \
  packages/framework/graph-engine/tests/composition/test_lock_model.py \
  packages/framework/graph-engine/tests/boot/test_graph_revision.py
git commit -m "feat: define authenticated graph revision manifests"
```

### Task 3: Authenticate the fixed graph factory allowlist

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/boot/source_authentication.py`
- Create: `packages/framework/graph-engine/tests/boot/test_source_authentication.py`
- Create: `packages/products/assurance-product/assurance_product/graph_factories.py`
- Create: `tests/product/test_feature_factory_allowlist.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/models.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/contributions.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_plugin_contracts.py`

**Interfaces:** `FeatureFactoryRef(owner_id, symbol)`, `ProductFactoryRef(product_id, symbol)`, `AuthenticatedFactory`, `authenticate_factory_ref(ref, sources)`, immutable `attempt_contracts` contribution projections, coexistence-safe Product-manifest `graph_factory_symbol`, and Product-owned `FEATURE_GRAPH_FACTORIES` with exactly six owner/symbol pairs. `.aa/` cannot provide or alter them.

- [ ] **Step 1: Test accepted ownership and all fail-closed cases.**

```python
def test_factory_module_must_belong_to_authenticated_owner(tmp_path: Path) -> None:
    source = authenticated_editable_source(
        owner_id="assurance.intake",
        root=tmp_path / "wheel",
        import_roots=("assurance_intake",),
    )
    accepted = authenticate_factory_ref(
        FeatureFactoryRef(
            owner_id="assurance.intake",
            symbol="assurance_intake.graphs.factory:build_intake_graphs",
        ),
        {"assurance.intake": source},
    )
    assert accepted.owner_id == "assurance.intake"


@pytest.mark.parametrize("symbol", [
    "sut_graphs:build",
    "assurance_generation.graphs.factory:build_generation_graphs",
    "assurance_intake.graphs.factory:_private",
])
def test_factory_origin_or_symbol_mismatch_is_rejected(symbol: str, sources: object) -> None:
    with pytest.raises(FactoryAuthenticationError):
        authenticate_factory_ref(FeatureFactoryRef(owner_id="assurance.intake", symbol=symbol), sources)
```

Add composition tests proving descriptor/realized `attempt_contracts` IDs and digests must match and configuration-tree contributions cannot declare contracts.

Create the Product constant in this Task, before any Feature factory implementation exists. It contains only the six frozen strings and owner IDs; importing it must not import the not-yet-created modules. `test_feature_factory_allowlist.py` asserts exact order/set and rejects a derived entry-point scan or configuration override.

Add a discriminated Python Product form to `ProductManifest`: exactly one of the legacy Workflow forms or `graph_factory_symbol` is present. Authenticate the symbol against the installed Product source with the same quarantine/provenance rules. Keep legacy forms during coexistence for current examples and legacy starts; Product Task 7 switches the examples/consumers, Product Task 8 switches Assurance declarations and deletes their YAML, and atomic Task 9 removes the legacy forms. Tests reject mixed/empty forms, SUT/config symbols and a symbol whose module is outside the authenticated Product import roots.

- [ ] **Step 2: Run tests to prove missing behavior.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/boot/test_source_authentication.py \
  packages/framework/graph-engine/tests/composition/test_plugin_contracts.py \
  tests/product/test_feature_factory_allowlist.py
```

Expected: new tests fail because factory authentication and Attempt contribution fields are absent.

- [ ] **Step 3: Implement closed symbol resolution.**

Split `module:attribute`, reject relative/private attributes, and reuse the existing snapshotted provider import transaction: quarantine preloaded modules, validate module provenance/source origin, import within the serialized import session, revalidate source bytes after import, and roll back module state on failure. Require a callable with the exact public attribute name and return it with owner/source digest. A plain `import_module` path is not acceptable. Do not scan entrypoints, directories, or `.aa/`.

- [ ] **Step 4: Add data-only Attempt contribution projection.**

Store contract ID plus canonical digest in descriptor and realized contribution projections. At this layer do not import Capability model classes; the later Attempt plan resolves the authenticated data into core contracts.

- [ ] **Step 5: Verify focused suites.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/boot/test_source_authentication.py \
  packages/framework/graph-engine/tests/composition/test_plugin_contracts.py \
  packages/framework/graph-engine/tests/composition/test_registry_platform.py
uv run lint-imports
```

Expected: all exit `0`; tests prove a SUT symbol and cross-owner symbol fail before import.

- [ ] **Step 6: Commit factory authentication.**

```bash
git add \
  packages/framework/graph-engine/graph_engine/boot/source_authentication.py \
  packages/framework/graph-engine/graph_engine/composition/models.py \
  packages/framework/graph-engine/graph_engine/composition/contributions.py \
  packages/products/assurance-product/assurance_product/graph_factories.py \
  packages/framework/graph-engine/tests/boot/test_source_authentication.py \
  packages/framework/graph-engine/tests/composition/test_plugin_contracts.py \
  tests/product/test_feature_factory_allowlist.py
git commit -m "feat: authenticate fixed Feature graph factories"
```

### Task 4: Define checkpoint journal records and the strict serializer

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/persistence/__init__.py`
- Create: `packages/framework/graph-engine/graph_engine/persistence/journal.py`
- Create: `packages/framework/graph-engine/graph_engine/stategraph/__init__.py`
- Create: `packages/framework/graph-engine/graph_engine/stategraph/checkpoint_bridge.py`
- Create: `packages/framework/graph-engine/tests/persistence/test_journal_contract.py`
- Create: `packages/framework/graph-engine/tests/persistence/test_strict_serializer.py`
- Create: `packages/framework/graph-engine/tests/stategraph/test_checkpoint_bridge.py`

**Interfaces:** `InvocationStarted`, `CheckpointAnchor`, `CheckpointAnchorState`, `CheckpointAnchorJournalPort`, `CheckpointIntegrityError`, `strict_checkpoint_serializer()`, framework-neutral `CheckpointBridgeMarker`, `CheckpointBridgeState`, and last-write `replace_checkpoint_marker_batch`. Attempt-journal types are deliberately deferred to Semantic Attempt Task 8 so this foundation does not reference types that do not yet exist.

- [ ] **Step 1: Specify canonical identity and serializer rejection.**

```python
def test_checkpoint_anchor_digest_covers_bytes_lineage_revision_and_fence() -> None:
    anchor = CheckpointAnchor.build(
        invocation_id="inv-1",
        thread_id="inv-1",
        checkpoint_id="cp-2",
        parent_checkpoint_id="cp-1",
        checkpoint_bytes=b"checkpoint",
        pending_write_bytes=(b"write-a",),
        task_identity="task-1",
        graph_revision="a" * 64,
        product_lock_digest="b" * 64,
        root_input_digest="c" * 64,
        fencing_token=4,
    )
    assert len(anchor.anchor_digest) == 64
    assert replace(anchor, fencing_token=5).canonical_digest() != anchor.anchor_digest


def test_strict_serializer_rejects_unapproved_python_type() -> None:
    serializer = strict_checkpoint_serializer()
    with pytest.raises((TypeError, ValueError)):
        serializer.dumps_typed(UnsafeCheckpointObject())
```

- [ ] **Step 2: Run and see missing-module failures.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/persistence/test_journal_contract.py \
  packages/framework/graph-engine/tests/persistence/test_strict_serializer.py
```

Expected: collection fails because persistence contracts do not exist.

- [ ] **Step 3: Implement immutable records and port.**

The journal port exposes idempotent append/read/CAS operations and requires the fencing token on every mutation:

```python
class CheckpointAnchorJournalPort(Protocol):
    async def start_invocation(self, record: InvocationStarted, *, fencing_token: int) -> None: ...
    async def append_checkpoint_anchor(self, anchor: CheckpointAnchor, *, fencing_token: int) -> None: ...
    async def read_checkpoint_anchor(
        self, thread_id: str, checkpoint_id: str
    ) -> CheckpointAnchor | None: ...
    async def assert_current_fence(self, invocation_id: str, fencing_token: int) -> None: ...


```

The port exposes no Workflow transition API. Duplicate identical records succeed; the same checkpoint identity with different bytes, digest, lineage, revision, input identity or fence fails closed.

- [ ] **Step 4: Construct strict JsonPlus serialization.**

Use:

```python
JsonPlusSerializer(
    pickle_fallback=False,
    allowed_json_modules=None,
    allowed_msgpack_modules=None,
)
```

Restrict graph state itself to data-only builtins/Pydantic JSON projections; add no broad module allowlist. Tests cover secrets/service objects and arbitrary classes.

Define `CheckpointBridgeState` as a `TypedDict` base with optional `assurance_checkpoint_markers: Annotated[list[CheckpointBridgeMarker], replace_checkpoint_marker_batch]`. Validate the two marker phases, canonical Attempt key/generation/ordinal/digest fields, deterministic ordering, and the fixed active-generation bound. The reducer replaces the prior batch rather than appending history. Feature/Product states must inherit this base; public adapters and semantic input selection never expose the reserved key.

- [ ] **Step 5: Run focused tests and commit.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/persistence
uv run pytest -q packages/framework/graph-engine/tests/stategraph/test_checkpoint_bridge.py
uv run pyright packages/framework/graph-engine/graph_engine/persistence
git add \
  packages/framework/graph-engine/graph_engine/persistence/__init__.py \
  packages/framework/graph-engine/graph_engine/persistence/journal.py \
  packages/framework/graph-engine/graph_engine/stategraph/__init__.py \
  packages/framework/graph-engine/graph_engine/stategraph/checkpoint_bridge.py \
  packages/framework/graph-engine/tests/persistence/test_journal_contract.py \
  packages/framework/graph-engine/tests/persistence/test_strict_serializer.py \
  packages/framework/graph-engine/tests/stategraph/test_checkpoint_bridge.py
git commit -m "feat: define anchored checkpoint journal contract"
```

Expected: tests and type check exit `0` before commit.

### Task 5: Implement the anchored checkpointer, including pending writes

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/persistence/anchored_checkpointer.py`
- Create: `packages/framework/graph-engine/graph_engine/persistence/checkpoint_store.py`
- Create: `packages/framework/graph-engine/graph_engine/persistence/checkpoint_observer.py`
- Create: `packages/framework/graph-engine/tests/persistence/test_anchored_checkpointer.py`
- Create: `packages/framework/graph-engine/tests/persistence/test_checkpoint_store_contract.py`
- Create: `packages/framework/graph-engine/tests/persistence/test_checkpoint_recovery.py`

**Interfaces:** async `AnchoredCheckpointer(BaseCheckpointSaver)`, `CheckpointStoreTransactionPort`, `CheckpointAnchorObserverPort`, durable `CheckpointOutboxRecord`, `CheckpointAnchorNotice`, `aput`, `aput_writes`, `aget_tuple`, `alist`, and `arecover()`. Sync methods fail explicitly so no caller can bypass anchoring.

- [ ] **Step 1: Write happy-path protocol tests against an in-memory backend.**

```python
async def test_put_is_visible_only_after_matching_journal_anchor(saver, journal) -> None:
    stored_config = await saver.aput(config, checkpoint, metadata, new_versions)
    checkpoint_id = stored_config["configurable"]["checkpoint_id"]
    assert await journal.read_checkpoint_anchor("inv-1", checkpoint_id) is not None
    loaded = await saver.aget_tuple(stored_config)
    assert loaded is not None
    assert loaded.checkpoint == checkpoint


async def test_put_writes_anchors_pending_task_identity(saver, journal) -> None:
    await saver.aput_writes(
        config,
        [("result", {"ok": True})],
        task_id="task-a",
        task_path="push-0",
    )
    anchor = journal.only_pending_write_anchor()
    assert anchor.task_identity == "task-a:push-0"
```

- [ ] **Step 2: Add every crash cut before implementation.**

Parameterize faults after store+outbox, after journal append, after mark-journal-anchored, after observer delivery, and before mark-observers-delivered. Assert `await saver.arecover(thread_id="inv-1")` completes the missing idempotent step. Add mismatch tests for missing bytes, digest drift, wrong parent, wrong revision, wrong root input, and stale fence; every read raises `CheckpointIntegrityError`.

Add the lease-loss cut: old fence stores checkpoint+outbox, replacement acquires a newer fence, and the old owner is rejected before journal anchoring. Successor `arecover` marks that unauthenticated old-fence row abandoned and invisible, then LangGraph replays from the previous anchored checkpoint; Attempt idempotency prevents duplicate mutation. If the journal anchor already exists, successor recovery may complete observer delivery instead. Tests prove exactly one matching row becomes visible and a stale row is never adopted as if it had the new fence.

Add the completion-source cut: `aput_writes` receives a node update containing `system_interrupt_completed`, then crashes before the superstep `aput`. Assert no completion observer notice exists and the Attempt generation remains active; restart must replay the issued ordinal. After the merged-state `aput` anchors, exactly one completion-batch notice is deliverable. Issuance markers from an interrupt pending write remain deliverable under their separate rule.

- [ ] **Step 3: Run tests and confirm all new cases fail.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/persistence/test_anchored_checkpointer.py \
  packages/framework/graph-engine/tests/persistence/test_checkpoint_recovery.py
```

Expected: collection/import failure.

- [ ] **Step 4: Implement the transaction and observer seams before the saver.**

`CheckpointStoreTransactionPort` has separate async methods for checkpoint rows and pending-write rows. Each method accepts already serialized canonical bytes plus a `CheckpointOutboxDraft`, commits the upstream-compatible data row and outbox row in one backend transaction, and returns the stored config/outbox ID. It also exposes idempotent `mark_journal_anchored`, `mark_observers_delivered`, raw read/list, and recovery-scan operations. No implementation may call another saver's `aput`/`aput_writes` and then insert the outbox separately.

`CheckpointAnchorObserverPort.on_anchored(notice)` receives the authenticated anchor plus persisted data-only markers under a strict source rule. An issuance marker may be extracted from an interrupt payload written by `aput_writes` or from the final `aput` checkpoint. A completion marker seen in `aput_writes` is stored only as ordinary pending-write bytes and is **not** placed in an observer notice; completion becomes deliverable only when `aput` durably writes and anchors the merged state containing that batch. Duplicate notice delivery is required. The outbox retains separate `journal_anchored_at` and `observers_delivered_at` phases so recovery can replay either half. The framework observer interface does not import Attempt types. It preserves a whole completion-marker batch in one final-checkpoint notice so an Attempt observer can retire multiple active generations atomically.

- [ ] **Step 5: Implement a protocol-complete saver.**

`aput` and `aput_writes` serialize canonical bytes with the strict serializer, call the matching atomic store method, append the journal anchor, mark journal anchoring, deliver every configured observer, mark observer delivery, then return. `aget_tuple`/`alist` expose only rows whose matching anchor and observer-delivery phases both validate. `arecover` scans incomplete outbox records and idempotently completes that sequence. `put`, `put_writes`, `get_tuple`, and `list` raise a clear async-only error; they never delegate around the handshake.

- [ ] **Step 6: Prove real LangGraph pending-write behavior.**

Compile a two-branch graph and drive it with `ainvoke`: one branch interrupts and the sibling completes. Assert the sibling's pending write is anchored through `aput_writes`, is not rerun after resume, and does not advance a later superstep while the interrupt is pending.

- [ ] **Step 7: Verify and commit.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/persistence
uv run pyright packages/framework/graph-engine/graph_engine/persistence
git add \
  packages/framework/graph-engine/graph_engine/persistence/anchored_checkpointer.py \
  packages/framework/graph-engine/graph_engine/persistence/checkpoint_store.py \
  packages/framework/graph-engine/graph_engine/persistence/checkpoint_observer.py \
  packages/framework/graph-engine/tests/persistence/test_anchored_checkpointer.py \
  packages/framework/graph-engine/tests/persistence/test_checkpoint_store_contract.py \
  packages/framework/graph-engine/tests/persistence/test_checkpoint_recovery.py
git commit -m "feat: anchor LangGraph checkpoints and pending writes"
```

### Task 6: Add the Invocation runner lease and monotonic fencing

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/persistence/runner_lease.py`
- Create: `packages/framework/graph-engine/tests/persistence/test_runner_lease.py`
- Modify: `packages/framework/graph-engine/graph_engine/persistence/anchored_checkpointer.py`
- Modify: `packages/framework/graph-engine/tests/persistence/test_anchored_checkpointer.py`

**Interfaces:** `InvocationRunnerLeasePort`, `LocalInvocationRunnerLease`, `RunnerLease`, `RunnerConflict`, `StaleFencingToken`; every saver write validates top-level configurable fencing metadata.

- [ ] **Step 1: Write contention and reclaim tests.**

```python
async def test_only_one_local_runner_acquires_invocation(tmp_path: Path) -> None:
    leases = LocalInvocationRunnerLease(tmp_path)
    first = await leases.acquire("inv-1", owner_id="runner-a")
    try:
        assert first.fencing_token == 1
        with pytest.raises(RunnerConflict):
            await leases.acquire("inv-1", owner_id="runner-b")
    finally:
        await leases.release(first)


async def test_crash_reclaim_fences_old_owner(tmp_path: Path) -> None:
    first_process = LocalInvocationRunnerLease(tmp_path)
    first = await first_process.acquire("inv-1", owner_id="runner-a")
    first_process.simulate_process_exit_for_test(first)

    replacement_process = LocalInvocationRunnerLease(tmp_path)
    second = await replacement_process.acquire("inv-1", owner_id="runner-b")
    try:
        assert second.fencing_token > first.fencing_token
        with pytest.raises(StaleFencingToken):
            await replacement_process.assert_current("inv-1", first.fencing_token)
    finally:
        await replacement_process.release(second)
```

- [ ] **Step 2: Confirm failures, then implement local lease storage.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/persistence/test_runner_lease.py
```

Expected: missing module. Implement `lease = await acquire(...)` and `await release(lease)` around a nonblocking `flock`, plus durable, atomically written owner/fence metadata under the runtime-owned control directory. Validate directory ownership, regular files, and no symlink traversal. Increment fencing monotonically on every successful new ownership epoch. Never break a live OS lock; crash reclaim is tested only after the first process/fixture closes its lock file descriptor while leaving the durable old fence record.

- [ ] **Step 3: Fence saver writes.**

Require `config["configurable"]["thread_id"]` and `assurance_fencing_token` on `aput`/`aput_writes`; validate that `thread_id` is the journal Invocation ID, await the lease/journal fence before backend mutation, and check it again before publishing the anchor. A newer token makes the old writer fail even if it still holds in-memory state.

- [ ] **Step 4: Run concurrency tests.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/persistence/test_runner_lease.py \
  packages/framework/graph-engine/tests/persistence/test_anchored_checkpointer.py
```

Expected: one acquisition succeeds, one receives `RunnerConflict`, and a reclaimed predecessor cannot checkpoint.

- [ ] **Step 5: Commit lease/fence changes.**

```bash
git add \
  packages/framework/graph-engine/graph_engine/persistence/runner_lease.py \
  packages/framework/graph-engine/graph_engine/persistence/anchored_checkpointer.py \
  packages/framework/graph-engine/tests/persistence/test_runner_lease.py \
  packages/framework/graph-engine/tests/persistence/test_anchored_checkpointer.py
git commit -m "feat: fence concurrent Invocation runners"
```

### Task 7: Define typed runtime context, status, and revision guard

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/application/__init__.py`
- Create: `packages/framework/graph-engine/graph_engine/application/runtime_context.py`
- Create: `packages/framework/graph-engine/graph_engine/application/revision_guard.py`
- Create: `packages/framework/graph-engine/graph_engine/application/status.py`
- Create: `packages/framework/graph-engine/tests/application/test_runtime_context.py`
- Create: `packages/framework/graph-engine/tests/application/test_revision_guard.py`
- Create: `packages/framework/graph-engine/tests/application/test_status.py`

**Interfaces:** `AssuranceRuntimeContext`, `RevisionMismatch`, `require_revision`, `InvocationStatus` and normalization helpers. Context carries Kernel/workspace/secrets/fence by reference and must be excluded from checkpoint serialization.

- [ ] **Step 1: Specify revision and context security behavior.**

```python
def test_runtime_context_is_not_graph_state_or_json_serializable(
    kernel: AttemptKernelPort,
    secrets: SecretResolverPort,
    workspaces: WorkspaceProviderPort,
) -> None:
    context = AssuranceRuntimeContext(
        revision_id="a" * 64,
        fencing_token=7,
        attempt_kernel=kernel,
        secret_resolver=secrets,
        workspace_provider=workspaces,
    )
    assert "attempt_kernel" not in context.checkpoint_projection()
    with pytest.raises(TypeError):
        canonical_json_bytes(context)


def test_revision_guard_reports_required_deployment() -> None:
    with pytest.raises(RevisionMismatch, match="a{64}"):
        require_revision(required="a" * 64, installed="b" * 64)
```

- [ ] **Step 2: Run the failing tests.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/application
```

Expected: missing application package.

- [ ] **Step 3: Implement the closed status vocabulary.**

Use exactly `running | blocked | interrupted | stopped | failed | completed`. Normalize only from compiled graph snapshots/terminal envelopes; do not fold the Attempt journal into a second Workflow state. Treat `GraphRecursionError` as failed runtime state, separate from business-budget terminals.

- [ ] **Step 4: Verify and commit.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/application
uv run pyright packages/framework/graph-engine/graph_engine/application
git add \
  packages/framework/graph-engine/graph_engine/application/__init__.py \
  packages/framework/graph-engine/graph_engine/application/runtime_context.py \
  packages/framework/graph-engine/graph_engine/application/revision_guard.py \
  packages/framework/graph-engine/graph_engine/application/status.py \
  packages/framework/graph-engine/tests/application/test_runtime_context.py \
  packages/framework/graph-engine/tests/application/test_revision_guard.py \
  packages/framework/graph-engine/tests/application/test_status.py
git commit -m "feat: add revision-pinned LangGraph runtime context"
```

### Task 8: Build offline and runtime Boot paths

**Dependency:** Semantic Attempt Tasks 1–2 are complete, so provider-neutral resolved contracts and the authenticated registry exist.

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/boot/boot.py`
- Create: `packages/framework/graph-engine/tests/boot/test_boot.py`
- Create: `packages/framework/graph-engine/tests/boot/test_boot_manifest_parity.py`
- Modify: `packages/framework/graph-engine/graph_engine/boot/__init__.py`

**Interfaces:** `GraphEngineBoot.compile_manifest(request)`, `GraphEngineBoot.boot(request, checkpointer, runtime_ports)`, `GraphBuildContext`, owner-scoped `CapabilityBuildContext`, and exact manifest parity. Boot consumes a Product-supplied fixed factory-ref tuple and generic owner-keyed Feature bundles; it contains no hard-coded Assurance keyword list.

- [ ] **Step 1: Write dry/runtime compile parity tests with fake factories.**

```python
def test_offline_compile_uses_no_saver_and_runtime_boot_matches_manifest(
    authenticator: SourceAuthenticator,
    resolver: ContractResolverPort,
    request: BootRequest,
    factories: RecordingFactories,
    anchored_saver: AnchoredCheckpointer,
    ports: RuntimePorts,
) -> None:
    boot = GraphEngineBoot(authenticator=authenticator, contract_resolver=resolver)
    manifest = boot.compile_manifest(request)
    assert factories.observed_checkpointers == [None] * 14

    artifact = boot.boot(request, checkpointer=anchored_saver, runtime_ports=ports)
    assert artifact.manifest == manifest
    assert set(artifact.entrypoints) == EXPECTED_ENTRYPOINTS
    assert artifact.checkpointer_backend_id == anchored_saver.backend_id


def test_feature_context_rejects_foreign_contract(
    build_context: GraphBuildContext,
    select_probe: object,
    publish_probe: object,
) -> None:
    context = build_context.for_capability("assurance.intake")
    with pytest.raises(ContractOwnershipError):
        context.attempt(
            "assurance.generation.agent.api.plan.v1",
            semantic_node_id="intake.foreign-probe",
            activation=BusinessActivation.one_shot(),
            select=select_probe,
            publish=publish_probe,
        )
```

- [ ] **Step 2: Run tests to establish failure.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/boot/test_boot.py \
  packages/framework/graph-engine/tests/boot/test_boot_manifest_parity.py
```

Expected: missing `GraphEngineBoot`.

- [ ] **Step 3: Implement two explicit build phases.**

Authenticate sources before importing factories; resolve contract closure through a Boot-internal `ContractResolverPort`; build a per-owner context that exposes only `attempt` and `compile_subgraph`; invoke the fixed Product factory composition; dry-compile all roots without saver; compute entrypoint/Attempt digests; verify the supplied manifest at runtime; recompile roots with the real saver. Reject extra/missing roots, nondeterministic manifest output, source drift, model-schema digest drift, and revision mismatch. Never expose a resolved executor-bearing contract to Feature graph code.

Freeze these build seams:

```python
class CapabilityBuildContext(Protocol):
    owner_id: str
    def attempt(
        self,
        contract_id: str,
        *,
        semantic_node_id: str,
        activation: object,
        select: object,
        publish: object,
    ) -> object: ...
    def compile_subgraph(self, builder: StateGraph[object]) -> CompiledStateGraph: ...


class GraphBuildContext(Protocol):
    def for_capability(self, owner_id: str) -> CapabilityBuildContext: ...
    def compile_root(self, builder: StateGraph[object]) -> CompiledStateGraph: ...
```

`compile_subgraph` always supplies `checkpointer=None`; only `compile_root` receives the dry `None` or runtime anchored saver. Feature code cannot access the root saver.

- [ ] **Step 4: Add negative tests for ambient/SUT influence.**

Change environment variables, current directory and SUT files between two builds; topology and manifest remain identical. An attempted `.aa/` topology/factory/module override is rejected. A valid organization policy/config change may change ProductLock and therefore `GraphRevision`, but cannot change factory symbols, graph topology or entrypoint schema digests unless an authenticated public contract also changed. A factory attempting an unapproved source read fails its spy-context policy test.

- [ ] **Step 5: Verify Boot and commit.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/boot
uv run lint-imports
uv run pyright packages/framework/graph-engine/graph_engine/boot
git add \
  packages/framework/graph-engine/graph_engine/boot/__init__.py \
  packages/framework/graph-engine/graph_engine/boot/boot.py \
  packages/framework/graph-engine/tests/boot/test_boot.py \
  packages/framework/graph-engine/tests/boot/test_boot_manifest_parity.py
git commit -m "feat: add offline manifest and runtime graph Boot"
```

### Task 9: Implement the engine-neutral Assurance application lifecycle

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/application/application.py`
- Create: `packages/framework/graph-engine/tests/application/test_application_lifecycle.py`
- Create: `packages/framework/graph-engine/tests/application/test_application_interrupts.py`
- Create: `packages/framework/graph-engine/tests/application/test_application_concurrency.py`
- Modify: `packages/framework/graph-engine/graph_engine/application/__init__.py`

**Interfaces:** async `AssuranceApplication.start/start_and_run/run/resume/status`; `thread_id == invocation_id`; lease around every mutating call; typed single/multiple interrupt resume; top-level recursion limit and fence configuration.

- [ ] **Step 1: Write start/run/status characterization with a tiny real StateGraph.**

```python
async def test_start_pins_identity_and_run_uses_same_thread(application) -> None:
    started = await application.start(
        artifact=artifact,
        invocation_id="inv-1",
        entrypoint="execute",
        graph_input={"change_id": "chg-1"},
        runtime_context=context,
    )
    assert started.thread_id == "inv-1"
    assert started.revision_id == artifact.manifest.revision.revision_id
    result = await application.run(
        artifact=artifact,
        invocation_id="inv-1",
        runtime_context=context,
    )
    assert result.status == "completed"
```

- [ ] **Step 2: Test resume mapping and ambiguity.**

One pending human interrupt accepts a scalar only after schema validation. Two pending interrupts require `{interrupt_id: validated_value}`; scalar resume raises `AmbiguousResume`. A system interrupt accepts only its wakeup/reconciliation envelope and never a human action.

- [ ] **Step 3: Test runner conflict and recursion normalization.**

Two concurrent `run` calls produce one owner and one `RunnerConflict`. Configure the graph into deliberate infinite recursion and assert `InvocationStatus(status="failed", reason="graph_recursion_limit")`; separately exhaust a business counter and assert the graph's declared business terminal.

- [ ] **Step 4: Run failing tests, then implement the lifecycle.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/application/test_application_lifecycle.py \
  packages/framework/graph-engine/tests/application/test_application_interrupts.py \
  packages/framework/graph-engine/tests/application/test_application_concurrency.py
```

Expected before implementation: missing `AssuranceApplication`. Implement config with top-level `recursion_limit` and the exact configurable keys `{thread_id, assurance_revision_id, assurance_product_lock_digest, assurance_root_input_digest, assurance_fencing_token, assurance_initial_checkpoint}`. `start` creates the anchored initial state with `await graph.aupdate_state(..., as_node=START)` without executing a business node; `run` uses `await graph.ainvoke(None, ...)`. Acquire/release the async lease in `try/finally`; verify revision before checkpoint read/invocation; never infer next-node state from the journal.

- [ ] **Step 5: Verify and commit.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/application
uv run pyright packages/framework/graph-engine/graph_engine/application
git add \
  packages/framework/graph-engine/graph_engine/application/__init__.py \
  packages/framework/graph-engine/graph_engine/application/application.py \
  packages/framework/graph-engine/tests/application/test_application_lifecycle.py \
  packages/framework/graph-engine/tests/application/test_application_interrupts.py \
  packages/framework/graph-engine/tests/application/test_application_concurrency.py
git commit -m "feat: add fenced LangGraph application lifecycle"
```

### Task 10: Add real SQLite restart and initial identity-handshake coverage

**Files:**

- Create: `packages/products/assurance-product/assurance_product/sqlite_checkpointer.py`
- Modify: `packages/products/assurance-product/assurance_product/change_workspace.py`
- Create: `tests/product/test_sqlite_checkpointer.py`
- Create: `tests/product/test_langgraph_sqlite_restart.py`
- Create: `tests/product/test_langgraph_initial_handshake.py`
- Modify: `packages/products/assurance-product/assurance_product/__init__.py`
- Modify: `tests/product/test_change_workspace_paths.py`
- Modify: `tests/product/test_change_runtime_layout.py`

**Interfaces:** Product-owned async SQLite transaction adapter plus Assurance anchor tables/journal/lease; exact safe control subtree `.runtime/langgraph/`. This is constructed but not yet selected by production CLI.

- [ ] **Step 1: Write a process-reopen restart test.**

Build a tiny interrupting graph, start it with a file-backed async SQLite saver, close every connection/object, reconstruct the backend, resume the same `invocation_id`, and assert the pre-interrupt node is not duplicated and the final state is committed.

- [ ] **Step 2: Write initial checkpoint/journal fault cuts.**

Inject failure after the initial checkpoint row/outbox and after `InvocationStarted`; on reopen, recovery must either complete a matching pair or fail closed. It must never expose a resumable checkpoint with different Product lock/input/revision identity.

- [ ] **Step 3: Run tests before implementation.**

```bash
uv run pytest -q \
  tests/product/test_langgraph_sqlite_restart.py \
  tests/product/test_sqlite_checkpointer.py \
  tests/product/test_langgraph_initial_handshake.py
```

Expected: missing Product backend factory.

- [ ] **Step 4: Implement the local backend.**

Extend `ChangePaths` with `.runtime/langgraph`, `.runtime/langgraph/checkpoints.sqlite3`, `.runtime/langgraph/leases`, and `.runtime/langgraph/selections`; retain legacy `.runtime/invocations` untouched. `ChangeWorkspace.initialize()` permits exactly that real-directory subtree plus the database regular file, rejects symlinks/unexpected types, and remains compatible with legacy-only workspaces. Open only this absolute, runtime-owned control path; create the strict serializer, Product `SqliteCheckpointStoreTransaction`, anchoring journal adapter, observer registry, and local lease as one lifetime-managed async context. The Product store implements the Foundation transaction port directly against the pinned `langgraph-checkpoint-sqlite==3.1.1` table contract: checkpoint/pending-write SQL and its outbox insert execute on the same `aiosqlite` connection inside one explicit transaction. `await delegate.aput()` followed by a separate outbox insert is forbidden. Contract tests compare the SQL adapter against upstream saver behavior for put/get/list/pending writes. Set full synchronous durability. SQLite is explicitly single-host and is not presented as a multi-worker deployment backend.

- [ ] **Step 5: Run the complete foundation gate.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/boot \
  packages/framework/graph-engine/tests/persistence \
  packages/framework/graph-engine/tests/application \
  tests/product/test_langgraph_sqlite_restart.py \
  tests/product/test_sqlite_checkpointer.py \
  tests/product/test_langgraph_initial_handshake.py
uv run ruff check \
  packages/framework/graph-engine/graph_engine/boot \
  packages/framework/graph-engine/graph_engine/persistence \
  packages/framework/graph-engine/graph_engine/application \
  packages/products/assurance-product/assurance_product/sqlite_checkpointer.py
uv run pyright
uv run lint-imports
```

Expected: all exit `0`.

- [ ] **Step 6: Commit the local backend.**

```bash
git add \
  packages/products/assurance-product/assurance_product/__init__.py \
  packages/products/assurance-product/assurance_product/change_workspace.py \
  packages/products/assurance-product/assurance_product/sqlite_checkpointer.py \
  tests/product/test_change_workspace_paths.py \
  tests/product/test_change_runtime_layout.py \
  tests/product/test_sqlite_checkpointer.py \
  tests/product/test_langgraph_sqlite_restart.py \
  tests/product/test_langgraph_initial_handshake.py
git commit -m "feat: persist LangGraph Invocations in anchored SQLite"
```

## Foundation exit gate

- [ ] Run `uv run pytest -q packages/framework/graph-engine/tests/boot packages/framework/graph-engine/tests/persistence packages/framework/graph-engine/tests/application tests/product/test_sqlite_checkpointer.py tests/product/test_langgraph_sqlite_restart.py tests/product/test_langgraph_initial_handshake.py` and retain the passing output.
- [ ] Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, and `uv run lint-imports`; every command exits `0`.
- [ ] Inspect `git diff --check` and `git status --short`; only intentional commits exist in the isolated worktree.
- [ ] Request a code review before starting Feature factories. The review must explicitly check `put_writes`, strict deserialization, initial identity handshake, manifest parity, source-origin validation, and stale-runner fencing.
