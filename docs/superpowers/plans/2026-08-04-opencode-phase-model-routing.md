# OpenCode Phase Model Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让同一条 AA OpenCode workflow 按精确 skill、上次 attempt 错误和显式 CLI override 选择模型，同时保持权限 persona、write set、retry budget 和非 OpenCode adapter 的既有行为不变。

**Architecture:** 在 graph 层新增纯 `ModelRouter`，把配置解析结果注入 `AgentHandler`，由 request-local `AgentRequest.model` 驱动 OpenCode 请求；在 core event 中追加可选、向后兼容的执行审计元数据。`aa retro` 复用同一 adapter/router 构造链，benchmark 再用 ledger 生成按 model/skill 聚合的对比指标。

**Tech Stack:** Python 3.11、Pydantic v2、Click、YAML、OpenCode legacy HTTP API、pytest、ruff、pyright、import-linter、Bash。

## Global Constraints

- 设计权威文件是 `docs/superpowers/specs/2026-08-04-opencode-phase-model-routing-design.md`。
- 当前工作区已有大量用户改动；每个任务只暂存该任务列出的文件，禁止 `git add .`、`git add -A`、reset、checkout 或清理未跟踪文件。
- provider、base URL、header 和密钥继续只存在于 `opencode.json`；`.aa/config.yaml` 只保存 `provider/model` 路由名。
- 模型路由只能改变 model，不能改变 agent、权限、allowed writes、timeout、retry budget、workflow topology 或 gate verdict。
- `.opencode/agents/aa-*.md` 不新增 `model` frontmatter；`aa init` 不写 Ark 专属模型 ID。
- v1 只接受精确 skill name，不支持 glob、node id、prompt/内容匹配。
- `--model` 是整条 workflow 的最高优先级 override，并禁用 route coverage 和 escalation；配置结构与 model 格式仍必须校验。
- 自动 escalation 只接受 `invalid_output` 和 `forbidden_write`；`auth`、`rate_limit`、`transport`、`timeout` 不切模型。
- Headless/Cursor 默认和模型解析顺序保持不变；OpenCode routing 不得泄漏给 Headless。
- 未知 token usage 必须写 `null`，不能伪造为 `0`；不得把 prompt、header、key 写进 ledger。
- 新增 event 字段必须是 optional，以保证历史 event strict replay；本计划不升级 event schema version。
- 每个生产改动先写失败测试，再写最小实现；每个任务结束执行列出的 targeted test。

---

## Task 1: 建立 typed model-routing 配置契约

**Files:**

- Modify: `assurance_agent/config.py`
- Modify: `tests/unit/test_config.py`

- [ ] **Step 0: 固化现有 OpenCode agent 权限文件基线**

Run:

```bash
rg --files assurance_agent/_resources/opencode/agents -g '*.md' \
  | sort \
  | xargs shasum -a 256 \
  > /tmp/aa-opencode-agent-permissions.before
```

Expected: manifest 创建成功。它记录当前脏工作区的用户版本，而不是拿 Git HEAD
误判用户已有改动。

- [ ] **Step 1: 写配置解析的失败测试**

在 `tests/unit/test_config.py` 增加覆盖：

```python
def test_model_routing_is_optional(tmp_path: Path) -> None:
    root = write_valid_config(tmp_path)
    assert load_config(root).execution.model_routing is None


def test_model_routing_rejects_unknown_nested_field(tmp_path: Path) -> None:
    root = write_config_with_routing(tmp_path, {"strict_routes": True, "typo": 1})
    with pytest.raises(ConfigInvalidError, match=r"execution\.model_routing\.typo"):
        load_config(root)


@pytest.mark.parametrize("model", ["", "glm-5.2", "/glm-5.2", "anthropic/"])
def test_model_routing_rejects_invalid_model_id(tmp_path: Path, model: str) -> None:
    root = write_config_with_routing(tmp_path, {"default": model})
    with pytest.raises(ConfigInvalidError):
        load_config(root)


def test_model_routing_rejects_wildcard_skill_key(tmp_path: Path) -> None:
    root = write_config_with_routing(
        tmp_path,
        {"routes": {"aa-*-plan": "anthropic/glm-5.2"}},
    )
    with pytest.raises(ConfigInvalidError, match="exact skill"):
        load_config(root)


def test_model_routing_rejects_duplicate_route_key(tmp_path: Path) -> None:
    root = write_raw_config_with_duplicate_routes(
        tmp_path,
        "aa-api-plan: anthropic/deepseek-v4-flash\n"
        "aa-api-plan: anthropic/glm-5.2\n",
    )
    with pytest.raises(ConfigInvalidError, match="duplicate key.*aa-api-plan"):
        load_config(root)


def test_model_routing_rejects_non_string_route_value(tmp_path: Path) -> None:
    root = write_config_with_routing(tmp_path, {"routes": {"aa-api-plan": 42}})
    with pytest.raises(ConfigInvalidError):
        load_config(root)


@pytest.mark.parametrize("kind", ["auth", "rate_limit", "transport", "timeout"])
def test_model_routing_rejects_non_contract_escalation_kind(
    tmp_path: Path, kind: str
) -> None:
    root = write_config_with_routing(
        tmp_path,
        {"escalation": {"model": "anthropic/glm-5.2", "on_error_kinds": [kind]}},
    )
    with pytest.raises(ConfigInvalidError):
        load_config(root)
```

- [ ] **Step 2: 运行测试并确认因字段不存在或错误配置被接受而失败**

Run: `uv run pytest tests/unit/test_config.py -q`

Expected: 新测试 FAIL；旧配置测试仍通过。

- [ ] **Step 3: 增加严格的 nested Pydantic models**

在 `assurance_agent/config.py` 中使用独立 strict base，而不是改变现有 `_Model(extra="allow")` 的全局兼容性：

