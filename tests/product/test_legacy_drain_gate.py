from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from graph_engine.boot.boot import BootValidationError
from graph_engine.canonical import canonical_digest
from graph_engine.evidence.legacy_v2 import (
    EMPTY_RUNTIME_AUTHORIZATION_DIGEST,
    GraphCompleted,
    GraphStarted,
    InvocationFinished,
    InvocationStarted,
    Ledger,
    NodeActivated,
    NodeInterrupted,
    TaskAttemptStarted,
    TaskAttemptStopped,
    empty_invocation_seed,
)

from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.models import ENTRYPOINT_RUNTIME_CUTOVER, PRODUCT_ENTRYPOINTS
from assurance_product.product import (
    coexistence_graph_manifest,
    product_lock_from_composition,
    resolve_assurance_composition,
)
from assurance_product.revision_registry import (
    CHECKPOINT_R_RELEASED_SHA,
    DrainAuthorizationError,
    DrainEvidence,
    EXPECTED_JOIN_ANY_ROWS,
    OperatorTerminalRecord,
    RevisionRegistry,
    authorize_legacy_deletion,
    collect_drain_evidence,
)
from assurance_product.runtime_selection import (
    LangGraphRuntimeRecord,
    LegacyRuntimeRecord,
    complete_initialized,
    load_selection,
    select_runtime,
    validate_entrypoint_runtime_cutover,
    write_initializing,
)
from tests.product.composition_harness import request_for

_GOLDEN_LOCK = (
    Path(__file__).resolve().parents[2]
    / "packages/framework/graph-engine/tests/composition/invocation-lock-v2.golden.json"
)
_ACTIVE_STATES = (
    "running",
    "interrupted",
    "stopped",
    "publication-indeterminate",
)
_EVIDENCE_GATES = (
    "checkpoint_r_sha",
    "product_lock",
    "graph_revision",
    "adapter_provider_model",
    "contract_inventory",
    "raw_binding_inventory",
    "agent_occurrence_inventory",
    "join_any_rows",
    "loop_scc_anchors",
    "min_matches_mapping",
    "validator_parity",
)
_T5D_RELEASED_SHA = "bd41e0b98055168bdfc4ffdd5f58633f60609b2e"


def _workspace(tmp_path: Path) -> ChangeWorkspace:
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("seed\n", encoding="utf-8")
    return ChangeWorkspace.prepare(project, "CH-DRAIN-001")


def _golden_lock_bytes() -> bytes:
    return _GOLDEN_LOCK.read_text(encoding="utf-8").strip().encode()


def _lock_digest() -> str:
    return hashlib.sha256(_golden_lock_bytes()).hexdigest()


def _started(invocation_id: str) -> InvocationStarted:
    seed = empty_invocation_seed()
    return InvocationStarted(
        invocation_id=invocation_id,
        lock_digest=_lock_digest(),
        entrypoint="archive",
        event_schema_version="2",
        runtime_authorization_digest=EMPTY_RUNTIME_AUTHORIZATION_DIGEST,
        root_input_digest=seed.root_input_digest,
    )


def _events_for_status(invocation_id: str, status: str) -> tuple[object, ...]:
    started = _started(invocation_id)
    graph_started = GraphStarted(graph_instance_id="root", graph_id="root")
    if status == "completed":
        return (
            started,
            graph_started,
            GraphCompleted(graph_instance_id="root"),
            InvocationFinished(invocation_id=invocation_id, status="succeeded"),
        )
    if status == "failed":
        return (
            started,
            graph_started,
            GraphCompleted(graph_instance_id="root"),
            InvocationFinished(invocation_id=invocation_id, status="failed", terminal_reason="failed"),
        )
    if status == "running":
        return (started, graph_started)
    if status == "interrupted":
        return (
            started,
            graph_started,
            NodeActivated(
                activation_id="act-1",
                graph_instance_id="root",
                node_id="review",
                token_ids=(),
            ),
            NodeInterrupted(
                activation_id="act-1",
                interrupt_id="int-1",
                graph_instance_id="root",
                reason="review",
                actions=("continue",),
                input=None,
            ),
        )
    if status == "stopped":
        return (
            started,
            graph_started,
            NodeActivated(
                activation_id="act-1",
                graph_instance_id="root",
                node_id="task",
                token_ids=(),
            ),
            TaskAttemptStarted(
                activation_id="act-1",
                attempt=1,
                lease_expires_at="2030-01-01T00:00:00Z",
            ),
            TaskAttemptStopped(activation_id="act-1", attempt=1, reason="stop"),
            InvocationFinished(invocation_id=invocation_id, status="stopped"),
        )
    if status == "publication-indeterminate":
        return (started, graph_started)
    raise ValueError(f"unsupported leftover status plant: {status}")


