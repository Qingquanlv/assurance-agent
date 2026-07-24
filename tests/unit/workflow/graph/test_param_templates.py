from pathlib import Path

import pytest

from assurance_agent.workflow.graph.models import RuntimeContext
from assurance_agent.workflow.graph.planner import PlanError, _resolve_template


def _ctx(tmp_path: Path, **params: object) -> RuntimeContext:
    return RuntimeContext(
        project_root=tmp_path,
        repo_root=tmp_path,
        change_dir=tmp_path / "change",
        change_id="CH-1",
        params=params,
    )


def test_resolve_template_expands_params_str(tmp_path: Path) -> None:
    out = _resolve_template(
        "project:qa/retro/${params.retro_id}/context.json",
        item_as="item",
        item=None,
        context=_ctx(tmp_path, retro_id="retro-20260725-120000"),
        nid="collect",
        path=True,
    )
    assert out == "project:qa/retro/retro-20260725-120000/context.json"


def test_resolve_template_rejects_empty_params_str(tmp_path: Path) -> None:
    with pytest.raises(PlanError, match="params.retro_id"):
        _resolve_template(
            "project:qa/retro/${params.retro_id}/context.json",
            item_as="item",
            item=None,
            context=_ctx(tmp_path, retro_id=""),
            nid="collect",
            path=True,
        )


def test_resolve_template_rejects_unsafe_params_segment(tmp_path: Path) -> None:
    with pytest.raises(PlanError):
        _resolve_template(
            "project:qa/retro/${params.retro_id}/x.json",
            item_as="item",
            item=None,
            context=_ctx(tmp_path, retro_id="../evil"),
            nid="collect",
            path=True,
        )
