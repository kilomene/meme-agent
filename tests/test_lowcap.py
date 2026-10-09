"""Tests for the low-cap runner hunter (daemon/launch_scanner/lowcap.py).

The hunter drops a CA only when ALL of these hold:
  * mcap at/below the low-cap ceiling,
  * run from the sub-ceiling baseline inside [70, 100] %,
  * demand evidence (rising polls, 5m volume, liquidity, buys),
  * the fast quality gate passes (known safety report, no hard fails,
    min score) — unknown/failed coins NEVER drop.
"""

import unittest
from types import SimpleNamespace

from agent.events import bus as event_bus
from daemon.launch_scanner import lowcap
from daemon.launch_scanner import LaunchScanner
from daemon.telegram import alerts as tg_alerts


def _cfg(**kw):
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
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _stats(mcap, vol=5000.0, liq=20000.0, buys=8, sells=3):
    return {"usd_market_cap": mcap, "price_usd": 0.001,
            "usd_liquidity": liq, "vol_m5": vol,
            "buys_m5": buys, "sells_m5": sells,
            "symbol": "TST", "url": "http://x"}


def _verdict_pass(expected_mint="MINT"):
    def fn(c):
        assert c["mint"] == expected_mint, c
        return True, "quality-pass score 62", {"score": 62}
    return fn


def _verdict_fail():
    def fn(c):
        return False, "safety-fail: honeypot", {"score": 5}
    return fn


def _verdict_unknown():
    def fn(c):
        return False, "safety-unknown", {"unknown": True}
    return fn


