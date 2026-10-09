"""Tests for the trade-plan card line and personal alert filters.

Feature A: every signal card carries the standing trade plan
(stop / TP / trailing / max hold + absolute mcap levels).
Feature B: per-chat personal filters (min score, momentum lane,
quiet hours) — tighten-only overlays; they never weaken global gates.
"""

import os
import time
import unittest

from daemon.telegram import alerts as tg_alerts
from daemon.telegram import commands as tg_commands
from daemon.telegram import filters as tg_filters
from daemon.telegram import security as tg_security

CHAT, USER = 6883056760, 111222333

PLAN = {"stop_pct": 30.0, "tp_pct": 200.0, "max_hold_h": 4.0,
        "trail_trigger_pct": 50.0, "trail_drawdown_pct": 30.0}


def _early_event(**kw):
    e = {"event": "launch.early", "event_id": "e1",
         "ts": "2026-09-25T19:00:00+00:00", "tier": "early",
         "symbol": "TKN", "chain": "solana", "source": "pumpfun",
         "mint": "MINTAAA", "usd_market_cap": 1200000,
         "age_minutes": 1.0, "safety_score": 80.0, "url": "u"}
    e.update(kw)
    return e


# ----------------------------------------------------------------------
# Feature A — plan line rendering
# ----------------------------------------------------------------------
class PlanLineTests(unittest.TestCase):
    def test_plan_line_with_mcap(self):
        line = tg_alerts._plan_line({"plan": dict(PLAN),
                                     "usd_market_cap": 1200000})
        self.assertIn("📋 Plan: stop -30% · TP +200%", line)
        self.assertIn("trail locks at +50% (exit -30% off peak)", line)
        self.assertIn("max hold 4h", line)
        self.assertIn("↳ @ mcap $1.20M: stop $840.0k · TP $3.60M", line)

    def test_plan_line_zero_mcap(self):
        line = tg_alerts._plan_line({"plan": dict(PLAN),
                                     "usd_market_cap": 0})
        self.assertIn("📋 Plan:", line)
        self.assertIn("levels set at first price", line)
        self.assertNotIn("↳ @ mcap", line)

    def test_plan_line_missing_plan_returns_none(self):
        self.assertIsNone(tg_alerts._plan_line({}))
        self.assertIsNone(tg_alerts._plan_line({"plan": None}))

    def test_plan_line_malformed_plan_returns_none(self):
        self.assertIsNone(tg_alerts._plan_line({"plan": {"tp_pct": 200}}))
        self.assertIsNone(tg_alerts._plan_line({"plan": "nonsense"}))

    def test_plan_line_never_raises_on_junk(self):
        line = tg_alerts._plan_line({"plan": dict(PLAN),
                                     "usd_market_cap": "junk"})
        self.assertIn("levels set at first price", line)

    def test_trade_plan_reads_degen_knobs(self):
        degen = type("D", (), {"stop_loss_pct": 25.0,
                               "take_profit_pct": 150.0,
                               "max_hold_hours": 2.0,
                               "trail_trigger_pct": 40.0,
                               "trail_drawdown_pct": 20.0,
                               "leg_a_trail_trigger_pct": 25.0,
                               "leg_a_trail_drawdown_pct": 12.0})()
        from daemon.launch_scanner import LaunchScanner
        plan = LaunchScanner._trade_plan(degen)
        self.assertEqual(plan, {"stop_pct": 25.0, "tp_pct": 150.0,
                                "max_hold_h": 2.0, "trail_trigger_pct": 40.0,
                                "trail_drawdown_pct": 20.0,
                                "leg_a_trail_trigger_pct": 25.0,
                                "leg_a_trail_drawdown_pct": 12.0,
                                "two_tier": True, "tier_b_max_hold_h": 24.0})

    def test_trade_plan_falls_back_on_empty_degen(self):
        from daemon.launch_scanner import LaunchScanner
        plan = LaunchScanner._trade_plan(object())
        self.assertEqual(plan["stop_pct"], 30.0)
        self.assertEqual(plan["tp_pct"], 200.0)

    def test_cards_show_plan_and_warning_does_not(self):
        early = tg_alerts.format_alert(_early_event(plan=dict(PLAN)))
        self.assertIn("📋 Plan:", early)
        no_plan = tg_alerts.format_alert(_early_event())
        self.assertNotIn("📋 Plan:", no_plan)
        # Warning cards are not trade signals: no plan line, ever.
        warn = {"event": "degen.warning", "door": "launch", "symbol": "APE",
                "chain": "solana", "mint": "M", "usd_market_cap": 50000,
                "safety_score": 1, "dangers": ["insider cluster"],
                "plan": dict(PLAN)}
        self.assertNotIn("📋 Plan:", tg_alerts.format_alert(warn))

    def test_digest_carries_single_plan_line(self):
        e = {"event": "launch.early_digest", "count": 2,
             "plan": dict(PLAN),
             "items": [{"symbol": "A", "mint": "MA", "tier": "early",
                        "safety_score": 80},
                       {"symbol": "B", "mint": "MB", "tier": "early",
                        "safety_score": 70}]}
        text = tg_alerts.format_alert(e)
        self.assertEqual(text.count("📋 Plan:"), 1)


