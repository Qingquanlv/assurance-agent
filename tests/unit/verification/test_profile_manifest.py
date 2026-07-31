"""Stable assurance-profile manifest and digest invariants."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from assurance_agent.artifacts.models.assurance import LAYER_NAMES, PLAN_CHECK_IDS
from assurance_agent.artifacts.models.review import PlanReview, Review
from assurance_agent.verification.checks.registry import CHECKS_BY_ID
from assurance_agent.verification.profiles import iter_layer_assurance_profiles
from assurance_agent.verification.profile_manifest import (
    PROFILE_SNAPSHOT_DIRECTORY,
    AssuranceProfileManifest,
    assurance_profile_bytes,
    assurance_profile_digest,
    assurance_profile_snapshot_relpath,
    normalized_assurance_profile_manifest,
    parse_assurance_profile_snapshot,
)
from assurance_agent.workflow.core.progression import ProgressionError, transaction


def _manifest() -> dict[str, Any]:
    return normalized_assurance_profile_manifest()


def test_manifest_profiles_follow_layer_names_order() -> None:
    manifest = _manifest()
    assert [profile["layer"] for profile in manifest["profiles"]] == list(LAYER_NAMES)


def test_manifest_applicable_check_ids_follow_plan_check_ids_order() -> None:
    manifest = _manifest()
    for profile in manifest["profiles"]:
        assert profile["applicable_check_ids"] == [
            check_id for check_id in PLAN_CHECK_IDS if check_id in set(profile["applicable_check_ids"])
        ]


def test_manifest_check_catalog_follows_plan_check_ids_order() -> None:
    manifest = _manifest()
    assert [entry["check_id"] for entry in manifest["check_catalog"]] == list(PLAN_CHECK_IDS)
    for entry in manifest["check_catalog"]:
        fn = CHECKS_BY_ID[entry["check_id"]]
        assert entry["callable"] == f"{fn.__module__}.{fn.__qualname__}"


def test_manifest_records_review_models_by_qualified_name() -> None:
    manifest = _manifest()
    expected = {
        profile.layer: f"{profile.review_model.__module__}.{profile.review_model.__name__}"
        for profile in iter_layer_assurance_profiles()
    }
    actual = {profile["layer"]: profile["review_model"] for profile in manifest["profiles"]}
    assert actual == expected
    assert actual["api"] == f"{PlanReview.__module__}.{PlanReview.__name__}"
    assert actual["fuzz"] == f"{Review.__module__}.{Review.__name__}"


def test_repeated_construction_is_byte_identical() -> None:
    first = assurance_profile_bytes()
    second = assurance_profile_bytes()
    assert first == second
    assert assurance_profile_digest() == assurance_profile_digest()


@pytest.mark.parametrize(
    ("path", "mutator"),
    [
        ("profiles[0].case_type", lambda m: m["profiles"][0].update({"case_type": "MUTATED"})),
        ("profiles[0].plan_artifacts", lambda m: m["profiles"][0].update({"plan_artifacts": ["mutated"]})),
        (
            "profiles[0].review_artifact",
            lambda m: m["profiles"][0].update({"review_artifact": "review/mutated.json"}),
        ),
        ("profiles[0].review_alias", lambda m: m["profiles"][0].update({"review_alias": "mutated_review"})),
        (
            "profiles[0].checks_artifact",
            lambda m: m["profiles"][0].update({"checks_artifact": "review/mutated-checks.json"}),
        ),
        ("profiles[0].gate_id", lambda m: m["profiles"][0].update({"gate_id": "mutated-gate"})),
        (
            "profiles[0].capability_contract_enabled",
            lambda m: m["profiles"][0].update(
                {"capability_contract_enabled": not m["profiles"][0]["capability_contract_enabled"]}
            ),
        ),
        (
            "profiles[0].applicable_check_ids",
            lambda m: m["profiles"][0].update(
                {"applicable_check_ids": list(reversed(m["profiles"][0]["applicable_check_ids"]))}
            ),
        ),
        (
            "profiles[0].review_model",
            lambda m: m["profiles"][0].update({"review_model": "mutated.ReviewModel"}),
        ),
        (
            "check_catalog order",
            lambda m: m["check_catalog"].sort(key=lambda entry: entry["check_id"], reverse=True),
        ),
    ],
)
def test_profile_manifest_mutation_changes_digest(path: str, mutator: Any) -> None:
    baseline = assurance_profile_digest()
    mutated = copy.deepcopy(_manifest())
    mutator(mutated)
    assert assurance_profile_digest(mutated) != baseline, path


def test_parse_assurance_profile_snapshot_accepts_canonical_bytes() -> None:
    data = assurance_profile_bytes()
    parsed = parse_assurance_profile_snapshot(data)
    assert isinstance(parsed, AssuranceProfileManifest)
    assert parsed.schema_version == "1"
    assert parsed.layer_names == LAYER_NAMES
    assert parsed.plan_check_ids == PLAN_CHECK_IDS
    assert assurance_profile_bytes(parsed.model_dump(mode="json")) == data


def test_parse_assurance_profile_snapshot_rejects_malformed() -> None:
    with pytest.raises(ValueError, match="assurance profile snapshot"):
        parse_assurance_profile_snapshot(b"{not json\n")


def test_parse_assurance_profile_snapshot_rejects_non_canonical() -> None:
    parsed = parse_assurance_profile_snapshot(assurance_profile_bytes())
    pretty = (json.dumps(parsed.model_dump(mode="json"), indent=2) + "\n").encode("utf-8")
    with pytest.raises(ValueError, match="canonical"):
        parse_assurance_profile_snapshot(pretty)


def test_parse_assurance_profile_snapshot_rejects_reordered_layers() -> None:
    mutated = copy.deepcopy(_manifest())
    mutated["layer_names"] = list(reversed(mutated["layer_names"]))
    mutated["profiles"] = list(reversed(mutated["profiles"]))
    with pytest.raises(ValueError, match="layer"):
        parse_assurance_profile_snapshot(assurance_profile_bytes(mutated))


def test_assurance_profile_snapshot_relpath_requires_sha256() -> None:
    digest = assurance_profile_digest()
    assert assurance_profile_snapshot_relpath(digest) == f"{PROFILE_SNAPSHOT_DIRECTORY}/{digest}.json"
    with pytest.raises(ValueError, match="digest"):
        assurance_profile_snapshot_relpath("ABC")


def test_profile_snapshot_create_once_is_idempotent_and_rejects_mismatch(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    data = assurance_profile_bytes()
    digest = assurance_profile_digest()
    rel = assurance_profile_snapshot_relpath(digest)
    with transaction(change) as txn:
        txn.write_runtime_file_once(rel, data)
    assert (change / rel).read_bytes() == data
    with transaction(change) as txn:
        txn.write_runtime_file_once(rel, data)
    with pytest.raises(ProgressionError, match="runtime file content mismatch"):
        with transaction(change) as txn:
            txn.write_runtime_file_once(rel, data + b" ")
