"""Invocation-bound authentication for the pre-dispatch generation-defect route."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from assurance_execution.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_execution.contracts.authority import (
    authenticate_generation_defect_cycle,
    record_current_generation_defect,
)
from assurance_execution.contracts.workflow import (
    ExecutionAttemptBindingV1,
    VerifiedGenerationDefectCycleV1,
)
from assurance_execution.graphs.nodes import activation_execute, select_execute
from graph_engine.attempts import derive_attempt_key
from graph_engine.canonical import JSONValue, canonical_digest

_CONTRACT_ID = "assurance.execution.task.execute.v1"
_SEMANTIC_NODE_ID = "execution.execute"


@dataclass(frozen=True, slots=True)
class GenerationDefectRouteAuthenticator:
    """Authenticate a defect against the execution identity selected for this invocation."""

    project_root: Path
    invocation_id: str
    public_entrypoint: str
    graph_revision: str

    def __post_init__(self) -> None:
        project = Path(self.project_root).resolve(strict=True)
        if project != self.project_root or not project.is_dir():
            raise ValueError("generation defect authority requires a canonical project root")
        if not self.invocation_id or not self.public_entrypoint:
            raise ValueError("generation defect authority requires current invocation coordinates")
        if len(self.graph_revision) != 64 or any(
            character not in "0123456789abcdef" for character in self.graph_revision
        ):
            raise ValueError("generation defect authority requires a canonical graph revision")

    def expected_binding(self, state: Mapping[str, object]) -> ExecutionAttemptBindingV1:
        selected = select_execute(state)
        generation = selected.generation_result
        if generation is None or selected.validation_profile is None:
            raise ValueError("generation defect route requires verified generation inputs")
        activation = activation_execute(state)
        contract = TASK_ATTEMPT_CONTRACTS[_CONTRACT_ID]
        attempt_key = derive_attempt_key(
            invocation_id=self.invocation_id,
            graph_revision=self.graph_revision,
            public_entrypoint=self.public_entrypoint,
            semantic_node_id=_SEMANTIC_NODE_ID,
            business_activation=activation,
            contract_id=contract.contract_id,
            validated_input=selected,
        )
        selected_payload: JSONValue = selected.model_dump(mode="json")
        generation_payload: JSONValue = generation.model_dump(mode="json")
        return ExecutionAttemptBindingV1(
            invocation_id=self.invocation_id,
            public_entrypoint=self.public_entrypoint,
            semantic_node_id=_SEMANTIC_NODE_ID,
            attempt_key=attempt_key,
            business_activation=activation,
            graph_revision=self.graph_revision,
            contract_id=_CONTRACT_ID,
            contract_digest=canonical_digest(cast(JSONValue, contract.canonical_projection())),
            input_digest=canonical_digest(selected_payload),
            change_id=generation.change_id,
            coverage_epoch=generation.coverage_epoch,
            repair_round=selected.repair_round,
            validation_profile=selected.validation_profile,
            generation_digest=canonical_digest(generation_payload),
        )

    def __call__(
        self,
        state: Mapping[str, object],
        cycle: VerifiedGenerationDefectCycleV1,
    ) -> None:
        expected = self.expected_binding(state)
        declared = ExecutionAttemptBindingV1.model_validate(state.get("generation_defect_execution_binding"))
        if declared != expected:
            raise ValueError("generation defect execution binding is not current")
        authenticate_generation_defect_cycle(self.project_root, cycle, expected)
        record_current_generation_defect(self.project_root, cycle, expected)


__all__ = ["GenerationDefectRouteAuthenticator"]
