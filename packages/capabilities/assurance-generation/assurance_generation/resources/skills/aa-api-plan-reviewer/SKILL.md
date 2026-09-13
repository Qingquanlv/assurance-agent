# API plan review

Capability-owned API plan review skill. Do not select a provider, model, or
adapter.

Review the API plan package and emit a `PlanReviewAuthoring` document from
`assurance_generation.contracts`. When mechanical plan-check facts are present,
treat them as evidence only; the downstream gate owns routing. Their absence is
not a reason to stop because this skill independently reviews the plan package.

Evidence-proven, bounded defects use `needs_fix`: set `auto_fix_allowed: true`,
`human_review_required: false`, `codegen_readiness: not_ready`, and list every
bounded finding ID in `auto_fix_plan`. Severity alone does not require human
review. Use `needs_human_review` only when correction requires a missing
product, policy, authorization, or safety decision; then prohibit automatic
repair and leave `auto_fix_plan` empty. A codegen-ready `pass` never requires
human review.

`auto_fix_plan` has one exact JSON shape: an array of non-empty finding ID
strings, for example `"auto_fix_plan": ["API-PLAN-001", "API-PLAN-002"]`.
Never put objects, `finding_id`/`action` pairs, prose, or locator data in this
array. Put repair detail in the matching finding's `message` and `locator`, and
in `next_action`. Use `"auto_fix_plan": []` for `pass` and
`needs_human_review`.

For `needs_fix`, `auto_fix_plan` must contain exactly the set of finding IDs
present in this response: no missing IDs and no extra IDs. Remove IDs for
findings that were resolved in an earlier round. Immediately before returning,
compare the two sets and correct the response if they differ.

Do not stop the review after finding the first defect. Before choosing a
decision, complete one exhaustive pass across every required plan artifact,
every mapping row, and every source-backed runtime boundary used by the plan.
Return all independently observable defects in the same review document so a
single bounded planner re-entry can repair the whole package.
During the existing full-package review, verify every mapped helper's sync/async
invocation against the test-runner configuration and check that the combined
assertion logic, including all alternative success paths, preserves the frozen
case oracle, reporting all source-supported defects in the current round.

Before the first decision in every round, close this runtime inventory for the
whole package, not only for the section most recently edited:

- Exact-read `.aa/data-knowledge.yaml`, resolve each consumed dotted Python
  symbol to its `.py` module, and read that module before classifying the symbol
  as present, absent, reusable, or create-if-missing.
- For every mapped pytest target and fixture parameter, read every ancestor
  `conftest.py` from the target directory through the test root. Verify the
  fixture's real name and the value-to-header or value-to-request handoff; do
  not infer a wrapper fixture from the capability name.
- Read implementations of every mapped helper and account for their required
  environment variables, credentials, and database or session paths in the run
  guidance. A helper that opens persistence directly is not executable from a
  base URL alone.
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

Closed mapping `target_file` values must stay under `qa/tests/`. The
execution view remaps `qa/tests/<rest>` to `tests/<rest>` for pytest
collection. Fixtures and support modules live under `qa/tests/`.
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

- `qa/results/plans/api-plan.md`
- `qa/results/plans/api-test-data-plan.md`
- `qa/results/plans/api-codegen-plan.md`
- `qa/results/plans/api-codegen-mapping.json`
- `qa/results/plans/m3-review-summary.md`
- `qa/cases/**/case.yaml`

### optional

- `qa/results/plans/data-knowledge.proposal.api.yaml`
- `.aa/data-knowledge.yaml`
- product source under the project source root (read-only contract evidence)
- `qa/tests/api/**` and `qa/tests/testdata/domain/**`

## Outputs

### required

- `qa/results/review/api-plan-review.json`
- `qa/results/review/api-plan-review-summary.md`

## Boundaries

Write only the review outputs listed above. Authorizing bounded planner re-entry
does not permit the reviewer to edit plan files. Each automatic finding must
point to an exact plan artifact and bounded key/section that the planner can
revise from observed source.

A finding locator authorizes exactly one artifact and key/section. When one
conceptual defect requires edits in multiple artifacts or sections, emit one
finding per target, give each finding its own locator, and include every finding
ID in `auto_fix_plan`. Never request an edit to an artifact that is not named by
the matching finding's locator.

