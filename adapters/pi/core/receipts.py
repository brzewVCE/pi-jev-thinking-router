"""Receipts: sanitized JSONL audit trail (CONTRACT.md § Receipts).

Daily-rotated file: ~/.jev-router/receipts-YYYYMMDD.jsonl

Sanitization rules (audit A3/A11):
  * any request key that smells like a credential is dropped recursively;
  * task/recent_turns are stored as a bounded excerpt (tail) or a hash, never as
    the whole conversation, per JEV_RECEIPT_CONTENT:
        full    -> verbatim (still secret-stripped)
        excerpt -> tail of `excerpt_chars` (default)
        hash    -> sha256 prefix only
        none    -> omitted
  * the API key value is scrubbed from string values too, as a belt-and-braces
    pass, so a key echoed by a buggy upstream cannot land on disk.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any

SECRET_KEY_HINTS = ("api_key", "apikey", "key", "token", "secret",
                   "authorization", "password", "credential", "bearer")

CONTENT_FIELDS = ("task", "recent_turns")


def _is_secret_key(key: str) -> bool:
    k = key.strip().lower()
    return any(h in k for h in SECRET_KEY_HINTS)


def strip_secrets(obj: Any) -> Any:
    """Recursively drop credential-shaped keys."""
    if isinstance(obj, dict):
        return {k: strip_secrets(v) for k, v in obj.items() if not _is_secret_key(str(k))}
    if isinstance(obj, list):
        return [strip_secrets(v) for v in obj]
    return obj


def scrub_value(obj: Any, secret: str) -> Any:
    """Replace any occurrence of the literal secret string inside values."""
    if not secret:
        return obj
    if isinstance(obj, str):
        return "<redacted>" if secret in obj else obj
    if isinstance(obj, dict):
        return {k: scrub_value(v, secret) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_value(v, secret) for v in obj]
    return obj


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def sanitize_request(req: dict[str, Any], mode: str, excerpt_chars: int,
                    secret: str = "") -> dict[str, Any]:
    """Build the receipt view of a /decide request."""
    out = strip_secrets(req)
    out = scrub_value(out, secret)
    for f in CONTENT_FIELDS:
        if f not in out:
            continue
        val = out.get(f)
        if not isinstance(val, str):
            continue
        if mode == "full":
            continue
        if mode == "hash":
            out[f] = _digest(val)
        elif mode == "none":
            out.pop(f, None)
        else:  # excerpt — keep the tail, that is where the live ask is
            if len(val) > excerpt_chars:
                out[f] = "…" + val[-excerpt_chars:]
            if f == "recent_turns" and mode == "excerpt":
                # recent_turns is bulk history: hash it, keep only its size.
                out[f] = _digest(val)
    return out


class ReceiptWriter:
    """Append-only JSONL with daily rotation. Thread-safe; write failures never
    propagate (W8 from the base: a full disk must not destroy a decision)."""

    def __init__(self, home: str, mode: str = "excerpt", excerpt_chars: int = 200,
                 secret: str = "", clock: Callable | None = None):  # noqa: F821
        self.home = os.path.expanduser(home)
        self.mode = mode
        self.excerpt_chars = excerpt_chars
        self.secret = secret
        self._clock = clock or time.time
        self._lock = threading.Lock()

    def path_for(self, ts: float | None = None) -> str:
        stamp = time.strftime("%Y%m%d", time.gmtime(ts if ts is not None else self._clock()))
        return os.path.join(self.home, f"receipts-{stamp}.jsonl")

    def write(self, *, request: dict[str, Any], decision: dict[str, Any],
             elapsed_ms: int, warmup_status: str = "") -> dict[str, Any]:
        ts = self._clock()
        entry = {
            "ts": ts,
            "date": time.strftime("%Y%m%d", time.gmtime(ts)),
            "host": request.get("host"),
            "session_id": request.get("session_id"),
            "turn_id": request.get("turn_id"),
            "request": sanitize_request(request, self.mode, self.excerpt_chars, self.secret),
            "decision": decision,
            "reason_code": decision.get("reason_code"),
            "elapsed_ms": elapsed_ms,
            "warmup": warmup_status,
            "policy_version": decision.get("policy_version"),
        }
        line = json.dumps(entry, ensure_ascii=False, default=str)
        try:
            with self._lock:
                path = self.path_for(ts)
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
                try:
                    os.write(fd, (line + "\n").encode("utf-8"))
                finally:
                    os.close(fd)
                if os.stat(path).st_mode & 0o077:
                    os.chmod(path, 0o600)
        except OSError:
            pass
        return entry
