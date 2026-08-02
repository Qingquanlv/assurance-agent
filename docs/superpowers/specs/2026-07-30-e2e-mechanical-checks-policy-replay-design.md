# API/E2E Mechanical Checks and Frozen Policy Replay Design

**Date:** 2026-07-30
**Status:** Approved for implementation planning

## 1. Context

The layer-assurance foundation now provides:

- a code-owned, immutable `LayerAssuranceProfile` for API, E2E, Fuzz, and Performance;
- deterministic layer applicability derived from case artifacts;
- a shared four-check registry;
- version 2 `PlanCheckDocument` evidence with explicit layer and applicability state; and
- profile-declared plan, review, check, and gate artifact names.

The runtime wiring and benchmark replay have not yet caught up with that foundation. API runs
mechanical checks before its reviewer, E2E does not run mechanical checks in its graph at all,
and benchmark policy replay is hard-coded to API paths and the current on-disk schema. The result
is neither cross-layer nor temporally sound.

This design is the second rollout increment. It makes API and E2E use one graph-engine-owned
assurance flow and replaces the API-only benchmark experiment with a four-layer, frozen-input
counterfactual policy matrix. Fuzz and Performance remain visibly unwired until the next
increment.

## 2. Problems

### 2.1 API mechanical evidence can be stale relative to its review

`capability_keys` consumes `review.required_capabilities`, but the current API graph runs
mechanical checks before the reviewer. On a first pass the review is absent and the check has no
requirements to evaluate. On a later pass it can consume a previous review. The attached review
gate then evaluates a newly authored review against mechanical evidence produced from different
inputs.

### 2.2 E2E has no runtime mechanical control path

The E2E profile and generic check execution already exist, but `e2e-plan-cycle` runs directly from
`START` to the reviewer. Its review gate reads only the review and L1, and its codegen precondition
does not read mechanical evidence. A valid-looking review can therefore reach E2E codegen without
the deterministic checks ever running.

### 2.3 Empty scope conflicts with the review contract

An inapplicable layer must be derived from cases, not authored by a reviewer. Invoking the reviewer
before that decision is both wasteful and unsound: `PlanReview` requires a non-empty capability
contract, while an empty layer has no plan assurance work to review. Empty scope needs a
deterministic pre-review branch.

### 2.4 The benchmark replay is not frozen or layer-aware

The current specialty reporter loads only API review/check artifacts, writes API artifact
overrides, invokes `api-plan-review-gate`, and evaluates the current schema and policy. It calls
this replay even when the schema, policy, or final disk artifacts differ from what the historical
gate actually consumed. Its output cannot distinguish a genuine counterfactual result from an
experiment over drifted inputs.

### 2.5 The real E2E authoring contract does not satisfy `shared_factory`

The shared check requires a `Factory Mapping` table in each codegen plan when L1 declares shared
factories. The API plan skill declares that table; the E2E plan skill declares a different data
setup table. Synthetic round-trip fixtures include the expected table and therefore hide the
production contract mismatch.

## 3. Goals

1. Make API and E2E use the same applicability, review, mechanical, and gate topology.
2. Ensure applicable mechanical evidence is generated after the current review and before gate
   evaluation.
3. Bypass the reviewer for an objectively inapplicable layer while still producing complete
   `not_applicable` evidence.
4. Make both API and E2E codegen preconditions fail closed on missing or invalid mechanical
   evidence.
5. Align real E2E plan/reviewer/fixer contracts with the four shared mechanical checks.
6. Produce a deterministic four-layer policy replay matrix from the run's frozen schema, policy,
   and gate-read evidence.
7. Keep graph topology explicit in the graph schema and keep policy actions global.

## 4. Non-Goals

- Wiring Fuzz or Performance capability contracts, mechanical nodes, or plan-review gates.
- Trace layer summaries, sufficiency joins, or recovery projection changes.
- Per-layer policy actions or project-configurable applicability expressions.
- Generating graph nodes, edges, or routes from Python profiles.
- Replacing the graph DSL or gate evaluator.
- Replaying agents, historical wall-clock behavior, or human decisions.
- The exhaustive four-layer round-trip, mutation, codegen-only, and resume matrix; that remains a
  later verification increment. This increment still includes the minimum API/E2E no-bypass
  coverage required for safe wiring.

## 5. Ownership Boundary

The implementation must preserve this separation:

| Concern | Authority |
|---|---|
| Layer names, case types, artifact paths, aliases, gate IDs, static check applicability | `LayerAssuranceProfile` |
| Nodes, edges, loops, interrupts, resume actions, and routes | `workflow-schema.yaml` |
| Applicability and mechanical facts | Deterministic Python operations |
| Gate verdicts and first-true rule order | Graph gate definitions and the existing gate evaluator |
| The next executable node | Graph planner |
| Counterfactual policy scenarios | A reusable replay engine invoking the real gate evaluator |

Operations return facts, never a next node or gate verdict. Profiles do not generate graph
topology. The compiler and mutation tests validate that the explicit graph agrees with the
profile-declared contract.

## 6. Runtime Architecture

### 6.1 Applicability preflight

Add one generic deterministic operation:

```text
derive_plan_layer_applicability(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult
```

The task receives `with.layer`. The operation:

1. resolves the `LayerAssuranceProfile`;
2. loads `cases/**/case.yaml` in deterministic path order;
3. validates only the fields required by the applicability seam;
4. calls the existing `derive_layer_applicability`; and
5. returns the typed `LayerApplicability` as its task value.