```python
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator

EscalationErrorKind = Literal["invalid_output", "forbidden_write"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _validated_model_id(value: str) -> str:
    normalized = value.strip()
    provider, separator, model = normalized.partition("/")
    if separator != "/" or not provider or not model:
        raise ValueError("model must be a non-empty provider/model identifier")
    return normalized


class ModelEscalationCfg(_StrictModel):
    model: StrictStr
    on_error_kinds: tuple[EscalationErrorKind, ...]

    _validate_model = field_validator("model")(_validated_model_id)


class ModelRoutingCfg(_StrictModel):
    default: StrictStr | None = None
    strict_routes: StrictBool = False
    routes: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    escalation: ModelEscalationCfg | None = None

    @field_validator("default")
    @classmethod
    def validate_default(cls, value: str | None) -> str | None:
        return None if value is None else _validated_model_id(value)

    @field_validator("routes")
    @classmethod
    def validate_routes(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for skill, model in value.items():
            if re.fullmatch(r"aa-[a-z0-9]+(?:-[a-z0-9]+)*", skill) is None:
                raise ValueError("routes keys must be exact aa-* skill names")
            normalized[skill] = _validated_model_id(model)
        return normalized


class ExecutionCfg(_Model):
    entry: str
    self_healing: SelfHealingCfg
    model_routing: ModelRoutingCfg | None = None
```

不要给 `routes` 做隐式 key 修正；大小写、空白或拼错必须保持可检测。
用自定义 `yaml.SafeLoader` mapping constructor 检测同一 mapping 内的重复 key，
在 `safe_load` 产生覆盖前抛 `yaml.YAMLError`，由 `load_config` 统一转成
`ConfigInvalidError`。这样重复 route 不会静默采用最后一个值。

- [ ] **Step 4: 补合法配置断言**

验证 tuple/error kind、route 和 default 均按规范加载，并确认原始 `.aa/config.yaml` 不含 routing 时行为不变。

- [ ] **Step 5: 运行配置测试**

Run: `uv run pytest tests/unit/test_config.py -q`

Expected: PASS。

- [ ] **Step 6: 提交本任务**

```bash
git add assurance_agent/config.py tests/unit/test_config.py
git commit -m "feat: validate opencode model routing config"
```

---

## Task 2: 实现纯 ModelRouter 与 strict compiled coverage

**Files:**

- Create: `assurance_agent/workflow/graph/model_routing.py`
- Create: `tests/unit/workflow/graph/test_model_routing.py`

- [ ] **Step 1: 写五级优先级和 digest 测试**

测试使用真实 `ModelRoutingCfg`，至少覆盖：无配置 → `opencode_default`；default；精确 route；两类 escalation；CLI override；相同 normalized policy digest 稳定。

```python
def test_cli_override_wins_over_escalation_and_skill_route() -> None:
    router = ModelRouter(policy())
    resolution = router.resolve(
        ModelRouteContext(
            adapter="opencode",
            skill="aa-api-plan",
            prior_error_kind="invalid_output",
            cli_override="anthropic/forced",
        )
    )
    assert resolution.model == "anthropic/forced"
    assert resolution.source == "cli_override"


@pytest.mark.parametrize("kind", ["invalid_output", "forbidden_write"])
def test_contract_failure_escalates(kind: ErrorKind) -> None:
    resolution = ModelRouter(policy()).resolve(context(prior_error_kind=kind))
    assert resolution.model == "anthropic/glm-5.2"
    assert resolution.source == "escalation"


@pytest.mark.parametrize("kind", ["auth", "rate_limit", "transport", "timeout"])
def test_infrastructure_failure_does_not_escalate(kind: ErrorKind) -> None:
    resolution = ModelRouter(policy()).resolve(context(prior_error_kind=kind))
    assert resolution.source == "skill_route"
```

- [ ] **Step 2: 写 strict coverage 失败测试**

构造包含 `skill:aa-api-plan`、`skill:aa-report-generator` 和 operation/gate 的 compiled workflow。断言 strict 模式只要求两个 skill；缺一个时在 session 创建前抛出列出缺失 skill 的 `ModelRoutingError`；CLI override 时跳过 coverage。

- [ ] **Step 3: 运行测试确认模块缺失**

Run: `uv run pytest tests/unit/workflow/graph/test_model_routing.py -q`

Expected: collection FAIL，提示 `model_routing` 模块不存在。

- [ ] **Step 4: 实现纯 router**

`assurance_agent/workflow/graph/model_routing.py` 的公开接口固定为：

```python
from typing import Literal

from pydantic import BaseModel, ConfigDict

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.config import ModelRoutingCfg
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.graph.models import CompiledWorkflow

RouteSource = Literal[
    "cli_override", "escalation", "skill_route", "default", "opencode_default"
]


class ModelRouteContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    adapter: str
    skill: str
    prior_error_kind: ErrorKind | None = None
    cli_override: str | None = None


class ModelResolution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    model: str | None
    source: RouteSource
    policy_sha256: str | None


class ModelRoutingError(AaError):
    pass


class ModelRouter:
    def __init__(self, policy: ModelRoutingCfg | None):
        self._policy = policy
        self._policy_sha256 = (
            None
            if policy is None
            else sha256_bytes(
                canonical_json_bytes(policy.model_dump(mode="json", exclude_none=True))
            )
        )
```

`resolve` 的签名固定为 `resolve(self, context: ModelRouteContext) -> ModelResolution`；
`validate_compiled` 的签名固定为
`validate_compiled(self, compiled: CompiledWorkflow, *, adapter: str, cli_override: str | None) -> None`。
两者按下述优先级和 coverage 约束实现，不增加其他输入源。

实现约束：

