from __future__ import annotations

import json
from importlib.resources import files

import pytest


def test_status_schema_is_closed_status_v1() -> None:
    raw = files("assurance_product").joinpath("resources/schemas/status-v1.json").read_bytes()
    schema = json.loads(raw.decode("utf-8"))
    assert schema["title"] == "StatusV1"
    assert schema["additionalProperties"] is False
    required = schema["required"]
    assert isinstance(required, list)
    assert {
        "invocation_id",
        "lock_digest",
        "entrypoint",
        "change",
        "status",
        "execution_gate",
        "quality_gate",
    } <= set(required)
    execution_gate = schema["$defs"]["ExecutionGateRefV1"]
    assert execution_gate["properties"]["semantic_node_id"]["enum"] == [
        "execution.execute",
        "execution.run",
    ]
    quality_gate = schema["$defs"]["QualityGateRefV1"]
    assert quality_gate["properties"]["inspection"] == {"$ref": "#/$defs/InspectionOutcomeV1"}
    assert quality_gate["properties"]["report"] == {
        "anyOf": [{"$ref": "#/$defs/ReportOutcomeV1"}, {"type": "null"}]
    }
    assert set(quality_gate["required"]) == {"inspection"}


def test_render_status_projects_started_invocation(cli_runner, installed_sources, tmp_path, monkeypatch):
    del cli_runner, installed_sources, tmp_path, monkeypatch
    pytest.skip("leftover Engine status projection was retired")
