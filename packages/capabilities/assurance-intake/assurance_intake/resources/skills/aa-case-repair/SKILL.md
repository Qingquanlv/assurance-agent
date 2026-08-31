# Locator-bounded case repair

Capability-owned repair skill. Use it only when the graph supplies a non-null
`review_repair` contract. Do not select a provider, model, adapter, skill, or
alternative repair scope.

## Authority

The graph-provided `review_repair.actions` array is the complete and exclusive
repair authority. Only edit the exact artifact, case_id, and allowed_paths named
by each action. Apply every action once. Do not infer extra work from the review
file, source code, proposal, matrix, test family, or prior conversation.

The contract also contains authenticated baseline digests and case documents.
The deterministic finalizer compares the resulting files with that baseline.
Changes outside the declared locators fail the node.

## Procedure

1. Read every target artifact listed by `review_repair.actions`.
2. For each action, locate the exact `case_id` and read its `allowed_paths` and
   `instructions`.
3. Use `apply_patch` for one target file at a time. Wait for the mutation to
   finish and read the file back before the next patch.
4. Change only the named field paths. A path such as `automation.fuzz` permits
   changes inside that nested object, not the rest of `automation`.
5. Re-read every graph-declared output and verify the locked receipt.

Do not add, remove, reorder, or rewrite any case unless a repair action explicitly
authorizes that structural field. Do not modify any non-target output file. Do
not change a target case's `trace`, identity, type, module, or automation fields
unless that exact field path is present in `allowed_paths`. Never replace an
existing file wholesale. If a patch cannot be applied narrowly, return failure;
do not broaden the edit.

If `validation_attempt` is `1`, also satisfy `validation_error`, but only inside
the same action locators. If the validation error cannot be repaired inside those
locators, return failure rather than changing unrelated data.

## Result

After all bounded edits, return structured JSON only:

```json
{"output_files":["qa/changes/<change-id>/.qa.yaml","qa/changes/<change-id>/cases/<module>/case.yaml","qa/changes/<change-id>/proposal.md","qa/changes/<change-id>/trace/minimum-coverage-matrix.json"]}
```

`output_files` must contain exactly every graph-declared case-design output,
sorted lexicographically. Do not duplicate artifact contents in the response.
