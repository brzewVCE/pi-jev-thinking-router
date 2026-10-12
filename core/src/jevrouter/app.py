"""FastAPI daemon: /classify /decide /health.

Decision-only protocol. Adapters POST state + context, get a decision back.
Every response carries X-Router-Protocol for adapter version checks.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .classifier import ClassifierUnavailable, ClassifierError, classify
from .config import RouterConfig
from .engine import apply_confidence_floor, apply_spawn_policy, decide
from .log import DecisionLog

PROTOCOL_VERSION_HEADER = "X-Router-Protocol"


def create_app(cfg: RouterConfig) -> FastAPI:
    app = FastAPI(title="jev-router-core", version="0.1.0")
    log = DecisionLog(cfg.log.get("path", "~/.jevrouter/decisions.jsonl"),
                     cfg.log.get("content", "full"))

    def with_proto(payload: dict[str, Any], status: int = 200) -> JSONResponse:
        return JSONResponse(payload, status_code=status,
                           headers={PROTOCOL_VERSION_HEADER: str(cfg.server.get("protocol_version", 1))})

    @app.get("/health")
    def health() -> JSONResponse:
        # Includes the state-window config so adapters can honor
        # state.last_messages / state.max_tokens without their own copy
        # of the config file (W12).
        return with_proto({"ok": True, "classifier": cfg.classifier.base_url,
                         "state": cfg.state,
                         "protocol_version": cfg.server.get("protocol_version", 1)})

    # C4: classifier I/O is blocking, so it runs via run_in_threadpool and
    # never stalls the event loop.
    @app.post("/classify")
    async def classify_endpoint(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except Exception as e:  # W9
            return with_proto({"ok": False, "error": "bad_request", "detail": str(e)}, 400)
        from starlette.concurrency import run_in_threadpool
        from .classifier import classify as _clf
        try:
            res = await run_in_threadpool(_clf, cfg.classifier, body.get("state", ""), cfg.questions)
        except ClassifierUnavailable as e:
            return with_proto({"ok": False, "error": "classifier_unavailable", "detail": str(e)}, 503)
        except ClassifierError as e:
            return with_proto({"ok": False, "error": "classifier_error", "detail": str(e)}, 502)
        return with_proto({"ok": True, **res})

    @app.post("/decide")
    async def decide_route(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except Exception as e:  # W9
            return with_proto({"ok": False, "error": "bad_request", "detail": str(e)}, 400)
        state: str = body.get("state", "")
        context: dict[str, Any] = body.get("context", {}) or {}
        harness: str = body.get("harness", "unknown")
        session: str = body.get("session", "")

        from starlette.concurrency import run_in_threadpool
        from .classifier import classify as _clf
        try:
            res = await run_in_threadpool(_clf, cfg.classifier, state, cfg.questions)
            answers = res["answers"]
            decision = decide(cfg, answers, context)
            decision = apply_confidence_floor(cfg, decision, answers)
            decision = apply_spawn_policy(cfg, decision, answers)
            _safe_log(log, harness=harness, session=session, state=state, answers=answers,
                     decision=decision, classifier_latency_ms=res["latency_ms"])
            return with_proto({"ok": True, "decision": decision,
                             "answers": answers,
                             "classifier_latency_ms": res["latency_ms"]})
        except ClassifierUnavailable as e:
            if cfg.classifier.down == "block":
                decision = {"model": None, "thinking": None, "source": "blocked",
                            "spawn": {"allowed": False, "forced": False, "vetoed": False}}
                status = 503
            else:
                fb = cfg.fallback.get("default", {})
                decision = {"model": fb.get("model"), "thinking": fb.get("thinking", "off"),
                            "source": "classifier_down_fallback",
                            "spawn": {"allowed": False, "forced": False, "vetoed": False}}
                status = 200
            _safe_log(log, harness=harness, session=session, state=state, answers=None,
                     decision=decision, error=str(e))
            return with_proto({"ok": status == 200, "decision": decision,
                              "error": "classifier_unavailable", "detail": str(e)}, status)
        except ClassifierError as e:
            _safe_log(log, harness=harness, session=session, state=state, answers=None,
                      decision={"source": "classifier_error"}, error=str(e))
            return with_proto({"ok": False, "error": "classifier_error", "detail": str(e)}, 502)

    return app


def _safe_log(log: DecisionLog, **kwargs) -> None:
    """W8: a logging failure (disk full, perms) must never destroy a computed decision."""
    try:
        log.write(**kwargs)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("jev-router: decision log write failed: %s", e)
