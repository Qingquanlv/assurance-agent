# Assurance workspace

Private uv workspace for the installed Assurance graph product. The publishable
wheels are `graph-engine`, six capability packages, `assurance-product`, and the
OpenCode adapter wheel. `aa` is owned by `assurance-product`.

There is no long-running service. Develop and test through `uv run`.

## Commands

```bash
uv sync --dev
uv run aa --help
uv run pytest -v
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run python scripts/build_wheels.py --check
uv run python scripts/build_wheels.py --package assurance-intake --out-dir dist
bash scripts/graph_engine_smoke_test.sh
bash scripts/assurance_capability_wheel_smoke_test.sh
bash scripts/assurance_product_wheel_smoke_test.sh
```

### Python plugin registration and generated declarations

Each wheel's `plugin.py` publishes handlers, schemas, resources, and validators.
Each wheel's agent ops live in `ops/<op>/`, and `plugin.py` derives its handler map
and resource manifest from the ops router. Execution has no agent phase; its two
task contracts stay hand-written because their ids and shared handler do not match
the router's task shape. The Feature bundle lives in
`feature.py` and topology in `graphs/`. Do not hand-edit `plugin-declaration.json`:
it is generated from the provider's `descriptor()`, including its attempt-contract
digests.

Run the build entry point from the repository root after `uv sync --dev`:

```bash
# Regenerate declarations, then build the selected wheel(s).
uv run python scripts/build_wheels.py --package assurance-intake --out-dir dist
# Omit --package to build all workspace members; repeat it to select several.

# Refresh declarations for editable development without building wheels.
uv run python scripts/build_wheels.py --declarations-only
# CI checks for missing/stale generated files without modifying them.
uv run python scripts/build_wheels.py --check
```

Commit regenerated declarations with Python changes. They remain checked in so
isolated builds and source review have the same static metadata. Direct
`uv build` only packages existing files; use the entry point above to refresh
them automatically. All three packaging smoke scripts use this entry point.

Generation imports only this trusted workspace's explicitly declared plugin
entry points, with source coordinates checked against `pyproject.toml`. It does
not scan the SUT or generate metadata at runtime. Installed-wheel loading still
validates the static dependency closure before loading providers, checks live
registrations against the declaration, and authenticates wheel bytes. Product
declarations and deployment binding generation are unchanged.

Healing proposes a bounded repair to existing generated tests, applies it automatically,
and reruns execution. It requires no human repair approval or separate approval file.
Source authentication, generated-file scope, assertion checks, and healing budgets
still apply. Deploy the rebuilt wheels; runs pinned to an older graph revision must
finish with that revision or be restarted.

Nodes generate business records in Python: Agent operations use their `after`
hook, and ordinary tasks stage records in their handler. Attempt validates,
seals, commits, and recovers those artifacts together with the node's files.
There is no separate Effect/Intent stage or global business-registration ledger;
different Attempts may record the same business operation. Improvement delivery
records retain the improvement id, version, target identity, and receipt.
Runs created with the former Effect protocol must finish on their original
wheels or restart with the rebuilt product; old Effect journals are not migrated.


Attempt execution separates progression from domain transactions. The kernel is
an entrypoint that composes a runtime and per-call handlers. The runtime chooses
explicit actions from the journal-derived phase and completed local prerequisites;
it returns waiting or unknown results without polling. The handlers own resource
authorization, activity dispatch or reconciliation, workspace validation and
promotion, and terminal/release proofs. Each handler controls its own journal
writes and durability barriers. Phase is not another persisted checkpoint.

This applies Pi Durable's separation of task progression and handler-controlled
commits within the existing Python engine; it does not install Pi Durable.
Technical retries remain in the node factory, business repair remains in Flow,
and production restart keeps the single-worker regeneration rules below. The
lower-level same-Attempt recovery API retains its existing journal and proof checks.
See the [Attempt runtime design](docs/superpowers/specs/2026-10-07-attempt-runtime-separation-design.md).

`aa compile`, `aa start`, `aa run`, `aa status`, `aa resume`,
`aa bindings build`, `aa lock show`, and `aa retro show` operate on an installed
product plus an explicit binding wheel and project configuration tree.

Delivery is `aa run` to achieved.

The `full` and `intake` entrypoints create one frozen assurance plan after
Explore. Their public input supplies candidates rather than a selected family:

