# Fuzz/Performance Assurance Wiring and Frozen Policy Replay Design

**Date:** 2026-07-31
**Status:** Approved for implementation planning
**Rollout increment:** 3

## 1. Context

The first two assurance rollout increments established a code-owned
LayerAssuranceProfile for all four layers, deterministic case-derived
applicability, a shared four-check registry, strong API/E2E plan-review
contracts, explicit API/E2E mechanical and gate topology, and a frozen
four-layer specialty replay report.

Fuzz and Performance deliberately remained visible but unwired in the second
increment. Their profiles already declare plan, review, checks, and gate
artifacts, but the runtime still has the legacy behavior:

- Fuzz and Performance reviews use the permissive Review model rather than
  PlanReview.
- Their reviewers author layer_applicable even though applicability is
  objectively derivable from cases.
- Their review gates read only reviewer JSON and do not consume mechanical
  evidence or L1.
- Their codegen gates re-evaluate the shallow reviewer verdict and can neither
  prove a current mechanical producer nor bind the evidence used by the gate.
- Their human fix path ends the cycle rather than proving a new review,
  mechanical artifact, and verdict.
- Specialty replay hard-codes API and E2E as wired and therefore reports every
  selected Fuzz or Performance layer as not_wired.

This is the third rollout increment. It activates Fuzz and Performance using
the same graph-engine-owned assurance protocol as API and E2E, while preserving
their different static check applicability and their lack of automatic plan
fixers. It also changes replay from a current-code layer allowlist to
pinned-topology classification.

This design preserves the ownership decisions and frozen-evidence rules in the
two preceding specs while refining their Fuzz/Performance rollout boundary and
replay wiring classification. API/E2E verdict behavior remains authoritative.

## 2. Problems

### 2.1 The declared profiles are not runtime controls

The Fuzz and Performance profiles declare capability contracts and shared
checks, but no current graph node materializes reviewed mechanical evidence.
The profiles therefore describe a capability that the runtime does not
enforce.

### 2.2 Reviewer-authored applicability is an authority inversion

The legacy gates use review.layer_applicable to skip an empty layer. A reviewer
can incorrectly skip an applicable layer, or force review and codegen for an
empty one. Malformed cases can also be hidden behind a reviewer-authored false
value.

### 2.3 Applicable review artifacts are too weak

The generic Review model permits missing change identity, readiness, risk,
capability, finding identity, and next-action fields. The capability check
treats an absent requirement list as no missing capability, creating a free
pass on a field that is supposed to be a hard precondition.

### 2.4 The real plan contracts do not satisfy the shared parser

The shared_factory check recognizes a Factory Mapping section with Shared
Module, Function, and Ownership columns. The current Fuzz and Performance
skills use different domain-specific headings, so a synthetic fixture can
pass while a plan produced from the real skill contract fails.

### 2.5 The current codegen gates do not prove freshness

The Fuzz and Performance precondition gates have no direct reads and only call
the shallow plan-review gate. They do not prove that:

- the current review cycle succeeded;
- a committed mechanical producer created the checks read by the gate;
- the review and checks belong to the current change and layer; or
- the L1 bytes used by the gate contain all required capabilities.

### 2.6 Replay confuses implementation support with historical wiring

The specialty collector currently classifies wiring with a hard-coded set.
Adding Fuzz and Performance to that set would retroactively reinterpret old
pinned graphs as wired. Leaving them out would report new fully wired runs as
not_wired. Artifact existence cannot solve this because stale files may exist
for an unwired graph and a newly wired graph may fail before producing them.

### 2.7 Profile and gate digest checks currently erase useful history

Replay rejects the whole definition binding when a historical global assurance
profile or gate-semantics digest differs from current code. Upgrading only
Fuzz and Performance changes those global digests, so an old pinned graph
would become globally incomplete before replay could correctly classify its
Fuzz and Performance rows as not_wired.

### 2.8 A Fuzz policy field has only a tautological consumer

The current Fuzz pass expression checks that
policy.fuzz.required_when_endpoint_has_auth is not null. The policy model
already requires a boolean, so this expression is always true and does not
establish whether an authenticated endpoint requires Fuzz assurance.

## 3. Goals

1. Make selected Fuzz and Performance layers execute deterministic
   applicability, strong review, mechanical checks, an explicit plan gate,
   and a codegen precondition.
2. Make applicable Fuzz and Performance reviews satisfy the same cross-skill
   PlanReview contract as API and E2E.
3. Keep assert_ideal statically not applicable to Fuzz and Performance while
   applying l1_path, shared_factory, and capability_keys.
4. Ensure every codegen path, including codegen-only and resume, is backed by
   current committed review, mechanical, gate, and L1 evidence.
5. Preserve manual remediation for layers that have no automatic fixer.
6. Classify each replay row from the pinned graph: complete, incomplete,
   not_wired, or not_selected.
7. Preserve definition-binding-valid v4+ pinned Fuzz/Performance legacy graphs
   as not_wired even after current runtime support is added.
8. Make partial wiring, malformed inputs, missing evidence, and digest drift
   fail closed.
9. Keep graph topology explicit and graph-engine-owned.

## 4. Non-Goals

- Adding Fuzz- or Performance-specific mechanical check IDs.
- Applying assert_ideal to robustness properties or performance thresholds.
- Adding automatic Fuzz or Performance plan fixer skills.
- Generating graph nodes, edges, or routes from LayerAssuranceProfile.
- Introducing per-layer plan-check policy actions.
- Deriving endpoint authentication from reviewer prose.
- Enforcing policy.fuzz.required_when_endpoint_has_auth without a typed
  endpoint-auth fact.
- Changing API/E2E verdict semantics or remediation topology.
- Changing TraceProjection, execution-result sufficiency, trace recovery, or
  execution-result joins.
- Replaying agents, human deliberation, wall-clock behavior, or external
  services.

## 5. Ownership Boundary

The following authority split is mandatory.

| Concern | Authority |
|---|---|
| Layer, case type, exact artifact paths, aliases, gate ID, review model, static check set | LayerAssuranceProfile |
| Nodes, edges, loops, interrupts, resume actions, and codegen routing | workflow-schema.yaml |
| Layer applicability | Deterministic applicability operation |
| Mechanical facts | Shared plan-check operation |
| Verdict and first-true precedence | Explicit gate definition and gate evaluator |
| Next executable node | Graph planner |
| Counterfactual policy scenarios | Frozen replay engine using the real gate evaluator |
| Historical wiring classification | Pinned workflow schema |

