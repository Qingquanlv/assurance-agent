# Layer Assurance Profile and Cross-Layer Assurance Design

**Date:** 2026-07-30

## 1. Problem

The capability-contract pilot currently has an API-shaped control path:

- `operation:verify-plan-mechanical` accepts a layer string but always runs every registered
  check;
- only `api-plan-cycle` materializes mechanical evidence;
- only `api-plan-review-gate` consumes that evidence and `policy.plan_checks`;
- the benchmark policy replay is hard-coded to the API review, check artifact, alias, and gate;
- Fuzz and Performance ask their reviewers to self-report `layer_applicable`, making an
  agent-authored verdict the authority for an objectively derivable fact;
- capability requirements are mandatory only for API and E2E review contracts;
- Trace rows carry `case_type`, but reports do not expose layer summaries and recovery paths can
  bypass reconciled projection materialization.

This creates two dangerous failure modes. A check can run against a plan shape it does not
understand and report a false failure, or it can fail to parse its expected input and silently
report `pass`. Separately, missing cross-layer wiring can look like successful assurance because
the check never ran.

## 2. Decision

Introduce one code-owned, immutable `LayerAssuranceProfile` registry as the source of truth for
cross-layer assurance wiring. Keep two concerns separate:

1. the profile decides whether a layer and each check are applicable and declares the artifacts
   required to evaluate them;
2. `.aa/policy.yaml` decides what action a failed applicable check causes.

The runtime, not a reviewer, derives applicability from validated case artifacts. Every known
check appears in mechanical evidence as `pass`, `fail`, or `not_applicable`; omission is never a
synonym for success.

The four layers share the existing `policy.plan_checks.<check_id>` actions. This change does not
introduce per-layer policy overrides or a configurable rule language.

## 3. Deep Module and Interface

Place the seam in the verification package so the mechanical runner, policy loader, workflow
gate helpers, and benchmark reporter consume the same interface without importing individual
check implementations.

The external interface is intentionally small:

```python
LayerName = Literal["api", "e2e", "fuzz", "performance"]

@dataclass(frozen=True)
class LayerAssuranceProfile:
    layer: LayerName
    case_type: Literal["API", "E2E", "Fuzz", "Performance"]
    plan_artifacts: tuple[str, ...]
    review_artifact: str
    review_alias: str
    checks_artifact: str
    gate_id: str
    applicable_check_ids: frozenset[str]
    capability_contract_enabled: bool

def get_layer_assurance_profile(layer: str) -> LayerAssuranceProfile: ...
def iter_layer_assurance_profiles() -> tuple[LayerAssuranceProfile, ...]: ...
```

`plan_artifacts` contains exact mechanically relevant plan paths, not globs. Exact paths make a
missing plan distinguishable from an empty or unparseable plan. Summary Markdown that no check
consumes is not included.

The registry must validate at import or construction time that:

- it contains exactly the four supported layers;
- layer, case type, artifact paths, aliases, and gate IDs are unique where uniqueness is required;
- every `applicable_check_id` is present in the check catalog;
- every check catalog entry is consumed by at least one profile;
- review and mechanical evidence paths remain under `review/`.

Workflow topology remains explicit in `workflow-schema.yaml`; the profile does not generate graph
nodes or edges. Compile-time mutation tests compare the explicit topology with the profile so the
YAML cannot silently drift.

## 4. Static Check Applicability

The initial matrix is:

| Check | API | E2E | Fuzz | Performance |
|---|---:|---:|---:|---:|
| `l1_path` | applicable | applicable | applicable | applicable |
| `shared_factory` | applicable | applicable | applicable | applicable |
| `assert_ideal` | applicable | applicable | not applicable | not applicable |
| `capability_keys` | applicable | applicable | applicable | applicable |

`assert_ideal` is limited to functional API/E2E assertions. Fuzz plans carry robustness
properties and Performance plans carry threshold/scenario expectations, so applying HTTP
rejection-preservation semantics to them would be a category error.

All four layers participate in the capability contract. `shared_factory` remains applicable to
all layers because all four plan contracts can consume shared domain factories and their
layer-specific adapters.

## 5. Dynamic Layer Applicability

The runtime derives a `LayerApplicability` result from validated `cases/**/case.yaml` documents:

```python
class LayerApplicability(BaseModel):
    layer: LayerName
    applicable: bool
    reason_code: Literal["automated_cases_present", "no_automated_cases"]
    case_ids: tuple[str, ...]
```

A layer is applicable when at least one `added` or `modified` case has the profile's exact
`case_type` and `automation.required is true`. Removed cases, manual-only cases, and cases for a
different layer do not make it applicable.

Malformed case documents are errors, not `no_automated_cases`. The operation must fail and the
gate must stop rather than treating unreadable scope as an empty scope.

`Review.layer_applicable` ceases to be an authority and is removed from authoring contracts and
reviewer instructions. Old review JSON may retain the field under the generic review model's
compatibility behavior, but no runtime, gate, or report reads it.