```json
{
  "schema_version": "1",
  "change_id": "CH-123",
  "requirement": "Protect the account recovery journey",
  "run_mode": "implement",
  "candidate_test_families": ["api", "e2e"],
  "case_delta_paths": ["qa/cases/account-recovery/case.yaml"],
  "capability_leafs": ["account.recovery.complete"],
  "capability_catalog": {"resource_id": "assurance.product.configuration.capability-catalog", "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "product_policy": {"resource_id": "assurance.product.configuration.product-policy", "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
  "data_knowledge": {"resource_id": "assurance.product.configuration.data-knowledge", "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"},
  "allowed_artifact_paths": ["qa/.qa.yaml", "qa/cases", "qa/fixtures", "qa/proposal.md", "qa/requirement.md", "qa/results", "qa/tests"],
  "budgets": {"review_rounds": 2, "coverage_rounds": 2, "healing_rounds": 1, "execution_retries": 1}
}
```

Standalone `case` and `execute` entrypoints are not supported. Start with `full`
(or `intake` for preparation and case review only); use `aa resume` to recover an
existing invocation. Public input does not accept `resolved_plan_ref`. Internal
case and execution subgraphs still use the plan produced earlier in the workflow.

Execution admission excludes other Workers in the same canonical workspace across
application, CLI, bootstrap and operator start/run/resume paths. Independent
workspaces can execute separately; status remains read-only. Canonical target
reservation precedes worktree creation and runtime seeding, including direct and
reuse-directory callers, and transfers to the lifetime lock without an admission
gap. Each reservation is `target.parent/.aa-preparation-locks/target.name` on the
target filesystem. The native basename lets that filesystem apply its own case
and Unicode alias rules and preserves its maximum filename length. The
`.aa-preparation-locks` namespace (and its casefold spelling variants) is reserved
control infrastructure and cannot itself be an execution target. Creating a run
requires permission to create that lock infrastructure.
Soft stop requests
pause at a durable boundary. Explicit force stop uses:

```sh
uv run aa operator stop --json --project-dir PATH --run-id ID --force
uv run aa bootstrap stop --run-dir RUN_DIR --force
```

Force stop confirms recorded Worker identities, owned children and external calls
have ended, then releases only that execution's resource grants. Shared OpenCode
services are never signaled. Confirmed stop exits 0; stopping exits 20; unconfirmed stop exits 40 and
keeps replacement execution blocked. Cancellation acknowledgment alone is
insufficient. Resume clears the stop request under the lifecycle guard.

On Linux, stop requires native `pidfd_open` and `pidfd_send_signal`: handles are
opened before creation-identity validation, and every signal, including child
termination and escalation, uses those verified handles. Unsupported or denied
pidfds keep admission closed; numeric PID/group signaling is not a fallback.
Dedicated groups are frozen and rescanned with a bounded deadline before force
termination; an unauthenticated remaining group or unconfirmed freeze fails
closed. Vanished process entries trigger bounded fresh scans before freeze
confirmation. Confirmed frozen groups receive KILL directly through their verified
handles and remain frozen through exit verification. A naturally exited owner's existing lifetime-lock and external
activity confirmation remains required. This is the existing owned process-group
containment boundary, not supervision of processes that escaped that group.
On macOS, repeated native creation checks remain, but the subsequent numeric
signal has a residual PID-reuse race; macOS does not provide the Linux
identity-bound signaling guarantee.

Production resume continues completed graph checkpoints and regenerates unfinished
work with a fresh Attempt and staging after confirmed stop and cleanup. Durable
generation registration occurs before dispatch; a registration-only crash consumes
an attempt. The finite budget includes the first generation and survives restarts
and technical-feedback input changes. Ordinary system/resource waits retain their
Attempt and original input; human waits retain graph checkpoint behavior. Neither
allocates another generation while waiting. Generation/phase journals record
counters and dispatch evidence separately from graph checkpoints.

Legacy ambiguous ownership or budget records, lost registered persistence, missing
authenticated call envelopes and unverifiable process identities refuse execution
with a diagnostic; read-only historical status remains available. Use a fresh
isolated diagnostic run rather than guessing takeover or resetting a budget.

Product tests live in `tests/product/`. The live OpenCode benchmark lives in
`benchmark/assurance-product/`; it is an optional operator/research tool, not a
merge or release gate.

