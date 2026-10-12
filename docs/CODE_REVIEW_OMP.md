Working...
## Verdict

**SHIP WITH FIXES** — core design is sound and the fail-open story mostly holds, but there are four issues that break stated contracts or shipped-config behavior.

## Critical issues (must fix before use)

**C1. Hermes adapter can raise into Hermes — violates fail-open contract.**
`adapters/hermes/__init__.py:93` — `len(json.dumps(request)) // 4` runs inside `on_llm_request` with no guard. Any non-JSON-serializable value in the provider kwargs (bytes, custom objects, numpy scalars in tool schemas) raises `TypeError` straight into Hermes middleware. The module docstring (lines 11–13) promises "any error … leaves the request untouched"; `_decide` is protected (line 53) but the handler bodies are not. Also: serializing the entire request per turn just to estimate tokens is wasteful.
Fix: wrap the bodies of `on_llm_request`/`on_tool_request` in `try/except Exception → logger.warning; return None`, and compute the token estimate from the extracted `state` string (`len(state)//4`) instead of the whole request.

**C2. `thinking: "off"` can never downgrade reasoning — both adapters.**
`adapters/hermes/__init__.py:104-109` and `adapters/pi/src/client.ts:88` — the guard `if thinking and thinking != "off"` means an "off" decision is a no-op. If the harness request already carries `reasoning_effort: "high"` (or Pi's thinking level is set high), a turn routed to `coding_trivial {thinking: "off"}` (examples/config.example.yaml:60-61) keeps high reasoning. The lane config's intent is defeated; the comment at line 106–107 acknowledges "'off' means don't request reasoning" but the code cannot *remove* an existing value.
Fix: when `decision.thinking == "off"`, explicitly set `updated["reasoning_effort"] = "none"` (or `None`, per Hermes semantics) and mark `changed = True`; in Pi, call `setThinkingLevel("off")`. Verify against the harness what clears the field.

**C3. Example config's PII rule is dead — references a field no question produces.**
`examples/config.example.yaml:94` gates on `pii_prob >= 0.5`, but `questions:` (lines 30–52) defines no `pii` question, so `pii_prob` is never in the field namespace (`engine.py:44-53` only derives `<name>_prob` from answers). `fields.get("pii_prob")` → `None` → `None >= 0.5` → False (`engine.py:19-24`). The flagship example silently never fires. The test hides this by injecting a synthetic `pii` answer (`core/tests/test_engine.py:57`) that the shipped config can never generate.
Fix: add a `pii` noul question to the example config (with `true_means`/`false_means`), and see W10 for load-time validation that would have caught this class of bug.

**C4. Blocking classifier I/O inside `async def` handlers stalls the event loop.**
`core/src/jevrouter/app.py:35,44` — `classify_only`/`decide_route` are `async def` but call `classify()`, which does synchronous blocking `urllib.request.urlopen` (`classifier.py:96`) for up to `timeout_ms` (2000 ms default). During a slow classification the loop is blocked: concurrent `/decide` requests serialize, and `/health` (and the Pi client's 1 s health check, `client.ts:67`) times out. For a daemon every agent turn hits, this is a throughput/availability bug.
Fix: make the handlers plain `def` (FastAPI runs them in the threadpool) or wrap with `starlette.concurrency.run_in_threadpool`.

## Warnings (should fix)

**W1. `or`-chain drops falsy answer values.** `classifier.py:69` — `a.get("value") or a.get("primary") or …`: a noul `{"value": false}` is falsy → skipped → `None` → argmax over probs yields the *string* `"false"` (line 71). Value typing becomes inconsistent (bool vs string), so `needs_spawn == false` (YAML bool) never matches while `== "false"` does. Use `if a.get("value") is None` chaining. Same class: a score value `0` is dropped.

**W2. `raw.keys()` on non-dict response crashes uncaught.** `classifier.py:106` — if the server returns a JSON array, `normalize_response` returns `{}` (via `.get` on a list? no — `raw.get` raises `AttributeError` inside the guarded try at 101–104, actually `src.items()` on a list raises there and is caught; but line 106's `raw.keys()` executes only when `answers` is empty and `raw` is a *list that somehow normalized empty* — still, `list.keys()` is `AttributeError` uncaught → 500). Type-check `raw` at entry to `normalize_response`.

**W3. `lane_template` catches only `KeyError`.** `engine.py:98-101` — `"{0}".format(...)` → `IndexError`; `"{x!x}"`/unbalanced-brace templates → `ValueError`. Both escape `decide()` → 500 from `/decide` without the `X-Router-Protocol` header. Catch `Exception` around `.format` (or pre-validate templates at load with `string.Formatter().parse`).

**W4. Empty `available_models` means "don't filter".** `engine.py:103` — `set(context.get("available_models") or []) or None`: an adapter reporting `[]` (nothing available) is indistinguishable from "not reported", so the first lane entry wins and you route to a model the harness doesn't have. Distinguish `None` from `[]`.

**W5. Hardcoded `needs_spawn` question name.** `engine.py:143` — `answers.get("needs_spawn", …)`. The spawn question's name is user-configurable (the PL pack calls it `potrzebuje_subagenta`, examples/categories/pl.yaml:23); renaming silently pins prob to 0.0 and disables spawn policy forever. Add `spawn.question: "needs_spawn"` to config.

**W6. `!=` matches missing fields.** `engine.py:17-20` — the `a is None` guard sits *after* `==`/`!=`, so `task_kind != "unclear"` (config.example.yaml:109) passes when `task_kind` is absent entirely (classifier omitted the question). A missing classification then routes via `lane_template "{task_kind}_{difficulty}"` → `None_None` → unresolvable → fallback, which is accidentally safe here, but for other rules `!=` on absent fields is a silent true. Document or make missing-field `!=` return False.

**W7. Adapter timeout < core classifier timeout.** `adapters/hermes/__init__.py:32` default 1500 ms vs `config.example.yaml:14` classifier `timeout_ms: 2000`. Every slow-but-successful classification is abandoned by the adapter while the core keeps burning the request and logging a decision the harness ignored. Adapter timeout must exceed core timeout + HTTP overhead (e.g., core 2000 → adapter 2500+).

**W8. Log failure 500s a valid decision.** `app.py:58-59, 74-75` — `log.write` is inside the try but only `ClassifierUnavailable`/`ClassifierError` are caught; a disk-full/permission error on `~/.jevrouter/decisions.jsonl` turns a computed decision into a 500. Wrap `log.write` in its own try/except and log-and-continue.

**W9. Malformed request body → unhandled 500.** `app.py:36,46` — `await req.json()` raises `JSONDecodeError` on bad JSON (and `body.get` fails if body is a list). Return a structured 400 with the protocol header.

**W10. No validation of rule `then` actions or field references.** `config.py:76-83` validates `when.op` but not: unknown action keys (typo `veto_spwn` silently does nothing), `pin` missing `model`, `lane`/`lane_template` referencing unknown lanes, or `when.field` referencing a field no question/context produces — the exact failure mode of C3. A load-time check ("field must be `<question>`, `<question>_confidence`, `<question>_prob`, or a documented context field") would have caught the broken example.

**W11. Noul `true_means`/`false_means` are silently dropped from the wire.** `classifier.py:51-57` — `build_questions_payload` sends only `type`/`instructions` (+`options`/`levels`). The example config's `true_means`/`false_means` (config.example.yaml:51-52, pl.yaml:26-27) never reach the classifier, so noul semantics rely entirely on `instructions`. Either fold them into `instructions` or pass them through.

**W12. `state.last_messages` / `state.max_tokens` are dead config.** `config.py:94` loads them; SPI.md:11 says state is "token-capped by adapter"; the Hermes adapter hardcodes `n=4` (`__init__.py:58`) and never caps length; Pi adapter doesn't build state at all. Either wire the config through or remove it from the example.

**W13. Hermes adapter never checks `X-Router-Protocol`.** SPI.md:4 mandates "Adapters must check it at startup and refuse mismatched majors." Pi client checks per-call (`client.ts:51-55`); the Hermes adapter ignores the header entirely.

**W14. Spawn-veto mechanism is aspirational, comment is wrong.** `adapters/hermes/__init__.py:137-139` — comment says "failing the call politely"; code only adds an `__jev_router_veto` key that `delegate_task` almost certainly ignores. Either implement a real veto (raise/return a failure shape Hermes honors) or rename the comment to "best-effort hint, unverified" and gate on a Hermes capability.

**W15. JSON decode error mislabeled "classifier_unavailable".** `classifier.py:98-99` — a server that's up but returning garbage takes the "down" policy path (`default_model`/`block`). Operationally different failure; classify decode failures as `ClassifierError` and reserve `ClassifierUnavailable` for connection/timeout/HTTP errors.

## Nits (optional polish)

- `config.py:39-40` vs `94-95`: `state`/`server` defaults written twice (dataclass default + `load_config` fallback) — drift risk.
- `config.example.yaml:83`: comment lists bare `confidence` as a rule field; only `<name>_confidence` exists (`engine.py:46`).
- `engine.py:52-53`: the `"true" in probs` override applies to *any* answer with a `"true"` prob key, not just noul-typed questions — a choice option literally named `true` gets its `_prob` redefined as P(true).
- `engine.py:128`: "primary = first `choice` question" is dict-order-dependent; a question reorder silently changes which confidence the floor guards. Make it explicit config.
- `engine.py:143`: `answers.get("needs_spawn", {})` crashes if the value is `None` (`.get` on None) — normalize guarantees dicts, but callers (CLI/tests) don't.
- `cli.py:52-56`: catches `ClassifierUnavailable` but not `ClassifierError` → raw traceback on unparseable responses.
- `classifier.py:64`: `cfg.timeout_ms = 0` → `urlopen(timeout=0)` = non-blocking socket, always fails; validate `timeout_ms > 0` in `config.py`.
- `classifier.py:84`: `base_url.rstrip("/") + path` — no check that `path` starts with `/`; `path: "v1/x"` yields `http://hostv1/x`.
- `log.py`: no rotation and default file mode; consider `os.open(..., 0o600)` since `content: full` stores prompt text.
- `tests_e2e_hermes.py:3,9,17`: hardcoded `/opt/data/...` absolute paths — machine-specific, lives at repo root, not runnable elsewhere.
- `client.ts`: no Pi extension entrypoint exists (ROADMAP:16 admits it); README:35 "Pi extension" overstates — it's a library.
- `examples/categories/pl.yaml`: Polish with diacritics stripped ("Przykladowy", "zeby") — if intentional ASCII-safety, say so in a comment; otherwise fix.

## Praise (genuinely good)

1. **Decision-only core separation is clean and consistently held** — no model traffic anywhere in `engine.py`/`app.py`; adapters own application. This is the right seam and it didn't leak.
2. **Fail-open discipline in the client paths**: `_decide`'s catch-all with an explicit `noqa: BLE001 - fail-open by design` comment (`__init__.py:53`) and Pi's `catch { return null; // fail-open }` (`client.ts:58`) make the intent auditable, not accidental.
3. **`normalize_response` defensiveness is documented with its escape hatch** — the module docstring (classifier.py:25-29) names the spike as source of truth and this function as the fix site; that's honest engineering about an unstable wire contract.
4. **Load-time validation of the cheap-but-lethal stuff**: op whitelist, `down` enum, question type/instructions (`config.py:70-83`) fail fast with actionable messages naming the rule/question.
5. **Tests encode real invariants, not plumbing**: `test_pin_survives_confidence_floor` (test_engine.py:83-88) and `test_lane_skips_unavailable_models` (44-47) assert contract-level behavior (source, rule name, chosen model) rather than internals.

## Missing-test checklist

Concrete tests to add:

- `test_normalize_falsy_bool_value` — `{"value": false}` stays `False`, not argmax string `"false"` (W1).
- `test_normalize_missing_value_uses_argmax` / `test_normalize_results_key` / `test_normalize_non_dict_answer_skipped`.
- `test_classify_timeout_raises_unavailable` and `test_classify_malformed_json_raises_error_not_unavailable` (W15) — monkeypatch `urlopen`.
- `test_config_rejects_unknown_op` / `missing_when_then` / `bad_down` / `bad_question_type` — each `ConfigError` branch in `config.py:70-83`.
- `test_config_rejects_rule_field_not_in_namespace` (W10) — would have caught C3.
- `test_decide_lane_template_missing_key_falls_through` and `test_decide_lane_template_malformed_does_not_raise` (W3).
- `test_decide_empty_available_models_vs_none` (W4).
- `test_decide_neq_on_missing_field` (W6) — pin the chosen semantics.
- `test_decide_spawn_flag_accumulation_order` — veto rule *after* a routing rule never applies; document via test.
- `test_apply_spawn_policy_custom_question_name` (W5, once configurable).
- `test_app_decide_classifier_down_block_returns_503` / `..._default_model_returns_200_with_fallback` — httpx/TestClient over `app.py:63-77`.
- `test_app_decide_malformed_body_returns_400` (W9).
- `test_health_carries_protocol_header`.
- `test_log_redact_hash_and_none` — `log.py:18-23` has zero coverage of the redaction contract.
- `test_hermes_adapter_fail_open_on_core_down` — monkeypatch `urlopen` to raise; assert `on_llm_request` returns None (the headline contract, currently untested).
- `test_hermes_adapter_survives_nonserializable_request` (C1 regression).
- `test_thinking_off_downgrades_existing_reasoning_effort` (C2 regression, both adapters).

**Verification note:** findings are from static reading of all files in scope (7 core modules, Hermes adapter, Pi client, 1 test file, e2e script, example configs, SPI/README/ROADMAP). No files modified.
EXIT:0