## 6. Mechanical Evidence Contract

Extend check evidence with an explicit third state:

```python
CheckStatus = Literal["pass", "fail", "not_applicable"]

class CheckEvidence(BaseModel):
    check_id: str
    status: CheckStatus
    applicability_reason: str | None
    findings: tuple[Finding, ...]
    refs: tuple[str, ...]
```

The plan-check document also binds itself to the evaluated layer and applicability result. Its
aggregate status is:

- `not_applicable` when the layer has no automated cases;
- `fail` when the layer is applicable and at least one applicable check fails;
- `pass` when the layer is applicable and every applicable check passes.

For an inapplicable layer, every known check is emitted as `not_applicable` with the layer reason.
For an applicable layer, checks excluded by the static profile are emitted as `not_applicable`
with a static-profile reason. Applicable checks must emit `pass` or `fail`.

The document is invalid if it contains duplicate or unknown check IDs, omits any known check ID,
reports findings on a `pass`/`not_applicable` check, or reports `fail` without findings. This
prevents partial execution from being interpreted as a successful check run.

The runner validates cases and derives layer applicability before loading plan inputs. An
inapplicable layer can therefore emit complete `not_applicable` evidence even when no layer plan
was generated. For an applicable layer, the runner loads all exact `plan_artifacts` declared by
the profile before invoking a check. A missing required plan or data-knowledge input is an
operation failure. A check may return `not_applicable` only through the profile executor;
individual check implementations continue to return factual `pass` or `fail` results.

## 7. Review and Capability Contracts

API, E2E, Fuzz, and Performance plan-review artifacts must all declare:

- a layer-specific `review_type`;
- `change_id`;
- `required_capabilities` using fully qualified L1 leaf keys;
- the existing decision, readiness, risk, finding, auto-fix, and next-action fields consumed by
  downstream skills and gates.

Add Fuzz and Performance review artifacts to the specific artifact registry before the generic
`review/*.json` matcher and bind them to the plan-review authoring model. Extend the allowed
`review_type` values and capability validation to all four plan review types.

For an applicable layer, `required_capabilities` must be present and non-empty. An inapplicable
layer is routed by mechanical evidence and does not rely on an empty capability list as an escape
hatch. Review prompts must derive the list from the plan and L1 contract and must not invent
missing L1 leaves.

## 8. Generic Gate Semantics and Graph Wiring

Each plan cycle runs its mechanical node before review. Any loop that changes plan inputs or L1
capability inputs returns through the mechanical node so the next gate cannot consume stale
evidence.

Each plan-review gate reads its canonical review artifact, mechanical evidence artifact, and
`repo:.aa/data-knowledge.yaml`. Gate evaluation follows this fail-closed precedence:

1. missing, malformed, wrong-layer, incomplete, or stale mechanical evidence → `stop`;
2. layer applicability is false → `skip`;
3. missing capability contract or missing required L1 leaves → the existing remediation/human
   path;
4. applicable check failures with action `require_human` → `needs_human_review`;
5. applicable check failures with action `block` → `reject`;
6. reviewer verdict/readiness and existing human-review rules;
7. only a valid reviewer pass plus a mechanically acceptable result → `pass`.

`warn` does not hide findings: the evidence retains them while the gate may pass. Unknown policy
actions, unknown check IDs, and a mismatch between the profile's applicable set and the evidence
are `stop`, never `pass`.

The API gate's behavior is preserved except where the new explicit applicability and completeness
checks intentionally make it stricter. E2E receives the same mechanical and policy path. Fuzz and
Performance receive capability remediation, mechanical evidence, and policy handling while
retaining a graceful empty-scope `skip`.

Every codegen entry, including `codegen-only`, must cross the corresponding plan-review gate and a
fresh mechanical evidence producer. A pre-existing review/check file on disk is not sufficient
unless it is bound to the current invocation's producer result. Resume continues from the audited
interrupt and re-runs mechanical checks whenever the chosen action can alter plans or L1.

## 9. Policy and Replay

Keep the existing global mapping:

```yaml
plan_checks:
  l1_path: warn
  shared_factory: warn
  assert_ideal: warn
  capability_keys: warn
```

The policy loader gets the known check set from the same foundational catalog used by the profile
registry. It continues to reject missing and unknown keys.

Refactor policy replay into a layer-parameterized function. For each applicable layer and each
action (`warn`, `block`, `require_human`), replay changes all check actions, evaluates that layer's
real gate with its canonical aliases/artifacts, and records verdict, matched rule, and missing
capabilities. Inapplicable layers are reported as `skip`, not omitted.

Benchmark output contains one replay row per change and layer. A missing layer artifact is shown
as an explicit error/incomplete result and cannot be coerced to the API artifact.

## 10. Trace Layer Summaries

`TraceRow.case_type` remains the row-level source of truth. Keep `TraceProjection` fact-only and
add a pure `summarize_projection_by_layer(projection)` view rather than rescanning case or result
files in the benchmark. For every layer, the fact summary reports at least:

- total, automated, covered, and uncovered rows;
- current-batch executed/not-present/not-selected counts;
- passed, failed, and skipped latest executions;
- failure links, open-problem links, and gap counts attributable to the layer.

All four layers are emitted even when their counts are zero. Projection-level gaps without a
target remain in a separate global bucket; target-bearing gaps contribute to the matching layer.
The sum of layer row counts must equal the projection row count. The benchmark/reporting adapter
joins this fact summary with `SufficiencyReport` by `case_id` to add sufficient and insufficient
counts; policy-derived sufficiency never becomes a field of `TraceProjection`.

## 11. Recovery Completion

The `inspect-with-issues` success path already materializes the reconciled trace projection, but
its analyzer-failure and project-sync-pending recovery routes currently jump directly to
`inspect-complete`. Change both recovery continuations to reach
`materialize-trace-projection` first.

Recovery materializes the current authoritative batch and records explicit recovery/source gaps.
It must not silently retain issue/problem associations from a prior batch. When current-batch
issue reconciliation is unavailable, execution facts and per-layer summaries remain present, but
the reconciled projection is `incomplete` and the missing reconciliation authority is visible in
gaps.

Projection materialization itself remains deterministic and non-LLM. If it cannot write a valid
projection, the recovery path fails rather than marking inspection complete.

## 12. Error Handling and Compatibility

- Unknown layer names fail at the profile lookup seam.
- Missing exact plan inputs, malformed cases, malformed L1, or malformed reviews fail closed.
- `not_applicable` is never synthesized from a parser failure or missing input.
- Existing valid API artifacts remain readable; new fields use deterministic defaults only where
  doing so cannot convert missing evidence into success.
- Historical `layer_applicable` fields are ignored during migration and are no longer rendered in
  authoring contracts.
- Existing policy files remain valid because policy keys and action meanings do not change.
- Trace schema changes must use an explicit compatible schema decision; consumers cannot infer a
  new layer summary from an absent field and call it complete.

## 13. Verification Strategy

### Round-trip tests

- Generate canonical plan/review/case/L1 fixtures for each layer from the declared authoring
  contracts, run mechanical checks, validate evidence, and evaluate the gate.
- Prove a compliant applicable layer reaches `pass` under warn policy with zero false findings.
- Prove an empty-scope layer produces explicit `not_applicable` evidence and a gate `skip`.
- Cross-check reviewer outputs against codegen/fixer STOP prerequisites for all four layers.

### Mutation guards

Mutate one contract edge at a time and prove validation or compilation fails:

- remove a profile, check ID, plan artifact, graph mechanical node, gate read, policy key,
  authoring field, or recovery projection continuation;
- add an unknown check or layer;
- change a review alias/path or connect a layer to another layer's evidence;
- omit one check result, mark a non-applicable check as pass, or suppress a parser failure;
- route recovery directly to completion.

Tests must assert runtime behavior or parsed topology/models, not English prose or source-text line
counts.

### Codegen-only and resume tests

- For every layer, `codegen-only` runs mechanical checks before its gate and cannot pass with
  missing/stale plan-check evidence.
- A gate interrupt followed by `fix_and_proceed` re-enters the mechanical node when plans or L1
  may change.
- `accept_risk` remains bound to audited gate reads and cannot approve a different review/check
  digest.
- Fresh runtime reconstruction from the ledger preserves the same next node and gate outcome.

### Trace and recovery tests

- Layer summaries reconcile exactly with row-level facts and keep deterministic ordering.
- Analyzer failure and project-sync failure both produce a current-batch incomplete projection
  before inspection completes.
- Recovery never carries prior-batch problem links as current facts.
- Initial execution and every healing rerun replace the authoritative batch consistently.

## 14. Out of Scope

- User-defined layers or checks.
- Per-project or per-layer applicability expressions.
- Per-layer policy actions.
- Replacing the workflow DSL or generating the workflow schema from Python.
- Changing the substantive algorithms inside the four existing checks beyond the minimum needed
  to honor their declared inputs.
- Expanding automated healing beyond its current API/E2E targets.

## 15. Acceptance Criteria

- One validated profile registry is the only cross-layer assurance mapping used by runtime and
  benchmark replay.
- No gate or reviewer consumes `Review.layer_applicable`.
- Mechanical evidence contains exactly every known check with explicit applicability status.
- API, E2E, Fuzz, and Performance applicable scopes all enforce capability presence, mechanical
  evidence, and policy action before codegen.
- Policy replay reports all four layers for all three actions without API-specific artifact
  assumptions.
- Trace output contains deterministic four-layer summaries whose totals reconcile with rows.
- Normal and recovery inspection paths materialize a current-batch reconciled projection;
  unavailable reconciliation is explicit and fail-closed.
- Round-trip, mutation, codegen-only, resume, recovery, focused unit/integration, and complete CI
  suites pass.
