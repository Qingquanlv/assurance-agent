"""AA 谓词小语言（DSL）：ast.parse 语法解析 + 白名单自解释 + 三值求值。

- 值模型：缺失路径 → MISSING（不是 None；None 是显式 null）。求值对缺失数据是全函数，
  不抛异常；仅结构性错误（未知函数、arity 错误、缺失 gate/file 解析器）抛 DslError。
- 语义严格对齐源版 dsl/evaluator.ts（三值真值表、typed 相等、any/all/count 隐式 lambda）。
"""

from __future__ import annotations

import ast
from collections.abc import Callable
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
    "capabilities_present": 2,
    "plan_review_route": 1,
    "any": 2,
    "all": 2,
    "count": 2,
    "gate": 1,
    "node": 1,
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
        if (
            not isinstance(index_node, ast.Constant)
            or not isinstance(index_node.value, (str, int))
            or isinstance(index_node.value, bool)
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
            raise DslError(f"function '{callee}' expects {BUILTIN_ARITY[callee]} arg(s), got {len(args)}")
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


class _Missing:
    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"


MISSING = _Missing()

FileExistsResolver = Callable[[str], bool]
GateResolver = Callable[[str], str]
NodeResolver = Callable[[str], object]
CapabilitiesPresentResolver = Callable[[object, object], bool]


class Scope:
    def __init__(
        self,
        vars: dict[str, object],
        *,
        file_exists: FileExistsResolver | None = None,
        gate_verdict: GateResolver | None = None,
        node_result: NodeResolver | None = None,
        capabilities_present: CapabilitiesPresentResolver | None = None,
    ) -> None:
        self._vars = vars
        self.file_exists = file_exists
        self.gate_verdict = gate_verdict
        self.node_result = node_result
        self.capabilities_present = capabilities_present

    def lookup(self, name: str) -> object:
        return self._vars[name] if name in self._vars else MISSING

    def child(self, element: object) -> Scope:
        base = dict(self._vars)
        if isinstance(element, dict):
            base.update(element)
        return Scope(
            base,
            file_exists=self.file_exists,
            gate_verdict=self.gate_verdict,
            node_result=self.node_result,
            capabilities_present=self.capabilities_present,
        )


def _to_bool(v: object) -> bool | None:
    if v is True:
        return True
    if v is False:
        return False
    return None  # MISSING/其它 → 三值 unknown


def _typed_eq(a: object, b: object) -> bool:
    if a is None or b is None:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    ta = "num" if isinstance(a, (int, float)) else type(a).__name__
    tb = "num" if isinstance(b, (int, float)) else type(b).__name__
    if ta != tb:
        return False
    return a == b


def evaluate(expr: Expr, scope: Scope) -> object:  # noqa: C901 - 语言解释器
    if isinstance(expr, Literal):
        return expr.value
    if isinstance(expr, ListLit):
        return list(expr.elements)
    if isinstance(expr, Ident):
        return scope.lookup(expr.name)
    if isinstance(expr, Member):
        obj = evaluate(expr.obj, scope)
        if not isinstance(obj, dict):
            return MISSING
        return obj[expr.prop] if expr.prop in obj else MISSING
    if isinstance(expr, Subscript):
        obj = evaluate(expr.obj, scope)
        idx = expr.index
        if isinstance(idx, str):
            return obj[idx] if isinstance(obj, dict) and idx in obj else MISSING
        # int 下标：list 越界/负数按缺失（fail-closed），不做 Python 负索引语义
        if isinstance(idx, int) and isinstance(obj, list) and 0 <= idx < len(obj):
            return obj[idx]
        return MISSING
    if isinstance(expr, Not):
        b = _to_bool(evaluate(expr.operand, scope))
        return MISSING if b is None else (not b)
    if isinstance(expr, BoolOp):
        return _eval_boolop(expr, scope)
    if isinstance(expr, Compare):
        return _eval_compare(expr, scope)
    if isinstance(expr, Call):
        return _eval_call(expr, scope)
    raise DslError(f"cannot evaluate node: {expr!r}")


def _eval_boolop(expr: BoolOp, scope: Scope) -> object:
    a = _to_bool(evaluate(expr.left, scope))
    if expr.op == "and":
        if a is False:
            return False
        b = _to_bool(evaluate(expr.right, scope))
        if a is True:
            return MISSING if b is None else b
        return False if b is False else MISSING  # a is None
    # or
    if a is True:
        return True
    b = _to_bool(evaluate(expr.right, scope))
    if a is False:
        return MISSING if b is None else b
    return True if b is True else MISSING  # a is None


def _eval_compare(expr: Compare, scope: Scope) -> object:
    if expr.op in ("in", "not in"):
        left = evaluate(expr.left, scope)
        right = evaluate(expr.right, scope)
        if left is MISSING or not isinstance(right, list):
            return MISSING
        found = any(_typed_eq(left, el) for el in right)
        return found if expr.op == "in" else (not found)

    left = evaluate(expr.left, scope)
    right = evaluate(expr.right, scope)
    if left is MISSING or right is MISSING:
        return MISSING
    if expr.op == "==":
        return _typed_eq(left, right)
    if expr.op == "!=":
        return not _typed_eq(left, right)
    both_num = (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and isinstance(right, (int, float))
        and not isinstance(right, bool)
    )
    both_str = isinstance(left, str) and isinstance(right, str)
    if not both_num and not both_str:
        return MISSING
    if expr.op == "<":
        return left < right  # type: ignore[operator]
    if expr.op == "<=":
        return left <= right  # type: ignore[operator]
    if expr.op == ">":
        return left > right  # type: ignore[operator]
    return left >= right  # type: ignore[operator]


def _eval_call(expr: Call, scope: Scope) -> object:
    callee = expr.callee
    if callee == "len":
        v = evaluate(expr.args[0], scope)
        if isinstance(v, (str, list, dict)):
            return len(v)
        return MISSING
    if callee == "defined":
        return evaluate(expr.args[0], scope) is not MISSING
    if callee == "file_exists":
        p = evaluate(expr.args[0], scope)
        if not isinstance(p, str):
            return MISSING
        if scope.file_exists is None:
            raise DslError("file_exists() called but no resolver was provided")
        return scope.file_exists(p)
    if callee == "capabilities_present":
        review_doc = evaluate(expr.args[0], scope)
        dk_doc = evaluate(expr.args[1], scope)
        if scope.capabilities_present is None:
            raise DslError("capabilities_present() called but no resolver was provided")
        return scope.capabilities_present(review_doc, dk_doc)
    if callee == "plan_review_route":
        from assurance_agent.knowledge.capabilities import plan_review_route as resolve_plan_review_route

        node_id = evaluate(expr.args[0], scope)
        if not isinstance(node_id, str):
            return MISSING
        if scope.node_result is None:
            raise DslError("plan_review_route() called but no node_result resolver was provided")
        return resolve_plan_review_route(scope.node_result(node_id))
    if callee == "gate":
        gid = evaluate(expr.args[0], scope)
        if not isinstance(gid, str):
            return MISSING
        if scope.gate_verdict is None:
            raise DslError("gate() called but no resolver was provided")
        return {"verdict": scope.gate_verdict(gid)}
    if callee == "node":
        node_id = evaluate(expr.args[0], scope)
        if not isinstance(node_id, str):
            return MISSING
        if scope.node_result is None:
            raise DslError("node() called but no resolver was provided")
        return scope.node_result(node_id)
    # any / all / count
    coll = evaluate(expr.args[0], scope)
    pred = expr.args[1]
    if not isinstance(coll, list):
        return MISSING
    if callee == "count":
        return sum(1 for el in coll if _to_bool(evaluate(pred, scope.child(el))) is True)
    saw_missing = False
    if callee == "all":
        for el in coll:
            r = _to_bool(evaluate(pred, scope.child(el)))
            if r is False:
                return False
            if r is None:
                saw_missing = True
        return MISSING if saw_missing else True
    # any
    for el in coll:
        r = _to_bool(evaluate(pred, scope.child(el)))
        if r is True:
            return True
        if r is None:
            saw_missing = True
    return MISSING if saw_missing else False


def is_satisfied(expr: Expr, scope: Scope) -> bool:
    return evaluate(expr, scope) is True
