from __future__ import annotations

from pathlib import Path

import pytest

from tests.product.test_binding_builder import _OPENCODE_MANIFEST

pytestmark = pytest.mark.usefixtures("installed_sources")


def test_bindings_build_writes_authenticated_wheel(cli_runner, tmp_path: Path):
    from assurance_product.binding_builder import build_deployment_wheel
    from assurance_product.cli import app

    output = tmp_path / "wheel-out"
    result = cli_runner.invoke(
        app,
        ["bindings", "build", "--json", "--manifest", str(_OPENCODE_MANIFEST), "--output-dir", str(output)],
    )
    assert result.exit_code == 0, result.output
    expected = build_deployment_wheel(_OPENCODE_MANIFEST, tmp_path / "direct")
    wheels = tuple(output.glob("*.whl"))
    assert len(wheels) == 1
    assert wheels[0].name == expected.wheel.name
    assert wheels[0].read_bytes() == expected.wheel.read_bytes()
    assert expected.distribution in result.output
    assert expected.declaration_path in result.output


def test_bindings_build_rejects_engine_root_and_secrets(cli_runner, tmp_path: Path):
    from assurance_product.cli import app

    output = tmp_path / "wheel-out"
    for extra in (
        ["--engine-root", str(tmp_path)],
        ["--project-dir", str(tmp_path)],
        ["--secret", "opencode.token=env:TOKEN"],
        ["--product", "assurance-opencode"],
    ):
        result = cli_runner.invoke(
            app,
            [
                "bindings",
                "build",
                "--manifest",
                str(_OPENCODE_MANIFEST),
                "--output-dir",
                str(output),
                *extra,
            ],
        )
        assert result.exit_code != 0, extra
        assert result.exit_code in {2, 40}


def test_bindings_build_rejects_nonempty_output(cli_runner, tmp_path: Path):
    from assurance_product.cli import app

    output = tmp_path / "occupied"
    output.mkdir()
    (output / "already.txt").write_text("no", encoding="utf-8")
    result = cli_runner.invoke(
        app,
        ["bindings", "build", "--manifest", str(_OPENCODE_MANIFEST), "--output-dir", str(output)],
    )
    assert result.exit_code == 40, result.output
