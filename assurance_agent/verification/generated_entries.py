"""Strict per-layer plan/case mapping extraction and selected-test AST classifiers."""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from assurance_agent.artifacts.models.assurance import LayerName
from assurance_agent.exceptions import AaError
from assurance_agent.verification.applicability import derive_layer_applicability
from assurance_agent.verification.checks.base import table_rows
from assurance_agent.verification.profiles import get_layer_assurance_profile

MappingErrorCode = Literal[
    "missing_mapping",
    "duplicated_mapping",
    "interrupted_mapping",
    "malformed_mapping",
    "wrong_layer_mapping",
    "unmapped_table",
    "missing_schema_acquisition",
    "schema_case_mismatch",
]

GeneratedEntryReason = Literal[
    "accepted",
    "parse_error",
    "unmapped_symbol",
    "wrong_case_symbol",
    "behaviorless_assignment",
    "behaviorless_return",
    "behaviorless_assert_true",
    "behaviorless_constant_compare",
    "decorator_only",
    "helper_only",
    "missing_client_request",
    "missing_response_assertion",
    "missing_navigation",
    "missing_interaction",
    "missing_page_assertion",
    "missing_schema_binding",
    "missing_schema_call",
    "missing_task_decorator",
    "missing_user_ownership",
    "missing_client_request_in_task",
]

_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*$")
_BACKTICK_RE = re.compile(r"^`([^`]+)`$")
_TRAILING_HEADING_QUALIFIER_RE = re.compile(r"\s+\([^()]*\)\s*$")

_DEFAULT_HTTP_METHODS: Final[frozenset[str]] = frozenset(
    {"get", "post", "put", "patch", "delete", "request", "head", "options"}
)
_DEFAULT_CLIENT_NAMES: Final[frozenset[str]] = frozenset(
    {"client", "api_client", "http_client", "authenticated_client", "session"}
)
_DEFAULT_SCHEMA_CALLS: Final[frozenset[str]] = frozenset({"call_and_validate"})
_DEFAULT_SCHEMA_DECORATORS: Final[frozenset[str]] = frozenset({"schema.parametrize", "parametrize"})
_DEFAULT_USER_BASES: Final[frozenset[str]] = frozenset({"HttpUser", "FastHttpUser"})
_DEFAULT_TASK_DECORATORS: Final[frozenset[str]] = frozenset({"task"})
_DEFAULT_NAVIGATION: Final[frozenset[str]] = frozenset({"goto", "go_back", "reload"})
_DEFAULT_INTERACTION: Final[frozenset[str]] = frozenset(
    {"click", "fill", "type", "press", "check", "uncheck", "select_option", "hover"}
)
_DEFAULT_EXPECT: Final[frozenset[str]] = frozenset({"expect"})


class MappingExtractionError(AaError):
    """Plan/case mapping cannot be derived under the selected layer contract."""

    def __init__(self, code: MappingErrorCode, message: str) -> None:
        self.code: MappingErrorCode = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class LayerBehavioralPolicy:
    """Closed registered client/schema-call forms for behavioral AST classification."""

    layer: LayerName
    client_names: frozenset[str]
    http_methods: frozenset[str]
    schema_call_attrs: frozenset[str]
    schema_decorator_names: frozenset[str]
    user_base_names: frozenset[str]
    task_decorator_names: frozenset[str]
    navigation_attrs: frozenset[str]
    interaction_attrs: frozenset[str]
    expect_names: frozenset[str]


def behavioral_policy_for_layer(layer: str) -> LayerBehavioralPolicy:
    """Return the closed behavioral policy for one assurance layer."""
    profile = get_layer_assurance_profile(layer)
    return LayerBehavioralPolicy(
        layer=profile.layer,
        client_names=_DEFAULT_CLIENT_NAMES,
        http_methods=_DEFAULT_HTTP_METHODS,
        schema_call_attrs=_DEFAULT_SCHEMA_CALLS,
        schema_decorator_names=_DEFAULT_SCHEMA_DECORATORS,
        user_base_names=_DEFAULT_USER_BASES,
        task_decorator_names=_DEFAULT_TASK_DECORATORS,
        navigation_attrs=_DEFAULT_NAVIGATION,
        interaction_attrs=_DEFAULT_INTERACTION,
        expect_names=_DEFAULT_EXPECT,
    )


