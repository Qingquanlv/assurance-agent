from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CapabilityPin:
    owner_id: str
    distribution: str
    entrypoint_name: str
    plugin: str
    version: str = "0.3.0"

    @property
    def package(self) -> str:
        return self.plugin.split(".", 1)[0]


CAPABILITIES: tuple[CapabilityPin, ...] = (
    CapabilityPin(
        "assurance.intake",
        "assurance-intake",
        "intake",
        "assurance_intake.plugin:IntakePlugin",
    ),
    CapabilityPin(
        "assurance.generation",
        "assurance-generation",
        "generation",
        "assurance_generation.plugin:GenerationPlugin",
    ),
    CapabilityPin(
        "assurance.execution",
        "assurance-execution",
        "execution",
        "assurance_execution.plugin:ExecutionPlugin",
    ),
    CapabilityPin(
        "assurance.healing",
        "assurance-healing",
        "healing",
        "assurance_healing.plugin:HealingPlugin",
    ),
    CapabilityPin(
        "assurance.quality",
        "assurance-quality",
        "quality",
        "assurance_quality.plugin:QualityPlugin",
    ),
    CapabilityPin(
        "assurance.improvement",
        "assurance-improvement",
        "improvement",
        "assurance_improvement.plugin:ImprovementPlugin",
    ),
)
CAPABILITY_OWNERS: tuple[str, ...] = tuple(pin.owner_id for pin in CAPABILITIES)
