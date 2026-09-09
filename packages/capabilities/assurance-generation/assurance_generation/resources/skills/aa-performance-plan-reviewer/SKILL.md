# Performance plan review

Capability-owned performance plan review skill. Do not select a provider,
model, or adapter.

Review performance plans and emit a `PlanReviewAuthoring` document from
`assurance_generation.contracts`.

Evidence-proven, bounded defects use `needs_fix`: set `auto_fix_allowed: true`,
`human_review_required: false`, `codegen_readiness: not_ready`, and list every
bounded finding ID in `auto_fix_plan`. Severity alone does not require human
review. Use `needs_human_review` only when correction requires a missing
product, policy, authorization, or safety decision; then prohibit automatic
repair and leave `auto_fix_plan` empty. A codegen-ready `pass` never requires
human review.

`auto_fix_plan` has one exact JSON shape: an array of non-empty finding ID
strings, for example `"auto_fix_plan": ["PERF-PLAN-001", "PERF-PLAN-002"]`.
Never put objects, `finding_id`/`action` pairs, prose, or locator data in this
array. Put repair detail in the matching finding's `message` and `locator`, and
in `next_action`. Use `"auto_fix_plan": []` for `pass` and
`needs_human_review`.

For `needs_fix`, `auto_fix_plan` must contain exactly the set of finding IDs
present in this response: no missing IDs and no extra IDs. Remove IDs for
findings that were resolved in an earlier round. Immediately before returning,
compare the two sets and correct the response if they differ.

Do not stop the review after finding the first defect. Before the first
decision in every round, close this runtime inventory for the whole package,
not only for the section most recently edited:

- Exact-read `.aa/data-knowledge.yaml`, resolve each consumed dotted Python
  symbol to its `.py` module, and read that module before classifying the symbol
  as present, absent, reusable, or create-if-missing.
- Read implementations of every mapped helper and adapter and verify the exact
  authentication return value, setup and cleanup handoff, host configuration,
  and lifecycle ownership.
- For every concrete runtime-support path or dotted Python symbol named by a
  plan, call the native read tool on that exact path before any glob or grep.
  Gitignored files remain exact-readable. If that exact read was not attempted,
  do not emit an absence finding from glob or grep output.
- Reconcile those facts across every required plan artifact and summary before
  emitting all independently observable findings. A repaired artifact does not
  narrow the next review: repeat this complete checklist on every retry.

A finding locator authorizes exactly one artifact and key/section. When one
conceptual defect requires corrections in multiple sections or artifacts, emit
one finding per target and include every finding ID in `auto_fix_plan`.

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

## Inputs

Read `proposal.md` first from the locked inputs. When its `Product Source Verification`
section lists exact product-source paths, read every listed path directly before any discovery.
A glob result of `No files found` is not evidence that product source is absent;
ignored source files remain exact-readable. Only declare source unavailable after
those exact reads and a path-scoped grep both fail.

The final JSON instruction part contains the mechanically locked
`review_input_paths`. Use the native read tool to read every listed path
directly before reviewing. Do not use glob, wildcard search, or ignore-aware
file discovery under `qa/changes/` to decide whether an input exists. The host
has already verified these exact paths as regular files.

### required

- `qa/changes/<change-id>/plans/performance-plan.md`
- `qa/changes/<change-id>/plans/performance-codegen-plan.md`
- `qa/changes/<change-id>/plans/performance-codegen-mapping.json`
- `qa/changes/<change-id>/plans/performance-review-summary.md`
- `qa/changes/<change-id>/cases/**/case.yaml`

### optional

- `.aa/data-knowledge.yaml`
- product source under the project source root (read-only)
- `tests/perf/**` and `tests/testdata/domain/**`

## Outputs

### required

- `qa/changes/<change-id>/review/performance-plan-review.json`
- `qa/changes/<change-id>/review/performance-plan-review-summary.md`

## Boundaries

Write only review outputs. Authorizing bounded planner re-entry does not permit
the reviewer to edit plan files. Each automatic finding must point to an exact
plan artifact and bounded key/section that the planner can revise from observed
source. A missing `performance-plan-checks.json` is not a stop condition; when
present, consume it as deterministic evidence. The graph owns phase state. Do
not write an orchestration state file.

A helper outside the codegen write whitelist can still be imported and reused.
Exact-read each declared setup, cleanup, and authentication symbol. When it is
present and already satisfies the lifecycle, reject a plan that requires
codegen to modify such a helper; route the planner to describe unchanged reuse
and keep codegen writes confined to mapped targets.

## Proven Product Defects

A source-proven SUT defect is test evidence, not a missing product decision.
When the approved requirement and expected assertion are clear, keep the test
intent and let execution and reporting record the failure. Do not require the
SUT defect to be corrected before codegen. Use `pass` with
`ready_with_warnings` when the mapped test remains executable; reserve
`needs_human_review` for a genuinely missing intent, product, policy,
authorization, credential, or safety decision.

## Domain Notes

Consume the same Task Mapping structure emitted by the planner:

- Validate the explicit `## Task Mapping` table independently in both
  `performance-plan.md` and `performance-codegen-plan.md`.
- Headers are `Case ID | Task Method | Target File`.
- Require exactly one row for every selected Performance Case ID.
- Reject a Case ID that appears more than once.
- Require the mapped method to be the primary executable load-test task under
  `tests/perf/**`.

For a codegen-ready plan, emit `"decision": "pass"`.

Required capability closure:

- Copy every `required_capabilities` key verbatim from a typed leaf that exists
  in `.aa/data-knowledge.yaml`; a valid-looking prefix is not evidence.
- Never invent a constraint or adapter suffix. If a needed leaf is absent, use
  `needs_human_review` with `not_ready`.

Runtime contract closure:

- Verify setup, measured, and cleanup operations by exact method/path against
  live OpenAPI when available, otherwise against router/schema source.
- Reject a hard-coded load-test host when the declared runtime provides
  `BASE_URL` or `API_BASE_URL`.
- Verify that seed identifier extraction matches the real create response, or
  that the plan names a supported lookup. Reject assumed `data.id` response
  shapes as `not_ready`.
- When an existing method/path and response shape prove a corrected lookup,
  route the exact affected plan sections through bounded `needs_fix`; do not
  require a human merely because the defect is blocking.
- Require scenario identity and numeric thresholds on the typed plan result.
