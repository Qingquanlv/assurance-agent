from __future__ import annotations

import json
import multiprocessing
import asyncio
import os
import sqlite3
from copy import deepcopy

import pytest

from tests.product.cli_support import lifecycle_authorization, write_product_input, write_project_dir

pytestmark = pytest.mark.usefixtures("installed_sources")


@pytest.mark.parametrize("entry", ["application", "cli", "application-resume", "cli-resume"])
def test_foreground_dead_owner_reconstructs_authenticated_cancel(
    tmp_path, opencode_composition, installed_sources, monkeypatch, entry
):
    from assurance_product import runtime_ports
    from assurance_product.application import AssuranceProductApplication
    from assurance_product.cli import _run_invocation, _resume_invocation, app
    from click.testing import CliRunner
    from assurance_product.product import prepare_change_workspace
    from assurance_product.worker_lifecycle import acquire_execution, control_root, ExecutionConflict
    from graph_engine.attempts.host_protocol import TaskHostCallResult
    from graph_engine.plugin_api import TaskOutcome, TaskActivityCancelResult
    from agent_runtime_fixture.contracts import frozen_run_request

    project = write_project_dir(tmp_path / "project")
    change = "CH-FOREGROUND"
    invocation = "inv-distinct"
    workspace = prepare_change_workspace(project, change)
    input_path = write_product_input(tmp_path / "input.json", opencode_composition, change_id=change)
    cancellations = []
    cancel_status = "acknowledged"
    resume_path = None
    if entry.endswith("resume"):
        pause = tmp_path / "pause"
        pause.touch()
        application = AssuranceProductApplication()
        application.run(
            project_dir=project,
            workspace=workspace,
            change_id=change,
            invocation_id=invocation,
            composition=opencode_composition,
            authorization=lifecycle_authorization(),
            entrypoint="issue-analyze",
            input_path=input_path,
            secrets=(),
            stop_file=pause,
        )
        from assurance_product.application import _graph_snapshot
        from assurance_product.runtime_ports import ProductRuntimePorts
        from assurance_product.invocation_identity import load_identity

        async def pending():
            async with ProductRuntimePorts.open(
                workspace, opencode_composition, invocation, authorization=lifecycle_authorization()
            ) as ports:
                record = load_identity(workspace, invocation)
                assert record is not None
                bound = await ports.read_only_execution(
                    invocation_id=invocation, root_input_digest=record.root_input_digest
                )
                return getattr(
                    await _graph_snapshot(bound.artifact, record.entrypoint, invocation), "interrupts"
                )

        interrupts = asyncio.run(pending())
        assert interrupts
        resume_path = tmp_path / "resume.json"
        resume_path.write_text(
            json.dumps({"interrupts": {item.id: {"wakeup": item.value["wakeup"]} for item in interrupts}})
        )
        pause.unlink()

    class Host:
        async def execute(self, call):
            if call.identity.phase == "runtime":
                os._exit(17)
            if call.identity.phase == "prepare":
                return TaskHostCallResult(
                    operation="execute",
                    outcome=TaskOutcome.succeeded(frozen_run_request().model_dump(mode="json")),
                )
            raise AssertionError("unexpected execution phase")

        def read_terminal_receipts(self, identity):
            return ()

        async def cancel(self, call):
            cancellations.append(call)
            return TaskHostCallResult(
                operation="cancel",
                cancel_result=TaskActivityCancelResult.model_validate(
                    {
                        "status": "terminal",
                        "outcome": TaskOutcome.stopped("cancelled").model_dump(mode="json"),
                    }
                    if cancel_status == "terminal"
                    else (
                        {"status": "indeterminate", "reason": "pending proof"}
                        if cancel_status == "indeterminate"
                        else {"status": "acknowledged"}
                    )
                ),
            )

    monkeypatch.setattr(runtime_ports, "create_production_task_execution_host", lambda **kwargs: Host())

    def run():
        if entry == "application-resume":
            AssuranceProductApplication().resume(
                workspace=workspace,
                composition=opencode_composition,
                authorization=lifecycle_authorization(),
                invocation_id=invocation,
                action=None,
                reason=None,
                resume_file=resume_path,
            )
        elif entry == "cli-resume":
            source = installed_sources.deployments["opencode"]
            _resume_invocation(
                project_dir=project,
                change_id=change,
                invocation_id=invocation,
                product="assurance-opencode",
                binding_dist=source.distribution,
                binding_entrypoint="deployment",
                binding_declaration=source.declaration_path,
                config_tree=str(installed_sources.configuration_tree.path),
                secrets=("opencode.token=env:AA_NEXT_OPENCODE_TOKEN",),
                action=None,
                reason=None,
                resume_file=resume_path,
            )
        elif entry == "application":
            AssuranceProductApplication().run(
                project_dir=project,
                workspace=workspace,
                change_id=change,
                invocation_id=invocation,
                composition=opencode_composition,
                authorization=lifecycle_authorization(),
                entrypoint="issue-analyze",
                input_path=input_path,
                secrets=(),
            )
        else:
            source = installed_sources.deployments["opencode"]
            _run_invocation(
                project_dir=project,
                change_id=change,
                invocation_id=invocation,
                product="assurance-opencode",
                binding_dist=source.distribution,
                binding_entrypoint="deployment",
                binding_declaration=source.declaration_path,
                config_tree=str(installed_sources.configuration_tree.path),
                entrypoint="issue-analyze",
                input_path=input_path,
                secrets=("opencode.token=env:AA_NEXT_OPENCODE_TOKEN",),
                reuse_directory=True,
            )

    process = multiprocessing.get_context("fork").Process(target=run)
    process.start()
    process.join(30)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("foreground did not reach retained runtime dispatch")
    assert process.exitcode == 17
    owner = json.loads((control_root(project) / "owner.json").read_text())
    assert owner["calls"]
    with pytest.raises(ExecutionConflict):
        with acquire_execution(project, "replacement"):
            pass
    owner_path = control_root(project) / "owner.json"
    assert owner["stop_authority"]["change_id"] == change
    assert "runtime-only-token" not in owner_path.read_text()
    for mutation in ("missing", "secret", "source", "revision", "change") if entry == "application" else ():
        changed = deepcopy(owner)
        if mutation == "missing":
            changed.pop("stop_authority")
        elif mutation == "secret":
            from graph_engine.attempts.secret_sources import SecretSourceBinding, runtime_authorization_digest

            source = changed["stop_authority"]["authorization"]["secret_sources"][0]
            source["source_locator"] = "AA_CHANGED_TOKEN"
            changed["stop_authority"]["authorization"]["digest"] = runtime_authorization_digest(
                (SecretSourceBinding(**source),)
            )
        elif mutation == "source":
            changed["stop_authority"]["binding_declaration"] = "missing.json"
        elif mutation == "revision":
            changed["stop_authority"]["graph_revision"] = "0" * 64
        else:
            changed["stop_authority"]["change_id"] = "CH-OTHER"
        owner_path.write_text(json.dumps(changed))
        assert (
            AssuranceProductApplication().stop(project_dir=project, invocation_id=invocation, force=True)
            == "unconfirmed"
        )
        assert cancellations == []
    owner_path.write_text(json.dumps(owner))
    assert (
        AssuranceProductApplication().stop(project_dir=project, invocation_id=invocation, force=True)
        == "unconfirmed"
    )
    with pytest.raises(ExecutionConflict):
        with acquire_execution(project, "replacement"):
            pass
    db = workspace.paths.langgraph_checkpoints
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT abandoned FROM assurance_attempt_generations WHERE owner_nonce = ?", (owner["nonce"],)
        ).fetchall() == [(0,)]
        conn.execute(
            "INSERT INTO assurance_attempt_generations (scope_digest, scope, attempt_key_digest, ordinal, owner_nonce, input_payload) SELECT 'foreign', scope, ?, ordinal, 'foreign', input_payload FROM assurance_attempt_generations LIMIT 1",
            ("f" * 64,),
        )
    from graph_engine.persistence.resource_authorization import (
        ResourceAuthorizationRecord,
        RESOURCE_AUTHORIZATION_SCHEMA_VERSION,
    )
    from graph_engine.plugin_api import ResourceClaims
    from graph_engine.canonical import canonical_json_bytes
    from assurance_product.bootstrap.resources import _active_grants
    from graph_engine.persistence.resource_authorization import decode_resource_authorization_record

    def resource_records(conn):
        return [
            decode_resource_authorization_record(
                json.loads(bytes(payload)), schema_version=schema, record_digest=digest
            )
            for schema, digest, payload in conn.execute(
                "SELECT schema_version, record_digest, payload FROM assurance_resource_authorizations ORDER BY revision"
            )
        ]

    with sqlite3.connect(db) as conn:
        records = resource_records(conn)
        assert _active_grants(records)
        foreign = ResourceAuthorizationRecord.build(
            revision=len(records),
            action="acquire",
            authorization_id="f" * 64,
            attempt_key_digest="f" * 64,
            fencing_token=1,
            claims=ResourceClaims(writes=("qa/foreign.txt",)),
        )
        conn.execute(
            "INSERT INTO assurance_resource_authorizations VALUES (?, ?, ?, ?, ?)",
            (
                foreign.revision,
                RESOURCE_AUTHORIZATION_SCHEMA_VERSION,
                foreign.fencing_token,
                foreign.record_digest,
                canonical_json_bytes(foreign.canonical_projection()),
            ),
        )
    cancel_status = "indeterminate"
    assert (
        AssuranceProductApplication().stop(project_dir=project, invocation_id=invocation, force=True)
        == "unconfirmed"
    )
    with pytest.raises(ExecutionConflict):
        with acquire_execution(project, "replacement"):
            pass
    cancel_status = "terminal"
    if entry.startswith("cli"):
        result = CliRunner().invoke(
            app, ["stop", "--project-dir", str(project), "--invocation-id", invocation, "--force"]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["status"] == "stopped"
    else:
        assert (
            AssuranceProductApplication().stop(project_dir=project, invocation_id=invocation, force=True)
            == "stopped"
        )
    assert len(cancellations) == 3
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT abandoned FROM assurance_attempt_generations WHERE owner_nonce = ?", (owner["nonce"],)
        ).fetchall() == [(1,)]
        assert conn.execute(
            "SELECT abandoned FROM assurance_attempt_generations WHERE owner_nonce = 'foreign'"
        ).fetchall() == [(0,)]
        remaining = _active_grants(resource_records(conn))
        assert [record.attempt_key_digest for record in remaining] == ["f" * 64]
    assert cancellations[0].identity.invocation_id == invocation
    with acquire_execution(project, "replacement"):
        pass


