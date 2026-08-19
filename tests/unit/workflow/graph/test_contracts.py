import textwrap
from pathlib import Path

import pytest

from assurance_agent.workflow.graph.compiler import (
    CompileError,
    canonical_digest,
    compile_workflow,
)
from assurance_agent.workflow.graph.contracts import (
    ContractError,
    ExecutionContract,
    ExecutionContractCatalog,
    ResourceClaims,
    ResourcePath,
    catalog_from_pinned_contracts,
    claims_conflict,
    load_execution_contracts,
    parse_execution_contracts,
    path_covers,
)
from assurance_agent.workflow.graph.schema_v2 import (
    NodeDef,
    ResourceDef,
    parse_workflow_v2,
)


def test_read_read_does_not_conflict() -> None:
    left = ResourceClaims(reads=(ResourcePath.parse("repo:tests/api/**"),))
    right = ResourceClaims(reads=(ResourcePath.parse("repo:tests/api/test_users.py"),))
    assert claims_conflict(left, right) is False


def test_write_read_and_write_write_conflict() -> None:
    writer = ResourceClaims(writes=(ResourcePath.parse("repo:tests/api/**"),))
    reader = ResourceClaims(reads=(ResourcePath.parse("repo:tests/api/test_users.py"),))
    other_writer = ResourceClaims(writes=(ResourcePath.parse("repo:tests/api/test_roles.py"),))
    assert claims_conflict(writer, reader) is True
    assert claims_conflict(writer, other_writer) is True


def test_disjoint_api_and_e2e_writes_do_not_conflict() -> None:
    api = ResourceClaims(writes=(ResourcePath.parse("repo:tests/api/**"),))
    e2e = ResourceClaims(writes=(ResourcePath.parse("repo:tests/e2e/**"),))
    assert claims_conflict(api, e2e) is False


def test_equal_exclusive_token_conflicts() -> None:
    left = ResourceClaims(exclusive=("repo:test-runtime",))
    right = ResourceClaims(exclusive=("repo:test-runtime",))
    assert claims_conflict(left, right) is True


def test_distinct_exclusive_tokens_do_not_conflict() -> None:
    left = ResourceClaims(exclusive=("repo:test-runtime",))
    right = ResourceClaims(exclusive=("repo:test-infra",))
    assert claims_conflict(left, right) is False


# ---------------------------------------------------------------------------
# ResourcePath 正规化


def test_parse_valid_resource_paths() -> None:
    path = ResourcePath.parse("repo:tests/api/**")
    assert path.root == "repo"
    assert path.pattern == "tests/api/**"
    glob_in_segment = ResourcePath.parse("change:plans/api-*.md")
    assert glob_in_segment.root == "change"
    directory = ResourcePath.parse("change:reports/")
    assert directory.pattern == "reports"  # 目录 claim 允许结尾 "/"


@pytest.mark.parametrize(
    "value",
    [
        "ftp:tests/api/x.py",  # 未知 root
        "tests/api/x.py",  # 缺少 root 分隔符
        ":tests/api/x.py",  # 空 root
    ],
)
def test_parse_rejects_unknown_roots(value: str) -> None:
    with pytest.raises(ContractError, match="invalid resource root"):
        ResourcePath.parse(value)


@pytest.mark.parametrize(
    "value",
    [
        "repo:/abs/path",  # 绝对路径
        "repo:tests\\api",  # 反斜杠
        "repo:tests/../escape",  # ..
        "repo:**/..",  # ..
        "repo:tests//api",  # 空 segment
        "repo:.",  # 当前目录
        "repo:",  # 空 pattern
        "repo:tests/a**",  # 部分 ** segment
        "repo:te**sts/api",  # 部分 ** segment
    ],
)
def test_parse_rejects_unsafe_patterns(value: str) -> None:
    with pytest.raises(ContractError, match="unsafe resource pattern"):
        ResourcePath.parse(value)


# ---------------------------------------------------------------------------
# 保守 path 相交（parent dirs / file-vs-glob / ** / global）


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("repo:tests/**", "repo:tests/api/test_users.py", True),  # 父目录 glob 覆盖子文件
        ("repo:tests/api/*", "repo:tests/api/test_users.py", True),  # file-vs-glob
        ("repo:tests/api/*", "repo:tests/e2e/test_users.py", False),  # * 只匹配一段
        ("repo:a/**", "repo:a/b/c/d.py", True),  # ** 匹配任意后缀
        ("repo:a/b/**", "repo:a/x/y.py", False),
        ("repo:a/x.py", "repo:a/x.py", True),  # 同一字面文件
        ("repo:a/x.py", "repo:a/y.py", False),  # 不同字面文件
        ("repo:tests/**", "change:tests/**", False),  # 不同 root 不相交
        ("global:**", "repo:tests/api/x.py", True),  # global root 与一切相交
        ("global:tmp/scratch", "project:tmp/scratch", True),
    ],
)
def test_write_write_intersection(left: str, right: str, expected: bool) -> None:
    left_claims = ResourceClaims(writes=(ResourcePath.parse(left),))
    right_claims = ResourceClaims(writes=(ResourcePath.parse(right),))
    assert claims_conflict(left_claims, right_claims) is expected
    assert claims_conflict(right_claims, left_claims) is expected  # 对称


def test_global_exclusive_fallback_serializes_unknown_tasks() -> None:
    unknown_a = ResourceClaims(exclusive=("global:exclusive",))
    unknown_b = ResourceClaims(exclusive=("global:exclusive",))
    assert claims_conflict(unknown_a, unknown_b) is True


# ---------------------------------------------------------------------------
# registry 加载（项目本地优先于打包资源，与 schema loader 同序）


def test_loads_packaged_registry(tmp_path: Path) -> None:
    catalog = load_execution_contracts(tmp_path)
    assert "operation:no-op" in catalog.contracts
    assert catalog.contracts["builtin:join"].side_effect_free is True


def test_run_tests_contract_reads_traceability_inputs() -> None:
    catalog = load_execution_contracts(Path.cwd())
    contract = catalog.contracts["operation:run-tests"]
    assert "change:cases/**" in contract.reads
    assert "change:execution/**" in contract.reads
    assert "project:.aa/policy.yaml" in contract.reads


@pytest.mark.parametrize("layer", ["api", "e2e"])
def test_codegen_fixer_contract_reads_its_codegen_plan(layer: str) -> None:
    catalog = load_execution_contracts(Path.cwd())
    contract = catalog.contracts[f"skill:aa-{layer}-codegen-fixer"]
    assert f"change:plans/{layer}-codegen-plan.md" in contract.reads
    assert "change:cases/**/case.yaml" in contract.reads


