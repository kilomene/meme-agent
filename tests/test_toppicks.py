"""Tests for the Top Picks shortlist (user decision 2026-10-01).

The daily card ranks the last 24h of confirmed runners by composite
score (sniper 30 / winner 25 / holder 20 / smart-money 15 /
fingerprint 10) and publishes ONE shortlist card — the few coins to
gamble from instead of 200+ alerts. Ranking only: gates, bar, and
caps are untouched; picks are alert-only, never auto-traded.
"""
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from agent.events import bus
from daemon.launch_scanner import toppicks
import daemon.telegram.alerts as alerts

WAT = timezone(timedelta(hours=1))


def _degen(**kw):
    d = {"toppicks_enabled": True, "toppicks_count": 5,
         "toppicks_time": "08:00"}
    d.update(kw)
    return types.SimpleNamespace(**d)


def _snap(**kw):
    s = {"symbol": "T", "chain": "solana", "sniper": 50.0,
         "winner": 50.0, "holder": 50.0, "smart_buys": 0,
         "fp": "", "mcap": 100000.0}
    s.update(kw)
    return s


class TestComposite(unittest.TestCase):
    def test_weights_sum_to_full_scale(self):
        score, why = toppicks.composite(_snap(
            sniper=100, winner=100, holder=100, smart_buys=3, fp="HOT"))
        self.assertAlmostEqual(score, 100.0)
        self.assertTrue(why)

    def test_zero_inputs_score_zero(self):
        score, _ = toppicks.composite(_snap(
            sniper=0, winner=0, holder=0, smart_buys=0, fp="COLD"))
        # smart 0 + finger COLD(20)*0.10 = 2.0
        self.assertAlmostEqual(score, 2.0)

    def test_smart_buys_capped(self):
        a, _ = toppicks.composite(_snap(smart_buys=3))
        b, _ = toppicks.composite(_snap(smart_buys=10))
        self.assertAlmostEqual(a, b)

    def test_never_raises_on_garbage(self):
        score, why = toppicks.composite({"sniper": "x", "fp": None})
        self.assertIsInstance(score, float)
        self.assertIsInstance(why, list)


class TestRecordAndBuild(unittest.TestCase):
    def test_ranking_best_first(self):
        st = {}
        now = time.time()
        toppicks.record_scores(st, "m_low", _snap(symbol="LOW", sniper=10,
                                                 winner=10, holder=10),
                               now=now)
        toppicks.record_scores(st, "m_high", _snap(symbol="HIGH", sniper=90,
                                                  winner=90, holder=90),
                               now=now)
        picks = toppicks.build_picks(st, n=5, now=now)
        self.assertEqual(len(picks), 2)
        self.assertEqual(picks[0]["symbol"], "HIGH")
        self.assertGreater(picks[0]["score"], picks[1]["score"])
        self.assertTrue(picks[0]["why"])

    def test_window_excludes_old(self):
        st = {}
        now = time.time()
        toppicks.record_scores(st, "m_old", _snap(symbol="OLD"), now=now)
        # Simulate a 30h-old snapshot by rewriting ts.
        st[toppicks.SCORE_KEY]["m_old"]["ts"] = now - 30 * 3600
        picks = toppicks.build_picks(st, n=5, now=now)
        self.assertEqual(picks, [])

    def test_bounded(self):
        st = {}
        now = time.time()
        for i in range(toppicks._MAX_SCORES + 10):
            toppicks.record_scores(st, "m%d" % i, _snap(), now=now)
        self.assertLessEqual(len(st[toppicks.SCORE_KEY]),
                             toppicks._MAX_SCORES)

    def test_n_clamped(self):
        st = {}
        now = time.time()
        for i in range(12):
            toppicks.record_scores(st, "m%d" % i, _snap(), now=now)
        self.assertEqual(len(toppicks.build_picks(st, n=99, now=now)), 10)
        self.assertEqual(len(toppicks.build_picks(st, n=-2, now=now)), 1)
        # n=0 is falsy -> default 5.
        self.assertEqual(len(toppicks.build_picks(st, n=0, now=now)), 5)


class TestSlot(unittest.TestCase):
    def _ts(self, h, m, day_off=0):
        base = datetime(2026, 10, 1, tzinfo=WAT) + timedelta(days=day_off)
        return base.replace(hour=h, minute=m).timestamp()

    def test_not_due_before_slot(self):
        self.assertIsNone(toppicks.slot_due(_degen(), self._ts(7, 59)))

    def test_due_after_slot(self):
        self.assertEqual(toppicks.slot_due(_degen(), self._ts(8, 0)),
                         "2026-10-01")
        self.assertEqual(toppicks.slot_due(_degen(), self._ts(23, 59)),
                         "2026-10-01")

    def test_bad_time_falls_back(self):
        self.assertEqual(
            toppicks.slot_due(_degen(toppicks_time="xx"), self._ts(9, 0)),
            "2026-10-01")


