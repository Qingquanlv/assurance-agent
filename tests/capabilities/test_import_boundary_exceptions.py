from __future__ import annotations

from pathlib import Path

from tests.capabilities.import_boundary_exceptions import is_declared_cross_wheel_import


def test_shared_intake_domain_is_not_an_operation_exception() -> None:
    root = (
        Path(__file__).resolve().parents[2]
        / "packages/capabilities/assurance-generation/assurance_generation"
    )
    assert not is_declared_cross_wheel_import(
        root,
        root / "operations/codegen.py",
        "assurance_intake.domain.plan_codec",
    )
    assert not is_declared_cross_wheel_import(
        root,
        root / "operations/codegen.py",
        "assurance_intake.ops.case_design",
    )
    assert not is_declared_cross_wheel_import(
        root,
        root / "graphs/nodes.py",
        "assurance_intake.domain.plan_codec",
    )
    assert not is_declared_cross_wheel_import(
        root,
        root / "operations/codegen.py",
        "assurance_intake.ops.case_review.hooks",
    )
