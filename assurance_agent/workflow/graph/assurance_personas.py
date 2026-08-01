"""Exact assurance skill → OpenCode persona registry (D11).

Task 15 activates this closed sixteen-target table in ``AgentHandler``. Unknown
and non-assurance targets keep the keyword fallback in ``agent_for_skill``.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Mapping

AssurancePersona = str

# Four planners + API/E2E plan fixers → aa-doc-author
# Four reviewers → aa-reviewer
# Four codegen + API/E2E codegen fixers → aa-test-author
ASSURANCE_PERSONA_BY_TARGET: Final[Mapping[str, AssurancePersona]] = MappingProxyType(
    {
        "aa-api-plan": "aa-doc-author",
        "aa-e2e-plan": "aa-doc-author",
        "aa-fuzz-plan": "aa-doc-author",
        "aa-performance-plan": "aa-doc-author",
        "aa-api-plan-fixer": "aa-doc-author",
        "aa-e2e-plan-fixer": "aa-doc-author",
        "aa-api-plan-reviewer": "aa-reviewer",
        "aa-e2e-plan-reviewer": "aa-reviewer",
        "aa-fuzz-plan-reviewer": "aa-reviewer",
        "aa-performance-plan-reviewer": "aa-reviewer",
        "aa-api-codegen": "aa-test-author",
        "aa-e2e-codegen": "aa-test-author",
        "aa-fuzz-codegen": "aa-test-author",
        "aa-performance-codegen": "aa-test-author",
        "aa-api-codegen-fixer": "aa-test-author",
        "aa-e2e-codegen-fixer": "aa-test-author",
    }
)


def expected_assurance_persona(target: str) -> AssurancePersona:
    """Return the exact persona for a closed assurance skill target.

    Accepts bare skill ids (``aa-api-plan``) or contract targets
    (``skill:aa-api-plan``). Unknown and non-assurance targets raise ``KeyError``.
    """
    key = target.removeprefix("skill:")
    try:
        return ASSURANCE_PERSONA_BY_TARGET[key]
    except KeyError as exc:
        raise KeyError(f"unknown assurance persona target: {target!r}") from exc
