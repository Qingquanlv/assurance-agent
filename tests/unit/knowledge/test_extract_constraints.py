"""ORM AST → DataKnowledgeProposal cold start (spec §5-A2)."""

from __future__ import annotations

from pathlib import Path

from assurance_agent.artifacts.models.data_knowledge import (
    DataKnowledge,
    DataKnowledgeProposal,
)
from assurance_agent.knowledge.extract_constraints import (
    constraint_known_keys,
    extract_entity_constraints,
    extract_entity_constraints_from_source,
)
from assurance_agent.verification.property_scan import (
    PROPERTY_UNKNOWN_KEY,
    scan_property_tests,
)

DEPT_MODEL = """
from tortoise import fields
from .base import BaseModel, TimestampMixin


class Dept(BaseModel, TimestampMixin):
    name = fields.CharField(max_length=20, unique=True, description="部门名称", index=True)
    desc = fields.CharField(max_length=500, null=True, description="备注")
    order = fields.IntField(default=0, description="排序", index=True)
    parent_id = fields.IntField(default=0, null=False, description="父部门ID", index=True)

    class Meta:
        table = "dept"
"""


def test_extract_from_source_maps_unique_max_length_and_null_false() -> None:
    proposal = extract_entity_constraints_from_source(DEPT_MODEL)
    assert isinstance(proposal, DataKnowledgeProposal)
    dept = proposal.entities["dept"]
    assert dept.constraints is not None
    assert dept.constraints["name_unique"] is True
    assert dept.constraints["name_has_max_length"] == 20
    assert dept.constraints["desc_has_max_length"] == 500
    assert dept.required_fields == ["parent_id"]
    # null=True fields must not become required
    assert "desc" not in (dept.required_fields or [])


def test_extract_produces_proposal_never_writes_l1(tmp_path: Path) -> None:
    model = tmp_path / "admin.py"
    model.write_text(DEPT_MODEL, encoding="utf-8")
    l1 = tmp_path / ".aa" / "data-knowledge.yaml"
    l1.parent.mkdir(parents=True)
    original = "version: 1\nentities: {}\n"
    l1.write_text(original, encoding="utf-8")

    proposal = extract_entity_constraints([model], based_on_l1_version=1)
    assert proposal.mode == "delta"
    assert proposal.based_on_l1_version == 1
    assert "dept" in proposal.entities
    assert l1.read_text(encoding="utf-8") == original
    assert proposal.needs_review
    assert proposal.promotion_checklist


def test_constraint_known_keys_closed_set_for_property_scan() -> None:
    proposal = extract_entity_constraints_from_source(DEPT_MODEL)
    keys = constraint_known_keys(proposal)
    assert "entities.dept.constraints.name_unique" in keys
    assert "entities.dept.constraints.name_has_max_length" in keys
    assert "entities.dept.constraints.desc_has_max_length" in keys
    # required_fields are not property constraint keys
    assert not any("required_fields" in key for key in keys)


def test_constraint_known_keys_expands_nested_shapes_to_a2_flattened() -> None:
    """Nested L1 shapes must yield A2 marker keys, not opaque field keys."""
    dk = DataKnowledge.model_validate(
        {
            "version": 1,
            "entities": {
                "dept": {
                    "constraints": {
                        "name": {"max_length": 20, "unique": True},
                        "desc_has_max_length": 500,
                    }
                }
            },
        }
    )
    keys = constraint_known_keys(dk)
    assert "entities.dept.constraints.name_unique" in keys
    assert "entities.dept.constraints.name_has_max_length" in keys
    assert "entities.dept.constraints.desc_has_max_length" in keys
    # Opaque nested field key must not remain as a closed-set member.
    assert "entities.dept.constraints.name" not in keys


def test_property_scan_unknown_keys_against_nested_expanded_known_set(
    tmp_path: Path,
) -> None:
    dk = DataKnowledge.model_validate(
        {
            "version": 1,
            "entities": {"dept": {"constraints": {"name": {"max_length": 20, "unique": True}}}},
        }
    )
    known = constraint_known_keys(dk)
    path = tmp_path / "test_props.py"
    path.write_text(
        "import pytest\n"
        "@pytest.mark.property(\n"
        '    "entities.dept.constraints.name_unique",\n'
        '    "entities.dept.constraints.invented_key",\n'
        ")\n"
        "def test_mixed():\n"
        "    pass\n",
        encoding="utf-8",
    )
    result = scan_property_tests([path], root=tmp_path, known_keys=known)
    assert "entities.dept.constraints.name_unique" in known
    assert len(result.unknown_key_gaps) == 1
    assert result.unknown_key_gaps[0].code == PROPERTY_UNKNOWN_KEY
    assert result.unknown_key_gaps[0].subject == "entities.dept.constraints.invented_key"


def test_entity_name_acronyms_and_simple_names() -> None:
    api_token = """
from tortoise import fields

class APIToken(BaseModel):
    name = fields.CharField(max_length=20, unique=True)
"""
    dept = """
from tortoise import fields

class Dept(BaseModel):
    name = fields.CharField(max_length=20, unique=True)
"""
    api = """
from tortoise import fields

class Api(BaseModel):
    path = fields.CharField(max_length=200, unique=True)
"""
    assert "api_token" in extract_entity_constraints_from_source(api_token).entities
    assert "a_p_i_token" not in extract_entity_constraints_from_source(api_token).entities
    assert "dept" in extract_entity_constraints_from_source(dept).entities
    assert "api" in extract_entity_constraints_from_source(api).entities


def test_bootstrap_mode_when_no_l1_version() -> None:
    proposal = extract_entity_constraints_from_source(DEPT_MODEL)
    assert proposal.mode == "bootstrap"
    assert proposal.based_on_l1_version is None


def test_syntax_error_source_yields_empty_entities() -> None:
    proposal = extract_entity_constraints_from_source("class Dept(\\n")
    assert proposal.entities == {}
