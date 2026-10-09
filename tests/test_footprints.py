"""Tests for footprint signals (measurement only).

Covers holder_velocity (crowd/quiet/unknown), accumulation
(loading/no/unknown), the measure() ledger behavior (bounded, TTL,
disabled, never-raises), and the config knob declarations.
"""

import dataclasses
import unittest
from types import SimpleNamespace

from daemon.launch_scanner import footprints


def _degen(**kw):
    d = {"footprints_enabled": True,
         "fp_velocity_crowd_per_min": 1.0,
         "fp_velocity_min_window_min": 5.0,
         "fp_accum_vol_accel": 2.0,
         "fp_accum_price_flat_pct": 10.0}
    d.update(kw)
    return SimpleNamespace(**d)


def _obs(ts, price=1.0, vol=1000.0, holders=None, mcap=100000.0):
    # (ts, mcap, price, vol_m5, buys_m5, sells_m5, liq, holders)
    return (float(ts), float(mcap), float(price), float(vol),
            10.0, 8.0, 50000.0, holders)


class HolderVelocityTests(unittest.TestCase):
    def test_crowd_arriving(self):
        # 100 -> 160 holders over 10 min = 6/min -> crowd
        obs = [_obs(0, holders=100), _obs(300, holders=130),
               _obs(600, holders=160)]
        r = footprints.holder_velocity(obs)
        self.assertEqual(r["verdict"], "crowd")
        self.assertAlmostEqual(r["per_min"], 6.0)
        self.assertEqual(r["holders_first"], 100)
        self.assertEqual(r["holders_last"], 160)

    def test_quiet_when_slow(self):
        # 100 -> 105 over 10 min = 0.5/min -> quiet
        obs = [_obs(0, holders=100), _obs(300, holders=102),
               _obs(600, holders=105)]
        r = footprints.holder_velocity(obs)
        self.assertEqual(r["verdict"], "quiet")
        self.assertAlmostEqual(r["per_min"], 0.5)

    def test_quiet_when_holders_leaving(self):
        obs = [_obs(0, holders=200), _obs(600, holders=150)]
        r = footprints.holder_velocity(obs)
        self.assertEqual(r["verdict"], "quiet")
        self.assertLess(r["per_min"], 0)

    def test_unknown_no_holder_data(self):
        # The common case for confirmed runners: holder refresh only
        # covers lowcap-tracked mints. Must be unknown, never faked.
        obs = [_obs(0), _obs(300), _obs(600)]
        r = footprints.holder_velocity(obs)
        self.assertEqual(r["verdict"], "unknown")

    def test_unknown_single_observation(self):
        obs = [_obs(0, holders=100), _obs(300), _obs(600)]
        r = footprints.holder_velocity(obs)
        self.assertEqual(r["verdict"], "unknown")

    def test_unknown_window_too_short(self):
        # 2 minutes of data can't establish a velocity.
        obs = [_obs(0, holders=100), _obs(120, holders=200)]
        r = footprints.holder_velocity(obs, min_window_min=5.0)
        self.assertEqual(r["verdict"], "unknown")

    def test_custom_threshold(self):
        obs = [_obs(0, holders=100), _obs(600, holders=160)]
        r = footprints.holder_velocity(obs, crowd_per_min=10.0)
        self.assertEqual(r["verdict"], "quiet")


class AccumulationTests(unittest.TestCase):
    def test_loading_vol_spike_flat_price(self):
        obs = [_obs(0, price=1.00, vol=1000.0),
               _obs(60, price=1.01, vol=1000.0),
               _obs(120, price=0.99, vol=1000.0),
               _obs(180, price=1.02, vol=5000.0)]
        r = footprints.accumulation(obs)
        self.assertEqual(r["verdict"], "loading")
        self.assertGreaterEqual(r["vol_accel"], 2.0)

    def test_no_when_price_moved(self):
        # Volume spiked but price already ran +30% — not quiet loading.
        obs = [_obs(0, price=1.00, vol=1000.0),
               _obs(60, price=1.10, vol=1000.0),
               _obs(120, price=1.30, vol=5000.0)]
        r = footprints.accumulation(obs)
        self.assertEqual(r["verdict"], "no")

    def test_no_when_no_vol_spike(self):
        obs = [_obs(0, price=1.00, vol=1000.0),
               _obs(60, price=1.01, vol=1100.0),
               _obs(120, price=1.00, vol=900.0)]
        r = footprints.accumulation(obs)
        self.assertEqual(r["verdict"], "no")

    def test_unknown_too_few_polls(self):
        obs = [_obs(0, price=1.0, vol=5000.0),
               _obs(60, price=1.0, vol=9000.0)]
        r = footprints.accumulation(obs)
        self.assertEqual(r["verdict"], "unknown")

    def test_tiny_base_does_not_fake_spike(self):
        # 1 -> 50 vol looks like 50x but the floor keeps it honest.
        obs = [_obs(0, price=1.0, vol=1.0),
               _obs(60, price=1.0, vol=2.0),
               _obs(120, price=1.0, vol=50.0)]
        r = footprints.accumulation(obs)
        self.assertEqual(r["verdict"], "no")