It reads cases only and writes no product, project, or change artifact. Unknown layers are
`invalid_input`; malformed cases are `invalid_output`. A parser failure is never converted to
`no_automated_cases`.

The operation result is a graph input, not a second source of persistent truth. The later
mechanical operation re-derives applicability before producing evidence so case drift cannot be
hidden by an earlier task value.

### 6.2 Shared API/E2E graph shape

Both `api-plan-cycle` and `e2e-plan-cycle` use this explicit topology:

```text
START -> applicability
applicability[applicable] -> review -> mechanical-plan-checks -> review-gate
applicability[not applicable] -> mechanical-plan-checks -> review-gate

review-gate[needs_fix] -> fix -> review
review-gate[knowledge_remediation] -> knowledge-remediation
knowledge-remediation[fix_and_proceed] -> mechanical-plan-checks
review-gate[needs_human_review] -> human-review
review-gate[pass or skip] -> END
review-gate[reject or stop] -> STOP
```

The applicability branches are ordinary graph `when` expressions over
`node('applicability').value.applicable`. Python does not choose the branch.

The reviewer node no longer has an attached gate. A separate `builtin:gate` node evaluates the
layer's profile-declared gate after the canonical mechanical artifact exists. Routes select
`plan_review_route('review-gate')`, preserving the existing split between ordinary human review
and knowledge remediation.

For an inapplicable layer, the reviewer is absent. The mechanical operation writes complete v2
evidence in which every known check is `not_applicable/layer_not_applicable`; the gate's valid
empty-scope rule returns `skip` before it references review or L1 fields.

### 6.3 Applicable mechanical execution

The API and E2E graph nodes invoke the generic mechanical operation with a declared
`require_review: true` input. For an applicable layer this mode requires:

- every exact profile plan artifact;
- the profile's canonical review artifact validated as `PlanReview`;
- `.aa/data-knowledge.yaml` validated as `DataKnowledge`; and
- validated applicability cases.

The operation also requires `review.review_type == f"{profile.layer}-plan"` and
`review.change_id == context.change_id`; Pydantic validity alone is not sufficient identity
binding.

It then runs all profile-applicable checks, validates the resulting `PlanCheckDocument` against
the profile, and atomically writes only `profile.checks_artifact`.

For an inapplicable layer, plans, review, and L1 are not required and are not interpreted as a
substitute for the case-derived applicability result.

The legacy non-reviewed execution mode is not gate-consumable. It may remain temporarily for
unwired Fuzz/Performance tests, but no wired review or codegen gate may treat its applicable
evidence as authoritative. The later Fuzz/Performance increment will switch those layers to the
review-required mode.

### 6.4 Iteration and remediation freshness

- A plan fix can change plan content and capability requirements, so it returns to `review`, then
  mechanical checks, then the gate.
- Knowledge remediation changes L1 while preserving the reviewed requirement list, so
  `fix_and_proceed` returns to mechanical checks, then the gate.
- A human `fix_and_proceed` returns through the fixer path and therefore through review and
  mechanical checks.
- `accept_risk` remains bound to the audited gate read and is not reinterpreted by the replay
  engine.

No agent node is authorized to write the mechanical evidence artifact.

Knowledge promotion occurs outside the frozen invocation tree. The mechanical execution contract
therefore synchronizes `project:.aa/data-knowledge.yaml` under the dedicated
`project:data-knowledge` exclusive lock. Merely overlaying that path when the task starts is not
enough: the current runtime performs ordinary materialization repair before scheduler overlay, so
a promoted L1 can otherwise be rejected as workspace drift before the retry is runnable.

The runtime must use one synchronized-aware repair boundary with two ordered phases.

First, it reaches a recovery barrier without accepting any live drift:

1. reconcile running attempts and reproject;
2. finish or fail pending write-set commit recovery against each write set's recorded base;
3. replay and acknowledge committed synchronized publications under their durable recorded lock
   tokens; and
4. reproject after every append or repair until no pending write set or prepared publication
   remains.

This phase completes and releases its durable recovery lock scopes before any next-wave lock is
acquired. It cannot borrow a future task's synchronization declaration to make an old pending
update valid.

Only from that stable projection does the runtime enter selected-wave repair:

1. derive the next runnable wave from the projected object tree without appending its planned
   events;
2. recursively preview already-created child invocations to identify the concrete runnable leaf
   wave;
3. take project lock tokens from the selected outer wave's compiled descendant footprint, but
   take drift-exempt synchronized paths only from the concrete leaf tasks' own operation
   contracts;
4. under those locks, capture the concrete declared live paths once and overlay the same bytes
   onto both the repair base and repair target;
5. perform ordinary drift comparison and materialization repair for every other canonical path;
6. require a second plan/wave check at every traversed invocation to select the same task
   identities, operation contracts, and resource footprint; and
7. execute the nested wave from that exact overlay tree without releasing the outer lock or
   recapturing the synchronized inputs.

If there is no existing concrete synchronized leaf, the runtime performs ordinary strict repair;
it does not exempt a speculative descendant path. Lock reservation may conservatively use a
compiled descendant union, as it does today, but that union is never also treated as the set of
allowed live changes.

The task attempt and committed superstep retain that overlay tree lineage, so a successful
mechanical retry pins the promoted L1 bytes. A failed attempt can retry the same procedure. No
path outside the selected wave's concrete `synchronized` declarations is exempted from drift
repair, and the union of all graph contracts is never used as a repair allowlist. A mutation to
any unrelated path still fails closed.

## 7. Gate Evidence State and Verdict Semantics

### 7.1 Deep validation helper

