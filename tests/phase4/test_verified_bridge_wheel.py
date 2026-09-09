"""A fresh runtime install must execute the installed bridge without dev groups."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from tests.phase4.wheel_isolation import (
    _build_wheel,
    _create_venv,
    _distributions_to_build,
    _install_wheels,
    _source_root,
)


def test_verified_bridge_runs_from_clean_installed_runtime_wheels(tmp_path: Path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    distributions = _distributions_to_build("assurance-execution")
    for name in distributions:
        _build_wheel(_source_root(name), wheels)
    environment = tmp_path / "runtime"
    _create_venv(environment)
    python = environment / "bin/python"
    # Only the published runtime wheel dependency closure is installed. No dev
    # extras, dependency groups, editable sources, pytest or report CLI installs.
    _install_wheels(wheels, python, distributions)
    view = tmp_path / "attempt/view"
    (view / "tests").mkdir(parents=True)
    (view / "tests/test_case.py").write_text(
        'from assurance_execution.bridge import execute_case\ndef test_case():\n    execute_case("case")\n'
    )
    probe = tmp_path / "probe.py"
    probe.write_text("""from pathlib import Path
import json, sys
from importlib import metadata, util
import assurance_execution
from assurance_execution.operations.verified_process import SubprocessVerificationHost
assert Path(assurance_execution.__file__).is_relative_to(Path(sys.prefix))
assert util.find_spec("ruff") is None
calls = []
receipt = SubprocessVerificationHost().run(
    view=Path.cwd(), nodeid="tests/test_case.py::test_case", case_id="case", container_name="unused",
    execute=lambda case, control: calls.append(case), cancel_requested=lambda: False,
)
print(json.dumps({"receipt": receipt.model_dump(mode="json"), "calls": calls,
    "runtime_requirements": metadata.requires("assurance-execution")}))
""")
    result = subprocess.run(
        [str(python), str(probe)],
        cwd=view,
        env={"PATH": os.defpath, "PYTHONNOUSERSITE": "1"},
        capture_output=True,
        text=True,
        timeout=90,
        check=True,
    )
    output = json.loads(result.stdout)
    receipt = output["receipt"]
    assert receipt["reason"] is None, receipt
    assert receipt["exit_code"] == 0
    assert receipt["cleanup_confirmed"]
    assert receipt["command"] == [str(python), "-m", "assurance_execution.bridge_runner"]
    assert output["calls"] == ["case"]
    assert "pytest==9.1.1" in output["runtime_requirements"]
    assert "pytest-json-report==1.5.0" in output["runtime_requirements"]
