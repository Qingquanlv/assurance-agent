from __future__ import annotations

from itertools import permutations

import pytest

from graph_engine.composition import DependencyConflict
from graph_engine.composition import resolve_dependency_order as _resolve_dependency_order
from graph_engine.composition.models import PluginRequirement
from graph_engine.plugin_api import PluginDependency, PluginDescriptor


def _descriptor(
    plugin_id: str,
    version: str = "1.0.0",
    *,
    engine_api: str = ">=2,<3",
    requires: tuple[tuple[str, str], ...] = (),
) -> PluginDescriptor:
    return PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id=plugin_id,
        plugin_version=version,
        engine_api=engine_api,
        task_handlers=(),
        commit_validators=(),
        dependencies=tuple(
            PluginDependency(dependency_id, specifier) for dependency_id, specifier in requires
        ),
    )


def _with_unvalidated_version(descriptor: PluginDescriptor, version: str) -> PluginDescriptor:
    object.__setattr__(descriptor, "plugin_version", version)
    return descriptor


def resolve_dependency_order(
    descriptors: dict[str, PluginDescriptor],
    required_plugin_ids: tuple[str, ...],
) -> tuple[str, ...]:
    requirements = tuple(
        PluginRequirement.model_construct(plugin_id=plugin_id, version_specifier=">=0")
        for plugin_id in required_plugin_ids
    )
    return _resolve_dependency_order(descriptors, requirements)


def test_resolver_validates_selected_versions_without_choosing() -> None:
    descriptors = {
        "toy.flow": _descriptor("toy.flow", "2.0.0", requires=(("toy.runtime", ">=1,<2"),)),
        "toy.runtime": _descriptor("toy.runtime", "1.4.0"),
    }

    assert resolve_dependency_order(descriptors, ("toy.flow", "toy.runtime")) == (
        "toy.runtime",
        "toy.flow",
    )


def test_resolver_rejects_incompatible_explicit_version() -> None:
    descriptors = {
        "toy.flow": _descriptor("toy.flow", "2.0.0", requires=(("toy.runtime", ">=1,<2"),)),
        "toy.runtime": _descriptor("toy.runtime", "2.1.0"),
    }

    with pytest.raises(DependencyConflict, match=r"toy.runtime==2.1.0"):
        resolve_dependency_order(descriptors, tuple(descriptors))


def test_resolver_rejects_incompatible_product_root_version() -> None:
    descriptor = _descriptor("toy.runtime", "1.0.0")

    with pytest.raises(DependencyConflict, match=r"toy.runtime==1.0.0"):
        _resolve_dependency_order(
            {"toy.runtime": descriptor},
            (PluginRequirement(plugin_id="toy.runtime", version_specifier="==9.9.9"),),
        )


def test_resolver_rejects_missing_explicit_dependency_source() -> None:
    descriptors = {
        "toy.flow": _descriptor("toy.flow", requires=(("toy.runtime", ">=1,<2"),)),
    }

    with pytest.raises(DependencyConflict, match=r"missing selected plugin source.*toy.runtime"):
        resolve_dependency_order(descriptors, ("toy.flow",))


def test_resolver_rejects_selected_source_outside_exact_closure() -> None:
    descriptors = {
        "toy.flow": _descriptor("toy.flow", requires=(("toy.runtime", ">=1,<2"),)),
        "toy.runtime": _descriptor("toy.runtime"),
        "toy.unused": _descriptor("toy.unused"),
    }

    with pytest.raises(DependencyConflict, match=r"unexpected selected plugin source.*toy.unused"):
        resolve_dependency_order(descriptors, ("toy.flow",))


def test_resolver_rejects_self_dependency() -> None:
    descriptors = {
        "toy.flow": _descriptor("toy.flow", requires=(("toy.flow", ">=1"),)),
    }

    with pytest.raises(DependencyConflict, match=r"toy.flow cannot depend on itself"):
        resolve_dependency_order(descriptors, ("toy.flow",))


def test_resolver_rejects_multi_node_cycle_with_canonical_member_list() -> None:
    descriptors = {
        "toy.alpha": _descriptor("toy.alpha", requires=(("toy.beta", ">=1"),)),
        "toy.beta": _descriptor("toy.beta", requires=(("toy.gamma", ">=1"),)),
        "toy.gamma": _descriptor("toy.gamma", requires=(("toy.alpha", ">=1"),)),
    }

    with pytest.raises(
        DependencyConflict,
        match=r"dependency cycle among: toy.alpha, toy.beta, toy.gamma",
    ):
        resolve_dependency_order(descriptors, ("toy.alpha",))


def test_resolver_rejects_invalid_selected_pep_440_version() -> None:
    descriptor = _with_unvalidated_version(_descriptor("toy.runtime"), "not-a-version")

    with pytest.raises(DependencyConflict, match=r"invalid selected plugin version.*toy.runtime"):
        resolve_dependency_order({"toy.runtime": descriptor}, ("toy.runtime",))


