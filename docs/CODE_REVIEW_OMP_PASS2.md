Working...
All evidence gathered. Report:

## Verdict: SHIP WITH FIXES

C1–C4 genuinely fixed in code; 12/15 warnings fully fixed, 2 partial, 1 fixed-as-advisory. Two medium new/residual issues (swallowed protocol check; Pi timeout class-of-W7) block a clean SHIP.

## Fix verification table

| Finding | Status | Evidence |
|---|---|---|
| C1 adapter raises into Hermes | **FIXED** | `adapters/hermes/__init__.py:118-153` (`on_llm_request` try/except→None), `:159-188` (`on_tool_request` same); token estimate now `len(state)//4` at `:125`, no whole-request `json.dumps` |
| C2 `thinking:"off"` no-op | **FIXED** | Hermes `__init__.py:137-142`: `"off"` → sets `reasoning_effort="none"` when current not in `(None,"none","off")`; Pi `client.ts:89-94`: `setThinkingLevel("off")` when current thinking set and ≠ off. Caveat: `"none"` clearing value unverified against real Hermes harness (review's own "verify against harness" step not evidenced) |
| C3 dead PII example rule | **FIXED** | `examples/config.example.yaml:56-60` adds `pii` noul question; rule `pii_prob >= 0.5` (`:96-98`) now references a produced field; `jevrouter validate` passes (4 questions/8 lanes/4 rules); `test_engine.py:53-58` uses real config question, not synthetic-only |
| C4 blocking I/O in async handlers | **FIXED** | `app.py:46-49, 72-74`: `classify` via `starlette.concurrency.run_in_threadpool`; `/health` plain `def` (`:30-31`) |
| W1 falsy value dropped | **FIXED** | `classifier.py:80-86` explicit `is None` chaining; test `test_falsy_bool_value_survives` |
| W2 non-dict response crash | **FIXED** | `classifier.py:70-74` isinstance guards raise ValueError; wrapped as `ClassifierError` at `:120-122` |
| W3 lane_template only KeyError | **FIXED** | `engine.py:105-109` `except Exception → lane_name=None → continue`; plus load-time parse `config.py:124-133` |
| W4 empty available_models = no filter | **FIXED** | `engine.py:113-120`: `None` → no filter; `[]` → `continue` (nothing usable) |
| W5 hardcoded spawn question name | **FIXED** | `engine.py:150` `spawn_cfg.get("question", "needs_spawn")`; test with `potrzebuje_subagenta` |
| W6 `!=` matches missing | **FIXED** | `engine.py:18-20`: `return a is not None and a != b` |
| W7 adapter timeout < core | **PARTIAL** | Hermes raised to 3000 ms (`__init__.py:34`) > core 2000. **Pi client default still 1500 ms** (`client.ts:38`) < core `timeout_ms: 2000` — same failure class unfixed |
| W8 log failure 500s decision | **FIXED** | `app.py:108-114` `_safe_log` try/except; used at `:80, 96, 101` |
| W9 malformed body 500 | **FIXED** | `app.py:43-47, 62-66`: non-dict/bad JSON → 400 with protocol header |
| W10 no rule/action/field validation | **FIXED** | `config.py:95-135`: unknown field, unknown action, `pin.model` required, lane existence, lane_template parse+field check; 8 tests |
| W11 noul semantics dropped | **FIXED** | `classifier.py:58-64` `true_means`/`false_means` passthrough |
| W12 dead state config | **FIXED (Hermes)** | `/health` returns `cfg.state` (`app.py:35-37`); adapter pulls at startup (`__init__.py:63-76`) and honors window (`:79-104`). Pi still doesn't build state (library takes caller-supplied state — pre-existing) |
| W13 Hermes protocol check | **PARTIAL** | Check exists (`__init__.py:68-71`) but the `RuntimeError` is caught by its own `except Exception` (`:75-76`). **Empirically verified**: against a protocol-2 core, adapter logs the mismatch and still registers both middlewares. SPI "refuse mismatched majors" not enforced |
| W14 aspirational veto | **FIXED (as advisory)** | `__init__.py:178-182` comment corrected to "best-effort hint… delegate_task does not honor this key today"; real veto deferred phase 3 |
| W15 decode mislabeled unavailable | **FIXED** | `classifier.py:108-118`: network/timeout/HTTP → `ClassifierUnavailable`; decode/normalize/empty → `ClassifierError` |

## New issues

1. **MEDIUM — protocol-mismatch check swallowed** (`hermes/__init__.py:70-76`): `raise RuntimeError` inside the try whose catch-all logs "using default state window" and proceeds. Adapter runs against incompatible core. Fix: set a module-level `_DISABLED` flag (or re-raise past the except) on mismatch; check it in `register()`.
2. **MEDIUM — Pi client default timeout 1500 ms < core classifier 2000 ms** (`client.ts:38` vs `config.example.yaml:15`): W7's exact failure mode left in the second adapter.
3. **LOW — Hermes docstring drift**: `__init__.py:17` says `JEVRROUTER_TIMEOUT_MS … default 1500`; code default is 3000 (`:34`).
4. **LOW — log 0600 hardening ineffective on first creation** (`log.py:17-22`): `chmod` runs before the file exists (FileNotFoundError→pass); `write()` creates via `open("a")` under umask. Verified: fresh file mode `0o644` despite the fix.
5. **LOW — stickiness is dead config** (W12-class, not covered by the fix): loaded at `config.py:141`, consumed nowhere in `engine.py`/adapters; `config.example.yaml:126-128` enables it with behavioral comment. SPI.md doesn't document stickiness as core behavior — example implies behavior that doesn't exist.
6. **LOW — validator rejects custom context fields the engine supports**: `build_fields` merges arbitrary adapter context (`engine.py:43`) but `_CONTEXT_FIELDS` is closed (`config.py:45-47`), so a rule on a custom context field fails load. Matches documented SPI, contradicts engine capability — document the intent.
7. **LOW — promised adapter/app regression tests missing**: checklist items `CODE_REVIEW_OMP.md:93-99` for `test_app_decide_*`, `test_health_carries_protocol_header`, `test_hermes_adapter_fail_open_on_core_down`, `test_hermes_adapter_survives_nonserializable_request`, `test_thinking_off_downgrades_existing_reasoning_effort` were NOT added. The 28 new tests cover classifier/config/engine/log only; C1/C2 adapter contracts (the headline fixes) have zero test coverage.

## Test run result

The specified command fails in this container: `core/.venv/bin/python` is a dangling symlink to `/usr/bin/python3` (absent here; venv built elsewhere). Ran with system CPython 3.11.2 + PyYAML (tests need no FastAPI/httpx):

```
$ cd core && python3 -m unittest discover -s tests -v
...
Ran 40 tests in 4.027s
OK
```

40/40 pass (12 engine + 28 regression), matching the commit claim.

## Remaining known-and-accepted gaps (docs vs code cross-check)

- **Spawn veto is advisory only** — code comment (`hermes/__init__.py:178-182`) admits `__jev_router_veto` is ignored by `delegate_task`; SPI.md never claims veto enforcement. Docs/code agree.
- **Pi "extension" is a library** — no Pi entrypoint (`package.json` main = `client.ts`); pass-1 nit, unchanged. SPI's adapter table (`onPrompt/applyRouting/...`) is aspirational for Pi: `CoreClient.decide` + `applyDecision` cover the first two rows only; `spawnChild`/`gateResult` unimplemented (ROADMAP phase 2/3).
- **Stickiness**: enabled in example, implemented nowhere, absent from SPI — should be removed from the example or documented as phase-2 (issue 5 above).
- **`/health` payload drift**: SPI.md documents `{"ok", "classifier"}`; code returns `state` + `protocol_version` too (`app.py:35-37`). Superset, adapters tolerant — update SPI.
- **C2 `"none"` semantics**: clearing value for Hermes `reasoning_effort` is unverified against the real harness (no spike evidence in repo).
- **Rule-order dependence of spawn accumulation** (veto after a routing rule never applies) — engine comment (`engine.py:84-86`) documents it; example config orders veto first. Consistent, but the promised `test_decide_spawn_flag_accumulation_order` is missing.

**Required before ship**: issues 1 and 2 (both one-liners). Rest are polish.
EXIT:0
