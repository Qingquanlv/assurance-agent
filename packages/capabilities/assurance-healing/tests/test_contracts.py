from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Literal, cast

import pytest
from pydantic import ValidationError

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes

from assurance_healing.contracts import (
    CoverageRepairBrief,
    FixProposal,
    HealApplyIntentV2,
    HealApplyReceiptV2,
    HealingAllocationIntentV2,
    HealingOverrideTokenV1,
    HealingStatusV1,
    ProposalApprovedIntentV1,
    ProposalApprovedReceiptV1,
    SafetyCheck,
    TestChangePolicyV1,
)
from assurance_healing.operations.status import project_episode
from assurance_healing.plugin import HealingPlugin

_CURRENT_HEALING_SCHEMA_MAPPING: dict[str, tuple[str, str]] = {
    "assurance.healing.schema.allocation-intent.v2": (
        "1",
        "dbd980c555b0bafa38e59c565f3da7eac505ae0b98bd8cfb8d8c00a5002aef84",
    ),
    "assurance.healing.schema.allocation-receipt.v2": (
        "1",
        "48e8dde3a6cefde2e027ba55886e9cb5844085983e05ad8f81935f23afdf797e",
    ),
    "assurance.healing.schema.coverage-repair.v1": (
        "1",
        "51bdecc0456894314c5cae7c3d508481cdb7b43834ca1c761c3e759bcc1aa422",
    ),
    "assurance.healing.schema.fix-proposal.v1": (
        "1",
        "45baef416a046697f7c9f7634c16f42a2fd765429a44be8e816119d7567020e4",
    ),
    "assurance.healing.schema.heal-apply-intent.v2": (
        "1",
        "1addb595efda8616f8e9fbaea045677abb9f733ad7bdc7d292dc6a719acad047",
    ),
    "assurance.healing.schema.heal-apply-receipt.v2": (
        "1",
        "e0ec92d710cab2f9697a422379e91d8b5dbec7e9a3a68a97720d3a7b4fb762c1",
    ),
    "assurance.healing.schema.healing-safety.v1": (
        "1",
        "de2609cbef7eff4f3fb644b21bfd688e6dbc5819618a14bf0bdb07ac1176ec47",
    ),
    "assurance.healing.schema.healing-status.v1": (
        "1",
        "6b7c39e35175fff1918929cbd51076b55d29173125905a95f9e04b6b688d9f27",
    ),
    "assurance.healing.schema.proposal-approved-intent.v1": (
        "1",
        "24f3d28027912dab7a3f699f82b1bd6dd6f745b3986bd0eacdde7a8230a54e0d",
    ),
    "assurance.healing.schema.proposal-approved-receipt.v1": (
        "1",
        "01872edb626924409496458706af1258add2b7f6bb6095e3c1df9e974e24ecf1",
    ),
    "assurance.healing.workflow.repair-coverage.input.v1": (
        "1",
        "590d0fd34463cb229d10571ff2ed4d37dbc2bf2526d9a22926ce5bf47e95990d",
    ),
    "assurance.healing.workflow.repair-coverage.output.v1": (
        "1",
        "274c1aecb03147846bc6dba1ff42a6fddcdd80fbc62073a944badfd93d1b2765",
    ),
    "assurance.healing.workflow.repair-failure.input.v1": (
        "1",
        "b2600357ef9c414b01d6d4661bbf76d6e1994e98422f8085d97e383da51cda5b",
    ),
    "assurance.healing.workflow.repair-failure.output.v1": (
        "1",
        "5ebdd6bb4ce22232a23c7a9a0fbe85eea971c2dd092393c331ddcb17af808079",
    ),
}