Do not write plan Markdown, tests, or knowledge files.

The graph owns phase state. Do not write an orchestration state file.

## Proven Product Defects

A source-proven SUT defect is test evidence, not a missing product decision.
When the approved requirement and expected assertion are clear, keep the test
intent and let execution and reporting record the failure. Do not require the
SUT defect to be corrected before codegen. Use `pass` with
`ready_with_warnings` when the mapped test remains executable; reserve
`needs_human_review` for a genuinely missing intent, product, policy,
authorization, credential, or safety decision.

## Initial administrator credential review

When the API plan requires administrator authentication, independently compare `admin_username`
and `admin_password` in the plan with the exact startup initialization or seed source cited by the
approved plan. Both values must deterministically match the values used to create the initial
administrator. Do not read `.env` or `*.env` files as credential evidence.

Treat a missing value, a contradictory value, or any credential fallback such as a test-runtime
default as a non-pass finding with `not_ready`. Use bounded `needs_fix` when the product source
proves the correction; use `needs_human_review` only when the source cannot resolve the required
credential without a runtime or policy decision.

## Domain Notes

Emit `codegen_readiness`, `auto_fix_allowed`, `human_review_required`, and
`risk_level`. Use decision `pass` when the plan is codegen-ready. Each finding must include
`id`, `severity`, `category`, `message`, and `locator` (`artifact` plus optional
`case_id` / `key`). Point locators at `plans/api-codegen-mapping.json` when the
defect is a mapping row.

Required capability closure:

- Copy every `required_capabilities` key verbatim from a typed leaf that exists
  in `.aa/data-knowledge.yaml`; a valid-looking prefix is not evidence.
- Never invent a constraint or adapter suffix. If a needed leaf is absent, use
  `needs_human_review` with `not_ready` instead of `pass` with a virtual key.
- Emit `pass` with `ready` / `ready_with_warnings` only when every required key
  resolves exactly.
- Treat capabilities as consumed reusable fixtures/helpers, not as aliases for
  the SUT operations under test. A source-proven direct HTTP call using declared
  auth and target-local helpers does not require a same-operation API adapter
  leaf. Do not request a new adapter solely because the case exercises create,
  list, get, update, or delete endpoints.

Runtime contract closure:

- Independently compare every method/path named by the API plan and codegen
  plan with the live OpenAPI document when available, otherwise with the actual
  router source. Naming convention is not evidence.
- Compare seed and mutation payloads with the registered request schema and
  compare identifier extraction with the real response shape.
- Trace each planned lifecycle end to end: create, identifier resolution,
  follow-up read, mutation, assertions, and cleanup. Validate every lookup used
  for both root and nested entities. For a tree endpoint, inspect whether server
  filtering happens before tree reconstruction; a filtered child that no longer
  has a returned root must use an unfiltered tree plus bounded recursive exact
  matching instead.
- A missing operation, wrong HTTP method, unverified payload field, or assumed
  response identifier is a non-pass finding with `not_ready`.
- Inspect the declared API and shared test-data trees before claiming a mapped
  test, fixture, or helper is absent.
- For every mapped helper that is absent, validate in the same review both its
  generated target and its actual source-backed implementation boundary,
  including required fixture or credential handoff. Do not assume an HTTP
  transport can expose persistence-only rows when the inspected router has no
  such operation; map a source-proven database/session read boundary instead.
- When existing source or a declared typed capability proves a corrected method,
  payload, identifier lookup, or mapping, use bounded `needs_fix`; do not require
  a human merely because the defect is high severity.
- Preserve a frozen Explore/case oracle whose `assertion_intent` is
  `assert_ideal`. When current source implements the opposite behavior, that
  mismatch is the product defect the test must expose, not a missing product or
  authorization decision. Do not reopen the decision or route it to human
  review. A stale knowledge note describing current behavior does not override
  the frozen assertion intent.
- An exact typed capability leaf may name a fixture/helper that codegen must
  implement under its declared writable test roots. If that exact symbol is not
  on disk yet but the plan maps its bounded generated target, treat the absence
  as codegen work (or bounded `needs_fix` when the mapping is incomplete), not
  as a human approval requirement. Escalate only when the capability leaf itself
  is absent or the required implementation would cross the declared write set.
