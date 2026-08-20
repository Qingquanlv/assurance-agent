from importlib.machinery import EXTENSION_SUFFIXES
from pathlib import Path
import sys
from types import ModuleType

from importlib import metadata

from graph_engine.composition.import_plan import (
    ImportPlanSession,
    ModuleImportPlan,
    ModuleClassification,
    ModuleRole,
    build_import_provenance_plan,
)
from graph_engine.composition.models import SourceFile, SourceIdentity, SourceKind, SourceSnapshot
from graph_engine.plugin_api import ProviderSource


def _source(
    *,
    entrypoint_value: str,
    import_roots: tuple[str, ...] = ("",),
) -> ProviderSource:
    return ProviderSource(
        distribution="plan-runtime",
        version="1.2.3",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="plan.runtime",
        entrypoint_value=entrypoint_value,
        declaration_path="plan-declaration.json",
        import_roots=import_roots,
    )


def _snapshot(root: Path, source: ProviderSource, files: dict[str, bytes]) -> SourceSnapshot:
    return SourceSnapshot.from_identity(
        SourceIdentity(
            kind=SourceKind.WHEEL_PLUGIN,
            root=root.resolve(),
            distribution=source.distribution,
            version=source.version,
            entrypoint_group=source.entrypoint_group,
            entrypoint_name=source.entrypoint_name,
            entrypoint_value=source.entrypoint_value,
            declaration_path=source.declaration_path,
            import_roots=source.import_roots,
            plugin_id="plan.runtime",
            plugin_version="1.2.3",
        ),
        tuple(SourceFile.from_bytes(path, content) for path, content in files.items()),
    )


def _write_files(root: Path, files: dict[str, bytes]) -> None:
    for relative_path, content in files.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _entrypoint(source: ProviderSource) -> metadata.EntryPoint:
    return metadata.EntryPoint(
        name=source.entrypoint_name,
        value=source.entrypoint_value,
        group=source.entrypoint_group,
    )


def _remove_modules(*prefixes: str) -> None:
    for module_name in tuple(sys.modules):
        if any(module_name == prefix or module_name.startswith(f"{prefix}.") for prefix in prefixes):
            sys.modules.pop(module_name, None)


def _reject_unowned(_entry: ModuleImportPlan, _module: ModuleType) -> bool:
    return False


def test_plan_contains_only_the_current_entrypoint_lineage(tmp_path: Path) -> None:
    source = _source(entrypoint_value="alpha_namespace.provider:provider")
    snapshot = _snapshot(
        tmp_path,
        source,
        {
            "alpha_namespace/provider.py": b"provider = object()\n",
            "unrelated_package/__init__.py": b"raise AssertionError('must not execute')\n",
            "unrelated_package/plugins/provider.py": b"provider = object()\n",
        },
    )
    entrypoint = metadata.EntryPoint(
        name="plan.runtime",
        value=source.entrypoint_value,
        group=source.entrypoint_group,
    )

    plan = build_import_provenance_plan(source, snapshot, entrypoint)

    assert tuple(entry.module_name for entry in plan.modules) == (
        "alpha_namespace",
        "alpha_namespace.provider",
    )
    assert plan.modules[0].roles == (ModuleRole.ANCESTOR,)
    assert plan.modules[0].classification is ModuleClassification.NAMESPACE
    assert plan.modules[1].roles == (ModuleRole.ENTRYPOINT,)


def test_plan_extends_with_provider_leaf_and_rejectable_initial_state(tmp_path: Path) -> None:
    source = _source(entrypoint_value="bridge_package.entry:provider")
    snapshot = _snapshot(
        tmp_path,
        source,
        {
            "bridge_package/__init__.py": b"",
            "bridge_package/entry.py": b"provider = object()\n",
            "implementation_package/__init__.py": b"",
            "implementation_package/provider.py": b"provider = object()\n",
        },
    )
    entrypoint = metadata.EntryPoint(
        name="plan.runtime",
        value=source.entrypoint_value,
        group=source.entrypoint_group,
    )
    preloaded = ModuleType("implementation_package.provider")
    sys.modules[preloaded.__name__] = preloaded
    try:
        plan = build_import_provenance_plan(
            source,
            snapshot,
            entrypoint,
            provider_module="implementation_package.provider",
        )
    finally:
        sys.modules.pop(preloaded.__name__, None)

    provider_entry = plan.module("implementation_package.provider")
    assert provider_entry.roles == (ModuleRole.PROVIDER,)
    assert provider_entry.initial_module is preloaded
    assert set(entry.module_name for entry in plan.modules) == {
        "bridge_package",
        "bridge_package.entry",
        "implementation_package",
        "implementation_package.provider",
    }


