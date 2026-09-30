from __future__ import annotations

from pathlib import Path

from tests.capabilities.import_boundary_exceptions import is_declared_cross_wheel_import


def test_cross_wheel_operation_exception_is_exact_to_importer_and_module() -> None:
    root = (
        Path(__file__).resolve().parents[2]
        / "packages/capabilities/assurance-generation/assurance_generation"
    )
    imported = "assurance_intake.operations.plan_codec"
    assert is_declared_cross_wheel_import(root, root / "operations/codegen.py", imported)
    assert not is_declared_cross_wheel_import(root, root / "graphs/nodes.py", imported)
    assert not is_declared_cross_wheel_import(
        root,
        root / "operations/codegen.py",
        "assurance_intake.operations.finalize",
    )
