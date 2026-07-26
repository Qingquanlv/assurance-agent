# Retro / Issue Separation and Improvement Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the already-implemented legacy Retro Task 15 with a read-only Issue history seam, schema-v2 Retro evidence, and a project-scoped Improvement lifecycle without changing Issue execution inside the full workflow.

**Architecture:** Keep Issue as the sole owner of Observation, Occurrence, Problem assessment, Problem state, human decisions, resolution, and regression. A deterministic `operation:retro-collect` queries Issue, Workflow, and Eval history through typed read-only adapters and freezes a digest-pinned `RetroContext`; the LLM sees only that context and emits noncanonical candidates. A deterministic reconciler validates the whole batch and appends canonical events to a new Improvement Ledger, while independent review, eval, export, apply, and rollback adapters advance Improvement state without mutating Problem state.

**Tech Stack:** Python 3.11, Pydantic v2 strict/frozen models, Click, PyYAML/ruamel.yaml, append-only JSONL ledgers, GraphRuntime schema v2 and synchronized project resources, pytest, Ruff, Pyright, import-linter, uv.

## Global Constraints

- The approved design is `docs/superpowers/specs/2026-07-25-retro-issue-improvement-separation-design.md`; it overrides the legacy Retro work in Task 15 of `docs/superpowers/plans/2026-07-25-issue-lifecycle-and-problem-ledger.md`.
- Do not change the existing full-workflow ordering: every authoritative execution and healing rerun remains `execution -> inspect -> collect-observations -> analyze-issues -> reconcile-issues`, before report/archive.
- Retro remains a project-scoped independent entrypoint; no full-workflow graph may call Retro, Improvement review, or an Improvement delivery operation.
- New production code must never scan, parse, migrate, or consume an earlier `qa/retro/**` run. Reading predecessor outputs inside the current `qa/retro/<retro-id>/` is allowed.
- Issue remains the sole writer of `qa/changes/<change-id>/issues/**` and `qa/issues/**`; Improvement remains the sole writer of Improvement lifecycle fields under `qa/improvements/**`.
- `skill:aa-retro` reads only the current run's `context.json` and writes only `proposal-candidates.json` plus `retro-summary.md`.
- Project Improvement writes declare `synchronized: [project:qa/improvements/**]` and `exclusive: [project:improvement-registry]`.
- Candidate batches are all-or-nothing; zero candidates are a successful no-op; reconciliation retry identity is `retro_id + candidate_batch_digest`.
- A Candidate cannot contain Problem classification, severity, status, version, disposition, or an independent root-cause copy; it references immutable Issue IDs instead.
- No new dependency is added. All timestamps are injected in tests; all JSON/projection output is canonical, UTF-8, sorted, and byte-stable.
- Every task follows red-green-refactor, runs its focused tests, then commits with a conventional commit message. Do not combine tasks into one commit.
- All commands run from `/Users/lvqingquan/agent/assurance-agent`.

### Human-approved plan amendments (2026-07-26)

1. **State recovery edges:** `needs_rework -> proposed`, `awaiting_baseline -> evaluating`, `eval_error -> evaluating` (in addition to `-> superseded`).
2. **Incomplete integrity:** forbid only `domain_knowledge`; other ImprovementKinds stay allowed.
3. **Tasks 6–8:** continuous commits in one PR; do not merge/push an intermediate half-cutover trunk.
4. **Fingerprint:** omitting `problem_ids` is intentional; identical intent across Problems links evidence onto one Improvement (Task 13 acceptance must pin this).
5. **Workspace:** implement in the current checkout on `feature/python-migration-m2-m9` (no new worktree).

---

## File map and dependency order

| Area | Files | Responsibility |
|---|---|---|
| Improvement contracts | `assurance_agent/artifacts/models/improvements.py` | Strict Candidate, source-reference, verification, canonical projection, review, eval, and receipt models |
| Improvement core | `assurance_agent/workflow/improvements/{identity,transitions,events,projection,ledger,reconciler,review}.py` | Deterministic identity, state machine, append-only event protocol, pure replay, atomic store, reconciliation, human review |
| Issue seam | `assurance_agent/workflow/issues/{history_models,history}.py` | `IssueHistoryReader`, production stable-head adapter, in-memory test adapter |
| Retro evidence | `assurance_agent/retro/{window,workflow_history,eval_history,context}.py` | Window resolution, authoritative non-Issue history adapters, schema-v2 signal aggregation |
| Retro analysis | `assurance_agent/retro/candidates.py`, packaged `aa-retro/SKILL.md`, graph agent prompt | Candidate document parsing and LLM boundary |
| Graph integration | workflow schema, execution contracts, retro operation handlers | Three-node Retro graph whose collect operation contains the logical select-window/evidence-collection stages, plus independent Improvement review/delivery entrypoints |
| Delivery | `assurance_agent/workflow/improvements/{memory_delivery,change_delivery,knowledge_delivery}.py` | Narrow, type-specific eval/export/apply/rollback adapters |
| CLI/read models | `assurance_agent/commands/improvement_cmd.py` | Improvement list/show and graph invocation guidance; never reconstruct state from Retro runs |
| Clean cut | legacy Retro modules/tests plus repository guard | Remove consumed cursor, promotion history, cross-run scans, old enums and per-run delivery state |

The dependency order is deliberate: Tasks 1–3 establish the canonical Improvement vocabulary; Task 4 establishes the Issue read seam; Tasks 5–7 build context and Candidate boundaries; Tasks 8–11 add writes and lifecycle actions; Tasks 12–13 switch public surfaces and delete the legacy path.

---

### Task 1: Add strict Improvement and Retro-v2 artifact models

**Files:**
- Create: `assurance_agent/artifacts/models/improvements.py`
- Modify: `assurance_agent/artifacts/models/__init__.py`
- Modify: `assurance_agent/retro/types.py`
- Test: `tests/unit/artifacts/test_models_improvements.py`
- Test: `tests/unit/retro/test_context_models_v2.py`

**Interfaces:**
- Consumes: existing `DataKnowledgeProposal`, `NonEmptyStr`, and the repository convention `ConfigDict(frozen=True, extra="forbid")`.
- Produces: `ImprovementKind`, `DeliveryKind`, `ImprovementState`, `ImprovementSourceRefs`, `ImprovementVerification`, `ImprovementCandidate`, `ImprovementCandidateDocument`, `ImprovementProjection`, `ImprovementLedgerProjection`, `ImprovementReviewQueue`, `RetroWindow`, `RetroSourceManifest`, `RetroIntegrity`, `RetroSignalSet`, and `RetroContext` schema v2.

- [ ] **Step 1: Write failing model tests for the kind/delivery matrix and forbidden Problem copies.**

```python
def test_candidate_rejects_wrong_delivery_and_problem_fields() -> None:
    common = {
        "candidate_id": "IMP-CAND-1",
        "source_refs": {"problem_ids": ["PROB-1"]},
        "target": "assurance_agent/workflow/inspect",
        "rationale": "Repeated truncation",
        "proposed_change": "Preserve pytest E lines",
        "verification": {"suites": ["workflow-full"], "success_criteria": "No truncation"},
        "risk": "low",
        "confidence": "high",
    }
    with pytest.raises(ValidationError):
        ImprovementCandidate.model_validate(
            {**common, "kind": "workflow_improvement", "delivery": "memory_patch"}
        )
    with pytest.raises(ValidationError):
        ImprovementCandidate.model_validate(
            {
                **common,
                "kind": "workflow_improvement",
                "delivery": "change_draft",
                "severity": "high",
                "status": "in_progress",
                "root_cause": "copied Problem assessment",
            }
        )
```

- [ ] **Step 2: Run the focused tests and confirm the new imports fail.**

Run: `uv run pytest tests/unit/artifacts/test_models_improvements.py tests/unit/retro/test_context_models_v2.py -q`

Expected: collection fails because `artifacts.models.improvements` and the schema-v2 Retro types do not exist.

- [ ] **Step 3: Implement the strict enums, source refs, Candidate document, and canonical Improvement projections.**

```python
class ImprovementKind(StrEnum):
    PROMPT = "prompt_improvement"
    FIXTURE = "fixture_improvement"
    TEST = "test_improvement"
    WORKFLOW = "workflow_improvement"
    DOMAIN_KNOWLEDGE = "domain_knowledge"


class DeliveryKind(StrEnum):
    MEMORY_PATCH = "memory_patch"
    CHANGE_DRAFT = "change_draft"
    KNOWLEDGE_DELTA = "knowledge_delta"


class ImprovementState(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REWORK = "needs_rework"
    EVALUATING = "evaluating"
    EXPORTED = "exported"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    AWAITING_BASELINE = "awaiting_baseline"
    EVAL_ERROR = "eval_error"
    SUPERSEDED = "superseded"


ALLOWED_DELIVERIES: dict[ImprovementKind, frozenset[DeliveryKind]] = {
    ImprovementKind.PROMPT: frozenset({DeliveryKind.MEMORY_PATCH}),
    ImprovementKind.FIXTURE: frozenset({DeliveryKind.MEMORY_PATCH, DeliveryKind.CHANGE_DRAFT}),
    ImprovementKind.TEST: frozenset({DeliveryKind.MEMORY_PATCH, DeliveryKind.CHANGE_DRAFT}),
    ImprovementKind.WORKFLOW: frozenset({DeliveryKind.CHANGE_DRAFT}),
    ImprovementKind.DOMAIN_KNOWLEDGE: frozenset({DeliveryKind.KNOWLEDGE_DELTA}),
}


class ImprovementVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    suites: tuple[str, ...] = ()
    required_cases: tuple[str, ...] = ()
    success_criteria: str = Field(min_length=1)


class ImprovementSourceRefs(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    problem_ids: tuple[str, ...] = ()
    occurrence_ids: tuple[str, ...] = ()
    issue_event_ids: tuple[str, ...] = ()
    workflow_evidence_ids: tuple[str, ...] = ()
    eval_run_ids: tuple[str, ...] = ()

    def all_ids(self) -> tuple[str, ...]:
        return tuple(sorted(set(chain.from_iterable(self.model_dump().values()))))


class ImprovementCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    candidate_id: str = Field(min_length=1)
    kind: ImprovementKind
    delivery: DeliveryKind
    source_refs: ImprovementSourceRefs
    target: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    proposed_change: str = Field(min_length=1)
    knowledge_delta: DataKnowledgeProposal | None = None
    verification: ImprovementVerification
    risk: Literal["low", "medium", "high"]
    confidence: Literal["low", "medium", "high"]
    supersedes: str | None = None

    @model_validator(mode="after")
    def validate_delivery(self) -> Self:
        if self.delivery not in ALLOWED_DELIVERIES[self.kind]:
            raise ValueError(f"{self.kind} cannot use {self.delivery}")
        if not self.source_refs.all_ids():
            raise ValueError("candidate requires at least one source ref")
        if (self.knowledge_delta is not None) != (self.delivery is DeliveryKind.KNOWLEDGE_DELTA):
            raise ValueError("knowledge_delta payload is required only for knowledge_delta delivery")
        return self


class ImprovementProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    improvement_id: str
    fingerprint: str
    fingerprint_version: Literal["1"] = "1"
    kind: ImprovementKind
    delivery: DeliveryKind
    source_refs: ImprovementSourceRefs
    target: str
    rationale: str
    proposed_change: str
    knowledge_delta: DataKnowledgeProposal | None = None
    verification: ImprovementVerification
    risk: Literal["low", "medium", "high"]
    confidence: Literal["low", "medium", "high"]
    state: ImprovementState
    version: int = Field(ge=1)
    proposed_by_retro_ids: tuple[str, ...]
    supersedes: str | None = None
    last_event_id: str


class ImprovementLedgerProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["1"] = "1"
    last_seq: int = Field(ge=0)
    improvements: dict[str, ImprovementProjection]
    by_fingerprint: dict[str, str]


class ImprovementReviewQueue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["1"] = "1"
    improvement_ids: tuple[str, ...]
```

