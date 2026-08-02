# Four-Layer Assurance Round-Trip and Runtime Continuity Design

**Date:** 2026-07-31
**Status:** Draft for written review
**Rollout increment:** 5

## 1. Context

The preceding assurance increments introduced a code-owned
`LayerAssuranceProfile`, deterministic applicability, four shared mechanical
checks, explicit plan gates, Fuzz/Performance runtime wiring, pinned policy
replay, and layered Trace reporting. Those increments deliberately deferred
the exhaustive round-trip, mutation, codegen-only, and resume matrix.

The focused suites are green, but the current tests do not prove the complete
production boundary. Several green tests stop at JSON serialization or
`plan_superstep`; others import already-completed assurance tasks or score
pre-seeded test files. This leaves a false-confidence gap: a release can pass
the tests without running a fresh reviewer, mechanical producer, plan gate,
codegen precondition, or codegen task.

This increment closes that gap. It adds a layered contract harness and a real
packaged-`GraphRuntime` matrix, and it includes the production corrections
that those tests already show are necessary. It does not introduce a second
workflow engine. The graph schema remains the runtime control plane; the new
code validates and observes that control plane.

### 1.1 Existing round-trip tests stop before the real consumers

`test_layer_assurance_round_trip.py` constructs placeholder plans and an empty
capability set, runs `run_plan_checks`, and round-trips the resulting
`PlanCheckDocument`. It proves model invariants, but not a canonical
cross-skill round trip.

The API/E2E contract fixtures exercise mechanical checks but do not cross the
real plan gate, codegen precondition, or frozen workspace. The
Fuzz/Performance fixtures parse strong review artifacts and derive assurance
state, but do not prove the codegen task can read every required input under
the execution contract. Some required review summaries and `.aa/config.yaml`
inputs are absent from those fixtures.

### 1.2 Skill instructions and execution contracts are not closed

The execution contract is the actual read-isolation boundary. A skill may say
an input is required while the corresponding contract makes that input
invisible. Current examples include:

- API/E2E plan skills referring to workflow state, `.qa.yaml`, proposal, case,
  knowledge, and existing test inputs while their contracts expose only a
  subset;
- API/E2E reviewers and codegen skills requiring cases and M3/M4 summaries
  that their contracts do not consistently expose;
- API/E2E skills retaining prompt-owned workflow-state update instructions
  even though the graph ledger owns phase state;
- Fuzz/Performance canonical examples referring to
  `tests/factories/account.py` while codegen contracts expose
  `tests/testdata/**`; and
- malformed Fuzz/Performance `## Inputs` headings that token-presence tests do
  not detect.

A canonical fixture can therefore pass a shallow parser while remaining
unexecutable in a real `TaskWorkspace`.

### 1.3 Current packaged topology validation is fail-open

The current activation validator discovers expected nodes and checks part of
their connectivity, but it does not completely constrain branch predicates,
route cases, interrupt bindings, checkpoints, or forbidden bypass edges.
Mutations such as a direct review-to-codegen edge, skip-to-codegen edge,
weakened codegen predicate, wrong precheck selector, wrong gate route, or
unaudited interrupt can still compile.

These are not test-only omissions. They are production compiler omissions:
the packaged schema can be safety-weakened while
`compile_packaged_workflow(...)` accepts it.

### 1.4 Current and historical topology have different compatibility rules

The current packaged release should satisfy the exact activation contract.
A pinned historical graph, however, must not be reclassified merely because
it uses an older node name or a semantically equivalent expression shape.

Reusing one strict validator for both concerns would trade one bug for
another: it would catch current mutations but retroactively turn safe pinned
runs into `partial`. The two authorities must be separate.

### 1.5 Codegen-only parameters and write policy disagree

The workflow parameter is a list and existing graph/CLI behavior supports one
or more selected layers, defaulting to API and E2E. The eval write scanner,
however, requires exactly one layer and defaults missing input to API. The
executor resolves missing input to `[api, e2e]` for runtime execution while
passing the unresolved value to write-policy selection.

The same attempt can therefore execute API and E2E while authorizing only API
writes. In addition, codegen contracts authorize the shared
`tests/testdata/**` factory area, but the eval allowlists omit it. Conversely,
the eval allowlists permit `qa/changes/**`, which is broader than the current
change owned by the invocation.

### 1.6 Codegen eval tiers can pass without codegen

The four workflow-codegen suites seed completed assurance and codegen tasks.
Their scorers check test syntax and summary-file presence, not whether the
current invocation produced those files. A pre-seeded stub and summary can
therefore pass without a new agent call, codegen attempt, or fresh gate
evidence.

### 1.7 Resume tests do not use the packaged four-layer graph

Existing Fuzz/Performance integration tests call the planner directly.
Existing crash and manual-revision tests use synthetic graphs. Those tests are
valuable for generic runtime behavior, but they do not prove the packaged
API/E2E/Fuzz/Performance topology preserves freshness across restart,
automatic fixing, manual revision, knowledge remediation, or pinned resume.

## 2. Goals

1. Prove a canonical artifact bundle for each layer crosses every production
   boundary from skill output through codegen precondition.
2. Make all required skill inputs visible under the exact execution contract,
   without broad `repo:**` or cross-change access.
3. Keep workflow state under graph/ledger ownership and remove conflicting
   prompt-owned state instructions from assurance skills.
4. Make the current packaged compiler reject every safety-relevant topology
   bypass with a stable, localized diagnostic.
5. Preserve safe historical pinned graphs through a separate semantic safety
   classifier.
6. Preserve multi-layer `codegen-only`, including the existing `[api, e2e]`
   default, and derive eval write policy from the resolved selected set.
7. Prove applicable and inapplicable codegen-only behavior for all four
   layers with real packaged `GraphRuntime` execution.
8. Prove stale pass-shaped artifacts cannot replace current committed
   reviewer, mechanical, gate, precheck, or codegen attempts.
9. Prove multi-layer generation joins wait for every active branch and never
   activate an unselected branch.
10. Prove ordinary restart, automatic fix, manual revision, knowledge
    remediation, accept-risk, named-crash-cut recovery, and pinned resume against
    the packaged graph.
11. Make workflow-codegen benchmark suites require a current physical codegen
    chain and a structured, behavior-evidenced selected-layer test write.
12. Keep the test suite deterministic and independent of an external OpenCode
    service.
13. Close declared-read isolation through persona and adapter-directory
    dispatch, not only workspace file filtering.
14. Make generated-file and change-location authority replayable from strict
    evidence rather than Markdown or a mutable final SUT.
15. Give commit-safety-unbound legacy roots one audited terminal/replacement
    exit without weakening ordinary active-invocation or restart policy.

## 3. Non-Goals

- Replacing `workflow-schema.yaml` with Python-generated topology.
- Moving route decisions from the graph planner into the contract harness.
- Making LayerAssuranceProfile a workflow generator.
- Replaying historical agents, human deliberation, or wall-clock behavior.
- Normalizing the existing full-mode parent-preflight difference between
  API/E2E and Fuzz/Performance.
- Adding new mechanical check IDs or changing the established applicability
  matrix.
- Adding per-layer policy overrides.
- Changing assurance plan-gate/codegen-precondition verdict precedence,
  force-continue/human-review routing, or their existing pass/skip/fix/reject
  destinations. The separately documented healing authority fail-closed route
  in sections 5.3 and 19.8 is an intentional safety change, not a plan-gate
  change.
- Allowing broad `repo:**`, `tests/**`, or `qa/changes/**` write scopes to make
  fixtures pass.
- Adding a general Markdown policy engine or mutation-testing English prose.
- Adding create-only filesystem semantics for shared factories; existing
  create-if-missing instructions and the `repo:test-infra` exclusive lock
  remain authoritative.
- Requiring an external agent server in CI.

## 4. Design Decisions

### D1. One graph control plane, one observing harness

The packaged graph remains the only runtime control plane. The contract
harness is a test seam that builds canonical inputs, drives existing artifact
and gate APIs, and records which boundary accepted or rejected the bundle. It
does not return a next node or emulate graph routing.

The complete round trip is:

```text
canonical skill-output fixture
  -> authoring model
  -> runtime artifact model and frozen write set
  -> deterministic layer applicability
  -> mechanical checks
  -> PlanCheckDocument wire round-trip
  -> real plan gate
  -> real codegen precondition
  -> codegen TaskWorkspace contract visibility
```

### D2. Canonical fixture closure is structural

Each skill must expose a real `## Inputs` and `## Outputs` section. A small
test-only Markdown section reader may parse backticked logical paths and
required/optional groups from those sections. It does not interpret prose or
run regex mutations against English sentences.

For each assurance role, every declared required input must be covered by the
execution contract's `reads`, every declared output by `writes`, and every
actual canonical output by the artifact ingest model where one exists. The
fixture must then run inside the real read-isolated `TaskWorkspace`; set
comparison alone is not sufficient.

### D3. Current activation conformance is strict and structured

Add a structured current-release diagnostic below the compatibility wrapper:

```python
AssuranceConformanceCode = Literal[
    "missing_unique_node",
    "missing_required_edge",
    "forbidden_bypass_edge",
    "route_case_mismatch",
    "selection_predicate_mismatch",
    "run_mode_predicate_mismatch",
    "gate_read_mismatch",
    "gate_rule_mismatch",
    "codegen_precondition_mismatch",
    "interrupt_binding_mismatch",
    "interrupt_checkpoint_mismatch",
    "interrupt_action_mismatch",
    "manual_revision_allowlist_mismatch",
    "remediation_return_mismatch",
    "generation_join_mismatch",
]


@dataclass(frozen=True, slots=True)
class AssuranceConformanceIssue:
    code: AssuranceConformanceCode
    layer: LayerName | None
    owner: str
    locator: str
    detail: str


def find_current_assurance_conformance_issues(
    schema: WorkflowSchemaV2,
) -> tuple[AssuranceConformanceIssue, ...]: ...
```

Compiler transport uses a small common envelope:

```python
CompileDiagnosticCategory = Literal[
    "assurance_conformance",
    "healing_conformance",
    "workflow_validation",
    "historical_ingest_identity",
    "historical_contract_identity",
]


@dataclass(frozen=True, slots=True)
class CompileDiagnostic:
    category: CompileDiagnosticCategory
    code: str
    layer: LayerName | None
    owner: str
    locator: str
    detail: str
```

Current assurance issues are losslessly wrapped (`code` stays the literal
shown above). Historical ingest/contract identity failures receive dedicated
categories/codes; generic existing validation messages may use
`workflow_validation` while they are incrementally made more specific.

Current healing safety uses a parallel
`find_current_healing_conformance_issues(...)` seam and stable codes for
authority/approval dominance, audited interrupt binding, precommit validator,
record/join/aggregate topology, and safety-gate evidence. Its findings use the
`healing_conformance` category. Keeping the functions separate prevents the
assurance-layer role manifest from becoming a general workflow generator, but
`compile_packaged_workflow(...)` invokes both current-release validators.

`validate_current_assurance_activation(...)` remains as a compatibility
wrapper that renders deterministic strings. `compile_packaged_workflow(...)`
continues to call only the current-release validator and fails before runtime
when any issue exists.

`CompileError` gains a typed, immutable
`tuple[CompileDiagnostic, ...]` for callers that need to classify the failure.
Human-readable text remains available through
`str(exc)`, but `definition_pinning._pinned_reason_for_compile_error(...)` and
tests must switch on diagnostic codes/categories rather than English
substrings such as `"contract"` or `"ingest_catalog_digest"`.

Current predicate comparison parses/normalizes the AST and then evaluates the
closed release-domain truth table defined in section 8.2. Parentheses, operand
order in commutative boolean expressions, and list literal order do not create
false drift, but weakening, broadening, or adding an unknown route condition
does. The validator is exact about release behavior, not exact about source
text.

### D4. Historical pinned safety is a separate semantic classifier

The version-dispatched historical classifier remains responsible for
`wired | legacy_unwired | partial`. The v6 implementation must not call the
current exact-shape validator; the frozen v5 implementation is retained only
for compatibility with invocations that never recorded a semantics digest.

Historical classification discovers semantic roles from graph references,
operations, gate ownership, outputs, and dependencies. Node names are
evidence only when needed to disambiguate otherwise identical roles.

A v6-bound pinned layer is:

- `legacy_unwired` when it has no activation markers for the assurance chain;
- `wired` when review/mechanical/gate/precheck/codegen roles are unambiguous,
  codegen is dominated by the precondition, and no safety bypass exists; or
- `partial` when activation markers exist but the safety obligations are
  missing, ambiguous, malformed, or bypassable.

Equivalent old predicate shapes remain acceptable. A direct path into
codegen, a skip route into codegen, an unaudited remediation return, or a gate
that cannot bind committed evidence remains `partial` regardless of naming.

The classifier is not an additional historical compile gate:
`compile_historical_workflow(...)` may load a structurally replayable graph and
report `partial`. It is nevertheless a replay authorization input. A pending
assurance path may not reach codegen from `partial` or unbound topology
evidence; definition identity compatibility alone is insufficient.

### D5. Codegen-only remains multi-layer

`codegen-only` accepts a non-empty unique subset of
`api | e2e | fuzz | performance`. Omitted selection preserves the existing
`[api, e2e]` default. The resolved tuple uses canonical layer order so
write-policy bytes and test expectations are deterministic.

The eval CLI/runner, executor, fixture importer, write scanner, and scorer must
consume the same resolved selection. The packaged workflow parameter validator
continues to validate the resulting list; the compiler does not normalize
invocation values. No component may apply a different implicit default or
reparse/stringify the raw value independently.

### D6. Freshness is attempt-bound, not file-presence-bound

A current assurance chain is proven by committed physical attempts in the
current root invocation subtree. A file already on disk, a `task_imported`
event, or a task from an unrelated invocation is insufficient.

For applicable layers the current chain contains committed attempts for:

1. applicability;
2. reviewer;
3. mechanical producer;
4. plan gate;
5. codegen precondition; and
6. codegen.

The gate's frozen `reads_sha256` must bind the mechanical and review bytes to
the producer attempts, and the codegen precondition must bind the successful
child cycle and current gate evidence. Codegen output bytes must be in the
committed codegen attempt's authority: both the human summary and the strict
generated-file manifest from D16 are in `outputs_sha256`, and every manifest
entry is reconciled against the frozen input snapshot and `write_set_id`.
Markdown prose is never parsed as file authority.

For inapplicable layers, applicability, mechanical N/A evidence, the skipped
plan gate, and skipped codegen precondition are current. Reviewer and codegen
attempts must be absent.

### D7. Runtime depth is real; agent behavior is deterministic

Integration tests load the packaged schema and execution contracts, create a
real `GraphRuntime`, use the real journal/tree store/freeze/apply pipeline, and
drive the real nested graph. A deterministic adapter supplies valid or
intentionally invalid agent outputs by target and attempt number.

This proves runtime behavior without adding an OpenCode dependency to CI.

### D8. Every mutation has one expected failure owner

Mutation tests operate on typed artifacts, decoded schema models, execution
contract models, or filesystem paths. Each case declares the expected
boundary, diagnostic code, and locator. It also asserts that unrelated checks
or branches retain their canonical outcome.

A test does not pass merely because some downstream assertion failed. If a
mutation intended for the codegen precondition is first rejected by an
unrelated plan parser, the mutation test fails as misclassified.

### D9. Write authorization is selected-layer and current-change scoped

The eval allowlist is derived from:

- the resolved current change location;
- the selected layer test roots;
- shared `tests/testdata/**`;
- the existing graph-runtime lock/publication metadata paths.

It does not include sibling layer roots, another change, all changes, or all
tests. The graph execution contract remains the runtime write authority; the
eval scanner is an independent post-execution check using the same scope.

### D10. Historical topology semantics are version-pinned

Changing role discovery or safety classification must not silently reinterpret
an already-recorded invocation. New roots use event schema v6 and record a
`topology_safety_semantics_digest` alongside the existing gate-semantics and
assurance-profile digests. Child invocations inherit the exact value.

Add a small `topology_semantics.py` manifest, parallel to gate semantics. Its
canonical digest covers the v1 classifier entrypoint, role-discovery/CFG/
dominance/truth-table helpers, status constants, and runtime versions. The
manifest exposes semantic ID `historical_topology_safety/v1` and
`topology_safety_semantics_digest()`; tests prove each declared dependency is
consumed and an implementation mutation changes the aggregate digest.

For v6 roots, `stage_pinned_definitions()` also persists canonical gate-,
topology-, and runtime-commit-safety semantics manifest bytes in write-once
digest-addressed files under the root definition bundle. The root/child
binding records their object IDs as well as digests, and live/replay
verification reloads bytes and recomputes each digest. Gate semantics gains
the same canonical descriptor/staging treatment; a digest with no recoverable
bytes is not a complete v6 binding. V1-v5 roots are not backfilled. A v4/v5
compatibility audit stages and binds the v6 topology-semantics manifest used by
its receipt without altering the legacy root binding; an old pending
assurance-codegen/healing path remains blocked by D14 because commit-safety
semantics are unbound even when topology is safe.

The executable keeps an explicit display-classification dispatch table:

- v1-v3 retain their existing report/fold compatibility path and remain
  ineligible for live definition-dependent resume;
- v4 retains its existing unbound legacy display classification and existing
  live-compatibility checks;
- v5, which has a profile snapshot but no topology-semantics binding, retains
  the frozen legacy-v5 classifier byte-for-byte—including its known false
  negatives—and is never upgraded or downgraded merely because the current
  classifier improved; and
- v6 with `historical_topology_safety/v1` uses the semantic role/CFG classifier
  specified in section 9.

There is no root-event rewrite/backfill. An unknown or mismatched digest is
incompatible, not an invitation to use the newest classifier. The new digest
participates in root/child definition-binding equality, pinned-definition
requests, snapshots, and live semantic compatibility checks. This changes
replay metadata, not the public `wired | legacy_unwired | partial` wire
vocabulary.

`PinnedLayerTopology` also reports `semantics_id` and `semantics_bound`. V4 and
v5 results use `legacy_v4_unbound`/`false` and
`legacy_v5_unbound`/`false`; downstream evidence must not present either status
as v6 safety proof. This surfaces the compatibility limitation without
pretending an unpinned old invocation had stronger semantics or silently
changing its historical status.

Before a live resume of any topology-unbound but otherwise replayable root
(currently v4 or v5), runtime uses **v6 semantic role discovery plus the
pinned projected reachable set** to decide whether the remaining path may
contain an assurance codegen role. This trigger never trusts the frozen v4/v5
display status, legacy node names, or a negative result from the legacy
classifier. An assurance marker, an agent contract authorized to write a
recognized private assurance-test root, or an ambiguous role/reachability
match triggers the audit; ambiguity blocks rather than exempts it. Only a
uniquely proven absence of every reachable assurance-codegen role may resume
without a receipt.

On the first such audited resume, runtime performs the v6 classifier against
the exact pinned schema/profile and appends an immutable
`topology_safety_compatibility_recorded` receipt. The receipt binds root ID,
root event version, graph/contract/catalog/profile digests, v6 semantics
digest, discovered role manifest digest, selected/reachable layers, and
per-layer result. Only all-safe results authorize planning; `partial`,
unknown, ambiguous, or audit failure blocks resume. Subsequent resumes reuse
the receipt and require byte-identical bindings. The append-only receipt does
not rewrite or upgrade the root event version.

Here “authorize planning” means topology compatibility only. Before dispatch,
D14 separately requires commit-safety binding for any remaining validator/
effect-bearing assurance work; a legacy root cannot use this receipt as that
binding.

V5 can audit from its pinned profile snapshot. V4 may audit only when its
existing live-compatibility boundary uniquely reconstructs the required
profile from runtime bytes whose digest equals the root's recorded profile
digest; a missing snapshot/binding, including a Fuzz/Performance profile that
cannot be reconstructed from the v4 record, blocks definition-dependent
resume. Non-assurance v4/v5 roots proven to have no reachable assurance
codegen and already-terminal/report-only reads do not need a receipt. The
historical display status remains frozen for its original epoch; the receipt
is a separate execution-safety decision, not a silent reclassification and
not a manual bypass.

