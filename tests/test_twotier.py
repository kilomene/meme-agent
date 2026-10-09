"""Two-tier exit legs: entry splits into A (classic) + B (runner).

Leg A keeps the classic exits (stop 30% / TP 3x / 4h hold / trail).
Leg B skips the fixed TP and holds up to tier_b_max_hold_hours (24h)
with the same stop and trail. Tests are hermetic: no HOME reads.
"""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from daemon.launch_scanner import LaunchScanner, count_base_positions


def _degen(**kw):
    base = dict(enabled=True, entry_sol=0.05, max_positions=5,
                stop_loss_pct=30.0, take_profit_pct=200.0,
                max_hold_hours=4.0, auto_enter=True,
                trail_trigger_pct=50.0, trail_drawdown_pct=30.0,
                two_tier_enabled=True, tier_b_max_hold_hours=24.0)
    base.update(kw)
    return SimpleNamespace(**base)


class _Adapter:
    """Paper adapter double: two submits -> two tickets; price feed."""

    def __init__(self, price=0.001):
        self.price = price
        self.submits = []
        self._ticket = 1000

    def _price_sol_quiet(self, symbol):
        return self.price

    def submit_order(self, req):
        self._ticket += 1
        self.submits.append(req)
        return SimpleNamespace(ticket=self._ticket, price=self.price)

    def close_position(self, ticket):
        return self.price


def _enter(sc, d, state=None, price=0.001):
    state = state if state is not None else {"degen_positions": {}}
    card = {"entry": None, "note": "", "tier": "early",
            "source": "pumpfun"}
    ad = _Adapter(price=price)
    ok = sc._paper_enter(None, ad, state, card, d, "MINT1", "TKN")
    return ok, state, card, ad


class TwoTierEntryTest(unittest.TestCase):
    def setUp(self):
        self.sc = LaunchScanner.__new__(LaunchScanner)

    def test_entry_creates_two_half_legs(self):
        ok, state, card, ad = _enter(self.sc, _degen())
        self.assertTrue(ok)
        pos = state["degen_positions"]
        self.assertIn("MINT1", pos)
        self.assertIn("MINT1|B", pos)
        a, b = pos["MINT1"], pos["MINT1|B"]
        self.assertEqual(a["leg"], "A")
        self.assertEqual(b["leg"], "B")
        # Half size each, same total as before.
        self.assertAlmostEqual(a["volume_sol"], 0.025)
        self.assertAlmostEqual(b["volume_sol"], 0.025)
        self.assertAlmostEqual(a["volume_sol"] + b["volume_sol"], 0.05)
        # Two broker orders, distinct idempotency keys.
        self.assertEqual(len(ad.submits), 2)
        keys = [r.idempotency_key for r in ad.submits]
        self.assertEqual(len(set(keys)), 2)
        self.assertTrue(all(abs(r.volume - 0.025) < 1e-12
                            for r in ad.submits))
        # Card keeps total volume + both tickets.
        self.assertAlmostEqual(card["entry"]["volume_sol"], 0.05)
        self.assertIsNotNone(card["entry"]["ticket_b"])

    def test_leg_b_has_no_tp_and_longer_hold(self):
        ok, state, card, ad = _enter(self.sc, _degen())
        a, b = state["degen_positions"]["MINT1"], \
            state["degen_positions"]["MINT1|B"]
        self.assertIsNotNone(a["tp"])
        self.assertIsNone(b["tp"])
        self.assertAlmostEqual(a["max_hold_hours"], 4.0)
        self.assertAlmostEqual(b["max_hold_hours"], 24.0)
        # Same disaster stop on both legs.
        self.assertAlmostEqual(a["sl"], b["sl"])
        # Same entry intel rides along on both legs.
        self.assertEqual(a["mint"], b["mint"])
        self.assertEqual(a["symbol"], b["symbol"])

    def test_disabled_is_single_full_position(self):
        ok, state, card, ad = _enter(self.sc,
                                     _degen(two_tier_enabled=False))
        self.assertTrue(ok)
        pos = state["degen_positions"]
        self.assertEqual(list(pos.keys()), ["MINT1"])
        self.assertIsNone(pos["MINT1"]["leg"])
        self.assertAlmostEqual(pos["MINT1"]["volume_sol"], 0.05)
        self.assertEqual(len(ad.submits), 1)
        self.assertIsNone(card["entry"]["ticket_b"])

    def test_leg_b_failure_leaves_leg_a(self):
        sc = self.sc
        state = {"degen_positions": {}}
        card = {"entry": None, "note": "", "tier": "early",
                "source": "pumpfun"}
        ad = _Adapter(price=0.001)
        real_submit = ad.submit_order
        calls = []

        def flaky(req):
            calls.append(req)
            if len(calls) == 2:
                raise RuntimeError("broker exploded")
            return real_submit(req)

        ad.submit_order = flaky
        ok = sc._paper_enter(None, ad, state, card, _degen(),
                             "MINT1", "TKN")
        self.assertTrue(ok)  # leg A still live
        self.assertIn("MINT1", state["degen_positions"])
        self.assertNotIn("MINT1|B", state["degen_positions"])
        self.assertIn("runner leg failed", card["note"])


