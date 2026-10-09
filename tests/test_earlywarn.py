"""Tests for the early-warning lane and the insider behavior tracker.

The warning lane must:
  * only warn on a KNOWN score at/above the floor (default 1.0),
  * NEVER warn on can't-exit coins (honeypot / cannot-sell / sell tax),
  * print the exact dangers on the card (never hide them),
  * respect the per-coin cooldown and the hourly rate limit,
  * publish ONLY {"event": "degen.warning"} — no trades, no broker.

The insider tracker must measure without ever raising, and must never
change any gate on its own (v1 = measurement + display only).
"""

import unittest
import time
from types import SimpleNamespace
from unittest.mock import patch

from daemon.launch_scanner import earlywarn, insiders
from daemon.telegram import alerts as tg_alerts


def _degen(**kw):
    d = {"warn_enabled": True, "warn_min_score": 1.0,
         "warn_max_per_hour": 4, "warn_cooldown_hours": 6.0,
         "warn_min_buys_m5": 20.0, "warn_min_vol_m5_usd": 5000.0,
         "insider_track_enabled": True, "insider_watch_max": 40,
         "insider_refresh_minutes": 15.0, "insider_fast_minutes": 5.0,
         "insider_fast_window_minutes": 60.0}
    d.update(kw)
    return SimpleNamespace(**d)


def _report(score, fails=None, flags=None):
    return {"score": score, "fails": fails or [], "flags": flags or []}


class FloorTest(unittest.TestCase):
    def test_score_1_passes_default_floor(self):
        self.assertTrue(earlywarn.floor_ok(_degen(), _report(1)))

    def test_score_50_passes(self):
        self.assertTrue(earlywarn.floor_ok(_degen(), _report(50)))

    def test_zero_and_none_are_silent(self):
        self.assertFalse(earlywarn.floor_ok(_degen(), _report(0)))
        self.assertFalse(earlywarn.floor_ok(_degen(), _report(None)))
        self.assertFalse(earlywarn.floor_ok(_degen(), None))

    def test_custom_floor(self):
        self.assertFalse(earlywarn.floor_ok(_degen(warn_min_score=5.0),
                                            _report(4)))
        self.assertTrue(earlywarn.floor_ok(_degen(warn_min_score=5.0),
                                           _report(5)))


class CantExitTest(unittest.TestCase):
    def test_honeypot_excluded(self):
        rep = _report(1, fails=["honeypot (sell simulation)"])
        self.assertTrue(earlywarn.cant_exit(rep))

    def test_cannot_sell_excluded(self):
        rep = _report(3, fails=["cannot sell all (sell simulation)"])
        self.assertTrue(earlywarn.cant_exit(rep))

    def test_sell_tax_excluded(self):
        rep = _report(10, fails=["sell tax 25% too high"])
        self.assertTrue(earlywarn.cant_exit(rep))

    def test_insider_fails_are_not_cant_exit(self):
        rep = _report(1, fails=["10 graph insiders (limit 5)"],
                      flags=["insider network detected"])
        self.assertFalse(earlywarn.cant_exit(rep))

    def test_no_report_is_safe(self):
        self.assertFalse(earlywarn.cant_exit(None))


class DangersTest(unittest.TestCase):
    def test_merges_fails_and_flags_deduped(self):
        rep = _report(1, fails=["a", "b"], flags=["b", "c"])
        self.assertEqual(earlywarn.dangers(rep), ["a", "b", "c"])

    def test_caps_at_six(self):
        rep = _report(1, fails=["f%d" % i for i in range(10)])
        self.assertEqual(len(earlywarn.dangers(rep)), 6)


