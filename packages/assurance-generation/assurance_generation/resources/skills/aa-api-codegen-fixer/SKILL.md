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
- current generated tests under `tests/api/**`

### optional

- `tests/testdata/**` when the allowed file set names those paths

## Outputs

### required

- updated generated-files document and closed mapping for the allowed paths
- corrected files named in the allowed file set

## Boundaries

Write only the files named in the fix input. Reject any other generated or
modified test path.

Do not modify product source.

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