def test_read_only_ports_preserve_no_owner_and_upgrade_old_host_call_table(tmp_path, opencode_composition):
    from assurance_product.product import prepare_change_workspace
    from assurance_product.runtime_ports import ProductRuntimePorts
    from assurance_product.worker_lifecycle import current_owner, control_root

    project = write_project_dir(tmp_path / "project")
    workspace = prepare_change_workspace(project, "CH-READ-ONLY")
    workspace.paths.langgraph_checkpoints.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(workspace.paths.langgraph_checkpoints) as conn:
        conn.execute(
            "CREATE TABLE assurance_host_calls (call_digest TEXT PRIMARY KEY, owner_nonce TEXT NOT NULL, attempt_key_digest TEXT NOT NULL, payload BLOB NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute("INSERT INTO assurance_host_calls VALUES ('old', 'old', 'old', ?, 0)", (b"{}",))

    async def read():
        assert current_owner() is None
        async with ProductRuntimePorts.open(
            workspace, opencode_composition, "inv-read", authorization=lifecycle_authorization()
        ):
            assert current_owner() is None

    asyncio.run(read())
    assert not (control_root(project) / "owner.json").exists()
    with sqlite3.connect(workspace.paths.langgraph_checkpoints) as conn:
        assert conn.execute(
            "SELECT call_digest, stop_authority_digest FROM assurance_host_calls"
        ).fetchall() == [("old", None)]