def test_derive_plan_layer_applicability_contract_reads_only_cases() -> None:
    catalog = load_execution_contracts(Path.cwd())
    contract = catalog.contracts["operation:derive-plan-layer-applicability"]
    assert contract.reads == ("change:cases/**/case.yaml",)
    assert contract.writes == (
        "change:review/api-plan-checks.json",
        "change:review/e2e-plan-checks.json",
        "change:review/fuzz-plan-checks.json",
        "change:review/performance-plan-checks.json",
    )
    assert contract.authorization_writes == contract.writes
    assert contract.synchronized == ()
    assert contract.side_effect_free is False
    reads_prefixes = {read.split("/", 1)[0].split(":", 1)[-1] for read in contract.reads}
    assert "plans" not in reads_prefixes
    assert "review" not in reads_prefixes


def test_materialize_trace_projection_contract_reads_full_surface() -> None:
    catalog = load_execution_contracts(Path.cwd())
    contract = catalog.contracts["operation:materialize-trace-projection"]
    assert contract.side_effect_free is False
    assert set(contract.reads) == {
        "change:cases/**",
        "change:execution/**",
        "change:facts/**",
        "change:review/**",
        "change:healing/**",
        "change:codegen/**",
        "change:inspect/failure-analysis.json",
        "change:inspect/issue-evidence-manifest.json",
        "change:inspect/observations.json",
        "change:inspect/issue-candidates.json",
        "change:inspect/issue-reconcile-status.json",
        "change:issues/events.jsonl",
        "change:issues/snapshot.json",
        "project:qa/issues/events.jsonl",
        "project:qa/issues/problems.json",
        "repo:tests/**",
    }
    # Feature-branch schema still materializes both projection and sufficiency.
    assert contract.writes == (
        "change:inspect/trace-projection.json",
        "change:inspect/trace-sufficiency.json",
    )
    assert contract.authorization_writes == contract.writes


def test_record_project_sync_pending_contract_writes_reconcile_status() -> None:
    catalog = load_execution_contracts(Path.cwd())
    contract = catalog.contracts["operation:record-project-sync-pending"]
    assert set(contract.reads) == {
        "change:inspect/issue-evidence-manifest.json",
        "change:inspect/issue-candidates.json",
    }
    assert set(contract.writes) == {
        "change:issues/events.jsonl",
        "change:issues/snapshot.json",
        "change:inspect/issue-reconcile-status.json",
    }
    assert set(contract.authorization_writes) == set(contract.writes)


def test_project_local_registry_overrides_packaged(tmp_path: Path) -> None:
    local = tmp_path / ".aa" / "execution-contracts.yaml"
    local.parent.mkdir(parents=True)
    local.write_text(
        'schema_version: "1"\ncontracts:\n  operation:custom:\n    handler: operation\n    side_effect_free: true\n',
        encoding="utf-8",
    )
    catalog = load_execution_contracts(tmp_path)
    assert "operation:custom" in catalog.contracts
    assert "operation:no-op" not in catalog.contracts  # 项目本地整体取代打包默认


def test_schemas_directory_registry_overrides_packaged_when_aa_absent(tmp_path: Path) -> None:
    local = tmp_path / "schemas" / "execution-contracts.yaml"
    local.parent.mkdir(parents=True)
    local.write_text(
        'schema_version: "1"\ncontracts:\n  operation:from-schemas:\n    handler: operation\n    side_effect_free: true\n',
        encoding="utf-8",
    )
    catalog = load_execution_contracts(tmp_path)
    assert "operation:from-schemas" in catalog.contracts
    assert "operation:no-op" not in catalog.contracts


def test_aa_registry_wins_over_schemas_directory(tmp_path: Path) -> None:
    (tmp_path / ".aa").mkdir()
    (tmp_path / "schemas").mkdir()
    (tmp_path / ".aa" / "execution-contracts.yaml").write_text(
        'schema_version: "1"\ncontracts:\n  operation:from-aa:\n    handler: operation\n    side_effect_free: true\n',
        encoding="utf-8",
    )
    (tmp_path / "schemas" / "execution-contracts.yaml").write_text(
        'schema_version: "1"\ncontracts:\n  operation:from-schemas:\n    handler: operation\n    side_effect_free: true\n',
        encoding="utf-8",
    )
    catalog = load_execution_contracts(tmp_path)
    assert "operation:from-aa" in catalog.contracts
    assert "operation:from-schemas" not in catalog.contracts


def test_explicit_registry_path(tmp_path: Path) -> None:
    explicit = tmp_path / "custom" / "contracts.yaml"
    explicit.parent.mkdir(parents=True)
    explicit.write_text(
        'schema_version: "1"\ncontracts:\n  operation:custom:\n    handler: operation\n',
        encoding="utf-8",
    )
    assert "operation:custom" in load_execution_contracts(tmp_path, explicit).contracts
    relative = load_execution_contracts(tmp_path, Path("custom/contracts.yaml"))
    assert "operation:custom" in relative.contracts
    with pytest.raises(ContractError, match="not found"):
        load_execution_contracts(tmp_path, tmp_path / "missing.yaml")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("schema_version: 1\ncontracts: {}\n", 'must be exactly "1"'),  # YAML int 不接受
        ('schema_version: "2"\ncontracts: {}\n', 'must be exactly "1"'),
        ('schema_version: "1"\ncontracts: []\n', "must be a mapping"),
        ('schema_version: "1"\nunknown: 1\ncontracts: {}\n', "unknown root keys"),
        (
            'schema_version: "1"\ncontracts:\n  operation:x:\n    handler: operation\n    writes: ["repo:../x"]\n',
            "unsafe resource pattern",
        ),
        (
            'schema_version: "1"\ncontracts:\n  operation:x:\n    handler: operation\n    reads: ["ftp:x"]\n',
            "invalid resource root",
        ),
        (
            'schema_version: "1"\ncontracts:\n  operation:x:\n    handler: operation\n    target: operation:y\n',
            "target mismatch",
        ),
        (
            'schema_version: "1"\ncontracts:\n  operation:x:\n    handler: operation\n    bogus: 1\n',
            "invalid execution contracts",
        ),
    ],
)
def test_parse_rejects_invalid_registries(text: str, message: str) -> None:
    with pytest.raises(ContractError, match=message):
        parse_execution_contracts(text)


# ---------------------------------------------------------------------------
# synchronized project resources


