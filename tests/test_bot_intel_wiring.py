"""Integration tests for the bot-intel layer: social sentiment tracker,
AI narrative picker + heat, and the backtest dataset recorder — wired
into the launch scanner, the Telegram cards, and the degen config.

Strict rule under test: all three are SOFT SIGNALS ONLY. They may ride
on cards and in logs, but they never allow, block, or drop a CA.
"""

import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from config.config import DegenConfig
from daemon.launch_scanner import LaunchScanner, narratives, sentiment
from daemon.telegram import alerts


def _degen(**kw):
    base = dict(enabled=True, poll_interval_seconds=30, entry_sol=0.05,
                max_positions=5, min_usd_market_cap=10000.0,
                max_age_minutes=10.0, velocity_multiplier=3.0,
                velocity_window_minutes=10.0, stop_loss_pct=30.0,
                take_profit_pct=200.0, max_hold_hours=4.0, auto_enter=True,
                early_safety_flash=False, early_flash_timeout_s=4.0,
                early_min_score=50.0,
                early_alert_max_per_window=10,
                early_alert_window_seconds=300.0,
                sentiment_enabled=True, sentiment_per_cycle=5,
                narrative_enabled=True, backtest_enabled=True,
                backtest_dataset_path="~/.forex-agent/launch_scanner/"
                "backtest_dataset.jsonl")
    base.update(kw)
    return SimpleNamespace(**base)


def _cfg(**kw):
    return SimpleNamespace(degen=_degen(**kw))


class IntelConfigTest(unittest.TestCase):
    def test_new_knobs_have_sane_defaults(self):
        d = DegenConfig()
        self.assertTrue(d.sentiment_enabled)
        self.assertTrue(d.narrative_enabled)
        self.assertTrue(d.backtest_enabled)
        self.assertEqual(d.sentiment_per_cycle, 2)
        self.assertIn("backtest_dataset.jsonl", d.backtest_dataset_path)


