from __future__ import annotations

from pathlib import Path
import threading

import pytest

from tests.product.cli_support import (
    SECRET_ENV,
    SECRET_VALUE,
    common_lifecycle_args,
    parse_json_output,
    scripted_engine_factory,
    source_args,
    write_product_input,
    write_project_dir,
)
from tests.product.composition_harness import copy_config_tree, request_for

pytestmark = pytest.mark.usefixtures("installed_sources")


@pytest.fixture(autouse=True)
def _reset_runtime_selector() -> None:
    yield
    try:
        from assurance_product.runtime_ports import ProductRuntimePorts
        from assurance_product.runtime_selection import use_test_runtime_selector

        use_test_runtime_selector(None)
        ProductRuntimePorts.test_kernel_resolutions = None
        ProductRuntimePorts._last_scripted_committed = None
    except ImportError:
        return


def _existing_args(
    *,
    project_dir: Path,
    change_id: str,
    invocation_id: str,
    installed_sources,
    secret: str,
) -> list[str]:
    return [
        "--project-dir",
        str(project_dir),
        "--change",
        change_id,
        "--invocation-id",
        invocation_id,
        *source_args(installed_sources),
        "--secret",
        secret,
        "--json",
    ]


def _change_runtime(project_dir: Path, change_id: str) -> Path:
    return project_dir / "qa" / "changes" / change_id / ".runtime"


def test_unknown_entrypoint_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, _project_dir, _change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-unknown-001",
        entrypoint="not-an-entrypoint",
    )
    result = cli_runner.invoke(app, ["start", *args])
    assert result.exit_code == 40, result.output


def test_wrong_runtime_vs_product_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, _project_dir, _change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-wrong-runtime-001",
    )
    product_index = args.index("--product") + 1
    args[product_index] = "assurance-cursor"
    result = cli_runner.invoke(app, ["start", *args])
    assert result.exit_code == 40, result.output


def test_forged_config_fails_closed_on_start(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, _project_dir, _change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-forged-001",
    )
    tree = copy_config_tree(tmp_path / "forged-config")
    (tree.path / "undeclared.txt").write_text("forged", encoding="utf-8")
    args[args.index("--config-tree") + 1] = str(tree.path)
    result = cli_runner.invoke(app, ["start", *args])
    assert result.exit_code == 40, result.output


def test_invalid_input_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, _project_dir, _change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-bad-input-001",
        extra={"selected_test_families": ("api",)},
    )
    result = cli_runner.invoke(app, ["start", *args])
    assert result.exit_code == 40, result.output


def test_missing_secret_handle_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    project_dir = write_project_dir(tmp_path / "project")
    input_path = write_product_input(tmp_path / "input.json", composition)
    result = cli_runner.invoke(
        app,
        [
            "start",
            "--json",
            "--project-dir",
            str(project_dir),
            "--change",
            "CH-DEMO-001",
            "--invocation-id",
            "inv-secret-001",
            *source_args(installed_sources),
            "--entrypoint",
            "intake",
            "--input",
            str(input_path),
        ],
    )
    assert result.exit_code == 40, result.output


def test_missing_invocation_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.cli import app

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    project_dir = write_project_dir(tmp_path / "project")
    ChangeWorkspace.prepare(project_dir, "CH-MISSING-001")
    result = cli_runner.invoke(
        app,
        [
            "status",
            "--json",
            "--project-dir",
            str(project_dir),
            "--change",
            "CH-MISSING-001",
            "--invocation-id",
            "inv-missing-001",
            *source_args(installed_sources),
            "--secret",
            f"opencode.token=env:{SECRET_ENV}",
        ],
    )
    assert result.exit_code == 40, result.output


def test_lock_drift_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-drift-001",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    tree = copy_config_tree(tmp_path / "drifted-config")
    policy = tree.path / ".aa" / "policy.yaml"
    policy.write_text(policy.read_text(encoding="utf-8") + "# drifted\n", encoding="utf-8")
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
            "inv-drift-001",
            "--product",
            args[args.index("--product") + 1],
            "--binding-dist",
            args[args.index("--binding-dist") + 1],
            "--binding-entrypoint",
            "deployment",
            "--binding-declaration",
            args[args.index("--binding-declaration") + 1],
            "--config-tree",
            str(tree.path),
            "--secret",
            args[args.index("--secret") + 1],
        ],
    )
    assert result.exit_code == 40, result.output


