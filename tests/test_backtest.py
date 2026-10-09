"""Tests for daemon/launch_scanner/backtest.py (standalone engine)."""

import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path

_BT = (Path(__file__).resolve().parents[1]
       / "daemon" / "launch_scanner" / "backtest.py")


def _load():
    spec = importlib.util.spec_from_file_location(
        "launch_scanner_backtest", str(_BT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


backtest = _load()


def _hist(prices, narratives=None, source="pumpfun",
          safety_score=80.0, unknown=False, rug=False, t_step=60.0):
    """Hand-built history with exact prices for exit-math tests."""
    snaps = []
    for i, p in enumerate(prices):
        snaps.append({"t": i * t_step, "price_usd": p,
                      "mcap_usd": 10000.0, "vol5m_usd": 1000.0,
                      "buys5m": 10, "sells5m": 5, "liq_usd": 20000.0})
    return {
        "mint": "MINT%d" % len(prices), "symbol": "T", "chain": "solana",
        "source": source, "t0": 0.0, "snapshots": snaps,
        "safety": {"score": safety_score, "passed": safety_score >= 50.0,
                   "fails": [], "unknown": unknown},
        "sentiment": None, "narratives": list(narratives or []),
        "outcome": {"peak_mult": max(prices), "peak_t": 0.0,
                    "rug": rug, "end_t": len(prices) * t_step},
    }


def _engine(**kw):
    entry = {"min_safety_score": 50.0, "require_known_safety": True,
             "max_entry_mcap_usd": 500000.0}
    entry.update(kw.get("entry_rules") or {})
    exitr = {"tp_ladder": [(2.0, 0.5), (5.0, 0.25)],
             "trailing_stop_pct": 30.0, "time_stop_h": 6.0}
    exitr.update(kw.get("exit_rules") or {})
    return backtest.BacktestEngine(entry_rules=entry, exit_rules=exitr)


class BacktestEngineTest(unittest.TestCase):
    def test_report_keys_present(self):
        r = _engine().run([])
        for key in ("n_histories", "n_trades", "wins", "losses", "win_rate",
                    "avg_mult", "median_mult", "profit_factor",
                    "max_drawdown_mult", "expectancy_mult", "per_source",
                    "per_narrative", "per_gate_blocked", "skipped_malformed",
                    "trades"):
            self.assertIn(key, r)
        self.assertEqual(r["n_histories"], 0)
        self.assertEqual(r["n_trades"], 0)

    def test_tp_ladder_two_x_sells_half(self):
        # entry 1.0 -> 2.0 touches the 2x rung: sell half at 2.0,
        # remainder exits at the final 1.0 -> 0.5*2 + 0.5*1 = 1.5
        eng = _engine(exit_rules={"tp_ladder": [(2.0, 0.5)]})
        r = eng.run([_hist([1.0, 2.0, 1.0])])
        self.assertEqual(r["n_trades"], 1)
        trade = r["trades"][0]
        self.assertAlmostEqual(trade["proceeds_mult"], 1.5, places=6)
        self.assertAlmostEqual(trade["pnl_mult"], 0.5, places=6)
        self.assertEqual(r["wins"], 1)

    def test_tp_ladder_two_rungs(self):
        # Default-style ladder: 2x sells half, 5x sells a quarter.
        # 0.5*2 + 0.25*5 + 0.25*1 (remainder at final) = 2.5
        eng = _engine(exit_rules={"tp_ladder": [(2.0, 0.5), (5.0, 0.25)]})
        r = eng.run([_hist([1.0, 2.0, 5.0, 1.0])])
        self.assertAlmostEqual(r["trades"][0]["proceeds_mult"], 2.5, places=6)
        self.assertEqual(r["wins"], 1)

    def test_trailing_stop_triggers(self):
        # peaks at 2.0 (>=1.5x arms the stop), falls to 1.3 which is
        # below 2.0 * 0.7 = 1.4 -> remainder exits at 1.3
        eng = _engine(exit_rules={"tp_ladder": []})
        r = eng.run([_hist([1.0, 2.0, 1.8, 1.3, 0.5])])
        trade = r["trades"][0]
        self.assertEqual(trade["exit_reason"], "trailing")
        self.assertAlmostEqual(trade["proceeds_mult"], 1.3, places=6)
        self.assertGreater(trade["max_drawdown_mult"], 0.0)

    def test_trailing_stop_not_armed_below_one_point_five(self):
        # peaks at 1.2 only -> no trailing stop; exits at final price
        eng = _engine(exit_rules={"tp_ladder": []})
        r = eng.run([_hist([1.0, 1.2, 0.8])])
        trade = r["trades"][0]
        self.assertEqual(trade["exit_reason"], "final")
        self.assertAlmostEqual(trade["proceeds_mult"], 0.8, places=6)

    def test_time_stop(self):
        # time_stop_h=0 -> deadline is entry_t; first post-entry snapshot
        # at t>=deadline sells the remainder at that price
        eng = _engine(exit_rules={"tp_ladder": [], "time_stop_h": 0.0})
        r = eng.run([_hist([1.0, 1.5, 3.0])])
        trade = r["trades"][0]
        self.assertEqual(trade["exit_reason"], "time-stop")
        self.assertAlmostEqual(trade["proceeds_mult"], 1.5, places=6)

    def test_rug_blocked(self):
        r = _engine().run([_hist([1.0, 2.0], rug=True)])
        self.assertEqual(r["n_trades"], 0)
        self.assertEqual(r["per_gate_blocked"].get("rug"), 1)

    def test_unknown_safety_blocked(self):
        r = _engine().run([_hist([1.0, 2.0], safety_score=0.0, unknown=True)])
        self.assertEqual(r["n_trades"], 0)
        self.assertEqual(r["per_gate_blocked"].get("safety-unknown"), 1)

    def test_low_safety_score_blocked(self):
        r = _engine().run([_hist([1.0, 2.0], safety_score=49.0)])
        self.assertEqual(r["n_trades"], 0)
        self.assertEqual(r["per_gate_blocked"].get("safety-score"), 1)

    def test_entry_mcap_blocked(self):
        h = _hist([1.0, 2.0])
        for s in h["snapshots"]:
            s["mcap_usd"] = 10_000_000.0  # above the 500k ceiling
        r = _engine().run([h])
        self.assertEqual(r["n_trades"], 0)
        self.assertEqual(r["per_gate_blocked"].get("entry-mcap"), 1)

    def test_malformed_record_skipped_not_crashed(self):
        bad = [None, "garbage", {"snapshots": "not-a-list"},
               {"snapshots": []}, 42]
        r = _engine().run(bad)
        self.assertEqual(r["n_trades"], 0)
        self.assertEqual(r["skipped_malformed"], 3)  # None, str, 42
        self.assertEqual(r["per_gate_blocked"].get("no-snapshots"), 2)

    def test_zero_and_none_prices_guarded(self):
        h = _hist([1.0, 2.0])
        h["snapshots"].append({"t": 180.0, "price_usd": 0,
                              "mcap_usd": None, "vol5m_usd": "bad",
                              "buys5m": None, "sells5m": None,
                              "liq_usd": None})
        h["snapshots"].append(None)  # not a dict
        r = _engine().run([h])  # must not raise
        self.assertEqual(r["n_trades"], 1)

    def test_per_narrative_attribution(self):
        win = _hist([1.0, 2.0, 1.0], narratives=["ai", "meme"])
        loss = _hist([1.0, 0.5], narratives=["ai"])
        r = _engine().run([win, loss])
        self.assertEqual(r["per_narrative"]["ai"]["trades"], 2)
        self.assertEqual(r["per_narrative"]["ai"]["win_rate"], 0.5)
        self.assertEqual(r["per_narrative"]["meme"]["trades"], 1)
        self.assertEqual(r["per_narrative"]["meme"]["win_rate"], 1.0)
        self.assertEqual(r["per_source"]["pumpfun"]["trades"], 2)

    def test_profit_factor_and_expectancy(self):
        # win +0.5, loss -0.5 -> profit factor 1.0, expectancy 0.0
        win = _hist([1.0, 2.0, 1.0])
        loss = _hist([1.0, 0.5])
        r = _engine().run([win, loss])
        self.assertAlmostEqual(r["profit_factor"], 1.0, places=6)
        self.assertAlmostEqual(r["expectancy_mult"], 0.0, places=6)
        self.assertAlmostEqual(r["avg_mult"], 1.0, places=6)

    def test_synthetic_determinism(self):
        a = backtest.synthetic_history(seed=7, n_snaps=30, drift=0.05,
                                       vol=0.1, rug=True)
        b = backtest.synthetic_history(seed=7, n_snaps=30, drift=0.05,
                                       vol=0.1, rug=True)
        self.assertEqual(a, b)
        c = backtest.synthetic_history(seed=8, n_snaps=30, drift=0.05,
                                       vol=0.1, rug=True)
        self.assertNotEqual(a["snapshots"], c["snapshots"])
        ra = _engine().run([a])
        rb = _engine().run([b])
        self.assertEqual(ra["n_trades"], rb["n_trades"])
        self.assertEqual(ra["trades"], rb["trades"])

    def test_synthetic_rug_blocked_in_engine(self):
        h = backtest.synthetic_history(seed=3, rug=True)
        r = _engine().run([h])
        self.assertEqual(r["per_gate_blocked"].get("rug"), 1)

    def test_record_history_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "hist.jsonl")
            h = backtest.synthetic_history(seed=1)
            self.assertTrue(backtest.record_history(path, h))
            r = _engine().run(backtest._load_dataset(path))
            self.assertEqual(r["n_histories"], 1)

    def test_record_history_never_raises(self):
        self.assertFalse(backtest.record_history("/nonexistent_dir_xyz/\x00",
                                                 {"a": 1}))

    def test_cli_missing_dataset_message(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = backtest.main(["/tmp/definitely-not-here-xyz.jsonl"])
        self.assertEqual(rc, 0)
        self.assertIn("no data yet", buf.getvalue())

    def test_cli_parses_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "h.jsonl")
            backtest.record_history(
                path, backtest.synthetic_history(seed=5, n_snaps=40,
                                                drift=0.08, vol=0.02))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = backtest.main([path, "--min-safety", "10",
                                    "--tp", "1.5:1.0", "--trailing", "20",
                                    "--time-stop-h", "2"])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("Backtest", out)
            self.assertIn("trades", out)


if __name__ == "__main__":
    unittest.main()