class LowcapHunterTests(unittest.TestCase):
    def _drive(self, mcaps, cfg=None, verdict=None, streak=2, **stat_kw):
        h = lowcap.LowcapHunter(cfg or _cfg())
        tracked = {}
        verdict = verdict or _verdict_pass()
        events = []
        now = 1000.0
        for m in mcaps:
            ev = h.update(tracked, mint="MINT", symbol="TST", name="Test",
                          chain="solana", source="dexscreener-boost",
                          url="http://x", stats=_stats(m, **stat_kw),
                          rising_streak=streak, now=now, verdict_fn=verdict)
            now += 30.0
            if ev:
                events.append(ev)
        return h, tracked, events

    def test_in_band_alert(self):
        _, tracked, events = self._drive([100_000, 120_000, 175_000])
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["event"], "launch.lowcap_runner")
        self.assertEqual(ev["tier"], "lowcap")
        self.assertFalse(ev["confirmed"])
        self.assertEqual(ev["run_pct"], 75.0)
        self.assertEqual(ev["base_mcap"], 100_000)
        self.assertEqual(ev["usd_market_cap"], 175_000)
        self.assertEqual(ev["safety_score"], 62)
        self.assertTrue(tracked["MINT"]["alerted"])

    def test_band_edges_inclusive(self):
        _, _, events = self._drive([100_000, 170_000])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["run_pct"], 70.0)
        _, _, events = self._drive([100_000, 200_000])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["run_pct"], 100.0)

    def test_below_band_stays_silent(self):
        _, tracked, events = self._drive([100_000, 150_000])
        self.assertEqual(events, [])
        self.assertIn("MINT", tracked)  # still being watched

    def test_above_band_stands_down(self):
        _, tracked, events = self._drive([100_000, 230_000, 240_000])
        self.assertEqual(events, [])
        self.assertTrue(tracked["MINT"]["alerted"])  # past window: done

    def test_above_ceiling_never_tracked(self):
        _, tracked, events = self._drive([600_000, 900_000])
        self.assertEqual(events, [])
        self.assertEqual(tracked, {})

    def test_leaves_ceiling_stands_down(self):
        _, tracked, events = self._drive([400_000, 700_000, 650_000])
        self.assertEqual(events, [])
        self.assertTrue(tracked["MINT"]["alerted"])

    def test_quality_fail_held_silently(self):
        _, tracked, events = self._drive([100_000, 180_000],
                                         verdict=_verdict_fail())
        self.assertEqual(events, [])
        self.assertFalse(tracked["MINT"]["alerted"])  # held, still tracked

    def test_quality_unknown_held_silently(self):
        _, tracked, events = self._drive([100_000, 180_000],
                                         verdict=_verdict_unknown())
        self.assertEqual(events, [])

    def test_held_coin_can_alert_later(self):
        h = lowcap.LowcapHunter(_cfg())
        tracked = {}
        now = 1000.0
        kw = dict(mint="MINT", symbol="TST", name="T", chain="solana",
                  source="s", url="u", rising_streak=2, verdict_fn=_verdict_fail())
        self.assertIsNone(h.update(tracked, stats=_stats(100_000), now=now, **kw))
        now += 30
        self.assertIsNone(h.update(tracked, stats=_stats(180_000), now=now, **kw))
        now += 30
        kw["verdict_fn"] = _verdict_pass()
        ev = h.update(tracked, stats=_stats(185_000), now=now, **kw)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["run_pct"], 85.0)

    def test_rising_streak_required(self):
        _, _, events = self._drive([100_000, 180_000], streak=1)
        self.assertEqual(events, [])
        _, _, events = self._drive([100_000, 180_000], streak=2)
        self.assertEqual(len(events), 1)

    def test_volume_gate(self):
        _, _, events = self._drive([100_000, 180_000], vol=100.0)
        self.assertEqual(events, [])

    def test_liquidity_gate(self):
        _, _, events = self._drive([100_000, 180_000], liq=100.0)
        self.assertEqual(events, [])

    def test_buys_gate(self):
        _, _, events = self._drive([100_000, 180_000], buys=1)
        self.assertEqual(events, [])

    def test_buy_sell_ratio_gate(self):
        _, _, events = self._drive([100_000, 180_000], buys=4, sells=10)
        self.assertEqual(events, [])

    def test_death_drops_tracking(self):
        _, tracked, events = self._drive([100_000, 150_000, 50_000])
        self.assertEqual(events, [])
        self.assertNotIn("MINT", tracked)

    def test_window_expiry_drops_tracking(self):
        h = lowcap.LowcapHunter(_cfg(lowcap_window_minutes=30.0))
        tracked = {}
        kw = dict(mint="MINT", symbol="T", name="T", chain="solana",
                  source="s", url="u", rising_streak=2,
                  verdict_fn=_verdict_pass())
        self.assertIsNone(h.update(tracked, stats=_stats(100_000),
                                   now=1000.0, **kw))
        # 31 minutes later: expired, no alert, entry gone.
        self.assertIsNone(h.update(tracked, stats=_stats(180_000),
                                   now=1000.0 + 31 * 60, **kw))
        self.assertNotIn("MINT", tracked)

    def test_one_alert_per_mint(self):
        _, _, events = self._drive([100_000, 175_000, 180_000, 190_000])
        self.assertEqual(len(events), 1)

    def test_disabled(self):
        _, tracked, events = self._drive([100_000, 180_000],
                                         cfg=_cfg(lowcap_enabled=False))
        self.assertEqual(events, [])
        self.assertEqual(tracked, {})

    def test_zero_mcap_ignored(self):
        _, tracked, events = self._drive([0, 0, 180_000])
        self.assertEqual(events, [])
        # baseline taken at the first positive sub-ceiling sighting
        self.assertEqual(tracked["MINT"]["base"], 180_000)

    def test_no_stats_no_crash(self):
        h = lowcap.LowcapHunter(_cfg())
        self.assertIsNone(h.update({}, mint="MINT", symbol="T", name="T",
                                   chain="solana", source="s", url="u",
                                   stats=None, rising_streak=2, now=1.0,
                                   verdict_fn=_verdict_pass()))

    def test_prune_caps_table(self):
        h = lowcap.LowcapHunter(_cfg())
        now = 100_000.0
        tracked = {"M%d" % i: {"base": 1000.0, "peak": 1000.0,
                               "base_ts": now - float(i), "alerted": False}
                   for i in range(600)}
        h.prune(tracked, now=now)
        self.assertEqual(len(tracked), lowcap.MAX_TRACKED)

    def test_prune_drops_expired(self):
        h = lowcap.LowcapHunter(_cfg(lowcap_window_minutes=30.0))
        tracked = {"OLD": {"base": 1000.0, "peak": 1000.0,
                           "base_ts": 0.0, "alerted": False}}
        h.prune(tracked, now=10_000.0)
        self.assertEqual(tracked, {})


class LowcapVerdictTests(unittest.TestCase):
    def test_flash_disabled_is_fail_closed(self):
        degen = SimpleNamespace(early_safety_flash=False)
        allow, reason, _ = LaunchScanner._lowcap_verdict(
            degen, {"mint": "MINT", "chain": "solana"})
        self.assertFalse(allow)
        self.assertIn("flash-disabled", reason)


