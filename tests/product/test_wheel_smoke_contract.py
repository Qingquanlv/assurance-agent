from __future__ import annotations

import re
from pathlib import Path
from types import ModuleType
from typing import Callable

import pytest

PluginSources = dict[str, tuple[str, str | None]]
ClosureCheck = Callable[[PluginSources, str, str], None]


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def smoke_script(repo_root: Path) -> str:
    return (repo_root / "scripts/assurance_product_wheel_smoke_test.sh").read_text()


@pytest.fixture
def closure_check(smoke_script: str) -> ClosureCheck:
    prefix = "cat >\"$smoke_root/check.py\" <<'PY'\n"
    embedded = smoke_script.split(prefix, maxsplit=1)[1].split("\nPY\n", maxsplit=1)[0]
    module = ModuleType("wheel_smoke_check")
    exec(compile(embedded, "wheel-smoke-check.py", "exec"), module.__dict__)
    return module.__dict__["check_selected_closure"]


def _selected_sources(adapter: str, binding_distribution: str) -> PluginSources:
    sources = {
        "assurance.execution": ("wheel_plugin", "assurance-execution"),
        "assurance.generation": ("wheel_plugin", "assurance-generation"),
        "assurance.healing": ("wheel_plugin", "assurance-healing"),
        "assurance.improvement": ("wheel_plugin", "assurance-improvement"),
        "assurance.intake": ("wheel_plugin", "assurance-intake"),
        "assurance.product.agent": ("wheel_plugin", binding_distribution),
        "assurance.product.configuration": ("config_tree", None),
        "assurance.quality": ("wheel_plugin", "assurance-quality"),
    }
    sources[f"runtime.{adapter}"] = ("wheel_plugin", f"agent-runtime-{adapter}")
    return sources


@pytest.mark.parametrize("adapter", ["opencode", "cursor"])
def test_success_checker_requires_the_exact_selected_plugin_source_closure(
    closure_check: ClosureCheck,
    adapter: str,
) -> None:
    binding_distribution = f"assurance-product-bindings-{adapter}"
    exact = _selected_sources(adapter, binding_distribution)

    closure_check(exact, adapter, binding_distribution)

    foreign_adapter = "cursor" if adapter == "opencode" else "opencode"
    with pytest.raises(SystemExit, match="selected plugin/source closure"):
        closure_check(
            exact
            | {
                f"runtime.{foreign_adapter}": (
                    "wheel_plugin",
                    f"agent-runtime-{foreign_adapter}",
                )
            },
            adapter,
            binding_distribution,
        )
    with pytest.raises(SystemExit, match="selected plugin/source closure"):
        closure_check(
            exact
            | {
                "assurance.product.agent": (
                    "wheel_plugin",
                    "assurance-product-bindings-foreign",
                )
            },
            adapter,
            binding_distribution,
        )


def _scenario_block(script: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^scenario {re.escape(name)}\n(?P<body>.*?)(?=^scenario |\Z)",
        script,
    )
    assert match is not None, f"missing independent {name} scenario"
    return match.group("body")


def test_wheel_smoke_covers_isolated_selection_and_binding_fault_matrix(
    smoke_script: str,
) -> None:
    assert "git archive HEAD" in smoke_script
    assert 'cd "$smoke_root"' in smoke_script

    both_opencode = _scenario_block(smoke_script, "both-opencode")
    assert "expect_compile_ok both-adapters assurance-opencode" in both_opencode

    both_cursor = _scenario_block(smoke_script, "both-cursor")
    assert "expect_compile_ok both-adapters assurance-cursor" in both_cursor

    foreign = _scenario_block(smoke_script, "foreign-binding")
    assert foreign.count("expect_compile_fail") == 2
    assert "assurance-opencode" in foreign and "$cursor_binding_distribution" in foreign
    assert "assurance-cursor" in foreign and "$opencode_binding_distribution" in foreign

    deployment_drift = _scenario_block(smoke_script, "deployment-drift")
    assert "tamper_installed_deployment" in deployment_drift
    assert "RECORD hash mismatch" in deployment_drift

    extra_binding = _scenario_block(smoke_script, "extra-binding")
    assert "add_authenticated_extra_binding" in extra_binding
    assert "expect_compile_fail" in extra_binding
    assert "product_lock" in smoke_script
    assert "graph_manifest" in smoke_script
    assert "14 roots" in smoke_script or "len(entrypoints) != 14" in smoke_script
    assert "41" in smoke_script
    assert "33" in smoke_script
    assert "ResolvedRawAgentExecutor" in smoke_script
    assert "resources/workflow/module.yaml" in smoke_script
    assert "resources/workflow/main.yaml" in smoke_script
    assert "graph-inventory.yaml" in smoke_script
    assert "runtime_selection" not in smoke_script or "runtime_selection.py" in smoke_script
    assert 'Path(member).name == "workspace.py"' not in smoke_script
    assert 'member == "graph_engine/workspace.py"' in smoke_script
