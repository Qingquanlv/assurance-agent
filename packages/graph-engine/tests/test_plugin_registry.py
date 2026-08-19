from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

import graph_engine
from graph_engine.plugin_api import (
    CandidateFile,
    CandidateWriteSet,
    CapabilityRegistry,
    CapabilityRegistryError,
    EnginePorts,
    PluginDescriptor,
    PluginRuntime,
    ResourceClaims,
    TaskContext,
    TaskFailure,
    TaskHandler,
    TaskOutcome,
    TaskRequest,
    ValidationContext,
    ValidationResult,
    assemble_registry,
)


async def _ping(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
    return TaskOutcome.succeeded({"pong": True})


def test_plugin_contracts_are_available_from_the_package_api() -> None:
    assert graph_engine.TaskRequest is TaskRequest
    assert graph_engine.assemble_registry is assemble_registry


def _accept(_candidate: CandidateWriteSet, _context: ValidationContext) -> ValidationResult:
    return ValidationResult(accepted=True)


class AcceptingValidator:
    def validate(self, _candidate: CandidateWriteSet, _context: ValidationContext) -> ValidationResult:
        return ValidationResult(accepted=True)


@dataclass(frozen=True)
class Provider:
    plugin_id: str

    def descriptor(self) -> PluginDescriptor:
        return PluginDescriptor(
            plugin_id=self.plugin_id,
            plugin_version="1.0.0",
            engine_api="1.0",
            task_handlers=(f"{self.plugin_id}.ping",),
            commit_validators=(f"{self.plugin_id}.validate",),
        )

    def bind(self, _ports: EnginePorts) -> PluginRuntime:
        return PluginRuntime(
            task_handlers={f"{self.plugin_id}.ping": _ping},
            commit_validators={f"{self.plugin_id}.validate": AcceptingValidator()},
        )


def test_registry_is_local_and_exact() -> None:
    first = assemble_registry((Provider("toy.one"),))
    second = assemble_registry((Provider("toy.two"),))
    assert set(first.task_handlers) == {"toy.one.ping"}
    assert set(second.task_handlers) == {"toy.two.ping"}
    assert set(first.commit_validators) == {"toy.one.validate"}
    assert set(second.commit_validators) == {"toy.two.validate"}


def test_descriptor_and_runtime_must_match() -> None:
    class Broken(Provider):
        def bind(self, _ports: EnginePorts) -> PluginRuntime:
            return PluginRuntime(task_handlers={}, commit_validators={})

    with pytest.raises(CapabilityRegistryError, match="declared and bound task handlers differ"):
        assemble_registry((Broken("toy.broken"),))


def test_duplicate_capability_fails_without_last_wins() -> None:
    duplicate = Provider("toy.one")
    with pytest.raises(CapabilityRegistryError, match="duplicate plugin id"):
        assemble_registry((duplicate, duplicate))


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"status": "succeeded", "failure": {"kind": "internal", "message": "bad"}}, "failure"),
        ({"status": "failed"}, "failure"),
        (
            {"status": "failed", "failure": {"kind": "internal", "message": "bad"}, "stop_reason": "halt"},
            "stop_reason",
        ),
        ({"status": "stopped"}, "stop_reason"),
        ({"status": "stopped", "stop_reason": ""}, "stop_reason"),
        ({"status": "succeeded", "stop_reason": "halt"}, "stop_reason"),
    ],
)
def test_task_outcome_rejects_inconsistent_status_fields(fields: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        TaskOutcome.model_validate(fields)


def test_task_outcome_factories_create_consistent_results() -> None:
    assert TaskOutcome.succeeded({"answer": [1, True]}).model_dump(mode="json") == {
        "status": "succeeded",
        "output": {"answer": [1, True]},
        "failure": None,
        "stop_reason": None,
    }
    assert TaskOutcome.failed("timeout", "too slow").failure == TaskFailure(
        kind="timeout", message="too slow"
    )
    assert TaskOutcome.stopped("operator request").stop_reason == "operator request"


def test_task_handler_outcome_cannot_report_interrupted() -> None:
    with pytest.raises(ValidationError, match="succeeded.*failed.*stopped"):
        TaskOutcome.model_validate({"status": "interrupted"})


def test_task_contracts_are_frozen_forbid_extra_and_enforce_attempts() -> None:
    request = TaskRequest(
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        node_id="node-1",
        capability_id="toy.one.ping",
        attempt=1,
        input={"items": [1, None]},
    )
    with pytest.raises(ValidationError, match="frozen"):
        request.attempt = 2
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        TaskRequest.model_validate({**request.model_dump(), "attempt": 0})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TaskFailure.model_validate({"kind": "internal", "message": "bad", "code": 500})


def test_task_context_is_frozen() -> None:
    context = TaskContext(workspace_root=Path("/workspace"), heartbeat=lambda: None)
    with pytest.raises(AttributeError):
        context.workspace_root = Path("/elsewhere")


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "/root",
        "../secret",
        "src/../secret",
        "src//file",
        "./src",
        "src/./file",
        "C:\\secret",
        "C:secret",
    ],
)
@pytest.mark.parametrize("field", ["reads", "writes", "exclusive"])
def test_resource_claims_reject_invalid_relative_prefixes(field: str, prefix: str) -> None:
    with pytest.raises(ValidationError, match="relative resource prefix"):
        ResourceClaims.model_validate({field: [prefix]})


