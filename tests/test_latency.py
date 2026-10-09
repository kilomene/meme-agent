"""Tests for the entry-timing (detection latency) measurement feature.

Covers:
  * latency computation on the instant-card path
  * the digest path counting as a first card (incl. idempotency vs an
    earlier instant card)
  * backfill for pre-deploy coins (no first_seen_ts -> unknown, no crash)
  * rolling median/p90 math + window bounding
  * /latency output formatting

Measurement only: nothing here trades, gates, or touches the broker.
"""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from daemon.launch_scanner import latency
from daemon.telegram import commands as tg_commands
from daemon.telegram import security as tg_security


def _degen(**kw):
    d = {"latency_enabled": True, "latency_window_coins": 100}
    d.update(kw)
    return SimpleNamespace(**d)


T0 = 1_700_000_000.0


class RecordFirstSeenTests(unittest.TestCase):
    def test_first_seen_idempotent(self):
        state = {}
        d = _degen()
        self.assertTrue(latency.record_first_seen(state, d, "MINT1", T0))
        # A later sighting must NOT move the baseline.
        self.assertTrue(latency.record_first_seen(state, d, "MINT1", T0 + 999))
        self.assertEqual(state["latency_coins"]["MINT1"]["first_seen_ts"], T0)

    def test_first_seen_empty_mint_rejected(self):
        state = {}
        self.assertFalse(latency.record_first_seen(state, _degen(), "", T0))
        self.assertFalse(latency.record_first_seen(state, _degen(), None, T0))
        self.assertFalse(state.get("latency_coins"))

    def test_disabled_is_noop(self):
        state = {}
        d = _degen(latency_enabled=False)
        self.assertFalse(latency.record_first_seen(state, d, "MINT1", T0))
        self.assertIsNone(latency.record_first_card(state, d, "MINT1", T0))
        self.assertNotIn("latency_coins", state)


class InstantPathTests(unittest.TestCase):
    def test_latency_computation_instant_path(self):
        state = {}
        d = _degen()
        latency.record_first_seen(state, d, "MINT1", T0, symbol="AAA")
        got = latency.record_first_card(state, d, "MINT1", T0 + 120.0,
                                        symbol="AAA", source="launch.early")
        self.assertAlmostEqual(got, 2.0)
        rec = state["latency_coins"]["MINT1"]
        self.assertEqual(rec["first_seen_ts"], T0)
        self.assertEqual(rec["first_card_ts"], T0 + 120.0)
        self.assertAlmostEqual(rec["latency_min"], 2.0)
        self.assertEqual(rec["source"], "launch.early")

    def test_first_card_wins_idempotent(self):
        state = {}
        d = _degen()
        latency.record_first_seen(state, d, "MINT1", T0)
        first = latency.record_first_card(state, d, "MINT1", T0 + 60.0,
                                          source="launch.early")
        # A later card (confirmed runner, digest, ...) keeps the first.
        second = latency.record_first_card(state, d, "MINT1", T0 + 600.0,
                                           source="launch.detected")
        self.assertAlmostEqual(first, 1.0)
        self.assertAlmostEqual(second, 1.0)
        rec = state["latency_coins"]["MINT1"]
        self.assertEqual(rec["first_card_ts"], T0 + 60.0)
        self.assertEqual(rec["source"], "launch.early")

    def test_momentum_alert_on_first_sight(self):
        # Momentum alerts fire on first sighting: first-seen == first-card.
        state = {}
        d = _degen()
        latency.record_first_seen(state, d, "MINT9", T0, symbol="MMM")
        got = latency.record_first_card(state, d, "MINT9", T0, symbol="MMM",
                                        source="momentum.alert",
                                        assume_seen=True)
        self.assertAlmostEqual(got, 0.0)