class LaunchTriggerTest(unittest.TestCase):
    def test_hot_fingerprint_triggers(self):
        self.assertEqual(
            earlywarn.launch_trigger(_degen(), {}, "HOT"), "hot_fingerprint")

    def test_warming_triggers(self):
        self.assertEqual(
            earlywarn.launch_trigger(_degen(), {}, "WARMING"),
            "hot_fingerprint")

    def test_strong_first_sight_triggers(self):
        c = {"buys_m5": 50, "vol_m5": 20000}
        self.assertEqual(
            earlywarn.launch_trigger(_degen(), c, "COLD"), "early_momentum")

    def test_weak_first_sight_silent(self):
        c = {"buys_m5": 2, "vol_m5": 100}
        self.assertIsNone(earlywarn.launch_trigger(_degen(), c, "COLD"))

    def test_no_data_silent(self):
        self.assertIsNone(earlywarn.launch_trigger(_degen(), {}, None))


class MaybeWarnTest(unittest.TestCase):
    def _published(self, *a, **kw):
        with patch("agent.events.publish") as pub:
            ok = earlywarn.maybe_warn(*a, **kw)
        return ok, (pub.call_args[0][0] if pub.called else None)

    def test_publishes_warning_event(self):
        ok, payload = self._published(
            {}, _degen(), "launch", "BRAIN", "MINT1", "solana",
            1500000.0, 1.0, ["insider network detected"],
            "hot_fingerprint")
        self.assertTrue(ok)
        self.assertEqual(payload["event"], "degen.warning")
        self.assertTrue(payload["unconfirmed"])
        self.assertEqual(payload["door"], "launch")
        self.assertEqual(payload["dangers"], ["insider network detected"])
        self.assertNotIn("trade", payload)

    def test_disabled_lane_silent(self):
        ok, payload = self._published(
            {}, _degen(warn_enabled=False), "launch", "BRAIN", "MINT1",
            "solana", 1500000.0, 1.0, ["x"], "hot_fingerprint")
        self.assertFalse(ok)
        self.assertIsNone(payload)

    def test_per_coin_cooldown(self):
        state = {}
        args = ({}, _degen(), "momentum", "SI", "MINT2", "solana",
                2000000.0, 1.0, ["10 graph insiders (limit 5)"],
                "momentum_qualify")
        ok1, _ = self._published(state, *args[1:])
        self.assertTrue(ok1)
        ok2, payload2 = self._published(state, *args[1:])
        self.assertFalse(ok2)
        self.assertIsNone(payload2)

    def test_hourly_rate_limit(self):
        state = {}
        for i in range(4):
            ok, _ = self._published(
                state, _degen(), "launch", "C%d" % i, "M%d" % i, "solana",
                1000000.0, 2.0, ["low score"], "early_momentum")
            self.assertTrue(ok)
        ok, payload = self._published(
            state, _degen(), "launch", "C9", "M9", "solana",
            1000000.0, 2.0, ["low score"], "early_momentum")
        self.assertFalse(ok)
        self.assertIsNone(payload)

    def test_missing_mint_or_trigger_silent(self):
        ok, _ = self._published(
            {}, _degen(), "launch", "X", "", "solana",
            1000.0, 1.0, ["x"], "hot_fingerprint")
        self.assertFalse(ok)
        ok, _ = self._published(
            {}, _degen(), "launch", "X", "M1", "solana",
            1000.0, 1.0, ["x"], "")
        self.assertFalse(ok)


class InsiderParseTest(unittest.TestCase):
    def test_parses_cluster_and_graph(self):
        rep = _report(1,
                      fails=["insider cluster holds 34% (limit 20%)",
                             "10 graph insiders (limit 5)"])
        cluster, graph_n = insiders.parse_insider_signals(rep)
        self.assertEqual(cluster, 34.0)
        self.assertEqual(graph_n, 10)

    def test_parses_flag_shape(self):
        rep = _report(20, flags=["insider network holds 12%"])
        cluster, graph_n = insiders.parse_insider_signals(rep)
        self.assertEqual(cluster, 12.0)
        self.assertEqual(graph_n, 0)

    def test_empty_report(self):
        self.assertEqual(insiders.parse_insider_signals(None), (0.0, 0))
        self.assertFalse(insiders.has_insider_signal(_report(80)))