def test_resource_claims_accept_segment_aware_relative_prefixes() -> None:
    claims = ResourceClaims(reads=("src", "src/package/file.py"), writes=("build/output",))
    assert claims.model_dump(mode="json") == {
        "reads": ["src", "src/package/file.py"],
        "writes": ["build/output"],
        "exclusive": [],
    }


@pytest.mark.parametrize(
    "fields",
    [
        {"accepted": False},
        {"accepted": False, "reason": ""},
        {"accepted": True, "reason": "not actually accepted"},
    ],
)
def test_validation_result_requires_reason_exactly_for_rejection(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="reason"):
        ValidationResult.model_validate(fields)


def test_candidate_and_validation_contracts_are_json_compatible_and_frozen() -> None:
    candidate = CandidateWriteSet(
        baseline_tree_id="base",
        candidate_tree_id="candidate",
        files=(CandidateFile(path="src/a.py", before_sha256=None, after_sha256="abc"),),
    )
    context = ValidationContext(
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        node_id="node-1",
        resources=ResourceClaims(reads=("src",)),
    )
    assert candidate.model_dump(mode="json")["files"] == [
        {"path": "src/a.py", "before_sha256": None, "after_sha256": "abc"}
    ]
    assert context.model_dump(mode="json")["resources"]["reads"] == ["src"]
    with pytest.raises(ValidationError, match="frozen"):
        candidate.baseline_tree_id = "changed"


def test_empty_registry_uses_immutable_empty_mappings() -> None:
    registry = CapabilityRegistry.empty()
    assert dict(registry.task_handlers) == {}
    assert dict(registry.commit_validators) == {}
    with pytest.raises(TypeError):
        registry.task_handlers["toy.one.ping"] = _ping


def test_registry_constructor_copies_mappings_to_preserve_immutability() -> None:
    handlers = {"toy.one.ping": _ping}
    registry = CapabilityRegistry(task_handlers=handlers, commit_validators={})
    handlers.clear()
    assert set(registry.task_handlers) == {"toy.one.ping"}
    with pytest.raises(TypeError):
        registry.task_handlers["toy.two.ping"] = _ping


@pytest.mark.parametrize(
    ("mapping_name", "message"),
    [("task_handlers", "invalid task handler id"), ("commit_validators", "invalid commit validator id")],
)
@pytest.mark.parametrize("invalid_id", ["invalid", 1])
def test_registry_constructor_rejects_invalid_capability_ids(
    mapping_name: str, message: str, invalid_id: object
) -> None:
    mappings = {"task_handlers": {}, "commit_validators": {}}
    mappings[mapping_name] = {invalid_id: _ping}
    with pytest.raises(CapabilityRegistryError, match=message):
        CapabilityRegistry(
            task_handlers=mappings["task_handlers"],  # type: ignore[arg-type]
            commit_validators=mappings["commit_validators"],  # type: ignore[arg-type]
        )


