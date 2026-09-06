"""Closed four-family plan validators and the mechanical dispatcher."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import cast

from pydantic import ValidationError

from graph_engine.plugin_api import PathWriteSet, ValidationContext, ValidationResult

from assurance_generation.contracts.agent import under_write_root
from assurance_generation.contracts.execution_plan import CaseExecutionPlanSetV1, ExecutionBindingsV1
from assurance_generation.contracts.families import LAYER_NAMES, LayerName
from assurance_generation.contracts.plans import PlanResultV1, canonical_relative_path

Family = LayerName
FAMILIES: tuple[Family, ...] = LAYER_NAMES


def closed_family(family: str) -> Family:
    if family not in FAMILIES:
        raise ValueError(f"unknown generation family: {family}")
    return cast(Family, family)


_DEFAULT_ROOTS = ("qa/changes/",)
_OUTSIDE_REASON = "generation plan candidate may write only declared plan write roots"
_UNLISTED_REASON = "generation plan candidate contains an unlisted file"


def _canonical_relative(path: str) -> bool:
    try:
        canonical_relative_path(path)
    except ValueError:
        return False
    return True


def _allowed_plan_path(path: str, write_roots: tuple[str, ...]) -> bool:
    return _canonical_relative(path) and under_write_root(path, write_roots)


def _plan_document_name(family: Family) -> str:
    return f"{family}-plan.json"


class FamilyPlanValidator:
    def __init__(
        self,
        family: Family,
        *,
        capability_leafs: frozenset[str] | None = None,
        case_ids: frozenset[str] | None = None,
        file_bytes: Mapping[str, bytes] | None = None,
        write_roots: tuple[str, ...] = _DEFAULT_ROOTS,
    ) -> None:
        self._family: Family = closed_family(family)
        self._capability_leafs = capability_leafs
        self._case_ids = case_ids
        self._file_bytes = dict(file_bytes or {})
        self._write_roots = write_roots

    def validate(self, staged: PathWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        listed = tuple(file.path for file in staged.files)
        for path in listed:
            if not _allowed_plan_path(path, self._write_roots):
                return ValidationResult(accepted=False, reason=_OUTSIDE_REASON)
        extra = sorted(set(self._file_bytes) - set(listed))
        if extra:
            return ValidationResult(accepted=False, reason=_UNLISTED_REASON)
        if not self._file_bytes or self._capability_leafs is None:
            return ValidationResult(accepted=True)
        return self._validate_documents()

    def _validate_documents(self) -> ValidationResult:
        documents = list(self._file_bytes.items())
        if not documents:
            return ValidationResult(accepted=True)
        leafs = self._capability_leafs
        assert leafs is not None
        for path, payload in documents:
            name = PurePosixPath(path).name
            if name not in {
                _plan_document_name(self._family),
                "api-execution-bindings.json",
                "api-case-execution-plan.json",
            }:
                continue
            try:
                raw = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                return ValidationResult(accepted=False, reason=str(error))
            if name == "api-execution-bindings.json":
                if self._family != "api":
                    return ValidationResult(accepted=False, reason="execution bindings belong to api")
                try:
                    bindings = ExecutionBindingsV1.model_validate(raw)
                except ValidationError as error:
                    return ValidationResult(accepted=False, reason=str(error))
                if self._case_ids is not None and bindings.case_id not in self._case_ids:
                    return ValidationResult(
                        accepted=False,
                        reason=f"unresolved case IDs: {[bindings.case_id]}",
                    )
                continue
            if name == "api-case-execution-plan.json":
                if self._family != "api":
                    return ValidationResult(accepted=False, reason="case execution plan belongs to api")
                try:
                    plan_set = CaseExecutionPlanSetV1.model_validate(raw)
                except ValidationError as error:
                    return ValidationResult(accepted=False, reason=str(error))
                if self._case_ids is not None:
                    unknown = [case.case_id for case in plan_set.cases if case.case_id not in self._case_ids]
                    if unknown:
                        return ValidationResult(accepted=False, reason=f"unresolved case IDs: {unknown}")
                continue
            family = raw.get("family") if isinstance(raw, dict) else None
            if family != self._family:
                return ValidationResult(
                    accepted=False,
                    reason=f"plan family {family!r} does not match validator family {self._family!r}",
                )
            try:
                document = PlanResultV1.model_validate(raw, context={"capability_leafs": leafs})
            except (ValidationError, ValueError) as error:
                return ValidationResult(accepted=False, reason=str(error))
            if self._case_ids is not None:
                unknown = [item for item in document.case_ids if item not in self._case_ids]
                if unknown:
                    return ValidationResult(
                        accepted=False,
                        reason=f"unresolved case IDs: {unknown}",
                    )
            for output in document.output_files:
                if not _allowed_plan_path(output, self._write_roots):
                    return ValidationResult(accepted=False, reason=_OUTSIDE_REASON)
        return ValidationResult(accepted=True)


def family_validator(
    family: str,
    *,
    capability_leafs: frozenset[str] | None = None,
    case_ids: frozenset[str] | None = None,
    file_bytes: Mapping[str, bytes] | None = None,
    write_roots: tuple[str, ...] = _DEFAULT_ROOTS,
) -> FamilyPlanValidator:
    return FamilyPlanValidator(
        closed_family(family),
        capability_leafs=capability_leafs,
        case_ids=case_ids,
        file_bytes=file_bytes,
        write_roots=write_roots,
    )


class PlanMechanicalValidator:
    def __init__(
        self,
        *,
        capability_leafs: frozenset[str] | None = None,
        case_ids: frozenset[str] | None = None,
        file_bytes: Mapping[str, bytes] | None = None,
        write_roots: tuple[str, ...] = _DEFAULT_ROOTS,
    ) -> None:
        self._capability_leafs = capability_leafs
        self._case_ids = case_ids
        self._file_bytes = dict(file_bytes or {})
        self._write_roots = write_roots
        self._table: Mapping[Family, FamilyPlanValidator] = {
            family: FamilyPlanValidator(
                family,
                capability_leafs=capability_leafs,
                case_ids=case_ids,
                file_bytes=self._file_bytes,
                write_roots=write_roots,
            )
            for family in FAMILIES
        }

    def validate(self, staged: PathWriteSet, context: ValidationContext) -> ValidationResult:
        listed = tuple(file.path for file in staged.files)
        for path in listed:
            if not _allowed_plan_path(path, self._write_roots):
                return ValidationResult(accepted=False, reason=_OUTSIDE_REASON)
        if not self._file_bytes or self._capability_leafs is None:
            return ValidationResult(accepted=True)
        discovered: list[tuple[Family, str]] = []
        for path, payload in self._file_bytes.items():
            name = PurePosixPath(path).name
            if name in {"api-execution-bindings.json", "api-case-execution-plan.json"}:
                discovered.append(("api", path))
                continue
            if not name.endswith("-plan.json"):
                continue
            stem = name.removesuffix("-plan.json")
            try:
                raw = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                return ValidationResult(accepted=False, reason=str(error))
            family = raw.get("family") if isinstance(raw, dict) else None
            if family not in FAMILIES:
                return ValidationResult(
                    accepted=False, reason=f"unknown plan family discriminator: {family!r}"
                )
            if stem != family:
                return ValidationResult(
                    accepted=False,
                    reason=f"plan family discriminator {family!r} does not match document {name}",
                )
            discovered.append((closed_family(str(family)), path))
        families = {item[0] for item in discovered}
        if len(families) > 1:
            return ValidationResult(
                accepted=False, reason=f"multiple plan families in candidate: {sorted(families)}"
            )
        if not families:
            return ValidationResult(accepted=True)
        family = closed_family(next(iter(families)))
        return self._table[family].validate(staged, context)


__all__ = [
    "FAMILIES",
    "Family",
    "FamilyPlanValidator",
    "LayerName",
    "PlanMechanicalValidator",
    "family_validator",
]
