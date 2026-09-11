from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.canonical import JSONValue, canonical_json_bytes

from assurance_execution.contracts import (
    ClosedMappingV1,
    ExecutionAgentResultV1,
    ExecutionEvidenceV1,
    ExecutionManifest,
    SelectedTargets,
)
from assurance_execution.plugin import ExecutionPlugin

_TESTS_ROOT = Path(__file__).resolve().parent
_WHEEL_ROOT = _TESTS_ROOT.parent
VALID_LEAFS = frozenset({"entities.item.create", "auth.session.create"})
VALID_CASES = frozenset({"TC_A", "TC_B"})
_PLAN_REF = {
    "path": f"qa/results/plan/{'a' * 64}/resolved-assurance-plan.json",
    "digest": "a" * 64,
}
_LEGACY_ROOTS = ("assurance_agent", "assurance_kernel")
_ALLOWED_ASSURANCE = (
    "assurance_intake.contracts",
    "assurance_generation.contracts",
)


def mapping(
    test: str,
    *,
    case_id: str = "TC_A",
    capability: str = "entities.item.create",
    layer: str = "api",
) -> dict[str, object]:
    return {"test": test, "case_id": case_id, "capability": capability, "layer": layer}


def valid_mapping_payload(*, selected: list[str] | None = None) -> dict[str, object]:
    tests = selected or ["tests/a.py"]
    return {"selected": tests, "mappings": [mapping(item) for item in tests]}


def valid_command_receipt(
    *,
    family: str = "api",
    command: list[str] | None = None,
) -> dict[str, object]:
    return {
        "family": family,
        "command": command or ["pytest", "tests/a.py"],
        "exit_code": 0,
        "collected": 1,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
    }


