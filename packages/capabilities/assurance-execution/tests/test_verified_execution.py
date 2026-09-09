from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Literal, cast

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_execution.contracts.verification import (
    VerificationEvidenceV1,
    VerifiedExecutionResultV1,
)
from assurance_execution.contracts.workflow import (
    VerifiedBridgeDefectResultV1,
    VerifiedExecutionCycleResultV1,
)
from assurance_execution.graphs.nodes import publish_execution

import httpx
import pytest

from assurance_execution.operations.verified_execution import ActionJournal, execute_frozen_action
from assurance_execution.operations.verification_manifest import build_verification_manifest
from assurance_generation.contracts.execution_plan import CasePlanContextV1
from assurance_generation.operations.execution_plan import compile_case_plan
from assurance_intake.contracts.verification import AssertionSourcesV1
from graph_engine.attempts import AttemptKey, BusinessActivation
from tests.verification_support import read_fixture

REPO = Path(__file__).resolve().parents[4]
_ACTIVITY_OWNERS: list[Any] = []


def _verified_dispatch_result(
    *, completion_status: Literal["collected", "incomplete"] = "incomplete"
) -> VerifiedExecutionResultV1:
    plan = formal_plan()
    execution_id = "12345678-1234-4123-8123-123456789abc"
    evidence_ref = EvidenceArtifactRefV1(
        path=f"qa/changes/{plan.change_id}/execution/{execution_id}/outcome.json",
        digest="8" * 64,
    )
    manifest_ref = EvidenceArtifactRefV1(
        path=f"qa/changes/{plan.change_id}/.staging/execution/manifest.json",
        digest="7" * 64,
    )
    process_ref = EvidenceArtifactRefV1(
        path=f"qa/changes/{plan.change_id}/execution/{execution_id}/process_terminal.json",
        digest="6" * 64,
    )
    evidence = VerificationEvidenceV1.model_validate(
        {
            "execution_id": execution_id,
            "manifest_digest": "7" * 64,
            "receipt_ref": process_ref.model_dump(mode="json"),
            "observations": [],
            "host_completion": {"state": "error", "reason": "bridge_not_called"},
            "collector_completion": {"state": "not_required"},
            "state": completion_status,
        }
    )
    return VerifiedExecutionResultV1(
        validation_profile="api_db.v1",
        change_id=plan.change_id,
        case_id=plan.case_id,
        reviewed_case=plan.reviewed_case,
        coverage_epoch=plan.coverage_epoch,
        repair_round=3,
        plan_digest=plan.plan_digest,
        plan_ref=plan.plan_ref,
        case_execution_plan_ref=EvidenceArtifactRefV1(
            path=f"qa/changes/{plan.change_id}/plans/api-case-execution-plan.json",
            digest="5" * 64,
        ),
        case_execution_plan_digest="5" * 64,
        spec_digest=plan.spec_digest,
        execution_id=execution_id,
        attempt_key=AttemptKey(digest="4" * 64),
        batch_id="verified-batch",
        mapping_digest="3" * 64,
        manifest_ref=manifest_ref,
        evidence_ref=evidence_ref,
        execution_authority_ref=EvidenceArtifactRefV1(
            path=f"qa/changes/{plan.change_id}/execution/{execution_id}/execution_terminal.json",
            digest="9" * 64,
        ),
        raw_evidence_refs=(process_ref,),
        executed_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
        completion_status=completion_status,
        evidence=evidence,
    )


def test_verified_publish_emits_a_separate_cycle_with_kernel_receipt() -> None:
    output = _verified_dispatch_result()
    generation = {
        "change_id": output.change_id,
        "coverage_epoch": output.coverage_epoch,
        "reviewed_case": output.reviewed_case.model_dump(mode="json"),
        "plan_digest": output.plan_digest,
        "plan_ref": output.plan_ref.model_dump(mode="json"),
        "mapping_ref": {
            "path": f"qa/changes/{output.change_id}/generation/epochs/2/mapping.json",
            "digest": output.mapping_digest,
        },
        "source_refs": [
            {
                "path": f"qa/changes/{output.change_id}/generated/api/files/tests/api/test_user.py",
                "digest": "1" * 64,
            }
        ],
        "plan_refs": [output.case_execution_plan_ref.model_dump(mode="json")],
        "case_execution_plan_ref": output.case_execution_plan_ref.model_dump(mode="json"),
        "case_execution_plan_digest": output.case_execution_plan_digest,
    }
    published = publish_execution(
        {
            "validation_profile": "api_db.v1",
            "generation_result": generation,
            "coverage_epoch": output.coverage_epoch,
            "repair_round": output.repair_round,
            "rounds_budget": 2,
            "rounds_used": 1,
        },
        output,
        {"receipt_id": "kernel-receipt", "receipt_digest": "9" * 64},
    )

    cycle = VerifiedExecutionCycleResultV1.model_validate(published["execution_result"])
    assert cycle.completion_status == "incomplete"
    assert cycle.execution_id == output.execution_id
    assert cycle.receipt.receipt_id == "kernel-receipt"
    assert cycle.mapping_digest == output.mapping_digest
    assert published["status"] == "incomplete"


def test_verified_publish_rejects_agent_all_pass_without_host_evidence() -> None:
    from test_execution_graph_factory import execution_evidence  # pyright: ignore[reportMissingImports]

    with pytest.raises(ValueError, match="verified host"):
        publish_execution(
            {
                "validation_profile": "api_db.v1",
                "rounds_budget": 2,
                "rounds_used": 0,
            },
            execution_evidence(),
            {"receipt_id": "kernel-receipt", "receipt_digest": "9" * 64},
        )