- [ ] **Step 4: Replace the old unversioned `RetroContext` shape with strict schema-v2 context types.**

```python
class RetroSelectionSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: Literal["change_ids", "time_range", "last"]
    requested_change_ids: tuple[str, ...] = ()
    requested_since: str | None = None
    requested_until: str | None = None
    requested_last: int | None = None


class RetroWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    selection: RetroSelectionSnapshot
    change_ids: tuple[str, ...]
    since: str | None = None
    until: str | None = None
    project_event_through: str | None = None


class RetroSourceDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["change_issue_ledger", "project_problem_ledger", "workflow_ledger", "eval_run"]
    change_id: str | None = None
    head_event_id: str | None = None
    sha256: str
    evidence_ids: tuple[str, ...] = ()


class RetroSourceManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    issue_slice_sha256: str
    issue_sources: tuple[RetroSourceDescriptor, ...]
    workflow_sources: tuple[RetroSourceDescriptor, ...]
    eval_sources: tuple[RetroSourceDescriptor, ...]

    def resolvable_ids(self) -> frozenset[str]:
        sources = (*self.issue_sources, *self.workflow_sources, *self.eval_sources)
        return frozenset(chain.from_iterable(source.evidence_ids for source in sources))


class RetroIntegrity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["complete", "incomplete"]
    reasons: tuple[str, ...] = ()


class RetroSignal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    signal_id: str
    source_refs: ImprovementSourceRefs
    metrics: dict[str, int | float | str]


class IssueRetroSignals(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    observation_distribution: tuple[RetroSignal, ...] = ()
    occurrence_trends: tuple[RetroSignal, ...] = ()
    assessment_corrections: tuple[RetroSignal, ...] = ()
    regressions: tuple[RetroSignal, ...] = ()
    review_decision_patterns: tuple[RetroSignal, ...] = ()
    resolution_outcomes: tuple[RetroSignal, ...] = ()
    repeated_not_an_issue: tuple[RetroSignal, ...] = ()


class WorkflowRetroSignals(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    gate_pushback: tuple[RetroSignal, ...] = ()
    healing_efficiency: tuple[RetroSignal, ...] = ()
    skill_execution_drift: tuple[RetroSignal, ...] = ()


class EvalRetroSignals(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    trends: tuple[RetroSignal, ...] = ()


class RetroSignalSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    issue: IssueRetroSignals
    workflow: WorkflowRetroSignals
    eval: EvalRetroSignals


class RetroContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["2"] = "2"
    retro_id: str
    generated_at: str
    window: RetroWindow
    source_manifest: RetroSourceManifest
    integrity: RetroIntegrity
    signals: RetroSignalSet
    signal_count: int = Field(ge=0)


class ImprovementCandidateDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["2"] = "2"
    retro_id: str
    context_sha256: str
    candidates: tuple[ImprovementCandidate, ...] = ()
```

Define the signal groups exactly as `signals.issue`, `signals.workflow`, and `signals.eval`. Every signal model carries immutable `source_refs`; `RetroSourceManifest.resolvable_ids()` returns the complete set accepted by Candidate validation.

- [ ] **Step 5: Run model tests and static checks.**

Run: `uv run pytest tests/unit/artifacts/test_models_improvements.py tests/unit/retro/test_context_models_v2.py -q && uv run ruff check assurance_agent/artifacts/models assurance_agent/retro/types.py tests/unit/artifacts/test_models_improvements.py tests/unit/retro/test_context_models_v2.py && uv run pyright`

Expected: all commands pass.

- [ ] **Step 6: Commit the model boundary.**

```bash
git add assurance_agent/artifacts/models/improvements.py assurance_agent/artifacts/models/__init__.py assurance_agent/retro/types.py tests/unit/artifacts/test_models_improvements.py tests/unit/retro/test_context_models_v2.py
git commit -m "feat: add improvement and retro v2 models"
```

---

### Task 2: Implement Improvement identity, transitions, and strict events

**Files:**
- Create: `assurance_agent/workflow/improvements/__init__.py`
- Create: `assurance_agent/workflow/improvements/identity.py`
- Create: `assurance_agent/workflow/improvements/transitions.py`
- Create: `assurance_agent/workflow/improvements/events.py`
- Test: `tests/unit/workflow/improvements/test_identity.py`
- Test: `tests/unit/workflow/improvements/test_transitions.py`
- Test: `tests/unit/workflow/improvements/test_events.py`

**Interfaces:**
- Consumes: `ImprovementCandidate`, `ImprovementSourceRefs`, `ImprovementState` from Task 1.
- Produces: `improvement_fingerprint(candidate, version="1") -> str`, `improvement_id_for_fingerprint(fingerprint) -> str`, `improvement_event_id(idempotency_key, event_type, ordinal) -> str`, `assert_improvement_transition(current, target) -> None`, `ImprovementEvent`, `IMPROVEMENT_EVENT_ADAPTER`, and `read_improvement_events(path) -> list[ImprovementEvent]`.

- [ ] **Step 1: Write failing tests for normalized identity and the complete transition matrix.**

```python
def test_fingerprint_ignores_retro_metadata_and_source_order(candidate) -> None:
    reordered = candidate.model_copy(
        update={
            "source_refs": candidate.source_refs.model_copy(
                update={"problem_ids": tuple(reversed(candidate.source_refs.problem_ids))}
            ),
            "rationale": "different wording",
            "confidence": "low",
        }
    )
    assert improvement_fingerprint(candidate) == improvement_fingerprint(reordered)


@pytest.mark.parametrize(
    ("source", "target"),
    [("proposed", "approved"), ("approved", "evaluating"),
     ("evaluating", "awaiting_baseline"), ("applied", "rolled_back"),
     ("rolled_back", "needs_rework")],
)
def test_allowed_transitions(source, target) -> None:
    assert_improvement_transition(ImprovementState(source), ImprovementState(target))
```

- [ ] **Step 2: Run the focused tests and confirm missing-module failures.**

Run: `uv run pytest tests/unit/workflow/improvements/test_identity.py tests/unit/workflow/improvements/test_transitions.py tests/unit/workflow/improvements/test_events.py -q`

Expected: collection fails because `workflow.improvements` has not been created.

- [ ] **Step 3: Implement versioned canonical fingerprinting and deterministic IDs.**

```python
def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def improvement_fingerprint(candidate: ImprovementCandidate, version: str = "1") -> str:
    payload = {
        "version": version,
        "kind": candidate.kind.value,
        "delivery": candidate.delivery.value,
        "target": _normalize(candidate.target),
        "intent": _normalize(candidate.proposed_change),
    }
    wire = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


def improvement_id_for_fingerprint(fingerprint: str) -> str:
    return f"IMP-{fingerprint[:20].upper()}"


def improvement_event_id(idempotency_key: str, event_type: str, ordinal: int) -> str:
    raw = f"{idempotency_key}\x1f{event_type}\x1f{ordinal}".encode("utf-8")
    return f"IMPEVT-{hashlib.sha256(raw).hexdigest()[:24].upper()}"
```

- [ ] **Step 4: Implement the exact state graph.**

```python
_ALLOWED: dict[ImprovementState, frozenset[ImprovementState]] = {
    ImprovementState.PROPOSED: frozenset({
        ImprovementState.APPROVED,
        ImprovementState.REJECTED,
        ImprovementState.NEEDS_REWORK,
        ImprovementState.SUPERSEDED,
    }),
    ImprovementState.APPROVED: frozenset({
        ImprovementState.EVALUATING,
        ImprovementState.EXPORTED,
        ImprovementState.SUPERSEDED,
    }),
    # Recovery edges (human-approved plan amendment 2026-07-26):
    # needs_rework -> proposed (re-open for review after rework)
    # awaiting_baseline -> evaluating (baseline becomes available)
    # eval_error -> evaluating (retry eval)
    ImprovementState.NEEDS_REWORK: frozenset({
        ImprovementState.PROPOSED,
        ImprovementState.SUPERSEDED,
    }),
    ImprovementState.EVALUATING: frozenset({
        ImprovementState.APPLIED,
        ImprovementState.ROLLED_BACK,
        ImprovementState.AWAITING_BASELINE,
        ImprovementState.EVAL_ERROR,
        ImprovementState.SUPERSEDED,
    }),
    ImprovementState.AWAITING_BASELINE: frozenset({
        ImprovementState.EVALUATING,
        ImprovementState.SUPERSEDED,
    }),
    ImprovementState.EVAL_ERROR: frozenset({
        ImprovementState.EVALUATING,
        ImprovementState.SUPERSEDED,
    }),
    ImprovementState.EXPORTED: frozenset({
        ImprovementState.APPLIED,
        ImprovementState.NEEDS_REWORK,
        ImprovementState.SUPERSEDED,
    }),
    ImprovementState.APPLIED: frozenset({
        ImprovementState.ROLLED_BACK,
        ImprovementState.SUPERSEDED,
    }),
    ImprovementState.ROLLED_BACK: frozenset({
        ImprovementState.NEEDS_REWORK,
        ImprovementState.SUPERSEDED,
    }),
    ImprovementState.REJECTED: frozenset(),
    ImprovementState.SUPERSEDED: frozenset(),
}
```

