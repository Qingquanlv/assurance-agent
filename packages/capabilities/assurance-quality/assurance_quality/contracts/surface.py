"""Live UI exploration and API discovery documents. Not fact-baseline."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

SurfaceSource = Literal["live", "unavailable", "unused"]
FeatureStatus = Literal["explored", "partial", "unreached"]
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
ApiAuth = Literal["bearer", "api_key", "basic", "none"]
TestFamily = Literal["api", "e2e", "fuzz", "performance"]
UI_EXPLORATION_PATH = "qa/results/facts/ui-exploration.json"
API_DISCOVERY_PATH = "qa/results/facts/api-discovery.json"
_FROZEN = ConfigDict(extra="forbid", frozen=True)
_UiPath = Annotated[str, Field(pattern=r"^/")]


class ExploredPage(BaseModel):
    model_config = _FROZEN

    path: str = Field(pattern=r"^/")
    landed_path: str = Field(pattern=r"^/")


class UiFeature(BaseModel):
    model_config = _FROZEN

    name: str = Field(min_length=1)
    status: FeatureStatus
    use_cases_reached: int = Field(ge=0)
    use_cases_total: int = Field(ge=1)
    flows: tuple[str, ...]
    pages: tuple[ExploredPage, ...]
    summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def _counts_match_status(self) -> "UiFeature":
        if self.use_cases_reached > self.use_cases_total:
            raise ValueError("use_cases_reached cannot exceed use_cases_total")
        if self.status == "explored" and self.use_cases_reached != self.use_cases_total:
            raise ValueError("explored requires every use case")
        if self.status == "partial" and not (0 < self.use_cases_reached < self.use_cases_total):
            raise ValueError("partial requires a proper prefix of the use cases")
        if self.status == "unreached" and (self.use_cases_reached != 0 or self.pages):
            raise ValueError("unreached has no reached use cases and no pages")
        if self.status != "unreached" and not self.pages:
            raise ValueError("reached features require pages")
        return self


class UiExplorationDocument(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: str = Field(min_length=1)
    source: SurfaceSource
    base_url: str
    warnings: tuple[str, ...]
    features: tuple[UiFeature, ...]

    @model_validator(mode="after")
    def _empty_when_not_live(self) -> "UiExplorationDocument":
        if self.source != "live" and self.features:
            raise ValueError("only a live UI document may contain features")
        if self.source == "unavailable" and not self.warnings:
            raise ValueError("unavailable UI document requires a warning")
        return self

    def allowed_paths(self) -> set[str]:
        return {
            page.path
            for feature in self.features
            if feature.status in {"explored", "partial"}
            for page in feature.pages
        }


class ApiRequestShape(BaseModel):
    model_config = _FROZEN

    required_headers: tuple[str, ...]
    query: tuple[str, ...]
    body_fields: tuple[str, ...]


class ApiResponseShape(BaseModel):
    model_config = _FROZEN

    status_codes: tuple[int, ...] = Field(min_length=1)
    body_fields: tuple[str, ...]


class ApiOperation(BaseModel):
    model_config = _FROZEN

    method: HttpMethod
    path: str = Field(pattern=r"^/")
    request: ApiRequestShape
    response: ApiResponseShape


class ApiFamily(BaseModel):
    model_config = _FROZEN

    name: str = Field(min_length=1)
    auth: ApiAuth
    operations: tuple[ApiOperation, ...] = Field(min_length=1)


class ApiDiscoveryDocument(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: str = Field(min_length=1)
    source: SurfaceSource
    base_url: str
    warnings: tuple[str, ...]
    families: tuple[ApiFamily, ...]

    @model_validator(mode="after")
    def _empty_when_not_live(self) -> "ApiDiscoveryDocument":
        if self.source != "live" and self.families:
            raise ValueError("only a live API document may contain families")
        if self.source == "unavailable" and not self.warnings:
            raise ValueError("unavailable API document requires a warning")
        return self

    def operation_keys(self) -> set[tuple[str, str]]:
        return {
            (operation.method, operation.path) for family in self.families for operation in family.operations
        }


class SurfaceProbeInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    candidate_test_families: tuple[TestFamily, ...] = ()
    api_base_url: str | None = None
    ui_base_url: str | None = None
    ui_paths: tuple[_UiPath, ...] = ()


class SurfaceProbeResultV1(FrozenModel):
    ui_exploration_ref: EvidenceArtifactRefV1
    api_discovery_ref: EvidenceArtifactRefV1
    ui_source: SurfaceSource
    api_source: SurfaceSource
