"""Config loading and validation for jev-router-core."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml


class ConfigError(Exception):
    pass


@dataclass
class ClassifierConfig:
    base_url: str
    path: str = "/v1/systemone"
    model: str = ""
    api_key_env: str | None = None
    timeout_ms: int = 2000
    down: str = "default_model"  # default_model | block

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


@dataclass
class RouterConfig:
    classifier: ClassifierConfig
    questions: dict[str, dict[str, Any]]
    lanes: dict[str, list[dict[str, str]]]
    rules: list[dict[str, Any]]
    fallback: dict[str, Any]
    spawn: dict[str, Any]
    stickiness: dict[str, Any]
    log: dict[str, Any]
    state: dict[str, Any] = field(default_factory=lambda: {"last_messages": 4, "max_tokens": 1500})
    server: dict[str, Any] = field(default_factory=lambda: {"host": "127.0.0.1", "port": 8790, "protocol_version": 1})
    raw: dict[str, Any] = field(default_factory=dict)


_REQUIRED_TOP = ("classifier", "questions", "lanes", "rules", "fallback")

# Fields adapters always provide in the rule namespace (see docs/SPI.md).
_CONTEXT_FIELDS = frozenset({
    "has_images", "prompt_tokens", "session_turn", "harness", "kind", "session_id",
})

# Valid keys inside a rule's `then:` block.
_KNOWN_ACTIONS = frozenset({
    "lane", "lane_template", "pin", "veto_spawn", "force_spawn",
})


def load_config(path: str) -> RouterConfig:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    missing = [k for k in _REQUIRED_TOP if k not in data]
    if missing:
        raise ConfigError(f"{path}: missing required keys: {missing}")

    clf_raw = data["classifier"]
    if "base_url" not in clf_raw:
        raise ConfigError("classifier.base_url is required")
    clf = ClassifierConfig(
        base_url=clf_raw["base_url"].rstrip("/"),
        path=clf_raw.get("path", "/v1/systemone"),
        model=clf_raw.get("model", ""),
        api_key_env=clf_raw.get("api_key_env"),
        timeout_ms=int(clf_raw.get("timeout_ms", 2000)),
        down=clf_raw.get("down", "default_model"),
    )
    if clf.down not in ("default_model", "block"):
        raise ConfigError("classifier.down must be 'default_model' or 'block'")
    if clf.timeout_ms <= 0:
        raise ConfigError("classifier.timeout_ms must be > 0")
    if not clf.path.startswith("/"):
        raise ConfigError("classifier.path must start with '/'")

    for name, q in data["questions"].items():
        if q.get("type") not in ("choice", "score", "noul"):
            raise ConfigError(f"questions.{name}.type must be choice|score|noul")
        if not q.get("instructions"):
            raise ConfigError(f"questions.{name}.instructions is required")

    for rule in data["rules"]:
        if "when" not in rule or "then" not in rule:
            raise ConfigError(f"rule {rule.get('name', '?')}: needs when/then")
        w = rule["when"]
        if not all(k in w for k in ("field", "op", "value")):
            raise ConfigError(f"rule {rule.get('name', '?')}: when needs field/op/value")
        if w["op"] not in ("==", "!=", ">", ">=", "<", "<=", "in"):
            raise ConfigError(f"rule {rule.get('name', '?')}: bad op {w['op']}")

        # W10: validate field references and action keys at load time so a
        # typo (veto_spwn) or a field no question produces (pii_prob without a
        # pii question) fails fast instead of silently never matching.
        known_fields = set(_CONTEXT_FIELDS)
        for qname, q in data["questions"].items():
            known_fields |= {qname, f"{qname}_confidence", f"{qname}_prob"}
        if w["field"] not in known_fields:
            raise ConfigError(
                f"rule {rule.get('name', '?')}: unknown field '{w['field']}'. "
                f"Known: {sorted(known_fields)}")

        t = rule["then"]
        actions = set(t)
        unknown_actions = actions - _KNOWN_ACTIONS
        if unknown_actions:
            raise ConfigError(
                f"rule {rule.get('name', '?')}: unknown action(s) {sorted(unknown_actions)}. "
                f"Known: {sorted(_KNOWN_ACTIONS)}")
        if "pin" in t:
            if not isinstance(t["pin"], dict) or "model" not in t["pin"]:
                raise ConfigError(f"rule {rule.get('name', '?')}: pin requires a 'model' key")
        if "lane" in t and t["lane"] not in data["lanes"]:
            raise ConfigError(f"rule {rule.get('name', '?')}: unknown lane '{t['lane']}'")
        if "lane_template" in t:
            import string as _string
            try:
                parsed = [f for (_, f, _, _) in _string.Formatter().parse(t["lane_template"]) if f]
            except Exception as e:
                raise ConfigError(f"rule {rule.get('name', '?')}: malformed lane_template: {e}")
            for fname in parsed:
                base = fname.split(".")[0].split("[")[0]
                if base not in known_fields:
                    raise ConfigError(
                        f"rule {rule.get('name', '?')}: lane_template references unknown field '{base}'")

    return RouterConfig(
        classifier=clf,
        questions=data["questions"],
        lanes=data["lanes"],
        rules=data["rules"],
        fallback=data["fallback"],
        spawn=data.get("spawn", {"enabled": False}),
        stickiness=data.get("stickiness", {"enabled": False}),
        log=data.get("log", {"path": "~/.jevrouter/decisions.jsonl", "content": "full"}),
        state=data.get("state", {"last_messages": 4, "max_tokens": 1500}),
        server=data.get("server", {"host": "127.0.0.1", "port": 8790, "protocol_version": 1}),
        raw=data,
    )
