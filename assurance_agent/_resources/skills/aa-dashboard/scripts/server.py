#!/usr/bin/env python3
"""Single-file static Case Center server (stdlib http.server) — Python replacement
for the former Node server.cjs. Serves qa/cases and qa/changes YAML to the SPA.

Env (all optional):
  QA_DASHBOARD_PORT         bind port (default: random 49152-65534)
  QA_DASHBOARD_HOST         bind host (default 127.0.0.1)
  QA_DASHBOARD_URL_HOST     host shown in the printed URL (default localhost)
  QA_DASHBOARD_DIR          session dir (default /tmp/qa-dashboard)
  QA_DASHBOARD_PROJECT_DIR  project root containing qa/ (default cwd)
  QA_DASHBOARD_OWNER_PID    parent pid; server exits when it dies
"""

from __future__ import annotations

import json
import os
import random
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("QA_DASHBOARD_PORT") or (49152 + random.randint(0, 16382)))
HOST = os.environ.get("QA_DASHBOARD_HOST", "127.0.0.1")
URL_HOST = os.environ.get("QA_DASHBOARD_URL_HOST") or ("localhost" if HOST == "127.0.0.1" else HOST)
SESSION_DIR = Path(os.environ.get("QA_DASHBOARD_DIR", "/tmp/qa-dashboard"))
STATE_DIR = SESSION_DIR / "state"
PROJECT_DIR = Path(os.environ.get("QA_DASHBOARD_PROJECT_DIR", os.getcwd())).resolve()
OWNER_PID = int(os.environ["QA_DASHBOARD_OWNER_PID"]) if os.environ.get("QA_DASHBOARD_OWNER_PID") else None
CASE_CENTER_HTML = Path(__file__).resolve().parent / "case-center.html"
IDLE_TIMEOUT_S = 60 * 60
CHANGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_last_activity = time.time()


def _walk_case_yaml(root: Path) -> list[str]:
    out: list[str] = []
    if not root.exists():
        return out
    for path in sorted(root.rglob("case.yaml")):
        out.append(str(path.relative_to(PROJECT_DIR)))
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: object) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(data).encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        global _last_activity
        _last_activity = time.time()
        parsed = urlparse(self.path)
        pathname = parsed.path

        if pathname == "/cases":
            if not CASE_CENTER_HTML.is_file():
                self._send(500, "text/plain", b"case-center.html not found")
                return
            self._send(200, "text/html; charset=utf-8", CASE_CENTER_HTML.read_bytes())
            return

        if pathname == "/yaml":
            rel = (parse_qs(parsed.query).get("path") or [None])[0]
            if not rel:
                self._json(400, {"error": "Missing path parameter"})
                return
            abs_path = (PROJECT_DIR / rel).resolve()
            if abs_path != PROJECT_DIR and PROJECT_DIR not in abs_path.parents:
                self._json(403, {"error": "Path traversal not allowed"})
                return
            if not abs_path.is_file():
                self._json(404, {"error": "File not found"})
                return
            self._send(200, "text/plain; charset=utf-8", abs_path.read_bytes())
            return

        if pathname == "/api/cases":
            self._json(200, {"files": _walk_case_yaml(PROJECT_DIR / "qa" / "cases")})
            return

        if pathname == "/api/changes":
            changes_dir = PROJECT_DIR / "qa" / "changes"
            changes = (
                sorted(p.name for p in changes_dir.iterdir() if p.is_dir()) if changes_dir.exists() else []
            )
            self._json(200, {"changes": changes})
            return

        match = re.match(r"^/api/changes/([^/]+)$", pathname)
        if match:
            change_id = match.group(1)
            if not CHANGE_ID_RE.match(change_id):
                self._json(400, {"error": "Invalid changeId"})
                return
            files = _walk_case_yaml(PROJECT_DIR / "qa" / "changes" / change_id / "cases")
            self._json(200, {"changeId": change_id, "files": files})
            return

        self._json(404, {"error": "Not found"})


def _owner_alive() -> bool:
    if OWNER_PID is None:
        return True
    try:
        os.kill(OWNER_PID, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def main() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    info = {
        "type": "server-started",
        "port": PORT,
        "host": HOST,
        "url_host": URL_HOST,
        "url": f"http://{URL_HOST}:{PORT}/cases",
        "project_dir": str(PROJECT_DIR),
        "session_dir": str(SESSION_DIR),
        "state_dir": str(STATE_DIR),
    }
    info_str = json.dumps(info)
    print(info_str, flush=True)
    (STATE_DIR / "server-info").write_text(info_str + "\n", encoding="utf-8")

    def _watchdog() -> None:
        while True:
            time.sleep(60)
            if not _owner_alive() or (time.time() - _last_activity) > IDLE_TIMEOUT_S:
                reason = "owner process exited" if not _owner_alive() else "idle timeout"
                print(json.dumps({"type": "server-stopped", "reason": reason}), flush=True)
                info_file = STATE_DIR / "server-info"
                if info_file.exists():
                    info_file.unlink()
                os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_watchdog, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