Raw `check_failed()` is deliberately false for missing or malformed evidence, so it cannot be a
completeness guard. Add one deterministic gate DSL helper with a deep interface:

```text
plan_assurance_state(checks, review, data_knowledge, layer)
    -> invalid | not_applicable | applicable
```

The helper:

- validates v2 `PlanCheckDocument` and the requested `LayerAssuranceProfile`;
- returns `not_applicable` only for a valid layer-level empty-scope document;
- for applicable evidence, validates the canonical `PlanReview` and `DataKnowledge` inputs;
- requires `review.review_type == f"{layer}-plan"`, so an API review cannot satisfy E2E and vice
  versa;
- uses `GateEvaluationContext.change_id` and requires `review.change_id` to match it;
- returns `invalid` for missing, malformed, wrong-layer, incomplete, duplicate, or unknown
  evidence; and
- never returns `applicable` because a referenced field happened to be missing.

Gate rules call `check_failed` only after `plan_assurance_state == 'applicable'` has passed.

### 7.2 Rule order

API and E2E gates use the same semantic order:

1. invalid assurance state -> `stop`;
2. valid `not_applicable` assurance state -> `skip`;
3. reviewer `needs_fix` with auto-fix allowed -> `needs_fix`;
4. missing required L1 capability leaves -> `needs_human_review`, routed to
   `knowledge_remediation`;
5. reviewer human/risk conditions or a failed check with `require_human` ->
   `needs_human_review`;
6. explicit reviewer reject/not-ready or a failed check with `block` -> `reject`;
7. valid reviewer pass/readiness, present capabilities, and all remaining existing conditions ->
   `pass`;
8. otherwise -> fail-closed default `stop`.

This preserves the gate engine's canonical `needs_fix -> needs_human_review -> reject -> pass`
order. A `decision == 'reject'` guard prevents explicit reviewer rejection from being upgraded to
human review or remediation. `warn` retains findings but does not change the baseline reviewer
verdict.

Capability absence is a hard precondition outside plan-check action policy. Consequently,
`policy.plan_checks.capability_keys = warn` cannot authorize codegen with missing L1 leaves.

### 7.3 Codegen preconditions

API and E2E codegen preconditions require all of the following:

- the current plan-review subgraph completed successfully;
- the profile's checks artifact has a valid applicable assurance state;
- the corresponding plan-review gate resolves to `pass`; and
- formal L1 exists.

The codegen-precondition gate has an explicit `skip` rule for a valid not-applicable assurance
state, and the parent branch routes that verdict to `END`. An inapplicable child graph therefore
ends without invoking codegen. Missing disk evidence, a stale subgraph result, or a wrong-layer
document cannot be promoted to pass.

## 8. E2E Authoring Contract Alignment

### 8.1 Factory Mapping

`aa-e2e-plan` adds this required section to `e2e-codegen-plan.md` whenever L1 declares shared
factories:

```markdown
## Factory Mapping

| Shared Module | Function | Ownership |
|---|---|---|
| tests/testdata/domain/dept.py | make_dept | reuse through E2E adapter |
```

The E2E reviewer validates it and the E2E fixer preserves or repairs it only through authorized
findings. The shared `shared_factory` parser remains authoritative; no E2E-private parser is
introduced. Browser-side synchronous code still uses an adapter when the shared factory itself is
asynchronous.

### 8.2 L1 path rule

`.aa/data-knowledge.yaml` remains the only legal L1 path token. The mechanical rule stays lexical
and deterministic; it does not attempt to infer natural-language negation in English or Chinese.
Plans must therefore omit pseudo-L1 tokens even in historical or negative prose. Authoring and
reviewer instructions use only the canonical path.

### 8.3 Canonical real-contract fixture

Add one fixture generated from the actual E2E authoring contract rather than ad hoc test prose. It
contains:

- an automated E2E case with a 4xx rejection expectation;
- typed L1 data with a shared factory and E2E adapter;
- all three exact E2E plan artifacts, including Factory Mapping and contiguous Case ID tables;
- a complete `PlanReview` with fully qualified required capability keys; and
- only the canonical L1 path.

The fixture must produce zero findings for `l1_path`, `shared_factory`, `assert_ideal`, and
`capability_keys` before policy wiring can be considered complete.

## 9. Carry-Forward Foundation Corrections

The following reviewed gaps are prerequisites for this increment and are included in its scope:

1. profile registry construction enforces the exact four-layer set and uniqueness of aliases and
   gate IDs in addition to the existing path/check invariants;
2. a present review is validated by the profile's registered review model rather than extracting
   a partial field mapping;
3. applicable L1 is validated as `DataKnowledge` rather than any YAML mapping; and
4. capability evidence cites `profile.review_artifact`, so E2E references
   `review/plan-review.json` rather than a synthesized `review/e2e-plan-review.json`.

The E2E ingest catalog entry is also aligned with the canonical `review/plan-review.json` path.

## 10. Execution and Write Contracts

- Applicability preflight reads only `change:cases/**/case.yaml` and has no synchronized writes.
- Reviewed mechanical execution declares exact profile plan reads, case reads, the canonical
  review read, formal L1 read, and the single checks output.
- Reviewed mechanical execution declares
  `synchronized: [project:.aa/data-knowledge.yaml]` and
  `exclusive: [project:data-knowledge]`, while retaining the canonical repo-level L1 read. This
  serializes API/E2E L1 snapshots and makes post-remediation live state visible to resume.
- API/E2E reviewer and fixer contracts cannot write their checks artifacts.
- The explicit gate reads only the profile-declared review, checks, and L1 paths.
- The replay engine writes only inside an isolated temporary project; the benchmark adapter writes
  the final report atomically.

