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

Each of the 27 Agent contracts has a prepare handler and a finalize handler.

Intake declares every operation, like a FastAPI route, in its own directory under
[`ops/`](packages/capabilities/assurance-intake/assurance_intake/ops/), for example
[case design](packages/capabilities/assurance-intake/assurance_intake/ops/case_design/):

- `__init__.py` calls `router.agent(...)` (or `router.task(...)` for a deterministic
  op such as `resolve_plan`) with `input`, `prepare=Prepare(...)`, `agent=Agent(...)`,
  `finalize=Finalize(...)` and `output`, in run order;
- `Prepare(hook, depends, writes, errors)` and `Finalize(hook, on_output_error, writes)`
  are the Kernel phases around the run, and their `writes` are the files those hooks
  may write;
- `Agent(profile, skill, result, writes)` is the run itself. Its `writes` lists exact
  files and `Dir(root, files=...)` entries: the contract claims each `Dir` root, the
  OpenCode boundary allows only the exact files `files(business)` returns for that run
  (each must sit under the root), and the output route is the exact files;
- `hooks.py` holds the op's own `before(ctx, business)` and `after(ctx, business, result)`
  steps; `ctx.write` enforces the phase's declared writes;
- `models.py`, exactly one `SKILL.md`, and `result.schema.json` sit next to them.

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

Finalize validates business output before Kernel commit. Existing production
in-flight recovery gaps are not closed by this authoring refactor.

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
