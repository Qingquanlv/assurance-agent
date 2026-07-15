# M7 — Skills 全量改写 + OpenCode 集成 + `aa skill refresh` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 TS 源仓库的 33 个 skill 与 OpenCode 集成资产（6 个 subagent、JS 插件、tools）系统性改写为 `aa-*` 净室内容，落到包内 `assurance_agent/_resources/{skills,opencode}/`，并交付 `aa skill refresh`、扩展 `aa init` 的 OpenCode 注册，配套内容校验测试与打包冒烟验证。

**Architecture:** 一个**一次性 dev 工具** `scripts/migrate_skills.py`（不入 wheel）把源内容按确定性规则表批量改写生成到 `_resources/`；非机械内容（dashboard 的 Python 静态服务器、插件 `aa.mjs`、`workflow_start` tool、4 个需人工复核的 skill）在计划里逐个写全。运行时同步逻辑放在 `assurance_agent/workflow/core/assets.py`（只经 `resources.py` 读包内资源、写目标项目），命令壳 `commands/skill_cmd.py` 与扩展后的 `commands/init_cmd.py` 只做参数解析、输出与退出码。内容正确性作为普通 pytest 常驻（`tests/unit/test_skills_content.py`）：`aws` 残留、跨 skill 引用、字段交叉核对（spec 4a）。

**Tech Stack:** Python 3.11+, uv, click, pydantic v2, PyYAML, pytest, ruff, pyright, import-linter；OpenCode 宿主约束下保留 JS 插件（`aa.mjs`）与 TS tool（`workflow_start.ts`）。

## Global Constraints

- CLI 命令名 `aa`；Python 包名 `assurance_agent`；项目配置目录 `.aa/`（写入目标项目）。
- Skill / agent 前缀 `aa-*`（`writing-skills` 保留原名，无前缀）；资源内容中不得残留 `aws` 字样（判定用词边界正则，见 Task 4；经查源内容无以 `aws` 开头的普通英文词，允许清单为空）。
- 运行时资源唯一源在包内 `assurance_agent/_resources/{schemas,skills,opencode}/`，随 wheel 分发；仓库自身的 `skills/`、`.opencode/` 由 `aa skill refresh` 从 `_resources/` 派生（生成物，git 忽略），与目标项目同一条路径（spec 15）。
- 包内任何模块**禁止**用源码仓库相对路径（`Path(__file__)/..`）定位资源；`_resources/` 只能经 `assurance_agent/resources.py`（M1 已落地：`read_text` / `exists` / `iter_children`，签名不得改动）访问。
- 分层约束（`.importlinter`，本计划 Task 5 更新）：`cli → commands → workflow → artifacts → config → resources`。`workflow` 允许消费 M2 artifacts，反向禁止；`skill refresh` / `init` 的资产逻辑在 `workflow/core/`。
- TS 源仓库 `/Users/lvqingquan/skills/assurance-workflow-skills` 仅作**内容来源**：本里程碑对其做确定性改写与人工复核，不引入任何 TS 运行代码到 Python 包（OpenCode 的 JS 插件与 TS tool、dashboard 前端 HTML 属宿主约束下的允许项，spec 14）。
- 除 OpenCode JS 插件（`aa.mjs`）、`workflow_start.ts` tool、dashboard 的浏览器 `case-center.html` 外，不新增任何 Node/TS 代码；dashboard 的 Node 服务器被 Python `http.server` 替换（spec 8.3）。
- `migrate_skills.py` 是 dev 工具，**不进 wheel**（放 `scripts/`，Task 7 用 hatch 配置显式排除并冒烟验证）。
- 每个 Task 结束必须通过 `uv run ruff check .` 与 `uv run pyright`，然后 `git commit`；提交信息用 conventional commits（feat/test/chore/docs）。
- 本计划中所有 pytest 命令在仓库根目录运行：`uv run pytest <path> -v`。

## 消费的跨里程碑接口契约（计划系列总览「接口契约」为准）

- M1 `assurance_agent/resources.py`：`read_text(*relpath) -> str`、`exists(*relpath) -> bool`、`iter_children(*relpath) -> list[str]`（本里程碑不改其签名）。
- M1 `assurance_agent/exceptions.py`：`class AaError(Exception)`。
- M1 `assurance_agent/workflow/core/generator.py`：`generate_project(root, answers) -> GenerateResult`（`aa init` 已调用，本里程碑在其后追加 OpenCode 注册）。
- M1 `assurance_agent/config.py`：`CONFIG_RELPATH = ".aa/config.yaml"`。
- M2 `assurance_agent/artifacts/models`：`Review`/`FixProposal`/`FailureAnalysis`/`ExecutionManifest`/`QualityGateResult`/`QualityReport`/`Advisory`/`FactBaselineFull`/`FactBaselineUnavailable`/`WorkflowState`/`SafetyCheck`/`ApplySummary`/`CaseYaml`/`QaYaml`——Task 4 字段交叉核对消费其 `model_fields`。
- M6 `aa workflow start ...`：detached 启动入口（供 OpenCode 插件 `workflow_start` tool 调用）。Task 3 的 `workflow_start.ts` 只收集参数并调用 `aa workflow start`，循环逻辑不驻留 JS。

## 文件结构总览

新建 / 修改：

```
scripts/migrate_skills.py                         # 一次性 dev 工具（不入 wheel）；Task 1
assurance_agent/_resources/skills/<aa-name>/…     # 33 个 skill（Task 1 生成 + Task 2 dashboard 覆写）
assurance_agent/_resources/opencode/
├── agents/aa-*.md            # 6 个 subagent（Task 1 由脚本生成 agents 分支）
├── plugins/aa.mjs            # Task 3 明写
├── tools/workflow_start.ts   # Task 3 明写
└── INSTALL.md                # Task 3（改写）
assurance_agent/workflow/core/assets.py           # Task 5 同步逻辑 + Task 6 init 注册
assurance_agent/commands/skill_cmd.py             # Task 5 `aa skill refresh`
assurance_agent/commands/init_cmd.py              # Task 6 追加 OpenCode 注册（修改）
assurance_agent/cli.py                            # Task 5 挂 skill 命令（修改）
pyproject.toml                                    # Task 7 排除 scripts/ 出 wheel（修改）
scripts/packaging_smoke_test.sh                   # Task 7 扩展（修改）
.gitignore                                        # Task 5 忽略派生的 /skills /.opencode（修改）
tests/unit/test_skills_content.py                 # Task 4
tests/data/skill_field_allowlist.txt              # Task 4
tests/integration/test_cli_skill_refresh.py       # Task 5
tests/integration/test_cli_init_opencode.py        # Task 6
```

## 改写规则表（`migrate_skills.py` 确定性应用，spec 8）

按**先后顺序**对每个文本文件的内容与路径段应用。顺序重要：先处理多词短语与大写 sentinel，再处理小写 token，最后收尾大写缩写。

| # | 规则 | 匹配（正则/字面） | 替换 | 说明 |
|---|---|---|---|---|
| R1 | 项目全名 | 字面 `Assurance Workflow Skills` | `Assurance Agent` | 项目名 |
| R2 | Sentinel | `AWS-HEALING-SKILLS-UNAVAILABLE` | `AA-HEALING-SKILLS-UNAVAILABLE` | 大写标记，先于 R8 |
| R3 | 名称/前缀/跨引用 | `\baws-` | `aa-` | 目录名、`name:` frontmatter、`skills/aws-*`、跨 skill/agent 引用；`writing-skills` 无前缀不受影响 |
| R4 | 配置目录（带斜杠） | `\.aws/` | `.aa/` | `.aws/config.yaml` 等 |
| R5 | 配置目录（裸） | `\.aws\b` | `.aa` | 句中 `.aws` |
| R6 | 环境变量前缀 | `\bAWS_` | `AA_` | `AWS_OPENCODE_*` 等 |
| R7 | CLI 调用 | `(?<![\w-])aws (?=[a-z])` | `aa ` | `aws run` / `aws status` / `aws gate check` → `aa …` |
| R7b | 反引号裸命令 | `` `aws` `` | `` `aa` `` | 文档里孤立的 `` `aws` `` |
| R7c | 小写裸词收尾 | `\baws\b` | `aa` | 收尾剩余小写 `aws`（如 `aws CLI`、`aws/foo`）；词边界排除 `draws`/`jigsaws` 等 |
| R8 | 大写缩写 | `\bAWS\b` | `AA` | 收尾剩余 `AWS` |
| R9 | npm→uv（构建/链接） | 字面 `npm run build && npm link` | `uv sync` | 构建说明 |
| R10 | npm→uv（全局安装） | 字面 `npm install -g assurance-agent` | `uv tool install assurance-agent` | 安装说明（注意 R1/R3 已把包名改写） |

路径段改写：目录/文件名以 `aws-` 开头者按 R3 改为 `aa-`（skills/ 内实际只有顶层 skill 目录名需改；`.opencode/agents/aws-*.md`、`.opencode/plugins/aws.mjs` 同理）。

**脚本不处理、Task 2/3 明写或人工复核的非机械内容：**
- `aa-dashboard/scripts/server.cjs`（Node 服务器）——脚本**丢弃**（`DROP` 名单），由 Task 2 的 `server.py` 替换。
- `aa-dashboard/scripts/{start,stop}-server.sh`、`SKILL.md` 里的 `node server.cjs` → `python3 server.py` 与 "Node.js" → "Python 3"（Task 2 明写覆盖）。
- `.opencode/plugins/aa.mjs`、`.opencode/tools/workflow_start.ts` 的语义改写（`aws workflow run --detach` → `aa workflow start`）——由 Task 3 明写。
- 4 个需人工复核的 skill 的判断性内容——Task 1 Step 6 逐条处理。

