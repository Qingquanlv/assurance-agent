from __future__ import annotations

from importlib import metadata
from importlib.resources import files
from pathlib import Path
import sys
from typing import cast
import zipfile

import pytest

from graph_engine.canonical import canonical_json_bytes
from graph_engine.composition.workflow_assembler import assemble_product_workflow
from graph_engine.plugin_api import InvocationWorkspaceBinding
from graph_engine.runtime.engine import Engine, EngineError
from graph_engine.runtime.invocation_lock import InvocationDrift
from graph_engine.runtime.secret_sources import empty_runtime_authorization
from graph_engine.runtime.seed import empty_invocation_seed

from tests.product.composition_harness import request_for
from tests.product.product_runner import ProductRun, resolve_product_workflow_composition

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PRODUCT_WORKFLOW_DIR = (
    _REPO_ROOT / "packages/products/assurance-product/assurance_product/resources/workflow"
)
_PRE_MODULAR_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "assurance-full-pre-modular.yaml"
_RUNBOOK = _REPO_ROOT / "docs/runbooks/assurance-modular-workflow-rollout.md"
_SOURCE_EXTENSION_KEYS = (
    "module",
    "distribution",
    "entrypoint",
    "path",
    "export",
    "capability",
    "schema",
    "implementation",
)
_PUBLIC_BOUNDARY_ACTIONS = (
    "approve",
    "reject",
    "request_rework",
    "supersede",
)
_PUBLIC_BOUNDARY_ENUMS = (
    ("review_decision", "pass"),
    ("review_decision", "needs_fix"),
    ("review_decision", "needs_human_review"),
    ("review_decision", "reject"),
    ("healing_decision", "allowed"),
    ("healing_decision", "disallowed"),
    ("execution_status", "passed"),
    ("execution_status", "failed"),
    ("execution_status", "product_issue"),
    ("execution_status", "infrastructure_failure"),
)
_PLANNED_TERMINALS = frozenset({"completed", "interrupted", "stopped", "failed", "succeeded"})


def _assembly_bytes(installed_sources) -> bytes:
    from assurance_product.product import resolve_assurance_composition

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    assembled = assemble_product_workflow(
        manifest=composition.manifest,
        descriptors={item.plugin_id: item for item in composition.descriptors},
        registries=composition.registries,
    )
    return canonical_json_bytes(assembled.model_dump(mode="json", by_alias=True, exclude_unset=True))


def _workspace_binding(root: Path) -> InvocationWorkspaceBinding:
    project = root / "project"
    attempts = root / "attempts"
    receipts = root / "receipts"
    for path in (project, attempts, receipts):
        path.mkdir(parents=True, exist_ok=True)
    return InvocationWorkspaceBinding(
        project_root=project,
        attempts_root=attempts,
        receipts_root=receipts,
    )


def _ledger_bytes(engine_root: Path, invocation_id: str) -> bytes:
    ledger = engine_root / "invocations" / invocation_id / "ledger"
    chunks = bytearray()
    for path in sorted(ledger.rglob("*")):
        if path.is_file():
            chunks.extend(path.relative_to(ledger).as_posix().encode())
            chunks.extend(path.read_bytes())
    return bytes(chunks)


def test_product_package_excludes_monolith_and_keeps_main_workflow() -> None:
    workflow_root = files("assurance_product").joinpath("resources/workflow")
    names = {item.name for item in workflow_root.iterdir() if item.is_file()}
    assert names == {"main.yaml"}
    assert not (_PRODUCT_WORKFLOW_DIR / "assurance-full.yaml").exists()
    assert _PRE_MODULAR_FIXTURE.is_file()


