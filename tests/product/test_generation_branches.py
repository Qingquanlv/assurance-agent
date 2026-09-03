from __future__ import annotations

import pytest

from assurance_generation.contracts.families import GENERATION_FAMILIES, validate_selected_families


@pytest.mark.parametrize("selected", [(), ("api", "api"), ("api", "mobile")])
def test_invalid_family_selection_fails_at_feature_input(selected: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        validate_selected_families(selected)


@pytest.mark.parametrize("family", list(GENERATION_FAMILIES))
def test_known_generation_families_are_accepted(family: str) -> None:
    assert validate_selected_families((family,)) == (family,)
