from __future__ import annotations

import json
import os
import stat
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlparse

from graph_engine.composition import EditableWheelPluginSource, WheelPluginSource
from graph_engine.plugin_api import ProviderSource

_SIX_CAPABILITY_SOURCES: tuple[ProviderSource, ...] = (
    ProviderSource(
        distribution="assurance-intake",
        version="0.3.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="intake",
        entrypoint_value="assurance_intake.plugin:IntakePlugin",
        declaration_path="assurance_intake/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-generation",
        version="0.3.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="generation",
        entrypoint_value="assurance_generation.plugin:GenerationPlugin",
        declaration_path="assurance_generation/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-execution",
        version="0.3.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="execution",
        entrypoint_value="assurance_execution.plugin:ExecutionPlugin",
        declaration_path="assurance_execution/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-healing",
        version="0.3.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="healing",
        entrypoint_value="assurance_healing.plugin:HealingPlugin",
        declaration_path="assurance_healing/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-quality",
        version="0.3.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="quality",
        entrypoint_value="assurance_quality.plugin:QualityPlugin",
        declaration_path="assurance_quality/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-improvement",
        version="0.3.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="improvement",
        entrypoint_value="assurance_improvement.plugin:ImprovementPlugin",
        declaration_path="assurance_improvement/plugin-declaration.json",
        import_roots=("",),
    ),
)

_OPENCODE_RUNTIME_SOURCE = ProviderSource(
    distribution="agent-runtime-opencode",
    version="0.1.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="opencode",
    entrypoint_value="agent_runtime_opencode.plugin:OpenCodePlugin",
    declaration_path="agent_runtime_opencode/plugin-declaration.json",
    import_roots=("",),
)


def product_source_catalog() -> tuple[ProviderSource, ...]:
    return (*_SIX_CAPABILITY_SOURCES, _OPENCODE_RUNTIME_SOURCE)


def wheel_plugin_source(source: ProviderSource) -> WheelPluginSource:
    if source.entrypoint_group != "graph_engine.plugins":
        raise ValueError("product catalog source must be a graph_engine.plugins coordinate")
    return WheelPluginSource(
        distribution=source.distribution,
        entrypoint_name=source.entrypoint_name,
        declaration_path=source.declaration_path,
    )


def editable_distribution_root(distribution_name: str) -> Path | None:
    try:
        distribution = metadata.distribution(distribution_name)
    except metadata.PackageNotFoundError:
        return None
    raw = distribution.read_text("direct_url.json")
    if raw is None:
        return None
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(document, dict):
        return None
    info = document.get("dir_info")
    url = document.get("url")
    if not isinstance(info, dict) or not info.get("editable") or not isinstance(url, str):
        return None
    parsed = urlparse(url)
    if parsed.scheme != "file" or not parsed.path:
        return None
    root = Path(unquote(parsed.path))
    if not root.is_dir():
        return None
    return root


def editable_source_files(root: Path) -> tuple[str, ...]:
    files: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(dirpath, name)
            try:
                mode = path.lstat().st_mode
            except OSError:
                continue
            if not stat.S_ISREG(mode):
                continue
            files.append(path.relative_to(root).as_posix())
    return tuple(sorted(files))


def installed_plugin_source(source: ProviderSource) -> WheelPluginSource | EditableWheelPluginSource:
    if source.entrypoint_group != "graph_engine.plugins":
        raise ValueError("product catalog source must be a graph_engine.plugins coordinate")
    root = editable_distribution_root(source.distribution)
    if root is None:
        return wheel_plugin_source(source)
    files = editable_source_files(root)
    if source.declaration_path not in files:
        raise ValueError(f"editable {source.distribution} is missing declaration {source.declaration_path}")
    return EditableWheelPluginSource(
        distribution=source.distribution,
        entrypoint_name=source.entrypoint_name,
        declaration_path=source.declaration_path,
        source_root=root,
        source_files=files,
    )
