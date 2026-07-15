from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def dataset_dir(tmp_path: Path) -> Path:
    root = tmp_path / "eval" / "datasets" / "workflow-case"
    root.mkdir(parents=True)
    return root


def write_sample(root: Path, sample_id: str, **fields: object) -> Path:
    payload: dict[str, object] = {"id": sample_id, "suite": "workflow-case"}
    payload.update(fields)
    path = root / f"{sample_id}.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def make_sample():
    return write_sample
