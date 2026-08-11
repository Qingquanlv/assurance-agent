# OpenCode OpenAI Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an independent OpenCode benchmark loop that maps all former DeepSeek phases to GPT-5.6 Luna and all former GLM phases to GPT-5.6 Terra without modifying the existing benchmark route policy at runtime.

**Architecture:** `load_config()` gains an opt-in, read-only `AA_MODEL_ROUTING_FILE` override that replaces only the validated `execution.model_routing` value. A dedicated OpenAI route document and a derived benchmark loop select that override and use an `opencode-openai` artifact identity, while the existing OpenCode loop and `.aa/config.yaml` remain untouched.

**Tech Stack:** Python 3.11, Pydantic, PyYAML, Bash, pytest, uv, Ruff.

## Global Constraints

- `anthropic/deepseek-v4-flash` maps to `openai/gpt-5.6-luna`.
- `anthropic/glm-5.2` maps to `openai/gpt-5.6-terra`.
- Use the non-fast OpenAI model variants.
- Do not write or temporarily replace `benchmark/vue-fastapi-admin/.aa/config.yaml`.
- Do not change `benchmark/vue-fastapi-admin/benchmark/run-workflow-loop.sh`.
- Keep `strict_routes: true` and preserve the existing escalation error kinds.
- Reject a non-empty global `OPENCODE_MODEL` in the new entrypoint.
- Use `uv run ...` for Python project commands.

---

### Task 1: Read-only model-routing override

**Files:**
- Modify: `assurance_agent/config.py`
- Create: `tests/unit/test_config_model_routing_override.py`

**Interfaces:**
- Consumes: `load_config(root: Path) -> AaConfig`, `ModelRoutingCfg`, and the existing no-duplicate YAML loader.
- Produces: environment variable `AA_MODEL_ROUTING_FILE`; helper `_load_model_routing_override(root: Path) -> ModelRoutingCfg | None`; unchanged `load_config()` behavior when the variable is unset.

- [ ] **Step 1: Write failing tests for unset, valid relative, missing, malformed, and schema-invalid overrides**

```python
from pathlib import Path

import pytest

from assurance_agent.config import ConfigInvalidError, load_config
from assurance_agent.workflow.core.templates import InitAnswers, build_config_yaml


def _write_project(root: Path) -> None:
    (root / ".aa").mkdir()
    (root / ".aa/config.yaml").write_text(
        build_config_yaml(InitAnswers()), encoding="utf-8"
    )


def test_model_routing_override_is_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_project(tmp_path)
    monkeypatch.delenv("AA_MODEL_ROUTING_FILE", raising=False)
    assert load_config(tmp_path).execution.model_routing is None


def test_relative_model_routing_override_replaces_only_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_project(tmp_path)
    (tmp_path / "benchmark").mkdir()
    (tmp_path / "benchmark/openai-routing.yaml").write_text(
        """default: openai/gpt-5.6-luna
strict_routes: true
routes:
  aa-case-design: openai/gpt-5.6-terra
escalation:
  model: openai/gpt-5.6-terra
  on_error_kinds: [invalid_output, forbidden_write]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("AA_MODEL_ROUTING_FILE", "benchmark/openai-routing.yaml")

    config = load_config(tmp_path)

    assert config.sources.frontend == "./frontend"
    assert config.execution.model_routing is not None
    assert config.execution.model_routing.default == "openai/gpt-5.6-luna"
    assert config.execution.model_routing.routes == {
        "aa-case-design": "openai/gpt-5.6-terra"
    }


@pytest.mark.parametrize(
    ("filename", "contents"),
    [
        ("missing.yaml", None),
        ("malformed.yaml", "routes: ["),
        ("invalid.yaml", "routes: {aa-case-design: gpt-5.6-terra}\n"),
    ],
)
def test_invalid_model_routing_override_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    contents: str | None,
) -> None:
    _write_project(tmp_path)
    if contents is not None:
        (tmp_path / filename).write_text(contents, encoding="utf-8")
    monkeypatch.setenv("AA_MODEL_ROUTING_FILE", filename)

    with pytest.raises(ConfigInvalidError, match="AA_MODEL_ROUTING_FILE"):
        load_config(tmp_path)
```

- [ ] **Step 2: Run the new test module and confirm RED**

Run: `uv run pytest tests/unit/test_config_model_routing_override.py -v`

Expected: the valid override test fails because `load_config()` ignores `AA_MODEL_ROUTING_FILE`; invalid override cases do not raise.

- [ ] **Step 3: Implement the validated override loader**

Add `import os`, an environment constant, and this helper to `assurance_agent/config.py`:

```python
MODEL_ROUTING_FILE_ENV = "AA_MODEL_ROUTING_FILE"


def _load_model_routing_override(root: Path) -> ModelRoutingCfg | None:
    configured = os.environ.get(MODEL_ROUTING_FILE_ENV, "").strip()
    if not configured:
        return None
    path = Path(configured)
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        raise ConfigInvalidError(
            f"{MODEL_ROUTING_FILE_ENV} file not found: {configured}"
        )
    try:
        raw = _safe_load_no_duplicates(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as err:
        raise ConfigInvalidError(
            f"{MODEL_ROUTING_FILE_ENV} parse error: {err}"
        ) from err
    try:
        return ModelRoutingCfg.model_validate(raw)
    except ValidationError as err:
        first = err.errors()[0]
        loc = ".".join(str(part) for part in first["loc"])
        raise ConfigInvalidError(
            f"{MODEL_ROUTING_FILE_ENV} schema invalid: {loc}: {first['msg']}"
        ) from err
```

