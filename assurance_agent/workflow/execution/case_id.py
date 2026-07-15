"""Canonical case-id extraction from test names (aligned with TS CASE_ID_RE).

Canonical form is TC_<MODULE>[_<LAYER>]_<NNN> (underscore, upper). Matching is
case-insensitive and accepts the legacy hyphen form; the extracted id is
canonicalized so it matches case_id values in case.yaml.
"""
import re

# Lookbehind (not \b) because `_` is a word char, so \b would not fire between
# `test_` and `tc_...`. The id terminates at its 3-digit numeric suffix.
_CASE_ID_RE = re.compile(
    r"(?<![A-Z0-9])(TC[-_][A-Z0-9]+(?:[-_][A-Z0-9]+)*[-_][0-9]{3})(?=$|[^A-Z0-9])",
    re.IGNORECASE,
)


def canonicalize_case_id(raw: str) -> str:
    return raw.upper().replace("-", "_")


def extract_case_id(text: str) -> str:
    match = _CASE_ID_RE.search(text)
    return canonicalize_case_id(match.group(1)) if match else ""
