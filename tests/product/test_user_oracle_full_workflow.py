from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BENCHMARK = REPO / "benchmark/assurance-product"
FAULTS = (
    "none",
    "missing-binding",
    "no-bridge",
    "skip-oracle",
    "wrong-value",
    "rollback",
    "rollback-success",
    "db-unavailable",
    "wrong-environment",
    "unknown-http",
    "forged-evidence",
    "downgrade",
    "missing-write",
)


def load(name):
    spec = importlib.util.spec_from_file_location(name, BENCHMARK / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fault_cli_is_closed_and_frozen_before_prepare(tmp_path):
    harness = load("user_oracle_harness")
    for fault in FAULTS:
        args = harness._parser().parse_args(
            [
                "prepare",
                "--workspace-root",
                str(tmp_path),
                "--project-dir",
                str(tmp_path / "project"),
                "--run-root",
                str(tmp_path / "run"),
                "--fault",
                fault,
            ]
        )
        assert args.fault == fault
    with pytest.raises(SystemExit):
        harness._parser().parse_args(
            [
                "prepare",
                "--workspace-root",
                str(tmp_path),
                "--project-dir",
                str(tmp_path / "project"),
                "--run-root",
                str(tmp_path / "run"),
                "--fault",
                "random",
            ]
        )


@pytest.mark.parametrize(
    ("fault", "expected_rows", "expected_active"),
    [
        ("none", 1, 1),
        ("wrong-value", 1, 0),
        ("rollback-success", 0, None),
    ],
)
def test_real_user_create_observes_commit_and_transaction_rollback(
    tmp_path, monkeypatch, fault, expected_rows, expected_active
):
    import sqlite3
    from urllib.request import Request, urlopen

    harness = load("user_oracle_harness")
    for name in ("AA_SUT_ADMIN_PASSWORD", "AA_SUT_RESET_PASSWORD", "AA_SUT_SECRET_KEY"):
        monkeypatch.setenv(name, "task10-local-only-password")
    project, run = tmp_path / "project", tmp_path / "runtime"
    harness.prepare(workspace_root=tmp_path, project_dir=project, run_root=run, fault=fault)
    started = harness.start(workspace_root=tmp_path, prepare_receipt=run / "harness-prepare.json")
    frozen_start = (run / "owned-process.json").read_bytes()

    def post(path, payload, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["token"] = token
        with urlopen(
            Request(started["base_url"] + path, data=json.dumps(payload).encode(), headers=headers), timeout=5
        ) as response:
            return response.status, json.load(response)

    try:
        _, login = post(
            "/api/v1/base/access_token", {"username": "admin", "password": "task10-local-only-password"}
        )
        status, response = post(
            "/api/v1/user/create",
            {
                "username": "task10-user",
                "email": "task10@example.com",
                "password": "task10-local-only-password",
                "is_active": True,
                "is_superuser": False,
                "dept_id": None,
                "role_ids": [],
            },
            login["data"]["access_token"],
        )
        assert status == response["code"] == 200
        with sqlite3.connect(started["sqlite_path"]) as db:
            rows = db.execute("SELECT is_active FROM user WHERE username = ?", ("task10-user",)).fetchall()
        assert len(rows) == expected_rows
        if rows:
            assert rows[0][0] == expected_active
        if fault == "rollback-success":
            facts = [json.loads(line) for line in (run / "fault-facts.jsonl").read_text().splitlines()]
            assert [fact["event"] for fact in facts] == [
                "transaction_entered",
                "write_observed",
                "rollback_observed",
            ]
            assert facts[1]["row_count"] == 1
            assert facts[2]["row_count"] == 0
            assert facts[0]["connection_id"] == facts[1]["connection_id"]
    finally:
        harness.stop(
            workspace_root=tmp_path,
            receipt_path=run / "owned-process.json",
            instance_id=started["instance_id"],
        )
    assert (run / "owned-process.json").read_bytes() == frozen_start
    assert json.loads((run / "stopped-process.json").read_text())["state"] == "stopped"


@pytest.fixture
def locked_original_source(tmp_path, monkeypatch):
    import hashlib

    harness = load("user_oracle_harness")
    source = tmp_path / "source"
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    files = {}
    for member in ("app/controllers/user.py", "migrations/schema.py", "run.py"):
        path = source / member
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# locked original source\n")
        files[member] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    (fixture / "original-source-lock.json").write_text(json.dumps({"files": files}))
    monkeypatch.setattr(harness, "FIXTURE_ROOT", fixture)
    assert harness.verify_original_source(source) == files
    return harness, source


def test_source_preflight_rejects_original_sut_drift(locked_original_source):
    harness, source = locked_original_source
    (source / "app/controllers/user.py").write_text("# drift\n")
    with pytest.raises(ValueError, match="source.*drift"):
        harness.verify_original_source(source)


def test_attempt_authority_files_are_immutable_and_separate(tmp_path):
    from assurance_execution.operations.user_attempt import retain_authority, read_retained_authority
    from assurance_execution.contracts.verification import ManagedSutAuthorityV1
    from tests.verified_generation_fixture import accepted_verified_execution_input

    project = tmp_path / "project"
    project.mkdir()
    accepted_verified_execution_input(project)
    host = tmp_path / "host"
    host.mkdir(mode=0o700)
    document = {
        "run_root": str(project / "runtime"),
        "ownership_token": {
            "path": str(project / "runtime/.ownership-token"),
            "device": 1,
            "inode": 1,
            "digest": "sha256:" + "a" * 64,
        },
        "prepare_receipt_digest": "b" * 64,
        "start_receipt_digest": "c" * 64,
        "authorization_scope_digest": "d" * 64,
        "activity_receipt_digest": "e" * 64,
    }
    first = ManagedSutAuthorityV1.model_validate(document)
    second = first.model_copy(update={"activity_receipt_digest": "f" * 64})
    a, b = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    retain_authority(host, a, first, project_root=project)
    retain_authority(host, b, second, project_root=project)
    assert read_retained_authority(host, a) == first
    assert read_retained_authority(host, b) == second
    with pytest.raises((FileExistsError, ValueError)):
        retain_authority(host, a, second, project_root=project)
    with pytest.raises(ValueError, match="outside"):
        retain_authority(project, a, first, project_root=project)


def test_one_invocation_prepares_independent_sut_attempts(tmp_path):
    from assurance_execution.operations.user_attempt import start_user_attempt
    from tests.verified_generation_fixture import accepted_verified_execution_input
    from graph_engine.attempts import AttemptKey

    project = tmp_path / "project"
    harness = load("user_oracle_harness")
    selected = harness.materialize_project(project_dir=project)
    root = accepted_verified_execution_input(
        project, reviewed_source_path="app/controllers/user.py"
    ).model_copy(update={"verification": None})
    host = tmp_path / "host"
    host.mkdir(mode=0o700)

    class Secrets:
        def resolve(self, handle):
            if handle == "sut.authority":
                return json.dumps(
                    {
                        "kind": "user-invocation-host.v1",
                        "authority_root": str(host),
                        "fault": "none",
                        "frozen_artifact_ref": {
                            "path": ".aa/user-oracle/runtime-lock.json",
                            "digest": selected["frozen_artifact_digest"].removeprefix("sha256:"),
                        },
                    }
                ).encode()
            return b"task10-local-only-password"

    attempts = []
    for char in ("a", "b"):
        owned = start_user_attempt(
            root,
            source_root=REPO,
            workspace_root=project,
            attempt_key=AttemptKey(digest=char * 64),
            invocation_id="same-invocation",
            task_id=char * 64,
            graph_instance_id="graph",
            node_id="execution.execute",
            authorization_scope_digest=char * 64,
            secrets=Secrets(),
            authority_handle="sut.authority",
            credential_handle="sut.credential",
        )
        try:
            attempts.append(owned.verification)
            assert owned.verification.sut_base_url.startswith("http://127.0.0.1:")
            assert owned.verification.sut_base_url != "http://127.0.0.1:9999"
            assert json.loads(owned.secrets.resolve("sut.credential"))["token"]
        finally:
            owned.stop()
    assert attempts[0].sut_instance_id != attempts[1].sut_instance_id
    assert attempts[0].managed_sqlite_path != attempts[1].managed_sqlite_path
    assert len(list(host.glob("*.json"))) == 2


@pytest.mark.parametrize("member", ["app", "migrations"])
def test_source_preflight_rejects_symlinked_source_directory(locked_original_source, member):
    harness, source = locked_original_source
    directory = source / member
    outside = source.parent / f"original-{member}"
    directory.rename(outside)
    directory.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="source"):
        harness.verify_original_source(source)


@pytest.mark.parametrize("member", ["app/extra", "migrations/extra", "app/__pycache__"])
def test_source_preflight_rejects_extra_directory_symlinks(locked_original_source, member):
    harness, source = locked_original_source
    outside = source.parent / "outside"
    outside.mkdir()
    (source / member).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="source"):
        harness.verify_original_source(source)