### Retro evidence

The `retro` entrypoint reads only explicit SHA-256-bound `artifacts` references.
Include `qa/results/execution/execute-result.json` and its exact `plan_ref`, alongside
the review histories, inspection and report. Test verdicts come from execution;
inspection `analyzed` only means classification finished. Inspection must bind the
same execution digest, change and batch. Report-only evidence is incomplete.

Full diagnostic flows snapshot redacted Kernel journal evidence before Retro at
`qa/results/workflow/<invocation-id-digest>/pre-retro/<snapshot-sha256>/workflow-evidence.json` and bind
its exact digest to Retro. Non-Retro `aa run` and `aa resume` also export a post-run
projection to
`qa/results/workflow/<invocation-id-digest>/workflow-evidence.json`. Include its exact
file digest in the Retro input to retain technical failures, even after recovery.
The projection contains message fingerprints, not raw prompts or error messages.
Running the same invocation again updates it; refresh its artifact reference.
`workflow_evidence_export_failed` warns that export failed without overriding the
main result; an older projection cannot establish complete coverage of that run.

Missing runtime history, execution evidence or formal issue ledgers remain explicit
integrity gaps. There is currently no complete skill-adherence audit source, so
`skill_drift_not_assessed` is explicit: review rework is not proof of drift, and
missing audit evidence is not proof of compliance. Available facts still produce
signals; `completed_with_gaps` does not mean no problems. Rebuild and deploy updated
wheels before a live rerun; existing benchmark environments do not update themselves.

## Python wheels own topology

Python wheels own `StateGraph` topology and semantic Agent contracts. OpenCode
writes authorized raw workspace files and returns one locally validated JSON
result. The Kernel seals and commits the actual bytes. Product explicitly
composes six Feature bundles; `.aa/` contains closed organization data only;
changing nodes, edges, contracts, bindings, schemas, or runtime policy requires
code review, the repository gate, wheel rebuild, and authenticated deployment.

The engine does not load executable plugins, graphs, handlers, schemas,
validators, or runtime bindings from the system under test.

Each capability wheel exports a static `FEATURE` and public graph-bundle type
from its `feature.py`. `assurance_product.features` explicitly lists the six
exports; Product uses them for graph factories, contracts and output routes.
`plugin.py` still owns installed handlers/resources, while `graphs/factory.py`
still owns LangGraph topology. No decorator scan or new graph DSL is involved.

### Agent op authoring

Each of the 26 Agent contracts has a prepare handler and a finalize handler.

Intake declares every operation, like a FastAPI route, in its own directory under
[`ops/`](packages/capabilities/assurance-intake/assurance_intake/ops/), for example
[case design](packages/capabilities/assurance-intake/assurance_intake/ops/case_design/):

- `__init__.py` calls `router.agent(...)` (or `router.task(...)` for a deterministic
  op such as `resolve_plan`) with `input`, `prepare=Prepare(...)`, `agent=Agent(...)`,
  `finalize=Finalize(...)` and `output`, in run order;
- `Prepare(hook, depends, writes, errors)` and `Finalize(hook, on_output_error, writes)`
  are the Kernel phases around the run, and their `writes` are the files those hooks
  may write;
- `Agent(profile, skill, result, writes)` is the run itself. `result` defaults to
  `ArtifactListResultV1`, the receipt of the files the run wrote; its JSON Schema is
  the result contract OpenCode receives and the Kernel validates. Its `writes` lists exact
  files and `Dir(root, files=...)` entries: the contract claims each `Dir` root, the
  OpenCode boundary allows only the exact files `files(business)` returns for that run
  (each must sit under the root), and the output route is the exact files, or the
  `Dir` roots when the Agent declares no exact file;
- `hooks.py` holds the op's own `before(ctx, business)` and `after(ctx, business, result)`
  steps; `ctx.write` enforces the phase's declared writes;
- `models.py` and exactly one `SKILL.md` sit next to them.

A run that needs its own skill, input or output is its own op rather than a branch
inside another op, and a second `*.SKILL.md` in an op directory fails discovery:
[case repair](packages/capabilities/assurance-intake/assurance_intake/ops/case_repair/)
applies a `needs_fix` review's bounded actions, while case design always authors the
full delta, and the case graph routes automatic fixes to case repair and human rework
to case design.

