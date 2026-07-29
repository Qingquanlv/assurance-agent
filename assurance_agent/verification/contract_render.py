"""把 node 声明 outputs 绑定的 pydantic 模型渲染为 prompt 的 OUTPUT CONTRACT 子句。

契约的唯一事实源是 artifact registry 里的模型：机器可推导部分读 ``model_fields``
（必填字段、Literal 枚举），人话规则读 ``model_config['json_schema_extra']['prompt_notes']``。
未注册的 output 不产生子句——渲染器绝不猜测契约。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from assurance_agent.artifacts.registry import match_artifact

_ROOT_PREFIXES = ("change:", "project:", "repo:")


def _strip_root(output: str) -> str | None:
    rel = output.strip()
    for prefix in _ROOT_PREFIXES:
        if rel.startswith(prefix):
            rel = rel[len(prefix) :]
            break
    if not rel or rel.endswith("/"):
        return None
    return rel


def _literal_values(field: FieldInfo) -> tuple[str, ...]:
    if get_origin(field.annotation) is not Literal:
        return ()
    return tuple(str(value) for value in get_args(field.annotation))


def _prompt_notes(model: type[BaseModel]) -> tuple[str, ...]:
    extra = model.model_config.get("json_schema_extra")
    if not isinstance(extra, dict):
        return ()
    notes = extra.get("prompt_notes")
    if not isinstance(notes, (list, tuple)):
        return ()
    return tuple(str(note) for note in notes)


def _render_model(rel: str, model: type[BaseModel]) -> str:
    parts = [f"{rel} must be a {model.__name__}"]
    required = [name for name, field in model.model_fields.items() if field.is_required()]
    if required:
        parts.append("required fields: " + ", ".join(required))
    for name, field in model.model_fields.items():
        values = _literal_values(field)
        if len(values) == 1:
            parts.append(f"{name} '{values[0]}'")
        elif len(values) > 1:
            parts.append(f"{name} one of {', '.join(values)}")
    parts.extend(_prompt_notes(model))
    return "; ".join(parts) + "."


def render_output_contract(outputs: Sequence[str]) -> str:
    rendered: list[str] = []
    seen: set[str] = set()
    for output in outputs:
        rel = _strip_root(output)
        if rel is None or rel in seen:
            continue
        spec = match_artifact(rel)
        if spec is None:
            continue
        seen.add(rel)
        rendered.append(_render_model(rel, spec.authoring_model or spec.model))
    if not rendered:
        return ""
    return " OUTPUT CONTRACT: " + " ".join(rendered)
