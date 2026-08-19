"""Deterministic mutation sampling layer (§5-B1).

mutmut discovers candidates; this module enforces Google-mode sampling:
under a fixed seed, **at most one mutant per touched line**. Pure and
deterministic so Task 3 ``run-mutation-sample`` can call it without owning
the selection policy.

``SAMPLER_VERSION`` is part of the mutation cache key — bumping it forces a
miss for every previously cached sample.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

# Bump when selection algorithm or MutantCandidate identity semantics change.
SAMPLER_VERSION = "1"


@dataclass(frozen=True)
class MutantCandidate:
    """One discoverable mutant before per-line sampling."""

    module: str
    line: int
    operator: str
    mutant_id: str


@dataclass(frozen=True)
class SelectedMutant:
    """A mutant retained after ≤1-per-touched-line sampling."""

    module: str
    line: int
    operator: str
    mutant_id: str


class MutationCandidateSource(Protocol):
    """Thin seam for Task 3's mutmut subprocess / discovery adapter."""

    def discover(self, modules: Sequence[str]) -> Sequence[MutantCandidate]:
        """Return candidate mutants for ``modules`` (posix project-relative paths)."""
        ...


def sample_mutants(
    candidates: Sequence[MutantCandidate],
    *,
    seed: int,
    touched_lines: Mapping[str, frozenset[int] | set[int]] | None = None,
) -> tuple[SelectedMutant, ...]:
    """Select at most one mutant per ``(module, line)`` under ``seed``.

    When ``touched_lines`` is provided, only candidates whose
    ``(module, line)`` appears in that map are eligible. Lines listed with no
    candidates contribute nothing. Output is sorted by ``(module, line)`` so
    replay does not depend on discovery order.
    """
    by_line: dict[tuple[str, int], list[MutantCandidate]] = defaultdict(list)
    for candidate in candidates:
        if touched_lines is not None:
            lines = touched_lines.get(candidate.module)
            if lines is None or candidate.line not in lines:
                continue
        by_line[(candidate.module, candidate.line)].append(candidate)

    rng = random.Random(seed)
    selected: list[SelectedMutant] = []
    for module, line in sorted(by_line):
        group = by_line[(module, line)]
        # Stable within-line order before RNG so equal seeds pick the same index
        # even when discovery emits candidates in different orders.
        group_sorted = sorted(group, key=lambda c: (c.operator, c.mutant_id))
        choice = group_sorted[rng.randrange(len(group_sorted))]
        selected.append(
            SelectedMutant(
                module=choice.module,
                line=choice.line,
                operator=choice.operator,
                mutant_id=choice.mutant_id,
            )
        )
    return tuple(selected)


__all__ = [
    "SAMPLER_VERSION",
    "MutantCandidate",
    "MutationCandidateSource",
    "SelectedMutant",
    "sample_mutants",
]