def _write_legacy(
    workspace: ChangeWorkspace,
    invocation_id: str,
    *,
    status: str,
    lock_bytes: bytes | None = None,
    unreadable: bool = False,
    sidecar_status: str | None = None,
) -> None:
    digest = "a" * 64
    write_initializing(
        workspace,
        LegacyRuntimeRecord(
            phase="initializing",
            invocation_id=invocation_id,
            entrypoint="archive",
            root_input_digest="b" * 64,
            build_identity=digest,
        ),
    )
    complete_initialized(
        workspace,
        LegacyRuntimeRecord(
            phase="initialized",
            invocation_id=invocation_id,
            entrypoint="archive",
            root_input_digest="b" * 64,
            build_identity=digest,
            identity_digest=digest,
        ),
    )
    RevisionRegistry(workspace).bind(invocation_id, runtime="legacy-v2", revision_id=digest)
    invocation = workspace.paths.runtime_root / "invocations" / invocation_id
    invocation.mkdir(parents=True, exist_ok=True)
    lock_path = invocation / "invocation.lock.json"
    if unreadable:
        lock_path.write_bytes(b"{not-json")
        return
    lock_path.write_bytes(lock_bytes if lock_bytes is not None else _golden_lock_bytes())
    if status not in {"unreadable"}:
        Ledger(invocation / "ledger").append_batch(
            _events_for_status(invocation_id, status),  # type: ignore[arg-type]
            expected_next_seq=1,
        )
    if status == "publication-indeterminate":
        (invocation / "ledger" / ".pending-drain.json").write_text("[]\n", encoding="utf-8")
    if sidecar_status is not None:
        (invocation / "legacy-drain.json").write_text(
            json.dumps({"status": sidecar_status}, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _operator_record(invocation_id: str, outcome: str = "terminated") -> OperatorTerminalRecord:
    payload = {
        "schema_version": "1",
        "invocation_id": invocation_id,
        "outcome": outcome,
        "operator_id": "operator.drain",
    }
    return OperatorTerminalRecord(
        invocation_id=invocation_id,
        outcome=outcome,  # type: ignore[arg-type]
        operator_id="operator.drain",
        record_digest=canonical_digest(payload),
    )


@pytest.fixture(scope="module")
def live_composition(installed_sources):
    return resolve_assurance_composition(request_for("opencode", installed_sources))


def _green_evidence(composition=None, **overrides: object) -> DrainEvidence:
    evidence = collect_drain_evidence(composition) if composition is not None else collect_drain_evidence()
    data = evidence.model_dump(mode="json")
    data.update(overrides)
    return DrainEvidence.model_validate(data)


def test_production_cutover_forbids_legacy_values() -> None:
    assert set(ENTRYPOINT_RUNTIME_CUTOVER) == set(PRODUCT_ENTRYPOINTS)
    assert set(ENTRYPOINT_RUNTIME_CUTOVER.values()) == {"langgraph-v1"}
    for name in PRODUCT_ENTRYPOINTS:
        assert select_runtime(name) == "langgraph-v1"
    with pytest.raises(BootValidationError, match="langgraph-v1"):
        validate_entrypoint_runtime_cutover({name: "legacy-v2" for name in PRODUCT_ENTRYPOINTS})


@pytest.mark.parametrize("status", _ACTIVE_STATES)
def test_deletion_authorization_fails_on_active_legacy(tmp_path: Path, live_composition, status: str) -> None:
    workspace = _workspace(tmp_path)
    _write_legacy(workspace, f"inv-active-{status}", status=status)
    with pytest.raises(DrainAuthorizationError, match=status.replace("-", " ") if "-" in status else status):
        authorize_legacy_deletion(workspace, evidence=_green_evidence(live_composition))


def test_deletion_authorization_fails_on_unreadable_identity(tmp_path: Path, live_composition) -> None:
    workspace = _workspace(tmp_path)
    _write_legacy(workspace, "inv-unreadable", status="completed", unreadable=True)
    with pytest.raises(DrainAuthorizationError, match="unreadable"):
        authorize_legacy_deletion(workspace, evidence=_green_evidence(live_composition))


def test_stopped_legacy_is_resumable_without_operator_record(tmp_path: Path, live_composition) -> None:
    workspace = _workspace(tmp_path)
    _write_legacy(workspace, "inv-stopped", status="stopped")
    with pytest.raises(DrainAuthorizationError, match="resumable"):
        authorize_legacy_deletion(workspace, evidence=_green_evidence(live_composition))


def test_operator_terminal_record_clears_stopped_legacy(tmp_path: Path, live_composition) -> None:
    workspace = _workspace(tmp_path)
    _write_legacy(workspace, "inv-stopped-cleared", status="stopped")
    authorize_legacy_deletion(
        workspace,
        evidence=_green_evidence(live_composition),
        operator_records=(_operator_record("inv-stopped-cleared"),),
    )


def test_operator_terminal_record_clears_running_leftover_without_change_root_status(
    tmp_path: Path, live_composition
) -> None:
    workspace = _workspace(tmp_path)
    _write_legacy(workspace, "inv-running-cleared", status="running")
    assert not (workspace.paths.change_root / "status.json").exists()
    with pytest.raises(DrainAuthorizationError, match="running"):
        authorize_legacy_deletion(workspace, evidence=_green_evidence(live_composition))
    authorize_legacy_deletion(
        workspace,
        evidence=_green_evidence(live_composition),
        operator_records=(_operator_record("inv-running-cleared"),),
    )


def test_succeeded_leftover_ledger_does_not_block_authorization(tmp_path: Path, live_composition) -> None:
    workspace = _workspace(tmp_path)
    _write_legacy(workspace, "inv-succeeded-fold", status="completed")
    sidecar = workspace.paths.runtime_root / "invocations" / "inv-succeeded-fold" / "legacy-drain.json"
    assert not sidecar.exists()
    result = authorize_legacy_deletion(workspace, evidence=_green_evidence(live_composition))
    assert result.authorized is True
    assert result.active_legacy == 0


@pytest.mark.parametrize(
    "status_payload",
    (
        {"status": "blocked", "publication": {"status": "not_ready"}},
        {"status": "running", "publication": {"status": "drifted"}},
    ),
    ids=("blocked", "publication-drifted"),
)
def test_succeeded_leftover_ledger_authorizes_when_change_root_status_json_is_blocked_or_drifted(
    tmp_path: Path, live_composition, status_payload: dict[str, object]
) -> None:
    workspace = _workspace(tmp_path)
    invocation_id = "inv-succeeded-vs-change-status"
    _write_legacy(workspace, invocation_id, status="completed")
    (workspace.paths.change_root / "status.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "invocation_id": "inv-current-langgraph",
                "status": status_payload["status"],
                "publication": status_payload["publication"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    result = authorize_legacy_deletion(workspace, evidence=_green_evidence(live_composition))
    assert result.authorized is True
    assert result.active_legacy == 0


def test_drain_scan_ignores_legacy_drain_sidecar(tmp_path: Path, live_composition) -> None:
    workspace = _workspace(tmp_path)
    _write_legacy(
        workspace,
        "inv-sidecar-lie",
        status="completed",
        sidecar_status="running",
    )
    result = authorize_legacy_deletion(workspace, evidence=_green_evidence(live_composition))
    assert result.authorized is True
    assert result.active_legacy == 0


@pytest.mark.parametrize("gate", _EVIDENCE_GATES)
def test_evidence_gate_failure_blocks_zero_legacy_authorization(
    tmp_path: Path, live_composition, gate: str
) -> None:
    workspace = _workspace(tmp_path)
    broken = {
        "checkpoint_r_sha": {"candidate_sha": "deadbeef"},
        "product_lock": {"product_lock_digest": ""},
        "graph_revision": {"graph_revision_id": ""},
        "adapter_provider_model": {"adapter": "cursor", "provider": "cursor", "model": "other"},
        "contract_inventory": {"contract_count": 32},
        "raw_binding_inventory": {"binding_count": 32},
        "agent_occurrence_inventory": {"agent_occurrence_count": 33},
        "join_any_rows": {"join_any_statuses": {"missing-row": "missing"}},
        "loop_scc_anchors": {"loop_scc_anchors": (("shifted", "anchor"),)},
        "min_matches_mapping": {
            "min_matches_mapping": {
                "assurance.generation.workflow.graph.generation/fanout": "send",
                "assurance.intake.workflow.graph.case-design/prepare": "send",
                "assurance.intake.workflow.graph.case-design/repair-prepare": "composite",
            }
        },
        "validator_parity": {"validator_parity": "missing"},
    }[gate]
    with pytest.raises(DrainAuthorizationError, match="evidence"):
        authorize_legacy_deletion(workspace, evidence=_green_evidence(live_composition, **broken))


def test_join_any_xfail_or_waiver_blocks_authorization(tmp_path: Path, live_composition) -> None:
    workspace = _workspace(tmp_path)
    live = collect_drain_evidence(live_composition)
    statuses = dict(live.join_any_statuses)
    first = next(iter(statuses))
    statuses[first] = "xfailed"
    with pytest.raises(DrainAuthorizationError, match="join"):
        authorize_legacy_deletion(
            workspace, evidence=_green_evidence(live_composition, join_any_statuses=statuses)
        )
    statuses[first] = "waived"
    with pytest.raises(DrainAuthorizationError, match="join"):
        authorize_legacy_deletion(
            workspace, evidence=_green_evidence(live_composition, join_any_statuses=statuses)
        )


def test_collect_drain_evidence_binds_live_composition_lock_and_revision(live_composition) -> None:
    product_lock = product_lock_from_composition(live_composition)
    manifest = coexistence_graph_manifest(live_composition, product_lock)
    evidence = collect_drain_evidence(live_composition)
    assert evidence.product_lock_digest == product_lock.digest
    assert evidence.graph_revision_id == manifest.revision.revision_id
    assert evidence.product_lock_digest != canonical_digest(
        {"gate": "checkpoint-r", "artifact": "ProductLock"}
    )
    assert evidence.graph_revision_id != canonical_digest(
        {"gate": "checkpoint-r", "artifact": "GraphRevision"}
    )
    assert evidence.adapter == "opencode"
    assert evidence.provider == "opencode"
    assert evidence.model == "fixture-model"
    assert evidence.contract_count == 33
    assert evidence.binding_count == 33
    assert evidence.agent_occurrence_count == 34


def test_join_gate_uses_live_collector_not_expected_copy(monkeypatch, live_composition) -> None:
    monkeypatch.setitem(
        collect_drain_evidence.__globals__,
        "_live_join_any_statuses",
        lambda: {"only-live-row": "missing"},
    )
    evidence = collect_drain_evidence(live_composition)
    assert evidence.join_any_statuses == {"only-live-row": "missing"}
    assert set(evidence.join_any_statuses) != set(EXPECTED_JOIN_ANY_ROWS)


def test_scc_and_min_matches_use_live_collectors(monkeypatch, live_composition) -> None:
    monkeypatch.setitem(
        collect_drain_evidence.__globals__,
        "_live_loop_scc_anchors",
        lambda: (("live", "anchor"),),
    )
    monkeypatch.setitem(
        collect_drain_evidence.__globals__,
        "_live_min_matches_mapping",
        lambda: {"live/site": "send"},
    )
    evidence = collect_drain_evidence(live_composition)
    assert evidence.loop_scc_anchors == (("live", "anchor"),)
    assert dict(evidence.min_matches_mapping) == {"live/site": "send"}


def test_checkpoint_r_sha_is_exact_released_t5d(tmp_path: Path, live_composition) -> None:
    assert CHECKPOINT_R_RELEASED_SHA == _T5D_RELEASED_SHA
    assert len(CHECKPOINT_R_RELEASED_SHA) == 40
    evidence = collect_drain_evidence(live_composition)
    assert evidence.candidate_sha == _T5D_RELEASED_SHA
    result = authorize_legacy_deletion(_workspace(tmp_path), evidence=evidence)
    assert result.authorized is True


def test_cited_join_test_status_is_missing_when_file_or_trigger_absent(tmp_path: Path, monkeypatch) -> None:
    from assurance_product.revision_registry import _cited_join_test_status

    missing = tmp_path / "absent_join_test.py"
    monkeypatch.setattr(
        "assurance_product.revision_registry._join_test_path",
        lambda _graph_id: missing,
    )
    assert (
        _cited_join_test_status("assurance.product.workflow.graph.product-execute", "failed-join")
        == "missing"
    )
    missing.write_text("def test_other():\n    return None\n", encoding="utf-8")
    assert (
        _cited_join_test_status("assurance.product.workflow.graph.product-execute", "failed-join")
        == "missing"
    )


def test_drain_scan_does_not_read_change_root_status_json() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "packages/products/assurance-product/assurance_product/revision_registry.py"
    ).read_text(encoding="utf-8")
    assert "_leftover_status_overlay" not in source
    assert 'change_root / "status.json"' not in source


def test_validator_parity_does_not_use_file_substring_waiver() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "packages/products/assurance-product/assurance_product/revision_registry.py"
    ).read_text(encoding="utf-8")
    assert '"waiver" in text' not in source
    assert '"pytest.mark.xfail" in text' not in source


def test_validator_parity_ignores_unrelated_waiver_substring(tmp_path: Path, monkeypatch) -> None:
    fake = tmp_path / "test_validator_shadow_parity.py"
    fake.write_text(
        "def test_accepted_candidate_validates_once_and_promotes_on_both_runtimes():\n"
        "    # mention waiver and xfail only in a comment\n"
        "    return None\n"
        "def test_rejected_candidate_validates_once_and_never_prepares_or_promotes():\n"
        "    return None\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(collect_drain_evidence.__globals__, "_validator_parity_path", lambda: fake)
    evidence = collect_drain_evidence()
    assert evidence.validator_parity == "passed"


def test_zero_legacy_with_green_evidence_authorizes(tmp_path: Path, live_composition) -> None:
    workspace = _workspace(tmp_path)
    result = authorize_legacy_deletion(workspace, evidence=_green_evidence(live_composition))
    assert result.authorized is True
    assert result.active_legacy == 0
    assert CHECKPOINT_R_RELEASED_SHA == _T5D_RELEASED_SHA


def test_completed_legacy_does_not_block_authorization(tmp_path: Path, live_composition) -> None:
    workspace = _workspace(tmp_path)
    _write_legacy(workspace, "inv-done", status="completed")
    result = authorize_legacy_deletion(workspace, evidence=_green_evidence(live_composition))
    assert result.authorized is True
    assert result.active_legacy == 0


def test_legacy_marked_reopen_keeps_old_artifact_while_new_start_is_langgraph(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    leftover = LegacyRuntimeRecord(
        phase="initialized",
        invocation_id="inv-leftover",
        entrypoint="archive",
        root_input_digest="c" * 64,
        build_identity="d" * 64,
        identity_digest="d" * 64,
    )
    write_initializing(
        workspace,
        LegacyRuntimeRecord(
            phase="initializing",
            invocation_id=leftover.invocation_id,
            entrypoint=leftover.entrypoint,
            root_input_digest=leftover.root_input_digest,
            build_identity=leftover.build_identity,
        ),
    )
    complete_initialized(workspace, leftover)
    existing = load_selection(workspace, leftover.invocation_id)
    assert existing is not None
    assert existing.runtime == "legacy-v2"
    assert select_runtime("archive") == "langgraph-v1"
    write_initializing(
        workspace,
        LangGraphRuntimeRecord(
            phase="initializing",
            invocation_id="inv-new-same-entrypoint",
            entrypoint="archive",
            root_input_digest="e" * 64,
            build_identity="f" * 64,
        ),
    )
    newest = load_selection(workspace, "inv-new-same-entrypoint")
    assert newest is not None
    assert newest.runtime == "langgraph-v1"
    assert load_selection(workspace, leftover.invocation_id) == existing


def test_legacy_drain_does_not_retire_pre_closure_langgraph_revision(
    tmp_path: Path, live_composition
) -> None:
    from graph_engine.boot.graph_revision import GraphRevision

    workspace = _workspace(tmp_path)
    revision = GraphRevision.build(
        product_lock_digest="a" * 64,
        wheel_source_digests={"assurance-product": "b" * 64},
        factory_symbols=("assurance_product.graphs.factory:build_product_graphs",),
        state_schema_versions={"product": "1"},
        langgraph_version="0.0.0",
        checkpoint_contract_version="1",
    )
    registry = RevisionRegistry(workspace)
    registry.remember(revision)
    registry.bind("inv-lg-live", runtime="langgraph-v1", revision_id=revision.revision_id)
    result = authorize_legacy_deletion(workspace, evidence=_green_evidence(live_composition))
    assert result.authorized is True
    assert result.active_legacy == 0
    with pytest.raises(Exception, match="resumable Invocation"):
        registry.retire(revision.revision_id)
    assert registry.get(revision.revision_id).revision_id == revision.revision_id
