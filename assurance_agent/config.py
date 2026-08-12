"""Load and validate the target project's .aa/config.yaml."""

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    ValidationError,
    field_validator,
)

from assurance_agent.exceptions import AaError

CONFIG_RELPATH = ".aa/config.yaml"
MODEL_ROUTING_FILE_ENV = "AA_MODEL_ROUTING_FILE"
EscalationErrorKind = Literal["invalid_output", "forbidden_write"]


class ConfigNotFoundError(AaError):
    pass


class ConfigInvalidError(AaError):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class _StrictModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _validated_model_id(value: str) -> str:
    normalized = value.strip()
    provider, separator, model = normalized.partition("/")
    if separator != "/" or not provider or not model:
        raise ValueError("model must be a non-empty provider/model identifier")
    return normalized


class ModelEscalationCfg(_StrictModel):
    model: StrictStr
    on_error_kinds: tuple[EscalationErrorKind, ...]

    _validate_model = field_validator("model")(_validated_model_id)


class ModelRoutingCfg(_StrictModel):
    default: StrictStr | None = None
    strict_routes: StrictBool = False
    routes: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    escalation: ModelEscalationCfg | None = None

    @field_validator("default")
    @classmethod
    def validate_default(cls, value: str | None) -> str | None:
        return None if value is None else _validated_model_id(value)

    @field_validator("routes")
    @classmethod
    def validate_routes(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for skill, model in value.items():
            if re.fullmatch(r"aa-[a-z0-9]+(?:-[a-z0-9]+)*", skill) is None:
                raise ValueError("routes keys must be exact skill names matching aa-*")
            normalized[skill] = _validated_model_id(model)
        return normalized


class _NoDuplicateKeySafeLoader(yaml.SafeLoader):
    pass


def _mapping_constructor(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict:
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in mapping:
            raise yaml.YAMLError(f"duplicate key {key!r}")
        mapping[key] = loader.construct_object(value_node)
    return mapping


_NoDuplicateKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _mapping_constructor,
)


def _safe_load_no_duplicates(text: str) -> object:
    return yaml.load(text, Loader=_NoDuplicateKeySafeLoader)


def _load_model_routing_override(root: Path) -> ModelRoutingCfg | None:
    raw_configured = os.environ.get(MODEL_ROUTING_FILE_ENV)
    if raw_configured is None:
        return None
    configured = raw_configured.strip()
    if not configured:
        raise ConfigInvalidError(f"{MODEL_ROUTING_FILE_ENV} must not be empty")
    path = Path(configured)
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        raise ConfigInvalidError(f"{MODEL_ROUTING_FILE_ENV} file not found: {configured}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as err:
        raise ConfigInvalidError(f"{MODEL_ROUTING_FILE_ENV} read error: {err}") from err
    try:
        raw = _safe_load_no_duplicates(text)
    except yaml.YAMLError as err:
        raise ConfigInvalidError(f"{MODEL_ROUTING_FILE_ENV} parse error: {err}") from err
    try:
        return ModelRoutingCfg.model_validate(raw)
    except ValidationError as err:
        first = err.errors()[0]
        loc = ".".join(str(part) for part in first["loc"])
        raise ConfigInvalidError(f"{MODEL_ROUTING_FILE_ENV} schema invalid: {loc}: {first['msg']}") from err


class SourcesCfg(_Model):
    frontend: str
    backend: str


class QaCfg(_Model):
    cases: str
    changes: str
    archive: str = "./qa/archive"


class TestsCfg(_Model):
    root: str
    api: str
    e2e: str
    fixtures: str = "./tests/fixtures"
    helpers: str = "./tests/helpers"
    reports: str = "./tests/reports"


class FrameworkCfg(_Model):
    enabled: bool
    name: str


class FrameworksCfg(_Model):
    api: FrameworkCfg
    e2e: FrameworkCfg


class E2eGenerationCfg(_Model):
    default_pom: bool


class GenerationCfg(_Model):
    prd_input_mode: str
    e2e: E2eGenerationCfg


class SelfHealingCfg(_Model):
    mode: str


class ExecutionCfg(_Model):
    entry: str
    self_healing: SelfHealingCfg
    model_routing: ModelRoutingCfg | None = None


class AaConfig(_Model):
    version: int
    sources: SourcesCfg
    qa: QaCfg
    tests: TestsCfg
    frameworks: FrameworksCfg
    generation: GenerationCfg
    execution: ExecutionCfg


def load_config(root: Path) -> AaConfig:
    path = root / CONFIG_RELPATH
    if not path.is_file():
        raise ConfigNotFoundError(f"{CONFIG_RELPATH} not found. Run `aa init` first.")
    try:
        raw = _safe_load_no_duplicates(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as err:
        raise ConfigInvalidError(f"{CONFIG_RELPATH} parse error: {err}") from err
    try:
        config = AaConfig.model_validate(raw)
    except ValidationError as err:
        first = err.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise ConfigInvalidError(f"{CONFIG_RELPATH} schema invalid: {loc}: {first['msg']}") from err
    override = _load_model_routing_override(root)
    if override is None:
        return config
    execution = config.execution.model_copy(update={"model_routing": override})
    return config.model_copy(update={"execution": execution})
