# Assurance Agent

Deterministic QA automation for a target project: changes, evidence, gates, healing, and archive.

## Language

**Change**:
A named unit of QA work identified by a filesystem-safe `change_id`. While active it lives under the configured changes directory; after archive it lives under the configured archive directory.
_Avoid_: ticket, request

**ChangeLocation**:
The resolved absolute directory of a Change plus its source role (`changes` or `archive`).
_Avoid_: ChangeContext, path tuple

**Active change**:
A Change whose directory exists under the configured changes path and may be written and may start an Invocation.
_Avoid_: unarchived

**Archived change**:
A Change whose directory exists under the configured archive path; a read-oriented evidence source for Retro and history sampling.
_Avoid_: completed change

**Workflow**:
The product-owned route a Change follows: entrypoints, subgraphs, gates, and interrupts.
_Avoid_: Walk, pipeline, plugin flow, graph

**Entrypoint**:
A named start on a Workflow.
_Avoid_: command, slice, mode, packaging entry point

**Invocation**:
One start of a Workflow at an Entrypoint.
_Avoid_: session, driver loop, checkpoint, `aa run`

**Run**:
One test batch inside a Change.
_Avoid_: Batch, Invocation, `aa run`

**Capability**:
One entry in the system-under-test capability catalog.
_Avoid_: plugin (when meaning a catalog leaf)

**Capability wheel**:
One of the six installed Python packages that contribute handlers, validators, schemas, and resources: Intake, Generation, Execution, Healing, Quality, and Improvement.
_Avoid_: feature (as a package role), client (as a package role)

**Ledger**:
The append-only event log of an Invocation.
_Avoid_: event store, event bus, the archived `events.jsonl` at the Change root

**Retro evidence slice**:
An immutable, typed, run-scoped projection of Issue, Workflow, or Eval evidence for one resolved Retro window. It is analysis input, not a new Ledger and not historical Retro output.
_Avoid_: `aa eval`, eval harness

**Retro signal**:
A domain analyzer's validated pattern claim whose references resolve only inside its evidence slice. Runtime code supplies the slice digest; analyzers do not author integrity fields.

**Retro context**:
The mechanically assembled v3 envelope containing the three domain statuses, source manifest, integrity result, and all validated signals for one Retro run. Improvement Candidates must reference both context signals and immutable source evidence.
