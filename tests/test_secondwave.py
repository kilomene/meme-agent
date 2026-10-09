"""Tests for second-wave re-entry (🔁 cards on pullback -> re-acceleration).

- pullback detection (>=40% from the milestone-tracked peak multiple),
  trough tracking (trough deepens / window restarts from the new low).
- re-acceleration confirmation (>=50% from trough within 45 min, with
  rising-poll confirmation; HOT fingerprints count as one rising poll;
  stale troughs never fire; one trough never fires twice).
- per-coin 6h second-wave cooldown, independent of the main
  duplicate-card cooldown lanes.
- safety re-check at re-entry is fail-closed (failing re-check ->
  silent hold: no card, no position, cooldown not burned).
- paper entry labeled second_wave with the wave number journaled.
- the launch.second_wave event validates against the strict event-bus
  schema and renders as a Telegram card; personal filters treat it as
  a signal event.
"""

import time
import unittest
import unittest.mock
from types import SimpleNamespace

from agent.events import bus as event_bus
from daemon.launch_scanner import LaunchScanner
from daemon.launch_scanner import milestones
from daemon.launch_scanner import secondwave
from daemon.telegram import alerts as tg_alerts
from daemon.telegram import filters as tg_filters


def _degen(**kw):
    d = {"second_wave_enabled": True,
         "second_wave_pullback_pct": 40.0,
         "second_wave_releg_gain_pct": 50.0,
         "second_wave_reentry_window_min": 45.0,
         "second_wave_cooldown_hours": 6.0,
         "early_min_score": 1.0,
         "stop_loss_pct": 30.0, "take_profit_pct": 200.0,
         "max_hold_hours": 4.0, "trail_trigger_pct": 50.0,
         "trail_drawdown_pct": 30.0,
         "entry_sol": 0.05, "max_positions": 5, "auto_enter": True,
         "daily_max_loss_sol": 0.15}
    d.update(kw)
    return SimpleNamespace(**d)


def _sig(mint="MINTW", mcap=100000.0, **kw):
    e = {"event": "launch.detected", "mint": mint, "symbol": "WAVE",
         "chain": "solana", "name": "Wave Token", "url": "http://x",
         "usd_market_cap": mcap, "safety_score": 72.0}
    e.update(kw)
    return e


def _track(state, degen, mint="MINTW", anchor=100000.0, peak=8.0):
    """Seed milestone tracking with a known anchor + peak multiple."""
    milestones.track_signal(state, degen, _sig(mint=mint, mcap=anchor))
    state["milestone_watch"][mint]["peak_mult"] = peak


def _poll(state, degen, mcap, now, mint="MINTW"):
    """Run secondwave.poll with a stubbed batch-stats fetch."""
    state.pop("second_wave_stats", None)  # force a fresh (mocked) read
    stats = {mint: {"usd_market_cap": mcap}} if mcap is not None else {}
    with unittest.mock.patch.object(secondwave, "_batch_stats",
                                    return_value=stats):
        return secondwave.poll(state, degen, now, 1)


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


def _scanner_with(safety_ok=True, score=72.0):
    sc = LaunchScanner.__new__(LaunchScanner)
    report = {"passed": True, "unknown": False, "score": float(score),
              "fails": [], "flags": []}
    sc._confirm_safety_ok = (
        lambda degen, cand: ((True, "ok", report) if safety_ok
                             else (False, "safety-fail: honeypot", report)))
    return sc


class PeakTrackingTests(unittest.TestCase):
    def test_milestones_poll_records_peak_mult(self):
        state, degen = {}, _degen()
        milestones.track_signal(state, degen, _sig())  # anchor 100k
        with unittest.mock.patch.object(
                milestones, "_batch_stats",
                return_value={"MINTW": {"usd_market_cap": 800000.0}}):
            milestones.poll(state, degen, time.time(), 4)
        self.assertEqual(state["milestone_watch"]["MINTW"]["peak_mult"], 8.0)
        # a later dip does not lower the recorded peak
        with unittest.mock.patch.object(
                milestones, "_batch_stats",
                return_value={"MINTW": {"usd_market_cap": 400000.0}}):
            milestones.poll(state, degen, time.time(), 8)
        self.assertEqual(state["milestone_watch"]["MINTW"]["peak_mult"], 8.0)

    def test_tracked_accessor(self):
        state, degen = {}, _degen()
        self.assertEqual(milestones.tracked(state), {})
        milestones.track_signal(state, degen, _sig())
        self.assertIn("MINTW", milestones.tracked(state))