- digest 只对 `policy.model_dump(mode="json", exclude_none=True)` 的 canonical JSON 求 SHA-256；无 policy 时为 `None`。
- `validate_compiled` 遍历 `compiled.graphs.values()` 中所有 node definition 的 `uses`，收集 `skill:` 后缀并去重排序。
- `adapter != "opencode"` 时 coverage no-op；Headless 不调用 `resolve`。
- strict 缺 route 时错误信息包含完整、排序后的 skill 列表。
- strict 模式不允许用 `default` 掩盖漏配；CLI override 是唯一 coverage bypass。

- [ ] **Step 5: 运行 router 测试**

Run: `uv run pytest tests/unit/workflow/graph/test_model_routing.py -q`

Expected: PASS。

- [ ] **Step 6: 验证 import layering**

Run: `uv run lint-imports`

Expected: PASS；graph 不 import driver。

- [ ] **Step 7: 提交本任务**

```bash
git add assurance_agent/workflow/graph/model_routing.py tests/unit/workflow/graph/test_model_routing.py
git commit -m "feat: resolve models by workflow skill"
```

---

## Task 3: 把 request-local route 接入 AgentHandler 和 runtime

**Files:**

- Modify: `assurance_agent/workflow/graph/agent_api.py`
- Modify: `assurance_agent/workflow/graph/handlers/agent.py`
- Modify: `assurance_agent/workflow/graph/task_runner.py`
- Modify: `assurance_agent/workflow/driver/runtime_factory.py`
- Modify: `assurance_agent/workflow/driver/loop.py`
- Modify: `assurance_agent/commands/workflow_cmd.py`
- Modify: `tests/unit/workflow/graph/test_task_runner.py`
- Create: `tests/unit/driver/test_runtime_factory.py`
- Modify: `tests/unit/driver/test_loop.py`
- Modify: `tests/integration/test_cli_workflow.py`
- Modify: `tests/integration/test_cli_workflow_v2.py`

- [ ] **Step 1: 写 AgentHandler 路由测试**

在 `test_task_runner.py` 用 recording invoker 验证同一 compiled graph 的两个 task 分别产生：

```python
assert requests[0].agent == "aa-doc-author"
assert requests[0].model == "anthropic/deepseek-v4-flash"
assert requests[0].model_route_source == "skill_route"
assert requests[1].agent == "aa-reviewer"
assert requests[1].model == "anthropic/glm-5.2"
```

同时断言 model 变化不改变 `allowed_writes`、workspace、timeout 和 agent persona。

- [ ] **Step 2: 写 retry escalation 集成测试**

让第一次 `aa-api-plan` 返回 `invalid_output`，第二 attempt 收到 planner 已提供的 `prior_error_kind`，断言第二 request 使用 GLM；另写 `rate_limit` 用例，断言仍使用 skill route 模型。

- [ ] **Step 3: 写 runtime fail-before-invoke 测试**

`strict_routes=true` 且 compiled workflow 缺 route 时，`build_graph_runtime` 抛 `ModelRoutingError`，recording invoker 调用数为 0。显式 `cli_model_override` 时构造成功。

在 `tests/integration/test_cli_workflow.py` 和
`tests/integration/test_cli_workflow_v2.py` 再断言：detached child 与非 detached run
透传相同的 `adapter_name` / `cli_model_override`；resume 和 import-checkpoint 也使用
本次 CLI 启动时加载的 routing，而不是遗漏参数或复用 adapter constructor 的旧值。

- [ ] **Step 4: 运行测试确认请求模型字段和注入参数缺失**

Run:

```bash
uv run pytest \
  tests/unit/workflow/graph/test_task_runner.py \
  tests/unit/driver/test_runtime_factory.py \
  tests/unit/driver/test_loop.py -q
```

Expected: 新测试 FAIL。

- [ ] **Step 5: 扩展 graph request seam**

在 `AgentRequest` 追加全部带默认值的字段，保持 fake/eval invoker 兼容：

```python
model: str | None = None
model_route_source: RouteSource | None = None
model_policy_sha256: str | None = None
```

- [ ] **Step 6: 向 AgentHandler 注入 router context**

扩展 `AgentHandler.__init__` 接受 `model_router: ModelRouter | None`、`adapter_name: str | None`、`cli_model_override: str | None`。在已有：

```python
skill = task.target.partition(":")[2]
```

之后、调用 adapter 之前解析：

```python
resolution = None
if self._model_router is not None and self._adapter_name == "opencode":
    resolution = self._model_router.resolve(
        ModelRouteContext(
            adapter="opencode",
            skill=skill,
            prior_error_kind=task.prior_error_kind,
            cli_override=self._cli_model_override,
        )
    )
```

然后把 `resolution` 三个字段写入 `AgentRequest`。不要改变现有 prior-failure prompt、agent routing 或 freeze 流程。

- [ ] **Step 7: 从 CLI 一路透传 adapter 名和 override**

为下列函数新增默认参数，避免测试/eval 调用者被迫修改：

向 `build_graph_runtime` 和 `run_workflow_loop` 都增加 keyword-only 参数
`adapter_name: str | None = None` 与 `cli_model_override: str | None = None`；
返回类型仍为现有的 `RuntimeBundle` 与 `LoopResult`。

`build_graph_runtime` 在 `adapter_name == "opencode"` 时：

1. `load_config(project_root)`；
2. 构造 `ModelRouter(config.execution.model_routing)`；
3. 对刚 compile 的 workflow 执行 `validate_compiled`；
4. 把 router 和 CLI override 传入 `build_default_node_runner` / `AgentHandler`。

`workflow run/resume/import-checkpoint` 及 detached child 都透传同一 `adapter_name` 和原始 `--model`。不要让 adapter constructor 自己成为第二套优先级决策器。

- [ ] **Step 8: 增加 Headless 回归测试**

