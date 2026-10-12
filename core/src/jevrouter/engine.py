"""Rule interpreter: classification answers -> routing decision.

Decision = {model, thinking, lane?, rule?, spawn: {allowed, forced, vetoed}}
The core NEVER calls models; it only decides. Adapters apply.
"""
from __future__ import annotations

from typing import Any

from .config import RouterConfig


def _cmp(op: str, a: Any, b: Any) -> bool:
    try:
        if op == "==":
            return a == b
        if op == "!=":
            # W6: a missing field must NOT satisfy `!=` — you cannot prove a
            # value differs from something when the value is absent.
            return a is not None and a != b
        if a is None:
            return False
        if op == ">":
            return a > b
        if op == ">=":
            return a >= b
        if op == "<":
            return a < b
        if op == "<=":
            return a <= b
        if op == "in":
            return a in b
    except TypeError:
        return False
    return False


def build_fields(answers: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Flatten answers + adapter context into the rule field namespace.

    Every answer exposes: <name> (value), <name>_confidence, <name>_prob
    (probability of the chosen value). Adapter context adds has_images,
    prompt_tokens, harness, session_turn, and any custom fields.
    """
    fields: dict[str, Any] = dict(context)
    for name, a in answers.items():
        fields[name] = a.get("value")
        fields[f"{name}_confidence"] = a.get("confidence", 0.0)
        probs = a.get("probabilities") or {}
        val = a.get("value")
        fields[f"{name}_prob"] = probs.get(val, a.get("confidence", 0.0))
        # For noul-style answers (bool value), expose the probability of TRUE
        # directly so rules like `pii_prob >= 0.5` read naturally. Restricted
        # to bool-valued answers so a choice option literally named "true"
        # cannot hijack its _prob field (nit).
        if isinstance(val, bool):
            fields[f"{name}_prob"] = probs.get("true", 1.0 if val else 0.0)
    return fields


def resolve_lane(lane_name: str, lanes: dict[str, list[dict[str, str]]], available: set[str] | None) -> dict[str, str] | None:
    """First entry in the lane whose model exists in the harness registry.

    `available` = set of model ids the adapter reported; None means
    "adapter does not filter" and the first entry wins.
    """
    entries = lanes.get(lane_name)
    if not entries:
        return None
    for e in entries:
        if available is None or e.get("model") in available:
            return e
    return None


def decide(cfg: RouterConfig, answers: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    fields = build_fields(answers, context)
    spawn_state = {"allowed": False, "forced": False, "vetoed": False}

    for rule in cfg.rules:
        w = rule["when"]
        if not _cmp(w["op"], fields.get(w["field"]), w["value"]):
            continue
        t = rule["then"]

        # Spawn flags accumulate across matched rules even when they carry no
        # routing action (e.g. a pure veto rule), so a later routing rule still
        # sees the veto.
        if t.get("veto_spawn"):
            spawn_state["vetoed"] = True
        if t.get("force_spawn"):
            spawn_state["forced"] = True

        decision: dict[str, Any] = {"rule": rule.get("name", "unnamed"), "matched_fields": {w["field"]: fields.get(w["field"])}}

        if "pin" in t:
            decision.update(t["pin"])
            decision["source"] = "pin"
        else:
            lane_name = t.get("lane")
            if "lane_template" in t:
                try:
                    lane_name = t["lane_template"].format(**fields)
                except Exception:  # W3: KeyError/IndexError/ValueError on malformed templates
                    lane_name = None
            if lane_name:
                raw_models = context.get("available_models")
                # W4: distinguish "adapter reported nothing available" ([]) from
                # "adapter did not report" (None). [] means NO model is usable.
                if raw_models is None:
                    available = None
                else:
                    available = set(raw_models)
                    if not available:
                        continue  # harness has no models -> next rule / fallback
                hit = resolve_lane(lane_name, cfg.lanes, available)
                if hit:
                    decision.update(hit)
                    decision["lane"] = lane_name
                    decision["source"] = "lane"
                else:
                    continue  # lane unresolvable -> try next rule, ultimately fallback
            else:
                continue  # spawn-only rule matched; keep scanning for a routing rule

        decision["spawn"] = spawn_state
        return decision

    # No rule matched -> fallback
    fb = cfg.fallback.get("default", {})
    return {"model": fb.get("model"), "thinking": fb.get("thinking", "off"),
            "source": "fallback", "rule": None, "spawn": spawn_state}


def apply_confidence_floor(cfg: RouterConfig, decision: dict[str, Any], answers: dict[str, Any]) -> dict[str, Any]:
    """If the PRIMARY classification (first `choice` question in config) has
    confidence below the floor, force fallback. Pins are user intent and survive."""
    floor = float(cfg.fallback.get("confidence_floor", 0.0))
    if floor <= 0:
        return decision
    primary_name = next((n for n, q in cfg.questions.items() if q.get("type") == "choice"), None)
    if primary_name is None:
        return decision
    primary = answers.get(primary_name)
    if primary is not None and primary.get("confidence", 0.0) < floor and decision.get("source") != "pin":
        fb = cfg.fallback.get("default", {})
        return {"model": fb.get("model"), "thinking": fb.get("thinking", "off"),
                "source": "confidence_floor", "rule": None, "spawn": decision.get("spawn", {})}
    return decision


def apply_spawn_policy(cfg: RouterConfig, decision: dict[str, Any], answers: dict[str, Any]) -> dict[str, Any]:
    spawn_cfg = cfg.spawn
    if not spawn_cfg.get("enabled", False):
        return decision
    # W5: the spawn question name is configurable (e.g. PL pack:
    # "potrzebuje_subagenta"); default keeps backward compat.
    qname = spawn_cfg.get("question", "needs_spawn")
    ans = answers.get(qname) or {}
    prob = float((ans.get("probabilities") or {}).get("true", 0.0))
    threshold = float(spawn_cfg.get("threshold", 0.7))
    st = decision.setdefault("spawn", {"allowed": False, "forced": False, "vetoed": False})
    st["needs_spawn_prob"] = prob
    if st.get("vetoed"):
        st["allowed"] = False
    elif prob >= threshold:
        st["allowed"] = True
        if spawn_cfg.get("mode") == "force":
            st["forced"] = True
    return decision
