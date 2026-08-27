from __future__ import annotations

import importlib.util
import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.product.test_phase5_benchmark_manifest import FULL_WORKFLOW_REQUIRED_STEPS, RUNNER_PATH

ITEM_ID = "opencode-ret-dept-management"
STAMP = "20260826-120000"
NONCE = "a1b2c3d4"
ORIGINAL_TEST = "tests/api/test_existing.py"
ORIGINAL_BYTES = b"assert original_sut_test\n"


def _load_runner():
    spec = importlib.util.spec_from_file_location("phase5_run_item_change_layout", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_sut(root: Path) -> Path:
    sut = root / "sut"
    (sut / "app").mkdir(parents=True)
    (sut / "web").mkdir()
    (sut / "tests" / "api").mkdir(parents=True)
    (sut / "tests" / "api" / "test_existing.py").write_bytes(ORIGINAL_BYTES)
    (sut / ".aa").mkdir()
    (sut / ".aa" / "data-knowledge.yaml").write_text(
        "schema_version: '1'\ncapabilities:\n  domain_factories: {}\n  adapters:\n    api: {}\n",
        encoding="utf-8",
    )
    return sut


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
        "selected_test_families": ["api", "e2e", "fuzz", "performance"],
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
        "publication": {"status": "not_ready"},
    }


def _failed_status(*, change_id: str) -> dict[str, Any]:
    status = _achieved_status(change_id=change_id)
    status["status"] = "failed"
    status["change"] = {"change_id": change_id, "state": "failed"}
    status["terminal_reason"] = "task_failed:execute:external_effect"
    status["node_states"] = []
    return status


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeAA:
    def __init__(
        self,
        *,
        sut: Path,
        change_id: str,
        terminal: Mapping[str, Any],
        export_receipt: Mapping[str, Any] | None,
    ) -> None:
        self.sut = sut
        self.change_id = change_id
        self.terminal = dict(terminal)
        self.export_receipt = export_receipt
        self.commands: list[list[str]] = []
        self.export_calls = 0
        self.project_dirs: list[str] = []

    def _record(self, command: Sequence[str]) -> None:
        recorded = [str(part) for part in command]
        self.commands.append(recorded)
        if "--project-dir" in recorded:
            self.project_dirs.append(recorded[recorded.index("--project-dir") + 1])

    def _materialize_change(self) -> Path:
        change_root = self.sut / "qa" / "changes" / self.change_id
        change_root.mkdir(parents=True, exist_ok=True)
        (change_root / "status.json").write_text(
            json.dumps(self.terminal, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return change_root

    def handle_aa(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self._record(command)
        verb = command[1] if len(command) > 1 else ""
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
        if verb == "export":
            self.export_calls += 1
            if self.export_receipt is None:
                return _completed(1, stderr="export must not run")
            receipt_path = self.sut / "qa" / "changes" / self.change_id / "publish-receipt.json"
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(json.dumps(self.export_receipt, indent=2, sort_keys=True) + "\n")
            return _completed(0, json.dumps(self.export_receipt))
        return _completed(1, stderr=f"unexpected aa command: {command}")

    def handle_subprocess(self, command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded = [str(part) for part in command]
        self.commands.append(recorded)
        joined = " ".join(recorded)
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
    monkeypatch.setattr(runner, "_check_opencode_agent_profiles", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(runner, "_check_opencode_boundary_plugin", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        runner, "_aa_next", lambda binary, *args, **kwargs: fake.handle_aa([str(binary), *args])
    )
    monkeypatch.setattr(runner.subprocess, "run", fake.handle_subprocess)
    monkeypatch.setattr(runner, "_load_opencode_secret", lambda _name: None)
    monkeypatch.setenv("AA_NEXT_OPENCODE_TOKEN", "test-token")


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


def test_success_uses_real_sut_change_and_exports_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    sut = _make_sut(tmp_path)
    change_id = runner.derive_change_id(item_id=ITEM_ID, stamp=STAMP, nonce=NONCE)
    output = tmp_path / "results" / "opencode-run"
    receipt = {
        "schema_version": "1",
        "change_id": change_id,
        "status": "published",
    }
    fake = _FakeAA(
        sut=sut,
        change_id=change_id,
        terminal=_achieved_status(change_id=change_id),
        export_receipt=receipt,
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

    change_root = sut / "qa" / "changes" / change_id
    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert code == 0
    assert fake.export_calls == 1
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
    assert (change_root / "publish-receipt.json").is_file()
    assert evidence["sut_root"] == str(sut)
    assert evidence["change_id"] == change_id
    assert evidence["change_root"] == str(change_root)
    assert evidence["terminal_status"] == "completed"
    assert evidence["publish_receipt"]["change_id"] == change_id
    assert evidence["provider"]["session"] or evidence["provider"]["process"]
    assert Path(evidence["logs"]["run_log"]).is_file()
    assert evidence.get("result_tree_digest") is None
    assert "auto_archive" not in evidence


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
        export_receipt=None,
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
    assert fake.export_calls == 0
    assert (sut / ORIGINAL_TEST).read_bytes() == ORIGINAL_BYTES
    assert not (output / "project").exists()
    assert not (output / "export").exists()
    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["change_id"] == change_id
    assert evidence["change_root"] == str(sut / "qa" / "changes" / change_id)
    assert evidence["terminal_status"] == "failed"
    assert evidence.get("publish_receipt") in (None, {})


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
        export_receipt=None,
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


def test_run_scripts_drive_real_adapter_entrypoints() -> None:
    opencode = RUNNER_PATH.parent / "run-opencode.sh"
    cursor = RUNNER_PATH.parent / "run-cursor.sh"
    assert opencode.is_file()
    assert cursor.is_file()
    opencode_text = opencode.read_text(encoding="utf-8")
    cursor_text = cursor.read_text(encoding="utf-8")
    assert "--adapter opencode" in opencode_text
    assert "--adapter cursor" in cursor_text
    assert "run_item.py" in opencode_text
    assert "run_item.py" in cursor_text
