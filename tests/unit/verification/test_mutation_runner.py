"""Injectable mutmut seam (§5-B1): discover + per-mutant test, no real binary required."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from assurance_agent.verification.mutation_runner import (
    MutantOutcome,
    MutantResult,
    MutationToolError,
    SubprocessMutmutRunner,
    candidate_from_mutmut_row,
    parse_mutmut_candidates,
    parse_mutmut_results_text,
)
from assurance_agent.verification.mutation_sampling import MutantCandidate, SelectedMutant


def test_mutant_result_locator_is_module_line_operator_id() -> None:
    selected = SelectedMutant(module="app/svc.py", line=10, operator="AOR", mutant_id="m1")
    result = MutantResult(mutant=selected, outcome="survived")
    assert result.locator == "app/svc.py:10:AOR:m1"


def test_mutation_tool_error_kinds_are_closed() -> None:
    err = MutationToolError(kind="start_failed", detail="mutmut missing")
    assert err.kind == "start_failed"
    with pytest.raises(ValueError):
        MutationToolError(kind="bogus", detail="x")  # type: ignore[arg-type]


def test_subprocess_runner_is_a_mutation_tool() -> None:
    runner = SubprocessMutmutRunner(project_root=".")
    assert hasattr(runner, "discover")
    assert hasattr(runner, "test")


def test_outcome_vocabulary_is_closed() -> None:
    allowed: set[MutantOutcome] = {"killed", "survived", "equivalent", "error"}
    assert allowed == {"killed", "survived", "equivalent", "error"}


def test_candidate_from_mutmut_row_round_trips() -> None:
    row = {"module": "app/a.py", "line": 3, "operator": "ROR", "id": "42"}
    candidate = candidate_from_mutmut_row(row)
    assert candidate == MutantCandidate(
        module="app/a.py",
        line=3,
        operator="ROR",
        mutant_id="42",
    )


def test_parse_mutmut_candidates_rejects_non_mapping_items() -> None:
    with pytest.raises(MutationToolError) as exc:
        parse_mutmut_candidates(
            json.dumps([{"module": "a.py", "line": 1, "operator": "AOR", "id": "1"}, "bad"])
        )
    assert exc.value.kind == "output_corrupt"


def test_parse_mutmut_results_text_maps_mutmut_37_lines(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "svc.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef is_positive(x):\n    return x > 0\n",
        encoding="utf-8",
    )
    text = "    app.svc.x_add__mutmut_1: killed\n    app.svc.x_is_positive__mutmut_2: not checked\n"
    candidates = parse_mutmut_results_text(text, project_root=tmp_path)
    assert {c.mutant_id for c in candidates} == {
        "app.svc.x_add__mutmut_1",
        "app.svc.x_is_positive__mutmut_2",
    }
    by_id = {c.mutant_id: c for c in candidates}
    assert by_id["app.svc.x_add__mutmut_1"].module == "app/svc.py"
    assert by_id["app.svc.x_add__mutmut_1"].line == 1
    assert by_id["app.svc.x_is_positive__mutmut_2"].line == 4
    assert by_id["app.svc.x_add__mutmut_1"].operator == "mutmut"


def test_discover_argv_uses_results_all_not_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(list(argv))
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout="    app.svc.x_add__mutmut_1: killed\n",
            stderr="",
        )

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "svc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    monkeypatch.setattr("assurance_agent.verification.mutation_runner.subprocess.run", fake_run)
    runner = SubprocessMutmutRunner(project_root=tmp_path, mutmut_bin="mutmut")
    candidates = runner.discover(["app/svc.py"])
    assert calls, "discover must invoke subprocess"
    argv = calls[0]
    assert argv[:2] == ["mutmut", "results"]
    assert "--json" not in argv
    assert "--all" in argv
    assert any(flag in {"true", "True", "1"} or flag.startswith("--all=") for flag in argv)
    assert len(candidates) == 1
    assert candidates[0].mutant_id == "app.svc.x_add__mutmut_1"


def test_discover_reads_on_disk_meta_without_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "app").mkdir(parents=True)
    (tmp_path / "app" / "svc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    meta_dir = tmp_path / "mutants" / "app"
    meta_dir.mkdir(parents=True)
    (meta_dir / "svc.py.meta").write_text(
        json.dumps(
            {
                "exit_code_by_key": {
                    "app.svc.x_add__mutmut_1": 1,
                    "app.svc.x_add__mutmut_2": None,
                },
                "hash_by_function_name": {},
                "type_check_error_by_key": {},
                "durations_by_key": {},
                "estimated_durations_by_key": {},
            }
        ),
        encoding="utf-8",
    )

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("subprocess must not be required when .meta exists")

    monkeypatch.setattr("assurance_agent.verification.mutation_runner.subprocess.run", boom)
    runner = SubprocessMutmutRunner(project_root=tmp_path)
    candidates = runner.discover(["app/svc.py"])
    assert {c.mutant_id for c in candidates} == {
        "app.svc.x_add__mutmut_1",
        "app.svc.x_add__mutmut_2",
    }


def test_discover_empty_results_is_empty_not_start_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("assurance_agent.verification.mutation_runner.subprocess.run", fake_run)
    runner = SubprocessMutmutRunner(project_root=tmp_path)
    assert runner.discover(["app/svc.py"]) == ()


def test_test_argv_is_mutmut_run_mutant_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(list(argv))
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout="Mutant results\n--------------\n🎉 app.svc.x_add__mutmut_1\n",
            stderr="",
        )

    monkeypatch.setattr("assurance_agent.verification.mutation_runner.subprocess.run", fake_run)
    runner = SubprocessMutmutRunner(project_root=tmp_path)
    selected = SelectedMutant(
        module="app/svc.py",
        line=1,
        operator="mutmut",
        mutant_id="app.svc.x_add__mutmut_1",
    )
    result = runner.test(selected, timeout_seconds=5.0)
    assert calls[0] == ["mutmut", "run", "app.svc.x_add__mutmut_1"]
    assert result.outcome == "killed"


def test_test_survived_emoji_not_confused_with_cli_exit_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout="Mutant results\n--------------\n🙁 app.svc.x_add__mutmut_1\n",
            stderr="",
        )

    monkeypatch.setattr("assurance_agent.verification.mutation_runner.subprocess.run", fake_run)
    runner = SubprocessMutmutRunner(project_root=tmp_path)
    selected = SelectedMutant(
        module="app/svc.py", line=1, operator="mutmut", mutant_id="app.svc.x_add__mutmut_1"
    )
    assert runner.test(selected, timeout_seconds=5.0).outcome == "survived"


def test_installed_mutmut_help_has_no_results_json_flag() -> None:
    """Cheap smoke: installed mutmut 3.7.x must not advertise a fake --json flag."""
    proc = subprocess.run(
        ["mutmut", "results", "--help"],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    assert proc.returncode == 0
    blob = f"{proc.stdout}\n{proc.stderr}"
    assert "--json" not in blob
    assert "--all" in blob
