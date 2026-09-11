from __future__ import annotations

from copy import deepcopy
import json
from importlib.resources import files
import re
import unicodedata

import pytest
from pydantic import ValidationError

from graph_engine.composition import FrozenComposition

pytestmark = pytest.mark.usefixtures("installed_sources")

_SHA = "a" * 64
_FAMILY_EMPTY_ENTRYPOINTS = (
    "case",
    "execute",
    "archive",
    "retro",
    "issue-review",
    "issue-analyze",
    "issue-reconcile",
    "improvement-review",
    "improvement-evaluate",
    "improvement-export",
    "improvement-apply",
    "improvement-rollback",
)
_FAMILY_NONEMPTY_ENTRYPOINTS = ("full", "intake")

_PLAN_REF = {
    "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
    "digest": _SHA,
}


def valid_product_input(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1",
        "change_id": "CH-DEMO-001",
        "requirement": "Add login",
        "run_mode": "case",
        "candidate_test_families": (),
        "case_delta_paths": (),
        "capability_leafs": (),
        "capability_catalog": {
            "resource_id": "assurance.product.configuration.capability-catalog",
            "sha256": _SHA,
        },
        "product_policy": {
            "resource_id": "assurance.product.configuration.product-policy",
            "sha256": _SHA,
        },
        "data_knowledge": {
            "resource_id": "assurance.product.configuration.data-knowledge",
            "sha256": _SHA,
        },
        "allowed_artifact_paths": ("qa/cases", "qa/fixtures", "qa/results", "qa/tests"),
        "budgets": {
            "review_rounds": 1,
            "coverage_rounds": 1,
            "healing_rounds": 1,
            "execution_retries": 1,
        },
    }
    payload.update(overrides)
    return payload


def test_product_input_is_closed_and_stable():
    from assurance_product.models import ProductInputV1

    value = ProductInputV1.model_validate(valid_product_input())
    assert value.schema_version == "1"
    with pytest.raises(ValidationError):
        ProductInputV1.model_validate({**valid_product_input(), "model": "ambient"})


def test_product_input_document_satisfies_model_and_schema():
    from assurance_product.models import ProductInputV1

    schema = json.loads(
        files("assurance_product")
        .joinpath("resources/schemas/product-input-v1.json")
        .read_text(encoding="utf-8")
    )
    document = valid_product_input(capability_leafs=("auth.session", "entities.user"))
    value = ProductInputV1.model_validate(document)
    required = schema["required"]
    properties = schema["properties"]
    assert isinstance(required, list)
    assert isinstance(properties, dict)
    assert "capability_leafs" in required
    assert "capability_leafs" in properties
    assert "case_delta_paths" in properties
    assert "retro_window" in properties
    assert value.capability_leafs == ("auth.session", "entities.user")
    assert document["capability_leafs"] == ("auth.session", "entities.user")


def test_only_retro_entrypoint_accepts_retro_window() -> None:
    from assurance_product.models import ProductInputV1

    window = {
        "selection": {
            "mode": "change_ids",
            "requested_change_ids": ["CH-DEMO-001", "CH-DEMO-002"],
        },
        "change_ids": ["CH-DEMO-001", "CH-DEMO-002"],
    }
    value = ProductInputV1.model_validate(valid_product_input(retro_window=window))
    value.validate_for_entrypoint("retro")
    with pytest.raises(ValueError, match="does not consume retro_window"):
        value.validate_for_entrypoint("archive")


def test_standalone_retro_preserves_an_explicit_window() -> None:
    from assurance_product.graphs.entrypoints import adapt_retro
    from assurance_improvement.contracts.retro import RetroSelectionSnapshot, RetroWindow

    window = RetroWindow(
        selection=RetroSelectionSnapshot(
            mode="change_ids", requested_change_ids=("CH-DEMO-001", "CH-DEMO-002")
        ),
        change_ids=("CH-DEMO-001", "CH-DEMO-002"),
    )
    state = valid_product_input(retro_window=window.model_dump(mode="json"))
    result = adapt_retro(state)  # type: ignore[arg-type]
    assert result["window"] == window.model_dump(mode="json")