Because the event version is global, compatibility coverage is global too.
Golden graph-start/projection fixtures for v1-v5 must still parse/fold with
their existing replayability rules. V6 writer tests cover both the assurance
root/children and representative non-assurance roots with children (`retro`,
`issue-review`, and `improvement-review`): every child inherits the same
topology digest even when that graph has no assurance layer to classify. V4/
v5 pinned resume tests remain green; the new field is never required from an
old event.

### D11. Assurance agents use declared-read isolation

After skill/contract parity is closed, every API/E2E/Fuzz/Performance plan,
reviewer, supported plan-fixer, codegen, and API/E2E codegen-fixer contract
uses `read_isolation: declared_only`.

The agent sees only materialized contract reads plus its authorized output
workspace. Missing inputs therefore fail at the contract boundary instead of
being recovered through ambient repository visibility.

Isolation is closed through the adapter boundary, not inferred from contract
text alone. A table-driven guard pins the exact sixteen target-to-persona
bindings: four planners and the two plan fixers use `aa-doc-author`, four
reviewers use `aa-reviewer`, and four codegen plus two codegen-fixer targets
use `aa-test-author`. The three packaged persona documents must retain a
deny-by-default edit floor, their role-specific allowlist, and
`permission.external_directory: deny`. A wrong/default persona, a broadened
edit glob, or removal of the external-directory denial fails packaging.

The OpenCode adapter receives the physical attempt workspace directory from
`AgentHandler`; the SUT root is never an adapter fallback. A mock-transport
contract test observes session creation, prompt dispatch, and every status
poll and requires the same exact attempt directory on each request. Missing,
rewritten, or SUT-root directory parameters fail before output ingest. This
proves packaged configuration and request binding without claiming that CI,
which does not launch a real OpenCode service, verifies the third-party
server's own sandbox implementation.

For `declared_only`, `AgentHandler` must not call the current unconditional
host-link policy: no task-visible symlink or bind to host `.venv`,
`node_modules`, change `events.jsonl`, or another coordinator/runtime path is
created. Adapter/runtime dependencies, if needed to launch the agent, stay
outside the agent-visible filesystem surface and cannot be opened through its
file tools. Tests inspect the actual task directory, attempt traversal/writes
at each former link location, and require both “path unavailable” and
byte-identical host targets. Existing host-link behavior for a separately
enumerated legacy isolation mode is not evidence for `declared_only`.

Read expansion must be exact enough for the role: selected plan/case/review
artifacts, L1/config/memory where declared, existing private tests and adapter
support, and shared `tests/testdata/**` where reuse is allowed. It may not use
`repo:**`, `change:**`, or an all-changes substitute. API/E2E plan writes are
narrowed from `change:plans/**` authorization to their exact four required
outputs plus the layer's conditional `data-knowledge.proposal.*.yaml` path.

### D12. Eval write evidence is content-aware

Git porcelain membership is not a sufficient before/after proof: modifying a
path that was already dirty or untracked leaves the same status entry, and an
ignored path may never appear. Each eval attempt therefore records a complete
leaf manifest of its isolated SUT (excluding only `.git/**` and the external
attempt/evidence directory). Every regular file records executable/mode bits
and content digest; every symlink records its target without following it.

The after scan compares the two manifests; porcelain snapshots remain only as
compatible diagnostic evidence. A path is changed when it appears/disappears,
changes kind/mode/target, or changes content. This detects clean tracked,
pre-dirty, untracked, and ignored paths uniformly. `write-diff.json` persists
the normalized before/after records and reason for each changed path plus the
SHA-256 of canonical `write-manifest-before.json` and
`write-manifest-after.json`. Scorer replay verifies those digests and performs
the same decision without rereading a mutated worktree. It recomputes the full
canonical diff from both manifests and requires exact equality with persisted
`write-diff.json`; it never trusts a stored count/path list. A forged empty
diff over manifests that show a forbidden change is evidence-integrity failure
before policy scoring.

This is an end-state detector. A transient forbidden write that is fully
restored before the after snapshot is outside its guarantee; runtime
`authorization_writes` remains the primary per-attempt enforcement boundary.

The persisted manifests use one canonical schema:

```python
class StrictWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class WorktreeManifestEntryV1(StrictWireModel):
    path: str                 # normalized repository-relative POSIX path
    kind: Literal["file", "symlink"]
    mode: int
    size: int
    sha256: str | None        # required for file, absent for symlink
    symlink_target: str | None  # required for symlink, absent for file


class WorktreeManifestV1(StrictWireModel):
    schema_version: Literal["1"]
    entries: list[WorktreeManifestEntryV1]
    total_entries: int
    total_file_bytes: int
```

Every new JSON/CAS/event/eval model introduced by D12-D15 and section 5.3
inherits `StrictWireModel` (including candidate receipts, acknowledgements, and
kind-specific effect payloads). JSON collections use `list[...]`, not strict
tuples that reject decoded arrays. Per-model mutations prove unknown fields and
string-to-number/boolean coercions fail rather than being dropped or normalized
into pass-shaped canonical bytes.

Entries are sorted by UTF-8 POSIX path before canonical serialization;
duplicate/non-normalized paths, field/kind mismatches, or aggregate-count
mismatches are invalid evidence. Directory traversal uses `lstat`, never
follows symlinks, and revalidates kind/identity while opening a regular file.
A socket, FIFO, device, permission/read/stat failure, or path changing kind
during capture is an infrastructure error, not a silently omitted entry.

Capture is deterministically bounded at 250,000 entries and 4 GiB of regular-
file bytes per manifest. Exceeding either cap fails the attempt before runtime
for the before manifest, or fails evidence finalization for the after
manifest. These are resource-integrity failures, never an empty/pass manifest.

### D13. Declared-only task inputs are attempt-bound

For a `declared_only` agent, the scheduler freezes the fully materialized input
view after contract filtering and skill-bundle injection but before the adapter
starts:

Declared-only workspace setup does not create the convenience `.git/**` tree
inside the agent project root. Tree-control metadata (currently
`.graph-runtime/tree.json`) moves to a TaskWorkspace sidecar outside that root,
along with the host paths prohibited by D11. Therefore the snapshot authority
domain is exactly the filtered TreeStore entries plus injected skill files; it
does not hash nondeterministic Git objects or platform metadata. Legacy
isolation modes may retain their convenience Git behavior. Tests prove
excluded sidecar metadata is neither agent-readable nor accepted as a plan/
case input by scorer or fixer authority.

```python
class TaskInputSnapshotEntryV1(StrictWireModel):
    physical_relpath: str
    repo_relpath: str | None
    logical_aliases: list[str]
    matched_claims: list[str]
    origins: list[Literal["contract_read", "skill_bundle"]]
    kind: Literal["file", "symlink"]
    mode: int
    sha256: str | None
    symlink_target: str | None


class TaskInputSnapshotV1(StrictWireModel):
    schema_version: Literal["1"]
    invocation_id: str
    task_id: str
    attempt_id: str
    base_tree_id: str
    materialized_tree_id: str
    input_sha256: str
    runtime_context_sha256: str | None
    contract_digest: str
    claims_digest: str
    entries: list[TaskInputSnapshotEntryV1]
```

The API/E2E plan-fixer skills no longer inspect `events.jsonl` to infer why
they were invoked. The scheduler builds one strict non-file context after
reserving the attempt identity:

```python
class PlanFixerRuntimeContextV1(StrictWireModel):
    schema_version: Literal["1"]
    mode: Literal["automatic_healing", "human_approved"]
    target: Literal["api", "e2e"]
    change_id: str
    root_invocation_id: str
    invocation_id: str
    task_id: str
    attempt_id: str
    base_tree_id: str
    source_review_path: str
    source_review_sha256: str
    source_interrupt_task_id: str | None
    resume_action: Literal["fix_and_proceed"] | None
    human_reason: str | None
    human_decision_sha256: str | None
```

It is stored in CAS, supplied to the adapter as the typed `Runtime Context`
prompt block, and bound by `runtime_context_sha256` on the input snapshot and
both attempt events. It is not materialized as a host-ledger or runtime-control
file. Plan-fixer skills declare the exact context schema in a structured
`## Runtime Context` section; the parity harness permits only the enumerated
`platform:plan-fixer-runtime-context/v1` injection for those two contracts.
All other assurance roles require `runtime_context_sha256 == null`. Missing,
stale, wrong-target/review/tree, or prompt-bytes/context-digest mismatch fails
before adapter dispatch. `automatic_healing` requires all four human-decision
fields to be null. `human_approved` is constructed only from the current
audited plan-review interrupt/resume transition and requires its interrupt
task, exact `fix_and_proceed` action, non-empty reason, and canonical decision
digest. This preserves the existing human-review route while preventing a
blank automatic context from laundering approval. The model sees mode, source
review, and any exact human authorization without gaining access to the
coordinator ledger.

Entries are unique by normalized task-project-relative physical path,
canonically sorted, and obey the same kind-field rules as D12. Every covering
declared claim/root-derived exact alias is retained; overlapping `project:` and
`repo:` roots are not collapsed by lexical tie-break. `repo_relpath` is set
only after containment-safe normalization against the bound repo root, and the
scorer compares plan Target Files through this field rather than through a
logical-root name. An aliased-root fixture proves one physical file is recorded
once with both aliases/claims and remains addressable as `repo:tests/...`.

The snapshot describes every visible materialized file, not a best-effort log
of files the model happened to open. Its canonical CAS digest is
`input_snapshot_id`, recorded on both `task_attempt_started` and
`task_attempt_succeeded`; the two IDs must match. The success event field is
optional for historical event compatibility, but every new declared-only
attempt must set it and current codegen scoring requires it.

This requires reordering `_begin_attempt`: reserve the attempt identity/lease,
materialize the filtered workspace, capture/store the snapshot, and only then
append `task_attempt_started` containing its ID. A crash after snapshot CAS
creation but before the started event leaves only unreachable workspace/CAS
data; recovery cleans it and retries with the same ledger-derived attempt
number. Once a started event exists, its snapshot object must already exist and
validate. No started event with an empty/deferred snapshot ID is permitted.

Synchronized project locks are acquired **before** reserving/materializing an
agent attempt. A conflict emits a separate scheduling-level
`task_scheduling_deferred` event containing task/token/reason but no attempt ID,
attempt number, budget consumption, or physical-attempt credit. Once the lock
is available, normal begin creates the first started event with its snapshot.
The former started+failed lock-conflict shape is removed for new events rather
than exempted from the snapshot invariant. A real two-owner lock regression
asserts no started/failed attempt on conflict and exactly one input-bound
attempt after release.

The deferred event is nevertheless durable scheduling state. It carries a
deterministic deferral ID, invocation/checkpoint/superstep/task/token,
monotonic deferral ordinal, retry-policy digest, and `next_retry_at`. Projection
folds the highest exact ordinal and suppresses task reselection before that
time; exact duplicate ID/payload is idempotent and a conflict is corruption.
At the next due time, continued lock conflict records one next ordinal with
normal capped backoff—never a hot loop or unlimited same-instant event spam.
It does not commit the superstep. Fresh runtimes honor the projected retry
time; after lock release, the next due selection creates exactly one real
attempt. Clock-controlled tests cover sustained conflict, multiple restarts,
deduplication, backoff, and eventual single execution.

The snapshot and any typed runtime context are frozen before any agent output,
host mount, or command. A
missing/tampered snapshot, one bound to another attempt/tree/contract, omitted
required plan/case mapping, or start/success mismatch is an authority failure.
Final workspace copies may differ after later graph steps; scorer and fixer
authority always use bytes referenced by the bound snapshot.

An AST consumer-set test pins the field name and requires parsed runtime
consumers in attempt-start serialization, attempt-success serialization/fold,
evidence export, fixer-authority derivation, and codegen scoring. This prevents
an implementation from producing a snapshot that no authorization boundary
actually reads.

A second consumer-set guard pins `runtime_context_sha256` to plan-fixer task
preparation, prompt rendering, start/success serialization/fold, resume, and
evidence export. It also requires zero plan-fixer skill or declared-only
workspace references to `events.jsonl`; this prevents a typed context from
being added while the ambient-state escape hatch remains live.

### D14. Candidate authorization precedes commit; durable effects are inline

Execution contracts may name a registered `precommit_validator`. After normal
artifact ingest and `freeze_write_set`, but before
`task_attempt_succeeded`/superstep commit, the scheduler invokes that validator
with the frozen input snapshot, outputs, and write set. It returns a canonical
CAS-backed validation receipt whose ID is recorded on the success event.
Validation failure is `invalid_output`: the candidate workspace/write set is
discarded and the canonical tree/host coordinator bytes remain unchanged.

The validator also receives a code-owned `PrecommitValidationContext` resolved
from the task's pinned root/child binding: current committed tree, pinned policy
object/digest, source gate/interrupt projection, and definition semantics. This
context is not an agent-visible host mount. For high risk, the approval receipt
must be present in the input snapshot, and the code-owned context must verify
its event/source/target-tree lineage; both views are required.

`CandidateValidationReceiptV1` binds validator ID and semantics digest, root/
invocation/task/attempt IDs, `input_snapshot_id`, canonical output digest map,
`write_set_id`, and the validated decision payload digest. The event field is
optional for old attempts, but required whenever a contract names a validator.
The execution-contract loader rejects an unknown validator or a validator
without a registered semantics manifest. The two codegen-fixer contracts
select `codegen_fix_candidate/v1`, while the four codegen contracts select
`generated_files_candidate/v1`; consumer-set tests pin both exact, disjoint
sets. Cross-root/attempt/tree/snapshot/write-set/output substitution or a
mismatched validator-semantics digest invalidates the receipt.

The two codegen-fixer contracts use
`precommit_validator: codegen_fix_candidate/v1`. This boundary enforces target,
intent, proposal, fixer-authority, exact claimed/write path equality, allowed
operations/digests, baseline rules, forbidden diff-safety predicates, and any
required audited proposal-approval receipt from section 5.3 before a test edit
can commit. The later record operation verifies the receipt binding and emits
authoritative audit/safety output; it is not the first authorization check.

The four codegen contracts use
`precommit_validator: generated_files_candidate/v1`. Its context exposes the
frozen input snapshot, typed plan/case/mapping artifacts, all hard output
digests, and the candidate write set. It performs D16's full manifest-to-
mapping/input/write-set reconciliation after ingest/freeze but before the
success event. A shape-valid but cross-artifact-invalid manifest is therefore
`invalid_output`, leaves no committed codegen attempt/tree change, and cannot
be deferred to the benchmark scorer.

Code-owned side effects use a target-agnostic durable-effect protocol:

```python
class DurableEffectIntentV1(StrictWireModel):
    schema_version: Literal["1"]
    effect_id: str
    kind: Literal[
        "healing_allocation/v2",
        "fixer_proposal_approved/v1",
        "heal_record_apply/v2",
    ]
    reconciler_semantics_digest: str
    payload_sha256: str
    payload: dict[str, object]
```

`payload` is not semantically opaque: the kind registry validates it through a
strict `HealingAllocationEffectV2`, `FixerProposalApprovedEffectV1`, or
`HealRecordApplyEffectV2` model before success. Execution contracts declare an
exact `durable_effects` kind list. Allocate, record-fixer-approval, and each
target record operation require exactly one matching effect; combine/fixer/
ordinary tasks require zero unless explicitly declared. Missing, extra,
duplicate, wrong-producer/kind, or malformed effects are `invalid_output`
before the success event. Packaged compilation verifies the exact four
producer-to-kind bindings.

Effects in a success event are non-empty-ID, unique, and canonically sorted by
`effect_id`; `payload_sha256` must equal the canonical payload digest.
`effect_id` is deterministically derived from kind, producer invocation/task/
attempt IDs, and the domain idempotency key, so recovery of the same successful
attempt cannot drift. The acknowledgement event binds invocation/task/attempt,
effect ID, kind, reconciler-semantics and payload digests, and the resulting
domain-event source sequence/canonical digest.

Validator registry/dependencies and durable-effect registry/reconcilers are
covered by one canonical staged manifest,
`runtime_commit_safety/v1`. New root/child definition bindings carry its
`commit_safety_semantics_digest` and object ID; children inherit exact identity,
and live/pinned resume verifies bytes before candidate validation or effect
reconciliation. The validator receipt carries the validator dependency digest;
every inline effect carries the selected reconciler digest, both of which must
be members of the root-bound manifest. A v1-v5 root has no such binding. If its
remaining reachable assurance path contains codegen, codegen-fixer, or a
graph-owned operation that would require a precommit validator/durable effect
under this release, resume is blocked as
`legacy_commit_safety_semantics_unbound` before dispatch. A topology
compatibility receipt alone cannot authorize it; the operator must start a
fresh v6 root or choose an audited manual disposition through D18's supersede
transition. This avoids executing current commit safety under an old
definition without leaving the change permanently active.

The manifest dependency inventory is closed, not a hand-maintained sample. It
contains the exact validator IDs and implementations, both validator context/
receipt builders, generated-file and fixer safety/path helpers, strict effect
intent/payload/ack models, effect-ID derivation, the three effect-kind
reconcilers and their domain-event builders/projections, acknowledgement
validation, and retry-sidecar semantics. An AST/import consumer-set test
requires every registered validator/effect kind and every declared helper to
appear exactly once in the manifest inventory; mutating each dependency in
turn must change the aggregate digest. A separate field-consumer guard requires
the commit-safety object ID/digest in root-start/definition-binding writes,
root and child projections, child inheritance, pinned-definition requests,
live/replay compatibility checks, validator/effect dispatch, snapshots, and
evidence export. A manifest that is produced but skipped by any execution
consumer is a packaging failure.

`TaskResult` supplies zero or more typed effects. Their full canonical
intent(s) are embedded in the **same single** `task_attempt_succeeded` JSONL
record (new optional field, empty for historical events); they are not a
second “pending” event that can be lost after success. The scheduler then
commits/applies the frozen superstep, a registry-selected reconciler
appends/reuses the strict domain event while verifying the committed hard-
output digests, and finally appends `durable_effect_acknowledged`. Successors
remain unplannable until every effect ID is acknowledged and the
superstep/write set is committed.

If the process dies before the success line, normal retry owns the candidate.
If it dies after that line, recovery always sees the complete intent, repairs
or verifies the committed frozen write set, scans unacknowledged effects from
succeeded attempts, and reconciles same-key/same-payload without reinvoking the
successful handler. Same key with different payload is ledger integrity
failure. The scheduler dispatches by registered effect kind, never by
operation target string; allocation, approval recording, and the API/E2E
record-apply operations are the exact first four producers of this general
seam.

Projection accepts an acknowledgement only when it references an exact inline
intent on a committed successful attempt and the bound domain event validates.
An exact duplicate acknowledgement folds idempotently; conflicting ack/domain
identity is corruption. Retryable I/O/lock failures leave the effect visibly
unacknowledged for recovery. A permanent model/digest/domain conflict records a
terminal `durable_effect_integrity_failed` result and stops the invocation; it
must not leave a succeeded task with an indefinitely pending successor and no
diagnostic.

Reconciliation backoff cannot be recorded through the same progression ledger
lock whose contention may be the failure. It uses a coordinator-owned,
agent-invisible sidecar with an independent compare-and-swap/atomic-replace
boundary:

```python
class EffectRetryStateV1(StrictWireModel):
    schema_version: Literal["1"]
    root_invocation_id: str
    invocation_id: str
    task_id: str
    attempt_id: str
    effect_id: str
    kind: str
    lock_key: str
    ordinal: PositiveInt
    retry_policy_digest: str
    next_retry_at: StrictStr  # canonical RFC 3339 UTC with `Z`
    last_retryable_error_code: str
```

