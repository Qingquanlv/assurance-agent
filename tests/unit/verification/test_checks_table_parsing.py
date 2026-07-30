"""Case ID table parsing keeps IDs and table boundaries exact."""

from assurance_agent.verification.checks.base import case_id_rows

DOC = """# Plan

| Case ID | Title |
|---|---|
| CASE-1 | first |
| CASE-10 | tenth |

Prose paragraph between tables.

| Case ID | Expected |
|---|---|
| CASE-1 | HTTP 4xx |

| Entity | Ownership |
|---|---|
| Dept | reuse |
"""


def test_only_case_id_tables_are_yielded() -> None:
    rows = list(case_id_rows(DOC))
    assert [row[2] for row in rows] == ["CASE-1", "CASE-10", "CASE-1"]


def test_table_index_distinguishes_cross_table_from_same_table_repeats() -> None:
    rows = list(case_id_rows(DOC))
    assert [row[0] for row in rows] == [0, 0, 1]


def test_case_id_cell_is_exact_not_substring() -> None:
    ids = {row[2] for row in case_id_rows(DOC)}
    assert "CASE-1" in ids and "CASE-10" in ids
    assert len([row for row in case_id_rows(DOC) if row[2] == "CASE-1"]) == 2


def test_a_table_without_a_case_id_column_is_ignored() -> None:
    assert all("Dept" not in row[3] for row in case_id_rows(DOC))


def test_line_numbers_point_at_the_source_row() -> None:
    first = next(iter(case_id_rows(DOC)))
    assert DOC.splitlines()[first[1] - 1].startswith("| CASE-1 ")
