"""Tests for the degen launch radar: sources normalization, qualification
tiers, paper entry idempotency, exits, and the Telegram launch card."""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from daemon.launch_scanner import sources
from daemon.launch_scanner import LaunchScanner
from daemon.telegram import alerts


def _degen(**kw):
    base = dict(enabled=True, poll_interval_seconds=30, entry_sol=0.05,
                max_positions=5, min_usd_market_cap=10000.0,
                max_age_minutes=10.0, velocity_multiplier=3.0,
                velocity_window_minutes=10.0, stop_loss_pct=30.0,
                take_profit_pct=200.0, max_hold_hours=4.0, auto_enter=True,
                early_safety_flash=False, early_flash_timeout_s=4.0,
                early_min_score=30.0,
                early_alert_max_per_window=10,
                early_alert_window_seconds=300.0,
                # Hermetic: the feedback scan reads the real paper-outcome
                # dataset by default — unit tests must never depend on
                # (or publish from) live HOME state.
                feedback_enabled=False,
                # Same for the weekly review card: it reads the live
                # dataset and publishes — unit scans stay silent.
                review_enabled=False)
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


class SourcesTest(unittest.TestCase):
    def test_pumpfun_normalizes(self):
        raw = [{"mint": "ABC", "symbol": "X", "name": "X Token",
                "created_timestamp": 1790145999000,
                "usd_market_cap": 7121.19, "complete": False,
                "reply_count": 3, "is_banned": False,
                "creator": "CREATOR"}]
        with patch.object(sources, "_get_json", return_value=raw):
            out = sources.pumpfun_new()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["mint"], "ABC")
        self.assertEqual(out[0]["chain"], "solana")
        self.assertEqual(out[0]["source"], "pumpfun")
        self.assertAlmostEqual(out[0]["created_ts"], 1790145999.0)
        self.assertTrue(out[0]["url"].endswith("ABC"))

    def test_pumpfun_failure_returns_empty(self):
        with patch.object(sources, "_get_json", return_value=None):
            self.assertEqual(sources.pumpfun_new(), [])

    def test_boosts_filters_chains(self):
        raw = [
            {"chainId": "solana", "tokenAddress": "A1", "url": "u1"},
            {"chainId": "base", "tokenAddress": "B1", "url": "u2"},
            {"chainId": "ethereum", "tokenAddress": "E1", "url": "u3"},
            {"chainId": "robinhood", "tokenAddress": "R1", "url": "u4"},
            {"chainId": "bsc", "tokenAddress": "Z1", "url": "u5"},
        ]
        with patch.object(sources, "_get_json", return_value=raw):
            out = sources.dexscreener_boosts()
        chains = {c["chain"] for c in out}
        self.assertEqual(chains, {"solana", "base", "ethereum", "robinhood"})

    def test_hyperliquid_skips_stables(self):
        raw = {"tokens": [{"name": "USDC"}, {"name": "PURR"}, {"name": "HYPE"}]}
        with patch.object(sources, "_post_json", return_value=raw):
            out = sources.hyperliquid_listings()
        self.assertEqual({c["symbol"] for c in out}, {"PURR", "HYPE"})
        self.assertTrue(all(c["mint"] is None for c in out))


class GeckoUsdcPoolsTest(unittest.TestCase):
    def _pool(self, base_id, quote_id, name="DIME / USDC 2.49%",
              pool_addr="POOL1", created="2026-09-23T18:10:35Z",
              fdv="44254219.48"):
        return {
            "attributes": {"name": name, "address": pool_addr,
                           "pool_created_at": created, "fdv_usd": fdv,
                           "market_cap_usd": None},
            "relationships": {
                "base_token": {"data": {"id": base_id, "type": "token"}},
                "quote_token": {"data": {"id": quote_id, "type": "token"}},
                "dex": {"data": {"id": "uniswap-v3", "type": "dex"}},
            },
        }

    def _payload(self, pools):
        return {"data": pools, "included": []}

    def test_usdc_quoted_pool_kept(self):
        pool = self._pool(
            "eth_0xDIME000000000000000000000000000000000001",
            "eth_0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")
        with patch.object(sources, "_get_json",
                          return_value=self._payload([pool])):
            out = sources.geckoterminal_usdc_pools()
        eth = [c for c in out if c["chain"] == "ethereum"]
        self.assertEqual(len(eth), 1)
        c = eth[0]
        self.assertEqual(c["mint"],
                         "0xDIME000000000000000000000000000000000001")
        self.assertEqual(c["symbol"], "DIME")
        self.assertEqual(c["source"], "gecko-usdc-pool")
        self.assertGreater(c["created_ts"], 0)
        self.assertIn("geckoterminal.com/eth/pools/POOL1", c["url"])

    def test_non_usdc_quote_dropped(self):
        pool = self._pool(
            "solana_2M8PKfobZpaZ3de9ZAuswuBBRDXe5pbcsCfi7QvDpump",
            "solana_So11111111111111111111111111111111111111112",
            name="GSTD / SOL")
        with patch.object(sources, "_get_json",
                          return_value=self._payload([pool])):
            out = sources.geckoterminal_usdc_pools()
        self.assertEqual(
            [c for c in out if c["chain"] == "solana"], [])

    def test_solana_usdc_pool_by_mint_not_name(self):
        # matched by the USDC mint address, not the pool name
        pool = self._pool(
            "solana_2M8PKfobZpaZ3de9ZAuswuBBRDXe5pbcsCfi7QvDpump",
            "solana_EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            name="GSTD / USDC")
        with patch.object(sources, "_get_json",
                          return_value=self._payload([pool])):
            out = sources.geckoterminal_usdc_pools()
        sol = [c for c in out if c["chain"] == "solana"]
        self.assertEqual(len(sol), 1)
        self.assertEqual(
            sol[0]["mint"], "2M8PKfobZpaZ3de9ZAuswuBBRDXe5pbcsCfi7QvDpump")

    def test_usdc_itself_never_alerted(self):
        pool = self._pool(
            "solana_EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "solana_EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            name="USDC / USDC")
        with patch.object(sources, "_get_json",
                          return_value=self._payload([pool])):
            out = sources.geckoterminal_usdc_pools()
        self.assertEqual(out, [])

    def test_bad_payload_returns_empty(self):
        with patch.object(sources, "_get_json", return_value=None):
            self.assertEqual(sources.geckoterminal_usdc_pools(), [])
        with patch.object(sources, "_get_json",
                          return_value={"data": "garbage"}):
            self.assertEqual(sources.geckoterminal_usdc_pools(), [])

    def test_qualify_fresh_usdc_pool_is_early(self):
        sc = LaunchScanner.__new__(LaunchScanner)
        now = time.time()
        c = {"mint": "0xABC", "symbol": "DIME", "name": "DIME / USDC",
             "chain": "ethereum", "source": "gecko-usdc-pool",
             "created_ts": now - 3 * 60, "usd_market_cap": 44254219.0,
             "extra": {}, "url": ""}
        entry = {"first_ts": now, "mcap": [], "alerted": False}
        self.assertEqual(sc._qualify(_degen(), c, entry, now), "early")

    def test_qualify_old_usdc_pool_is_graduated(self):
        sc = LaunchScanner.__new__(LaunchScanner)
        now = time.time()
        c = {"mint": "0xABC", "symbol": "DIME", "name": "DIME / USDC",
             "chain": "ethereum", "source": "gecko-usdc-pool",
             "created_ts": now - 3 * 3600, "usd_market_cap": 44254219.0,
             "extra": {}, "url": ""}
        entry = {"first_ts": now, "mcap": [], "alerted": False}
        self.assertEqual(sc._qualify(_degen(), c, entry, now),
                         "graduated")

    def test_qualify_usdc_pool_below_mcap_bar_rejected(self):
        sc = LaunchScanner.__new__(LaunchScanner)
        now = time.time()
        c = {"mint": "0xABC", "symbol": "DUST", "name": "DUST / USDC",
             "chain": "base", "source": "gecko-usdc-pool",
             "created_ts": now - 60, "usd_market_cap": 500.0,
             "extra": {}, "url": ""}
        entry = {"first_ts": now, "mcap": [], "alerted": False}
        self.assertIsNone(sc._qualify(_degen(), c, entry, now))


class QualifyTest(unittest.TestCase):
    def setUp(self):
        self.sc = LaunchScanner.__new__(LaunchScanner)

    def _entry(self, first_ts=None):
        return {"first_ts": first_ts or time.time(), "mcap": [],
                "alerted": False, "symbol": "TKN", "name": "Token",
                "chain": "solana", "source": "pumpfun", "url": ""}

    def test_early_tier(self):
        d = _degen()
        c = _pump_coin(usd_market_cap=15000.0)
        tier = self.sc._qualify(d, c, self._entry(), time.time())
        self.assertEqual(tier, "early")

    def test_below_mcap_bar_no_qualify(self):
        d = _degen()
        c = _pump_coin(usd_market_cap=500.0)
        tier = self.sc._qualify(d, c, self._entry(), time.time())
        self.assertIsNone(tier)

    def test_banned_never_qualifies(self):
        d = _degen()
        c = _pump_coin(usd_market_cap=999999.0,
                       extra={"complete": False, "banned": True})
        tier = self.sc._qualify(d, c, self._entry(), time.time())
        self.assertIsNone(tier)

    def test_graduated_tier(self):
        d = _degen()
        c = _pump_coin(usd_market_cap=50000.0,
                       extra={"complete": True, "banned": False})
        tier = self.sc._qualify(d, c, self._entry(), time.time())
        self.assertEqual(tier, "graduated")

    def test_graduated_below_mcap_floor_rejected(self):
        # Graduates bypass the age gate but NOT the absolute mcap floor.
        d = _degen()
        c = _pump_coin(usd_market_cap=10.0,
                       extra={"complete": True, "banned": False})
        tier = self.sc._qualify(d, c, self._entry(), time.time())
        self.assertIsNone(tier)

    def test_velocity_tier(self):
        d = _degen(min_usd_market_cap=10000.0)
        now = time.time()
        entry = self._entry(first_ts=now - 20 * 60)  # too old for "early"
        entry["mcap"] = [[now - 300, 2000.0], [now - 60, 8000.0]]
        c = _pump_coin(usd_market_cap=8000.0)
        tier = self.sc._qualify(d, c, entry, now)
        self.assertEqual(tier, "velocity")

    def test_boost_tier_with_liquidity(self):
        d = _degen()
        c = {"mint": "B1", "chain": "base", "source": "dexscreener-boost",
             "symbol": "", "name": "", "url": "u", "usd_market_cap": 0}
        entry = {"first_ts": time.time(), "mcap": [], "alerted": False,
                 "symbol": "", "name": "", "chain": "base",
                 "source": "dexscreener-boost", "url": "u"}
        stats = {"symbol": "BASE", "name": "Base Coin", "usd_liquidity": 50000,
                 "usd_market_cap": 200000, "url": "https://dexscreener.com/x"}
        with patch.object(sources, "dexscreener_token_stats",
                           return_value=stats):
            tier = self.sc._qualify(d, c, entry, time.time())
        self.assertEqual(tier, "boost")
        self.assertEqual(entry["symbol"], "BASE")

    def test_boost_rejected_without_liquidity(self):
        d = _degen()
        c = {"mint": "B1", "chain": "base", "source": "dexscreener-boost",
             "symbol": "", "name": "", "url": "u", "usd_market_cap": 0}
        entry = {"first_ts": time.time(), "mcap": [], "alerted": False,
                 "symbol": "", "name": "", "chain": "base",
                 "source": "dexscreener-boost", "url": "u"}
        stats = {"symbol": "SCAM", "name": "s", "usd_liquidity": 100,
                 "usd_market_cap": 1000, "url": ""}
        with patch.object(sources, "dexscreener_token_stats",
                           return_value=stats):
            tier = self.sc._qualify(d, c, entry, time.time())
        self.assertIsNone(tier)


class PaperEntryTest(unittest.TestCase):
    def setUp(self):
        self.sc = LaunchScanner.__new__(LaunchScanner)

    def _adapter(self, price=0.001):
        def _result():
            return SimpleNamespace(ticket=4242, price=price)
        ad = SimpleNamespace()
        ad._price_sol_quiet = lambda symbol: price
        ad.submit_order = lambda req: _result()
        ad.close_position = lambda ticket: price * 2  # +100%
        return ad

    def test_entry_records_position_and_card(self):
        d = _degen()
        state = {"degen_positions": {}}
        card = {"entry": None, "note": ""}
        ok = self.sc._paper_enter(None, self._adapter(), state, card, d,
                                  "MINT1", "TKN")
        self.assertTrue(ok)
        self.assertIn("MINT1", state["degen_positions"])
        self.assertEqual(card["entry"]["ticket"], 4242)

    def test_no_quote_no_entry(self):
        d = _degen()
        state = {"degen_positions": {}}
        card = {"entry": None, "note": ""}
        ok = self.sc._paper_enter(None, self._adapter(price=0), state,
                                  card, d, "MINT1", "TKN")
        self.assertFalse(ok)
        self.assertEqual(state["degen_positions"], {})
        self.assertIn("no live quote", card["note"])

    def test_take_profit_exit_emits_event(self):
        d = _degen()
        state = {"degen_positions": {
            "MINT1": {"ticket": 7, "symbol": "TKN", "entry_price_sol": 0.001,
                      "entry_ts": time.time(), "sl": 0.0007, "tp": 0.003}}}
        published = []
        import agent.events as events_pkg
        with patch.object(events_pkg, "publish", side_effect=published.append):
            # price 0.004 > tp 0.003 -> take_profit
            self.sc._manage_exits(SimpleNamespace(degen=d),
                                  self._adapter(price=0.004), state)
        self.assertEqual(state["degen_positions"], {})
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["event"], "position.closed")
        self.assertIn("take_profit", published[0]["reason"])
        self.assertGreater(published[0]["profit"], 0)

    def test_stop_loss_exit(self):
        d = _degen()
        state = {"degen_positions": {
            "MINT1": {"ticket": 7, "symbol": "TKN", "entry_price_sol": 0.001,
                      "entry_ts": time.time(), "sl": 0.0007, "tp": 0.003}}}
        published = []
        import agent.events as events_pkg
        with patch.object(events_pkg, "publish", side_effect=published.append):
            self.sc._manage_exits(SimpleNamespace(degen=d),
                                  self._adapter(price=0.0005), state)
        # close_position mock returns price*2 = 0.001 -> pnl 0, but exit fired
        self.assertEqual(state["degen_positions"], {})
        self.assertIn("stop_loss", published[0]["reason"])

    def test_max_hold_exit(self):
        d = _degen(max_hold_hours=4.0)
        state = {"degen_positions": {
            "MINT1": {"ticket": 7, "symbol": "TKN", "entry_price_sol": 0.001,
                      "entry_ts": time.time() - 5 * 3600,
                      "sl": 0.0007, "tp": 0.003}}}
        published = []
        import agent.events as events_pkg
        with patch.object(events_pkg, "publish", side_effect=published.append):
            self.sc._manage_exits(SimpleNamespace(degen=d),
                                  self._adapter(price=0.001), state)
        self.assertEqual(state["degen_positions"], {})
        self.assertIn("max_hold", published[0]["reason"])


