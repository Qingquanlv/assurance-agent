"""Archive integrity validator: subject, manifest, summary, and pre-archive tree."""

from __future__ import annotations

from collections.abc import Mapping

from graph_engine.plugin_api import PathWriteSet, ValidationContext, ValidationResult

from assurance_improvement.validators.documents import bytes_match_digest, load_json, rejected
from assurance_improvement.validators.paths import canonical_relative, under_root

_ROOTS = ("qa/results/", "qa/cases/")
_OUTSIDE = "archive candidate may write only archive, case, and authenticated source paths"
_REQUIRED = {
    "subject": "qa/results/subject.json",
    "manifest": "qa/results/artifact-manifest.json",
    "summary": "qa/results/archive-summary.md",
    "pre_archive": "qa/results/pre-archive-tree.json",
}
_MISSING = {key: f"archive is missing the authenticated {key} document" for key in _REQUIRED}
_MISMATCH = {key: f"archive {key} digest does not match the authenticated document" for key in _REQUIRED}
_BYTES = "archive candidate bytes are not authenticated"
_MEMBER = "archive summary is not a locked manifest member"
_SUBJECT = "archive subject is not authenticated"


class ArchiveIntegrityValidator:
    def __init__(
        self,
        *,
        expected: Mapping[str, str] | None = None,
        path_only: bool = False,
        file_bytes: Mapping[str, bytes] | None = None,
    ) -> None:
        self._expected = dict(expected or {})
        self._path_only = path_only
        self._file_bytes = dict(file_bytes or {})

    def validate(self, staged: PathWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        for item in staged.files:
            if not canonical_relative(item.path) or not under_root(item.path, _ROOTS):
                return ValidationResult(accepted=False, reason=_OUTSIDE)
        if self._path_only:
            return ValidationResult(accepted=True)
        listed = {item.path: item.after_sha256 for item in staged.files}
        for key, path in _REQUIRED.items():
            if path not in listed:
                return ValidationResult(accepted=False, reason=_MISSING[key])
        if not self._expected:
            return ValidationResult(accepted=False, reason="archive source digests are not authenticated")
        if any(key not in self._expected for key in _REQUIRED):
            return ValidationResult(accepted=False, reason="archive expected digest map is incomplete")
        for key, path in _REQUIRED.items():
            if listed.get(path) != self._expected[key]:
                return ValidationResult(accepted=False, reason=_MISMATCH[key])
        return self._validate_documents(listed)

    def _validate_documents(self, listed: Mapping[str, str | None]) -> ValidationResult:
        if not self._file_bytes:
            return rejected(_BYTES)
        for key, path in _REQUIRED.items():
            raw = self._file_bytes.get(path)
            if raw is None or not bytes_match_digest(raw, listed.get(path)):
                return rejected(_BYTES)
        subject = load_json(self._file_bytes, _REQUIRED["subject"])
        if not isinstance(subject, dict) or not str(subject.get("change_id") or "").strip():
            return rejected(_SUBJECT)
        manifest = load_json(self._file_bytes, _REQUIRED["manifest"])
        paths: tuple[str, ...]
        if isinstance(manifest, dict):
            raw_paths = manifest.get("artifact_paths", ())
            paths = tuple(str(item) for item in raw_paths) if isinstance(raw_paths, list | tuple) else ()
        elif isinstance(manifest, list | tuple):
            paths = tuple(str(item) for item in manifest)
        else:
            return rejected(_MEMBER)
        if _REQUIRED["summary"] not in paths:
            return rejected(_MEMBER)
        return ValidationResult(accepted=True)
