"""The one dormant registry for layer-specific generated-file contracts."""

from dataclasses import dataclass

from assurance_agent.artifacts.models.assurance import LayerName


@dataclass(frozen=True, slots=True)
class LayerGeneratedFilesContract:
    layer: LayerName
    summary_path: str
    manifest_path: str
    private_test_root: str
    model_id: str


_CONTRACTS: tuple[LayerGeneratedFilesContract, ...] = (
    LayerGeneratedFilesContract(
        "api",
        "change:codegen/api-codegen-summary.md",
        "change:codegen/api-generated-files.json",
        "tests/api",
        "api_generated_files/v1",
    ),
    LayerGeneratedFilesContract(
        "e2e",
        "change:codegen/e2e-codegen-summary.md",
        "change:codegen/e2e-generated-files.json",
        "tests/e2e",
        "e2e_generated_files/v1",
    ),
    LayerGeneratedFilesContract(
        "fuzz",
        "change:codegen/fuzz-codegen-summary.md",
        "change:codegen/fuzz-generated-files.json",
        "tests/fuzz",
        "fuzz_generated_files/v1",
    ),
    LayerGeneratedFilesContract(
        "performance",
        "change:codegen/performance-codegen-summary.md",
        "change:codegen/performance-generated-files.json",
        "tests/perf",
        "performance_generated_files/v1",
    ),
)
_BY_LAYER = {contract.layer: contract for contract in _CONTRACTS}


def get_generated_files_contract(layer: str) -> LayerGeneratedFilesContract:
    """Return one exact contract or raise ValueError for an unknown layer."""
    try:
        return _BY_LAYER[layer]
    except KeyError as error:
        raise ValueError(f"unknown assurance layer: {layer}") from error


__all__ = ["LayerGeneratedFilesContract", "get_generated_files_contract"]