class InsiderWatchTest(unittest.TestCase):
    def _rep(self):
        return _report(1, fails=["10 graph insiders (limit 5)"])

    def test_watch_adds_entry(self):
        state = {}
        insiders.watch(state, _degen(), "M1", "solana", self._rep())
        w = state["insider_watch"]["M1"]
        self.assertEqual(w["first_graph_n"], 10)
        self.assertEqual(w["samples"], 1)

    def test_watch_idempotent(self):
        state = {}
        insiders.watch(state, _degen(), "M1", "solana", self._rep())
        insiders.watch(state, _degen(), "M1", "solana", self._rep())
        self.assertEqual(len(state["insider_watch"]), 1)

    def test_watch_ignores_clean_coin(self):
        state = {}
        insiders.watch(state, _degen(), "M1", "solana", _report(80))
        self.assertEqual(state.get("insider_watch", {}), {})

    def test_watch_respects_max(self):
        state = {}
        d = _degen(insider_watch_max=1)
        insiders.watch(state, d, "M1", "solana", self._rep())
        insiders.watch(state, d, "M2", "solana", self._rep())
        self.assertEqual(len(state["insider_watch"]), 1)

    def test_disabled_tracker(self):
        state = {}
        insiders.watch(state, _degen(insider_track_enabled=False),
                       "M1", "solana", self._rep())
        self.assertEqual(state.get("insider_watch", {}), {})


class InsiderTrendTest(unittest.TestCase):
    def _watched(self):
        state = {}
        rep = _report(1, fails=["insider cluster holds 20% (limit 20%)"])
        insiders.watch(state, _degen(), "M1", "solana", rep)
        return state

    def test_steady_on_first_snapshot(self):
        state = self._watched()
        t = insiders.trend(state, "M1")
        self.assertEqual(t["direction"], "steady")
        self.assertIn("steady", insiders.trend_line(state, "M1"))

    def test_diluting(self):
        state = self._watched()
        state["insider_watch"]["M1"]["last_cluster_pct"] = 8.0
        self.assertEqual(insiders.trend(state, "M1")["direction"],
                         "diluting")
        self.assertIn("diluting", insiders.trend_line(state, "M1"))

    def test_accumulating(self):
        state = self._watched()
        state["insider_watch"]["M1"]["last_cluster_pct"] = 45.0
        self.assertEqual(insiders.trend(state, "M1")["direction"],
                         "accumulating")
        self.assertIn("caution", insiders.trend_line(state, "M1"))

    def test_unwatched_mint(self):
        self.assertIsNone(insiders.trend({}, "NOPE"))
        self.assertEqual(insiders.trend_line({}, "NOPE"), "")


