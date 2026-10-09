"""Wiring tests for ideas #2 and #5 (user decision 2026-10-01).

#2 wallet auto-discovery: the daily hook in the scanner's main loop feeds
past 5x+ milestone runners into wallet_discovery.run_discovery with the
real smartmoney RPC; proposals stay review-only.

#5 winner-likeness ranker: every confirmed/lowcap runner is scored via
LaunchScanner._winner_score, the score rides on the card, and the
winner_min_score knob (default 0 = OFF) can hold low-scoring runners
through _winner_hold_check. feedback.analyze() now includes the winner
proposals with insider rows plumbed from daemon state.
"""
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from daemon.launch_scanner import LaunchScanner, feedback, milestones
from daemon.launch_scanner import wallet_discovery, winner_model


def _degen(**kw):
    base = dict(feedback_min_outcomes=5, winner_min_score=0.0,
                discovery_enabled=True, discovery_runner_mult=5.0,
                discovery_early_window_s=3600, discovery_min_hits=2,
                discovery_min_hit_rate=0.5, discovery_max_runners=10,
                discovery_max_sigs=30, discovery_max_new=5,
                discovery_interval_s=86400)
    base.update(kw)
    return SimpleNamespace(**base)


def _rec(mint, peak, smart_buys=0, heat=1.0, sniper=50, rug=False,
         label="launch"):
    return {"mint": mint, "symbol": mint[:6], "peak_mult": peak,
            "rug": rug, "safety": {"score": 10},
            "sniper_score": sniper,
            "entry_signals": {"smart_buys": smart_buys,
                              "fp_verdict": "none",
                              "rising_polls": 3,
                              "buy_pressure": 0.5,
                              "hot_deployer": False},
            "narrative_heat": heat, "narratives": ["other"],
            "label": label, "source": "pumpfun-log", "wave": 1,
            "snapshots": []}


class WinnerHoldCheckTest(unittest.TestCase):
    def test_knob_off_never_holds(self):
        state = {}
        self.assertFalse(LaunchScanner._winner_hold_check(
            _degen(winner_min_score=0.0), state, "M1", "AAA",
            5.0, True, "launch", time.time()))
        self.assertNotIn("winner_held", state)

    def test_hold_below_threshold(self):
        state = {}
        self.assertTrue(LaunchScanner._winner_hold_check(
            _degen(winner_min_score=60.0), state, "M1", "AAA",
            20.0, True, "launch", 1000.0))
        held = state["winner_held"]["M1"]
        self.assertEqual(held["symbol"], "AAA")
        self.assertEqual(held["score"], 20.0)
        self.assertEqual(held["lane"], "launch")

    def test_no_hold_when_model_not_ready(self):
        state = {}
        self.assertFalse(LaunchScanner._winner_hold_check(
            _degen(winner_min_score=60.0), state, "M1", "AAA",
            0.0, False, "launch", time.time()))
        self.assertNotIn("winner_held", state)

    def test_no_hold_at_or_above_threshold(self):
        state = {}
        self.assertFalse(LaunchScanner._winner_hold_check(
            _degen(winner_min_score=60.0), state, "M1", "AAA",
            60.0, True, "launch", time.time()))

    def test_held_bounded(self):
        state = {"winner_held": {
            "OLD%d" % i: {"symbol": "X", "score": 1.0, "min": 60.0,
                          "ts": float(i), "lane": "launch"}
            for i in range(250)}}
        LaunchScanner._winner_hold_check(
            _degen(winner_min_score=60.0), state, "NEW", "AAA",
            1.0, True, "launch", 9999.0)
        self.assertLessEqual(len(state["winner_held"]), 200)
        self.assertIn("NEW", state["winner_held"])