_TESTS_ROOT = Path(__file__).resolve().parent
_WHEEL_ROOT = _TESTS_ROOT.parent
_LEGACY_ROOTS = ("assurance_agent", "assurance_kernel")
_ALLOWED_ASSURANCE = (
    "assurance_intake.contracts",
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


def valid_heal_apply_receipt(*, idempotency_key: str | None = None) -> dict[str, object]:
    from assurance_healing.contracts.wire import heal_apply_intent_digest

    record_key = "heal-apply-CH-DEMO-001-api"
    payload: dict[str, object] = {
        "schema_version": "2",
        "record_key": record_key,
        "change_id": "CH-DEMO-001",
        "owner_id": "assurance.healing",
        "target": "api",
        "entry_batch_id": "20260822T000000Z",
        "outcome": "applied",
        "candidate_digest": _HEX_A,
        "baseline_digest": _HEX_B,
        "policy_digest": _HEX_C,
        "write_set_id": "ws-1",
        "proposal_ids": ["P1"],
        "claimed_modified_paths": ["tests/api/test_users.py"],
        "safety_payload_digest": _HEX_E,
    }
    payload["intent_digest"] = heal_apply_intent_digest(payload)
    payload["idempotency_key"] = record_key if idempotency_key is None else idempotency_key
    payload["settlement_key"] = _HEX_A
    return payload


def forged_receipt() -> dict[str, object]:
    return valid_heal_apply_receipt(idempotency_key="forged-record-key")


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


def resource_bytes_for(resource_id: str) -> bytes:
    contribution = HealingPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    for resource in contribution.resources:
        if resource.resource_id == resource_id:
            return bytes(resource.content)
    raise KeyError(resource_id)


def forbidden_healing_imports() -> set[str]:
    root = _WHEEL_ROOT / "assurance_healing"
    if not root.is_dir():
        raise FileNotFoundError(f"package source is missing: {root}")
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for module_name in _imported_modules(ast.parse(path.read_text(encoding="utf-8"))):
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


def test_exclusive_document_versions_are_locked_on_models() -> None:
    # SchemaContribution has no per-schema version field; exclusive document
    # versions live on the Pydantic models themselves.
    assert HealApplyIntentV2.model_fields["schema_version"].annotation == Literal["2"]
    assert HealApplyReceiptV2.model_fields["schema_version"].annotation == Literal["2"]
    assert ProposalApprovedIntentV1.model_fields["schema_version"].annotation == Literal["1"]
    assert ProposalApprovedReceiptV1.model_fields["schema_version"].annotation == Literal["1"]
    assert HealingAllocationIntentV2.model_fields["schema_version"].annotation == Literal["2"]


def current_allocation_event(*, seq: int = 1) -> dict[str, object]:
    return {
        "type": "healing_attempt_allocated_v2",
        "seq": seq,
        "schema_version": "2",
        "episode_id": "ep-1",
        "attempt_id": "at-1",
        "attempt_number": 1,
        "operation_id": "op-1",
        "change_id": "CH-DEMO-001",
        "owner_id": "assurance.healing",
        "source_batch_id": "batch-src",
        "entry_batch_id": "batch-entry",
        "candidate_digest": _HEX_A,
        "baseline_digest": _HEX_B,
        "policy_digest": _HEX_C,
        "execution_evidence_digest": _HEX_D,
        "baseline_embedded": True,
    }


def current_proposal_approved_event(*, seq: int = 1) -> dict[str, object]:
    return {
        "type": "fixer_proposal_approved",
        "seq": seq,
        "schema_version": "1",
        "approval_id": "apr-1",
        "change_id": "CH-DEMO-001",
        "owner_id": "assurance.healing",
        "root_invocation_id": "inv-1",
        "interrupt_task_id": "task-1",
        "source_gate_attempt_id": "gate-1",
        "source_tree_id": "tree-src",
        "target_tree_id": "tree-dst",
        "proposal_digest": _HEX_A,
        "fixer_authority_digest": _HEX_B,
        "candidate_digest": _HEX_C,
        "baseline_digest": _HEX_D,
        "policy_digest": _HEX_E,
        "targets": ["api"],
        "paths": ["tests/api/test_users.py"],
        "action": "approve_and_apply",
    }


def current_heal_apply_event(*, seq: int = 2) -> dict[str, object]:
    return {"type": "heal_record_apply_v2", "seq": seq, **valid_heal_apply_receipt()}


@pytest.mark.parametrize(
    "event_type",
    ["healing_attempt_allocated", "healing_entry_baseline_pinned", "heal_record_apply"],
)
def test_episode_rejects_old_event_kinds(event_type: str) -> None:
    with pytest.raises(ValueError, match="current schema"):
        project_episode(
            [
                {
                    "type": event_type,
                    "seq": 1,
                    "episode_id": "ep-1",
                    "attempt_key": "legacy-key",
                    "artifact_sha256": _HEX_A,
                }
            ]
        )


def test_episode_rejects_missing_record_key() -> None:
    with pytest.raises(ValueError, match="current schema"):
        project_episode(
            [
                {
                    "type": "heal_record_apply_v2",
                    "seq": 2,
                    "target": "api",
                    "outcome": "applied",
                    "claimed_modified_paths": ["tests/api/test_users.py"],
                }
            ]
        )


def test_episode_rejects_former_approval_field_aliases() -> None:
    with pytest.raises(ValueError, match="current schema"):
        project_episode(
            [
                {
                    "type": "fixer_proposal_approved",
                    "seq": 1,
                    "approval_id": "apr-1",
                    "proposal_sha256": f"sha256:{_HEX_A}",
                    "fixer_authority_sha256": f"sha256:{_HEX_B}",
                    "entry_baseline_sha256": f"sha256:{_HEX_C}",
                    "policy_sha256": f"sha256:{_HEX_D}",
                }
            ]
        )


def test_episode_rejects_former_apply_field_aliases() -> None:
    with pytest.raises(ValueError, match="current schema"):
        project_episode(
            [
                {
                    "type": "heal_record_apply_v2",
                    "seq": 2,
                    "record_key": "heal-apply-CH-DEMO-001-api",
                    "attempt_key": "legacy-key",
                    "safety_payload_sha256": _HEX_E,
                    "files_modified": ["tests/api/test_users.py"],
                }
            ]
        )


def test_episode_rejects_former_allocation_field_aliases() -> None:
    with pytest.raises(ValueError, match="current schema"):
        project_episode(
            [
                {
                    "type": "healing_attempt_allocated_v2",
                    "seq": 1,
                    "episode_id": "ep-1",
                    "attempt_id": "at-1",
                    "attempt_number": 1,
                    "operation_id": "op-1",
                    "source_batch_id": "batch-src",
                    "entry_batch_id": "batch-entry",
                    "baseline_sha256": _HEX_B,
                    "baseline_embedded": True,
                }
            ]
        )


def test_episode_projects_current_allocation_document() -> None:
    event = current_allocation_event()
    document = {key: value for key, value in event.items() if key not in {"type", "seq"}}
    HealingAllocationIntentV2.model_validate(document)
    projection = project_episode([event])
    dumped = json.dumps(projection)
    assert "legacy:" not in dumped
    assert '"form": "legacy"' not in dumped
    allocations = projection["allocations"]
    assert isinstance(allocations, list) and len(allocations) == 1
    allocation = allocations[0]
    assert isinstance(allocation, dict)
    assert allocation["operation_id"] == "op-1"
    assert allocation["episode_id"] == "ep-1"
    assert allocation["attempt_id"] == "at-1"
    assert allocation["baseline_digest"] == _HEX_B
    assert "baseline_sha256" not in allocation
    assert projection["attempts_used"] == 1
    baseline = projection["baseline"]
    assert isinstance(baseline, dict)
    assert baseline["artifact_sha256"] == _HEX_B
    assert baseline.get("form") != "legacy"


def test_episode_projects_current_proposal_approved_document() -> None:
    event = current_proposal_approved_event()
    document = {key: value for key, value in event.items() if key not in {"type", "seq"}}
    ProposalApprovedIntentV1.model_validate(document)
    projection = project_episode([event])
    dumped = json.dumps(projection)
    assert "legacy:" not in dumped
    assert '"form": "legacy"' not in dumped
    approvals = projection["approvals"]
    assert isinstance(approvals, list) and len(approvals) == 1
    approval = approvals[0]
    assert isinstance(approval, dict)
    assert approval["approval_id"] == "apr-1"
    assert approval["proposal_digest"] == _HEX_A
    assert approval["fixer_authority_digest"] == _HEX_B
    assert approval["baseline_digest"] == _HEX_D
    assert approval["policy_digest"] == _HEX_E
    assert approval["targets"] == ["api"]
    assert approval["paths"] == ["tests/api/test_users.py"]
    assert approval.get("form") != "legacy"


def test_episode_projects_current_heal_apply_document() -> None:
    event = current_heal_apply_event()
    document = {key: value for key, value in event.items() if key not in {"type", "seq"}}
    HealApplyReceiptV2.model_validate(document)
    projection = project_episode([event])
    dumped = json.dumps(projection)
    assert "legacy:" not in dumped
    assert '"form": "legacy"' not in dumped
    records = projection["records"]
    assert isinstance(records, list) and len(records) == 1
    record = records[0]
    assert isinstance(record, dict)
    assert record["record_key"] == document["record_key"]
    assert record["safety_payload_digest"] == _HEX_E
    assert record["intent_digest"] == document["intent_digest"]
    assert record["write_set_id"] == "ws-1"
    assert record["claimed_modified_paths"] == ["tests/api/test_users.py"]
    assert record["form"] == "v2"
    assert record.get("form") != "legacy"


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


def test_effect_receipt_rejects_wrong_idempotency_key() -> None:
    with pytest.raises(ValidationError, match="idempotency key"):
        HealApplyReceiptV2.model_validate(forged_receipt())


def test_effect_receipt_accepts_matching_idempotency_key() -> None:
    receipt = HealApplyReceiptV2.model_validate(valid_heal_apply_receipt())
    assert receipt.idempotency_key == receipt.record_key


def test_effect_receipt_rejects_unbound_intent_digest() -> None:
    payload = valid_heal_apply_receipt()
    payload["intent_digest"] = _HEX_D
    with pytest.raises(ValidationError, match="intent digest"):
        HealApplyReceiptV2.model_validate(payload)


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


def test_allocation_intent_requires_binding_digests() -> None:
    with pytest.raises(ValidationError):
        HealingAllocationIntentV2.model_validate(
            {
                "schema_version": "2",
                "episode_id": "ep-1",
                "attempt_id": "at-1",
                "attempt_number": 1,
                "operation_id": "op-1",
                "change_id": "CH-DEMO-001",
                "owner_id": "assurance.healing",
                "source_batch_id": "batch-src",
                "entry_batch_id": "batch-entry",
                "candidate_digest": "not-a-digest",
                "baseline_digest": _HEX_B,
                "policy_digest": _HEX_C,
                "execution_evidence_digest": _HEX_D,
                "baseline_embedded": True,
            }
        )


def test_proposal_approved_receipt_rejects_wrong_idempotency_key() -> None:
    with pytest.raises(ValidationError, match="idempotency key"):
        ProposalApprovedReceiptV1.model_validate(
            {
                "schema_version": "1",
                "approval_id": "apr-1",
                "idempotency_key": "forged-approval",
                "settlement_key": _HEX_A,
                "change_id": "CH-DEMO-001",
                "owner_id": "assurance.healing",
                "root_invocation_id": "inv-1",
                "interrupt_task_id": "task-1",
                "source_gate_attempt_id": "gate-1",
                "source_tree_id": "tree-src",
                "target_tree_id": "tree-dst",
                "proposal_digest": _HEX_A,
                "fixer_authority_digest": _HEX_B,
                "candidate_digest": _HEX_C,
                "baseline_digest": _HEX_D,
                "policy_digest": _HEX_E,
                "targets": ["api"],
                "paths": ["tests/api/test_users.py"],
                "action": "approve_and_apply",
            }
        )


def test_healing_schema_bytes_equal_model_schema() -> None:
    from assurance_healing.contracts import (
        HealApplyIntentV2,
        HealingAllocationReceiptV2,
        ProposalApprovedIntentV1,
    )

    assert schema_bytes("assurance.healing.schema.fix-proposal.v1") == canonical_json_bytes(
        cast(JSONValue, FixProposal.model_json_schema())
    )
    assert schema_bytes("assurance.healing.schema.healing-safety.v1") == canonical_json_bytes(
        cast(JSONValue, SafetyCheck.model_json_schema())
    )
    assert schema_bytes("assurance.healing.schema.coverage-repair.v1") == canonical_json_bytes(
        cast(JSONValue, CoverageRepairBrief.model_json_schema())
    )
    assert schema_bytes("assurance.healing.schema.healing-status.v1") == canonical_json_bytes(
        cast(JSONValue, HealingStatusV1.model_json_schema())
    )
    assert schema_bytes("assurance.healing.schema.allocation-intent.v2") == canonical_json_bytes(
        cast(JSONValue, HealingAllocationIntentV2.model_json_schema())
    )
    assert schema_bytes("assurance.healing.schema.allocation-receipt.v2") == canonical_json_bytes(
        cast(JSONValue, HealingAllocationReceiptV2.model_json_schema())
    )
    assert schema_bytes("assurance.healing.schema.proposal-approved-intent.v1") == canonical_json_bytes(
        cast(JSONValue, ProposalApprovedIntentV1.model_json_schema())
    )
    assert schema_bytes("assurance.healing.schema.proposal-approved-receipt.v1") == canonical_json_bytes(
        cast(JSONValue, ProposalApprovedReceiptV1.model_json_schema())
    )
    assert schema_bytes("assurance.healing.schema.heal-apply-intent.v2") == canonical_json_bytes(
        cast(JSONValue, HealApplyIntentV2.model_json_schema())
    )
    assert schema_bytes("assurance.healing.schema.heal-apply-receipt.v2") == canonical_json_bytes(
        cast(JSONValue, HealApplyReceiptV2.model_json_schema())
    )


def test_policy_resource_is_closed_and_canonical() -> None:
    raw = resource_bytes_for("assurance.healing.policy.test-change-policy.v1")
    assert raw == canonical_json_bytes(cast(JSONValue, valid_policy()))
    policy = TestChangePolicyV1.model_validate_json(raw)
    assert policy.allowed_test_roots == ("tests",)


def test_healing_contracts_import_only_upstream_public_contracts() -> None:
    assert forbidden_healing_imports() == set()


def test_healing_agent_job_catalog_is_feature_owned() -> None:
    from types import MappingProxyType

    from assurance_healing.contracts.attempts import AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES

    expected = {
        "coverage-repair": (
            "aa-coverage-repair",
            "assurance-v1-test-author",
            ("qa/results/healing/coverage-repair.json",),
        ),
        "fix-proposal": (
            "aa-fix-proposal",
            "assurance-v1-doc-author",
            ("qa/results/healing/fix-proposal.json",),
        ),
        "apply-test-repair": (
            "aa-apply-test-repair",
            "assurance-v1-test-author",
            (
                "qa/results/healing/epochs/{coverage_epoch}/rounds/{repair_round}",
                "qa/tests",
            ),
        ),
    }
    assert isinstance(AGENT_JOB_CONTRACTS, MappingProxyType)
    assert isinstance(OUTPUT_ROUTE_TEMPLATES, MappingProxyType)
    assert len(AGENT_JOB_CONTRACTS) == 3
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
                        "qa/results/healing/epochs",
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
    assert "qa/changes" not in rendered
    assert "{change_id}" not in rendered
    assert "qa/archive" not in rendered
    assert all(path.startswith("qa/") for paths in OUTPUT_ROUTE_TEMPLATES.values() for path in paths)
