"""Tests for the weekly learner review (2026-09-28, user decision).

One card every Monday 08:00 WAT: new proposals, applied changes +
grades, the week's win rate vs the prior week. Informational only —
/tune apply stays the only write path. /review shows it on demand.
"""

import calendar
import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from agent.events import bus as event_bus  # noqa: E402
from daemon.launch_scanner import feedback  # noqa: E402
from daemon.telegram import alerts as tg_alerts  # noqa: E402


def _wat_ts(y, mo, d, h, mi):
    # WAT = UTC+1: the given wall-clock in Africa/Lagos.
    return calendar.timegm((y, mo, d, h - 1, mi, 0))


MON_0800 = _wat_ts(2026, 9, 28, 8, 0)   # Monday 2026-09-28 08:00 WAT


def _degen(tmp, **kw):
    d = {"review_enabled": True, "review_weekday": 0,
         "review_time": "08:00",
         "backtest_dataset_path": os.path.join(tmp, "ds.jsonl")}
    d.update(kw)
    return SimpleNamespace(**d)


def _close(t_close, peak=1.1, realized=0.7):
    return {"outcome_kind": "paper", "t_close": t_close,
            "peak_mult": peak, "realized_mult": realized,
            "rug": realized <= 0.10}


def _prop(pid, knob, status, created_days_ago, decided_days_ago=None,
          followup=None, now=None):
    # Anchor to the review's fixed clock by default: the partition
    # logic is relative to the review's `now`, and real time drifts.
    now = MON_0800 + 3600 if now is None else now
    p = {"id": pid, "knob": knob, "area": "exits",
         "current": 30.0, "suggested": 35.0,
         "status": status, "created_ts": now - created_days_ago * 86400}
    if decided_days_ago is not None:
        p["decided_ts"] = now - decided_days_ago * 86400
    if followup:
        p["followup"] = followup
    return p


def _write_store(tmp, proposals, insights=None):
    path = os.path.join(tmp, "store.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"version": 2, "proposals": proposals,
                   "insights": insights or [], "insights_ts": time.time()},
                  fh)
    return path