Recovery scans committed unacknowledged intents, loads an exact matching
sidecar, and suppresses reconciliation until `next_retry_at`. Each due
contention advances one ordinal with capped deterministic backoff; concurrent
runtimes use the sidecar CAS so only one next state wins. This consumes no task
attempt and successors remain blocked. A completed domain event plus
acknowledgement makes any stale sidecar inert and eligible for cleanup. A
permanent payload/model/domain conflict bypasses retry state and follows the
terminal integrity path. Tests hold the real progression lock across multiple
runtime restarts, prove no busy loop before due time, then release it and
observe exactly one domain event, one acknowledgement, and one successor.
The strict model validates and parses the timestamp explicitly after wire
validation; it does not rely on Pydantic coercing a JSON string to `datetime`.

### D15. Evidence paths compare by pinned physical identity

Write-set logical roots are not stable path identity: when `project` and
`repo` both map to `.`, TreeStore may serialize `tests/...` as
`project:tests/...`. Add one code-owned resolver shared by fixer authority/
precommit, eval policy, selected-test classification, and scorer. Given the
write set's `base_tree_id`, pinned tree-root mapping, current change location,
and a logical entry, it returns:

```python
class ResolvedEvidencePath(StrictWireModel):
    physical_relpath: str
    repo_relpath: str | None
    ownership: Literal["current_change", "repo", "project"]
    logical_aliases: list[str]
```

Resolution normalizes without following an escaping symlink, preserves every
equivalent alias, and assigns ownership by physical containment: current
change is most specific, then repo, then project. Intent/plan/policy comparisons
use normalized `repo_relpath`; exclusion of intent/summary change outputs uses
`current_change` ownership. No consumer compares the raw `logical_path` root
name. Missing pinned roots, path escape, or a physical mapping that cannot be
classified uniquely is fail-closed.

Positive controls cover API/E2E/Fuzz/Performance write entries serialized as
`project:tests/...` but resolved to the correct selected repo root. Negative
controls cover alias/root substitution, another change, ambiguous containment,
and traversal.

### D16. Generated-file authority is structured, not prose-derived

Every codegen node emits its existing human summary and one path-specific
strict JSON manifest. The Markdown `Generated Files` section may remain for
readability, but no gate, fixer-authority builder, or scorer parses it as an
authorization source.

```python
class GeneratedFileEntryV1(StrictWireModel):
    repo_path: StrictStr
    disposition: Literal["generated", "updated", "reused"]
    role: Literal["test_entry", "support", "shared_builder"]
    case_ids: list[StrictStr]
    content_sha256: str


class GeneratedFilesV1(StrictWireModel):
    schema_version: Literal["1"]
    change_id: str
    files: list[GeneratedFileEntryV1]


class ApiGeneratedFilesV1(GeneratedFilesV1):
    layer: Literal["api"]


class E2eGeneratedFilesV1(GeneratedFilesV1):
    layer: Literal["e2e"]


class FuzzGeneratedFilesV1(GeneratedFilesV1):
    layer: Literal["fuzz"]


class PerformanceGeneratedFilesV1(GeneratedFilesV1):
    layer: Literal["performance"]
```

The four exact catalog paths are
`change:codegen/{api,e2e,fuzz,performance}-generated-files.json`; each selects its
concrete model, so a valid E2E payload cannot be written at the API path.
Entries and `case_ids` are normalized, unique, and canonically sorted. Every
`generated`/`updated` entry must equal a regular-file add/content-modify in the
same committed codegen write set, and `content_sha256` must equal its after
digest. The full set of repository test/testdata writes must appear in the
manifest; omission is invalid output. A `reused` entry writes nothing, must be
an exact selected private-root target in the plan mapping, must be present in
the codegen input snapshot, and binds its snapshot content digest. Reuse never
grants authority to shared testdata or an unmapped helper.

The current plan mapping and automated cases—not agent-supplied `case_ids`—
decide which entries are mapped test entries. `case_ids` must equal that
derived relation; `support` and `shared_builder` entries cannot earn selected-
test credit. Both summary and manifest digests are mandatory codegen outputs.
Fixer authority and the benchmark scorer consume the manifest, plan/case
mapping, input snapshot, and write set as one closed proof. Summary-only,
malformed, duplicate, cross-layer/change, wrong-disposition/digest, unbound
reuse, and manifest/write-set mismatch cases fail at artifact ingest or the
named authority boundary.

That named boundary is D14's
`precommit_validator: generated_files_candidate/v1`, selected by exactly the
four codegen execution contracts. It runs after strict artifact ingest and
write-set freeze, with the bound input snapshot and typed plan/case artifacts,
but before `task_attempt_succeeded`. The validator semantics and cross-artifact
helpers are members of `runtime_commit_safety/v1`, and its receipt binds the
manifest/output, input-snapshot, write-set, and derived mapping-decision
digests. This is intentionally stronger than Pydantic shape validation.

### D17. Change location is exported as replayable evidence

Policy replay must not infer a custom change root from the scorer's mutable raw
SUT. The executor resolves change location once, using the same production
pure resolver as runtime, and persists both the exact configuration bytes and
a strict decision record in the external evidence directory:

```python
class ChangeLocationCandidateV1(StrictWireModel):
    source: Literal["changes", "archive"]
    configured_root: str
    candidate_repo_path: str
    lstat_kind: Literal["missing", "directory", "symlink", "other"]
    mode: int | None
    has_before_manifest_leaf: bool
    selected: bool


class ChangeLocationEvidenceV1(StrictWireModel):
    schema_version: Literal["1"]
    change_id: str
    preference: Literal["active"]
    selected_source: Literal["changes"]
    resolved_change_repo_path: str
    configured_changes_root: str
    configured_archive_root: str
    config_sha256: str
    candidates: list[ChangeLocationCandidateV1]
```

`change-location-config.yaml` is a byte-for-byte copy of the configuration
input used by the resolver; `change-location.json` binds its digest and the
candidate/selection result. Candidate probes use containment-safe `lstat`
without following symlinks; kind/mode consistency is strict, and the leaf bit
is recomputed from the D12 before manifest. `execution.json` binds both files'
digest/size and that before-manifest digest. The offline scorer strictly parses
the record, verifies the copied bytes and leaf bits, and reruns the same pure
resolver over those bytes plus the candidate probes. It requires byte-identical
output before reconstructing write policy.

This write workflow always calls the production resolver with
`preference="active"`: its selected source is the configured `changes` root.
The archive remains a recorded candidate so coexistence and ambiguity can be
replayed, but archive-only data is not converted into writable policy.
Absolute/parent paths, symlink escape, a missing or extra candidate, ambiguous
selection, archive-only resolution, digest drift, or disagreement with
`execution.json` is evidence-integrity failure/infrastructure failure before
runtime. Tests cover non-default configured changes/archive roots, coexistence
selecting `changes`, archived-only rejection, missing/tampered configuration,
another change, and path escape.

### D18. A blocked legacy root has an audited, reachable exit

Returning `legacy_commit_safety_semantics_unbound` from resume is not enough:
the nonterminal root would keep the change-level active-invocation guard, and a
`restart: once` entrypoint would reject an ordinary fresh run after a terminal
stop. Add an explicit operator command:

```text
aa workflow supersede --change <id> --invocation <root> \
  --action rerun-v6|stop --who <identity> --reason <text> [--params <json>]
```

The command is accepted only for the latest active **root** whose exact pinned
replay currently fails with
`legacy_commit_safety_semantics_unbound`. It rejects a child ID, another
change/entrypoint, or an already-terminal unrelated root. Eligibility scans the
root's complete recorded descendant-invocation closure and rejects any live
lease/open attempt, prepared or uncommitted superstep/write set, pending
synchronized publication, unacknowledged durable effect/retry sidecar, or child
being concurrently created anywhere in that subtree. Under the progression
lock it verifies the expected event sequence, pinned binding, canonical sorted
descendant IDs, and subtree digest, then appends one strict
`graph_invocation_superseded` event binding those values plus root/entrypoint,
stable reason code, operator/reason, action, resolved replacement params
digest, current v6 definition-request digest, and a deterministic
`supersede_id`. No reason is inferred from free-form error text.

The root event is a terminal fence for the entire bound subtree: root and child
projections report stopped/superseded for scheduling, direct
`GraphRuntime.resume(child_id)` returns a stable superseded result, recovery
does not adopt/retry a descendant, and a late worker/publication is rejected by
the fence. Existing status consumers may render `stopped`, while the typed
supersede audit record remains available. Exact replay is idempotent; same ID
with different payload is corruption.

For `stop`, no replacement authority is created. For `rerun-v6`, the same event
contains a single-use `replacement_authorization_id`. A new optional pair on
the v6 root-start event—`supersedes_invocation_id` and
`replacement_authorization_id`—must consume it under the progression lock.
This is the only bypass to `restart: once`; it applies only to the same
entrypoint, exact bound params/current v6 definition request, and an unused
authorization. Import-checkpoint, another entrypoint, a second replacement,
or a generic `run` without the ID cannot consume it. The command stages the
current v6 definitions before terminalizing the old root and then immediately
starts/drives the replacement; if it crashes between those steps, an exact
retry consumes the pending authorization. If the replacement root-start event
already exists, retry resumes that root rather than creating another.

`--params` is valid only with `rerun-v6`; omission reuses the old root's exact
recorded parameter map as overrides to the current resolver, while an explicit
value replaces those overrides. The resolved canonical bytes, not the raw CLI
string, are authorized. `rerun-v6` fails before superseding unless the staged
definition request resolves to event schema v6 and those params are valid.
`stop` rejects `--params` and leaves replacement fields null. Once the
supersede event exists, retry loads its staged definition request by digest
rather than silently switching to a still newer package.

Crash/concurrency tests cover before supersede append, after supersede before
root start, after root start before command return, two concurrent exact
commands, conflicting params/reason, active-guard behavior, `restart: once`, a
live child lease, an open child superstep/write set, direct child resume, and a
late publication. They prove refusal until the subtree is quiescent, then one
terminal supersede event and at most one bound replacement root. This makes
“fresh v6 rerun” reachable without weakening ordinary restart policy; `stop`
is the audited manual disposition.

## 5. Canonical Four-Layer Contract Bundle

### 5.1 Fixture ownership

Create one independent fixture root per layer under
`tests/fixtures/assurance/`. API must not import benchmark files, and no layer
may inherit pass-shaped review/check/codegen output from another fixture.

Each fixture contains:

- one valid added or modified automated case for the exact case type;
- the layer's complete plan artifact set;
- the layer's plan summary (`m3-review-summary.md`,
  `m4-review-summary.md`, `fuzz-review-summary.md`, or
  `performance-review-summary.md`);
- a valid `PlanReviewAuthoring` document and its frozen `PlanReview` form;
- `.aa/config.yaml` and `.aa/data-knowledge.yaml` when declared by the role;
- an L1 domain-factory capability rooted at
  `tests/testdata/domain/account.py`;
- the exact existing layer adapter/test paths declared by the contract; and
- no pre-generated mechanical checks or codegen output.

The canonical shared capability is always named from L1, for example
`capabilities.domain_factories.account.make_account`, and every plan Factory
Mapping uses `reuse` against `tests/testdata/domain/account.py`. A separate
mutation fixture covers `create-if-missing`; the baseline does not depend on
creating shared state.

### 5.2 Fixed check expectations

| Layer | `l1_path` | `shared_factory` | `assert_ideal` | `capability_keys` |
|---|---:|---:|---:|---:|
| API | pass | pass | pass | pass |
| E2E | pass | pass | pass | pass |
| Fuzz | pass | pass | not_applicable | pass |
| Performance | pass | pass | not_applicable | pass |

All four check entries are present for every layer, and the production
producer emits `PLAN_CHECK_IDS` order. Consumer validity is the exact set,
not the incidental wire order. Static N/A is an explicit entry with
`check_not_in_profile`; it is not omission. An empty dynamic layer emits four
N/A entries with `layer_not_applicable`.

### 5.3 Skill and execution-contract closure

The skill cleanup follows these rules:

- give every assurance skill an exact structured `## State Authority` section
  containing `owner: graph_ledger` and `agent_state_writes: forbidden`;
- remove every literal/path reference to `workflow-state.yaml` and every
  prompt-owned `phases.*`/state-delta instruction from these skills; graph
  events and their compatibility projection remain the only state authority;
- remove `events.jsonl` from both plan-fixer skills; their automatic-healing
  mode and source review arrive only through D13's typed Runtime Context;
- make every Fuzz/Performance `## Inputs` heading structurally valid;
- use `tests/testdata/domain/**` for shared domain builders in all examples;
- list summaries, cases, proposal/config/knowledge, and existing test support
  paths in the structured Inputs section only when the skill actually reads
  them; and
- keep plan, reviewer, plan-fixer, codegen, and codegen-fixer writes confined
  to their existing role outputs.

Execution-contract reads are then changed narrowly to cover those structured
inputs. The parity test rejects both directions of drift:

- every structured required/optional skill input must be covered by a
  contract read; and
- every agent-visible contract read must map back to a structured skill input.

The only reverse-direction exemptions are enumerated platform-injected
metadata that the agent cannot open directly; there is no wildcard exemption.
Optional product-source, memory, and existing-test reads remain explicit; they
are never implemented as `repo:**`.

Write closure is equally bidirectional:

- every structured skill output is covered by both `writes` and
  `authorization_writes`;
- every agent-owned contract write and authorization maps to one structured
  required or conditional output; and
- `writes` and `authorization_writes` cover the same agent-owned logical path
  set after prefix normalization.

An enumerated platform-owned synchronized/runtime path may be exempted only by
a typed exemption carrying exact path, owner, and reason; it cannot appear as
an agent output or authorize product/test writes. The parity test rejects an
extra exact path as well as a broad prefix, so narrowing one side cannot leave
stealth authority on the other.

The `## State Authority` parser validates exact keys/values and requires zero
occurrences of `workflow-state.yaml` and `phases.` across all sixteen skill
files. This is a bounded structural/token invariant, not a mutation test over
English prose. API/E2E codegen fixers replace their former state-derived
generated-file list with declared execution/inspection/failure artifacts and
the graph-owned fixer-authority projection; their structured Outputs list only
the target intent plus authorized private/shared test changes. Ledger events
and frozen artifacts—not a compatibility projection or chat-reported delta—
decide retry/fix authority.

Related executable contradictions use the same zero-occurrence guard: API/E2E
plan-fixer skills contain no `events.jsonl`, API/E2E codegen skills contain no
`pytest --collect-only` or fabricated “Traceability
Verification” collection claim, and API/E2E codegen-fixer skills contain no
`aa heal record-apply`. Their exact Outputs sections are parsed as part of
contract parity, so a stale command or legacy apply-summary output fails the
fixture before a deterministic adapter can mask it.

All sixteen assurance agent contracts (four plan, four reviewer, API/E2E plan
fixer, four codegen, and API/E2E codegen-fixer targets) opt into
`read_isolation: declared_only`. API/E2E plan contracts replace their broad
`authorization_writes: [change:plans/**]` entries with their four required
plan/summary files and one exact conditional knowledge-proposal path; `writes`
covers the same five paths even though only the four always-required files are
node outputs. Reviewer/fixer/codegen contracts keep their role-specific write
sets. Existing private tests, layer adapters/config, layer-specific product
source roots used by planners, and `tests/testdata/**` are added to reads only
for roles whose structured Inputs declare that surface.

The API/E2E codegen-fixer skills receive focused workspace fixtures built from
the canonical generated-file manifest, human codegen summary,
execution/inspection/failure evidence, a typed graph-owned fixer-authority
projection, existing selected private tests, and shared testdata. Tests prove
they can locate the failed generated file without
`workflow-state.yaml`, cannot read a sibling/change projection, and can freeze
only their existing fixer result plus authorized private/shared test writes.
They are contract-closure smoke tests, not extra nodes in the plan-to-codegen
round trip.

The fixer agent no longer invokes `aa heal record-apply`. Instead each fixer
has exactly one hard artifact output, registered with a strict ingest model:

```python
class CodegenFixApplyIntentV1(StrictWireModel):
    schema_version: Literal["1"]
    outcome: Literal["applied", "no_op", "skipped"]
    proposal_ids: list[StrictStr]
    reason: StrictStr | None = None
    claimed_modified_paths: list[StrictStr]


class ApiCodegenFixApplyIntentV1(CodegenFixApplyIntentV1):
    target: Literal["api"]


class E2eCodegenFixApplyIntentV1(CodegenFixApplyIntentV1):
    target: Literal["e2e"]
```

The two path-specific catalog entries select the two concrete models, so an
E2E target cannot validate at the API path. Their validators enforce canonical unique
proposal/path order, normalized allowed paths, a non-empty applied proposal
set, and a required reason for `no_op`/`skipped`. The API/E2E fixer node output
becomes `healing/api-apply-intent.json` or
`healing/e2e-apply-intent.json`. Its contract writes only that intent and the
target private/shared test roots; exact proposal, entry-baseline,
execution/inspection/failure, summary, and existing-test inputs replace the
current incomplete reads and broad `change:healing/**` authorization. The
graph-owned `healing/fixer-proposal-approval.json` is a conditional structured
input and exact contract read: absence is valid only for a low-risk proposal;
high-risk precommit requires its bytes in the bound input snapshot.

`allocate-healing-attempt` also materializes a strict
`healing/fixer-authority.json`. For each active target it binds the current
codegen attempt, generated-file-manifest digest, summary digest, write-set ID,
execution batch, and the exact normalized path authority. A generated/updated
path is proven by an
`add`/content-`modify` write-set entry and binds its after digest. A reused path
is permitted only under the selected private root when D16's committed
manifest and plan mapping both name it; the projection binds its input-snapshot
digest, and later fixer modification must have that exact before digest.
Summary-only helpers or shared-testdata reuse do not gain edit authority;
shared testdata is editable only when the current codegen write set already
created/updated that exact path.

The agent may read this frozen projection but never the ledger or CAS object
store. Proposal target paths must be a subset of it. The later record operation
re-resolves and verifies every binding from authoritative ledger/object data;
the projection is a least-authority input, not a new source of truth. A merely
`task_imported` codegen completion has no physical attempt/write set and cannot
create this authority: `fixer-authority-ready` fails with
`unverified_imported_codegen`, no fixer runs, and the healing graph terminates
failed for explicit human/rerun handling. Existing imported L2/L3 success paths
remain unchanged; only their formerly unproven automatic-fix path is
intentionally closed and receives a regression test.

Healing then uses this exact topology:

```text
allocate -> fixer-authority-ready
              pass -> fixer-proposal-approval
                        pass -> fixer-dispatch
                                  -> fix-api -> record-api  \
                                                                  fixer-join -> combine-fixer-safety -> safety
                                  -> fix-e2e -> record-e2e /
                        needs_human_review -> fixer-approval-interrupt
                          approve_and_apply -> record-fixer-approval -> fixer-proposal-approval
                        stop ---------------------------------------> complete-failed
              stop ------------------------------------------------> complete-failed
```

The gate route has exactly one `pass` target, `fixer-dispatch`. That
side-effect-free code-owned operation has no hard outputs and fans out through
two ordinary guarded edges; each guard selects its target only when the frozen
proposal has an eligible API/E2E entry. This is representable by the existing
one-target-per-route-case schema. There are no duplicate `pass` route cases,
and the dispatch value is not a second selection authority.

`fixer-proposal-approval` passes low-risk proposals directly. Any eligible
proposal marked high-risk/`needs_review`, or required by policy to receive
human approval, reaches an audited `healing.fixer_approval` interrupt **before**
either agent runs. `approve_and_apply` records an immutable receipt binding the
proposal/authority/entry-baseline/policy digests, exact target/path set, and
source gate; `stop` terminates. Approval resumes into the code-owned
`record-fixer-approval` operation, not directly to the gate. That operation
reads the bound resume/source-gate evidence, writes the receipt as its one hard
output, and returns to the approval gate only after its superstep and durable
effect are committed/acknowledged. Proposal or tree drift invalidates it.

