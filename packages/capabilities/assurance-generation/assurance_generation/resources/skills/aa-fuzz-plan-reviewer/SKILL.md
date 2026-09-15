# Fuzz plan review

Capability-owned fuzz plan review skill. Do not select a provider, model, or
adapter.

Review fuzz plans and emit a `PlanReviewAuthoring` document from
`assurance_generation.contracts`.

Evidence-proven, bounded defects use `needs_fix`: set `auto_fix_allowed: true`,
`human_review_required: false`, `codegen_readiness: not_ready`, and list every
bounded finding ID in `auto_fix_plan`. Severity alone does not require human
review. Use `needs_human_review` only when correction requires a missing
product, policy, authorization, or safety decision; then prohibit automatic
repair and leave `auto_fix_plan` empty. A codegen-ready `pass` never requires
human review.

`auto_fix_plan` has one exact JSON shape: an array of non-empty finding ID
strings, for example `"auto_fix_plan": ["FUZZ-PLAN-001", "FUZZ-PLAN-002"]`.
Never put objects, `finding_id`/`action` pairs, prose, or locator data in this
array. Put repair detail in the matching finding's `message` and `locator`, and
in `next_action`. Use `"auto_fix_plan": []` for `pass` and
`needs_human_review`.

For `needs_fix`, `auto_fix_plan` must contain exactly the set of finding IDs
present in this response: no missing IDs and no extra IDs. Remove IDs for
findings that were resolved in an earlier round. Immediately before returning,
compare the two sets and correct the response if they differ.

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

Before deciding the first review, build one complete support/runtime inventory:

- From every plan artifact, exact-read every shared module, fixture, and helper
  claimed as reusable. A reusable claim requires the exact module and symbol to
  exist; a plausible path or name is not evidence.
- For every concrete runtime-support path or dotted Python symbol named by a
  plan, call the native read tool on that exact path before any glob or grep.
  Gitignored files remain exact-readable. If that exact read was not attempted,
  do not emit an absence finding from glob or grep output.
- For every mapped pytest target, read every ancestor `conftest.py` and verify
  the fixture name, returned value, and handoff into the request or test helper.
- For schema loaders, validate schema acquisition as an executable expression, including how a
  relative schema path is joined to a runtime base URL and how a fallback is
  converted into the library's schema object.
- Trace cleanup, authentication, environment variables, database/session paths,
  and every create-if-missing support boundary through both the plan and codegen
  plan.

Do not stop after the first defect. Complete the inventory and review every
required plan artifact, then return all independently observable defects in the
same response. A repaired artifact does not narrow the next review: repeat the
whole inventory and all checks on every round. Do not defer another
independently observable defect to a later round merely because one bounded
finding already requires re-entry.

### required

- `qa/results/plans/fuzz-plan.md`
- `qa/results/plans/fuzz-codegen-plan.md`
- `qa/results/plans/fuzz-codegen-mapping.json`
- `qa/results/plans/fuzz-review-summary.md`
- `qa/cases/**/case.yaml`

### optional

- `.aa/data-knowledge.yaml`
- product source under the project source root (read-only)
- `qa/tests/fuzz/**` and `qa/tests/testdata/domain/**`

## Outputs

### required

- `qa/results/review/fuzz-plan-review.json`
- `qa/results/review/fuzz-plan-review-summary.md`

## Boundaries

Write only review outputs. Authorizing bounded planner re-entry does not permit
the reviewer to edit plan files. Each automatic finding must point to an exact
plan artifact and bounded key/section that the planner can revise from observed
source. A missing `fuzz-plan-checks.json` is not a stop condition; when present,
consume it as deterministic evidence. The graph owns phase state. Do not write
an orchestration state file.

## Proven Product Defects

A source-proven SUT defect is test evidence, not a missing product decision.
When the approved requirement and expected assertion are clear, keep the test
intent and let execution and reporting record the failure. Do not require the
SUT defect to be corrected before codegen. Use `pass` with
`ready_with_warnings` when the mapped test remains executable; reserve
`needs_human_review` for a genuinely missing intent, product, policy,
authorization, credential, or safety decision.

## Domain Notes

Independently inspect both `fuzz-plan.md` and `fuzz-codegen-plan.md`. Each must
contain the exact `## Test Function Mapping` heading with a four-column
`Case ID | Test Function | Target File | Schema Acquisition` table. Return a
non-pass review when either file lacks the independently parseable mapping or
when the two relations differ.

Every mapped function must use `test_<case_id_lowercase>__<behavior>` with the
complete Case ID. For a codegen-ready plan, emit `"decision": "pass"`.

Required capability closure:

- Copy every `required_capabilities` key verbatim from a typed leaf that exists
  in `.aa/data-knowledge.yaml`; a valid-looking prefix is not evidence.
- Never invent constraint suffixes. If a needed leaf is absent, use
  `needs_human_review` with `not_ready`.

Runtime contract closure:

- For URI schema acquisition, resolve each fuzz target by exact `(METHOD, PATH)`
  membership in the live OpenAPI document before passing the plan.
- A codegen-ready plan must execute cases generated by the exact selected
  operation's `operation.as_strategy()`. Merely inspecting the operation or
  deriving hand-authored payload shapes from it does not qualify; finite
  deterministic probes may supplement, but never replace, the generated-case
  execution strategy.
- Schemathesis `case.call(..., session=...)` accepts only a `requests.Session`;
  never pass an `httpx.Client` as the Schemathesis `session`. Reject a plan
  that conflates an inherited HTTPX lifecycle fixture with Schemathesis's
  Requests transport; require separate generated-request and cleanup handoffs.
- Validate every positive seed against the real request schema. Reject unproven
  reserved email domains such as `example.test`.
- Reject a rejection oracle for inputs that do not violate an observed schema
  or application constraint.
- When an existing method/path, schema, or application constraint proves a
  corrected bounded strategy, use `needs_fix`; do not require a human merely
  because the defect is blocking.
- An incorrect application import, router export, schema loader, or fallback
  acquisition path is a bounded automatic finding when repository source proves
  the exact replacement. Put its finding ID in `auto_fix_plan`; never escalate
  that source-backed correction to human review.
- When the requirement owner has already frozen "the project's existing
  response contract" and source defines a concrete response class or envelope,
  use that source-backed envelope as contract evidence. A missing OpenAPI
  `response_model`/success content schema is a reportable contract-documentation
  gap, not a new product decision. Route a plan that relies only on the missing
  OpenAPI response schema to bounded `needs_fix`: revise its oracle to the exact
  source-backed envelope and let execution/issue analysis expose any mismatch.
- Escalate a success-response oracle only when both the frozen requirement and
  inspected source leave the accepted shape genuinely ambiguous. Do not ask a
  human to restate an acceptance criterion already present in the requirement.
- Require an endpoint/property strategy on the typed plan result.
