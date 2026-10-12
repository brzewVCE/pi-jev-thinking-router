"""jeu-router core config (SPI v1) — everything from env, stdlib only.

Adapted from jev-router-core/core/src/jevrouter/config.py. The base read a YAML
rule-DSL (questions/lanes/rules); CONTRACT.md drops the rule DSL entirely
("Jev decides", no word lists in the core), so the config here is transport +
policy plumbing only.

Why stdlib and not the base's FastAPI: the base venv is unusable in this
container — `.venv/bin/python` symlinks to /usr/bin/python3 (3.11) while every
wheel in site-packages is built for cpython-313 (`pydantic_core._pydantic_core`
fails to import) and installing is forbidden. PLAYBOOK B1 asks for an
`http.server` daemon with threads anyway, so the core has zero third-party deps.

Env vars (CONTRACT.md § Backend transport):
  JEV_API_URL       OpenAI-compat base URL, e.g. http://127.0.0.1:1234/v1
  JEV_API_KEY       optional bearer token; NEVER logged / receipted / in /health
  JEV_MODEL         decision model name
  JEV_KIND          openai-compat (typesafe reserved for a later phase)
  JEV_TIMEOUT_MS    backend timeout
  JEV_CORE_PORT     daemon port (default 8790)

Extra knobs (all optional):
  JEV_CORE_HOST          bind host (default 127.0.0.1)
  JEV_HOME               state dir (default ~/.jev-router)
  JEV_CAP_TTL_S          capabilities cache TTL (default 3600)
  JEV_WARMUP_ON_START    "0" skips startup warmup (default "1")
  JEV_WARMUP_PROBE       "0" skips per-level server-side probing (default "1")
  JEV_JSON_MODE          "0" drops response_format=json_object (default "1")
  JEV_RECEIPT_CONTENT    full | excerpt | hash | none (default excerpt)
  JEV_EXCERPT_CHARS      receipt excerpt length (default 200)
  JEV_LAST_MESSAGES      /health state window (default 4)
  JEV_MAX_TOKENS         /health state window (default 1500)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from .policy import POLICY_VERSION  # single owner: policy.py (audit B1-1)

PROTOCOL_VERSION = 1
DEFAULT_CORE_PORT = 8790
DEFAULT_CORE_HOST = "127.0.0.1"
DEFAULT_HOME = "~/.jev-router"
DEFAULT_CAP_TTL_S = 3600
DEFAULT_TIMEOUT_MS = 3000

RECEIPT_CONTENT_MODES = ("full", "excerpt", "hash", "none")
SUPPORTED_KINDS = ("openai-compat", "typesafe")


class ConfigError(Exception):
    pass


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from e


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    s = str(raw).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean-ish value, got {raw!r}")


@dataclass
class JevConfig:
    api_url: str = ""
    api_key: str = ""
    model: str = ""
    kind: str = "openai-compat"
    timeout_ms: int = DEFAULT_TIMEOUT_MS

    core_host: str = DEFAULT_CORE_HOST
    core_port: int = DEFAULT_CORE_PORT

    home: str = DEFAULT_HOME
    cap_ttl_s: int = DEFAULT_CAP_TTL_S
    warmup_on_start: bool = True
    warmup_probe: bool = True
    json_mode: bool = True

    receipt_content: str = "excerpt"
    excerpt_chars: int = 200

    state: dict[str, int] = field(default_factory=lambda: {"last_messages": 4, "max_tokens": 1500})
    protocol_version: int = PROTOCOL_VERSION
    policy_version: str = POLICY_VERSION

    # -- derived -----------------------------------------------------------
    @property
    def credential_present(self) -> bool:
        """Only this bool ever reaches /health or receipts (CONTRACT: no secrets)."""
        return bool(self.api_key)

    @property
    def backend_configured(self) -> bool:
        return bool(self.api_url and self.model)

    @property
    def home_path(self) -> str:
        return os.path.expanduser(self.home)

    @property
    def chat_completions_url(self) -> str:
        return self.api_url.rstrip("/") + "/chat/completions"

    def public_dict(self) -> dict[str, Any]:
        """Secret-free view used by /health."""
        return {
            "backend": "configured" if self.backend_configured else "not_configured",
            "model": self.model or None,
            "kind": self.kind,
            "policy_version": self.policy_version,
            "credential_present": self.credential_present,
            "timeout_ms": self.timeout_ms,
            "cap_ttl_s": self.cap_ttl_s,
            "state": dict(self.state),
        }

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "JevConfig":
        e = os.environ if env is None else env
        cfg = cls(
            api_url=(e.get("JEV_API_URL") or "").strip().rstrip("/"),
            api_key=(e.get("JEV_API_KEY") or "").strip(),
            model=(e.get("JEV_MODEL") or "").strip(),
            kind=(e.get("JEV_KIND") or "openai-compat").strip(),
            timeout_ms=_env_int(e, "JEV_TIMEOUT_MS", DEFAULT_TIMEOUT_MS),
            core_host=(e.get("JEV_CORE_HOST") or DEFAULT_CORE_HOST).strip(),
            core_port=_env_int(e, "JEV_CORE_PORT", DEFAULT_CORE_PORT),
            home=(e.get("JEV_HOME") or DEFAULT_HOME).strip(),
            cap_ttl_s=_env_int(e, "JEV_CAP_TTL_S", DEFAULT_CAP_TTL_S),
            warmup_on_start=_env_bool(e, "JEV_WARMUP_ON_START", True),
            warmup_probe=_env_bool(e, "JEV_WARMUP_PROBE", True),
            json_mode=_env_bool(e, "JEV_JSON_MODE", True),
            receipt_content=(e.get("JEV_RECEIPT_CONTENT") or "excerpt").strip().lower(),
            excerpt_chars=_env_int(e, "JEV_EXCERPT_CHARS", 200),
            state={
                "last_messages": _env_int(e, "JEV_LAST_MESSAGES", 4),
                "max_tokens": _env_int(e, "JEV_MAX_TOKENS", 1500),
            },
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.kind not in SUPPORTED_KINDS:
            raise ConfigError(f"JEV_KIND must be one of {SUPPORTED_KINDS}, got {self.kind!r}")
        if self.timeout_ms <= 0:
            raise ConfigError("JEV_TIMEOUT_MS must be > 0")
        if not (0 < self.core_port < 65536):
            raise ConfigError("JEV_CORE_PORT must be a valid TCP port")
        if self.cap_ttl_s <= 0:
            raise ConfigError("JEV_CAP_TTL_S must be > 0")
        if self.receipt_content not in RECEIPT_CONTENT_MODES:
            raise ConfigError(
                f"JEV_RECEIPT_CONTENT must be one of {RECEIPT_CONTENT_MODES}, "
                f"got {self.receipt_content!r}")
        if self.excerpt_chars <= 0:
            raise ConfigError("JEV_EXCERPT_CHARS must be > 0")
        if self.api_url and not self.api_url.startswith(("http://", "https://")):
            raise ConfigError("JEV_API_URL must be http(s)")
