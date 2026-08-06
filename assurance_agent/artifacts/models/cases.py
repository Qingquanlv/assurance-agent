"""cases/**/case.yaml and .qa.yaml — skill-authored case artifacts (must_compat).

Field names, optionality and enum values transcribed one-for-one from the TS
validators src/schema/case_yaml.ts and src/schema/qa_yaml.ts.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assurance_agent.artifacts.models.common import CaseId, NonEmptyStr, RiskTier

# Named because the risk tier lower bound (`evidence/risk_tier.py`) is a total
# function of these two vocabularies, and a value added here without a bound
# there would silently resolve to the lowest band.
CasePriority = Literal["P0", "P1", "P2", "P3"]
CaseSeverity = Literal["blocker", "critical", "major", "minor"]


class CaseRisk(BaseModel):
    """The author's own risk assessment of a case.

    Real ``case.yaml`` documents have carried this block all along while
    ``CaseEntry`` declared nothing, so ``level`` — the field that selects an
    evidence floor band — reached consumers unvalidated: an authoring model
    labelled a P0 case ``medium`` and nothing objected. Declaring it closes the
    typo half of that hole; the *self-assessment* half is closed elsewhere, by
    ``evidence.risk_tier`` treating this level as a value that may only raise a
    mechanical bound derived from ``priority``/``severity``.

    ``level`` is required and extras are refused, because a risk block whose one
    load-bearing field is absent or misspelled is exactly the document this
    change exists to reject. ``likelihood``/``impact`` stay optional and
    unbounded: they are inputs to the author's own reasoning, nothing mechanical
    reads them, and narrowing a scale nobody consumes would only invalidate
    documents for no gain.
    """

    model_config = ConfigDict(extra="forbid")

    level: RiskTier
    likelihood: int | None = None
    impact: int | None = None
    rationale: str = ""


class CaseEntry(BaseModel):
    case_id: CaseId
    title: NonEmptyStr
    status: Literal["draft", "active", "deprecated"]
    priority: CasePriority
    severity: CaseSeverity
    type: Literal["API", "E2E", "Fuzz", "Performance"]
    module: NonEmptyStr
    # Optional so the many documents written before it stay valid; the model
    # deliberately keeps accepting extra keys (skills author far more fields than
    # this transcription declares), so absence here means "not stated", never
    # "stated as low".
    risk: CaseRisk | None = None


class CaseRemoval(BaseModel):
    case_id: CaseId


class CaseYaml(BaseModel):
    schema_version: NonEmptyStr
    added: list[CaseEntry]
    modified: list[CaseEntry]
    removed: list[CaseRemoval]


class QaChange(BaseModel):
    change_id: NonEmptyStr
    requirement_id: NonEmptyStr
    feature_name: NonEmptyStr
    status: NonEmptyStr


class QaCaseTarget(BaseModel):
    module: NonEmptyStr
    change_case_file: NonEmptyStr
    target_case_file: NonEmptyStr


class QaTargets(BaseModel):
    cases: list[QaCaseTarget]


class QaWorkflow(BaseModel):
    current_step: str | None = None
    next_step: str | None = None


class QaYaml(BaseModel):
    # The artifact has a literal "schema" key; that name shadows a BaseModel
    # attribute, so the field is schema_ with an input alias.
    model_config = ConfigDict(populate_by_name=True)

    schema_version: NonEmptyStr
    schema_: NonEmptyStr = Field(alias="schema")
    created_at: NonEmptyStr
    change: QaChange
    targets: QaTargets
    workflow: QaWorkflow | None = None
