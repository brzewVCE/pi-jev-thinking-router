"""jev-router Hermes adapter — SPI v1, effort-only.

Registers:
  - llm_request middleware: asks the jev-router core daemon for an EFFORT
    decision on the current turn and stamps `reasoning_effort` into the
    provider kwargs before the request goes out. Model, messages, system prompt
    and tools are never touched (prompt-cache friendly).
  - tool_request middleware: the same effort decision for delegate_task
    children. SPI v1 decisions carry no `model` field, so the old
    model/provider/base_url injection is gone; the spawn veto stays advisory.

Decision shape (CONTRACT.md): {effort, changed, source, reason_code}.
  changed == false  -> return None, the request is left byte-identical.
  any error         -> return None (fail-open). A dead router never breaks Hermes.

Config (env):
  JEVRROUTER_URL               core daemon base URL (default http://127.0.0.1:8790)
  JEVRROUTER_TIMEOUT_MS        per-call timeout (default 3000)
  JEVRROUTER_ENABLED           "0" disables the adapter entirely (default "1")
  JEVRROUTER_CHILDREN          "0" disables delegate_task routing (default "1")
  JEVRROUTER_SUPPORTED_LEVELS  what this Hermes build can run
                               (default "none,minimal,low,medium,high")
  JEVRROUTER_CAP               user ceiling, e.g. from /reasoning; empty = current
                               effort, so the router only ever moves within it
  JEVRROUTER_DEFAULT_EFFORT    effort assumed when the request carries none
                               (default "medium")
  JEVRROUTER_UI_DEFAULT      startup routing default for new sessions: "on"/"off"
                             (overrides the persisted plugin default; see below)
  JEVRROUTER_UI_STATE        plugin UI state file (default ~/.jev-router/ui-hermes.json)
                             holds the persistent per-toggle defaults

Slash command (all platforms the gateway serves, Telegram included):
  /jev on|off | status | last | history [n] | show on|off
  Footer delivery note: this Hermes build exposes no post-response hook, so the
  per-answer footer is NOT deliverable (verified against the gateway sources, see
  BUILD-2.md). `jev show` records the preference for when a hook exists;
  `jev last` / `jev history` are the peek channels.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_URL = os.environ.get("JEVRROUTER_URL", "http://127.0.0.1:8790").rstrip("/")
# Adapter timeout must EXCEED core backend timeout + HTTP overhead (W7),
# otherwise a slow-but-successful decision gets abandoned mid-flight.
_TIMEOUT = float(os.environ.get("JEVRROUTER_TIMEOUT_MS", "3000")) / 1000.0
_ENABLED = os.environ.get("JEVRROUTER_ENABLED", "1") != "0"
_CHILDREN = os.environ.get("JEVRROUTER_CHILDREN", "1") != "0"
_SUPPORTED_LEVELS = [
    s.strip() for s in os.environ.get(
        "JEVRROUTER_SUPPORTED_LEVELS", "none,minimal,low,medium,high").split(",") if s.strip()
]
_CAP = os.environ.get("JEVRROUTER_CAP", "").strip()
_DEFAULT_EFFORT = os.environ.get("JEVRROUTER_DEFAULT_EFFORT", "medium").strip() or "medium"

_PROTOCOL = "1"


def _decide(request_body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """POST /decide (SPI v1). Returns the decision dict or None on any failure."""
    body = json.dumps(request_body).encode("utf-8")
    req = urllib.request.Request(f"{_URL}/decide", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Router-Protocol", _PROTOCOL)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("ok"):
            return payload.get("decision")
        return None
    except Exception as e:  # noqa: BLE001 - fail-open by design
        logger.warning("jev-router: core unreachable/failed (%s); keeping current effort", e)
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
    try:
        proto_num = int(proto)
    except (TypeError, ValueError):
        _PROTOCOL_OK = False
        raise RuntimeError(f"non-numeric core protocol header {proto!r}")
    if proto_num != 1:
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
    """Extract the tail of the conversation from provider kwargs as the task text,
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


