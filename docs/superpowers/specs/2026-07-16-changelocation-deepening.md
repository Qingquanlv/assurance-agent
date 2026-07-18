# ChangeLocation 深化：让 ChangeLocation 成为 Change 的唯一接口

日期：2026-07-16
状态：设计已获用户批准（grilling 8 项决定全部锁定）；待实现

## 1. 背景与动机

架构评审（candidate 1）发现：`ChangeLocation`（`change_location.py`）本应是"这个 Change 在哪"的唯一 seam，但在栈里被中途抛弃——调用方拿到 `.path` 后就丢掉 `ChangeLocation`，然后在约 10 处各自重新推导 Change 目录与项目根：

- `gates._project_root` 用 `change_dir.parents[2]` 反推项目根（`gates.py:33-35`）；
- `resolve_change_path` 用 `change_dir.name` 反推 change_id（`gates.py:44`）；
- retro `nightly/driver`、`phase_a`、`archive_reader`、`execution/runner` 各自重复 `rel[2:] if rel.startswith("./")` 的 `./`-strip；
- `risk/paths.py`、`risk/context._sample_archives` 硬编码 `project_root / "qa" / "changes"` / `"qa" / "archive"`，完全忽略 `AaConfig.qa`；
- `PhaseContext` 把 `change_id` / `change_dir` / `project_root` 三个字段分开穿（`loop.py:80-82`）。

**潜在正确性 bug**：`parents[2]` 写死"`change_dir = <root>/qa/changes/<id>` 正好 3 层"。默认布局成立，但配置 `qa.changes` 为非 3 层路径（如 `./work/ch`）时反推错误。这是"config 布局被到处忽略"的具体体现。

**领域事实（`aa-archive/SKILL.md` 佐证）**：归档是 **copy-not-move**（第 196 行 "Copy (do not move)"），且**不删除** `qa/changes/<id>/`（第 215、294 行，保留为引用，仅人工清理）。因此归档后 Change **永久同时存在于** `qa/changes/` 和 `qa/archive/` 两处——这是正常稳态，不是冲突。当前 `resolve_change_any` 却把双存在当作致命 `ChangeAmbiguousError`，逼得 retro 造了 archive-first 的绕行适配器，并留了一个回归测试记录这次崩溃。

目标：把 Change 的目录/角色/根的**全部策略收进一个深模块**，接口就是 `ChangeLocation`；消除所有反推与硬编码；删除不该存在的 ambiguity 概念。

## 2. 已锁定的设计决定

| # | 决定 | 结论 |
|---|---|---|
| Q1 | 双存在语义 | 双存在是正常稳态；**删除 `ChangeAmbiguousError`**；接口用两值 `prefer` |
| Q2 | seam 位置与公开面 | 路径策略（`changes_root`/`archive_root`）收进 `change_location.py`；retro 的 `unarchived`/`archive` 词汇留在 retro 边界做薄映射 |
| Q3 | 爆炸半径 | **方案 B**：只在"当前需要反推"的地方传 `ChangeLocation`；纯读 change 目录的叶子函数保持收 `change_dir: Path`（传 `loc.path`） |
| Q4 | 命名 | **保留 `resolve_change` 名字**，加 keyword `prefer`（默认 `active`）；不改名为 `resolve()` |
| Q5 | 硬编码路径边界 | **改 risk**（`risk/paths.py`、`risk/context._sample_archives`）；**不改 `phase_prompt.py`**（agent 行为契约，另议） |
| Q6 | gates/engine | **B-plus**：把 `project_root` 传进 `resolve_change_path`，根治 `parents[2]`；连带改 engine/operations/gates 规则求值/review_fix_episode/status provider |
| Q7 | ADR | 记 **ADR-0002** |
| Q8 | 测试 | 非默认布局回归测试 + `prefer` 矩阵测试；保留旧回归测试仅改 docstring |

## 3. 目标接口（`change_location.py` 完整公开面）

```python
ChangeSource = Literal["changes", "archive"]      # 解析结果落在哪个根
ChangePreference = Literal["active", "archive"]    # 调用方的角色偏好

@dataclass(frozen=True)
class ChangeLocation:
    project_root: Path
    change_id: str
    path: Path
    source: ChangeSource

def resolve_change(
    project_root: Path,
    change_id: str,
    *,
    prefer: ChangePreference = "active",   # "active" → changes; "archive" → archive-first
) -> ChangeLocation: ...

def changes_root(project_root: Path) -> Path: ...   # 吸收 ./-strip
def archive_root(project_root: Path) -> Path: ...
```

> 说明：`prefer` 是独立于 `ChangeSource` 的两值 literal。`prefer="active"` 语义 = 原 `resolve_change`（changes 存在则返回，否则 `ChangeNotFoundError`，保留 "found under archive … write commands require an active change" 提示）。`prefer="archive"` 语义 = archive 存在则返回 `source="archive"`，否则回退 changes（`source="changes"`）。

**删除**：`resolve_change_any`、`ChangeAmbiguousError`。

## 4. 实施范围

