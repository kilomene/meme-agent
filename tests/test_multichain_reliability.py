"""Multi-chain reliability: the Base/EVM "switch" is the data feed.

Regression tests for the 2026-09-23 find that DexScreener's multi-chain
feed (boosts / token stats / batch stats — the source of every non-Solana
candidate) was silently dying on this VM: urllib's keep-alive connection
reuse gets truncated bodies (http.client.IncompleteRead) through the egress
path, while close-per-request works. Every urllib call site must therefore
send ``Connection: close``.

Also covers the _consider re-qualification fix: once a mint is a live
candidate the judge owns it — re-running _qualify every scan burned one
DexScreener HTTP call per boost candidate per scan (self-inflicted rate
limiting on top of the keep-alive failures).
"""

import io
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from daemon.launch_scanner import LaunchScanner, lowcap, safety, sources


def _degen(**kw):
    base = dict(enabled=True, poll_interval_seconds=30, entry_sol=0.05,
                max_positions=5, min_usd_market_cap=10000.0,
                max_age_minutes=10.0, velocity_multiplier=3.0,
                velocity_window_minutes=10.0, stop_loss_pct=30.0,
                take_profit_pct=200.0, max_hold_hours=4.0, auto_enter=True,
                early_safety_flash=True, early_flash_timeout_s=4.0,
                early_min_score=30.0,
                early_alert_max_per_window=10,
                early_alert_window_seconds=300.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _config(**kw):
    return SimpleNamespace(degen=_degen(**kw))


class ConnectionCloseHeaderTest(unittest.TestCase):
    """Every urllib call site must send Connection: close."""

    def test_sources_ua_has_connection_close(self):
        self.assertEqual(sources._UA.get("Connection"), "close")

    def test_safety_get_json_sends_connection_close(self):
        captured = {}

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({}).encode()

        import urllib.request
        orig = urllib.request.urlopen

        def fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.header_items())
            return FakeResp()

        with patch.object(urllib.request, "urlopen", fake_urlopen):
            safety._get_json("https://example.invalid/x")
        lowered = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(lowered.get("connection"), "close")

    def test_safety_check_token_evm_uses_close_header(self):
        # _check_evm goes through the same _get_json; force the urllib
        # fallback path (curl-first is covered by CurlFirstFetchTest).
        captured = {}

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return json.dumps({"result": {}}).encode()

        import urllib.request

        def fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.header_items())
            return FakeResp()

        with patch.object(safety.shutil, "which", return_value=None), \
             patch.object(urllib.request, "urlopen", fake_urlopen):
            safety.check_token("base", "0xabc", timeout=2)
        lowered = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(lowered.get("connection"), "close")

    def test_logstream_rpc_sends_connection_close(self):
        from daemon.launch_scanner import logstream
        captured = {}

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return json.dumps({"result": []}).encode()

        import urllib.request

        def fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.header_items())
            return FakeResp()

        ls = logstream.LogStream.__new__(logstream.LogStream)
        ls._http_rpc_urls = ["https://example.invalid/rpc"]
        ls._rpc_health = {"https://example.invalid/rpc": 1.0}
        ls._rpc_active = "https://example.invalid/rpc"
        with patch.object(urllib.request, "urlopen", fake_urlopen):
            ls._http_rpc("getSlot", [])
        lowered = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(lowered.get("connection"), "close")


