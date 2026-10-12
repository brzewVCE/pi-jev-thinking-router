"""Regression tests for OMP review findings (C1-C4, W1-W15)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from jevrouter.classifier import (
    ClassifierError,
    ClassifierUnavailable,
    build_questions_payload,
    classify,
    normalize_response,
)
from jevrouter.config import ConfigError, load_config
from jevrouter.engine import _cmp, apply_spawn_policy, build_fields, decide
from jevrouter.log import DecisionLog

CFG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "examples", "config.example.yaml")


def answers(task="coding", diff="moderate", conf=0.9, spawn_true=0.1):
    return {
        "task_kind": {"value": task, "confidence": conf, "probabilities": {task: conf}},
        "difficulty": {"value": diff, "confidence": conf, "probabilities": {diff: conf}},
        "needs_spawn": {"value": spawn_true >= 0.5, "confidence": max(spawn_true, 1 - spawn_true),
                        "probabilities": {"true": spawn_true, "false": 1 - spawn_true}},
    }


class TestNormalize(unittest.TestCase):
    def test_falsy_bool_value_survives(self):
        # W1: {"value": false} must stay False, not become argmax string "false"
        out = normalize_response({"answers": {"x": {"value": False,
                "probabilities": {"true": 0.2, "false": 0.8}}}})
        self.assertIs(out["x"]["value"], False)

    def test_falsy_zero_value_survives(self):
        out = normalize_response({"answers": {"s": {"value": 0, "confidence": 0.9}}})
        self.assertEqual(out["s"]["value"], 0)

    def test_missing_value_uses_argmax(self):
        out = normalize_response({"answers": {"c": {"probabilities": {"a": 0.3, "b": 0.7}}}})
        self.assertEqual(out["c"]["value"], "b")

    def test_results_key_alias(self):
        out = normalize_response({"results": {"c": {"value": "x"}}})
        self.assertEqual(out["c"]["value"], "x")

    def test_non_dict_answer_skipped(self):
        out = normalize_response({"answers": {"a": "junk", "b": {"value": "ok"}}})
        self.assertNotIn("a", out)
        self.assertIn("b", out)

    def test_non_dict_response_raises(self):
        # W2
        with self.assertRaises(ValueError):
            normalize_response(["not", "a", "dict"])


class TestClassifyErrors(unittest.TestCase):
    def _cfg(self):
        from jevrouter.config import ClassifierConfig
        return ClassifierConfig(base_url="http://x", model="m", timeout_ms=50)

    def test_connection_error_is_unavailable(self):
        with self.assertRaises(ClassifierUnavailable):
            classify(self._cfg(), "s", {"q": {"type": "noul", "instructions": "i"}})

    def test_malformed_json_raises_classifier_error(self):
        from unittest import mock
        cfg = self._cfg()
        class FakeResp:
            def read(self): return b"not json {{{"
            def __enter__(self): return self
            def __exit__(self, *a): return False
        with mock.patch("urllib.request.urlopen", return_value=FakeResp()):
            with self.assertRaises(ClassifierError):
                classify(cfg, "s", {"q": {"type": "noul", "instructions": "i"}})


class TestNoulWire(unittest.TestCase):
    def test_true_means_passed_through(self):
        # W11
        payload = build_questions_payload({
            "pii": {"type": "noul", "instructions": "secret?",
                     "true_means": "yes secret", "false_means": "no secret"}})
        self.assertEqual(payload["pii"]["true_means"], "yes secret")
        self.assertEqual(payload["pii"]["false_means"], "no secret")


class TestCmp(unittest.TestCase):
    def test_neq_missing_field_is_false(self):
        # W6
        self.assertFalse(_cmp("!=", None, "unclear"))

    def test_neq_present_different_is_true(self):
        self.assertTrue(_cmp("!=", "coding", "unclear"))

    def test_gt_missing_is_false(self):
        self.assertFalse(_cmp(">=", None, 0.5))


class TestAvailableModels(unittest.TestCase):
    def test_empty_list_means_nothing_available(self):
        # W4: [] must NOT behave like None (no filter)
        cfg = load_config(CFG_PATH)
        d = decide(cfg, answers(), {"available_models": []})
        self.assertEqual(d["source"], "fallback")

    def test_none_means_no_filter(self):
        cfg = load_config(CFG_PATH)
        d = decide(cfg, answers(), {})  # available_models absent
        self.assertEqual(d["source"], "lane")


class TestSpawnQuestionConfigurable(unittest.TestCase):
    def test_custom_question_name(self):
        # W5
        cfg = load_config(CFG_PATH)
        cfg.spawn = dict(cfg.spawn)
        cfg.spawn["question"] = "potrzebuje_subagenta"
        a = {"potrzebuje_subagenta": {"value": True, "confidence": 0.9,
              "probabilities": {"true": 0.9, "false": 0.1}}}
        d = apply_spawn_policy(cfg, {"spawn": {"allowed": False, "forced": False, "vetoed": False}}, a)
        self.assertTrue(d["spawn"]["allowed"])

    def test_missing_named_question_is_zero_not_crash(self):
        cfg = load_config(CFG_PATH)
        cfg.spawn = dict(cfg.spawn)
        cfg.spawn["question"] = "nie_istnieje"
        d = apply_spawn_policy(cfg, {"spawn": {"allowed": False, "forced": False, "vetoed": False}}, {})
        self.assertEqual(d["spawn"]["needs_spawn_prob"], 0.0)


class TestConfigValidation(unittest.TestCase):
    def _write(self, text):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def _base(self):
        return """
