"""jev-router core daemon (SPI v1) — stdlib http.server + threads.

Playbook B1: `jev_core.py` — daemon (http.server + threads). Endpoints per
CONTRACT.md: POST /decide, GET /health, POST /warmup. Every response carries
`X-Router-Protocol: 1`; a request whose header declares a different major is
rejected with 4xx so the host adapter never registers against a mismatched core.

Run:   python -m core.jev_core            (from /workspace/jev-routing)
       JEV_API_URL=http://127.0.0.1:8080/v1 JEV_MODEL=jev-latest python -m core.jev_core
"""
from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .capabilities import CAPABILITIES_FILENAME, CapabilityCache, warmup
from .config import JevConfig, PROTOCOL_VERSION
from .decide import Decider, RequestError
from .receipts import ReceiptWriter

log = logging.getLogger("jev-router.core")

PROTOCOL_HEADER = "X-Router-Protocol"
MAX_BODY_BYTES = 1 << 20  # 1 MiB


class CoreApp:
    """Transport-agnostic request handler: (method, path, headers, body) -> (status, payload).

    Kept separate from BaseHTTPRequestHandler so the whole contract is testable
    without sockets, and so the socket layer stays a thin adapter.

    """
    def __init__(self, cfg: JevConfig, cache: CapabilityCache | None = None,
                 decider: Decider | None = None, receipts: ReceiptWriter | None = None):
        self.cfg = cfg
        self.cache = cache or CapabilityCache(
            os.path.join(cfg.home_path, CAPABILITIES_FILENAME), cfg.cap_ttl_s)
        self.decider = decider or Decider(cfg, self.cache)
        self.receipts = receipts or ReceiptWriter(
            cfg.home_path, cfg.receipt_content, cfg.excerpt_chars, cfg.api_key)

    # -- protocol ---------------------------------------------------------
    def _protocol_ok(self, headers: dict[str, str]) -> tuple[bool, str]:
        raw = None
        for k, v in headers.items():
            if k.lower() == PROTOCOL_HEADER.lower():
                raw = v
                break
        if raw is None:
            return True, ""  # absent header is not a mismatch
        try:
            major = int(str(raw).strip())
        except (TypeError, ValueError):
            return False, f"non-numeric {PROTOCOL_HEADER} {raw!r}"
        if major != self.cfg.protocol_version:
            return False, f"unsupported protocol major {major}"
        return True, ""

    # -- routes -----------------------------------------------------------
    def health(self) -> tuple[int, dict[str, Any]]:
        payload = self.cfg.public_dict()
        payload["ok"] = True
        payload["warmup"] = self.decider.warmup_status()
        payload["protocol_version"] = self.cfg.protocol_version
        return 200, payload

    def warmup_now(self, force: bool = True) -> tuple[int, dict[str, Any]]:
        caps = warmup(self.cfg, self.cache, force=force)
        status = 200 if caps.ok else 503
        return status, {
            "ok": caps.ok,
            "warmup": caps.status,
            "accepted_levels": list(caps.accepted_levels),
            "model": caps.model,
            "backend": "configured" if self.cfg.backend_configured else "not_configured",
        }

    def decide(self, body: Any) -> tuple[int, dict[str, Any]]:
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "bad_request", "detail": "body must be a JSON object"}
        t0_wall = None
        try:
            decision = self.decider.decide(body)
        except RequestError as e:
            return 400, {"ok": False, "error": "bad_request", "detail": str(e)}
        warm_status = self.decider.warmup_status()
        try:
            self.receipts.write(request=body, decision=decision,
                               elapsed_ms=decision.get("elapsed_ms", 0),
                               warmup_status=warm_status)
        except Exception as e:  # noqa: BLE001 - W8: logging must never eat a decision
            log.warning("receipt write failed: %s", e)
        return 200, {"ok": True, "decision": decision}

    def handle(self, method: str, path: str, headers: dict[str, str],
              body: bytes | None) -> tuple[int, dict[str, Any]]:
        proto_ok, proto_err = self._protocol_ok(headers)
        if not proto_ok:
            return 400, {"ok": False, "error": "protocol_mismatch", "detail": proto_err}
        p = path.split("?", 1)[0].rstrip("/") or "/"
        if method == "GET" and p == "/health":
            return self.health()
        if method == "POST" and p == "/decide":
            try:
                parsed = json.loads((body or b"").decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                return 400, {"ok": False, "error": "bad_request", "detail": f"invalid JSON: {e}"}
            return self.decide(parsed)
        if method == "POST" and p == "/warmup":
            return self.warmup_now(force=True)
        return 404, {"ok": False, "error": "not_found", "detail": f"no route {method} {p}"}


class _Handler(BaseHTTPRequestHandler):
    server_version = "jev-router-core/1"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> CoreApp:  # type: ignore[override]
        return self.server.app  # type: ignore[attr-defined]

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ValueError(f"body too large: {length} > {MAX_BODY_BYTES}")
        return self.rfile.read(length) if length > 0 else b""

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header(PROTOCOL_HEADER, str(self.app.cfg.protocol_version))
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, method: str) -> None:
        try:
            body = self._read_body()
        except ValueError as e:
            self._respond(413, {"ok": False, "error": "payload_too_large", "detail": str(e)})
            return
        headers = {k: v for k, v in self.headers.items()}
        try:
            status, payload = self.app.handle(method, self.path, headers, body)
        except Exception as e:  # noqa: BLE001 - the daemon must never 500 on a bug
            log.exception("unhandled error in %s %s", method, self.path)
            # B1-4: the exception text goes to the daemon log only (log.exception
            # above); the API surface keeps a controlled string, never str(e).
            status, payload = 500, {"ok": False, "error": "internal_error",
                                   "detail": "internal error; see daemon log"}
        self._respond(status, payload)

    def do_GET(self) -> None:      # noqa: N802 - http.server API
        self._dispatch("GET")

    def do_POST(self) -> None:     # noqa: N802
        self._dispatch("POST")

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet, secret-free
        log.info("%s - %s", self.address_string(), fmt % args)


def create_server(cfg: JevConfig, app: CoreApp | None = None):
    srv = ThreadingHTTPServer((cfg.core_host, cfg.core_port), _Handler)
    srv.daemon_threads = True
    srv.app = app or CoreApp(cfg)  # type: ignore[attr-defined]
    return srv


def start_warmup_thread(cfg: JevConfig, cache: CapabilityCache) -> threading.Thread:
    t = threading.Thread(target=lambda: warmup(cfg, cache, force=False),
                        name="jev-warmup", daemon=True)
    t.start()
    return t


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = JevConfig.from_env()
    cache = CapabilityCache(f"{cfg.home_path}/capabilities.json", cfg.cap_ttl_s)
    app = CoreApp(cfg, cache)
    if cfg.warmup_on_start:
        start_warmup_thread(cfg, cache)
    srv = create_server(cfg, app)
    log.info("jev-router core listening on http://%s:%d (protocol %d, policy %s)",
            cfg.core_host, cfg.core_port, cfg.protocol_version, cfg.policy_version)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
