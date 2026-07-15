"""Coverage/performance sub-config extracted from AaConfig (extra='allow').

AaConfig (M1) does not declare coverage/performance keys but preserves them in
model_extra because of extra='allow'; these loaders read them with defaults so
run_change never crashes on a minimal config.
"""
from typing import Any, Literal

from pydantic import BaseModel

from assurance_agent.artifacts.models import CoverageThreshold
from assurance_agent.config import AaConfig


class CoverageConfig(BaseModel):
    enabled: bool = True
    gate_mode: Literal["warn", "block"] = "warn"
    target_package: str = "app"
    threshold: CoverageThreshold


class PerfConfig(BaseModel):
    enabled: bool = True
    base_url: str = "http://localhost:8000"
    default_load: dict[str, Any] = {"users": 10, "spawn_rate": 2, "run_time_s": 30}


def _section(config: AaConfig, key: str) -> dict[str, Any]:
    value = getattr(config, key, None)
    return value if isinstance(value, dict) else {}


def load_coverage_config(config: AaConfig) -> CoverageConfig:
    raw = _section(config, "coverage")
    threshold_raw = raw.get("threshold")
    thr: dict[str, Any] = threshold_raw if isinstance(threshold_raw, dict) else {}
    gate_mode = raw.get("gate_mode", "warn")
    return CoverageConfig(
        enabled=bool(raw.get("enabled", True)),
        gate_mode="block" if gate_mode == "block" else "warn",
        target_package=str(raw.get("target_package", "app")),
        threshold=CoverageThreshold(
            line=float(thr.get("line", 70)),
            branch=float(thr.get("branch", 60)),
            module_line=thr.get("module_line"),
            diff_line=thr.get("diff_line"),
        ),
    )


def load_perf_config(config: AaConfig) -> PerfConfig:
    raw = _section(config, "performance")
    load_raw = raw.get("default_load")
    load: dict[str, Any] = load_raw if isinstance(load_raw, dict) else {}
    return PerfConfig(
        enabled=bool(raw.get("enabled", True)),
        base_url=str(raw.get("base_url", "http://localhost:8000")),
        default_load={
            "users": int(load.get("users", 10)),
            "spawn_rate": int(load.get("spawn_rate", 2)),
            "run_time_s": int(load.get("run_time_s", 30)),
        },
    )
