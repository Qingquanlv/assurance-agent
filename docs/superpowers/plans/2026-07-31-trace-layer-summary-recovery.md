# Trace Layer Summary and Recovery Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver versioned, authority-safe Trace projections with deterministic four-layer summaries, recovery-complete workflow materialization, bound sufficiency evidence, and typed specialty-report v3 output.

**Architecture:** Keep Trace facts in artifacts/evidence and keep policy/time joins in reporting. Move pure issue schemas, identities, and replay below workflow; build reconciled enrichment through one fail-closed authority evaluator; route every settled issue path through the existing materializer. Version each changed wire boundary explicitly, then make benchmark v3 consume only shared loaders, summaries, joins, and pinned policy evidence.

**Tech Stack:** Python 3.11, Pydantic v2, frozen models, append-only JSONL ledgers, YAML workflow DSL, GraphRuntime, pytest, Ruff, Pyright, import-linter, uv, Bash benchmark helpers.

## Global Constraints

- The authoritative design is `docs/superpowers/specs/2026-07-31-trace-layer-summary-recovery-design.md`, with one prerequisite-induced correction recorded in Locked Interface Clarification 10: its older D7 graph/contract-drift ordering is superseded by the already approved pinned-execution-bundle contract from the Fuzz/Performance plan Tasks 10 and 13. Preserve every other authority rule, gap code, topology rule, recovery boundary, and compatibility promise.
- Complete `docs/superpowers/plans/2026-07-31-fuzz-performance-assurance-wiring.md` Tasks 10–13 before starting this plan. In particular, specialty replay must already use `counterfactual_plan_check_actions/v2`, derive wiring from pinned topology, and expose no `--schema-root` replay escape hatch.
- `TraceProjection` remains fact-only. Do not add policy action, wall clock, benchmark verdict, sufficiency verdict, or recovery as a third phase.
- Layer order is exactly `api`, `e2e`, `fuzz`, `performance`; case-type order is exactly `API`, `E2E`, `Fuzz`, `Performance`. Zero-row layers are present in new summaries.
- New Trace writers emit schema `"2"`; new reconcile-status and quality writers emit schema `"2.0"`; new specialty writers emit schema `"3"`. V1/V2 readers remain explicit and never fabricate a newer complete artifact.
- Legacy names `TraceProjection`, `TraceGap`, `TraceGapCode`, `IssueReconcileStatus`, and `QualityGateResult` remain compatibility aliases to their V1 classes/types only. Never repoint an old name to V2.
- New recovery/authority gap codes exist only in Trace V2 and are always included in `VERIFY_BLOCKING_GAP_CODES`.
- Manifest entry paths are relative, unique, symlink-safe, and restricted to `execution/**`, `cases/**`, `facts/**`, `review/**`, `healing/**`, and `codegen/**`. Every file is opened once; the same bytes feed the redacted entry digest and raw `TraceSource.sha256`.
- Evidence entry and candidate digests retain the `sha256:<hex>` wire format. Projection, policy, and `TraceSource.sha256` retain unprefixed lowercase hex. Do not change canonical newline behavior while extracting helpers.
- One failure source produces at most one failure-authority gap. The complete issue authority evaluation produces at most one issue-authority gap, using the source and reason precedence in spec §7.2.
- A recovery result is a valid current-batch reconciled V2 projection with `integrity="incomplete"`, a blocking gap, and no problem links. Missing output is reserved for materializer/model/publication failure.
- Do not add target-specific Graph compiler logic. Schema edges and recovery continuations are the control plane.
- Preserve the current GraphRuntime ordering: repair only definition-independent manual-revision prefixes, resolve the exact current-or-pinned execution bundle and check live semantic/model compatibility, run invocation-local pending write/publication/materialization recovery, then plan. Historical graph/contract/catalog metadata may be loaded; historical Python handler implementations may not.
- Do not modify the generic Graph publication protocol, checkpoint authority, object-store durability, or SIGKILL windows excluded by spec sections 3 and 16.
- Preserve `QualityReport` schema `"1.1"` and legacy specialty v1/v2 payloads. Old reports render as `legacy_unlayered`; they never receive synthetic zero-valued layer rows.
- Expected specialty collection failures must atomically publish a model-valid typed incomplete v3 report before returning nonzero. A matching durable publication receipt is the commit token (and must match the current attempt on fresh collection); unexpected failures never publish that token, so an unacknowledged report file is ineligible for fresh or resumed registration.
- Write every behavior test first, run it, and observe the expected failure before production changes.
- Run every Python command through `uv run`.
- Do not assume the shared worktree or index is clean. A single integrator serializes every `git add` and `git commit`; subagents may edit disjoint files but never stage or commit.

## Locked Interface Clarifications

The approved spec contains a few prose/code-name ambiguities. This plan fixes one implementation vocabulary so tasks cannot drift:

1. `TraceLayerSufficiencySummary` is the canonical artifacts-owned layer-join DTO. `join_layer_sufficiency(...)` returns that type; do not create an eval-owned duplicate named `TraceLayerEvidenceSummary`.
2. Shared decoded-JSON loaders accept `raw: object`; filesystem callers perform JSON decoding first:

   ```python
   load_trace_projection_document(raw: object) -> TraceProjectionV1 | TraceProjectionV2
   load_issue_reconcile_status_document(raw: object) -> IssueReconcileStatusV1 | IssueReconcileStatusV2
   load_quality_gate_result_document(raw: object) -> QualityGateResultV1 | QualityGateResultV2
   ```

3. The phase-pair interface is:

   ```python
   class TracePhasePairError(ValueError):
       """Execution and reconciled projections violate enrichment-only monotonicity."""

   def validate_trace_phase_pair(
       execution: TraceProjectionV1 | TraceProjectionV2,
       reconciled: TraceProjectionV1 | TraceProjectionV2,
   ) -> None:
       """Raise TracePhasePairError when the phase pair is not monotonic."""
   ```

   Reconciled-only sources may be the exact authority filenames or a safely validated manifest entry under the six allowed prefixes. Any other added source fails.
4. Current projection loading uses three typed errors plus a closed stale reason so benchmark reason mapping is deterministic:

   ```python
   CurrentProjectionStaleReason = Literal[
       "legacy_version",
       "phase_mismatch",
       "change_id_mismatch",
       "batch_id_mismatch",
       "digest_mismatch",
   ]


   class CurrentProjectionError(Exception):
       """Base class for persisted-current projection loading failures."""


   class CurrentProjectionMissingError(CurrentProjectionError):
       """The persisted reconciled projection does not exist."""

   class CurrentProjectionInvalidError(CurrentProjectionError):
       """The persisted reconciled projection is not valid Trace JSON."""

   class CurrentProjectionStaleError(CurrentProjectionError):
       """The persisted projection is not the current reconciled V2 fold."""

       def __init__(self, reason: CurrentProjectionStaleReason) -> None:
           super().__init__(reason)
           self.reason = reason
   ```

5. Event identity validation always recomputes `event_id = "EVT-" + sha256(idempotency_key)[:16]`. `recomputable_issue_event_idempotency_key(event) -> str | None` also reconstructs the exact existing key for event types whose persisted payload contains its full preimage; replay requires equality when that function returns a value. Legacy events such as merge suggestions that omit the per-candidate digest return `None` and are instead constrained by nested occurrence/problem/evidence relations; do not invent a new historical key format.
6. `IssueReconcileStatusV2` requires a non-empty `candidate_digest` in every state. `completed` requires a nonnegative `occurrence_count` and null `error`; `failed` requires null count and non-empty `error`; `pending` requires null count and null `error`.
7. Trace source construction uses one path-keyed `TraceSourceRecorder`. Re-adding the same path with identical `(exists, sha256)` is idempotent; conflicting facts raise `TraceSourceConflictError` before a projection is constructed.
8. Missing capability `definition_binding` maps to the existing closed specialty reason `sufficiency_binding_mismatch` with detail `capability_definition_binding_missing`; do not add an undocumented reason code.
9. The frozen Cursor evidence row is exactly:

   ```text
   change_id|collection_status|reason_code|trace_exit|integrity|gap_count|verify_exit|verdict|blocking|insufficient
   ```

   `collection_status` is `raw`, `complete`, `incomplete`, or `legacy_unlayered`. Unknown values are the literal `unknown`, never numeric zero.
10. The completed Fuzz/Performance prerequisite changes the old-pinned boundary assumed by design D7. A graph, contract, or ingest-catalog identity drift resolves the exact verified pinned execution bundle and may continue the old topology; it does not by itself raise `GraphDefinitionChanged`. Gate-semantics/profile incompatibility or a pinned-model/current-class schema mismatch raises `GraphDefinitionChanged` before definition-dependent pending-write recovery or planning. The recovery barrier remains invocation-local, and handler code is never archived: an already-successful task replays its frozen write set without reinvoking a handler, while later old-topology tasks may execute only through current code proven compatible with the pinned catalog.
11. A specialty V3 report is committed only by a durable sibling `state="committed"` publication receipt whose change ID, report digest, trace status, and capability integrity match. Before replacing report bytes, a fresh attempt atomically overwrites that sibling with `state="pending"` and its new attempt ID; after report rename it atomically replaces pending with committed. This invalidation prevents an older same-digest receipt from creating an ABA false commit. Fresh validation additionally requires the caller's expected attempt ID; reuse validates the recorded non-empty attempt ID and all report bindings. V1/V2 legacy reports remain receiptless-readable only when the sibling receipt path is absent; any present pending, committed, malformed, or mismatched receipt blocks legacy bypass as well as V3 reuse.

## File Structure

### New production files

- `assurance_agent/artifacts/models/issue_events.py` — immutable event wire schemas only; no filesystem or workflow imports.
- `assurance_agent/artifacts/models/sufficiency.py` — bound sufficiency V2 DTOs and four-layer sufficiency summary DTOs.
- `assurance_agent/evidence/digests.py` — canonical projection digest, entry redaction/digest, bundle digest, safe one-read file access, and source recorder.
- `assurance_agent/evidence/issue_identity.py` — canonical candidate, per-candidate, observation, occurrence, problem, event, and idempotency identities.
- `assurance_agent/evidence/issue_replay.py` — strict JSONL readers, event identity verification, pure change/project projection, canonical projection bytes.
- `assurance_agent/evidence/layer_summary.py` — pure four-layer fact summary, phase-pair validator, and layer sufficiency join.
- `assurance_agent/evidence/trace_authority.py` — current-batch failure/issue authority truth table and validated enrichment decision.
- `assurance_agent/evidence/current_projection.py` — point-in-time persisted reconciled projection freshness loader and typed errors.

### New tests

- `tests/unit/artifacts/test_sufficiency_models.py`
- `tests/unit/evidence/test_digests.py`
- `tests/unit/evidence/test_layer_summary.py`
- `tests/unit/evidence/test_layer_sufficiency.py`
- `tests/unit/evidence/test_issue_replay_authority.py`
- `tests/integration/test_trace_recovery_workflows.py`

### Modified production areas

- `assurance_agent/artifacts/models/{trace.py,issues.py,inspect.py,policy.py,__init__.py}` and `assurance_agent/artifacts/registry.py` — explicit wire versions, wrappers, loaders, exports, and registry dispatch.
- `assurance_agent/evidence/{trace.py,sufficiency.py,verify.py}` — V2 construction, shared summaries/digests, authority evaluation, and blocking verdicts.
- `assurance_agent/workflow/issues/{identity.py,events.py,projection.py,history.py,collector.py,reconciler.py,operations.py,ledger.py}` — compatibility re-exports, lower-layer reuse, status V2 writers, and no digest duplication.
- `assurance_agent/workflow/report/{quality_gate.py,inspector.py,report_builder.py}` and `assurance_agent/workflow/execution/{runner.py,evidence.py}` — bound quality V2 writer and concrete version consumers.
- `assurance_agent/workflow/graph/handlers/trace_projection.py`, `assurance_agent/_resources/schemas/{execution-contracts.yaml,workflow-schema.yaml}` — V2 materializer reads/writes and all-terminal-path routing.
- `assurance_agent/eval/{specialty_models.py,specialty_render.py}` and `benchmark/vue-fastapi-admin/benchmark/{benchmark_specialty_report.py,cursor-loop-helpers.sh,run-workflow-loop-cursor.sh}` — specialty v3 collection, rendering, legacy display, and collection-aware rows.
- `docs/schemas.md` — Trace/status/quality/specialty versions, authority semantics, and benchmark row contract.

## Execution Preflight

- [ ] Confirm the Fuzz/Performance plan Tasks 10–13 are committed. Run:

  ```bash
  rg -n 'counterfactual_plan_check_actions/v2' assurance_agent/eval/specialty_models.py
  ! rg -n -- '--schema-root' benchmark/vue-fastapi-admin/benchmark
  uv run pytest -q \
    tests/unit/eval/test_specialty_models.py \
    tests/unit/workflow/graph/test_four_layer_replay.py \
    tests/unit/benchmark/test_specialty_report.py \
    tests/unit/benchmark/test_cursor_loop_helpers.py
  ```

  Expected: the v2 replay literal exists, no benchmark helper accepts `--schema-root`, and the focused prerequisite suite passes.
- [ ] Record `git rev-parse HEAD`, `git status --short`, and `git diff --cached --name-only`. Require a clean tracked worktree and empty index before Task 1. Do not stash, discard, or absorb another session's changes.
- [ ] Before every commit inspect `git diff --cached --name-only`, the full cached diff, and `git diff --cached --check`. The staged path set must exactly equal the task's declared files.
- [ ] For every Python-changing task run Ruff on changed Python files and the full `uv run pyright` before commit. Focused pytest alone is not an intermediate green gate.
- [ ] Run tasks in numbered order. Tasks 12–15 may not start until the shared interfaces from Tasks 1–11 are committed.

---

### Task 1: Introduce the Trace V1/V2 Document Boundary

**Files:**
- Modify: `assurance_agent/artifacts/models/trace.py`
- Modify: `assurance_agent/artifacts/models/__init__.py`
- Modify: `assurance_agent/artifacts/registry.py`
- Modify: `tests/unit/artifacts/test_trace_models.py`
- Modify: `tests/unit/artifacts/test_registry.py`
- Modify: `tests/unit/artifacts/test_validate.py`

**Interfaces:**
- Produces: `TraceGapCodeV1`, `TraceGapV1`, `TraceProjectionV1`, `TraceGapCodeV2`, `TraceSummaryGapCode`, `TraceGapV2`, `TraceProjectionV2`, `TraceProjectionDocument`, `TraceProjectionLike`, and `load_trace_projection_document(raw: object)`.
- Preserves: `TraceGapCode = TraceGapCodeV1`, `TraceGap = TraceGapV1`, and `TraceProjection = TraceProjectionV1` for legacy imports.
- Defers: `fold_trace` continues to construct V1 until Task 12 atomically switches writers.

- [ ] **Step 1: Add failing version-dispatch and V2 invariant tests**

Add parameterized tests with these assertions:

```python
def test_trace_document_reads_legacy_missing_version_only() -> None:
    payload = valid_projection_v1()
    payload.pop("schema_version")
    loaded = load_trace_projection_document(payload)
    assert isinstance(loaded, TraceProjectionV1)

    for invalid in (None, "", "99"):
        payload["schema_version"] = invalid
        with pytest.raises(ValidationError):
            load_trace_projection_document(payload)


def test_trace_v1_rejects_v2_recovery_gap() -> None:
    payload = valid_projection_v1()
    payload["gaps"] = [
        {
            "code": "project_sync_pending",
            "source": "inspect/issue-reconcile-status.json",
        }
    ]
    with pytest.raises(ValidationError):
        TraceProjectionV1.model_validate(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_case",
        "duplicate_source",
        "duplicate_gap",
        "execution_enrichment",
        "coverage_mismatch",
    ],
)
def test_trace_v2_rejects_semantic_mutations(mutation: str) -> None:
    payload = mutate_trace_v2(valid_projection_v2(), mutation)
    with pytest.raises(ValidationError):
        TraceProjectionV2.model_validate(payload)
```

Also assert the registry points at `TraceProjectionDocument` with `compat == "versioned"`, V1 and V2 round-trip, explicit unknown versions fail, execution target matches row case type, and non-empty gap targets belong to the four-layer closed set.

- [ ] **Step 2: Run the tests and observe missing V2/document failures**

```bash
uv run pytest -q \
  tests/unit/artifacts/test_trace_models.py \
  tests/unit/artifacts/test_registry.py \
  tests/unit/artifacts/test_validate.py
```

Expected: imports or assertions fail because the document wrapper and V2 classes do not exist.

- [ ] **Step 3: Implement explicit versions and the registry wrapper**

Use shared row/source models but separate gap/projection classes:

```python
TraceGapCodeV2 = TraceGapCodeV1 | Literal[
    "failure_analysis_identity_mismatch",
    "issues_snapshot_identity_mismatch",
    "issue_analysis_failed",
    "project_sync_pending",
    "issue_reconcile_failed",
    "issue_reconciliation_unavailable",
]
TraceSummaryGapCode = TraceGapCodeV1 | TraceGapCodeV2


class TraceGapV1(BaseModel):
    model_config = _FROZEN
    code: TraceGapCodeV1
    source: str
    batch_id: str | None = None
    target: str | None = None
    detail: str = ""


class TraceGapV2(BaseModel):
    model_config = _FROZEN
    code: TraceGapCodeV2
    source: str
    batch_id: str | None = None
    target: str | None = None
    detail: str = ""


class TraceProjectionV1(BaseModel):
    model_config = _FROZEN
    schema_version: Literal["1"] = "1"
    change_id: str
    phase: Literal["execution", "reconciled"]
    authoritative_batch_id: str
    sources: tuple[TraceSource, ...] = ()
    rows: tuple[TraceRow, ...] = ()
    unmapped_tests: tuple[UnmappedTest, ...] = ()
    gaps: tuple[TraceGapV1, ...] = ()
    integrity: TraceIntegrity


class TraceProjectionV2(BaseModel):
    model_config = _FROZEN
    schema_version: Literal["2"] = "2"
    change_id: str
    phase: Literal["execution", "reconciled"]
    authoritative_batch_id: str
    sources: tuple[TraceSource, ...] = ()
    rows: tuple[TraceRow, ...] = ()
    unmapped_tests: tuple[UnmappedTest, ...] = ()
    gaps: tuple[TraceGapV2, ...] = ()
    integrity: TraceIntegrity

    @model_validator(mode="after")
    def _validate_semantics(self) -> Self:
        validate_unique_case_ids(self.rows)
        validate_unique_source_paths(self.sources)
        validate_unique_gaps(self.gaps)
        validate_row_semantics(self.phase, self.rows)
        validate_gap_targets(self.gaps)
        return self


TraceProjectionLike = TraceProjectionV1 | TraceProjectionV2


TraceProjectionVariant = Annotated[
    TraceProjectionV1 | TraceProjectionV2,
    Field(discriminator="schema_version"),
]


class TraceProjectionDocument(RootModel[TraceProjectionVariant]):
    @model_validator(mode="before")
    @classmethod
    def _legacy_missing_version(cls, raw: object) -> object:
        if isinstance(raw, dict) and "schema_version" not in raw:
            return {**raw, "schema_version": "1"}
        return raw


def load_trace_projection_document(raw: object) -> TraceProjectionLike:
    return TraceProjectionDocument.model_validate(raw).root
```

Keep V1 unchanged beyond renaming. Put the five semantic checks in named private helpers and test each independently through V2 construction. Do not put new gap literals or semantic validators into V1.

- [ ] **Step 4: Run the artifact seam and static checks**

```bash
uv run pytest -q \
  tests/unit/artifacts/test_trace_models.py \
  tests/unit/artifacts/test_registry.py \
  tests/unit/artifacts/test_validate.py
uv run ruff check .
uv run pyright
```

Expected: all commands pass.

- [ ] **Step 5: Commit the Trace wire boundary**

```bash
git add assurance_agent/artifacts/models/trace.py assurance_agent/artifacts/models/__init__.py assurance_agent/artifacts/registry.py tests/unit/artifacts/test_trace_models.py tests/unit/artifacts/test_registry.py tests/unit/artifacts/test_validate.py
git diff --cached --check
git commit -m "feat(trace): version projection wire contract"
```

---

### Task 2: Add Shared Projection Digests, Four-Layer Facts, and Phase-Pair Validation

**Files:**
- Create: `assurance_agent/evidence/digests.py`
- Create: `assurance_agent/evidence/layer_summary.py`
- Modify: `assurance_agent/artifacts/models/trace.py`
- Modify: `assurance_agent/artifacts/models/__init__.py`
- Modify: `assurance_agent/evidence/verify.py`
- Modify: `assurance_agent/evidence/trace.py`
- Create: `tests/unit/evidence/test_digests.py`
- Create: `tests/unit/evidence/test_layer_summary.py`
- Modify: `tests/unit/evidence/test_fold_trace_execution.py`
- Modify: `tests/unit/evidence/test_fold_trace_reconciled.py`
- Modify: `tests/integration/test_verify_cli.py`

