"""Mock OpenAI-compat backend for tests (PLAYBOOK B6: warmup/transport tested
against an HTTP mock written in the tests, never against a real provider).

Runs a real ThreadingHTTPServer on 127.0.0.1 so the transport under test goes
through genuine sockets: connection refused, timeouts, HTTP 4xx/5xx, garbage
bodies and header handling are all exercised for real.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def parse_state(body: dict[str, Any]) -> dict[str, Any]:
    """Extract the JSON state block the core puts in the selector user message."""
    decoder = json.JSONDecoder()
    for m in body.get("messages") or []:
        if m.get("role") != "user":
            continue
        content = m.get("content") or ""
        start = content.find("{")
        if start < 0:
            continue
        try:
            # raw_decode: the state JSON is followed by prose ("Pick the effort…"),
            # which plain json.loads would reject as trailing data.
            obj, _end = decoder.raw_decode(content[start:])
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict):
            return obj
    return {}


def openai_reply(level: str, confidence: float = 0.8) -> dict[str, Any]:
    return {
        "id": "mock-1",
        "object": "chat.completion",
        "model": "mock",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant",
                       "content": json.dumps({"effort": level, "confidence": confidence})},
            "finish_reason": "stop",
        }],
    }


class MockBackend:
    """Configurable OpenAI-compat mock.

    Attributes a test can set:
      level            level to return; None (default) echoes the requested level
      raw_content      if set, returned verbatim as message.content (garbage tests)
      status           HTTP status to return instead of 200
      delay_s          sleep before answering (timeout tests)
      known_levels     if not None, palettes containing an unknown level -> 400
      raise_reset      close the connection immediately (connection reset tests)
    """

    def __init__(self, path: str = "/v1/chat/completions"):
        self.path = path
        self.level: str | None = None
        self.raw_content: str | None = None
        self.status: int | None = None
        self.delay_s = 0.0
        self.known_levels: list[str] | None = None
        self.raise_reset = False
        self.responder: Callable[[dict, dict], tuple[int, dict]] | None = None
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.port = 0  # real port assigned by the OS at bind time (F-1)
        self._srv: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        mock = self

        class _H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8"))
                except Exception:  # noqa: BLE001
                    body = {}
                headers = {k.lower(): v for k, v in self.headers.items()}
                with mock._lock:
                    mock.requests.append({
                        "path": self.path, "headers": headers, "body": body,
                        "raw": raw.decode("utf-8", "replace"), "t": time.time(),
                    })
                if self.path != mock.path:
                    self._send(404, {"error": {"message": "no route"}})
                    return
                if mock.raise_reset:
                    self.connection.close()
                    return
                if mock.delay_s:
                    time.sleep(mock.delay_s)
                status, payload = mock._answer(body, headers)
                self._send(status, payload)

            def _send(self, status, payload):
                data = json.dumps(payload).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    # Client gave up first (timeout tests). Expected — keep output clean.
                    pass

            def log_message(self, fmt, *args):
                pass

        self._handler_cls = _H

    def _answer(self, body: dict, headers: dict) -> tuple[int, dict]:
        if self.responder is not None:
            return self.responder(body, headers)
        if self.status is not None:
            return self.status, {"error": {"message": f"mock status {self.status}"}}
        state = parse_state(body)
        palette = state.get("supported_levels") or []
        if self.known_levels is not None:
            bad = [l for l in palette if l not in self.known_levels]
            if bad:
                return 400, {"error": {"message": f"unsupported reasoning level(s): {bad}"}}
        if self.raw_content is not None:
            return 200, {
                "choices": [{"index": 0,
                            "message": {"role": "assistant", "content": self.raw_content}}]
            }
        level = self.level if self.level is not None else (palette[0] if palette else "medium")
        return 200, openai_reply(level)

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "MockBackend":
        # Bind the ephemeral port 0 and read the assigned number back: a
        # pick-then-rebind free_port() here is a TOCTOU race (audit F-1 flake).
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_cls)
        self.port = self._srv.server_address[1]
        self._srv.daemon_threads = True
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *a):
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def last_request(self) -> dict[str, Any]:
        with self._lock:
            return self.requests[-1] if self.requests else {}

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.requests)

    def reset(self) -> None:
        with self._lock:
            self.requests.clear()
