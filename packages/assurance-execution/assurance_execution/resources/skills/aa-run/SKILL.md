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

The graph owns phase state. Do not write an orchestration state file.

When the selected mapping is empty, return empty evidence that still covers the
mapping. When a result names a test outside the mapping, fail closed.
