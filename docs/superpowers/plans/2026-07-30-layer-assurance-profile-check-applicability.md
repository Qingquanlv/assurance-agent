# Layer Assurance Profile and Check Applicability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce one typed four-layer assurance profile registry and make deterministic plan-check execution emit complete, explicit applicability evidence without changing policy actions or cross-layer workflow gates yet.

**Architecture:** Put shared layer/check value types in the artifacts layer, immutable runtime profiles and applicability derivation in verification, and keep `run_plan_checks` as the deep interface used by the graph operation. Version 2 mechanical evidence records the layer, dynamic layer applicability, every known check, and explicit `not_applicable` reasons; version 1 artifacts remain readable but cannot be produced by the new runner.

**Tech Stack:** Python 3.11, frozen dataclasses, Pydantic v2, PyYAML, pytest, Ruff, Pyright, import-linter, uv.

## Global Constraints

- The profile decides applicability; `.aa/policy.yaml` continues to decide only `warn`, `block`, or `require_human` actions.
- `LayerAssuranceProfile` is code-owned and immutable; do not add YAML-configurable profiles or applicability expressions.
- The registry contains exactly `api`, `e2e`, `fuzz`, and `performance`.
- Every version 2 mechanical document contains exactly every known check ID; omission is never success.
- A parser error, malformed case, missing required plan, or missing L1 input is never `not_applicable`.
- `assert_ideal` applies only to API and E2E; the other three existing checks apply to all four layers.
- Keep `policy.plan_checks.<check_id>` and its action meanings unchanged.
- Preserve read compatibility for version 1 `api-plan-checks.json`; only version 2 receives the new completeness invariants.
- Do not change review authoring contracts, reviewer skills, workflow gates/topology, benchmark replay, Trace, or recovery in this plan. Those are separate dependent plans.
- Do not globally tighten `CaseYaml`; validate only the fields needed to derive applicability at the verification seam.
- Write each behavior test first, run it and observe the expected failure, then implement the minimum production change.
- Run commands through `uv run`; the final gate is Ruff, format check, Pyright, import-linter, pytest, and packaging smoke.

---

## File Structure

### New files

- `assurance_agent/artifacts/models/assurance.py` — shared layer, case-type, and plan-check identifiers; contains no runtime behavior.
- `assurance_agent/verification/profiles.py` — immutable `LayerAssuranceProfile` registry and lookup interface.
- `assurance_agent/verification/applicability.py` — pure derivation of `LayerApplicability` from case mappings.
- `tests/unit/verification/test_profiles.py` — registry contents and registry mutation guards.
- `tests/unit/verification/test_applicability.py` — dynamic scope derivation and malformed-input behavior.
- `tests/unit/verification/test_layer_assurance_round_trip.py` — four-layer profile → checks → evidence contract tests.

### Modified files

- `assurance_agent/artifacts/models/plan_checks.py` — versioned applicability-aware evidence model and invariants.
- `assurance_agent/artifacts/models/policy.py` — import the shared check/case types instead of owning duplicate literals.
- `assurance_agent/artifacts/models/__init__.py` — export the new shared artifact types.
- `assurance_agent/verification/checks/base.py` — type `CheckContext.layer` with `LayerName`.
- `assurance_agent/verification/checks/registry.py` — profile-aware complete check execution.
- `assurance_agent/workflow/graph/handlers/plan_checks.py` — replace layer/review hard-coding with profile metadata and exact inputs.
- `tests/unit/artifacts/test_plan_checks_model.py` — version 1 compatibility and version 2 invariants.
- `tests/unit/artifacts/test_policy.py` — shared catalog policy validation.
- `tests/unit/verification/test_checks_assert_ideal.py` — adapt the registry-level test to complete API inputs.
- `tests/unit/verification/test_contract_round_trip.py` — assert version 2 API evidence.
- `tests/unit/workflow/graph/handlers/test_plan_checks_operation.py` — exact input, empty-scope, malformed case, and layer-path behavior.
- `docs/schemas.md` — document version 2 mechanical evidence and applicability semantics.

---

### Task 1: Shared Identifier Catalog and Immutable Layer Profiles

**Files:**
- Create: `assurance_agent/artifacts/models/assurance.py`
- Create: `assurance_agent/verification/profiles.py`
- Modify: `assurance_agent/artifacts/models/__init__.py`
- Test: `tests/unit/verification/test_profiles.py`

**Interfaces:**
- Produces: `LayerName`, `CaseType`, `CASE_TYPES`, `PlanCheckId`, `PLAN_CHECK_IDS`, and `KNOWN_PLAN_CHECK_IDS`.
- Produces: `LayerAssuranceProfile`, `get_layer_assurance_profile(layer)`, and `iter_layer_assurance_profiles()`.
- Consumes: no check implementations and no workflow schema types.

