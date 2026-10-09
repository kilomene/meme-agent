"""Sniper tier: runner-probability ranking engine (sniper.py) + wiring.

Covers the pure scoring math (threshold boundary, weights, every
signal, fail-safe on missing inputs) and the scanner wiring:

  * quality-passed + below threshold -> digest queue (never silent,
    even with instant cap room)
  * quality-passed + tier-1 -> staged; the end-of-cycle flush emits
    highest-rank-first through the standing instant cap
  * cap-tripped tier-1 -> digest queue (never dropped); the cap counts
    actual sends, not evaluations
  * hard fail / unknown verdicts never reach the ranker
  * rank errors fail OPEN (pre-sniper behavior) rather than blocking
  * deployer launch recorded exactly once across stage + digest fallback
  * smart fast-track: a smart buy boosts the rank but cannot bypass
    the threshold
  * cards carry the one-line sniper reason; the strict event-bus
    schema accepts the new fields

No network: the safety screen is mocked, publish is captured.
"""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.events import bus as event_bus
from agent.events.bus import validate_event
from daemon.launch_scanner import LaunchScanner, sniper
from daemon.telegram import alerts


def _degen(**kw):
    base = dict(
        enabled=True, poll_interval_seconds=30, entry_sol=0.05,
        max_positions=5, min_usd_market_cap=10000.0,
        max_age_minutes=10.0, velocity_multiplier=3.0,
        velocity_window_minutes=10.0, stop_loss_pct=30.0,
        take_profit_pct=200.0, max_hold_hours=4.0, auto_enter=True,
        sniper_enabled=True,
        sniper_tier_threshold=50.0,
        sniper_weight_fp=30.0,
        sniper_weight_smart=35.0,
        sniper_weight_velocity=20.0,
        sniper_weight_pressure=10.0,
        sniper_boost_deployer=5.0,
        early_safety_flash=True,
        early_min_score=1.0,
        early_alert_max_per_window=4,
        early_alert_window_seconds=3600.0,
        milestones_enabled=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _sig(**kw):
    s = {"fp_composite": None, "fp_verdict": None, "smart_buys": 0,
         "rising_polls": 0, "buy_pressure": None, "hot_deployer": False}
    s.update(kw)
    return s


class RankCandidateTest(unittest.TestCase):
    def test_all_missing_is_zero_standard(self):
        r = sniper.rank_candidate(_degen(), _sig())
        self.assertEqual(r["score"], 0.0)
        self.assertEqual(r["tier"], "standard")
        self.assertEqual(r["reasons"], [])
        self.assertEqual(r["threshold"], 50.0)
        self.assertTrue(r["enabled"])

    def test_none_signals_fail_safe(self):
        r = sniper.rank_candidate(_degen(), None)
        self.assertEqual(r["score"], 0.0)
        self.assertEqual(r["tier"], "standard")

    def test_disabled_never_tiers(self):
        r = sniper.rank_candidate(
            _degen(sniper_enabled=False),
            _sig(fp_composite=100, fp_verdict="HOT", smart_buys=5,
                 rising_polls=9, buy_pressure=1.0, hot_deployer=True))
        self.assertGreaterEqual(r["score"], 50.0)
        self.assertEqual(r["tier"], "standard")
        self.assertFalse(r["enabled"])

    def test_threshold_boundary(self):
        # smart weight 50: 2 buys -> exactly 50.0 -> tier (>=).
        d = _degen(sniper_weight_smart=50.0, sniper_weight_fp=0.0,
                   sniper_weight_velocity=0.0, sniper_weight_pressure=0.0,
                   sniper_boost_deployer=0.0)
        r = sniper.rank_candidate(d, _sig(smart_buys=2))
        self.assertEqual(r["score"], 50.0)
        self.assertEqual(r["tier"], "sniper")
        # ...one buy -> 25.0 -> standard.
        r = sniper.rank_candidate(d, _sig(smart_buys=1))
        self.assertEqual(r["score"], 25.0)
        self.assertEqual(r["tier"], "standard")

    def test_weight_overrides(self):
        r = sniper.rank_candidate(_degen(sniper_weight_smart=10.0),
                                  _sig(smart_buys=2))
        self.assertEqual(r["score"], 10.0)

    def test_smart_wallet_half_then_full(self):
        self.assertEqual(
            sniper.rank_candidate(_degen(), _sig(smart_buys=1))["score"],
            17.5)
        self.assertEqual(
            sniper.rank_candidate(_degen(), _sig(smart_buys=2))["score"],
            35.0)
        # 5 wallets: still full weight, never more.
        self.assertEqual(
            sniper.rank_candidate(_degen(), _sig(smart_buys=5))["score"],
            35.0)
        r = sniper.rank_candidate(_degen(), _sig(smart_buys=2))
        self.assertIn("2 smart buys", r["reasons"])

    def test_fp_hot_warming_cold(self):
        d = _degen()
        r = sniper.rank_candidate(d, _sig(fp_composite=70,
                                         fp_verdict="HOT"))
        self.assertEqual(r["score"], 21.0)
        self.assertIn("HOT fingerprint", r["reasons"])
        r = sniper.rank_candidate(d, _sig(fp_composite=70,
                                         fp_verdict="WARMING"))
        self.assertIn("warming fingerprint", r["reasons"])
        # COLD contributes its small score with no praise.
        r = sniper.rank_candidate(d, _sig(fp_composite=20,
                                         fp_verdict="COLD"))
        self.assertEqual(r["score"], 6.0)
        self.assertEqual(r["reasons"], [])
        # fp=0 / None: no contribution at all.
        self.assertEqual(
            sniper.rank_candidate(d, _sig(fp_composite=0))["score"], 0.0)
        self.assertEqual(
            sniper.rank_candidate(
                d, _sig(fp_composite=None))["score"], 0.0)

    def test_rising_velocity_ladder(self):
        d = _degen(sniper_weight_velocity=20.0, sniper_weight_fp=0.0,
                   sniper_weight_smart=0.0, sniper_weight_pressure=0.0,
                   sniper_boost_deployer=0.0)
        self.assertEqual(
            sniper.rank_candidate(d, _sig(rising_polls=1))["score"], 6.0)
        self.assertEqual(
            sniper.rank_candidate(d, _sig(rising_polls=2))["score"], 13.0)
        self.assertEqual(
            sniper.rank_candidate(d, _sig(rising_polls=3))["score"], 20.0)
        self.assertEqual(
            sniper.rank_candidate(d, _sig(rising_polls=9))["score"], 20.0)
        r = sniper.rank_candidate(d, _sig(rising_polls=2))
        self.assertIn("rising momentum", r["reasons"])

    def test_buy_pressure(self):
        d = _degen()
        self.assertEqual(
            sniper.rank_candidate(d, _sig(buy_pressure=0.8))["score"], 8.0)
        # >1 clamps to full weight.
        self.assertEqual(
            sniper.rank_candidate(d, _sig(buy_pressure=1.5))["score"],
            10.0)
        # None stays 0.
        self.assertEqual(
            sniper.rank_candidate(d, _sig(buy_pressure=None))["score"],
            0.0)

    def test_hot_deployer_boost(self):
        d = _degen()
        r = sniper.rank_candidate(d, _sig(hot_deployer=True))
        self.assertEqual(r["score"], 5.0)
        self.assertIn("hot deployer", r["reasons"])
        r = sniper.rank_candidate(d, _sig(hot_deployer=False))
        self.assertEqual(r["score"], 0.0)

    def test_score_caps_at_100(self):
        r = sniper.rank_candidate(
            _degen(), _sig(fp_composite=100, fp_verdict="HOT",
                           smart_buys=9, rising_polls=9,
                           buy_pressure=1.0, hot_deployer=True))
        self.assertEqual(r["score"], 100.0)
        self.assertEqual(r["tier"], "sniper")

    def test_composite_default_weights(self):
        # HOT fp (70->21) + 2 smart (35) + 3 rising (20) + pressure
        # 0.9 (9) + hot deployer (5) = 90.0 -> tier-1.
        r = sniper.rank_candidate(
            _degen(), _sig(fp_composite=70, fp_verdict="HOT",
                           smart_buys=2, rising_polls=3,
                           buy_pressure=0.9, hot_deployer=True))
        self.assertEqual(r["score"], 90.0)
        self.assertEqual(r["tier"], "sniper")


class SniperLineTest(unittest.TestCase):
    def test_format(self):
        r = {"score": 72.5, "reasons": ["HOT fingerprint", "2 smart buys"]}
        self.assertEqual(sniper.sniper_line(r),
                         "🎯 sniper 72.5 — HOT fingerprint + 2 smart buys")

    def test_empty_reasons(self):
        r = {"score": 50.0, "reasons": []}
        self.assertEqual(sniper.sniper_line(r),
                         "🎯 sniper 50.0 — quality-passed")

    def test_none_result(self):
        self.assertEqual(sniper.sniper_line(None), "")


class SniperWiringTest(unittest.TestCase):
    """Scanner wiring: _consider -> rank -> stage/flush/digest."""

    def setUp(self):
        self.sc = LaunchScanner.__new__(LaunchScanner)
        self.published = []
        self._pub = patch.object(
            event_bus, "publish",
            lambda evt, severity=None: self.published.append(evt)
            or {"ok": True})
        self._pub.start()
        self.addCleanup(self._pub.stop)
        import agent.events as events_pkg
        self._pub2 = patch.object(
            events_pkg, "publish",
            lambda evt, severity=None: self.published.append(evt)
            or {"ok": True})
        self._pub2.start()
        self.addCleanup(self._pub2.stop)

    def _cfg(self, **kw):
        return SimpleNamespace(degen=_degen(**kw))

    def _coin(self, mint, symbol="TKN", **kw):
        c = {"mint": mint, "symbol": symbol, "name": symbol,
             "chain": "solana", "source": "pumpfun",
             "url": "https://x", "usd_market_cap": 15000.0,
             "tier": "pump"}
        c.update(kw)
        return c

    def _state(self):
        return {"seen": {}, "degen_positions": {}, "candidates": {}}

    def _pass(self, score=90):
        return (True, "quality-pass score %s" % score,
                {"score": score, "passed": True, "fails": []})

    def _hardfail(self):
        return (False, "safety-fail: honeypot",
                {"score": 1, "passed": False, "fails": ["honeypot"]})

    def _consider(self, cfg, state, c, verdict):
        with patch.object(LaunchScanner, "_instant_verdict",
                          staticmethod(lambda d, x: verdict)):
            self.sc._consider(cfg, None, state, c)

    # -- routing --------------------------------------------------------

    def test_below_threshold_goes_to_digest_even_with_cap_room(self):
        # Zero signals -> score 0 < 50 -> digest queue, NOT instant,
        # even though the 4/hour cap is wide open.
        cfg = self._cfg()
        state = self._state()
        self._consider(cfg, state, self._coin("MINTLOW1"), self._pass())
        self.assertEqual(self.published, [])
        self.assertEqual(state.get("sniper_pending") or [], [])
        digest = state.get("early_digest") or []
        self.assertEqual(len(digest), 1)
        item = digest[0]
        self.assertEqual(item["mint"], "MINTLOW1")
        self.assertEqual(item["sniper_score"], 0.0)
        self.assertEqual(item["sniper_tier"], "standard")
        # Nothing quality-passed is silently discarded.
        self.assertIn("MINTLOW1", state["candidates"])

    def test_tier1_stages_then_flush_publishes(self):
        cfg = self._cfg(sniper_tier_threshold=0.0)  # everything tiers
        state = self._state()
        self._consider(cfg, state, self._coin("MINTTOP1"), self._pass())
        # Staged mid-cycle — nothing published yet.
        self.assertEqual(self.published, [])
        self.assertEqual(len(state.get("sniper_pending") or []), 1)
        self.sc._flush_sniper_pending(cfg, state)
        self.assertEqual(len(self.published), 1)
        evt = self.published[0]
        self.assertEqual(evt["event"], "launch.early")
        self.assertFalse(evt["confirmed"])
        self.assertIn("sniper", evt)
        self.assertIn("sniper_line", evt)
        self.assertIn("🎯 sniper", evt["sniper_line"])
        # The strict bus schema accepts the new fields.
        validate_event(dict(evt))
        # The card renderer shows the one-line reason.
        card = alerts._launch_early(evt)
        self.assertIn("🎯 sniper", card)

    def test_digest_item_carries_sniper_score(self):
        cfg = self._cfg()
        state = self._state()
        self._consider(cfg, state, self._coin("MINTDIG1"), self._pass())
        item = (state.get("early_digest") or [])[0]
        digest_item = LaunchScanner._digest_item(item, state)
        self.assertEqual(digest_item["sniper_score"], 0.0)
        self.assertEqual(digest_item["sniper_tier"], "standard")
        line = alerts._digest_item_line(digest_item)
        self.assertIn("🎯0", line)

    # -- ordering + cap -------------------------------------------------

    def test_highest_rank_first(self):
        # 1 cap slot, 3 tier-1 coins: the strongest setup wins the slot;
        # the other two join the digest (never dropped).
        cfg = self._cfg(early_alert_max_per_window=1,
                        early_alert_window_seconds=3600.0)
        state = self._state()
        state["early_window"] = {"start": time.time(), "count": 0}
        scores = {"M1": 60.0, "M2": 90.0, "M3": 70.0}

        def fake_sniper_rank(scn, config, st, c, entry, new_cand):
            m = c.get("mint")
            return {"score": scores[m], "tier": "sniper",
                    "reasons": ["test"], "threshold": 50.0,
                    "enabled": True}

        with patch.object(LaunchScanner, "_sniper_rank",
                          fake_sniper_rank):
            for m in ("M1", "M2", "M3"):
                self._consider(cfg, state, self._coin(m), self._pass())
            self.assertEqual(len(state.get("sniper_pending") or []), 3)
            self.sc._flush_sniper_pending(cfg, state)
        early = [e for e in self.published
                 if e.get("event") == "launch.early"]
        self.assertEqual(len(early), 1)
        self.assertEqual(early[0]["mint"], "M2")  # highest rank first
        self.assertEqual(early[0]["sniper"], 90.0)
        digest_mints = {d["mint"] for d in (state.get("early_digest")
                                            or [])}
        self.assertEqual(digest_mints, {"M1", "M3"})
        # Cap consumed exactly one slot (the send), not three evals.
        self.assertEqual(state["early_window"]["count"], 1)

    def test_cap_counts_sends_not_evals(self):
        cfg = self._cfg(sniper_tier_threshold=0.0,
                        early_alert_max_per_window=2)
        state = self._state()
        now = time.time()
        state["early_window"] = {"start": now, "count": 2}  # cap full
        for m in ("C1", "C2"):
            self._consider(cfg, state, self._coin(m), self._pass())
        self.sc._flush_sniper_pending(cfg, state)
        self.assertEqual(self.published, [])
        self.assertEqual(len(state.get("early_digest") or []), 2)
        # Staging + tripped flush consume no slots.
        self.assertEqual(state["early_window"]["count"], 2)

    def test_flush_publish_failure_never_loses_cards(self):
        cfg = self._cfg(sniper_tier_threshold=0.0,
                        early_alert_max_per_window=4)
        state = self._state()
        for m in ("F1", "F2", "F3"):
            self._consider(cfg, state, self._coin(m), self._pass())
        self.assertEqual(len(state.get("sniper_pending") or []), 3)
        import agent.events as events_pkg
        sent = []

        def boom(evt, severity=None):
            if evt.get("mint") == "F2":
                raise RuntimeError("storage backend down")
            sent.append(evt)
            return {"ok": True}

        with patch.object(events_pkg, "publish", boom):
            self.sc._flush_sniper_pending(cfg, state)
        # F1 and F3 still went out; the failed F2 fell back to digest.
        self.assertEqual({e["mint"] for e in sent}, {"F1", "F3"})
        digest_mints = {d["mint"] for d in (state.get("early_digest")
                                           or [])}
        self.assertIn("F2", digest_mints)
        # The failed send's cap slot was rolled back: 2 sends counted.
        self.assertEqual(state["early_window"]["count"], 2)
        # Nothing left stranded in pending.
        self.assertEqual(state.get("sniper_pending") or [], [])

    # -- safety ordering -------------------------------------------------

    def test_hard_fail_never_reaches_ranker(self):
        calls = []
        real = sniper.rank_candidate

        def spy(degen, signals):
            calls.append(signals)
            return real(degen, signals)

        cfg = self._cfg()
        state = self._state()
        with patch.object(sniper, "rank_candidate", spy):
            self._consider(cfg, state, self._coin("MINTRUG1"),
                           self._hardfail())
        self.assertEqual(calls, [])
        self.assertEqual(self.published, [])
        self.assertEqual(state.get("sniper_pending") or [], [])
        self.assertEqual(state.get("early_digest") or [], [])
        # Still tracked silently for the confirmed stage.
        self.assertIn("MINTRUG1", state["candidates"])

    def test_unknown_never_reaches_ranker(self):
        calls = []
        real = sniper.rank_candidate

        def spy(degen, signals):
            calls.append(signals)
            return real(degen, signals)

        cfg = self._cfg()
        state = self._state()
        unknown = (False, "no safety report", None)
        with patch.object(sniper, "rank_candidate", spy):
            self._consider(cfg, state, self._coin("MINTUNK1"), unknown)
        self.assertEqual(calls, [])
        self.assertEqual(self.published, [])
        self.assertEqual(state.get("sniper_pending") or [], [])
        self.assertEqual(state.get("early_digest") or [], [])

    def test_low_score_never_reaches_ranker(self):
        calls = []
        real = sniper.rank_candidate

        def spy(degen, signals):
            calls.append(signals)
            return real(degen, signals)

        cfg = self._cfg(early_min_score=30.0)
        state = self._state()
        low = (False, "score 12 below bar 30", {"score": 12})
        with patch.object(sniper, "rank_candidate", spy):
            self._consider(cfg, state, self._coin("MINTLOW2"), low)
        self.assertEqual(calls, [])

    # -- fail-open -------------------------------------------------------

    def test_rank_error_fails_open_to_instant_lane(self):
        # Ranking is an optimization, not a gate: if the ranker blows
        # up, the coin takes the pre-sniper instant lane rather than
        # being blocked or silently dropped.
        cfg = self._cfg()
        state = self._state()
        with patch.object(sniper, "rank_candidate",
                          side_effect=RuntimeError("boom")):
            self._consider(cfg, state, self._coin("MINTERR1"),
                           self._pass())
        self.assertEqual(len(state.get("sniper_pending") or []), 1)
        self.sc._flush_sniper_pending(cfg, state)
        self.assertEqual(len(self.published), 1)
        self.assertEqual(self.published[0]["mint"], "MINTERR1")

    # -- deployer dedup ---------------------------------------------------

    def test_deployer_launch_recorded_once_across_stage_and_digest(self):
        # The deployer trigger resolves at stage time; the cap-tripped
        # digest fallback must reuse it, never re-resolve/record.
        cfg = self._cfg(sniper_tier_threshold=0.0)
        state = self._state()
        now = time.time()
        state["early_window"] = {"start": now, "count": 4}  # cap full
        calls = []
        real = LaunchScanner._deployer_card_line

        def counting(self_, config, st, mint, chain, c, allow):
            calls.append(mint)
            return real(self_, config, st, mint, chain, c, allow)

        with patch.object(LaunchScanner, "_deployer_card_line",
                          counting):
            self._consider(cfg, state, self._coin("MINTDEP1"),
                           self._pass())
        self.sc._flush_sniper_pending(cfg, state)
        self.assertEqual(self.published, [])  # cap tripped -> digest
        self.assertEqual(len(state.get("early_digest") or []), 1)
        self.assertEqual(calls, ["MINTDEP1"])  # exactly once


class SmartFastTrackSniperTest(unittest.TestCase):
    """A smart buy boosts the rank but cannot bypass the threshold."""

    def setUp(self):
        self.sc = LaunchScanner.__new__(LaunchScanner)
        self.published = []
        self._pub = patch.object(
            event_bus, "publish",
            lambda evt, severity=None: self.published.append(evt)
            or {"ok": True})
        self._pub.start()
        self.addCleanup(self._pub.stop)
        import agent.events as events_pkg
        self._pub2 = patch.object(
            events_pkg, "publish",
            lambda evt, severity=None: self.published.append(evt)
            or {"ok": True})
        self._pub2.start()
        self.addCleanup(self._pub2.stop)

    def _cfg(self, **kw):
        return SimpleNamespace(degen=_degen(**kw))

    def test_sub_tier_smart_buy_cannot_bypass_threshold(self):
        # Default knobs: a lone smart buy scores 17.5 < 50 -> digest,
        # even with the instant cap wide open.
        from daemon.launch_scanner import smartmoney
        mint = "MINTSMARTSUB1111111111111111111111111"
        state = {"seen": {}, "smart_activity": {mint: ["WALLET1"]},
                 "early_digest": []}
        cfg = self._cfg()
        trig = {"reason": "quality-pass score 5", "safety_score": 5}
        with patch.object(smartmoney, "evaluate_fasttrack",
                          return_value=trig), \
             patch.object(smartmoney, "wallet_labels_for",
                          return_value=["WALLET1"]):
            self.sc._smart_fasttrack(cfg, state, {mint: ["WALLET1"]})
        self.assertEqual(self.published, [])
        self.assertEqual(state.get("sniper_pending") or [], [])
        digest = state.get("early_digest") or []
        self.assertEqual(len(digest), 1)
        self.assertTrue(digest[0]["fasttrack"])
        self.assertEqual(digest[0]["sniper_tier"], "standard")

    def test_tier1_smart_buy_stages_for_flush(self):
        # Smart-heavy knobs: the same buy now clears the threshold ->
        # staged tier-1, published by the flush.
        from daemon.launch_scanner import smartmoney
        mint = "MINTSMARTTOP2222222222222222222222222"
        state = {"seen": {}, "smart_activity": {mint: ["WALLET1"]},
                 "early_digest": []}
        cfg = self._cfg(sniper_weight_smart=100.0, sniper_weight_fp=0.0,
                        sniper_weight_velocity=0.0,
                        sniper_weight_pressure=0.0,
                        sniper_boost_deployer=0.0)
        trig = {"reason": "quality-pass score 5", "safety_score": 5}
        with patch.object(smartmoney, "evaluate_fasttrack",
                          return_value=trig), \
             patch.object(smartmoney, "wallet_labels_for",
                          return_value=["WALLET1"]):
            self.sc._smart_fasttrack(cfg, state, {mint: ["WALLET1"]})
        self.assertEqual(self.published, [])
        self.assertEqual(len(state.get("sniper_pending") or []), 1)
        self.sc._flush_sniper_pending(cfg, state)
        self.assertEqual(len(self.published), 1)
        evt = self.published[0]
        self.assertTrue(evt["fasttrack"])
        self.assertIn("WALLET1", evt["smart_money"])
        validate_event(dict(evt))


if __name__ == "__main__":
    unittest.main()
