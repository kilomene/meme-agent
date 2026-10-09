"""Tests for the #6 learning upgrade: entry/exit/context learners plus
proposal lifecycle (expiry, cooldown, follow-up grading).

The bot learns from real paper outcomes; nothing auto-applies; safety
knobs stay untouchable.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMP = tempfile.mkdtemp(prefix="forex_learners_")

from daemon.launch_scanner import feedback, sniper  # noqa: E402


def _degen(**kw):
    base = dict(sniper_enabled=True, sniper_tier_threshold=50.0,
                sniper_weight_fp=30.0, sniper_weight_smart=35.0,
                sniper_weight_velocity=20.0, sniper_weight_pressure=10.0,
                sniper_boost_deployer=5.0, narrative_boost_enabled=True,
                narrative_boost_max=8.0,
                take_profit_pct=200.0, stop_loss_pct=30.0,
                trail_trigger_pct=50.0, trail_drawdown_pct=30.0,
                max_hold_hours=4.0,
                feedback_enabled=True, feedback_min_outcomes=20,
                backtest_dataset_path=os.path.join(
                    _TMP, "ds_%d.jsonl" % id(kw)))
    base.update(kw)
    return SimpleNamespace(**base)


def _sig(**kw):
    s = {"smart_buys": 0.0, "fp": 0.0, "fp_verdict": "",
         "rising_polls": 0.0, "buy_pressure": 0.0,
         "hot_deployer": False, "heat": 1.0}
    s.update(kw)
    return s


def _xrec(exit_reason, peak, realized, signals=None, **kw):
    r = {"outcome_kind": "paper", "exit_reason": exit_reason,
         "peak_mult": peak, "realized_mult": realized,
         "rug": realized <= 0.10,
         "t_entry": time.time() - 3600, "t_close": time.time(),
         "entry_signals": signals if signals is not None else _sig(),
         "label": "launch", "source": "pump",
         "narratives": ["other"], "sniper_score": 60.0,
         "narrative_heat": 1.0}
    r.update(kw)
    return r


def _write(path, recs):
    with open(path, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def _props(tmp):
    return feedback.load_proposals(str(Path(tmp) / "props.json"))


class TestSignalSnapshot(unittest.TestCase):
    def test_snapshot_has_all_signals(self):
        signals = {"fp_composite": 80.0, "fp_verdict": "HOT",
                   "smart_buys": 2, "rising_polls": 3,
                   "buy_pressure": 0.7, "hot_deployer": True,
                   "narrative_heat": 1.4}
        r = sniper.rank_candidate(_degen(), signals)
        snap = r["signals_snapshot"]
        self.assertEqual(snap["smart_buys"], 2.0)
        self.assertEqual(snap["fp"], 80.0)
        self.assertEqual(snap["fp_verdict"], "HOT")
        self.assertEqual(snap["rising_polls"], 3.0)
        self.assertEqual(snap["buy_pressure"], 0.7)
        self.assertTrue(snap["hot_deployer"])
        self.assertEqual(snap["heat"], 1.4)


class TestEntryLearner(unittest.TestCase):
    def _ds(self, recs):
        d = _degen()
        _write(d.backtest_dataset_path, recs)
        return d

    def test_smart_money_weight_raised(self):
        recs = []
        for _ in range(10):  # smart coins: 8/10 runners
            recs.append(_xrec("trail_stop", 2.5, 1.8,
                              _sig(smart_buys=2.0)))
        for _ in range(2):
            recs.append(_xrec("stop_loss", 1.2, 0.6,
                              _sig(smart_buys=2.0)))
        for _ in range(10):  # no-smart coins: 5/18 runners
            recs.append(_xrec("trail_stop", 2.5, 1.8,
                              _sig(smart_buys=0.0)))
        for _ in range(13):
            recs.append(_xrec("stop_loss", 1.1, 0.6,
                              _sig(smart_buys=0.0)))
        d = self._ds(recs)  # 30 records
        props = feedback.analyze_entry(d, feedback.load_outcomes(
            d.backtest_dataset_path), time.time(), "2026-09-28", 20)
        smart = [p for p in props if p["knob"] == "sniper_weight_smart"]
        self.assertEqual(len(smart), 1)
        self.assertEqual(smart[0]["suggested"], 40.0)  # 35 + 5
        self.assertIn("smart-money", smart[0]["rationale"])

    def test_noisy_signal_weight_lowered(self):
        recs = []
        for _ in range(10):  # HOT fp: 1/10 runners
            recs.append(_xrec("trail_stop", 2.5, 1.8,
                              _sig(fp_verdict="HOT", fp=85.0)))
        for _ in range(9):
            recs.append(_xrec("stop_loss", 1.1, 0.6,
                              _sig(fp_verdict="HOT", fp=85.0)))
        for _ in range(11):  # not HOT: 8/11 runners
            recs.append(_xrec("trail_stop", 2.5, 1.8,
                              _sig(fp_verdict="WARMING", fp=50.0)))
        d = self._ds(recs)  # 30 records
        props = feedback.analyze_entry(d, feedback.load_outcomes(
            d.backtest_dataset_path), time.time(), "2026-09-28", 20)
        fp = [p for p in props if p["knob"] == "sniper_weight_fp"]
        self.assertEqual(len(fp), 1)
        self.assertEqual(fp[0]["suggested"], 25.0)  # 30 - 5
        self.assertIn("noise", fp[0]["rationale"])

    def test_max_two_entry_proposals(self):
        # smart AND velocity both predictive -> capped at 2
        recs = []
        for _ in range(10):
            recs.append(_xrec("trail_stop", 2.5, 1.8,
                              _sig(smart_buys=3.0, rising_polls=4.0)))
        for _ in range(2):
            recs.append(_xrec("stop_loss", 1.1, 0.6,
                              _sig(smart_buys=3.0, rising_polls=4.0)))
        for _ in range(18):
            recs.append(_xrec("stop_loss", 1.1, 0.6, _sig()))
        d = self._ds(recs)  # 30 records
        props = feedback.analyze_entry(d, feedback.load_outcomes(
            d.backtest_dataset_path), time.time(), "2026-09-28", 20)
        self.assertLessEqual(len(props), 2)

    def test_unscored_lanes_skipped(self):
        recs = [_xrec("trail_stop", 2.5, 1.8, None) for _ in range(20)]
        for r in recs:
            r["entry_signals"] = None
        d = self._ds(recs)
        props = feedback.analyze_entry(d, recs, time.time(), "2026-09-28", 20)
        self.assertEqual(props, [])


class TestExitLearner(unittest.TestCase):
    def _ds(self, recs):
        d = _degen()
        _write(d.backtest_dataset_path, recs)
        return d

    def _analyze(self, recs):
        d = self._ds(recs)
        return feedback.analyze_exits(
            d, feedback.load_outcomes(d.backtest_dataset_path),
            time.time(), "2026-09-28", 20)

    def _filler(self, n):
        return [_xrec("stop_loss", 1.1, 0.65) for _ in range(n)]

    def test_trail_giveback_tightens(self):
        # 8 profitable trail exits giving back 1.5x on average
        recs = [_xrec("trail_stop", 3.0, 1.5) for _ in range(8)]
        recs += self._filler(12)
        props = self._analyze(recs)
        dd = [p for p in props if p["knob"] == "trail_drawdown_pct"]
        self.assertEqual(len(dd), 1)
        self.assertEqual(dd[0]["suggested"], 25.0)  # 30 - 5

    def test_trail_shakeout_loosens(self):
        # 5 runners shaken out for +10%, 8 fine trail exits
        recs = [_xrec("trail_stop", 2.5, 1.1) for _ in range(5)]
        recs += [_xrec("trail_stop", 2.0, 1.5) for _ in range(8)]
        recs += self._filler(7)
        props = self._analyze(recs)
        dd = [p for p in props if p["knob"] == "trail_drawdown_pct"]
        self.assertEqual(len(dd), 1)
        self.assertEqual(dd[0]["suggested"], 35.0)  # 30 + 5

    def test_tp_fantasy_lowers(self):
        # TP hit 2/20; 10 runners peaked ~2.2x, below the +200% target
        recs = [_xrec("take_profit", 3.0, 3.0) for _ in range(2)]
        recs += [_xrec("trail_stop", 2.2, 1.4) for _ in range(10)]
        recs += self._filler(8)
        props = self._analyze(recs)
        tp = [p for p in props if p["knob"] == "take_profit_pct"]
        self.assertEqual(len(tp), 1)
        self.assertEqual(tp[0]["suggested"], 175.0)  # 200 - 25

    def test_flat_max_hold_shortens(self):
        recs = [_xrec("max_hold", 1.1, 1.0) for _ in range(6)]
        recs += [_xrec("max_hold", 1.4, 1.5) for _ in range(2)]
        recs += self._filler(12)
        props = self._analyze(recs)
        mh = [p for p in props if p["knob"] == "max_hold_hours"]
        self.assertEqual(len(mh), 1)
        self.assertEqual(mh[0]["suggested"], 3.0)  # 4 - 1

    def test_wicked_stops_arm_trail_earlier(self):
        # 5/8 stop-outs had peaked past +50% (the trail trigger)
        recs = [_xrec("stop_loss", 1.8, 0.6) for _ in range(5)]
        recs += [_xrec("stop_loss", 1.2, 0.6) for _ in range(3)]
        recs += [_xrec("max_hold", 1.1, 1.0) for _ in range(6)]
        recs += [_xrec("take_profit", 3.0, 3.0) for _ in range(6)]
        props = self._analyze(recs)
        tt = [p for p in props if p["knob"] == "trail_trigger_pct"]
        self.assertEqual(len(tt), 1)
        self.assertEqual(tt[0]["suggested"], 40.0)  # 50 - 10

    def _sl(self, n, peak=1.1, hold_min=20.0, **kw):
        now = time.time()
        return [_xrec("stop_loss", peak, 0.65,
                      t_entry=now - hold_min * 60, t_close=now,
                      **kw) for _ in range(n)]

    def _tp(self, n):
        return [_xrec("take_profit", 3.0, 3.0) for _ in range(n)]

    def _sl_props(self, recs):
        return [p for p in self._analyze(recs)
                if p["knob"] == "stop_loss_pct"]

    def test_stop_too_tight_widens_on_fast_shakeouts(self):
        # 10/20 stopped, all dead within 20 min -> widen 30 -> 35
        recs = self._sl(10, peak=1.1, hold_min=20.0) + self._tp(10)
        sl = self._sl_props(recs)
        self.assertEqual(len(sl), 1)
        self.assertEqual(sl[0]["suggested"], 35.0)
        self.assertIn("45 minutes", sl[0]["rationale"])

    def test_stop_too_tight_widens_on_promising_stopouts(self):
        # Stops not fast, but 8/8 had peaked +15% before stopping
        recs = self._sl(8, peak=1.3, hold_min=120.0) + self._tp(12)
        sl = self._sl_props(recs)
        self.assertEqual(len(sl), 1)
        self.assertEqual(sl[0]["suggested"], 35.0)

    def test_stop_too_loose_tightens_on_dead_bleeds(self):
        # 8/20 stopped, never peaked +5%, bled 120 min -> tighten 30 -> 25
        recs = self._sl(8, peak=1.02, hold_min=120.0) + self._tp(12)
        sl = self._sl_props(recs)
        self.assertEqual(len(sl), 1)
        self.assertEqual(sl[0]["suggested"], 25.0)
        self.assertIn("60+ minutes", sl[0]["rationale"])

    def test_stop_widen_wins_tie_over_tighten(self):
        # Bimodal: 6 fast promising + 6 dead slow -> exactly one
        # proposal, and it is the widen (shakeouts cost winners).
        recs = self._sl(6, peak=1.3, hold_min=20.0)
        recs += self._sl(6, peak=1.02, hold_min=120.0)
        recs += self._tp(8)
        sl = self._sl_props(recs)
        self.assertEqual(len(sl), 1)
        self.assertEqual(sl[0]["suggested"], 35.0)

    def test_stop_no_proposal_when_small_share(self):
        # 5/20 stops: below both the count and share bars
        recs = self._sl(5, peak=1.1, hold_min=20.0) + self._tp(15)
        self.assertEqual(self._sl_props(recs), [])

    def test_stop_respects_upper_clamp(self):
        d = _degen(stop_loss_pct=60.0)
        recs = self._sl(10, peak=1.1, hold_min=20.0) + self._tp(10)
        _write(d.backtest_dataset_path, recs)
        props = feedback.analyze_exits(
            d, feedback.load_outcomes(d.backtest_dataset_path),
            time.time(), "2026-09-28", 20)
        self.assertEqual(
            [p for p in props if p["knob"] == "stop_loss_pct"], [])

    def test_stop_respects_lower_clamp(self):
        d = _degen(stop_loss_pct=5.0)
        recs = self._sl(8, peak=1.02, hold_min=120.0) + self._tp(12)
        _write(d.backtest_dataset_path, recs)
        props = feedback.analyze_exits(
            d, feedback.load_outcomes(d.backtest_dataset_path),
            time.time(), "2026-09-28", 20)
        self.assertEqual(
            [p for p in props if p["knob"] == "stop_loss_pct"], [])

    def test_stop_hold_min_field_preferred_over_timestamps(self):
        # hold_min says 20 (fast) even though t_entry/t_close are far apart
        now = time.time()
        recs = [_xrec("stop_loss", 1.1, 0.65, hold_min=20.0,
                      t_entry=now - 7200, t_close=now)
                for _ in range(10)] + self._tp(10)
        sl = self._sl_props(recs)
        self.assertEqual(len(sl), 1)
        self.assertEqual(sl[0]["suggested"], 35.0)

    def test_stop_proposal_passes_analyze_guards(self):
        # End-to-end: the proposal survives the TUNABLE/cooldown/cap
        # guards in analyze() (never-tune set, knob cooldown, max 5).
        d = _degen()
        recs = self._sl(10, peak=1.1, hold_min=20.0) + self._tp(10)
        _write(d.backtest_dataset_path, recs)
        with mock.patch.object(feedback, "load_proposals", return_value=[]):
            props = feedback.analyze(d, time.time(), existing=[])
        sl = [p for p in props if p["knob"] == "stop_loss_pct"]
        self.assertEqual(len(sl), 1)
        self.assertEqual(sl[0]["area"], "exits")


class TestContextLearner(unittest.TestCase):
    def test_hot_and_cold_sources(self):
        recs = []
        for _ in range(8):
            recs.append(_xrec("trail_stop", 2.5, 1.8, source="pump"))
        for _ in range(4):
            recs.append(_xrec("stop_loss", 1.1, 0.6, source="pump"))
        for _ in range(12):
            recs.append(_xrec("stop_loss", 1.1, 0.6, source="gt"))
        ins = feedback.analyze_context(recs)
        by_label = {(i["kind"], i["label"]): i for i in ins}
        self.assertEqual(by_label[("source", "pump")]["mood"], "hot")
        self.assertEqual(by_label[("source", "gt")]["mood"], "cold")

    def test_too_few_records_no_insights(self):
        self.assertEqual(feedback.analyze_context(
            [_xrec("trail_stop", 2.0, 1.5) for _ in range(5)]), [])


class TestLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forex_life_")
        self.ppath = str(Path(self.tmp) / "props.json")

    def _open(self, knob, suggested, created_ago_days):
        return {"id": "T-OLD", "knob": knob, "current": 1.0,
                "suggested": suggested, "rationale": "x",
                "evidence": {}, "created_ts": time.time() -
                created_ago_days * 86400, "status": "open"}

    def test_expiry(self):
        feedback.save_proposals([self._open("take_profit_pct", 175.0, 15)],
                                self.ppath)
        n = feedback.expire_stale(self.ppath, time.time())
        self.assertEqual(n, 1)
        self.assertEqual(_props(self.tmp)[0]["status"], "expired")

    def test_no_expiry_when_fresh(self):
        feedback.save_proposals([self._open("take_profit_pct", 175.0, 3)],
                                self.ppath)
        self.assertEqual(feedback.expire_stale(self.ppath, time.time()), 0)

    def test_cooldown_blocks_reproposal(self):
        # TP fantasy data would propose take_profit_pct, but the knob
        # was applied 3 days ago -> skipped.
        recs = [_xrec("take_profit", 3.0, 3.0) for _ in range(2)]
        recs += [_xrec("trail_stop", 2.2, 1.4) for _ in range(10)]
        recs += [_xrec("stop_loss", 1.1, 0.6) for _ in range(8)]
        d = _degen()
        _write(d.backtest_dataset_path, recs)
        existing = [{"id": "T-X", "knob": "take_profit_pct",
                     "status": "applied",
                     "decided_ts": time.time() - 3 * 86400}]
        props = feedback.analyze(d, time.time(), existing)
        self.assertFalse(any(p["knob"] == "take_profit_pct"
                             for p in props))

    def test_cooldown_expires(self):
        recs = [_xrec("take_profit", 3.0, 3.0) for _ in range(2)]
        recs += [_xrec("trail_stop", 2.2, 1.4) for _ in range(10)]
        recs += [_xrec("stop_loss", 1.1, 0.6) for _ in range(8)]
        d = _degen()
        _write(d.backtest_dataset_path, recs)
        existing = [{"id": "T-X", "knob": "take_profit_pct",
                     "status": "applied",
                     "decided_ts": time.time() - 10 * 86400}]
        props = feedback.analyze(d, time.time(), existing)
        self.assertTrue(any(p["knob"] == "take_profit_pct"
                            for p in props))

    def _applied(self, baseline_rate, decided_days_ago):
        base = [{"peak_mult": 2.5 if i < int(20 * baseline_rate) else 1.2,
                 "realized_mult": 2.0 if i < int(20 * baseline_rate) else 1.0,
                 "rug": False} for i in range(20)]
        p = {"id": "T-G", "knob": "take_profit_pct", "status": "applied",
             "decided_ts": time.time() - decided_days_ago * 86400,
             "baseline": {"n": 20, "runner_rate": baseline_rate,
                          "avg_realized": 1.2},
             "followup": "pending"}
        feedback.save_proposals([p], self.ppath)
        return base

    def _after(self, rate, n, decided_days_ago):
        return [{"peak_mult": 2.5 if i < int(n * rate) else 1.2,
                 "realized_mult": 2.0, "rug": False,
                 "t_close": time.time() -
                 (decided_days_ago - 1) * 86400} for i in range(n)]

    def test_grading_helped(self):
        self._applied(0.2, 9)
        recs = self._after(0.5, 12, 9)
        self.assertEqual(feedback.grade_applied(
            self.ppath, recs, time.time()), 1)
        self.assertEqual(_props(self.tmp)[0]["followup"], "helped")

    def test_grading_hurt(self):
        self._applied(0.4, 9)
        recs = self._after(0.1, 12, 9)
        feedback.grade_applied(self.ppath, recs, time.time())
        self.assertEqual(_props(self.tmp)[0]["followup"], "hurt")

    def test_grading_unclear(self):
        self._applied(0.3, 9)
        recs = self._after(0.33, 12, 9)
        feedback.grade_applied(self.ppath, recs, time.time())
        self.assertEqual(_props(self.tmp)[0]["followup"], "unclear")

    def test_grading_waits_for_evidence(self):
        self._applied(0.3, 9)
        recs = self._after(0.5, 5, 9)  # only 5 closes
        self.assertEqual(feedback.grade_applied(
            self.ppath, recs, time.time()), 0)
        self.assertEqual(_props(self.tmp)[0]["followup"], "pending")


class TestGuards(unittest.TestCase):
    def test_trail_knobs_now_tunable(self):
        opath = Path(_TMP) / "ovr1.json"
        try:
            feedback.apply_override("trail_drawdown_pct", 25.0,
                                    str(opath))
            data = json.loads(opath.read_text(encoding="utf-8"))
            self.assertEqual(data["degen"]["trail_drawdown_pct"], 25.0)
        finally:
            opath.unlink(missing_ok=True)

    def test_safety_still_refused(self):
        for knob in ("early_min_score", "daily_max_loss_sol",
                     "instant_max_per_hour", "kill_switch_enabled"):
            with self.assertRaises(ValueError):
                feedback.apply_override(knob, 5.0,
                                        str(Path(_TMP) / "ovr2.json"))

    def test_apply_clamps(self):
        opath = Path(_TMP) / "ovr3.json"
        try:
            feedback.apply_override("take_profit_pct", 5000.0,
                                    str(opath))
            data = json.loads(opath.read_text(encoding="utf-8"))
            self.assertEqual(data["degen"]["take_profit_pct"], 1000.0)
        finally:
            opath.unlink(missing_ok=True)

    def test_config_merge_allows_tunable_only(self):
        from config.config import load_config
        opath = Path(_TMP) / "config_overrides.json"
        opath.write_text(json.dumps(
            {"degen": {"early_min_score": 5.0,
                       "sniper_weight_smart": 40.0}}), encoding="utf-8")
        try:
            with mock.patch.dict(os.environ, {"FOREX_AGENT_HOME": _TMP}):
                cfg = load_config()
            # safety knob ignored — the standing 1/100 bar stands
            self.assertEqual(cfg.degen.early_min_score, 1.0)
            # tunable knob merges
            self.assertEqual(cfg.degen.sniper_weight_smart, 40.0)
        finally:
            opath.unlink(missing_ok=True)

    def test_run_scan_end_to_end(self):
        home = tempfile.mkdtemp(prefix="forex_scan_")
        d = _degen(backtest_dataset_path=os.path.join(
            home, "ds.jsonl"))
        recs = [_xrec("take_profit", 3.0, 3.0, source="pump")
                for _ in range(2)]
        recs += [_xrec("trail_stop", 2.2, 1.4, source="pump")
                 for _ in range(10)]
        recs += [_xrec("stop_loss", 1.1, 0.6, source="gt")
                 for _ in range(12)]
        _write(d.backtest_dataset_path, recs)
        with mock.patch.dict(os.environ, {"FOREX_AGENT_HOME": home}):
            new, insights = feedback.run_scan(d, time.time())
            # stored
            stored_p = feedback.load_proposals()
            stored_i = feedback.load_insights()
        self.assertTrue(any(p["knob"] == "take_profit_pct" for p in new))
        self.assertTrue(len(insights) > 0)
        self.assertTrue(len(stored_p) > 0)
        self.assertTrue(len(stored_i) > 0)


if __name__ == "__main__":
    unittest.main()
