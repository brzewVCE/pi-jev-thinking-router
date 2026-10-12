"""jev-router core (SPI v1) — stdlib-only decision daemon.

Modules:
  config        env-driven configuration (JEV_*)
  policy        canonical level ladder + selector prompt (versioned)
  transport     OpenAI-compat chat/completions over urllib
  capabilities  warmup + per-(url, model) cache with TTL, RAM + disk
  decide        normalization, cap, clamp-nearest-weaker, hysteresis, turn cache
  receipts      sanitized daily-rotated JSONL audit trail
  jev_core      http.server daemon wiring it all together

No third-party dependencies (the base repo's FastAPI venv is unusable here and
PLAYBOOK B1 asks for http.server + threads anyway).
"""
from .config import JevConfig, PROTOCOL_VERSION
from .policy import POLICY_VERSION

__all__ = ["JevConfig", "POLICY_VERSION", "PROTOCOL_VERSION"]
