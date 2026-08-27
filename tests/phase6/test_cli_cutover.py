from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from tests.phase5.test_product_packaging import read_wheel_metadata


@pytest.fixture
def built_product_wheel(tmp_path: Path) -> Path:
    import subprocess

    subprocess.run(
        [
            "uv",
            "build",
            "--package",
            "assurance-product",
            "--out-dir",
            str(tmp_path),
        ],
        check=True,
    )
    wheels = tuple(tmp_path.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_product_wheel_owns_only_final_aa(built_product_wheel: Path) -> None:
    metadata = read_wheel_metadata(built_product_wheel)
    assert metadata.entry_points["console_scripts"] == {"aa": "assurance_product.cli:main"}
    assert "aa-next" not in metadata.entry_points["console_scripts"]


def test_main_sets_aa_program_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from assurance_product.cli import app, main

    captured: dict[str, object] = {}

    def fake_main(*args: object, **kwargs: object) -> None:
        del args
        captured.update(kwargs)

    monkeypatch.setattr(app, "main", fake_main)
    main()
    assert captured["prog_name"] == "aa"


def test_help_identity_is_aa_not_aa_next() -> None:
    from assurance_product.cli import app

    assert app.__doc__ is not None
    assert "aa-next" not in app.__doc__
    assert "Phase 5" not in app.__doc__
    result = CliRunner().invoke(app, ["--help"], prog_name="aa")
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("Usage: aa")
    assert "aa-next" not in result.stdout
