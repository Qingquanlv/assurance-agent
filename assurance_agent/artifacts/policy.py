"""加载并摘要组织策略：缺项目文件时回落到打包默认值。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from assurance_agent import resources
from assurance_agent.artifacts.models.policy import Policy
from assurance_agent.exceptions import AaError

POLICY_REL_PATH = ".aa/policy.yaml"
_DEFAULT_RESOURCE = ("schemas", "policy-default.yaml")


class PolicyError(AaError):
    pass


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


def load_policy(project_root: Path) -> Policy:
    path = project_root / POLICY_REL_PATH
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as err:
            raise PolicyError(f"cannot read {path}: {err}") from err
        return _parse(text, str(path))
    return _parse(resources.read_text(*_DEFAULT_RESOURCE), "packaged policy-default.yaml")


def policy_digest(policy: Policy) -> str:
    payload = json.dumps(
        policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