def test_pre_modular_loader_reads_only_the_test_fixture() -> None:
    import inspect

    from assurance_product import product as product_mod

    source_text = (_REPO_ROOT / "packages/products/assurance-product/assurance_product/product.py").read_text(
        encoding="utf-8"
    )
    assert "load_canonical_workflow" not in source_text
    resolver_source = inspect.getsource(product_mod._resolve_pre_modular_workflow_path)
    assert "cwd" not in resolver_source
    assert "Path.cwd" not in resolver_source
    assert product_mod._PRODUCT_MODULE_PATH.name == "main.yaml"
    assert product_mod._PRODUCT_MODULE_PATH.is_file()
    assert product_mod._PRODUCT_MODULE_PATH.resolve() != _PRE_MODULAR_FIXTURE.resolve()
    resolved = product_mod._resolve_pre_modular_workflow_path()
    if resolved is not None:
        assert resolved == _PRE_MODULAR_FIXTURE
        assert "resources/workflow/assurance-full.yaml" not in str(resolved)
    workflow = product_mod.load_pre_modular_workflow(_PRE_MODULAR_FIXTURE)
    module = product_mod.load_product_workflow_module()
    assert workflow.name == "assurance"
    assert module.module_id == "assurance.product.workflow"
    assert module.role == "product"
    assert product_mod._PRODUCT_MODULE_PATH.read_text(encoding="utf-8") != _PRE_MODULAR_FIXTURE.read_text(
        encoding="utf-8"
    )
    assert not hasattr(product_mod, "load_canonical_workflow")
    assert len(workflow.entrypoints) == 14


