from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from agent_runtime_contracts import (
    AgentPhaseWriteClaims,
    AgentRunRequest,
    AgentRunResult,
    AgentWorkspaceV1,
    FrozenExecutionSelection,
    InstructionPart,
    ResultContract,
    rebind_agent_run_workspace,
)
from agent_runtime_contracts.schema import canonical_digest


_REPO_ROOT = Path(__file__).resolve().parents[4]
_PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "agent_runtime_contracts"
_ENGINE_ROOT = _REPO_ROOT / "packages" / "framework" / "graph-engine" / "graph_engine"
_SHA_A = "1" * 64
_SHA_B = "2" * 64
_SHA_C = "3" * 64
_SHA_D = "4" * 64
_ALLOWED_GRAPH_ENGINE_MODULES = (
    "graph_engine.plugin_api",
    "graph_engine.attempts",
    "graph_engine.attempts.contracts",
    "graph_engine.attempts.context",
    "graph_engine.attempts.keys",
    "graph_engine.identifiers",
)
_FORBIDDEN_GRAPH_ENGINE_PREFIXES = (
    "graph_engine.runtime",
    "graph_engine.composition",
    "graph_engine.graph",
    "graph_engine.canonical",
    "graph_engine.frozen_json",
    "graph_engine.errors",
)
_FORBIDDEN_PEER_PREFIXES = (
    "agent_runtime_opencode",
    "agent_runtime_cursor",
    "assurance_agent",
    "assurance_kernel",
)
_FORBIDDEN_RESULT_FIELDS = (
    "candidate_tree_id",
    "write_set_digest",
    "transcript",
    "model_history",
    "tokens",
    "cost",
    "events",
    "secret",
    "secrets",
)


def _selection(
    *,
    provider_model: str = "provider_default",
    **overrides: object,
) -> FrozenExecutionSelection:
    payload: dict[str, object] = {
        "provider_model": provider_model,
        "worker_profile": "fixture-v1",
        "permission_profile_digest": _SHA_B,
        "limits": {"max_seconds": 120},
    }
    payload.update(overrides)
    return FrozenExecutionSelection.model_validate(payload)


def _workspace(
    *,
    write_root: str = "qa/.staging/task-1/attempt-1",
    allowed_outputs: tuple[str, ...] = ("qa/proposal.md",),
    read_roots: tuple[str, ...] = (),
    agent_profile: str = "assurance-v1-doc-author",
) -> AgentWorkspaceV1:
    payload = {
        "schema_version": "1",
        "agent_profile": agent_profile,
        "scope_id": "CH-1",
        "write_root": write_root,
        "allowed_outputs": allowed_outputs,
        "read_roots": read_roots,
    }
    return AgentWorkspaceV1.model_validate({**payload, "identity_digest": canonical_digest(payload)})


def _request() -> AgentRunRequest:
    return AgentRunRequest(
        schema_version="1",
        instructions=(InstructionPart.text("text/plain", "write result.json"),),
        result_contract=ResultContract(
            schema_id="fixture.result.v1",
            schema_digest=_SHA_A,
            delivery_mode="assistant_json_local_v1",
        ),
        execution=_selection(),
        workspace=_workspace(),
        request_policy_digest=_SHA_C,
        request_config_digest=_SHA_D,
    )


def test_agent_run_request_is_strict_frozen_and_canonical() -> None:
    request = AgentRunRequest(
        schema_version="1",
        instructions=(InstructionPart.text("text/plain", "write result.json"),),
        result_contract=ResultContract(
            schema_id="fixture.result.v1",
            schema_digest="1" * 64,
            delivery_mode="assistant_json_local_v1",
        ),
        execution=FrozenExecutionSelection(
            provider_model="provider_default",
            worker_profile="fixture-v1",
            permission_profile_digest="2" * 64,
            limits={"max_seconds": 120},  # type: ignore[arg-type]
        ),
        workspace=_workspace(),
        request_policy_digest="3" * 64,
        request_config_digest="4" * 64,
    )
    assert AgentRunRequest.model_validate_json(request.canonical_bytes()).canonical_bytes() == (
        request.canonical_bytes()
    )
    with pytest.raises(ValidationError, match="extra"):
        AgentRunRequest.model_validate({**request.model_dump(), "fallback_model": "x"})