class LaunchCardTest(unittest.TestCase):
    def test_card_always_carries_ca(self):
        e = {"event": "launch.detected", "tier": "early", "source": "pumpfun",
             "chain": "solana", "mint": "MINTABC123", "symbol": "TKN",
             "name": "Token", "url": "https://pump.fun/coin/MINTABC123",
             "usd_market_cap": 15400, "age_minutes": 2.5,
             "entry": {"ticket": 9, "volume_sol": 0.05,
                       "price_sol": 0.0000123, "stop_loss_pct": 30.0,
                       "take_profit_pct": 200.0},
             "note": ""}
        text = alerts.format_alert(e)
        self.assertIn("MINTABC123", text)
        self.assertIn("🚀", text)
        self.assertIn("paper entry", text)

    def test_card_alert_only(self):
        e = {"event": "launch.detected", "tier": "boost", "source": "dexscreener-boost",
             "chain": "base", "mint": "0xABC", "symbol": "BASE",
             "name": "", "url": "https://dexscreener.com/x",
             "usd_market_cap": 200000, "age_minutes": 0,
             "entry": None, "note": "alert only — paper engine prices Solana"}
        text = alerts.format_alert(e)
        self.assertIn("0xABC", text)
        self.assertIn("alert only", text)

    def test_formatter_registered(self):
        self.assertIn("launch.detected", alerts.FORMATTERS)


if __name__ == "__main__":
    unittest.main()


