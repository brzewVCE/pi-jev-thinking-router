"""Offline tests for the rule engine (no classifier needed)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from jevrouter.config import load_config
from jevrouter.engine import (
    apply_confidence_floor,
    apply_spawn_policy,
    build_fields,
    decide,
    resolve_lane,
)

CFG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "examples", "config.example.yaml")


def answers(task="coding", diff="moderate", conf=0.9, spawn_true=0.1):
    return {
        "task_kind": {"value": task, "confidence": conf, "probabilities": {task: conf}},
        "difficulty": {"value": diff, "confidence": conf, "probabilities": {diff: conf}},
        "needs_spawn": {"value": spawn_true >= 0.5, "confidence": max(spawn_true, 1 - spawn_true),
                        "probabilities": {"true": spawn_true, "false": 1 - spawn_true}},
    }


class TestEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_config(CFG_PATH)

    def test_config_loads(self):
        self.assertIn("task_kind", self.cfg.questions)
        self.assertEqual(len(self.cfg.rules), 4)

    def test_default_mapping_coding_moderate(self):
        d = decide(self.cfg, answers(), {"available_models": ["qwen3-32b-local", "qwen3-4b-local"]})
        self.assertEqual(d["model"], "qwen3-32b-local")
        self.assertEqual(d["thinking"], "medium")
        self.assertEqual(d["rule"], "default-mapping")

    def test_lane_skips_unavailable_models(self):
        d = decide(self.cfg, answers(diff="easy"), {"available_models": ["qwen3-32b-local"]})
        # coding_easy lane: qwen3-4b unavailable -> next entry wins
        self.assertEqual(d["model"], "qwen3-32b-local")

    def test_vision_rule(self):
        d = decide(self.cfg, answers(task="general"), {"has_images": True,
                 "available_models": ["qwen-vl-7b-local", "qwen3-4b-local"]})
        self.assertEqual(d["model"], "qwen-vl-7b-local")
        self.assertEqual(d["rule"], "images-need-vision")

    def test_pii_example_rule_pins(self):
        a = answers()
        a["pii"] = {"value": True, "confidence": 0.9, "probabilities": {"true": 0.9, "false": 0.1}}
        d = decide(self.cfg, a, {"available_models": ["qwen3-32b-local"]})
        self.assertEqual(d["rule"], "keep-secrets-local")
        self.assertEqual(d["source"], "pin")

    def test_veto_spawn_trivial(self):
        d = decide(self.cfg, answers(diff="trivial"), {"available_models": ["qwen3-4b-local"]})
        self.assertTrue(d["spawn"]["vetoed"])

    def test_spawn_policy_allows_above_threshold(self):
        d = {"spawn": {"allowed": False, "forced": False, "vetoed": False}}
        d = apply_spawn_policy(self.cfg, d, answers(spawn_true=0.8))
        self.assertTrue(d["spawn"]["allowed"])
        self.assertAlmostEqual(d["spawn"]["needs_spawn_prob"], 0.8)

    def test_spawn_policy_blocks_below_threshold(self):
        d = {"spawn": {"allowed": False, "forced": False, "vetoed": False}}
        d = apply_spawn_policy(self.cfg, d, answers(spawn_true=0.3))
        self.assertFalse(d["spawn"]["allowed"])

    def test_confidence_floor_forces_fallback(self):
        d = decide(self.cfg, answers(conf=0.2), {"available_models": ["qwen3-32b-local"]})
        d = apply_confidence_floor(self.cfg, d, answers(conf=0.2))
        self.assertEqual(d["source"], "confidence_floor")
        self.assertEqual(d["model"], self.cfg.fallback["default"]["model"])

    def test_pin_survives_confidence_floor(self):
        a = answers(conf=0.2)
        a["pii"] = {"value": True, "confidence": 0.9, "probabilities": {"true": 0.9, "false": 0.1}}
        d = decide(self.cfg, a, {"available_models": ["qwen3-32b-local"]})
        d = apply_confidence_floor(self.cfg, d, a)
        self.assertEqual(d["source"], "pin")  # pins are user intent, floor must not override

    def test_unresolvable_lane_falls_through(self):
        d = decide(self.cfg, answers(task="coding", diff="hard"), {"available_models": ["nonexistent"]})
        # coding_hard has no available model; default-mapping continues to fallback
        self.assertIn(d["source"], ("fallback", "confidence_floor"))

    def test_build_fields_noul_prob(self):
        f = build_fields(answers(spawn_true=0.77), {})
        self.assertAlmostEqual(f["needs_spawn_prob"], 0.77)


if __name__ == "__main__":
    unittest.main()
