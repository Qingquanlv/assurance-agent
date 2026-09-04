from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.product.checkpoint_r_support import (
    CheckpointRPreflightError,
    LIVE_OBSERVATION_HORIZON_SECONDS,
    count_exact_agent_occurrences,
    installed_agent_contract_ids,
    locked_runtime_binding_digests,
    required_change_files,
    require_checkpoint_r_preflight,
    seed_change_workspace,
)

_T5A_LANGGRAPH = frozenset(
    {
        "improvement-evaluate",
        "improvement-export",
        "improvement-apply",
        "improvement-rollback",
    }
)
_T5B_LANGGRAPH = frozenset(
    {
        "intake",
        "case",
        "archive",
        "retro",
        "issue-review",
        "issue-analyze",
        "issue-reconcile",
        "improvement-review",
    }
)
_T5C_LANGGRAPH = frozenset({"execute"})
_T5D_LANGGRAPH = frozenset({"full"})
_LANGGRAPH = _T5A_LANGGRAPH | _T5B_LANGGRAPH | _T5C_LANGGRAPH | _T5D_LANGGRAPH
_LEFTOVER: frozenset[str] = frozenset()
_REPO_ROOT = Path(__file__).resolve().parents[2]
_STRUCTURED_ARTIFACT_MARKERS = (
    "class ArtifactContract",
    "materialization_receipt",
    "typed artifact slot",
    "StructuredArtifact",
)
_PRODUCTION_ROOTS = (
    "packages/adapters/agent-runtime-opencode/agent_runtime_opencode",
    "packages/adapters/agent-runtime-contracts/agent_runtime_contracts",
    "packages/products/assurance-product/assurance_product",
    "packages/capabilities",
    "packages/framework/graph-engine/graph_engine",
)
_REMOVED_LIVE_MARKERS = (
    "OpenCodeHandler",
    "AA_CHECKPOINT_R_AUTH_FILE",
    "AA_CHECKPOINT_R_PROVIDER_CONFIG",
)


def test_installed_live_rows_have_valid_input_fixtures() -> None:
    from assurance_product.agent_contracts import all_feature_agent_contracts
    from tests.product.checkpoint_r_support import installed_contract_input, installed_live_agent_rows

    contracts = all_feature_agent_contracts()
    rows = installed_live_agent_rows()
    assert len(rows) == 33
    assert tuple(row.contract_id for row in rows) == tuple(sorted(contracts))
    for row in rows:
        payload = installed_contract_input(contracts[row.contract_id])
        assert isinstance(payload, contracts[row.contract_id].input_model)


def test_protected_preflight_fails_closed_without_live_inputs(monkeypatch) -> None:
    monkeypatch.delenv("AA_CHECKPOINT_R_LIVE", raising=False)
    with pytest.raises(CheckpointRPreflightError, match="AA_CHECKPOINT_R_LIVE"):
        require_checkpoint_r_preflight()


def test_candidate_inventory_is_exact(candidate_inventory) -> None:
    assert len(candidate_inventory.agent_contracts) == 33
    assert len(candidate_inventory.agent_occurrences) == 35
    assert len(candidate_inventory.semantic_contracts) == 41
    assert len(candidate_inventory.attempt_occurrences) == 44
    assert candidate_inventory.agent_contract_ids == installed_agent_contract_ids()
    assert candidate_inventory.runtime_binding_digests == locked_runtime_binding_digests()


def test_all_fourteen_roots_select_langgraph() -> None:
    from assurance_product.application import ENTRYPOINT_AGENT_CONTRACT_IDS
    from assurance_product.models import PRODUCT_ENTRYPOINTS

    assert set(ENTRYPOINT_AGENT_CONTRACT_IDS) == set(PRODUCT_ENTRYPOINTS)
    assert set(PRODUCT_ENTRYPOINTS) == set(_LANGGRAPH)
    assert set(_LEFTOVER) == set()
    for name in _T5A_LANGGRAPH:
        assert ENTRYPOINT_AGENT_CONTRACT_IDS[name] == ()
    for name in _T5B_LANGGRAPH | _T5C_LANGGRAPH | _T5D_LANGGRAPH:
        assert ENTRYPOINT_AGENT_CONTRACT_IDS[name]


def test_checkpoint_r_records_candidate_lock_and_revision(
    candidate_inventory,
    opencode_composition,
) -> None:
    from assurance_product.product import (
        product_graph_manifest,
        product_lock_from_composition,
    )
    from assurance_product.agent_contracts import all_feature_agent_contracts
    from assurance_product.runtime_bindings import (
        authenticate_raw_agent_runtime_bindings,
        raw_agent_runtime_binding_rows,
    )

    composition = opencode_composition
    product_lock = product_lock_from_composition(composition)
    manifest = product_graph_manifest(composition, product_lock)
    rows = raw_agent_runtime_binding_rows(composition)
    authenticated = authenticate_raw_agent_runtime_bindings(
        rows,
        all_feature_agent_contracts(),
        adapter="opencode",
    )

    assert len(candidate_inventory.candidate_sha) == 40
    assert len(product_lock.digest) == 64
    assert len(manifest.revision.revision_id) == 64
    assert manifest.revision.product_lock_digest == product_lock.digest
    assert composition.lock.digest
    assert len(authenticated) == 33
    assert {row.adapter for row in authenticated} == {"opencode"}
    assert {row.provider for row in authenticated} == {"opencode"}
    assert len(composition.semantic_attempt_contracts) == 41
    assert tuple(row.contract_id for row in authenticated) == candidate_inventory.agent_contract_ids


