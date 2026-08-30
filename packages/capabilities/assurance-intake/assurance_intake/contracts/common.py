"""Shared intake contract primitives."""

from __future__ import annotations

from typing import Annotated, Literal, get_args

from pydantic import Field

NonEmptyStr = Annotated[str, Field(min_length=1)]
CaseId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_]+$")]
RiskTier = Literal["low", "medium", "high", "critical"]
RISK_TIER_ORDER: tuple[RiskTier, ...] = get_args(RiskTier)
