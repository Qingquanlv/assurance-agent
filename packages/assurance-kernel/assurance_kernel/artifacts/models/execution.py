"""execution/execution-manifest.yaml — written by `aa run` (versioned).

Transcribed from src/schema/execution_manifest.ts. final_status / batch_id /
selected_targets are must_compat fields: healing gates and benchmark scripts
reference them by these exact names.
"""

from typing import Literal

from pydantic import AwareDatetime, BaseModel

from assurance_kernel.artifacts.models.common import GateStatus, NonEmptyStr


class SelectedTargets(BaseModel):
    api: bool
    e2e: bool
    fuzz: bool
    performance: bool


class ExecutionManifest(BaseModel):
    """One published execution batch.

    ``executed_at`` is the batch's authoritative instant and is ``AwareDatetime``:
    a naive value is rejected rather than localized to a guessed zone, because
    recency judgements downstream compare it against an aware ``as_of``. It stays
    optional so manifests written before it existed keep loading — those fall
    back to the batch-id UTC approximation, which is exactly why every new
    manifest must publish it.
    """

    schema_version: Literal["1.0"]
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    executed_at: AwareDatetime | None = None
    selected_targets: SelectedTargets
    result_files: dict[str, str]
    tests_tree_sha256: str | None = None
    test_files_sha256: dict[str, str] | None = None
    product_tree_sha256: str | None = None
    final_status: GateStatus | None = None
    executed_at: AwareDatetime | None = None
