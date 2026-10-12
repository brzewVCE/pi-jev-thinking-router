# SMOKE-PI — live Pi smoke (2026-10-02)

Host: `pi` on PATH = omp/18.2.11 (`/usr/local/bin/pi`), node v22.23.2, python3 3.11.2.
JevK5 backend `:8091` alive (`GET /v1/models` -> 200, `jevk5-0.2`).
A pre-existing compatible daemon answered on `:8790` (Python 3.13.5, external to this build).
Extension under test: `adapters/pi/src/index.ts` loaded via `pi -e ./src/index.ts -p --no-session`.
Smoke state dirs under `/tmp/jev-smoke-state*` (XDG_STATE_HOME); daemon spawned in smoke A was killed after the run.

## A — embedded daemon SPAWNED by the extension (free port 8795)

```console
$ rm -rf /tmp/jev-smoke-state && cd /workspace/jev-router-core/adapters/pi
$ XDG_STATE_HOME=/tmp/jev-smoke-state JEV_CORE_PORT=8795 timeout 120 pi -e ./src/index.ts -p --no-session "reply with exactly: OK"
exit=0
Working...
OK
---pidfile---
170332 8795
---health 8795---
{"backend": "configured", "model": "jevk5-0.2", "kind": "typesafe", "policy_version": "1", "credential_present": false, "timeout_ms": 3000, "cap_ttl_s": 3600, "state": {"last_messages": 4, "max_tokens": 1500}, "ok": true, "warmup": "ok", "protocol_version": 1}
X-Router-Protocol: 1
```

Spawned process is detached (PPID 1) with cwd = package dir:

```console
$ ps -o pid,ppid,cmd -p 170332; readlink /proc/170332/cwd
   PID   PPID COMMAND
170332      1 python3 -m core.jev_core
/workspace/jev-router-core/adapters/pi
```

`/decide` on the spawned daemon returns a real JevK5 decision (`source: jev`):

```console
$ curl -s -H 'X-Router-Protocol: 1' -H 'Content-Type: application/json' -X POST http://127.0.0.1:8795/decide \
    -d '{"host":"pi","session_id":"smoke-curl","turn_id":"1","task":"Explain the tradeoff between consistency and availability in distributed systems, in depth.","current_effort":"medium","supported_levels":["off","minimal","low","medium","high","xhigh","max"],"budget_ms":3000}'
{"ok": true, "decision": {"effort": "medium", "changed": false, "source": "jev", "reason_code": "capped", "policy_version": "1", "elapsed_ms": 211, "confidence": 0.4756828882407751}}
```

The pi session itself called `/decide` through the extension (receipt written by the spawned daemon; sanitized — `recent_turns` stored as sha256, no secrets):

```json
{"ts": 1790947723.7732446, "date": "20261002", "host": "pi", "session_id": "01a0fccd-86ff-716d-908f-934b6d4d63a1", "turn_id": "1", "request": {"host": "pi", "session_id": "01a0fccd-86ff-716d-908f-934b6d4d63a1", "turn_id": "1", "task": "reply with exactly: OK", "recent_turns": "sha256:e3b0c44298fc1c14", "has_images": false, "current_effort": "medium", "supported_levels": ["off", "minimal", "low", "medium", "high", "xhigh", "max"], "budget_ms": 3000}, "decision": {"effort": "medium", "changed": false, "source": "hysteresis", "reason_code": "hysteresis_hold", "policy_version": "1", "elapsed_ms": 363, "confidence": 0.9104620271413277}, "reason_code": "hysteresis_hold", "elapsed_ms": 363, "warmup": "ok", "policy_version": "1"}
```

```console
$ tail -5 /tmp/jev-smoke-state/jev-adaptive-router/daemon.log
2026-10-02 13:28:43,359 INFO jev-router.core 127.0.0.1 - "GET /health HTTP/1.1" 200 -
2026-10-02 13:28:43,360 INFO jev-router.core 127.0.0.1 - "GET /health HTTP/1.1" 200 -
2026-10-02 13:28:43,773 INFO jev-router.core 127.0.0.1 - "POST /decide HTTP/1.1" 200 -
2026-10-02 13:28:44,679 INFO jev-router.core 127.0.0.1 - "GET /health HTTP/1.1" 200 -
2026-10-02 13:28:56,074 INFO jev-router.core 127.0.0.1 - "POST /decide HTTP/1.1" 200 -
```

## B — ADOPT existing daemon (no spawn)

Default window (8790 already answered with `X-Router-Protocol: 1`):

```console
$ XDG_STATE_HOME=/tmp/jev-smoke-state-b timeout 120 pi -e ./src/index.ts -p --no-session "reply with exactly: OK"
exit=0
Working...
OK
$ ls -A /tmp/jev-smoke-state-b/jev-adaptive-router/     # empty => adopted, nothing spawned
```

Deterministic variant — window pointed at smoke A's live daemon (8795): fresh state dir stays
pidfile-free (adoption) and the ADOPTED daemon's receipt shows the session's `/decide`:

```console
$ XDG_STATE_HOME=/tmp/jev-smoke-state-b2 JEV_CORE_PORT=8795 pi -e ./src/index.ts -p --no-session "reply with exactly: OK"
exit=0; Working... OK
$ ls -A /tmp/jev-smoke-state-b2/jev-adaptive-router/    # empty => no spawn
$ tail -1 /tmp/jev-smoke-state/jev-adaptive-router/receipts-20261002.jsonl
host: pi | task: reply with exactly: OK | decision: {'effort': 'medium', 'changed': False, 'source': 'hysteresis', 'reason_code': 'hysteresis_hold', 'policy_version': '1', 'elapsed_ms': 225, 'confidence': 0.9104620271413277}
```

## C — DEGRADED: python missing => fail-open, no retry storm

```console
$ rm -rf /tmp/jev-smoke-state-c
$ XDG_STATE_HOME=/tmp/jev-smoke-state-c JEV_CORE_PORT=8796 JEVRROUTER_PYTHON=/nonexistent/python3 timeout 120 pi -e ./src/index.ts -p --no-session "reply with exactly: OK"
exit=0
Working...
OK
$ ls -A /tmp/jev-smoke-state-c/jev-adaptive-router/
daemon.log            # created empty; spawn failed with ENOENT before any pidfile write
$ curl -s -m 1 -o /dev/null -w "%{http_code}" http://127.0.0.1:8796/health
000                   # nothing listening; session still completed (fail-open)
```

The ENOENT path returns after ONE spawn attempt (unit-tested: `spawn ENOENT => failed with a
python hint and exactly one attempt`); `/route status` surfaces
`daemon: UNAVAILABLE — python not found (/nonexistent/python3); set JEVRROUTER_PYTHON...`
(interactive command; print mode does not render notifications).

## Notes

- `JEVRROUTER_URL` set => external mode: no probe, no spawn (this is how the 285-line
  `route.test.ts` suite stays hermetic).
- Smoke A daemon (pid 170332) was killed after the run; `8795 now: 000`.
