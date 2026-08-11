"""New FactBaseline artifacts may not invent a broad endpoint inventory."""

from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.artifacts.models import FactBaseline
from assurance_agent.workflow.graph.finalize import _validate_registry_outputs
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.verification.contract_render import render_output_contract


def _workspace(tmp_path: Path) -> TaskWorkspace:
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    return TaskWorkspace(
        task_id="fact-baseline-task",
        root=tmp_path,
        project_root=tmp_path,
        repo_root=tmp_path,
        change_dir=change_dir,
        base_tree_id="",
    )


def _payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "source": "seed_file",
        "seed_file": "app/core/init_app.py",
        "facts": {
            "route_prefix": "/api/v1",
            "login_endpoint": {"method": "POST", "path": "/api/v1/login"},
            "user_endpoints": [
                {"method": "PUT", "path": "/api/v1/user/update"},
            ],
        },
        "warnings": [],
    }


def test_new_fact_baseline_rejects_module_endpoint_inventory(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    output = workspace.change_dir / "facts" / "fact-baseline.json"
    output.parent.mkdir(parents=True)
    output.write_text(json.dumps(_payload()), encoding="utf-8")

    result = _validate_registry_outputs(
        workspace=workspace,
        outputs=("change:facts/fact-baseline.json",),
    )

    assert result is not None
    assert result.error_kind == "invalid_output"
    assert "endpoint inventories" in (result.error or "")
    assert "user_endpoints" in (result.error or "")


def test_unavailable_fact_baseline_cannot_hide_endpoint_inventory(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    output = workspace.change_dir / "facts" / "fact-baseline.json"
    output.parent.mkdir(parents=True)
    output.write_text(
        json.dumps(
            {
                "source": "unavailable",
                "facts": {"user_endpoints": [{"method": "PUT", "path": "/api/v1/user/update"}]},
                "warnings": ["seed file unavailable"],
            }
        ),
        encoding="utf-8",
    )

    result = _validate_registry_outputs(
        workspace=workspace,
        outputs=("change:facts/fact-baseline.json",),
    )

    assert result is not None
    assert result.error_kind == "invalid_output"
    assert "endpoint inventories" in (result.error or "")


def test_historical_fact_baseline_model_keeps_endpoint_inventory_compatibility() -> None:
    model = FactBaseline.model_validate(_payload())

    assert model.root.facts["user_endpoints"][0]["method"] == "PUT"


def test_fact_baseline_prompt_exposes_endpoint_inventory_boundary() -> None:
    contract = render_output_contract(("change:facts/fact-baseline.json",))

    assert "do not emit facts.endpoints or facts.*_endpoints inventories" in contract
