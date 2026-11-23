#!/usr/bin/env python3
"""TAN Gemini Core — Local HTTP Webhook & IPC Wakeup Bridge Daemon (v5.0).

Binds strictly to loopback (`127.0.0.1:18790`) using Python 3.12 stdlib only.
Exposes a clean local HTTP bridge so any cron job, filesystem watcher, marketplace
monitor, or local script can inspect the active Gemini session bridge OR immediately
wake the live Antigravity Gemini conversation by enqueuing an inbox event and
signaling the active `session_bridge.py wait` waiter with `SIGUSR1`.

Endpoints:
  GET  /health  -> {"ok": true, "service": "tan-gemini-bridge", "waiter_alive": ..., "wake_count": ...}
  GET  /status  -> full `memory/session_bridge_state.json` + inbox depth
  GET  /capsule -> cold-start `memory/session_capsule.json`
  POST /wake    -> {"source": "...", "reason": "...", "priority": "normal|high", "payload": {...}}
                   enqueues event into `memory/wake_inbox/` & sends SIGUSR1 to active waiter
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

import session_bridge  # noqa: E402


def _pid_alive(pid: Optional[int]) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def make_handler(paths: session_bridge.BridgePaths):
    class BridgeHTTPHandler(BaseHTTPRequestHandler):
        server_version = "TAN-Gemini-Bridge/5.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
            # Compact structured stderr logging
            sys.stderr.write(f"[bridge-http] {self.address_string()} - {fmt % args}\n")
            sys.stderr.flush()

        def _send_json(self, status: int, body: Dict[str, Any]) -> None:
            raw = (json.dumps(body, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            route = self.path.split("?", 1)[0]
            if route == "/health":
                state = session_bridge.load_bridge_state(paths)
                waiter = state.get("active_waiter") or {}
                inbox_count = len(session_bridge.list_inbox_events(paths))
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "service": "tan-gemini-session-bridge",
                        "timestamp_utc": session_bridge.utc_iso(),
                        "session_id": state.get("session_id"),
                        "wake_count": state.get("wake_count", 0),
                        "consecutive_autonomous_wakes": state.get("consecutive_autonomous_wakes", 0),
                        "last_wake_id": state.get("last_wake_id"),
                        "waiter_pid": waiter.get("pid"),
                        "waiter_alive": _pid_alive(waiter.get("pid")),
                        "pending_inbox_events": inbox_count,
                    },
                )
                return

            if route == "/status":
                state = session_bridge.load_bridge_state(paths)
                waiter = state.get("active_waiter") or {}
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "state": state,
                        "waiter_alive": _pid_alive(waiter.get("pid")),
                        "pending_inbox_events": [ev for _, ev in session_bridge.list_inbox_events(paths)],
                    },
                )
                return

            if route == "/capsule":
                capsule = session_bridge.build_session_capsule(paths)
                self._send_json(HTTPStatus.OK, capsule)
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"unknown route: {route}"})

        def do_POST(self) -> None:
            route = self.path.split("?", 1)[0]
            if route != "/wake":
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"unknown route: {route}"})
                return

            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > 65536:
                self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "payload too large"})
                return

            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(body, dict):
                    raise ValueError("JSON body must be an object")
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"invalid JSON: {exc}"})
                return

            source = str(body.get("source") or "http_webhook")[:80]
            reason = str(body.get("reason") or "Webhook wake trigger")[:400]
            priority = "high" if str(body.get("priority") or "").lower() == "high" else "normal"
            payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}

            res = session_bridge.enqueue_inbox_event(
                paths=paths,
                source=source,
                reason=reason,
                priority=priority,
                payload=payload,
                signal_waiter=True,
            )
            self._send_json(HTTPStatus.ACCEPTED, res)

    return BridgeHTTPHandler


def create_server(host: str, port: int, root: Path) -> ThreadingHTTPServer:
    paths = session_bridge.BridgePaths(root)
    paths.ensure_dirs()
    ThreadingHTTPServer.allow_reuse_address = True
    return ThreadingHTTPServer((host, port), make_handler(paths))


def main() -> int:
    ap = argparse.ArgumentParser(description="TAN Gemini Core — Local HTTP Wakeup Bridge Daemon")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18790)
    ap.add_argument("--root", default=str(session_bridge.DEFAULT_ROOT))
    args = ap.parse_args()

    server = create_server(args.host, args.port, Path(args.root))
    bound_host, bound_port = server.server_address[:2]

    def _shutdown(_sig: int, _frame: object) -> None:
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(
        json.dumps(
            {
                "status": "listening",
                "url": f"http://{bound_host}:{bound_port}",
                "pid": os.getpid(),
                "root": str(Path(args.root).resolve()),
                "started_at_utc": session_bridge.utc_iso(),
            }
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
