"""Pure CFG, dominance, reachability, and finite truth-table helpers."""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from assurance_agent.workflow.graph.schema_v2 import GraphDef
from assurance_agent.workflow.orchestration.dsl import (
    BUILTIN_ARITY,
    MISSING,
    BoolOp,
    Call,
    Compare,
    DslError,
    Expr,
    Ident,
    ListLit,
    Literal as DslLiteral,
    Member,
    Not,
    Scope,
    Subscript,
    evaluate,
    parse_expression,
)

Assignment = Mapping[str, object]
Truth = Literal[True, False, "mismatch"]


@dataclass(frozen=True, slots=True)
class CfgEdge:
    """One ordinary or route-outcome edge in a compiled CFG view."""

    src: str
    dst: str
    kind: Literal["ordinary", "route"]
    label: str | None = None


@dataclass(frozen=True, slots=True)
class ControlFlowGraph:
    """Directed CFG over node IDs plus synthetic terminals."""

    nodes: frozenset[str]
    edges: tuple[CfgEdge, ...]
    successors: Mapping[str, tuple[str, ...]]
    predecessors: Mapping[str, tuple[str, ...]]


def build_cfg(graph: GraphDef) -> ControlFlowGraph:
    """Build a CFG from ordinary edges and every route case/default outcome."""
    nodes = set(graph.nodes) | {"START", "END", "STOP", "FAIL"}
    edges: list[CfgEdge] = []
    for edge in graph.edges:
        nodes.add(edge.from_)
        nodes.add(edge.to)
        edges.append(CfgEdge(src=edge.from_, dst=edge.to, kind="ordinary", label=edge.when))
    for route in graph.routes:
        nodes.add(route.from_)
        for label, target in route.cases.items():
            nodes.add(target)
            edges.append(CfgEdge(src=route.from_, dst=target, kind="route", label=label))
        if route.default is not None:
            nodes.add(route.default)
            edges.append(CfgEdge(src=route.from_, dst=route.default, kind="route", label="default"))
    successors: dict[str, list[str]] = {nid: [] for nid in nodes}
    predecessors: dict[str, list[str]] = {nid: [] for nid in nodes}
    for edge in edges:
        if edge.dst not in successors[edge.src]:
            successors[edge.src].append(edge.dst)
        if edge.src not in predecessors[edge.dst]:
            predecessors[edge.dst].append(edge.src)
    return ControlFlowGraph(
        nodes=frozenset(nodes),
        edges=tuple(edges),
        successors={nid: tuple(dsts) for nid, dsts in successors.items()},
        predecessors={nid: tuple(srcs) for nid, srcs in predecessors.items()},
    )


def reachable_from(cfg: ControlFlowGraph, start: str) -> frozenset[str]:
    """Return every node reachable from ``start`` over ordinary and route edges."""
    if start not in cfg.nodes:
        return frozenset()
    seen: set[str] = set()
    stack = [start]
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        stack.extend(cfg.successors.get(nid, ()))
    return frozenset(seen)


def can_reach(cfg: ControlFlowGraph, start: str, target: str) -> bool:
    return target in reachable_from(cfg, start)


def dominates(cfg: ControlFlowGraph, dominator: str, node: str, *, entry: str = "START") -> bool:
    """Return True when every path from ``entry`` to ``node`` passes through ``dominator``."""
    if dominator == node:
        return dominator in reachable_from(cfg, entry) or dominator == entry
    if not can_reach(cfg, entry, node):
        return False
    if dominator == entry:
        return True
    # Remove dominator and check whether node remains reachable.
    blocked = _reachable_avoiding(cfg, entry, avoid=dominator)
    return node not in blocked


