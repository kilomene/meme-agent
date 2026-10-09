"""Tests for the digest-unvetted lane (user decision 2026-09-28).

Safety-unknown coins (the screen ran but produced NO report — NOT
known-bad) join the hourly digest with a prominent UNVETTED label.
Never instant alerts, never paper entries. Hard fails (honeypot /
can't-sell / high sell tax / insider clusters), safety errors, and
below-bar scores stay held silently.
"""

import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from daemon.launch_scanner import LaunchScanner
from daemon.launch_scanner import momentum
from daemon.telegram import alerts


def _degen(**kw):
    base = dict(enabled=True, poll_interval_seconds=30, entry_sol=0.05,
                max_positions=5, min_usd_market_cap=10000.0,
                max_age_minutes=10.0, velocity_multiplier=3.0,
                velocity_window_minutes=10.0, stop_loss_pct=30.0,
                take_profit_pct=200.0, max_hold_hours=4.0, auto_enter=True,
                early_safety_flash=True, early_flash_timeout_s=4.0,
                early_min_score=30.0,
                early_alert_max_per_window=10,
                early_alert_window_seconds=300.0,
                feedback_enabled=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _pump_coin(**kw):
    c = {"mint": "MINT1", "symbol": "TKN", "name": "Token",
         "chain": "solana", "source": "pumpfun",
         "created_ts": time.time(), "usd_market_cap": 12000.0,
         "extra": {"complete": False, "reply_count": 0, "banned": False},
         "url": "https://pump.fun/coin/MINT1"}
    c.update(kw)
    return c


def _pool(**kw):
    p = {"pool_address": "pool1", "mint": "MINT9", "symbol": "FWOG",
         "name": "Fwog", "chain": "solana", "chg_24h_pct": 25.0,
         "chg_1h_pct": 5.0, "vol_24h_usd": 200000.0, "liq_usd": 80000.0,
         "usd_market_cap": 5000000.0,
         "url": "https://www.geckoterminal.com/solana/pools/pool1"}
    p.update(kw)
    return p


_UNKNOWN = {"passed": True, "unknown": True}
_HONEYPOT = {"passed": False, "unknown": False, "fails": ["honeypot"]}
_LOW = {"passed": True, "unknown": False, "fails": [], "score": 12}


class LaunchUnvettedTest(unittest.TestCase):
    def setUp(self):
        self.sc = LaunchScanner.__new__(LaunchScanner)

    def _state(self):
        return {"seen": {}, "degen_positions": {}, "candidates": {}}

    def _consider(self, report, degen_kw=None):
        from agent import events as event_bus
        from daemon.launch_scanner import safety
        state = self._state()
        cfg = SimpleNamespace(degen=_degen(**(degen_kw or {})))
        c = _pump_coin()
        with patch.object(event_bus, "publish") as pub, \
                patch.object(safety, "check_token",
                             return_value=report):
            self.sc._consider(cfg, None, state, c)
        return state, pub

    def test_unknown_queued_to_digest_when_knob_on(self):
        state, pub = self._consider(
            _UNKNOWN, {"digest_include_unknown_safety": True})
        pub.assert_not_called()  # NEVER an instant alert
        digest = state.get("early_digest") or []
        self.assertEqual(len(digest), 1)
        self.assertTrue(digest[0].get("unvetted"))
        self.assertIsNone(digest[0].get("safety_score"))
        # Still tracked as a candidate; no paper position opened.
        self.assertIn("MINT1", state["candidates"])
        self.assertEqual(state["degen_positions"], {})

    def test_unknown_silent_when_knob_off(self):
        state, pub = self._consider(
            _UNKNOWN, {"digest_include_unknown_safety": False})
        pub.assert_not_called()
        self.assertEqual(state.get("early_digest") or [], [])
        self.assertIn("MINT1", state["candidates"])

    def test_hard_fail_never_reaches_digest(self):
        state, pub = self._consider(
            _HONEYPOT, {"digest_include_unknown_safety": True})
        pub.assert_not_called()
        self.assertEqual(state.get("early_digest") or [], [])

    def test_below_bar_score_never_reaches_digest(self):
        state, pub = self._consider(
            _LOW, {"digest_include_unknown_safety": True})
        pub.assert_not_called()
        self.assertEqual(state.get("early_digest") or [], [])

    def test_safety_error_never_reaches_digest(self):
        from agent import events as event_bus
        from daemon.launch_scanner import safety
        state = self._state()
        cfg = SimpleNamespace(
            degen=_degen(digest_include_unknown_safety=True))
        with patch.object(event_bus, "publish") as pub, \
                patch.object(safety, "check_token",
                             side_effect=RuntimeError("boom")):
            self.sc._consider(cfg, None, state, _pump_coin())
        pub.assert_not_called()
        self.assertEqual(state.get("early_digest") or [], [])

    def test_unvetted_never_staged_instant_even_at_zero_threshold(self):
        # The sniper rank is best-effort; even a "sniper" tier result on
        # an unknown coin must NOT stage an instant card.
        state, pub = self._consider(
            _UNKNOWN, {"digest_include_unknown_safety": True,
                       "sniper_tier_threshold": 0.0})
        pub.assert_not_called()
        digest = state.get("early_digest") or []
        self.assertEqual(len(digest), 1)
        self.assertTrue(digest[0].get("unvetted"))

    def test_unvetted_digest_item_carries_sniper_score(self):
        state, _ = self._consider(
            _UNKNOWN, {"digest_include_unknown_safety": True})
        item = (state.get("early_digest") or [])[0]
        # Ranked for browse value (None only if ranking blew up).
        self.assertIn("sniper_score", item)


class MomentumUnvettedTest(unittest.TestCase):
    def _handle(self, reason, report, degen_kw=None):
        cfg = SimpleNamespace(degen=_degen(**(degen_kw or {})))
        sc = LaunchScanner.__new__(LaunchScanner)
        state = {}
        tracked = {}
        scanner = momentum.MomentumScanner()
        r = _pool()

        class FakeBus:
            def __init__(self):
                self.published = []

            def publish(self, e):
                self.published.append(e)

        bus = FakeBus()
        with patch.object(LaunchScanner, "_instant_verdict") as iv, \
             patch.object(LaunchScanner, "_sentiment_track",
                          return_value=None):
            iv.return_value = (False, reason, report)
            sc._momentum_handle(cfg, None, state, tracked, scanner, r,
                                1000.0, bus)
        return state, bus

    def test_momentum_unknown_queued_to_digest(self):
        state, bus = self._handle("safety-unknown", _UNKNOWN,
                                  {"digest_include_unknown_safety": True})
        self.assertEqual(bus.published, [])  # no instant momentum card
        digest = state.get("early_digest") or []
        self.assertEqual(len(digest), 1)
        self.assertTrue(digest[0].get("unvetted"))
        self.assertIn("momentum", digest[0].get("tier", ""))

    def test_momentum_unknown_silent_when_knob_off(self):
        state, bus = self._handle("safety-unknown", _UNKNOWN,
                                  {"digest_include_unknown_safety": False})
        self.assertEqual(bus.published, [])
        self.assertEqual(state.get("early_digest") or [], [])

    def test_momentum_hard_fail_never_reaches_digest(self):
        state, bus = self._handle("safety-fail: honeypot", _HONEYPOT,
                                  {"digest_include_unknown_safety": True})
        self.assertEqual(bus.published, [])
        self.assertEqual(state.get("early_digest") or [], [])


class FlushHonestyTest(unittest.TestCase):
    def test_flush_counts_unvetted_and_labels_note(self):
        from agent import events as event_bus
        sc = LaunchScanner.__new__(LaunchScanner)
        cfg = SimpleNamespace(degen=_degen())
        state = {
            "early_digest": [
                {"mint": "A", "symbol": "AAA", "tier": "standard",
                 "safety_score": 80, "ts": time.time() - 10},
                {"mint": "B", "symbol": "BBB", "tier": "standard",
                 "safety_score": None, "unvetted": True,
                 "ts": time.time() - 5},
            ],
            "early_window": {"start": time.time() - 99999, "count": 0},
        }
        with patch.object(event_bus, "publish") as pub:
            sc._flush_early_digest(cfg, state)
        self.assertEqual(pub.call_count, 1)
        evt = pub.call_args[0][0]
        self.assertEqual(evt["event"], "launch.early_digest")
        self.assertEqual(evt["unvetted_count"], 1)
        self.assertIn("UNVETTED", evt["note"])
        # The flag survives into the rendered items.
        items = (evt["items_safe"] or []) + (evt["items_fresh"] or [])
        unv = [i for i in items if i.get("mint") == "B"]
        self.assertTrue(unv and unv[0].get("unvetted"))
        # Queue drained after flush.
        self.assertEqual(state["early_digest"], [])

    def test_flush_note_clean_when_no_unvetted(self):
        from agent import events as event_bus
        sc = LaunchScanner.__new__(LaunchScanner)
        cfg = SimpleNamespace(degen=_degen())
        state = {
            "early_digest": [
                {"mint": "A", "symbol": "AAA", "tier": "standard",
                 "safety_score": 80, "ts": time.time() - 10},
            ],
            "early_window": {"start": time.time() - 99999, "count": 0},
        }
        with patch.object(event_bus, "publish") as pub:
            sc._flush_early_digest(cfg, state)
        evt = pub.call_args[0][0]
        self.assertEqual(evt["unvetted_count"], 0)
        self.assertIn("quality gate", evt["note"])


class DigestRenderTest(unittest.TestCase):
    def test_item_line_marks_unvetted(self):
        line = alerts._digest_item_line(
            {"symbol": "TKN", "mint": "MINT1", "tier": "standard",
             "safety_score": None, "unvetted": True,
             "narratives": ["other"]})
        self.assertIn("UNVETTED", line)
        self.assertNotIn("[score", line)
        self.assertIn("MINT1", line)

    def test_item_line_unchanged_for_vetted(self):
        line = alerts._digest_item_line(
            {"symbol": "TKN", "mint": "MINT1", "tier": "standard",
             "safety_score": 80, "narratives": ["other"]})
        self.assertIn("[score 80/100]", line)
        self.assertNotIn("UNVETTED", line)

    def test_digest_header_counts_unvetted(self):
        card = alerts._launch_early_digest({
            "count": 3, "unvetted_count": 2,
            "items_safe": [], "items_fresh": [],
            "note": "x",
        })
        self.assertIn("UNVETTED", card)
        self.assertIn("2", card)

    def test_digest_header_clean_without_unvetted(self):
        card = alerts._launch_early_digest({
            "count": 1, "unvetted_count": 0,
            "items_safe": [], "items_fresh": [],
            "note": "x",
        })
        self.assertNotIn("UNVETTED", card)


class SchemaAndConfigTest(unittest.TestCase):
    def test_event_schema_accepts_unvetted_count(self):
        from agent.events.bus import validate_event
        evt = validate_event({
            "event": "launch.early_digest",
            "count": 2,
            "unvetted_count": 1,
            "items_safe": [], "items_fresh": [],
            "note": "n",
        })
        self.assertEqual(evt["unvetted_count"], 1)

    def test_knob_defaults_true(self):
        from config.config import DegenConfig
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEGEN_DIGEST_INCLUDE_UNKNOWN_SAFETY", None)
            self.assertTrue(
                DegenConfig().digest_include_unknown_safety)

    def test_knob_env_override(self):
        from config.config import DegenConfig
        with patch.dict(os.environ,
                        {"DEGEN_DIGEST_INCLUDE_UNKNOWN_SAFETY": "0"},
                        clear=False):
            self.assertFalse(
                DegenConfig().digest_include_unknown_safety)


if __name__ == "__main__":
    unittest.main()
