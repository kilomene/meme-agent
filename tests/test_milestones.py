"""Tests for milestone tracking (🏆 cards on carded-coin runners).

- track_signal registers coins from signal events; idempotent per mint.
- poll fires each threshold (5/10/25/50/100x) exactly once, anchored to
  OUR card-time mcap; pending (zero-mcap) anchors backfill on first price.
- milestone events validate against the strict event-bus schema.
- the Telegram card renders; filters treat it as a signal event
  (quiet-hours batching, min-score overlay).
"""

import time
import unittest
import unittest.mock
from types import SimpleNamespace

from agent.events import bus as event_bus
from daemon.launch_scanner import milestones
from daemon.telegram import alerts as tg_alerts
from daemon.telegram import filters as tg_filters


def _degen(**kw):
    d = {"milestones_enabled": True}
    d.update(kw)
    return SimpleNamespace(**d)


def _sig(**kw):
    e = {"event": "launch.early", "mint": "MINTAAA", "symbol": "TKN",
         "chain": "solana", "name": "Token", "url": "http://x",
         "usd_market_cap": 10000.0, "safety_score": 72.0}
    e.update(kw)
    return e


class TrackTests(unittest.TestCase):
    def test_register_and_idempotent(self):
        state, degen = {}, _degen()
        self.assertTrue(milestones.track_signal(state, degen, _sig()))
        first = state["milestone_watch"]["MINTAAA"]["anchor"]
        # second card for same mint keeps the FIRST anchor
        milestones.track_signal(state, degen,
                                _sig(usd_market_cap=99999.0))
        self.assertEqual(state["milestone_watch"]["MINTAAA"]["anchor"],
                         first)
        self.assertEqual(first, 10000.0)

    def test_disabled_tracks_nothing(self):
        state = {}
        self.assertFalse(milestones.track_signal(state, _degen(
            milestones_enabled=False), _sig()))
        self.assertNotIn("milestone_watch", state)

    def test_no_mint_no_track(self):
        state = {}
        self.assertFalse(milestones.track_signal(state, _degen(),
                                                 _sig(mint="")))
        self.assertEqual(state.get("milestone_watch") or {}, {})

    def test_pending_anchor(self):
        state = {}
        milestones.track_signal(state, _degen(), _sig(usd_market_cap=0))
        rec = state["milestone_watch"]["MINTAAA"]
        self.assertTrue(rec["pending"])
        self.assertEqual(rec["anchor"], 0.0)

    def test_watch_bounded(self):
        state, degen = {}, _degen()
        for i in range(milestones._MAX_TRACKED + 10):
            milestones.track_signal(state, degen,
                                    _sig(mint="MINT%04d" % i))
        self.assertLessEqual(len(state["milestone_watch"]),
                             milestones._MAX_TRACKED)


class PollTests(unittest.TestCase):
    def _stats(self, mcap):
        return {"MINTAAA": {"usd_market_cap": mcap}}

    def test_threshold_fires_once_each(self):
        state, degen = {}, _degen()
        milestones.track_signal(state, degen, _sig())  # anchor 10k
        fired = []
        for mcap in (51000.0, 105000.0, 260000.0, 510000.0):
            with unittest.mock.patch.object(
                    milestones, "_batch_stats",
                    return_value=self._stats(mcap)):
                fired += milestones.poll(state, degen, time.time(), 4)
        thresholds = sorted(e["threshold"] for e in fired)
        self.assertEqual(thresholds, [2.0, 5.0, 10.0, 25.0, 50.0])
        # re-poll at same levels: nothing new
        with unittest.mock.patch.object(
                milestones, "_batch_stats",
                return_value=self._stats(510000.0)):
            self.assertEqual(milestones.poll(state, degen, time.time(), 8),
                             [])

    def test_no_retirement_at_top(self):
        # The ladder is open-ended now: past 1000x the thresholds keep
        # doubling, so the coin stays tracked until the 24h expiry.
        state, degen = {}, _degen()
        milestones.track_signal(state, degen, _sig())
        with unittest.mock.patch.object(
                milestones, "_batch_stats",
                return_value=self._stats(1100000.0)):
            evts = milestones.poll(state, degen, time.time(), 4)
        self.assertTrue(any(e["threshold"] == 100.0 for e in evts))
        self.assertIn("MINTAAA", state["milestone_watch"])

    def test_dynamic_tail_doubles_past_ladder(self):
        state, degen = {}, _degen()
        milestones.track_signal(state, degen, _sig())  # anchor 10k
        with unittest.mock.patch.object(
                milestones, "_batch_stats",
                return_value=self._stats(25000000.0)):  # 2500x
            evts = milestones.poll(state, degen, time.time(), 4)
        thresholds = sorted(e["threshold"] for e in evts)
        self.assertIn(1000.0, thresholds)
        self.assertIn(2000.0, thresholds)   # dynamic tail
        self.assertNotIn(4000.0, thresholds)  # not reached
        # tail fires once: re-poll adds nothing new
        with unittest.mock.patch.object(
                milestones, "_batch_stats",
                return_value=self._stats(25000000.0)):
            evts2 = milestones.poll(state, degen, time.time(), 8)
        self.assertEqual(evts2, [])

    def test_pending_backfill_then_fires(self):
        state, degen = {}, _degen()
        milestones.track_signal(state, degen, _sig(usd_market_cap=0))
        with unittest.mock.patch.object(
                milestones, "_batch_stats",
                return_value=self._stats(20000.0)):
            self.assertEqual(milestones.poll(state, degen, time.time(), 4),
                             [])  # anchor set, no multiple yet
        rec = state["milestone_watch"]["MINTAAA"]
        self.assertFalse(rec["pending"])
        self.assertEqual(rec["anchor"], 20000.0)
        with unittest.mock.patch.object(
                milestones, "_batch_stats",
                return_value=self._stats(100000.0)):
            evts = milestones.poll(state, degen, time.time(), 8)
        self.assertEqual(sorted(e["threshold"] for e in evts), [2.0, 5.0])
        self.assertEqual(evts[0]["multiple"], 5.0)

    def test_dust_floor_blocks(self):
        state = {}
        degen = _degen(milestone_min_mcap_now_usd=5000.0)
        milestones.track_signal(state, degen, _sig(usd_market_cap=100.0))
        with unittest.mock.patch.object(
                milestones, "_batch_stats",
                return_value=self._stats(1000.0)):  # 10x but <$5k
            self.assertEqual(milestones.poll(state, degen, time.time(), 4),
                             [])

    def test_expiry(self):
        state, degen = {}, _degen()
        milestones.track_signal(state, degen, _sig())
        state["milestone_watch"]["MINTAAA"]["card_ts"] = (
            time.time() - milestones._EXPIRY_S - 1)
        with unittest.mock.patch.object(
                milestones, "_batch_stats",
                return_value=self._stats(999999.0)):
            self.assertEqual(milestones.poll(state, degen, time.time(), 4),
                             [])
        self.assertNotIn("MINTAAA", state["milestone_watch"])

    def test_throttle(self):
        state, degen = {}, _degen()
        milestones.track_signal(state, degen, _sig())
        with unittest.mock.patch.object(
                milestones, "_batch_stats",
                return_value=self._stats(999999.0)) as bs:
            milestones.poll(state, degen, time.time(), 3)  # not a poll cycle
            bs.assert_not_called()

    def test_custom_thresholds(self):
        degen = _degen(milestone_thresholds=[3, 7])
        self.assertEqual(milestones.thresholds(degen), [3.0, 7.0])
        self.assertEqual(milestones.thresholds(_degen()),
                         [2.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0,
                          1000.0])

    def test_event_fields(self):
        state, degen = {}, _degen()
        milestones.track_signal(state, degen, _sig())
        with unittest.mock.patch.object(
                milestones, "_batch_stats",
                return_value=self._stats(55000.0)):
            evts = milestones.poll(state, degen, time.time(), 4)
        e = evts[0]
        self.assertEqual(e["event"], "launch.milestone")
        self.assertEqual(e["symbol"], "TKN")
        self.assertEqual(e["mint"], "MINTAAA")
        self.assertEqual(e["card_mcap"], 10000.0)
        self.assertEqual(e["mcap_now"], 55000.0)
        self.assertEqual(e["multiple"], 5.5)
        self.assertEqual(e["safety_score"], 72.0)


