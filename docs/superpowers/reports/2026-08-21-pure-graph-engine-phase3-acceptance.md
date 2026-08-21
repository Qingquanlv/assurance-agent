# Phase 3 acceptance report

**Date:** 2026-08-22  
**Branch:** `codex/pure-graph-engine-phase3-spec`  
**Baseline HEAD:** `543f040` (`fix(agent-runtime): observe rebinding lock and delivered request`)  
**Task 18 commit:** recorded after this report is committed  
**Phase 3 complete:** **no**

Live OpenCode and live Cursor single-item runs did not reach graph-ledger terminal success. Deterministic fakes remain the crash-safety oracle. This report is ready for coordinator two-axis review (Task 18 Step 9). It does not merge, push, or cut over `aa`.

## Commands and pass counts

| Gate | Command | Result |
|---|---|---|
| Import contracts | `uv run lint-imports` | 17 kept, 0 broken |
| Ruff | `uv run ruff check .` | all checks passed |
| Format | `uv run ruff format --check .` | pre-existing: `packages/graph-engine/tests/runtime/test_activity_models.py` would reformat on HEAD; no new format debt in Task 18 files |
| Pyright | `uv run pyright` | 0 errors |
| Phase 3 tests | `uv run pytest packages/graph-engine/tests packages/agent-runtime-contracts/tests packages/agent-runtime-opencode/tests packages/agent-runtime-cursor/tests tests/agent_runtime -q` | **1381 passed, 1 skipped** |
| Packaging smoke | `bash scripts/packaging_smoke_test.sh` | OK; aa wheel METADATA and install omit `agent-runtime-opencode` / `agent-runtime-cursor` / `agent-runtime-fixture` |
| Agent-runtime wheel smoke | `bash scripts/agent_runtime_wheel_smoke_test.sh` | must run after this commit (`git archive HEAD`); working-copy preview of source/version pins is in the version-audit table |
| Full repo | `uv run pytest -q` | **5 failed, 8124 passed, 13 skipped** — 4 are pre-existing `tests/unit/benchmark/test_opencode_openai_loop.py` (`benchmark/vue-fastapi-admin` missing); 1 is pre-existing `tests/architecture/test_graph_engine_boundaries.py` (`packaging` not in `ALLOWED_ROOTS`, reproduces on `543f040`). No new Phase 3 failure |
| Live OpenCode | `bash benchmark/agent-runtime-phase3/run-opencode.sh` | **exit 1** (profile unavailable) |
| Live Cursor | `bash benchmark/agent-runtime-phase3/run-cursor.sh` | **exit 1** (secret unset) |

Phase 3 package baseline before this task: **1377 passed, 1 skipped**.  
This task: **1381 passed, 1 skipped**. Delta **+4**:

1. `test_checkpoint_document_requires_schema_version_2`
2. `test_committed_live_manifest_pins_the_required_release_fields`
3–4. `test_live_scripts_consume_only_the_committed_manifest_and_fail_closed` × two scripts

## Version hard-cut audit

| Surface | Before | After | 1.0 alias? | Evidence |
|---|---|---|---|---|
| `ENGINE_API_VERSION` | `"1.0"` | `"2.0"` | no | `packages/graph-engine/graph_engine/__init__.py`; `packages/graph-engine/tests/test_primitives.py` |
| Checkpoint document + digest payload | no `schema_version` | `Literal["2"]`; missing/`"1"` rejected | no | `packages/graph-engine/graph_engine/runtime/checkpoint.py`; `test_checkpoint_document_requires_schema_version_2` |
| Runtime event kinds | v2 golden | unchanged v2 golden | no | `packages/graph-engine/tests/runtime/activity-events-v2.golden.json` |
| Invocation lock schema | `"2"` / `engine_api` `"1.0"` | `"2"` / `engine_api` `"2.0"` | no | regenerated `packages/graph-engine/tests/composition/invocation-lock-v2.golden.json` (digest `7a07475a052758dab020a0712fd4da02f018aa186fcc4e54d2baa7add06eea86`) |
| Host wire schema | `"1"` | `"1"` (not this cut) | n/a | `TASK_HOST_WIRE_SCHEMA_VERSION` |
| `AgentRunRequest.schema_version` | `"1"` | `"1"` (contract, not engine API) | n/a | contracts models |
| Plugin/product static `engine_api` | `"1.0"` | `"2.0"` | no | adapter, fixture, and toy declaration JSON |
| Phase 2 prototype resume fixture | none found | none found | n/a | `rg -n -i 'prototype resume\|phase2.*invocation\|resume fixture'` over engine/fixture/agent-runtime tests |