def test_plan_maps_editable_src_layout_from_authenticated_import_root(tmp_path: Path) -> None:
    source = _source(
        entrypoint_value="acme.plugins.bridge:provider",
        import_roots=("src",),
    )
    snapshot = _snapshot(
        tmp_path,
        source,
        {
            "src/acme/plugins/bridge.py": b"provider = object()\n",
            "src/acme/implementations/provider.py": b"provider = object()\n",
        },
    )
    entrypoint = metadata.EntryPoint(
        name="plan.runtime",
        value=source.entrypoint_value,
        group=source.entrypoint_group,
    )

    plan = build_import_provenance_plan(
        source,
        snapshot,
        entrypoint,
        provider_module="acme.implementations.provider",
    )

    assert all(not entry.module_name.startswith("src.") for entry in plan.modules)
    assert plan.module("acme").namespace_locations == (tmp_path / "src" / "acme",)
    assert plan.module("acme.plugins.bridge").physical_origin == (
        tmp_path / "src" / "acme" / "plugins" / "bridge.py"
    )


def test_plan_uses_one_initializer_predicate_for_extension_packages(tmp_path: Path) -> None:
    suffix = EXTENSION_SUFFIXES[0]
    source = _source(entrypoint_value="provider_package.plugins.impl:provider")
    snapshot = _snapshot(
        tmp_path,
        source,
        {
            f"provider_package/__init__{suffix}": b"extension initializer",
            "provider_package/plugins/impl.py": b"provider = object()\n",
        },
    )
    entrypoint = metadata.EntryPoint(
        name="plan.runtime",
        value=source.entrypoint_value,
        group=source.entrypoint_group,
    )

    plan = build_import_provenance_plan(source, snapshot, entrypoint)

    assert plan.module("provider_package").classification is ModuleClassification.EXTENSION
    assert plan.module("provider_package.plugins").classification is ModuleClassification.NAMESPACE
    assert plan.module("provider_package.plugins.impl").classification is ModuleClassification.REGULAR


def test_import_plan_loads_only_current_disjoint_entrypoint_in_both_orders(
    tmp_path: Path,
    monkeypatch,
) -> None:
    files = {
        "plan_alpha/entry.py": b"provider = object()\n",
        "plan_beta/__init__.py": (
            b"import builtins\nbuiltins._plan_beta_imports = getattr(builtins, '_plan_beta_imports', 0) + 1\n"
        ),
        "plan_beta/plugins/entry.py": b"provider = object()\n",
    }
    _write_files(tmp_path, files)
    monkeypatch.setattr("builtins._plan_beta_imports", 0, raising=False)

    def run(order: tuple[str, str]) -> None:
        _remove_modules("plan_alpha", "plan_beta")
        monkeypatch.setattr("builtins._plan_beta_imports", 0, raising=False)
        for name in order:
            imports_before = getattr(__import__("builtins"), "_plan_beta_imports")
            value = "plan_alpha.entry:provider" if name == "alpha" else "plan_beta.plugins.entry:provider"
            source = _source(entrypoint_value=value)
            snapshot = _snapshot(tmp_path, source, files)
            entrypoint = _entrypoint(source)
            initial = dict(sys.modules)
            plan = build_import_provenance_plan(
                source,
                snapshot,
                entrypoint,
                initial_modules=initial,
            )
            with ImportPlanSession(source, snapshot, initial) as session:
                session.preload(plan, _reject_unowned)
                entrypoint.load()
                session.validate(plan, _reject_unowned)
            if name == "alpha":
                assert getattr(__import__("builtins"), "_plan_beta_imports") == imports_before
        assert getattr(__import__("builtins"), "_plan_beta_imports") == 1

    try:
        run(("alpha", "beta"))
        run(("beta", "alpha"))
    finally:
        _remove_modules("plan_alpha", "plan_beta")


