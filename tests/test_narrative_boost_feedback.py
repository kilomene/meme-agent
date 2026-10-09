"""Tests for idea #5 (narrative-heat boost) and idea #6 (feedback loop).

#5: hot narratives boost the sniper score; cold/neutral never penalize.
#6: the analyzer proposes tuning from real outcomes; nothing auto-applies;
    safety knobs can never be proposed or applied.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# NOTE: tests never mutate os.environ at import time (other modules
# overwrite FOREX_AGENT_HOME without restoring it). Every test that
# needs HOME isolation pins it explicitly via mock.patch.dict or an
# explicit path argument.
_TMP = tempfile.mkdtemp(prefix="forex_boost_feedback_")

from daemon.launch_scanner import feedback, narratives, sniper  # noqa: E402


def _degen(**kw):
    base = dict(sniper_enabled=True, sniper_tier_threshold=50.0,
                sniper_weight_fp=30.0, sniper_weight_smart=35.0,
                sniper_weight_velocity=20.0, sniper_weight_pressure=10.0,
                sniper_boost_deployer=5.0, narrative_boost_enabled=True,
                narrative_boost_max=8.0, feedback_enabled=True,
                feedback_min_outcomes=20,
                backtest_dataset_path=os.path.join(
                    _TMP, "backtest_dataset.jsonl"))
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------- #5 ----
class TestNarrativeBoost(unittest.TestCase):
    def _score(self, heat, **kw):
        signals = {"fp_composite": 60.0, "fp_verdict": "WARMING",
                   "smart_buys": 0, "rising_polls": 0,
                   "buy_pressure": None, "hot_deployer": False}
        if heat != "missing":
            signals["narrative_heat"] = heat
        return sniper.rank_candidate(_degen(**kw), signals)

    def test_hot_heat_boosts(self):
        # fp 60 -> 30 * 0.6 = 18.0 base; heat 1.5 -> +4.0
        r = self._score(1.5)
        self.assertEqual(r["score"], 22.0)
        self.assertTrue(any("hot narrative" in x for x in r["reasons"]))

    def test_max_heat_full_boost(self):
        # heat 2.0 -> full narrative_boost_max (8.0)
        r = self._score(2.0)
        self.assertEqual(r["score"], 26.0)

    def test_neutral_heat_no_change(self):
        self.assertEqual(self._score(1.0)["score"], 18.0)

    def test_cold_heat_never_penalizes(self):
        r = self._score(0.5)
        self.assertEqual(r["score"], 18.0)
        self.assertFalse(any("narrative" in x for x in r["reasons"]))

    def test_missing_heat_no_change(self):
        self.assertEqual(self._score("missing")["score"], 18.0)

    def test_knob_off_disables_boost(self):
        r = self._score(2.0, narrative_boost_enabled=False)
        self.assertEqual(r["score"], 18.0)

    def test_boost_can_promote_tier(self):
        # 48 base + 8 boost = 56 >= 50 -> sniper
        signals = {"fp_composite": 100.0, "smart_buys": 1,
                   "narrative_heat": 2.0}
        r = sniper.rank_candidate(_degen(), signals)
        # fp 30 + smart 17.5 = 47.5, +8 = 55.5 -> sniper
        self.assertEqual(r["tier"], "sniper")
        self.assertGreaterEqual(r["score"], 50.0)

    def test_cold_cannot_demote(self):
        signals = {"fp_composite": 100.0, "smart_buys": 2,
                   "narrative_heat": 0.5}
        r = sniper.rank_candidate(_degen(), signals)
        self.assertEqual(r["tier"], "sniper")  # 30+35=65, no penalty


class TestHeatLeaderboard(unittest.TestCase):
    def _tracker(self):
        tr = narratives.NarrativeHeat()
        now = time.time()
        # "cat": 4 closed, 3 runners -> hot
        for i in range(3):
            tr.record_outcome(["cat"], 3.0, False, now - 100 - i)
        tr.record_outcome(["cat"], 0.5, False, now - 100)
        # "dog": 4 closed, 0 runners -> cold
        for i in range(4):
            tr.record_outcome(["dog"], 0.8, False, now - 100 - i)
        # "ai": only 2 closed -> omitted (needs 3)
        tr.record_outcome(["ai"], 5.0, False, now - 100)
        tr.record_outcome(["ai"], 4.0, False, now - 100)
        return tr, now

    def test_leaderboard_sorted_and_filtered(self):
        tr, now = self._tracker()
        rows = tr.leaderboard(now)
        tags = [r["tag"] for r in rows]
        self.assertIn("cat", tags)
        self.assertIn("dog", tags)
        self.assertNotIn("ai", tags)  # too few outcomes
        self.assertNotIn("other", tags)
        self.assertEqual(tags[0], "cat")  # hottest first
        cat = rows[0]
        self.assertGreater(cat["heat"], 1.0)
        self.assertEqual(cat["closed"], 4)
        self.assertEqual(cat["runners"], 3)

    def test_leaderboard_empty(self):
        tr = narratives.NarrativeHeat()
        self.assertEqual(tr.leaderboard(time.time()), [])


# ---------------------------------------------------------------- #6 ----
def _write_dataset(path, records):
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _rec(score, peak_mult, heat=1.0, rug=False):
    return {"outcome_kind": "paper", "sniper_score": score,
            "peak_mult": peak_mult, "narrative_heat": heat, "rug": rug,
            "t_close": time.time()}


class TestFeedbackAnalyze(unittest.TestCase):
    def setUp(self):
        self.dpath = os.path.join(_TMP, "ds_%d.jsonl" % id(self))

    def test_no_proposals_below_min_outcomes(self):
        _write_dataset(self.dpath, [_rec(60, 3.0) for _ in range(5)])
        d = _degen(backtest_dataset_path=self.dpath,
                   feedback_min_outcomes=20)
        self.assertEqual(feedback.analyze(d), [])

    def test_threshold_raise_proposal(self):
        recs = []
        # weak band [50,55): 10 coins, 1 runner (10%)
        for i in range(9):
            recs.append(_rec(51 + (i % 4), 1.2))
        recs.append(_rec(52, 2.5))
        # strong band [55,100]: 12 coins, 7 runners (~58%)
        for i in range(5):
            recs.append(_rec(60 + i, 1.3))
        for i in range(7):
            recs.append(_rec(60 + i, 3.0))
        _write_dataset(self.dpath, recs)
        d = _degen(backtest_dataset_path=self.dpath,
                   feedback_min_outcomes=20)
        props = feedback.analyze(d)
        thr = [p for p in props if p["knob"] == "sniper_tier_threshold"]
        self.assertEqual(len(thr), 1)
        self.assertEqual(thr[0]["suggested"], 55.0)
        self.assertEqual(thr[0]["current"], 50.0)
        # runner preservation: 8 runners total, 7 kept = 87.5% >= 80%
        kept, total = thr[0]["evidence"]["runners_kept"]
        self.assertGreaterEqual(kept / total, 0.8)

    def test_runner_preservation_blocks_bad_raise(self):
        recs = []
        # weak band has most of the runners -> raising kills them
        for i in range(10):
            recs.append(_rec(51, 3.0 if i < 8 else 1.1))
        for i in range(12):
            recs.append(_rec(70, 3.0 if i < 2 else 1.1))
        _write_dataset(self.dpath, recs)
        d = _degen(backtest_dataset_path=self.dpath,
                   feedback_min_outcomes=20)
        props = feedback.analyze(d)
        thr = [p for p in props if p["knob"] == "sniper_tier_threshold"]
        self.assertEqual(thr, [])  # would keep only 2/10 runners

    def test_boost_raise_proposal(self):
        recs = []
        # hot entries: 8 coins, 6 runners (75%)
        for i in range(6):
            recs.append(_rec(60, 3.0, heat=1.6))
        for i in range(2):
            recs.append(_rec(60, 1.2, heat=1.6))
        # rest: 14 coins, 3 runners (~21%)
        for i in range(3):
            recs.append(_rec(60, 2.5, heat=1.0))
        for i in range(11):
            recs.append(_rec(60, 1.1, heat=1.0))
        _write_dataset(self.dpath, recs)
        d = _degen(backtest_dataset_path=self.dpath,
                   feedback_min_outcomes=20)
        props = feedback.analyze(d)
        bst = [p for p in props if p["knob"] == "narrative_boost_max"]
        self.assertEqual(len(bst), 1)
        self.assertEqual(bst[0]["suggested"], 12.0)  # 8 + 4

    def test_boost_shrink_proposal(self):
        recs = []
        # hot entries: 8 coins, 1 runner (12.5%) — noise
        recs.append(_rec(60, 3.0, heat=1.6))
        for i in range(7):
            recs.append(_rec(60, 1.1, heat=1.6))
        # rest: 14 coins, 7 runners (50%)
        for i in range(7):
            recs.append(_rec(60, 2.5, heat=1.0))
        for i in range(7):
            recs.append(_rec(60, 1.1, heat=1.0))
        _write_dataset(self.dpath, recs)
        d = _degen(backtest_dataset_path=self.dpath,
                   feedback_min_outcomes=20)
        props = feedback.analyze(d)
        bst = [p for p in props if p["knob"] == "narrative_boost_max"]
        self.assertEqual(len(bst), 1)
        self.assertEqual(bst[0]["suggested"], 4.0)  # 8 - 4

    def test_only_tunable_knobs_proposed(self):
        recs = [_rec(60, 3.0 if i % 2 else 1.1, heat=1.6 if i % 3 else 1.0)
                for i in range(30)]
        _write_dataset(self.dpath, recs)
        d = _degen(backtest_dataset_path=self.dpath,
                   feedback_min_outcomes=20)
        for p in feedback.analyze(d):
            self.assertIn(p["knob"], feedback.TUNABLE)
            self.assertNotIn(p["knob"], feedback.SAFETY_NEVER_TUNE)

    def test_safety_knobs_in_never_tune(self):
        for k in ("early_min_score", "insider_cluster_max_pct",
                  "graph_insiders_max", "bundle_max_cluster_pct",
                  "daily_max_loss_sol"):
            self.assertIn(k, feedback.SAFETY_NEVER_TUNE)


class TestProposalStore(unittest.TestCase):
    def setUp(self):
        self.ppath = os.path.join(_TMP, "props_%d.json" % id(self))

    def _p(self, knob="sniper_tier_threshold", suggested=55.0):
        return {"id": "T-ABC%d" % suggested, "knob": knob,
                "current": 50.0, "suggested": suggested,
                "rationale": "test", "evidence": {},
                "created_ts": time.time(), "status": "open"}

    def test_add_and_dedupe(self):
        added = feedback.add_proposals([self._p()], self.ppath)
        self.assertEqual(len(added), 1)
        added2 = feedback.add_proposals([self._p()], self.ppath)
        self.assertEqual(added2, [])  # dupe skipped
        self.assertEqual(len(feedback.load_proposals(self.ppath)), 1)

    def test_safety_proposal_never_stored(self):
        p = self._p(knob="early_min_score", suggested=50.0)
        added = feedback.add_proposals([p], self.ppath)
        self.assertEqual(added, [])
        self.assertEqual(feedback.load_proposals(self.ppath), [])

    def test_mark_proposal(self):
        feedback.add_proposals([self._p()], self.ppath)
        hit = feedback.mark_proposal("T-ABC55", "applied", self.ppath)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["status"], "applied")
        self.assertEqual(feedback.mark_proposal("T-NOPE", "applied",
                                                self.ppath), None)

    def test_apply_override_safety_refused(self):
        with self.assertRaises(ValueError):
            feedback.apply_override("early_min_score", 50.0,
                                    os.path.join(_TMP, "ov1.json"))
        with self.assertRaises(ValueError):
            feedback.apply_override("not_a_knob", 1.0,
                                    os.path.join(_TMP, "ov2.json"))

    def test_apply_override_writes(self):
        opath = os.path.join(_TMP, "ov3.json")
        data = feedback.apply_override("sniper_tier_threshold", 55.0,
                                       opath)
        self.assertEqual(data["degen"]["sniper_tier_threshold"], 55.0)
        st = os.stat(opath)
        self.assertEqual(oct(st.st_mode & 0o777), "0o600")


class TestDailyScan(unittest.TestCase):
    def _cfg(self, **kw):
        return SimpleNamespace(degen=_degen(**kw))

    def test_runs_once_per_day(self):
        cfg = self._cfg()
        state: dict = {}
        # no dataset -> no proposals, but the day is still marked
        self.assertEqual(feedback.maybe_daily_scan(cfg, state,
                                                   time.time()), [])
        self.assertIn("feedback_last_scan_day", state)
        self.assertEqual(feedback.maybe_daily_scan(cfg, state,
                                                   time.time()), [])

    def test_disabled(self):
        cfg = self._cfg(feedback_enabled=False)
        self.assertEqual(feedback.maybe_daily_scan(cfg, {}, time.time()),
                         [])


class TestConfigOverrides(unittest.TestCase):
    def test_override_merges_on_top(self):
        from unittest import mock
        from config.config import load_config
        odata = {"degen": {"sniper_tier_threshold": 62.5}}
        opath = Path(_TMP) / "config_overrides.json"
        opath.write_text(json.dumps(odata), encoding="utf-8")
        try:
            # Pin HOME: other test modules overwrite FOREX_AGENT_HOME
            # without restoring it (test pollution).
            with mock.patch.dict(os.environ, {"FOREX_AGENT_HOME": _TMP}):
                cfg = load_config()
            self.assertEqual(cfg.degen.sniper_tier_threshold, 62.5)
            # untouched knobs keep defaults
            self.assertEqual(cfg.degen.narrative_boost_max, 8.0)
        finally:
            opath.unlink(missing_ok=True)

    def test_unknown_keys_ignored(self):
        from unittest import mock
        from config.config import load_config
        opath = Path(_TMP) / "config_overrides.json"
        opath.write_text(json.dumps(
            {"degen": {"nope_not_a_knob": 1}}), encoding="utf-8")
        try:
            with mock.patch.dict(os.environ, {"FOREX_AGENT_HOME": _TMP}):
                cfg = load_config()
            self.assertFalse(hasattr(cfg.degen, "nope_not_a_knob"))
        finally:
            opath.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
