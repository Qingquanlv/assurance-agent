"""Mechanical checks of the existing plan tables against authoritative inputs."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping
import re
from typing import Any

from assurance_generation.contracts.codegen import CodegenMapping
from assurance_generation.contracts.plans import ObservationBindingV1
from assurance_generation.contracts.reviews import ObligationSemanticReviewV1
from assurance_intake.contracts.obligations import PreparedObligationV1, VerificationRequirementV1


def _tables(text: str) -> Iterator[tuple[str, tuple[str, ...], list[tuple[str, ...]]]]:
    section = ""
    header: tuple[str, ...] = ()
    rows: list[tuple[str, ...]] = []
    fence: str | None = None
    for line in (*text.splitlines(), ""):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if fence is None:
                if header:
                    yield section, header, rows
                header, rows = (), []
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None:
            continue
        if not line.strip().startswith("|"):
            if header:
                yield section, header, rows
            header, rows = (), []
            if line.startswith("#"):
                section = line.lstrip("#").strip()
            continue
        cells = tuple(cell.strip().strip("`") for cell in line.strip().strip("|").split("|"))
        if all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            continue
        if not header:
            header = cells
        else:
            rows.append(cells)


def check_plan_consistency(
    images: Mapping[str, bytes],
    *,
    mapping: CodegenMapping,
    facts: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return all proved contradictions; unparsed prose and unknowns need Review.

    These checks supplement the closed JSON mapping. They never infer fixture
    absence or product semantics from an incomplete static inventory.
    """
    errors: list[str] = []
    leafs = set(facts["capability_leafs"])
    observed = {
        (file["path"], symbol["name"]) for file in facts["files"] for symbol in file.get("symbols", ())
    }
    expected = Counter((row.case_id, row.symbol, row.target_file) for row in mapping.entries)
    for path, data in sorted(images.items()):
        if not path.endswith(".md"):
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            errors.append(f"{path}: plan Markdown must be UTF-8")
            continue
        for section, header, rows in _tables(text):
            locator = f"{path}#{section}"
            if tuple(name.casefold() for name in header) == ("case id", "test function", "target file"):
                if Counter(rows) != expected:
                    errors.append(f"{locator}: Test Function Mapping contradicts closed codegen mapping")
            for row in rows:
                if len(row) != len(header):
                    # Markdown prose can contain pipes; do not invent a parser
                    # verdict for a row outside these closed table shapes.
                    continue
                fields = dict(zip((name.casefold() for name in header), row, strict=True))
                for name in ("capability", "required capabilities"):
                    for key in re.findall(
                        r"\b(?:auth|accounts|entities|auth_matrix|capabilities)(?:\.[A-Za-z_]\w*)+",
                        fields.get(name, ""),
                    ):
                        if key not in leafs:
                            errors.append(f"{locator}: unknown capability leaf {key}")
                symbol = (fields.get("shared module", ""), fields.get("function", ""))
                if fields.get("ownership") == "create-if-missing" and symbol in observed:
                    errors.append(
                        f"{locator}: {symbol[0]}::{symbol[1]} has an observed definition; create-if-missing contradicts source, use reuse and describe any amendment"
                    )
    return tuple(sorted(set(errors)))


def required_observation_keys(requirement: VerificationRequirementV1) -> frozenset[str]:
    return frozenset(item.observation_key for item in requirement.observations)


def validate_observation_binding(
    requirement: VerificationRequirementV1,
    bindings: tuple[ObservationBindingV1, ...],
) -> None:
    required = required_observation_keys(requirement)
    actual_keys = {item.observation_key for item in bindings}
    actual_ids = [item.observation_id for item in bindings]
    if len(actual_ids) != len(set(actual_ids)) or actual_keys != required:
        raise ValueError("observation bindings must cover frozen semantic requirements")


def expectation_ready(
    obligation: PreparedObligationV1,
    requirement: VerificationRequirementV1,
    observation_key: str,
    review: ObligationSemanticReviewV1 | None,
) -> bool:
    if review is None or obligation.open_questions:
        return False
    observation = next(
        (item for item in requirement.observations if item.observation_key == observation_key),
        None,
    )
    if observation is None or observation.expected is None or not observation.basis_refs:
        return False
    authenticated = {
        (basis.source.kind, basis.source.artifact.path, basis.source.artifact.digest, basis.source.locator)
        for basis in obligation.expected_basis_refs
        if basis.source_status == "authenticated"
    }
    if any(
        (ref.kind, ref.artifact.path, ref.artifact.digest, ref.locator) not in authenticated
        for ref in observation.basis_refs
    ):
        return False
    matches = [item for item in review.expectation_reviews if item.observation_key == observation_key]
    if len(matches) != 1 or matches[0].status != "pass" or not matches[0].reason.strip():
        return False
    observed = {
        (ref.kind, ref.artifact.path, ref.artifact.digest, ref.locator) for ref in observation.basis_refs
    }
    if not matches[0].basis_refs or any(
        (ref.kind, ref.artifact.path, ref.artifact.digest, ref.locator) not in observed
        for ref in matches[0].basis_refs
    ):
        return False
    return review.status == "pass" and review.requirement_id == requirement.requirement_id