**Interfaces:**
- Produces: the exact moved `canonical_json_bytes`, `projection_digest`, `TraceSourceRecorder`, `TraceSourceConflictError`, `TraceLayerSummaryError`, `TraceGapAggregate`, `TraceLayerFacts`, `TraceLayerFactSummary`, `derive_trace_integrity`, `summarize_projection_by_layer`, `TracePhasePairError`, and `validate_trace_phase_pair`.
- Internal helpers produced in `layer_summary.py`: `_summarize_layer(rows, gaps, layer, case_type)`, `_aggregate_gaps(gaps)`, and `_validate_fact_arithmetic(summary, projection)`.
- Consumes: the concrete V1/V2 classes from Task 1 and `LAYER_NAMES`/`CASE_TYPES` from `artifacts.models.assurance`.
- Preserves: the exact existing unprefixed projection digest bytes used by verify.

- [ ] **Step 1: Add failing pure-summary arithmetic tests**

Create fixtures containing all four case types and assert:

```python
summary = summarize_projection_by_layer(projection)
assert [row.layer for row in summary.layers] == ["api", "e2e", "fuzz", "performance"]
assert [row.case_type for row in summary.layers] == ["API", "E2E", "Fuzz", "Performance"]
assert sum(row.total for row in summary.layers) == len(projection.rows)
assert (
    sum(row.gaps.total for row in summary.layers) + summary.global_gaps.total
    == len(projection.gaps)
)
for aggregate in [*(row.gaps for row in summary.layers), summary.global_gaps]:
    assert aggregate.total == sum(aggregate.by_code.values())
```

Add zero-layer, coverage/current/latest partitions, execution enrichment zeroing, failure/problem row/link/unique counts, target/global gap bucketing, duplicate case, unknown target, strict integer, zero/positive breakdown, optimistic/pessimistic integrity misstatement, and canonical-byte stability cases.

Before moving the digest helper, pin exact `evidence.trace.canonical_json_bytes` output for a payload containing sorted/unsorted keys, non-ASCII text, `datetime`, and `SelectedTargets`. The golden must prove the existing behavior: compact sorted JSON, default `ensure_ascii=True`, no trailing newline, and the exact old default serializer. Also pin existing projection digests. These tests must fail if implementation substitutes `artifacts.canonical.canonical_json_bytes`, whose UTF-8/newline contract is intentionally different.

- [ ] **Step 2: Add failing phase-pair mutation tests**

Parameterize mutations that alter a shared row field, delete an execution source, change an execution gap, alter `unmapped_tests`, add an unauthorized source, or improve integrity:

```python
@pytest.mark.parametrize(
    "mutation",
    [
        "row_execution_fact",
        "remove_execution_source",
        "rewrite_execution_gap",
        "change_unmapped_test",
        "unauthorized_added_source",
        "unauthorized_added_gap",
        "improve_integrity",
    ],
)
def test_phase_pair_rejects_non_monotonic_mutation(mutation: str) -> None:
    execution, reconciled = valid_phase_pair()
    with pytest.raises(TracePhasePairError):
        validate_trace_phase_pair(execution, mutate(reconciled, mutation))
```

Also assert legal failure/problem enrichment, approved authority filenames, and safe manifest-prefix sources pass.

- [ ] **Step 3: Run the new tests and observe missing interfaces**

```bash
uv run pytest -q \
  tests/unit/evidence/test_digests.py \
  tests/unit/evidence/test_layer_summary.py \
  tests/unit/evidence/test_fold_trace_execution.py \
  tests/unit/evidence/test_fold_trace_reconciled.py
```

Expected: collection fails on missing summary/digest/phase-pair symbols.

- [ ] **Step 4: Implement strict DTOs, digest preservation, and pure aggregation**

Use strict frozen DTOs:

```python
StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
StrictPositiveInt = Annotated[int, Field(strict=True, gt=0)]


class TraceLayerSummaryError(ValueError):
    """A projection cannot produce a conserving four-layer summary."""


class TraceGapAggregate(BaseModel):
    model_config = _FROZEN
    total: StrictNonNegativeInt
    by_code: dict[TraceSummaryGapCode, StrictPositiveInt]

    @model_validator(mode="after")
    def _total_matches_breakdown(self) -> Self:
        if self.total != sum(self.by_code.values()):
            raise ValueError("gap total must equal by_code sum")
        return self


class TraceLayerFacts(BaseModel):
    model_config = _FROZEN
    layer: LayerName
    case_type: CaseType
    total: StrictNonNegativeInt
    automated: StrictNonNegativeInt
    covered: StrictNonNegativeInt
    uncovered: StrictNonNegativeInt
    not_required: StrictNonNegativeInt
    current_executed: StrictNonNegativeInt
    current_not_present: StrictNonNegativeInt
    target_not_selected: StrictNonNegativeInt
    latest_passed: StrictNonNegativeInt
    latest_failed: StrictNonNegativeInt
    latest_skipped: StrictNonNegativeInt
    never_run: StrictNonNegativeInt
    failure_rows: StrictNonNegativeInt
    failure_links: StrictNonNegativeInt
    open_problem_rows: StrictNonNegativeInt
    open_problem_links: StrictNonNegativeInt
    unique_open_problems: StrictNonNegativeInt
    gaps: TraceGapAggregate


class TraceLayerFactSummary(BaseModel):
    model_config = _FROZEN
    schema_version: Literal["1"] = "1"
    change_id: NonEmptyStr
    phase: Literal["execution", "reconciled"]
    authoritative_batch_id: NonEmptyStr
    source_projection_digest: NonEmptyStr
    projection_integrity: TraceIntegrity
    layers: tuple[TraceLayerFacts, ...]
    global_gaps: TraceGapAggregate


def projection_digest(projection: TraceProjectionLike) -> str:
    payload = projection.model_dump(mode="json")
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def derive_trace_integrity(
    rows: Sequence[TraceRow],
    gaps: Sequence[TraceGapV1 | TraceGapV2],
) -> TraceIntegrity:
    if not rows:
        return "incomplete"
    if not gaps:
        return "complete"
    if {gap.code for gap in gaps} == {"mapped_test_missing_from_tree"}:
        return "degraded"
    return "incomplete"


def summarize_projection_by_layer(
    projection: TraceProjectionLike,
) -> TraceLayerFactSummary:
    rows_by_type = {
        case_type: tuple(row for row in projection.rows if row.case_type == case_type)
        for case_type in CASE_TYPES
    }
    layer_rows = tuple(
        _summarize_layer(
            rows_by_type[case_type],
            tuple(gap for gap in projection.gaps if gap.target == layer),
            layer,
            case_type,
        )
        for layer, case_type in zip(LAYER_NAMES, CASE_TYPES, strict=True)
    )
    summary = TraceLayerFactSummary(
        schema_version="1",
        change_id=projection.change_id,
        phase=projection.phase,
        authoritative_batch_id=projection.authoritative_batch_id,
        source_projection_digest=projection_digest(projection),
        projection_integrity=projection.integrity,
        layers=layer_rows,
        global_gaps=_aggregate_gaps(
            tuple(gap for gap in projection.gaps if gap.target is None)
        ),
    )
    if summary.projection_integrity != derive_trace_integrity(
        projection.rows,
        projection.gaps,
    ):
        raise TraceLayerSummaryError("projection integrity does not match rows/gaps")
    _validate_fact_arithmetic(summary, projection)
    return summary
```

Move the old trace canonicalizer byte-for-byte into `evidence/digests.py`:

```python
def canonical_json_bytes(obj: object) -> bytes:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        default=_canonical_json_default,
    ).encode("utf-8")


def _canonical_json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, SelectedTargets):
        return value.model_dump()
    raise TypeError(f"unsupported type for canonical JSON: {type(value)!r}")
```

Import that function back into `evidence.trace` under the existing public name and delete only the old local body/default helper. This avoids a `trace ↔ digests` cycle and preserves any existing `from evidence.trace import canonical_json_bytes` caller. Do not import `artifacts.canonical` at this seam.

`TraceLayerFacts` has a model validator for the three per-layer partitions, failure/problem row bounds, unique-problem bounds, and gap arithmetic. `TraceLayerFactSummary` itself enforces exact layer/case-type order and zero execution-phase failure/problem counts. These artifacts-owned invariants remain enforceable after serialization without a projection. `_summarize_layer` derives every field directly from its rows; `_aggregate_gaps` emits only positive counts in lexical code order; `_validate_fact_arithmetic` additionally checks the projection-dependent row/gap totals while the projection is available.

- [ ] **Step 5: Implement source conflict detection and phase-pair monotonicity**

```python
class TraceSourceConflictError(ValueError):
    """The same source path was observed with conflicting facts."""


class TraceSourceRecorder:
    def __init__(self) -> None:
        self._by_path: dict[str, TraceSource] = {}

    def add(self, source: TraceSource) -> None:
        existing = self._by_path.get(source.path)
        if existing is not None and existing != source:
            raise TraceSourceConflictError(source.path)
        self._by_path[source.path] = source

    def freeze(self) -> tuple[TraceSource, ...]:
        return tuple(self._by_path[path] for path in sorted(self._by_path))
```

`validate_trace_phase_pair` must compare change/batch/case sets, strip only `failures` and `open_problem_ids` before comparing rows, require exact `unmapped_tests`, preserve every execution source and gap, allow only declared reconciled additions, and enforce integrity rank `complete < degraded < incomplete`. New gaps are restricted to `failure_analysis_missing`, `issues_snapshot_missing`, `problems_snapshot_missing`, `problem_alias_invalid`, and the six V2 authority/recovery codes; an execution-only gap cannot first appear in reconciled. Use canonical model dumps for gap multiset comparison so tuple order cannot hide deletion/rewrite.

Replace verify's private digest body with an import from `evidence.digests` and replace trace's private integrity derivation with `derive_trace_integrity`. Call `summarize_projection_by_layer()` before `fold_trace` returns even while it still constructs V1, so malformed rows or hand-stated integrity fail before publication. Use `TraceSourceRecorder` throughout fold source construction.

- [ ] **Step 6: Run summary, fold, and verify seams**

```bash
uv run pytest -q \
  tests/unit/evidence/test_digests.py \
  tests/unit/evidence/test_layer_summary.py \
  tests/unit/evidence/test_fold_trace_execution.py \
  tests/unit/evidence/test_fold_trace_reconciled.py \
  tests/integration/test_verify_cli.py
uv run ruff check .
uv run pyright
```

Expected: all commands pass and existing projection digests remain byte-identical.

- [ ] **Step 7: Commit the pure Trace layer**

```bash
git add assurance_agent/artifacts/models/trace.py assurance_agent/artifacts/models/__init__.py assurance_agent/evidence/digests.py assurance_agent/evidence/layer_summary.py assurance_agent/evidence/verify.py assurance_agent/evidence/trace.py tests/unit/evidence/test_digests.py tests/unit/evidence/test_layer_summary.py tests/unit/evidence/test_fold_trace_execution.py tests/unit/evidence/test_fold_trace_reconciled.py tests/integration/test_verify_cli.py
git diff --cached --check
git commit -m "feat(trace): add deterministic layer summaries"
```

---

### Task 3: Define Bound Sufficiency V2 and the Four-Layer Join

**Files:**
- Create: `assurance_agent/artifacts/models/sufficiency.py`
- Modify: `assurance_agent/artifacts/models/policy.py`
- Modify: `assurance_agent/artifacts/models/__init__.py`
- Modify: `assurance_agent/evidence/sufficiency.py`
- Modify: `assurance_agent/evidence/layer_summary.py`
- Modify: `assurance_agent/evidence/verify.py`
- Modify: `tests/helpers_aa.py`
- Create: `tests/unit/artifacts/test_sufficiency_models.py`
- Modify: `tests/unit/artifacts/test_policy.py`
- Modify: `tests/unit/evidence/test_sufficiency.py`
- Create: `tests/unit/evidence/test_layer_sufficiency.py`
- Modify: `tests/integration/test_verify_cli.py`

**Interfaces:**
- Produces: `ExecutionState`, `SufficiencyReasonCode`, `SufficiencyRowVerdictV2`, `SufficiencyReportV2`, `SufficiencyReportLike`, `LayerSufficiencyCounts`, `TraceLayerSufficiencySummary`, `SufficiencyBindingError`, and `join_layer_sufficiency`.
- Consumes: `projection_digest` and `TraceLayerFactSummary` from Task 2 plus canonical `policy_digest` from `assurance_agent.artifacts.policy`.
- Changes: `evaluate_sufficiency(...) -> SufficiencyReportV2`; all new quality/verify callers consume this bound DTO.
- Preserves: existing `RowVerdict` and `SufficiencyReport` as explicit legacy V1 classes for old fixtures/readers. They are never returned by new evaluation and cannot enter Quality V2 or a complete specialty join.

- [ ] **Step 1: Add failing closed-model tests**

Test strict booleans/positive integers, aware time, unique case IDs, unique missing kinds, and the exact kind/reason matrix:

```python
@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("covered", "uncovered"),
        ("execution_recent", "not_in_current_batch"),
        ("execution_recent", "never_run"),
        ("execution_recent", "execution_stale"),
        ("fuzz_run", "not_in_current_batch"),
        ("fuzz_run", "fuzz_run_missing"),
        ("perf_run", "not_in_current_batch"),
        ("perf_run", "perf_run_missing"),
        ("pass_status", "not_in_current_batch"),
        ("pass_status", "no_pass"),
        ("pass_status", "pass_stale"),
    ],
)
def test_sufficiency_v2_accepts_declared_kind_reason_pairs(
    kind: str,
    reason: str,
) -> None:
    verdict = make_verdict(missing_kinds=[kind], reason_codes=[reason])
    assert SufficiencyRowVerdictV2.model_validate(verdict).sufficient is False
```

Mutate sufficient/empty equivalence, reason length, unknown reason, every undeclared kind/reason cross-product, naive `as_of`, nonpositive recency, duplicate report case IDs, duplicate policy required kinds, and a serialized `reason_counts` entry with value zero; each must fail. `execution_state_counts` must still contain all three keys and may contain zeros.

- [ ] **Step 2: Add failing evaluator-binding and exact-join tests**

Assert the evaluator writes all provenance fields and the join rejects every non-bijection/binding error:

```python
report = evaluate_sufficiency(
    projection,
    policy,
    as_of=AWARE_NOW,
    require_current_batch=True,
)
assert report.source_projection_digest == projection_digest(projection)
assert report.source_policy_digest == policy_digest(policy)
assert report.semantics == "evidence_sufficiency/v2"
assert report.require_current_batch is True

joined = join_layer_sufficiency(
    projection,
    summarize_projection_by_layer(projection),
    report,
    expected_policy_digest=policy_digest(policy),
)
assert [row.layer for row in joined.layers] == ["api", "e2e", "fuzz", "performance"]
```

Add missing/extra/duplicate cases, wrong projection/policy digest, wrong semantics/mode, wrong layer assignment, incomplete execution-state key set, wrong reason multiset, and conservation mutations. Every join/binding failure raises `SufficiencyBindingError`; summary construction failures retain `TraceLayerSummaryError` so collector mapping is disjoint.

- [ ] **Step 3: Run the model/evaluator tests and observe failures**

```bash
uv run pytest -q \
  tests/unit/artifacts/test_sufficiency_models.py \
  tests/unit/artifacts/test_policy.py \
  tests/unit/evidence/test_sufficiency.py \
  tests/unit/evidence/test_layer_sufficiency.py \
  tests/integration/test_verify_cli.py
```

Expected: imports fail or old unbound reports violate the new assertions.

- [ ] **Step 4: Implement artifacts-owned DTOs**

Use these closed wire types:

```python
ExecutionState = Literal["never_run", "stale", "fresh"]
SufficiencyReasonCode = Literal[
    "not_in_current_batch",
    "uncovered",
    "never_run",
    "execution_stale",
    "fuzz_run_missing",
    "perf_run_missing",
    "no_pass",
    "pass_stale",
]
StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
StrictPositiveInt = Annotated[int, Field(strict=True, gt=0)]


class SufficiencyRowVerdictV2(BaseModel):
    model_config = _FROZEN
    case_id: NonEmptyStr
    sufficient: StrictBool
    missing_kinds: tuple[EvidenceKind, ...]
    reason_codes: tuple[SufficiencyReasonCode, ...]
    execution_state: ExecutionState


class SufficiencyReportV2(BaseModel):
    model_config = _FROZEN
    schema_version: Literal["2.0"] = "2.0"
    source_projection_digest: NonEmptyStr
    source_policy_digest: NonEmptyStr
    semantics: Literal["evidence_sufficiency/v2"] = "evidence_sufficiency/v2"
    require_current_batch: StrictBool
    as_of: AwareDatetime
    recency_hours: StrictPositiveInt
    verdicts: tuple[SufficiencyRowVerdictV2, ...]

    @property
    def all_sufficient(self) -> bool:
        return all(verdict.sufficient for verdict in self.verdicts)


class LayerSufficiencyCounts(BaseModel):
    model_config = _FROZEN
    layer: LayerName
    case_type: CaseType
    sufficient: StrictNonNegativeInt
    insufficient: StrictNonNegativeInt
    reason_counts: dict[SufficiencyReasonCode, StrictPositiveInt]
    execution_state_counts: dict[ExecutionState, StrictNonNegativeInt]


class TraceLayerSufficiencySummary(BaseModel):
    model_config = _FROZEN
    schema_version: Literal["1"] = "1"
    source_projection_digest: NonEmptyStr
    source_policy_digest: NonEmptyStr
    semantics: Literal["evidence_sufficiency/v2"]
    require_current_batch: Literal[True]
    as_of: AwareDatetime
    recency_hours: StrictPositiveInt
    layers: tuple[LayerSufficiencyCounts, ...]
```

Model validators enforce unique case IDs, unique missing kinds, equal missing/reason lengths, `sufficient` iff both tuples are empty, the exact zipped kind/reason matrix, execution-state consistency, exact four-layer order, positive-only present reason counts, full execution-state keys (zeros allowed), and per-layer arithmetic.

Keep the existing legacy classes under their current names and define:

```python
SufficiencyReportLike = SufficiencyReport | SufficiencyReportV2


class SufficiencyBindingError(ValueError):
    """A typed sufficiency report cannot be joined to the claimed facts/policy."""
```

During Tasks 3–4, `EvidenceCoverageEvaluation.report` and verify's internal reader accept `SufficiencyReportLike` so legacy tests and V1 quality remain readable. `build_evidence_coverage_evaluation` and `evaluate_sufficiency` always produce V2. Task 5 narrows the new Quality V2 writer with `isinstance(report, SufficiencyReportV2)`; it never upgrades a legacy report by defaults.

- [ ] **Step 5: Return bound reports and implement the strict join**

Change `evaluate_sufficiency` to fill:

```python
return SufficiencyReportV2(
    source_projection_digest=projection_digest(projection),
    source_policy_digest=policy_digest(policy),
    require_current_batch=require_current_batch,
    as_of=as_of,
    recency_hours=recency_hours,
    verdicts=verdicts,
)
```

Implement `join_layer_sufficiency` in `evidence/layer_summary.py`. It validates exact case-set equality and every binding before grouping verdicts by `TraceRow.case_type`; all declared join failures raise `SufficiencyBindingError`. Build `reason_counts` in lexical order and `execution_state_counts` with all three keys in `never_run, stale, fresh` order. Add a policy validator rejecting duplicates within each case type's `required_kinds` list.

Update verify annotations/helpers to consume `SufficiencyReportLike` while its live evaluator path receives V2. Add a compatibility test showing a caller-supplied legacy report remains readable for old fixtures but cannot pass `join_layer_sufficiency`.

- [ ] **Step 6: Run focused tests and static checks**

```bash
uv run pytest -q \
  tests/unit/artifacts/test_sufficiency_models.py \
  tests/unit/artifacts/test_policy.py \
  tests/unit/evidence/test_sufficiency.py \
  tests/unit/evidence/test_layer_sufficiency.py \
  tests/integration/test_verify_cli.py
uv run ruff check .
uv run pyright
```

Expected: all commands pass.

- [ ] **Step 7: Commit bound sufficiency**

```bash
git add assurance_agent/artifacts/models/sufficiency.py assurance_agent/artifacts/models/policy.py assurance_agent/artifacts/models/__init__.py assurance_agent/evidence/sufficiency.py assurance_agent/evidence/layer_summary.py assurance_agent/evidence/verify.py tests/helpers_aa.py tests/unit/artifacts/test_sufficiency_models.py tests/unit/artifacts/test_policy.py tests/unit/evidence/test_sufficiency.py tests/unit/evidence/test_layer_sufficiency.py tests/integration/test_verify_cli.py
git diff --cached --check
git commit -m "feat(evidence): bind sufficiency to projection and policy"
```

---

### Task 4: Define the Quality Gate V2 Wire Boundary Without Switching Writers

**Files:**
- Modify: `assurance_agent/artifacts/models/inspect.py`
- Modify: `assurance_agent/artifacts/models/__init__.py`
- Modify: `assurance_agent/artifacts/registry.py`
- Modify: `tests/unit/artifacts/test_models_inspect_report.py`
- Modify: `tests/unit/artifacts/test_registry.py`
- Modify: `tests/unit/artifacts/test_validate.py`