Code that already reads `ENGINE_API_VERSION` updated automatically. Dependency tests that encoded “current engine is 1.x” now use `>=2,<3` / `engine is 2.0`.

## Import contracts

Registered root packages: `agent_runtime_contracts`, `agent_runtime_opencode`, `agent_runtime_cursor`, `agent_runtime_fixture`.

Added:

- `graph-engine-provider-neutral` (brief)
- `adapter-independence` (brief)
- `runtime-adapters-no-old-runtime` (brief)
- `contracts-wheel-independence` — contracts must not import adapters, fixture, or Assurance
- `fixture-downward-only` — fixture must not import adapters or Assurance
- `graph-engine-independent` now also forbids `agent_runtime_fixture`

No ignore edges were added.

Classified exception: contracts **may** import `graph_engine.plugin_api` (spec §7.2). A forbidden `agent_runtime_contracts → graph_engine` contract would require an ignore edge; that was not added.

## Source scans

Exact commands and classification:

```bash
rg -n -i 'opencode|agent.?run|persona|openai|anthropic' packages/graph-engine/graph_engine --glob '*.py'
# 0 hits

rg -n -w 'prompt|persona|provider_model|AgentRun|tool_call' packages/graph-engine/graph_engine --glob '*.py'
# 0 hits

rg -n -i 'cursor' packages/graph-engine/graph_engine --glob '*.py'
# FoldCursor / integer fold cursor only — generic ledger cursor, not Cursor the product

rg -n -w 'session' packages/graph-engine/graph_engine --glob '*.py'
# ImportPlanSession only — authenticated import isolation, not a provider session

rg -n -w 'provider' packages/graph-engine/graph_engine --glob '*.py'
# Plugin/product provider modules only — Phase 2 composition vocabulary

rg -n '^(from|import) (assurance_agent|assurance_kernel)' \
  packages/agent-runtime-contracts packages/agent-runtime-opencode/agent_runtime_opencode \
  packages/agent-runtime-cursor/agent_runtime_cursor examples/agent-runtime-fixture/agent_runtime_fixture --glob '*.py'
# 0 hits

rg -n 'from graph_engine|import graph_engine|agent_runtime_' assurance_agent --glob '*.py'
# 0 hits — aa still uses the old runtime

rg -n 'canary-secret|sk-secret|Bearer sk-' \
  packages/graph-engine/graph_engine packages/agent-runtime-contracts/agent_runtime_contracts \
  packages/agent-runtime-opencode/agent_runtime_opencode packages/agent-runtime-cursor/agent_runtime_cursor \
  examples/agent-runtime-fixture/agent_runtime_fixture --glob '*.py'
# 0 hits in production sources
```

No ignore comments were added to hide hits.

## Fixture / wheel pins

From the committed manifest (`benchmark/agent-runtime-phase3/manifest.json`):

| Pin | Value |
|---|---|
| Fixture source digest | `2e57c0238c8191843933340a3de59e7522b39a1eaf405032bb1f33b05bc252d1` |
| Fixture wheel sha256 | `820c1059f0ca50780949900d36a15e9966d1da42ab5879177e0b99fdb1562d67` |
| Canonical request digest | `05f48e44df6defbf5fb077afebd17519c0d5ffbc54d9bea93c9901100c026d7f` |
| Result schema digest | `56e341c9d4dd6ccaac2bc2038dad7539ff03d3895c3d66d02b24172d4ba9141e` |
| Expected `result.json` digest | `86f85b3898c11dad47aba8ee2bf20e913ed08dccde985a1c6c5b3fefc66d6c79` |
| OpenCode adapter source digest | `d2cee676fd6ca906692be505e07f172da8bae1a5096e4c4aa57c8ba85445424c` |
| Cursor adapter source digest | `22d65265309ac9d9ca587eeb8aadd9153e5c80de55bdba761c629ab3851e431c` |

