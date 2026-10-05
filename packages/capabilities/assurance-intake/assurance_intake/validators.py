"""Commit-time binding of Intake's validated artifact bytes to the Kernel seal."""

from __future__ import annotations

from collections.abc import Mapping

from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import PathWriteSet, ValidationContext, ValidationResult

SEALED_ARTIFACT_REFS_VALIDATOR_ID = "assurance.intake.validator.sealed-artifact-refs.v1"


def _ref(value: object) -> tuple[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("Intake output artifact ref is not a mapping")
    path, digest = value.get("path"), value.get("digest")
    if not isinstance(path, str) or not isinstance(digest, str):
        raise ValueError("Intake output artifact ref lacks a path or digest")
    return path, digest


_OWNED_OUTPUT_REF_FIELDS = ("plan_ref", "preparation_refs_ref", "rework_ref")


def _output_refs(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("Intake task output is not a mapping")
    refs: dict[str, str] = {}
    if "artifacts" in value:
        artifacts = value["artifacts"]
        if not isinstance(artifacts, list | tuple):
            raise ValueError("Intake output artifacts are not a list")
        candidates = artifacts
    else:
        candidates = [
            value[field] for field in _OWNED_OUTPUT_REF_FIELDS if field in value and value[field] is not None
        ]
        plan = value.get("plan")
        if isinstance(plan, Mapping) and plan.get("exploration_ref") is not None:
            candidates.append(plan["exploration_ref"])
        if not candidates:
            raise ValueError("Intake output has no artifact refs")
    for item in candidates:
        path, digest = _ref(item)
        previous = refs.get(path)
        if previous is not None and previous != digest:
            raise ValueError(f"conflicting artifact digests: {path}")
        refs[path] = digest
    return refs


def _authenticated_baselines(output: object, task_input: object) -> dict[str, str]:
    allowed: dict[str, str] = {}
    if isinstance(output, Mapping):
        repair = output.get("review_repair")
        if isinstance(repair, Mapping):
            raw = repair.get("baseline_file_digests")
            if isinstance(raw, Mapping):
                allowed.update(
                    {
                        path: digest
                        for path, digest in raw.items()
                        if isinstance(path, str) and isinstance(digest, str)
                    }
                )
        if "plan" in output and isinstance(task_input, Mapping):
            source = task_input.get("exploration_ref")
            if source is not None:
                path, digest = _ref(source)
                allowed[path] = digest
    return allowed


class SealedArtifactRefsValidator:
    def validate(self, staged: PathWriteSet, context: ValidationContext) -> ValidationResult:
        try:
            output = thaw_json(context.task_output)
            refs = _output_refs(output)
            baselines = _authenticated_baselines(output, thaw_json(context.task_input))
        except ValueError as error:
            return ValidationResult(accepted=False, reason=str(error))
        sealed = {file.path: file for file in staged.files}
        for file in staged.files:
            expected = refs.get(file.path)
            if expected is None:
                return ValidationResult(
                    accepted=False,
                    reason=f"unbound Intake staged file: {file.path}",
                )
            if file.after_sha256 != expected:
                return ValidationResult(
                    accepted=False,
                    reason=f"Intake artifact changed before seal: {file.path}",
                )
        for path, digest in refs.items():
            if path not in sealed and baselines.get(path) != digest:
                return ValidationResult(
                    accepted=False,
                    reason=f"Intake output is missing from seal: {path}",
                )
        return ValidationResult(accepted=True)