**Interfaces:**
- Produces: `QualityGateResultV1`, `EvidenceCoverageSuccessV2`, `EvidenceCoverageErrorV2`, `EvidenceCoveragePayloadV2`, `CoverageDimensionV2`, `QualityGateDimensionsV2`, `QualityGateResultV2`, `QualityGateResultDocument`, `QualityGateResultLike`, and `load_quality_gate_result_document(raw: object)`.
- Preserves: legacy `CoverageDimension`, `QualityGateResult = QualityGateResultV1`, and `QualityReport` schema/fields.
- Defers: `build_quality_gate` and all production consumers remain V1-typed until Task 5 switches writer and readers in one Pyright-clean commit.

- [ ] **Step 1: Add failing V1/V2 model and loader tests**

```python
def test_quality_document_round_trips_both_versions() -> None:
    v1 = load_quality_gate_result_document(valid_quality_v1())
    v2 = load_quality_gate_result_document(valid_quality_v2_success())
    assert isinstance(v1, QualityGateResultV1)
    assert isinstance(v2, QualityGateResultV2)


@pytest.mark.parametrize(
    "payload",
    [
        valid_quality_v2_success(extra_evidence_field=True),
        valid_quality_v2_error(error_code="unknown"),
        valid_quality_v2_success(report=None),
        valid_quality_v2_success(require_current_batch=False),
    ],
)
def test_quality_v2_rejects_open_or_malformed_evidence(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        QualityGateResultDocument.model_validate(payload)
```

Also prove unknown schema versions fail, registry uses the wrapper, and command tests for `aa validate --change CH-V1` / `aa validate --change CH-V2` accept the respective fixture trees.

- [ ] **Step 2: Run the artifact tests and observe missing V2 types**

```bash
uv run pytest -q \
  tests/unit/artifacts/test_models_inspect_report.py \
  tests/unit/artifacts/test_registry.py \
  tests/unit/artifacts/test_validate.py
```

Expected: V2 imports fail; production writer behavior is intentionally untouched.

- [ ] **Step 3: Implement the separate V2 hierarchy**

```python
class EvidenceCoverageSuccessV2(BaseModel):
    model_config = _FROZEN
    kind: Literal["sufficiency"]
    report: SufficiencyReportV2

    @model_validator(mode="after")
    def _require_current_batch(self) -> Self:
        if self.report.require_current_batch is not True:
            raise ValueError("quality v2 sufficiency must require current batch")
        return self


class EvidenceCoverageErrorV2(BaseModel):
    model_config = _FROZEN
    kind: Literal["error"]
    error_code: Literal["evidence_projection_missing", "policy_error"]


EvidenceCoveragePayloadV2 = Annotated[
    EvidenceCoverageSuccessV2 | EvidenceCoverageErrorV2,
    Field(discriminator="kind"),
]


class CoverageDimensionV2(BaseModel):
    model_config = _FROZEN
    status: GateStatus
    available: bool
    line_coverage: float
    branch_coverage: float
    threshold: CoverageThreshold
    scope: Any = None
    evidence: EvidenceCoveragePayloadV2


class QualityGateDimensionsV2(BaseModel):
    model_config = _FROZEN
    functional: FunctionalDimension
    coverage: CoverageDimensionV2
    non_functional: NonFunctionalDimension | None = None


class QualityGateResultV2(BaseModel):
    model_config = _FROZEN
    schema_version: Literal["2.0"] = "2.0"
    change_id: str
    batch_id: str
    dimensions: QualityGateDimensionsV2
    final_status: GateStatus
    warnings: list[str] | None = None
    diagnostics: dict | None = None


QualityGateResultLike = QualityGateResultV1 | QualityGateResultV2
```

Wrap V1/V2 in a schema discriminator and register `QualityGateResultDocument`. Do not subclass or tighten legacy `CoverageDimension`.

- [ ] **Step 4: Run focused tests and static checks**

```bash
uv run pytest -q \
  tests/unit/artifacts/test_models_inspect_report.py \
  tests/unit/artifacts/test_registry.py \
  tests/unit/artifacts/test_validate.py
uv run ruff check .
uv run pyright
```

Expected: all commands pass.

- [ ] **Step 5: Commit the quality V2 wire boundary**

```bash
git add assurance_agent/artifacts/models/inspect.py assurance_agent/artifacts/models/__init__.py assurance_agent/artifacts/registry.py tests/unit/artifacts/test_models_inspect_report.py tests/unit/artifacts/test_registry.py tests/unit/artifacts/test_validate.py
git diff --cached --check
git commit -m "feat(report): version quality gate wire contract"
```

---

### Task 5: Migrate Every Quality Consumer Through Version Dispatch

**Files:**
- Modify: `assurance_agent/workflow/report/quality_gate.py`
- Modify: `assurance_agent/workflow/execution/runner.py`
- Modify: `assurance_agent/workflow/execution/evidence.py`
- Modify: `assurance_agent/workflow/report/inspector.py`
- Modify: `assurance_agent/workflow/report/report_builder.py`
- Modify: `tests/unit/report/test_quality_gate.py`
- Modify: `tests/unit/execution/test_runner_trace.py`
- Modify: `tests/unit/execution/test_evidence.py`
- Modify: `tests/unit/report/test_inspector.py`
- Modify: `tests/unit/report/test_compat_fallback.py`
- Modify: `tests/unit/report/test_report_builder.py`
- Modify: `tests/unit/commands/test_report_cmd.py`

**Interfaces:**
- Consumes: `load_quality_gate_result_document` and `QualityGateResultLike` from Task 4.
- Produces: internal `quality_gate_legacy_view(gate: QualityGateResultLike) -> tuple[FunctionalDimension, CoverageDimension, NonFunctionalDimension | None]` for unchanged `QualityReport` construction.
- Changes: `build_quality_gate(...) -> QualityGateResultV2` and all new batch/latest quality artifacts to schema `"2.0"` in the same commit that widens every consumer annotation.
- Preserves: inspection fallback rules, scoring, risk, report schema `"1.1"`, and legacy V1 readability.

- [ ] **Step 1: Add failing writer and V1/V2 consumer matrix tests**

First assert the new writer arms:

```python
def test_quality_gate_v2_embeds_typed_current_sufficiency() -> None:
    gate = build_quality_gate(evidence_coverage=successful_v2_evidence(), **gate_inputs())
    assert gate.schema_version == "2.0"
    evidence = gate.dimensions.coverage.evidence
    assert isinstance(evidence, EvidenceCoverageSuccessV2)
    assert evidence.report.require_current_batch is True


def test_quality_gate_v2_embeds_closed_error() -> None:
    gate = build_quality_gate(evidence_coverage=missing_projection_evidence(), **gate_inputs())
    assert isinstance(gate.dimensions.coverage.evidence, EvidenceCoverageErrorV2)
```

Assert runner batch and latest quality files both load as `QualityGateResultV2` and bind the actual execution projection/policy digests.

Parameterize execution reload, inspection, and report generation over V1 and V2 fixtures:

```python
@pytest.mark.parametrize("version", ["1.0", "2.0"])
def test_execution_evidence_loads_concrete_quality_variant(
    tmp_path: Path,
    version: str,
) -> None:
    write_execution_fixture(tmp_path, quality_version=version)
    loaded = load_execution_evidence(tmp_path / "execution")
    expected = QualityGateResultV1 if version == "1.0" else QualityGateResultV2
    assert isinstance(loaded.quality_gate, expected)
```

Add unknown-version and invalid-UTF-8 rejection, V2 error-evidence inspection without traceback, no false compat fallback for V2, and equivalent `QualityReport` score/risk fields for semantically equivalent V1/V2 inputs.

- [ ] **Step 2: Run consumer tests and observe direct-model failures**

```bash
uv run pytest -q \
  tests/unit/execution/test_evidence.py \
  tests/unit/execution/test_runner_trace.py \
  tests/unit/report/test_quality_gate.py \
  tests/unit/report/test_inspector.py \
  tests/unit/report/test_compat_fallback.py \
  tests/unit/report/test_report_builder.py \
  tests/unit/commands/test_report_cmd.py
```

Expected: the writer still emits V1 and V2 artifacts fail direct `QualityGateResult.model_validate*` calls.

- [ ] **Step 3: Switch the writer and replace every direct quality parse atomically**

In `_coverage_from_evidence`, narrow the report union and construct exactly one typed arm:

```python
if evidence_coverage.error_code is not None:
    payload: EvidenceCoveragePayloadV2 = EvidenceCoverageErrorV2(
        kind="error",
        error_code=evidence_coverage.error_code,
    )
else:
    report = evidence_coverage.report
    if not isinstance(report, SufficiencyReportV2):
        raise TypeError("new quality writer requires SufficiencyReportV2")
    payload = EvidenceCoverageSuccessV2(kind="sufficiency", report=report)
```

Retain existing worst-status/action behavior, return `CoverageDimensionV2`, and construct `QualityGateResultV2`. Runner evaluates with `require_current_batch=True` and persists V2 at batch and latest paths.

Then decode every persisted quality document once and dispatch:

Decode JSON once, then dispatch:

```python
def _load_quality(path: Path) -> QualityGateResultLike | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return load_quality_gate_result_document(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError):
        return None
```

Use this seam in execution evidence, inspector, and report builder. Type `ExecutionEvidence.quality_gate` and `InspectResult.quality_gate` as `QualityGateResultLike | None`.

- [ ] **Step 4: Adapt V2 dimensions only at the legacy report boundary**

```python
def quality_gate_legacy_view(
    gate: QualityGateResultLike,
) -> tuple[FunctionalDimension, CoverageDimension, NonFunctionalDimension | None]:
    return (
        FunctionalDimension.model_validate(gate.dimensions.functional.model_dump(mode="json")),
        CoverageDimension.model_validate(gate.dimensions.coverage.model_dump(mode="json")),
        (
            None
            if gate.dimensions.non_functional is None
            else NonFunctionalDimension.model_validate(
                gate.dimensions.non_functional.model_dump(mode="json")
            )
        ),
    )
```

The legacy coverage model intentionally stores V2 typed evidence as an ordinary dict inside `QualityReport`; no consumer may treat that report copy as a new sufficiency authority.

- [ ] **Step 5: Run consumer tests and static checks**

```bash
uv run pytest -q \
  tests/unit/execution/test_evidence.py \
  tests/unit/execution/test_runner_trace.py \
  tests/unit/report/test_quality_gate.py \
  tests/unit/report/test_inspector.py \
  tests/unit/report/test_compat_fallback.py \
  tests/unit/report/test_report_builder.py \
  tests/unit/commands/test_report_cmd.py
uv run ruff check .
uv run pyright
```

Expected: both versions pass, unknown versions fail closed, and `QualityReport` remains `"1.1"`.

- [ ] **Step 6: Commit consumer migration**

```bash
git add assurance_agent/workflow/report/quality_gate.py assurance_agent/workflow/execution/runner.py assurance_agent/workflow/execution/evidence.py assurance_agent/workflow/report/inspector.py assurance_agent/workflow/report/report_builder.py tests/unit/report/test_quality_gate.py tests/unit/execution/test_runner_trace.py tests/unit/execution/test_evidence.py tests/unit/report/test_inspector.py tests/unit/report/test_compat_fallback.py tests/unit/report/test_report_builder.py tests/unit/commands/test_report_cmd.py
git diff --cached --check
git commit -m "feat(report): switch bound quality v2 with version dispatch"
```

---

### Task 6: Extract Safe Evidence-Entry and Bundle Digest Primitives

**Files:**
- Modify: `assurance_agent/evidence/digests.py`
- Modify: `assurance_agent/workflow/issues/collector.py`
- Modify: `tests/unit/evidence/test_digests.py`
- Modify: `tests/unit/workflow/issues/test_collector.py`

**Interfaces:**
- Produces: `EVIDENCE_ENTRY_DIGEST_SEMANTICS`, `EvidenceEntryPathError`, `ValidatedEvidenceEntry`, `raw_sha256`, `evidence_entry_digest_v1`, `evidence_bundle_digest_v1`, `normalize_evidence_entry_path`, and `read_evidence_entry_v1`.
- Preserves: collector V1 manifest bytes for every existing fixture, including redaction order and trailing/canonical JSON behavior.
- Supplies: manifest revalidation and raw source facts to Task 9 without reopening files.

- [ ] **Step 1: Add failing digest compatibility and one-read tests**

```python
@pytest.mark.parametrize(
    ("content", "expected_digest"),
    [
        (b'{"access_token":"abcdefghijklmnop"}', ENTRY_DIGEST_GOLDENS["secret"]),
        ("普通文本".encode(), ENTRY_DIGEST_GOLDENS["utf8"]),
        (b"\xff\x00\x80", ENTRY_DIGEST_GOLDENS["binary"]),
    ],
)
def test_entry_digest_v1_matches_collector_semantics(
    content: bytes,
    expected_digest: str,
) -> None:
    assert evidence_entry_digest_v1(content) == expected_digest
```

Define `ENTRY_DIGEST_GOLDENS` as literal values captured from the current collector before extraction; do not compute the expectations through the new helper. Assert changing visible text/binary changes entry digest, changing only a secret value preserves the redacted digest but changes `raw_sha256`, and bundle digest is canonical path order. Add mutation tests that remove, reorder, or change a redaction pattern/replacement and prove at least one pinned v1 golden digest changes.

Instrument `os.open`/`os.read` or the injected reader seam to prove each accepted entry is opened once and the returned bytes produce both hashes.

- [ ] **Step 2: Add failing path/symlink/race tests**

Reject absolute paths, backslashes, `..`, empty/dot segments, duplicate normalized paths, prefixes outside the six-item allowlist, a symlinked intermediate directory, a symlinked final file, non-regular files, and a component swapped to a symlink between validation and open.

```python
@pytest.mark.parametrize(
    "path",
    [
        "/tmp/result.json",
        "../result.json",
        "execution/../../secret",
        "unknown/result.json",
        "execution\\result.json",
    ],
)
def test_normalize_evidence_entry_path_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(EvidenceEntryPathError):
        normalize_evidence_entry_path(path)
```

- [ ] **Step 3: Run digest/collector tests and observe missing helper failures**

```bash
uv run pytest -q \
  tests/unit/evidence/test_digests.py \
  tests/unit/workflow/issues/test_collector.py
```

Expected: new helper imports fail.

- [ ] **Step 4: Implement exact digest primitives**

```python
EVIDENCE_ENTRY_DIGEST_SEMANTICS = "evidence_entry_digest/v1"
EVIDENCE_ENTRY_PREFIXES = (
    "execution",
    "cases",
    "facts",
    "review",
    "healing",
    "codegen",
)


class EvidenceEntryPathError(ValueError):
    """A manifest evidence path cannot be opened under the allowed root."""


@dataclass(frozen=True, slots=True)
class ValidatedEvidenceEntry:
    path: str
    data: bytes
    entry_digest: str
    raw_sha256: str


def raw_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def evidence_entry_digest_v1(data: bytes) -> str:
    try:
        digest_input = redact_evidence_text_v1(data.decode("utf-8")).encode("utf-8")
    except UnicodeDecodeError:
        digest_input = data
    return f"sha256:{raw_sha256(digest_input)}"


def evidence_bundle_digest_v1(
    entries: Sequence[IssueEvidenceManifestEntry],
) -> str:
    canonical = json.dumps(
        [
            {"digest": entry.digest, "path": entry.path}
            for entry in sorted(entries, key=lambda item: item.path)
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"sha256:{raw_sha256(canonical)}"
```

Move the existing ordered regex tuple and replacement literal unchanged into `redact_evidence_text_v1`.

- [ ] **Step 5: Implement no-follow component walking and migrate collector**

`read_evidence_entry_v1` must normalize first, open the change root directory, walk every directory component with `O_DIRECTORY | O_NOFOLLOW` and `dir_fd`, open the final regular file with `O_RDONLY | O_NOFOLLOW`, read it to EOF once, close every descriptor in `finally`, then construct `ValidatedEvidenceEntry` from the captured bytes.

Collector `_build_evidence_manifest` calls this helper for each sorted unique path and calls `evidence_bundle_digest_v1`. Delete collector-local `_REDACT_PATTERNS`, `_redact`, `_sha256_*`, and bundle canonicalization.

- [ ] **Step 6: Run compatibility, race, and static checks**

```bash
uv run pytest -q \
  tests/unit/evidence/test_digests.py \
  tests/unit/workflow/issues/test_collector.py
uv run ruff check .
uv run pyright
```

Expected: all existing collector manifest golden bytes remain unchanged and all unsafe/race cases fail closed.

- [ ] **Step 7: Commit the digest foundation**

```bash
git add assurance_agent/evidence/digests.py assurance_agent/workflow/issues/collector.py tests/unit/evidence/test_digests.py tests/unit/workflow/issues/test_collector.py
git diff --cached --check
git commit -m "refactor(evidence): centralize safe manifest digests"
```

---

### Task 7: Move Issue Schemas, Identities, and Replay Below Workflow

**Files:**
- Create: `assurance_agent/artifacts/models/issue_events.py`
- Modify: `assurance_agent/artifacts/models/__init__.py`
- Create: `assurance_agent/evidence/issue_identity.py`
- Create: `assurance_agent/evidence/issue_replay.py`
- Modify: `assurance_agent/workflow/issues/identity.py`
- Modify: `assurance_agent/workflow/issues/events.py`
- Modify: `assurance_agent/workflow/issues/projection.py`
- Modify: `assurance_agent/workflow/issues/history.py`
- Modify: `assurance_agent/workflow/issues/collector.py`
- Modify: `assurance_agent/workflow/issues/reconciler.py`
- Modify: `assurance_agent/workflow/issues/ledger.py`
- Modify: `assurance_agent/workflow/issues/review.py`
- Modify: `assurance_agent/workflow/issues/operations.py`
- Modify: `tests/unit/workflow/issues/test_identity.py`
- Modify: `tests/unit/workflow/issues/test_events.py`
- Modify: `tests/unit/workflow/issues/test_projection.py`
- Modify: `tests/unit/workflow/issues/test_ledger.py`
- Modify: `tests/unit/workflow/issues/test_history.py`
- Modify: `tests/unit/workflow/issues/test_reconciler.py`
- Modify: `tests/unit/workflow/issues/test_review.py`
- Modify: `tests/unit/workflow/issues/test_operations.py`

**Interfaces:**
- Artifacts produces: every existing change/problem event model plus `ChangeIssueEvent`, `ProblemEvent`, and their type adapters.
- Evidence identity produces: `candidate_document_digest`, `per_candidate_digest`, `observation_id`, `occurrence_id`, `reconciliation_idempotency_key`, `problem_fingerprint`, `problem_id`, `event_id`, and `recomputable_issue_event_idempotency_key`.
- Evidence replay produces: `IssueLedgerMissingError`, `IssueLedgerIntegrityError`, `load_change_issue_ledger`, `load_problem_ledger`, `read_change_issue_events_from_bytes`, `read_problem_events_from_bytes`, `project_change_issues`, `project_problems`, `project_review_queue`, and `dump_projection`.
- Workflow keeps mutation stores and compatibility re-exports. `evidence` imports no `workflow` module.

- [ ] **Step 1: Freeze existing identity/event/projection bytes with characterization tests**

Before moving code, serialize representative events for every union arm and pin:

```python
def test_event_id_matches_existing_sha256_prefix() -> None:
    key = "project_sync_pending:CH-1:B1:sha256:candidate"
    assert event_id(key) == "EVT-" + hashlib.sha256(key.encode()).hexdigest()[:16]


def test_workflow_and_foundational_projectors_are_byte_identical(
    typed_change_events: tuple[ChangeIssueEvent, ...],
    typed_problem_events: tuple[ProblemEvent, ...],
) -> None:
    assert dump_projection(project_change_issues(typed_change_events)) == EXPECTED_CHANGE_BYTES
    assert dump_projection(project_problems(typed_problem_events)) == EXPECTED_PROJECT_BYTES
```

Pin candidate/per-candidate/observation/occurrence/problem identities and all current idempotency-key formats. Prove candidate mapping key order and whitespace/JSON formatting do not change the canonical digest, while a semantic field change does. For non-recomputable legacy events, assert `recomputable_issue_event_idempotency_key(event) is None`.

- [ ] **Step 2: Add failing strict replay identity tests**

Mutate sequential IDs, duplicate event/idempotency IDs, blank lines, unknown events, `event_id`, envelope/nested change or batch, nested evidence digest, observation ID, occurrence ID, expected problem version, and every recomputable key. Add distinct event envelopes that attempt to define the same observation ID twice and the same occurrence ID twice; references to an already-defined ID remain legal, duplicate defining events do not.

```python
def test_strict_change_replay_rejects_forged_event_id(tmp_path: Path) -> None:
    path = write_change_ledger(tmp_path, mutate={"event_id": "EVT-forged"})
    with pytest.raises(IssueLedgerIntegrityError, match="event_id"):
        load_change_issue_ledger(path)


def test_authority_loader_distinguishes_missing_from_empty(tmp_path: Path) -> None:
    with pytest.raises(IssueLedgerMissingError):
        load_problem_ledger(tmp_path / "missing.jsonl")
```

Keep a compatibility assertion that `workflow.issues.events.read_problem_events(missing_path) == []` for existing mutation callers.

- [ ] **Step 3: Run the characterization/replay suite and observe missing lower modules**

```bash
uv run pytest -q \
  tests/unit/workflow/issues/test_identity.py \
  tests/unit/workflow/issues/test_events.py \
  tests/unit/workflow/issues/test_projection.py \
  tests/unit/workflow/issues/test_ledger.py \
  tests/unit/workflow/issues/test_history.py \
  tests/unit/workflow/issues/test_reconciler.py \
  tests/unit/workflow/issues/test_review.py \
  tests/unit/workflow/issues/test_operations.py
```