class TwoTierExitsTest(unittest.TestCase):
    def setUp(self):
        self.sc = LaunchScanner.__new__(LaunchScanner)

    def _legs(self, price_now, age_h_a=0.1, age_h_b=0.1):
        now = time.time()
        state = {"degen_positions": {
            "MINT1": {"ticket": 1, "symbol": "TKN", "mint": "MINT1",
                      "entry_price_sol": 0.001, "peak_price_sol": 0.001,
                      "entry_ts": now - age_h_a * 3600,
                      "sl": 0.0007, "tp": 0.003, "leg": "A",
                      "volume_sol": 0.025, "max_hold_hours": 4.0},
            "MINT1|B": {"ticket": 2, "symbol": "TKN", "mint": "MINT1",
                        "entry_price_sol": 0.001, "peak_price_sol": 0.001,
                        "entry_ts": now - age_h_b * 3600,
                        "sl": 0.0007, "tp": None, "leg": "B",
                        "volume_sol": 0.025, "max_hold_hours": 24.0},
        }}
        return state

    def _run(self, d, state, price):
        published = []
        import agent.events as events_pkg
        with patch.object(events_pkg, "publish",
                          side_effect=published.append):
            self.sc._manage_exits(SimpleNamespace(degen=d),
                                   _Adapter(price=price), state)
        return published

    def test_leg_a_tps_while_leg_b_rides(self):
        # 3.5x: leg A must TP; leg B has no TP so it stays open.
        state = self._legs(price_now=0.0035)
        pub = self._run(_degen(), state, 0.0035)
        self.assertEqual(len(pub), 1)
        self.assertEqual(pub[0]["leg"], "A")
        self.assertIn("take_profit", pub[0]["reason"])
        self.assertIn("MINT1|B", state["degen_positions"])
        self.assertNotIn("MINT1", state["degen_positions"])

    def test_leg_b_max_hold_is_24h(self):
        # 5h old: leg A hits its 4h hold; leg B (24h) stays open.
        state = self._legs(price_now=0.0011, age_h_a=5.0, age_h_b=5.0)
        pub = self._run(_degen(), state, 0.0011)
        self.assertEqual(len(pub), 1)
        self.assertEqual(pub[0]["leg"], "A")
        self.assertIn("max_hold", pub[0]["reason"])
        self.assertIn("MINT1|B", state["degen_positions"])

    def test_leg_b_max_hold_fires_at_24h(self):
        state = self._legs(price_now=0.0011, age_h_a=0.1, age_h_b=25.0)
        pub = self._run(_degen(), state, 0.0011)
        reasons = {p["leg"]: p["reason"] for p in pub}
        self.assertIn("max_hold", reasons.get("B", ""))

    def test_both_legs_stop_together(self):
        state = self._legs(price_now=0.0005)
        pub = self._run(_degen(), state, 0.0005)
        self.assertEqual(len(pub), 2)
        self.assertTrue(all("stop_loss" in p["reason"] for p in pub))
        # Half-size P/L per leg.
        for p in pub:
            self.assertAlmostEqual(p["profit"], 0.025 * (0.5 - 1.0),
                                   places=9)

    def test_legacy_position_uses_global_defaults(self):
        # Pre-two-tier position: no leg/volume/max_hold keys — must
        # behave exactly like the old code.
        now = time.time()
        state = {"degen_positions": {
            "MINT9": {"ticket": 9, "symbol": "OLD", "mint": "MINT9",
                      "entry_price_sol": 0.001,
                      "entry_ts": now - 5 * 3600,
                      "sl": 0.0007, "tp": 0.003}}}
        pub = self._run(_degen(), state, 0.001)
        self.assertEqual(len(pub), 1)
        self.assertIn("max_hold", pub[0]["reason"])
        self.assertEqual(pub[0]["leg"], "A")
        self.assertAlmostEqual(pub[0]["profit"], 0.05 * (1.0 - 1.0))

    def test_trail_applies_to_leg_b(self):
        # Peak 2x then -35%: leg B trails out (no TP to save it).
        now = time.time()
        state = {"degen_positions": {
            "MINT1|B": {"ticket": 2, "symbol": "TKN", "mint": "MINT1",
                        "entry_price_sol": 0.001, "peak_price_sol": 0.002,
                        "entry_ts": now - 3600,
                        "sl": 0.0007, "tp": None, "leg": "B",
                        "volume_sol": 0.025, "max_hold_hours": 24.0},
        }}
        pub = self._run(_degen(), state, 0.0013)  # -35% off 0.002 peak
        self.assertEqual(len(pub), 1)
        self.assertIn("trail_stop", pub[0]["reason"])
        self.assertEqual(pub[0]["leg"], "B")


