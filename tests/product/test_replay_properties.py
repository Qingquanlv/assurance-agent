from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, TypedDict, cast

import pytest

from tests.product.test_result_export import CHANGE_ID, TARGET_A, TARGET_B, write_achieved

_CRASH_PHASES = ("prepared", "replacing", "committed")
_ORDERED_CRASH_SUBSETS = tuple(
    subset for size in range(len(_CRASH_PHASES) + 1) for subset in combinations(_CRASH_PHASES, size)
)


class _InjectedPublishCrash(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _TerminalProjection:
    receipt: dict[str, Any]
    source_digest: str
    final_digest: str
    target_bytes: tuple[tuple[str, bytes], ...]
    durable_effects: tuple[dict[str, Any], ...]
    journal_records: tuple[dict[str, Any], ...]
    publication_status: str
    transaction_residue: tuple[str, ...]


def _install_durable_effect_projection(project: Path) -> None:
    status_path = project / "qa" / "changes" / CHANGE_ID / "status.json"
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    payload["durable_effects"] = [
        {
            "effect_id": "effect-archive-001",
            "kind": "assurance.improvement.effect.archive.v1",
            "state": "applied",
            "receipt_digest": f"sha256:{'b' * 64}",
        }
    ]
    status_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _terminal_projection(project: Path, receipt: object) -> _TerminalProjection:
    from assurance_product.models import PublishJournalV1, PublishReceiptV1, StatusV1

    typed_receipt = PublishReceiptV1.model_validate(receipt)
    change_root = project / "qa" / "changes" / CHANGE_ID
    durable_status = StatusV1.model_validate_json((change_root / "status.json").read_bytes())
    journal = PublishJournalV1.model_validate_json((change_root / "publish-journal.json").read_bytes())
    residue = tuple(
        path.relative_to(project).as_posix()
        for path in sorted(project.rglob("*"))
        if path.name.endswith((".tmp", ".bak"))
    )
    return _TerminalProjection(
        receipt=typed_receipt.model_dump(mode="json"),
        source_digest=typed_receipt.source_digest,
        final_digest=typed_receipt.final_digest,
        target_bytes=(
            (TARGET_A, (project / TARGET_A).read_bytes()),
            (TARGET_B, (project / TARGET_B).read_bytes()),
        ),
        durable_effects=tuple(item.model_dump(mode="json") for item in durable_status.durable_effects),
        journal_records=tuple(item.model_dump(mode="json") for item in journal.records),
        publication_status=durable_status.publication.status,
        transaction_residue=residue,
    )


def test_publish_replay_matches_uninterrupted_projection_for_every_ordered_crash_subset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assurance_product import export as export_mod

    assert _ORDERED_CRASH_SUBSETS == (
        (),
        ("prepared",),
        ("replacing",),
        ("committed",),
        ("prepared", "replacing"),
        ("prepared", "committed"),
        ("replacing", "committed"),
        ("prepared", "replacing", "committed"),
    )

    uninterrupted = write_achieved(tmp_path, project=tmp_path / "uninterrupted")
    _install_durable_effect_projection(uninterrupted)
    expected = _terminal_projection(
        uninterrupted,
        export_mod.publish_achieved(uninterrupted, CHANGE_ID),
    )

    for index, schedule in enumerate(_ORDERED_CRASH_SUBSETS):
        project = write_achieved(tmp_path, project=tmp_path / f"schedule-{index}")
        _install_durable_effect_projection(project)
        pending = list(schedule)

        def crash_at_next_selected_phase(phase: str) -> None:
            if pending and phase == pending[0]:
                expected_phase = pending.pop(0)
                raise _InjectedPublishCrash(expected_phase)

        monkeypatch.setattr(export_mod, "_journal_cut", crash_at_next_selected_phase)
        for phase in schedule:
            with pytest.raises(_InjectedPublishCrash, match=phase):
                export_mod.publish_achieved(project, CHANGE_ID)
        assert pending == []

        monkeypatch.setattr(export_mod, "_journal_cut", lambda _phase: None)
        recovered = export_mod.publish_achieved(project, CHANGE_ID)
        assert _terminal_projection(project, recovered) == expected, schedule


def test_modular_resume_against_legacy_lock_leaves_ledger_bytes_unchanged(
    tmp_path: Path, installed_sources
) -> None:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from graph_engine.application import AssuranceApplication, FixedExecutionFactory, RevisionMismatch
    from graph_engine.boot.graph_revision import BootArtifact, GraphBuildManifest, GraphRevision
    from graph_engine.canonical import canonical_digest
    from graph_engine.persistence.runner_lease import LocalInvocationRunnerLease
    from tests.product.product_runner import adapter_product_composition, modular_product_composition

    legacy = adapter_product_composition(installed_sources, "cursor")
    modular = modular_product_composition(installed_sources)
    assert legacy.lock_digest != modular.lock_digest

    class _State(TypedDict, total=False):
        value: str

    def execute(state: _State) -> _State:
        return state

    builder = StateGraph(_State)
    builder.add_node("execute", execute)
    builder.add_edge(START, "execute")
    builder.add_edge("execute", END)
    graph = builder.compile(checkpointer=InMemorySaver())

    def _artifact(lock: str) -> BootArtifact:
        revision = GraphRevision.build(
            product_lock_digest=lock,
            wheel_source_digests={"assurance.product": "d" * 64},
            factory_symbols=("assurance_product.graphs.factory:build_product_graphs",),
            state_schema_versions={"intake": "1"},
            langgraph_version="1.2.11",
            checkpoint_contract_version="1",
        )
        return BootArtifact(
            manifest=GraphBuildManifest(
                revision=revision,
                entrypoint_contract_digests={"intake": canonical_digest({"entrypoint": "intake"})},
                attempt_contract_digests={},
            ),
            entrypoints={"intake": graph},
            attempt_contracts={},
            checkpointer_backend_id="memory",
        )

    original = _artifact(legacy.lock_digest)
    drifted = _artifact(modular.lock_digest)
    lease_root = tmp_path / "replay-legacy-lock"
    lease_root.mkdir()
    application = AssuranceApplication(lease=LocalInvocationRunnerLease(lease_root), owner_id="runner-a")

    def _factory(artifact: BootArtifact) -> FixedExecutionFactory:
        return FixedExecutionFactory(
            artifact=artifact,
            attempt_kernel=cast(Any, object()),
            secret_resolver=object(),
            workspace_provider=object(),
        )

    async def _run() -> None:
        await application.start(
            invocation_id="inv-replay-legacy-001",
            entrypoint="intake",
            graph_input={"change_id": "CH-1"},
            execution_factory=_factory(original),
        )

        def _durable() -> tuple[tuple[Path, bytes], ...]:
            return tuple(
                sorted(
                    (path, path.read_bytes())
                    for path in lease_root.rglob("*")
                    if path.is_file() and path.name != "runner.json"
                )
            )

        before = _durable()
        with pytest.raises(RevisionMismatch):
            await application.run(
                invocation_id="inv-replay-legacy-001",
                execution_factory=_factory(drifted),
            )
        after = _durable()
        assert after == before

    import asyncio

    asyncio.run(_run())
