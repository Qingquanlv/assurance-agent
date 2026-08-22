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

When done, state which selected tests ran and confirm the evidence covers the
mapping exactly.
