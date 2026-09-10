from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import cast

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
BENCHMARK = REPO / "benchmark/assurance-product"
FAULTS = (
    "none",
    "missing-binding",
    "no-bridge",
    "no-action",
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
    "drop-business-span",
    "drop-write-span",
    "broken-context",
    "stale-trace",
    "drain-timeout",
    "early-completed",
    "refactor",
)
TRACE_FAULT_MATRIX = (
    ("drop-business-span", "INCOMPLETE", "quality.report", True),
    ("drop-write-span", "INCOMPLETE", "quality.report", True),
    ("broken-context", "INCOMPLETE", "quality.report", True),
    ("stale-trace", "INCOMPLETE", "quality.report", True),
    ("drain-timeout", "INCOMPLETE", "quality.report", True),
    ("early-completed", "FAILED", "quality.inspect", True),
    ("refactor", "PASSED", "quality.report", True),
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
    project.chmod(0o700)
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

    from urllib.error import URLError
    from urllib.request import urlopen

    attempts = []
    previous_url = None
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
            if previous_url is not None:
                with pytest.raises(URLError):
                    urlopen(previous_url + "/api/v1/base/access_token", timeout=1)
            attempts.append(owned.verification)
            assert owned.verification.sut_base_url.startswith("http://127.0.0.1:")
            assert owned.verification.sut_base_url != "http://127.0.0.1:9999"
            assert json.loads(owned.secrets.resolve("sut.credential"))["token"]
        finally:
            previous_url = owned.verification.sut_base_url
            owned.stop()
    assert attempts[0].sut_instance_id != attempts[1].sut_instance_id
    assert attempts[0].managed_sqlite_path != attempts[1].managed_sqlite_path
    assert attempts[0].sut_base_url != attempts[1].sut_base_url
    assert len(list(host.glob("*.json"))) == 2
    with pytest.raises(URLError):
        urlopen(attempts[1].sut_base_url + "/api/v1/base/access_token", timeout=1)


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


def test_prepare_adapter_locks_assertion_sources_for_verified_profile():
    from assurance_product.graphs.entrypoints import adapt_case, adapt_prepare
    from tests.product.test_product_input import valid_product_input

    case_path = "qa/changes/CH-DEMO-001/cases/system/user/case.yaml"
    source_path = "qa/changes/CH-DEMO-001/cases/system/user/assertion-sources.json"
    state = valid_product_input(
        validation_profile="api_db.v1",
        verification_config_digest="a" * 64,
        case_delta_paths=[case_path],
        candidate_test_families=["api"],
    )
    prepared = adapt_prepare(state)  # pyright: ignore[reportArgumentType]
    prepared_feature = cast(dict[str, object], prepared["feature_input"])
    assert prepared["assertion_source_paths"] == [source_path]
    assert prepared_feature["assertion_source_paths"] == [source_path]

    state.update(
        {
            "plan_digest": "c" * 64,
            "plan_ref": {"path": "plan", "digest": "d" * 64},
            "selected_test_families": ["api"],
        }
    )
    cased = adapt_case(state)  # pyright: ignore[reportArgumentType]
    cased_feature = cast(dict[str, object], cased["feature_input"])
    assert cased["assertion_source_paths"] == [source_path]
    assert cased_feature["assertion_source_paths"] == [source_path]
    from typing import get_type_hints

    from assurance_product.graphs.state import ProductState

    assert "assertion_source_paths" in get_type_hints(ProductState)


def test_prepare_adapter_omits_assertion_sources_for_legacy_profile():
    from assurance_product.graphs.entrypoints import adapt_prepare
    from tests.product.test_product_input import valid_product_input

    state = valid_product_input(
        case_delta_paths=["qa/changes/CH-DEMO-001/cases/system/user/case.yaml"],
        candidate_test_families=["api"],
    )
    prepared = adapt_prepare(state)  # pyright: ignore[reportArgumentType]
    prepared_feature = cast(dict[str, object], prepared["feature_input"])
    assert "assertion_source_paths" not in prepared
    assert "assertion_source_paths" not in prepared_feature


def test_verified_manifest_item_is_api_only_and_product_input_uses_candidates():
    runner = load("run_item")
    document = json.loads((BENCHMARK / "manifest.json").read_text(encoding="utf-8"))
    item = runner._manifest_item(document, "opencode-user-api-db", "opencode")
    assert item["entrypoint"] == "full"
    assert item["run_mode"] == "case"
    assert item["case_modules"] == ["system/user"]
    assert item["selected_test_families"] == ["api"]
    assert item["validation_profile"] == "api_db.v1"
    assert item["sut_item_id"] == "RET-user-management"
    requirement = (REPO / item["requirement_path"]).read_text(encoding="utf-8")
    assert "RET-user-management" in requirement
    assert "仅覆盖" in requirement
    assert (
        '"candidate_test_families": list(arguments["selected_test_families"])' in runner._WRITE_PRODUCT_INPUT
    )
    assert (
        '"selected_test_families": list(arguments["selected_test_families"])'
        not in runner._WRITE_PRODUCT_INPUT
    )


def test_verified_manifest_rejects_non_api_families_and_stopped_required_nodes():
    runner = load("run_item")
    document = json.loads((BENCHMARK / "manifest.json").read_text(encoding="utf-8"))
    user = next(item for item in document["items"] if item["id"] == "opencode-user-api-db")
    invalid = {**user, "selected_test_families": ["api", "e2e"]}
    with pytest.raises(SystemExit, match="API-only"):
        runner._manifest_item({"items": [invalid]}, "opencode-user-api-db", "opencode")
    assert (
        runner._status_steps(
            {
                "graph_hierarchy": [{"graph_instance_id": "inspect", "graph_id": "quality.inspect"}],
                "node_states": [
                    {
                        "graph_instance_id": "inspect",
                        "node_id": "finalize",
                        "state": "stopped",
                    }
                ],
            }
        )
        == ()
    )


def test_user_project_is_api_only_empty_change_with_no_frontend(tmp_path):
    runner = load("run_item")
    project = tmp_path / "project"
    selected = runner._prepare_user_project(repo=REPO, project_dir=project, fault="none")

    assert not (project / "web").exists()
    assert not (project / "tests").exists()
    assert not (project / "qa/changes").exists()
    assert (project / "app/models/admin.py").is_file()
    policy = yaml.safe_load((project / ".aa/policy.yaml").read_text(encoding="utf-8"))
    assert policy["test_family_policy"] == {"required": ["api"], "allowed": ["api"]}
    assert selected["fault"] == "none"


def test_user_configuration_and_host_binding_are_fixed_without_oci(tmp_path):
    import shutil

    runner = load("run_item")
    project = tmp_path / "project"
    runner._prepare_user_project(repo=REPO, project_dir=project, fault="none")
    tree = tmp_path / "config-tree"
    shutil.copytree(REPO / "tests/product/fixtures/project-config", tree)
    runner._prepare_project_config_tree(tree, project)
    plugin = yaml.safe_load((tree / "plugin.yaml").read_text(encoding="utf-8"))
    resources = {item["resource_id"] for item in plugin["files"]}
    assert "assurance.product.configuration.verification-policy" in resources
    from graph_engine.composition import ConfigTreePluginSource
    from assurance_product.configuration import load_project_configuration

    contribution = load_project_configuration(ConfigTreePluginSource(path=tree))
    published = {resource.resource_id: resource for resource in contribution.resources}
    assert yaml.safe_load(published["assurance.product.configuration.verification-policy"].content) == {
        "validation_profile": "api_db.v1"
    }
    published_policy = yaml.safe_load(published["assurance.product.configuration.product-policy"].content)
    assert published_policy["coverage_floor_by_tier"] == {
        "low": 0.7,
        "medium": 0.8,
        "high": 0.9,
        "critical": 1.0,
    }
    assert published_policy["evidence_sufficiency"] == {
        "recency_hours": 24,
        "require_current_batch": True,
    }

    item = dict(
        runner._manifest_item(
            json.loads((BENCHMARK / "manifest.json").read_text(encoding="utf-8")),
            "opencode-user-api-db",
            "opencode",
        )
    )
    output = tmp_path / "output"
    output.mkdir()
    runner._configure_user_host(repo=REPO, project=project, output=output, item=item, fault="none")
    assert item["verification_host"] == {
        "sut_source_root": str(REPO),
        "managed_sut_authority_handle": "sut.authority",
        "managed_sut_readiness_handle": None,
        "credential_handle": "sut.credential",
        "collector_readiness_handle": None,
    }
    assert "runner" not in item["verification_host"]
    deployment = tmp_path / "deployment.json"
    runner._write_deployment_manifest(deployment, item, project_scope=str(project), adapter="opencode")
    document = json.loads(deployment.read_text(encoding="utf-8"))
    assert document["validation_profile"] == "api_db.v1"
    assert document["verification_host"] == item["verification_host"]
    assert {"sut.authority", "sut.credential"} <= set(document["secret_handles"])
    assert "qualification" not in json.dumps(document)


def test_user_fault_parser_includes_runtime_no_action(tmp_path):
    runner = load("run_item")
    parser = runner._parser()
    args = parser.parse_args(
        [
            "--item",
            "opencode-user-api-db",
            "--adapter",
            "opencode",
            "--output",
            str(tmp_path),
            "--fault",
            "no-action",
        ]
    )
    assert args.fault == "no-action"


@pytest.mark.parametrize(
    ("fault", "verdict"),
    (
        ("no-action", "INCOMPLETE"),
        ("skip-oracle", "INCOMPLETE"),
        ("db-unavailable", "INCOMPLETE"),
        ("wrong-value", "FAILED"),
        ("rollback-success", "FAILED"),
    ),
)
def test_fault_outcome_accepts_only_expected_non_delivery(fault, verdict):
    runner = load("run_item")
    assert runner._fault_outcome_errors(fault=fault, verdict=verdict, achieved=False, published=False) == []
    assert runner._fault_outcome_errors(fault=fault, verdict="PASSED", achieved=True, published=True)


def test_no_bridge_fault_is_generation_admission_not_runtime_a03():
    runner = load("run_item")
    change_id = "CH-USER-NO-BRIDGE"
    prefix = (
        "intake.intake",
        "intake.explore",
        "intake.case-design",
        "intake.case-review",
        "generation.api.plan",
        "generation.api.plan-review",
    )
    graphs = [
        {"graph_instance_id": f"g-{step}", "graph_id": step} for step in (*prefix, "generation.api.codegen")
    ]
    nodes = [
        {
            "graph_instance_id": f"g-{step}",
            "node_id": f"{step}/finalize",
            "state": "succeeded",
        }
        for step in prefix
    ]
    nodes.append(
        {
            "graph_instance_id": "g-generation.api.codegen",
            "node_id": "generation.api.codegen/finalize",
            "state": "failed",
        }
    )
    status = {
        "invocation_id": change_id,
        "status": "failed",
        "terminal_reason": "generation_bridge_missing",
        "selected_test_families": ["api"],
        "change": {"change_id": change_id, "state": "failed"},
        "graph_hierarchy": graphs,
        "node_states": nodes,
        "execution_gate": None,
        "quality_gate": None,
        "publication": {"status": "not_ready"},
    }
    item = runner._manifest_item(
        json.loads((BENCHMARK / "manifest.json").read_text(encoding="utf-8")),
        "opencode-user-api-db",
        "opencode",
    )
    assert runner._fault_result_errors(fault="no-bridge", item=item, status=status, change_id=change_id) == []
    runtime_a03 = {
        **status,
        "status": "completed",
        "terminal_reason": "verification_incomplete",
        "change": {"change_id": change_id, "state": "stopped"},
        "execution_gate": {
            "validation_profile": "api_db.v1",
            "execution_receipt_id": "execution-receipt",
            "execution_receipt_digest": "a" * 64,
            "batch_id": "batch-current",
        },
        "quality_gate": {
            "inspection": {
                "verification_status": "INCOMPLETE",
                "verification_ref": {"path": "verification.json", "digest": "b" * 64},
                "batch_id": "batch-current",
            }
        },
    }
    assert runner._fault_result_errors(fault="no-bridge", item=item, status=runtime_a03, change_id=change_id)


def test_no_action_requires_runtime_verification_material():
    runner = load("run_item")
    change_id = "CH-USER-NO-ACTION"
    item = runner._manifest_item(
        json.loads((BENCHMARK / "manifest.json").read_text(encoding="utf-8")),
        "opencode-user-api-db",
        "opencode",
    )
    required = tuple(item["required_steps"])
    status = {
        "invocation_id": change_id,
        "status": "failed",
        "terminal_reason": "verification_incomplete",
        "selected_test_families": ["api"],
        "change": {"change_id": change_id, "state": "failed"},
        "graph_hierarchy": [{"graph_instance_id": f"g-{step}", "graph_id": step} for step in required],
        "node_states": [
            {
                "graph_instance_id": f"g-{step}",
                "node_id": f"{step}/finalize",
                "state": "succeeded",
            }
            for step in required
        ],
        "execution_gate": {
            "validation_profile": "api_db.v1",
            "execution_receipt_id": "execution-receipt",
            "execution_receipt_digest": "a" * 64,
            "batch_id": "batch-current",
        },
        "quality_gate": {
            "inspection": {
                "verification_status": "INCOMPLETE",
                "verification_ref": {"path": "verification.json", "digest": "b" * 64},
                "batch_id": "batch-current",
            }
        },
        "publication": {"status": "not_ready"},
    }
    assert runner._fault_result_errors(fault="no-action", item=item, status=status, change_id=change_id) == []
    admission = {
        **status,
        "status": "failed",
        "terminal_reason": "generation_bridge_missing",
        "change": {"change_id": change_id, "state": "failed"},
        "execution_gate": None,
        "quality_gate": None,
    }
    assert runner._fault_result_errors(fault="no-action", item=item, status=admission, change_id=change_id)


def test_missing_binding_fault_requires_its_generation_admission_boundary():
    runner = load("run_item")
    change_id = "CH-USER-MISSING-BINDING"
    prefix = ("intake.intake", "intake.explore", "intake.case-design", "intake.case-review")
    graphs = [
        {"graph_instance_id": f"g-{step}", "graph_id": step} for step in (*prefix, "generation.api.plan")
    ]
    nodes = [
        {
            "graph_instance_id": f"g-{step}",
            "node_id": f"{step}/finalize",
            "state": "succeeded",
        }
        for step in prefix
    ]
    nodes.append(
        {
            "graph_instance_id": "g-generation.api.plan",
            "node_id": "generation.api.plan/finalize",
            "state": "failed",
        }
    )
    status = {
        "invocation_id": change_id,
        "status": "failed",
        "terminal_reason": "generation_binding_missing",
        "selected_test_families": ["api"],
        "change": {"change_id": change_id, "state": "failed"},
        "graph_hierarchy": graphs,
        "node_states": nodes,
        "execution_gate": None,
        "quality_gate": None,
        "publication": {"status": "not_ready"},
    }
    item = runner._manifest_item(
        json.loads((BENCHMARK / "manifest.json").read_text(encoding="utf-8")),
        "opencode-user-api-db",
        "opencode",
    )

    assert (
        runner._fault_result_errors(fault="missing-binding", item=item, status=status, change_id=change_id)
        == []
    )
    assert runner._fault_result_errors(
        fault="missing-binding",
        item=item,
        status={**status, "node_states": []},
        change_id=change_id,
    )


def test_db_unavailable_requires_runtime_verification_material():
    runner = load("run_item")
    change_id = "CH-USER-DB-UNAVAILABLE"
    item = runner._manifest_item(
        json.loads((BENCHMARK / "manifest.json").read_text(encoding="utf-8")),
        "opencode-user-api-db",
        "opencode",
    )
    required = tuple(item["required_steps"])
    status = {
        "invocation_id": change_id,
        "status": "failed",
        "terminal_reason": "verification_incomplete",
        "selected_test_families": ["api"],
        "change": {"change_id": change_id, "state": "failed"},
        "graph_hierarchy": [{"graph_instance_id": f"g-{step}", "graph_id": step} for step in required],
        "node_states": [
            {
                "graph_instance_id": f"g-{step}",
                "node_id": f"{step}/finalize",
                "state": "succeeded",
            }
            for step in required
        ],
        "execution_gate": {
            "validation_profile": "api_db.v1",
            "execution_receipt_id": "execution-receipt",
            "execution_receipt_digest": "a" * 64,
            "batch_id": "batch-current",
        },
        "quality_gate": {
            "inspection": {
                "verification_status": "INCOMPLETE",
                "verification_ref": {"path": "verification.json", "digest": "b" * 64},
                "batch_id": "batch-current",
            }
        },
        "publication": {"status": "not_ready"},
    }

    assert (
        runner._fault_result_errors(fault="db-unavailable", item=item, status=status, change_id=change_id)
        == []
    )
    pre_execution = {
        **status,
        "status": "failed",
        "terminal_reason": "database_unavailable",
        "change": {"change_id": change_id, "state": "failed"},
        "node_states": status["node_states"][:-3],
        "execution_gate": None,
        "quality_gate": None,
    }
    assert runner._fault_result_errors(
        fault="db-unavailable", item=item, status=pre_execution, change_id=change_id
    )


def test_verification_verdict_ignores_top_level_status():
    runner = load("run_item")
    status = {
        "change": {"state": "failed"},
        "execution_gate": {
            "validation_profile": "api_db_trace.v1",
            "execution_receipt_id": "execution-receipt",
            "execution_receipt_digest": "a" * 64,
            "batch_id": "batch-current",
        },
        "quality_gate": None,
        "verification_status": "PASSED",
    }
    assert runner._verification_verdict(status) is None


def test_verification_verdict_reads_quality_gate_artifact(tmp_path):
    import hashlib

    runner = load("run_item")
    stray = tmp_path / "qa/changes/CH-USER/verification.json"
    stray.parent.mkdir(parents=True)
    stray.write_text(json.dumps({"verdict": "PASSED"}), encoding="utf-8")
    artifact = tmp_path / "qa/changes/CH-USER/inspect/verification.json"
    artifact.parent.mkdir(parents=True)
    payload = json.dumps({"verdict": "INCOMPLETE"}).encode()
    artifact.write_bytes(payload)
    status = {
        "change": {"state": "failed"},
        "execution_gate": {"batch_id": "batch-current"},
        "quality_gate": {
            "inspection": {
                "verification_status": "PASSED",
                "verification_ref": {
                    "path": "qa/changes/CH-USER/inspect/verification.json",
                    "digest": hashlib.sha256(payload).hexdigest(),
                },
            }
        },
        "verification_status": "PASSED",
    }
    assert runner._verification_verdict(status, project=tmp_path) == "INCOMPLETE"


def test_runtime_fault_fails_without_quality_gate():
    runner = load("run_item")
    change_id = "CH-USER-EARLY-COMPLETED"
    item = runner._manifest_item(
        json.loads((BENCHMARK / "manifest.json").read_text(encoding="utf-8")),
        "opencode-user-api-db-trace",
        "opencode",
    )
    required = tuple(item["required_steps"])
    prefix = required[: required.index("quality.inspect") + 1]
    status = {
        "invocation_id": change_id,
        "status": "failed",
        "terminal_reason": "needs_human",
        "selected_test_families": list(item["selected_test_families"]),
        "change": {"change_id": change_id, "state": "failed"},
        "graph_hierarchy": [{"graph_instance_id": f"g-{step}", "graph_id": step} for step in prefix],
        "node_states": [
            {
                "graph_instance_id": f"g-{step}",
                "node_id": f"{step}/finalize",
                "state": "succeeded",
            }
            for step in prefix
        ],
        "execution_gate": {
            "validation_profile": item["validation_profile"],
            "execution_receipt_id": "execution-receipt",
            "execution_receipt_digest": "a" * 64,
            "batch_id": "batch-current",
        },
        "quality_gate": None,
        "verification_status": "FAILED",
        "publication": {"status": "not_ready"},
    }
    errors = runner._fault_result_errors(
        fault="early-completed", item=item, status=status, change_id=change_id
    )
    assert errors
    assert any("quality verification material" in error for error in errors)


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


def test_trace_manifest_item_uses_same_user_spec_and_trace_profile():
    runner = load("run_item")
    document = json.loads((BENCHMARK / "manifest.json").read_text(encoding="utf-8"))
    stage1 = runner._manifest_item(document, "opencode-user-api-db", "opencode")
    stage2 = runner._manifest_item(document, "opencode-user-api-db-trace", "opencode")
    assert stage2["validation_profile"] == "api_db_trace.v1"
    assert stage1["validation_profile"] == "api_db.v1"
    assert stage2["requirement_path"] == stage1["requirement_path"]
    assert stage2["sut_item_id"] == stage1["sut_item_id"] == "RET-user-management"
    assert stage2["selected_test_families"] == stage1["selected_test_families"] == ["api"]
    assert stage2["case_modules"] == stage1["case_modules"] == ["system/user"]
    assert stage2["required_steps"] == stage1["required_steps"]
    assert stage2["entrypoint"] == "full"
    assert stage2["run_mode"] == "case"


def test_trace_item_writes_only_api_db_trace_policy(tmp_path):
    runner = load("run_item")
    project = tmp_path / "project"
    runner._prepare_user_project(
        repo=REPO, project_dir=project, fault="none", validation_profile="api_db_trace.v1"
    )
    assert (project / ".aa/verification-policy.yaml").read_text(encoding="utf-8") == (
        "validation_profile: api_db_trace.v1\n"
    )
    tree = tmp_path / "config-tree"
    import shutil

    shutil.copytree(REPO / "tests/product/fixtures/project-config", tree)
    runner._prepare_project_config_tree(tree, project)
    from graph_engine.composition import ConfigTreePluginSource
    from assurance_product.configuration import load_project_configuration

    contribution = load_project_configuration(ConfigTreePluginSource(path=tree))
    published = {resource.resource_id: resource for resource in contribution.resources}
    assert yaml.safe_load(published["assurance.product.configuration.verification-policy"].content) == {
        "validation_profile": "api_db_trace.v1"
    }


def test_trace_host_binds_collector_handle_without_oci(tmp_path):
    runner = load("run_item")
    project = tmp_path / "project"
    runner._prepare_user_project(
        repo=REPO, project_dir=project, fault="none", validation_profile="api_db_trace.v1"
    )
    item = dict(
        runner._manifest_item(
            json.loads((BENCHMARK / "manifest.json").read_text(encoding="utf-8")),
            "opencode-user-api-db-trace",
            "opencode",
        )
    )
    output = tmp_path / "output"
    output.mkdir()
    runner._configure_user_host(repo=REPO, project=project, output=output, item=item, fault="none")
    assert item["verification_host"]["collector_readiness_handle"] == "sut.collector"
    assert "runner" not in item["verification_host"]
    collector_args = [
        item["host_secret_args"][index + 1]
        for index, flag in enumerate(item["host_secret_args"])
        if flag == "--secret" and item["host_secret_args"][index + 1].startswith("sut.collector=")
    ]
    assert collector_args
    collector_path = Path(collector_args[0].split("=", 1)[1].removeprefix("file:"))
    from assurance_execution.operations.readiness import (
        CollectorQualification,
        authenticate_collector_artifacts,
    )

    qualification = CollectorQualification.model_validate_json(collector_path.read_bytes())
    assert qualification.validation_profile == "api_db_trace.v1"
    assert qualification.configuration_digest == runner._verification_config_digest(
        validation_profile="api_db_trace.v1", host=item["verification_host"]
    )

    class _Secrets:
        def resolve(self, handle):
            assert handle == "sut.collector"
            return collector_path.read_bytes()

    authenticate_collector_artifacts(_Secrets(), "sut.collector", qualification.configuration_digest)
    deployment = tmp_path / "deployment.json"
    runner._write_deployment_manifest(deployment, item, project_scope=str(project), adapter="opencode")
    document = json.loads(deployment.read_text(encoding="utf-8"))
    assert document["validation_profile"] == "api_db_trace.v1"
    assert document["verification_host"]["collector_readiness_handle"] == "sut.collector"
    assert "sut.collector" in document["secret_handles"]
    assert "qualification" not in json.dumps(document)


@pytest.mark.parametrize(("fault", "verdict", "prefix_end", "verified"), TRACE_FAULT_MATRIX)
def test_trace_fault_matrix_records_stop_and_verdict(fault, verdict, prefix_end, verified):
    runner = load("run_item")
    expected = runner._FAULT_EXPECTATIONS[fault]
    assert expected["verdict"] == verdict
    assert expected["prefix_end"] == prefix_end
    assert expected["verified_material"] is verified
    if verdict == "PASSED":
        assert (
            runner._fault_outcome_errors(fault=fault, verdict="PASSED", achieved=True, published=True) == []
        )
        assert runner._fault_outcome_errors(
            fault=fault, verdict="INCOMPLETE", achieved=False, published=False
        )
    else:
        assert (
            runner._fault_outcome_errors(fault=fault, verdict=verdict, achieved=False, published=False) == []
        )
        assert runner._fault_outcome_errors(fault=fault, verdict="PASSED", achieved=True, published=True)


def test_refactor_freezes_new_source_artifacts_before_intake(tmp_path):
    harness = load("user_oracle_harness")
    project = tmp_path / "project"
    selected = harness.materialize_project(project_dir=project, fault="refactor")
    helper = project / "app/controllers/user_persist.py"
    controller = project / "app/controllers/user.py"
    assert helper.is_file()
    helper_text = helper.read_text(encoding="utf-8")
    assert "persist_created_user" in helper_text
    assert "persist_user" in helper_text
    assert "User.create" in helper_text or "filter(" in helper_text
    assert "persist_created_user" in controller.read_text(encoding="utf-8")
    original = harness.materialize_project(project_dir=tmp_path / "original", fault="none")
    assert selected["source_digest"] != original["source_digest"]
    assert selected["runtime_digest"] != original["runtime_digest"]
    lock = json.loads((project / ".aa/user-oracle/runtime-lock.json").read_bytes())
    assert lock["fault"] == "refactor"
    assert lock["source_digest"] == selected["source_digest"]
    assert "sut-source/app/controllers/user_persist.py" in lock["files"]


def test_ordinary_ci_has_no_docker_or_qualification():
    workflow = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    commands = "\n".join(
        line
        for line in workflow.splitlines()
        if line.lstrip().startswith("run:") or line.lstrip().startswith("- run:")
    ).lower()
    assert "docker" not in commands
    assert "colima" not in commands
    assert "qualification" not in commands
    assert "build_verification_runner" not in commands
    assert "scripts/graph_engine_smoke_test.sh" in workflow
    assert "scripts/assurance_capability_wheel_smoke_test.sh" in workflow
    assert "scripts/assurance_product_wheel_smoke_test.sh" in workflow


def test_stage2_delivery_record_keeps_three_classes_separate():
    record = (BENCHMARK / "stage2-delivery.md").read_text(encoding="utf-8")
    assert "deterministic" in record.lower()
    assert "otel" in record.lower()
    assert "agent full" in record.lower() or "live full" in record.lower()
    assert "not accepted" in record.lower() or "未验收" in record
    assert "oci" in record.lower()
    assert "optional" in record.lower() or "可选" in record
    assert "opencode-user-api-db-trace" in record
