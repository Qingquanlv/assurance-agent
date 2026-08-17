"""workflow-state.yaml 原子读写 + 完整性哈希（防篡改）。

read_state/write_state 以 M2 canonical `WorkflowState`（extra='allow'）为交换类型，
不返回裸 dict——这样 M4/M6 的属性读写与引擎的 model_dump() 作用域构造完全一致。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from assurance_agent.artifacts.models import RunContext, WorkflowState
from assurance_agent.artifacts.paths import WORKFLOW_STATE_REL, existing_with_alias
from assurance_agent.exceptions import AaError

WORKFLOW_STATE_RELPATH = WORKFLOW_STATE_REL
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
    file = existing_with_alias(state_file(change_dir))
    if file is None:
        return None
    text = file.read_text(encoding="utf-8")
    doc = json.loads(text) if file.suffix == ".json" else yaml.safe_load(text)
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
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, file)  # 原子替换：磁盘 state 永不半写


def verify_state_integrity(change_dir: Path) -> str | None:
    """Return an error message when ``_integrity.state_sha256`` mismatches, else None."""
    try:
        doc = _load_doc(change_dir)
    except StateIntegrityError as err:
        return str(err)
    if doc is None:
        return None
    integrity = doc.get(_INTEGRITY_KEY)
    if not isinstance(integrity, dict) or not isinstance(integrity.get("state_sha256"), str):
        return None
    if _canonical_hash(doc) != integrity["state_sha256"]:
        return f"workflow-state integrity check failed: {state_file(change_dir)}"
    return None


def read_state_lenient(change_dir: Path) -> WorkflowState:
    """Load workflow-state without raising on integrity mismatch (for status audits)."""
    try:
        doc = _load_doc(change_dir)
    except StateIntegrityError:
        return WorkflowState()
    if doc is None:
        return WorkflowState()
    fields = {k: v for k, v in doc.items() if k != _INTEGRITY_KEY}
    return WorkflowState.model_validate(fields)


def state_guard(change_dir: Path) -> str:
    """磁盘 state 文件 sha256（对齐源版 computeStateGuard）；文件缺失返回空串哨兵。"""
    file = state_file(change_dir)
    if not file.exists():
        return ""
    return hashlib.sha256(file.read_bytes()).hexdigest()


# ---- state configure（TS workflow_state.ts configureWorkflowParams/stampRunContext 移植）----

# Params keys the configure command may write (schema-aligned allowlist).
CONFIGURE_PARAM_KEYS = frozenset(
    {
        "run_mode",
        "test_types",
        "run_tests",
        "max_case_fix_attempts",
        "max_plan_fix_attempts",
        "max_healing_attempts",
        "auto_archive",
        "force_continue",
        "e2e_framework",
    }
)

CONFIGURE_ORCHESTRATORS = frozenset({"aa-workflow", "aa-intake", "aa-execute"})

# orchestrator -> (interaction_mode, active_scope)
_RUN_CONTEXT_BY_ORCHESTRATOR = {
    "aa-intake": ("interactive", "intake"),
    "aa-execute": ("autonomous", "execute"),
    "aa-workflow": ("autonomous", "full"),
}

# run_mode x orchestrator allowlist (TS assertRunModeAllowed 矩阵).
_RUN_MODE_ALLOWLIST = {
    "aa-workflow": frozenset(
        {
            "full",
            "case-only",
            "api-only",
            "e2e-only",
            "plan-only",
            "codegen-only",
            "review-case",
            "review-plan",
        }
    ),
    "aa-intake": frozenset({"full", "case-only", "review-case"}),
    "aa-execute": frozenset({"full", "api-only", "e2e-only", "plan-only", "codegen-only", "review-plan"}),
}


def configure_workflow_params(
    change_dir: Path,
    params: dict[str, Any],
    orchestrator_skill: str,
    *,
    stamped_at: str | None = None,
) -> WorkflowState:
    """Merge allowlisted runtime params into workflow-state.yaml, then stamp run_context.

    语义对齐 TS ``configureWorkflowParams`` + ``stampRunContext``：phases 等其它
    顶层键原样保留；run_mode 校验读取合并后的 params（既存 run_mode 同样受限）。
    与 TS 的差异：校验全部通过后才单次落盘（TS 先写 params 再盖章，run_mode 非法时
    params 已残留），此处保持 all-or-nothing。
    """
    if orchestrator_skill not in CONFIGURE_ORCHESTRATORS:
        expected = ", ".join(sorted(CONFIGURE_ORCHESTRATORS))
        raise AaError(f'unsupported orchestrator "{orchestrator_skill}". Expected {expected}')

    state = read_state(change_dir)
    merged = dict(state.params)
    for key, value in params.items():
        if key not in CONFIGURE_PARAM_KEYS:
            raise AaError(f'configure: unknown param "{key}" (not in allowlist)')
        merged[key] = value

    run_mode = merged.get("run_mode")
    if isinstance(run_mode, str) and run_mode and run_mode not in _RUN_MODE_ALLOWLIST[orchestrator_skill]:
        raise AaError(f"{orchestrator_skill} cannot run with run_mode {run_mode}")

    interaction_mode, active_scope = _RUN_CONTEXT_BY_ORCHESTRATOR[orchestrator_skill]
    state.params = merged
    state.run_context = RunContext(
        orchestrator_skill=orchestrator_skill,
        interaction_mode=interaction_mode,
        active_scope=active_scope,
        stamped_at=stamped_at or datetime.now(timezone.utc).isoformat(),
    )
    write_state(change_dir, state)
    return state
