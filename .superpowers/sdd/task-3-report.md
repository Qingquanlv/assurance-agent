# Task 3 Report: Lever 2 — treat v4/v5 resume like v6 (skip topology audit)

**Status:** DONE  
**Branch:** `chore/slimming-dead-paths`  
**Commit:** `d177795` — `fix: skip v4/v5 topology audit on resume`

## What was implemented

v4/v5 same-definition resume now takes the v6 short-circuit: `allowed=True`, no topology receipt.

- `evaluate_resume_compatibility` returns the v6 decision for every schema version. The legacy audit body was removed from this function; `audit_topology_for_resume`, receipt helpers, and `assess_remaining_work` stay in the file.
- Runtime wrappers `_compatibility_decision_readonly` and `_enforce_resume_compatibility` always return `allowed=True` without loading a bundle, receipt, or calling evaluate’s audit path.
- `supersede.py`, `aa workflow supersede`, and `evaluate_supersede_eligibility` were not changed. `test_supersede.py` still constructs blocked decisions for eligibility unit tests.
- Same-definition resume still fail-closes on digest drift via `_resolve_bundle` / `assert_live_semantic_compatibility` (unchanged).
- README + release-note sentences that claimed resume still blocks with `legacy_commit_safety_semantics_unbound` were updated. The reason string remains in the docs corpus (supersede eligibility). `test_docs_contract.py` did not need assertion changes.

## TDD Evidence

### RED

Command:

```
uv run pytest tests/unit/workflow/graph/test_resume_compatibility.py::test_v4_and_v5_skip_legacy_topology_audit_like_v6 -v
```

Result: **FAILED** (exit 1)

```
E           AssertionError: assert False is True
E            +  where False = ResumeCompatibilityDecision(..., allowed=False, reason='legacy_commit_safety_semantics_unbound', ..., event_schema_version=4, ...).allowed
```

Failure was the missing short-circuit on v4 (pending codegen fixture), not a typo.

### GREEN

After the unconditional skip in `evaluate_resume_compatibility`:

```
uv run pytest tests/unit/workflow/graph/test_resume_compatibility.py::test_v4_and_v5_skip_legacy_topology_audit_like_v6 -v
```

Result: **1 passed, 1 warning in 0.56s** (exit 0)

Covering suite:

```
uv run pytest \
  tests/unit/workflow/graph/test_resume_compatibility.py \
  tests/unit/workflow/graph/test_supersede.py \
  tests/unit/test_docs_contract.py \
  tests/integration/test_graph_runtime.py \
  tests/integration/test_graph_runtime_faults.py \
  -v
```

Result: **112 passed, 1 warning in 55.65s** (exit 0)

The one warning is pre-existing and unrelated:

```
assurance_agent/workflow/graph/models.py:70: UserWarning: Field name "schema" in "CompiledWorkflow" shadows an attribute in parent "BaseModel"
```

Lint on touched Python: `ruff check` clean, `ruff format --check` clean, `pyright` 0 errors.

## Files changed

Committed (this task only):

| Path | Action |
|------|--------|
| `assurance_agent/workflow/graph/resume_compatibility.py` | unconditional skip in `evaluate_resume_compatibility` |
| `assurance_agent/workflow/graph/runtime.py` | wrappers always no-op |
| `tests/unit/workflow/graph/test_resume_compatibility.py` | new skip test; v4/v5 evaluate tests expect `allowed=True` / `receipt is None` |
| `README.md` | resume line no longer claims the unbound resume block |
| `docs/release-notes/2026-08-four-layer-assurance.md` | same-definition resume skip; reason kept for supersede |

Not committed (out of scope): `.superpowers/sdd/progress.md`, `.superpowers/sdd/task-3-report.md`

Not modified: `supersede.py`, `evaluate_supersede_eligibility`, `test_docs_contract.py`, `test_supersede.py`

## Self-review

**Completeness:** v4/v5/v6 pending-codegen fixtures now `allowed=True` with `new_receipt is None`. Runtime wrappers never call the audit path. `audit_topology_for_resume` remains. Supersede CLI/eligibility unchanged.

**Quality / discipline:** Evaluate signature kept (callers still pass bundle/receipt args). Audit helpers left in `resume_compatibility.py` rather than a large delete. Runtime `_load_topology_compatibility_receipt` / `_legacy_profile_reconstructable` are now unused; left in place to avoid an extra delete in this slice.

**Testing:** New test failed first on v4 `legacy_commit_safety_semantics_unbound`, then passed. Evaluate-based v4/v5 block tests were rewritten to the skip assertions. Receipt round-trip still uses `build_topology_compatibility_receipt` (evaluate no longer emits receipts). Eligibility tests still construct a blocked decision.

**Findings:** none to fix in production.

## Concerns

None that affect correctness. Leftover unused runtime helpers (`_load_topology_compatibility_receipt`, `_legacy_profile_reconstructable`) and `_selected_layers` can be deleted in a later slimming slice.
