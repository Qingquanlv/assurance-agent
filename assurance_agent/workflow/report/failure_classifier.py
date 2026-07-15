"""Data-driven failure classifier (rules in _resources/rules/failure-classification.yaml).

Pipeline order and regexes are transcribed from TS failure_classifier.ts; the
rule table lives in packaged YAML so classifications can evolve without code
changes. The matcher is deterministic: first matching rule wins.
"""
import re
from functools import lru_cache
from typing import Any, Literal

import yaml
from pydantic import BaseModel

from assurance_agent import resources
from assurance_agent.artifacts.models import FailureCategory, FailureSeverity

Target = Literal["api", "e2e", "fuzz"]


class Classification(BaseModel):
    category: FailureCategory
    fix_proposal_eligible: bool
    severity: FailureSeverity
    needs_review: bool


class _Rules:
    def __init__(self, doc: dict[str, Any]) -> None:
        self.patterns: dict[str, re.Pattern[str]] = {
            name: re.compile(expr, re.IGNORECASE) for name, expr in doc["patterns"].items()
        }
        self.pipelines: dict[str, dict[str, Any]] = doc["pipelines"]
        self.fix_proposal: dict[str, Any] = doc["fix_proposal"]
        self.severity: dict[str, str] = doc["severity"]


@lru_cache(maxsize=1)
def _rules() -> _Rules:
    doc = yaml.safe_load(resources.read_text("rules", "failure-classification.yaml"))
    return _Rules(doc)


def _matches(match: Any, text: str, rules: _Rules) -> bool:
    if isinstance(match, str):
        return rules.patterns[match].search(text) is not None
    if isinstance(match, dict):
        if "any_of" in match:
            return any(_matches(item, text, rules) for item in match["any_of"])
        if "all_of" in match:
            return all(_matches(item, text, rules) for item in match["all_of"])
    return False


def classify_failure(
    *,
    message: str,
    log_excerpt: str,
    target: Target,
    has_trace: bool = False,
    has_screenshot: bool = False,
) -> Classification:
    del has_trace, has_screenshot  # reserved for future rule extensions
    rules = _rules()
    text = f"{message} {log_excerpt}".lower()
    pipeline = rules.pipelines["fuzz" if target == "fuzz" else "default"]

    category: str = pipeline["fallback"]
    for rule in pipeline["rules"]:
        allowed_targets = rule.get("target")
        if allowed_targets and target not in allowed_targets:
            continue
        if _matches(rule["match"], text, rules):
            category = rule["category"]
            break

    allowed = rules.fix_proposal.get(category, False)
    return Classification(
        category=category,  # type: ignore[arg-type]
        fix_proposal_eligible=allowed is True,
        severity=rules.severity.get(category, "low"),  # type: ignore[arg-type]
        needs_review=category == "unknown" or allowed == "review",
    )
