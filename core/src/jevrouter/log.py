"""JSONL decision log. content: full | hash | none."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any


class DecisionLog:
    def __init__(self, path: str, content: str = "full"):
        self.path = Path(os.path.expanduser(path))
        self.content = content
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Nits: 'content: full' stores prompt text -> keep the file owner-only.
        # Create with 0600 atomically (pass2 issue 4: chmod-before-exist was a no-op).
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            os.close(fd)
        except OSError:
            pass

    def _redact(self, state: str) -> str:
        if self.content == "full":
            return state
        if self.content == "hash":
            return "sha256:" + hashlib.sha256(state.encode("utf-8")).hexdigest()[:16]
        return "<omitted>"

    def write(self, *, harness: str, session: str, state: str, answers: dict[str, Any] | None,
             decision: dict[str, Any], classifier_latency_ms: int | None = None,
             error: str | None = None) -> None:
        entry = {
            "ts": time.time(),
            "harness": harness,
            "session": session,
            "state": self._redact(state),
            "answers": answers,
            "decision": decision,
            "classifier_latency_ms": classifier_latency_ms,
            "error": error,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
