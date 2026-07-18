"""Scheme E phase prompt contract — driver-owned constant.

Clean-room port of the TS buildPhasePrompt (src/workflow/driver/phase_prompt.ts):
aws->aa renames, same behavioral contract. The prompt pins the change scope and
forbids the phase agent from touching gate/status/workflow-state — those are the
CLI's exclusive responsibility.
"""


def build_phase_prompt(skill: str, phase: str, change_id: str, item: str | None = None) -> str:
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
    return (
        f"Call skill(name='{skill}'). "
        f"Operate strictly on change_id='{change_id}' — read and write only under "
        f"qa/changes/{change_id}/. Do NOT infer the change from other directories "
        f"or pick a different (e.g. more recent) change. "
        f"Produce only the outputs for phase {phase} as described in the skill."
        f"{fix_proposal_binding}"
        f"{item_binding} "
        f"Do NOT run aa gate/status. Do NOT modify workflow-state.yaml."
    )
