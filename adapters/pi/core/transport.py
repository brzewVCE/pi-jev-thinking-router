"""Backend transport: OpenAI-compat chat/completions over urllib (stdlib only).

Replaces the base repo's `classifier.py` (System One / Kev wire). The contract
(CONTRACT.md § Backend transport) speaks OpenAI-compat now, so the transport is
a chat/completions call whose reply must parse to a whitelisted effort level.

Error taxonomy maps 1:1 onto the closed reason-code set:
  BackendUnavailable -> jev_error   (connection refused, DNS, HTTP 5xx/4xx)
  BackendTimeout     -> jev_timeout (socket timeout / deadline exceeded)
  BackendUnparsable  -> jev_unparsable (bad JSON, no level, level off the palette)

Nothing here knows about hosts, sessions or hysteresis — it moves bytes.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Sequence

from .config import JevConfig


class BackendError(Exception):
    """Base for every backend failure. Carries a reason_code for the decision."""

    reason_code = "jev_error"


class BackendUnavailable(BackendError):
    reason_code = "jev_error"

    def __init__(self, msg: str, http_status: int | None = None):
        super().__init__(msg)
        # 400 from a server that validates reasoning levels server-side is not a
        # dead backend — it is a capability signal (CONTRACT warmup point c).
        self.http_status = http_status


class BackendTimeout(BackendError):
    reason_code = "jev_timeout"


class BackendUnparsable(BackendError):
    reason_code = "jev_unparsable"


def build_request_body(cfg: JevConfig, messages: Sequence[dict[str, str]],
                     max_tokens: int = 64) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": cfg.model,
        "messages": list(messages),
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if cfg.json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def _extract_content(raw: dict[str, Any]) -> str:
    """Pull the assistant text out of an OpenAI-compat reply."""
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        raise BackendUnparsable(f"no choices in response: {list(raw.keys())}")
    first = choices[0]
    if not isinstance(first, dict):
        raise BackendUnparsable("choices[0] is not an object")
    msg = first.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("content"), str):
        return msg["content"]
    # Some servers return the text directly on the choice.
    if isinstance(first.get("text"), str):
        return first["text"]
    raise BackendUnparsable("no message.content in response")


def _strip_fences(text: str) -> str:
    """Models wrap JSON in ``` fences despite being told not to. Unwrap the fence
    only — the payload inside still has to be strict JSON."""
    s = text.strip()
    if s.startswith("```"):
        s = s[3:]
        if s[:4].lower() == "json":
            s = s[4:]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def parse_decision_text(text: str, allowed: Sequence[str]) -> tuple[str, float | None]:
    """Strict, whitelist-only parse. Anything off the palette is unparsable.

    This is the anti-injection chokepoint (audit A8): the selector may only ever
    return a level we already decided is real. Free text, extra keys, nested
    directives — all rejected.
    """
    try:
        obj = json.loads(_strip_fences(text))
    except Exception as e:
        raise BackendUnparsable(f"not JSON: {e}") from e
    if not isinstance(obj, dict):
        raise BackendUnparsable("JSON payload is not an object")
    level = obj.get("effort")
    if not isinstance(level, str):
        raise BackendUnparsable("missing string 'effort' field")
    level = level.strip().lower()
    if level not in allowed:
        raise BackendUnparsable(f"level {level!r} not in allowed set {list(allowed)}")
    conf = obj.get("confidence")
    conf_f: float | None = None
    if isinstance(conf, (int, float)) and not isinstance(conf, bool):
        conf_f = max(0.0, min(1.0, float(conf)))
    return level, conf_f