class DigestPathTests(unittest.TestCase):
    def test_digest_path_first_card_counted(self):
        # Seen at T0, no instant card (spam guard), digest flushes at
        # T0+300 -> the digest item IS the first card.
        state = {}
        d = _degen()
        latency.record_first_seen(state, d, "MINT2", T0, symbol="BBB")
        got = latency.record_first_card(state, d, "MINT2", T0 + 300.0,
                                        symbol="BBB", source="digest")
        self.assertAlmostEqual(got, 5.0)
        self.assertEqual(latency.latency_for(state, "MINT2"), 5.0)

    def test_digest_does_not_beat_earlier_instant_card(self):
        state = {}
        d = _degen()
        latency.record_first_seen(state, d, "MINT3", T0)
        instant = latency.record_first_card(state, d, "MINT3", T0 + 30.0,
                                            source="launch.early")
        digest = latency.record_first_card(state, d, "MINT3", T0 + 300.0,
                                           source="digest")
        self.assertAlmostEqual(instant, 0.5)
        self.assertAlmostEqual(digest, 0.5)  # instant keeps its time

    def test_digest_beats_later_confirmed_card(self):
        state = {}
        d = _degen()
        latency.record_first_seen(state, d, "MINT4", T0)
        # Digest flushes first...
        dgot = latency.record_first_card(state, d, "MINT4", T0 + 300.0,
                                         source="digest")
        # ...then the confirmed card arrives: digest time stands.
        cgot = latency.record_first_card(state, d, "MINT4", T0 + 900.0,
                                         source="launch.detected")
        self.assertAlmostEqual(dgot, 5.0)
        self.assertAlmostEqual(cgot, 5.0)


class BackfillTests(unittest.TestCase):
    def test_no_first_seen_gives_unknown_not_crash(self):
        # Coin carded before this module deployed: no first_seen_ts ->
        # latency recorded as unknown (None), never a fake zero.
        state = {}
        d = _degen()
        got = latency.record_first_card(state, d, "OLDMINT", T0 + 500.0,
                                        symbol="OLD", source="launch.early")
        self.assertIsNone(got)
        rec = state["latency_coins"]["OLDMINT"]
        self.assertIsNone(rec["first_seen_ts"])
        self.assertIsNone(rec["latency_min"])
        self.assertEqual(rec["first_card_ts"], T0 + 500.0)
        # Unknown cards never enter the rolling window.
        self.assertFalse(state.get("latency_window"))
        # A repeated card is still idempotent and unknown.
        again = latency.record_first_card(state, d, "OLDMINT", T0 + 900.0,
                                          source="digest")
        self.assertIsNone(again)

    def test_summary_counts_unknown_separately(self):
        state = {}
        d = _degen()
        latency.record_first_card(state, d, "OLD1", T0, source="digest")
        latency.record_first_seen(state, d, "NEW1", T0)
        latency.record_first_card(state, d, "NEW1", T0 + 60.0,
                                  source="launch.early")
        summ = latency.summary(state)
        self.assertEqual(summ["n"], 1)
        self.assertEqual(summ["unknown"], 1)
        self.assertAlmostEqual(summ["median_min"], 1.0)