def test_agent_run_request_rejects_agent_profile() -> None:
    payload = _request().model_dump(mode="json")
    payload["agent_profile"] = "aa-doc-author"
    with pytest.raises(ValidationError, match="extra"):
        AgentRunRequest.model_validate(payload)


@pytest.mark.parametrize(
    "agent_profile",
    [
        "assurance-v1-doc-author",
        "assurance-v1-reviewer",
        "assurance-v1-executor",
    ],
)
def test_agent_workspace_accepts_closed_bounded_agent_profiles(agent_profile: str) -> None:
    workspace = _workspace(agent_profile=agent_profile)
    assert workspace.agent_profile == agent_profile
    expected = canonical_digest(workspace.model_dump(mode="json", exclude={"identity_digest"}))
    assert workspace.identity_digest == expected


def test_agent_workspace_authenticates_a_safe_opaque_scope_id() -> None:
    payload = _workspace().model_dump(mode="json")
    payload["scope_id"] = "CH-CURRENT-001"
    payload.pop("identity_digest")
    payload["identity_digest"] = canonical_digest(payload)

    workspace = AgentWorkspaceV1.model_validate(payload)

    assert workspace.scope_id == "CH-CURRENT-001"
    assert workspace.identity_digest == canonical_digest(
        workspace.model_dump(mode="json", exclude={"identity_digest"})
    )


@pytest.mark.parametrize("scope_id", ["", ".", "..", "scope/id", "scope\\id", "scope ", "scope.", "NUL"])
def test_agent_workspace_rejects_unsafe_scope_ids(scope_id: str) -> None:
    payload = _workspace().model_dump(mode="json")
    payload["scope_id"] = scope_id
    payload.pop("identity_digest")
    payload["identity_digest"] = canonical_digest(payload)

    with pytest.raises(ValidationError, match="scope"):
        AgentWorkspaceV1.model_validate(payload)


@pytest.mark.parametrize(
    "agent_profile",
    ["assurance-worker", "fixture-v1", "aa-doc-author", "assurance-v1-author", ""],
)
def test_agent_workspace_rejects_unbounded_agent_profiles(agent_profile: str) -> None:
    payload = _workspace().model_dump(mode="json")
    payload["agent_profile"] = agent_profile
    payload.pop("identity_digest")
    payload["identity_digest"] = canonical_digest(payload)
    with pytest.raises(ValidationError):
        AgentWorkspaceV1.model_validate(payload)


def test_agent_run_request_rejects_mutation() -> None:
    request = _request()
    with pytest.raises(ValidationError, match="frozen"):
        request.schema_version = "2"  # type: ignore[misc]


def test_agent_workspace_accepts_canonical_relative_write_root_and_sorted_outputs() -> None:
    workspace = _workspace(
        write_root="qa/.staging/task-1/attempt-1",
        allowed_outputs=("qa/proposal.md", "qa/results/cases.yaml"),
    )
    assert workspace.schema_version == "1"
    assert workspace.write_root == "qa/.staging/task-1/attempt-1"
    assert workspace.allowed_outputs == (
        "qa/proposal.md",
        "qa/results/cases.yaml",
    )
    expected = canonical_digest(workspace.model_dump(mode="json", exclude={"identity_digest"}))
    assert workspace.identity_digest == expected
    again = AgentWorkspaceV1.model_validate(workspace.model_dump(mode="json"))
    assert again.identity_digest == workspace.identity_digest
    request = _request()
    assert request.workspace.write_root == workspace.write_root
    with pytest.raises(ValidationError, match="frozen"):
        request.workspace.write_root = "other"  # type: ignore[misc]


def test_agent_workspace_authenticates_read_roots_and_rebinds_attempt_local_roots(
    tmp_path: Path,
) -> None:
    old_write_root = "qa/.staging/task-1/attempt-1"
    old_view = f"{old_write_root}/qa/.staging/execution/batch-1"
    original = _request().model_copy(
        update={"workspace": _workspace(agent_profile="assurance-v1-executor", read_roots=(old_view,))}
    )
    project = tmp_path / "project"
    new_write_root = project / "qa/.staging/task-2/attempt-1"
    new_write_root.mkdir(parents=True)

    effective = rebind_agent_run_workspace(
        original,
        project_root=project,
        write_root=new_write_root,
    )

    assert original.workspace.read_roots == (old_view,)
    assert effective.workspace.read_roots == ("qa/.staging/task-2/attempt-1/qa/.staging/execution/batch-1",)
    assert effective.workspace.identity_digest == canonical_digest(
        effective.workspace.model_dump(mode="json", exclude={"identity_digest"})
    )