@pytest.mark.parametrize("record", ["action_terminal", "process_terminal", "outcome"])
def test_journal_partial_write_never_publishes_terminal(tmp_path, managed_sut, monkeypatch, record):
    import os

    _, _, journal, _ = setup_action(tmp_path, managed_sut, "atomic-" + record)
    journal.write("action_started", {"state": "started"})
    write = os.write
    calls = 0

    def partial(fd, value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("power cut")
        return write(fd, value[:7])

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", partial)
        with pytest.raises(KeyboardInterrupt):
            journal.write(record, {"state": "complete"})
    assert not (journal.root / (record + ".json")).exists()
    assert journal.read("action_started") == {"state": "started"}
    journal.write(record, {"state": "complete"})
    journal.write(record, {"state": "complete"})
    assert journal.read(record) == {"state": "complete"}
    with pytest.raises((ValueError, FileExistsError)):
        journal.write(record, {"state": "different"})


def test_unconfirmed_cleanup_retains_nonterminal_and_retries(managed_sut):
    from graph_engine.attempts.activity import TaskActivityIndeterminate
    from assurance_execution.operations.verified_execution import VerifiedExecutionHandler

    class CleanupHost(RealPipeHost):
        confirmed = False
        stops = 0

        def run(self, **kwargs):
            return super().run(**kwargs).model_copy(update={"cleanup_confirmed": False})

        def stop(self, container_name) -> bool:
            self.stops += 1
            return self.confirmed

    request, context, manifest, _ = handler_case(managed_sut, "cleanup-recovery")
    host = CleanupHost()
    handler = VerifiedExecutionHandler(process_host=host)
    with pytest.raises(TaskActivityIndeterminate, match="cleanup"):
        asyncio.run(handler.execute(request, context))
    root = context.write_root / manifest.evidence_root
    assert (root / "action_terminal.json").is_file()
    assert (root / "process_terminal.json").is_file()
    assert not (root / "outcome.json").exists()
    assert context.activity is not None
    for _ in range(2):
        result = asyncio.run(handler.reconcile(request, context, context.activity.snapshot))
        assert result.status == "indeterminate"
        assert not (root / "outcome.json").exists()
    host.confirmed = True
    result = asyncio.run(handler.reconcile(request, context, context.activity.snapshot))
    assert result.status == "terminal"
    assert host.stops == 3
    host.confirmed = False
    assert (
        asyncio.run(handler.reconcile(request, context, context.activity.snapshot)).status == "indeterminate"
    )


@pytest.mark.parametrize("record", ["action_terminal", "process_terminal", "outcome"])
def test_journal_crash_after_publish_leaves_complete_authenticated_record(
    tmp_path, managed_sut, monkeypatch, record
):
    from assurance_execution.operations import verified_execution

    _, _, journal, _ = setup_action(tmp_path, managed_sut, "published-" + record)
    publish = verified_execution._publish_exclusive

    def crash(source, destination):
        publish(source, destination)
        raise KeyboardInterrupt("after atomic publication")

    with monkeypatch.context() as patch:
        patch.setattr(verified_execution, "_publish_exclusive", crash)
        with pytest.raises(KeyboardInterrupt):
            journal.write(record, {"complete": True})
    assert journal.read(record) == {"complete": True}
    assert (journal.root / (record + ".json")).stat().st_nlink == 1


@pytest.mark.parametrize("attack", ["symlink", "hardlink"])
def test_journal_rejects_link_attacks_and_exclusive_action_replay(tmp_path, managed_sut, attack):
    import os

    _, _, journal, _ = setup_action(tmp_path, managed_sut, "links-" + attack)
    journal.write("action_started", {"started": True})
    with pytest.raises(FileExistsError):
        journal.write("action_started", {"started": True})
    source = journal.root / "action_started.json"
    if attack == "symlink":
        source.rename(tmp_path / "target")
        source.symlink_to(tmp_path / "target")
    else:
        os.link(source, tmp_path / "target")
    with pytest.raises(ValueError, match="nonregular"):
        journal.read("action_started")


def test_execute_near_process_deadline_never_posts(tmp_path, managed_sut):
    import sys
    from assurance_execution.operations.verified_process import run_bridge_process, ProcessLimits
    from assurance_execution.operations.sqlite_oracle import observe_user

    plan, manifest, journal, token = setup_action(tmp_path, managed_sut, "near-deadline")
    code = 'import sys,time;sys.stdin.readline();time.sleep(.2);print(\'{"type":"execute","case_id":"case"}\',flush=True);sys.stdin.readline()'
    receipt = run_bridge_process(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        nodeid=manifest.nodeid,
        case_id="case",
        limits=ProcessLimits(timeout_seconds=1),
        execute=lambda _, control: execute_frozen_action(
            plan,
            manifest,
            journal,
            json.dumps({"token": token, "user_password": "parent-password"}).encode(),
            control,
        ),
    )
    assert receipt.reason == "parent_execution_error"
    assert receipt.cleanup_confirmed
    assert journal.read("action_started") is None
    assert (
        observe_user(Path(manifest.sqlite.path), manifest.inputs.username, manifest.inputs.email)["rows"]
        == []
    )


def test_cancellation_during_real_http_stops_worker_and_never_replays(tmp_path, managed_sut):
    import sys
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from assurance_execution.contracts.verification import ManagedSutV1
    from assurance_execution.operations.verified_process import run_bridge_process

    plan, manifest, _, token = setup_action(tmp_path, managed_sut, "cancel-http")
    requested = threading.Event()
    release = threading.Event()
    posts = []

    class SlowPeer(BaseHTTPRequestHandler):
        def do_POST(self):
            posts.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            requested.set()
            release.wait(3)

        def log_message(self, format: str, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowPeer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manifest = manifest.model_copy(
        update={
            "sut": ManagedSutV1(
                instance_id=manifest.sut.instance_id,
                base_url=f"http://127.0.0.1:{server.server_port}",
                sqlite_path=manifest.sqlite.path,
            )
        }
    )
    journal = ActionJournal(tmp_path / "cancel-evidence", manifest, b"host-key" * 4)
    credential = json.dumps({"token": token, "user_password": "parent-password"}).encode()
    code = 'import sys;sys.stdin.readline();print(\'{"type":"execute","case_id":"case"}\',flush=True);sys.stdin.readline()'
    before = time.monotonic()
    cleanup = []
    try:
        receipt = run_bridge_process(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            nodeid=manifest.nodeid,
            case_id="case",
            execute=lambda _, control: execute_frozen_action(plan, manifest, journal, credential, control),
            cancel_requested=requested.is_set,
            cleanup=lambda: cleanup.append(True) or True,
        )
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert time.monotonic() - before < 2
    assert receipt.reason == "cancelled"
    assert receipt.cleanup_confirmed and cleanup == [True]
    terminal = journal.read("action_terminal")
    assert terminal is not None and terminal["http"]["state"] == "timeout"
    assert terminal["oracle"]["state"] == "skipped"
    with pytest.raises(ValueError, match="already started"):
        execute_frozen_action(plan, manifest, journal, credential)
    assert posts == ["/api/v1/user/create"]
    assert not any(item.name.startswith("verified-action") for item in threading.enumerate())


def formal_plan(profile: Literal["api_db.v1", "api_db_trace.v1"] = "api_db.v1"):
    raw = read_fixture("user-plan.json")
    return compile_case_plan(
        read_fixture("user-case.json"),
        AssertionSourcesV1.model_validate(read_fixture("user-sources.json")),
        cast(dict[str, object], raw["bindings"]),
        profile,
        context=CasePlanContextV1.model_validate(raw["context"]),
    )


def _accepted_verified_root(project: Path):
    from tests.verified_generation_fixture import accepted_verified_execution_input

    return accepted_verified_execution_input(project)


def _canonical_document(path: Path, document: dict[str, Any]) -> str:
    from graph_engine.canonical import canonical_json_bytes

    encoded = canonical_json_bytes(cast(Any, document)) + b"\n"
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def _synchronize_public_generation_refs(
    project: Path,
    raw: dict[str, Any],
    *,
    machine_document: dict[str, Any] | None = None,
) -> None:
    generation = cast(dict[str, Any], raw["generation_result"])
    reviewed = cast(dict[str, Any], generation["reviewed_case"])
    machine_ref = cast(dict[str, str], generation["case_execution_plan_ref"])
    machine_path = project / machine_ref["path"]
    machine = machine_document or cast(dict[str, Any], json.loads(machine_path.read_bytes()))
    for case in cast(list[dict[str, Any]], machine["cases"]):
        case["reviewed_case"] = reviewed
    machine_ref = {"path": machine_ref["path"], "digest": _canonical_document(machine_path, machine)}
    generation["case_execution_plan_ref"] = machine_ref
    generation["case_execution_plan_digest"] = machine_ref["digest"]
    generation["plan_refs"] = [
        machine_ref if item["path"] == machine_ref["path"] else item for item in generation["plan_refs"]
    ]
    cast(dict[str, Any], raw["verification"])["case_execution_plan_ref"] = machine_ref

    manifest_ref = next(
        item for item in generation["source_refs"] if item["path"].endswith("-generated-files.json")
    )
    manifest_path = project / manifest_ref["path"]
    manifest = cast(dict[str, Any], json.loads(manifest_path.read_bytes()))
    mapping = cast(dict[str, Any], manifest["mapping"])
    mapping["reviewed_case"] = reviewed
    mapping["case_execution_plan_ref"] = machine_ref
    mapping["case_execution_plan_digest"] = machine_ref["digest"]
    manifest_ref["digest"] = _canonical_document(manifest_path, manifest)


def _damage_generation(project: Path, root, fault: str):
    raw = root.model_dump(mode="json")
    generation = cast(dict[str, Any], raw["generation_result"])
    if fault == "missing":
        raw["generation_result"] = None
    elif fault == "epoch":
        generation["coverage_epoch"] = 1
        reviewed = cast(dict[str, Any], generation["reviewed_case"])
        reviewed["coverage_epoch"] = 1
    elif fault == "reviewed_case":
        ref = cast(dict[str, str], cast(dict[str, Any], generation["reviewed_case"])["case_refs"][0])
        (project / ref["path"]).write_bytes(b"stale reviewed case\n")
    elif fault == "root_plan":
        (project / root.plan_ref.path).write_bytes(b"stale root plan\n")
    elif fault == "machine_plan":
        assert root.verification is not None
        (project / root.verification.case_execution_plan_ref.path).write_bytes(b"stale machine plan\n")
    elif fault == "mapping":
        (project / generation["mapping_ref"]["path"]).write_bytes(b"{}\n")
    elif fault == "source":
        source = next(item for item in generation["source_refs"] if "/generated/api/files/" in item["path"])
        (project / source["path"]).write_bytes(b"stale generated source\n")
    elif fault == "forged_bridge":
        source = next(item for item in generation["source_refs"] if "/generated/api/files/" in item["path"])
        path = project / source["path"]
        path.write_bytes(b"def test_tc_user_create_001__create():\n    pass\n")
        source["digest"] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif fault == "forged_review":
        reviewed = cast(dict[str, Any], generation["reviewed_case"])
        review_ref = cast(dict[str, str], reviewed["review_ref"])
        review_path = project / review_ref["path"]
        review = cast(dict[str, Any], json.loads(review_path.read_bytes()))
        review["decision"] = "reject"
        review_ref["digest"] = _canonical_document(review_path, review)
        _synchronize_public_generation_refs(project, raw)
    elif fault == "forged_pass_review":
        reviewed = cast(dict[str, Any], generation["reviewed_case"])
        review_ref = cast(dict[str, str], reviewed["review_ref"])
        review_path = project / review_ref["path"]
        review = cast(dict[str, Any], json.loads(review_path.read_bytes()))
        review["risk_level"] = "critical"
        review_ref["digest"] = _canonical_document(review_path, review)
        _synchronize_public_generation_refs(project, raw)
    elif fault == "forged_provenance":
        reviewed = cast(dict[str, Any], generation["reviewed_case"])
        source_ref = next(
            item for item in reviewed["preparation_refs"] if item["path"].endswith("assertion-sources.json")
        )
        source_path = project / source_ref["path"]
        sources = cast(dict[str, Any], json.loads(source_path.read_bytes()))
        cast(dict[str, Any], cast(list[Any], sources["sources"])[0])["content_ref"] = {
            "path": f"qa/changes/{root.change_id}/requirement.md",
            "digest": "0" * 64,
        }
        source_ref["digest"] = _canonical_document(source_path, sources)
        _synchronize_public_generation_refs(project, raw)
    elif fault == "machine_semantics":
        machine_ref = cast(dict[str, str], generation["case_execution_plan_ref"])
        machine = cast(dict[str, Any], json.loads((project / machine_ref["path"]).read_bytes()))
        cast(dict[str, Any], cast(list[dict[str, Any]], machine["cases"])[0]["inputs"])["username"] = (
            "forged_user"
        )
        _synchronize_public_generation_refs(project, raw, machine_document=machine)
    elif fault == "machine_context":
        machine_ref = cast(dict[str, str], generation["case_execution_plan_ref"])
        machine = cast(dict[str, Any], json.loads((project / machine_ref["path"]).read_bytes()))
        cast(list[dict[str, Any]], machine["cases"])[0]["technical_config_digest"] = "0" * 64
        _synchronize_public_generation_refs(project, raw, machine_document=machine)
    elif fault in {"spec", "profile"}:
        manifest = next(
            item for item in generation["source_refs"] if item["path"].endswith("-generated-files.json")
        )
        path = project / manifest["path"]
        document = json.loads(path.read_bytes())
        if fault == "spec":
            document["mapping"]["case_spec_digests"]["TC_USER_CREATE_001"] = "0" * 64
        else:
            document["mapping"]["validation_profile"] = "api_db_trace.v1"
        encoded = json.dumps(document, sort_keys=True).encode()
        path.write_bytes(encoded)
        manifest["digest"] = hashlib.sha256(encoded).hexdigest()
    elif fault == "replay":
        replay = project / "qa/changes/CH-USER-001/generation/epochs/1/mapping.json"
        replay.parent.mkdir(parents=True, exist_ok=True)
        replay.write_bytes((project / generation["mapping_ref"]["path"]).read_bytes())
        generation["mapping_ref"] = {
            "path": replay.relative_to(project).as_posix(),
            "digest": hashlib.sha256(replay.read_bytes()).hexdigest(),
        }
    return raw


def test_accepted_generation_closure_authenticates_before_verified_dispatch(tmp_path: Path) -> None:
    from assurance_execution.operations.agent_skills import authenticate_generation_result

    root = _accepted_verified_root(tmp_path)

    authenticate_generation_result(root, tmp_path)


def test_exact_invalid_generated_bridge_becomes_typed_incomplete_before_dispatch(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from assurance_execution.contracts.agent import ExecutionPrepareInputV1
    from assurance_product.models import VerificationHostConfigV1
    from assurance_product.verification_execution import (
        ProfiledExecutionExecutor,
        VerificationConfiguration,
    )

    attempt_key = AttemptKey(digest="4" * 64)
    root = _accepted_verified_root(tmp_path)
    damaged = ExecutionPrepareInputV1.model_validate(_damage_generation(tmp_path, root, "forged_bridge"))
    calls: list[str] = []
    facade = ProfiledExecutionExecutor(
        config=VerificationConfiguration(
            validation_profile="api_db.v1",
            host=VerificationHostConfigV1(
                managed_sut_authority_handle="sut.authority",
                credential_handle="sut.credential",
            ),
        ),
        config_digest="c" * 64,
        legacy=None,
        callable_path="assurance_execution.operations.verified_attempt:VerifiedAttemptHandler.execute",
    )

    class Host:
        async def execute(self, call):
            calls.append("host")
            return SimpleNamespace(outcome=None)

    facade._host = Host()
    result = asyncio.run(
        facade.execute(
            damaged,
            cast(
                Any,
                SimpleNamespace(
                    workspace=SimpleNamespace(project_root=tmp_path),
                    execution=SimpleNamespace(attempt_key=attempt_key),
                ),
            ),
        )
    )

    defect = VerifiedBridgeDefectResultV1.model_validate(result.output.root)
    assert defect.completion_status == "incomplete"
    assert defect.defect.defect_kind == "invalid_bridge"
    assert defect.defect.attempt_key == attempt_key
    assert defect.batch_id == attempt_key.digest
    assert calls == []


def test_legacy_execution_rejects_generation_from_a_stale_coverage_epoch(tmp_path: Path) -> None:
    from assurance_execution.contracts.agent import ExecutionPrepareInputV1
    from assurance_execution.operations.agent_skills import authenticate_generation_result
    from assurance_execution.operations.common import InputError

    root = _accepted_verified_root(tmp_path)
    raw = _damage_generation(tmp_path, root, "epoch")
    raw["validation_profile"] = None
    raw["verification_config_digest"] = None
    raw["verification"] = None
    legacy = ExecutionPrepareInputV1.model_validate(raw)

    with pytest.raises(InputError, match="generation result identity"):
        authenticate_generation_result(legacy, tmp_path)


def test_legacy_execution_accepts_generation_with_matching_identity(tmp_path: Path) -> None:
    from assurance_execution.contracts.agent import ExecutionPrepareInputV1
    from assurance_execution.operations.agent_skills import authenticate_generation_result

    raw = _accepted_verified_root(tmp_path).model_dump(mode="json")
    raw["validation_profile"] = None
    raw["verification_config_digest"] = None
    raw["verification"] = None
    legacy = ExecutionPrepareInputV1.model_validate(raw)

    authenticate_generation_result(legacy, tmp_path)


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "epoch",
        "reviewed_case",
        "root_plan",
        "machine_plan",
        "mapping",
        "source",
        "forged_bridge",
        "forged_review",
        "forged_pass_review",
        "forged_provenance",
        "machine_semantics",
        "machine_context",
        "spec",
        "profile",
        "replay",
    ],
)
def test_task_facade_and_verified_attempt_reject_generation_drift_before_side_effects(
    tmp_path: Path, monkeypatch, fault: str
) -> None:
    from types import SimpleNamespace

    from assurance_execution.contracts.agent import ExecutionPrepareInputV1
    from assurance_execution.operations import verified_attempt, verified_execution
    from assurance_execution.operations.verified_attempt import VerifiedAttemptHandler
    from assurance_product.models import VerificationHostConfigV1
    from assurance_product.verification_execution import ProfiledExecutionExecutor, VerificationConfiguration
    from graph_engine.plugin_api import InvocationMetadata, TaskContext, TaskRequest, TaskWorkspaceIdentity
    from graph_engine.canonical import canonical_digest

    root = _accepted_verified_root(tmp_path)
    damaged = ExecutionPrepareInputV1.model_validate(_damage_generation(tmp_path, root, fault))
    calls: list[str] = []
    facade = ProfiledExecutionExecutor(
        config=VerificationConfiguration(
            validation_profile="api_db.v1",
            host=VerificationHostConfigV1(
                managed_sut_authority_handle="sut.authority", credential_handle="sut.credential"
            ),
        ),
        config_digest="c" * 64,
        legacy=None,
        callable_path="assurance_execution.operations.verified_attempt:VerifiedAttemptHandler.execute",
    )

    class Host:
        async def execute(self, call):
            calls.append("host")
            return SimpleNamespace(outcome=None)

    def host_call(value, scope):
        calls.append("host-call")
        return SimpleNamespace()

    facade._host = Host()
    facade._call = host_call  # type: ignore[method-assign]
    with pytest.raises(ValueError):
        asyncio.run(
            facade.execute(
                damaged,
                cast(Any, SimpleNamespace(workspace=SimpleNamespace(project_root=tmp_path))),
            )
        )
    assert calls == []

    identity_raw = {
        "task_id": "d" * 64,
        "attempt": 1,
        "attempt_id": "attempt-1",
        "output_paths": [],
        "baseline_files": [],
        "project_digest": "a" * 64,
        "write_root_digest": "b" * 64,
        "layout_schema_version": "1",
    }
    identity = TaskWorkspaceIdentity(**identity_raw, identity_digest=canonical_digest(identity_raw))
    invocation = InvocationMetadata(
        invocation_id="inv", lock_digest="a" * 64, composition_digest="b" * 64, entrypoint="full"
    )
    request = TaskRequest(
        invocation_id="inv",
        task_id="task",
        graph_instance_id="graph",
        node_id="execution.execute",
        capability_id="assurance.execution.verified-attempt",
        invocation=invocation,
        attempt=1,
        input=damaged.model_dump(mode="json"),
        binding_data={
            "user_host": {"sut_source_root": str(REPO)},
            "readiness": {
                "selection_handle": "sut.selection",
                "authority_handle": "sut.authority",
                "collector_handle": None,
                "configuration_digest": "c" * 64,
                "validation_profile": "api_db.v1",
            },
        },
    )
    context = TaskContext(
        project_root=tmp_path,
        write_root=tmp_path,
        workspace_identity=identity,
        heartbeat=lambda: None,
        cancel_requested=lambda: False,
        invocation=invocation,
        secrets=Secrets({}),
    )
    assert damaged.verification is not None
    selection = SimpleNamespace(
        workspace_root=str(tmp_path.resolve()),
        verification=damaged.verification,
        execution_id="00000000-0000-4000-8000-000000000001",
    )
    monkeypatch.setattr(verified_attempt, "authenticate_host_readiness", lambda *args, **kwargs: selection)
    posts: list[str] = []
    monkeypatch.setattr(verified_execution, "_post", lambda *args, **kwargs: posts.append("http"))
    handler = VerifiedAttemptHandler()
    with pytest.raises(ValueError):
        asyncio.run(handler.execute(request, context))
    assert posts == []
    assert list(tmp_path.rglob("action_started.json")) == []


@pytest.fixture(scope="module")
def managed_sut(tmp_path_factory):
    yield from _managed_sut(tmp_path_factory.mktemp("task4-managed"))


@pytest.fixture
def managed_fault_sut(tmp_path_factory, request):
    yield from _managed_sut(tmp_path_factory.mktemp("task4-fault"), fault=request.param)


def _managed_sut(workspace, *, fault="none"):
    from assurance_execution.operations.managed_sut import ManagedUserSutHost

    harness = ManagedUserSutHost(
        source_root=REPO,
        secret_port=Secrets(
            {
                "managed-sut.admin-password": b"task4-host-only-canary",
                "managed-sut.reset-password": b"task4-host-only-canary",
                "managed-sut.secret-key": b"task4-host-only-canary",
            }
        ),
    )
    prepared = harness.prepare(
        workspace_root=workspace, project_dir=workspace / "project", run_root=workspace / "run", fault=fault
    )
    started = harness.start(workspace_root=workspace, prepare_receipt=workspace / "run/harness-prepare.json")
    try:
        response = httpx.post(
            started["base_url"] + "/api/v1/base/access_token",
            json={"username": "admin", "password": "task4-host-only-canary"},
            trust_env=False,
        )
        response.raise_for_status()
        token = response.json()["data"]["access_token"]
        yield workspace, prepared, started, token
    finally:
        harness.stop(
            workspace_root=workspace,
            receipt_path=workspace / "run/owned-process.json",
            instance_id=started["instance_id"],
        )
        for owner in _ACTIVITY_OWNERS:
            owner.loop.call_soon_threadsafe(owner.loop.stop)
            owner.thread.join(timeout=2)
            owner.loop.close()
        _ACTIVITY_OWNERS.clear()


def setup_action(tmp_path: Path, managed_sut, suffix: str, *, plan=None):
    _, prepared, started, token = managed_sut
    plan = plan or formal_plan()
    from assurance_execution.operations.verification_manifest import allocate_user_inputs
    from assurance_execution.operations.sqlite_oracle import observe_user

    inputs = allocate_user_inputs(
        lambda username, email: bool(observe_user(Path(prepared["sqlite_path"]), username, email)["rows"]),
        token_factory=lambda: hashlib.sha256(suffix.encode()).hexdigest()[:12],
    )
    manifest = build_verification_manifest(
        change_id=plan.change_id,
        case_id=plan.case_id,
        nodeid="tests/test_case.py::test_case",
        invocation_id="invocation",
        task_id="task",
        graph_instance_id="graph",
        attempt_key=AttemptKey(digest=hashlib.sha256(suffix.encode()).hexdigest()),
        business_activation=BusinessActivation.for_trigger("execution"),
        coverage_epoch=0,
        repair_round=0,
        authorization_scope_digest="a" * 64,
        activity_receipt_digest="b" * 64,
        plan_ref=plan.plan_ref.path,
        plan_digest=plan.plan_digest,
        case_execution_plan_ref="plans/case.json",
        case_execution_plan_digest="c" * 64,
        spec_digest=plan.spec_digest,
        mapping_digest="d" * 64,
        sut_digest=plan.sut_digest,
        technical_config_digest=plan.technical_config_digest,
        validation_profile="api_db.v1",
        sut_base_url=started["base_url"],
        sut_instance_id=started["instance_id"],
        sut_sqlite_path=Path(prepared["sqlite_path"]),
        sqlite_path=Path(prepared["sqlite_path"]),
        username=inputs.username,
        email=inputs.email,
        evidence_root=f"qa/changes/{plan.change_id}/execution",
    )
    journal = ActionJournal(tmp_path / "evidence", manifest, b"host-key" * 4)
    return plan, manifest, journal, token


def test_real_parent_http_then_new_sqlite_observer(tmp_path, managed_sut, monkeypatch):
    from assurance_execution.operations import verified_execution

    original_observe = verified_execution.observe_user
    timeouts = []

    def observe(*args, **kwargs):
        timeouts.append(kwargs["timeout_s"])
        return original_observe(*args, **kwargs)

    monkeypatch.setattr(verified_execution, "observe_user", observe)
    plan, manifest, journal, token = setup_action(tmp_path, managed_sut, "created")
    execute_frozen_action(
        plan,
        manifest,
        journal,
        json.dumps({"token": token, "user_password": "host-only-created-password"}).encode(),
    )
    started = journal.read("action_started")
    terminal = journal.read("action_terminal")
    assert started is not None and terminal is not None
    assert started["execution_id"] == manifest.execution_id
    assert terminal["http"]["status"] == 200
    assert terminal["http"]["code"] == 200
    assert terminal["initial"]["rows"] == []
    assert terminal["oracle"]["rows"] == [manifest.inputs.model_dump(mode="json")]
    assert len(timeouts) == 2  # One initial read and one bounded post-action read.
    assert all(0 < timeout <= 2 for timeout in timeouts)
    assert token not in json.dumps(terminal)
    with pytest.raises(ValueError, match="already started"):
        execute_frozen_action(
            plan,
            manifest,
            journal,
            json.dumps({"token": token, "user_password": "host-only-created-password"}).encode(),
        )


def test_no_action_fault_runs_bridge_without_dispatching_business_action(tmp_path, managed_sut):
    from assurance_execution.operations.verified_execution import collect_facts

    plan, manifest, journal, token = setup_action(tmp_path, managed_sut, "no-action")
    execute_frozen_action(
        plan,
        manifest,
        journal,
        json.dumps(
            {
                "token": token,
                "user_password": "host-only-created-password",
                "benchmark_fault": "no-action",
            }
        ).encode(),
    )

    terminal = journal.read("action_terminal")
    assert terminal is not None
    assert terminal["http"] == {"state": "skipped", "reason": "benchmark_no_action"}
    assert terminal["oracle"] == {
        "state": "skipped",
        "reason": "action_not_dispatched",
        "rows": [],
    }
    facts = {item.obligation_id: item for item in collect_facts(journal, plan)}
    assert facts["action.finished"].state == "missing"
    assert facts["oracle.executed"].state == "missing"


def test_skip_oracle_fault_keeps_http_fact_but_marks_db_evidence_missing(tmp_path, managed_sut):
    from assurance_execution.operations.verified_execution import collect_facts

    plan, manifest, journal, token = setup_action(tmp_path, managed_sut, "skip-oracle")
    execute_frozen_action(
        plan,
        manifest,
        journal,
        json.dumps(
            {
                "token": token,
                "user_password": "host-only-created-password",
                "benchmark_fault": "skip-oracle",
            }
        ).encode(),
    )

    terminal = journal.read("action_terminal")
    assert terminal is not None
    assert terminal["http"]["state"] == "observed"
    assert terminal["oracle"] == {
        "state": "skipped",
        "reason": "benchmark_skip_oracle",
        "rows": [],
    }
    facts = {item.obligation_id: item for item in collect_facts(journal, plan)}
    assert facts["action.finished"].state == "observed"
    assert facts["oracle.executed"].state == "missing"


def test_journal_drift_is_rejected(tmp_path, managed_sut):
    _, _, journal, _ = setup_action(tmp_path, managed_sut, "drift")
    journal.write("action_started", {"state": "started"})
    path = tmp_path / "evidence/action_started.json"
    path.chmod(0o600)
    value = json.loads(path.read_text())
    value["payload"]["state"] = "finished"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="authentication"):
        journal.read("action_started")


