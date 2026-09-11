from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from pydantic import BaseModel

from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.invocation_identity import InvocationIdentityRecord
from graph_engine.attempts.keys import AttemptKey
from graph_engine.composition import FrozenComposition
from graph_engine.plugin_api import ResourceClaimTemplate, ResourceClaims
from tests.product.cli_support import (
    SECRET_ENV,
    SECRET_HANDLE,
    SECRET_VALUE,
    common_lifecycle_args,
    parse_json_output,
)

pytestmark = pytest.mark.usefixtures("installed_sources")

_NON_AGENT_ENTRYPOINTS = frozenset(
    {
        "improvement-apply",
        "improvement-evaluate",
        "improvement-export",
        "improvement-rollback",
    }
)
_AGENT_ENTRYPOINTS = frozenset(
    {
        "archive",
        "case",
        "execute",
        "full",
        "improvement-review",
        "intake",
        "issue-analyze",
        "issue-reconcile",
        "issue-review",
        "retro",
    }
)


def _identity_path(project_dir: Path, change_id: str, invocation_id: str) -> Path:
    return (
        project_dir
        / "qa" / ".runtime"
        / "langgraph"
        / "identities"
        / f"{invocation_id}.json"
    )


def _load_identity(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("identity record must be an object")
    return payload


def test_all_fourteen_public_entrypoints_are_current() -> None:
    from assurance_product.application import ENTRYPOINT_AGENT_CONTRACT_IDS
    from assurance_product import models
    from assurance_product.models import PRODUCT_ENTRYPOINTS

    assert not hasattr(models, "ENTRYPOINT_RUNTIME_CUTOVER")
    assert set(ENTRYPOINT_AGENT_CONTRACT_IDS) == set(PRODUCT_ENTRYPOINTS)
    assert len(PRODUCT_ENTRYPOINTS) == 14
    assert set(PRODUCT_ENTRYPOINTS) == _NON_AGENT_ENTRYPOINTS | _AGENT_ENTRYPOINTS
    assert all(ENTRYPOINT_AGENT_CONTRACT_IDS[name] == () for name in _NON_AGENT_ENTRYPOINTS)
    assert all(ENTRYPOINT_AGENT_CONTRACT_IDS[name] for name in _AGENT_ENTRYPOINTS)


def _existing_lifecycle_args(
    args: list[str], project_dir: Path, change_id: str, invocation_id: str
) -> list[str]:
    return [
        "--json",
        "--project-dir",
        str(project_dir),
        "--change",
        change_id,
        "--invocation-id",
        invocation_id,
        "--product",
        args[args.index("--product") + 1],
        "--binding-dist",
        args[args.index("--binding-dist") + 1],
        "--binding-entrypoint",
        "deployment",
        "--binding-declaration",
        args[args.index("--binding-declaration") + 1],
        "--config-tree",
        args[args.index("--config-tree") + 1],
        "--secret",
        args[args.index("--secret") + 1],
    ]


def test_leftover_invocation_without_identity_fails_closed(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.cli import app

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "README.md").write_text("seed\n", encoding="utf-8")
    change_id = "CH-LEFTOVER-001"
    ChangeWorkspace.prepare(project_dir, change_id)
    leftover = project_dir / "qa" / ".runtime" / "invocations"
    leftover.mkdir(parents=True, exist_ok=True)
    (leftover / "inv-pre-migration-001").mkdir()
    path = _identity_path(project_dir, change_id, "inv-pre-migration-001")
    assert not path.exists()
    result = cli_runner.invoke(
        app,
        [
            "status",
            "--json",
            "--project-dir",
            str(project_dir),
            "--change",
            change_id,
            "--invocation-id",
            "inv-pre-migration-001",
            "--product",
            "assurance-opencode",
            "--binding-dist",
            installed_sources.deployments["opencode"].distribution,
            "--binding-entrypoint",
            "deployment",
            "--binding-declaration",
            installed_sources.deployments["opencode"].declaration_path,
            "--config-tree",
            str(installed_sources.configuration_tree.path),
            "--secret",
            f"opencode.token=env:{SECRET_ENV}",
        ],
    )
    assert result.exit_code == 40, result.output
    assert not path.exists()
    assert leftover.is_dir()


_EVALUATE_WAKE = "wake-evaluate-1"
_EVALUATE_INVOCATION = "inv-evaluate-reopen-001"


def _evaluate_task_payload() -> dict[str, object]:
    from assurance_improvement.contracts.improvements import ImprovementProjection

    projection = ImprovementProjection.model_validate(
        {
            "improvement_id": "IMP-1",
            "fingerprint": "f" * 64,
            "kind": "prompt_improvement",
            "delivery": "memory_patch",
            "source_refs": {"problem_ids": ["PROB-1"], "occurrence_ids": ["OCC-1"]},
            "target": ".aa/memory/aa-api-plan.md",
            "rationale": "gap",
            "proposed_change": "register adapters",
            "verification": {"suites": [], "required_cases": [], "success_criteria": "review"},
            "risk": "low",
            "confidence": "high",
            "state": "approved",
            "version": 1,
            "proposed_by_retro_ids": ["RET-1"],
            "last_event_id": "IMPEVT-1",
            "approval_source": "automatic",
            "last_auto_review": {
                "review_id": "REV-1",
                "subject_sha256": f"sha256:{'a' * 64}",
                "assessment_sha256": f"sha256:{'a' * 64}",
                "policy_version": "1",
                "verdict": "auto_approved",
            },
        }
    )
    return {
        "projection": projection.model_dump(mode="json"),
        "eval_run_id": "eval-1",
        "outcome": "passed",
        "report_sha256": "r",
        "staged_sha256": "s",
        "baseline_sha256": None,
        "target_digest": "a" * 64,
    }


def _durable_chain(
    *,
    project_dir: Path,
    change_id: str,
    invocation_id: str,
    composition: FrozenComposition,
    identity: InvocationIdentityRecord,
    expect_attempt_records: bool,
    workspace: ChangeWorkspace | None = None,
) -> object:
    from assurance_product.cli import _authorization
    from assurance_product.runtime_ports import ProductRuntimePorts

    async def _read() -> object:
        opened = workspace or ChangeWorkspace.open(project_dir.resolve(), change_id)
        authorization = _authorization([f"{SECRET_HANDLE}=env:{SECRET_ENV}"])
        async with ProductRuntimePorts.open(
            opened,
            composition,
            invocation=invocation_id,
            authorization=authorization,
        ) as ports:
            started = await ports.backend.journal.read_invocation_started(invocation_id)
            assert started is not None
            assert started.invocation_id == identity.invocation_id
            assert started.product_lock_digest == identity.product_lock_digest
            assert started.root_input_digest == identity.root_input_digest
            assert started.graph_revision == identity.revision_id
            assert started.fencing_token >= 1
            artifact = ports._compile_bound(
                invocation_id=invocation_id,
                root_input_digest=identity.root_input_digest,
                fencing_token=started.fencing_token,
            )
            snapshot = await artifact.entrypoints["improvement-evaluate"].aget_state(
                {
                    "configurable": {
                        "thread_id": invocation_id,
                        "assurance_revision_id": artifact.manifest.revision.revision_id,
                        "assurance_product_lock_digest": artifact.manifest.revision.product_lock_digest,
                        "assurance_root_input_digest": identity.root_input_digest,
                        "assurance_fencing_token": started.fencing_token,
                        "assurance_initial_checkpoint": False,
                    }
                }
            )
            configurable = dict(getattr(snapshot, "config", {}) or {}).get("configurable") or {}
            checkpoint_id = configurable.get("checkpoint_id")
            assert isinstance(checkpoint_id, str) and checkpoint_id
            anchor = await ports.backend.journal.read_checkpoint_anchor(invocation_id, checkpoint_id)
            assert anchor is not None
            assert anchor.product_lock_digest == identity.product_lock_digest
            assert anchor.graph_revision == identity.revision_id
            assert anchor.root_input_digest == identity.root_input_digest
            assert started.fencing_token >= anchor.fencing_token >= 1
            records = await ports.attempt_journal.read_records()
            if expect_attempt_records:
                assert records
                assert any(
                    getattr(event, "receipt_digest", None) or getattr(event, "envelope_digest", None)
                    for record in records
                    for event in record.events
                )
            assert ports.network.allow_opencode is False
            return started

    return asyncio.run(_read())


def _reject_lifecycle_tampers(
    *,
    cli_runner,
    app: object,
    existing: list[str],
    identity_path: Path,
    identity_bytes: bytes,
    project_dir: Path,
    change_id: str,
    monkeypatch,
) -> None:
    escaped = identity_path.with_name(f"{identity_path.name}.outside")
    escaped.write_bytes(identity_bytes)
    identity_path.unlink()
    identity_path.symlink_to(escaped)
    linked = cli_runner.invoke(app, ["run", *existing])
    assert linked.exit_code == 40, linked.output
    identity_path.unlink()
    identity_path.write_bytes(identity_bytes)
    escaped.unlink()

    identity_path.chmod(0o000)
    modest = cli_runner.invoke(app, ["run", *existing])
    assert modest.exit_code == 40, modest.output
    identity_path.chmod(0o644)

    checkpoints = (
        project_dir / "qa" / ".runtime" / "langgraph" / "checkpoints.sqlite3"
    )
    if checkpoints.is_file():
        real = checkpoints.with_name("checkpoints.sqlite3.real")
        checkpoints.rename(real)
        checkpoints.symlink_to(real)
        escaped_db = cli_runner.invoke(app, ["run", *existing])
        assert escaped_db.exit_code == 40, escaped_db.output
        checkpoints.unlink()
        real.rename(checkpoints)

    escaped_root = project_dir.parent / "escaped-project"
    if escaped_root.exists() or escaped_root.is_symlink():
        escaped_root.unlink()
    escaped_root.symlink_to(project_dir.resolve())
    escaped_args = list(existing)
    escaped_args[escaped_args.index("--project-dir") + 1] = str(escaped_root)
    escaped_project = cli_runner.invoke(app, ["run", *escaped_args])
    assert escaped_project.exit_code == 40, escaped_project.output
    escaped_root.unlink()

    from assurance_product import runtime_ports as ports_mod
    from assurance_product.runtime_ports import NetworkPolicy

    original_preflight = ports_mod._preflight_selected_root
    monkeypatch.setattr(
        ports_mod, "_preflight_selected_root", lambda *_a, **_k: NetworkPolicy(allow_opencode=True)
    )
    mutated_network = cli_runner.invoke(app, ["run", *existing])
    assert mutated_network.exit_code == 40, mutated_network.output
    monkeypatch.setattr(ports_mod, "_preflight_selected_root", original_preflight)


def _authenticate_reopen(
    *,
    cli_runner,
    app: object,
    existing: list[str],
    identity: InvocationIdentityRecord,
    composition: FrozenComposition,
    expected_status: str | None = None,
) -> dict[str, Any]:

    statused = cli_runner.invoke(app, ["status", *existing])
    assert statused.exit_code == 0, statused.output
    status_doc = parse_json_output(statused.stdout)
    locked = cli_runner.invoke(app, ["lock", "show", *existing])
    assert locked.exit_code == 0, locked.output
    lock_doc = parse_json_output(locked.stdout)
    reopened = InvocationIdentityRecord.model_validate_json(
        _identity_path(
            Path(existing[existing.index("--project-dir") + 1]),
            str(existing[existing.index("--change") + 1]),
            str(existing[existing.index("--invocation-id") + 1]),
        ).read_bytes()
    )
    assert reopened.model_dump(mode="json") == identity.model_dump(mode="json")
    assert status_doc["invocation_id"] == reopened.invocation_id
    assert status_doc["lock_digest"] == reopened.product_lock_digest
    assert status_doc["root_input_digest"] == reopened.root_input_digest
    assert status_doc["entrypoint"] == "improvement-evaluate"
    assert lock_doc["lock_digest"] == reopened.product_lock_digest
    assert lock_doc["lock"]["schema_version"] == "3"
    assert lock_doc["revision"]["revision_id"] == reopened.revision_id
    assert lock_doc["revision"]["product_lock_digest"] == reopened.product_lock_digest
    assert lock_doc["lock"]["digest"] == composition.lock_digest
    assert "compiled_workflow" not in lock_doc["lock"]
    if expected_status is not None:
        assert status_doc["status"] == expected_status
    project_dir = Path(existing[existing.index("--project-dir") + 1])
    change_id = str(existing[existing.index("--change") + 1])
    invocation_id = str(existing[existing.index("--invocation-id") + 1])
    _durable_chain(
        project_dir=project_dir,
        change_id=change_id,
        invocation_id=invocation_id,
        composition=composition,
        identity=identity,
        expect_attempt_records=expected_status == "completed",
    )
    return status_doc


def _inject_evaluate_payload(
    *,
    project_dir: Path,
    change_id: str,
    invocation_id: str,
    composition: FrozenComposition,
    identity: InvocationIdentityRecord,
) -> None:
    from assurance_product.cli import _authorization
    from assurance_product.runtime_ports import ProductRuntimePorts

    async def _update() -> None:
        workspace = ChangeWorkspace.open(project_dir.resolve(), change_id)
        authorization = _authorization([f"{SECRET_HANDLE}=env:{SECRET_ENV}"])
        async with ProductRuntimePorts.open(
            workspace,
            composition,
            invocation=invocation_id,
            authorization=authorization,
        ) as ports:
            started = await ports.backend.journal.read_invocation_started(invocation_id)
            assert started is not None
            assert started.product_lock_digest == identity.product_lock_digest
            assert started.root_input_digest == identity.root_input_digest
            assert started.graph_revision == identity.revision_id
            assert started.fencing_token >= 1
            artifact = ports._compile_bound(
                invocation_id=invocation_id,
                root_input_digest=identity.root_input_digest,
                fencing_token=started.fencing_token,
            )
            graph = artifact.entrypoints["improvement-evaluate"]
            await graph.aupdate_state(
                {
                    "configurable": {
                        "thread_id": invocation_id,
                        "assurance_revision_id": artifact.manifest.revision.revision_id,
                        "assurance_product_lock_digest": artifact.manifest.revision.product_lock_digest,
                        "assurance_root_input_digest": identity.root_input_digest,
                        "assurance_fencing_token": started.fencing_token,
                        "assurance_initial_checkpoint": False,
                    }
                },
                _evaluate_task_payload(),
                as_node="validate",
            )

    asyncio.run(_update())


def test_non_agent_root_survives_reopen_status_lock_resume_and_publication(
    cli_runner, installed_sources, opencode_composition, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.invocation_identity import InvocationIdentityRecord
    from graph_engine.attempts.resolutions import PendingTaskResult, SystemReference
    from graph_engine.attempts.resource_arbiter import ResourceArbiter
    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = opencode_composition
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id=_EVALUATE_INVOCATION,
        entrypoint="improvement-evaluate",
        change_id="CH-PUB-001",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    assert SECRET_VALUE not in started.output
    started_doc = parse_json_output(started.stdout)
    runtime = project_dir / "qa" / ".runtime"
    for path in runtime.rglob("*"):
        if path.is_file():
            assert SECRET_VALUE.encode() not in path.read_bytes()
    identity_path = _identity_path(project_dir, change_id, _EVALUATE_INVOCATION)
    identity_bytes = identity_path.read_bytes()
    identity = InvocationIdentityRecord.model_validate_json(identity_bytes)
    assert identity.phase == "initialized"
    assert identity.entrypoint == "improvement-evaluate"
    assert "runtime" not in identity.model_dump(mode="json")
    assert started_doc["lock_digest"] == composition.lock_digest == identity.product_lock_digest
    assert started_doc["root_input_digest"] == identity.root_input_digest

    existing = _existing_lifecycle_args(args, project_dir, change_id, _EVALUATE_INVOCATION)
    premature_resume = tmp_path / "premature-wakeup.json"
    premature_resume.write_text(
        json.dumps({"wakeup": {"reference_id": "wake-1"}}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    premature = cli_runner.invoke(app, ["resume", *existing, "--resume-file", str(premature_resume)])
    assert premature.exit_code == 40, premature.output
    tampered = json.loads(identity_bytes.decode("utf-8"))
    tampered["product_lock_digest"] = "b" * 64
    identity_path.write_text(json.dumps(tampered, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    rejected = cli_runner.invoke(app, ["run", *existing])
    assert rejected.exit_code == 40, rejected.output
    identity_path.write_bytes(identity_bytes)
    _reject_lifecycle_tampers(
        cli_runner=cli_runner,
        app=app,
        existing=existing,
        identity_path=identity_path,
        identity_bytes=identity_bytes,
        project_dir=project_dir,
        change_id=change_id,
        monkeypatch=monkeypatch,
    )
    _durable_chain(
        project_dir=project_dir,
        change_id=change_id,
        invocation_id=_EVALUATE_INVOCATION,
        composition=composition,
        identity=identity,
        expect_attempt_records=False,
    )

    _inject_evaluate_payload(
        project_dir=project_dir,
        change_id=change_id,
        invocation_id=_EVALUATE_INVOCATION,
        composition=composition,
        identity=identity,
    )
    from types import MappingProxyType
    from assurance_product import application as application_mod

    original_reachable = application_mod.ENTRYPOINT_AGENT_CONTRACT_IDS
    forced_reachable = dict(original_reachable)
    forced_reachable["improvement-evaluate"] = ("assurance.intake.agent.intake.v1",)
    monkeypatch.setattr(
        application_mod,
        "ENTRYPOINT_AGENT_CONTRACT_IDS",
        MappingProxyType(forced_reachable),
    )
    secret_args = list(existing)
    secret_args[secret_args.index("--secret") + 1] = "not-opencode.token=env:AA_OPENCODE_TOKEN"
    denied_secret = cli_runner.invoke(app, ["run", *secret_args])
    assert denied_secret.exit_code == 40, denied_secret.output
    monkeypatch.setattr(application_mod, "ENTRYPOINT_AGENT_CONTRACT_IDS", original_reachable)

    original_acquire = ResourceArbiter.acquire
    issued = {"pending": False}

    async def interrupt_once(
        self: ResourceArbiter,
        attempt_key: AttemptKey,
        claims: ResourceClaims | ResourceClaimTemplate,
        *,
        fencing_token: int,
        validated_input: BaseModel | None = None,
    ) -> object:
        if not issued["pending"]:
            issued["pending"] = True
            return PendingTaskResult(wakeup=SystemReference(reference_id=_EVALUATE_WAKE))
        return await original_acquire(
            self,
            attempt_key,
            claims,
            fencing_token=fencing_token,
            validated_input=validated_input,
        )

    monkeypatch.setattr(ResourceArbiter, "acquire", interrupt_once)
    interrupted = cli_runner.invoke(app, ["run", *existing])
    assert interrupted.exit_code in {20, 30}, interrupted.output
    interrupted_doc = parse_json_output(interrupted.stdout)
    assert interrupted_doc["status"] in {"blocked", "interrupted"}
    pending = _authenticate_reopen(
        cli_runner=cli_runner,
        app=app,
        existing=existing,
        identity=identity,
        composition=composition,
        expected_status=str(interrupted_doc["status"]),
    )
    assert pending["pending_interrupt"] is not None
    assert pending["change"]["state"] in {"blocked", "interrupted"}

    monkeypatch.setattr(ResourceArbiter, "acquire", original_acquire)
    resume_file = tmp_path / "system-wakeup.json"
    resume_file.write_text(
        json.dumps({"wakeup": {"reference_id": _EVALUATE_WAKE}}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    resumed = cli_runner.invoke(app, ["resume", *existing, "--resume-file", str(resume_file)])
    assert resumed.exit_code == 0, resumed.output
    resumed_doc = parse_json_output(resumed.stdout)
    assert resumed_doc["status"] == "completed"
    achieved = _authenticate_reopen(
        cli_runner=cli_runner,
        app=app,
        existing=existing,
        identity=identity,
        composition=composition,
        expected_status="completed",
    )
    assert achieved["change"]["state"] == "achieved"
    assert achieved["pending_interrupt"] is None
    assert achieved["entrypoint"] == "improvement-evaluate"
    _durable_chain(
        project_dir=project_dir,
        change_id=change_id,
        invocation_id=_EVALUATE_INVOCATION,
        composition=composition,
        identity=identity,
        expect_attempt_records=True,
    )
