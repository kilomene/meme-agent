"""Wiring tests for selectivity ideas #1, #4, #3 (user decision 2026-10-01).

Verifies the bot integration (not just the pure modules):
  #1 holder-quality ranker   -> _holder_score on both card lanes,
                             holder_score/holder_factors on the cards,
                             schema + Telegram line.
  #4 sustained-velocity tier -> seed at alert time, delayed re-check in
                             _sustained_poll, launch.sustained card only
                             on SUSTAINED, schema + renderer.
  #3 second-wave measurement -> record_wave on every published wave
                             card, daily settle before the learner,
                             proposal-only knobs after >=10 settled waves.
"""
import time
import types
import unittest
from unittest import mock

from agent import events as event_bus
from agent.events import bus
from daemon.launch_scanner import feedback, holder_quality, milestones
from daemon.launch_scanner import sustained, wavefront
from daemon.launch_scanner import LaunchScanner
import daemon.telegram.alerts as alerts


def _degen(**kw):
    d = {
        "sustained_enabled": True,
        "sustained_delay_min": 30.0,
        "sustained_peak_hold_pct": 70.0,
        "sustained_lookback_min": 15.0,
        "sustained_max_drawdown_pct": 30.0,
        "wavefront_enabled": True,
        "milestone_enabled": True,
        "second_wave_pullback_pct": 40.0,
        "second_wave_releg_gain_pct": 50.0,
        "second_wave_reentry_window_min": 45.0,
    }
    d.update(kw)
    return types.SimpleNamespace(**d)


class HolderWiringTests(unittest.TestCase):
    def test_holder_score_unknown_is_neutral(self):
        s, factors = LaunchScanner._holder_score(
            None, {}, {"safety": None}, "MINT1", None)
        self.assertEqual(s, 50.0)
        # Only informational factors — nothing that moves the score.
        self.assertFalse(any("distribution" in f or "holders +" in f
                             for f in factors))

    def test_holder_score_uses_fingerprint_and_divergence(self):
        state = {"fingerprints": {
            "MINT2": {"holder_growth_pct": 60.0, "buy_pressure": 0.8}}}
        s, factors = LaunchScanner._holder_score(
            None, state, {"safety": None}, "MINT2", 40.0)
        self.assertGreater(s, 80.0)
        self.assertTrue(any("broadening" in f or "keeping up" in f
                            for f in factors))

    def test_holder_score_distribution_detected(self):
        state = {"fingerprints": {
            "MINT3": {"holder_growth_pct": 2.0, "buy_pressure": 0.5}}}
        s, factors = LaunchScanner._holder_score(
            None, state, {"safety": None}, "MINT3", 150.0)
        self.assertLess(s, 50.0)
        self.assertTrue(any("distribution" in f for f in factors))

    def test_holder_schema_accepts_fields(self):
        for ev_name in ("launch.detected", "launch.lowcap_runner"):
            ev = {"event": ev_name, "symbol": "X", "holder_score": 72.5,
                  "holder_factors": "holders +40%"}
            bus.validate_event(dict(ev))  # must not raise

    def test_holder_line_renders_and_omits_when_absent(self):
        line = alerts._holder_line(
            {"holder_score": 72.5, "holder_factors": "holders +40%"})
        self.assertIn("72/100", line)
        self.assertIn("holders +40%", line)
        self.assertEqual(alerts._holder_line({}), "")


class SustainedWiringTests(unittest.TestCase):
    def _stub_self(self):
        return types.SimpleNamespace(
            _fp_tracker=None,
            _trade_plan=lambda degen: "stop 30% / TP 200%")

    def test_sustained_card_publishes_on_sustained(self):
        degen = _degen()
        now = time.time()
        state = {"sustained_watch": {
            "MINT1": {"symbol": "ABC", "confirm_ts": now - 31 * 60,
                      "checked": False, "chain": "solana",
                      "card_mcap": 100000.0}},
                 "milestone_watch": {
            "MINT1": {"symbol": "ABC", "chain": "solana",
                      "anchor": 100000.0, "peak_mult": 5.0,
                      "mcap_now": 450000.0}}}
        published = []
        with mock.patch.object(event_bus, "publish",
                               side_effect=lambda e: published.append(e)):
            LaunchScanner._sustained_poll(self._stub_self(),
                                         types.SimpleNamespace(degen=degen),
                                         state, now)
        self.assertEqual(len(published), 1)
        card = published[0]
        self.assertEqual(card["event"], "launch.sustained")
        self.assertEqual(card["symbol"], "ABC")
        self.assertAlmostEqual(card["hold_pct"], 90.0, places=0)
        bus.validate_event(dict(card))  # schema accepts it

    def test_fading_and_dead_stay_silent(self):
        degen = _degen()
        now = time.time()
        state = {"sustained_watch": {
            "F1": {"symbol": "F", "confirm_ts": now - 31 * 60,
                   "checked": False, "card_mcap": 100000.0},
            "D1": {"symbol": "D", "confirm_ts": now - 31 * 60,
                   "checked": False, "card_mcap": 100000.0}},
                 "milestone_watch": {
            "F1": {"anchor": 100000.0, "peak_mult": 5.0,
                   "mcap_now": 200000.0},   # 40% hold -> FADING
            "D1": {"anchor": 100000.0, "peak_mult": 5.0,
                   "mcap_now": 50000.0}}}   # 10% hold -> DEAD
        published = []
        with mock.patch.object(event_bus, "publish",
                               side_effect=lambda e: published.append(e)):
            LaunchScanner._sustained_poll(self._stub_self(),
                                         types.SimpleNamespace(degen=degen),
                                         state, now)
        self.assertEqual(published, [])
        self.assertTrue(state["sustained_watch"]["F1"]["checked"])
        self.assertTrue(state["sustained_watch"]["D1"]["checked"])

    def test_not_due_does_not_fire(self):
        degen = _degen()
        now = time.time()
        state = {"sustained_watch": {
            "MINT1": {"symbol": "ABC", "confirm_ts": now - 5 * 60,
                      "checked": False, "card_mcap": 100000.0}},
                 "milestone_watch": {
            "MINT1": {"anchor": 100000.0, "peak_mult": 5.0,
                      "mcap_now": 500000.0}}}
        published = []
        with mock.patch.object(event_bus, "publish",
                               side_effect=lambda e: published.append(e)):
            LaunchScanner._sustained_poll(self._stub_self(),
                                         types.SimpleNamespace(degen=degen),
                                         state, now)
        self.assertEqual(published, [])
        self.assertFalse(state["sustained_watch"]["MINT1"]["checked"])

    def test_disabled_does_nothing(self):
        degen = _degen(sustained_enabled=False)
        state = {"sustained_watch": {
            "MINT1": {"symbol": "ABC", "confirm_ts": 0, "checked": False}}}
        with mock.patch.object(event_bus, "publish") as pub:
            LaunchScanner._sustained_poll(self._stub_self(),
                                         types.SimpleNamespace(degen=degen),
                                         state, time.time())
        pub.assert_not_called()

    def test_sustained_renderer(self):
        card = {"event": "launch.sustained", "symbol": "ABC",
                "chain": "solana", "mint": "M1",
                "usd_market_cap": 450000.0, "hold_pct": 90.0,
                "trend_pct": 5.0, "drawdown_pct": 10.0,
                "peak_mult": 5.0, "minutes_since_card": 31.0,
                "note": "n"}
        text = alerts.FORMATTERS["launch.sustained"](card)
        self.assertIn("SUSTAINED RUNNER", text)
        self.assertIn("ABC", text)