项目即使配置 OpenCode routes，`adapter_name="headless"` 也不得在 `AgentRequest` 写 OpenCode model；现有 `_resolve_headless_model` 的 CLI → env → Cursor default 顺序保持不变。

- [ ] **Step 9: 运行 targeted tests**

Run:

```bash
uv run pytest \
  tests/unit/workflow/graph/test_task_runner.py \
  tests/unit/driver/test_runtime_factory.py \
  tests/unit/driver/test_loop.py \
  tests/integration/test_cli_workflow.py \
  tests/integration/test_cli_workflow_v2.py -q
```

Expected: PASS。

- [ ] **Step 10: 提交本任务**

```bash
git add \
  assurance_agent/workflow/graph/agent_api.py \
  assurance_agent/workflow/graph/handlers/agent.py \
  assurance_agent/workflow/graph/task_runner.py \
  assurance_agent/workflow/driver/runtime_factory.py \
  assurance_agent/workflow/driver/loop.py \
  assurance_agent/commands/workflow_cmd.py \
  tests/unit/workflow/graph/test_task_runner.py \
  tests/unit/driver/test_runtime_factory.py \
  tests/unit/driver/test_loop.py \
  tests/integration/test_cli_workflow.py \
  tests/integration/test_cli_workflow_v2.py
git commit -m "feat: route agent attempts through runtime"
```

---

## Task 4: 让 OpenCode adapter 使用 request-local model

**Files:**

- Modify: `assurance_agent/workflow/driver/opencode_adapter.py`
- Modify: `tests/unit/driver/test_opencode_adapter.py`
- Modify: `tests/unit/workflow/graph/test_task_runner.py`

- [ ] **Step 1: 写 transport body 测试**

构造 adapter fallback 为 GLM，但 request-local model 为 DeepSeek，断言同一 `prompt_async` body 同时包含指定 agent 和 request model：

```python
result = adapter.invoke(
    valid_agent_request(
        agent="aa-doc-author",
        model="anthropic/deepseek-v4-flash",
        model_route_source="skill_route",
    )
)
assert transport.prompt_bodies[-1]["agent"] == "aa-doc-author"
assert transport.prompt_bodies[-1]["model"] == {
    "providerID": "anthropic",
    "modelID": "deepseek-v4-flash",
}
```

再断言没有 request-local model 时，graph `invoke()` 省略 model；legacy `run_phase()` 仍可使用 constructor fallback。

- [ ] **Step 2: 写不可用模型不回落测试**

模拟 prompt endpoint 对显式 model 返回 400/404，断言：

- `AgentResult.ok is False`；
- `error_kind == "invalid_input"`；
- transport 只调用一次；
- 不再发不带 model 的请求。

同时保留 401/403 → `auth`、429 → `rate_limit` 的既有断言。

- [ ] **Step 3: 运行测试确认 constructor model 仍覆盖 request**

Run: `uv run pytest tests/unit/driver/test_opencode_adapter.py -q`

Expected: request-local 用例 FAIL。

- [ ] **Step 4: 修改 adapter 调用边界**

把 `_dispatch_prompt` 改为显式接收已解析 model：

```python
def _dispatch_prompt(
    self,
    session_id: str,
    prompt: str,
    *,
    agent: str | None,
    model: dict[str, str] | None,
    directory: str | None = None,
) -> None:
    body: dict[str, Any] = {"parts": [{"type": "text", "text": prompt}]}
    if model is not None:
        body["model"] = model
    if agent is not None:
        body["agent"] = agent
    response = self._request(
        "POST",
        f"/session/{session_id}/prompt_async",
        json=body,
        directory=directory,
    )
    if response.status_code != 204 and not 200 <= response.status_code < 300:
        raise _OpenCodeCallError(
            _classify_prompt_status(
                response.status_code,
                model_was_explicit=model is not None,
            ),
            f"opencode prompt failed ({response.status_code}): {response.text[:300]}",
        )
```

`invoke()` 只解析 `request.model`；`run_phase()` 继续传 `self._model`。删除任何显式 model 被拒后重发默认模型的分支。

- [ ] **Step 5: 只对携带显式 model 的 prompt 400/404 做错误归一化**

保留通用 `_classify_http_status` 不变，新增 prompt-local classifier：

```python
def _classify_prompt_status(
    status_code: int,
    *,
    model_was_explicit: bool,
) -> ErrorKind:
    if model_was_explicit and status_code in (400, 404):
        return "invalid_input"
    return _classify_http_status(status_code)
```

`_dispatch_prompt` 必须把 `model is not None` 传给该 helper。补一组反向测试：
没有显式 model 的 prompt 返回 400/404 时仍按既有语义归类为 `internal`，避免把普通
请求格式、session 或 endpoint 问题误报成模型不可用。401/403 和 429 无论是否携带
model，仍分别归为 `auth` 和 `rate_limit`。

- [ ] **Step 6: 运行 adapter 与 graph invoke 测试**

Run:

```bash
uv run pytest \
  tests/unit/driver/test_opencode_adapter.py \
  tests/unit/workflow/graph/test_task_runner.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交本任务**

```bash
git add \
  assurance_agent/workflow/driver/opencode_adapter.py \
  tests/unit/driver/test_opencode_adapter.py \
  tests/unit/workflow/graph/test_task_runner.py