class WinnerScoreHelperTest(unittest.TestCase):
    def setUp(self):
        self.sc = LaunchScanner()

    def test_no_data_returns_not_ready(self):
        cfg = SimpleNamespace(degen=_degen())
        with patch.object(feedback, "load_outcomes", return_value=[]), \
             patch.object(feedback, "default_dataset_path",
                          return_value="/nonexistent"):
            score, factors, ready = self.sc._winner_score(
                cfg, {}, {"mint": "M1"}, "M1")
        self.assertEqual(score, 0.0)
        self.assertEqual(factors, [])
        self.assertFalse(ready)

    def test_scores_live_candidate_when_trained(self):
        recs = [_rec("X%d" % i, 25.0, smart_buys=2, heat=1.5, sniper=90)
                for i in range(4)]
        recs += [_rec("Y%d" % i, 0.5, smart_buys=0, heat=1.0, sniper=40)
                 for i in range(3)]
        cfg = SimpleNamespace(degen=_degen())
        state = {"fingerprints": {
            "M1": {"verdict": "HOT", "buy_pressure": 0.8}}}
        cand = {"mint": "M1", "safety": {"score": 10},
                "smart_buys": 2, "narrative_heat": 1.5,
                "narratives": ["other"], "source": "pumpfun-log",
                "confirm": {"rising_polls": 3}}
        with patch.object(feedback, "load_outcomes", return_value=recs), \
             patch.object(feedback, "default_dataset_path",
                          return_value="/nonexistent"):
            score, factors, ready = self.sc._winner_score(
                cfg, state, cand, "M1")
        self.assertTrue(ready)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 100.0)
        self.assertIsInstance(factors, list)
        # A strong candidate should outscore a weak one.
        weak = dict(cand, smart_buys=0, narrative_heat=1.0)
        # bust the cache so the weak candidate scores fresh
        self.sc._winner_model_ts = 0
        with patch.object(feedback, "load_outcomes", return_value=recs), \
             patch.object(feedback, "default_dataset_path",
                          return_value="/nonexistent"):
            wscore, _, _ = self.sc._winner_score(cfg, state, weak, "M1")
        self.assertGreaterEqual(score, wscore)

    def test_synth_record_adapter(self):
        cand = {"mint": "M9", "safety": {"score": 33},
                "sniper_score": 72, "smart_buys": 1,
                "narrative_heat": 1.2, "narratives": ["dog"],
                "source": "pumpfun-log",
                "confirm": {"rising_polls": 2}}
        fp = {"verdict": "WARMING", "buy_pressure": 0.55}
        rec = winner_model.synth_record_from_candidate(
            cand, fp=fp, snaps=[{"t": 1, "mcap_usd": 100},
                               {"t": 500, "mcap_usd": 300}])
        self.assertEqual(rec["mint"], "M9")
        self.assertEqual(rec["safety"]["score"], 33)
        self.assertEqual(rec["entry_signals"]["smart_buys"], 1)
        self.assertEqual(rec["entry_signals"]["fp_verdict"], "WARMING")
        self.assertEqual(rec["entry_signals"]["rising_polls"], 2)
        feats = winner_model.features_from_record(rec)
        self.assertEqual(feats["safety_score"], "21-50")
        self.assertEqual(feats["smart_buys"], "1")
        self.assertEqual(feats["fp_verdict"], "WARMING")
        # Empty candidate: all unknowns, fail-neutral.
        rec2 = winner_model.synth_record_from_candidate({})
        feats2 = winner_model.features_from_record(rec2)
        self.assertEqual(feats2["smart_buys"], "0")
        self.assertEqual(feats2["safety_score"], "unknown")


class FeedbackWinnerHookTest(unittest.TestCase):
    def test_analyze_includes_winner_proposal(self):
        recs = [_rec("X%d" % i, 25.0, smart_buys=2, heat=1.5, sniper=90)
                for i in range(6)]
        recs += [_rec("Y%d" % i, 0.5, smart_buys=0, heat=1.0, sniper=40)
                 for i in range(5)]
        insiders = [{"mint": "X0", "direction": "diluting",
                     "cluster_start": 3.0}]
        degen = _degen()
        with patch.object(feedback, "load_outcomes", return_value=recs), \
             patch.object(feedback, "load_proposals", return_value=[]):
            props = feedback.analyze(degen, now=time.time(), existing=[],
                                     insider_rows=insiders)
        knobs = {p["knob"] for p in props}
        self.assertIn("winner_min_score", knobs)

    def test_winner_min_score_in_allowlist(self):
        self.assertIn("winner_min_score", feedback.TUNABLE)
        lo, hi = feedback.KNOB_CLAMPS["winner_min_score"]
        self.assertEqual((lo, hi), (0.0, 100.0))
        self.assertEqual(feedback.KNOB_DEFAULTS["winner_min_score"], 0.0)

    def test_run_scan_passes_insider_rows(self):
        seen = {}

        def fake_analyze(degen, now=None, existing=None,
                         insider_rows=None, wave_rows=None):
            seen["insider_rows"] = insider_rows
            seen["wave_rows"] = wave_rows
            return []

        degen = _degen()
        with patch.object(feedback, "expire_stale"), \
             patch.object(feedback, "load_outcomes", return_value=[]), \
             patch.object(feedback, "grade_applied"), \
             patch.object(feedback, "load_proposals", return_value=[]), \
             patch.object(feedback, "add_proposals",
                          return_value=[]), \
             patch.object(feedback, "analyze_context",
                          return_value=[]), \
             patch.object(feedback, "save_insights"), \
             patch.object(feedback, "analyze", side_effect=fake_analyze):
            feedback.run_scan(degen, time.time(),
                              insider_rows=[{"mint": "M1"}])
        self.assertEqual(seen["insider_rows"], [{"mint": "M1"}])


