"""schema v2 静态编译器：拓扑、表达式、有界 cycle、route 与 canonical digest。

加载期（schema_v2）只保证模型结构合法；编译期把 v1 driver 时代的隐式约束全部
显式化，且所有错误在执行前一次性收集报告（CompileError）：

- entrypoint / graph / node / gate / policy / budget 引用必须存在；
- graph/node ID 与 output 逻辑路径必须是安全标识；
- 所有 node 必须从 START 可达，且能到 END/STOP/FAIL/interrupt；
- condition/route/gate DSL 完成 parse、arity、identifier 与 reference 校验
  （表达式只允许 params、state、本图 artifact symbol 和已声明的 ``node('id')``）；
- route 对 gate verdict 必须 exhaustive，interrupt action 必须有完整 resume route；
- state writer 必须声明合法，同 step ``replace`` 多 writer 拒绝；
- subgraph 调用图不允许递归；
- 每个 cyclic SCC（Tarjan）必须含有限业务预算消费点，且 ``exhausted_to`` 离开 SCC；
- 提供 execution contract catalog 时：``uses`` 必须解析到 contract 且 handler 与
  前缀一致、agent node 的 target 必须是 ``skill:*``、显式 node resources 只能收窄
  contract 授权写范围、retry policy 的 ``retry_on`` 必须落在目标 contract 的
  ``retryable_errors`` 白名单内；同时计算各 graph 的保守资源 footprint 与
  workflow 引用到的 contract digests。

校验通过后生成冻结的 CompiledWorkflow：拓扑用 tuple 固化，digest 来自
canonical JSON（sort_keys + 紧凑分隔符）的 SHA-256，两次编译同一 schema 结果
逐字节稳定。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

from pydantic import BaseModel

from assurance_agent.exceptions import AaError
from assurance_agent.workflow.graph.contracts import (
    ContractError,
    ExecutionContract,
    ExecutionContractCatalog,
    ResourceClaims,
    ResourcePath,
    normalize_claim_pattern,
    path_covers,
    unknown_claims,
)
from assurance_agent.workflow.graph.models import (
    CompiledEntrypoint,
    CompiledExport,
    CompiledGraph,
    CompiledNode,
    CompiledWorkflow,
)
from assurance_agent.workflow.graph.ingest_catalog import validate_catalog_runtime
from assurance_agent.workflow.graph.schema_v2 import (
    GraphDef,
    NodeDef,
    ParamDef,
    StateDef,
    WorkflowSchemaV2,
)
from assurance_agent.workflow.orchestration.dsl import (
    BoolOp,
    Call,
    Compare,
    DslError,
    Expr,
    Ident,
    Literal,
    Member,
    Not,
    Subscript,
    parse_expression,
)
from assurance_agent.workflow.orchestration.schema import derive_alias


class CompileError(AaError):
    """workflow v2 编译期结构校验失败（所有错误一次性报告）。"""


_TERMINALS = frozenset({"END", "STOP", "FAIL"})
_RESERVED_IDS = frozenset({"START", "END", "STOP", "FAIL"})
_SAFE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_PATH_ROOTS = ("change:", "project:", "repo:")
_TEMPLATE = re.compile(r"\$\{([^}]+)\}")
_PREDICATE_BUILTINS = ("any", "all", "count")


def compile_workflow(
    schema: WorkflowSchemaV2,
    contracts: ExecutionContractCatalog | None = None,
) -> CompiledWorkflow:
    errors: list[str] = []
    errors.extend(_validate_params_and_entrypoints(schema))
    errors.extend(_validate_graph_refs(schema))
    errors.extend(_validate_expressions(schema))
    errors.extend(_validate_routes_and_interrupts(schema))
    errors.extend(_validate_state_writers(schema))
    errors.extend(_validate_subgraph_recursion(schema))
    errors.extend(_validate_bounded_sccs(schema))
    errors.extend(_validate_exports(schema))
    if contracts is not None:
        errors.extend(_validate_contract_usage(schema, contracts))
    if errors:
        raise CompileError("workflow v2 compile failed:\n  - " + "\n  - ".join(errors))
    footprints, node_claims = _graph_footprints(schema, contracts)
    graphs = {
        graph_id: _compile_graph(schema, graph_id, graph, footprints[graph_id], node_claims[graph_id])
        for graph_id, graph in schema.graphs.items()
    }
    canonical = schema.model_dump(mode="json", by_alias=True, exclude_none=True)
    catalog_digest = validate_catalog_runtime().digest
    return CompiledWorkflow(
        schema=schema,
        digest=canonical_digest(canonical),
        entrypoints=_compile_entrypoints(schema),
        graphs=graphs,
        contract_digests=_referenced_contract_digests(schema, contracts) if contracts is not None else {},
        ingest_catalog_digest=catalog_digest,
    )


def canonical_digest(value: BaseModel | Mapping[str, object]) -> str:
    """canonical JSON（sort_keys、紧凑分隔符、非 ASCII 不转义）的 SHA-256。"""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True, exclude_none=True)
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_params(schema: WorkflowSchemaV2, overrides: Mapping[str, object]) -> dict[str, object]:
    unknown = sorted(set(overrides) - set(schema.params))
    if unknown:
        raise CompileError(f"unknown params: {', '.join(unknown)}")
    resolved = {name: definition.default for name, definition in schema.params.items()}
    resolved.update(overrides)
    _validate_param_values(schema.params, resolved)
    mode = resolved.get("run_mode")
    test_types = resolved.get("test_types")
    if mode == "api-only" and isinstance(test_types, list) and "api" not in test_types:
        raise CompileError("api-only requires test_types to contain api")
    if mode == "e2e-only" and isinstance(test_types, list) and "e2e" not in test_types:
        raise CompileError("e2e-only requires test_types to contain e2e")
    return resolved


# ---------------------------------------------------------------------------
# params / entrypoints


def _validate_param_values(params: dict[str, ParamDef], resolved: Mapping[str, object]) -> None:
    errors: list[str] = []
    for name, definition in params.items():
        if name in resolved:
            errors.extend(_check_param_value(name, definition, resolved[name]))
    if errors:
        raise CompileError("; ".join(errors))


def _check_param_value(name: str, definition: ParamDef, value: object) -> list[str]:
    if definition.type == "enum":
        if definition.values and value not in definition.values:
            return [f"param '{name}' value {value!r} not in enum values {definition.values!r}"]
        return []
    if definition.type == "bool":
        if not isinstance(value, bool):
            return [f"param '{name}' value {value!r} must be bool"]
        return []
    if definition.type == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            return [f"param '{name}' value {value!r} must be int"]
        return []
    if definition.type == "str":
        if not isinstance(value, str):
            return [f"param '{name}' value {value!r} must be str"]
        return []
    # list
    if not isinstance(value, list):
        return [f"param '{name}' value {value!r} must be a list"]
    errors: list[str] = []
    if definition.values:
        bad = [item for item in value if item not in definition.values]
        if bad:
            errors.append(f"param '{name}' values {bad!r} not in allowed values {definition.values!r}")
    if definition.min_items is not None and len(value) < definition.min_items:
        errors.append(f"param '{name}' needs at least {definition.min_items} item(s)")
    if definition.unique and len({repr(item) for item in value}) != len(value):
        errors.append(f"param '{name}' values must be unique")
    return errors


def _validate_params_and_entrypoints(schema: WorkflowSchemaV2) -> list[str]:
    errors: list[str] = []
    for name, definition in schema.params.items():
        if definition.type == "enum" and not definition.values:
            errors.append(f"param '{name}' is enum but declares no values")
    # 默认 params 在编译期冻结校验；invocation override 由 resolve_params 把关。
    try:
        resolve_params(schema, {})
    except CompileError as exc:
        errors.append(str(exc))
    for name, entrypoint in schema.entrypoints.items():
        if entrypoint.graph not in schema.graphs:
            errors.append(f"entrypoint '{name}' references unknown graph '{entrypoint.graph}'")
        unknown = sorted(set(entrypoint.with_) - set(schema.params))
        if unknown:
            errors.append(f"entrypoint '{name}' overrides unknown params: {', '.join(unknown)}")
            continue
        resolved = {pname: definition.default for pname, definition in schema.params.items()}
        resolved.update(entrypoint.with_)
        try:
            _validate_param_values(schema.params, resolved)
        except CompileError as exc:
            errors.append(f"entrypoint '{name}': {exc}")
    return errors


# ---------------------------------------------------------------------------
# graph 引用与结构


def _node_gate_id(node: NodeDef) -> str | None:
    if node.gate is not None:
        return node.gate
    if node.uses == "builtin:gate":
        candidate = node.with_.get("gate")
        if isinstance(candidate, str):
            return candidate
    return None


def _check_uses(loc: str, node: NodeDef, schema: WorkflowSchemaV2) -> list[str]:
    errors: list[str] = []
    uses = node.uses
    prefix, sep, target = uses.partition(":")
    if not sep or not target:
        return [f"{loc} has malformed uses '{uses}'"]
    if prefix == "graph":
        if target not in schema.graphs:
            errors.append(f"{loc} uses unknown graph '{target}'")
    elif prefix == "builtin":
        if target not in ("join", "interrupt", "gate"):
            errors.append(f"{loc} uses unknown builtin '{uses}'")
    elif prefix not in ("skill", "operation"):
        errors.append(f"{loc} uses unknown prefix '{prefix}:'")
    if node.join is not None and uses != "builtin:join":
        errors.append(f"{loc} declares join but uses '{uses}' (join requires builtin:join)")
    if uses == "builtin:join" and node.join is None:
        errors.append(f"{loc} uses builtin:join but declares no join")
    if node.interrupt is not None and uses != "builtin:interrupt":
        errors.append(f"{loc} declares interrupt but uses '{uses}' (interrupt requires builtin:interrupt)")
    if uses == "builtin:interrupt" and node.interrupt is None:
        errors.append(f"{loc} uses builtin:interrupt but declares no interrupt")
    if (
        uses == "builtin:gate"
        and _node_gate_id(node) is None
        and not isinstance(node.with_.get("expression"), str)
    ):
        errors.append(f"{loc} uses builtin:gate but declares neither gate nor with.expression")
    return errors


def _check_output_path(
    loc: str, output: str, node: NodeDef, *, param_names: frozenset[str] = frozenset()
) -> list[str]:
    errors: list[str] = []
    if not output.startswith(_PATH_ROOTS):
        return [f"{loc} output '{output}' must be rooted in change:/project:/repo:"]
    allowed_vars = {"context.change_id"} | {f"params.{name}" for name in param_names}
    if node.fan_out is not None:
        allowed_vars.add(node.fan_out.item_as)
    for match in _TEMPLATE.finditer(output):
        if match.group(1) not in allowed_vars:
            errors.append(f"{loc} output '{output}' uses unknown template '${{{match.group(1)}}}'")
    concrete = _TEMPLATE.sub("item", output)
    _, _, rest = concrete.partition(":")
    segments = rest.split("/")
    body = segments[:-1] if segments and segments[-1] == "" else segments  # 目录 output 允许结尾 "/"
    if rest.startswith("/") or "\\" in rest or any(seg in ("", ".", "..") for seg in body):
        errors.append(f"{loc} output '{output}' has unsafe path")
    return errors


def _check_budget_limit(
    schema: WorkflowSchemaV2, graph_id: str, budget_id: str, limit: str | int
) -> list[str]:
    hint = "must be a non-negative int or an int param reference"
    if isinstance(limit, bool):
        return [f"graph '{graph_id}' budget '{budget_id}' limit {hint}"]
    if isinstance(limit, int):
        if limit < 0:
            return [f"graph '{graph_id}' budget '{budget_id}' limit must be non-negative"]
        return []
    try:
        expr = parse_expression(limit)
    except DslError as exc:
        return [f"graph '{graph_id}' budget '{budget_id}' limit: invalid expression — {exc}"]
    if isinstance(expr, Member) and isinstance(expr.obj, Ident) and expr.obj.name == "params":
        param = schema.params.get(expr.prop)
        if param is None:
            return [f"graph '{graph_id}' budget '{budget_id}' limit references unknown param '{expr.prop}'"]
        if param.type != "int":
            return [f"graph '{graph_id}' budget '{budget_id}' limit param '{expr.prop}' is not int"]
        return []
    return [f"graph '{graph_id}' budget '{budget_id}' limit {hint}"]


def _graph_adjacency(graph: GraphDef) -> dict[str, list[str]]:
    """node→node 有向边：edges + route targets + budget exhausted_to（都是真实控制流）。"""
    adj: dict[str, list[str]] = {nid: [] for nid in graph.nodes}
    for edge in graph.edges:
        if edge.from_ in adj and edge.to in adj:
            adj[edge.from_].append(edge.to)
    for route in graph.routes:
        if route.from_ in adj:
            targets = list(route.cases.values()) + ([route.default] if route.default is not None else [])
            adj[route.from_].extend(t for t in targets if t in adj)
    for nid, node in graph.nodes.items():
        if node.budget is not None and node.budget.exhausted_to in adj:
            adj[nid].append(node.budget.exhausted_to)
    return adj


def _check_reachability(graph_id: str, graph: GraphDef) -> list[str]:
    adj = _graph_adjacency(graph)
    reachable: set[str] = set()
    stack = [edge.to for edge in graph.edges if edge.from_ == "START" and edge.to in adj]
    while stack:
        nid = stack.pop()
        if nid in reachable:
            continue
        reachable.add(nid)
        stack.extend(adj[nid])
    errors = [
        f"graph '{graph_id}' node '{nid}' is unreachable from START"
        for nid in graph.nodes
        if nid not in reachable
    ]
    terminal_seeds = {nid for nid, node in graph.nodes.items() if node.interrupt is not None}
    for edge in graph.edges:
        if edge.from_ in graph.nodes and edge.to in _TERMINALS:
            terminal_seeds.add(edge.from_)
    for route in graph.routes:
        if route.from_ not in graph.nodes:
            continue
        targets = list(route.cases.values()) + ([route.default] if route.default is not None else [])
        if any(t in _TERMINALS for t in targets):
            terminal_seeds.add(route.from_)
    for nid, node in graph.nodes.items():
        if node.budget is not None and node.budget.exhausted_to in _TERMINALS:
            terminal_seeds.add(nid)
    reverse: dict[str, list[str]] = {nid: [] for nid in graph.nodes}
    for src, outs in adj.items():
        for dst in outs:
            reverse[dst].append(src)
    can_exit: set[str] = set()
    stack = list(terminal_seeds)
    while stack:
        nid = stack.pop()
        if nid in can_exit:
            continue
        can_exit.add(nid)
        stack.extend(reverse[nid])
    errors.extend(
        f"graph '{graph_id}' node '{nid}' cannot reach END/STOP/FAIL/interrupt"
        for nid in graph.nodes
        if nid not in can_exit
    )
    return errors


def _validate_graph_refs(schema: WorkflowSchemaV2) -> list[str]:
    errors: list[str] = []
    for name, policy in schema.policies.timeout.items():
        if policy.heartbeat_seconds >= policy.run_seconds:
            errors.append(f"timeout policy '{name}' heartbeat_seconds must be < run_seconds")
    for graph_id, graph in schema.graphs.items():
        if not _SAFE_ID.match(graph_id):
            errors.append(f"unsafe graph id '{graph_id}'")
        for nid, node in graph.nodes.items():
            loc = f"graph '{graph_id}' node '{nid}'"
            if not _SAFE_ID.match(nid):
                errors.append(f"unsafe node id '{nid}' in graph '{graph_id}'")
            if nid in _RESERVED_IDS:
                errors.append(f"node id '{nid}' is reserved")
            errors.extend(_check_uses(loc, node, schema))
            if node.retry is not None and node.retry not in schema.policies.retry:
                errors.append(f"{loc} references unknown retry policy '{node.retry}'")
            if node.timeout is not None and node.timeout not in schema.policies.timeout:
                errors.append(f"{loc} references unknown timeout policy '{node.timeout}'")
            gate_id = _node_gate_id(node)
            if gate_id is not None and gate_id not in schema.gates:
                errors.append(f"{loc} references unknown gate '{gate_id}'")
            for output in node.outputs:
                errors.extend(_check_output_path(loc, output, node, param_names=frozenset(schema.params)))
            if node.join is not None:
                for src in node.join.sources:
                    if src not in graph.nodes:
                        errors.append(f"{loc} join source unknown node '{src}'")
                    elif src == nid:
                        errors.append(f"{loc} join must not list itself as a source")
            if node.fan_out is not None and node.fan_out.reduce is not None:
                if node.fan_out.reduce.into not in graph.state:
                    errors.append(
                        f"{loc} fan_out reduce into undeclared state key '{node.fan_out.reduce.into}'"
                    )
            for key in node.state_writes:
                if key not in graph.state:
                    errors.append(f"{loc} writes undeclared state key '{key}'")
            if node.budget is not None:
                budget = node.budget
                if budget.consume not in graph.budgets:
                    errors.append(f"{loc} consumes unknown budget '{budget.consume}'")
                else:
                    errors.extend(
                        _check_budget_limit(
                            schema, graph_id, budget.consume, graph.budgets[budget.consume].limit
                        )
                    )
                if budget.exhausted_to not in graph.nodes and budget.exhausted_to not in _TERMINALS:
                    errors.append(f"{loc} exhausted_to unknown node '{budget.exhausted_to}'")
        for edge in graph.edges:
            if edge.from_ != "START" and edge.from_ not in graph.nodes:
                errors.append(f"graph '{graph_id}' edge from unknown node '{edge.from_}'")
            if edge.to not in graph.nodes and edge.to not in _TERMINALS:
                errors.append(f"graph '{graph_id}' edge to unknown node '{edge.to}'")
        for route in graph.routes:
            if route.from_ not in graph.nodes:
                errors.append(f"graph '{graph_id}' route from unknown node '{route.from_}'")
            targets = list(route.cases.values()) + ([route.default] if route.default is not None else [])
            for target in targets:
                if target not in graph.nodes and target not in _TERMINALS:
                    errors.append(
                        f"graph '{graph_id}' route from '{route.from_}' targets unknown node '{target}'"
                    )
        errors.extend(_check_reachability(graph_id, graph))
    return errors


# ---------------------------------------------------------------------------
# 表达式


def _collect_artifact_symbols(graph_id: str, graph: GraphDef) -> tuple[dict[str, str], list[str]]:
    """每个 JSON output 从文件名 stem 派生表达式 symbol（change:explore/advisory.json → advisory）。

    同一 graph 内 stem 冲突即拒绝——symbol 是表达式访问该 artifact 的唯一入口。
    """
    symbols: dict[str, str] = {}
    errors: list[str] = []
    for nid, node in graph.nodes.items():
        for output in node.outputs:
            if not output.endswith(".json"):
                continue
            symbol = derive_alias(output)
            if not symbol:
                errors.append(f"graph '{graph_id}' node '{nid}' output '{output}' yields no artifact symbol")
                continue
            if symbol in symbols and symbols[symbol] != output:
                errors.append(
                    f"graph '{graph_id}' duplicate artifact symbol '{symbol}': "
                    f"'{symbols[symbol]}' vs '{output}'"
                )
                continue
            symbols[symbol] = output
    return symbols, errors


def _walk_expression(
    expr: Expr,
    errors: list[str],
    *,
    loc: str,
    allowed_idents: set[str],
    allow_node: bool,
    node_ids: set[str],
    gate_ids: set[str],
    param_names: set[str],
    in_predicate: bool,
) -> None:
    child_kw = {
        "loc": loc,
        "allowed_idents": allowed_idents,
        "allow_node": allow_node,
        "node_ids": node_ids,
        "gate_ids": gate_ids,
        "param_names": param_names,
    }
    if isinstance(expr, Ident):
        if not in_predicate and expr.name not in allowed_idents:
            errors.append(f"{loc}: unknown identifier '{expr.name}'")
        return
    if isinstance(expr, Member):
        if (
            "params" in allowed_idents
            and isinstance(expr.obj, Ident)
            and expr.obj.name == "params"
            and expr.prop not in param_names
        ):
            errors.append(f"{loc}: references unknown param '{expr.prop}'")
        _walk_expression(expr.obj, errors, in_predicate=in_predicate, **child_kw)
        return
    if isinstance(expr, Subscript):
        if (
            "params" in allowed_idents
            and isinstance(expr.obj, Ident)
            and expr.obj.name == "params"
            and isinstance(expr.index, str)
            and expr.index not in param_names
        ):
            errors.append(f"{loc}: references unknown param '{expr.index}'")
        _walk_expression(expr.obj, errors, in_predicate=in_predicate, **child_kw)
        return
    if isinstance(expr, Call):
        if expr.callee == "node":
            arg = expr.args[0]
            if not allow_node:
                errors.append(f"{loc}: node() is not allowed here")
            elif not (isinstance(arg, Literal) and isinstance(arg.value, str)):
                errors.append(f"{loc}: node() argument must be a string literal")
            elif arg.value not in node_ids:
                errors.append(f"{loc}: references unknown node('{arg.value}')")
        if expr.callee == "gate":
            arg = expr.args[0]
            if isinstance(arg, Literal) and isinstance(arg.value, str) and arg.value not in gate_ids:
                errors.append(f"{loc}: references unknown gate '{arg.value}'")
        for i, arg in enumerate(expr.args):
            # any/all/count 的谓词在 element child scope 求值，裸标识符是元素字段，
            # 不做顶层 allowlist；其中的 call/引用仍然递归校验。
            nested = in_predicate or (expr.callee in _PREDICATE_BUILTINS and i == 1)
            _walk_expression(arg, errors, in_predicate=nested, **child_kw)
        return
    children: list[Expr] = []
    if isinstance(expr, (Compare, BoolOp)):
        children = [expr.left, expr.right]
    elif isinstance(expr, Not):
        children = [expr.operand]
    for child in children:
        _walk_expression(child, errors, in_predicate=in_predicate, **child_kw)


def _check_expression(
    loc: str,
    text: str,
    *,
    allowed_idents: set[str],
    allow_node: bool,
    node_ids: set[str],
    schema: WorkflowSchemaV2,
) -> list[str]:
    try:
        expr = parse_expression(text)
    except DslError as exc:
        return [f"{loc}: invalid expression — {exc}"]
    errors: list[str] = []
    _walk_expression(
        expr,
        errors,
        loc=loc,
        allowed_idents=allowed_idents,
        allow_node=allow_node,
        node_ids=node_ids,
        gate_ids=set(schema.gates),
        param_names=set(schema.params),
        in_predicate=False,
    )
    return errors


def _validate_expressions(schema: WorkflowSchemaV2) -> list[str]:
    errors: list[str] = []
    for name, entrypoint in schema.entrypoints.items():
        if entrypoint.allow:
            errors.extend(
                _check_expression(
                    f"entrypoint '{name}'.allow",
                    entrypoint.allow,
                    allowed_idents={"params"},
                    allow_node=False,
                    node_ids=set(),
                    schema=schema,
                )
            )
    for gate in schema.gates.values():
        aliases = {entry.alias for entry in gate.reads}
        for rule in gate.rules:
            errors.extend(
                _check_expression(
                    f"gate '{gate.id}'.{rule.field}",
                    rule.expr,
                    allowed_idents={"params", "state"} | aliases,
                    allow_node=False,
                    node_ids=set(),
                    schema=schema,
                )
            )
    for graph_id, graph in schema.graphs.items():
        symbols, symbol_errors = _collect_artifact_symbols(graph_id, graph)
        errors.extend(symbol_errors)
        allowed = {"params", "state"} | set(symbols)
        node_ids = set(graph.nodes)
        for nid, node in graph.nodes.items():
            loc = f"graph '{graph_id}' node '{nid}'"
            if node.when:
                errors.extend(
                    _check_expression(
                        f"{loc}.when",
                        node.when,
                        allowed_idents=allowed,
                        allow_node=True,
                        node_ids=node_ids,
                        schema=schema,
                    )
                )
            with_expr = node.with_.get("expression")
            if isinstance(with_expr, str):
                errors.extend(
                    _check_expression(
                        f"{loc}.with.expression",
                        with_expr,
                        allowed_idents=allowed,
                        allow_node=True,
                        node_ids=node_ids,
                        schema=schema,
                    )
                )
            if node.fan_out is not None:
                errors.extend(
                    _check_expression(
                        f"{loc}.fan_out.items",
                        node.fan_out.items,
                        allowed_idents=allowed,
                        allow_node=True,
                        node_ids=node_ids,
                        schema=schema,
                    )
                )
            for key, target in node.state_writes.items():
                errors.extend(
                    _check_expression(
                        f"{loc}.state_writes['{key}']",
                        target,
                        allowed_idents={"result"},
                        allow_node=False,
                        node_ids=node_ids,
                        schema=schema,
                    )
                )
        for edge in graph.edges:
            if edge.when:
                errors.extend(
                    _check_expression(
                        f"graph '{graph_id}' edge {edge.from_} -> {edge.to}.when",
                        edge.when,
                        allowed_idents=allowed,
                        allow_node=True,
                        node_ids=node_ids,
                        schema=schema,
                    )
                )
        for route in graph.routes:
            errors.extend(
                _check_expression(
                    f"graph '{graph_id}' route from '{route.from_}'.select",
                    route.select,
                    allowed_idents=allowed | {"resume"},
                    allow_node=True,
                    node_ids=node_ids,
                    schema=schema,
                )
            )
    return errors


# ---------------------------------------------------------------------------
# routes / interrupts


def _gate_verdict_select(expr: Expr) -> str | None:
    """``node('<nid>').gate.verdict`` → nid；其它形状 → None。"""
    if not (isinstance(expr, Member) and expr.prop == "verdict"):
        return None
    inner = expr.obj
    if not (isinstance(inner, Member) and inner.prop == "gate"):
        return None
    call = inner.obj
    if not (isinstance(call, Call) and call.callee == "node"):
        return None
    arg = call.args[0]
    if isinstance(arg, Literal) and isinstance(arg.value, str):
        return arg.value
    return None


def _validate_routes_and_interrupts(schema: WorkflowSchemaV2) -> list[str]:
    errors: list[str] = []
    for graph_id, graph in schema.graphs.items():
        for nid, node in graph.nodes.items():
            if node.interrupt is None:
                continue
            routed = {label for route in graph.routes if route.from_ == nid for label in route.cases}
            for action in node.interrupt.actions:
                if action not in routed:
                    errors.append(
                        f"graph '{graph_id}' interrupt node '{nid}' action '{action}' has no resume route"
                    )
        for route in graph.routes:
            try:
                select = parse_expression(route.select)
            except DslError:
                continue  # parse 错误已在 expressions 校验报告
            nid = _gate_verdict_select(select)
            if nid is None or nid not in graph.nodes:
                continue
            gate_id = _node_gate_id(graph.nodes[nid])
            if gate_id is None or gate_id not in schema.gates:
                continue
            gate = schema.gates[gate_id]
            possible = {str(rule.verdict) for rule in gate.rules}
            possible.add(str(gate.default))
            for extra in (gate.invalid_json, gate.missing_field_is, gate.missing_file_is):
                if extra is not None:
                    possible.add(str(extra))
            if route.default is None:
                missing = sorted(possible - set(route.cases))
                if missing:
                    errors.append(
                        f"graph '{graph_id}' route from '{route.from_}' is not exhaustive for gate "
                        f"'{gate_id}': missing verdicts {', '.join(missing)} "
                        "(declare cases or a fail-closed default)"
                    )
    return errors


# ---------------------------------------------------------------------------
# state writers


def _state_default_matches(state: StateDef) -> bool:
    value = state.default
    if state.type == "list":
        return isinstance(value, list)
    if state.type == "object":
        return isinstance(value, dict)
    if state.type == "str":
        return isinstance(value, str)
    if state.type == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, bool)


def _validate_state_writers(schema: WorkflowSchemaV2) -> list[str]:
    errors: list[str] = []
    for graph_id, graph in schema.graphs.items():
        for key, state in graph.state.items():
            if not _state_default_matches(state):
                errors.append(f"graph '{graph_id}' state '{key}' default does not match type '{state.type}'")
        writers: dict[str, list[str]] = {}
        for nid, node in graph.nodes.items():
            for key in node.state_writes:
                writers.setdefault(key, []).append(nid)
        for key, nids in writers.items():
            state = graph.state.get(key)
            if state is None:
                continue  # 未声明引用已在 graph_refs 报告
            if state.reducer == "replace" and len(nids) > 1:
                errors.append(
                    f"graph '{graph_id}' state key '{key}' reducer 'replace' "
                    f"has multiple writers: {', '.join(nids)}"
                )
    return errors


# ---------------------------------------------------------------------------
# subgraph 递归


def _validate_subgraph_recursion(schema: WorkflowSchemaV2) -> list[str]:
    calls: dict[str, list[str]] = {gid: [] for gid in schema.graphs}
    for gid, graph in schema.graphs.items():
        for node in graph.nodes.values():
            prefix, _, target = node.uses.partition(":")
            if prefix == "graph" and target in schema.graphs:
                calls[gid].append(target)
    errors: list[str] = []
    white, grey, black = 0, 1, 2
    color = {gid: white for gid in calls}
    stack: list[str] = []

    def dfs(gid: str) -> bool:
        color[gid] = grey
        stack.append(gid)
        for nxt in calls[gid]:
            if color[nxt] == grey:
                cycle = stack[stack.index(nxt) :] + [nxt]
                errors.append("subgraph recursion: " + " -> ".join(cycle))
                return True
            if color[nxt] == white and dfs(nxt):
                return True
        stack.pop()
        color[gid] = black
        return False

    for gid in calls:
        if color[gid] == white and dfs(gid):
            break
    return errors


# ---------------------------------------------------------------------------
# execution contract 校验与资源 footprint

_HANDLER_BY_PREFIX = {"skill": "agent", "operation": "operation", "builtin": "builtin"}


def _validate_contract_usage(schema: WorkflowSchemaV2, catalog: ExecutionContractCatalog) -> list[str]:
    """catalog 存在时的 target 级校验：uses 解析、handler 一致、授权收窄、retry kind 白名单。"""
    errors: list[str] = []
    for graph_id, graph in schema.graphs.items():
        for nid, node in graph.nodes.items():
            loc = f"graph '{graph_id}' node '{nid}'"
            uses = node.uses
            prefix, _, _ = uses.partition(":")
            if prefix == "graph":
                continue  # graph:<id> 引用与递归由拓扑校验负责，footprint 另行展开
            contract = catalog.contracts.get(uses)
            if contract is None:
                errors.append(f"{loc} uses '{uses}' which has no execution contract")
                continue
            expected = _HANDLER_BY_PREFIX.get(prefix)
            if expected is not None and contract.handler != expected:
                errors.append(f"{loc} uses '{uses}' but its contract handler is '{contract.handler}'")
            if node.agent is not None and prefix != "skill":
                errors.append(f"{loc} declares agent '{node.agent}' but uses '{uses}' which is not skill:*")
            errors.extend(_check_authorization_narrowing(loc, node, contract))
            errors.extend(_check_retry_kinds(loc, node, schema, contract))
    return errors


def _check_authorization_narrowing(loc: str, node: NodeDef, contract: ExecutionContract) -> list[str]:
    """显式 node resources.writes 只能收窄 contract 授权写范围，不能扩大。"""
    declared = node.resources
    if declared is None:
        return []
    authorization = tuple(ResourcePath.parse(v) for v in contract.authorization_writes)
    errors: list[str] = []
    for write in declared.writes:
        try:
            claim = ResourcePath.parse(normalize_claim_pattern(write))
        except ContractError as exc:
            errors.append(f"{loc}: {exc}")
            continue
        if not any(path_covers(auth, claim) for auth in authorization):
            errors.append(
                f"{loc} resources.writes '{write}' expands authorization beyond '{contract.target}'"
            )
    return errors


def _check_retry_kinds(
    loc: str,
    node: NodeDef,
    schema: WorkflowSchemaV2,
    contract: ExecutionContract,
) -> list[str]:
    """retry policy 的 retry_on 必须落在目标 contract 的 retryable_errors 白名单内。"""
    if node.retry is None:
        return []
    policy = schema.policies.retry.get(node.retry)
    if policy is None:
        return []  # 未知 policy 已在 graph_refs 报告
    unsupported = sorted(set(policy.retry_on) - set(contract.retryable_errors))
    if unsupported:
        return [
            f"{loc} retry policy '{node.retry}' kinds not retryable for "
            f"'{contract.target}': {', '.join(unsupported)}"
        ]
    return []


def _graph_footprints(
    schema: WorkflowSchemaV2, catalog: ExecutionContractCatalog | None
) -> tuple[dict[str, ResourceClaims], dict[str, dict[str, ResourceClaims]]]:
    """每个 graph 的保守资源 footprint 与逐 node claim。

    footprint 是全图 node claim 的并集（``graph:<id>`` 递归展开）；逐 node claim
    保留 catalog 合成的本 node 授权范围，供 scheduler 做更细粒度的 wave 冲突判定。
    互斥的运行期条件在 v2 首版不用于削减 claim——保守串行是正确的。subgraph
    递归已在编译期拒绝，memoized 展开不会成环。
    """
    memo: dict[str, ResourceClaims] = {}
    node_claims: dict[str, dict[str, ResourceClaims]] = {}

    def footprint(graph_id: str) -> ResourceClaims:
        if graph_id in memo:
            return memo[graph_id]
        claims = ResourceClaims()
        graph = schema.graphs[graph_id]
        per_node: dict[str, ResourceClaims] = {}
        for nid, node in graph.nodes.items():
            prefix, _, target = node.uses.partition(":")
            if prefix == "graph":
                child = footprint(target) if target in schema.graphs else unknown_claims()
            elif catalog is None:
                child = unknown_claims()
            else:
                child = catalog.claims_for(node)
            per_node[nid] = child
            claims = claims.union(child)
        node_claims[graph_id] = per_node
        memo[graph_id] = claims
        return claims

    footprints = {graph_id: footprint(graph_id) for graph_id in schema.graphs}
    return footprints, node_claims


def _referenced_contract_digests(
    schema: WorkflowSchemaV2, catalog: ExecutionContractCatalog
) -> dict[str, str]:
    """workflow 实际引用到的 contract 的 canonical digest（invocation 冻结与 resume 校验用）。"""
    digests: dict[str, str] = {}
    for graph in schema.graphs.values():
        for node in graph.nodes.values():
            contract = catalog.contracts.get(node.uses)
            if contract is not None and node.uses not in digests:
                digests[node.uses] = canonical_digest(contract)
    return digests


# ---------------------------------------------------------------------------
# 有界 cycle（Tarjan SCC）


def _tarjan_sccs(adj: Mapping[str, list[str]]) -> list[tuple[str, ...]]:
    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    sccs: list[tuple[str, ...]] = []
    counter = 0

    def strongconnect(v: str) -> None:
        nonlocal counter
        index_of[v] = lowlink[v] = counter
        counter += 1
        stack.append(v)
        on_stack.add(v)
        for w in adj[v]:
            if w not in index_of:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index_of[w])
        if lowlink[v] == index_of[v]:
            members: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                members.append(w)
                if w == v:
                    break
            sccs.append(tuple(members))

    for v in adj:
        if v not in index_of:
            strongconnect(v)
    return sccs


def _is_cyclic(scc: tuple[str, ...], adj: Mapping[str, list[str]]) -> bool:
    return len(scc) > 1 or any(nid in adj[nid] for nid in scc)


def _validate_bounded_sccs(schema: WorkflowSchemaV2) -> list[str]:
    errors: list[str] = []
    for graph_id, graph in schema.graphs.items():
        adj = _graph_adjacency(graph)
        for scc in _tarjan_sccs(adj):
            if not _is_cyclic(scc, adj):
                continue
            label = ", ".join(sorted(scc))
            consumers = [nid for nid in scc if graph.nodes[nid].budget is not None]
            if not consumers:
                errors.append(f"graph '{graph_id}' cycle [{label}] has no bounded budget consumer")
                continue
            for nid in consumers:
                budget = graph.nodes[nid].budget
                if budget is None or budget.consume not in graph.budgets:
                    continue  # 未知/非法 budget 已在 graph_refs 报告
                if budget.exhausted_to in scc:
                    errors.append(
                        f"graph '{graph_id}' node '{nid}' exhausted_to '{budget.exhausted_to}' "
                        f"does not leave cycle [{label}]"
                    )
    return errors


# ---------------------------------------------------------------------------
# 编译产物


def _condensation_order(
    graph: GraphDef, adj: Mapping[str, list[str]], sccs: list[tuple[str, ...]]
) -> list[tuple[str, ...]]:
    """SCC 冷凝 DAG 的确定性拓扑序（并列时按成员最小 declaration index）。"""
    decl = {nid: i for i, nid in enumerate(graph.nodes)}

    def scc_key(i: int) -> int:
        return min(decl[n] for n in sccs[i])

    scc_of = {nid: i for i, scc in enumerate(sccs) for nid in scc}
    cadj: dict[int, set[int]] = {i: set() for i in range(len(sccs))}
    indegree = {i: 0 for i in range(len(sccs))}
    for src, outs in adj.items():
        for dst in outs:
            a, b = scc_of[src], scc_of[dst]
            if a != b and b not in cadj[a]:
                cadj[a].add(b)
                indegree[b] += 1
    ready = sorted((i for i in range(len(sccs)) if indegree[i] == 0), key=scc_key)
    order: list[int] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for nxt in sorted(cadj[current], key=scc_key):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
        ready.sort(key=scc_key)
    return [sccs[i] for i in order]


def _validate_exports(schema: WorkflowSchemaV2) -> list[str]:
    errors: list[str] = []
    for graph_id, graph in schema.graphs.items():
        for symbol, spec in graph.exports.items():
            if spec.from_ not in graph.nodes:
                errors.append(f"graph '{graph_id}' export '{symbol}' references unknown node '{spec.from_}'")
    return errors


def _compiled_exports(schema: WorkflowSchemaV2, node: NodeDef) -> tuple[CompiledExport, ...]:
    if not node.uses.startswith("graph:"):
        return ()
    target_id = node.uses.split(":", 1)[1]
    target = schema.graphs.get(target_id)
    if target is None:
        return ()
    return tuple(
        CompiledExport(symbol=symbol, from_node=spec.from_, output=spec.output)
        for symbol, spec in sorted(target.exports.items())
    )


def _compile_graph(
    schema: WorkflowSchemaV2,
    graph_id: str,
    graph: GraphDef,
    footprint: ResourceClaims,
    node_claims: dict[str, ResourceClaims],
) -> CompiledGraph:
    decl = {nid: i for i, nid in enumerate(graph.nodes)}
    adj = _graph_adjacency(graph)
    ordered_sccs = _condensation_order(graph, adj, _tarjan_sccs(adj))
    scc_pos = {nid: pos for pos, scc in enumerate(ordered_sccs) for nid in scc}
    # topology_rank：SCC 冷凝 DAG 拓扑序 → declaration index → node ID。
    ranked = sorted(graph.nodes, key=lambda nid: (scc_pos[nid], decl[nid], nid))
    topology_rank = {nid: rank for rank, nid in enumerate(ranked)}
    nodes = {
        nid: CompiledNode(
            graph_id=graph_id,
            node_id=nid,
            declaration_index=decl[nid],
            topology_rank=topology_rank[nid],
            definition=node,
            incoming=tuple(edge for edge in graph.edges if edge.to == nid),
            outgoing=tuple(edge for edge in graph.edges if edge.from_ == nid),
            routes=tuple(route for route in graph.routes if route.from_ == nid),
            resources=node_claims.get(nid, unknown_claims()),
            exports=_compiled_exports(schema, node),
        )
        for nid, node in graph.nodes.items()
    }
    symbols, _ = _collect_artifact_symbols(graph_id, graph)  # 校验已通过，无错误
    return CompiledGraph(
        graph_id=graph_id,
        max_supersteps=graph.max_supersteps,
        declaration_order=tuple(graph.nodes),
        nodes=nodes,
        sccs=tuple(tuple(sorted(scc, key=lambda nid: decl[nid])) for scc in ordered_sccs),
        artifact_symbols=symbols,
        resource_footprint=footprint,
    )


def _compile_entrypoints(schema: WorkflowSchemaV2) -> dict[str, CompiledEntrypoint]:
    return {
        name: CompiledEntrypoint(
            name=name,
            graph_id=entrypoint.graph,
            allow_expr=parse_expression(entrypoint.allow) if entrypoint.allow else None,
            param_overrides=dict(entrypoint.with_),
        )
        for name, entrypoint in schema.entrypoints.items()
    }
