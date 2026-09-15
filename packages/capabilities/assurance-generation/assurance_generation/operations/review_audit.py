"""Verify API review coverage against the wheel-prepared case and source inventory.

No SUT imports or model-authored paths are read here. Evidence comes only from
locked plan images and the bounded planning-facts index. This proves coverage
and factual consistency, not the correctness of a model's semantic judgment.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from assurance_generation.contracts.review_audit import PlanReviewAudit
from assurance_generation.contracts.reviews import PlanReviewAuthoring

_TARGET_SECTIONS = frozenset({"target files", "codegen scope", "output file candidates", "factory mapping"})


def _evidence_paths(facts: Mapping[str, Any], images: Mapping[str, bytes]) -> list[str]:
    return sorted(
        set(images) | {item["path"] for item in (*facts["inputs"], *facts["files"]) if "digest" in item}
    )


def api_review_requirements(
    *, case_ids: tuple[str, ...], facts: Mapping[str, Any], images: Mapping[str, bytes]
) -> dict[str, Any]:
    text = "\n".join(data.decode("utf-8") for path, data in images.items() if "/plans/" in path)
    files = {file["path"]: file for file in facts["files"]}
    helpers = []
    for declared in facts["declared_symbols"]:
        # Include transitive module references as well as explicit L1 keys.
        # This closes role-adapter -> domain-role imports even if the planner
        # forgot to put the domain capability in required_capabilities.
        module = declared["symbol"].rpartition(".")[0]
        if not any(
            re.search(r"(?<![\w.])" + re.escape(token) + r"(?![\w.])", text)
            for token in (declared["capability"], declared["symbol"], declared["path"], module)
        ):
            continue
        source = files.get(declared["path"], {})
        definition = next(
            (item for item in source.get("symbols", ()) if item["name"] == declared["name"]), None
        )
        helpers.append(
            {
                "capability": declared["capability"],
                "symbol": declared["symbol"],
                "declared_kind": declared["kind"],
                "target_file": declared["path"],
                "observed_signature": definition["signature"] if definition else None,
                "observed_async": definition["is_async"] if definition else None,
                "is_stub": definition["is_stub"] if definition else None,
            }
        )
    return {
        "case_ids": sorted(case_ids),
        "input_refs": [
            {"path": path, "digest": hashlib.sha256(data).hexdigest()}
            for path, data in sorted(images.items())
        ],
        "planning_facts_digest": facts["digest"],
        "allowed_evidence_paths": _evidence_paths(facts, images),
        "helpers": helpers,
    }


def _section(text: str, heading: str) -> str:
    lines = text.splitlines()
    for start, line in enumerate(lines):
        if not line.startswith("#") or line.lstrip("#").strip() != heading:
            continue
        level = len(line) - len(line.lstrip("#"))
        end = next(
            (
                index
                for index in range(start + 1, len(lines))
                if lines[index].startswith("#") and len(lines[index]) - len(lines[index].lstrip("#")) <= level
            ),
            len(lines),
        )
        return "\n".join(lines[start:end])
    return ""


def repair_api_review_audit(
    document: PlanReviewAuthoring,
    *,
    requirements: Mapping[str, Any],
    facts: Mapping[str, Any],
    images: Mapping[str, bytes],
) -> tuple[PlanReviewAuthoring, tuple[str, ...]]:
    """Drop extraneous citations, but never invent checks, facts or input identities."""
    if document.review_audit is None:
        raise ValueError("review_audit is required for API plan review in every round")
    known = set(_evidence_paths(facts, images))
    raw = document.review_audit.model_dump(mode="json")
    warnings: set[str] = set()
    for row in (*raw["cases"], *raw["helpers"]):
        paths = row["evidence_paths"]
        if set(paths) - known:
            warnings.add("evidence_paths")
            row["evidence_paths"] = [path for path in paths if path in known]
    audit = PlanReviewAudit.model_validate(raw)
    repaired = document.model_copy(update={"review_audit": audit})
    validate_api_review_audit(repaired, requirements=requirements, facts=facts, images=images)
    return repaired, tuple(sorted(warnings))


def validate_api_review_audit(
    document: PlanReviewAuthoring,
    *,
    requirements: Mapping[str, Any],
    facts: Mapping[str, Any],
    images: Mapping[str, bytes],
) -> None:
    audit = document.review_audit
    if audit is None:
        raise ValueError("review_audit is required for API plan review in every round")
    refs = [ref.model_dump(mode="json") for ref in audit.input_refs]
    if sorted(refs, key=lambda ref: ref["path"]) != requirements["input_refs"]:
        raise ValueError("review_audit input_refs must match every locked input and digest exactly once")
    if audit.planning_facts_digest != facts["digest"]:
        raise ValueError("review_audit planning_facts_digest is stale or incorrect")
    if sorted(row.case_id for row in audit.cases) != requirements["case_ids"]:
        raise ValueError("review_audit must cover every selected case exactly once")
    expected_helpers = {item["capability"]: item for item in requirements["helpers"]}
    if sorted(row.capability for row in audit.helpers) != sorted(expected_helpers):
        raise ValueError("review_audit must cover every prepared helper exactly once")
    findings = {item["id"] for item in document.findings}
    known_paths = set(_evidence_paths(facts, images))
    for row in (*audit.cases, *audit.helpers):
        if not set(row.evidence_paths) <= known_paths:
            raise ValueError("review_audit evidence_paths must reference locked inputs or observed source")
        if not set(row.finding_ids) <= findings or len(set(row.finding_ids)) != len(row.finding_ids):
            raise ValueError("review_audit references unknown or duplicate finding IDs")
    for row in audit.cases:
        results = row.checks.model_dump().values()
        if ("finding" in results) != bool(row.finding_ids):
            raise ValueError("review_audit case finding checks must link their findings")
        if row.checks.request == "not_applicable" or row.checks.assertion == "not_applicable":
            raise ValueError("API request and assertion checks cannot be not_applicable")
        if not any(path in images and "/plans/" in path for path in row.evidence_paths):
            raise ValueError("review_audit case checks need plan evidence")
    for row in audit.helpers:
        expected = expected_helpers[row.capability]
        for name in ("symbol", "declared_kind", "target_file", "observed_signature", "observed_async"):
            if getattr(row, name) != expected[name]:
                raise ValueError(f"review_audit helper {row.capability}: {name} contradicts source facts")
        if ".aa/data-knowledge.yaml" not in row.evidence_paths:
            raise ValueError("review_audit helper needs its L1 declaration as evidence")
        if row.implementation == "existing":
            if expected["observed_signature"] is None or expected["is_stub"]:
                raise ValueError("review_audit cannot claim an unknown helper or stub is executable")
            if row.target_file not in row.evidence_paths:
                raise ValueError("review_audit existing helper needs its source file as evidence")
            if row.invocation != ("async" if expected["observed_async"] else "sync"):
                raise ValueError("review_audit invocation contradicts the existing helper definition")
        if row.implementation == "planned":
            location = row.plan_location
            if location is None or location.artifact not in images or "/plans/" not in location.artifact:
                raise ValueError("review_audit planned helper needs a bounded plan location")
            heading = re.sub(r"^\d+[.)]?\s*", "", location.section).casefold()
            if heading not in _TARGET_SECTIONS:
                raise ValueError(
                    "review_audit planned helper needs a generation target section, not an import"
                )
            if row.target_file not in _section(images[location.artifact].decode("utf-8"), location.section):
                raise ValueError("review_audit planned helper is missing from its generation target section")
            if location.artifact not in row.evidence_paths:
                raise ValueError("review_audit planned helper needs its plan as evidence")
            if row.invocation == "unknown":
                raise ValueError("review_audit planned helper must specify its invocation")
            if row.declared_kind == "async_factory" and row.invocation != "async":
                raise ValueError("review_audit planned async_factory must use async invocation")
        if row.implementation == "unresolved" and not row.finding_ids:
            raise ValueError("review_audit unresolved helper must link a finding")
    if document.decision == "pass" and (
        any(row.finding_ids for row in (*audit.cases, *audit.helpers))
        or any(row.implementation == "unresolved" for row in audit.helpers)
    ):
        raise ValueError("passing review cannot contain unresolved review_audit findings")
