"""Tests for the confirmed-runner alert lane + runners recap (2026-09-28).

Build 1: chart-confirmed runners (multi-poll proof) drop the CA even when
no safety report exists — with a prominent UNVETTED label. Hard fails
(honeypot / can't-sell / high sell tax / insider clusters) still block.
Unconfirmed coins stay silent.

Build 2: twice-daily 🏆 runners recap — only coins that actually ran
(peak >= recap_min_mult from card-time anchor) since the last recap.
Informational only, never trades.
"""

import time
import unittest
import unittest.mock
from types import SimpleNamespace

from agent.events import bus as event_bus
from daemon.launch_scanner import LaunchScanner, recap
from daemon.telegram import alerts as tg_alerts


def _degen(**kw):
    d = {"confirm_unvetted_alerts": True, "recap_enabled": True,
         "recap_times": ["08:00", "20:00"], "recap_min_mult": 2.0,
         "recap_max_items": 20}
    d.update(kw)
    return SimpleNamespace(**d)


def _rec(mint, peak, **kw):
    r = {"symbol": "TKN" + mint[-2:], "chain": "solana",
         "peak_mult": peak, "safety_score": 72.0,
         "url": "http://x/" + mint}
    r.update(kw)
    return r


# --- Build 1: unvetted decision ----------------------------------------

class UnvettedDecisionTests(unittest.TestCase):
    def test_unknown_report_plus_knob_allows(self):
        degen = _degen()
        report = {"unknown": True}
        self.assertTrue(
            LaunchScanner._unvetted_confirm_allowed(degen, report))

    def test_knob_off_blocks(self):
        degen = _degen(confirm_unvetted_alerts=False)
        self.assertFalse(LaunchScanner._unvetted_confirm_allowed(
            degen, {"unknown": True}))

    def test_hard_fail_never_allowed(self):
        # Known bad report (honeypot etc.) must NOT take the unvetted path.
        degen = _degen()
        report = {"unknown": False, "passed": False,
                  "fails": ["honeypot"]}
        self.assertFalse(
            LaunchScanner._unvetted_confirm_allowed(degen, report))

    def test_known_report_not_unvetted(self):
        degen = _degen()
        report = {"unknown": False, "passed": True, "score": 80.0}
        self.assertFalse(
            LaunchScanner._unvetted_confirm_allowed(degen, report))

    def test_none_report_safe(self):
        degen = _degen()
        self.assertFalse(
            LaunchScanner._unvetted_confirm_allowed(degen, None))


class UnvettedCardTests(unittest.TestCase):
    def _card(self, **kw):
        e = {"event": "launch.detected", "symbol": "TKN", "tier": "runner",
             "chain": "solana", "mint": "MINTXYZ", "safety": None,
             "safety_score": None, "unvetted": False,
             "usd_market_cap": 300000.0, "age_minutes": 45.0,
             "evidence": {"gain_mult": 3.2, "rising_polls": 4,
                          "vol_m5": 50000, "liquidity": 40000,
                          "buys_m5": 120, "sells_m5": 40}}
        e.update(kw)
        return e

    def test_unvetted_card_renders_label(self):
        text = tg_alerts._launch(self._card(unvetted=True))
        self.assertIn("UNVETTED", text)
        self.assertIn("MINTXYZ", text)  # CA still drops

    def test_vetted_card_has_no_unvetted_label(self):
        text = tg_alerts._launch(self._card(
            unvetted=False, safety="score 80/100", safety_score=80.0))
        self.assertNotIn("UNVETTED", text)
        self.assertIn("80/100", text)

    def test_unvetted_event_validates(self):
        e = self._card(unvetted=True)
        event_bus.validate_event(dict(e))  # raises on schema violation


# --- Build 2: recap ------------------------------------------------------

def _wat_ts(hour, minute, day_off=0):
    # Fixed WAT (UTC+1) timestamp for a slot time on 2026-09-28 +/- days.
    import calendar
    base = calendar.timegm((2026, 9, 28, 0, 0, 0))  # 2026-09-28 00:00 UTC
    return base + day_off * 86400 + (hour - 1) * 3600 + minute * 60