Expected: new lower-layer imports fail.

- [ ] **Step 4: Move event schemas and canonical identity implementations**

`issue_events.py` contains only Pydantic event models/adapters and imports only artifacts. Move all pure identity bodies without changing canonical JSON, token normalization, digest prefixes, or key formats.

```python
def event_id(idempotency_key: str) -> str:
    return "EVT-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]


def per_candidate_digest(candidate: IssueCandidate | Mapping[str, object]) -> str:
    payload = (
        candidate.model_dump(mode="json")
        if isinstance(candidate, IssueCandidate)
        else dict(candidate)
    )
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ) + "\n"
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

Implement `recomputable_issue_event_idempotency_key` as a match over event classes. Return exact existing keys for observation, analysis completed/failed, occurrence detected/linked, project sync pending, problem detected/linked/regressed/resolved, human review transitions, merge, and verification request when all preimage fields are persisted; return `None` for legacy formats whose preimage is absent.

- [ ] **Step 5: Implement strict replay and pure projection**

```python
def load_change_issue_ledger(path: Path) -> tuple[ChangeIssueEvent, ...]:
    if not path.is_file():
        raise IssueLedgerMissingError(str(path))
    return tuple(_read_strict_bytes(path.read_bytes(), CHANGE_ISSUE_EVENT_ADAPTER, path))


def validate_event_identity(event: ChangeIssueEvent | ProblemEvent) -> None:
    if event.event_id != event_id(event.idempotency_key):
        raise IssueLedgerIntegrityError("event_id mismatch")
    expected_key = recomputable_issue_event_idempotency_key(event)
    if expected_key is not None and event.idempotency_key != expected_key:
        raise IssueLedgerIntegrityError("idempotency_key mismatch")
    validate_event_nested_identity(event)
```

`_read_strict_bytes` enforces UTF-8, no blank holes, sequential strict-int `seq`, unique event/key, unique defining observation IDs, unique defining occurrence IDs, adapter validation, and `validate_event_identity`. Move projectors and canonical dump unchanged. Authority loaders raise on missing; workflow compatibility wrappers explicitly return empty only for missing paths.

- [ ] **Step 6: Replace workflow implementations with imports/re-exports**

`workflow/issues/identity.py`, `events.py`, and `projection.py` become thin compatibility facades over the new modules. Update collector, reconciler, operations, ledger, review, and history to import the lower-layer functions directly. Delete every workflow-local `_event_id` implementation (reconciler, review, and operations) plus reconciler-local `_per_candidate_digest` and `_batch_candidate_digest`. Add an AST guard asserting no `def _event_id` remains under `workflow/issues` and every event writer imports the foundational `event_id`.

Add an AST/import test asserting no module under `assurance_agent/evidence` imports `assurance_agent.workflow`.

- [ ] **Step 7: Run identity/replay, import-layer, and static checks**

```bash
uv run pytest -q \
  tests/unit/workflow/issues/test_identity.py \
  tests/unit/workflow/issues/test_events.py \
  tests/unit/workflow/issues/test_projection.py \
  tests/unit/workflow/issues/test_ledger.py \
  tests/unit/workflow/issues/test_history.py \
  tests/unit/workflow/issues/test_reconciler.py \
  tests/unit/workflow/issues/test_review.py \
  tests/unit/workflow/issues/test_operations.py
uv run lint-imports
uv run ruff check .
uv run pyright
```

Expected: all characterization bytes match and the import boundary passes.

- [ ] **Step 8: Commit the pure issue-domain downshift**

```bash
git add assurance_agent/artifacts/models/issue_events.py assurance_agent/artifacts/models/__init__.py assurance_agent/evidence/issue_identity.py assurance_agent/evidence/issue_replay.py assurance_agent/workflow/issues/identity.py assurance_agent/workflow/issues/events.py assurance_agent/workflow/issues/projection.py assurance_agent/workflow/issues/history.py assurance_agent/workflow/issues/collector.py assurance_agent/workflow/issues/reconciler.py assurance_agent/workflow/issues/ledger.py assurance_agent/workflow/issues/review.py assurance_agent/workflow/issues/operations.py tests/unit/workflow/issues/test_identity.py tests/unit/workflow/issues/test_events.py tests/unit/workflow/issues/test_projection.py tests/unit/workflow/issues/test_ledger.py tests/unit/workflow/issues/test_history.py tests/unit/workflow/issues/test_reconciler.py tests/unit/workflow/issues/test_review.py tests/unit/workflow/issues/test_operations.py
git diff --cached --check
git commit -m "refactor(issues): move replay authority below workflow"
```

---

### Task 8: Add Reconcile-Status V2 and a Digest-Bound Pending Recovery Writer

**Files:**
- Modify: `assurance_agent/artifacts/models/issues.py`
- Modify: `assurance_agent/artifacts/models/__init__.py`
- Modify: `assurance_agent/artifacts/registry.py`
- Modify: `assurance_agent/workflow/issues/operations.py`
- Modify: `assurance_agent/workflow/report/report_builder.py`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Modify: `tests/unit/artifacts/test_models_issues.py`
- Modify: `tests/unit/artifacts/test_registry.py`
- Modify: `tests/unit/artifacts/test_validate.py`
- Modify: `tests/unit/workflow/issues/test_operations.py`
- Modify: `tests/unit/workflow/graph/test_contracts.py`
- Modify: `tests/unit/report/test_report_builder.py`

**Interfaces:**
- Produces: `IssueReconcileStatusV1`, `IssueReconcileStatusV2`, `IssueReconcileStatusDocument`, `IssueReconcileStatusLike`, and `load_issue_reconcile_status_document(raw: object)`.
- Preserves: `IssueReconcileStatus = IssueReconcileStatusV1` and all V1 fixtures as legacy-only facts.
- Changes: completed, failed, and pending writers emit V2; pending operation writes `inspect/issue-reconcile-status.json` in the same task write-set.

- [ ] **Step 1: Add failing V1/V2 status model tests**

```python
@pytest.mark.parametrize(
    ("status", "count", "error"),
    [
        ("completed", 0, None),
        ("failed", None, "candidate validation failed"),
        ("pending", None, None),
    ],
)
def test_reconcile_status_v2_state_shapes(
    status: str,
    count: int | None,
    error: str | None,
) -> None:
    loaded = IssueReconcileStatusV2(
        schema_version="2.0",
        change_id="CH-1",
        batch_id="B1",
        status=status,
        evidence_bundle_digest="sha256:evidence",
        candidate_digest="sha256:candidate",
        occurrence_count=count,
        error=error,
    )
    assert loaded.status == status
```

Mutate missing candidate digest, completed missing count, completed error, failed count/null error, pending count/error, unknown version, and V1 pending. Assert only the declared V2 shapes pass.

- [ ] **Step 2: Add failing pending-operation and semantic-failure tests**

Replace the existing fallback test with:

```python
def test_record_sync_pending_fails_without_candidates_and_writes_nothing(
    tmp_path: Path,
) -> None:
    workspace = pending_workspace(tmp_path, include_candidates=False)
    result = record_project_sync_pending_operation(task(), workspace, context())
    assert result.status == "failed"
    assert not (workspace.change_dir / "issues" / "events.jsonl").exists()
    assert not (workspace.change_dir / "inspect" / "issue-reconcile-status.json").exists()
```

Assert wrong candidate change/batch/evidence digest fails before any mutation. Happy/idempotent pending must write a V2 status whose candidate digest equals `candidate_document_digest(authored_json)`. Semantic reconcile failure must also write V2 with that digest and a non-empty error; success writes count.

- [ ] **Step 3: Run the status/operation tests and observe failures**

```bash
uv run pytest -q \
  tests/unit/artifacts/test_models_issues.py \
  tests/unit/artifacts/test_registry.py \
  tests/unit/artifacts/test_validate.py \
  tests/unit/workflow/issues/test_operations.py \
  tests/unit/workflow/graph/test_contracts.py \
  tests/unit/report/test_report_builder.py
```

Expected: V2 symbols are missing and the old pending fallback assertion exposes current behavior.

- [ ] **Step 4: Implement the status document**

```python
class IssueReconcileStatusV2(BaseModel):
    model_config = _FROZEN
    schema_version: Literal["2.0"] = "2.0"
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    status: Literal["completed", "failed", "pending"]
    evidence_bundle_digest: NonEmptyStr
    candidate_digest: NonEmptyStr
    occurrence_count: Annotated[int, Field(strict=True, ge=0)] | None = None
    error: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _state_shape(self) -> Self:
        if self.status == "completed":
            if self.occurrence_count is None or self.error is not None:
                raise ValueError("completed requires occurrence_count and null error")
        elif self.status == "failed":
            if self.occurrence_count is not None or self.error is None:
                raise ValueError("failed requires null count and non-empty error")
        elif self.occurrence_count is not None or self.error is not None:
            raise ValueError("pending requires null count and null error")
        return self


IssueReconcileStatusLike = IssueReconcileStatusV1 | IssueReconcileStatusV2
```

Add the V1/V2 discriminator, decoded-object loader, V1 alias, exports, and registry wrapper.

- [ ] **Step 5: Make every new status writer V2 and fail closed**

`record_project_sync_pending_operation` must strictly decode `IssueEvidenceManifest` and authored `IssueCandidateDocument`, validate exact change/batch/evidence binding, compute the authored candidate digest, construct the pending event, rebuild the change store, and write:

```python
pending_status = IssueReconcileStatusV2(
    change_id=context.change_id,
    batch_id=candidates.batch_id,
    status="pending",
    evidence_bundle_digest=candidates.evidence_bundle_digest,
    candidate_digest=authored_candidate_digest,
)
```

Delete the evidence-digest fallback. In `reconcile_issues_operation` construct V2 for both semantic failure and completed output. Add `change:inspect/issue-reconcile-status.json` to pending `writes` and `authorization_writes`. Change report builder's status reader to the shared loader and branch explicitly on V1/V2.

- [ ] **Step 6: Run status, workflow, and static checks**

```bash
uv run pytest -q \
  tests/unit/artifacts/test_models_issues.py \
  tests/unit/artifacts/test_registry.py \
  tests/unit/artifacts/test_validate.py \
  tests/unit/workflow/issues/test_operations.py \
  tests/unit/workflow/graph/test_contracts.py \
  tests/unit/report/test_report_builder.py
uv run ruff check .
uv run pyright
```

Expected: all commands pass; missing/malformed candidates never create pending authority.

- [ ] **Step 7: Commit reconcile-status V2**

```bash
git add assurance_agent/artifacts/models/issues.py assurance_agent/artifacts/models/__init__.py assurance_agent/artifacts/registry.py assurance_agent/workflow/issues/operations.py assurance_agent/workflow/report/report_builder.py assurance_agent/_resources/schemas/execution-contracts.yaml tests/unit/artifacts/test_models_issues.py tests/unit/artifacts/test_registry.py tests/unit/artifacts/test_validate.py tests/unit/workflow/issues/test_operations.py tests/unit/workflow/graph/test_contracts.py tests/unit/report/test_report_builder.py
git diff --cached --check
git commit -m "feat(issues): bind reconcile recovery status v2"
```

---

### Task 9: Validate Failure Authority and the Current Issue-Recovery Prefix

**Files:**
- Create: `assurance_agent/evidence/trace_authority.py`
- Create: `tests/unit/evidence/test_issue_replay_authority.py`
- Modify: `tests/unit/evidence/test_fold_trace_reconciled.py`

**Interfaces:**
- Produces: `AuthoritySource`, `AuthorityReason`, `AuthorityValidationError`, `FailureAuthorityResult`, `ValidatedAuthorityPrefix`, `AuthorityPrefixResult`, `validate_failure_authority`, and `validate_issue_authority_prefix`.
- Consumes: Task 6 safe manifest digests, Task 7 strict replay/identity, Task 8 status loader, and `TraceSourceRecorder`.
- Defers: completed project/history membership is implemented in Tasks 10–11; `fold_trace` does not call this module until Task 12.

- [ ] **Step 1: Add failing failure-authority truth-table tests**

Parameterize missing, malformed, wrong change, wrong batch, wrong source batch, and valid failure analysis. Assert exactly one canonical code/detail and no untrusted failure links:

```python
@pytest.mark.parametrize(
    ("mutation", "code", "detail"),
    [
        ("missing", "failure_analysis_missing", "reason=missing"),
        ("malformed", "failure_analysis_missing", "reason=malformed"),
        ("wrong_change", "failure_analysis_identity_mismatch", "reason=change_id_mismatch"),
        ("wrong_batch", "failure_analysis_identity_mismatch", "reason=batch_id_mismatch"),
        (
            "wrong_source_batch",
            "failure_analysis_identity_mismatch",
            "reason=source_batch_id_mismatch",
        ),
    ],
)
def test_failure_authority_has_one_canonical_gap(
    authority_tree: Path,
    mutation: str,
    code: str,
    detail: str,
) -> None:
    mutate_failure(authority_tree, mutation)
    result = validate_failure_authority(authority_tree, CHANGE_ID, BATCH_ID)
    assert result.failures_by_case == {}
    assert [(gap.code, gap.detail) for gap in result.gaps] == [(code, detail)]
```

Add a multi-error mutation proving precedence is `missing > malformed > change > batch > source_batch`.

- [ ] **Step 2: Add failing manifest/candidate/observation prefix tests**

Cover manifest missing/malformed/change/batch, duplicate or unsafe entry, missing execution anchor, entry digest mismatch, bundle digest mismatch, candidates missing/malformed/change/batch/evidence mismatch, observations missing/malformed/change/batch/replay mismatch, and strict change-ledger errors.

For every case assert the exact machine tuple:

```python
result = validate_issue_authority_prefix(authority_tree, CHANGE_ID, BATCH_ID)
assert result.state == "unavailable"
assert [
    (gap.code, gap.source, gap.batch_id, gap.detail)
    for gap in result.gaps
] == [
    (
        "issue_reconciliation_unavailable",
        EXPECTED_SOURCE[mutation],
        BATCH_ID,
        f"reason={EXPECTED_REASON[mutation]}",
    )
]
assert result.validated is None
```

Synchronously mutate manifest `digest` and every downstream M reference; assert rehashed bundle still rejects it. Include UTF-8 secret, visible UTF-8, and binary round-trips and exact execution-anchor rehash. For manifest, candidates, observations, change ledger, persisted snapshot, and reconcile status, inject two same-source errors and assert the source-local precedence from spec section 7.2. Inject errors in adjacent sources and assert cross-source prefix order: manifest, candidates, observations, change ledger, then the selected recovery/completed branch.

- [ ] **Step 3: Add failing recovery-state precedence tests**

Create strict ledgers for:

- current `IssueAnalysisFailedEvent` plus replayed failed snapshot;
- current V2 reconcile `failed`;
- current `ProjectSyncPendingEvent` plus V2 `pending`;
- V1 status carrying similar optional fields;
- status without the required recovery event;
- digest/identity mismatch in each state.

For analysis failed, separately mutate `candidate_count` away from `len(candidates)` and assert the result is unavailable rather than `issue_analysis_failed`.

```python
@pytest.mark.parametrize(
    ("state", "expected_code"),
    [
        ("analysis_failed", "issue_analysis_failed"),
        ("reconcile_failed", "issue_reconcile_failed"),
        ("project_sync_pending", "project_sync_pending"),
    ],
)
def test_recovery_prefix_returns_one_blocking_state(
    authority_tree: Path,
    state: str,
    expected_code: str,
) -> None:
    write_recovery_state(authority_tree, state)
    result = validate_issue_authority_prefix(authority_tree, CHANGE_ID, BATCH_ID)
    assert result.state == state
    assert [gap.code for gap in result.gaps] == [expected_code]
    assert result.validated is None
```

Prove analysis failed masks later missing reconcile/project inputs, reconcile failed masks stale/missing persisted snapshots and project inputs, pending masks snapshot authoritative-batch mismatch/project, and malformed manifest/candidate/observation/ledger prefix inputs are never masked. For each masked file, still assert its `TraceSource` exists/missing fact is recorded.

- [ ] **Step 4: Run authority tests and observe missing module failures**

```bash
uv run pytest -q \
  tests/unit/evidence/test_issue_replay_authority.py \
  tests/unit/evidence/test_fold_trace_reconciled.py
```

Expected: the new authority module and truth-table results do not exist.

- [ ] **Step 5: Implement stable reason/source/gap mapping**

```python
AuthoritySource = Literal[
    "inspect/failure-analysis.json",
    "inspect/issue-evidence-manifest.json",
    "inspect/issue-candidates.json",
    "inspect/observations.json",
    "issues/events.jsonl",
    "issues/snapshot.json",
    "inspect/issue-reconcile-status.json",
    "qa/issues/events.jsonl",
    "qa/issues/problems.json",
]


AuthorityReason = Literal[
    "missing",
    "malformed",
    "change_id_mismatch",
    "batch_id_mismatch",
    "source_batch_id_mismatch",
    "entry_path_invalid",
    "entry_duplicate",
    "execution_anchor_missing",
    "entry_digest_mismatch",
    "bundle_digest_mismatch",
    "evidence_digest_mismatch",
    "candidate_digest_mismatch",
    "observations_replay_mismatch",
    "ledger_missing",
    "ledger_malformed",
    "event_identity_mismatch",
    "projection_replay_mismatch",
    "recovery_event_missing",
    "analysis_pending",
    "candidate_count_mismatch",
    "occurrence_count_mismatch",
    "occurrence_set_mismatch",
    "occurrence_identity_mismatch",
    "observation_reference_invalid",
    "problem_occurrence_mismatch",
    "status_inconsistent",
]


class AuthorityValidationError(ValueError):
    def __init__(self, source: AuthoritySource, reason: AuthorityReason) -> None:
        super().__init__(f"{source}:{reason}")
        self.source = source
        self.reason = reason


@dataclass(frozen=True, slots=True)
class FailureAuthorityResult:
    sources: tuple[TraceSource, ...]
    gaps: tuple[TraceGapV2, ...]
    failures_by_case: Mapping[str, tuple[TraceFailure, ...]]


@dataclass(frozen=True, slots=True)
class ValidatedAuthorityPrefix:
    manifest_digest: str
    candidate_digest: str
    manifest: IssueEvidenceManifest
    candidates: IssueCandidateDocument
    observations: ObservationDocument
    change_events: tuple[ChangeIssueEvent, ...]
    replayed_snapshot: ChangeIssueSnapshot
    reconcile_status: IssueReconcileStatusV2


@dataclass(frozen=True, slots=True)
class AuthorityPrefixResult:
    state: Literal[
        "completed",
        "analysis_failed",
        "reconcile_failed",
        "project_sync_pending",
        "unavailable",
    ]
    sources: tuple[TraceSource, ...]
    gaps: tuple[TraceGapV2, ...]
    validated: ValidatedAuthorityPrefix | None
```

`AuthoritySource` is a closed literal for failure analysis, manifest, candidates, observations, change ledger, change snapshot, reconcile status, project ledger, and project problems. Internal validators raise `AuthorityValidationError` with one closed source/reason; public authority evaluators translate it. Build gaps only through one `authority_gap(code, source, batch_id, reason)` helper so detail is always `reason=<literal>` and never a validator exception string.

- [ ] **Step 6: Implement prefix validation and recovery precedence**

Implement the separate failure seam first:

```python
def validate_failure_authority(
    change_dir: Path,
    change_id: str,
    batch_id: str,
) -> FailureAuthorityResult:
    """Return current validated failure links or one canonical failure gap."""
```

Read `inspect/failure-analysis.json` once, always record its `TraceSource`, and classify in the exact precedence `missing > malformed > change_id > batch_id > source_batch_id`. On a valid document, group typed failures by case ID into lexically ordered immutable tuples; on any expected failure return an empty mapping plus exactly one `failure_analysis_missing` or `failure_analysis_identity_mismatch` gap from `authority_gap`. This seam never returns `AuthorityPrefixResult`, never raises raw JSON/model messages, and never mixes failure gaps with issue-authority gaps.

Validation order is exactly spec section 7.2:

1. manifest safe entry set, one execution anchor, entry/bundle rehash;
2. candidates identity and M binding; compute C;
3. observations identity and exact current replay set;
4. strict change ledger/event identity and derive the canonical in-memory snapshot;
5. when the ledger proves current analysis-failed, require persisted snapshot canonical equality and its failed analysis M/C/count chain, then return analysis failed;
6. otherwise inspect current V2 reconcile status: a valid failed status returns reconcile failed before persisted snapshot validation;
7. a ledger-proven project-sync-pending event plus valid V2 pending status returns pending before persisted snapshot batch/equality validation;
8. only the completed branch requires persisted snapshot existence, identity, canonical replay equality, completed analysis M/C/count, completed reconcile M/C/count, and project authority.

Record every declared source in `TraceSourceRecorder` even when a higher-priority state masks its gap. Return at most one issue gap. V1 reconcile status can be parsed but always produces `issue_reconciliation_unavailable` for current authority.

- [ ] **Step 7: Run authority and static checks**

```bash
uv run pytest -q \
  tests/unit/evidence/test_issue_replay_authority.py \
  tests/unit/evidence/test_fold_trace_reconciled.py
