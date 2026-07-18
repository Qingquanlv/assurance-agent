# ADR 0002: ChangeLocation resolves by role preference; both-roots coexistence is normal

## Status

Accepted — 2026-07-16

## Context

`ChangeLocation` (`change_location.py`) is the intended interface for "where is
this Change, and in which role?". Before this decision it was abandoned mid-stack:
callers took `.path` and discarded the handle, then re-derived the project root
with `change_dir.parents[2]` (`gates._project_root`, `operations._ensure_healing_available`,
`retro/nightly/driver`) and re-implemented `qa.changes` / `qa.archive` resolution
with copied `./`-stripping (`retro/archive_reader`, `retro/nightly/phase_a`) or
hardcoded `qa/changes` / `qa/archive` paths (`risk/paths`, `risk/context`).

Two problems followed:

1. **`parents[2]` is a latent config bug.** It assumes `change_dir` is exactly
   `<root>/qa/changes/<id>` (three levels). A non-default `qa.changes`
   (e.g. `./work/ch`) silently resolves the wrong project root.

2. **Ambiguity that isn't.** `aa-archive` **copies (does not move)** process
   artifacts to `qa/archive/<id>/` and **does not delete** `qa/changes/<id>/`
   (`aa-archive/SKILL.md`: "Copy (do not move)"; "Do not delete … preserved as a
   reference"). So after archive a Change exists under **both** roots — the
   normal steady state. The old `resolve_change_any` treated coexistence as a
   fatal `ChangeAmbiguousError`, which crashed `aa retro nightly collect` on
   every archived Change and forced retro to build a parallel archive-first
   adapter plus a regression test documenting the crash.

## Decision

- `resolve_change(project_root, change_id, *, prefer="active"|"archive")` is the
  single resolver. `prefer="active"` returns the writeable Change under
  `qa.changes` (error if only an archived copy exists); `prefer="archive"`
  returns the archived copy if present, else falls back to the active copy.
- Coexistence under both roots is **normal** and never raises. `resolve_change_any`
  and `ChangeAmbiguousError` are removed. Future reviews must not reintroduce a
  both-roots "ambiguity" check on the grounds that it is "safer" — coexistence is
  by construction, not a conflict.
- Project-root and root-directory resolution go through `ChangeLocation` /
  `changes_root()` / `archive_root()`, which read `qa.changes` / `qa.archive`
  from config. `parents[2]`-style depth reverse-derivation is banned.
- Orchestration and driver code that needs the project root (`gates`, `engine`,
  `review_fix_episode`, `healing_episode`, the driver loop's `PhaseContext`)
  takes a `ChangeLocation`, not a bare `change_dir: Path`. Pure leaf functions
  that only read files under the Change directory keep `change_dir: Path`.

## Consequences

- One place owns Change layout policy; non-default `qa.changes` / `qa.archive`
  now works end-to-end (resolve → gates → status → selection → retro → risk).
- retro's `unarchived` / `archive` vocabulary stays a thin mapping at the retro
  adapter (`resolve_change_dir`), as CONTEXT.md prescribes.
- Unrelated to ADR-0001: driver phase outcomes still commit in-process via
  `apply_phase_outcome`; this ADR does not touch the write boundary.
- `phase_prompt.py`'s literal `qa/changes/<id>/` instruction to the phase agent
  is a separate behavioral contract and is intentionally out of scope here.
