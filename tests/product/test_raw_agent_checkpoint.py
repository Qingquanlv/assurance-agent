from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.product.composition_harness import request_for
from tests.product.test_feature_graph_bundles import PUBLIC_BUNDLE_FIELDS, _build_owner

_T5A_LANGGRAPH = frozenset(
    {
        "improvement-evaluate",
        "improvement-export",
        "improvement-apply",
        "improvement-rollback",
    }
)
_CASE_DESIGN_ID = "assurance.intake.agent.case-design.v1"
_EVALUATE_ID = "assurance.improvement.task.evaluate-memory-improvement"
_STRUCTURED_ARTIFACT_MARKERS = (
    "class ArtifactContract",
    "materialization_receipt",
    "typed artifact slot",
    "StructuredArtifact",
)
_PRODUCTION_ROOTS = (
    "packages/adapters/agent-runtime-opencode/agent_runtime_opencode",
    "packages/adapters/agent-runtime-contracts/agent_runtime_contracts",
    "packages/products/assurance-product/assurance_product",
    "packages/capabilities",
    "packages/framework/graph-engine/graph_engine",
)
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _feature_bound_ids() -> tuple[str, ...]:
    bound: list[str] = []
    for owner_id in PUBLIC_BUNDLE_FIELDS:
        _bundle, context, _digest = _build_owner(owner_id)
        bound.extend(context.bound_contract_ids)
    return tuple(bound)


def _candidate_sha() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=_REPO_ROOT,
        text=True,
    ).strip()


def test_checkpoint_r_inventory_is_33_33_34_41_43() -> None:
    from assurance_product.agent_contracts import (
        LEGACY_AGENT_PHASE_ALIASES,
        all_feature_agent_contracts,
        all_feature_task_contracts,
    )
    from assurance_product.runtime_bindings import AGENT_RUNTIME_BINDINGS, RAW_AGENT_RUNTIME_BINDING_ROWS

    contracts = all_feature_agent_contracts()
    tasks = {contract.contract_id: contract for contract in all_feature_task_contracts().values()}
    bound = _feature_bound_ids()
    agent_bound = tuple(item for item in bound if item in contracts)
    task_bound = tuple(item for item in bound if item in tasks)

    assert len(contracts) == 33
    assert len(AGENT_RUNTIME_BINDINGS) == 33
    assert set(AGENT_RUNTIME_BINDINGS) == set(contracts)
    assert len(RAW_AGENT_RUNTIME_BINDING_ROWS) == 33
    assert len(contracts) + len(tasks) == 41
    assert len(LEGACY_AGENT_PHASE_ALIASES) == 99
    assert all("alias" not in row.schema_version for row in RAW_AGENT_RUNTIME_BINDING_ROWS)

    assert set(agent_bound) == set(contracts)
    assert set(task_bound) == set(tasks)
    assert agent_bound.count(_CASE_DESIGN_ID) == 2
    assert task_bound.count(_EVALUATE_ID) == 2

    agent_occurrences = len(set(agent_bound)) + (agent_bound.count(_CASE_DESIGN_ID) - 1)
    attempt_occurrences = (
        len(set(agent_bound) | set(task_bound))
        + (agent_bound.count(_CASE_DESIGN_ID) - 1)
        + (task_bound.count(_EVALUATE_ID) - 1)
    )
    assert agent_occurrences == 34
    assert attempt_occurrences == 43
    assert len(contracts) + len(tasks) + 2 == 43


def test_t5a_roots_stay_langgraph_and_ten_agent_roots_stay_legacy() -> None:
    from assurance_product.models import ENTRYPOINT_RUNTIME_CUTOVER, PRODUCT_ENTRYPOINTS
    from assurance_product.runtime_selection import ENTRYPOINT_AGENT_CONTRACT_IDS, select_runtime

    assert set(ENTRYPOINT_RUNTIME_CUTOVER) == set(PRODUCT_ENTRYPOINTS)
    flipped = {name for name, kind in ENTRYPOINT_RUNTIME_CUTOVER.items() if kind == "langgraph-v1"}
    leftover = {name for name, kind in ENTRYPOINT_RUNTIME_CUTOVER.items() if kind == "legacy-v2"}
    assert flipped == set(_T5A_LANGGRAPH)
    assert len(leftover) == 10
    for name in _T5A_LANGGRAPH:
        assert select_runtime(name) == "langgraph-v1"
        assert ENTRYPOINT_AGENT_CONTRACT_IDS[name] == ()
    for name in leftover:
        assert select_runtime(name) == "legacy-v2"
        assert ENTRYPOINT_AGENT_CONTRACT_IDS[name]