@pytest.mark.parametrize(
    "read_roots",
    [
        ("/tmp/view",),
        ("../view",),
        ("qa/../view",),
        ("qa/results/view-b", "qa/results/view-a"),
        ("qa/results/view-a", "qa/results/view-a"),
    ],
)
def test_agent_workspace_rejects_noncanonical_read_roots(
    read_roots: tuple[str, ...],
) -> None:
    payload = _workspace().model_dump(mode="json")
    payload["read_roots"] = list(read_roots)
    payload.pop("identity_digest")
    payload["identity_digest"] = canonical_digest(payload)

    with pytest.raises(ValidationError):
        AgentWorkspaceV1.model_validate(payload)


@pytest.mark.parametrize(
    "write_root",
    [
        "/tmp/stage",
        "C:/stage",
        "../escape",
        "qa/../secret",
        "qa\\changes\\stage",
        "/qa/results/stage",
        "",
        ".",
        "..",
    ],
)
def test_agent_workspace_rejects_absolute_or_parent_write_roots(write_root: str) -> None:
    payload = _workspace().model_dump(mode="json")
    payload["write_root"] = write_root
    payload.pop("identity_digest")
    payload["identity_digest"] = canonical_digest(payload)
    with pytest.raises(ValidationError):
        AgentWorkspaceV1.model_validate(payload)


@pytest.mark.parametrize(
    "allowed_outputs",
    [
        ("/tmp/out.md",),
        ("../escape.md",),
        ("qa/../secret.md",),
        ("qa\\changes\\proposal.md",),
        ("qa/results/proposal.md", "qa/results/cases.yaml"),
        ("qa/results/proposal.md", "qa/results/proposal.md"),
    ],
)
def test_agent_workspace_rejects_unsorted_absolute_or_parent_outputs(
    allowed_outputs: tuple[str, ...],
) -> None:
    payload = _workspace().model_dump(mode="json")
    payload["allowed_outputs"] = list(allowed_outputs)
    payload.pop("identity_digest")
    payload["identity_digest"] = canonical_digest(payload)
    with pytest.raises(ValidationError):
        AgentWorkspaceV1.model_validate(payload)


def test_agent_workspace_identity_digest_is_stable_and_authenticated() -> None:
    first = _workspace()
    second = _workspace()
    assert first.identity_digest == second.identity_digest
    drifted = first.model_dump(mode="json")
    drifted["identity_digest"] = _SHA_A
    with pytest.raises(ValidationError, match="canonical"):
        AgentWorkspaceV1.model_validate(drifted)
    different = _workspace(write_root="qa/.staging/task-1/attempt-2")
    assert different.identity_digest != first.identity_digest


def test_rebind_agent_run_workspace_uses_current_stage_and_preserves_authority(
    tmp_path: Path,
) -> None:
    original = _request()
    project_root = tmp_path / "project"
    write_root = project_root / "qa/.staging/execute-task/attempt-1"
    write_root.mkdir(parents=True)

    effective = rebind_agent_run_workspace(
        original,
        project_root=project_root,
        write_root=write_root,
    )

    assert original.workspace.write_root == "qa/.staging/task-1/attempt-1"
    assert effective.workspace.write_root == "qa/.staging/execute-task/attempt-1"
    assert effective.workspace.agent_profile == original.workspace.agent_profile
    assert effective.workspace.scope_id == original.workspace.scope_id
    assert effective.workspace.allowed_outputs == original.workspace.allowed_outputs
    assert effective.workspace.identity_digest == canonical_digest(
        effective.workspace.model_dump(mode="json", exclude={"identity_digest"})
    )
    assert effective.model_dump(mode="json", exclude={"workspace"}) == original.model_dump(
        mode="json", exclude={"workspace"}
    )


def test_rebind_agent_run_workspace_rejects_stage_outside_project(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValueError, match="inside project_root"):
        rebind_agent_run_workspace(
            _request(),
            project_root=project_root,
            write_root=outside,
        )


