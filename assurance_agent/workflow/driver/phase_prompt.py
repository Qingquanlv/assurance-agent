"""Scheme E phase prompt contract — driver-owned constant.

Clean-room port of the TS buildPhasePrompt (src/workflow/driver/phase_prompt.ts)
with the same behavioral contract. The prompt pins the change scope and
forbids the phase agent from touching gate/status/workflow-state — those are the
CLI's exclusive responsibility.

v2 分支（传入 ``allowed_writes``）委托 graph 拥有的 ``build_node_prompt``：列出
contract 授权写范围，不再宣称每个 phase 只能写 change 目录。v1 分支（不传）
保留原 Scheme E 文案，``loop.py`` 与既有调用方不受影响。
"""

from collections.abc import Sequence
from pathlib import Path

from assurance_agent.artifacts.paths import WORKFLOW_STATE_REL
from assurance_agent.workflow.graph.agent_api import build_node_prompt
from assurance_agent.workflow.skill_memory import load_skill_memory


def build_phase_prompt(
    skill: str,
    phase: str,
    change_id: str,
    *,
    allowed_writes: Sequence[str] | None = None,
    item: str | None = None,
    project_root: Path | str | None = None,
) -> str:
    if allowed_writes is not None:
        memory_root = Path(project_root) if project_root is not None else None
        return build_node_prompt(
            skill,
            phase,
            change_id,
            allowed_writes=allowed_writes,
            item=item,
            memory_root=memory_root,
        )
    fix_proposal_binding = (
        " Set fix-proposal.json source_batch_id from the current execution "
        "manifest and source_analysis_sha256 to the SHA256 of the exact current "
        "inspect/failure-analysis.json."
        if phase == "fix-proposal"
        else ""
    )
    item_binding = (
        f" This is a fanned-out dispatch: operate ONLY on item '{item}' and write "
        f"only the declared outputs of phase {phase} for that item."
        if item is not None
        else ""
    )
    memory_clause = ""
    if project_root is not None:
        memory = load_skill_memory(Path(project_root), skill)
        if memory:
            memory_clause = (
                f" Active skill memory for {skill}:\n{memory}\n"
                "Apply these rules when producing declared outputs."
            )
    return (
        f"Call skill(name='{skill}'). "
        f"Operate strictly on change_id='{change_id}' — read and write only under "
        f"qa/changes/{change_id}/. Do NOT infer the change from other directories "
        f"or pick a different (e.g. more recent) change. "
        f"Produce only the outputs for phase {phase} as described in the skill."
        f"{fix_proposal_binding}"
        f"{item_binding}"
        f"{memory_clause} "
        f"Do NOT run aa gate/status. Do NOT modify {WORKFLOW_STATE_REL}."
    )
