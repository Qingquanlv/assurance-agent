# Assurance Telemetry Wheel Implementation Plan

> **For agentic workers:** Execute in the `user-full-workflow-db-oracle-trace` worktree. Do not change full graph topology.

**Goal:** 把 OTLP 解析/注入/封存/义务核验抽成已安装 `assurance.telemetry` wheel，图只认 `api_db_trace.v1`。

**Architecture:** 第七个 capability plugin，无 task handler、无新节点。execute/quality 调端口。第一版实现仍是 Demoso OTLP JSONL。`.aa/` 组织配置仍只列六个编排插件。

**Tech Stack:** 现有 uv workspace、Pydantic v2、graph_engine.plugins、OTel SDK。

## Global Constraints

- 不新增 full / execute-tail 节点或边，不新增 `api_db_otel.v1` / `api_db_cat.v1`
- telemetry 不得 import `assurance_execution`
- quality 不 import telemetry 或 execution 的 operations
- 不实现 CAT
- 不改 SUT `app/__init__.py`（除非重写 runtime-lock）
- 不提交 `.superpowers/sdd/*`
- 不伪造 live OpenCode

## Files

- Create: `packages/capabilities/assurance-telemetry/**`
- Modify: execution telemetry modules → re-export shims
- Modify: quality `assessment.py` imports
- Modify: product catalog / `_CAPABILITY_PLUGIN_IDS` / binding_builder / product declaration
- Modify: root `pyproject.toml`, `.importlinter`, execution/quality/product `pyproject.toml`
- Test: plugin conformance + catalog includes telemetry + existing telemetry tests via shims

---

### Task 1: Package and port

Create the wheel, move contracts/operations, invert Manifest/Observation imports via duck typing + `TraceObservationV1`.

### Task 2: Product assembly

Always assemble `assurance.telemetry` into the product lock (not into `.aa/` six-plugin config). No graph factory changes.

### Task 3: Verify

`uv sync`, regenerate declarations, run import-linter + telemetry/plugin/product provider/quality replay tests.
