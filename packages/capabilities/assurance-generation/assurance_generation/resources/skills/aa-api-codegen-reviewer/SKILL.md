# API codegen review

Capability-owned API codegen review skill. Do not select a provider, model, or
adapter.

Review the generated API tests and mapping and emit a `PlanReviewAuthoring` document from
`assurance_generation.contracts`. When mechanical plan-check facts are present,
treat them as evidence only; the downstream gate owns routing. Their absence is
not a reason to stop because this skill independently reviews the generated tests and mapping.

Required observation keys must appear as `aa_observe.request(observation_id=...)`
calls. A passing client assertion without that call, a same-named fake fixture,
or a missing observation ID is not collection-closed.

Routing uses exactly two fields: `route` and `finding_ids`. The host validates
that combination and does not rewrite it. An illegal pair is invalid output
and retries this node.

Evidence-proven, bounded defects use `route: auto_fix` and list every bounded
finding ID in `finding_ids`. Severity alone does not require human review.
Use `route: human` only when correction requires a missing product, policy,
authorization, or safety decision; then leave `finding_ids` empty. A
codegen-ready review uses `route: codegen` and `"finding_ids": []`.

`finding_ids` has one exact JSON shape: an array of non-empty finding ID
strings, for example `"finding_ids": ["API-PLAN-001", "API-PLAN-002"]`.
Never put objects, `finding_id`/`action` pairs, prose, or locator data in this
array. Put repair detail in the matching finding's `message` and `locator`, and
in `next_action`. Use `"finding_ids": []` for `codegen`, `human`, and `reject`.

For `auto_fix`, `finding_ids` must contain exactly the set of finding IDs
present in this response: no missing IDs and no extra IDs. Remove IDs for
findings that were resolved in an earlier round. Immediately before returning,
compare the two sets and correct the response if they differ.

Do not stop the review after finding the first defect. Before choosing a
decision, complete one exhaustive pass across every locked generated test, testdata file, and mapping
and every selected case. Review case semantics only: request bodies, auth,
setup data, assertion oracles, and cleanup. Return all independently
observable case-semantic defects in the same review document so a single
bounded codegen re-entry can repair the whole package.

Do not emit needs_fix for the wheel-owned pytest runner contract. The host
sets `asyncio_mode=auto`. Missing `@pytest.mark.asyncio`,
`pytest_asyncio.fixture`, Run Guidance Markers, Tortoise/session fixtures,
or sync-versus-async invocation wording are not plan defects.

On a codegen re-entry, still write a complete valid `route` and `finding_ids`
pair. The host does not drop new IDs or keep a prior `codegen` sticky.

Evaluate boundary expressions rather than accepting narrative labels. An upper
bound is not an exact length. Compare constructions, expected responses and
runtime assertions across every plan table and summary. The audit makes omissions
and source contradictions checkable; it does not replace semantic source review.

Before the first decision, exact-read the locked generated tests and mapping and each
selected case. Check request, assertion, and cleanup fidelity against the
frozen case oracle and independently read product source. Do not reopen the
wheel-owned pytest runner contract. On retry, only the previous
`finding_ids` remain in scope.

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
within the authorized generated tests and mapping. Include every bounded finding in `finding_ids`.
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

- `qa/cases/**/case.yaml`

### optional

- `.aa/data-knowledge.yaml`
- product source under the project source root (read-only contract evidence)
- `qa/tests/api/**` and `qa/tests/testdata/domain/**`

## Outputs

### required

- `qa/results/review/api-codegen-review.json`
- `qa/results/review/api-codegen-review-summary.md`

The JSON file and final assistant JSON are two deliveries of the same complete
`PlanReviewAuthoring` object. Build that object once using every required field
in the supplied result schema, including `route` and `finding_ids`.
Write the entire object to `api-codegen-review.json`, then read and parse that exact
file. After any correction, rewrite and re-read the file first; a corrected final
response alone does not repair the staged artifact. The Markdown summary is the
human-readable summary, not a replacement or reduced shape for the JSON file.
### Host-owned derived files

Your outputs are the review JSON and Markdown summary. After authenticating
the raw review, the host finalize handler generates history and finding-scope
under declared write claims. Digests and identities are computed by the host.
Return the same complete JSON object that you wrote to the review file.

## Boundaries

Write only the review outputs listed above. Authorizing bounded codegen re-entry
does not permit the reviewer to edit tests, testdata, mapping, cases, or
knowledge files. Each automatic finding must point at a locked generated test
or mapping path and a bounded key/section that codegen can revise.

A finding locator anchors the observed defect. Do not split one conceptual
defect into a new finding solely for each occurrence.

The graph owns phase state. Do not write an orchestration state file.

## Proven Product Defects

A source-proven SUT defect is test evidence, not a missing product decision.
When the approved requirement and expected assertion are clear, keep the test
intent and let execution and reporting record the failure. Do not require the
SUT defect to be corrected before codegen. Use `route: codegen` when the mapped test remains executable; reserve
`route: human` for a genuinely missing intent, product, policy,
authorization, credential, or safety decision.

## Initial administrator credential review

When the API plan requires administrator authentication, independently compare `admin_username`
and `admin_password` in the plan with the exact startup initialization or seed source cited by the
approved plan. Both values must deterministically match the values used to create the initial
administrator. Do not read `.env` or `*.env` files as credential evidence.

Treat a missing value, a contradictory value, or any credential fallback such as a test-runtime
default as a non-codegen finding. Use bounded `route: auto_fix` when the product source
proves the correction; use `route: human` only when the source cannot resolve the required
credential without a runtime or policy decision.

## Domain Notes

Emit `route`, `finding_ids`, and
`risk_level`. Use `route: codegen` when the plan is codegen-ready. Each finding must include
`id`, `severity`, `category`, `message`, and `locator` (`artifact` plus optional
`case_id` / `key`). Point locators at `plans/api-codegen-mapping.json` when the
defect is a mapping row.

Required capability closure:

- Copy every `required_capabilities` key verbatim from a typed leaf that exists
  in `.aa/data-knowledge.yaml`; a valid-looking prefix is not evidence.
- Never invent a constraint or adapter suffix. If a needed leaf is absent, use
  `route: human` instead of `route: codegen` with a virtual key.
- Emit `route: codegen` only when every required key
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
  response identifier is a non-codegen finding.
- Inspect the declared API and shared test-data trees before claiming a mapped
  test, fixture, or helper is absent.
- For every mapped helper that is absent, validate in the same review both its
  generated target and its actual source-backed implementation boundary,
  including required fixture or credential handoff. Do not assume an HTTP
  transport can expose persistence-only rows when the inspected router has no
  such operation; map a source-proven database/session read boundary instead.
- When existing source or a declared typed capability proves a corrected method,
  payload, identifier lookup, or mapping, use bounded `route: auto_fix`; do not require
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
  as codegen work (or bounded `route: auto_fix` when the mapping is incomplete), not
  as a human approval requirement. Escalate only when the capability leaf itself
  is absent or the required implementation would cross the declared write set.