def test_checkpoint_r_records_candidate_lock_and_revision(installed_sources) -> None:
    from assurance_product.product import (
        coexistence_graph_manifest,
        product_lock_from_composition,
        resolve_assurance_composition,
    )
    from assurance_product.runtime_bindings import RAW_AGENT_RUNTIME_BINDING_ROWS

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    product_lock = product_lock_from_composition(composition)
    manifest = coexistence_graph_manifest(composition, product_lock)
    rows = RAW_AGENT_RUNTIME_BINDING_ROWS
    sha = _candidate_sha()

    assert len(sha) == 40
    assert len(product_lock.digest) == 64
    assert len(manifest.revision.revision_id) == 64
    assert manifest.revision.product_lock_digest == product_lock.digest
    assert composition.lock.digest
    assert {row.adapter for row in rows} == {"opencode"}
    assert {row.provider for row in rows} == {"opencode"}
    assert {row.model for row in rows} == {"fixture-model"}
    assert len(composition.semantic_attempt_contracts) == 41
    print(
        "checkpoint-r "
        f"candidate_sha={sha} "
        f"product_lock={product_lock.digest} "
        f"graph_revision={manifest.revision.revision_id} "
        "adapter=opencode provider=opencode model=fixture-model"
    )


def test_checkpoint_r_proves_no_structured_artifact_pipeline() -> None:
    hits: list[str] = []
    for root in _PRODUCTION_ROOTS:
        path = _REPO_ROOT / root
        files: Iterator[Path] = path.rglob("*.py") if path.is_dir() else iter(())
        for file in files:
            if "tests" in file.parts:
                continue
            text = file.read_text(encoding="utf-8")
            for marker in _STRUCTURED_ARTIFACT_MARKERS:
                if marker in text:
                    hits.append(f"{file.relative_to(_REPO_ROOT)}:{marker}")
    assert hits == []
    assert not (_REPO_ROOT / "scripts" / "opencode_structured_output_eligibility_probe.py").exists()
    assert not (
        _REPO_ROOT / "tests" / "agent_runtime" / "test_opencode_structured_output_eligibility_probe.py"
    ).exists()
    opencode = _REPO_ROOT / "packages" / "adapters" / "agent-runtime-opencode" / "agent_runtime_opencode"
    adapter_text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(opencode.rglob("*.py")))
    assert "format.type" not in adapter_text
    assert 'format": "json_schema"' not in adapter_text
    assert "json_schema" not in adapter_text


def _live_opencode_ready() -> bool:
    if shutil.which("opencode") is None:
        return False
    if os.environ.get("AA_CHECKPOINT_R_LIVE") != "1":
        return False
    return bool(os.environ.get("OPENCODE_API_KEY") or os.environ.get("OPENCODE_SERVER_PASSWORD"))


@pytest.mark.skipif(
    not _live_opencode_ready(),
    reason="official OpenCode binary plus AA_CHECKPOINT_R_LIVE=1 and operator credentials are required",
)
def test_live_opencode_cutover_binding_records_checkpoint_r(installed_sources) -> None:
    from assurance_product.product import (
        coexistence_graph_manifest,
        product_lock_from_composition,
        resolve_assurance_composition,
    )
    from assurance_product.runtime_bindings import RAW_AGENT_RUNTIME_BINDING_ROWS

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    product_lock = product_lock_from_composition(composition)
    manifest = coexistence_graph_manifest(composition, product_lock)
    row = RAW_AGENT_RUNTIME_BINDING_ROWS[0]
    assert row.adapter == "opencode"
    assert row.provider == "opencode"
    assert row.model == "fixture-model"
    assert shutil.which("opencode") is not None
    print(
        "checkpoint-r-live "
        f"candidate_sha={_candidate_sha()} "
        f"product_lock={product_lock.digest} "
        f"graph_revision={manifest.revision.revision_id} "
        f"adapter={row.adapter} provider={row.provider} model={row.model}"
    )
