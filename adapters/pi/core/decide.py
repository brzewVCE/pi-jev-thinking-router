"""/decide logic: normalization -> backend -> cap -> clamp -> hysteresis -> cache.

This is the only decision-making code in the system (CONTRACT.md: "Jev decyduje").
The core contributes no content heuristics — just transport, normalization,
clamping, per-turn caching, downgrade hysteresis and receipts.

Pipeline (order matters and is contractual):

  1. normalize + validate the request (host names -> canon ladder)
  2. backend not configured / warmup failed  -> keep (no_backend)
  3. turn cache hit (same session+turn)      -> cached
  4. effective palette = host supported ∩ warmup accepted
     (warmup still pending -> conservative: trust the host list, CONTRACT §warmup.4)
  5. call the backend inside the remaining budget
  6. backend failure                         -> keep (jev_error / jev_timeout / jev_unparsable)
  7. Jev level above cap                     -> capped -> cap   (user ceiling wins)
  8. Jev level off the palette               -> clamp to nearest WEAKER (never up)
  9. downgrade of >1 rank below last applied -> hysteresis_hold until a second
     agreeing Jev decision arrives
 10. changed = final != current_effort; persist session state + turn cache

Fail-open invariant (PLAYBOOK): no path below may ever return a level stronger than
`current_effort` unless Jev explicitly selected it and it is within cap. Errors
always return `current_effort` with changed=false.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .capabilities import Capabilities, CapabilityCache, cache_key
from .config import JevConfig
from .policy import POLICY_VERSION, normalize_levels, rank, selector_messages, to_canon
from .transport import BackendError, call_backend

REASON_CODES = frozenset({
    "jev_selected", "jev_error", "jev_timeout", "jev_unparsable",
    "cached", "hysteresis_hold", "capped", "no_backend",
})

SOURCES = frozenset({"jev", "cache", "hysteresis", "fallback"})

MAX_TURNS_CACHED = 2048
MAX_SESSIONS_CACHED = 4096


class RequestError(Exception):
    """Client sent something unusable -> HTTP 400."""


@dataclass
class NormalizedRequest:
    host: str
    session_id: str
    turn_id: str
    task: str
    recent_turns: str
    has_images: bool
    current_effort: str          # host's own spelling
    current_canon: str
    cap: str                     # host's own spelling
    cap_canon: str
    supported_levels: list[str]  # host's own spelling
    supported_canon: list[str]
    budget_ms: int
    # canon rank -> host's level name (round-trip back to the host's alphabet)
    host_by_rank: dict[int, str] = field(default_factory=dict)


def normalize(req: Any) -> NormalizedRequest:
    if not isinstance(req, dict):
        raise RequestError("request body must be a JSON object")

    host = req.get("host")
    if not isinstance(host, str) or not host.strip():
        raise RequestError("'host' is required and must be a non-empty string")
    session_id = req.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise RequestError("'session_id' is required and must be a non-empty string")

    cur_raw = req.get("current_effort")
    cur_canon = to_canon(cur_raw)
    if cur_canon is None:
        raise RequestError(f"unknown current_effort {cur_raw!r}")

    sup_canon = normalize_levels(req.get("supported_levels"))
    if not sup_canon:
        raise RequestError("'supported_levels' must be a non-empty list of known levels")

    cap_raw = req.get("cap")
    if cap_raw is None or (isinstance(cap_raw, str) and not cap_raw.strip()):
        cap_canon, cap_host = cur_canon, cur_raw
    else:
        cap_canon = to_canon(cap_raw)
        if cap_canon is None:
            raise RequestError(f"unknown cap {cap_raw!r}")
        cap_host = cap_raw

    # The cap must itself be reachable on this host; otherwise clamp it down to the
    # strongest level the host actually supports (a cap we cannot honour).
    if cap_canon not in sup_canon:
        weaker = [l for l in sup_canon if rank(l) <= rank(cap_canon)]
        cap_canon = max(weaker, key=rank) if weaker else min(sup_canon, key=rank)
        cap_host = cap_canon

    host_by_rank: dict[int, str] = {}
    for lvl in req.get("supported_levels") or []:
        c = to_canon(lvl)
        if c is not None and rank(c) not in host_by_rank:
            host_by_rank[rank(c)] = lvl
    # current_effort may be spelled differently from the palette entry it maps to;
    # prefer the host's own spelling for that rank so keep-decisions are verbatim.
    host_by_rank.setdefault(rank(cur_canon), cur_raw)

    budget = req.get("budget_ms")
    if isinstance(budget, bool) or not isinstance(budget, (int, float)):
        budget_ms = 0
    else:
        budget_ms = int(budget)
    if budget_ms <= 0:
        budget_ms = 0  # 0 = "host gave no budget"; core uses its own timeout

    return NormalizedRequest(
        host=host.strip(),
        session_id=session_id.strip(),
        turn_id=(req.get("turn_id") or "").strip() if isinstance(req.get("turn_id"), (str, int)) else "",
        task=str(req.get("task") or ""),
        recent_turns=str(req.get("recent_turns") or ""),
        has_images=bool(req.get("has_images")),
        current_effort=str(cur_raw),
        current_canon=cur_canon,
        cap=str(cap_host),
        cap_canon=cap_canon,
        supported_levels=list(req.get("supported_levels") or []),
        supported_canon=sup_canon,
        budget_ms=budget_ms,
        host_by_rank=host_by_rank,
    )


def clamp_weaker(decision_canon: str, palette_canon: list[str]) -> str:
    """Nearest WEAKER level on the palette; never stronger than the decision.

    Hermes `clamp_effort` semantics (reasoning_effort.py:118). If the palette has
    nothing weaker than the decision, the weakest available level is returned —
    still never a level the host did not offer, and never an escalation above what
    Jev asked for.
    """
    if decision_canon in palette_canon:
        return decision_canon
    weaker = [l for l in palette_canon if rank(l) < rank(decision_canon)]
    if weaker:
        return max(weaker, key=rank)
    return min(palette_canon, key=rank)


def _decision(effort: str, changed: bool, source: str, reason_code: str,
              confidence: float | None = None, elapsed_ms: int = 0) -> dict[str, Any]:
    if reason_code not in REASON_CODES:
        raise ValueError(f"reason_code {reason_code!r} not in closed set")
    if source not in SOURCES:
        raise ValueError(f"source {source!r} not in closed set")
    out: dict[str, Any] = {
        "effort": effort,
        "changed": changed,
        "source": source,
        "reason_code": reason_code,
        "policy_version": POLICY_VERSION,
        "elapsed_ms": elapsed_ms,
    }
    if confidence is not None:
        out["confidence"] = confidence
    return out


def _keep(n: NormalizedRequest, reason_code: str, elapsed_ms: int = 0) -> dict[str, Any]:
    """Fail-open: keep the host's current effort, verbatim spelling."""
    return _decision(n.current_effort, False, "fallback", reason_code, None, elapsed_ms)