class PullbackTests(unittest.TestCase):
    def test_pullback_detected_at_threshold(self):
        state, degen = {}, _degen()
        now = time.time()
        _track(state, degen, peak=8.0)
        # 4.7x = 41.25% below the 8x peak -> pullback
        out = _poll(state, degen, 470000.0, now)
        self.assertEqual(out, [])
        rec = state["second_wave"]["MINTW"]
        self.assertEqual(rec["phase"], "pullback")
        self.assertAlmostEqual(rec["trough_mult"], 4.7)

    def test_no_pullback_above_threshold(self):
        state, degen = {}, _degen()
        now = time.time()
        _track(state, degen, peak=8.0)
        # 4.9x = 38.75% below peak -> still "up"
        out = _poll(state, degen, 490000.0, now)
        self.assertEqual(out, [])
        self.assertEqual(state["second_wave"]["MINTW"]["phase"], "up")

    def test_no_pullback_while_running(self):
        state, degen = {}, _degen()
        now = time.time()
        _track(state, degen, peak=8.0)
        out = _poll(state, degen, 700000.0, now + 31)
        self.assertEqual(out, [])
        self.assertEqual(state["second_wave"]["MINTW"]["phase"], "up")


class TroughTests(unittest.TestCase):
    def test_trough_deepens_and_restarts_window(self):
        state, degen = {}, _degen()
        t0 = time.time()
        _track(state, degen, peak=8.0)
        _poll(state, degen, 470000.0, t0)          # trough 4.7 @ t0
        rec = state["second_wave"]["MINTW"]
        first_ts = rec["trough_ts"]
        _poll(state, degen, 400000.0, t0 + 60)    # trough deepens to 4.0
        rec = state["second_wave"]["MINTW"]
        self.assertAlmostEqual(rec["trough_mult"], 4.0)
        self.assertGreater(rec["trough_ts"], first_ts)
        self.assertEqual(rec["rise_streak"], 0)
        # a bounce that does not beat the trough keeps the deep trough
        _poll(state, degen, 420000.0, t0 + 120)
        rec = state["second_wave"]["MINTW"]
        self.assertAlmostEqual(rec["trough_mult"], 4.0)
        self.assertEqual(rec["rise_streak"], 1)

    def test_stale_trough_resets_without_firing(self):
        state, degen = {}, _degen()
        t0 = time.time()
        _track(state, degen, peak=8.0)
        _poll(state, degen, 470000.0, t0)          # trough 4.7 @ t0
        # 46 min later (window is 45): even a huge re-acceleration
        # must not fire from the stale trough
        out = _poll(state, degen, 900000.0, t0 + 46 * 60)
        self.assertEqual(out, [])
        self.assertEqual(state["second_wave"]["MINTW"]["phase"], "up")


