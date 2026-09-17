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

Non-Retro `aa run` and `aa resume` export a redacted Kernel journal projection to
`qa/results/workflow/<invocation-id-digest>/workflow-evidence.json`. Include its exact
file digest in the Retro input to retain technical failures, even after recovery.
The projection contains message fingerprints, not raw prompts or error messages.
Running the same invocation again updates it; refresh its artifact reference.
`workflow_evidence_export_failed` warns that export failed without overriding the
main result; an older projection cannot establish complete coverage of that run.

Missing runtime history, execution evidence or formal issue ledgers remain explicit
integrity gaps. There is currently no complete skill-adherence audit source, so
`skill_drift_evidence_absent` is retained: review rework is not proof of drift, and
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
