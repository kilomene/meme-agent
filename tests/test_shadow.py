"""Tests for the shadow ledger (paper-simulation of blocked runners).

The shadow ledger measures what loosening the gates would have done.
It must NEVER touch real positions, publish events, or call the broker.
"""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from daemon.launch_scanner import shadow


def _degen(**kw):
    d = {"shadow_enabled": True, "shadow_max_open": 20,
         "shadow_stop_pct": 30.0, "shadow_tp_pct": 200.0,
         "shadow_max_hold_hours": 24.0,
         "shadow_pending_timeout_minutes": 60.0}
    d.update(kw)
    return SimpleNamespace(**d)


class MaybeOpenTest(unittest.TestCase):
    def test_opens_with_valid_mcap(self):
        state = {}
        ok = shadow.maybe_open(state, _degen(), "momentum", "BRAIN",
                               "MINT1", "solana", 1500000.0,
                               "score 1 < 50")
        self.assertTrue(ok)
        pos = state["shadow_open"]["MINT1"]
        self.assertEqual(pos["entry_mcap"], 1500000.0)
        self.assertEqual(pos["symbol"], "BRAIN")

    def test_zero_mcap_opens_pending_not_rejected(self):
        # First-sight holds (logstream mints) arrive before any price feed
        # exists. The ledger must measure them via pending entries, not
        # silently skip them (the Bagwork blind spot).
        state = {}
        ok = shadow.maybe_open(state, _degen(), "launch", "Bagwork",
                               "5NhN6zz", "solana", 0, "score 1 < 50")
        self.assertTrue(ok)
        pos = state["shadow_open"]["5NhN6zz"]
        self.assertTrue(pos["pending"])
        self.assertEqual(pos["entry_mcap"], 0.0)

    def test_rejects_empty_mint(self):
        state = {}
        self.assertFalse(shadow.maybe_open(state, _degen(), "momentum",
                                           "X", "", "solana", 1000, "r"))
        self.assertEqual(state.get("shadow_open", {}), {})

    def test_pending_counts_toward_max_open_cap(self):
        state = {}
        d = _degen(shadow_max_open=1)
        self.assertTrue(shadow.maybe_open(state, d, "launch", "A", "M1",
                                          "solana", 0, "r"))
        self.assertFalse(shadow.maybe_open(state, d, "launch", "B", "M2",
                                           "solana", 1000, "r"))

    def test_no_duplicate_open_for_same_mint(self):
        state = {}
        d = _degen()
        self.assertTrue(shadow.maybe_open(state, d, "launch", "A", "M1",
                                          "solana", 1000, "r"))
        self.assertFalse(shadow.maybe_open(state, d, "launch", "A", "M1",
                                           "solana", 2000, "r"))
        self.assertEqual(len(state["shadow_open"]), 1)

    def test_respects_max_open_cap(self):
        state = {}
        d = _degen(shadow_max_open=2)
        self.assertTrue(shadow.maybe_open(state, d, "launch", "A", "M1",
                                          "solana", 1000, "r"))
        self.assertTrue(shadow.maybe_open(state, d, "launch", "B", "M2",
                                          "solana", 1000, "r"))
        self.assertFalse(shadow.maybe_open(state, d, "launch", "C", "M3",
                                           "solana", 1000, "r"))

    def test_disabled_opens_nothing(self):
        state = {}
        self.assertFalse(shadow.maybe_open(state, _degen(shadow_enabled=False),
                                           "momentum", "A", "M1", "solana",
                                           1000, "r"))
        self.assertEqual(state.get("shadow_open", {}), {})

    def test_never_raises(self):
        self.assertFalse(shadow.maybe_open(None, _degen(), "x", "y", "z",
                                           "w", 100, "r"))