class RunnerJudgeTest(unittest.TestCase):
    """Strict runner confirmation: only sustained, real-demand coins confirm."""

    def _judge(self, **kw):
        base = dict(confirm_gain_mult=1.8, confirm_rising_polls=3,
                    confirm_min_m5_volume_usd=3000.0,
                    confirm_min_liquidity_usd=12000.0, confirm_min_buys_m5=5,
                    confirm_min_buy_sell_ratio=1.5,
                    confirm_window_minutes=30.0)
        base.update(kw)
        from daemon.launch_scanner import runner
        return runner.RunnerJudge(SimpleNamespace(**base))

    def _cand(self):
        from daemon.launch_scanner import runner
        return runner.new_candidate("k", "MINT1", "TKN", "Token", "solana",
                                    "pumpfun", "early", "u", time.time())

    def _stats(self, mcap, price, **kw):
        s = {"usd_market_cap": mcap, "price_usd": price,
             "usd_liquidity": 20000.0, "vol_m5": 5000.0,
             "buys_m5": 20, "sells_m5": 5, "url": "u"}
        s.update(kw)
        return s

    def _run_polls(self, judge, cand, prices, mcap0=10000.0):
        from daemon.launch_scanner import runner
        now = time.time()
        verdict = runner.WAIT
        for i, p in enumerate(prices):
            verdict = judge.update(
                cand, self._stats(mcap0 * (p / prices[0]), p), now + i * 15)
        return verdict

    def test_confirms_on_sustained_climb(self):
        from daemon.launch_scanner import runner
        judge = self._judge()
        cand = self._cand()
        verdict = self._run_polls(judge, cand, [0.01, 0.012, 0.015, 0.019])
        self.assertEqual(verdict, runner.CONFIRM)

    def test_no_confirm_without_enough_gain(self):
        from daemon.launch_scanner import runner
        judge = self._judge()
        cand = self._cand()
        verdict = self._run_polls(judge, cand, [0.01, 0.011, 0.012, 0.013])
        self.assertEqual(verdict, runner.WAIT)

    def test_no_confirm_on_weak_volume(self):
        from daemon.launch_scanner import runner
        judge = self._judge()
        cand = self._cand()
        now = time.time()
        verdict = runner.WAIT
        for i, p in enumerate([0.01, 0.013, 0.017, 0.022]):
            s = self._stats(10000 * p / 0.01, p, vol_m5=100.0)
            verdict = judge.update(cand, s, now + i * 15)
        self.assertEqual(verdict, runner.WAIT)

    def test_no_confirm_on_sell_pressure(self):
        from daemon.launch_scanner import runner
        judge = self._judge()
        cand = self._cand()
        now = time.time()
        verdict = runner.WAIT
        for i, p in enumerate([0.01, 0.013, 0.017, 0.022]):
            s = self._stats(10000 * p / 0.01, p, buys_m5=6, sells_m5=30)
            verdict = judge.update(cand, s, now + i * 15)
        self.assertEqual(verdict, runner.WAIT)

    def test_rejects_on_dump(self):
        from daemon.launch_scanner import runner
        judge = self._judge()
        cand = self._cand()
        now = time.time()
        judge.update(cand, self._stats(10000, 0.01), now)
        judge.update(cand, self._stats(15000, 0.015), now + 15)
        verdict = judge.update(cand, self._stats(5000, 0.005), now + 30)
        self.assertEqual(verdict, runner.REJECT)

    def test_rejects_when_window_expires(self):
        from daemon.launch_scanner import runner
        judge = self._judge(confirm_window_minutes=0.01)
        cand = self._cand()
        now = time.time()
        judge.update(cand, self._stats(10000, 0.01), now)
        verdict = judge.update(cand, self._stats(11000, 0.011), now + 120)
        self.assertEqual(verdict, runner.REJECT)

    def test_rejects_when_never_indexed(self):
        from daemon.launch_scanner import runner
        judge = self._judge()
        cand = self._cand()
        verdict = runner.WAIT
        for i in range(11):
            verdict = judge.update(cand, None, time.time() + i * 15)
        self.assertEqual(verdict, runner.REJECT)

    def test_upside_band(self):
        from daemon.launch_scanner.runner import upside_band
        self.assertIn("10x", upside_band(20000))
        self.assertIn("10x", upside_band(200000))
        self.assertIn("10x", upside_band(2000000))


class TwoStageFlowTest(unittest.TestCase):
    """Qualification -> silent candidate; confirmation -> alert + entry."""

    def setUp(self):
        self.sc = LaunchScanner.__new__(LaunchScanner)

    def test_consider_emits_early_alert_not_confirmed(self):
        from agent import events as event_bus
        from daemon.launch_scanner import safety
        # Strict fail-closed: the instant CA needs a LIVE safety screen —
        # flash-disabled no longer emits. Mock the screen as passing.
        # Sniper tier: threshold 0 stages everything tier-1; the flush
        # publishes highest-rank-first through the standing cap.
        d = _degen(early_safety_flash=True, sniper_tier_threshold=0.0)
        state = {"seen": {}, "degen_positions": {}, "candidates": {}}
        c = _pump_coin(usd_market_cap=15000.0)
        ok = {"passed": True, "unknown": False, "score": 90}
        with patch.object(event_bus, "publish") as pub, \
                patch.object(safety, "check_token", return_value=ok):
            self.sc._consider(SimpleNamespace(degen=d), None, state, c)
            self.assertEqual(pub.call_count, 0)  # staged, not yet sent
            self.sc._flush_sniper_pending(SimpleNamespace(degen=d), state)
        self.assertIn("MINT1", state["candidates"])
        self.assertEqual(state["candidates"]["MINT1"]["tier"], "early")
        # Tier-1: instant CA, explicitly unconfirmed...
        self.assertEqual(pub.call_count, 1)
        card = pub.call_args[0][0]
        self.assertEqual(card["event"], "launch.early")
        self.assertFalse(card["confirmed"])
        self.assertEqual(card["mint"], "MINT1")
        # ...and a second sighting does NOT re-alert.
        with patch.object(event_bus, "publish") as pub2:
            self.sc._consider(SimpleNamespace(degen=d), None, state, c)
            self.sc._flush_sniper_pending(SimpleNamespace(degen=d), state)
        pub2.assert_not_called()

    def test_consider_hold_records_skipped_digest(self):
        from agent import events as event_bus
        from daemon.launch_scanner import safety
        d = _degen(early_safety_flash=True)
        state = {"seen": {}, "degen_positions": {}, "candidates": {}}
        c = _pump_coin(usd_market_cap=15000.0)
        bad = {"passed": False, "unknown": False, "score": 1,
               "fails": ["graph insiders detected: 7 (limit 5)"]}
        with patch.object(event_bus, "publish") as pub, \
                patch.object(safety, "check_token", return_value=bad):
            self.sc._consider(SimpleNamespace(degen=d), None, state, c)
        pub.assert_not_called()
        buf = state.get("skipped_runners") or []
        self.assertEqual(len(buf), 1)
        rec = buf[0]
        self.assertEqual(rec["kind"], "launch")
        self.assertEqual(rec["mint"], "MINT1")
        self.assertEqual(rec["tier"], "early")
        self.assertEqual(rec["safety_score"], 1)

    def test_judge_confirms_runner_and_alerts(self):
        from agent import events as event_bus
        from daemon.launch_scanner import runner
        d = _degen()
        cfg = SimpleNamespace(degen=d)
        now = time.time()
        cand = runner.new_candidate("MINT1", "MINT1", "TKN", "Token",
                                    "solana", "pumpfun", "early", "u",
                                    now - 120)
        state = {"seen": {"MINT1": {"first_ts": now - 120, "alerted": False,
                                    "symbol": "TKN", "name": "Token",
                                    "chain": "solana", "source": "pumpfun",
                                    "url": "u"}},
                 "degen_positions": {}, "candidates": {"MINT1": cand}}
        stats = {"usd_market_cap": 30000.0, "price_usd": 0.03,
                 "usd_liquidity": 25000.0, "vol_m5": 8000.0,
                 "buys_m5": 30, "sells_m5": 6, "url": "u",
                 "symbol": "TKN", "name": "Token"}
        # Prime the judge: first sighting + 3 rising polls.
        judge = runner.RunnerJudge(d)
        judge.update(cand, dict(stats, usd_market_cap=12000.0,
                                price_usd=0.012), now - 90)
        judge.update(cand, dict(stats, usd_market_cap=16000.0,
                                price_usd=0.016), now - 60)
        judge.update(cand, dict(stats, usd_market_cap=22000.0,
                                price_usd=0.022), now - 30)
        cand["stats"] = stats
        cand["stats_ts"] = now  # force fresh fetch path in _verdict
        with patch.object(sources, "dexscreener_token_stats",
                           return_value=stats), \
             patch.object(event_bus, "publish") as pub, \
             patch("daemon.launch_scanner.safety.check_token",
                   return_value={"passed": True, "score": 90, "flags": [],
                                 "fails": [], "unknown": False}), \
             patch("daemon.telegram.watchlist.add_to_watchlist",
                   return_value=(True, "ok")):
            adapter = SimpleNamespace()
            adapter._price_sol_quiet = lambda s: 0.03
            adapter.submit_order = lambda req: SimpleNamespace(
                ticket=777, price=0.03)
            self.sc._judge(cfg, adapter, state)
        self.assertNotIn("MINT1", state["candidates"])
        kinds = [c[0][0]["event"] for c in pub.call_args_list]
        self.assertIn("launch.detected", kinds)
        card = pub.call_args_list[0][0][0]
        self.assertTrue(card["confirmed"])
        self.assertIn("10x", card["upside"])
        self.assertEqual(card["mint"], "MINT1")

    def test_confirmed_card_passes_event_validation(self):
        # Regression: launch.detected must declare every field _on_launch
        # sets — the bus rejects unknown fields, and the judge was
        # crashing on every real confirmation (judge failed in prod logs).
        from agent import events as event_bus
        from agent.events.bus import validate_event
        from daemon.launch_scanner import runner
        d = _degen()
        cfg = SimpleNamespace(degen=d)
        now = time.time()
        cand = runner.new_candidate("MINTV", "MINTV", "TKN", "Token",
                                    "solana", "pumpfun", "early", "u",
                                    now - 120)
        state = {"seen": {"MINTV": {"first_ts": now - 120, "alerted": False,
                                    "symbol": "TKN", "name": "Token",
                                    "chain": "solana", "source": "pumpfun",
                                    "url": "u"}},
                 "degen_positions": {}, "candidates": {"MINTV": cand}}
        stats = {"usd_market_cap": 30000.0, "price_usd": 0.03,
                 "usd_liquidity": 25000.0, "vol_m5": 8000.0,
                 "buys_m5": 30, "sells_m5": 6, "url": "u",
                 "symbol": "TKN", "name": "Token"}
        judge = runner.RunnerJudge(d)
        judge.update(cand, dict(stats, usd_market_cap=12000.0,
                                price_usd=0.012), now - 90)
        judge.update(cand, dict(stats, usd_market_cap=16000.0,
                                price_usd=0.016), now - 60)
        judge.update(cand, dict(stats, usd_market_cap=22000.0,
                                price_usd=0.022), now - 30)
        cand["stats"] = stats
        cand["stats_ts"] = now  # force fresh fetch path in _verdict
        published = []

        def validating_publish(e):
            validate_event(e)  # raises if the schema rejects the card
            published.append(e)

        with patch.object(sources, "dexscreener_token_stats",
                           return_value=stats), \
             patch.object(event_bus, "publish",
                          side_effect=validating_publish), \
             patch("daemon.launch_scanner.safety.check_token",
                   return_value={"passed": True, "score": 90, "flags": [],
                                 "fails": [], "unknown": False}), \
             patch("daemon.telegram.watchlist.add_to_watchlist",
                   return_value=(True, "ok")):
            adapter = SimpleNamespace()
            adapter._price_sol_quiet = lambda s: 0.03
            adapter.submit_order = lambda req: SimpleNamespace(
                ticket=777, price=0.03)
            self.sc._judge(cfg, adapter, state)
        kinds = [e["event"] for e in published]
        self.assertIn("launch.detected", kinds)
        card = next(e for e in published
                    if e["event"] == "launch.detected")
        self.assertTrue(card["confirmed"])
        self.assertIn("evidence", card)

    def test_judge_drops_weak_coin_silently(self):
        from agent import events as event_bus
        from daemon.launch_scanner import runner
        d = _degen()
        cfg = SimpleNamespace(degen=d)
        now = time.time()
        cand = runner.new_candidate("MINT9", "MINT9", "WEAK", "Weak",
                                    "solana", "pumpfun", "early", "u",
                                    now - 2000)  # window expired
        cand["first_mcap"] = 12000.0
        cand["peak_mcap"] = 12000.0
        state = {"seen": {}, "degen_positions": {},
                 "candidates": {"MINT9": cand}}
        stats = {"usd_market_cap": 11000.0, "price_usd": 0.011,
                 "usd_liquidity": 20000.0, "vol_m5": 5000.0,
                 "buys_m5": 20, "sells_m5": 5, "url": "u"}
        with patch.object(sources, "dexscreener_token_stats",
                           return_value=stats), \
             patch.object(event_bus, "publish") as pub:
            self.sc._judge(cfg, None, state)
        pub.assert_not_called()
        self.assertNotIn("MINT9", state["candidates"])

    def test_runner_card_shows_evidence(self):
        e = {"tier": "early", "symbol": "TKN", "name": "Token",
             "chain": "solana", "source": "pumpfun", "mint": "MINT1",
             "usd_market_cap": 30000, "age_minutes": 4.0,
             "evidence": {"gain_mult": 2.5, "rising_polls": 3,
                          "vol_m5": 8000, "liquidity": 25000,
                          "buys_m5": 30, "sells_m5": 6},
             "upside": "micro-cap — 10x = $300k", "url": "u"}
        text = alerts._launch(e)
        self.assertIn("CONFIRMED RUNNER", text)
        self.assertIn("CA: MINT1", text)
        self.assertIn("x2.5", text)
        self.assertIn("10x", text)


