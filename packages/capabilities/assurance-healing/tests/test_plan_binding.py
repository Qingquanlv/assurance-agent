from assurance_healing.contracts.agent import (
    AllocateHealingInputV1,
    FixProposalInputV1,
    RecordApplyInputV1,
    RecordApprovalInputV1,
)
from assurance_healing.contracts.application import (
    AppliedTestRepairV1,
    ApplyTestRepairInputV1,
    VerifiedTestRepairV1,
)


def test_healing_inputs_require_the_frozen_plan() -> None:
    for model in (
        FixProposalInputV1,
        AllocateHealingInputV1,
        RecordApprovalInputV1,
        RecordApplyInputV1,
        ApplyTestRepairInputV1,
        VerifiedTestRepairV1,
        AppliedTestRepairV1,
    ):
        assert model.model_fields["plan_digest"].is_required()
        assert model.model_fields["plan_ref"].is_required()
