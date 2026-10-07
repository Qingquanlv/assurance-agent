# Apply a bounded existing-test repair

Edit only the exact `allowed_test_paths` supplied in the authenticated input. These
paths are existing generated test sources bound to the reviewed Case and closed
mapping.

## Required behavior

- Apply only eligible items from the authenticated fix proposal.
- Preserve every mapped test function or method and its Case identity.
- Preserve assertions, expected values, `pytest.raises` contracts, and skip/xfail
  behavior. If the repair requires changing an oracle, do not edit the file.
- Do not create tests, change Case YAML, change mappings, or edit product code.
- Write every changed file at its exact logical path beneath the provided write root.
- Return `TestRepairResultV1` with the exact sorted `output_files` write set and a
  concise summary. Do not list `repair.json` in `output_files`.

The runtime finalizer compares staged test bytes with the authenticated source
refs. A proposal or summary alone never counts as an applied repair.

### Host-owned repair history

Your outputs are only the eligible changed test files. After verifying the
repair, the host finalize handler generates `repair.json` under its declared
write claims, binding the actual changed bytes. History is not an Agent output.