class NarrativeWiringTest(unittest.TestCase):
    def setUp(self):
        self.sc = LaunchScanner()
        self.tmp = os.path.join(
            os.environ.get("TMPDIR", "/tmp"), "narr_test_%d.json" % time.time_ns())
        p = patch.object(narratives, "default_heat_path",
                         return_value=self.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(lambda: os.path.exists(self.tmp)
                        and os.remove(self.tmp))

    def test_attach_sets_heat_on_candidate(self):
        cand = {"mint": "M1", "symbol": "DOGE2",
                "narratives": ["dog", "meme_classic"]}
        h = self.sc._narrative_attach(_cfg(), {}, cand, time.time())
        self.assertEqual(h, 1.0)  # no outcomes yet -> neutral
        self.assertEqual(cand["narrative_heat"], 1.0)

    def test_attach_disabled_leaves_candidate_clean(self):
        cand = {"mint": "M1", "narratives": ["dog"]}
        h = self.sc._narrative_attach(_cfg(narrative_enabled=False), {},
                                      cand, time.time())
        self.assertIsNone(h)
        self.assertNotIn("narrative_heat", cand)

    def test_heat_learns_from_recorded_outcomes(self):
        tr = self.sc._narr_heat_tracker()
        now = time.time()
        # dog narrative strongly outperforming the global baseline
        for _ in range(4):
            tr.record_outcome(["dog"], 5.0, False, now)
        tr.record_outcome(["dog"], 1.1, False, now)
        for _ in range(9):
            tr.record_outcome(["cat"], 1.1, False, now)
        tr.record_outcome(["cat"], 3.0, False, now)
        self.assertGreater(tr.heat(["dog"], now), 1.0)
        self.assertLess(tr.heat(["cat"], now), 1.0)

    def test_heat_persisted_with_0600(self):
        tr = self.sc._narr_heat_tracker()
        now = time.time()
        for _ in range(4):
            tr.record_outcome(["ai"], 4.0, False, now)
        for _ in range(6):
            tr.record_outcome(["other"], 1.1, False, now)
        self.sc._narr_heat_save(time.time(), force=True)
        self.assertTrue(os.path.exists(self.tmp))
        self.assertEqual(os.stat(self.tmp).st_mode & 0o777, 0o600)
        tr2 = narratives.NarrativeHeat.load(self.tmp)
        self.assertGreater(tr2.heat(["ai"], time.time()), 1.0)

    def test_record_narrative_close_uses_stored_tags(self):
        state = {"narrative_tags": {"MINT9": ["ai"]}}
        now = time.time()
        for _ in range(4):
            self.sc._record_narrative_close(
                _degen(), state, "MINT9", pnl=0.05)  # 2x on 0.05 entry
        for _ in range(6):
            self.sc._record_narrative_close(
                _degen(), {"narrative_tags": {"MINT0": ["other"]}},
                "MINT0", pnl=-0.02)  # baseline losers
        tr = self.sc._narr_heat_tracker()
        self.assertGreater(tr.heat(["ai"], now), 1.0)
        # file was force-saved by the close recorder
        self.assertTrue(os.path.exists(self.tmp))

    def test_record_narrative_close_never_raises(self):
        # missing tags map, zero entry_sol — must not raise
        self.sc._record_narrative_close(_degen(entry_sol=0), {}, "M", 1.0)


class SentimentWiringTest(unittest.TestCase):
    def setUp(self):
        self.sc = LaunchScanner()
        self.state = {}

    def test_disabled_returns_none_without_fetch(self):
        with patch.object(sentiment, "fetch_token_profile") as f:
            out = self.sc._sentiment_track(
                _cfg(sentiment_enabled=False), self.state,
                {"mint": "M1", "chain": "solana"}, time.time())
        self.assertIsNone(out)
        f.assert_not_called()

    def test_two_growing_profiles_score_and_attach(self):
        profiles = [
            {"websites": [], "socials": [{"type": "twitter",
                                          "url": "https://x.com/a"}]},
            {"websites": ["https://a.xyz"],
             "socials": [{"type": "twitter", "url": "https://x.com/a"},
                         {"type": "telegram", "url": "https://t.me/a"}]},
        ]
        now = time.time()
        cfg = _cfg()  # one shared config: the tracker keys on the degen object
        with patch.object(sentiment, "fetch_token_profile",
                          side_effect=profiles) as f:
            cand = {"mint": "M2", "chain": "solana"}
            self.assertIsNone(self.sc._sentiment_track(
                cfg, self.state, cand, now))
            out = self.sc._sentiment_track(
                cfg, self.state, cand, now + 700)
            self.assertEqual(f.call_count, 2)
        self.assertIsNotNone(out)
        self.assertIn(out["verdict"], ("HOT", "WARM", "NEUTRAL"))
        self.assertIn("new_telegram", out["signals"])
        self.assertEqual(cand["sentiment"]["verdict"], out["verdict"])

    def test_budget_caps_fetches_per_cycle(self):
        with patch.object(sentiment, "fetch_token_profile",
                          return_value={"websites": [],
                                        "socials": []}) as f:
            cfg = _cfg(sentiment_per_cycle=1)
            now = time.time()
            self.sc._sent_budget = 1
            self.sc._sentiment_track(cfg, self.state,
                                     {"mint": "A", "chain": "solana"}, now)
            self.sc._sentiment_track(cfg, self.state,
                                     {"mint": "B", "chain": "solana"}, now)
            self.assertEqual(f.call_count, 1)  # second mint starved


class BacktestRecorderTest(unittest.TestCase):
    def setUp(self):
        self.sc = LaunchScanner()

    def _cand(self):
        return {"mint": "MINT7", "symbol": "T7", "chain": "solana",
                "source": "pumpfun", "first_ts": 1000.0,
                "first_mcap": 10000.0, "last_mcap": 30000.0,
                "narratives": ["dog"],
                "safety": {"score": 72, "fails": []}}

    def test_reject_recorded_as_jsonl(self):
        path = "/tmp/bt_test_%d.jsonl" % time.time_ns()
        self.addCleanup(lambda: os.path.exists(path)
                        and os.remove(path))
        self.sc._backtest_record(_cfg(backtest_dataset_path=path), {},
                                 self._cand(), "reject", 2000.0)
        with open(path) as fh:
            rec = json.loads(fh.readline())
        self.assertEqual(rec["verdict"], "reject")
        self.assertEqual(rec["mint"], "MINT7")
        self.assertEqual(rec["narratives"], ["dog"])
        self.assertEqual(rec["safety"]["score"], 72)
        self.assertIn("snapshots", rec)
        self.assertIn("outcome", rec)

    def test_confirm_outcome_carries_gain(self):
        path = "/tmp/bt_test_%d.jsonl" % time.time_ns()
        self.addCleanup(lambda: os.path.exists(path)
                        and os.remove(path))
        self.sc._backtest_record(_cfg(backtest_dataset_path=path), {},
                                 self._cand(), "confirm", 2000.0)
        with open(path) as fh:
            rec = json.loads(fh.readline())
        self.assertEqual(rec["outcome"]["peak_mult"], 3.0)

    def test_disabled_writes_nothing(self):
        path = "/tmp/bt_test_%d.jsonl" % time.time_ns()
        self.addCleanup(lambda: os.path.exists(path)
                        and os.remove(path))
        self.sc._backtest_record(
            _cfg(backtest_enabled=False, backtest_dataset_path=path), {},
            self._cand(), "reject", 2000.0)
        self.assertFalse(os.path.exists(path))

    def test_snapshots_come_from_fingerprint_series(self):
        from daemon.launch_scanner import fingerprints
        path = "/tmp/bt_test_%d.jsonl" % time.time_ns()
        self.addCleanup(lambda: os.path.exists(path)
                        and os.remove(path))
        tr = fingerprints.FingerprintTracker(_degen())
        self.sc._fp_tracker = tr
        stats = {"usd_market_cap": 50000.0, "price_usd": 0.001,
                 "vol_m5": 2000.0, "buys_m5": 10, "sells_m5": 4,
                 "usd_liquidity": 9000.0}
        tr.observe("MINT7", stats, 1000.0)
        tr.observe("MINT7", stats, 1060.0)
        self.sc._backtest_record(_cfg(backtest_dataset_path=path), {},
                                 self._cand(), "reject", 2000.0)
        with open(path) as fh:
            rec = json.loads(fh.readline())
        self.assertEqual(len(rec["snapshots"]), 2)
        self.assertEqual(rec["snapshots"][0]["price_usd"], 0.001)


class IntelCardTest(unittest.TestCase):
    def _event(self):
        return {"symbol": "DOGE2", "name": "Doge 2", "chain": "solana",
                "source": "pumpfun", "mint": "MINT1",
                "usd_market_cap": 50000, "age_minutes": 3.2,
                "safety_score": 72, "narratives": ["dog", "meme_classic"],
                "narrative_heat": 1.3, "sentiment": "WARM",
                "url": "https://dexscreener.com/solana/MINT1"}

    def test_intel_line_renders(self):
        line = alerts._intel_line(self._event())
        self.assertIn("dog", line)
        self.assertIn("1.3x", line)
        self.assertIn("WARM", line)

    def test_intel_line_empty_when_nothing(self):
        self.assertEqual(alerts._intel_line({}), "")
        # "other" tags are noise — omitted
        self.assertEqual(alerts._intel_line(
            {"narratives": ["other"], "narrative_heat": 1.0}), "")

    def test_early_card_includes_intel(self):
        card = alerts._launch_early(
            dict(self._event(), tier="early", confirmed=False,
                 note="n"))
        self.assertIn("🏷️", card)
        self.assertIn("💬", card)
        self.assertIn("72/100", card)  # safety line still strict

    def test_confirmed_card_includes_intel(self):
        card = alerts._launch(
            dict(self._event(), tier="runner", confirmed=True,
                 safety="score 72/100 — no hard fails"))
        self.assertIn("🏷️", card)
        self.assertIn("💬", card)
        self.assertIn("72/100", card)

    def test_lowcap_card_includes_intel(self):
        card = alerts._launch_lowcap(
            dict(self._event(), run_pct=85.0, base_mcap=20000,
                 evidence={"rising_polls": 2}))
        self.assertIn("🏷️", card)
        self.assertIn("💬", card)
        self.assertIn("72/100", card)

    def test_cards_without_intel_still_render(self):
        ev = {"symbol": "X", "chain": "solana", "source": "pumpfun",
              "mint": "M", "usd_market_cap": 1, "age_minutes": 1,
              "safety_score": 60, "tier": "early", "confirmed": False,
              "note": "n"}
        card = alerts._launch_early(ev)
        self.assertNotIn("🏷️", card)
        self.assertIn("60/100", card)


if __name__ == "__main__":
    unittest.main()


class IntelEndToEndTest(unittest.TestCase):
    """End-to-end proof the intel layer is live on the bot:

    - narrative heat survives a daemon restart (gap 1),
    - early + confirmed cards carry the candidate's real intel, not
      defaults read from the wrong object (gap 2),
    - the backtest dataset records REAL paper-close outcomes with true
      entry/exit/peak prices (gap 3).
    """

    def setUp(self):
        self.sc = LaunchScanner()
        self.tmp = os.path.join(
            os.environ.get("TMPDIR", "/tmp"),
            "intel_e2e_%d.json" % time.time_ns())
        p = patch.object(narratives, "default_heat_path",
                         return_value=self.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(lambda: os.path.exists(self.tmp)
                        and os.remove(self.tmp))

    def test_heat_restored_from_disk_after_restart(self):
        tr = self.sc._narr_heat_tracker()
        now = time.time()
        for _ in range(4):
            tr.record_outcome(["ai"], 4.0, False, now)
        for _ in range(6):
            tr.record_outcome(["other"], 1.1, False, now)
        self.sc._narr_heat_save(now, force=True)
        self.assertTrue(os.path.exists(self.tmp))
        # Simulate a daemon restart: drop the in-memory tracker entirely.
        del self.sc._narr_heat
        tr2 = self.sc._narr_heat_tracker()
        self.assertGreater(tr2.heat(["ai"], now), 1.0)

    def test_early_alert_carries_candidate_intel(self):
        import agent.events as event_bus
        from agent.events.bus import validate_event
        # Seed real heat for the dog narrative so the fallback (1.0) and
        # the real value are distinguishable.
        tr = self.sc._narr_heat_tracker()
        now = time.time()
        for _ in range(4):
            tr.record_outcome(["dog"], 4.0, False, now)
        for _ in range(6):
            tr.record_outcome(["other"], 1.1, False, now)
        expected_heat = round(
            tr.heat(narratives.classify_narrative(
                "DOGE2", "Doge Two", ""), now), 2)
        self.assertGreater(expected_heat, 1.0)

        published = []
        cfg = _cfg(sniper_tier_threshold=0.0)
        state = {"seen": {}, "candidates": {}, "narrative_tags": {}}
        c = {"mint": "EARLY1", "symbol": "DOGE2", "name": "Doge Two",
             "chain": "solana", "source": "pumpfun",
             "url": "https://x", "usd_market_cap": 42000.0}
        with patch.object(self.sc, "_qualify", return_value="pump"), \
             patch.object(self.sc, "_instant_verdict",
                          return_value=(True, "test gate",
                                        {"score": 72, "fails": []})), \
             patch.object(event_bus, "publish",
                          side_effect=lambda e: published.append(e)):
            self.sc._consider(cfg, None, state, c)
            self.sc._flush_sniper_pending(cfg, state)
        early = [e for e in published if e.get("event") == "launch.early"]
        self.assertEqual(len(early), 1)
        ev = early[0]
        self.assertIn("dog", ev["narratives"])
        self.assertEqual(ev["narrative_heat"], expected_heat)
        self.assertEqual(ev["safety_score"], 72)
        validate_event(dict(ev))  # schema accepts the real payload

    def test_confirmed_card_carries_candidate_intel(self):
        import agent.events as event_bus
        from agent.events.bus import validate_event
        published = []
        cfg = _cfg(auto_enter=False)  # card only, no paper entry
        state = {"seen": {}, "candidates": {}, "narrative_tags": {},
                 "degen_positions": {}}
        # Shape mirrors the judge's fake_c after the confirm-path fix.
        c = {"mint": "CONF1", "symbol": "CATX", "name": "Cat X",
             "chain": "solana", "source": "pumpfun",
             "url": "https://x", "usd_market_cap": 90000.0,
             "confirm": {"gain_mult": 3.0, "rising_polls": 3,
                         "vol_m5": 5000, "liquidity": 12000,
                         "buys_m5": 40, "sells_m5": 10},
             "safety": {"score": 80, "fails": [], "passed": True},
             "smart_buys": 0,
             "narratives": ["cat", "ai"],
             "narrative_heat": 1.4,
             "sentiment": {"verdict": "HOT", "score": 85}}
        entry = {"first_ts": time.time() - 600}
        with patch.object(event_bus, "publish",
                          side_effect=lambda e: published.append(e)):
            self.sc._on_launch(cfg, None, state, c, entry, "pump", "CONF1")
        cards = [e for e in published
                 if e.get("event") == "launch.detected"]
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["narratives"], ["cat", "ai"])
        self.assertEqual(card["narrative_heat"], 1.4)
        self.assertEqual(card["sentiment"], "HOT")
        self.assertIn("pass (score 80)", card["safety"])
        validate_event(dict(card))

    def test_paper_enter_stashes_intel_on_position(self):
        from contextlib import contextmanager
        import broker as broker_mod
        import core.execution.broker_guard as bg
        from daemon.launch_scanner import scoreboard as sb_mod

        @contextmanager
        def _scope():
            yield

        class FakeAdapter:
            def _price_sol_quiet(self, symbol):
                return 0.01

            def submit_order(self, req):
                return SimpleNamespace(ticket="T1", price=0.01)

        cfg = _cfg()
        state = {"degen_positions": {}}
        card = {"tier": "pump", "source": "pumpfun",
                "narratives": ["dog"], "narrative_heat": 1.3,
                "sentiment": "WARM"}
        with patch.object(bg, "execution_scope", _scope), \
             patch.object(broker_mod, "OrderRequest",
                          lambda **kw: SimpleNamespace(**kw)), \
             patch.object(sb_mod, "load_degen_closes", return_value=[]), \
             patch.object(sb_mod, "daily_pnl_sol", return_value=0.0):
            ok = self.sc._paper_enter(
                cfg, FakeAdapter(), state, card, cfg.degen,
                "MINTX", "DOGX",
                intel={"sentiment": {"verdict": "WARM", "score": 60},
                       "safety": {"score": 72, "fails": []}})
        self.assertTrue(ok)
        pos = state["degen_positions"]["MINTX"]
        self.assertEqual(pos["narratives"], ["dog"])
        self.assertEqual(pos["narrative_heat"], 1.3)
        self.assertEqual(pos["sentiment_verdict"], "WARM")
        self.assertEqual(pos["sentiment"], {"verdict": "WARM", "score": 60})
        self.assertEqual(pos["safety_score"], 72)
        self.assertEqual(pos["entry_price_sol"], 0.01)
        self.assertEqual(pos["ticket"], "T1")

    def test_paper_close_records_real_outcome(self):
        path = "/tmp/bt_e2e_%d.jsonl" % time.time_ns()
        self.addCleanup(lambda: os.path.exists(path)
                        and os.remove(path))
        pos = {"ticket": "T1", "symbol": "DOGX",
               "entry_price_sol": 0.01, "peak_price_sol": 0.05,
               "entry_ts": 5000.0, "tier": "pump",
               "source": "pumpfun", "narratives": ["dog"],
               "narrative_heat": 1.5,
               "sentiment": {"verdict": "HOT", "score": 90},
               "sentiment_verdict": "HOT",
               "safety_score": 72, "safety_fails": []}
        self.sc._backtest_record_paper_close(
            _cfg(backtest_dataset_path=path), {}, "MINTX", "DOGX", pos,
            0.01, 0.03, "take_profit", 0.10, 9000.0)
        with open(path) as fh:
            rec = json.loads(fh.readline())
        self.assertEqual(rec["outcome_kind"], "paper")
        self.assertEqual(rec["entry_price_sol"], 0.01)
        self.assertEqual(rec["close_price_sol"], 0.03)
        self.assertEqual(rec["peak_price_sol"], 0.05)
        self.assertEqual(rec["realized_mult"], 3.0)
        self.assertEqual(rec["peak_mult"], 5.0)
        self.assertEqual(rec["exit_reason"], "take_profit")
        self.assertFalse(rec["rug"])
        self.assertEqual(rec["narratives"], ["dog"])
        self.assertEqual(rec["sentiment_verdict"], "HOT")
        self.assertEqual(rec["safety"]["score"], 72)

    def test_paper_close_flags_rug_on_near_total_loss(self):
        path = "/tmp/bt_e2e_%d.jsonl" % time.time_ns()
        self.addCleanup(lambda: os.path.exists(path)
                        and os.remove(path))
        pos = {"entry_price_sol": 0.01, "peak_price_sol": 0.02,
               "entry_ts": 5000.0, "narratives": ["cat"]}
        self.sc._backtest_record_paper_close(
            _cfg(backtest_dataset_path=path), {}, "RUG1", "RUGX", pos,
            0.01, 0.0005, "stop_loss", -0.0475, 9000.0)
        with open(path) as fh:
            rec = json.loads(fh.readline())
        self.assertTrue(rec["rug"])
        self.assertEqual(rec["realized_mult"], 0.05)

    def test_judge_records_tagged_judge_kind(self):
        path = "/tmp/bt_e2e_%d.jsonl" % time.time_ns()
        self.addCleanup(lambda: os.path.exists(path)
                        and os.remove(path))
        cand = {"mint": "MINT7", "symbol": "T7", "chain": "solana",
                "source": "pumpfun", "first_ts": 1000.0,
                "first_mcap": 10000.0, "last_mcap": 30000.0,
                "narratives": ["dog"],
                "safety": {"score": 72, "fails": []}}
        self.sc._backtest_record(_cfg(backtest_dataset_path=path), {},
                                 cand, "confirm", 2000.0)
        with open(path) as fh:
            rec = json.loads(fh.readline())
        self.assertEqual(rec["outcome_kind"], "judge")

    def test_engine_tallies_real_paper_outcomes(self):
        from daemon.launch_scanner import backtest
        hists = [
            {"outcome_kind": "paper", "mint": "A", "symbol": "A",
             "realized_mult": 2.5, "pnl_sol": 0.075, "rug": False,
             "exit_reason": "take_profit", "narratives": ["dog"]},
            {"outcome_kind": "paper", "mint": "B", "symbol": "B",
             "realized_mult": 0.6, "pnl_sol": -0.02, "rug": False,
             "exit_reason": "stop_loss", "narratives": ["cat"]},
            {"outcome_kind": "paper", "mint": "C", "symbol": "C",
             "realized_mult": 0.05, "pnl_sol": -0.0475, "rug": True,
             "exit_reason": "stop_loss", "narratives": ["dog"]},
        ]
        rep = backtest.BacktestEngine().run(hists)
        p = rep["paper"]
        self.assertEqual(p["trades"], 3)
        self.assertEqual(p["wins"], 1)
        self.assertEqual(p["losses"], 2)
        self.assertEqual(p["rugs"], 1)
        self.assertAlmostEqual(p["total_pnl_sol"], 0.0075)
        self.assertEqual(p["per_narrative"]["dog"]["trades"], 2)
        self.assertEqual(p["per_narrative"]["dog"]["wins"], 1)
        self.assertEqual(p["per_exit_reason"]["stop_loss"]["trades"], 2)
        # Paper records need no snapshots and are never re-simulated.
        self.assertEqual(rep["n_trades"], 0)
        self.assertEqual(rep["n_histories"], 3)

    def test_mixed_dataset_keeps_judge_and_paper_separate(self):
        from daemon.launch_scanner import backtest
        judge_hist = backtest.synthetic_history(
            mint="J1", symbol="J", narratives=["ai"])
        paper_hist = {"outcome_kind": "paper", "mint": "P1", "symbol": "P",
                      "realized_mult": 2.0, "pnl_sol": 0.05, "rug": False,
                      "exit_reason": "take_profit", "narratives": ["ai"]}
        rep = backtest.BacktestEngine().run([judge_hist, paper_hist])
        self.assertEqual(rep["n_trades"], 1)  # judge path replayed
        self.assertEqual(rep["per_narrative"]["ai"]["trades"], 1)
        self.assertEqual(rep["paper"]["trades"], 1)  # paper path tallied
        self.assertEqual(rep["paper"]["per_narrative"]["ai"]["wins"], 1)
        self.assertEqual(rep["paper"]["wins"], 1)
