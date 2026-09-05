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
- the exact read-only `execution_view_root` for this Attempt

## Outputs

### required

- `ExecutionAgentResultV1` bound to the mapping, baseline tree, runner profile,
  one activity receipt, and result rows; do not invent Kernel-owned digests,
  terminal status, or execution timestamp
- the activity receipt's `commands` array contains exactly one command receipt
  for every selected family, ordered API, E2E, Fuzz, Performance; each entry
  names its family and preserves that spawn's argv, exit status, and counts
- PR metric input derived from the same selected set and results

## Boundaries

The public handler accepts only canonical data. Do not read ambient runner
configuration and do not expand a shell.

Resolve every selected path underneath the supplied `execution_view_root`.
Reject traversal, symbolic links, non-regular files, another Attempt's view,
and project-tree paths. Pass that exact root to the family runner and spawn
only after the selected set equals the closed mapping.

Unmapped existing tests are neither discovered nor run.

Never fabricate a pass, fail, skip, duration, or coverage number.

Do not create or update `.venv`, `venv`, `node_modules`, a package lock, or any
other dependency environment inside the attempt workspace. Use only these
canonical command forms and keep the shown option order. `<mapped-selector>` is
the locked pytest node id prefixed by `<execution_view_root>/`:

- API and Fuzz: `PYTHONDONTWRITEBYTECODE=1
  HYPOTHESIS_STORAGE_DIRECTORY=/tmp/aa-hypothesis-<batch_id> uv run --isolated
  pytest -p no:cacheprovider --rootdir <execution_view_root>
  <execution_view_root>/<mapped-selector> ...`.
- E2E: `PYTHONDONTWRITEBYTECODE=1
  HYPOTHESIS_STORAGE_DIRECTORY=/tmp/aa-hypothesis-<batch_id> uv run --isolated
  pytest -p no:cacheprovider --rootdir <execution_view_root>
  --output=/tmp/aa-playwright-<batch_id>
  <execution_view_root>/<mapped-selector> ...`.
- Performance: `PYTHONDONTWRITEBYTECODE=1 uv run --isolated locust --locustfile
  <execution_view_root>/<mapped-locustfile> --headless ...`, with bounded load
  parameters from the reviewed plan.

Never collect a Locust file with pytest. Run each selected family separately
and preserve every real exit status and diagnostic in canonical family order.
Never merge commands into a shell wrapper or attribute one family's counts to
another. For every family, the command receipt's passed, failed, and skipped
counts must each be at least the corresponding counts in that family's result
rows. A non-zero command exit with no failed test row still requires failed to
be at least 1; preserve the runner diagnostic on that family's failed result.
Runtime caches and reports must stay outside the candidate tree.

The graph owns phase state. Do not write an orchestration state file.

Do not write files. Return exactly one terminal `ExecutionAgentResultV1` JSON
object; the trusted finalize phase derives and writes durable
`ExecutionEvidenceV1`.

When the selected mapping is empty or a selected family has no mapped test, fail
closed without spawning. When a result names a test outside the mapping, fail
closed.
