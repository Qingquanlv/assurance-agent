from __future__ import annotations

import hashlib
import json
from pathlib import Path

from assurance_product.generated_merge import merge_generated

CHANGE_ID = "CH-DEMO-001"
FAMILIES = ("api", "e2e", "fuzz", "performance")
FAMILY_TARGETS = {
    "api": "tests/api/test_users.py",
    "e2e": "tests/e2e/test_users.py",
    "fuzz": "tests/fuzz/test_users.py",
    "performance": "tests/perf/test_users.py",
}
SHARED_TARGET = "tests/testdata/domain/users.py"


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _staged_path(family: str, target: str) -> str:
    return f"qa/changes/{CHANGE_ID}/generated/{family}/files/{target}"


def _write(project: Path, relative: str, content: bytes) -> Path:
    path = project.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _promote(project: Path, family: str, target: str, content: bytes) -> None:
    _write(project, _staged_path(family, target), content)
    _write(
        project,
        f"qa/changes/{CHANGE_ID}/codegen/{family}-generated-files.json",
        json.dumps(
            {
                "schema_version": "1",
                "change_id": CHANGE_ID,
                "layer": family,
                "files": [
                    {
                        "target_path": target,
                        "disposition": "generated",
                        "role": "test_entry",
                        "case_ids": [f"TC_{family.upper()}_001"],
                        "content_sha256": _digest(content),
                    }
                ],
            }
        ).encode("utf-8"),
    )


def test_four_families_keep_isolated_physical_namespaces_for_the_same_logical_target(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    (project / "qa" / "changes" / CHANGE_ID).mkdir(parents=True)
    (project / "tests" / "testdata" / "domain").mkdir(parents=True)
    original = project / "tests" / "testdata" / "domain" / "users.py"
    original.write_bytes(b"sut-original\n")

    for family in FAMILIES:
        _promote(project, family, FAMILY_TARGETS[family], f"{family}-private\n".encode())
        _write(project, _staged_path(family, SHARED_TARGET), f"{family}-shared\n".encode())

    for family in FAMILIES:
        private = project / _staged_path(family, FAMILY_TARGETS[family])
        shared = project / _staged_path(family, SHARED_TARGET)
        assert private.read_bytes() == f"{family}-private\n".encode()
        assert shared.read_bytes() == f"{family}-shared\n".encode()

    assert original.read_bytes() == b"sut-original\n"
    assert not (project / "tests" / "api" / "test_users.py").exists()


def test_two_lane_review_counters_are_order_independent() -> None:
    import sys
    from pathlib import Path as _Path

    from assurance_generation.contracts.families import GENERATION_FAMILIES

    feature_tests = _Path(__file__).resolve().parents[2] / "packages/features/assurance-generation/tests"
    if str(feature_tests) not in sys.path:
        sys.path.insert(0, str(feature_tests))
    from test_workflow_module import _drive_generate

    first = _drive_generate(selected=("api", "e2e"), reviews=("needs_fix", "pass"))
    second = _drive_generate(selected=("e2e", "api"), reviews=("needs_fix", "pass"))
    assert first.counters == second.counters == (1, 1)
    assert first.dispatched_families == second.dispatched_families == {"api", "e2e"}
    assert set(GENERATION_FAMILIES) - first.dispatched_families == first.skip_families


def test_parallel_family_completion_order_does_not_change_merged_sources(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "qa" / "changes" / CHANGE_ID).mkdir(parents=True)

    for family in ("performance", "api", "fuzz", "e2e"):
        _promote(project, family, FAMILY_TARGETS[family], f"{family}\n".encode())

    merged = merge_generated(project, CHANGE_ID, ("e2e", "performance", "api", "fuzz"))

    assert list(merged.sources) == sorted(FAMILY_TARGETS.values())
    for family, target in FAMILY_TARGETS.items():
        assert merged.sources[target] == _staged_path(family, target)
        assert (project / merged.sources[target]).read_bytes() == f"{family}\n".encode()
    assert not any(path.as_posix().startswith("tests/") for path in project.rglob("test_users.py"))
