"""Compatibility facade over ``assurance_agent.evidence.issue_replay`` projectors."""

from __future__ import annotations

from assurance_agent.evidence.issue_replay import (
    ProjectionError,
    dump_projection,
    project_change_issues,
    project_problems,
    project_review_queue,
)

__all__ = [
    "ProjectionError",
    "dump_projection",
    "project_change_issues",
    "project_problems",
    "project_review_queue",
]
