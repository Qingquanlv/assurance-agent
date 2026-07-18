# Eval Framework Migration (M1–M4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐 Python 版 eval 的数据层、fixture 种子机制、nightly eval 接线与 retro promotion 闸门，使引擎回归可测、提案落地前可验证。

**Architecture:** 混合布局——引擎仓库 `eval/` 存 suites/datasets/baselines/suts.yaml；SUT 侧 `eval-fixtures/` 存黄金样本与 tiers；运行产物写 SUT `eval/out/`。种子走 `read_state`/`write_state` 保审计完整性。M1→M4 在同一计划内连续交付，但任务仍按依赖顺序排列，每任务可独立测试。

**Tech Stack:** Python 3.11+, pydantic v2, click, PyYAML, pytest, existing `assurance_agent.eval.*` / `retro.*` / `workflow.core.state`.

**Spec:** `docs/superpowers/specs/2026-07-16-eval-framework-migration-design.md`

## Global Constraints

- 种子重置 state 必须经 `read_state`/`write_state`，禁止直接改 YAML（避免 `STATE-INTEGRITY-TAMPERED`）。
- eval 永不写真实 SUT：工作流类 suite 在 `_copy_attempt_workspace` 沙箱内运行。
- `eval/out/` 统一落 SUT 根，供 `read_eval_trend(sut_root)` 读取。
- 确定性 suite（`executor.type: aws-run` / workflow-run 无 agent）用 fake adapter / CLI；agent suite 用 cursor-agent。
- TDD：先失败测试再实现；每个 Task 结束 `git commit`（conventional commits）。
- 不迁原版 4 个无 suite 遗留数据集；不做 judge 校准迁移。

## File Map

| Path | Responsibility |
|------|----------------|
| `assurance_agent/eval/paths.py` | 双根：数据根(引擎) + 输出根(SUT) |
| `assurance_agent/eval/fixtures.py` | TierManifest、load_tier、seed_change |
| `assurance_agent/eval/suts.py` | 读 suts.yaml、解析 local_dir |
| `assurance_agent/eval/executor.py` | fixture_tier 种子 + executor.type 分发 |
| `assurance_agent/eval/runner.py` | 输出写 SUT out；传 fixtures_root |
| `assurance_agent/eval/scorers/*.py` | 补 4 个 scorer |
| `assurance_agent/commands/eval_cmd.py` | sut 输出根、extra-memory-dir 接线 |
| `assurance_agent/commands/retro_cmd.py` | resume 接 eval_runner；apply 子命令 |
| `assurance_agent/retro/nightly/driver.py` | resume 完整闸门 |
| `assurance_agent/retro/apply.py` | 提案 apply 到 stage-dir |
| `eval/suites/*.yaml` + `eval/datasets/**` | 引擎契约数据 |
| `benchmark/vue-fastapi-admin/eval-fixtures/**` | 黄金样本 |
| `scripts/capture_eval_fixture.py` | 从归档捕获 fixture |

---

### Task 1: Dual-root paths + suts registry

**Files:**
- Modify: `assurance_agent/eval/paths.py`
- Create: `assurance_agent/eval/suts.py`
- Create: `eval/suts.yaml`
- Test: `tests/unit/eval/test_paths.py`, `tests/unit/eval/test_suts.py`

**Interfaces:**
- Produces:
  - `eval_data_root(engine_root: Path) -> Path` → `engine_root / "eval"`
  - `eval_out_root(sut_root: Path) -> Path` → `sut_root / "eval" / "out"`
  - `runs_dir(sut_root: Path) -> Path`, `run_dir(sut_root, run_id)`, `attempt_dir(...)` 全部基于 sut_root
  - `datasets_dir(engine_root, suite)` 基于 engine_root
  - `load_sut_registry(engine_root: Path) -> dict[str, SutEntry]` where `SutEntry` has `local_dir: str`, `pinned_rev: str | None`
  - `resolve_sut_dir(engine_root: Path, sut_name: str | None = None) -> Path`

- [ ] **Step 1: Write failing tests** for `runs_dir(sut)` under `tmp/sut/eval/out/runs` and `datasets_dir(engine)` under `tmp/engine/eval/datasets/<suite>`; `resolve_sut_dir` reads `eval/suts.yaml`.

- [ ] **Step 2: Implement paths + suts; update callers** (`runner.py`, `baseline.py`, `report.py`, `eval_cmd.py`, `eval_trend.py` if needed) so out paths take `sut_root`, data paths take `engine_root`.

- [ ] **Step 3: Commit** `feat(eval): dual-root paths and suts registry`

---

### Task 2: Fixture TierManifest + seed_change

**Files:**
- Create: `assurance_agent/eval/fixtures.py`
- Test: `tests/unit/eval/test_fixtures.py`