# ----------------------------------------------------------------------
# Feature B — filter logic
# ----------------------------------------------------------------------
class FilterLogicTests(unittest.TestCase):
    def test_defaults(self):
        flt = tg_filters.get_filters({}, CHAT)
        self.assertIsNone(flt["min_score"])
        self.assertEqual(flt["lane"], "both")
        self.assertIsNone(flt["quiet"])

    def test_minscore_pass_fail(self):
        flt = {"min_score": 50.0, "lane": "both", "quiet": None}
        self.assertTrue(tg_filters.event_passes(_early_event(), flt))
        self.assertFalse(tg_filters.event_passes(
            _early_event(safety_score=30.0), flt))
        # unknown score fails closed when a bar is set
        self.assertFalse(tg_filters.event_passes(
            _early_event(safety_score=None), flt))
        # no bar -> everything passes
        self.assertTrue(tg_filters.event_passes(
            _early_event(safety_score=1.0),
            {"min_score": None, "lane": "both", "quiet": None}))

    def test_lane_filter_only_touches_momentum(self):
        lowcap = {"event": "momentum.alert", "lane": "lowcap",
                  "safety_score": 80}
        est = {"event": "momentum.alert", "lane": "established",
               "safety_score": 80}
        flt = {"min_score": None, "lane": "lowcap", "quiet": None}
        self.assertTrue(tg_filters.event_passes(lowcap, flt))
        self.assertFalse(tg_filters.event_passes(est, flt))
        flt["lane"] = "both"
        self.assertTrue(tg_filters.event_passes(est, flt))
        # launch cards ignore the lane filter
        flt["lane"] = "established"
        self.assertTrue(tg_filters.event_passes(_early_event(), flt))

    def test_digest_items_filtered_in_place(self):
        e = {"event": "launch.early_digest", "count": 3,
             "items": [{"symbol": "A", "safety_score": 90},
                       {"symbol": "B", "safety_score": 20},
                       {"symbol": "C", "safety_score": 60}]}
        flt = {"min_score": 50.0, "lane": "both", "quiet": None}
        self.assertTrue(tg_filters.event_passes(e, flt))
        self.assertEqual([i["symbol"] for i in e["items"]], ["A", "C"])
        self.assertEqual(e["count"], 2)
        e2 = {"event": "launch.early_digest", "count": 1,
              "items": [{"symbol": "Z", "safety_score": 5}]}
        self.assertFalse(tg_filters.event_passes(e2, flt))

    def test_in_quiet_wrap_midnight(self):
        flt = {"quiet": {"start": 23, "end": 7}}
        for h in (23, 0, 3, 6):
            self.assertTrue(tg_filters.in_quiet(flt, hour=h), h)
        for h in (7, 12, 22):
            self.assertFalse(tg_filters.in_quiet(flt, hour=h), h)

    def test_in_quiet_daytime_range(self):
        flt = {"quiet": {"start": 9, "end": 17}}
        self.assertTrue(tg_filters.in_quiet(flt, hour=9))
        self.assertTrue(tg_filters.in_quiet(flt, hour=16))
        self.assertFalse(tg_filters.in_quiet(flt, hour=17))
        self.assertFalse(tg_filters.in_quiet(flt, hour=8))

    def test_in_quiet_no_filter(self):
        self.assertFalse(tg_filters.in_quiet({"quiet": None}, hour=3))

    def test_parse_quiet_range(self):
        self.assertEqual(tg_filters.parse_quiet_range("23-7"),
                         {"start": 23, "end": 7})
        self.assertEqual(tg_filters.parse_quiet_range("9-17"),
                         {"start": 9, "end": 17})
        for bad in ("7-7", "abc", "25-3", "9", "9-17-2", ""):
            self.assertIsNone(tg_filters.parse_quiet_range(bad), bad)

    def test_quiet_queue_bounded(self):
        state = {}
        for i in range(55):
            tg_filters.queue_quiet(state, {"symbol": "S%d" % i})
        q = state["quiet_queue"]
        self.assertEqual(len(q), 50)
        self.assertEqual(q[0]["symbol"], "S5")  # oldest dropped

    def test_format_quiet_digest(self):
        items = [tg_filters.summarize_for_quiet(_early_event()),
                 tg_filters.summarize_for_quiet(
                     {"event": "launch.early_digest", "count": 3,
                      "symbol": "?", "mint": "", "safety_score": None})]
        text = tg_filters.format_quiet_digest(items)
        self.assertIn("🌙 QUIET-HOURS DIGEST — 2 signals held", text)
        self.assertIn("CA: MINTAAA", text)
        self.assertIn("×3", text)

    def test_wat_hour_sane(self):
        h = tg_filters.wat_hour()
        self.assertIsInstance(h, int)
        self.assertTrue(0 <= h <= 23)

    def test_event_passes_never_raises(self):
        self.assertTrue(tg_filters.event_passes(None, {}))
        self.assertTrue(tg_filters.event_passes({}, {"min_score": "junk"}))


