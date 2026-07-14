# M1 — 脚手架与项目管理命令 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 `assurance_agent` Python 包骨架、运行时资源分发机制，并交付可用的 `aa init` / `aa doctor` / `aa config print` 三个命令。

**Architecture:** 单一 click CLI 入口分发到 `commands/` 下的命令模块；命令模块只做参数解析与输出，业务逻辑在 `workflow/core/`（generator、checks、templates）与 `config.py`（pydantic 配置模型）。运行时资源（schemas）放在包内 `_resources/`，经 `resources.py` 用 `importlib.resources` 统一访问，禁止 `__file__` 相对路径。

**Tech Stack:** Python 3.11+, uv, click, pydantic v2, PyYAML, pytest, ruff, pyright。

## Global Constraints

- CLI 命令名 `aa`；Python 包名 `assurance_agent`；项目配置目录 `.aa/`（写入目标项目）。
- Skill / agent 前缀 `aa-*`；资源文件中不得残留 `aws` 字样（除非是无关英文单词，本里程碑不存在此情况）。
- 工具链固定：uv + pyproject.toml + pydantic v2 + click + ruff + pyright + pytest。
- 包内任何模块禁止用源码仓库相对路径（`Path(__file__)/..`）定位 `_resources/` 之外的资源；`_resources/` 只能经 `assurance_agent/resources.py` 访问。
- 分层约束：`commands/` 可以 import `workflow/`、`config`、`resources`；反向禁止。
- 每个 Task 结束必须 `git commit`；提交信息用 conventional commits（feat/test/chore/docs）。
- 本计划中所有 pytest 命令在仓库根目录运行：`uv run pytest <path> -v`。

---

### Task 1: pyproject + 包骨架 + `aa --version`

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `.gitignore`
- Create: `assurance_agent/__init__.py`
- Create: `assurance_agent/cli.py`
- Create: `assurance_agent/exceptions.py`
- Create: `tests/__init__.py`（空文件，下同）
- Create: `tests/integration/__init__.py`
- Test: `tests/integration/test_cli_entry.py`

**Interfaces:**
- Consumes: 无（首个任务）。
- Produces: `assurance_agent.cli:main`——click Group，后续所有命令模块通过 `main.add_command(...)` 挂载；`assurance_agent.__version__: str`；`assurance_agent.exceptions.AaError(Exception)`——CLI 层统一捕获并以退出码 1 结束。

- [ ] **Step 1: 写 pyproject.toml 与基础文件**

```toml
# pyproject.toml
[project]
name = "assurance-agent"
description = "Assurance Agent - AI-driven QA workflow orchestrator (deterministic CLI + skill suite)"
version = "0.1.0"
readme = "README.md"
requires-python = ">=3.11,<4.0"
dependencies = [
    "click>=8.1",
    "pydantic>=2.7",
    "pyyaml>=6.0",
]

[project.scripts]
aa = "assurance_agent.cli:main"

[dependency-groups]
dev = [
    "pytest>=8.0",
    "ruff>=0.5",
    "pyright>=1.1.380",
    "import-linter>=2.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["assurance_agent"]
# _resources 含非 .py 文件，hatchling 默认包含包内数据文件；
# artifacts 声明确保未来被 .gitignore 模式覆盖的资源也入包。
artifacts = ["assurance_agent/_resources/**"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 110
target-version = "py311"

[tool.pyright]
include = ["assurance_agent", "tests"]
typeCheckingMode = "standard"
```

```
# .python-version
3.11
```

```
# .gitignore
__pycache__/
*.pyc
.venv/
dist/
.pytest_cache/
.ruff_cache/
*.egg-info/
```

```python
# assurance_agent/__init__.py
__version__ = "0.1.0"
```

```python
# assurance_agent/exceptions.py
class AaError(Exception):
    """Base error for assurance-agent. CLI catches it and exits 1 with the message."""
```

```python
# assurance_agent/cli.py
import click

from assurance_agent import __version__


@click.group()
@click.version_option(__version__, prog_name="aa")
def main() -> None:
    """aa - Assurance Agent deterministic QA workflow CLI."""
```

- [ ] **Step 2: 写失败测试**

```python
# tests/integration/test_cli_entry.py
from click.testing import CliRunner

from assurance_agent.cli import main


def test_version_flag_prints_version() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "aa, version 0.1.0" in result.output


def test_help_lists_program_description() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Assurance Agent" in result.output
```

- [ ] **Step 3: 安装依赖并跑测试**

Run: `uv sync && uv run pytest tests/integration/test_cli_entry.py -v`
Expected: 2 passed（本任务实现先于测试完成，测试直接通过即可；后续任务严格先红后绿）

- [ ] **Step 4: 验证控制台入口**

Run: `uv run aa --version`
Expected: `aa, version 0.1.0`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .python-version .gitignore assurance_agent/ tests/ uv.lock
git commit -m "feat: scaffold assurance_agent package with aa CLI entry"
```

---

### Task 2: 资源分发——`_resources/` + `resources.py`

**Files:**
- Create: `assurance_agent/_resources/__init__.py`（空）
- Create: `assurance_agent/_resources/schemas/workflow-schema.yaml`（自源仓库改写）
- Create: `assurance_agent/_resources/schemas/explore-advisory.schema.json`（自源仓库复制）
- Create: `assurance_agent/_resources/schemas/explore-context.schema.json`（自源仓库复制）
- Create: `assurance_agent/resources.py`
- Create: `tests/unit/__init__.py`
- Test: `tests/unit/test_resources.py`

**Interfaces:**
- Consumes: 无。
- Produces: `resources.read_text(*relpath: str) -> str`；`resources.exists(*relpath: str) -> bool`；`resources.iter_children(*relpath: str) -> list[str]`。后续里程碑（schema 加载、skill refresh）都经这三个函数访问包内资源。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_resources.py
import yaml

from assurance_agent import resources


def test_read_packaged_workflow_schema() -> None:
    text = resources.read_text("schemas", "workflow-schema.yaml")
    doc = yaml.safe_load(text)
    assert doc["schema_version"] == "1"
    assert doc["name"] == "aa-full"


def test_workflow_schema_has_no_legacy_aws_references() -> None:
    text = resources.read_text("schemas", "workflow-schema.yaml")
    assert "aws" not in text.lower()


def test_exists_and_missing() -> None:
    assert resources.exists("schemas", "workflow-schema.yaml")
    assert not resources.exists("schemas", "no-such-file.yaml")


def test_iter_children_lists_schema_files() -> None:
    names = resources.iter_children("schemas")
    assert "workflow-schema.yaml" in names
    assert "explore-advisory.schema.json" in names
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_resources.py -v`
Expected: FAIL（`ModuleNotFoundError`/`FileNotFoundError`）

