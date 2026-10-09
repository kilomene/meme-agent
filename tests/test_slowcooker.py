"""Tests for the slow-cooker late-runner watch (🐢 cards).

Covers:
  * trigger logic: no fire inside the 45-min initial window, fire on a
    late 2x (past 45 min, within 3h), source allowlist (never
    launch.detected / launch.second_wave), window expiry, below-trigger
    multiples;
  * dedupe with milestones: milestone skips its 2x celebration when the
    late runner fired first (5x/10x+ still fire), and vice versa the
    slow cooker skips when the milestone 2x already celebrated;
  * safety re-check is fail-closed (failing coin -> silent, no fired
    flag burned, may trigger again later);
  * paper entry labeled late_runner with the standard exit plan;
  * spam-cap path: queues into the digest like everything else, no
    position, no bypass;
  * digest-queued coins are in the pool (the e/acc shape);
  * disabled flag is a no-op;
  * the launch.late_runner event validates against the strict
    event-bus schema and renders as a Telegram card; personal filters
    treat it as a signal event.
"""

import time
import unittest
import unittest.mock
from types import SimpleNamespace

from agent.events import bus as event_bus
from daemon.launch_scanner import LaunchScanner
from daemon.launch_scanner import milestones
from daemon.launch_scanner import slowcooker
from daemon.telegram import alerts as tg_alerts
from daemon.telegram import filters as tg_filters


def _degen(**kw):
    d = {"slow_cooker_enabled": True,
         "slow_cooker_window_hours": 3.0,
         "slow_cooker_trigger_pct": 100.0,
         "early_min_score": 1.0,
         "stop_loss_pct": 30.0, "take_profit_pct": 200.0,
         "max_hold_hours": 4.0, "trail_trigger_pct": 50.0,
         "trail_drawdown_pct": 30.0,
         "entry_sol": 0.05, "max_positions": 5, "auto_enter": True,
         "daily_max_loss_sol": 0.15,
         "early_alert_max_per_window": 4,
         "early_alert_window_seconds": 3600.0}
    d.update(kw)
    return SimpleNamespace(**d)


def _track(state, degen, mint="MINTL", anchor=100000.0,
           source="launch.early_digest", card_age_h=0.0, now=None,
           symbol="LATE"):
    """Seed milestone tracking (the slow-cooker pool) with a known age."""
    now = time.time() if now is None else now
    milestones.track_signal(
        state, degen,
        {"event": source, "mint": mint, "symbol": symbol,
         "chain": "solana", "name": "Late Token", "url": "http://x",
         "usd_market_cap": anchor, "safety_score": 55.0})
    rec = state["milestone_watch"][mint]
    rec["card_ts"] = now - card_age_h * 3600.0
    return rec


def _poll(state, degen, mcap, now, mint="MINTL", cycle=4):
    """Run slowcooker.poll with a stubbed batch-stats fetch."""
    state.pop("slowcooker_stats", None)  # force a fresh (mocked) read
    stats = {mint: {"usd_market_cap": mcap}} if mcap is not None else {}
    with unittest.mock.patch.object(slowcooker, "_batch_stats",
                                    return_value=stats):
        return slowcooker.poll(state, degen, now, cycle)


def _adapter(price=0.001):
    captured = {"reqs": []}

    def _submit(req):
        captured["reqs"].append(req)
        return SimpleNamespace(ticket=4242, price=price)

    ad = SimpleNamespace()
    ad._price_sol_quiet = lambda symbol: price
    ad.submit_order = _submit
    ad.close_position = lambda ticket: price
    ad.captured = captured
    return ad


def _scanner_with(safety_ok=True, score=55.0, early_allowed=True):
    sc = LaunchScanner.__new__(LaunchScanner)
    report = {"passed": True, "unknown": False, "score": float(score),
              "fails": [], "flags": []}
    sc._confirm_safety_ok = (
        lambda degen, cand: ((True, "ok", report) if safety_ok
                             else (False, "safety-fail: honeypot", report)))
    sc._early_allowed = (lambda degen, state, now: early_allowed)
    return sc


