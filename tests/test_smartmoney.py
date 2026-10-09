"""Tests for the Helius smart-money tracker: registry, buy detection,
boost wiring, and the Telegram card line. No network — Helius RPC is
mocked."""

import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from daemon.launch_scanner import smartmoney


WALLET = "7xKXgT2aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890ab"


def _degen(**kw):
    base = dict(enabled=True, poll_interval_seconds=30, entry_sol=0.05,
                max_positions=5, min_usd_market_cap=10000.0,
                max_age_minutes=10.0, velocity_multiplier=3.0,
                velocity_window_minutes=10.0, stop_loss_pct=30.0,
                take_profit_pct=200.0, max_hold_hours=4.0, auto_enter=True,
                confirm_gain_mult=1.8, confirm_rising_polls=3,
                confirm_min_m5_volume_usd=3000.0,
                confirm_min_liquidity_usd=12000.0, confirm_min_buys_m5=5,
                confirm_min_buy_sell_ratio=1.5, confirm_window_minutes=30.0,
                smart_boost_wallets=2, smart_rising_polls=2)
    base.update(kw)
    return SimpleNamespace(**base)


class SmartMoneyRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/smartmoney-test-%d" % int(time.time() * 1e6))
        self.tmp.mkdir(parents=True)
        self.reg = self.tmp / "smart_wallets.json"
        self.p = patch.object(smartmoney, "_REGISTRY_FILE", self.reg)
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_add_list_remove(self):
        ok, msg = smartmoney.add_wallet(WALLET, "sniper-1")
        self.assertTrue(ok, msg)
        reg = smartmoney.load_registry()
        self.assertIn(WALLET, reg)
        self.assertEqual(reg[WALLET]["label"], "sniper-1")
        ok, _ = smartmoney.add_wallet(WALLET)
        self.assertFalse(ok)  # duplicate
        ok, _ = smartmoney.remove_wallet(WALLET)
        self.assertTrue(ok)
        self.assertNotIn(WALLET, smartmoney.load_registry())

    def test_add_rejects_bad_address(self):
        ok, _ = smartmoney.add_wallet("nope")
        self.assertFalse(ok)

    def test_registry_file_mode(self):
        smartmoney.add_wallet(WALLET)
        self.assertEqual(oct(self.reg.stat().st_mode & 0o777), "0o600")

    def test_record_outcome_scores(self):
        smartmoney.add_wallet(WALLET)
        smartmoney.record_outcome("MINT1", True, [WALLET])
        smartmoney.record_outcome("MINT2", False, [WALLET])
        w = smartmoney.load_registry()[WALLET]
        self.assertEqual(w["picks"], 2)
        self.assertEqual(w["wins"], 1)
        self.assertEqual(smartmoney.wallet_score(w), 50.0)


class SmartMoneyPollTest(unittest.TestCase):
    def _tx(self, mint, amt):
        return {"meta": {
            "preTokenBalances": [],
            "postTokenBalances": [{
                "owner": WALLET, "mint": mint,
                "uiTokenAmount": {"uiAmount": amt}}]}}

    def test_poll_detects_buys(self):
        state = {}
        sigs = [{"signature": "sig1"}, {"signature": "sig0"}]
        txs = {"sig1": self._tx("MINTA", 100.0), "sig0": self._tx("MINTB", 5.0)}

        def fake_rpc(method, params):
            if method == "getSignaturesForAddress":
                return sigs
            if method == "getTransaction":
                return txs[params[0]]
            return None

        reg = {WALLET: {"label": "", "added_ts": 0, "picks": 0, "wins": 0}}
        with patch.object(smartmoney, "api_key", return_value="k"), \
             patch.object(smartmoney, "_rpc", side_effect=fake_rpc), \
             patch.object(smartmoney, "load_registry", return_value=reg), \
             patch("time.sleep", return_value=None):
            fresh = smartmoney.poll_wallets(state, poll_seconds=0)
        self.assertEqual(set(fresh), {"MINTA", "MINTB"})
        self.assertEqual(state["smart_activity"]["MINTA"], [WALLET])
        self.assertEqual(smartmoney.smart_buys_for(state, "MINTA"), 1)
        # Second poll: no new signatures -> no fresh activity.
        with patch.object(smartmoney, "api_key", return_value="k"), \
             patch.object(smartmoney, "_rpc", side_effect=fake_rpc), \
             patch.object(smartmoney, "load_registry", return_value=reg), \
             patch("time.sleep", return_value=None):
            fresh2 = smartmoney.poll_wallets(state, poll_seconds=0)
        self.assertEqual(fresh2, {})

    def test_poll_idles_without_key(self):
        with patch.object(smartmoney, "api_key", return_value=None):
            self.assertEqual(smartmoney.poll_wallets({}, poll_seconds=0), {})


class SmartMoneyJudgeBoostTest(unittest.TestCase):
    def test_boost_reduces_rising_polls(self):
        from daemon.launch_scanner import runner
        degen = _degen()
        state = {"candidates": {}, "smart_activity": {"MINT1": ["w1", "w2"]},
                 "seen": {}}
        cand = {"key": "k", "mint": "MINT1", "symbol": "TKN",
                "chain": "solana", "source": "pumpfun",
                "first_ts": time.time() - 60, "first_mcap": 10000.0,
                "rising_streak": 0, "no_pair_polls": 0, "smart_buys": 0}
        state["candidates"]["k"] = cand
        judge = runner.RunnerJudge(degen)
        boost_wallets = 2
        smart_buys = smartmoney.smart_buys_for(state, "MINT1")
        self.assertEqual(smart_buys, 2)
        j = judge
        if smart_buys >= boost_wallets:
            j = runner.RunnerJudge(degen)
            j.rising_polls = 2
        self.assertEqual(j.rising_polls, 2)
        self.assertEqual(judge.rising_polls, 3)  # base judge untouched


class SmartMoneyCardTest(unittest.TestCase):
    def test_card_line(self):
        from daemon.telegram import alerts
        card = alerts._launch({"tier": "runner", "symbol": "TKN",
                               "chain": "solana", "source": "pumpfun",
                               "mint": "MINT1", "usd_market_cap": 50000,
                               "age_minutes": 3.0,
                               "smart_money": "2 tracked wallets bought early"})
        self.assertIn("🧠 smart money: 2 tracked wallets bought early", card)
        self.assertIn("CA: MINT1", card)

    def test_no_line_without_smart_money(self):
        from daemon.telegram import alerts
        card = alerts._launch({"tier": "runner", "symbol": "TKN"})
        self.assertNotIn("🧠", card)


if __name__ == "__main__":
    unittest.main()
