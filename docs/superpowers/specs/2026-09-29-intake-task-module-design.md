# Intake Task Module and Contract-Only Models

## Intent

Make the Intake wheel match the user's five-part mental model. `task.py` is
the real task-definition entrance, not a re-exporting manifest: it defines the
four Agent Task classes with their `@before`, `run`, `@after`, and optional
`@finally_` phases, the Agent and deterministic Task execution contracts,
and the existing Product-facing `FEATURE` value. `plugin.py` registers trusted
handlers, schemas, resources, and validators. `contracts/` defines input,
raw-result, and output data models. `operations/` implements prepare,
finalize, and deterministic work. `graphs/` owns LangGraph topology,
selectors, publishers, and routes.

This is an Intake-only structural migration. Preserve all contract IDs,
canonical digests, handler IDs, node IDs, graph edges, retry/timeout values,
authorized read/write paths, source authentication, OpenCode execution, and
Kernel sealing behavior.

## Directory and interface

`assurance_intake/task.py` replaces `feature.py` as the public Product import.
It owns `IntakeTask`, `ExploreTask`, `CaseDesignTask`, `CaseReviewTask`, their
execution-contract catalog, deterministic Task contract catalog, output-route
templates, `IntakeGraphs`, and `FEATURE`. The phase objects remain injected by
Product; task definitions do not create an OpenCode client. Remove the old
task-class-only `operations/agent_tasks.py` and `operations/case_design.py`
after updating their consumers. Do not leave forwarding shims at the old
paths.

`contracts/` retains Pydantic/FrozenModel request, raw-result, and output
types. Local field/model validators and private pure helpers required to
define a model stay with it. Runtime execution policy (`AttemptRetryPolicy`,
`AttemptTimeoutPolicy`, `ResourceClaims`, `AgentExecutionContract`, and
`TaskAttemptContract`) moves out of `contracts/attempts.py` into `task.py`.
Move non-model filesystem reading, plan sealing/decoding, planning-fact
collection, workflow decisions, and document normalization into the existing
`operations/` or `graphs/` modules according to their callers. Remove
`contracts/attempts.py` once consumers use `task.py`; keep the `contracts`
package's public exports data-model-only.

`operations/agent_skills.py` currently combines four prepare handlers,
common Agent request construction, evidence authentication, and case-design
repair computation. Split along those actual reasons to change, with common
request assembly and evidence authentication reused by the handlers. Keep
the handler IDs and `plugin.py` registrations unchanged. `resources/` remains
the wheel's packaged prompt/persona/schema data, and `validators/` remains
the commit-validator implementation; they are supporting directories omitted
from the five-part conceptual tree, not files to delete.

`graphs/factory.py` retains `add_attempt_node` and all topology. It imports
`IntakeGraphs` from `task.py`; it does not move `activation/select/publish`
into `task.py`. Product imports Intake's `FEATURE` and bundle type from
`task.py` while the other five wheels remain unchanged.

## Dependency and failure handling

The plugin descriptor needs canonical Attempt contract references. After
moving the catalog to `task.py`, `plugin.py` must obtain those references only
when `descriptor()` or `contribute()` runs, rather than importing `task.py`
at module load time. `task.py` can then import `IntakePlugin` and construct
`FEATURE` without an import-time cycle. Keep one authoritative catalog and
fail closed on any source or contract digest drift. Input-validation and
evidence-authentication failures must retain their existing TaskOutcome
codes/retryability; no new write occurs outside the authorized prepare root.

## Verification

Update old Intake import paths in Product, graphs, tests, and documentation.
Tests must assert the four Task classes and `FEATURE` come from `task.py`,
`contracts/` no longer exports execution policy, plugin-declaration digests
remain identical, all existing Intake graph exports and Product roots compile,
and prepare/finalize/repair behavior remains unchanged. Run targeted Intake
and Product tests, the repository lint/type/import gates, full pytest, and
installed-wheel smoke scripts. The unrelated existing Execution persona
declaration failure in the capability smoke is tracked separately, not
silently treated as an Intake migration failure.

## Alternatives rejected

Keeping Task classes under `operations/` and merely renaming `feature.py` to
`task.py` would preserve the shallow entrance the user explicitly rejected.
Moving prompt/schema bytes or validator implementations into `plugin.py`
would erase useful internal modules and conflict with wheel packaging; the
plugin registers them rather than containing them.
