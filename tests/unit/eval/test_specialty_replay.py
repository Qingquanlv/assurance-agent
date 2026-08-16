"""specialty_replay consumes FrozenDefinitionBinding without name rediscovery."""

from __future__ import annotations

import ast
from pathlib import Path

from benchmark.specialty import specialty_replay as specialty_replay_mod
from benchmark.specialty.specialty_replay import collect_capability_policy_replay
from tests.unit.workflow.graph.test_replay_binding import (
    _CHANGE_ID,
    _ENTRYPOINT,
    _ROOT_INV,
    _build_fixture,
)


def test_specialty_replay_has_no_second_evaluate_layer_selection() -> None:
    source = Path(specialty_replay_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "evaluate_layer_selection")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "evaluate_layer_selection")
        )
    ]
    assert calls == []


def test_collect_capability_policy_replay_uses_binding_selected_layers(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, include_e2e=False)
    replay = collect_capability_policy_replay(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    by_layer = {row.layer: row for row in replay.rows}
    assert by_layer["api"].status in {"complete", "incomplete", "not_wired"}
    assert by_layer["e2e"].status in {"not_selected", "incomplete", "not_wired", "complete"}
    assert replay.definition_binding is not None
    assert set(by_layer) == {"api", "e2e", "fuzz", "performance"}