class DiscoveryWiringTest(unittest.TestCase):
    def test_run_discovery_uses_config_knobs(self):
        # The daily hook reads discovery_* knobs from degen and passes
        # milestones runners >= discovery_runner_mult. Verify the knob
        # surface run_discovery honors.
        state = {}
        degen = _degen(discovery_min_hits=1, discovery_max_runners=2,
                       discovery_max_sigs=5, discovery_max_new=5,
                       discovery_interval_s=0)
        runners = [
            {"mint": "R1", "symbol": "AAA", "peak_mult": 8.0,
             "first_seen_ts": time.time() - 7200},
            {"mint": "R2", "symbol": "BBB", "peak_mult": 6.0,
             "first_seen_ts": time.time() - 7200},
        ]
        seen = {}

        def fake_rpc(method, params):
            seen.setdefault("calls", []).append(method)
            if method == "getSignaturesForAddress":
                return [{"signature": "sig-%s-%d" % (params[0][:4], i)}
                        for i in range(2)]
            if method == "getTransaction":
                return {"transaction": {"message": {
                    "accountKeys": ["WALLET_A", "WALLET_B"]}}}
            return None

        out = wallet_discovery.run_discovery(
            state, degen, runners, rpc=fake_rpc, now=time.time())
        self.assertEqual(out["runners_mined"], 2)
        self.assertTrue(seen["calls"])
        # Proposals file written, 0600, review-only (no registry write).
        import json
        path = wallet_discovery.proposals_path()
        self.assertTrue(os.path.exists(path))
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        props = json.loads(open(path).read()
                           ) if os.path.getsize(path) else {}
        self.assertIsInstance(props.get("proposals"), list)

    def test_milestone_runner_shape_fits_discovery(self):
        # The hook builds runner dicts from milestones.tracked(state);
        # peak_mult + card_ts must be present on tracked records.
        state = {}
        degen = _degen()
        event = {"event": "launch.detected", "mint": "MABC",
                 "symbol": "AAA", "chain": "solana",
                 "usd_market_cap": 10000.0, "safety_score": 10}
        self.assertTrue(milestones.track_signal(state, degen, event))
        rec = milestones.tracked(state)["MABC"]
        rec["peak_mult"] = 7.5
        runner = {"mint": "MABC", "symbol": rec.get("symbol") or "",
                  "peak_mult": float(rec.get("peak_mult") or 0),
                  "first_seen_ts": float(rec.get("card_ts") or 0)}
        self.assertEqual(runner["peak_mult"], 7.5)
        self.assertGreater(runner["first_seen_ts"], 0)


if __name__ == "__main__":
    unittest.main()


class AlertRenderTest(unittest.TestCase):
    def test_winner_line_on_confirmed_card(self):
        from daemon.telegram import alerts
        e = {"event": "launch.detected", "symbol": "AAA",
             "winner_score": 82.5, "winner_factors": "smart_buys=2+"}
        text = alerts.format_alert(e)
        self.assertIn("winner-likeness 82/100", text)
        self.assertIn("smart_buys=2+", text)

    def test_no_winner_line_when_absent(self):
        from daemon.telegram import alerts
        e = {"event": "launch.detected", "symbol": "AAA"}
        text = alerts.format_alert(e)
        self.assertNotIn("winner-likeness", text)

    def test_winner_line_on_lowcap_card(self):
        from daemon.telegram import alerts
        e = {"event": "launch.lowcap_runner", "symbol": "BBB",
             "winner_score": 41.0}
        text = alerts.format_alert(e)
        self.assertIn("winner-likeness 41/100", text)

    def test_discovery_proposals_card(self):
        from daemon.telegram import alerts
        e = {"event": "discovery.proposals", "count": 3,
             "runners_mined": 7, "note": "n"}
        text = alerts.format_alert(e)
        self.assertIn("Smart-wallet candidates (3 new)", text)
        self.assertIn("Runners mined: 7", text)
        self.assertIn("/smartadd", text)

    def test_discovery_event_passes_validation(self):
        from agent.events.bus import validate_event
        validate_event({"event": "discovery.proposals", "count": 2,
                        "runners_mined": 5, "note": "x"})
        validate_event({"event": "launch.detected", "symbol": "AAA",
                        "winner_score": 80.0, "winner_factors": "a=b"})
