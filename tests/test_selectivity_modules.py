"""Tests for the three built-but-unwired selectivity modules (2026-10-01).

- holder_quality (idea #1): holder-growth vs mcap divergence ranker.
- sustained (idea #4): is-the-run-still-alive re-check.
- wavefront (idea #3): second-wave outcome ledger + trigger proposals.

None of these are wired into the daemon; these tests pin the pure
behavior so wiring later is safe.
"""
import os
import sys
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from daemon.launch_scanner import holder_quality, sustained, wavefront


class HolderQualityTest(unittest.TestCase):
    def test_broadening_scores_high(self):
        inp = holder_quality.holder_inputs(
            {"holder_growth_pct": 60.0, "buy_pressure": 0.7},
            {}, holder_count=1500.0)
        s, factors = holder_quality.score(inp, mcap_growth_pct=40.0)
        self.assertGreaterEqual(s, 80.0)
        self.assertTrue(any("broadening" in f for f in factors))

    def test_distribution_scores_low(self):
        # mcap sprinting while holders stall = distribution into exits.
        inp = holder_quality.holder_inputs(
            {"holder_growth_pct": 2.0, "buy_pressure": 0.5}, {})
        s, factors = holder_quality.score(inp, mcap_growth_pct=120.0)
        self.assertLessEqual(s, 45.0)
        self.assertTrue(any("distribution" in f for f in factors))

    def test_concentrated_top_holder_penalized(self):
        inp = holder_quality.holder_inputs(
            {"holder_growth_pct": 30.0},
            {"top_holder_share_pct": 65.0})
        s, _ = holder_quality.score(inp, mcap_growth_pct=20.0)
        s2, _ = holder_quality.score(
            holder_quality.holder_inputs({"holder_growth_pct": 30.0}, {}),
            mcap_growth_pct=20.0)
        self.assertLess(s, s2)

    def test_unknowns_are_neutral(self):
        s, _ = holder_quality.score(holder_quality.holder_inputs(None, None))
        self.assertEqual(s, 50.0)

    def test_unknown_holder_growth_never_flags_distribution(self):
        # Regression: unknown holder growth used to coerce to 0.0, which
        # falsely flagged "distribution" on any coin with mcap growth.
        inp = holder_quality.holder_inputs(
            {"buy_pressure": 0.7}, {})  # holder_growth_pct absent
        s, factors = holder_quality.score(inp, mcap_growth_pct=120.0)
        self.assertEqual(s, 60.0)  # 50 + 10 buy pressure only
        self.assertFalse(any("distribution" in f for f in factors))
        self.assertTrue(any("divergence skipped" in f for f in factors))

    def test_known_zero_holder_growth_flags_distribution(self):
        # ...but a KNOWN 0% holder growth with mcap sprinting IS
        # distribution — the signal still works when data exists.
        inp = holder_quality.holder_inputs(
            {"holder_growth_pct": 0.0, "buy_pressure": 0.5}, {})
        s, factors = holder_quality.score(inp, mcap_growth_pct=120.0)
        self.assertLessEqual(s, 40.0)
        self.assertTrue(any("distribution" in f for f in factors))

    def test_rank_orders_best_first(self):
        cands = [
            {"mint": "A", "holder_inputs": holder_quality.holder_inputs(
                {"holder_growth_pct": 5.0}, {}), "mcap_growth_pct": 100.0},
            {"mint": "B", "holder_inputs": holder_quality.holder_inputs(
                {"holder_growth_pct": 55.0, "buy_pressure": 0.7}, {}),
             "mcap_growth_pct": 40.0},
        ]
        ranked = holder_quality.rank(cands)
        self.assertEqual(ranked[0]["mint"], "B")
        self.assertIn("holder_score", ranked[0])
        self.assertIn("holder_factors", ranked[0])

    def test_never_raises_on_garbage(self):
        s, f = holder_quality.score(None, None)
        self.assertIsInstance(s, float)
        self.assertEqual(holder_quality.rank(None), [])


