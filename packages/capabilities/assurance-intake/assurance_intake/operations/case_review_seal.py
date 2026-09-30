"""Deterministic case-review artifacts materialized by the trusted finalize handler."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from typing import cast

from graph_engine.canonical import JSONValue, canonical_digest

from assurance_intake.contracts.case_selection import CaseSelectionV1, SelectedCaseV1, selection_path
from assurance_intake.contracts.loop_history import LoopRoundHistoryV1
from assurance_intake.domain.loop_history import build_loop_round_history
from assurance_intake.contracts.plan import ResolvedAssurancePlan
from assurance_intake.contracts.review import CaseReviewResultV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1


def case_review_runtime_paths(*, coverage_epoch: int, review_round: int) -> tuple[str, str, str]:
    return (
        "qa/cases/reviewed-case.json",
        f"qa/cases/reviews/epochs/{coverage_epoch}/rounds/{review_round}.json",
        selection_path(coverage_epoch),
    )


def collect_selected_cases(
    case_refs: Sequence[EvidenceArtifactRefV1],
    case_delta_paths: Sequence[str],
    documents: Sequence[tuple[EvidenceArtifactRefV1, Mapping[str, object]]],
) -> tuple[SelectedCaseV1, ...]:
    delta = set(case_delta_paths)
    selected: list[SelectedCaseV1] = []
    for ref, source in documents:
        reused = ref.path not in delta
        for section in ("added", "modified"):
            entries = source.get(section)
            if not isinstance(entries, list):
                continue
            for index, entry in enumerate(entries):
                if not isinstance(entry, Mapping) or not isinstance(entry.get("case_id"), str):
                    continue
                selected.append(
                    SelectedCaseV1(
                        case_id=str(entry["case_id"]),
                        origin="reuse" if reused else ("modified" if section == "modified" else "added"),
                        source_ref=ref,
                        source_locator=f"{section}[{index}]",
                        mrc_ids=(),
                    )
                )
    if not selected:
        raise ValueError("case selection must include at least one case")
    seen = {item.case_id for item in selected}
    if len(seen) != len(selected):
        raise ValueError("selection case_id must be unique")
    bound = {item.path for item in case_refs}
    if any(item.source_ref.path not in bound for item in selected):
        raise ValueError("selection source is not in reviewed case_refs")
    return tuple(selected)


def expected_case_selection(
    *,
    change_id: str,
    coverage_epoch: int,
    plan: ResolvedAssurancePlan,
    cases: Sequence[SelectedCaseV1],
) -> CaseSelectionV1:
    return CaseSelectionV1(
        schema_version="1",
        change_id=change_id,
        coverage_epoch=coverage_epoch,
        plan_digest=plan.plan_digest,
        inventory_ref=plan.impact_inventory_ref,
        cases=tuple(cases),
    )


def expected_review_history(
    *,
    change_id: str,
    coverage_epoch: int,
    review_round: int,
    document: CaseReviewResultV1,
    preparation_refs: Sequence[EvidenceArtifactRefV1],
    case_refs: Sequence[EvidenceArtifactRefV1],
    review_ref: EvidenceArtifactRefV1,
) -> LoopRoundHistoryV1:
    input_refs = tuple(sorted((*preparation_refs, *case_refs), key=lambda item: item.path))
    return build_loop_round_history(
        change_id=change_id,
        coverage_epoch=coverage_epoch,
        loop_kind="case_review",
        family=None,
        round_index=review_round,
        outcome=document.public_outcome or document.decision,
        review_input_digest=canonical_digest(_as_json_list(input_refs)),
        source_refs=tuple(sorted((*input_refs, review_ref), key=lambda item: item.path)),
    )


def expected_reviewed_case(
    *,
    change_id: str,
    coverage_epoch: int,
    plan_digest: str,
    plan_ref: EvidenceArtifactRefV1,
    preparation_refs: Sequence[EvidenceArtifactRefV1],
    case_refs: Sequence[EvidenceArtifactRefV1],
    review_ref: EvidenceArtifactRefV1,
    selection_ref: EvidenceArtifactRefV1,
) -> ReviewedCaseV1:
    return ReviewedCaseV1(
        change_id=change_id,
        coverage_epoch=coverage_epoch,
        plan_digest=plan_digest,
        plan_ref=plan_ref,
        preparation_refs=tuple(preparation_refs),
        case_refs=tuple(case_refs),
        review_ref=review_ref,
        selection_ref=selection_ref,
    )


def _as_json_list(refs: Sequence[EvidenceArtifactRefV1]) -> JSONValue:
    return cast(JSONValue, [item.model_dump(mode="json") for item in refs])
