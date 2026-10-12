# Roadmap

> 2026-09-28 re-prioritization (owner): model switching mid-conversation is a prompt-cache
> miss — re-prefill costs more than tiered models save. Phase 1 therefore routes
> **thinking level only** (same model, cache prefix intact). Model routing is demoted to an
> opt-in for session *boundaries* (new session / explicit pin), never per-turn.
> Dynamic subagent spawning moves up as the primary cost lever: children start fresh
> sessions, so a cheap child model costs zero cache.

## Phase 1 — thinking-only routing (current, re-scoped)
- [x] Core: config schema + loader with validation
- [x] Core: System One wire client (Kev/Laya/Jev-cloud, defensive normalizer)
- [x] Core: rule DSL interpreter (when/op/then, lanes, pins, spawn flags)
- [x] Core: confidence floor, fallback, classifier_down policy
- [x] Core: JSONL decision log (full/hash/none)
- [x] Core: FastAPI daemon (/classify /decide /health, X-Router-Protocol)
- [x] Core: CLI (classify / decide / serve / validate)
- [x] Offline engine tests (12)
- [x] Hermes adapter: llm_request middleware (model + reasoning_effort rewrite)
- [x] Pi adapter: core client + applyDecision (setModel/setThinkingLevel)
- [ ] **THINKING-ONLY MODE**: config `routing.mode: thinking_only` (default) —
      decision carries `thinking` only; `model` field ignored mid-session.
      Lanes collapse to thinking levels; per-turn model rewrite disabled in both adapters.
- [ ] Model change allowed only at: session start, user pin (`/pin`), or explicit
      `model_change: on_new_session` rule action.
- [ ] SPIKE: Kev-4B zero-shot quality on own prompts (blocks enabling, not building)

## Phase 2 — dynamic subagent spawning (promoted; the real savings)
- [ ] needs_spawn (noul) + threshold enforced per prompt — core decides, adapter spawns
- [ ] Child-instruction classification -> (model, thinking) for the CHILD.
      Child = fresh session = no parent cache to break -> cheap model here is pure win.
- [ ] Hermes: tool_request middleware on delegate_task -> full override
      {model, provider, base_url, reasoning}; subagent_lifecycle launch as model-only path
- [ ] Pi: spawnChild via pi-subagents RPC (library does execution; core supplies decision)
- [ ] Result gate (pi-jev-gate pattern: noul vs diff/output), auto-escalation with limits
- [ ] Spawn budget: max_parallel_children, max_children_per_session, cost ceiling in log

## Phase 3 — force spawn + cross-harness polish
- [ ] force mode: core scans prompt BEFORE parent turn and mandates delegation
- [ ] SPIKE: can Hermes tool_request middleware swap the *tool* (answer -> delegate_task)
      or only args? Plan B: prompt-injection + veto mode.
- [ ] Pi: adapter intercepts turn before run
- [ ] adapter-codex (SPI consumer; first non-Pi/Hermes harness proof)

## Deferred (was Phase 1/2, parked by cache-miss rationale)
- [ ] Per-turn model routing (lanes with model swaps) — keep code, hide behind
      `routing.mode: model` opt-in; document cache cost with measured numbers
- [ ] Stickiness heuristics (min_turns_before_downgrade) — largely moot under
      thinking-only mode; revisit only if model mode is re-enabled

## Out of scope (explicit)
- Fine-tuning / training (zero-shot only)
- Full request proxying (decision-only core)
- GPU scheduling (user self-hosts the classifier)
- Built-in PII policy (PII rules are user-defined examples)
