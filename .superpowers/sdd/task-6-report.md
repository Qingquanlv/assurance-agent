# Task 6 Report — Candidate Validation (Resolution B)

## Status: DONE

**Resolution:** B (`.superpowers/sdd/task-6-resolution.md`)  
**Worktree tip at start of this pass:** `c7cb27a`  
Edit + test only (no git add/commit). Cursor-loop-helpers left alone. No `FixerAuthority`.

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
| Receipt fold | Optional when no validator; **required** when started event names a validator |

## Fail-closed fixes (this pass)

| Gap | Fix |
|-----|-----|
| P1 cases from live FS / empty list | `load_case_documents_from_snapshot` — snapshot/CAS only; empty → `CandidateValidationError` |
| P2 plan live FS fallback | `load_plan_text_from_snapshot` — snapshot miss → `CandidateValidationError` / `invalid_output` |
| P2 malformed case JSON/YAML | Parse failures → `CandidateValidationError` (scheduler settles `invalid_output`) |
| P2 fold without receipt | Named `precommit_validator` + success without receipt → `LedgerIntegrityError`; CAS/identity verify on project |
| Step 2 mutations | summary-only, omit write, wrong `case_ids` covered |

## Deferred to Task 8

- `FixerAuthorityV1` and proposal path-subset surface
- Full `codegen_fix_candidate/v1` algorithm + Step 3 mutations
- Codegen-fix scheduler no-commit cases
- Diff-safety predicates depending on authority/proposal

## Key files

**Created (prior land)**
- `assurance_agent/workflow/graph/precommit.py`
- `assurance_agent/verification/generated_entries.py`
- `tests/unit/workflow/graph/test_precommit_validation.py`
- `tests/unit/verification/test_generated_entries.py`

**Modified (this pass)**
- `assurance_agent/workflow/graph/precommit.py` — snapshot loaders + `bind_receipt_to_success_event`
- `assurance_agent/workflow/graph/scheduler.py` — use snapshot loaders; stamp `precommit_validator` on started
- `assurance_agent/workflow/core/graph_events.py` / `models.py` — `precommit_validator` on started / task projection
- `assurance_agent/workflow/graph/checkpoint.py` — required receipt fold + CAS bind verify
- `tests/unit/workflow/graph/test_precommit_validation.py`
- `tests/unit/workflow/graph/test_checkpoint.py`

## Dark-ship

- Packaged `execution-contracts.yaml` unchanged; all `precommit_validator` remain `None`
- Selecting `codegen_fix_candidate/v1` on a contract fails at load with “not implemented until Task 8”
- Contracts with no validator stay byte-compatible and emit no receipt
- Note: mid-segment plan globs (`change:plans/api-*.md`) are not `path_covers`-provable under `declared_only`; validator-bearing contracts need exact / `**` reads so plan+cases enter the input snapshot

## Verify

```text
uv run pytest -q \
  tests/unit/workflow/graph/test_precommit_validation.py \
  tests/unit/verification/test_generated_entries.py \
  tests/unit/workflow/graph/test_contracts.py \
  tests/unit/workflow/graph/test_scheduler.py \
  tests/unit/workflow/graph/test_checkpoint.py \
  tests/unit/workflow/graph/test_ingest.py
→ 188 passed

uv run ruff check … → All checks passed
uv run pyright → 0 errors, 0 warnings, 0 informations
```

## Notes for integrator

- Do not stage `benchmark/.../cursor-loop-helpers.sh` or its test.
- Suggested commit message: `fix(graph): fail-closed precommit plan/case snapshot and receipt fold`
- Task 8 should flip `codegen_fix_candidate/v1` from load-reject to implemented without renaming the ID.