git commit -m "feat: send request-local model to opencode"
```

---

## Task 5: 把实际模型选择持久化到 settled attempt event

**Files:**

- Create: `assurance_agent/workflow/core/agent_execution.py`
- Modify: `assurance_agent/workflow/core/graph_events.py`
- Modify: `assurance_agent/workflow/graph/agent_api.py`
- Modify: `assurance_agent/workflow/graph/model_routing.py`
- Modify: `assurance_agent/workflow/graph/models.py`
- Modify: `assurance_agent/workflow/graph/handlers/agent.py`
- Modify: `assurance_agent/workflow/graph/task_runner.py`
- Modify: `assurance_agent/workflow/graph/finalize.py`
- Modify: `assurance_agent/workflow/graph/scheduler.py`
- Modify: `tests/unit/workflow/graph/test_scheduler.py`
- Modify: `tests/unit/workflow/graph/test_checkpoint.py`
- Modify: `tests/unit/workflow/graph/test_task_runner.py`
- Modify: `tests/unit/workflow/graph/test_finalize_and_child_stop.py`

- [ ] **Step 1: 写 success/failure/STOP 审计测试**

分别驱动 agent task 成功、adapter 失败、输出校验失败/forbidden write 和 STOP，读取 strict ledger 后断言 settlement event 都包含：

```python
assert event.agent_execution.skill == "aa-api-plan"
assert event.agent_execution.agent == "aa-doc-author"
assert event.agent_execution.model == "anthropic/deepseek-v4-flash"
assert event.agent_execution.route_source == "skill_route"
assert event.agent_execution.model_policy_sha256.startswith("sha256:")
assert event.agent_execution.session_id == "ses_123"
assert event.agent_execution.usage is None
```

`skill` 必须保存，因为 benchmark 要按 model/skill 聚合，不能从易变 node id 反推。

- [ ] **Step 2: 写历史 event replay 测试**

用不含 `agent_execution` 的旧 success/failed/stopped JSON fixture 走 `read_events_strict` 和 checkpoint projection，断言仍通过且字段为 `None`。

再模拟一次 resume 前后更新项目 routing：第一个 attempt 使用旧 router digest，第二个
新 attempt 使用新 router digest。断言两个 settled event 的
`model_policy_sha256` 不同，旧的 successful task 不重跑，event schema version 不变。

- [ ] **Step 3: 运行测试确认 strict event 拒绝新字段**

Run:

```bash
uv run pytest \
  tests/unit/workflow/graph/test_scheduler.py \
  tests/unit/workflow/graph/test_checkpoint.py \
  tests/unit/workflow/graph/test_task_runner.py \
  tests/unit/workflow/graph/test_finalize_and_child_stop.py -q
```

Expected: 新审计测试 FAIL。

- [ ] **Step 4: 在 core 定义可复用审计模型**

创建不依赖 graph/driver 的模型：

```python
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RouteSource = Literal[
    "cli_override", "escalation", "skill_route", "default", "opencode_default"
]


class AgentUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class AgentExecutionMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    skill: str
    agent: str | None = None
    model: str | None = None
    route_source: RouteSource | None = None
    model_policy_sha256: str | None = None
    session_id: str | None = None
    usage: AgentUsage | None = None
```

让 `model_routing.py` import 此处的 `RouteSource`，避免重复 literal 漂移。

- [ ] **Step 5: 贯穿 result seam**

向 `AgentResult` 和 graph `TaskResult` 增加：

```python
agent_execution: AgentExecutionMetadata | None = None
```

`AgentHandler` 在 adapter 返回后，用 request 的 skill/agent/route、result session 和可信 usage 构造 metadata。即使后续 artifact validation 或 `freeze_write_set` 失败，也必须把这份 metadata 保留在失败 result 中。

把 `task_failure` 扩展为可选接收 `agent_execution`。`finalize_task_result` 的
`_ensure_outputs_frozen`、`_ingest_frozen_outputs`、registry validation、subgraph export
和 attached gate 失败分支在把 succeeded result 转成 failed result 时，必须复制原
`result.agent_execution`；不能因为错误发生在 adapter 返回之后就丢失模型归因。

- [ ] **Step 6: 扩展三种 settled event 并由 scheduler 传递**

向 `TaskAttemptSucceededEvent`、`TaskAttemptFailedEvent`、`TaskAttemptStoppedEvent` 添加 optional `agent_execution`。扩展 `_persist_result`、`_persist_success`、`_persist_failure`；freeze failure 调用 `_persist_failure` 时也传 `result.agent_execution`。非 agent task 保持 `None`。

- [ ] **Step 7: 运行 replay、scheduler 和 layering 检查**

Run:

```bash
uv run pytest \
  tests/unit/workflow/graph/test_scheduler.py \
  tests/unit/workflow/graph/test_checkpoint.py \
  tests/unit/workflow/graph/test_task_runner.py \
  tests/unit/workflow/graph/test_finalize_and_child_stop.py -q
uv run lint-imports
```

Expected: PASS；event schema version 保持 1。

- [ ] **Step 8: 提交本任务**

```bash
git add \
  assurance_agent/workflow/core/agent_execution.py \
  assurance_agent/workflow/core/graph_events.py \
  assurance_agent/workflow/graph/agent_api.py \
  assurance_agent/workflow/graph/model_routing.py \
  assurance_agent/workflow/graph/models.py \
  assurance_agent/workflow/graph/handlers/agent.py \
  assurance_agent/workflow/graph/task_runner.py \
  assurance_agent/workflow/graph/finalize.py \
  assurance_agent/workflow/graph/scheduler.py \
  tests/unit/workflow/graph/test_scheduler.py \
  tests/unit/workflow/graph/test_checkpoint.py \
  tests/unit/workflow/graph/test_task_runner.py \
  tests/unit/workflow/graph/test_finalize_and_child_stop.py
