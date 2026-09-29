"""Operator promotion of a knowledge delta into `.aa/data-knowledge.yaml`."""

from __future__ import annotations

import json
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from assurance_improvement.contracts.improvements import DeliveryKind, ImprovementLedgerProjection
from assurance_improvement.contracts.knowledge import PersistedDataKnowledgeProposal
from assurance_improvement.operations.knowledge_merge import (
    PromoteConflict,
    collect_leaf_entries,
    merge_l2_into_l1,
)

L1_REL = ".aa/data-knowledge.yaml"
CONFLICTS_REL = "qa/improvements/promote-conflicts.json"
_PROMOTABLE_STATES = frozenset({"proposed", "approved", "exported", "applied"})


class KnowledgePromoteError(Exception):
    def __init__(self, message: str, *, code: int = 40, outcome: PromoteOutcome | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.outcome = outcome


@dataclass(frozen=True)
class PromoteOutcome:
    written: bool
    merged_keys: tuple[str, ...]
    conflicts_path: str | None
    conflicts: tuple[PromoteConflict, ...]


def _round_trip_yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    return yaml


def _to_plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    return value


def _dump_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    model = PersistedDataKnowledgeProposal.model_validate(proposal)
    dumped = model.model_dump(mode="python", by_alias=True, exclude_none=True)
    if not isinstance(dumped, dict):
        raise KnowledgePromoteError("knowledge delta must be a mapping")
    return dumped


def load_proposal_file(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise KnowledgePromoteError(f"proposal file is missing: {path}")
    loaded = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise KnowledgePromoteError(f"proposal must be a mapping: {path}")
    try:
        return _dump_proposal(loaded)
    except KnowledgePromoteError:
        raise
    except Exception as error:
        raise KnowledgePromoteError(f"invalid proposal {path}: {error}") from error


def load_promotable_delta(project_root: Path, improvement_id: str) -> dict[str, Any]:
    ledger_path = project_root / "qa" / "improvements" / "ledger.json"
    if not ledger_path.is_file() or ledger_path.is_symlink():
        raise KnowledgePromoteError("qa/improvements/ledger.json is missing")
    try:
        ledger = ImprovementLedgerProjection.model_validate_json(ledger_path.read_text(encoding="utf-8"))
    except Exception as error:
        raise KnowledgePromoteError(f"invalid ledger: {error}") from error
    item = ledger.improvements.get(improvement_id)
    if item is None:
        raise KnowledgePromoteError(f"improvement {improvement_id} is not in the ledger")
    if item.delivery is not DeliveryKind.KNOWLEDGE_DELTA or item.knowledge_delta is None:
        raise KnowledgePromoteError(f"improvement {improvement_id} has no knowledge delta")
    if item.target != L1_REL:
        raise KnowledgePromoteError(f"improvement {improvement_id} target must be {L1_REL}")
    if item.state.value not in _PROMOTABLE_STATES:
        raise KnowledgePromoteError(f"improvement {improvement_id} is {item.state.value}")
    return _dump_proposal(item.knowledge_delta.model_dump(mode="python"))


def _apply_leaf(document: Any, dotted: str, leaf: dict[str, Any]) -> None:
    parts = dotted.split(".")
    cursor = document
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = CommentedMap()
            cursor[part] = nxt
        cursor = nxt
    cursor[parts[-1]] = CommentedMap(_to_plain(leaf))


def _write_conflicts(path: Path, conflicts: tuple[PromoteConflict, ...]) -> None:
    payload = [
        {"key": item.key, "l1_value": item.l1_value, "proposal_value": item.proposal_value}
        for item in conflicts
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def promote_knowledge(
    project_root: Path,
    proposal: dict[str, Any],
    *,
    yes: bool = False,
    force: bool = False,
) -> PromoteOutcome:
    l1_path = project_root / L1_REL
    if l1_path.is_symlink() or not l1_path.is_file():
        raise KnowledgePromoteError(f"{L1_REL} is missing")
    document = _round_trip_yaml().load(l1_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise KnowledgePromoteError(f"{L1_REL} must contain a mapping")
    try:
        dumped = _dump_proposal(proposal)
    except KnowledgePromoteError:
        raise
    except Exception as error:
        raise KnowledgePromoteError(f"invalid knowledge delta: {error}") from error
    result = merge_l2_into_l1(_to_plain(document), dumped, force=force)
    conflicts_path = project_root / CONFLICTS_REL
    outcome = PromoteOutcome(
        written=False,
        merged_keys=tuple(result.merged_keys),
        conflicts_path=CONFLICTS_REL if result.conflicts else None,
        conflicts=tuple(result.conflicts),
    )
    if result.conflicts and not force:
        _write_conflicts(conflicts_path, outcome.conflicts)
        raise KnowledgePromoteError(
            f"{len(result.conflicts)} conflict(s) written to {CONFLICTS_REL}; re-run with --force",
            code=30,
            outcome=outcome,
        )
    if conflicts_path.is_file() and not result.conflicts:
        conflicts_path.unlink()
    if not result.changed:
        return outcome
    if not yes:
        raise KnowledgePromoteError(
            "promotion would modify L1; re-run with --yes",
            code=30,
            outcome=outcome,
        )
    leaves = collect_leaf_entries(result.merged)
    for key in result.merged_keys:
        _apply_leaf(document, key, leaves[key])
    buffer = StringIO()
    _round_trip_yaml().dump(document, buffer)
    l1_path.write_text(buffer.getvalue(), encoding="utf-8")
    return PromoteOutcome(
        written=True,
        merged_keys=outcome.merged_keys,
        conflicts_path=None,
        conflicts=(),
    )
