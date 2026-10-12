"""CLI: jevrouter classify|decide|serve|validate."""
from __future__ import annotations

import argparse
import json
import sys

from .classifier import ClassifierError, ClassifierUnavailable, classify
from .config import load_config, ConfigError
from .engine import apply_confidence_floor, apply_spawn_policy, decide


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="jevrouter")
    p.add_argument("--config", required=True, help="path to router config YAML")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("classify", help="classify a text (raw answers)")
    c.add_argument("text")

    d = sub.add_parser("decide", help="full decision for a text")
    d.add_argument("text")
    d.add_argument("--harness", default="cli")
    d.add_argument("--available-models", default="", help="comma-separated model ids")
    d.add_argument("--has-images", action="store_true")

    s = sub.add_parser("serve", help="run the FastAPI daemon")
    s.add_argument("--host", default=None)
    s.add_argument("--port", type=int, default=None)

    sub.add_parser("validate", help="validate the config file")

    args = p.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if args.cmd == "validate":
        print(f"OK: {args.config} ({len(cfg.questions)} questions, {len(cfg.lanes)} lanes, {len(cfg.rules)} rules)")
        return 0

    if args.cmd == "serve":
        import uvicorn
        from .app import create_app
        uvicorn.run(create_app(cfg),
                    host=args.host or cfg.server.get("host", "127.0.0.1"),
                    port=args.port or int(cfg.server.get("port", 8790)))
        return 0

    try:
        res = classify(cfg.classifier, args.text, cfg.questions)
    except ClassifierUnavailable as e:
        print(f"classifier unavailable: {e}", file=sys.stderr)
        return 3
    except ClassifierError as e:
        print(f"classifier error: {e}", file=sys.stderr)
        return 4
    if args.cmd == "classify":
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    context = {
        "harness": args.harness,
        "has_images": args.has_images,
        "prompt_tokens": len(args.text) // 4,
        "available_models": [m for m in args.available_models.split(",") if m],
    }
    decision = decide(cfg, res["answers"], context)
    decision = apply_confidence_floor(cfg, decision, res["answers"])
    decision = apply_spawn_policy(cfg, decision, res["answers"])
    print(json.dumps({"answers": res["answers"], "decision": decision}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