Contract rendering and task events remain graph-engine responsibilities. The profile supplies
metadata but does not authorize writes.

## 11. Frozen Counterfactual Policy Replay

### 11.1 Semantics

The feature is a deterministic counterfactual sensitivity analysis, not a historical workflow
re-execution:

```text
counterfactual_plan_check_actions/v1
```

It asks: given the gate definition, non-plan-check policy fields, and evidence that this run
actually used, what would the real gate evaluator report if every plan-check action were `warn`,
`block`, or `require_human`?

The replay runs no agent, applies no resume action, and mutates no real policy or artifact.
Scenario verdicts cannot change workflow, archive, quality, or benchmark product verdicts. A
failure to establish replay input integrity may still fail the specialty evidence stage, as
defined in section 13.2.

### 11.2 Frozen definition binding

The benchmark runner records the exact root `invocation_id` returned by the workflow command in
the item's durable run state and passes it to the collector. Resume retains that ID. The collector
validates that the event exists, has no parent, belongs to the requested change and workflow
entrypoint, and is the invocation represented by the item's terminal workflow result. It never
discovers a v2 root by taking the latest invocation from disk. A missing or contradictory binding
is `incomplete: root_invocation_unbound`; legacy runs without the binding remain v1-display-only.
All layer rows in one report must descend from that same authoritative root, so the collector
never combines layers from different runs.

For that invocation, the collector must:

1. strictly parse graph events and identify the authoritative root invocation;
2. load the normalized runtime policy snapshot from
   `.graph-runtime/policies/<policy_digest>.json`;
3. read `policy_origin` (`project` or `packaged_default`) from the invocation event;
4. parse the snapshot and require its canonical digest to equal the invocation's recorded
   `policy_digest`;
5. for a project-origin policy, additionally require the source bytes in `root_tree_id` to parse
   to the same digest;
6. load `.graph-runtime/schemas/<graph_digest>.json`;
7. validate it as `WorkflowSchemaV2`, recompute its canonical digest, and require equality with
   the recorded graph digest; and
8. use the gates from that pinned schema, never the current packaged schema.

GraphRuntime must persist the normalized policy mapping when starting a new root invocation, just
as it already pins the normalized workflow schema. The content-addressed file contains only the
canonical normalized policy bytes, so identical policy content has one immutable file regardless
of origin. `GraphInvocationStartedEvent` records `policy_origin` separately and bumps its event
schema version. This is required even when the origin is the packaged default: a future packaged
default cannot reconstruct historical bytes from a digest alone. The snapshot is atomically
written and read-only after creation.

Pinned YAML alone does not freeze Python gate semantics. The invocation event therefore also
records:

- `gate_semantics_digest`, derived from a code-owned versioned manifest covering rule evaluation,
  every DSL/helper callable used by replayable plan gates, `capabilities_present`,
  `plan_review_route`, and the assurance evidence validators; and
- `assurance_profile_digest`, derived from the normalized four-profile registry and ordered check
  catalog.

Replay requires both recorded digests to equal the current compatible implementation before it
invokes Python. A mismatch is `incomplete: gate_semantics_mismatch` or
`incomplete: assurance_profile_mismatch`, never a best-effort replay. The semantic manifest lists
each callable and an explicit semantic version, but versions alone are not the digest input. Each
entry also contains a mechanically computed implementation digest over its normalized Python AST
(`ast.dump(tree, include_attributes=False)`) plus the normalized values of referenced code-owned
constants and rule tables. The aggregate digest additionally includes the Python minor version
and locked versions of parsing/validation libraries that can change evaluation semantics.

An AST call/dependency coverage test requires every code-owned callable, class validator, and
constant reachable from the replayable gate DSL to appear in the manifest. A second test
recomputes every implementation digest from source, so changing a branch without bumping the
human semantic version still changes `gate_semantics_digest`. Mutation coverage changes each
counterfactual-only policy branch independently and proves that the recorded digest becomes
incompatible even when the frozen baseline policy would not exercise that branch.

The new invocation-event fields are optional only when parsing earlier event-schema versions.
Producing a v2 specialty report requires non-empty `policy_origin`, `gate_semantics_digest`, and
`assurance_profile_digest`; an older invocation without them remains renderable through v1
compatibility but is not silently upgraded to replayable v2 evidence.

As an additional calibration, replay first evaluates the frozen baseline policy over the bound
raw evidence and frozen params. Its gate ID, verdict, matched rule, reason, value, and normalized
details must equal the corresponding fields in the recorded gate report. The route is not a gate
report field: replay derives it with the frozen `plan_review_route` semantics and, when subsequent
activation/skip events exist, cross-checks the derived route against those pinned events. A gate
field disagreement is `incomplete: baseline_gate_mismatch`; a contradictory route event is
`incomplete: baseline_route_mismatch`. Only after calibration may counterfactual scenarios run.

Missing, ambiguous, or digest-mismatched definitions produce typed incomplete results. The
collector does not silently fall back to current files.

### 11.3 Gate-read evidence binding

For each wired and selected layer, follow `parent_invocation_id` from child invocations to the
authoritative root and select the unique child whose graph ID and structural path correspond to
that profile's plan cycle. Within that child, select the last **committed** explicit gate attempt
in ledger order before the child's terminal event. Match start/success by `attempt_id`, then
require the task ID in a later `superstep_committed.committed_task_ids` entry for the same
invocation. Failed, abandoned, or succeeded-but-uncommitted attempts are ignored. A later
committed gate attempt supersedes every earlier one, and its commit `target_tree_id` is the
primary tree for evidence recovery.