Reject self-transitions; duplicate event idempotency is handled by the Ledger, not the state machine.

- [ ] **Step 5: Implement strict JSONL event variants.**

All variants inherit a frozen, extra-forbid envelope with `schema_version: Literal["1.0"]`, `seq`, `event_id`, `idempotency_key`, `ts`, `improvement_id`, and `expected_improvement_version`. Define all eleven discriminated variants from the spec. `ImprovementProposedEvent` owns kind, delivery, target, proposed change, verification, risk, confidence, fingerprint, fingerprint version, source refs, Retro provenance, and optional `supersedes`; later events carry only data specific to the transition.

Use these exact payloads and state effects:

| Event type | Required payload after the envelope | State effect |
|---|---|---|
| `improvement_proposed` | `fingerprint`, `fingerprint_version`, `kind`, `delivery`, `source_refs`, `target`, `rationale`, `proposed_change`, optional `knowledge_delta`, `verification`, `risk`, `confidence`, `retro_id`, `candidate_id`, `context_sha256`, `candidate_batch_digest`, optional `supersedes` | create `proposed` |
| `improvement_evidence_linked` | `source_refs`, `retro_id`, `candidate_id`, `context_sha256`, `candidate_batch_digest` | no state change; union refs |
| `improvement_review_approved` | `who`, `reason`, `review_id` | `proposed -> approved` |
| `improvement_review_rejected` | `who`, `reason`, `review_id` | `proposed -> rejected` |
| `improvement_rework_requested` | `who`, `reason`, `review_id` | `proposed|exported|rolled_back -> needs_rework` |
| `improvement_eval_requested` | `eval_run_id`, `suites`, `staged_sha256`, optional `baseline_sha256` | `approved -> evaluating` |
| `improvement_eval_completed` | `eval_run_id`, `outcome: passed|regressed|awaiting_baseline|error`, `report_sha256`, `staged_sha256`, optional `baseline_sha256`, optional `error` | passed keeps `evaluating`; other outcomes select `rolled_back|awaiting_baseline|eval_error` |
| `improvement_exported` | `artifact_path`, `artifact_sha256` | `approved -> exported` |
| `improvement_applied` | `target`, `before_sha256`, `after_sha256`, `receipt_sha256` | `evaluating|exported -> applied` |
| `improvement_rolled_back` | `target`, `restored_sha256`, `reason` | `applied|evaluating -> rolled_back` |
| `improvement_superseded` | `superseded_by`, `reason` | any nonterminal state -> `superseded` |

```python
ImprovementEvent = Annotated[
    ImprovementProposedEvent
    | ImprovementEvidenceLinkedEvent
    | ImprovementReviewApprovedEvent
    | ImprovementReviewRejectedEvent
    | ImprovementReworkRequestedEvent
    | ImprovementEvalRequestedEvent
    | ImprovementEvalCompletedEvent
    | ImprovementExportedEvent
    | ImprovementAppliedEvent
    | ImprovementRolledBackEvent
    | ImprovementSupersededEvent,
    Field(discriminator="type"),
]
IMPROVEMENT_EVENT_ADAPTER = TypeAdapter(ImprovementEvent)
```

The strict reader rejects malformed JSON, blank/non-object lines, non-contiguous `seq`, duplicate `event_id` with different bytes, unknown event types, and undeclared fields by raising `ImprovementLedgerIntegrityError`.

- [ ] **Step 6: Run focused tests and static checks.**

Run: `uv run pytest tests/unit/workflow/improvements/test_identity.py tests/unit/workflow/improvements/test_transitions.py tests/unit/workflow/improvements/test_events.py -q && uv run ruff check assurance_agent/workflow/improvements tests/unit/workflow/improvements && uv run pyright`

Expected: all commands pass.

- [ ] **Step 7: Commit the Improvement protocol.**

```bash
git add assurance_agent/workflow/improvements tests/unit/workflow/improvements/test_identity.py tests/unit/workflow/improvements/test_transitions.py tests/unit/workflow/improvements/test_events.py
git commit -m "feat: define improvement event protocol"
```

---

### Task 3: Build the Improvement Ledger and byte-stable projections

**Files:**
- Create: `assurance_agent/workflow/improvements/projection.py`
- Create: `assurance_agent/workflow/improvements/ledger.py`
- Test: `tests/unit/workflow/improvements/test_projection.py`
- Test: `tests/unit/workflow/improvements/test_ledger.py`
- Test: `tests/integration/test_improvement_ledger_concurrency.py`

**Interfaces:**
- Consumes: `ImprovementEvent`, strict reader, transition validator, and canonical models from Tasks 1–2.
- Produces: `project_improvements(events) -> ImprovementLedgerProjection`, `project_improvement_review_queue(events) -> ImprovementReviewQueue`, and `ProjectImprovementStore.append_and_rebuild(events) -> ImprovementLedgerProjection`.

- [ ] **Step 1: Write failing replay tests covering evidence linking, state transitions, aliases, and byte stability.**

```python
def test_projection_links_evidence_without_duplicate_improvement(events) -> None:
    projection = project_improvements(events)
    assert tuple(projection.improvements) == ("IMP-ABC",)
    assert projection.improvements["IMP-ABC"].source_refs.problem_ids == ("PROB-1", "PROB-2")


def test_rebuild_is_byte_stable(tmp_path, events) -> None:
    store = ProjectImprovementStore(tmp_path)
    store.append_and_rebuild(events)
    first = (tmp_path / "qa/improvements/improvements.json").read_bytes()
    store.append_and_rebuild(events)
    assert (tmp_path / "qa/improvements/improvements.json").read_bytes() == first
```

- [ ] **Step 2: Run tests and verify projection/store imports fail.**

Run: `uv run pytest tests/unit/workflow/improvements/test_projection.py tests/unit/workflow/improvements/test_ledger.py -q`

Expected: collection fails on missing `projection.py` and `ledger.py`.

- [ ] **Step 3: Implement a pure reducer with optimistic versions.**

```python
def project_improvements(events: Sequence[ImprovementEvent]) -> ImprovementLedgerProjection:
    state: dict[str, ImprovementProjection] = {}
    seen_idempotency: dict[str, bytes] = {}
    for event in events:
        _assert_idempotency(event, seen_idempotency)
        current = state.get(event.improvement_id)
        _assert_expected_version(event, current)
        state[event.improvement_id] = _apply_event(current, event)
    return ImprovementLedgerProjection(
        schema_version="1",
        last_seq=events[-1].seq if events else 0,
        improvements={key: state[key] for key in sorted(state)},
        by_fingerprint={
            item.fingerprint: item.improvement_id
            for item in sorted(state.values(), key=lambda value: value.fingerprint)
        },
    )
```

`improvement_evidence_linked` unions and sorts each source-ref family. A passed `improvement_eval_completed` pins the successful staged/report digests while remaining `evaluating`; the later atomic apply appends `improvement_applied`. Other eval outcomes map to `rolled_back`, `awaiting_baseline`, or `eval_error`. Review queue contains only `proposed`, `needs_rework`, `awaiting_baseline`, and `eval_error`, sorted by Improvement ID.

- [ ] **Step 4: Implement append/filter/rebuild with the existing Issue store's atomic publication conventions.**

```python
class ProjectImprovementStore:
    def __init__(self, project_root: Path) -> None:
        self.root = project_root / "qa" / "improvements"

    def append_and_rebuild(self, events: Sequence[ImprovementEvent]) -> ImprovementLedgerProjection:
        existing = read_improvement_events(self.root / "events.jsonl")
        new_events = filter_idempotent_events(existing, events)
        combined = [*existing, *new_events]
        projection = project_improvements(combined)
        queue = project_improvement_review_queue(combined)
        atomic_append_jsonl(self.root / "events.jsonl", new_events)
        atomic_write_json(self.root / "improvements.json", projection.model_dump(mode="json"))
        atomic_write_json(self.root / "review-queue.json", queue.model_dump(mode="json"))
        return projection
```

Keep helper implementations local to the Improvement package; do not import private `_atomic_*` names from `workflow/issues/ledger.py`.

- [ ] **Step 5: Add a two-process test that appends the same fingerprint under the project lock.**

Use `ProjectResourceLocks` with token `project:improvement-registry`; both workers submit the same deterministic proposal event, then assert one Improvement, one proposed event, no sequence gaps, and valid projections.

- [ ] **Step 6: Run Ledger tests and static checks.**

Run: `uv run pytest tests/unit/workflow/improvements/test_projection.py tests/unit/workflow/improvements/test_ledger.py tests/integration/test_improvement_ledger_concurrency.py -q && uv run ruff check assurance_agent/workflow/improvements tests/unit/workflow/improvements tests/integration/test_improvement_ledger_concurrency.py && uv run pyright`

Expected: all commands pass.

- [ ] **Step 7: Commit the canonical store.**

```bash
git add assurance_agent/workflow/improvements/projection.py assurance_agent/workflow/improvements/ledger.py tests/unit/workflow/improvements/test_projection.py tests/unit/workflow/improvements/test_ledger.py tests/integration/test_improvement_ledger_concurrency.py
git commit -m "feat: add project improvement ledger"
```

---

### Task 4: Add the read-only IssueHistoryReader seam

**Files:**
- Create: `assurance_agent/workflow/issues/history_models.py`
- Create: `assurance_agent/workflow/issues/history.py`
- Modify: `assurance_agent/workflow/issues/__init__.py`
- Test: `tests/unit/workflow/issues/test_history.py`
- Test: `tests/integration/test_issue_history_stable_read.py`

**Interfaces:**
- Consumes: strict Change Issue/Project Problem event readers and pure projections already implemented under `workflow/issues`.
- Produces: `IssueWindowSelection`, `IssueEvidenceSlice`, `IssueHistoryReader` Protocol, `LedgerIssueHistoryReader`, `InMemoryIssueHistoryReader`, `IssueHistoryConflict`, and `IssueHistoryIntegrityError`.

- [ ] **Step 1: Write failing contract tests comparing production and in-memory adapters.**

