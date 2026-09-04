from __future__ import annotations

import json
from pathlib import Path

from tests.product.test_result_export import CHANGE_ID, write_achieved


def test_tampered_receipt_is_rejected_before_archive(cli_runner, tmp_path: Path) -> None:
    from assurance_product.cli import app
    from assurance_product.export import publish_achieved

    project = write_achieved(tmp_path)
    publish_achieved(project, CHANGE_ID)
    receipt = project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    files = payload["files"]
    files[0]["final_sha256"] = files[0]["final_sha256"][:-1] + (
        "0" if files[0]["final_sha256"][-1] != "0" else "1"
    )
    receipt.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result = cli_runner.invoke(
        app,
        ["archive", "--json", "--project-dir", str(project), "--change", CHANGE_ID],
    )
    assert result.exit_code == 40, result.output