class FlashSpamLogstreamTest(unittest.TestCase):
    """Flash safety, spam guard, and sub-second logstream integration."""

    def setUp(self):
        self.sc = LaunchScanner.__new__(LaunchScanner)

    def _state(self):
        return {"seen": {}, "degen_positions": {}, "candidates": {}}

    def _flash_degen(self, **kw):
        d = _degen(early_safety_flash=True, **kw)
        return SimpleNamespace(degen=d)

    # -- flash safety -------------------------------------------------
    def test_flash_suppresses_hard_fail_but_keeps_candidate(self):
        from agent import events as event_bus
        from daemon.launch_scanner import safety
        state = self._state()
        c = _pump_coin(usd_market_cap=15000.0)
        bad = {"passed": False, "unknown": False, "fails": ["honeypot"]}
        with patch.object(event_bus, "publish") as pub, \
                patch.object(safety, "check_token", return_value=bad) as ck:
            self.sc._consider(self._flash_degen(), None, state, c)
        ck.assert_called_once()
        pub.assert_not_called()  # no instant CA for the hard fail
        self.assertIn("MINT1", state["candidates"])  # still tracked silently

    def test_quality_gate_holds_on_unknown(self):
        # Fail-CLOSED on unknown: no raw CA spam for unanalyzed coins.
        # They stay candidates and can still reach the confirmed stage.
        from agent import events as event_bus
        from daemon.launch_scanner import safety
        state = self._state()
        c = _pump_coin(usd_market_cap=15000.0)
        unknown = {"passed": True, "unknown": True}
        with patch.object(event_bus, "publish") as pub, \
                patch.object(safety, "check_token", return_value=unknown):
            self.sc._consider(self._flash_degen(), None, state, c)
        pub.assert_not_called()
        self.assertIn("MINT1", state["candidates"])

    def test_quality_gate_holds_low_score(self):
        from agent import events as event_bus
        from daemon.launch_scanner import safety
        state = self._state()
        c = _pump_coin(usd_market_cap=15000.0)
        low = {"passed": True, "unknown": False, "fails": [], "score": 12}
        with patch.object(event_bus, "publish") as pub, \
                patch.object(safety, "check_token", return_value=low):
            self.sc._consider(self._flash_degen(early_min_score=30.0),
                              None, state, c)
        pub.assert_not_called()
        self.assertIn("MINT1", state["candidates"])

    def test_flash_pass_allows_alert(self):
        from agent import events as event_bus
        from daemon.launch_scanner import safety
        state = self._state()
        c = _pump_coin(usd_market_cap=15000.0)
        ok = {"passed": True, "unknown": False, "score": 90}
        cfg = self._flash_degen(sniper_tier_threshold=0.0)
        with patch.object(event_bus, "publish") as pub, \
                patch.object(safety, "check_token", return_value=ok):
            self.sc._consider(cfg, None, state, c)
            self.sc._flush_sniper_pending(cfg, state)
        self.assertEqual(pub.call_count, 1)
        evt = pub.call_args[0][0]
        self.assertEqual(evt["event"], "launch.early")
        self.assertEqual(evt["safety_score"], 90)
        self.assertIn("Fast analysis passed", evt["note"])

    def test_flash_disabled_is_fail_closed(self):
        # Disabling the flash safety screen disables instant CAs
        # entirely — no screen ran, so no CA may drop. The coin stays a
        # silent candidate and can still reach the confirmed stage.
        from agent import events as event_bus
        from daemon.launch_scanner import safety
        state = self._state()
        c = _pump_coin(usd_market_cap=15000.0)
        cfg = SimpleNamespace(degen=_degen(early_safety_flash=False))
        with patch.object(event_bus, "publish") as pub, \
                patch.object(safety, "check_token",
                             side_effect=AssertionError("no screen")):
            self.sc._consider(cfg, None, state, c)
        pub.assert_not_called()
        self.assertIn("MINT1", state["candidates"])

    def test_scan_seeds_hyperliquid_baseline_silently(self):
        # An empty HL baseline + a full-book feed must seed silently —
        # never fire the whole existing book as "new listings" (the
        # 2026-09-23 alert storm: ~500 bogus listing cards).
        from agent import events as event_bus
        from daemon.launch_scanner import sources
        d = _degen()
        cfg = SimpleNamespace(degen=d)
        state = {"seen": {}, "degen_positions": {}, "candidates": {},
                 "cycle": 0}
        hl = [{"mint": None, "symbol": "AAA", "chain": "hyperliquid",
               "source": "hyperliquid-listing", "usd_market_cap": 0.0},
              {"mint": None, "symbol": "BBB", "chain": "hyperliquid",
               "source": "hyperliquid-listing", "usd_market_cap": 0.0}]
        with patch.object(sources, "pumpfun_new", return_value=[]), \
             patch.object(sources, "dexscreener_boosts", return_value=[]), \
             patch.object(sources, "hyperliquid_listings",
                          return_value=hl), \
             patch.object(event_bus, "publish") as pub:
            self.sc._scan(cfg, None, state)
        pub.assert_not_called()  # existing book seeds silently
        self.assertEqual(sorted(state["hl_tokens"]), ["AAA", "BBB"])
        # A genuinely new listing on the NEXT scan still alerts.
        hl2 = hl + [{"mint": None, "symbol": "CCC",
                     "chain": "hyperliquid",
                     "source": "hyperliquid-listing",
                     "usd_market_cap": 0.0}]
        state["cycle"] = 20  # next scan hits the HL cadence again
        with patch.object(sources, "pumpfun_new", return_value=[]), \
             patch.object(sources, "dexscreener_boosts", return_value=[]), \
             patch.object(sources, "hyperliquid_listings",
                          return_value=hl2), \
             patch.object(event_bus, "publish") as pub2:
            self.sc._scan(cfg, None, state)
        # CCC is created, skips the instant card (no CA), and the judge
        # auto-confirms a fresh listing: exactly one listing card.
        kinds = [c[0][0]["event"] for c in pub2.call_args_list]
        self.assertNotIn("launch.early", kinds)
        detected = [c[0][0] for c in pub2.call_args_list
                    if c[0][0]["event"] == "launch.detected"]
        self.assertEqual(len(detected), 1)
        self.assertEqual(detected[0]["symbol"], "CCC")

    def test_listing_without_mint_skips_instant_card(self):
        # CEX listings carry no CA — no instant card (the confirmed
        # listing card is the whole signal). Still becomes a candidate.
        from agent import events as event_bus
        from daemon.launch_scanner import safety
        state = self._state()
        state["hl_seeded"] = True
        state.setdefault("hl_tokens", ["OLD"])
        c = _pump_coin(mint=None, chain="hyperliquid",
                       source="hyperliquid-listing", symbol="HL",
                       usd_market_cap=15000.0)
        with patch.object(event_bus, "publish") as pub, \
                patch.object(safety, "check_token",
                             side_effect=AssertionError("no CA to check")):
            self.sc._consider(self._flash_degen(), None, state, c)
        pub.assert_not_called()  # no tier-1 card for a mint-less listing
        self.assertIn("hyperliquid:HL", state["candidates"])

    # -- spam guard ---------------------------------------------------
    def test_spam_guard_caps_and_queues_digest(self):
        from agent import events as event_bus
        from daemon.launch_scanner import safety
        state = self._state()
        cfg = SimpleNamespace(degen=_degen(early_safety_flash=True,
                                           early_alert_max_per_window=3,
                                           early_alert_window_seconds=300.0,
                                           sniper_tier_threshold=0.0))
        ok = {"passed": True, "unknown": False, "score": 90}
        published = []
        with patch.object(event_bus, "publish",
                          side_effect=lambda e: published.append(e)), \
                patch.object(safety, "check_token", return_value=ok):
            for i in range(5):
                c = _pump_coin(mint="MINT%d" % i, symbol="S%d" % i,
                               usd_market_cap=15000.0)
                self.sc._consider(cfg, None, state, c)
            # The end-of-cycle flush emits highest-rank-first through
            # the cap; the tripped remainder joins the digest queue.
            self.sc._flush_sniper_pending(cfg, state)
        early = [e for e in published if e["event"] == "launch.early"]
        self.assertEqual(len(early), 3)
        self.assertEqual(len(state.get("early_digest", [])), 2)

    def test_digest_flush_publishes_once_per_window(self):
        from agent import events as event_bus
        from daemon.launch_scanner import safety
        state = self._state()
        cfg = SimpleNamespace(degen=_degen(early_safety_flash=True,
                                           early_alert_max_per_window=1,
                                           early_alert_window_seconds=300.0,
                                           sniper_tier_threshold=0.0))
        ok = {"passed": True, "unknown": False, "score": 90}
        with patch.object(event_bus, "publish"), \
                patch.object(safety, "check_token", return_value=ok):
            self.sc._consider(cfg, None, state,
                              _pump_coin(mint="A", usd_market_cap=15000.0))
            self.sc._consider(cfg, None, state,
                              _pump_coin(mint="B", usd_market_cap=15000.0))
            # A wins the single cap slot; B (staged tier-1) joins the
            # digest when the cap trips.
            self.sc._flush_sniper_pending(cfg, state)
        self.assertEqual(len(state["early_digest"]), 1)
        # Window not expired yet: no digest.
        with patch.object(event_bus, "publish") as pub:
            self.sc._flush_early_digest(cfg, state)
        pub.assert_not_called()
        # Force expiry: digest fires once and resets.
        state["early_window"]["start"] = time.time() - 301
        with patch.object(event_bus, "publish") as pub:
            self.sc._flush_early_digest(cfg, state)
        self.assertEqual(pub.call_count, 1)
        card = pub.call_args[0][0]
        self.assertEqual(card["event"], "launch.early_digest")
        self.assertEqual(card["count"], 1)
        self.assertEqual(card["shown"], 1)
        self.assertEqual(card["items_safe"][0]["mint"], "B")
        self.assertEqual(state["early_digest"], [])

    # -- digest quality gate ------------------------------------------
    # The spam-guard digest ALSO drops CAs — so the quality gate must run
    # before digest queuing, not just before the instant card. Unknown /
    # failed / low-score coins can NEVER enter the digest.
    def _tripped_degen(self, **kw):
        # Rate limit of 1: the first quality-passed coin goes instant and
        # trips the guard; everything after takes the digest path.
        # (sniper_tier_threshold=0 stages every quality-passed coin
        # tier-1 so the trip actually consumes the cap slot.)
        d = _degen(early_safety_flash=True, early_alert_max_per_window=1,
                   early_alert_window_seconds=300.0,
                   sniper_tier_threshold=0.0, **kw)
        return SimpleNamespace(degen=d)

    def _trip_guard(self, state, cfg):
        from agent import events as event_bus
        from daemon.launch_scanner import safety
        ok = {"passed": True, "unknown": False, "score": 90}
        with patch.object(event_bus, "publish"), \
                patch.object(safety, "check_token", return_value=ok):
            self.sc._consider(cfg, None, state,
                              _pump_coin(mint="TRIP", symbol="TRIP",
                                         usd_market_cap=15000.0))
            self.sc._flush_sniper_pending(cfg, state)

    def test_digest_rejects_unknown_token(self):
        from agent import events as event_bus
        from daemon.launch_scanner import safety
        state = self._state()
        cfg = self._tripped_degen()
        self._trip_guard(state, cfg)
        unknown = {"passed": True, "unknown": True}
        with patch.object(event_bus, "publish") as pub, \
                patch.object(safety, "check_token",
                             return_value=unknown):
            self.sc._consider(cfg, None, state,
                              _pump_coin(mint="BAD1", symbol="BAD1",
                                         usd_market_cap=15000.0))
        pub.assert_not_called()  # held silently — never queued, never sent
        self.assertEqual(state.get("early_digest", []), [])
        self.assertIn("BAD1", state["candidates"])  # still tracked

    def test_digest_rejects_hard_fail_token(self):
        from agent import events as event_bus
        from daemon.launch_scanner import safety
        state = self._state()
        cfg = self._tripped_degen()
        self._trip_guard(state, cfg)
        bad = {"passed": False, "unknown": False, "fails": ["honeypot"]}
        with patch.object(event_bus, "publish") as pub, \
                patch.object(safety, "check_token", return_value=bad):
            self.sc._consider(cfg, None, state,
                              _pump_coin(mint="BAD2", symbol="BAD2",
                                         usd_market_cap=15000.0))
        pub.assert_not_called()
        self.assertEqual(state.get("early_digest", []), [])

    def test_digest_rejects_low_score_token(self):
        from agent import events as event_bus
        from daemon.launch_scanner import safety
        state = self._state()
        cfg = self._tripped_degen()
        self._trip_guard(state, cfg)
        low = {"passed": True, "unknown": False, "fails": [], "score": 12}
        with patch.object(event_bus, "publish") as pub, \
                patch.object(safety, "check_token", return_value=low):
            self.sc._consider(cfg, None, state,
                              _pump_coin(mint="BAD3", symbol="BAD3",
                                         usd_market_cap=15000.0))
        pub.assert_not_called()
        self.assertEqual(state.get("early_digest", []), [])

    def test_digest_accepts_quality_passed_token(self):
        from agent import events as event_bus
        from daemon.launch_scanner import safety
        state = self._state()
        cfg = self._tripped_degen()
        self._trip_guard(state, cfg)
        ok = {"passed": True, "unknown": False, "score": 85}
        with patch.object(event_bus, "publish") as pub, \
                patch.object(safety, "check_token", return_value=ok):
            self.sc._consider(cfg, None, state,
                              _pump_coin(mint="GOOD", symbol="GOOD",
                                         usd_market_cap=15000.0))
            # Tier-1 but the guard is tripped: the flush routes it to
            # the digest queue instead of sending.
            self.sc._flush_sniper_pending(cfg, state)
        pub.assert_not_called()  # cap-tripped: no instant card...
        digest = state.get("early_digest", [])
        self.assertEqual(len(digest), 1)  # ...but queued for the digest
        self.assertEqual(digest[0]["mint"], "GOOD")
        self.assertEqual(digest[0]["safety_score"], 85)
        self.assertIn("quality-pass", digest[0]["reason"])
        self.assertEqual(digest[0]["sniper_tier"], "sniper")
        # Flush carries the quality evidence to Telegram.
        state["early_window"]["start"] = time.time() - 301
        with patch.object(event_bus, "publish") as pub2:
            self.sc._flush_early_digest(cfg, state)
        card = pub2.call_args[0][0]
        self.assertEqual(card["event"], "launch.early_digest")
        self.assertEqual(card["items_safe"][0]["safety_score"], 85)

    # -- logstream tier ------------------------------------------------
    def test_logstream_tier_qualifies_without_mcap(self):
        d = _degen()
        c = {"mint": "NEWMINT", "chain": "solana", "source": "pumpfun-log",
             "usd_market_cap": 0.0, "extra": {}}
        tier = self.sc._qualify(d, c, {"first_ts": time.time(),
                                       "mcap_history": []}, time.time(), {})
        self.assertEqual(tier, "logstream")

    def test_drain_logstream_feeds_consider(self):
        from agent import events as event_bus
        from daemon.launch_scanner import logstream, safety
        state = self._state()
        cfg = SimpleNamespace(degen=_degen(early_safety_flash=True,
                                           sniper_tier_threshold=0.0))
        ls = logstream.LogStream.__new__(logstream.LogStream)
        import queue as _q
        ls._q = _q.Queue()
        ls._q.put({"mint": "LOGMINT1", "signature": "sig1",
                   "ts": time.time()})
        self.sc._logstream = ls
        ok = {"passed": True, "unknown": False, "score": 90}
        with patch.object(event_bus, "publish") as pub, \
                patch.object(safety, "check_token", return_value=ok):
            self.sc._drain_logstream(cfg, None, state)
            self.sc._flush_sniper_pending(cfg, state)
        self.assertIn("LOGMINT1", state["candidates"])
        self.assertEqual(state["candidates"]["LOGMINT1"]["tier"], "logstream")
        card = pub.call_args[0][0]
        self.assertEqual(card["event"], "launch.early")
        self.assertEqual(card["mint"], "LOGMINT1")


