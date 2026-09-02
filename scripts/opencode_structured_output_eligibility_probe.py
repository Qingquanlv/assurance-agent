#!/usr/bin/env python3
"""Standalone exact-release OpenCode structured-output eligibility probe (Checkpoint S0, Task 0).

This is a **standalone two-phase CLI** -- not a production adapter change. It talks to a
direct native OpenCode loopback origin with ``httpx`` and emits closed, non-promotable
owner-only evidence describing whether an exact OpenCode release/provider/model candidate
can honour the structured-output seam (``format.type=json_schema`` + terminal
``info.structured``) and survive an operator restart.

Two phases::

    pre-restart  -> OpenCodeStructuredOutputEligibilityPreStateV1
                    status in {message_roundtrip_red, provider_canary_red, awaiting_restart}
    post-restart -> OpenCodeStructuredOutputEligibilityReportV1
                    status in {post_restart_red, eligible}

Only an ``awaiting_restart`` pre-state may enter ``post-restart``. Every status and the final
``eligible`` value is derived from recorded checks, never caller supplied. All identity inputs
(release tag/commit/asset/platform/arch, expected version, candidate-binary SHA-256, endpoint
origin, OpenCode data-root identity, workspace ``?directory=`` scope, auth-mode/username) are
**explicitly operator-trusted** eligibility inputs: the probe does not claim that a local file
hash cryptographically identifies the serving process or that a restart occurred. It records
``restart_evidence_level = "operator_executed_unverified"``; proving old-instance to
new-instance recovery is deferred to later Structured tasks. This report must never be
promoted into ``capabilities_for(...)``, installed resources, Product Boot, or a binding wheel.

Deliberately imports neither ``graph_engine``, ``agent_runtime_contracts``,
``agent_runtime_opencode``, a Capability package, nor Product code.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

# --------------------------------------------------------------------------------------
# Fixed local constants (a single fixed canary schema; there is no Agent contract here)
# --------------------------------------------------------------------------------------

CANARY_VALUE = "AA_OPENCODE_S0_V1"

FIXED_CANARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"canary": {"type": "string", "const": CANARY_VALUE}},
    "required": ["canary"],
}

FIXED_NO_REPLY_INSTRUCTION = (
    "AA S0 no-model roundtrip probe: do not reply. This message exists only so the server "
    "persists the submitted json_schema format for a message list/single re-read."
)
FIXED_CANARY_INSTRUCTION = (
    "AA S0 structured-output canary: respond with exactly the JSON object "
    '{"canary": "AA_OPENCODE_S0_V1"} in structured output and nothing else.'
)

# Probe-generated official-shaped caller message IDs: ``msg_`` + a 26-character suffix.
CALLER_NO_REPLY_MESSAGE_ID = "msg_s0probenoreplyusermsgid000"
CALLER_CANARY_MESSAGE_ID = "msg_s0probecanaryusermsgid0000"

EXPECTED_SERVER_RETRY_COUNT = 2

# Fixed defaults for the bounded transport / polling budget. Overridable only for tests.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.25
DEFAULT_POLL_DEADLINE_SECONDS = 12.0
DEFAULT_MAX_RESPONSE_BYTES = 1_048_576
MAX_CANDIDATE_BYTES = 4_096
MAX_PASSWORD_BYTES = 4_096

EXIT_OK = 0
EXIT_ERROR = 1  # input / sink / fsync failure: exits nonzero WITHOUT claiming a report
EXIT_RED = 2  # a red state/report was atomically written; still nonzero

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_FLOATING_IDENTIFIERS = frozenset({"current", "latest"})
_RANGE_MARKERS = "*^~<>= "

_PRE_STATE_KIND = "opencode_structured_output_eligibility_pre_state_v1"
_REPORT_KIND = "opencode_structured_output_eligibility_report_v1"
_RESTART_EVIDENCE_LEVEL = "operator_executed_unverified"


# --------------------------------------------------------------------------------------
# Canonical JSON + digest helpers (self-contained)
# --------------------------------------------------------------------------------------


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _canonical_equal(left: object, right: object) -> bool:
    return canonical_json_bytes(left) == canonical_json_bytes(right)


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class ProbeInputError(Exception):
    """Input / sink / fsync failure -> exit nonzero without claiming any report exists."""


class _OperationalError(Exception):
    """A transport/redirect/oversize/decode failure encountered while probing.

    These fail the current check closed (a red state/report is still atomically written);
    they never produce a spurious eligible claim.
    """


# --------------------------------------------------------------------------------------
# Closed evidence models
# --------------------------------------------------------------------------------------


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class OpenCodeStructuredOutputEligibilityPreStateV1(_Frozen):
    schema_version: Literal["1"] = "1"
    kind: Literal["opencode_structured_output_eligibility_pre_state_v1"] = _PRE_STATE_KIND
    status: Literal["message_roundtrip_red", "provider_canary_red", "awaiting_restart"]
    release_identity_digest: str
    endpoint_origin_digest: str
    data_root_identity_digest: str
    workspace_directory_digest: str
    auth_mode_digest: str
    provider: str
    model: str
    binary_sha256: str
    expected_server_version: str
    canary_schema_digest: str
    no_reply_session_id: str | None
    no_reply_caller_message_id: str
    prompt_status_code: int | None
    list_status_code: int | None
    single_status_code: int | None
    user_roundtrip_digest: str | None
    canary_session_id: str | None
    canary_caller_message_id: str | None
    canary_assistant_message_id: str | None
    candidate_digest: str | None
    request_timeout_seconds: float
    poll_interval_seconds: float
    poll_deadline_seconds: float
    max_response_bytes: int


class OpenCodeStructuredOutputEligibilityReportV1(_Frozen):
    schema_version: Literal["1"] = "1"
    kind: Literal["opencode_structured_output_eligibility_report_v1"] = _REPORT_KIND
    status: Literal["post_restart_red", "eligible"]
    restart_evidence_level: Literal["operator_executed_unverified"] = _RESTART_EVIDENCE_LEVEL
    release_identity_digest: str
    endpoint_origin_digest: str
    data_root_identity_digest: str
    workspace_directory_digest: str
    auth_mode_digest: str
    provider: str
    model: str
    binary_sha256: str
    expected_server_version: str
    canary_schema_digest: str
    user_roundtrip_digest: str | None
    candidate_digest: str | None
    canary_session_id: str | None
    canary_caller_message_id: str | None
    canary_assistant_message_id: str | None


# --------------------------------------------------------------------------------------
# Input validation
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _EndpointOrigin:
    scheme: str
    host: str
    port: int | None

    @property
    def base_url(self) -> str:
        netloc = f"[{self.host}]" if ":" in self.host else self.host
        if self.port is not None:
            netloc = f"{netloc}:{self.port}"
        return f"{self.scheme}://{netloc}"

    @property
    def digest(self) -> str:
        return canonical_digest({"scheme": self.scheme, "host": self.host, "port": self.port})


def _validate_endpoint(endpoint: str) -> _EndpointOrigin:
    parts = urlsplit(endpoint)
    if parts.scheme not in ("http", "https"):
        raise ProbeInputError(f"endpoint scheme must be http/https, got {parts.scheme!r}")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ProbeInputError("endpoint must not carry userinfo")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ProbeInputError("endpoint must be a bare origin (no path/query/fragment)")
    host = parts.hostname
    if host not in _LOOPBACK_HOSTS:
        raise ProbeInputError("endpoint host must be the loopback IP literal 127.0.0.1 or [::1]")
    try:
        port = parts.port
    except ValueError as exc:  # pragma: no cover - defensive
        raise ProbeInputError("endpoint port is invalid") from exc
    return _EndpointOrigin(scheme=parts.scheme, host=host, port=port)


def _validate_release_identity(
    *, tag: str, commit: str, asset: str, platform: str, architecture: str, version: str
) -> str:
    fields = {
        "release_tag": tag,
        "release_commit": commit,
        "release_asset": asset,
        "platform": platform,
        "architecture": architecture,
        "expected_server_version": version,
    }
    for name, value in fields.items():
        if not value or not value.strip():
            raise ProbeInputError(f"{name} must be present")
    if tag.strip().lower() in _FLOATING_IDENTIFIERS or version.strip().lower() in _FLOATING_IDENTIFIERS:
        raise ProbeInputError("floating release identifiers (current/latest) are rejected")
    if not _is_exact_tag(tag):
        raise ProbeInputError(f"release_tag must be an exact tag like v1.18.26, got {tag!r}")
    if not _is_exact_version(version):
        raise ProbeInputError(f"expected_server_version must be exact, got {version!r}")
    if not _is_full_commit(commit):
        raise ProbeInputError("release_commit must be a full 40-character hex commit")
    for name in ("release_asset", "platform", "architecture"):
        if any(ch.isspace() for ch in fields[name]):
            raise ProbeInputError(f"{name} must not contain whitespace")
    return canonical_digest(
        {
            "release_tag": tag,
            "release_commit": commit.lower(),
            "release_asset": asset,
            "platform": platform,
            "architecture": architecture,
        }
    )


def _is_exact_tag(tag: str) -> bool:
    if any(marker in tag for marker in _RANGE_MARKERS):
        return False
    body = tag[1:] if tag.startswith("v") else tag
    return _is_exact_version(body) if tag.startswith("v") else False


def _is_exact_version(value: str) -> bool:
    if any(marker in value for marker in _RANGE_MARKERS):
        return False
    segments = value.split(".")
    if len(segments) != 3:
        return False
    return all(segment.isdigit() for segment in segments)


def _is_full_commit(value: str) -> bool:
    return len(value) == 40 and all(ch in "0123456789abcdef" for ch in value.lower())


def _regular_file_digest(path_str: str, *, label: str) -> str:
    path = Path(path_str)
    if path.is_symlink():
        raise ProbeInputError(f"{label} must not be a symlink")
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ProbeInputError(f"{label} is not accessible: {exc.strerror or exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ProbeInputError(f"{label} must be a regular file")
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _assert_owner_only_dir(path: Path, *, label: str) -> os.stat_result:
    if path.is_symlink():
        raise ProbeInputError(f"{label} must not be a symlink")
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ProbeInputError(f"{label} is not accessible: {exc.strerror or exc}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ProbeInputError(f"{label} must be a directory")
    if info.st_uid != os.getuid():
        raise ProbeInputError(f"{label} must be owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ProbeInputError(f"{label} must be owner-only (no group/other permissions)")
    return info


def _assert_outside_worktrees(path: Path, *, label: str) -> None:
    resolved = path.resolve()
    for ancestor in (resolved, *resolved.parents):
        if (ancestor / ".git").exists():
            raise ProbeInputError(f"{label} must live outside every git worktree")


def _data_root_identity_digest(path_str: str) -> str:
    path = Path(path_str)
    info = _assert_owner_only_dir(path, label="--server-data-root")
    _assert_outside_worktrees(path, label="--server-data-root")
    projection = {
        "canonical_path": os.path.abspath(path_str),
        "st_dev": info.st_dev,
        "st_ino": info.st_ino,
        "st_uid": info.st_uid,
        "owner_permission_class": stat.S_IMODE(info.st_mode) & 0o700,
    }
    return canonical_digest(projection)


def _read_password(password_file: str) -> str:
    path = Path(password_file)
    if path.is_symlink():
        raise ProbeInputError("--password-file must not be a symlink")
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ProbeInputError("--password-file must be a regular file")
    if info.st_uid != os.getuid() or (stat.S_IMODE(info.st_mode) & 0o077):
        raise ProbeInputError("--password-file must be an owner-only regular file")
    if info.st_size > MAX_PASSWORD_BYTES:
        raise ProbeInputError("--password-file exceeds the maximum bounded size")
    raw = path.read_bytes()
    try:
        password = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProbeInputError("--password-file must be valid UTF-8") from exc
    if not password:
        raise ProbeInputError("--password-file must not be empty")
    if any(ch in password for ch in ("\x00", "\r", "\n")):
        raise ProbeInputError("--password-file must not contain NUL/CR/LF")
    return password


@dataclass(frozen=True)
class _AuthProfile:
    auth_mode: str  # "none" | "opencode_basic"
    username: str | None
    authorization_header: str | None

    @property
    def digest(self) -> str:
        return canonical_digest({"auth_mode": self.auth_mode, "username": self.username})


def _resolve_auth(auth_mode_cli: str, username: str | None, password_file: str | None) -> _AuthProfile:
    if auth_mode_cli == "none":
        if username or password_file:
            raise ProbeInputError("auth-mode none must not receive a username/password file")
        return _AuthProfile(auth_mode="none", username=None, authorization_header=None)
    if auth_mode_cli != "opencode-basic":
        raise ProbeInputError(f"unsupported auth-mode {auth_mode_cli!r}")
    if not username:
        raise ProbeInputError("opencode-basic requires --username")
    if any(ch in username for ch in (":", "\x00", "\r", "\n")):
        raise ProbeInputError("--username must not contain ':' or NUL/CR/LF")
    if not password_file:
        raise ProbeInputError("opencode-basic requires --password-file")
    password = _read_password(password_file)
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return _AuthProfile(auth_mode="opencode_basic", username=username, authorization_header=f"Basic {token}")


# --------------------------------------------------------------------------------------
# Atomic evidence sink
# --------------------------------------------------------------------------------------


def _atomic_write_json(path: Path, model: BaseModel) -> None:
    payload = json.dumps(model.model_dump(mode="json"), indent=2, sort_keys=True).encode("utf-8") + b"\n"
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        with _suppress_os_error():
            os.unlink(tmp)
        raise ProbeInputError(f"failed to atomically write evidence sink: {exc.strerror or exc}") from exc


class _suppress_os_error:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# HTTP layer (read-only reader; writer adds POST for the pre-restart phase only)
# --------------------------------------------------------------------------------------


class _OpenCodeReader:
    """Read-only OpenCode HTTP client. Exposes GET only -> zero POST-capable code paths."""

    def __init__(
        self,
        *,
        origin: _EndpointOrigin,
        directory: str,
        auth: _AuthProfile,
        request_timeout_seconds: float,
        max_response_bytes: int,
        transport: httpx.BaseTransport | None,
    ) -> None:
        self._directory = directory
        self._max_response_bytes = max_response_bytes
        headers = {"Accept": "application/json"}
        if auth.authorization_header is not None:
            headers["Authorization"] = auth.authorization_header
        self._client = httpx.Client(
            base_url=origin.base_url,
            timeout=httpx.Timeout(request_timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            headers=headers,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, *, scoped: bool) -> tuple[int, bytes]:
        params = {"directory": self._directory} if scoped else None
        return self._request("GET", path, params=params, json_body=None)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None,
        json_body: dict[str, Any] | None,
    ) -> tuple[int, bytes]:
        try:
            with self._client.stream(method, path, params=params, json=json_body) as response:
                if response.status_code in _REDIRECT_CODES:
                    raise _OperationalError("redirects are refused")
                total = 0
                chunks: list[bytes] = []
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > self._max_response_bytes:
                        raise _OperationalError("response exceeds max_response_bytes")
                    chunks.append(chunk)
                return response.status_code, b"".join(chunks)
        except httpx.HTTPError as exc:
            raise _OperationalError(f"transport error: {exc.__class__.__name__}") from exc


class _OpenCodeWriter(_OpenCodeReader):
    """Pre-restart client: adds session create + prompt_async POSTs."""

    def post(self, path: str, body: dict[str, Any], *, scoped: bool = True) -> tuple[int, bytes]:
        params = {"directory": self._directory} if scoped else None
        return self._request("POST", path, params=params, json_body=body)


# --------------------------------------------------------------------------------------
# Read outcomes / polling
# --------------------------------------------------------------------------------------


@dataclass
class _ReadOutcome:
    status_code: int | None
    withparts: dict[str, Any] | None
    found: bool
    terminal_red: bool


def _parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _OperationalError("response body was not valid JSON") from exc


def _poll(deadline: float, interval: float, once: "Any") -> _ReadOutcome:
    while True:
        outcome: _ReadOutcome = once()
        if outcome.found or outcome.terminal_red:
            return outcome
        if time.monotonic() >= deadline:
            return outcome
        if interval > 0:
            time.sleep(interval)


def _read_user_via_list(reader: _OpenCodeReader, sid: str, caller_id: str) -> _ReadOutcome:
    try:
        status, raw = reader.get(f"/session/{sid}/message", scoped=True)
        if status == 200:
            data = _parse_json(raw)
            if isinstance(data, list):
                for item in data:
                    info = item.get("info") if isinstance(item, dict) else None
                    if isinstance(info, dict) and info.get("id") == caller_id and info.get("role") == "user":
                        return _ReadOutcome(200, item, found=True, terminal_red=False)
            return _ReadOutcome(200, None, found=False, terminal_red=False)
        if status == 404:
            return _ReadOutcome(404, None, found=False, terminal_red=False)
        return _ReadOutcome(status, None, found=False, terminal_red=True)
    except _OperationalError:
        return _ReadOutcome(None, None, found=False, terminal_red=True)


def _read_message_via_single(reader: _OpenCodeReader, sid: str, message_id: str) -> _ReadOutcome:
    try:
        status, raw = reader.get(f"/session/{sid}/message/{message_id}", scoped=True)
        if status == 200:
            data = _parse_json(raw)
            if isinstance(data, dict) and isinstance(data.get("info"), dict):
                return _ReadOutcome(200, data, found=True, terminal_red=False)
            return _ReadOutcome(200, None, found=False, terminal_red=True)
        if status == 404:
            return _ReadOutcome(404, None, found=False, terminal_red=False)
        return _ReadOutcome(status, None, found=False, terminal_red=True)
    except _OperationalError:
        return _ReadOutcome(None, None, found=False, terminal_red=True)


# --------------------------------------------------------------------------------------
# Projections / validation of message content
# --------------------------------------------------------------------------------------


def _format_projection(fmt: object) -> dict[str, Any] | None:
    if not isinstance(fmt, dict):
        return None
    return {"type": fmt.get("type"), "schema": fmt.get("schema"), "retryCount": fmt.get("retryCount")}


def _valid_user_info(info: object, sid: str, caller_id: str) -> bool:
    if not isinstance(info, dict):
        return False
    if info.get("id") != caller_id or info.get("sessionID") != sid or info.get("role") != "user":
        return False
    fmt = info.get("format")
    if not isinstance(fmt, dict) or set(fmt.keys()) != {"type", "schema", "retryCount"}:
        return False
    return (
        fmt.get("type") == "json_schema"
        and _canonical_equal(fmt.get("schema"), FIXED_CANARY_SCHEMA)
        and fmt.get("retryCount") == EXPECTED_SERVER_RETRY_COUNT
    )


def _user_projection(info: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": info.get("id"),
        "sessionID": info.get("sessionID"),
        "role": info.get("role"),
        "format": _format_projection(info.get("format")),
    }


def _is_terminal_candidate(info: object, provider: str, model: str) -> bool:
    if not isinstance(info, dict):
        return False
    if info.get("role") != "assistant":
        return False
    if info.get("providerID") != provider or info.get("modelID") != model:
        return False
    if info.get("error") is not None:
        return False
    time_field = info.get("time")
    if not isinstance(time_field, dict) or time_field.get("completed") is None:
        return False
    return info.get("structured") is not None


def _valid_canary(structured: object) -> bool:
    if not _canonical_equal(structured, {"canary": CANARY_VALUE}):
        return False
    return len(canonical_json_bytes(structured)) <= MAX_CANDIDATE_BYTES


def _candidate_projection(
    *, sid: str, user_id: str, assistant_id: str, provider: str, model: str, assistant_info: dict[str, Any]
) -> dict[str, Any]:
    return {
        "session_id": sid,
        "user_message_id": user_id,
        "assistant_message_id": assistant_id,
        "provider": provider,
        "model": model,
        "role": "assistant",
        "assistant_format": _format_projection(assistant_info.get("format")),
        "structured": assistant_info.get("structured"),
        "completed_present": True,
    }


# --------------------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------------------


def _poll_health_version(reader: _OpenCodeReader, deadline: float, interval: float) -> str | None:
    while True:
        try:
            status, raw = reader.get("/global/health", scoped=False)
            if status == 200:
                data = _parse_json(raw)
                version = data.get("version") if isinstance(data, dict) else None
                if isinstance(version, str) and version:
                    return version
        except _OperationalError:
            pass
        if time.monotonic() >= deadline:
            return None
        if interval > 0:
            time.sleep(interval)


# --------------------------------------------------------------------------------------
# Shared bound / identity context
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _ProbeBounds:
    request_timeout_seconds: float
    poll_interval_seconds: float
    poll_deadline_seconds: float
    max_response_bytes: int


@dataclass(frozen=True)
class _IdentityContext:
    origin: _EndpointOrigin
    directory: str
    auth: _AuthProfile
    binary_sha256: str
    data_root_identity_digest: str
    workspace_directory_digest: str
    provider: str
    model: str
    expected_server_version: str
    release_identity_digest: str
    bounds: _ProbeBounds


def _workspace_directory_digest(directory: str) -> str:
    if not directory or any(ch in directory for ch in ("\x00", "\r", "\n")):
        raise ProbeInputError("--workspace-directory must be a non-empty single-line value")
    return canonical_digest({"directory_query": directory})


def _resolve_identity(
    namespace: argparse.Namespace,
    *,
    require_release: bool,
    provider: str | None = None,
    model: str | None = None,
) -> _IdentityContext:
    origin = _validate_endpoint(namespace.endpoint)
    auth = _resolve_auth(namespace.auth_mode, namespace.username, namespace.password_file)
    binary_sha256 = _regular_file_digest(namespace.server_binary, label="--server-binary")
    data_root_digest = _data_root_identity_digest(namespace.server_data_root)
    workspace_digest = _workspace_directory_digest(namespace.workspace_directory)
    provider = _require_token(
        provider if provider is not None else getattr(namespace, "provider", None), label="--provider"
    )
    model = _require_token(model if model is not None else getattr(namespace, "model", None), label="--model")
    expected_version = namespace.expected_server_version
    if not _is_exact_version(expected_version):
        raise ProbeInputError("expected_server_version must be exact")
    if require_release:
        release_digest = _validate_release_identity(
            tag=namespace.release_tag,
            commit=namespace.release_commit,
            asset=namespace.release_asset,
            platform=namespace.platform,
            architecture=namespace.architecture,
            version=expected_version,
        )
    else:
        release_digest = ""
    bounds = _ProbeBounds(
        request_timeout_seconds=float(namespace.request_timeout_seconds),
        poll_interval_seconds=float(namespace.poll_interval_seconds),
        poll_deadline_seconds=float(namespace.poll_deadline_seconds),
        max_response_bytes=int(namespace.max_response_bytes),
    )
    return _IdentityContext(
        origin=origin,
        directory=namespace.workspace_directory,
        auth=auth,
        binary_sha256=binary_sha256,
        data_root_identity_digest=data_root_digest,
        workspace_directory_digest=workspace_digest,
        provider=provider,
        model=model,
        expected_server_version=expected_version,
        release_identity_digest=release_digest,
        bounds=bounds,
    )


def _require_token(value: str | None, *, label: str) -> str:
    if not value or not value.strip() or any(ch.isspace() for ch in value):
        raise ProbeInputError(f"{label} must be a non-empty whitespace-free token")
    return value


# --------------------------------------------------------------------------------------
# pre-restart phase
# --------------------------------------------------------------------------------------


@dataclass
class _CanaryResult:
    status: Literal["provider_canary_red", "awaiting_restart"]
    session_id: str | None = None
    assistant_id: str | None = None
    user_roundtrip_digest: str | None = None
    candidate_digest: str | None = None


def _run_pre_restart(namespace: argparse.Namespace, transport: httpx.BaseTransport | None) -> int:
    state_path = Path(namespace.state)
    _prepare_pre_evidence_dir(state_path)
    identity = _resolve_identity(namespace, require_release=True)

    writer = _OpenCodeWriter(
        origin=identity.origin,
        directory=identity.directory,
        auth=identity.auth,
        request_timeout_seconds=identity.bounds.request_timeout_seconds,
        max_response_bytes=identity.bounds.max_response_bytes,
        transport=transport,
    )
    try:
        state = _drive_pre_restart(writer, identity)
    finally:
        writer.close()

    _atomic_write_json(state_path, state)
    return EXIT_OK if state.status == "awaiting_restart" else EXIT_RED


def _drive_pre_restart(
    writer: _OpenCodeWriter, identity: _IdentityContext
) -> OpenCodeStructuredOutputEligibilityPreStateV1:
    deadline = time.monotonic() + identity.bounds.poll_deadline_seconds
    interval = identity.bounds.poll_interval_seconds

    no_reply_sid: str | None = None
    prompt_status: int | None = None
    list_status: int | None = None
    single_status: int | None = None
    roundtrip_ok = False

    try:
        version = _poll_health_version(writer, deadline, interval)
        if version == identity.expected_server_version:
            no_reply_sid = _create_session(writer)
            prompt_status = _send_prompt(writer, no_reply_sid, _no_reply_prompt_body())
            if prompt_status == 204:
                list_outcome = _poll(
                    deadline,
                    interval,
                    lambda: _read_user_via_list(writer, _s(no_reply_sid), CALLER_NO_REPLY_MESSAGE_ID),
                )
                single_outcome = _poll(
                    deadline,
                    interval,
                    lambda: _read_message_via_single(writer, _s(no_reply_sid), CALLER_NO_REPLY_MESSAGE_ID),
                )
                list_status = list_outcome.status_code
                single_status = single_outcome.status_code
                roundtrip_ok = (
                    list_outcome.found
                    and single_outcome.found
                    and list_outcome.withparts is not None
                    and single_outcome.withparts is not None
                    and _valid_user_info(
                        list_outcome.withparts.get("info"), no_reply_sid, CALLER_NO_REPLY_MESSAGE_ID
                    )
                    and _valid_user_info(
                        single_outcome.withparts.get("info"), no_reply_sid, CALLER_NO_REPLY_MESSAGE_ID
                    )
                )
    except _OperationalError:
        roundtrip_ok = False

    if not roundtrip_ok:
        return _pre_state(
            identity,
            status="message_roundtrip_red",
            no_reply_sid=no_reply_sid,
            prompt_status=prompt_status,
            list_status=list_status,
            single_status=single_status,
            canary=None,
        )

    try:
        canary = _run_canary(writer, identity, deadline, interval)
    except _OperationalError:
        canary = _CanaryResult(status="provider_canary_red")

    return _pre_state(
        identity,
        status=canary.status,
        no_reply_sid=no_reply_sid,
        prompt_status=prompt_status,
        list_status=list_status,
        single_status=single_status,
        canary=canary,
    )


def _run_canary(
    writer: _OpenCodeWriter, identity: _IdentityContext, deadline: float, interval: float
) -> _CanaryResult:
    sid = _create_session(writer)
    prompt_status = _send_prompt(writer, sid, _canary_prompt_body(identity.provider, identity.model))
    if prompt_status != 204:
        return _CanaryResult(status="provider_canary_red", session_id=sid)

    outcome = _poll(
        deadline,
        interval,
        lambda: _read_terminal_candidate(writer, sid, identity.provider, identity.model),
    )
    if not outcome.found or outcome.withparts is None:
        return _CanaryResult(status="provider_canary_red", session_id=sid)

    candidate_info = outcome.withparts["info"]
    structured = candidate_info.get("structured")
    if not _valid_canary(structured):
        return _CanaryResult(status="provider_canary_red", session_id=sid)

    user_digest = _canary_user_roundtrip_digest(writer, sid, deadline, interval)
    if user_digest is None:
        return _CanaryResult(status="provider_canary_red", session_id=sid)

    assistant_id = candidate_info.get("id")
    if not isinstance(assistant_id, str) or not assistant_id:
        return _CanaryResult(status="provider_canary_red", session_id=sid)

    single = _read_message_via_single(writer, sid, assistant_id)
    if not single.found or single.withparts is None:
        return _CanaryResult(status="provider_canary_red", session_id=sid)
    single_info = single.withparts["info"]
    if not _is_terminal_candidate(single_info, identity.provider, identity.model) or not _valid_canary(
        single_info.get("structured")
    ):
        return _CanaryResult(status="provider_canary_red", session_id=sid)

    list_projection = _candidate_projection(
        sid=sid,
        user_id=CALLER_CANARY_MESSAGE_ID,
        assistant_id=assistant_id,
        provider=identity.provider,
        model=identity.model,
        assistant_info=candidate_info,
    )
    single_projection = _candidate_projection(
        sid=sid,
        user_id=CALLER_CANARY_MESSAGE_ID,
        assistant_id=assistant_id,
        provider=identity.provider,
        model=identity.model,
        assistant_info=single_info,
    )
    if not _canonical_equal(list_projection, single_projection):
        return _CanaryResult(status="provider_canary_red", session_id=sid)

    return _CanaryResult(
        status="awaiting_restart",
        session_id=sid,
        assistant_id=assistant_id,
        user_roundtrip_digest=user_digest,
        candidate_digest=canonical_digest(single_projection),
    )


def _canary_user_roundtrip_digest(
    reader: _OpenCodeReader, sid: str, deadline: float, interval: float
) -> str | None:
    outcome = _poll(deadline, interval, lambda: _read_user_via_list(reader, sid, CALLER_CANARY_MESSAGE_ID))
    if not outcome.found or outcome.withparts is None:
        return None
    info = outcome.withparts.get("info")
    if not _valid_user_info(info, sid, CALLER_CANARY_MESSAGE_ID):
        return None
    assert isinstance(info, dict)
    return canonical_digest(_user_projection(info))


def _read_terminal_candidate(reader: _OpenCodeReader, sid: str, provider: str, model: str) -> _ReadOutcome:
    try:
        status, raw = reader.get(f"/session/{sid}/message", scoped=True)
    except _OperationalError:
        return _ReadOutcome(None, None, found=False, terminal_red=True)
    if status == 404:
        return _ReadOutcome(404, None, found=False, terminal_red=False)
    if status != 200:
        return _ReadOutcome(status, None, found=False, terminal_red=True)
    data = _parse_json(raw)
    if not isinstance(data, list):
        return _ReadOutcome(200, None, found=False, terminal_red=True)
    candidates: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        info = item.get("info")
        if not isinstance(info, dict) or info.get("parentID") != CALLER_CANARY_MESSAGE_ID:
            continue
        if _is_terminal_candidate(info, provider, model):
            candidates.append(item)
    if len(candidates) == 1:
        return _ReadOutcome(200, candidates[0], found=True, terminal_red=False)
    if len(candidates) > 1:
        return _ReadOutcome(200, None, found=False, terminal_red=True)
    return _ReadOutcome(200, None, found=False, terminal_red=False)


def _create_session(writer: _OpenCodeWriter) -> str:
    status, raw = writer.post("/session", {})
    if status != 200:
        raise _OperationalError(f"session create returned {status}")
    data = _parse_json(raw)
    session_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(session_id, str) or not session_id:
        raise _OperationalError("session create response is missing an id")
    return session_id


def _send_prompt(writer: _OpenCodeWriter, sid: str, body: dict[str, Any]) -> int:
    status, _ = writer.post(f"/session/{sid}/prompt_async", body)
    return status


def _no_reply_prompt_body() -> dict[str, Any]:
    return {
        "messageID": CALLER_NO_REPLY_MESSAGE_ID,
        "parts": [{"type": "text", "text": FIXED_NO_REPLY_INSTRUCTION}],
        "noReply": True,
        "format": {"type": "json_schema", "schema": FIXED_CANARY_SCHEMA},
    }


def _canary_prompt_body(provider: str, model: str) -> dict[str, Any]:
    return {
        "messageID": CALLER_CANARY_MESSAGE_ID,
        "model": {"providerID": provider, "modelID": model},
        "parts": [{"type": "text", "text": FIXED_CANARY_INSTRUCTION}],
        "format": {"type": "json_schema", "schema": FIXED_CANARY_SCHEMA},
    }


def _pre_state(
    identity: _IdentityContext,
    *,
    status: Literal["message_roundtrip_red", "provider_canary_red", "awaiting_restart"],
    no_reply_sid: str | None,
    prompt_status: int | None,
    list_status: int | None,
    single_status: int | None,
    canary: _CanaryResult | None,
) -> OpenCodeStructuredOutputEligibilityPreStateV1:
    return OpenCodeStructuredOutputEligibilityPreStateV1(
        status=status,
        release_identity_digest=identity.release_identity_digest,
        endpoint_origin_digest=identity.origin.digest,
        data_root_identity_digest=identity.data_root_identity_digest,
        workspace_directory_digest=identity.workspace_directory_digest,
        auth_mode_digest=identity.auth.digest,
        provider=identity.provider,
        model=identity.model,
        binary_sha256=identity.binary_sha256,
        expected_server_version=identity.expected_server_version,
        canary_schema_digest=canonical_digest(FIXED_CANARY_SCHEMA),
        no_reply_session_id=no_reply_sid,
        no_reply_caller_message_id=CALLER_NO_REPLY_MESSAGE_ID,
        prompt_status_code=prompt_status,
        list_status_code=list_status,
        single_status_code=single_status,
        user_roundtrip_digest=None if canary is None else canary.user_roundtrip_digest,
        canary_session_id=None if canary is None else canary.session_id,
        canary_caller_message_id=(
            CALLER_CANARY_MESSAGE_ID if canary is not None and canary.status == "awaiting_restart" else None
        ),
        canary_assistant_message_id=None if canary is None else canary.assistant_id,
        candidate_digest=None if canary is None else canary.candidate_digest,
        request_timeout_seconds=identity.bounds.request_timeout_seconds,
        poll_interval_seconds=identity.bounds.poll_interval_seconds,
        poll_deadline_seconds=identity.bounds.poll_deadline_seconds,
        max_response_bytes=identity.bounds.max_response_bytes,
    )


def _prepare_pre_evidence_dir(state_path: Path) -> None:
    if not state_path.is_absolute():
        raise ProbeInputError("--state must be an absolute path")
    evidence_dir = state_path.parent
    _assert_owner_only_dir(evidence_dir, label="evidence directory")
    _assert_outside_worktrees(evidence_dir, label="evidence directory")
    if list(os.listdir(evidence_dir)):
        raise ProbeInputError("evidence directory must be empty before pre-restart")
    if state_path.exists():
        raise ProbeInputError("pre-state output file already exists (reuse is refused)")


def _s(value: str | None) -> str:
    if value is None:  # pragma: no cover - guarded by call sites
        raise _OperationalError("missing session id")
    return value


# --------------------------------------------------------------------------------------
# post-restart phase
# --------------------------------------------------------------------------------------


def _run_post_restart(namespace: argparse.Namespace, transport: httpx.BaseTransport | None) -> int:
    state_path = Path(namespace.state)
    output_path = Path(namespace.output)
    pre_state = _load_pre_state(state_path)
    if pre_state.status != "awaiting_restart":
        raise ProbeInputError(
            f"post-restart requires an awaiting_restart pre-state, got {pre_state.status!r}"
        )
    _prepare_post_evidence_dir(state_path=state_path, output_path=output_path)

    identity = _resolve_identity(
        namespace, require_release=False, provider=pre_state.provider, model=pre_state.model
    )

    reader = _OpenCodeReader(
        origin=identity.origin,
        directory=identity.directory,
        auth=identity.auth,
        request_timeout_seconds=identity.bounds.request_timeout_seconds,
        max_response_bytes=identity.bounds.max_response_bytes,
        transport=transport,
    )
    try:
        eligible = _drive_post_restart(reader, identity, pre_state)
    finally:
        reader.close()

    report = _report(identity, pre_state, status="eligible" if eligible else "post_restart_red")
    _atomic_write_json(output_path, report)
    return EXIT_OK if eligible else EXIT_RED


def _drive_post_restart(
    reader: _OpenCodeReader,
    identity: _IdentityContext,
    pre_state: OpenCodeStructuredOutputEligibilityPreStateV1,
) -> bool:
    if identity.origin.digest != pre_state.endpoint_origin_digest:
        return False
    if identity.data_root_identity_digest != pre_state.data_root_identity_digest:
        return False
    if identity.workspace_directory_digest != pre_state.workspace_directory_digest:
        return False
    if identity.auth.digest != pre_state.auth_mode_digest:
        return False
    if identity.binary_sha256 != pre_state.binary_sha256:
        return False
    if identity.expected_server_version != pre_state.expected_server_version:
        return False

    sid = pre_state.canary_session_id
    assistant_id = pre_state.canary_assistant_message_id
    if sid is None or assistant_id is None:
        return False

    deadline = time.monotonic() + identity.bounds.poll_deadline_seconds
    interval = identity.bounds.poll_interval_seconds

    try:
        version = _poll_health_version(reader, deadline, interval)
        if version != identity.expected_server_version:
            return False

        user_list = _poll(
            deadline, interval, lambda: _read_user_via_list(reader, sid, CALLER_CANARY_MESSAGE_ID)
        )
        user_single = _poll(
            deadline, interval, lambda: _read_message_via_single(reader, sid, CALLER_CANARY_MESSAGE_ID)
        )
        if not (user_list.found and user_single.found):
            return False
        if user_list.withparts is None or user_single.withparts is None:
            return False
        list_info = user_list.withparts.get("info")
        single_info = user_single.withparts.get("info")
        if not _valid_user_info(list_info, sid, CALLER_CANARY_MESSAGE_ID):
            return False
        if not _valid_user_info(single_info, sid, CALLER_CANARY_MESSAGE_ID):
            return False
        assert isinstance(list_info, dict) and isinstance(single_info, dict)
        if not _canonical_equal(_user_projection(list_info), _user_projection(single_info)):
            return False
        if canonical_digest(_user_projection(single_info)) != pre_state.user_roundtrip_digest:
            return False

        assistant = _poll(
            deadline,
            interval,
            lambda: _read_terminal_candidate(reader, sid, identity.provider, identity.model),
        )
        assistant_single = _read_message_via_single(reader, sid, assistant_id)
    except _OperationalError:
        return False

    if not assistant.found or assistant.withparts is None:
        return False
    if not assistant_single.found or assistant_single.withparts is None:
        return False
    list_candidate_info = assistant.withparts["info"]
    single_candidate_info = assistant_single.withparts["info"]
    if list_candidate_info.get("id") != assistant_id or single_candidate_info.get("id") != assistant_id:
        return False
    if not _is_terminal_candidate(single_candidate_info, identity.provider, identity.model):
        return False
    if not _valid_canary(single_candidate_info.get("structured")):
        return False

    projection = _candidate_projection(
        sid=sid,
        user_id=CALLER_CANARY_MESSAGE_ID,
        assistant_id=assistant_id,
        provider=identity.provider,
        model=identity.model,
        assistant_info=single_candidate_info,
    )
    list_projection = _candidate_projection(
        sid=sid,
        user_id=CALLER_CANARY_MESSAGE_ID,
        assistant_id=assistant_id,
        provider=identity.provider,
        model=identity.model,
        assistant_info=list_candidate_info,
    )
    if not _canonical_equal(projection, list_projection):
        return False
    return canonical_digest(projection) == pre_state.candidate_digest


def _report(
    identity: _IdentityContext,
    pre_state: OpenCodeStructuredOutputEligibilityPreStateV1,
    *,
    status: Literal["post_restart_red", "eligible"],
) -> OpenCodeStructuredOutputEligibilityReportV1:
    return OpenCodeStructuredOutputEligibilityReportV1(
        status=status,
        release_identity_digest=pre_state.release_identity_digest,
        endpoint_origin_digest=pre_state.endpoint_origin_digest,
        data_root_identity_digest=pre_state.data_root_identity_digest,
        workspace_directory_digest=pre_state.workspace_directory_digest,
        auth_mode_digest=pre_state.auth_mode_digest,
        provider=pre_state.provider,
        model=pre_state.model,
        binary_sha256=pre_state.binary_sha256,
        expected_server_version=pre_state.expected_server_version,
        canary_schema_digest=pre_state.canary_schema_digest,
        user_roundtrip_digest=pre_state.user_roundtrip_digest,
        candidate_digest=pre_state.candidate_digest,
        canary_session_id=pre_state.canary_session_id,
        canary_caller_message_id=pre_state.canary_caller_message_id,
        canary_assistant_message_id=pre_state.canary_assistant_message_id,
    )


def _load_pre_state(state_path: Path) -> OpenCodeStructuredOutputEligibilityPreStateV1:
    if not state_path.is_file():
        raise ProbeInputError("pre-state file does not exist")
    try:
        document = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProbeInputError(f"pre-state file is not readable JSON: {exc}") from exc
    try:
        return OpenCodeStructuredOutputEligibilityPreStateV1.model_validate(document)
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError surfaced as input error
        raise ProbeInputError(f"pre-state file is not a valid PreStateV1: {exc}") from exc


def _prepare_post_evidence_dir(*, state_path: Path, output_path: Path) -> None:
    if not state_path.is_absolute() or not output_path.is_absolute():
        raise ProbeInputError("--state and --output must be absolute paths")
    evidence_dir = output_path.parent
    if state_path.parent != evidence_dir:
        raise ProbeInputError("--state and --output must live in the same evidence directory")
    _assert_owner_only_dir(evidence_dir, label="evidence directory")
    _assert_outside_worktrees(evidence_dir, label="evidence directory")
    entries = set(os.listdir(evidence_dir))
    if entries != {state_path.name}:
        raise ProbeInputError("evidence directory must contain only the named pre-state before post-restart")
    if output_path.exists():
        raise ProbeInputError("post-restart output file already exists (reuse is refused)")


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _add_common_arguments(parser: argparse.ArgumentParser, *, include_release: bool) -> None:
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--auth-mode", required=True, choices=["none", "opencode-basic"])
    parser.add_argument("--username", default=None)
    parser.add_argument("--password-file", default=None)
    parser.add_argument("--server-data-root", required=True)
    parser.add_argument("--workspace-directory", required=True)
    parser.add_argument("--server-binary", required=True)
    parser.add_argument("--expected-server-version", required=True)
    parser.add_argument("--provider", required=include_release)
    parser.add_argument("--model", required=include_release)
    if include_release:
        parser.add_argument("--release-tag", required=True)
        parser.add_argument("--release-commit", required=True)
        parser.add_argument("--release-asset", required=True)
        parser.add_argument("--platform", required=True)
        parser.add_argument("--architecture", required=True)
    parser.add_argument("--request-timeout-seconds", type=float, default=DEFAULT_REQUEST_TIMEOUT_SECONDS)
    parser.add_argument("--poll-interval-seconds", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    parser.add_argument("--poll-deadline-seconds", type=float, default=DEFAULT_POLL_DEADLINE_SECONDS)
    parser.add_argument("--max-response-bytes", type=int, default=DEFAULT_MAX_RESPONSE_BYTES)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opencode_structured_output_eligibility_probe",
        description="Standalone two-phase OpenCode structured-output eligibility probe (S0 Task 0).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    pre = subparsers.add_parser("pre-restart", help="Probe the exact release before the operator restart.")
    _add_common_arguments(pre, include_release=True)
    pre.add_argument("--state", required=True)

    post = subparsers.add_parser("post-restart", help="Re-read recorded evidence after the operator restart.")
    _add_common_arguments(post, include_release=False)
    # provider/model remain declared for the post identity/candidate re-read.
    post.add_argument("--state", required=True)
    post.add_argument("--output", required=True)
    return parser


def run_cli(argv: list[str], *, transport: httpx.BaseTransport | None = None) -> int:
    parser = build_parser()
    try:
        namespace = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_ERROR
    try:
        if namespace.command == "pre-restart":
            return _run_pre_restart(namespace, transport)
        return _run_post_restart(namespace, transport)
    except ProbeInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    return run_cli(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