def _current_effort(request: Dict[str, Any]) -> str:
    """The effort the host is about to run with — that is also the fallback."""
    for key in ("reasoning_effort", "effort"):
        val = request.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return _DEFAULT_EFFORT


def _build_request(task: str, current: str, ctx: Dict[str, Any],
                  has_images: bool, turn_id: str = "") -> Dict[str, Any]:
    req: Dict[str, Any] = {
        "host": "hermes",
        "session_id": str(ctx.get("session_id") or "hermes-default"),
        "turn_id": turn_id or str(ctx.get("turn_id") or ""),
        "task": task,
        "recent_turns": "",
        "has_images": has_images,
        "current_effort": current,
        "supported_levels": list(_SUPPORTED_LEVELS),
        "budget_ms": int(_TIMEOUT * 1000),
    }
    # Empty cap => the core defaults it to current_effort (CONTRACT.md).
    if _CAP:
        req["cap"] = _CAP
    return req


def _usable_effort(decision: Optional[Dict[str, Any]]) -> Optional[str]:
    """Effort to stamp, or None: changed=false or a level outside the host palette.

    Shared guard for the llm_request and tool_request paths (audit B1-6):
    Hermes must never run a level it did not declare, even if the core said so.
    """
    if not decision or not decision.get("changed"):
        return None
    effort = decision.get("effort")
    if not isinstance(effort, str) or effort not in _SUPPORTED_LEVELS:
        logger.warning("jev-router: decision level %r not in host palette; request untouched",
                       effort)
        return None
    return effort


def _apply_effort(request: Dict[str, Any], decision: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """changed=false or an unusable level => None (request untouched)."""
    effort = _usable_effort(decision)
    if effort is None:
        logger.debug("jev-router: keep %s (%s)",
                     request.get("reasoning_effort"), (decision or {}).get("reason_code"))
        return None
    updated = dict(request)
    updated["reasoning_effort"] = effort
    return {"request": updated, "source": "jev-router",
            "reason": str(decision.get("reason_code") or "unspecified")}


# ---------------------------------------------------------------------------
# UI / slash-command layer — session-scoped routing + decision visibility
# ---------------------------------------------------------------------------
# State model:
#   * Per-session flags (enabled, show) live in RAM keyed by session id; a
#     toggle in one session never leaks into another.
#   * Every toggle ALSO persists as the default for future sessions in a small
#     JSON file (the switchyard persist_plugin_receipt_mode pattern):
#     {"enabled_default": bool, "show_default": bool}.
#   * JEVRROUTER_UI_DEFAULT ("on"/"off") overrides the persisted enabled
#     default at import time so the operator can pin startup behavior.
#
# Footer delivery — verified against the Hermes gateway sources: there is NO
# post-response / outbound-mutation hook. The gateway awaits its post-turn hook
# but DISCARDS the result (run_inbound.py:1282-1287, `return _agent_result`
# hands back the untouched string); `pre_gateway_dispatch` rewrites the
# INBOUND event only (run_inbound.py:44-46); plugin slash handlers receive
# only the args string (run_inbound.py:998). So `jev show` records the
# preference for a future hook, but the per-answer footer cannot be appended
# without core changes. `jev last` / `jev history` are the peek channels.
# The response wire is NOT hacked via prompt injection or middleware tricks.

_UI_STATE_PATH = os.path.expanduser(
    os.environ.get("JEVRROUTER_UI_STATE", "~/.jev-router/ui-hermes.json"))
_HISTORY_MAX = 50
_NO_SESSION = "hermes-default"

# True only if the gateway ever grows a post-response mutation hook.
_FOOTER_AVAILABLE = False

_ui_lock = threading.Lock()
_ui_sessions: Dict[str, Dict[str, bool]] = {}
_decision_history: Dict[str, list] = {}
_pending_cmd_session: Optional[str] = None


def _parse_bool(raw: Any) -> Optional[bool]:
    if not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    if s in ("on", "1", "true", "enabled", "yes"):
        return True
    if s in ("off", "0", "false", "disabled", "no"):
        return False
    return None


def _load_ui_defaults() -> Dict[str, bool]:
    """Built-in defaults <- persisted file <- JEVRROUTER_UI_DEFAULT env override."""
    d = {"enabled": True, "show": False}
    try:
        if os.path.exists(_UI_STATE_PATH):
            with open(_UI_STATE_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                if isinstance(raw.get("enabled_default"), bool):
                    d["enabled"] = raw["enabled_default"]
                if isinstance(raw.get("show_default"), bool):
                    d["show"] = raw["show_default"]
    except Exception as e:  # noqa: BLE001 - a corrupt state file must not break the plugin
        logger.warning("jev-router: ignoring unreadable UI state %s (%s)", _UI_STATE_PATH, e)
    env_default = _parse_bool(os.environ.get("JEVRROUTER_UI_DEFAULT"))
    if env_default is not None:
        d["enabled"] = env_default
    return d


_ui_defaults = _load_ui_defaults()


def _persist_ui_defaults() -> None:
    """Atomic best-effort write of the persistent defaults (never raises)."""
    try:
        os.makedirs(os.path.dirname(_UI_STATE_PATH) or ".", exist_ok=True)
        tmp = f"{_UI_STATE_PATH}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"enabled_default": _ui_defaults["enabled"],
                      "show_default": _ui_defaults["show"]}, f)
        os.replace(tmp, _UI_STATE_PATH)
    except Exception as e:  # noqa: BLE001 - persistence is best-effort; toggles still apply
        logger.warning("jev-router: could not persist UI defaults to %s (%s)", _UI_STATE_PATH, e)