Profiles are metadata. They never generate topology or return the next node.
Operations return facts, not routes. The schema remains reviewable as the
complete workflow definition, and compile-time topology checks prove that it
agrees with profile metadata.

## 6. Review and Artifact Contracts

### 6.1 Strong plan-review types

The capability-gated plan-review type set expands from API/E2E to:

- api-plan
- e2e-plan
- fuzz-plan
- performance-plan

PlanReview and PlanReviewAuthoring accept all four types. An applicable review
must contain:

- schema_version;
- review_type;
- change_id;
- decision;
- findings, with a non-empty id on every item;
- auto_fix_allowed;
- auto_fix_plan;
- human_review_required;
- codegen_readiness;
- risk_level;
- required_capabilities; and
- next_action.

required_capabilities is a non-empty list of non-empty, fully qualified L1 leaf
keys. A malformed or empty list is an authoring validation error, not an empty
requirement set.

Fuzz and Performance have no automatic plan fixer in this increment. Their
authoring contracts require:

    auto_fix_allowed: false
    auto_fix_plan: []

The PlanReview runtime validator enforces that conditional invariant whenever
review_type is fuzz-plan or performance-plan. It is not only a prompt
instruction.

The runtime still routes decision needs_fix to manual remediation. The absence
of an automatic fixer does not turn needs_fix into stop or pass.

The legacy layer_applicable field remains part of the broad generic Review
model for unrelated legacy paths, but it is removed from Fuzz and Performance
authoring instructions and is not consumed by applicability, gates, codegen,
or replay.

The PlanReview docstring and schema notes must describe all four plan-review
types and the additional cross-skill validation; they must not claim that the
class merely inherits Review validation.

### 6.2 Exact artifact registration

The artifact registry adds exact entries, before the generic review wildcard:

| Path | Validation model | Authoring model |
|---|---|---|
| review/fuzz-plan-review.json | PlanReview | PlanReviewAuthoring |
| review/performance-plan-review.json | PlanReview | PlanReviewAuthoring |

The Fuzz and Performance profiles change review_model from Review to
PlanReview. A profile, artifact registry, prompt renderer, mechanical handler,
gate-state validator, and replay loader must therefore agree on one model.

Artifact registry resolution is current-path based rather than pinned-version
aware. Consequently, after the exact registrations land, running current
aa validate against an old canonical Fuzz/Performance review file may reject
fields that were previously accepted by generic Review. This design does not
invent a version-aware registry fallback. Definition-valid legacy unwired
replay classifies the pinned topology before evidence recovery and never parses
those review files, so their stricter current validation cannot turn a
not_wired row into wired evidence.

### 6.3 Canonical plan shape

Both Fuzz and Performance codegen plans include a canonical Factory Mapping
section:

    ## Factory Mapping

    | Shared Module | Function | Ownership |
    |---|---|---|
    | tests/factories/account.py | make_account | reuse |

Domain-specific Capability Mapping or Seed Mapping sections may remain, but
they do not replace Factory Mapping. The deterministic shared parser does not
guess layer-specific synonyms.

Fuzz reviewer-owned semantic obligations include schema source, related API
case, authentication semantics, and seed/corpus adequacy. Performance
reviewer-owned obligations include absolute thresholds, load shape, scenario
coverage, and statistical interpretation. This increment does not pretend
that the shared mechanical checks prove those domain judgments.

The Fuzz and Performance reviewer skills are also aligned with the human-only
remediation topology:

- graph-owned phase checks, workflow-state mutations, and reported state
  deltas are removed from plan/reviewer instructions;
- empty scope is no longer reviewed and the skills remove every
  layer_applicable instruction;
- needs_fix uses auto_fix_allowed false, an empty auto_fix_plan, and a manual
  fix/human-review next action;
- no next_action names a nonexistent Fuzz or Performance fixer;
- required_capabilities is always present and non-empty for an applicable
  review; and
- the artifact registry rendered by aa validate is the schema source of truth;
  stale references to a removed TypeScript review schema are deleted.

### 6.4 Execution-contract ownership

The contracts are narrowed to exact ownership:

- Applicability reads change:cases/**/case.yaml and writes nothing.
- A plan skill reads cases, proposal, config, L1, any fact baseline it
  actually consumes, and declared optional memory/source patterns. It writes
  only its declared plan and summary artifacts.
- A reviewer reads the exact layer plans and summary, selected cases, L1, and
  any config it evaluates. It writes only its exact review and summary
  artifacts.
