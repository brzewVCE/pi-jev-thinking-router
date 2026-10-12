# SPI / Protocol reference (v1)

Core speaks HTTP JSON. Every response carries `X-Router-Protocol: 1`.
Adapters must check it at startup and refuse mismatched majors.

## POST /decide

Request:
```json
{
  "state": "text the classifier judges (last N messages joined, token-capped by adapter)",
  "context": {
    "has_images": false,
    "prompt_tokens": 123,
    "session_turn": 5,
    "session_id": "abc",
    "available_models": ["qwen3-4b-local", "qwen3-32b-local"],
    "kind": "child_instruction"
  },
  "harness": "pi | hermes | cli",
  "session": "abc"
}
```

Response 200:
```json
{
  "ok": true,
  "decision": {
    "model": "qwen3-32b-local",
    "thinking": "medium",
    "lane": "coding_moderate",
    "rule": "default-mapping",
    "source": "lane | pin | fallback | confidence_floor | classifier_down_fallback",
    "spawn": { "allowed": false, "forced": false, "vetoed": false, "needs_spawn_prob": 0.12 }
  },
  "answers": { "task_kind": {"value": "coding", "confidence": 0.93, "probabilities": {"...": 0.0}}, "...": {} },
  "classifier_latency_ms": 88
}
```

Errors:
- 503 `classifier_unavailable` — only when `classifier.down: block` (otherwise 200 with fallback decision)
- 502 `classifier_error` — unparseable classifier response

## POST /classify
Same request minus context/harness/session; returns raw normalized answers only.

## GET /health
`{"ok": true, "classifier": "<base_url>"}` — 200 if core alive (does NOT imply classifier alive).

## Adapter contract (SPI)

An adapter implements four behaviors against the core:

| SPI method | Core call | Adapter action |
|---|---|---|
| onPrompt(state, ctx) | POST /decide | get decision |
| applyRouting(decision) | — | harness-specific: setModel/setThinkingLevel/middleware kwargs |
| spawnChild(instruction) | POST /decide (kind=child_instruction) | pass model/thinking into harness spawn API |
| gateResult(result, criteria) | POST /classify (phase 2) | escalate/report |

Adapters are fail-open: core unreachable => keep current model, log warning.
Pins (decision.source == "pin") must never be overridden by adapter heuristics.

## Rule field namespace (what `when.field` can reference)
- Every question `<name>` -> chosen value
- `<name>_confidence`, `<name>_prob` (prob of chosen; for noul: prob of TRUE)
- context fields: `has_images`, `prompt_tokens`, `session_turn`, `harness`, `kind`
- Ops: `== != > >= < <= in`
- Actions: `lane`, `lane_template` (str.format over fields), `pin {model, thinking}`,
  `veto_spawn`, `force_spawn`
