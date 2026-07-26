"""Strict schema-v2 Improvement Candidate read/digest/whole-batch validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from assurance_agent.artifacts.models.improvements import (
    DeliveryKind,
    ImprovementCandidate,
    ImprovementCandidateDocument,
    ImprovementKind,
)
from assurance_agent.exceptions import AaError
from assurance_agent.retro.types import RetroContext
from assurance_agent.workflow.improvements.identity import improvement_fingerprint

FORBIDDEN_PROBLEM_FIELDS = frozenset(
    {
        "classification",
        "severity",
        "status",
        "version",
        "root_cause",
        "resolved",
        "not_an_issue",
        "accepted_risk",
    }
)

CANDIDATE_DOCUMENT_NAME = "proposal-candidates.json"


@dataclass(frozen=True)
class CandidateValidationError:
    code: str
    ids: tuple[str, ...] = ()
    candidate_id: str | None = None
    message: str = ""


class CandidateBatchInvalid(AaError):
    """Whole Candidate batch rejected; callers must perform zero project writes."""

    def __init__(self, errors: tuple[CandidateValidationError, ...]) -> None:
        self.errors = errors
        codes = ", ".join(sorted({item.code for item in errors})) or "unknown"
        super().__init__(f"candidate batch invalid: {codes}")


def _canonical_json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def context_sha256(context: RetroContext) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(context.model_dump(mode="json"))).hexdigest()


def candidate_batch_digest(document: ImprovementCandidateDocument) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(document.model_dump(mode="json"))).hexdigest()


def _parse_errors(err: ValidationError) -> list[CandidateValidationError]:
    errors: list[CandidateValidationError] = []
    for issue in err.errors():
        loc = tuple(str(part) for part in issue["loc"])
        field = loc[-1] if loc else ""
        if issue.get("type") == "extra_forbidden" and field in FORBIDDEN_PROBLEM_FIELDS:
            errors.append(
                CandidateValidationError(
                    code="forbidden_problem_field",
                    message=field,
                )
            )
            continue
        msg = issue.get("msg", "")
        if "cannot use" in msg:
            errors.append(CandidateValidationError(code="incompatible_delivery", message=msg))
            continue
        errors.append(
            CandidateValidationError(
                code="invalid_candidate",
                message=f"{'.'.join(loc)}: {msg}" if loc else msg,
            )
        )
    return errors or [CandidateValidationError(code="invalid_candidate", message=str(err))]


def read_candidate_document(retro_dir: Path) -> ImprovementCandidateDocument:
    path = retro_dir / CANDIDATE_DOCUMENT_NAME
    if not path.is_file():
        raise AaError(f"{CANDIDATE_DOCUMENT_NAME} missing: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise AaError(f"{path} is not valid JSON: {err}") from err
    try:
        return ImprovementCandidateDocument.model_validate(raw)
    except ValidationError as err:
        raise CandidateBatchInvalid(tuple(_parse_errors(err))) from err


def validate_knowledge_eligibility(
    candidate: ImprovementCandidate,
    context: RetroContext,
) -> list[CandidateValidationError]:
    errors: list[CandidateValidationError] = []
    if candidate.kind is not ImprovementKind.DOMAIN_KNOWLEDGE:
        return errors
    if not context.allows_domain_knowledge:
        errors.append(
            CandidateValidationError(
                code="domain_knowledge_blocked",
                candidate_id=candidate.candidate_id,
                message="incomplete Issue integrity forbids domain_knowledge",
            )
        )
        return errors
    if not candidate.source_refs.problem_ids:
        errors.append(
            CandidateValidationError(
                code="invalid_knowledge_delta",
                candidate_id=candidate.candidate_id,
                message="domain_knowledge requires at least one problem_id",
            )
        )
    delta = candidate.knowledge_delta
    if delta is None:
        errors.append(
            CandidateValidationError(
                code="invalid_knowledge_delta",
                candidate_id=candidate.candidate_id,
                message="knowledge_delta payload required",
            )
        )
        return errors
    if delta.mode != "delta":
        errors.append(
            CandidateValidationError(
                code="invalid_knowledge_delta",
                candidate_id=candidate.candidate_id,
                message="knowledge_delta mode must be delta",
            )
        )
    if candidate.delivery is not DeliveryKind.KNOWLEDGE_DELTA:
        errors.append(
            CandidateValidationError(
                code="incompatible_delivery",
                candidate_id=candidate.candidate_id,
            )
        )
    caps = delta.capabilities
    has_caps = bool(
        caps.domain_factories
        or caps.cleanup
        or caps.adapters.api
        or caps.adapters.e2e
        or caps.adapters.fuzz
        or caps.adapters.performance
    )
    if not (delta.accounts or delta.auth or delta.entities or has_caps):
        errors.append(
            CandidateValidationError(
                code="invalid_knowledge_delta",
                candidate_id=candidate.candidate_id,
                message="knowledge_delta has no L2 leaves",
            )
        )
    return errors


def validate_candidate_document(
    context: RetroContext,
    document: ImprovementCandidateDocument,
) -> None:
    errors: list[CandidateValidationError] = []
    if document.retro_id != context.retro_id:
        errors.append(CandidateValidationError(code="retro_id_mismatch"))
    if document.context_sha256 != context_sha256(context):
        errors.append(CandidateValidationError(code="context_digest_mismatch"))

    seen_ids: set[str] = set()
    seen_fingerprints: dict[str, str] = {}
    resolvable = context.source_manifest.resolvable_ids()

    for candidate in document.candidates:
        if candidate.candidate_id in seen_ids:
            errors.append(
                CandidateValidationError(
                    code="duplicate_candidate_id",
                    candidate_id=candidate.candidate_id,
                    ids=(candidate.candidate_id,),
                )
            )
        seen_ids.add(candidate.candidate_id)

        fingerprint = improvement_fingerprint(candidate)
        prior = seen_fingerprints.get(fingerprint)
        if prior is not None:
            errors.append(
                CandidateValidationError(
                    code="duplicate_fingerprint",
                    candidate_id=candidate.candidate_id,
                    ids=tuple(sorted({prior, candidate.candidate_id})),
                )
            )
        else:
            seen_fingerprints[fingerprint] = candidate.candidate_id

        unknown = set(candidate.source_refs.all_ids()) - resolvable
        if unknown:
            errors.append(
                CandidateValidationError(
                    code="unknown_source_ref",
                    candidate_id=candidate.candidate_id,
                    ids=tuple(sorted(unknown)),
                )
            )
        errors.extend(validate_knowledge_eligibility(candidate, context))

    if errors:
        raise CandidateBatchInvalid(tuple(errors))
