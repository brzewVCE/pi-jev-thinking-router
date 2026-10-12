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
  JEVRROUTER_TIMEOUT_MS per-call timeout (default 3000)
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
# Adapter timeout must EXCEED core classifier timeout + HTTP overhead (W7),
# otherwise slow-but-successful classifications get abandoned mid-flight.
_TIMEOUT = float(os.environ.get("JEVRROUTER_TIMEOUT_MS", "3000")) / 1000.0
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


_state_window = {"last_messages": 4, "max_tokens": 1500}  # defaults; refreshed from core /health
_PROTOCOL_OK = True  # pass2 issue 1: set False on protocol major mismatch; register() refuses


def _refresh_state_window() -> None:
    """W12/W13: pull state window + protocol version from the core at startup.
    Mismatched protocol major disables the adapter (SPI contract)."""
    global _PROTOCOL_OK
    try:
        with urllib.request.urlopen(f"{_URL}/health", timeout=_TIMEOUT) as resp:
            proto = resp.headers.get("X-Router-Protocol", "1")
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 - core down: fail-open with defaults
        logger.warning("jev-router: could not read core /health (%s); using default state window", e)
        return
    if int(proto) != 1:
        # Raised OUTSIDE the try so it cannot be swallowed (pass2 issue 1).
        _PROTOCOL_OK = False
        raise RuntimeError(f"unsupported core protocol {proto}")
    w = payload.get("state") or {}
    _state_window["last_messages"] = int(w.get("last_messages", 4))
    _state_window["max_tokens"] = int(w.get("max_tokens", 1500))


def _cap_tokens(text: str, max_tokens: int) -> str:
    # Rough token cap (~4 chars/token); keeps the tail (most recent context).
    limit = max_tokens * 4
    if len(text) <= limit:
        return text
    return text[-limit:]


def _last_messages_from_kwargs(request: Dict[str, Any]) -> str:
    """Extract the tail of the conversation from provider kwargs as classifier state,
    honoring the core-provided state window (W12)."""
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
        if len(tail) >= _state_window["last_messages"]:
            break
    joined = "\n---\n".join(reversed(tail))
    return _cap_tokens(joined, _state_window["max_tokens"])


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
    try:
        state = _last_messages_from_kwargs(request)
        if not state.strip():
            return None
        decision = _decide(state, {
            "has_images": _has_images(request),
            "prompt_tokens": len(state) // 4,
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
        if thinking == "off":
            # Explicitly CLEAR reasoning so a trivial lane downgrades a request
            # that already carries a high reasoning_effort.
            if request.get("reasoning_effort") not in (None, "none", "off"):
                updated["reasoning_effort"] = "none"
                changed = True
        elif thinking:
            updated["reasoning_effort"] = thinking
            changed = True
        if not changed:
            return None
        logger.info("jev-router: routed turn -> model=%s thinking=%s (rule=%s)",
                   decision.get("model"), thinking, decision.get("rule"))
        return {"request": updated, "source": "jev-router", "reason": f"rule:{decision.get('rule')}"}
    except Exception as e:  # noqa: BLE001 - fail-open by design (C1)
        logger.warning("jev-router: llm_request middleware error (%s); leaving request untouched", e)
        return None


def on_tool_request(tool_name: str, args: Dict[str, Any], original_args: Dict[str, Any], **ctx: Any):
    if not (_ENABLED and _CHILDREN) or tool_name != "delegate_task":
        return None
    try:
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
            # Best-effort veto hint. NOTE (W14): delegate_task does not honor
            # this key today; a real veto needs the phase-3 spike on whether
            # tool_request can fail/replace the call. Until then this is a
            # logged advisory only.
            updated["__jev_router_veto"] = "core vetoed this spawn (rule: " + str(decision.get("rule")) + "); handle inline"
        logger.info("jev-router: child instruction routed -> %s (rule=%s)",
                    decision.get("model"), decision.get("rule"))
        return {"args": updated, "source": "jev-router", "reason": f"child-rule:{decision.get('rule')}"}
    except Exception as e:  # noqa: BLE001 - fail-open by design (C1)
        logger.warning("jev-router: tool_request middleware error (%s); leaving args untouched", e)
        return None


def register(ctx) -> None:
    if not _ENABLED:
        logger.info("jev-router adapter disabled via JEVRROUTER_ENABLED=0")
        return
    try:
        _refresh_state_window()
    except RuntimeError as e:
        logger.error("jev-router adapter NOT registered: %s", e)
        return
    if not _PROTOCOL_OK:
        logger.error("jev-router adapter NOT registered: protocol mismatch")
        return
    ctx.register_middleware("llm_request", on_llm_request)
    ctx.register_middleware("tool_request", on_tool_request)
    logger.info("jev-router adapter registered (core at %s, window=%s)", _URL, _state_window)
