from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes

from assurance_healing.contracts import (
    FixProposal,
    HealingOverrideTokenV1,
    HealingStatusV1,
    SafetyCheck,
    TestChangePolicyV1,
)
from assurance_healing.plugin import HealingPlugin
from tests.capabilities.import_boundary_exceptions import is_declared_cross_wheel_import

_CURRENT_HEALING_SCHEMA_MAPPING: dict[str, tuple[str, str]] = {
    "assurance.healing.schema.fix-proposal.v1": (
        "1",
        "45baef416a046697f7c9f7634c16f42a2fd765429a44be8e816119d7567020e4",
    ),
    "assurance.healing.schema.healing-safety.v1": (
        "1",
        "de2609cbef7eff4f3fb644b21bfd688e6dbc5819618a14bf0bdb07ac1176ec47",
    ),
    "assurance.healing.schema.healing-status.v1": (
        "1",
        "6b7c39e35175fff1918929cbd51076b55d29173125905a95f9e04b6b688d9f27",
    ),
}

_TESTS_ROOT = Path(__file__).resolve().parent
_WHEEL_ROOT = _TESTS_ROOT.parent
_LEGACY_ROOTS = ("assurance_agent", "assurance_kernel")
_ALLOWED_ASSURANCE = (
    "assurance_intake.contracts",
    "assurance_intake.domain",
    "assurance_generation.contracts",
    "assurance_execution.contracts",
)
_HEX_A = "a" * 64
_HEX_B = "b" * 64
_HEX_C = "c" * 64
_HEX_D = "d" * 64
_HEX_E = "e" * 64


def forged_override_token(*, policy_digest: str, candidate_digest: str) -> dict[str, object]:
    return {
        "schema_version": "1",
        "change_id": "CH-DEMO-001",
        "action": "allow_test_changes",
        "reason": "forged",
        "policy_digest": policy_digest,
        "candidate_digest": candidate_digest,
        "token_digest": "f" * 64,
    }


def valid_override_token(
    *,
    policy_digest: str = _HEX_A,
    candidate_digest: str = _HEX_B,
) -> dict[str, object]:
    from assurance_healing.contracts.safety import override_token_digest

    return {
        "schema_version": "1",
        "change_id": "CH-DEMO-001",
        "action": "allow_test_changes",
        "reason": "approved test change",
        "policy_digest": policy_digest,
        "candidate_digest": candidate_digest,
        "token_digest": override_token_digest(
            change_id="CH-DEMO-001",
            policy_digest=policy_digest,
            candidate_digest=candidate_digest,
        ),
    }


def valid_policy() -> dict[str, object]:
    return {
        "allowed_test_roots": ["tests"],
        "forbidden_product_roots": ["app", "src", "web/src"],
        "max_files": 16,
        "require_approval": True,
    }


def schema_bytes(schema_id: str) -> bytes:
    contribution = HealingPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    for schema in contribution.schemas:
        if schema.schema_id == schema_id:
            return bytes(schema.content)
    raise KeyError(schema_id)


def forbidden_healing_imports() -> set[str]:
    root = _WHEEL_ROOT / "assurance_healing"
    if not root.is_dir():
        raise FileNotFoundError(f"package source is missing: {root}")
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for module_name in _imported_modules(ast.parse(path.read_text(encoding="utf-8"))):
            if is_declared_cross_wheel_import(root, path, module_name):
                continue
            if any(module_name == item or module_name.startswith(f"{item}.") for item in _LEGACY_ROOTS):
                found.add(module_name)
            if (
                module_name.startswith("assurance_intake.")
                or module_name.startswith("assurance_generation.")
                or module_name.startswith("assurance_execution.")
            ):
                if not any(
                    module_name == allowed or module_name.startswith(f"{allowed}.")
                    for allowed in _ALLOWED_ASSURANCE
                ):
                    found.add(module_name)
            if module_name in {"assurance_intake", "assurance_generation", "assurance_execution"}:
                found.add(module_name)
            if module_name == "assurance_product" or module_name.startswith("assurance_product."):
                found.add(module_name)
    return found


def _imported_modules(tree: ast.AST) -> tuple[str, ...]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return tuple(names)


def _installed_schema_mapping() -> dict[str, tuple[str, str]]:
    contribution = HealingPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    return {
        schema.schema_id: (
            "1",
            canonical_digest(cast(JSONValue, json.loads(schema.content))),
        )
        for schema in contribution.schemas
    }


def test_healing_product_lock_schema_mapping_is_current_only() -> None:
    assert _installed_schema_mapping() == _CURRENT_HEALING_SCHEMA_MAPPING


