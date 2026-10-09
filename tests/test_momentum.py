"""Tests for the established-coin momentum scanner.

Covers: GeckoTerminal trending-pool normalization, the qualification
gates (incl. the anti-top-buying guard and stablecoin exclusions),
cooldown semantics, config-name alignment, the momentum.alert event
schema + Telegram card, and the two-stage alert/enter wiring — with the
critical property that a safety-held coin NEVER publishes, NEVER burns
cooldown, and NEVER paper-enters.
"""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.events import bus as event_bus
from daemon.launch_scanner import LaunchScanner, momentum, sources
from daemon.telegram import alerts


def _degen(**kw):
    base = dict(
        momentum_enabled=True,
        momentum_scan_seconds=300,
        momentum_networks=["solana"],
        momentum_min_gain_24h=20.0,
        momentum_max_gain_24h=300.0,
        momentum_max_drawdown_1h=15.0,
        momentum_min_vol_24h_usd=100000.0,
        momentum_min_liq_usd=50000.0,
        momentum_cooldown_hours=6.0,
        early_min_score=50.0,
        early_safety_flash=True,
        early_flash_timeout_s=4.0,
        early_alert_max_per_window=10,
        early_alert_window_seconds=300.0,
        sentiment_enabled=False,
        auto_enter=True,
        max_positions=5,
        stop_loss_pct=30.0,
        take_profit_pct=200.0,
        entry_sol=0.05,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _config(**kw):
    return SimpleNamespace(degen=_degen(**kw))


def _pool(**kw):
    p = {"pool_address": "pool1", "mint": "MINT1", "symbol": "FWOG",
         "name": "Fwog", "chain": "solana", "chg_24h_pct": 25.0,
         "chg_1h_pct": 5.0, "vol_24h_usd": 200000.0, "liq_usd": 80000.0,
         "usd_market_cap": 5000000.0,
         "url": "https://www.geckoterminal.com/solana/pools/pool1"}
    p.update(kw)
    return p


class TrendingSourceTest(unittest.TestCase):
    def test_normalizes_gecko_payload(self):
        payload = {"data": [{
            "attributes": {
                "address": "pool9",
                "name": "NEET / SOL",
                "price_change_percentage": {"h1": 3.5, "h24": 42.0},
                "volume_usd": {"h24": 250000.0},
                "reserve_in_usd": 120000.0,
                "market_cap_usd": 8000000.0,
            },
            "relationships": {
                "base_token": {"data": {"id": "solana_MINT9"}},
            },
        }]}
        with patch.object(sources, "_get_json", return_value=payload):
            pools = sources.geckoterminal_trending_pools("solana")
        self.assertEqual(len(pools), 1)
        p = pools[0]
        self.assertEqual(p["mint"], "MINT9")
        self.assertEqual(p["symbol"], "NEET")
        self.assertEqual(p["chg_24h_pct"], 42.0)
        self.assertEqual(p["chg_1h_pct"], 3.5)
        self.assertEqual(p["vol_24h_usd"], 250000.0)
        self.assertEqual(p["liq_usd"], 120000.0)
        self.assertEqual(p["usd_market_cap"], 8000000.0)
        self.assertIn("geckoterminal.com", p["url"])

    def test_eth_network_maps_to_ethereum_chain(self):
        # The safety gate keys EVM checks off "ethereum" — a raw "eth"
        # chain would come back safety-unknown and be held silently.
        payload = {"data": [{
            "attributes": {"address": "poolE", "name": "PEPE / WETH",
                           "price_change_percentage": {"h1": 2.0,
                                                       "h24": 30.0},
                           "volume_usd": {"h24": 500000.0},
                           "reserve_in_usd": 200000.0},
            "relationships": {
                "base_token": {"data": {"id": "eth_0xABC"}}},
        }]}
        with patch.object(sources, "_get_json", return_value=payload):
            pools = sources.geckoterminal_trending_pools("eth")
        self.assertEqual(pools[0]["chain"], "ethereum")
        self.assertEqual(pools[0]["mint"], "0xABC")

    def test_base_network_chain(self):
        payload = {"data": [{
            "attributes": {"address": "poolB", "name": "BRETT / WETH",
                           "price_change_percentage": {"h1": 1.0,
                                                       "h24": 28.0},
                           "volume_usd": {"h24": 300000.0},
                           "reserve_in_usd": 150000.0},
            "relationships": {
                "base_token": {"data": {"id": "base_0xDEF"}}},
        }]}
        with patch.object(sources, "_get_json", return_value=payload):
            pools = sources.geckoterminal_trending_pools("base")
        self.assertEqual(pools[0]["chain"], "base")

    def test_fetch_failure_returns_empty(self):
        with patch.object(sources, "_get_json",
                          side_effect=RuntimeError("down")):
            self.assertEqual(sources.geckoterminal_trending_pools(), [])

    def test_skips_pool_without_base_token(self):
        payload = {"data": [{"attributes": {"name": "X / SOL"},
                             "relationships": {}}]}
        with patch.object(sources, "_get_json", return_value=payload):
            self.assertEqual(sources.geckoterminal_trending_pools(), [])


class QualifyTest(unittest.TestCase):
    def test_runner_passes(self):
        s = momentum.MomentumScanner()
        r = s.qualify(_degen(), _pool(), now=1000.0)
        self.assertIsNotNone(r)
        self.assertEqual(r["symbol"], "FWOG")
        self.assertEqual(r["source"], "gecko-trending")

    def test_anti_top_guard(self):
        s = momentum.MomentumScanner()
        self.assertIsNone(s.qualify(_degen(), _pool(chg_24h_pct=500.0),
                                    now=1000.0))

    def test_weak_move_rejected(self):
        s = momentum.MomentumScanner()
        self.assertIsNone(s.qualify(_degen(), _pool(chg_24h_pct=5.0),
                                    now=1000.0))

    def test_dumping_rejected(self):
        s = momentum.MomentumScanner()
        self.assertIsNone(s.qualify(_degen(), _pool(chg_1h_pct=-30.0),
                                    now=1000.0))

    def test_thin_volume_rejected(self):
        s = momentum.MomentumScanner()
        self.assertIsNone(s.qualify(_degen(), _pool(vol_24h_usd=500.0),
                                    now=1000.0))

    def test_thin_liquidity_rejected(self):
        s = momentum.MomentumScanner()
        self.assertIsNone(s.qualify(_degen(), _pool(liq_usd=500.0),
                                    now=1000.0))

    def test_stablecoins_excluded(self):
        s = momentum.MomentumScanner()
        for sym in ("USDC", "USDT", "SOL", "WSOL", "WETH", "ETH"):
            self.assertIsNone(
                s.qualify(_degen(), _pool(symbol=sym), now=1000.0),
                sym)

    def test_missing_mint_rejected(self):
        s = momentum.MomentumScanner()
        self.assertIsNone(s.qualify(_degen(), _pool(mint=""), now=1000.0))

    def test_cooldown_blocks_then_expires(self):
        s = momentum.MomentumScanner()
        d = _degen()
        self.assertIsNotNone(s.qualify(d, _pool(), now=1000.0))
        s.mark_alerted("pool1", now=1000.0)
        self.assertIsNone(s.qualify(d, _pool(), now=2000.0))
        self.assertIsNotNone(
            s.qualify(d, _pool(), now=1000.0 + 7 * 3600))

    def test_skip_cooldown_bypasses_alert_cooldown(self):
        # The 6h cooldown gates duplicate alert CARDS only. The stage-2
        # confirmation pass re-qualifies with skip_cooldown=True so a
        # runner marked at stage 1 can still be evaluated for paper
        # entry on later scans.
        s = momentum.MomentumScanner()
        d = _degen()
        self.assertIsNotNone(s.qualify(d, _pool(), now=1000.0))
        s.mark_alerted("pool1", now=1000.0)
        self.assertIsNone(s.qualify(d, _pool(), now=2000.0))
        self.assertIsNotNone(
            s.qualify(d, _pool(), now=2000.0, skip_cooldown=True))

    def test_config_names_drive_gates(self):
        # Regression: the scanner must read the REAL degen attribute
        # names (a rename once left the YAML knobs dead).
        s = momentum.MomentumScanner()
        d = _degen(momentum_min_gain_24h=90.0)
        self.assertIsNone(s.qualify(d, _pool(chg_24h_pct=25.0),
                                    now=1000.0))
        self.assertIsNotNone(s.qualify(d, _pool(chg_24h_pct=95.0),
                                       now=1000.0))

    def test_scan_never_raises(self):
        s = momentum.MomentumScanner()
        out = s.scan(_degen(), [_pool(), None, {"junk": 1}], now=1000.0)
        self.assertEqual(len(out), 1)

    def test_lowcap_lane_tagged(self):
        s = momentum.MomentumScanner()
        r = s.qualify(_degen(), _pool(usd_market_cap=5000000.0),
                      now=1000.0)
        self.assertEqual(r["lane"], "lowcap")

    def test_established_lane_tagged(self):
        s = momentum.MomentumScanner()
        r = s.qualify(_degen(), _pool(usd_market_cap=50000000.0),
                      now=1000.0)
        self.assertEqual(r["lane"], "established")

    def test_unknown_mcap_is_established_lane(self):
        s = momentum.MomentumScanner()
        r = s.qualify(_degen(), _pool(usd_market_cap=0.0), now=1000.0)
        self.assertEqual(r["lane"], "established")

    def test_ceiling_boundary_is_lowcap(self):
        s = momentum.MomentumScanner()
        r = s.qualify(_degen(), _pool(usd_market_cap=10000000.0),
                      now=1000.0)
        self.assertEqual(r["lane"], "lowcap")

    def test_ceiling_read_from_config(self):
        s = momentum.MomentumScanner()
        d = _degen(momentum_lowcap_max_mcap_usd=1000000.0)
        r = s.qualify(d, _pool(usd_market_cap=5000000.0), now=1000.0)
        self.assertEqual(r["lane"], "established")
        r2 = s.qualify(d, _pool(usd_market_cap=500000.0), now=1000.0)
        self.assertEqual(r2["lane"], "lowcap")

    def test_below_floor_excluded(self):
        s = momentum.MomentumScanner()
        self.assertIsNone(s.qualify(_degen(), _pool(usd_market_cap=49999.0),
                                    now=1000.0))

    def test_floor_boundary_excluded(self):
        s = momentum.MomentumScanner()
        self.assertIsNone(s.qualify(_degen(), _pool(usd_market_cap=50000.0),
                                    now=1000.0))

    def test_just_above_floor_is_lowcap(self):
        s = momentum.MomentumScanner()
        r = s.qualify(_degen(), _pool(usd_market_cap=50001.0), now=1000.0)
        self.assertEqual(r["lane"], "lowcap")

    def test_floor_read_from_config(self):
        s = momentum.MomentumScanner()
        d = _degen(momentum_lowcap_min_mcap_usd=100000.0)
        self.assertIsNone(s.qualify(d, _pool(usd_market_cap=99999.0),
                                    now=1000.0))
        r = s.qualify(d, _pool(usd_market_cap=100001.0), now=1000.0)
        self.assertEqual(r["lane"], "lowcap")

    def test_unknown_mcap_still_established_with_floor(self):
        # Unknown mcap keeps the pre-lane behavior: established lane,
        # never the dust-zone exclusion.
        s = momentum.MomentumScanner()
        d = _degen(momentum_lowcap_min_mcap_usd=100000.0)
        r = s.qualify(d, _pool(usd_market_cap=0.0), now=1000.0)
        self.assertEqual(r["lane"], "established")


class WiringTest(unittest.TestCase):
    def _handle(self, **kw):
        cfg = _config(**kw.pop("degen_kw", {}))
        sc = LaunchScanner()
        state = {}
        tracked = {}
        scanner = momentum.MomentumScanner()
        published = []

        class FakeBus:
            def publish(self, e):
                published.append(e)

        r = _pool(**kw)
        with patch.object(LaunchScanner, "_instant_verdict") as iv, \
             patch.object(LaunchScanner, "_sentiment_track",
                          return_value=None):
            iv.return_value = (True, "quality-pass score 80",
                               {"score": 80, "fails": []})
            sc._momentum_handle(cfg, None, state, tracked, scanner, r,
                                1000.0, FakeBus())
        return cfg, sc, state, tracked, scanner, published, r

    def test_stage1_alert_publishes_with_score(self):
        cfg, sc, state, tracked, scanner, published, r = self._handle()
        self.assertEqual(len(published), 1)
        e = published[0]
        self.assertEqual(e["event"], "momentum.alert")
        self.assertEqual(e["symbol"], "FWOG")
        self.assertEqual(e["mint"], "MINT1")
        self.assertEqual(e["safety_score"], 80)
        self.assertEqual(e["source"], "gecko-trending")
        self.assertIn("narratives", e)
        # Cooldown consumed only because the alert went out.
        self.assertIn("pool1", scanner._last_alert)
        self.assertIn("MINT1", tracked)
        self.assertFalse(tracked["MINT1"]["entered"])

    def test_stage1_event_carries_lane(self):
        cfg, sc, state, tracked, scanner, published, r = self._handle(
            lane="lowcap")
        e = published[0]
        self.assertEqual(e["lane"], "lowcap")
        self.assertIn("Lowcap runner", e["note"])

    def test_stage1_defaults_to_established_lane(self):
        cfg, sc, state, tracked, scanner, published, r = self._handle()
        e = published[0]
        self.assertEqual(e["lane"], "established")
        self.assertIn("Established runner", e["note"])

    def test_safety_hold_publishes_nothing(self):
        cfg = _config()
        sc = LaunchScanner()
        state, tracked, scanner, r = {}, {}, momentum.MomentumScanner(), \
            _pool()
        published = []

        class FakeBus:
            def publish(self, e):
                published.append(e)

        with patch.object(LaunchScanner, "_instant_verdict") as iv, \
             patch.object(LaunchScanner, "_sentiment_track",
                          return_value=None):
            iv.return_value = (False, "safety-unknown", {"unknown": True})
            sc._momentum_handle(cfg, None, state, tracked, scanner, r,
                                1000.0, FakeBus())
        self.assertEqual(published, [])
        # Held coin burns NO cooldown and leaves NO track: it may
        # re-qualify once it scores cleanly.
        self.assertEqual(scanner._last_alert, {})
        self.assertEqual(tracked, {})

    def test_stage2_paper_entry_on_second_qualification(self):
        cfg, sc, state, tracked, scanner, published, r = self._handle()
        self.assertEqual(len(published), 1)  # stage 1 alert
        state["degen_positions"] = {}

        class FakeBus:
            def publish(self, e):
                pass

        with patch.object(LaunchScanner, "_instant_verdict") as iv, \
             patch.object(LaunchScanner, "_sentiment_track",
                          return_value=None), \
             patch.object(LaunchScanner, "_paper_enter",
                          return_value=True) as pe, \
             patch("daemon.telegram.watchlist.add_to_watchlist",
                   return_value=(True, "ok")):
            iv.return_value = (True, "quality-pass score 80",
                               {"score": 80, "fails": []})
            sc._momentum_handle(cfg, None, state, tracked, scanner, r,
                                1300.0, FakeBus())
        self.assertTrue(tracked["MINT1"]["entered"])
        self.assertEqual(pe.call_count, 1)
        # The position card carries gecko-trending for weekly attribution.
        card = pe.call_args[0][3]
        self.assertEqual(card["source"], "gecko-trending")

    def test_stage2_no_entry_when_safety_fails(self):
        cfg, sc, state, tracked, scanner, published, r = self._handle()
        state["degen_positions"] = {}

        class FakeBus:
            def publish(self, e):
                pass

        with patch.object(LaunchScanner, "_instant_verdict") as iv, \
             patch.object(LaunchScanner, "_sentiment_track",
                          return_value=None), \
             patch.object(LaunchScanner, "_paper_enter") as pe:
            iv.return_value = (False, "score 40 < 50",
                               {"score": 40, "fails": []})
            sc._momentum_handle(cfg, None, state, tracked, scanner, r,
                                1300.0, FakeBus())
        pe.assert_not_called()
        self.assertFalse(tracked["MINT1"]["entered"])

    def test_momentum_scan_respects_disabled_flag(self):
        cfg = _config(momentum_enabled=False)
        sc = LaunchScanner()
        state = {}
        with patch.object(sources, "geckoterminal_trending_pools") as gp:
            sc._momentum_scan(cfg, None, state)
        gp.assert_not_called()
        self.assertNotIn("momentum_last_scan", state)

    def test_momentum_scan_cadence(self):
        cfg = _config()
        sc = LaunchScanner()
        # Last scan just now -> the poll is skipped.
        state = {"momentum_last_scan": time.time()}
        with patch.object(sources, "geckoterminal_trending_pools") as gp:
            sc._momentum_scan(cfg, None, state)
        gp.assert_not_called()
        # Last scan long ago -> the poll runs.
        state = {"momentum_last_scan": time.time() - 3600}
        with patch.object(sources, "geckoterminal_trending_pools",
                          return_value=[]) as gp:
            sc._momentum_scan(cfg, None, state)
        gp.assert_called_once()

    def test_momentum_scan_fans_out_to_all_networks(self):
        cfg = _config(momentum_networks=["solana", "base", "eth"])
        sc = LaunchScanner()
        state = {}
        seen = []

        def fake_trending(net):
            seen.append(net)
            return [_pool(pool_address="p-" + net, mint="m-" + net,
                          symbol="SYM" + net, chain="c-" + net)]

        with patch.object(sources, "geckoterminal_trending_pools",
                          side_effect=fake_trending):
            with patch.object(LaunchScanner, "_momentum_handle") as mh:
                sc._momentum_scan(cfg, None, state)
        self.assertEqual(seen, ["solana", "base", "eth"])
        self.assertEqual(mh.call_count, 3)
        addrs = {c[0][5].get("pool_address") for c in mh.call_args_list}
        self.assertEqual(addrs, {"p-solana", "p-base", "p-eth"})

    def test_momentum_scan_survives_one_network_failing(self):
        cfg = _config(momentum_networks=["solana", "base"])
        sc = LaunchScanner()
        state = {}

        def fake_trending(net):
            if net == "base":
                raise RuntimeError("gecko down")
            return [_pool()]

        with patch.object(sources, "geckoterminal_trending_pools",
                          side_effect=fake_trending):
            with patch.object(LaunchScanner, "_momentum_handle") as mh:
                sc._momentum_scan(cfg, None, state)  # must not raise
        self.assertEqual(mh.call_count, 1)

    def test_stage2_confirmation_bypasses_cooldown(self):
        """Regression: the 6h alert cooldown must not block stage-2.

        Scan 1 fires the stage-1 alert card (no entry). Scan 2, ~7
        minutes later, must still evaluate the tracked runner for paper
        entry even though the pool sits inside its 6h cooldown — the
        cooldown suppresses duplicate CARDS only. Before the fix,
        stage 2 could never happen through the real scan path, so
        momentum paper entries were effectively dead.
        """
        cfg = _config()
        sc = LaunchScanner()
        state = {}
        pool = _pool()
        verdict = (True, "quality-pass score 80", {"score": 80,
                                                  "fails": []})

        def run_scan(now):
            with patch.object(sources, "geckoterminal_trending_pools",
                              return_value=[pool]), \
                 patch("agent.events.publish") as pub, \
                 patch.object(LaunchScanner, "_instant_verdict",
                              return_value=verdict), \
                 patch.object(LaunchScanner, "_sentiment_track",
                              return_value=None), \
                 patch.object(LaunchScanner, "_paper_enter",
                              return_value=True) as pe, \
                 patch("daemon.telegram.watchlist.add_to_watchlist",
                       return_value=(True, "ok")), \
                 patch("time.time", return_value=now):
                sc._momentum_scan(cfg, None, state)
            return pub, pe

        pub1, pe1 = run_scan(1000.0)
        # Stage 1: exactly one alert card, tracked but not entered —
        # never a one-snapshot entry.
        self.assertEqual(pub1.call_count, 1)
        self.assertFalse(state["momentum_seen"]["MINT1"]["entered"])
        pe1.assert_not_called()

        pub2, pe2 = run_scan(1400.0)  # 400s later, inside 6h cooldown
        # Stage 2: the confirmation pass evaluated the runner (cooldown
        # bypassed) and paper-entered...
        self.assertTrue(state["momentum_seen"]["MINT1"]["entered"])
        pe2.assert_called_once()
        # ...but no duplicate alert card went out (cooldown intact).
        self.assertEqual(pub2.call_count, 0)

    def test_confirmation_blocked_when_safety_fails_on_later_scan(self):
        """Safety still gates stage 2: a runner that turns risky on the
        later scan is not entered, and stays tracked for retry."""
        cfg = _config()
        sc = LaunchScanner()
        state = {}
        pool = _pool()
        good = (True, "quality-pass score 80", {"score": 80,
                                               "fails": []})
        bad = (False, "score 40 < 50", {"score": 40, "fails": []})

        def run_scan(now, verdict):
            with patch.object(sources, "geckoterminal_trending_pools",
                              return_value=[pool]), \
                 patch("agent.events.publish"), \
                 patch.object(LaunchScanner, "_instant_verdict",
                              return_value=verdict), \
                 patch.object(LaunchScanner, "_sentiment_track",
                              return_value=None), \
                 patch.object(LaunchScanner, "_paper_enter",
                              return_value=True) as pe, \
                 patch("time.time", return_value=now):
                sc._momentum_scan(cfg, None, state)
            return pe

        run_scan(1000.0, good)
        self.assertFalse(state["momentum_seen"]["MINT1"]["entered"])
        pe2 = run_scan(1400.0, bad)
        pe2.assert_not_called()
        self.assertFalse(state["momentum_seen"]["MINT1"]["entered"])


class SchemaAndCardTest(unittest.TestCase):
    def test_event_schema_registered(self):
        schema = event_bus.EVENT_SCHEMAS.get("momentum.alert")
        self.assertIsNotNone(schema)
        self.assertIn("symbol", schema["required"])
        for field in ("chg_24h_pct", "chg_1h_pct", "vol_24h_usd",
                      "liq_usd", "safety_score", "narratives",
                      "sentiment"):
            self.assertIn(field, schema["optional"])

    def test_card_renders(self):
        e = {"event": "momentum.alert", "symbol": "FWOG", "mint": "MINT1",
             "chain": "solana", "source": "gecko-trending",
             "chg_24h_pct": 25.0, "chg_1h_pct": 5.0,
             "vol_24h_usd": 200000.0, "liq_usd": 80000.0,
             "usd_market_cap": 5000000.0, "safety_score": 80,
             "narratives": ["meme"], "narrative_heat": 2.5,
             "sentiment": "bullish",
             "url": "https://www.geckoterminal.com/solana/pools/pool1"}
        text = alerts.format_alert(e)
        self.assertIsNotNone(text)
        self.assertIn("MOMENTUM RUNNER", text)
        self.assertIn("FWOG", text)
        self.assertIn("MINT1", text)
        self.assertIn("80/100", text)  # score line never omitted
        self.assertIn("meme", text)

    def test_card_without_score_says_so(self):
        e = {"event": "momentum.alert", "symbol": "FWOG",
             "safety_score": None}
        text = alerts.format_alert(e)
        self.assertIn("no safety report", text)

    def test_lane_in_schema(self):
        schema = event_bus.EVENT_SCHEMAS.get("momentum.alert")
        self.assertIn("lane", schema["optional"])

    def test_lowcap_card_title(self):
        e = {"event": "momentum.alert", "symbol": "FWOG", "lane": "lowcap",
             "safety_score": 80}
        text = alerts.format_alert(e)
        self.assertIn("LOWCAP MOMENTUM", text)
        self.assertNotIn("MOMENTUM RUNNER", text)

    def test_established_card_title_unchanged(self):
        e = {"event": "momentum.alert", "symbol": "FWOG",
             "safety_score": 80}
        text = alerts.format_alert(e)
        self.assertIn("MOMENTUM RUNNER", text)
        self.assertNotIn("LOWCAP", text)

    def test_copy_ca_markup(self):
        mk = alerts.format_alert_markup({"event": "momentum.alert",
                                         "mint": "MINT1"})
        self.assertIsNotNone(mk)
        btn = mk["inline_keyboard"][0][0]
        self.assertEqual(btn["copy_text"]["text"], "MINT1")


class SkippedDigestTest(unittest.TestCase):
    """The skipped-runners digest: recording, bounds, and hooks."""

    def test_record_skipped_dedups_repeats(self):
        state = {}
        for _ in range(3):
            LaunchScanner._record_skipped(
                state, "momentum", "TKN", "MINT1", "solana",
                "safety-fail: graph insiders detected: 7 (limit 5)",
                {"chg_24h_pct": 460.0, "safety_score": 1})
        buf = state["skipped_runners"]
        self.assertEqual(len(buf), 1)
        self.assertEqual(buf[0]["hits"], 3)
        self.assertEqual(buf[0]["kind"], "momentum")
        self.assertEqual(buf[0]["chg_24h_pct"], 460.0)
        self.assertEqual(buf[0]["safety_score"], 1)
        self.assertIn("insiders", buf[0]["reason"])

    def test_record_skipped_bounds_and_prunes(self):
        state = {}
        for i in range(250):
            LaunchScanner._record_skipped(
                state, "launch", "T%d" % i, "MINT%d" % i, "solana",
                "held", None)
        self.assertEqual(len(state["skipped_runners"]), 200)
        # A record older than 7 days is pruned on the next write.
        old = state["skipped_runners"][0]
        old["ts"] = time.time() - 8 * 86400
        LaunchScanner._record_skipped(
            state, "launch", "NEW", "MINTNEW", "solana", "held", None)
        mints = [r["mint"] for r in state["skipped_runners"]]
        self.assertNotIn(old["mint"], mints)
        self.assertIn("MINTNEW", mints)

    def test_record_skipped_never_raises(self):
        LaunchScanner._record_skipped(None, "x", "y", "z", "w", "r", None)
        LaunchScanner._record_skipped(
            {"skipped_runners": ["garbage"]}, "x", "y", "z", "w", "r", None)

    def test_momentum_hold_records_skipped_no_publish(self):
        cfg = _config()
        sc = LaunchScanner()
        state, tracked, scanner, r = {}, {}, momentum.MomentumScanner(), \
            _pool(chg_24h_pct=180.0)
        published = []

        class FakeBus:
            def publish(self, e):
                published.append(e)

        with patch.object(LaunchScanner, "_instant_verdict") as iv, \
             patch.object(LaunchScanner, "_sentiment_track",
                          return_value=None):
            iv.return_value = (
                False, "safety-fail: graph insiders detected: 7 (limit 5)",
                {"score": 1, "fails": ["graph insiders"]})
            sc._momentum_handle(cfg, None, state, tracked, scanner, r,
                                1000.0, FakeBus())
        self.assertEqual(published, [])
        self.assertEqual(scanner._last_alert, {})
        buf = state.get("skipped_runners") or []
        self.assertEqual(len(buf), 1)
        rec = buf[0]
        self.assertEqual(rec["kind"], "momentum")
        self.assertEqual(rec["mint"], "MINT1")
        self.assertEqual(rec["chg_24h_pct"], 180.0)
        self.assertEqual(rec["safety_score"], 1)
        self.assertIn("insiders", rec["reason"])


if __name__ == "__main__":
    unittest.main()