uv run ruff check .
uv run pyright
uv run lint-imports
```

Expected: all prefix/recovery cases pass with one stable gap.

- [ ] **Step 8: Commit the current authority prefix**

```bash
git add assurance_agent/evidence/trace_authority.py tests/unit/evidence/test_issue_replay_authority.py tests/unit/evidence/test_fold_trace_reconciled.py
git diff --cached --check
git commit -m "feat(trace): validate reconciliation authority prefix"
```

---

### Task 10: Prove Completed Current-Batch Occurrence and Project Membership

**Files:**
- Modify: `assurance_agent/evidence/trace_authority.py`
- Modify: `tests/unit/evidence/test_issue_replay_authority.py`
- Modify: `tests/unit/evidence/test_fold_trace_reconciled.py`

**Interfaces:**
- Produces: `ValidatedCompletedAuthority` and `validate_completed_authority(prefix, project_root, change_id, batch_id)`.
- Consumes: `ValidatedAuthorityPrefix` from Task 9 and shared per-candidate/occurrence/problem identities from Task 7.
- Establishes: exact current candidate→occurrence→observation→source-problem membership before any problem link is permitted.

- [ ] **Step 1: Add failing candidate/count/exact-set tests**

Start from a valid completed fixture and mutate candidate count, reconcile count, missing/duplicate/additional current occurrence, wrong occurrence ID, wrong per-candidate digest, wrong problem ID, wrong evidence digest, and dangling/cross-change/cross-batch observation references.

```python
@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("candidate_count", "candidate_count_mismatch"),
        ("occurrence_count", "occurrence_count_mismatch"),
        ("missing_occurrence", "occurrence_set_mismatch"),
        ("duplicate_occurrence", "occurrence_set_mismatch"),
        ("wrong_occurrence_identity", "occurrence_identity_mismatch"),
        ("dangling_observation", "observation_reference_invalid"),
    ],
)
def test_completed_authority_rejects_current_set_mutations(
    completed_tree: Path,
    mutation: str,
    reason: str,
) -> None:
    prefix = completed_prefix(completed_tree)
    mutate_completed_tree(completed_tree, mutation)
    with pytest.raises(AuthorityValidationError) as raised:
        validate_completed_authority(
            prefix,
            completed_tree,
            CHANGE_ID,
            BATCH_ID,
        )
    assert raised.value.reason == reason
```

Count equality with a different occurrence set must fail.

- [ ] **Step 2: Add failing strict project replay and genesis tests**

Cover project ledger missing/malformed/version/event identity, problems projection missing/malformed, replay mismatch, missing/duplicate/wrong current membership, invalid alias, and these genesis cases. Assert the typed failure source/reason at this layer, then in Task 11 assert the public gap mapping: non-genesis project-ledger failures become `issue_reconciliation_unavailable` sourced at `qa/issues/events.jsonl`; missing/malformed project problems become `problems_snapshot_missing` sourced at `qa/issues/problems.json`; projection or membership mismatch becomes `issue_reconciliation_unavailable` sourced at `qa/issues/problems.json`.

| Project events | Problems file | Change history occurrences | Result |
|---|---|---:|---|
| missing | missing | 0 | canonical empty accepted |
| missing | present | any | reject |
| present | missing | any | reject |
| missing | missing | >0 | reject |

Assert a valid completed empty candidate batch with true genesis passes.

- [ ] **Step 3: Run completed-authority tests and observe missing validation**

```bash
uv run pytest -q \
  tests/unit/evidence/test_issue_replay_authority.py \
  tests/unit/evidence/test_fold_trace_reconciled.py \
  -k 'completed or occurrence or project or genesis'
```

Expected: current implementation accepts count/set/project mismatches or lacks the interface.

- [ ] **Step 4: Recompute every expected current occurrence**

```python
@dataclass(frozen=True, slots=True)
class ExpectedOccurrence:
    occurrence_id: str
    problem_id: str
    observation_ids: tuple[str, ...]
    per_candidate_digest: str


def expected_occurrences(
    candidates: IssueCandidateDocument,
) -> dict[str, ExpectedOccurrence]:
    result: dict[str, ExpectedOccurrence] = {}
    for candidate in candidates.candidates:
        digest = per_candidate_digest(candidate)
        expected = ExpectedOccurrence(
            occurrence_id=occurrence_id(candidates.change_id, candidates.batch_id, digest),
            problem_id=problem_id(
                problem_fingerprint(
                    affected_surface=candidate.affected_surface,
                    fingerprint_inputs=candidate.fingerprint_inputs,
                )
            ),
            observation_ids=tuple(candidate.observation_ids),
            per_candidate_digest=digest,
        )
        if expected.occurrence_id in result:
            raise AuthorityValidationError(
                "issues/snapshot.json",
                "occurrence_set_mismatch",
            )
        result[expected.occurrence_id] = expected
    return result
```

Compare exact sets with no duplicates, then compare every nested identity, M, C, problem, and observation relation. Do not accept a count-only match.

- [ ] **Step 5: Strictly replay project authority and verify current membership**

```python
@dataclass(frozen=True, slots=True)
class ValidatedCompletedAuthority:
    prefix: ValidatedAuthorityPrefix
    problem_events: tuple[ProblemEvent, ...]
    replayed_problems: ProblemProjection
    expected_occurrence_ids: frozenset[str]
```

If and only if both project files are absent and replayed change history has zero occurrences, construct `ProblemProjection(schema_version="1.0", problems=[], generated_at="1970-01-01T00:00:00Z")`, matching the existing first-reconcile representation. Otherwise both files are required: strict replay, canonical JSON equality, and project version rules must pass.

For every current expected occurrence, require membership exactly once in the `occurrence.problem_id` source Problem; validate the unique acyclic source→terminal alias chain but do not require target ownership.

- [ ] **Step 6: Run completed authority and static checks**

```bash
uv run pytest -q \
  tests/unit/evidence/test_issue_replay_authority.py \
  tests/unit/evidence/test_fold_trace_reconciled.py \
  -k 'completed or occurrence or project or genesis'
uv run ruff check .
uv run pyright
```

Expected: exact sets, project replay, membership, and genesis all pass.

- [ ] **Step 7: Commit completed current authority**

```bash
git add assurance_agent/evidence/trace_authority.py tests/unit/evidence/test_issue_replay_authority.py tests/unit/evidence/test_fold_trace_reconciled.py
git diff --cached --check
git commit -m "feat(trace): prove completed occurrence membership"
```

---

### Task 11: Validate Historical Cross-Ledger Links and Merge Ownership

**Files:**
- Modify: `assurance_agent/evidence/trace_authority.py`
- Modify: `tests/unit/evidence/test_issue_replay_authority.py`
- Modify: `tests/unit/evidence/test_fold_trace_reconciled.py`

**Interfaces:**
- Produces: `ReconciledAuthorityDecision` and `evaluate_reconciled_authority(project_root, change_dir, change_id, batch_id)`.
- Consumes: validated current prefix/completed authority from Tasks 9–10.
- Returns: validated failure links, validated historical/current open-problem links, all deduplicated sources, zero or one failure gap, zero or one issue gap, and zero or one independent project-projection gap. Raw snapshots never leave this boundary as authority.

- [ ] **Step 1: Add failing independently-valid-but-cross-ledger-mismatched fixtures**

Build two ledgers that each replay successfully, then inject:

- a B0 occurrence referencing a missing observation;
- a B0 occurrence referencing a B1 observation;
- source Problem missing the occurrence;
- occurrence membership only on merge target;
- membership on both source and target;
- wrong source owner;
- project-only current-change occurrence membership.
- a raw snapshot-only cross-change occurrence;
- a same-change forged historical occurrence absent from the strict ledger;
- duplicate observation identity hidden only in the snapshot.

```python
def test_valid_ledgers_with_cross_batch_historical_observation_are_unavailable(
    completed_tree: Path,
) -> None:
    make_each_ledger_individually_replayable(completed_tree)
    point_historical_occurrence_at_current_observation(completed_tree)
    decision = evaluate_reconciled_authority(
        completed_tree,
        change_dir(completed_tree),
        CHANGE_ID,
        BATCH_ID,
    )
    assert decision.open_problem_ids_by_case == {}
    assert [gap.detail for gap in decision.issue_gaps] == [
        "reason=observation_reference_invalid"
    ]
```

Every mismatch clears the whole problem-authority group; it must not silently skip one bad history item. Add explicit project-source mapping assertions for the three cases fixed in Task 10, including `code`, `source`, current `batch_id`, and stable `detail`.

- [ ] **Step 2: Add legal multi-batch and merge-source acceptance tests**

Create B0 source Problem P owning O, merge P→T, then a valid B1 completed batch. Assert:

```python
decision = evaluate_reconciled_authority(
    project_root,
    change_dir,
    CHANGE_ID,
    "B1",
)
assert decision.issue_gaps == ()
assert decision.open_problem_ids_by_case["TC_API_001"] == ("PROB-T",)
```

The occurrence stays in P's membership; T supplies canonical ID/open status. Also prove closed/non-product terminal problems produce no link, valid old open problems survive a later batch, canonical fingerprints deduplicate, and alias cycles/missing targets fail.

- [ ] **Step 3: Run historical authority tests and observe unsafe acceptance**

```bash
uv run pytest -q \
  tests/unit/evidence/test_issue_replay_authority.py \
  tests/unit/evidence/test_fold_trace_reconciled.py \
  -k 'historical or merge or membership or alias'
```

Expected: current fold either trusts snapshots directly or applies terminal ownership incorrectly.

- [ ] **Step 4: Implement bidirectional historical joins**

For every replayed change occurrence:

1. require every observation ID to resolve exactly once;
2. require observation/occurrence change and batch equality;
3. require `occurrence.problem_id` source Problem to exist and own the occurrence exactly once;
4. require the occurrence not to appear in another source Problem;
5. resolve one acyclic source→terminal chain;
6. reverse-check current-change project event memberships against the same change occurrence/source owner;
7. only then use terminal classification/status/canonical ID.

```python
@dataclass(frozen=True, slots=True)
class ReconciledAuthorityDecision:
    sources: tuple[TraceSource, ...]
    failure_gaps: tuple[TraceGapV2, ...]
    issue_gaps: tuple[TraceGapV2, ...]
    project_gaps: tuple[TraceGapV2, ...]
    failures_by_case: Mapping[str, tuple[TraceFailure, ...]]
    open_problem_ids_by_case: Mapping[str, tuple[str, ...]]
```

Use sorted immutable mappings/tuples at the boundary. The evaluation catches only declared source/model/authority failures and converts them through the complete section 7.2 truth table; programmer errors propagate. Preserve the existing `problems_snapshot_missing` mapping for missing/malformed project projection in `project_gaps` rather than collapsing it into the new unavailable code. The new issue-authority group still emits at most one `issue_*` authority gap; `project_gaps` independently contains zero or one existing project-projection gap.

Add recovery fixtures with missing/malformed project projection and assert both channels survive: exactly one recovery `issue_gap` plus exactly one `problems_snapshot_missing` `project_gap`, while problem links remain empty.

- [ ] **Step 5: Enforce canonical link semantics**

Terminal problems count as open links only when their status is not closed and their classification is `product_bug`. Deduplicate within a case by canonical fingerprint and emit canonical problem IDs in lexical order. Source Problem retains occurrence ownership after merge.

- [ ] **Step 6: Run the full authority suite and static checks**

```bash
uv run pytest -q \
  tests/unit/evidence/test_issue_replay_authority.py \
  tests/unit/evidence/test_fold_trace_reconciled.py
uv run ruff check .
uv run pyright
uv run lint-imports
```

Expected: valid multi-batch history remains visible and every cross-ledger mutation yields one unavailable gap.

- [ ] **Step 7: Commit historical authority validation**

```bash
git add assurance_agent/evidence/trace_authority.py tests/unit/evidence/test_issue_replay_authority.py tests/unit/evidence/test_fold_trace_reconciled.py
git diff --cached --check
git commit -m "feat(trace): validate historical issue links"
```

---

### Task 12: Activate Trace V2 Fold, Current Freshness Loading, and Blocking Verify

**Files:**
- Modify: `assurance_agent/evidence/trace.py`
- Create: `assurance_agent/evidence/current_projection.py`
- Modify: `assurance_agent/evidence/verify.py`
- Modify: `assurance_agent/commands/trace_cmd.py`
- Modify: `assurance_agent/commands/verify_cmd.py`
- Modify: `assurance_agent/workflow/graph/handlers/trace_projection.py`
- Modify: `assurance_agent/workflow/execution/runner.py`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Modify: `tests/unit/evidence/test_fold_trace_execution.py`
- Modify: `tests/unit/evidence/test_fold_trace_reconciled.py`
- Modify: `tests/unit/execution/test_runner_trace.py`
- Modify: `tests/unit/workflow/graph/handlers/test_trace_projection_operation.py`
- Modify: `tests/unit/workflow/graph/test_contracts.py`
- Modify: `tests/integration/test_trace_cli.py`
- Modify: `tests/integration/test_verify_cli.py`

**Interfaces:**
- Changes: every new `fold_trace` result is `TraceProjectionV2`; execution and reconciled both pass shared summary validation, and reconciled additionally passes phase-pair validation.
- Produces: `load_current_reconciled_projection(project_root, change_id) -> TraceProjectionV2`, `CurrentProjectionStaleReason`, `CurrentProjectionError`, and the three typed current-loader subclasses fixed above.
- Consumes: `evaluate_reconciled_authority` from Task 11 and all shared summary/digest/loaders.
- Expands: materializer exact read claims; write authority remains only `change:inspect/trace-projection.json`.

- [ ] **Step 1: Add failing fold activation and enrichment-rule tests**

Assert execution/reconciled folds emit schema `"2"`, call summary validation, and use only the authority decision:

```python
def test_recovery_fold_is_current_incomplete_without_old_problem_links(
    authority_tree: Path,
) -> None:
    preseed_b0_projection_and_write_b1_pending(authority_tree)
    projection = fold_trace(authority_tree, CHANGE_ID, phase="reconciled")
    assert projection.schema_version == "2"
    assert projection.authoritative_batch_id == "B1"
    assert projection.integrity == "incomplete"
    assert {gap.code for gap in projection.gaps} == {"project_sync_pending"}
    assert all(row.open_problem_ids == () for row in projection.rows)
```

Cover valid completed, failure identity mismatch with independent problem links, analysis failed, reconcile failed, pending, unavailable, recovery plus independent project-projection gap, source deduplication, and malformed summary preventing publication.

- [ ] **Step 2: Add failing current-loader freshness tests**

```python
def test_current_loader_rejects_projection_after_problem_review(
    completed_tree: Path,
) -> None:
    materialize_reconciled_v2(completed_tree)
    mutate_project_problem_through_valid_review(completed_tree)
    with pytest.raises(CurrentProjectionStaleError):
        load_current_reconciled_projection(completed_tree, CHANGE_ID)
```

Test missing file, invalid UTF-8, invalid JSON/non-mapping/model, V1 artifact, wrong or missing raw phase/change/batch, digest mismatch, and exact current V2 success. Assert each stale case exposes its exact closed `reason`. Add compound payloads with a raw identity error plus an unrelated model defect and prove phase → change → live batch preflight wins before full model validation. Add a secret-only manifest-entry edit: its redacted entry digest remains stable, raw `TraceSource.sha256` changes, live projection digest changes, and the persisted projection is rejected as stale.

- [ ] **Step 3: Add failing verify/materializer/runner tests**

Parameterize every new authority code and assert verify returns `fail` even when policy `on_insufficient=warn`. Assert materializer writes V2 for completed and all three recovery states, replaces preseeded B0, and lets structural fold/model errors escape as task failure with no successful result. For runner ordering, assert a gate/first-fold summary failure publishes neither new quality nor Trace; assert any first- or second-fold summary failure publishes no Trace. A second disk-fold failure may leave the already-published quality artifact and this task does not claim rollback or staged atomic publication.

- [ ] **Step 4: Run fold/current/CLI tests and observe V1/trust failures**

```bash
uv run pytest -q \
  tests/unit/evidence/test_fold_trace_execution.py \
  tests/unit/evidence/test_fold_trace_reconciled.py \
  tests/unit/execution/test_runner_trace.py \
  tests/unit/workflow/graph/handlers/test_trace_projection_operation.py \
  tests/unit/workflow/graph/test_contracts.py \
  tests/integration/test_trace_cli.py \
  tests/integration/test_verify_cli.py
```

Expected: fold still emits V1, old enrichment trusts snapshots, and current loader is missing.

- [ ] **Step 5: Replace reconciled loading with the authority decision and switch constructors**

```python
def _enrich_reconciled(
    project_root: Path,
    change_dir: Path,
    execution: TraceProjectionV2,
) -> TraceProjectionV2:
    decision = evaluate_reconciled_authority(
        project_root,
        change_dir,
        execution.change_id,
        execution.authoritative_batch_id,
    )
    rows = tuple(
        row.model_copy(
            update={
                "failures": decision.failures_by_case.get(row.case_id, ()),
                "open_problem_ids": decision.open_problem_ids_by_case.get(row.case_id, ()),
            }
        )
        for row in execution.rows
    )
    gaps = canonical_gaps(
        (
            *execution.gaps,
            *decision.failure_gaps,
            *decision.issue_gaps,
            *decision.project_gaps,
        )
    )
    projection = TraceProjectionV2(
        change_id=execution.change_id,
        phase="reconciled",
        authoritative_batch_id=execution.authoritative_batch_id,
        sources=merge_sources(execution.sources, decision.sources),
        rows=rows,
        unmapped_tests=execution.unmapped_tests,
        gaps=gaps,
        integrity=derive_trace_integrity(rows, gaps),
    )
    summarize_projection_by_layer(projection)
    validate_trace_phase_pair(execution, projection)
    return projection
```

`merge_sources` uses `TraceSourceRecorder` and `canonical_gaps` uses `_gap_sort_key` with exact duplicate rejection. Delete the old direct failure/snapshot/problem loaders and indexers. Construct execution as V2, summarize it before returning, and only then enrich.

- [ ] **Step 6: Implement point-in-time loading**

```python
def load_current_reconciled_projection(
    project_root: Path,
    change_id: str,
) -> TraceProjectionV2:
    path = resolve_change(project_root, change_id).path / "inspect" / "trace-projection.json"
    if not path.is_file():
        raise CurrentProjectionMissingError(str(path))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CurrentProjectionInvalidError(str(path)) from exc
    if not isinstance(raw, dict):
        raise CurrentProjectionInvalidError(str(path))
    if raw.get("phase") != "reconciled":
        raise CurrentProjectionStaleError("phase_mismatch")
    if raw.get("change_id") != change_id:
        raise CurrentProjectionStaleError("change_id_mismatch")
    live = fold_trace(project_root, change_id, phase="reconciled")
    if raw.get("authoritative_batch_id") != live.authoritative_batch_id:
        raise CurrentProjectionStaleError("batch_id_mismatch")
    try:
        persisted = load_trace_projection_document(raw)
    except ValidationError as exc:
        raise CurrentProjectionInvalidError(str(path)) from exc
    if not isinstance(persisted, TraceProjectionV2):
        raise CurrentProjectionStaleError("legacy_version")
    if projection_digest(persisted) != projection_digest(live):
        raise CurrentProjectionStaleError("digest_mismatch")
    return persisted
