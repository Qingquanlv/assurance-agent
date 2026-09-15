# E2E plan review

Capability-owned E2E plan review skill. Do not select a provider, model, or
adapter.

Review the E2E plan package and emit a `PlanReviewAuthoring` document from
`assurance_generation.contracts`. Mechanical plan-check facts arrive as inputs;
do not infer or apply a policy action from them.

Evidence-proven, bounded defects use `needs_fix`: set `auto_fix_allowed: true`,
`human_review_required: false`, `codegen_readiness: not_ready`, and list every
bounded finding ID in `auto_fix_plan`. Severity alone does not require human
review. Use `needs_human_review` only when correction requires a missing
product, policy, authorization, or safety decision; then prohibit automatic
repair and leave `auto_fix_plan` empty. A codegen-ready `pass` never requires
human review.

`auto_fix_plan` has one exact JSON shape: an array of non-empty finding ID
strings, for example `"auto_fix_plan": ["E2E-PLAN-001", "E2E-PLAN-002"]`.
Never put objects, `finding_id`/`action` pairs, prose, or locator data in this
array. Put repair detail in the matching finding's `message` and `locator`, and
in `next_action`. Use `"auto_fix_plan": []` for `pass` and
`needs_human_review`.

For `needs_fix`, `auto_fix_plan` must contain exactly the set of finding IDs
present in this response: no missing IDs and no extra IDs. Remove IDs for
findings that were resolved in an earlier round. Immediately before returning,
compare the two sets and correct the response if they differ.

Before the first decision in every round, close this runtime inventory for the
whole package, not only for the section most recently edited:

- Exact-read `.aa/data-knowledge.yaml`, resolve each consumed dotted Python
  symbol to its `.py` module, and read that module before classifying the symbol
  as present, absent, reusable, or create-if-missing.
- For every mapped pytest target and fixture parameter, read every ancestor
  `conftest.py` from the target directory through the test root. Verify the
  fixture's real name and its browser, credential, or request handoff; do not
  infer a wrapper fixture from the capability name.
- Read implementations of every mapped helper and adapter and account for
  their base URLs, credentials, event-loop constraints, and cleanup state.
- For every concrete runtime-support path or dotted Python symbol named by a
  plan, call the native read tool on that exact path before any glob or grep.
  Gitignored files remain exact-readable. If that exact read was not attempted,
  do not emit an absence finding from glob or grep output.
- Reconcile those facts across every plan artifact and summary before emitting
  findings. A repaired artifact does not narrow the next review: repeat this
  complete checklist on every retry and report newly observable defects in the
  same round as any remaining repair defects.

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

For each defect, inspect its references across the complete package and emit a
separate locator for every affected artifact/section in this review. Include every
bounded finding in `auto_fix_plan`. Independently verify source-backed claims;
the facts digest binds observations, not semantic truth. Re-review repaired output
before passing; the desired repair count never changes the acceptance criteria.

## Durable mapping and the execution view

Closed mapping `target_file` values must stay under `qa/tests/`.
Execute runs durable `qa/tests/` in place with `pythonpath=qa`.
Fixtures and support modules live under `qa/tests/`.
Treat a missing `qa/tests/**/conftest.py` as fixture unavailability.
Do not look up fixtures under the SUT `tests/` tree.
Do not retarget mapping rows to `tests/`.

## Inputs

Read `proposal.md` first from the locked inputs. When its `Product Source Verification`
section lists exact product-source paths, read every listed path directly before any discovery.
A glob result of `No files found` is not evidence that product source is absent;
ignored source files remain exact-readable. Only declare source unavailable after
those exact reads and a path-scoped grep both fail.

The final JSON instruction part contains the mechanically locked
`review_input_paths`. Use the native read tool to read every listed path
directly before reviewing. Do not use glob, wildcard search, or ignore-aware
file discovery under `qa/` to decide whether an input exists. The host
has already verified these exact paths as regular files.

### required

- `qa/results/plans/e2e-plan.md`
- `qa/results/plans/e2e-test-data-plan.md`
- `qa/results/plans/e2e-codegen-plan.md`
- `qa/results/plans/e2e-codegen-mapping.json`
- `qa/results/plans/m4-review-summary.md`
- `qa/cases/**/case.yaml`

### optional

- `qa/results/plans/data-knowledge.proposal.e2e.yaml`
- `.aa/data-knowledge.yaml`
- backend and frontend product source (read-only)
- `qa/tests/e2e/**` and `qa/tests/testdata/domain/**`

## Outputs

### required

- `qa/results/review/e2e-plan-review.json`
- `qa/results/review/e2e-plan-review-summary.md`

## Boundaries

Write only the review outputs listed above. Authorizing bounded planner re-entry
does not permit the reviewer to edit plan files. Each automatic finding must
point to an exact plan artifact and bounded key/section that the planner can
revise from observed source. Keep semantic factory mapping review. A missing
`e2e-plan-checks.json` is not a stop condition; when present, read it as
deterministic evidence but never write it. The graph owns phase state. Do not
write an orchestration state file.

## Proven Product Defects

A source-proven SUT defect is test evidence, not a missing product decision.
When the approved requirement and expected assertion are clear, keep the test
intent and let execution and reporting record the failure. Do not require the
SUT defect to be corrected before codegen. Use `pass` with
`ready_with_warnings` when the mapped test remains executable; reserve
`needs_human_review` for a genuinely missing intent, product, policy,
authorization, credential, or safety decision.

## Domain Notes

Use decision `pass` when the plan is codegen-ready. Emit `codegen_readiness`,
`auto_fix_allowed`, `human_review_required`, and `risk_level`. Each finding
must include `id`, `severity`, `category`, `message`, and `locator`. Point
mapping defects at `plans/e2e-codegen-mapping.json`.

Do not stop the review after finding the first defect. Before returning,
complete one exhaustive pass across every required plan artifact and trace each
setup, assertion, and cleanup lifecycle statement through all files that repeat it. Return all
independently observable defects in the same review document. A finding locator
authorizes exactly one artifact and key/section, so emit one finding per target
when the same correction is required in multiple artifacts. This lets one
bounded planner re-entry repair the complete package instead of spending later
rounds rediscovering the same contradiction in another file.

Required capability closure:

- Copy every `required_capabilities` key verbatim from a typed leaf that exists
  in `.aa/data-knowledge.yaml`; a valid-looking prefix is not evidence.
- Never invent a constraint or adapter suffix. If a needed leaf is absent, use
  `needs_human_review` with `not_ready`.
- Emit `pass` with `ready` / `ready_with_warnings` only when every required key
  resolves exactly.

Runtime contract closure:

- Independently inspect the declared backend/frontend sources and existing
  E2E/test-data trees.
- Compare setup and cleanup payloads, identifier extraction, and browser
  locators with the real handlers and DOM. A create response without an
  identifier requires an exact supported lookup; assumed `data.id` is
  `not_ready`.
- Verify each setup and cleanup HTTP method/path against live OpenAPI when
  available, otherwise against the declared router/schema source.
- When an existing typed leaf and inspected DOM/source prove a corrected
  capability mapping, payload, lifecycle, or locator, use bounded `needs_fix`;
  do not require a human merely because the defect is high severity.