- [ ] **Step 3: 复制并改写资源文件**

```bash
mkdir -p assurance_agent/_resources/schemas
touch assurance_agent/_resources/__init__.py
SRC=/Users/lvqingquan/skills/assurance-workflow-skills/schemas
cp "$SRC/explore-advisory.schema.json" assurance_agent/_resources/schemas/
cp "$SRC/explore-context.schema.json" assurance_agent/_resources/schemas/
sed -e 's/name: aws-full/name: aa-full/' \
    -e 's/aws-/aa-/g' \
    -e 's/\.aws\//.aa\//g' \
    -e 's/`aws /`aa /g' \
    "$SRC/workflow-schema.yaml" > assurance_agent/_resources/schemas/workflow-schema.yaml
```

然后人工检查改写结果（sed 只覆盖机械模式）：

```bash
grep -ni 'aws' assurance_agent/_resources/schemas/*.{yaml,json}
```

对每一处残留逐个处理：头部注释 `AWS Workflow Schema` 改为 `AA Workflow Schema`；正文中 `aws status` / `aws gate check` 等命令示例改为 `aa ...`；`name: aws-full` 改为 `name: aa-full`；两个 JSON schema 里如有 `aws` 字样同样改写。处理完后上面 grep 应无输出，Step 1 的严格断言测试才能通过。

- [ ] **Step 4: 实现 resources.py**

```python
# assurance_agent/resources.py
"""Sole access point for packaged runtime resources under assurance_agent/_resources/.

Never locate resources via __file__ arithmetic elsewhere in the codebase;
importlib.resources keeps this working from wheels and editable installs alike.
"""
from importlib.resources import files
from importlib.abc import Traversable


def _root() -> Traversable:
    return files("assurance_agent") / "_resources"


def _node(*relpath: str) -> Traversable:
    node = _root()
    for part in relpath:
        node = node / part
    return node


def read_text(*relpath: str) -> str:
    return _node(*relpath).read_text(encoding="utf-8")


def exists(*relpath: str) -> bool:
    node = _node(*relpath)
    return node.is_file() or node.is_dir()


def iter_children(*relpath: str) -> list[str]:
    return sorted(child.name for child in _node(*relpath).iterdir())
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_resources.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add assurance_agent/_resources assurance_agent/resources.py tests/unit
git commit -m "feat: package runtime schemas under _resources with importlib.resources accessor"
```

---

### Task 3: 初始化模板（templates.py）

**Files:**
- Create: `assurance_agent/workflow/__init__.py`（空）
- Create: `assurance_agent/workflow/core/__init__.py`（空）
- Create: `assurance_agent/workflow/core/templates.py`
- Test: `tests/unit/test_templates.py`

**Interfaces:**
- Consumes: 无。
- Produces: `InitAnswers`（pydantic 模型）；`build_config_yaml(answers: InitAnswers) -> str`；`build_execution_policy(answers: InitAnswers) -> dict`；`build_module_map_yaml() -> str`；`build_data_knowledge_yaml() -> str`。Task 5 的 generator 消费全部四个 builder。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_templates.py
import json

import yaml

from assurance_agent.workflow.core.templates import (
    InitAnswers,
    build_config_yaml,
    build_data_knowledge_yaml,
    build_execution_policy,
    build_module_map_yaml,
)


def make_answers(**overrides: object) -> InitAnswers:
    defaults: dict = {
        "api_framework": "pytest",
        "e2e_framework": "playwright",
        "enable_mcp": False,
        "frontend_path": None,
        "backend_path": None,
    }
    defaults.update(overrides)
    return InitAnswers(**defaults)


def test_config_yaml_defaults_parse_and_point_to_aa_paths() -> None:
    doc = yaml.safe_load(build_config_yaml(make_answers()))
    assert doc["version"] == 1
    assert doc["sources"] == {"frontend": "./frontend", "backend": "./backend"}
    assert doc["frameworks"]["api"] == {"enabled": True, "name": "pytest"}
    assert doc["frameworks"]["e2e"] == {"enabled": True, "name": "playwright"}
    assert doc["execution"]["policy_file"] == "./.aa/execution-policy.json"
    assert doc["generation"]["prd_input_mode"] == "prompt"
    assert doc["execution"]["self_healing"]["mode"] == "proposal-only"
    assert doc["generation"]["e2e"]["default_pom"] is False
    assert doc["mcp"]["enabled"] is False


def test_config_yaml_none_framework_disables_layer() -> None:
    doc = yaml.safe_load(build_config_yaml(make_answers(api_framework="none")))
    assert doc["frameworks"]["api"] == {"enabled": False, "name": "none"}


def test_config_yaml_custom_source_paths() -> None:
    doc = yaml.safe_load(build_config_yaml(make_answers(frontend_path="./web", backend_path="./server")))
    assert doc["sources"] == {"frontend": "./web", "backend": "./server"}


def test_execution_policy_targets_follow_enabled_layers() -> None:
    policy = build_execution_policy(make_answers(e2e_framework="none"))
    assert policy["targets"] == ["api"]
    assert policy["parallel"] == {"api": 4}
    assert policy["retry"] == {"api": 0}
    assert "e2e" not in policy
    assert policy["healing"]["mode"] == "proposal-only"
    json.dumps(policy)  # 必须可序列化