def _handle(state, degen, payload, safety_ok=True, early_allowed=True,
            score=55.0):
    sc = _scanner_with(safety_ok=safety_ok, score=score,
                       early_allowed=early_allowed)
    cfg = SimpleNamespace(degen=degen)
    state["degen_positions"] = {}
    ad = _adapter()
    published = []
    with unittest.mock.patch("agent.events.publish",
                             side_effect=published.append):
        ok = slowcooker.handle_late(sc, cfg, ad, state, payload)
    return ok, published, ad


class TriggerTests(unittest.TestCase):
    def test_no_trigger_inside_initial_window(self):
        state, degen = {}, _degen()
        now = time.time()
        _track(state, degen, card_age_h=0.0, now=now)
        # 3x just 10 minutes after the card: the main lanes' window —
        # the slow cooker stays out of it.
        out = _poll(state, degen, 300000.0, now + 600)
        self.assertEqual(out, [])

    def test_trigger_fires_late(self):
        state, degen = {}, _degen()
        now = time.time()
        _track(state, degen, card_age_h=2.0, now=now,
               source="launch.early_digest")
        out = _poll(state, degen, 250000.0, now)
        self.assertEqual(len(out), 1)
        p = out[0]
        self.assertEqual(p["mint"], "MINTL")
        self.assertEqual(p["symbol"], "LATE")
        self.assertAlmostEqual(p["multiple_now"], 2.5)
        self.assertAlmostEqual(p["gain_pct"], 150.0)
        self.assertAlmostEqual(p["hours_since_seen"], 2.0)
        self.assertEqual(p["source"], "launch.early_digest")

    def test_confirmed_sources_never_qualify(self):
        for source in ("launch.detected", "launch.second_wave",
                       "momentum.alert"):
            state, degen = {}, _degen()
            now = time.time()
            _track(state, degen, card_age_h=2.0, now=now, source=source)
            out = _poll(state, degen, 300000.0, now)
            self.assertEqual(out, [], source)

    def test_instant_and_lowcap_sources_qualify(self):
        for source in ("launch.early", "launch.lowcap_runner"):
            state, degen = {}, _degen()
            now = time.time()
            _track(state, degen, card_age_h=2.0, now=now, source=source)
            out = _poll(state, degen, 250000.0, now)
            self.assertEqual(len(out), 1, source)

    def test_window_expiry(self):
        state, degen = {}, _degen()
        now = time.time()
        _track(state, degen, card_age_h=4.0, now=now)  # window is 3h
        out = _poll(state, degen, 300000.0, now)
        self.assertEqual(out, [])

    def test_below_trigger_no_fire(self):
        state, degen = {}, _degen()
        now = time.time()
        _track(state, degen, card_age_h=2.0, now=now)
        out = _poll(state, degen, 150000.0, now)  # 1.5x < 2.0
        self.assertEqual(out, [])

    def test_custom_trigger_pct(self):
        state, degen = {}, _degen(slow_cooker_trigger_pct=50.0)  # 1.5x
        now = time.time()
        _track(state, degen, card_age_h=2.0, now=now)
        out = _poll(state, degen, 160000.0, now)
        self.assertEqual(len(out), 1)

    def test_poll_throttled_to_milestone_cadence(self):
        state, degen = {}, _degen()
        now = time.time()
        _track(state, degen, card_age_h=2.0, now=now)
        self.assertEqual(_poll(state, degen, 250000.0, now, cycle=1), [])
        self.assertEqual(len(_poll(state, degen, 250000.0, now,
                                   cycle=4)), 1)

    def test_disabled_is_noop(self):
        state, degen = {}, _degen(slow_cooker_enabled=False)
        now = time.time()
        _track(state, degen, card_age_h=2.0, now=now)
        out = slowcooker.poll(state, degen, now, 4)
        self.assertEqual(out, [])
        self.assertNotIn("slowcooker_fired", state)

    def test_confirmed_later_retires_from_pool(self):
        # A coin carded unconfirmed, then CONFIRMED later: the record
        # keeps its first anchor but is upgraded so the slow cooker
        # retires it — confirmed runners are the other lanes' job.
        state, degen = {}, _degen()
        now = time.time()
        _track(state, degen, card_age_h=2.0, now=now,
               source="launch.early")
        milestones.track_signal(
            state, degen,
            {"event": "launch.detected", "mint": "MINTL",
             "symbol": "LATE", "chain": "solana",
             "usd_market_cap": 250000.0, "safety_score": 55.0})
        rec = state["milestone_watch"]["MINTL"]
        self.assertTrue(rec.get("confirmed_runner"))
        self.assertEqual(rec["source"], "launch.early")  # anchor kept
        out = _poll(state, degen, 250000.0, now)  # 2.5x — would fire
        self.assertEqual(out, [])

    def test_digest_queued_coins_are_in_the_pool(self):
        # The e/acc shape: quality-passed, digest-queued at birth, quiet
        # for hours, then running. The scanner tracks digest items in
        # the milestone watch, so the pool already contains them.
        state, degen = {}, _degen()
        now = time.time()
        milestones.track_signal(
            state, degen,
            {"event": "launch.early_digest", "mint": "EACC",
             "symbol": "EACC", "chain": "solana",
             "usd_market_cap": 20000.0, "safety_score": 3.0})
        self.assertIn("EACC", milestones.tracked(state))
        state["milestone_watch"]["EACC"]["card_ts"] = now - 2.5 * 3600
        out = _poll(state, degen, 600000.0, now, mint="EACC")  # 30x
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "launch.early_digest")
        self.assertAlmostEqual(out[0]["multiple_now"], 30.0)

    def test_fired_flags_pruned_with_watch(self):
        state, degen = {}, _degen()
        now = time.time()
        _track(state, degen, card_age_h=2.0, now=now)
        state["slowcooker_fired"] = {"MINTL": now, "GONE": now - 10}
        _poll(state, degen, 250000.0, now)
        self.assertNotIn("GONE", state["slowcooker_fired"])