class CountBasePositionsTest(unittest.TestCase):
    def test_counts_pairs_once(self):
        pos = {"M1": {}, "M1|B": {}, "M2": {}, "M2|B": {}, "M3": {}}
        self.assertEqual(count_base_positions(pos), 3)

    def test_empty_and_none(self):
        self.assertEqual(count_base_positions({}), 0)
        self.assertEqual(count_base_positions(None), 0)

    def test_wave_keys_untouched(self):
        pos = {"second_wave:M1": {}, "second_wave:M1|B": {}}
        self.assertEqual(count_base_positions(pos), 1)


class TradePlanTest(unittest.TestCase):
    def test_plan_carries_two_tier(self):
        plan = LaunchScanner._trade_plan(_degen())
        self.assertTrue(plan["two_tier"])
        self.assertAlmostEqual(plan["tier_b_max_hold_h"], 24.0)
        self.assertAlmostEqual(plan["max_hold_h"], 4.0)

    def test_plan_line_renders_runner(self):
        from daemon.telegram import alerts
        plan = LaunchScanner._trade_plan(_degen())
        line = alerts._plan_line({"plan": plan,
                                  "usd_market_cap": 100000})
        self.assertIn("runner half", line)
        self.assertIn("no fixed TP", line)

    def test_close_card_labels_leg(self):
        from daemon.telegram import alerts
        txt = alerts._position_closed({"ticket": 7, "symbol": "TKN",
                                       "profit": 0.01,
                                       "reason": "degen_take_profit",
                                       "leg": "B"})
        self.assertIn("[leg B]", txt)
        txt2 = alerts._position_closed({"ticket": 7, "symbol": "TKN",
                                        "profit": 0.01,
                                        "reason": "degen_stop_loss"})
        self.assertNotIn("[leg", txt2)


if __name__ == "__main__":
    unittest.main()