The receipt is a strict registered graph-owned artifact, never an agent
output:

```python
class FixerProposalApprovalReceiptV1(StrictWireModel):
    schema_version: Literal["1"]
    approval_id: str
    root_invocation_id: str
    interrupt_task_id: str
    source_gate_attempt_id: str
    source_tree_id: str
    proposal_sha256: str
    fixer_authority_sha256: str
    entry_baseline_sha256: str
    policy_sha256: str
    targets: list[Literal["api", "e2e"]]
    paths: list[StrictStr]
    action: Literal["approve_and_apply"]
```

Targets/paths are non-empty, unique, canonical, and derived from the source
gate reads. `approval_id` deterministically binds every field plus the audited
resume transition. A strict `fixer_proposal_approved` event and
`healing/fixer-proposal-approval.json` projection share that identity. Exact
transition replay is idempotent; same ID/different bytes is corruption. The
operation's inline `fixer_proposal_approved/v1` effect is reconciled after its
receipt write set commits; the event additionally binds the resulting target
tree ID. The approval gate and fixer input snapshot read the receipt from that
committed tree, then verify event/projection/source interrupt and current read
digests before pass. No direct host write is an approval projection.

Allocation itself uses D14's recoverable outbox. Its hard-output candidates are
entry-baseline/fixer-authority, and its complete
`healing_allocation/v2` effect intent is embedded in the same success event.
After the superstep/write set is committed, reconciliation appends or reuses
one strict `healing_attempt_allocated_v2` event and acknowledges the effect
before the scheduler may plan `fixer-authority-ready`. On the first episode the
single v2 payload embeds the entry-baseline identity/data plus allocation; on a
later attempt it binds the existing baseline ID plus allocation. This avoids a
kill prefix between separate baseline/allocation event lines. Historical
`healing_entry_baseline_pinned` + `healing_attempt_allocated` pairs remain
parseable/foldable; new projection logic folds either legacy pair or v2 but
never mixes them for one allocation. Recovery scans succeeded
attempts with unacknowledged inline effects. Same-key/same-payload replay is
idempotent; a mismatch is integrity failure. This closes the existing task-
success-before-side-ledger crash window without reinvoking the successful
operation or relying on two event appends being atomic.

One normalized `HealingEpisodeProjection` is the sole version-aware consumer.
It converts a legacy baseline/allocation pair or a v2 combined event into the
same baseline plus ordered logical allocations. `derive_healing_state`,
`derive_guard_context`, budget/allocation checks, record-apply safety,
workflow-history/retro projection, and graph guards consume this projection;
none filters raw legacy event names. An AST consumer-set test permits direct
`healing_entry_baseline_pinned`/`healing_attempt_allocated` matching only in
the event codec, the normalized projector, and versioned fixture tests.

The first v2 event yields `attempts_used == 1`, a current active guard context,
and a reachable record-apply path. A later v2 allocation reuses the exact
baseline ID and increments once. A legacy pair projects identically. A mixed
ledger may contain distinct legacy and v2 allocations in order; if both forms
claim the same logical allocation key, byte-equivalent payload is deduplicated
and any ID/payload/baseline conflict is ledger corruption. Tests exercise all
named public consumers against v2-only, legacy-only, and mixed ledgers so a
new writer cannot ship while guards still read only the old names.

Hard outputs and ingest identities are exact:

- `allocate.outputs` contains `change:healing/entry-baseline.json` and
  `change:healing/fixer-authority.json`; both have strict registered models,
  and the authority document records ready/unverified status for every active
  target;
- `fix-api.outputs`/`fix-e2e.outputs` contain only their path-specific intent;
- `record-fixer-approval.outputs` contains only
  `change:healing/fixer-proposal-approval.json`;
- `record-api.outputs` contains
  `change:healing/api-apply-summary.json` and
  `change:healing/api-fixer-safety-check.json`, with E2E symmetric; and
- `combine-fixer-safety.outputs` contains only
  `change:healing/fixer-safety-check.json`.

Authority, intent, approval receipt, target apply summary, target safety
fragment, and aggregate safety each have a distinct catalog symbol/model identity. Markdown apply
summaries are optional code-owned projections and never hard evidence. A
record operation missing either JSON hard output fails before the join; a
contract `writes` entry alone cannot satisfy node completion.

`record-api` and `record-e2e` are target-specific uses of the code-owned
`operation:record-codegen-fix-apply`. Each is active iff its fixer is active;
`fixer-join` changes its `all_active` sources to the two record nodes. A record
operation accepts only a current fixer attempt with a committed D14 candidate-
validation receipt, re-verifies that receipt against the intent/write set, and
emits the target apply-summary JSON plus
`healing/<target>-fixer-safety-check.json`. Its inline
`heal_record_apply/v2` durable effect is consumed only by the D14 reconciler,
which appends/reuses the authoritative v2 event after those hard outputs commit
and before the join can plan. The operation handler never writes the host
ledger directly.
`combine-fixer-safety` reads the proposal and **current committed** fragments
for exactly the active target set, rejects missing/stale/unexpected active
evidence, and emits the sole shared
`healing/fixer-safety-check.json` consumed by the existing safety gate. A
single-target run therefore never requires the inactive target's outputs.

The record idempotency key binds root, record task, fixer attempt, intent
digest, and write-set ID. Exact-key/exact-payload replay is a no-op; a payload
conflict is ledger corruption. If a crash follows committed record outputs but
precedes the domain event, recovery consumes the inline effect; if it follows
the event but precedes acknowledgement, reconciliation reuses the same event.
The graph cannot reach the join or safety gate until record/combine attempts
are committed and every record effect is acknowledged. Thus an agent retry
cannot append the host ledger before its own authorized output commit, and a
post-event crash is repairable without applying the fix twice.

This is represented by a new strict `heal_record_apply_v2` event carrying the
record key, root/record-task/fixer-attempt IDs, target, entry batch, intent
digest, write-set ID, outcome, canonical proposal IDs/modified paths, and
safety payload digest. The existing `heal_record_apply` event model remains
unchanged so historical ledgers/goldens still parse and fold. New record
effects reconcile only to v2; projections dispatch both versions, while
idempotent replay/conflict detection is available only from the fully bound v2
payload.
No required field is retrofitted onto the legacy event.

Precommit candidate validation (and record receipt revalidation) uses exact
set equality:

- `claimed_modified_paths` equals every repository test/testdata change in the
  frozen fixer candidate write set; the intent artifact itself is excluded;
- `applied` has non-empty proposal IDs and test writes, every path is authorized
  by both proposal and fixer-authority projection, and no undeclared write is
  hidden by omission; and
- `no_op`/`skipped` has zero repository test writes.

Every `applied` repository entry must be a regular-file `add` or true content
`modify` with non-empty after/blob digest. Delete, symlink/kind replacement,
mode-only change, missing after bytes, or a no-op digest is invalid before an
authoritative apply event can be appended. Precommit also runs the code-owned
assertion-expected-value, skip/xfail, unrelated-test, and product-code diff
checks over bound before/after bytes; any forbidden result rejects the
candidate with no publication. A high-risk/review-gated proposal additionally
requires the exact audited approval receipt above.

The per-target record operation recomputes these predicates for audit and
emits them in its safety fragment; a mismatch with the precommit receipt is
integrity failure, not a post-commit human-review substitute. The aggregate
safety gate recognizes an exact bound preapproval for the same high-risk
proposal and otherwise cannot pass it. Path authorization alone can never set
`passed=true`. Mutation coverage includes deletion, assertion weakening,
skip/xfail, unrelated/product writes, unapproved high risk, approval drift, and
an approved high-risk positive control.

Target, change, root, and attempt identity come from the bound graph node and
committed attempt, never from agent-supplied identity fields. The
`aa-test-author` persona therefore needs no shell/`aa` permission for either
codegen or fixer completion.

API/E2E codegen skills also remove their contradictory in-agent
`pytest --collect-only` requirement. A declared-only task workspace is not a
standalone Python project/dependency closure, and `uv` parent-project discovery
would violate isolation. Codegen produces tests and a summary; code-owned
execution/inspection nodes perform collection and execution later. No codegen
summary field may claim collection evidence that no graph operation produced.

Codegen writes remain:

| Layer | Private write root | Shared write root | Required change outputs |
|---|---|---|---|
| API | `repo:tests/api/**` | `repo:tests/testdata/**` | `change:codegen/api-codegen-summary.md`, `change:codegen/api-generated-files.json` |
| E2E | `repo:tests/e2e/**` | `repo:tests/testdata/**` | `change:codegen/e2e-codegen-summary.md`, `change:codegen/e2e-generated-files.json` |
| Fuzz | `repo:tests/fuzz/**` | `repo:tests/testdata/**` | `change:codegen/fuzz-codegen-summary.md`, `change:codegen/fuzz-generated-files.json` |
| Performance | `repo:tests/perf/**` | `repo:tests/testdata/**` | `change:codegen/performance-codegen-summary.md`, `change:codegen/performance-generated-files.json` |

The `repo:test-infra` exclusive lock remains on all four codegen contracts.

### 5.4 Harness observation model

The helper returns an immutable test observation rather than an untyped bag:

```python
@dataclass(frozen=True, slots=True)
class AssuranceContractObservation:
    layer: LayerName
    authoring_review: PlanReviewAuthoring
    frozen_review: PlanReview
    applicability: LayerApplicability
    checks: PlanCheckDocument
    plan_gate_verdict: str
    codegen_precondition_verdict: str
    visible_codegen_inputs: tuple[str, ...]
    frozen_output_digests: tuple[tuple[str, str], ...]
    generated_files_manifest_sha256: str
    candidate_validation_receipt_id: str
```

The helper invokes production loaders, models, operations, and gate evaluator.
It does not duplicate check implementations or verdict precedence.

Every artifact round trip serializes the validated model to canonical bytes,
reloads those bytes through the registry-selected runtime model, serializes a
second time, and requires byte equality. This catches aliases/defaults/order
drift that a one-way `model_validate(...)` assertion would miss.

## 6. Full Round-Trip Contract

### 6.1 Authoring-to-runtime review boundary

For every layer, the fixture review is first parsed through the registry's
`authoring_model`. The frozen artifact path then parses the emitted bytes
through the runtime `PlanReview` model. The test proves:

- `review_type`, `change_id`, `auto_fix_plan`, `next_action`, and every finding
  ID survive the boundary;
- human-only Fuzz/Performance reviews cannot authorize an automatic fix;
- the frozen artifact has the exact change/layer identity consumed by the
  gate; and
- extra authoring-only ambiguity cannot be erased into a pass-shaped runtime
  artifact.

This is an artifact-registry round trip, not a direct constructor-to-
constructor conversion.

### 6.2 Frozen workspace boundary

The harness creates a real task workspace from the execution contract. It
opens each required input through the workspace API and records the logical
path that was visible. It then freezes the canonical skill output and verifies
the frozen write set against both `writes` and `authorization_writes`.

The following are distinct failures:

- `required_input_missing`: the fixture lacks a structurally required input;
- `contract_read_missing`: the input exists in the repository but is not
  visible through the task contract;
- `artifact_ingest_invalid`: output bytes do not satisfy the registered model;
- `synchronized_write_undeclared`: a synchronized output falls outside the
  declared contract prefix; and
- `authorization_write_forbidden`: an attempted output is visible but not
  authorized for the role.

Tests assert the exact failure class/message code already used by the owning
runtime boundary. The harness does not translate these into a generic
`round_trip_failed` result.

### 6.3 Mechanical and gate boundary

The harness derives applicability from the real case documents, runs
`verify-plan-mechanical` with `require_review=True`, freezes the resulting
`PlanCheckDocument`, and reloads it from canonical JSON. It then evaluates the
actual layer plan gate with the fixture review, checks, L1, and current policy.

The plan gate must report reads bound to the frozen review/check/L1 digests.
The codegen precondition is evaluated against a committed child-cycle result
and those bound artifacts. A pass requires the exact current child success,
not merely files with matching shapes on disk.

### 6.4 Codegen visibility boundary

Finally, the harness constructs the codegen `TaskWorkspace` from the real
contract and proves every structured required input can be read. It also
proves that an undeclared input from a sibling layer and an artifact belonging
to another change cannot be read.

The deterministic codegen adapter then emits one mapped behavioral test, its
human summary, and the layer's strict D16 manifest. Production ingest,
`freeze_write_set`, and `generated_files_candidate/v1` run in order. The
observation records the committed summary/manifest/test digests and validation
receipt, proving that visibility, output shape, and cross-artifact authority
close before success.

This final step catches the current class of defect where all artifact models
and gates pass but the agent cannot see a plan summary or case that its skill
requires.

## 7. Contract and Artifact Mutation Matrix

### 7.1 Mutation ownership

Each mutation fixture declares:

```python
@dataclass(frozen=True, slots=True)
class AssuranceMutationExpectation:
    layer: LayerName
    boundary: Literal[
        "skill_contract",
        "workspace_read",
        "authoring_ingest",
        "runtime_ingest",
        "applicability",
        "mechanical",
        "plan_gate",
        "codegen_precondition",
        "precommit_validator",
        "adapter_dispatch",
        "workspace_write",
        "eval_write_policy",
        "evidence_integrity",
        "legacy_supersede",
    ]
    reason_code: str
    locator: str
```

The runner applies exactly one structured mutation to a canonical bundle,
runs through the expected boundary, and asserts:

1. every preceding boundary succeeds;
2. the declared owner rejects the mutation with the declared reason/locator;
3. no later boundary is invoked; and
4. independent checks retain their canonical statuses.

### 7.2 Skill/contract mutations

The minimum matrix covers all four layers and every applicable role class:

| Mutation | Expected owner |
|---|---|
| Remove or corrupt the real `## Inputs` heading | `skill_contract` |
| Declare a required summary/case/config input but remove its contract read | `skill_contract` parity guard |
| Remove the required input from the fixture while retaining the contract read | `workspace_read` |
| Ask `TaskWorkspace.open(...)` for a sibling-layer path without changing the contract | `workspace_read` |
| Add a sibling-layer read to the contract without declaring it in skill Inputs | `skill_contract` reverse-parity guard |
| Add a broad `repo:**`, `tests/**`, or `qa/changes/**` contract scope | `skill_contract` parity guard |
| Add an exact but undeclared read such as `repo:.env` or `repo:src/x.py` | `skill_contract` reverse-parity guard |
| Add an exact undeclared write/authorization such as `repo:src/x.py` | `skill_contract` reverse-parity guard |
| Write a differently named/sibling knowledge proposal | `workspace_write` |
| Attempt a sibling private-root write inside `TaskWorkspace` | `workspace_write` |
| Inject the same sibling path out-of-band into the eval SUT | `eval_write_policy` |
| Attempt another change/product/`.aa/memory/**` write in `TaskWorkspace` | `workspace_write` |
| Inject the same forbidden class out-of-band into the eval SUT | `eval_write_policy` |
| Restore `tests/factories/**` in a plan mapping | `mechanical` |
| Restore a prompt-owned workflow-state write | `skill_contract` |
| Restore a plan-fixer `events.jsonl` input | `skill_contract` |
| Route any of the sixteen targets to the wrong/default persona | `adapter_dispatch` |
| Remove persona external-directory denial or broaden its edit floor | `skill_contract` packaging guard |
| Omit/fallback/rewrite the attempt directory on an OpenCode request | `adapter_dispatch` |

The broad-scope guard is intentional. A fixture must be repaired by aligning
specific reads and outputs, not by weakening isolation.

Positive controls are kept outside the single-owner mutation table: the exact
conditional API/E2E knowledge proposal, the current layer private test root,
and `tests/testdata/domain/**` from a selected codegen layer must pass both
workspace and eval-policy checks.

The missing/forbidden/shared-factory distinction is asserted in
`reason_code`; it is not encoded as a second owner.

### 7.3 Artifact mutations

The round-trip suite retains and strengthens existing useful mutations:

- missing or extra authoring fields;
- blank finding IDs or unresolved `auto_fix_plan` references;
- wrong `review_type`, change ID, or layer;
- missing exact plan artifact;
- malformed `automation.required`, never treated as false;
- missing/rewritten Factory Mapping;
- wrong L1 path or missing capability leaf;
- broken assert-ideal case coverage for API/E2E;
- an unexpected assert-ideal result on Fuzz/Performance rather than static
  N/A;
- dropped, duplicated, or unknown mechanical check entries;
- reordered mechanical checks, which remain semantically valid while the
  canonical producer still emits registry order;
- stale review/check bytes with otherwise valid models; and
- a valid child result from another invocation or definition epoch;
- malformed/extra/duplicate/cross-layer D16 generated-file manifest fields;
- a shape-valid manifest with an omitted/extra write, wrong disposition/digest,
  unbound reuse, or plan/case mismatch, owned by `precommit_validator`;
- missing/stale/wrong-mode/target/review/tree/human-decision plan-fixer Runtime
  Context, owned before `adapter_dispatch`;
- malformed/tampered/custom-root/archive-only D17 config/location evidence,
  owned by `evidence_integrity`; and
- ineligible, conflicting, wrong-subtree, or reused D18 replacement authority,
  owned by `legacy_supersede`.

For a mechanical mutation, the named check alone changes to `fail`; all other
applicable checks keep their canonical status. The document-level status and
finding IDs are asserted as well as the individual check.

The reordered-check case is an explicit positive control, not a failure-owner
case: it must preserve semantic validation, while a freshly produced document
still uses registry order.

## 8. Current Packaged Topology Conformance

### 8.1 Role discovery

The current conformance validator discovers roles from production metadata:

- the assurance parent uses `graph:<layer>-branch` for the selected layer;
- the branch owns the layer plan skill, cycle subgraph call, codegen
  precondition gate, and codegen skill;
- the cycle owns applicability, reviewer, reviewed mechanical producer,
  explicit plan gate, and the layer-specific remediation nodes; and
- gate reads and artifact paths come from `LayerAssuranceProfile`.

The validator requires exactly one node for each critical role. A second
candidate is an ambiguity failure, not a reason to select the first.

This strict validator is selected by definition origin, not by byte
similarity: only `load_workflow_v2_with_origin(...).origin == "packaged"`
flows through `compile_packaged_workflow(...)`. Project/explicit custom graphs
retain generic compilation, and a pinned snapshot always uses historical
compilation even when its bytes happen to equal the current package.

### 8.2 Selection and run-mode predicates

For the current release, the assurance parent selection must be semantically
equivalent to:

```text
'<layer>' in params.test_types
and params.run_mode in <the layer's supported modes>
```

`codegen-only` must activate every selected layer and must not activate an
unselected one. API/E2E preserve their supported `api-only`/`e2e-only` modes;
Fuzz/Performance do not gain new run modes.

Within a branch:

- codegen-only never invokes the plan skill;
- full-mode behavior preserves the existing API/E2E versus
  Fuzz/Performance parent-preflight distinction;
- codegen may be selected only for the layer's generation modes; and
- plan-only/review-plan cannot reach codegen.

Equivalence is decided over a closed release domain, not by pretty-printing:
all declared run modes, all sixteen layer-selection subsets (including the
invalid empty set as a negative probe), applicability true/false, and every
declared route/gate verdict. For each assignment the actual and required
expressions must produce the same boolean/target. Parse errors, undeclared
params, unknown builtins, or values outside the enumerated schema domain are
structured mismatches rather than `false` defaults.

### 8.3 Exact safety chain

Every applicable codegen path must cross this chain in order:

```text
applicability -> review -> mechanical -> explicit plan gate
             -> child cycle success -> codegen precondition -> codegen
```

