"""Identifiers that are safe to use as one filesystem path segment."""

import re

from assurance_agent.exceptions import AaError

_CHANGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class UnsafeIdentifierError(AaError):
    pass


def assert_path_segment_safe(value: str, *, label: str = "identifier") -> None:
    if not _CHANGE_ID_RE.fullmatch(value):
        raise UnsafeIdentifierError(f"unsafe {label}: {value!r}")


def assert_change_id_safe(change_id: str) -> None:
    assert_path_segment_safe(change_id, label="change id")
