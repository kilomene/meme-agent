"""Tests for the dead-pool exit path and the position write-through.

Two coupled bugs (2026-10-05):
1. `_manage_exits` skipped any position whose quote was missing
   (`continue` before every exit check) — a dead pool could never
   close. Now: when the market feed is healthy for other coins in the
   same cycle but one pool is specifically dead, the time stop fires
   at ~0 ("dead_pool") instead of skipping forever.
2. `run_once()` reloaded daemon state from disk every cycle while the
   full save was throttled to 300s — position keys added/removed in a
   non-save cycle were lost on the next reload, orphaning broker
   tickets (never closable) and resurrecting closed ones (zombie close
   retries every cycle). degen_positions key changes now trigger an
   immediate save, like the daily-slot flags.
"""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from daemon.launch_scanner import LaunchScanner
from daemon.launch_scanner.feedback import EXIT_REASONS


def _degen(**kw):
    d = dict(max_hold_hours=4.0, entry_sol=0.05,
             trail_trigger_pct=50.0, trail_drawdown_pct=30.0,
             leg_a_trail_trigger_pct=20.0, leg_a_trail_drawdown_pct=15.0)
    d.update(kw)
    return SimpleNamespace(**d)


def _pos(ticket=7, symbol="TKN", age_h=5.0, **kw):
    p = dict(ticket=ticket, symbol=symbol, mint="MINT1",
             entry_price_sol=0.001, entry_ts=time.time() - age_h * 3600,
             sl=0.0007, tp=0.003, volume_sol=0.05, leg="A")
    p.update(kw)
    return p


class _Adapter:
    """price_map: symbol -> price (None = dead quote)."""

    def __init__(self, price_map):
        self.price_map = price_map
        self.closed_at = []  # (ticket, price, reason)

    def _price_sol_quiet(self, symbol):
        return self.price_map.get(symbol)

    def close_position(self, ticket):
        raise AssertionError("should not market-close in dead-pool tests")

    def close_position_at(self, ticket, price_sol, reason=""):
        self.closed_at.append((ticket, price_sol, reason))
        return price_sol


class DeadPoolTests(unittest.TestCase):
    def setUp(self):
        self.sc = LaunchScanner()

    def _run(self, degen, adapter, state):
        published = []
        import agent.events as events_pkg
        with patch.object(events_pkg, "publish",
                          side_effect=published.append):
            self.sc._manage_exits(SimpleNamespace(degen=degen),
                                  adapter, state)
        return published

    def test_dead_pool_closes_at_zero_when_market_alive(self):
        d = _degen(max_hold_hours=4.0)
        state = {"degen_positions": {
            "MINT1": _pos(age_h=5.0),                       # dead, ancient
            "MINT2": _pos(ticket=8, symbol="OK", mint="MINT2",
                          age_h=0.1),                       # alive, young
        }}
        ad = _Adapter({"TKN": None, "OK": 0.0012})
        published = self._run(d, ad, state)
        self.assertEqual(ad.closed_at, [(7, 0.0, "dead_pool")])
        self.assertNotIn("MINT1", state["degen_positions"])
        self.assertIn("MINT2", state["degen_positions"])  # untouched
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["reason"], "degen_dead_pool")
        # Total loss recorded honestly.
        self.assertAlmostEqual(published[0]["profit"], -0.05, places=9)

    def test_market_outage_skips_everything(self):
        d = _degen(max_hold_hours=4.0)
        state = {"degen_positions": {"MINT1": _pos(age_h=50.0)}}
        ad = _Adapter({"TKN": None})  # nothing prices -> outage, not death
        published = self._run(d, ad, state)
        self.assertEqual(ad.closed_at, [])
        self.assertEqual(published, [])
        self.assertIn("MINT1", state["degen_positions"])

    def test_young_dead_position_waits(self):
        d = _degen(max_hold_hours=4.0)
        state = {"degen_positions": {
            "MINT1": _pos(age_h=1.0),                        # dead but young
            "MINT2": _pos(ticket=8, symbol="OK", mint="MINT2", age_h=0.1),
        }}
        ad = _Adapter({"TKN": None, "OK": 0.0012})
        published = self._run(d, ad, state)
        self.assertEqual(ad.closed_at, [])
        self.assertEqual(published, [])
        self.assertIn("MINT1", state["degen_positions"])

    def test_alive_positions_still_exit_normally(self):
        d = _degen(max_hold_hours=4.0)
        state = {"degen_positions": {
            "MINT2": _pos(ticket=8, symbol="OK", mint="MINT2",
                          age_h=5.0),                        # alive, ancient
        }}

        class _A(_Adapter):
            def close_position(self, ticket):
                self.closed_at.append((ticket, 0.001, "market"))
                return 0.001

        ad = _A({"OK": 0.001})
        published = self._run(d, ad, state)
        self.assertEqual(len(ad.closed_at), 1)
        self.assertEqual(ad.closed_at[0][2], "market")  # not dead_pool
        self.assertIn("max_hold", published[0]["reason"])

    def test_exit_reasons_include_dead_pool(self):
        self.assertIn("dead_pool", EXIT_REASONS)


if __name__ == "__main__":
    unittest.main()