def test_instruction_part_permits_exactly_one_text_or_json_form() -> None:
    text_part = InstructionPart.text("text/plain", "write result.json")
    assert text_part.media_type == "text/plain"
    assert text_part.text_content == "write result.json"
    assert text_part.json_content is None
    assert text_part.digest == canonical_digest("write result.json")

    json_part = InstructionPart.from_json({"task": "write result.json"})
    assert json_part.media_type == "application/json"
    assert json_part.text_content is None
    assert json_part.json_content == {"task": "write result.json"}
    assert json_part.digest == canonical_digest({"task": "write result.json"})

    with pytest.raises(ValidationError, match="exactly one"):
        InstructionPart(media_type="text/plain", digest=canonical_digest("x"))
    with pytest.raises(ValidationError, match="exactly one"):
        InstructionPart(
            media_type="text/plain",
            text_content="hello",
            json_content={"x": 1},
            digest=canonical_digest("hello"),
        )


def test_instruction_part_rejects_unknown_media_type_and_bad_digest() -> None:
    with pytest.raises(ValidationError):
        InstructionPart.text("text/markdown", "write result.json")
    with pytest.raises(ValidationError, match="canonical"):
        InstructionPart(
            media_type="text/plain",
            text_content="write result.json",
            digest=_SHA_A,
        )


def test_frozen_execution_selection_accepts_exact_or_provider_default() -> None:
    defaulted = _selection()
    assert defaulted.provider_model == "provider_default"
    exact = _selection(provider_model="openai/gpt-4.1")
    assert exact.provider_model == "openai/gpt-4.1"


@pytest.mark.parametrize(
    "payload",
    [
        {"candidates": ["openai/gpt-4.1", "anthropic/claude"]},
        {"fallback_model": "openai/gpt-4.1"},
        {"fallbacks": ["openai/gpt-4.1"]},
        {"routing": "cheapest"},
        {"routing_expression": "cost < 1"},
    ],
)
def test_frozen_execution_selection_rejects_candidates_routing_and_fallbacks(
    payload: dict[str, object],
) -> None:
    body = _selection().model_dump()
    body.update(payload)
    with pytest.raises(ValidationError, match="extra"):
        FrozenExecutionSelection.model_validate(body)


@pytest.mark.parametrize(
    "provider_model",
    [
        "openai/gpt-4.1,anthropic/claude",
        "openai/gpt-4.1|anthropic/claude",
        "route:cheapest",
        "fallback:openai/gpt-4.1",
        ["openai/gpt-4.1", "anthropic/claude"],
    ],
)
def test_frozen_execution_selection_rejects_non_exact_provider_models(
    provider_model: object,
) -> None:
    with pytest.raises(ValidationError):
        _selection(provider_model=provider_model)  # type: ignore[arg-type]


def test_frozen_execution_selection_rejects_unknown_limit_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        _selection(limits={"max_seconds": 120, "max_cost": 1})


def test_result_contract_extraction_mode_is_closed() -> None:
    with pytest.raises(ValidationError):
        ResultContract.model_validate(
            {
                "schema_id": "fixture.result.v1",
                "schema_digest": _SHA_A,
                "delivery_mode": "transcript",
            }
        )


def test_agent_run_result_authenticates_digests_and_forbids_provider_payloads() -> None:
    structured = {"status": "ok", "artifact": "result.json"}
    result = AgentRunResult.model_validate(
        {
            "result_payload": structured,
            "result_digest": canonical_digest(structured),
            "evidence_digest": _SHA_B,
            "provider_diff_digest": _SHA_C,
            "adapter_id": "agent-runtime-fixture",
            "adapter_version": "1.0.0",
            "diagnostics": ("idle",),
        }
    )
    assert result.schema_version == "1"
    assert AgentRunResult.model_validate_json(result.canonical_bytes()).canonical_bytes() == (
        result.canonical_bytes()
    )
    with pytest.raises(ValidationError, match="canonical"):
        AgentRunResult.model_validate(
            {
                "result_payload": structured,
                "result_digest": _SHA_A,
                "evidence_digest": _SHA_B,
                "adapter_id": "agent-runtime-fixture",
                "adapter_version": "1.0.0",
            }
        )
    dumped = result.model_dump()
    for field_name in _FORBIDDEN_RESULT_FIELDS:
        with pytest.raises(ValidationError, match="extra"):
            AgentRunResult.model_validate({**dumped, field_name: "leak"})
    # Service-secret rejection belongs to the producer with the actual secret
    # context; the envelope must not guess from business-result string shapes.
    business = {"status": "ok", "token": "sk-source-grounded-business-value"}
    governed = AgentRunResult.model_validate(
        {
            "result_payload": business,
            "result_digest": canonical_digest(business),
            "evidence_digest": _SHA_B,
            "adapter_id": "agent-runtime-fixture",
            "adapter_version": "1.0.0",
        }
    )
    assert governed.model_dump(mode="json")["result_payload"] == business