class InsiderUpdateTest(unittest.TestCase):
    def test_update_refreshes_and_prunes(self):
        state = {}
        rep = _report(1, fails=["insider cluster holds 20% (limit 20%)"])
        insiders.watch(state, _degen(), "M1", "solana", rep)
        w = state["insider_watch"]["M1"]
        w["last_ts"] -= 20 * 60  # force stale past the 15m interval
        fresh = _report(1, fails=["insider cluster holds 9% (limit 20%)"])
        with patch("daemon.launch_scanner.safety.check_token",
                   return_value=fresh):
            insiders.update(state, _degen())
        self.assertEqual(w["last_cluster_pct"], 9.0)
        self.assertEqual(w["samples"], 2)

    def test_update_prunes_24h_stale(self):
        state = {}
        insiders.watch(state, _degen(), "M1", "solana",
                       _report(1, fails=["10 graph insiders (limit 5)"]))
        state["insider_watch"]["M1"]["last_ts"] -= 25 * 3600
        insiders.update(state, _degen())
        self.assertEqual(state["insider_watch"], {})

    def test_update_never_raises(self):
        insiders.update({}, _degen())
        with patch("daemon.launch_scanner.safety.check_token",
                   side_effect=RuntimeError("boom")):
            state = {}
            insiders.watch(
                state, _degen(), "M1", "solana",
                _report(1, fails=["insider cluster holds 20% (limit 20%)"]))
            state["insider_watch"]["M1"]["last_ts"] -= 20 * 60
            insiders.update(state, _degen())  # must not raise

    def test_fast_start_refreshes_within_5_minutes(self):
        state = {}
        insiders.watch(state, _degen(), "M1", "solana",
                       _report(1, fails=["10 graph insiders (limit 5)"]))
        w = state["insider_watch"]["M1"]
        w["last_ts"] -= 6 * 60  # 6 min stale: past fast 5m, inside 60m window
        fresh = _report(1, fails=["12 graph insiders (limit 5)"])
        with patch("daemon.launch_scanner.safety.check_token",
                   return_value=fresh):
            insiders.update(state, _degen())
        self.assertEqual(w["last_graph_n"], 12)
        self.assertEqual(w["samples"], 2)

    def test_fast_window_expires_back_to_15_minutes(self):
        state = {}
        insiders.watch(state, _degen(), "M1", "solana",
                       _report(1, fails=["10 graph insiders (limit 5)"]))
        w = state["insider_watch"]["M1"]
        w["first_ts"] -= 61 * 60  # watched over an hour ago: window over
        w["last_ts"] -= 6 * 60    # 6 min stale: past fast 5m, under normal 15m
        with patch("daemon.launch_scanner.safety.check_token") as ck:
            insiders.update(state, _degen())
            ck.assert_not_called()  # normal interval not reached yet
        w["last_ts"] -= 16 * 60   # now past the normal 15m interval
        with patch("daemon.launch_scanner.safety.check_token",
                   return_value=_report(1, fails=["11 graph insiders"])):
            insiders.update(state, _degen())
        self.assertEqual(w["last_graph_n"], 11)

    def test_fast_start_disabled_when_knob_zero(self):
        state = {}
        degen = _degen(insider_fast_minutes=0.0)
        insiders.watch(state, degen, "M1", "solana",
                       _report(1, fails=["10 graph insiders (limit 5)"]))
        state["insider_watch"]["M1"]["last_ts"] -= 6 * 60
        with patch("daemon.launch_scanner.safety.check_token") as ck:
            insiders.update(state, degen)
            ck.assert_not_called()  # falls back to the 15m interval

    def test_watch_cap_defaults_to_40(self):
        state = {}
        for i in range(45):
            insiders.watch(state, _degen(), "M%d" % i, "solana",
                           _report(1, fails=["10 graph insiders (limit 5)"]))
        self.assertEqual(len(state["insider_watch"]), 40)

    def test_interval_for_missing_first_ts_never_raises(self):
        w = {"last_ts": time.time() - 3600}  # legacy entry, no first_ts
        iv = insiders._interval_for(w, _degen(), time.time())
        self.assertGreater(iv, 0)


class WarningCardTest(unittest.TestCase):
    def test_card_labels_unconfirmed_and_dangers(self):
        text = tg_alerts.format_alert({
            "event": "degen.warning", "door": "launch", "symbol": "BRAIN",
            "chain": "solana", "mint": "MINT1",
            "usd_market_cap": 1500000.0, "safety_score": 1,
            "dangers": ["10 graph insiders (limit 5)",
                        "insider network detected"],
            "trigger": "hot_fingerprint",
            "insider_line": "🧠 Insiders: 20% → 20% (holding steady)",
        })
        self.assertIn("UNCONFIRMED", text)
        self.assertIn("1/100", text)
        self.assertIn("BELOW the safety bar", text)
        self.assertIn("10 graph insiders (limit 5)", text)
        self.assertIn("NOT VETTED", text)
        self.assertIn("MINT1", text)

    def test_card_never_suggests_buying(self):
        text = tg_alerts.format_alert({
            "event": "degen.warning", "door": "momentum", "symbol": "SI",
            "chain": "solana", "mint": "MINT2",
            "usd_market_cap": 2000000.0, "safety_score": 1,
            "dangers": ["10 graph insiders (limit 5)"],
            "trigger": "momentum_qualify",
        }).lower()
        self.assertNotIn("buy", text)


if __name__ == "__main__":
    unittest.main()