def test_attempt_rejects_stale_generation_before_starting_http(tmp_path, monkeypatch):
    from assurance_execution.operations.user_attempt import start_user_attempt
    from assurance_execution.operations.managed_sut import ManagedUserSutHost
    from tests.verified_generation_fixture import accepted_verified_execution_input
    from graph_engine.attempts import AttemptKey

    project = tmp_path / "project"
    project.mkdir()
    root = accepted_verified_execution_input(project).model_copy(update={"verification": None})
    assert root.generation_result is not None
    root = root.model_copy(
        update={"generation_result": root.generation_result.model_copy(update={"source_refs": ()})}
    )
    host = tmp_path / "host"
    host.mkdir(mode=0o700)

    class Secrets:
        def resolve(self, handle):
            return json.dumps(
                {
                    "kind": "user-invocation-host.v1",
                    "authority_root": str(host),
                    "fault": "none",
                    "frozen_artifact_ref": {"path": ".aa/user-oracle/runtime-lock.json", "digest": "a" * 64},
                }
            ).encode()

    def forbidden(*args, **kwargs):
        pytest.fail("stale generation reached SUT startup")

    monkeypatch.setattr(ManagedUserSutHost, "prepare", forbidden)
    with pytest.raises(ValueError, match="generation"):
        start_user_attempt(
            root,
            source_root=REPO,
            workspace_root=project,
            attempt_key=AttemptKey(digest="a" * 64),
            invocation_id="invocation",
            task_id="task",
            graph_instance_id="graph",
            node_id="execute",
            authorization_scope_digest="b" * 64,
            secrets=Secrets(),
            authority_handle="sut.authority",
            credential_handle="sut.credential",
        )


