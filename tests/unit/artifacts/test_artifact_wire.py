"""ArtifactSpec.wire/owner is the parse authority; callers must not sniff suffixes."""

from __future__ import annotations

import json

import pytest
import yaml

from assurance_agent.artifacts.registry import (
    REGISTRY,
    UnregisteredArtifactError,
    load_registered_artifact,
    match_artifact,
    parse_wire,
)


def test_json_specs_declare_json_wire_and_machine_owner() -> None:
    spec = match_artifact("review/api-plan-review.json")
    assert spec is not None
    assert spec.wire == "json"
    assert spec.owner == "machine"


def test_human_maintained_yaml_declares_yaml_wire_and_human_owner() -> None:
    for rel in ("cases/menus/case.yaml", ".qa.yaml"):
        spec = match_artifact(rel)
        assert spec is not None, rel
        assert spec.wire == "yaml"
        assert spec.owner == "human"


def test_every_spec_pattern_suffix_matches_declared_wire() -> None:
    for spec in REGISTRY:
        if spec.wire == "json":
            assert spec.pattern.endswith(".json"), spec.pattern
        elif spec.wire == "yaml":
            assert spec.pattern.endswith((".yaml", ".yml")), spec.pattern
        else:
            assert spec.pattern.endswith(".md"), spec.pattern


def test_markdown_wire_is_free_compat() -> None:
    for spec in REGISTRY:
        if spec.wire == "markdown":
            assert spec.compat == "free", spec.pattern


def test_machine_artifacts_are_json_wire() -> None:
    """Phase 6: machine-owned artifacts are JSON; historical YAML is an alias, not a spec."""
    for spec in REGISTRY:
        if spec.owner == "machine":
            assert spec.wire == "json", spec.pattern
    actual = frozenset(spec.pattern for spec in REGISTRY if spec.owner == "machine" and spec.wire == "yaml")
    assert actual == frozenset()


def test_historical_yaml_aliases_still_resolve() -> None:
    spec = match_artifact("plans/api-codegen-mapping.yaml")
    assert spec is not None
    assert spec.pattern == "plans/*-codegen-mapping.json"
    loaded = load_registered_artifact(
        "plans/api-codegen-mapping.yaml", "schema_version: '1.0'\nentries: []\n"
    )
    assert loaded == {"schema_version": "1.0", "entries": []}


def test_load_registered_artifact_parses_json_by_spec_not_suffix() -> None:
    payload = {"schema_version": "1.0", "decision": "pass", "findings": []}
    loaded = load_registered_artifact("review/case-review.json", json.dumps(payload))
    assert loaded == payload


def test_load_registered_artifact_parses_yaml_by_spec() -> None:
    text = "schema_version: '1.0'\nadded: []\nmodified: []\nremoved: []\n"
    loaded = load_registered_artifact("cases/menus/case.yaml", text)
    assert loaded == yaml.safe_load(text)


def test_load_registered_artifact_rejects_unregistered_paths() -> None:
    with pytest.raises(UnregisteredArtifactError, match="proposal.md"):
        load_registered_artifact("proposal.md", "# hello\n")


def test_parse_wire_json_rejects_yaml_document() -> None:
    with pytest.raises(json.JSONDecodeError):
        parse_wire("json", "schema_version: '1'\n")