class RollingWindowTests(unittest.TestCase):
    def _feed(self, state, d, latencies, t0=T0):
        for i, lat in enumerate(latencies):
            mint = "W%d" % i
            latency.record_first_seen(state, d, mint, t0)
            latency.record_first_card(state, d, mint, t0 + lat * 60.0,
                                      source="launch.early")

    def test_median_p90_math(self):
        state = {}
        d = _degen(latency_window_coins=100)
        self._feed(state, d, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        summ = latency.summary(state)
        self.assertEqual(summ["n"], 10)
        self.assertAlmostEqual(summ["median_min"], 5.5)
        # nearest-rank p90: ceil(0.9 * 10) = 9th value -> 9.0
        self.assertAlmostEqual(summ["p90_min"], 9.0)
        # Slowest 3, descending.
        slowest = summ["slowest"]
        self.assertEqual([s["latency_min"] for s in slowest], [10.0, 9.0, 8.0])

    def test_window_bounded_to_knob(self):
        state = {}
        d = _degen(latency_window_coins=3)
        self._feed(state, d, [1, 2, 3, 4, 5])
        window = state["latency_window"]
        self.assertEqual(len(window), 3)
        self.assertEqual([w["latency_min"] for w in window],
                         [3.0, 4.0, 5.0])
        summ = latency.summary(state)
        self.assertAlmostEqual(summ["median_min"], 4.0)

    def test_funnel_bits(self):
        state = {}
        d = _degen()
        self.assertEqual(latency.funnel_bits(state), "")
        self._feed(state, d, [0.4, 2.1, 9.9])
        bits = latency.funnel_bits(state)
        self.assertIn("lat_n=3", bits)
        self.assertIn("med=2.1m", bits)
        self.assertIn("p90=9.9m", bits)


class LatencyForTests(unittest.TestCase):
    def test_latency_for(self):
        state = {}
        d = _degen()
        self.assertIsNone(latency.latency_for(state, "NOPE"))
        self.assertIsNone(latency.latency_for({}, "NOPE"))
        latency.record_first_seen(state, d, "M1", T0)
        self.assertIsNone(latency.latency_for(state, "M1"))  # seen, not carded
        latency.record_first_card(state, d, "M1", T0 + 180.0)
        self.assertAlmostEqual(latency.latency_for(state, "M1"), 3.0)


# ----------------------------------------------------------------------
# /latency command
# ----------------------------------------------------------------------
def _make_ctx(state):
    sent = []
    sec = tg_security.SecurityContext({})
    ctx = tg_commands.HandlerContext(
        send=lambda chat_id, text, markup: sent.append((chat_id, text)),
        security=sec,
        tools=object(),
        publish=lambda e, sev=None: None,
        audit_log=[],
    )
    return ctx, sent


class CmdLatencyTests(unittest.TestCase):
    def _run(self, state):
        ctx, sent = _make_ctx(state)
        with patch("daemon.common.get_daemon_state", return_value=state), \
             patch("daemon.common.get_store", return_value=object()):
            tg_commands._cmd_latency(ctx, 123, 456, 1, [])
        self.assertEqual(len(sent), 1)
        return sent[0][1]

    def test_empty_state(self):
        text = self._run({})
        self.assertIn("No carded coins tracked yet", text)

    def test_output_format(self):
        state = {}
        d = _degen()
        latency.record_first_seen(state, d, "AAA", T0)
        latency.record_first_card(state, d, "AAA", T0 + 24.0,
                                  symbol="AAA", source="launch.early")
        latency.record_first_seen(state, d, "BBB", T0)
        latency.record_first_card(state, d, "BBB", T0 + 600.0,
                                  symbol="BBB", source="digest")
        latency.record_first_seen(state, d, "CCC", T0)
        latency.record_first_card(state, d, "CCC", T0 + 120.0,
                                  symbol="CCC", source="momentum.alert")
        text = self._run(state)
        self.assertIn("⏱", text)
        self.assertIn("last 3 carded", text)
        self.assertIn("median 2.0m", text)   # median of [0.4, 2.0, 10.0]
        self.assertIn("p90 10.0m", text)     # nearest-rank ceil(0.9*3)=3
        self.assertIn("Slowest recent:", text)
        # 3 slowest examples with coin + latency.
        self.assertIn("BBB", text)
        self.assertIn("+10.0m", text)
        self.assertIn("(digest)", text)
        self.assertIn("AAA", text)
        self.assertIn("+24s", text)         # sub-minute -> seconds
        self.assertIn("Measurement only", text)

    def test_unknown_only_state(self):
        state = {}
        d = _degen()
        latency.record_first_card(state, d, "OLD", T0, symbol="OLD",
                                  source="launch.early")
        text = self._run(state)
        self.assertIn("unknown first sighting", text)
        self.assertNotIn("median", text)


if __name__ == "__main__":
    unittest.main()
