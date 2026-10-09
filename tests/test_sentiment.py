"""Tests for the social-sentiment tracker (daemon/launch_scanner/sentiment.py).

The tracker is a soft-boost scorer only — these tests pin the verdict
boundaries, the anti-fake floors (300s window, velocity flooring), and
the state bounds. Nothing here asserts allow/block behaviour because
the module must not have any.
"""

import os
import sys
import time
import unittest
from unittest import mock
from urllib.error import URLError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daemon.launch_scanner import sentiment


def _profile(websites=(), socials=()):
    return {"websites": list(websites),
            "socials": [{"type": t, "url": u} for t, u in socials]}


def _tracker():
    return sentiment.SentimentTracker()


class TrackerBasicsTest(unittest.TestCase):
    def test_single_observation_returns_none(self):
        t = _tracker()
        self.assertIsNone(t.observe("MINT", _profile(), 1000.0))
        self.assertIn("MINT", t._series)

    def test_observe_none_profile_returns_none(self):
        t = _tracker()
        self.assertIsNone(t.observe("MINT", None, 1000.0))
        self.assertNotIn("MINT", t._series)

    def test_disabled_tracker_returns_none(self):
        t = sentiment.SentimentTracker(cfg=type("C", (), {
            "sentiment_enabled": False})())
        self.assertIsNone(t.observe("MINT", _profile(), 1000.0))
        self.assertNotIn("MINT", t._series)

    def test_zero_socials_never_hot(self):
        t = _tracker()
        t.observe("MINT", _profile(), 1000.0)
        r = t.observe("MINT", _profile(websites=["https://x.io"]), 5000.0)
        self.assertEqual(r["score"], 0.0)
        self.assertEqual(r["verdict"], "NEUTRAL")
        self.assertEqual(r["signals"], ["has_website", "new_website"])
        self.assertEqual(r["velocity"], 0.0)


class VerdictBoundaryTest(unittest.TestCase):
    def _observe_pair(self, t, first, last, dt_s=3600.0):
        t.observe("MINT", first, 1000.0)
        return t.observe("MINT", last, 1000.0 + dt_s)

    def test_hot_boundary(self):
        # base 20 + 3 new socials (45) + 4 distinct types (10) = 75
        t = _tracker()
        first = _profile(socials=[("discord", "https://d.gg/a")])
        last = _profile(
            websites=["https://coin.io"],
            socials=[("discord", "https://d.gg/a"),
                     ("twitter", "https://x.com/c"),
                     ("telegram", "https://t.me/c"),
                     ("reddit", "https://r.io/c")])
        r = self._observe_pair(t, first, last)
        self.assertEqual(r["score"], 75.0)
        self.assertEqual(r["verdict"], "HOT")
        self.assertEqual(r["new_socials"], 3)
        self.assertEqual(r["distinct_types"], 4)
        for sig in ("has_website", "has_twitter", "has_telegram",
                    "new_website", "new_twitter", "new_telegram",
                    "social_count_up"):
            self.assertIn(sig, r["signals"])

    def test_warm_boundary(self):
        # base 20 + 2 new socials (30) = 50 -> WARM
        t = _tracker()
        first = _profile()
        last = _profile(socials=[("twitter", "https://x.com/c"),
                                 ("telegram", "https://t.me/c")])
        r = self._observe_pair(t, first, last)
        self.assertEqual(r["score"], 50.0)
        self.assertEqual(r["verdict"], "WARM")

    def test_low_score_is_neutral(self):
        # base 20 only, nothing new -> NEUTRAL
        t = _tracker()
        p = _profile(socials=[("twitter", "https://x.com/c")])
        r = self._observe_pair(t, p, p)
        self.assertEqual(r["score"], 20.0)
        self.assertEqual(r["verdict"], "NEUTRAL")
        self.assertIn("has_twitter", r["signals"])

    def test_score_never_exceeds_100(self):
        t = _tracker()
        first = _profile(socials=[("discord", "https://d.gg/a")])
        last = _profile(
            websites=["https://coin.io"],
            socials=[("discord", "https://d.gg/a")] +
                    [("s%d" % i, "https://u%d.io" % i) for i in range(10)])
        r = self._observe_pair(t, first, last)
        self.assertLessEqual(r["score"], 100.0)
        # 15-per-new is capped at +45 even with 10 new socials
        self.assertEqual(r["score"], 75.0)