class WavefrontWiringTests(unittest.TestCase):
    def test_record_and_settle_roundtrip(self):
        degen = _degen()
        state = {}
        card = {"event": "launch.second_wave", "symbol": "W",
                "mint": "WM1", "wave_number": 2,
                "pullback_pct": 45.0, "relegain_pct": 60.0,
                "peak_multiple": 5.0, "multiple_now": 3.0}
        self.assertTrue(wavefront.record_wave(state, card, 1000.0))
        settled = wavefront.settle(
            state, {"WM1": {"peak_mult": 6.0}}, 1000.0 + 25 * 3600)
        self.assertEqual(settled, 1)
        ledger = state["wavefront_ledger"]
        self.assertEqual(len(ledger), 1)
        self.assertAlmostEqual(ledger[0]["forward_mult"], 2.0)
        self.assertTrue(ledger[0]["hit_2x"])

    def test_analyze_quiet_below_ten_settled(self):
        rows = [{"forward_mult": 3.0, "pullback_pct": 45.0,
                 "relegain_pct": 60.0,
                 "reentry_window_min": 45.0} for _ in range(9)]
        self.assertEqual(wavefront.analyze(_degen(), rows), [])

    def _spread_rows(self):
        # 6 shallow-pullback misses + 6 deep-pullback hits: deeper
        # pullbacks clearly win -> proposes raising the pullback bar.
        rows = []
        for _ in range(6):
            rows.append({"forward_mult": 0.5, "hit_2x": False,
                         "pullback_pct": 25.0, "relegain_pct": 60.0,
                         "reentry_window_min": 45.0})
        for _ in range(6):
            rows.append({"forward_mult": 3.0, "hit_2x": True,
                         "pullback_pct": 60.0, "relegain_pct": 60.0,
                         "reentry_window_min": 45.0})
        return rows

    def test_analyze_proposes_after_ten(self):
        props = wavefront.analyze(_degen(), self._spread_rows())
        self.assertTrue(props)
        knobs = {p["knob"] for p in props}
        self.assertIn("second_wave_pullback_pct", knobs)

    def test_knobs_are_proposal_only_and_guarded(self):
        for knob in ("second_wave_pullback_pct",
                     "second_wave_releg_gain_pct",
                     "second_wave_reentry_window_min"):
            self.assertIn(knob, feedback.TUNABLE)
            self.assertNotIn(knob, feedback.SAFETY_NEVER_TUNE)
            lo, hi = feedback.KNOB_CLAMPS[knob]
            self.assertLess(lo, hi)
            # Clamp ranges mirror the wavefront module's own ranges.
            self.assertEqual((lo, hi), wavefront.KNOB_CLAMPS[knob])

    def test_feedback_threads_wave_rows(self):
        # analyze() must accept and use wave_rows without real outcomes.
        # The other analyzers are stubbed: production never runs them on
        # an empty dataset (min_n early-return), this only exercises
        # the wavefront threading.
        with mock.patch.object(feedback, "load_outcomes",
                               return_value=[]), \
             mock.patch.object(feedback, "_min_n", return_value=0), \
             mock.patch.object(feedback, "load_proposals",
                               return_value=[]), \
             mock.patch.object(feedback, "analyze_ranking",
                               return_value=[]), \
             mock.patch.object(feedback, "analyze_entry",
                               return_value=[]), \
             mock.patch.object(feedback, "analyze_exits",
                               return_value=[]), \
             mock.patch.object(feedback.winner_model,
                               "analyze_winner_ranking", return_value=[]):
            props = feedback.analyze(_degen(),
                                     wave_rows=self._spread_rows())
        # 12 settled waves with deeper pullbacks winning -> pullback
        # proposal survives the guard
        self.assertTrue(any(p["knob"] == "second_wave_pullback_pct"
                            for p in props))


if __name__ == "__main__":
    unittest.main()
