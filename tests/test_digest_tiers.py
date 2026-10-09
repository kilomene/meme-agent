"""Tests for FIX A — digest honesty & coverage.

Covers:
  * tier split correctness: items_safe = top 15 by safety score,
    items_fresh = up to 15 more by most-recently-queued (``ts``), with
    no overlap between the tiers;
  * honest footer counts ("showing X of Y" with real numbers);
  * the tiered card rendering (🛡 Highest safety / 🎲 Fresh shots);
  * the read-only /digestfull command, incl. the empty-queue message;
  * every queued coin retrievable via either the card tiers or
    /digestfull (no silent drops);
  * flush cadence + the instant spam-guard cap unchanged.
"""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent import events as event_bus
from daemon.launch_scanner import LaunchScanner
from daemon.telegram import alerts as tg_alerts
from daemon.telegram import commands as tg_commands
from daemon.telegram import security as tg_security

T0 = 1_700_000_000.0


def _degen(**kw):
    d = {"early_alert_window_seconds": 300.0,
         "early_alert_max_per_window": 4,
         "early_min_score": 1.0,
         "stop_loss_pct": 30.0, "take_profit_pct": 200.0,
         "max_hold_hours": 4.0, "trail_trigger_pct": 50.0,
         "trail_drawdown_pct": 30.0}
    d.update(kw)
    return SimpleNamespace(**d)


def _item(i, score=None, ts=None):
    return {
        "mint": "MINT%03d" % i,
        "symbol": "SYM%03d" % i,
        "name": "Coin %d" % i,
        "chain": "solana",
        "tier": "early",
        "source": "pumpfun",
        "url": "https://pump.fun/coin/MINT%03d" % i,
        "reason": "quality-pass score %s" % score,
        "safety_score": score,
        "narratives": ["other"],
        "narrative_heat": 1.0,
        "sentiment": None,
        "ts": T0 + i * 60.0 if ts is None else ts,
    }


def _queue(n, score_fn=None):
    """Seed a digest queue of n items; score_fn(i) -> score or None."""
    out = []
    for i in range(n):
        score = score_fn(i) if score_fn else float(i)
        out.append(_item(i, score=score))
    return out


def _flush(state, degen):
    """Run _flush_early_digest with an expired window; return the card."""
    sc = LaunchScanner.__new__(LaunchScanner)
    cfg = SimpleNamespace(degen=degen)
    state["early_window"] = {"start": time.time() - 301.0, "count": 0}
    published = []
    with patch.object(event_bus, "publish",
                      side_effect=published.append):
        sc._flush_early_digest(cfg, state)
    assert len(published) == 1
    return published[0]


class TierSplitTests(unittest.TestCase):
    def test_fifteen_fifteen_no_overlap(self):
        state = {"early_digest": _queue(40, score_fn=lambda i: float(i))}
        card = _flush(state, _degen())
        safe = card["items_safe"]
        fresh = card["items_fresh"]
        self.assertEqual(len(safe), 15)
        self.assertEqual(len(fresh), 15)
        # items_safe: top 15 by safety score (scores were 0..39).
        self.assertEqual([d["safety_score"] for d in safe],
                         [float(s) for s in range(39, 24, -1)])
        # No overlap between the tiers.
        safe_mints = {d["mint"] for d in safe}
        fresh_mints = {d["mint"] for d in fresh}
        self.assertEqual(safe_mints & fresh_mints, set())
        # items_fresh: most recently queued first (ts = T0 + i*60),
        # skipping the 15 already in items_safe.
        self.assertEqual([d["mint"] for d in fresh],
                         ["MINT%03d" % i for i in range(24, 9, -1)])

    def test_fresh_tier_picks_recency_not_score(self):
        # Reverse the correlation: the OLDEST coins have the best
        # scores, so the fresh tier must still pick the newest ones.
        state = {"early_digest": _queue(
            20, score_fn=lambda i: float(100 - i))}
        card = _flush(state, _degen())
        safe_mints = {d["mint"] for d in card["items_safe"]}
        fresh_mints = {d["mint"] for d in card["items_fresh"]}
        # safe = highest scores = oldest (i=0..14); fresh must be the
        # newest that are NOT in safe.
        self.assertEqual(safe_mints,
                         {"MINT%03d" % i for i in range(15)})
        self.assertEqual([d["mint"] for d in card["items_fresh"]],
                         ["MINT%03d" % i for i in range(19, 14, -1)])
        self.assertEqual(safe_mints & fresh_mints, set())

    def test_small_queue_single_tier(self):
        state = {"early_digest": _queue(5)}
        card = _flush(state, _degen())
        self.assertEqual(len(card["items_safe"]), 5)
        self.assertEqual(card["items_fresh"], [])
        self.assertEqual(card["count"], 5)
        self.assertEqual(card["shown"], 5)

    def test_item_fields_carried(self):
        state = {"early_digest": [_item(0, score=72.0)]}
        card = _flush(state, _degen())
        it = card["items_safe"][0]
        self.assertEqual(it["mint"], "MINT000")
        self.assertEqual(it["symbol"], "SYM000")
        self.assertEqual(it["safety_score"], 72.0)
        # strict event-bus schema: must validate
        event_bus.validate_event(dict(card))

    def test_none_scores_sort_last(self):
        q = _queue(16, score_fn=lambda i: None if i < 3 else float(i))
        state = {"early_digest": q}
        card = _flush(state, _degen())
        # top 15 by score: the 13 scored coins + 2 unscored (score 0).
        self.assertEqual(len(card["items_safe"]), 15)
        self.assertEqual(card["items_fresh"][0]["mint"], "MINT002")