**Interfaces:**
- Produces:
  - `class TierManifest(BaseModel): name, extends: str | None, description: str = "", paths: list[str], resets: dict, source_prefix: str | None = None`
  - `load_tier(fixtures_root: Path, tier_name: str) -> TierManifest` — resolve extends chain, accumulate paths, deep-merge resets
  - `seed_change(*, sut_sandbox: Path, change_id: str, tier_name: str, fixtures_root: Path, sample_id: str | None = None) -> None`
    - clear `qa/changes/<change_id>/`
    - copy paths (`tests/` → sut tests dir, else → change dir)
    - apply `resets.workflow_state` via `read_state`/`write_state`
    - apply `resets.qa_yaml` if present

- [ ] **Step 1: Write failing tests** — extends chain merge; seed creates files; after seed `verify_state_integrity` is None and synthetic status audit has no STATE-INTEGRITY-TAMPERED.

- [ ] **Step 2: Implement fixtures.py** (atomic: seed to temp then replace).

- [ ] **Step 3: Commit** `feat(eval): fixture tier loading and seed_change`

---

### Task 3: Wire seeding into executor + runner out-root

**Files:**
- Modify: `assurance_agent/eval/executor.py`
- Modify: `assurance_agent/eval/runner.py`
- Modify: `assurance_agent/commands/eval_cmd.py`
- Test: `tests/unit/eval/test_executor_seed.py`, update existing `tests/unit/eval/*`

**Interfaces:**
- Consumes: `seed_change`, dual-root paths
- Produces: `execute_attempt(..., fixtures_root: Path | None = None)` seeds when `sample.input.get("fixture_tier")`; `run_suite(..., engine_root, sut_dir)` writes to `run_dir(sut_dir, run_id)`

- [ ] **Step 1: Failing test** — sample with fixture_tier calls seed before loop; run_suite writes report under sut `eval/out/runs/`.

- [ ] **Step 2: Implement; keep no-fixture samples backward compatible.**

- [ ] **Step 3: Commit** `feat(eval): seed before execute and write out under SUT`

---

### Task 4: Synthetic workflow-run suite (M1 acceptance)

**Files:**
- Create: `eval/suites/workflow-run.yaml`
- Create: `eval/datasets/workflow-run/WR-SYNTH-001.yaml`
- Create: `tests/fixtures/eval_synth/` (tiny golden sample + L3 tier for unit/integration)
- Test: `tests/integration/test_eval_workflow_run_synth.py`

**Interfaces:**
- Suite `executor.type: aws-run` (or `workflow-run` with fake adapter), scorer `workflow-run`, thresholds soft/empty for M1.

- [ ] **Step 1: Failing integration test** with `AA_EVAL_FAKE_ADAPTER=1`, synthetic fixtures under tmp, assert report.json exists and gate verdict not crash.

- [ ] **Step 2: Add suite/dataset/synth fixtures; make green.**

- [ ] **Step 3: Commit** `feat(eval): synthetic workflow-run suite for M1`

---

### Task 5: Capture script + real golden fixture (M2 data)

**Files:**
- Create: `scripts/capture_eval_fixture.py`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/` (captured)
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L0-*.yaml` … `L3-*.yaml`
- Update: `eval/suts.yaml` pinned_rev
- Update: `.gitignore` — allow `!benchmark/vue-fastapi-admin/eval-fixtures/`

**Interfaces:**
- Script CLI: `python scripts/capture_eval_fixture.py --archive <path> --out benchmark/vue-fastapi-admin/eval-fixtures --sample-id eval-sample-001`
- Writes sample snapshot + tier manifests (L0 case → L3 run-ready) by truncating phase state.

- [ ] **Step 1: Implement capture script; dry-run against archive RET-api-management-20260716-192358-cursor.**

- [ ] **Step 2: Commit captured fixtures** `chore(eval): capture api-management golden fixture L0-L3`

- [ ] **Step 3: Failing test** that `seed_change` on L3 against captured sample leaves integrity OK.

- [ ] **Step 4: Commit** `test(eval): golden fixture L3 seed integrity`

---

### Task 6: Migrate all 11 suites + datasets; executor type dispatch; missing scorers

**Files:**
- Create: `eval/suites/{_test,case-generation,classification-unit,safety-lite,workflow-api-codegen,workflow-case,workflow-e2e-codegen,workflow-full,workflow-fuzz-codegen,workflow-performance-codegen}.yaml` (+ workflow-run already in Task 4)
- Create: `eval/datasets/<suite>/*.yaml` (port/adapt from original; change_id → eval-sample-001; fixture_tier per suite)
- Modify: `assurance_agent/eval/executor.py` — dispatch `aws-run` | `workflow-run` | `in_process` | `subprocess`
- Create: `assurance_agent/eval/scorers/classification_unit.py`, `safety_lite.py`, `case_generation.py`, `_test.py`
- Modify: `assurance_agent/eval/scorers/__init__.py`
- Test: `tests/unit/eval/test_suite_load_all.py`, `tests/unit/eval/test_scorer_registry.py`

