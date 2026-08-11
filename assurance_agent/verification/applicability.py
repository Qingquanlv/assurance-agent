"""Derive runtime layer applicability from validated case mappings."""

from collections.abc import Mapping, Sequence

from assurance_agent.artifacts.models.assurance import CASE_TYPES
from assurance_agent.artifacts.models.plan_checks import LayerApplicability
from assurance_agent.verification.profiles import LayerAssuranceProfile

_CASE_TYPES = frozenset(CASE_TYPES)


def derive_layer_applicability(
    cases: Sequence[Mapping[str, object]],
    profile: LayerAssuranceProfile,
) -> LayerApplicability:
    case_ids: set[str] = set()
    for document_index, document in enumerate(cases):
        if not isinstance(document, Mapping):
            raise ValueError(f"cases[{document_index}] must be a mapping")
        for bucket in ("added", "modified"):
            if bucket not in document:
                raise ValueError(f"cases[{document_index}] missing required key '{bucket}'")
            entries = document[bucket]
            if not isinstance(entries, list):
                raise ValueError(f"cases[{document_index}].{bucket} must be a list")
            for entry_index, entry in enumerate(entries):
                locator = f"cases[{document_index}].{bucket}[{entry_index}]"
                if not isinstance(entry, Mapping):
                    raise ValueError(f"{locator} must be a mapping")
                case_type = entry.get("type")
                if case_type not in _CASE_TYPES:
                    raise ValueError(f"{locator}.type is invalid")
                if "automation" not in entry:
                    required = False
                else:
                    automation = entry["automation"]
                    if not isinstance(automation, Mapping):
                        raise ValueError(f"{locator}.automation must be a mapping")
                    required = automation.get("required", False)
                    if not isinstance(required, bool):
                        raise ValueError(f"{locator}.automation.required must be a boolean")
                if required:
                    case_id = entry.get("case_id")
                    if not isinstance(case_id, str) or not case_id.strip():
                        raise ValueError(f"{locator}.case_id must be a non-empty string")
                    if case_type == profile.case_type:
                        case_ids.add(case_id)
    ordered = tuple(sorted(case_ids))
    return LayerApplicability(
        layer=profile.layer,
        applicable=bool(ordered),
        reason_code="automated_cases_present" if ordered else "no_automated_cases",
        case_ids=ordered,
    )
