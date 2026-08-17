import yaml

from assurance_agent import resources


def test_read_packaged_workflow_schema() -> None:
    text = resources.read_text("schemas", "workflow-schema.yaml")
    doc = yaml.safe_load(text)
    assert "schema_version" not in doc
    assert "graphs" in doc
    assert "entrypoints" in doc
    assert "phases" not in doc


def test_workflow_schema_has_no_legacy_aws_references() -> None:
    text = resources.read_text("schemas", "workflow-schema.yaml")
    assert "aws" not in text.lower()


def test_exists_and_missing() -> None:
    assert resources.exists("schemas", "workflow-schema.yaml")
    assert not resources.exists("schemas", "no-such-file.yaml")


def test_iter_children_lists_schema_files() -> None:
    names = resources.iter_children("schemas")
    assert "workflow-schema.yaml" in names
    assert "explore-advisory.schema.json" in names