For Fuzz/Performance, the parent cases-only preflight precedes the optional
plan node and cycle. For API/E2E, applicability remains inside the existing
cycle. This design does not force their full-mode parent topology to match.

The validator computes reachability and dominance over ordinary edges and
route outcomes. Removing the plan gate or codegen precondition must make
codegen unreachable; otherwise a forbidden bypass exists. No ordinary edge or
route case other than the precondition's pass case may enter codegen.

### 8.4 Gate and route obligations

The plan gate must:

- read the profile review artifact through the profile alias;
- read the profile checks artifact through the derived alias;
- read `.aa/data-knowledge.yaml` through `data_knowledge`;
- retain the established first-true verdict precedence; and
- route `pass`, `skip`, `needs_fix`, `knowledge_remediation`,
  `needs_human_review`, `reject`, and `stop` to their exact current targets.

The codegen precondition must:

- read current review/check/L1 evidence;
- read the current cycle result through the exact selector;
- skip only for current inapplicable evidence;
- stop on missing, malformed, stale, wrong-layer, wrong-change, or uncommitted
  producer evidence;
- pass only on current committed plan-gate success and capability readiness;
  and
- route only pass to codegen; skip/stop terminate the branch without codegen.

### 8.5 Interrupt and remediation obligations

Human and knowledge interrupts are audited state transitions. Current
conformance verifies:

- the exact checkpoint name;
- non-`none` audited binding;
- the allowed actions;
- the exact manual revision allowlist;
- the source gate/revision epoch binding; and
- the return node after each action.

API/E2E automatic fixers return to review. Fuzz/Performance
`fix_and_proceed` returns to review after an audited plan tree change.
Knowledge remediation returns to mechanical so checks and the plan gate
consume the promoted L1 without unnecessarily rerunning the reviewer.

### 8.6 Generation join obligation

The assurance `generation-join` retains `all_active` semantics. It cannot
release execution after only one active layer finishes. The current validator
checks the join mode, `cancel_remaining == false`, the exact four potential
branch inputs, and the guarded edges from the join to execution/END. A direct
active-branch-to-execution edge is a forbidden bypass.

### 8.7 Required current mutation corpus

At minimum, mutate the decoded packaged schema to prove rejection of:

- assurance selection without the layer test-type predicate;
- assurance selection using the wrong layer, wrong run-mode set, an extra
  unknown param/builtin, or an expression that differs on one closed-domain
  truth-table row;
- a broadened or unconditional parent preflight edge;
- review-cycle directly to codegen;
- skip directly to codegen;
- plan directly to codegen;
- codegen enabled only or additionally in plan-only/review-plan;
- codegen precondition reading `review-cycle.status` instead of its bound
  current child result;
- missing review/check/L1 gate reads or wrong aliases;
- plan gate `invalid_json`/missing evidence changed from stop, or removal/
  weakening of its invalid, N/A-skip, capability, review-decision, readiness,
  policy-check, reject, or pass atom, including any order change that alters
  first-true verdict precedence on at least one closed-domain row;
- precondition removal/weakening of current child success,
  applicable/invalid/N/A state, plan-gate pass, L1 presence, or independent
  `capabilities_present(...)` atoms (including the API/E2E capability atom
  added by this increment);
- any gate/precondition expression that makes malformed/missing/unknown input
  reach skip/pass, or any default other than fail-closed stop;
- plan-gate pass routed to human review;
- precondition pass/skip/stop routed to the wrong target;
- duplicate reviewer, mechanical, gate, precheck, or codegen owner;
- automatic fixer returning anywhere other than review;
- knowledge remediation returning anywhere other than mechanical;
- `bind: none`, wrong checkpoint, wrong actions, or a broadened revision
  allowlist;
- generation join changed from `all_active`, `cancel_remaining` enabled,
  missing/adding a source, misrouting a guarded successor, or bypassed by a
  direct edge; and
- a current layer with only a subset of the chain.

Mutations are performed with Pydantic `model_copy` on decoded schema objects.
Tests do not edit YAML with text replacement and do not depend on source
formatting.

Positive controls reorder operands wholly inside a commutative `and`/`or`
subexpression while preserving the closed-domain truth table. They must pass;
only semantic/precedence changes are rejection cases.

### 8.8 Current healing safety conformance

The separate current healing validator from D3 proves that
`fixer-authority-ready` and `fixer-proposal-approval` dominate both fixer
agents; no edge/route can enter a fixer from allocate, proposal, interrupt, or
another recovery node while bypassing either gate. The approval gate reads the
exact proposal, authority, entry-baseline, policy, and optional graph-owned
approval receipt. Low risk may pass directly; high-risk/`needs_review` can only
reach the audited interrupt, and stop reaches `complete-failed`.

The interrupt must use checkpoint `healing.fixer_approval`, an audited source-
gate binding and exact actions `approve_and_apply | stop`. Approved resume
must plan `record-fixer-approval`, whose committed/acknowledged successor is
the approval gate; stop terminates. Its receipt model/event/projection identity
is exactly section 5.3. Approval pass has the single route target
`fixer-dispatch`; only its two ordinary proposal-guarded edges may activate
fixers. Both fixer contracts select the one registered
`codegen_fix_candidate/v1` precommit validator. Each fixer flows only through
its target record operation; the `all_active` join sources are the record
nodes, then one combine operation owns the aggregate consumed by the existing
safety gate. Direct fixer-to-join/safety/rerun edges are bypasses.

Decoded current-schema mutations cover:

- removing, weakening, or bypassing authority or approval dominance;
- treating missing/malformed/stale approval as pass, or allowing high risk to
  pass without an exact receipt;
- changing checkpoint/bind/action set, accepting approval for another root,
  tree, proposal, authority, policy, target/path set, or routing approved resume
  anywhere except record operation -> approval gate;
- routing approval pass directly to multiple targets, removing/duplicating
  dispatch, weakening its target guards, or adding an unguarded successor;
- removing/changing the precommit validator, selecting an unknown validator,
  or permitting a fixer success without a bound validation receipt;
- routing a fixer directly to join/safety/rerun, joining fixer rather than
  record nodes, requiring an inactive target output, or letting combine/gate
  consume stale/per-target evidence; and
- changing record/combine hard outputs or durable-effect kinds.

Each mutation fails packaged compilation with a stable
`healing_conformance` code/owner/locator. Positive controls cover low-risk
direct pass, exact approved high risk, API-only, E2E-only, and both-active
record joins.

## 9. Historical Pinned Safety Classification

### 9.1 Compatibility boundary

Current conformance findings never feed directly into historical replay.
Historical compilation continues to validate generic graph syntax,
references, expressions, contracts, and replay-safe dependencies using the
pinned graph/contract/catalog bundle.

`validate_historical_replay_surface(...)` may validate params-only predicates
and gate dependency safety, but it must not require current graph/node IDs such
as `assurance`, `api`, or `<layer>-plan-cycle`. Its role lookup shares the
historical semantic discovery seam, so a safe renamed graph reaches the
classifier instead of failing compilation first.

Discovery produces one immutable manifest rather than letting each consumer
rediscover names independently:

```python
@dataclass(frozen=True, slots=True)
class DiscoveredHistoricalLayerRoles:
    layer: LayerName
    selection_event_node_id: str
    branch_call_node_id: str
    branch_graph_id: str
    cycle_call_node_id: str
    cycle_graph_id: str
    applicability_node_id: str
    reviewer_node_id: str
    mechanical_node_id: str
    plan_gate_node_id: str
    precondition_node_id: str
    codegen_node_id: str
    artifact_aliases: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class DiscoveredHistoricalAssuranceRoles:
    root_graph_id: str
    assurance_call_node_id: str
    assurance_graph_id: str
    layers: tuple[DiscoveredHistoricalLayerRoles, ...]
    canonical_digest: str
```

The manifest is derived from pinned graph references, operation/skill
contracts, artifacts, aliases, routes, and dependencies; it is not populated
from current IDs. `validate_historical_replay_surface(...)`, historical
selection validation/evaluation, `_bind_assurance_invocation(...)`, replay and
trace binding, the v4/v5 audit trigger, and the v6 classifier all consume this
same object. A consumer may project fields from it but may not run a second
name-based discovery pass. Missing/duplicate roles, ambiguous aliases, or a
role that cannot be connected to one branch/cycle lineage returns a structured
discovery issue and fails closed at that consumer's boundary.

Layer replay dispatches the historical classifier from the root binding. A
v6-bound graph may be safely `wired` even when its node names, edge ordering,
or equivalent predicate AST differs from the current release. A v5 result is
reported under `legacy_v5_unbound` and preserves its historical status,
including known gaps; it is compatibility evidence, not v6 safety proof.

### 9.2 V6 semantic safety obligations

A v6 historically wired layer must prove from the pinned schema:

- one unambiguous selected layer branch and cycle lineage;
- deterministic applicability;
- review before the mechanical producer on the applicable path;
- explicit plan-gate ownership and frozen evidence reads;
- no path from cycle/branch entry to codegen that avoids the current-in-that-
  invocation precondition;
- no skip/stop/recovery route into codegen; and
- an audited remediation return that regenerates the evidence it can change.

These are graph safety properties, not current node-name equality.

Historical predicate evaluation uses a finite truth table over the values
declared by the pinned schema. It asks a one-sided safety question: whenever
the old graph can activate/reach codegen, is the complete evidence chain
unavoidable? Equivalent expressions and additional hardening that only removes
activation cases are safe. Any assignment that creates a false-positive
activation/bypass is `partial`; an unknown root/builtin, unbounded value
domain referenced by the expression, or unevaluable expression is also
`partial`, never an implicit pass. Unrelated pinned params such as `retro_id`
are outside the expression dependency closure and do not affect the table.

Activation classification is explicit: no assurance-chain role markers is
`legacy_unwired`; at least one unambiguous activation with all reachable
codegen paths safe is `wired`; markers with no provably safe activation, mixed
safe/unsafe activation, or ambiguous roles is `partial`.

Whether an invocation actually produced committed applicability/mechanical/
gate/codegen attempts is not a schema property. Replay binding and the
current-chain scorer prove that separately from ledger events and write sets.

### 9.3 Classification mutation matrix

V6 tests pin three graph families:

1. zero-marker legacy graphs, which remain `legacy_unwired`;
2. older safe wired graphs with renamed nodes and normalized-equivalent
   predicates, which remain `wired`; and
3. partially activated or bypassable graphs, which are `partial` with a
   stable safety diagnostic.

The same dangerous bypass mutation rejected by current compilation must make
an already-activated v6 historical graph `partial`, while a harmless rename
must not. Separately, golden v5 fixtures freeze their existing status for the
same bytes—even where a known v5 false negative differs from v6—and assert
`semantics_bound == false`. A test that merely calls the newest classifier on
old bytes is insufficient replay coverage.

The historical corpus makes the semantic boundary explicit:

| Mutation | Expected result |
|---|---|
| Rename every graph/node role and alias consistently | `wired`; compile, selection, bind, trace, and classification use the same discovered manifest |
| Reorder a commutative predicate or replace it with a truth-table-equivalent form | `wired` |
| Add a condition that only removes activation cases | `wired` when at least one safe activation remains |
| Reference an unknown builtin or a parameter with no finite pinned domain | `partial` |
| Duplicate a critical role or make an alias resolve to two producers | `partial`/replay authorization failure with structured ambiguity |
| Keep one safe path and add one direct/skip/recovery codegen bypass | `partial` |
| Remove all assurance markers and test-write authorities | `legacy_unwired` |

Every rename positive control drives historical compilation through selection
and `_bind_assurance_invocation(...)`; a classifier-only pass is insufficient.
Every negative control asserts both the stable discovery/safety diagnostic and
that definition-dependent planning cannot reach codegen.

Live-resume fixtures additionally cover safe and bypassed v4 and v5 roots.
V4 passes only when its recorded compatibility digests allow an exact profile
reconstruction; an unbound/unreconstructable profile blocks. For both epochs,
the v6-discovered audit trigger fires even when the frozen display classifier
says `legacy_unwired` or `wired`, and the bypass fixture cannot append/reuse a
safe receipt.

## 10. Multi-Layer Codegen-Only Semantics

### 10.1 One normalization boundary

Introduce one shared selection normalizer for eval/CLI-facing values. It
accepts the existing single string, comma-separated string, or list form at
the boundary and returns a canonical tuple of `LayerName` values. The graph
still receives the validated list parameter defined by the workflow schema.

Normalization rules are:

- missing input -> `("api", "e2e")`;
- empty input -> error;
- both legacy `test_type` and `test_types` keys present -> error;
- unknown layer -> error;
- duplicate layer -> error; and
- output order -> `api`, `e2e`, `fuzz`, `performance`, regardless of caller
  order.

The eval executor resolves the selection once. The same resolved tuple is
used for runtime params, fixture import validation, write-policy construction,
execution evidence, and scorer expectations. Passing the raw unresolved
value to any of those consumers is forbidden.

`eval/runner.py` passes the raw YAML scalar/list to the normalizer before any
`str(...)` conversion; executor and runner typing accept that boundary object
or the already-resolved tuple. Here “CLI” means the `aa eval run` suite-loading
path. The generic workflow CLI keeps its existing validated `--params`
behavior and receives a normal list.

Runner selection is based on key presence, never truthiness: an explicitly
empty string/list reaches the normalizer and fails before fixture seeding or
the before manifest, rather than being mistaken for omitted input and gaining
the API/E2E default.

Pure normalizer/policy tests exhaust all fifteen non-empty layer subsets and
the reverse order of each, requiring the same canonical tuple and policy
bytes. Boundary tests cover omitted, single scalar, comma scalar, YAML list,
empty, duplicate, and unknown forms. A real `aa eval run` test with a temporary
list-form suite asserts `execution.json`, persisted policy, fixture validation,
and runtime params all contain the same tuple.

### 10.2 Derived write-policy union

For `codegen-only`, construct the allowlist as:

```text
resolved current change root/**
qa/.graph-runtime/locks/**
qa/.graph-runtime/publications/**
tests/testdata
tests/testdata/**
<private root for each selected layer>
<private root for each selected layer>/**
```

The layer-to-private-root mapping is:

```python
{
    "api": "tests/api",
    "e2e": "tests/e2e",
    "fuzz": "tests/fuzz",
    "performance": "tests/perf",
}
```

The current change root is resolved through the repository's change-location
authority and then converted to a safe repository-relative prefix. It is not
constructed from an unchecked string and never becomes `qa/changes/**`.

The persisted `write-policy.json` contains the resolved canonical patterns so
scorer replay does not need to reinterpret defaults.

The attempt/evidence directory is outside `attempt_sut`; therefore
`eval/out/runs/**` inside the scanned SUT is not scanner output and is not
allowlisted.

### 10.3 Policy mutation matrix

For each selected layer, assert its private root and shared testdata are
allowed. For each unselected layer, assert its private root is forbidden.
Also cover:

- a two-layer API/E2E selection;
- a nonadjacent API/Fuzz/Performance selection;
- all four layers;
- another change under the normal change root;
- a non-default configured changes root, changes/archive coexistence selecting
  changes, and archive-only/path-escape resolution failing before policy;
- product source, `.aa/memory/**`, and unrelated graph-runtime paths;
- allowed locks/publications versus forbidden arbitrary runtime metadata;
- any `eval/out/runs/**` write inside the attempt SUT, which is forbidden;
- renames and deletions in the symmetric content-manifest diff (with porcelain
  retained only as diagnostic context); and
- modifying bytes, mode, file kind, or symlink target of a clean tracked,
  already dirty/untracked, or ignored path;
- creating a new ignored file under a forbidden root;
- leaving a pre-existing dirty path byte-identical, which must not be
  attributed to the attempt;
- codegen summary/manifest outputs under the current change versus the same
  paths under a different change.

The graph contract and write scanner must agree on every selected/sibling/
shared case.

## 11. Real GraphRuntime Codegen-Only Matrix

### 11.1 Runtime harness

The integration harness loads the packaged workflow and contract catalog,
builds the production runtime services, and seeds a real change tree. It uses
the normal import-checkpoint path for plan-ready predecessors and then drives
entrypoint `execute` with `run_mode="codegen-only"`, the resolved selected
tuple, and `run_tests=false`. The base/recovery matrix therefore ends through
the join-to-END edge and asserts no `operation:run-tests` attempt. Execution
behavior is covered by its existing suites, not smuggled into this assurance
matrix through the schema's `run_tests=true` default.

The selected assurance chain is never imported. Imported tasks may establish
bootstrap or plan-ready predecessor closure, but reviewer, mechanical, plan
gate, codegen precondition, codegen, and the selected assurance branch itself
must remain pending before the run.

A deterministic adapter maps `(target, layer, attempt number)` to an output
fixture. The adapter records every invocation and refuses an unexpected target
so a silently skipped or newly introduced agent task fails the test.

### 11.2 Four-by-two base matrix

Run every layer in both applicability states:

| Layer state | Plan | Reviewer | Mechanical | Plan gate | Precheck | Codegen |
|---|---:|---:|---:|---:|---:|---:|
| applicable | absent | current success | current 4-check evidence | pass | pass | current success |
| inapplicable | absent | absent | current 4×N/A evidence | skip | skip | absent |

For Fuzz/Performance, `assert_ideal` is statically N/A inside the applicable
row while the other three checks pass. For a dynamically inapplicable layer,
all four checks are N/A.

Every applicable cell asserts:

- exactly one cycle applicability attempt for every layer, plus the existing
  branch preflight attempt for Fuzz/Performance;
- one physical reviewer attempt;
- one physical mechanical attempt whose output digest matches the gate read;
- one committed plan-gate attempt;
- one committed codegen-precondition attempt bound to the current child;
- one physical codegen attempt whose `outputs_sha256` binds the summary and
  D16 manifest, whose validation receipt reconciles the input/mapping/write
  set, and whose write set contains a behavior-bearing selected-layer test
  add/modify;
- no physical plan attempt; and
- terminal completion only after the branch output is committed.

Every inapplicable cell asserts:

- the same exact applicability-attempt ownership/count as the layer's
  applicable cell and one current mechanical attempt;
- four current N/A check entries;
- plan and codegen preconditions with `skip` verdicts;
- no reviewer, plan, codegen, fixer, or manual-revision attempt; and
- terminal completion without a generated layer test.

Malformed case YAML or a non-boolean `automation.required` is an applicability
error and never emits N/A. A fully valid case set containing only another
layer, or no selected automated case, is the dynamic inapplicable row and
emits exactly four current `layer_not_applicable` entries.

Attempt counts are computed from the recorded root invocation and its nested
`parent_invocation_id`/structural-path lineage. The harness joins
`graph_invocation_started`, task-attempt, and committed-superstep events by
IDs; it never counts a convenient node name from another root.

### 11.3 Stale-artifact matrix

Before each run, seed pass-shaped review, checks, gate-looking data, and
codegen summary/manifest bytes from an unrelated invocation. Run these exact
negative controls:

- the reviewer adapter returns success but writes no review: the task fails
  `invalid_output`/missing output, and mechanical/gate/codegen never run;
- codegen writes a fresh selected test but omits either summary or manifest:
  the task fails output validation and receives zero current-codegen/test-write
  credit;
- codegen writes both hard outputs but the manifest omits/misclassifies the
  test: `generated_files_candidate/v1` rejects before success;
- the mechanical operation fails or produces no committed checks while stale
  checks remain on disk: the plan gate does not accept them; and
- a forged `task_imported` record or fixture import for a selected assurance
  role is rejected before execution.

The positive control makes the current producer rewrite byte-identical
canonical review/check/summary/manifest content. It passes only because the
current attempt owns the frozen digest, input snapshot, validation receipt,
and write set, proving freshness is authority-based rather than byte-novelty-
based.

The assertions are ledger-first:

- a file digest is accepted only when a committed current producer emitted it;
- a gate read digest must point to that producer's output;
- a `task_imported` record for a selected assurance node is rejected by the
  fixture/import validator;