def test_module_map_and_data_knowledge_parse() -> None:
    module_map = yaml.safe_load(build_module_map_yaml())
    assert isinstance(module_map["rules"], list)
    knowledge = yaml.safe_load(build_data_knowledge_yaml())
    assert knowledge["version"] == 1
    assert knowledge["accounts"] == {}
    assert set(knowledge["capabilities"]["adapters"]) == {"api", "e2e", "fuzz", "performance"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_templates.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 实现 templates.py**

四个模板从源仓库 `src/workflow/templates/*.ts` 的字符串逐段移植，仅做 `.aws/` → `.aa/` 与 `aws init` → `aa init` 替换。完整实现：

```python
# assurance_agent/workflow/core/templates.py
"""Project scaffolding templates written by `aa init`.

Ported from the TS templates (config-yaml.ts, execution-policy.ts,
module-map-yaml.ts, data-knowledge-yaml.ts) with .aws -> .aa renames.
"""
from typing import Literal

from pydantic import BaseModel


class InitAnswers(BaseModel):
    api_framework: Literal["pytest", "none"] = "pytest"
    e2e_framework: Literal["playwright", "none"] = "playwright"
    enable_mcp: bool = False
    frontend_path: str | None = None
    backend_path: str | None = None


def build_config_yaml(answers: InitAnswers) -> str:
    api_enabled = answers.api_framework != "none"
    e2e_enabled = answers.e2e_framework != "none"
    frontend = answers.frontend_path or "./frontend"
    backend = answers.backend_path or "./backend"

    return f"""version: 1

project:
  name: ""
  root: .
  layout: default

sources:
  frontend: {frontend}
  backend: {backend}

qa:
  cases: ./qa/cases
  changes: ./qa/changes

tests:
  root: ./tests
  api: ./tests/api
  e2e: ./tests/e2e
  fuzz: ./tests/fuzz
  fixtures: ./tests/fixtures
  helpers: ./tests/helpers
  reports: ./tests/reports

frameworks:
  api:
    enabled: {str(api_enabled).lower()}
    name: {answers.api_framework}
  e2e:
    enabled: {str(e2e_enabled).lower()}
    name: {answers.e2e_framework}

workflow:
  primary_runner: skill
  agent: opencode
  agents:
    claude_code: false
    codex: false

mcp:
  enabled: {str(answers.enable_mcp).lower()}

generation:
  prd_input_mode: prompt
  e2e:
    default_pom: false
    locator_priority:
      - role
      - label
      - testid
      - css
  api:
    prefer_existing_fixtures: true

execution:
  entry: cli
  policy_file: ./.aa/execution-policy.json
  ci_must_use_cli: true
  product_code_roots:
    - app
    - web/src
    - src
  self_healing:
    mode: proposal-only
    allow_assertion_change: false
    allow_product_code_change: false
    allow_auto_merge: false

review:
  require_case_review: true
  require_subplan_review: true
  require_fix_proposal_review: true

archive:
  enable_trace_check: true
  regression_default: true

coverage:
  enabled: true
  mode: pytest-cov             # pytest-cov | server-process
  server_command: ""           # e.g. "uvicorn app:app --port 9999" when mode=server-process
  server_port: 0               # set when mode=server-process
  target_package: app          # --cov=<target_package>
  threshold:
    line: 70
    branch: 60
    module_line: 80
    diff_line: 90
  gate_mode: warn              # warn: below threshold -> PASS_WITH_WARNINGS. block: below -> FAIL.

# Fuzz layer (schemathesis via pytest). Tests live under tests/fuzz/.
fuzz:
  enabled: true
  schema_source: ""            # OpenAPI URL or file

# Performance layer (Locust, absolute thresholds). Locustfiles live under tests/perf/.
performance:
  enabled: true
  base_url: http://localhost:8000
  default_load:
    users: 10
    spawn_rate: 2
    run_time_s: 30
"""


def build_execution_policy(answers: InitAnswers) -> dict:
    api_enabled = answers.api_framework != "none"
    e2e_enabled = answers.e2e_framework != "none"

    targets: list[str] = []
    parallel: dict[str, int] = {}
    retry: dict[str, int] = {}
    if api_enabled:
        targets.append("api")
        parallel["api"] = 4
        retry["api"] = 0
    if e2e_enabled:
        targets.append("e2e")
        parallel["e2e"] = 2
        retry["e2e"] = 1

    policy: dict = {"tier": "local", "targets": targets, "parallel": parallel, "retry": retry}
    if api_enabled:
        policy["api"] = {"timeoutSeconds": 30}
    if e2e_enabled:
        policy["e2e"] = {
            "browsers": ["chromium"],
            "trace": "on-first-retry",
            "screenshot": "only-on-failure",
            "video": "retain-on-failure",
        }
    policy["healing"] = {
        "mode": "proposal-only",
        "maxAttempts": 2,
        "allowAssertionChange": False,
        "allowProductCodeChange": False,
        "allowAutoMerge": False,
        "testChangesOverride": "forbidden",
    }
    return policy


def build_module_map_yaml() -> str:
    return """# Module map for `aa risk context`
# Maps changed file paths to QA modules (supports one-to-many).
#
# rules:
#   - pattern: glob relative to project root
#     modules: [module names matching qa/cases/<module>/]
#     confidence: high | medium | low
#     reason: optional explanation for medium/low mappings

rules:
  - pattern: "backend/app/api/v1/menus/**"
    modules: ["menus"]
    confidence: high

  - pattern: "backend/app/core/auth/**"
    modules: ["users", "roles", "menus"]
    confidence: medium
    reason: "auth middleware affects protected modules"

  - pattern: "backend/app/models/**"
    modules: ["users", "roles", "menus"]
    confidence: low
    reason: "shared persistence model"
"""


def build_data_knowledge_yaml() -> str:
    return """# =============================================================================
# .aa/data-knowledge.yaml - L1 static domain knowledge (human-maintained)
# =============================================================================
#
# This is the FORMAL knowledge base that codegen skills read before generating
# tests. It is a SCAFFOLD created by `aa init`: fill it in before running
# codegen. `aa-api-codegen` / `aa-e2e-codegen` STOP when this file is empty
# of the capability they need.
#
# Do NOT let skills write here directly - planning writes discoveries to
# `qa/changes/<id>/plans/data-knowledge.proposal.yaml`; a human promotes
# confirmed entries into this file.
# =============================================================================

version: 1

# Test accounts and their permission levels.
# Never hardcode real production credentials in tests - reference these keys.
accounts: {}

# Auth mechanism and where a valid token comes from.
auth: {}

# Business entities and their known states (menus, users, roles, depts, ...).
entities: {}

# Reusable test-data capabilities. Domain factories describe business-valid
# data independently of a test runner. Adapters own the execution mechanism for
# one test layer and must not be reused across layer boundaries.
capabilities:
  domain_factories: {}
  adapters:
    api: {}
    e2e: {}
    fuzz: {}
    performance: {}
  cleanup: {}
"""
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_templates.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add assurance_agent/workflow tests/unit/test_templates.py
git commit -m "feat: add aa init scaffolding templates (config, policy, module map, data knowledge)"
```

---

### Task 4: 项目配置模型（config.py）

**Files:**
- Create: `assurance_agent/config.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: Task 3 的 `build_config_yaml`（测试中用它生成合法配置）。
- Produces: `AaConfig`（pydantic 模型，`extra="allow"`，字段见实现）；`load_config(root: Path) -> AaConfig`——找不到 `.aa/config.yaml` 时抛 `ConfigNotFoundError(AaError)`，解析/校验失败抛 `ConfigInvalidError(AaError)`。Task 7 doctor 和后续所有读配置的命令消费它。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_config.py
from pathlib import Path

import pytest

from assurance_agent.config import AaConfig, ConfigInvalidError, ConfigNotFoundError, load_config
from assurance_agent.workflow.core.templates import InitAnswers, build_config_yaml


def write_default_config(root: Path) -> None:
    (root / ".aa").mkdir()
    (root / ".aa/config.yaml").write_text(build_config_yaml(InitAnswers()), encoding="utf-8")


def test_load_config_parses_generated_template(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    cfg = load_config(tmp_path)
    assert cfg.sources.frontend == "./frontend"
    assert cfg.frameworks.api.enabled is True
    assert cfg.generation.prd_input_mode == "prompt"
    assert cfg.execution.entry == "cli"
    assert cfg.execution.self_healing.mode == "proposal-only"
    assert cfg.generation.e2e.default_pom is False


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigNotFoundError):
        load_config(tmp_path)


def test_load_config_invalid_yaml_raises(tmp_path: Path) -> None:
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa/config.yaml").write_text("frameworks: [not-a-mapping", encoding="utf-8")
    with pytest.raises(ConfigInvalidError):
        load_config(tmp_path)


def test_load_config_schema_violation_raises(tmp_path: Path) -> None:
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa/config.yaml").write_text("version: 1\nsources: {frontend: 1, backend: ./b}\n", encoding="utf-8")
    with pytest.raises(ConfigInvalidError):
        load_config(tmp_path)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 实现 config.py**

```python
# assurance_agent/config.py
"""Load and validate the target project's .aa/config.yaml."""
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from assurance_agent.exceptions import AaError

CONFIG_RELPATH = ".aa/config.yaml"


class ConfigNotFoundError(AaError):
    pass


class ConfigInvalidError(AaError):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class SourcesCfg(_Model):
    frontend: str
    backend: str


class QaCfg(_Model):
    cases: str
    changes: str


class TestsCfg(_Model):
    root: str
    api: str
    e2e: str
    fixtures: str = "./tests/fixtures"
    helpers: str = "./tests/helpers"
    reports: str = "./tests/reports"


class FrameworkCfg(_Model):
    enabled: bool
    name: str


class FrameworksCfg(_Model):
    api: FrameworkCfg
    e2e: FrameworkCfg


class E2eGenerationCfg(_Model):
    default_pom: bool


class GenerationCfg(_Model):
    prd_input_mode: str
    e2e: E2eGenerationCfg


class SelfHealingCfg(_Model):
    mode: str


class ExecutionCfg(_Model):
    entry: str
    self_healing: SelfHealingCfg


class AaConfig(_Model):
    version: int
    sources: SourcesCfg
    qa: QaCfg
    tests: TestsCfg
    frameworks: FrameworksCfg
    generation: GenerationCfg
    execution: ExecutionCfg


def load_config(root: Path) -> AaConfig:
    path = root / CONFIG_RELPATH
    if not path.is_file():
        raise ConfigNotFoundError(f"{CONFIG_RELPATH} not found. Run `aa init` first.")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as err:
        raise ConfigInvalidError(f"{CONFIG_RELPATH} parse error: {err}") from err
    try:
        return AaConfig.model_validate(raw)
    except ValidationError as err:
        first = err.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise ConfigInvalidError(f"{CONFIG_RELPATH} schema invalid: {loc}: {first['msg']}") from err
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add assurance_agent/config.py tests/unit/test_config.py
git commit -m "feat: add pydantic model and loader for .aa/config.yaml"
```

---

### Task 5: 项目生成器（generator.py）

**Files:**
- Create: `assurance_agent/workflow/core/generator.py`
- Test: `tests/unit/test_generator.py`

**Interfaces:**
- Consumes: Task 3 的 `InitAnswers` 与四个 builder。
- Produces: `GenerateResult`（pydantic 模型：`created: list[str]`、`skipped: list[str]`）；`generate_project(root: Path, answers: InitAnswers) -> GenerateResult`；`repair_project(root: Path) -> GenerateResult`——缺 `.aa/config.yaml` 时抛 `ConfigNotFoundError`。Task 6 的 `aa init` 消费两者。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_generator.py
import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.config import ConfigNotFoundError
from assurance_agent.workflow.core.generator import GITKEEP_DIRS, generate_project, repair_project
from assurance_agent.workflow.core.templates import InitAnswers


def test_generate_project_writes_config_and_scaffold(tmp_path: Path) -> None:
    result = generate_project(tmp_path, InitAnswers())

    assert ".aa/config.yaml" in result.created
    assert yaml.safe_load((tmp_path / ".aa/config.yaml").read_text())["version"] == 1
    policy = json.loads((tmp_path / ".aa/execution-policy.json").read_text())
    assert policy["targets"] == ["api", "e2e"]
    assert (tmp_path / ".aa/module-map.yaml").is_file()
    assert (tmp_path / ".aa/data-knowledge.yaml").is_file()
    for rel in GITKEEP_DIRS:
        assert (tmp_path / rel / ".gitkeep").is_file(), rel
    assert result.skipped == []


def test_generate_project_never_overwrites_data_knowledge(tmp_path: Path) -> None:
    generate_project(tmp_path, InitAnswers())
    knowledge = tmp_path / ".aa/data-knowledge.yaml"
    knowledge.write_text("version: 1\naccounts: {admin: {}}\n", encoding="utf-8")

    result = generate_project(tmp_path, InitAnswers())

    assert ".aa/data-knowledge.yaml" in result.skipped
    assert "admin" in knowledge.read_text()


def test_repair_only_creates_missing(tmp_path: Path) -> None:
    generate_project(tmp_path, InitAnswers())
    config_before = (tmp_path / ".aa/config.yaml").read_text()
    (tmp_path / "qa/cases/.gitkeep").unlink()

    result = repair_project(tmp_path)

    assert "qa/cases/.gitkeep" in result.created
    assert (tmp_path / ".aa/config.yaml").read_text() == config_before


def test_repair_without_config_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigNotFoundError):
        repair_project(tmp_path)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_generator.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 实现 generator.py**

```python
# assurance_agent/workflow/core/generator.py
"""Write the .aa/ + qa/ + tests/ scaffold into a target project."""
import json
from pathlib import Path

from pydantic import BaseModel

from assurance_agent.config import CONFIG_RELPATH, ConfigNotFoundError
from assurance_agent.workflow.core.templates import (
    InitAnswers,
    build_config_yaml,
    build_data_knowledge_yaml,
    build_execution_policy,
    build_module_map_yaml,
)

GITKEEP_DIRS = [
    ".aa/cache",
    "qa/cases",
    "qa/changes",
    "tests/api",
    "tests/api/adapters",
    "tests/e2e",
    "tests/e2e/adapters",
    "tests/fuzz/adapters",
    "tests/fuzz/strategies",
    "tests/perf/adapters",
    "tests/testdata/domain",
    "tests/helpers",
    "tests/reports",
]


class GenerateResult(BaseModel):
    created: list[str] = []
    skipped: list[str] = []


def _write(root: Path, relpath: str, content: str, result: GenerateResult, overwrite: bool) -> None:
    path = root / relpath
    if path.exists() and not overwrite:
        result.skipped.append(relpath)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    result.created.append(relpath)


def generate_project(root: Path, answers: InitAnswers) -> GenerateResult:
    result = GenerateResult()
    _write(root, ".aa/config.yaml", build_config_yaml(answers), result, overwrite=True)
    policy = json.dumps(build_execution_policy(answers), indent=2) + "\n"
    _write(root, ".aa/execution-policy.json", policy, result, overwrite=True)
    _write(root, ".aa/module-map.yaml", build_module_map_yaml(), result, overwrite=True)
    # Human-maintained knowledge base - never overwrite a filled-in file.
    _write(root, ".aa/data-knowledge.yaml", build_data_knowledge_yaml(), result, overwrite=False)
    for rel in GITKEEP_DIRS:
        _write(root, f"{rel}/.gitkeep", "", result, overwrite=False)
    return result


def repair_project(root: Path) -> GenerateResult:
    if not (root / CONFIG_RELPATH).is_file():
        raise ConfigNotFoundError(f"{CONFIG_RELPATH} not found. Run `aa init` first.")
    result = GenerateResult()
    for rel in GITKEEP_DIRS:
        _write(root, f"{rel}/.gitkeep", "", result, overwrite=False)
    _write(root, ".aa/module-map.yaml", build_module_map_yaml(), result, overwrite=False)
    _write(root, ".aa/data-knowledge.yaml", build_data_knowledge_yaml(), result, overwrite=False)
    return result
```

注意 `generate_project` 中 `.gitkeep` 用 `overwrite=False`：重复 init 时目录已存在不算 created（源版 safeWriteFile 语义）。`test_generate_project_writes_config_and_scaffold` 的 `result.skipped == []` 只对全新目录成立。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_generator.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add assurance_agent/workflow/core/generator.py tests/unit/test_generator.py
git commit -m "feat: add project scaffold generator with repair mode"
```

---

### Task 6: `aa init` 命令

**Files:**
- Create: `assurance_agent/commands/__init__.py`（空）
- Create: `assurance_agent/commands/init_cmd.py`
- Modify: `assurance_agent/cli.py`
- Test: `tests/integration/test_cli_init.py`

**Interfaces:**
- Consumes: Task 5 `generate_project` / `repair_project`；Task 3 `InitAnswers`。
- Produces: `aa init` 子命令。选项：`--repair`、`--yes`（跳过交互取默认值）、`--api-framework [pytest|none]`、`--e2e-framework [playwright|none]`、`--frontend PATH`、`--backend PATH`、`--enable-mcp`。OpenCode 注册（opencode.json 写入、agents/tools 复制）**不在本里程碑**：M7 交付后再往 `aa init` 追加该调用，本任务的实现与输出完全不涉及 OpenCode。

- [ ] **Step 1: 写失败测试**

```python
# tests/integration/test_cli_init.py
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main


def test_init_yes_writes_scaffold(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["init", "--yes"])
        assert result.exit_code == 0, result.output
        assert "created: .aa/config.yaml" in result.output
        assert Path(".aa/execution-policy.json").is_file()
        assert Path("qa/changes/.gitkeep").is_file()


def test_init_options_override_defaults(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            main,
            ["init", "--yes", "--e2e-framework", "none", "--frontend", "./web"],
        )
        assert result.exit_code == 0, result.output
        config = Path(".aa/config.yaml").read_text()
        assert "frontend: ./web" in config
        assert "name: none" in config


def test_init_repair_requires_existing_config(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["init", "--repair"])
        assert result.exit_code == 1
        assert ".aa/config.yaml not found" in result.output


def test_init_repair_recreates_missing_gitkeep(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init", "--yes"])
        Path("qa/cases/.gitkeep").unlink()
        result = runner.invoke(main, ["init", "--repair"])
        assert result.exit_code == 0, result.output
        assert "created: qa/cases/.gitkeep" in result.output


def test_init_interactive_prompts_for_frameworks(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        # prompts: api framework, e2e framework, enable MCP, confirm
        result = runner.invoke(main, ["init"], input="pytest\nplaywright\nn\ny\n")
        assert result.exit_code == 0, result.output
        assert Path(".aa/config.yaml").is_file()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/integration/test_cli_init.py -v`
Expected: FAIL（`No such command 'init'`）

- [ ] **Step 3: 实现 init_cmd.py 并挂载**

```python
# assurance_agent/commands/init_cmd.py
from pathlib import Path

import click

from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.generator import GenerateResult, generate_project, repair_project
from assurance_agent.workflow.core.templates import InitAnswers


def _print_result(result: GenerateResult) -> None:
    for rel in result.created:
        click.secho(f"created: {rel}", fg="green")
    for rel in result.skipped:
        click.secho(f"skipped (exists): {rel}", fg="yellow")


@click.command("init")
@click.option("--repair", is_flag=True, help="Repair mode: only create missing files, never overwrite.")
@click.option("--yes", is_flag=True, help="Non-interactive: accept all defaults.")
@click.option("--api-framework", type=click.Choice(["pytest", "none"]), default=None)
@click.option("--e2e-framework", type=click.Choice(["playwright", "none"]), default=None)
@click.option("--frontend", default=None, help="Frontend source path (default ./frontend).")
@click.option("--backend", default=None, help="Backend source path (default ./backend).")
@click.option("--enable-mcp", is_flag=True, default=None)
def init_command(
    repair: bool,
    yes: bool,
    api_framework: str | None,
    e2e_framework: str | None,
    frontend: str | None,
    backend: str | None,
    enable_mcp: bool | None,
) -> None:
    """Initialize an assurance-agent QA project in the current directory."""
    root = Path.cwd()
    try:
        if repair:
            click.echo("Running repair...")
            _print_result(repair_project(root))
            click.secho("Repair complete.", fg="green", bold=True)
            return

        answers = _collect_answers(yes, api_framework, e2e_framework, frontend, backend, enable_mcp)
        if answers is None:
            click.echo("Init cancelled.")
            return
        _print_result(generate_project(root, answers))
        click.secho("assurance-agent initialized successfully.", fg="green", bold=True)
        click.echo("Run 'aa doctor' to verify your environment.")
    except AaError as err:
        # stdout（而非 stderr）：click 8.2+ 的 CliRunner.output 不再合并 stderr，
        # 错误走 stdout 让集成测试与人工使用看到一致的输出。
        click.secho(str(err), fg="red")
        raise SystemExit(1)


def _collect_answers(
    yes: bool,
    api_framework: str | None,
    e2e_framework: str | None,
    frontend: str | None,
    backend: str | None,
    enable_mcp: bool | None,
) -> InitAnswers | None:
    if yes:
        return InitAnswers(
            api_framework=api_framework or "pytest",
            e2e_framework=e2e_framework or "playwright",
            enable_mcp=bool(enable_mcp),
            frontend_path=frontend,
            backend_path=backend,
        )

    api = api_framework or click.prompt(
        "API test framework", type=click.Choice(["pytest", "none"]), default="pytest"
    )
    e2e = e2e_framework or click.prompt(
        "E2E test framework", type=click.Choice(["playwright", "none"]), default="playwright"
    )
    mcp = enable_mcp if enable_mcp is not None else click.confirm("Enable MCP config?", default=False)
    if not click.confirm("Confirm and write files?", default=True):
        return None
    return InitAnswers(
        api_framework=api, e2e_framework=e2e, enable_mcp=mcp,
        frontend_path=frontend, backend_path=backend,
    )
```

```python
# assurance_agent/cli.py（追加）
import click

from assurance_agent import __version__
from assurance_agent.commands.init_cmd import init_command


@click.group()
@click.version_option(__version__, prog_name="aa")
def main() -> None:
    """aa - Assurance Agent deterministic QA workflow CLI."""


main.add_command(init_command)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_cli_init.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add assurance_agent/commands assurance_agent/cli.py tests/integration/test_cli_init.py
git commit -m "feat: add aa init command with repair and non-interactive modes"
```

---

### Task 7: doctor 检查引擎 + `aa doctor`

**Files:**
- Create: `assurance_agent/workflow/core/checks.py`
- Create: `assurance_agent/commands/doctor.py`
- Modify: `assurance_agent/cli.py`
- Test: `tests/unit/test_checks.py`
- Test: `tests/integration/test_cli_doctor.py`

**Interfaces:**
- Consumes: Task 4 `load_config` / `ConfigNotFoundError` / `ConfigInvalidError`。
- Produces: `CheckResult`（pydantic：`id: str`、`group: str`、`status: Literal["ok","warning","error"]`、`message: str`、`suggested_fix: str | None = None`）；`DoctorResult`（`status: Literal["ok","warning","error"]`、`checks: list[CheckResult]`）；`run_doctor_checks(root: Path) -> DoctorResult`；`aa doctor [--json]` 命令（error → 退出码 1，否则 0）。检查组本里程碑覆盖 config / sources / directories / frameworks；workflow / execution / mcp 组由后续里程碑扩展。

- [ ] **Step 1: 写失败单元测试**

```python
# tests/unit/test_checks.py
import shutil
from pathlib import Path

from assurance_agent.workflow.core.checks import run_doctor_checks
from assurance_agent.workflow.core.generator import generate_project
from assurance_agent.workflow.core.templates import InitAnswers


def check_ids(result) -> dict[str, str]:
    return {c.id: c.status for c in result.checks}


def test_missing_config_is_single_error(tmp_path: Path) -> None:
    result = run_doctor_checks(tmp_path)
    assert result.status == "error"
    assert check_ids(result) == {"config.exists": "error"}


def test_fresh_scaffold_reports_config_ok_sources_warning(tmp_path: Path) -> None:
    generate_project(tmp_path, InitAnswers())
    result = run_doctor_checks(tmp_path)
    ids = check_ids(result)
    assert ids["config.exists"] == "ok"
    assert ids["config.schema"] == "ok"
    assert ids["config.prd_input_mode"] == "ok"
    assert ids["config.execution_entry"] == "ok"
    assert ids["config.self_healing_mode"] == "ok"
    assert ids["config.e2e_default_pom"] == "ok"
    # ./frontend 和 ./backend 不存在 -> warning，不 error
    assert ids["sources.frontend"] == "warning"
    assert ids["sources.backend"] == "warning"
    assert ids["dir.qa.cases"] == "ok"
    assert result.status in ("ok", "warning")


def test_invalid_config_value_is_error(tmp_path: Path) -> None:
    generate_project(tmp_path, InitAnswers())
    config = tmp_path / ".aa/config.yaml"
    config.write_text(config.read_text().replace("entry: cli", "entry: manual"))
    result = run_doctor_checks(tmp_path)
    assert check_ids(result)["config.execution_entry"] == "error"
    assert result.status == "error"


def test_framework_check_reflects_binary_presence(tmp_path: Path) -> None:
    generate_project(tmp_path, InitAnswers())
    result = run_doctor_checks(tmp_path)
    ids = check_ids(result)
    expected = "ok" if shutil.which("pytest") else "warning"
    assert ids["framework.pytest"] == expected
```

- [ ] **Step 2: 跑单元测试确认失败**

Run: `uv run pytest tests/unit/test_checks.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 实现 checks.py**

```python
# assurance_agent/workflow/core/checks.py
"""Deterministic environment checks behind `aa doctor`."""
import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from assurance_agent.config import (
    CONFIG_RELPATH,
    AaConfig,
    ConfigInvalidError,
    ConfigNotFoundError,
    load_config,
)

CheckStatus = Literal["ok", "warning", "error"]


class CheckResult(BaseModel):
    id: str
    group: str
    status: CheckStatus
    message: str
    suggested_fix: str | None = None


class DoctorResult(BaseModel):
    status: CheckStatus
    checks: list[CheckResult]


def _build(checks: list[CheckResult]) -> DoctorResult:
    status: CheckStatus = "ok"
    if any(c.status == "error" for c in checks):
        status = "error"
    elif any(c.status == "warning" for c in checks):
        status = "warning"
    return DoctorResult(status=status, checks=checks)


def _value_check(check_id: str, ok: bool, ok_msg: str, fix: str) -> CheckResult:
    if ok:
        return CheckResult(id=check_id, group="config", status="ok", message=ok_msg)
    return CheckResult(id=check_id, group="config", status="error", message=fix, suggested_fix=fix)


def _path_check(root: Path, rel: str, check_id: str, group: str, label: str) -> CheckResult:
    if (root / rel).exists():
        return CheckResult(id=check_id, group=group, status="ok", message=f"{label} exists")
    return CheckResult(id=check_id, group=group, status="warning", message=f"{label} not found: {rel}")


def _framework_check(name: str) -> CheckResult:
    if shutil.which(name):
        return CheckResult(id=f"framework.{name}", group="frameworks", status="ok",
                           message=f"{name} available")
    return CheckResult(id=f"framework.{name}", group="frameworks", status="warning",
                       message=f"{name} not found on PATH",
                       suggested_fix=f"Install {name} in the project environment")


def run_doctor_checks(root: Path) -> DoctorResult:
    checks: list[CheckResult] = []
    try:
        cfg = load_config(root)
    except ConfigNotFoundError:
        checks.append(CheckResult(
            id="config.exists", group="config", status="error",
            message=f"{CONFIG_RELPATH} not found", suggested_fix="Run `aa init`",
        ))
        return _build(checks)
    except ConfigInvalidError as err:
        checks.append(CheckResult(id="config.exists", group="config", status="ok",
                                  message=f"{CONFIG_RELPATH} found"))
        checks.append(CheckResult(id="config.schema", group="config", status="error",
                                  message=str(err)))
        return _build(checks)

    checks.append(CheckResult(id="config.exists", group="config", status="ok",
                              message=f"{CONFIG_RELPATH} found"))
    checks.append(CheckResult(id="config.schema", group="config", status="ok",
                              message="config schema valid"))
    checks.extend(_config_value_checks(cfg))
    checks.extend(_source_and_dir_checks(root, cfg))
    checks.extend(_framework_checks(cfg))
    return _build(checks)


def _config_value_checks(cfg: AaConfig) -> list[CheckResult]:
    return [
        _value_check("config.prd_input_mode", cfg.generation.prd_input_mode == "prompt",
                     "PRD input mode = prompt", 'generation.prd_input_mode must be "prompt"'),
        _value_check("config.execution_entry", cfg.execution.entry == "cli",
                     "execution entry = cli", 'execution.entry must be "cli"'),
        _value_check("config.self_healing_mode", cfg.execution.self_healing.mode == "proposal-only",
                     "self-healing mode = proposal-only",
                     'execution.self_healing.mode must be "proposal-only"'),
        _value_check("config.e2e_default_pom", cfg.generation.e2e.default_pom is False,
                     "e2e.default_pom = false", "generation.e2e.default_pom must be false"),
    ]


def _source_and_dir_checks(root: Path, cfg: AaConfig) -> list[CheckResult]:
    checks = [
        _path_check(root, cfg.sources.frontend, "sources.frontend", "sources", "frontend path"),
        _path_check(root, cfg.sources.backend, "sources.backend", "sources", "backend path"),
    ]
    dirs = [
        ("dir.qa.cases", cfg.qa.cases, "qa/cases"),
        ("dir.qa.changes", cfg.qa.changes, "qa/changes"),
        ("dir.tests.api", cfg.tests.api, "tests/api"),
        ("dir.tests.e2e", cfg.tests.e2e, "tests/e2e"),
        ("dir.tests.helpers", cfg.tests.helpers, "tests/helpers"),
        ("dir.tests.reports", cfg.tests.reports, "tests/reports"),
    ]
    checks.extend(_path_check(root, rel, cid, "directories", label) for cid, rel, label in dirs)
    return checks


def _framework_checks(cfg: AaConfig) -> list[CheckResult]:
    checks: list[CheckResult] = []
    if cfg.frameworks.api.enabled:
        checks.append(_framework_check(cfg.frameworks.api.name))
    if cfg.frameworks.e2e.enabled:
        checks.append(_framework_check(cfg.frameworks.e2e.name))
    return checks
```

- [ ] **Step 4: 跑单元测试确认通过**

Run: `uv run pytest tests/unit/test_checks.py -v`
Expected: 4 passed

- [ ] **Step 5: 写失败集成测试**

```python
# tests/integration/test_cli_doctor.py
import json

from click.testing import CliRunner

from assurance_agent.cli import main


def test_doctor_without_init_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 1
        assert ".aa/config.yaml not found" in result.output


def test_doctor_after_init_exits_0_and_reports_groups() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--yes"])
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0, result.output
        assert "Config" in result.output
        assert "Result:" in result.output


def test_doctor_json_is_machine_readable() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--yes"])
        result = runner.invoke(main, ["doctor", "--json"])
        assert result.exit_code == 0
        doc = json.loads(result.output)
        assert doc["status"] in ("ok", "warning")
        assert any(c["id"] == "config.schema" for c in doc["checks"])
```

- [ ] **Step 6: 跑集成测试确认失败**

Run: `uv run pytest tests/integration/test_cli_doctor.py -v`
Expected: FAIL（`No such command 'doctor'`）

- [ ] **Step 7: 实现 doctor 命令并挂载**

```python
# assurance_agent/commands/doctor.py
from pathlib import Path

import click

from assurance_agent.workflow.core.checks import CheckResult, DoctorResult, run_doctor_checks

_GROUP_LABELS = {
    "config": "Config",
    "sources": "Sources",
    "directories": "Directories",
    "frameworks": "Frameworks",
}
_ICONS = {"ok": ("✓", "green"), "warning": ("!", "yellow"), "error": ("✗", "red")}


@click.command("doctor")
@click.option("--json", "as_json", is_flag=True, help="Output machine-readable JSON.")
def doctor_command(as_json: bool) -> None:
    """Check assurance-agent environment and configuration."""
    result = run_doctor_checks(Path.cwd())
    if as_json:
        click.echo(result.model_dump_json(indent=2))
    else:
        _print_result(result)
    raise SystemExit(1 if result.status == "error" else 0)


def _print_result(result: DoctorResult) -> None:
    click.secho("\naa doctor\n", bold=True)
    seen_groups: list[str] = []
    for check in result.checks:
        if check.group not in seen_groups:
            seen_groups.append(check.group)
            click.secho(_GROUP_LABELS.get(check.group, check.group), bold=True)
        _print_check(check)
    color = {"ok": "green", "warning": "yellow", "error": "red"}[result.status]
    click.echo("Result: " + click.style(result.status.upper(), fg=color, bold=True))


def _print_check(check: CheckResult) -> None:
    icon, color = _ICONS[check.status]
    click.echo(f"{click.style(icon, fg=color)} {check.message}")
    if check.suggested_fix and check.status != "ok":
        click.secho(f"  -> {check.suggested_fix}", dim=True)
```

```python
# assurance_agent/cli.py（追加两行）
from assurance_agent.commands.doctor import doctor_command
main.add_command(doctor_command)
```

- [ ] **Step 8: 跑集成测试确认通过**

Run: `uv run pytest tests/integration/test_cli_doctor.py -v`
Expected: 3 passed

- [ ] **Step 9: Commit**

```bash
git add assurance_agent/workflow/core/checks.py assurance_agent/commands/doctor.py assurance_agent/cli.py tests/
git commit -m "feat: add aa doctor with config/sources/directories/frameworks checks"
```

---

### Task 8: `aa config print`

**Files:**
- Create: `assurance_agent/commands/config_cmd.py`
- Modify: `assurance_agent/cli.py`
- Test: `tests/integration/test_cli_config.py`

**Interfaces:**
- Consumes: `assurance_agent.config.CONFIG_RELPATH`。
- Produces: `aa config print` 子命令——原样输出 `.aa/config.yaml`，缺失时退出码 1。

- [ ] **Step 1: 写失败测试**

```python
# tests/integration/test_cli_config.py
from click.testing import CliRunner

from assurance_agent.cli import main


def test_config_print_outputs_raw_yaml() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--yes"])
        result = runner.invoke(main, ["config", "print"])
        assert result.exit_code == 0
        assert result.output.startswith("version: 1")


def test_config_print_missing_config_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["config", "print"])
        assert result.exit_code == 1
        assert ".aa/config.yaml not found" in result.output
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/integration/test_cli_config.py -v`
Expected: FAIL（`No such command 'config'`）

- [ ] **Step 3: 实现并挂载**

```python
# assurance_agent/commands/config_cmd.py
from pathlib import Path