def test_http_return_before_terminal_cut_never_resends(tmp_path, managed_sut, monkeypatch):
    plan, manifest, journal, token = setup_action(tmp_path, managed_sut, "cut")
    original_write = journal.write

    def interrupted(name: str, payload: dict[str, Any]):
        if name == "action_terminal":
            raise KeyboardInterrupt("crash after HTTP")
        return original_write(name, payload)

    monkeypatch.setattr(journal, "write", interrupted)
    with pytest.raises(KeyboardInterrupt):
        execute_frozen_action(
            plan,
            manifest,
            journal,
            json.dumps({"token": token, "user_password": "host-only-created-password"}).encode(),
        )
    assert journal.read("action_started") is not None
    assert journal.read("action_terminal") is None
    with pytest.raises(ValueError, match="already started"):
        execute_frozen_action(
            plan,
            manifest,
            journal,
            json.dumps({"token": token, "user_password": "host-only-created-password"}).encode(),
        )


def test_recoverable_handler_uses_production_activity_protocol():
    from assurance_execution.operations.verified_execution import VerifiedExecutionHandler
    from graph_engine.plugin_api import RecoverableTaskHandler

    assert isinstance(VerifiedExecutionHandler(), RecoverableTaskHandler)


class Activity:
    """Use the production journal-backed activity port with a test fence owner."""

    def __init__(self, identity, request):
        import threading
        from graph_engine.attempts.activity import JournalBackedTaskActivityPort
        from graph_engine.attempts.events import AttemptOpened, ResourcesAuthorized, ActivityPrepared
        from graph_engine.attempts.host_protocol import TaskActivityRpcIdentity, current_bound_identity
        from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
        from graph_engine.canonical import canonical_digest

        loop = asyncio.new_event_loop()
        self.loop = loop
        self.thread = threading.Thread(target=loop.run_forever, daemon=True)
        self.thread.start()
        _ACTIVITY_OWNERS.append(self)
        journal = MemoryAttemptJournal()
        key = AttemptKey(digest=identity.task_id)

        async def seed():
            await journal.append(
                key,
                (
                    AttemptOpened(
                        contract_digest="d" * 64,
                        input_digest="e" * 64,
                        graph_revision="c" * 64,
                        invocation_id=request.invocation_id,
                        public_entrypoint="full",
                        semantic_node_id=request.node_id,
                    ),
                    ResourcesAuthorized(authorization_id="b" * 64),
                    ActivityPrepared(activity_id="activity"),
                ),
                expected_revision=0,
                fencing_token=1,
            )

        asyncio.run_coroutine_threadsafe(seed(), loop).result(timeout=5)
        bound = current_bound_identity(
            attempt_key_digest=key.digest,
            authorization_id="b" * 64,
            workspace_identity_digest=identity.identity_digest,
            request_digest=canonical_digest(request.model_dump(mode="json")),
            graph_revision="c" * 64,
            product_lock_digest=request.invocation.lock_digest,
            handler_id=request.capability_id,
        )
        rpc = TaskActivityRpcIdentity.model_validate(
            dict(
                bound,
                invocation_id=request.invocation_id,
                task_id=request.task_id,
                activation_id="activation",
                attempt=1,
                activity_id="activity",
            )
        )

        async def fence():
            return None

        self.port = JournalBackedTaskActivityPort(
            journal=journal,
            attempt_key=key,
            identity=rpc,
            workspace_identity=identity,
            assert_live_fence=fence,
            owner_loop=loop,
            remaining_deadline=5,
        )

    @property
    def snapshot(self):
        return self.port.snapshot

    def mark_dispatch_started(self, fingerprint):
        return self.port.mark_dispatch_started(fingerprint)

    def bind(self, reference):
        return self.port.bind(reference)


