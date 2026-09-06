from __future__ import annotations

from assurance_execution.contracts.attempts import AGENT_JOB_CONTRACTS, attempt_contract_refs
from assurance_execution.contracts.evidence import ExecutionAgentResultV1, ExecutionEvidenceV1
from assurance_execution.contracts.execution import (
    ExecutionCommandReceiptV1,
    ExecutionManifest,
    ExecutionReceiptV1,
    RawTestResultV1,
)
from assurance_execution.contracts.selection import (
    ClosedMappingEntryV1,
    ClosedMappingV1,
    SelectedTargets,
)
from assurance_execution.contracts.workflow import (
    ExecutionCycleInputV1,
    ExecutionCycleResultV1,
    VerifiedExecutionCycleResultV1,
)
from assurance_execution.contracts.verification import (
    ExecutionDispatchResultV1,
    EvidenceCompletionV1,
    FrozenUserInputsV1,
    ManagedSutAuthorityV1,
    ManagedSutOwnershipTokenV1,
    ManagedSutV1,
    ObservationV1,
    SqliteFileIdentityV1,
    SqliteObservationMetadataV1,
    VerificationEvidenceV1,
    VerificationManifestV1,
    VerifiedExecutionResultV1,
    VerifiedProcessLimitsV1,
    VerifiedProcessReceiptV1,
)

__all__ = [
    "AGENT_JOB_CONTRACTS",
    "ClosedMappingEntryV1",
    "ClosedMappingV1",
    "ExecutionAgentResultV1",
    "ExecutionCommandReceiptV1",
    "ExecutionEvidenceV1",
    "ExecutionDispatchResultV1",
    "ExecutionCycleInputV1",
    "ExecutionCycleResultV1",
    "VerifiedExecutionCycleResultV1",
    "ExecutionManifest",
    "ExecutionReceiptV1",
    "RawTestResultV1",
    "SelectedTargets",
    "EvidenceCompletionV1",
    "FrozenUserInputsV1",
    "ManagedSutAuthorityV1",
    "ManagedSutOwnershipTokenV1",
    "ManagedSutV1",
    "ObservationV1",
    "SqliteFileIdentityV1",
    "SqliteObservationMetadataV1",
    "VerificationEvidenceV1",
    "VerificationManifestV1",
    "VerifiedExecutionResultV1",
    "VerifiedProcessLimitsV1",
    "VerifiedProcessReceiptV1",
    "attempt_contract_refs",
]