def test_synchronized_project_path_parses_and_propagates_to_claims() -> None:
    catalog = parse_execution_contracts(
        """schema_version: "1"
contracts:
  operation:update-issues:
    handler: operation
    reads: [project:qa/issues/**]
    writes: [project:qa/issues/**]
    authorization_writes: [project:qa/issues/**]
    synchronized: [project:qa/issues/**]
    exclusive: [project:issue-registry]
"""
    )

    claims = catalog.claims_for(NodeDef(uses="operation:update-issues"))

    assert catalog.contracts["operation:update-issues"].synchronized == ("project:qa/issues/**",)
    assert claims.synchronized == (ResourcePath.parse("project:qa/issues/**"),)
    ordinary_writer = ResourceClaims(writes=(ResourcePath.parse("project:qa/issues/ISSUE-1.yaml"),))
    assert claims_conflict(claims, ordinary_writer) is True


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "reads: [repo:qa/issues/**]\n"
            "    synchronized: [repo:qa/issues/**]\n"
            "    exclusive: [project:issue-registry]",
            "project root",
        ),
        (
            "reads: [project:qa/problems/**]\n"
            "    synchronized: [project:qa/issues/**]\n"
            "    exclusive: [project:issue-registry]",
            "covered by reads",
        ),
        (
            "reads: [project:qa/issues/**]\n"
            "    authorization_writes: [project:qa/issues/**]\n"
            "    synchronized: [project:qa/issues/**]\n"
            "    exclusive: [project:issue-registry]",
            "covered by writes",
        ),
        (
            "reads: [project:qa/issues/**]\n"
            "    writes: [project:qa/issues/**]\n"
            "    synchronized: [project:qa/issues/**]\n"
            "    exclusive: [project:issue-registry]",
            "authorization_writes",
        ),
        (
            "reads: [project:qa/*]\n"
            "    synchronized: [project:qa/*]\n"
            "    exclusive: [project:issue-registry]",
            "concrete file or directory prefix",
        ),
        (
            "reads: [project:qa/issues/**]\n"
            "    synchronized: [project:qa/issues/**]\n"
            "    exclusive: [repo:issue-registry]",
            "project exclusive token",
        ),
        (
            "reads: [project:qa/issues/**]\n"
            "    synchronized: [project:qa/issues/**]\n"
            '    exclusive: ["project:"]',
            "project exclusive token",
        ),
        (
            "reads: [project:qa/issues/]\n"
            "    synchronized: [project:qa/issues/]\n"
            "    exclusive: [project:issue-registry]",
            "directory prefix",
        ),
    ],
)
def test_synchronized_project_path_contract_invariants(body: str, message: str) -> None:
    text = (
        f'schema_version: "1"\ncontracts:\n  operation:update-issues:\n    handler: operation\n    {body}\n'
    )
    with pytest.raises(ContractError, match=message):
        parse_execution_contracts(text)


# ---------------------------------------------------------------------------
# claims_for 合成


def _claims_catalog() -> ExecutionContractCatalog:
    return parse_execution_contracts(
        'schema_version: "1"\n'
        "contracts:\n"
        "  skill:codegen:\n"
        "    handler: agent\n"
        "    reads: [change:plans/api-*.md]\n"
        "    writes: [repo:tests/api/**]\n"
        "    exclusive: [repo:test-infra]\n"
        "    authorization_writes: [repo:tests/**, change:codegen/**]\n"
        "    retryable_errors: [timeout, transport]\n"
        "  operation:opaque:\n"
        "    handler: operation\n"
        "  builtin:join:\n"
        "    handler: builtin\n"
        "    side_effect_free: true\n"
    )


def test_claims_for_unknown_target_gets_global_exclusive() -> None:
    claims = _claims_catalog().claims_for(NodeDef(uses="operation:ghost"))
    assert claims == ResourceClaims(exclusive=("global:exclusive",))


def test_claims_compose_registry_outputs_and_node_resources() -> None:
    node = NodeDef(
        uses="skill:codegen",
        outputs=["change:codegen/${context.change_id}/summary.json"],
        resources=ResourceDef(
            reads=["repo:pyproject.toml"],
            writes=["repo:tests/api/smoke/**"],
            exclusive=["repo:test-runtime"],
        ),
    )
    claims = _claims_catalog().claims_for(node)
    assert ResourcePath.parse("change:plans/api-*.md") in claims.reads
    assert ResourcePath.parse("repo:pyproject.toml") in claims.reads
    assert ResourcePath.parse("repo:tests/api/**") in claims.writes
    # outputs 自动派生 write claim；${...} 模板变量按单段 * 处理
    assert ResourcePath.parse("change:codegen/*/summary.json") in claims.writes
    assert ResourcePath.parse("repo:tests/api/smoke/**") in claims.writes
    assert set(claims.exclusive) == {"repo:test-infra", "repo:test-runtime"}
    # node resources.writes 收窄授权写范围；declared outputs remain authorized.
    assert claims.authorization_writes == (
        ResourcePath.parse("repo:tests/api/smoke/**"),
        ResourcePath.parse("change:codegen/*/summary.json"),
    )


def test_claims_for_write_capable_contract_without_scope_is_unknown() -> None:
    claims = _claims_catalog().claims_for(NodeDef(uses="operation:opaque"))
    assert claims == ResourceClaims(exclusive=("global:exclusive",))


def test_claims_for_side_effect_free_without_scope_is_empty() -> None:
    claims = _claims_catalog().claims_for(NodeDef(uses="builtin:join"))
    assert claims == ResourceClaims()


# ---------------------------------------------------------------------------
# compiler 的 contract-aware 校验与 footprint

_HEADER = """\
params:
  run_mode: {type: enum, values: [full], default: full}
entrypoints:
  full: {graph: main}
policies:
  retry:
    agent-transient: {max_attempts: 3, retry_on: [timeout, transport]}
    too-broad: {max_attempts: 2, retry_on: [timeout, internal]}
  timeout: {local: {run_seconds: 60, heartbeat_seconds: 10}}
gates: {}
"""

_COMPILER_CATALOG = """\
schema_version: "1"
contracts:
  skill:codegen:
    handler: agent
    reads: [change:plans/api-*.md]
    writes: [repo:tests/api/**]
    exclusive: [repo:test-infra]
    authorization_writes: [repo:tests/**]
    retryable_errors: [timeout, transport]
  operation:no-op:
    handler: operation
    side_effect_free: true
  operation:shady:
    handler: builtin
  builtin:join:
    handler: builtin
    side_effect_free: true
  builtin:interrupt:
    handler: builtin
    side_effect_free: true
  builtin:gate:
    handler: builtin
    side_effect_free: true
"""


