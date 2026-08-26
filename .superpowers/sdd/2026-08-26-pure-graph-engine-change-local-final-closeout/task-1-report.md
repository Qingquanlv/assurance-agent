# Task 1 Report — prerequisite admission

## Status: DONE

Implemented a read-only admission checker and frozen evidence that preserves
the upstream Change-local verdict as `not_accepted`. The only admissible
downstream state is `accepted_with_waivers`; `complete` is rejected.

## Baseline

- Committed HEAD: `e7129433fd66aeb05ddc843030703eb5cb3ae5ef`.
- Pre-existing status: 79 lines, SHA-256
  `94660d908c1cb7ef4d90283259c46eb959deebf912ff69083aac143b344742a7`.
- Authenticated upstream commit: `8d8d1ba0a755398ae1edccd6c55cff986c9e6729`.
- Authenticated plan/spec/acceptance SHA-256 values are in `admission.json`.

## RED/GREEN evidence

- RED: focused admission tests failed because `admission.json` and
  `residual-disposition.json` were absent.
- GREEN: `uv run pytest tests/phase6/test_remaining_phase_admission.py -q`
  passed `4`; the standalone checker accepted the waiver admission.
- Focused Ruff check and format check passed; focused Pyright reported
  `0 errors, 0 warnings, 0 informations`.

## Scope

No upstream Change-local plan, progress, report, or implementation file was modified.
All pre-existing dirty capability/product/benchmark files and untracked
results/tmp remain outside this task's staging set.

## Fix Round 1

- RED: `uv run pytest tests/phase6/test_remaining_phase_admission.py -q`
  produced `3 failed, 9 passed, 1 warning`; the duplicate waiver and both
  multiline/reversed unresolved-priority cases were incorrectly accepted.
- Added exact waiver cardinality/uniqueness validation. The checker now rejects
  every `P1`/`P2` marker in the upstream report fail-closed, including
  `Open P1` and multiline `P2` status forms, and turns malformed Pydantic
  evidence into a rejection rather than an uncaught parser failure.
- GREEN: `uv run pytest tests/phase6/test_remaining_phase_admission.py -q`:
  `15 passed, 1 warning`; `uv run python scripts/check_remaining_phase_admission.py
  --admission .superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/admission.json`:
  `remaining-phase admission accepted_with_waivers`.
- `uv run ruff check tests/phase6 scripts/check_remaining_phase_admission.py`:
  `All checks passed!`; `uv run ruff format --check ...`: `4 files already
  formatted`; `uv run pyright tests/phase6 scripts/check_remaining_phase_admission.py`:
  `0 errors, 0 warnings, 0 informations`.