class ConfirmTests(unittest.TestCase):
    def _to_trough(self, state, degen, t0):
        _track(state, degen, peak=8.0)
        _poll(state, degen, 470000.0, t0)          # pullback, trough 4.7
        self.assertEqual(state["second_wave"]["MINTW"]["phase"],
                         "pullback")

    def test_reacceleration_confirms(self):
        state, degen = {}, _degen()
        t0 = time.time()
        self._to_trough(state, degen, t0)
        t = t0
        for mcap in (520000.0, 580000.0, 650000.0):
            t += 60
            out = _poll(state, degen, mcap, t)
            self.assertEqual(out, [])  # not yet >=50% from trough
        t += 60
        out = _poll(state, degen, 740000.0, t)     # 7.4x = +57% of trough
        self.assertEqual(len(out), 1)
        sw = out[0]
        self.assertEqual(sw["mint"], "MINTW")
        self.assertEqual(sw["wave_number"], 2)     # wave 1 was the card
        self.assertAlmostEqual(sw["pullback_pct"], 41.2, places=1)
        self.assertAlmostEqual(sw["relegain_pct"], 57.4, places=1)
        self.assertAlmostEqual(sw["peak_multiple"], 8.0)
        self.assertAlmostEqual(sw["multiple_now"], 7.4)
        self.assertGreaterEqual(sw["rise_streak"], 2)

    def test_single_poll_spike_does_not_confirm(self):
        state, degen = {}, _degen()
        t0 = time.time()
        self._to_trough(state, degen, t0)
        # +57% from the trough in ONE poll: only one rising poll, needs 2
        out = _poll(state, degen, 740000.0, t0 + 60)
        self.assertEqual(out, [])

    def test_fp_hot_counts_as_rising_poll(self):
        state, degen = {}, _degen()
        t0 = time.time()
        self._to_trough(state, degen, t0)
        state["fingerprints"] = {"MINTW": {"verdict": "HOT"}}
        out = _poll(state, degen, 740000.0, t0 + 60)  # +57%, 1 rising poll
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["fp_hot"])

    def test_no_duplicate_waves_from_one_trough(self):
        state, degen = {}, _degen()
        t0 = time.time()
        self._to_trough(state, degen, t0)
        t = t0
        for mcap in (520000.0, 580000.0, 650000.0, 740000.0):
            t += 60
            out = _poll(state, degen, mcap, t)
        self.assertEqual(len(out), 1)  # fired once
        # the run continues — the consumed trough must not fire again
        for mcap in (800000.0, 850000.0, 900000.0):
            t += 60
            out = _poll(state, degen, mcap, t)
            self.assertEqual(out, [])
        # a FRESH pullback from the new peak can form a new wave
        state["milestone_watch"]["MINTW"]["peak_mult"] = 9.0
        t += 60
        _poll(state, degen, 530000.0, t)  # 5.3x = 41% below the 9x peak
        rec = state["second_wave"]["MINTW"]
        self.assertEqual(rec["phase"], "pullback")
        t += 60
        _poll(state, degen, 600000.0, t)
        t += 60
        _poll(state, degen, 680000.0, t)
        t += 60
        out = _poll(state, degen, 800000.0, t)  # +51% from 5.3x trough
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["wave_number"], 2)  # not yet handled -> 2


class CooldownTests(unittest.TestCase):
    def test_cooldown_suppresses_repeat_wave(self):
        state, degen = {}, _degen()
        t0 = time.time()
        _track(state, degen, peak=8.0)
        _poll(state, degen, 470000.0, t0)
        t = t0
        fired = None
        for mcap in (520000.0, 580000.0, 650000.0, 740000.0):
            t += 60
            out = _poll(state, degen, mcap, t)
            if out:
                fired = out[0]
        self.assertIsNotNone(fired)
        sc = _scanner_with(safety_ok=True)
        cfg = SimpleNamespace(degen=degen)
        state["degen_positions"] = {}
        published = []
        with unittest.mock.patch("agent.events.publish",
                                        side_effect=published.append):
            ok = secondwave.handle_wave(sc, cfg, _adapter(), state, fired)
        self.assertTrue(ok)
        self.assertEqual(len(published), 1)
        rec = state["second_wave"]["MINTW"]
        first_card_ts = rec["last_card_ts"]
        self.assertGreater(first_card_ts, 0)
        self.assertEqual(rec["wave_number"], 2)
        # a second confirmation 10 min later is swallowed by the cooldown
        state["milestone_watch"]["MINTW"]["peak_mult"] = 9.0
        t += 600
        _poll(state, degen, 530000.0, t)
        t += 60
        out2 = _poll(state, degen, 800000.0, t)
        if out2:
            with unittest.mock.patch("agent.events.publish",
                                            side_effect=published.append):
                ok2 = secondwave.handle_wave(sc, cfg, _adapter(), state,
                                             out2[0])
            self.assertFalse(ok2)
        self.assertEqual(len(published), 1)  # no second card
        self.assertEqual(rec["last_card_ts"], first_card_ts)  # not burned

    def test_cooldown_independent_of_main_lanes(self):
        state, degen = {}, _degen()
        t0 = time.time()
        _track(state, degen, peak=8.0)
        _poll(state, degen, 470000.0, t0)
        t = t0
        fired = None
        for mcap in (520000.0, 580000.0, 650000.0, 740000.0):
            t += 60
            out = _poll(state, degen, mcap, t)
            if out:
                fired = out[0]
        sc = _scanner_with(safety_ok=True)
        cfg = SimpleNamespace(degen=degen)
        before = set(state.keys())
        with unittest.mock.patch("agent.events.publish"):
            secondwave.handle_wave(sc, cfg, _adapter(), state, fired)
        # only the second-wave lane and the position book changed;
        # nothing the main duplicate-card / spam-guard lanes use.
        for lane_key in ("early_window", "early_digest", "momentum_seen",
                         "candidates", "seen", "pending_entry"):
            self.assertNotIn(lane_key, state)
        new_keys = set(state.keys()) - before
        self.assertLessEqual(new_keys,
                             {"second_wave", "second_wave_stats",
                              "degen_positions"})

    def test_cooldown_not_burned_on_safety_hold(self):
        state, degen = {}, _degen()
        t0 = time.time()
        _track(state, degen, peak=8.0)
        _poll(state, degen, 470000.0, t0)
        t = t0
        fired = None
        for mcap in (520000.0, 580000.0, 650000.0, 740000.0):
            t += 60
            out = _poll(state, degen, mcap, t)
            if out:
                fired = out[0]
        self.assertIsNotNone(fired)
        sc = _scanner_with(safety_ok=False)  # re-check fails
        cfg = SimpleNamespace(degen=degen)
        with unittest.mock.patch("agent.events.publish") as pub:
            ok = secondwave.handle_wave(sc, cfg, _adapter(), state, fired)
        self.assertFalse(ok)
        pub.assert_not_called()
        rec = state["second_wave"]["MINTW"]
        self.assertEqual(rec["last_card_ts"], 0.0)  # cooldown not burned
        self.assertEqual(rec["wave_number"], 1)     # no wave journaled