class HonestFooterTests(unittest.TestCase):
    def test_footer_shows_real_numbers(self):
        state = {"early_digest": _queue(40)}
        card = _flush(state, _degen())
        self.assertEqual(card["count"], 40)
        self.assertEqual(card["shown"], 30)
        text = tg_alerts._launch_early_digest(card)
        self.assertIn("showing 30 of 40", text)
        self.assertNotIn("211 alerts collapsed", text)

    def test_tier_headers_render(self):
        state = {"early_digest": _queue(40)}
        card = _flush(state, _degen())
        text = tg_alerts._launch_early_digest(card)
        self.assertIn("🛡 Highest safety", text)
        self.assertIn("🎲 Fresh shots", text)
        # every shown coin gets a line with its score
        self.assertEqual(text.count("[score "), 30)

    def test_small_queue_footer(self):
        state = {"early_digest": _queue(5)}
        card = _flush(state, _degen())
        text = tg_alerts._launch_early_digest(card)
        self.assertIn("showing 5 of 5", text)
        self.assertNotIn("Fresh shots", text)

    def test_legacy_items_still_render(self):
        # Pre-tier journal events keep rendering.
        e = {"event": "launch.early_digest", "count": 25,
             "items": [{"symbol": "A", "mint": "MA", "tier": "early",
                        "safety_score": 80}]}
        text = tg_alerts._launch_early_digest(e)
        self.assertIn("• A [EARLY] [score 80/100]", text)
        self.assertIn("…and 5 more (see journal)", text)


class NoSilentDropsTests(unittest.TestCase):
    def test_every_queued_coin_retrievable(self):
        state = {"early_digest": _queue(40)}
        queued_mints = {d["mint"] for d in state["early_digest"]}
        # Before the flush: /digestfull exposes the whole queue.
        dump = self._digestfull_dump(state)
        for m in queued_mints:
            self.assertIn(m, dump)
        # After the flush: the card's tiers cover 30 of the 40, with an
        # honest footer — nothing silently dropped from the card.
        card = _flush(state, _degen())
        tier_mints = ({d["mint"] for d in card["items_safe"]}
                      | {d["mint"] for d in card["items_fresh"]})
        self.assertTrue(tier_mints <= queued_mints)
        self.assertEqual(len(tier_mints), 30)
        self.assertIn("showing 30 of 40",
                      tg_alerts._launch_early_digest(card))

    def test_digestfull_bounded_newest_200(self):
        state = {"early_digest": _queue(250)}
        dump = self._digestfull_dump(state)
        self.assertIn("250 waiting", dump)
        self.assertIn("CA: MINT249", dump)   # newest shown
        self.assertIn("CA: MINT050", dump)   # 200th newest shown
        self.assertNotIn("CA: MINT049", dump)  # older cut off
        self.assertIn("…and 50 older", dump)

    def test_digestfull_chunks_big_queues(self):
        # The newest-200 slice is split across Telegram-safe messages;
        # every queued line stays retrievable, none truncated away.
        state = {"early_digest": _queue(250)}
        sent = self._digestfull_text(state)
        self.assertGreater(len(sent), 1)
        for msg in sent:
            self.assertLessEqual(len(msg), 3900)
        for i, msg in enumerate(sent[1:], start=2):
            self.assertIn("🗞 (cont.)", msg)
        dump = "\n".join(sent)
        self.assertIn("CA: MINT249", dump)
        self.assertIn("CA: MINT050", dump)

    def test_digestfull_small_queue_single_message(self):
        state = {"early_digest": _queue(10)}
        sent = self._digestfull_text(state)
        self.assertEqual(len(sent), 1)
        self.assertIn("10 waiting", sent[0])

    def test_digestfull_line_format(self):
        now = time.time()
        state = {"early_digest": [
            {"mint": "ABC", "symbol": "APE", "safety_score": 72.0,
             "ts": now - 12 * 60},
            {"mint": "DEF", "symbol": "DUD", "safety_score": None,
             "ts": now - 3 * 60},
        ]}
        dump = self._digestfull_dump(state)
        self.assertIn("• APE [score 72.0/100] +12min CA: ABC", dump)
        self.assertIn("• DUD [score n/a] +3min CA: DEF", dump)

    def test_digestfull_empty_queue(self):
        dump = self._digestfull_dump({})
        self.assertIn("digest queue is empty right now", dump)

    def _digestfull_text(self, state):
        sent = []
        sec = tg_security.SecurityContext({})
        ctx = tg_commands.HandlerContext(
            send=lambda chat_id, text, markup: sent.append(text),
            security=sec,
            tools=object(),
            publish=lambda e, sev=None: None,
            audit_log=[],
        )
        with patch("daemon.common.get_daemon_state",
                   return_value=state), \
             patch("daemon.common.get_store", return_value=object()):
            tg_commands._cmd_digestfull(ctx, 123, 456, 1, [])
        self.assertTrue(sent)
        return sent

    def _digestfull_dump(self, state):
        return "\n".join(self._digestfull_text(state))


