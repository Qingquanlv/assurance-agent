from __future__ import annotations

from pydantic import Field

from graph_engine.plugin_api import FrozenModel


class DataKnowledgeRefV1(FrozenModel):
    resource_id: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class InitTestRuntimeInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    data_knowledge: DataKnowledgeRefV1
    capability_leafs: tuple[str, ...] = ()
    allowed_artifact_paths: tuple[str, ...] = ()


class InitTestRuntimeResultV1(FrozenModel):
    schema_version: str = "1"
    change_id: str
    harness_created: tuple[str, ...] = ()
    harness_skipped: tuple[str, ...] = ()
    symbol_files_created: tuple[str, ...] = ()
    symbol_files_skipped: tuple[str, ...] = ()
    symbols_declared: tuple[str, ...] = ()