def test_generation_adapter_preserves_verified_profile_and_reviewed_references():
    from assurance_product.graphs.execute import adapt_generation
    from tests.product.test_product_input import valid_product_input

    state = valid_product_input(validation_profile="api_db.v1", verification_config_digest="a" * 64)
    state.update(
        {
            "reviewed_case": {"approved": True},
            "source_artifacts": [{"path": "sources", "digest": "b" * 64}],
            "plan_digest": "c" * 64,
            "plan_ref": {"path": "plan", "digest": "d" * 64},
        }
    )
    projected = adapt_generation(state)  # pyright: ignore[reportArgumentType]
    assert projected["validation_profile"] == "api_db.v1"
    assert projected["reviewed_case"] == state["reviewed_case"]
    assert projected["source_artifacts"] == state["source_artifacts"]
    assert projected["plan_ref"] == state["plan_ref"]
    assert projected["plan_digest"] == state["plan_digest"]
    assert "case_plan_context" not in projected


def test_attempt_rejects_reviewed_a_with_functional_frozen_b_before_any_post(tmp_path, monkeypatch):
    import httpx
    from graph_engine.attempts import AttemptKey
    from assurance_execution.operations.user_attempt import start_user_attempt
    from tests.verified_generation_fixture import accepted_verified_execution_input

    project = tmp_path / "project"
    selected = load("user_oracle_harness").materialize_project(project_dir=project)
    source = project / "app/controllers/user.py"
    source.write_bytes(source.read_bytes() + b"\n# reviewed source A differs from frozen B\n")
    root = accepted_verified_execution_input(
        project, reviewed_source_path="app/controllers/user.py"
    ).model_copy(update={"verification": None})
    host = tmp_path / "host"
    host.mkdir(mode=0o700)

    class Secrets:
        def resolve(self, handle):
            if handle == "sut.authority":
                return json.dumps(
                    {
                        "kind": "user-invocation-host.v1",
                        "authority_root": str(host),
                        "fault": "none",
                        "frozen_artifact_ref": {
                            "path": ".aa/user-oracle/runtime-lock.json",
                            "digest": selected["frozen_artifact_digest"].removeprefix("sha256:"),
                        },
                    }
                ).encode()
            return b"task10-local-only-password"

    def forbidden_post(*args, **kwargs):
        pytest.fail("unreviewed runtime reached HTTP POST")

    monkeypatch.setattr(httpx.Client, "post", forbidden_post)
    with pytest.raises(ValueError, match="NOT_READY: reviewed source"):
        start_user_attempt(
            root,
            source_root=REPO,
            workspace_root=project,
            attempt_key=AttemptKey(digest="e" * 64),
            invocation_id="invocation",
            task_id="task",
            graph_instance_id="graph",
            node_id="execute",
            authorization_scope_digest="a" * 64,
            secrets=Secrets(),
            authority_handle="sut.authority",
            credential_handle="sut.credential",
        )
    assert not list(project.glob(".aa/managed-user/*/runtime/owned-process.json"))
    assert not list(host.glob("*.json"))
