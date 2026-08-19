"""The one dormant registry for layer-specific generated-file contracts."""

from dataclasses import dataclass

from assurance_kernel.artifacts.models.assurance import LayerName
from assurance_kernel.artifacts.models.generated_files import (
    ApiGeneratedFilesV1,
    E2eGeneratedFilesV1,
    FuzzGeneratedFilesV1,
    GeneratedFilesV1,
    PerformanceGeneratedFilesV1,
)


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
_MODELS: dict[LayerName, type[GeneratedFilesV1]] = {
    "api": ApiGeneratedFilesV1,
    "e2e": E2eGeneratedFilesV1,
    "fuzz": FuzzGeneratedFilesV1,
    "performance": PerformanceGeneratedFilesV1,
}
_BY_MODEL_ID = {contract.model_id: contract for contract in _CONTRACTS}


def get_generated_files_contract(layer: str) -> LayerGeneratedFilesContract:
    """Return one exact contract or raise ValueError for an unknown layer."""
    try:
        return _BY_LAYER[layer]
    except KeyError as error:
        raise ValueError(f"unknown assurance layer: {layer}") from error


def get_generated_files_model(layer: str) -> type[GeneratedFilesV1]:
    """Return the strict layer-specific manifest model."""
    contract = get_generated_files_contract(layer)
    return _MODELS[contract.layer]


def generated_files_contract_for_model_id(model_id: str) -> LayerGeneratedFilesContract:
    try:
        return _BY_MODEL_ID[model_id]
    except KeyError as error:
        raise ValueError(f"unknown generated-files model id: {model_id}") from error


__all__ = [
    "LayerGeneratedFilesContract",
    "generated_files_contract_for_model_id",
    "get_generated_files_contract",
    "get_generated_files_model",
]