class TestDailyRun(unittest.TestCase):
    def _cfg(self, **kw):
        return types.SimpleNamespace(degen=_degen(**kw))

    def _ts(self, h, m):
        return datetime(2026, 10, 1, h, m, tzinfo=WAT).timestamp()

    def test_publishes_once_per_day(self):
        st = {}
        now = self._ts(9, 0)
        toppicks.record_scores(st, "m1", _snap(symbol="A", sniper=80,
                                              winner=80, holder=80),
                               now=now)
        with mock.patch("agent.events.publish") as pub:
            picks = toppicks.maybe_daily_picks(self._cfg(), st, now,
                                               milestones={})
        self.assertIsNotNone(picks)
        self.assertEqual(pub.call_count, 1)
        ev = pub.call_args[0][0]
        self.assertEqual(ev["event"], "launch.top_picks")
        bus.validate_event(dict(ev))  # schema must accept it
        self.assertEqual(st[toppicks.SLOT_KEY], "2026-10-01")
        # Second call same day: silent.
        with mock.patch("agent.events.publish") as pub2:
            self.assertIsNone(
                toppicks.maybe_daily_picks(self._cfg(), st, now,
                                           milestones={}))
        self.assertEqual(pub2.call_count, 0)

    def test_disabled_is_silent(self):
        st = {}
        now = self._ts(9, 0)
        toppicks.record_scores(st, "m1", _snap(), now=now)
        with mock.patch("agent.events.publish") as pub:
            self.assertIsNone(toppicks.maybe_daily_picks(
                self._cfg(toppicks_enabled=False), st, now,
                milestones={}))
        self.assertEqual(pub.call_count, 0)

    def test_marks_slot_when_empty(self):
        st = {}
        now = self._ts(9, 0)
        with mock.patch("agent.events.publish") as pub:
            self.assertIsNone(
                toppicks.maybe_daily_picks(self._cfg(), st, now,
                                           milestones={}))
        self.assertEqual(pub.call_count, 0)
        self.assertEqual(st[toppicks.SLOT_KEY], "2026-10-01")

    def test_yesterday_graded_from_milestones(self):
        st = {toppicks.LOG_KEY: [
            {"date": "2026-09-30",
             "picks": [{"mint": "win1", "symbol": "W", "score": 90,
                        "anchor_mcap": 1.0},
                       {"mint": "dead1", "symbol": "D", "score": 80,
                        "anchor_mcap": 1.0}]}]}
        now = self._ts(9, 0)
        toppicks.record_scores(st, "m1", _snap(symbol="A", sniper=90,
                                              winner=90, holder=90),
                               now=now)
        ms = {"win1": {"peak_mult": 3.2}, "dead1": {"peak_mult": 1.1}}
        with mock.patch("agent.events.publish") as pub:
            toppicks.maybe_daily_picks(self._cfg(), st, now,
                                       milestones=ms)
        ev = pub.call_args[0][0]
        y = ev["yesterday"]
        self.assertEqual(y["date"], "2026-09-30")
        self.assertEqual(y["count"], 2)
        self.assertEqual(y["hit_2x"], 1)
        self.assertAlmostEqual(y["best_mult"], 3.2)

    def test_never_raises(self):
        st = {"pick_scores": "garbage"}
        self.assertIsNone(toppicks.maybe_daily_picks(
            self._cfg(), st, self._ts(9, 0), milestones=None))


class TestRenderer(unittest.TestCase):
    def test_top_picks_card(self):
        e = {"event": "launch.top_picks", "date": "2026-10-01",
             "count": 2,
             "picks": [
                 {"mint": "CA1", "symbol": "AAA", "chain": "solana",
                  "score": 87.5, "why": "sniper 90/100; 2 tracked wallets in"},
                 {"mint": "CA2", "symbol": "BBB", "chain": "base",
                  "score": 71.0, "why": "winner 80/100; fingerprint HOT"}],
             "yesterday": {"date": "2026-09-30", "count": 5,
                           "hit_2x": 1, "best_mult": 3.2},
             "note": "Paper-tracked."}
        bus.validate_event(dict(e))
        text = alerts._top_picks(e)
        self.assertIn("🎯 TOP 2 PICKS", text)
        self.assertIn("CA1", text)
        self.assertIn("CA2", text)
        self.assertIn("1/5 hit 2x+", text)
        # Registered on the dispatch map.
        self.assertIs(alerts.FORMATTERS["launch.top_picks"],
                      alerts._top_picks)

    def test_renderer_handles_empty(self):
        e = {"event": "launch.top_picks", "date": "2026-10-01",
             "count": 0, "picks": []}
        text = alerts._top_picks(e)
        self.assertIn("TOP 0 PICKS", text)


class TestConfigKnobs(unittest.TestCase):
    def test_degen_config_declares_toppicks(self):
        # Regression: YAML knobs must exist on DegenConfig, not just
        # getattr fallbacks (the 2026-10-01 selectivity bug).
        from config.config import DegenConfig
        import dataclasses
        names = {f.name for f in dataclasses.fields(DegenConfig)}
        for k in ("toppicks_enabled", "toppicks_count", "toppicks_time"):
            self.assertIn(k, names)


if __name__ == "__main__":
    unittest.main()