class SafetyTests(unittest.TestCase):
    def _confirmed(self):
        state, degen = {}, _degen()
        t0 = time.time()
        _track(state, degen, peak=8.0)
        _poll(state, degen, 470000.0, t0)
        t = t0
        fired = None
        for mcap in (520000.0, 580000.0, 650000.0, 740000.0):
            t += 60
            out = _poll(state, degen, mcap, t)
            if out:
                fired = out[0]
        self.assertIsNotNone(fired)
        return state, degen, fired

    def test_safety_fail_holds_silently(self):
        state, degen, fired = self._confirmed()
        sc = _scanner_with(safety_ok=False)
        cfg = SimpleNamespace(degen=degen)
        state["degen_positions"] = {}
        published = []
        with unittest.mock.patch("agent.events.publish",
                                        side_effect=published.append):
            ok = secondwave.handle_wave(sc, cfg, _adapter(), state, fired)
        self.assertFalse(ok)
        self.assertEqual(published, [])
        self.assertEqual(state["degen_positions"], {})

    def test_safety_pass_emits_card_and_position(self):
        state, degen, fired = self._confirmed()
        sc = _scanner_with(safety_ok=True, score=72.0)
        cfg = SimpleNamespace(degen=degen)
        state["degen_positions"] = {}
        ad = _adapter()
        published = []
        with unittest.mock.patch("agent.events.publish",
                                        side_effect=published.append):
            ok = secondwave.handle_wave(sc, cfg, ad, state, fired)
        self.assertTrue(ok)
        self.assertEqual(len(published), 1)
        card = published[0]
        # strict event-bus schema: must validate
        event_bus.validate_event(dict(card))
        self.assertEqual(card["event"], "launch.second_wave")
        self.assertEqual(card["wave_number"], 2)
        self.assertEqual(card["safety_score"], 72.0)
        self.assertIn("pass (score 72)", card["safety"])
        plan = card["plan"]
        self.assertEqual(plan["stop_pct"], 30.0)
        self.assertEqual(plan["tp_pct"], 200.0)
        self.assertIsNotNone(card["entry"])
        # paper position labeled second_wave, wave number journaled
        pos = state["degen_positions"].get("second_wave:MINTW")
        self.assertIsNotNone(pos)
        self.assertEqual(pos["wave"], 2)
        self.assertEqual(pos["wave_number"], 2)
        self.assertEqual(pos["mint"], "MINTW")
        self.assertNotIn("MINTW", state["degen_positions"])  # no clash
        req = ad.captured["reqs"][0]
        self.assertEqual(req.signal_id, "second_wave:MINTW")
        self.assertEqual(req.idempotency_key, "second_wave:MINTW")
        self.assertEqual(req.comment, "degen-second-wave")

    def test_non_solana_is_alert_only(self):
        state, degen, fired = self._confirmed()
        fired["chain"] = "base"
        state["milestone_watch"]["MINTW"]["chain"] = "base"
        sc = _scanner_with(safety_ok=True)
        cfg = SimpleNamespace(degen=degen)
        state["degen_positions"] = {}
        published = []
        with unittest.mock.patch("agent.events.publish",
                                        side_effect=published.append):
            ok = secondwave.handle_wave(sc, cfg, _adapter(), state, fired)
        self.assertTrue(ok)
        self.assertEqual(len(published), 1)
        self.assertIn("paper engine prices Solana",
                      published[0]["note"])
        self.assertEqual(state["degen_positions"], {})