@dataclass(frozen=True, slots=True)
class MappedTestEntry:
    case_id: str
    symbol: str
    target_file: str


@dataclass(frozen=True, slots=True)
class LayerMappingRelation:
    layer: LayerName
    entries: tuple[MappedTestEntry, ...]
    selected_case_ids: tuple[str, ...]
    schema_case_ids: tuple[str, ...] = ()
    behavioral_policy: LayerBehavioralPolicy | None = None

    def policy(self) -> LayerBehavioralPolicy:
        if self.behavioral_policy is not None:
            return self.behavioral_policy
        return behavioral_policy_for_layer(self.layer)


@dataclass(frozen=True, slots=True)
class GeneratedEntryDecision:
    accepted: bool
    layer: LayerName
    case_id: str | None
    symbol: str | None
    target_file: str | None
    reason_code: GeneratedEntryReason


def extract_layer_mapping(
    *,
    layer: str,
    plan_text: str,
    cases: Sequence[Mapping[str, object]],
) -> LayerMappingRelation:
    """Derive the closed Case ID → symbol → Target File relation for one layer."""
    profile = get_layer_assurance_profile(layer)
    selected = derive_layer_applicability(cases, profile).case_ids
    policy = behavioral_policy_for_layer(profile.layer)
    if layer in {"api", "e2e"}:
        entries = _extract_named_mapping(
            plan_text,
            section="Test Function Mapping",
            required_headers=("case id", "test function", "target file"),
            layer=layer,
        )
        return LayerMappingRelation(
            layer=profile.layer,
            entries=entries,
            selected_case_ids=selected,
            behavioral_policy=policy,
        )
    if layer == "fuzz":
        entries = _extract_named_mapping(
            plan_text,
            section="Test Function Mapping",
            required_headers=("case id", "test function", "target file"),
            layer=layer,
        )
        schema_ids = _extract_schema_acquisition_case_ids(plan_text)
        mapped_ids = tuple(sorted({entry.case_id for entry in entries}))
        if schema_ids != mapped_ids:
            raise MappingExtractionError(
                "schema_case_mismatch",
                "Schema Acquisition case IDs must equal Test Function Mapping case IDs",
            )
        return LayerMappingRelation(
            layer=profile.layer,
            entries=entries,
            selected_case_ids=selected,
            schema_case_ids=schema_ids,
            behavioral_policy=policy,
        )
    if layer == "performance":
        entries = _extract_named_mapping(
            plan_text,
            section="Task Mapping",
            required_headers=("case id", "task method", "target file"),
            layer=layer,
        )
        return LayerMappingRelation(
            layer=profile.layer,
            entries=entries,
            selected_case_ids=selected,
            behavioral_policy=policy,
        )
    raise MappingExtractionError("wrong_layer_mapping", f"unsupported assurance layer: {layer}")


def mapped_case_ids_for_path(relation: LayerMappingRelation, repo_path: str) -> tuple[str, ...]:
    """Return selected automated case IDs mapped to ``repo_path`` (canonical order)."""
    selected = set(relation.selected_case_ids)
    ids = sorted(
        {
            entry.case_id
            for entry in relation.entries
            if entry.target_file == repo_path and entry.case_id in selected
        }
    )
    return tuple(ids)


def selected_private_root_targets(relation: LayerMappingRelation, private_root: str) -> frozenset[str]:
    """Exact mapped target files under the selected private root."""
    prefix = private_root.rstrip("/") + "/"
    return frozenset(
        entry.target_file
        for entry in relation.entries
        if entry.case_id in set(relation.selected_case_ids)
        and (entry.target_file == private_root or entry.target_file.startswith(prefix))
    )