class Secrets:
    def __init__(self, values):
        self.values = values

    def resolve(self, handle):
        return self.values[handle]


class RealPipeHost:
    """Exercises real installed pytest transport; it does not qualify OCI isolation."""

    def preflight(self):
        return {"transport": "real-pipe-contract-test"}

    def run(self, *, view, nodeid, case_id, container_name, execute, cancel_requested):
        from assurance_execution.operations.verified_process import SubprocessVerificationHost

        return SubprocessVerificationHost().run(
            view=view,
            nodeid=nodeid,
            case_id=case_id,
            container_name=container_name,
            execute=execute,
            cancel_requested=cancel_requested,
        )

    def stop(self, container_name) -> bool:
        return True


def handler_case(managed_sut, suffix, source=None, store=None):
    from assurance_execution.contracts.agent import VerifiedExecutionPrepareV1
    from assurance_execution.operations.verified_execution import VerifiedExecutionInputV1
    from assurance_execution.operations.verification_manifest import build_managed_sut_authority
    from assurance_execution.execution_view import build_or_authenticate_execution_view
    from assurance_execution.generated_merge import GeneratedFileV2, MergedGeneratedSet, staged_generated_path
    from assurance_generation.contracts.execution_plan import CaseExecutionPlanSetV1
    from graph_engine.canonical import canonical_digest
    from graph_engine.plugin_api import InvocationMetadata, TaskWorkspaceIdentity, TaskContext, TaskRequest
    from tests.verified_generation_fixture import accepted_verified_execution_input

    workspace, prepared, started, token = managed_sut
    project = workspace
    write_root = project
    accepted = accepted_verified_execution_input(
        project, reviewed_source_path="project/app/controllers/user.py"
    )
    assert accepted.verification is not None
    plan = CaseExecutionPlanSetV1.model_validate_json(
        (project / accepted.verification.case_execution_plan_ref.path).read_bytes()
    ).cases[0]
    plan, manifest, _, _ = setup_action(write_root, managed_sut, suffix, plan=plan)
    artifact_root = f"qa/changes/{manifest.change_id}/.staging/execution/{suffix}"
    plan_relative = f"{artifact_root}/case-plan.json"
    manifest_relative = f"{artifact_root}/manifest.json"
    raw = {
        "task_id": manifest.attempt_key.digest,
        "attempt": 1,
        "attempt_id": "attempt-1",
        "output_paths": [],
        "baseline_files": [],
        "project_digest": "a" * 64,
        "write_root_digest": "b" * 64,
        "layout_schema_version": "1",
    }
    identity = TaskWorkspaceIdentity(**raw, identity_digest=canonical_digest(raw))
    if store is not None:
        from assurance_execution.execution_view import execution_view_relative

        binding = store.begin(
            task_id=manifest.attempt_key.digest,
            attempt=1,
            output_paths=(
                manifest.evidence_root,
                execution_view_relative(manifest.change_id, suffix, manifest.execution_id),
                f"qa/changes/{manifest.change_id}/execution/execute-result.json",
            ),
        )
        identity, write_root = binding.identity, binding.write_root
    activity_digest = canonical_digest(
        {
            "attempt_key": identity.task_id,
            "invocation_id": "invocation",
            "task_id": "task",
            "graph_instance_id": "graph",
            "node_id": "execution.execute",
            "workspace_identity_digest": identity.identity_digest,
        }
    )
    plan_ref_path = project / plan_relative
    plan_ref_path.parent.mkdir(parents=True, exist_ok=True)
    plan_bytes = CaseExecutionPlanSetV1(change_id=plan.change_id, cases=(plan,)).model_dump_json().encode()
    plan_ref_path.write_bytes(plan_bytes)
    manifest = manifest.model_copy(
        update={
            "authorization_scope_digest": identity.identity_digest,
            "activity_receipt_digest": activity_digest,
            "coverage_epoch": plan.coverage_epoch,
            "case_execution_plan_ref": plan_ref_path.relative_to(project).as_posix(),
            "case_execution_plan_digest": hashlib.sha256(plan_bytes).hexdigest(),
        }
    )
    manifest_path = project / manifest_relative
    manifest_path.write_text(manifest.model_dump_json())
    authority_token = project / "run/.ownership-token"
    token_stat = authority_token.stat()

    def ref(path):
        return {
            "path": path.relative_to(project).as_posix(),
            "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    authority = build_managed_sut_authority(
        run_root=project / "run",
        ownership_token_path=authority_token,
        ownership_token_device=token_stat.st_dev,
        ownership_token_inode=token_stat.st_ino,
        ownership_token_digest="sha256:" + hashlib.sha256(authority_token.read_bytes()).hexdigest(),
        prepare_receipt_digest=ref(project / "run/harness-prepare.json")["digest"],
        start_receipt_digest=ref(project / "run/owned-process.json")["digest"],
        authorization_scope_digest=identity.identity_digest,
        activity_receipt_digest=activity_digest,
    )
    profile = VerifiedExecutionPrepareV1(
        validation_profile="api_db.v1",
        case_execution_plan_ref=EvidenceArtifactRefV1.model_validate(ref(plan_ref_path)),
        nodeid=manifest.nodeid,
        business_activation=manifest.business_activation,
        sut_instance_id=started["instance_id"],
        sut_base_url=started["base_url"],
        managed_sqlite_path=prepared["sqlite_path"],
        observer_sqlite_path=prepared["sqlite_path"],
        user_inputs=manifest.inputs,
        managed_sut_prepare_receipt_ref=EvidenceArtifactRefV1.model_validate(
            ref(project / "run/harness-prepare.json")
        ),
        managed_sut_start_receipt_ref=EvidenceArtifactRefV1.model_validate(
            ref(project / "run/owned-process.json")
        ),
        managed_sut_authority_handle="sut-authority",
    )
    test_source = (
        source
        or 'from assurance_execution.bridge import execute_case\ndef test_case():\n    execute_case("TC_USER_CREATE_001")\n'
    )
    target = "tests/test_case.py"
    staged = staged_generated_path(manifest.change_id, "api", target)
    staged_path = project / staged
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_text(test_source)
    item = GeneratedFileV2(
        target_path=target,
        staged_path=staged,
        sha256="sha256:" + hashlib.sha256(test_source.encode()).hexdigest(),
        mode=0o644,
        operation="generated",
        family="api",
    )
    merged = MergedGeneratedSet(files=(item,), digest=canonical_digest([item.model_dump(mode="json")]))
    view = build_or_authenticate_execution_view(
        project,
        write_root=write_root,
        request=merged.execution_view_input(
            change_id=manifest.change_id,
            batch_id=suffix,
            execution_id=manifest.execution_id,
            selected=(manifest.nodeid,),
        ),
    )
    payload = VerifiedExecutionInputV1(
        manifest_ref=EvidenceArtifactRefV1.model_validate(ref(manifest_path)), verification=profile, view=view
    )
    invocation = InvocationMetadata(
        invocation_id="invocation", lock_digest="a" * 64, composition_digest="b" * 64, entrypoint="full"
    )
    request = TaskRequest(
        invocation_id="invocation",
        task_id="task",
        graph_instance_id="graph",
        node_id="execution.execute",
        capability_id="assurance.execution.run-tests",
        invocation=invocation,
        attempt=1,
        input=payload.model_dump(mode="json"),
    )
    context = TaskContext(
        project_root=project,
        write_root=write_root,
        workspace_identity=identity,
        heartbeat=lambda: None,
        cancel_requested=lambda: False,
        invocation=invocation,
        activity=Activity(identity, request),
        secrets=Secrets(
            {
                "sut-authority": authority.model_dump_json().encode(),
                plan.action.credential_ref: json.dumps(
                    {"token": token, "user_password": "host-password"}
                ).encode(),
            }
        ),
    )
    return request, context, manifest, plan