def _wf(graph_body: str) -> str:
    return "name: t\n" + _HEADER + "graphs:\n" + textwrap.indent(textwrap.dedent(graph_body), "  ")


def _compile(graph_body: str, catalog: ExecutionContractCatalog | None = None):
    return compile_workflow(parse_workflow_v2(_wf(graph_body)), catalog)


def _compiler_catalog() -> ExecutionContractCatalog:
    return parse_execution_contracts(_COMPILER_CATALOG)


def test_compiler_rejects_unknown_uses_with_catalog() -> None:
    with pytest.raises(CompileError, match="no execution contract"):
        _compile(
            """
            main:
              max_supersteps: 5
              nodes:
                a: {uses: operation:ghost}
              edges:
                - {from: START, to: a}
                - {from: a, to: END}
            """,
            _compiler_catalog(),
        )


def test_compiler_rejects_agent_node_with_non_skill_target() -> None:
    with pytest.raises(CompileError, match="not skill"):
        _compile(
            """
            main:
              max_supersteps: 5
              nodes:
                a: {uses: operation:no-op, agent: aa-doc-author}
              edges:
                - {from: START, to: a}
                - {from: a, to: END}
            """,
            _compiler_catalog(),
        )


def test_compiler_rejects_contract_handler_mismatch() -> None:
    with pytest.raises(CompileError, match="contract handler"):
        _compile(
            """
            main:
              max_supersteps: 5
              nodes:
                a: {uses: operation:shady}
              edges:
                - {from: START, to: a}
                - {from: a, to: END}
            """,
            _compiler_catalog(),
        )


def test_compiler_rejects_builtin_with_wrong_detail_block() -> None:
    with pytest.raises(CompileError, match="declares no join"):
        _compile(
            """
            main:
              max_supersteps: 5
              nodes:
                a: {uses: builtin:join}
              edges:
                - {from: START, to: a}
                - {from: a, to: END}
            """,
            _compiler_catalog(),
        )


def test_compiler_rejects_resources_expanding_authorization() -> None:
    with pytest.raises(CompileError, match="expands authorization"):
        _compile(
            """
            main:
              max_supersteps: 5
              nodes:
                gen:
                  uses: skill:codegen
                  resources: {writes: [repo:src/**]}
              edges:
                - {from: START, to: gen}
                - {from: gen, to: END}
            """,
            _compiler_catalog(),
        )


def test_compiler_rejects_retry_kinds_outside_contract() -> None:
    with pytest.raises(CompileError, match="not retryable"):
        _compile(
            """
            main:
              max_supersteps: 5
              nodes:
                gen: {uses: skill:codegen, retry: too-broad}
              edges:
                - {from: START, to: gen}
                - {from: gen, to: END}
            """,
            _compiler_catalog(),
        )


def test_compiler_accepts_narrowed_authorization_and_allowed_retry() -> None:
    compiled = _compile(
        """
        main:
          max_supersteps: 5
          nodes:
            gen:
              uses: skill:codegen
              retry: agent-transient
              resources: {writes: [repo:tests/api/smoke/**]}
          edges:
            - {from: START, to: gen}
            - {from: gen, to: END}
        """,
        _compiler_catalog(),
    )
    footprint = compiled.graphs["main"].resource_footprint
    assert ResourcePath.parse("repo:tests/api/smoke/**") in footprint.writes


def test_compiler_populates_contract_digests_and_footprint() -> None:
    catalog = _compiler_catalog()
    graph_body = """
        main:
          max_supersteps: 5
          nodes:
            gen:
              uses: skill:codegen
              agent: aa-doc-author
              outputs: [change:codegen/summary.json]
            join:
              uses: builtin:join
              join: {sources: [gen], mode: all}
          edges:
            - {from: START, to: gen}
            - {from: gen, to: join}
            - {from: join, to: END}
    """
    compiled = _compile(graph_body, catalog)
    assert set(compiled.contract_digests) == {"skill:codegen", "builtin:join"}
    assert compiled.contract_digests["skill:codegen"] == canonical_digest(catalog.contracts["skill:codegen"])
    assert _compile(graph_body, catalog).contract_digests == compiled.contract_digests  # digest 稳定
    footprint = compiled.graphs["main"].resource_footprint
    assert ResourcePath.parse("repo:tests/api/**") in footprint.writes
    assert ResourcePath.parse("change:codegen/summary.json") in footprint.writes
    assert ResourcePath.parse("change:plans/api-*.md") in footprint.reads
    assert "repo:test-infra" in footprint.exclusive


def test_compiler_populates_per_node_resource_claims() -> None:
    catalog = _compiler_catalog()
    compiled = _compile(
        """
        main:
          max_supersteps: 5
          nodes:
            gen:
              uses: skill:codegen
              agent: aa-doc-author
              outputs: [change:codegen/summary.json]
            join:
              uses: builtin:join
              join: {sources: [gen], mode: all}
          edges:
            - {from: START, to: gen}
            - {from: gen, to: join}
            - {from: join, to: END}
        """,
        catalog,
    )
    nodes = compiled.graphs["main"].nodes
    gen_claims = nodes["gen"].resources
    join_claims = nodes["join"].resources
    # 每个 node 携带自己的 contract claim，而不是全图 footprint 并集。
    assert ResourcePath.parse("repo:tests/api/**") in gen_claims.writes
    assert "repo:test-infra" in gen_claims.exclusive
    # side_effect_free 的 join 是空 claim —— 不继承 gen 的 exclusive，可与他人并行。
    assert join_claims == ResourceClaims()
    assert join_claims != compiled.graphs["main"].resource_footprint
    assert claims_conflict(gen_claims, join_claims) is False


def test_compiled_subgraph_node_carries_child_footprint_claims() -> None:
    compiled = _compile(
        """
        main:
          max_supersteps: 5
          nodes:
            call-child: {uses: graph:child}
          edges:
            - {from: START, to: call-child}
            - {from: call-child, to: END}
        child:
          max_supersteps: 5
          nodes:
            gen:
              uses: skill:codegen
              outputs: [change:codegen/summary.json]
          edges:
            - {from: START, to: gen}
            - {from: gen, to: END}
        """,
        _compiler_catalog(),
    )
    call_child = compiled.graphs["main"].nodes["call-child"].resources
    assert call_child == compiled.graphs["child"].resource_footprint