class CapCadenceTests(unittest.TestCase):
    def test_flush_cadence_unchanged(self):
        # Window not expired -> no flush, queue untouched.
        state = {"early_digest": _queue(3)}
        state["early_window"] = {"start": time.time(), "count": 0}
        sc = LaunchScanner.__new__(LaunchScanner)
        cfg = SimpleNamespace(degen=_degen())
        with patch.object(event_bus, "publish") as pub:
            sc._flush_early_digest(cfg, state)
        pub.assert_not_called()
        self.assertEqual(len(state["early_digest"]), 3)
        # Empty queue -> no-op.
        state["early_digest"] = []
        with patch.object(event_bus, "publish") as pub:
            sc._flush_early_digest(cfg, state)
        pub.assert_not_called()

    def test_instant_cap_still_queues_overflow(self):
        # The 4/hour instant cap is untouched: overflow still lands in
        # the queue (verified via _early_allowed, not the full pipeline).
        degen = _degen(early_alert_max_per_window=4,
                       early_alert_window_seconds=3600.0)
        state = {}
        now = time.time()
        allowed = [LaunchScanner._early_allowed(degen, state, now)
                   for _ in range(6)]
        self.assertEqual(allowed, [True] * 4 + [False] * 2)


class DigestPauseSwitchTests(unittest.TestCase):
    """early_digest_enabled pause switch (user decision 2026-10-02)."""

    def _flush_quiet(self, state, degen):
        sc = LaunchScanner.__new__(LaunchScanner)
        cfg = SimpleNamespace(degen=degen)
        state["early_window"] = {"start": time.time() - 301.0, "count": 0}
        published = []
        with patch.object(event_bus, "publish",
                          side_effect=published.append):
            sc._flush_early_digest(cfg, state)
        return published

    def test_disabled_drops_queue_silently(self):
        state = {"early_digest": _queue(5)}
        published = self._flush_quiet(
            state, _degen(early_digest_enabled=False))
        self.assertEqual(published, [])
        self.assertEqual(state["early_digest"], [])

    def test_enabled_still_publishes(self):
        state = {"early_digest": _queue(5)}
        published = self._flush_quiet(
            state, _degen(early_digest_enabled=True))
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["event"], "launch.early_digest")

    def test_missing_knob_keeps_historical_behavior(self):
        # Code default True: callers without the knob (older tests,
        # ad-hoc scripts) keep the historical publish behavior.
        state = {"early_digest": _queue(5)}
        published = self._flush_quiet(state, _degen())
        self.assertEqual(len(published), 1)

    def test_empty_queue_noop_when_disabled(self):
        state = {"early_digest": []}
        published = self._flush_quiet(
            state, _degen(early_digest_enabled=False))
        self.assertEqual(published, [])

    def test_config_declares_knob(self):
        from config.config import DegenConfig
        import dataclasses
        names = {f.name for f in dataclasses.fields(DegenConfig)}
        self.assertIn("early_digest_enabled", names)

    def test_yaml_default_is_paused(self):
        import yaml
        with open("config/defaults.yaml") as fh:
            y = yaml.safe_load(fh)
        self.assertIs((y.get("degen") or {}).get(
            "early_digest_enabled"), False)


if __name__ == "__main__":
    unittest.main()