After validating the base `AaConfig`, apply the immutable in-memory update:

```python
config = AaConfig.model_validate(raw)
override = _load_model_routing_override(root)
if override is None:
    return config
execution = config.execution.model_copy(update={"model_routing": override})
return config.model_copy(update={"execution": execution})
```

Keep the existing base-config `ValidationError` conversion unchanged.

- [ ] **Step 4: Run focused tests and Ruff**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_config_model_routing_override.py -v`

Expected: all tests pass.

Run: `uv run ruff check assurance_agent/config.py tests/unit/test_config_model_routing_override.py`

Expected: exit 0.

- [ ] **Step 5: Commit the config override**

```bash
git add assurance_agent/config.py tests/unit/test_config_model_routing_override.py
git commit -m "feat(config): allow model routing override file"
```

---

### Task 2: Independent Luna/Terra OpenCode benchmark

**Files:**
- Create: `benchmark/vue-fastapi-admin/benchmark/opencode-openai-model-routing.yaml`
- Create: `benchmark/vue-fastapi-admin/benchmark/run-workflow-loop-opencode-openai.sh`
- Create: `tests/unit/benchmark/test_opencode_openai_loop.py`

**Interfaces:**
- Consumes: `AA_MODEL_ROUTING_FILE` from Task 1 and the lifecycle behavior of `run-workflow-loop.sh`.
- Produces: executable `run-workflow-loop-opencode-openai.sh`; complete Luna/Terra route map; artifact suffix `opencode-openai`.

- [ ] **Step 1: Write failing route and script contract tests**

```python
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


def test_openai_routes_mirror_existing_hybrid_policy() -> None:
    base = yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))["execution"][
        "model_routing"
    ]
    openai = yaml.safe_load(OPENAI_ROUTES.read_text(encoding="utf-8"))

    assert openai["strict_routes"] is True
    assert set(openai["routes"]) == set(base["routes"])
    assert openai["default"] == "openai/gpt-5.6-luna"
    for skill, original_model in base["routes"].items():
        expected = {
            "anthropic/deepseek-v4-flash": "openai/gpt-5.6-luna",
            "anthropic/glm-5.2": "openai/gpt-5.6-terra",
        }[original_model]
        assert openai["routes"][skill] == expected, skill
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
    assert '$RUNSTAMP-opencode-openai' in source
    assert '${base_id}-${RUNSTAMP}-opencode-openai' in source
    assert "opencode-openai-loop-latest.pid" in source
    assert "AA_MODEL_ROUTING_FILE" not in original
    assert "opencode-openai" not in original


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
```

- [ ] **Step 2: Run the contract tests and confirm RED**

Run: `uv run pytest tests/unit/benchmark/test_opencode_openai_loop.py -v`

Expected: collection succeeds, then tests fail because the route and loop files do not exist.

- [ ] **Step 3: Add the complete OpenAI routing document**

Create a direct `ModelRoutingCfg` YAML document by copying every current route key from `.aa/config.yaml` and replacing model values with the exact mapping in Global Constraints. Preserve:

```yaml
default: openai/gpt-5.6-luna
strict_routes: true
routes:
  aa-explore: openai/gpt-5.6-luna
  aa-fact-baseline: openai/gpt-5.6-luna
  aa-case-design: openai/gpt-5.6-terra
  aa-case-fixer: openai/gpt-5.6-terra
  aa-api-plan: openai/gpt-5.6-terra
  aa-api-plan-fixer: openai/gpt-5.6-terra
  aa-e2e-plan: openai/gpt-5.6-terra
  aa-e2e-plan-fixer: openai/gpt-5.6-terra
  aa-fuzz-plan: openai/gpt-5.6-terra
  aa-performance-plan: openai/gpt-5.6-terra
  aa-coverage-repair: openai/gpt-5.6-terra
  aa-case-reviewer: openai/gpt-5.6-luna
  aa-api-plan-reviewer: openai/gpt-5.6-luna
  aa-e2e-plan-reviewer: openai/gpt-5.6-luna
  aa-fuzz-plan-reviewer: openai/gpt-5.6-luna
  aa-performance-plan-reviewer: openai/gpt-5.6-luna
  aa-api-codegen: openai/gpt-5.6-luna
  aa-api-codegen-fixer: openai/gpt-5.6-luna
  aa-e2e-codegen: openai/gpt-5.6-luna
  aa-e2e-codegen-fixer: openai/gpt-5.6-luna
  aa-fuzz-codegen: openai/gpt-5.6-luna
  aa-performance-codegen: openai/gpt-5.6-luna
  aa-fix-proposal: openai/gpt-5.6-luna
  aa-issue-analyzer: openai/gpt-5.6-luna
  aa-issue-triage-advisor: openai/gpt-5.6-luna
  aa-archive: openai/gpt-5.6-luna
  aa-retro-issue-analysis: openai/gpt-5.6-luna
  aa-retro-workflow-analysis: openai/gpt-5.6-luna
  aa-retro-eval-analysis: openai/gpt-5.6-luna
  aa-retro: openai/gpt-5.6-luna
  aa-improvement-reviewer: openai/gpt-5.6-terra