class RecapDueTests(unittest.TestCase):
    def test_due_after_slot(self):
        state = {}
        self.assertTrue(
            recap.due(state, _degen(), _wat_ts(9, 0)))  # past 08:00

    def test_not_due_before_first_slot(self):
        state = {}
        self.assertFalse(
            recap.due(state, _degen(), _wat_ts(7, 0)))  # before 08:00

    def test_not_due_when_already_sent(self):
        state = {"recap_last_slot": "2026-09-28 08:00"}
        self.assertFalse(
            recap.due(state, _degen(), _wat_ts(9, 0)))

    def test_due_for_next_slot(self):
        state = {"recap_last_slot": "2026-09-28 08:00"}
        self.assertTrue(
            recap.due(state, _degen(), _wat_ts(21, 0)))  # past 20:00

    def test_disabled_never_due(self):
        state = {}
        self.assertFalse(
            recap.due(state, _degen(recap_enabled=False), _wat_ts(21, 0)))

    def test_bad_times_fall_back(self):
        state = {}
        # garbage times -> default ["08:00","20:00"] -> due at 09:00
        self.assertTrue(
            recap.due(state, _degen(recap_times=["nope"]), _wat_ts(9, 0)))


class RecapCollectTests(unittest.TestCase):
    def test_only_runners_collected_sorted(self):
        state = {"milestone_watch": {
            "A": _rec("A", 1.5), "B": _rec("B", 12.0),
            "C": _rec("C", 3.0), "D": _rec("D", 0.5)}}
        shown, overflow = recap.collect(state, _degen())
        self.assertEqual([m for m, _ in shown], ["B", "C"])
        self.assertEqual(overflow, 0)
        # not marked until mark_sent — a second collect finds them again
        shown2, _ = recap.collect(state, _degen())
        self.assertEqual(len(shown2), 2)
        recap.mark_sent(shown)
        shown3, _ = recap.collect(state, _degen())
        self.assertEqual(shown3, [])

    def test_max_items(self):
        state = {"milestone_watch": {
            "M%d" % i: _rec("M%d" % i, 2.0 + i) for i in range(30)}}
        shown, overflow = recap.collect(state, _degen(recap_max_items=5))
        self.assertEqual(len(shown), 5)
        self.assertEqual(shown[0][1]["peak_mult"], 31.0)
        self.assertEqual(overflow, 25)
        # overflow stays unmarked and leads the next recap
        recap.mark_sent(shown)
        shown2, overflow2 = recap.collect(state, _degen(recap_max_items=5))
        self.assertEqual(len(shown2), 5)
        self.assertEqual(overflow2, 20)

    def test_min_mult_floor(self):
        state = {"milestone_watch": {"A": _rec("A", 1.9)}}
        shown, _ = recap.collect(state, _degen(recap_min_mult=2.0))
        self.assertEqual(shown, [])
        shown2, _ = recap.collect(state, _degen(recap_min_mult=1.5))
        self.assertEqual(len(shown2), 1)


class RecapEventTests(unittest.TestCase):
    def test_event_validates_and_renders(self):
        runners = [("MINTB", _rec("MINTB", 12.0)),
                   ("MINTC", _rec("MINTC", 3.0, safety_score=None))]
        e = recap.build_event(runners, "2026-09-28 20:00")
        event_bus.validate_event(dict(e))  # raises on schema violation
        text = tg_alerts._runners_recap(e)
        self.assertIn("RUNNERS RECAP", text)
        self.assertIn("x12.0", text)
        self.assertIn("MINTB", text)
        self.assertIn("unvetted", text)  # no-score coin labeled
        self.assertIn("72/100", text)

    def test_maybe_send_publishes_once(self):
        state = {"milestone_watch": {"B": _rec("B", 5.0)}}
        degen = _degen()
        import agent.events as events_pkg
        with unittest.mock.patch.object(events_pkg, "publish") as pub:
            e1 = recap.maybe_send(SimpleNamespace(degen=degen), state,
                                  _wat_ts(21, 0))
            self.assertIsNotNone(e1)
            self.assertEqual(e1["count"], 1)
            pub.assert_called_once()
            # second call same slot: silent
            e2 = recap.maybe_send(SimpleNamespace(degen=degen), state,
                                  _wat_ts(21, 30))
            self.assertIsNone(e2)
            pub.assert_called_once()

    def test_empty_slot_skips_card_but_marks_sent(self):
        state = {"milestone_watch": {"A": _rec("A", 1.2)}}
        degen = _degen()
        import agent.events as events_pkg
        with unittest.mock.patch.object(events_pkg, "publish") as pub:
            e = recap.maybe_send(SimpleNamespace(degen=degen), state,
                                 _wat_ts(21, 0))
            self.assertIsNone(e)
            pub.assert_not_called()
            self.assertEqual(state.get("recap_last_slot"),
                             "2026-09-28 20:00")


