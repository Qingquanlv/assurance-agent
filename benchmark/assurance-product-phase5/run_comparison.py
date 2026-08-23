from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from compare import (
    CASE_IDS,
    COMPARED_FIELDS,
    ComparisonManifestV1,
    ComparisonResultV1,
    DispositionV1,
    compare_case,
)

REPO = Path(__file__).resolve().parents[2]
HARNESS_ROOT = Path(__file__).resolve().parent
PHASE5_TESTS = REPO / "tests" / "phase5"
MANIFEST_PATH = HARNESS_ROOT / "comparison-manifest.json"
DISPOSITIONS_PATH = (
    REPO
    / ".superpowers"
    / "sdd"
    / "2026-08-22-pure-graph-engine-phase5-assurance-product-assembly"
    / "comparison-dispositions.yaml"
)


def load_manifest() -> ComparisonManifestV1:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return ComparisonManifestV1.model_validate(raw)


def load_disposition_rows() -> tuple[DispositionV1, ...]:
    raw = yaml.safe_load(DISPOSITIONS_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise SystemExit("comparison-dispositions.yaml must contain a mapping")
    cases = raw.get("cases")
    if not isinstance(cases, list):
        raise SystemExit("comparison-dispositions.yaml must list cases")
    rows: list[DispositionV1] = []
    for case in cases:
        if not isinstance(case, Mapping):
            raise SystemExit("each comparison case must be a mapping")
        case_id = case.get("id")
        fields = case.get("fields") or []
        if not isinstance(case_id, str) or not isinstance(fields, list):
            raise SystemExit("disposition rows must have a string id and fields array")
        if case_id not in CASE_IDS or any(token in case_id for token in ("*", "?", "[")):
            raise SystemExit(f"wildcard or unknown case disposition: {case_id}")
        for item in fields:
            if not isinstance(item, Mapping):
                raise SystemExit(f"{case_id} field row must be a mapping")
            field = item.get("field")
            if not isinstance(field, str) or field not in COMPARED_FIELDS:
                raise SystemExit(f"wildcard or unknown field disposition: {case_id}/{field}")
            rows.append(DispositionV1.model_validate({"case_id": case_id, **dict(item)}))
    return tuple(rows)


def dispositions_for(case_id: str, rows: tuple[DispositionV1, ...]) -> dict[str, DispositionV1]:
    return {item.field: item for item in rows if item.case_id == case_id}


def resolve_export(relative: str) -> Path:
    return PHASE5_TESTS / relative


def run_matrix() -> dict[str, Any]:
    manifest = load_manifest()
    rows = load_disposition_rows()
    seen = {item.id for item in manifest.cases}
    missing = [case_id for case_id in CASE_IDS if case_id not in seen]
    extra = [case_id for case_id in seen if case_id not in CASE_IDS]
    results: list[ComparisonResultV1] = []
    undisposed = 0
    for item in manifest.cases:
        result = compare_case(
            item.id,
            resolve_export(item.legacy_export),
            resolve_export(item.current_export),
            dispositions_for(item.id, rows),
        )
        results.append(result)
        undisposed += len(result.undisposed_differences)
    governed_passes = sum(1 for item in results if item.passed)
    return {
        "schema_version": "1",
        "executed": len(results),
        "governed_passes": governed_passes,
        "missing": len(missing),
        "extra": len(extra),
        "undisposed_mismatches": undisposed,
        "case_ids": [item.id for item in manifest.cases],
        "results": [
            {
                "case_id": item.case_id,
                "passed": item.passed,
                "undisposed": len(item.undisposed_differences),
            }
            for item in results
        ],
    }


def main() -> int:
    payload = run_matrix()
    print(json.dumps(payload, sort_keys=True, indent=2))
    closed = (
        payload["executed"] == 25
        and payload["governed_passes"] == 25
        and payload["missing"] == 0
        and payload["extra"] == 0
        and payload["undisposed_mismatches"] == 0
    )
    return 0 if closed else 1


if __name__ == "__main__":
    raise SystemExit(main())
