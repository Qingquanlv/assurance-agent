"""Human decision carried by the apply gate."""

from __future__ import annotations

from typing import Literal

from graph_engine.plugin_api import FrozenModel


class ApplyHumanDecision(FrozenModel):
    action: Literal["approve", "reject", "request_rework", "supersede"]


__all__ = ["ApplyHumanDecision"]
