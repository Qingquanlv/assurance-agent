from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

import pytest

import graph_engine.plugin_api as plugin_api
from graph_engine.canonical import canonical_digest
from graph_engine.composition import (
    EditableWheelPluginSource,
    EditableWheelProductSource,
    FrozenComposition,
    RegistryPlatform,
    ResolutionRequest,
)
from graph_engine.runtime.engine import Engine
from graph_engine.runtime.invocation_lock import InvocationDrift
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.secret_sources import empty_runtime_authorization
from graph_engine.runtime.seed import InvocationSeed


def _composition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FrozenComposition:
    source = tmp_path / "composition-source"
    repository = Path(__file__).parents[4]
    shutil.copytree(
        repository / "examples" / "graph-engine-toy-a",
        source,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    source_files = tuple(
        sorted(path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file())
    )
    monkeypatch.syspath_prepend(str(source))
    for module_name in tuple(sys.modules):
        if module_name == "graph_engine_toy_a" or module_name.startswith("graph_engine_toy_a."):
            sys.modules.pop(module_name, None)
    importlib.invalidate_caches()
    common: dict[str, object] = {
        "distribution": "graph-engine-toy-a",
        "entrypoint_name": "toy-a",
        "source_root": source,
        "source_files": source_files,
    }
    return RegistryPlatform().resolve(
        ResolutionRequest(
            product=EditableWheelProductSource(
                **common,
                declaration_path="graph_engine_toy_a/product-declaration.json",
            ),
            plugins=(
                EditableWheelPluginSource(
                    **common,
                    declaration_path="graph_engine_toy_a/plugin-declaration.json",
                ),
            ),
        )
    )


def _seed() -> InvocationSeed:
    root_input = {"request": "root only"}
    return InvocationSeed(
        schema_version="2", root_input=root_input, root_input_digest=canonical_digest(root_input)
    )


def _binding(tmp_path: Path, *, prefix: str = "bound"):
    binding_type = getattr(plugin_api, "InvocationWorkspaceBinding", None)
    assert binding_type is not None, "InvocationWorkspaceBinding must be part of plugin_api"
    project_root = tmp_path / f"{prefix}-project"
    attempts_root = tmp_path / f"{prefix}-attempts"
    receipts_root = tmp_path / f"{prefix}-promotion-receipts"
    project_root.mkdir()
    attempts_root.mkdir()
    receipts_root.mkdir()
    return binding_type(
        project_root=project_root,
        attempts_root=attempts_root,
        receipts_root=receipts_root,
    )


def test_invocation_seed_contains_root_input_but_no_project_tree_identity() -> None:
    seed = _seed()

    assert seed.root_input == {"request": "root only"}
    assert not hasattr(seed, "workspace")
    assert set(seed.__dataclass_fields__) == {
        "schema_version",
        "root_input",
        "root_input_digest",
    }


def test_start_requires_binding_and_does_not_capture_the_project_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composition = _composition(tmp_path, monkeypatch)
    binding = _binding(tmp_path)
    sentinel = binding.project_root / "must-not-be-read.txt"
    sentinel.write_text("large project content", encoding="utf-8")
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path.resolve() == sentinel.resolve():
            raise AssertionError("engine start captured a project file")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    engine = Engine(tmp_path / "engine")
    try:
        with engine.start(
            composition,
            entrypoint="hello",
            invocation_id="inv-binding",
            seed=_seed(),
            authorization=empty_runtime_authorization(),
            workspace_binding=binding,
        ) as handle:
            payloads = [item.event.model_dump(mode="json") for item in handle.ledger.read_all()]
            encoded = repr(payloads)
            assert str(binding.project_root) not in encoded
            assert str(binding.attempts_root) not in encoded
            assert str(binding.receipts_root) not in encoded
            assert all("initial_tree_id" not in payload for payload in payloads)
            assert not (handle.invocation_root / "workspace").exists()
            assert not (handle.invocation_root / "HEAD.json").exists()
    finally:
        engine.close()


@pytest.mark.parametrize("changed", ["project_root", "attempts_root"])
def test_open_rejects_workspace_root_drift_before_reading_the_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    composition = _composition(tmp_path, monkeypatch)
    binding = _binding(tmp_path)
    engine = Engine(tmp_path / "engine")
    try:
        engine.start(
            composition,
            entrypoint="hello",
            invocation_id="inv-binding",
            seed=_seed(),
            authorization=empty_runtime_authorization(),
            workspace_binding=binding,
        ).close()
        replacement = _binding(tmp_path, prefix="replacement")
        values = {
            "project_root": replacement.project_root
            if changed == "project_root"
            else binding.project_root,
            "attempts_root": replacement.attempts_root
            if changed == "attempts_root"
            else binding.attempts_root,
            "receipts_root": binding.receipts_root,
        }
        drifted = type(binding)(**values)

        def forbidden_read(_ledger: Ledger):
            raise AssertionError("workspace binding must authenticate before ledger reads")

        monkeypatch.setattr(Ledger, "read_all", forbidden_read)
        with pytest.raises(InvocationDrift, match="workspace binding"):
            engine.open(
                "inv-binding",
                composition,
                authorization=empty_runtime_authorization(),
                workspace_binding=drifted,
            )
    finally:
        engine.close()