def test_agent_run_result_bounds_and_redacts_diagnostics() -> None:
    structured = {"status": "ok"}
    result = AgentRunResult.model_validate(
        {
            "result_payload": structured,
            "result_digest": canonical_digest(structured),
            "evidence_digest": _SHA_B,
            "adapter_id": "agent-runtime-fixture",
            "adapter_version": "1.0.0",
            "diagnostics": (
                "Authorization: Bearer sk-secret-canary",
                "x" * 512,
            ),
        }
    )
    assert "sk-secret-canary" not in result.diagnostics[0]
    assert "[redacted]" in result.diagnostics[0]
    assert len(result.diagnostics[1]) <= 240
    overflow = tuple(f"note-{index}" for index in range(17))
    with pytest.raises(ValidationError, match="bound"):
        AgentRunResult.model_validate(
            {
                "result_payload": structured,
                "result_digest": canonical_digest(structured),
                "evidence_digest": _SHA_B,
                "adapter_id": "agent-runtime-fixture",
                "adapter_version": "1.0.0",
                "diagnostics": overflow,
            }
        )


def _imported_modules(tree: ast.AST) -> tuple[str, ...]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return tuple(names)


def test_contracts_source_imports_only_plugin_api_primitives() -> None:
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        imported = _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
        for module_name in imported:
            assert not module_name.startswith(_FORBIDDEN_PEER_PREFIXES), (path, module_name)
            assert not module_name.startswith(_FORBIDDEN_GRAPH_ENGINE_PREFIXES), (path, module_name)
            if module_name == "graph_engine" or module_name.startswith("graph_engine."):
                assert module_name in _ALLOWED_GRAPH_ENGINE_MODULES, (path, module_name)


def test_graph_engine_does_not_import_agent_runtime_contracts() -> None:
    for path in sorted(_ENGINE_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "agent_runtime_contracts" not in text, path


def test_isolated_wheel_import_does_not_load_adapters_or_assurance(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    venv = tmp_path / "venv"
    dist.mkdir()
    for package in ("graph-engine", "agent-runtime-contracts"):
        subprocess.run(
            [
                "uv",
                "build",
                "--offline",
                "--wheel",
                "--package",
                package,
                "--out-dir",
                str(dist),
            ],
            cwd=_REPO_ROOT,
            check=True,
        )
    engine_wheel = next(dist.glob("graph_engine-*.whl"))
    contracts_wheel = next(dist.glob("agent_runtime_contracts-*.whl"))
    subprocess.run(["uv", "venv", "--offline", "--python", "3.11", str(venv)], check=True)
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--python",
            str(venv / "bin" / "python"),
            "--no-deps",
            str(engine_wheel),
            str(contracts_wheel),
        ],
        check=True,
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--python",
            str(venv / "bin" / "python"),
            "pydantic",
            "packaging",
            "pyyaml",
            "langgraph",
        ],
        check=True,
    )
    script = r"""
import importlib.util
import sys

import agent_runtime_contracts
from agent_runtime_contracts import AgentRunRequest
from graph_engine.plugin_api import FrozenModel

assert issubclass(AgentRunRequest, FrozenModel)
for package in (
    "agent_runtime_opencode",
    "agent_runtime_cursor",
    "assurance_agent",
    "assurance_kernel",
):
    assert importlib.util.find_spec(package) is None, package
    assert not any(name == package or name.startswith(package + ".") for name in sys.modules), package
"""
    completed = subprocess.run(
        [str(venv / "bin" / "python"), "-c", script],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert "graph_engine.plugin_api" not in completed.stderr


def test_agent_result_and_finalize_output_are_distinct_contracts() -> None:
    from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy
    from graph_engine.plugin_api import ResourceClaims

    from agent_runtime_contracts import AgentExecutionContract

    class CaseDesignInput(BaseModel):
        change_id: str

    class CaseDesignAgentResult(BaseModel):
        output_files: tuple[str, ...]

    class CaseDesignOutput(BaseModel):
        status: str

    claims = ResourceClaims()
    retry = AttemptRetryPolicy(max_attempts=1)
    timeout = AttemptTimeoutPolicy(seconds=60)
    contract = AgentExecutionContract(
        contract_id="assurance.intake.agent.case-design.v1",
        owner_id="assurance.intake",
        prepare_handler_id="assurance.intake.case-design.prepare",
        finalize_handler_id="assurance.intake.case-design.finalize",
        skill_id="aa-case-design",
        agent_profile="assurance-v1-doc-author",
        input_model=CaseDesignInput,
        agent_result_model=CaseDesignAgentResult,
        output_model=CaseDesignOutput,
        resources=claims,
        retry=retry,
        timeout=timeout,
        validators=(),
        phase_write_claims=AgentPhaseWriteClaims(prepare=(), runtime=(), finalize=()),
    )
    assert contract.agent_result_model is not contract.output_model


def _local_agent_contract():
    from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy
    from graph_engine.plugin_api import ResourceClaims

    from agent_runtime_contracts import AgentExecutionContract

    class IntakeInput(BaseModel):
        change_id: str

    class IntakeAgentResult(BaseModel):
        output_files: tuple[str, ...]
        note: str = "omitted"

    class IntakeOutput(BaseModel):
        status: str

    return AgentExecutionContract(
        contract_id="assurance.intake.agent.intake.v1",
        owner_id="assurance.intake",
        prepare_handler_id="assurance.intake.intake.prepare",
        finalize_handler_id="assurance.intake.intake.finalize",
        skill_id="aa-intake",
        agent_profile="assurance-v1-doc-author",
        input_model=IntakeInput,
        agent_result_model=IntakeAgentResult,
        output_model=IntakeOutput,
        resources=ResourceClaims(),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=60),
        validators=(),
        phase_write_claims=AgentPhaseWriteClaims(prepare=(), runtime=(), finalize=()),
    )


