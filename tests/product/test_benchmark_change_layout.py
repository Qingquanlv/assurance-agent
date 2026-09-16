from __future__ import annotations

from contextlib import contextmanager
import importlib.util
from importlib.resources import files
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from assurance_product.opencode_agents import _opencode_config
from assurance_product.cli import app
from click.testing import CliRunner
from tests.acg_plan_fixture import install_plan

from tests.product.test_benchmark_manifest import (
    FULL_WORKFLOW_REQUIRED_STEPS,
    REPO,
    RUNNER_PATH,
)

ITEM_ID = "opencode-ret-dept-management"
STAMP = "20260826-120000"
NONCE = "a1b2c3d4"
ORIGINAL_TEST = "tests/api/test_existing.py"
ORIGINAL_BYTES = b"assert original_sut_test\n"


def _load_runner():
    spec = importlib.util.spec_from_file_location("run_item_change_layout", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_sut(root: Path) -> Path:
    sut = root / "sut"
    (sut / "app").mkdir(parents=True)
    (sut / "app" / "__init__.py").write_text("app = object()\n", encoding="utf-8")
    (sut / "web").mkdir()
    (sut / "tests" / "api").mkdir(parents=True)
    (sut / "tests" / "api" / "test_existing.py").write_bytes(ORIGINAL_BYTES)
    (sut / ".aa").mkdir()
    (sut / ".aa" / "data-knowledge.yaml").write_text(
        "schema_version: '1'\ncapabilities:\n  domain_factories: {}\n  adapters:\n    api: {}\n",
        encoding="utf-8",
    )
    (sut / ".aa" / "policy.yaml").write_text(
        "schema_version: '1'\n"
        "test_family_policy:\n"
        "  required: [api]\n"
        "  allowed: [api, e2e, fuzz, performance]\n"
        "coverage_floor_by_tier:\n"
        "  low: 0.7\n"
        "  medium: 0.8\n"
        "  high: 0.9\n"
        "  critical: 1.0\n"
        "evidence_sufficiency:\n"
        "  recency_hours: 24\n"
        "  require_current_batch: true\n",
        encoding="utf-8",
    )
    return sut


def test_project_config_tree_uses_exact_live_sut_policy(tmp_path: Path) -> None:
    runner = _load_runner()
    sut = _make_sut(tmp_path)
    config_tree = tmp_path / "config-tree"
    shutil.copytree(REPO / "tests" / "product" / "fixtures" / "project-config", config_tree)

    runner._prepare_project_config_tree(config_tree, sut)

    source_policy = (sut / ".aa" / "policy.yaml").read_bytes()
    assert (config_tree / ".aa" / "policy.yaml").read_bytes() == source_policy
    envelope = yaml.safe_load((config_tree / ".aa" / "config.yaml").read_text(encoding="utf-8"))
    assert envelope["product_policy"] == yaml.safe_load(source_policy)


def test_project_config_tree_materializes_exact_live_sut_catalog(tmp_path: Path) -> None:
    runner = _load_runner()
    sut = _make_sut(tmp_path)
    config_tree = tmp_path / "config-tree"
    shutil.copytree(REPO / "tests" / "product" / "fixtures" / "project-config", config_tree)

    runner._prepare_project_config_tree(config_tree, sut)

    source_catalog = (sut / ".aa" / "capability-catalog.json").read_bytes()
    assert source_catalog == (config_tree / ".aa" / "capability-catalog.json").read_bytes()
    envelope = yaml.safe_load((config_tree / ".aa" / "config.yaml").read_text(encoding="utf-8"))
    assert envelope["capability_catalog"] == json.loads(source_catalog)


def test_project_config_tree_uses_exact_live_sut_knowledge_bytes(tmp_path: Path) -> None:
    runner = _load_runner()
    sut = _make_sut(tmp_path)
    (sut / ".aa" / "data-knowledge.yaml").write_text(
        'schema_version: "1"\ncapabilities: {domain_factories: {}, adapters: {api: {}}}\n# keep-bytes\n',
        encoding="utf-8",
    )
    config_tree = tmp_path / "config-tree"
    shutil.copytree(REPO / "tests" / "product" / "fixtures" / "project-config", config_tree)

    source_knowledge = (sut / ".aa" / "data-knowledge.yaml").read_bytes()
    runner._prepare_project_config_tree(config_tree, sut)

    assert (config_tree / ".aa" / "data-knowledge.yaml").read_bytes() == source_knowledge


def _required_node_states() -> list[dict[str, str]]:
    states: list[dict[str, str]] = []
    for step in FULL_WORKFLOW_REQUIRED_STEPS:
        if step.startswith("generation."):
            node_id = f"{step}/finalize"
        elif step.startswith(("intake.", "execution.", "quality.")):
            node_id = f"{step.split('.', 1)[1]}/finalize"
        else:
            node_id = f"{step}/finalize"
        states.append(
            {
                "graph_instance_id": f"graph-{step}",
                "node_id": node_id,
                "state": "succeeded",
            }
        )
    return states


def _achieved_status(*, change_id: str) -> dict[str, Any]:
    graphs = [
        {
            "graph_instance_id": f"graph-{step}",
            "graph_id": step,
        }
        for step in FULL_WORKFLOW_REQUIRED_STEPS
    ]
    return {
        "status": "completed",
        "lock_digest": "a" * 64,
        "entrypoint": "full",
        "selected_test_families": ["api"],
        "coverage_progress": None,
        "terminal_reason": None,
        "graph_hierarchy": graphs,
        "node_states": _required_node_states(),
        "change": {"change_id": change_id, "state": "achieved"},
        "adapter_evidence": [
            {
                "activation_id": "act-1",
                "activity_id": "act-1",
                "reference_digest": "b" * 64,
                "terminal_receipt_digest": "c" * 64,
            }
        ],
        "execution_gate": None,
        "quality_gate": None,
    }


def _failed_status(*, change_id: str) -> dict[str, Any]:
    status = _achieved_status(change_id=change_id)
    status["status"] = "failed"
    status["change"] = {"change_id": change_id, "state": "failed"}
    status["terminal_reason"] = "task_failed:execute:external_effect"
    status["node_states"] = []
    return status


_REAL_SUBPROCESS_RUN = subprocess.run


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeAA:
    def __init__(
        self,
        *,
        sut: Path,
        change_id: str,
        terminal: Mapping[str, Any],
    ) -> None:
        self.sut = sut
        self.change_id = change_id
        self.terminal = dict(terminal)
        self.commands: list[list[str]] = []
        self.project_dirs: list[str] = []

    def _record(self, command: Sequence[str]) -> None:
        recorded = [str(part) for part in command]
        self.commands.append(recorded)
        if "--project-dir" in recorded:
            self.project_dirs.append(recorded[recorded.index("--project-dir") + 1])

    def _materialize_change(self) -> Path:
        change_root = self.sut / "qa"
        change_root.mkdir(parents=True, exist_ok=True)
        (change_root / "status.json").write_text(
            json.dumps(self.terminal, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return change_root

    def handle_aa(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self._record(command)
        verb = command[1] if len(command) > 1 else ""
        if verb not in app.commands:
            result = CliRunner().invoke(app, list(command[1:]))
            assert result.exit_code != 0
            return _completed(result.exit_code, stderr=result.output)
        if verb == "bindings":
            return _completed(
                0,
                json.dumps(
                    {
                        "distribution": "assurance-product-bindings-test",
                        "declaration_path": "deployment",
                        "wheel": "/tmp/fake-binding.whl",
                    }
                ),
            )
        if verb == "compile":
            return _completed(0, json.dumps({"lock_digest": "a" * 64}))
        if verb == "start":
            self._materialize_change()
            return _completed(0, json.dumps({"lock_digest": "a" * 64, "invocation_id": ITEM_ID}))
        if verb == "status":
            return _completed(0, json.dumps(self.terminal))
        if verb == "run":
            return _completed(
                0,
                json.dumps(
                    {
                        "invocation_id": ITEM_ID,
                        "status": self.terminal.get("status"),
                        "terminal_reason": self.terminal.get("terminal_reason"),
                        "actions": [],
                    }
                ),
            )
        return _completed(1, stderr=f"unexpected aa command: {command}")

    def handle_subprocess(self, command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded = [str(part) for part in command]
        self.commands.append(recorded)
        joined = " ".join(recorded)
        if recorded and Path(recorded[0]).name == "git":
            return _REAL_SUBPROCESS_RUN(command, **kwargs)
        if "install_opencode_agents" in joined or "OpenCode agent" in joined:
            raise AssertionError("live run must not install OpenCode configuration")
        if "validate_export.py" in joined:
            raise AssertionError("live run must not validate a result-tree export")
        if recorded and Path(recorded[0]).name.endswith("aa-next"):
            stdout = kwargs.get("stdout")
            result = self.handle_aa(recorded)
            write = getattr(stdout, "write", None)
            if callable(write):
                write(result.stdout or "")
                return _completed(result.returncode)
            return result
        if "write_product_input.py" in joined:
            args_path = Path(recorded[-1])
            arguments = json.loads(args_path.read_text(encoding="utf-8"))
            assert "auto_archive" not in arguments
            Path(arguments["output"]).write_text("{}\n", encoding="utf-8")
            return _completed(0)
        if "uv" in recorded:
            return _completed(0)
        return _completed(0)


class _UnstructuredRunAA(_FakeAA):
    def handle_aa(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if len(command) > 1 and command[1] == "run":
            return _completed(23, stdout="deterministic local runner failure\n")
        if len(command) > 1 and command[1] == "status":
            raise AssertionError("unstructured non-zero run must fail before status polling")
        return super().handle_aa(command)


def _wire_fake(runner, monkeypatch: pytest.MonkeyPatch, fake: _FakeAA, sut: Path) -> None:
    python = Path("/tmp/fake-python")
    aa_next = Path("/tmp/fake-aa-next")
    monkeypatch.setattr(runner, "_resolve_sut", lambda _repo, _relative: sut)
    monkeypatch.setattr(
        runner,
        "_prepare_installed_product_env",
        lambda **_kwargs: (python, aa_next),
    )
    monkeypatch.setattr(runner, "_check_opencode", lambda _endpoint: 0)
    monkeypatch.setattr(runner, "_runtime_environment_errors", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(runner, "_project_opencode_asset_errors", lambda _project: [])
    monkeypatch.setattr(runner, "_check_opencode_agent_profiles", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(runner, "_check_opencode_boundary_plugin", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        runner, "_aa_next", lambda binary, *args, **kwargs: fake.handle_aa([str(binary), *args])
    )
    monkeypatch.setattr(runner.subprocess, "run", fake.handle_subprocess)
    monkeypatch.setattr(runner, "_load_opencode_secret", lambda _name: None)
    monkeypatch.setattr(
        runner,
        "_prepare_sut_worktree",
        lambda **kwargs: kwargs["sut_root"],
    )

    @contextmanager
    def ready_runtime(**_kwargs):
        yield runner._SutRuntime(
            env={},
            backend_url="http://127.0.0.1:9999",
            frontend_url="http://127.0.0.1:3100",
            sqlite_file=Path("/tmp/fake-sut.sqlite3"),
            backend_log=Path("/tmp/fake-backend.log"),
            frontend_log=Path("/tmp/fake-frontend.log"),
        )

    monkeypatch.setattr(runner, "_managed_sut_runtime", ready_runtime)


def test_explicit_project_runs_without_touching_retained_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    original = _make_sut(tmp_path / "original")
    retained = original / "qa/results/report/report.md"
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b"retained departmental evidence\n")
    isolated = _make_sut(tmp_path / "isolated")
    change_id = runner.derive_change_id(item_id=ITEM_ID, stamp=STAMP, nonce=NONCE)
    fake = _FakeAA(sut=isolated, change_id=change_id, terminal=_achieved_status(change_id=change_id))
    real_resolver = runner._resolve_sut
    _wire_fake(runner, monkeypatch, fake, isolated)

    def resolve(repo, relative):
        assert relative == str(isolated)
        return real_resolver(repo, relative)

    monkeypatch.setattr(runner, "_resolve_sut", resolve)
    output = tmp_path / "new-result"
    code = runner.main(
        [
            "--item",
            ITEM_ID,
            "--adapter",
            "opencode",
            "--project-dir",
            str(isolated),
            "--output",
            str(output),
            "--stamp",
            STAMP,
            "--nonce",
            NONCE,
        ]
    )
    assert code == 0
    evidence = json.loads((output / "evidence.json").read_text())
    assert evidence["change_root"] == str(isolated / "qa")
    assert (isolated / "qa/status.json").is_file()
    assert retained.read_bytes() == b"retained departmental evidence\n"
    assert not (original / "qa/status.json").exists()


def test_explicit_project_refuses_existing_qa_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    sut = _make_sut(tmp_path)
    retained = sut / "qa/status.json"
    retained.parent.mkdir()
    retained.write_bytes(b"old identity\n")
    code = runner.main(
        [
            "--item",
            ITEM_ID,
            "--adapter",
            "opencode",
            "--project-dir",
            str(sut),
            "--output",
            str(tmp_path / "result"),
        ]
    )
    assert code != 0
    assert retained.read_bytes() == b"old identity\n"
    monkeypatch.setenv("AA_NEXT_OPENCODE_TOKEN", "test-token")


def test_run_item_does_not_materialize_test_runtime_seed() -> None:
    runner = _load_runner()
    assert not hasattr(runner, "_materialize_test_runtime_seed")
    assert not hasattr(runner, "_TEST_RUNTIME_MANIFEST_SHA256")


@pytest.mark.parametrize("preflight_failure", (False, True))
def test_retained_init_receipt_is_not_current_run_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preflight_failure: bool
) -> None:
    runner = _load_runner()
    sut = _make_sut(tmp_path)
    receipt = sut / "qa/results/init/test-runtime.json"
    receipt.parent.mkdir(parents=True)
    retained = b'{"schema_version":"1","change_id":"previous-run"}\n'
    receipt.write_bytes(retained)
    change_id = runner.derive_change_id(item_id=ITEM_ID, stamp=STAMP, nonce=NONCE)
    fake = _FakeAA(sut=sut, change_id=change_id, terminal=_failed_status(change_id=change_id))
    _wire_fake(runner, monkeypatch, fake, sut)
    if preflight_failure:
        monkeypatch.setattr(runner, "_check_opencode", lambda _endpoint: 1)
    output = tmp_path / "output"
    assert (
        runner.main(
            [
                "--item",
                ITEM_ID,
                "--adapter",
                "opencode",
                "--output",
                str(output),
                "--nonce",
                NONCE,
                "--stamp",
                STAMP,
            ]
        )
        != 0
    )
    evidence = json.loads((output / "evidence.json").read_text())
    assert evidence.get("test_runtime_seed") is None
    assert receipt.read_bytes() == retained


def test_limited_role_provisioning_creates_an_unprivileged_role_and_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    requests: list[tuple[str, str, str, object]] = []

    def request_json(backend_url, path, *, method, token, payload=None):
        requests.append((backend_url, path, method, payload))
        assert token == "admin-token"
        if path.startswith("/api/v1/role/list?"):
            return {"code": 200, "data": [{"id": 7, "name": "aa-limited"}]}
        return {"code": 200, "data": None}

    logins: list[tuple[str, str, str]] = []

    def acquire_token(backend_url: str, *, username: str, password: str) -> str:
        logins.append((backend_url, username, password))
        return "limited-token"

    monkeypatch.setattr(runner, "_request_sut_json", request_json)
    monkeypatch.setattr(runner, "_acquire_token", acquire_token)
    monkeypatch.setattr(runner.secrets, "token_urlsafe", lambda _length: "ephemeral-password")

    token, username, password = runner._provision_limited_role_identity(
        "http://127.0.0.1:9999",
        admin_token="admin-token",
    )

    assert (token, username, password) == (
        "limited-token",
        "aa_limited",
        "ephemeral-password",
    )
    assert requests == [
        (
            "http://127.0.0.1:9999",
            "/api/v1/role/create",
            "POST",
            {"name": "aa-limited", "desc": "Assurance benchmark restricted role"},
        ),
        (
            "http://127.0.0.1:9999",
            "/api/v1/role/list?page=1&page_size=100&role_name=aa-limited",
            "GET",
            None,
        ),
        (
            "http://127.0.0.1:9999",
            "/api/v1/role/authorized",
            "POST",
            {"id": 7, "menu_ids": [], "api_infos": []},
        ),
        (
            "http://127.0.0.1:9999",
            "/api/v1/user/create",
            "POST",
            {
                "email": "aa_limited@example.com",
                "username": "aa_limited",
                "password": "ephemeral-password",
                "is_active": True,
                "is_superuser": False,
                "role_ids": [7],
                "dept_id": 0,
            },
        ),
    ]
    assert logins == [("http://127.0.0.1:9999", "aa_limited", "ephemeral-password")]


def test_managed_sut_is_ready_before_yield_and_cleans_both_groups_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    sut = _make_sut(tmp_path)
    (sut / "migrations").mkdir()
    (sut / "migrations" / "seed.py").write_text("migration = 1\n", encoding="utf-8")
    (sut / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (sut / "pyproject.toml").write_text("[project]\nname='sut'\nversion='0'\n", encoding="utf-8")
    vite = sut / "web" / "node_modules" / ".bin" / "vite"
    vite.parent.mkdir(parents=True)
    vite.write_text("#!/bin/sh\n", encoding="utf-8")
    vite.chmod(0o755)
    (sut / "web" / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    state = {
        "ports": False,
        "backend": False,
        "backend_ready": False,
        "login": False,
        "limited_identity": False,
        "frontend": False,
        "frontend_ready": False,
    }
    stopped: list[str] = []

    class Process:
        def __init__(self, name: str, pid: int) -> None:
            self.name = name
            self.pid = pid

    monkeypatch.setattr(runner, "_prepare_sut_python", lambda **_kwargs: Path(sys.executable))
    checked_ports: list[int] = []

    def port_available(port: int) -> None:
        checked_ports.append(port)
        state["ports"] = checked_ports == [9999, 3100]

    monkeypatch.setattr(runner, "_assert_loopback_port_available", port_available)

    def spawn(*, label: str, command, cwd, env, log_path):
        del command, cwd, log_path
        if label == "backend":
            assert state["ports"]
            state["backend"] = True
        else:
            assert state["limited_identity"]
            assert env["E2E_API_TOKEN"] == "runtime-token"
            assert env["API_LIMITED_ROLE_USER_TOKEN"] == "limited-token"
            assert env["QA_LIMITED_USERNAME"] == "aa_limited"
            assert env["QA_LIMITED_PASSWORD"] == "ephemeral-password"
            state["frontend"] = True
        return Process(label, 100 + len(stopped))

    def ready(url: str, *, accept: str, **_kwargs) -> None:
        if url.endswith("openapi.json"):
            assert state["backend"]
            assert accept == "application/json"
            state["backend_ready"] = True
        else:
            assert state["frontend"]
            assert accept == "text/html,application/xhtml+xml"
            state["frontend_ready"] = True

    def login(*_args, **_kwargs) -> str:
        assert state["backend_ready"]
        state["login"] = True
        return "runtime-token"

    def provision(*_args, admin_token: str, **_kwargs) -> tuple[str, str, str]:
        assert state["login"]
        assert admin_token == "runtime-token"
        state["limited_identity"] = True
        return "limited-token", "aa_limited", "ephemeral-password"

    monkeypatch.setattr(runner, "_spawn_managed_process", spawn)
    monkeypatch.setattr(runner, "_wait_http_ready", ready)
    monkeypatch.setattr(runner, "_acquire_token", login)
    monkeypatch.setattr(runner, "_provision_limited_role_identity", provision)
    monkeypatch.setattr(
        runner,
        "_stop_managed_process",
        lambda process: stopped.append(process.name),
    )

    with pytest.raises(RuntimeError, match="benchmark body failed"):
        with runner._managed_sut_runtime(
            repo=RUNNER_PATH.parents[2],
            project_dir=sut,
            output=tmp_path / "output",
            env=os.environ,
        ) as runtime:
            assert all(state.values())
            assert runtime.env["BASE_URL"] == "http://127.0.0.1:9999"
            assert runtime.env["E2E_FRONTEND_URL"] == "http://127.0.0.1:3100"
            assert runtime.env["QA_FUZZ_SCHEMA_MODE"] == "uri"
            assert runtime.env["NO_PROXY"] == "127.0.0.1,localhost"
            assert runtime.env["no_proxy"] == "127.0.0.1,localhost"
            assert runtime.env["QA_SQLITE_FILE"] == str(runtime.sqlite_file)
            assert runtime.env["API_LIMITED_ROLE_USER_TOKEN"] == "limited-token"
            assert runtime.env["QA_LIMITED_USERNAME"] == "aa_limited"
            assert runtime.env["QA_LIMITED_PASSWORD"] == "ephemeral-password"
            assert runtime.sqlite_file.parent.joinpath("app", "__init__.py").is_file()
            assert runtime.sqlite_file.parent.joinpath("migrations", "seed.py").is_file()
            raise RuntimeError("benchmark body failed")

    assert stopped == ["frontend", "backend"]


def test_loopback_port_preflight_rejects_an_existing_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()

    class Probe:
        def __init__(self, *, occupied: bool) -> None:
            self.occupied = occupied

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, _seconds: float) -> None:
            return None

        def connect_ex(self, _address) -> int:
            return 0 if self.occupied else 61

    monkeypatch.setattr(runner.socket, "socket", lambda: Probe(occupied=True))
    with pytest.raises(SystemExit, match="already in use"):
        runner._assert_loopback_port_available(9999)

    monkeypatch.setattr(runner.socket, "socket", lambda: Probe(occupied=False))
    runner._assert_loopback_port_available(9999)


def test_sut_python_environment_uses_an_isolated_frozen_uv_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    sut = tmp_path / "sut"
    runtime_root = tmp_path / "runtime"
    sut.mkdir()
    runtime_root.mkdir()
    (sut / "pyproject.toml").write_text("[project]\nname='sut'\nversion='0'\n", encoding="utf-8")
    (sut / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    calls: list[tuple[list[str], Mapping[str, str], str]] = []

    def run_checked(command, *, env, label, **_kwargs):
        calls.append((list(command), dict(env), label))
        if label == "managed SUT uv venv":
            python = runtime_root / "venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
        return _completed(0)

    monkeypatch.setattr(runner, "_run_checked", run_checked)

    python = runner._prepare_sut_python(
        project_dir=sut,
        runtime_root=runtime_root,
        env={"PATH": "/bin"},
    )

    assert python == runtime_root / "venv" / "bin" / "python"
    assert calls[0][0] == ["uv", "venv", "--python", "3.11", str(runtime_root / "venv")]
    assert calls[1][0] == [
        "uv",
        "sync",
        "--active",
        "--frozen",
        "--project",
        str(sut),
        "--no-install-project",
    ]
    assert calls[1][1]["VIRTUAL_ENV"] == str(runtime_root / "venv")


def test_managed_process_uses_a_new_session_and_refuses_group_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    observed: dict[str, Any] = {}

    class Process:
        pid = 4312

        @staticmethod
        def poll() -> None:
            return None

    def popen(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return Process()

    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    process = runner._spawn_managed_process(
        label="backend",
        command=("python", "-m", "uvicorn"),
        cwd=tmp_path,
        env={"PATH": "/bin"},
        log_path=tmp_path / "backend.log",
    )
    assert observed["start_new_session"] is True
    assert observed["stdin"] is subprocess.DEVNULL
    assert observed["stderr"] is subprocess.STDOUT

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(runner.os, "getpgid", lambda _pid: 9999)
    monkeypatch.setattr(runner.os, "killpg", lambda group, sig: killed.append((group, sig)))
    with pytest.raises(SystemExit, match="changed group identity"):
        runner._stop_managed_process(process)
    assert killed == []


def test_runner_has_no_project_copy_or_export_tree_helpers() -> None:
    runner = _load_runner()
    source = RUNNER_PATH.read_text(encoding="utf-8")

    assert not hasattr(runner, "_copy_sut")
    assert not hasattr(runner, "_SUT_COPY_IGNORE")
    assert "_copy_sut" not in source
    assert "_SUT_COPY_IGNORE" not in source
    assert "auto_archive" not in source
    assert 'output / "project"' not in source
    assert 'output / "export"' not in source
    assert "workspace/trees" not in source
    assert "HEAD.json" not in source
    assert "latest-run" not in source
    assert "result-registry" not in source
    assert "result_registry" not in source
    assert "install_opencode_agents" not in source


def test_change_id_is_unique_and_deterministic_from_item_stamp_and_nonce() -> None:
    runner = _load_runner()

    first = runner.derive_change_id(item_id=ITEM_ID, stamp=STAMP, nonce=NONCE)
    second = runner.derive_change_id(item_id=ITEM_ID, stamp=STAMP, nonce=NONCE)
    other = runner.derive_change_id(item_id=ITEM_ID, stamp=STAMP, nonce="ffff9999")

    assert first == second
    assert first != other
    assert ITEM_ID in first
    assert STAMP in first
    assert NONCE in first
    assert "/" not in first
    assert "\\" not in first
    assert " " not in first


def test_runtime_environment_contract_is_exact_and_binds_the_run_scoped_database(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    output = tmp_path / "result"
    required = {
        "AA_ADMIN_PASSWORD": "123456",
        "AA_ADMIN_USERNAME": "admin",
        "AA_BASE_URL": "http://127.0.0.1:9999",
        "AA_SQLITE_PATH": str(output / "sut-runtime" / "db.sqlite3"),
        "API_BASE_URL": "http://127.0.0.1:9999",
        "BASE_URL": "http://127.0.0.1:9999",
        "E2E_BACKEND_URL": "http://127.0.0.1:9999",
        "E2E_FRONTEND_URL": "http://127.0.0.1:3100",
        "FRONTEND_URL": "http://127.0.0.1:3100",
        "FUZZ_SCHEMA_MODE": "uri",
        "NO_PROXY": "127.0.0.1,localhost",
        "QA_ADMIN_PASSWORD": "123456",
        "QA_ADMIN_USERNAME": "admin",
        "QA_FUZZ_SCHEMA_MODE": "uri",
        "QA_SQLITE_FILE": str(output / "sut-runtime" / "db.sqlite3"),
        "no_proxy": "127.0.0.1,localhost",
    }

    assert runner._required_runtime_environment(output) == required
    opencode_environment = runner._required_opencode_environment(output)
    assert opencode_environment["XDG_CONFIG_HOME"] == str(output.parent / ".opencode-config" / output.name)
    assert not Path(opencode_environment["XDG_CONFIG_HOME"]).is_relative_to(output)
    assert "XDG_DATA_HOME" not in opencode_environment
    assert "AA_ADMIN_PASSWORD" not in opencode_environment
    assert "QA_ADMIN_PASSWORD" not in opencode_environment
    assert "AA_ADMIN_USERNAME" not in opencode_environment
    assert "QA_ADMIN_USERNAME" not in opencode_environment
    assert runner._runtime_environment_errors(opencode_environment, output=output) == []
    opencode_environment["BASE_URL"] = "http://127.0.0.1:8000"
    assert runner._runtime_environment_errors(opencode_environment, output=output) == [
        "OpenCode server environment BASE_URL must equal 'http://127.0.0.1:9999'"
    ]


def test_success_uses_current_cli_and_finishes_at_achieved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    sut = _make_sut(tmp_path)
    change_id = runner.derive_change_id(item_id=ITEM_ID, stamp=STAMP, nonce=NONCE)
    output = tmp_path / "results" / "opencode-run"
    fake = _FakeAA(
        sut=sut,
        change_id=change_id,
        terminal=_achieved_status(change_id=change_id),
    )
    _wire_fake(runner, monkeypatch, fake, sut)

    code = runner.main(
        [
            "--item",
            ITEM_ID,
            "--adapter",
            "opencode",
            "--output",
            str(output),
            "--nonce",
            NONCE,
            "--stamp",
            STAMP,
        ]
    )

    change_root = sut / "qa"
    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert code == 0
    assert all(command[1] != "export" for command in fake.commands if len(command) > 1)
    assert fake.project_dirs
    assert set(fake.project_dirs) == {str(sut)}
    assert not (output / "project").exists()
    assert not (output / "export").exists()
    assert not (output / "workspace").exists()
    assert not (output / "HEAD.json").exists()
    assert not (sut / "qa" / "latest").exists()
    assert not (sut / "qa" / "HEAD.json").exists()
    assert not (output / "latest").exists()
    assert not (output / "result-registry.json").exists()
    assert change_root.is_dir()
    assert not (change_root / "results" / "publish-receipt.json").exists()
    assert evidence["sut_root"] == str(sut)
    assert evidence["change_id"] == change_id
    assert evidence["change_root"] == str(change_root)
    assert evidence["terminal_status"] == "completed"
    assert "publish_receipt" not in evidence
    assert evidence["provider"]["session"] or evidence["provider"]["process"]
    assert Path(evidence["logs"]["run_log"]).is_file()
    assert evidence.get("result_tree_digest") is None
    assert "auto_archive" not in evidence


def test_benchmark_plan_evidence_reads_the_real_producer_layout(tmp_path: Path) -> None:
    runner = _load_runner()
    plan, ref = install_plan(tmp_path, "CH-DEMO-001")
    assert (tmp_path / ref["path"]).is_file()
    actual_plan, actual_ref = runner._acg_plan(tmp_path / "qa")
    assert actual_plan == plan.model_dump(mode="json")
    assert actual_ref == ref


def test_main_keeps_managed_sut_active_from_start_through_achieved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    sut = _make_sut(tmp_path)
    change_id = runner.derive_change_id(item_id=ITEM_ID, stamp=STAMP, nonce=NONCE)
    fake = _FakeAA(
        sut=sut,
        change_id=change_id,
        terminal=_achieved_status(change_id=change_id),
    )
    _wire_fake(runner, monkeypatch, fake, sut)
    active = False
    lifecycle: list[str] = []

    @contextmanager
    def tracked_runtime(**_kwargs):
        nonlocal active
        active = True
        lifecycle.append("entered")
        try:
            yield runner._SutRuntime(
                env={"BASE_URL": "http://127.0.0.1:9999"},
                backend_url="http://127.0.0.1:9999",
                frontend_url="http://127.0.0.1:3100",
                sqlite_file=Path("/tmp/fake-sut.sqlite3"),
                backend_log=Path("/tmp/fake-backend.log"),
                frontend_log=Path("/tmp/fake-frontend.log"),
            )
        finally:
            active = False
            lifecycle.append("exited")

    original_handle = fake.handle_aa

    def require_runtime(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if len(command) > 1 and command[1] in {"start", "run", "status"}:
            assert active, f"{command[1]} ran outside the managed SUT lifecycle"
        return original_handle(command)

    fake.handle_aa = require_runtime  # type: ignore[method-assign]
    monkeypatch.setattr(runner, "_managed_sut_runtime", tracked_runtime)

    code = runner.main(
        [
            "--item",
            ITEM_ID,
            "--adapter",
            "opencode",
            "--output",
            str(tmp_path / "output"),
            "--nonce",
            NONCE,
            "--stamp",
            STAMP,
        ]
    )

    assert code == 0
    assert lifecycle == ["entered", "exited"]
    assert active is False


def test_failure_leaves_original_sut_tests_unchanged_and_skips_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    sut = _make_sut(tmp_path)
    change_id = runner.derive_change_id(item_id=ITEM_ID, stamp=STAMP, nonce=NONCE)
    output = tmp_path / "results" / "opencode-fail"
    fake = _FakeAA(
        sut=sut,
        change_id=change_id,
        terminal=_failed_status(change_id=change_id),
    )
    _wire_fake(runner, monkeypatch, fake, sut)

    code = runner.main(
        [
            "--item",
            ITEM_ID,
            "--adapter",
            "opencode",
            "--output",
            str(output),
            "--nonce",
            NONCE,
            "--stamp",
            STAMP,
        ]
    )

    assert code != 0
    assert all(command[1] != "export" for command in fake.commands if len(command) > 1)
    assert (sut / ORIGINAL_TEST).read_bytes() == ORIGINAL_BYTES
    assert not (output / "project").exists()
    assert not (output / "export").exists()
    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["change_id"] == change_id
    assert evidence["change_root"] == str(sut / "qa")
    assert evidence["terminal_status"] == "failed"
    assert "publish_receipt" not in evidence


def test_nonzero_unstructured_run_fails_closed_without_status_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    sut = _make_sut(tmp_path)
    change_id = runner.derive_change_id(item_id=ITEM_ID, stamp=STAMP, nonce=NONCE)
    output = tmp_path / "results" / "opencode-unstructured-run"
    fake = _UnstructuredRunAA(
        sut=sut,
        change_id=change_id,
        terminal=_achieved_status(change_id=change_id),
    )
    _wire_fake(runner, monkeypatch, fake, sut)

    code = runner.main(
        [
            "--item",
            ITEM_ID,
            "--adapter",
            "opencode",
            "--output",
            str(output),
            "--nonce",
            NONCE,
            "--stamp",
            STAMP,
        ]
    )

    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert code == 23
    assert evidence["outcome"] == "blocked"
    assert evidence["validation"]["last_run"]["returncode"] == 23
    assert "structured run result" in evidence["notes"]
    assert all(command[1] != "status" for command in fake.commands if len(command) > 1)


def test_run_result_parser_does_not_reuse_a_prior_invocation_json_object(tmp_path: Path) -> None:
    runner = _load_runner()
    run_log = tmp_path / "run.log"
    run_log.write_text('{"status":"running"}\n', encoding="utf-8")
    current_start = run_log.stat().st_size
    with run_log.open("a", encoding="utf-8") as stream:
        stream.write("deterministic local runner failure\n")

    assert runner._last_json_object(run_log, start=current_start) is None


def test_preflight_uses_product_locked_profiles_and_does_not_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    sut = _make_sut(tmp_path)
    installed = {"agent": {"assurance-v1-explorer": {"prompt": "locked", "permission": {}, "tools": {}}}}
    resolved = {"agent": {"assurance-v1-explorer": {"prompt": "drifted", "permission": {}, "tools": {}}}}
    monkeypatch.setattr(runner, "_product_locked_opencode_config", lambda: installed)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit: int) -> bytes:
            return json.dumps(resolved).encode()

    monkeypatch.setattr(runner, "urlopen", lambda *_args, **_kwargs: Response())
    installs: list[Path] = []
    monkeypatch.setattr(
        runner,
        "_install_opencode_agents",
        lambda project: installs.append(project) or (_ for _ in ()).throw(AssertionError("install")),
        raising=False,
    )

    errors = runner._check_opencode_agent_profiles("http://127.0.0.1:4096", sut)

    assert installs == []
    assert not (sut / "opencode.json").exists()
    assert any("prompt" in error for error in errors)


def test_project_opencode_asset_preflight_rejects_a_stale_boundary_plugin(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    sut = _make_sut(tmp_path)
    (sut / "opencode.json").write_text(_opencode_config(), encoding="utf-8")
    plugin = sut / ".opencode" / "plugins" / "assurance-boundary.mjs"
    plugin.parent.mkdir(parents=True)
    expected_plugin = (
        files("assurance_product").joinpath("resources", "opencode", "assurance-boundary.mjs").read_bytes()
    )
    plugin.write_bytes(expected_plugin)
    assert runner._project_opencode_asset_errors(sut) == []

    plugin.write_bytes(expected_plugin + b"\n// stale benchmark copy\n")

    assert runner._project_opencode_asset_errors(sut) == [
        "project OpenCode boundary plugin differs from the installed product"
    ]


def test_stale_project_opencode_asset_blocks_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    sut = _make_sut(tmp_path)
    change_id = runner.derive_change_id(item_id=ITEM_ID, stamp=STAMP, nonce=NONCE)
    output = tmp_path / "results" / "stale-plugin"
    fake = _FakeAA(
        sut=sut,
        change_id=change_id,
        terminal=_achieved_status(change_id=change_id),
    )
    _wire_fake(runner, monkeypatch, fake, sut)
    monkeypatch.setattr(
        runner,
        "_project_opencode_asset_errors",
        lambda _project: ["project OpenCode boundary plugin differs from the installed product"],
    )

    code = runner.main(
        [
            "--item",
            ITEM_ID,
            "--adapter",
            "opencode",
            "--output",
            str(output),
            "--nonce",
            NONCE,
            "--stamp",
            STAMP,
        ]
    )

    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert code == 1
    assert evidence["outcome"] == "blocked"
    assert "boundary plugin differs" in evidence["notes"]
    assert all(command[1] != "start" for command in fake.commands if len(command) > 1)


def test_run_scripts_drive_real_adapter_entrypoints() -> None:
    opencode = RUNNER_PATH.parent / "run-opencode.sh"
    assert opencode.is_file()
    assert not (RUNNER_PATH.parent / "run-cursor.sh").exists()
    opencode_text = opencode.read_text(encoding="utf-8")
    assert "--adapter opencode" in opencode_text
    assert "run_item.py" in opencode_text


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _make_git_sut(root: Path) -> Path:
    sut = _make_sut(root)
    (sut / "web" / "node_modules" / ".bin").mkdir(parents=True)
    vite = sut / "web" / "node_modules" / ".bin" / "vite"
    vite.write_text("#!/bin/sh\n", encoding="utf-8")
    vite.chmod(0o755)
    (sut / "web" / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (sut / "migrations").mkdir()
    (sut / "migrations" / ".keep").write_text("keep\n", encoding="utf-8")
    (sut / ".gitignore").write_text("web/node_modules/\nmigrations/\n", encoding="utf-8")
    _git(sut, "init")
    _git(sut, "add", "-A")
    _git(sut, "-c", "user.email=t@t.test", "-c", "user.name=t", "commit", "-m", "init")
    return sut


def test_prepare_sut_worktree_checks_out_outside_the_sut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    sut = _make_git_sut(tmp_path / "source")
    home = tmp_path / "worktrees"
    monkeypatch.setattr(runner, "_sut_worktree_home", lambda _repo: home)
    change_id = runner.derive_change_id(item_id=ITEM_ID, stamp=STAMP, nonce=NONCE)

    worktree = runner._prepare_sut_worktree(repo=tmp_path, sut_root=sut, change_id=change_id)

    assert worktree == (home / sut.name / change_id).resolve()
    assert worktree.is_dir()
    assert not worktree.is_relative_to(sut.resolve())
    assert (worktree / "app").is_dir()
    assert not (worktree / "qa").exists()
    assert (worktree / ".opencode" / "plugins" / "assurance-boundary.mjs").is_file()
    assert (worktree / "opencode.json").read_text(encoding="utf-8") == _opencode_config()
    assert not (worktree / "web" / "node_modules").exists()
    assert (worktree / "migrations" / ".keep").read_text(encoding="utf-8") == "keep\n"
    assert runner._frontend_web_root(worktree) == (sut / "web").resolve()
    listed = _git(sut, "worktree", "list", "--porcelain").stdout
    assert str(worktree) in listed
    assert _git(sut, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != f"bench/{change_id}"
    _git(sut, "worktree", "remove", "--force", str(worktree))


def test_prepare_sut_worktree_refuses_parent_repo_root(tmp_path: Path) -> None:
    runner = _load_runner()
    parent = tmp_path / "aa"
    sut = _make_sut(parent / "benchmark")
    _git(parent, "init")
    _git(parent, "add", "-A")
    _git(parent, "-c", "user.email=t@t.test", "-c", "user.name=t", "commit", "-m", "parent")

    with pytest.raises(SystemExit, match="refuse to worktree the parent repo"):
        runner._prepare_sut_worktree(
            repo=parent,
            sut_root=sut,
            change_id=runner.derive_change_id(item_id=ITEM_ID, stamp=STAMP, nonce=NONCE),
        )


def test_main_without_project_dir_runs_in_a_fresh_sut_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    sut = _make_git_sut(tmp_path / "source")
    leftover = sut / "qa" / ".qa.yaml"
    leftover.parent.mkdir()
    leftover.write_text("change:\n  change_id: BENCH-leftover-dept\n", encoding="utf-8")
    change_id = runner.derive_change_id(item_id=ITEM_ID, stamp=STAMP, nonce=NONCE)
    home = tmp_path / "worktrees"
    worktree = home / sut.name / change_id
    fake = _FakeAA(
        sut=worktree,
        change_id=change_id,
        terminal=_achieved_status(change_id=change_id),
    )
    real_prepare = runner._prepare_sut_worktree
    _wire_fake(runner, monkeypatch, fake, sut)
    monkeypatch.setattr(runner, "_prepare_sut_worktree", real_prepare)
    monkeypatch.setattr(runner, "_sut_worktree_home", lambda _repo: home)

    code = runner.main(
        [
            "--item",
            ITEM_ID,
            "--adapter",
            "opencode",
            "--output",
            str(tmp_path / "output"),
            "--nonce",
            NONCE,
            "--stamp",
            STAMP,
        ]
    )

    evidence = json.loads((tmp_path / "output" / "evidence.json").read_text(encoding="utf-8"))
    assert code == 0
    assert evidence["sut_root"] == str(worktree.resolve())
    assert evidence["change_root"] == str(worktree.resolve() / "qa")
    assert set(fake.project_dirs) == {str(worktree.resolve())}
    assert leftover.read_text(encoding="utf-8") == "change:\n  change_id: BENCH-leftover-dept\n"
    assert not (sut / "qa" / "status.json").exists()
    _git(sut, "worktree", "remove", "--force", str(worktree.resolve()))