class LowcapEventTests(unittest.TestCase):
    def _event(self):
        return {
            "event": "launch.lowcap_runner",
            "tier": "lowcap",
            "confirmed": False,
            "source": "dexscreener-boost",
            "chain": "solana",
            "mint": "MINT",
            "symbol": "TST",
            "name": "Test",
            "url": "http://x",
            "usd_market_cap": 175_000,
            "base_mcap": 100_000,
            "run_pct": 75.0,
            "age_minutes": 3.0,
            "safety_score": 62,
            "evidence": {"rising_polls": 2, "vol_m5": 5000.0,
                         "liquidity": 20000.0, "buys_m5": 8, "sells_m5": 3},
            "note": "n",
        }

    def test_schema_accepts_event(self):
        event_bus.validate_event(self._event())  # must not raise

    def test_schema_rejects_unknown_field(self):
        ev = self._event()
        ev["bogus"] = 1
        with self.assertRaises(ValueError):
            event_bus.validate_event(ev)

    def test_telegram_card(self):
        text = tg_alerts.format_alert(self._event())
        self.assertIn("LOWCAP RUNNER", text)
        self.assertIn("CA: MINT", text)
        self.assertIn("+75%", text)
        self.assertIn("UNCONFIRMED", text)

    def test_copy_ca_button(self):
        markup = tg_alerts.format_alert_markup(self._event())
        self.assertIsNotNone(markup)
        btn = markup["inline_keyboard"][0][0]
        self.assertEqual(btn["copy_text"]["text"], "MINT")


class LowcapProbeTests(unittest.TestCase):
    """The probe reports exactly which gate holds each tracked coin."""

    def _update(self, h, tracked, mcap, streak=2, verdict=None,
                now=1000.0, stats_kw=None):
        probe: dict = {}
        ev = h.update(tracked, mint="MINT", symbol="TST", name="Test",
                      chain="solana", source="dexscreener-boost",
                      url="http://x",
                      stats=_stats(mcap, **(stats_kw or {})),
                      rising_streak=streak, now=now,
                      verdict_fn=verdict or _verdict_pass(), probe=probe)
        return ev, probe

    def test_probe_baseline(self):
        h = lowcap.LowcapHunter(_cfg())
        ev, probe = self._update(h, {}, 100000.0)
        self.assertIsNone(ev)
        self.assertTrue(probe["baselined"])
        self.assertFalse(probe["fired"])
        self.assertIsNone(probe["blocked_by"])

    def test_probe_waiting_run(self):
        h = lowcap.LowcapHunter(_cfg())
        tracked: dict = {}
        self._update(h, tracked, 100000.0)
        ev, probe = self._update(h, tracked, 120000.0, now=1030.0)
        self.assertIsNone(ev)
        self.assertEqual(probe["blocked_by"], "waiting_run")

    def test_probe_streak(self):
        h = lowcap.LowcapHunter(_cfg())
        tracked: dict = {}
        self._update(h, tracked, 100000.0)
        ev, probe = self._update(h, tracked, 180000.0, streak=0, now=1030.0)
        self.assertIsNone(ev)
        self.assertEqual(probe["blocked_by"], "streak")

    def test_probe_safety(self):
        h = lowcap.LowcapHunter(_cfg())
        tracked: dict = {}
        self._update(h, tracked, 100000.0)
        ev, probe = self._update(h, tracked, 180000.0, now=1030.0,
                                 verdict=_verdict_fail())
        self.assertIsNone(ev)
        self.assertEqual(probe["blocked_by"], "safety")

    def test_probe_fired(self):
        h = lowcap.LowcapHunter(_cfg())
        tracked: dict = {}
        self._update(h, tracked, 100000.0)
        ev, probe = self._update(h, tracked, 175000.0, now=1030.0)
        self.assertIsNotNone(ev)
        self.assertTrue(probe["fired"])
        self.assertIsNone(probe["blocked_by"])

    def test_probe_no_stats(self):
        h = lowcap.LowcapHunter(_cfg())
        probe: dict = {}
        ev = h.update({}, mint="MINT", symbol="T", name="t", chain="solana",
                      source="s", url="u", stats=None, rising_streak=0,
                      now=1000.0, verdict_fn=_verdict_pass(), probe=probe)
        self.assertIsNone(ev)
        self.assertEqual(probe["blocked_by"], "no_stats")

    def test_probe_optional_backwards_compatible(self):
        # Old callers pass no probe — behavior unchanged.
        h = lowcap.LowcapHunter(_cfg())
        tracked: dict = {}
        ev = h.update(tracked, mint="MINT", symbol="TST", name="Test",
                      chain="solana", source="s", url="u",
                      stats=_stats(100000.0), rising_streak=2, now=1000.0,
                      verdict_fn=_verdict_pass())
        self.assertIsNone(ev)
        self.assertIn("MINT", tracked)


if __name__ == "__main__":
    unittest.main()