```python
def test_file_and_memory_adapters_return_same_slice(issue_tree, typed_events) -> None:
    selection = IssueWindowSelection(change_ids=("RET-1",), project_event_through="PEVT-4")
    file_slice = LedgerIssueHistoryReader(issue_tree).read_window(selection)
    memory_slice = InMemoryIssueHistoryReader.from_events(typed_events).read_window(selection)
    assert file_slice.model_dump(mode="json") == memory_slice.model_dump(mode="json")
```

Also cover merge alias closure, a late review event referring to an older Change, resolved/regressed Problem events, `analysis_failed`, `project_sync_pending`, malformed JSONL, an unknown event schema, and a changed Project head during a read.

- [ ] **Step 2: Run tests and verify the new Interface is absent.**

Run: `uv run pytest tests/unit/workflow/issues/test_history.py tests/integration/test_issue_history_stable_read.py -q`

Expected: collection fails on missing history modules.

- [ ] **Step 3: Define the additive immutable query and slice models.**

```python
@dataclass(frozen=True)
class IssueWindowSelection:
    change_ids: tuple[str, ...]
    project_event_through: str | None = None
    event_since: str | None = None
    event_until: str | None = None
    include_late_review_closure: bool = False


class IssueHistoryReader(Protocol):
    def read_window(self, selection: IssueWindowSelection) -> IssueEvidenceSlice: ...
```

`IssueEvidenceSlice` contains schema version `"1"`, the frozen selection, typed source descriptors (`kind`, optional `change_id`, head event ID, SHA-256), explicit integrity status/reasons, Observations, Occurrences, read-only Problem snapshots, and relevant Problem events. It exposes `resolvable_ids()` and canonical bytes/digest helpers.

- [ ] **Step 4: Implement the in-memory adapter and reference-closure algorithm first.**

For explicit/last-N selection, include all selected Change Issue events, then every connected Problem lifecycle event through the pinned head. For since/until selection, include Issue events whose envelope `ts` is in the interval and recursively include the referenced Observation, Occurrence, Problem, merge target/alias, and source Change records required to explain each event. Return sorted IDs and canonical model ordering.

- [ ] **Step 5: Implement stable production reads.**

```python
def _stable_project_bytes(self, retries: int = 3) -> bytes:
    path = self._project_root / "qa/issues/events.jsonl"
    for _attempt in range(retries):
        before = path.read_bytes() if path.exists() else b""
        read_problem_events_from_bytes(before)
        after = path.read_bytes() if path.exists() else b""
        if before == after:
            return before
    raise IssueHistoryConflict("project Problem ledger changed during Retro collection")
```

Read Change ledgers strictly from archive-first `resolve_change(..., prefer="archive")`; do not read projections as authority. When an Issue projection exists, replay the authoritative events and reject a mismatched projection rather than trusting it. A corrupt ledger raises `IssueHistoryIntegrityError`. Analysis failure or pending sync remains valid evidence but marks the slice incomplete with a typed reason.

- [ ] **Step 6: Run interface tests and existing Issue regression tests.**

Run: `uv run pytest tests/unit/workflow/issues/test_history.py tests/integration/test_issue_history_stable_read.py tests/unit/workflow/issues tests/integration/test_issue_lifecycle_acceptance.py -q && uv run ruff check assurance_agent/workflow/issues tests/unit/workflow/issues tests/integration/test_issue_history_stable_read.py && uv run pyright && uv run lint-imports`

Expected: all commands pass; no existing Issue writer or full-workflow behavior changes.

- [ ] **Step 7: Commit the Issue read seam.**

```bash
git add assurance_agent/workflow/issues/history_models.py assurance_agent/workflow/issues/history.py assurance_agent/workflow/issues/__init__.py tests/unit/workflow/issues/test_history.py tests/integration/test_issue_history_stable_read.py
git commit -m "feat: expose read-only issue history"
```

---

### Task 5: Implement deterministic Retro window and non-Issue history adapters

**Files:**
- Create: `assurance_agent/retro/window.py`
- Create: `assurance_agent/retro/workflow_history.py`
- Create: `assurance_agent/retro/eval_history.py`
- Modify: `assurance_agent/retro/nightly/types.py`
- Test: `tests/unit/retro/test_window.py`
- Test: `tests/unit/retro/test_workflow_history.py`
- Test: `tests/unit/retro/test_eval_history.py`

**Interfaces:**
- Consumes: strict workflow event envelopes, archive-first Change resolution, Eval `report.json` models, and Task 1 Retro source models.
- Produces: `RetroWindowSelection`, `ResolvedRetroWindow`, `WorkflowHistoryReader`, `EvalHistoryReader`, `LedgerWorkflowHistoryReader`, `FileEvalHistoryReader`, and in-memory adapters for both.

- [ ] **Step 1: Write failing tests for the three mutually exclusive window modes.**

```python
def test_window_modes_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="exactly one window mode"):
        RetroWindowSelection(change_ids=("RET-1",), since="2026-07-01T00:00:00Z", last=None)


def test_last_n_uses_terminal_event_time_not_directory_mtime(history) -> None:
    resolved = resolve_retro_window(RetroWindowSelection(last=2), workflow_history=history)
    assert resolved.change_ids == ("RET-2", "RET-3")
```

Cover explicit IDs sorted/deduplicated, closed since/until boundaries, omitted-until source-head pinning, stable tie-break by Change ID, missing workflow source as a degraded reason, and no consumed marking.

- [ ] **Step 2: Run tests and verify the adapters are absent.**

Run: `uv run pytest tests/unit/retro/test_window.py tests/unit/retro/test_workflow_history.py tests/unit/retro/test_eval_history.py -q`

Expected: collection fails on missing modules.

- [ ] **Step 3: Implement window selection with one canonical constructor.**

```python
class RetroWindowSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    change_ids: tuple[str, ...] = ()
    since: str | None = None
    until: str | None = None
    last: int | None = Field(default=10, ge=1)

    @model_validator(mode="after")
    def exactly_one_mode(self) -> Self:
        modes = (bool(self.change_ids), self.since is not None or self.until is not None, self.last is not None)
        if sum(modes) != 1:
            raise ValueError("exactly one window mode is required")
        return self
```

For CLI/default construction pass `last=10`; when explicit IDs or dates are supplied, pass `last=None`. `ResolvedRetroWindow` freezes sorted Change IDs, mode, effective since/until, workflow head IDs/digests, and integrity reasons.

- [ ] **Step 4: Implement strict Workflow and Eval readers.**

`LedgerWorkflowHistoryReader.list_terminal_changes()` reads authoritative event `ts` values and terminal graph/audit events, never directory mtimes. `read_window()` extracts gate pushback, healing outcomes, and `skill_loaded=false` evidence using immutable event IDs/digests. `FileEvalHistoryReader.read_window()` validates each selected report, uses `run_id` as the reference, and records a concrete missing/corrupt-source reason instead of silently returning an apparently complete empty history.

- [ ] **Step 5: Remove consumed-state fields from `NightlyOptions` and make the resolver reusable by graph and CLI callers.**

Keep only `retro_id`, `change_ids`, `since`, `until`, `last`, `dry_run`, and agent selection in Retro invocation options. Do not delete legacy runtime modules in this task; Task 12 performs the clean cut after the new graph is active.

- [ ] **Step 6: Run focused tests and static checks.**

Run: `uv run pytest tests/unit/retro/test_window.py tests/unit/retro/test_workflow_history.py tests/unit/retro/test_eval_history.py -q && uv run ruff check assurance_agent/retro tests/unit/retro/test_window.py tests/unit/retro/test_workflow_history.py tests/unit/retro/test_eval_history.py && uv run pyright`

Expected: all commands pass.

- [ ] **Step 7: Commit the history adapters.**

```bash
git add assurance_agent/retro/window.py assurance_agent/retro/workflow_history.py assurance_agent/retro/eval_history.py assurance_agent/retro/nightly/types.py tests/unit/retro/test_window.py tests/unit/retro/test_workflow_history.py tests/unit/retro/test_eval_history.py
git commit -m "feat: add deterministic retro history windows"
```

---

### Task 6: Build schema-v2 RetroContext through typed readers

**Files:**
- Create: `assurance_agent/retro/context.py`
- Modify: `assurance_agent/retro/collect_stage.py`
- Modify: `assurance_agent/workflow/graph/handlers/retro_ops.py`
- Test: `tests/unit/retro/test_context.py`
- Test: `tests/unit/retro/test_collect_stage_v2.py`
- Test: `tests/unit/workflow/graph/test_retro_ops.py`

**Interfaces:**
- Consumes: the approved `build_retro_context(selection, *, issue_history, workflow_history, eval_history)` reader-injection seam from Tasks 4–5; `retro_id` and `generated_at` are additional injected keyword-only values, never read from the filesystem or wall clock inside the pure function.
- Produces: a pure schema-v2 aggregator, `run_retro_collect(...) -> RetroCollectResult`, and an immutable current-run `context.json`.

- [ ] **Step 1: Write failing signal and failure-semantics tests using only in-memory readers.**

```python
def test_context_aggregates_issue_signals_without_problem_copies(readers) -> None:
    context = build_retro_context(
        RetroWindowSelection(change_ids=("RET-1", "RET-2"), last=None),
        issue_history=readers.issues,
        workflow_history=readers.workflow,
        eval_history=readers.eval,
        retro_id="retro-1",
        generated_at="2026-07-25T00:00:00Z",
    )
    assert context.schema_version == "2"
    assert context.signals.issue.occurrence_trends[0].source_refs.problem_ids == ("PROB-1",)
    assert "severity" not in context.signals.issue.occurrence_trends[0].model_dump()
```

Add cases for assessment corrections, regressions, review decisions, resolution outcomes, repeated not-an-issue, gate pushback, healing efficiency, skill execution drift, eval trend, source-ID resolvability, byte-identical output, and zero-signal success.

- [ ] **Step 2: Run tests and verify the old aggregator cannot satisfy v2.**

Run: `uv run pytest tests/unit/retro/test_context.py tests/unit/retro/test_collect_stage_v2.py tests/unit/workflow/graph/test_retro_ops.py -q`

Expected: failures show missing schema-v2 context fields and the old direct filesystem coupling.

- [ ] **Step 3: Implement the pure aggregator.**

