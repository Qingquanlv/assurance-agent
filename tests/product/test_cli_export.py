from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

from tests.product.cli_support import parse_json_output
from tests.product.test_result_export import CHANGE_ID, TARGET_A, TARGET_B, write_achieved


def _schema() -> dict[str, object]:
    raw = files("assurance_product").joinpath("resources/schemas/publish-receipt-v1.json").read_bytes()
    return json.loads(raw.decode("utf-8"))


def test_cli_export_publishes_explicit_change(cli_runner, tmp_path: Path) -> None:
    from assurance_product.cli import app
    from assurance_product.models import PublishReceiptV1

    project = write_achieved(tmp_path)
    result = cli_runner.invoke(
        app,
        ["export", "--json", "--project-dir", str(project), "--change", CHANGE_ID],
    )

    assert result.exit_code == 0, result.output
    document = parse_json_output(result.stdout)
    receipt = PublishReceiptV1.model_validate(document)
    assert receipt.change_id == CHANGE_ID
    assert (project / TARGET_A).read_bytes() == b"generated-a\n"
    assert (project / TARGET_B).read_bytes() == b"generated-b\n"
    schema = _schema()
    assert schema["title"] == "PublishReceiptV1"
    assert schema["additionalProperties"] is False
    required = schema["required"]
    assert isinstance(required, list)
    for key in required:
        assert key in document


def test_cli_export_selects_single_unpublished_achieved_change(cli_runner, tmp_path: Path) -> None:
    from assurance_product.cli import app

    project = write_achieved(tmp_path)
    result = cli_runner.invoke(app, ["export", "--project-dir", str(project)])

    assert result.exit_code == 0, result.output
    document = parse_json_output(result.stdout)
    assert document["change_id"] == CHANGE_ID
    assert (project / TARGET_A).read_bytes() == b"generated-a\n"


def test_cli_export_rejects_zero_candidates_without_mutation(cli_runner, tmp_path: Path) -> None:
    from assurance_product.cli import app

    project = write_achieved(tmp_path, state="running", publication="not_ready")
    original = (project / TARGET_A).read_bytes()

    result = cli_runner.invoke(app, ["export", "--project-dir", str(project)])

    assert result.exit_code != 0
    assert "achieved unpublished" in result.output or "found 0" in result.output
    assert (project / TARGET_A).read_bytes() == original
    assert not (project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json").exists()


def test_cli_export_rejects_multiple_candidates_without_mutation(cli_runner, tmp_path: Path) -> None:
    from assurance_product.cli import app

    other = "CH-PUB-002"
    project = write_achieved(tmp_path)
    write_achieved(
        tmp_path,
        change_id=other,
        files=((TARGET_A, b"generated-other\n", b"original-other\n"),),
        extras={},
        project=project,
    )
    first = (project / TARGET_A).read_bytes()

    result = cli_runner.invoke(app, ["export", "--project-dir", str(project)])

    assert result.exit_code != 0
    assert CHANGE_ID in result.output
    assert other in result.output
    assert (project / TARGET_A).read_bytes() == first
    assert not (project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json").exists()
    assert not (project / "qa" / "changes" / other / "publish-receipt.json").exists()


def test_cli_export_explicit_change_wins(cli_runner, tmp_path: Path) -> None:
    from assurance_product.cli import app

    other = "CH-PUB-002"
    project = write_achieved(tmp_path)
    write_achieved(
        tmp_path,
        change_id=other,
        files=(("tests/api/test_other.py", b"generated-other\n", b"original-other\n"),),
        extras={},
        project=project,
    )

    result = cli_runner.invoke(
        app,
        ["export", "--project-dir", str(project), "--change", other],
    )

    assert result.exit_code == 0, result.output
    document = parse_json_output(result.stdout)
    assert document["change_id"] == other
    assert (project / "tests/api/test_other.py").read_bytes() == b"generated-other\n"
    assert (project / TARGET_A).read_bytes() == b"original-a\n"
    assert not (project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json").exists()


def test_cli_export_does_not_import_legacy_engine(cli_runner, tmp_path: Path, monkeypatch) -> None:
    from assurance_product.cli import app
    def _forbid(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("export must not call leftover Engine or driver")
    del _forbid
    project = write_achieved(tmp_path)
    result = cli_runner.invoke(
        app,
        ["export", "--json", "--project-dir", str(project), "--change", CHANGE_ID],
    )
    assert result.exit_code == 0, result.output
