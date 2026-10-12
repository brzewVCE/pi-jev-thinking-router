# pi-jev-thinking-router

Self-contained adaptive thinking-effort routing for the [Pi coding agent](https://github.com/badlogic/pi-mono) (and omp).

Powered by TypeSafe Jev (SPI v1 decision protocol).

Unlike typical model routers that switch between models (and discard KV prompt cache across turns), **pi-jev-thinking-router keeps the model stable and dynamically adjusts only the reasoning/thinking effort** (`minimal` to `high` / `max`). Disabling thinking (`off`) is intentionally excluded to preserve prompt cache continuity and prevent cache misses on frontier reasoning models.

## Architecture

- **Effort-only routing**: Before each turn, the extension queries the local core daemon (`POST /decide`) to choose the optimal thinking level for the task. The model is never switched.
- **LSP pattern**: Automatically spawns and manages an embedded, stdlib-only Python daemon on demand (`127.0.0.1:8790`). No manual background processes or external services needed.
- **Fail-open by design**: Any network, daemon, or timeout failure keeps the current thinking level and continues the turn without disruption.
- **Minimalist Status Bar**: Clean indicator in the Pi status bar (`⚡ <effort> (<ms>ms)` / `⚡ fallback` / `⚡ n/a`) keeping the editor clean.

## Repository Layout

```text
adapters/
  pi/             Pi/omp extension package (TypeScript + embedded core)
  hermes/         Hermes plugin (SPI v1 middleware)
docs/
  SPI.md          SPI v1 protocol specification
  ATTRIBUTION.md  Third-party project references
```

## Quick Start (Pi)

Install permanently:
```bash
pi install npm:pi-jev-thinking-router
```

Or load from a local checkout:
```bash
pi install ./adapters/pi
```

## Slash Commands

- `/route on` / `/route off` — enable / disable routing decisions for this session
- `/route status` — daemon health, protocol version, and turn statistics
- `/route last` — detailed breakdown of the last routing decision
- `/route show on|off` — toggle status bar indicator visibility

## Tests

```bash
cd adapters/pi
npm test        # 34 tests, no external network or Python required
```

## License

Apache-2.0