def test_full_retro_uses_the_current_change_and_report_receipt_identity() -> None:
    from assurance_product.graphs.entrypoints import adapt_retro

    report_receipt = {"receipt_id": "report-1", "receipt_digest": "b" * 64}
    state = valid_product_input()
    state["report_outcome"] = {"report_receipt": report_receipt}
    first = adapt_retro(state)  # type: ignore[arg-type]
    second = adapt_retro(state)  # type: ignore[arg-type]
    assert first["window"]["change_ids"] == ["CH-DEMO-001"]  # type: ignore[index]
    assert first["retro_id"] == second["retro_id"]


def test_product_input_rejects_auto_archive():
    from assurance_product.models import ProductInputV1

    with pytest.raises(ValidationError):
        ProductInputV1.model_validate(valid_product_input(auto_archive=False))
    with pytest.raises(ValidationError):
        ProductInputV1.model_validate(valid_product_input(auto_archive=True))


def test_product_input_normalizes_change_id_and_requirement():
    from assurance_product.models import ProductInputV1

    decomposed = "Add login\u0301"
    value = ProductInputV1.model_validate(
        valid_product_input(change_id="  CH-DEMO-001  ", requirement=f"  {decomposed}  ")
    )
    assert value.change_id == "CH-DEMO-001"
    assert value.requirement == unicodedata.normalize("NFC", decomposed)
    assert unicodedata.is_normalized("NFC", value.requirement)


@pytest.mark.parametrize("change_id", ["", "   ", "CH DEMO", "CH\tDEMO"])
def test_product_input_rejects_non_canonical_change_id(change_id: str):
    from assurance_product.models import ProductInputV1

    with pytest.raises(ValidationError):
        ProductInputV1.model_validate(valid_product_input(change_id=change_id))


@pytest.mark.parametrize("requirement", ["", "   "])
def test_product_input_rejects_empty_requirement(requirement: str):
    from assurance_product.models import ProductInputV1

    with pytest.raises(ValidationError):
        ProductInputV1.model_validate(valid_product_input(requirement=requirement))


def test_product_input_requires_canonical_family_order():
    from assurance_product.models import ProductInputV1

    value = ProductInputV1.model_validate(
        valid_product_input(candidate_test_families=("api", "e2e", "fuzz", "performance"))
    )
    assert value.candidate_test_families == ("api", "e2e", "fuzz", "performance")
    with pytest.raises(ValidationError):
        ProductInputV1.model_validate(valid_product_input(candidate_test_families=("e2e", "api")))
    with pytest.raises(ValidationError):
        ProductInputV1.model_validate(valid_product_input(candidate_test_families=("api", "api")))


def test_product_input_rejects_the_removed_public_selection_field() -> None:
    from assurance_product.models import ProductInputV1

    with pytest.raises(ValidationError):
        ProductInputV1.model_validate(valid_product_input(selected_test_families=("api",)))


def test_product_input_requires_sorted_unique_capability_leafs():
    from assurance_product.models import ProductInputV1

    value = ProductInputV1.model_validate(
        valid_product_input(capability_leafs=("auth.session", "entities.user"))
    )
    assert value.capability_leafs == ("auth.session", "entities.user")
    with pytest.raises(ValidationError):
        ProductInputV1.model_validate(valid_product_input(capability_leafs=("entities.user", "auth.session")))


def test_product_input_requires_sorted_relative_artifact_prefixes():
    from assurance_product.models import ProductInputV1

    value = ProductInputV1.model_validate(
        valid_product_input(allowed_artifact_paths=("qa/cases", "qa/fixtures", "qa/results", "qa/tests"))
    )
    assert value.allowed_artifact_paths == ("qa/cases", "qa/fixtures", "qa/results", "qa/tests")
    with pytest.raises(ValidationError):
        ProductInputV1.model_validate(
            valid_product_input(allowed_artifact_paths=("qa/changes", "qa/archive"))
        )
    with pytest.raises(ValidationError):
        ProductInputV1.model_validate(valid_product_input(allowed_artifact_paths=("/abs/path",)))
    with pytest.raises(ValidationError):
        ProductInputV1.model_validate(valid_product_input(allowed_artifact_paths=("qa/../secret",)))
    with pytest.raises(ValidationError):
        ProductInputV1.model_validate(
            valid_product_input(allowed_artifact_paths=("qa/changes", "qa/changes"))
        )


