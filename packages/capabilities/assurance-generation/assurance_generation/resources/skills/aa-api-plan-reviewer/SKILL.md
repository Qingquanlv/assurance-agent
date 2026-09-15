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
decision, complete one exhaustive pass across every required plan artifact
and every selected case. Review case semantics only: request bodies, auth,
setup data, assertion oracles, and cleanup. Return all independently
observable case-semantic defects in the same review document so a single
bounded planner re-entry can repair the whole package.

Do not emit needs_fix for the wheel-owned pytest runner contract. The host
sets `asyncio_mode=auto`. Missing `@pytest.mark.asyncio`,
`pytest_asyncio.fixture`, Run Guidance Markers, Tortoise/session fixtures,
or sync-versus-async invocation wording are not plan defects.

On a planner re-entry, review only the finding IDs listed in the previous
`auto_fix_plan`. A repaired artifact narrows the next review to those IDs;
the reviewer must not add finding IDs. A prior pass is sticky: do not
convert it to `needs_fix` because a later pass noticed a new defect.

## Verifiable review coverage

The locked instruction includes `review_requirements`: exact selected Case IDs,
input refs/digests and helper observations prepared by Python. Return `review_audit`
in both the review JSON artifact and your final JSON result, in every round and for
every decision (including `needs_fix`). Copy its `input_refs` and
`planning_facts_digest` from the requirements, never invent hashes.

The host owns the coverage table. Missing rows, foreign `evidence_paths`,
stale digests, Import Strategy locations, `unknown` planned invocation, and
stub-as-existing claims are repaired at finalize and do not veto the review.
Mark `finding` or `pass` for case semantics; do not spend the review on
bookkeeping.

For each selected Case ID, emit exactly one `cases` row with all six `checks`:
`request`, `auth`, `setup`, `assertion`, `cleanup`, `helpers`. Each result is
`pass`, `finding` or `not_applicable`; request and assertion are always applicable.
Include `evidence_paths`, `finding_ids` and a concise `rationale` explaining the
checks, especially any not-applicable result. Prefer `evidence_paths` from
`review_requirements.allowed_evidence_paths`. A source file being readable
does not add it to that inventory; the host drops unknown paths. A finding
result links to IDs in this review. Complete every row before deciding, even
when an early row fails.

For every helper in `review_requirements.helpers`, emit exactly one `helpers` row.
Copy `capability`, `symbol`, `declared_kind`, `target_file`, `observed_signature`
and `observed_async` exactly. `null` means unobserved, not absent. The L1 kind and
the actual Python definition are separate facts: `helper` alone does not imply
sync, and `async_factory` must not be invented for a helper. Inspect the source
and specify the intended `invocation` (`sync`, `async`, or `unknown`).

Classify `implementation` as `existing`, `planned`, or `unresolved`. An existing
implementation needs its observed source in `evidence_paths` and must not be a
stub. A planned creation or amendment needs `plan_location` (`artifact`, exact
`section` heading) naming its target in Target Files, Codegen Scope, Output File
Candidates or Factory Mapping; an import alone is not a generated-target contract.
A `planned` helper also needs a plan-supported `invocation` of `sync` or `async`.
For a pytest fixture this describes the fixture implementation (`def` or
`async def`), not a direct call by the test; pytest injects the resulting value.
Keep unobserved signature/async fields `null` even when the plan specifies a
future implementation. If its invocation cannot be established, classify it as
`unresolved` and link a finding asking the planner to specify it; `unknown` is
not a codegen-ready planned invocation. An unresolved helper links a finding and
prevents `pass`. Always cite
`.aa/data-knowledge.yaml` for the declaration; include `finding_ids` and a concise
`rationale` for lifecycle, fixture and environment handoff. Module references can
include transitive helpers, such as a role adapter importing a domain factory;
check their generated targets and provisioning as part of the same review.

Evaluate boundary expressions rather than accepting narrative labels. An upper
bound is not an exact length. Compare constructions, expected responses and
runtime assertions across every plan table and summary. The audit makes omissions
and source contradictions checkable; it does not replace semantic source review.

Before the first decision, exact-read the locked plan package and each
selected case. Check request, assertion, and cleanup fidelity against the
frozen case oracle and independently read product source. Do not reopen the
wheel-owned pytest runner contract. On retry, only the previous
`auto_fix_plan` IDs remain in scope.

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

For each defect, inspect its references across the complete package. Give the
finding a precise locator and describe all related occurrences that must change
within the authorized plan package. Include every bounded finding in `auto_fix_plan`.
Independently verify source-backed claims;
the facts digest binds observations, not semantic truth. Re-review repaired output
before passing; the desired repair count never changes the acceptance criteria.

## Durable mapping and the execution view

Closed mapping `target_file` values must stay under `qa/tests/`.
Execute runs durable `qa/tests/` in place with `pythonpath=qa`.
Fixtures and support modules live under `qa/tests/`.
Treat a missing `qa/tests/**/conftest.py` as fixture unavailability.
Do not look up fixtures under the SUT `tests/` tree.
Do not retarget mapping rows to `tests/`.
For every concrete runtime-support path or dotted Python symbol named by a
plan, call the native read tool on that exact path before any glob or grep.
Gitignored files remain exact-readable. If that exact read was not attempted,
do not emit an absence finding from glob or grep output.

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

The JSON file and final assistant JSON are two deliveries of the same complete
`PlanReviewAuthoring` object. Build that object once using every required field
in the supplied result schema, including `codegen_readiness` and `review_audit`.
Write the entire object to `api-plan-review.json`, then read and parse that exact
file. Check its required fields and audit evidence-path membership before
returning the parsed object as the final JSON. After any correction, rewrite and
re-read the file first; a corrected final response alone does not repair the
staged artifact. The Markdown summary is the human-readable summary, not a
replacement or reduced shape for the JSON file.

## Boundaries

Write only the review outputs listed above. Authorizing bounded planner re-entry
does not permit the reviewer to edit plan files. Each automatic finding must
point to an exact plan artifact and bounded key/section that the planner can
revise from observed source.

A finding locator anchors the observed defect. Planner re-entry may synchronize
that same finding's related occurrences throughout its locked plan-package write
set, including removal of stale repair notes. Do not split one conceptual defect
into a new finding solely for each occurrence. This permission does not extend to
unrelated decisions, frozen case oracles, product code, tests or knowledge files.

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
