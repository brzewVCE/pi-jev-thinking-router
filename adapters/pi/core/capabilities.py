"""Warmup + capabilities cache (CONTRACT.md § warmup).

The core must never guess the decision backend's palette. At startup (or on the
first /decide for an unknown host+model pair) it runs a SYNTHETIC decision
prompt — zero user data — and records:
  a) the backend answers at all (url/key/model alive),
  b) the reply parses,
  c) which levels the backend actually accepts (server-side validation probing).

Cached per (backend_url, model), TTL default 1h, in RAM + on disk so it survives
a core restart. A failed warmup marks the backend unavailable and every /decide
answers `no_backend` (keep current effort) until POST /warmup or TTL expiry.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .config import JevConfig
from .policy import CANONICAL_LEVELS, selector_messages
from .transport import BackendError, BackendUnavailable, call_backend

CAPABILITIES_FILENAME = "capabilities.json"

# Synthetic warmup task. Fixed, neutral, contains NO user data (audit A6).
WARMUP_TASK = (
    "Synthetic warmup probe (no user data). Trivial self-contained check: "
    "compute 2 + 2 and report the number."
)


@dataclass
class Capabilities:
    backend_url: str
    model: str
    accepted_levels: tuple[str, ...]
    status: str  # "ok" | "failed:<reason>"
    warmed_at: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.status == "ok" and bool(self.accepted_levels)

    def fresh(self, ttl_s: int, now: float | None = None) -> bool:
        """Purely time-based. A FAILED warmup must stay cached too — CONTRACT says
        an unavailable backend keeps answering `no_backend` until a manual
        re-warmup or TTL expiry, so failures cannot be treated as "no entry"."""
        return (now if now is not None else time.time()) - self.warmed_at < ttl_s

    def to_json(self) -> dict[str, Any]:
        return {
            "backend_url": self.backend_url,
            "model": self.model,
            "accepted_levels": list(self.accepted_levels),
            "status": self.status,
            "warmed_at": self.warmed_at,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Capabilities":
        return cls(
            backend_url=str(data.get("backend_url", "")),
            model=str(data.get("model", "")),
            accepted_levels=tuple(data.get("accepted_levels") or ()),
            status=str(data.get("status", "failed:corrupt")),
            warmed_at=float(data.get("warmed_at", 0.0)),
        )


def cache_key(backend_url: str, model: str) -> str:
    return f"{backend_url.rstrip('/')}|{model}"


class CapabilityCache:
    """RAM cache backed by ~/.jev-router/capabilities.json, guarded by a lock.

    The file never contains the API key — only url/model/levels/status/ts.
    """

    def __init__(self, path: str, ttl_s: int = 3600):
        self.path = os.path.expanduser(path)
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._mem: dict[str, Capabilities] = {}
        self._loaded = False

    # -- persistence -------------------------------------------------------
    def _load_disk(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception:  # noqa: BLE001 - corrupt cache is just a cold cache
            return
        if not isinstance(data, dict):
            return
        for key, entry in data.items():
            if isinstance(entry, dict):
                try:
                    caps = Capabilities.from_json(entry)
                except Exception:  # noqa: BLE001
                    continue
                if caps.fresh(self.ttl_s):
                    self._mem[key] = caps

    def _persist(self) -> None:
        """Atomic write with 0600 (pattern carried over from the base DecisionLog:
        create with the final mode, never chmod-after-create)."""
        payload = {k: c.to_json() for k, c in self._mem.items() if c.fresh(self.ttl_s)}
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = f"{self.path}.tmp.{os.getpid()}"
            fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            try:
                os.write(fd, json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"))
            finally:
                os.close(fd)
            os.replace(tmp, self.path)
            if os.stat(self.path).st_mode & 0o077:
                os.chmod(self.path, 0o600)
        except OSError:
            pass  # a disk we cannot write must not break decisions

    # -- api ---------------------------------------------------------------
    def get(self, key: str, now: float | None = None) -> Capabilities | None:
        with self._lock:
            self._load_disk()
            caps = self._mem.get(key)
            if caps is None:
                return None
            if not caps.fresh(self.ttl_s, now):
                del self._mem[key]
                return None
            return caps

    def put(self, caps: Capabilities) -> None:
        with self._lock:
            self._load_disk()
            self._mem[cache_key(caps.backend_url, caps.model)] = caps
            self._persist()

    def clear(self) -> None:
        with self._lock:
            self._mem.clear()
            self._loaded = True
            try:
                os.remove(self.path)
            except OSError:
                pass

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            self._load_disk()
            return {k: c.to_json() for k, c in self._mem.items()}


def _probe_level(cfg: JevConfig, level: str, call: Callable[..., dict[str, Any]]) -> bool:
    """Ask the backend to decide with a single-level palette.

    A backend that validates levels server-side (LM Studio answers 400 for an
    unknown reasoning level) shows up here as a rejection. A backend that answers
    200 with a parsable level accepts it.
    """
    msgs = selector_messages(task=WARMUP_TASK, current_effort=level, cap=level,
                            supported_levels=[level])
    try:
        res = call(cfg, msgs, [level], cfg.timeout_ms / 1000.0)
    except BackendError:
        return False
    return res.get("level") == level


def _fail(cache: CapabilityCache, cfg: JevConfig, reason: str) -> Capabilities:
    caps = Capabilities(cfg.api_url, cfg.model, (), f"failed:{reason}", time.time())
    cache.put(caps)
    return caps

def warmup(cfg: JevConfig, cache: CapabilityCache, *, force: bool = False,
           call: Callable[..., dict[str, Any]] = call_backend,
           levels: Sequence[str] = CANONICAL_LEVELS) -> Capabilities:
    """Run the dry-run and cache the result. Never raises for backend failures —
    it returns a failed Capabilities so callers can keep serving keep-decisions."""
    key = cache_key(cfg.api_url, cfg.model)
    if not force:
        cached = cache.get(key)
        if cached is not None:
            return cached

    if not cfg.backend_configured:
        return _fail(cache, cfg, "no_backend")

    # (a)+(b): primary synthetic call over the full canonical ladder.
    ladder = tuple(levels)
    msgs = selector_messages(task=WARMUP_TASK, current_effort="medium", cap="max",
                            supported_levels=list(ladder))
    try:
        call(cfg, msgs, list(ladder), cfg.timeout_ms / 1000.0)
        # The backend took the whole palette: every canonical level is real.
        accepted = ladder
    except BackendUnavailable as e:
        # A 400 here means the backend validates reasoning levels server-side and
        # rejected the full palette. That is NOT a dead backend — probe level by
        # level and keep the accepted subset (CONTRACT warmup point c).
        if e.http_status != 400:
            return _fail(cache, cfg, e.reason_code)
        if not cfg.warmup_probe:
            return _fail(cache, cfg, "levels_rejected_no_probe")
        accepted = tuple(lvl for lvl in ladder if _probe_level(cfg, lvl, call))
    except BackendError as e:
        return _fail(cache, cfg, e.reason_code)

    if not accepted:
        return _fail(cache, cfg, "no_levels")

    caps = Capabilities(cfg.api_url, cfg.model, accepted, "ok", time.time())
    cache.put(caps)
    return caps
