from __future__ import annotations

import importlib.util
import json
import sys

import httpx
import pytest

from tests.product.test_phase5_benchmark_manifest import TEST_RUNTIME_SEED_ROOT


def _load_fixtures(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    for name, relative in (
        ("tests.config", "tests/config.py"),
        ("benchmark_api_fixtures", "tests/api/conftest.py"),
    ):
        spec = importlib.util.spec_from_file_location(name, TEST_RUNTIME_SEED_ROOT / relative)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("fixture_name", "is_superuser", "role_ids"),
    [
        ("limited_role_user_token", False, [41]),
        ("no_role_user_token", False, []),
        ("no_role_superuser_token", True, []),
    ],
)
@pytest.mark.parametrize("login_fails", [False, True])
def test_auth_fixture_provisions_and_cleans_its_identity_without_inherited_tokens(
    monkeypatch: pytest.MonkeyPatch,
    fixture_name: str,
    is_superuser: bool,
    role_ids: list[int],
    login_fails: bool,
) -> None:
    module = _load_fixtures(monkeypatch)
    created: dict[str, object] = {}
    deleted: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        payload = json.loads(request.content) if request.content else {}
        data = None
        if path == "/api/v1/base/access_token":
            assert request.headers.get("token") is None
            assert payload == {key: created[key] for key in ("username", "password")}
            if login_fails:
                return httpx.Response(200, json={"code": 400, "msg": "login rejected"})
            data = {"access_token": "fixture-private-token"}
        else:
            assert request.headers["token"] == "admin-private-token"
            if path == "/api/v1/role/create":
                created["role_name"] = payload["name"]
            elif path == "/api/v1/role/list":
                data = [{"id": 41, "name": created["role_name"]}]
            elif path == "/api/v1/role/authorized":
                assert payload == {"id": 41, "menu_ids": [], "api_infos": []}
            elif path == "/api/v1/user/create":
                assert payload["is_superuser"] is is_superuser
                assert payload["role_ids"] == role_ids
                created.update(payload)
            elif path == "/api/v1/user/list":
                data = [{"id": 73, "email": created["email"]}]
            elif path.endswith("/delete"):
                deleted.append((path, str(request.url.query, "ascii")))
            else:
                pytest.fail(f"unexpected auth setup endpoint: {path}")
        return httpx.Response(200, json={"code": 200, "data": data})

    with httpx.Client(base_url="http://127.0.0.1:9999", transport=httpx.MockTransport(respond)) as client:
        fixture = getattr(module, fixture_name).__wrapped__(client, "admin-private-token")
        if login_fails:
            with pytest.raises(RuntimeError, match="authentication fixture") as error:
                next(fixture)
            assert "private-token" not in str(error.value)
            assert str(created["password"]) not in str(error.value)
        else:
            assert next(fixture) == "fixture-private-token"
            assert deleted == []
            with pytest.raises(StopIteration):
                next(fixture)
    assert deleted == [
        ("/api/v1/user/delete", "user_id=73"),
        *([("/api/v1/role/delete", "role_id=41")] if role_ids else []),
    ]