```python
def build_retro_context(
    selection: RetroWindowSelection,
    *,
    issue_history: IssueHistoryReader,
    workflow_history: WorkflowHistoryReader,
    eval_history: EvalHistoryReader,
    retro_id: str,
    generated_at: str,
) -> RetroContext:
    window = resolve_retro_window(selection, workflow_history=workflow_history)
    issue_slice = issue_history.read_window(window.to_issue_selection())
    workflow_slice = workflow_history.read_window(window)
    eval_slice = eval_history.read_window(window)
    manifest = build_source_manifest(issue_slice, workflow_slice, eval_slice)
    signals = aggregate_signals(issue_slice, workflow_slice, eval_slice)
    assert_signal_refs_resolve(signals, manifest)
    return RetroContext(
        retro_id=retro_id,
        generated_at=generated_at,
        window=window.to_context_window(),
        source_manifest=manifest,
        integrity=combine_integrity(issue_slice, workflow_slice, eval_slice),
        signals=signals,
        signal_count=count_signals(signals),
    )
```

- [ ] **Step 4: Rewire collect to construct production adapters and write only current `context.json`.**

Remove calls to `read_state`, `enumerate_candidates`, `snapshot_unarchived_evidence`, `mark_consumed_change`, and `complete_retro_stage`. If `context.json` already exists with different bytes, raise `RetroContextImmutableError`; identical bytes are an idempotent retry. `IssueHistoryIntegrityError` is a hard failure before agent invocation. Incomplete Issue analysis/sync marks context incomplete; it does not abort collection.

- [ ] **Step 5: Enforce process-only Candidate eligibility at the context boundary.**

Expose `context.allows_domain_knowledge` only when all Issue source integrity is complete.
Human-approved amendment (2026-07-26): when integrity is incomplete, **only**
`domain_knowledge` Candidates are forbidden; `prompt_improvement`,
`fixture_improvement`, `test_improvement`, and `workflow_improvement` remain
eligible. Preserve specific degraded reasons for missing Workflow/Eval sources.
Do not turn those reasons into fabricated zero-valued signals.

- [ ] **Step 6: Run collect tests and regression tests.**

Run: `uv run pytest tests/unit/retro/test_context.py tests/unit/retro/test_collect_stage_v2.py tests/unit/workflow/graph/test_retro_ops.py tests/unit/workflow/issues/test_history.py -q && uv run ruff check assurance_agent/retro/context.py assurance_agent/retro/collect_stage.py assurance_agent/workflow/graph/handlers/retro_ops.py tests/unit/retro/test_context.py tests/unit/retro/test_collect_stage_v2.py && uv run pyright && uv run lint-imports`

Expected: all commands pass.

- [ ] **Step 7: Commit the evidence module.**

```bash
git add assurance_agent/retro/context.py assurance_agent/retro/collect_stage.py assurance_agent/workflow/graph/handlers/retro_ops.py tests/unit/retro/test_context.py tests/unit/retro/test_collect_stage_v2.py tests/unit/workflow/graph/test_retro_ops.py
git commit -m "refactor: collect retro evidence through typed readers"
```

---

### Task 7: Replace legacy Retro proposals with Candidate schema v2

**Files:**
- Create: `assurance_agent/retro/candidates.py`
- Modify: `assurance_agent/_resources/skills/aa-retro/SKILL.md`
- Modify: `assurance_agent/retro/nightly/agent.py`
- Modify: `assurance_agent/workflow/graph/handlers/agent.py`
- Test: `tests/unit/retro/test_candidates.py`
- Modify: `tests/unit/retro/nightly/test_agent.py`
- Modify: `tests/unit/workflow/graph/test_task_runner.py`
- Modify: `tests/unit/test_skills_content.py`

**Interfaces:**
- Consumes: `RetroContext`, `ImprovementCandidateDocument`, and `RetroSourceManifest.resolvable_ids()` from Tasks 1 and 6.
- Produces: `read_candidate_document(retro_dir)`, `validate_candidate_document(context, document)`, `candidate_batch_digest(document)`, and an LLM contract restricted to current-run inputs/outputs.

- [ ] **Step 1: Write failing batch-validation tests.**

```python
def test_candidate_batch_is_rejected_as_a_unit(context, valid_candidate) -> None:
    bad = valid_candidate.model_copy(
        update={"source_refs": ImprovementSourceRefs(problem_ids=("PROB-MISSING",))}
    )
    document = ImprovementCandidateDocument(
        retro_id=context.retro_id,
        context_sha256="wrong",
        candidates=(valid_candidate, bad),
    )
    with pytest.raises(CandidateBatchInvalid) as error:
        validate_candidate_document(context, document)
    assert {item.code for item in error.value.errors} == {"context_digest_mismatch", "unknown_source_ref"}
```

Also test the compatibility matrix, duplicate Candidate IDs, duplicate Candidate fingerprints within one batch, forbidden extra Problem fields, incomplete context blocking knowledge, and knowledge L2 validation.

- [ ] **Step 2: Run tests and verify missing Candidate reader failures.**

Run: `uv run pytest tests/unit/retro/test_candidates.py tests/unit/retro/nightly/test_agent.py tests/unit/workflow/graph/test_task_runner.py tests/unit/test_skills_content.py -q`

Expected: failures reference old `proposals.json`, old enum values, and broad read permissions.

- [ ] **Step 3: Implement strict read/digest/whole-batch validation.**

```python
def validate_candidate_document(context: RetroContext, document: ImprovementCandidateDocument) -> None:
    errors: list[CandidateValidationError] = []
    if document.retro_id != context.retro_id:
        errors.append(CandidateValidationError(code="retro_id_mismatch"))
    if document.context_sha256 != context_sha256(context):
        errors.append(CandidateValidationError(code="context_digest_mismatch"))
    resolvable = context.source_manifest.resolvable_ids()
    for candidate in document.candidates:
        unknown = set(candidate.source_refs.all_ids()) - resolvable
        if unknown:
            errors.append(CandidateValidationError(code="unknown_source_ref", ids=tuple(sorted(unknown))))
        errors.extend(validate_knowledge_eligibility(candidate, context))
    if errors:
        raise CandidateBatchInvalid(tuple(errors))
```

- [ ] **Step 4: Rewrite the packaged skill and both prompt builders.**

The skill must say, without optional context, that it reads only `qa/retro/<retro-id>/context.json`, writes `proposal-candidates.json` and `retro-summary.md`, uses the five `ImprovementKind` values and three `DeliveryKind` values, cites manifest-resolvable IDs, and never declares Problem lifecycle fields. Remove instructions to inspect previous promotions, archives, Issue Ledgers, memory files, workflow schema, product files, or data knowledge.

```python
return (
    "Call skill(name='aa-retro'). "
    f"Read only qa/retro/{retro_id}/context.json. "
    f"Write qa/retro/{retro_id}/proposal-candidates.json and qa/retro/{retro_id}/retro-summary.md. "
    "Set schema_version='2' and pin context_sha256. Do not read any other Retro run, "
    "qa/issues, raw archive, qa/improvements, memory, data knowledge, or project source files."
)
```

- [ ] **Step 5: Run Candidate/prompt tests and static checks.**

Run: `uv run pytest tests/unit/retro/test_candidates.py tests/unit/retro/nightly/test_agent.py tests/unit/workflow/graph/test_task_runner.py tests/unit/test_skills_content.py -q && uv run ruff check assurance_agent/retro/candidates.py assurance_agent/retro/nightly/agent.py assurance_agent/workflow/graph/handlers/agent.py tests/unit/retro/test_candidates.py && uv run pyright`

Expected: all commands pass and packaged skill tests assert no broad historical reads.

- [ ] **Step 6: Commit the LLM boundary.**

```bash
git add assurance_agent/retro/candidates.py assurance_agent/_resources/skills/aa-retro/SKILL.md assurance_agent/retro/nightly/agent.py assurance_agent/workflow/graph/handlers/agent.py tests/unit/retro/test_candidates.py tests/unit/retro/nightly/test_agent.py tests/unit/workflow/graph/test_task_runner.py tests/unit/test_skills_content.py
git commit -m "refactor: emit retro improvement candidates"
```

---

### Task 8: Reconcile Candidate batches into the Improvement Ledger

**Files:**
- Create: `assurance_agent/workflow/improvements/reconciler.py`
- Modify: `assurance_agent/retro/accept_stage.py`
- Test: `tests/unit/workflow/improvements/test_reconciler.py`
- Modify: `tests/unit/retro/test_accept_stage.py`
- Test: `tests/integration/test_improvement_reconcile_concurrency.py`

**Interfaces:**
- Consumes: validated Candidate documents, RetroContext, identity functions, current Improvement projection, strict events, and `ProjectImprovementStore`.
- Produces: `ImprovementReconciliationPlan`, `reconcile_improvement_candidates(...)`, `run_improvement_reconcile(...)`, current-run `accept-status.json`, and `review-queue.md`.

- [ ] **Step 1: Write failing pure-reconciler tests.**

```python
def test_existing_fingerprint_adds_evidence_only(context, document, current) -> None:
    plan = reconcile_improvement_candidates(document, context, current)
    assert [event.type for event in plan.events] == ["improvement_evidence_linked"]
    assert plan.events[0].improvement_id == current.by_fingerprint[document_fingerprint(document)]


def test_same_retry_is_byte_identical(context, document, current) -> None:
    first = reconcile_improvement_candidates(document, context, current)
    second = reconcile_improvement_candidates(document, context, current)
    assert first.model_dump_json() == second.model_dump_json()
```

Add tests for a new fingerprint, duplicate refs causing no event, explicit supersedes, invalid batch causing zero events/files, same fingerprint across two Retro IDs, and a semantic intent change producing a new Improvement.

- [ ] **Step 2: Run tests and verify reconciliation is absent.**

Run: `uv run pytest tests/unit/workflow/improvements/test_reconciler.py tests/unit/retro/test_accept_stage.py tests/integration/test_improvement_reconcile_concurrency.py -q`

Expected: failures reference the old per-run acceptance/promotion path.

- [ ] **Step 3: Implement pure reconciliation.**

```python
def reconcile_improvement_candidates(
    candidates: ImprovementCandidateDocument,
    context: RetroContext,
    current: ImprovementLedgerProjection,
) -> ImprovementReconciliationPlan:
    validate_candidate_document(context, candidates)
    batch_digest = candidate_batch_digest(candidates)
    key = f"{context.retro_id}:{batch_digest}"
    events: list[ImprovementEvent] = []
    for ordinal, candidate in enumerate(candidates.candidates):
        fingerprint = improvement_fingerprint(candidate)
        existing = current.by_fingerprint.get(fingerprint)
        events.extend(
            plan_link_or_propose(
                candidate,
                existing,
                idempotency_key=key,
                ordinal=ordinal,
                next_seq=current.last_seq + len(events) + 1,
            )
        )
    return ImprovementReconciliationPlan(
        retro_id=context.retro_id,
        candidate_batch_digest=batch_digest,
        idempotency_key=key,
        events=tuple(events),
    )
```