## Live-run status

Scripts consume only the committed manifest. They exit non-zero on a missing or drifted prerequisite. They do not rewrite the manifest, choose fallback values, or report fake success.

### OpenCode — unavailable (fail closed)

- Pinned endpoint `http://127.0.0.1:4096` was reachable.
- `/global/health` reported `{"healthy":true,"version":"1.18.4"}`, matching the pinned external tool version.
- `/config` is the live OpenCode **user** config, not `AcceptedOpenCodeProfile` (`opencode-http-v1` / `caller-message-id-v1` / `conflict-on-body-drift`).
- Script exited 1. No graph-ledger run. No replay.

### Cursor — unavailable (fail closed)

- Pinned regular file `/Users/lvqingquan/.local/share/cursor-agent/versions/2026.08.11-e8db854/cursor-agent` existed.
- File digest and `--version` (`2026.08.11-e8db854`) matched the manifest.
- Pinned secret env `CURSOR_API_KEY` was unset. Script refused to invent credentials and exited 1.
- No graph-ledger run. No replay.

Deterministic conformance fakes remain the crash-safety oracle (`tests/agent_runtime`).

## Spec §20 criteria

| Criterion | Status | Evidence |
|---|---|---|
| Phase 2 remains green except versioned hard-cut goldens | met | lock golden regenerated for `engine_api` 2.0; event v2 golden unchanged |
| Engine has no adapter/provider/business implementation | met | source scans above; import contracts |
| `TaskHandler.execute()` is the only normal dispatch | met | Tasks 1–7 / `plugin_api.py` |
| Prepare/start/lease are one batch | met | Task 5 tests |
| Bind / cancel / terminal / adoption | met | Tasks 2, 4, 6, 7 |
| Reconcile before reclaim | met | Task 6 |
| Workspace identity preserved | met | Tasks 5–6 |
| Lost/drifted workspace blocks recovery | met | Task 5 |
| No ledger writer to plugins | met | Task 4 port |
| Host confinement + secret port | met | Tasks 3, 7 |
| Versioned host transport / receipts | met | Tasks 3, 7 |
| Effects/checkpoints are not activity authority | met | Task 2 / checkpoint tests |
| OpenCode create/rediscovery/admission/SSE | met (deterministic) | Tasks 9–12 |
| No `SessionEvent` copy | met | repo scan |
| Cursor confinement / stream-json / receipt | met (deterministic) | Tasks 13–15 |
| Shared conformance, honest capabilities | met | Task 16 |
| Neutral fixture rebinding + isolated wheels | met (deterministic) | Task 17; wheel smoke after this commit |
| **Provider-live OpenCode + Cursor + replay** | **not met** | live scripts exit 1 |
| Endpoint/executable/source locked | met for pins; live not completed | manifest + adapter tests |
| No credentials in durable outputs | met | canary scans; production sources clean |
| Import-linter / ruff / pyright / Phase 3 tests / packaging | met | table above |
| Full-repo pytest | revalidated; no new Phase 3 failure | see command table |
| No `aa` cutover / no Assurance move | met | `assurance_agent` has no `graph_engine` / `agent_runtime_*` imports; aa not in adapter METADATA |

## Ready for review

Task 18 Step 9 (independent Standards + Spec review against the Phase 2 merge base and this spec) is coordinator work. Do not merge or push from this task.

---

See also Task 18 implementation report: `.superpowers/sdd/2026-08-21-pure-graph-engine-phase3-agent-runtime-adapters/task-18-report.md` (not staged).
