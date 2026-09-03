from __future__ import annotations

from pathlib import Path

from tests.architecture.legacy_import_inventory import (
    FORBIDDEN_MODULES,
    FORBIDDEN_SYMBOLS,
    allowlist_is_explicit,
    load_explicit_allowlist,
    scan_legacy_imports,
    unallowlisted_hits,
)


def test_allowlist_is_generated_from_explicit_task_inventories() -> None:
    allowlist = load_explicit_allowlist()
    assert allowlist_is_explicit(allowlist)
    assert "packages/products/assurance-product/assurance_product/application.py" in (
        allowlist.retained_implementations
    )
    assert (
        "packages/products/assurance-product/assurance_product/cli.py" in allowlist.retained_implementations
    )
    assert (
        "packages/products/assurance-product/assurance_product/status.py"
        in allowlist.retained_implementations
    )
    assert "packages/framework/graph-engine/graph_engine/graph/compiler.py" in (
        allowlist.retained_implementations
    )
    assert "packages/capabilities/assurance-intake/assurance_intake/resources/workflow/module.yaml" in (
        allowlist.task8_consumers
    )
    assert "packages/framework/graph-engine/graph_engine/runtime/engine.py" in (
        allowlist.task9_characterization
    )
    assert "tests/product/product_runner.py" in allowlist.task8_consumers
    assert "tests/product/product_runner.py" not in allowlist.paths
    assert "tests/product/composition_harness.py" not in allowlist.paths
    assert not any("*" in path for path in allowlist.paths)


def test_scanner_enumerates_required_compiler_and_runtime_symbols() -> None:
    assert FORBIDDEN_SYMBOLS == {
        "WorkflowModuleDef",
        "GraphDef",
        "NodeDef",
        "CompiledWorkflow",
        "compile_workflow",
        "assemble_product_workflow",
    }
    assert FORBIDDEN_MODULES == {
        "graph_engine.graph.expressions",
        "graph_engine.graph.input_projection",
        "graph_engine.graph.output_projection",
    }


def test_scan_fails_closed_on_unreadable_or_unparsable_python(tmp_path: Path) -> None:
    broken = tmp_path / "packages" / "broken.py"
    broken.parent.mkdir(parents=True)
    broken.write_text("def oops(:\n", encoding="utf-8")
    hits = scan_legacy_imports(tmp_path)
    assert any(hit.kind == "parse-error" for hit in hits)
    allowlist = load_explicit_allowlist()
    assert unallowlisted_hits(hits, allowlist)


def test_legacy_imports_are_only_on_explicit_allowlist() -> None:
    allowlist = load_explicit_allowlist()
    hits = scan_legacy_imports()
    unexpected = unallowlisted_hits(hits, allowlist)
    paths = tuple(sorted({hit.path for hit in unexpected}))
    assert unexpected == (), paths
