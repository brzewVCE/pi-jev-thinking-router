# jev-router core (SPI v1)

Stdlib-only decision daemon per `/workspace/jev-routing/CONTRACT.md`.
The single place where effort decisions are made; hosts (Hermes, Pi, later
Codex / Claude Code) are thin adapters that extract state, POST it, and apply
the returned effort.

## Run

```bash
cd /workspace/jev-routing
JEV_API_URL=http://127.0.0.1:8080/v1 JEV_MODEL=jev-latest python3 -m core.jev_core
# daemon on 127.0.0.1:8790 (JEV_CORE_PORT), warmup on start (JEV_WARMUP_ON_START=0 to skip)
```

## Endpoints (all responses carry `X-Router-Protocol: 1`)

- `POST /decide` — request/response per CONTRACT.md; decision shape
  `{effort, changed, source, reason_code, policy_version, elapsed_ms[, confidence]}`;
  reason codes: `jev_selected | jev_error | jev_timeout | jev_unparsable |
  cached | hysteresis_hold | capped | no_backend`.
- `GET /health` — secret-free status (`credential_present` bool only),
  warmup state, policy version, state window.
- `POST /warmup` — force re-warmup (synthetic prompt, zero user data).

A request declaring a different protocol major is rejected with 4xx.

## Modules

| module | responsibility |
|---|---|
| `config.py` | env-driven config (`JEV_*`), validation |
| `policy.py` | canonical ladder, aliases, selector prompt — **sole owner of `POLICY_VERSION`** (bump here when the prompt or ladder changes) |
| `transport.py` | OpenAI-compat chat/completions over urllib; error types map 1:1 to reason codes; level whitelist at parse |
| `capabilities.py` | warmup + per-`(backend_url, model)` cache, TTL 1 h, RAM + `~/.jev-router/capabilities.json` (0600) |
| `decide.py` | normalize → cap → clamp-nearest-weaker → hysteresis → per-turn cache |
| `receipts.py` | sanitized daily-rotated JSONL audit trail |
| `jev_core.py` | thin `http.server` routing layer (no domain logic) |

Invariants: fail-open on every backend error (`changed:false`, host request
untouched, never an escalation); no secrets in logs/receipts/health;
zero third-party dependencies.

## Test

```bash
cd /workspace/jev-routing/core
python3 -m unittest discover -p 'test_*.py' -v   # 130 tests, stdlib only
```