def test_wrong_authorization_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    monkeypatch.setenv("AA_NEXT_OTHER_TOKEN", "other")
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-auth-001",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    result = cli_runner.invoke(
        app,
        [
            "status",
            *_existing_args(
                project_dir=project_dir,
                change_id=change_id,
                invocation_id="inv-auth-001",
                installed_sources=installed_sources,
                secret="opencode.token=env:AA_NEXT_OTHER_TOKEN",
            ),
        ],
    )
    assert result.exit_code == 40, result.output


def test_invalid_resume_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product import cli
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    monkeypatch.setattr(cli, "create_engine", scripted_engine_factory())
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-resume-bad-001",
    )
    run = cli_runner.invoke(app, ["run", *args])
    assert run.exit_code == 0, run.output
    result = cli_runner.invoke(
        app,
        [
            "resume",
            *_existing_args(
                project_dir=project_dir,
                change_id=change_id,
                invocation_id="inv-resume-bad-001",
                installed_sources=installed_sources,
                secret=args[args.index("--secret") + 1],
            ),
            "--action",
            "approve",
            "--reason",
            "accepted",
        ],
    )
    assert result.exit_code == 40, result.output


def test_active_run_conflict_fails_closed(cli_runner, installed_sources, tmp_path: Path, monkeypatch):
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition
    from tests.product.product_runner import Engine
    from graph_engine.attempts.secret_sources import (
        InvocationRuntimeAuthorization,
        SecretSourceBinding,
        runtime_authorization_digest,
    )

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-conflict-001",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    secrets = (
        SecretSourceBinding(handle="opencode.token", source_kind="environment", source_locator=SECRET_ENV),
    )
    authorization = InvocationRuntimeAuthorization(
        schema_version="1",
        secret_sources=secrets,
        digest=runtime_authorization_digest(secrets),
    )
    held = threading.Event()
    release = threading.Event()
    engine_root = _change_runtime(project_dir, change_id)
    workspace = ChangeWorkspace.open(project_dir, change_id)

    def hold_claim() -> None:
        with Engine(engine_root) as engine:
            handle = engine.open(
                "inv-conflict-001",
                composition,
                authorization=authorization,
                workspace_binding=workspace.runtime_binding(),
            )
            try:
                claim = engine._acquire_runner_claim(handle._invocation_fd)
                held.set()
                release.wait(timeout=10)
                os_close = __import__("os").close
                os_close(claim)
            finally:
                handle.close()

    worker = threading.Thread(target=hold_claim, name="hold-claim")
    worker.start()
    assert held.wait(timeout=10)
    try:
        result = cli_runner.invoke(
            app,
            [
                "run",
                *_existing_args(
                    project_dir=project_dir,
                    change_id=change_id,
                    invocation_id="inv-conflict-001",
                    installed_sources=installed_sources,
                    secret=args[args.index("--secret") + 1],
                ),
                "--entrypoint",
                "intake",
                "--input",
                args[args.index("--input") + 1],
            ],
        )
        assert result.exit_code == 40, result.output
    finally:
        release.set()
        worker.join(timeout=10)


