"""Digest-keyed copies of admitted node outputs for one Run."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path

from graph_engine.attempts.resources.workspace import TaskWorkspaceProvider, TaskWorkspaceStore
from graph_engine.plugin_api import PreparedWorkspaceRef, PromotionReceipt

from assurance_product.operator_views import RunOutputRefV1

_KINDS = frozenset({"obligations", "cases", "tests", "report", "advisory", "retro", "execution", "artifact"})
_logger = logging.getLogger(__name__)


class RunHistoryError(ValueError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def write_projection_bytes(path: Path, content: bytes) -> None:
    """Publish a display copy without sharing a temporary name with another writer."""
    pending: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            pending = Path(stream.name)
            stream.write(content)
        os.replace(pending, path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def preserve_run_output(
    *,
    run_dir: Path,
    change_id: str,
    node_id: str,
    activation_id: str,
    attempt_key: str | None,
    kind: str,
    logical_path: str,
    content: bytes,
) -> RunOutputRefV1:
    if kind not in _KINDS:
        raise RunHistoryError("invalid_input", "output kind is unknown")
    if not change_id or not node_id or not activation_id or not logical_path:
        raise RunHistoryError("invalid_input", "output identity is incomplete")
    if logical_path.startswith(".aa/") or ".." in logical_path.split("/"):
        raise RunHistoryError("invalid_input", "output path is not an admitted product path")
    digest = hashlib.sha256(content).hexdigest()
    output_id = hashlib.sha256(
        f"{change_id}\n{node_id}\n{activation_id}\n{attempt_key or ''}\n{logical_path}\n{digest}".encode()
    ).hexdigest()
    objects = run_dir / "history" / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    destination = objects / digest
    if not destination.is_file():
        write_projection_bytes(destination, content)
    stored = destination.read_bytes()
    if hashlib.sha256(stored).hexdigest() != digest:
        raise RunHistoryError("unavailable", "preserved output digest does not match")
    ref = RunOutputRefV1.model_validate(
        {
            "output_id": output_id,
            "kind": kind,
            "logical_path": logical_path,
            "digest": digest,
            "stored_path": str(destination.resolve()),
            "change_id": change_id,
            "node_id": node_id,
            "activation_id": activation_id,
            "attempt_key": attempt_key,
        }
    )
    _publish_ref(run_dir, ref)
    return ref


def read_preserved_output(run_dir: Path, output: RunOutputRefV1) -> bytes:
    path = Path(output.stored_path)
    objects = (run_dir / "history" / "objects").resolve()
    try:
        resolved = path.resolve()
    except OSError as error:
        raise RunHistoryError("unavailable", "preserved output is missing") from error
    if objects not in resolved.parents or resolved.name != output.digest:
        raise RunHistoryError("unavailable", "preserved output is missing")
    try:
        content = resolved.read_bytes()
    except OSError as error:
        raise RunHistoryError("unavailable", "preserved output is missing") from error
    if hashlib.sha256(content).hexdigest() != output.digest:
        raise RunHistoryError("unavailable", "preserved output digest does not match")
    return content


def _publish_ref(run_dir: Path, ref: RunOutputRefV1) -> None:
    manifest_path = run_dir / "history" / "outputs.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.with_suffix(".lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current: list[dict[str, object]] = []
        if manifest_path.is_file():
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                current = [item for item in raw if isinstance(item, dict)]
        payload = ref.model_dump(mode="json")
        current = [item for item in current if item.get("output_id") != ref.output_id]
        current.append(payload)
        write_projection_bytes(
            manifest_path, (json.dumps(current, indent=2, sort_keys=True) + "\n").encode("utf-8")
        )


def read_output_ref(run_dir: Path, output_id: str) -> RunOutputRefV1:
    manifest_path = run_dir / "history" / "outputs.json"
    if not manifest_path.is_file():
        raise RunHistoryError("unavailable", "preserved output is missing")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RunHistoryError("unavailable", "preserved output is missing") from error
    if not isinstance(raw, list):
        raise RunHistoryError("unavailable", "preserved output is missing")
    for item in raw:
        if isinstance(item, dict) and item.get("output_id") == output_id:
            return RunOutputRefV1.model_validate(item)
    raise RunHistoryError("unavailable", "preserved output is missing")


def preserved_refs(run_dir: Path) -> tuple[RunOutputRefV1, ...]:
    manifest_path = run_dir / "history" / "outputs.json"
    if not manifest_path.is_file():
        return ()
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RunHistoryError("unavailable", "preserved output manifest is unreadable") from error
    if not isinstance(raw, list):
        raise RunHistoryError("unavailable", "preserved output manifest is unreadable")
    return tuple(RunOutputRefV1.model_validate(item) for item in raw if isinstance(item, dict))


def capture_attempt_files(run_dir: Path, attempt_digest: str, files: tuple[tuple[str, bytes], ...]) -> None:
    """Keep the bytes promoted by one attempt before another node can replace them."""
    if len(attempt_digest) != 64 or any(char not in "0123456789abcdef" for char in attempt_digest):
        raise RunHistoryError("invalid_input", "attempt digest is invalid")
    objects = run_dir / "history" / "objects"
    promotions = run_dir / "history" / "promotions"
    objects.mkdir(parents=True, exist_ok=True)
    promotions.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, str]] = []
    for logical_path, content in files:
        if not logical_path.startswith("qa/") or ".." in logical_path.split("/"):
            raise RunHistoryError("invalid_input", "output path is not an admitted product path")
        digest = hashlib.sha256(content).hexdigest()
        destination = objects / digest
        if not destination.is_file():
            write_projection_bytes(destination, content)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise RunHistoryError("unavailable", "promoted output digest does not match")
        entries.append({"logical_path": logical_path, "digest": digest})
    path = promotions / f"{attempt_digest}.json"
    encoded = json.dumps(sorted(entries, key=lambda item: item["logical_path"]), sort_keys=True)
    if path.is_file():
        if path.read_text(encoding="utf-8") != encoded:
            raise RunHistoryError("unavailable", "attempt output changed after promotion")
        return
    write_projection_bytes(path, encoded.encode("utf-8"))


def publish_attempt_outputs(
    run_dir: Path,
    attempt_digest: str,
    change_id: str,
    node_id: str,
    activation_id: str,
    attempt_key: str | None,
) -> tuple[RunOutputRefV1, ...]:
    path = run_dir / "history" / "promotions" / f"{attempt_digest}.json"
    if not path.is_file():
        return ()
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RunHistoryError("unavailable", "attempt output record is unreadable") from error
    if not isinstance(entries, list):
        raise RunHistoryError("unavailable", "attempt output record is unreadable")
    existing = {
        (ref.node_id, ref.activation_id, ref.attempt_key, ref.logical_path, ref.digest): ref
        for ref in preserved_refs(run_dir)
        if ref.change_id == change_id and ref.kind == "artifact"
    }
    refs: list[RunOutputRefV1] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise RunHistoryError("unavailable", "attempt output record is unreadable")
        logical_path = entry.get("logical_path")
        digest = entry.get("digest")
        if (
            not isinstance(logical_path, str)
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise RunHistoryError("unavailable", "attempt output record is unreadable")
        known = existing.get((node_id, activation_id, attempt_key, logical_path, digest))
        if known is not None:
            refs.append(known)
            continue
        try:
            content = (run_dir / "history" / "objects" / digest).read_bytes()
        except OSError as error:
            raise RunHistoryError("unavailable", "promoted output is missing") from error
        if hashlib.sha256(content).hexdigest() != digest:
            raise RunHistoryError("unavailable", "promoted output digest does not match")
        refs.append(
            preserve_run_output(
                run_dir=run_dir,
                change_id=change_id,
                node_id=node_id,
                activation_id=activation_id,
                attempt_key=attempt_key,
                kind="artifact",
                logical_path=logical_path,
                content=content,
            )
        )
    return tuple(refs)


class RunOutputWorkspaceProvider(TaskWorkspaceProvider):
    """Capture promoted bytes under the Run that owns this provider."""

    def __init__(self, store: TaskWorkspaceStore, run_dir: Path) -> None:
        super().__init__(store)
        self.run_dir = run_dir

    def _capture(self, prepared: PreparedWorkspaceRef) -> None:
        if not self.run_dir.is_dir():
            return
        try:
            capture_attempt_files(
                self.run_dir,
                prepared.identity.task_id,
                tuple(
                    (item.path, item.content) for item in prepared.sealed.files if item.path.startswith("qa/")
                ),
            )
        except (OSError, ValueError) as error:
            _logger.warning("run_output_capture_failed: %s", error)

    async def promote(self, prepared: PreparedWorkspaceRef) -> PromotionReceipt:
        self._capture(prepared)
        return await super().promote(prepared)

    async def recover_promotion(self, prepared: PreparedWorkspaceRef) -> PromotionReceipt:
        self._capture(prepared)
        return await super().recover_promotion(prepared)
