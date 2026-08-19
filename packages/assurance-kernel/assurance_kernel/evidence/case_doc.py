"""Evidence-side read-only case DTO (D1'), replacing the earlier plan for a
global ``CaseYaml`` extension.

Captures only the fields the trace fold needs from a raw ``case.yaml``
document, without touching ``artifacts/models/cases.py`` or the registry —
``CaseYaml`` keeps transcribing the skill-authored schema 1:1. On-disk
documents carry far more fields than this DTO exposes (``extra="ignore"``);
a field this DTO *does* consume that has the wrong type is not silently
coerced — it turns the whole file's read into a typed ``case_unreadable``
gap instead of raising or falling back to a truthy/falsy guess.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

from assurance_kernel.artifacts.models.trace import TraceCaseType, TraceGap


class EvidenceCaseEntry(BaseModel):
    """evidence 侧只读 case DTO：从 raw YAML 捕获所需字段，不触碰 registry 的 CaseYaml。

    extra="ignore"（盘上字段远比此处多）；所需字段类型错误即 case_unreadable gap。
    """

    model_config = ConfigDict(extra="ignore")

    case_id: str
    module: str
    type: TraceCaseType
    assertions: tuple[str, ...] = ()
    automation_required: StrictBool = False
    perf_capability: str | None = None


def _load_yaml_mapping(path: Path) -> dict[str, object]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} is not a YAML mapping")
    return raw


def _extract_perf_capability(automation: dict[str, object]) -> str | None:
    """Flatten ``automation.performance.scenario.capability``.

    A missing key at any level defaults to ``None`` (nothing to flatten); a
    key that is *present* with the wrong type is a malformed document and
    raises, so the caller can turn it into a ``case_unreadable`` gap instead
    of silently discarding a value that was actually there.
    """
    if "performance" not in automation:
        return None
    performance = automation["performance"]
    if not isinstance(performance, dict):
        raise ValueError("automation.performance is not a mapping")

    if "scenario" not in performance:
        return None
    scenario = performance["scenario"]
    if not isinstance(scenario, dict):
        raise ValueError("automation.performance.scenario is not a mapping")

    if "capability" not in scenario:
        return None
    capability = scenario["capability"]
    if not isinstance(capability, str):
        raise ValueError("automation.performance.scenario.capability is not a string")
    return capability


def _build_entry(raw_case: dict[str, object]) -> EvidenceCaseEntry:
    automation = raw_case.get("automation")
    if automation is None:
        automation = {}
    elif not isinstance(automation, dict):
        raise ValueError("automation is not a mapping")

    assertions = raw_case.get("assertions", [])
    if not isinstance(assertions, list):
        raise ValueError("assertions is not a list")

    return EvidenceCaseEntry.model_validate(
        {
            "case_id": raw_case.get("case_id"),
            "module": raw_case.get("module"),
            "type": raw_case.get("type"),
            "assertions": assertions,
            "automation_required": automation.get("required", False),
            "perf_capability": _extract_perf_capability(automation),
        }
    )


def load_case_entries(change_dir: Path) -> tuple[list[EvidenceCaseEntry], list[TraceGap]]:
    """读取 cases/**/case.yaml 的 added+modified；removed 不产出。

    Unreadable files and per-case type errors both surface as typed
    ``case_unreadable`` gaps rather than exceptions.
    """
    entries: list[EvidenceCaseEntry] = []
    gaps: list[TraceGap] = []

    for path in sorted(change_dir.glob("cases/**/case.yaml")):
        rel = path.relative_to(change_dir).as_posix()
        try:
            document = _load_yaml_mapping(path)
        except (OSError, yaml.YAMLError, ValueError) as exc:
            gaps.append(TraceGap(code="case_unreadable", source=rel, detail=str(exc)))
            continue

        for bucket in ("added", "modified"):
            # Default only when the key is absent; a *present* falsy or
            # otherwise malformed value (``""``, ``0``, ``null``, ...) must
            # still surface as a gap rather than being silently treated as
            # an empty bucket.
            raw_cases = document[bucket] if bucket in document else []
            if not isinstance(raw_cases, list):
                gaps.append(TraceGap(code="case_unreadable", source=rel, detail=f"{bucket} is not a list"))
                continue
            for raw_case in raw_cases:
                if not isinstance(raw_case, dict):
                    gaps.append(
                        TraceGap(
                            code="case_unreadable", source=rel, detail=f"{bucket} entry is not a mapping"
                        )
                    )
                    continue
                try:
                    entries.append(_build_entry(raw_case))
                except (ValueError, ValidationError) as exc:
                    case_id = raw_case.get("case_id")
                    gaps.append(
                        TraceGap(
                            code="case_unreadable",
                            source=rel,
                            detail=f"{case_id}: {exc}" if case_id else str(exc),
                        )
                    )

    return entries, gaps


__all__ = ["EvidenceCaseEntry", "load_case_entries"]
