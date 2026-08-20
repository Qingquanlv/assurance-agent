from __future__ import annotations

import hashlib
import json
import math

import pytest
from pydantic import ValidationError

from graph_engine.composition import (
    InvocationLock,
    LockedDependency,
    LockedPlugin,
    LockedProduct,
    LockedSource,
    LockedSourceFile,
    RegistryDigests,
    RegistryProjections,
)
from graph_engine.canonical import canonical_digest, canonical_json_bytes


_A = "a" * 64
_B = "b" * 64
_C = "c" * 64
_D = "d" * 64
_E = "e" * 64
_F = "f" * 64


def _lock(*, reverse_manifest: bool = False) -> InvocationLock:
    manifest = (
        {"product_id": "toy.a", "entrypoints": {"你好": "根"}}
        if reverse_manifest
        else {"entrypoints": {"你好": "根"}, "product_id": "toy.a"}
    )
    product_source = LockedSource(
        kind="wheel_product",
        identity={"entrypoint_name": "toy.a", "distribution": "toy-a"},
        digest=_A,
        files=(LockedSourceFile(path="toy_a/product.py", sha256=_B),),
    )
    plugin_source = LockedSource(
        kind="wheel_plugin",
        identity={"entrypoint_name": "toy.runtime", "distribution": "toy-runtime"},
        digest=_C,
        files=(LockedSourceFile(path="toy_runtime/plugin.py", sha256=_D),),
    )
    product = LockedProduct(
        product_id="toy.a",
        product_version="1.0.0",
        manifest=manifest,
        manifest_digest=hashlib.sha256(canonical_json_bytes(manifest)).hexdigest(),
        source=product_source,
    )
    plugin = LockedPlugin(
        plugin_id="toy.runtime",
        plugin_version="2.0.0",
        descriptor_digest=_E,
        dependencies=(LockedDependency(plugin_id="toy.base", version_specifier=">=1,<2"),),
        source=plugin_source,
    )
    registry_projections = RegistryProjections(
        sources=[{"source_id": "graph.engine", "digest": _F}],
        capabilities=[
            {
                "capability_id": "toy.runtime.alias",
                "data": {"locale": "zh-CN"},
                "implementation_digest": _C,
                "kind": "binding",
                "owner_id": "toy.runtime",
                "resource_ids": ["toy.runtime.prompt"],
                "target_capability_id": "toy.runtime.greet",
            }
        ],
        schemas=[],
        resources=[{"resource_id": "toy.runtime.prompt", "sha256": _D}],
        effects=[],
    )
    registry_digests = RegistryDigests(
        sources=canonical_digest([{"source_id": "graph.engine", "digest": _F}]),
        capabilities=canonical_digest(
            [
                {
                    "capability_id": "toy.runtime.alias",
                    "data": {"locale": "zh-CN"},
                    "implementation_digest": _C,
                    "kind": "binding",
                    "owner_id": "toy.runtime",
                    "resource_ids": ["toy.runtime.prompt"],
                    "target_capability_id": "toy.runtime.greet",
                }
            ]
        ),
        schemas=canonical_digest([]),
        resources=canonical_digest([{"resource_id": "toy.runtime.prompt", "sha256": _D}]),
        effects=canonical_digest([]),
    )
    configuration = {"toy.runtime": {"greeting": "你好"}}
    capability_bindings = [
        {
            "capability_id": "toy.runtime.alias",
            "data": {"locale": "zh-CN"},
            "implementation_digest": _C,
            "kind": "binding",
            "owner_id": "toy.runtime",
            "resource_ids": ["toy.runtime.prompt"],
            "target_capability_id": "toy.runtime.greet",
        }
    ]
    compiled_workflow = {
        "entrypoints": {"你好": "根"},
        "graphs": {"根": {"start": "node"}},
        "name": "toy",
    }
    return InvocationLock.create(
        engine_api="1.0",
        engine_digest=_F,
        product=product,
        plugins=(plugin,),
        dependency_order=("toy.runtime",),
        registry_projections=registry_projections,
        registry_digests=registry_digests,
        configuration=configuration,
        configuration_digest=canonical_digest(configuration),
        capability_bindings=capability_bindings,
        capability_bindings_digest=canonical_digest(capability_bindings),
        compiled_workflow=compiled_workflow,
        compiled_workflow_digest=canonical_digest(compiled_workflow),
    )