def test_override_token_is_bound_to_policy_and_candidate() -> None:
    with pytest.raises(ValidationError, match="override token digest"):
        HealingOverrideTokenV1.model_validate(
            forged_override_token(policy_digest="0" * 64, candidate_digest="1" * 64)
        )


def test_override_token_accepts_matching_policy_and_candidate_digest() -> None:
    token = HealingOverrideTokenV1.model_validate(valid_override_token())
    assert token.policy_digest == _HEX_A
    assert token.candidate_digest == _HEX_B


def test_override_token_rejects_rewritten_change_id() -> None:
    token = valid_override_token()
    token["change_id"] = "CH-OTHER"
    with pytest.raises(ValidationError, match="override token digest"):
        HealingOverrideTokenV1.model_validate(token)


def test_test_change_policy_rejects_extra_keys() -> None:
    with pytest.raises(ValidationError):
        TestChangePolicyV1.model_validate({**valid_policy(), "extra": True})


def test_test_change_policy_rejects_absolute_traversal_and_empty_roots() -> None:
    with pytest.raises(ValidationError):
        TestChangePolicyV1.model_validate({**valid_policy(), "allowed_test_roots": ["/tmp"]})
    with pytest.raises(ValidationError):
        TestChangePolicyV1.model_validate({**valid_policy(), "allowed_test_roots": ["tests/../secret"]})
    with pytest.raises(ValidationError):
        TestChangePolicyV1.model_validate({**valid_policy(), "allowed_test_roots": []})
    with pytest.raises(ValidationError):
        TestChangePolicyV1.model_validate({**valid_policy(), "forbidden_product_roots": ["../app"]})


def test_test_change_policy_accepts_closed_roots() -> None:
    policy = TestChangePolicyV1.model_validate(valid_policy())
    assert policy.allowed_test_roots == ("tests",)
    assert policy.require_approval is True


def test_healing_schema_bytes_equal_model_schema() -> None:

    assert schema_bytes("assurance.healing.schema.fix-proposal.v1") == canonical_json_bytes(
        cast(JSONValue, FixProposal.model_json_schema())
    )
    assert schema_bytes("assurance.healing.schema.healing-safety.v1") == canonical_json_bytes(
        cast(JSONValue, SafetyCheck.model_json_schema())
    )
    assert schema_bytes("assurance.healing.schema.healing-status.v1") == canonical_json_bytes(
        cast(JSONValue, HealingStatusV1.model_json_schema())
    )


def test_healing_contracts_import_only_upstream_public_contracts() -> None:
    assert forbidden_healing_imports() == set()


def test_healing_agent_job_catalog_is_feature_owned() -> None:
    from types import MappingProxyType

    from assurance_healing.contracts.attempts import AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES

    expected = {
        "apply-test-repair": (
            "aa-apply-test-repair",
            "assurance-v1-test-author",
            ("qa/tests",),
        ),
        "fix-proposal": (
            "aa-fix-proposal",
            "assurance-v1-doc-author",
            ("qa/results/healing/fix-proposal.json",),
        ),
    }
    assert isinstance(AGENT_JOB_CONTRACTS, MappingProxyType)
    assert isinstance(OUTPUT_ROUTE_TEMPLATES, MappingProxyType)
    assert len(AGENT_JOB_CONTRACTS) == 2
    assert tuple(AGENT_JOB_CONTRACTS) == tuple(expected)
    assert tuple(OUTPUT_ROUTE_TEMPLATES) == tuple(expected)
    for base, (skill_id, agent_profile, writes) in expected.items():
        contract = AGENT_JOB_CONTRACTS[base]
        assert contract.contract_id == f"assurance.healing.agent.{base}.v1"
        assert contract.skill_id == skill_id
        assert contract.agent_profile == agent_profile
        expected_claims = (
            tuple(
                sorted(
                    (
                        "qa/results/healing/applied-repair.json",
                        "qa/results/healing/epochs",
                        "qa/results/healing/verified-repair.json",
                        "qa/tests",
                    )
                )
            )
            if base == "apply-test-repair"
            else writes
        )
        assert contract.resources.writes == expected_claims
        assert OUTPUT_ROUTE_TEMPLATES[base] == writes
        dumped = json.dumps(contract.canonical_projection()).lower()
        assert "opencode" not in dumped
        assert "cursor" not in dumped


def test_output_routes_are_flat_qa_paths() -> None:
    from assurance_healing.contracts.attempts import OUTPUT_ROUTE_TEMPLATES

    rendered = "\n".join(path for paths in OUTPUT_ROUTE_TEMPLATES.values() for path in paths)
    assert "qa/" + "changes" not in rendered
    assert "{change_id}" not in rendered
    assert "qa/" + "archive" not in rendered
    assert all(path.startswith("qa/") for paths in OUTPUT_ROUTE_TEMPLATES.values() for path in paths)