def _ui_for_locked(session_id: str) -> Dict[str, bool]:
    """Seed-and-get for one session. Caller MUST hold `_ui_lock` (non-reentrant)."""
    st = _ui_sessions.get(session_id)
    if st is None:
        st = {"enabled": _ui_defaults["enabled"], "show": _ui_defaults["show"]}
        _ui_sessions[session_id] = st
    return st


def _ui_for(session_id: str) -> Dict[str, bool]:
    """Per-session flags, seeded from the persistent defaults on first touch."""
    with _ui_lock:
        return _ui_for_locked(session_id)


def _set_flag(session_id: str, key: str, value: bool) -> None:
    with _ui_lock:
        _ui_for_locked(session_id)[key] = value


def _record_decision(session_id: str, decision: Dict[str, Any], kind: str) -> None:
    """Bounded per-session ring of decisions for `jev last` / `jev history`.

    Carries the same fields the core receipts do (effort/source/reason/elapsed);
    the adapter never reads the core's receipt files — the hexagonal boundary
    stays intact and the command works with a core on another box."""
    entry = {
        "ts": time.time(),
        "kind": kind,
        "effort": decision.get("effort"),
        "changed": bool(decision.get("changed")),
        "source": decision.get("source"),
        "reason_code": decision.get("reason_code"),
        "elapsed_ms": decision.get("elapsed_ms"),
    }
    with _ui_lock:
        hist = _decision_history.setdefault(session_id, [])
        hist.append(entry)
        if len(hist) > _HISTORY_MAX:
            del hist[:-_HISTORY_MAX]


def _fetch_health() -> Optional[Dict[str, Any]]:
    """GET /health for `jev status`; None when the core is unreachable."""
    try:
        with urllib.request.urlopen(f"{_URL}/health", timeout=min(_TIMEOUT, 2.0)) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:  # noqa: BLE001 - status must work with a dead core
        return None


def _on_pre_command(**kw: Any) -> None:
    """Capture the session key of an incoming /jev command.

    The gateway hands slash handlers only the args string
    (run_inbound.py:998); the pre_command observer is the one channel that
    carries the session key (run_inbound.py:693-696), so the handler that
    runs right after can scope itself to the issuing session."""
    global _pending_cmd_session
    try:
        if str(kw.get("command") or "").strip().lower() == "jev":
            sk = kw.get("session_key")
            if isinstance(sk, str) and sk.strip():
                _pending_cmd_session = sk.strip()
    except Exception:  # noqa: BLE001 - an observer must never break dispatch
        pass


