"""Shared identifiers for the four generation families."""

from __future__ import annotations

from typing import Literal, cast

LayerName = Literal["api", "e2e", "fuzz", "performance"]
CaseType = Literal["API", "E2E", "Fuzz", "Performance"]
PlanCheckId = Literal["l1_path", "shared_factory", "assert_ideal", "capability_keys"]

LAYER_NAMES: tuple[LayerName, ...] = ("api", "e2e", "fuzz", "performance")
GENERATION_FAMILIES: tuple[LayerName, ...] = LAYER_NAMES


def validate_selected_families(values: tuple[str, ...] | list[str]) -> tuple[LayerName, ...]:
    families = tuple(values)
    if not families:
        raise ValueError("selected_test_families must be a non-empty family tuple")
    unknown = [item for item in families if item not in LAYER_NAMES]
    if unknown:
        raise ValueError(f"unknown generation family: {unknown[0]}")
    if len(families) != len(set(families)):
        raise ValueError("selected_test_families must not contain duplicates")
    return cast(tuple[LayerName, ...], families)


CASE_TYPES: tuple[CaseType, ...] = ("API", "E2E", "Fuzz", "Performance")
PLAN_CHECK_IDS: tuple[PlanCheckId, ...] = (
    "l1_path",
    "shared_factory",
    "assert_ideal",
    "capability_keys",
)
KNOWN_PLAN_CHECK_IDS = frozenset(PLAN_CHECK_IDS)
