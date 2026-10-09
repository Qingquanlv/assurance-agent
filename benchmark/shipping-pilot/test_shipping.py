"""Acceptance over real HTTP, with real databases on both sides."""

import concurrent.futures
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

PILOT = Path(__file__).resolve().parent
SUT = PILOT.parent / "vue-fastapi-admin"
TOKEN = "test-carrier-service-secret"
PARCEL = {
    "recipient": "试点收件人",
    "phone": "13800138000",
    "address": "上海市测试路 1 号",
    "item_name": "测试文具",
    "quantity": 2,
}


def environment(runtime, carrier_url="http://127.0.0.1:1"):
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(SUT), str(PILOT)]),
        "SECRET_KEY": "shipping-test-jwt-secret",
        "CARRIER_TOKEN": TOKEN,
        "CARRIER_URL": carrier_url,
        "CARRIER_TIMEOUT": "0.1",
        "CARRIER_DB": str(runtime / "carrier.sqlite3"),
        "TORTOISE_ORM": json.dumps(
            {
                "connections": {"sqlite": f"sqlite://{runtime / 'main.sqlite3'}"},
                "apps": {
                    "models": {"models": ["app.models", "aerich.models"], "default_connection": "sqlite"}
                },
                "use_tz": False,
                "timezone": "Asia/Shanghai",
            }
        ),
    }


@contextmanager
def serve(module, env, runtime, name):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log_path = runtime / f"{name}.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(port)],
            cwd=runtime,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10) as client:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    assert process.poll() is None, log_path.read_text()
                    try:
                        if client.get("/openapi.json").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.05)
                else:
                    pytest.fail(log_path.read_text())
                yield client
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def ok(response):
    assert response.status_code == 200, response.text
    assert response.json()["code"] == 200, response.text
    return response.json()["data"]


def login(client, username="admin", password="123456"):
    return {
        "token": ok(
            client.post("/api/v1/base/access_token", json={"username": username, "password": password})
        )["access_token"]
    }


def create(client, headers, **values):
    return ok(
        client.post(
            "/api/v1/shipping/create", headers=headers, json={"client_ref": str(uuid4()), **PARCEL, **values}
        )
    )


def act(client, headers, order, action):
    return ok(client.post(f"/api/v1/shipping/{action}", headers=headers, params={"id": order["id"]}))


def test_draft_creation_without_carrier_preserves_existing_business(tmp_path):
    with serve("app:app", environment(tmp_path), tmp_path, "main") as main:
        auth = login(main)
        ok(main.get("/api/v1/dept/list", headers=auth))
        order = create(main, auth)
        assert order["status"] == "DRAFT"
        assert order["waybill_no"] is None
        rows = ok(main.get("/api/v1/shipping/list", headers=auth))
        assert [row["id"] for row in rows] == [order["id"]]
        assert act(main, auth, order, "submit")["status"] == "UNKNOWN"
        ok(main.get("/api/v1/dept/list", headers=auth))


@pytest.fixture
def live(tmp_path):
    env = environment(tmp_path)
    env["CARRIER_TIMEOUT"] = "2"
    with serve("logistics_service:app", env, tmp_path, "carrier") as carrier:
        env["CARRIER_URL"] = str(carrier.base_url)
        with serve("app:app", env, tmp_path, "main") as main:
            yield main, carrier


def test_real_carrier_and_duplicate_submission(live):
    main, carrier = live
    auth = login(main)
    order = create(main, auth)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: act(main, auth, order, "submit"), range(4)))
    final = act(main, auth, order, "reconcile")
    assert final["status"] == "ACCEPTED"
    assert all(row["status"] in ("PENDING", "ACCEPTED") for row in results)
    assert act(main, auth, order, "submit")["waybill_no"] == final["waybill_no"]
    parcel = carrier.get(
        f"/shipments/{order['order_no']}", headers={"Authorization": f"Bearer {TOKEN}"}
    ).json()
    assert parcel["waybill_no"] == final["waybill_no"]
    assert parcel["reference"] == order["order_no"]
    assert final["events"][-1]["status"] == "ACCEPTED"


def test_real_business_rejection(live):
    main, _ = live
    auth = login(main)
    result = act(main, auth, create(main, auth, quantity=101), "submit")
    assert result["status"] == "REJECTED"
    assert result["waybill_no"] is None
    assert "100" in result["reason"]


