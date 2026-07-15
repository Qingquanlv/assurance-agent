"""Load and validate the target project's .aa/config.yaml."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from assurance_agent.exceptions import AaError

CONFIG_RELPATH = ".aa/config.yaml"


class ConfigNotFoundError(AaError):
    pass


class ConfigInvalidError(AaError):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


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
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as err:
        raise ConfigInvalidError(f"{CONFIG_RELPATH} parse error: {err}") from err
    try:
        return AaConfig.model_validate(raw)
    except ValidationError as err:
        first = err.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise ConfigInvalidError(f"{CONFIG_RELPATH} schema invalid: {loc}: {first['msg']}") from err