class DedupeTests(unittest.TestCase):
    def test_milestone_skips_2x_after_late_runner(self):
        state, degen = {}, _degen()
        now = time.time()
        _track(state, degen, card_age_h=2.0, now=now)
        out = _poll(state, degen, 250000.0, now)
        self.assertEqual(len(out), 1)
        ok, published, _ = _handle(state, degen, out[0], safety_ok=True)
        self.assertTrue(ok)
        self.assertTrue(
            state["milestone_watch"]["MINTL"].get("late_runner_fired"))
        # Milestones: the 2x celebration stays silent...
        with unittest.mock.patch.object(
                milestones, "_batch_stats",
                return_value={"MINTL": {"usd_market_cap": 250000.0}}):
            mevents = milestones.poll(state, degen, now, 4)
        self.assertEqual([e for e in mevents
                          if e.get("threshold") == 2.0], [])
        # ...but higher milestones (5x/10x+) still fire.
        with unittest.mock.patch.object(
                milestones, "_batch_stats",
                return_value={"MINTL": {"usd_market_cap": 600000.0}}):
            mevents = milestones.poll(state, degen, now + 60, 8)
        self.assertTrue(any(e.get("threshold") == 5.0
                            for e in mevents))

    def test_slowcooker_skips_when_milestone_celebrated_first(self):
        state, degen = {}, _degen()
        now = time.time()
        _track(state, degen, card_age_h=2.0, now=now)
        # The milestone 2x celebrated in an EARLIER cycle.
        with unittest.mock.patch.object(
                milestones, "_batch_stats",
                return_value={"MINTL": {"usd_market_cap": 250000.0}}):
            mevents = milestones.poll(state, degen, now - 300, 4)
        self.assertTrue(any(e.get("threshold") == 2.0
                            for e in mevents))
        # The slow cooker must not double-fire for the same 2x.
        out = _poll(state, degen, 260000.0, now)
        self.assertEqual(out, [])

    def test_safety_fail_leaves_milestone_2x_intact(self):
        # Safety re-check fails -> slow cooker holds silently AND the
        # milestone 2x celebration still fires as the consolation card.
        state, degen = {}, _degen()
        now = time.time()
        _track(state, degen, card_age_h=2.0, now=now)
        out = _poll(state, degen, 250000.0, now)
        ok, published, _ = _handle(state, degen, out[0],
                                   safety_ok=False)
        self.assertFalse(ok)
        self.assertEqual(published, [])
        with unittest.mock.patch.object(
                milestones, "_batch_stats",
                return_value={"MINTL": {"usd_market_cap": 250000.0}}):
            mevents = milestones.poll(state, degen, now, 4)
        self.assertTrue(any(e.get("threshold") == 2.0
                            for e in mevents))


