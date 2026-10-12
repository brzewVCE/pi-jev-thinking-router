---
name: jev-router
description: Control and diagnose the pi-jev-thinking-router extension (thinking-effort routing). Use when asking about /route commands, why the thinking level changes on its own, JEVRROUTER_* or JEV_* environment variables, degraded/fail-open routing, or the local jev-router core daemon.
---

# jev-router (Pi/omp adapter)

The extension asks a local SPI v1 core daemon before each turn which thinking
level to use, then applies `pi.setThinkingLevel()`. It never changes the model.

## Commands

- `/route on` / `/route off` — enable/disable routing decisions for this
  session. `off` makes ZERO `/decide` calls; thinking stays exactly as set.
- `/route status` — routing/visibility flags, core URL + health, daemon mode
  (`external` / `adopted` / `spawned` / `UNAVAILABLE`), turns decided, last decision.
- `/route last` — the last routing decision this session (effort, source,
  reason_code, confidence, latency).
- `/route show on|off` — widget / status bar / notification visibility only;
  routing keeps running underneath.
- `/router` — legacy health + last decision.

## Environment

Extension behavior (read at startup):

| Var | Meaning | Default |
|---|---|---|
| `JEVRROUTER_URL` | external daemon URL; when set the extension does NOT probe/spawn | unset |
| `JEVRROUTER_TIMEOUT_MS` | per-call client timeout | 3000 |
| `JEVRROUTER_ENABLED` | `0` disables the extension entirely | 1 |
| `JEVRROUTER_NOTIFY` | `0` silences per-turn notifications | 1 |
| `JEVRROUTER_CAP` | ceiling level the core must not exceed | unset |
| `JEVRROUTER_DEFAULT_EFFORT` | effort when the harness reports none | medium |
| `JEVRROUTER_UI_DEFAULT` | startup routing state (`on|off`) | on |
| `JEVRROUTER_PYTHON` | interpreter used to spawn the embedded daemon | python3 |

Embedded daemon knobs — set as `JEVRROUTER_<SUFFIX>` or `JEV_<SUFFIX>`
(the `JEVRROUTER_` form wins); passed to the spawned daemon:

| Var | Meaning | Default |
|---|---|---|
| `JEV_KIND` | decision backend kind | typesafe |
| `JEV_API_URL` | backend base URL | http://localhost:8091/v1 |
| `JEV_MODEL` | decision model | jevk5-0.2 |
| `JEV_CORE_PORT` | base port of the probe window 8790-8792 | 8790 |
| `JEV_TIMEOUT_MS` | backend timeout inside the daemon | 3000 |
| `JEV_HOME` | daemon state dir (receipts, capabilities) | ~/.local/state/jev-adaptive-router |
| `JEV_API_KEY` | bearer token for the backend; never logged, never in /health | unset |

## Embedded daemon (LSP pattern)

On activation the extension probes `http://127.0.0.1:8790/health` (header
`X-Router-Protocol: 1`, 300 ms timeout). Compatible answer => adopt. A foreign
listener (no header / 404) => try 8791, then 8792. Nothing listening => spawn
`python3 -m core.jev_core` detached from the package dir; log
`~/.local/state/jev-adaptive-router/daemon.log`, pidfile `jev-core.pid`
there. After spawn it re-probes every 250 ms for up to 5 s.

## Degraded / fail-open

If no daemon answers (python missing, ports busy, daemon dead), the adapter
degrades: status bar shows `⚡ fallback`, every turn keeps the current
thinking level untouched. Routing NEVER escalates effort on an error path and
never breaks a turn. `reason_code` values: `jev_selected`, `jev_error`,
`jev_timeout`, `jev_unparsable`, `cached`, `hysteresis_hold`, `capped`,
`no_backend`.

## Checking the daemon by hand

```bash
curl -s -H 'X-Router-Protocol: 1' http://127.0.0.1:8790/health
# expect: {"ok":true,...,"warmup":"ok","protocol_version":1} + header X-Router-Protocol: 1
cat ~/.local/state/jev-adaptive-router/jev-core.pid   # "<pid> <port>"
tail -n 20 ~/.local/state/jev-adaptive-router/daemon.log
```

If the port is held by a foreign service the daemon may live on 8791/8792 —
`/route status` shows the actual URL and mode.