def test_carrier_auth_and_idempotency(live):
    _, carrier = live
    payload = {"reference": "SP-test-one", **PARCEL}
    headers = {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": payload["reference"]}
    assert carrier.post("/shipments", json=payload).status_code == 401
    first = carrier.post("/shipments", headers=headers, json=payload)
    assert first.status_code == 200
    assert carrier.post("/shipments", headers=headers, json=payload).json() == first.json()
    assert carrier.post("/shipments", headers=headers, json={**payload, "quantity": 3}).status_code == 409


def test_create_idempotency_and_validation(live):
    main, _ = live
    auth = login(main)
    ref = str(uuid4())
    first = create(main, auth, client_ref=ref)
    assert create(main, auth, client_ref=ref)["id"] == first["id"]
    assert (
        main.post(
            "/api/v1/shipping/create", headers=auth, json={"client_ref": ref, **PARCEL, "quantity": 3}
        ).status_code
        == 409
    )
    for invalid in ({"quantity": 0}, {"quantity": True}, {"recipient": "   "}, {"phone": "bad"}):
        assert (
            main.post(
                "/api/v1/shipping/create",
                headers=auth,
                json={"client_ref": str(uuid4()), **PARCEL, **invalid},
            ).status_code
            == 422
        )


@contextmanager
def mock_carrier(replies):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.respond()

        def do_GET(self):
            self.respond()

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            payload = json.loads(body) if body else {}
            calls.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "body": payload,
                    "key": self.headers.get("Idempotency-Key"),
                    "auth": self.headers.get("Authorization"),
                }
            )
            status, result, delay = replies[min(len(calls) - 1, len(replies) - 1)]
            if callable(result):
                result = result(payload, self.path)
            time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if isinstance(result, bytes):
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            try:
                self.wfile.write(result if isinstance(result, bytes) else json.dumps(result).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def accepted(payload, path):
    return {
        "reference": payload.get("reference") or path.rsplit("/", 1)[-1],
        "waybill_no": "WB-MOCK-001",
        "status": "ACCEPTED",
    }


@pytest.mark.parametrize(
    "replies,expected,count",
    [
        ([(503, {}, 0), (503, {}, 0), (200, accepted, 0)], "ACCEPTED", 3),
        ([(503, {}, 0)], "UNKNOWN", 3),
        ([(200, accepted, 0.3)], "UNKNOWN", 3),
        ([(422, {"code": "QUANTITY_LIMIT", "message": "最多100件"}, 0)], "REJECTED", 1),
        ([(401, {}, 0)], "UNKNOWN", 1),
        ([(302, {}, 0)], "UNKNOWN", 1),
        ([(200, {"reference": "wrong", "waybill_no": "WB-1", "status": "ACCEPTED"}, 0)], "UNKNOWN", 1),
        ([(200, {}, 0)], "UNKNOWN", 1),
    ],
)
def test_faults_change_business_state(tmp_path, replies, expected, count):
    with mock_carrier(replies) as (url, calls):
        with serve("app:app", environment(tmp_path, url), tmp_path, "main") as main:
            auth = login(main)
            order = create(main, auth)
            result = act(main, auth, order, "submit")
            assert result["status"] == expected
            assert len(calls) == count
            assert {call["key"] for call in calls} == {order["order_no"]}
            assert all(call["auth"] == f"Bearer {TOKEN}" for call in calls)
            assert len({json.dumps(call["body"], sort_keys=True) for call in calls}) == 1
            if expected != "ACCEPTED":
                assert result["waybill_no"] is None
                assert result["reason"]


def test_reconcile_after_lost_response(tmp_path):
    with mock_carrier([(200, accepted, 0.3)] * 3 + [(200, accepted, 0)]) as (url, calls):
        with serve("app:app", environment(tmp_path, url), tmp_path, "main") as main:
            auth = login(main)
            order = create(main, auth)
            assert act(main, auth, order, "submit")["status"] == "UNKNOWN"
            assert act(main, auth, order, "reconcile")["status"] == "ACCEPTED"
            assert calls[-1]["method"] == "GET"
            assert act(main, auth, order, "submit")["status"] == "ACCEPTED"
            assert len(calls) == 4


def test_real_carrier_commit_before_response_loss(tmp_path):
    env = environment(tmp_path)
    with serve("logistics_service:app", env, tmp_path, "carrier") as carrier:

        def forward(payload, path):
            headers = {"Authorization": f"Bearer {TOKEN}"}
            if payload:
                headers["Idempotency-Key"] = payload["reference"]
                response = carrier.post(path, headers=headers, json=payload)
            else:
                response = carrier.get(path, headers=headers)
            assert response.status_code == 200
            return response.json()

        with mock_carrier([(200, forward, 0.3)] * 3 + [(200, forward, 0)]) as (url, calls):
            env["CARRIER_URL"] = url
            with serve("app:app", env, tmp_path, "main") as main:
                auth = login(main)
                order = create(main, auth)
                assert act(main, auth, order, "submit")["status"] == "UNKNOWN"
                result = act(main, auth, order, "reconcile")
                assert result["status"] == "ACCEPTED"
                with sqlite3.connect(tmp_path / "carrier.sqlite3") as db:
                    assert db.execute("SELECT COUNT(*) FROM shipment").fetchone()[0] == 1
                    assert db.execute("SELECT waybill_no FROM shipment").fetchone()[0] == result["waybill_no"]
                assert len(calls) == 4


def test_corrupt_compression_becomes_unknown_and_releases_lease(tmp_path):
    with mock_carrier([(200, b"not-a-gzip-stream", 0), (200, accepted, 0)]) as (url, calls):
        with serve("app:app", environment(tmp_path, url), tmp_path, "main") as main:
            auth = login(main)
            order = create(main, auth)
            result = act(main, auth, order, "submit")
            assert result["status"] == "UNKNOWN"
            assert not result["busy"]
            assert len(calls) == 1
            assert act(main, auth, order, "reconcile")["status"] == "ACCEPTED"


def test_carrier_persists_waybill_after_restart(tmp_path):
    env = environment(tmp_path)
    payload = {"reference": "SP-persisted", **PARCEL}
    headers = {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": payload["reference"]}
    with serve("logistics_service:app", env, tmp_path, "carrier-first") as carrier:
        first = carrier.post("/shipments", headers=headers, json=payload).json()
    with serve("logistics_service:app", env, tmp_path, "carrier-second") as carrier:
        assert carrier.get("/shipments/SP-persisted", headers=headers).json() == first
        assert carrier.post("/shipments", headers=headers, json=payload).json() == first


def test_expired_operation_and_menu_grants_survive_restart(tmp_path):
    env = environment(tmp_path)
    with mock_carrier([(200, accepted, 0)]) as (url, calls):
        env["CARRIER_URL"] = url
        with serve("app:app", env, tmp_path, "main-first") as main:
            auth = login(main)
            order = create(main, auth)
            roles = ok(main.get("/api/v1/role/list", headers=auth))
            role_id = next(role["id"] for role in roles if role["name"] == "普通用户")
            ok(
                main.post(
                    "/api/v1/user/create",
                    headers=auth,
                    json={
                        "username": "reader",
                        "password": "123456",
                        "email": "reader@example.com",
                        "is_active": True,
                        "is_superuser": False,
                        "role_ids": [role_id],
                    },
                )
            )
            reader = login(main, "reader")
            assert any(
                menu["path"] == "/shipping" for menu in ok(main.get("/api/v1/base/usermenu", headers=reader))
            )
            ok(main.get("/api/v1/shipping/list", headers=reader))
            assert (
                main.post("/api/v1/shipping/submit", params={"id": order["id"]}, headers=reader).status_code
                == 403
            )
            with sqlite3.connect(tmp_path / "main.sqlite3") as db:
                # Reproduce the durable record left by a killed worker, with its lease expired.
                db.execute(
                    "UPDATE shipping_order SET status='PENDING', operation_token='crashed', lease_until=1 WHERE id=?",
                    (order["id"],),
                )
                before_menus = db.execute("SELECT COUNT(*) FROM menu").fetchone()[0]
            # Explicit revocation must not be undone by startup seeding.
            ok(
                main.post(
                    "/api/v1/role/authorized",
                    headers=auth,
                    json={"id": role_id, "menu_ids": [], "api_infos": []},
                )
            )
        with serve("app:app", env, tmp_path, "main-second") as main:
            result = act(main, login(main), order, "reconcile")
            assert result["status"] == "ACCEPTED"
            assert len(calls) == 1
            assert main.get("/api/v1/shipping/list", headers=login(main, "reader")).status_code == 403
            with sqlite3.connect(tmp_path / "main.sqlite3") as db:
                assert db.execute("SELECT COUNT(*) FROM menu").fetchone()[0] == before_menus


@pytest.mark.parametrize("time_wait", [False, True], ids=["normal", "time-wait"])
def test_launcher_restart_and_cleanup(tmp_path, time_wait):
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        backend_port, carrier_port = first.getsockname()[1], second.getsockname()[1]
    if time_wait:
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", backend_port))
            listener.listen()
            with socket.create_connection(("127.0.0.1", backend_port)) as peer:
                accepted_socket, _ = listener.accept()
                accepted_socket.close()
                assert peer.recv(1) == b""
    command = [
        sys.executable,
        str(PILOT / "run.py"),
        "--runtime",
        str(tmp_path),
        "--backend-port",
        str(backend_port),
        "--carrier-port",
        str(carrier_port),
    ]
    for launch in range(2):
        log_path = tmp_path / f"launcher-{launch}.log"
        with log_path.open("w") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 20
                while "Backend:" not in log_path.read_text():
                    assert process.poll() is None, log_path.read_text()
                    assert time.monotonic() < deadline, log_path.read_text()
                    time.sleep(0.05)
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{backend_port}", trust_env=False, timeout=10
                ) as main:
                    auth = login(main)
                    if launch == 0:
                        order = create(main, auth)
                        first_result = act(main, auth, order, "submit")
                        assert first_result["status"] == "ACCEPTED"
                    else:
                        result = act(main, auth, order, "submit")
                        assert result["waybill_no"] == first_result["waybill_no"]
                    assert (tmp_path / "credentials.json").stat().st_mode & 0o777 == 0o600
            finally:
                process.terminate()
                process.wait(timeout=15)
        assert process.returncode == 0
        for port in (backend_port, carrier_port):
            with socket.socket() as sock:
                assert sock.connect_ex(("127.0.0.1", port)) != 0


def test_upgrade_existing_database_preserves_business_and_adds_shipping(tmp_path):
    legacy = tmp_path / "legacy-source"
    shutil.copytree(SUT / "app", legacy / "app", ignore=shutil.ignore_patterns("__pycache__", "logs"))
    subprocess.run(
        ["git", "apply", "--no-index", "--reverse", "--include=app/*", str(PILOT / "shipping.patch")],
        cwd=legacy,
        check=True,
        capture_output=True,
    )
    env = environment(tmp_path)
    env["PYTHONPATH"] = str(legacy)
    with serve("app:app", env, tmp_path, "legacy") as main:
        auth = login(main)
        assert main.get("/api/v1/shipping/list", headers=auth).status_code == 404
        ok(main.post("/api/v1/dept/create", headers=auth, json={"name": "升级前部门"}))
        roles = ok(main.get("/api/v1/role/list", headers=auth))
        role_id = next(role["id"] for role in roles if role["name"] == "普通用户")
        ok(
            main.post(
                "/api/v1/user/create",
                headers=auth,
                json={
                    "username": "existing-reader",
                    "password": "123456",
                    "email": "existing@example.com",
                    "is_active": True,
                    "is_superuser": False,
                    "role_ids": [role_id],
                },
            )
        )
    env["PYTHONPATH"] = os.pathsep.join([str(SUT), str(PILOT)])
    with serve("app:app", env, tmp_path, "upgraded") as main:
        auth = login(main)
        assert ok(main.get("/api/v1/dept/list", headers=auth))[0]["name"] == "升级前部门"
        assert create(main, auth)["status"] == "DRAFT"
        reader = login(main, "existing-reader")
        menus = ok(main.get("/api/v1/base/usermenu", headers=reader))
        assert sum(menu["path"] == "/shipping" for menu in menus) == 1
        ok(main.get("/api/v1/shipping/list", headers=reader))
        assert (
            main.post(
                "/api/v1/shipping/create", headers=reader, json={"client_ref": str(uuid4()), **PARCEL}
            ).status_code
            == 403
        )
