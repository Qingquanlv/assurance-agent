from __future__ import annotations

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from tests.phase4.wheel_isolation import REPO_ROOT

ASSURANCE_WHEEL_DEPENDENCIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("assurance-intake", ("graph-engine", "agent-runtime-contracts")),
    ("assurance-generation", ("graph-engine", "agent-runtime-contracts", "assurance-intake")),
    (
        "assurance-execution",
        ("graph-engine", "agent-runtime-contracts", "assurance-intake", "assurance-generation"),
    ),
    (
        "assurance-healing",
        (
            "graph-engine",
            "agent-runtime-contracts",
            "assurance-intake",
            "assurance-generation",
            "assurance-execution",
        ),
    ),
    (
        "assurance-quality",
        (
            "graph-engine",
            "agent-runtime-contracts",
            "assurance-intake",
            "assurance-generation",
            "assurance-execution",
            "assurance-healing",
        ),
    ),
    (
        "assurance-improvement",
        (
            "graph-engine",
            "agent-runtime-contracts",
            "assurance-intake",
            "assurance-generation",
            "assurance-execution",
            "assurance-healing",
            "assurance-quality",
        ),
    ),
)

_ASSURANCE_DAG: tuple[str, ...] = (
    "assurance-intake",
    "assurance-generation",
    "assurance-execution",
    "assurance-healing",
    "assurance-quality",
    "assurance-improvement",
)
_FORBIDDEN_DISTRIBUTIONS = frozenset({"assurance-agent", "assurance-kernel"})
_PHASE4_RUNTIME_ORDER: tuple[str, ...] = (
    "graph-engine",
    "agent-runtime-contracts",
    *_ASSURANCE_DAG,
)
_PHASE4_RUNTIME_INDEX = {name: index for index, name in enumerate(_PHASE4_RUNTIME_ORDER)}
_SMOKE_SCRIPT = REPO_ROOT / "scripts" / "assurance_capability_wheel_smoke_test.sh"
_PACKAGING_SMOKE = REPO_ROOT / "scripts" / "packaging_smoke_test.sh"
_CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


@pytest.mark.parametrize(
    "distribution, dependencies",
    ASSURANCE_WHEEL_DEPENDENCIES,
)
def test_wheel_metadata_has_exact_assurance_dependencies(
    distribution: str, dependencies: tuple[str, ...]
) -> None:
    assert phase4_runtime_dependency_names(distribution) == dependencies


@pytest.mark.parametrize(
    "distribution, dependencies",
    ASSURANCE_WHEEL_DEPENDENCIES,
)
def test_wheel_metadata_requires_declared_edges_and_forbids_legacy_paths(
    distribution: str, dependencies: tuple[str, ...]
) -> None:
    requirements = _installed_requirements(distribution)
    names = tuple(canonicalize_name(requirement.name) for requirement in requirements)
    for dependency in dependencies:
        assert dependency in names
    assert not _FORBIDDEN_DISTRIBUTIONS.intersection(names)
    assert all(requirement.url is None for requirement in requirements)
    assert not _downstream_assurance_names(distribution).intersection(names)


def test_committed_head_isolation_script_is_the_release_authority() -> None:
    assert _SMOKE_SCRIPT.is_file()
    text = _SMOKE_SCRIPT.read_text(encoding="utf-8")
    assert "archive HEAD" in text
    assert "--offline" in text
    assert "--find-links" in text
    assert "set -euo pipefail" in text


def test_packaging_smoke_and_ci_invoke_capability_wheel_isolation() -> None:
    packaging = _PACKAGING_SMOKE.read_text(encoding="utf-8")
    workflow = _CI_WORKFLOW.read_text(encoding="utf-8")
    assert "assurance_capability_wheel_smoke_test.sh" in packaging
    assert "assurance capability wheel smoke test" in workflow
    assert "bash scripts/assurance_capability_wheel_smoke_test.sh" in workflow


def phase4_runtime_dependency_names(distribution: str) -> tuple[str, ...]:
    """Canonical PEP 503 names limited to graph-engine, contracts, and assurance-*."""
    found: set[str] = set()
    for requirement in _installed_requirements(distribution):
        name = canonicalize_name(requirement.name)
        if name in _PHASE4_RUNTIME_INDEX or name.startswith("assurance-"):
            found.add(name)
    return tuple(sorted(found, key=lambda name: _PHASE4_RUNTIME_INDEX.get(name, len(_PHASE4_RUNTIME_INDEX))))


def _installed_requirements(distribution: str) -> tuple[Requirement, ...]:
    from importlib import metadata

    raw = metadata.metadata(distribution).get_all("Requires-Dist") or []
    return tuple(Requirement(item) for item in raw)


def _downstream_assurance_names(distribution: str) -> frozenset[str]:
    name = canonicalize_name(distribution)
    if name not in _ASSURANCE_DAG:
        return frozenset(_ASSURANCE_DAG)
    return frozenset(_ASSURANCE_DAG[_ASSURANCE_DAG.index(name) + 1 :])
