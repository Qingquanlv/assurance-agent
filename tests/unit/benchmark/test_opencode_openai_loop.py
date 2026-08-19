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
CURSOR_LOOP = BENCHMARK / "run-workflow-loop-cursor.sh"
TEST_PACKAGE_MARKER = ROOT / "benchmark/vue-fastapi-admin/tests/__init__.py"
E2E_CONFTEST = ROOT / "benchmark/vue-fastapi-admin/tests/e2e/conftest.py"
FUZZ_CONFTEST = ROOT / "benchmark/vue-fastapi-admin/tests/fuzz/conftest.py"
FUZZ_USER_TEST = ROOT / "benchmark/vue-fastapi-admin/tests/fuzz/test_user_fuzz.py"
PERF_USER_TEST = ROOT / "benchmark/vue-fastapi-admin/tests/perf/locustfile_user.py"
DOMAIN_FIXTURES = tuple(
    ROOT / f"benchmark/vue-fastapi-admin/tests/testdata/domain/{entity}.py"
    for entity in ("dept", "role", "user")
)


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


def test_benchmark_test_scaffold_is_an_importable_package() -> None:
    assert TEST_PACKAGE_MARKER.is_file()
    for script in (OPENAI_LOOP, ORIGINAL_LOOP, CURSOR_LOOP):
        source = script.read_text(encoding="utf-8")
        assert "tests/__init__.py" in source, script.name


def test_all_benchmark_loops_prepare_shared_execution_credentials() -> None:
    for script in (OPENAI_LOOP, ORIGINAL_LOOP, CURSOR_LOOP):
        source = script.read_text(encoding="utf-8")
        assert 'export API_BASE_URL="${API_BASE_URL:-$BASE_URL}"' in source, script.name
        assert 'export E2E_BACKEND_URL="${E2E_BACKEND_URL:-$BASE_URL}"' in source, script.name
        assert 'export QA_ADMIN_USERNAME="${QA_ADMIN_USERNAME:-admin}"' in source, script.name
        assert 'export AA_ADMIN_USERNAME="${AA_ADMIN_USERNAME:-$QA_ADMIN_USERNAME}"' in source
        assert "prepare_execution_credentials()" in source
        assert 'export E2E_API_TOKEN="${E2E_API_TOKEN:-$token}"' in source
        assert 'export API_ADMIN_TOKEN="${API_ADMIN_TOKEN:-$E2E_API_TOKEN}"' in source
        assert (
            "ensure_loop_sut || exit 1\n"
            "prepare_execution_credentials || exit 1\n"
            "ensure_loop_frontend || exit 1"
        ) in source


def test_http_phases_receive_bounded_sut_and_test_evidence() -> None:
    from assurance_agent.workflow.graph.contracts import load_execution_contracts

    catalog = load_execution_contracts(ROOT)
    for layer in ("api", "e2e", "fuzz", "performance"):
        test_root = "perf" if layer == "performance" else layer
        for phase in ("plan", "plan-reviewer", "codegen"):
            target = f"skill:aa-{layer}-{phase}"
            assert "repo:app/**" in catalog.contracts[target].reads, target
            assert "repo:**" not in catalog.contracts[target].reads, target
            if layer == "e2e":
                assert "repo:web/**" in catalog.contracts[target].reads, target
            if phase == "plan-reviewer":
                assert f"repo:tests/{test_root}/**" in catalog.contracts[target].reads, target
                assert "repo:tests/testdata/domain/**" in catalog.contracts[target].reads, target


def test_all_benchmark_loops_use_a_run_scoped_sut_database() -> None:
    for script in (OPENAI_LOOP, ORIGINAL_LOOP, CURSOR_LOOP):
        source = script.read_text(encoding="utf-8")
        run_dir = source.index('RUN_DIR="$SCRIPT_DIR/runs/$RUNSTAMP-')
        runtime_root = source.index('SUT_RUNTIME_ROOT="$RUN_DIR/sut-runtime"')
        database = source.index('export QA_SQLITE_FILE="${QA_SQLITE_FILE:-$SUT_RUNTIME_ROOT/db.sqlite3}"')
        assert run_dir < runtime_root < database, script.name
        assert 'cp -R "$PROJECT_ROOT/app/." "$SUT_RUNTIME_ROOT/app"' in source, script.name
        assert 'cd "$1" && exec "$2" -m uvicorn app:app' in source, script.name
        assert 'export QA_SQLITE_FILE="${QA_SQLITE_FILE:-$PROJECT_ROOT/db.sqlite3}"' not in source, (
            script.name
        )


def test_shared_e2e_login_uses_locators_present_in_the_real_dom() -> None:
    source = E2E_CONFTEST.read_text(encoding="utf-8")

    assert 'get_by_placeholder("admin")' in source
    assert 'get_by_placeholder("123456")' in source
    assert 'get_by_role("textbox", name=' not in source


def test_shared_fuzz_fixtures_use_the_live_sut_without_invented_app_imports() -> None:
    source = FUZZ_CONFTEST.read_text(encoding="utf-8")

    assert "httpx.Client" in source
    assert "from tests.fuzz.settings import" in source
    assert "base_url()" in source
    assert "from app.main" not in source
    assert "TestClient" not in source


def test_generated_http_scaffold_matches_observed_response_shapes() -> None:
    for fixture in DOMAIN_FIXTURES:
        source = fixture.read_text(encoding="utf-8")
        assert "was not found by exact-name lookup" in source, fixture.name

    performance = PERF_USER_TEST.read_text(encoding="utf-8")
    assert '{"data", "total", "page", "page_size"}.difference(body)' in performance
    assert ".difference(payload)" not in performance
    assert 'os.environ.get("BASE_URL")' in performance
    assert 'os.environ.get("API_BASE_URL")' in performance

    fuzz = FUZZ_USER_TEST.read_text(encoding="utf-8")
    assert 'if create_variant != "overlong_username"' in fuzz
    assert "invalid_seed = _valid_create_payload" in fuzz
    assert "_find_exact_user_id" in fuzz
    assert "dept_id if dept_id is not None else 0" in fuzz
    assert "min_size=8, max_size=10" in fuzz


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