class SafetyTests(unittest.TestCase):
    def _triggered(self, **kw):
        state, degen = {}, _degen()
        now = time.time()
        _track(state, degen, card_age_h=2.0, now=now)
        out = _poll(state, degen, 250000.0, now)
        self.assertEqual(len(out), 1)
        return state, degen, out[0]

    def test_safety_fail_holds_silently(self):
        state, degen, payload = self._triggered()
        ok, published, _ = _handle(state, degen, payload,
                                   safety_ok=False)
        self.assertFalse(ok)
        self.assertEqual(published, [])
        self.assertEqual(state["degen_positions"], {})
        # No fired flag burned — the coin may trigger again later.
        self.assertNotIn("MINTL", state.get("slowcooker_fired", {}))
        self.assertFalse(
            state["milestone_watch"]["MINTL"].get("late_runner_fired"))
        state.pop("slowcooker_stats", None)
        now = time.time()
        with unittest.mock.patch.object(
                slowcooker, "_batch_stats",
                return_value={"MINTL": {"usd_market_cap": 250000.0}}):
            out2 = slowcooker.poll(state, degen, now, 4)
        self.assertEqual(len(out2), 1)

    def test_safety_pass_emits_card_and_position(self):
        state, degen, payload = self._triggered()
        ok, published, ad = _handle(state, degen, payload,
                                    safety_ok=True, score=55.0)
        self.assertTrue(ok)
        self.assertEqual(len(published), 1)
        card = published[0]
        # strict event-bus schema: must validate
        event_bus.validate_event(dict(card))
        self.assertEqual(card["event"], "launch.late_runner")
        self.assertEqual(card["symbol"], "LATE")
        self.assertEqual(card["mint"], "MINTL")
        self.assertEqual(card["safety_score"], 55.0)
        self.assertIn("pass (score 55)", card["safety"])
        self.assertAlmostEqual(card["multiple_now"], 2.5)
        self.assertAlmostEqual(card["gain_pct"], 150.0)
        plan = card["plan"]
        self.assertEqual(plan["stop_pct"], 30.0)
        self.assertEqual(plan["tp_pct"], 200.0)
        self.assertEqual(plan["max_hold_h"], 4.0)
        self.assertIsNotNone(card["entry"])
        # paper position labeled late_runner, keyed separately
        pos = state["degen_positions"].get("late_runner:MINTL")
        self.assertIsNotNone(pos)
        self.assertEqual(pos["label"], "late_runner")
        self.assertEqual(pos["mint"], "MINTL")
        self.assertNotIn("MINTL", state["degen_positions"])  # no clash
        req = ad.captured["reqs"][0]
        self.assertEqual(req.signal_id, "late_runner:MINTL")
        self.assertEqual(req.idempotency_key, "late_runner:MINTL")
        self.assertEqual(req.comment, "degen-late-runner")
        # commit marks: dedupe + fired flag
        self.assertTrue(
            state["milestone_watch"]["MINTL"].get("late_runner_fired"))
        self.assertIn("MINTL", state["slowcooker_fired"])
        # never fires twice for the same coin
        state.pop("slowcooker_stats", None)
        with unittest.mock.patch.object(
                slowcooker, "_batch_stats",
                return_value={"MINTL": {"usd_market_cap": 250000.0}}):
            out2 = slowcooker.poll(state, degen, time.time(), 8)
        self.assertEqual(out2, [])

    def test_non_solana_is_alert_only(self):
        state, degen = {}, _degen()
        now = time.time()
        _track(state, degen, card_age_h=2.0, now=now)
        out = _poll(state, degen, 250000.0, now)
        out[0]["chain"] = "base"
        state["milestone_watch"]["MINTL"]["chain"] = "base"
        ok, published, _ = _handle(state, degen, out[0], safety_ok=True)
        self.assertTrue(ok)
        self.assertEqual(len(published), 1)
        self.assertEqual(state["degen_positions"], {})
        self.assertIn("alert only", published[0]["note"])


