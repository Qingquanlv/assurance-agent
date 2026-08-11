"""AST property-marker scan and ``property_unknown_key`` gaps (§5-A2)."""

from __future__ import annotations

from pathlib import Path

from assurance_agent.verification.property_scan import (
    PROPERTY_UNKNOWN_KEY,
    extract_property_markers,
    scan_property_tests,
)


_SOURCE = """\
import pytest

@pytest.mark.property("entities.dept.constraints.name_unique")
async def test_duplicate_dept_name_rejected():
    assert True

@pytest.mark.property(
    "entities.dept.constraints.name_has_max_length",
    "entities.dept.constraints.required_fields",
)
def test_dept_field_rules():
    pass

@pytest.mark.api
def test_plain_api_case():
    pass

def test_no_marker():
    pass
"""


def test_extract_property_markers_from_ast() -> None:
    hits = extract_property_markers(_SOURCE, file="tests/api/test_props.py")
    assert [(h.test_name, h.constraint_keys) for h in hits] == [
        ("test_duplicate_dept_name_rejected", ("entities.dept.constraints.name_unique",)),
        (
            "test_dept_field_rules",
            (
                "entities.dept.constraints.name_has_max_length",
                "entities.dept.constraints.required_fields",
            ),
        ),
    ]
    assert all(h.file == "tests/api/test_props.py" for h in hits)


def test_unknown_constraint_key_emits_stable_property_unknown_key_gap(tmp_path: Path) -> None:
    path = tmp_path / "tests" / "api" / "test_props.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        '@pytest.mark.property("entities.dept.constraints.invented_key")\n'
        "def test_invented():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    known = frozenset(
        {
            "entities.dept.constraints.name_unique",
            "entities.dept.constraints.name_has_max_length",
        }
    )
    result = scan_property_tests([path], root=tmp_path, known_keys=known)

    assert len(result.hits) == 1
    assert result.hits[0].constraint_keys == ("entities.dept.constraints.invented_key",)
    assert len(result.unknown_key_gaps) == 1
    gap = result.unknown_key_gaps[0]
    assert gap.code == PROPERTY_UNKNOWN_KEY
    assert gap.code == "property_unknown_key"
    assert gap.metric == "constraint_coverage"
    assert gap.subject == "entities.dept.constraints.invented_key"
    assert "test_invented" in gap.detail
    # Stable shape for MetricsDocument.collection_gaps consumers.
    assert gap.model_dump(mode="json") == {
        "code": "property_unknown_key",
        "metric": "constraint_coverage",
        "subject": "entities.dept.constraints.invented_key",
        "detail": gap.detail,
    }


def test_known_keys_produce_no_unknown_gap(tmp_path: Path) -> None:
    path = tmp_path / "test_ok.py"
    path.write_text(
        '@pytest.mark.property("entities.dept.constraints.name_unique")\ndef test_ok():\n    pass\n',
        encoding="utf-8",
    )
    result = scan_property_tests(
        [path],
        root=tmp_path,
        known_keys=frozenset({"entities.dept.constraints.name_unique"}),
    )
    assert result.unknown_key_gaps == ()


def test_without_known_keys_skips_unknown_detection(tmp_path: Path) -> None:
    """Closed-key set is injectable; omitting it does not invent SUT keys."""
    path = tmp_path / "test_x.py"
    path.write_text(
        '@pytest.mark.property("entities.dept.constraints.anything")\ndef test_x():\n    pass\n',
        encoding="utf-8",
    )
    result = scan_property_tests([path], root=tmp_path, known_keys=None)
    assert len(result.hits) == 1
    assert result.unknown_key_gaps == ()


def test_pytest_mark_property_via_attr_chain() -> None:
    source = (
        "import pytest\n"
        "@pytest.mark.property('entities.dept.constraints.name_unique')\n"
        "def test_attr():\n"
        "    pass\n"
    )
    hits = extract_property_markers(source, file="t.py")
    assert hits[0].constraint_keys == ("entities.dept.constraints.name_unique",)


def test_extract_property_markers_from_class_scoped_tests() -> None:
    source = """\
import pytest

class TestDeptConstraints:
    @pytest.mark.property("entities.dept.constraints.name_unique")
    def test_duplicate_name(self):
        assert True

    @pytest.mark.property(
        "entities.dept.constraints.name_has_max_length",
        "entities.dept.constraints.required_fields",
    )
    def test_field_rules(self):
        pass

    def test_unmarked(self):
        pass
"""
    hits = extract_property_markers(source, file="tests/api/test_class_props.py")
    assert [(h.test_name, h.constraint_keys) for h in hits] == [
        ("test_duplicate_name", ("entities.dept.constraints.name_unique",)),
        (
            "test_field_rules",
            (
                "entities.dept.constraints.name_has_max_length",
                "entities.dept.constraints.required_fields",
            ),
        ),
    ]


def test_empty_known_keys_marks_every_key_unknown(tmp_path: Path) -> None:
    """``known_keys=frozenset()`` means every referenced key is unknown."""
    path = tmp_path / "test_x.py"
    path.write_text(
        '@pytest.mark.property("entities.dept.constraints.name_unique")\ndef test_x():\n    pass\n',
        encoding="utf-8",
    )
    result = scan_property_tests([path], root=tmp_path, known_keys=frozenset())
    assert len(result.hits) == 1
    assert len(result.unknown_key_gaps) == 1
    assert result.unknown_key_gaps[0].subject == "entities.dept.constraints.name_unique"
    assert result.unknown_key_gaps[0].code == PROPERTY_UNKNOWN_KEY
