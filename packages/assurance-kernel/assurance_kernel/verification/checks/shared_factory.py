"""L1-declared shared factories may only be reused by code-generation plans."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping

from assurance_kernel.artifacts.models.plan_checks import CheckEvidence, Finding
from assurance_kernel.verification.checks.base import CheckContext, evidence, table_rows

CHECK_ID = "shared_factory"
_MODULE_COLUMN = "shared module"
_FUNCTION_COLUMN = "function"
_OWNERSHIP_COLUMN = "ownership"
_HEADING = re.compile(r"^\s*#{1,6}\s+(?P<title>.+?)\s*#*\s*$")


def _is_factory_mapping_heading(title: str) -> bool:
    words = re.findall(r"[a-z]+", title.casefold())
    return any(word in {"factory", "factories"} for word in words) and any(
        word in {"mapping", "mappings"} for word in words
    )


def _declared_symbols(dk: Mapping[str, object]) -> dict[tuple[str, str], str]:
    capabilities = dk.get("capabilities")
    factories = capabilities.get("domain_factories") if isinstance(capabilities, Mapping) else None
    declared: dict[tuple[str, str], str] = {}
    if not isinstance(factories, Mapping):
        return declared
    for group in factories.values():
        if not isinstance(group, Mapping):
            continue
        for leaf in group.values():
            symbol = leaf.get("symbol") if isinstance(leaf, Mapping) else None
            if not isinstance(symbol, str) or "." not in symbol:
                continue
            module_dotted, _, function = symbol.rpartition(".")
            declared[(module_dotted.replace(".", "/") + ".py", function)] = symbol
    return declared


def _factory_mapping_rows(text: str) -> Iterator[tuple[int, list[str]]]:
    in_factory_mapping = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        heading = _HEADING.match(line)
        if heading is not None:
            in_factory_mapping = _is_factory_mapping_heading(heading.group("title"))
            continue
        if not in_factory_mapping:
            continue
        for _, cells in table_rows(line):
            yield lineno, cells


def check_shared_factory(ctx: CheckContext) -> CheckEvidence:
    declared = _declared_symbols(ctx.data_knowledge)
    findings: list[Finding] = []
    for rel in sorted(ctx.plan_texts):
        columns: dict[str, int] | None = None
        parsed_table = False
        for lineno, cells in _factory_mapping_rows(ctx.plan_texts[rel]):
            lowered = [cell.lower() for cell in cells]
            if _MODULE_COLUMN in lowered and _OWNERSHIP_COLUMN in lowered and _FUNCTION_COLUMN in lowered:
                columns = {
                    name: lowered.index(name)
                    for name in (_MODULE_COLUMN, _FUNCTION_COLUMN, _OWNERSHIP_COLUMN)
                }
                parsed_table = True
                continue
            if columns is None or len(cells) <= max(columns.values()):
                continue
            module = cells[columns[_MODULE_COLUMN]]
            function = cells[columns[_FUNCTION_COLUMN]]
            ownership = cells[columns[_OWNERSHIP_COLUMN]]
            symbol = declared.get((module, function))
            if symbol is None or "reuse" in ownership.lower():
                continue
            findings.append(
                Finding(
                    locator=f"{rel}:{lineno}",
                    actual=ownership,
                    expected=f"reuse the L1-declared factory {symbol}",
                )
            )
        if rel.endswith("-codegen-plan.md") and declared and not parsed_table:
            findings.append(
                Finding(
                    locator=rel,
                    actual="Factory Mapping table not parsed",
                    expected="a Factory Mapping table with Shared Module, Function, and Ownership columns",
                )
            )
    return evidence(CHECK_ID, findings, tuple(ctx.plan_texts))