- [ ] **Step 4: Replace acceptance with validate-plan-lock-append-rebuild-receipt.**

Validate the complete document before acquiring the project lock. Under `project:improvement-registry`, reload current events/projection, recompute the plan, append and rebuild, then atomically write `accept-status.json` with context digest, batch digest, result, canonical Improvement IDs, and event IDs. On retry, first locate Ledger events with the same idempotency key and rebuild the same receipt from those event bytes; do not replace it with an empty-event receipt after the first append. Write `review-queue.md` only as a projection of those canonical IDs. On validation failure, write a failed receipt only in the current run and perform zero project writes.

- [ ] **Step 5: Test true concurrency and retry.**

Two processes reconcile equivalent Candidates from different Retro IDs. Assert one `improvement_proposed`, one canonical Improvement, at most one evidence link per source-ref set, contiguous sequence numbers, and byte-stable projections. Re-run either batch and assert no new bytes.

- [ ] **Step 6: Run reconciliation tests and static checks.**

Run: `uv run pytest tests/unit/workflow/improvements/test_reconciler.py tests/unit/retro/test_accept_stage.py tests/integration/test_improvement_reconcile_concurrency.py -q && uv run ruff check assurance_agent/workflow/improvements/reconciler.py assurance_agent/retro/accept_stage.py tests/unit/workflow/improvements/test_reconciler.py tests/unit/retro/test_accept_stage.py tests/integration/test_improvement_reconcile_concurrency.py && uv run pyright`

Expected: all commands pass.

- [ ] **Step 7: Commit deterministic reconciliation.**

```bash
git add assurance_agent/workflow/improvements/reconciler.py assurance_agent/retro/accept_stage.py tests/unit/workflow/improvements/test_reconciler.py tests/unit/retro/test_accept_stage.py tests/integration/test_improvement_reconcile_concurrency.py
git commit -m "feat: reconcile retro candidates into improvements"
```

---

### Task 9: Switch the Retro graph and execution contracts to the closed loop

**Files:**
- Modify: `assurance_agent/_resources/schemas/workflow-schema.yaml`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Modify: `assurance_agent/workflow/graph/contracts.py`
- Modify: `assurance_agent/workflow/graph/planner.py`
- Modify: `assurance_agent/workflow/graph/handlers/agent.py`
- Modify: `assurance_agent/workflow/graph/handlers/retro_ops.py`
- Modify: `assurance_agent/workflow/graph/handlers/operation.py`
- Modify: `tests/unit/workflow/graph/test_canonical_schema_v2.py`
- Modify: `tests/unit/workflow/graph/test_contracts.py`
- Modify: `tests/unit/workflow/graph/test_planner.py`
- Modify: `tests/unit/workflow/graph/test_workspace.py`
- Modify: `tests/unit/workflow/graph/test_retro_workflow.py`
- Test: `tests/integration/test_retro_improvement_workflow.py`

**Interfaces:**
- Consumes: Tasks 6–8 operations and GraphRuntime synchronized project publication already used by Issue reconciliation.
- Produces: canonical `collect-retro-evidence -> propose-improvements -> reconcile-improvements -> END` graph and least-privilege contracts. Inside `operation:retro-collect`, `select-window -> collect-retro-evidence` remains the two-stage deterministic flow; this preserves the design rule that one Adapter alone reads Issue/Workflow/Eval authority.

- [ ] **Step 1: Write failing topology and authorization tests.**

```python
def test_retro_graph_is_independent_and_closed(schema) -> None:
    retro = schema.graphs["retro-workflow"]
    assert tuple(retro.nodes) == ("collect-retro-evidence", "propose-improvements", "reconcile-improvements")
    full_targets = {node.uses for graph in full_graph_closure(schema) for node in graph.nodes.values()}
    assert not any("retro" in target or "improvement" in target for target in full_targets)


def test_retro_agent_can_only_read_current_context(contracts) -> None:
    task = planned_retro_agent_task(contracts, retro_id="retro-current")
    assert tuple((path.root, path.pattern) for path in task.resources.reads) == (
        ("project", "qa/retro/retro-current/context.json"),
    )
    assert all("retro-other" not in path.pattern for path in task.resources.reads)
```

Also assert reconcile synchronization/exclusivity, no write access to `qa/issues/**`, memory, data knowledge, or product files, and no `project:qa/retro/**` wildcard read for the agent.

- [ ] **Step 2: Run graph tests and observe legacy topology/contract failures.**

Run: `uv run pytest tests/unit/workflow/graph/test_canonical_schema_v2.py tests/unit/workflow/graph/test_contracts.py tests/unit/workflow/graph/test_planner.py tests/unit/workflow/graph/test_workspace.py tests/unit/workflow/graph/test_retro_workflow.py tests/integration/test_retro_improvement_workflow.py -q`

Expected: failures show the current three-node graph, `proposals.json`, and broad Retro/Issue reads.

- [ ] **Step 3: Make `operation:retro-collect` own both deterministic collection stages.**

The operation first calls `resolve_retro_window`, immediately freezes the selected Change IDs and source heads in memory, and then passes that immutable result to the three readers and context aggregator. Its only artifact is immutable `context.json`; `window` and source pins are fields inside that file. Zero signals route from collect to `END`; dry-run also routes to `END`. Otherwise propose writes Candidate+summary and reconcile writes receipt+queue plus Improvement Ledger.

```yaml
retro-workflow:
  nodes:
    collect-retro-evidence: {uses: operation:retro-collect, outputs: [project:qa/retro/${params.retro_id}/context.json]}
    propose-improvements:
      uses: skill:aa-retro
      outputs: [project:qa/retro/${params.retro_id}/proposal-candidates.json, project:qa/retro/${params.retro_id}/retro-summary.md]
    reconcile-improvements:
      uses: operation:reconcile-improvements
      outputs: [project:qa/retro/${params.retro_id}/accept-status.json, project:qa/retro/${params.retro_id}/review-queue.md, project:qa/improvements/**]
```

- [ ] **Step 4: Narrow runtime resource claims to expanded current-run paths.**

The static registry uses the safe one-segment bounds `project:qa/retro/*/context.json`, `project:qa/retro/*/proposal-candidates.json`, and `project:qa/retro/*/retro-summary.md`. The Retro nodes additionally declare `${params.retro_id}` resources. Extend non-fan-out task planning to expand `definition.resources`, validate every expanded read/write is covered by the static contract, and replace the task's broad read claims with those concrete paths. Preserve concrete expanded outputs in writes/authorization. Make both Agent and operation workspace creation consume `task.resources`; a sibling Retro directory must not be materialized into the task workspace.

```python
def narrow_claims(
    base: ResourceClaims,
    *,
    reads: tuple[ResourcePath, ...],
    writes: tuple[ResourcePath, ...],
    outputs: tuple[ResourcePath, ...],
) -> ResourceClaims:
    if not all(any(path_covers(bound, item) for bound in base.reads) for item in reads):
        raise ContractError("expanded read exceeds the static execution contract")
    requested_writes = (*writes, *outputs)
    if not all(
        any(path_covers(bound, item) for bound in base.authorization_writes)
        for item in requested_writes
    ):
        raise ContractError("expanded write exceeds the static execution contract")
    concrete_writes = tuple(dict.fromkeys((*writes, *outputs)))
    return ResourceClaims(
        reads=reads,
        writes=concrete_writes,
        synchronized=base.synchronized,
        exclusive=base.exclusive,
        authorization_writes=concrete_writes,
    )
```

- [ ] **Step 5: Narrow contracts and enable project synchronization.**

`retro-collect` is the only Retro Adapter that reads selected Change Issue/execution/workflow evidence, `qa/issues/**`, and Eval history; it writes only current `context.json`. `aa-retro` has the exact current-run read/write paths. `reconcile-improvements` reads current context/candidates plus `qa/improvements/**`, writes only current receipt/queue plus `qa/improvements/**`, and declares the required synchronized path/token.

- [ ] **Step 6: Add end-to-end assertions for retries and failures.**

Test zero-signal no-agent behavior, corrupt Issue Ledger hard failure before agent invocation, incomplete Issue context allowing only process improvements, invalid Candidate batch producing no project write, and a successful run producing all five current-run artifacts plus one canonical Improvement.

- [ ] **Step 7: Run graph/integration tests and static checks.**

Run: `uv run pytest tests/unit/workflow/graph/test_canonical_schema_v2.py tests/unit/workflow/graph/test_contracts.py tests/unit/workflow/graph/test_planner.py tests/unit/workflow/graph/test_workspace.py tests/unit/workflow/graph/test_retro_workflow.py tests/integration/test_retro_improvement_workflow.py -q && uv run ruff check assurance_agent/workflow/graph tests/unit/workflow/graph tests/integration/test_retro_improvement_workflow.py && uv run pyright && uv run lint-imports`

Expected: all commands pass.

- [ ] **Step 8: Commit the graph cutover.**

```bash
git add assurance_agent/_resources/schemas/workflow-schema.yaml assurance_agent/_resources/schemas/execution-contracts.yaml assurance_agent/workflow/graph/contracts.py assurance_agent/workflow/graph/planner.py assurance_agent/workflow/graph/handlers/agent.py assurance_agent/workflow/graph/handlers/retro_ops.py assurance_agent/workflow/graph/handlers/operation.py tests/unit/workflow/graph/test_canonical_schema_v2.py tests/unit/workflow/graph/test_contracts.py tests/unit/workflow/graph/test_planner.py tests/unit/workflow/graph/test_workspace.py tests/unit/workflow/graph/test_retro_workflow.py tests/integration/test_retro_improvement_workflow.py
git commit -m "feat: close retro into improvement reconciliation"
```

---

### Task 10: Add the independent Improvement review workflow

**Files:**
- Create: `assurance_agent/workflow/improvements/review.py`
- Modify: `assurance_agent/artifacts/models/improvements.py`
- Modify: `assurance_agent/workflow/graph/handlers/operation.py`
- Modify: `assurance_agent/_resources/schemas/workflow-schema.yaml`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Modify: `assurance_agent/commands/workflow_cmd.py`
- Test: `tests/unit/workflow/improvements/test_review.py`
- Test: `tests/integration/test_improvement_review_workflow.py`

