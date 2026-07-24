"""Shared canonical retro proposal fixtures for unit tests."""

from __future__ import annotations

from assurance_agent.retro.types import RetroProposal


def memory_proposal(**overrides: object) -> RetroProposal:
    data = memory_proposal_dict(**overrides)
    return RetroProposal.model_validate(data)


def memory_proposal_dict(**overrides: object) -> dict:
    data = {
        "id": "P-1",
        "finding_kind": "prompt_rule",
        "apply_kind": "memory_append",
        "payload": {"body": "remember to check fixtures"},
        "eval_suite": "workflow-run",
        "target": ".aa/memory/aa-run.md",
        "problem": "flaky fixture use",
        "proposed_change": "remember to check fixtures",
        "evidence_ids": ["CH-1#F-1"],
    }
    data.update(overrides)
    return data


def issue_proposal(**overrides: object) -> RetroProposal:
    data = {
        "id": "P-ISSUE",
        "finding_kind": "workflow_bug",
        "apply_kind": "issue_export",
        "payload": {
            "title": "inspect misclassifies schema KeyError",
            "target": "assurance_agent/workflow/report/failure_classifier.py",
            "severity": "medium",
            "evidence_ids": ["CH-1#F-1"],
            "proposed_change": "register schema keys before assert_matches_schema",
        },
        "problem": "unknown classification for schema KeyError",
        "evidence_ids": ["CH-1#F-1"],
    }
    data.update(overrides)
    return RetroProposal.model_validate(data)


def issue_proposal_dict(**overrides: object) -> dict:
    data = {
        "id": "P-ISSUE",
        "finding_kind": "workflow_bug",
        "apply_kind": "issue_export",
        "payload": {
            "title": "inspect misclassifies schema KeyError",
            "target": "assurance_agent/workflow/report/failure_classifier.py",
            "severity": "medium",
            "evidence_ids": ["CH-1#F-1"],
            "proposed_change": "register schema keys before assert_matches_schema",
        },
        "problem": "unknown classification for schema KeyError",
        "evidence_ids": ["CH-1#F-1"],
    }
    data.update(overrides)
    return data


def knowledge_proposal(**overrides: object) -> RetroProposal:
    data = {
        "id": "P-KNOW",
        "finding_kind": "domain_knowledge",
        "apply_kind": "knowledge_delta",
        "payload": {
            "schema_version": "1",
            "mode": "delta",
            "based_on_l1_version": 1,
            "auth": {
                "api_admin_token": {
                    "method": "token",
                    "notes": "retro discovered admin token helper",
                }
            },
        },
        "problem": "missing auth capability in L1",
        "evidence_ids": ["CH-1#F-1"],
    }
    data.update(overrides)
    return RetroProposal.model_validate(data)