# ----------------------------------------------------------------------
# Feature B — command handlers
# ----------------------------------------------------------------------
class FakeClient:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))


class FakeTools:
    pass


def _ctx(state):
    client = FakeClient()
    sec = tg_security.SecurityContext(state)
    sec.state["paired_chat_id"] = CHAT
    sec.state["paired_user_id"] = USER
    ctx = tg_commands.HandlerContext(
        send=lambda c, t, m: client.send_message(c, t, reply_markup=m),
        security=sec, tools=FakeTools(),
        publish=lambda e, sev=None: None)
    return client, ctx


class FilterCommandTests(unittest.TestCase):
    def test_minscore_set_and_clear(self):
        state = {}
        client, ctx = _ctx(state)
        tg_commands.handle_message(ctx, CHAT, USER, 1, "/minscore 50")
        flt = tg_filters.get_filters(state, CHAT)
        self.assertEqual(flt["min_score"], 50.0)
        self.assertTrue(any("≥ 50/100" in t for _, t, _ in client.sent))
        tg_commands.handle_message(ctx, CHAT, USER, 2, "/minscore off")
        self.assertIsNone(tg_filters.get_filters(state, CHAT)["min_score"])

    def test_minscore_rejects_bad_input(self):
        state = {}
        client, ctx = _ctx(state)
        tg_commands.handle_message(ctx, CHAT, USER, 1, "/minscore abc")
        tg_commands.handle_message(ctx, CHAT, USER, 2, "/minscore 150")
        self.assertIsNone(tg_filters.get_filters(state, CHAT)["min_score"])
        self.assertTrue(any("Usage" in t for _, t, _ in client.sent))

    def test_lane_set_and_reject(self):
        state = {}
        client, ctx = _ctx(state)
        tg_commands.handle_message(ctx, CHAT, USER, 1, "/lane lowcap")
        self.assertEqual(tg_filters.get_filters(state, CHAT)["lane"], "lowcap")
        tg_commands.handle_message(ctx, CHAT, USER, 2, "/lane bogus")
        self.assertEqual(tg_filters.get_filters(state, CHAT)["lane"], "lowcap")
        self.assertTrue(any("Usage" in t for _, t, _ in client.sent))

    def test_quiet_set_and_clear(self):
        state = {}
        client, ctx = _ctx(state)
        tg_commands.handle_message(ctx, CHAT, USER, 1, "/quiet 23-7")
        self.assertEqual(tg_filters.get_filters(state, CHAT)["quiet"],
                         {"start": 23, "end": 7})
        tg_commands.handle_message(ctx, CHAT, USER, 2, "/quiet 7-7")
        self.assertEqual(tg_filters.get_filters(state, CHAT)["quiet"],
                         {"start": 23, "end": 7})  # unchanged on bad input
        tg_commands.handle_message(ctx, CHAT, USER, 3, "/quiet off")
        self.assertIsNone(tg_filters.get_filters(state, CHAT)["quiet"])

    def test_filters_command_shows_state(self):
        state = {}
        client, ctx = _ctx(state)
        tg_commands.handle_message(ctx, CHAT, USER, 1, "/minscore 30")
        tg_commands.handle_message(ctx, CHAT, USER, 2, "/lane lowcap")
        tg_commands.handle_message(ctx, CHAT, USER, 3, "/filters")
        text = client.sent[-1][1]
        self.assertIn("≥ 30/100", text)
        self.assertIn("lowcap", text)
        self.assertIn("quiet hours: off", text)

    def test_state_persists_in_live_dict(self):
        # The handler mutates ctx.security.state — the same dict the
        # daemon saves at cycle end. Prove no copy is involved.
        state = {}
        _, ctx = _ctx(state)
        tg_commands.handle_message(ctx, CHAT, USER, 1, "/minscore 42")
        self.assertEqual(state["personal_filters"][str(CHAT)]["min_score"],
                         42.0)