def test_subgraph_footprint_unions_reachable_child_claims() -> None:
    compiled = _compile(
        """
        main:
          max_supersteps: 5
          nodes:
            call-child: {uses: graph:child}
          edges:
            - {from: START, to: call-child}
            - {from: call-child, to: END}
        child:
          max_supersteps: 5
          nodes:
            gen:
              uses: skill:codegen
              outputs: [change:codegen/summary.json]
          edges:
            - {from: START, to: gen}
            - {from: gen, to: END}
        """,
        _compiler_catalog(),
    )
    assert set(compiled.contract_digests) == {"skill:codegen"}  # graph:<id> 不是 contract
    main_footprint = compiled.graphs["main"].resource_footprint
    child_footprint = compiled.graphs["child"].resource_footprint
    assert ResourcePath.parse("repo:tests/api/**") in main_footprint.writes
    assert ResourcePath.parse("change:codegen/summary.json") in main_footprint.writes
    assert "repo:test-infra" in main_footprint.exclusive
    assert main_footprint == child_footprint  # main 的唯一节点就是 child subgraph


def test_compile_without_catalog_keeps_topology_only_behavior() -> None:
    compiled = _compile(
        """
        main:
          max_supersteps: 5
          nodes:
            a: {uses: operation:no-op}
          edges:
            - {from: START, to: a}
            - {from: a, to: END}
        """
    )
    assert compiled.contract_digests == {}
    # 无 catalog 时资源范围不可推导，footprint 保守为 global:exclusive
    assert compiled.graphs["main"].resource_footprint == ResourceClaims(exclusive=("global:exclusive",))


def test_packaged_contracts_compile_minimal_fixture(tmp_path: Path) -> None:
    catalog = load_execution_contracts(tmp_path)  # 无 .aa 覆盖 → 打包 registry
    text = Path("tests/fixtures/workflow-v2-minimal.yaml").read_text(encoding="utf-8")
    compiled = compile_workflow(parse_workflow_v2(text), catalog)
    assert set(compiled.contract_digests) == {"operation:no-op"}
    # no-op 是 side_effect_free，无写范围 → footprint 为空 claims
    assert compiled.graphs["main"].resource_footprint == ResourceClaims()


def test_narrow_claims_rejects_expanded_read_outside_static_bounds() -> None:
    from assurance_agent.workflow.graph.contracts import narrow_claims

    base = ResourceClaims(
        reads=(ResourcePath.parse("project:qa/retro/*/context.json"),),
        writes=(ResourcePath.parse("project:qa/retro/*/proposal-candidates.json"),),
        authorization_writes=(ResourcePath.parse("project:qa/retro/*/proposal-candidates.json"),),
    )
    with pytest.raises(ContractError, match="expanded read exceeds"):
        narrow_claims(
            base,
            reads=(ResourcePath.parse("project:qa/issues/problems.json"),),
            writes=(),
            outputs=(),
        )


def test_run_tests_contract_declares_the_three_reads_the_trace_shadow_added() -> None:
    """`aa run` folds a trace projection before it builds the gate.

    That fold reads the change's case documents and its historical execution
    batches, and the shadow sufficiency evaluation reads the project policy —
    the three claims this asserts were added for. A read the runner performs but
    does not declare is invisible to the scheduler, so a concurrent writer of
    those paths would not be serialized against it. The pre-existing test and
    codegen claims are pinned alongside them only to catch an accidental
    replacement of the set.
    """
    run_tests = load_execution_contracts(Path.cwd()).contracts["operation:run-tests"]

    assert {"change:cases/**", "change:execution/**", "project:.aa/policy.yaml"} <= set(run_tests.reads)
    assert set(run_tests.reads) == {
        "repo:tests/api/**",
        "repo:tests/e2e/**",
        "repo:tests/fuzz/**",
        "repo:tests/perf/**",
        "change:codegen/**",
        "change:cases/**",
        "change:execution/**",
        "project:.aa/policy.yaml",
        # A1/B1 diff base, read to diff and rewritten past a green batch.
        "project:.aa/cache/diff-base/**",
    }
    # Evidence still lands under change:execution/**; the diff base is the one
    # path outside it the runner owns, and it must be authorized or the snapshot
    # freezes as a forbidden_write.
    assert run_tests.writes == ("change:execution/**", "project:.aa/cache/diff-base/**")
    assert run_tests.authorization_writes == (
        "change:execution/**",
        "project:.aa/cache/diff-base/**",
    )
    # The full assurance graph also publishes other synchronized project
    # resources.  Its cumulative freeze therefore requires every project write
    # in the footprint to name its own synchronized prefix; authorization alone
    # is not sufficient.
    assert run_tests.synchronized == ("project:.aa/cache/diff-base/**",)
    assert run_tests.exclusive == ("repo:test-runtime", "project:diff-base-cache")


def test_constraint_coverage_contract_reads_change_cases_for_touched_scope() -> None:
    contract = load_execution_contracts(Path.cwd()).contracts["operation:compute-constraint-coverage"]

    assert "change:cases/**" in contract.reads


def test_coverage_gap_contract_materializes_journey_oracle_evidence() -> None:
    """The A4 weak-oracle feedstock must exist inside the operation sandbox."""
    contract = load_execution_contracts(Path.cwd()).contracts["operation:build-coverage-gap-signals"]

    assert "change:execution/runs/*/journey-coverage.json" in contract.reads