def _command_session(parts: list, ctx: Dict[str, Any]) -> "tuple[str, list]":
    """Resolve the target session and return the parts without `--session`.

    Precedence: explicit `--session <id>` > handler kwargs (should the host
    ever pass them) > the pre_command stash > the no-session bucket."""
    rest = list(parts)
    sid: Optional[str] = None
    if "--session" in rest:
        i = rest.index("--session")
        if i + 1 < len(rest):
            sid = rest[i + 1]
            del rest[i:i + 2]
        else:
            del rest[i]
    if not sid:
        for key in ("session_id", "session_key"):
            v = ctx.get(key)
            if isinstance(v, str) and v.strip():
                sid = v.strip()
                break
    if not sid:
        sid = _pending_cmd_session
    return (sid or _NO_SESSION), rest


_JEV_HELP = (
    "jev-router — usage:\n"
    "  jev on | off          enable/disable routing (this session + persistent default)\n"
    "  jev status            core/backend, cap, levels, visibility\n"
    "  jev last              last routing decision this session\n"
    "  jev history [n]       last n decisions this session (default 5, max 50)\n"
    "  jev show on | off     decision-footer visibility (delivery: see jev status)\n"
    "  --session <id>        target a specific session (multi-session control)"
)


def _fmt_decision(e: Dict[str, Any]) -> str:
    elapsed = e.get("elapsed_ms")
    s = "effort={} source={} reason={} elapsed={}".format(
        e.get("effort", "-"), e.get("source", "-"), e.get("reason_code", "-"),
        f"{elapsed}ms" if elapsed is not None else "-")
    if e.get("kind") and e.get("kind") != "turn":
        s += f" [{e.get('kind')}]"
    return s


def _status_text(sid: str) -> str:
    st = _ui_for(sid)
    health = _fetch_health()
    if health is None:
        core_line = (f"  core: {_URL} — UNREACHABLE "
                     "(on/off/show still work; routing fail-open)")
    else:
        core_line = ("  core: {} — online (protocol {}, backend: {}, warmup: {}, model: {})"
                    .format(_URL, health.get("protocol_version", "?"),
                           health.get("backend", "?"), health.get("warmup", "?"),
                           health.get("model") or "-"))
    cap_line = f"  cap: {_CAP}" if _CAP else "  cap: current effort (no explicit JEVRROUTER_CAP)"
    with _ui_lock:
        n = len(_decision_history.get(sid, []))
    if st["show"]:
        footer_line = ("  footer: ON — delivery unavailable on this Hermes build "
                       "(no post-response hook, see BUILD-2.md); use 'jev last'")
    else:
        footer_line = "  footer: OFF"
    return "\n".join([
        f"⚡ jev-router status (session '{sid}')",
        "  routing: {} (default for new sessions: {})".format(
            "ON" if st["enabled"] else "OFF",
            "on" if _ui_defaults["enabled"] else "off"),
        footer_line,
        core_line,
        cap_line,
        f"  supported levels: {', '.join(_SUPPORTED_LEVELS)}",
        f"  decisions this session: {n}",
    ])


def _last_text(sid: str) -> str:
    with _ui_lock:
        hist = list(_decision_history.get(sid, []))
    if not hist:
        return f"jev: no decision recorded yet for session '{sid}'."
    return f"jev last ({sid}): {_fmt_decision(hist[-1])}"


def _history_text(sid: str, rest: list) -> str:
    n = 5
    if rest:
        try:
            n = int(rest[0])
        except ValueError:
            return "usage: jev history [n]  (n = 1..50)"
        n = max(1, min(n, _HISTORY_MAX))
    with _ui_lock:
        hist = list(_decision_history.get(sid, []))
    if not hist:
        return f"jev: no decisions recorded yet for session '{sid}'."
    tail = hist[-n:]
    lines = [f"jev history — last {len(tail)} decision(s) for session '{sid}':"]
    lines += [f"  {i}. {_fmt_decision(e)}" for i, e in enumerate(tail, 1)]
    return "\n".join(lines)