git commit -m "feat: audit model choice for agent attempts"
```

---

## Task 6: 让 canonical Retro 复用 OpenCode adapter 和 routing

**Files:**

- Create: `assurance_agent/workflow/driver/adapter_factory.py`
- Modify: `assurance_agent/commands/workflow_cmd.py`
- Modify: `assurance_agent/commands/retro_cmd.py`
- Create: `tests/unit/driver/test_adapter_factory.py`
- Modify: `tests/integration/test_retro_cli.py`
- Modify: `tests/integration/test_cli_workflow.py`

- [ ] **Step 1: 写 Retro CLI 兼容性测试**

保留现有 `AA_RETRO_AGENT_CMD=true` 的默认 Headless 测试，并增加：

- `aa retro --help` 展示 `--adapter`、`--server`、`--directory`、`--model`、`--agent-cmd`；
- 默认 adapter 仍为 Headless；
- `--adapter opencode` 缺 `--server` fail closed；
- OpenCode Retro 把 `adapter_name="opencode"` 和 `cli_model_override` 传给 `run_workflow_loop`；
- Retro graph 中 `aa-retro-*` skill 能按项目配置获得不同 request-local model。

- [ ] **Step 2: 运行测试确认 Retro 固定 Headless**

Run:

```bash
uv run pytest \
  tests/integration/test_retro_cli.py \
  tests/integration/test_cli_workflow.py -q
```

Expected: 新 OpenCode Retro 测试 FAIL。

- [ ] **Step 3: 抽取不依赖 Click 的 adapter factory**

把 `workflow_cmd.py` 中 `_DEFAULT_CURSOR_MODEL`、`_resolve_headless_model` 和构造逻辑搬到 `workflow/driver/adapter_factory.py`：

```python
DEFAULT_CURSOR_MODEL = "cursor-grok-4.5-high-fast"


def resolve_headless_model(model: str | None, *, env: Mapping[str, str]) -> str:
    if model is not None and model.strip():
        return model.strip()
    for key in ("AA_CURSOR_MODEL", "CURSOR_MODEL"):
        if value := env.get(key, "").strip():
            return value
    return DEFAULT_CURSOR_MODEL
```

`build_adapter` 保留现有七个输入：`adapter_name`、`project_root`、`server`、
`directory`、`model`、`parent_session`、`agent_cmd`，返回 `AgentInvoker`。

factory 抛 `DriverError`；Click command 只负责显示并转成 `EXIT_ERROR`。测试分别钉住 Headless CLI/env/default 顺序和 OpenCode server required。

- [ ] **Step 4: 扩展 `aa retro` 选项并接同一 runtime seam**

默认值保持：`adapter=headless`、`agent_cmd` 来自 `AA_RETRO_AGENT_CMD` 或 `cursor-agent --print`。OpenCode 模式用共享 factory，并调用：

```python
run_workflow_loop(
    project_root=invocation.project_root,
    change_id=invocation.shell_change_id,
    entrypoint="retro",
    adapter=adapter,
    params=dict(invocation.params),
    adapter_name=adapter_name,
    cli_model_override=model,
)
```

不要把 `opencode run` shell 字符串继续塞进 `HeadlessAdapter`；那条路径无法提供 request-local model、agent 或审计元数据。

- [ ] **Step 5: 运行 factory 和 Retro 测试**

Run:

```bash
uv run pytest \
  tests/unit/driver/test_adapter_factory.py \
  tests/integration/test_retro_cli.py \
  tests/integration/test_cli_workflow.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交本任务**

```bash
git add \
  assurance_agent/workflow/driver/adapter_factory.py \
  assurance_agent/commands/workflow_cmd.py \
  assurance_agent/commands/retro_cmd.py \
  tests/unit/driver/test_adapter_factory.py \
  tests/integration/test_retro_cli.py \
  tests/integration/test_cli_workflow.py
git commit -m "feat: route retro through selectable adapter"
```

---

## Task 7: 配置五项 benchmark 的保守混合路由

**Files:**

- Modify: `benchmark/vue-fastapi-admin/.aa/config.yaml`
- Modify: `benchmark/vue-fastapi-admin/benchmark/run-workflow-loop.sh`
- Modify: `benchmark/vue-fastapi-admin/benchmark/cursor-loop-helpers.sh`
- Modify: `tests/unit/benchmark/test_opencode_loop_parity.py`
- Modify: `tests/unit/benchmark/test_cursor_loop_helpers.py`

- [ ] **Step 1: 写 packaged workflow route coverage 测试**

测试加载 benchmark config 和 packaged workflow，收集全部 compiled `skill:*`，断言与 `strict_routes=true` 的 route keys 集合相等。额外遍历 `assurance_agent/_resources/opencode/agents/aa-*.md` frontmatter，断言不存在 `model`。

- [ ] **Step 2: 写 benchmark adapter 分流测试**

Shell/parity 测试钉住：

- `OPENCODE_MODEL` 非空时 workflow 和 Retro 都传 `--model`，形成单模型 override；
- `OPENCODE_MODEL` 未设置时不传 `--model`，读取项目 mixed routing；
- OpenCode Retro 使用 `aa retro --adapter opencode --server http://127.0.0.1:4096`；
- Cursor helper 仍使用 Headless/`AA_RETRO_AGENT_CMD`，不读取 OpenCode routes；
- 日志明确输出 `routing=cli-override` 或 `routing=project-config`。

- [ ] **Step 3: 运行测试确认 config 和 Retro 脚本尚未接通**

Run:

```bash
uv run pytest \
  tests/unit/benchmark/test_opencode_loop_parity.py \
  tests/unit/benchmark/test_cursor_loop_helpers.py -q
```

Expected: 新测试 FAIL。

- [ ] **Step 4: 写入 spec 确认的完整 route map**

在 benchmark `.aa/config.yaml` 添加设计文档第 4 节的完整精确映射：

- DeepSeek 首次：四类 plan、report、archive、三类 Retro signal analysis；
- GLM：explore、fact baseline、case、所有 reviewer/fixer、所有 codegen、fix proposal、inspect、issue、Retro proposal、Improvement review；
- escalation：GLM，仅 `invalid_output`、`forbidden_write`；
- `strict_routes: true`。

Codegen 在具备 executable semantic mapping gate 前不得改成 DeepSeek。