def test_agent_contract_exposes_local_result_schema_independent_of_capabilities() -> None:
    from agent_runtime_contracts.runtime_binding import AgentRuntimeCapabilities

    contract = _local_agent_contract()
    schema = contract.agent_result_schema_document()
    digest = contract.agent_result_schema_digest()

    assert schema == contract.agent_result_model.model_json_schema()
    assert digest == canonical_digest(schema)
    assert not hasattr(contract, "requires_provider_schema")
    capabilities = AgentRuntimeCapabilities.model_validate({})
    assert "provider_schema" not in capabilities.model_dump()
    assert "provider_schema" not in str(contract.canonical_projection()).lower()


def test_agent_execution_contract_projection_is_raw_agent_contract_v1() -> None:
    contract = _local_agent_contract()
    projection = contract.canonical_projection()

    assert projection["schema_version"] == "raw-agent-contract-v1"
    assert projection["agent_result_schema_digest"] == canonical_digest(
        contract.agent_result_model.model_json_schema()
    )
    assert projection["phase_write_claims"] == {"prepare": [], "runtime": [], "finalize": []}
    assert "requires_provider_schema" not in projection
    assert "provider_schema" not in projection


def test_agent_result_validates_and_digests_exact_payload_before_pydantic() -> None:
    from agent_runtime_contracts.schema import validate_local_agent_result

    contract = _local_agent_contract()
    payload = {"output_files": ["qa/proposal.md"]}
    exact, digest, validated = validate_local_agent_result(
        payload,
        result_model=contract.agent_result_model,
    )

    assert exact == payload
    assert digest == canonical_digest(payload)
    assert digest != canonical_digest(validated.model_dump(mode="json"))
    assert validated.note == "omitted"