escalation:
  model: openai/gpt-5.6-terra
  on_error_kinds:
    - invalid_output
    - forbidden_write
```

The route-key parity test additionally guards against future omissions.

- [ ] **Step 4: Derive the independent OpenAI loop**

Mechanically copy `run-workflow-loop.sh` to `run-workflow-loop-opencode-openai.sh`, then make only these behavior changes:

```bash
AA_MODEL_ROUTING_FILE="$SCRIPT_DIR/opencode-openai-model-routing.yaml"
export AA_MODEL_ROUTING_FILE
```

After `OPENCODE_MODEL` is initialized, reject a global override before any run directory is created:

```bash
if [ -n "$OPENCODE_MODEL" ]; then
  printf 'ERROR: OPENCODE_MODEL must be empty for the Luna/Terra OpenAI benchmark\n' >&2
  exit 1
fi
```

Replace persisted identity tokens only:

```text
$RUNSTAMP-opencode            -> $RUNSTAMP-opencode-openai
${RUNSTAMP}-opencode          -> ${RUNSTAMP}-opencode-openai
${base_id}-${RUNSTAMP}-opencode -> ${base_id}-${RUNSTAMP}-opencode-openai
opencode-loop-                -> opencode-openai-loop-
opencode-loop-latest          -> opencode-openai-loop-latest
```

Update the script title/start/done log text to identify the OpenAI variant. Keep adapter names, `.opencode.log` extensions, agent sync, server validation, and lifecycle logic unchanged. Mark the new script executable.

- [ ] **Step 5: Run focused benchmark contract tests**

Run: `uv run pytest tests/unit/benchmark/test_opencode_openai_loop.py tests/unit/benchmark/test_opencode_loop_parity.py -v`

Expected: all tests pass.

Run: `bash -n benchmark/vue-fastapi-admin/benchmark/run-workflow-loop.sh benchmark/vue-fastapi-admin/benchmark/run-workflow-loop-opencode-openai.sh`

Expected: exit 0.

- [ ] **Step 6: Commit the OpenAI benchmark entrypoint**

```bash
git add benchmark/vue-fastapi-admin/benchmark/opencode-openai-model-routing.yaml benchmark/vue-fastapi-admin/benchmark/run-workflow-loop-opencode-openai.sh tests/unit/benchmark/test_opencode_openai_loop.py
git commit -m "feat(benchmark): add OpenAI OpenCode loop"
```

---

### Task 3: Final verification and review

**Files:**
- Verify only; no planned file changes.

**Interfaces:**
- Consumes: Task 1 and Task 2 deliverables.
- Produces: evidence that the isolated script, route mapping, and existing test suite are healthy.

- [ ] **Step 1: Run targeted tests together**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_config_model_routing_override.py tests/unit/benchmark/test_opencode_openai_loop.py tests/unit/benchmark/test_opencode_loop_parity.py -v`

Expected: all tests pass.

- [ ] **Step 2: Run formatting, type, and shell checks for changed files**

Run: `uv run ruff check assurance_agent/config.py tests/unit/test_config_model_routing_override.py tests/unit/benchmark/test_opencode_openai_loop.py`

Run: `uv run ruff format --check assurance_agent/config.py tests/unit/test_config_model_routing_override.py tests/unit/benchmark/test_opencode_openai_loop.py`

Run: `uv run pyright assurance_agent/config.py tests/unit/test_config_model_routing_override.py tests/unit/benchmark/test_opencode_openai_loop.py`

Run: `bash -n benchmark/vue-fastapi-admin/benchmark/run-workflow-loop.sh benchmark/vue-fastapi-admin/benchmark/run-workflow-loop-opencode-openai.sh`

Expected: every command exits 0.

- [ ] **Step 3: Run the full test suite once**

Run: `uv run pytest`

Expected: exit 0. If unrelated pre-existing worktree changes cause failures, preserve the complete failure list and distinguish them from the targeted feature checks.

- [ ] **Step 4: Verify source-file isolation and model availability**

Run: `git diff 92b6832 -- benchmark/vue-fastapi-admin/.aa/config.yaml benchmark/vue-fastapi-admin/benchmark/run-workflow-loop.sh`

Expected: no new diff introduced after the design commit; compare against the pre-implementation working-tree snapshot because both files may already contain user changes.

Run: `opencode models openai`

Expected: output contains both `openai/gpt-5.6-luna` and `openai/gpt-5.6-terra`.

- [ ] **Step 5: Review the implementation**

Invoke the `code-review` skill against the two implementation commits. Resolve any correctness issue, rerun affected checks, and commit fixes separately.