class SafetyTest(unittest.TestCase):
    def _rug(self, **kw):
        d = {"rugged": False, "mintAuthority": None, "freezeAuthority": None,
             "risks": [], "score_normalised": 85, "topHolders": [],
             "graphInsidersDetected": False}
        d.update(kw)
        return d

    def test_clean_coin_passes(self):
        from daemon.launch_scanner import safety
        with patch.object(safety, "_get_json", return_value=self._rug()):
            r = safety.check_token("solana", "MINT1")
        self.assertTrue(r["passed"])
        self.assertFalse(r["unknown"])

    def test_mint_authority_fails(self):
        from daemon.launch_scanner import safety
        with patch.object(safety, "_get_json",
                           return_value=self._rug(mintAuthority="abc")):
            r = safety.check_token("solana", "MINT1")
        self.assertFalse(r["passed"])
        self.assertTrue(any("mint authority" in f for f in r["fails"]))

    def test_rugged_fails(self):
        from daemon.launch_scanner import safety
        with patch.object(safety, "_get_json",
                           return_value=self._rug(rugged=True)):
            r = safety.check_token("solana", "MINT1")
        self.assertFalse(r["passed"])

    def test_danger_risk_fails(self):
        from daemon.launch_scanner import safety
        rep = self._rug(risks=[{"name": "Single holder 95%", "level": "danger"}])
        with patch.object(safety, "_get_json", return_value=rep):
            r = safety.check_token("solana", "MINT1")
        self.assertFalse(r["passed"])

    def test_api_down_is_unknown_not_crash(self):
        from daemon.launch_scanner import safety
        with patch.object(safety, "_get_json", return_value=None):
            r = safety.check_token("solana", "MINT1")
        self.assertTrue(r["unknown"])
        self.assertTrue(r["passed"])  # fail-open; gate decides

    def test_concentrated_holders_fail(self):
        from daemon.launch_scanner import safety
        rep = self._rug(topHolders=[{"address": "a", "pct": 85.0}])
        with patch.object(safety, "_get_json", return_value=rep):
            r = safety.check_token("solana", "MINT1")
        self.assertFalse(r["passed"])

    def test_goplus_honeypot_fails(self):
        from daemon.launch_scanner import safety
        info = {"is_honeypot": "1", "buy_tax": "2", "sell_tax": "2"}
        data = {"result": {"0xABC": info}}
        with patch.object(safety, "_get_json", return_value=data):
            r = safety.check_token("base", "0xabc")
        self.assertFalse(r["passed"])
        self.assertIn("honeypot", r["fails"])

    def test_goplus_clean_passes(self):
        from daemon.launch_scanner import safety
        info = {"is_honeypot": "0", "buy_tax": "1", "sell_tax": "1",
                "is_mintable": "0", "cannot_buy": "0",
                "cannot_sell_all": "0"}
        data = {"result": {"0xABC": info}}
        with patch.object(safety, "_get_json", return_value=data):
            r = safety.check_token("base", "0xabc")
        self.assertTrue(r["passed"])


