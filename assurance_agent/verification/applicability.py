"""Derive runtime layer applicability from validated case mappings."""

from collections.abc import Mapping, Sequence

from assurance_agent.artifacts.models.plan_checks import LayerApplicability
from assurance_agent.verification.profiles import LayerAssuranceProfile


def derive_layer_applicability(
    cases: Sequence[Mapping[str, object]],
    profile: LayerAssuranceProfile,
) -> LayerApplicability:
    case_ids: set[str] = set()
    for document_index, document in enumerate(cases):
        for bucket in ("added", "modified"):
            entries = document.get(bucket)
            if not isinstance(entries, list):
                raise ValueError(f"cases[{document_index}].{bucket} must be a list")
            for entry_index, entry in enumerate(entries):
                locator = f"cases[{document_index}].{bucket}[{entry_index}]"
                if not isinstance(entry, Mapping):
                    raise ValueError(f"{locator} must be a mapping")
                case_type = entry.get("type")
                if case_type not in {"API", "E2E", "Fuzz", "Performance"}:
                    raise ValueError(f"{locator}.type is invalid")
                automation = entry.get("automation")
                if automation is None:
                    required = False
                elif not isinstance(automation, Mapping):
                    raise ValueError(f"{locator}.automation must be a mapping")
                else:
                    required = automation.get("required", False)
                    if not isinstance(required, bool):
                        raise ValueError(f"{locator}.automation.required must be a boolean")
                if case_type != profile.case_type or not required:
                    continue
                case_id = entry.get("case_id")
                if not isinstance(case_id, str) or not case_id.strip():
                    raise ValueError(f"{locator}.case_id must be a non-empty string")
                case_ids.add(case_id)
    ordered = tuple(sorted(case_ids))
    return LayerApplicability(
        layer=profile.layer,
        applicable=bool(ordered),
        reason_code="automated_cases_present" if ordered else "no_automated_cases",
        case_ids=ordered,
    )