The [`OpRouter`](packages/adapters/agent-runtime-contracts/agent_runtime_contracts/ops/router.py)
in `ops/__init__.py` runs the shared prepare/finalize pipeline and generates the
contracts, output routes, handler map and resource manifest from the declarations.
Every handler id dispatches through the single module-level `ops.execute`, and an
`ops/` subpackage without a declaration fails discovery. Graphs import only
`ops/<op>` declaration packages; ops never import graphs or one another; other wheels
read only intake `contracts/` and `domain/`.

Quality, healing, improvement, and generation declare the same shape. Generation's
eight family jobs are `ops/<family>_codegen` and `ops/<family>_codegen_review`;
the family handler classes in `operations/` remain for direct callers. Execution's
task contracts stay in `contracts/attempts.py`. `InputError` and `OutputError`
become `invalid_input` and `invalid_output`. Product injects installed,
authenticated phases; op code does not create a client or own retries.

Adding an agent op takes one `ops/<op>/` directory. The router publishes the
contract, routes, handler ids, and skill resources. A deterministic task whose
contract id or handler id does not match `router.task` stays hand-written in
`contracts/attempts.py`.

[`add_attempt_node`](packages/framework/graph-engine/graph_engine/stategraph/registration.py)
registers a normal Attempt node in a native LangGraph `StateGraph`, including
the 18 deterministic Task contracts.
[`add_attempt_edge` and `add_route`](packages/framework/graph-engine/graph_engine/stategraph/routing.py)
wire failure edges and conditional routes;
[`human_gate`](packages/framework/graph-engine/graph_engine/stategraph/human.py)
builds a human decision node on LangGraph `interrupt`. Pure state nodes and compiled subgraphs still
use native `add_node`. Routing, activation, selection and post-commit
publication stay in the graph. All 28 Feature graph exports and 15 Product
roots are covered.

Finalize validates business output before Kernel commit. Production uses durable
finite regeneration for unfinished work, including promotion before a completion
receipt; existing promotion evidence remains historical. The low-level Kernel
`execute_or_recover` API and default node-factory recovery compatibility remain
available to direct callers and are distinct from production resume policy.
Installed handlers retain the existing host containment model; external activity
whose terminal state cannot be confirmed keeps replacement execution blocked.

Execute and run are deterministic tasks. They do not call an LLM. Same-process
`aa_observe` collection detects omitted or mismatched observations; it is not a
cryptographic anti-forgery guarantee. `output_preexisting`, review, and fault
injection increase detection, not authenticity. Unconfirmed expectations stay
`expectation_unconfirmed` and cannot achieve. OpenChamber must upgrade to the
`minimum-coverage-result.json` 2.0 reader to show the obligation view; see
`docs/superpowers/specs/2026-09-19-obligation-evidence-closure-design.md`.
Live OpenCode codegen acceptance is a separate authorized run and is not claimed
by the deterministic test gate.

Execution reads the SUT from `project_root`; its lock, collector documents,
diagnostics and final evidence are staged in the Task's `write_root`. Only the
Kernel promotes them. The committed execution result carries the observation
reference and locked execution time into Quality. An obligation requiring human
confirmation or blocking delivery cannot be overridden by a coverage/test repair.

The lockout fault experiment proves the observation/verdict component, not a
product-level `achieved` outcome. The execution commit-boundary tests separately
exercise real pytest collection, the production execution subgraph and Kernel,
root checkpoint resume, and routing into Quality without replaying the tests.
Those tests use the host interpreter; isolated SUT dependency provisioning and
live OpenCode acceptance are separate checks.

Foreground execution can be stopped from another terminal using its actual workspace directory and Invocation ID:

```sh
uv run aa stop --project-dir /absolute/path/to/run-workspace --invocation-id inv-123 --force
```

The command verifies the local owner's process identity, confirms termination of its retained external activity, and releases only its owned resources before replacement is admitted. An acknowledged or unknown cancellation reports `unconfirmed` (exit 40); retry the same stop command after terminal proof becomes available. A pending local exit reports `stopping` (exit 20); confirmed stop exits 0. Omit `--force` to wait for local exit without sending a signal. The Python control API is `AssuranceProductApplication.stop(project_dir=..., invocation_id=..., force=True)`.
