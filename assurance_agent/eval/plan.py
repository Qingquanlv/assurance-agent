from __future__ import annotations

import json
from pathlib import Path

import yaml

from assurance_agent.eval.paths import eval_root
from assurance_agent.eval.types import EvalSuite
from assurance_agent.exceptions import AaError


def load_suite(project_root: Path, suite_name: str) -> tuple[EvalSuite, Path]:
    path = eval_root(project_root) / "suites" / f"{suite_name}.yaml"
    if not path.exists():
        raise AaError(f"suite not found: {suite_name} ({path})")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return EvalSuite.model_validate(data), path


def load_suite_file(suite_file: Path) -> EvalSuite:
    data = yaml.safe_load(suite_file.read_text(encoding="utf-8"))
    return EvalSuite.model_validate(data)


def generate_plan(event: str, changed_files: list[str] | None, suite: str | None) -> dict:
    suites: list[str] = []
    if suite:
        suites.append(suite)
    return {"event": event, "changed_files": changed_files or [], "suites": suites}


def write_plan(plan: dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")


def read_plan(path: Path) -> dict:
    if not path.exists():
        raise AaError(f"plan not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))
