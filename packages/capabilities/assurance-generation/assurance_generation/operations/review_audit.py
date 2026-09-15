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

_PASS_CHECKS = {
    "request": "pass",
    "auth": "pass",
    "setup": "pass",
    "assertion": "pass",
    "cleanup": "pass",
    "helpers": "pass",
}
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


def _default_plan(images: Mapping[str, bytes]) -> str | None:
    plans = [path for path in images if "/plans/" in path]
    return next((path for path in plans if path.endswith("api-plan.md")), plans[0] if plans else None)


def _kept_paths(paths: object, known: set[str], warnings: list[str], *, label: str) -> list[str]:
    original = [item for item in paths] if isinstance(paths, (list, tuple)) else []
    kept = [item for item in original if item in known]
    if original and set(original) - known:
        warnings.append(label)
    return kept


def _unique_findings(ids: object, findings: set[str], warnings: list[str]) -> list[str]:
    original = [item for item in ids] if isinstance(ids, (list, tuple)) else []
    kept: list[str] = []
    for item in original:
        if item in findings and item not in kept:
            kept.append(item)
        elif item not in findings:
            warnings.append("unknown_finding")
    return kept


def _repair_case(
    row: Mapping[str, Any],
    *,
    known: set[str],
    findings: set[str],
    images: Mapping[str, bytes],
    default_plan: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    checks = dict(_PASS_CHECKS)
    raw_checks = row.get("checks")
    if isinstance(raw_checks, Mapping):
        for area, value in raw_checks.items():
            if area in checks and value in {"pass", "finding", "not_applicable"}:
                checks[area] = value
    if checks["request"] == "not_applicable":
        checks["request"] = "pass"
        warnings.append("request_not_applicable")
    if checks["assertion"] == "not_applicable":
        checks["assertion"] = "pass"
        warnings.append("assertion_not_applicable")
    finding_ids = _unique_findings(row.get("finding_ids"), findings, warnings)
    if "finding" in checks.values() and not finding_ids:
        checks = {area: ("pass" if value == "finding" else value) for area, value in checks.items()}
        warnings.append("unlinked_finding")
    elif finding_ids and "finding" not in checks.values():
        checks["helpers"] = "finding"
    paths = _kept_paths(row.get("evidence_paths"), known, warnings, label="evidence_paths")
    if default_plan and not any(path in images and "/plans/" in path for path in paths):
        paths.append(default_plan)
    if not paths and known:
        paths.append(sorted(known)[0])
    rationale = row.get("rationale")
    return {
        "case_id": row["case_id"],
        "checks": checks,
        "evidence_paths": paths,
        "finding_ids": finding_ids,
        "rationale": rationale
        if isinstance(rationale, str) and rationale.strip()
        else "Host-completed case coverage.",
    }


def _infer_invocation(expected: Mapping[str, Any]) -> str:
    if expected.get("observed_async") is True or expected.get("declared_kind") == "async_factory":
        return "async"
    return "sync"


def _repair_plan_location(
    location: object,
    *,
    images: Mapping[str, bytes],
    target_file: str,
    default_plan: str | None,
    warnings: list[str],
) -> dict[str, str] | None:
    artifact = default_plan
    if isinstance(location, Mapping):
        candidate = location.get("artifact")
        if isinstance(candidate, str) and candidate in images and "/plans/" in candidate:
            artifact = candidate
        heading = re.sub(r"^\d+[.)]?\s*", "", str(location.get("section") or "")).casefold()
        section_name = location.get("section")
        if (
            artifact
            and heading in _TARGET_SECTIONS
            and isinstance(section_name, str)
            and target_file in _section(images[artifact].decode("utf-8"), section_name)
        ):
            return {"artifact": artifact, "section": section_name}
        if heading not in _TARGET_SECTIONS:
            warnings.append("plan_location")
    if artifact is None:
        return None
    text = images[artifact].decode("utf-8")
    for heading in ("Target Files", "Codegen Scope", "Output File Candidates", "Factory Mapping"):
        if target_file in _section(text, heading):
            return {"artifact": artifact, "section": heading}
    return {"artifact": artifact, "section": "Target Files"}


def _repair_helper(
    row: Mapping[str, Any],
    *,
    expected: Mapping[str, Any],
    known: set[str],
    findings: set[str],
    images: Mapping[str, bytes],
    default_plan: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    repaired = {
        "capability": expected["capability"],
        "symbol": expected["symbol"],
        "declared_kind": expected["declared_kind"],
        "observed_signature": expected["observed_signature"],
        "observed_async": expected["observed_async"],
        "target_file": expected["target_file"],
        "implementation": row.get("implementation") or "planned",
        "invocation": row.get("invocation") or "unknown",
        "plan_location": row.get("plan_location"),
        "rationale": row.get("rationale")
        if isinstance(row.get("rationale"), str) and str(row.get("rationale")).strip()
        else "Host-completed helper coverage.",
    }
    for name in ("symbol", "declared_kind", "target_file", "observed_signature", "observed_async"):
        if row.get(name) != expected[name]:
            warnings.append(f"helper_{name}")
    paths = _kept_paths(row.get("evidence_paths"), known, warnings, label="evidence_paths")
    if ".aa/data-knowledge.yaml" in known and ".aa/data-knowledge.yaml" not in paths:
        paths.append(".aa/data-knowledge.yaml")
    finding_ids = _unique_findings(row.get("finding_ids"), findings, warnings)
    implementation = repaired["implementation"]
    if implementation not in {"existing", "planned", "unresolved"}:
        implementation = "planned"
    if implementation == "existing" and (expected["observed_signature"] is None or expected["is_stub"]):
        implementation = "planned"
        warnings.append("stub_existing")
    if implementation == "unresolved" and not finding_ids:
        implementation = "planned"
        warnings.append("unresolved_without_finding")
    invocation = repaired["invocation"]
    if invocation not in {"sync", "async", "unknown"}:
        invocation = "unknown"
    location = None
    if implementation == "existing":
        invocation = "async" if expected["observed_async"] else "sync"
        if expected["target_file"] in known and expected["target_file"] not in paths:
            paths.append(expected["target_file"])
    elif implementation == "planned":
        if invocation == "unknown":
            invocation = _infer_invocation(expected)
            warnings.append("unknown_invocation")
        if expected["declared_kind"] == "async_factory":
            invocation = "async"
        location = _repair_plan_location(
            repaired["plan_location"],
            images=images,
            target_file=expected["target_file"],
            default_plan=default_plan,
            warnings=warnings,
        )
        if location and location["artifact"] not in paths:
            paths.append(location["artifact"])
    if not paths and known:
        paths.append(sorted(known)[0])
    repaired.update(
        implementation=implementation,
        invocation=invocation,
        plan_location=location,
        evidence_paths=paths,
        finding_ids=finding_ids,
    )
    return repaired


def repair_api_review_audit(
    document: PlanReviewAuthoring,
    *,
    requirements: Mapping[str, Any],
    facts: Mapping[str, Any],
    images: Mapping[str, bytes],
) -> tuple[PlanReviewAuthoring, tuple[str, ...]]:
    if document.review_audit is None:
        raise ValueError("review_audit is required for API plan review in every round")
    warnings: list[str] = []
    known = set(_evidence_paths(facts, images))
    findings = {
        item["id"] for item in document.findings if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    default_plan = _default_plan(images)
    raw = document.review_audit.model_dump(mode="json")
    if [ref for ref in raw.get("input_refs") or []] != requirements["input_refs"]:
        warnings.append("input_refs")
    if raw.get("planning_facts_digest") != facts["digest"]:
        warnings.append("planning_facts_digest")
    by_case: dict[str, Mapping[str, Any]] = {}
    for row in raw.get("cases") or []:
        case_id = row.get("case_id")
        if isinstance(case_id, str) and case_id not in by_case:
            by_case[case_id] = row
    cases = []
    for case_id in requirements["case_ids"]:
        if case_id not in by_case:
            warnings.append(f"missing_case:{case_id}")
        cases.append(
            _repair_case(
                by_case.get(case_id) or {"case_id": case_id},
                known=known,
                findings=findings,
                images=images,
                default_plan=default_plan,
                warnings=warnings,
            )
        )
    expected_helpers = {item["capability"]: item for item in requirements["helpers"]}
    by_helper: dict[str, Mapping[str, Any]] = {}
    for row in raw.get("helpers") or []:
        capability = row.get("capability")
        if isinstance(capability, str) and capability not in by_helper:
            by_helper[capability] = row
    helpers = []
    for capability, expected in expected_helpers.items():
        if capability not in by_helper:
            warnings.append(f"missing_helper:{capability}")
        helpers.append(
            _repair_helper(
                by_helper.get(capability) or {"capability": capability},
                expected=expected,
                known=known,
                findings=findings,
                images=images,
                default_plan=default_plan,
                warnings=warnings,
            )
        )
    if document.decision == "pass":
        for row in (*cases, *helpers):
            if row.get("finding_ids"):
                warnings.append("pass_finding_ids")
                row["finding_ids"] = []
            if row.get("implementation") == "unresolved":
                warnings.append("pass_unresolved")
                row["implementation"] = "planned"
        for row in cases:
            checks = row["checks"]
            if "finding" in checks.values():
                row["checks"] = {area: "pass" for area in checks}
    audit = PlanReviewAudit.model_validate(
        {
            "input_refs": list(requirements["input_refs"]),
            "planning_facts_digest": facts["digest"],
            "cases": cases,
            "helpers": helpers,
        }
    )
    return document.model_copy(update={"review_audit": audit}), tuple(warnings)


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
            if heading not in {"target files", "codegen scope", "output file candidates", "factory mapping"}:
                raise ValueError(
                    "review_audit planned helper needs a generation target section, not an import"
                )
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