## 人工复核清单（Task 1 Step 6 逐个处理，spec 8 要求「系统性改写而非照搬」）

已逐个巡检源内容，需人工介入的非机械改动：

- **aa-workflow**（`SKILL.md` + `FALLBACK-RUNBOOK.md`）：文中多处 "TS driver" / "the TS workflow driver" 需改为 "the aa workflow driver"（净室版无 TS）；确认首选入口 `aa workflow run --scope full` 与 `workflow_start` tool 描述、`aa status`/`aa gate check`/`workflow-state.yaml` 归属表述正确；`FALLBACK-RUNBOOK.md` 内 `phases.fact_baseline.*` 等 workflow-state 路径保持不变（它们是状态字段，不是 CLI）。
- **aa-test-infra-bootstrap**（`SKILL.md`）：正文 "Write the file using the templates in `templates/`" 引用了一个**并不存在**的 `templates/` 目录（该 skill 只有 SKILL.md）。人工改为把三份脚手架文件（`tests/config.py` / `tests/conftest.py` / `tests/schema_validation.py`）的契约要求内联描述，删去对 `templates/` 的路径引用，避免指向缺失资源。
- **aa-dashboard**（`SKILL.md`）：Requirements 段 "Node.js must be available" → "Python 3 must be available"（Task 2 覆写）。
- **writing-skills**（`SKILL.md` 及支撑文件）：保留原名与内容；它引用的是 `superpowers:*` 外部技能与 `@testing-skills-with-subagents.md` / `@graphviz-conventions.dot` 同目录文件，**不含** `aws`/`aa` 项目 CLI 引用，属通用元技能，机械规则不触及；人工只需确认残留检查（Task 4）不误报、且 `render-graphs.js`（作者用可选 Node 辅助脚本，非运行时依赖）保留原样即可，无需改写。

---

### Task 1: 迁移脚本 + 生成 33 个 skill + 6 个 agent 到 `_resources/`

**Files:**
- Create: `scripts/migrate_skills.py`
- Generate（脚本产出，纳入 git）：`assurance_agent/_resources/skills/<aa-name>/…`（33 个）、`assurance_agent/_resources/opencode/agents/aa-*.md`（6 个）
- Test: `tests/unit/scripts/test_migrate_skills.py`（rewrite 规则、source 注入、拒绝覆盖人工复核结果）；生成内容的常驻校验在 Task 4。

**Interfaces:**
- Consumes: `--source <path>` 或 `AA_TS_SOURCE` 指定的参考仓库 `{skills,.opencode/agents}`；不得把某个开发者的绝对路径写进可执行合同。
- Produces: `_resources/skills/`（33 个 skill 目录，含支撑文件）与 `_resources/opencode/agents/`（6 个 aa-* agent）。Task 4 校验、Task 5 同步、Task 7 打包冒烟消费。脚本函数 `rewrite_text(text) -> str`、`target_name(name) -> str`、`migrate() -> int`。

- [ ] **Step 1: 先写迁移脚本单测并确认失败**

```python
# tests/unit/scripts/test_migrate_skills.py
from pathlib import Path

import pytest

from scripts.migrate_skills import migrate, rewrite_text, target_name


def test_rewrite_text_rewrites_cli_config_and_skill_names() -> None:
    source = "aws run; .aws/config; AWS_HOME; aws-workflow; `aws`"
    assert rewrite_text(source) == "aa run; .aa/config; AA_HOME; aa-workflow; `aa`"


def test_target_name_preserves_non_prefixed_skill() -> None:
    assert target_name("aws-workflow") == "aa-workflow"
    assert target_name("writing-skills") == "writing-skills"


def test_migrate_refuses_to_overwrite_reviewed_destination(tmp_path: Path) -> None:
    destination = tmp_path / "resources"
    reviewed = destination / "skills/aa-workflow/SKILL.md"
    reviewed.parent.mkdir(parents=True)
    reviewed.write_text("reviewed", encoding="utf-8")
    with pytest.raises(RuntimeError, match="--force"):
        migrate(tmp_path / "source", destination)
```

Run: `uv run pytest tests/unit/scripts/test_migrate_skills.py -v`
Expected: FAIL（脚本尚不存在）

- [ ] **Step 2: 写迁移脚本**

```python
#!/usr/bin/env python3
# scripts/migrate_skills.py
"""One-off dev tool: migrate aws-* skills and OpenCode agents from the TS repo
into assurance_agent/_resources/ with deterministic aa-* rewrites (spec 8/9).

NOT shipped in the wheel (lives in scripts/, excluded by pyproject). Run once to
seed _resources/; Task 2 (dashboard server.py) and the manual review pass are
applied on top and are the source of truth afterward. The default mode refuses
to overwrite a non-empty skills destination; `--force` is an explicit destructive
regeneration and requires the manual review pass to be repeated.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

DEFAULT_SRC_ROOT = Path(os.environ.get("AA_TS_SOURCE", "../assurance-workflow-skills"))
DST_ROOT = Path(__file__).resolve().parent.parent / "assurance_agent" / "_resources"

# Files whose content we rewrite as text (everything else is copied verbatim).
TEXT_SUFFIXES = {".md", ".sh", ".dot", ".js", ".cjs", ".html", ".txt", ".yaml", ".yml", ".json"}
# Never copy these (junk or replaced-by-hand).
DROP_NAMES = {".DS_Store"}
DROP_RELPATHS = {"aws-dashboard/scripts/server.cjs"}  # replaced by server.py in Task 2


def rewrite_text(text: str) -> str:
    text = text.replace("Assurance Workflow Skills", "Assurance Agent")        # R1
    text = text.replace("AWS-HEALING-SKILLS-UNAVAILABLE", "AA-HEALING-SKILLS-UNAVAILABLE")  # R2
    text = re.sub(r"\baws-", "aa-", text)                                       # R3
    text = re.sub(r"\.aws/", ".aa/", text)                                     # R4
    text = re.sub(r"\.aws\b", ".aa", text)                                     # R5
    text = re.sub(r"\bAWS_", "AA_", text)                                       # R6
    text = re.sub(r"(?<![\w-])aws (?=[a-z])", "aa ", text)                     # R7
    text = text.replace("`aws`", "`aa`")                                        # R7b
    text = re.sub(r"\baws\b", "aa", text)                                       # R7c
    text = re.sub(r"\bAWS\b", "AA", text)                                       # R8
    text = text.replace("npm run build && npm link", "uv sync")                 # R9
    text = text.replace("npm install -g assurance-agent", "uv tool install assurance-agent")  # R10
    return text


def target_name(name: str) -> str:
    return "aa-" + name[len("aws-"):] if name.startswith("aws-") else name


def _migrate_tree(src_dir: Path, dst_dir: Path, drop_prefix: str) -> int:
    """Copy+rewrite one source tree into dst_dir; returns number of top-level entries."""
    count = 0
    for entry in sorted(src_dir.iterdir()):
        if not entry.is_dir():
            continue
        out_dir = dst_dir / target_name(entry.name)
        for src_file in sorted(entry.rglob("*")):
            if src_file.is_dir():
                continue
            if src_file.name in DROP_NAMES:
                continue
            rel_from_src_root = src_file.relative_to(src_dir).as_posix()
            if f"{drop_prefix}{rel_from_src_root}" in DROP_RELPATHS or rel_from_src_root in DROP_RELPATHS:
                continue
            rel = Path(*[target_name(p) for p in src_file.relative_to(entry).parts])
            dst_file = out_dir / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            if src_file.suffix in TEXT_SUFFIXES:
                dst_file.write_text(rewrite_text(src_file.read_text(encoding="utf-8")), encoding="utf-8")
            else:
                shutil.copyfile(src_file, dst_file)
        count += 1
    return count


def _migrate_agents(src_dir: Path, dst_dir: Path) -> int:
    count = 0
    dst_dir.mkdir(parents=True, exist_ok=True)
    for src_file in sorted(src_dir.iterdir()):
        if src_file.suffix != ".md" or not src_file.name.startswith("aws-"):
            continue
        dst_file = dst_dir / target_name(src_file.name)
        dst_file.write_text(rewrite_text(src_file.read_text(encoding="utf-8")), encoding="utf-8")
        count += 1
    return count


def migrate(src_root: Path = DEFAULT_SRC_ROOT, dst_root: Path = DST_ROOT, *, force: bool = False) -> int:
    skills_dst = dst_root / "skills"
    if skills_dst.is_dir() and any(skills_dst.iterdir()) and not force:
        raise RuntimeError(f"destination already contains reviewed skills: {skills_dst}; pass --force to replace")
    if not src_root.is_dir():
        raise FileNotFoundError(f"reference source repo not found: {src_root}")
    skills = _migrate_tree(src_root / "skills", skills_dst, drop_prefix="")
    agents = _migrate_agents(src_root / ".opencode" / "agents", dst_root / "opencode" / "agents")
    print(f"migrated {skills} skills, {agents} agents into {dst_root}")
    return skills


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SRC_ROOT)
    parser.add_argument("--destination", type=Path, default=DST_ROOT)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        migrate(args.source, args.destination, force=args.force)
    except (FileNotFoundError, RuntimeError) as err:
        sys.exit(str(err))
```

- [ ] **Step 3: 跑单测，再运行脚本生成内容**

Run: `uv run pytest tests/unit/scripts/test_migrate_skills.py -v && uv run python scripts/migrate_skills.py --source "$AA_TS_SOURCE"`
Expected: `migrated 33 skills, 6 agents into .../assurance_agent/_resources`

- [ ] **Step 4: 校验 skill 数量与关键改名**

