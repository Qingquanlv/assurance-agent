# Task 15 Report — Atomically Activate V6 Assurance Contracts, Manifests, and Healing Topology

## Status

**DONE** (edit + verify only; no git add/commit — controller owns commits)

Tip at start: `773b381`. Left unrelated dirty files alone:
- `benchmark/vue-fastapi-admin/benchmark/cursor-loop-helpers.sh`
- `tests/unit/benchmark/test_cursor_loop_helpers.py`

## What Flipped (activation cutover)

| Surface | Activation |
| --- | --- |
| **16 assurance skills** | Canonical `## Inputs` / `## Outputs` / `## State Authority` (+ plan-fixer `## Runtime Context`); `owner: graph_ledger`; `agent_state_writes: forbidden`; no host ledger / workflow-state / heal CLI prose |
| **16 execution contracts** | `read_isolation: declared_only`; closed reads/writes/authorization; codegen `precommit_validator: generated_files_candidate/v1`; codegen-fixers `codegen_fix_candidate/v1` |
| **Codegen hard outputs** | All four layers require summary **+** `*-generated-files.json` manifest |
| **Artifact registry + ingest catalog** | Exact path pins for `api|e2e|fuzz|performance_generated_files_v1` (+ healing intent/authority/approval/safety entries retained) |
| **Healing topology** | `allocate → fixer-authority-ready → fixer-proposal-approval → (interrupt/record loop) → fixer-dispatch → fix-* → record-* → fixer-join → combine-fixer-safety → safety`; join `all_active` over **record-api/record-e2e** |
| **Durable effects** | `healing_allocation/v2`, `fixer_proposal_approved/v1`, `heal_record_apply/v2` on packaged ops |
| **Compile gates** | `compile_packaged_workflow` runs **assurance_conformance** + **healing_conformance** (no longer dark-ship only); fails closed when contracts missing |
| **Runtime** | `AgentHandler` persona via `ASSURANCE_PERSONA_BY_TARGET`; plan-fixer Runtime Context sidecar bound by scheduler; Task 4 deferred Runtime Context **activated here** |
| **Personas** | `aa-test-author` deny-first; private roots + `tests/testdata/**` only (not broad `**tests/**`) |
| **Capability atoms** | API/E2E codegen preconditions require `capabilities_present(...)` |
| **Benchmark compatibility** | Frozen manifests for eval-sample-001; L2-{api,e2e,fuzz,performance}-codegen-seed + L3-run-seed/done output lists updated; `fixture-lock.json` recomputed |

## Prior Resolutions Applied Here

- **Task 4** skill Runtime Context activation: plan-fixer skills + scheduler sidecar binding live.
- **Task 8** dormant healing ops: reachable via packaged topology (`fixer-authority-ready`, `fixer-dispatch`, `record-*`, `combine-fixer-safety`, approval interrupt/record).
- **Conformance validators**: packaged compile gate, not optional dark-ship.

## Files Touched (activation unit; uncommitted)

Packaged resources: workflow-schema, execution-contracts, ingest-artifact-catalog, 16 skills, 3 personas.

Runtime: `artifacts/registry.py`, `workflow/graph/compiler.py`, `handlers/agent.py`, `handlers/operation.py`, `scheduler.py`, `task_inputs.py`, `assurance_personas.py`.

Benchmark fixtures: 4 new `codegen/*-generated-files.json`, 6 tier YAMLs, `fixture-lock.json`.

Tests updated as needed for activation (registry, contracts, packaged compile, topology, canonical schema v2 healing walk, skills slimming/fuzz matrix, durable effects, etc.).

## Verification (Step 9)

```text
uv run pytest -q <Step 9 suite>   # 486 passed
uv run ruff check assurance_agent tests/helpers_assurance_contract.py tests/unit/verification tests/unit/workflow/graph
                                  # All checks passed
uv run pyright                    # 0 errors
uv run lint-imports               # 6 kept, 0 broken
```

## Explicitly Not Done

- No `git add` / commit (per controller instruction).
- No cursor-loop helper changes.
- No pending-tier / scorer / dataset activation beyond L2/L3 complete-history compatibility manifests.