@pytest.mark.parametrize("row_count", [0, 1])
def test_unknown_http_keeps_user_postconditions_missing(managed_sut, monkeypatch, row_count):
    from assurance_execution.operations import verified_execution

    request, context, manifest, _ = handler_case(managed_sut, f"unknown_http_{row_count}")
    assert context.activity is not None
    original_post = verified_execution._post
    posts = []

    async def lose_response(plan, manifest, credential):
        posts.append(manifest.execution_id)
        if row_count == 0:
            credential = json.dumps({"token": "invalid", "user_password": "host-password"}).encode()
        await original_post(plan, manifest, credential)
        raise httpx.ReadError("response lost after dispatch")

    monkeypatch.setattr(verified_execution, "_post", lose_response)
    handler = verified_execution.VerifiedExecutionHandler(process_host=RealPipeHost())
    outcome = asyncio.run(handler.execute(request, context))
    result = VerifiedExecutionResultV1.model_validate(outcome.output)
    terminal_path = context.write_root / manifest.evidence_root / "action_terminal.json"
    retained = terminal_path.read_bytes()
    terminal = json.loads(retained)["payload"]
    assert terminal["http"] == {"state": "timeout", "reason": "http_terminal_unknown"}
    assert len(terminal["oracle"]["rows"]) == row_count
    observations = {item.obligation_id: item for item in result.evidence.observations}
    assert observations["oracle.executed"].state == "observed"
    assert observations["oracle.executed"].actual is True
    assert observations["user.row_count"].state == "missing"
    for key, observation in observations.items():
        if key.startswith("user."):
            assert observation.state == "missing", key
            assert observation.actual is None, key
            assert observation.reason == "http_terminal_unknown", key
    assert observations["action.finished"].state == "missing"
    assert observations["action.finished"].reason == "http_terminal_unknown"
    assert result.completion_status == "incomplete"
    assert result.evidence.state == "incomplete"
    for _ in range(2):
        recovered = asyncio.run(handler.reconcile(request, context, context.activity.snapshot))
        assert recovered.status == "terminal"
        assert recovered.outcome == outcome
        assert terminal_path.read_bytes() == retained
    assert posts == [manifest.execution_id]