- a prior root or sibling invocation cannot satisfy the current chain; and
- identical bytes still require a current physical producer attempt.

This makes freshness independent of byte novelty. Reproducing identical
canonical bytes is valid; reusing old authority is not.

### 11.4 Multi-layer activation and join

Add representative multi-layer runs:

1. API + E2E, both applicable;
2. API + Fuzz + Performance with a mix of applicable and inapplicable layers;
3. all four applicable; and
4. one selected layer with all other layers unselected.

The runtime may execute independent active branches in parallel waves. The
test asserts only partial ordering required by safety, not a total order
between siblings. `generation-join` starts exactly once and only after every
selected branch has reached its terminal output. A blocking adapter holds the
last active codegen. For an inapplicable branch, which has no agent call, a
test-only `BarrierNodeRunner` wraps the production runner and pauses the target
operation/gate result before scheduler commit. Both prove that the join has not
started; releasing the barrier allows the branch to settle, then the join, and
only then its guarded successor. No run-tests/execution successor is planned
after the first sibling alone.

Unselected layers have no branch child invocation or task attempt, selection
evidence says false, and no assurance artifact is attributed to the current
root. Their parent source may be represented only by the planner's inactive/
skipped bookkeeping required by `all_active`; that bookkeeping is not a child
execution.

## 12. Resume and Recovery Matrix

### 12.1 Ordinary restart seams

Durable interruption reuses `tests/integration/_graph_fault_worker.py` and its
subprocess/SIGKILL protocol. Extend its builder with a packaged-four-layer
scenario and a target selector `(structural_path, node_id, occurrence)`. The
worker wraps the existing journal/scheduler/workspace persistence seams; it
never hand-writes success/commit events and no production route special case is
added.

The named cuts are:

| Scenario | Required cut IDs |
|---|---|
| Before/inside task result | `before_attempt_started`, `after_attempt_started`, `handler_before_success` |
| Declared input snapshot | `snapshot_created_before_started`, `started_with_snapshot_before_handler` |
| Candidate validation | `candidate_after_freeze_before_validate`, `candidate_after_validate_before_success`, `candidate_validation_rejected` |
| Successful result before wave commit | `target_success_before_commit` |
| Target committed boundary | `target_superstep_committed` |
| Tree/materialization | `tree_pointer_superstep`, `canonical_materialization`, `checkpoint_snapshot_write` |
| Synchronized publication | `sync_apply_pending`, `sync_ack_pending` |
| Manual revision | `revision_target_objects`, `manual_plan_revision_append`, `graph_resumed_ordinal_<n>` |
| Fixer approval | `fixer_approval_after_resume_before_operation`, `fixer_approval_before_success_line`, `fixer_approval_after_success_before_superstep_commit`, `fixer_approval_after_superstep_commit_before_domain_event`, `fixer_approval_after_domain_event_before_ack`, `fixer_approval_after_ack_before_gate` |
| Child wrapper | `child_started_before_wrapper_success`, `child_pending_before_wrapper_success` |
| Healing allocation | `allocate_before_success_line`, `allocate_after_success_before_superstep_commit`, `allocate_after_superstep_commit_before_domain_event`, `allocate_after_domain_event_before_ack`, `allocate_after_ack_before_successor` |
| Healing record | `heal_record_before_success_line`, `heal_record_after_success_before_superstep_commit`, `heal_record_after_superstep_commit_before_domain_event`, `heal_record_after_domain_event_before_ack`, `heal_record_after_ack_before_successor` |

`target_*` and child IDs are test-worker extensions parameterized by the
selector, not new production environment switches. A non-crash
`BarrierNodeRunner` is used only for join ordering; durable recovery assertions
always kill the subprocess and construct a fresh runtime.

At `snapshot_created_before_started`, recovery observes no running attempt,
cleans the unreachable task workspace/CAS reference, and retries with the same
attempt number. At `started_with_snapshot_before_handler`, the referenced
snapshot already exists, validates, and is reused by that attempt; recovery
never fabricates it from a later tree.

For each layer, stop and reconstruct the runtime after these committed
boundaries:

- reviewer;
- mechanical producer;
- plan gate; and
- codegen precondition.

Resume uses the same root invocation. Already committed nodes are not
physically reinvoked; the next pending node runs once. The final gate/codegen
bindings still point to the committed current attempts before the restart.

Also cover the ordinary publication windows already supported by the runtime:

- `task_attempt_started` with no succeeded event;
- frozen candidate/write-set bytes with no succeeded event;
- `task_attempt_succeeded` with no committed superstep;
- committed superstep with a pending ordinary apply; and
- applied write set with final acknowledgement/continuation still pending.

With no succeeded event the handler may run in a new attempt; the guarantee is
one committed success/output, not exactly-once external adapter side effects.
Once `task_attempt_succeeded` exists, recovery commits or repairs the
ledger-proven frozen write set and does not reinvoke that successful handler.

For a graph-wrapper task, restart first resolves an already-created child by
`parent_invocation_id`/`parent_task_id` and resumes that child. It must not
start a second child or exhaust the wrapper's `max_attempts=1` merely because
the process died while the child was pending.

### 12.2 API/E2E automatic fixer

For API and E2E, a deterministic first review/gate cycle returns `needs_fix`.
The automatic fixer changes an allowed plan artifact and returns to review.
The second cycle must produce new reviewer, mechanical, and gate attempts
bound to the new tree before precheck/codegen.

Cover restart:

- before the fixer attempt;
- after fixer success but before frozen write-set commit;
- after fixer apply but before review is replanned; and
- after the second mechanical producer but before the second gate.

No first-epoch pass-shaped check or gate result may authorize codegen after
the plan tree changes.

### 12.3 Fuzz/Performance manual revision

For Fuzz and Performance, `needs_fix` reaches the real human interrupt. Resume
with `fix_and_proceed` supplies an exact audited plan revision under the
declared allowlist. The runtime records the revision transition, commits a new
tree, and returns to review.

The new review, mechanical evidence, and plan gate must all belong to the new
tree before precheck/codegen. No-op revisions, extra paths, conflicting base
digests, reused transition IDs, wrong source-gate attempts, and sibling-layer
plan edits fail closed.

Cover every named manual-revision cut in section 12.1
(`revision_target_objects`, `manual_plan_revision_append`, and the applicable
`graph_resumed_ordinal_<n>`) so recovery cannot apply a revision twice or lose
the audited source binding.

### 12.4 Knowledge remediation

For all four layers, a knowledge-remediation verdict reaches the audited
knowledge checkpoint. Promotion changes only the allowed L1 path and records
the expected source gate/tree. Resume returns to mechanical, then the plan
gate, without rerunning the reviewer.

The new mechanical and gate attempts must bind the promoted L1 digest.
Codegen is blocked when promotion is absent, no-op, stale, or affects an
unrelated live path.

### 12.5 Accept-risk and stop

`accept_risk` is not a generic bypass. It remains valid only when:

- the interrupt declares the action;
- the resume command is bound to the audited interrupt/source gate;
- no manual revision is smuggled into a non-revision action;
- mandatory capability/codegen preconditions still pass; and
- the resulting route is one declared by the pinned graph.

`stop` records terminal stop and never plans precheck or codegen. Tests assert
both paths from the real packaged interrupt definitions.

Two packaged negative controls resume with an otherwise valid accepted-risk
override: one removes a required capability, and one supplies stale/wrong-tree
review/check/L1 evidence. Both reach precheck `stop` with no codegen attempt;
the interrupt decision cannot override these hard preconditions.

### 12.6 Pinned definition resume

Start the invocation under a pinned graph/contract/catalog bundle, stop at a
resume seam, then expose a newer packaged release. Resume resolves the exact
pinned execution bundle and continues its old topology when gate semantics,
assurance profile, topology-safety semantics, runtime-commit-safety semantics,
and ingest model schemas remain compatible.

The test asserts:

- graph, contract, and catalog digests equal the root invocation's pinned
  request;
- gate/profile/topology/commit-safety semantics digests and staged object bytes
  equal the root and every child invocation binding;
- current packaged identities are not substituted;
- imported predecessors remain imported and have no physical attempt;
- newly pending nodes run through current handler code only when compatible
  with the pinned catalog; and
- no new node is injected into the old topology.

Separate negative cases retain the fail-closed boundary for gate semantics,
assurance profile, topology semantics, commit-safety semantics, and ingest
models before definition-dependent recovery or planning. Pending-fixer and
already-validated-receipt resumes both reject a changed validator/reconciler
manifest. A v4/v5 root keeps its frozen display classifier. D10's v6-bound
topology audit remains necessary, but is not a commit-safety binding: a root
with remaining assurance codegen/healing work stops before dispatch with
`legacy_commit_safety_semantics_unbound`. A v4/v5 root with no remaining
validator/effect-bearing assurance work may still finish its already-safe
report/terminal path under existing compatibility rules. Regression tests
cover safe report-only continuation, pending codegen, pending fixer, and a
pre-fixer compatibility stop. The diagnostic directs the operator to D18's
audited `rerun-v6` or `stop` transition; resume never synthesizes a binding for
the old root. The supersede crash/retry matrix proves the old root cannot wedge
the active guard and `restart: once` permits only the single authorized
replacement.

### 12.7 Codegen-fixer record and safety aggregation

Drive the packaged healing graph in API-only, E2E-only, and both-active modes.
Each active agent writes one valid intent and an exact authorized test change;
each active record operation first commits its summary/safety fragment plus
inline effect, then the D14 reconciler commits exactly one v2 domain event/ack;
`fixer-join` waits for every active **record** node; and
`combine-fixer-safety` commits one aggregate before the existing safety gate
reads it. An inactive target needs no intent, record output, summary, or
fragment.

Authority cases include a current generated/updated path, a current D16-
manifest-and-plan-mapped reused private test whose input-snapshot digest
matches, a reused-path digest mismatch, summary-only helper/shared reuse, and imported codegen with no
physical write set. Only the first two schedule a fixer; imported codegen
reaches `unverified_imported_codegen`/`complete-failed` without manufacturing
physical authority.

Fail-closed cases cover missing/malformed intent, wrong target, noncanonical or
unauthorized path/proposal, claimed-path versus write-set mismatch, test writes
under `no_op`/`skipped`, delete/assertion-weakening/skip/xfail/unrelated/product
diffs, unapproved high risk, approval receipt drift, an unexpected inactive
fragment, a stale fragment from another attempt/root, and a missing active
fragment. The approved-high-risk positive control binds the exact proposal and
tree before commit; any later drift rejects. The gate cannot consume a per-
target fragment directly or a pre-existing aggregate. Every candidate-
validation failure asserts that the root tree, canonical test paths, and host
domain ledger are byte-identical to their pre-attempt state, with no succeeded
event or superstep commit, including a crash after freeze but before the
rejected success line. Receipt mutations cover cross root/task/attempt,
input-snapshot/output/write-set substitution and validator-semantics drift;
none may defer rejection to the post-commit record node.

Run every candidate-validation cut. A pre-validation crash leaves only an
unpublished frozen candidate; a post-validation/pre-success crash may leave an
unreferenced receipt CAS object; a rejected hook leaves neither success nor
publication. Because these cuts follow `task_attempt_started`, recovery marks
that attempt abandoned, consumes normal retry budget, and—when policy allows—
uses the deterministic **next** ledger-derived attempt number. At
`max_attempts=1` it exhausts rather than secretly reusing the number. It cleans
unreachable data and never treats an orphan receipt as authority.

Run every fixer-approval cut. Before an acknowledged exact approval there is
no fixer attempt. Recovery appends/reuses one approval event/projection/resume
transition: the audited resume plans `record-fixer-approval`, its committed
superstep adds the receipt to the current tree, and its reconciler appends/acks
the bound domain event before the gate. Recovery then activates only the
receipt-bound target set once. Cross-root/tree/proposal/authority/policy/path
receipt substitutions fail before dispatch.

Run every named healing-allocation and healing-record cut in section 12.1 in a
subprocess and rebuild the runtime. Allocation recovery reconciles the pending
inline effect without reinvoking the succeeded allocate task. A pre-success
cut may rerun the handler; after-success/pre-superstep recovery commits the
frozen write set without rerunning it; after-superstep/pre-domain-event recovery
appends once; after-domain-event/pre-ack recovery reuses the exact v2 event;
and after-ack/pre-successor recovery plans the successor once. Exact idempotency-
key/payload replay is a no-op, while the same key with
different payload raises ledger integrity error. No `heal_record_apply_v2`
event appears until the committed fixer intent/write set exists, and neither
fixer persona requires shell access.

Ack mutation tests cover cross root/task/attempt, wrong known kind,
reconciler-semantics/payload/domain-event identity drift, acknowledgement before
superstep/domain event, and a conflicting duplicate. Each produces an explicit
integrity failure and no successor plan; checking `effect_id` alone is never
sufficient.

## 13. Benchmark and Eval Corrections

### 13.1 Plan-ready, codegen-pending tiers

Do not repurpose the existing `L2-*-codegen-seed` tiers. `L3-run-seed` extends
the API L2 tier and `L3-run-done` extends that chain; those run/full fixtures
still require completed codegen, summaries, and tests.

Add an independent `L1-assurance-input-ready` base that does not extend the
additive plan-complete chain. It seeds only common proposal/case/fact/registry
files and may import the validated bootstrap registry plus fact-baseline
predecessor. Add four new
`L2-{api,e2e,fuzz,performance}-codegen-pending` tiers for the workflow-codegen
datasets. Each extends the new base, adds only its selected layer's plan/
summary artifacts and declared reusable test-support files, and declares
`expected_layers` as validation metadata.

`expected_layers` never supplies runtime params. The dataset/suite executor is
the sole selection authority; it resolves once, passes that tuple into fixture
seeding, and the tier must match or fail before copying. Tier workflow-state or
`.qa.yaml` resets cannot override the resolved tuple.

Extend the locked fixture manifest with explicit, sandbox-only `repo_paths`
for `.aa/config.yaml`, `.aa/data-knowledge.yaml`, and reusable test support.
`seed_change` copies these paths into the isolated attempt SUT only, rejects
absolute/parent/symlink paths, and verifies them through `fixture-lock.json`.
The new base uses this mechanism to provide a truthful L1 capability and its
existing `tests/testdata/domain/<entity>.py` implementation.

The current Fuzz/Performance benchmark artifacts are repaired away from the
fictional `account` / `tests/factories/account.py` pair. They use a domain
entity actually present in the benchmark SUT and L1 (the existing API entity
is the initial target), map it to
`capabilities.domain_factories.api.make_api` and
`tests/testdata/domain/api.py`, and pass an import/capability smoke check. If
that implementation cannot be captured truthfully from the benchmark SUT, the
fixture must choose another real L1 entity; it may not synthesize a pass-shaped
leaf or review document.

A pending tier may contain an existing adapter/config/conftest/support file
under a private root when the skill declares it as input. It must not contain
the target generated test/stub for the sample, plan review/check evidence,
codegen summary, or generated-file manifest. The content-aware before manifest
and layer test-file
classifier distinguish reusable support from a newly generated test.

Remove `_STUB_REVIEW_DOCS` and any seeding helper that manufactures a
pass-shaped selected-layer review/check document for codegen tiers. Plan
artifacts, summaries, cases, `.aa` config, and data knowledge are input files,
not imported completion authority. Do not import any selected-layer
applicability, reviewer, mechanical, plan-gate, branch wrapper, precheck,
codegen, or generation-join completion.

Fixture validation rejects a codegen tier if its import manifest includes any
selected-layer assurance-chain node or if a reset marks the selected codegen
phase done. It also expands the full `extends` chain before validation so a
forbidden parent import cannot hide behind an apparently clean child manifest.
The same expansion scans ordinary `paths` and locked `repo_paths`: it rejects
directly or inherited selected mapped test targets/stubs, plan-review/check
JSON, codegen summaries, and generated-file manifests while permitting declared support/config/adapter
inputs. A preseeded mapped test followed by a comment-only modification is
therefore not a generated-test success path. Tests cover each forbidden class
both directly and through a parent tier.
The four workflow-codegen datasets switch to the new pending IDs; L3 run/full
datasets retain their existing complete-tier ancestry and receive explicit
regression coverage.

### 13.2 Execution evidence

`execute_attempt` records the resolved selected layer tuple and the returned
root invocation ID in `execution.json`, together with the change ID, safely
resolved repository-relative change root, selection-normalizer version, and
write-policy schema version. The root ID is set for both a fresh run and
import-checkpoint continuation. Failure before a root is established records
null and cannot receive current-attempt credit.

The record also binds D17's `change-location.json`, exact
`change-location-config.yaml`, and before-manifest digest by path, SHA-256, and
size. Change resolution completes before policy construction or runtime start;
archive-only or unsafe resolution produces no writable policy/root.

`execution.json` is an executor-owned trusted envelope written in the external
attempt/evidence directory after runtime; neither the agent contracts nor the
attempt-SUT write policy can modify it. In addition to artifact digests it
binds the source-ledger digest/size/count, root-slice digest/algorithm, and the
expected ordered `(source_seq, canonical_event_digest)` pairs/count selected
from the full source ledger. The trust boundary is explicit: the exporter unit
tests prove selection completeness against the full ledger; the offline scorer
verifies the exported slice against this envelope rather than pretending a
filtered slice can prove what was omitted from its own sequence gaps.

The executor exports a bounded root-execution evidence closure instead of the
entire change ledger or graph object store:

```python
class RootEventSliceEventV1(StrictWireModel):
    export_seq: PositiveInt
    source_seq: PositiveInt
    event: GraphEvent


class RootEventSliceV1(StrictWireModel):
    schema_version: Literal["1"]
    export_algorithm: Literal["root_execution_closure/v1"]
    root_invocation_id: str
    source_ledger_sha256: str
    source_ledger_size: int
    source_event_count: int
    events: list[RootEventSliceEventV1]


class EvidenceExportObjectV1(StrictWireModel):
    kind: Literal[
        "root_event_slice",
        "definition_binding",
        "graph",
        "execution_contract",
        "ingest_catalog",
        "policy",
        "assurance_profile",
        "gate_semantics",
        "topology_semantics",
        "commit_safety_semantics",
        "input_snapshot",
        "runtime_context",
        "validation_receipt",
        "write_set",
        "blob",
    ]
    logical_id: str
    relative_path: str
    sha256: str
    size: int


class EvidenceExportManifestV1(StrictWireModel):
    schema_version: Literal["1"]
    root_invocation_id: str
    selected_layers: list[LayerName]
    objects: list[EvidenceExportObjectV1]
```

The root event slice contains the recorded root and descendants selected by
`parent_invocation_id`, plus their imported-task, task-attempt, committed-
superstep, binding, interrupt/revision, and synchronized-apply events. When the
root-start event consumes D18 replacement authority, it additionally contains
the one exact earlier `graph_invocation_superseded` event whose old-root and
authorization IDs it binds; no other predecessor history is admitted. Exporter
selection-completeness tests scan the full ledger to prove that authorization
exists, targets this root exactly once, and was not consumed by another root.
Each
payload is validated through the production graph-event `TypeAdapter`.
`export_seq` is exactly `1..N`; `source_seq` is strictly increasing but may
have gaps where other roots/events were filtered. The source ledger digest,
byte size, event count, and export-algorithm version are bound in both the
slice and `execution.json`. This is a distinct wire model and is never passed
to `read_events_strict(...)`, whose contiguous source-ledger invariant remains
unchanged.