def test_agent_result_validation_keeps_feature_owned_context() -> None:
    from typing import Self

    from pydantic import ValidationInfo, model_validator

    from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy
    from graph_engine.plugin_api import ResourceClaims

    from agent_runtime_contracts import AgentExecutionContract
    from agent_runtime_contracts.schema import validate_local_agent_result

    class LeafInput(BaseModel):
        change_id: str

    class LeafAwareResult(BaseModel):
        leaf: str

        @model_validator(mode="after")
        def _require_declared_leaf(self, info: ValidationInfo) -> Self:
            context = info.context or {}
            leafs = context.get("capability_leafs")
            if not isinstance(leafs, frozenset) or self.leaf not in leafs:
                raise ValueError("unknown capability leaf")
            return self

    class LeafOutput(BaseModel):
        status: str

    contract = AgentExecutionContract(
        contract_id="assurance.generation.agent.codegen.v1",
        owner_id="assurance.generation",
        prepare_handler_id="assurance.generation.codegen.prepare",
        finalize_handler_id="assurance.generation.codegen.finalize",
        skill_id="aa-codegen",
        agent_profile="assurance-v1-executor",
        input_model=LeafInput,
        agent_result_model=LeafAwareResult,
        output_model=LeafOutput,
        resources=ResourceClaims(),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=60),
        validators=(),
        phase_write_claims=AgentPhaseWriteClaims(prepare=(), runtime=(), finalize=()),
    )
    payload = {"leaf": "api"}
    with pytest.raises(ValidationError, match="capability leaf"):
        validate_local_agent_result(payload, result_model=contract.agent_result_model)
    exact, digest, validated = validate_local_agent_result(
        payload,
        result_model=contract.agent_result_model,
        context={"capability_leafs": frozenset({"api"})},
    )
    assert exact == payload
    assert digest == canonical_digest(payload)
    assert validated.leaf == "api"


def test_result_contract_delivery_mode_is_assistant_json_local_v1() -> None:
    schema = {
        "additionalProperties": False,
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "type": "object",
    }
    contract = ResultContract(
        schema_id="fixture.result.v1",
        schema_digest=canonical_digest(schema),
        delivery_mode="assistant_json_local_v1",
        schema_document=schema,
    )
    assert contract.delivery_mode == "assistant_json_local_v1"
    assert not hasattr(contract, "extraction_mode")
    with pytest.raises(ValidationError):
        ResultContract.model_validate(
            {
                "schema_id": "fixture.result.v1",
                "schema_digest": canonical_digest(schema),
                "delivery_mode": "structured",
            }
        )
    with pytest.raises(ValidationError, match="extra"):
        ResultContract.model_validate(
            {
                "schema_id": "fixture.result.v1",
                "schema_digest": canonical_digest(schema),
                "delivery_mode": "assistant_json_local_v1",
                "extraction_mode": "structured",
            }
        )


def test_agent_run_result_uses_result_payload() -> None:
    payload = {"status": "ok", "artifact": "result.json"}
    result = AgentRunResult.model_validate(
        {
            "result_payload": payload,
            "result_digest": canonical_digest(payload),
            "evidence_digest": _SHA_B,
            "adapter_id": "agent-runtime-fixture",
            "adapter_version": "1.0.0",
        }
    )
    assert result.result_payload == payload
    assert result.result_digest == canonical_digest(payload)
    assert not hasattr(result, "structured_result")
    dumped = result.model_dump()
    assert "structured_result" not in dumped
    with pytest.raises(ValidationError, match="extra"):
        AgentRunResult.model_validate({**dumped, "structured_result": payload})


def test_agent_execution_contract_is_provider_neutral() -> None:
    from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy
    from graph_engine.plugin_api import ResourceClaimTemplate

    from agent_runtime_contracts import AgentExecutionContract

    class IntakeInput(BaseModel):
        change_id: str

    class IntakeAgentResult(BaseModel):
        output_files: tuple[str, ...]

    class IntakeOutput(BaseModel):
        status: str

    contract = AgentExecutionContract(
        contract_id="assurance.intake.agent.intake.v1",
        owner_id="assurance.intake",
        prepare_handler_id="assurance.intake.intake.prepare",
        finalize_handler_id="assurance.intake.intake.finalize",
        skill_id="aa-intake",
        agent_profile="assurance-v1-doc-author",
        input_model=IntakeInput,
        agent_result_model=IntakeAgentResult,
        output_model=IntakeOutput,
        resources=ResourceClaimTemplate(
            reads=("qa",),
            writes=("qa/requirement.md",),
        ),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=60),
        validators=(),
        phase_write_claims=AgentPhaseWriteClaims(
            prepare=(),
            runtime=("qa/requirement.md",),
            finalize=(),
        ),
    )
    dumped = str(contract.canonical_projection()).lower()
    assert contract.contract_id == "assurance.intake.agent.intake.v1"
    assert "opencode" not in dumped
    assert "cursor" not in dumped