class PaperEnterWaveTests(unittest.TestCase):
    def setUp(self):
        self.sc = LaunchScanner.__new__(LaunchScanner)

    def test_first_wave_unchanged(self):
        d = _degen()
        state = {"degen_positions": {}}
        ad = _adapter()
        card = {"entry": None, "note": ""}
        ok = self.sc._paper_enter(None, ad, state, card, d,
                                  "MINT1", "TKN")
        self.assertTrue(ok)
        self.assertIn("MINT1", state["degen_positions"])
        pos = state["degen_positions"]["MINT1"]
        self.assertEqual(pos["wave"], 1)
        self.assertEqual(ad.captured["reqs"][0].signal_id, "launch:MINT1")
        self.assertEqual(ad.captured["reqs"][0].comment, "degen-launch")
        # two-tier: leg A + runner leg B submitted
        self.assertEqual(len(ad.captured["reqs"]), 2)
        self.assertEqual(ad.captured["reqs"][1].signal_id, "launch:MINT1:B")
        self.assertIn("MINT1|B", state["degen_positions"])

    def test_second_wave_labeled(self):
        d = _degen()
        state = {"degen_positions": {}}
        ad = _adapter()
        card = {"entry": None, "note": ""}
        ok = self.sc._paper_enter(None, ad, state, card, d,
                                  "MINT2", "TKN2", wave=2)
        self.assertTrue(ok)
        self.assertIn("second_wave:MINT2", state["degen_positions"])
        pos = state["degen_positions"]["second_wave:MINT2"]
        self.assertEqual(pos["wave"], 2)
        self.assertEqual(pos["wave_number"], 2)
        self.assertEqual(pos["mint"], "MINT2")
        req = ad.captured["reqs"][0]
        self.assertEqual(req.signal_id, "second_wave:MINT2")
        self.assertEqual(req.idempotency_key, "second_wave:MINT2")
        self.assertEqual(req.comment, "degen-second-wave")
        # same exit plan as normal entries
        self.assertAlmostEqual(pos["sl"], 0.001 * 0.7)
        self.assertAlmostEqual(pos["tp"], 0.001 * 3.0)

    def test_second_wave_coexists_with_first_wave(self):
        d = _degen()
        state = {"degen_positions": {}}
        card = {"entry": None, "note": ""}
        self.assertTrue(self.sc._paper_enter(
            None, _adapter(), state, card, d, "MINT3", "TKN3"))
        self.assertTrue(self.sc._paper_enter(
            None, _adapter(), state, card, d, "MINT3", "TKN3", wave=2))
        self.assertIn("MINT3", state["degen_positions"])
        self.assertIn("second_wave:MINT3", state["degen_positions"])
        # two-tier: each wave is an A+B leg pair
        self.assertEqual(len(state["degen_positions"]), 4)
        self.assertIn("MINT3|B", state["degen_positions"])
        self.assertIn("second_wave:MINT3|B", state["degen_positions"])

    def test_second_wave_counts_toward_max_positions(self):
        d = _degen(max_positions=1)
        sc = _scanner_with(safety_ok=True)
        cfg = SimpleNamespace(degen=d)
        st, dg, fired = SafetyTests()._confirmed()
        st["degen_positions"] = {"MINT9": {"ticket": 1}}  # cap reached
        with unittest.mock.patch("agent.events.publish") as pub:
            ok = secondwave.handle_wave(sc, cfg, _adapter(), st, fired)
        self.assertTrue(ok)
        self.assertIn("max degen positions reached",
                      pub.call_args[0][0]["note"])
        self.assertNotIn("second_wave:MINTW", st["degen_positions"])


