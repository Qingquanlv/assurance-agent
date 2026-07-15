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