The selected child invocation's recorded `policy_digest`, `graph_digest`,
`gate_semantics_digest`, and `assurance_profile_digest` must equal the authoritative root binding.
The gate evaluated the child task workspace, so a divergent child definition cannot be replayed
using root definitions. Any mismatch is the corresponding typed definition-integrity failure.

The succeeded event must contain a gate report for `profile.gate_id`. Its `reads_sha256` must
always bind the canonical checks artifact. It must additionally bind the canonical review and L1
for applicable evidence. For a valid inapplicable document those two files are intentionally
absent and their hashes are not required.

Resolve the exact bytes from the recorded task/tree history when possible. Current or archived
disk bytes may be used only when their digests match the gate report. If bytes cannot be recovered
or a digest differs, emit `incomplete: gate_evidence_unbound` or
`incomplete: gate_evidence_drift`.

Before replay, validate:

- review as `PlanReview`;
- checks as v2 `PlanCheckDocument` and against the layer profile; and
- L1 as `DataKnowledge` for applicable layers.

A valid inapplicable checks document does not require review or L1 bytes and replays to `skip` in
all three scenarios. Any other missing expected hash is `incomplete: gate_evidence_unbound`.

The selected gate attempt also binds the mechanical execution contract. Select the last committed
mechanical attempt in ledger order before that gate whose
`outputs_sha256[f"change:{profile.checks_artifact}"]` equals
`gate_report.reads_sha256[profile.checks_artifact]`. One shared logical-path normalization helper
must perform this prefixed-output/unprefixed-read comparison. The matching
`task_attempt_started.contract_digest` is the reported
`mechanical_execution_contract_digest`. A later matching committed generation supersedes an
earlier identical-byte generation; succeeded-but-uncommitted attempts do not participate. No
matching committed producer makes the row incomplete rather than guessing provenance.

### 11.4 Replay engine

Put the reusable evaluator in the runtime/orchestration layer rather than the benchmark script:

```text
replay_plan_check_policy(
    *,
    gates: Mapping[str, GateDef],
    profile: LayerAssuranceProfile,
    review: BoundArtifact[PlanReview] | None,
    checks: BoundArtifact[PlanCheckDocument],
    data_knowledge: BoundArtifact[DataKnowledge] | None,
    base_policy: Policy,
    change_id: str,
    params: Mapping[str, object],
) -> LayerPolicyReplay
```

`BoundArtifact[T]` contains the canonical logical path, verified raw bytes, SHA-256 digest, and
the parsed model `T`. The engine stages `raw_bytes` verbatim; it never reserializes a model and
then claims the result has the historical gate hash.

`params` comes from the selected layer child invocation, including the frozen
`force_continue` value. Replayable API/E2E plan-review gates may depend on reads, `params`, and
policy, but not on `state`, `node()`, or another `gate()`. Compiler guards reject those hidden
dependencies for these two gates; replay therefore uses empty state/node scopes without changing
semantics.

For the fixed action order `warn`, `block`, `require_human`, it:

1. copies the frozen policy and replaces every `plan_checks` action with the scenario action;
2. stages policy plus the gate-bound raw evidence bytes at canonical paths in a temporary
   project/change tree;
3. uses an empty audit-events directory so historical human decisions cannot mask a branch;
4. invokes the existing `check_gate_in_view` for `profile.gate_id`; and
5. records action, scenario policy digest, verdict, `plan_review_route`, matched rule, reason, and
   missing capabilities.

Artifact overrides are not used because the gate's audited read hashes must describe the same
bytes the evaluator consumed.

When hard capability remediation determines the result before plan-check policy can act, the
scenario records `policy_effect: shadowed_by_capability_precondition`. It must not claim that the
scenario action controlled the verdict.

`policy_effect` is one of `applied`, `no_failed_checks`,
`shadowed_by_capability_precondition`, or `shadowed_by_gate_precondition`. Passing evidence uses
`no_failed_checks`. A scenario uses `applied` when at least one policy-eligible applicable check
failed and no earlier gate precondition determined the result. For `block` and `require_human`
this normally means an explicit policy rule matched; for `warn`, the policy-controlled fall-through
to the baseline reviewer verdict also counts as applied. If failed checks exist but an earlier
reviewer needs-fix, human/risk, or explicit-reject rule determines the result, the effect is
`shadowed_by_gate_precondition`; `matched_rule` and `reason` identify the exact shadowing rule.
Capability shadowing is used only when the capability precondition itself is the first determining
rule.

## 12. Four-Layer Replay Matrix

The collector emits exactly one row per profile in stable profile order. The wired set for this
schema version is exactly `{api, e2e}` and is declared by the report contract, not inferred from
artifact existence.

```python
LayerReplayStatus = Literal[
    "complete",
    "not_selected",
    "not_wired",
    "incomplete",
]
```

Selection comes from the pinned graph's actual layer-branch predicates, never from whether a file
happens to exist or from `test_types` alone. The collector locates the unique `assurance` child
invocation under the authoritative root, resolves each profile's branch node from the pinned
schema, and evaluates that node's `when` expression with the assurance invocation's frozen params
and the same versioned DSL semantics. This preserves conjunctions such as `test_types` plus
`run_mode`; for example, an `api-only` run does not select E2E merely because the default
`test_types` contains E2E.

The four replay-classifying branch predicates are a params-only DSL subset. The compiler rejects
artifact symbols, `state`, `node()`, `gate()`, `file_exists()`, or other ambient inputs in those
predicates; the collector independently validates the pinned expressions against the same subset.
This keeps selection replayable from frozen invocation params instead of silently treating an
unbound dependency as false.