def test_retro_closed_loop_contracts_are_least_privilege() -> None:
    catalog = load_execution_contracts(Path.cwd())
    assert "operation:retro-accept" not in catalog.contracts
    collect = catalog.contracts["operation:retro-collect-v3"]
    agent = catalog.contracts["skill:aa-retro"]
    assemble = catalog.contracts["operation:assemble-retro-context-v3"]
    reconcile = catalog.contracts["operation:reconcile-improvements"]

    assert "project:qa/issues/**" in collect.reads
    assert "project:qa/retro/**" not in collect.reads
    assert collect.writes == ("project:qa/retro/**",)
    assert set(collect.authorization_writes) == {
        "project:qa/retro/*/window.json",
        "project:qa/retro/*/evidence/**",
        "project:qa/retro/*/signals/discovery.json",
        "project:qa/retro/*/signals/coverage_gap.json",
    }
    assert set(collect.synchronized) == {
        "project:qa/archive/**",
        "project:qa/changes/**",
        "project:qa/issues/**",
        "project:qa/eval/**",
        "project:qa/retro/**",
    }
    assert collect.exclusive == ("project:retro-evidence-snapshot",)

    assert set(assemble.authorization_writes) >= {
        "project:qa/retro/*/context.json",
        "project:qa/retro/*/context-agent.json",
    }
    assert set(agent.reads) == {
        "project:qa/retro/*/context.json",
        "project:qa/retro/*/context-agent.json",
    }
    assert "project:qa/issues/**" not in agent.reads
    assert "project:qa/retro/**" not in agent.reads
    assert "project:qa/archive/**" not in agent.reads
    assert set(agent.writes) == {
        "project:qa/retro/*/proposal-candidates.json",
        "project:qa/retro/*/retro-summary.md",
    }
    assert agent.read_isolation == "declared_only"
    for domain in ("issue", "workflow", "eval"):
        analyzer = catalog.contracts[f"skill:aa-retro-{domain}-analysis"]
        assert analyzer.read_isolation == "declared_only"
        assert set(analyzer.reads) == {
            f"project:qa/retro/*/evidence/{domain}-slice.json",
            f"project:qa/retro/*/evidence/agent/{domain}-slice.json",
        }

    assert "project:qa/improvements/**" in reconcile.reads
    assert "project:qa/improvements/**" in reconcile.writes
    assert "project:qa/retro/**" in reconcile.reads
    assert set(reconcile.synchronized) == {
        "project:qa/improvements/**",
        "project:qa/retro/**",
    }
    assert "project:improvement-registry" in reconcile.exclusive
    assert "project:qa/issues/**" not in reconcile.writes
    assert "project:qa/issues/**" not in reconcile.authorization_writes

    load_subject = catalog.contracts["operation:load-review-subject"]
    reviewer = catalog.contracts["skill:aa-improvement-reviewer"]
    assert set(load_subject.reads) == {
        "project:qa/improvements/review-subjects/*",
        "project:qa/improvements/review-subjects/agent/*",
    }
    assert set(reviewer.reads) == {
        "project:qa/improvements/review-subjects/*",
        "project:qa/improvements/review-subjects/agent/*",
    }
    assert reviewer.read_isolation == "declared_only"


def test_coverage_repair_contracts_are_declared_and_least_privilege() -> None:
    """Coverage-repair inner loop contracts (design §8.1)."""
    catalog = load_execution_contracts(Path.cwd())
    targets = {
        "operation:probe-coverage-repair-need",
        "operation:compute-coverage-repair-safety",
        "operation:allocate-coverage-repair-attempt",
        "operation:record-coverage-repair-status",
        "skill:aa-coverage-repair",
    }
    assert targets <= set(catalog.contracts)

    probe = catalog.contracts["operation:probe-coverage-repair-need"]
    assert "repo:.aa/policy.yaml" in probe.reads
    assert all(write.startswith("change:coverage-repair/") for write in probe.writes)
    assert not any(write.startswith("change:inspect/") for write in probe.writes)

    safety = catalog.contracts["operation:compute-coverage-repair-safety"]
    assert not any(read == "change:healing/**" or read.startswith("change:healing/") for read in safety.reads)
    assert "change:coverage-repair/entry-baseline.json" in safety.reads
    assert {"repo:tests/**", "repo:**", "project:.aa/config.yaml"} <= set(safety.reads)

    allocate = catalog.contracts["operation:allocate-coverage-repair-attempt"]
    assert "change:coverage-repair/entry-baseline.json" in allocate.writes
    assert "change:coverage-repair/status.json" in allocate.reads
    assert {"repo:tests/**", "repo:**", "project:.aa/config.yaml"} <= set(allocate.reads)

    skill = catalog.contracts["skill:aa-coverage-repair"]
    assert "change:coverage-repair/entry-baseline.json" in skill.reads
    assert "repo:test-infra" in skill.exclusive
    assert set(skill.writes) == {
        "repo:tests/**",
        "change:coverage-repair/apply-summary.json",
    }
    assert set(skill.authorization_writes) == {
        "repo:tests/**",
        "change:coverage-repair/**",
    }
    assert "change:cases/**" not in skill.writes
    assert "change:cases/**" not in skill.authorization_writes
    assert "project:.aa/**" not in skill.writes
    assert "project:.aa/**" not in skill.authorization_writes

    record = catalog.contracts["operation:record-coverage-repair-status"]
    assert {
        "change:coverage-repair/status.json",
        "change:coverage-repair/brief.json",
    } <= set(record.reads)

    metrics_writers = {
        target
        for target, contract in catalog.contracts.items()
        if "change:inspect/metrics.json" in contract.writes
    }
    assert metrics_writers == {"operation:materialize-pr-metrics"}


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (change / "driver.json").write_text("{}\n", encoding="utf-8")
    (change / "driver.lock").write_text("1\ntoken\n", encoding="utf-8")
    return project


def test_api_fixer_authorization_forbids_checks_write(tmp_path: Path) -> None:
    from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend, WorkspaceError

    project = _make_project(tmp_path)
    change = project / "qa" / "changes" / "CH-1"
    store = TreeStore(change)
    backend = WorkspaceBackend(change)
    base_tree = store.capture(project)
    claims = load_execution_contracts(project).claims_for(
        NodeDef(uses="skill:aa-api-plan", outputs=["change:plans/api-plan.md"])
    )
    workspace = backend.create(task_id="fixer", base_tree_id=base_tree, store=store)
    checks = workspace.change_dir / "review" / "api-plan-checks.json"
    checks.parent.mkdir(parents=True, exist_ok=True)
    checks.write_text('{"status":"pass"}\n', encoding="utf-8")
    with pytest.raises(
        WorkspaceError,
        match=r"forbidden write outside authorization_writes: change:review/api-plan-checks\.json",
    ):
        store.freeze_write_set(workspace, claims=claims)


@pytest.mark.parametrize(
    ("uses", "checks_path"),
    [
        ("skill:aa-e2e-plan", "review/e2e-plan-checks.json"),
    ],
)
def test_e2e_plan_skill_forbids_checks_write(tmp_path: Path, uses: str, checks_path: str) -> None:
    from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend, WorkspaceError

    project = _make_project(tmp_path)
    change = project / "qa" / "changes" / "CH-1"
    store = TreeStore(change)
    backend = WorkspaceBackend(change)
    base_tree = store.capture(project)
    outputs = ["change:review/plan-review.json"] if "reviewer" in uses else ["change:plans/e2e-plan.md"]
    claims = load_execution_contracts(project).claims_for(NodeDef(uses=uses, outputs=outputs))
    workspace = backend.create(task_id="agent", base_tree_id=base_tree, store=store)
    checks = workspace.change_dir / checks_path
    checks.parent.mkdir(parents=True, exist_ok=True)
    checks.write_text('{"status":"pass"}\n', encoding="utf-8")
    with pytest.raises(WorkspaceError, match=r"forbidden write outside authorization_writes"):
        store.freeze_write_set(workspace, claims=claims)