**Interfaces:**
- `get_scorer(suite_name)` covers all 11 suite names
- `in_process` executor: no sandbox; call registered callable from suite config
- `subprocess` / agent `workflow-run`: HeadlessAdapter(cursor-agent)

- [ ] **Step 1: Failing tests** — load every suite file; registry has all scorers.

- [ ] **Step 2: Port suite YAML + minimal datasets; implement scorers (metrics from original TS scorers, simplified where needed); executor dispatch.**

- [ ] **Step 3: Commit** `feat(eval): migrate 11 suites, scorers, executor dispatch`

---

### Task 7: Baseline approve path + first baseline (M2 acceptance)

**Files:**
- Existing: `assurance_agent/commands/eval_cmd.py` baseline update
- Create/update: `eval/baselines/main.json` after a successful fake/synth or real run
- Test: `tests/unit/eval/test_baseline_update.py` (extend if needed)

- [ ] **Step 1: Run synth suite, `aa eval baseline update --suite workflow-run --run <id> --approved-by migration --yes`, commit baseline.**

- [ ] **Step 2: Commit** `chore(eval): approve initial workflow-run baseline`

---

### Task 8: Wire eval_runner into resume_nightly (M3)

**Files:**
- Modify: `assurance_agent/commands/retro_cmd.py` resume
- Modify: `assurance_agent/retro/nightly/driver.py` as needed
- Modify: `assurance_agent/retro/eval_trend.py` if path assumption wrong (should already be sut-relative after Task 1)
- Test: `tests/unit/retro/test_resume_eval_runner.py`

**Interfaces:**
- `eval_runner(*, suite: str, sut_dir: Path, engine_root: Path, extra_memory_dir: Path | None = None) -> dict` with keys `run_id`, `verdict`, `metrics`
- resume without `--skip-eval` invokes runner when promotions pending for eval

- [ ] **Step 1: Failing test** — resume with mock eval_runner records call and returns NIGHTLY_OK when proposals exist.

- [ ] **Step 2: Wire real runner in retro_cmd; keep skip_eval / None path.**

- [ ] **Step 3: Commit** `feat(retro): wire eval_runner into resume_nightly`

---

### Task 9: aa retro apply + promotion gate (M4)

**Files:**
- Create: `assurance_agent/retro/apply.py`
- Modify: `assurance_agent/commands/retro_cmd.py` — `apply` command
- Modify: `assurance_agent/retro/nightly/driver.py` — staging → eval → compare baseline → promote/rollback
- Test: `tests/unit/retro/test_apply.py`, `tests/integration/test_retro_promotion_gate.py`

**Interfaces:**
- `apply_proposal_to_stage(*, sut_root, retro_id, proposal_id, stage_dir: Path) -> None` — copy memory overlay for `memory_append` proposals into stage_dir
- `resume_nightly` groups promoted proposals by suite; for each group: apply to stage → eval_runner with extra_memory_dir=stage → compare_with_baseline → write `promotions.json` (`applied` | `rolled_back` | `needs_rework`); missing baseline → `inconclusive` (no hard fail)

- [ ] **Step 1: Failing unit tests** for apply and both gate outcomes (pass→applied, fail→rolled_back).

- [ ] **Step 2: Implement apply + resume gate loop.**

- [ ] **Step 3: Commit** `feat(retro): proposal apply staging and eval promotion gate`

---

### Task 10: End-to-end smoke + docs touch

**Files:**
- Update: `docs/eval.md` (if tracked) or short note in plan commit message
- Test: run `uv run pytest tests/unit/eval tests/unit/retro tests/integration/test_eval* tests/integration/test_retro* -q`

- [ ] **Step 1: Full related suite green.**

- [ ] **Step 2: Commit** `test(eval): M1-M4 related suites green`

---

## Spec coverage checklist

| Spec section | Tasks |
|---|---|
| §2 Dual-root layout + suts.yaml | 1 |
| §3 seed_change + integrity | 2, 3 |
| §4 executor types + 11 suites + scorers + capture | 4, 5, 6 |
| §5 nightly eval_runner | 8 |
| §6 promotion gate + apply | 9 |
| §7 M1–M4 acceptance | 4, 7, 8, 9, 10 |
| §8 risks (sandbox, inconclusive baseline) | 2, 3, 9 |
| §9 non-goals | omitted by design |

## Self-review notes

- No TBD placeholders in task interfaces.
- `run_suite` signature after Task 3: `(*, suite_file, engine_root, sut_dir, ...)` — all later tasks use this.
- Baseline approval is Task 7 (M2), not M1 — matches corrected spec.