**Interfaces:**
- Consumes: Improvement projection/store and GraphRuntime repeatable interrupt flow.
- Produces: `build_improvement_review_context(...)`, `validate_improvement_review_action(...)`, `operation:load-improvement-review-context`, `operation:apply-improvement-review`, and repeatable `improvement-review` entrypoint.

- [ ] **Step 1: Write failing review/action/version tests.**

```python
@pytest.mark.parametrize(
    ("action", "event_type"),
    [("approve", "improvement_review_approved"),
     ("reject", "improvement_review_rejected"),
     ("request_rework", "improvement_rework_requested"),
     ("supersede", "improvement_superseded")],
)
def test_review_action_builds_typed_event(projection, action, event_type) -> None:
    event = validate_improvement_review_action(
        projection, action=action, reason="reviewed", who="alice", expected_version=projection.version
    )
    assert event.type == event_type
```

Add stale-version rejection, terminal-state rejection, delivery-specific advice fields, and proof that no `qa/issues/**` bytes change.

- [ ] **Step 2: Run tests and verify the entrypoint is missing.**

Run: `uv run pytest tests/unit/workflow/improvements/test_review.py tests/integration/test_improvement_review_workflow.py -q`

Expected: failures show missing review functions and workflow entrypoint.

- [ ] **Step 3: Implement review context and action validation.**

Follow `workflow/issues/review.py` structure but use only Improvement models and events. Context includes Improvement ID/version, state, source refs, target, proposed change, verification, risk, confidence, and allowed actions. It does not expand Problem snapshots into writable fields.

- [ ] **Step 4: Add the repeatable graph.**

Add params `improvement_id` and `improvement_review_id`; add entrypoint `improvement-review` with `restart: repeatable` and a non-empty allow expression. Graph nodes are load context, human interrupt (`approve`, `reject`, `request_rework`, `supersede`, `stop`), and apply review. The apply operation writes only the Improvement Ledger under the project lock.

- [ ] **Step 5: Add the CLI entrypoint choice and integration test.**

Extend `_ENTRYPOINT_CHOICE` with `improvement-review`. Exercise interrupt/resume and assert the Improvement version increments while the referenced Problem projection and Issue ledger bytes remain identical.

- [ ] **Step 6: Run review and contract tests.**

Run: `uv run pytest tests/unit/workflow/improvements/test_review.py tests/integration/test_improvement_review_workflow.py tests/unit/workflow/graph/test_contracts.py tests/unit/workflow/graph/test_resume_v3.py -q && uv run ruff check assurance_agent/workflow/improvements/review.py assurance_agent/commands/workflow_cmd.py tests/unit/workflow/improvements/test_review.py tests/integration/test_improvement_review_workflow.py && uv run pyright && uv run lint-imports`

Expected: all commands pass.

- [ ] **Step 7: Commit independent review.**

```bash
git add assurance_agent/workflow/improvements/review.py assurance_agent/artifacts/models/improvements.py assurance_agent/workflow/graph/handlers/operation.py assurance_agent/_resources/schemas/workflow-schema.yaml assurance_agent/_resources/schemas/execution-contracts.yaml assurance_agent/commands/workflow_cmd.py tests/unit/workflow/improvements/test_review.py tests/integration/test_improvement_review_workflow.py
git commit -m "feat: add improvement review workflow"
```

---

### Task 11: Add narrow delivery adapters for memory, change drafts, and knowledge

**Files:**
- Create: `assurance_agent/workflow/improvements/memory_delivery.py`
- Create: `assurance_agent/workflow/improvements/change_delivery.py`
- Create: `assurance_agent/workflow/improvements/knowledge_delivery.py`
- Modify: `assurance_agent/retro/apply.py`
- Modify: `assurance_agent/knowledge/promote.py`
- Modify: `assurance_agent/workflow/graph/handlers/operation.py`
- Modify: `assurance_agent/_resources/schemas/workflow-schema.yaml`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Modify: `assurance_agent/commands/workflow_cmd.py`
- Test: `tests/unit/workflow/improvements/test_memory_delivery.py`
- Test: `tests/unit/workflow/improvements/test_change_delivery.py`
- Test: `tests/unit/workflow/improvements/test_knowledge_delivery.py`
- Test: `tests/integration/test_improvement_delivery.py`
- Modify: `tests/unit/workflow/graph/test_canonical_schema_v2.py`

**Interfaces:**
- Consumes: approved canonical Improvement projections, existing Eval runner/baseline gate, existing memory marker logic, and existing knowledge semantic validation/promotion.
- Produces: `MemoryPatchDelivery.evaluate/apply/rollback`, `ChangeDraftDelivery.export/record_applied`, `KnowledgeDeltaDelivery.export/record_applied`, typed receipts/events, narrow operation contracts, and repeatable delivery entrypoints.

- [ ] **Step 1: Write failing delivery eligibility and idempotency tests.**

```python
def test_memory_apply_requires_approved_eval_and_pinned_digest(delivery, improvement) -> None:
    with pytest.raises(ImprovementDeliveryError, match="successful eval"):
        delivery.apply(improvement, expected_target_sha256=sha256_bytes(b"original"))


def test_change_draft_is_hash_idempotent(delivery, improvement) -> None:
    first = delivery.export(improvement)
    second = delivery.export(improvement)
    assert first.sha256 == second.sha256
    assert first.created is True and second.created is False
```

Also test missing baseline -> `awaiting_baseline`, eval regression -> `rolled_back` with unchanged/restored real memory, conflicting draft bytes -> explicit conflict, knowledge eligibility rejection for every prohibited Problem state, L2 semantic rejection, and no direct L1 mutation on knowledge export.

- [ ] **Step 2: Run delivery tests and verify missing adapter failures.**

Run: `uv run pytest tests/unit/workflow/improvements/test_memory_delivery.py tests/unit/workflow/improvements/test_change_delivery.py tests/unit/workflow/improvements/test_knowledge_delivery.py tests/integration/test_improvement_delivery.py -q`

Expected: collection fails because the delivery adapters do not exist.

- [ ] **Step 3: Implement memory eval/apply/rollback with Improvement markers.**

Stage the exact proposed change under a temporary memory root, run the declared existing suites, persist eval artifact digests, then append `improvement_eval_requested` and `improvement_eval_completed`. Apply only a successful evaluated digest and use marker `<!-- improvement:<improvement-id> evidence:<sorted-source-ids> -->`. Rollback validates the applied block digest before removal/deprecation and appends `improvement_rolled_back`.

- [ ] **Step 4: Implement project-scoped change draft export.**

Write `qa/improvements/drafts/<improvement-id>.yaml` with Improvement ID/version, kind, source refs, target, proposed change, verification scope, and content SHA-256. Do not include Problem title, classification, severity, status, disposition, or root cause. Existing identical bytes are success; different bytes raise `ImprovementDeliveryConflict` unless an explicit rework action changed the Improvement version.

- [ ] **Step 5: Implement knowledge export and eligibility checks.**

Require at least one cited Problem snapshot in the pinned Retro source, `assessment.authority == human_confirmed`, `status == resolved`, and a verified resolution scope. Reject `detected`, `triaged`, `in_progress`, `verification_pending`, `accepted_risk`, and `not_an_issue`. Validate `knowledge_delta` using existing `DataKnowledgeProposal` and semantic validation, then write only `qa/improvements/knowledge-delta/<improvement-id>.proposal.yaml`. `knowledge.promote` remains the only L1 writer; after promotion, an apply-record operation verifies the promoted L1 digest before appending `improvement_applied`.

- [ ] **Step 6: Register separate operations/contracts.**

Use separate targets: `operation:load-improvement-delivery`, `operation:evaluate-memory-improvement`, `operation:apply-memory-improvement`, `operation:rollback-memory-improvement`, `operation:export-change-improvement`, `operation:record-change-improvement-applied`, `operation:export-knowledge-improvement`, and `operation:record-knowledge-improvement-applied`. Each contract declares only its delivery-specific target plus synchronized Improvement Ledger writes; none uses a shared wildcard over product source, memory, and data knowledge. The two `record-*-applied` operations require an actor, reason, and externally produced artifact digest; the knowledge variant additionally verifies the current L1 digest.

- [ ] **Step 7: Add repeatable delivery graphs with delivery-specific branches.**

Add `improvement-evaluate`, `improvement-export`, `improvement-apply`, and `improvement-rollback` entrypoints. Each graph begins with `load-improvement-delivery`; conditional edges route only to the operation matching the canonical `delivery` value. `improvement-evaluate` accepts only `memory_patch`; export accepts `change_draft` or `knowledge_delta`; apply accepts an evaluated `memory_patch` or records verified external application of an exported change/knowledge artifact; rollback accepts only an applied memory patch. Add these names to `_ENTRYPOINT_CHOICE` and reject a nonmatching delivery before any target write.

- [ ] **Step 8: Run delivery, graph, Eval, and Knowledge regression tests.**

Run: `uv run pytest tests/unit/workflow/improvements/test_memory_delivery.py tests/unit/workflow/improvements/test_change_delivery.py tests/unit/workflow/improvements/test_knowledge_delivery.py tests/integration/test_improvement_delivery.py tests/unit/workflow/graph/test_canonical_schema_v2.py tests/unit/workflow/graph/test_contracts.py tests/unit/eval tests/unit/knowledge -q && uv run ruff check assurance_agent/workflow/improvements assurance_agent/knowledge/promote.py assurance_agent/commands/workflow_cmd.py tests/unit/workflow/improvements tests/integration/test_improvement_delivery.py && uv run pyright && uv run lint-imports`

Expected: all commands pass.

- [ ] **Step 9: Commit the delivery boundary.**

```bash
git add assurance_agent/workflow/improvements/memory_delivery.py assurance_agent/workflow/improvements/change_delivery.py assurance_agent/workflow/improvements/knowledge_delivery.py assurance_agent/retro/apply.py assurance_agent/knowledge/promote.py assurance_agent/workflow/graph/handlers/operation.py assurance_agent/_resources/schemas/workflow-schema.yaml assurance_agent/_resources/schemas/execution-contracts.yaml assurance_agent/commands/workflow_cmd.py tests/unit/workflow/improvements/test_memory_delivery.py tests/unit/workflow/improvements/test_change_delivery.py tests/unit/workflow/improvements/test_knowledge_delivery.py tests/integration/test_improvement_delivery.py tests/unit/workflow/graph/test_canonical_schema_v2.py
git commit -m "feat: add typed improvement deliveries"
```

---

### Task 12: Add Improvement CLI projections and remove legacy Retro runtime paths