def _recreate_lock(
    lock: InvocationLock,
    *,
    registry_projections: RegistryProjections | None = None,
    registry_digests: RegistryDigests | None = None,
    configuration: object | None = None,
    configuration_digest: str | None = None,
    capability_bindings: object | None = None,
    capability_bindings_digest: str | None = None,
    compiled_workflow: object | None = None,
    compiled_workflow_digest: str | None = None,
) -> InvocationLock:
    return InvocationLock.create(
        engine_api=lock.engine_api,
        engine_digest=lock.engine_digest,
        product=lock.product,
        plugins=lock.plugins,
        dependency_order=lock.dependency_order,
        registry_projections=registry_projections or lock.registry_projections,
        registry_digests=registry_digests or lock.registry_digests,
        configuration=lock.configuration if configuration is None else configuration,
        configuration_digest=configuration_digest or lock.configuration_digest,
        capability_bindings=(
            lock.capability_bindings if capability_bindings is None else capability_bindings
        ),
        capability_bindings_digest=(capability_bindings_digest or lock.capability_bindings_digest),
        compiled_workflow=(lock.compiled_workflow if compiled_workflow is None else compiled_workflow),
        compiled_workflow_digest=(compiled_workflow_digest or lock.compiled_workflow_digest),
    )


def test_invocation_lock_has_one_golden_canonical_projection() -> None:
    lock = _lock()

    expected = (
        '{"capability_bindings":[{"capability_id":"toy.runtime.alias","data":{"locale":"zh-CN"},'
        '"implementation_digest":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
        '"kind":"binding","owner_id":"toy.runtime","resource_ids":["toy.runtime.prompt"],'
        '"target_capability_id":"toy.runtime.greet"}],"capability_bindings_digest":'
        '"dffdf2389f5b4ddb5326e612b76de04d00153f6e8577cbd3a56e46553b295973",'
        '"compiled_workflow":{"entrypoints":{"你好":"根"},"graphs":{"根":{"start":"node"}},'
        '"name":"toy"},"compiled_workflow_digest":'
        '"c77e73e3e946970327283bf1cfa31c500c69a4b1a1f4bfe3d7a73b6eea54a344",'
        '"configuration":{"toy.runtime":{"greeting":"你好"}},"configuration_digest":'
        '"5d074489f17d704e0bed8396c3cd634359c62571aaa9cce3366ca4dcd146a0cf",'
        '"dependency_order":["toy.runtime"],"digest_algorithm":"graph-engine-source-v1",'
        '"engine_api":"1.0","engine_digest":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",'
        '"plugins":[{"dependencies":[{"plugin_id":"toy.base","version_specifier":"<2,>=1"}],'
        '"descriptor_digest":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",'
        '"plugin_id":"toy.runtime","plugin_version":"2.0.0","source":{'
        '"digest":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
        '"files":[{"path":"toy_runtime/plugin.py","sha256":'
        '"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}],'
        '"identity":{"distribution":"toy-runtime","entrypoint_name":"toy.runtime"},'
        '"kind":"wheel_plugin"}}],"product":{"manifest":{"entrypoints":{"你好":"根"},'
        '"product_id":"toy.a"},"manifest_digest":'
        '"767523ff9ec5b9ec22a926b4cdff280d345a44d42ab7abd9e4fa3b865af75c15",'
        '"product_id":"toy.a","product_version":"1.0.0","source":{'
        '"digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"files":[{"path":"toy_a/product.py","sha256":'
        '"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}],'
        '"identity":{"distribution":"toy-a","entrypoint_name":"toy.a"},'
        '"kind":"wheel_product"}},"registry_digests":{"capabilities":'
        '"dffdf2389f5b4ddb5326e612b76de04d00153f6e8577cbd3a56e46553b295973",'
        '"effects":"4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",'
        '"resources":"2d4f065bb585bd2967f593ff72573972c23e858b604fa24bf5e2147c6b609a85",'
        '"schemas":"4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",'
        '"sources":"394c48218fe483e3236f06612daae0545e1f81556f23a7b835a8a58da306b51b"},'
        '"registry_projections":{"capabilities":[{"capability_id":"toy.runtime.alias",'
        '"data":{"locale":"zh-CN"},"implementation_digest":'
        '"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
        '"kind":"binding","owner_id":"toy.runtime","resource_ids":["toy.runtime.prompt"],'
        '"target_capability_id":"toy.runtime.greet"}],"effects":[],"resources":'
        '[{"resource_id":"toy.runtime.prompt","sha256":'
        '"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}],'
        '"schemas":[],"sources":[{"digest":'
        '"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",'
        '"source_id":"graph.engine"}]},'
        '"schema_version":"1"}'
    ).encode()
    assert lock.canonical_bytes == expected
    assert lock.digest == hashlib.sha256(expected).hexdigest()
    assert b'"canonical_bytes"' not in lock.canonical_bytes
    assert "digest" not in json.loads(lock.canonical_bytes)


