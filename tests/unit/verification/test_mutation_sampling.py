"""Deterministic mutation sampling (§5-B1): ≤1 mutant per touched line."""

from __future__ import annotations

from assurance_agent.verification.mutation_sampling import (
    SAMPLER_VERSION,
    MutantCandidate,
    sample_mutants,
)


def _candidates() -> tuple[MutantCandidate, ...]:
    return (
        MutantCandidate(module="app/svc.py", line=10, operator="AOR", mutant_id="m1"),
        MutantCandidate(module="app/svc.py", line=10, operator="ROR", mutant_id="m2"),
        MutantCandidate(module="app/svc.py", line=11, operator="AOR", mutant_id="m3"),
        MutantCandidate(module="app/svc.py", line=11, operator="COI", mutant_id="m4"),
        MutantCandidate(module="app/svc.py", line=12, operator="SDL", mutant_id="m5"),
        MutantCandidate(module="app/other.py", line=3, operator="AOR", mutant_id="m6"),
        MutantCandidate(module="app/other.py", line=3, operator="ROR", mutant_id="m7"),
    )


def test_sampler_version_is_a_fixed_nonempty_string() -> None:
    assert isinstance(SAMPLER_VERSION, str)
    assert SAMPLER_VERSION.strip() != ""


def test_fixed_seed_is_deterministic() -> None:
    first = sample_mutants(_candidates(), seed=42)
    second = sample_mutants(_candidates(), seed=42)
    assert first == second
    assert first  # non-empty under these candidates


def test_different_seeds_can_select_differently() -> None:
    """Not required to always differ, but with multiple options seed must matter."""
    seen = {sample_mutants(_candidates(), seed=seed) for seed in range(50)}
    assert len(seen) > 1


def test_at_most_one_mutant_per_touched_line() -> None:
    selected = sample_mutants(_candidates(), seed=7)
    lines = [(m.module, m.line) for m in selected]
    assert len(lines) == len(set(lines))
    assert set(lines) == {
        ("app/svc.py", 10),
        ("app/svc.py", 11),
        ("app/svc.py", 12),
        ("app/other.py", 3),
    }
    assert all(m.mutant_id for m in selected)


def test_touched_lines_filter_restricts_selection() -> None:
    selected = sample_mutants(
        _candidates(),
        seed=7,
        touched_lines={
            "app/svc.py": frozenset({10, 12}),
            "app/other.py": frozenset({99}),  # no candidates on this line
        },
    )
    assert {(m.module, m.line) for m in selected} == {
        ("app/svc.py", 10),
        ("app/svc.py", 12),
    }


def test_selection_order_is_stable() -> None:
    selected = sample_mutants(_candidates(), seed=1)
    # Primary sort: (module, line) ascending for replayability.
    keys = [(m.module, m.line) for m in selected]
    assert keys == sorted(keys)