class MomentumHoldWarnsIntegrationTest(unittest.TestCase):
    """End-to-end: a momentum runner held at score 1 emits exactly one
    degen.warning (UNCONFIRMED) — and nothing else."""

    def test_held_score1_coin_warns(self):
        from unittest.mock import patch as _patch
        from daemon.launch_scanner import LaunchScanner, momentum

        cfg = SimpleNamespace(degen=_degen())
        sc = LaunchScanner()
        state, tracked = {}, {}
        scanner = momentum.MomentumScanner()
        r = {"pool_address": "pool1", "mint": "MINT9", "symbol": "BRAIN",
             "name": "Brain", "chain": "solana", "chg_24h_pct": 120.0,
             "chg_1h_pct": 15.0, "vol_24h_usd": 500000.0,
             "liq_usd": 120000.0, "usd_market_cap": 1800000.0,
             "url": "https://example.invalid/p"}
        published = []

        class FakeBus:
            def publish(self, e):
                published.append(e)

        report = {"score": 1, "fails": [],
                  "flags": ["insider network detected"]}
        with _patch.object(LaunchScanner, "_instant_verdict",
                           return_value=(False, "score 1 < 50", report)), \
             _patch.object(LaunchScanner, "_sentiment_track",
                           return_value=None), \
             _patch("agent.events.publish") as pub:
            sc._momentum_handle(cfg, None, state, tracked, scanner, r,
                                1000.0, FakeBus())
            published.extend(a[0][0] for a in pub.call_args_list)

        warnings = [e for e in published
                    if e.get("event") == "degen.warning"]
        self.assertEqual(len(warnings), 1)
        w = warnings[0]
        self.assertEqual(w["door"], "momentum")
        self.assertTrue(w["unconfirmed"])
        self.assertEqual(w["safety_score"], 1)
        self.assertIn("insider network detected", w["dangers"])
        # Main stream untouched: no vetted alert, no track, no cooldown.
        self.assertFalse(any(e.get("event") == "momentum.alert"
                             for e in published))
        self.assertEqual(tracked, {})
        self.assertEqual(scanner._last_alert, {})

    def test_held_honeypot_stays_silent(self):
        from unittest.mock import patch as _patch
        from daemon.launch_scanner import LaunchScanner, momentum

        cfg = SimpleNamespace(degen=_degen())
        sc = LaunchScanner()
        state, tracked = {}, {}
        scanner = momentum.MomentumScanner()
        r = {"pool_address": "pool1", "mint": "MINT10", "symbol": "RUG",
             "chain": "solana", "chg_24h_pct": 200.0,
             "usd_market_cap": 900000.0}
        published = []

        class FakeBus:
            def publish(self, e):
                published.append(e)

        report = {"score": 5, "fails": ["honeypot (sell simulation)"],
                  "flags": []}
        with _patch.object(LaunchScanner, "_instant_verdict",
                           return_value=(False, "honeypot", report)), \
             _patch.object(LaunchScanner, "_sentiment_track",
                           return_value=None), \
             _patch("agent.events.publish") as pub:
            sc._momentum_handle(cfg, None, state, tracked, scanner, r,
                                1000.0, FakeBus())
            published.extend(a[0][0] for a in pub.call_args_list)
        self.assertEqual(published, [])


if __name__ == "__main__":
    unittest.main()