def test_lock_projection_normalizes_unicode_map_and_tuple_order() -> None:
    first = _lock()
    second = _lock(reverse_manifest=True)

    assert first.canonical_bytes == second.canonical_bytes
    assert first.dependency_order == ("toy.runtime",)
    assert b'["toy.runtime"]' in first.canonical_bytes
    assert "你好".encode() in first.canonical_bytes
    assert b"\\u4f60" not in first.canonical_bytes


@pytest.mark.parametrize("bad_float", (math.nan, math.inf, -math.inf))
def test_lock_values_reject_non_finite_floats(bad_float: float) -> None:
    with pytest.raises((ValidationError, ValueError), match="finite"):
        LockedProduct(
            product_id="toy.a",
            product_version="1.0.0",
            manifest={"ratio": bad_float},
            manifest_digest=_A,
            source=LockedSource(
                kind="product_file",
                identity={"product_id": "toy.a"},
                digest=_B,
                files=(),
            ),
        )


def test_invocation_lock_rejects_noncanonical_bytes_and_digest() -> None:
    lock = _lock()

    with pytest.raises(ValidationError, match="canonical bytes"):
        InvocationLock(
            **lock.model_dump(exclude={"canonical_bytes", "digest"}),
            canonical_bytes=b"{}",
            digest=hashlib.sha256(b"{}").hexdigest(),
        )
    with pytest.raises(ValidationError, match="digest"):
        InvocationLock(
            **lock.model_dump(exclude={"canonical_bytes", "digest"}),
            canonical_bytes=lock.canonical_bytes,
            digest=_A,
        )


def test_locked_plugin_authenticates_id_and_version_against_source_identity() -> None:
    source = LockedSource(
        kind="wheel_plugin",
        identity={
            "distribution": "toy-runtime",
            "version": "2.0.0",
            "entrypoint_group": "graph_engine.plugins",
            "entrypoint_name": "toy.runtime",
        },
        digest=_A,
        files=(),
    )

    with pytest.raises(ValidationError, match="source identity"):
        LockedPlugin(
            plugin_id="toy.other",
            plugin_version="2.0.0",
            descriptor_digest=_B,
            dependencies=(),
            source=source,
        )
    with pytest.raises(ValidationError, match="source identity"):
        LockedPlugin(
            plugin_id="toy.runtime",
            plugin_version="3.0.0",
            descriptor_digest=_B,
            dependencies=(),
            source=source,
        )


def test_lock_authenticates_auditable_projections_against_their_digests() -> None:
    lock = _lock()

    with pytest.raises(ValidationError, match="registry digests"):
        _recreate_lock(
            lock,
            registry_digests=lock.registry_digests.model_copy(update={"sources": _A}),
        )
    with pytest.raises(ValidationError, match="configuration digest"):
        _recreate_lock(lock, configuration={"toy.runtime": {"greeting": "changed"}})
    with pytest.raises(ValidationError, match="capability bindings disagree"):
        _recreate_lock(
            lock,
            capability_bindings=[],
            capability_bindings_digest=canonical_digest([]),
        )
    with pytest.raises(ValidationError, match="workflow digest"):
        _recreate_lock(lock, compiled_workflow={"entrypoints": {}, "graphs": {}, "name": "toy"})
