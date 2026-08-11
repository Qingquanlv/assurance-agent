"""加载并摘要组织策略：缺项目文件时回落到打包默认值。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ValidationError

from assurance_agent import resources
from assurance_agent.artifacts.models.policy import Policy
from assurance_agent.exceptions import AaError

POLICY_REL_PATH = ".aa/policy.yaml"
_DEFAULT_RESOURCE = ("schemas", "policy-default.yaml")

PolicyOrigin = Literal["project", "packaged_default"]


class PolicyError(AaError):
    pass


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    policy: Policy
    origin: PolicyOrigin
    canonical_bytes: bytes
    digest: str


def normalized_policy_bytes(policy: Policy) -> bytes:
    payload = json.dumps(
        policy.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return (payload + "\n").encode("utf-8")


def policy_digest(policy: Policy) -> str:
    return hashlib.sha256(normalized_policy_bytes(policy)).hexdigest()


def _parse(text: str, origin: str) -> Policy:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise PolicyError(f"{origin} is not valid YAML: {err}") from err
    if not isinstance(raw, dict):
        raise PolicyError(f"{origin} must be a YAML mapping")
    try:
        return Policy.model_validate(raw)
    except ValidationError as err:
        raise PolicyError(f"{origin} is not a valid policy: {err}") from err


def _snapshot_from_policy(policy: Policy, origin: PolicyOrigin) -> PolicySnapshot:
    canonical = normalized_policy_bytes(policy)
    return PolicySnapshot(
        policy=policy,
        origin=origin,
        canonical_bytes=canonical,
        digest=hashlib.sha256(canonical).hexdigest(),
    )


def _error_origin(origin: PolicyOrigin) -> str:
    if origin == "packaged_default":
        return "packaged policy-default.yaml"
    return POLICY_REL_PATH


def load_policy(project_root: Path) -> Policy:
    path = project_root / POLICY_REL_PATH
    if path.exists():
        try:
            data = path.read_bytes()
        except OSError as err:
            raise PolicyError(f"cannot read {path}: {err}") from err
        return load_policy_bytes(data, origin=str(path))
    return load_policy_bytes(None, origin=str(path))


def load_policy_bytes(data: bytes | None, *, origin: str) -> Policy:
    """Parse captured policy bytes; absence selects the packaged default."""
    if data is None:
        return _parse(resources.read_text(*_DEFAULT_RESOURCE), "packaged policy-default.yaml")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as err:
        raise PolicyError(f"{origin} is not valid UTF-8: {err}") from err
    return _parse(text, origin)


def load_policy_snapshot(project_root: Path) -> PolicySnapshot:
    path = project_root / POLICY_REL_PATH
    if path.exists():
        try:
            data = path.read_bytes()
        except OSError as err:
            raise PolicyError(f"cannot read {path}: {err}") from err
        return load_policy_snapshot_bytes(data, origin="project")
    return load_policy_snapshot_bytes(None, origin="packaged_default")


def load_policy_snapshot_bytes(data: bytes | None, *, origin: PolicyOrigin) -> PolicySnapshot:
    policy = load_policy_bytes(data, origin=_error_origin(origin))
    return _snapshot_from_policy(policy, origin)
