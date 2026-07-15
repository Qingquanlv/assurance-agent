from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.eval.dataset_loader import load_dataset, load_for_run
from assurance_agent.eval.types import DatasetSample
from assurance_agent.exceptions import AaError


def test_load_dataset_parses_and_validates(dataset_dir: Path, make_sample) -> None:
    make_sample(dataset_dir, "WC-001", input={"change_id": "eval-sample-001"}, expected={})
    make_sample(dataset_dir, "WC-002", input={"change_id": "eval-sample-002"}, expected={})
    samples = load_dataset(dataset_dir)
    assert [s.id for s in samples] == ["WC-001", "WC-002"]
    assert all(isinstance(s, DatasetSample) for s in samples)
    assert samples[0].annotation_source == "synthetic"  # 默认


def test_load_dataset_empty_dir_raises(dataset_dir: Path) -> None:
    with pytest.raises(AaError, match="no samples"):
        load_dataset(dataset_dir)


def test_load_dataset_invalid_yaml_raises(dataset_dir: Path, make_sample) -> None:
    (dataset_dir / "bad.yaml").write_text("id: [unclosed", encoding="utf-8")
    with pytest.raises(AaError, match="bad.yaml"):
        load_dataset(dataset_dir)


def test_load_dataset_missing_id_raises(dataset_dir: Path) -> None:
    (dataset_dir / "x.yaml").write_text("suite: workflow-case\n", encoding="utf-8")
    with pytest.raises(AaError):
        load_dataset(dataset_dir)


def test_load_for_run_filters_by_sample_id(dataset_dir: Path, make_sample) -> None:
    make_sample(dataset_dir, "WC-001", input={}, expected={})
    make_sample(dataset_dir, "WC-002", input={}, expected={})
    got = load_for_run(dataset_dir, sample_id="WC-002")
    assert [s.id for s in got] == ["WC-002"]


def test_load_for_run_unknown_sample_id_raises(dataset_dir: Path, make_sample) -> None:
    make_sample(dataset_dir, "WC-001", input={}, expected={})
    with pytest.raises(AaError, match="WC-999"):
        load_for_run(dataset_dir, sample_id="WC-999")


def test_load_for_run_human_only_and_tags_and_max(dataset_dir: Path, make_sample) -> None:
    make_sample(dataset_dir, "WC-001", input={}, expected={}, annotation_source="human", tags=["smoke"])
    make_sample(dataset_dir, "WC-002", input={}, expected={}, annotation_source="synthetic", tags=["smoke"])
    make_sample(dataset_dir, "WC-003", input={}, expected={}, annotation_source="human", tags=["slow"])
    human_smoke = load_for_run(dataset_dir, human_only=True, tags=["smoke"])
    assert [s.id for s in human_smoke] == ["WC-001"]
    capped = load_for_run(dataset_dir, max_samples=2)
    assert len(capped) == 2
