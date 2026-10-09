"""Tests for the Leg A profit-lock trail (v2 exits).

The profit lock banks Leg A profit early: once the peak hits
leg_a_trail_trigger_pct (+20%), a leg_a_trail_drawdown_pct (15%)
pullback exits with reason "profit_lock" instead of round-tripping to
the stop. Past +50% the pre-existing 30% trail governs ("trail_stop",
unchanged). Leg B keeps the single pre-existing stage — the moon bag
keeps full room to run.
"""

import dataclasses
import unittest
from types import SimpleNamespace

from daemon.launch_scanner import EXIT_POLICY_VERSION, trail_exit_reason


def _degen(**kw):
    d = {"trail_trigger_pct": 50.0,
         "trail_drawdown_pct": 30.0,
         "leg_a_trail_trigger_pct": 20.0,
         "leg_a_trail_drawdown_pct": 15.0}
    d.update(kw)
    return SimpleNamespace(**d)


class ProfitLockTests(unittest.TestCase):
    def test_leg_a_stage1_fires(self):
        # Peak +25%, price 16% off peak -> bank the profit.
        r = trail_exit_reason(_degen(), "A", 1.0, 1.25, 1.05)
        self.assertEqual(r, "profit_lock")

    def test_leg_a_stage1_holds_on_small_pullback(self):
        # Peak +25%, price only 12% off peak -> hold.
        r = trail_exit_reason(_degen(), "A", 1.0, 1.25, 1.10)
        self.assertIsNone(r)

    def test_leg_a_stage1_boundary(self):
        # Peak exactly +20%: 15% pullback exits at ~+2%.
        self.assertEqual(
            trail_exit_reason(_degen(), "A", 1.0, 1.20, 1.02),
            "profit_lock")
        self.assertIsNone(
            trail_exit_reason(_degen(), "A", 1.0, 1.20, 1.03))

    def test_leg_a_stage1_never_loses_money(self):
        # Worst case: armed at +20%, exits at +2% — never a loss.
        r = trail_exit_reason(_degen(), "A", 1.0, 1.20, 1.019)
        self.assertEqual(r, "profit_lock")

    def test_leg_a_stage2_unchanged(self):
        # Peak +60%: the pre-existing 30% trail governs, not the
        # tighter 15% stage.
        self.assertIsNone(
            trail_exit_reason(_degen(), "A", 1.0, 1.60, 1.20))  # -25%
        self.assertEqual(
            trail_exit_reason(_degen(), "A", 1.0, 1.60, 1.10),  # -31%
            "trail_stop")

    def test_leg_a_below_trigger_no_trail(self):
        # Peak +10%: trail not armed — the stop-loss owns this zone,
        # not the trail.
        self.assertIsNone(
            trail_exit_reason(_degen(), "A", 1.0, 1.10, 0.80))

    def test_leg_b_unchanged(self):
        # Leg B: single pre-existing stage. +25% peak with a 16%
        # pullback does NOT exit (Leg A would profit_lock here).
        self.assertIsNone(
            trail_exit_reason(_degen(), "B", 1.0, 1.25, 1.05))
        self.assertEqual(
            trail_exit_reason(_degen(), "B", 1.0, 1.60, 1.10),
            "trail_stop")

    def test_missing_leg_treated_as_a(self):
        self.assertEqual(
            trail_exit_reason(_degen(), None, 1.0, 1.25, 1.05),
            "profit_lock")

    def test_knob_overrides(self):
        d = _degen(leg_a_trail_trigger_pct=30.0,
                   leg_a_trail_drawdown_pct=10.0)
        # +25% peak no longer arms the lock (needs +30%).
        self.assertIsNone(trail_exit_reason(d, "A", 1.0, 1.25, 1.05))
        # +35% peak, 11% pullback -> fires under the custom knobs.
        self.assertEqual(
            trail_exit_reason(d, "A", 1.0, 1.35, 1.20), "profit_lock")

    def test_never_raises_on_garbage(self):
        # degen=None still works: getattr defaults apply (never
        # raises, sensible fallback) — the profit lock arms normally.
        self.assertEqual(
            trail_exit_reason(None, "A", 1.0, 1.25, 1.05), "profit_lock")
        for args in [(_degen(), "A", 0, 1.25, 1.05),
                      (_degen(), "A", -1.0, 1.25, 1.05),
                      (_degen(), "A", 1.0, 0, 1.05),
                      (_degen(), "A", 1.0, 1.25, 0),
                      (_degen(), "A", 1.0, 0.9, 1.05)]:
            self.assertIsNone(trail_exit_reason(*args))

    def test_knob_defaults_when_missing(self):
        # A degen namespace without leg_a_* attrs must not crash —
        # getattr defaults apply (no repeat of the two-tier getattr bug).
        r = trail_exit_reason(SimpleNamespace(), "A", 1.0, 1.25, 1.05)
        self.assertEqual(r, "profit_lock")


class ExitVersionTests(unittest.TestCase):
    def test_version_is_v2(self):
        self.assertEqual(EXIT_POLICY_VERSION, "v2")

    def test_feedback_knows_profit_lock(self):
        from daemon.launch_scanner import feedback
        self.assertIn("profit_lock", feedback.EXIT_REASONS)


class ConfigKnobTests(unittest.TestCase):
    def test_degen_config_declares_leg_a_trail_knobs(self):
        from config.config import DegenConfig
        names = {f.name for f in dataclasses.fields(DegenConfig)}
        for k in ("leg_a_trail_trigger_pct", "leg_a_trail_drawdown_pct"):
            self.assertIn(k, names)

    def test_yaml_declares_leg_a_trail_knobs(self):
        import yaml
        with open("config/defaults.yaml") as fh:
            y = yaml.safe_load(fh)
        degen = y.get("degen") or {}
        for k in ("leg_a_trail_trigger_pct", "leg_a_trail_drawdown_pct"):
            self.assertIn(k, degen,
                          "defaults.yaml missing degen.%s" % k)


if __name__ == "__main__":
    unittest.main()