def test_resolver_rejects_duplicate_dependency_declaration() -> None:
    descriptors = {
        "toy.flow": _descriptor(
            "toy.flow",
            requires=(("toy.runtime", ">=1"), ("toy.runtime", ">=1")),
        ),
        "toy.runtime": _descriptor("toy.runtime"),
    }

    with pytest.raises(DependencyConflict, match=r"duplicate dependency.*toy.runtime"):
        resolve_dependency_order(descriptors, ("toy.flow",))


def test_resolver_rejects_engine_api_mismatch() -> None:
    descriptors = {
        "toy.runtime": _descriptor("toy.runtime", engine_api=">=1,<2"),
    }

    with pytest.raises(DependencyConflict, match=r"requires engine API.*engine is 2.0"):
        resolve_dependency_order(descriptors, ("toy.runtime",))


def test_resolver_rejects_unqualified_selected_ids_for_all_input_permutations() -> None:
    entries = (
        ("toy.runtime", _descriptor("toy.runtime")),
        ("not-qualified", _descriptor("toy.other")),
    )
    errors: set[str] = set()

    for source_order in permutations(entries):
        for required_order in permutations(tuple(plugin_id for plugin_id, _descriptor_value in entries)):
            with pytest.raises(DependencyConflict) as error:
                resolve_dependency_order(dict(source_order), required_order)
            errors.add(str(error.value))

    assert errors == {"invalid selected plugin source ID: 'not-qualified'"}


def test_resolver_rejects_unqualified_required_roots_for_all_input_permutations() -> None:
    descriptors = {
        "toy.alpha": _descriptor("toy.alpha"),
        "toy.runtime": _descriptor("toy.runtime"),
    }
    errors: set[str] = set()

    for required_order in permutations(("toy.runtime", "not-qualified", "toy.alpha")):
        with pytest.raises(DependencyConflict) as error:
            resolve_dependency_order(descriptors, required_order)
        errors.add(str(error.value))

    assert errors == {"invalid required plugin ID: 'not-qualified'"}


def test_resolver_uses_canonical_order_for_all_input_permutations() -> None:
    entries = (
        ("toy.flow", _descriptor("toy.flow", requires=(("toy.runtime", ">=1"),))),
        ("toy.runtime", _descriptor("toy.runtime")),
        ("toy.alpha", _descriptor("toy.alpha")),
    )

    for source_order in permutations(entries):
        for required_order in permutations(tuple(plugin_id for plugin_id, _descriptor_value in entries)):
            assert resolve_dependency_order(dict(source_order), required_order) == (
                "toy.alpha",
                "toy.runtime",
                "toy.flow",
            )


def test_resolver_uses_canonical_error_text_for_all_input_permutations() -> None:
    entries = (
        ("toy.flow", _descriptor("toy.flow", requires=(("toy.runtime", ">=1,<2"),))),
        ("toy.runtime", _descriptor("toy.runtime", "2.0.0")),
    )
    errors: set[str] = set()

    for source_order in permutations(entries):
        for required_order in permutations(tuple(plugin_id for plugin_id, _descriptor_value in entries)):
            with pytest.raises(DependencyConflict) as error:
                resolve_dependency_order(dict(source_order), required_order)
            errors.add(str(error.value))

    assert errors == {"plugin toy.flow requires toy.runtime>=1,<2, but selected toy.runtime==2.0.0"}


def test_resolver_uses_canonical_cycle_error_for_all_input_permutations() -> None:
    entries = (
        ("toy.alpha", _descriptor("toy.alpha", requires=(("toy.beta", ">=1"),))),
        ("toy.beta", _descriptor("toy.beta", requires=(("toy.gamma", ">=1"),))),
        ("toy.gamma", _descriptor("toy.gamma", requires=(("toy.alpha", ">=1"),))),
    )
    errors: set[str] = set()

    for source_order in permutations(entries):
        for required_order in permutations(tuple(plugin_id for plugin_id, _descriptor_value in entries)):
            with pytest.raises(DependencyConflict) as error:
                resolve_dependency_order(dict(source_order), required_order)
            errors.add(str(error.value))

    assert errors == {"dependency cycle among: toy.alpha, toy.beta, toy.gamma"}


def test_resolver_uses_canonical_missing_parent_error_for_all_input_permutations() -> None:
    entries = (
        ("toy.flow", _descriptor("toy.flow", requires=(("toy.runtime", ">=1"),))),
        ("toy.report", _descriptor("toy.report", requires=(("toy.runtime", ">=1"),))),
    )
    errors: set[str] = set()

    for source_order in permutations(entries):
        for required_order in permutations(tuple(plugin_id for plugin_id, _descriptor_value in entries)):
            with pytest.raises(DependencyConflict) as error:
                resolve_dependency_order(dict(source_order), required_order)
            errors.add(str(error.value))

    assert errors == {"missing selected plugin source: toy.runtime (required by toy.flow, toy.report)"}
