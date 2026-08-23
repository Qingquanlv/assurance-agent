from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_wheel_smoke_covers_exact_install_matrix(repo_root):
    script = (repo_root / "scripts/assurance_product_wheel_smoke_test.sh").read_text()
    assert "git archive HEAD" in script
    assert "base-no-adapter" in script
    assert "opencode-product" in script
    assert "cursor-product" in script
    assert "source-drift" in script
    assert "aa-next compile" in script
