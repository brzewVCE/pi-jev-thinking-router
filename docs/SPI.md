# SPI / Protocol reference (v1)

Core speaks HTTP JSON (stdlib `http.server` daemon, `/workspace/jev-routing/core`).
Every response carries `X-Router-Protocol: 1`. Adapters check it at startup and
refuse mismatched majors (no registration). This document mirrors
`/workspace/jev-routing/CONTRACT.md`, which is authoritative.

Principle: **Jev decides**. The core applies no word lists or content
heuristics. Its only logic: transport to the decision backend, level
normalization, clamp to host-supported levels, per-turn cache, downgrade
hysteresis, receipts. Backend error = "keep" (no change).

## POST /decide

Request:
```json
{
  "host": "hermes | pi | codex | claude-code",
  "session_id": "…",
  "turn_id": "…",
  "task": "task text (tail of recent user turns)",
  "recent_turns": "optional condensed context",
  "has_images": false,
  "current_effort": "medium",
  "supported_levels": ["low", "medium", "high"],
  "cap": "high",
  "budget_ms": 4000
}
```

Response 200:
```json
{
  "ok": true,
  "decision": {
    "effort": "low",
    "changed": true,
    "source": "jev",
    "reason_code": "jev_selected",
    "confidence": 0.8,
    "policy_version": "1",
    "elapsed_ms": 210
  }
}
```

Keep-decision (error / timeout / no answer):
```json
{ "ok": true, "decision": { "effort": "<current_effort>", "changed": false,
  "source": "fallback", "reason_code": "jev_error" } }
```

`effort` is ALWAYS in the host's `supported_levels` (after clamp).
`source` ∈ `jev | cache | hysteresis | fallback`.

reason_code (closed set):

| code | meaning |
|---|---|
| jev_selected | Jev picked a level, accepted after clamp |
| jev_error | backend HTTP/protocol error → keep |
| jev_timeout | budget_ms exceeded → keep |
| jev_unparsable | answer without a recognizable level → keep |
| cached | same turn, decision served from cache |
| hysteresis_hold | >1-level downgrade held (awaiting 2 agreeing decisions) |
| capped | Jev above user cap → cap applied |
| no_backend | no configured/unavailable backend → keep |

## GET /health

```json
{ "ok": true, "backend": "configured", "model": "…", "policy_version": "1",
  "credential_present": true, "warmup": "ok", "state": { "last_messages": 4, "max_tokens": 1500 } }
```
Header `X-Router-Protocol: 1`. The API key itself is never exposed —
only `credential_present`. `warmup` ∈ `pending | ok | failed:<reason>`.

## POST /warmup

Forces a re-warmup of the decision backend (synthetic prompt only, zero user
data). 200 on success, 503 on failure; result cached per `(backend_url,
model)` with TTL (default 1 h) in RAM + `~/.jev-router/capabilities.json`.
A failed warmup marks the backend unavailable: `/decide` answers
`no_backend` (keep) until manual re-warmup or TTL expiry.

## Level normalization

Internal canon: `off, minimal, low, medium, high, xhigh, max` (7 rungs).
Hosts send their own spellings; the core maps to canon, decides, maps back and
clamps to `supported_levels` (nearest weaker). Never returns a level outside
the host's list. Hermes: `none,minimal,low,medium,high`. Pi: full canon.
Codex: `minimal,low,medium`.

## Adapter contract (SPI v1)

| SPI behavior | Core call | Adapter action |
|---|---|---|
| onTurn(state) | POST /decide | extract host state, send request above |
| applyDecision(d) | — | apply `effort` only when `changed: true`; validate against host palette; never touch model/messages/system prompt |
| handshake | GET /health | check `X-Router-Protocol`; major mismatch => do not register |

Adapters are fail-open: core unreachable / timeout / bad decision => keep the
host's current effort, request untouched. Adapters MUST NOT escalate effort
on any error path. Receipts (JSONL, sanitized, daily rotation) are written by
the core, not the adapters.

## What v1 deliberately does NOT contain

- trivial-word lists (excluded by decision: Jev decides),
- model routing (Phase 5; a `model` field in responses does not exist),
- delegate_task veto (lives in the Hermes adapter as advisory, outside v1 core).
