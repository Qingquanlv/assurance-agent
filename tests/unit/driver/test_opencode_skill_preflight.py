from __future__ import annotations

import hashlib
from typing import Any

import pytest
import yaml

from assurance_agent import resources
from assurance_agent.workflow.driver.adapter import DriverError
from assurance_agent.workflow.driver import opencode_adapter
from assurance_agent.workflow.driver.opencode_adapter import (
    validate_packaged_skill_catalog,
    validate_packaged_skill_server,
)


def _packaged_catalog() -> list[dict[str, Any]]:
    def live_fields(name: str) -> tuple[str, str]:
        raw = resources.read_text("skills", name, "SKILL.md")
        frontmatter, separator, body = raw[4:].partition("\n---\n")
        assert separator
        # OpenCode parses the YAML including the line break immediately before
        # the closing delimiter. That distinction is observable for `>` versus
        # `>-` block scalars and must not be modeled with the production parser.
        metadata = yaml.safe_load(frontmatter + "\n")
        assert isinstance(metadata, dict)
        description = metadata.get("description")
        assert isinstance(description, str)
        return description, body

    catalog: list[dict[str, Any]] = []
    for name in resources.iter_children("skills"):
        if not name.startswith("aa-"):
            continue
        description, body = live_fields(name)
        catalog.append(
            {
                "name": name,
                "description": description,
                "location": f"/runtime/{name}/SKILL.md",
                "content": body,
            }
        )
    return catalog


def _packaged_boundary_tool_id() -> str:
    digest = hashlib.sha256(resources.read_bytes("opencode", "plugins", "aa.mjs")).hexdigest()
    return f"aa_boundary_probe_v1_{digest}"


def test_packaged_skill_catalog_accepts_exact_live_contents() -> None:
    validate_packaged_skill_catalog(_packaged_catalog())


def test_packaged_skill_catalog_rejects_missing_skill_and_requires_restart() -> None:
    catalog = _packaged_catalog()
    catalog = [item for item in catalog if item["name"] != "aa-case-design"]

    with pytest.raises(DriverError, match=r"aa-case-design.*missing.*restart OpenCode"):
        validate_packaged_skill_catalog(catalog)


def test_packaged_skill_catalog_rejects_casefolded_duplicate() -> None:
    catalog = _packaged_catalog()
    case_design = next(item for item in catalog if item["name"] == "aa-case-design")
    catalog.append({**case_design, "name": "AA-CASE-DESIGN"})

    with pytest.raises(DriverError, match=r"duplicate.*aa-case-design.*restart OpenCode"):
        validate_packaged_skill_catalog(catalog)


def test_packaged_skill_catalog_rejects_stale_content_hash() -> None:
    catalog = _packaged_catalog()
    case_design = next(item for item in catalog if item["name"] == "aa-case-design")
    case_design["content"] += "\nstale cached body\n"

    with pytest.raises(
        DriverError,
        match=r"aa-case-design.*sha256 mismatch.*restart OpenCode",
    ):
        validate_packaged_skill_catalog(catalog)


def test_packaged_skill_catalog_rejects_stale_description() -> None:
    catalog = _packaged_catalog()
    case_design = next(item for item in catalog if item["name"] == "aa-case-design")
    case_design["description"] = "stale selection metadata"

    with pytest.raises(
        DriverError,
        match=r"aa-case-design.*description mismatch.*restart OpenCode",
    ):
        validate_packaged_skill_catalog(catalog)


def test_packaged_skill_catalog_rejects_extra_aa_namespace_member() -> None:
    catalog = _packaged_catalog()
    catalog.append(
        {
            "name": "aa-obsolete",
            "description": "removed skill",
            "location": "/runtime/aa-obsolete/SKILL.md",
            "content": "obsolete\n",
        }
    )

    with pytest.raises(DriverError, match=r"unexpected.*aa-obsolete.*restart OpenCode"):
        validate_packaged_skill_catalog(catalog)