- [ ] **Step 1: Write failing tests for the exact four profiles**

Create `tests/unit/verification/test_profiles.py` with assertions equivalent to:

```python
import pytest

from typing import cast

from assurance_agent.artifacts.models.assurance import PLAN_CHECK_IDS, PlanCheckId
from assurance_agent.verification.profiles import (
    get_layer_assurance_profile,
    iter_layer_assurance_profiles,
)


def test_profiles_cover_exactly_four_layers_in_stable_order() -> None:
    profiles = iter_layer_assurance_profiles()
    assert tuple(profile.layer for profile in profiles) == (
        "api",
        "e2e",
        "fuzz",
        "performance",
    )
    assert all(profile.capability_contract_enabled for profile in profiles)


def test_static_check_applicability_matrix() -> None:
    expected = set(PLAN_CHECK_IDS)
    assert get_layer_assurance_profile("api").applicable_check_ids == expected
    assert get_layer_assurance_profile("e2e").applicable_check_ids == expected
    assert get_layer_assurance_profile("fuzz").applicable_check_ids == expected - {"assert_ideal"}
    assert get_layer_assurance_profile("performance").applicable_check_ids == expected - {
        "assert_ideal"
    }


def test_unknown_layer_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown assurance layer: mobile"):
        get_layer_assurance_profile("mobile")
```

Also assert exact API/E2E/Fuzz/Performance `plan_artifacts`, `review_artifact`, `review_alias`,
`checks_artifact`, and `gate_id` values from the approved design.

- [ ] **Step 2: Run the profile tests and observe the missing-module failure**

Run:

```bash
uv run pytest tests/unit/verification/test_profiles.py -q
```

Expected: collection fails because `assurance_agent.verification.profiles` and the shared identifier catalog do not exist.

- [ ] **Step 3: Add shared literal types and stable check order**

Create `assurance_agent/artifacts/models/assurance.py` with this public shape:

```python
from typing import Literal

LayerName = Literal["api", "e2e", "fuzz", "performance"]
CaseType = Literal["API", "E2E", "Fuzz", "Performance"]
PlanCheckId = Literal["l1_path", "shared_factory", "assert_ideal", "capability_keys"]

CASE_TYPES: tuple[CaseType, ...] = ("API", "E2E", "Fuzz", "Performance")
PLAN_CHECK_IDS: tuple[PlanCheckId, ...] = (
    "l1_path",
    "shared_factory",
    "assert_ideal",
    "capability_keys",
)
KNOWN_PLAN_CHECK_IDS = frozenset(PLAN_CHECK_IDS)
```

Export these names from `assurance_agent/artifacts/models/__init__.py` and its `__all__` list.

- [ ] **Step 4: Implement the immutable profile registry**

Create `assurance_agent/verification/profiles.py` with a frozen dataclass matching the spec:

```python
from dataclasses import dataclass

from assurance_agent.artifacts.models.assurance import CaseType, LayerName, PlanCheckId


@dataclass(frozen=True)
class LayerAssuranceProfile:
    layer: LayerName
    case_type: CaseType
    plan_artifacts: tuple[str, ...]
    review_artifact: str
    review_alias: str
    checks_artifact: str
    gate_id: str
    applicable_check_ids: frozenset[PlanCheckId]
    capability_contract_enabled: bool
```

Populate the four profiles with these exact mechanically relevant plans:

```python
{
    "api": (
        "plans/api-plan.md",
        "plans/api-test-data-plan.md",
        "plans/api-codegen-plan.md",
    ),
    "e2e": (
        "plans/e2e-plan.md",
        "plans/e2e-test-data-plan.md",
        "plans/e2e-codegen-plan.md",
    ),
    "fuzz": ("plans/fuzz-plan.md", "plans/fuzz-codegen-plan.md"),
    "performance": (
        "plans/performance-plan.md",
        "plans/performance-codegen-plan.md",
    ),
}
```

Use canonical review aliases and paths:

```python
{
    "api": ("review/api-plan-review.json", "api_plan_review"),
    "e2e": ("review/plan-review.json", "plan_review"),
    "fuzz": ("review/fuzz-plan-review.json", "fuzz_plan_review"),
    "performance": ("review/performance-plan-review.json", "performance_plan_review"),
}
```

The registry builder must reject duplicate layers, duplicate case types, duplicate review/check
paths, artifact paths outside `review/`, unknown check IDs, and a known check consumed by no
profile. Keep the builder private; test mutations through a module-local `_build_profile_registry`
because it is the construction seam used by the production constant.

