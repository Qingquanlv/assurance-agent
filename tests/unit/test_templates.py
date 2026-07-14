import json

import yaml

from assurance_agent.workflow.core.templates import (
    InitAnswers,
    build_config_yaml,
    build_data_knowledge_yaml,
    build_execution_policy,
    build_module_map_yaml,
)


def make_answers(**overrides: object) -> InitAnswers:
    defaults: dict = {
        "api_framework": "pytest",
        "e2e_framework": "playwright",
        "enable_mcp": False,
        "frontend_path": None,
        "backend_path": None,
    }
    defaults.update(overrides)
    return InitAnswers(**defaults)


def test_config_yaml_defaults_parse_and_point_to_aa_paths() -> None:
    doc = yaml.safe_load(build_config_yaml(make_answers()))
    assert doc["version"] == 1
    assert doc["sources"] == {"frontend": "./frontend", "backend": "./backend"}
    assert doc["frameworks"]["api"] == {"enabled": True, "name": "pytest"}
    assert doc["frameworks"]["e2e"] == {"enabled": True, "name": "playwright"}
    assert doc["execution"]["policy_file"] == "./.aa/execution-policy.json"
    assert doc["generation"]["prd_input_mode"] == "prompt"
    assert doc["execution"]["self_healing"]["mode"] == "proposal-only"
    assert doc["generation"]["e2e"]["default_pom"] is False
    assert doc["mcp"]["enabled"] is False


def test_config_yaml_none_framework_disables_layer() -> None:
    doc = yaml.safe_load(build_config_yaml(make_answers(api_framework="none")))
    assert doc["frameworks"]["api"] == {"enabled": False, "name": "none"}


def test_config_yaml_custom_source_paths() -> None:
    doc = yaml.safe_load(build_config_yaml(make_answers(frontend_path="./web", backend_path="./server")))
    assert doc["sources"] == {"frontend": "./web", "backend": "./server"}


def test_execution_policy_targets_follow_enabled_layers() -> None:
    policy = build_execution_policy(make_answers(e2e_framework="none"))
    assert policy["targets"] == ["api"]
    assert policy["parallel"] == {"api": 4}
    assert policy["retry"] == {"api": 0}
    assert "e2e" not in policy
    assert policy["healing"]["mode"] == "proposal-only"
    json.dumps(policy)  # 必须可序列化


def test_module_map_and_data_knowledge_parse() -> None:
    module_map = yaml.safe_load(build_module_map_yaml())
    assert isinstance(module_map["rules"], list)
    knowledge = yaml.safe_load(build_data_knowledge_yaml())
    assert knowledge["version"] == 1
    assert knowledge["accounts"] == {}
    assert set(knowledge["capabilities"]["adapters"]) == {"api", "e2e", "fuzz", "performance"}