class ConsiderSkipRequalifyTest(unittest.TestCase):
    """_consider must not re-run _qualify for already-tracked candidates."""

    def setUp(self):
        self.sc = LaunchScanner.__new__(LaunchScanner)

    def _state_with_candidate(self, key="MINT1"):
        cand = {"key": key, "mint": "MINT1", "symbol": "TKN",
                "chain": "solana", "source": "dexscreener-boost",
                "first_seen": time.time(), "rising_streak": 0,
                "stats": None, "stats_ts": 0.0}
        return {"seen": {key: {"first_ts": time.time(), "mcap": [],
                               "alerted": False, "symbol": "TKN"}},
                "candidates": {key: cand}}

    def test_tracked_candidate_skips_qualify(self):
        c = {"mint": "MINT1", "symbol": "TKN", "chain": "solana",
             "source": "dexscreener-boost", "usd_market_cap": 60000.0}
        state = self._state_with_candidate()
        with patch.object(LaunchScanner, "_qualify",
                          side_effect=AssertionError("must not re-qualify")):
            self.sc._consider(_config(), None, state, c)
        # candidate untouched, still tracked exactly once
        self.assertIn("MINT1", state["candidates"])

    def test_new_candidate_still_qualifies(self):
        c = {"mint": "NEWMINT", "symbol": "NEW", "chain": "base",
             "source": "dexscreener-boost", "usd_market_cap": 0.0,
             "extra": {}, "url": ""}
        state = {"seen": {}, "candidates": {}}
        stats = {"symbol": "NEW", "usd_liquidity": 50000.0,
                 "usd_market_cap": 80000.0}
        cfg = _config(sniper_tier_threshold=0.0)
        with patch.object(LaunchScanner, "_qualify", return_value="boost") \
                as q, \
             patch("agent.events.publish") as pub:
            with patch.object(safety, "check_token",
                              return_value={"passed": True, "score": 100,
                                            "flags": [], "fails": []}):
                self.sc._consider(cfg, None, state, c)
                self.sc._flush_sniper_pending(cfg, state)
        q.assert_called_once()
        self.assertIn("NEWMINT", state["candidates"])
        # base chain carried through to the instant alert
        evt = pub.call_args[0][0]
        self.assertEqual(evt["event"], "launch.early")
        self.assertEqual(evt["chain"], "base")
        self.assertEqual(evt["mint"], "NEWMINT")


class BaseInstantVerdictTest(unittest.TestCase):
    """A clean Base coin must pass the instant quality gate (the user's
    'drop CA on base too' requirement) — and a dirty one must be held."""

    def setUp(self):
        self.sc = LaunchScanner.__new__(LaunchScanner)

    def _coin(self, chain="base"):
        return {"mint": "0xBaseCoin", "symbol": "BSE", "name": "Base Coin",
                "chain": chain, "source": "dexscreener-boost",
                "usd_market_cap": 120000.0}

    def test_clean_base_coin_passes(self):
        report = {"passed": True, "score": 100, "flags": [],
                  "fails": [], "unknown": False}
        with patch.object(safety, "check_token", return_value=report):
            allow, reason, rep = self.sc._instant_verdict(
                _degen(), self._coin())
        self.assertTrue(allow)
        self.assertIn("100", reason)

    def test_honeypot_base_coin_held(self):
        report = {"passed": False, "score": 0, "flags": [],
                  "fails": ["honeypot"], "unknown": False}
        with patch.object(safety, "check_token", return_value=report):
            allow, reason, rep = self.sc._instant_verdict(
                _degen(), self._coin())
        self.assertFalse(allow)
        self.assertIn("honeypot", reason)

    def test_unknown_safety_held(self):
        report = {"passed": True, "score": 0, "flags": [],
                  "fails": [], "unknown": True}
        with patch.object(safety, "check_token", return_value=report):
            allow, reason, rep = self.sc._instant_verdict(
                _degen(), self._coin("robinhood"))
        self.assertFalse(allow)
        self.assertEqual(reason, "safety-unknown")