def _handle_jev(raw_args: str = "", **ctx: Any) -> str:
    """Slash-command entry. The gateway passes only the args string; session
    scoping comes from `--session`, handler kwargs, or the pre_command stash."""
    parts = (raw_args or "").strip().split()
    if not parts:
        return _JEV_HELP
    sub = parts[0].lower()
    sid, rest = _command_session(parts[1:], ctx)
    if sub in ("on", "off"):
        enabled = sub == "on"
        _set_flag(sid, "enabled", enabled)
        with _ui_lock:
            _ui_defaults["enabled"] = enabled
        _persist_ui_defaults()
        return (f"⚡ jev-router: routing {sub.upper()} for session '{sid}'. "
                f"Default for new sessions: {sub}.")
    if sub == "status":
        return _status_text(sid)
    if sub == "last":
        return _last_text(sid)
    if sub == "history":
        return _history_text(sid, rest)
    if sub == "show":
        val = _parse_bool(rest[0]) if rest else None
        if val is None:
            return "usage: jev show on|off"
        _set_flag(sid, "show", val)
        with _ui_lock:
            _ui_defaults["show"] = val
        _persist_ui_defaults()
        msg = (f"⚡ jev-router: decision footer {'ON' if val else 'OFF'} for session "
               f"'{sid}' (default for new sessions: {'on' if val else 'off'}).")
        if val:
            msg += (" Note: footer delivery is unavailable on this Hermes build "
                    "(no post-response hook, see BUILD-2.md); use 'jev last'.")
        return msg
    return _JEV_HELP


def on_llm_request(request: Dict[str, Any], original_request: Dict[str, Any], **ctx: Any):
    if not _ENABLED:
        return None
    try:
        sid = str(ctx.get("session_id") or _NO_SESSION)
        if not _ui_for(sid)["enabled"]:
            return None
        state = _last_messages_from_kwargs(request)
        if not state.strip():
            return None
        current = _current_effort(request)
        decision = _decide(_build_request(state, current, ctx, _has_images(request)))
        if decision:
            _record_decision(sid, decision, "turn")
        return _apply_effort(request, decision or {})
    except Exception as e:  # noqa: BLE001 - fail-open by design (C1)
        logger.warning("jev-router: llm_request middleware error (%s); leaving request untouched", e)
        return None


def on_tool_request(tool_name: str, args: Dict[str, Any], original_args: Dict[str, Any], **ctx: Any):
    """Effort-only routing for delegate_task children.

    SPI v1 decisions carry no `model`/`provider`/`base_url`, so the old model
    injection is intentionally gone. The spawn veto stays as a logged advisory
    (W14: delegate_task does not honor it today).
    """
    if not (_ENABLED and _CHILDREN) or tool_name != "delegate_task":
        return None
    try:
        sid = str(ctx.get("session_id") or _NO_SESSION)
        if not _ui_for(sid)["enabled"]:
            return None
        instruction = args.get("task") or args.get("goal") or ""
        if not instruction.strip():
            return None
        current = _current_effort(args)
        decision = _decide(_build_request(instruction, current, ctx, False,
                                         turn_id="child:" + str(ctx.get("turn_id") or "")))
        if decision:
            _record_decision(sid, decision, "child")
        effort = _usable_effort(decision)
        if effort is None:
            return None
        updated = dict(args)
        updated["reasoning_effort"] = effort
        if decision.get("spawn", {}).get("vetoed"):
            updated["__jev_router_veto"] = (
                "core vetoed this spawn (reason: " + str(decision.get("reason_code"))
                + "); handle inline")
        logger.info("jev-router: child effort %s -> %s (reason=%s)",
                    current, effort, decision.get("reason_code"))
        return {"args": updated, "source": "jev-router",
                "reason": "child:" + str(decision.get("reason_code") or "unspecified")}
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
    ctx.register_hook("pre_command", _on_pre_command)
    ctx.register_command("jev", handler=_handle_jev,
                        description="jev-router: on|off, status, last, history [n], show on|off")
    logger.info("jev-router adapter registered (core at %s, window=%s, levels=%s)",
                _URL, _state_window, _SUPPORTED_LEVELS)
