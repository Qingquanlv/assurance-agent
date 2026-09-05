from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PRODUCT_TESTS = REPO / "tests" / "product"
BENCHMARK_ROOT = REPO / "benchmark" / "assurance-product"
MANIFEST_PATH = BENCHMARK_ROOT / "manifest.json"

RETAINED_COMPARISON_ASSERTIONS = {
    "STOP": (
        "tests/product/test_stop_and_interrupts.py",
        ("test_revision_mismatch_rejects_drifted_resume",),
    ),
    "interrupt": (
        "tests/product/test_stop_and_interrupts.py",
        (
            "test_invalid_resume_input_fails",
            "test_interrupt_runtime_lives_under_the_change_without_tree_store",
        ),
    ),
    "replay": (
        "tests/product/test_replay_properties.py",
        ("test_publish_replay_matches_uninterrupted_projection_for_every_ordered_crash_subset",),
    ),
    "coverage": (
        "tests/product/test_coverage_loop.py",
        (
            "test_coverage_insufficient_reenters_the_shared_case_flow",
            "test_exhausted_coverage_budget_stops_without_report_or_retro",
        ),
    ),
    "healing": (
        "tests/product/test_issue_healing_flow.py",
        (
            "test_applied_test_repair_is_the_only_path_to_rerun",
            "test_fix_proposal_output_cannot_parse_as_applied_repair",
        ),
    ),
    "report": (
        "tests/product/test_report_flow.py",
        ("test_report_is_mandatory_on_success",),
    ),
    "retro": (
        "tests/product/test_archive_retro_improvement.py",
        (
            "test_full_ends_at_achieved_without_archive",
            "test_retro_and_improvement_are_independent_entrypoints",
        ),
    ),
    "improvement": (
        "tests/product/test_archive_retro_improvement.py",
        ("test_improvement_evaluate_export_and_rollback_are_independent",),
    ),
}

DELETED_COMPARISON_PATHS = (
    "tests/product/test_comparison_dispositions.py",
    "tests/product/test_comparison_isolation.py",
    "tests/product/test_comparison_matrix.py",
    "tests/product/fixtures/comparison/legacy",
    "benchmark/assurance-product/compare.py",
    "benchmark/assurance-product/comparison-manifest.json",
    "benchmark/assurance-product/generate_comparison_fixtures.py",
    "benchmark/assurance-product/run-comparison.sh",
    "benchmark/assurance-product/run_comparison.py",
)


def _top_level_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}


@pytest.fixture
def repo_root() -> Path:
    return REPO


def test_retained_comparison_assertions_map_to_product_tests(repo_root: Path) -> None:
    assert tuple(RETAINED_COMPARISON_ASSERTIONS) == (
        "STOP",
        "interrupt",
        "replay",
        "coverage",
        "healing",
        "report",
        "retro",
        "improvement",
    )
    for category, (relative, names) in RETAINED_COMPARISON_ASSERTIONS.items():
        path = repo_root / relative
        assert path.is_file(), f"{category} home missing: {relative}"
        defined = _top_level_functions(path)
        missing = tuple(name for name in names if name not in defined)
        assert missing == (), f"{category} missing product tests: {missing}"


def test_comparison_only_surface_is_removed(repo_root: Path) -> None:
    remaining = tuple(relative for relative in DELETED_COMPARISON_PATHS if (repo_root / relative).exists())
    assert remaining == ()
    assert not (repo_root / "tests/product/fixtures/comparison").exists()
    assert not (repo_root / "tests/phase5").exists()
    assert not (repo_root / "benchmark/assurance-product-phase5").exists()


def test_final_live_manifest_has_exactly_one_opencode_item(repo_root: Path) -> None:
    document = json.loads((repo_root / MANIFEST_PATH.relative_to(repo_root)).read_text(encoding="utf-8"))
    assert document["schema_version"] == "1"
    items = document["items"]
    assert isinstance(items, list)
    assert len(items) == 1
    item = items[0]
    assert item["sut_item_id"] == "RET-dept-management"
    assert item["id"] == "opencode-ret-dept-management"
    assert item["product"] == "assurance-opencode"
    assert item["adapter_binding"]["protocol_profile"] == "opencode-http-v1"
    encoded = json.dumps(item)
    assert "cursor" not in encoded.lower()
    assert all("cursor" not in json.dumps(entry).lower() for entry in items)


def test_cursor_adapter_packaging_and_live_entries_are_absent(repo_root: Path) -> None:
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    items = document["items"]
    assert all(entry.get("adapter_binding", {}).get("protocol_profile") != "cursor" for entry in items)
    assert all("cursor" not in str(entry.get("id", "")).lower() for entry in items)
    assert not (repo_root / "packages/adapters/agent-runtime-cursor").exists()
    packaging = repo_root / "tests/product/test_product_packaging.py"
    providers = repo_root / "tests/product/test_product_providers.py"
    cursor_fixture = repo_root / "tests/product/fixtures/deployment/cursor.yaml"
    assert packaging.is_file()
    assert providers.is_file()
    assert not cursor_fixture.exists()
    assert "assurance-cursor" not in packaging.read_text(encoding="utf-8")
    assert "agent-runtime-cursor" not in providers.read_text(encoding="utf-8")


def test_final_benchmark_keeps_runner_and_projection_layout(repo_root: Path) -> None:
    assert (BENCHMARK_ROOT / "run-opencode.sh").is_file()
    assert not (BENCHMARK_ROOT / "run-cursor.sh").exists()
    assert (BENCHMARK_ROOT / "run_item.py").is_file()
    assert (BENCHMARK_ROOT / "eval.py").is_file()
    assert (BENCHMARK_ROOT / "projection.py").is_file()
    assert (BENCHMARK_ROOT / "schemas" / "behavioral-projection-v1.json").is_file()
    assert PRODUCT_TESTS.is_dir()
