"""Shared identifiers for the four assurance layers."""

from typing import Literal

LayerName = Literal["api", "e2e", "fuzz", "performance"]
CaseType = Literal["API", "E2E", "Fuzz", "Performance"]
PlanCheckId = Literal["l1_path", "shared_factory", "assert_ideal", "capability_keys"]

LAYER_NAMES: tuple[LayerName, ...] = ("api", "e2e", "fuzz", "performance")
CASE_TYPES: tuple[CaseType, ...] = ("API", "E2E", "Fuzz", "Performance")
PLAN_CHECK_IDS: tuple[PlanCheckId, ...] = (
    "l1_path",
    "shared_factory",
    "assert_ideal",
    "capability_keys",
)
KNOWN_PLAN_CHECK_IDS = frozenset(PLAN_CHECK_IDS)
