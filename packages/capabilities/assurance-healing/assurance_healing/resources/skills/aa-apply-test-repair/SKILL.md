# Apply an approved existing-test repair

Edit only the exact `allowed_test_paths` supplied in the authenticated input. These
paths are existing generated test sources bound to the reviewed Case and closed
mapping.

## Required behavior

- Apply only eligible items from the authenticated fix proposal and approval.
- When the input carries a verified generation defect, replace only its exact bridge
  path with the canonical `execute_case(<Case ID>)` bridge. Do not derive expected
  values or assertions from the failed source, runtime observations, DB, or Trace.
- Preserve every mapped test function or method and its Case identity.
- Preserve assertions, expected values, `pytest.raises` contracts, and skip/xfail
  behavior. If the repair requires changing an oracle, do not edit the file.
- Do not create tests, change Case YAML, change mappings, or edit product code.
- Write every changed file at its exact logical path beneath the provided write root.
- Return `TestRepairResultV1` with the exact sorted `output_files` write set and a
  concise summary.

The runtime finalizer independently authenticates the defect's exact host terminal
receipt and kernel promotion receipt, recompiles the machine plan from the frozen
ReviewedCase and assertion sources, and rejects replay from another execution attempt
or any changed verification obligation. A proposal or summary alone never counts as
an applied repair.
