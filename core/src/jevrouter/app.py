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
        return with_proto({"ok": True, "classifier": cfg.classifier.base_url})

    @app.post("/classify")
    async def classify_only(req: Request) -> JSONResponse:
        body = await req.json()
        state = body.get("state", "")
        try:
            res = classify(cfg.classifier, state, cfg.questions)
        except ClassifierUnavailable as e:
            return with_proto({"ok": False, "error": "classifier_unavailable", "detail": str(e)}, 503)
        return with_proto({"ok": True, **res})

    @app.post("/decide")
    async def decide_route(req: Request) -> JSONResponse:
        body = await req.json()
        state: str = body.get("state", "")
        context: dict[str, Any] = body.get("context", {}) or {}
        harness: str = body.get("harness", "unknown")
        session: str = body.get("session", "")

        try:
            res = classify(cfg.classifier, state, cfg.questions)
            answers = res["answers"]
            decision = decide(cfg, answers, context)
            decision = apply_confidence_floor(cfg, decision, answers)
            decision = apply_spawn_policy(cfg, decision, answers)
            log.write(harness=harness, session=session, state=state, answers=answers,
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
            log.write(harness=harness, session=session, state=state, answers=None,
                     decision=decision, error=str(e))
            return with_proto({"ok": status == 200, "decision": decision,
                              "error": "classifier_unavailable", "detail": str(e)}, status)
        except ClassifierError as e:
            log.write(harness=harness, session=session, state=state, answers=None,
                     decision={"source": "classifier_error"}, error=str(e))
            return with_proto({"ok": False, "error": "classifier_error", "detail": str(e)}, 502)

    return app
