from __future__ import annotations

import json
from collections.abc import Mapping
from importlib.resources import files
from typing import cast

from agent_runtime_contracts import (
    AgentRunRequest,
    AgentWorkspaceV1,
    FrozenExecutionSelection,
    InstructionPart,
    ResultContract,
)
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import JSONValue

INSTRUCTIONS_RESOURCE_ID = "fixture.runtime.instructions"
RESULT_SCHEMA_RESOURCE_ID = "fixture.runtime.result-schema"
RUN_CAPABILITY_ID = "fixture.binding.run"


def package_resource_bytes() -> dict[str, bytes]:
    root = files("agent_runtime_fixture").joinpath("resources")
    return {
        INSTRUCTIONS_RESOURCE_ID: root.joinpath("instructions.txt").read_bytes(),
        RESULT_SCHEMA_RESOURCE_ID: root.joinpath("result-schema.json").read_bytes(),
    }


def fixture_resources() -> dict[str, bytes]:
    owned = package_resource_bytes()
    return {
        "fixture.instructions": owned[INSTRUCTIONS_RESOURCE_ID],
        "fixture.result-schema": owned[RESULT_SCHEMA_RESOURCE_ID],
    }


def fixture_config() -> dict[str, JSONValue]:
    return {
        "permissions": {"writes": ["result.json"]},
        "request_policy": {"delivery_mode": "assistant_json_local_v1"},
    }


def _result_contract(schema_bytes: bytes) -> ResultContract:
    schema = json.loads(schema_bytes.decode("utf-8"))
    if not isinstance(schema, dict):
        raise ValueError("fixture result schema must be a JSON object")
    return ResultContract(
        schema_id="fixture.result.v1",
        schema_digest=canonical_digest(cast(JSONValue, schema)),
        delivery_mode="assistant_json_local_v1",
        schema_document=schema,
    )


def _agent_workspace(config: Mapping[str, object]) -> AgentWorkspaceV1:
    permissions = config.get("permissions")
    writes = permissions.get("writes") if isinstance(permissions, Mapping) else None
    allowed = sorted(str(item) for item in writes) if isinstance(writes, list) else ["result.json"]
    payload = {
        "schema_version": "1",
        "agent_profile": "assurance-v1-doc-author",
        "scope_id": "CH-1",
        "write_root": "qa/changes/CH-1/.staging/attempt-1",
        "allowed_outputs": allowed,
        "read_roots": [],
    }
    return AgentWorkspaceV1.model_validate({**payload, "identity_digest": canonical_digest(payload)})


def assemble_request(resources: Mapping[str, bytes], config: JSONValue) -> AgentRunRequest:
    if not isinstance(config, Mapping):
        raise TypeError("fixture config must be a mapping")
    instruction = resources["fixture.instructions"].decode("utf-8")
    return AgentRunRequest(
        schema_version="1",
        instructions=(InstructionPart.text("text/plain", instruction),),
        result_contract=_result_contract(resources["fixture.result-schema"]),
        execution=FrozenExecutionSelection(
            provider_model="provider_default",
            worker_profile="fixture-v1",
            permission_profile_digest=canonical_digest(config["permissions"]),
            limits={"max_seconds": 120},  # type: ignore[arg-type]
        ),
        workspace=_agent_workspace(config),
        request_policy_digest=canonical_digest(config["request_policy"]),
        request_config_digest=canonical_digest(config),
    )


EXPECTED_AGENT_RUN_REQUEST_BYTES = assemble_request(fixture_resources(), fixture_config()).canonical_bytes()