# ---------------------------------------------------------------------------
# Fuzz / Performance exact ownership + mechanical narrowing (Task 3)
# ---------------------------------------------------------------------------


def _path_text(path: ResourcePath) -> str:
    return f"{path.root}:{path.pattern}"


def _covers(pattern: str, claim: str) -> bool:
    return path_covers(ResourcePath.parse(pattern), ResourcePath.parse(claim))


_FUZZ_PLAN_READS = (
    "change:cases/**/case.yaml",
    "change:proposal.md",
    "change:facts/fact-baseline.json",
    "repo:.aa/config.yaml",
    "repo:.aa/data-knowledge.yaml",
    "repo:tests/fuzz/**",
    "repo:tests/testdata/domain/**",
)
_FUZZ_PLAN_WRITES = (
    "change:plans/fuzz-plan.md",
    "change:plans/fuzz-codegen-plan.md",
    "change:plans/fuzz-codegen-mapping.json",
    "change:plans/fuzz-review-summary.md",
)
_FUZZ_REVIEWER_READS = (
    "change:plans/fuzz-plan.md",
    "change:plans/fuzz-codegen-plan.md",
    "change:plans/fuzz-codegen-mapping.json",
    "change:plans/fuzz-review-summary.md",
    "change:review/fuzz-plan-checks.json",
    "change:cases/**/case.yaml",
    "repo:.aa/data-knowledge.yaml",
)
_FUZZ_REVIEWER_WRITES = (
    "change:review/fuzz-plan-review.json",
    "change:review/fuzz-plan-review-summary.md",
    "change:review/fuzz-plan-checks.json",
)
_FUZZ_CODEGEN_READS = (
    "change:plans/fuzz-plan.md",
    "change:plans/fuzz-codegen-plan.md",
    "change:plans/fuzz-codegen-mapping.json",
    "change:plans/fuzz-review-summary.md",
    "change:review/fuzz-plan-review.json",
    "change:review/fuzz-plan-checks.json",
    "change:cases/**/case.yaml",
    "repo:.aa/config.yaml",
    "repo:.aa/data-knowledge.yaml",
    "repo:tests/fuzz/**",
    "repo:tests/testdata/domain/**",
)
_FUZZ_CODEGEN_WRITES = (
    "change:codegen/fuzz-codegen-summary.md",
    "change:codegen/fuzz-generated-files.json",
    "repo:tests/fuzz/**",
    "repo:tests/testdata/**",
)

_PERF_PLAN_READS = (
    "change:cases/**/case.yaml",
    "change:proposal.md",
    "change:facts/fact-baseline.json",
    "repo:.aa/config.yaml",
    "repo:.aa/data-knowledge.yaml",
    "repo:tests/perf/**",
    "repo:tests/testdata/domain/**",
)
_PERF_PLAN_WRITES = (
    "change:plans/performance-plan.md",
    "change:plans/performance-codegen-plan.md",
    "change:plans/performance-codegen-mapping.json",
    "change:plans/performance-review-summary.md",
)
_PERF_REVIEWER_READS = (
    "change:plans/performance-plan.md",
    "change:plans/performance-codegen-plan.md",
    "change:plans/performance-codegen-mapping.json",
    "change:plans/performance-review-summary.md",
    "change:review/performance-plan-checks.json",
    "change:cases/**/case.yaml",
    "repo:.aa/data-knowledge.yaml",
)
_PERF_REVIEWER_WRITES = (
    "change:review/performance-plan-review.json",
    "change:review/performance-plan-review-summary.md",
    "change:review/performance-plan-checks.json",
)
_PERF_CODEGEN_READS = (
    "change:plans/performance-plan.md",
    "change:plans/performance-codegen-plan.md",
    "change:plans/performance-codegen-mapping.json",
    "change:plans/performance-review-summary.md",
    "change:review/performance-plan-review.json",
    "change:review/performance-plan-checks.json",
    "change:cases/**/case.yaml",
    "repo:.aa/config.yaml",
    "repo:.aa/data-knowledge.yaml",
    "repo:tests/perf/**",
    "repo:tests/testdata/domain/**",
)
_PERF_CODEGEN_WRITES = (
    "change:codegen/performance-codegen-summary.md",
    "change:codegen/performance-generated-files.json",
    "repo:tests/perf/**",
    "repo:tests/testdata/**",
)

_SIX_TARGETS = (
    ("skill:aa-fuzz-plan", _FUZZ_PLAN_READS, _FUZZ_PLAN_WRITES),
    ("skill:aa-fuzz-plan-reviewer", _FUZZ_REVIEWER_READS, _FUZZ_REVIEWER_WRITES),
    ("skill:aa-fuzz-codegen", _FUZZ_CODEGEN_READS, _FUZZ_CODEGEN_WRITES),
    ("skill:aa-performance-plan", _PERF_PLAN_READS, _PERF_PLAN_WRITES),
    ("skill:aa-performance-plan-reviewer", _PERF_REVIEWER_READS, _PERF_REVIEWER_WRITES),
    ("skill:aa-performance-codegen", _PERF_CODEGEN_READS, _PERF_CODEGEN_WRITES),
)


def _assert_covered(patterns: tuple[str, ...], claim: str) -> None:
    assert any(_covers(pattern, claim) for pattern in patterns), f"{claim} not covered by {patterns}"


@pytest.mark.parametrize(("target", "reads", "writes"), _SIX_TARGETS)
def test_fuzz_performance_skill_contracts_match_exact_matrix(
    target: str,
    reads: tuple[str, ...],
    writes: tuple[str, ...],
) -> None:
    catalog = load_execution_contracts(Path.cwd())
    contract = catalog.contracts[target]
    assert contract.reads == reads
    assert contract.writes == writes
    assert contract.authorization_writes == writes
    for logical in reads:
        _assert_covered(contract.reads, logical)
    for output in writes:
        _assert_covered(contract.writes, output)
        _assert_covered(contract.authorization_writes, output)
    for forbidden in ("repo:**", "change:plans/**", "change:review/**"):
        assert forbidden not in contract.reads
        assert forbidden not in contract.writes
        assert forbidden not in contract.authorization_writes
    if target.endswith("-codegen"):
        assert contract.exclusive == ("repo:test-infra",)


