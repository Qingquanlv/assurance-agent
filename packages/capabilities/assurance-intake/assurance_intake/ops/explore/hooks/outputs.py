"""Explore output checks and sealing of the official exploration document."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import cast

import yaml
from pydantic import ValidationError

from agent_runtime_contracts.ops import (
    OutputError,
)
from graph_engine.canonical import canonical_json_bytes

from assurance_intake.contracts.common import TestFamily
from assurance_intake.contracts.explore import (
    EXPLORATION_PATH,
    REQUIREMENT_PATH,
    RUN_SPEC_SNAPSHOT_PATH,
    ExploreAdvisoryV1,
    PreparedExploreV1,
)
from assurance_intake.contracts.impact import ChangeImpactInventoryV1
from assurance_intake.contracts.obligations import ExpectedBasisV1, SourceRefV1
from assurance_intake.contracts.workflow import (
    EvidenceArtifactRefV1,
)
from assurance_intake.domain.artifacts import file_digest, workspace_file
from assurance_intake.domain.impact_validation import validate_inventory_references
from assurance_intake.domain.obligations import (
    TrustedIntakeSourcesV1,
    apply_scope_exclusions,
    authenticate_source,
    normalize_obligation_drafts,
    resolve_requirement_quote,
)
from assurance_intake.ops.explore.models import CONTEXT_PATH, ExploreContextV1


def _load_explore_context(workspace: Path, *, change_id: str) -> ExploreContextV1:
    path = workspace_file(workspace, CONTEXT_PATH)
    try:
        context = ExploreContextV1.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError) as error:
        raise OutputError(f"explore context.json is missing or invalid in staging: {error}") from error
    if context.change_id != change_id:
        raise OutputError("explore context.json change_id does not match its change directory")
    return context


def _advisory_evidence_ids(document: ExploreAdvisoryV1) -> frozenset[str]:
    cited: set[str] = set()
    groups: tuple[list[object], ...] = (
        document.watchlist,
        document.case_design_guidance.priority_hints,
        document.case_design_guidance.suggested_scenarios,
        document.case_design_guidance.regression_focus,
    )
    for group in groups:
        for item in group:
            if not isinstance(item, Mapping):
                continue
            ids = item.get("evidence_ids")
            if isinstance(ids, list):
                cited.update(value for value in ids if isinstance(value, str))
    for row in document.test_strategy.layer_recommendation:
        cited.update(row.evidence_ids)
    return frozenset(cited)


def validate_explore_outputs(
    workspace: Path,
    declared: tuple[str, ...],
    *,
    change_id: str,
    capability_leafs: frozenset[str],
) -> None:
    advisory: ExploreAdvisoryV1 | None = None
    inventory: ChangeImpactInventoryV1 | None = None
    for relative in declared:
        parts = PurePosixPath(relative).parts
        if relative.endswith("/explore/exploration-draft.json"):
            if parts != ("qa", "results", "explore", "exploration-draft.json"):
                raise OutputError(f"invalid exploration-draft.json path: {relative}")
            path = workspace_file(workspace, relative)
            try:
                advisory = ExploreAdvisoryV1.model_validate_json(path.read_bytes())
            except (OSError, ValidationError, ValueError) as error:
                raise OutputError(f"invalid exploration-draft.json: {error}") from error
            if advisory.change_id != change_id:
                raise OutputError("exploration-draft.json change_id does not match its change directory")
            if advisory.context_ref != "explore/context.json":
                raise OutputError("exploration-draft.json context_ref must be explore/context.json")
        elif relative.endswith("/explore/impact-inventory.json"):
            if parts != ("qa", "results", "explore", "impact-inventory.json"):
                raise OutputError(f"invalid impact-inventory.json path: {relative}")
            path = workspace_file(workspace, relative)
            try:
                inventory = ChangeImpactInventoryV1.model_validate_json(path.read_bytes())
            except (OSError, ValidationError, ValueError) as error:
                raise OutputError(f"invalid impact-inventory.json: {error}") from error
            if inventory.change_id != change_id:
                raise OutputError("impact-inventory.json change_id does not match its change directory")
    if advisory is None or inventory is None:
        return
    context = _load_explore_context(workspace, change_id=change_id)
    resolvable = context.impact.resolvable_ids() | frozenset(
        item.id for item in advisory.source_code_evidence
    )
    resolvable |= frozenset(
        item["id"]
        for item in context.evidence
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    )
    unresolvable = sorted(_advisory_evidence_ids(advisory) - resolvable)
    if unresolvable:
        raise OutputError(f"exploration.json cites unresolvable evidence ids: {unresolvable}")
    try:
        validate_inventory_references(
            inventory,
            resolvable=resolvable,
            seed_ids=context.impact.seed_ids(),
            capability_leafs=capability_leafs,
        )
    except ValueError as error:
        raise OutputError(f"impact-inventory.json: {error}") from error


def _trusted_sources(workspace: Path) -> TrustedIntakeSourcesV1 | None:
    requirement = workspace.joinpath(*REQUIREMENT_PATH.split("/"))
    snapshot = workspace.joinpath(*RUN_SPEC_SNAPSHOT_PATH.split("/"))
    if not requirement.is_file() or requirement.is_symlink():
        return None
    if not snapshot.is_file() or snapshot.is_symlink():
        return None
    requirement_bytes = requirement.read_bytes()
    snapshot_bytes = snapshot.read_bytes()
    families: tuple[TestFamily, ...] = ()
    try:
        document = yaml.safe_load(snapshot_bytes)
        raw = document.get("candidate_test_families") if isinstance(document, Mapping) else None
        if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
            families = cast(tuple[TestFamily, ...], tuple(raw))
    except yaml.YAMLError:
        return None
    return TrustedIntakeSourcesV1(
        requirement_ref=EvidenceArtifactRefV1(path=REQUIREMENT_PATH, digest=file_digest(requirement_bytes)),
        run_spec_ref=EvidenceArtifactRefV1(path=RUN_SPEC_SNAPSHOT_PATH, digest=file_digest(snapshot_bytes)),
        accepted_input_digest=file_digest(requirement_bytes),
        candidate_test_families=families,
    )


def seal_official_exploration(
    workspace: Path,
    advisory: ExploreAdvisoryV1,
    *,
    candidate_families: frozenset[str],
    policy_required: frozenset[str],
) -> tuple[PreparedExploreV1, bytes]:
    requirement = workspace.joinpath(*REQUIREMENT_PATH.split("/"))
    text = (
        requirement.read_text(encoding="utf-8")
        if requirement.is_file() and not requirement.is_symlink()
        else ""
    )
    digest = file_digest(requirement.read_bytes()) if requirement.is_file() else ""
    resolved: dict[tuple[str, str], SourceRefV1] = {}
    quotes = [
        quote
        for draft in advisory.minimum_required_coverage
        for quote in (
            *draft.basis_quotes,
            *(item for goal in draft.observation_goals for item in goal.basis_quotes),
        )
    ]
    for quote in quotes:
        if quote.source_id != "requirement" or not text:
            continue
        try:
            start, end = resolve_requirement_quote(text, quote.quote, quote.context_quote)
        except ValueError:
            continue
        resolved[(quote.source_id, quote.quote)] = SourceRefV1(
            kind="requirement",
            artifact=EvidenceArtifactRefV1(path=REQUIREMENT_PATH, digest=digest),
            locator=f"bytes:{start}-{end}",
        )
    rows = normalize_obligation_drafts(advisory.minimum_required_coverage, resolved_quotes=resolved)
    sources = _trusted_sources(workspace)
    sealed: list[object] = []
    for row in rows:
        bases = []
        for basis in row.expected_basis_refs:
            status = "pending"
            if sources is not None:
                try:
                    authenticate_source(
                        basis.source,
                        purpose="expected_basis",
                        workspace=workspace,
                        sources=sources,
                    )
                    status = "authenticated"
                except ValueError:
                    status = "pending"
            bases.append(ExpectedBasisV1(source=basis.source, source_status=status))
        sealed.append(row.model_copy(update={"expected_basis_refs": tuple(bases)}))
    if sources is not None:
        sealed = list(
            apply_scope_exclusions(
                tuple(sealed),  # type: ignore[arg-type]
                candidate_families=candidate_families or frozenset(sources.candidate_test_families),
                policy_required_families=policy_required,
                exclusion_basis=SourceRefV1(
                    kind="decision",
                    artifact=sources.run_spec_ref,
                    locator="/candidate_test_families",
                ),
            )
        )
    official = PreparedExploreV1(
        schema_version="1",
        change_id=advisory.change_id,
        context_ref=advisory.context_ref,
        generated_at=advisory.generated_at,
        executive_summary=advisory.executive_summary,
        watchlist=tuple(advisory.watchlist),
        evidence_inventory=advisory.evidence_inventory,
        source_code_evidence=tuple(advisory.source_code_evidence),
        case_design_guidance=advisory.case_design_guidance,
        minimum_required_coverage=tuple(sealed),  # type: ignore[arg-type]
        open_questions_for_case_design=tuple(advisory.open_questions_for_case_design),
        test_strategy=advisory.test_strategy,
    )
    data = canonical_json_bytes(official.model_dump(mode="json")) + b"\n"
    destination = workspace.joinpath(*EXPLORATION_PATH.split("/"))
    if destination.exists() or destination.is_symlink():
        raise OutputError("official exploration.json must be written by finalize only")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return official, data