- [ ] **Step 5: Add registry mutation tests**

Add tests that use `dataclasses.replace` on real profiles and prove `_build_profile_registry`
rejects:

```python
with pytest.raises(ValueError, match="duplicate layer"):
    _build_profile_registry((api, api), known_check_ids=set(PLAN_CHECK_IDS))

with pytest.raises(ValueError, match="unknown check"):
    _build_profile_registry(
        (
            replace(
                api,
                applicable_check_ids=frozenset({cast(PlanCheckId, "invented")}),
            ),
        ),
        known_check_ids=set(PLAN_CHECK_IDS),
    )
```

Include separate mutations for an unconsumed known check and a check artifact outside `review/`.

- [ ] **Step 6: Run tests, type checks, and commit**

Run:

```bash
uv run pytest tests/unit/verification/test_profiles.py -q
uv run pyright assurance_agent/artifacts/models/assurance.py assurance_agent/verification/profiles.py
uv run lint-imports
```

Expected: all profile tests pass, Pyright reports zero errors, and all import contracts are kept.

Commit:

```bash
git add assurance_agent/artifacts/models/assurance.py assurance_agent/artifacts/models/__init__.py assurance_agent/verification/profiles.py tests/unit/verification/test_profiles.py
git commit -m "feat(assurance): add typed layer profiles"
```

---

### Task 2: Version 2 Mechanical Evidence Contract

**Files:**
- Modify: `assurance_agent/artifacts/models/plan_checks.py`
- Test: `tests/unit/artifacts/test_plan_checks_model.py`

**Interfaces:**
- Consumes: `LayerName`, `PLAN_CHECK_IDS`, and `KNOWN_PLAN_CHECK_IDS` from Task 1.
- Produces: `LayerApplicability`, `CheckStatus`, applicability-aware `CheckEvidence`, and version 2 `PlanCheckDocument.from_checks(...)`.
- Preserves: validation of historical version 1 documents without assigning invented layer/applicability facts.

- [ ] **Step 1: Write failing model tests for explicit applicability**

Add tests with this intended interface:

```python
from assurance_agent.artifacts.models.plan_checks import (
    CheckEvidence,
    LayerApplicability,
    PlanCheckDocument,
)


def _api_scope(*, applicable: bool = True) -> LayerApplicability:
    return LayerApplicability(
        layer="api",
        applicable=applicable,
        reason_code=("automated_cases_present" if applicable else "no_automated_cases"),
        case_ids=("TC_API_001",) if applicable else (),
    )


def test_v2_inapplicable_document_contains_every_known_check() -> None:
    checks = [
        CheckEvidence(
            check_id=check_id,
            status="not_applicable",
            applicability_reason="layer_not_applicable",
        )
        for check_id in PLAN_CHECK_IDS
    ]
    doc = PlanCheckDocument.from_checks(
        layer="api",
        applicability=_api_scope(applicable=False),
        checks=checks,
    )
    assert doc.schema_version == "2"
    assert doc.status == "not_applicable"
```

Add focused failures for duplicate IDs, omitted IDs, unknown IDs, layer mismatch, aggregate status
mismatch, a `fail` without findings, findings on `pass`, findings on `not_applicable`, missing N/A
reason, and an N/A reason on `pass`/`fail`.

- [ ] **Step 2: Run the new tests and verify they fail on the old two-state model**

Run:

```bash
uv run pytest tests/unit/artifacts/test_plan_checks_model.py -q
```

Expected: failures mention unsupported `not_applicable`, missing `LayerApplicability`, or the old `from_checks` parameters.

- [ ] **Step 3: Implement the versioned evidence types**

Add these public types:

```python
CheckStatus = Literal["pass", "fail", "not_applicable"]
LayerApplicabilityReason = Literal["automated_cases_present", "no_automated_cases"]
CheckApplicabilityReason = Literal["layer_not_applicable", "check_not_in_profile"]


class LayerApplicability(BaseModel):
    model_config = _FROZEN
    layer: LayerName
    applicable: bool
    reason_code: LayerApplicabilityReason
    case_ids: tuple[str, ...] = ()
```

Use model validators to enforce:

- applicable → `automated_cases_present` and at least one sorted, unique case ID;
- not applicable → `no_automated_cases` and no case IDs;
- version 2 document layer equals applicability layer;
- version 2 check IDs equal `PLAN_CHECK_IDS` exactly once;
- document status is derived from applicability and check statuses.

Change `CheckEvidence.status` to `CheckStatus`, add
`applicability_reason: CheckApplicabilityReason | None = None`, and enforce the finding/reason
truth table described by the tests.