@pytest.mark.parametrize(
    "case_delta_paths",
    [
        ("qa/results/cases/system/dept/case.yaml",),
        (
            "qa/cases/system/role/case.yaml",
            "qa/cases/system/dept/case.yaml",
        ),
        ("qa/cases/system/dept/not-case.yaml",),
        ("qa/cases/case.yaml",),
        (" qa/cases/system/dept/case.yaml",),
        ("qa/cases/部门/case.yaml",),
        ("qa/cases/system/../../../../CH-SIBLING/cases/x/case.yaml",),
    ],
)
def test_product_input_requires_exact_current_change_case_delta_paths(
    case_delta_paths: tuple[str, ...],
) -> None:
    from assurance_product.models import ProductInputV1

    with pytest.raises(ValidationError):
        ProductInputV1.model_validate(valid_product_input(case_delta_paths=case_delta_paths))


def test_product_input_accepts_exact_current_change_case_delta_path() -> None:
    from assurance_product.models import ProductInputV1

    path = "qa/cases/system/dept/case.yaml"
    value = ProductInputV1.model_validate(valid_product_input(case_delta_paths=(path,)))
    assert value.case_delta_paths == (path,)


def test_case_delta_path_model_matches_public_schema_ascii_shape() -> None:
    schema = json.loads(
        files("assurance_product")
        .joinpath("resources/schemas/product-input-v1.json")
        .read_text(encoding="utf-8")
    )
    pattern = re.compile(schema["properties"]["case_delta_paths"]["items"]["pattern"])
    valid = "qa/cases/system/dept/case.yaml"
    invalid = (
        f" {valid}",
        "qa/cases/部门/case.yaml",
        "qa/cases/system/dept/not-case.yaml",
        ("qa/cases/system/../../../../CH-SIBLING/cases/x/case.yaml"),
    )
    assert pattern.fullmatch(valid)
    assert all(pattern.fullmatch(path) is None for path in invalid)


@pytest.mark.parametrize("entrypoint", ("full", "intake", "case"))
def test_case_design_entrypoints_require_exact_case_delta_paths(entrypoint: str) -> None:
    from assurance_product.models import ProductInputV1

    candidate = ("api",) if entrypoint in {"full", "intake"} else ()
    plan_ref = _PLAN_REF if entrypoint == "case" else None
    with pytest.raises(ValueError, match="case_delta_paths"):
        ProductInputV1.model_validate(
            valid_product_input(
                candidate_test_families=candidate,
                resolved_plan_ref=plan_ref,
            )
        ).validate_for_entrypoint(entrypoint)


def test_resource_ref_sha256_is_lowercase_hex():
    from assurance_product.models import ProductInputV1

    with pytest.raises(ValidationError):
        ProductInputV1.model_validate(
            valid_product_input(
                product_policy={
                    "resource_id": "assurance.product.configuration.product-policy",
                    "sha256": "A" * 64,
                }
            )
        )
    with pytest.raises(ValidationError):
        ProductInputV1.model_validate(
            valid_product_input(
                product_policy={
                    "resource_id": "assurance.product.configuration.product-policy",
                    "sha256": "abc",
                }
            )
        )


@pytest.mark.parametrize("entrypoint", _FAMILY_EMPTY_ENTRYPOINTS)
def test_empty_family_entrypoints_require_empty_selection(entrypoint: str):
    from assurance_product.models import ProductInputV1

    case_delta_paths = ("qa/cases/system/dept/case.yaml",) if entrypoint == "case" else ()
    plan_ref = _PLAN_REF if entrypoint in {"case", "execute"} else None
    value = ProductInputV1.model_validate(
        valid_product_input(case_delta_paths=case_delta_paths, resolved_plan_ref=plan_ref)
    )
    value.validate_for_entrypoint(entrypoint)
    with pytest.raises(ValueError):
        ProductInputV1.model_validate(
            valid_product_input(
                candidate_test_families=("api",),
                case_delta_paths=case_delta_paths,
                resolved_plan_ref=plan_ref,
            )
        ).validate_for_entrypoint(entrypoint)


