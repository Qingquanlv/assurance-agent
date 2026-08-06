from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from assurance_agent.artifacts.models.data_knowledge import (
    AuthLeaf,
    CapabilityLeaf,
    DataKnowledge,
    DataKnowledgeProposal,
    EntityLeaf,
)
from assurance_agent.artifacts.registry import match_artifact
from assurance_agent.artifacts.repo_registry import resolve_repo_model

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "data_knowledge"


def _load(name: str) -> object:
    return yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))


def test_capability_leaf_valid_fixture() -> None:
    CapabilityLeaf.model_validate(_load("capability_leaf_valid.yaml"))


def test_capability_leaf_invalid_fixture_rejected() -> None:
    with pytest.raises(ValidationError):
        CapabilityLeaf.model_validate(_load("capability_leaf_invalid.yaml"))


def test_auth_leaf_valid_fixture_without_symbol() -> None:
    leaf = AuthLeaf.model_validate(_load("auth_leaf_valid.yaml"))
    assert leaf.method == "token"
    assert leaf.symbol == "tests.api.conftest.admin_token"


def test_auth_leaf_invalid_fixture_rejected() -> None:
    with pytest.raises(ValidationError):
        AuthLeaf.model_validate(_load("auth_leaf_invalid.yaml"))


def test_l1_valid_fixture() -> None:
    DataKnowledge.model_validate(_load("l1_valid.yaml"))


def test_l1_historical_boolean_max_length_remains_readable_without_normalization() -> None:
    raw = {
        "version": 1,
        "entities": {
            "dept": {
                "required_fields": ["name"],
                "constraints": {
                    "name_non_empty": True,
                    "name_has_max_length": True,
                    "name_unique": True,
                },
            }
        },
    }

    parsed = DataKnowledge.model_validate(raw)

    assert parsed.entities["dept"].constraints == raw["entities"]["dept"]["constraints"]


def test_l1_invalid_fixture_rejected() -> None:
    with pytest.raises(ValidationError):
        DataKnowledge.model_validate(_load("l1_invalid.yaml"))


def test_l2_valid_fixture() -> None:
    DataKnowledgeProposal.model_validate(_load("l2_valid.api.yaml"))


def test_entity_leaf_preserves_historical_flattened_non_max_length_flags() -> None:
    EntityLeaf.model_validate(
        {
            "constraints": {
                "name_non_empty": True,
                "name_unique": True,
            }
        }
    )


def test_entity_leaf_accepts_parameterized_flattened_max_length() -> None:
    leaf = EntityLeaf.model_validate({"constraints": {"name_has_max_length": 20, "name_unique": True}})
    assert leaf.constraints is not None
    assert leaf.constraints["name_has_max_length"] == 20


def test_entity_leaf_accepts_positive_nested_max_length() -> None:
    EntityLeaf.model_validate({"constraints": {"name": {"max_length": 20, "unique": True}}})


@pytest.mark.parametrize("max_length", [True, False, 0, -1, "20"])
def test_entity_leaf_rejects_invalid_nested_max_length(max_length: object) -> None:
    with pytest.raises(ValidationError, match="max_length must be a positive integer"):
        EntityLeaf.model_validate({"constraints": {"name": {"max_length": max_length}}})


@pytest.mark.parametrize("max_length", [True, False, 0, -1, "20"])
def test_entity_leaf_rejects_invalid_flattened_max_length(max_length: object) -> None:
    with pytest.raises(ValidationError, match="max_length must be a positive integer"):
        EntityLeaf.model_validate({"constraints": {"name_has_max_length": max_length}})


@pytest.mark.parametrize(
    "nested_sequence",
    ([{"max_length": True}], ({"max_length": True},)),
)
def test_entity_leaf_rejects_boolean_max_length_in_nested_sequences(
    nested_sequence: object,
) -> None:
    with pytest.raises(ValidationError, match="max_length must be a positive integer"):
        EntityLeaf.model_validate({"constraints": {"nested": nested_sequence}})


def test_resolve_repo_model_for_l1() -> None:
    assert resolve_repo_model(".aa/data-knowledge.yaml") is DataKnowledge


def test_registry_matches_per_layer_proposal() -> None:
    spec = match_artifact("plans/data-knowledge.proposal.api.yaml")
    assert spec is not None
    assert spec.model is DataKnowledgeProposal