class TrailStopTest(unittest.TestCase):
    def setUp(self):
        self.sc = LaunchScanner.__new__(LaunchScanner)

    def _pos(self, entry=0.01, price=0.01, peak=None, tier="early"):
        return {"ticket": 1, "symbol": "TKN", "entry_price_sol": entry,
                "peak_price_sol": peak if peak is not None else entry,
                "entry_ts": time.time(), "tier": tier,
                "sl": entry * 0.7, "tp": entry * 3.0}

    def test_trail_arms_and_fires(self):
        from agent import events as event_bus
        d = _degen(trail_trigger_pct=50.0, trail_drawdown_pct=30.0)
        cfg = SimpleNamespace(degen=d)
        state = {"degen_positions": {"M": self._pos()}}
        adapter = SimpleNamespace()
        adapter._price_sol_quiet = lambda s: 0.016  # +60%: peak, armed
        adapter.close_position = lambda t: 0.016
        with patch.object(event_bus, "publish"):
            self.sc._manage_exits(cfg, adapter, state)
        self.assertIn("M", state["degen_positions"])  # not yet: no pullback
        self.assertAlmostEqual(
            state["degen_positions"]["M"]["peak_price_sol"], 0.016)
        adapter._price_sol_quiet = lambda s: 0.011  # -31% from peak
        adapter.close_position = lambda t: 0.011
        with patch.object(event_bus, "publish") as pub:
            self.sc._manage_exits(cfg, adapter, state)
        self.assertNotIn("M", state["degen_positions"])
        reason = pub.call_args[0][0]["reason"]
        self.assertEqual(reason, "degen_trail_stop")

    def test_trail_does_not_fire_before_trigger(self):
        from agent import events as event_bus
        d = _degen(trail_trigger_pct=50.0, trail_drawdown_pct=30.0)
        cfg = SimpleNamespace(degen=d)
        state = {"degen_positions": {"M": self._pos()}}
        adapter = SimpleNamespace()
        adapter._price_sol_quiet = lambda s: 0.009  # -10%, above SL
        with patch.object(event_bus, "publish") as pub:
            self.sc._manage_exits(cfg, adapter, state)
        pub.assert_not_called()
        self.assertIn("M", state["degen_positions"])


class ScoreboardTest(unittest.TestCase):
    def test_summarize(self):
        from daemon.launch_scanner import scoreboard
        closes = [
            {"reason": "degen_take_profit", "profit": 0.05,
             "degen_tier": "early"},
            {"reason": "degen_stop_loss", "profit": -0.015,
             "degen_tier": "early"},
            {"reason": "degen_trail_stop", "profit": 0.02,
             "degen_tier": "velocity"},
        ]
        s = scoreboard.summarize(closes)
        self.assertEqual(s["trades"], 3)
        self.assertEqual(s["wins"], 2)
        self.assertAlmostEqual(s["win_rate_pct"], 66.7, places=1)
        self.assertAlmostEqual(s["total_pnl_sol"], 0.055)
        self.assertAlmostEqual(s["profit_factor"], 0.07 / 0.015, places=2)
        self.assertEqual(s["by_tier"]["early"]["trades"], 2)
        card = scoreboard.format_card(s)
        self.assertIn("DEGEN SCOREBOARD", card)
        self.assertIn("win rate", card)

    def test_empty(self):
        from daemon.launch_scanner import scoreboard
        s = scoreboard.summarize([])
        self.assertEqual(s["trades"], 0)
        self.assertEqual(s["profit_factor"], 0.0)


class BatchStatsTest(unittest.TestCase):
    def test_batch_maps_by_mint(self):
        from daemon.launch_scanner import sources
        pair_a = {"baseToken": {"address": "MINT_A", "symbol": "A"},
                  "dexId": "raydium", "chainId": "solana",
                  "pairAddress": "P1",
                  "liquidity": {"usd": 5000}, "marketCap": 100000,
                  "priceUsd": "0.001", "volume": {"m5": 4000},
                  "txns": {"m5": {"buys": 9, "sells": 3}}}
        pair_b = dict(pair_a)
        pair_b["baseToken"] = {"address": "MINT_B", "symbol": "B"}
        pair_b["liquidity"] = {"usd": 9000}
        with patch.object(sources, "_get_json",
                           return_value=[pair_a, pair_b]):
            out = sources.dexscreener_batch_stats(
                ["MINT_A", "MINT_B", "MINT_MISSING"], "solana")
        self.assertEqual(set(out), {"MINT_A", "MINT_B"})
        self.assertEqual(out["MINT_A"]["symbol"], "A")
        self.assertEqual(out["MINT_A"]["buys_m5"], 9)
        self.assertEqual(out["MINT_B"]["usd_liquidity"], 9000)

    def test_batch_empty_and_bad(self):
        from daemon.launch_scanner import sources
        self.assertEqual(sources.dexscreener_batch_stats([], "solana"), {})
        with patch.object(sources, "_get_json", return_value=None):
            self.assertEqual(
                sources.dexscreener_batch_stats(["X"], "solana"), {})