class ConfigTests(unittest.TestCase):
    def test_degen_config_has_new_knobs(self):
        from config.config import DegenConfig
        d = DegenConfig()
        self.assertTrue(d.confirm_unvetted_alerts)
        self.assertTrue(d.recap_enabled)
        self.assertEqual(list(d.recap_times), ["08:00", "20:00"])
        self.assertEqual(d.recap_min_mult, 2.0)
        self.assertEqual(d.recap_max_items, 20)
        self.assertEqual(d.milestone_max_tracked, 1000)


class WatchCapTests(unittest.TestCase):
    def _sig(self, mint):
        return {"event": "launch.early", "mint": mint, "symbol": "T" + mint,
                "chain": "solana", "name": "Token", "url": "http://x",
                "usd_market_cap": 10000.0, "safety_score": 72.0}

    def test_knob_raises_watch_cap(self):
        from daemon.launch_scanner import milestones
        state = {}
        degen = _degen(milestone_max_tracked=1000)
        for i in range(150):
            milestones.track_signal(state, degen, self._sig("M%04d" % i))
        self.assertEqual(len(state["milestone_watch"]), 150)

    def test_default_cap_still_120_without_knob(self):
        from daemon.launch_scanner import milestones
        state = {}
        degen = SimpleNamespace()  # no knob -> module default
        for i in range(150):
            milestones.track_signal(state, degen, self._sig("M%04d" % i))
        self.assertEqual(len(state["milestone_watch"]), 120)


class RecapOverflowTests(unittest.TestCase):
    def _state(self, n):
        return {"milestone_watch": {
            "M%03d" % i: _rec("M%03d" % i, 2.0 + i) for i in range(n)}}

    def test_overflow_in_event_and_card(self):
        state = self._state(25)
        shown, overflow = recap.collect(state, _degen(recap_max_items=20))
        self.assertEqual(len(shown), 20)
        self.assertEqual(overflow, 5)
        e = recap.build_event(shown, "2026-09-28 20:00", overflow)
        event_bus.validate_event(dict(e))  # raises on schema violation
        self.assertEqual(e["overflow"], 5)
        text = tg_alerts._runners_recap(e)
        self.assertIn("…and 5 more runners", text)
        self.assertIn("lead the next recap", text)

    def test_no_overflow_no_line(self):
        state = self._state(3)
        shown, overflow = recap.collect(state, _degen())
        e = recap.build_event(shown, "2026-09-28 20:00", overflow)
        text = tg_alerts._runners_recap(e)
        self.assertNotIn("more runners", text)

    def test_maybe_send_marks_only_after_publish(self):
        from agent import events as events_pkg
        cfg = SimpleNamespace(degen=_degen())
        state = self._state(3)
        with unittest.mock.patch.object(
                events_pkg, "publish",
                side_effect=RuntimeError("bus down")):
            self.assertIsNone(
                recap.maybe_send(cfg, state, _wat_ts(21, 0)))
        # publish failed: slot NOT marked, runners NOT marked -> retry
        self.assertNotIn("recap_last_slot", state)
        watch = state["milestone_watch"]
        self.assertFalse(any(r.get("recap_sent") for r in watch.values()))
        self.assertTrue(recap.due(state, cfg.degen, _wat_ts(21, 30)))

    def test_maybe_send_happy_path(self):
        from agent import events as events_pkg
        cfg = SimpleNamespace(degen=_degen())
        state = self._state(3)
        seen = []
        with unittest.mock.patch.object(
                events_pkg, "publish",
                side_effect=lambda e: seen.append(e)):
            e = recap.maybe_send(cfg, state, _wat_ts(21, 0))
        self.assertIsNotNone(e)
        self.assertEqual(len(seen), 1)
        self.assertEqual(state["recap_last_slot"], "2026-09-28 20:00")
        watch = state["milestone_watch"]
        self.assertTrue(all(r.get("recap_sent") for r in watch.values()))
        self.assertFalse(recap.due(state, cfg.degen, _wat_ts(21, 30)))


if __name__ == "__main__":
    unittest.main()
