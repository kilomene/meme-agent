"""Tests for the narrative picker + heat tracker (narratives.py)."""

import json
import os
import stat
import unittest

from daemon.launch_scanner import narratives
from daemon.launch_scanner.narratives import (
    NARRATIVE_KEYWORDS,
    NarrativeHeat,
    classify_narrative,
    default_heat_path,
)

NOW = 1_000_000.0


def _hot_setup():
    """10 closed outcomes in the last 24h: dog 4/5 runners, cat 0/5."""
    h = NarrativeHeat()
    for _ in range(4):
        h.record_outcome(["dog"], 3.0, False, NOW)
    h.record_outcome(["dog"], 1.2, False, NOW)
    for _ in range(5):
        h.record_outcome(["cat"], 1.1, False, NOW)
    return h


class ClassifyTest(unittest.TestCase):
    def test_taxonomy_covers_required_tags(self):
        for tag in ("dog", "cat", "frog", "ai", "gaming", "political",
                    "celebrity", "sports", "food", "space", "degen",
                    "rwa", "meme_classic"):
            self.assertIn(tag, NARRATIVE_KEYWORDS)
            self.assertTrue(6 <= len(NARRATIVE_KEYWORDS[tag]) <= 15,
                            tag)

    def test_classify_dog_coin(self):
        self.assertEqual(classify_narrative("PUPPY", "Puppy Coin"), ["dog"])

    def test_classify_multi_tag(self):
        self.assertEqual(classify_narrative("AIDOGE", "AI Doge"),
                         ["ai", "dog", "meme_classic"])

    def test_classify_no_match_gives_other(self):
        self.assertEqual(
            classify_narrative("ZXQ", "Zxq Token", "a brand new project"),
            ["other"])

    def test_classify_none_input_safe(self):
        self.assertEqual(classify_narrative(None, None), ["other"])
        self.assertEqual(classify_narrative(None, None, None), ["other"])

    def test_classify_number_input_safe(self):
        self.assertEqual(classify_narrative(123, 456.7, 890), ["other"])

    def test_classify_uses_description(self):
        self.assertEqual(
            classify_narrative("XYZ", "Xyz",
                               "an artificial intelligence trading bot"),
            ["ai"])

    def test_classify_case_insensitive(self):
        self.assertEqual(classify_narrative("PiZzA", "pIzZa ToKeN"),
                         ["food"])


class HeatTest(unittest.TestCase):
    def test_heat_neutral_with_no_data(self):
        h = NarrativeHeat()
        self.assertEqual(h.heat(["dog"], NOW), 1.0)
        self.assertEqual(h.heat(["other"], NOW), 1.0)

    def test_heat_above_one_when_tag_outperforms(self):
        h = _hot_setup()
        got = h.heat(["dog"], NOW)
        self.assertEqual(got, 1.4)
        self.assertGreater(got, 1.0)

    def test_heat_below_one_when_tag_underperforms(self):
        h = _hot_setup()
        got = h.heat(["cat"], NOW)
        self.assertEqual(got, 0.6)
        self.assertLess(got, 1.0)

    def test_heat_averages_multiple_tags(self):
        h = _hot_setup()
        self.assertEqual(h.heat(["dog", "cat"], NOW), 1.0)

    def test_heat_min_closed_rule(self):
        h = NarrativeHeat()
        h.record_outcome(["degen"], 5.0, False, NOW)
        h.record_outcome(["degen"], 6.0, False, NOW)  # 2 closed < 3
        self.assertEqual(h.heat(["degen"], NOW), 1.0)

    def test_rug_excluded_from_runners(self):
        h = NarrativeHeat()
        h.record_outcome(["frog"], 5.0, True, NOW)   # rug: not a runner
        h.record_outcome(["frog"], 1.1, False, NOW)
        h.record_outcome(["frog"], 1.2, False, NOW)
        h.record_outcome(["space"], 3.0, False, NOW)
        h.record_outcome(["space"], 3.5, False, NOW)
        for _ in range(5):
            h.record_outcome(["space"], 1.0, False, NOW)
        # frog 0/3 runners vs global 2/10 -> 1.0 + (0.0 - 0.2) = 0.8.
        # If the rug had counted as a runner it would be ~1.13.
        self.assertEqual(h.heat(["frog"], NOW), 0.8)

    def test_heat_ignores_stale_outcomes(self):
        h = NarrativeHeat()
        h.record_outcome(["dog"], 5.0, False, NOW - 90000)  # >24h ago
        h.record_outcome(["dog"], 5.0, False, NOW - 90000)
        h.record_outcome(["dog"], 5.0, False, NOW - 90000)
        self.assertEqual(h.heat(["dog"], NOW), 1.0)

    def test_heat_clamped_to_bounds(self):
        h = NarrativeHeat()
        for _ in range(100):
            h.record_outcome(["dog"], 9.0, False, NOW)
        for _ in range(100):
            h.record_outcome(["cat"], 0.1, False, NOW)
        self.assertLessEqual(h.heat(["dog"], NOW), 2.0)
        self.assertGreaterEqual(h.heat(["cat"], NOW), 0.5)


