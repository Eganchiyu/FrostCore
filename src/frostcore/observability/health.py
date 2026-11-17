"""Out-of-loop HTTP health probe server supporting multi-bot aggregation."""
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from frostcore.contracts.probe import AppHealthSnapshot, HealthReporter

DRY_RUN_ENV_VAR = "FROSTCORE_DRY_RUN"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def is_dry_run(env_var: str = DRY_RUN_ENV_VAR) -> bool:
    """Return True if dry-run mode is requested via environment variable."""
    return os.getenv(env_var, "").strip().lower() in _TRUE_VALUES


class HealthProbeServer:
    """Standard-library threaded HTTP /health probe decoupled from asyncio event loops.

    Supports registering multiple FrostApp instances (single-process multi-bot).
    Returns HTTP 200 only when at least one reporter is registered (or empty_ready=True)
    and all registered reporters report ready=True; otherwise HTTP 503.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8778,
        *,
        empty_ready: bool = False,
        dry_run_env_var: str = DRY_RUN_ENV_VAR,
    ) -> None:
        self.host = host
        self.requested_port = port
        self.empty_ready = empty_ready
        self.dry_run_env_var = dry_run_env_var
        self._started_at = time.time()
        self._lock = threading.Lock()
        self._reporters: dict[str, HealthReporter] = {}
        self._notify_handler: Callable[[dict[str, Any]], tuple[bool, str]] | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._server is not None:
            return int(self._server.server_address[1])
        return self.requested_port

    def register(self, app_id: str, reporter: HealthReporter) -> None:
        with self._lock:
            self._reporters[str(app_id)] = reporter

    def unregister(self, app_id: str) -> None:
        with self._lock:
            self._reporters.pop(str(app_id), None)

    def set_notifier(
        self,
        fn: Callable[[dict[str, Any]], tuple[bool, str]] | None,
    ) -> None:
        with self._lock:
            self._notify_handler = fn

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            reporters = dict(self._reporters)

        apps: dict[str, dict[str, Any]] = {}
        all_ready = bool(reporters) or self.empty_ready
        for app_id, reporter in reporters.items():
            try:
                snap: AppHealthSnapshot = reporter.health_snapshot()
                apps[app_id] = snap.model_dump(mode="json")
                if not snap.ready:
                    all_ready = False
            except Exception as exc:
                all_ready = False
                apps[app_id] = {"app_id": app_id, "ready": False, "probe_error": str(exc)}

        return {
            "status": "ok" if all_ready else "starting",
            "ready": all_ready,
            "pid": os.getpid(),
            "uptime": round(time.time() - self._started_at, 2),
            "dry_run": is_dry_run(self.dry_run_env_var),
            "apps": apps,
        }

    def start(self) -> int:
        """Start the probe server idempotently and return the bound port."""
        with self._lock:
            if self._server is not None:
                return int(self._server.server_address[1])

            probe = self

            class _Handler(BaseHTTPRequestHandler):
                def do_GET(self) -> None:
                    path = self.path.split("?", 1)[0]
                    if path not in ("/health", "/"):
                        self.send_response(404)
                        self.end_headers()
                        return
                    payload = probe.snapshot()
                    code = 200 if payload["ready"] else 503
                    self._send_json(code, payload)

                def do_POST(self) -> None:
                    path = self.path.split("?", 1)[0]
                    if path != "/notify":
                        self.send_response(404)
                        self.end_headers()
                        return
                    with probe._lock:
                        handler = probe._notify_handler
                    if handler is None:
                        self._send_json(503, {"ok": False, "detail": "notifier_not_registered"})
                        return
                    try:
                        length = int(self.headers.get("Content-Length") or 0)
                        raw = self.rfile.read(length) if length else b"{}"
                        body = json.loads(raw or b"{}")
                        if not isinstance(body, dict):
                            raise ValueError("payload must be JSON object")
                    except Exception as exc:
                        self._send_json(400, {"ok": False, "detail": f"invalid_payload: {exc}"})
                        return
                    try:
                        ok, detail = handler(body)
                        self._send_json(200 if ok else 500, {"ok": bool(ok), "detail": str(detail)})
                    except Exception as exc:
                        self._send_json(500, {"ok": False, "detail": str(exc)})

                def _send_json(self, code: int, payload: dict[str, Any]) -> None:
                    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)

                def log_message(self, format: str, *args: Any) -> None:
                    return None

            server = ThreadingHTTPServer((self.host, self.requested_port), _Handler)
            server.daemon_threads = True
            thread = threading.Thread(
                target=server.serve_forever,
                name="frostcore-health-probe",
                daemon=True,
            )
            thread.start()
            self._server = server
            self._thread = thread
            return int(server.server_address[1])

    def stop(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)


__all__ = [
    "DRY_RUN_ENV_VAR",
    "HealthProbeServer",
    "is_dry_run",
]