def test_import_plan_rejects_same_path_preload_behind_regular_bridge(tmp_path: Path) -> None:
    files = {
        "plan_bridge/__init__.py": b"",
        "plan_bridge/entry.py": b"from plan_implementation.provider import provider\n",
        "plan_implementation/__init__.py": b"",
        "plan_implementation/provider.py": (b"class Provider: pass\nprovider = Provider()\n"),
    }
    _write_files(tmp_path, files)
    source = _source(entrypoint_value="plan_bridge.entry:provider")
    snapshot = _snapshot(tmp_path, source, files)
    entrypoint = _entrypoint(source)
    fake_parent = ModuleType("plan_implementation")
    fake_parent.__path__ = [str(tmp_path / "plan_implementation")]
    fake_provider_module = ModuleType("plan_implementation.provider")
    fake_provider_module.__file__ = str(tmp_path / "plan_implementation" / "provider.py")
    fake_provider_type = type("Provider", (), {})
    fake_provider_type.__module__ = "plan_implementation.provider"
    setattr(fake_provider_module, "provider", fake_provider_type())
    sys.modules[fake_parent.__name__] = fake_parent
    sys.modules[fake_provider_module.__name__] = fake_provider_module
    try:
        initial = dict(sys.modules)
        phase_one = build_import_provenance_plan(
            source,
            snapshot,
            entrypoint,
            initial_modules=initial,
        )
        with ImportPlanSession(source, snapshot, initial) as session:
            session.preload(phase_one, _reject_unowned)
            provider = entrypoint.load()
            final = build_import_provenance_plan(
                source,
                snapshot,
                entrypoint,
                provider_module=provider.__module__,
                initial_modules=initial,
            )
            try:
                session.preload(final, _reject_unowned)
            except Exception as error:
                assert "not platform-authenticated" in str(error)
            else:  # pragma: no cover - regression guard.
                raise AssertionError("same-path preloaded provider was accepted")
    finally:
        _remove_modules("plan_bridge", "plan_implementation")


def test_import_plan_rejects_namespace_created_transitively_by_regular_parent(
    tmp_path: Path,
) -> None:
    files = {
        "plan_regular/__init__.py": (
            b"import sys, types\n"
            b"fake = types.ModuleType('plan_regular.plugins')\n"
            b"fake.__path__ = [__path__[0] + '/plugins']\n"
            b"sys.modules['plan_regular.plugins'] = fake\n"
        ),
        "plan_regular/plugins/entry.py": b"provider = object()\n",
    }
    _write_files(tmp_path, files)
    source = _source(entrypoint_value="plan_regular.plugins.entry:provider")
    snapshot = _snapshot(tmp_path, source, files)
    entrypoint = _entrypoint(source)
    initial = dict(sys.modules)
    plan = build_import_provenance_plan(
        source,
        snapshot,
        entrypoint,
        initial_modules=initial,
    )
    try:
        with ImportPlanSession(source, snapshot, initial) as session:
            try:
                session.preload(plan, _reject_unowned)
            except Exception as error:
                assert "planned standard import" in str(error)
            else:  # pragma: no cover - regression guard.
                raise AssertionError("transitively forged namespace was accepted")
    finally:
        _remove_modules("plan_regular")


def test_editable_src_namespace_reexport_first_load_and_repeat_use_same_plan(
    tmp_path: Path,
) -> None:
    files = {
        "src/plan_editable/bridge.py": (b"from plan_editable.implementation.provider import provider\n"),
        "src/plan_editable/implementation/provider.py": (b"class Provider: pass\nprovider = Provider()\n"),
    }
    _write_files(tmp_path, files)
    source = _source(
        entrypoint_value="plan_editable.bridge:provider",
        import_roots=("src",),
    )
    snapshot = _snapshot(tmp_path, source, files)
    entrypoint = _entrypoint(source)
    authority: dict[str, tuple[ModuleType, str]] = {}

    def is_owned(entry: ModuleImportPlan, module: ModuleType) -> bool:
        return authority.get(entry.module_name) == (module, entry.source_digest)

    try:
        initial = dict(sys.modules)
        phase_one = build_import_provenance_plan(
            source,
            snapshot,
            entrypoint,
            initial_modules=initial,
        )
        with ImportPlanSession(source, snapshot, initial) as session:
            session.preload(phase_one, is_owned)
            provider = entrypoint.load()
            final = build_import_provenance_plan(
                source,
                snapshot,
                entrypoint,
                provider_module=provider.__module__,
                initial_modules=initial,
            )
            session.preload(final, is_owned)
            validated = session.validate(final, is_owned)
        authority.update((entry.module_name, (module, entry.source_digest)) for entry, module in validated)

        repeated = build_import_provenance_plan(
            source,
            snapshot,
            entrypoint,
            provider_module=provider.__module__,
            initial_modules=dict(sys.modules),
        )
        with ImportPlanSession(source, snapshot, dict(sys.modules)) as session:
            session.preload(repeated, is_owned)
            assert entrypoint.load() is provider
            assert session.validate(repeated, is_owned)
    finally:
        _remove_modules("plan_editable")