class MeasureTests(unittest.TestCase):
    def _series(self, mint="M1"):
        return {mint: [
            _obs(0, price=1.00, vol=1000.0, holders=100),
            _obs(300, price=1.01, vol=1000.0, holders=130),
            _obs(600, price=0.99, vol=1000.0, holders=160),
            _obs(900, price=1.02, vol=6000.0, holders=220),
        ]}

    def test_records_both_signals(self):
        state = {}
        rec = footprints.measure(state, self._series(), _degen(), "M1",
                                 now=1000.0)
        self.assertIsNotNone(rec)
        self.assertIn("M1", state["footprint_signals"])
        self.assertEqual(rec["velocity"]["verdict"], "crowd")
        self.assertEqual(rec["accumulation"]["verdict"], "loading")

    def test_disabled_records_nothing(self):
        state = {}
        rec = footprints.measure(state, self._series(),
                                 _degen(footprints_enabled=False), "M1")
        self.assertIsNone(rec)
        self.assertNotIn("footprint_signals", state)

    def test_no_mint_no_series(self):
        self.assertIsNone(footprints.measure({}, {}, _degen(), ""))
        rec = footprints.measure({}, {}, _degen(), "M9", now=10.0)
        self.assertEqual(rec["velocity"]["verdict"], "unknown")
        self.assertEqual(rec["accumulation"]["verdict"], "unknown")

    def test_never_raises_on_garbage(self):
        state = {}
        rec = footprints.measure(state, None, _degen(), "M1")
        self.assertIsNotNone(rec)
        rec = footprints.measure(state, {"M1": "junk"}, _degen(), "M1")
        self.assertIsNotNone(rec)
        rec = footprints.measure(state, {"M1": [(1,)]}, _degen(), "M1")
        self.assertIsNotNone(rec)

    def test_bounded_ledger(self):
        state = {"footprint_signals": {}}
        for i in range(600):
            footprints.measure(state, {}, _degen(), "M%d" % i,
                               now=1000.0 + i)
        self.assertLessEqual(len(state["footprint_signals"]), 500)
        # Newest survive.
        self.assertIn("M599", state["footprint_signals"])

    def test_prunes_stale(self):
        state = {"footprint_signals": {
            "OLD": {"ts": 1.0, "velocity": {}, "accumulation": {}}}}
        footprints.measure(state, {}, _degen(), "NEW",
                           now=8 * 24 * 3600.0)
        self.assertNotIn("OLD", state["footprint_signals"])
        self.assertIn("NEW", state["footprint_signals"])

    def test_summary_line(self):
        rec = {"velocity": {"verdict": "crowd"},
               "accumulation": {"verdict": "loading"}}
        self.assertEqual(footprints.summary(rec),
                         "VELOCITY:crowd ACCUM:loading")
        rec = {"velocity": {"verdict": "unknown"},
               "accumulation": {"verdict": "no"}}
        self.assertEqual(footprints.summary(rec),
                         "VELOCITY:unknown ACCUM:no")
        self.assertEqual(footprints.summary(None), "no footprint data")


class ConfigKnobTests(unittest.TestCase):
    def test_degen_config_declares_footprint_knobs(self):
        from config.config import DegenConfig
        names = {f.name for f in dataclasses.fields(DegenConfig)}
        for k in ("footprints_enabled", "fp_velocity_crowd_per_min",
                  "fp_velocity_min_window_min", "fp_accum_vol_accel",
                  "fp_accum_price_flat_pct"):
            self.assertIn(k, names)

    def test_yaml_declares_footprint_knobs(self):
        import yaml
        with open("config/defaults.yaml") as fh:
            y = yaml.safe_load(fh)
        degen = y.get("degen") or {}
        for k in ("footprints_enabled", "fp_velocity_crowd_per_min",
                  "fp_velocity_min_window_min", "fp_accum_vol_accel",
                  "fp_accum_price_flat_pct"):
            self.assertIn(k, degen,
                          "defaults.yaml missing degen.%s" % k)

    def test_knob_defaults_used_when_missing(self):
        # A degen namespace without fp_* attrs must not crash
        # measure() — getattr defaults apply (no repeat of the
        # two-tier getattr bug).
        rec = footprints.measure({}, {}, SimpleNamespace(), "M",
                                 now=10.0)
        self.assertIsNotNone(rec)


if __name__ == "__main__":
    unittest.main()
