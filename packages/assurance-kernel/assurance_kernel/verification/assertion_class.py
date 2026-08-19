"""API and E2E assertion-strength classifiers (§5-B2).

Two separate taxonomies over two assertion populations — never average them
into one number. This module is the reusable classifier M2's
``compute-assertion-strength`` must call; do not invent a second AST taxonomy.

Weak assertions never count as a covering oracle for A2/A4 (``counts_as_covered_oracle``).
Task 6's constraint/journey covered join is the consumer of that gate.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Literal

AssertionStrength = Literal["strong", "weak"]
AssertionSurface = Literal["api", "e2e"]

_HELPER_PREFIXES = ("assert_", "check_", "verify_", "ensure_")
_STATUS_200_VALUES = frozenset({200, "200"})
_E2E_VISIBILITY_ATTRS = frozenset(
    {
        "is_visible",
        "to_be_visible",
        "to_be_attached",
        "to_be_hidden",
        "to_be_enabled",
        "to_be_disabled",
    }
)
_E2E_PAGE_LOAD_ATTRS = frozenset(
    {
        "goto",
        "wait_for_load_state",
        "wait_for_url",
        "reload",
    }
)
_E2E_STRONG_ATTRS = frozenset(
    {
        "to_contain_text",
        "to_have_text",
        "to_have_value",
        "to_have_count",
        "to_have_attribute",
        "to_have_url",
        "inner_text",
        "text_content",
        "input_value",
        "count",
    }
)


@dataclass(frozen=True)
class AssertionClassification:
    strength: AssertionStrength
    surface: AssertionSurface
    reasons: tuple[str, ...]


def counts_as_covered_oracle(classification: AssertionClassification) -> bool:
    """Whether this classification may contribute to A2/A4 covered (Task 6 join)."""
    return classification.strength == "strong"


def classify_api_assertions(source: str, *, function_name: str | None = None) -> AssertionClassification:
    body = _function_body(source, function_name)
    if body is None:
        return AssertionClassification(strength="weak", surface="api", reasons=("unparseable",))

    asserts = list(_iter_assert_tests(body))
    helper_calls = list(_iter_helper_calls(body))

    if not asserts and helper_calls:
        return AssertionClassification(strength="weak", surface="api", reasons=("helper_only",))
    if not asserts:
        return AssertionClassification(strength="weak", surface="api", reasons=("no_assertions",))

    reasons: list[str] = []
    has_strong = False
    status_200_only_candidates = 0
    for test in asserts:
        kind = _classify_api_assert_test(test)
        if kind == "strong":
            has_strong = True
        elif kind == "constant":
            reasons.append("constant")
        elif kind == "status_200":
            status_200_only_candidates += 1
            reasons.append("status_200")
        elif kind == "helper":
            reasons.append("helper_only")
        else:
            # §5-B2 fail-closed: unlisted / unknown assert shapes are weak.
            reasons.append("other")

    if has_strong:
        return AssertionClassification(strength="strong", surface="api", reasons=("business_predicate",))

    if asserts and status_200_only_candidates == len(asserts):
        return AssertionClassification(strength="weak", surface="api", reasons=("status_200_only",))

    if asserts and all(r == "helper_only" for r in reasons):
        return AssertionClassification(strength="weak", surface="api", reasons=("helper_only",))

    if reasons:
        # Deduplicate while preserving order.
        ordered = tuple(dict.fromkeys(reasons))
        return AssertionClassification(strength="weak", surface="api", reasons=ordered)

    return AssertionClassification(strength="weak", surface="api", reasons=("no_strong_predicate",))


def classify_e2e_assertions(source: str, *, function_name: str | None = None) -> AssertionClassification:
    body = _function_body(source, function_name)
    if body is None:
        return AssertionClassification(strength="weak", surface="e2e", reasons=("unparseable",))

    signals = _collect_e2e_signals(body)
    if signals.strong:
        reason = "cross_page_postcondition" if signals.goto_count >= 2 else "business_postcondition"
        return AssertionClassification(strength="strong", surface="e2e", reasons=(reason,))

    reasons: list[str] = []
    if signals.visibility:
        reasons.append("visibility_only")
    if signals.page_load and not signals.visibility:
        reasons.append("page_load_only")
    elif signals.page_load and signals.visibility:
        reasons.append("page_load_only")

    if not reasons:
        reasons.append("no_assertions")

    return AssertionClassification(strength="weak", surface="e2e", reasons=tuple(dict.fromkeys(reasons)))


def _function_body(source: str, function_name: str | None) -> list[ast.stmt] | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    if function_name is None:
        return list(tree.body)
    qualified_parts = function_name.split("::")
    if len(qualified_parts) > 1:
        body: list[ast.stmt] = list(tree.body)
        for index, part in enumerate(qualified_parts):
            is_last = index == len(qualified_parts) - 1
            expected = (ast.FunctionDef, ast.AsyncFunctionDef) if is_last else (ast.ClassDef,)
            match = next(
                (node for node in body if isinstance(node, expected) and node.name == part),
                None,
            )
            if match is None:
                return None
            body = list(match.body)
        return body
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return list(node.body)
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == function_name:
                    return list(item.body)
    return None


def _iter_assert_tests(body: list[ast.stmt]) -> list[ast.expr]:
    tests: list[ast.expr] = []
    for node in body:
        for child in ast.walk(node):
            if isinstance(child, ast.Assert) and child.test is not None:
                tests.append(child.test)
    return tests


def _iter_helper_calls(body: list[ast.stmt]) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in body:
        for child in ast.walk(node):
            if not isinstance(child, ast.Expr) or not isinstance(child.value, ast.Call):
                continue
            name = _call_name(child.value)
            if name and name.startswith(_HELPER_PREFIXES):
                calls.append(child.value)
    return calls


def _classify_api_assert_test(
    test: ast.expr,
) -> Literal["constant", "status_200", "helper", "strong", "other"]:
    if _is_constant_predicate(test):
        return "constant"
    if _is_status_200_predicate(test):
        return "status_200"
    if isinstance(test, ast.Call):
        name = _call_name(test)
        if name and name.startswith(_HELPER_PREFIXES):
            return "helper"
    if _looks_like_business_predicate(test):
        return "strong"
    return "other"


def _is_constant_predicate(test: ast.expr) -> bool:
    if isinstance(test, ast.Constant):
        return True
    if isinstance(test, ast.Compare):
        operands = [test.left, *test.comparators]
        return all(isinstance(op, ast.Constant) for op in operands)
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _is_constant_predicate(test.operand)
    if isinstance(test, ast.BoolOp):
        return all(_is_constant_predicate(v) for v in test.values)
    return False


def _is_status_200_predicate(test: ast.expr) -> bool:
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq):
        left, right = test.left, test.comparators[0]
        return (_is_status_attr(left) and _is_200(right)) or (_is_status_attr(right) and _is_200(left))
    return False


def _is_status_attr(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr in {"status_code", "status"}


def _is_200(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value in _STATUS_200_VALUES


def _looks_like_business_predicate(test: ast.expr) -> bool:
    """Field/body/detail comparisons and non-200 status checks count as business."""
    for node in ast.walk(test):
        if isinstance(node, ast.Subscript):
            return True
        if isinstance(node, ast.Attribute) and node.attr in {
            "json",
            "text",
            "detail",
            "message",
            "code",
            "data",
            "name",
            "id",
            "error",
        }:
            return True
        if isinstance(node, ast.Compare) and _is_status_attr(node.left):
            # Non-200 status is a business rejection predicate.
            if not any(_is_200(c) for c in node.comparators):
                return True
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name in {"json", "get_json"}:
                return True
    return False


@dataclass(frozen=True)
class _E2ESignals:
    visibility: bool
    page_load: bool
    strong: bool
    goto_count: int


def _collect_e2e_signals(body: list[ast.stmt]) -> _E2ESignals:
    visibility = False
    page_load = False
    strong = False
    goto_count = 0
    for node in body:
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                attr = _call_attr(child)
                if attr in _E2E_VISIBILITY_ATTRS:
                    visibility = True
                if attr in _E2E_PAGE_LOAD_ATTRS:
                    page_load = True
                    if attr == "goto":
                        goto_count += 1
                if attr in _E2E_STRONG_ATTRS:
                    strong = True
            if isinstance(child, ast.Assert):
                # ``assert await locator.count() == 1`` etc.
                if child.test is not None and _assert_has_e2e_strong(child.test):
                    strong = True
                elif child.test is not None and _assert_has_visibility(child.test):
                    visibility = True
    # Cross-page: ≥2 gotos with any visibility/text check is a postcondition.
    if goto_count >= 2 and (visibility or strong):
        strong = True
    return _E2ESignals(
        visibility=visibility,
        page_load=page_load,
        strong=strong,
        goto_count=goto_count,
    )


def _assert_has_e2e_strong(test: ast.expr) -> bool:
    for node in ast.walk(test):
        if isinstance(node, ast.Call) and _call_attr(node) in _E2E_STRONG_ATTRS:
            return True
        if isinstance(node, ast.Attribute) and node.attr in {"count", "inner_text", "text_content"}:
            return True
    return False


def _assert_has_visibility(test: ast.expr) -> bool:
    for node in ast.walk(test):
        if isinstance(node, ast.Call) and _call_attr(node) in _E2E_VISIBILITY_ATTRS:
            return True
        if isinstance(node, ast.Attribute) and node.attr == "is_visible":
            return True
    return False


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _call_attr(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


__all__ = [
    "AssertionClassification",
    "AssertionStrength",
    "AssertionSurface",
    "classify_api_assertions",
    "classify_e2e_assertions",
    "counts_as_covered_oracle",
]
