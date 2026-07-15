"""cases/**/case.yaml and .qa.yaml — skill-authored case artifacts (must_compat).

Field names, optionality and enum values transcribed one-for-one from the TS
validators src/schema/case_yaml.ts and src/schema/qa_yaml.ts.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assurance_agent.artifacts.models.common import CaseId, NonEmptyStr


class CaseEntry(BaseModel):
    case_id: CaseId
    title: NonEmptyStr
    status: Literal["draft", "active", "deprecated"]
    priority: Literal["P0", "P1", "P2", "P3"]
    severity: Literal["blocker", "critical", "major", "minor"]
    type: Literal["API", "E2E", "Fuzz", "Performance"]
    module: NonEmptyStr


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