Define `PlanCheckDocument` as:

```python
class PlanCheckDocument(BaseModel):
    model_config = _FROZEN
    schema_version: Literal["1", "2"] = "2"
    layer: LayerName | None = None
    applicability: LayerApplicability | None = None
    status: CheckStatus
    checks: tuple[CheckEvidence, ...] = ()
```

Version 1 accepts the old two-state shape and does not synthesize layer/applicability. Version 2
requires both fields and all completeness invariants. `from_checks` always emits version 2 and
requires keyword-only `layer`, `applicability`, and `checks`.

- [ ] **Step 4: Prove version 1 read compatibility**

Add this regression test:

```python
def test_v1_document_remains_readable_without_invented_applicability() -> None:
    doc = PlanCheckDocument.model_validate(
        {
            "schema_version": "1",
            "status": "pass",
            "checks": [{"check_id": "l1_path", "status": "pass"}],
        }
    )
    assert doc.layer is None
    assert doc.applicability is None
```

Also prove version 1 rejects `not_applicable`, so the new state cannot appear without version 2
context.

- [ ] **Step 5: Run tests and commit**

Run:

```bash
uv run pytest tests/unit/artifacts/test_plan_checks_model.py -q
uv run pyright assurance_agent/artifacts/models/plan_checks.py
```

Expected: all tests pass and Pyright reports zero errors.

Commit:

```bash
git add assurance_agent/artifacts/models/plan_checks.py tests/unit/artifacts/test_plan_checks_model.py
git commit -m "feat(assurance): version plan-check applicability evidence"
```

---

### Task 3: Dynamic Layer Applicability Derivation

**Files:**
- Create: `assurance_agent/verification/applicability.py`
- Test: `tests/unit/verification/test_applicability.py`

**Interfaces:**
- Consumes: `LayerAssuranceProfile` and raw validated-boundary case mappings.
- Produces: `derive_layer_applicability(cases, profile) -> LayerApplicability`.
- Error mode: raises `ValueError` with a source locator for structurally ambiguous scope.

- [ ] **Step 1: Write failing tests for the scope rules**

Create fixtures with `added`, `modified`, and `removed` arrays and assert:

```python
def test_added_and_modified_automated_cases_make_layer_applicable() -> None:
    result = derive_layer_applicability(
        [
            {
                "added": [_case("TC_API_002", "API", required=True)],
                "modified": [_case("TC_API_001", "API", required=True)],
                "removed": [{"case_id": "TC_API_OLD"}],
            }
        ],
        get_layer_assurance_profile("api"),
    )
    assert result.applicable is True
    assert result.case_ids == ("TC_API_001", "TC_API_002")


def test_manual_and_other_layer_cases_are_empty_scope() -> None:
    result = derive_layer_applicability(
        [
            {
                "added": [_case("TC_API_MANUAL", "API", required=False)],
                "modified": [_case("TC_E2E_001", "E2E", required=True)],
                "removed": [],
            }
        ],
        get_layer_assurance_profile("api"),
    )
    assert result.model_dump(mode="json") == {
        "layer": "api",
        "applicable": False,
        "reason_code": "no_automated_cases",
        "case_ids": [],
    }
```

Add tests proving `automation.required: "true"`, non-mapping entries, non-list `added`/`modified`,
blank/missing `case_id` on an automated entry, an unknown case `type`, and non-mapping
`automation` raise `ValueError`. Missing `automation` and missing `automation.required` are
manual-only, not malformed.

- [ ] **Step 2: Run tests and observe the missing function failure**

Run:

```bash
uv run pytest tests/unit/verification/test_applicability.py -q
```

Expected: collection fails because `derive_layer_applicability` does not exist.

- [ ] **Step 3: Implement the pure derivation**

Implement:

```python
def derive_layer_applicability(
    cases: Sequence[Mapping[str, object]],
    profile: LayerAssuranceProfile,
) -> LayerApplicability:
    case_ids: set[str] = set()
    for document_index, document in enumerate(cases):
        for bucket in ("added", "modified"):
            entries = document.get(bucket)
            if not isinstance(entries, list):
                raise ValueError(f"cases[{document_index}].{bucket} must be a list")
            for entry_index, entry in enumerate(entries):
                locator = f"cases[{document_index}].{bucket}[{entry_index}]"
                if not isinstance(entry, Mapping):
                    raise ValueError(f"{locator} must be a mapping")
                case_type = entry.get("type")
                if case_type not in {"API", "E2E", "Fuzz", "Performance"}:
                    raise ValueError(f"{locator}.type is invalid")
                automation = entry.get("automation")
                if automation is None:
                    required = False
                elif not isinstance(automation, Mapping):
                    raise ValueError(f"{locator}.automation must be a mapping")
                else:
                    required = automation.get("required", False)
                    if not isinstance(required, bool):
                        raise ValueError(f"{locator}.automation.required must be a boolean")
                if case_type != profile.case_type or not required:
                    continue
                case_id = entry.get("case_id")
                if not isinstance(case_id, str) or not case_id.strip():
                    raise ValueError(f"{locator}.case_id must be a non-empty string")
                case_ids.add(case_id)
    ordered = tuple(sorted(case_ids))
    return LayerApplicability(
        layer=profile.layer,
        applicable=bool(ordered),
        reason_code="automated_cases_present" if ordered else "no_automated_cases",
        case_ids=ordered,
    )
```