class CurlFirstFetchTest(unittest.TestCase):
    """curl-first fetching (VM egress quirk): curl when available, urllib
    fallback otherwise. Never raises."""

    def _curl_ok(self, payload=b'{"a": 1}'):
        m = unittest.mock.MagicMock()
        m.returncode = 0
        m.stdout = payload
        m.stderr = b""
        return m

    def test_get_json_prefers_curl(self):
        import urllib.request
        with patch.object(sources.shutil, "which", return_value="/usr/bin/curl"), \
             patch.object(sources.subprocess, "run",
                          return_value=self._curl_ok()) as run, \
             patch.object(urllib.request, "urlopen",
                          side_effect=AssertionError("urllib must not run")):
            self.assertEqual(sources._get_json("https://example.invalid/x"),
                             {"a": 1})
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "/usr/bin/curl")
        self.assertIn("https://example.invalid/x", cmd)
        # no shell ever
        self.assertIsInstance(cmd, list)

    def test_get_json_falls_back_to_urllib_when_curl_fails(self):
        import urllib.request

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"b": 2}'

        bad = unittest.mock.MagicMock()
        bad.returncode = 7
        bad.stdout = b""
        bad.stderr = b"connect failed"
        with patch.object(sources.shutil, "which", return_value="/usr/bin/curl"), \
             patch.object(sources.subprocess, "run", return_value=bad), \
             patch.object(urllib.request, "urlopen", return_value=FakeResp()):
            self.assertEqual(sources._get_json("https://example.invalid/x"),
                             {"b": 2})

    def test_get_json_without_curl_uses_urllib(self):
        import urllib.request

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"c": 3}'

        with patch.object(sources.shutil, "which", return_value=None), \
             patch.object(sources.subprocess, "run",
                          side_effect=AssertionError("curl must not run")), \
             patch.object(urllib.request, "urlopen", return_value=FakeResp()):
            self.assertEqual(sources._get_json("https://example.invalid/x"),
                             {"c": 3})

    def test_post_json_uses_curl_post(self):
        with patch.object(sources.shutil, "which", return_value="/usr/bin/curl"), \
             patch.object(sources.subprocess, "run",
                          return_value=self._curl_ok(b'{"ok": true}')) as run:
            self.assertEqual(
                sources._post_json("https://example.invalid/x", {"q": 1}),
                {"ok": True})
        cmd = run.call_args[0][0]
        self.assertIn("--data-binary", cmd)
        self.assertIn("Content-Type: application/json",
                      " ".join(cmd))

    def test_safety_curl_json_parses(self):
        with patch.object(safety.shutil, "which", return_value="/usr/bin/curl"), \
             patch.object(safety.subprocess, "run",
                          return_value=self._curl_ok(b'{"result": {}}')):
            self.assertEqual(
                safety._curl_json("https://example.invalid/x", 5.0),
                {"result": {}})

    def test_safety_get_json_falls_back_to_urllib(self):
        import urllib.request

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"result": {}}'

        with patch.object(safety.shutil, "which", return_value=None), \
             patch.object(urllib.request, "urlopen", return_value=FakeResp()):
            self.assertEqual(
                safety._get_json("https://example.invalid/x"), {"result": {}})


class LowcapHunterReuseTest(unittest.TestCase):
    def test_hunter_stores_cfg(self):
        cfg = _degen()
        h = lowcap.LowcapHunter(cfg)
        self.assertIs(h.cfg, cfg)


class OnePointSafetyBarTest(unittest.TestCase):
    """The user's STANDING rule (permanent decision 2026-09-27): EVERY coin
    dropped to the bot must note its safety score, and the bar is 1/100
    — no CA below it, at either the instant or the confirmed stage."""

    def setUp(self):
        self.sc = LaunchScanner.__new__(LaunchScanner)

    def test_config_default_is_one(self):
        import yaml
        from config.config import DegenConfig
        with open("config/defaults.yaml") as fh:
            y = yaml.safe_load(fh)
        self.assertEqual(y["degen"]["early_min_score"], 1.0)
        self.assertEqual(DegenConfig().early_min_score, 1.0)

    def _instant(self, score, **kw):
        d = _degen(early_min_score=1.0, **kw)
        coin = {"mint": "0xB70", "symbol": "B70", "name": "Bar",
                "chain": "base", "source": "dexscreener-boost",
                "usd_market_cap": 120000.0}
        report = {"passed": True, "score": score, "flags": [],
                  "fails": [], "unknown": False}
        with patch.object(safety, "check_token", return_value=report):
            return self.sc._instant_verdict(d, coin)

    def test_instant_allows_1(self):
        allow, reason, _ = self._instant(1)
        self.assertTrue(allow)
        self.assertIn("1", reason)

    def test_instant_allows_100(self):
        allow, _, _ = self._instant(100)
        self.assertTrue(allow)

    def test_instant_holds_0(self):
        allow, reason, _ = self._instant(0)
        self.assertFalse(allow)
        self.assertIn("0 < 1", reason)

    def _confirm(self, score, unknown=False, passed=True, fail_closed=False):
        d = _degen(early_min_score=1.0, safety_fail_closed=fail_closed)
        cand = {"mint": "0xB70", "symbol": "B70", "chain": "base"}
        report = {"passed": passed, "score": score, "flags": [],
                  "fails": [] if passed else ["honeypot"],
                  "unknown": unknown}
        with patch.object(safety, "check_token", return_value=report):
            return self.sc._confirm_safety_ok(d, cand)

    def test_confirm_allows_1(self):
        ok, _, _ = self._confirm(1)
        self.assertTrue(ok)

    def test_confirm_blocks_0(self):
        ok, reason, _ = self._confirm(0)
        self.assertFalse(ok)
        self.assertIn("0 < 1", reason)

    def test_confirm_blocks_zero_score(self):
        ok, _, _ = self._confirm(0)
        self.assertFalse(ok)

    def test_confirm_unknown_blocks_without_score(self):
        # Standing 1/100 rule (permanent 2026-09-27): a coin with no real
        # safety report has NO score to note on the card, so it can never
        # satisfy the minimum — unknown always blocks confirmation,
        # regardless of safety_fail_closed.
        ok, reason, _ = self._confirm(0, unknown=True, fail_closed=False)
        self.assertFalse(ok)
        self.assertIn("safety-unknown", reason)

    def test_confirm_unknown_fail_closed_blocks(self):
        ok, _, _ = self._confirm(0, unknown=True, fail_closed=True)
        self.assertFalse(ok)

    def test_confirm_hard_fail_blocks(self):
        ok, reason, _ = self._confirm(100, passed=False)
        self.assertFalse(ok)
        self.assertIn("honeypot", reason)


