# Task 22 Report: Complete release gates, ownership closure, Phase 5 handoff, and Phase 6 deletion inventory

**Status:** DONE (Important #1 fixed)
**Branch:** `codex/pure-graph-engine-phase3-spec`
**HEAD before:** `00b444a`
**Close commit:** `0fd03a6fb204ffc2eace0eca2bde85d8c5da3e07` — `docs: close phase 4 capability extraction`
**Fix commit:** `b4be45ec93be5584eb1fef93b3f18cc9ada509d9` — `fix(assurance): prove Task 22 operation and artifact rows`

## What was implemented

No `aa` cutover. No production product manifest or full graph. No Phase 2/3 public-interface change. `seed_ownership_items` was not used to regenerate the ledger.

- `test_phase4_ownership_is_fully_verified` — extracted migrate kinds `{operation, skill, persona, validator, effect, hook, artifact, schema, resource}` must be `verified` with a verification pointer; every verified row has a pointer; migrate `module`/`callable` stay `planned` / `verification is None`; `semantic_pins` stays `delete_phase6` / planned / owner null / new_id null.
- Freeze tests follow the Task 18 pattern: operations and validators/effects now assert `verified` plus a real pytest node.
- Ledger edited in place (547 items, no keys dropped, verified hook rows untouched):
  - 70 operations, 57 shipped artifacts, 6 validators, 3 effects → `verified` with row-specific pytest nodes
  - 7 unshipped quality resources reclassified `delete_phase6` (dashboard scripts, `failure-classification.yaml`, explore JSON samples are not in the quality wheel)
  - 17 unshipped artifacts reclassified `delete_phase6` / owner null / new_id null / planned (not named in a live `contribute()` schema ID or wheel contract export)
- `phase5-handoff.md` — live IDs/digests from the six wheels + Task 19/20 fixtures; `ENGINE_API_VERSION` `"2.0"`; `binding_data` exactly `{execution, request_policy_digest, request_config_digest}`; quality/improvement filesystem finalize still `del context`; known concerns with owner + target phase.
- `phase6-deletion.txt` — 286 sorted paths including `ProductHooks`, `operations_catalog.py`, old skill/persona resources, artifact-model wrappers, duplicate domain implementations. Benchmark datasets/scorers excluded.
- README `开发与测试` and umbrella architecture spec state wheels exist and pass isolation; `aa` still defaults to the legacy product; no production manifest/graph; Phase 5 next; Phase 6 hard cut.

## Remaining planned migrate rows

- 178 `module`
- 4 `callable` (`CaseSourceClaim`, `CaseSourceVerification`, `CaseMinimumCoverageReview`, `CaseReviewAuthoring`)
- `semantic_pins` hook: `delete_phase6` / planned
- 17 leftover artifacts: `advisory`, `issue_triage_advice`, `metrics_nightly_document`, `issue_evidence_manifest`, `issue_reconcile_status`, five discovery leftovers, `improvement_reconcile_outbox_v1`, and six retro signal/evidence-slice types

## Important #1 fix (live operation / artifact proof)

Review: operation and artifact `verified` pointers were one per-wheel identity or schema-bytes node. That did not prove the row (and several artifact types were not in the schema set).

- Added `tests/phase4/test_ownership_live.py`:
  - `test_migrate_operation_is_live_handler[legacy_id]` loads the owning wheel's live `PluginProvider.contribute(...)` handler IDs and asserts `item.new_id` is in that set.
  - `test_migrate_artifact_is_live_contract[legacy_id]` asserts the type is named in a live contributed schema ID or a live wheel contract/model export. Unshipped types are not parametrized (they are no longer migrate).
  - `ids=` is the `legacy_id`, so the node contains the row id.
- Each migrate operation/artifact `verification` is that parametrized node, e.g. `tests/phase4/test_ownership_live.py::test_migrate_operation_is_live_handler[operation:run-tests]`. Shared `test_*_source_identity` / schema-bytes tests remain but are no longer the ledger proof.
- `test_phase4_ownership_is_fully_verified` now requires every verified pointer to look like `path::test_name` or `path::test_name[id]`. For `operation` and `artifact`, the `[id]` / test source must contain that row's `legacy_id` or `new_id`. Hooks stay on the Task 18 shared fixture.
- `tests/phase4/ownership.py` leftover override: dashboard scripts and the 17 artifact leftovers are `delete_phase6` in the collector/`_artifact_assignment`, so `seed_ownership_items` would not restore migrate.
- Ledger edited in place (547 items, no keys dropped, verified hook rows untouched). `phase5-handoff.md` unchanged: the seam table is the same.

Shipped artifacts such as `observation_document`, `failure_analysis`, and `issue_candidate_document` stay migrate/verified because they are live quality contract exports even without a dedicated schema ID.

## TDD evidence

### RED (Important #1)

```
uv run pytest tests/phase4/test_ownership_live.py tests/phase4/test_ownership_ledger.py::test_phase4_ownership_is_fully_verified -q
```

FAILED before ledger/pointer edits: `test_migrate_artifact_is_live_contract[advisory]` (`artifact_type_is_live` is false) plus 16 other unshipped migrate artifacts; `test_phase4_ownership_is_fully_verified` rejected shared wheel-identity pointers that did not name the row.

### GREEN

```
uv run pytest tests/phase4/test_ownership_ledger.py tests/phase4/test_ownership_live.py -q
```

**137 passed** (10 ledger tests + 70 live operations + 57 live artifacts).

Focused static gate for this fix:

```
uv run ruff check tests/phase4 packages/assurance-intake packages/assurance-generation packages/assurance-execution packages/assurance-healing packages/assurance-quality packages/assurance-improvement
uv run ruff format --check tests/phase4
uv run lint-imports
git diff --check
```

All pass. `lint-imports`: 29 kept, 0 broken.

## Focused gate (Step 5)

**2098 passed, 1 skipped**

Phase 1–3 four-package subset: **1376 passed, 1 skipped**. Plan text 1369/1 is stale; `c060933` + later Phase 4 did not change those four packages in this task.

## Static gates (Step 6)

| Command | Result |
|---|---|
| `uv run ruff check .` | pass |
| `uv run ruff format --check .` | pre-existing fail: `packages/graph-engine/tests/runtime/test_activity_models.py` identical on `c060933` |
| `uv run pyright` | 0 errors |
| `uv run lint-imports` | 29 kept, 0 broken |
| `git diff --check` | pass |

## Full pytest (Step 7)

**5 failed, 8858 passed, 13 skipped**. All five exist on merge-base `c060933` (same sources / same missing gitignored SUT files). Not Phase-4-owned.

- `tests/architecture/test_graph_engine_boundaries.py::test_graph_engine_imports_no_product_packages`
- `tests/unit/benchmark/test_opencode_openai_loop.py::test_benchmark_test_scaffold_is_an_importable_package`
- `tests/unit/benchmark/test_opencode_openai_loop.py::test_shared_e2e_login_uses_locators_present_in_the_real_dom`
- `tests/unit/benchmark/test_opencode_openai_loop.py::test_shared_fuzz_fixtures_use_the_live_sut_without_invented_app_imports`
- `tests/unit/benchmark/test_opencode_openai_loop.py::test_generated_http_scaffold_matches_observed_response_shapes`

## Smokes (Step 8, committed HEAD `0fd03a6fb204ffc2eace0eca2bde85d8c5da3e07`)

- `bash scripts/assurance_capability_wheel_smoke_test.sh` — exit 0, `assurance capability wheel smoke test: OK`
- `bash scripts/packaging_smoke_test.sh` — exit 0, `packaging smoke test: OK`

Untracked `.superpowers/` reports did not fail packaging smoke.

## Scope scans (Step 9)

No production `graph_engine.products` / `ProductManifest` matches.

Production mentions (pre-existing, negative / path deny-list only):

- `assurance_quality/contracts/sufficiency.py` docstring: does not import `assurance_agent.evidence`
- `assurance_intake/contracts/review.py` forbids writes under `.opencode/`

All other scan hits are tests or characterization negative assertions.

## Concerns

- Quality/improvement finalize still `del context` (Task 21 uncovered filesystem cells) → Phase 5.
- Module/callable rows remain planned by Task 1 freeze → Phase 6.
- `semantic_pins` and unshipped quality leftovers are `delete_phase6`.
- Pre-existing `ruff format` fail on graph-engine activity-model tests; pre-existing architecture `packaging` allowlist miss; pre-existing benchmark SUT-file tests.