The export closure is deliberately defined rather than inferred from every
event field. Starting from the slice, it includes all root/child definition
bindings and their pinned graph, every per-target execution-contract object
referenced by the binding (never an invented aggregate catalog), the ingest
catalog, exact pinned policy snapshot, assurance profile, gate-semantics, and
topology/commit-safety-semantics objects. Child policy/semantics bindings must
equal the root
as required by runtime compatibility. For each selected current codegen
attempt it includes the `input_snapshot_id`,
`candidate_validation_receipt_id`, `write_set_id`, any non-null runtime-context
object, and blobs needed to verify mapped plan/case inputs, both D16 hard
outputs, and `add`/`modify` outputs. Tree IDs in events remain lineage/epoch
identifiers but are not recursively exported; the input snapshots/write sets
are the bounded content proof. Before/after manifests, write diff,
execution record, and write policy remain top-level eval evidence and are
bound by digest from `execution.json`.

D13's canonical input snapshot binds the prepared attempt, input tree,
task-input/contract/claims digests, and every file materialized by declared
reads. Codegen scoring loads plan/case/mapping bytes only from blobs named by
that snapshot; mutable final raw-output copies never become the codegen input
authority.

Object records are unique and canonically sorted by `(kind, logical_id)`.
Export must equal the transitive closure: a missing/tampered referenced object,
an unreferenced object record, duplicate logical ID, path escape, or digest/
size mismatch is evidence-integrity failure. An unrelated sentinel object in
the source store is not copied. `execution.json` records the canonical export-
manifest digest so replacing the manifest is also detected.

V6 requires staged gate/topology/commit-safety manifest objects from D10/D14.
A legacy binding exports only object kinds that its historical schema actually
pinned; a v4/v5 compatibility receipt additionally exports the staged audit-
semantics object referenced by that receipt. No exporter reconstructs
historical semantics bytes from the current executable.

The scorer uses typed slice parsing, validates export/source sequence and
ancestry closure, and consumes the exported pinned topology lineage; it does
not search for the latest convenient task by node name across all history.

Tests include two interleaved roots in one valid source ledger. The selected
slice has source-sequence gaps but validates; deleted/duplicated/reordered
slice events, non-contiguous export sequence, non-increasing source sequence,
wrong source metadata, root-slice digest/pair mismatch, or an ancestry escape
fails evidence integrity against the executor envelope.

`write-diff.json` is emitted from the content-aware snapshot in D12. Missing
write-set/blob bytes are evidence-integrity failure, not a reason to fall back
to the copied SUT tree.

### 13.3 Current-chain scorer

Add shared scorer evidence that binds the root, assurance child, selected
layer branch/cycle, and committed attempts. It first emits one typed result per
resolved selected layer; it never collapses the event tree to the first branch
found. For an applicable layer, the scorer requires:

- the current cycle applicability attempt, plus the current branch preflight
  attempt for Fuzz/Performance;
- current reviewer, mechanical, plan-gate, precheck, and codegen attempts;
- no selected-layer plan attempt in codegen-only;
- review/check/L1 digests bound by the gate;
- codegen summary and path-specific D16 manifest digests bound by the committed
  codegen attempt;
- a successful `generated_files_candidate/v1` receipt binding that manifest,
  the input snapshot, plan/case mapping, and write set;
- a non-empty codegen `write_set_id` whose frozen entries include at least one
  layer-classified test file under the selected private root with
  `operation in {add, modify}`, a non-empty after/blob digest, and a true
  content transition (`before_sha256 != after_sha256`);
- a final manifest digest for that path equal to the write-set
  `after_sha256`/blob digest;
- the same path attributed as changed by the content-aware eval snapshot;
- zero writes under an unselected private root; and
- zero forbidden writes under the resolved policy.

Before classifying any write, the scorer validates `policy_integrity`. It
strictly parses `execution.json` and `write-policy.json`, revalidates the
recorded change ID/root through D17's copied config/strict location evidence,
and calls the single production policy builder with the recorded canonical selected tuple.
Schema version, mode, and the canonically ordered pattern list must be byte-for-
byte equal to the persisted policy—no extra, missing, duplicate, broader, or
sibling pattern. Integrity failure happens before allow/deny verdicts and sets
all current-chain hard metrics to zero.

Negative replay cases include `tests/**`, a missing selected root, an added
unselected sibling root, the wrong change root, an execution-tuple/policy
mismatch, reordered/noncanonical patterns, and an unknown schema version. The
positive replay matrix covers all fifteen non-empty selections and requires
exact reconstructed policy bytes for each.

`selected_test_write_rate` uses one code-owned dispatcher with typed per-layer
profiles, shared by scorer and fixture/runtime assertions. The plan formats are
made structural enough to avoid guessing from Purpose prose: API/E2E retain
`Test Function Mapping`; Fuzz gains the same Case ID / Test Function / Target
File table; Performance `Task Mapping` gains an exact Target File column. The
Fuzz/Performance plan skills, canonical fixtures, reviewers/mechanical parser,
and benchmark plans are updated together.

Every qualifying path is a D16 `test_entry`, is an add/content-modify in the
selected private root, is the exact mapped target for a current selected
automated case, parses as Python, and satisfies a layer-specific behavioral
shape. “Non-empty function” is not evidence: a bare return, assignment-only
body, `assert True`/another constant assertion, decorator-only function, or
Locust task without a request receives no credit. The exact obligations are:

| Layer | Mapping | AST behavioral evidence |
|---|---|---|
| API | Test Function Mapping | exact mapped `test_*` function/method makes an HTTP request through a declared client fixture/type and performs a non-constant assertion over that response's status/body/decoded payload |
| E2E | Test Function Mapping | exact mapped `test_*` function/method performs Playwright navigation plus an interaction and an `expect(...)`/assertion whose value depends on page/locator state |
| Fuzz | Test Function Mapping + Schema Acquisition | exact mapped `test_*` entrypoint is bound to the declared Schemathesis schema/parametrize decorator, consumes the generated case, and invokes `case.call_and_validate(...)` or the profile's explicitly registered equivalent schema-call helper |
| Performance | Task Mapping | exact mapped method is owned by a Locust `HttpUser`/`FastHttpUser` subclass, is decorated with `@task`/`@task(...)`, and makes an HTTP request through `self.client` in that method |

The dispatcher consumes mapping/case bytes bound by the codegen attempt's input
snapshot and reconciles the D16 manifest against its write set, never mutable
final copies. Registered client/schema-call forms are closed policy data with
mutation coverage; arbitrary call-name substring matches are forbidden.
Helper/conftest/adapter files, an unmapped test or task, import-only/empty
files, wrong-case symbols, a Fuzz function without the bound schema call, an
API response without dependent validation, an E2E navigation without
interaction/assertion, and a Locust-looking function outside the mapped User/
task ownership receive no selected-test-write credit. Each layer has an
independent positive/negative corpus. This is static code-owned evidence; the
codegen agent does not run collection and the benchmark's `run_tests=false`
remains truthful rather than implying execution proof.

The existing syntax, secret, summary, and evidence-integrity metrics remain,
but file presence alone no longer establishes success. Add hard metrics such
as:

```text
current_assurance_chain_rate == 1.0
current_codegen_attempt_rate == 1.0
selected_test_write_rate == 1.0
```

All four `workflow-*-codegen.yaml` suites declare each of these metrics in
`regression.metrics` with `higher_is_better`/`max_regression: 0.0` and as a
hard `gte 1.0` threshold. A loader test opens all four real suite files and
asserts the exact entries; deleting any one threshold/regression key makes the
test fail. A focused `aa eval run` failure fixture proves a zero metric changes
the suite verdict rather than appearing only in diagnostics.

Scorer unit tests also cover the inapplicable shape: current N/A evidence and
the same layer-specific applicability/preflight ownership, but no reviewer/
codegen attempt. A suite may choose an applicable sample for the benchmark
gate, but the absence classifier remains tested.

Aggregation is explicit:

- `current_assurance_chain_rate` uses every selected layer as denominator;
  applicable layers pass only with the full chain, inapplicable layers only
  with current N/A plus required absence;
- `current_codegen_attempt_rate` and `selected_test_write_rate` use selected
  applicable layers as denominator; if none are applicable, both are `0.0`
  (not a vacuous pass); and
- the hard suite result is green only when every selected layer satisfies its
  applicable/inapplicable shape.

Mixed multi-layer scorer tests include one applicable and one inapplicable
layer, two applicable layers where only one writes a test, and a historical
successful branch from another root. None may yield a global 1.0 unless every
current selected branch satisfies its own contract.

Lineage is epoch-closed, not merely root-closed. Applicability, review,
mechanical, gate, precheck, and codegen must share the same discovered branch/
cycle generation, source/target tree chain, and producer bindings. A negative
fixture combines review from cycle 1 with byte-identical mechanical/gate/
precheck/codegen evidence from cycle 2; it cannot be laundered into one valid
chain. Inapplicable negatives cover absent, malformed, wrong-layer,
wrong-change, stale, and wrong-tree N/A documents—required absence of reviewer/
codegen never substitutes for current valid N/A authority.

Deletion-only, chmod/mode-only, shared-testdata-only, support-file-only, and
summary-only write sets receive zero `selected_test_write_rate`, even when a
pre-seeded test elsewhere remains syntactically valid.

### 13.4 No seeded-code fallback

`_copy_sut_tests` may continue mirroring the final SUT tests into raw output,
but the scorer must cross-check `write-diff.json`. A syntactically valid test
that was present before the runtime receives no selected-test-write credit.

Likewise, a pre-existing summary receives no current-codegen credit unless the
current committed attempt's output digest map binds those exact bytes.

## 14. Error Handling and Failure Semantics

### 14.1 Compile-time failures

An invalid current packaged topology fails in
`compile_packaged_workflow(...)` before the runtime, fixture import, or agent
adapter starts. The rendered error includes the stable conformance code and
locator. Multiple independent findings are sorted deterministically by layer,
owner, locator, and code.

The thrown `CompileError.diagnostics` preserves those typed findings (and a
typed category for generic identity/contract/catalog compilation failures).
Pinned-definition reason mapping consumes that structure. Tests forbid the
old message-substring classifier from returning different reason codes when
human wording changes.

Synthetic/custom graphs compiled through `compile_workflow(...)` retain their
generic behavior. They are not forced to implement the packaged assurance
shape. Historical compilation uses the historical replay-surface and safety
classifier, never current activation conformance.

### 14.2 Parameter and write-policy failures

Invalid or empty selected layers fail before fixture seeding and before the
initial content-aware write manifest. Runtime params and write-policy evidence are
therefore never created from different interpretations of the same input.

A current change that cannot be safely resolved under the SUT repository is
an infrastructure error. The scanner never falls back to `qa/changes/**`.

### 14.3 Contract and artifact failures

Missing required input, unauthorized read, invalid authoring output, invalid
runtime artifact, or forbidden write remains the owning boundary's explicit
failure. The harness and eval executor do not convert these failures into
inapplicable evidence.

Malformed cases are errors. A missing selected automated case is a valid
inapplicable result only after all case documents have parsed successfully.

### 14.4 Gate and freshness failures

Missing, malformed, wrong-change, wrong-layer, stale, uncommitted, or
wrong-definition review/check/L1 evidence stops the relevant gate or
precondition. It never falls through to skip or pass.

Only a valid current inapplicable document produces skip. This preserves the
difference between “the layer was checked and has no automated scope” and “the
check did not run.”

### 14.5 Runtime and adapter failures

Expected deterministic adapter failures retain normal retry/recovery behavior.
Unexpected adapter calls, programmer errors, ledger integrity failures, or
definition incompatibility escape as errors. They are not translated into N/A
evidence, a benchmark zero, or a partial success.

A fixer precommit failure publishes no success/superstep/tree change. A
retryable durable-effect reconciliation error remains explicitly unacknowledged
and recoverable; a permanent payload/model/domain conflict terminates with
`durable_effect_integrity_failed`. Neither condition is represented as a
successful node with a silently blocked successor.

`legacy_commit_safety_semantics_unbound` leaves the old root unchanged until an
explicit D18 command. An ineligible/conflicting supersede writes nothing. Once
the strict supersede event commits, the complete bound subtree is terminally
fenced; only an exact pending single-use replacement authorization can cross
`restart: once`, and command retry resumes rather than duplicates it.

### 14.6 Scorer failures

Missing root invocation identity, invalid ledger bytes, ambiguous lineage,
missing committed attempts, or digest mismatch produces zero current-chain
credit and fails the hard metric. The scorer may still report syntax or secret
diagnostics, but those secondary metrics cannot override the authority
failure.

## 15. Test Organization

### 15.1 Shared helpers and fixtures

Add:

- `tests/helpers_assurance_contract.py` for canonical bundle loading,
  structured skill-section parsing, production boundary driving, and mutation
  observations;
- independent API/E2E/Fuzz/Performance roots under
  `tests/fixtures/assurance/`;
- deterministic runtime adapter helpers that fail on unexpected targets; and
- event assertions that bind attempts by root/parent lineage and committed
  superstep rather than node-name search alone.

The helpers expose small typed interfaces. They do not become a second copy of
mechanical, gate, planner, or replay logic.

### 15.2 Unit suites

Create or reshape:

- `tests/unit/verification/test_assurance_contract_round_trip.py` — full
  fixture/authoring/freeze/mechanical/gate/precheck/workspace path;
- `tests/unit/verification/test_assurance_contract_mutations.py` — exact
  failure ownership and unaffected-check assertions;
- rename or narrow `test_layer_assurance_round_trip.py` to explicit
  PlanCheck exact-check-set, exact-status, aggregation, and canonical-byte
  invariant coverage;
- `tests/unit/workflow/graph/test_assurance_topology_mutations.py` — decoded
  current schema mutations and structured diagnostics;
- expand `test_replay_schema.py` with historical rename/equivalence/bypass
  classification and v5/v6 semantics dispatch;
- add topology-semantics manifest dependency/digest mutation coverage;
- add v1-v5 event reader/fold goldens and v6 assurance/non-assurance
  root-to-child binding inheritance tests;
- add D18 supersede event/CLI/start-guard tests for stable typed eligibility,
  exact idempotency/conflict, single-use same-entrypoint v6 replacement,
  terminal stop, restart-once exemption scope, and active/lease rejection;
- add task-input-snapshot schema/CAS, overlapping project/repo alias,
  start-success ID, consumer-set, old-event optionality, cross-attempt, omitted-
  mapping, tampered-object, pre-start crash/cleanup, and started-object-presence
  tests;
- add plan-fixer runtime-context tests for automatic and human-approved modes,
  exact interrupt/reason/review binding, wrong/stale prompt context, resume, and
  zero host-ledger visibility;
- add strict precommit-validator/receipt and durable-effect/ack tests: exactly
  four codegen contracts select `generated_files_candidate/v1` and exactly two
  fixer contracts select `codegen_fix_candidate/v1`; unknown validator/effect
  kinds, payload/semantics digest drift, cross-bound receipts, old-event
  optionality, same-key conflict, and a missing acknowledgement all fail
  closed;
- add all four strict generated-file catalog/model/output tests and reconcile
  manifest/input-snapshot/write-set/plan/case mappings for generated, updated,
  and reused files; malformed, duplicate, summary-only, cross-layer/change,
  unbound reuse, omitted write, extra write, and digest drift fail before
  codegen success;
- add `runtime_commit_safety/v1` exact dependency and registered-consumer-set
  tests, mutate every validator/helper/model/reconciler/retry dependency to
  change the aggregate digest, and pin object ID/digest use through root/child,
  request, projection, replay, dispatch, snapshot, and export;
- add durable-effect acknowledgement substitutions for wrong root/task/
  attempt/kind/semantics/payload/domain event, ack-before-superstep/domain,
  conflicting duplicate, plus real progression-lock retry-sidecar backoff/
  restart/release controls;
- add shared evidence-path resolver tests for project/repo aliasing across all
  four layers, physical ownership precedence, another change, ambiguity, and
  traversal;
- expand compiler/definition-pinning tests to prove typed reason mapping and
  reject message-substring classification;
- add common historical-role-manifest consumer tests that rename all role IDs
  and drive compile, selection, invocation binding, trace binding, and safety
  classification together;
- add codegen-fixer intent/authority/record/combine model tests, including
  exact write-set equality, per-target conditional outputs, idempotency-key
  conflict, and persona command-denial compatibility;
- add declared-only mount-policy tests proving `.venv`, `node_modules`, and
  coordinator-ledger host links are absent and unwritable; pin all sixteen
  role-to-persona mappings, persona edit floors/external-directory denial, and
  OpenCode create/prompt/status attempt-directory request binding;
- expand `tests/unit/eval/test_write_scan.py` for resolved multi-layer/current-
  change policy, all fifteen policy replays, manifest schema/resource/special-
  file failures, and pre-dirty content/mode/kind/symlink fingerprints;
- add D17 change-location/config evidence replay for custom roots,
  changes/archive coexistence, archive-only rejection, tamper/missing bytes,
  another change, and path escape;
- add bounded evidence-export closure/tamper tests and static selected-test
  classifier controls per layer for helper, unmapped, empty, import-only,
  wrong-case, unbound Fuzz, and invalid Locust ownership files;
- add interleaved-ledger root-slice sequence/ancestry tests and load all four
  real codegen suites to pin their hard-metric threshold/regression entries;
- add fixture-tier validation tests that expand additive parents and reject
  any hidden selected assurance completion, generated-file manifest, or mapped
  test stub; and
- expand codegen scorer/fixture/executor tests for current physical attempt
  binding.

Healing migration tests drive v2-only, legacy-only, and mixed ledgers through
the normalized `HealingEpisodeProjection` and every direct public consumer:
`derive_healing_state`, `derive_guard_context`, allocation budget, record-
apply safety, graph guards, and workflow-history/retro projection. A consumer-
set guard rejects direct raw legacy event filtering outside the codec/
projector/versioned fixtures.

### 15.3 Integration suites

Add:

- `tests/integration/test_four_layer_codegen_only.py` for the four-by-two,
  stale-artifact, multi-layer, and generation-join matrices; and
- `tests/integration/test_four_layer_resume.py` for restart, automatic fixer,
  manual revision, knowledge remediation, accept-risk/stop, pinned definition
  resume, and the exact before/inside, snapshot, codegen-candidate, target/
  tree/materialization, synchronized-publication, manual-revision, and child-
  wrapper cut IDs from section 12.1; and
- `tests/integration/test_codegen_fixer_record.py` for API-only, E2E-only,
  both-active healing, active-record join, aggregate safety binding, and every
  fixer-candidate, fixer-approval, healing-allocation, healing-record, and
  effect-retry-lock cut ID from sections 12.1/12.7; and
- packaged v4/v5 resume fixtures for report-only continuation versus stable
  `legacy_commit_safety_semantics_unbound` at pending codegen/fixer, with the
  D18 before-append/after-append/after-root-start/concurrent replacement crash
  matrix and terminal-stop control.

These suites use the packaged graph and contracts. Planner-only tests remain
as fast local topology smoke coverage but no longer serve as end-to-end proof.

### 15.4 Existing generic tests

Retain generic runtime fault, manual revision, import replay, and pinned
definition tests. The new packaged tests complement them; they do not replace
generic state-machine coverage with layer-specific copies.

## 16. Production Change Surface

The expected production changes are focused in:

- `assurance_agent/_resources/skills/aa-{api,e2e,fuzz,performance}-{plan,plan-reviewer,codegen}/SKILL.md`
  plus the API/E2E plan-fixer and codegen-fixer skills
  for structured input/output closure, workflow-state ownership cleanup, and
  shared-factory path consistency, including removal of agent-side collection
  and `record-apply` commands, plan-fixer ledger reads, and addition of typed
  Runtime Context/generated-file manifest outputs;
- `assurance_agent/_resources/schemas/execution-contracts.yaml` for precise
  matching reads/writes, exact plan/codegen outputs, both precommit validator
  sets, fixer intents, and declared-read isolation;
- `assurance_agent/_resources/schemas/workflow-schema.yaml`, the ingest artifact
  catalog, and artifact models/registry for fixer authority/intent and the four
  path-specific generated-file manifests,
  pre-fix proposal-approval gate/interrupt, per-target record operations,
  active-record join, and aggregate safety;
