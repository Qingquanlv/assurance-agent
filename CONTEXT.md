# Assurance Agent

Deterministic QA workflow automation for a target project: changes, evidence, gates, healing, and archive.

## Language

**Change**:
A named unit of QA work identified by a filesystem-safe `change_id`. While active it lives under the configured changes directory; after archive it lives under the configured archive directory.
_Avoid_: ticket, request, run (a run is an execution batch inside a change)

**ChangeLocation**:
The resolved absolute directory of a Change plus its source role (`changes` or `archive`).
_Avoid_: ChangeContext (reserved if we later mean schema+state+params), path tuple

**Active change**:
A Change whose directory exists under the configured `qa.changes` path and is eligible for write commands and the driver.
_Avoid_: unarchived (retro evidence-lifecycle wording; map at the retro adapter)

**Archived change**:
A Change whose directory exists under the configured `qa.archive` path; read-oriented evidence source for retro and history sampling.
_Avoid_: completed change (completion is a workflow status, not a directory role)

**Ledger**:
The append-only event log for a Change (`events.jsonl`), queried through one interface for sequence-aware lookups (filter / latest by type and attributes).
_Avoid_: event store, event bus, audit log (when meaning the query seam over `events.jsonl`)

**Fan-out item**:
One element of a phase-level `fan_out.each` list; the engine expands the base phase into a child phase `<base>[<item>]` per item at projection time, with `{item}` templating in `produces`.
_Avoid_: shard, batch (a run batch is an execution concept)

**Loop kind**:
A registered projector for a `loops:` entry (`healing`, `review_fix`), consumed by the engine only through the unified `LoopSnapshot` protocol.
_Avoid_: hardcoded loop, special-case loop

**Checkpoint**:
A driver main-loop boundary (one committed phase outcome or control action); the checkpoint payload is `workflow-state.yaml` + `events.jsonl`, `driver.json` is only the non-authoritative process pointer (`invocation_id` / `checkpoint_id` / `event_seq`).
_Avoid_: snapshot, savepoint (recovery is re-projection, not snapshot restore)
