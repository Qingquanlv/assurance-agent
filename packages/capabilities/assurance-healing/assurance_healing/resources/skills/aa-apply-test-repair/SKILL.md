# Apply an approved existing-test repair

Edit only the exact `allowed_test_paths` supplied in the authenticated input. These
paths are existing generated test sources bound to the reviewed Case and closed
mapping.

## Required behavior

- Apply only eligible items from the authenticated fix proposal and approval.
- Preserve every mapped test function or method and its Case identity.
- Preserve assertions, expected values, `pytest.raises` contracts, and skip/xfail
  behavior. If the repair requires changing an oracle, do not edit the file.
- Do not create tests, change Case YAML, change mappings, or edit product code.
- Write every changed file at its exact logical path beneath the provided write root.
- Return `TestRepairResultV1` with the exact sorted `output_files` write set and a
  concise summary.

The runtime finalizer compares staged bytes with the authenticated source refs. A
proposal or summary alone never counts as an applied repair.

For a verified bridge repair, the product derives `RepairAuthorizationV1` from the
current anchored workflow state and committed execution Attempt journal. Treat that
authorization as an exact transition input: it permits only the named bridge repair,
and the finalizer must re-run generation admission and preserve the machine plan's
business-obligation projection before publishing the result.
