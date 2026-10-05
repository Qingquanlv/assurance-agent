# Locator-bounded case repair

Capability-owned case-repair skill. The business input always carries the frozen
`review_repair` contract built from the committed `needs_fix` case review. Do not
select a provider, model, adapter, skill, or alternative repair scope.

## Prepared source observations

The JSON instruction includes `planning_facts`: bounded static observations with
file digests, exact symbol names/signature shapes, fixture names, and environment
variable names. Use the indexed paths for direct reads instead of rediscovering
them. `unknown` and `uninspected_paths` never prove absence; imports, plugins,
dynamic registrations and transitive environment dependencies may be unresolved.
Environment names expose no values and do not establish availability or necessity.
Check original source for behavior, auth semantics and oracle claims. Keep owner
requirements and frozen assertion intent distinct from observed implementation;
a source defect must not weaken the expected test behavior.

After bounded edits, read all graph-declared outputs and check that linked case,
proposal and MRC claims agree. This check grants no additional write authority;
report any required out-of-locator correction rather than silently widening scope.

## Authority

The graph-provided `review_repair.actions` array is the complete and exclusive
repair authority. Only edit the exact artifact, case_id, and allowed_paths named
by each action. Apply every action once. Do not infer extra work from the review
file, source code, proposal, matrix, test family, or prior conversation.

Treat `allowed_paths` as a strict field allowlist, not a semantic-consistency hint.
For example, when an action allows only `test_data`, `steps`, and `assertions`,
preserve `objective` and `summary` byte-for-byte even if changing them would make
the repaired wording feel more consistent.

The contract also contains authenticated baseline digests and case documents.
The deterministic finalizer compares the resulting files with that baseline.
Changes outside the declared locators fail the node.

## Procedure

1. Read every target artifact listed by `review_repair.actions`.
   A staged `case.yaml` must remain a complete case document with the baseline's
   sections, cases, full case fields, and ordering. Never replace it with only the
   target cases or allowed fields; that is a patch payload, not a valid case artifact.
2. For each action, locate the exact `case_id` and read its `allowed_paths` and
   `instructions`.
3. Use `apply_patch` for one target file at a time. Wait for the mutation to
   finish and read the file back before the next patch.
4. Change only the named field paths. A path such as `automation.fuzz` permits
   changes inside that nested object, not the rest of `automation`.
   For `proposal.md`, the one allowed path is a complete `## ` heading and only
   that section body may change. For the minimum-coverage matrix, allowed paths
   are exact `mrc_id` values and only their `status`, `covered_by_cases`, and
   `skip_reason` fields may change. `.qa.yaml` is never an automatic repair target.
5. Re-read every graph-declared output and verify the locked receipt.

Do not add, remove, reorder, or rewrite any case unless a repair action explicitly
authorizes that structural field. Do not modify any non-target output file. Do
not change a target case's `trace`, identity, type, module, or automation fields
unless that exact field path is present in `allowed_paths`. Never replace an
existing file wholesale. If a patch cannot be applied narrowly, return failure;
do not broaden the edit.

## Result

After all bounded edits, return structured JSON only. This run's JSON instruction
includes `outputs`, mapping `marker`, `proposal`, `matrix`, and `case` to the
paths authorized for this repair.

`output_files` must contain exactly the path in `outputs.marker`, every path in
`outputs.case`, the path in `outputs.proposal`, and the path in `outputs.matrix`,
sorted lexicographically. Do not duplicate artifact contents in the response.
