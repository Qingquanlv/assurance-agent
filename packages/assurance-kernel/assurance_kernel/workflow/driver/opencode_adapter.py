"""OpenCode server HTTP adapter (spec 5a).

Endpoints/payloads transcribed from the CURRENT TS opencode_adapter.ts:
- POST /session                    {title, parentID?}                 -> {id}
- POST /session/{id}/prompt_async  {parts:[{type:'text',text}], model?, agent?, variant?} -> 204
- GET  /session/status             -> { "<sid>": {"type": "busy|retry"} | "idle" }
- GET  /session/{id}/message       -> [{info:{role,error?}, parts:[...]}]

The prompt is dispatched ASYNCHRONOUSLY and completion is detected by polling
/session/status for a SUSTAINED idle streak. A synchronous POST /message would
hold a connection open for the entire (multi-minute) agent run and trip the
server's ~300s header timeout ("fetch failed"), killing every long phase; and
treating the first idle as "done" aborts phases mid-flight because OpenCode
briefly drops a session from the status map between tool rounds.

Every request carries ?directory=<sut> and optional Basic-Auth headers derived
from the environment (never from CLI args). Only an *explicit* model is pinned;
otherwise the field is omitted so the server resolves its own default (TS
behavior — pinning a parent session's stale model breaks phases).

同时实现 graph 的 ``AgentInvoker``：v2 路径的每个请求都使用
``request.workspace_root`` 作为 ``?directory=``（task 私有 workspace），并把
失败归一化为 typed error kind（401/403 → ``auth``，429 → ``rate_limit``，网络
错误 → ``transport``，轮询超时 → ``timeout``）；成功时返回新建 session ID 供
reconnect 元数据使用。
"""

import base64
import hashlib
import inspect
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml

from assurance_kernel import resources
from assurance_kernel.workflow.core.graph_types import ErrorKind
from assurance_kernel.workflow.driver.adapter import DriverError, PhaseRequest, PhaseResult
from assurance_kernel.workflow.graph.agent_api import AgentRequest, AgentResult

SessionStatus = Literal["busy", "retry", "idle"]

DEFAULT_POLL_INTERVAL_S = 2.0
DEFAULT_POLL_MAX_S = 3600.0
DEFAULT_IDLE_DONE_STREAK = 8
MAX_PROVIDER_OVERLOAD_RETRY_ATTEMPTS = 8
DEFAULT_ABORT_TIMEOUT_S = 30.0
OPENCODE_VARIANT_ENV = "AA_OPENCODE_VARIANT"

BOUNDED_OPENCODE_AGENTS = (
    "aa-archiver",
    "aa-doc-author",
    "aa-explorer",
    "aa-intake-host",
    "aa-reporter",
    "aa-reviewer",
    "aa-test-author",
)
SANDBOX_ESCAPE_TOOLS = (
    "task",
    "task_create",
    "task_get",
    "task_list",
    "task_update",
    "call_omo_agent",
    "look_at",
    "skill_mcp",
    "interactive_bash",
    "monitor_start",
    "session_list",
    "session_read",
    "session_search",
    "session_info",
    "background_output",
    "background_cancel",
    "webfetch",
    "websearch",
    "websearch_web_search_exa",
)
BOUNDED_REQUIRED_TOOLS = ("write", "artifact_write")
BOUNDED_PROMPT_TOOL_OVERRIDES = {
    "artifact_write": True,
    "write": True,
    "edit": True,
    "apply_patch": False,
    "ast_grep_replace": False,
    "webfetch": False,
    "websearch": False,
    "websearch_web_search_exa": False,
}
_SKILL_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)
_BOUNDARY_PROBE_PREFIX = "aa_boundary_probe_v1_"