@dataclass
class _Session:
    last_applied: str            # canon currently in effect on the host
    pending_downgrade: str | None = None
    updated_at: float = field(default_factory=time.time)


class Decider:
    """Holds per-session hysteresis state and the per-turn decision cache.

    All mutable state is guarded by one lock; the (slow) backend call happens
    outside it so parallel hosts never serialize on I/O.
    """

    def __init__(self, cfg: JevConfig, cache: CapabilityCache,
                 call: Callable[..., dict[str, Any]] = call_backend):
        self.cfg = cfg
        self.cache = cache
        self._call = call
        self._lock = threading.Lock()
        self._turns: "dict[tuple[str, str], dict[str, Any]]" = {}
        self._turn_order: list[tuple[str, str]] = []
        self._sessions: dict[str, _Session] = {}

    # -- state helpers (call with lock held) --------------------------------
    def _session_for(self, session_id: str, current_canon: str) -> _Session:
        s = self._sessions.get(session_id)
        if s is None:
            # Seed from the host's current effort: the router has never applied
            # anything here, so "last applied" == what the host reports. This is
            # what makes a first-turn drop of >1 level require two agreements.
            s = _Session(last_applied=current_canon)
            self._sessions[session_id] = s
            if len(self._sessions) > MAX_SESSIONS_CACHED:
                oldest = min(self._sessions.items(), key=lambda kv: kv[1].updated_at)[0]
                self._sessions.pop(oldest, None)
        elif s.last_applied != current_canon:
            # The host reports a different effort than the last one we applied:
            # the user moved it out-of-band (/reasoning). Reality wins — resync,
            # otherwise hysteresis would compare against a stale baseline and
            # hold a downgrade that is already in effect.
            s.last_applied = current_canon
            s.pending_downgrade = None
        return s

    def _cache_turn(self, key: tuple[str, str], decision: dict[str, Any]) -> None:
        if not key[1]:
            return  # no turn_id -> nothing to cache per-turn
        if key not in self._turns and len(self._turns) >= MAX_TURNS_CACHED:
            evict = self._turn_order.pop(0)
            self._turns.pop(evict, None)
        self._turns[key] = decision
        self._turn_order.append(key)

    def warmup_status(self) -> str:
        caps = self.cache.get(cache_key(self.cfg.api_url, self.cfg.model))
        if caps is None:
            return "pending"
        return caps.status

    def effective_palette(self, n: NormalizedRequest,
                         caps: Capabilities | None) -> tuple[list[str], str]:
        """(palette canon, warmup mode) where mode ∈ pending|ok|failed."""
        if caps is None:
            return list(n.supported_canon), "pending"
        if not caps.ok:
            return list(n.supported_canon), "failed"
        accepted = set(caps.accepted_levels)
        palette = [l for l in n.supported_canon if l in accepted]
        if not palette:
            # Host and warmup disagree completely: fall back to the host's word
            # (CONTRACT §warmup.4 — never block the host).
            return list(n.supported_canon), "ok"
        return palette, "ok"

    # -- main ---------------------------------------------------------------
    def decide(self, req: Any) -> dict[str, Any]:
        n = normalize(req)
        t0 = time.monotonic()

        def elapsed() -> int:
            return int((time.monotonic() - t0) * 1000)

        if not self.cfg.backend_configured:
            return _keep(n, "no_backend", elapsed())

        caps = self.cache.get(cache_key(self.cfg.api_url, self.cfg.model))
        if caps is not None and not caps.ok:
            return _keep(n, "no_backend", elapsed())

        turn_key = (n.session_id, n.turn_id)
        if n.turn_id:
            with self._lock:
                hit = self._turns.get(turn_key)
            if hit is not None:
                cached = dict(hit)
                cached["source"] = "cache"
                cached["reason_code"] = "cached"
                cached["elapsed_ms"] = elapsed()
                return cached

        palette, _mode = self.effective_palette(n, caps)

        # Deadline: whichever is tighter, the host's budget or our own timeout.
        limit_ms = self.cfg.timeout_ms
        if n.budget_ms > 0:
            limit_ms = min(limit_ms, n.budget_ms)
        remaining = (limit_ms - elapsed()) / 1000.0

        msgs = selector_messages(
            task=n.task, current_effort=n.current_canon, cap=n.cap_canon,
            supported_levels=palette, recent_turns=n.recent_turns,
            has_images=n.has_images)

        try:
            res = self._call(self.cfg, msgs, palette, max(remaining, 0.0))
        except BackendError as e:
            return _keep(n, e.reason_code, elapsed())

        jev_canon = res["level"]
        conf = res.get("confidence")

        # 7. cap (user ceiling) — a cap-driven move is user intent, not a Jev
        #    downgrade, so hysteresis does not apply to it.
        capped = rank(jev_canon) > rank(n.cap_canon)
        level_canon = n.cap_canon if capped else jev_canon

        # 8. clamp to what the host can actually run, nearest weaker.
        level_canon = clamp_weaker(level_canon, palette)

        # 9. hysteresis on Jev-driven downgrades only.
        source = "jev"
        reason = "capped" if capped else "jev_selected"
        with self._lock:
            sess = self._session_for(n.session_id, n.current_canon)
            drop = rank(sess.last_applied) - rank(level_canon)
            if (not capped) and drop > 1:
                if sess.pending_downgrade == level_canon:
                    sess.pending_downgrade = None  # second agreeing decision
                else:
                    sess.pending_downgrade = level_canon
                    sess.updated_at = time.time()
                    held = _decision(n.current_effort, False, "hysteresis",
                                    "hysteresis_hold", conf, elapsed())
                    self._cache_turn(turn_key, held)
                    return held
            sess.last_applied = level_canon
            sess.pending_downgrade = None
            sess.updated_at = time.time()

        host_level = n.host_by_rank.get(rank(level_canon), level_canon)
        changed = host_level != n.current_effort
        decision = _decision(host_level, changed, source, reason, conf, elapsed())
        with self._lock:
            self._cache_turn(turn_key, decision)
        return decision
