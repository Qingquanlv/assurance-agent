from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from assurance_agent.eval.types import DatasetSample
from assurance_agent.exceptions import AaError

_SKIP_DIRS = {"_candidates", "calibration", "_test"}


def load_dataset(dataset_dir: Path) -> list[DatasetSample]:
    if not dataset_dir.is_dir():
        raise AaError(f"dataset dir not found: {dataset_dir}")
    samples: list[DatasetSample] = []
    for path in sorted(dataset_dir.rglob("*.y*ml")):
        if any(part in _SKIP_DIRS for part in path.relative_to(dataset_dir).parts[:-1]):
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as err:
            raise AaError(f"invalid YAML in {path.name}: {err}") from err
        if not isinstance(raw, dict):
            raise AaError(f"sample {path.name} is not a mapping")
        raw.setdefault("suite", dataset_dir.name)
        try:
            samples.append(DatasetSample.model_validate(raw))
        except ValidationError as err:
            raise AaError(f"sample {path.name} failed schema: {err}") from err
    if not samples:
        raise AaError(f"no samples found in {dataset_dir}")
    samples.sort(key=lambda s: s.id)
    return samples


def load_for_run(
    dataset_dir: Path,
    *,
    sample_id: str | None = None,
    tags: list[str] | None = None,
    human_only: bool = False,
    max_samples: int | None = None,
) -> list[DatasetSample]:
    samples = load_dataset(dataset_dir)
    if sample_id is not None:
        selected = [s for s in samples if s.id == sample_id]
        if not selected:
            raise AaError(f"sample not found: {sample_id}")
        return selected
    if human_only:
        samples = [s for s in samples if s.annotation_source == "human"]
    if tags:
        wanted = set(tags)
        samples = [s for s in samples if wanted.issubset(set(s.tags))]
    if max_samples is not None:
        samples = samples[:max_samples]
    if not samples:
        raise AaError(f"no samples matched filters in {dataset_dir}")
    return samples
