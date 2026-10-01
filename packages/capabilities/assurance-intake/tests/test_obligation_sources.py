from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from typing import cast

from assurance_intake.domain.obligations import TrustedIntakeSourcesV1
from assurance_intake.contracts.common import TestFamily
from assurance_intake.domain.explore_context import build_explore_context
from assurance_intake.contracts.obligations import SourceRefV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.domain.obligations import (
    authenticate_source,
    build_source_index,
    resolve_requirement_quote,
    scope_exclusion_allowed,
)


def test_quote_offsets_are_computed_by_host() -> None:
    assert resolve_requirement_quote("前文\n锁定返回423", "锁定返回423") == (7, 22)
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_requirement_quote("拒绝登录；拒绝登录", "拒绝登录")


def test_context_quote_disambiguates_then_fails_when_still_ambiguous() -> None:
    text = "前文锁定返回423；后文锁定返回423"
    assert resolve_requirement_quote(text, "锁定返回423", "前文锁定返回423") == (6, 21)
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_requirement_quote("拒绝登录；拒绝登录", "拒绝登录", "拒绝登录；拒绝登录")


def test_api_only_excludes_e2e_but_not_both_or_policy_required() -> None:
    api = frozenset({"api"})
    assert scope_exclusion_allowed(
        obligation_families=frozenset({"e2e"}),
        candidate_families=api,
        policy_required_families=frozenset(),
    )
    assert not scope_exclusion_allowed(
        obligation_families=frozenset({"api", "e2e"}),
        candidate_families=api,
        policy_required_families=frozenset(),
    )
    assert not scope_exclusion_allowed(
        obligation_families=frozenset({"e2e"}),
        candidate_families=api,
        policy_required_families=frozenset({"e2e"}),
    )


def _write(root: Path, relative: str, data: bytes) -> EvidenceArtifactRefV1:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(data).hexdigest())


def _sources(tmp_path: Path, *, families: tuple[str, ...] = ("api",)) -> TrustedIntakeSourcesV1:
    requirement = _write(tmp_path, "qa/requirement.md", "锁定后返回423\n".encode())
    snapshot = yaml.safe_dump({"candidate_test_families": list(families)}, sort_keys=True).encode()
    run_spec = _write(tmp_path, "qa/results/intake/sources/run-spec.effective.yaml", snapshot)
    return TrustedIntakeSourcesV1(
        requirement_ref=requirement,
        run_spec_ref=run_spec,
        accepted_input_digest=requirement.digest,
        candidate_test_families=cast(tuple[TestFamily, ...], families),
    )


def test_run_spec_cannot_authorize_expected_basis(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    index = build_source_index(sources, workspace=tmp_path)
    run_spec = index["run-spec"][0]
    with pytest.raises(ValueError, match="expected_basis"):
        authenticate_source(
            run_spec,
            purpose="expected_basis",
            workspace=tmp_path,
            sources=sources,
        )
    authenticate_source(
        run_spec,
        purpose="scope_exclusion",
        workspace=tmp_path,
        sources=sources,
    )


def test_unregistered_contract_and_case_id_are_rejected(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    contract = SourceRefV1(
        kind="contract",
        artifact=sources.requirement_ref,
        locator="/openapi/login",
    )
    with pytest.raises(ValueError, match="contract"):
        authenticate_source(
            contract,
            purpose="expected_basis",
            workspace=tmp_path,
            sources=sources,
        )
    case = SourceRefV1(
        kind="case",
        artifact=sources.requirement_ref,
        locator="CASE-OLD",
    )
    with pytest.raises(ValueError, match="case"):
        authenticate_source(
            case,
            purpose="expected_basis",
            workspace=tmp_path,
            sources=sources,
        )


def test_forged_run_spec_is_rejected(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    forged = _write(
        tmp_path,
        "qa/results/intake/sources/run-spec.forged.yaml",
        yaml.safe_dump({"candidate_test_families": ["e2e"]}, sort_keys=True).encode(),
    )
    fake = SourceRefV1(kind="decision", artifact=forged, locator="/candidate_test_families")
    with pytest.raises(ValueError, match="trusted"):
        authenticate_source(
            fake,
            purpose="scope_exclusion",
            workspace=tmp_path,
            sources=sources,
        )


def test_requirement_suffix_after_2000_chars_is_in_context(tmp_path: Path) -> None:
    prefix = "x" * 2000
    clause = "锁定返回423"
    _write(tmp_path, "qa/requirement.md", f"{prefix}{clause}\n".encode())
    context = build_explore_context(tmp_path, change_id="CH-1", capability_leafs=())
    assert context.requirement_summary is not None
    assert clause in context.requirement_summary
    assert context.requirement_read_facts.read_state == "complete"
    assert resolve_requirement_quote(context.requirement_summary, clause)[0] == len(prefix.encode())


def test_truncated_requirement_cannot_claim_unread_suffix(tmp_path: Path) -> None:
    suffix = "后半条款必须检查"
    data = ("前半" + ("y" * 70000) + suffix).encode("utf-8")
    _write(tmp_path, "qa/requirement.md", data)
    context = build_explore_context(tmp_path, change_id="CH-1", capability_leafs=())
    assert context.requirement_read_facts.read_state == "truncated"
    assert context.requirement_read_facts.provided_bytes <= 65536
    assert suffix not in (context.requirement_summary or "")
    with pytest.raises(ValueError, match="not found"):
        resolve_requirement_quote(context.requirement_summary or "", suffix)