- A reviewer is not authorized to write review/*-plan-checks.json.
- Mechanical execution reads exact plans, cases, the exact review, and
  repo:.aa/data-knowledge.yaml and writes only the profile checks artifact.
- A gate writes no artifact and hashes every audited read.
- Codegen reads exact plans, review, checks, cases, config, L1, and declared
  existing-test/style inputs required by its skill contract.

After normalization to logical resource paths, every mandatory path named by a
skill Context Contract or Inputs section must be covered by its execution
contract reads. Every declared output must be covered by writes and by a
narrow authorization_writes prefix. Optional source inspection is either
covered by an explicit read pattern or removed from the skill; it is not an
undeclared escape hatch.

Workflow phase ordering and state transitions are deliberately absent from
these skill read contracts. The explicit graph and event projection own
progression; adding workflow-state checks back into prompts would create a
second control plane.

The mechanical operation synchronizes project:.aa/data-knowledge.yaml and uses
the project:data-knowledge exclusive lock, matching the API/E2E retry and
promotion boundary. A synchronized path is not a wildcard authorization for
other project writes.

## 7. Applicability and Mechanical Evidence

### 7.1 Deterministic applicability

Fuzz and Performance reuse derive-plan-layer-applicability. A layer is
applicable only when an added or modified case has:

- the profile's exact case type; and
- automation.required whose value is the boolean true.

Removed cases, manual cases, and cases for another layer do not activate the
layer. Missing automation defaults to not required for compatibility.
automation.required values such as the string "true" are invalid.

Malformed mappings, buckets, case types, automation blocks, booleans, or case
IDs cause invalid_output. A parser error never becomes no_automated_cases.

The preflight writes no artifact. The mechanical producer re-derives
applicability from its frozen case inputs so drift cannot be hidden by an
earlier task value.

### 7.2 Static check matrix

Every PlanCheckDocument contains exactly the four known check IDs in canonical
PLAN_CHECK_IDS order.

| Check | Fuzz | Performance |
|---|---:|---:|
| l1_path | applicable | applicable |
| shared_factory | applicable | applicable |
| assert_ideal | not_applicable/check_not_in_profile | not_applicable/check_not_in_profile |
| capability_keys | applicable | applicable |

For an inapplicable layer, all four checks are:

    status: not_applicable
    applicability_reason: layer_not_applicable

For an applicable layer:

- require_review is true;
- all exact profile plans must exist;
- the review must validate as PlanReview;
- review_type must equal the layer plus "-plan";
- review.change_id must equal the runtime change ID;
- L1 must exist and validate as DataKnowledge; and
- the three applicable checks must return pass or fail.

Missing or malformed inputs cause invalid_output. The operation does not write
a partial or synthetic pass document.

### 7.3 Producer and freeze invariant

A checks file is authoritative only when:

1. its operation attempt succeeded;
2. that attempt was included in a committed superstep;
3. the attempt output digest matches the committed tree bytes; and
4. the subsequent gate report read that same digest.

File existence alone is never evidence. Failed, abandoned, uncommitted, or
superseded attempts cannot satisfy a gate or replay.

## 8. Explicit Graph Topology

### 8.1 Parent branches

The Fuzz and Performance parent branches run a cheap cases-only applicability
preflight before any plan agent:

    START -> applicability-preflight
    applicability-preflight[true, non-codegen-only] -> plan -> review-cycle
    applicability-preflight[true, codegen-only] -> review-cycle
    applicability-preflight[false] -> review-cycle

The false route bypasses plan generation. This is required because the real
plan skills stop when no automated layer cases exist and cannot be required to
produce fake empty plans merely to reach a skip gate.

The review cycle intentionally re-derives applicability from its own frozen
case inputs. The parent preflight is an optimization and routing guard, not
persistent evidence. A disagreement caused by drift fails at the normal
workspace/frozen-input boundary or at the cycle's required-input validation;
it never falls back to reviewer-authored scope.

For full or codegen-only execution:

    review-cycle -> codegen-precheck -> codegen -> END

For plan-only or review-plan:

    review-cycle -> END

The old shallow codegen-gate nodes are removed or replaced; they may not
re-evaluate only the reviewer JSON.

### 8.2 Review-cycle topology

Each Fuzz and Performance cycle explicitly declares:

    START -> applicability
    applicability[true] -> review -> mechanical-plan-checks -> review-gate
    applicability[false] -> mechanical-plan-checks -> review-gate

The reviewer has no attached gate. review-gate is a separate builtin:gate
node using the profile gate ID.

The route from review-gate uses plan_review_route and supports:

| Route | Target |
|---|---|
| pass | END |
| skip | END |
| needs_fix | human-review |
| knowledge_remediation | knowledge-remediation |
| needs_human_review | human-review |
| reject | STOP |
| stop | STOP |
| unknown/default | STOP |

The topology validator supports two legitimate remediation shapes:

- an automatic-fixer shape in which fix returns to review; and
- a human-only shape in which needs_fix reaches human-review and
  fix_and_proceed returns directly to review.

It must not require a synthetic fix node for Fuzz or Performance.

### 8.3 Human and knowledge remediation

Human review remains an audited interrupt bound to the layer plan gate.

- human-review.fix_and_proceed returns to review, then mechanical checks and
  the gate;
- human-review.accept_risk uses the existing audited gate-decision mechanism;
- human-review.stop reaches STOP.

Knowledge remediation is a distinct audited interrupt:

- fix_and_proceed returns to mechanical-plan-checks because L1 changed while
  the reviewed requirement list remains the input contract;
- accept_risk, if retained for compatibility, cannot bypass the direct
  capability test in the codegen precondition;
- stop reaches STOP.

An imported manual plan revision requires a new review, mechanical, and gate
attempt committed after the revision tree. The design does not claim that a
plan-only byte change automatically invalidates a decision whose audited gate
reads do not contain plans. Freshness comes from the required post-revision
producer sequence and the codegen precondition, not from an unrecorded plan
digest dependency.

#### Manual plan-revision ingestion

A bare resume action is not sufficient to preserve a user's plan edits. The
runtime ordinarily repairs materialized drift back to the invocation object
tree before selecting the next wave. Without an explicit ingestion boundary,
an edit made while the graph is interrupted would be discarded before the
reviewer runs.

The Fuzz and Performance human interrupts therefore declare an exact
fix_and_proceed revision allowlist containing only their plan-node outputs
under change:plans. The graph schema owns this list; the profile may validate
that its exact plan artifacts are covered but does not generate the list.

For a version-5 interrupt with that allowlist, the leaf cycle interrupt
handler materializes a separate durable writable revision view before its
ephemeral TaskWorkspace is cleaned up:

    .graph-runtime/revision-views/<interrupt-id>/

The view contains only the allowlisted plan files copied from the leaf
invocation's interrupted tree. It is distinct from the read-only audited gate
artifact view. The graph_interrupted projection records the leaf invocation
ID as revision_owner_invocation_id, base tree ID, revision-view path, exact
logical allowlist, and baseline digests. Bubbled parent interrupt projections
preserve that owner unchanged, and status output exposes the view path to the
human operator. Once graph_interrupted is committed, materialization is
create-once for that unresolved interrupt: replay validates the recorded
metadata and preserves existing user edits rather than replacing the view. An
orphan view without a committed interrupt is recreated from the leaf tree and
is never eligible for ingestion. Files and directories within the bounded
view may be edited, but additions, symlinks, and paths outside the exact
allowlist are rejected at resume. The view itself is transport, not evidence;
the accepted immutable target tree and ledger event are authoritative.

On fix_and_proceed, before ordinary materialization repair, the runtime:

1. verifies that the unresolved interrupt, action, user, reason, audited gate
   read, and revision allowlist belong to the same pinned invocation;
2. compares only the declared plan bytes in the recorded revision view with
   the leaf interrupted tree;
3. rejects missing required files, symlink escapes, non-files, undeclared
   changes presented as part of the revision, and digest ambiguity;
4. requires at least one allowlisted path to have a different before/after
   digest; a byte-identical fix_and_proceed is rejected as
   manual_plan_revision_noop and leaves the interrupt unresolved;
5. captures the accepted plan bytes into a new immutable object tree based on
   the interrupted tree; and
6. derives a deterministic revision_transition_id and ordered root-to-leaf
   resume-anchor chain, then appends manual_plan_revision before the
   graph_resumed chain. The revision event binds the transition ID, interrupt
   ID, action, who/reason, audited reads, base/target tree IDs, exact logical
   paths, before/after digests, and the complete expected resume-anchor chain.

This is a crash-recoverable prefix protocol, not a claim that ProgressionTxn is
power-loss atomic. Target object publication and event appends may leave any
legal durable prefix:

| Durable prefix | Recovery |
|---|---|
| Target objects only | Objects are unreferenced and are not evidence; retry derives the same target from the recorded view. |
| manual_plan_revision only | The leaf tree transition is authoritative; append the full resume chain. |
| Revision plus a proper prefix of graph_resumed | Verify the prefix and append only the missing ordered suffix. |
| Revision plus the complete chain | Continue driving from review without appending duplicates. |

revision_transition_id is the canonical digest of the pinned invocation,
interrupt, action, identity/reason, audited reads, base/target trees, exact
paths and digests, and ordered resume anchors. Every graph_resumed event in
this protocol carries that ID plus its zero-based ordinal and chain length.
Status reports an open transition as revision_resume_recovery_pending. Before
drive or resume performs planning or materialization repair, runtime scans the
global ledger for that transition. It verifies that all present resume events
form the exact expected prefix, including anchor and parent-anchor linkage,
then appends only the absent suffix. A gap, reordering, duplicate ordinal,
different transition ID, or mismatched payload is
manual_plan_revision_prefix_conflict and fails closed.

The global scan, committed-field revalidation, and missing-suffix staging hold
the progression lock end to end. Once the manual event exists, recovery
reconstructs its transition ID only from committed event fields and never
rereads the mutable revision view.

Once manual_plan_revision is durable, later edits to the transport view are
ignored; recovery uses the committed target tree and event. Retrying the same
command derives the same transition ID and performs the same prefix repair.
A non-identical retry is an integrity conflict. A user who does not intend to
change the plan must choose accept_risk or stop rather than claiming
fix_and_proceed.

manual_plan_revision is a version-5 leaf-projection transition. Its
invocation_id is exactly the interrupt-producing Fuzz/Performance cycle
invocation, never the root or branch invocation. Its fold requires an
unresolved matching leaf interrupt, base_tree_id equal to that leaf
projection's current tree, and target_tree_id different from base_tree_id; it
then advances the leaf current_tree_id to target_tree_id before the leaf's
graph_resumed event is folded. The leaf fold rejects graph_resumed without a
prior unconsumed matching revision transition with the same transition ID.
There is no separate mutable epoch counter: the non-equal content-addressed
leaf-tree transition is the revision epoch.

The repair protocol appends graph_resumed for the complete root to branch to
leaf namespace chain, but no manual_plan_revision is copied to an ancestor.
Ancestor current_tree_id values remain unchanged during resume. After the
revised leaf cycle succeeds, ordinary child write-set propagation and
superstep commits carry the resulting tree through the branch to the root.
This preserves the graph engine's existing nested ownership boundary rather
than mutating ancestor projections out of band.

Version-5 human decisions also bind the source gate attempt and its committed
tree ID. A decision override is valid only for re-evaluation of that same gate
evidence epoch. Committing a manual revision advances the tree epoch, so the
source fix_and_proceed or accept_risk decision cannot authorize a later gate
attempt merely because a regenerated review or checks artifact happens to
have identical bytes. The later attempt needs its own verdict or interrupt.

Ordinary drift checking still runs for every path outside the exact revision
allowlist. The mechanism cannot edit review, checks, L1, product code, tests,
or project configuration. accept_risk and stop never ingest live edits.
Knowledge-remediation fix_and_proceed continues to use the separately
synchronized L1 boundary and does not use manual plan ingestion.

The manual revision event is lineage evidence, not a reviewer verdict. Replay
continues to bind the later committed review, mechanical, and gate attempts;
it does not infer assurance from the human edit alone.

### 8.4 Codegen preconditions

fuzz-codegen-precondition-gate and
performance-codegen-precondition-gate read:

- the exact PlanReview;
- the exact PlanCheckDocument; and
- repo:.aa/data-knowledge.yaml.

They return skip only for a valid not-applicable state. They return stop when
the review cycle did not succeed, the state is invalid, L1 is absent, required
capabilities are absent, or the plan gate is not pass.

Pass requires all of:

    review-cycle.status == succeeded
    plan_assurance_state == applicable
    plan-review-gate.verdict == pass
    capabilities_present(review, data_knowledge)
    data-knowledge file exists

The direct capabilities_present clause is intentional. An audited accept-risk
decision may resolve ordinary human-risk review, but cannot authorize codegen
with a missing declared capability.

The topology validator checks more than the precondition read set. It parses
the precondition DSL and requires the normalized hard predicates with the
profile's exact aliases, layer, cycle node, and gate ID:

- skip is based on plan_assurance_state equal to not_applicable;
- stop covers cycle status not succeeded, assurance state invalid, and missing
  L1;
- pass is a top-level conjunction containing cycle status succeeded,
  assurance state applicable, the exact plan gate verdict pass,
  capabilities_present, and L1 existence.

Required pass conjuncts cannot sit under an OR or another expression that
admits a bypass. Additional pass conjuncts may only narrow the result.
Removing, negating, changing the operands of, or disjoining around any hard
predicate makes current schema activation invalid and pinned wiring partial.

codegen-only does not regenerate plans. It always runs current applicability;
an applicable layer then runs review, mechanical, gate, and precondition,
while an inapplicable layer runs mechanical and the skip gate without a
reviewer. A pass artifact left on disk from another invocation cannot skip the
required producers.

## 9. Plan-Gate Semantics

### 9.1 Gate reads

Each plan gate reads, with profile-canonical aliases:

- its review artifact;
- its checks artifact; and
- repo:.aa/data-knowledge.yaml as data_knowledge.

Invalid JSON, missing required fields, values rejected by the applicable typed
model, wrong layer, wrong change, incomplete checks, or malformed L1 are stop.

### 9.2 Fail-closed precedence

Gate rules and guards implement this semantic order:

1. plan_assurance_state invalid -> stop.
2. Valid state not_applicable -> skip.
3. Review decision needs_fix -> needs_fix.
4. Missing declared capability -> needs_human_review, converted by
   plan_review_route to knowledge_remediation.
5. Explicit needs_human_review or changes_requested decision,
   human_review_required, configured risk, or a failed check whose action is
   require_human -> needs_human_review.
6. Explicit reject, not_ready, or a failed check whose action is block ->
   reject.
7. A valid pass review, acceptable readiness, present capabilities, and no
   blocking condition -> pass.
8. Every other combination -> stop.

Because the evaluator uses first-true rule precedence, every ordinary
human-review expression excludes decision reject. A reject plus high risk is
reject, never needs_human_review.

The successful reviewer decision is pass. The legacy approved value remains a
valid generic Review enum for compatibility but is not a successful
Fuzz/Performance plan-gate value.

needs_fix does not depend on auto_fix_allowed. It routes to the manual
remediation path because these layers have no automatic fixer.

### 9.3 Plan-check actions

For each failed applicable check:

| Action | Gate effect |
|---|---|
| warn | Preserve finding; do not block by that check |
| require_human | needs_human_review |
| block | reject |

An N/A check never triggers an action. Missing declared capabilities remain a
hard remediation precondition independently of the capability_keys action.

### 9.4 Deferred Fuzz auth policy

The tautological condition that checks only whether
policy.fuzz.required_when_endpoint_has_auth is non-null is removed from the
pass expression.

The field remains parseable so existing project policies and pinned snapshots
do not become unreadable. It is registered as an explicit deferred policy
obligation with:

    owner: fuzz-auth-applicability
    status: deferred
    reason: no_typed_endpoint_auth_fact

Policy-consumer validation enforces:

    all policy fields
      == real runtime consumers union explicit deferred fields

    real runtime consumers intersect explicit deferred fields
      == empty

A deferred field is forbidden from gate verdict expressions and is excluded
from claims of runtime enforcement. A later design may activate it only after
introducing a deterministic typed endpoint-auth fact.

## 10. Frozen Replay Semantics v2

### 10.1 Semantics identifier

New specialty reports continue to use report schema_version 2 but emit:

    counterfactual_plan_check_actions/v2

The loader and renderer continue to accept
counterfactual_plan_check_actions/v1. Version 1 retains its historical
interpretation: API/E2E are wired and Fuzz/Performance cannot be complete.
Version 2 uses pinned topology classification.

Old report files are never rewritten.

### 10.2 Two-stage binding

Replay separates definition anchoring from per-layer replayability.

Definition anchoring:

1. binds the requested terminal root invocation;
2. verifies the pinned schema bytes and graph digest;
3. verifies the root ingest_catalog_digest against
   .graph-runtime/ingest-catalogs/<digest>.json with the snapshot's versioned
   compatibility parser and canonical digest;
4. verifies every target/digest pair recorded in the root
   contract_digests map against its immutable
   .graph-runtime/contracts/<digest>.json snapshot, including the snapshot's
   canonical digest and declared target;
5. reconstructs the historical ExecutionContractCatalog exclusively from
   those verified snapshots and compiles the pinned schema with it, never with
   the current project or packaged contract catalog;
6. verifies the pinned policy snapshot, digest, and origin;
7. binds frozen root and assurance params;
8. evaluates layer selection from the pinned assurance graph; and
9. records compatibility diagnostics without yet rejecting every row because
   current profile or gate-semantics code differs.

Historical compilation receives the verified frozen ingest catalog and digest
through an explicit historical compile context. It never calls
validate_catalog_runtime, resolve_model, or current model-schema digest code.
The snapshot is definition evidence, not permission to execute a historical
ingest model; replay only needs its integrity-bound catalog identity. Current
packaged compilation continues to validate the live catalog and live model
schemas.

Every non-graph operation referenced by the pinned schema must resolve to the
recorded target and digest. A missing snapshot, digest mismatch, target
mismatch, unrecorded referenced target, or extra conflicting binding makes
definition anchoring incomplete before topology classification. It cannot be
downgraded to not_wired. Current packaged-schema validation still loads the
current catalog; only historical compilation uses the reconstructed catalog.
After compilation, the reconstructed compiled.contract_digests map must equal
the root event's recorded map exactly, and compiled.ingest_catalog_digest must
equal the verified root digest.

Per-layer processing:

1. not selected -> not_selected;
2. selected and legacy-unwired topology -> not_wired;
3. selected and partially or malformed wired topology -> incomplete;
4. selected and fully wired topology -> recover evidence;
5. fully wired plus complete frozen evidence -> complete;
6. fully wired plus missing, invalid, drifted, or incompatible evidence ->
   incomplete.

This ordering is required so a current Fuzz/Performance profile upgrade cannot
erase the historically true not_wired classification of a v4+ invocation whose
pinned definitions are intact.

### 10.3 Pinned-topology classifier

The hard-coded WIRED_REPLAY_LAYERS, _WIRED_LAYERS, and _UNWIRED_LAYERS sets are
not authorities under v2.

Current activation validation and historical classification are separate
interfaces:

- Current packaged-schema validation requires all four layer contracts to be
  fully wired and fails compilation if Fuzz or Performance activation is
  absent or malformed.
- Historical pinned-schema compilation validates schema syntax, graph
  integrity, replay-safe expressions, and gate dependencies against the
  verified pinned execution-contract catalog and ingest-catalog identity
  without applying the current four-layer activation requirement or loading
  either current catalog.
- After historical compilation, the per-layer pinned-topology classifier
  labels complete wiring, recognized legacy absence, or partial/malformed
  wiring.

compile_workflow must therefore not invoke one current-profile activation
validator unconditionally for both packaged and historical schemas. The
validation seam either takes an explicit current versus historical purpose or
is split into core compilation validation plus current activation validation.
Replay never weakens the packaged-schema release gate, and the release gate
never rejects a valid legacy graph before row classification.

The classifier follows the pinned assurance node to its branch graph and then
the branch review-cycle node to its cycle graph. A fully wired layer requires:

- exactly one branch and cycle binding;
- exactly one applicability operation for the layer;
- exactly one reviewer;
- exactly one require_review=true mechanical producer for the profile checks
  artifact;
- exactly one explicit plan-gate owner;
- no reviewer-attached plan gate;
- review before mechanical on the applicable path;
- mechanical before gate on both paths;
- the approved human-only or automatic-fixer recovery shape;
- for the human-only shape, an exact audited plan-revision ingestion contract;
- knowledge remediation returning to mechanical;
- a codegen precondition reading review/checks/L1 whose parsed DSL contains
  every hard skip/stop/pass predicate from section 8.4 without a permissive
  disjunction; and
- a replayable plan gate whose reads and aliases match the profile.

Those are the shared four-layer cycle requirements. The Fuzz and Performance
wiring contract additionally requires the cases-only parent preflight described
in section 8.1 because their real plan skills do not author empty-scope plans.
API and E2E retain the parent topology approved in rollout increment 2 and are
not retroactively required to add this guard. Removing the guard from a new
Fuzz/Performance graph is partial_assurance_wiring, not a legacy not_wired
signature.

A recognized legacy Fuzz/Performance graph has none of the new activation
markers in its cycle: no deterministic applicability producer, no reviewed
mechanical producer, and no explicit cycle gate owner. Its shallow attached
reviewer gate and branch codegen gate do not make it wired.

If any new activation marker exists but the complete topology does not
validate, classification is malformed and the row is incomplete. Partial or
duplicate wiring is never downgraded to not_wired.

Artifact presence, current source support, and current graph topology do not
participate in this classification.

### 10.4 Evidence recovery for wired layers

For a wired layer, replay binds the unique structural branch and cycle
invocations recorded below the frozen assurance invocation. It selects:

- the last committed gate attempt at or before the cycle terminal event; and
- the last earlier committed mechanical attempt whose output digest equals
  the checks digest in the gate's reads_sha256.

The replay engine reads raw bytes from the committed gate tree and verifies
every digest before parsing. Applicable state requires review, checks, and L1.
Inapplicable state requires checks only and deliberately nulls review and L1.

The recorded gate report is calibrated by evaluating the pinned gate and
pinned baseline policy against the recovered raw bytes. gate_id, verdict,
matched_rule, reason, details, and audited-read digests must agree with the
committed report. Route is not a gate-report field: replay separately derives
the recorded and recomputed routes with plan_review_route, compares them, and
calibrates the result against committed downstream activation/skip events. A
mismatch is incomplete, not a new historical verdict.

The row records the mechanical attempt's execution-contract digest. Abandoned,
failed, uncommitted, later, or digest-mismatched attempts are ignored.

### 10.5 Complete row shapes

An applicable complete row contains:

- review, checks, and L1 digests;
- mechanical execution-contract digest;
- exactly four ordered check summaries;
- assert_ideal as not_applicable;
- required and missing capability summaries; and
- exactly warn, block, and require_human scenarios in that order.

An inapplicable complete row contains:

- checks digest;
- mechanical execution-contract digest;
- null review and L1 digests;
- null capability summary;
- four layer_not_applicable checks; and
- three skip scenarios.

Complete means the replay evidence is complete. It does not mean the baseline
or every counterfactual verdict passes.

### 10.6 Counterfactual scenarios

Each scenario starts from the pinned baseline policy and changes all
plan_checks actions to exactly one of warn, block, or require_human. It invokes
the real pinned plan-gate definition with the current evaluator only when the
recorded gate-semantics digest is compatible.

For an applicable passing review with at least one failed applicable check:

| Scenario | Expected policy effect |
|---|---|
| warn | finding retained; gate may pass |
| block | reject |
| require_human | needs_human_review |

If no applicable check failed, policy_effect is no_failed_checks. If missing
capabilities determine the route, policy_effect is
shadowed_by_capability_precondition. Reviewer reject, needs_fix, or not_ready
may similarly shadow plan-check policy.

### 10.7 Profile snapshot pinning

New root invocations persist the normalized assurance-profile manifest at:

    .graph-runtime/assurance-profiles/<digest>.json

This activation bumps graph event_schema_version to 5. A version-5 root
invocation requires the profile snapshot and records that requirement as part
of its definition binding; migration never fabricates the file. Child
invocations inherit both the digest and the snapshot-required epoch.

The immutable bytes are written and verified before an invocation event may
refer to the digest. A repeated write with identical bytes is idempotent; a
different byte sequence at the same digest is an integrity error.

Replay hashes and parses this snapshot. It does not silently replace it with
the current profile registry. Current executable gate/check semantics must
still match the recorded gate-semantics digest before a wired row can be
complete; the manifest snapshot is metadata and audit evidence, not archived
Python code.

For a historical version-4 invocation without a profile snapshot:

- a legacy-unwired row can still be classified from the pinned graph;
- an API/E2E wired row is complete only if the recorded profile digest is
  provably compatible with the available manifest;
- a pinned graph containing complete Fuzz/Performance activation markers
  requires a snapshot even if its event incorrectly claims the older epoch,
  and is incomplete when that snapshot is absent;
- otherwise the wired row is incomplete.

Pre-v4 invocations and invocations whose schema, policy, params, or root
binding cannot be verified are not eligible for a not_wired upgrade. They
remain report-level incomplete or continue through the legacy report renderer.

## 11. Error Model

The replay error vocabulary adds stable reasons where the existing reason is
not specific enough:

- partial_assurance_wiring;
- pinned_ingest_catalog_missing;
- pinned_ingest_catalog_invalid;
- pinned_ingest_catalog_digest_mismatch;
- pinned_contract_snapshot_missing;
- pinned_contract_digest_mismatch;
- pinned_contract_target_mismatch;
- profile_snapshot_missing;
- profile_snapshot_digest_mismatch; and
- profile_definition_incompatible.

The runtime adds manual_plan_revision_noop and
manual_plan_revision_prefix_conflict as stable resume errors.

Existing reasons remain in use for:

- root_invocation_unbound;
- pinned_schema_missing;
- pinned_schema_digest_mismatch;
- policy_snapshot_missing;
- policy_digest_mismatch;
- policy_origin_mismatch;
- gate_semantics_mismatch;
- ambiguous_assurance_invocation;
- ambiguous_graph_wiring;
- selection_evidence_mismatch;
- no_successful_gate_evidence;
- mechanical_producer_unbound;
- gate_evidence_unbound;
- gate_evidence_drift;
- invalid_checks;
- missing_evidence;
- invalid_evidence;
- baseline_gate_mismatch; and
- baseline_route_mismatch.

The runtime and replay outcomes are:

| Condition | Runtime | Replay |
|---|---|---|
| Malformed case applicability | invalid_output; stop layer | wired row incomplete |
| Missing/malformed applicable plan, review, or L1 | mechanical failure; no committed checks | incomplete |
| Invalid checks state | gate stop | incomplete/invalid_checks |
| No committed gate | no codegen | incomplete/no_successful_gate_evidence |
| Producer/read digest mismatch | no codegen | incomplete/mechanical_producer_unbound |
| Manual fix contains undeclared, unsafe, or ambiguous revision | resume rejected; interrupt remains unresolved | no later complete evidence |
| Manual fix is byte-identical to the interrupted plan tree | resume rejected/manual_plan_revision_noop | no later complete evidence |
| Manual revision resume events are gapped, reordered, duplicated, or conflicting | resume rejected/manual_plan_revision_prefix_conflict | no later complete evidence |
| Partial new topology | schema validation failure | incomplete/partial_assurance_wiring |
| Valid v4+ binding with complete legacy topology absence | historical behavior | not_wired |
| Pinned ingest-catalog snapshot is missing, invalid, or mismatched | no historical mutation | definition-level incomplete |
| Pinned contract snapshot is missing, mismatched, or bound to the wrong target | no historical mutation | definition-level incomplete |
| Pinned definition integrity failure | no historical mutation | incomplete |

No failure path writes a pass-shaped substitute.

## 12. Verification Strategy

### 12.1 Model and registry tests

Tests prove:

- PlanReview and PlanReviewAuthoring accept canonical Fuzz and Performance
  reviews;
- all required cross-skill fields and finding IDs are enforced;
- required_capabilities is non-empty for all four plan-review types;
- wrong review_type and change identity fail the gate-state seam;
- exact Fuzz/Performance registry entries win before review/*.json;
- both profiles use PlanReview; and
- the PlanReview docstring and schema notes describe all four types.

### 12.2 Canonical authoring round trips

Two fixtures exercise the real authoring contract rather than synthetic
fragments.

The Fuzz fixture includes:

- an automated Fuzz case;
- related API/schema/auth context;
- both exact Fuzz plans;
- a complete Fuzz PlanReview;
- typed L1 factory, adapter, and capability leaves; and
- a canonical Factory Mapping table.

The Performance fixture includes:

- an automated Performance case;
- absolute thresholds, concurrency/load shape, duration, and scenarios;
- both exact Performance plans;
- a complete Performance PlanReview;
- typed L1 factory, adapter, and capability leaves; and
- a canonical Factory Mapping table.

Each fixture passes:

    artifact authoring validation
    -> run_plan_checks
    -> plan_assurance_state
    -> real plan gate

Expected mechanical results are zero findings for l1_path, shared_factory, and
capability_keys, plus assert_ideal not_applicable/check_not_in_profile.

### 12.3 Contract mutations

Mutation tests cover:

- automation.required set to the string "true";
- malformed case bucket, type, automation, or case ID;
- missing review fields or finding IDs;
- empty, malformed, or unknown capability keys;
- wrong review_type or change_id;
- Fuzz/Performance auto_fix_allowed true or a non-empty auto_fix_plan;
- stale layer_applicable or nonexistent fixer next_action instructions in the
  real reviewer skill contract;
- reviewer attempts to write checks;
- missing or malformed Factory Mapping;
- incorrect factory ownership;
- missing L1 leaves;
- assert_ideal under block policy remaining N/A; and
- missing plan artifacts failing closed.

### 12.4 Topology mutations

For both Fuzz and Performance, tests delete or duplicate each critical node,
edge, route, read, and ownership declaration. They prove:

- the parent applicability preflight runs before plan and its false route
  bypasses plan;
- reviewer-attached gates are rejected;
- explicit gate ownership is unique;
- applicable review precedes mechanical;
- both paths reach mechanical before gate;
- needs_fix reaches the human-only remediation loop;
- human fix_and_proceed returns to review;
- the human-only interrupt declares only exact plan-output revision paths;
- deleting or broadening the revision allowlist fails validation;
- knowledge fix_and_proceed returns to mechanical;
- codegen-precheck reads review/checks/L1;
- an empty codegen-precheck read set is rejected; and
- removing or altering each codegen hard predicate, or wrapping required pass
  predicates in a bypassing OR, is rejected;
- partial wiring is incomplete rather than not_wired.

The validator also proves that no profile field generates or selects a graph
edge. Separate tests prove that current packaged validation rejects legacy
Fuzz/Performance absence while historical compilation accepts the same valid
legacy schema for pinned-topology classification.

### 12.5 Gate truth tables

Both layer gates have table-driven coverage for:

- invalid -> stop;
- not applicable -> skip;
- needs_fix -> manual needs_fix route;
- missing capability -> knowledge remediation for all three policy actions;
- explicit reject plus high risk -> reject;
- not_ready -> reject;
- failed check plus warn -> non-blocking;
- failed check plus block -> reject;
- failed check plus require_human -> human review;
- N/A assert_ideal never affecting a verdict;
- force-continue behavior matching existing audited rules; and
- unmatched combinations -> stop.

### 12.6 Runtime and recovery tests

Tests use the real GraphRuntime, TaskWorkspace freeze, and event ledger rather
than a workspace double that bypasses write-set enforcement.

They cover:

- applicable full runs reaching codegen only after committed review,
  mechanical, and gate attempts;
- inapplicable runs skipping reviewer and codegen while committing complete
  N/A evidence, without invoking the plan skill;
- applicable codegen-only re-running review, mechanical, gate, and
  precondition despite pre-existing pass files;
- human fix resume returning through review;
- human plan edits being committed through the exact revision allowlist before
  review, while undeclared edits and symlink escapes fail closed;
- fault injection after target publication, after manual_plan_revision, and
  after every root-to-leaf graph_resumed append, proving each legal prefix is
  recovered by appending only its missing suffix;
- gapped, reordered, duplicated, or payload-conflicting resume prefixes
  failing closed without ordinary graph planning;
- a real root to branch to Fuzz/Performance cycle interruption proving the
  writable revision view survives leaf TaskWorkspace cleanup, only the leaf
  projection advances at resume, and normal child commits later propagate the
  revised tree to its ancestors;
- byte-identical fix_and_proceed being rejected with the interrupt still
  pending, and an exact retry after a committed revision adding no duplicate
  revision or resume events;
- codegen requiring review, mechanical, and gate attempts committed after the
  manual revision tree even if a later artifact happens to have identical
  bytes;
- accept_risk and stop not importing live plan changes;
- knowledge remediation resume returning through mechanical;
- accepted ordinary risk remaining bound to unchanged audited bytes;
- a source human decision not carrying across a manual-revision tree even when
  later review/check bytes are identical;
- accepted risk not bypassing missing declared capabilities;
- import-checkpoint and resume using the pinned invocation;
- failed and abandoned attempts not becoming producers; and
- Fuzz/Performance outcomes preserving generation-join behavior for other
  active layers.

### 12.7 Replay matrix tests

The minimum matrix is:

| Pinned state | Expected row |
|---|---|
| New graph, selected Fuzz, complete evidence | complete |
| New graph, selected Performance, complete evidence | complete |
| New graph, valid inapplicable skip | complete |
| New graph, selected, missing gate/mechanical evidence | incomplete |
| New graph, partially wired | incomplete |
| Definition-valid v4+ old graph, selected Fuzz/Performance | not_wired |
| Layer not selected by frozen params | not_selected |
| Definition-valid v4+ old graph with stray Fuzz/Performance files | not_wired |
| Pre-v4 or definition binding cannot be verified | report-level incomplete or legacy rendering |
| New graph with current-disk drift | frozen tree or incomplete on digest mismatch |
| Current schema/profile/ingest/contract changes after run | pinned topology classification unchanged |
| Historical semantics v1 report | still loads and renders |
| New collector output | semantics v2 |

Additional replay mutations cover missing snapshots, digest mismatch,
ambiguous structural invocation, baseline route mismatch, abandoned later
attempts, and exact scenario ordering. Event-version tests prove that:

- a version-5 root cannot bind without its profile snapshot;
- deleting a new snapshot cannot activate the version-4 fallback;
- a fully activated Fuzz/Performance graph claiming version 4 but lacking the
  snapshot is incomplete;
- a definition-valid version-4 legacy graph remains classifiable as
  not_wired; and
- pre-v4 events are not upgraded into replay-v2 evidence.

Definition-pinning tests replace the current ingest catalog, ingest model
schemas, and project/packaged execution contracts after a valid run and prove
historical compilation and layer classification are unchanged. Deleting or
corrupting the pinned ingest snapshot, or making its digest disagree with the
root event, fails definition anchoring. Deleting a referenced pinned contract
snapshot, changing its bytes, changing its declared target, or adding a graph
reference absent from root contract_digests likewise fails before
not_wired/complete classification.

### 12.8 Regression and CI

API and E2E topology, verdict, replay, codegen-only, and resume tests remain
green. The complete CI gate is:

- uv run ruff check .
- uv run ruff format --check .
- uv run pyright
- uv run lint-imports
- uv run pytest
- bash scripts/packaging_smoke_test.sh

## 13. Rollout and Compatibility

The activation must land atomically enough that no packaged graph can select a
strong gate before its models, registry entries, skills, contracts, and
mechanical node exist.

The safe dependency order is:

1. strong review models, exact artifact registration, and authoring contracts;
2. canonical Fuzz/Performance plan fixtures;
3. profile and generic topology validation support;
4. execution contracts, cycle topology, plan gates, and codegen preconditions;
5. runtime and resume coverage;
6. profile snapshot pinning and two-stage replay binding;
7. semantics-v2 models, collector, renderer, and replay mutations.

This order is an implementation dependency, not a request for profiles to
generate graph YAML.

Historical compatibility rules are:

- generic Review JSON on paths that remain generic stays readable; old
  canonical Fuzz/Performance review files are not promised to pass current
  aa validate after exact PlanReview registration;
- new applicable Fuzz/Performance runtime paths require PlanReview;
- semantics-v1 reports remain loadable;
- definition-valid v4+ old pinned Fuzz/Performance graphs remain not_wired;
- new fully wired graphs with missing evidence are incomplete;
- current source files never rewrite historical classification; and
- no migration fabricates missing historical ingest/contract/profile
  snapshots, review, checks, L1, attempt, or gate evidence.

## 14. Acceptance Criteria

The increment is complete only when all of the following are true:

1. Fuzz and Performance profiles, registry entries, skills, handlers, gates,
   and codegen consumers agree on strong PlanReview contracts.
2. Selected applicable runs commit review, checks, and gate evidence before
   codegen.
3. Selected inapplicable runs commit complete N/A checks and skip without a
   reviewer-authored applicability decision.
4. l1_path, shared_factory, and capability_keys are active; assert_ideal is
   explicitly N/A.
5. No reviewer or codegen skill can author the mechanical evidence artifact.
6. Every codegen-only and resumed path re-establishes current evidence.
7. Human fix_and_proceed preserves only explicitly declared plan revisions in
   an audited committed tree, crash-recovers every legal prefix of the full
   interrupt namespace chain, rejects no-op or conflicting revisions, and is
   idempotent after completion; ordinary workspace drift remains forbidden.
8. Missing capabilities cannot be bypassed to reach codegen by plan-check warn
   or accepted risk.
9. Selected Fuzz/Performance rows from a fully activated version-5 pinned
   topology are complete or incomplete, never not_wired.
10. Definition-valid v4+ pinned Fuzz/Performance legacy graphs remain
   not_wired; pre-v4 or definition-damaged invocations are not upgraded.
11. Partial wiring is incomplete and never mistaken for legacy absence.
12. Replay reconstructs historical compilation from pinned schema, pinned
    ingest-catalog identity, and pinned execution-contract snapshots, then
    uses pinned policy, params, profile metadata, committed attempts, raw
    gate-read bytes, and contract digests.
13. The tautological Fuzz auth-policy expression is gone and the field is
    explicitly deferred rather than falsely reported as enforced.
14. API/E2E behavior and all CI gates remain green.
15. Traceability projection, sufficiency, and recovery remain out of scope and
    unchanged.