class SustainedTest(unittest.TestCase):
    def _series(self, mcaps, step_s=60, t0=None):
        t0 = t0 if t0 is not None else time.time() - len(mcaps) * step_s
        return [(t0 + i * step_s, m) for i, m in enumerate(mcaps)]

    def test_sustained_run(self):
        # Climbs then holds near peak.
        s = self._series([100, 200, 400, 380, 390, 370])
        verdict, ev = sustained.assess(s)
        self.assertEqual(verdict, "SUSTAINED")
        self.assertGreaterEqual(ev["hold_pct"], 70.0)

    def test_dead_after_rug(self):
        s = self._series([100, 400, 380, 120, 80])
        verdict, ev = sustained.assess(s)
        self.assertEqual(verdict, "DEAD")

    def test_fading_mid_drawdown(self):
        # Holds 70% of peak but the last-15-min trend is clearly down.
        now = time.time()
        s = [(now - 2000, 100), (now - 1700, 400), (now - 1400, 360),
             (now - 1000, 350), (now - 500, 330), (now - 100, 280)]
        verdict, ev = sustained.assess(s, now=now)
        self.assertEqual(verdict, "FADING")
        self.assertLess(ev["trend_pct"], -10.0)

    def test_empty_series_is_dead(self):
        verdict, _ = sustained.assess([])
        self.assertEqual(verdict, "DEAD")

    def test_watch_seed_and_due(self):
        state = {}
        degen = SimpleNamespace(sustained_delay_min=30.0)
        now = time.time()
        sustained.seed_watch(state, "M1", "AAA", now=now - 3600)
        self.assertTrue(sustained.due_for_check(state, "M1", degen,
                                                now=now))
        # Too early: not due.
        state2 = {}
        sustained.seed_watch(state2, "M1", "AAA", now=now - 60)
        self.assertFalse(sustained.due_for_check(state2, "M1", degen,
                                                 now=now))

    def test_check_marks_once(self):
        state = {}
        degen = SimpleNamespace()
        now = time.time()
        sustained.seed_watch(state, "M1", "AAA", now=now - 7200)
        s = self._series([100, 300, 290, 295])
        r1 = sustained.check(state, degen, "M1", s, now=now)
        self.assertTrue(r1["due"])
        self.assertEqual(r1["verdict"], "SUSTAINED")
        r2 = sustained.check(state, degen, "M1", s, now=now)
        self.assertFalse(r2["due"])
        self.assertEqual(r2["verdict"], "WAIT")

    def test_watch_bounded(self):
        state = {}
        for i in range(600):
            sustained.seed_watch(state, "M%d" % i, "X",
                                 now=float(i), max_entries=500)
        self.assertLessEqual(len(state["sustained_watch"]), 500)


class WavefrontTest(unittest.TestCase):
    def _wave(self, mint, pb=45.0, rg=60.0, mult_now=3.0):
        return {"event": "launch.second_wave", "mint": mint,
                "symbol": mint[:4], "wave_number": 2,
                "pullback_pct": pb, "relegain_pct": rg,
                "peak_multiple": 5.0, "multiple_now": mult_now,
                "safety_score": 10}

    def test_record_and_settle(self):
        state = {}
        now = time.time()
        self.assertTrue(wavefront.record_wave(
            state, self._wave("M1"), now=now - 100000))
        self.assertTrue(wavefront.record_wave(
            state, self._wave("M2"), now=now - 100000))
        mw = {"M1": {"peak_mult": 9.0}, "M2": {"peak_mult": 3.0}}
        n = wavefront.settle(state, mw, now=now)
        self.assertEqual(n, 2)
        ledger = state["wavefront_ledger"]
        by_mint = {r["mint"]: r for r in ledger}
        # forward = peak / multiple_now
        self.assertAlmostEqual(by_mint["M1"]["forward_mult"], 3.0)
        self.assertTrue(by_mint["M1"]["hit_2x"])
        self.assertFalse(by_mint["M2"]["hit_2x"])
        self.assertEqual(state["wavefront"], {})

    def test_young_waves_not_settled(self):
        state = {}
        now = time.time()
        wavefront.record_wave(state, self._wave("M1"), now=now)
        n = wavefront.settle(state, {"M1": {"peak_mult": 9.0}}, now=now)
        self.assertEqual(n, 0)
        self.assertIn("M1", state["wavefront"])

    def test_report_honest_when_unproven(self):
        rep = wavefront.report({})
        self.assertEqual(rep["waves_settled"], 0)
        self.assertIn("unproven", rep["verdict"])

    def test_report_with_data(self):
        state = {"wavefront_ledger": [
            {"mint": "M%d" % i, "forward_mult": 3.0 if i < 4 else 1.2,
             "hit_2x": i < 4} for i in range(12)]}
        rep = wavefront.report(state)
        self.assertEqual(rep["waves_settled"], 12)
        self.assertAlmostEqual(rep["hit_rate"], 4 / 12, places=2)

    def test_analyze_needs_minimum(self):
        self.assertEqual(wavefront.analyze(SimpleNamespace(), []), [])

    def test_analyze_proposes_when_clear(self):
        rows = []
        for i in range(14):
            # Deep pullbacks win; shallow ones don't.
            deep = i < 7
            rows.append({
                "mint": "M%d" % i,
                "pullback_pct": 60.0 if deep else 25.0,
                "relegain_pct": 60.0,
                "forward_mult": 3.0 if deep else 1.1,
                "hit_2x": deep,
            })
        props = wavefront.analyze(SimpleNamespace(), rows)
        knobs = {p["knob"] for p in props}
        self.assertIn("second_wave_pullback_pct", knobs)
        p = next(p for p in props
                 if p["knob"] == "second_wave_pullback_pct")
        self.assertGreater(p["suggested"], p["current"])  # raise the bar
        lo, hi = wavefront.KNOB_CLAMPS["second_wave_pullback_pct"]
        self.assertLessEqual(p["suggested"], hi)

    def test_record_bounded(self):
        state = {}
        for i in range(600):
            wavefront.record_wave(state, self._wave("M%d" % i))
        self.assertLessEqual(len(state["wavefront"]), 500)


if __name__ == "__main__":
    unittest.main()