import click

from assurance_agent.config import CONFIG_RELPATH


@click.group("config")
def config_group() -> None:
    """Manage assurance-agent configuration."""


@config_group.command("print")
def config_print() -> None:
    """Print the raw contents of .aa/config.yaml."""
    path = Path.cwd() / CONFIG_RELPATH
    if not path.is_file():
        click.secho(f"{CONFIG_RELPATH} not found. Run `aa init` first.", fg="red")
        raise SystemExit(1)
    click.echo(path.read_text(encoding="utf-8"), nl=False)
```

```python
# assurance_agent/cli.py（追加两行）
from assurance_agent.commands.config_cmd import config_group
main.add_command(config_group)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_cli_config.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add assurance_agent/commands/config_cmd.py assurance_agent/cli.py tests/integration/test_cli_config.py
git commit -m "feat: add aa config print command"
```

---

### Task 9: Lint / 类型检查 / 分层约束 / pre-commit

**Files:**
- Create: `.importlinter`
- Create: `.pre-commit-config.yaml`
- Modify: 前序任务中 ruff/pyright 报出的文件（如有）

**Interfaces:**
- Consumes: 全部已有代码。
- Produces: 三条可复跑的质量门禁命令，后续所有里程碑沿用：`uv run ruff check .`、`uv run pyright`、`uv run lint-imports`。

- [ ] **Step 1: 写分层契约**

```ini
# .importlinter
[importlinter]
root_package = assurance_agent

