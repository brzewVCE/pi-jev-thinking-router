# jev-router-core

Harness-agnostic decision engine for model routing: classify a prompt with a
System One–style classifier (Kev / Laya / Jev cloud — any local or remote
endpoint), map the classification to **(model, thinking)** via user-defined
rules, and let adapters apply the decision in Pi, Hermes, or any agent.

## Design principles

1. **Decision-only core.** The core returns decisions; adapters apply them
   (`setModel`/`setThinkingLevel` in Pi, middleware kwargs in Hermes).
   The core never proxies model traffic.
2. **User freedom.** Categories, rules, thresholds, lanes, fallbacks are all
   user-defined YAML. Nothing is hardcoded — not even PII gating. The repo
   ships *examples* (like `.env.example`), not policies.
3. **Zero-shot only.** Classification works from option descriptions in the
   request. No fine-tuning required or assumed.
4. **Local-first.** The classifier is expected to be self-hosted (e.g. Kev).
   If it is down: `classifier_down: default_model | block` decides behavior.
5. **Full decision log.** Every decision logged as JSONL with full prompt
   content, reason, and confidence — auditable, greppable.

## Layout

```
core/               Python decision engine (FastAPI daemon + CLI)
  src/jevrouter/
    app.py          FastAPI endpoints (/classify /decide /health)
    engine.py       rule interpreter: classification -> (model, thinking)
    classifier.py   System One wire client (Kev/Laya/Jev-cloud)
    config.py       YAML config loader + validation
    log.py          JSONL decision log
adapters/
  hermes/           Hermes plugin: llm_request + tool_request middleware
  pi/               Pi extension (TypeScript): setModel/setThinkingLevel
examples/
  config.example.yaml   full annotated example (rules incl. PII as example)
  categories/           example category packs (EN, PL)
docs/               architecture, SPI reference, rule DSL
```

## Quick start (dev)

```bash
cd core
uv venv && uv pip install -e .
jevrouter --config ../examples/config.example.yaml classify "fix the login bug"
jevrouter serve --config ../examples/config.example.yaml   # FastAPI on :8790
```

## Protocol

- HTTP JSON. Every response carries `X-Router-Protocol: 1` (core and
  adapters version-check at startup).
- Core versions independently from adapters.

## Status

Phase 1 (MVP): core engine + Hermes adapter. See `docs/ROADMAP.md`.

## License

Apache-2.0. Bundled examples reference third-party projects under their own
licenses (see docs/ATTRIBUTION.md).