Run:
```bash
ls assurance_agent/_resources/skills | wc -l
ls assurance_agent/_resources/skills | grep -c '^aa-'
ls assurance_agent/_resources/opencode/agents
test -f assurance_agent/_resources/skills/writing-skills/SKILL.md && echo "writing-skills OK"
test ! -e assurance_agent/_resources/skills/aa-dashboard/scripts/server.cjs && echo "server.cjs dropped OK"
```
Expected: 第一行 `33`；第二行 `32`（32 个 `aa-*` + `writing-skills` 不计）；agents 列出 6 个 `aa-archiver.md`/`aa-doc-author.md`/`aa-intake-host.md`/`aa-reporter.md`/`aa-reviewer.md`/`aa-test-author.md`；后两条打印 OK。

- [ ] **Step 5: 校验机械改写的残留（应仅剩需人工/Task2/3 处理的项）**

Run:
```bash
if rg -n '(?i)(?<![a-z])aws' assurance_agent/_resources/skills assurance_agent/_resources/opencode/agents; then
  echo "ERROR: legacy aws residue found" >&2
  exit 1
else
  rc=$?
  test "$rc" -eq 1 && echo "NO RESIDUE"
fi
```
Expected: 打印 `NO RESIDUE`。若有残留：
- 属 `aa-dashboard/scripts/server.cjs`（已丢弃）——不应出现；
- 其余任何残留是规则表未覆盖的边角，回到 Step 1 补规则并重跑脚本，直到本步 `NO RESIDUE`。

- [ ] **Step 6: 人工复核 4 个 skill（按上文「人工复核清单」逐条改）**

对 `aa-workflow/SKILL.md`、`aa-workflow/FALLBACK-RUNBOOK.md`、`aa-test-infra-bootstrap/SKILL.md`、`aa-dashboard/SKILL.md`（server.py 相关留给 Task 2）、`writing-skills/SKILL.md` 逐个应用清单里的判断性改动。改完再跑一次 Step 5 的残留检查与 Task 4 常驻内容测试。脚本默认拒绝覆盖这些人工复核结果；只有明确 `--force` 才允许重新生成，之后必须重做本步。

- [ ] **Step 7: Commit**

```bash
git add scripts/migrate_skills.py tests/unit/scripts/test_migrate_skills.py \
        assurance_agent/_resources/skills assurance_agent/_resources/opencode/agents
git commit -m "feat: migrate 33 skills and 6 opencode agents to aa-* resources"
```

---

### Task 2: `aa-dashboard` Python 单文件静态服务器

**Files:**
- Create: `assurance_agent/_resources/skills/aa-dashboard/scripts/server.py`
- Modify: `assurance_agent/_resources/skills/aa-dashboard/scripts/start-server.sh`
- Modify: `assurance_agent/_resources/skills/aa-dashboard/scripts/stop-server.sh`
- Modify: `assurance_agent/_resources/skills/aa-dashboard/SKILL.md`
- Test: 无独立单测（内容随 Task 4 残留/引用校验覆盖）；本任务用内联 curl 冒烟。

**Interfaces:**
- Consumes: Task 1 产出的 `aa-dashboard` skill 目录（不含 server.cjs）。
- Produces: 纯 Python（stdlib `http.server`）静态服务器，路由与源 `server.cjs` 一一对应（`/cases`、`/yaml?path=`、`/api/cases`、`/api/changes`、`/api/changes/:id`），env 变量沿用 `QA_DASHBOARD_*`（非 aws，无需改名）。消灭 dashboard 的 Node 依赖（spec 8.3）。

- [ ] **Step 1: 写 server.py（完整移植 server.cjs 的路由与安全约束）**

```python
#!/usr/bin/env python3
# assurance_agent/_resources/skills/aa-dashboard/scripts/server.py
"""Single-file static Case Center server (stdlib http.server) — Python replacement
for the former Node server.cjs. Serves qa/cases and qa/changes YAML to the SPA.

Env (all optional):
  QA_DASHBOARD_PORT         bind port (default: random 49152-65534)
  QA_DASHBOARD_HOST         bind host (default 127.0.0.1)
  QA_DASHBOARD_URL_HOST     host shown in the printed URL (default localhost)
  QA_DASHBOARD_DIR          session dir (default /tmp/qa-dashboard)
  QA_DASHBOARD_PROJECT_DIR  project root containing qa/ (default cwd)
  QA_DASHBOARD_OWNER_PID    parent pid; server exits when it dies
"""
from __future__ import annotations

import json
import os
import random
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("QA_DASHBOARD_PORT") or (49152 + random.randint(0, 16382)))
HOST = os.environ.get("QA_DASHBOARD_HOST", "127.0.0.1")
URL_HOST = os.environ.get("QA_DASHBOARD_URL_HOST") or ("localhost" if HOST == "127.0.0.1" else HOST)
SESSION_DIR = Path(os.environ.get("QA_DASHBOARD_DIR", "/tmp/qa-dashboard"))
STATE_DIR = SESSION_DIR / "state"
PROJECT_DIR = Path(os.environ.get("QA_DASHBOARD_PROJECT_DIR", os.getcwd())).resolve()
OWNER_PID = int(os.environ["QA_DASHBOARD_OWNER_PID"]) if os.environ.get("QA_DASHBOARD_OWNER_PID") else None
CASE_CENTER_HTML = Path(__file__).resolve().parent / "case-center.html"
IDLE_TIMEOUT_S = 60 * 60
CHANGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_last_activity = time.time()


def _walk_case_yaml(root: Path) -> list[str]:
    out: list[str] = []
    if not root.exists():
        return out
    for path in sorted(root.rglob("case.yaml")):
        out.append(str(path.relative_to(PROJECT_DIR)))
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_: object) -> None:  # silence default stderr logging
        pass

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: object) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(data).encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        global _last_activity
        _last_activity = time.time()
        parsed = urlparse(self.path)
        pathname = parsed.path

        if pathname == "/cases":
            if not CASE_CENTER_HTML.is_file():
                self._send(500, "text/plain", b"case-center.html not found")
                return
            self._send(200, "text/html; charset=utf-8", CASE_CENTER_HTML.read_bytes())
            return

        if pathname == "/yaml":
            rel = (parse_qs(parsed.query).get("path") or [None])[0]
            if not rel:
                self._json(400, {"error": "Missing path parameter"})
                return
            abs_path = (PROJECT_DIR / rel).resolve()
            if abs_path != PROJECT_DIR and PROJECT_DIR not in abs_path.parents:
                self._json(403, {"error": "Path traversal not allowed"})
                return
            if not abs_path.is_file():
                self._json(404, {"error": "File not found"})
                return
            self._send(200, "text/plain; charset=utf-8", abs_path.read_bytes())
            return

        if pathname == "/api/cases":
            self._json(200, {"files": _walk_case_yaml(PROJECT_DIR / "qa" / "cases")})
            return

        if pathname == "/api/changes":
            changes_dir = PROJECT_DIR / "qa" / "changes"
            changes = (
                sorted(p.name for p in changes_dir.iterdir() if p.is_dir())
                if changes_dir.exists()
                else []
            )
            self._json(200, {"changes": changes})
            return

        match = re.match(r"^/api/changes/([^/]+)$", pathname)
        if match:
            change_id = match.group(1)
            if not CHANGE_ID_RE.match(change_id):
                self._json(400, {"error": "Invalid changeId"})
                return
            files = _walk_case_yaml(PROJECT_DIR / "qa" / "changes" / change_id / "cases")
            self._json(200, {"changeId": change_id, "files": files})
            return

        self._json(404, {"error": "Not found"})


def _owner_alive() -> bool:
    if OWNER_PID is None:
        return True
    try:
        os.kill(OWNER_PID, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def main() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    info = {
        "type": "server-started",
        "port": PORT,
        "host": HOST,
        "url_host": URL_HOST,
        "url": f"http://{URL_HOST}:{PORT}/cases",
        "project_dir": str(PROJECT_DIR),
        "session_dir": str(SESSION_DIR),
        "state_dir": str(STATE_DIR),
    }
    info_str = json.dumps(info)
    print(info_str, flush=True)
    (STATE_DIR / "server-info").write_text(info_str + "\n", encoding="utf-8")

    def _watchdog() -> None:
        while True:
            time.sleep(60)
            if not _owner_alive() or (time.time() - _last_activity) > IDLE_TIMEOUT_S:
                reason = "owner process exited" if not _owner_alive() else "idle timeout"
                print(json.dumps({"type": "server-stopped", "reason": reason}), flush=True)
                info_file = STATE_DIR / "server-info"
                if info_file.exists():
                    info_file.unlink()
                os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_watchdog, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 改 start-server.sh / stop-server.sh 调用 Python**

`start-server.sh`：把最后两处 `node server.cjs` 改为 `python3 server.py`（保留 env 前缀 `QA_DASHBOARD_*` 与前后台/健康检查逻辑不变）。用 StrReplace 分别替换前台与 `nohup` 后台两处：

前台分支（原 `env QA_DASHBOARD_... node server.cjs`）替换为：
```bash
  env QA_DASHBOARD_DIR="$SESSION_DIR" QA_DASHBOARD_HOST="$BIND_HOST" \
      QA_DASHBOARD_URL_HOST="$URL_HOST" QA_DASHBOARD_OWNER_PID="$OWNER_PID" \
      QA_DASHBOARD_PROJECT_DIR="$PROJECT_DIR" python3 server.py
