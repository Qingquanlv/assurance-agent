"""Run the real main app and Carrier Service with an isolated persistent DB."""

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx


PILOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sut", type=Path, default=PILOT.parent / "vue-fastapi-admin")
    parser.add_argument("--runtime", type=Path, default=PILOT / "results" / "local")
    parser.add_argument("--backend-port", type=int, default=19999)
    parser.add_argument("--carrier-port", type=int, default=20000)
    parser.add_argument("--frontend-port", type=int, default=13100)
    parser.add_argument(
        "--carrier-url", help="Use an external carrier or HTTP Mock instead of starting the local carrier"
    )
    parser.add_argument("--frontend", action="store_true", help="Also launch the existing Vue/Vite app")
    args = parser.parse_args()
    sut, runtime = args.sut.resolve(), args.runtime.resolve()
    if not (sut / "app" / "api" / "v1" / "shipping.py").is_file():
        parser.error("Run prepare.py first")
    ports = (
        [args.backend_port]
        + ([] if args.carrier_url else [args.carrier_port])
        + ([args.frontend_port] if args.frontend else [])
    )
    if len(set(ports)) != len(ports) or any(not 0 < port < 65536 for port in ports):
        parser.error("Ports must be distinct and in 1..65535")
    for port in ports:
        with socket.socket() as sock:
            # Match Uvicorn's bind behavior: permit TIME_WAIT, not an active listener.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
    vite = sut / "web" / "node_modules" / "vite" / "bin" / "vite.js"
    if args.frontend and not vite.exists():
        parser.error("Install the SUT's frontend dependencies first: pnpm install --frozen-lockfile")
    runtime.mkdir(parents=True, exist_ok=True)
    credentials_path = runtime / "credentials.json"
    if not credentials_path.exists():
        with open(credentials_path, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
            json.dump({"SECRET_KEY": secrets.token_hex(32), "CARRIER_TOKEN": secrets.token_hex(32)}, stream)
    credentials = json.loads(credentials_path.read_text())
    db = runtime / "pilot.sqlite3"
    carrier_url = args.carrier_url or f"http://127.0.0.1:{args.carrier_port}"
    backend_url = f"http://127.0.0.1:{args.backend_port}"
    env = {
        **os.environ,
        **credentials,
        "PYTHONPATH": os.pathsep.join([str(sut), str(PILOT)]),
        "CARRIER_DB": str(runtime / "carrier.sqlite3"),
        "CARRIER_URL": carrier_url,
        "CARRIER_TIMEOUT": "2",
        "PILOT_BACKEND_URL": backend_url,
        "BROWSER": "none",
        "TORTOISE_ORM": json.dumps(
            {
                "connections": {"sqlite": f"sqlite://{db}"},
                "apps": {
                    "models": {"models": ["app.models", "aerich.models"], "default_connection": "sqlite"}
                },
                "use_tz": False,
                "timezone": "Asia/Shanghai",
            }
        ),
    }
    processes, logs = [], []

    def start(name, command, cwd):
        log = (runtime / f"{name}.log").open("a")
        logs.append(log)
        processes.append(subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT))

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        for name, module, port in (
            ("carrier", "logistics_service:app", args.carrier_port),
            ("main", "app:app", args.backend_port),
        ):
            if name == "carrier" and args.carrier_url:
                continue
            start(
                name,
                [sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(port)],
                runtime,
            )
        with httpx.Client(trust_env=False, timeout=1) as client:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if any(process.poll() is not None for process in processes):
                    raise RuntimeError(f"A service exited; inspect logs in {runtime}")
                try:
                    main_ready = client.get(f"{backend_url}/openapi.json").status_code == 200
                    carrier_ready = (
                        bool(args.carrier_url)
                        or client.get(
                            f"{carrier_url}/health",
                            headers={"Authorization": f"Bearer {credentials['CARRIER_TOKEN']}"},
                        ).status_code
                        == 200
                    )
                    if main_ready and carrier_ready:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError(f"Services did not become ready; inspect logs in {runtime}")
        if args.frontend:
            start(
                "frontend",
                ["node", str(vite), "--host", "127.0.0.1", "--port", str(args.frontend_port), "--strictPort"],
                sut / "web",
            )
            print(f"Frontend: http://127.0.0.1:{args.frontend_port}", flush=True)
        print(f"Backend: {backend_url}\nCarrier service: {carrier_url}\nDatabase: {db}", flush=True)
        print("Fresh DB login: admin / 123456 (local pilot only). Ctrl-C stops all services.", flush=True)
        while True:
            if any(process.poll() is not None for process in processes):
                raise RuntimeError(f"A service exited; inspect logs in {runtime}")
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for process in reversed(processes):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
