from __future__ import annotations

import json
from typing import Any, cast

import pytest
from pydantic import ValidationError

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.plugin_api import TaskHandler
from assurance_healing.agent_ops.fix_proposal import finalize as fix_proposal_finalize
from assurance_healing.contracts.agent import FixProposalInputV1
from assurance_healing.contracts.coverage_repair import CoverageRepairBrief
from assurance_quality.operations.coverage import coverage_gap_to_repair_brief
from tests.capabilities.cross_wheel import (
    CAPABILITY_CATALOG,
    CATALOG_PATH,
    _healing_finalize_payload,
    _run,
    all_capability_leaf_validators,
    consume_handoff,
    encode_handoff,
    forbidden_imports,
    handoff_seams,
    load_capability_catalog,
    quality_gap_fixture,
    validate_catalog_document,
)


@pytest.mark.parametrize(
    "value",
    (
        "auth.fake",
        "entities.user.fake",
        "capabilities.adapters.nonexistent",
    ),
)
def test_every_contract_rejects_prefix_valid_unknown_leaf(value: str) -> None:
    for validator in all_capability_leaf_validators():
        with pytest.raises((ValidationError, ValueError), match="unknown|typed leaf"):
            validator(value, catalog=CAPABILITY_CATALOG)


def test_quality_gap_converts_to_healing_contract_without_reverse_import() -> None:
    brief = coverage_gap_to_repair_brief(quality_gap_fixture())
    assert isinstance(brief, CoverageRepairBrief)
    assert forbidden_imports("assurance_healing", prefix="assurance_quality") == set()


def test_capability_catalog_is_canonical_and_exact() -> None:
    document = load_capability_catalog()
    assert CATALOG_PATH.read_bytes() == canonical_json_bytes(cast(JSONValue, document))
    assert validate_catalog_document(document) == CAPABILITY_CATALOG
    assert CAPABILITY_CATALOG == frozenset(
        {"auth.session.create", "capabilities.adapters.create", "entities.item.create"}
    )
    digest_input = document["digest_input"]
    assert document["digest"] == canonical_digest(cast(JSONValue, digest_input))


@pytest.mark.parametrize(
    ("mutator", "match"),
    (
        (lambda doc: _replace_leafs(doc, ["auth.session.create", "auth.session.create"]), "duplicate"),
        (
            lambda doc: _replace_leafs(doc, sorted([*CAPABILITY_CATALOG, "auth.session"])),
            "prefix|non-leaf|unknown",
        ),
        (lambda doc: _replace_leafs(doc, sorted(["auth", *CAPABILITY_CATALOG])), "prefix|non-leaf|unknown"),
        (lambda doc: _replace_leafs(doc, sorted(["accounts.user.create", *CAPABILITY_CATALOG])), "unknown"),
        (lambda doc: _replace_leafs(doc, ["auth.session.create"]), "exact typed leaf set"),
    ),
)
def test_capability_catalog_rejects_invalid_leaf_sets(
    mutator: Any,
    match: str,
) -> None:
    document = mutator(load_capability_catalog())
    with pytest.raises(ValueError, match=match):
        validate_catalog_document(document)


@pytest.mark.parametrize("seam", handoff_seams(), ids=lambda item: str(item["name"]))
def test_canonical_handoff_round_trips(seam: dict[str, Any]) -> None:
    payload = cast(dict[str, Any], seam["payload"])
    raw = encode_handoff(str(seam["schema_id"]), str(seam["family"]), payload)
    consumed = consume_handoff(
        raw,
        schema_id=str(seam["schema_id"]),
        family=str(seam["family"]),
        catalog=CAPABILITY_CATALOG,
        allowed_fields=cast(frozenset[str], seam["allowed"]),
        consumer=seam["consumer"],
    )
    assert consumed is not None
    assert json.loads(raw)["digest"] == canonical_digest(cast(JSONValue, payload))


@pytest.mark.parametrize("seam", handoff_seams(), ids=lambda item: str(item["name"]))
@pytest.mark.parametrize(
    "fault",
    ("schema_id", "digest", "missing", "extra", "family", "capability"),
)
def test_canonical_handoff_rejects_broken_bytes(seam: dict[str, Any], fault: str) -> None:
    payload = json.loads(json.dumps(seam["payload"]))
    raw = encode_handoff(str(seam["schema_id"]), str(seam["family"]), payload)
    envelope = json.loads(raw)
    if fault == "schema_id":
        envelope["schema_id"] = "assurance.unknown.schema.v1"
    elif fault == "digest":
        envelope["digest"] = "0" * 64
    elif fault == "family":
        envelope["family"] = "e2e" if envelope["family"] == "api" else "api"
    elif fault == "missing":
        key = next(iter(payload))
        del payload[key]
        envelope["payload"] = payload
        envelope["digest"] = canonical_digest(cast(JSONValue, payload))
    elif fault == "extra":
        payload["unexpected_field"] = True
        envelope["payload"] = payload
        envelope["digest"] = canonical_digest(cast(JSONValue, payload))
    else:
        _inject_unknown_capability(payload)
        envelope["payload"] = payload
        envelope["digest"] = canonical_digest(cast(JSONValue, payload))
    broken = canonical_json_bytes(cast(JSONValue, envelope))
    with pytest.raises(ValueError, match="changed|missing|extra|wrong family|unknown"):
        consume_handoff(
            broken,
            schema_id=str(seam["schema_id"]),
            family=str(seam["family"]),
            catalog=CAPABILITY_CATALOG,
            allowed_fields=cast(frozenset[str], seam["allowed"]),
            consumer=seam["consumer"],
        )


def test_execution_evidence_handoff_binds_healing_proposal_digest() -> None:
    seam = next(item for item in handoff_seams() if item["name"] == "execution_evidence")
    payload = cast(dict[str, Any], seam["payload"])
    digest = canonical_digest(cast(JSONValue, payload))
    consumed = consume_handoff(
        encode_handoff(str(seam["schema_id"]), str(seam["family"]), payload),
        schema_id=str(seam["schema_id"]),
        family=str(seam["family"]),
        catalog=CAPABILITY_CATALOG,
        allowed_fields=cast(frozenset[str], seam["allowed"]),
        consumer=seam["consumer"],
    )
    assert isinstance(consumed, FixProposalInputV1)
    assert consumed.execution_evidence_digest == digest

    finalize = _healing_finalize_payload("entities.item.create", CAPABILITY_CATALOG, evidence_digest=digest)
    finalize["execution_evidence_digest"] = "0" * 64
    outcome = _run(cast(TaskHandler, fix_proposal_finalize), finalize)
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


def _replace_leafs(document: dict[str, Any], leafs: list[str]) -> dict[str, Any]:
    digest_input = {"leafs": leafs}
    return {
        "schema_version": "1",
        "digest_input": digest_input,
        "digest": canonical_digest(cast(JSONValue, digest_input)),
    }


def _inject_unknown_capability(payload: dict[str, Any]) -> None:
    if "required_capabilities" in payload:
        payload["required_capabilities"] = ["auth.fake"]
        return
    added = payload.get("added")
    if isinstance(added, list) and added and isinstance(added[0], dict):
        added[0]["trace"] = {"auth.fake": {"covered": True}}
        return
    mapping = payload.get("mapping")
    if isinstance(mapping, dict):
        rows = mapping.get("mappings")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            rows[0]["capability"] = "auth.fake"
            return
    plan = payload.get("plan")
    if isinstance(plan, dict) and "required_capabilities" in plan:
        plan["required_capabilities"] = ["auth.fake"]
        return
    payload["capability"] = "auth.fake"