class InsiderOutcomeLedgerTest(unittest.TestCase):
    """Phase 1: shadow closes joined with insider trends."""

    def _watched(self, mint="M1", first=34.0, last=20.0):
        state = {}
        rep = _report(1, fails=["insider cluster holds 20% (limit 20%)"])
        insiders.watch(state, _degen(), mint, "solana", rep)
        state["insider_watch"][mint]["first_cluster_pct"] = first
        state["insider_watch"][mint]["last_cluster_pct"] = last
        state["insider_watch"][mint]["symbol"] = "BRN"
        return state

    def test_record_outcome_joins_trend(self):
        state = self._watched()  # 34 -> 20 = diluting
        ok = insiders.record_outcome(state, _degen(), "M1", {
            "symbol": "BRN", "chain": "solana",
            "outcome": "win", "pnl_pct": 210.0})
        self.assertTrue(ok)
        rows = state["insider_outcomes"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["direction"], "diluting")
        self.assertEqual(rows[0]["pnl_pct"], 210.0)
        self.assertEqual(rows[0]["outcome"], "win")
        self.assertEqual(rows[0]["cluster_start"], 34.0)
        self.assertEqual(rows[0]["cluster_end"], 20.0)

    def test_record_outcome_accumulating(self):
        state = self._watched(first=20.0, last=45.0)
        insiders.record_outcome(state, _degen(), "M1",
                               {"outcome": "loss", "pnl_pct": -30.0})
        self.assertEqual(state["insider_outcomes"][0]["direction"],
                         "accumulating")

    def test_record_outcome_unknown_mint_is_noop(self):
        state = {}
        self.assertFalse(insiders.record_outcome(
            state, _degen(), "NOPE", {"outcome": "win", "pnl_pct": 5.0}))
        self.assertNotIn("insider_outcomes", state)

    def test_record_outcome_never_raises(self):
        self.assertFalse(insiders.record_outcome(None, None, None, None))
        self.assertEqual(
            insiders.outcome_summary(None)["total"], 0)

    def test_ledger_cap(self):
        state = self._watched()
        d = _degen(insider_ledger_max=3)
        for i in range(6):
            insiders.record_outcome(state, d, "M1",
                                   {"outcome": "win", "pnl_pct": float(i)})
        self.assertEqual(len(state["insider_outcomes"]), 3)
        # newest kept
        self.assertEqual(state["insider_outcomes"][-1]["pnl_pct"], 5.0)

    def test_outcome_summary_aggregation(self):
        state = self._watched(mint="M1", first=34.0, last=20.0)  # diluting
        state2 = self._watched(mint="M2", first=10.0, last=40.0)  # accumulating
        state["insider_watch"].update(state2["insider_watch"])
        insiders.record_outcome(state, _degen(), "M1",
                               {"outcome": "win", "pnl_pct": 210.0})
        insiders.record_outcome(state, _degen(), "M1",
                               {"outcome": "loss", "pnl_pct": -30.0})
        insiders.record_outcome(state, _degen(), "M2",
                               {"outcome": "win", "pnl_pct": 50.0})
        summ = insiders.outcome_summary(state)
        self.assertEqual(summ["total"], 3)
        self.assertEqual(summ["by_direction"]["diluting"]["n"], 2)
        self.assertEqual(summ["by_direction"]["diluting"]["win_rate"], 50.0)
        self.assertEqual(summ["by_direction"]["diluting"]["avg_pnl"], 90.0)
        self.assertEqual(summ["by_direction"]["accumulating"]["n"], 1)
        self.assertEqual(summ["by_direction"]["accumulating"]["win_rate"],
                         100.0)

    def test_shadow_close_joins_insider_ledger(self):
        from daemon.launch_scanner import shadow
        state = self._watched()
        pos = {"symbol": "BRN", "chain": "solana", "kind": "launch",
               "entry_mcap": 1000000.0, "opened_ts": time.time(),
               "reason": "score 1 < 50"}
        shadow._close(state, _degen(), "M1", pos, 3100000.0, "win",
                      time.time())
        self.assertEqual(state["shadow_closed"][0]["outcome"], "win")
        rows = state.get("insider_outcomes") or []
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["direction"], "diluting")
        self.assertAlmostEqual(rows[0]["pnl_pct"], 210.0)

    def test_shadow_close_without_insider_watch_still_works(self):
        from daemon.launch_scanner import shadow
        state = {}
        pos = {"symbol": "X", "chain": "solana", "kind": "launch",
               "entry_mcap": 1000000.0, "opened_ts": time.time()}
        shadow._close(state, _degen(), "M9", pos, 700000.0, "loss",
                      time.time())
        self.assertEqual(state["shadow_closed"][0]["outcome"], "loss")
        self.assertFalse(state.get("insider_outcomes"))
