# Foundation Task 8 Report: Build offline and runtime Boot paths

## Status

DONE_WITH_CONCERNS

## What I implemented

Two explicit Boot phases. `graph-engine` does not import adapters, capabilities, or product. Feature factories do not exist; tests authenticate Product-allowlisted symbols against disposable source trees and invoke those callables.

- `GraphEngineBoot.compile_manifest(request)` authenticates Product + Feature sources, then imports only the Product-supplied factory-ref tuple. It resolves a data-only contract closure through `ContractResolverPort`, invokes Feature factories into a generic owner-keyed mapping, invokes the Product factory, dry-compiles every root with `checkpointer=None`, and emits `GraphBuildManifest`.
- `GraphEngineBoot.boot(request, checkpointer, runtime_ports)` repeats that authentication/import/composition and recompiles roots with the supplied saver into `BootArtifact`. A compiled graph is never serialized. `runtime_ports` are required and are not stored on the artifact.
- `CapabilityBuildContext` exposes only `attempt` and `compile_subgraph` (`checkpointer=None`). `GraphBuildContext.compile_root` is the only path that receives the dry `None` or runtime saver. Feature code cannot reach the root saver. `attempt` binds a data-only `TaskAttemptContract` and never a `ResolvedAttemptContract`.
- Boot consumes `BootRequest.feature_factories` and owner-keyed Feature bundles. It contains no hard-coded Assurance keyword list. Extra/missing roots, source drift, organization `.aa/` topology/factory/module overrides, unapproved factory source reads, and expected-manifest revision mismatch fail closed.

## What I tested and test results

| Command | Result |
|---|---|
| Step 2 RED: the two new suites | 2 collection errors: `No module named 'graph_engine.boot.boot'` |
| Same two suites (GREEN) | 13 passed |
| Step 5: `uv run pytest -q packages/framework/graph-engine/tests/boot` | 36 passed |
| `uv run lint-imports` | 14 kept, 0 broken |
| `uv run pyright packages/framework/graph-engine/graph_engine/boot` | 0 errors, 0 warnings |
| `uv run ruff check` / `ruff format --check` on the committed paths | passed / formatted |

## TDD Evidence

### RED

Command:

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/boot/test_boot.py \
  packages/framework/graph-engine/tests/boot/test_boot_manifest_parity.py
```

Failing output:

```
==================================== ERRORS ====================================
___ ERROR collecting packages/framework/graph-engine/tests/boot/test_boot.py ___
...
E   ModuleNotFoundError: No module named 'graph_engine.boot.boot'
_ ERROR collecting packages/framework/graph-engine/tests/boot/test_boot_manifest_parity.py _
...
E   ModuleNotFoundError: No module named 'graph_engine.boot.boot'
=========================== short test summary info ============================
ERROR packages/framework/graph-engine/tests/boot/test_boot.py
ERROR packages/framework/graph-engine/tests/boot/test_boot_manifest_parity.py
!!!!!!!!!!!!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!!!!!!!!!!!!
2 errors in 0.64s
```

Why expected: tests were added before `graph_engine.boot.boot` existed. The brief names missing `GraphEngineBoot`.

### GREEN

Command:

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/boot/test_boot.py \
  packages/framework/graph-engine/tests/boot/test_boot_manifest_parity.py
```

Passing output:

```
.............                                                            [100%]
13 passed in 1.74s
```

Step 5:

```
....................................                                     [100%]
36 passed in 1.96s
Contracts: 14 kept, 0 broken.
0 errors, 0 warnings, 0 informations
```

Offline dry-compile records `[None] * 14` checkpointers. Runtime `BootArtifact.manifest` equals the offline manifest, entrypoint names are the 14 public roots, and `checkpointer_backend_id` matches the saver. A foreign `assurance.generation.agent.api.plan.v1` bind from an Intake context raises `ContractOwnershipError`. Ambient env/cwd/SUT files do not change the manifest. `.aa/` factory/topology/module files raise `OrganizationOverrideError`. A valid ProductLock configuration change updates `product_lock_digest` / `revision_id` but not factory symbols, topology, or entrypoint schema digests. Changing an authenticated public contract changes attempt-contract digests only. An unapproved factory source read raises `FactorySourcePolicyError`.

## Files changed

Committed in `2cdc94d3`:

- `packages/framework/graph-engine/graph_engine/boot/__init__.py` (modified)
- `packages/framework/graph-engine/graph_engine/boot/boot.py` (created)
- `packages/framework/graph-engine/tests/boot/test_boot.py` (created)
- `packages/framework/graph-engine/tests/boot/test_boot_manifest_parity.py` (created)

Not committed (out of scope): `.superpowers/sdd/progress.md`, this report, and the task brief.

## Self-review findings

- Sources are authenticated and byte-revalidated before and after factory import. Import uses the injected `SourceAuthenticator` (tests wrap `authenticate_factory_ref`).
- Feature graph code receives only `TaskAttemptContract`. Executor maps are attached to `BootArtifact` after compile.
- `compile_subgraph` always passes `checkpointer=None`. Only `compile_root` receives the dry `None` or runtime saver.
- Product factory composition is generic: Feature bundles are `{owner_id: bundle}` from the supplied factory-ref tuple.
- Commit used the explicit path list from the brief; no `git add .` / `-A`.
- `lint-imports` still forbids `graph_engine` → adapters/capabilities/product.

## Issues or concerns

Not blocking.

- Pytest reserves the fixture name `request`. The brief-verbatim parameter is therefore `boot_request: BootRequest`, then `request = boot_request` so the body stays `compile_manifest(request)` / `boot(request, ...)`.
- The frozen Protocol used `StateGraph[object]`; LangGraph 1.2 types `StateT` as `StateLike`, so the implemented seam is `StateGraph[Any]`. Behavior matches the brief.
- Runtime compile requires a real `BaseCheckpointSaver`. Tests use `InMemorySaver` with `backend_id = "memory"` rather than constructing a full `AnchoredCheckpointer` journal/store (that module was not in the commit list).
- Real Feature/Product factories and `aa compile` wiring are not in this task. Fake factories implement the Product allowlist symbols only.

## Review fix: host-saver identity seam

`AnchoredCheckpointer` now exposes `backend_id = "anchored"`. `test_runtime_boot_accepts_real_anchored_checkpointer` constructs a real `AnchoredCheckpointer` (`MemoryCheckpointStore` + `MemoryCheckpointAnchorJournal`) and drives `GraphEngineBoot.boot`. The `anchored_saver` fixture no longer hides the seam behind `_AnchoredSaver(InMemorySaver)`.

### TDD

RED (`uv run pytest -q packages/framework/graph-engine/tests/boot/test_boot.py::test_runtime_boot_accepts_real_anchored_checkpointer`):

```
E   graph_engine.boot.boot.BootValidationError: checkpointer backend id must be nonempty
FAILED .../test_boot.py::test_runtime_boot_accepts_real_anchored_checkpointer
1 failed in 0.97s
```

GREEN (`uv run pytest -q packages/framework/graph-engine/tests/boot/test_boot.py`):

```
....                                                                     [100%]
4 passed in 1.32s
```

`ruff check` / `ruff format --check` on the two Python paths: passed / formatted.
