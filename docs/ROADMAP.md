# Roadmap

## Phase 1 — MVP (current)
- [x] Core: config schema + loader with validation
- [x] Core: System One wire client (Kev/Laya/Jev-cloud, defensive normalizer)
- [x] Core: rule DSL interpreter (when/op/then, lanes, pins, spawn flags)
- [x] Core: confidence floor, fallback, classifier_down policy
- [x] Core: JSONL decision log (full/hash/none)
- [x] Core: FastAPI daemon (/classify /decide /health, X-Router-Protocol)
- [x] Core: CLI (classify / decide / serve / validate)
- [x] Offline engine tests (12)
- [x] Hermes adapter: llm_request middleware (model + reasoning_effort rewrite)
- [x] Hermes adapter: tool_request middleware (delegate_task child routing)
- [x] Pi adapter: core client + applyDecision (setModel/setThinkingLevel)
- [ ] SPIKE: Kev-4B zero-shot quality on own prompts (blocks enabling, not building)
- [ ] Wire Pi extension entrypoint (pi install git:...)

## Phase 2 — child routing + gate
- [ ] Child-instruction classification -> (model, thinking) into pi-subagents / delegate_task
- [ ] Result gate (pi-jev-gate pattern: noul vs diff/output), auto-escalation with limits
- [ ] Spawn veto end-to-end in both harnesses
- [ ] Stickiness (keep expensive model during long conversations for cache reuse)

## Phase 3 — smart spawn (core-initiated)
- [ ] needs_spawn threshold enforcement
- [ ] SPIKE: can Hermes tool_request middleware swap the *tool* (answer -> delegate_task)
      or only args? Plan B: prompt-injection + veto mode.
- [ ] force mode in Pi (adapter intercepts turn before run)

## Out of scope (explicit)
- Fine-tuning / training (zero-shot only)
- Full request proxying (decision-only core)
- GPU scheduling (user self-hosts the classifier)
- Built-in PII policy (PII rules are user-defined examples)
