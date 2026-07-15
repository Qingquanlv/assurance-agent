"""AA 谓词小语言（DSL）：ast.parse 语法解析 + 白名单自解释 + 三值求值。

- 值模型：缺失路径 → MISSING（不是 None；None 是显式 null）。求值对缺失数据是全函数，
  不抛异常；仅结构性错误（未知函数、arity 错误、缺失 gate/file 解析器）抛 DslError。
- 语义严格对齐源版 dsl/evaluator.ts（三值真值表、typed 相等、any/all/count 隐式 lambda）。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass

from assurance_agent.exceptions import AaError


class DslError(AaError):
    """DSL 表达式非法或求值期结构性错误。"""


MAX_EXPR_LEN = 2000
MAX_DEPTH = 40

BUILTIN_ARITY: dict[str, int] = {
    "len": 1,
    "file_exists": 1,
    "defined": 1,
    "any": 2,
    "all": 2,
    "count": 2,
    "gate": 1,
}

_KEYWORD_LITERALS: dict[str, object] = {"true": True, "false": False, "null": None}
_COMPARE_OPS: dict[type[ast.cmpop], str] = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.In: "in",
    ast.NotIn: "not in",
}


@dataclass(frozen=True)
class Literal:
    value: object


@dataclass(frozen=True)
class ListLit:
    elements: tuple[object, ...]


@dataclass(frozen=True)
class Ident:
    name: str


@dataclass(frozen=True)
class Member:
    obj: Expr
    prop: str


@dataclass(frozen=True)
class Subscript:
    # 常量下标访问 `x['k']` / `x[0]`（Spec:77 白名单节点）。index 只允许
    # 字符串或整数常量——使带连字符的 key（如 state.phases['skill-registry-check']）
    # 可表达；成员访问 `x.k` 与 `x['k']` 语义等价。
    obj: Expr
    index: object


@dataclass(frozen=True)
class Compare:
    op: str
    left: Expr
    right: Expr


@dataclass(frozen=True)
class BoolOp:
    op: str  # "and" | "or"
    left: Expr
    right: Expr


@dataclass(frozen=True)
class Not:
    operand: Expr


@dataclass(frozen=True)
class Call:
    callee: str
    args: tuple[Expr, ...]


Expr = Literal | ListLit | Ident | Member | Subscript | Compare | BoolOp | Not | Call


def parse_expression(text: str) -> Expr:
    if len(text) > MAX_EXPR_LEN:
        raise DslError(f"expression too long ({len(text)} > {MAX_EXPR_LEN})")
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as exc:
        raise DslError(f"syntax error: {exc.msg}") from exc
    expr = _convert(tree.body, depth=0)
    return expr


def _convert(node: ast.expr, depth: int) -> Expr:
    if depth > MAX_DEPTH:
        raise DslError(f"expression nested too deeply (> {MAX_DEPTH})")
    d = depth + 1

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (str, int, float, bool)) or node.value is None:
            return Literal(node.value)
        raise DslError(f"unsupported constant: {node.value!r}")

    if isinstance(node, ast.Name):
        if node.id in _KEYWORD_LITERALS:
            return Literal(_KEYWORD_LITERALS[node.id])
        return Ident(node.id)

    if isinstance(node, ast.Attribute):
        return Member(_convert(node.value, d), node.attr)

    if isinstance(node, ast.Subscript):
        index_node = node.slice
        if not isinstance(index_node, ast.Constant) or not isinstance(index_node.value, (str, int)) or isinstance(
            index_node.value, bool
        ):
            raise DslError("subscript index must be a string or integer constant")
        return Subscript(_convert(node.value, d), index_node.value)

    if isinstance(node, ast.List):
        elements: list[object] = []
        for el in node.elts:
            lit = _convert(el, d)
            if not isinstance(lit, Literal):
                raise DslError("list literals may only contain literals")
            elements.append(lit.value)
        return ListLit(tuple(elements))

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return Not(_convert(node.operand, d))

    if isinstance(node, ast.BoolOp):
        op = "and" if isinstance(node.op, ast.And) else "or"
        # ast 把 a and b and c 折叠为多元；还原为左结合二元树
        parts = [_convert(v, d) for v in node.values]
        acc = parts[0]
        for right in parts[1:]:
            acc = BoolOp(op, acc, right)
        return acc

    if isinstance(node, ast.Compare):
        if len(node.ops) != 1 or len(node.comparators) != 1:
            raise DslError("chained comparisons are not allowed")
        op_type = type(node.ops[0])
        if op_type not in _COMPARE_OPS:
            raise DslError(f"comparison operator not allowed: {op_type.__name__}")
        return Compare(_COMPARE_OPS[op_type], _convert(node.left, d), _convert(node.comparators[0], d))

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise DslError("only bare-name function calls are allowed")
        callee = node.func.id
        if callee not in BUILTIN_ARITY:
            raise DslError(f"unknown function '{callee}' (not in the allow-list)")
        if node.keywords:
            raise DslError(f"function '{callee}' does not accept keyword arguments")
        args = tuple(_convert(a, d) for a in node.args)
        if len(args) != BUILTIN_ARITY[callee]:
            raise DslError(
                f"function '{callee}' expects {BUILTIN_ARITY[callee]} arg(s), got {len(args)}"
            )
        return Call(callee, args)

    raise DslError(f"expression node not allowed: {type(node).__name__}")


def _walk(expr: Expr) -> list[Expr]:
    out: list[Expr] = [expr]
    if isinstance(expr, Member):
        out += _walk(expr.obj)
    elif isinstance(expr, Subscript):
        out += _walk(expr.obj)
    elif isinstance(expr, (Compare, BoolOp)):
        out += _walk(expr.left) + _walk(expr.right)
    elif isinstance(expr, Not):
        out += _walk(expr.operand)
    elif isinstance(expr, Call):
        for a in expr.args:
            out += _walk(a)
    return out


def collect_calls(expr: Expr) -> list[Call]:
    return [n for n in _walk(expr) if isinstance(n, Call)]


def collect_gate_refs(expr: Expr) -> list[str]:
    refs: list[str] = []
    for call in collect_calls(expr):
        if call.callee == "gate" and call.args and isinstance(call.args[0], Literal):
            val = call.args[0].value
            if isinstance(val, str):
                refs.append(val)
    return refs
