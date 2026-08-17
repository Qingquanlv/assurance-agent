from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[3]
BENCHMARK = ROOT / "benchmark/vue-fastapi-admin/benchmark"
BASE_CONFIG = ROOT / "benchmark/vue-fastapi-admin/.aa/config.yaml"
OPENAI_ROUTES = BENCHMARK / "opencode-openai-model-routing.yaml"
OPENAI_LOOP = BENCHMARK / "run-workflow-loop-opencode-openai.sh"
ORIGINAL_LOOP = BENCHMARK / "run-workflow-loop.sh"


def test_openai_routes_pin_every_skill_to_terra() -> None:
    base = yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))["execution"]["model_routing"]
    openai = yaml.safe_load(OPENAI_ROUTES.read_text(encoding="utf-8"))

    assert openai["strict_routes"] is True
    assert set(openai["routes"]) == set(base["routes"])
    assert openai["default"] == "openai/gpt-5.6-terra"
    for skill in base["routes"]:
        assert openai["routes"][skill] == "openai/gpt-5.6-terra", skill
    assert openai["escalation"] == {
        "model": "openai/gpt-5.6-terra",
        "on_error_kinds": ["invalid_output", "forbidden_write"],
    }


def test_openai_loop_is_syntax_valid_and_artifact_isolated() -> None:
    subprocess.run(["bash", "-n", str(OPENAI_LOOP)], check=True)
    source = OPENAI_LOOP.read_text(encoding="utf-8")
    original = ORIGINAL_LOOP.read_text(encoding="utf-8")

    assert 'AA_MODEL_ROUTING_FILE="$SCRIPT_DIR/opencode-openai-model-routing.yaml"' in source
    assert "export AA_MODEL_ROUTING_FILE" in source
    assert "$RUNSTAMP-opencode-openai" in source
    assert "${base_id}-${RUNSTAMP}-opencode-openai" in source
    assert "opencode-openai-loop-latest.pid" in source
    assert "AA_MODEL_ROUTING_FILE" not in original
    assert "opencode-openai" not in original


def test_openai_loop_uses_the_pinned_python_runtime() -> None:
    source = OPENAI_LOOP.read_text(encoding="utf-8")

    assert "python3" not in source
    assert '"$AA_PYTHON"' in source


def test_openai_loop_pins_routing_after_loading_benchmark_env() -> None:
    source = OPENAI_LOOP.read_text(encoding="utf-8")

    source_env = source.index('[ -f "$CONFIG_FILE" ] && source "$CONFIG_FILE"')
    pin_routing = source.index(
        'readonly AA_MODEL_ROUTING_FILE="$SCRIPT_DIR/opencode-openai-model-routing.yaml"'
    )
    assert source_env < pin_routing
    assert source.index("export AA_MODEL_ROUTING_FILE", pin_routing) > pin_routing


def test_openai_loop_pins_max_reasoning_variant() -> None:
    source = OPENAI_LOOP.read_text(encoding="utf-8")

    source_env = source.index('[ -f "$CONFIG_FILE" ] && source "$CONFIG_FILE"')
    pin_variant = source.index('readonly AA_OPENCODE_VARIANT="max"')
    assert source_env < pin_variant
    assert source.index("export AA_OPENCODE_VARIANT", pin_variant) > pin_variant


def test_openai_loop_never_cleans_shared_qa_artifacts() -> None:
    source = OPENAI_LOOP.read_text(encoding="utf-8")

    assert 'readonly CLEAN_ARTIFACTS="false"' in source
    assert 'CLEAN_ARTIFACTS="${CLEAN_ARTIFACTS:-true}"' not in source


def test_openai_loop_rejects_global_model_override(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "BENCHMARK_ENV": str(tmp_path / "missing.env"),
        "OPENCODE_MODEL": "openai/gpt-5.6-sol",
    }
    result = subprocess.run(
        ["bash", str(OPENAI_LOOP)],
        cwd=OPENAI_LOOP.parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "OPENCODE_MODEL must be empty" in result.stderr