def test_registry_constructor_rejects_cross_kind_capability_collision() -> None:
    with pytest.raises(CapabilityRegistryError, match="handler and validator"):
        CapabilityRegistry(
            task_handlers={"toy.shared.capability": _ping},
            commit_validators={"toy.shared.capability": _accept},
        )


def test_assembled_registry_copies_runtime_mappings_and_is_immutable() -> None:
    handlers = {"toy.one.ping": _ping}

    class MutableProvider(Provider):
        def bind(self, _ports: EnginePorts) -> PluginRuntime:
            return PluginRuntime(
                task_handlers=handlers,
                commit_validators={"toy.one.validate": AcceptingValidator()},
            )

    registry = assemble_registry((MutableProvider("toy.one"),))
    handlers.clear()
    assert set(registry.task_handlers) == {"toy.one.ping"}
    with pytest.raises(TypeError):
        registry.task_handlers["toy.two.ping"] = _ping


def test_runtime_mapping_is_snapshotted_once_before_validation_and_installation() -> None:
    class ChangingMapping(Mapping[str, TaskHandler]):
        def __init__(self) -> None:
            self.iterations = 0

        def __getitem__(self, _key: str) -> TaskHandler:
            return _ping

        def __iter__(self) -> Iterator[str]:
            self.iterations += 1
            if self.iterations == 1:
                return iter(("toy.two.ping",))
            return iter(("toy.one.ping",))

        def __len__(self) -> int:
            return 1

    changing_handlers = ChangingMapping()

    class ChangingProvider(Provider):
        def bind(self, _ports: EnginePorts) -> PluginRuntime:
            return PluginRuntime(
                task_handlers=changing_handlers,
                commit_validators={"toy.two.validate": AcceptingValidator()},
            )

    registry = assemble_registry((Provider("toy.one"), ChangingProvider("toy.two")))
    assert set(registry.task_handlers) == {"toy.one.ping", "toy.two.ping"}
    assert changing_handlers.iterations == 1


def test_bind_is_called_exactly_once_with_engine_ports() -> None:
    calls: list[EnginePorts] = []

    class CountingProvider(Provider):
        def bind(self, ports: EnginePorts) -> PluginRuntime:
            calls.append(ports)
            return super().bind(ports)

    assemble_registry((CountingProvider("toy.one"),))
    assert calls == [EnginePorts(engine_api="1.0")]


@pytest.mark.parametrize("plugin_id", ["toy", "Toy.one", "toy_one.plugin", "toy/one"])
def test_invalid_plugin_ids_are_rejected(plugin_id: str) -> None:
    with pytest.raises(CapabilityRegistryError, match="invalid plugin id"):
        assemble_registry((Provider(plugin_id),))


def test_non_string_plugin_id_fails_as_a_registry_error() -> None:
    with pytest.raises(CapabilityRegistryError, match="invalid plugin id"):
        assemble_registry((Provider(1),))  # type: ignore[arg-type]


@pytest.mark.parametrize("capability_id", ["ping", "Toy.one.ping", "toy_one.ping", "toy/one.ping"])
def test_invalid_declared_capability_ids_are_rejected(capability_id: str) -> None:
    class InvalidCapability(Provider):
        def descriptor(self) -> PluginDescriptor:
            return PluginDescriptor(
                plugin_id=self.plugin_id,
                plugin_version="1.0.0",
                engine_api="1.0",
                task_handlers=(capability_id,),
                commit_validators=(),
            )

        def bind(self, _ports: EnginePorts) -> PluginRuntime:
            return PluginRuntime(
                task_handlers={capability_id: _ping},
                commit_validators={f"{self.plugin_id}.validate": AcceptingValidator()},
            )

    with pytest.raises(CapabilityRegistryError, match="invalid task handler id"):
        assemble_registry((InvalidCapability("toy.one"),))


def test_invalid_bound_capability_id_is_rejected_before_key_comparison() -> None:
    class InvalidRuntime(Provider):
        def bind(self, _ports: EnginePorts) -> PluginRuntime:
            return PluginRuntime(
                task_handlers={"invalid": _ping},
                commit_validators={f"{self.plugin_id}.validate": AcceptingValidator()},
            )

    with pytest.raises(CapabilityRegistryError, match="invalid bound task handler id"):
        assemble_registry((InvalidRuntime("toy.one"),))