def test_unknown_http_two_diagnostic_rows_remain_missing(tmp_path, managed_sut):
    from assurance_execution.operations.verified_execution import collect_facts

    plan, manifest, journal, _ = setup_action(tmp_path, managed_sut, "unknown_http_two")
    rows = [manifest.inputs.model_dump(mode="json")] * 2
    journal.write(
        "action_terminal",
        {
            "initial": {"state": "observed", "rows": []},
            "http": {"state": "timeout", "reason": "http_terminal_unknown"},
            "oracle": {"state": "observed", "rows": rows},
        },
    )
    observations = {item.obligation_id: item for item in collect_facts(journal, plan)}
    assert observations["oracle.executed"].actual is True
    for key, observation in observations.items():
        if key == "action.finished" or key.startswith("user."):
            assert observation.state == "missing", key
            assert observation.reason == "http_terminal_unknown", key
    terminal = journal.read("action_terminal")
    assert terminal is not None and terminal["oracle"]["rows"] == rows


@pytest.mark.parametrize(
    "managed_fault_sut", ["missing-write", "rollback-success", "rollback"], indirect=True
)
def test_known_http_terminal_keeps_zero_rows_evaluable(tmp_path, managed_fault_sut, request):
    from assurance_execution.operations.verified_execution import collect_facts

    fault = request.node.callspec.params["managed_fault_sut"]
    plan, manifest, journal, token = setup_action(tmp_path, managed_fault_sut, f"known_http_{fault}")
    execute_frozen_action(
        plan, manifest, journal, json.dumps({"token": token, "user_password": "host-password"}).encode()
    )
    terminal = journal.read("action_terminal")
    assert terminal is not None
    assert terminal["http"]["state"] == "observed"
    assert terminal["http"]["status"] == (500 if fault == "rollback" else 200)
    if fault != "rollback":
        assert terminal["http"]["code"] == 200
    assert terminal["oracle"]["rows"] == []
    observations = {item.obligation_id: item for item in collect_facts(journal, plan)}
    assert observations["action.finished"].state == "observed"
    assert observations["action.finished"].actual is True
    assert observations["user.row_count"].state == "observed"
    assert observations["user.row_count"].actual == 0
    if fault.startswith("rollback"):
        facts = [
            json.loads(line)
            for line in (managed_fault_sut[0] / "run/fault-facts.jsonl").read_text().splitlines()
        ]
        assert any(item["event"] == "write_observed" and item["row_count"] == 1 for item in facts)
        assert any(item["event"] == "rollback_observed" and item["row_count"] == 0 for item in facts)


def test_handler_fixture_binds_reviewed_source_to_managed_runtime(managed_sut):
    _, context, _, plan = handler_case(managed_sut, "fixture-source-closure")
    prepared = managed_sut[1]
    review = context.project_root / plan.reviewed_case.review_ref.path
    assert review.is_file()
    assert hashlib.sha256(review.read_bytes()).hexdigest() == plan.reviewed_case.review_ref.digest
    source_paths = json.loads(review.read_bytes())["source_verification"]["reviewed_source_files"]
    refs = {ref.path: ref for ref in plan.reviewed_case.preparation_refs}
    for source in source_paths:
        ref = refs[source]
        mapping = prepared["source_file_mapping"][source]
        for path in (
            context.project_root / source,
            Path(prepared["frozen_artifact"]) / mapping["frozen_path"],
            Path(prepared["sut_dir"]) / mapping["runtime_path"],
        ):
            assert path.is_file()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == ref.digest


def test_full_parent_handler_real_pipe_http_sqlite_and_recovery(managed_sut):
    from assurance_execution.operations.verified_execution import VerifiedExecutionHandler

    request, context, manifest, plan = handler_case(managed_sut, "handler")
    assert plan.plan_digest != plan.plan_ref.digest
    assert context.activity is not None
    handler = VerifiedExecutionHandler(process_host=RealPipeHost())
    first = asyncio.run(handler.execute(request, context))
    result = VerifiedExecutionResultV1.model_validate(first.output)
    evidence = result.evidence
    authority = json.loads((context.write_root / result.execution_authority_ref.path).read_bytes())
    assert authority["payload"]["plan_ref"] == plan.plan_ref.model_dump(mode="json")
    assert evidence.state == "collected"
    observations = {item.obligation_id: item.actual for item in evidence.observations}
    assert observations["user.dept_id"] is None
    assert observations["user.username"] == manifest.inputs.username
    recovered = asyncio.run(handler.reconcile(request, context, context.activity.snapshot))
    assert recovered.status == "terminal"
    assert recovered.outcome == first
    for path in (context.write_root / manifest.evidence_root).rglob("*.json"):
        assert "host-password" not in path.read_text()
        assert managed_sut[3] not in path.read_text()


def test_parent_handler_rejects_mismatched_manifest_plan_identity(managed_sut):
    from assurance_execution.operations.verified_execution import (
        VerifiedExecutionHandler,
        VerifiedExecutionInputV1,
    )

    request, context, manifest, _ = handler_case(managed_sut, "mismatched_plan_identity")
    payload = VerifiedExecutionInputV1.model_validate(request.input)
    manifest_path = context.project_root / payload.manifest_ref.path
    manifest_path.write_text(manifest.model_copy(update={"plan_digest": "f" * 64}).model_dump_json())
    payload = payload.model_copy(
        update={
            "manifest_ref": payload.manifest_ref.model_copy(
                update={"digest": hashlib.sha256(manifest_path.read_bytes()).hexdigest()}
            )
        }
    )
    request = request.model_copy(update={"input": payload.model_dump(mode="json")})
    with pytest.raises(ValueError, match="formal plan does not match manifest"):
        asyncio.run(VerifiedExecutionHandler(process_host=RealPipeHost()).execute(request, context))
    assert not (context.write_root / manifest.evidence_root / "action_started.json").exists()


