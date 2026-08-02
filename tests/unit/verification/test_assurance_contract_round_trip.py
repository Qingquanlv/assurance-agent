"""Canonical four-layer fixture independence and production-boundary round trips."""

from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.artifacts.models.assurance import PLAN_CHECK_IDS
from assurance_agent.verification.checks.base import CheckContext
from assurance_agent.verification.checks.registry import run_plan_checks
from assurance_agent.verification.profiles import get_layer_assurance_profile
from tests.helpers_assurance_contract import (
    ASSURANCE_FIXTURE_ROOT,
    SkillContractParseError,
    assert_fixture_independence,
    boundary_map,
    expected_check_statuses,
    load_canonical_assurance_bundle,
    observe_assurance_contract,
    parse_skill_contract_sections,
)

_LAYERS = ("api", "e2e", "fuzz", "performance")


@pytest.mark.parametrize("layer", _LAYERS)
def test_fixture_root_is_independent_and_closed(layer: str) -> None:
    bundle = load_canonical_assurance_bundle(layer)
    assert_fixture_independence(bundle.root)
    profile = get_layer_assurance_profile(layer)
    assert bundle.change_id == "CH-CANONICAL"
    assert (bundle.root / "tests" / "testdata" / "domain" / "account.py").is_file()
    assert (bundle.root / ".aa" / "config.yaml").is_file()
    assert (bundle.root / bundle.summary_relpath).is_file()
    assert not (bundle.root / profile.checks_artifact).exists()
    assert not (bundle.root / "codegen").exists()
    assert bundle.adapter_paths
    for other in _LAYERS:
        if other == layer:
            continue
        assert f"tests/fixtures/assurance/{other}-contract" not in bundle.summary_text


@pytest.mark.parametrize("layer", _LAYERS)
def test_structural_skill_contract_reader_accepts_fixture_skill(layer: str) -> None:
    bundle = load_canonical_assurance_bundle(layer)
    sections = parse_skill_contract_sections(bundle.skill_path)
    assert sections.inputs
    assert sections.outputs
    assert ("owner", "graph_ledger") in sections.state_authority
    assert ("agent_state_writes", "forbidden") in sections.state_authority
    assert {group.name for group in sections.inputs} >= {"required"}


def test_structural_skill_contract_reader_rejects_duplicates_and_unknown_groups(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(
        """## Inputs

### required

- `change:plans/api-plan.md`

### mystery

- `change:plans/x.md`

## Outputs

### required

- `change:codegen/api-codegen-summary.md`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`
""",
        encoding="utf-8",
    )
    with pytest.raises(SkillContractParseError, match="unknown group"):
        parse_skill_contract_sections(path)

    path.write_text(
        """## Inputs

### required

- `change:plans/api-plan.md`

## Outputs

### required

- `change:codegen/api-codegen-summary.md`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Inputs

### required

- `change:plans/api-plan.md`
""",
        encoding="utf-8",
    )
    with pytest.raises(SkillContractParseError, match="duplicate heading"):
        parse_skill_contract_sections(path)

    path.write_text(
        """## Inputs

### required

- change:plans/api-plan.md

## Outputs

### required

- `change:codegen/api-codegen-summary.md`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`
""",
        encoding="utf-8",
    )
    with pytest.raises(SkillContractParseError, match="malformed path row"):
        parse_skill_contract_sections(path)

    path.write_text(
        """## Inputs

### required

- `change:plans/api-plan.md`

## Unexpected Interrupt

### required

- `change:plans/x.md`

## Outputs

### required

- `change:codegen/api-codegen-summary.md`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`
""",
        encoding="utf-8",
    )
    with pytest.raises(SkillContractParseError, match="interrupted by unexpected heading"):
        parse_skill_contract_sections(path)


@pytest.mark.parametrize(
    ("layer", "statuses"),
    [
        ("api", ("pass", "pass", "pass", "pass")),
        ("e2e", ("pass", "pass", "pass", "pass")),
        ("fuzz", ("pass", "pass", "not_applicable", "pass")),
        ("performance", ("pass", "pass", "not_applicable", "pass")),
    ],
)
def test_canonical_mechanical_matrix_and_empty_layer_na(
    layer: str, statuses: tuple[str, str, str, str]
) -> None:
    bundle = load_canonical_assurance_bundle(layer)
    profile = get_layer_assurance_profile(layer)
    document = run_plan_checks(
        CheckContext(
            plan_texts=bundle.plan_texts,
            cases=bundle.cases,
            data_knowledge=bundle.data_knowledge,
            layer=layer,  # type: ignore[arg-type]
            required_capabilities=bundle.required_capabilities,
        )
    )
    assert tuple(item.check_id for item in document.checks) == PLAN_CHECK_IDS
    assert tuple(item.status for item in document.checks) == statuses
    assert {item.check_id: item.status for item in document.checks} == expected_check_statuses(layer)
    for item in document.checks:
        if item.check_id not in profile.applicable_check_ids:
            assert item.applicability_reason == "check_not_in_profile"

    empty = run_plan_checks(
        CheckContext(
            plan_texts={},
            cases=(),
            data_knowledge=bundle.data_knowledge,
            layer=layer,  # type: ignore[arg-type]
            required_capabilities=(),
        )
    )
    assert tuple(item.status for item in empty.checks) == ("not_applicable",) * 4
    assert all(item.applicability_reason == "layer_not_applicable" for item in empty.checks)


@pytest.mark.parametrize("layer", _LAYERS)
def test_observe_assurance_contract_crosses_production_boundaries(tmp_path: Path, layer: str) -> None:
    bundle = load_canonical_assurance_bundle(layer)
    observation = observe_assurance_contract(bundle, tmp_path=tmp_path)
    boundaries = boundary_map(observation)
    for name in (
        "skill_contract",
        "authoring_ingest",
        "runtime_ingest",
        "freeze",
        "applicability",
        "mechanical",
        "wire",
        "plan_gate",
        "codegen_precondition",
        "workspace_read",
        "workspace_write",
        "codegen_workspace",
    ):
        assert boundaries[name].status == "ok", (name, boundaries[name])
    assert observation.plan_gate_verdict == "pass"
    assert observation.codegen_precondition_verdict == "pass"
    assert observation.frozen_review.review_type == f"{layer}-plan"
    assert observation.frozen_review.change_id == bundle.change_id
    assert observation.generated_files_manifest_sha256.startswith("sha256:")
    assert observation.visible_codegen_inputs
    assert observation.frozen_output_digests
    # Task 6 owns candidate validation receipts; keep the dormant field empty for now.
    assert observation.candidate_validation_receipt_id == ""


def test_assurance_fixture_tree_contains_exactly_four_layer_roots() -> None:
    roots = sorted(
        path.name
        for path in ASSURANCE_FIXTURE_ROOT.iterdir()
        if path.is_dir() and path.name.endswith("-contract")
    )
    assert roots == ["api-contract", "e2e-contract", "fuzz-contract", "performance-contract"]