If a corresponding `node_activated` or `node_skipped` event exists, it must agree with the
predicate result. A contradiction is `incomplete: selection_evidence_mismatch`. Event absence is
allowed only when an earlier terminal outcome prevented the branch decision; the predicate still
determines selected versus unselected, and a selected wired layer without gate evidence becomes
incomplete. Zero or multiple candidate assurance invocations or layer branch nodes produce a
typed incomplete definition-binding result. This selection classification happens before graph
owner discovery, allowing selected Fuzz/Performance to remain `not_wired` in this increment.

- API/E2E selected and valid: `complete`, with exactly three scenarios.
- API/E2E valid but dynamically inapplicable: `complete`, applicability `not_applicable`, and all
  three scenario verdicts `skip`.
- Any unselected layer: `not_selected`, with no fabricated scenarios.
- Selected Fuzz/Performance in this increment: `not_wired`, with no fabricated scenarios.
- Selected API/E2E with missing or invalid binding: `incomplete`, with a typed reason and no
  borrowed artifact.

Each complete layer row has this fixed core shape (scenario diagnostic fields may be extended
compatibly, but the named fields and four-check completeness are mandatory):

```json
{
  "layer": "e2e",
  "case_type": "E2E",
  "status": "complete",
  "reason_code": null,
  "applicability": "applicable",
  "gate_id": "e2e-plan-review-gate",
  "review_artifact": "review/plan-review.json",
  "checks_artifact": "review/e2e-plan-checks.json",
  "capabilities": {
    "required": ["capabilities.domain_factories.dept.make_dept"],
    "missing": []
  },
  "mechanical_checks": {
    "status": "fail",
    "finding_count": 1,
    "checks": [
      {"check_id": "l1_path", "status": "pass", "finding_count": 0},
      {"check_id": "shared_factory", "status": "fail", "finding_count": 1},
      {"check_id": "assert_ideal", "status": "pass", "finding_count": 0},
      {"check_id": "capability_keys", "status": "pass", "finding_count": 0}
    ]
  },
  "mechanical_execution_contract_digest": "sha256:mechanical-contract-example",
  "evidence_digests": {
    "review": "sha256:review-example",
    "checks": "sha256:checks-example",
    "data_knowledge": "sha256:l1-example"
  },
  "scenarios": [
    {
      "action": "warn",
      "policy_digest": "sha256:warn-policy-example",
      "verdict": "pass",
      "route": "pass",
      "matched_rule": "pass_when",
      "reason": "review passed; failed mechanical check is warning-only",
      "missing_capabilities": [],
      "policy_effect": "applied"
    },
    {
      "action": "block",
      "policy_digest": "sha256:block-policy-example",
      "verdict": "reject",
      "route": "reject",
      "matched_rule": "reject_when",
      "reason": "failed mechanical check is blocking",
      "missing_capabilities": [],
      "policy_effect": "applied"
    },
    {
      "action": "require_human",
      "policy_digest": "sha256:human-policy-example",
      "verdict": "needs_human_review",
      "route": "needs_human_review",
      "matched_rule": "needs_human_review_when",
      "reason": "failed mechanical check requires human review",
      "missing_capabilities": [],
      "policy_effect": "applied"
    }
  ]
}
```

The matrix key is `(change_id, layer)`. Duplicate or unknown layers, duplicate actions, missing
actions on a complete row, or unstable row order invalidate the report.

`mechanical_checks.checks` is ordered by `PLAN_CHECK_IDS` and contains every known check exactly
once. Its aggregate status and finding count must reconcile with the source `PlanCheckDocument`.
For a complete inapplicable row all four entries are `not_applicable` with zero findings.
Every scenario contains exactly the eight fields shown above. For an inapplicable complete row,
`capabilities`, `evidence_digests.review`, and `evidence_digests.data_knowledge` are `null`, while
the checks digest and mechanical contract digest remain mandatory.

## 13. Specialty Report Version 2

New collection writes `schema_version: "2"` and reshapes the capability section around layers.
The following is the definition-binding fragment; the same object also contains exactly the four
layer rows defined in section 12:

```json
{
  "semantics": "counterfactual_plan_check_actions/v1",
  "integrity": "complete",
  "definition_binding": {
    "root_invocation_id": "invocation-example",
    "assurance_invocation_id": "assurance-invocation-example",
    "graph_digest": "sha256:graph-example",
    "gate_definition_source": "pinned_schema",
    "baseline_policy_digest": "sha256:policy-example",
    "policy_source": "pinned_runtime_snapshot",
    "policy_origin": "project",
    "gate_semantics_digest": "sha256:gate-semantics-example",
    "assurance_profile_digest": "sha256:assurance-profile-example"
  }
}
```

The existing traceability/evidence projection object is preserved unchanged.

`integrity` is the closed set `complete | incomplete` and is derived, not caller supplied:

- `complete` requires a valid definition binding, exactly four valid rows in stable profile order,
  and every selected wired row to have `status: complete`;
- expected `not_selected` and selected-but-unwired `not_wired` rows do not lower integrity; and
- any definition-binding failure, invalid matrix invariant, or `incomplete` row forces top-level
  `integrity: incomplete`.

The report model validator enforces this equivalence in both directions. The capability replay
stage contributes a non-zero result exactly when its derived integrity is `incomplete`; other
specialty sections may fail independently. A report cannot claim complete while containing an
incomplete layer.

Markdown rendering adds:

1. `Layer Assurance Matrix`, covering applicability, mechanical checks/findings, capability
   counts, and row status; and
2. `Policy Replay Matrix`, keyed by change and layer with warn/block/require-human verdicts.

Fuzz/Performance cells show `not_selected` or `not_wired`; they never display an API-derived
verdict.

### 13.1 v1 compatibility

- The new collector emits only v2.
- The renderer may read an already frozen v1 report and label it `legacy_api_only`.
- A v1 report does not synthesize missing layer rows and cannot satisfy new four-layer acceptance
  checks.
- An archived v1 report may still be displayed during resume. A new or safely recollectable item
  must produce v2.

### 13.2 Incomplete reports

The collector atomically writes typed incomplete rows before returning failure. A selected,
wired API/E2E row with missing definitions, artifacts, event binding, or digest integrity makes
the specialty stage exit non-zero. This retains diagnostic evidence while preventing incomplete
replay from being counted as success.

The benchmark caller must still validate and append an atomically written incomplete v2 report to
the render set before setting `SPECIALTY_REPORT_FAILED=true`. A non-zero collector exit may not
discard the report path or reduce the final summary to a generic log-only error.

Expected `not_wired` rows for Fuzz/Performance and all `not_selected` rows do not fail this
increment's specialty stage.

## 14. Graph/Profile Consistency Guards

Profiles remain metadata, while topology remains explicit YAML. Compile-time/runtime-facing tests
must prove for each wired profile:

- exactly one plan-cycle graph owns `profile.gate_id`;
- the graph contains one applicability operation for the profile layer;
- applicable and inapplicable paths both reach the profile's mechanical producer;
- review precedes the applicable mechanical producer;
- the explicit gate follows the producer;
- the gate reads the exact profile review/check/L1 paths with canonical aliases;
- the codegen precondition reads the exact checks artifact;
- fix and remediation edges re-enter at the approved freshness points; and
- no Python profile field is used to synthesize nodes or edges.

For the schema-v2 wired set `{api, e2e}`, replay discovers graph ownership from the pinned schema
using gate ID, operation target, and `with.layer`. Zero or multiple matches produce
`incomplete: ambiguous_graph_wiring`. Fuzz/Performance are classified as `not_selected` or
`not_wired` before owner discovery, so their intentionally absent wiring is not an ambiguity.

## 15. Error Model

| Condition | Runtime result | Replay/report result |
|---|---|---|
| Unknown layer | applicability/mechanical `invalid_input` | unknown row invalidates report |
| Malformed case | applicability `invalid_output` | `incomplete: no_successful_gate_evidence` |
| Applicable missing plan/review/L1 | mechanical `invalid_output` | `incomplete: missing_evidence` |
| Malformed review/L1 | mechanical `invalid_output` | `incomplete: invalid_evidence` |
| Invalid/wrong-layer/incomplete checks | gate `stop` | `incomplete: invalid_checks` |
| Valid empty scope | gate `skip` | complete row; all scenarios `skip` |
| Missing required capability leaf | knowledge remediation | complete row with shadowed policy effect |
| Mechanical finding | policy-driven gate verdict | scenario records real evaluator result |
| Missing/contradictory benchmark root binding | no fallback | `root_invocation_unbound`, specialty failure |
| Pinned schema/policy digest mismatch | no fallback | typed incomplete, specialty failure |
| Gate semantics/profile digest mismatch | no fallback | typed incomplete, specialty failure |
| Baseline gate/route disagreement | no fallback | typed incomplete, specialty failure |
| Layer predicate/event disagreement | no fallback | typed incomplete, specialty failure |
| Gate-read evidence drift | no fallback | typed incomplete, specialty failure |

`not_applicable` is never synthesized from missing or invalid inputs.

## 16. Verification Strategy

### 16.1 Mechanical and authoring tests

- Applicability preflight: API/E2E applicable, empty, manual-only, other-layer, and malformed cases.
- Applicable mechanical mode rejects missing or malformed review/L1 and exact plan omissions.
- Empty scope succeeds without plan/review/L1 and emits every known check as not applicable.
- The real-contract E2E fixture produces zero findings across all four checks.
- Mutating Factory Mapping, canonical L1 path, case table continuity, rejection expectation, or a
  required capability produces the expected named finding.

### 16.2 Gate truth table

- Failed check + warn preserves the reviewer baseline verdict.
- Failed check + block rejects.
- Failed check + require-human requests human review.
- Passing and not-applicable checks are inert for all three actions.
- Reviewer needs-fix precedes a blocking check.
- Explicit reject is not upgraded by human/remediation conditions.
- Capability absence routes to knowledge remediation and is recorded as policy-shadowing.
- Failed checks shadowed by reviewer needs-fix, human/risk, or explicit-reject rules report
  `shadowed_by_gate_precondition` rather than claiming that the scenario action applied.
- Missing, malformed, incomplete, wrong-layer, or statically inconsistent evidence stops.
- Empty scope skips without a review artifact.

### 16.3 Graph and codegen tests

- API and E2E packaged topology matches the approved graph shape.
- Both branches invoke the reviewed mechanical producer before their explicit gate.
- API/E2E codegen preconditions stop on missing or invalid checks and cannot pass from old disk
  artifacts without a successful current review-cycle.
- Fix and L1 remediation return through their designated freshness points.
- Promoting L1 between a knowledge-remediation interrupt and `fix_and_proceed` reaches the next
  mechanical/gate attempt with the promoted bytes, while an unrelated live-path mutation still
  fails materialization repair.