Use `isinstance(required, bool)`, never truthiness or string coercion. Validate only fields needed
for scope derivation; do not edit `CaseYaml` or `CaseEntry`.

- [ ] **Step 4: Run tests, type checks, and commit**

Run:

```bash
uv run pytest tests/unit/verification/test_applicability.py -q
uv run pyright assurance_agent/verification/applicability.py
```

Expected: all tests pass and Pyright reports zero errors.

Commit:

```bash
git add assurance_agent/verification/applicability.py tests/unit/verification/test_applicability.py
git commit -m "feat(assurance): derive runtime layer applicability"
```

---

### Task 4: Profile-Aware Complete Check Execution

**Files:**
- Modify: `assurance_agent/verification/checks/base.py`
- Modify: `assurance_agent/verification/checks/registry.py`
- Modify: `tests/unit/verification/test_checks_assert_ideal.py`
- Test: `tests/unit/verification/test_layer_assurance_round_trip.py`

**Interfaces:**
- Consumes: profile lookup, applicability derivation, stable `PLAN_CHECK_IDS`, and existing check functions.
- Produces: `run_plan_checks(ctx, *, applicability=None) -> PlanCheckDocument` version 2 and `validate_plan_check_document(document, profile) -> PlanCheckDocument`.
- Preserves: individual checks remain fact functions returning only `pass` or `fail`.

- [ ] **Step 1: Write failing four-layer execution tests**

Create `tests/unit/verification/test_layer_assurance_round_trip.py`. Build one automated case and
all exact plan keys for each profile. Use an L1 mapping with empty `domain_factories` and no
required capabilities so the canonical text is mechanically clean.

Assert for API/E2E:

```python
document = run_plan_checks(_context_for("e2e"))
assert document.schema_version == "2"
assert document.layer == "e2e"
assert document.status == "pass"
assert {item.check_id: item.status for item in document.checks} == {
    "l1_path": "pass",
    "shared_factory": "pass",
    "assert_ideal": "pass",
    "capability_keys": "pass",
}
```

Assert for Fuzz/Performance that `assert_ideal` is `not_applicable` with
`check_not_in_profile`, while the other checks pass. Assert an empty API scope emits all four
checks as `not_applicable` with `layer_not_applicable` and aggregate status `not_applicable`.

- [ ] **Step 2: Add fail-closed registry tests**

Add tests proving:

- an applicable layer missing one exact plan key raises `ValueError` naming that path;
- an unknown `ctx.layer` raises the profile lookup error;
- a registered function returning the wrong `check_id` raises `ValueError` rather than being
folded under the wrong policy key;
- supplying `applicability` for another layer raises `ValueError`.
- a version 2 document whose N/A statuses contradict the static profile is rejected by
  `validate_plan_check_document`.

For the check-ID mutation, call a private `_run_profile_checks(ctx, profile, applicability,
checks_by_id)` helper with a copied mapping containing one deliberately misbehaving function.

- [ ] **Step 3: Run tests and verify the old registry fails**

Run:

```bash
uv run pytest tests/unit/verification/test_layer_assurance_round_trip.py tests/unit/verification/test_checks_assert_ideal.py::test_registry_runs_every_check_and_folds_status -q
```

Expected: failures show the old runner has no profile selection, no N/A status, and no version 2 metadata.

- [ ] **Step 4: Implement the profile-aware registry**

Type `CheckContext.layer` as `LayerName` while retaining the default `"api"`. Replace the bare
function tuple with a read-only check-ID mapping in stable catalog order:

```python
CHECKS_BY_ID: Mapping[PlanCheckId, CheckFn] = MappingProxyType(
    {
        "l1_path": check_l1_path,
        "shared_factory": check_shared_factory,
        "assert_ideal": check_assert_ideal,
        "capability_keys": check_capability_keys,
    }
)
```

Keep `PLAN_CHECKS` as a compatibility tuple derived from `PLAN_CHECK_IDS` if existing tests or
callers still import it. Implement execution in this order:

1. resolve the profile from `ctx.layer`;
2. derive or validate `LayerApplicability`;
3. if applicable, reject missing exact `profile.plan_artifacts` keys;
4. iterate every `PLAN_CHECK_IDS` item;
5. emit `layer_not_applicable` without calling checks when the layer is empty;
6. emit `check_not_in_profile` without calling checks when statically excluded;
7. otherwise call the registered function and verify its returned ID;
8. build version 2 evidence with `PlanCheckDocument.from_checks`;
9. call `validate_plan_check_document(document, profile)` before returning it.

`validate_plan_check_document` must reject a wrong document layer, a layer/applicability mismatch,
an applicable profile check reported as N/A, a statically excluded check reported as pass/fail,
or incorrect N/A reason codes. For an inapplicable layer it requires every check to be
`not_applicable` with `layer_not_applicable`.

- [ ] **Step 5: Adapt the existing registry test without weakening it**

Give `test_registry_runs_every_check_and_folds_status` all three exact API plan keys and one
automated API case. Preserve its assertion that the pseudo-L1 path makes the document fail and
that every check ID appears.

- [ ] **Step 6: Run focused tests and commit**

Run:

```bash
uv run pytest tests/unit/verification/test_layer_assurance_round_trip.py tests/unit/verification/test_checks_assert_ideal.py tests/unit/verification/test_contract_round_trip.py -q
uv run pyright assurance_agent/verification/checks
```

Expected: all focused tests pass and Pyright reports zero errors.

Commit:

```bash
git add assurance_agent/verification/checks/base.py assurance_agent/verification/checks/registry.py tests/unit/verification/test_checks_assert_ideal.py tests/unit/verification/test_layer_assurance_round_trip.py tests/unit/verification/test_contract_round_trip.py
git commit -m "refactor(assurance): execute checks through layer profiles"
```

---

### Task 5: Profile-Driven Mechanical Operation Inputs

**Files:**
- Modify: `assurance_agent/workflow/graph/handlers/plan_checks.py`
- Modify: `tests/unit/workflow/graph/handlers/test_plan_checks_operation.py`

**Interfaces:**
- Consumes: `get_layer_assurance_profile`, `derive_layer_applicability`, and `run_plan_checks(..., applicability=...)`.
- Produces: `review/<layer>-plan-checks.json` at the exact profile path with version 2 evidence.
- Error modes: `invalid_input` for unknown layer; `invalid_output` for malformed cases, missing applicable inputs, malformed L1/review, or write failure.

- [ ] **Step 1: Rewrite operation fixtures around real applicability**

Change the default workspace fixture to contain one automated API case and add a helper that
writes all exact plan artifacts from a profile:

```python
def _write_profile_plans(workspace: TaskWorkspace, layer: str, body: str = "# Plan\n") -> None:
    profile = get_layer_assurance_profile(layer)
    for rel in profile.plan_artifacts:
        path = workspace.change_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
```

Assert the helper creates `change_dir/plans/api-plan.md` before using it.

- [ ] **Step 2: Add failing operation tests for profile behavior**

Add tests proving:

- API writes `review/api-plan-checks.json` with `schema_version == "2"` and `layer == "api"`;
- E2E/Fuzz/Performance tasks write their own profile-declared check paths;
- an empty-scope layer succeeds without plan or L1 files and writes aggregate
  `not_applicable` evidence;
- an applicable layer missing one exact plan returns `invalid_output` naming the missing path;
- an applicable layer missing `.aa/data-knowledge.yaml` remains `invalid_output`;
- `automation.required: "true"` returns `invalid_output` instead of becoming empty scope;
- a review at the profile's canonical path supplies `required_capabilities` to
  `capability_keys` on the second/reviewed pass.

- [ ] **Step 3: Run tests and observe hard-coded layer/review failures**

Run:

```bash
uv run pytest tests/unit/workflow/graph/handlers/test_plan_checks_operation.py -q
```

Expected: new assertions fail because the operation uses `_LAYERS`, `_REVIEW_NAMES`, globs, and the old evidence model.

- [ ] **Step 4: Refactor the operation to profile metadata**

Remove `_LAYERS` and `_REVIEW_NAMES`. Resolve the profile once, load every case mapping, derive
applicability, then:

```python
if applicability.applicable:
    plan_texts = {
        rel: (workspace.change_dir / rel).read_text(encoding="utf-8")
        for rel in profile.plan_artifacts
    }
    data_knowledge = _load_yaml_mapping(workspace.project_root / ".aa" / "data-knowledge.yaml")
else:
    plan_texts = {}
    data_knowledge = {}
```

