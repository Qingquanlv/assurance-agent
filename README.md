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
`aa bindings build`, and `aa lock show` operate on an installed
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
  "case_delta_paths": ["qa/changes/CH-123/cases/account-recovery/case.yaml"],
  "capability_leafs": ["account.recovery.complete"],
  "capability_catalog": {"resource_id": "assurance.product.configuration.capability-catalog", "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "product_policy": {"resource_id": "assurance.product.configuration.product-policy", "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
  "data_knowledge": {"resource_id": "assurance.product.configuration.data-knowledge", "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"},
  "allowed_artifact_paths": ["qa/changes", "tests"],
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
  "resolved_plan_ref": {"path": "qa/changes/CH-123/plan/dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd/resolved-assurance-plan.json", "digest": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"},
  "case_delta_paths": ["qa/changes/CH-123/cases/account-recovery/case.yaml"],
  "capability_leafs": ["account.recovery.complete"],
  "capability_catalog": {"resource_id": "assurance.product.configuration.capability-catalog", "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "product_policy": {"resource_id": "assurance.product.configuration.product-policy", "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
  "data_knowledge": {"resource_id": "assurance.product.configuration.data-knowledge", "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"},
  "allowed_artifact_paths": ["qa/changes", "tests"],
  "budgets": {"review_rounds": 2, "coverage_rounds": 2, "healing_rounds": 1, "execution_retries": 1}
}
```

Product tests live in `tests/product/`. The live OpenCode benchmark lives in
`benchmark/assurance-product/`; it is an optional operator/research tool, not a
merge or release gate.

## Python wheels own topology

Python wheels own `StateGraph` topology and semantic Agent contracts. OpenCode
writes authorized raw workspace files and returns one locally validated JSON
result. The Kernel seals and commits the actual bytes. Product explicitly
composes six Feature bundles; `.aa/` contains closed organization data only;
changing nodes, edges, contracts, bindings, schemas, or runtime policy requires
code review, the repository gate, wheel rebuild, and authenticated deployment.

The engine does not load executable plugins, graphs, handlers, schemas,
validators, or runtime bindings from the system under test.
