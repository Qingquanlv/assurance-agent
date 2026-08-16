"""Deterministic Markdown rendering for specialty v1/v2/v3 benchmark reports."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from benchmark.specialty.specialty_models import (
    CompleteLayerRow,
    CompleteTraceabilityEvidenceV3,
    IncompleteLayerRow,
    IncompleteTraceabilityEvidenceV3,
    LayerRow,
    LegacySpecialtyReportV1,
    NotSelectedLayerRow,
    NotWiredLayerRow,
    SpecialtyReport,
    SpecialtyReportV2,
    SpecialtyReportV3,
    SpecialtyReportVariant,
)


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _reason_counts(reasons: Mapping[Any, Any]) -> str:
    return ", ".join(f"{key}:{value}" for key, value in sorted(reasons.items())) or "none"


def _layer_assurance_cells(row: LayerRow) -> tuple[str, str, str, str, str, str]:
    if isinstance(row, CompleteLayerRow):
        if row.applicability == "applicable" and row.capabilities is not None:
            capabilities = f"{len(row.capabilities.required)}/{len(row.capabilities.missing)}"
        else:
            capabilities = "-"
        contract = "yes" if row.mechanical_execution_contract_digest else "no"
        return (
            row.status,
            row.applicability,
            row.mechanical_checks.status,
            str(row.mechanical_checks.finding_count),
            capabilities,
            contract,
        )
    if isinstance(row, (NotSelectedLayerRow, NotWiredLayerRow, IncompleteLayerRow)):
        return (row.status, "-", "-", "-", "-", "-")
    raise TypeError(f"unsupported layer row type: {type(row)!r}")


def _replay_verdict_cells(row: LayerRow) -> tuple[str, str, str]:
    if isinstance(row, CompleteLayerRow):
        by_action = {item.action: item.verdict for item in row.scenarios}
        return (
            _cell(by_action["warn"]),
            _cell(by_action["block"]),
            _cell(by_action["require_human"]),
        )
    if isinstance(row, NotSelectedLayerRow):
        return ("not_selected", "not_selected", "not_selected")
    if isinstance(row, NotWiredLayerRow):
        return ("not_wired", "not_wired", "not_wired")
    if isinstance(row, IncompleteLayerRow):
        reason = _cell(row.reason_code)
        return (reason, reason, reason)
    raise TypeError(f"unsupported layer row type: {type(row)!r}")


def _replay_reports(
    reports: Sequence[SpecialtyReportVariant],
) -> list[SpecialtyReportV2 | SpecialtyReportV3]:
    return [report for report in reports if isinstance(report, (SpecialtyReportV2, SpecialtyReportV3))]


def _render_layer_assurance_matrix(
    reports: Sequence[SpecialtyReportV2 | SpecialtyReportV3],
) -> list[str]:
    lines = [
        "### Layer Assurance Matrix",
        "",
        "| change_id | layer | status | applicability | mechanical | findings | capabilities req/miss | contract digest |",
        "|---|---|---|---|---|---:|---|---|",
    ]
    for report in reports:
        for row in report.capability_contract_policy.rows:
            status, applicability, mechanical, findings, capabilities, contract = _layer_assurance_cells(row)
            lines.append(
                "| `{}` | {} | {} | {} | {} | {} | {} | {} |".format(
                    _cell(report.change_id),
                    _cell(row.layer),
                    _cell(status),
                    _cell(applicability),
                    _cell(mechanical),
                    _cell(findings),
                    _cell(capabilities),
                    _cell(contract),
                )
            )
    return lines


def _render_policy_replay_matrix(reports: Sequence[SpecialtyReportVariant]) -> list[str]:
    lines = [
        "### Policy Replay Matrix",
        "",
        "| change_id | layer | warn | block | require_human |",
        "|---|---|---|---|---|",
    ]
    for report in reports:
        if isinstance(report, (SpecialtyReportV2, SpecialtyReportV3)):
            for row in report.capability_contract_policy.rows:
                warn, block, require_human = _replay_verdict_cells(row)
                lines.append(
                    "| `{}` | {} | {} | {} | {} |".format(
                        _cell(report.change_id),
                        _cell(row.layer),
                        warn,
                        block,
                        require_human,
                    )
                )
        elif isinstance(report, LegacySpecialtyReportV1):
            cap = report.capability_contract_policy
            replay = {row["action"]: row["verdict"] for row in cap["policy_replay"]}
            lines.append(
                "| `{}` | legacy_api_only | {} | {} | {} |".format(
                    _cell(report.change_id),
                    _cell(replay.get("warn", "missing")),
                    _cell(replay.get("block", "missing")),
                    _cell(replay.get("require_human", "missing")),
                )
            )
        else:
            raise TypeError(f"unsupported specialty report type: {type(report)!r}")
    return lines


def _render_legacy_api_only(reports: Sequence[LegacySpecialtyReportV1]) -> list[str]:
    lines = [
        "### legacy_api_only",
        "",
        "| change_id | mechanical | checks | findings | capabilities required/missing | execution contracts | output contracts | prompt audit | policy source/actions | digest match |",
        "|---|---|---|---:|---|---:|---:|---|---|---|",
    ]
    for report in reports:
        cap = report.capability_contract_policy
        checks = (
            ", ".join(
                f"{check_id}={item['status']}({item['finding_count']})"
                for check_id, item in cap["mechanical_checks"]["by_check"].items()
            )
            or "none"
        )
        current_digest = cap["policy"]["digest"]
        recorded_digest = cap["policy"]["recorded_digest"]
        policy_actions = ", ".join(
            f"{check_id}={action}" for check_id, action in sorted(cap["policy"]["plan_checks"].items())
        )
        lines.append(
            "| `{}` | {} | {} | {} | {}/{} | {} | {} | {} | {}/{} | {} |".format(
                _cell(report.change_id),
                _cell(cap["mechanical_checks"]["status"]),
                _cell(checks),
                cap["mechanical_checks"]["finding_count"],
                len(cap["capabilities"]["required"]),
                len(cap["capabilities"]["missing"]),
                len(cap["contracts"]["agent_execution_contract_digests"]),
                cap["contracts"]["rendered_output_contract_count"],
                _cell(cap["contracts"]["prompt_observability"]),
                _cell(cap["policy"]["source"]),
                _cell(policy_actions),
                "yes" if current_digest == recorded_digest else "no",
            )
        )
    return lines


def _render_capability_sections(reports: Sequence[SpecialtyReportVariant]) -> list[str]:
    sorted_reports = sorted(reports, key=lambda item: item.change_id)
    replay_reports = _replay_reports(sorted_reports)
    v1_reports = [report for report in sorted_reports if isinstance(report, LegacySpecialtyReportV1)]

    lines = ["## Capability + Contract + Policy", ""]
    if replay_reports:
        semantics = sorted({report.capability_contract_policy.semantics for report in replay_reports})
        lines.append("Replay semantics: " + ", ".join(f"`{item}`" for item in semantics))
        lines.append("")
        lines.extend(_render_layer_assurance_matrix(replay_reports))
        lines.append("")
    if replay_reports or v1_reports:
        lines.extend(_render_policy_replay_matrix(sorted_reports))
    if v1_reports:
        lines.extend(["", *_render_legacy_api_only(v1_reports)])
    return lines


def _render_legacy_traceability(reports: Sequence[SpecialtyReportV2 | LegacySpecialtyReportV1]) -> list[str]:
    lines = [
        "### Execution Projection",
        "",
        "| change_id | phase | batch | integrity | rows | sources | gaps | unmapped tests |",
        "|---|---|---|---|---:|---:|---:|---:|",
    ]
    for report in reports:
        item = report.traceability_evidence["execution_projection"]
        lines.append(
            "| `{}` | {} | {} | {} | {} | {} | {} | {} |".format(
                _cell(report.change_id),
                _cell(item["phase"]),
                _cell(item["batch_id"]),
                _cell(item["integrity"]),
                item["row_count"],
                item["source_count"],
                item["gap_count"],
                item["unmapped_test_count"],
            )
        )
    lines.extend(
        [
            "",
            "### Reconciled Projection",
            "",
            "| change_id | phase | batch | integrity | rows | sources | gaps | unmapped tests | failure rows/links | problem rows/links/unique |",
            "|---|---|---|---|---:|---:|---:|---:|---|---|",
        ]
    )
    for report in reports:
        item = report.traceability_evidence["reconciled_projection"]
        lines.append(
            "| `{}` | {} | {} | {} | {} | {} | {} | {} | {}/{} | {}/{}/{} |".format(
                _cell(report.change_id),
                _cell(item["phase"]),
                _cell(item["batch_id"]),
                _cell(item["integrity"]),
                item["row_count"],
                item["source_count"],
                item["gap_count"],
                item["unmapped_test_count"],
                item["failure_row_count"],
                item["failure_link_count"],
                item["open_problem_row_count"],
                item["open_problem_link_count"],
                item["unique_open_problem_count"],
            )
        )
    lines.extend(
        [
            "",
            "### Evidence Sufficiency and Coverage",
            "",
            "| change_id | sufficient | insufficient | reason counts | evidence coverage | line % | branch % | final status |",
            "|---|---:|---:|---|---|---:|---:|---|",
        ]
    )
    for report in reports:
        evidence = report.traceability_evidence
        sufficiency = evidence["sufficiency"]
        coverage = evidence["coverage"]
        lines.append(
            "| `{}` | {} | {} | {} | {} | {} | {} | {} |".format(
                _cell(report.change_id),
                sufficiency["sufficient_count"],
                sufficiency["insufficient_count"],
                _cell(_reason_counts(sufficiency["reason_counts"])),
                _cell(coverage["status"]),
                _cell(coverage["line"]),
                _cell(coverage["branch"]),
                _cell(coverage["final_status"]),
            )
        )
    lines.extend(["", "### Verify Diagnostics", ""])
    for report in reports:
        verify = report.traceability_evidence["verify"]
        lines.append(
            "- `{}`: verdict={}; blocking gaps={}; open problems={}; reported insufficient={}; observed insufficient={}.".format(
                _cell(report.change_id),
                _cell(verify["verdict"]),
                verify["blocking_gap_count"],
                verify["open_problem_count"],
                verify["reported_insufficient_count"],
                verify["observed_insufficient_count"],
            )
        )
    return lines


def _render_v3_overview(
    report: SpecialtyReportV3,
    evidence: CompleteTraceabilityEvidenceV3,
) -> list[str]:
    lines: list[str] = []
    for title, phase in (
        ("Execution Projection", evidence.execution),
        ("Reconciled Projection", evidence.reconciled),
    ):
        overview = phase.overview
        lines.extend(
            [
                f"### {title}",
                "",
                f"- `{_cell(report.change_id)}`: phase={overview.phase}; batch={_cell(overview.batch_id)}; "
                f"integrity={overview.integrity}; rows={overview.row_count}; sources={overview.source_count}; "
                f"gaps={overview.gap_count}; unmapped={overview.unmapped_test_count}.",
                "",
            ]
        )
    coverage = evidence.coverage
    lines.extend(
        [
            "### Evidence Sufficiency and Coverage",
            "",
            f"- `{_cell(report.change_id)}`: coverage={coverage.status}; line={coverage.line}; "
            f"branch={coverage.branch}; final_status={coverage.final_status}.",
            "",
            "### Verify Diagnostics",
            "",
            "- `{}`: verdict={}; blocking gaps={}; open problems={}; reported insufficient={}.".format(
                _cell(report.change_id),
                _cell(evidence.verify.verdict),
                evidence.verify.blocking_gap_count,
                evidence.verify.open_problem_count,
                evidence.verify.reported_insufficient_count,
            ),
            "",
            "### Global Gaps",
            "",
        ]
    )
    for phase in (evidence.execution, evidence.reconciled):
        gaps = phase.facts.global_gaps
        by_code = ", ".join(f"{code}:{count}" for code, count in sorted(gaps.by_code.items())) or "none"
        lines.append(
            f"- `{_cell(report.change_id)}`/{phase.overview.phase}: total={gaps.total}; by_code={_cell(by_code)}."
        )
    lines.append("")
    return lines


def _render_v3_fact_rows(
    report: SpecialtyReportV3,
    evidence: CompleteTraceabilityEvidenceV3,
) -> list[str]:
    lines: list[str] = []
    for phase in (evidence.execution, evidence.reconciled):
        for layer in phase.facts.layers:
            lines.append(
                "| `{}` | {} | {} | {} | {} | {} | {} | {} |".format(
                    _cell(report.change_id),
                    _cell(phase.overview.phase),
                    _cell(layer.layer),
                    _cell(layer.case_type),
                    layer.total,
                    layer.covered,
                    layer.uncovered,
                    layer.gaps.total,
                )
            )
    return lines


def _render_v3_sufficiency_rows(
    report: SpecialtyReportV3,
    evidence: CompleteTraceabilityEvidenceV3,
) -> list[str]:
    lines: list[str] = []
    for layer in evidence.sufficiency.layers:
        lines.append(
            "| `{}` | {} | {} | {} | {} | {} |".format(
                _cell(report.change_id),
                _cell(layer.layer),
                _cell(layer.case_type),
                layer.sufficient,
                layer.insufficient,
                _cell(_reason_counts(dict(layer.reason_counts))),
            )
        )
    return lines


def _render_v3_trace(
    report: SpecialtyReportV3,
) -> tuple[list[str], list[str], list[str]]:
    evidence = report.traceability_evidence
    if isinstance(evidence, IncompleteTraceabilityEvidenceV3):
        return (
            [f"- `{_cell(report.change_id)}`: {evidence.reason_code}; {_cell(evidence.detail)}."],
            [],
            [],
        )
    return (
        _render_v3_overview(report, evidence),
        _render_v3_fact_rows(report, evidence),
        _render_v3_sufficiency_rows(report, evidence),
    )


def _render_traceability_sections(reports: Sequence[SpecialtyReportVariant]) -> list[str]:
    sorted_reports = sorted(reports, key=lambda item: item.change_id)
    lines = ["", "## Traceability / Evidence Projection", ""]

    legacy = [
        report
        for report in sorted_reports
        if isinstance(report, (SpecialtyReportV2, LegacySpecialtyReportV1))
    ]
    v3_reports = [report for report in sorted_reports if isinstance(report, SpecialtyReportV3)]

    if legacy:
        lines.extend(_render_legacy_traceability(legacy))
        if v3_reports:
            lines.append("")

    incomplete_lines: list[str] = []
    overview_lines: list[str] = []
    fact_rows: list[str] = []
    sufficiency_rows: list[str] = []
    for report in v3_reports:
        overview, facts, sufficiency = _render_v3_trace(report)
        if not facts and not sufficiency:
            incomplete_lines.extend(overview)
            continue
        overview_lines.extend(overview)
        fact_rows.extend(facts)
        sufficiency_rows.extend(sufficiency)

    if incomplete_lines:
        lines.extend(["### Incomplete Collection", "", *incomplete_lines, ""])
    if overview_lines:
        lines.extend(overview_lines)

    lines.extend(["### Trace Layer Facts", ""])
    if fact_rows:
        lines.extend(
            [
                "| change_id | phase | layer | case_type | total | covered | uncovered | gaps |",
                "|---|---|---|---|---:|---:|---:|---:|",
                *fact_rows,
            ]
        )
    if legacy:
        for report in legacy:
            lines.append(f"- `{_cell(report.change_id)}`: `legacy_unlayered`")
    if not fact_rows and not legacy:
        lines.append("_none_")
    lines.append("")

    lines.extend(["### Trace Layer Sufficiency", ""])
    if sufficiency_rows:
        lines.extend(
            [
                "| change_id | layer | case_type | sufficient | insufficient | reason counts |",
                "|---|---|---|---:|---:|---|",
                *sufficiency_rows,
            ]
        )
    if legacy:
        for report in legacy:
            lines.append(f"- `{_cell(report.change_id)}`: `legacy_unlayered`")
    if not sufficiency_rows and not legacy:
        lines.append("_none_")
    return lines


def render_specialty_sections(reports: Sequence[SpecialtyReport]) -> str:
    lines = _render_capability_sections(reports)
    lines.extend(_render_traceability_sections(reports))
    return "\n".join(lines) + "\n"


__all__ = ["render_specialty_sections"]
