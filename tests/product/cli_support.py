from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from graph_engine.composition import FrozenComposition
from graph_engine.attempts.secret_sources import (
    InvocationRuntimeAuthorization,
    SecretSourceBinding,
    runtime_authorization_digest,
)
from tests.product.composition_harness import InstalledSources
from tests.product.test_product_input import valid_product_input

SECRET_HANDLE = "opencode.token"
SECRET_ENV = "AA_NEXT_OPENCODE_TOKEN"
SECRET_VALUE = "runtime-only-token"


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


def command_names(help_text: str) -> set[str]:
    names: set[str] = set()
    in_commands = False
    for line in help_text.splitlines():
        stripped = line.strip()
        if stripped == "Commands:":
            in_commands = True
            continue
        if in_commands:
            if not stripped:
                break
            names.add(stripped.split()[0])
    return names


def nested_command_names(cli_runner: CliRunner, app: Any, group: str) -> set[str]:
    result = cli_runner.invoke(app, [group, "--help"])
    assert result.exit_code == 0, result.output
    return command_names(result.stdout)


def source_args(installed_sources: InstalledSources, adapter: str = "opencode") -> list[str]:
    deployment = installed_sources.deployments[adapter]
    return [
        "--product",
        f"assurance-{adapter}",
        "--binding-dist",
        deployment.distribution,
        "--binding-entrypoint",
        "deployment",
        "--binding-declaration",
        deployment.declaration_path,
        "--config-tree",
        str(installed_sources.configuration_tree.path),
    ]


def ref_from_composition(composition: FrozenComposition, resource_id: str) -> dict[str, str]:
    entry = composition.registries.resources.entries[resource_id]
    return {"resource_id": resource_id, "sha256": entry.sha256}


def _first_resource_ref(composition: FrozenComposition, *resource_ids: str) -> dict[str, str]:
    for resource_id in resource_ids:
        if resource_id in composition.registries.resources.entries:
            return ref_from_composition(composition, resource_id)
    raise KeyError(resource_ids)


def write_product_input(
    path: Path,
    composition: FrozenComposition,
    **overrides: object,
) -> Path:
    payload = valid_product_input(
        capability_catalog=ref_from_composition(
            composition,
            "assurance.product.configuration.capability-catalog",
        ),
        product_policy=_first_resource_ref(
            composition,
            "assurance.product.configuration.product-policy",
        ),
        data_knowledge=_first_resource_ref(
            composition,
            "assurance.product.configuration.data-knowledge",
        ),
        **overrides,
    )
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_project_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("minimal SUT seed\n", encoding="utf-8")
    (path / "src").mkdir(exist_ok=True)
    (path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    return path


def parse_json_output(output: str) -> dict[str, Any]:
    text = output.strip()
    return json.loads(text)


def scripted_engine_factory(
    *,
    review_decision: str = "pass",
    healing_decision: str = "allowed",
):
    del review_decision, healing_decision

    def factory(root: Path, authorization: InvocationRuntimeAuthorization) -> object:
        del root, authorization
        raise AssertionError("leftover Engine scripted factory was retired; drive Application instead")

    return factory


@dataclass
class LifecycleInvocation:
    engine: object
    id: str
    authorization: InvocationRuntimeAuthorization
    lock_digest: str
    composition: FrozenComposition
    project_dir: Path
    change_id: str
    engine_root: Path


def lifecycle_authorization() -> InvocationRuntimeAuthorization:
    sources = (
        SecretSourceBinding(
            handle=SECRET_HANDLE,
            source_kind="environment",
            source_locator=SECRET_ENV,
        ),
    )
    return InvocationRuntimeAuthorization(
        schema_version="1",
        secret_sources=sources,
        digest=runtime_authorization_digest(sources),
    )


def start_lifecycle_invocation(
    tmp_path: Path,
    installed_sources: InstalledSources,
    *,
    invocation_id: str,
    drive: bool = False,
    require_succeeded: bool = True,
    entrypoint: str = "intake",
    change_id: str = "CH-DEMO-001",
    families: tuple[str, ...] = (),
    extra_project_files: Mapping[str, str] | None = None,
    host_factory=None,
) -> LifecycleInvocation:
    del (
        tmp_path,
        installed_sources,
        invocation_id,
        drive,
        require_succeeded,
        entrypoint,
        change_id,
        families,
        extra_project_files,
        host_factory,
    )
    raise AssertionError("leftover Engine lifecycle start was retired; drive Application instead")


@pytest.fixture
def completed_invocation(
    tmp_path: Path,
    installed_sources: InstalledSources,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[LifecycleInvocation]:
    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    invocation = start_lifecycle_invocation(
        tmp_path,
        installed_sources,
        invocation_id="inv-export-completed",
        drive=True,
    )
    try:
        yield invocation
    finally:
        close = getattr(invocation.engine, "close", None)
        if callable(close):
            close()


def common_lifecycle_args(
    *,
    tmp_path: Path,
    installed_sources: InstalledSources,
    composition: FrozenComposition,
    invocation_id: str,
    entrypoint: str = "intake",
    change_id: str = "CH-DEMO-001",
    families: tuple[str, ...] = (),
    extra: Mapping[str, object] | None = None,
) -> tuple[list[str], Path, str]:
    project_dir = write_project_dir(tmp_path / "project")
    candidate_families = families
    if entrypoint in {"full", "intake"} and not candidate_families:
        candidate_families = ("api",)
    overrides = {
        "candidate_test_families": (candidate_families if entrypoint in {"full", "intake"} else ()),
        "change_id": change_id,
        "case_delta_paths": (
            ("qa/cases/system/dept/case.yaml",)
            if entrypoint in {"full", "intake", "case"}
            else ()
        ),
        **dict(extra or {}),
    }
    input_path = write_product_input(
        tmp_path / "input.json",
        composition,
        **overrides,
    )
    args = [
        "--project-dir",
        str(project_dir),
        "--change",
        change_id,
        "--invocation-id",
        invocation_id,
        *source_args(installed_sources),
        "--entrypoint",
        entrypoint,
        "--input",
        str(input_path),
        "--secret",
        f"{SECRET_HANDLE}=env:{SECRET_ENV}",
        "--json",
    ]
    return args, project_dir, change_id
