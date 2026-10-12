"""System One wire client (Kev / Laya / jeff / Jev cloud).

Request shape (per TypeSafe System One contract, as used by Kev-compatible
servers):

    POST {base_url}{path}
    {
      "model": "...",
      "state": "<text the classifier judges>",
      "questions": {
        "<name>": {"type": "choice", "instructions": "...",
                    "options": {"a": "desc a", "b": "desc b"}},
        "<name>": {"type": "score",  "instructions": "...",
                    "levels": ["trivial", "easy", "moderate", "hard"]},
        "<name>": {"type": "noul",   "instructions": "..."}
      }
    }

Response shape (normalized here):

    {"answers": {"<name>": {"value": ..., "confidence": float,
                             "probabilities": {opt: p}}},
     "latency_ms": int}

NOTE: exact field names vary slightly between Jev cloud and open clones
(SemIf's author noted "format going in and coming back is slightly
different"). `normalize_response` is deliberately defensive; the phase-1
spike against a real Kev server is the source of truth and this module is
where any mismatch gets fixed.
"""
from __future__ import annotations

import time
import urllib.request
import json
from typing import Any

from .config import ClassifierConfig


class ClassifierUnavailable(Exception):
    pass


class ClassifierError(Exception):
    pass


def build_questions_payload(questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for name, q in questions.items():
        entry: dict[str, Any] = {"type": q["type"], "instructions": q["instructions"]}
        if q["type"] == "choice":
            entry["options"] = q.get("options", {})
        elif q["type"] == "score":
            entry["levels"] = list(q.get("levels", {}).keys()) if isinstance(q.get("levels"), dict) else q.get("levels", [])
        elif q["type"] == "noul":
            # W11: pass noul semantics through; servers that ignore extra keys
            # are unaffected, servers that read them get the richer contract.
            if q.get("true_means"):
                entry["true_means"] = q["true_means"]
            if q.get("false_means"):
                entry["false_means"] = q["false_means"]
        payload[name] = entry
    return payload


def normalize_response(raw: dict[str, Any]) -> dict[str, Any]:
    """Map a raw System One response to our normalized answers dict."""
    if not isinstance(raw, dict):  # W2: non-dict body (e.g. JSON array)
        raise ValueError(f"response is not a dict: {type(raw).__name__}")
    answers: dict[str, Any] = {}
    src = raw.get("answers") or raw.get("results") or {}
    if not isinstance(src, dict):
        raise ValueError(f"answers/results is not a dict: {type(src).__name__}")
    for name, a in src.items():
        if not isinstance(a, dict):
            continue
        probs = a.get("probabilities") or a.get("probs") or {}
        # W1: explicit None-chaining so falsy values (False, 0) survive.
        value = a.get("value")
        if value is None:
            value = a.get("primary")
        if value is None:
            value = a.get("primaryValue")
        if value is None and probs:
            value = max(probs, key=lambda k: probs[k])
        conf = a.get("confidence")
        if conf is None and probs and value is not None:
            conf = probs.get(value)
        answers[name] = {
            "value": value,
            "confidence": float(conf) if conf is not None else 0.0,
            "probabilities": {k: float(v) for k, v in probs.items()},
        }
    return answers


def classify(cfg: ClassifierConfig, state: str, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    url = cfg.base_url + cfg.path
    body = json.dumps({
        "model": cfg.model,
        "state": state,
        "questions": build_questions_payload(questions),
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if cfg.api_key:
        req.add_header("Authorization", f"Bearer {cfg.api_key}")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout_ms / 1000.0) as resp:
            raw_bytes = resp.read()
    except Exception as e:  # network, timeout, HTTP error
        raise ClassifierUnavailable(str(e)) from e
    latency = int((time.monotonic() - t0) * 1000)
    try:  # W15: decode failures are server errors, not "down"
        raw = json.loads(raw_bytes.decode("utf-8"))
    except Exception as e:
        raise ClassifierError(f"undecodable response: {e}") from e
    try:
        answers = normalize_response(raw)
    except Exception as e:
        raise ClassifierError(f"unparseable response: {e}") from e
    if not answers:
        raise ClassifierError(f"no answers in response: {list(raw.keys())}")
    return {"answers": answers, "latency_ms": latency}