class SpamCapTests(unittest.TestCase):
    def test_cap_hit_queues_digest_no_position_no_bypass(self):
        state, degen = {}, _degen()
        now = time.time()
        _track(state, degen, card_age_h=2.0, now=now)
        out = _poll(state, degen, 250000.0, now)
        self.assertEqual(len(out), 1)
        ok, published, _ = _handle(state, degen, out[0],
                                   safety_ok=True, early_allowed=False)
        self.assertFalse(ok)            # no instant card
        self.assertEqual(published, [])
        self.assertEqual(state["degen_positions"], {})  # no position
        dq = state.get("early_digest") or []
        self.assertEqual(len(dq), 1)
        self.assertEqual(dq[0]["mint"], "MINTL")
        self.assertEqual(dq[0]["tier"], "late_runner")
        self.assertIn("late runner", dq[0]["reason"])
        self.assertIsNotNone(dq[0]["ts"])
        # committed: fired flag burned, milestone 2x suppressed
        self.assertIn("MINTL", state["slowcooker_fired"])
        self.assertTrue(
            state["milestone_watch"]["MINTL"].get("late_runner_fired"))

    def test_cap_counts_toward_window(self):
        # The real _early_allowed (not the stub): the late-runner card
        # consumes one of the 4 hourly instant slots.
        state, degen = {}, _degen()
        now = time.time()
        _track(state, degen, card_age_h=2.0, now=now)
        out = _poll(state, degen, 250000.0, now)
        sc = LaunchScanner.__new__(LaunchScanner)
        report = {"passed": True, "unknown": False, "score": 55.0,
                  "fails": [], "flags": []}
        sc._confirm_safety_ok = lambda degen, cand: (True, "ok", report)
        cfg = SimpleNamespace(degen=degen)
        state["degen_positions"] = {}
        with unittest.mock.patch("agent.events.publish"):
            ok = slowcooker.handle_late(sc, cfg, _adapter(), state,
                                        out[0])
        self.assertTrue(ok)
        self.assertEqual(state["early_window"]["count"], 1)


class CardTests(unittest.TestCase):
    def _card(self):
        return {
            "event": "launch.late_runner", "symbol": "LATE",
            "mint": "MINTL", "chain": "solana",
            "source": "launch.early_digest",
            "name": "Late Token", "url": "http://x",
            "usd_market_cap": 250000.0, "hours_since_seen": 2.0,
            "gain_pct": 150.0, "multiple_now": 2.5,
            "safety": "pass (score 55)", "safety_score": 55.0,
            "plan": {"stop_pct": 30.0, "tp_pct": 200.0,
                     "max_hold_h": 4.0, "trail_trigger_pct": 50.0,
                     "trail_drawdown_pct": 30.0},
            "entry": {"volume_sol": 0.05, "price_sol": 0.001,
                      "ticket": 4242, "stop_loss_pct": 30.0,
                      "take_profit_pct": 200.0},
            "note": "",
        }

    def test_card_renders(self):
        text = tg_alerts._launch_late_runner(self._card())
        self.assertIn("🐢 LATE RUNNER", text)
        self.assertIn("2.0h after first card", text)
        self.assertIn("+150%", text)
        self.assertIn("2.5x of card mcap", text)
        self.assertIn("55.0/100", text)
        self.assertIn("📋 Plan:", text)
        self.assertIn("stop $175.0k", text)  # absolute mcap levels
        self.assertIn("TP $750.0k", text)
        self.assertIn("[late_runner]", text)
        self.assertIn("CA: MINTL", text)

    def test_card_validates_and_formats(self):
        card = self._card()
        event_bus.validate_event(dict(card))
        text = tg_alerts.format_alert(card)
        self.assertIn("🐢 LATE RUNNER", text)
        # one-tap Copy CA button, like the other runner cards
        markup = tg_alerts.format_alert_markup(card)
        self.assertIn("MINTL", str(markup))

    def test_filters_treat_as_signal(self):
        self.assertIn("launch.late_runner", tg_filters.SIGNAL_EVENTS)
        flt = {"min_score": 50.0, "lane": "both", "quiet": None}
        good = {"event": "launch.late_runner", "safety_score": 55.0}
        bad = {"event": "launch.late_runner", "safety_score": 10.0}
        self.assertTrue(tg_filters.event_passes(good, flt))
        self.assertFalse(tg_filters.event_passes(bad, flt))
        # lane filter does not touch it
        flt2 = {"min_score": None, "lane": "lowcap", "quiet": None}
        self.assertTrue(tg_filters.event_passes(good, flt2))


if __name__ == "__main__":
    unittest.main()