class _NoDuplicateKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that treats duplicate metadata keys as malformed."""


def _construct_unique_mapping(
    loader: yaml.SafeLoader,
    node: yaml.MappingNode,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in mapping:
            raise yaml.YAMLError(f"duplicate key {key!r}")
        mapping[key] = loader.construct_object(value_node)
    return mapping


_NoDuplicateKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class _OpenCodeCallError(DriverError):
    """带 typed ``ErrorKind`` 的 opencode 调用失败；v1 run_phase 仍按 DriverError 捕获。"""

    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind: ErrorKind = kind


@dataclass(frozen=True)
class _RetryAction:
    reason: str | None = None
    provider: str | None = None
    title: str | None = None
    message: str | None = None
    label: str | None = None
    link: str | None = None


@dataclass(frozen=True)
class _SessionStatusInfo:
    type: SessionStatus
    attempt: int | None = None
    message: str | None = None
    action: _RetryAction | None = None
    next: int | None = None


@dataclass(frozen=True)
class _PackagedSkillContract:
    name: str
    description: str
    content: str


@dataclass(frozen=True)
class _PackagedAgentContract:
    name: str
    description: str
    mode: str
    prompt: str
    disabled_tools: tuple[str, ...]
    enabled_tools: tuple[str, ...]
    permission_rules: tuple[tuple[str, str, str], ...]


def _classify_http_status(status_code: int) -> ErrorKind:
    if status_code in (401, 403):
        return "auth"
    if status_code == 429:
        return "rate_limit"
    return "internal"


def _classify_prompt_status(status_code: int, *, model_was_explicit: bool) -> ErrorKind:
    if model_was_explicit and status_code in (400, 404):
        return "invalid_input"
    return _classify_http_status(status_code)


def _shield_bounded_prompt(prompt: str, agent: str | None) -> str:
    """Keep third-party keyword hooks from rewriting bounded AA instructions.

    OpenCode plugins conventionally exclude ``system-reminder`` blocks from
    keyword-derived mode injection.  AA graph prompts contain unavoidable words
    such as the ``aa-explore`` skill name and source-search prohibitions; without
    this boundary a plugin can prepend contradictory delegation/search commands
    after the runtime has already validated the worker policy.
    """
    if agent not in BOUNDED_OPENCODE_AGENTS:
        return prompt
    return f"<system-reminder>\n{prompt}\n</system-reminder>"


def auth_headers_from_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    env = env if env is not None else os.environ
    user = env.get("OPENCODE_SERVER_USERNAME") or env.get("AA_OPENCODE_USERNAME")
    password = env.get("OPENCODE_SERVER_PASSWORD") or env.get("AA_OPENCODE_PASSWORD")
    if not user or not password:
        return {}
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _packaged_bounded_agent_contracts() -> dict[str, _PackagedAgentContract]:
    contracts: dict[str, _PackagedAgentContract] = {}
    repair = "repair the packaged agents, refresh agents, and restart OpenCode"
    for name in BOUNDED_OPENCODE_AGENTS:
        raw = resources.read_text("opencode", "agents", f"{name}.md")
        frontmatter = _SKILL_FRONTMATTER_RE.match(raw)
        if frontmatter is None:
            raise _OpenCodeCallError("internal", f"packaged agent {name!r} has invalid frontmatter; {repair}")
        try:
            metadata = yaml.load(
                frontmatter.group(1) + "\n",
                Loader=_NoDuplicateKeySafeLoader,
            )
        except yaml.YAMLError as exc:
            raise _OpenCodeCallError(
                "internal",
                f"packaged agent {name!r} has invalid YAML; {repair}",
            ) from exc
        if not isinstance(metadata, Mapping) or metadata.get("name") != name:
            raise _OpenCodeCallError(
                "internal",
                f"packaged agent {name!r} has invalid name metadata; {repair}",
            )
        description = metadata.get("description")
        mode = metadata.get("mode")
        tools = metadata.get("tools")
        permissions = metadata.get("permission")
        prompt = raw[frontmatter.end() :].rstrip("\r\n")
        if not isinstance(description, str) or not description.strip():
            raise _OpenCodeCallError(
                "internal",
                f"packaged agent {name!r} has invalid description; {repair}",
            )
        if not isinstance(mode, str) or not mode.strip():
            raise _OpenCodeCallError("internal", f"packaged agent {name!r} has invalid mode; {repair}")
        if not prompt:
            raise _OpenCodeCallError("internal", f"packaged agent {name!r} has empty prompt; {repair}")
        if not isinstance(tools, Mapping) or any(
            not isinstance(tool, str)
            or not isinstance(enabled, bool)
            or (enabled is True and tool not in BOUNDED_REQUIRED_TOOLS)
            for tool, enabled in tools.items()
        ):
            raise _OpenCodeCallError(
                "internal",
                f"packaged agent {name!r} has invalid tools policy; {repair}",
            )
        if not isinstance(permissions, Mapping):
            raise _OpenCodeCallError(
                "internal",
                f"packaged agent {name!r} has invalid permission policy; {repair}",
            )
        permission_rules: list[tuple[str, str, str]] = []
        for permission, policy in permissions.items():
            if not isinstance(permission, str):
                raise _OpenCodeCallError(
                    "internal",
                    f"packaged agent {name!r} has invalid permission name; {repair}",
                )
            if isinstance(policy, str):
                permission_rules.append((permission, "*", policy))
                continue
            if not isinstance(policy, Mapping):
                raise _OpenCodeCallError(
                    "internal",
                    f"packaged agent {name!r} has invalid permission policy; {repair}",
                )
            for pattern, action in policy.items():
                if not isinstance(pattern, str) or not isinstance(action, str):
                    raise _OpenCodeCallError(
                        "internal",
                        f"packaged agent {name!r} has invalid permission rule; {repair}",
                    )
                permission_rules.append((permission, pattern, action))
        contracts[name] = _PackagedAgentContract(
            name=name,
            # Match OpenCode's YAML parser exactly: folded block scalars using
            # clip chomping retain one trailing newline in live /agent data.
            description=description,
            mode=mode,
            prompt=prompt,
            disabled_tools=tuple(tool for tool, enabled in tools.items() if enabled is False),
            enabled_tools=tuple(tool for tool, enabled in tools.items() if enabled is True),
            permission_rules=tuple(permission_rules),
        )
    return contracts


def _live_permission_rules(agent_name: str, raw: Any) -> list[tuple[str, str, str]]:
    restart = "refresh agents and restart OpenCode"
    if not isinstance(raw, list):
        raise _OpenCodeCallError(
            "internal",
            f"bounded OpenCode agent {agent_name!r} has malformed permission policy; {restart}",
        )
    rules: list[tuple[str, str, str]] = []
    for index, rule in enumerate(raw):
        if not isinstance(rule, Mapping):
            raise _OpenCodeCallError(
                "internal",
                f"bounded OpenCode agent {agent_name!r} has malformed permission rule {index}; {restart}",
            )
        permission = rule.get("permission")
        pattern = rule.get("pattern", "*")
        action = rule.get("action")
        if not isinstance(permission, str) or not isinstance(pattern, str) or not isinstance(action, str):
            raise _OpenCodeCallError(
                "internal",
                f"bounded OpenCode agent {agent_name!r} has malformed permission rule {index}; {restart}",
            )
        rules.append((permission, pattern, action))
    return rules


def _contiguous_rule_index(
    live: list[tuple[str, str, str]],
    expected: tuple[tuple[str, str, str], ...],
) -> int | None:
    if not expected:
        return 0
    for start in range(len(live) - len(expected) + 1):
        if tuple(live[start : start + len(expected)]) == expected:
            return start
    return None


def validate_bounded_agent_catalog(payload: Any, agent_names: tuple[str, ...]) -> None:
    """Fail closed unless live workers match packaged selection and safety policy.

    Project agent files can be refreshed while a long-running OpenCode server
    still serves cached or globally shadowed definitions.  The live ``/agent``
    catalog is therefore the authority for this preflight, not the files on
    disk.  The denied set covers child-session delegation plus plugin tools that
    can execute code or MCP calls outside the worker's permission floor.
    """
    if not isinstance(payload, list):
        raise _OpenCodeCallError("internal", "opencode /agent returned a non-list payload")
    by_name: dict[str, list[Mapping[str, Any]]] = {}
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            raise _OpenCodeCallError(
                "internal",
                f"opencode /agent returned malformed entry at index {index}; restart OpenCode",
            )
        name = item.get("name")
        if not isinstance(name, str) or not name or name != name.strip():
            raise _OpenCodeCallError(
                "internal",
                f"opencode /agent returned malformed name at index {index}; restart OpenCode",
            )
        by_name.setdefault(name.casefold(), []).append(item)
    contracts = _packaged_bounded_agent_contracts()
    for agent_name in agent_names:
        matches = by_name.get(agent_name.casefold(), [])
        if not matches:
            raise _OpenCodeCallError(
                "internal",
                f"bounded OpenCode agent {agent_name!r} is missing; refresh agents and restart OpenCode",
            )
        if len(matches) != 1:
            raise _OpenCodeCallError(
                "internal",
                f"duplicate bounded OpenCode agent {agent_name!r}; refresh agents and restart OpenCode",
            )
        agent = matches[0]
        if agent.get("name") != agent_name:
            raise _OpenCodeCallError(
                "internal",
                f"bounded OpenCode agent {agent_name!r} has mismatched name; refresh agents and restart OpenCode",
            )
        contract = contracts[agent_name]
        for field, expected in (
            ("description", contract.description),
            ("mode", contract.mode),
            ("prompt", contract.prompt),
        ):
            actual = agent.get(field)
            if field == "prompt" and isinstance(actual, str):
                actual = actual.rstrip("\r\n")
            if actual != expected:
                raise _OpenCodeCallError(
                    "internal",
                    f"bounded OpenCode agent {agent_name!r} {field} mismatch; "
                    "refresh agents and restart OpenCode",
                )
        tools = agent.get("tools")
        live_rules = _live_permission_rules(agent_name, agent.get("permission"))

        def explicitly_disabled(tool: str) -> bool:
            if isinstance(tools, dict) and tools.get(tool) is False:
                return True
            action: str | None = None
            for permission, pattern, candidate in live_rules:
                if permission == tool and pattern == "*":
                    action = candidate
            return action == "deny"

        def explicitly_enabled(tool: str) -> bool:
            if isinstance(tools, dict) and tools.get(tool) is True:
                return True
            action: str | None = None
            for permission, pattern, candidate in live_rules:
                if permission == tool and pattern == "*":
                    action = candidate
            return action == "allow"

        unsafe = [tool for tool in contract.disabled_tools if not explicitly_disabled(tool)]
        if unsafe:
            joined = ", ".join(unsafe)
            raise _OpenCodeCallError(
                "internal",
                f"bounded OpenCode agent {agent_name!r} has stale/unsafe tools ({joined}); "
                "refresh agents and restart OpenCode before running the workflow",
            )
        missing = [tool for tool in contract.enabled_tools if not explicitly_enabled(tool)]
        if missing:
            joined = ", ".join(missing)
            raise _OpenCodeCallError(
                "internal",
                f"bounded OpenCode agent {agent_name!r} has stale/missing required tools ({joined}); "
                "refresh agents and restart OpenCode before running the workflow",
            )

        declared_start = _contiguous_rule_index(live_rules, contract.permission_rules)
        if declared_start is None:
            raise _OpenCodeCallError(
                "internal",
                f"bounded OpenCode agent {agent_name!r} permission policy mismatch; "
                "refresh agents and restart OpenCode",
            )
        protected_permissions = {
            *contract.disabled_tools,
            *contract.enabled_tools,
            *(permission for permission, _pattern, _action in contract.permission_rules),
        }
        declared_end = declared_start + len(contract.permission_rules)
        for permission, pattern, _action in live_rules[declared_end:]:
            # OpenCode may append a per-tool-output external-directory grant
            # after resolving an agent. It does not weaken the plugin's
            # session-root boundary and is distinct from a wildcard override.
            if permission == "external_directory" and pattern not in {"*", "**"}:
                continue
            if permission in protected_permissions:
                raise _OpenCodeCallError(
                    "internal",
                    f"bounded OpenCode agent {agent_name!r} has extra unsafe permission rule; "
                    "refresh agents and restart OpenCode",
                )


def validate_bounded_agent_server(server: str, directory: str) -> None:
    """Validate all packaged worker policies against a live OpenCode server."""
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(
                f"{server.rstrip('/')}/agent",
                params={"directory": directory},
                headers=auth_headers_from_env(),
            )
    except httpx.TransportError as exc:
        raise _OpenCodeCallError("transport", f"opencode agent preflight failed: {exc}") from exc
    if not 200 <= response.status_code < 300:
        raise _OpenCodeCallError(
            _classify_http_status(response.status_code),
            f"opencode agent preflight failed ({response.status_code}): {response.text[:300]}",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise _OpenCodeCallError("internal", "opencode /agent returned invalid JSON") from exc
    validate_bounded_agent_catalog(payload, BOUNDED_OPENCODE_AGENTS)


def _packaged_namespaced_skill_contracts() -> dict[str, _PackagedSkillContract]:
    expected: dict[str, _PackagedSkillContract] = {}
    for name in resources.iter_children("skills"):
        if not name.startswith("aa-"):
            continue
        if not resources.exists("skills", name, "SKILL.md"):
            raise _OpenCodeCallError(
                "internal",
                f"packaged skill {name!r} has no SKILL.md; restart OpenCode after repairing the "
                "assurance-agent installation",
            )
        raw = resources.read_text("skills", name, "SKILL.md")
        frontmatter = _SKILL_FRONTMATTER_RE.match(raw)
        if frontmatter is None:
            raise _OpenCodeCallError(
                "internal",
                f"packaged skill {name!r} has invalid frontmatter; restart OpenCode after repairing "
                "the assurance-agent installation",
            )
        try:
            metadata = yaml.load(
                # Include the line break immediately before the closing
                # delimiter. YAML's default (clip) chomping for ``>``/``|``
                # depends on it, and OpenCode's frontmatter parser preserves
                # that trailing newline in the rendered metadata value.
                frontmatter.group(1) + "\n",
                Loader=_NoDuplicateKeySafeLoader,
            )
        except yaml.YAMLError as exc:
            raise _OpenCodeCallError(
                "internal",
                f"packaged skill {name!r} has invalid YAML; restart OpenCode after repairing "
                "the assurance-agent installation",
            ) from exc
        if not isinstance(metadata, Mapping):
            raise _OpenCodeCallError(
                "internal",
                f"packaged skill {name!r} frontmatter is not a mapping; restart OpenCode after "
                "repairing the assurance-agent installation",
            )
        declared_name = metadata.get("name")
        if declared_name != name:
            raise _OpenCodeCallError(
                "internal",
                f"packaged skill {name!r} name {declared_name!r} does not match its directory; "
                "restart OpenCode after repairing the assurance-agent installation",
            )
        description = metadata.get("description")
        if not isinstance(description, str) or not description.strip():
            raise _OpenCodeCallError(
                "internal",
                f"packaged skill {name!r} has invalid description; restart OpenCode after "
                "repairing the assurance-agent installation",
            )
        # OpenCode's /skill endpoint returns gray-matter's parsed ``content``
        # field, i.e. the exact body after the closing frontmatter delimiter.
        content = raw[frontmatter.end() :]
        if not content.strip():
            raise _OpenCodeCallError(
                "internal",
                f"packaged skill {name!r} has empty body; restart OpenCode after repairing the "
                "assurance-agent installation",
            )
        expected[name] = _PackagedSkillContract(
            name=name,
            description=description,
            content=content,
        )
    return expected


def validate_packaged_skill_catalog(payload: Any) -> None:
    """Compare the live ``/skill`` catalog to packaged namespaced skill bytes."""

    restart = "restart OpenCode so it reloads the synchronized AA skills"
    if not isinstance(payload, list):
        raise _OpenCodeCallError(
            "internal",
            f"opencode /skill returned a non-list payload; {restart}",
        )

    by_casefolded_name: dict[str, list[Mapping[str, Any]]] = {}
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            raise _OpenCodeCallError(
                "internal",
                f"opencode /skill returned malformed entry at index {index}; {restart}",
            )
        name = item.get("name")
        if not isinstance(name, str) or not name or name != name.strip():
            raise _OpenCodeCallError(
                "internal",
                f"opencode /skill returned malformed name at index {index}; {restart}",
            )
        by_casefolded_name.setdefault(name.casefold(), []).append(item)

    contracts = _packaged_namespaced_skill_contracts()
    expected_names = {name.casefold() for name in contracts}
    for folded_name, items in by_casefolded_name.items():
        live_name = items[0].get("name")
        if folded_name.startswith("aa-") and folded_name not in expected_names:
            raise _OpenCodeCallError(
                "internal",
                f"unexpected live OpenCode AA skill {live_name!r}; {restart}",
            )

    for expected_name, contract in contracts.items():
        matches = by_casefolded_name.get(expected_name.casefold(), [])
        if not matches:
            raise _OpenCodeCallError(
                "internal",
                f"live OpenCode skill {expected_name!r} is missing; {restart}",
            )
        if len(matches) != 1:
            raise _OpenCodeCallError(
                "internal",
                f"duplicate live OpenCode skill {expected_name!r}; {restart}",
            )
        item = matches[0]
        live_name = item.get("name")
        if live_name != expected_name:
            raise _OpenCodeCallError(
                "internal",
                f"live OpenCode skill {expected_name!r} has mismatched name {live_name!r}; {restart}",
            )
        live_description = item.get("description")
        if live_description != contract.description:
            raise _OpenCodeCallError(
                "internal",
                f"live OpenCode skill {expected_name!r} description mismatch; {restart}",
            )
        live_content = item.get("content")
        if not isinstance(live_content, str):
            raise _OpenCodeCallError(
                "internal",
                f"live OpenCode skill {expected_name!r} has invalid content; {restart}",
            )
        expected_digest = hashlib.sha256(contract.content.encode("utf-8")).hexdigest()
        live_digest = hashlib.sha256(live_content.encode("utf-8")).hexdigest()
        if live_digest != expected_digest:
            raise _OpenCodeCallError(
                "internal",
                f"live OpenCode skill {expected_name!r} sha256 mismatch "
                f"(expected {expected_digest}, actual {live_digest}); {restart}",
            )


def _packaged_boundary_probe_tool() -> str:
    plugin = resources.read_bytes("opencode", "plugins", "aa.mjs")
    digest = hashlib.sha256(plugin).hexdigest()
    return f"{_BOUNDARY_PROBE_PREFIX}{digest}"


def _validate_live_boundary_probe(payload: Any) -> None:
    restart = "restart OpenCode so it reloads the synchronized AA boundary plugin"
    if not isinstance(payload, list) or any(not isinstance(item, str) for item in payload):
        raise _OpenCodeCallError(
            "internal",
            f"opencode live tool catalog is malformed; {restart}",
        )
    expected = _packaged_boundary_probe_tool()
    markers = [item for item in payload if item.startswith(_BOUNDARY_PROBE_PREFIX)]
    if markers != [expected]:
        raise _OpenCodeCallError(
            "internal",
            "live AA boundary plugin does not match the packaged plugin bytes "
            f"(expected {expected!r}, actual {markers!r}); {restart}",
        )


def validate_packaged_skill_server(server: str, directory: str) -> None:
    """Fail closed unless OMO resolves from this SUT and serves exact AA runtime bytes.

    OMO's replacement ``skill`` tool discovers project skills from the server
    process working directory, not from OpenCode's per-request ``directory``
    query.  Validate the unqualified ``/path`` first so a shared server cannot
    make ``/skill?directory=<sut>`` look healthy while OMO consumes a different
    project catalog at execution time.  A SHA-256-named live tool also proves
    that the running server loaded the exact packaged boundary plugin, rather
    than merely observing a refreshed copy on disk.
    """

    restart = "restart OpenCode so it reloads the synchronized AA skills"
    try:
        with httpx.Client(timeout=30.0) as client:
            path_response = client.get(
                f"{server.rstrip('/')}/path",
                headers=auth_headers_from_env(),
            )
            if not 200 <= path_response.status_code < 300:
                raise _OpenCodeCallError(
                    _classify_http_status(path_response.status_code),
                    "opencode server working directory preflight failed "
                    f"({path_response.status_code}): {path_response.text[:300]}; {restart}",
                )
            try:
                path_payload = path_response.json()
            except ValueError as exc:
                raise _OpenCodeCallError(
                    "internal",
                    f"opencode /path returned invalid JSON; {restart}",
                ) from exc
            if not isinstance(path_payload, Mapping):
                raise _OpenCodeCallError(
                    "internal",
                    f"opencode /path returned a non-mapping payload; {restart}",
                )
            live_directory = path_payload.get("directory")
            if (
                not isinstance(live_directory, str)
                or not live_directory
                or live_directory != live_directory.strip()
            ):
                raise _OpenCodeCallError(
                    "internal",
                    f"opencode /path returned an invalid server working directory; {restart}",
                )
            try:
                actual_root = Path(live_directory).expanduser().resolve(strict=False)
                expected_root = Path(directory).expanduser().resolve(strict=False)
            except OSError as exc:
                raise _OpenCodeCallError(
                    "internal",
                    f"cannot canonicalize OpenCode server working directory: {exc}; {restart}",
                ) from exc
            if actual_root != expected_root:
                raise _OpenCodeCallError(
                    "internal",
                    f"OpenCode/OMO server working directory {actual_root} does not match "
                    f"the benchmark project {expected_root}; {restart} from {expected_root}",
                )

            probe_response = client.get(
                f"{server.rstrip('/')}/experimental/tool/ids",
                params={"directory": directory},
                headers=auth_headers_from_env(),
            )
            if not 200 <= probe_response.status_code < 300:
                raise _OpenCodeCallError(
                    _classify_http_status(probe_response.status_code),
                    "opencode live AA boundary plugin preflight failed "
                    f"({probe_response.status_code}): {probe_response.text[:300]}; {restart}",
                )
            try:
                probe_payload = probe_response.json()
            except ValueError as exc:
                raise _OpenCodeCallError(
                    "internal",
                    f"opencode live tool catalog returned invalid JSON; {restart}",
                ) from exc
            _validate_live_boundary_probe(probe_payload)

            response = client.get(
                f"{server.rstrip('/')}/skill",
                params={"directory": directory},
                headers=auth_headers_from_env(),
            )
    except httpx.TransportError as exc:
        raise _OpenCodeCallError(
            "transport",
            f"opencode skill preflight failed: {exc}; {restart}",
        ) from exc
    if not 200 <= response.status_code < 300:
        raise _OpenCodeCallError(
            _classify_http_status(response.status_code),
            f"opencode skill preflight failed ({response.status_code}): {response.text[:300]}; {restart}",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise _OpenCodeCallError(
            "internal",
            f"opencode /skill returned invalid JSON; {restart}",
        ) from exc
    validate_packaged_skill_catalog(payload)


def parse_model(raw: str | dict[str, str] | None) -> dict[str, str] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    text = raw.strip()
    if not text:
        return None
    slash = text.find("/")
    if slash <= 0 or slash >= len(text) - 1:
        raise DriverError(f'--model must be "provider/model" (got "{raw}")')
    return {"providerID": text[:slash], "modelID": text[slash + 1 :]}


def _optional_non_negative_int(raw: Mapping[str, Any], field: str) -> int | None:
    value = raw.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _OpenCodeCallError("internal", f"opencode retry status has invalid {field}")
    return value


def _retry_action(raw: Any) -> _RetryAction | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise _OpenCodeCallError("internal", "opencode retry status has invalid action")

    def optional_string(field: str) -> str | None:
        value = raw.get(field)
        if value is not None and not isinstance(value, str):
            raise _OpenCodeCallError(
                "internal",
                f"opencode retry status has invalid action {field}",
            )
        return value

    return _RetryAction(
        reason=optional_string("reason"),
        provider=optional_string("provider"),
        title=optional_string("title"),
        message=optional_string("message"),
        label=optional_string("label"),
        link=optional_string("link"),
    )


def _parse_session_status(payload: Any, session_id: str) -> _SessionStatusInfo:
    if not isinstance(payload, Mapping):
        raise _OpenCodeCallError("internal", "opencode status returned a non-mapping payload")
    raw = payload.get(session_id)
    if isinstance(raw, str):
        value = raw
        if value in ("busy", "retry", "idle"):
            return _SessionStatusInfo(type=value)  # type: ignore[arg-type]
        raise _OpenCodeCallError("internal", f"opencode returned invalid session status {value!r}")
    if isinstance(raw, Mapping):
        value = raw.get("type")
        if value == "idle":
            return _SessionStatusInfo(type="idle")
        if value == "busy":
            return _SessionStatusInfo(type="busy")
        if value != "retry":
            raise _OpenCodeCallError("internal", f"opencode returned invalid session status {value!r}")
        message = raw.get("message")
        if message is not None and not isinstance(message, str):
            raise _OpenCodeCallError("internal", "opencode retry status has invalid message")
        return _SessionStatusInfo(
            type="retry",
            attempt=_optional_non_negative_int(raw, "attempt"),
            message=message,
            action=_retry_action(raw.get("action")),
            next=_optional_non_negative_int(raw, "next"),
        )
    if raw is None:
        return _SessionStatusInfo(type="idle")
    raise _OpenCodeCallError("internal", "opencode returned invalid session status payload")


def _session_status(payload: Any, session_id: str) -> SessionStatus:
    return _parse_session_status(payload, session_id).type


def _account_rate_limit_detail(status: _SessionStatusInfo) -> str | None:
    if status.type != "retry":
        return None
    if (
        status.action is not None
        and status.action.reason is not None
        and status.action.reason.casefold() == "account_rate_limit"
    ):
        return status.message or status.action.message or "action.reason=account_rate_limit"
    message = status.message or ""
    folded = message.casefold()
    compact = "".join(character for character in folded if character.isalnum())
    if "accountquotaexceeded" in compact or "too many requests" in folded:
        return message
    if (
        status.attempt is not None
        and status.attempt >= MAX_PROVIDER_OVERLOAD_RETRY_ATTEMPTS
        and ("overload" in folded or "try again later" in folded)
    ):
        return f"provider retry budget exhausted after attempt {status.attempt}: {message}"
    return None


def _accepts_timeout_keyword(call: Callable[..., Any]) -> bool:
    try:
        parameters = inspect.signature(call).parameters
    except (TypeError, ValueError):
        return True
    return "timeout" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


def _normalize_exception(exc: Exception) -> tuple[ErrorKind, str]:
    if isinstance(exc, _OpenCodeCallError):
        return exc.kind, str(exc)
    if isinstance(exc, DriverError):
        return "invalid_input", str(exc)
    if isinstance(exc, httpx.TransportError):
        return "transport", f"opencode transport error: {exc}"
    return "internal", f"opencode internal error: {exc}"


class OpenCodeAdapter:
    def __init__(
        self,
        server: str,
        directory: str,
        *,
        model: str | dict[str, str] | None = None,
        parent_session: str | None = None,
        auth_headers: dict[str, str] | None = None,
        client: httpx.Client | None = None,
        timeout: float = 3600.0,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        poll_max_s: float = DEFAULT_POLL_MAX_S,
        idle_done_streak: int = DEFAULT_IDLE_DONE_STREAK,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base = server.rstrip("/")
        self._directory = directory
        self._model = parse_model(model)
        configured_variant = os.environ.get(OPENCODE_VARIANT_ENV, "").strip()
        if configured_variant and re.fullmatch(r"[A-Za-z0-9_-]+", configured_variant) is None:
            raise DriverError(f"{OPENCODE_VARIANT_ENV} contains an invalid variant name")
        self._variant = configured_variant or None
        self._parent = parent_session
        self._headers = auth_headers if auth_headers is not None else auth_headers_from_env()
        self._client = client or httpx.Client(timeout=timeout)
        self._fallback_request_timeout = httpx.Timeout(timeout)
        self._client_accepts_timeout = _accepts_timeout_keyword(self._client.request)
        self._poll_interval = poll_interval_s
        self._poll_max = poll_max_s
        self._idle_streak_target = idle_done_streak
        self._sleep = sleep
        self._monotonic = monotonic
        self._validated_agent_policies: set[tuple[str, str]] = set()

    def _request(
        self,
        method: str,
        path: str,
        json: Any | None = None,
        *,
        directory: str | None = None,
        timeout_s: float | None = None,
    ) -> httpx.Response:
        kwargs: dict[str, Any] = {
            "params": {"directory": directory if directory is not None else self._directory},
            "json": json,
            "headers": self._headers,
        }
        if timeout_s is not None and self._client_accepts_timeout:
            configured = getattr(self._client, "timeout", self._fallback_request_timeout)
            if not isinstance(configured, httpx.Timeout):
                configured = httpx.Timeout(configured)

            def bounded(component: float | None) -> float:
                return timeout_s if component is None else min(component, timeout_s)

            kwargs["timeout"] = httpx.Timeout(
                connect=bounded(configured.connect),
                read=bounded(configured.read),
                write=bounded(configured.write),
                pool=bounded(configured.pool),
            )
        return self._client.request(method, f"{self._base}{path}", **kwargs)

    def _create_session(
        self,
        title: str,
        parent: str | None,
        *,
        directory: str | None = None,
    ) -> str:
        body: dict[str, Any] = {"title": title}
        if parent:
            body["parentID"] = parent
        resp = self._request("POST", "/session", json=body, directory=directory)
        if not 200 <= resp.status_code < 300:
            raise _OpenCodeCallError(
                _classify_http_status(resp.status_code),
                f"opencode create session failed ({resp.status_code}): {resp.text[:300]}",
            )
        session_id = (resp.json() or {}).get("id")
        if not session_id:
            raise _OpenCodeCallError("internal", "opencode create session: missing id")
        return session_id

    def _validate_live_agent_policy(self, agent: str | None, *, directory: str) -> None:
        if agent not in BOUNDED_OPENCODE_AGENTS:
            return
        key = (directory, agent)
        if key in self._validated_agent_policies:
            return
        resp = self._request("GET", "/agent", directory=directory)
        if not 200 <= resp.status_code < 300:
            raise _OpenCodeCallError(
                _classify_http_status(resp.status_code),
                f"opencode agent preflight failed ({resp.status_code}): {resp.text[:300]}",
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise _OpenCodeCallError("internal", "opencode /agent returned invalid JSON") from exc
        validate_bounded_agent_catalog(payload, (agent,))
        self._validated_agent_policies.add(key)

    def _dispatch_prompt(
        self,
        session_id: str,
        prompt: str,
        *,
        agent: str | None = None,
        model: dict[str, str] | None = None,
        directory: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"parts": [{"type": "text", "text": _shield_bounded_prompt(prompt, agent)}]}
        if agent in BOUNDED_OPENCODE_AGENTS:
            # OpenCode may retain globally registered tools in the model-facing
            # prompt even when agent/config permissions deny them.  The
            # message-level override is the final authority used when building
            # this provider request.
            body["tools"] = dict(BOUNDED_PROMPT_TOOL_OVERRIDES)
        if model is not None:
            body["model"] = model
        if self._variant is not None:
            body["variant"] = self._variant
        if agent:
            body["agent"] = agent
        resp = self._request("POST", f"/session/{session_id}/prompt_async", json=body, directory=directory)
        if resp.status_code != 204 and not 200 <= resp.status_code < 300:
            raise _OpenCodeCallError(
                _classify_prompt_status(
                    resp.status_code,
                    model_was_explicit=model is not None,
                ),
                f"opencode prompt failed ({resp.status_code}): {resp.text[:300]}",
            )

    def _get_status_info(
        self,
        session_id: str,
        *,
        directory: str | None = None,
        timeout_s: float | None = None,
    ) -> _SessionStatusInfo:
        resp = self._request(
            "GET",
            "/session/status",
            directory=directory,
            timeout_s=timeout_s,
        )
        if not 200 <= resp.status_code < 300:
            raise _OpenCodeCallError(
                _classify_http_status(resp.status_code),
                f"opencode status failed ({resp.status_code}): {resp.text[:300]}",
            )
        try:
            payload = resp.json() if resp.content else {}
        except ValueError as exc:
            raise _OpenCodeCallError("internal", "opencode status returned invalid JSON") from exc
        return _parse_session_status(payload, session_id)

    def get_status(self, session_id: str, *, directory: str | None = None) -> SessionStatus:
        return self._get_status_info(session_id, directory=directory).type

    def _await_idle(
        self,
        session_id: str,
        *,
        directory: str | None = None,
        poll_max: float | None = None,
    ) -> None:
        limit = poll_max if poll_max is not None else self._poll_max
        deadline = self._monotonic() + limit
        saw_busy = False
        idle_streak = 0
        # Grace period so the session can flip to busy before we trust "idle".
        grace_remaining = deadline - self._monotonic()
        if grace_remaining > 0:
            self._sleep(min(self._poll_interval, 1.5, grace_remaining))
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            status = self._get_status_info(
                session_id,
                directory=directory,
                timeout_s=remaining,
            )
            rate_limit_detail = _account_rate_limit_detail(status)
            if rate_limit_detail is not None:
                attempt = f" on attempt {status.attempt}" if status.attempt is not None else ""
                raise _OpenCodeCallError(
                    "rate_limit",
                    f"opencode account rate limit{attempt}: {rate_limit_detail[:500]}",
                )
            if status.type in ("busy", "retry"):
                saw_busy = True
                idle_streak = 0
            else:
                idle_streak += 1
                # Never-busy sessions may be slow to start; require a longer streak.
                need = self._idle_streak_target if saw_busy else self._idle_streak_target + 4
                if idle_streak >= need:
                    return
            sleep_remaining = deadline - self._monotonic()
            if sleep_remaining <= 0:
                break
            self._sleep(min(self._poll_interval, sleep_remaining))
        raise _OpenCodeCallError("timeout", f"opencode phase timed out after {limit}s (session {session_id})")

    def _abort_session(self, session_id: str, *, directory: str | None = None) -> None:
        resp = self._request(
            "POST",
            f"/session/{session_id}/abort",
            directory=directory,
            timeout_s=DEFAULT_ABORT_TIMEOUT_S,
        )
        if not 200 <= resp.status_code < 300:
            raise _OpenCodeCallError(
                "internal",
                f"opencode abort failed ({resp.status_code}): {resp.text[:300]}",
            )

    def _abort_before_failure(
        self,
        session_id: str,
        *,
        original_kind: ErrorKind,
        original_error: str,
        directory: str | None = None,
    ) -> tuple[ErrorKind, str]:
        try:
            self._abort_session(session_id, directory=directory)
        except Exception as abort_exc:
            return (
                "internal",
                f"{original_error}; session cleanup failed: {abort_exc}",
            )
        return original_kind, original_error

    def _abort_before_cancellation(
        self,
        session_id: str,
        cancellation: BaseException,
        *,
        directory: str | None = None,
    ) -> None:
        try:
            self._abort_session(session_id, directory=directory)
        except Exception as abort_exc:
            cancellation.add_note(f"OpenCode session cleanup failed: {abort_exc}")

    def _raise_on_session_error(
        self,
        session_id: str,
        *,
        model_was_explicit: bool,
        directory: str | None = None,
    ) -> None:
        """Surface the terminal assistant API error hidden behind an idle session.

        ``prompt_async`` returns before the provider call.  Provider failures are
        therefore recorded on the assistant message while ``/session/status``
        simply becomes idle; treating idle as success turns auth/rate-limit errors
        into misleading artifact-missing failures downstream.
        """
        resp = self._request("GET", f"/session/{session_id}/message", directory=directory)
        if not 200 <= resp.status_code < 300:
            raise _OpenCodeCallError(
                _classify_http_status(resp.status_code),
                f"opencode messages failed ({resp.status_code}): {resp.text[:300]}",
            )
        try:
            payload = resp.json() if resp.content else []
        except ValueError as exc:
            raise _OpenCodeCallError("internal", "opencode messages returned invalid JSON") from exc
        if not isinstance(payload, list):
            raise _OpenCodeCallError("internal", "opencode messages returned a non-list payload")
        for message in reversed(payload):
            if not isinstance(message, dict):
                continue
            info = message.get("info")
            if not isinstance(info, dict) or info.get("role") != "assistant":
                continue
            error = info.get("error")
            if error is None:
                return
            error_data = error.get("data") if isinstance(error, dict) else None
            status_code = error_data.get("statusCode") if isinstance(error_data, dict) else None
            kind = (
                _classify_prompt_status(
                    status_code,
                    model_was_explicit=model_was_explicit,
                )
                if isinstance(status_code, int)
                else "internal"
            )
            detail = error_data.get("message") if isinstance(error_data, dict) else None
            if not isinstance(detail, str) or not detail.strip():
                detail = str(error)[:500]
            raise _OpenCodeCallError(kind, f"opencode assistant error: {detail[:500]}")

    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        session_id: str | None = None
        try:
            session_id = self._create_session(f"Phase {request.phase_id}", self._parent)
            self._dispatch_prompt(
                session_id,
                request.prompt,
                agent=request.agent,
                model=self._model,
            )
            self._await_idle(session_id)
            self._raise_on_session_error(
                session_id,
                model_was_explicit=self._model is not None,
            )
        except Exception as err:
            kind, error = _normalize_exception(err)
            if session_id is not None:
                _, error = self._abort_before_failure(
                    session_id,
                    original_kind=kind,
                    original_error=error,
                )
            return PhaseResult(ok=False, output="", error=error)
        except BaseException as cancellation:
            if session_id is not None:
                self._abort_before_cancellation(session_id, cancellation)
            raise
        # Output is written to artifacts by the agent; GraphRuntime commits the
        # attempt outcome and routes gates from the ledger projection.
        return PhaseResult(ok=True, output="")

    def invoke(self, request: AgentRequest) -> AgentResult:
        """graph AgentInvoker：每个请求都以 task 私有 workspace root 为 directory。"""
        directory = str(request.workspace_root)
        session_id: str | None = None
        try:
            self._validate_live_agent_policy(request.agent, directory=directory)
            session_id = self._create_session(
                f"Phase {request.node_id}",
                request.reconnect_session_id or self._parent,
                directory=directory,
            )
            self._dispatch_prompt(
                session_id,
                request.prompt,
                agent=request.agent,
                model=parse_model(request.model),
                directory=directory,
            )
            self._await_idle(session_id, directory=directory, poll_max=request.timeout_seconds)
            self._raise_on_session_error(
                session_id,
                model_was_explicit=request.model is not None,
                directory=directory,
            )
        except Exception as exc:
            kind, error = _normalize_exception(exc)
            if session_id is not None:
                kind, error = self._abort_before_failure(
                    session_id,
                    original_kind=kind,
                    original_error=error,
                    directory=directory,
                )
            return AgentResult(
                ok=False,
                error_kind=kind,
                error=error,
                session_id=session_id,
            )
        except BaseException as cancellation:
            if session_id is not None:
                self._abort_before_cancellation(
                    session_id,
                    cancellation,
                    directory=directory,
                )
            raise
        return AgentResult(ok=True, session_id=session_id)
