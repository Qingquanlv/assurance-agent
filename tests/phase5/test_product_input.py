from __future__ import annotations

from copy import deepcopy
import unicodedata

import pytest
from pydantic import ValidationError

from graph_engine.composition import FrozenComposition

from tests.phase5.composition_harness import request_for

pytestmark = pytest.mark.usefixtures("installed_sources")

_SHA = "a" * 64
_FAMILY_EMPTY_ENTRYPOINTS = (
    "intake",
    "case",
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
_FAMILY_NONEMPTY_ENTRYPOINTS = ("full", "execute")


def valid_product_input(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1",
        "change_id": "CH-DEMO-001",
        "requirement": "Add login",
        "run_mode": "case",
        "selected_test_families": (),
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
        "allowed_artifact_paths": ("qa/changes",),
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
        valid_product_input(selected_test_families=("api", "e2e", "fuzz", "performance"))
    )
    assert value.selected_test_families == ("api", "e2e", "fuzz", "performance")
    with pytest.raises(ValidationError):
        ProductInputV1.model_validate(valid_product_input(selected_test_families=("e2e", "api")))
    with pytest.raises(ValidationError):
        ProductInputV1.model_validate(valid_product_input(selected_test_families=("api", "api")))


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
        valid_product_input(allowed_artifact_paths=("qa/archive", "qa/changes"))
    )
    assert value.allowed_artifact_paths == ("qa/archive", "qa/changes")
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

    value = ProductInputV1.model_validate(valid_product_input())
    value.validate_for_entrypoint(entrypoint)
    with pytest.raises(ValueError):
        ProductInputV1.model_validate(
            valid_product_input(selected_test_families=("api",))
        ).validate_for_entrypoint(entrypoint)


@pytest.mark.parametrize("entrypoint", _FAMILY_NONEMPTY_ENTRYPOINTS)
def test_full_and_execute_require_non_empty_families(entrypoint: str):
    from assurance_product.models import ProductInputV1

    ProductInputV1.model_validate(
        valid_product_input(selected_test_families=("api",))
    ).validate_for_entrypoint(entrypoint)
    with pytest.raises(ValueError):
        ProductInputV1.model_validate(valid_product_input()).validate_for_entrypoint(entrypoint)


def test_product_input_authenticates_resource_refs_against_composition(installed_sources):
    from assurance_product.models import ProductInputV1
    from assurance_product.product import resolve_assurance_composition

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
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