[importlinter:contract:layers]
name = commands depend on domain, never the reverse
type = layers
layers =
    assurance_agent.cli
    assurance_agent.commands
    assurance_agent.workflow
    assurance_agent.config
    assurance_agent.resources
```

- [ ] **Step 2: 写 pre-commit 配置**

```yaml
# .pre-commit-config.yaml
repos:
  - repo: local
    hooks:
      - id: ruff
        name: ruff check
        entry: uv run ruff check --fix
        language: system
        types: [python]
      - id: ruff-format
        name: ruff format
        entry: uv run ruff format
        language: system
        types: [python]
      - id: pyright
        name: pyright
        entry: uv run pyright
        language: system
        types: [python]
        pass_filenames: false
```

- [ ] **Step 3: 跑三条门禁并修复**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run lint-imports`
Expected: 全部通过。如 ruff/pyright 报错，逐个修复（典型问题：未用 import、缺类型标注）；不得用 `# type: ignore` 静默。

- [ ] **Step 4: 全量测试回归**

Run: `uv run pytest -v`
Expected: 前序全部测试通过（约 22 个）

- [ ] **Step 5: Commit**

```bash
git add .importlinter .pre-commit-config.yaml
git add -u
git commit -m "chore: add ruff/pyright/import-linter gates and pre-commit config"
```