```
后台分支（原 `nohup env ... node server.cjs > "$LOG_FILE" 2>&1 &`）替换为：
```bash
nohup env QA_DASHBOARD_DIR="$SESSION_DIR" QA_DASHBOARD_HOST="$BIND_HOST" \
    QA_DASHBOARD_URL_HOST="$URL_HOST" QA_DASHBOARD_OWNER_PID="$OWNER_PID" \
    QA_DASHBOARD_PROJECT_DIR="$PROJECT_DIR" python3 server.py > "$LOG_FILE" 2>&1 &
```
`stop-server.sh` 不引用 `node`（它按 pid 文件 kill），Task 1 机械改写后应已无 `aws` 残留，无需再改；若其中出现 `node` 字样一并删除。

- [ ] **Step 3: 改 SKILL.md 的运行时要求**

用 StrReplace 把 Requirements 段的 `- Node.js must be available` 改为 `- Python 3 must be available`。确认 "How to Start" 仍指向 `skills/aa-dashboard/scripts/start-server.sh`（Task 1 机械改写已把 `aws-dashboard` → `aa-dashboard`）。

- [ ] **Step 4: 冒烟：起服务并请求路由**

Run:
```bash
cd assurance_agent/_resources/skills/aa-dashboard/scripts
QA_DASHBOARD_PROJECT_DIR="$PWD" QA_DASHBOARD_PORT=48999 python3 server.py &
SRV=$!; sleep 1
curl -s http://127.0.0.1:48999/api/changes
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:48999/cases
kill $SRV
```
Expected: 第一条返回 `{"changes": []}`（该目录无 `qa/changes`）；第二条状态码 `200`（`case-center.html` 存在并返回）。

- [ ] **Step 5: Commit**

```bash
git add assurance_agent/_resources/skills/aa-dashboard
git commit -m "feat: replace aa-dashboard node server with stdlib python http server"
```

---

### Task 3: OpenCode 插件 `aa.mjs` + `workflow_start` tool + INSTALL.md

**Files:**
- Create: `assurance_agent/_resources/opencode/plugins/aa.mjs`
- Create: `assurance_agent/_resources/opencode/tools/workflow_start.ts`
- Create: `assurance_agent/_resources/opencode/INSTALL.md`
- Test: 无独立单测（内容随 Task 4 残留/引用/权限校验覆盖）；本任务用内联 `node --check`。

**Interfaces:**
- Consumes: Task 1 产出的 `_resources/opencode/agents/`；M6 `aa workflow start`。
- Produces: `aa.mjs`（注册 `<project>/skills` 目录、注入 bootstrap，调用目标改为 `aa` CLI；OMO 存在时不注册 `skills.paths`，保留宿主约束）；`workflow_start.ts`（只收集参数并调用 `aa workflow start`，循环逻辑不驻留 JS，spec 5a/9）；`INSTALL.md`（uv 版安装说明）。Task 5 `sync_opencode`、Task 6 `aa init` 注册消费。

- [ ] **Step 1: 写 aa.mjs（改写自源 aws.mjs，AWS→AA、`aws …`→`aa …`、`aws/<skill>`→`aa/<skill>`）**

```javascript
// assurance_agent/_resources/opencode/plugins/aa.mjs
/**
 * AA (Assurance Agent) plugin for OpenCode.ai
 *
 * Registers the AA skills directory so OpenCode discovers all QA workflow
 * skills without symlinks or manual config. Skills live at <project>/skills/
 * (laid down by `aa skill refresh` / `aa init`), two levels up from
 * .opencode/plugins/.
 */
import path from 'path';
import fs from 'fs';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Skills live two levels up from .opencode/plugins/
const AA_SKILLS_DIR = path.resolve(__dirname, '../../skills');

