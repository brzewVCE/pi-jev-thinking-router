# jev-router-core

Adapter + documentation home for the **jev-router SPI v1** decision protocol.

The canonical core daemon lives in `/workspace/jev-routing/core/` (SPI v1,
stdlib-only, per `/workspace/jev-routing/CONTRACT.md`). This repo hosts the
host-side adapters that speak that protocol:

```
adapters/
  hermes/         Hermes plugin: llm_request + tool_request middleware (SPI v1)
  pi/             Pi/omp extension (TypeScript): setThinkingLevel (SPI v1)
docs/
  SPI.md          SPI v1 protocol reference (mirrors CONTRACT.md)
  ATTRIBUTION.md  third-party projects referenced (no code copied)
```

## Protocol (SPI v1)

- HTTP JSON. Every response carries `X-Router-Protocol: 1`. Adapters
  version-check at startup; mismatched majors refuse registration (fail-closed
  at handshake, fail-open at decision time).
- v1 decision shape is **effort-only**:
  `{effort, changed, source, reason_code, policy_version, elapsed_ms[, confidence]}`.
  The core never routes models in v1 — the executing model stays whatever the
  host/user picked; only the reasoning effort changes per turn.
- Fail-open is inviolable: backend down / timeout / unparsable answer =>
  `changed: false`, the host's request stays untouched, no escalation.
- Secrets (JEV_API_KEY) are never logged, receipted, or returned by `/health`
  (only `credential_present: bool`).

See `docs/SPI.md` for the full endpoint reference and
`/workspace/jev-routing/CONTRACT.md` for the authoritative contract.

## History note (clean cutover, 2026-09-28)

This repo previously contained a protocol-2 core (TOML rule DSL, classifier
answers, `thinking`+`model` decisions). Per AUDIT-B-1 (B1-3) and the owner's
decision, that core was **removed** — SPI v1 (`CONTRACT.md`) is the single
source of truth. The old code remains in git history (pre-2026-09-28 commits).

## Testing

The v1 core + adapter test suites live with the core:

```bash
cd /workspace/jev-routing/core
python3 -m unittest discover -p 'test_*.py' -v   # 130 tests, stdlib only
```

## License

Apache-2.0. Bundled references to third-party projects are under their own
licenses (see docs/ATTRIBUTION.md).
