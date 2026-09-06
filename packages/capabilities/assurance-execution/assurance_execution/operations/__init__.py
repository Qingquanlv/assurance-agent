from collections.abc import Mapping
from types import MappingProxyType

from graph_engine.plugin_api import TaskHandler

from assurance_execution.operations.agent_skills import (
    ExecuteFinalizeHandler,
    ExecutePrepareHandler,
    RunFinalizeHandler,
    RunPrepareHandler,
)
from assurance_execution.operations.normalize import NormalizeHandler
from assurance_execution.operations.runner import (
    ConfinedExecutionProcessHost,
    ExecutionProcessHost,
    RunTestsAndCollectPrMetricsHandler,
    RunTestsHandler,
)
from assurance_execution.operations.selection import SelectHandler
from assurance_execution.operations.sqlite_oracle import observe_user
from assurance_execution.operations.verification_manifest import (
    allocate_user_inputs,
    authenticate_verification_manifest,
    build_managed_sut_authority,
    build_verification_manifest,
    sqlite_file_identity,
)

from assurance_execution.operations.managed_sut import authenticate_managed_sut_receipts
from assurance_execution.operations.verified_execution import (
    VerifiedExecutionHandler,
    VerifiedExecutionInputV1,
)
from assurance_execution.operations.verified_process import DockerVerificationHost, VerifiedProcessReceiptV1


def execution_handlers(
    *,
    process_host: ExecutionProcessHost | None = None,
) -> Mapping[str, TaskHandler]:
    host = process_host or ConfinedExecutionProcessHost()
    return MappingProxyType(
        {
            "assurance.execution.execute.finalize": ExecuteFinalizeHandler(),
            "assurance.execution.execute.prepare": ExecutePrepareHandler(),
            "assurance.execution.normalize": NormalizeHandler(),
            "assurance.execution.run-tests": RunTestsHandler(process_host=host),
            "assurance.execution.run-tests-and-collect-pr-metrics": RunTestsAndCollectPrMetricsHandler(
                process_host=host
            ),
            "assurance.execution.run.finalize": RunFinalizeHandler(),
            "assurance.execution.run.prepare": RunPrepareHandler(),
            "assurance.execution.select": SelectHandler(),
        }
    )


__all__ = [
    "ConfinedExecutionProcessHost",
    "DockerVerificationHost",
    "VerifiedExecutionHandler",
    "VerifiedExecutionInputV1",
    "VerifiedProcessReceiptV1",
    "authenticate_managed_sut_receipts",
    "ExecuteFinalizeHandler",
    "ExecutePrepareHandler",
    "ExecutionProcessHost",
    "NormalizeHandler",
    "RunFinalizeHandler",
    "RunPrepareHandler",
    "RunTestsAndCollectPrMetricsHandler",
    "RunTestsHandler",
    "SelectHandler",
    "allocate_user_inputs",
    "authenticate_verification_manifest",
    "build_managed_sut_authority",
    "build_verification_manifest",
    "execution_handlers",
    "observe_user",
    "sqlite_file_identity",
]
