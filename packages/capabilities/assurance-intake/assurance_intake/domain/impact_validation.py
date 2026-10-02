"""Validate an inventory against authenticated context and derive required families."""

from __future__ import annotations

from assurance_intake.contracts.common import TEST_FAMILY_ORDER, TestFamily
from assurance_intake.contracts.impact import ChangeImpactInventoryV1

_KIND_FAMILY: dict[str, TestFamily] = {
    "api": "api",
    "journey": "e2e",
    "role": "api",
    "data_constraint": "api",
}


def validate_inventory_references(
    inventory: ChangeImpactInventoryV1,
    *,
    resolvable: frozenset[str],
    seed_ids: frozenset[str],
    capability_leafs: frozenset[str],
) -> None:
    """Every cited id must resolve to sealed context; every seed must be handled."""

    errors: list[str] = []
    for row in inventory.rows:
        for label, ids in (
            ("change_evidence_ids", row.change_evidence_ids),
            ("expected_basis_ids", row.expected_basis_ids),
            ("assets.case_ids", row.assets.case_ids),
            ("assets.problem_ids", row.assets.problem_ids),
        ):
            unknown = sorted(set(ids) - resolvable)
            if unknown:
                errors.append(f"{row.row_id}: unresolvable {label}: {unknown}")
        unknown_leafs = sorted(set(row.assets.factory_leafs) - capability_leafs)
        if unknown_leafs:
            errors.append(f"{row.row_id}: assets.factory_leafs outside the typed catalog: {unknown_leafs}")
        behavior = row.affected_behavior
        if (
            behavior.kind == "data_constraint"
            and row.disposition != "capability_gap"
            and behavior.key not in capability_leafs
        ):
            errors.append(
                f"{row.row_id}: data_constraint key is not a typed leaf; declare capability_gap: {behavior.key}"
            )
    for exclusion in inventory.exclusions:
        if exclusion.seed_id not in seed_ids:
            errors.append(f"exclusion cites unknown seed: {exclusion.seed_id}")
    handled = {seed_id for row in inventory.rows for seed_id in row.change_evidence_ids}
    handled.update(item.seed_id for item in inventory.exclusions)
    unhandled = sorted(seed_ids - handled)
    if unhandled:
        errors.append(f"seeds without an impact row or exclusion: {unhandled}")
    if errors:
        raise ValueError("; ".join(errors))


def validate_inventory_closed_keys(
    inventory: ChangeImpactInventoryV1,
    *,
    journey_keys: frozenset[str],
) -> None:
    """Journey rows must name an authenticated journey unless they declare a capability gap."""

    errors = [
        f"{row.row_id}: journey key is not an authenticated journey; declare capability_gap: {row.affected_behavior.key}"
        for row in inventory.rows
        if row.affected_behavior.kind == "journey"
        and row.disposition != "capability_gap"
        and row.affected_behavior.key not in journey_keys
    ]
    if errors:
        raise ValueError("; ".join(errors))


def impact_required_families(inventory: ChangeImpactInventoryV1) -> tuple[TestFamily, ...]:
    """Families that closed rows need; open rows never retain a family."""

    open_row_ids = {row.row_id for row in inventory.open_rows()}
    families = {
        _KIND_FAMILY[row.affected_behavior.kind] for row in inventory.rows if row.row_id not in open_row_ids
    }
    return tuple(family for family in TEST_FAMILY_ORDER if family in families)


__all__ = ["impact_required_families", "validate_inventory_closed_keys", "validate_inventory_references"]