def test_fuzz_and_performance_reviewers_authorize_checks() -> None:
    catalog = load_execution_contracts(Path.cwd())
    fuzz_reviewer = catalog.contracts["skill:aa-fuzz-plan-reviewer"]
    assert "change:review/fuzz-plan-checks.json" in fuzz_reviewer.authorization_writes
    assert fuzz_reviewer.precommit_validator == "plan_mechanical_candidate/v1"
    performance_reviewer = catalog.contracts["skill:aa-performance-plan-reviewer"]
    assert "change:review/performance-plan-checks.json" in performance_reviewer.authorization_writes
    assert performance_reviewer.precommit_validator == "plan_mechanical_candidate/v1"


def test_catalog_from_pinned_contracts_rebuilds_packaged_targets() -> None:
    packaged = load_execution_contracts(Path.cwd())
    rebuilt = catalog_from_pinned_contracts(tuple(packaged.contracts.values()))
    assert set(rebuilt.contracts) == set(packaged.contracts)
    for target, contract in packaged.contracts.items():
        assert canonical_digest(rebuilt.contracts[target]) == canonical_digest(contract)


def test_catalog_from_pinned_contracts_rejects_duplicate_targets() -> None:
    packaged = load_execution_contracts(Path.cwd())
    first = next(iter(packaged.contracts.values()))
    with pytest.raises(ContractError, match="duplicate"):
        catalog_from_pinned_contracts((first, first))


def test_catalog_from_pinned_contracts_rejects_unsafe_resource_paths() -> None:
    bad = ExecutionContract(
        target="operation:bad",
        handler="operation",
        reads=("change:../escape",),
        writes=(),
        authorization_writes=(),
    )
    with pytest.raises(ContractError, match="unsafe|invalid"):
        catalog_from_pinned_contracts((bad,))


def test_packaged_contracts_select_exact_precommit_validators() -> None:
    catalog = load_execution_contracts(Path.cwd())
    selected = {
        target: contract.precommit_validator
        for target, contract in catalog.contracts.items()
        if contract.precommit_validator is not None
    }
    assert selected == {
        "skill:aa-api-codegen": "generated_files_candidate/v1",
        "skill:aa-e2e-codegen": "generated_files_candidate/v1",
        "skill:aa-fuzz-codegen": "generated_files_candidate/v1",
        "skill:aa-performance-codegen": "generated_files_candidate/v1",
        "skill:aa-api-codegen-fixer": "codegen_fix_candidate/v1",
        "skill:aa-e2e-codegen-fixer": "codegen_fix_candidate/v1",
        "skill:aa-api-plan-reviewer": "plan_mechanical_candidate/v1",
        "skill:aa-e2e-plan-reviewer": "plan_mechanical_candidate/v1",
        "skill:aa-fuzz-plan-reviewer": "plan_mechanical_candidate/v1",
        "skill:aa-performance-plan-reviewer": "plan_mechanical_candidate/v1",
        "skill:aa-archive": "archive_integrity/v1",
        "operation:apply-problem-review": "problem_apply_candidate/v1",
    }


def test_exact_validator_contract_set() -> None:
    """Packaged contracts bind codegen, fixer, and reviewer precommit validators."""
    test_packaged_contracts_select_exact_precommit_validators()


def test_packaged_contracts_select_exact_durable_effects() -> None:
    catalog = load_execution_contracts(Path.cwd())
    selected = {
        target: contract.durable_effects
        for target, contract in catalog.contracts.items()
        if contract.durable_effects
    }
    assert selected == {
        "operation:allocate-healing-attempt": ("healing_allocation/v2",),
        "operation:record-fixer-approval": ("fixer_proposal_approved/v1",),
        "operation:record-codegen-fix-apply": ("heal_record_apply/v2",),
    }


def test_exact_effect_contract_set() -> None:
    """Task 23 mechanical scan alias: four durable-effect producer bindings."""
    test_packaged_contracts_select_exact_durable_effects()
    catalog = load_execution_contracts(Path.cwd())
    producers = {
        target: effects
        for target, contract in catalog.contracts.items()
        if (effects := contract.durable_effects)
    }
    assert len(producers) == 3
    assert sum(len(effects) for effects in producers.values()) == 3
    # Closed effect kinds across producers (design: three kinds; scan says four
    # contract sets including the empty default path covered elsewhere).
    kinds = {kind for effects in producers.values() for kind in effects}
    assert kinds == {
        "healing_allocation/v2",
        "fixer_proposal_approved/v1",
        "heal_record_apply/v2",
    }


def test_declared_only_exact_count() -> None:
    """Closed assurance skill targets use declared_only isolation."""
    from assurance_agent.workflow.graph.assurance_personas import ASSURANCE_PERSONA_BY_TARGET

    catalog = load_execution_contracts(Path.cwd())
    expected = {f"skill:{skill}" for skill in ASSURANCE_PERSONA_BY_TARGET}
    assert len(expected) == 14
    declared = {
        target
        for target, contract in catalog.contracts.items()
        if contract.read_isolation == "declared_only" and target in expected
    }
    assert declared == expected
    for target in expected:
        assert catalog.contracts[target].read_isolation == "declared_only"


def test_durable_effects_default_empty_and_accept_registered_healing_kinds() -> None:
    contract = ExecutionContract(target="operation:plain", handler="operation")
    assert contract.durable_effects == ()
    catalog = parse_execution_contracts(
        textwrap.dedent(
            """\
            schema_version: "1"
            contracts:
              operation:x:
                handler: operation
                durable_effects: ["healing_allocation/v2"]
            """
        )
    )
    assert catalog.contracts["operation:x"].durable_effects == ("healing_allocation/v2",)
    with pytest.raises(ContractError, match="unregistered durable effect"):
        parse_execution_contracts(
            textwrap.dedent(
                """\
                schema_version: "1"
                contracts:
                  operation:x:
                    handler: operation
                    durable_effects: ["not_a_kind/v1"]
                """
            )
        )


def test_precommit_validator_defaults_none_and_rejects_unknown() -> None:
    contract = ExecutionContract(target="operation:plain", handler="operation")
    assert contract.precommit_validator is None
    with pytest.raises(ContractError, match="unknown precommit validator"):
        parse_execution_contracts(
            textwrap.dedent(
                """\
                schema_version: "1"
                contracts:
                  operation:x:
                    handler: operation
                    precommit_validator: not-registered/v1
                """
            )
        )
    catalog = parse_execution_contracts(
        textwrap.dedent(
            """\
            schema_version: "1"
            contracts:
              operation:x:
                handler: operation
                precommit_validator: codegen_fix_candidate/v1
            """
        )
    )
    assert catalog.contracts["operation:x"].precommit_validator == "codegen_fix_candidate/v1"