def chat(cfg: JevConfig, messages: Sequence[dict[str, str]], allowed: Sequence[str],
        timeout_s: float) -> dict[str, Any]:
    """One chat/completions round trip -> {"level", "confidence", "latency_ms"}."""
    if timeout_s <= 0:
        raise BackendTimeout("no time budget left")
    body = json.dumps(build_request_body(cfg, messages)).encode("utf-8")
    req = urllib.request.Request(cfg.chat_completions_url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if cfg.api_key:
        req.add_header("Authorization", f"Bearer {cfg.api_key}")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw_bytes = resp.read()
    except socket.timeout as e:
        raise BackendTimeout(f"backend timed out after {timeout_s:.3f}s") from e
    except urllib.error.HTTPError as e:
        # Read a little of the body for the message, but never the request headers
        # (they carry the key).
        detail = ""
        try:
            detail = e.read(256).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        raise BackendUnavailable(f"backend HTTP {e.code}: {detail[:200]}",
                            http_status=e.code) from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, socket.timeout):
            raise BackendTimeout(f"backend timed out: {e.reason}") from e
        raise BackendUnavailable(f"backend unreachable: {e.reason}") from e
    except (ConnectionError, OSError) as e:
        raise BackendUnavailable(f"backend connection error: {e}") from e
    latency_ms = int((time.monotonic() - t0) * 1000)
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except Exception as e:
        raise BackendUnparsable(f"undecodable response body: {e}") from e
    if isinstance(raw, dict) and raw.get("error"):
        raise BackendUnavailable(f"backend error payload: {str(raw['error'])[:200]}")
    content = _extract_content(raw)
    level, conf = parse_decision_text(content, allowed)
    return {"level": level, "confidence": conf, "latency_ms": latency_ms}


# -- typesafe kind: native Jev /v1/systemone -----------------------------------

def _task_from_messages(messages: Sequence[dict[str, str]]) -> str:
    """decide.py serializes the STATE block (JSON) into the user message; the
    typesafe backend wants the raw task as `state`, not the chat prompt."""
    import re
    for m in reversed(list(messages)):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            match = re.search(r"STATE[^\n]*\n(.*?)\n\nPick the effort", m["content"], re.S)
            if match:
                try:
                    state = json.loads(match.group(1))
                    task = state.get("task")
                    if isinstance(task, str):
                        return task
                except Exception:  # noqa: BLE001 - fall through to raw text
                    pass
            return m["content"]
    return ""


def systemone(cfg: JevConfig, messages: Sequence[dict[str, str]],
              allowed: Sequence[str], timeout_s: float) -> dict[str, Any]:
    """One POST /v1/systemone round trip -> {"level","confidence","latency_ms"}.

    The Jev wire: {state, model, questions:{effort:{type:choice, criteria}}}
    -> answers.effort.{choice, confidence}. The choice must be on the offered
    palette or it is unparsable (same chokepoint as the JSON kind).
    """
    if timeout_s <= 0:
        raise BackendTimeout("no time budget left")
    from .policy import EFFORT_CRITERIA
    body = json.dumps({
        "model": cfg.model,
        "state": _task_from_messages(messages),
        "questions": {"effort": {
            "type": "choice",
            "instructions": "Reasoning effort the executor model should use for this turn.",
            "criteria": {lvl: EFFORT_CRITERIA.get(lvl, lvl) for lvl in allowed},
        }},
    }).encode("utf-8")
    url = cfg.api_url.rstrip("/") + "/systemone"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if cfg.api_key:
        req.add_header("Authorization", f"Bearer {cfg.api_key}")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except socket.timeout as e:
        raise BackendTimeout(f"backend timed out after {timeout_s:.3f}s") from e
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read(256).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        raise BackendUnavailable(f"backend HTTP {e.code}: {detail[:200]}",
                                 http_status=e.code) from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, socket.timeout):
            raise BackendTimeout(f"backend timed out: {e.reason}") from e
        raise BackendUnavailable(f"backend unreachable: {e.reason}") from e
    except (ConnectionError, OSError, ValueError) as e:
        raise BackendUnavailable(f"backend connection error: {e}") from e
    latency_ms = int((time.monotonic() - t0) * 1000)
    try:
        ans = raw["answers"]["effort"]
        level = str(ans["choice"]).strip().lower()
    except (KeyError, TypeError) as e:
        raise BackendUnparsable(f"no answers.effort.choice in reply: {list(raw.keys())}") from e
    if level not in allowed:
        raise BackendUnparsable(f"level {level!r} not in allowed set {list(allowed)}")
    conf = ans.get("confidence")
    conf_f = float(conf) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else None
    if conf_f is not None:
        conf_f = max(0.0, min(1.0, conf_f))
    return {"level": level, "confidence": conf_f, "latency_ms": latency_ms}


def call_backend(cfg: JevConfig, messages: Sequence[dict[str, str]],
                 allowed: Sequence[str], timeout_s: float) -> dict[str, Any]:
    """Kind dispatch: the single place that knows openai-compat vs typesafe."""
    if cfg.kind == "typesafe":
        return systemone(cfg, messages, allowed, timeout_s)
    return chat(cfg, messages, allowed, timeout_s)
