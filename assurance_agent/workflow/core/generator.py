"""Write the .aa/ + qa/ + tests/ scaffold into a target project."""

import json
from pathlib import Path

from pydantic import BaseModel

from assurance_agent.config import CONFIG_RELPATH, ConfigNotFoundError
from assurance_agent.workflow.core.templates import (
    InitAnswers,
    build_config_yaml,
    build_data_knowledge_yaml,
    build_execution_policy,
    build_minimal_workflow_schema_yaml,
    build_module_map_yaml,
)

GITKEEP_DIRS = [
    "qa/cases",
    "qa/changes",
    "qa/archive",
    "tests/api",
    "tests/api/adapters",
    "tests/e2e",
    "tests/e2e/adapters",
    "tests/fuzz/adapters",
    "tests/fuzz/strategies",
    "tests/perf/adapters",
    "tests/testdata/domain",
    "tests/helpers",
    "tests/reports",
]


class GenerateResult(BaseModel):
    created: list[str] = []
    skipped: list[str] = []


def _write(root: Path, relpath: str, content: str, result: GenerateResult, overwrite: bool) -> None:
    path = root / relpath
    if path.exists() and not overwrite:
        result.skipped.append(relpath)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    result.created.append(relpath)


def generate_project(root: Path, answers: InitAnswers) -> GenerateResult:
    result = GenerateResult()
    _write(root, ".aa/config.yaml", build_config_yaml(answers), result, overwrite=True)
    policy = json.dumps(build_execution_policy(answers), indent=2) + "\n"
    _write(root, ".aa/execution-policy.json", policy, result, overwrite=True)
    _write(root, ".aa/module-map.yaml", build_module_map_yaml(), result, overwrite=True)
    # Human-maintained knowledge base - never overwrite a filled-in file.
    _write(root, ".aa/data-knowledge.yaml", build_data_knowledge_yaml(), result, overwrite=False)
    if answers.with_schema:
        _write(
            root,
            ".aa/workflow-schema.yaml",
            build_minimal_workflow_schema_yaml(),
            result,
            overwrite=False,
        )
    for rel in GITKEEP_DIRS:
        _write(root, f"{rel}/.gitkeep", "", result, overwrite=False)
    return result


def repair_project(root: Path) -> GenerateResult:
    if not (root / CONFIG_RELPATH).is_file():
        raise ConfigNotFoundError(f"{CONFIG_RELPATH} not found. Run `aa init` first.")
    result = GenerateResult()
    for rel in GITKEEP_DIRS:
        _write(root, f"{rel}/.gitkeep", "", result, overwrite=False)
    _write(root, ".aa/module-map.yaml", build_module_map_yaml(), result, overwrite=False)
    _write(root, ".aa/data-knowledge.yaml", build_data_knowledge_yaml(), result, overwrite=False)
    return result
