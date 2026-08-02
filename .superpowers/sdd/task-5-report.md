# Task 5 Report — Resolve Evidence by Pinned Physical Identity

## Status: DONE

Branch tip at start: `2597e06`. Edit + test only (no commit).

## What Was Implemented

### `assurance_agent/workflow/graph/evidence_paths.py` (new)
- `ResolvedEvidencePath` (`StrictWireModel`) with `physical_relpath`, `repo_relpath`, `ownership`, canonical `logical_aliases`.
- Pure `resolve_evidence_path(logical_path=, tree_roots=, current_change_repo_path=)` — no FS open, no symlink follow; reuses workspace lexical normalization (`_assert_safe_prefix`, `_physical_for`, `_resolutions`).
- Ownership by nested physical containment with precedence `current_change > repo > project`; nesting/precedence disagreement → `ambiguous_containment`.
- Typed `EvidencePathError` with closed codes:
  `missing_pinned_roots`, `base_tree_roots_mismatch`, `path_traversal`, `absolute_path`, `another_change`, `ambiguous_containment`, `unowned_path`, `unknown_root`, `invalid_logical_path`, `unsafe_root_prefix`.
- `pinned_write_set_roots` / `verify_write_set_base_tree_roots` for current evidence validation over write-set-bound maps.

### `assurance_agent/workflow/graph/workspace.py`
- `WriteSet.base_tree_roots: dict[str, str] | None = None` (historical-compatible).
- `TreeStore.tree_roots(tree_id) -> Mapping[str, str]` read-only accessor.
- `freeze_write_set` copies verified base-tree roots into the payload **before** hashing `write_set_id`.
- `load_write_set` accepts historical manifests without roots; when roots are present, requires exact agreement with `tree_roots(base_tree_id)`.

## Coverage (Steps 1–2)

Positive:
- Four private roots (`tests/api|e2e|fuzz|perf`) via `project:…` with project+repo → `.`: one physical path, repo-rel path, both aliases, ownership `repo`.
- Current-change output → ownership `current_change`.

Fail-closed typed codes:
- missing pinned map; roots mismatch at load/`verify_write_set_base_tree_roots`; traversal; absolute paths; another change; ambiguous nested-vs-precedence containment; unowned path.
- Symlink-like pinned prefix cannot rewrite lexical physical identity.
- Historical write set without roots loads, but `pinned_write_set_roots` fails `missing_pinned_roots`.

## Dark-ship

No packaged contracts, validators, fixer authority, eval, export, or scorer switches.

## Left alone

- `benchmark/vue-fastapi-admin/benchmark/cursor-loop-helpers.sh`
- `tests/unit/benchmark/test_cursor_loop_helpers.py`

## Verify

```bash
uv run pytest -q tests/unit/workflow/graph/test_evidence_paths.py tests/unit/workflow/graph/test_workspace.py
# → 71 passed

uv run ruff check assurance_agent/workflow/graph/evidence_paths.py \
  assurance_agent/workflow/graph/workspace.py \
  tests/unit/workflow/graph/test_evidence_paths.py
# → All checks passed

uv run pyright
# → 0 errors, 0 warnings, 0 informations
```

## Files ready to stage (integrator owns commit)

- `assurance_agent/workflow/graph/evidence_paths.py`
- `assurance_agent/workflow/graph/workspace.py`
- `tests/unit/workflow/graph/test_evidence_paths.py`
- `tests/unit/workflow/graph/test_workspace.py`
- `.superpowers/sdd/task-5-report.md` (optional ledger)

Suggested commit message: `feat(graph): resolve evidence by pinned physical path`

## Concerns

None blocking. Exact `EvidencePathError` code strings were derived from Task 5 failure-mode inventory (design D15 does not enumerate the Literal set); rename is cheap if a later consumer inventory pins different tokens.
