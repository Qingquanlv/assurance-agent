"""explore/advisory.json and facts/fact-baseline.json (must_compat).

Transcribed from src/schema/advisory.ts and src/schema/fact_baseline.ts.
zod's z.any() fields accept absent keys, hence `Any = None` defaults here;
z.array(...) fields are required, hence no default.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

from assurance_agent.artifacts.models.common import NonEmptyStr


class Advisory(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: NonEmptyStr
    watchlist: list[Any]
    open_questions_for_case_design: list[Any]
    change_id: Any = None
    context_ref: Any = None
    generated_at: Any = None
    executive_summary: Any = None
    evidence_inventory: Any = None
    case_design_guidance: Any = None
    minimum_required_coverage: Any = None


class FactBaselineUnavailable(BaseModel):
    model_config = ConfigDict(extra="allow")

    source: Literal["unavailable"]
    warnings: list[Any]
    facts: Any = None


class FactBaselineFull(BaseModel):
    model_config = ConfigDict(extra="allow")

    source: Literal["seed_file", "db_probe", "both"]
    schema_version: NonEmptyStr
    warnings: list[Any]
    change_id: Any = None
    generated_at: Any = None
    seed_file: Any = None
    facts: Any = None


FactBaselineVariant = Annotated[FactBaselineUnavailable | FactBaselineFull, Field(discriminator="source")]


class FactBaseline(RootModel[FactBaselineVariant]):
    """Discriminated union on `source`, mirroring the TS z.discriminatedUnion."""