def _write_ds(tmp, recs):
    path = os.path.join(tmp, "ds.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    return path


class ReviewSlotTests(unittest.TestCase):
    def test_due_monday_after_slot(self):
        d = _degen(tempfile.mkdtemp())
        self.assertEqual(feedback._review_slot(d, MON_0800 + 3600),
                         "2026-09-28")

    def test_not_due_before_slot(self):
        d = _degen(tempfile.mkdtemp())
        self.assertIsNone(feedback._review_slot(d, MON_0800 - 3600))

    def test_sunday_maps_to_same_monday(self):
        d = _degen(tempfile.mkdtemp())
        sun = _wat_ts(2026, 10, 4, 9, 0)
        self.assertEqual(feedback._review_slot(d, sun), "2026-09-28")

    def test_disabled_never_due(self):
        d = _degen(tempfile.mkdtemp(), review_enabled=False)
        cfg = SimpleNamespace(degen=d)
        self.assertIsNone(
            feedback.maybe_weekly_review(cfg, {}, MON_0800 + 3600))

    def test_bad_time_falls_back(self):
        d = _degen(tempfile.mkdtemp(), review_time="nope")
        self.assertEqual(feedback._review_slot(d, MON_0800 + 3600),
                         "2026-09-28")


class BuildReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forex_review_")
        self.now = MON_0800 + 3600

    def _review(self, proposals, recs, insights=None):
        store = _write_store(self.tmp, proposals, insights)
        _write_ds(self.tmp, recs)
        return feedback.build_weekly_review(
            _degen(self.tmp), self.now, path=store)

    def test_partitions_proposals(self):
        props = [
            _prop("P1", "stop_loss_pct", "open", 2),          # new
            _prop("P2", "take_profit_pct", "open", 10),       # old
            _prop("P3", "max_hold_hours", "applied", 9, 2, "helped"),
            _prop("P4", "trail_drawdown_pct", "dismissed", 3),
        ]
        e = self._review(props, [])
        self.assertEqual([p["id"] for p in e["new_proposals"]], ["P1"])
        self.assertEqual(e["new_proposals_total"], 1)
        self.assertEqual(e["open_older"], 1)
        self.assertEqual(len(e["applied"]), 1)
        self.assertEqual(e["applied"][0]["followup"], "helped")

    def test_week_stats_vs_prior(self):
        recs = [_close(self.now - 86400, peak=3.0, realized=2.5)   # runner
                for _ in range(2)]
        recs += [_close(self.now - 86400, peak=1.1, realized=0.7)
                 for _ in range(6)]
        recs += [_close(self.now - 10 * 86400, peak=1.1, realized=0.7)
                 for _ in range(4)]
        e = self._review([], recs)
        st = e["stats"]
        self.assertEqual(st["n"], 8)
        self.assertEqual(st["runner_rate"], 0.25)
        self.assertEqual(st["prev_n"], 4)
        self.assertEqual(st["prev_runner_rate"], 0.0)

    def test_insights_capped(self):
        ins = [{"kind": "k", "label": "l%d" % i, "mood": "hot",
                "detail": "d"} for i in range(5)]
        e = self._review([], [], insights=ins)
        self.assertEqual(len(e["insights"]), 3)

    def test_event_validates(self):
        e = self._review([_prop("P1", "stop_loss_pct", "open", 2)], [])
        event_bus.validate_event(dict(e))  # raises on schema violation

    def test_never_raises_on_garbage(self):
        d = _degen(self.tmp)
        e = feedback.build_weekly_review(d, self.now,
                                         path="/nonexistent/x.json")
        self.assertEqual(e["event"], "tuning.weekly_review")


class RenderReviewTests(unittest.TestCase):
    def test_card_renders(self):
        e = {"event": "tuning.weekly_review", "week": "2026-09-28",
             "new_proposals": [
                 {"id": "SL-1", "knob": "stop_loss_pct", "area": "exits",
                  "current": 30.0, "suggested": 35.0}],
             "new_proposals_total": 1, "open_older": 2,
             "applied": [{"knob": "take_profit_pct", "current": 200.0,
                          "suggested": 175.0, "followup": "pending"}],
             "stats": {"n": 42, "runner_rate": 0.07,
                       "avg_realized": 0.81, "prev_runner_rate": 0.09},
             "insights": [{"kind": "source", "label": "pump",
                           "mood": "hot", "detail": "x"}],
             "note": "The bot proposes, you decide."}
        text = tg_alerts._tuning_weekly_review(e)
        self.assertIn("WEEKLY LEARNER REVIEW", text)
        self.assertIn("stop_loss_pct", text)
        self.assertIn("SL-1", text)
        self.assertIn("grade: pending", text)
        self.assertIn("2 older proposals", text)
        self.assertIn("42 closes", text)

    def test_empty_week(self):
        e = {"event": "tuning.weekly_review", "week": "2026-09-28",
             "new_proposals": [], "new_proposals_total": 0,
             "open_older": 0, "applied": [],
             "stats": {"n": 0, "runner_rate": 0.0, "avg_realized": 0.0,
                       "prev_runner_rate": 0.0},
             "insights": [], "note": ""}
        text = tg_alerts._tuning_weekly_review(e)
        self.assertIn("No closes this week yet", text)


class MaybeReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forex_review_")
        _write_ds(self.tmp, [])
        self.store = _write_store(self.tmp, [])

    def _cfg(self, **kw):
        return SimpleNamespace(degen=_degen(self.tmp, **kw))

    def test_publishes_and_marks_slot(self):
        from agent import events as events_pkg
        seen = []
        with unittest.mock.patch.object(
                events_pkg, "publish",
                side_effect=lambda e: seen.append(e)):
            with unittest.mock.patch.object(
                    feedback, "build_weekly_review",
                    return_value={"event": "tuning.weekly_review",
                                  "new_proposals_total": 1,
                                  "applied": []}):
                state = {}
                e = feedback.maybe_weekly_review(
                    self._cfg(), state, MON_0800 + 3600)
        self.assertIsNotNone(e)
        self.assertEqual(len(seen), 1)
        self.assertEqual(state["review_last_slot"], "2026-09-28")
        # second call same week: not due
        self.assertIsNone(feedback.maybe_weekly_review(
            self._cfg(), state, MON_0800 + 7200))

    def test_publish_failure_retries(self):
        from agent import events as events_pkg
        with unittest.mock.patch.object(
                events_pkg, "publish",
                side_effect=RuntimeError("bus down")):
            state = {}
            self.assertIsNone(feedback.maybe_weekly_review(
                self._cfg(), state, MON_0800 + 3600))
        self.assertNotIn("review_last_slot", state)


if __name__ == "__main__":
    unittest.main()
