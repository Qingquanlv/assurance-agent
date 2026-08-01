# Task 6 Report — Candidate Validation (Resolution B)

## Status: DONE

**Resolution:** B (`.superpowers/sdd/task-6-resolution.md`)  
**Worktree tip at start:** `7aedd29`  
Edit + test only (no git add/commit). Cursor-loop-helpers left alone.

## Scope applied

| Item | Result |
|------|--------|
| `PrecommitValidationContext` / `CandidateValidationReceiptV1` | Implemented in `precommit.py` (strict, canonical output-key order, CAS verify) |
| `generated_entries.py` mapping extraction | API/E2E Test Function Mapping; Fuzz + Schema Acquisition; Performance Task Mapping |
| `generated_files_candidate/v1` | Fully implemented (manifest ↔ write-set ↔ snapshot ↔ plan/case mapping) |
| `codegen_fix_candidate/v1` | Known closed ID; **load-rejects** / dispatch-rejects until Task 8 |
| Contract field `precommit_validator` | Defaults `None`; unknown IDs reject at load |
| Packaged catalog | Asserts neither validator selected |
| Scheduler hook | After freeze, before `task_attempt_succeeded`; no-commit on `invalid_output` |
| Receipt fold | Optional historical field on success event + `TaskProjection` |

## Deferred to Task 8

- `FixerAuthorityV1` and proposal path-subset surface
- Full `codegen_fix_candidate/v1` algorithm + Step 3 mutations
- Codegen-fix scheduler no-commit cases
- Diff-safety predicates depending on authority/proposal

## Key files

**Created**
- `assurance_agent/workflow/graph/precommit.py`
- `assurance_agent/verification/generated_entries.py`
- `tests/unit/workflow/graph/test_precommit_validation.py`
- `tests/unit/verification/test_generated_entries.py`

**Modified**
- `assurance_agent/workflow/graph/contracts.py`
- `assurance_agent/workflow/graph/models.py`
- `assurance_agent/workflow/core/graph_events.py`
- `assurance_agent/workflow/graph/checkpoint.py`
- `assurance_agent/workflow/graph/finalize.py` (docstring seam note)
- `assurance_agent/workflow/graph/scheduler.py`
- `assurance_agent/workflow/graph/ingest_catalog.py` (dormant generated-files `resolve_model`)
- `assurance_agent/verification/generated_files.py` (model lookup helpers)
- `tests/unit/workflow/graph/test_contracts.py`
- `tests/unit/workflow/graph/test_checkpoint.py`
- `tests/unit/workflow/graph/test_ingest.py`

## Dark-ship

- Packaged `execution-contracts.yaml` unchanged; all `precommit_validator` remain `None`
- Selecting `codegen_fix_candidate/v1` on a contract fails at load with “not implemented until Task 8”
- Contracts with no validator stay byte-compatible and emit no receipt

## Verify

```text
uv run pytest -q \
  tests/unit/workflow/graph/test_precommit_validation.py \
  tests/unit/verification/test_generated_entries.py \
  tests/unit/workflow/graph/test_contracts.py \
  tests/unit/workflow/graph/test_scheduler.py \
  tests/unit/workflow/graph/test_checkpoint.py \
  tests/unit/workflow/graph/test_ingest.py
→ 182 passed

uv run ruff check … → All checks passed
uv run pyright → 0 errors, 0 warnings, 0 informations
```

## Notes for integrator

- Do not stage `benchmark/.../cursor-loop-helpers.sh` or its test.
- Suggested commit message: `feat(graph): validate generated-file candidates before commit`
- Task 8 should flip `codegen_fix_candidate/v1` from load-reject to implemented without renaming the ID.
