Working...
## Verdict: SHIP

## Pass-2 issue verification

**1. Protocol-mismatch swallowed (MEDIUM) — FIXED.** `adapters/hermes/__init__.py:68-78`: the `try` ends at `:74`; the `if int(proto) != 1: _PROTOCOL_OK = False; raise RuntimeError(...)` block is outside it. `register()` `:200-204` catches `RuntimeError`, logs "NOT registered", returns before `ctx.register_middleware` at `:208-209`. Empirically confirmed by test run output: `jev-router adapter NOT registered: unsupported core protocol 2` with zero registrations asserted (`core/tests/test_adapter_hermes.py:97-104`).

**2. Pi timeout 1500 < core 2000 (MEDIUM) — FIXED.** `adapters/pi/src/client.ts:29` interface comment: `default 3000 (must exceed core classifier 2000ms + overhead)`. `client.ts:38` actual fallback: `this.opts.timeoutMs ?? 3000`. Both locations match.

**3. Hermes docstring drift (LOW) — FIXED.** `adapters/hermes/__init__.py:17`: `JEVRROUTER_TIMEOUT_MS per-call timeout (default 3000)` matches code default at `:34`.

**4. Log 0600 no-op on first create (LOW) — FIXED.** `core/src/jevrouter/log.py:19-23`: `os.open(path, O_CREAT|O_WRONLY|O_APPEND, 0o600)`. Verified empirically: fresh file under umask 022 → `0o600` after create and after write.

**5. Stickiness dead config presented as active (LOW) — FIXED.** `examples/config.example.yaml:131-136`: explicit `NOT YET IMPLEMENTED in core (phase 2...)` / "Loaded and validated but consumed nowhere yet... config-shape example... not active behavior."

**6. Validator-vs-engine custom-field mismatch (LOW) — NOT FIXED.** No change in commit 58a4763 to `config.py`; `_CONTEXT_FIELDS` still closed (`config.py:47-49`), engine still merges arbitrary context. Only implicit documentation: the example's field list (`config.example.yaml:88-91`). The "document the intent" ask was not done. Not claimed in the commit message either.

**7. Missing adapter contract tests (LOW) — FIXED.** 6 tests in `core/tests/test_adapter_hermes.py:86-159`, all exercising real behavior against a live fake HTTP core (`_FakeCore`, `:33-74`) — no mocks of the code under test, no tautologies:
- `test_registers_on_matching_protocol` — asserts both middlewares registered AND state window 2/100 pulled from fake core (distinguishes from defaults 4/1500). Real.
- `test_refuses_mismatched_protocol` — asserts `registered == []` + `_PROTOCOL_OK is False`. Directly pins issue 1. Real.
- `test_fail_open_when_core_down` — dead port 1: still registers with defaults, `on_llm_request` returns None without raising. Real.
- `test_survives_nonserializable_kwargs` — request carries a `Weird()` object; asserts rewrite succeeds through the real HTTP round-trip. Meaningful: if whole-request `json.dumps` regressed, the except would return None and the assert fails. Real.
- `test_thinking_off_downgrades_reasoning_effort` — `high` → `none`. Real.
- `test_thinking_off_does_not_touch_when_already_none` — the "not touched" claim is indistinguishable from "rewritten to same value" at `:155`, but the `req2` case (`:157-159`, no model change + no thinking change → `None`) genuinely pins the no-op contract. Slightly weak name, real assertion.

## New issues from pass-2 fixes

- **LOW — `adapters/hermes/__init__.py:75`**: `int(proto)` on a non-numeric `X-Router-Protocol` header (e.g. `"v1"`) raises `ValueError`, which `register()`'s `except RuntimeError` (`:202`) does not catch → propagates into the Hermes plugin loader. Pre-fix, the catch-all inside `_refresh_state_window` absorbed this. Contradicts the module's "fail-open everywhere" docstring (`:11-13`). Only triggerable by a broken/malicious core; fix: `except (RuntimeError, ValueError)` or wrap the int conversion.
- **INFO — `adapters/hermes/__init__.py:205-207`**: `if not _PROTOCOL_OK` is dead code — the only path setting it False also raises, and that path returns at `:204`. Harmless belt-and-suspenders.
- **LOW — `core/src/jevrouter/log.py:20`**: `os.open` mode applies only at creation; a `decisions.jsonl` created before this fix stays 0644 (no chmod on existing files). First-create case fixed; residual for upgraded installs.
- **ENV — `core/.venv` is broken**: `python3.13` symlink actually runs system 3.11.2 and lacks PyYAML; the specified venv command fails. Packaging hygiene issue, not code.

## Test run

Specified command fails: `core/.venv/bin/python -m unittest discover -s tests` → `ModuleNotFoundError: No module named 'yaml'` (2 errors). Fallback per instructions:

```
$ cd core && PYTHONPATH=src python3 -m unittest discover -s tests
Ran 46 tests in 6.156s
OK
```

46/46 pass (40 prior + 6 new adapter contract tests), matching the commit claim.

## Gate

Shippable as Apache-2.0 open-core phase 1. Both pass-2 blockers are genuinely fixed and now backed by behavioral tests running against a live fake core — the protocol-mismatch path provably registers zero middlewares, and the Pi timeout is consistent across comment and code. The log hardening was empirically confirmed at 0600 on fresh create. Remaining findings are LOW-severity edges (non-numeric protocol header crashing `register()` instead of failing open, pre-existing log files keeping old mode, undocumented validator closed-set on custom fields) — none affect the honest surface of the release: docs now match code, dead config is labeled dead, and the headline C1/C2 adapter contracts have real test coverage. Recommend the `int(proto)` guard and a working venv/rebuild note as fast-follows, not gates.
EXIT:0