class HygieneTest(unittest.TestCase):
    def test_prune_drops_old_outcomes(self):
        h = NarrativeHeat()
        h.record_outcome(["dog"], 3.0, False, NOW - 100 * 3600)
        h.record_outcome(["cat"], 3.0, False, NOW)
        h.prune(NOW)
        self.assertEqual(len(h), 1)
        self.assertEqual(h.to_state()["outcomes"][0][0], NOW)

    def test_bound_eviction(self):
        h = NarrativeHeat()
        for i in range(1, 5006):
            h.record_outcome(["dog"], 1.5, False, float(i))
        self.assertEqual(len(h), 5000)
        # Oldest evicted first: earliest surviving ts is 6.
        self.assertEqual(h.to_state()["outcomes"][0][0], 6.0)
        self.assertEqual(h.to_state()["outcomes"][-1][0], 5005.0)

    def test_from_state_tolerant(self):
        self.assertEqual(len(NarrativeHeat.from_state(None)), 0)
        self.assertEqual(len(NarrativeHeat.from_state("junk")), 0)
        self.assertEqual(len(NarrativeHeat.from_state({"outcomes": "junk"})), 0)
        h = NarrativeHeat.from_state({"outcomes": [["bad"], None, 42]})
        self.assertEqual(len(h), 0)


class PersistenceTest(unittest.TestCase):
    def test_save_load_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "heat.json")
            h = _hot_setup()
            h.save(p)
            mode = stat.S_IMODE(os.stat(p).st_mode)
            self.assertEqual(mode, 0o600)
            h2 = NarrativeHeat.load(p)
            self.assertEqual(h2.heat(["dog"], NOW), 1.4)
            self.assertEqual(h2.heat(["cat"], NOW), 0.6)
            self.assertEqual(len(h2), 10)

    def test_load_missing_and_corrupt(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                len(NarrativeHeat.load(os.path.join(td, "nope.json"))), 0)
            bad = os.path.join(td, "bad.json")
            with open(bad, "w") as fh:
                fh.write("{not json")
            self.assertEqual(len(NarrativeHeat.load(bad)), 0)

    def test_default_heat_path(self):
        # Isolation: other test modules set FOREX_AGENT_HOME at import
        # time; this test must not depend on ambient environment.
        saved = os.environ.pop("FOREX_AGENT_HOME", None)
        try:
            p = str(default_heat_path())
        finally:
            if saved is not None:
                os.environ["FOREX_AGENT_HOME"] = saved
        self.assertTrue(p.endswith(
            os.path.join(".forex-agent", "launch_scanner",
                         "narrative_heat.json")))

    def test_default_heat_path_honors_env(self):
        saved = os.environ.get("FOREX_AGENT_HOME")
        os.environ["FOREX_AGENT_HOME"] = "/tmp/narr_env_test"
        try:
            p = str(default_heat_path())
        finally:
            if saved is None:
                os.environ.pop("FOREX_AGENT_HOME", None)
            else:
                os.environ["FOREX_AGENT_HOME"] = saved
        self.assertEqual(p, "/tmp/narr_env_test/launch_scanner/"
                            "narrative_heat.json")


if __name__ == "__main__":
    unittest.main()
