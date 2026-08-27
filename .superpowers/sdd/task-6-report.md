# Task 6 Report — Candidate Validation (Resolution B)

## Status: DONE

**Resolution:** B (`.superpowers/sdd/task-6-resolution.md`)  
**Worktree tip at start of this pass:** `c7cb27a`  
Edit + test only (no git add/commit). Cursor-loop left alone. No `FixerAuthority`.

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

## Fail-closed fixes

| Gap | Fix |
|-----|-----|
| P1 cases from live FS / empty list | `load_case_documents_from_snapshot` — snapshot/CAS only; empty → `CandidateValidationError` |
| P2 plan live FS fallback | `load_plan_text_from_snapshot` — snapshot miss → `CandidateValidationError` / `invalid_output` |
| P2 malformed case JSON/YAML | Parse failures → `CandidateValidationError` (scheduler settles `invalid_output`) |
| P2 fold without receipt | Named `precommit_validator` + success without receipt → `LedgerIntegrityError`; CAS/identity verify on project |
| P1 interrupt success without receipt | Same-ns interrupt runs `_run_precommit_if_needed` before success; receipt on event or `invalid_output` (no bare success). Nested bubbles still skip freeze. |
| Step 2 mutations | summary-only, omit write, wrong `case_ids` covered |

### Interrupt approach chosen

**Prefer precommit-before-interrupt-success** (option 1): same-namespace interrupt still appends `task_attempt_succeeded` (resume routing), so a named validator makes that event commit-shaped. Scheduler now:

1. Freezes same-ns interrupt write-sets (nested `checkpoint_ns != task.checkpoint_ns` still skip freeze).
2. Runs `_run_precommit_if_needed` before appending interrupt success.
3. Writes `candidate_validation_receipt_id` on success, or settles `invalid_output` without `task_attempt_succeeded` / `graph_interrupted` when validation cannot pass.

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

**Modified**
- `assurance_agent/workflow/graph/precommit.py` — snapshot loaders + `bind_receipt_to_success_event`
- `assurance_agent/workflow/graph/scheduler.py` — snapshot loaders; stamp validator; interrupt precommit + same-ns freeze
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
→ 190 passed

uv run ruff check … → All checks passed
uv run pyright → 0 errors, 0 warnings, 0 informations
```

## Notes for integrator

- Do not stage `benchmark/.../cursor-loop-helpers.sh` or its test.
- Suggested commit message: `fix(graph): fail-closed precommit snapshot, receipt fold, and interrupt success`
- Task 8 should flip `codegen_fix_candidate/v1` from load-reject to implemented without renaming the ID.

---

## Fix round 3 — close executor bash grammar

Executor bash was still a “contains the execution view” token filter. Four reviewer cwd/root mutations were ALLOW against the installed plugin.

### RED

```
uv run pytest tests/phase5/test_opencode_staging_boundary.py::test_executor_cwd_root_mutation_bypasses_are_denied_by_installed_plugin -q
```

Result: **4 failed** (exit 1). Each case returned plugin ALLOW (`returncode == 0`) instead of DENY (`23`):

- `python -c "…os.rename(getcwd()…)" qa/changes/CH-1/.staging/execution/api`
- `PYTHONDONTWRITEBYTECODE=1 uv run --isolated python -c "…os.rename(getcwd()…)" qa/changes/CH-1/.staging/execution/api`
- allowlisted pytest `--rootdir` view + `<(python -c "…os.rename(getcwd()…)")`
- allowlisted pytest `--rootdir` view + `>hijack`

Failure was the missing grammar, not a typo.

### GREEN

Plugin `assertExecutorShell` now matches only the live executor `_BASH_RULES` invocations (`uv run pytest…`, `npx playwright…`, npm/pnpm/locust forms). It rejects `python -c`, process substitution, redirects, backticks, `$()`, `;`/`&&`, and cwd-relative output files. Author/reviewer bash stays denied.

```
uv run pytest tests/phase5/test_opencode_staging_boundary.py::test_executor_cwd_root_mutation_bypasses_are_denied_by_installed_plugin tests/phase5/test_opencode_staging_boundary.py::test_executor_execution_view_shell_is_allowed -q
```

Result: **5 passed** (exit 0)

Covering set (round 2 + staging boundary):

```
uv run pytest \
  tests/phase5/test_opencode_staging_boundary.py \
  tests/phase5/test_agent_execution_contracts.py \
  packages/agent-runtime-cursor/tests/test_filesystem_sandbox.py \
  packages/agent-runtime-opencode/tests/test_binding_authority.py \
  packages/agent-runtime-contracts/tests/test_models.py \
  -q --deselect tests/phase5/test_agent_execution_contracts.py::test_transient_agent_provider_failure_retries_the_skill_node
```

Result: **113 passed, 1 skipped, 1 deselected** (exit 0)

Kept: binding stamp, `artifact_write` mediation, Cursor sandbox, `;`/`&&` denials, `_BASH_RULES` strings.