**Files:**
- Create: `assurance_agent/commands/improvement_cmd.py`
- Modify: `assurance_agent/cli.py`
- Modify: `assurance_agent/commands/retro_cmd.py`
- Delete: `assurance_agent/retro/archive_reader.py`
- Delete: `assurance_agent/retro/aggregator.py`
- Delete: `assurance_agent/retro/apply.py`
- Delete: `assurance_agent/retro/eval_trend.py`
- Delete: `assurance_agent/retro/export.py`
- Delete: `assurance_agent/retro/promotions.py`
- Delete: `assurance_agent/retro/projection.py`
- Delete: `assurance_agent/retro/proposals.py`
- Delete: `assurance_agent/retro/state.py`
- Delete: `assurance_agent/retro/nightly/driver.py`
- Delete: `assurance_agent/retro/nightly/agent.py`
- Delete: `assurance_agent/retro/nightly/exit_codes.py`
- Delete: `assurance_agent/retro/nightly/phase_a.py`
- Delete: `assurance_agent/retro/nightly/phase_d.py`
- Delete: `assurance_agent/retro/nightly/phase_f.py`
- Delete: `assurance_agent/retro/nightly/types.py`
- Delete: `assurance_agent/retro/nightly/utils.py`
- Delete obsolete tests: `tests/unit/retro/test_{aggregator,apply_and_gate,export,memory_apply,projection,promotions,proposals,state}.py`
- Delete obsolete tests: `tests/unit/retro/nightly/test_{collect,phases,resume_gate}.py`
- Modify: `tests/integration/test_retro_cli.py`
- Test: `tests/integration/test_improvement_cli.py`

**Interfaces:**
- Consumes: canonical Improvement projections and the current-run Retro graph from Tasks 3 and 9.
- Produces: `aa improvement list`, `aa improvement show --id`, project-ledger-backed JSON views, and a reduced Retro CLI that never scans prior runs.

- [ ] **Step 1: Write failing CLI tests before deleting the old modules.**

```python
def test_improvement_list_and_show_read_project_projection(runner, project) -> None:
    listed = runner.invoke(main, ["improvement", "list", "--json"])
    shown = runner.invoke(main, ["improvement", "show", "--id", "IMP-ABC", "--json"])
    assert listed.exit_code == shown.exit_code == 0
    assert json.loads(shown.output)["improvement_id"] == "IMP-ABC"


def test_retro_cli_has_no_per_run_lifecycle_commands(runner) -> None:
    help_text = runner.invoke(main, ["retro", "--help"]).output
    for old in ("promote", "complete", "apply", "rollback", "export-issues", "export-knowledge", "nightly", "proposals-for-change"):
        assert old not in help_text
```

- [ ] **Step 2: Run CLI tests and verify the old surface fails expectations.**

Run: `uv run pytest tests/integration/test_retro_cli.py tests/integration/test_improvement_cli.py -q`

Expected: `aa improvement` is missing and legacy Retro subcommands are still present.

- [ ] **Step 3: Implement project-ledger-backed Improvement views.**

`list` loads `qa/improvements/improvements.json` through the strict model and sorts by ID; optional filters are exact `--state`, `--kind`, and `--delivery`. `show` returns the canonical projection plus event timeline from `events.jsonl`. Neither command walks `qa/retro/`.

- [ ] **Step 4: Reduce Retro CLI to the canonical workflow trigger/current-run display.**

Preserve only options that resolve a new window (`--change`, `--since`, `--until`, `--last`, `--retro-id`, `--dry-run`) and route execution through the canonical Retro graph/operations. A requested explicit run may display files under that exact current `retro-id`; no list or cross-run search is allowed.

- [ ] **Step 5: Delete legacy readers, per-run lifecycle code, and tests.**

Delete the files listed above only after `rg` confirms no production import remains. Keep legacy `qa/retro/**` directories on disk untouched. Remove generation/reads of `_state.json`, `cross-run-report.json`, `promotions.json`, per-run `eval-results.json`, and per-run `issue-drafts/**`.

- [ ] **Step 6: Run all Retro/Improvement/CLI tests and static checks.**

Run: `uv run pytest tests/unit/retro tests/unit/workflow/improvements tests/integration/test_retro_cli.py tests/integration/test_improvement_cli.py tests/integration/test_retro_improvement_workflow.py -q && uv run ruff check assurance_agent tests/unit/retro tests/unit/workflow/improvements tests/integration/test_retro_cli.py tests/integration/test_improvement_cli.py && uv run pyright && uv run lint-imports`

Expected: all commands pass with no imports of deleted modules.

- [ ] **Step 7: Commit the clean runtime cut.**

```bash
git add -A assurance_agent/retro assurance_agent/commands assurance_agent/cli.py tests/unit/retro tests/integration/test_retro_cli.py tests/integration/test_improvement_cli.py
git commit -m "refactor: remove legacy retro lifecycle state"
```

---

### Task 13: Add clean-cut guards, end-to-end acceptance, and documentation

**Files:**
- Create: `tests/unit/retro/test_no_historical_retro_reads.py`
- Create: `tests/integration/test_retro_issue_improvement_acceptance.py`
- Modify: `tests/integration/test_issue_lifecycle_workflow.py`
- Modify: `tests/integration/test_issue_lifecycle_acceptance.py`
- Modify: `docs/schemas.md`
- Modify: `docs/eval.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: the finished Issue, Retro, Improvement, graph, review, and delivery surfaces.
- Produces: deletion guards, six acceptance scenarios, updated artifact/command documentation, and the final verification record.

- [ ] **Step 1: Write a production-source guard with explicit fixture/doc exclusions.**

```python
FORBIDDEN = (
    "qa/retro/_state.json",
    "cross-run-report.json",
    "promotions.json",
    '"workflow_bug"',
    '"issue_export"',
)


def test_production_has_no_historical_retro_reader() -> None:
    roots = (Path("assurance_agent"),)
    offenders = []
    for root in roots:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            offenders.extend(f"{path}:{token}" for token in FORBIDDEN if token in text)
    skill = Path("assurance_agent/_resources/skills/aa-retro/SKILL.md").read_text(encoding="utf-8")
    assert "qa/retro/*" not in skill and "project:qa/retro/**" not in skill
    assert offenders == []
```

Also inspect YAML contracts for broad `skill:aa-retro` reads and AST calls to `iterdir/glob/rglob` rooted at `qa/retro`. Historical docs, plans, and benchmark fixtures are intentionally outside the production scan.

- [ ] **Step 2: Run the guard and fix every production offender.**

Run: `uv run pytest tests/unit/retro/test_no_historical_retro_reads.py -q`

Expected: the first run identifies any legacy constants/imports left by Task 12; after removal it passes.

- [ ] **Step 3: Implement the six end-to-end acceptance scenarios.**

Cover exactly:

1. Full workflow creates a `product_bug` Problem and never invokes Retro.
2. Multiple Changes with the same misclassification yield one `prompt_improvement`, not a new Problem.
3. An inspect truncation exists first as `workflow_issue`, then supports a separate `workflow_improvement`.
4. The same window rerun under another Retro ID yields one canonical Improvement with linked evidence.
5. Applying an Improvement leaves the Problem unchanged until later authoritative Issue verification.
6. Only a resolved, human-confirmed, scope-verified Problem can export knowledge; L1 bytes remain identical before promotion.

Each scenario asserts event IDs/digests, artifact paths, and the absence of unauthorized writes.

- [ ] **Step 4: Add graph-order regression assertions.**

Assert initial execution and every healing rerun traverse `inspect-with-issues` before decide/report, and that no full graph closure contains `retro-workflow`, `reconcile-improvements`, Improvement review, or delivery nodes.

- [ ] **Step 5: Update public documentation.**

Document schema-v2 `context.json`, `proposal-candidates.json`, `accept-status.json`, `qa/improvements/{events,improvements,review-queue}.json*`, the five kinds/three deliveries, independent review flow, delivery paths, window selection semantics, and removed legacy commands/files. Update README commands so documented Click commands and actual CLI remain bidirectionally consistent.

- [ ] **Step 6: Run focused acceptance and packaging tests.**

Run: `uv run pytest tests/unit/retro/test_no_historical_retro_reads.py tests/integration/test_retro_issue_improvement_acceptance.py tests/integration/test_issue_lifecycle_workflow.py tests/integration/test_issue_lifecycle_acceptance.py tests/integration/test_packaged_happy_path.py tests/integration/test_readme_commands.py -q`

Expected: all commands pass.

- [ ] **Step 7: Run the full repository gates.**

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
bash scripts/packaging_smoke_test.sh
```

Expected: every command exits 0; report the exact pytest pass count and packaging smoke result in the implementation handoff.

- [ ] **Step 8: Commit acceptance and docs.**

```bash
git add tests/unit/retro/test_no_historical_retro_reads.py tests/integration/test_retro_issue_improvement_acceptance.py tests/integration/test_issue_lifecycle_workflow.py tests/integration/test_issue_lifecycle_acceptance.py docs/schemas.md docs/eval.md README.md
git commit -m "test: close retro improvement lifecycle acceptance"
```

---

## Final acceptance checklist

- [ ] Existing Issue full-workflow and healing-rerun ordering is byte-for-byte/topologically unchanged except for tests that make the invariant explicit.
- [ ] Retro has no full-workflow edge and no hidden invocation from Issue, report, healing, or archive.
- [ ] `LedgerIssueHistoryReader` is the only production adapter translating Issue Ledger layout into Retro evidence; Retro modules do not import Issue event readers or ledger paths.
- [ ] `skill:aa-retro` receives only the current digest-pinned context and has no broad read grant.
- [ ] Every Candidate source ref resolves through the current `source_manifest`; any invalid Candidate rejects the entire batch before project writes.
- [ ] Improvement fingerprint deduplicates exact intent across Retro IDs while preserving additional evidence links.
- [ ] Improvement review/eval/export/apply/rollback append canonical Improvement events and never mutate Problem status.
- [ ] Domain knowledge export requires a resolved, human-confirmed, verified Problem and never writes L1 before knowledge promotion.
- [ ] Production reads of historical `qa/retro/**`, consumed state, cross-run report, per-run promotions/eval, `workflow_bug`, and `issue_export` are zero.
- [ ] Canonical JSONL replay/projections and identical reconciliation retries are byte-stable.
- [ ] Focused tests, full pytest, Ruff check/format, Pyright, import-linter, and packaging smoke all pass.
