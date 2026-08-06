"""L1 accepts only `.aa/data-knowledge.yaml`; another directory is pseudo-L1."""

from __future__ import annotations

import re

from assurance_agent.artifacts.models.plan_checks import CheckEvidence, Finding
from assurance_agent.verification.checks.base import CheckContext, evidence

CHECK_ID = "l1_path"
_CANONICAL = ".aa/data-knowledge.yaml"
_CANONICAL_MARKER = re.compile(r"(?<![\w.-])\.aa(?:/data-knowledge\.yaml)?(?=$|[^\w-])")
_REFERENCE = re.compile(r"(?P<prefix>[\w./-]+/)data-knowledge\.yaml")
_ABSENCE_CUE = re.compile(
    r"(?i)(?:\bis\s+)?(?:absent|missing|unavailable|not\s+present|does\s+not\s+exist)"
    r"|不存在|缺失|未(?:配置|找到|提供)|没有"
)
_ABSENCE_PREFIX_CUE = re.compile(r"(?i)(?:\bno\b|without|不存在|缺失|没有)[^.;|]{0,48}$")
_NEGATED_USE_PREFIX_CUE = re.compile(
    r"(?i)(?:do\s+not|don't|must\s+not|should\s+not|need\s+not)\s+"
    r"(?:require|use|read|load|materializ\w*|create|write|block|treat)\b[^.;|]{0,64}$"
)
_NON_REQUIREMENT_SUFFIX_CUE = re.compile(
    r"(?i)^[\s`*_]*(?:is\s+not\s+(?:a\s+)?"
    r"(?:blocker|requirement|hard[- ]gate|canonical|authoritative|required)"
    r"|(?:does|should|must)\s+not\s+(?:block|gate|prevent))\b"
)


def _is_noncanonical_disclaimer(line: str, match: re.Match[str]) -> bool:
    if _CANONICAL_MARKER.search(line) is None:
        return False
    tail = line[match.end() :]
    next_reference = _REFERENCE.search(tail)
    scope = tail[: next_reference.start()] if next_reference is not None else tail[:96]
    prefix_scope = line[max(0, match.start() - 96) : match.start()]
    return bool(
        _ABSENCE_CUE.search(scope)
        or _ABSENCE_PREFIX_CUE.search(prefix_scope)
        or _NEGATED_USE_PREFIX_CUE.search(prefix_scope)
        or _NON_REQUIREMENT_SUFFIX_CUE.search(scope)
    )


def check_l1_path(ctx: CheckContext) -> CheckEvidence:
    findings: list[Finding] = []
    for rel in sorted(ctx.plan_texts):
        for lineno, line in enumerate(ctx.plan_texts[rel].splitlines(), start=1):
            for match in _REFERENCE.finditer(line):
                if match.group("prefix") == ".aa/":
                    continue
                if _is_noncanonical_disclaimer(line, match):
                    continue
                findings.append(
                    Finding(locator=f"{rel}:{lineno}", actual=match.group(0), expected=_CANONICAL)
                )
    return evidence(CHECK_ID, findings, tuple(ctx.plan_texts))