def valid_receipt(
    *,
    commands: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {"commands": commands or [valid_command_receipt()]}


def valid_result(test: str = "tests/a.py") -> dict[str, object]:
    return {"test": test, "status": "passed", "duration_ms": 1, "message": ""}


def valid_evidence(
    *,
    selected: list[str] | None = None,
    results: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    tests = selected or ["tests/a.py"]
    return {
        "change_id": "CH-DEMO-001",
        "plan_digest": "a" * 64,
        "plan_ref": _PLAN_REF,
        "batch_id": "20260822T000000Z",
        "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
        "mapping": valid_mapping_payload(selected=tests),
        "mapping_digest": "a" * 64,
        "baseline_tree_id": "b" * 64,
        "runner_profile_digest": "c" * 64,
        "receipt_digest": "d" * 64,
        "receipt": valid_receipt(),
        "results": results if results is not None else [valid_result(item) for item in tests],
    }


def schema_bytes(schema_id: str) -> bytes:
    contribution = ExecutionPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    for schema in contribution.schemas:
        if schema.schema_id == schema_id:
            return bytes(schema.content)
    raise KeyError(schema_id)


def forbidden_execution_imports() -> set[str]:
    root = _WHEEL_ROOT / "assurance_execution"
    if not root.is_dir():
        raise FileNotFoundError(f"package source is missing: {root}")
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for module_name in _imported_modules(ast.parse(path.read_text(encoding="utf-8"))):
            if any(module_name == item or module_name.startswith(f"{item}.") for item in _LEGACY_ROOTS):
                found.add(module_name)
            if module_name.startswith("assurance_intake.") or module_name.startswith("assurance_generation."):
                if not any(
                    module_name == allowed or module_name.startswith(f"{allowed}.")
                    for allowed in _ALLOWED_ASSURANCE
                ):
                    found.add(module_name)
            if module_name in {"assurance_intake", "assurance_generation"}:
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


def test_closed_mapping_rejects_duplicate_or_unselected_test() -> None:
    with pytest.raises(ValidationError, match="mapping must equal selected tests"):
        ClosedMappingV1.model_validate({"selected": ["tests/a.py"], "mappings": [mapping("tests/b.py")]})


def test_closed_mapping_rejects_duplicate_selected_or_mapped_test() -> None:
    with pytest.raises(ValidationError, match="mapping must equal selected tests"):
        ClosedMappingV1.model_validate(
            {"selected": ["tests/a.py", "tests/a.py"], "mappings": [mapping("tests/a.py")]}
        )
    with pytest.raises(ValidationError, match="mapping must equal selected tests"):
        ClosedMappingV1.model_validate(
            {"selected": ["tests/a.py"], "mappings": [mapping("tests/a.py"), mapping("tests/a.py")]}
        )


def test_closed_mapping_accepts_exact_selected_set() -> None:
    model = ClosedMappingV1.model_validate(
        valid_mapping_payload(selected=["tests/a.py", "tests/b.py"]),
        context={"capability_leafs": VALID_LEAFS, "case_ids": VALID_CASES},
    )
    assert tuple(entry.test for entry in model.mappings) == ("tests/a.py", "tests/b.py")


def test_closed_mapping_rejects_unknown_case_or_capability() -> None:
    raw = valid_mapping_payload()
    with pytest.raises(ValidationError, match="unknown capability leaf"):
        ClosedMappingV1.model_validate(
            raw,
            context={"capability_leafs": frozenset({"auth.session.create"}), "case_ids": VALID_CASES},
        )
    with pytest.raises(ValidationError, match="unknown case id"):
        ClosedMappingV1.model_validate(
            raw,
            context={"capability_leafs": VALID_LEAFS, "case_ids": frozenset({"TC_OTHER"})},
        )


def test_execution_evidence_rejects_unselected_old_test() -> None:
    raw = valid_evidence(results=[valid_result("tests/legacy_test.py")])
    with pytest.raises(ValidationError, match="test outside the closed mapping"):
        ExecutionEvidenceV1.model_validate(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("collected", 2),
        ("passed", 0),
        ("failed", 1),
        ("skipped", 1),
    ],
)
def test_execution_evidence_rejects_receipt_count_drift(field: str, value: int) -> None:
    raw = valid_evidence()
    receipt = cast(dict[str, object], raw["receipt"])
    command = cast(list[dict[str, object]], receipt["commands"])[0]
    command[field] = value

    with pytest.raises(ValidationError, match="receipt counts"):
        ExecutionEvidenceV1.model_validate(raw)


def test_execution_evidence_accepts_one_ordered_command_receipt_per_selected_family() -> None:
    families = ("api", "e2e", "fuzz", "performance")
    tests = [f"tests/{family}.py" for family in families]
    raw = valid_evidence(
        selected=tests,
        results=[valid_result(test) for test in tests],
    )
    raw["selected_targets"] = {family: True for family in families}
    raw["mapping"] = {
        "selected": tests,
        "mappings": [
            mapping(test, case_id="TC_A", layer=family) for family, test in zip(families, tests, strict=True)
        ],
    }
    raw["receipt"] = {
        "commands": [
            valid_command_receipt(
                family=family,
                command=["locust", "--locustfile", test] if family == "performance" else ["pytest", test],
            )
            for family, test in zip(families, tests, strict=True)
        ]
    }

    evidence = ExecutionEvidenceV1.model_validate(raw)

    assert tuple(item.family for item in evidence.receipt.commands) == families
    assert evidence.receipt.collected == 4
    assert evidence.receipt.passed == 4
    assert evidence.receipt.failed == 0
    assert evidence.receipt.skipped == 0
    assert evidence.receipt.exit_code == 0


@pytest.mark.parametrize(
    "commands",
    [
        [],
        [
            valid_command_receipt(family="e2e", command=["pytest", "tests/e2e.py"]),
            valid_command_receipt(family="api", command=["pytest", "tests/api.py"]),
        ],
        [valid_command_receipt(family="api"), valid_command_receipt(family="api")],
    ],
)
def test_execution_evidence_rejects_missing_reordered_or_duplicate_command_receipts(
    commands: list[dict[str, object]],
) -> None:
    raw = valid_evidence()
    raw["selected_targets"] = {"api": True, "e2e": True, "fuzz": False, "performance": False}
    raw["receipt"] = {"commands": commands}

    with pytest.raises(ValidationError, match="command receipt|at least 1"):
        ExecutionEvidenceV1.model_validate(raw)


def test_execution_evidence_rejects_flat_single_command_receipt() -> None:
    raw = valid_evidence()
    raw["receipt"] = valid_command_receipt()

    with pytest.raises(ValidationError):
        ExecutionEvidenceV1.model_validate(raw)


def test_execution_evidence_rejects_a_command_runner_that_contradicts_its_family() -> None:
    raw = valid_evidence()
    raw["receipt"] = {
        "commands": [
            valid_command_receipt(
                family="api",
                command=["locust", "--locustfile", "tests/a.py"],
            )
        ]
    }

    with pytest.raises(ValidationError, match="runner must match its family"):
        ExecutionEvidenceV1.model_validate(raw)


def test_execution_evidence_rejects_selected_family_without_a_mapped_test() -> None:
    raw = valid_evidence()
    raw["selected_targets"] = {"api": True, "e2e": True, "fuzz": False, "performance": False}
    raw["receipt"] = {
        "commands": [
            valid_command_receipt(),
            {
                **valid_command_receipt(family="e2e", command=["pytest", "tests/e2e.py"]),
                "collected": 0,
                "passed": 0,
            },
        ]
    }

    with pytest.raises(ValidationError, match="mapped test for every selected family"):
        ExecutionEvidenceV1.model_validate(raw)


def test_execution_evidence_accepts_selected_results() -> None:
    model = ExecutionEvidenceV1.model_validate(valid_evidence())
    assert tuple(result.test for result in model.results) == ("tests/a.py",)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "passed"),
        ("executed_at", "2000-01-01T00:00:00Z"),
        ("mapping_digest", "a" * 64),
        ("receipt_digest", "b" * 64),
    ],
)
def test_execution_agent_result_rejects_kernel_derived_fields(field: str, value: str) -> None:
    raw = valid_evidence()
    raw.pop("mapping_digest")
    raw.pop("receipt_digest")
    raw[field] = value

    with pytest.raises(ValidationError, match="extra"):
        ExecutionAgentResultV1.model_validate(raw)