class UpdateTest(unittest.TestCase):
    def _open(self, state, mcap=1000.0, age_s=0.0):
        state["shadow_last_refresh"] = time.time() - 1000.0  # force refresh
        state["shadow_open"] = {
            "M1": {"symbol": "A", "kind": "launch", "chain": "solana",
                   "entry_mcap": mcap, "last_mcap": mcap,
                   "opened_ts": time.time() - age_s,
                   "last_ts": time.time(), "reason": "score 1 < 50"},
        }

    def _run_update(self, state, last_mcap):
        d = _degen()
        with patch.object(shadow, "_refresh_mcaps") as rf:
            def _fake(open_book):
                for pos in open_book.values():
                    pos["last_mcap"] = last_mcap
            rf.side_effect = _fake
            return shadow.update(state, d)

    def test_stop_loss_closes(self):
        state = {}
        self._open(state)
        res = self._run_update(state, 699.0)  # -30.1%
        self.assertEqual(res["closed_loss"], 1)
        self.assertEqual(state["shadow_open"], {})
        closed = state["shadow_closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["outcome"], "loss")
        self.assertAlmostEqual(closed[0]["pnl_pct"], -30.1, places=1)

    def test_take_profit_closes(self):
        state = {}
        self._open(state)
        res = self._run_update(state, 3000.0)  # +200%
        self.assertEqual(res["closed_win"], 1)
        closed = state["shadow_closed"]
        self.assertEqual(closed[0]["outcome"], "win")
        self.assertAlmostEqual(closed[0]["pnl_pct"], 200.0, places=1)

    def test_timeout_closes(self):
        state = {}
        self._open(state, age_s=25 * 3600.0)
        res = self._run_update(state, 1100.0)  # +10%, no stop/TP
        self.assertEqual(res["closed_timeout"], 1)
        self.assertEqual(state["shadow_closed"][0]["outcome"], "timeout")

    def test_small_move_stays_open(self):
        state = {}
        self._open(state)
        res = self._run_update(state, 1100.0)  # +10%
        self.assertEqual(
            (res["closed_win"], res["closed_loss"], res["closed_timeout"]),
            (0, 0, 0))
        self.assertIn("M1", state["shadow_open"])

    def test_throttled_refresh(self):
        state = {}
        self._open(state)
        state["shadow_last_refresh"] = time.time()  # just refreshed
        d = _degen()
        with patch.object(shadow, "_refresh_mcaps") as rf:
            res = shadow.update(state, d)
        rf.assert_not_called()
        self.assertEqual(res["refreshed"], 0)

    def test_never_raises(self):
        self.assertEqual(shadow.update(None, _degen())["refreshed"], 0)


class PendingEntryTest(unittest.TestCase):
    """Pending entries: first-sight holds with no price yet."""

    def _pending(self, state, age_s=0.0):
        state["shadow_last_refresh"] = time.time() - 1000.0  # force refresh
        state["shadow_open"] = {
            "M1": {"symbol": "Bagwork", "kind": "launch", "chain": "solana",
                   "entry_mcap": 0.0, "last_mcap": 0.0,
                   "opened_ts": time.time() - age_s,
                   "last_ts": time.time(), "reason": "score 1 < 50",
                   "pending": True, "entry_ts": 0.0},
        }

    def test_backfills_entry_on_first_price(self):
        state = {}
        self._pending(state)
        d = _degen()
        with patch.object(shadow, "_refresh_mcaps") as rf:
            def _fake(open_book):
                for pos in open_book.values():
                    pos["last_mcap"] = 50000.0  # first indexed price
            rf.side_effect = _fake
            res = shadow.update(state, d)
        self.assertEqual(res["backfilled"], 1)
        pos = state["shadow_open"]["M1"]
        self.assertFalse(pos["pending"])
        self.assertEqual(pos["entry_mcap"], 50000.0)
        self.assertGreater(pos["entry_ts"], 0)
        # A backfilled position then behaves like a normal one.
        self.assertEqual(state.get("shadow_closed", []), [])

    def test_pending_ignores_stop_tp_without_price(self):
        state = {}
        self._pending(state)
        d = _degen()
        with patch.object(shadow, "_refresh_mcaps"):
            res = shadow.update(state, d)  # no price arrives
        self.assertEqual(
            (res["closed_win"], res["closed_loss"], res["closed_timeout"]),
            (0, 0, 0))
        self.assertIn("M1", state["shadow_open"])
        self.assertTrue(state["shadow_open"]["M1"]["pending"])

    def test_pending_times_out_as_no_data_not_a_loss(self):
        state = {}
        self._pending(state, age_s=61 * 60.0)  # older than 60m timeout
        d = _degen()
        with patch.object(shadow, "_refresh_mcaps"):
            res = shadow.update(state, d)
        self.assertEqual(res["pending_dropped"], 1)
        self.assertEqual(state["shadow_open"], {})
        # "No data" is not a loss: nothing lands in shadow_closed and the
        # insider outcome ledger is never joined for it.
        self.assertEqual(state.get("shadow_closed", []), [])

    def test_pending_timeout_disabled_by_zero(self):
        state = {}
        self._pending(state, age_s=10 * 3600.0)
        d = _degen(shadow_pending_timeout_minutes=0)
        with patch.object(shadow, "_refresh_mcaps"):
            shadow.update(state, d)
        self.assertIn("M1", state["shadow_open"])

    def test_summary_counts_pending_separately(self):
        state = {
            "shadow_open": {
                "M1": {"pending": True},
                "M2": {"pending": False, "entry_mcap": 1000},
                "M3": {},
            },
            "shadow_closed": [],
        }
        s = shadow.summary(state)
        self.assertEqual(s["pending"], 1)
        self.assertEqual(s["open"], 2)

    def test_never_raises(self):
        state = {"shadow_open": {"M1": {"pending": True}}}
        d = _degen()
        with patch.object(shadow, "_refresh_mcaps",
                          side_effect=RuntimeError("boom")):
            res = shadow.update(state, d)
        self.assertIn("M1", state["shadow_open"])


class SummaryTest(unittest.TestCase):
    def test_aggregates(self):
        state = {
            "shadow_open": {"M1": {}},
            "shadow_closed": [
                {"outcome": "win", "pnl_pct": 200.0},
                {"outcome": "win", "pnl_pct": 250.0},
                {"outcome": "loss", "pnl_pct": -30.0},
                {"outcome": "timeout", "pnl_pct": 5.0},
            ],
        }
        s = shadow.summary(state)
        self.assertEqual(s["open"], 1)
        self.assertEqual(s["closed"], 4)
        self.assertEqual(s["wins"], 2)
        self.assertEqual(s["losses"], 1)
        self.assertEqual(s["timeouts"], 1)
        self.assertAlmostEqual(s["win_rate"], 50.0)
        self.assertAlmostEqual(s["avg_pnl"], 106.25)
        self.assertAlmostEqual(s["best"], 250.0)
        self.assertAlmostEqual(s["worst"], -30.0)

    def test_empty(self):
        s = shadow.summary({})
        self.assertEqual((s["open"], s["closed"], s["win_rate"]),
                         (0, 0, 0.0))


class ShadowHookTest(unittest.TestCase):
    """A held runner opens a shadow position — and nothing else."""

    def test_momentum_hold_opens_shadow_no_publish(self):
        from daemon.launch_scanner import LaunchScanner, momentum
        from tests.test_momentum import _config, _pool
        cfg = _config()
        sc = LaunchScanner()
        state, tracked = {}, {}
        scanner = momentum.MomentumScanner()
        r = _pool(chg_24h_pct=180.0, usd_market_cap=1500000.0)
        published = []

        class FakeBus:
            def publish(self, e):
                published.append(e)

        with patch.object(LaunchScanner, "_instant_verdict") as iv, \
             patch.object(LaunchScanner, "_sentiment_track",
                          return_value=None):
            iv.return_value = (False, "score 1 < 50", {"score": 1})
            sc._momentum_handle(cfg, None, state, tracked, scanner, r,
                                1000.0, FakeBus())
        self.assertEqual(published, [])  # still silent
        self.assertIn("MINT1", state.get("shadow_open", {}))
        pos = state["shadow_open"]["MINT1"]
        self.assertEqual(pos["entry_mcap"], 1500000.0)
        self.assertEqual(pos["kind"], "momentum")


if __name__ == "__main__":
    unittest.main()
