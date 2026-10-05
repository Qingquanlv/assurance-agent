from __future__ import annotations

from assurance_quality.contracts.surface import surface_readiness


def test_surface_readiness_requires_a_live_side_for_requested_families() -> None:
    assert surface_readiness(["e2e"], "live", "unused") == "ready"
    assert surface_readiness(["e2e"], "unavailable", "unused") == "not_ready"
    assert surface_readiness(["api"], "unused", "live") == "ready"
    assert surface_readiness(["fuzz", "performance"], "unused", "live") == "ready"
    assert surface_readiness(["api"], "unused", "unused") == "not_ready"
    assert surface_readiness([], "unused", "unused") == "ready"
    assert surface_readiness("api", "live", "live") == "not_ready"
    assert surface_readiness(["e2e"], "elsewhere", "live") == "not_ready"
