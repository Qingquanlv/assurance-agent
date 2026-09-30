"""Generate wheel plugin declarations from Python registrations, then build.

Run with ``uv run python scripts/build_wheels.py`` from the workspace root.
This is a trusted source-build tool, never a runtime plugin discovery path.
"""

from __future__ import annotations

import argparse
from importlib import import_module
from importlib.metadata import EntryPoint
from pathlib import Path
import subprocess
import sys
import tomllib


def workspace_projects(root: Path) -> dict[str, tuple[Path, dict]]:
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    projects = {}
    for member in config["tool"]["uv"]["workspace"]["members"]:
        directory = (root / member).resolve()
        if not directory.is_relative_to(root):
            raise ValueError(f"workspace member escapes repository: {member}")
        project = tomllib.loads((directory / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        name = project["name"]
        if name in projects:
            raise ValueError(f"duplicate workspace package: {name}")
        projects[name] = (directory, project)
    return projects


def plugin_declarations(projects: dict[str, tuple[Path, dict]], selected: list[str]) -> dict[Path, bytes]:
    # Prefer this checkout over stale installed copies, including cross-wheel imports.
    sys.path[:0] = [str(directory) for directory, _ in projects.values()]

    from graph_engine.canonical import canonical_json_bytes
    from graph_engine.composition.sources import WheelPluginDeclaration

    declarations = {}
    for name in selected:
        directory, project = projects[name]
        group = "graph_engine.plugins"
        for entry_name, value in project.get("entry-points", {}).get(group, {}).items():
            entry = EntryPoint(name=entry_name, value=value, group=group)
            module = import_module(entry.module)
            if module.__file__ is None or not Path(module.__file__).resolve().is_relative_to(directory):
                raise ValueError(f"plugin entry point is not from this checkout: {name}:{value}")
            descriptor = entry.load().descriptor()
            declaration = WheelPluginDeclaration(
                schema_version="1", kind="plugin", source=descriptor.source, descriptor=descriptor
            )
            source = declaration.source
            if (
                source.distribution,
                source.version,
                source.entrypoint_group,
                source.entrypoint_name,
                source.entrypoint_value,
            ) != (name, project["version"], group, entry_name, value):
                raise ValueError(f"plugin source coordinates disagree with pyproject.toml: {name}")
            target = directory / source.declaration_path
            if target.is_symlink() or not target.resolve().is_relative_to(directory):
                raise ValueError(f"plugin declaration escapes package: {target}")
            if target in declarations:
                raise ValueError(f"duplicate plugin declaration target: {target}")
            declarations[target] = canonical_json_bytes(declaration.model_dump(mode="json")) + b"\n"
    return declarations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", action="append", help="Workspace package; repeat to select several.")
    parser.add_argument("--out-dir", type=Path, default=Path("dist"))
    parser.add_argument("--offline", action="store_true", help="Use only cached uv build dependencies.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check", action="store_true", help="Check generated declarations without writing/building."
    )
    mode.add_argument(
        "--declarations-only", action="store_true", help="Refresh declarations without building."
    )
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        projects = workspace_projects(root)
        selected = list(dict.fromkeys(args.package or projects))
        unknown = set(selected) - projects.keys()
        if unknown:
            raise ValueError(f"unknown workspace packages: {', '.join(sorted(unknown))}")
        # Validate all selected providers before updating any generated file.
        declarations = plugin_declarations(projects, selected)
        stale = [
            target
            for target, content in declarations.items()
            if not target.is_file() or target.read_bytes() != content
        ]
        for target in stale:
            print(f"{'STALE' if args.check else 'GENERATED'} {target.relative_to(root)}", flush=True)
            if not args.check:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(declarations[target])
        if args.check:
            return int(bool(stale))
        if not args.declarations_only:
            for name in selected:
                command = [
                    "uv",
                    "build",
                    "--wheel",
                    "--no-sources",
                    "--python",
                    "3.11",
                    "--package",
                    name,
                    "--out-dir",
                    str(args.out_dir.resolve()),
                ]
                if args.offline:
                    command.append("--offline")
                subprocess.run(command, cwd=root, check=True)
    except subprocess.CalledProcessError as error:
        return error.returncode
    except (ImportError, AttributeError, KeyError, OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