---

### Task 10: 打包冒烟测试

**Files:**
- Create: `scripts/packaging_smoke_test.sh`

**Interfaces:**
- Consumes: 全部已交付命令。
- Produces: 可在 CI 与本地复跑的冒烟脚本，验证 wheel 安装后资源解析不依赖源码仓库（spec 第 15 节）。M9 把它接入 CI。

- [ ] **Step 1: 写脚本**

```bash
#!/usr/bin/env bash
# scripts/packaging_smoke_test.sh
# Build a wheel, install it into a throwaway venv, and run aa outside the
# source tree to prove packaged resources resolve without the repo.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

cd "$REPO_ROOT"
uv build --wheel --out-dir "$WORK_DIR/dist"

python3 -m venv "$WORK_DIR/venv"
"$WORK_DIR/venv/bin/pip" install --quiet "$WORK_DIR"/dist/*.whl

mkdir "$WORK_DIR/project"
cd "$WORK_DIR/project"

"$WORK_DIR/venv/bin/aa" --version
"$WORK_DIR/venv/bin/aa" init --yes
test -f .aa/config.yaml
test -f .aa/execution-policy.json
"$WORK_DIR/venv/bin/aa" doctor --json > doctor.json
python3 - <<'PY'
import json
doc = json.load(open("doctor.json"))
assert doc["status"] in ("ok", "warning"), doc["status"]
PY
"$WORK_DIR/venv/bin/aa" config print > /dev/null

# Packaged resources must be readable from the wheel install.
"$WORK_DIR/venv/bin/python" - <<'PY'
from assurance_agent import resources
assert "aa-full" in resources.read_text("schemas", "workflow-schema.yaml")
PY

echo "packaging smoke test: OK"
```

- [ ] **Step 2: 赋权并运行**

Run: `chmod +x scripts/packaging_smoke_test.sh && ./scripts/packaging_smoke_test.sh`
Expected: 最后输出 `packaging smoke test: OK`。若 wheel 内缺 `_resources`（`FileNotFoundError`），检查 pyproject 的 `[tool.hatch.build.targets.wheel]` artifacts 配置。

- [ ] **Step 3: 全量回归 + 收尾**

Run: `uv run pytest -v && uv run ruff check . && uv run pyright && uv run lint-imports`
Expected: 全部通过

- [ ] **Step 4: Commit**

```bash
git add scripts/packaging_smoke_test.sh
git commit -m "test: add packaging smoke test proving wheel-only resource resolution"
```

---

## M1 验收清单

- `uv run aa init --yes && uv run aa doctor && uv run aa config print` 在全新目录可用。
- `./scripts/packaging_smoke_test.sh` 通过（源码目录之外、纯 wheel 安装运行）。
- `uv run pytest` / `ruff` / `pyright` / `lint-imports` 全绿。
- 包内资源仅经 `assurance_agent/resources.py` 访问；`workflow-schema.yaml` 无 `aws` 残留。