def _reachable_avoiding(cfg: ControlFlowGraph, start: str, *, avoid: str) -> frozenset[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        nid = stack.pop()
        if nid == avoid or nid in seen:
            continue
        seen.add(nid)
        stack.extend(cfg.successors.get(nid, ()))
    return frozenset(seen)


def paths_exist_avoiding(
    cfg: ControlFlowGraph,
    start: str,
    target: str,
    *,
    avoid: frozenset[str] | set[str],
) -> bool:
    """Return True when ``target`` is reachable from ``start`` without visiting ``avoid``."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        nid = stack.pop()
        if nid in avoid or nid in seen:
            continue
        if nid == target:
            return True
        seen.add(nid)
        stack.extend(cfg.successors.get(nid, ()))
    return False


def layer_selection_domain(
    *,
    layers: Sequence[str],
    run_modes: Sequence[str],
) -> tuple[dict[str, object], ...]:
    """Closed release domain over test_types subsets (incl. empty) and run modes."""
    assignments: list[dict[str, object]] = []
    for width in range(0, len(layers) + 1):
        for subset in itertools.combinations(layers, width):
            for mode in run_modes:
                assignments.append({"test_types": list(subset), "run_mode": mode})
    return tuple(assignments)


def expressions_truth_equivalent(
    left: str | Expr,
    right: str | Expr,
    assignments: Iterable[Assignment],
    *,
    allowed_params: frozenset[str] | set[str],
    allowed_builtins: frozenset[str] | set[str] | None = None,
) -> bool:
    """Return True when both expressions agree on every finite-domain assignment.

    Parse failures, undeclared params, or unknown builtins count as mismatch
    (not as ``False``).
    """
    allowed_builtins = frozenset(BUILTIN_ARITY) if allowed_builtins is None else frozenset(allowed_builtins)
    left_expr = _parse_or_none(left)
    right_expr = _parse_or_none(right)
    if left_expr is None or right_expr is None:
        return False
    if _unknown_dependencies(left_expr, allowed_params, allowed_builtins):
        return False
    if _unknown_dependencies(right_expr, allowed_params, allowed_builtins):
        return False
    for assignment in assignments:
        left_val = _eval_bool(left_expr, assignment)
        right_val = _eval_bool(right_expr, assignment)
        if left_val == "mismatch" or right_val == "mismatch" or left_val != right_val:
            return False
    return True


def expression_matches_required(
    actual: str | Expr,
    required: str | Expr,
    assignments: Iterable[Assignment],
    *,
    allowed_params: frozenset[str] | set[str],
    allowed_builtins: frozenset[str] | set[str] | None = None,
) -> bool:
    """Truth-table equality against a required expression over the closed domain."""
    return expressions_truth_equivalent(
        actual,
        required,
        assignments,
        allowed_params=allowed_params,
        allowed_builtins=allowed_builtins,
    )


def _parse_or_none(value: str | Expr) -> Expr | None:
    if isinstance(value, str):
        try:
            return parse_expression(value)
        except (DslError, Exception):  # noqa: BLE001 - treat any parse failure as mismatch
            return None
    return value


def _unknown_dependencies(
    expr: Expr,
    allowed_params: frozenset[str] | set[str],
    allowed_builtins: frozenset[str] | set[str],
) -> bool:
    allowed_roots = frozenset({"params", "policy", "resume", "state"})
    for ident in _root_idents(expr):
        if ident in allowed_roots:
            continue
        # Bare identifiers outside allowed roots are undeclared (mismatch).
        if ident not in allowed_params:
            return True
    for path in _param_paths(expr):
        if path and path[0] not in allowed_params:
            return True
    for call in _collect_calls(expr):
        if call.callee not in allowed_builtins:
            return True
    return False


def _root_idents(expr: Expr) -> set[str]:
    found: set[str] = set()

    def walk(node: Expr) -> None:
        if isinstance(node, Ident):
            found.add(node.name)
        elif isinstance(node, Member):
            walk(node.obj)
        elif isinstance(node, Subscript):
            walk(node.obj)
        elif isinstance(node, Compare):
            walk(node.left)
            walk(node.right)
        elif isinstance(node, BoolOp):
            walk(node.left)
            walk(node.right)
        elif isinstance(node, Not):
            walk(node.operand)
        elif isinstance(node, Call):
            for arg in node.args:
                walk(arg)

    walk(expr)
    return found


def _param_paths(expr: Expr) -> set[tuple[str, ...]]:
    paths: set[tuple[str, ...]] = set()

    def walk(node: Expr, prefix: tuple[str, ...] | None = None) -> None:
        if isinstance(node, Ident):
            if node.name == "params":
                return
            if prefix == ("params",):
                paths.add((node.name,))
            return
        if isinstance(node, Member):
            if isinstance(node.obj, Ident) and node.obj.name == "params":
                paths.add((node.prop,))
                return
            walk(node.obj, prefix)
            return
        if isinstance(node, Subscript):
            walk(node.obj, prefix)
            return
        if isinstance(node, Compare):
            walk(node.left)
            walk(node.right)
            return
        if isinstance(node, BoolOp):
            walk(node.left)
            walk(node.right)
            return
        if isinstance(node, Not):
            walk(node.operand)
            return
        if isinstance(node, Call):
            for arg in node.args:
                walk(arg)

    walk(expr)
    return paths


def _collect_calls(expr: Expr) -> list[Call]:
    found: list[Call] = []

    def walk(node: Expr) -> None:
        if isinstance(node, Call):
            found.append(node)
            for arg in node.args:
                walk(arg)
            return
        if isinstance(node, Member):
            walk(node.obj)
            return
        if isinstance(node, Subscript):
            walk(node.obj)
            return
        if isinstance(node, Compare):
            walk(node.left)
            walk(node.right)
            return
        if isinstance(node, BoolOp):
            walk(node.left)
            walk(node.right)
            return
        if isinstance(node, Not):
            walk(node.operand)

    walk(expr)
    return found


def _eval_bool(expr: Expr, assignment: Assignment) -> Truth:
    params = {
        key: value
        for key, value in assignment.items()
        if key
        in {
            "test_types",
            "run_mode",
            "run_tests",
            "force_continue",
        }
        or not key.startswith("_")
    }
    # Keep undeclared params out of scope so lookups become MISSING.
    scope_vars: dict[str, object] = {"params": params}
    for key, value in assignment.items():
        if key.startswith("params."):
            continue
        if key not in {"test_types", "run_mode", "run_tests", "force_continue"}:
            scope_vars[key] = value

    def file_exists(path: str) -> bool:
        files = assignment.get("_files")
        if isinstance(files, Mapping):
            return bool(files.get(path, False))
        return path == "repo:.aa/data-knowledge.yaml"

    def gate_verdict(gate_id: str) -> str:
        gates = assignment.get("_gates")
        if isinstance(gates, Mapping) and gate_id in gates:
            return str(gates[gate_id])
        return "stop"

    def node_result(node_id: str) -> object:
        nodes = assignment.get("_nodes")
        if isinstance(nodes, Mapping) and node_id in nodes:
            return nodes[node_id]
        return MISSING

    def capabilities_present(review_doc: object, dk_doc: object) -> bool:
        _ = (review_doc, dk_doc)
        value = assignment.get("_capabilities_present")
        return True if value is None else bool(value)

    def plan_assurance_state(
        checks: object,
        review: object,
        dk: object,
        layer: object,
    ) -> str:
        _ = (checks, review, dk, layer)
        value = assignment.get("_plan_assurance_state")
        return "applicable" if value is None else str(value)

    scope = Scope(
        scope_vars,
        file_exists=file_exists,
        gate_verdict=gate_verdict,
        node_result=node_result,
        capabilities_present=capabilities_present,
        plan_assurance_state=plan_assurance_state,
    )
    try:
        result = evaluate(expr, scope)
    except DslError:
        return "mismatch"
    except Exception:  # noqa: BLE001
        return "mismatch"
    if result is True:
        return True
    if result is False:
        return False
    return "mismatch"


def flatten_boolop(expr: Expr, op: str) -> list[Expr]:
    """Flatten a left-associative boolean chain of the same operator."""
    if isinstance(expr, BoolOp) and expr.op == op:
        return flatten_boolop(expr.left, op) + flatten_boolop(expr.right, op)
    return [expr]


def reorder_commutative_and(text: str) -> str:
    """Test helper: reverse top-level ``and`` operands while preserving semantics."""
    expr = parse_expression(text)
    parts = flatten_boolop(expr, "and")
    if len(parts) < 2:
        return text
    return " and ".join(_expr_to_source(part) for part in reversed(parts))


def reorder_list_literal(text: str) -> str:
    """Test helper: reverse the first list literal found in a membership check."""
    expr = parse_expression(text)

    def rewrite(node: Expr) -> Expr:
        if isinstance(node, Compare) and node.op == "in" and isinstance(node.right, ListLit):
            return Compare(node.op, node.left, ListLit(tuple(reversed(node.right.elements))))
        if isinstance(node, BoolOp):
            return BoolOp(node.op, rewrite(node.left), rewrite(node.right))
        if isinstance(node, Not):
            return Not(rewrite(node.operand))
        if isinstance(node, Call):
            return Call(node.callee, tuple(rewrite(arg) for arg in node.args))
        if isinstance(node, Member):
            return Member(rewrite(node.obj), node.prop)
        if isinstance(node, Subscript):
            return Subscript(rewrite(node.obj), node.index)
        if isinstance(node, Compare):
            return Compare(node.op, rewrite(node.left), rewrite(node.right))
        return node

    return _expr_to_source(rewrite(expr))


def _expr_to_source(expr: Expr) -> str:  # noqa: C901 - structural pretty-printer
    if isinstance(expr, DslLiteral):
        if isinstance(expr.value, str):
            return repr(expr.value)
        if expr.value is None:
            return "null"
        if expr.value is True:
            return "true"
        if expr.value is False:
            return "false"
        return repr(expr.value)
    if isinstance(expr, ListLit):
        inner = ", ".join(
            repr(el)
            if isinstance(el, str)
            else ("true" if el is True else "false" if el is False else repr(el))
            for el in expr.elements
        )
        return f"[{inner}]"
    if isinstance(expr, Ident):
        return expr.name
    if isinstance(expr, Member):
        return f"{_expr_to_source(expr.obj)}.{expr.prop}"
    if isinstance(expr, Subscript):
        return f"{_expr_to_source(expr.obj)}[{expr.index!r}]"
    if isinstance(expr, Not):
        return f"not {_expr_to_source(expr.operand)}"
    if isinstance(expr, BoolOp):
        return f"({_expr_to_source(expr.left)} {expr.op} {_expr_to_source(expr.right)})"
    if isinstance(expr, Compare):
        return f"{_expr_to_source(expr.left)} {expr.op} {_expr_to_source(expr.right)}"
    if isinstance(expr, Call):
        args = ", ".join(_expr_to_source(arg) for arg in expr.args)
        return f"{expr.callee}({args})"
    raise TypeError(f"unsupported expr: {type(expr)!r}")
