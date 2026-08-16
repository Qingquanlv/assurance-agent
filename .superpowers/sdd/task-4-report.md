# Task 4 Report: Lever 3 — ledger state_values for import-checkpoint and gate finalize

**Status:** DONE  
**Branch:** `chore/slimming-dead-paths`  
**Commit:** `187bf3b` — `fix: read gate state from the ledger projection`

## What was implemented

Import-checkpoint and live gate finalize now use folded `GraphProjection.state_values` when an invocation projection exists. YAML `workflow-state.yaml` is fallback only when there is no projection (import) or fold raises `LedgerIntegrityError` (finalize).

- Extracted `state_values_for_import(context, projection)` in `checkpoint.py`. If `projection is not None`, returns `dict(projection.state_values)`; otherwise `_state_values_from_change(context)`.
- `validate_import` calls that helper instead of always loading YAML.
- `_reevaluate_gate` fallback uses the same helper (optional `projection=`), instead of `_state_values_from_change` directly.
- `_attach_gate_report` folds first; on success passes the projection into `_state_values_for_gate`, which starts from `projection.state_values` then merges `result.state_updates`. YAML path only on `LedgerIntegrityError`.
- `_state_values_from_change` kept for no-projection import. `aa decide` / `configure_workflow_params` / skills still use `set_state`. Checkpoint snapshot format unchanged. `durable_effects.py` and `replay_binding.py` untouched.

## TDD Evidence

### RED

Command:

```
uv run pytest tests/unit/workflow/graph/test_state_values_from_projection.py -v
```

Result: **ERROR** (exit 2) — collection failed because the helper did not exist:

```
E   ImportError: cannot import name 'state_values_for_import' from 'assurance_agent.workflow.graph.checkpoint'
```

Failure was the missing function, not a typo.

### GREEN

After extracting `state_values_for_import` and wiring import/finalize:

```
uv run pytest tests/unit/workflow/graph/test_state_values_from_projection.py -v
```

Result: **2 passed, 1 warning in 0.53s** (exit 0)

Covering suite (brief + `test_import_checkpoint.py`):

```
uv run pytest \
  tests/unit/workflow/graph/test_state_values_from_projection.py \
  tests/unit/workflow/graph/test_checkpoint.py \
  tests/unit/workflow/graph/test_derive_graph_state.py \
  tests/unit/workflow/graph/test_import_checkpoint.py \
  -v
```

Result: **65 passed, 1 warning in 1.89s** (exit 0)

Also ran finalize tests during implementation: `test_finalize_and_child_stop.py` + `test_finalize_review_validation.py` with the files above — **110 passed, 1 warning in 2.90s**.

The one warning is pre-existing and unrelated:

```
assurance_agent/workflow/graph/models.py:70: UserWarning: Field name "schema" in "CompiledWorkflow" shadows an attribute in parent "BaseModel"
```

Lint on touched Python: `ruff check` clean, `ruff format --check` clean, `pyright` 0 errors.

## Import-checkpoint tests grep

Grep `test_*import*checkpoint*` found:

- `tests/unit/workflow/graph/test_import_checkpoint.py` — `state_values={}` only as an empty gate-eval fixture; does not assume YAML `phases` win when a projection exists. No update.
- Other hits were eval/CLI import-checkpoint flows whose gates read review JSON, not `state.phases`.

## Files changed

Committed (this task only):

| Path | Action |
|------|--------|
| `assurance_agent/workflow/graph/checkpoint.py` | `state_values_for_import`; `validate_import` + `_reevaluate_gate` fallback |
| `assurance_agent/workflow/graph/finalize.py` | fold-first `_attach_gate_report`; `_state_values_for_gate(projection=)` |
| `tests/unit/workflow/graph/test_state_values_from_projection.py` | new RED/GREEN tests |

Not committed (out of scope): `.superpowers/sdd/progress.md`, `.superpowers/sdd/task-3-report.md`, this report.

## Self-review

- Spec: projection wins; YAML only with no projection / fold integrity error. Helper signature matches the brief verbatim.
- Did not migrate `aa decide` / `configure_workflow_params` / skills off `set_state`.
- Did not change checkpoint snapshot to offset-only.
- Did not delete `durable_effects.py` or `replay_binding.py`.
- `_state_values_from_change` remains the YAML fallback.
- Passing `projection=` into `_reevaluate_gate` is defensive: `validate_import` already supplies `state_values`.

## Concerns

None blocking. Live gates with a successful fold will no longer see YAML `phases` written by `aa decide`; that is the slice. Those CLI paths still write YAML via `set_state` and are left for a later lever.
