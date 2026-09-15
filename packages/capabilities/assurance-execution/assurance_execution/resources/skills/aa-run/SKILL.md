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

Run the locked selectors in place under durable `qa/tests/`.
Reject traversal, symbolic links, non-regular files, remapped staging copies,
and project-tree paths outside that tree. Spawn only after the selected set
equals the closed mapping.

Unmapped existing tests are neither discovered nor run.

Never fabricate a pass, fail, skip, duration, or coverage number.

Do not create or update `.venv`, `venv`, `node_modules`, a package lock, or any
other dependency environment inside the attempt workspace. Use only these
canonical command forms and keep the shown option order. Each Bash tool input
is one physical line: replace the visual line wrapping below with spaces and
never include a newline, `&&`, `;`, pipe, redirect, shell wrapper, or setup
command. `<mapped-selector>` is the locked pytest node id under `qa/tests/`:

- API and Fuzz: `PYTHONDONTWRITEBYTECODE=1
  HYPOTHESIS_STORAGE_DIRECTORY=/tmp/aa-hypothesis-<batch_id> uv run --isolated
  pytest -p no:cacheprovider --tb=line -o pythonpath=qa
  <mapped-selector> ...`.
- E2E: `PYTHONDONTWRITEBYTECODE=1
  HYPOTHESIS_STORAGE_DIRECTORY=/tmp/aa-hypothesis-<batch_id> uv run --isolated
  pytest -p no:cacheprovider --tb=line -o pythonpath=qa
  --output=/tmp/aa-playwright-<batch_id>
  <mapped-selector> ...`.
- Performance: `PYTHONDONTWRITEBYTECODE=1 uv run --isolated locust --locustfile
  <mapped-locustfile> --headless ...`, with bounded load
parameters from the reviewed plan.

Every pytest command must include `--tb=line`; line-only tracebacks prevent
fixture values such as temporary authentication tokens from entering
Agent-visible runner output.

Never collect a Locust file with pytest. The first shell action must be the
canonical command for the first selected family. Do not probe the execution
view with Bash, grep, glob, find, ls, pwd, or an alternate test command; the
closed mapping already supplies every permitted path. Invoke Bash exactly once
per selected family in canonical family order. A rejected tool call is not a
family execution: retry that family with its exact canonical single-line
command and continue with the remaining families. Put only commands that
actually ran in the receipt. Never fabricate a receipt for an unissued command,
merge commands into a shell wrapper, or attribute one family's counts to
another. The receipt `command` field is a tokenized argv array: each environment
assignment, executable, option, option value, and selected path is a separate
array element, and `pytest` or `locust` appears as its own element. Never return
the whole Bash tool input as one array element. For every family, the command receipt's passed, failed, and skipped
counts must each be at least the corresponding counts in that family's result
rows, and passed + failed + skipped must equal collected. For Performance,
Locust has no pytest-style collection summary: count the normalized mapped
result rows, not Locust request events. For a non-zero command exit with no
native test report, emit one failed result row for every selected mapping in
that family, set `collected` and `failed` to that row count, set `passed` and
`skipped` to zero, and preserve the real runner diagnostic in every failed row.
This is the defined normalization of a runner-level failure, not a fabricated
test count.
Runtime caches and reports must stay outside the candidate tree.

The graph owns phase state. Do not write an orchestration state file.

Do not write files. Return exactly one terminal `ExecutionAgentResultV1` JSON
object; the trusted finalize phase derives and writes durable
`ExecutionEvidenceV1`.

When the selected mapping is empty or a selected family has no mapped test, fail
closed without spawning. When a result names a test outside the mapping, fail
closed.
