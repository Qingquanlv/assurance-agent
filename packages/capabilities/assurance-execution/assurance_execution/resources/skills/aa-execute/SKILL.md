# Execute

Capability-owned execute skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Resume after intake using the locked closed mapping and reviewed cases.
Schema truth is `assurance_execution.contracts` for runner facts and mapping,
`assurance_generation.contracts` for generated-file mappings, and
`assurance_intake.contracts` for reviewed cases.

## Inputs

### required

- change identity
- selected targets
- closed mapping (`ClosedMappingV1`)
- frozen capability leaves and case ids
- baseline tree identity
- the exact read-only `execution_view_root` for this Attempt
- reviewed generation mappings for the selected layers

### optional

- authenticated evidence files already present in the workspace

## Outputs

### required

- raw execution facts (`ExecutionAgentResultV1`); do not invent the Kernel-owned
  status, execution timestamp, mapping digest, or receipt digest
- one activity receipt containing exactly one command receipt for every selected
  family, ordered API, E2E, Fuzz, Performance; each command receipt names its
  family and carries that spawn's argv, exit status, collected, passed, failed,
  and skipped counts
- one result row for every selected test and no other test

## Boundaries

Run only the selected mapping. Existing tests that are not in the mapping are
neither discovered nor executed.

Run the locked selectors in place under durable `qa/tests/`. Never execute a
project-tree path outside that tree, a remapped staging copy, or an unmapped
existing test.

Do not invent collection counts, durations, or failure text.

Do not choose a runner profile from ambient host configuration.

The graph owns phase state. Do not write an orchestration state file.

Do not write files. Return exactly one terminal `ExecutionAgentResultV1` JSON
object; the trusted finalize phase derives and writes durable
`ExecutionEvidenceV1`.

## Execution Environment and Family Runners

Do not create or update `.venv`, `venv`, `node_modules`, a package lock, or any
other dependency environment inside the attempt workspace. For Python tests,
invoke the project through `uv run --isolated ...`; the isolated environment
must live outside the candidate tree and must not be reported as an output.
Every Python runner command must also set `PYTHONDONTWRITEBYTECODE=1`. Every
pytest command must set
`HYPOTHESIS_STORAGE_DIRECTORY=/tmp/aa-hypothesis-<batch_id>` and pass
`-p no:cacheprovider --tb=line`. The line-only traceback is mandatory so fixture
values such as temporary authentication tokens never enter Agent-visible runner
output. E2E pytest commands must additionally pass
`--output=/tmp/aa-playwright-<batch_id>`. These locked, batch-scoped locations
keep bytecode, Hypothesis examples, pytest cache, and Playwright output outside
the authenticated candidate tree. Do not shorten, omit, or redirect those
settings back into the project.

Use only these canonical command forms. `<mapped-selector>` is the locked
pytest node id under `qa/tests/`; `<mapped-locustfile>` is the locked
performance file under `qa/tests/`. Each Bash tool input is one physical line:
replace the visual line wrapping below with spaces and never include a newline,
`&&`, `;`, pipe, redirect, shell wrapper, or setup command. Keep the shown
option order:

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
  <mapped-locustfile> --headless ...` with bounded users,
  spawn rate, and duration from the reviewed plan. Never ask pytest to import
  or collect a Locust file.

The first shell action must be the canonical command for the first selected
family. Do not probe the execution view with Bash, grep, glob, find, ls, pwd, or
an alternate test command; the closed mapping already supplies every permitted
path. Invoke Bash exactly once per selected family, in API, E2E, Fuzz,
Performance order, so one collector or environment failure cannot suppress the
other selected families. A rejected tool call is not a family execution: retry
that family with its exact canonical single-line command and continue with the
remaining families. Put only commands that actually ran in the activity
receipt's `commands` array. Never fabricate a receipt for an unissued command,
merge commands into a shell wrapper, or attribute one family's counts to
another.
The receipt `command` field is a tokenized argv array: each environment
assignment, executable, option, option value, and selected path is a separate
array element, and `pytest` or `locust` appears as its own element. Never return
the whole Bash tool input as one array element.
Normalize each command's real exit status and output into that family's selected
result rows. For every family, the command receipt's passed, failed, and skipped
counts must each be at least the corresponding counts in that family's result
rows, and passed + failed + skipped must equal collected. For Performance,
Locust has no pytest-style collection summary: count the normalized mapped
result rows, not Locust request events. For a non-zero command exit with no
native test report, emit one failed result row for every selected mapping in
that family, set `collected` and `failed` to that row count, set `passed` and
`skipped` to zero, and preserve the real runner diagnostic in every failed row.
This is the defined normalization of a runner-level failure, not a fabricated
test count.

When done, state which selected tests ran and confirm the evidence covers the
mapping exactly.
