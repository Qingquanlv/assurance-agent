# API codegen fix

Capability-owned API codegen-fix skill. Do not select a provider, model, or
adapter. Do not look up a global skill catalog.

Apply an approved API generated-test correction inside the allowed file set
named by the fix input. Schema truth is `assurance_generation.contracts` for
the resulting generated files, mapping, and fix candidate.

## Inputs

### required

- reviewed API plan (`PlanResultV1`)
- frozen case references
- baseline tree identity
- approved fix proposal
- allowed file set (the only paths this invocation may write)
- `qa/changes/<change-id>/codegen/api-codegen-summary.md`
- `qa/changes/<change-id>/codegen/api-generated-files.json`
- current generated tests under `qa/changes/<change-id>/generated/api/files/tests/api/**`

### optional

- `qa/changes/<change-id>/generated/api/files/tests/testdata/**` when the allowed
  file set names those logical `tests/testdata/**` targets

## Outputs

### required

- updated generated-files document and closed mapping for the allowed paths
- corrected staged files named in the allowed file set under
  `qa/changes/<change-id>/generated/api/files/`

The generated-files manifest and mapping keep `target_path="tests/..."`. Do not
write generated tests into the original `tests/**` tree.

## Boundaries

Write only the staged files named in the fix input. Reject any other generated
or modified test path. Physical writes stay under
`qa/changes/<change-id>/generated/api/files/`.

Do not modify product source.

When `verified_codegen.validation_profile` is present, preserve its complete
root-plan, ReviewedCase/epoch, machine-plan and spec-digest identity exactly.
Every mapped test must remain only the installed
`assurance_execution.bridge.execute_case("<mapped-case-id>")` entrypoint. Do
not add HTTP, SQLite, Trace, expected values, credentials, execution IDs,
runtime verdicts or Python assertions.

Do not invent a broader write set than the approved proposal.

The graph owns phase state. Do not write an orchestration state file.

Framework remains pytest. Keep Case ID → symbol → target file equality exact.
Capability keys must be exact typed leaves.

## Fix Candidate Rules

- Authenticate the approved proposal and the baseline tree identity.
- Every resulting `test_entry` path must remain in the closed mapping.
- Support and shared-builder files keep `case_ids: []`.
- Compute file digests from workspace bytes after the write; do not guess
  hashes.
- A no-op allowed path may stay `reused` only when the bytes are unchanged.
