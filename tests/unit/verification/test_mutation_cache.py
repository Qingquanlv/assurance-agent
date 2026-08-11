"""Mutation cache hit/miss (§5-B1): module + test-tree + sampler version."""

from __future__ import annotations

from pathlib import Path

from assurance_agent.artifacts.registry import match_artifact
from assurance_agent.verification.mutation_cache import (
    MUTATION_CACHE_DIR_REL,
    MutationCacheKey,
    MutationCacheRecord,
    cache_digest,
    cache_path,
    digest_module_contents,
    digest_test_tree,
    read_cache,
    write_cache,
)
from assurance_agent.verification.mutation_sampling import (
    SAMPLER_VERSION,
    MutantCandidate,
    sample_mutants,
)


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _module_map(root: Path, *rels: str) -> dict[str, bytes]:
    return {rel: (root / rel).read_bytes() for rel in rels}


def _key_for(
    root: Path,
    module_rels: tuple[str, ...],
    *,
    sampler_version: str = SAMPLER_VERSION,
) -> MutationCacheKey:
    return MutationCacheKey(
        module_digest=digest_module_contents(_module_map(root, *module_rels)),
        test_tree_digest=digest_test_tree(root),
        sampler_version=sampler_version,
    )


def _record(key: MutationCacheKey, seed: int = 42) -> MutationCacheRecord:
    selected = sample_mutants(
        (
            MutantCandidate(module="app/svc.py", line=1, operator="AOR", mutant_id="a"),
            MutantCandidate(module="app/svc.py", line=1, operator="ROR", mutant_id="b"),
            MutantCandidate(module="app/svc.py", line=2, operator="COI", mutant_id="c"),
        ),
        seed=seed,
    )
    return MutationCacheRecord(key=key, seed=seed, selected=selected)


def test_cache_path_lives_under_project_aa_cache_mutation(tmp_path: Path) -> None:
    _write(tmp_path, "app/svc.py", "x = 1\n")
    _write(tmp_path, "tests/test_a.py", "def test_a():\n    assert True\n")
    key = _key_for(tmp_path, ("app/svc.py",))
    path = cache_path(tmp_path, key)
    assert path == tmp_path / MUTATION_CACHE_DIR_REL / f"{cache_digest(key)}.json"
    assert MUTATION_CACHE_DIR_REL == ".aa/cache/mutation"
    assert match_artifact(path.relative_to(tmp_path).as_posix()) is None


def test_cache_hit_when_module_and_test_tree_and_version_unchanged(tmp_path: Path) -> None:
    _write(tmp_path, "app/svc.py", "def f(x):\n    return x + 1\n")
    _write(tmp_path, "tests/test_a.py", "def test_a():\n    assert True\n")
    key = _key_for(tmp_path, ("app/svc.py",))
    written = write_cache(tmp_path, _record(key))
    assert written.is_file()

    hit = read_cache(tmp_path, key)
    assert hit is not None
    assert hit.key == key
    assert hit.selected == _record(key).selected
    assert hit.seed == 42


def test_cache_miss_when_module_content_changes(tmp_path: Path) -> None:
    module = _write(tmp_path, "app/svc.py", "def f(x):\n    return x + 1\n")
    _write(tmp_path, "tests/test_a.py", "def test_a():\n    assert True\n")
    key_before = _key_for(tmp_path, ("app/svc.py",))
    write_cache(tmp_path, _record(key_before))

    module.write_text("def f(x):\n    return x + 2\n", encoding="utf-8")
    key_after = _key_for(tmp_path, ("app/svc.py",))
    assert key_after.module_digest != key_before.module_digest
    assert cache_digest(key_after) != cache_digest(key_before)
    assert read_cache(tmp_path, key_after) is None
    # Old entry still present under the previous digest path.
    assert read_cache(tmp_path, key_before) is not None


def test_cache_miss_when_test_tree_changes(tmp_path: Path) -> None:
    _write(tmp_path, "app/svc.py", "def f(x):\n    return x\n")
    test = _write(tmp_path, "tests/test_a.py", "def test_a():\n    assert True\n")
    key_before = _key_for(tmp_path, ("app/svc.py",))
    write_cache(tmp_path, _record(key_before))

    test.write_text("def test_a():\n    assert 1 == 1\n", encoding="utf-8")
    key_after = _key_for(tmp_path, ("app/svc.py",))
    assert key_after.test_tree_digest != key_before.test_tree_digest
    assert read_cache(tmp_path, key_after) is None


def test_cache_miss_when_sampler_version_bumps(tmp_path: Path) -> None:
    _write(tmp_path, "app/svc.py", "def f(x):\n    return x\n")
    _write(tmp_path, "tests/test_a.py", "def test_a():\n    pass\n")
    key_v1 = _key_for(tmp_path, ("app/svc.py",), sampler_version="1")
    write_cache(tmp_path, _record(key_v1))

    key_v2 = _key_for(tmp_path, ("app/svc.py",), sampler_version="2")
    assert cache_digest(key_v2) != cache_digest(key_v1)
    assert read_cache(tmp_path, key_v2) is None


def test_module_digest_aggregates_sorted_paths(tmp_path: Path) -> None:
    _write(tmp_path, "app/a.py", "a = 1\n")
    _write(tmp_path, "app/b.py", "b = 2\n")
    forward = digest_module_contents(_module_map(tmp_path, "app/a.py", "app/b.py"))
    reverse = digest_module_contents(_module_map(tmp_path, "app/b.py", "app/a.py"))
    assert forward == reverse
    only_a = digest_module_contents(_module_map(tmp_path, "app/a.py"))
    assert only_a != forward


def test_test_tree_digest_ignores_pycache(tmp_path: Path) -> None:
    _write(tmp_path, "tests/test_a.py", "def test_a():\n    pass\n")
    before = digest_test_tree(tmp_path)
    _write(tmp_path, "tests/__pycache__/x.pyc", "not-a-real-pyc")
    assert digest_test_tree(tmp_path) == before
