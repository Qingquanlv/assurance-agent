"""Deterministic validation for agent-authored plan outputs."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from assurance_kernel.verification.generated_entries import (
    LayerMappingRelation,
    MappingExtractionError,
    extract_layer_mapping,
)


class PlanOutputValidationError(ValueError):
    """An authored plan cannot satisfy its downstream parser contract."""


def validate_fuzz_plan_outputs(change_dir: Path) -> None:
    """Validate both Fuzz plans with the exact parser used by codegen precommit.

    The codegen validator consumes only ``fuzz-codegen-plan.md``.  Validating at
    the producer boundary makes an invalid relation retry the plan author
    instead of repeatedly invoking codegen with an unusable frozen input.
    """
    cases = _load_case_documents(change_dir)
    relations: dict[str, LayerMappingRelation] = {}
    for name in ("fuzz-plan.md", "fuzz-codegen-plan.md"):
        path = change_dir / "plans" / name
        try:
            text = path.read_text(encoding="utf-8")
            relations[name] = extract_layer_mapping(layer="fuzz", plan_text=text, cases=cases)
        except (OSError, UnicodeError, MappingExtractionError, ValueError) as exc:
            raise PlanOutputValidationError(f"{name}: {exc}") from exc

    authored = _mapping_identity(relations["fuzz-plan.md"])
    codegen = _mapping_identity(relations["fuzz-codegen-plan.md"])
    if authored != codegen:
        raise PlanOutputValidationError(
            "fuzz-plan.md and fuzz-codegen-plan.md must contain the same "
            "Case ID, test function, target file, and Schema Acquisition relation"
        )


def _load_case_documents(change_dir: Path) -> list[Mapping[str, object]]:
    paths = sorted((change_dir / "cases").glob("**/case.yaml"))
    if not paths:
        raise PlanOutputValidationError("no change:cases/**/case.yaml inputs found")
    cases: list[Mapping[str, object]] = []
    for path in paths:
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise PlanOutputValidationError(f"{path.relative_to(change_dir)}: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise PlanOutputValidationError(f"{path.relative_to(change_dir)} must contain a YAML mapping")
        cases.append(payload)
    return cases


def _mapping_identity(
    relation: LayerMappingRelation,
) -> tuple[tuple[tuple[str, str, str], ...], tuple[str, ...]]:
    entries = tuple(sorted((entry.case_id, entry.symbol, entry.target_file) for entry in relation.entries))
    return entries, tuple(sorted(relation.schema_case_ids))