class CardTests(unittest.TestCase):
    def _card(self):
        return {"event": "launch.second_wave", "symbol": "WAVE",
                "mint": "MINTW", "chain": "solana", "name": "Wave Token",
                "url": "http://x", "usd_market_cap": 740000.0,
                "wave_number": 2, "pullback_pct": 41.2,
                "relegain_pct": 57.4, "peak_multiple": 8.0,
                "multiple_now": 7.4, "safety": "pass (score 72)",
                "safety_score": 72.0,
                "plan": {"stop_pct": 30.0, "tp_pct": 200.0,
                         "max_hold_h": 4.0, "trail_trigger_pct": 50.0,
                         "trail_drawdown_pct": 30.0},
                "source": "launch.detected", "confirmed": True,
                "minutes_since_card": 63.0,
                "entry": {"ticket": 4242, "volume_sol": 0.05,
                         "price_sol": 0.001, "stop_loss_pct": 30.0,
                         "take_profit_pct": 200.0},
                "note": ""}

    def test_card_validates_and_renders(self):
        card = self._card()
        event_bus.validate_event(dict(card))
        text = tg_alerts.format_alert(card)
        self.assertIn("🔁 SECOND WAVE", text)
        self.assertIn("wave #2", text)
        self.assertIn("MINTW", text)
        self.assertIn("📋 Plan", text)
        self.assertIn("stop -30%", text)
        self.assertIn("fast-analysis safety score: 72.0/100", text)
        self.assertIn("pullback -41%", text)
        self.assertIn("paper entry", text)
        markup = tg_alerts.format_alert_markup(card)
        self.assertIn("MINTW", str(markup))

    def test_filters_treat_as_signal_event(self):
        self.assertIn("launch.second_wave", tg_filters.SIGNAL_EVENTS)
        self.assertIn("launch.second_wave", tg_filters._KIND_LABELS)
        card = self._card()
        self.assertTrue(tg_filters.event_passes(card, {"min_score": 50.0}))
        self.assertFalse(tg_filters.event_passes(card, {"min_score": 80.0}))
        self.assertTrue(tg_filters.event_passes(card, {}))


class DisabledTests(unittest.TestCase):
    def test_disabled_tracks_nothing(self):
        state, degen = {}, _degen(second_wave_enabled=False)
        milestones.track_signal(state, degen, _sig())
        state["milestone_watch"]["MINTW"]["peak_mult"] = 8.0
        out = _poll(state, degen, 400000.0, time.time())
        self.assertEqual(out, [])
        self.assertNotIn("second_wave", state)

    def test_pending_anchor_skipped(self):
        state, degen = {}, _degen()
        milestones.track_signal(state, degen, _sig(mcap=0))  # pending
        rec = state["milestone_watch"]["MINTW"]
        self.assertTrue(rec["pending"])
        out = _poll(state, degen, 400000.0, time.time())
        self.assertEqual(out, [])
        self.assertNotIn("MINTW", state.get("second_wave") or {})

    def test_expired_milestone_prunes(self):
        state, degen = {}, _degen()
        t0 = time.time()
        _track(state, degen, peak=8.0)
        _poll(state, degen, 470000.0, t0)
        self.assertIn("MINTW", state["second_wave"])
        # milestone watch drops the coin (24h expiry) -> pruned too
        state["milestone_watch"].pop("MINTW")
        out = _poll(state, degen, 470000.0, t0 + 25 * 3600)
        self.assertEqual(out, [])
        self.assertNotIn("MINTW", state.get("second_wave") or {})


if __name__ == "__main__":
    unittest.main()
