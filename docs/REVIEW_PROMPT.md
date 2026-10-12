You are a strict senior code reviewer. Perform a CLEAN CODE review of the repository at the current directory (jev-router-core). Do NOT modify any files. Read everything relevant, then produce a structured report.

Scope: all Python under core/src/jevrouter, core/tests, adapters/hermes, adapters/pi/src, examples/*.yaml, docs.

Review criteria (in priority order):

1. CORRECTNESS
   - Logic bugs, unhandled edge cases, wrong assumptions
   - Rule DSL: operator semantics, field namespace gaps, template formatting errors
   - Classifier client: response normalization holes, error paths, timeout handling
   - Concurrency: shared state, daemon thread safety, log file interleaving

2. CLEAN CODE / DESIGN
   - Single responsibility per module; leaky abstractions
   - Naming clarity (does the name match behavior?)
   - Duplication that should be extracted vs accidental duplication that should stay
   - Dead code, speculative generality, over-engineering
   - Magic values that belong in config vs constants that are fine

3. ERROR HANDLING & FAIL-OPEN CONTRACT
   - The adapter MUST be fail-open: verify no path can raise into Hermes
   - Broad `except Exception` — justified or hiding bugs?
   - Error messages actionable?

4. SECURITY
   - Injection risks in rule template formatting (str.format over user fields)
   - Log content: secrets in state text, redaction gaps
   - SSRF-ish: config-controlled URLs — acceptable for local tool?

5. TEST QUALITY
   - Coverage gaps: which behaviors are untested?
   - Tests that assert implementation instead of contract
   - Missing negative tests

6. DOCS/CONFIG CONSISTENCY
   - Does config.example.yaml match what the loader actually validates?
   - SPI.md vs actual endpoints/fields
   - README claims vs reality

Output format:
## Verdict (one of: SHIP / SHIP WITH FIXES / REWORK)
## Critical issues (must fix before use) — file:line, what, why, suggested fix
## Warnings (should fix) — same format
## Nits (optional polish)
## Praise (what's genuinely good — keep it real, max 5 items)
## Missing-test checklist (concrete test names to add)

Be specific: cite file:line for every finding. No generic advice. If a category has no findings, say "none found" explicitly.