- [ ] **Step 5: 更新 OpenCode/Cursor Retro 调用**

OpenCode loop 删除 `opencode_agent_cmd_prefix` 驱动 canonical Retro 的做法，改为真实 OpenCode adapter；Cursor loop 保持现有 Headless 命令。两边复用批次 manifest、硬超时、SUT/前端管理、归档和结果判定，不改流程语义。

- [ ] **Step 6: 运行 benchmark parity tests 和 shell syntax check**

Run:

```bash
uv run pytest \
  tests/unit/benchmark/test_opencode_loop_parity.py \
  tests/unit/benchmark/test_cursor_loop_helpers.py -q
bash -n benchmark/vue-fastapi-admin/benchmark/run-workflow-loop.sh
bash -n benchmark/vue-fastapi-admin/benchmark/cursor-loop-helpers.sh
```

Expected: PASS。

- [ ] **Step 7: 提交本任务**

```bash
git add \
  benchmark/vue-fastapi-admin/.aa/config.yaml \
  benchmark/vue-fastapi-admin/benchmark/run-workflow-loop.sh \
  benchmark/vue-fastapi-admin/benchmark/cursor-loop-helpers.sh \
  tests/unit/benchmark/test_opencode_loop_parity.py \
  tests/unit/benchmark/test_cursor_loop_helpers.py
git commit -m "feat: configure hybrid benchmark model routes"
```

---

## Task 8: 生成按 model/skill 聚合的 benchmark 指标

**Files:**

- Create: `benchmark/vue-fastapi-admin/benchmark/summarize_model_routes.py`
- Modify: `benchmark/vue-fastapi-admin/benchmark/run-workflow-loop.sh`
- Create: `tests/unit/benchmark/test_model_route_summary.py`
- Modify: `tests/unit/benchmark/test_opencode_loop_parity.py`

- [ ] **Step 1: 写 synthetic ledger 聚合测试**

fixture 包含两个 attempt：DeepSeek `invalid_output` 失败，GLM escalation 成功；另含旧 event、usage null 和 STOP。断言输出：

```json
{
  "models": {
    "anthropic/deepseek-v4-flash": {
      "skills": {
        "aa-api-plan": {
          "attempts": 1,
          "succeeded": 0,
          "failed": 1,
          "stopped": 0,
          "invalid_output": 1,
          "forbidden_write": 0
        }
      }
    }
  }
}
```

还要断言 elapsed seconds 由同一 `attempt_id` 的 `started_at` 到 settlement `ts` 计算；usage 缺失不计作零 token；历史无 metadata event 计入 `unattributed_attempts`，不能静默丢失。

- [ ] **Step 2: 运行测试确认 summarizer 不存在**

Run: `uv run pytest tests/unit/benchmark/test_model_route_summary.py -q`

Expected: collection FAIL。

- [ ] **Step 3: 实现 archive-first 的只读 summarizer**

脚本输入 batch manifest/批次目录，优先读取 archive 中的 change events，找不到才读 canonical change。按 `attempt_id` 配对 start 和三类 settled event，并聚合：

- attempts / succeeded / failed / stopped；
- invalid_output / forbidden_write；
- elapsed seconds；
- input/output tokens，仅 usage 非 null 时；
- route source 计数；
- unattributed attempts。

输出 `model-routing-metrics.json`，并提供 deterministic Markdown table。不得估算费用；provider 没有 usage 时保留 null/无样本。

- [ ] **Step 4: 接入 loop summary**

每批结束调用 summarizer，并在现有汇总追加：

```markdown
## Model Routing Metrics

| Model | Skill | Attempts | Success | Invalid output | Forbidden write | Elapsed s | Tokens |
```

汇总脚本失败应使 benchmark 结果标为 infrastructure failure，而不是把空表当作 pass。

- [ ] **Step 5: 运行单测和 parity tests**

Run:

```bash
uv run pytest \
  tests/unit/benchmark/test_model_route_summary.py \
  tests/unit/benchmark/test_opencode_loop_parity.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交本任务**

```bash
git add \
  benchmark/vue-fastapi-admin/benchmark/summarize_model_routes.py \
  benchmark/vue-fastapi-admin/benchmark/run-workflow-loop.sh \
  tests/unit/benchmark/test_model_route_summary.py \
  tests/unit/benchmark/test_opencode_loop_parity.py
git commit -m "feat: summarize benchmark model routing metrics"
```

---

## Task 9: 文档化配置边界与兼容行为

**Files:**

- Modify: `README.md`
- Modify: `assurance_agent/_resources/opencode/INSTALL.md`
- Modify: `docs/schemas.md`
- Modify: `tests/unit/test_templates.py`

- [ ] **Step 1: 写 provider-neutral init 回归测试**

生成最小 SUT 的 `.aa/config.yaml`，断言不包含 `anthropic/glm-5.2`、`anthropic/deepseek-v4-flash` 或 `model_routing`。这是通用产品模板边界，不因 benchmark 配置改变。

- [ ] **Step 2: 更新三处文档**

文档必须明确：

- provider/model 注册和凭据在 `opencode.json`；
- phase routing 在项目 `.aa/config.yaml`；
- 权限在 `.opencode/agents/aa-*.md`，模型切换不改变权限；
- exact skill、strict coverage、escalation 两类错误；
- `--model` 强制单模型并覆盖 route/escalation；
- 未配置时使用 OpenCode default；
- Headless/Cursor 忽略 OpenCode routing；
- `aa retro --adapter opencode --server http://127.0.0.1:4096` 才会执行 Retro phase routing；
- usage 不可用时指标为空，不伪造 token/费用。

- [ ] **Step 3: 运行模板与文档相关测试**

Run:

```bash
uv run pytest \
  tests/unit/test_templates.py \
  tests/unit/test_config.py \
  tests/unit/benchmark/test_opencode_loop_parity.py -q
```