def test_execution_schema_bytes_equal_model_schema() -> None:
    assert schema_bytes("assurance.execution.schema.selected-targets.v1") == canonical_json_bytes(
        cast(JSONValue, SelectedTargets.model_json_schema())
    )
    assert schema_bytes("assurance.execution.schema.execution-manifest.v1") == canonical_json_bytes(
        cast(JSONValue, ExecutionManifest.model_json_schema())
    )
    assert schema_bytes("assurance.execution.schema.closed-mapping.v1") == canonical_json_bytes(
        cast(JSONValue, ClosedMappingV1.model_json_schema())
    )
    assert schema_bytes("assurance.execution.schema.execution-evidence.v1") == canonical_json_bytes(
        cast(JSONValue, ExecutionEvidenceV1.model_json_schema())
    )


def test_execution_contracts_import_only_intake_and_generation_contracts() -> None:
    assert forbidden_execution_imports() == set()


def test_execution_agent_job_catalog_is_feature_owned() -> None:
    from types import MappingProxyType

    from assurance_execution.contracts.attempts import AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES

    expected = {
        "execute": (
            "aa-execute",
            "assurance-v1-executor",
            ("qa/results/execution/execute-result.json",),
        ),
        "run": (
            "aa-run",
            "assurance-v1-executor",
            ("qa/results/execution/run-result.json",),
        ),
    }
    assert isinstance(AGENT_JOB_CONTRACTS, MappingProxyType)
    assert isinstance(OUTPUT_ROUTE_TEMPLATES, MappingProxyType)
    assert len(AGENT_JOB_CONTRACTS) == 2
    assert tuple(AGENT_JOB_CONTRACTS) == tuple(expected)
    assert tuple(OUTPUT_ROUTE_TEMPLATES) == tuple(expected)
    for base, (skill_id, agent_profile, writes) in expected.items():
        contract = AGENT_JOB_CONTRACTS[base]
        assert contract.contract_id == f"assurance.execution.agent.{base}.v1"
        assert contract.skill_id == skill_id
        assert contract.agent_profile == agent_profile
        assert contract.resources.writes == tuple(
            sorted(("qa/.staging/execution", *writes))
        )
        assert OUTPUT_ROUTE_TEMPLATES[base] == writes
        dumped = json.dumps(contract.canonical_projection()).lower()
        assert "opencode" not in dumped
        assert "cursor" not in dumped


def test_output_routes_are_flat_qa_paths() -> None:
    from assurance_execution.contracts.attempts import OUTPUT_ROUTE_TEMPLATES

    rendered = "\n".join(path for paths in OUTPUT_ROUTE_TEMPLATES.values() for path in paths)
    assert "qa/changes" not in rendered
    assert "{change_id}" not in rendered
    assert "qa/archive" not in rendered
    assert all(path.startswith("qa/") for paths in OUTPUT_ROUTE_TEMPLATES.values() for path in paths)