class SchemaTests(unittest.TestCase):
    def test_milestone_schema_registered(self):
        self.assertIn("launch.milestone", event_bus.EVENT_SCHEMAS)

    def test_milestone_event_validates(self):
        evt = {"event": "launch.milestone", "symbol": "TKN",
               "mint": "MINTAAA", "multiple": 10.2, "threshold": 10.0,
               "chain": "solana", "card_mcap": 10000.0,
               "mcap_now": 102000.0, "safety_score": 72.0,
               "source_event": "launch.early", "minutes_since_card": 12.5}
        event_bus.validate_event(dict(evt))  # raises on violation

    def test_milestone_missing_required_rejected(self):
        with self.assertRaises(Exception):
            event_bus.validate_event({"event": "launch.milestone",
                                      "symbol": "TKN"})


class TelegramTests(unittest.TestCase):
    def _evt(self):
        return {"event": "launch.milestone", "symbol": "TKN",
                "mint": "MINTAAA", "multiple": 112.4, "threshold": 100.0,
                "chain": "solana", "name": "Token",
                "card_mcap": 3900.0, "mcap_now": 438360.0,
                "safety_score": 72.0, "source_event": "launch.early",
                "minutes_since_card": 38.0, "url": "http://x"}

    def test_card_renders(self):
        text = tg_alerts.format_alert(self._evt())
        self.assertIn("🏆 MILESTONE", text)
        self.assertIn("112.4x from our call", text)
        self.assertIn("called @ mcap $3.9k → now $438.4k", text)
        self.assertIn("CA: MINTAAA", text)
        self.assertIn("safety score: 72.0/100", text)
        self.assertIn("Not a new entry signal", text)

    def test_copy_ca_markup(self):
        mk = tg_alerts.format_alert_markup(self._evt())
        self.assertIsNotNone(mk)
        btn = mk["inline_keyboard"][0][0]
        self.assertEqual(btn["copy_text"]["text"], "MINTAAA")

    def test_unknown_event_still_none(self):
        self.assertIsNone(tg_alerts.format_alert({"event": "nope.x"}))


class FilterTests(unittest.TestCase):
    def _evt(self, **kw):
        e = {"event": "launch.milestone", "symbol": "TKN", "mint": "M",
             "multiple": 10.0, "safety_score": 72.0}
        e.update(kw)
        return e

    def test_milestone_is_signal_event(self):
        self.assertIn("launch.milestone", tg_filters.SIGNAL_EVENTS)

    def test_minscore_overlay(self):
        flt = {"min_score": 80, "lane": "both", "quiet": None}
        self.assertFalse(tg_filters.event_passes(self._evt(), flt))
        flt2 = {"min_score": 70, "lane": "both", "quiet": None}
        self.assertTrue(tg_filters.event_passes(self._evt(), flt2))

    def test_quiet_summary(self):
        item = tg_filters.summarize_for_quiet(self._evt())
        self.assertEqual(item["kind"], "🏆 milestone")
        self.assertEqual(item["symbol"], "TKN")


if __name__ == "__main__":
    unittest.main()
