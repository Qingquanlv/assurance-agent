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

Before repair routing, the product authenticates the exact host terminal receipt and
kernel promotion receipt against an AttemptKey rederived from the current invocation,
entrypoint, graph revision, execution input, contract, and activation. The runtime
prepare and finalizer repeat durable receipt authentication and bind the checkpointed
execution identity to their current invocation. They recompile the machine plan from
the frozen ReviewedCase and assertion sources, and reject replay from another
invocation, execution attempt, or repair round, plus any changed verification
obligation. A proposal or summary alone never counts as an applied repair.