def test_checkpoint_r_proves_no_structured_artifact_pipeline() -> None:
    hits: list[str] = []
    for root in _PRODUCTION_ROOTS:
        path = _REPO_ROOT / root
        files = path.rglob("*.py") if path.is_dir() else ()
        for file in files:
            if "tests" in file.parts:
                continue
            text = file.read_text(encoding="utf-8")
            for marker in _STRUCTURED_ARTIFACT_MARKERS:
                if marker in text:
                    hits.append(f"{file.relative_to(_REPO_ROOT)}:{marker}")
    assert hits == []
    assert not (_REPO_ROOT / "scripts" / "opencode_structured_output_eligibility_probe.py").exists()
    assert not (
        _REPO_ROOT / "tests" / "agent_runtime" / "test_opencode_structured_output_eligibility_probe.py"
    ).exists()
    opencode = _REPO_ROOT / "packages" / "adapters" / "agent-runtime-opencode" / "agent_runtime_opencode"
    adapter_text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(opencode.rglob("*.py")))
    assert "format.type" not in adapter_text
    assert 'format": "json_schema"' not in adapter_text
    assert "json_schema" not in adapter_text


def test_cutover_live_helpers_are_absent() -> None:
    support = (_REPO_ROOT / "tests" / "product" / "checkpoint_r_support.py").read_text(encoding="utf-8")
    live = (_REPO_ROOT / "tests" / "product" / "test_checkpoint_r_live.py").read_text(encoding="utf-8")
    script = (_REPO_ROOT / "scripts" / "checkpoint_r.sh").read_text(encoding="utf-8")
    workflow = (_REPO_ROOT / ".github" / "workflows" / "checkpoint-r.yml").read_text(encoding="utf-8")
    surfaces = "\n".join((support, live, script, workflow))
    for marker in _REMOVED_LIVE_MARKERS:
        assert marker not in surfaces
    defined = {
        node.name
        for node in ast.walk(ast.parse(Path(__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef)
    }
    assert "test_live_opencode_cutover_binding_records_checkpoint_r" not in defined
    assert "test_checkpoint_r_inventory_is_33_33_34_41_43" not in defined
    assert "skipif" not in live
    assert "xfail" not in live
    assert "pytest.skip" not in live
    assert "waiver" not in live.lower()
    assert "fixture-model" not in live
    assert "fixture-model" not in script
    assert "CHECKPOINT_R_OPENCODE_AUTH_JSON" in script
    assert "CHECKPOINT_R_OPENCODE_PROVIDER_JSON" in script
    assert "CHECKPOINT_R_OPENCODE_BINARY_SHA256" in script
    assert "CHECKPOINT_R_SERVER_SECRET" in script
    assert "checkpoint_r_live" in live
    assert "ProductRuntimePorts" in support
    assert "len(agents) + len(extras)" not in support
    assert "len(agents)+2" not in support.replace(" ", "")
    assert "tests/product/fixtures/deployment/checkpoint-r-opencode.yaml" in script
    assert "tests/product/fixtures/deployment/opencode.yaml" not in script
    assert 'cd "$project_root"' in script
    assert "AA_CHECKPOINT_R_PROJECT_ROOT" in script
    assert "apply_live_deployment_overrides" in script
    assert "runs-on:" in workflow and "checkpoint-r" in workflow
    assert "environment:" in workflow and "checkpoint-r" in workflow


def test_count_exact_agent_occurrences_uses_graph_bound_multiset(
    graph_bound_contract_ids,
) -> None:
    assert count_exact_agent_occurrences(graph_bound_contract_ids) == 35
    assert count_exact_agent_occurrences() == 35
    source = (_REPO_ROOT / "tests" / "product" / "checkpoint_r_support.py").read_text(encoding="utf-8")
    assert "return len(agents) + len(extras)" not in source


def test_checkpoint_r_live_package_is_not_the_30s_fixture() -> None:
    import yaml

    live = yaml.safe_load(
        (
            _REPO_ROOT / "tests" / "product" / "fixtures" / "deployment" / "checkpoint-r-opencode.yaml"
        ).read_text(encoding="utf-8")
    )
    fixture = yaml.safe_load(
        (_REPO_ROOT / "tests" / "product" / "fixtures" / "deployment" / "opencode.yaml").read_text(
            encoding="utf-8"
        )
    )
    binding = live["adapter_binding"]
    assert binding["observation_horizon_seconds"] == LIVE_OBSERVATION_HORIZON_SECONDS
    assert binding["request_timeout_seconds"] == 300
    assert binding["max_response_bytes"] == 4_000_000
    assert binding["observation_horizon_seconds"] > fixture["adapter_binding"]["observation_horizon_seconds"]
    assert (
        live["request_policies"]["assurance.product.agent.request.default"]["max_output_bytes"] == 4_000_000
    )


def test_seed_change_workspace_writes_required_files_and_fails_closed(tmp_path: Path) -> None:
    from tests.product.checkpoint_r_support import require_seeded_workspace
    from tests.product.cli_support import write_project_dir

    project = write_project_dir(tmp_path / "project")
    written = seed_change_workspace(project)
    assert set(written) == set(required_change_files())
    require_seeded_workspace(project)
    for relative in written:
        path = project.joinpath(*relative.split("/"))
        assert path.is_file()
        assert not path.is_symlink()
    missing = project / "qa" / "changes" / "CH-R-001" / "cases" / "demo" / "case.yaml"
    missing.unlink()
    with pytest.raises(CheckpointRPreflightError, match="missing required change-local files"):
        require_seeded_workspace(project)
