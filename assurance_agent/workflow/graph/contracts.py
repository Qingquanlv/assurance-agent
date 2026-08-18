"""execution contract registry：资源 claim 正规化与确定性冲突判定。

contract 声明 skill/operation/builtin target 的默认 reads/writes/exclusive 与授权写
范围。``ExecutionContractCatalog.claims_for`` 按「registry 默认 → outputs 派生 write
→ node 补充 concurrency claim / 收窄授权」合成 node 的最终 claim；目标缺失或
write-capable 目标的写范围仍不可推导时返回 ``global:exclusive``，不做猜测。

冲突判定在逻辑 root + path segment 层面保守进行（不用 fnmatch——它不是 sound 的
glob 相交测试）：read/read 可并行；write/read、write/write 与相同 exclusive token
串行；``*`` 匹配一段、``**`` 匹配任意后缀、``global`` root 与一切相交。保守方向的
误判只会增加串行，不会放行冲突。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from assurance_agent import resources
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.graph.schema_v2 import NodeDef

RootName = Literal["change", "project", "repo", "global"]

_ROOTS = frozenset({"change", "project", "repo", "global"})
_TEMPLATE = re.compile(r"\$\{([^}]+)\}")
_CONTRACTS_ROOT_KEYS = frozenset({"schema_version", "contracts"})


class ContractError(AaError):
    """execution contract registry 非法，或 resource claim 无法安全推导。"""


def _assert_safe_pattern(pattern: str) -> None:
    segments = pattern.split("/")
    if (
        not pattern
        or pattern.startswith("/")
        or "\\" in pattern
        or any(seg in ("", ".", "..") for seg in segments)
        or any("**" in seg and seg != "**" for seg in segments)
    ):
        raise ContractError(f"unsafe resource pattern: {pattern}")


@dataclass(frozen=True)
class ResourcePath:
    root: RootName
    pattern: str

    @classmethod
    def parse(cls, value: str) -> "ResourcePath":
        root, separator, pattern = value.partition(":")
        if not separator or root not in {"change", "project", "repo", "global"}:
            raise ContractError(f"invalid resource root: {value}")
        if pattern.endswith("/"):
            pattern = pattern[:-1]  # 目录 claim 允许结尾 "/"
        _assert_safe_pattern(pattern)
        return cls(root=cast(RootName, root), pattern=pattern)

    @property
    def segments(self) -> tuple[str, ...]:
        return tuple(self.pattern.split("/"))


@dataclass(frozen=True)
class ResourceClaims:
    reads: tuple[ResourcePath, ...] = ()
    writes: tuple[ResourcePath, ...] = ()
    synchronized: tuple[ResourcePath, ...] = ()
    exclusive: tuple[str, ...] = ()
    authorization_writes: tuple[ResourcePath, ...] = ()

    def union(self, other: "ResourceClaims") -> "ResourceClaims":
        """逐项并集；exclusive token 去重且保持声明序（确定性）。"""
        return ResourceClaims(
            reads=(*self.reads, *other.reads),
            writes=(*self.writes, *other.writes),
            synchronized=(*self.synchronized, *other.synchronized),
            exclusive=tuple(dict.fromkeys((*self.exclusive, *other.exclusive))),
            authorization_writes=(*self.authorization_writes, *other.authorization_writes),
        )


def unknown_claims() -> ResourceClaims:
    """目标缺失或写范围不可推导时的保守 claim：``global:exclusive``。"""
    return ResourceClaims(exclusive=("global:exclusive",))


def normalize_claim_pattern(value: str) -> str:
    """``${...}`` 模板变量在静态 claim 阶段保守按单段 ``*`` 处理。"""
    return _TEMPLATE.sub("*", value)


# ---------------------------------------------------------------------------
# 保守 path 相交与覆盖


def _segment_may_intersect(left: str, right: str) -> bool:
    if left == right or left == "*" or right == "*":
        return True
    # 部分 glob（如 api-*.md）不做精确相交证明，保守视为相交——只会多串行。
    return "*" in left or "*" in right


def _segments_intersect(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    if not left:
        return all(seg == "**" for seg in right)  # ** 可匹配零段
    if not right:
        return all(seg == "**" for seg in left)
    left_head, right_head = left[0], right[0]
    if left_head == "**":
        return _segments_intersect(left[1:], right) or _segments_intersect(left, right[1:])
    if right_head == "**":
        return _segments_intersect(left, right[1:]) or _segments_intersect(left[1:], right)
    return _segment_may_intersect(left_head, right_head) and _segments_intersect(left[1:], right[1:])


def paths_intersect(left: ResourcePath, right: ResourcePath) -> bool:
    """root 不同则不相交（``global`` 与一切相交）；否则按 segment 保守判定。"""
    if left.root != right.root and left.root != "global" and right.root != "global":
        return False
    return _segments_intersect(left.segments, right.segments)


def claims_conflict(left: ResourceClaims, right: ResourceClaims) -> bool:
    """read/read 可并行；write/read、write/write 与相同 exclusive token 串行。"""
    for write in left.writes:
        if any(paths_intersect(write, other) for other in (*right.reads, *right.writes)):
            return True
    for write in right.writes:
        if any(paths_intersect(write, read) for read in left.reads):
            return True
    return bool(set(left.exclusive) & set(right.exclusive))


def _segment_covers(authorization: str, claim: str) -> bool:
    # 只有整段 * 或逐字相同才能「证明」覆盖；部分 glob 无法证明时 fail closed。
    return authorization == "*" or authorization == claim


def _segments_cover(authorization: tuple[str, ...], claim: tuple[str, ...]) -> bool:
    if not authorization:
        return not claim
    head, rest = authorization[0], authorization[1:]
    if head == "**":
        return _segments_cover(rest, claim) or (bool(claim) and _segments_cover(authorization, claim[1:]))
    if not claim or claim[0] == "**":
        return False  # claim 的 ** 只能由 authorization 的 ** 覆盖
    return _segment_covers(head, claim[0]) and _segments_cover(rest, claim[1:])


def path_covers(authorization: ResourcePath, claim: ResourcePath) -> bool:
    """authorization 的写范围保守覆盖 claim（node resources 收窄校验用）。"""
    if authorization.root != claim.root and authorization.root != "global":
        return False
    return _segments_cover(authorization.segments, claim.segments)


def narrow_claims(
    base: ResourceClaims,
    *,
    reads: tuple[ResourcePath, ...],
    writes: tuple[ResourcePath, ...],
    outputs: tuple[ResourcePath, ...],
    synchronized: tuple[ResourcePath, ...] | None = None,
) -> ResourceClaims:
    """Replace broad static claims with concrete expanded current-run paths.

    Every expanded read must be covered by ``base.reads``; every expanded write
    or output must be covered by ``base.authorization_writes``. Synchronized and
    exclusive tokens are preserved from the static contract.
    """
    if not all(any(path_covers(bound, item) for bound in base.reads) for item in reads):
        raise ContractError("expanded read exceeds the static execution contract")
    requested_writes = (*writes, *outputs)
    if not all(
        any(path_covers(bound, item) for bound in base.authorization_writes) for item in requested_writes
    ):
        raise ContractError("expanded write exceeds the static execution contract")
    concrete_synchronized = base.synchronized if synchronized is None else synchronized
    if not all(
        any(path_covers(bound, item) for bound in base.synchronized) for item in concrete_synchronized
    ):
        raise ContractError("expanded synchronized path exceeds the static execution contract")
    concrete_writes = tuple(dict.fromkeys((*writes, *outputs)))
    return ResourceClaims(
        reads=reads,
        writes=concrete_writes,
        synchronized=concrete_synchronized,
        exclusive=base.exclusive,
        authorization_writes=concrete_writes,
    )


# ---------------------------------------------------------------------------
# contract 模型与 catalog


class ExecutionContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target: str
    handler: Literal["agent", "operation", "builtin"]
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    synchronized: tuple[str, ...] = ()
    exclusive: tuple[str, ...] = ()
    authorization_writes: tuple[str, ...] = ()
    retryable_errors: tuple[ErrorKind, ...] = ()
    side_effect_free: bool = False
    reconnect: bool = False
    read_isolation: Literal["declared_only"] | None = None
    precommit_validator: str | None = None
    durable_effects: tuple[str, ...] = ()


class ExecutionContractCatalog(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    contracts: dict[str, ExecutionContract]

    def claims_for(self, node: NodeDef) -> ResourceClaims:
        """合成 registry 默认、outputs 派生 write 与 node 补充的 concurrency claim。

        目标缺失，或 write-capable 目标最终写范围仍不可推导（无 writes、无
        authorization、无 outputs）时，返回 ``global:exclusive`` 而不是猜测。
        """
        contract = self.contracts.get(node.uses)
        if contract is None:
            return unknown_claims()
        declared = node.resources
        reads = [ResourcePath.parse(v) for v in contract.reads]
        writes = [ResourcePath.parse(v) for v in contract.writes]
        synchronized = tuple(ResourcePath.parse(v) for v in contract.synchronized)
        output_paths = tuple(ResourcePath.parse(normalize_claim_pattern(o)) for o in node.outputs)
        writes.extend(output_paths)
        exclusive = list(contract.exclusive)
        if declared is not None:
            reads.extend(ResourcePath.parse(normalize_claim_pattern(v)) for v in declared.reads)
            writes.extend(ResourcePath.parse(normalize_claim_pattern(v)) for v in declared.writes)
            exclusive.extend(declared.exclusive)
        if declared is not None and declared.writes:
            # node resources.writes 收窄授权（compiler 已校验其不超出 contract 授权）。
            authorization = tuple(ResourcePath.parse(normalize_claim_pattern(v)) for v in declared.writes)
        else:
            authorization = tuple(ResourcePath.parse(v) for v in contract.authorization_writes)
        # Declared outputs are always authorized — freeze requires the agent to write them,
        # and mid-segment globs like ``api-*.md`` are not provable by path_covers.
        authorization = tuple(dict.fromkeys((*authorization, *output_paths)))
        if not contract.side_effect_free and not writes and not authorization:
            return unknown_claims()
        return ResourceClaims(
            reads=tuple(reads),
            writes=tuple(writes),
            synchronized=synchronized,
            exclusive=tuple(dict.fromkeys(exclusive)),
            authorization_writes=authorization,
        )


def _validate_catalog_paths(catalog: ExecutionContractCatalog) -> None:
    """registry 在加载期完成路径安全校验，之后的冲突/授权判定不再接触未审字符串。"""
    for key, contract in catalog.contracts.items():
        try:
            for value in (
                *contract.reads,
                *contract.writes,
                *contract.synchronized,
                *contract.authorization_writes,
            ):
                ResourcePath.parse(value)
        except ContractError as exc:
            raise ContractError(f"contract '{key}': {exc}") from exc
        if any(not token.strip() for token in contract.exclusive):
            raise ContractError(f"contract '{key}' has empty exclusive token")
        synchronized = tuple(ResourcePath.parse(value) for value in contract.synchronized)
        if synchronized and not any(
            token.startswith("project:") and token.removeprefix("project:").strip()
            for token in contract.exclusive
        ):
            raise ContractError(f"contract '{key}' synchronized paths require a project exclusive token")
        reads = tuple(ResourcePath.parse(value) for value in contract.reads)
        writes = tuple(ResourcePath.parse(value) for value in contract.writes)
        authorization = tuple(ResourcePath.parse(value) for value in contract.authorization_writes)
        for raw_path, path in zip(contract.synchronized, synchronized, strict=True):
            if path.root != "project":
                raise ContractError(
                    f"contract '{key}' synchronized path must use the project root: {path.pattern}"
                )
            wildcard_segments = [segment for segment in path.segments if "*" in segment]
            if raw_path.endswith("/") or (
                wildcard_segments and not (wildcard_segments == ["**"] and path.segments[-1] == "**")
            ):
                raise ContractError(
                    f"contract '{key}' synchronized path must be a concrete file or directory prefix: "
                    f"{path.pattern}"
                )
            if not any(path_covers(bound, path) for bound in (*reads, *writes)):
                raise ContractError(
                    f"contract '{key}' synchronized path must be covered by reads or writes: {path.pattern}"
                )
            can_write = any(paths_intersect(path, claim) for claim in (*writes, *authorization))
            if can_write and not any(path_covers(write, path) for write in writes):
                raise ContractError(
                    f"contract '{key}' writable synchronized path must be covered by writes: {path.pattern}"
                )
            # Authorization may fully cover the synchronized prefix, or refine it to
            # concrete ledger/delivery paths under that prefix (least privilege).
            auth_covers_sync = any(path_covers(auth, path) for auth in authorization)
            auth_refines_sync = any(path_covers(path, auth) for auth in authorization)
            if can_write and not (auth_covers_sync or auth_refines_sync):
                raise ContractError(
                    f"contract '{key}' writable synchronized path must be covered by "
                    f"authorization_writes: {path.pattern}"
                )


def parse_execution_contracts(
    yaml_text: str,
    *,
    effect_registry: object | None = None,
) -> ExecutionContractCatalog:
    try:
        doc = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise ContractError(f"invalid execution contracts: {exc}") from exc
    if not isinstance(doc, dict):
        raise ContractError("contracts root is not a mapping")

    version = doc.get("schema_version")
    if not isinstance(version, str) or version != "1":
        raise ContractError('execution contracts schema_version must be exactly "1"')

    unknown = sorted(str(k) for k in doc if k not in _CONTRACTS_ROOT_KEYS)
    if unknown:
        raise ContractError("unknown root keys: " + ", ".join(unknown))

    raw = doc.get("contracts")
    if not isinstance(raw, dict):
        raise ContractError("'contracts' must be a mapping")
    contracts: dict[str, ExecutionContract] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            raise ContractError(f"contract '{key}' must be a mapping")
        declared = entry.get("target")
        if declared is not None and declared != key:
            raise ContractError(f"contract '{key}' target mismatch: {declared}")
        try:
            contracts[str(key)] = ExecutionContract.model_validate({**entry, "target": str(key)})
        except ValidationError as exc:
            raise ContractError(f"invalid execution contracts: {exc}") from exc
    catalog = ExecutionContractCatalog(contracts=contracts)
    _validate_catalog_paths(catalog)
    _validate_catalog_precommit_validators(catalog)
    _validate_catalog_durable_effects(catalog, effect_registry=effect_registry)
    return catalog


def load_execution_contracts(project_root: Path, explicit: Path | None = None) -> ExecutionContractCatalog:
    """与 schema loader 同序：显式路径 → ``.aa/`` → ``schemas/`` → 打包资源。"""
    if explicit is not None:
        path = explicit if explicit.is_absolute() else project_root / explicit
        if not path.exists():
            raise ContractError(f"explicit execution contracts not found: {path}")
        return parse_execution_contracts(path.read_text(encoding="utf-8"))
    for rel in (Path(".aa") / "execution-contracts.yaml", Path("schemas") / "execution-contracts.yaml"):
        candidate = project_root / rel
        if candidate.exists():
            return parse_execution_contracts(candidate.read_text(encoding="utf-8"))
    return parse_execution_contracts(resources.read_text("schemas", "execution-contracts.yaml"))


def catalog_from_pinned_contracts(
    contracts: Sequence[ExecutionContract],
    *,
    effect_registry: object | None = None,
) -> ExecutionContractCatalog:
    """Validate target uniqueness and resource path safety."""
    mapping: dict[str, ExecutionContract] = {}
    for contract in contracts:
        if contract.target in mapping:
            raise ContractError(f"duplicate pinned contract target: {contract.target}")
        mapping[contract.target] = contract
    catalog = ExecutionContractCatalog(contracts=mapping)
    _validate_catalog_paths(catalog)
    _validate_catalog_precommit_validators(catalog)
    _validate_catalog_durable_effects(catalog, effect_registry=effect_registry)
    return catalog


def _validate_catalog_precommit_validators(catalog: ExecutionContractCatalog) -> None:
    from assurance_agent.workflow.graph.precommit import (
        CandidateValidationError,
        validate_precommit_validator_id,
    )

    for key, contract in catalog.contracts.items():
        try:
            validate_precommit_validator_id(contract.precommit_validator)
        except CandidateValidationError as exc:
            raise ContractError(f"contract '{key}': {exc}") from exc


def _validate_catalog_durable_effects(
    catalog: ExecutionContractCatalog,
    *,
    effect_registry: object | None = None,
) -> None:
    from assurance_agent.workflow.graph.durable_effects import (
        DurableEffectValidationError,
        EffectRegistry,
        validate_durable_effect_kinds,
    )

    registry = effect_registry if isinstance(effect_registry, EffectRegistry) else None
    for key, contract in catalog.contracts.items():
        try:
            validate_durable_effect_kinds(contract.durable_effects, registry=registry)
        except DurableEffectValidationError as exc:
            raise ContractError(f"contract '{key}': {exc}") from exc