class BundleTest(unittest.TestCase):
    def _rpc(self, method, params):
        if method == "getTokenLargestAccounts":
            return {"value": [
                {"address": "H1", "uiAmount": 40.0},
                {"address": "H2", "uiAmount": 35.0},
                {"address": "H3", "uiAmount": 25.0}]}
        if method == "getSignaturesForAddress":
            holder = params[0]
            return [{"signature": "sig-%s-new" % holder},
                    {"signature": "sig-%s-old" % holder}]
        if method == "getTransaction":
            sig = params[0]
            holder = sig.replace("sig-", "").replace("-old", "")
            # H1 and H2 funded by same wallet -> cluster 75% of top.
            funder = "FUNDER_X" if holder in ("H1", "H2") else "FUNDER_Y"
            return {
                "transaction": {"message": {"instructions": [
                    {"parsed": {"type": "transfer",
                                "info": {"source": funder,
                                         "destination": holder}}}]}},
                "meta": {"innerInstructions": []}}
        return None

    def test_clustered_supply_fails(self):
        from daemon.launch_scanner import bundle
        with patch.object(bundle, "_rpc", side_effect=self._rpc), \
             patch.object(bundle.time, "sleep"):
            r = bundle.analyze("MINT", max_cluster_pct=30.0)
        self.assertFalse(r["passed"])
        self.assertAlmostEqual(r["cluster_pct"], 75.0)
        self.assertTrue(r["fails"])

    def test_no_shared_funder_passes(self):
        from daemon.launch_scanner import bundle

        def rpc(method, params):
            if method == "getTokenLargestAccounts":
                return {"value": [{"address": "H1", "uiAmount": 40.0},
                                  {"address": "H2", "uiAmount": 35.0}]}
            if method == "getSignaturesForAddress":
                return [{"signature": "sig-%s" % params[0]}]
            if method == "getTransaction":
                holder = params[0].replace("sig-", "")
                return {
                    "transaction": {"message": {"instructions": [
                        {"parsed": {
                            "type": "transfer",
                            "info": {"source": "UNIQUE-" + holder,
                                     "destination": holder}}}]}},
                    "meta": {"innerInstructions": []}}
            return None

        with patch.object(bundle, "_rpc", side_effect=rpc), \
             patch.object(bundle.time, "sleep"):
            r = bundle.analyze("MINT", max_cluster_pct=30.0)
        self.assertTrue(r["passed"])
        self.assertEqual(r["cluster_pct"], 0.0)

    def test_no_holder_data_is_unknown(self):
        from daemon.launch_scanner import bundle
        with patch.object(bundle, "_rpc", return_value=None):
            r = bundle.analyze("MINT")
        self.assertTrue(r["unknown"])
        self.assertTrue(r["passed"])


class DailyBreakerTest(unittest.TestCase):
    def test_daily_pnl(self):
        from daemon.launch_scanner import scoreboard
        closes = [
            {"profit": 0.05, "_ts": "2026-09-23T08:00:00+00:00"},
            {"profit": -0.02, "_ts": "2026-09-23T09:00:00+00:00"},
            {"profit": -0.50, "_ts": "2026-09-22T09:00:00+00:00"},
        ]
        day_start = __import__("datetime").datetime(
            2026, 9, 23, tzinfo=__import__("datetime").timezone.utc
        ).timestamp()
        pnl = scoreboard.daily_pnl_sol(closes, day_start)
        self.assertAlmostEqual(pnl, 0.03)

    def test_deployer_flag(self):
        from daemon.launch_scanner import safety
        rep = {"rugged": False, "mintAuthority": None,
               "freezeAuthority": None, "risks": [],
               "score_normalised": 90, "topHolders": [],
               "graphInsidersDetected": False,
               "creatorTokens": [{"mint": "x"}] * 12}
        with patch.object(safety, "_get_json", return_value=rep):
            r = safety.check_token("solana", "MINT1")
        self.assertTrue(r["passed"])
        self.assertTrue(any("deployer" in f for f in r["flags"]))
        rep["creatorTokens"] = [{"mint": "x"}] * 50
        with patch.object(safety, "_get_json", return_value=rep):
            r = safety.check_token("solana", "MINT1")
        self.assertFalse(r["passed"])


# ----------------------------------------------------------------------
# regression tests for the 2026-09-23 hardening pass
# ----------------------------------------------------------------------
class RunnerCadenceTest(unittest.TestCase):
    """Duplicate/cached pair-stats polls must be neutral — they must not
    reset the rising streak, or real runners can never confirm."""

    def _judge(self):
        from daemon.launch_scanner import runner
        return runner.RunnerJudge(SimpleNamespace(
            confirm_gain_mult=1.8, confirm_rising_polls=3,
            confirm_min_m5_volume_usd=3000.0,
            confirm_min_liquidity_usd=12000.0, confirm_min_buys_m5=5,
            confirm_min_buy_sell_ratio=1.5, confirm_window_minutes=30.0))

    def _cand(self):
        from daemon.launch_scanner import runner
        return runner.new_candidate("k", "MINT1", "TKN", "Token", "solana",
                                    "pumpfun", "early", "u", time.time())

    def _stats(self, mcap, price):
        return {"usd_market_cap": mcap, "price_usd": price,
                "usd_liquidity": 20000.0, "vol_m5": 5000.0,
                "buys_m5": 20, "sells_m5": 5, "url": "u"}

    def test_duplicate_poll_does_not_reset_streak(self):
        from daemon.launch_scanner import runner
        judge, cand = self._judge(), self._cand()
        now = time.time()
        judge.update(cand, self._stats(10000.0, 0.010), now)       # seed
        judge.update(cand, self._stats(12000.0, 0.012), now + 15)  # streak=1
        self.assertEqual(cand["rising_streak"], 1)
        v = judge.update(cand, self._stats(12000.0, 0.012), now + 30)  # dup
        self.assertEqual(v, runner.WAIT)
        self.assertEqual(cand["rising_streak"], 1)  # neutral, not reset
        v = judge.update(cand, self._stats(15000.0, 0.015), now + 45)
        self.assertEqual(cand["rising_streak"], 2)
        v = judge.update(cand, self._stats(19000.0, 0.019), now + 60)
        self.assertEqual(v, runner.CONFIRM)

    def test_genuinely_flat_poll_still_resets(self):
        from daemon.launch_scanner import runner
        judge, cand = self._judge(), self._cand()
        now = time.time()
        judge.update(cand, self._stats(10000.0, 0.010), now)
        judge.update(cand, self._stats(12000.0, 0.012), now + 15)
        # Same price but different mcap = genuinely flat, not a dup.
        judge.update(cand, self._stats(12500.0, 0.012), now + 30)
        self.assertEqual(cand["rising_streak"], 0)


class HyperliquidQualifyTest(unittest.TestCase):
    """No NameError; first scan seeds the baseline silently; only truly
    new listings alert afterwards."""

    def setUp(self):
        self.sc = LaunchScanner.__new__(LaunchScanner)

    def _listing(self, sym):
        return {"mint": None, "symbol": sym, "name": sym,
                "chain": "hyperliquid", "source": "hyperliquid-listing",
                "created_ts": 0.0, "usd_market_cap": 0.0,
                "extra": {}, "url": "u"}

    def _entry(self):
        return {"first_ts": time.time(), "mcap": [], "alerted": False,
                "symbol": "", "name": "", "chain": "hyperliquid",
                "source": "hyperliquid-listing", "url": "u"}

    def test_first_sighting_seeds_silently(self):
        d = _degen()
        state = {}
        tier = self.sc._qualify(d, self._listing("PURR"), self._entry(),
                               time.time(), state)
        self.assertIsNone(tier)  # baseline — no alert on startup
        self.assertIn("PURR", state["hl_tokens"])

    def test_new_listing_after_seed_alerts_once(self):
        d = _degen()
        state = {"hl_tokens": ["PURR"], "hl_seeded": True}
        tier = self.sc._qualify(d, self._listing("HYPE"), self._entry(),
                               time.time(), state)
        self.assertEqual(tier, "listing")
        tier2 = self.sc._qualify(d, self._listing("HYPE"), self._entry(),
                                time.time(), state)
        self.assertIsNone(tier2)  # already known


class SourcesRetryTest(unittest.TestCase):
    """Flaky upstreams get bounded retries; permanent failure -> []."""

    def _resp(self, body: bytes):
        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return body
        return R()

    def test_get_json_retries_incomplete_read(self):
        import http.client
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise http.client.IncompleteRead("partial")
            return self._resp(b'{"ok": true}')

        with patch.object(sources, "_BACKOFF_S", (0, 0)), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            out = sources._get_json("https://x.test/y")
        self.assertEqual(out, {"ok": True})
        self.assertEqual(len(calls), 2)

    def test_get_json_gives_up_after_budget(self):
        import http.client

        def always_fail(req, timeout=None):
            raise http.client.RemoteDisconnected("gone")

        with patch.object(sources, "_BACKOFF_S", (0, 0)), \
             patch("urllib.request.urlopen", side_effect=always_fail):
            out = sources._get_json("https://x.test/y")
        self.assertIsNone(out)