def test_pre_modular_loader_ignores_a_cwd_decoy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from assurance_product import product as product_mod

    decoy = tmp_path / "tests/product/fixtures/assurance-full-pre-modular.yaml"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("schema_version: '1'\nname: decoy\nentrypoints: {}\ngraphs: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    resolved = product_mod._resolve_pre_modular_workflow_path()
    assert resolved != decoy.resolve()
    if resolved is not None:
        assert resolved == _PRE_MODULAR_FIXTURE
        assert product_mod.load_pre_modular_workflow().name == "assurance"
    else:
        with pytest.raises(FileNotFoundError):
            product_mod.load_pre_modular_workflow()
    assert product_mod.load_pre_modular_workflow(_PRE_MODULAR_FIXTURE).name == "assurance"


def test_release_identities_are_the_modular_0_2_0_cut() -> None:
    from assurance_product.models import CONFIGURATION_PLUGIN_VERSION, PLUGIN_VERSION
    from assurance_product.product import _PRODUCT_VERSION
    from assurance_product.source_catalog import product_source_catalog

    assert _PRODUCT_VERSION == "0.2.0"
    assert PLUGIN_VERSION == "1.1.0"
    assert CONFIGURATION_PLUGIN_VERSION == "1.0.0"
    catalog = product_source_catalog("opencode")
    feature_versions = {
        source.distribution: source.version
        for source in catalog
        if source.distribution.startswith("assurance-")
    }
    assert feature_versions == {
        "assurance-intake": "0.2.0",
        "assurance-generation": "0.2.0",
        "assurance-execution": "0.2.0",
        "assurance-healing": "0.2.0",
        "assurance-quality": "0.2.0",
        "assurance-improvement": "0.2.0",
    }
    runtime = next(source for source in catalog if source.distribution == "agent-runtime-opencode")
    assert runtime.version == "0.1.0"


def test_drain_and_pin_runbook_is_committed() -> None:
    text = _RUNBOOK.read_text(encoding="utf-8")
    required = (
        "stop new legacy Invocations",
        "retain immutable old environment",
        "finish/export/archive",
        "start new Invocations on modular release",
        "composition mismatch",
        "pinned old runner",
    )
    for needle in required:
        assert needle in text, needle
    assert "automatic historical wheel selection" not in text.lower()


@pytest.mark.usefixtures("installed_sources")
def test_sut_local_and_ambient_sources_do_not_change_assembly_bytes(
    installed_sources,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _assembly_bytes(installed_sources)
    sut = tmp_path / "sut"
    plugins = sut / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "forged.py").write_text("raise RuntimeError('sut plugin imported')\n", encoding="utf-8")
    fake_pkg = tmp_path / "assurance_forged_module"
    fake_pkg.mkdir()
    (fake_pkg / "__init__.py").write_text("raise RuntimeError('fake package imported')\n", encoding="utf-8")
    (fake_pkg / "module.yaml").write_text(
        "schema_version: '1'\nrole: feature\nowner_id: forged\nmodule_id: forged.workflow\n",
        encoding="utf-8",
    )
    (sut / "url-import.yaml").write_text("source: https://evil.example/module.yaml\n", encoding="utf-8")
    (sut / "path-import.yaml").write_text("source: /tmp/forged/module.yaml\n", encoding="utf-8")
    (sut / "glob-import.yaml").write_text("source: ./**/module.yaml\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(sut)
    original = metadata.entry_points

    def ambient(group: str | None = None, **kwargs):
        selected = original(group=group, **kwargs) if group is not None else original(**kwargs)
        extra = metadata.EntryPoint(
            name="sut-local",
            value="assurance_forged_module:ForgedPlugin",
            group="graph_engine.plugins",
        )
        if group in {None, "graph_engine.plugins"}:
            return metadata.EntryPoints((*selected, extra))
        return selected

    monkeypatch.setattr(metadata, "entry_points", ambient)
    assert "assurance_forged_module" not in sys.modules
    assert _assembly_bytes(installed_sources) == baseline
    assert "assurance_forged_module" not in sys.modules


@pytest.mark.parametrize("key", _SOURCE_EXTENSION_KEYS)
def test_aa_source_extension_keys_are_unknown_configuration(key: str) -> None:
    from assurance_product.configuration import ProjectConfigurationError, parse_project_config

    document = {
        "schema_version": "1",
        "product_policy": {"schema_version": "1", "organization": "example"},
        "data_knowledge": {"schema_version": "1", "notes": []},
        "capability_catalog": {"schema_version": "1", "typed_leafs": []},
        "node_policy_values": {},
        key: "https://evil.example/module.yaml" if key in {"path", "module"} else "forged",
    }
    with pytest.raises(ProjectConfigurationError, match="unknown configuration|runtime authority"):
        parse_project_config(document)


@pytest.mark.parametrize("key", _SOURCE_EXTENSION_KEYS)
def test_nested_aa_source_extension_keys_are_unknown_configuration(key: str) -> None:
    from assurance_product.configuration import ProjectConfigurationError, parse_project_config

    document = {
        "schema_version": "1",
        "product_policy": {
            "schema_version": "1",
            "organization": "example",
            key: "/tmp/forged/*.yaml",
        },
        "data_knowledge": {"schema_version": "1", "notes": []},
        "capability_catalog": {"schema_version": "1", "typed_leafs": []},
        "node_policy_values": {},
    }
    with pytest.raises(ProjectConfigurationError, match="unknown configuration|runtime authority"):
        parse_project_config(document)


@pytest.mark.usefixtures("installed_sources")
def test_modular_runner_leaves_legacy_lock_ledger_unchanged(installed_sources, tmp_path: Path) -> None:
    from assurance_product.product import load_pre_modular_workflow
    from tests.product.test_workflow_modularization_golden import _modular_composition

    legacy = resolve_product_workflow_composition(load_pre_modular_workflow(_PRE_MODULAR_FIXTURE))
    modular = _modular_composition(installed_sources)
    engine_root = tmp_path / "legacy-lock"
    engine_root.mkdir()
    engine = Engine(engine_root)
    handle = engine.start(
        legacy,
        entrypoint="intake",
        invocation_id="inv-legacy-lock-001",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
        workspace_binding=_workspace_binding(engine_root),
    )
    handle.close()
    before = _ledger_bytes(engine_root, "inv-legacy-lock-001")
    assert before
    with pytest.raises((InvocationDrift, EngineError)):
        Engine(engine_root).open(
            "inv-legacy-lock-001",
            modular,
            authorization=empty_runtime_authorization(),
            workspace_binding=_workspace_binding(engine_root),
        )
    assert _ledger_bytes(engine_root, "inv-legacy-lock-001") == before


@pytest.mark.usefixtures("installed_sources")
def test_same_composition_resume_after_mismatch_probe_still_opens(installed_sources, tmp_path: Path) -> None:
    from tests.product.test_workflow_modularization_golden import _modular_composition

    modular = _modular_composition(installed_sources)
    engine_root = tmp_path / "same-composition"
    engine_root.mkdir()
    engine = Engine(engine_root)
    handle = engine.start(
        modular,
        entrypoint="intake",
        invocation_id="inv-same-001",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
        workspace_binding=_workspace_binding(engine_root),
    )
    handle.close()
    reopened = Engine(engine_root).open(
        "inv-same-001",
        modular,
        authorization=empty_runtime_authorization(),
        workspace_binding=_workspace_binding(engine_root),
    )
    reopened.close()


@pytest.mark.usefixtures("installed_sources")
def test_public_boundary_enums_and_actions_reach_a_planned_outcome(installed_sources, tmp_path: Path) -> None:
    from tests.product.test_workflow_modularization_golden import _modular_composition

    composition = _modular_composition(installed_sources)
    seen: list[str] = []
    for field, value in _PUBLIC_BOUNDARY_ENUMS:
        kwargs: dict[str, object] = {"entrypoint": "full"}
        if field == "review_decision":
            kwargs["review_decision"] = value
        elif field == "healing_decision":
            kwargs["healing_decision"] = value
            kwargs["execution_sequence"] = ("failed",)
        else:
            kwargs["execution_sequence"] = (value,)
        result = ProductRun(
            entrypoint="full",
            selected_test_families=("api",),
            review_decision=str(kwargs.get("review_decision", "pass")),
            healing_decision=str(kwargs.get("healing_decision", "allowed")),
            execution_sequence=cast(tuple[str, ...], kwargs.get("execution_sequence", ())),
            engine_root=tmp_path / f"enum-{field}-{value}",
            composition=composition,
        ).run_to_terminal()
        assert result.status in _PLANNED_TERMINALS, (field, value, result.status)
        assert result.status != "running"
        seen.append(f"{field}:{value}")
    for action in _PUBLIC_BOUNDARY_ACTIONS:
        stopped = ProductRun(
            entrypoint="improvement-apply",
            selected_test_families=(),
            review_decision="needs_human_review",
            healing_decision="allowed",
            engine_root=tmp_path / f"action-{action}",
            composition=composition,
        ).run_to_terminal()
        assert stopped.status == "interrupted"
        resumed = stopped.resume({"decision": action})
        assert resumed.status in _PLANNED_TERMINALS, (action, resumed.status)
        assert resumed.status != "running"
        seen.append(f"action:{action}")
    assert {item for item in seen if item.startswith("action:")} == {
        f"action:{action}" for action in _PUBLIC_BOUNDARY_ACTIONS
    }
    assert set(seen) >= {f"{field}:{value}" for field, value in _PUBLIC_BOUNDARY_ENUMS}


@pytest.mark.usefixtures("installed_sources")
def test_compiled_boundaries_have_a_next_transition_or_terminal(installed_sources) -> None:
    from assurance_product.product import resolve_assurance_composition

    workflow = resolve_assurance_composition(request_for("opencode", installed_sources)).workflow
    stuck: list[str] = []
    for graph_id, graph in workflow.graphs.items():
        for node_id, node in graph.nodes.items():
            kind = node.definition.kind
            if kind == "end":
                continue
            if kind == "interrupt":
                assert node.definition.actions, f"{graph_id}/{node_id} interrupt has no actions"
                continue
            if node.outgoing:
                continue
            stuck.append(f"{graph_id}/{node_id}:{kind}")
    assert stuck == []


def test_product_wheel_ships_only_main_workflow_source(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(
        ["uv", "build", "--package", "assurance-product", "--out-dir", str(tmp_path)],
        check=True,
    )
    wheel = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    workflow = [name for name in names if "resources/workflow/" in name and not name.endswith("/")]
    assert workflow == ["assurance_product/resources/workflow/main.yaml"]
    assert all("assurance-full.yaml" not in name for name in names)