classifier: {base_url: "http://x", timeout_ms: 100}
questions:
  task_kind: {type: choice, instructions: "i", options: {a: "d"}}
lanes: {a_a: [{model: m1, thinking: off}]}
fallback: {default: {model: m1, thinking: low}}
rules:
  - name: r
    when: {field: task_kind, op: "==", value: "a"}
    then: {lane: a_a}
"""

    def test_valid_base_loads(self):
        load_config(self._write(self._base()))

    def test_unknown_field_rejected(self):
        # W10 / C3 class
        bad = self._base().replace("field: task_kind", "field: nieznane_prob")
        with self.assertRaises(ConfigError):
            load_config(self._write(bad))

    def test_unknown_action_rejected(self):
        bad = self._base().replace("lane: a_a", "veto_spwn: true")
        with self.assertRaises(ConfigError):
            load_config(self._write(bad))

    def test_pin_without_model_rejected(self):
        bad = self._base().replace("then: {lane: a_a}", "then: {pin: {thinking: high}}")
        with self.assertRaises(ConfigError):
            load_config(self._write(bad))

    def test_unknown_lane_rejected(self):
        bad = self._base().replace("lane: a_a", "lane: nie_ma")
        with self.assertRaises(ConfigError):
            load_config(self._write(bad))

    def test_bad_timeout_rejected(self):
        bad = self._base().replace("timeout_ms: 100", "timeout_ms: 0")
        with self.assertRaises(ConfigError):
            load_config(self._write(bad))

    def test_bad_path_rejected(self):
        bad = self._base().replace("timeout_ms: 100", 'timeout_ms: 100, path: "v1/x"')
        with self.assertRaises(ConfigError):
            load_config(self._write(bad))

    def test_malformed_lane_template_rejected(self):
        bad = self._base().replace("then: {lane: a_a}", 'then: {lane_template: "{nieznane_{x}"}')
        with self.assertRaises(ConfigError):
            load_config(self._write(bad))


class TestNoulProbNit(unittest.TestCase):
    def test_choice_option_named_true_does_not_hijack_prob(self):
        # nit: only bool-valued answers get the P(true) override
        f = build_fields({"c": {"value": "true", "confidence": 0.6,
                                 "probabilities": {"true": 0.6, "false": 0.4}}}, {})
        self.assertAlmostEqual(f["c_prob"], 0.6)  # probs.get("true") via value lookup, legit
        f2 = build_fields({"c": {"value": False, "confidence": 0.9,
                                  "probabilities": {"true": 0.1, "false": 0.9}}}, {})
        self.assertAlmostEqual(f2["c_prob"], 0.1)  # bool False -> P(true)=0.1


class TestLogRedaction(unittest.TestCase):
    def _log(self, content):
        import tempfile
        d = tempfile.mkdtemp()
        return DecisionLog(os.path.join(d, "l.jsonl"), content), os.path.join(d, "l.jsonl")

    def _write_and_read(self, log, path):
        log.write(harness="t", session="s", state="SECRETTEXT", answers=None, decision={})
        with open(path) as f:
            return f.read()

    def test_full_keeps_text(self):
        log, p = self._log("full")
        self.assertIn("SECRETTEXT", self._write_and_read(log, p))

    def test_hash_redacts(self):
        log, p = self._log("hash")
        out = self._write_and_read(log, p)
        self.assertNotIn("SECRETTEXT", out)
        self.assertIn("sha256:", out)

    def test_none_omits(self):
        log, p = self._log("none")
        out = self._write_and_read(log, p)
        self.assertNotIn("SECRETTEXT", out)
        self.assertIn("omitted", out)


if __name__ == "__main__":
    unittest.main()