class BundleAnalyzeTest(unittest.TestCase):
    """Owner resolution, total-supply denominator, and the time budget."""

    def _rpc_script(self):
        from daemon.launch_scanner import bundle

        def fake_rpc(method, params):
            if method == "getTokenLargestAccounts":
                return {"value": [
                    {"address": "ATA1", "uiAmount": 200000.0},
                    {"address": "ATA2", "uiAmount": 200000.0},
                ]}
            if method == "getAccountInfo":
                acct = params[0]
                owner = {"ATA1": "OWNER1", "ATA2": "OWNER2"}[acct]
                return {"value": {"data": {"parsed": {"info": {
                    "owner": owner}}}}}
            if method == "getTokenSupply":
                return {"value": {"uiAmount": 1_000_000.0}}
            if method == "getSignaturesForAddress":
                return [{"signature": "sig-of-" + params[0]}]
            if method == "getTransaction":
                sig = params[0]
                dest = sig.replace("sig-of-", "")
                return {"transaction": {"message": {"instructions": [
                    {"parsed": {"type": "transfer",
                                "info": {"source": "FUNDER",
                                         "destination": dest}}}]}},
                        "meta": {"innerInstructions": []}}
            return None

        return patch.object(bundle, "_rpc", side_effect=fake_rpc)

    def test_shared_funder_cluster_uses_total_supply(self):
        from daemon.launch_scanner import bundle
        bundle._OWNER_CACHE.clear()
        with self._rpc_script():
            out = bundle.analyze("MINT1", max_cluster_pct=30.0)
        # 400k of 1M total supply = 40% clustered -> bundled -> FAIL.
        # (Against the old top-N denominator this would read 100%.)
        self.assertFalse(out["passed"])
        self.assertAlmostEqual(out["cluster_pct"], 40.0)
        self.assertEqual(out["total_supply"], 1_000_000.0)
        self.assertTrue(any(c["wallets"] == 2 for c in out["clusters"]))

    def test_budget_expiry_reports_unknown(self):
        from daemon.launch_scanner import bundle
        bundle._OWNER_CACHE.clear()
        fake_time = unittest.mock.MagicMock()
        # deadline computed at t=100 (+45s); first budget check at t=200.
        fake_time.time.side_effect = [100.0, 200.0]
        with self._rpc_script(), \
             patch("daemon.launch_scanner.bundle.time", fake_time):
            out = bundle.analyze("MINT1")
        self.assertTrue(out["unknown"])
        self.assertTrue(any("timed out" in f for f in out["flags"]))


class EarlyAlertCardTest(unittest.TestCase):
    def test_early_card_marks_unconfirmed_and_carries_ca(self):
        e = {"event": "launch.early", "tier": "early", "symbol": "TKN",
             "name": "Token", "chain": "solana", "source": "pumpfun",
             "mint": "MINT1", "usd_market_cap": 15000, "age_minutes": 0.5,
             "url": "u"}
        text = alerts.format_alert(e)
        self.assertIn("EARLY DETECT", text)
        self.assertIn("CA: MINT1", text)
        self.assertIn("UNCONFIRMED", text)
        self.assertNotIn("CONFIRMED RUNNER", text)

    def test_early_card_without_mint(self):
        e = {"event": "launch.early", "tier": "listing", "symbol": "HYPE",
             "chain": "hyperliquid", "source": "hyperliquid-listing",
             "mint": None, "usd_market_cap": 0, "age_minutes": 1.0,
             "url": "u"}
        text = alerts.format_alert(e)
        self.assertIn("no contract address", text)


class LogstreamRpcFailoverTest(unittest.TestCase):
    """Multi-RPC failover for the logstream's transaction fetches."""

    def _ls(self, urls):
        from daemon.launch_scanner import logstream
        return logstream.LogStream(http_rpcs=urls)

    def _ok_resp(self, result):
        import io, json

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"jsonrpc": "2.0", "id": 1,
                                   "result": result}).encode()

        return Resp()

    def test_default_pool_has_multiple_endpoints(self):
        from daemon.launch_scanner import logstream
        ls = logstream.LogStream()
        self.assertGreaterEqual(len(ls._http_rpc_urls), 2)
        self.assertEqual(ls._http_rpc_urls[0],
                         logstream.HTTP_RPC_URLS[0])

    def test_single_url_backcompat(self):
        from daemon.launch_scanner import logstream
        ls = logstream.LogStream(http_rpc="https://custom.example/rpc")
        self.assertEqual(ls._http_rpc_urls, ["https://custom.example/rpc"])

    def test_failover_to_second_url(self):
        import urllib.error
        from daemon.launch_scanner import logstream
        ls = self._ls(["https://dead.example", "https://live.example"])
        seen = []

        def fake_urlopen(req, timeout=None):
            seen.append(req.full_url)
            if "dead" in req.full_url:
                raise urllib.error.URLError("boom")
            return self._ok_resp({"ok": True})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = ls._http_rpc("getTransaction", ["sig"])
        self.assertEqual(res, {"ok": True})
        self.assertTrue(any("dead" in u for u in seen))
        self.assertTrue(any("live" in u for u in seen))
        # Dead endpoint penalized; live one becomes healthiest and active.
        self.assertEqual(ls.rpc_urls[0], "https://live.example")
        self.assertEqual(ls._rpc_active, "https://live.example")

    def test_all_endpoints_fail_returns_none(self):
        import urllib.error
        ls = self._ls(["https://dead1.example", "https://dead2.example"])

        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("down")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with patch.object(ls, "_note_rpc_failure") as noted:
                res = ls._http_rpc("getTransaction", ["sig"])
        self.assertIsNone(res)
        noted.assert_called_once()

    def test_config_carries_rpc_pool(self):
        from config.config import DegenConfig
        urls = DegenConfig().logstream_rpc_urls
        self.assertIsInstance(urls, list)
        self.assertGreaterEqual(len(urls), 2)

    def test_rpc_label_redacts_api_key(self):
        from daemon.launch_scanner import logstream
        url = "https://mainnet.helius-rpc.com/?api-key=SUPERSECRET"
        self.assertNotIn("SUPERSECRET", logstream._rpc_label(url))
        self.assertEqual(logstream._rpc_label(url),
                         "https://mainnet.helius-rpc.com/")

    def test_helius_url_picked_up_from_key_file(self):
        import os
        import tempfile
        from daemon.launch_scanner import _helius_rpc_url
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "helius"))
            with open(os.path.join(home, "helius", "api_key"), "w") as f:
                f.write("TESTKEY123")
            with patch.dict(os.environ, {"FOREX_AGENT_HOME": home}):
                url = _helius_rpc_url()
        self.assertEqual(url,
                         "https://mainnet.helius-rpc.com/?api-key=TESTKEY123")

    def test_helius_url_none_when_no_key(self):
        import os
        import tempfile
        from daemon.launch_scanner import _helius_rpc_url
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"FOREX_AGENT_HOME": home}):
                self.assertIsNone(_helius_rpc_url())

    def test_ensure_logstream_prefers_helius(self):
        import os
        import tempfile
        from types import SimpleNamespace
        from daemon.launch_scanner import LaunchScanner, logstream
        sc = LaunchScanner.__new__(LaunchScanner)
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "helius"))
            with open(os.path.join(home, "helius", "api_key"), "w") as f:
                f.write("K")
            cfg = SimpleNamespace(
                degen=SimpleNamespace(logstream_enabled=True,
                                      logstream_rpc_urls=["https://a.example"]))
            with patch.dict(os.environ, {"FOREX_AGENT_HOME": home}):
                with patch.object(logstream.LogStream, "start"):
                    sc._ensure_logstream(cfg)
        ls = sc._logstream
        self.assertTrue(
            ls._http_rpc_urls[0].startswith("https://mainnet.helius-rpc.com/"))
        self.assertIn("https://a.example", ls._http_rpc_urls)


class SeenPruneTest(unittest.TestCase):
    """state['seen'] must not grow without bound (2026-09-29: 228k entries,
    102MB state dumps every ~90s stalling the SQLite backup API)."""

    def _state(self, now):
        return {"seen": {
            # fresh: active within retention
            "FRESH": {"first_ts": now - 3600, "alerted": True,
                      "mcap": [[now - 600, 50000.0]]},
            # stale: idle 5 days, retention 48h -> evicted
            "STALE": {"first_ts": now - 5 * 86400, "alerted": True,
                      "mcap": [[now - 5 * 86400, 12000.0]]},
            # old first_ts but recent mcap activity -> kept
            "ACTIVE": {"first_ts": now - 5 * 86400, "alerted": False,
                       "mcap": [[now - 300, 20000.0]]},
            # bare alerted marker, no timestamps -> stamped, kept
            "MARKER": {"alerted": True},
        }}

    def test_prune_evicts_only_stale(self):
        now = time.time()
        degen = _degen(seen_retention_hours=48.0, seen_max_entries=100000)
        state = self._state(now)
        removed = LaunchScanner._prune_seen(state, degen, now)
        self.assertEqual(removed, 1)
        self.assertIn("FRESH", state["seen"])
        self.assertIn("ACTIVE", state["seen"])
        self.assertIn("MARKER", state["seen"])
        self.assertNotIn("STALE", state["seen"])
        # bare marker got a timestamp so it ages out naturally later
        self.assertAlmostEqual(state["seen"]["MARKER"]["first_ts"], now,
                               delta=5.0)

    def test_prune_hard_cap_keeps_most_recent(self):
        now = time.time()
        degen = _degen(seen_retention_hours=48.0, seen_max_entries=3)
        state = {"seen": {
            "A": {"first_ts": now - 100, "alerted": True},
            "B": {"first_ts": now - 200, "alerted": True},
            "C": {"first_ts": now - 300, "alerted": True},
            "D": {"first_ts": now - 400, "alerted": True},
        }}
        removed = LaunchScanner._prune_seen(state, degen, now)
        self.assertEqual(removed, 1)
        self.assertNotIn("D", state["seen"])
        self.assertEqual(len(state["seen"]), 3)

    def test_prune_tolerates_missing_or_bad_seen(self):
        now = time.time()
        degen = _degen()
        self.assertEqual(LaunchScanner._prune_seen({}, degen, now), 0)
        self.assertEqual(
            LaunchScanner._prune_seen({"seen": "nope"}, degen, now), 0)
        # non-dict entries and garbage timestamps don't crash the prune
        state = {"seen": {"X": None, "Y": {"first_ts": "garbage"}}}
        LaunchScanner._prune_seen(state, degen, now)