### 4.1 `change_location.py`
- 给 `resolve_change` 加 `*, prefer="active"`；实现两种偏好。
- 新增 `changes_root` / `archive_root`（复用 `_normalize_rel`）。
- 删除 `resolve_change_any` 与 `ChangeAmbiguousError`。

### 4.2 反推点替换（B）
- `driver/loop.py`：`PhaseContext` 持有 `ChangeLocation`（不再分开存 `change_id`/`change_dir`/`project_root`；用 `@property` 暴露 `change_id`/`change_dir`/`project_root`，叶子执行器零改动）；构造处从 `resolve_change` 拿 `loc`。
- retro `nightly/driver.py`、`nightly/phase_a.py`：去掉 `./`-strip 与 `parents[2]`，改用 `changes_root`/`archive_root`；`_default_is_terminal` 由 `collect_nightly` 用 `functools.partial(_default_is_terminal, sut)` 绑定 `project_root`（`IsTerminal` 协议签名不变）。
- **不改** `execution/runner.py`：其 `_strip` 处理的是 `config.tests.*` 目录（与 Change 定位无关），且 `project_root` 是显式入参、`change_dir.name` 取末段可靠——本次无需触碰。

### 4.3 gates/engine 根治（B-plus，Q6b：change_dir → ChangeLocation 一换一）
把 gate/engine 内部**调用 `resolve_change_path` 的那条链**上的 `change_dir: Path` 参数**换成** `loc: ChangeLocation`（一换一，不新增透传参数——避免 review 批评的 pass-through config threading）。`change_dir` → `loc.path`，`project_root` → `loc.project_root`，`change_id` → `loc.change_id`。
- `gates.resolve_change_path(loc, rel)`：删 `_project_root`/`parents[2]`，用 `loc.project_root` + `loc.change_id`。
- 连带换 `loc` 的函数：`gates`（`_load_doc`、`build_evidence_scope`、`resolve_gate_verdict`、`_adjudicate`、`check_gate`）、`engine`（`_file_exists`、`_produces_present`、`compute_status`、`_phase_view`）、`review_fix_episode.project_review_fix_loop`、`healing_episode.project_healing_episode`、`operations`（:261/:550）。
- 纯叶子函数（只读 change 目录下文件，如 `derive_healing_state`、`_overlay_healing`）保持 `change_dir: Path`，由上游传 `loc.path`。
- 上游入口 `status_cmd`、`gate_cmd` 已持有 `loc`；`operations`、retro nightly driver、loop status provider 需构造/持有 `loc`。

### 4.4 risk
- `risk/paths.py`、`risk/context._sample_archives`：`qa/changes`、`qa/archive` 硬编码 → `changes_root`/`archive_root`。
- 保留 risk 的**无 config 运行能力**（`aa risk context` 历史上不要求 `.aa/config.yaml`）：`ConfigNotFoundError` 时回退默认布局 `qa/changes` / `qa/archive`。有 config 时一律以 config 为准（达成 Q5 目标，零行为回归）。

### 4.5 retro 边界
- `archive_reader.resolve_change_dir`：内部改调 `resolve_change(..., prefer="archive")`；`loc.source == "changes"` → `"unarchived"` 映射保留在 retro；返回类型 `tuple[Path, EvidenceSource] | None` 不变（`aggregator`、`phase_a` 两个调用方不动）。
- `archive_reader.list_archived_changes`：`./`-strip → `archive_root`。

### 4.6 不改
- `phase_prompt.py`：发给 agent 的 `qa/changes/{change_id}/` 提示文本是行为契约，本次不动。

## 5. ADR-0002（待写）

`docs/adr/0002-changelocation-role-preference.md`，锁定：
1. 归档是 copy-not-move；`qa/changes/<id>` 与 `qa/archive/<id>` 双存在是正常稳态。
2. `resolve_change` 用 `prefer` 角色偏好解析，**不做** ambiguity 检测（`ChangeAmbiguousError` 已删除）；未来 review 不得以"更安全"为由加回双存在检测。
3. 路径根一律走 config（`changes_root`/`archive_root`），**禁止** `parents[2]` 之类深度反推。

## 6. 测试

1. **非默认布局回归测试**：fixture 将 `qa.changes` 配成非默认（如 `./work/ch`，深度 ≠ 3），跑 `resolve_change`、gates 求值（经 `resolve_change_path`）、status、selection，断言全部正确解析。核心证据：B-plus 消除了 `parents[2]` 假设。
2. **`prefer` 矩阵**：`resolve_change` 在 (只 active / 只 archive / 两者都在) × (`prefer="active"` / `"archive"`) 组合下的返回；断言双存在不再抛异常。
3. `test_enumerate_candidates_handles_preserved_change_dir_after_archive`：**保留**，仅更新 docstring 去掉对 `ChangeAmbiguousError` 的引用。

## 7. 影响与后续

- 迁移面：约 15 个 `resolve_change` 调用方零改动（默认 `prefer`）；gates/engine 链 + risk + retro 适配器约 8-10 个文件。
- 为后续 candidate 铺路：候选 4（统一 `workflow-state.yaml` reader）与候选 7（`ArchiveIndex`）都依赖 `ChangeLocation` 进入其接口。