def test_secret_value_is_not_persisted_in_status_or_lock(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-secret-persist-001",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    assert SECRET_VALUE not in started.output
    secret = args[args.index("--secret") + 1]
    status = cli_runner.invoke(
        app,
        [
            "status",
            *_existing_args(
                project_dir=project_dir,
                change_id=change_id,
                invocation_id="inv-secret-persist-001",
                installed_sources=installed_sources,
                secret=secret,
            ),
        ],
    )
    assert status.exit_code == 0, status.output
    assert SECRET_VALUE not in status.output
    lock = cli_runner.invoke(
        app,
        [
            "lock",
            "show",
            *_existing_args(
                project_dir=project_dir,
                change_id=change_id,
                invocation_id="inv-secret-persist-001",
                installed_sources=installed_sources,
                secret=secret,
            ),
        ],
    )
    assert lock.exit_code == 0, lock.output
    assert SECRET_VALUE not in lock.output
    parse_json_output(status.stdout)
    for path in _change_runtime(project_dir, change_id).rglob("*"):
        if path.is_file():
            assert SECRET_VALUE.encode() not in path.read_bytes()


def test_modular_runner_rejects_legacy_lock_without_mutating_ledger(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition
    from graph_engine.plugin_api import InvocationWorkspaceBinding
    from graph_engine.attempts.secret_sources import empty_runtime_authorization
    from tests.product.product_runner import Engine, empty_invocation_seed

    from tests.product.product_runner import adapter_product_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-legacy-lock-cli-001",
    )
    engine_root = _change_runtime(project_dir, change_id)
    engine_root.mkdir(parents=True, exist_ok=True)
    (engine_root / "invocations").mkdir(exist_ok=True)
    legacy = adapter_product_composition(installed_sources, "cursor")
    engine = Engine(engine_root)
    project = project_dir
    attempts = engine_root.parent / "attempts"
    receipts = engine_root.parent / "receipts"
    for path in (attempts, receipts):
        path.mkdir(parents=True, exist_ok=True)
    handle = engine.start(
        legacy,
        entrypoint="intake",
        invocation_id="inv-legacy-lock-cli-001",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
        workspace_binding=InvocationWorkspaceBinding(
            project_root=project,
            attempts_root=attempts,
            receipts_root=receipts,
        ),
    )
    handle.close()
    engine.close()
    ledger = engine_root / "invocations" / "inv-legacy-lock-cli-001" / "ledger"
    before = tuple(sorted((path, path.read_bytes()) for path in ledger.rglob("*") if path.is_file()))
    result = cli_runner.invoke(
        app,
        [
            "resume",
            *_existing_args(
                project_dir=project_dir,
                change_id=change_id,
                invocation_id="inv-legacy-lock-cli-001",
                installed_sources=installed_sources,
                secret=args[args.index("--secret") + 1],
            ),
            "--action",
            "approve",
            "--reason",
            "accepted",
        ],
    )
    assert result.exit_code == 40, result.output
    after = tuple(sorted((path, path.read_bytes()) for path in ledger.rglob("*") if path.is_file()))
    assert after == before


def test_binding_entrypoint_must_be_deployment(cli_runner, installed_sources):
    from assurance_product.cli import app

    args = source_args(installed_sources)
    args[args.index("--binding-entrypoint") + 1] = "wildcard"
    result = cli_runner.invoke(app, ["compile", "--json", *args])
    assert result.exit_code in {2, 40}, result.output


def test_cli_rejects_graph_import_and_workflow_overrides(cli_runner, tmp_path: Path):
    from assurance_product.cli import app

    for extra in (
        ["--graph", str(tmp_path / "graph.py")],
        ["--workflow", str(tmp_path / "workflow.yaml")],
        ["--execution-contracts", str(tmp_path / "execution-contracts.yaml")],
        ["--python-import", "sut.graphs:build"],
    ):
        result = cli_runner.invoke(app, ["compile", "--json", *extra])
        assert result.exit_code == 2, extra
        assert "no such option" in result.output.lower() or "no such option" in str(result.exception).lower()


def test_resume_file_rejects_unknown_and_duplicate_ids(cli_runner, tmp_path: Path):
    from assurance_product.application import parse_resume_file

    missing = tmp_path / "missing.json"
    missing.write_text('{"interrupt_id":"unknown","action":"approve"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown|missing|duplicate"):
        parse_resume_file(missing, pending_ids=("known",))
    duplicates = tmp_path / "dup.json"
    duplicates.write_text(
        '{"interrupts":{"a":{"action":"approve"},"a":{"action":"reject"}}}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate|unknown|missing"):
        parse_resume_file(duplicates, pending_ids=("a", "b"))
    scalar = tmp_path / "scalar.json"
    scalar.write_text('"approve"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="ambiguous"):
        parse_resume_file(scalar, pending_ids=("a", "b"))


def test_application_resume_file_rejects_unknown_interrupt_id(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition
    from assurance_product.runtime_ports import ProductRuntimePorts
    from assurance_product.runtime_selection import use_test_runtime_selector
    from graph_engine.attempts.resolutions import PendingTaskResult, SystemReference

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    use_test_runtime_selector(lambda _entrypoint: "langgraph-v1")
    ProductRuntimePorts.test_kernel_resolutions = [
        PendingTaskResult(wakeup=SystemReference(reference_id="wake-unknown")),
    ]
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-resume-unknown",
        entrypoint="archive",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    blocked = cli_runner.invoke(
        app,
        [
            "run",
            "--json",
            "--project-dir",
            str(project_dir),
            "--change",
            change_id,
            "--invocation-id",
            "inv-resume-unknown",
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
        ],
    )
    assert blocked.exit_code in {20, 30}, blocked.output
    resume_file = tmp_path / "unknown-resume.json"
    resume_file.write_text('{"interrupt_id":"unknown","action":"approve"}\n', encoding="utf-8")
    resumed = cli_runner.invoke(
        app,
        [
            "resume",
            "--json",
            "--project-dir",
            str(project_dir),
            "--change",
            change_id,
            "--invocation-id",
            "inv-resume-unknown",
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
            "--resume-file",
            str(resume_file),
        ],
    )
    assert resumed.exit_code == 40, resumed.output
    assert "unknown" in resumed.output.lower() or "missing" in resumed.output.lower()
