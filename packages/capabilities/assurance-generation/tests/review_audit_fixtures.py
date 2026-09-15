from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codegen_fixtures import codegen_result, locked_oracle_paths  # pyright: ignore[reportMissingImports]
from planning_fixtures import plan_input  # pyright: ignore[reportMissingImports]

HELPER_PATH = "qa/tests/testdata/domain/item.py"
PLAN_PATH = "qa/results/codegen/api-codegen-summary.md"


def write(root: Path, path: str, content: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def write_review(root: Path, review: dict[str, Any]) -> None:
    write(root, "qa/results/review/api-codegen-review.json", json.dumps(review))
    write(root / "qa/.staging/attempt-1", "qa/results/review/api-codegen-review.json", json.dumps(review))


def write_codegen_artifacts(
    root: Path,
    family: str = "api",
    *,
    summary: str | None = None,
    helper_mention: str | None = None,
    generated_target: bool = True,
) -> dict[str, Any]:
    test_path, data_path = locked_oracle_paths(family)
    manifest = codegen_result(files=[test_path, data_path], family=family)
    body = summary
    if body is None:
        mention = helper_mention or ""
        target_line = f"- `{HELPER_PATH}`: implement rows.\n" if generated_target else "None.\n"
        body = f"# Codegen\n\n## Import Strategy\n{mention}\n\n## Target Files\n{target_line}"
    write(root, f"qa/results/codegen/{family}-codegen-summary.md", body)
    write(root, f"qa/results/codegen/{family}-generated-files.json", json.dumps(manifest))
    write(root, data_path, "helper\n")
    return manifest


def review_prepare_input(family: str, business: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(business or plan_input(family))
    test_path, data_path = locked_oracle_paths(family)
    payload["codegen_output"] = codegen_result(files=[test_path, data_path], family=family)
    return payload
