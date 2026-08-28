# Run

Capability-owned run skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Execute the locked closed mapping and return authenticated evidence.
Schema truth is `assurance_execution.contracts`.

## Inputs

### required

- change identity and batch identity
- selected targets
- closed mapping (`ClosedMappingV1`)
- frozen capability leaves and case ids
- baseline tree identity
- runner profile digest

## Outputs

### required

- `ExecutionEvidenceV1` bound to the mapping digest, baseline tree, runner
  profile digest, and receipt digest
- collected, passed, failed, and skipped counts taken from the confined receipt
- PR metric input derived from the same selected set and results

## Boundaries

The public handler accepts only canonical data. Do not read ambient runner
configuration and do not expand a shell.

Resolve every selected path against the attempt workspace. Reject traversal,
symbolic links, and non-regular files. Spawn only after the selected set equals
the closed mapping.

Unmapped existing tests are neither discovered nor run.

Never fabricate a pass, fail, skip, duration, or coverage number.

Do not create or update `.venv`, `venv`, `node_modules`, a package lock, or any
other dependency environment inside the attempt workspace. Run Python tooling
through an environment outside the candidate tree: API, E2E, and Fuzz use
`uv run --isolated pytest` with exactly the mapped pytest node ids; Performance
uses `uv run --isolated locust --headless` with the mapped locustfile and the
bounded load parameters from its reviewed plan. Prefix every command with
`PYTHONDONTWRITEBYTECODE=1`. For pytest, also set
`HYPOTHESIS_STORAGE_DIRECTORY=/tmp/aa-hypothesis-<batch_id>` and pass
`-p no:cacheprovider`; for E2E additionally pass
`--output=/tmp/aa-playwright-<batch_id>`. Never collect a Locust file with
pytest. Run each selected family separately and preserve every real exit status
and diagnostic. Runtime caches and reports must stay outside the candidate
tree.

The graph owns phase state. Do not write an orchestration state file.

When the selected mapping is empty, return empty evidence that still covers the
mapping. When a result names a test outside the mapping, fail closed.
