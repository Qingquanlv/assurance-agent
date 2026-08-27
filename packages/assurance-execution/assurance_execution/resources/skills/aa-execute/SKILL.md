# Execute

Capability-owned execute skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Resume after intake using the locked closed mapping and reviewed cases.
Schema truth is `assurance_execution.contracts` for evidence and mapping,
`assurance_generation.contracts` for generated-file mappings, and
`assurance_intake.contracts` for reviewed cases.

## Inputs

### required

- change identity
- selected targets
- closed mapping (`ClosedMappingV1`)
- frozen capability leaves and case ids
- baseline tree identity
- reviewed generation mappings for the selected layers

### optional

- authenticated evidence files already present in the workspace

## Outputs

### required

- normalized execution evidence (`ExecutionEvidenceV1`)
- command receipt for the confined spawn
- one result row for every selected test and no other test

## Boundaries

Run only the selected mapping. Existing tests that are not in the mapping are
neither discovered nor executed.

Do not invent collection counts, durations, or failure text.

Do not choose a runner profile from ambient host configuration.

The graph owns phase state. Do not write an orchestration state file.

Write only change-scoped execution paths under `qa/changes/**/execution/` plus
the declared evidence files.

## Execution Environment and Family Runners

Do not create or update `.venv`, `venv`, `node_modules`, a package lock, or any
other dependency environment inside the attempt workspace. For Python tests,
invoke the project through `uv run --isolated ...`; the isolated environment
must live outside the candidate tree and must not be reported as an output.
Every Python runner command must also set `PYTHONDONTWRITEBYTECODE=1`. Every
pytest command must set
`HYPOTHESIS_STORAGE_DIRECTORY=/tmp/aa-hypothesis-<batch_id>` and pass
`-p no:cacheprovider`. E2E pytest commands must additionally pass
`--output=/tmp/aa-playwright-<batch_id>`. These locked, batch-scoped locations
keep bytecode, Hypothesis examples, pytest cache, and Playwright output outside
the authenticated candidate tree. Do not shorten, omit, or redirect those
settings back into the project.

Use the runner declared by the selected family:

- API and Fuzz: `PYTHONDONTWRITEBYTECODE=1
  HYPOTHESIS_STORAGE_DIRECTORY=/tmp/aa-hypothesis-<batch_id> uv run --isolated
  pytest -p no:cacheprovider` with exactly the mapped pytest node ids.
- E2E: the same isolated pytest prefix, plus
  `--output=/tmp/aa-playwright-<batch_id>`, with exactly the mapped
  pytest-playwright node ids.
- Performance: `PYTHONDONTWRITEBYTECODE=1 uv run --isolated locust --headless`
  with the mapped locustfile and bounded users, spawn rate, and duration from
  the reviewed plan. Never ask pytest to import or collect a Locust file.

Run different families as separate commands so one collector or environment
failure cannot suppress the other selected families. Normalize each command's
real exit status and output into the corresponding selected result rows.

When done, state which selected tests ran and confirm the evidence covers the
mapping exactly.
