"""Change-local adversarial discovery workflow helpers (Phase 1 API slice)."""

from assurance_agent.workflow.discovery.campaign import (
    CampaignAttemptRunner,
    CampaignError,
    CampaignOutcome,
    ExecutionObservation,
    StrategySnapshot,
    run_deterministic_api_campaign,
)
from assurance_agent.workflow.discovery.candidates import (
    CandidateBuildError,
    build_regression_candidate,
)
from assurance_agent.workflow.discovery.ce_bridge import (
    CeBridgeError,
    CeIngestResult,
    confirmed_ces_to_candidate_document,
    counterexample_to_observation,
    ingest_confirmed_counterexamples,
)
from assurance_agent.workflow.discovery.materialize import (
    MaterializationError,
    MaterializationReceipt,
    MaterializeResult,
    destroy_workspace,
    materialize_round,
    materialize_test_overlay,
    validate_generated_manifest_op,
)
from assurance_agent.workflow.discovery.replay_receipts import (
    ReplayReceiptIntegrityError,
    load_replay_attempt_receipts,
    write_replay_attempt_receipts,
)

__all__ = [
    "CampaignAttemptRunner",
    "CampaignError",
    "CampaignOutcome",
    "CandidateBuildError",
    "CeBridgeError",
    "CeIngestResult",
    "ExecutionObservation",
    "MaterializationError",
    "MaterializationReceipt",
    "MaterializeResult",
    "ReplayReceiptIntegrityError",
    "StrategySnapshot",
    "build_regression_candidate",
    "confirmed_ces_to_candidate_document",
    "counterexample_to_observation",
    "destroy_workspace",
    "ingest_confirmed_counterexamples",
    "load_replay_attempt_receipts",
    "materialize_round",
    "materialize_test_overlay",
    "run_deterministic_api_campaign",
    "validate_generated_manifest_op",
    "write_replay_attempt_receipts",
]