@pytest.mark.parametrize("entrypoint", _FAMILY_NONEMPTY_ENTRYPOINTS)
def test_plan_creators_require_non_empty_candidates(entrypoint: str):
    from assurance_product.models import ProductInputV1

    case_delta_paths = ("qa/cases/system/dept/case.yaml",)
    ProductInputV1.model_validate(
        valid_product_input(
            candidate_test_families=("api",),
            case_delta_paths=case_delta_paths,
        )
    ).validate_for_entrypoint(entrypoint)
    with pytest.raises(ValueError):
        ProductInputV1.model_validate(
            valid_product_input(case_delta_paths=case_delta_paths)
        ).validate_for_entrypoint(entrypoint)


@pytest.mark.parametrize("entrypoint", ("case", "execute"))
def test_plan_consumers_require_one_resolved_plan_ref(entrypoint: str) -> None:
    from assurance_product.models import ProductInputV1

    case_delta_paths = ("qa/cases/system/dept/case.yaml",) if entrypoint == "case" else ()
    ProductInputV1.model_validate(
        valid_product_input(case_delta_paths=case_delta_paths, resolved_plan_ref=_PLAN_REF)
    ).validate_for_entrypoint(entrypoint)
    with pytest.raises(ValueError, match="requires resolved_plan_ref"):
        ProductInputV1.model_validate(
            valid_product_input(case_delta_paths=case_delta_paths)
        ).validate_for_entrypoint(entrypoint)


@pytest.mark.parametrize("entrypoint", ("full", "intake"))
def test_plan_creators_reject_imported_plan(entrypoint: str) -> None:
    from assurance_product.models import ProductInputV1

    with pytest.raises(ValueError, match="cannot accept resolved_plan_ref"):
        ProductInputV1.model_validate(
            valid_product_input(
                candidate_test_families=("api",),
                resolved_plan_ref=_PLAN_REF,
                case_delta_paths=("qa/cases/system/dept/case.yaml",),
            )
        ).validate_for_entrypoint(entrypoint)


def test_product_input_authenticates_resource_refs_against_composition(opencode_composition):
    from assurance_product.models import ProductInputV1

    composition = opencode_composition
    refs = {
        "capability_catalog": _ref_from_composition(
            composition, "assurance.product.configuration.capability-catalog"
        ),
        "product_policy": _ref_from_composition(
            composition, "assurance.product.configuration.product-policy"
        ),
        "data_knowledge": _ref_from_composition(
            composition, "assurance.product.configuration.data-knowledge"
        ),
    }
    value = ProductInputV1.model_validate(valid_product_input(**refs))
    value.authenticate_against(composition)

    drifted = deepcopy(refs)
    drifted["product_policy"] = {**drifted["product_policy"], "sha256": "b" * 64}
    with pytest.raises(ValueError):
        ProductInputV1.model_validate(valid_product_input(**drifted)).authenticate_against(composition)

    missing = deepcopy(refs)
    missing["product_policy"] = {
        "resource_id": "assurance.product.configuration.missing-policy",
        "sha256": refs["product_policy"]["sha256"],
    }
    with pytest.raises((ValidationError, ValueError)):
        ProductInputV1.model_validate(valid_product_input(**missing)).authenticate_against(composition)

    forged_leafs = ProductInputV1.model_validate(
        valid_product_input(capability_leafs=("entities.virtual",), **refs)
    )
    with pytest.raises(ValueError, match="authenticated catalog"):
        forged_leafs.authenticate_against(composition)


def _ref_from_composition(composition: FrozenComposition, resource_id: str) -> dict[str, str]:
    entry = composition.registries.resources.entries[resource_id]
    return {"resource_id": resource_id, "sha256": entry.sha256}
