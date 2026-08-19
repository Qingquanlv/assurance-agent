"""L1 accepts only `.aa/data-knowledge.yaml`; another directory is pseudo-L1."""

from __future__ import annotations

import re

from assurance_kernel.artifacts.models.plan_checks import CheckEvidence, Finding
from assurance_kernel.verification.checks.base import CheckContext, evidence

CHECK_ID = "l1_path"
_CANONICAL = ".aa/data-knowledge.yaml"
_REFERENCE = re.compile(r"(?P<prefix>[\w./-]+/)data-knowledge\.yaml")


def check_l1_path(ctx: CheckContext) -> CheckEvidence:
    findings: list[Finding] = []
    for rel in sorted(ctx.plan_texts):
        for lineno, line in enumerate(ctx.plan_texts[rel].splitlines(), start=1):
            for match in _REFERENCE.finditer(line):
                if match.group("prefix") == ".aa/":
                    continue
                findings.append(
                    Finding(locator=f"{rel}:{lineno}", actual=match.group(0), expected=_CANONICAL)
                )
    return evidence(CHECK_ID, findings, tuple(ctx.plan_texts))
