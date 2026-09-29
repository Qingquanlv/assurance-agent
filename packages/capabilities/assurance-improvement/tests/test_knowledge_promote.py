from __future__ import annotations

import json
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from assurance_improvement.operations.knowledge_promote import (
    KnowledgePromoteError,
    load_promotable_delta,
    promote_knowledge,
)

_L1 = """\
# canonical domain knowledge
version: 1
journeys:
  - dept_management_crud
entities:
  dept:
    required_fields:
      - name
"""


def _project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    target = path / ".aa" / "data-knowledge.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(_L1, encoding="utf-8")
    return path


def _proposal() -> dict[str, object]:
    return {
        "schema_version": "1",
        "based_on_l1_version": 99,
        "mode": "delta",
        "needs_review": ["entities.user"],
        "entities": {"user": {"required_fields": ["username"], "constraints": None, "notes": None}},
    }


def test_promote_requires_yes_before_writing(tmp_path: Path) -> None:
    project = _project(tmp_path)
    before = (project / ".aa" / "data-knowledge.yaml").read_text(encoding="utf-8")

    with pytest.raises(KnowledgePromoteError) as raised:
        promote_knowledge(project, _proposal(), yes=False)

    assert raised.value.code == 30
    assert raised.value.outcome is not None
    assert raised.value.outcome.written is False
    assert raised.value.outcome.merged_keys == ("entities.user",)
    assert (project / ".aa" / "data-knowledge.yaml").read_text(encoding="utf-8") == before


def test_promote_writes_the_leaf_and_keeps_comments_and_journeys(tmp_path: Path) -> None:
    project = _project(tmp_path)
    outcome = promote_knowledge(project, _proposal(), yes=True)
    text = (project / ".aa" / "data-knowledge.yaml").read_text(encoding="utf-8")
    loaded = YAML(typ="safe").load(text)

    assert outcome.written is True
    assert outcome.merged_keys == ("entities.user",)
    assert "# canonical domain knowledge" in text
    assert loaded["version"] == 1
    assert loaded["journeys"] == ["dept_management_crud"]
    assert loaded["entities"]["dept"]["required_fields"] == ["name"]
    assert loaded["entities"]["user"] == {"required_fields": ["username"]}
    assert "needs_review" not in text


def test_promote_writes_conflicts_and_leaves_l1_unchanged(tmp_path: Path) -> None:
    project = _project(tmp_path)
    before = (project / ".aa" / "data-knowledge.yaml").read_bytes()
    proposal = {"schema_version": "1", "mode": "delta", "entities": {"dept": {"required_fields": ["title"]}}}

    with pytest.raises(KnowledgePromoteError) as raised:
        promote_knowledge(project, proposal, yes=True)

    conflicts = json.loads((project / "qa/improvements/promote-conflicts.json").read_text(encoding="utf-8"))
    assert raised.value.code == 30
    assert conflicts[0]["key"] == "entities.dept"
    assert (project / ".aa" / "data-knowledge.yaml").read_bytes() == before


def test_promote_force_overwrites(tmp_path: Path) -> None:
    project = _project(tmp_path)
    proposal = {"schema_version": "1", "mode": "delta", "entities": {"dept": {"required_fields": ["title"]}}}
    outcome = promote_knowledge(project, proposal, yes=True, force=True)
    loaded = YAML(typ="safe").load((project / ".aa" / "data-knowledge.yaml").read_text(encoding="utf-8"))

    assert outcome.written is True
    assert loaded["entities"]["dept"]["required_fields"] == ["title"]
    assert not (project / "qa/improvements/promote-conflicts.json").exists()


def test_repeated_promote_is_a_noop(tmp_path: Path) -> None:
    project = _project(tmp_path)
    promote_knowledge(project, _proposal(), yes=True)
    second = promote_knowledge(project, _proposal(), yes=True)

    assert second.written is False
    assert second.merged_keys == ()


def test_load_promotable_delta_accepts_proposed(tmp_path: Path) -> None:
    project = _project(tmp_path)
    ledger = project / "qa" / "improvements" / "ledger.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "last_seq": 1,
                "improvements": {
                    "IMP-1": {
                        "improvement_id": "IMP-1",
                        "fingerprint": "f" * 64,
                        "kind": "domain_knowledge",
                        "delivery": "knowledge_delta",
                        "source_refs": {"problem_ids": ["PROB-1"]},
                        "target": ".aa/data-knowledge.yaml",
                        "rationale": "missing user factory",
                        "proposed_change": "add entities.user",
                        "knowledge_delta": {
                            "schema_version": "1",
                            "mode": "delta",
                            "entities": {"user": {"required_fields": ["username"]}},
                        },
                        "verification": {"success_criteria": "user leaf exists"},
                        "risk": "low",
                        "confidence": "high",
                        "state": "proposed",
                        "version": 1,
                        "proposed_by_retro_ids": ["retro-1"],
                        "last_event_id": "IMPEVT-1",
                    }
                },
                "by_fingerprint": {},
            }
        ),
        encoding="utf-8",
    )

    assert load_promotable_delta(project, "IMP-1")["entities"]["user"]["required_fields"] == ["username"]


def test_load_promotable_delta_rejects_a_rejected_item(tmp_path: Path) -> None:
    project = _project(tmp_path)
    ledger = project / "qa" / "improvements" / "ledger.json"
    ledger.parent.mkdir(parents=True)
    body = {
        "schema_version": "1",
        "last_seq": 1,
        "improvements": {
            "IMP-1": {
                "improvement_id": "IMP-1",
                "fingerprint": "f" * 64,
                "kind": "domain_knowledge",
                "delivery": "knowledge_delta",
                "source_refs": {"problem_ids": ["PROB-1"]},
                "target": ".aa/data-knowledge.yaml",
                "rationale": "missing user factory",
                "proposed_change": "add entities.user",
                "knowledge_delta": {
                    "schema_version": "1",
                    "mode": "delta",
                    "entities": {"user": {"required_fields": ["username"]}},
                },
                "verification": {"success_criteria": "user leaf exists"},
                "risk": "low",
                "confidence": "high",
                "state": "rejected",
                "version": 1,
                "proposed_by_retro_ids": ["retro-1"],
                "last_event_id": "IMPEVT-1",
            }
        },
        "by_fingerprint": {},
    }
    ledger.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(KnowledgePromoteError) as raised:
        load_promotable_delta(project, "IMP-1")
    assert raised.value.code == 40