def classify_generated_entry(
    *,
    layer: str,
    source: str,
    entry: MappedTestEntry,
    policy: LayerBehavioralPolicy | None = None,
) -> GeneratedEntryDecision:
    """Dispatch one layer-specific AST classifier for a mapped selected entry."""
    resolved = policy or behavioral_policy_for_layer(layer)
    if layer == "api":
        return classify_api_entry(source, entry=entry, policy=resolved)
    if layer == "e2e":
        return classify_e2e_entry(source, entry=entry, policy=resolved)
    if layer == "fuzz":
        return classify_fuzz_entry(source, entry=entry, policy=resolved)
    if layer == "performance":
        return classify_performance_entry(source, entry=entry, policy=resolved)
    return GeneratedEntryDecision(
        accepted=False,
        layer=resolved.layer,
        case_id=entry.case_id,
        symbol=entry.symbol,
        target_file=entry.target_file,
        reason_code="unmapped_symbol",
    )


def classify_api_entry(
    source: str,
    *,
    entry: MappedTestEntry,
    policy: LayerBehavioralPolicy | None = None,
) -> GeneratedEntryDecision:
    resolved = policy or behavioral_policy_for_layer("api")
    return _classify_with(
        source,
        entry=entry,
        policy=resolved,
        checker=_api_behavior_ok,
    )


def classify_e2e_entry(
    source: str,
    *,
    entry: MappedTestEntry,
    policy: LayerBehavioralPolicy | None = None,
) -> GeneratedEntryDecision:
    resolved = policy or behavioral_policy_for_layer("e2e")
    return _classify_with(
        source,
        entry=entry,
        policy=resolved,
        checker=_e2e_behavior_ok,
    )


def classify_fuzz_entry(
    source: str,
    *,
    entry: MappedTestEntry,
    policy: LayerBehavioralPolicy | None = None,
) -> GeneratedEntryDecision:
    resolved = policy or behavioral_policy_for_layer("fuzz")
    return _classify_with(
        source,
        entry=entry,
        policy=resolved,
        checker=_fuzz_behavior_ok,
    )


def classify_performance_entry(
    source: str,
    *,
    entry: MappedTestEntry,
    policy: LayerBehavioralPolicy | None = None,
) -> GeneratedEntryDecision:
    resolved = policy or behavioral_policy_for_layer("performance")
    return _classify_with(
        source,
        entry=entry,
        policy=resolved,
        checker=_performance_behavior_ok,
    )


def _classify_with(
    source: str,
    *,
    entry: MappedTestEntry,
    policy: LayerBehavioralPolicy,
    checker: object,
) -> GeneratedEntryDecision:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _decision(policy.layer, entry, False, "parse_error")

    fn = _find_function(tree, entry.symbol)
    if fn is None:
        if _file_has_only_helpers(tree):
            return _decision(policy.layer, entry, False, "helper_only")
        return _decision(policy.layer, entry, False, "unmapped_symbol")

    if policy.layer in {"api", "e2e", "fuzz"} and not entry.symbol.startswith("test_"):
        return _decision(policy.layer, entry, False, "wrong_case_symbol")

    behaviorless = _behaviorless_reason(fn)
    if behaviorless is not None:
        return _decision(policy.layer, entry, False, behaviorless)

    reason = checker(tree, fn, policy)  # type: ignore[operator]
    if reason == "accepted":
        return _decision(policy.layer, entry, True, "accepted")
    return _decision(policy.layer, entry, False, reason)


def _decision(
    layer: LayerName,
    entry: MappedTestEntry,
    accepted: bool,
    reason: GeneratedEntryReason,
) -> GeneratedEntryDecision:
    return GeneratedEntryDecision(
        accepted=accepted,
        layer=layer,
        case_id=entry.case_id,
        symbol=entry.symbol,
        target_file=entry.target_file,
        reason_code=reason,
    )