```

- [ ] **Step 7: Expand materializer reads and block every new gap**

The materializer contract reads:

```yaml
reads:
  - change:cases/**
  - change:execution/**
  - change:facts/**
  - change:review/**
  - change:healing/**
  - change:codegen/**
  - change:inspect/failure-analysis.json
  - change:inspect/issue-evidence-manifest.json
  - change:inspect/observations.json
  - change:inspect/issue-candidates.json
  - change:inspect/issue-reconcile-status.json
  - change:issues/events.jsonl
  - change:issues/snapshot.json
  - project:qa/issues/events.jsonl
  - project:qa/issues/problems.json
  - repo:tests/**
```

Keep one exact write/authorization. Add all six V2 codes to verify's blocking set. Catch expected fold/model errors in CLI commands and return `EXIT_ERROR` with a stable diagnostic; do not emit a traceback or pass-shaped JSON. Do not catch them inside `materialize_trace_projection`.

- [ ] **Step 8: Run activation, contract, CLI, and static checks**

```bash
uv run pytest -q \
  tests/unit/evidence/test_fold_trace_execution.py \
  tests/unit/evidence/test_fold_trace_reconciled.py \
  tests/unit/evidence/test_issue_replay_authority.py \
  tests/unit/evidence/test_layer_summary.py \
  tests/unit/execution/test_runner_trace.py \
  tests/unit/workflow/graph/handlers/test_trace_projection_operation.py \
  tests/unit/workflow/graph/test_contracts.py \
  tests/integration/test_trace_cli.py \
  tests/integration/test_verify_cli.py
uv run ruff check .
uv run pyright
uv run lint-imports
```

Expected: all new writes are V2, authority gaps block, and stale persisted facts fail closed.

- [ ] **Step 9: Commit Trace V2 activation**

```bash
git add assurance_agent/evidence/trace.py assurance_agent/evidence/current_projection.py assurance_agent/evidence/verify.py assurance_agent/commands/trace_cmd.py assurance_agent/commands/verify_cmd.py assurance_agent/workflow/graph/handlers/trace_projection.py assurance_agent/workflow/execution/runner.py assurance_agent/_resources/schemas/execution-contracts.yaml tests/unit/evidence/test_fold_trace_execution.py tests/unit/evidence/test_fold_trace_reconciled.py tests/unit/execution/test_runner_trace.py tests/unit/workflow/graph/handlers/test_trace_projection_operation.py tests/unit/workflow/graph/test_contracts.py tests/integration/test_trace_cli.py tests/integration/test_verify_cli.py
git diff --cached --check
git commit -m "feat(trace): activate current authority projection v2"
```

---

### Task 13: Route Every Settled Graph Path Through the Materializer

**Files:**
- Modify: `assurance_agent/_resources/schemas/workflow-schema.yaml`
- Modify: `tests/unit/workflow/graph/test_canonical_schema_v2.py`
- Modify: `tests/unit/workflow/graph/test_compiler.py`
- Create: `tests/integration/test_trace_recovery_workflows.py`

**Interfaces:**
- Changes only declarative graph topology and retry-aware `max_supersteps` values; no compiler, runtime, or target-specific dispatch API changes.
- Adds `operation:materialize-trace-projection` to `issue-analyze-workflow` and `issue-reconcile-workflow`.
- Preserves dedicated recovery `via` nodes as edge-free persistence operations.

- [ ] **Step 1: Add failing canonical path and recovery-continuation assertions**

Add an explicit expected matrix:

```python
EXPECTED_TRACE_TERMINALS = {
    "inspect-with-issues": {
        "ordinary": ("reconcile-issues", "materialize-trace-projection", "inspect-complete"),
        "recoveries": {
            "analyze-issues": ("record-analysis-failure", "materialize-trace-projection"),
            "reconcile-issues": ("record-project-sync-pending", "materialize-trace-projection"),
        },
        "successor": "inspect-complete",
    },
    "issue-analyze-workflow": {
        "ordinary": ("reconcile-issues", "materialize-trace-projection", "END"),
        "recoveries": {
            "analyze-issues": ("record-analysis-failure", "materialize-trace-projection"),
            "reconcile-issues": ("record-project-sync-pending", "materialize-trace-projection"),
        },
        "successor": "END",
    },
    "issue-reconcile-workflow": {
        "ordinary": ("reconcile-issues", "materialize-trace-projection", "END"),
        "recoveries": {
            "reconcile-issues": ("record-project-sync-pending", "materialize-trace-projection"),
        },
        "successor": "END",
    },
}
```

For each graph, assert the materializer operation/output, the exact ordinary successor, each `recover.via` and `recover.continue_to` pair, and no ordinary incoming/outgoing edge for `record-analysis-failure` or `record-project-sync-pending`.

- [ ] **Step 2: Add a failing all-terminal-path walk**

Walk the compiled ordinary edges and each recovery continuation as alternate transitions. Starting at every entry transition, enumerate every reachable completion and assert `materialize-trace-projection` occurs exactly once before `inspect-complete` or `END`. Bound traversal by the graph's declared nodes so a mutation that introduces a cycle fails rather than hangs.

Compute the retry-aware bound from each node's retry profile: every planned retry, the recovery `via` task, the materializer, any completion node, and the final planner pass that delivers END all consume the budget boundary. The worst `inspect-with-issues` path records twelve `SuperstepPlannedEvent`s and still needs the terminal planner pass, so its minimum safe limit is `13`; the independent-workflow minima are `9` for `issue-analyze-workflow` and `6` for `issue-reconcile-workflow`. Configured values after this task remain `15`, `9`, and `6` respectively.

- [ ] **Step 3: Add failing topology mutation guards**

For each mutation, compile the modified decoded schema and assert the canonical invariant helper rejects it:

- change either `inspect-with-issues` recovery continuation to `inspect-complete`;
- change either independent workflow recovery continuation to `END`;
- bypass the materializer on an ordinary success edge;
- give a dedicated recovery `via` node an ordinary edge;
- add a second ordinary successor to the materializer.
- lower `inspect-with-issues.max_supersteps` to `12`.
- restore `issue-analyze-workflow.max_supersteps` to `8` or `issue-reconcile-workflow.max_supersteps` to `4`.

Also retain a compiler regression asserting no branch keyed by any of the three target names is needed.

- [ ] **Step 4: Add failing runtime tests for the two exhausted-retry paths**

Use deterministic retryable failures and real `GraphRuntime`:

- issue-analyze: analyzer succeeds on attempt 3, reconcile exhausts all 3 attempts, pending recovery runs, materializer runs, then END is delivered;
- issue-reconcile: reconcile exhausts all 3 attempts, pending recovery runs, materializer runs, then END is delivered.

Assert each invocation completes, the recovery handler and materializer each commit once, and no `max_supersteps exhausted` failure occurs. With the old limits 8/4 these tests must fail after the last meaningful operation but before terminal delivery.

- [ ] **Step 5: Run the topology tests and observe missing routes/budget**

```bash
uv run pytest -q \
  tests/unit/workflow/graph/test_canonical_schema_v2.py \
  tests/unit/workflow/graph/test_compiler.py \
  tests/integration/test_trace_recovery_workflows.py
```

Expected: both independent issue workflows lack the materializer, current recovery continuations bypass it, and old max-superstep values cannot complete the exhausted-retry paths after adding materialization.

- [ ] **Step 6: Update the canonical YAML only**

Apply these exact shapes:

```yaml
# inspect-with-issues
analyze-issues:
  recover:
    via: record-analysis-failure
    continue_to: materialize-trace-projection
reconcile-issues:
  recover:
    via: record-project-sync-pending
    continue_to: materialize-trace-projection

# issue-analyze-workflow
max_supersteps: 9
edges:
  - {from: reconcile-issues, to: materialize-trace-projection}
  - {from: materialize-trace-projection, to: END}

# issue-reconcile-workflow
max_supersteps: 6
edges:
  - {from: reconcile-issues, to: materialize-trace-projection}
  - {from: materialize-trace-projection, to: END}
```

Declare the materializer node in both independent workflows with the same operation, output, and contract as the main graph. Point their recovery continuations to it. Do not add ordinary edges to recovery `via` nodes and do not special-case the targets in the compiler.

Retain `inspect-with-issues.max_supersteps: 15`. Raise only the two proven-insufficient independent-workflow limits from `8` to `9` and from `4` to `6`.

- [ ] **Step 7: Run topology, packaged-schema, runtime, and static checks**

```bash
uv run pytest -q \
  tests/unit/workflow/graph/test_canonical_schema_v2.py \
  tests/unit/workflow/graph/test_compiler.py \
  tests/unit/workflow/graph/test_packaged_schema_compiles.py \
  tests/integration/test_trace_recovery_workflows.py
uv run ruff check .
uv run pyright
```

Expected: all terminal-path and mutation guards pass without a compiler change.

- [ ] **Step 8: Commit declarative topology**

```bash
git add assurance_agent/_resources/schemas/workflow-schema.yaml tests/unit/workflow/graph/test_canonical_schema_v2.py tests/unit/workflow/graph/test_compiler.py tests/integration/test_trace_recovery_workflows.py
git diff --cached --check
git commit -m "feat(graph): materialize trace on settled issue paths"
```

---

### Task 14: Prove Recovery, Healing, and Pinned Historical Resume Boundaries End to End

**Files:**
- Modify: `tests/integration/test_trace_recovery_workflows.py`
- Modify: `tests/integration/test_issue_lifecycle_workflow.py`
- Modify: `tests/integration/test_graph_runtime.py`
- Modify: `tests/integration/test_graph_runtime_faults.py`
- Modify: `tests/integration/_graph_fault_worker.py`

**Interfaces:**
- Exercises existing `GraphRuntime`, workspace journal, recovery barrier, and definition pinning as black-box contracts.
- Requires no production runtime change: the prerequisite runtime already resolves an exact execution bundle before definition-dependent recovery and planning.
- Treats each recovery barrier as invocation-local: resume the exact invocation that owns a pending write-set seam. A parent's barrier never consumes child-owned work, although a compatible parent may later reach `run_child`, which explicitly drives the child under its own barrier.
- Resolves graph, execution-contract, and ingest-catalog identities from pinned snapshots while requiring live-compatible gate/profile and model-schema epochs. Operation handler code is not pinned; an old handler may create the pre-upgrade frozen write set, but no handler is reinvoked while replay repairs that successful task.

- [ ] **Step 1: Build a reusable B0-to-B1 terminal-path harness**

The fixture creates valid B0 execution/reconciled projections, advances the manifest and issue inputs to B1, runs one terminal path, then calls `load_current_reconciled_projection(project_root, change_id)` against live authority. Main-graph cases must enter through the real `assurance` parent and its nested `uses: graph:inspect-with-issues` child; capture the child invocation ID for seam-specific assertions. Parameterize these seven paths:

```python
TRACE_TERMINAL_CASES = (
    ("assurance", "inspect-with-issues", "analyzer_recovery"),
    ("assurance", "inspect-with-issues", "sync_recovery"),
    ("issue-analyze-workflow", None, "success"),
    ("issue-analyze-workflow", None, "analyzer_recovery"),
    ("issue-analyze-workflow", None, "sync_recovery"),
    ("issue-reconcile-workflow", None, "success"),
    ("issue-reconcile-workflow", None, "sync_recovery"),
)
```

Every case asserts current batch B1, one materializer success event in the owning invocation, a model-valid current projection, and no B0 problem link. Recovery cases additionally assert `integrity == "incomplete"` and the exact blocking gap (`issue_analysis_failed` or `project_sync_pending`). A generic document load is insufficient evidence here: the test must fail if current-authority digest or batch checks fail.

- [ ] **Step 2: Add main-graph happy and semantic-failure cases**

Cover normal analyzer/reconcile success and semantic reconcile failure through the same `assurance` parent boundary. The latter operation task succeeds, the graph completes, and the projection remains incomplete with `issue_reconcile_failed`. Prove timeout, transport, rate-limit, invalid-output analyzer recovery and conflict/transport reconcile recovery map to the same typed projections without duplicating materializer effects. End every case with `load_current_reconciled_projection`, not `load_trace_projection_document`.

- [ ] **Step 3: Add materializer failure and repeatable healing tests**

Inject a fold/model failure at the materializer for each graph and assert no completion marker/terminal success is recorded and no newer projection is published. Then cover this repeatability/healing matrix:

- healing rerun at B1 after happy analysis/reconcile, after analysis recovery, and after sync recovery;
- prior `analysis_failed` followed by `issue-analyze-workflow` success, which clears the failure gap;
- prior `analysis_failed` followed by repeated analyzer failure, which remains current and incomplete;
- prior `project_sync_pending` followed by `issue-reconcile-workflow` success, which clears the pending gap;
- prior `project_sync_pending` followed by repeated reconcile conflict, which remains current and pending;
- rerun with unchanged authoritative source bytes, which produces byte-identical canonical projection bytes and identical execution/reconciled layer-summary model dumps.

Every repaired B1 projection must replace B0, contain B1 summaries and bindings, remove all B0-only problem links, and pass the current-authority loader.

- [ ] **Step 4: Add synchronized and ordinary publication-recovery tests**

Use the fault worker to stop at all four existing ledger boundaries in the invocation that owns the affected node:

1. synchronized reconcile has `superstep_committed`, but synchronized apply is pending;
2. synchronized reconcile apply is complete, but acknowledgement is pending;
3. ordinary materializer has `task_attempt_succeeded`, but its superstep is uncommitted, exercising `_commit_pending_write_sets`;
4. ordinary materializer has `superstep_committed`, but canonical apply is pending, exercising `_repair_ordinary_materialization`.

Seed a unique project-authority byte change for each synchronized case so the later fold proves it observed the repaired apply rather than pre-fault state. Resume the exact owner invocation and assert the recovery barrier finishes synchronized apply/ack before any materializer planning event; for ordinary cases, assert the ledger-proven frozen write set is committed/applied exactly once and the handler is not reinvoked.

- [ ] **Step 5: Add pinned historical execution and incompatible-epoch fixtures**

Start with committed old graph/contract/catalog snapshots and use the pre-upgrade handler to create a frozen write set at each repairable boundary. Then expose new packaged definitions and cover three independently observable classes:

1. graph identity drift using a pre-Task13 pinned independent `issue-analyze-workflow` or `issue-reconcile-workflow`, with all live semantic/model epochs compatible;
2. contract-only drift with the graph and ingest-catalog digests unchanged;
3. ingest-catalog-only drift with graph and contract digests unchanged.

For each class, resume the exact invocation owning the seam and assert `_definition_resolver` loads a bundle whose graph, contract, and catalog digests exactly equal the invocation's full `PinnedDefinitionRequest`; current packaged identities are not substituted. The owner-local barrier then converges idempotently and the pinned topology continues. Assert one handler execution and byte-identical frozen output; ordinary frozen commit/apply occurs once, while synchronized apply may be safely replayed when acknowledgement was missing. Recovery never upgrades V1 bytes to V2.

Separate topology assertions from handler-version assertions. The chosen pre-Task13 pinned independent issue graph has no materializer node, so resume neither injects one nor backfills reconciled V2; its V1/missing result remains legacy/incomplete. Do not make that claim about `inspect-with-issues`, whose ordinary path already contained a materializer before Task 13. A contract-only or catalog-only drift fixture may retain a post-Task13 graph whose not-yet-executed materializer is legitimately planned later; because Python handlers are not pinned, that real node may emit V2 through current compatible code. In that case assert only that the node existed in the pinned graph and executed under pinned contracts/catalog—never that identity drift suppresses its legitimate output.

Add separate fail-closed fixtures for (a) gate-semantics digest mismatch, (b) assurance-profile digest mismatch, and (c) pinned ingest model-schema mismatch. After any legal definition-independent manual-revision prefix repair, exact bundle resolution must raise `GraphDefinitionChanged` before pending write-set commit, synchronized apply/ack, ordinary materialization repair, handler resolution, or planning. Assert the pending seam and artifact bytes remain unchanged. Do not use graph/contract drift to test this boundary; those identities are intentionally recoverable from pinned snapshots.

Add nested invocation cases around the real `assurance -> graph:inspect-with-issues` boundary. Direct child resume repairs a child-owned seam. A parent with an incompatible semantic/model epoch fails before it can drive the child, leaving the child seam unchanged. A compatible parent may later reach `run_child`, but any child recovery events must be owned by the child invocation and use its inherited pinned request; never attribute them to a recursive parent barrier.

- [ ] **Step 6: Run the new tests and observe missing materialization assertions**

```bash
uv run pytest -q \
  tests/integration/test_trace_recovery_workflows.py \
  tests/integration/test_issue_lifecycle_workflow.py \
  tests/integration/test_graph_runtime.py \
  tests/integration/test_graph_runtime_faults.py
```

Task 14 is test-only verification after Tasks 12–13. Expected now: all current-definition terminal and healing cases publish current B1 evidence; all four ledger seams converge idempotently with one handler execution and ordinary frozen commit/apply once; compatible graph/contract/catalog drift continues through exact pinned bundles; incompatible semantic/profile/model epochs stop before definition-dependent recovery. If these assertions fail, fix the responsible earlier task or prerequisite regression rather than adding runtime target dispatch here.

- [ ] **Step 7: Complete only fixture/harness code and verify runtime remains generic**

Wire the integration fixtures to deterministic handlers, existing fault labels, full `PinnedDefinitionRequest` helpers, and committed definition snapshots. Assert the ordering through ledger events and resolver spies rather than private-call assumptions. Do not add a target-specific runtime exception or a graph-digest-only compatibility resolver.

- [ ] **Step 8: Run graph integration and static checks**

```bash
uv run pytest -q \
  tests/integration/test_trace_recovery_workflows.py \
  tests/integration/test_issue_lifecycle_acceptance.py \
  tests/integration/test_issue_lifecycle_workflow.py \
  tests/integration/test_graph_runtime.py \
  tests/integration/test_graph_runtime_faults.py \
  tests/unit/workflow/graph/test_resume_v3.py
uv run ruff check .
uv run pyright
```

Expected: all current paths materialize B1, publication recovery converges idempotently without duplicate handler effects, pinned identity drift resumes exact old topology without injected nodes or frozen-byte upgrades, legitimate pending pinned nodes may still run, and incompatible executable epochs remain fail-closed.

- [ ] **Step 9: Commit integration coverage**

```bash
git add tests/integration/test_trace_recovery_workflows.py tests/integration/test_issue_lifecycle_workflow.py tests/integration/test_graph_runtime.py tests/integration/test_graph_runtime_faults.py tests/integration/_graph_fault_worker.py
git diff --cached --check
git commit -m "test(trace): cover recovery healing and pinned resume"
```

---

### Task 15: Define the Closed Specialty Report V3 Contract

**Files:**
- Modify: `assurance_agent/eval/specialty_models.py`
- Modify: `tests/unit/eval/test_specialty_models.py`

**Interfaces:**
- Adds: `TraceCollectionFailureReason`, `TraceCommandStatus`, `ProjectionOverview`, `TracePhaseEvidence`, `CoverageSummary`, `VerifyDiagnostics`, `CompleteTraceabilityEvidenceV3`, `IncompleteTraceabilityEvidenceV3`, and `SpecialtyReportV3`.
- Reuses: artifacts-owned `TraceLayerFactSummary`, `TraceLayerSufficiencySummary`, and the final `CapabilityPolicyReplayV2` from the Fuzz/Performance prerequisite.
- Adds dormant `load_specialty_report_document(raw: object) -> SpecialtyReportVariant` for concrete V1/V2/V3 dispatch; unknown, null, and malformed versions fail closed.
- Preserves through Task 17: public `load_specialty_report(payload)` and `SpecialtyReport` remain V1/V2-only so existing renderer/callers stay Pyright-clean. V1/V2 model fields and permissive legacy trace payloads remain byte-for-byte; Task 18 widens the public boundary atomically with renderer dispatch.

- [ ] **Step 1: Add a canonical complete v3 round-trip fixture**

Build execution/reconciled summaries from real projection fixtures and sufficiency through `join_layer_sufficiency`. Assert the parsed report is frozen, every nested model forbids extras, and JSON round-trip preserves schema `"3"`. The exact outer shape is:

```python
class SpecialtyReportV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["3"] = "3"
    change_id: str
    capability_contract_policy: CapabilityPolicyReplayV2
    traceability_evidence: TraceabilityEvidenceV3
```

Use a discriminated `status` union for complete/incomplete trace evidence. Declare every field and strict count type exactly as spec section 9; do not keep `dict[str, Any]` anywhere inside v3 trace evidence.

- [ ] **Step 2: Add failing complete-payload invariant mutations**

Starting from the canonical fixture, mutate one field at a time and require `ValidationError`:

- unknown sufficiency reason or execution state;
- naive `as_of`, zero `recency_hours`, boolean/float count;
- wrong layer count/order/case type;
- outer change ID or execution/reconciled authoritative batch mismatch;
- overview phase/batch/digest/integrity mismatch;
- overview row/gap count mismatch against fact aggregates;
- fact layer total or gap total/breakdown mismatch;
- sufficiency total arithmetic or execution-state/reason arithmetic mismatch;
- a sufficiency layer that remains internally valid but whose `sufficient + insufficient` total differs from the corresponding execution fact layer total;
- sufficiency digest not bound to execution;
- verify digest not bound to reconciled;
- sufficiency or verify policy digest not bound to replay baseline;
- missing replay `definition_binding`;
- wrong semantics, `require_current_batch`, or verify phase;
- extra complete field.

The v3 loader relies on the artifacts-owned `TraceLayerFacts`, `TraceGapAggregate`, `TraceLayerFactSummary`, and `TraceLayerSufficiencySummary` model validators for self-contained arithmetic. Its outer validator adds cross-object checks: overview row/gap reconciliation, identities, digests, policy binding, exact execution fact/sufficiency layer identity, and `sufficient + insufficient == execution facts total` for each zipped layer. `source_count` and `unmapped_test_count` are strict nonnegative overview diagnostics derived by the collector, but the serialized facts summary has no independent values from which a loader could prove them; do not claim that changing one valid integer is detectable from an opaque digest.

- [ ] **Step 3: Add failing incomplete-union and loader tests**

Parameterize every literal in the closed `TraceCollectionFailureReason` set and prove it round-trips through the new document loader. Assert unknown reasons, non-string details, and complete-only keys on an incomplete object are rejected; the documented empty-string detail default remains valid. Assert V1 and V2 fixtures still load to their original concrete types and preserve their legacy payload; missing/null/`"4"` versions fail. Separately assert the existing public V1/V2 loader signature and behavior remain unchanged through this task.

```python
def test_incomplete_v3_rejects_partial_complete_matrix() -> None:
    payload = incomplete_v3("reconciled_projection_stale")
    payload["traceability_evidence"]["execution"] = canonical_execution_phase()
    with pytest.raises(ValidationError, match="extra"):
        load_specialty_report_document(payload)
```

- [ ] **Step 4: Run the model tests and observe absent v3 symbols**

```bash
uv run pytest -q tests/unit/eval/test_specialty_models.py
```

Expected: imports fail because the v3 union and invariants are not implemented.

- [ ] **Step 5: Implement exact closed DTOs and validators**

Define the reason literals exactly:

```python
TraceCollectionFailureReason = Literal[
    "execution_projection_missing",
    "execution_projection_invalid",
    "reconciled_projection_missing",
    "reconciled_projection_invalid",
    "reconciled_projection_stale",
    "projection_identity_mismatch",
    "projection_phase_pair_mismatch",
    "quality_gate_missing",
    "quality_gate_invalid",
    "quality_gate_binding_mismatch",
    "sufficiency_binding_mismatch",
    "verify_result_missing",
    "verify_result_invalid",
    "verify_binding_mismatch",
    "layer_summary_invalid",
]
```

Implement `ProjectionOverview.from_projection` and `TracePhaseEvidence.from_projection` constructors so counts/digests are derived, not caller supplied. Complete validation enforces outer change ID equality with both fact summaries, equal execution/reconciled batch, replay binding, exact four-layer order, overview-to-summary reconciliation, gap breakdown arithmetic, and bound sufficiency: each sufficiency layer must match the corresponding execution fact layer/case type and its sufficient+insufficient total must equal that fact layer's total. It also enforces verify binding. Incomplete validation accepts only `reason_code`, stable `detail`, and optional command status.

- [ ] **Step 6: Replace manual version branching with an explicit document union**

```python
SpecialtyReportVariant = Annotated[
    LegacySpecialtyReportV1 | SpecialtyReportV2 | SpecialtyReportV3,
    Field(discriminator="schema_version"),
]


class SpecialtyReportDocument(RootModel[SpecialtyReportVariant]):
    """Discriminated specialty-report wire document."""


def load_specialty_report_document(raw: object) -> SpecialtyReportVariant:
    return SpecialtyReportDocument.model_validate(raw).root
```

Keep V1/V2 classes untouched beyond participating in the new union. Export the concrete variants, v3 nested DTOs, and new document loader explicitly. Do not widen or replace the existing public `SpecialtyReport` alias/`load_specialty_report` function yet.

- [ ] **Step 7: Run model, replay-prerequisite, and static checks**

```bash
uv run pytest -q \
  tests/unit/eval/test_specialty_models.py \
  tests/unit/workflow/graph/test_four_layer_replay.py
uv run ruff check .
uv run pyright
```

Expected: v3 is closed and self-validating; v1/v2 compatibility stays green.

- [ ] **Step 8: Commit the specialty wire contract**

```bash
git add assurance_agent/eval/specialty_models.py tests/unit/eval/test_specialty_models.py
git diff --cached --check
git commit -m "feat(eval): define specialty trace report v3"
```

---

### Task 16: Build the Complete V3 Collection Path Alongside the Live V2 CLI

**Files:**
- Modify: `benchmark/vue-fastapi-admin/benchmark/benchmark_specialty_report.py`
- Modify: `tests/unit/benchmark/test_specialty_report.py`

**Interfaces:**
- Adds: frozen internal `TraceCollectionInputs`, `TraceCollectionFailure(reason_code, detail)`, and `_collect_complete_traceability(inputs, capability) -> CompleteTraceabilityEvidenceV3`; `schema_root` is already absent after the pinned-topology replay prerequisite.
- Consumes: versioned artifact loaders, `load_current_reconciled_projection`, `validate_trace_phase_pair`, `summarize_projection_by_layer`, `join_layer_sufficiency`, and the replay's pinned baseline policy digest.
- Defers activation: the public `collect_report`/CLI, benchmark-local V2 aggregation, renderer, evidence row, and shell caller remain unchanged and green until Task 18 atomically switches the whole public surface.
- Preserves: caller-supplied nonnegative trace/verify command exits as diagnostics, not as proof of artifact validity.

- [ ] **Step 1: Replace the happy fixture with a fully authority-valid item**

The fixture must include:

- execution Trace V2 at the CLI output path;
- a byte-current reconciled Trace V2 at `change:inspect/trace-projection.json`;
- a valid enrichment-only phase pair for one case in each of API/E2E/Fuzz/Performance;
- QualityGateResult V2 success with bound SufficiencyReportV2;
- VerifyResult bound to the reconciled projection and pinned policy;
- completed capability replay with `definition_binding.baseline_policy_digest`.

Do not patch a digest after model construction. Generate each artifact through its production constructor/helper so the test crosses the same seam as a real run.

- [ ] **Step 2: Add failing complete collection assertions**

```python
inputs = TraceCollectionInputs(
    project_root=project,
    change_id=CHANGE_ID,
    root_invocation_id=ROOT_INVOCATION_ID,
    workflow_entrypoint="assurance",
    trace_path=execution_trace_path,
    verify_path=verify_path,
    trace_exit=0,
    verify_exit=0,
)
capability = collect_capability_policy_replay(
    change_dir=inputs.change_dir,
    change_id=inputs.change_id,
    root_invocation_id=inputs.root_invocation_id,
    expected_entrypoint=inputs.workflow_entrypoint,
)
trace = _collect_complete_traceability(inputs, capability)
assert trace.status == "complete"
assert [row.layer for row in trace.execution.facts.layers] == [
    "api", "e2e", "fuzz", "performance"
]
assert trace.execution.overview.projection_digest == trace.sufficiency.source_projection_digest
assert trace.reconciled.overview.projection_digest == trace.verify.projection_digest
binding = capability.definition_binding
assert binding is not None
assert (
    trace.sufficiency.source_policy_digest
    == binding.baseline_policy_digest
    == trace.verify.policy_digest
)
```

Wrap the returned trace section with the same capability fixture in a `SpecialtyReportV3` and assert it round-trips through dormant `load_specialty_report_document`. Assert zero-row layers remain present, reconciled business recovery can still be collection `complete` with projection `integrity="incomplete"`, and global gaps occur only in `facts.global_gaps`. Re-evaluate sufficiency with a different policy or `as_of` and prove only the sufficiency view changes; both phase fact summaries stay byte-identical.

In the same task, retain a public compatibility assertion that the existing `collect_report(...)` call and CLI still produce `SpecialtyReportV2`; the new internal path must not be reachable from `main` before Task 18.

- [ ] **Step 3: Add failing seam-spy tests**

Monkeypatch the shared current loader, phase-pair validator, fact summarizer, sufficiency join, and quality document loader one at a time. Assert each is called and its returned typed value is used. Inspect only the new `_collect_complete_traceability` function's AST/source and forbid local aggregation or direct legacy model parsing there. Do not impose a whole-file ban yet: the still-live V2 collector retains its old helpers until Task 18 removes them atomically with public activation.

- [ ] **Step 4: Run the complete collector tests and observe legacy payload output**

```bash
uv run pytest -q tests/unit/benchmark/test_specialty_report.py -k 'complete or shared or zero_layer or recovery'
```

Expected: the live public collector still returns V2 with untyped global summaries and the new internal complete-v3 seam is absent; the prerequisite has already removed caller-selected schema roots.

- [ ] **Step 5: Implement the ordered complete collection pipeline**

Collect in this order:

1. capability replay from pinned invocation topology/policy;
2. execution projection through `load_trace_projection_document`, requiring concrete V2, requested change, `phase="execution"`;
3. current reconciled projection through `load_current_reconciled_projection`;
4. exact change/batch identity and `validate_trace_phase_pair`;
5. execution/reconciled `TracePhaseEvidence.from_projection`, so summary failures take precedence over later quality/verify failures;
6. QualityGateResult through `load_quality_gate_result_document`, requiring V2 success;
7. `join_layer_sufficiency(execution, execution_facts, quality.dimensions.coverage.evidence.report, expected_policy_digest=baseline_policy_digest)`;
8. VerifyResult and its change/phase/projection/policy bindings; `verdict="pass"` requires a non-null scope. When scope is present, its cases must equal the reconciled projection's canonical case-ID tuple and its batch, policy digest, and projection digest must each equal the corresponding top-level/current value. `scope=None` remains valid only for fail/needs-human results, including typed recovery;
9. `SpecialtyReportV3` construction.

```python
class TraceCollectionFailure(Exception):
    def __init__(
        self,
        reason_code: TraceCollectionFailureReason,
        detail: str,
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class TraceCollectionInputs:
    project_root: Path
    change_id: str
    root_invocation_id: str
    workflow_entrypoint: str
    trace_path: Path
    verify_path: Path
    trace_exit: int
    verify_exit: int
    change_dir: Path = field(init=False)
    command_status: TraceCommandStatus = field(init=False)

    def __post_init__(self) -> None:
        for name, value in (
            ("trace_exit", self.trace_exit),
            ("verify_exit", self.verify_exit),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if not self.workflow_entrypoint:
            raise ValueError("workflow entrypoint is required")
        change_dir = resolve_change(self.project_root, self.change_id).path
        object.__setattr__(self, "change_dir", change_dir)
        object.__setattr__(
            self,
            "command_status",
            TraceCommandStatus(trace_exit=self.trace_exit, verify_exit=self.verify_exit),
        )


def _collect_complete_traceability(
    inputs: TraceCollectionInputs,
    capability: CapabilityPolicyReplayV2,
) -> CompleteTraceabilityEvidenceV3:
    binding = capability.definition_binding
    if binding is None:
        raise TraceCollectionFailure(
            "sufficiency_binding_mismatch",
            "capability_definition_binding_missing",
        )
    execution = _load_execution_projection_v2(inputs)
    reconciled = load_current_reconciled_projection(
        inputs.project_root,
        inputs.change_id,
    )
    _validate_projection_identity(inputs.change_id, execution, reconciled)
    validate_trace_phase_pair(execution, reconciled)
    execution_evidence = TracePhaseEvidence.from_projection(execution)
    reconciled_evidence = TracePhaseEvidence.from_projection(reconciled)
    quality = _load_bound_quality_v2(inputs, execution, binding.baseline_policy_digest)
    coverage_evidence = quality.dimensions.coverage.evidence
    if not isinstance(coverage_evidence, EvidenceCoverageSuccessV2):
        raise TraceCollectionFailure(
            "quality_gate_binding_mismatch",
            "typed_sufficiency_unavailable",
        )
    sufficiency = join_layer_sufficiency(
        execution,
        execution_evidence.facts,
        coverage_evidence.report,
        expected_policy_digest=binding.baseline_policy_digest,
    )
    verify = _load_bound_verify(inputs, reconciled, binding.baseline_policy_digest)
    coverage_summary = CoverageSummary(
        status=quality.dimensions.coverage.status,
        line=quality.dimensions.coverage.line_coverage,
        branch=quality.dimensions.coverage.branch_coverage,
        final_status=quality.final_status,
    )
    verify_diagnostics = VerifyDiagnostics(
        phase=verify.phase,
        verdict=verify.verdict,
        policy_digest=verify.policy_digest,
        projection_digest=verify.projection_digest,
        blocking_gap_count=len(verify.blocking_gaps),
        open_problem_count=len(verify.open_problem_ids),
        reported_insufficient_count=len(verify.insufficient),
    )
    return CompleteTraceabilityEvidenceV3(
        status="complete",
        command_status=inputs.command_status,
        execution=execution_evidence,
        reconciled=reconciled_evidence,
        sufficiency=sufficiency,
        coverage=coverage_summary,
        verify=verify_diagnostics,
    )
```

`TraceCollectionInputs` resolves the change through the repository's configured change-location authority instead of hard-coding `project_root/qa/changes`; unsafe or missing change IDs fail before any trace path is consumed. An empty root invocation ID remains representable so capability replay can emit its typed `root_invocation_unbound` incomplete diagnostic; the separate publication attempt ID is always non-empty. It does not expose a schema root.

- [ ] **Step 6: Run complete collection, replay, and static checks**

```bash
uv run pytest -q \
  tests/unit/benchmark/test_specialty_report.py -k 'complete or shared or zero_layer or recovery' \
  tests/unit/eval/test_specialty_models.py \
  tests/unit/workflow/graph/test_four_layer_replay.py
uv run ruff check .
uv run pyright
uv run pyright benchmark/vue-fastapi-admin/benchmark/benchmark_specialty_report.py
```

Expected: the internal complete-v3 path is built exclusively from typed shared evidence and pinned replay, while all existing public V2 CLI/caller tests remain unchanged and green.

- [ ] **Step 7: Commit the complete collector path**

```bash
git add benchmark/vue-fastapi-admin/benchmark/benchmark_specialty_report.py tests/unit/benchmark/test_specialty_report.py
git diff --cached --check
git commit -m "feat(benchmark): build bound trace evidence v3 path"
```

---

### Task 17: Translate Every Expected Collection Failure into Typed Incomplete V3

**Files:**
- Modify: `benchmark/vue-fastapi-admin/benchmark/benchmark_specialty_report.py`
- Modify: `tests/unit/benchmark/test_specialty_report.py`

**Interfaces:**
- Uses `TraceCollectionFailure(reason_code, detail)` from Task 16 as the only expected trace-collection exception and completes its closed translation matrix.
- Adds internal `collect_v3_report(inputs) -> SpecialtyReportV3`, which always returns a model-valid V3 report for modeled collection failures after capability replay is available.
- Defers activation: public `collect_report`, CLI output, renderer/evidence-row, and shell caller remain V2-compatible and unchanged through this commit. Task 18 switches all of them together with receipt publication.
- Preserves: unexpected exceptions escape the internal builder; no broad catch turns programmer errors into diagnostic wire data.

- [ ] **Step 1: Parameterize the complete reason-code matrix**

Create at least one black-box collector mutation for every reason:

| Reason | Fixture mutation |
|---|---|
| `execution_projection_missing` | remove requested execution trace |
| `execution_projection_invalid` | invalid UTF-8, malformed JSON/non-mapping, V1, or invalid V2 after raw identity passes |
| `reconciled_projection_missing` | remove current persisted projection |
| `reconciled_projection_invalid` | invalid UTF-8 or malformed JSON/model |
| `reconciled_projection_stale` | V1 persisted artifact or mutate a live authority source after materialization |
| `projection_identity_mismatch` | execution or current reconciled raw change/phase/batch is missing or wrong; includes the corresponding current-loader stale reasons |
| `projection_phase_pair_mismatch` | mutate a shared row/source/gap invariant |
| `quality_gate_missing` | remove quality artifact |
| `quality_gate_invalid` | invalid UTF-8, malformed JSON, or non-binding model error |
| `quality_gate_binding_mismatch` | valid V1, valid V2 error payload, or wrong quality change/batch |
| `sufficiency_binding_mismatch` | nested report projection/policy/semantics/mode/case bijection (including duplicate verdict IDs) or missing replay binding |
| `verify_result_missing` | remove verify artifact |
| `verify_result_invalid` | invalid UTF-8, malformed JSON, or non-identity model error |
| `verify_binding_mismatch` | raw change/phase mismatch; pass with null scope; present scope case-set/order/batch/policy/projection mismatch; or wrong top-level projection/policy digest |
| `layer_summary_invalid` | shared summary validator rejects an otherwise parsed projection |

Each case asserts a `SpecialtyReportV3` with `status="incomplete"`, the exact closed reason, no complete matrix keys, and no exception text in `detail`. Include a quality success payload whose only defect is a duplicate sufficiency verdict `case_id` and pin it to `sufficiency_binding_mismatch`. Add compound mutations to prove the ordered mapping below, rather than relying on whichever Pydantic validator happens to run first.

- [ ] **Step 2: Add direct incomplete-builder and public-compatibility tests**

For every matrix row, call `collect_v3_report`, validate the result through dormant `load_specialty_report_document`, and assert the exact incomplete arm. Add these distinct cases:

- capability replay incomplete with trace complete: the V3 object preserves both states;
- trace incomplete with capability replay complete: the exact closed reason survives;
- both incomplete: both diagnostics survive in one valid V3 object;
- unexpected `RuntimeError`: the internal builder raises and constructs no replacement object.

Retain a black-box regression that the public `collect_report`/CLI still emits the existing V2 wire and current argument/exit behavior during Tasks 16–17. This is the atomic-activation guard, not a permanent compatibility promise.

- [ ] **Step 3: Run the failure matrix and observe uncaught exceptions**

```bash
uv run pytest -q tests/unit/benchmark/test_specialty_report.py -k 'incomplete or missing or invalid or mismatch or stale or atomic'
```

Expected: the new internal incomplete builder is absent or aborts before producing a model-valid diagnostic object; the public V2 collector remains unchanged.

- [ ] **Step 4: Implement stable exception translation at narrow seams**

```python
def collect_v3_report(inputs: TraceCollectionInputs) -> SpecialtyReportV3:
    capability = collect_capability_policy_replay(
        change_dir=inputs.change_dir,
        change_id=inputs.change_id,
        root_invocation_id=inputs.root_invocation_id,
        expected_entrypoint=inputs.workflow_entrypoint,
    )
    try:
        traceability = _collect_complete_traceability(inputs, capability)
    except TraceCollectionFailure as exc:
        traceability = IncompleteTraceabilityEvidenceV3(
            status="incomplete",
            reason_code=exc.reason_code,
            detail=exc.detail,
            command_status=inputs.command_status,
        )
    return SpecialtyReportV3(
        change_id=inputs.change_id,
        capability_contract_policy=capability,
        traceability_evidence=traceability,
    )
```

Use this disjoint order at each file seam:

1. missing path → the corresponding `*_missing`;
2. invalid UTF-8 / invalid JSON / non-mapping → the corresponding `*_invalid`;
3. raw identity preflight before a Literal model can erase classification;
4. full shared document/model loader;
5. concrete-version/union-arm binding;
6. cross-artifact typed validation.

For quality, require raw V2 change/batch identity first. If the success arm contains string/bool binding scalars that are well-typed but differ (`source_projection_digest`, `source_policy_digest`, `semantics`, `require_current_batch`), raise `sufficiency_binding_mismatch` before the full Literal model; missing/wrongly-typed nested fields remain `quality_gate_invalid`. Before full model validation, if `verdicts` is a structurally valid list of mappings with well-typed non-empty string `case_id`s, detect duplicate IDs and raise `sufficiency_binding_mismatch`; malformed verdict shape/ID remains `quality_gate_invalid`. This raw uniqueness preflight is required because `SufficiencyReportV2` rejects duplicates before the typed join. A valid V1 or valid V2 error arm is `quality_gate_binding_mismatch`. After a valid success model, `SufficiencyBindingError` from missing/extra/exact case join or other binding failures maps only to `sufficiency_binding_mismatch`.

For verify, preflight required raw `change_id` and `phase` before `VerifyResult.model_validate` so wrong/missing phase maps to `verify_binding_mismatch` rather than parse-order-dependent invalid. Other model failures map to `verify_result_invalid`. After parsing, top-level projection/policy digest errors map to `verify_binding_mismatch`; `verdict="pass"` with `scope=None` is also a binding mismatch. When scope is present, require `scope.cases == tuple(row.case_id for row in reconciled.rows)` with no omission/extra/duplicate/reordering, and require `scope.batch`, `scope.policy_digest`, and `scope.projection_digest` to equal the authoritative batch and the same bound top-level digests. Scope absence remains valid only for typed fail/needs-human results.

For execution projection, after UTF-8/JSON/mapping checks but before the document loader, require raw `change_id == inputs.change_id`, `phase == "execution"`, and a non-empty string `authoritative_batch_id`; missing/wrong values map to `projection_identity_mismatch`, even when another model defect is present. Then require concrete V2; a well-identified V1 or other model defect maps to `execution_projection_invalid`.

For current projection, the shared loader performs the same raw precedence and compares raw batch against the live fold before full model validation. Map `CurrentProjectionStaleError.reason` values `phase_mismatch`, `change_id_mismatch`, and `batch_id_mismatch` to `projection_identity_mismatch`; map `legacy_version` and `digest_mismatch` to `reconciled_projection_stale`. `TracePhasePairError` maps only to phase-pair mismatch and `TraceLayerSummaryError` only to layer-summary invalid. Catch no broad `ValueError` and never copy an exception message into the wire artifact.

- [ ] **Step 5: Keep public V3 activation deferred**

Do not call `collect_v3_report` from `main`, replace the existing public V2 `collect_report`, or delete the legacy V2 aggregation yet. Add a source/behavior guard that Tasks 16–17 introduce only the typed internal path. Task 18 will atomically switch the public collector, CLI output/exit, renderer, row parser, receipt protocol, and shell registration in one commit.

- [ ] **Step 6: Run the full collector and static checks**

```bash
uv run pytest -q tests/unit/benchmark/test_specialty_report.py
uv run ruff check .
uv run pyright
uv run pyright benchmark/vue-fastapi-admin/benchmark/benchmark_specialty_report.py
```

Expected: every closed failure produces an internal typed diagnostic object, programming errors produce none, and the live V2 CLI/caller remains green until Task 18.

- [ ] **Step 7: Commit incomplete publication semantics**

```bash
git add benchmark/vue-fastapi-admin/benchmark/benchmark_specialty_report.py tests/unit/benchmark/test_specialty_report.py
git diff --cached --check
git commit -m "feat(benchmark): translate incomplete trace evidence v3"
```

---

### Task 18: Render Four-Layer Evidence and Make Cursor Collection-Aware

**Files:**
- Modify: `assurance_agent/eval/specialty_models.py`
- Modify: `assurance_agent/eval/specialty_render.py`
- Modify: `benchmark/vue-fastapi-admin/benchmark/benchmark_specialty_report.py`
- Modify: `benchmark/vue-fastapi-admin/benchmark/cursor-loop-helpers.sh`
- Modify: `benchmark/vue-fastapi-admin/benchmark/run-workflow-loop-cursor.sh`
- Modify: `tests/unit/eval/test_specialty_models.py`
- Modify: `tests/unit/benchmark/test_specialty_report.py`
- Modify: `tests/unit/benchmark/test_cursor_loop_helpers.py`

**Interfaces:**
- Atomically activates public `SpecialtyReport = SpecialtyReportVariant` and `load_specialty_report(raw: object) -> SpecialtyReportVariant` by delegating to Task 15's document loader, plus `collect_report(inputs) -> SpecialtyReportV3` by delegating to Task 17's internal builder; widens renderer/caller dispatch and deletes benchmark-local V2 aggregation/direct legacy parses only in this commit.
- Adds an internal discriminated `SpecialtyPublicationReceiptDocument`: pending carries schema/state/non-empty attempt/change; committed additionally carries exact canonical report SHA-256, trace status, and capability integrity.
- Changes CLI `collect`: requires `--attempt-id` and a stable sibling `--publication-receipt`, atomically writes pending receipt → report → committed receipt, then returns the V3 overall outcome. The files are independently atomic, not one transaction.
- Renders v3 complete as two exact four-layer matrices plus global gaps, coverage, and verify diagnostics.
- Renders v3 incomplete as one stable diagnostic and no layer rows.
- Renders existing v1/v2 global trace data unchanged and labels the layer section `legacy_unlayered` without synthetic rows.
- Changes `evidence_row(report: SpecialtyReportVariant, *, expected_change_id: str) -> str` to dispatch on validated concrete models; the CLI does not dump back to an untyped dictionary first.
- Adds `report_collection_exit(report: SpecialtyReportVariant) -> Literal[0, 1]`: V3 returns 0 only when capability replay and trace collection are complete; V2 returns 0 only when capability replay is complete; V1 legacy remains readable and returns 0.
- Adds `validate-publication` CLI validation for receipt state plus attempt/change/report digest/trace-status/capability-integrity. Fresh V3 registration validates the expected attempt ID; V3 resume/reuse validates the durable sibling receipt and its recorded attempt ID. Legacy V1/V2 are receiptless-readable only when the sibling receipt does not exist; a present receipt is never ignored.
- Changes every Cursor evidence row to the frozen ten-column contract from Locked Clarification 9.

- [ ] **Step 1: Add renderer golden tests for all four report states**

Test a mixed report set containing:

1. v3 complete with zero-row layers and one global gap;
2. v3 collection complete with reconciled business integrity incomplete;
3. v3 collection incomplete;
4. existing v1 and v2 legacy reports.

Assert `Trace Layer Facts` has exactly sixteen rows for the two v3-complete items (two changes × two phases × four layers), `Trace Layer Sufficiency` has exactly four rows per complete item, and global gaps appear once per phase rather than once per layer. The incomplete change emits only its closed reason/detail. V1/V2 keep their old projection/coverage/verify overview and display `legacy_unlayered` with no fabricated layer rows.

- [ ] **Step 2: Add exact evidence-row tests**

Use this column order:

```text
change_id|collection_status|reason_code|trace_exit|integrity|gap_count|verify_exit|verdict|blocking|insufficient
```

Expected state rules:

- raw CLI observation: `raw|none` plus its observed execution/verify fields;
- v3 complete: `complete|none`, using execution overview integrity/gap count (the same phase as raw/legacy rows) and verify diagnostics;
- v3 incomplete: `incomplete|<closed reason>`, optional command exits when present, and literal `unknown` for every unavailable field;
- loaded v1/v2 report: `legacy_unlayered|none` plus its existing global values.

Assert booleans, negative counts, wrong field count, unknown collection status/reason, and numeric zero substituted for an unavailable incomplete field fail. Raw trace/verify missing, invalid UTF-8, invalid JSON, or missing count arrays produce literal `unknown` fields—not `invalid`, `-1`, or `0`—and make the raw row fail before numeric comparisons.

The `evidence-row` command always prints a row for a valid report, then exits 0 for `report_collection_exit == 0` or exits 1 for a valid-but-incomplete overall report. Invalid report/model/identity exits 2 without a row. Cover V3 trace complete + capability incomplete, V3 trace incomplete, V2 capability incomplete, V2 complete, and V1 legacy.

- [ ] **Step 3: Add failing shell parser and caller retention tests**

First pin the public activation boundary: `collect_report` and CLI `collect` now emit V3, every Task 17 modeled reason writes a valid incomplete V3 plus committed receipt before returning 1, complete V3 returns 0, and unexpected exceptions publish no new committed receipt. Add source guards proving the old benchmark-local V2 projection/sufficiency aggregation and direct legacy model parses are gone only after this switch.

Update shell fixtures to ten fields. Assert:

- `run_trace_verify_stage` adds `raw` rows;
- `run_specialty_report_stage` selects trace/verify exits by named positions, not the old eight-field offset;
- a nonzero collect that published a valid incomplete v3 report plus matching current-attempt receipt registers it in `SPECIALTY_REPORT_FILES`, replaces the raw row, sets `SPECIALTY_REPORT_FAILED=true`, and continues rendering;
- a nonzero collect with no matching receipt registers nothing even when a valid report from a prior attempt exists;
- a pending receipt registers nothing on fresh finalization or later reuse;
- pending receipt plus an old V1 or V2 report still registers nothing; a committed V3 receipt plus a substituted V1/V2 report also fails rather than downgrading around digest validation;
- a receipt with wrong state, attempt, change, report digest, trace status, or capability integrity registers nothing;
- empty attempt/change, or output and receipt paths resolving to the same filesystem path, fails before either file is mutated;
- resume/reuse accepts V3 complete/incomplete only with a matching durable receipt, accepts legacy V1/V2 without one, and marks V3 or V2 capability replay incomplete even when trace status is complete;
- a V3 left with a pending receipt after report rename but before committed receipt publication is rejected on later reuse;
- incomplete overall outcomes and raw unknown fields fail item success immediately without parsing `unknown` as an integer;
- final Markdown prints collection status/reason columns.

- [ ] **Step 4: Run render/Cursor tests and observe legacy assumptions**

```bash
uv run pytest -q \
  tests/unit/eval/test_specialty_models.py \
  tests/unit/benchmark/test_specialty_report.py \
  tests/unit/benchmark/test_cursor_loop_helpers.py
```

Expected: renderer subscripts typed v3 as dictionaries, row parsers expect eight columns, and nonzero collect drops a valid incomplete report from the render set.

- [ ] **Step 5: Dispatch rendering by concrete report and collection variant**

Use small type-directed helpers:

```python
def _render_v3_trace(
    report: SpecialtyReportV3,
) -> tuple[list[str], list[str], list[str]]:
    evidence = report.traceability_evidence
    if isinstance(evidence, IncompleteTraceabilityEvidenceV3):
        return (
            [f"- `{_cell(report.change_id)}`: {evidence.reason_code}; {_cell(evidence.detail)}."],
            [],
            [],
        )
    return (
        _render_v3_overview(report, evidence),
        _render_v3_fact_rows(report, evidence),
        _render_v3_sufficiency_rows(report, evidence),
    )
```

The literal Markdown backticks around a change ID are presentation characters inside the returned string. Capability/policy rendering treats V2 and V3 as consumers of the same replay model. Legacy global rendering remains isolated from v3 validators.

- [ ] **Step 6: Atomically activate V3, add the receipt commit point, and centralize row replacement**

Switch public `load_specialty_report` to Task 15's concrete document dispatch, then widen renderer/callers in the same commit. Switch public `collect_report` and CLI to Task 17's V3 builder and remove the old V2 aggregation/direct model parsing in that same atomic activation. Require a non-empty unique attempt ID and change ID before mutation. Shell callers derive the one stable sibling path exactly as `receipt_file="${report_file}.receipt.json"`, so resume needs no pointer/index. Resolve output and receipt paths first and reject equality (including symlink-equivalent existing paths) before writing pending; neither target may overwrite the other.

Use a closed discriminated receipt document:

```python
class PendingSpecialtyPublicationReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["1"] = "1"
    state: Literal["pending"] = "pending"
    attempt_id: NonEmptyStr
    change_id: NonEmptyStr


class CommittedSpecialtyPublicationReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["1"] = "1"
    state: Literal["committed"] = "committed"
    attempt_id: NonEmptyStr
    change_id: NonEmptyStr
    report_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    trace_status: Literal["complete", "incomplete"]
    capability_integrity: Literal["complete", "incomplete"]


SpecialtyPublicationReceipt = Annotated[
    PendingSpecialtyPublicationReceipt | CommittedSpecialtyPublicationReceipt,
    Field(discriminator="state"),
]
```

Wrap that union in a Pydantic `RootModel` for loading; null/unknown version/state and extra fields fail closed. `validate-publication` accepts only the committed arm.

Build and fully validate the V3 object/canonical bytes in memory before touching either path. Then use this ABA-safe publication order:

1. atomically replace the sibling receipt with a valid `state="pending"` record for the new attempt/change;
2. atomically replace the report with canonical validated V3 bytes;
3. atomically replace pending with `state="committed"`, binding the same attempt/change and exact report digest/status fields;
4. only then return `report_collection_exit(report)`.

Inject crashes before pending replacement, after pending replacement, after report replacement, and after committed replacement. Before pending replacement, the untouched prior report/committed receipt remains a valid old publication but cannot validate as the new fresh attempt. In both middle windows, pending blocks fresh and reuse registration even when old/new report bytes are identical. After committed replacement, both fresh and reuse validation succeed. This is a three-rename protocol, not a claim of pairwise transactionality.

Add one helper that validates ten columns and branches on `collection_status` before any integer comparison. Add one helper that replaces the existing row for a change ID, so final output has one authoritative row rather than both raw and specialty rows. Pass the unique attempt ID and stable receipt path to `collect`, and require `validate-publication` success before registering the report.

```bash
IFS='|' read -r \
  cid collection_status reason_code trace_exit integrity gap_count \
  verify_exit verdict blocking insufficient <<EOF
$row
EOF

case "$collection_status" in
  incomplete)
    SPECIALTY_REPORT_FAILED="true"
    ;;
  raw)
    if [ "$integrity" = "unknown" ] || [ "$gap_count" = "unknown" ] \
      || [ "$verdict" = "unknown" ] || [ "$blocking" = "unknown" ] \
      || [ "$insufficient" = "unknown" ]; then
      SPECIALTY_REPORT_FAILED="true"
    fi
    ;;
  complete|legacy_unlayered)
    ;;
  *)
    return 1
    ;;
esac
```

Use a stable sibling receipt path for each report. For fresh collection, `finalize_benchmark_specialty_report` first validates committed state and receipt attempt/change/digest/status fields against the report and current attempt; only then run `evidence-row`. Exit 0 registers a successful row, exit 1 registers the valid report/row and separately marks item failure, and exit 2 registers nothing. Reuse checks the sibling path before version dispatch: if a receipt exists, pending/malformed/mismatched state fails and only a committed receipt bound to a V3 report proceeds; if no receipt exists, V3 fails while V1/V2 may use the legacy evidence-row path. Thus a pending-publication crash artifact, receipt-backed downgrade, and capability-incomplete V2/V3 cannot become successful merely by resume.

- [ ] **Step 7: Run all renderer, helper, and shell syntax checks**

```bash
uv run pytest -q \
  tests/unit/eval/test_specialty_models.py \
  tests/unit/benchmark/test_specialty_report.py \
  tests/unit/benchmark/test_cursor_loop_helpers.py
bash -n benchmark/vue-fastapi-admin/benchmark/cursor-loop-helpers.sh
bash -n benchmark/vue-fastapi-admin/benchmark/run-workflow-loop-cursor.sh
uv run ruff check .
uv run pyright
uv run pyright benchmark/vue-fastapi-admin/benchmark/benchmark_specialty_report.py
```

Expected: mixed-version rendering is deterministic and Cursor never confuses unknown evidence with zero evidence.

- [ ] **Step 8: Commit collection-aware presentation**

```bash
git add assurance_agent/eval/specialty_models.py assurance_agent/eval/specialty_render.py benchmark/vue-fastapi-admin/benchmark/benchmark_specialty_report.py benchmark/vue-fastapi-admin/benchmark/cursor-loop-helpers.sh benchmark/vue-fastapi-admin/benchmark/run-workflow-loop-cursor.sh tests/unit/eval/test_specialty_models.py tests/unit/benchmark/test_specialty_report.py tests/unit/benchmark/test_cursor_loop_helpers.py
git diff --cached --check
git commit -m "feat(benchmark): render layered and incomplete trace evidence"
```

---

### Task 19: Document Wire Compatibility and Run the Release Gate

**Files:**
- Modify: `docs/schemas.md`
- Modify: `tests/unit/artifacts/test_validate.py`
- Modify: `tests/unit/commands/test_report_cmd.py`
- Modify: `tests/unit/benchmark/test_specialty_report.py`
- Modify: `tests/unit/benchmark/test_cursor_loop_helpers.py`
- Modify: `tests/integration/test_cli_validate.py`

**Interfaces:**
- Documents exact writer/reader versions, V1 alias promises, current-authority limits, recovery gaps, four-layer summaries, and v3 collection semantics.
- Adds command-level compatibility fixtures for registry validation, report generation, mixed specialty rendering, and old-pinned collection.
- Generates those deterministic artifact trees under pytest `tmp_path`; no repository fixture directory is added.
- Changes no production behavior.

- [ ] **Step 1: Add failing command-level compatibility coverage**

Create explicit tests that build their mixed-version artifact trees under `tmp_path` and prove:

- `aa validate --change <fixture-change>` accepts legacy missing-version Trace V1 and explicit Trace V2, IssueReconcileStatus V1/V2, and QualityGateResult V1/V2;
- explicit null/unknown versions fail closed;
- `aa report generate --change <fixture-change>` reads V1-only and V2-only quality artifacts through concrete dispatch while keeping QualityReport `"1.1"`;
- specialty render accepts a mixed V1/V2/V3 set and labels only the old reports `legacy_unlayered`;
- collection against an old pinned run with no current reconciled V2 atomically emits `reconciled_projection_missing` and exits nonzero;
- an old repaired V1 projection emits stale/incomplete, never a complete v3 matrix;
- receipt-committed V3 resumes, report-only V3 is rejected, and receiptless V1/V2 remain readable only when no sibling receipt exists.

- [ ] **Step 2: Run compatibility tests and observe documentation/fixture gaps**

```bash
uv run pytest -q \
  tests/unit/artifacts/test_validate.py \
  tests/unit/commands/test_report_cmd.py \
  tests/unit/benchmark/test_specialty_report.py \
  tests/unit/benchmark/test_cursor_loop_helpers.py \
  tests/integration/test_cli_validate.py
```

Expected: any remaining direct-version assumptions or missing legacy classification fail.

- [ ] **Step 3: Update the schema reference**

Document:

- Trace Projection `1` versus `2`, V2-only codes, legacy missing-version default, and point-in-time freshness;
- Issue Reconcile Status `1.0` versus `2.0` and exact pending/completed/failed shapes;
- Quality Gate `1.0` versus `2.0` typed sufficiency/error union;
- Sufficiency `2.0` provenance and reporting-only layer join;
- execution/reconciled enrichment-only phase-pair invariant;
- three-graph settled-path topology and recovery-as-incomplete rule;
- Specialty Report `1`/`2` legacy versus `3` complete/incomplete union;
- V3 pending-receipt → report → committed-receipt publication, fresh-attempt validation, ABA protection, and receipt-required reuse semantics;
- exact Cursor ten-column row and `unknown` semantics;
- pinned historical graph/contract/catalog continuation, incompatible semantic/model fail-closed boundary, and lack of historical Python handler guarantees.

Do not describe v1 aliases as current writers and do not imply a stale disk projection is live authority.

- [ ] **Step 4: Run focused compatibility plus every design seam**

```bash
uv run pytest -q \
  tests/unit/artifacts/test_trace_models.py \
  tests/unit/artifacts/test_models_issues.py \
  tests/unit/artifacts/test_models_inspect_report.py \
  tests/unit/artifacts/test_sufficiency_models.py \
  tests/unit/artifacts/test_registry.py \
  tests/unit/artifacts/test_validate.py \
  tests/unit/execution/test_evidence.py \
  tests/unit/execution/test_runner_trace.py \
  tests/unit/report/test_quality_gate.py \
  tests/unit/report/test_inspector.py \
  tests/unit/report/test_compat_fallback.py \
  tests/unit/report/test_report_builder.py \
  tests/unit/commands/test_report_cmd.py \
  tests/unit/evidence/test_digests.py \
  tests/unit/evidence/test_layer_summary.py \
  tests/unit/evidence/test_layer_sufficiency.py \
  tests/unit/evidence/test_issue_replay_authority.py \
  tests/unit/evidence/test_fold_trace_execution.py \
  tests/unit/evidence/test_fold_trace_reconciled.py \
  tests/unit/workflow/graph/test_canonical_schema_v2.py \
  tests/unit/workflow/graph/test_contracts.py \
  tests/integration/test_trace_recovery_workflows.py \
  tests/integration/test_graph_runtime_faults.py \
  tests/integration/test_cli_validate.py \
  tests/unit/eval/test_specialty_models.py \
  tests/unit/benchmark/test_specialty_report.py \
  tests/unit/benchmark/test_cursor_loop_helpers.py
```

Expected: the full spec section 13 verification matrix is represented and green.

- [ ] **Step 5: Run all six CI gates**

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pyright benchmark/vue-fastapi-admin/benchmark/benchmark_specialty_report.py
uv run lint-imports
uv run pytest -q
bash scripts/packaging_smoke_test.sh
```

Expected: all commands exit zero. Missing optional frontend/backend/Playwright checks are not part of this repository gate.

- [ ] **Step 6: Run CLI and benchmark smoke checks**

```bash
uv run aa --version
uv run aa validate --help
uv run aa report generate --help
uv run pytest -q \
  tests/integration/test_trace_cli.py \
  tests/integration/test_verify_cli.py \
  tests/integration/test_cli_validate.py \
  tests/unit/commands/test_report_cmd.py
uv run python benchmark/vue-fastapi-admin/benchmark/benchmark_specialty_report.py --help
```

The focused compatibility test builds one deterministic mixed-version artifact tree under `tmp_path`, renders it twice, and compares exact bytes. It also runs `evidence-row` for generated v3 complete, v3 incomplete, and v2 legacy reports and asserts no `--schema-root` option remains. Do not add hand-maintained committed JSON fixture copies for these model-generated wires.

- [ ] **Step 7: Commit docs and compatibility fixtures**

```bash
git add docs/schemas.md tests/unit/artifacts/test_validate.py tests/unit/commands/test_report_cmd.py tests/unit/benchmark/test_specialty_report.py tests/unit/benchmark/test_cursor_loop_helpers.py tests/integration/test_cli_validate.py
git diff --cached --check
git commit -m "docs(trace): record layered recovery evidence contracts"
```

---

## Requirement Coverage Map

| Design obligation | Implemented and proven in |
|---|---|
| D1/D2 pure four-layer facts and phase orthogonality | Tasks 1–3, 12, 15–16 |
| D3 manifest-rooted failure/current/historical issue authority | Tasks 6–12 |
| D4 recovery publishes current incomplete evidence | Tasks 8–12, 14 |
| D5 every settled graph path materializes | Tasks 12–14 |
| D6 typed specialty v3 and legacy rendering | Tasks 15–19 |
| D7 corrected pinned historical continuation and executable-epoch fail-closed boundary | Task 14 |
| D8 disk projection point-in-time freshness | Tasks 11–12, 16–17 |
| D9 explicit Trace/status/quality version boundaries | Tasks 1, 4–5, 8, 19 |
| Bound sufficiency reporting join | Tasks 3–5, 15–17 |
| Healing, repeatability, atomic incomplete reporting | Tasks 14, 17–18 |
| Full validation matrix and six CI gates | Tasks 13–19 |

## Final Acceptance Checklist

- [ ] Every new writer emits the planned version and every legacy alias still names V1.
- [ ] Every document wrapper round-trips both supported versions and rejects unknown versions.
- [ ] Pure fact and sufficiency summaries contain exactly four ordered layers and conserve every count.
- [ ] Execution/reconciled pairs allow only declared enrichment and cannot improve integrity.
- [ ] Reconciled issue/problem links are justified by strict current or historical cross-ledger authority.
- [ ] Each typed recovery produces current-batch incomplete V2 with one stable blocking gap and no stale links.
- [ ] All seven terminal-path classes refresh a preseeded B0 projection to B1 before completion.
- [ ] Compatible pinned graph/contract/catalog drift resumes exact old topology without injected nodes or frozen-byte upgrades; any later V2 comes only from a materializer already present and newly executed in that pinned graph. Incompatible semantic/profile/model epochs stop before pending recovery or planning.
- [ ] Complete specialty v3 is fully bound; every modeled failure publishes through pending receipt → atomic closed incomplete V3 → committed receipt, with all crash windows and identical-byte ABA replay fail-closed.
- [ ] Cursor retains only receipt-committed V3 (including valid incomplete reports), keeps V1/V2 legacy compatibility, and treats unavailable fields as `unknown`, never zero.
- [ ] Focused design matrix, Ruff, format, Pyright, import-linter, pytest, and packaging smoke all pass.

---
