"""Tests for the early-fingerprint toolkit (fingerprints.py) and its
low-cap hunter integration: buy acceleration, holder growth, volume
acceleration, composite HOT/WARMING/COLD scoring.
"""

import io
import unittest
from types import SimpleNamespace
from unittest import mock

from daemon.launch_scanner import fingerprints
from daemon.launch_scanner import lowcap


def _cfg(**kw):
    base = dict(
        fp_enabled=True,
        fp_window_polls=6,
        fp_min_polls=3,
        fp_buy_floor=3.0,
        fp_vol_floor_usd=1000.0,
        fp_hot_score=70.0,
        fp_warm_score=45.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _stats(mcap=100000.0, vol=2000.0, buys=5, sells=2, liq=20000.0):
    return {"usd_market_cap": mcap, "price_usd": 0.001,
            "usd_liquidity": liq, "vol_m5": vol,
            "buys_m5": buys, "sells_m5": sells}


def _drive(buys, vols=None, holders=None, sells=None, cfg=None):
    t = fingerprints.FingerprintTracker(cfg or _cfg())
    now = 1000.0
    for i, b in enumerate(buys):
        v = (vols[i] if vols else 2000.0)
        s = (sells[i] if sells else 2)
        h = (holders[i] if holders else None)
        t.observe("MINT", _stats(vol=v, buys=b, sells=s), now, holders=h)
        now += 30.0
    return t


class BuyAccelerationTests(unittest.TestCase):
    def test_accelerating_buys(self):
        t = _drive([2, 2, 10])
        sc = t.score("MINT")
        self.assertAlmostEqual(sc["buy_accel"], 10 / 3.0, places=2)

    def test_flat_buys_no_accel(self):
        t = _drive([5, 5, 5])
        sc = t.score("MINT")
        self.assertAlmostEqual(sc["buy_accel"], 1.0, places=2)

    def test_floor_stops_tiny_base_faking(self):
        t = _drive([0, 1, 2])
        sc = t.score("MINT")
        # denominator floored at 3.0, not mean(0,1)=0.5
        self.assertAlmostEqual(sc["buy_accel"], 2 / 3.0, places=2)

    def test_accel_capped(self):
        t = _drive([3, 3, 300])
        sc = t.score("MINT")
        self.assertEqual(sc["buy_accel"], 9.99)

    def test_rising_buy_streak(self):
        t = _drive([2, 5, 9])
        sc = t.score("MINT")
        self.assertEqual(sc["rising_buys"], 2)

    def test_broken_streak(self):
        t = _drive([9, 5, 9])
        sc = t.score("MINT")
        self.assertEqual(sc["rising_buys"], 1)


class VolumeAccelerationTests(unittest.TestCase):
    def test_accelerating_volume(self):
        t = _drive([5, 5, 5], vols=[1500.0, 1500.0, 9000.0])
        sc = t.score("MINT")
        self.assertAlmostEqual(sc["vol_accel"], 9000 / 1500.0, places=2)

    def test_vol_floor(self):
        t = _drive([5, 5, 5], vols=[10.0, 10.0, 5000.0])
        sc = t.score("MINT")
        self.assertAlmostEqual(sc["vol_accel"], 5000 / 1000.0, places=2)


class HolderGrowthTests(unittest.TestCase):
    def test_holder_growth_pct(self):
        t = _drive([5, 5, 5], holders=[100, 120, 150])
        sc = t.score("MINT")
        self.assertAlmostEqual(sc["holder_growth_pct"], 50.0, places=1)

    def test_no_holders_is_none(self):
        t = _drive([5, 5, 5])
        sc = t.score("MINT")
        self.assertIsNone(sc["holder_growth_pct"])

    def test_single_holder_obs_is_none(self):
        t = _drive([5, 5, 5], holders=[None, None, 150])
        sc = t.score("MINT")
        self.assertIsNone(sc["holder_growth_pct"])


class CompositeScoreTests(unittest.TestCase):
    def test_hot_on_strong_accel(self):
        t = _drive([3, 3, 15], vols=[1500.0, 1500.0, 9000.0], sells=[0, 0, 0])
        sc = t.score("MINT")
        self.assertEqual(sc["verdict"], "HOT")
        self.assertGreaterEqual(sc["composite"], 70.0)

    def test_warming_on_moderate(self):
        t = _drive([4, 4, 8], vols=[2000.0, 2000.0, 4000.0])
        sc = t.score("MINT")
        self.assertEqual(sc["verdict"], "WARMING")

    def test_cold_on_flat(self):
        t = _drive([5, 5, 5], vols=[2000.0, 2000.0, 2000.0])
        sc = t.score("MINT")
        self.assertEqual(sc["verdict"], "COLD")

    def test_holder_growth_lifts_score(self):
        plain = _drive([3, 3, 15], vols=[1500.0, 1500.0, 9000.0],
                       sells=[0, 0, 0]).score("MINT")
        grown = _drive([3, 3, 15], vols=[1500.0, 1500.0, 9000.0],
                       sells=[0, 0, 0],
                       holders=[100, 130, 200]).score("MINT")
        self.assertGreater(grown["composite"], plain["composite"])

    def test_too_early_returns_none(self):
        t = fingerprints.FingerprintTracker(_cfg())
        t.observe("MINT", _stats(), 1000.0)
        t.observe("MINT", _stats(), 1030.0)
        self.assertIsNone(t.score("MINT"))

    def test_unknown_mint_returns_none(self):
        t = fingerprints.FingerprintTracker(_cfg())
        self.assertIsNone(t.score("NOPE"))

    def test_disabled_observe_noop(self):
        t = fingerprints.FingerprintTracker(_cfg(fp_enabled=False))
        t.observe("MINT", _stats(), 1000.0)
        self.assertIsNone(t.score("MINT"))

    def test_prune_drops_stale(self):
        t = fingerprints.FingerprintTracker(_cfg())
        t.observe("MINT", _stats(), 1000.0)
        t.prune(1000.0 + 7200.0)
        self.assertNotIn("MINT", t._series)


class FetchHoldersTests(unittest.TestCase):
    def _resp(self, payload):
        m = mock.MagicMock()
        m.read.return_value = payload.encode()
        m.__enter__.return_value = m
        m.__exit__.return_value = False
        return m

    def test_parses_total_holders(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=self._resp('{"totalHolders": 1234}')):
            self.assertEqual(
                fingerprints.fetch_total_holders("MINT"), 1234)

    def test_zero_or_missing_is_none(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=self._resp('{"totalHolders": 0}')):
            self.assertIsNone(fingerprints.fetch_total_holders("MINT"))

    def test_network_trouble_is_none(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=Exception("boom")):
            self.assertIsNone(fingerprints.fetch_total_holders("MINT"))

    def test_empty_mint_is_none(self):
        self.assertIsNone(fingerprints.fetch_total_holders(""))


def _lowcap_cfg(**kw):
    base = dict(
        lowcap_enabled=True,
        lowcap_mcap_ceiling_usd=500000.0,
        lowcap_min_run_pct=70.0,
        lowcap_max_run_pct=100.0,
        lowcap_rising_polls=2,
        lowcap_min_m5_volume_usd=2000.0,
        lowcap_min_liquidity_usd=8000.0,
        lowcap_min_buys_m5=4,
        lowcap_min_buy_sell_ratio=1.3,
        lowcap_window_minutes=30.0,
        lowcap_fp_boost_enabled=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _lc_stats(mcap):
    return {"usd_market_cap": mcap, "price_usd": 0.001,
            "usd_liquidity": 20000.0, "vol_m5": 5000.0,
            "buys_m5": 8, "sells_m5": 3}


def _verdict_pass(c):
    return True, "quality-pass score 62", {"score": 62}


_HOT_FP = {"composite": 85.0, "verdict": "HOT", "buy_accel": 4.2,
           "vol_accel": 3.1, "holder_growth_pct": 40.0}


class LowcapFingerprintBoostTests(unittest.TestCase):
    def _run(self, streak, fp_score, cfg=None):
        h = lowcap.LowcapHunter(cfg or _lowcap_cfg())
        tracked: dict = {}
        probe: dict = {}
        h.update(tracked, mint="MINT", symbol="TST", name="t",
                 chain="solana", source="s", url="u",
                 stats=_lc_stats(100000.0), rising_streak=0,
                 now=1000.0, verdict_fn=_verdict_pass, probe=probe)
        ev = h.update(tracked, mint="MINT", symbol="TST", name="t",
                      chain="solana", source="s", url="u",
                      stats=_lc_stats(175000.0), rising_streak=streak,
                      now=1030.0, verdict_fn=_verdict_pass, probe=probe,
                      fp_score=fp_score)
        return ev, probe

    def test_hot_fp_boost_clears_streak_with_one_poll(self):
        ev, probe = self._run(streak=1, fp_score=_HOT_FP)
        self.assertIsNotNone(ev)
        self.assertTrue(probe.get("fp_boost"))
        self.assertTrue(ev["evidence"]["fp_boost"])
        self.assertEqual(ev["evidence"]["fingerprint"]["verdict"], "HOT")

    def test_no_fp_still_needs_two_polls(self):
        ev, probe = self._run(streak=1, fp_score=None)
        self.assertIsNone(ev)
        self.assertEqual(probe["blocked_by"], "streak")

    def test_cold_fp_gives_no_boost(self):
        cold = dict(_HOT_FP, verdict="COLD", composite=20.0)
        ev, probe = self._run(streak=1, fp_score=cold)
        self.assertIsNone(ev)
        self.assertFalse(probe.get("fp_boost"))

    def test_boost_disabled_by_config(self):
        cfg = _lowcap_cfg(lowcap_fp_boost_enabled=False)
        ev, probe = self._run(streak=1, fp_score=_HOT_FP, cfg=cfg)
        self.assertIsNone(ev)
        self.assertEqual(probe["blocked_by"], "streak")

    def test_fingerprint_evidence_on_card_note(self):
        ev, _ = self._run(streak=1, fp_score=_HOT_FP)
        self.assertIn("Fingerprints:", ev["note"])
        self.assertIn("4.2x", ev["note"])


if __name__ == "__main__":
    unittest.main()