Read required capabilities only from `workspace.change_dir / profile.review_artifact`. Missing
review is valid on the first mechanical pass and yields an empty tuple; malformed present review
must raise instead of being silently ignored. Write to
`workspace.change_dir / profile.checks_artifact`.

Do not catch `ValidationError` broadly as success. Convert `OSError`, `ValueError`, Pydantic
validation errors, and YAML errors to `task_failure("invalid_output", ...)`.

- [ ] **Step 5: Run operation and artifact tests and commit**

Run:

```bash
uv run pytest tests/unit/workflow/graph/handlers/test_plan_checks_operation.py tests/unit/artifacts/test_plan_checks_model.py tests/unit/artifacts/test_registry.py -q
uv run pyright assurance_agent/workflow/graph/handlers/plan_checks.py
```

Expected: all tests pass and Pyright reports zero errors.

Commit:

```bash
git add assurance_agent/workflow/graph/handlers/plan_checks.py tests/unit/workflow/graph/handlers/test_plan_checks_operation.py
git commit -m "refactor(workflow): load plan checks from layer profiles"
```

---

### Task 6: Make Policy Consume the Shared Check Catalog

**Files:**
- Modify: `assurance_agent/artifacts/models/policy.py`
- Modify: `tests/unit/artifacts/test_policy.py`
- Test: `tests/unit/verification/test_profiles.py`

**Interfaces:**
- Consumes: `CaseType`, `CASE_TYPES`, and `KNOWN_PLAN_CHECK_IDS` from `artifacts.models.assurance`.
- Preserves: `Policy.plan_checks: dict[str, PlanCheckAction]`, default YAML, digests, and all action behavior.
- Removes: duplicate literal ownership from `policy.py` without changing its import-level compatibility name.

- [ ] **Step 1: Write a failing shared-catalog identity test**

Add:

```python
import assurance_agent.artifacts.models.assurance as assurance_model
import assurance_agent.artifacts.models.policy as policy_model
from assurance_agent.artifacts.models.assurance import KNOWN_PLAN_CHECK_IDS


def test_policy_requires_exact_shared_check_catalog(tmp_path: Path) -> None:
    policy = load_policy(tmp_path)
    assert set(policy.plan_checks) == KNOWN_PLAN_CHECK_IDS


def test_policy_reexports_the_shared_catalog_object() -> None:
    assert policy_model.KNOWN_PLAN_CHECK_IDS is assurance_model.KNOWN_PLAN_CHECK_IDS
```

Add a profile test asserting every profile's applicable IDs are a subset of the same constant.

- [ ] **Step 2: Run focused tests before the import refactor**

Run:

```bash
uv run pytest tests/unit/artifacts/test_policy.py tests/unit/verification/test_profiles.py -q
```

Expected: `test_policy_reexports_the_shared_catalog_object` fails because `policy.py` still owns a
different frozenset; the other policy behavior tests pass.

- [ ] **Step 3: Replace duplicate policy literals with shared imports**

Import `CaseType` and `KNOWN_PLAN_CHECK_IDS` from
`assurance_agent.artifacts.models.assurance`. Keep a module-level imported
`KNOWN_PLAN_CHECK_IDS` name so existing internal imports remain compatible. Remove the local
`CaseType`, `_REQUIRED_CASE_TYPES`, and check-ID definitions; bind `_REQUIRED_CASE_TYPES =
CASE_TYPES` so evidence-sufficiency key validation consumes the same shared order.

- [ ] **Step 4: Run policy, AST-consumer, and import-layer tests**

Run:

```bash
uv run pytest tests/unit/artifacts/test_policy.py tests/unit/workflow/orchestration/test_policy_scope.py tests/unit/test_dsl_schema_corpus.py -q
uv run lint-imports
uv run pyright assurance_agent/artifacts/models/policy.py
```

Expected: no policy behavior or digest fixture changes, all six import contracts kept, and zero
Pyright errors.

- [ ] **Step 5: Commit**

```bash
git add assurance_agent/artifacts/models/policy.py tests/unit/artifacts/test_policy.py tests/unit/verification/test_profiles.py
git commit -m "refactor(policy): share the assurance check catalog"
```

---

### Task 7: Canonical Round-Trip, Mutation Guards, and Documentation

**Files:**
- Modify: `tests/unit/verification/test_layer_assurance_round_trip.py`
- Modify: `tests/unit/verification/test_contract_round_trip.py`
- Modify: `docs/schemas.md`

**Interfaces:**
- Consumes: all interfaces from Tasks 1–6.
- Produces: end-to-end core contract coverage that later gate/replay plans can reuse.