Expected: PASS。

- [ ] **Step 4: 提交本任务**

```bash
git add \
  README.md \
  assurance_agent/_resources/opencode/INSTALL.md \
  docs/schemas.md \
  tests/unit/test_templates.py
git commit -m "docs: explain opencode phase model routing"
```

---

## Task 10: 验证跨边界行为与完整 CI

**Files:**

- Modify only if a failing test exposes a defect in files already named by Tasks 1–9.

- [ ] **Step 1: 运行路由专项测试集**

Run:

```bash
uv run pytest \
  tests/unit/test_config.py \
  tests/unit/workflow/graph/test_model_routing.py \
  tests/unit/workflow/graph/test_task_runner.py \
  tests/unit/workflow/graph/test_scheduler.py \
  tests/unit/driver/test_runtime_factory.py \
  tests/unit/driver/test_loop.py \
  tests/unit/driver/test_opencode_adapter.py \
  tests/unit/driver/test_adapter_factory.py \
  tests/integration/test_cli_workflow.py \
  tests/integration/test_cli_workflow_v2.py \
  tests/integration/test_retro_cli.py \
  tests/unit/benchmark/test_opencode_loop_parity.py \
  tests/unit/benchmark/test_cursor_loop_helpers.py \
  tests/unit/benchmark/test_model_route_summary.py -q
```

Expected: PASS。

- [ ] **Step 2: 运行完整 CI gate**

Run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest
bash scripts/packaging_smoke_test.sh
```

Expected: 六项全部 PASS。若失败，先区分本计划回归与工作区既存未提交改动；只修与本计划直接相关的问题。

- [ ] **Step 3: 检查权限文件未被模型功能改写**

Run:

```bash
rg --files assurance_agent/_resources/opencode/agents -g '*.md' \
  | sort \
  | xargs shasum -a 256 \
  > /tmp/aa-opencode-agent-permissions.after
diff -u \
  /tmp/aa-opencode-agent-permissions.before \
  /tmp/aa-opencode-agent-permissions.after
rg -n '^model:' assurance_agent/_resources/opencode/agents -g '*.md'
```

Expected: baseline diff 无输出，`rg` 无输出；本功能没有改变用户现有的 agent 权限文件。

- [ ] **Step 4: 检查初始化模板无 provider-specific ID**

Run:

```bash
rg -n 'glm-5\.2|deepseek-v4-flash|model_routing' \
  assurance_agent/workflow/core/templates.py \
  assurance_agent/_resources -g '*.yaml' -g '*.yml'
```

Expected: 通用初始化模板无命中；benchmark 项目配置不在该搜索范围内。

- [ ] **Step 5: 若验证修复产生代码变更，单独提交**

只暂存 Step 2 实际修过、且已在 Tasks 1–9 列出的明确文件，然后执行
`git commit -m "fix: close model routing verification gaps"`。

若没有修复，不创建空提交。

---

## Task 11: 执行同 commit、同 fixture 的三组五项 benchmark 验收

**Files:**

- Generated only: benchmark batch logs、archives、`model-routing-metrics.json` 和 loop summary；不提交生成物。

- [ ] **Step 1: 固定验收基线**

记录当前 commit SHA、OpenCode server URL、五个 item manifest、fixture lock digest 和 provider 配置版本。确认三组运行之间不改代码、配置或 fixture；每组开始前按 benchmark 现有安全清理/归档流程恢复同一基线。

- [ ] **Step 2: 执行 DeepSeek-only 五项批次**

Run:

```bash
OPENCODE_MODEL=anthropic/deepseek-v4-flash \
  benchmark/vue-fastapi-admin/benchmark/run-workflow-loop.sh
```

Expected: 日志显示 `routing=cli-override`；所有 settled agent attempts 的 route source 是 `cli_override`，model 均为 DeepSeek。

- [ ] **Step 3: 执行 GLM-only 五项批次**

Run:

```bash
OPENCODE_MODEL=anthropic/glm-5.2 \
  benchmark/vue-fastapi-admin/benchmark/run-workflow-loop.sh
```

Expected: 日志显示 `routing=cli-override`；所有 settled agent attempts 的 model 均为 GLM。

- [ ] **Step 4: 执行阶段 A mixed-routing 五项批次**

Run:

```bash
env -u OPENCODE_MODEL \
  benchmark/vue-fastapi-admin/benchmark/run-workflow-loop.sh
```

Expected: 日志显示 `routing=project-config`；DeepSeek/GLM 按 exact skill 分布；DeepSeek 的 `invalid_output` 或 `forbidden_write` 后若 graph 允许 retry，下一 attempt 记录 `route_source=escalation` 和 GLM。

- [ ] **Step 5: 评审三组结果**

对同一五项比较：

- workflow 完成率与最终状态；
- artifact invalid、forbidden-write、STOP 和 retry 次数；
- case → plan → codegen traceability findings；
- 总墙钟时间；
- 按 model/skill 的 token/usage（仅可获得时）；
- 五个 Improvement 的事实依据、重复度、是否属于程序 bug。

阶段 A mixed 只有在不降低 GLM-only 完成率和语义 traceability 的情况下才可成为 benchmark 默认。没有可信 provider usage 时只比较耗时与质量，不推算费用。

- [ ] **Step 6: 做出 rollout 结论**

- 若 mixed 达标：保留阶段 A map，后续另立任务建设 semantic mapping gate，再讨论 Codegen 首次使用 DeepSeek。
- 若 mixed 回退：依据 `model-routing-metrics.json` 定位具体 skill，把该 skill route 调回 GLM；不得通过放宽 agent 权限、contract、gate 或 retry policy 掩盖模型问题。
- 若失败来自 auth/rate-limit/transport/timeout：标为 infrastructure inconclusive，不算模型质量失败，也不自动切模型。
