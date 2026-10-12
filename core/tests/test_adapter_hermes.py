"""Adapter contract tests (pass2 issue 7): fail-open, protocol mismatch,
non-serializable kwargs, thinking-off downgrade — without the Hermes harness.

The adapter module is loaded directly with env pointing at a fake core server.
"""
import importlib
import importlib.util
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ADAPTER_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "adapters", "hermes", "__init__.py")


def load_adapter(env: dict):
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location(f"jev_adapter_{threading.get_ident()}_{len(os.environ)}", ADAPTER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return mod


class _FakeCore:
    """Context manager running a fake core daemon; routes configurable per test."""

    def __init__(self, health_proto="1", health_body=None, decide_body=None, decide_status=200):
        self.health_proto = health_proto
        self.health_body = health_body or {"ok": True, "classifier": "http://x",
                                          "state": {"last_messages": 2, "max_tokens": 100}}
        self.decide_body = decide_body
        self.decide_status = decide_status

    def __enter__(self):
        hb, dp, ds = self.health_body, self.health_proto, (self.decide_body, self.decide_status)

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a, **k):  # silence
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("X-Router-Protocol", dp)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(hb).encode())

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                self.rfile.read(n)
                body, status = ds
                self.send_response(status)
                self.send_header("X-Router-Protocol", dp)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()


class _Ctx:
    def __init__(self):
        self.registered = []

    def register_middleware(self, kind, cb):
        self.registered.append(kind)


class TestAdapterContracts(unittest.TestCase):
    def test_registers_on_matching_protocol(self):
        with _FakeCore(health_proto="1") as core:
            mod = load_adapter({"JEVRROUTER_URL": f"http://127.0.0.1:{core.port}"})
            ctx = _Ctx()
            mod.register(ctx)
            self.assertIn("llm_request", ctx.registered)
            self.assertIn("tool_request", ctx.registered)
            # state window pulled from core /health
            self.assertEqual(mod._state_window["last_messages"], 2)
            self.assertEqual(mod._state_window["max_tokens"], 100)

    def test_refuses_mismatched_protocol(self):
        # pass2 issue 1: mismatch must NOT be swallowed; adapter must not register
        with _FakeCore(health_proto="2") as core:
            mod = load_adapter({"JEVRROUTER_URL": f"http://127.0.0.1:{core.port}"})
            ctx = _Ctx()
            mod.register(ctx)
            self.assertEqual(ctx.registered, [])
            self.assertFalse(mod._PROTOCOL_OK)

    def test_refuses_nonnumeric_protocol_header(self):
        # pass3 LOW: "v1" header must not crash register()
        with _FakeCore(health_proto="v1") as core:
            mod = load_adapter({"JEVRROUTER_URL": f"http://127.0.0.1:{core.port}"})
            ctx = _Ctx()
            mod.register(ctx)
            self.assertEqual(ctx.registered, [])

    def test_fail_open_when_core_down(self):
        # /health unreachable -> defaults kept, adapter still registers (core may
        # come up later); decide calls return None silently.
        mod = load_adapter({"JEVRROUTER_URL": "http://127.0.0.1:1"})  # dead port
        ctx = _Ctx()
        mod.register(ctx)
        self.assertIn("llm_request", ctx.registered)
        self.assertEqual(mod._state_window["last_messages"], 4)  # default
        req = {"model": "m", "reasoning_effort": "high",
               "messages": [{"role": "user", "content": "x"}]}
        out = mod.on_llm_request(req, dict(req))
        self.assertIsNone(out)  # fail-open: no rewrite, no raise

    def test_survives_nonserializable_kwargs(self):
        # C1 regression via real decide response
        decision = {"ok": True, "decision": {"model": "small-model", "thinking": "off"}}
        with _FakeCore(decide_body=decision) as core:
            mod = load_adapter({"JEVRROUTER_URL": f"http://127.0.0.1:{core.port}"})
            mod._refresh_state_window()
            class Weird:
                pass
            req = {"model": "big", "reasoning_effort": "high", "weird": Weird(),
                   "messages": [{"role": "user", "content": "hello"}]}
            out = mod.on_llm_request(req, dict(req))
            self.assertIsNotNone(out)
            self.assertEqual(out["request"]["model"], "small-model")

    def test_thinking_off_downgrades_reasoning_effort(self):
        # C2 regression on the Hermes side
        decision = {"ok": True, "decision": {"model": "m2", "thinking": "off"}}
        with _FakeCore(decide_body=decision) as core:
            mod = load_adapter({"JEVRROUTER_URL": f"http://127.0.0.1:{core.port}"})
            mod._refresh_state_window()
            req = {"model": "m1", "reasoning_effort": "high",
                   "messages": [{"role": "user", "content": "hello"}]}
            out = mod.on_llm_request(req, dict(req))
            self.assertEqual(out["request"].get("reasoning_effort"), "none")

    def test_thinking_off_does_not_touch_when_already_none(self):
        decision = {"ok": True, "decision": {"model": "m2", "thinking": "off"}}
        with _FakeCore(decide_body=decision) as core:
            mod = load_adapter({"JEVRROUTER_URL": f"http://127.0.0.1:{core.port}"})
            mod._refresh_state_window()
            req = {"model": "m1", "reasoning_effort": "none",
                   "messages": [{"role": "user", "content": "hello"}]}
            out = mod.on_llm_request(req, dict(req))
            # model m1->m2 still rewrites, but reasoning_effort must NOT be
            # touched again (already "none" -> stays "none", no churn)
            self.assertEqual(out["request"]["model"], "m2")
            self.assertEqual(out["request"]["reasoning_effort"], "none")
            # and with no model change either -> None (no rewrite at all)
            req2 = {"model": "m2", "reasoning_effort": "none",
                    "messages": [{"role": "user", "content": "hello"}]}
            self.assertIsNone(mod.on_llm_request(req2, dict(req2)))


if __name__ == "__main__":
    unittest.main()
