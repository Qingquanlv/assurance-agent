from pathlib import Path

import tomllib


def test_sample_pyproject_does_not_depend_on_assurance_agent() -> None:
    doc = tomllib.loads(Path("examples/minimal-product/pyproject.toml").read_text(encoding="utf-8"))
    deps = " ".join(doc["project"]["dependencies"])
    assert "assurance-kernel" in deps
    assert "assurance-agent" not in deps
