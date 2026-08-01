from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import yaml
import pytest

from assurance_agent.artifacts.models.plan_checks import PlanCheckDocument
from assurance_agent.eval.fixtures import (
    load_tier,
    seed_change,
    validate_tier_for_selection,
    write_fixture_lock,
)
from assurance_agent.eval.types import FixtureImportDef, FixtureResets, TierManifest
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.state import verify_state_integrity, write_state
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
from assurance_agent.artifacts.models import WorkflowState


def _write_synth_fixtures(root: Path) -> Path:
    fixtures = root / "eval-fixtures"
    sample = fixtures / "samples" / "eval-sample-001"
    sample.mkdir(parents=True)
    (sample / "proposal.md").write_text("# proposal\n", encoding="utf-8")
    (sample / "cases").mkdir()
    (sample / "cases" / "case.yaml").write_text("cases: []\n", encoding="utf-8")
    write_state(
        sample,
        WorkflowState.model_validate(
            {
                "phases": {
                    "case-design": {"status": "done"},
                    "execution": {"status": "pending"},
                }
            }
        ),
    )
    # Also keep a copy named workflow-state.yaml already via write_state
    tiers = fixtures / "tiers"
    tiers.mkdir()
    (tiers / "L0-case-seed.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "L0-case-seed",
                "paths": ["proposal.md", "cases/case.yaml", "workflow-state.yaml"],
                "resets": {
                    "workflow_state": {
                        "phases.case-design.status": "done",
                        "phases.execution.status": "pending",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (tiers / "L3-run-seed.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "L3-run-seed",
                "extends": "L0-case-seed",
                "paths": ["tests/api/test_synth.py"],
                "resets": {"workflow_state": {"phases.api_codegen.status": "done"}},
            }
        ),
        encoding="utf-8",
    )
    (sample / "tests" / "api").mkdir(parents=True)
    (sample / "tests" / "api" / "test_synth.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    write_fixture_lock(fixtures, {"fixture-001": "samples/eval-sample-001"})
    return fixtures


def test_load_tier_extends_merges_paths_and_resets(tmp_path: Path) -> None:
    fixtures = _write_synth_fixtures(tmp_path)
    tier = load_tier(fixtures, "L3-run-seed")
    assert "proposal.md" in tier.paths
    assert "tests/api/test_synth.py" in tier.paths
    assert tier.resets.workflow_state["phases.api_codegen.status"] == "done"
    assert tier.resets.workflow_state["phases.execution.status"] == "pending"


def test_seed_change_preserves_state_integrity(tmp_path: Path) -> None:
    fixtures = _write_synth_fixtures(tmp_path)
    sut = tmp_path / "sut"
    sut.mkdir()
    result = seed_change(
        sut_sandbox=sut,
        change_id="eval-sample-001",
        tier_name="L3-run-seed",
        fixtures_root=fixtures,
        fixture_id="fixture-001",
        entrypoint=None,
    )
    change = result.change_dir
    assert result.import_manifest_path is None
    assert (change / "proposal.md").exists()
    assert (sut / "tests" / "api" / "test_synth.py").exists()
    assert verify_state_integrity(change) is None


def test_seed_change_rejects_fixture_drift(tmp_path: Path) -> None:
    fixtures = _write_synth_fixtures(tmp_path)
    (fixtures / "samples" / "eval-sample-001" / "proposal.md").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(AaError, match="fixture lock mismatch"):
        seed_change(
            sut_sandbox=tmp_path / "sut",
            change_id="eval-sample-001",
            tier_name="L3-run-seed",
            fixtures_root=fixtures,
            fixture_id="fixture-001",
        )


def test_load_tier_merges_imports_by_entrypoint(tmp_path: Path) -> None:
    fixtures = tmp_path / "eval-fixtures"
    tiers = fixtures / "tiers"
    tiers.mkdir(parents=True)
    (tiers / "parent.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "parent",
                "imports": {
                    "execute": {
                        "entrypoint": "execute",
                        "inputs": ["change:proposal.md"],
                        "completed": [
                            {
                                "path": "execute-workflow/bootstrap/bootstrap",
                                "graph": "bootstrap",
                                "node": "registry",
                                "outputs": ["change:workflow-state.yaml"],
                                "gate": "registry-gate",
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (tiers / "child.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "child",
                "extends": "parent",
                "imports": {
                    "execute": {
                        "entrypoint": "execute",
                        "inputs": ["change:.qa.yaml"],
                        "completed": [
                            {
                                "path": "execute-workflow/assurance/assurance",
                                "graph": "assurance",
                                "node": "fact-baseline",
                                "outputs": ["change:facts/fact-baseline.json"],
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    tier = load_tier(fixtures, "child")
    imp = tier.imports["execute"]
    assert "change:proposal.md" in imp.inputs
    assert "change:.qa.yaml" in imp.inputs
    nodes = {t.node for t in imp.completed}
    assert nodes == {"registry", "fact-baseline"}


def test_benchmark_api_review_imports_include_mechanical_predecessor() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    fixtures = repo_root / "benchmark" / "vue-fastapi-admin" / "eval-fixtures"
    expected_path = "execute-workflow/assurance/assurance/api/api-branch/review-cycle/api-plan-cycle"

    for tier_name in ("L1-plan-seed", "L2-api-codegen-seed", "L3-run-seed", "L3-run-done"):
        completed = load_tier(fixtures, tier_name).imports["execute"].completed
        review_index = next(
            index
            for index, task in enumerate(completed)
            if task.path == expected_path and task.node == "review"
        )
        checks = completed[review_index - 1]
        assert checks.path == expected_path, tier_name
        assert checks.node == "mechanical-plan-checks", tier_name
        assert checks.outputs == ["change:review/api-plan-checks.json"], tier_name

    evidence_path = fixtures / "samples" / "eval-sample-001" / "review" / "api-plan-checks.json"
    evidence = PlanCheckDocument.model_validate_json(evidence_path.read_text(encoding="utf-8"))
    assert evidence.status == "pass"


def test_benchmark_codegen_imports_attach_gate_to_precheck_not_codegen() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    fixtures = repo_root / "benchmark" / "vue-fastapi-admin" / "eval-fixtures"
    tier_names = (
        "L2-api-codegen-seed",
        "L2-e2e-codegen-seed",
        "L2-fuzz-codegen-seed",
        "L2-performance-codegen-seed",
        "L3-run-seed",
        "L3-run-done",
    )

    for tier_name in tier_names:
        completed = load_tier(fixtures, tier_name).imports["execute"].completed
        for index, task in enumerate(completed):
            if task.node != "codegen":
                continue
            precheck = completed[index - 1]
            layer = task.graph.removesuffix("-branch")
            precheck_node = "codegen-precheck"
            gate_suffix = "codegen-precondition-gate"
            assert precheck.path == task.path, tier_name
            assert precheck.node == precheck_node, tier_name
            assert precheck.gate == f"{layer}-{gate_suffix}", tier_name
            assert task.gate is None, tier_name


def test_benchmark_fixture_import_nodes_exist_in_packaged_schema() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    fixtures = repo_root / "benchmark" / "vue-fastapi-admin" / "eval-fixtures"
    compiled = compile_workflow(load_workflow_v2(repo_root), load_execution_contracts(repo_root))

    for tier_path in sorted((fixtures / "tiers").glob("*.yaml")):
        tier = load_tier(fixtures, tier_path.stem)
        for import_def in tier.imports.values():
            for task in import_def.completed:
                assert task.graph in compiled.schema.graphs, tier.name
                assert task.node in compiled.schema.graphs[task.graph].nodes, (
                    tier.name,
                    task.graph,
                    task.node,
                )


def _benchmark_fixtures() -> Path:
    return Path(__file__).resolve().parents[3] / "benchmark" / "vue-fastapi-admin" / "eval-fixtures"


@pytest.mark.parametrize(
    ("role_node", "graph", "path"),
    [
        (
            "applicability",
            "api-plan-cycle",
            "execute-workflow/assurance/assurance/api/api-branch/review-cycle/api-plan-cycle",
        ),
        (
            "review",
            "api-plan-cycle",
            "execute-workflow/assurance/assurance/api/api-branch/review-cycle/api-plan-cycle",
        ),
        (
            "mechanical-plan-checks",
            "api-plan-cycle",
            "execute-workflow/assurance/assurance/api/api-branch/review-cycle/api-plan-cycle",
        ),
        (
            "review-gate",
            "api-plan-cycle",
            "execute-workflow/assurance/assurance/api/api-branch/review-cycle/api-plan-cycle",
        ),
        (
            "review-cycle",
            "api-branch",
            "execute-workflow/assurance/assurance/api/api-branch",
        ),
        (
            "codegen-precheck",
            "api-branch",
            "execute-workflow/assurance/assurance/api/api-branch",
        ),
        (
            "codegen",
            "api-branch",
            "execute-workflow/assurance/assurance/api/api-branch",
        ),
        (
            "api",
            "assurance",
            "execute-workflow/assurance/assurance",
        ),
    ],
)
def test_pending_tier_rejects_selected_roles_direct_and_inherited(
    tmp_path: Path, role_node: str, graph: str, path: str
) -> None:
    fixtures = tmp_path / "eval-fixtures"
    tiers = fixtures / "tiers"
    tiers.mkdir(parents=True)
    sample = fixtures / "samples" / "s"
    sample.mkdir(parents=True)
    (sample / "proposal.md").write_text("x\n", encoding="utf-8")
    write_fixture_lock(fixtures, {"f": "samples/s"})
    (tiers / "parent.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "parent",
                "expected_layers": ["api"],
                "imports": {
                    "execute": {
                        "entrypoint": "execute",
                        "completed": [
                            {
                                "path": path,
                                "graph": graph,
                                "node": role_node,
                                "outputs": [],
                                "gate": "api-plan-review-gate" if role_node == "review" else None,
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (tiers / "child.yaml").write_text(
        yaml.safe_dump({"name": "child", "extends": "parent", "expected_layers": ["api"]}),
        encoding="utf-8",
    )
    child = load_tier(fixtures, "child")
    with pytest.raises(AaError, match="selected api"):
        validate_tier_for_selection(child, selected_layers=("api",), sample_root=sample)


@pytest.mark.parametrize(
    "artifact",
    [
        "review/api-plan-review.json",
        "review/api-plan-checks.json",
        "codegen/api-codegen-summary.md",
        "codegen/api-generated-files.json",
        "tests/api/test_api_management_api.py",
    ],
)
def test_pending_tier_rejects_forbidden_artifacts(artifact: str) -> None:
    fixtures = _benchmark_fixtures()
    sample = fixtures / "samples" / "eval-sample-001"
    tier = TierManifest(
        name="bad-pending",
        expected_layers=["api"],
        paths=[artifact],
        imports={"execute": FixtureImportDef(entrypoint="execute")},
    )
    with pytest.raises(AaError, match="forbidden selected artifact"):
        validate_tier_for_selection(tier, selected_layers=("api",), sample_root=sample)


def test_pending_tier_rejects_codegen_done_reset() -> None:
    tier = TierManifest(
        name="bad-reset",
        expected_layers=["api"],
        resets=FixtureResets(workflow_state={"phases.api-codegen.status": "done"}),
        imports={"execute": FixtureImportDef(entrypoint="execute")},
    )
    with pytest.raises(AaError, match="codegen done"):
        validate_tier_for_selection(tier, selected_layers=("api",))


def test_repo_paths_reject_unsafe_and_digest_mismatch(tmp_path: Path) -> None:
    fixtures = _write_synth_fixtures(tmp_path)
    tiers = fixtures / "tiers"
    (tiers / "bad-repo.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "bad-repo",
                "paths": ["proposal.md", "workflow-state.yaml"],
                "repo_paths": ["../escape.yaml"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AaError, match="unsafe repo_paths"):
        seed_change(
            sut_sandbox=tmp_path / "sut",
            change_id="eval-sample-001",
            tier_name="bad-repo",
            fixtures_root=fixtures,
            fixture_id="fixture-001",
        )


def test_domain_api_module_import_smoke_without_app_installed() -> None:
    sample = _benchmark_fixtures() / "samples" / "eval-sample-001"
    module_path = sample / "tests" / "testdata" / "domain" / "api.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    # Product deps must not be imported at module top-level.
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", None) or ""
            names = [alias.name for alias in node.names]
            assert not module.startswith("app.")
            assert "app" not in names
    spec = importlib.util.spec_from_file_location("fixture_domain_api", module_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert callable(mod.make_api)
    assert mod.MAKE_API.endswith("make_api")


@pytest.mark.parametrize(
    "tier_name",
    [
        "L2-api-codegen-pending",
        "L2-e2e-codegen-pending",
        "L2-fuzz-codegen-pending",
        "L2-performance-codegen-pending",
    ],
)
def test_pending_tiers_seed_without_selected_completion(tmp_path: Path, tier_name: str) -> None:
    fixtures = _benchmark_fixtures()
    sut = tmp_path / "sut"
    sut.mkdir()
    layer = tier_name.split("-")[1]
    result = seed_change(
        sut_sandbox=sut,
        change_id="eval-sample-001",
        tier_name=tier_name,
        fixtures_root=fixtures,
        fixture_id="eval-sample-001",
        entrypoint="execute",
        selected_layers=(layer,),  # type: ignore[arg-type]
    )
    assert (sut / ".aa" / "data-knowledge.yaml").is_file()
    assert (sut / "tests" / "testdata" / "domain" / "api.py").is_file()
    assert not (result.change_dir / "review" / f"{layer}-plan-review.json").exists()
    assert not (result.change_dir / "codegen").exists()
    private = {
        "api": "tests/api/test_api_management_api.py",
        "e2e": "tests/e2e/test_api_management_e2e.py",
        "fuzz": "tests/fuzz/test_api_fuzz.py",
        "performance": "tests/perf/locustfile_api.py",
    }[layer]
    assert not (sut / private).exists()


@pytest.mark.parametrize(
    "tier_name",
    [
        "L2-api-codegen-seed",
        "L2-e2e-codegen-seed",
        "L2-fuzz-codegen-seed",
        "L2-performance-codegen-seed",
        "L3-run-seed",
        "L3-run-done",
    ],
)
def test_complete_tiers_seed_without_dynamic_assurance_helper(tmp_path: Path, tier_name: str) -> None:
    fixtures = _benchmark_fixtures()
    # Dynamic helper must be gone.
    import assurance_agent.eval.fixtures as fixtures_mod

    assert not hasattr(fixtures_mod, "_ensure_assurance_seed_artifacts")
    sut = tmp_path / "sut"
    sut.mkdir()
    result = seed_change(
        sut_sandbox=sut,
        change_id="eval-sample-001",
        tier_name=tier_name,
        fixtures_root=fixtures,
        fixture_id="eval-sample-001",
        entrypoint="execute",
    )
    assert result.import_manifest_path is not None
    assert result.import_manifest_path.is_file()
    if "fuzz" in tier_name:
        assert (result.change_dir / "review" / "fuzz-plan-review.json").is_file()
        assert (result.change_dir / "review" / "fuzz-plan-checks.json").is_file()
    if "performance" in tier_name:
        assert (result.change_dir / "review" / "performance-plan-review.json").is_file()
        assert (result.change_dir / "review" / "performance-plan-checks.json").is_file()


def test_live_codegen_datasets_reference_pending_tiers_only() -> None:
    from assurance_agent.eval.dataset_loader import load_dataset
    from assurance_agent.eval.paths import datasets_dir
    from assurance_agent.eval.types import CODEGEN_SUITE_PENDING_TIERS

    repo = Path(__file__).resolve().parents[3]
    for suite_name, tier in CODEGEN_SUITE_PENDING_TIERS.items():
        samples = load_dataset(datasets_dir(repo, suite_name))
        assert samples
        assert {sample.input.get("fixture_tier") for sample in samples} == {tier}


def test_pending_chain_has_no_complete_ancestry() -> None:
    fixtures = _benchmark_fixtures()
    for tier_name in (
        "L2-api-codegen-pending",
        "L2-e2e-codegen-pending",
        "L2-fuzz-codegen-pending",
        "L2-performance-codegen-pending",
    ):
        tier = load_tier(fixtures, tier_name)
        assert tier.extends == "L1-assurance-input-ready"
        assert "L1-plan-seed" not in (tier.extends or "")
        assert tier.expected_layers
        # Expanded imports must not include selected codegen completion.
        for task in tier.imports.get("execute", FixtureImportDef(entrypoint="execute")).completed:
            assert task.node not in {"codegen", "codegen-precheck", "review", "applicability"}