# ----------------------------------------------------------------------
# Feature B — fan-out gating + quiet queue
# ----------------------------------------------------------------------
class FanOutFilterTests(unittest.TestCase):
    def _daemon(self, client):
        from daemon.telegram import TelegramDaemon
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        self.addCleanup(lambda: os.environ.pop("TELEGRAM_BOT_TOKEN", None))
        return TelegramDaemon(client_factory=lambda t: client,
                              tools=FakeTools())

    def _run(self, daemon, state, journal):
        import daemon.telegram as tg_mod
        real = (tg_mod.get_store, tg_mod.get_daemon_state,
                tg_mod.save_daemon_state, tg_mod.event_bus.configure,
                tg_mod.event_bus.poll)
        tg_mod.get_store = lambda: object()
        tg_mod.get_daemon_state = lambda s, name: state
        tg_mod.save_daemon_state = lambda s, name, st: state.update(st)
        tg_mod.event_bus.configure = lambda store=None: None
        tg_mod.event_bus.poll = lambda since=None, limit=100: list(journal)
        try:
            daemon.run_once()
        finally:
            (tg_mod.get_store, tg_mod.get_daemon_state,
             tg_mod.save_daemon_state, tg_mod.event_bus.configure,
             tg_mod.event_bus.poll) = real

    def _signal_event(self, eid="q1", score=80.0):
        return _early_event(event_id=eid, safety_score=score)

    def test_minscore_blocks_low_score_card(self):
        client = FakeClient()
        daemon = self._daemon(client)
        state = {"paired_chat_id": CHAT, "paired_user_id": USER,
                 "personal_filters": {str(CHAT): {"min_score": 50.0,
                                                  "lane": "both",
                                                  "quiet": None}}}
        self._run(daemon, state, [self._signal_event(score=20.0)])
        self.assertEqual(client.sent, [])  # nothing sent...
        # ...but the event is consumed (marked seen, cursor advanced)
        self.assertIn("q1", state["alerted_event_ids"])
        self.assertEqual(state["last_event_ts"],
                         "2026-09-25T19:00:00+00:00")

    def test_quiet_hours_queue_and_flush(self):
        client = FakeClient()
        daemon = self._daemon(client)
        hour = tg_filters.wat_hour()
        state = {"paired_chat_id": CHAT, "paired_user_id": USER,
                 "personal_filters": {str(CHAT): {
                     "min_score": None, "lane": "both",
                     "quiet": {"start": hour, "end": (hour + 2) % 24}}}}
        self._run(daemon, state, [self._signal_event()])
        self.assertEqual(client.sent, [])  # held, not sent
        self.assertEqual(len(state["quiet_queue"]), 1)
        # quiet ends -> one digest, queue cleared
        state["personal_filters"][str(CHAT)]["quiet"] = None
        self._run(daemon, state, [])
        self.assertEqual(len(client.sent), 1)
        self.assertIn("QUIET-HOURS DIGEST", client.sent[0][1])
        self.assertIn("CA: MINTAAA", client.sent[0][1])
        self.assertEqual(state["quiet_queue"], [])

    def test_safety_events_ignore_quiet(self):
        client = FakeClient()
        daemon = self._daemon(client)
        hour = tg_filters.wat_hour()
        state = {"paired_chat_id": CHAT, "paired_user_id": USER,
                 "personal_filters": {str(CHAT): {
                     "min_score": None, "lane": "both",
                     "quiet": {"start": hour, "end": (hour + 2) % 24}}}}
        kill = {"event": "kill_switch.activated", "event_id": "k1",
                "ts": "2026-09-25T19:00:00+00:00",
                "source": "test", "reason": "test"}
        self._run(daemon, state, [kill])
        self.assertTrue(any("KILL SWITCH" in t for _, t, _ in client.sent))

    def test_failed_digest_send_keeps_queue(self):
        from daemon.telegram.client import TelegramError
        client = FakeClient()
        daemon = self._daemon(client)
        state = {"paired_chat_id": CHAT, "paired_user_id": USER,
                 "quiet_queue": [{"kind": "⚡ early", "symbol": "TKN",
                                  "mint": "M", "score": 80, "ts": "t"}]}
        real_send = client.send_message

        def boom(chat_id, text, reply_markup=None, **kw):
            raise TelegramError("down")
        client.send_message = boom
        TelegramDaemon = type(daemon)
        TelegramDaemon._flush_quiet(daemon, state, CHAT)
        client.send_message = real_send
        self.assertEqual(len(state["quiet_queue"]), 1)  # kept for retry


if __name__ == "__main__":
    unittest.main()
