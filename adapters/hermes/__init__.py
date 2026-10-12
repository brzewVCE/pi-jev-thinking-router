"""jev-router Hermes adapter.

Registers:
  - llm_request middleware: asks the jev-router-core daemon for a decision on
    the current conversation state and rewrites {model, reasoning_effort} in
    the provider kwargs before the request goes out.
  - tool_request middleware: when the model calls delegate_task, asks the core
    to classify the child instruction and injects {model, provider, base_url}
    overrides into the tool args (delegate_task supports these).

Fail-open everywhere: any error (core down, timeout, bad response) leaves the
request untouched and logs a warning. Hermes middleware is fail-open by
contract, so a dead router never breaks the agent.

Config (env):
  JEVRROUTER_URL        core daemon base URL (default http://127.0.0.1:8790)
  JEVRROUTER_TIMEOUT_MS per-call timeout (default 1500)
  JEVRROUTER_ENABLED    "0" disables the adapter entirely (default "1")
  JEVRROUTER_CHILDREN   "0" disables delegate_task routing (default "1")
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_URL = os.environ.get("JEVRROUTER_URL", "http://127.0.0.1:8790").rstrip("/")
_TIMEOUT = float(os.environ.get("JEVRROUTER_TIMEOUT_MS", "1500")) / 1000.0
_ENABLED = os.environ.get("JEVRROUTER_ENABLED", "1") != "0"
_CHILDREN = os.environ.get("JEVRROUTER_CHILDREN", "1") != "0"


def _decide(state: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """POST /decide; returns decision dict or None on any failure."""
    body = json.dumps({
        "state": state,
        "context": context,
        "harness": "hermes",
        "session": str(context.get("session_id") or ""),
    }).encode("utf-8")
    req = urllib.request.Request(f"{_URL}/decide", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("ok"):
            return payload.get("decision")
        return None
    except Exception as e:  # noqa: BLE001 - fail-open by design
        logger.warning("jev-router: core unreachable/failed (%s); keeping current model", e)
        return None


def _last_messages_from_kwargs(request: Dict[str, Any], n: int = 4) -> str:
    """Extract the tail of the conversation from provider kwargs as classifier state."""
    msgs = request.get("messages") or request.get("input") or []
    tail: list[str] = []
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                tail.append(content)
            elif isinstance(content, list):  # multimodal parts
                parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                if parts:
                    tail.append(" ".join(parts))
        if len(tail) >= n:
            break
    return "\n---\n".join(reversed(tail))


def _has_images(request: Dict[str, Any]) -> bool:
    for m in request.get("messages") or []:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list):
            if any(isinstance(p, dict) and p.get("type") in ("image_url", "image") for p in content):
                return True
    return False


def on_llm_request(request: Dict[str, Any], original_request: Dict[str, Any], **ctx: Any):
    if not _ENABLED:
        return None
    state = _last_messages_from_kwargs(request)
    if not state.strip():
        return None
    decision = _decide(state, {
        "has_images": _has_images(request),
        "prompt_tokens": ctx.get("prompt_tokens") or len(json.dumps(request)) // 4,
        "session_turn": ctx.get("turn_id"),
        "session_id": ctx.get("session_id"),
    })
    if not decision or not decision.get("model"):
        return None
    updated = dict(request)
    changed = False
    if decision["model"] != request.get("model"):
        updated["model"] = decision["model"]
        changed = True
    thinking = decision.get("thinking")
    if thinking and thinking != "off":
        # Hermes maps reasoning_config -> provider reasoning_effort; the OpenAI-compat
        # kwarg is reasoning_effort. 'off' means don't request reasoning.
        updated["reasoning_effort"] = thinking
        changed = True
    if not changed:
        return None
    logger.info("jev-router: routed turn -> model=%s thinking=%s (rule=%s)",
               decision.get("model"), thinking, decision.get("rule"))
    return {"request": updated, "source": "jev-router", "reason": f"rule:{decision.get('rule')}"}


def on_tool_request(tool_name: str, args: Dict[str, Any], original_args: Dict[str, Any], **ctx: Any):
    if not (_ENABLED and _CHILDREN) or tool_name != "delegate_task":
        return None
    instruction = args.get("task") or args.get("goal") or ""
    if not instruction.strip():
        return None
    decision = _decide(instruction, {
        "kind": "child_instruction",
        "session_id": ctx.get("session_id"),
    })
    if not decision or not decision.get("model"):
        return None
    updated = dict(args)
    updated["model"] = decision["model"]
    if decision.get("provider"):
        updated["provider"] = decision["provider"]
    if decision.get("base_url"):
        updated["base_url"] = decision["base_url"]
    if decision.get("thinking"):
        updated["reasoning_effort"] = decision["thinking"]
    if decision.get("spawn", {}).get("vetoed"):
        # Veto: tell the parent to do it itself by failing the call politely.
        updated["__jev_router_veto"] = "core vetoed this spawn (rule: " + str(decision.get("rule")) + "); handle inline"
    logger.info("jev-router: child instruction routed -> %s (rule=%s)",
                decision.get("model"), decision.get("rule"))
    return {"args": updated, "source": "jev-router", "reason": f"child-rule:{decision.get('rule')}"}


def register(ctx) -> None:
    if not _ENABLED:
        logger.info("jev-router adapter disabled via JEVRROUTER_ENABLED=0")
        return
    ctx.register_middleware("llm_request", on_llm_request)
    ctx.register_middleware("tool_request", on_tool_request)
    logger.info("jev-router adapter registered (core at %s)", _URL)