// Simple frontmatter parser (no external dependencies)
const extractAndStripFrontmatter = (content) => {
  const match = content.match(/^---\n([\s\S]*?)\n---\n([\s\S]*)$/);
  if (!match) return { frontmatter: {}, content };

  const frontmatterStr = match[1];
  const body = match[2];
  const frontmatter = {};

  for (const line of frontmatterStr.split('\n')) {
    const colonIdx = line.indexOf(':');
    if (colonIdx > 0) {
      const key = line.slice(0, colonIdx).trim();
      const value = line.slice(colonIdx + 1).trim().replace(/^["']|["']$/g, '');
      frontmatter[key] = value;
    }
  }

  return { frontmatter, content: body };
};

// Cached bootstrap content (loaded once per session)
let _bootstrapCache = undefined;

const getBootstrapContent = () => {
  if (_bootstrapCache !== undefined) return _bootstrapCache;

  const skillsDir = AA_SKILLS_DIR;
  if (!fs.existsSync(skillsDir)) {
    _bootstrapCache = null;
    return null;
  }

  const skills = [];
  for (const entry of fs.readdirSync(skillsDir)) {
    const skillMdPath = path.join(skillsDir, entry, 'SKILL.md');
    if (!fs.existsSync(skillMdPath)) continue;
    try {
      const raw = fs.readFileSync(skillMdPath, 'utf8');
      const { frontmatter } = extractAndStripFrontmatter(raw);
      if (frontmatter.name && frontmatter.description) {
        skills.push(`- **${frontmatter.name}**: ${frontmatter.description}`);
      }
    } catch {
      // skip unreadable files
    }
  }

  if (skills.length === 0) {
    _bootstrapCache = null;
    return null;
  }

  _bootstrapCache = `
You have AA (Assurance Agent) QA workflow skills available.

Use OpenCode's native \`skill\` tool to load a skill by its actual frontmatter name:
  skill load <skill-name>
For example: \`skill load aa-workflow\` (there is no extra \`aa/\` namespace).

**Available AA Skills:**
${skills.join('\n')}

**Tool Mapping for OpenCode:**
- \`Bash\` / \`Shell\` → Your native bash tool
- \`Read\` / \`Write\` → Your native file tools
- \`TodoWrite\` → \`todowrite\`
- \`Task\` with subagents → OpenCode's subagent system

**Key CLI commands (must be run in terminal, never fabricated):**
- \`aa status --change <change-id> --json\` — compute deterministic workflow phase status
- \`aa gate check --change <change-id> --phase <phase-id> --json\` — adjudicate one phase gate deterministically
- \`aa run --change <change-id>\` — execute tests (skill: aa-run)
- \`aa report inspect --change <change-id>\` — classify failures (skill: aa-inspect)
`;

  return _bootstrapCache;
};

export default async ({ client, directory }) => {
  return {
    // Register AA skills directory for native OpenCode discovery.
    // Skip when oh-my-openagent (omo) is installed: omo replaces the native skill
    // tool and only scans ~/.config/opencode/skills/. Registering skills.paths as
    // well would duplicate every aa-* skill in the palette.
    config: async (config) => {
      const plugins = Array.isArray(config.plugin) ? config.plugin : [];
      const usesOmo = plugins.some((p) => /oh-my-openagent|oh-my-opencode/i.test(String(p)));
      if (usesOmo) return;

      config.skills = config.skills || {};
      config.skills.paths = config.skills.paths || [];
      if (!config.skills.paths.includes(AA_SKILLS_DIR)) {
        config.skills.paths.push(AA_SKILLS_DIR);
      }
    },

    // Inject brief bootstrap context into the first user message of each session
    'experimental.chat.messages.transform': async (_input, output) => {
      const bootstrap = getBootstrapContent();
      if (!bootstrap || !output.messages.length) return;

      const firstUser = output.messages.find(m => m.info.role === 'user');
      if (!firstUser || !firstUser.parts.length) return;

      // Guard: skip if already injected
      if (firstUser.parts.some(p => p.type === 'text' && p.text.includes('AA (Assurance Agent)'))) return;

      const ref = firstUser.parts[0];
      firstUser.parts.unshift({ ...ref, type: 'text', text: bootstrap });
    },
  };
};
```

- [ ] **Step 2: 写 workflow_start.ts（改用 `aa workflow start`，spec 5a/M6 契约）**

```typescript
// assurance_agent/_resources/opencode/tools/workflow_start.ts
/**
 * OpenCode custom tool: start the AA workflow driver detached from chat.
 * Synced into a project's .opencode/tools/ by `aa init` / `aa skill refresh --sync-agents`.
 *
 * This tool ONLY collects params and delegates to `aa workflow start` (M6 detached
 * entry). The dispatch loop lives in the Python driver, never in JS.
 *
 * Fail-closed server URL: requires AA_OPENCODE_SERVER_URL (or OPENCODE_SERVER_URL),
 * or explicit --hostname/--port on the opencode process argv (no implicit port).
 */
import { spawnSync } from 'child_process';
import { z } from 'zod';

function resolveServerUrl(): string {
  const url = process.env.AA_OPENCODE_SERVER_URL || process.env.OPENCODE_SERVER_URL;
  if (url && url.trim()) return url.trim().replace(/\/$/, '');
  const argv = process.argv;
  let hostname: string | null = null;
  let port: string | null = null;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if ((a === '--hostname' || a === '--host') && argv[i + 1]) hostname = argv[i + 1];
    if (a === '--port' && argv[i + 1]) port = argv[i + 1];
    const hm = /^--hostname=(.+)$/.exec(a) || /^--host=(.+)$/.exec(a);
    if (hm) hostname = hm[1];
    const pm = /^--port=(.+)$/.exec(a);
    if (pm) port = pm[1];
  }
  if (hostname && port) {
    const host = hostname === '0.0.0.0' ? '127.0.0.1' : hostname;
    return `http://${host}:${port}`;
  }
  throw new Error(
    'Cannot resolve OpenCode server URL. Set AA_OPENCODE_SERVER_URL ' +
    '(or OPENCODE_SERVER_URL), or start opencode with explicit --hostname/--port.',
  );
}

export default {
  description:
    'Start or continue the AA workflow driver for a change in the background. ' +
    'Call after intake user confirmation, or after a human decision. ' +
    'Progress appears in nested sessions and the QA panel.',
  args: {
    change_id: z.string().regex(
      /^[A-Za-z0-9][A-Za-z0-9._-]*$/,
      'change_id must be one safe path segment',
    ).describe('Change ID (e.g. REQ-001-login)'),
    scope: z.enum(['execute', 'full']).default('execute').describe('Workflow scope'),
    params_json: z.string().optional().describe('Optional runtime params as JSON object string'),
  },
  async execute(
    args: { change_id: string; scope?: 'execute' | 'full'; params_json?: string },
    context: { sessionID?: string; sessionId?: string; directory?: string; worktree?: string },
  ) {
    const scope = args.scope ?? 'execute';
    const directory = context.directory ?? context.worktree ?? process.cwd();
    const parent = context.sessionID ?? context.sessionId;

    let serverUrl: string;
    try {
      serverUrl = resolveServerUrl();
    } catch (err) {
      return `workflow_start failed: ${(err as Error).message}`;
    }

    const cliArgs = [
      'workflow', 'start',
      '--change', args.change_id,
      '--scope', scope,
      '--server', serverUrl,
      '--directory', directory,
    ];
    if (parent) cliArgs.push('--parent-session', parent);
    if (args.params_json) cliArgs.push('--params', args.params_json);

    const result = spawnSync('aa', cliArgs, {
      cwd: directory,
      encoding: 'utf-8',
      env: process.env,
    });
    const out = `${result.stdout ?? ''}${result.stderr ?? ''}`.trim();
    if (result.error) {
      return `workflow_start failed: ${result.error.message}${out ? `\n${out}` : ''}`;
    }
    if (result.status !== 0) {
      return `workflow_start failed (exit ${result.status}): ${out || 'no output'}`;
    }
    return out || `已启动 workflow（change=${args.change_id}, scope=${scope}）`;
  },
};
```

- [ ] **Step 3: 写 INSTALL.md（uv 版；去掉 Amazon aws-cli 命名冲突章节，因 CLI 名为 `aa`）**

```markdown
# Installing AA for OpenCode

> **Naming note:**
> - **AA** = **Assurance Agent**. This is the project name and CLI prefix.
> - `aa-*` is used for all skills and OpenCode agents in this project.
> - `aa` is the project CLI — for example `aa run` and `aa report inspect`.

---

## Prerequisites

- [OpenCode.ai](https://opencode.ai) installed
- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- Git available in your terminal

---

## Installation

### Option 1: via `aa init` (recommended)

Run in your project directory:

```bash
aa init
```

`aa init` writes `opencode.json` with the plugin entry and copies the AA
skills, agents, tools and plugin into the project. Then restart OpenCode and
run `skill load aa-workflow` to start the workflow.

### Option 2: manual

Add AA to the `plugin` array in your project `opencode.json`, and copy the
package assets in with `aa skill refresh --sync-agents`:

```json
{
  "plugin": ["./.opencode/plugins/aa.mjs"]
}
```

Restart OpenCode after editing `opencode.json`. The plugin registers all AA QA
workflow skills automatically.

---

## Usage

The main entry skill is `aa-workflow`:

```
skill load aa-workflow
```

Key CLI commands (run in terminal, never fabricated):

```bash
aa run --change <change-id>
aa report inspect --change <change-id>
```

## Updating

If skill updates do not appear after restart, re-sync from the package and
restart OpenCode:

```bash
aa skill refresh --sync-agents
```

`aa skill refresh` copies the packaged skills into `<project>/skills/` and, with
`--sync-agents`, the agents/tools/plugin into `<project>/.opencode/`.
```

- [ ] **Step 4: 语法自检**

Run: `node --check assurance_agent/_resources/opencode/plugins/aa.mjs && echo "aa.mjs OK"`
Expected: `aa.mjs OK`（如本机无 node，跳过并在 Task 4 依赖内容测试；`workflow_start.ts` 为 TS，不做 node 语法检查）。

- [ ] **Step 5: Commit**

```bash
git add assurance_agent/_resources/opencode/plugins assurance_agent/_resources/opencode/tools assurance_agent/_resources/opencode/INSTALL.md
git commit -m "feat: add aa opencode plugin, workflow_start tool and uv install doc"
```

---

### Task 4: 内容校验测试（残留 / 跨引用 / 字段交叉核对）

**Files:**
- Create: `tests/unit/test_skills_content.py`
- Create: `tests/data/skill_field_allowlist.txt`
- Test: 本任务即测试；它对 Task 1–3 的 `_resources/{skills,opencode}` 内容常驻校验。

**Interfaces:**
- Consumes: `assurance_agent.resources`（`iter_children` / `read_text`）读包内内容；M2 `assurance_agent.artifacts.models` 的模型字段。
- Produces: 三类常驻校验——(a) `aws` 残留=0（词边界正则）；(b) 跨 skill/agent 引用链接存在；(c) 字段交叉核对（SKILL.md 中 `root.field` 点式引用 ⊂ M2 模型字段，spec 4a）；外加 (d) agent 权限地板保留检查。允许清单 `tests/data/skill_field_allowlist.txt`。

**校验设计（写进测试注释）：**
- **残留正则** `(?i)(?<![a-z])aws`：仅匹配非字母打头的 `aws`（含 `AWS`、`.aws`、`aws-`、`aws `），天然排除 `laws`/`draws`/`flaws` 等普通英文词（其 `aws` 前均为字母）。经巡检源内容无以 `aws` 开头的普通英文词，故残留应为 0，`AWS_RESIDUE_ALLOWLIST` 为空集（保留为将来的逃生口）。
- **跨引用**：从每个 SKILL.md / agent .md 抽取 `\b(aa-[a-z0-9-]+|writing-skills)\b` token（去掉尾随 `.md`/`.`），断言 ∈ {skill 目录名} ∪ {agent 名}；非 skill/agent 的 `aa-*` token（如 schema 名 `aa-full`）进 `AA_REF_ALLOWLIST`。
- **字段交叉核对**：对每个 SKILL.md，用 `(?<![\w.])<root>\.([a-z_][a-z0-9_]*)` 抓取点式引用的**首段字段**（`(?<![\w.])` 天然排除 `phases.fact_baseline.status` 这类 workflow-state 状态路径与 `review.json` 这类文件名——首段落在 `FILE_SUFFIXES` 的直接跳过）；断言首段 ∈ 对应模型的顶层字段名/别名集合，否则须在允许清单。深层 `extra="allow"` 子模型字段不在本核对范围（首段挂载点即足以捕获 must_compat 根字段改名/拼写漂移）。

- [ ] **Step 1: 写允许清单（起始为空 + 注释）**

```text
# tests/data/skill_field_allowlist.txt
# Field cross-check allowlist (spec 4a). One "root.field" per line; blank lines
# and lines starting with '#' are ignored. Legit references to fields NOT present
# in the M2 artifact model land here. Current migrated content needs no entries.
```

- [ ] **Step 2: 写测试**

```python
# tests/unit/test_skills_content.py
"""Resident content checks for migrated skills + opencode assets (spec 8/9/4a)."""
import re
from pathlib import Path

from assurance_agent import resources
from assurance_agent.artifacts.models import (
    Advisory,
    ApplySummary,
    CaseYaml,
    ExecutionManifest,
    FactBaselineFull,
    FactBaselineUnavailable,
    FailureAnalysis,
    FixProposal,
    QaYaml,
    QualityGateResult,
    QualityReport,
    Review,
    SafetyCheck,
    WorkflowState,
)
from pydantic import BaseModel

RESIDUE_RE = re.compile(r"(?i)(?<![a-z])aws")
AWS_RESIDUE_ALLOWLIST: set[str] = set()
AA_REF_RE = re.compile(r"\b(aa-[a-z0-9-]+|writing-skills)\b")
AA_REF_ALLOWLIST = {"aa-full"}  # workflow schema name, not a skill/agent
FILE_SUFFIXES = {"json", "ts", "yaml", "yml", "md", "schema", "py"}
ALLOWLIST_FILE = Path(__file__).resolve().parents[1] / "data" / "skill_field_allowlist.txt"


def _walk_resource_files(*rel: str):
    """Yield (relpath_tuple) for every file under _resources/<rel> (recursive)."""
    for name in resources.iter_children(*rel):
        child = (*rel, name)
        try:
            resources.iter_children(*child)  # dir -> recurse
        except (NotADirectoryError, ValueError):
            yield child
        else:
            yield from _walk_resource_files(*child)


def _all_text_files(*rel: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for parts in _walk_resource_files(*rel):
        name = parts[-1]
        if name.endswith((".md", ".sh", ".mjs", ".ts", ".html", ".txt", ".yaml", ".yml", ".json", ".dot")):
            out["/".join(parts)] = resources.read_text(*parts)
    return out


def _skill_names() -> set[str]:
    return set(resources.iter_children("skills"))


def _agent_names() -> set[str]:
    return {n[:-3] for n in resources.iter_children("opencode", "agents") if n.endswith(".md")}


def _model_fields(*models: type[BaseModel]) -> set[str]:
    out: set[str] = set()
    for model in models:
        for field_name, field in model.model_fields.items():
            out.add(field_name)
            if field.alias:
                out.add(field.alias)
    return out


ARTIFACT_ROOTS: dict[str, set[str]] = {
    "review": _model_fields(Review),
    "fix_proposal": _model_fields(FixProposal),
    "failure_analysis": _model_fields(FailureAnalysis),
    "execution_manifest": _model_fields(ExecutionManifest),
    "quality_gate_result": _model_fields(QualityGateResult),
    "quality_report": _model_fields(QualityReport),
    "advisory": _model_fields(Advisory),
    "fact_baseline": _model_fields(FactBaselineFull, FactBaselineUnavailable),
    "workflow_state": _model_fields(WorkflowState),
    "safety_check": _model_fields(SafetyCheck),
    "apply_summary": _model_fields(ApplySummary),
    "case_yaml": _model_fields(CaseYaml),
    "qa_yaml": _model_fields(QaYaml),
}


def _load_field_allowlist() -> set[str]:
    entries: set[str] = set()
    for line in ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.add(line)
    return entries


def test_thirty_three_skills_present() -> None:
    names = _skill_names()
    assert len(names) == 33
    assert "writing-skills" in names
    assert "aa-workflow" in names
    assert "aa-dashboard" in names
    assert not any(n.startswith("aws-") for n in names)


def test_no_aws_residue_in_skills_and_opencode() -> None:
    offenders: list[str] = []
    files = {**_all_text_files("skills"), **_all_text_files("opencode")}
    for relpath, text in files.items():
        for match in RESIDUE_RE.finditer(text):
            token = text[match.start(): match.start() + 3]
            if token not in AWS_RESIDUE_ALLOWLIST:
                line = text[:match.start()].count("\n") + 1
                offenders.append(f"{relpath}:{line}:{token}")
    assert offenders == [], f"aws residue found: {offenders[:20]}"


def test_cross_skill_references_resolve() -> None:
    valid = _skill_names() | _agent_names() | AA_REF_ALLOWLIST
    offenders: list[str] = []
    files = {**_all_text_files("skills"), **_all_text_files("opencode", "agents")}
    for relpath, text in files.items():
        for match in AA_REF_RE.finditer(text):
            token = match.group(1).rstrip(".")
            if token.endswith(".md"):
                token = token[:-3]
            if token not in valid:
                offenders.append(f"{relpath}: {token}")
    assert offenders == [], f"dangling aa-* references: {sorted(set(offenders))[:20]}"


def test_skill_field_references_subset_of_models() -> None:
    allowlist = _load_field_allowlist()
    offenders: list[str] = []
    for relpath, text in _all_text_files("skills").items():
        for root, fields in ARTIFACT_ROOTS.items():
            for match in re.finditer(rf"(?<![\w.]){root}\.([a-z_][a-z0-9_]*)", text):
                first = match.group(1)
                if first in FILE_SUFFIXES:
                    continue
                if first in fields:
                    continue
                if f"{root}.{first}" in allowlist:
                    continue
                offenders.append(f"{relpath}: {root}.{first}")
    assert offenders == [], f"skill fields not in M2 models: {sorted(set(offenders))[:20]}"


def test_agents_preserve_permission_floor() -> None:
    """No runtime agent grants an allow-rule for aa gate/status or workflow-state writes (spec 9).

    Agents legitimately MENTION `aa gate check` / workflow-state.yaml in prose (the
    prohibition text), so we assert on permission *allow-rules* only, not substrings.
    """
    allow_gate = re.compile(r'"[^"]*aa (gate|status)[^"]*"\s*:\s*allow')
    allow_state = re.compile(r'"[^"]*workflow-state\.yaml"\s*:\s*allow')
    agents = [n for n in resources.iter_children("opencode", "agents") if n.endswith(".md")]
    assert len(agents) == 6
    for name in agents:
        text = resources.read_text("opencode", "agents", name)
        assert allow_gate.search(text) is None, name
        assert allow_state.search(text) is None, name
```

- [ ] **Step 3: 跑测试**

Run: `uv run pytest tests/unit/test_skills_content.py -v`
Expected: 5 passed。若失败：
- 残留失败 → 回 Task 1 Step 4/5 修 `_resources` 内容或补迁移规则；
- 跨引用失败 → 打印的 dangling token 多半是拼写漂移或指向被改名的 skill，回相应 SKILL.md 修正；
- 字段核对失败 → 若确属合法引用，把 `root.field` 加入 `tests/data/skill_field_allowlist.txt` 并注明理由；否则修 SKILL.md 让其对齐 M2 模型字段；
- agent 权限失败 → 修 `_resources/opencode/agents/*.md` 保留权限地板。

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_skills_content.py tests/data/skill_field_allowlist.txt
git commit -m "test: add resident skill content checks (aws residue, cross-refs, field cross-check)"
```

---

### Task 5: `aa skill refresh` + 同步逻辑 + 仓库自身 `.opencode/` 派生

**Files:**
- Create: `assurance_agent/workflow/core/assets.py`
- Create: `assurance_agent/commands/skill_cmd.py`
- Modify: `assurance_agent/cli.py`
- Modify: `.gitignore`
- Test: `tests/unit/test_assets.py`、`tests/integration/test_cli_skill_refresh.py`

**Interfaces:**
- Consumes: `assurance_agent.resources`（`iter_children`/`read_text`/`exists`）；M1 `AaError`、`CONFIG_RELPATH`。
- Produces:
  - `assurance_agent/workflow/core/assets.py`：
    - `class SyncResult(BaseModel)`：`created: list[str]`、`updated: list[str]`、`unchanged: list[str]`。
    - `find_project_root(start: Path) -> Path | None`——自 `start` 向上找含 `.aa/config.yaml` 或 `qa/` 的目录；找不到返回 `None`。
    - `sync_skills(project_root: Path, dry_run: bool = False) -> SyncResult`——把 `_resources/skills/**` 同步到 `<root>/skills/`。
    - `sync_opencode(project_root: Path, dry_run: bool = False) -> SyncResult`——把 `_resources/opencode/{agents,tools,plugins}/**` 同步到 `<root>/.opencode/`。
    - `PLUGIN_ENTRY = "./.opencode/plugins/aa.mjs"`（Task 6 复用）。
  - `aa skill refresh [--sync-agents] [--dry-run]` 子命令（`skill` group 下）：始终 `sync_skills`；`--sync-agents` 追加 `sync_opencode`；报告 created/updated/unchanged 计数；幂等；无项目根时退出码 1。

> 说明（对齐 M6 契约「`--build-link` 删除」）：TS `skill refresh` 的 `--build-link`（npm 构建/链接）在 Python 净室中**移除**；`--dry-run`、`--sync-agents` 保留；TS 的 OMO 符号链接/缓存清理/`skills.paths` 去重逻辑不迁移（净室改为「从包资源拷贝到目标项目」的确定性同步）。

- [ ] **Step 1: 写 assets.py 单测**

```python
# tests/unit/test_assets.py
from pathlib import Path

from assurance_agent.workflow.core.assets import (
    find_project_root,
    sync_opencode,
    sync_skills,
)


def _make_project(tmp_path: Path) -> Path:
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa/config.yaml").write_text("version: 1\n", encoding="utf-8")
    return tmp_path


def test_find_project_root_by_config(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    assert find_project_root(nested) == root


def test_find_project_root_by_qa_dir(tmp_path: Path) -> None:
    (tmp_path / "qa").mkdir()
    assert find_project_root(tmp_path) == tmp_path


def test_find_project_root_none(tmp_path: Path) -> None:
    assert find_project_root(tmp_path) is None


def test_sync_skills_creates_then_idempotent(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    first = sync_skills(root)
    assert (root / "skills" / "aa-workflow" / "SKILL.md").is_file()
    assert (root / "skills" / "writing-skills" / "SKILL.md").is_file()
    assert len(first.created) > 30
    assert first.updated == []

    second = sync_skills(root)
    assert second.created == []
    assert second.updated == []
    assert len(second.unchanged) == len(first.created)


def test_sync_skills_detects_update(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    sync_skills(root)
    target = root / "skills" / "aa-workflow" / "SKILL.md"
    target.write_text("stale", encoding="utf-8")
    result = sync_skills(root)
    assert "skills/aa-workflow/SKILL.md" in result.updated


def test_sync_skills_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    result = sync_skills(root, dry_run=True)
    assert len(result.created) > 30
    assert not (root / "skills").exists()


def test_sync_opencode_lays_down_agents_tools_plugin(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    result = sync_opencode(root)
    assert (root / ".opencode/agents/aa-doc-author.md").is_file()
    assert (root / ".opencode/tools/workflow_start.ts").is_file()
    assert (root / ".opencode/plugins/aa.mjs").is_file()
    assert any(p.startswith(".opencode/agents/") for p in result.created)
    assert sync_opencode(root).created == []
```

- [ ] **Step 2: 跑单测确认失败**

Run: `uv run pytest tests/unit/test_assets.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'assurance_agent.workflow.core.assets'`

- [ ] **Step 3: 实现 assets.py**

```python
# assurance_agent/workflow/core/assets.py
"""Sync packaged skills / OpenCode assets into a target project.

Reads packaged resources only via assurance_agent.resources (no __file__ paths),
writes into <project>/skills/ and <project>/.opencode/. Content-hash based:
reports created / updated / unchanged so callers can print idempotent summaries.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from assurance_agent import resources
from assurance_agent.config import CONFIG_RELPATH

PLUGIN_ENTRY = "./.opencode/plugins/aa.mjs"


class SyncResult(BaseModel):
    created: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)


def find_project_root(start: Path) -> Path | None:
    cur = start.resolve()
    while True:
        if (cur / CONFIG_RELPATH).is_file() or (cur / "qa").is_dir():
            return cur
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent


def _walk_resource_files(*rel: str) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []
    for name in resources.iter_children(*rel):
        child = (*rel, name)
        try:
            resources.iter_children(*child)
        except (NotADirectoryError, ValueError):
            out.append(child)
        else:
            out.extend(_walk_resource_files(*child))
    return out


def _sync(
    resource_rel: tuple[str, ...],
    dest_root: Path,
    report_prefix: str,
    result: SyncResult,
    dry_run: bool,
) -> None:
    for parts in _walk_resource_files(*resource_rel):
        rel_under = Path(*parts[len(resource_rel):])
        report = (Path(report_prefix) / rel_under).as_posix()
        content = resources.read_text(*parts)
        dest = dest_root / rel_under
        if dest.is_file() and dest.read_text(encoding="utf-8") == content:
            result.unchanged.append(report)
            continue
        status_updated = dest.is_file()
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        (result.updated if status_updated else result.created).append(report)


def sync_skills(project_root: Path, dry_run: bool = False) -> SyncResult:
    result = SyncResult()
    _sync(("skills",), project_root / "skills", "skills", result, dry_run)
    return result


def sync_opencode(project_root: Path, dry_run: bool = False) -> SyncResult:
    result = SyncResult()
    for sub in ("agents", "tools", "plugins"):
        if resources.exists("opencode", sub):
            _sync(("opencode", sub), project_root / ".opencode" / sub, f".opencode/{sub}", result, dry_run)
    return result
```

> 报告路径以目标项目根为基准（如 `skills/…`、`.opencode/agents/…`），CLI 分组输出与测试断言都以此为准。

- [ ] **Step 4: 跑单测确认通过**

Run: `uv run pytest tests/unit/test_assets.py -v`
Expected: 7 passed

- [ ] **Step 5: 写命令壳 skill_cmd.py 并挂载**

```python
# assurance_agent/commands/skill_cmd.py
from pathlib import Path

import click

from assurance_agent.workflow.core.assets import (
    SyncResult,
    find_project_root,
    sync_opencode,
    sync_skills,
)


def _print(kind: str, result: SyncResult) -> None:
    click.echo(
        f"{kind}: {len(result.created)} created, "
        f"{len(result.updated)} updated, {len(result.unchanged)} unchanged"
    )
    for rel in result.created:
        click.secho(f"  created: {rel}", fg="green")
    for rel in result.updated:
        click.secho(f"  updated: {rel}", fg="yellow")


@click.group("skill")
def skill_group() -> None:
    """Skill maintenance commands."""


@skill_group.command("refresh")
@click.option("--sync-agents", is_flag=True, help="Also sync .opencode/ agents, tools and plugin.")
@click.option("--dry-run", is_flag=True, help="Show what would change without writing.")
def refresh_command(sync_agents: bool, dry_run: bool) -> None:
    """Sync packaged skills (always) and OpenCode assets (--sync-agents) into this project."""
    root = find_project_root(Path.cwd())
    if root is None:
        click.secho("No assurance-agent project found near cwd (.aa/config.yaml or qa/).", fg="red")
        raise SystemExit(1)

    click.secho(f"aa skill refresh{' (DRY RUN)' if dry_run else ''} — {root}", bold=True)
    _print("skills", sync_skills(root, dry_run=dry_run))
    if sync_agents:
        _print("opencode", sync_opencode(root, dry_run=dry_run))
    if not dry_run:
        click.secho("Refresh complete. Restart OpenCode to pick up changes.", fg="green", bold=True)
```

```python
# assurance_agent/cli.py（追加 import 与挂载，保持既有内容不变）
from assurance_agent.commands.skill_cmd import skill_group

main.add_command(skill_group)
```

- [ ] **Step 6: 写集成测试**

```python
# tests/integration/test_cli_skill_refresh.py
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main


def _init(runner: CliRunner) -> None:
    assert runner.invoke(main, ["init", "--yes"]).exit_code == 0


def test_refresh_without_project_exits_1(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["skill", "refresh"])
        assert result.exit_code == 1
        assert "No assurance-agent project" in result.output


def test_refresh_creates_skills_then_idempotent(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        _init(runner)
        first = runner.invoke(main, ["skill", "refresh"])
        assert first.exit_code == 0, first.output
        assert Path("skills/aa-workflow/SKILL.md").is_file()
        assert len(list(Path("skills").iterdir())) == 33

        second = runner.invoke(main, ["skill", "refresh"])
        assert second.exit_code == 0
        assert "0 created, 0 updated" in second.output


def test_refresh_sync_agents_lays_down_opencode(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        _init(runner)
        result = runner.invoke(main, ["skill", "refresh", "--sync-agents"])
        assert result.exit_code == 0, result.output
        assert Path(".opencode/agents/aa-doc-author.md").is_file()
        assert Path(".opencode/tools/workflow_start.ts").is_file()
        assert Path(".opencode/plugins/aa.mjs").is_file()


def test_refresh_dry_run_writes_nothing(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        _init(runner)
        result = runner.invoke(main, ["skill", "refresh", "--dry-run"])
        assert result.exit_code == 0
        assert not Path("skills").exists()
```

- [ ] **Step 7: 跑集成测试确认通过**

Run: `uv run pytest tests/integration/test_cli_skill_refresh.py -v`
Expected: 4 passed

- [ ] **Step 8: 忽略仓库派生目录 + 记录仓库自身派生方式**

把仓库根的 `skills/`、`.opencode/` 标为生成物（`_resources/` 是唯一源，spec 15）。用 StrReplace 在 `.gitignore` 追加：

```
/skills/
/.opencode/
```

仓库自身要 dogfood OpenCode 时，在仓库根运行 `uv run aa skill refresh --sync-agents` 即从 `_resources/` 派生出与目标项目同构的 `skills/` 与 `.opencode/`（不入 git）。

- [ ] **Step 9: 质量门禁 + Commit**

Run: `uv run ruff check . && uv run pyright && uv run lint-imports`
Expected: 全部通过（`commands → workflow` 分层不破）。

```bash
git add assurance_agent/workflow/core/assets.py assurance_agent/commands/skill_cmd.py \
        assurance_agent/cli.py .gitignore tests/unit/test_assets.py tests/integration/test_cli_skill_refresh.py
git commit -m "feat: add aa skill refresh syncing packaged skills and opencode assets"
```

---

### Task 6: 扩展 `aa init` 的 OpenCode 注册

**Files:**
- Modify: `assurance_agent/workflow/core/assets.py`（追加 `register_opencode`）
- Modify: `assurance_agent/commands/init_cmd.py`（生成后追加注册）
- Test: `tests/unit/test_opencode_register.py`、`tests/integration/test_cli_init_opencode.py`

**Interfaces:**
- Consumes: Task 5 的 `sync_skills` / `sync_opencode` / `SyncResult` / `PLUGIN_ENTRY`；M1 `generate_project`。
- Produces:
  - `assets.py` 追加 `class OpenCodeInitResult(BaseModel)`（`opencode_json_created: bool`、`skills: SyncResult`、`opencode: SyncResult`）与 `register_opencode(project_root: Path) -> OpenCodeInitResult`——(1) 合并/创建 `<root>/opencode.json` 的 `plugin` 数组（幂等追加 `PLUGIN_ENTRY`，保留既有其它 plugin 与其它键，2 空格缩进 + 末尾换行，对齐 TS `registerOpenCode` 语义）；(2) `sync_skills` + `sync_opencode` 铺资产。
  - `aa init`（非 repair 分支）在 `generate_project` 之后调用 `register_opencode` 并打印结果；`--repair` 分支不动。

- [ ] **Step 1: 写单测**

```python
# tests/unit/test_opencode_register.py
import json
from pathlib import Path

from assurance_agent.workflow.core.assets import PLUGIN_ENTRY, register_opencode


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa/config.yaml").write_text("version: 1\n", encoding="utf-8")
    return tmp_path


def test_register_creates_opencode_json_and_assets(tmp_path: Path) -> None:
    root = _project(tmp_path)
    result = register_opencode(root)
    assert result.opencode_json_created is True
    doc = json.loads((root / "opencode.json").read_text(encoding="utf-8"))
    assert doc["plugin"] == [PLUGIN_ENTRY]
    assert (root / ".opencode/plugins/aa.mjs").is_file()
    assert (root / "skills/aa-workflow/SKILL.md").is_file()


def test_register_merges_into_existing_plugins(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "opencode.json").write_text(
        json.dumps({"plugin": ["other-plugin@1.0.0"], "theme": "dark"}, indent=2) + "\n",
        encoding="utf-8",
    )
    result = register_opencode(root)
    assert result.opencode_json_created is False
    doc = json.loads((root / "opencode.json").read_text(encoding="utf-8"))
    assert doc["plugin"] == ["other-plugin@1.0.0", PLUGIN_ENTRY]
    assert doc["theme"] == "dark"


def test_register_is_idempotent(tmp_path: Path) -> None:
    root = _project(tmp_path)
    register_opencode(root)
    register_opencode(root)
    doc = json.loads((root / "opencode.json").read_text(encoding="utf-8"))
    assert doc["plugin"].count(PLUGIN_ENTRY) == 1
```

- [ ] **Step 2: 跑单测确认失败**

Run: `uv run pytest tests/unit/test_opencode_register.py -v`
Expected: FAIL with `ImportError`（`register_opencode` 不存在）

- [ ] **Step 3: 在 assets.py 追加 register_opencode**

```python
# assurance_agent/workflow/core/assets.py（追加）
import json


class OpenCodeInitResult(BaseModel):
    opencode_json_created: bool
    skills: SyncResult
    opencode: SyncResult


def register_opencode(project_root: Path) -> OpenCodeInitResult:
    opencode_json = project_root / "opencode.json"
    created = not opencode_json.is_file()
    config: dict = {}
    if not created:
        try:
            config = json.loads(opencode_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            config = {}
            created = True  # unreadable file is rewritten fresh

    plugins = config.get("plugin")
    if not isinstance(plugins, list):
        plugins = []
    if PLUGIN_ENTRY not in plugins:
        plugins.append(PLUGIN_ENTRY)
    config["plugin"] = plugins
    opencode_json.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    return OpenCodeInitResult(
        opencode_json_created=created,
        skills=sync_skills(project_root),
        opencode=sync_opencode(project_root),
    )
```

- [ ] **Step 4: 跑单测确认通过**

Run: `uv run pytest tests/unit/test_opencode_register.py -v`
Expected: 3 passed

- [ ] **Step 5: 在 init_cmd.py 追加注册**

用 StrReplace 在 `init_cmd.py` 里，`generate_project` 成功打印之后、`Run 'aa doctor'` 提示之前追加 OpenCode 注册。改动点：

导入追加：
```python
from assurance_agent.workflow.core.assets import OpenCodeInitResult, register_opencode
```

在 `_print_result(generate_project(root, answers))` 之后插入：
```python
        _register_opencode(root)
        click.secho("assurance-agent initialized successfully.", fg="green", bold=True)
        click.echo("Run 'aa doctor' to verify your environment.")
        click.echo("Restart OpenCode, then: skill load aa-workflow")
```
（把原来的成功提示行替换为上面这段——即在成功提示前先注册 OpenCode。）

并新增辅助函数：
```python
def _register_opencode(root: Path) -> None:
    result: OpenCodeInitResult = register_opencode(root)
    if result.opencode_json_created:
        click.secho("created: opencode.json", fg="green")
    else:
        click.secho("updated: opencode.json (plugin entry ensured)", fg="green")
    click.echo(
        f"opencode assets: {len(result.skills.created)} skills, "
        f"{len(result.opencode.created)} agents/tools/plugin files"
    )
```

- [ ] **Step 6: 写集成测试**

```python
# tests/integration/test_cli_init_opencode.py
import json
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main
from assurance_agent.workflow.core.assets import PLUGIN_ENTRY


def test_init_registers_opencode_fresh(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["init", "--yes"])
        assert result.exit_code == 0, result.output
        assert "created: opencode.json" in result.output
        doc = json.loads(Path("opencode.json").read_text(encoding="utf-8"))
        assert doc["plugin"] == [PLUGIN_ENTRY]
        assert Path(".opencode/plugins/aa.mjs").is_file()
        assert Path("skills/aa-workflow/SKILL.md").is_file()


def test_init_merges_existing_opencode_json(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("opencode.json").write_text(
            json.dumps({"plugin": ["keep-me@2.0.0"], "model": "x"}, indent=2) + "\n",
            encoding="utf-8",
        )
        result = runner.invoke(main, ["init", "--yes"])
        assert result.exit_code == 0, result.output
        assert "updated: opencode.json" in result.output
        doc = json.loads(Path("opencode.json").read_text(encoding="utf-8"))
        assert doc["plugin"] == ["keep-me@2.0.0", PLUGIN_ENTRY]
        assert doc["model"] == "x"


def test_init_opencode_registration_idempotent(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init", "--yes"])
        runner.invoke(main, ["init", "--yes"])
        doc = json.loads(Path("opencode.json").read_text(encoding="utf-8"))
        assert doc["plugin"].count(PLUGIN_ENTRY) == 1
```

- [ ] **Step 7: 跑集成测试确认通过**

Run: `uv run pytest tests/integration/test_cli_init_opencode.py -v`
Expected: 3 passed

- [ ] **Step 8: 质量门禁 + Commit**

Run: `uv run ruff check . && uv run pyright && uv run lint-imports`
Expected: 全部通过

```bash
git add assurance_agent/workflow/core/assets.py assurance_agent/commands/init_cmd.py \
        tests/unit/test_opencode_register.py tests/integration/test_cli_init_opencode.py
git commit -m "feat: register opencode plugin and copy assets during aa init"
```

---

### Task 7: 打包排除 dev 脚本 + 冒烟测试扩展 + 全量回归

**Files:**
- Modify: `pyproject.toml`（wheel 排除 `scripts/`）
- Modify: `scripts/packaging_smoke_test.sh`（校验 wheel 内含 33 skills + opencode，且不含迁移脚本）
- Test: 冒烟脚本本身 + 全量 pytest 回归

**Interfaces:**
- Consumes: 前面全部产出。
- Produces: 证明 wheel 内 `resources.iter_children("skills")` 能发现 33 个 skill、`resources.iter_children("opencode", …)` 能发现 agents/tools/plugin，且 `scripts/migrate_skills.py` 不在 wheel。

- [ ] **Step 1: 确认 `_resources` 入 wheel、`scripts/` 出 wheel**

`pyproject.toml`（M1 已有 `[tool.hatch.build.targets.wheel] packages = ["assurance_agent"]` 与 `artifacts = ["assurance_agent/_resources/**"]`）。hatchling 默认只打包 `packages` 里声明的包目录，`scripts/` 位于包外故本就不入 wheel——无需额外 exclude。为防将来误配，用 StrReplace 在 wheel target 段追加显式排除注释与 `exclude`：

```toml
[tool.hatch.build.targets.wheel]
packages = ["assurance_agent"]
# _resources 含非 .py 文件（schemas/skills/opencode），hatchling 默认包含包内数据文件；
# artifacts 声明确保被 .gitignore 模式覆盖的资源也入包。
artifacts = ["assurance_agent/_resources/**"]
# scripts/ 是 dev 工具（migrate_skills.py 等），显式排除以防误入包。
exclude = ["scripts", "/scripts/**"]
```

- [ ] **Step 2: 扩展冒烟脚本**

用 StrReplace 在 `scripts/packaging_smoke_test.sh` 末尾 `echo "packaging smoke test: OK"` 之前，插入资源发现校验：

```bash
# Packaged skills + opencode assets must resolve from the wheel install.
"$WORK_DIR/venv/bin/python" - <<'PY'
from assurance_agent import resources
skills = resources.iter_children("skills")
assert len(skills) == 33, f"expected 33 skills, got {len(skills)}"
assert "aa-workflow" in skills and "writing-skills" in skills, skills
assert "aa-doc-author.md" in resources.iter_children("opencode", "agents")
assert "aa.mjs" in resources.iter_children("opencode", "plugins")
assert "workflow_start.ts" in resources.iter_children("opencode", "tools")
PY

# The one-off migration dev tool must NOT ship in the wheel.
"$WORK_DIR/venv/bin/python" - <<'PY'
import importlib.util
assert importlib.util.find_spec("scripts") is None, "scripts package leaked into wheel"
assert importlib.util.find_spec("migrate_skills") is None, "migrate_skills leaked into wheel"
PY
```

> `migrate_skills.py` 位于包外 `scripts/`，wheel 的 site-packages 不含它，两条 `find_spec` 均应为 `None`。

- [ ] **Step 3: 运行冒烟脚本**

Run: `./scripts/packaging_smoke_test.sh`
Expected: 末行 `packaging smoke test: OK`。若 `iter_children("skills")` 报 `FileNotFoundError` 或数量不符：确认 Task 1 的 `_resources/skills` 已提交且 `artifacts` 配置生效。

- [ ] **Step 4: 全量回归 + 质量门禁**

Run: `uv run pytest -v && uv run ruff check . && uv run pyright && uv run lint-imports`
Expected: 全部通过（M1–M6 既有测试 + 本里程碑新增：migration 单测 3 + assets 单测 7 + skill refresh 集成 4 + 内容校验 5 + opencode 注册单测 3 + init opencode 集成 3 = 25）。

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml scripts/packaging_smoke_test.sh
git commit -m "test: verify wheel ships 33 skills and opencode assets, excludes dev scripts"
```

---

## M7 验收清单

- `_resources/skills/` 含 33 个 skill（32 个 `aa-*` + `writing-skills`），无 `aws` 残留（Task 4 词边界正则）；跨 skill/agent 引用全部可解析；SKILL.md 字段点式引用 ⊂ M2 模型字段。
- `aa-dashboard` 用纯 Python `http.server` 服务器（无 Node 依赖），路由与安全约束与源版一致。
- `_resources/opencode/` 含 6 个 `aa-*` agent（权限地板保留：`workflow-state.yaml: deny`、无 `aa gate`/`aa status` 放行）、`aa.mjs` 插件（调 `aa` CLI）、`workflow_start.ts`（只收参数并调 M6 `aa workflow start`）、uv 版 `INSTALL.md`。
- `uv run aa skill refresh` 在 `aa init` 出的项目里同步 33 skills 并幂等；`--sync-agents` 追加 agents/tools/plugin；`--dry-run` 不写盘；无项目根退出码 1。
- `uv run aa init --yes` 在全新目录创建/合并 `opencode.json`（`plugin` 追加 `./.opencode/plugins/aa.mjs`，保留既有其它 plugin），并铺 skills + `.opencode/` 资产；重复 init 幂等。
- `./scripts/packaging_smoke_test.sh` 通过：wheel 内可发现 33 skills 与 opencode 资产，且迁移脚本不入包。
- `uv run pytest` / `ruff` / `pyright` / `lint-imports` 全绿。
- 仓库自身 `skills/`、`.opencode/` 为 `aa skill refresh` 派生的生成物（git 忽略），`_resources/` 是唯一源。

## 执行交接

Plan complete and saved to `docs/superpowers/plans/2026-07-15-m7-skills-opencode.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration（REQUIRED SUB-SKILL: superpowers:subagent-driven-development）。
2. **Inline Execution** — 本会话内按 executing-plans 批量执行 + 检查点（REQUIRED SUB-SKILL: superpowers:executing-plans）。

Which approach?