def _find_function(tree: ast.AST, symbol: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol:
            return node
    return None


def _file_has_only_helpers(tree: ast.AST) -> bool:
    defs = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not defs:
        return False
    return all(not node.name.startswith("test_") for node in defs)


def _behaviorless_reason(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
) -> GeneratedEntryReason | None:
    body = [stmt for stmt in fn.body if not isinstance(stmt, ast.Pass)]
    # docstring-only / empty
    if not body:
        return "decorator_only"
    if len(body) == 1 and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        return "decorator_only"
    if all(isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)) for stmt in body):
        return "behaviorless_assignment"
    if all(isinstance(stmt, ast.Return) for stmt in body):
        return "behaviorless_return"
    if _only_assert_true(body):
        return "behaviorless_assert_true"
    if _only_constant_compare(body):
        return "behaviorless_constant_compare"
    # Decorator-only: function body is pass/docstring and decorators exist.
    nontrivial = [
        stmt
        for stmt in body
        if not (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        )
    ]
    if not nontrivial and fn.decorator_list:
        return "decorator_only"
    return None


def _only_assert_true(body: list[ast.stmt]) -> bool:
    asserts = [stmt for stmt in body if isinstance(stmt, ast.Assert)]
    if len(asserts) != len(body) or not asserts:
        return False
    for stmt in asserts:
        test = stmt.test
        if isinstance(test, ast.Constant) and test.value is True:
            continue
        if isinstance(test, ast.Name) and test.id in {"True"}:
            continue
        return False
    return True


def _only_constant_compare(body: list[ast.stmt]) -> bool:
    compares: list[ast.Compare] = []
    for stmt in body:
        if isinstance(stmt, ast.Assert) and isinstance(stmt.test, ast.Compare):
            compares.append(stmt.test)
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Compare):
            compares.append(stmt.value)
        else:
            return False
    if not compares:
        return False
    for compare in compares:
        if not _is_constant_expr(compare.left):
            return False
        if not all(_is_constant_expr(comp) for comp in compare.comparators):
            return False
    return True


def _is_constant_expr(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Not)):
        return _is_constant_expr(node.operand)
    if isinstance(node, ast.Tuple):
        return all(_is_constant_expr(elt) for elt in node.elts)
    return False


def _api_behavior_ok(
    tree: ast.Module,
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    policy: LayerBehavioralPolicy,
) -> GeneratedEntryReason:
    response_names = _collect_client_response_names(fn, policy)
    if not response_names:
        return "missing_client_request"
    if not _has_response_dependent_assertion(fn, response_names):
        return "missing_response_assertion"
    return "accepted"


def _e2e_behavior_ok(
    tree: ast.Module,
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    policy: LayerBehavioralPolicy,
) -> GeneratedEntryReason:
    if not _has_attr_call(fn, policy.navigation_attrs):
        return "missing_navigation"
    if not _has_attr_call(fn, policy.interaction_attrs):
        return "missing_interaction"
    if not _has_page_dependent_assertion(fn, policy):
        return "missing_page_assertion"
    return "accepted"


def _fuzz_behavior_ok(
    tree: ast.Module,
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    policy: LayerBehavioralPolicy,
) -> GeneratedEntryReason:
    if not _has_schema_binding(fn, policy):
        return "missing_schema_binding"
    case_params = {arg.arg for arg in fn.args.args} | {arg.arg for arg in fn.args.kwonlyargs}
    if "case" not in case_params and "testcase" not in case_params:
        return "missing_schema_binding"
    if not _has_schema_call(fn, policy):
        return "missing_schema_call"
    return "accepted"


def _performance_behavior_ok(
    tree: ast.Module,
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    policy: LayerBehavioralPolicy,
) -> GeneratedEntryReason:
    owner = _owning_user_class(tree, fn, policy)
    if owner is None:
        return "missing_user_ownership"
    if not _has_task_decorator(fn, policy):
        return "missing_task_decorator"
    if not _has_self_client_request(fn, policy):
        return "missing_client_request_in_task"
    return "accepted"


