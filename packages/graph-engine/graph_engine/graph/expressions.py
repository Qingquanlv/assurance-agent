from __future__ import annotations

import ast
import math
from collections.abc import Mapping
from typing import cast

from graph_engine.canonical import JSONValue
from graph_engine.errors import GraphEngineError


class ExpressionError(GraphEngineError, ValueError):
    """Raised when an expression is outside the closed expression language."""


def evaluate_expression(expression: str, scope: Mapping[str, JSONValue]) -> JSONValue:
    if len(expression) > 2_048:
        raise ExpressionError("expression exceeds the 2,048 character limit")
    try:
        parsed = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise ExpressionError("invalid expression syntax") from error
    return cast(JSONValue, _evaluate(parsed.body, scope))


def _evaluate(node: ast.expr, scope: Mapping[str, JSONValue]) -> object:
    if isinstance(node, ast.Constant):
        value = node.value
        if not isinstance(value, None | bool | int | float | str):
            raise ExpressionError(f"unsupported constant: {type(value).__name__}")
        if isinstance(value, float) and not math.isfinite(value):
            raise ExpressionError("non-finite numbers are not supported")
        return value

    if isinstance(node, ast.Name):
        literals: dict[str, JSONValue] = {"true": True, "false": False, "null": None}
        if node.id in literals:
            return literals[node.id]
        if node.id not in scope:
            raise ExpressionError(f"unknown name: {node.id}")
        return scope[node.id]

    if isinstance(node, ast.Attribute):
        if node.attr.startswith("_"):
            raise ExpressionError("attributes beginning with '_' are not allowed")
        container = _evaluate(node.value, scope)
        if not isinstance(container, Mapping):
            raise ExpressionError("attribute lookup requires a dictionary")
        if node.attr not in container:
            raise ExpressionError(f"unknown dictionary name: {node.attr}")
        return container[node.attr]

    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And | ast.Or):
        values = node.values
        if isinstance(node.op, ast.And):
            result = _evaluate(values[0], scope)
            for value in values[1:]:
                if not result:
                    return result
                result = _evaluate(value, scope)
            return result
        result = _evaluate(values[0], scope)
        for value in values[1:]:
            if result:
                return result
            result = _evaluate(value, scope)
        return result

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _evaluate(node.operand, scope)

    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, scope)
        for operator, comparator in zip(node.ops, node.comparators, strict=True):
            right = _evaluate(comparator, scope)
            if not _compare(operator, left, right):
                return False
            left = right
        return True

    raise ExpressionError(f"unsupported expression syntax: {type(node).__name__}")


def _compare(operator: ast.cmpop, left: object, right: object) -> bool:
    try:
        if isinstance(operator, ast.Eq):
            return left == right
        if isinstance(operator, ast.NotEq):
            return left != right
        if isinstance(operator, ast.Lt):
            return left < right  # type: ignore[operator]
        if isinstance(operator, ast.LtE):
            return left <= right  # type: ignore[operator]
        if isinstance(operator, ast.Gt):
            return left > right  # type: ignore[operator]
        if isinstance(operator, ast.GtE):
            return left >= right  # type: ignore[operator]
        if isinstance(operator, ast.In):
            return left in right  # type: ignore[operator]
    except (TypeError, ValueError) as error:
        raise ExpressionError("comparison operands are incompatible") from error
    raise ExpressionError(f"unsupported comparison operator: {type(operator).__name__}")


__all__ = ["ExpressionError", "evaluate_expression"]