@pytest.mark.parametrize("malformed", [None, {}, {"name": 42}])
def test_packaged_skill_catalog_rejects_malformed_entries(malformed: object) -> None:
    catalog: list[object] = [*_packaged_catalog(), malformed]

    with pytest.raises(DriverError, match=r"malformed.*restart OpenCode"):
        validate_packaged_skill_catalog(catalog)


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("---\nname: [\ndescription: broken\n---\nbody\n", "invalid YAML"),
        (
            "---\nname: aa-case-design\nname: aa-case-design\ndescription: valid\n---\nbody\n",
            "invalid YAML",
        ),
        ("---\nname: aa-wrong\ndescription: valid\n---\nbody\n", "name.*aa-wrong"),
        ("---\nname: aa-case-design\n---\nbody\n", "description"),
        ("---\nname: aa-case-design\ndescription: valid\n---\n", "empty body"),
    ],
)
def test_packaged_skill_catalog_rejects_malformed_packaged_contract(
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
    message: str,
) -> None:
    catalog = _packaged_catalog()
    read_text = opencode_adapter.resources.read_text

    def malformed_case_design(*parts: str) -> str:
        if parts == ("skills", "aa-case-design", "SKILL.md"):
            return replacement
        return read_text(*parts)

    monkeypatch.setattr(opencode_adapter.resources, "read_text", malformed_case_design)

    with pytest.raises(DriverError, match=rf"aa-case-design.*{message}.*restart OpenCode"):
        validate_packaged_skill_catalog(catalog)


def test_packaged_skill_server_reads_live_skill_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, dict[str, str] | None]] = []

    class FakeClient:
        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(
            self,
            url: str,
            *,
            params: dict[str, str] | None = None,
            headers: dict[str, str],
        ) -> opencode_adapter.httpx.Response:
            del headers
            seen.append((url, params))
            if url.endswith("/path"):
                return opencode_adapter.httpx.Response(200, json={"directory": "/sut"})
            if url.endswith("/experimental/tool/ids"):
                return opencode_adapter.httpx.Response(200, json=[_packaged_boundary_tool_id()])
            return opencode_adapter.httpx.Response(200, json=_packaged_catalog())

    monkeypatch.setattr(opencode_adapter.httpx, "Client", lambda *, timeout: FakeClient())

    validate_packaged_skill_server("http://opencode.example/", "/sut")

    assert seen == [
        ("http://opencode.example/path", None),
        ("http://opencode.example/experimental/tool/ids", {"directory": "/sut"}),
        ("http://opencode.example/skill", {"directory": "/sut"}),
    ]


@pytest.mark.parametrize(
    "live_tools",
    [
        [],
        ["aa_boundary_probe_v1_" + "0" * 64],
        [_packaged_boundary_tool_id(), "aa_boundary_probe_v1_" + "0" * 64],
    ],
)
def test_packaged_skill_server_rejects_missing_or_stale_live_boundary_plugin(
    monkeypatch: pytest.MonkeyPatch,
    live_tools: list[str],
) -> None:
    class FakeClient:
        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(
            self,
            url: str,
            *,
            params: dict[str, str] | None = None,
            headers: dict[str, str],
        ) -> opencode_adapter.httpx.Response:
            del params, headers
            if url.endswith("/path"):
                return opencode_adapter.httpx.Response(200, json={"directory": "/sut"})
            if url.endswith("/experimental/tool/ids"):
                return opencode_adapter.httpx.Response(200, json=live_tools)
            raise AssertionError(f"preflight must stop before skill validation: {url}")

    monkeypatch.setattr(opencode_adapter.httpx, "Client", lambda *, timeout: FakeClient())

    with pytest.raises(
        DriverError,
        match=r"live AA boundary plugin.*restart OpenCode",
    ):
        validate_packaged_skill_server("http://opencode.example", "/sut")


def test_packaged_skill_server_rejects_omo_server_working_directory_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    class FakeClient:
        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(
            self,
            url: str,
            *,
            params: dict[str, str] | None = None,
            headers: dict[str, str],
        ) -> opencode_adapter.httpx.Response:
            del params, headers
            seen.append(url)
            return opencode_adapter.httpx.Response(200, json={"directory": "/shared-server-cwd"})

    monkeypatch.setattr(opencode_adapter.httpx, "Client", lambda *, timeout: FakeClient())

    with pytest.raises(
        DriverError,
        match=r"server working directory.*shared-server-cwd.*restart OpenCode",
    ):
        validate_packaged_skill_server("http://opencode.example", "/sut")

    assert seen == ["http://opencode.example/path"]
