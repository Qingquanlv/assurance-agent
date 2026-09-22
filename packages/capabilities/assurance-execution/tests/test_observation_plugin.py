from __future__ import annotations

import importlib.util
import copy
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
        _aa_observe = {
            "context": {},
            "observer": None,
            "collected": [],
            "errors": [],
            "tests": {},
            "complete": True,
        }

    class _Session:
        config = _Config()

    plugin.pytest_sessionfinish(_Session(), 0)
    assert destination.read_text(encoding="utf-8") == "keep-me"
    assert "output_preexisting" in _Session.config._aa_observe["errors"]


def _binding_context() -> dict:
    return {
        "identity": {
            "plan_digest": "a" * 64,
            "method_plan_refs": [],
            "mapping_digest": "b" * 64,
            "batch_id": "B-1",
            "baseline_tree_id": "c" * 64,
            "runner_profile_digest": "d" * 64,
        },
        "allowed_origins": ["http://127.0.0.1:9"],
        "timeout_seconds": 2,
        "max_response_bytes": 4096,
        "requirements": [
            {
                "requirement_id": "REQ-1",
                "profile_id": "api-default",
                "prerequisites": [],
                "observations": [
                    {
                        "observation_key": "locked_valid_password",
                        "condition": "valid password after five failures",
                        "predicate": "status_code_eq",
                        "expected": 423,
                        "basis_refs": [],
                    }
                ],
                "semantic_review_required": True,
                "subject_binding_required": False,
            }
        ],
        "method_plans": [
            {
                "mrc_id": "MRC-1",
                "requirement_id": "REQ-1",
                "profile_id": "api-default",
                "case_ids": ["TC-1"],
                "prerequisites": [],
                "steps": [{"step_id": "S1", "purpose": "observe", "action": "post"}],
                "observations": [
                    {
                        "observation_id": "OBS-1",
                        "observation_key": "locked_valid_password",
                        "step_id": "S1",
                        "test_nodeid": "qa/tests/api/test_lockout.py::test_locks",
                        "assertion_id": "A1",
                    }
                ],
            }
        ],
    }


def test_emitted_observation_carries_every_contract_field(tmp_path: Path, monkeypatch) -> None:
    from assurance_execution.contracts.observations import RuntimeObservationV1

    plugin = _plugin()
    observer = plugin._Observer(_binding_context())
    observer.bind_test("qa/tests/api/test_lockout.py::test_locks")
    monkeypatch.setattr(
        plugin.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            plugin.urllib.error.HTTPError("http://127.0.0.1:9/login", 423, "Locked", {}, None)
        ),
    )

    observer.request(observation_id="OBS-1", method="POST", url="http://127.0.0.1:9/login")

    row = RuntimeObservationV1.model_validate(observer.observations[0])
    assert row.mrc_id == "MRC-1"
    assert row.test_nodeid == "qa/tests/api/test_lockout.py::test_locks"
    assert row.assertion_id == "A1"
    assert row.step_id == "S1"
    assert row.actual_status == 423
    assert row.predicate_passed is True
    assert row.sequence_index == 0


def test_same_observation_key_is_scoped_by_requirement(tmp_path: Path, monkeypatch) -> None:
    del tmp_path
    plugin = _plugin()
    context = _binding_context()
    second_requirement = copy.deepcopy(context["requirements"][0])
    second_requirement["requirement_id"] = "REQ-2"
    second_requirement["observations"][0]["expected"] = 200
    second_plan = copy.deepcopy(context["method_plans"][0])
    second_plan["mrc_id"] = "MRC-2"
    second_plan["requirement_id"] = "REQ-2"
    second_plan["observations"][0]["observation_id"] = "OBS-2"
    context["requirements"].append(second_requirement)
    context["method_plans"].append(second_plan)
    observer = plugin._Observer(context)
    observer.bind_test("qa/tests/api/test_lockout.py::test_locks")
    monkeypatch.setattr(
        plugin.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            plugin.urllib.error.HTTPError("http://127.0.0.1:9/login", 423, "Locked", {}, None)
        ),
    )

    observer.request(observation_id="OBS-1", method="POST", url="http://127.0.0.1:9/login")

    assert observer.observations[0]["mrc_id"] == "MRC-1"
    assert observer.observations[0]["predicate_passed"] is True