- [ ] **Step 1: Add a parameterized canonical four-layer round trip**

Parameterize `api`, `e2e`, `fuzz`, and `performance`. For each layer:

1. build raw case mappings with one exact automated case;
2. build all exact plan keys from the profile;
3. call `run_plan_checks`;
4. serialize with `model_dump_json`;
5. parse with `PlanCheckDocument.model_validate_json`;
6. assert equality, version 2, exact check order, matching layer, and no findings.

The expected status map is all `pass` for API/E2E and `assert_ideal=not_applicable` for
Fuzz/Performance. Do not weaken this to aggregate-status-only assertions.

- [ ] **Step 2: Add mutation guards at the public seams**

Add one test per mutation:

- delete a required plan key → `run_plan_checks` raises with the exact path;
- replace a boolean automation flag with the string `"true"` → applicability raises;
- drop a check from serialized version 2 evidence → model validation raises;
- add an unknown check → model validation raises;
- change evidence layer without changing applicability layer → model validation raises;
- mark statically excluded `assert_ideal` as `pass` in Fuzz evidence →
  `validate_plan_check_document` raises.

The last mutation must call the verification validation seam, not only Pydantic, because static
profile applicability is runtime catalog knowledge.

- [ ] **Step 3: Update the existing API canonical fixture assertion**

Extend `test_canonical_api_plan_satisfies_its_mechanical_contract` to assert:

```python
assert document.schema_version == "2"
assert document.layer == "api"
assert document.applicability is not None
assert document.applicability.applicable is True
assert tuple(item.check_id for item in document.checks) == PLAN_CHECK_IDS
```

- [ ] **Step 4: Document the version 2 artifact**

Update `docs/schemas.md` with:

- the `layer` and `applicability` fields;
- `pass`, `fail`, and `not_applicable` meanings;
- the distinction between empty layer scope and a statically excluded check;
- exact-completeness and fail-closed malformed-input rules;
- version 1 read-only compatibility and version 2 production behavior;
- a note that cross-layer graph/gate consumption lands in the dependent plan, so
  `Review.layer_applicable` is not removed by this commit series.

- [ ] **Step 5: Run the complete focused assurance suite**

Run:

```bash
uv run pytest tests/unit/artifacts/test_plan_checks_model.py tests/unit/artifacts/test_policy.py tests/unit/verification tests/unit/workflow/graph/handlers/test_plan_checks_operation.py -q
uv run ruff check assurance_agent/artifacts/models/assurance.py assurance_agent/artifacts/models/plan_checks.py assurance_agent/artifacts/models/policy.py assurance_agent/verification assurance_agent/workflow/graph/handlers/plan_checks.py tests/unit/verification tests/unit/artifacts/test_plan_checks_model.py tests/unit/artifacts/test_policy.py tests/unit/workflow/graph/handlers/test_plan_checks_operation.py
uv run ruff format --check assurance_agent/artifacts/models/assurance.py assurance_agent/artifacts/models/plan_checks.py assurance_agent/artifacts/models/policy.py assurance_agent/verification assurance_agent/workflow/graph/handlers/plan_checks.py tests/unit/verification tests/unit/artifacts/test_plan_checks_model.py tests/unit/artifacts/test_policy.py tests/unit/workflow/graph/handlers/test_plan_checks_operation.py
uv run pyright
uv run lint-imports
```

Expected: all focused tests pass, Ruff and format checks pass, Pyright reports zero errors, and all
import contracts are kept.

- [ ] **Step 6: Run full CI-equivalent verification**

Run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest -q
bash scripts/packaging_smoke_test.sh
```

Expected: all commands exit 0. Only the existing Pydantic field-shadow warning is acceptable;
new warnings are failures.

- [ ] **Step 7: Commit**

```bash
git add docs/schemas.md tests/unit/verification/test_layer_assurance_round_trip.py tests/unit/verification/test_contract_round_trip.py
git commit -m "test(assurance): lock layer profile round trips"
```

---

## Dependent Plans

After this plan passes complete CI, write and execute two separate plans in order:

1. **Cross-layer gate and replay wiring** — extend plan-review authoring contracts to Fuzz and
   Performance, add E2E/Fuzz/Performance mechanical nodes, capability checks, gate consumption,
   codegen-only/resume coverage, and four-layer policy replay; remove all runtime consumption of
   `Review.layer_applicable`.
2. **Trace layer summaries and recovery** — add the pure fact summary, join policy sufficiency in
   reporting, route both inspection recovery paths through projection materialization, reject
   prior-batch reconciliation authority, and add normal/recovery/healing-rerun tests.

Do not start either dependent plan until this plan's version 2 evidence and profile interfaces are
merged and stable.