@pytest.mark.parametrize(
    "cut",
    [
        "before_dispatch",
        "dispatch_before_action",
        "http_before_terminal",
        "before_seal",
        "after_promotion",
        "partial_action_terminal",
        "partial_process_terminal",
        "partial_outcome",
    ],
)
def test_production_handler_recovery_cuts_never_repeat_post(managed_sut, monkeypatch, cut):
    from assurance_execution.operations.verified_execution import VerifiedExecutionHandler

    store = None
    if cut == "after_promotion":
        from graph_engine.attempts.workspace import TaskWorkspaceStore

        root = managed_sut[0]
        store = TaskWorkspaceStore(root, root / "promotion-attempts", root / "promotion-receipts")
    request, context, manifest, _ = handler_case(managed_sut, "cut_" + cut, store=store)
    assert context.activity is not None
    handler = VerifiedExecutionHandler(process_host=RealPipeHost())
    original = ActionJournal.write
    posts = []
    from assurance_execution.operations import verified_execution

    original_post = verified_execution._post

    async def count_post(*args):
        posts.append(1)
        return await original_post(*args)

    monkeypatch.setattr(verified_execution, "_post", count_post)
    if cut == "before_dispatch":
        result = asyncio.run(handler.reconcile(request, context, context.activity.snapshot))
        assert result.status == "not_dispatched"
        assert posts == []
        return
    original_bind = context.activity.bind
    if cut == "dispatch_before_action":

        def crash_bind(reference):
            original_bind(reference)
            raise KeyboardInterrupt("cut after dispatch")

        monkeypatch.setattr(context.activity, "bind", crash_bind)
    else:
        record = {
            "http_before_terminal": "action_terminal",
            "before_seal": "outcome",
            "after_promotion": "never",
            "partial_action_terminal": "action_terminal",
            "partial_process_terminal": "process_terminal",
            "partial_outcome": "outcome",
        }[cut]

        def crash_write(self, name, payload):
            if name == record:
                if cut.startswith("partial_"):
                    import os

                    write = os.write
                    calls = 0

                    def partial(fd, value):
                        nonlocal calls
                        calls += 1
                        if calls == 2:
                            raise KeyboardInterrupt("cut after partial temporary write")
                        return write(fd, value[:7])

                    with monkeypatch.context() as patch:
                        patch.setattr(os, "write", partial)
                        return original(self, name, payload)
                raise KeyboardInterrupt("cut")
            return original(self, name, payload)

        monkeypatch.setattr(ActionJournal, "write", crash_write)
    if cut != "after_promotion":
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(handler.execute(request, context))
    else:
        asyncio.run(handler.execute(request, context))
    monkeypatch.setattr(ActionJournal, "write", original)
    if cut == "dispatch_before_action":
        monkeypatch.setattr(context.activity, "bind", original_bind)
    if store is not None:
        staged = store.seal(context.workspace_identity)
        promoted = store.promote(context.workspace_identity, staged)
        assert (context.project_root / manifest.evidence_root / "outcome.json").is_file()
        assert store.promote(context.workspace_identity, staged) == promoted
    promoted_files = (
        {
            str(path): path.read_bytes()
            for path in (context.project_root / manifest.evidence_root).rglob("*")
            if path.is_file()
        }
        if store is not None
        else None
    )
    recovered = asyncio.run(handler.reconcile(request, context, context.activity.snapshot))
    assert recovered.status == "terminal"
    assert recovered.outcome is not None
    assert VerifiedExecutionResultV1.model_validate(recovered.outcome.output).completion_status == (
        "collected" if cut in {"before_seal", "after_promotion", "partial_outcome"} else "incomplete"
    )
    again = asyncio.run(handler.reconcile(request, context, context.activity.snapshot))
    assert again.outcome == recovered.outcome
    assert len(posts) == (0 if cut == "dispatch_before_action" else 1)
    if store is not None:
        assert {
            str(path): path.read_bytes()
            for path in (context.project_root / manifest.evidence_root).rglob("*")
            if path.is_file()
        } == promoted_files
        store.close()


def test_managed_lifecycle_rejects_harness_source_drift(tmp_path):
    from assurance_execution.operations.managed_sut import ManagedUserSutHost

    host = ManagedUserSutHost(source_root=tmp_path, secret_port=Secrets({}))
    with pytest.raises(ValueError, match="NOT_READY"):
        host.prepare(workspace_root=tmp_path, project_dir=tmp_path / "project", run_root=tmp_path / "run")


def test_unbridged_pytest_pass_is_incomplete_and_secrets_never_leak(managed_sut):
    from assurance_execution.operations.verified_execution import VerifiedExecutionHandler

    request, context, manifest, _ = handler_case(
        managed_sut, "no_bridge", "def test_case():\n    assert True\n"
    )
    outcome = asyncio.run(VerifiedExecutionHandler(process_host=RealPipeHost()).execute(request, context))
    verified = VerifiedExecutionResultV1.model_validate(outcome.output)
    assert verified.completion_status == "incomplete"
    root = context.write_root / manifest.evidence_root
    terminal_path = root / "execution_terminal.json"
    assert verified.execution_authority_ref.digest == hashlib.sha256(terminal_path.read_bytes()).hexdigest()
    terminal = json.loads(terminal_path.read_bytes())
    assert terminal["record"] == "execution_terminal"
    assert terminal["payload"] == verified.model_dump(mode="json", exclude={"execution_authority_ref"})
    assert len(terminal["seal"]) == 64
    assert not (root / "action_started.json").exists()
    for path in root.rglob("*.json"):
        contents = path.read_text()
        assert "task4-host-only-canary" not in contents
        assert "host-password" not in contents
        assert managed_sut[3] not in contents


def test_current_pointer_tracks_only_the_latest_attempt(managed_sut):
    from assurance_execution.operations.verified_execution import VerifiedExecutionHandler

    handler = VerifiedExecutionHandler(process_host=RealPipeHost())
    results = []
    manifests = []
    for suffix in ("current-first", "current-second"):
        request, context, manifest, _ = handler_case(
            managed_sut, suffix, "def test_case():\n    assert True\n"
        )
        results.append(
            VerifiedExecutionResultV1.model_validate(asyncio.run(handler.execute(request, context)).output)
        )
        manifests.append(manifest)
    first, second = results
    assert first.execution_id != second.execution_id
    current = managed_sut[0] / "qa/changes/CH-USER-001/execution/execute-result.json"
    assert VerifiedExecutionResultV1.model_validate_json(current.read_bytes()) == second
    assert all((managed_sut[0] / manifest.evidence_root / "outcome.json").is_file() for manifest in manifests)


def test_recovery_rejects_unauthenticated_activity(managed_sut):
    from dataclasses import replace
    from assurance_execution.operations.verified_execution import VerifiedExecutionHandler

    request, context, _, _ = handler_case(managed_sut, "no_history")
    assert context.activity is not None
    activity = context.activity.snapshot
    result = asyncio.run(
        VerifiedExecutionHandler(process_host=RealPipeHost()).reconcile(
            request, replace(context, activity=None), activity
        )
    )
    assert result.status == "indeterminate"