- Running-attempt reconciliation, pending write-set recovery, and committed publication replay
  finish before selected-wave preview; a succeeded-but-uncommitted predecessor cannot expose its
  downstream wave early.
- A parent subgraph's conservative descendant lock footprint does not exempt synchronized paths
  from inactive leaf branches.

The later verification increment expands these into the exhaustive codegen-only/resume and
ledger-reconstruction matrix.

### 16.4 Replay tests

- API and E2E failing-check action matrices invoke the real pinned gate.
- Passing evidence is inert; inapplicable evidence skips for all actions.
- Capability remediation shadows plan-check actions explicitly.
- Fuzz/Performance are always present and are `not_selected` or `not_wired`.
- Missing/malformed/wrong-layer/incomplete evidence produces typed incomplete rows.
- The collector uses the benchmark item's persisted root invocation ID; a later workflow run in
  the same change cannot replace it, and a missing/foreign root binding fails closed.
- Gate path, alias, or owner mutation produces `ambiguous_graph_wiring` or `gate_not_wired`.
- Policy root-tree, pinned-schema, and gate-read digest mismatch suppress scenario verdicts.
- A succeeded-but-uncommitted gate or mechanical attempt is ignored; the last committed attempt
  is selected deterministically.
- Bound JSON/YAML raw bytes are staged verbatim, including formatting that would change under
  model reserialization, and their replay reads retain the recorded hashes.
- Frozen child params, including `force_continue`, are passed to the evaluator and can change the
  calibrated baseline exactly as they did in the original gate.
- A packaged-default policy snapshot survives a later default change; project/default policies
  with equal normalized content share snapshot bytes while retaining distinct `policy_origin`.
- Gate-semantics or assurance-profile digest drift suppresses replay, and a baseline gate or route
  mismatch produces its dedicated incomplete reason.
- Mutating a counterfactual-only gate branch or reachable validator without changing its manual
  semantic version still changes the normalized implementation digest and suppresses replay.
- The pinned branch predicate, including both `run_mode` and `test_types`, drives all four row
  selections; an API-only run with default test types marks E2E `not_selected`.
- A layer-branch predicate mutation that introduces state, artifact, node, gate, or filesystem
  dependencies is rejected by both graph compilation and pinned-report validation.
- Audit events cannot influence replay.
- Two identical inputs produce byte-identical JSON and stable Markdown row order.
- v1 rendering is visibly legacy and never satisfies v2 completeness.
- Top-level integrity is complete exactly for valid four-row reports without an incomplete selected
  wired row; all contradictory integrity/row combinations fail model validation.

### 16.5 CI

The implementation is not complete until all of the following pass:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest -q
bash scripts/packaging_smoke_test.sh
```

## 17. Rollout Order

1. Close the four carry-forward profile/input/path gaps.
2. Add the two-phase synchronized recovery/selected-wave repair boundary.
3. Add applicability preflight and deep gate evidence-state validation.
4. Migrate API and E2E to the shared graph shape and codegen preconditions.
5. Align E2E plan/reviewer/fixer Factory Mapping and canonical fixtures.
6. Add frozen policy/semantic bindings and the reusable replay engine.
7. Upgrade specialty collection/rendering to four-layer v2.
8. Run focused mutation/truth-table coverage and full CI.

The first runtime rollout keeps existing project policy values unchanged. Replay remains
observational. No default action is promoted from `warn` to `block` by this design.

## 18. Acceptance Criteria

- API and E2E applicable mechanical evidence is generated after the current review.
- Empty API/E2E scope bypasses the reviewer and produces complete not-applicable evidence.
- API/E2E use explicit graph gate nodes; operations do not decide routes or verdicts.
- Fix and knowledge-remediation paths cannot feed stale evidence to the next gate.
- Synchronized L1 refresh is scoped to the selected wave and cannot hide unrelated workspace
  drift.
- API/E2E codegen cannot pass on missing, invalid, wrong-layer, or old-disk mechanical evidence.
- A strict real E2E authoring fixture passes all four checks with zero findings.
- Capability absence remains a hard knowledge-remediation precondition.
- Policy replay uses the run's frozen root policy, pinned schema, and gate-bound evidence bytes.
- Policy replay refuses incompatible gate semantics, assurance profiles, baseline results, or
  branch-selection evidence instead of approximating them with current code.
- The benchmark item's exact root invocation ID and params-only pinned branch predicates determine
  the four rows; a later run or ambient workspace artifact cannot replace that identity.
- Every v2 benchmark report contains exactly API, E2E, Fuzz, and Performance rows.
- Selected API/E2E rows contain exactly warn/block/require-human scenarios or an explicit typed
  incomplete reason.
- Fuzz/Performance are explicit `not_selected`/`not_wired`, never omitted or coerced to API.
- Capability-policy integrity is mechanically derived and cannot be complete when a selected wired
  row or definition binding is incomplete.
- v1 reports remain displayable but cannot masquerade as four-layer v2 evidence.
- All CI gates pass without new warnings.

## 19. Explicit Refinements to the Umbrella Design

This spec makes three deliberate refinements:

1. API is migrated together with E2E because its pre-review `capability_keys` evidence has the
   same temporal flaw; leaving it unchanged would make the two replay rows incomparable.
2. Gate order follows the existing engine's canonical needs-fix-before-reject semantics. The
   earlier umbrella prose that listed plan-check block before reviewer verdict is not adopted.
3. The four-layer replay shape is introduced now, while Fuzz/Performance remain explicit
   `not_wired` rows until their dedicated wiring increment.

These are intentional scope and semantic decisions, not incidental implementation deviations.
