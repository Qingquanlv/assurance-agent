# Archive

Capability-owned archive skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Project a typed archive summary from authenticated quality, review, and execution
artifacts. Schema truth is `assurance_improvement.contracts` for `ArchiveResultV1`.

## Inputs

### required

- locked change identity and invocation id
- authenticated quality report digest
- selected artifact manifest and pre-archive tree digest

## Outputs

### required

- structured `ArchiveResultV1`
- `archive_status` is `archived` or `archived_with_warnings`
- `archive_digest` echoes the locked pre-archive tree digest

## Rules

- Consume the quality report `issues` section. If `issue_risk` is anything other
  than `clear` or absent, `archive_status` must be `archived_with_warnings`.
- Issue state never blocks archive. It only changes archive-status wording.
- Review decisions must already be `pass` for every applicable gate present in
  the locked inputs. Do not invent a pass from conversation.
- Execution `FAIL` blocks archive unless an authenticated override is present.
- Do not hand-edit orchestration snapshots or ledgers.
- Copy process artifacts into the archive tree. Do not delete the change tree.
- Do not run product CLI commands or rewrite orchestration snapshots.
- Do not emit provider session transcripts or secret-bearing diagnostics.
- Use the locked execution binding from the prepare request.
- Return the typed result and stop.
