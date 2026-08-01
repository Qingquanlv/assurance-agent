"""Complete selection normalization table and list-form single-resolution tests."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models.assurance import LAYER_NAMES
from assurance_agent.eval.executor import execute_attempt
from assurance_agent.eval.runner import run_suite
from assurance_agent.eval.selection import (
    MISSING,
    SELECTION_NORMALIZER_VERSION,
    SelectionError,
    normalize_selected_layers,
)
from assurance_agent.eval.types import DatasetSample
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult


def _all_nonempty_subsets() -> list[tuple[str, ...]]:
    layers = list(LAYER_NAMES)
    out: list[tuple[str, ...]] = []
    for width in range(1, len(layers) + 1):
        for combo in itertools.combinations(layers, width):
            out.append(tuple(combo))
    assert len(out) == 15
    return out


@pytest.mark.parametrize("subset", _all_nonempty_subsets())
def test_normalize_selected_layers_all_subsets_and_reverse(subset: tuple[str, ...]) -> None:
    reversed_subset = tuple(reversed(subset))
    assert normalize_selected_layers(test_types=list(subset)) == subset
    assert normalize_selected_layers(test_types=list(reversed_subset)) == subset
    assert normalize_selected_layers(test_types=",".join(reversed_subset)) == subset
    assert normalize_selected_layers(test_type=",".join(reversed_subset)) == subset


def test_normalize_selected_layers_omitted_defaults_api_e2e() -> None:
    assert normalize_selected_layers() == ("api", "e2e")
    assert normalize_selected_layers(test_type=MISSING, test_types=MISSING) == ("api", "e2e")


def test_normalize_selected_layers_single_scalar() -> None:
    assert normalize_selected_layers(test_type="fuzz") == ("fuzz",)
    assert normalize_selected_layers(test_types="performance") == ("performance",)


def test_normalize_selected_layers_comma_scalar() -> None:
    assert normalize_selected_layers(test_types="e2e,api,performance") == ("api", "e2e", "performance")


def test_normalize_selected_layers_yaml_list() -> None:
    assert normalize_selected_layers(test_types=["performance", "api"]) == ("api", "performance")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        [],
        None,
        [""],
        "   ",
    ],
)
def test_normalize_selected_layers_falsey_explicit_never_defaults(raw: object) -> None:
    with pytest.raises(SelectionError, match="non-empty"):
        normalize_selected_layers(test_types=raw)
    with pytest.raises(SelectionError, match="non-empty"):
        normalize_selected_layers(test_type=raw)


def test_normalize_selected_layers_duplicate_and_unknown_and_both_keys() -> None:
    with pytest.raises(SelectionError, match="duplicate"):
        normalize_selected_layers(test_types=["api", "api"])
    with pytest.raises(SelectionError, match="unknown"):
        normalize_selected_layers(test_types="web")
    with pytest.raises(SelectionError, match="both test_type and test_types"):
        normalize_selected_layers(test_type="api", test_types=["e2e"])


class _CaptureRuntime:
    def __init__(self) -> None:
        self.params: dict[str, object] | None = None
        self.invocation_id = "inv-list-form"

    def run(self, compiled, entrypoint, context):  # noqa: ANN001, ANN201
        self.params = dict(context.params)
        return type("R", (), {"exit_code": 0, "reason": "ok"})()

    def import_checkpoint(self, compiled, manifest, context):  # noqa: ANN001, ANN201
        raise AssertionError("unexpected import")

    def status(self, invocation_id):  # noqa: ANN001, ANN201
        raise AssertionError("unexpected status")


class _Invoker:
    def invoke(self, request: AgentRequest) -> AgentResult:
        return AgentResult(ok=True)


def test_list_form_selection_single_resolution_through_executor(tmp_path: Path) -> None:
    from tests.helpers_aa import write_aa_config
    import subprocess

    sut = tmp_path / "sut"
    write_aa_config(sut)
    (sut / "qa" / "changes" / "eval-sample-001").mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=sut, check=True, capture_output=True)

    runtime = _CaptureRuntime()

    def factory(**kwargs):  # noqa: ANN003, ANN202
        return type("B", (), {"runtime": runtime, "compiled": object()})()

    sample = DatasetSample(
        id="LF-001",
        suite="workflow-case",
        input={"change_id": "eval-sample-001"},
        expected={},
    )
    attempt = tmp_path / "attempt-0"
    layers = normalize_selected_layers(test_types=["performance", "api", "e2e"])
    result = execute_attempt(
        sample,
        attempt,
        suite="workflow-case",
        sut_dir=sut,
        adapter=_Invoker(),
        entrypoint="case",
        runtime_factory=factory,
        run_mode="case-only",
        selected_layers=layers,
        run_tests=False,
    )
    assert layers == ("api", "e2e", "performance")
    assert result.selected_layers == layers
    assert result.selection_normalizer_version == SELECTION_NORMALIZER_VERSION
    assert runtime.params is not None
    assert runtime.params["test_types"] == list(layers)
    execution = json.loads((attempt / "execution.json").read_text(encoding="utf-8"))
    assert execution["selected_layers"] == list(layers)
    assert execution["selection_normalizer_version"] == SELECTION_NORMALIZER_VERSION
    assert execution["runtime_params"]["test_types"] == list(layers)


def test_list_form_selection_single_resolution_through_run_suite(tmp_path: Path, monkeypatch) -> None:
    from tests.helpers_aa import write_aa_config
    import subprocess

    project = tmp_path / "project"
    sut = project / "sut"
    write_aa_config(sut)
    (sut / "qa" / "changes" / "eval-sample-001").mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=sut, check=True, capture_output=True)

    suites = project / "eval" / "suites"
    suites.mkdir(parents=True)
    suite_file = suites / "workflow-case.yaml"
    suite_file.write_text(
        yaml.safe_dump(
            {
                "name": "workflow-case",
                "scorer": "workflow-case",
                "executor": {
                    "type": "workflow-run",
                    "entrypoint": "case",
                    "run_mode": "case-only",
                    "test_types": ["performance", "api"],
                    "run_tests": False,
                },
                "thresholds": [],
            }
        ),
        encoding="utf-8",
    )
    ds = project / "eval" / "datasets" / "workflow-case"
    ds.mkdir(parents=True)
    (ds / "LF-001.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "LF-001",
                "suite": "workflow-case",
                "input": {"change_id": "eval-sample-001"},
                "expected": {},
            }
        ),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    def fake_execute(sample, attempt_dir, **kwargs):  # noqa: ANN001, ANN003, ANN202
        from assurance_agent.eval.types import ExecutionResult

        captured["selected_layers"] = kwargs.get("selected_layers")
        attempt_dir.mkdir(parents=True, exist_ok=True)
        raw = attempt_dir / "raw-output"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "review").mkdir(parents=True, exist_ok=True)
        (raw / "review" / "case-review.json").write_text('{"decision":"pass"}', encoding="utf-8")
        (attempt_dir / "stdout.log").write_text("ok\n", encoding="utf-8")
        (attempt_dir / "stderr.log").write_text("", encoding="utf-8")
        layers = kwargs["selected_layers"]
        (attempt_dir / "execution.json").write_text(
            json.dumps(
                {
                    "exit_code": 0,
                    "selected_layers": list(layers),
                    "selection_normalizer_version": SELECTION_NORMALIZER_VERSION,
                    "runtime_params": {"test_types": list(layers)},
                }
            ),
            encoding="utf-8",
        )
        return ExecutionResult(
            sample_id=sample.id,
            attempt=0,
            executor="workflow-run",
            status="ok",
            exit_code=0,
            selected_layers=layers,
            selection_normalizer_version=SELECTION_NORMALIZER_VERSION,
        )

    monkeypatch.setattr("assurance_agent.eval.runner.execute_attempt", fake_execute)
    run_id, _gate = run_suite(
        suite_file=suite_file,
        project_root=project,
        sut_dir=sut,
        adapter_factory=lambda **kwargs: _Invoker(),
    )
    assert captured["selected_layers"] == ("api", "performance")
    attempt = next((project / "sut" / "eval" / "out" / "runs" / run_id).rglob("execution.json"))
    payload = json.loads(attempt.read_text(encoding="utf-8"))
    assert payload["selected_layers"] == ["api", "performance"]
    assert payload["runtime_params"]["test_types"] == ["api", "performance"]


def test_execute_attempt_rejects_unresolved_test_types_kwarg(tmp_path: Path) -> None:
    sample = DatasetSample(id="X", suite="workflow-case", input={"change_id": "c"}, expected={})
    with pytest.raises(Exception, match="rejected unresolved selection"):
        execute_attempt(
            sample,
            tmp_path / "a",
            suite="workflow-case",
            sut_dir=tmp_path,
            adapter=_Invoker(),
            test_types="api",  # type: ignore[call-arg]
        )
