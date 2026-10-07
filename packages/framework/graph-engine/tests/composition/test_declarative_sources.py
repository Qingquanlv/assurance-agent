from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import stat

import pytest
import yaml

from graph_engine.composition import (
    ConfigTreePluginSource,
    DeclarativePluginRejected,
    DeclarativeProductRejected,
    ProductFileSource,
    SourceIdentity,
    SourceKind,
    SourceSnapshot,
    load_config_tree,
    load_product_file,
)


def _write_plugin_tree(
    root: Path,
    *,
    extra: dict[str, object] | None = None,
    bindings: dict[str, dict[str, object]] | None = None,
    files: list[dict[str, object]] | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if files is None:
        files = [
            {
                "kind": "resource",
                "resource_id": "toy.flow.role",
                "path": "role.md",
                "media_type": "text/markdown",
            }
        ]
    document: dict[str, object] = {
        "schema_version": "1",
        "plugin_id": "toy.flow",
        "plugin_version": "1.2.3",
        "engine_api": ">=0.2,<0.3",
        "dependencies": [{"plugin_id": "toy.runtime", "version_specifier": ">=1,<2"}],
        "files": files,
        "bindings": [
            {
                "capability_id": capability_id,
                "target_capability_id": binding["target"],
                "data": binding.get("data"),
                "resource_ids": binding.get("resource_ids", []),
            }
            for capability_id, binding in (bindings or {}).items()
        ],
    }
    if extra:
        document.update(extra)
    (root / "plugin.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False),
        encoding="utf-8",
    )
    for declared in files:
        path = root / str(declared["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        media_type = declared["media_type"]
        if media_type in {"application/json", "application/schema+json"}:
            path.write_text('{"type":"object"}\n', encoding="utf-8")
        elif media_type in {"application/yaml", "text/yaml"}:
            path.write_text("value: data\n", encoding="utf-8")
        else:
            path.write_text("role instructions\n", encoding="utf-8")


def _write_product_file(
    path: Path,
    *,
    extra: dict[str, object] | None = None,
    graph_factory_symbol: str = "toy.product:build",
    config_plugin_paths: list[str] | None = None,
) -> None:
    document: dict[str, object] = {
        "schema_version": "1",
        "product_id": "toy.product",
        "product_version": "1.0.0",
        "engine_api": ">=0.2,<0.3",
        "plugins": [
            {"plugin_id": "toy.runtime", "version_specifier": ">=1,<2"},
            {"plugin_id": "toy.flow", "version_specifier": "==1.2.3"},
        ],
        "entrypoints": {"main": "root"},
        "configuration": {"toy.runtime": {"greeting": "hello"}},
        "config_plugin_paths": config_plugin_paths or [],
        "graph_factory_symbol": graph_factory_symbol,
    }
    if extra:
        document.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_config_plugin_loads_frozen_descriptor_and_contribution(tmp_path: Path) -> None:
    files = [
        {
            "kind": "schema",
            "resource_id": "toy.flow.input-schema",
            "path": "schema.json",
            "media_type": "application/schema+json",
        },
        {
            "kind": "resource",
            "resource_id": "toy.flow.role",
            "path": "role.md",
            "media_type": "text/markdown",
        },
    ]
    _write_plugin_tree(tmp_path, files=files)

    loaded = load_config_tree(ConfigTreePluginSource(path=tmp_path))

    assert loaded.descriptor.plugin_id == "toy.flow"
    assert loaded.descriptor.plugin_version == "1.2.3"
    assert loaded.descriptor.dependencies[0].plugin_id == "toy.runtime"
    assert loaded.descriptor.schemas == ("toy.flow.input-schema",)
    assert loaded.descriptor.resources == ("toy.flow.role",)
    assert loaded.contribution.schemas[0].content == b'{"type":"object"}\n'
    assert loaded.contribution.resources[0].content == b"role instructions\n"
    assert loaded.snapshot.identity.kind == SourceKind.CONFIG_TREE
    assert loaded.snapshot.identity.root == tmp_path.resolve()
    assert loaded.snapshot.identity.plugin_id == "toy.flow"
    assert loaded.snapshot.identity.plugin_version == "1.2.3"
    assert tuple(file.path for file in loaded.snapshot.files) == (
        "plugin.yaml",
        "role.md",
        "schema.json",
    )


@pytest.mark.parametrize(
    "payload",
    (
        {"python": "pkg.module:function"},
        {"command": ["sh", "-c", "echo bad"]},
        {"shell": "echo bad"},
        {"template": "{{ __import__('os') }}"},
    ),
)
def test_config_plugin_rejects_executable_forms(tmp_path: Path, payload: dict[str, object]) -> None:
    _write_plugin_tree(tmp_path, extra=payload)

    with pytest.raises(DeclarativePluginRejected):
        load_config_tree(ConfigTreePluginSource(path=tmp_path))


def test_config_plugin_binds_data_to_selected_wheel_capability(tmp_path: Path) -> None:
    _write_plugin_tree(
        tmp_path,
        bindings={
            "toy.flow.greet": {
                "target": "toy.runtime.execute",
                "data": {"skill": "toy.flow.greeting-skill"},
            }
        },
    )

    loaded = load_config_tree(ConfigTreePluginSource(path=tmp_path))

    binding = loaded.contribution.bindings[0]
    assert binding.capability_id == "toy.flow.greet"
    assert binding.target_capability_id == "toy.runtime.execute"
    assert binding.data == {"skill": "toy.flow.greeting-skill"}
    assert loaded.contribution.task_handlers == {}
    assert loaded.contribution.commit_validators == {}


def test_config_plugin_binding_data_is_recursively_immutable(tmp_path: Path) -> None:
    _write_plugin_tree(
        tmp_path,
        bindings={
            "toy.flow.greet": {
                "target": "toy.runtime.execute",
                "data": {"skills": ["toy.flow.greeting-skill"]},
            }
        },
    )
    loaded = load_config_tree(ConfigTreePluginSource(path=tmp_path))
    binding = loaded.contribution.bindings[0]
    data = binding.data
    assert isinstance(data, Mapping)
    assert not isinstance(data, dict)
    skills = data["skills"]
    assert isinstance(skills, tuple)

    with pytest.raises(TypeError):
        data["other"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        dict.__setitem__(data, "other", True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        list.append(skills, "toy.flow.other-skill")  # type: ignore[arg-type]
    assert binding.model_dump(mode="json")["data"] == {"skills": ["toy.flow.greeting-skill"]}


def test_config_plugin_rejects_unknown_nested_keys(tmp_path: Path) -> None:
    files = [
        {
            "kind": "resource",
            "resource_id": "toy.flow.role",
            "path": "role.md",
            "media_type": "text/markdown",
            "unknown": True,
        }
    ]
    _write_plugin_tree(tmp_path, files=files)

    with pytest.raises(DeclarativePluginRejected, match="invalid plugin.yaml"):
        load_config_tree(ConfigTreePluginSource(path=tmp_path))


def test_config_plugin_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    _write_plugin_tree(tmp_path)
    plugin_path = tmp_path / "plugin.yaml"
    content = plugin_path.read_text(encoding="utf-8")
    plugin_path.write_text(
        content.replace(
            "plugin_id: toy.flow\n",
            "plugin_id: toy.first\nplugin_id: toy.flow\n",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeclarativePluginRejected, match="duplicate YAML key"):
        load_config_tree(ConfigTreePluginSource(path=tmp_path))


def test_config_plugin_rejects_recursive_yaml_aliases_without_recursing(tmp_path: Path) -> None:
    _write_plugin_tree(tmp_path)
    plugin_path = tmp_path / "plugin.yaml"
    with plugin_path.open("a", encoding="utf-8") as stream:
        stream.write("cycle: &cycle [*cycle]\n")

    with pytest.raises(DeclarativePluginRejected, match="YAML aliases"):
        load_config_tree(ConfigTreePluginSource(path=tmp_path))


@pytest.mark.parametrize("fault", ("extra", "missing"))
def test_config_plugin_requires_exact_declared_file_inventory(tmp_path: Path, fault: str) -> None:
    _write_plugin_tree(tmp_path)
    if fault == "extra":
        (tmp_path / "ambient.txt").write_text("ambient\n", encoding="utf-8")
    else:
        (tmp_path / "role.md").unlink()

    with pytest.raises(DeclarativePluginRejected, match="declared source file set"):
        load_config_tree(ConfigTreePluginSource(path=tmp_path))


@pytest.mark.parametrize("bad_path", ("../escape.md", "/absolute.md", "a/../b.md", "a\\b.md"))
def test_config_plugin_rejects_unsafe_declared_paths(tmp_path: Path, bad_path: str) -> None:
    files = [
        {
            "kind": "resource",
            "resource_id": "toy.flow.role",
            "path": bad_path,
            "media_type": "text/markdown",
        }
    ]
    tmp_path.mkdir(exist_ok=True)
    document = {
        "schema_version": "1",
        "plugin_id": "toy.flow",
        "plugin_version": "1.2.3",
        "engine_api": ">=0.2,<0.3",
        "dependencies": [],
        "files": files,
        "bindings": [],
    }
    (tmp_path / "plugin.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(DeclarativePluginRejected, match="invalid plugin.yaml|unsafe declared"):
        load_config_tree(ConfigTreePluginSource(path=tmp_path))


@pytest.mark.parametrize(
    "media_type",
    (
        "application/octet-stream",
        "text/x-python",
        "application/x-sh",
        "text/html",
        "text/plain; charset=utf-8",
        "application/vnd.graph-engine.workflow+yaml",
        "application/vnd.graph-engine.workflow-module+yaml",
    ),
)
def test_config_plugin_rejects_unsafe_media_types(tmp_path: Path, media_type: str) -> None:
    files = [
        {
            "kind": "resource",
            "resource_id": "toy.flow.unsafe",
            "path": "unsafe.dat",
            "media_type": media_type,
        }
    ]
    _write_plugin_tree(tmp_path, files=files)

    with pytest.raises(DeclarativePluginRejected, match="media type"):
        load_config_tree(ConfigTreePluginSource(path=tmp_path))


@pytest.mark.parametrize(
    ("media_type", "path", "content"),
    (
        ("application/json", "data.json", b'{"value":1,"value":2}\n'),
        (
            "application/json",
            "data.json",
            b'{"nested":[{"command":["sh","-c","bad"]}]}\n',
        ),
        ("application/json", "data.json", b'{"value":1e999}\n'),
        ("application/schema+json", "schema.json", b'{"type":"object","type":"array"}\n'),
        ("application/yaml", "data.yaml", b"value: 1\nvalue: 2\n"),
        ("text/yaml", "data.yml", b"shared: &shared [one]\ncopy: *shared\n"),
        ("application/yaml", "data.yaml", b"nested:\n- shell: echo bad\n"),
        ("application/yaml", "data.yaml", b"value: .inf\n"),
    ),
)
def test_config_plugin_rejects_unsafe_structured_resource_payloads(
    tmp_path: Path,
    media_type: str,
    path: str,
    content: bytes,
) -> None:
    files = [
        {
            "kind": "schema" if media_type == "application/schema+json" else "resource",
            "resource_id": "toy.flow.data",
            "path": path,
            "media_type": media_type,
        }
    ]
    _write_plugin_tree(tmp_path, files=files)
    (tmp_path / path).write_bytes(content)

    with pytest.raises(DeclarativePluginRejected, match="resource"):
        load_config_tree(ConfigTreePluginSource(path=tmp_path))


@pytest.mark.parametrize(
    ("media_type", "path", "kind"),
    (
        ("application/json", "data.json", "resource"),
        ("application/schema+json", "schema.json", "schema"),
        ("application/yaml", "data.yaml", "resource"),
        ("text/yaml", "data.yml", "resource"),
        ("text/markdown", "role.md", "resource"),
        ("text/plain", "prompt.txt", "resource"),
    ),
)
def test_config_plugin_accepts_only_registered_path_media_pairs(
    tmp_path: Path,
    media_type: str,
    path: str,
    kind: str,
) -> None:
    files = [
        {
            "kind": kind,
            "resource_id": "toy.flow.data",
            "path": path,
            "media_type": media_type,
        }
    ]
    _write_plugin_tree(tmp_path, files=files)

    loaded = load_config_tree(ConfigTreePluginSource(path=tmp_path))

    assert loaded.document.files[0].path == path


@pytest.mark.parametrize(
    ("media_type", "path"),
    (
        ("text/plain", "handler.rb"),
        ("text/plain", "plugin.pl"),
        ("text/plain", "script.php"),
        ("text/plain", "task.lua"),
        ("text/plain", "prompt"),
        ("text/markdown", "role.txt"),
        ("application/json", "data.yaml"),
        ("application/yaml", "data.json"),
    ),
)
def test_config_plugin_rejects_unregistered_path_media_pairs(
    tmp_path: Path,
    media_type: str,
    path: str,
) -> None:
    files = [
        {
            "kind": "resource",
            "resource_id": "toy.flow.unsafe",
            "path": path,
            "media_type": media_type,
        }
    ]
    _write_plugin_tree(tmp_path, files=files)

    with pytest.raises(DeclarativePluginRejected, match="path.*media type"):
        load_config_tree(ConfigTreePluginSource(path=tmp_path))


@pytest.mark.parametrize("name", ("handler.py", "run.sh", "module.wasm", "script.js"))
def test_config_plugin_rejects_executable_file_extensions(tmp_path: Path, name: str) -> None:
    files = [
        {
            "kind": "resource",
            "resource_id": "toy.flow.unsafe",
            "path": name,
            "media_type": "text/plain",
        }
    ]
    _write_plugin_tree(tmp_path, files=files)

    with pytest.raises(DeclarativePluginRejected, match="executable file type"):
        load_config_tree(ConfigTreePluginSource(path=tmp_path))


def test_config_plugin_rejects_executable_posix_mode(tmp_path: Path) -> None:
    _write_plugin_tree(tmp_path)
    path = tmp_path / "role.md"
    path.chmod(path.stat().st_mode | stat.S_IXUSR)

    with pytest.raises(DeclarativePluginRejected, match="executable file"):
        load_config_tree(ConfigTreePluginSource(path=tmp_path))


@pytest.mark.parametrize("target", ("file", "directory"))
def test_config_plugin_rejects_symlinks(tmp_path: Path, target: str) -> None:
    physical = tmp_path / "physical"
    _write_plugin_tree(physical)
    if target == "file":
        workflow = physical / "role.md"
        outside = tmp_path / "outside.yaml"
        outside.write_bytes(workflow.read_bytes())
        workflow.unlink()
        workflow.symlink_to(outside)
        source = physical
    else:
        source = tmp_path / "linked"
        source.symlink_to(physical, target_is_directory=True)

    with pytest.raises(DeclarativePluginRejected, match="no-follow|regular file or directory"):
        load_config_tree(ConfigTreePluginSource(path=source))


def test_config_plugin_rejects_special_files_without_blocking(tmp_path: Path) -> None:
    _write_plugin_tree(tmp_path)
    workflow = tmp_path / "role.md"
    workflow.unlink()
    os.mkfifo(workflow)

    with pytest.raises(DeclarativePluginRejected, match="regular file or directory"):
        load_config_tree(ConfigTreePluginSource(path=tmp_path))


def test_config_plugin_resource_change_changes_snapshot_digest(tmp_path: Path) -> None:
    _write_plugin_tree(tmp_path)
    first = load_config_tree(ConfigTreePluginSource(path=tmp_path))
    (tmp_path / "role.md").write_text("changed role instructions\n", encoding="utf-8")

    second = load_config_tree(ConfigTreePluginSource(path=tmp_path))

    assert first.snapshot.digest != second.snapshot.digest
    assert first.contribution.resources[0].content != second.contribution.resources[0].content


def test_config_plugin_normalizes_exact_version_across_loaded_values(tmp_path: Path) -> None:
    _write_plugin_tree(tmp_path)
    plugin_path = tmp_path / "plugin.yaml"
    document = yaml.safe_load(plugin_path.read_text(encoding="utf-8"))
    document["plugin_version"] = "01.002.0003"
    plugin_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    loaded = load_config_tree(ConfigTreePluginSource(path=tmp_path))

    assert loaded.document.plugin_version == "1.2.3"
    assert loaded.descriptor.plugin_version == "1.2.3"
    assert loaded.snapshot.identity.plugin_version == "1.2.3"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("plugin_id", "not-qualified"),
        ("resource_id", "not-qualified"),
        ("capability_id", "not-qualified"),
        ("target_capability_id", "not-qualified"),
    ),
)
def test_config_plugin_rejects_invalid_qualified_ids(tmp_path: Path, field: str, value: str) -> None:
    bindings = {
        "toy.flow.greet": {
            "target": "toy.runtime.execute",
            "resource_ids": ["toy.flow.role"],
        }
    }
    _write_plugin_tree(tmp_path, bindings=bindings)
    document = yaml.safe_load((tmp_path / "plugin.yaml").read_text(encoding="utf-8"))
    if field == "plugin_id":
        document[field] = value
    elif field == "resource_id":
        document["files"][0][field] = value
    else:
        document["bindings"][0][field] = value
    (tmp_path / "plugin.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(DeclarativePluginRejected, match="invalid plugin.yaml"):
        load_config_tree(ConfigTreePluginSource(path=tmp_path))


@pytest.mark.parametrize("fault", ("duplicate_path", "duplicate_id", "dangling_binding_resource"))
def test_config_plugin_rejects_non_closed_local_references(tmp_path: Path, fault: str) -> None:
    files = [
        {
            "kind": "resource",
            "resource_id": "toy.flow.role",
            "path": "role.md",
            "media_type": "text/markdown",
        },
        {
            "kind": "resource",
            "resource_id": "toy.flow.other",
            "path": "other.md",
            "media_type": "text/markdown",
        },
    ]
    bindings: dict[str, dict[str, object]] = {}
    if fault == "duplicate_path":
        files[1]["path"] = "role.md"
    elif fault == "duplicate_id":
        files[1]["resource_id"] = "toy.flow.role"
    else:
        bindings = {
            "toy.flow.greet": {
                "target": "toy.runtime.execute",
                "resource_ids": ["toy.flow.missing"],
            }
        }
    _write_plugin_tree(tmp_path, files=files, bindings=bindings)

    with pytest.raises(DeclarativePluginRejected):
        load_config_tree(ConfigTreePluginSource(path=tmp_path))


def test_product_file_resolves_only_explicit_manifest_relative_config_paths(
    tmp_path: Path,
) -> None:
    product_path = tmp_path / "products" / "product.yaml"
    config = product_path.parent / "config" / "flow"
    config.mkdir(parents=True)
    (product_path.parent / "unrelated.txt").write_text("ignored sibling\n", encoding="utf-8")
    _write_product_file(product_path, config_plugin_paths=["config/flow"])

    loaded = load_product_file(ProductFileSource(path=product_path))

    assert loaded.manifest.product_id == "toy.product"
    assert loaded.manifest.required_plugin_ids == ("toy.runtime", "toy.flow")
    assert loaded.config_plugin_paths == (config.absolute(),)
    assert loaded.manifest.config_plugin_paths == ("config/flow",)
    assert loaded.snapshot.identity.kind == SourceKind.PRODUCT_FILE
    assert loaded.snapshot.identity.root == product_path.parent.resolve()
    assert tuple(file.path for file in loaded.snapshot.files) == ("product.yaml",)


def test_product_file_normalizes_and_binds_complete_source_identity(tmp_path: Path) -> None:
    product_path = tmp_path / "product.yaml"
    _write_product_file(product_path)
    document = yaml.safe_load(product_path.read_text(encoding="utf-8"))
    document["product_version"] = "01.002.0000"
    product_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    loaded = load_product_file(ProductFileSource(path=product_path))
    identity = loaded.snapshot.identity

    assert loaded.manifest.product_version == "1.2.0"
    assert identity.product_id == "toy.product"
    assert identity.product_version == "1.2.0"
    assert loaded.snapshot == SourceSnapshot.from_identity(identity, loaded.snapshot.files)
    provisional = SourceSnapshot.from_files(
        SourceKind.PRODUCT_FILE,
        identity.root,
        loaded.snapshot.files,
    )
    assert loaded.snapshot.digest != provisional.digest


def test_equivalent_product_version_spellings_share_identity_but_not_raw_digest(
    tmp_path: Path,
) -> None:
    product_path = tmp_path / "product.yaml"
    _write_product_file(product_path)
    first_document = yaml.safe_load(product_path.read_text(encoding="utf-8"))
    first_document["product_version"] = "1.2.0"
    product_path.write_text(yaml.safe_dump(first_document), encoding="utf-8")
    first = load_product_file(ProductFileSource(path=product_path))
    first_document["product_version"] = "01.002.0000"
    product_path.write_text(yaml.safe_dump(first_document), encoding="utf-8")

    second = load_product_file(ProductFileSource(path=product_path))

    assert first.snapshot.identity == second.snapshot.identity
    assert first.snapshot.digest != second.snapshot.digest


def test_declarative_source_identity_requires_complete_normalized_coordinates(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="complete product coordinates"):
        SourceIdentity(
            kind=SourceKind.PRODUCT_FILE,
            root=tmp_path.resolve(),
            product_id="toy.product",
        )
    with pytest.raises(ValueError, match="normalized product version"):
        SourceIdentity(
            kind=SourceKind.PRODUCT_FILE,
            root=tmp_path.resolve(),
            product_id="toy.product",
            product_version="01.002.0000",
        )
    with pytest.raises(ValueError, match="normalized plugin version"):
        SourceIdentity(
            kind=SourceKind.CONFIG_TREE,
            root=tmp_path.resolve(),
            plugin_id="toy.flow",
            plugin_version="01.002.0003",
        )


def test_product_manifest_nested_configuration_is_immutable(
    tmp_path: Path,
) -> None:
    product_path = tmp_path / "product.yaml"
    _write_product_file(product_path)
    loaded = load_product_file(ProductFileSource(path=product_path))

    assert loaded.manifest.graph_factory_symbol == "toy.product:build"
    with pytest.raises(TypeError, match="frozen"):
        loaded.manifest.entrypoints["other"] = "root"
    with pytest.raises(TypeError, match="frozen"):
        loaded.manifest.configuration["toy.runtime"]["greeting"] = "changed"


@pytest.mark.parametrize("bad_path", ("../config", "/tmp/config", "a/../config", "a\\config"))
def test_product_file_rejects_out_of_root_config_paths(tmp_path: Path, bad_path: str) -> None:
    product_path = tmp_path / "product.yaml"
    _write_product_file(product_path, config_plugin_paths=[bad_path])

    with pytest.raises(DeclarativeProductRejected, match="config plugin path"):
        load_product_file(ProductFileSource(path=product_path))


@pytest.mark.parametrize(
    "extra",
    (
        {"unknown": True},
        {"python": "pkg.product:provider"},
        {"command": ["sh", "-c", "bad"]},
        {"shell": "bad"},
        {"template": "{{ bad() }}"},
    ),
)
def test_product_file_rejects_unknown_and_executable_fields(tmp_path: Path, extra: dict[str, object]) -> None:
    product_path = tmp_path / "product.yaml"
    _write_product_file(product_path, extra=extra)

    with pytest.raises(DeclarativeProductRejected, match="invalid product manifest"):
        load_product_file(ProductFileSource(path=product_path))


@pytest.mark.parametrize("fault", ("symlink", "executable", "fifo"))
def test_product_file_rejects_non_data_file_forms(tmp_path: Path, fault: str) -> None:
    product_path = tmp_path / "product.yaml"
    _write_product_file(product_path)
    if fault == "symlink":
        outside = tmp_path / "outside.yaml"
        outside.write_bytes(product_path.read_bytes())
        product_path.unlink()
        product_path.symlink_to(outside)
    elif fault == "executable":
        product_path.chmod(product_path.stat().st_mode | stat.S_IXUSR)
    else:
        product_path.unlink()
        os.mkfifo(product_path)

    with pytest.raises(DeclarativeProductRejected):
        load_product_file(ProductFileSource(path=product_path))


@pytest.mark.parametrize(
    "extra",
    (
        {
            "imports": {
                "run": {
                    "owner_id": "toy.feature",
                    "module_id": "toy.feature.workflow",
                    "export": "run",
                }
            }
        },
        {
            "configuration": {
                "toy.runtime": {
                    "imports": {
                        "run": {
                            "owner_id": "toy.feature",
                            "module_id": "toy.feature.workflow",
                            "export": "run",
                        }
                    }
                }
            }
        },
    ),
)
def test_product_file_rejects_imports_outside_workflow_module_path(
    tmp_path: Path, extra: dict[str, object]
) -> None:
    product_path = tmp_path / "product.yaml"
    _write_product_file(product_path, extra=extra)

    with pytest.raises(DeclarativeProductRejected, match="executable declaration"):
        load_product_file(ProductFileSource(path=product_path))


def test_plugin_document_rejects_imports_even_at_workflow_module_path(tmp_path: Path) -> None:
    _write_plugin_tree(
        tmp_path,
        extra={
            "workflow_module": {
                "imports": {
                    "run": {
                        "owner_id": "toy.feature",
                        "module_id": "toy.feature.workflow",
                        "export": "run",
                    }
                }
            }
        },
    )

    with pytest.raises(DeclarativePluginRejected, match="executable declaration"):
        load_config_tree(ConfigTreePluginSource(path=tmp_path))


def test_plugin_resource_rejects_imports_even_at_workflow_module_path(tmp_path: Path) -> None:
    files: list[dict[str, object]] = [
        {
            "kind": "resource",
            "resource_id": "toy.flow.data",
            "path": "data.yaml",
            "media_type": "application/yaml",
        }
    ]
    _write_plugin_tree(tmp_path, files=files)
    (tmp_path / "data.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow_module": {
                    "imports": {
                        "run": {
                            "owner_id": "toy.feature",
                            "module_id": "toy.feature.workflow",
                            "export": "run",
                        }
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeclarativePluginRejected, match="resource"):
        load_config_tree(ConfigTreePluginSource(path=tmp_path))


def test_config_tree_rejects_workflow_module_media_type(tmp_path: Path) -> None:
    files: list[dict[str, object]] = [
        {
            "kind": "resource",
            "resource_id": "toy.flow.module",
            "path": "module.yaml",
            "media_type": "application/vnd.graph-engine.workflow-module+yaml",
        }
    ]
    _write_plugin_tree(tmp_path, files=files)

    with pytest.raises(DeclarativePluginRejected, match="media type"):
        load_config_tree(ConfigTreePluginSource(path=tmp_path))


@pytest.mark.parametrize(
    "extra",
    (
        {"workflow": {"name": "toy"}},
        {"workflow_resource_id": "toy.flow.workflow"},
        {"workflow_module": {"owner_id": "toy.product"}},
    ),
)
def test_product_file_rejects_leftover_workflow_keys(tmp_path: Path, extra: dict[str, object]) -> None:
    product_path = tmp_path / "product.yaml"
    _write_product_file(product_path, extra=extra)

    with pytest.raises(DeclarativeProductRejected, match="invalid product manifest"):
        load_product_file(ProductFileSource(path=product_path))