def _collect_client_response_names(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    policy: LayerBehavioralPolicy,
) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and _is_client_http_call(node.value, policy):
                names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None and _is_client_http_call(node.value, policy):
                names.add(node.target.id)
    # Also accept await client.get(...)
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value
            if isinstance(value, ast.Await) and _is_client_http_call(value.value, policy):
                names.add(node.targets[0].id)
    return names


def _is_client_http_call(node: ast.AST, policy: LayerBehavioralPolicy) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in policy.http_methods:
        if isinstance(func.value, ast.Name) and func.value.id in policy.client_names:
            return True
        if isinstance(func.value, ast.Attribute) and func.value.attr in policy.client_names:
            return True
    return False


def _has_response_dependent_assertion(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    response_names: set[str],
) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            if _names_used(node.test) & response_names and not _is_constant_expr(node.test):
                return True
        if isinstance(node, ast.Call):
            # pytest-style assert via helper using response
            if _names_used(node) & response_names:
                func = node.func
                if isinstance(func, ast.Name) and func.id in {"assert_that", "assertEqual"}:
                    return True
                if isinstance(func, ast.Attribute) and func.attr in {"assert_status", "assert_ok"}:
                    return True
    return False


def _has_attr_call(fn: ast.AST, attrs: frozenset[str]) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in attrs:
                return True
    return False


def _has_page_dependent_assertion(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    policy: LayerBehavioralPolicy,
) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            if _uses_page_or_locator(node.test) and not _is_constant_expr(node.test):
                return True
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in policy.expect_names:
                return True
            if isinstance(func, ast.Attribute) and func.attr in {
                "to_have_url",
                "to_be_visible",
                "to_have_text",
            }:
                return True
    return False


