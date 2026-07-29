"""Read-only case document DTO for evidence fold (D1').

Does not extend registry ``CaseYaml``; captures only the fields fold needs from
raw YAML under ``cases/**/case.yaml``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

from assurance_agent.artifacts.models.trace import TraceGap

_CASE_TYPES = frozenset({"API", "E2E", "Fuzz", "Performance"})
_TYPE_ERROR = object()


class EvidenceCaseEntry(BaseModel):
    """evidence 侧只读 case DTO：从 raw YAML 捕获所需字段，不触碰 registry 的 CaseYaml。

    extra="ignore"（盘上字段远比此处多）；所需字段类型错误即 case_unreadable gap。
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    case_id: str
    module: str
    type: Literal["API", "E2E", "Fuzz", "Performance"]
    assertions: tuple[str, ...] = ()
    automation_required: StrictBool = False
    perf_capability: str | None = None


def _as_str_tuple(value: object) -> tuple[str, ...] | None:
    if value is None:
        return ()
    if not isinstance(value, list):
        return None
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        out.append(item)
    return tuple(out)


def _perf_capability(automation: dict[str, object]) -> str | None | object:
    performance = automation.get("performance")
    if performance is None:
        return None
    if not isinstance(performance, dict):
        return _TYPE_ERROR
    scenario = performance.get("scenario")
    if scenario is None:
        return None
    if not isinstance(scenario, dict):
        return _TYPE_ERROR
    capability = scenario.get("capability")
    if capability is None:
        return None
    if not isinstance(capability, str) or not capability:
        return _TYPE_ERROR
    return capability


def _entry_from_raw(raw: object) -> EvidenceCaseEntry | None:
    if not isinstance(raw, dict):
        return None
    case_id = raw.get("case_id")
    module = raw.get("module")
    case_type = raw.get("type")
    if not isinstance(case_id, str) or not case_id:
        return None
    if not isinstance(module, str) or not module:
        return None
    if case_type not in _CASE_TYPES:
        return None
    assertions = _as_str_tuple(raw.get("assertions"))
    if assertions is None:
        return None
    automation_raw = raw.get("automation", {})
    if automation_raw is None:
        automation_raw = {}
    if not isinstance(automation_raw, dict):
        return None
    if "required" in automation_raw:
        required = automation_raw["required"]
        if not isinstance(required, bool):
            return None
    else:
        required = False
    capability = _perf_capability(automation_raw)
    if capability is _TYPE_ERROR:
        return None
    try:
        return EvidenceCaseEntry(
            case_id=case_id,
            module=module,
            type=case_type,  # type: ignore[arg-type]
            assertions=assertions,
            automation_required=required,
            perf_capability=capability if isinstance(capability, str) else None,
        )
    except ValidationError:
        return None


def load_case_entries(change_dir: Path) -> tuple[list[EvidenceCaseEntry], list[TraceGap]]:
    """读取 cases/**/case.yaml 的 added+modified；removed 不产出。"""
    entries: list[EvidenceCaseEntry] = []
    gaps: list[TraceGap] = []
    cases_root = change_dir / "cases"
    if not cases_root.is_dir():
        return entries, gaps
    for path in sorted(cases_root.glob("**/case.yaml")):
        rel = path.relative_to(change_dir).as_posix()
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as err:
            gaps.append(TraceGap(code="case_unreadable", source=rel, detail=str(err)))
            continue
        if not isinstance(raw, dict):
            gaps.append(TraceGap(code="case_unreadable", source=rel, detail="not a YAML mapping"))
            continue
        file_entries: list[EvidenceCaseEntry] = []
        file_bad = False
        for bucket in ("added", "modified"):
            values = raw.get(bucket, [])
            if values is None:
                values = []
            if not isinstance(values, list):
                gaps.append(TraceGap(code="case_unreadable", source=rel, detail=f"{bucket} is not a list"))
                file_bad = True
                break
            for item in values:
                parsed = _entry_from_raw(item)
                if parsed is None:
                    gaps.append(
                        TraceGap(
                            code="case_unreadable",
                            source=rel,
                            detail=f"invalid entry in {bucket}",
                        )
                    )
                    file_bad = True
                    break
                file_entries.append(parsed)
            if file_bad:
                break
        if not file_bad:
            entries.extend(file_entries)
    return entries, gaps