def test_http_redirect_is_never_followed_and_started_is_durable(tmp_path, managed_sut):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from assurance_execution.contracts.verification import ManagedSutV1

    plan, manifest, journal, token = setup_action(tmp_path, managed_sut, "redirect")
    calls = []

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(self.path)
            assert journal.read("action_started") is not None
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body["username"] == manifest.inputs.username
            assert body["password"] == "parent-password"
            self.send_response(302)
            self.send_header("Location", "/unexpected-second-request")
            self.end_headers()

        def log_message(self, format: str, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manifest = manifest.model_copy(
        update={
            "sut": ManagedSutV1(
                instance_id=manifest.sut.instance_id,
                base_url=f"http://127.0.0.1:{server.server_port}",
                sqlite_path=manifest.sqlite.path,
            )
        }
    )
    journal = ActionJournal(tmp_path / "redirect-evidence", manifest, b"host-key" * 4)
    try:
        execute_frozen_action(
            plan, manifest, journal, json.dumps({"token": token, "user_password": "parent-password"}).encode()
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    terminal = journal.read("action_terminal")
    assert terminal is not None
    assert terminal["http"]["status"] == 302
    assert calls == ["/api/v1/user/create"]


def test_http_total_deadline_bounds_a_continuously_streaming_peer(tmp_path, managed_sut):
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from assurance_execution.contracts.verification import ManagedSutV1

    plan, manifest, _, token = setup_action(tmp_path, managed_sut, "stream")
    calls = []

    class SlowStream(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(self.path)
            self.send_response(200)
            self.end_headers()
            try:
                for _ in range(140):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format: str, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowStream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manifest = manifest.model_copy(
        update={
            "sut": ManagedSutV1(
                instance_id=manifest.sut.instance_id,
                base_url=f"http://127.0.0.1:{server.server_port}",
                sqlite_path=manifest.sqlite.path,
            )
        }
    )
    journal = ActionJournal(tmp_path / "stream-evidence", manifest, b"host-key" * 4)
    before = time.monotonic()
    try:
        execute_frozen_action(
            plan, manifest, journal, json.dumps({"token": token, "user_password": "parent-password"}).encode()
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert time.monotonic() - before < 12
    terminal = journal.read("action_terminal")
    assert terminal is not None
    assert terminal["http"]["state"] == "timeout"
    assert calls == ["/api/v1/user/create"]


@pytest.mark.parametrize("degradation", ["expired", "stopped"])
@pytest.mark.parametrize("method", ["reconcile", "cancel"])
def test_actual_semantic_handler_recovers_without_live_collector(
    managed_sut, tmp_path, monkeypatch, degradation, method
):
    from types import SimpleNamespace
    from datetime import datetime, timedelta, timezone
    from assurance_execution.contracts.agent import ExecutionPrepareInputV1
    from assurance_execution.contracts.readiness import ManagedSutReadinessSelectionV1
    from assurance_execution.operations.verified_attempt import VerifiedAttemptHandler
    from assurance_execution.operations.verified_execution import (
        VerifiedExecutionHandler,
        VerifiedExecutionInputV1,
    )
    from assurance_execution.operations import verified_attempt, verified_execution
    from assurance_generation.contracts.execution_plan import CaseExecutionPlanSetV1
    from tests.product.test_verified_readiness import collector_listener

    # Use the actual test listener fixture generator with this test's retained identity.
    listener = collector_listener(tmp_path)
    collector, process = next(listener)
    try:
        base, context, manifest, plan = handler_case(managed_sut, "semantic_" + degradation + method)
        assert isinstance(context.secrets, Secrets)
        secrets = context.secrets
        payload = VerifiedExecutionInputV1.model_validate(base.input)
        trace_fields = formal_plan("api_db_trace.v1").model_dump(mode="json")
        plan = type(plan).model_validate(
            plan.model_dump(mode="json")
            | {
                key: trace_fields[key]
                for key in ("validation_profile", "trace", "completion", "required", "bindings")
            }
        )
        plan_bytes = (
            CaseExecutionPlanSetV1(change_id=plan.change_id, cases=(plan,)).model_dump_json().encode()
        )
        (context.project_root / payload.verification.case_execution_plan_ref.path).write_bytes(plan_bytes)
        plan_ref = payload.verification.case_execution_plan_ref.model_copy(
            update={"digest": hashlib.sha256(plan_bytes).hexdigest()}
        )
        profile = payload.verification.model_copy(
            update={"validation_profile": "api_db_trace.v1", "case_execution_plan_ref": plan_ref}
        )
        manifest = manifest.model_copy(
            update={"validation_profile": "api_db_trace.v1", "case_execution_plan_digest": plan_ref.digest}
        )
        manifest_bytes = manifest.model_dump_json().encode()
        (context.project_root / payload.manifest_ref.path).write_bytes(manifest_bytes)
        payload = payload.model_copy(
            update={
                "verification": profile,
                "manifest_ref": payload.manifest_ref.model_copy(
                    update={"digest": hashlib.sha256(manifest_bytes).hexdigest()}
                ),
            }
        )
        selected = ManagedSutReadinessSelectionV1(
            workspace_root=str(context.project_root),
            verification=profile,
            configuration_digest="c" * 64,
            execution_id=manifest.execution_id,
            authorization_scope_digest=manifest.authorization_scope_digest,
            activity_receipt_digest=manifest.activity_receipt_digest,
        )
        collector.update(
            {
                "sut_instance_id": manifest.sut.instance_id,
                "execution_id": manifest.execution_id,
                "authorization_scope_digest": manifest.authorization_scope_digest,
                "activity_receipt_digest": manifest.activity_receipt_digest,
            }
        )
        response = json.dumps(
            {"probe_nonce": collector["probe_nonce"], "execution_id": manifest.execution_id}
        ).encode()
        (tmp_path / "response.json").write_bytes(response)
        collector["endpoint_response_digest"] = hashlib.sha256(response).hexdigest()
        secrets.values.update(
            {
                "sut.selection": selected.model_dump_json().encode(),
                "sut.collector": json.dumps(collector).encode(),
            }
        )
        root = ExecutionPrepareInputV1(
            change_id=plan.change_id,
            plan_ref=plan.plan_ref,
            plan_digest=plan.plan_digest,
            selected_test_families=("api",),
            capability_leafs=(),
            validation_profile="api_db_trace.v1",
            verification_config_digest="c" * 64,
            verification=profile,
        )

        request = base.model_copy(
            update={
                "input": root.model_dump(mode="json"),
                "binding_data": {
                    "user_host": {"sut_source_root": str(REPO)},
                    "readiness": {
                        "selection_handle": "sut.selection",
                        "authority_handle": "sut-authority",
                        "collector_handle": "sut.collector",
                        "configuration_digest": "c" * 64,
                        "validation_profile": "api_db_trace.v1",
                    },
                },
            }
        )
        # T3 prepare contracts are covered independently; this seam supplies its already authenticated output.
        prepared = SimpleNamespace(
            change_id=plan.change_id,
            batch_id=payload.view.batch_id,
            execution_id=manifest.execution_id,
            verification_manifest_ref=payload.manifest_ref,
            mapping=SimpleNamespace(selected=(manifest.nodeid,)),
            execution_view_digest=payload.view.digest,
            executed_at=payload.view.executed_at,
        )
        monkeypatch.setattr(verified_attempt, "assemble_execution_input", lambda *args, **kwargs: prepared)
        posts, cleanups = [], []
        original_post = verified_execution._post

        async def count_post(*args):
            posts.append(1)
            return await original_post(*args)

        monkeypatch.setattr(verified_execution, "_post", count_post)

        class Host(RealPipeHost):
            def run(self, **kwargs):
                return (
                    super()
                    .run(**kwargs)
                    .model_copy(
                        update={"cleanup_confirmed": False, "reason": "container_cleanup_unconfirmed"}
                    )
                )

            def stop(self, container_name):
                cleanups.append(container_name)
                return True

        handler = VerifiedAttemptHandler()
        handler._delegate = VerifiedExecutionHandler(process_host=Host())
        original_context = context
        # Make the receipt invalid before a fresh dispatch: it must never start.
        stale = {
            **collector,
            "issued_at": (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat(),
            "checked_at": (datetime.now(timezone.utc) - timedelta(seconds=40)).isoformat(),
            "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        }
        secrets.values["sut.collector"] = json.dumps(stale).encode()
        with pytest.raises(ValueError):
            asyncio.run(handler.execute(request, context))
        assert posts == []
        secrets.values["sut.collector"] = json.dumps(collector).encode()
        from graph_engine.attempts.activity import TaskActivityIndeterminate

        with pytest.raises(TaskActivityIndeterminate):
            asyncio.run(handler.execute(request, context))
        assert len(posts) == 1
        if degradation == "expired":
            secrets.values["sut.collector"] = json.dumps(stale).encode()
        else:
            process.terminate()
            process.wait(timeout=5)
        assert context.activity is not None
        forged = context.activity.snapshot.model_copy(update={"state": "prepared"})
        rejected = asyncio.run(getattr(handler, method)(request, context, forged))
        assert rejected.status == "indeterminate"
        recovered = asyncio.run(getattr(handler, method)(request, context, context.activity.snapshot))
        assert recovered.status == "terminal"
        assert recovered.outcome is not None
        assert (context.write_root / manifest.evidence_root / "cleanup_terminal.json").is_file()
        assert cleanups and len(posts) == 1
        bindings = cast(dict[str, Any], request.binding_data)
        altered = request.model_copy(
            update={
                "binding_data": {
                    **bindings,
                    "readiness": {
                        **dict(bindings["readiness"]),
                        "configuration_digest": "f" * 64,
                    },
                }
            }
        )
        try:
            result = asyncio.run(
                getattr(handler, method)(altered, original_context, context.activity.snapshot)
            )
            assert result.status == "indeterminate"
        except ValueError:
            pass
        assert len(posts) == 1
    finally:
        listener.close()


def test_default_verified_host_executes_real_bridge_without_oci(managed_sut, monkeypatch):
    from assurance_execution.operations.verified_execution import VerifiedExecutionHandler
    from assurance_execution.operations.verified_process import DockerVerificationHost
    import sys

    def unavailable(self):
        pytest.fail("business execution must not call Docker qualification")

    monkeypatch.setattr(DockerVerificationHost, "preflight", unavailable)
    monkeypatch.setenv("PATH", "/docker-and-colima-unavailable")
    request, context, _, _ = handler_case(managed_sut, "subprocess-default")
    request = request.model_copy(update={"binding_data": {}})
    outcome = asyncio.run(VerifiedExecutionHandler().execute(request, context))
    assert outcome.status == "succeeded"
    result = VerifiedExecutionResultV1.model_validate(outcome.output)
    assert result.evidence.state == "collected"
    receipt_path = context.write_root / result.evidence.receipt_ref.path
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt["payload"]["command"] == [sys.executable, "-m", "assurance_execution.bridge_runner"]


def test_subprocess_cleanup_uncertainty_cannot_be_promoted_by_noop_stop(managed_sut):
    from graph_engine.attempts.activity import TaskActivityIndeterminate
    from assurance_execution.operations.verified_execution import VerifiedExecutionHandler
    from assurance_execution.operations.verified_process import SubprocessVerificationHost

    class UnconfirmedHost(SubprocessVerificationHost):
        def run(self, **kwargs):
            return super().run(**kwargs).model_copy(update={"cleanup_confirmed": False})

    request, context, manifest, _ = handler_case(managed_sut, "uncertain-process-group")
    handler = VerifiedExecutionHandler(process_host=UnconfirmedHost())
    with pytest.raises(TaskActivityIndeterminate, match="cleanup"):
        asyncio.run(handler.execute(request, context))
    assert context.activity is not None
    result = asyncio.run(handler.reconcile(request, context, context.activity.snapshot))
    assert result.status == "indeterminate"
    assert not (context.write_root / manifest.evidence_root / "cleanup_terminal.json").exists()
    assert not (context.write_root / manifest.evidence_root / "outcome.json").exists()
