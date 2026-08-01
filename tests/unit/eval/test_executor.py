from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.helpers_aa import write_aa_config

from assurance_agent.eval.executor import execute_attempt
from assurance_agent.eval.selection import SELECTION_NORMALIZER_VERSION, normalize_selected_layers
from assurance_agent.eval.types import DatasetSample
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)


class FakeInvoker:
    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []

    def invoke(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        return AgentResult(ok=True)


def test_execute_in_process_writes_evidence_without_change_id(tmp_path: Path) -> None:
    sample = DatasetSample(
        id="FC-001",
        suite="classification-unit",
        input={"message": "x", "log_excerpt": "y", "target": "e2e"},
        expected={"category": "locator_failure"},
    )
    attempt = tmp_path / "attempt-0"
    result = execute_attempt(
        sample,
        attempt,
        suite="classification-unit",
        sut_dir=tmp_path / "sut",
        adapter=object(),
        executor_type="in_process",
    )
    assert result.status == "ok"
    assert result.executor == "in_process"
    assert (attempt / "stdout.log").is_file()
    assert (attempt / "stderr.log").is_file()
    assert (attempt / "execution.json").is_file()
    assert (attempt / "raw-output" / ".in-process").is_file()


def test_execute_attempt_copies_sut_tests_into_raw_output(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    sut = tmp_path / "sut"
    write_aa_config(sut)
    change_dir = sut / "qa" / "changes" / "eval-sample-001"
    change_dir.mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text(
        "phases:\n  skill_registry_check: {status: pass}\n"
        "run_context: {interaction_mode: autonomous, orchestrator_skill: aa-workflow}\n",
        encoding="utf-8",
    )
    tests = sut / "tests" / "api"
    tests.mkdir(parents=True)
    (tests / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    for required in ("config.py", "conftest.py", "schema_validation.py"):
        (sut / "tests" / required).write_text("# bootstrap\n", encoding="utf-8")
    _git_init(sut)

    sample = DatasetSample(
        id="WAC-001",
        suite="workflow-api-codegen",
        input={"change_id": "eval-sample-001"},
        expected={},
    )
    attempt = tmp_path / "run" / "WAC-001" / "attempt-0"

    # Runtime may fail without full graph setup; still copy artifacts after.
    result = execute_attempt(
        sample,
        attempt,
        suite="workflow-api-codegen",
        sut_dir=sut,
        adapter=FakeInvoker(),
        entrypoint="execute",
        run_mode="codegen-only",
        selected_layers=("api",),
        run_tests=False,
    )
    assert (attempt / "raw-output" / "tests" / "api" / "test_a.py").is_file()
    assert (attempt / "execution.json").exists()
    assert result.exit_code is not None


def test_execute_attempt_records_runtime_error(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    sut = tmp_path / "sut"
    write_aa_config(sut)
    (sut / "qa" / "changes" / "eval-sample-002").mkdir(parents=True)
    _git_init(sut)
    sample = DatasetSample(
        id="WC-002", suite="workflow-case", input={"change_id": "eval-sample-002"}, expected={}
    )
    attempt = tmp_path / "run" / "s" / "WC-002" / "attempt-0"

    result = execute_attempt(
        sample,
        attempt,
        suite="workflow-case",
        sut_dir=sut,
        adapter=FakeInvoker(),
        entrypoint="case",
        run_mode="case-only",
        selected_layers=("api", "e2e"),
    )
    assert result.status == "error"
    assert result.exit_code != 0


def test_execute_attempt_list_form_selection_in_execution_json(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    sut = tmp_path / "sut"
    write_aa_config(sut)
    (sut / "qa" / "changes" / "eval-sample-001").mkdir(parents=True)
    _git_init(sut)
    sample = DatasetSample(
        id="LF-001", suite="workflow-case", input={"change_id": "eval-sample-001"}, expected={}
    )
    attempt = tmp_path / "run" / "LF-001" / "attempt-0"
    layers = normalize_selected_layers(test_types=["fuzz", "api"])

    class _Bundle:
        def __init__(self) -> None:
            self.params: list | None = None

        def run(self, compiled, entrypoint, context):  # noqa: ANN001, ANN201
            self.params = list(context.params.get("test_types"))
            return type("R", (), {"exit_code": 0, "reason": "ok"})()

    bundle_holder: dict = {}

    def factory(**kwargs):  # noqa: ANN003, ANN202
        runtime = _Bundle()
        bundle_holder["runtime"] = runtime
        return type("B", (), {"runtime": runtime, "compiled": object()})()

    result = execute_attempt(
        sample,
        attempt,
        suite="workflow-case",
        sut_dir=sut,
        adapter=FakeInvoker(),
        entrypoint="case",
        runtime_factory=factory,
        run_mode="case-only",
        selected_layers=layers,
        run_tests=False,
    )
    assert result.selected_layers == ("api", "fuzz")
    assert result.selection_normalizer_version == SELECTION_NORMALIZER_VERSION
    assert bundle_holder["runtime"].params == ["api", "fuzz"]
    payload = json.loads((attempt / "execution.json").read_text(encoding="utf-8"))
    assert payload["selected_layers"] == ["api", "fuzz"]
    assert payload["runtime_params"]["test_types"] == ["api", "fuzz"]


def test_execute_attempt_rejects_missing_selected_layers(tmp_path: Path) -> None:
    sample = DatasetSample(id="X", suite="workflow-case", input={"change_id": "c"}, expected={})
    with pytest.raises(AaError, match="requires selected_layers"):
        execute_attempt(
            sample,
            tmp_path / "a",
            suite="workflow-case",
            sut_dir=tmp_path,
            adapter=FakeInvoker(),
        )
