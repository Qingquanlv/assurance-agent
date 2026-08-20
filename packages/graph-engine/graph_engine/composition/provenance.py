from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class StandardLoader(str, Enum):
    SOURCE = "source"
    NAMESPACE = "namespace"
    EXTENSION = "extension"


@dataclass(frozen=True, slots=True)
class ModuleProvenance:
    """One canonical source/import proof consumed throughout a binding lifetime."""

    standard_loader: StandardLoader
    standard_is_package: bool
    canonical_origin: Path | None
    canonical_locations: tuple[Path, ...]
    authenticated_locations: tuple[Path, ...]
    physical_sha256: str | None
    source_digest: str

    def authenticates_same_module(self, other: ModuleProvenance) -> bool:
        """Compare physical standard-import authority across selected source identities."""
        return (
            self.standard_loader is other.standard_loader
            and self.standard_is_package == other.standard_is_package
            and self.canonical_origin == other.canonical_origin
            and self.canonical_locations == other.canonical_locations
            and self.physical_sha256 == other.physical_sha256
        )


__all__ = ["ModuleProvenance", "StandardLoader"]