class StrictScoreDisplayTest(unittest.TestCase):
    """Every dropped-coin card must ALWAYS note the safety score — the
    line is never silently omitted, even when no score exists."""

    def test_early_card_always_shows_score_line(self):
        from daemon.telegram import alerts
        e = {"symbol": "B70", "name": "Bar", "chain": "base",
             "source": "dexscreener-boost", "mint": "0xB70",
             "usd_market_cap": 120000.0, "age_minutes": 3.0,
             "tier": "boost", "safety_score": 85}
        self.assertIn("85/100", alerts._launch_early(e))
        del e["safety_score"]
        text = alerts._launch_early(e)
        self.assertIn("safety score", text.lower())
        self.assertIn("unavailable", text)

    def test_digest_item_always_tagged(self):
        from daemon.telegram import alerts
        e = {"count": 2, "items": [
            {"symbol": "A", "mint": "0xA", "tier": "boost",
             "safety_score": 91},
            {"symbol": "B", "mint": "0xB", "tier": "boost"},
        ]}
        text = alerts._launch_early_digest(e)
        self.assertIn("[score 91/100]", text)
        self.assertIn("[score n/a]", text)

    def test_lowcap_card_always_shows_score_line(self):
        from daemon.telegram import alerts
        e = {"symbol": "B70", "chain": "base", "source": "pumpfun",
             "mint": "0xB70", "run_pct": 85.0,
             "usd_market_cap": 185000.0, "base_mcap": 100000.0,
             "age_minutes": 12.0}
        text = alerts._launch_lowcap(e)
        self.assertIn("safety score", text.lower())
        self.assertIn("unavailable", text)
        e["safety_score"] = 77
        self.assertIn("77/100", alerts._launch_lowcap(e))

    def test_confirmed_card_always_shows_shield_line(self):
        from daemon.telegram import alerts
        e = {"symbol": "B70", "chain": "base", "source": "pumpfun",
             "mint": "0xB70", "usd_market_cap": 300000.0,
             "age_minutes": 20.0, "tier": "runner"}
        # No safety report at all — the shield line must still appear.
        text = alerts._launch(e)
        self.assertIn("🛡", text)
        self.assertIn("no score", text)
        e["safety"] = "pass (score 88)"
        self.assertIn("score 88", alerts._launch(e))

    def test_verdict_line_unknown_names_no_score(self):
        self.assertIn("no score", safety.verdict_line(
            {"passed": True, "score": 0, "unknown": True}))
        self.assertIn("score 88", safety.verdict_line(
            {"passed": True, "score": 88, "flags": [], "unknown": False}))


if __name__ == "__main__":
    unittest.main()