def test_engine_api_mismatch_is_rejected_without_binding() -> None:
    bound = False

    class Incompatible(Provider):
        def descriptor(self) -> PluginDescriptor:
            descriptor = super().descriptor()
            return PluginDescriptor(
                plugin_id=descriptor.plugin_id,
                plugin_version=descriptor.plugin_version,
                engine_api="2.0",
                task_handlers=descriptor.task_handlers,
                commit_validators=descriptor.commit_validators,
            )

        def bind(self, _ports: EnginePorts) -> PluginRuntime:
            nonlocal bound
            bound = True
            return super().bind(_ports)

    with pytest.raises(CapabilityRegistryError, match="engine API"):
        assemble_registry((Incompatible("toy.one"),))
    assert bound is False


def test_duplicate_task_handler_across_plugins_is_rejected() -> None:
    class SharedCapability(Provider):
        def descriptor(self) -> PluginDescriptor:
            return PluginDescriptor(self.plugin_id, "1.0.0", "1.0", ("shared.task",), ())

        def bind(self, _ports: EnginePorts) -> PluginRuntime:
            return PluginRuntime({"shared.task": _ping}, {})

    with pytest.raises(CapabilityRegistryError, match="duplicate task handler"):
        assemble_registry((SharedCapability("toy.one"), SharedCapability("toy.two")))


def test_duplicate_validator_across_plugins_is_rejected() -> None:
    class SharedValidator(Provider):
        def descriptor(self) -> PluginDescriptor:
            return PluginDescriptor(self.plugin_id, "1.0.0", "1.0", (), ("shared.validate",))

        def bind(self, _ports: EnginePorts) -> PluginRuntime:
            return PluginRuntime({}, {"shared.validate": _accept})

    with pytest.raises(CapabilityRegistryError, match="duplicate commit validator"):
        assemble_registry((SharedValidator("toy.one"), SharedValidator("toy.two")))


def test_capability_id_cannot_be_shared_across_handler_and_validator_kinds() -> None:
    class Handler(Provider):
        def descriptor(self) -> PluginDescriptor:
            return PluginDescriptor(self.plugin_id, "1.0.0", "1.0", ("shared.capability",), ())

        def bind(self, _ports: EnginePorts) -> PluginRuntime:
            return PluginRuntime({"shared.capability": _ping}, {})

    class Validator(Provider):
        def descriptor(self) -> PluginDescriptor:
            return PluginDescriptor(self.plugin_id, "1.0.0", "1.0", (), ("shared.capability",))

        def bind(self, _ports: EnginePorts) -> PluginRuntime:
            return PluginRuntime({}, {"shared.capability": _accept})

    with pytest.raises(CapabilityRegistryError, match="duplicate commit validator"):
        assemble_registry((Handler("toy.one"), Validator("toy.two")))


@pytest.mark.parametrize("kind", ["task_handlers", "commit_validators"])
def test_duplicate_capability_inside_descriptor_is_rejected(kind: str) -> None:
    class DuplicateDescriptor(Provider):
        def descriptor(self) -> PluginDescriptor:
            values = ("toy.one.capability", "toy.one.capability")
            return PluginDescriptor(
                self.plugin_id,
                "1.0.0",
                "1.0",
                values if kind == "task_handlers" else (),
                values if kind == "commit_validators" else (),
            )

        def bind(self, _ports: EnginePorts) -> PluginRuntime:
            if kind == "task_handlers":
                return PluginRuntime({"toy.one.capability": _ping}, {})
            return PluginRuntime({}, {"toy.one.capability": _accept})

    with pytest.raises(CapabilityRegistryError, match="duplicate declared"):
        assemble_registry((DuplicateDescriptor("toy.one"),))


def test_validator_descriptor_and_runtime_must_match() -> None:
    class BrokenValidator(Provider):
        def descriptor(self) -> PluginDescriptor:
            descriptor = super().descriptor()
            return PluginDescriptor(
                descriptor.plugin_id,
                descriptor.plugin_version,
                descriptor.engine_api,
                descriptor.task_handlers,
                ("toy.missing.validate",),
            )

    with pytest.raises(CapabilityRegistryError, match="declared and bound commit validators differ"):
        assemble_registry((BrokenValidator("toy.one"),))