def _uses_page_or_locator(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in {"page", "locator", "frame"}:
            return True
        if isinstance(child, ast.Attribute) and child.attr in {
            "url",
            "title",
            "inner_text",
            "text_content",
            "is_visible",
            "locator",
        }:
            return True
    return False


def _has_schema_binding(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    policy: LayerBehavioralPolicy,
) -> bool:
    for decorator in fn.decorator_list:
        text = _decorator_qualname(decorator)
        if text in policy.schema_decorator_names:
            return True
        if text.endswith(".parametrize") and "schema" in text:
            return True
    return False


def _decorator_qualname(node: ast.AST) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    parts: list[str] = []
    current: ast.AST | None = target
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _has_schema_call(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    policy: LayerBehavioralPolicy,
) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in policy.schema_call_attrs:
            if isinstance(func.value, ast.Name) and func.value.id in {"case", "testcase"}:
                return True
        if isinstance(func, ast.Name) and func.id in policy.schema_call_attrs:
            return True
    return False


def _owning_user_class(
    tree: ast.Module,
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    policy: LayerBehavioralPolicy,
) -> ast.ClassDef | None:
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(_base_name(base) in policy.user_base_names for base in node.bases):
            continue
        for item in node.body:
            if item is fn:
                return node
    return None


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _has_task_decorator(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    policy: LayerBehavioralPolicy,
) -> bool:
    for decorator in fn.decorator_list:
        name = _decorator_qualname(decorator)
        if name in policy.task_decorator_names or name.endswith(".task"):
            short = name.rsplit(".", 1)[-1]
            if short in policy.task_decorator_names:
                return True
    return False


def _has_self_client_request(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    policy: LayerBehavioralPolicy,
) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in policy.http_methods:
            continue
        # self.client.get(...)
        if (
            isinstance(func.value, ast.Attribute)
            and func.value.attr == "client"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "self"
        ):
            return True
    return False


def _names_used(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def _extract_named_mapping(
    plan_text: str,
    *,
    section: str,
    required_headers: tuple[str, ...],
    layer: str,
) -> tuple[MappedTestEntry, ...]:
    section_text = _extract_section(plan_text, section)
    if section_text is None:
        raise MappingExtractionError("missing_mapping", f"missing section: {section}")
    header: list[str] | None = None
    header_line: int | None = None
    rows: list[tuple[int, list[str]]] = []
    for lineno, cells in table_rows(section_text):
        lowered = [cell.lower() for cell in cells]
        if header is None and all(name in lowered for name in required_headers):
            header = lowered
            header_line = lineno
            continue
        if header is None:
            # A Case ID table that is not the required mapping is unmapped for this layer.
            if "case id" in lowered:
                raise MappingExtractionError(
                    "unmapped_table",
                    f"found Case ID table that is not {section} for layer {layer}",
                )
            continue
        if "case id" in lowered:
            raise MappingExtractionError(
                "interrupted_mapping",
                f"mapping table interrupted by a new header at line {lineno}",
            )
        rows.append((lineno, cells))

    if header is None or header_line is None:
        raise MappingExtractionError(
            "missing_mapping",
            f"{section} has no table with headers {required_headers}",
        )

    indexes = {name: header.index(name) for name in required_headers}
    width = max(indexes.values()) + 1
    seen_case: set[str] = set()
    seen_file_symbol: set[tuple[str, str]] = set()
    entries: list[MappedTestEntry] = []
    for lineno, cells in rows:
        if len(cells) < width:
            raise MappingExtractionError(
                "malformed_mapping",
                f"row at line {lineno} has too few columns",
            )
        case_id = _unwrap(cells[indexes["case id"]])
        symbol_key = "test function" if "test function" in indexes else "task method"
        symbol = _unwrap(cells[indexes[symbol_key]])
        target_file = _unwrap(cells[indexes["target file"]])
        if not case_id or not symbol or not target_file:
            raise MappingExtractionError(
                "malformed_mapping",
                f"empty mapping cell at line {lineno}",
            )
        if case_id in seen_case:
            raise MappingExtractionError(
                "duplicated_mapping",
                f"duplicate case id {case_id!r}",
            )
        key = (target_file, symbol)
        if key in seen_file_symbol:
            raise MappingExtractionError(
                "duplicated_mapping",
                f"duplicate symbol/path mapping {symbol!r} -> {target_file!r}",
            )
        if "\\" in target_file or target_file.startswith("/") or ".." in target_file.split("/"):
            raise MappingExtractionError(
                "malformed_mapping",
                f"unsafe target file at line {lineno}: {target_file}",
            )
        if layer == "api" and not target_file.startswith("tests/api/"):
            raise MappingExtractionError(
                "wrong_layer_mapping",
                f"API mapping target outside tests/api: {target_file}",
            )
        if layer == "e2e" and not target_file.startswith("tests/e2e/"):
            raise MappingExtractionError(
                "wrong_layer_mapping",
                f"E2E mapping target outside tests/e2e: {target_file}",
            )
        if layer == "fuzz" and not target_file.startswith("tests/fuzz/"):
            raise MappingExtractionError(
                "wrong_layer_mapping",
                f"Fuzz mapping target outside tests/fuzz: {target_file}",
            )
        if layer == "performance" and not target_file.startswith("tests/perf/"):
            raise MappingExtractionError(
                "wrong_layer_mapping",
                f"Performance mapping target outside tests/perf: {target_file}",
            )
        seen_case.add(case_id)
        seen_file_symbol.add(key)
        entries.append(MappedTestEntry(case_id=case_id, symbol=symbol, target_file=target_file))
    if not entries:
        raise MappingExtractionError("missing_mapping", f"{section} table has no data rows")
    return tuple(entries)


def _extract_schema_acquisition_case_ids(plan_text: str) -> tuple[str, ...]:
    section_text = _extract_section(plan_text, "Schema Acquisition")
    if section_text is None:
        return _extract_inline_schema_acquisition_case_ids(plan_text)
    header: list[str] | None = None
    case_ids: list[str] = []
    seen: set[str] = set()
    for lineno, cells in table_rows(section_text):
        lowered = [cell.lower() for cell in cells]
        if header is None:
            if "case id" in lowered:
                header = lowered
            continue
        if "case id" in lowered:
            raise MappingExtractionError(
                "interrupted_mapping",
                f"Schema Acquisition interrupted at line {lineno}",
            )
        idx = header.index("case id")
        if len(cells) <= idx:
            raise MappingExtractionError(
                "malformed_mapping",
                f"Schema Acquisition row too short at line {lineno}",
            )
        case_id = _unwrap(cells[idx])
        if not case_id:
            raise MappingExtractionError(
                "malformed_mapping",
                f"empty Schema Acquisition case id at line {lineno}",
            )
        if case_id in seen:
            raise MappingExtractionError(
                "duplicated_mapping",
                f"duplicate Schema Acquisition case id {case_id!r}",
            )
        seen.add(case_id)
        case_ids.append(case_id)
    if header is None:
        return _extract_inline_schema_acquisition_case_ids(plan_text)
    if not case_ids:
        raise MappingExtractionError(
            "missing_schema_acquisition",
            "Schema Acquisition has no Case ID rows",
        )
    return tuple(sorted(case_ids))


def _extract_inline_schema_acquisition_case_ids(plan_text: str) -> tuple[str, ...]:
    section_text = _extract_section(plan_text, "Test Function Mapping")
    if section_text is None:
        raise MappingExtractionError(
            "missing_schema_acquisition",
            "missing section: Schema Acquisition",
        )
    header: list[str] | None = None
    case_ids: list[str] = []
    seen: set[str] = set()
    for lineno, cells in table_rows(section_text):
        lowered = [cell.lower() for cell in cells]
        if header is None:
            if "case id" in lowered and "schema acquisition" in lowered:
                header = lowered
            continue
        case_idx = header.index("case id")
        acquisition_idx = header.index("schema acquisition")
        if len(cells) <= max(case_idx, acquisition_idx):
            raise MappingExtractionError(
                "malformed_mapping",
                f"inline Schema Acquisition row too short at line {lineno}",
            )
        case_id = _unwrap(cells[case_idx])
        acquisition = _unwrap(cells[acquisition_idx])
        if not case_id or not acquisition:
            raise MappingExtractionError(
                "malformed_mapping",
                f"empty inline Schema Acquisition cell at line {lineno}",
            )
        if case_id in seen:
            raise MappingExtractionError(
                "duplicated_mapping",
                f"duplicate inline Schema Acquisition case id {case_id!r}",
            )
        seen.add(case_id)
        case_ids.append(case_id)
    if header is None or not case_ids:
        raise MappingExtractionError(
            "missing_schema_acquisition",
            "Schema Acquisition has no Case ID rows or inline mapping column",
        )
    return tuple(sorted(case_ids))


def _extract_section(text: str, heading: str) -> str | None:
    lines = text.splitlines()
    heading_lower = heading.strip().lower()
    accepted_headings = {heading_lower}
    if heading_lower == "schema acquisition":
        accepted_headings.add("schema acquisition procedure")
    start: int | None = None
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line.strip())
        if not match:
            continue
        candidate = _TRAILING_HEADING_QUALIFIER_RE.sub("", match.group(1)).strip().lower()
        if candidate in accepted_headings:
            start = index + 1
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start, len(lines)):
        if _HEADING_RE.match(lines[index].strip()):
            end = index
            break
    return "\n".join(lines[start:end])


def _unwrap(value: str) -> str:
    stripped = value.strip()
    match = _BACKTICK_RE.match(stripped)
    return match.group(1) if match else stripped


__all__ = [
    "GeneratedEntryDecision",
    "LayerBehavioralPolicy",
    "LayerMappingRelation",
    "MappedTestEntry",
    "MappingExtractionError",
    "behavioral_policy_for_layer",
    "classify_api_entry",
    "classify_e2e_entry",
    "classify_fuzz_entry",
    "classify_generated_entry",
    "classify_performance_entry",
    "extract_layer_mapping",
    "mapped_case_ids_for_path",
    "selected_private_root_targets",
]
