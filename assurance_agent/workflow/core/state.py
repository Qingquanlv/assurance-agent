"""workflow-state.yaml 原子读写 + 完整性哈希（防篡改）。

read_state/write_state 以 M2 canonical `WorkflowState`（extra='allow'）为交换类型，
不返回裸 dict——这样 M4/M6 的属性读写与引擎的 model_dump() 作用域构造完全一致。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import yaml

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.exceptions import AaError

WORKFLOW_STATE_RELPATH = "workflow-state.yaml"
_INTEGRITY_KEY = "_integrity"


class StateIntegrityError(AaError):
    """workflow-state.yaml 完整性哈希不匹配（疑似被篡改）。"""


def state_file(change_dir: Path) -> Path:
    return change_dir / WORKFLOW_STATE_RELPATH


def _canonical_hash(doc: dict[str, object]) -> str:
    payload = {k: v for k, v in doc.items() if k != _INTEGRITY_KEY}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _load_doc(change_dir: Path) -> dict[str, object] | None:
    file = state_file(change_dir)
    if not file.exists():
        return None
    doc = yaml.safe_load(file.read_text(encoding="utf-8"))
    if doc is None:
        return None
    if not isinstance(doc, dict):
        raise StateIntegrityError(f"workflow-state is not a mapping: {file}")
    return doc


def read_state(change_dir: Path) -> WorkflowState:
    doc = _load_doc(change_dir)
    if doc is None:
        return WorkflowState()
    integrity = doc.get(_INTEGRITY_KEY)
    if isinstance(integrity, dict) and isinstance(integrity.get("state_sha256"), str):
        if _canonical_hash(doc) != integrity["state_sha256"]:
            raise StateIntegrityError(f"workflow-state integrity check failed: {state_file(change_dir)}")
    # 剥离文件级元数据（pydantic v2 不接受下划线前缀键作 field/extra）。
    fields = {k: v for k, v in doc.items() if k != _INTEGRITY_KEY}
    return WorkflowState.model_validate(fields)


def write_state(change_dir: Path, state: WorkflowState) -> None:
    change_dir.mkdir(parents=True, exist_ok=True)
    doc: dict[str, object] = state.model_dump(mode="json")
    doc[_INTEGRITY_KEY] = {"state_sha256": _canonical_hash(doc)}
    file = state_file(change_dir)
    tmp = file.with_suffix(file.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.replace(tmp, file)  # 原子替换：磁盘 state 永不半写


def state_guard(change_dir: Path) -> str:
    """磁盘 state 文件 sha256（对齐源版 computeStateGuard）；文件缺失返回空串哨兵。"""
    file = state_file(change_dir)
    if not file.exists():
        return ""
    return hashlib.sha256(file.read_bytes()).hexdigest()
