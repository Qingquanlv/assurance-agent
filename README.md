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
bash scripts/graph_engine_smoke_test.sh
bash scripts/assurance_capability_wheel_smoke_test.sh
bash scripts/assurance_product_wheel_smoke_test.sh
```

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

The standalone `case` and `execute` entrypoints import that committed plan.
They use an empty candidate set and the exact content-addressed reference:

```json
{
  "schema_version": "1",
  "change_id": "CH-123",
  "requirement": "Protect the account recovery journey",
  "run_mode": "case",
  "candidate_test_families": [],
  "resolved_plan_ref": {"path": "qa/results/plan/dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd/resolved-assurance-plan.json", "digest": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"},
  "case_delta_paths": ["qa/cases/account-recovery/case.yaml"],
  "capability_leafs": ["account.recovery.complete"],
  "capability_catalog": {"resource_id": "assurance.product.configuration.capability-catalog", "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "product_policy": {"resource_id": "assurance.product.configuration.product-policy", "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
  "data_knowledge": {"resource_id": "assurance.product.configuration.data-knowledge", "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"},
  "allowed_artifact_paths": ["qa/.qa.yaml", "qa/cases", "qa/fixtures", "qa/proposal.md", "qa/requirement.md", "qa/results", "qa/tests"],
  "budgets": {"review_rounds": 2, "coverage_rounds": 2, "healing_rounds": 1, "execution_retries": 1}
}
```

Product tests live in `tests/product/`. The live OpenCode benchmark lives in
`benchmark/assurance-product/`; it is an optional operator/research tool, not a
merge or release gate.

### Retro evidence

The `retro` entrypoint reads only explicit SHA-256-bound `artifacts` references.
Include `qa/results/execution/execute-result.json` and its exact `plan_ref`, alongside
the review histories, inspection and report. Test verdicts come from execution;
inspection `analyzed` only means classification finished. Inspection must bind the
same execution digest, change and batch. Report-only evidence is incomplete.

Full/execute diagnostic flows snapshot redacted Kernel journal evidence before Retro at
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
exports; Product uses them for graph factories, contracts, output routes and
Agent Task binding. `plugin.py` still owns installed handlers/resources, while
`graphs/factory.py` still owns LangGraph topology. No decorator scan or new
graph DSL is involved.

### Agent Task authoring

All 26 Agent contracts have named Task classes with `before`/`run`/`after` and
optional `finally_` hooks. [CaseDesignTask](packages/capabilities/assurance-intake/assurance_intake/operations/case_design.py)
is one example, not a special execution path. Product injects installed,
authenticated phases; Task code does not create a client or own retries.

[`add_attempt_node`](packages/framework/graph-engine/graph_engine/stategraph/registration.py)
registers a normal Attempt node in a native LangGraph `StateGraph`, including
the 19 deterministic Task contracts. Pure state nodes and compiled subgraphs
still use native `add_node`. Routing, activation, selection and post-commit
publication stay in the graph. All 28 Feature graph exports and 15 Product
roots are covered.

`after` validates business output before Kernel commit. `finally_` observes
local execution-segment exit, not durable completion; terminal replay may
skip it. Existing production in-flight recovery gaps are not closed by this
authoring refactor.

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