- `assurance_agent/workflow/graph/handlers/agent.py`, `handlers/operation.py`,
  packaged persona documents, and the OpenCode adapter for exact target/
  persona/edit-floor/attempt-directory binding and contract-aware host mount
  policy; declared-only agents receive no task-visible host runtime/coordinator
  symlink;
- scheduler/task preparation reorder, workspace materialization/sidecars,
  TreeStore object models, graph attempt events/folds, and event goldens for
  `TaskInputSnapshotV1`; historical attempt events keep the new field optional,
  while current declared-only start/success events must bind the same ID; add
  `PlanFixerRuntimeContextV1` CAS/prompt/event binding without a ledger mount;
- execution-contract loader/validator registry, candidate-validation receipt
  CAS/event fields, `TaskResult.durable_effects`, generic effect registry,
  acknowledgement events, planner successor blocking, and recovery scanning
  for the D14 precommit/outbox seam, plus the independent atomic effect-retry
  sidecar/backoff projection;
- a small shared graph/eval evidence-path resolver used by fixer authority,
  precommit validation, write policy, selected-test classification, and scorer;
- healing allocation/safety/operation and core event/projection modules for the
  recoverable allocation outbox, graph-owned fixer authority,
  single-record `healing_attempt_allocated_v2`, approval record operation/
  `fixer_proposal_approved`, `heal_record_apply_v2`, idempotent
  `record-codegen-fix-apply`, and safety aggregation; legacy allocation/apply
  events/goldens remain readable;
- a normalized healing-episode projector consumed by healing state/guards,
  allocation, record safety, graph guards, and workflow-history/retro code;
- `assurance_agent/verification/` plan-check/profile code for the typed Fuzz
  Test Function and Performance Task/Target mappings, plus the shared
  layer-specific generated-entry classifier;
- `assurance_agent/workflow/graph/replay_schema.py` for structured current
  conformance, semantic historical safety classification, and frozen legacy
  epoch dispatch;
- a shared historical role-discovery manifest consumed by replay-surface,
  selection, invocation/replay/trace binding, the unbound-epoch audit trigger,
  and the classifier;
- a focused `assurance_agent/workflow/graph/topology_semantics.py` manifest for
  the v6 classifier dependency digest, plus gate-semantics canonical manifest
  bytes, a `runtime_commit_safety/v1` validator/reconciler manifest, and
  definition-pinning storage/verification for all three staged objects,
  including exact dependency/consumer closure;
- `assurance_agent/workflow/graph/compiler.py` and
  `definition_pinning.py` for typed compile diagnostics/reason mapping;
- graph event/projection/definition-binding/replay-binding/runtime-factory
  modules for the v6 topology-semantics digest, exact pinned inheritance, and
  append-only v4/v5 compatibility receipts, plus commit-safety manifest
  binding/inheritance/live-compatibility checks;
- workflow CLI/runtime start guards and graph event/projection models for D18's
  typed legacy-root supersede, single-use replacement authorization, and v6
  root-start ancestry fields;
- `runtime.py`/the subgraph recovery seam only as needed to resume an existing
  child after a wrapper crash without creating a duplicate;
- `assurance_agent/eval/runner.py`, `write_scan.py`, and `executor.py` for one
  raw-value-to-selected-layers normalization,
  content-aware snapshots, current-change policy, root invocation evidence,
  D17 config/change-location evidence, bounded evidence-export closure, and
  write-set/blob preservation;
- `assurance_agent/eval/scorers/codegen.py` plus a small shared event-binding
  helper, policy-integrity replay, and a shared static selected-test classifier
  for current-chain scoring, including D16 manifest/receipt reconciliation and
  per-layer behavioral AST obligations;
- `assurance_agent/eval/fixtures.py`, one independent assurance-input base,
  four new codegen-pending fixture tiers, and four workflow-codegen
  suite/dataset definitions, including locked `repo_paths`; existing complete
  L2/L3 tiers remain intact; and
- focused documentation for eval/codegen-only semantics.

No GraphRuntime target dispatch, scheduler special case, or Python-generated
layer topology is added.

## 17. Rollout Order

1. Prepare repaired skill text, canonical fixtures, structural parity tests,
   and the round-trip harness, but do not yet flip any contract to
   `declared_only`.
2. Dark-ship the strict artifacts, task-input/runtime-context snapshots,
   no-host-link workspace mechanism, precommit/effect registries, retry
   sidecar, normalized healing projection, and all three semantics manifests.
   No execution contract selects a new validator/effect and no healing route
   changes yet, so v5 public execution cannot encounter an unbound seam.
3. Add typed compiler diagnostics, freeze legacy-v4/v5 display classifiers,
   implement/test the v6 semantic classifier, stage gate/topology/commit-safety
   objects, and add every v6 binding reader/inheritance/live-replay check. Pass
   the v1-v5 and assurance/non-assurance child compatibility matrix, then flip
   new root writers to v6. No build emits a v6 root without all three staged
   semantics identities; existing contracts still have their prior behavior.
4. In one atomic v6-only activation unit, add structured current conformance,
   generated-file manifests and both validator contract sets, the new healing
   topology/effects, exact closed contracts, typed plan-fixer context, and then
   flip all sixteen assurance agents to `declared_only`. The unit includes
   persona/directory, manifest, fixer, snapshot, effect, and crash tests. No
   intermediate release selects a validator/effect without a root-bound
   `runtime_commit_safety/v1` object or advertises isolation with ambient links.
5. Unify multi-layer selection, D17 change-location evidence, and content-aware
   write-policy evidence.
6. Add the real codegen-only matrix and stale-evidence assertions.
7. Add restart/remediation/named-crash-cut/pinned resume coverage.
8. Add codegen-pending benchmark tiers and require current-chain
   scorer evidence.
9. Publish the intentional v1-v5 pending assurance-codegen/healing resume
   narrowing, stable error/operator exits, and imported-codegen healing
   narrowing in release notes.
10. Run the complete compatibility and CI gate.

Each stage is independently releasable only when its existing public behavior
remains green. In particular, strict current conformance is not reused for
pinned replay, and benchmark scorers are not activated before fixture tiers
stop importing the selected assurance chain.

## 18. Verification Matrix

### 18.1 Contract round-trip

- Four canonical layers cross authoring, runtime model, freeze, applicability,
  mechanical, JSON, plan gate, precheck, and codegen visibility.
- API/E2E emit four passes.
- Fuzz/Performance emit three passes plus static assert-ideal N/A.
- Dynamic empty scope emits four current N/A entries.
- Every structured skill required input is visible through its exact contract.
- Every assurance agent runs with declared-read isolation; an ambient sibling
  or undeclared repository file is unreadable.
- Declared-only workspaces expose no host `.venv`, `node_modules`, ledger,
  convenience Git, or runtime-control link; start/success bind one valid input
  snapshot.
- All sixteen targets use their exact bounded persona; packaged personas retain
  deny-by-default edit floors/external-directory denial, and OpenCode create,
  prompt, and status requests carry the exact attempt workspace directory.
- Each codegen attempt hard-outputs both its summary and path-specific strict
  generated-file manifest; each plan fixer receives a bound typed Runtime
  Context rather than reading the ledger.
- Every role output is within both write and authorization scopes.
- Forbidden workflow-state/agent-command tokens have zero occurrences in the
  scoped skills, and fixer Outputs are the exact path-specific intents.
- Registry-selected serialize -> load -> serialize bytes are identical.

### 18.2 Contract mutations

- Missing/undeclared/forbidden reads fail at workspace or skill parity.
- Current-layer and shared-testdata writes pass.
- Sibling-layer, other-change, product, and memory writes fail.
- Invalid fixer intent/proposal/authority/write-set candidates have no success
  event, superstep, canonical-tree change, or host-ledger change.
- Shape-valid but manifest/input-snapshot/write-set/plan/case-inconsistent
  codegen output has no success event or tree change; wrong/stale automatic or
  human-approved plan-fixer context never reaches adapter dispatch.
- Authoring/runtime identity and required-field defects fail at ingest.
- Mechanical mutations fail only the named check and preserve other results.
- Stale but model-valid evidence fails at the gate/precondition owner.

### 18.3 Current topology mutations

- Every selection, route, predicate, bypass, alias, gate, interrupt,
  remediation, and join mutation in section 8.7 fails packaged compilation.
- Diagnostics have stable codes, owners, layers, and locators.
- AST-equivalent formatting/operand permutations pass the closed-domain truth
  table.
- Project/explicit schemas retain generic compilation, and pinned schemas
  never enter current validation.

### 18.4 Historical classification

- V6 marker-free old graphs are `legacy_unwired`.
- V6 safely wired renamed/equivalent old graphs are `wired`.
- V6 activated graphs with any safety bypass are `partial`.
- Current exactness alone never changes a pinned classification.
- V4/V5 golden results—including known false-negative statuses—stay frozen
  under their unbound display semantics; v6 results require the recorded
  `historical_topology_safety/v1` digest.
- Topologically safe pending v4/v5 assurance paths append/reuse a bound
  compatibility receipt; known-bypass fixtures are blocked despite frozen
  display status, and v4 with an unreconstructable profile is blocked before
  planning. The receipt does not satisfy D14 commit-safety binding.
- Receipt triggering comes from v6 semantic discovery/reachability; an
  ambiguous marker/test-write role cannot evade audit by a legacy false
  negative.
- Unknown/mismatched topology-semantics digests fail before classification.
- Unknown/mismatched `runtime_commit_safety/v1` bytes, object identity, or any
  validator/reconciler dependency fail before dispatch/recovery; mutate-each-
  dependency tests change the aggregate digest.
- V1-v5 event fixtures retain reader/fold behavior, and v6 assurance, retro,
  issue-review, and improvement-review children inherit the root digest.

### 18.5 Codegen-only

- All eight layer × applicability cells pass their exact presence/absence
  matrix.
- Malformed cases fail rather than skip.
- Pre-existing pass-shaped files cannot substitute for current attempts.
- Default API/E2E, nonadjacent mixed, and all-four selections use one policy
  and one runtime selection.
- All fifteen non-empty selections and reverse-order inputs normalize to exact
  canonical policy bytes; policy replay rejects any broadened/missing/sibling/
  wrong-change pattern before write classification.
- Sibling writes fail and shared testdata writes pass.
- A content/mode/kind/symlink change to a clean, pre-dirty, untracked, or
  ignored path is detected, while an unchanged pre-dirty path is not
  attributed to the attempt.
- API entries require request plus response-dependent validation, E2E entries
  require navigation/action/page-state assertion, Fuzz entries consume and
  execute the bound generated schema case, and Performance tasks issue a
  `self.client` request; helpers, unmapped/empty/trivial/wrong-case/unbound
  entries do not count.
- `generation-join` waits for every active branch.

### 18.6 Resume and recovery

- Ordinary restart after reviewer/mechanical/gate/precheck runs only the next
  pending node.
- API/E2E fixer creates a new assurance epoch before codegen.
- Fuzz/Performance audited revision creates a new tree and evidence epoch.
- Knowledge remediation refreshes L1-dependent mechanical/gate evidence.
- Accept-risk cannot bypass capability/current-evidence preconditions.
- Every named crash cut in section 12.1 converges to one committed result; a recorded
  successful handler is not rerun.
- A pending graph wrapper resumes its existing child and never creates a
  duplicate child invocation.
- Compatible pinned bundles resume exact old topology; incompatible semantic
  (including topology-safety and commit-safety) or model epochs fail before
  definition-dependent recovery.
- V4/v5 report/terminal-only roots may continue under their historical rules,
  but pending assurance codegen/fixer work deterministically stops with
  `legacy_commit_safety_semantics_unbound` and D18's single audited
  replacement/stop exit; a topology receipt is necessary but not sufficient.
- API-only, E2E-only, and both-active codegen-fixer paths commit intent,
  target record evidence, join, aggregate safety, and v2 ledger events exactly
  once; imported codegen without physical authority runs no automatic fixer.
- Real progression-lock contention advances the independent effect-retry
  sidecar only at due times across restarts; release yields one domain event,
  one acknowledgement, and one successor.

### 18.7 Benchmark

- Four codegen tiers seed plan-ready/codegen-pending state.
- Their fully expanded parent chains contain no selected assurance completion.
- Existing complete L2 -> L3 run/full fixture inheritance and suites retain
  their previous seeded-codegen behavior.
- Scorers require the recorded current root invocation and committed chain.
- Applicable samples contain a new selected-layer test write and a bound
  summary, D16 manifest, `generated_files_candidate/v1` receipt, input snapshot,
  mapping, and write set.
- All four suite files hard-gate and regression-track the three current-chain
  metrics; deleting any configured metric fails suite validation.
- D17 copied configuration/change-location evidence, policy, before/after/
  diff, input-snapshot, and bounded export-manifest integrity are replayed
  before scoring.
- Seeded syntax/summary files alone score zero current-chain credit.
- Inapplicable scorer fixtures require current N/A and absence of
  reviewer/codegen attempts.

### 18.8 CI

The release gate remains:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest -q
bash scripts/packaging_smoke_test.sh
```

Focused suites run before the full gate so a failure is attributable to the
contract, topology, runtime, resume, or benchmark boundary.

## 19. Compatibility and Risks

### 19.1 Public codegen-only behavior

Multi-layer selection and the existing default are preserved. The eval path
becomes consistent with the public graph rather than narrowing it to a
single-layer benchmark assumption.

### 19.2 Historical replay

Strict current validation is intentionally stronger, but it cannot
retroactively change a pinned graph's recorded epoch. V6 unsafe bypasses are
fail-closed as `partial`. Existing unbound v5 ledgers remain parseable and keep
their frozen classifier—including known false negatives—rather than being
backfilled or reclassified; v4 behaves the same for its older display epoch.
Consumers see `semantics_bound == false` and must not treat either status as v6
proof. A pending v4/v5 assurance-codegen path requires the append-only v6
compatibility receipt and is blocked when that audit is unsafe or the legacy
profile cannot be reconstructed exactly. That receipt proves topology only and
is necessary, never sufficient, for live execution: v1-v5 roots do not bind
`runtime_commit_safety/v1`, so any remaining assurance codegen/fixer or other
validator/effect-bearing task stops before dispatch with
`legacy_commit_safety_semantics_unbound`. Only report/terminal-only remaining
work may continue under the existing compatibility boundary. The reachable
operator exits are D18's audited `rerun-v6` replacement or terminal `stop`;
there is no backfill. This intentional narrowing and stable code appear in
release notes. The v6 root metadata addition remains forward-written only.

### 19.3 Fixture cost

Real GraphRuntime matrices are slower than planner tests. The design keeps one
canonical harness, parameterizes the eight base cells, and uses representative
multi-layer combinations rather than every subset × recovery combination.
Full branch cross-products are unnecessary once per-layer behavior and join
semantics are independently proven.

### 19.4 Diagnostic stability

Structured codes and locators are compatibility surfaces for tests and
operators. Human detail text may improve without breaking tests. The compiler
sorts findings so schema mapping order cannot change output.

### 19.5 Contract over-expansion

The largest implementation risk is repairing invisible inputs with broad
reads. Parity and mutation guards explicitly reject that shortcut. Required
paths are narrowed by role, layer, and current change; writes stay narrower
than reads.

### 19.6 Test-only duplicate logic

The harness must call production loaders/checks/gates/workspaces and inspect
production ledger events. If a helper begins reproducing verdict expressions,
route decisions, or replay selection, it violates this design.

### 19.7 Snapshot and object-store retention

Current-attempt scoring now depends on content fingerprints, input snapshots,
and committed codegen write-set objects. Eval cleanup/copy routines retain the
exact evidence-export/v1 closure, never the entire object store. Missing,
extra, or pruned required objects fail evidence integrity explicitly. Raw
output grows by selected snapshots/write sets, only the plan/case/output blobs
needed by scoring, the root event slice, and two worktree manifests. Manifest
capture is O(files + bytes hashed), never follows symlinks, and fails at the
deterministic entry/byte caps in D12 instead of exhausting resources silently.

### 19.8 Healing authority compatibility

Removing agent-visible host links and agent-side `aa heal record-apply` closes
an existing authorization/atomicity hole. Fresh physically generated or
strictly bound reused tests retain automatic API/E2E healing through the new
intent/record chain. A failed workflow whose codegen exists only as an imported
completion can no longer enter automatic fixer execution because it has no
attempt/write-set authority; it ends with explicit
`unverified_imported_codegen`. Existing imported L2/L3 success/run behavior is
unchanged. Separately, a v1-v5 root stopped before assurance codegen or an
automatic fixer cannot enter the new validator/effect chain; it receives
`legacy_commit_safety_semantics_unbound` and exits only through D18's audited
single replacement or terminal stop. Both safe narrowings are intentional and
must appear in release notes.

## 20. Acceptance Criteria

1. Four independent canonical fixtures cross the complete production
   assurance boundary with the fixed check matrix.
2. Skill structured inputs/outputs and execution-contract reads/writes are
   closed under declared-read isolation without broad repository or
   cross-change scopes; no declared-only workspace exposes host runtime/
   coordinator symlinks, all sixteen targets retain exact bounded persona/
   attempt-directory dispatch, and canonical artifact bytes are stable across
   two registry-selected serializations.
3. All assurance skills treat workflow state as graph-owned and all shared
   factory examples use `tests/testdata/domain/**`; plan fixers consume a
   start/success/prompt-bound typed Runtime Context and never the host ledger.
4. Every current topology mutation in section 8.7 makes
   `compile_packaged_workflow(...)` fail with the expected structured owner.
5. Safe historical renames/equivalent predicates remain wired, marker-free old
   graphs remain legacy, and true bypasses become partial under v6, without
   changing frozen (possibly weaker) v4/v5 outcomes; v6 binds the topology-
   and commit-safety semantics objects, while v4/v5 report unbound semantics.
   A topology receipt is necessary but never sufficient for remaining
   validator/effect-bearing work: such roots stop with
   `legacy_commit_safety_semantics_unbound`, and D18 supersedes them exactly
   once into a bound v6 replacement or audited terminal stop without weakening
   the ordinary active/restart-once guards.
6. Codegen-only supports any non-empty selected-layer subset and preserves the
   `[api, e2e]` default across schema, CLI, eval, policy, and scorer.
7. The eval write policy permits only the current change, selected private test
   roots, shared testdata, and approved runtime coordination metadata; attempt
   output remains external, and content-aware snapshots detect clean,
   pre-dirty, untracked, and ignored-path changes. Scorer replay reconstructs
   the exact policy/diff from execution-bound D17 copied configuration/change-
   location evidence and rejects forged, archive-only, or broadened evidence.
8. The real GraphRuntime four-by-two matrix proves applicable/inapplicable
   attempt presence, N/A semantics, freshness, and no plan regeneration.
9. Representative multi-layer runs prove unselected absence and
   `generation-join` all-active behavior.
10. Ordinary, fixer, manual, knowledge, accept-risk/stop, named-crash-cut, and
    pinned resume tests pass against the packaged graph, including child
    wrapper recovery without duplicate child creation.
11. Workflow-codegen fixtures no longer import selected assurance/codegen
    completion through any parent tier, and benchmark success requires current
    physical attempts plus a path-specific D16 manifest and successful
    `generated_files_candidate/v1` receipt binding the input snapshot, mapping,
    write set, and layer-specific behavioral test/Locust-task evidence;
    existing complete L2/L3 run fixtures remain compatible.
12. API-only, E2E-only, and both-active codegen-fixer paths use the strict
    intent -> record -> active join -> aggregate safety chain, survive every
    allocation/record/effect-lock crash cut without duplicate v2 events, fold
    v2/legacy/mixed allocations through one normalized projection, and never
    require agent shell/host-ledger access.
13. Focused suites and all six repository CI gates pass without an external
    OpenCode service.

---

This increment converts the final assurance verification gap from
file-presence confidence into contract-closed, topology-checked, attempt-bound
runtime evidence.
