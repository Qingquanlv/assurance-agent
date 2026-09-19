from __future__ import annotations

import importlib.util
from importlib.resources import files
from pathlib import Path


def _plugin():
    path = files("assurance_execution").joinpath("resources/runner/aa_observe.py")
    spec = importlib.util.spec_from_file_location("aa_observe", str(path))
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    return plugin


def test_actual_status_not_test_pass_label() -> None:
    plugin = _plugin()
    assert plugin.evaluate_status(expected=423, actual=200) is False
    assert plugin.evaluate_status(expected=423, actual=423) is True


def test_preexisting_output_is_not_overwritten(tmp_path: Path, monkeypatch) -> None:
    plugin = _plugin()
    destination = tmp_path / "collector.json"
    destination.write_text("keep-me", encoding="utf-8")
    monkeypatch.setenv("AA_OBSERVE_OUTPUT", str(destination))

    class _Config:
        _aa_observe = {"context": {}, "observer": None, "collected": [], "errors": [], "tests": {}, "complete": True}

    class _Session:
        config = _Config()

    plugin.pytest_sessionfinish(_Session(), 0)
    assert destination.read_text(encoding="utf-8") == "keep-me"
    assert "output_preexisting" in _Session.config._aa_observe["errors"]