class AntiFakeFloorTest(unittest.TestCase):
    def test_short_window_forces_neutral(self):
        # HOT-looking profile, but only 60s between polls -> NEUTRAL
        t = _tracker()
        t.observe("MINT", _profile(), 1000.0)
        r = t.observe("MINT", _profile(
            websites=["https://coin.io"],
            socials=[("twitter", "https://x.com/c"),
                     ("telegram", "https://t.me/c"),
                     ("discord", "https://d.gg/c")]), 1060.0)
        self.assertEqual(r["score"], 75.0)  # raw score still computed
        self.assertEqual(r["verdict"], "NEUTRAL")  # but window too small
        self.assertEqual(r["velocity"], 180.0)  # 3 per 60s

    def test_velocity_floored_without_new_socials(self):
        t = _tracker()
        p = _profile(socials=[("twitter", "https://x.com/c")])
        t.observe("MINT", p, 1000.0)
        r = t.observe("MINT", p, 5000.0)
        self.assertEqual(r["velocity"], 0.0)
        self.assertEqual(r["new_socials"], 0)

    def test_velocity_per_hour(self):
        t = _tracker()
        t.observe("MINT", _profile(), 1000.0)
        r = t.observe("MINT", _profile(
            socials=[("twitter", "https://x.com/c"),
                     ("telegram", "https://t.me/c")]), 4600.0)
        self.assertEqual(r["new_socials"], 2)
        self.assertEqual(r["velocity"], 2.0)
        self.assertEqual(r["window_s"], 3600.0)


class StateBoundsTest(unittest.TestCase):
    def test_prune_drops_old(self):
        t = _tracker()
        t.observe("OLD", _profile(), 1000.0)
        t.observe("NEW", _profile(), 90000.0)
        t.prune(100000.0, max_age_s=86400.0)
        self.assertNotIn("OLD", t._series)
        self.assertIn("NEW", t._series)

    def test_eviction_bound_respected(self):
        t = _tracker()
        for i in range(2005):
            t.observe("MINT%05d" % i, _profile(), 1000.0 + i)
        self.assertLessEqual(len(t._series), 2000)
        self.assertNotIn("MINT00000", t._series)  # oldest evicted
        self.assertIn("MINT02004", t._series)     # newest kept


class FetchProfileTest(unittest.TestCase):
    def setUp(self):
        sentiment._last_call_ts = 0.0  # no cross-test rate-limit sleeps

    def _resp(self, payload):
        m = mock.MagicMock()
        m.read.return_value = payload.encode()
        m.__enter__.return_value = m
        m.__exit__.return_value = False
        return m

    def test_returns_normalized_dict(self):
        payload = ('[{"chainId": "solana", "info": {'
                   '"websites": [{"label": "Website", "url": "https://coin.io"}],'
                   '"socials": [{"type": "twitter", "url": "https://x.com/c"},'
                   ' {"type": "telegram", "url": "https://t.me/c"}]}}]')
        with mock.patch("urllib.request.urlopen",
                        return_value=self._resp(payload)) as uo:
            r = sentiment.fetch_token_profile("solana", "MINT")
        self.assertEqual(r, {
            "websites": ["https://coin.io"],
            "socials": [{"type": "twitter", "url": "https://x.com/c"},
                        {"type": "telegram", "url": "https://t.me/c"}],
        })
        self.assertEqual(uo.call_count, 1)  # one call only, no boost call
        url = uo.call_args[0][0].full_url
        self.assertIn("/solana/MINT", url)

    def test_chain_mapping(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=self._resp("[]")) as uo:
            self.assertIsNone(
                sentiment.fetch_token_profile("ethereum", "0xABC"))
        url = uo.call_args[0][0].full_url
        self.assertIn("tokens/v1/eth/0xABC", url)

    def test_none_on_http_error(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=URLError("down")):
            self.assertIsNone(
                sentiment.fetch_token_profile("solana", "MINT"))

    def test_none_on_parse_error(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=self._resp("not json{{")):
            self.assertIsNone(
                sentiment.fetch_token_profile("solana", "MINT"))

    def test_none_on_empty_pairs(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=self._resp("[]")):
            self.assertIsNone(
                sentiment.fetch_token_profile("solana", "MINT"))

    def test_bad_chain_and_empty_mint_are_none(self):
        with mock.patch("urllib.request.urlopen") as uo:
            self.assertIsNone(sentiment.fetch_token_profile("doge", "MINT"))
            self.assertIsNone(sentiment.fetch_token_profile("solana", ""))
        uo.assert_not_called()

    def test_rate_limit_gap_between_calls(self):
        sentiment._last_call_ts = time.monotonic()
        with mock.patch("urllib.request.urlopen",
                        return_value=self._resp("[]")):
            start = time.monotonic()
            sentiment.fetch_token_profile("solana", "MINT")
            elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.9)


if __name__ == "__main__":
    unittest.main()
