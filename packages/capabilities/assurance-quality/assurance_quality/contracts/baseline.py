"""explore/advisory.json and facts/fact-baseline.json (must_compat).

Transcribed from src/schema/advisory.ts and src/schema/fact_baseline.ts.
zod's z.any() fields accept absent keys, hence `Any = None` defaults here;
z.array(...) fields are required, hence no default.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from assurance_intake.contracts import NonEmptyStr
from assurance_intake.contracts.obligations import PreparedObligationV1


class SourceCodeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: NonEmptyStr
    source: Literal["source_code"]
    type: NonEmptyStr
    description: NonEmptyStr
    parse_confidence_cap: Literal["medium", "low"] = "medium"
    module: str | None = None


class Advisory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: NonEmptyStr
    watchlist: list[Any]
    open_questions_for_case_design: list[Any]
    change_id: Any = None
    context_ref: Any = None
    generated_at: Any = None
    executive_summary: Any = None
    evidence_inventory: Any = None
    case_design_guidance: Any = None
    minimum_required_coverage: tuple[PreparedObligationV1, ...] | None = None
    source_code_evidence: list[SourceCodeEvidence] = Field(default_factory=list)


class FactBaselineUnavailable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["unavailable"]
    warnings: list[Any]
    facts: Any = None


class FactBaselineFull(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["seed_file", "db_probe", "both"]
    schema_version: NonEmptyStr
    warnings: list[Any]
    change_id: Any = None
    generated_at: Any = None
    seed_file: Any = None
    facts: Any = None


def _reject_endpoint_inventories(facts: object) -> None:
    if not isinstance(facts, dict):
        return
    inventories = sorted(
        key
        for key in facts
        if isinstance(key, str) and (key.casefold() == "endpoints" or key.casefold().endswith("_endpoints"))
    )
    if inventories:
        raise ValueError(
            "FactBaseline endpoint inventories are out of scope; "
            f"remove {inventories!r} and let Explore/Plan verify the complete route surface"
        )


class FactBaselineFullAuthoring(FactBaselineFull):
    """Newly authored facts stay within FactBaseline's stable-fact remit."""

    @model_validator(mode="after")
    def _forbid_endpoint_inventories(self) -> "FactBaselineFullAuthoring":
        _reject_endpoint_inventories(self.facts)
        return self


FactBaselineVariant = Annotated[FactBaselineUnavailable | FactBaselineFull, Field(discriminator="source")]


class FactBaseline(RootModel[FactBaselineVariant]):
    """Discriminated union on `source`, mirroring the TS z.discriminatedUnion."""


FactBaselineAuthoringVariant = Annotated[
    FactBaselineUnavailable | FactBaselineFullAuthoring,
    Field(discriminator="source"),
]


class FactBaselineAuthoring(RootModel[FactBaselineAuthoringVariant]):
    """Authoring contract for current FactBaseline documents."""

    model_config = ConfigDict(
        json_schema_extra={
            "prompt_notes": [
                "facts may contain stable auth, role, seed, route-prefix, token, or single "
                "login-endpoint facts",
                "do not emit facts.endpoints or facts.*_endpoints inventories; Explore and Plan "
                "own complete route verification",
            ]
        }
    )

    @model_validator(mode="after")
    def _forbid_unavailable_endpoint_inventories(self) -> "FactBaselineAuthoring":
        if not isinstance(self.root, FactBaselineFullAuthoring):
            _reject_endpoint_inventories(self.root.facts)
        return self
