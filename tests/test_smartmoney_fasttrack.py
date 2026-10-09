"""Tests for smart-buy fast-track and wallet PnL grading.

No network — the safety verdict is injected as a callable, the grades
file is patched to tmp, and launch_scanner state is synthetic dicts."""

import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from daemon.launch_scanner import smartgrade, smartmoney
from daemon.telegram import commands_smart

W1 = "7xKXgT2aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890ab"
W2 = "8yLYhU3bCdEfGhIjKlMnOpQrStUvWxYz2345678901bc"


def _degen(**kw):
    base = dict(smart_fasttrack_enabled=True, early_min_score=1.0,
                smart_grade_dedup_days=7.0, smart_grade_window_days=30.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _pass(degen, candidate):
    assert candidate["mint"]
    assert candidate["chain"] == "solana"
    return True, "quality-pass score 5", {"score": 5, "passed": True}


def _honeypot(degen, candidate):
    return False, "safety-fail: honeypot", {"score": 1, "passed": False,
                                            "fails": ["honeypot"]}


class FastTrackTest(unittest.TestCase):
    def test_fires_on_first_seen_wallet_buy(self):
        state = {}
        trig = smartmoney.evaluate_fasttrack(
            state, _degen(), "MINT_NEW", ["sniper-1"],
            is_new=True, verdict_fn=_pass)
        self.assertIsNotNone(trig)
        self.assertEqual(trig["trigger"], "smart_fasttrack")
        self.assertEqual(trig["mint"], "MINT_NEW")
        self.assertEqual(trig["wallets"], ["sniper-1"])
        self.assertEqual(trig["safety_score"], 5.0)
        self.assertEqual(trig["card_source"], "smart-fasttrack")

    def test_hard_fail_blocks_and_marks_evaluated(self):
        # A wallet buying a honeypot: the gate wins, no trigger, and the
        # mint is marked evaluated so it never re-fires.
        state = {}
        trig = smartmoney.evaluate_fasttrack(
            state, _degen(), "MINT_RUG", ["sniper-1"],
            is_new=True, verdict_fn=_honeypot)
        self.assertIsNone(trig)
        self.assertIn("MINT_RUG", state["smart_fasttracked"])
        # Second poll sees the same buy again: still nothing.
        trig2 = smartmoney.evaluate_fasttrack(
            state, _degen(), "MINT_RUG", ["sniper-1"],
            is_new=True, verdict_fn=_pass)
        self.assertIsNone(trig2)

    def test_no_double_trigger(self):
        state = {}
        kw = dict(wallet_names=["sniper-1"], is_new=True,
                  verdict_fn=_pass)
        first = smartmoney.evaluate_fasttrack(
            state, _degen(), "MINT1", **kw)
        second = smartmoney.evaluate_fasttrack(
            state, _degen(), "MINT1", **kw)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_disabled_knob(self):
        state = {}
        trig = smartmoney.evaluate_fasttrack(
            state, _degen(smart_fasttrack_enabled=False), "MINT1",
            ["sniper-1"], is_new=True, verdict_fn=_pass)
        self.assertIsNone(trig)
        self.assertNotIn("MINT1", state.get("smart_fasttracked", {}))

    def test_no_verdict_fn_is_fail_closed(self):
        state = {}
        trig = smartmoney.evaluate_fasttrack(
            state, _degen(), "MINT1", ["sniper-1"], is_new=True,
            verdict_fn=None)
        self.assertIsNone(trig)

    def test_not_new_mint(self):
        state = {}
        trig = smartmoney.evaluate_fasttrack(
            state, _degen(), "MINT1", ["sniper-1"], is_new=False,
            verdict_fn=_pass)
        self.assertIsNone(trig)

    def test_verdict_crash_holds_and_marks(self):
        def boom(degen, cand):
            raise RuntimeError("safety exploded")
        state = {}
        trig = smartmoney.evaluate_fasttrack(
            state, _degen(), "MINT1", ["sniper-1"], is_new=True,
            verdict_fn=boom)
        self.assertIsNone(trig)
        self.assertIn("MINT1", state["smart_fasttracked"])

    def test_mint_is_new_helper(self):
        state = {"seen": {"MINT_SEEN": {}}, "candidates": {},
                 "milestone_watch": {"MINT_CARD": {}}}
        self.assertFalse(smartmoney.mint_is_new(state, "MINT_SEEN"))
        self.assertFalse(smartmoney.mint_is_new(state, "MINT_CARD"))
        self.assertTrue(smartmoney.mint_is_new(state, "MINT_FRESH"))
        state["smart_fasttracked"] = {"MINT_FRESH": {"ts": time.time()}}
        self.assertFalse(smartmoney.mint_is_new(state, "MINT_FRESH"))

    def test_is_new_callable_is_honored(self):
        state = {}
        trig = smartmoney.evaluate_fasttrack(
            state, _degen(), "MINT1", ["sniper-1"],
            is_new=lambda m: smartmoney.mint_is_new(state, m),
            verdict_fn=_pass)
        self.assertIsNotNone(trig)

    def test_wallet_labels_for(self):
        state = {"smart_activity": {"MINT1": [W1]}}
        reg = {W1: {"label": "sniper-1"}}
        with patch.object(smartmoney, "load_registry",
                          return_value=reg):
            self.assertEqual(smartmoney.wallet_labels_for(state, "MINT1"),
                             ["sniper-1"])
        # Unknown wallet falls back to a short address, never the full one.
        with patch.object(smartmoney, "load_registry", return_value={}):
            labels = smartmoney.wallet_labels_for(state, "MINT1")
            self.assertEqual(labels, [W1[:8] + "…"])


class GradeLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/smartgrade-test-%d" % int(time.time() * 1e6))
        self.tmp.mkdir(parents=True)
        self.p = patch.object(smartgrade, "_GRADES_FILE",
                              self.tmp / "smart_wallet_grades.json")
        self.p.start()
        self.addCleanup(self.p.stop)

    def _watch(self):
        return {
            "MINT_A": {"anchor": 10000.0, "hit": [2.0, 5.0],
                       "peak_mult": 6.0},
            "MINT_B": {"anchor": 20000.0, "hit": [],
                       "peak_mult": 1.2},
        }

    def test_record_buys_dedup(self):
        n = smartgrade.record_wallet_buys({"MINT_A": [W1], "MINT_B": [W2]})
        self.assertEqual(n, 2)
        # Same mints again within the dedup window: no new records.
        n2 = smartgrade.record_wallet_buys({"MINT_A": [W1]})
        self.assertEqual(n2, 0)
        g = smartgrade.load_grades()
        self.assertEqual(len(g[W1]["buys"]), 1)
        self.assertGreater(g[W1]["last_active_ts"], 0)

    def test_reconcile_anchors_and_grades(self):
        smartgrade.record_wallet_buys(
            {"MINT_A": [W1], "MINT_B": [W1], "MINT_C": [W1]})
        updated = smartgrade.reconcile({"milestone_watch": self._watch()})
        self.assertGreater(updated, 0)
        reg = {W1: {"label": "sniper-1"}}
        g = smartgrade.grade_wallet(W1, reg)
        self.assertEqual(g["buys_tracked"], 3)
        self.assertEqual(g["buys_graded"], 2)   # MINT_C never carded
        self.assertEqual(g["n_2x"], 1)
        self.assertEqual(g["pct_2x"], 50.0)
        self.assertEqual(g["median_mult"], 3.6)  # median(1.2, 6.0)
        self.assertEqual(g["best_mult"], 6.0)
        self.assertEqual(g["worst_mult"], 1.2)

    def test_freezes_after_watch_expiry(self):
        smartgrade.record_wallet_buys({"MINT_A": [W1]})
        smartgrade.reconcile({"milestone_watch": self._watch()})
        # Watch record gone (24h expiry): values freeze, no crash.
        smartgrade.reconcile({"milestone_watch": {}})
        g = smartgrade.grade_wallet(W1, {W1: {}})
        self.assertEqual(g["buys_graded"], 1)
        self.assertEqual(g["best_mult"], 6.0)
        self.assertEqual(g["n_2x"], 1)

    def test_prune_old_buys(self):
        smartgrade.record_wallet_buys({"MINT_A": [W1]})
        g = smartgrade.load_grades()
        g[W1]["buys"][0]["buy_ts"] = time.time() - 60 * 86400
        smartgrade.save_grades(g)
        smartgrade.reconcile({"milestone_watch": {}})
        g2 = smartgrade.grade_wallet(W1, {W1: {}})
        self.assertEqual(g2["buys_tracked"], 0)

    def test_grades_file_mode(self):
        smartgrade.record_wallet_buys({"MINT_A": [W1]})
        f = self.tmp / "smart_wallet_grades.json"
        self.assertTrue(f.exists())
        self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600")


class CmdSmartGradeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/smartgradecmd-test-%d"
                        % int(time.time() * 1e6))
        self.tmp.mkdir(parents=True)
        self.p = patch.object(smartgrade, "_GRADES_FILE",
                              self.tmp / "smart_wallet_grades.json")
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_no_wallets(self):
        text = commands_smart.build_grade_text({}, {})
        self.assertIn("No smart wallets tracked yet", text)
        self.assertIn("/smartadd", text)

    def test_empty_history_renders_cleanly(self):
        reg = {W1: {"label": "sniper-1"}, W2: {"label": ""}}
        text = commands_smart.build_grade_text(reg, {})
        self.assertIn("no buys in trailing 30d", text)
        self.assertIn("sniper-1", text)
        self.assertNotIn("Traceback", text)

    def test_table_output(self):
        smartgrade.record_wallet_buys({"MINT_A": [W1], "MINT_B": [W1]})
        watch = {"MINT_A": {"anchor": 10000.0, "hit": [2.0],
                            "peak_mult": 3.0},
                 "MINT_B": {"anchor": 20000.0, "hit": [],
                            "peak_mult": 0.8}}
        reg = {W1: {"label": "sniper-1"}}
        text = commands_smart.build_grade_text(reg,
                                               {"milestone_watch": watch})
        self.assertIn("🧠", text)
        self.assertIn("sniper-1", text)
        self.assertIn("2 buys", text)
        self.assertIn("50%", text)          # 1 of 2 graded hit 2x
        self.assertIn("best 3.0x", text)
        self.assertIn("measurement only", text)

    def test_cmd_returns_string(self):
        with patch.object(smartmoney, "load_registry", return_value={}):
            with patch("daemon.common.get_daemon_state",
                       return_value={}):
                text = commands_smart.cmd_smartgrade(None, [])
        self.assertIsInstance(text, str)
        self.assertIn("No smart wallets", text)


if __name__ == "__main__":
    unittest.main()
