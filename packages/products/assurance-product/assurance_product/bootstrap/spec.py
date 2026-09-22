from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from assurance_product.bootstrap.contracts import RunSpecV1

_ALLOWED_OVERRIDES = frozenset({"requirement", "candidate_test_families", "case_modules"})


class SpecOverrideError(ValueError):
    pass


def load_run_spec(path: Path) -> RunSpecV1:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise SpecOverrideError(f"run-spec is not readable YAML: {path}") from error
    if not isinstance(raw, dict):
        raise SpecOverrideError("run-spec must be a mapping")
    try:
        return RunSpecV1.model_validate(raw)
    except Exception as error:
        raise SpecOverrideError(str(error)) from error


def merge_run_spec(base: RunSpecV1, overrides: Mapping[str, object]) -> RunSpecV1:
    unknown = set(overrides) - _ALLOWED_OVERRIDES
    if unknown:
        raise SpecOverrideError("unsupported override keys: " + ", ".join(sorted(unknown)))
    payload = base.model_dump(mode="json")
    for key in _ALLOWED_OVERRIDES:
        if key in overrides:
            payload[key] = overrides[key]
    try:
        return RunSpecV1.model_validate(payload)
    except Exception as error:
        raise SpecOverrideError(str(error)) from error
