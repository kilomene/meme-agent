"""Tests for deployer tracking: registry math, hot-deployer rule, fast-track
ordering vs the safety gate, outcome recording, /deployers output, and the
registry file's 0600 permissions. No network — Helius RPC is mocked."""

import json
import os
import stat
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from daemon.launch_scanner import deployers
from daemon.telegram import commands_deployers

DEP1 = "DeplAAAA11111111111111111111111111111111111"
DEP2 = "DeplBBBB22222222222222222222222222222222222"
DEP3 = "DeplCCCC33333333333333333333333333333333333"


def _degen(**kw):
    base = dict(deployer_tracking_enabled=True, deployer_min_launches=3,
                deployer_hot_hit_rate=0.30, deployer_resolve_ttl_s=21600.0)
    base.update(kw)
    return SimpleNamespace(**base)


class DeployerRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/deployer-test-%d" % int(time.time() * 1e6))
        self.tmp.mkdir(parents=True)
        self.reg = self.tmp / "deployer_history.json"
        self.p = patch.object(deployers, "_REGISTRY_FILE", self.reg)
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_record_launches_and_outcomes_hit_rate_math(self):
        for _ in range(5):
            deployers.record_launch(DEP1)
        self.assertTrue(deployers.record_outcome("mintA", DEP1, "hit_2x"))
        self.assertTrue(deployers.record_outcome("mintB", DEP1, "hit_2x"))
        self.assertTrue(deployers.record_outcome("mintC", DEP1, "rug"))
        self.assertTrue(deployers.record_outcome("mintD", DEP1, "dead"))
        hist = deployers.load_registry()
        entry = hist[DEP1]
        self.assertEqual(entry["launches_seen"], 5)
        self.assertEqual(entry["hits_2x"], 2)
        self.assertEqual(entry["rugs"], 1)
        self.assertEqual(entry["dead"], 1)
        self.assertAlmostEqual(deployers.hit_rate(entry), 0.4)
        self.assertGreater(entry["first_seen"], 0)
        self.assertGreaterEqual(entry["last_seen"], entry["first_seen"])

    def test_registry_file_created_with_0600(self):
        deployers.record_launch(DEP1)
        self.assertTrue(self.reg.exists())
        mode = stat.S_IMODE(os.stat(self.reg).st_mode)
        self.assertEqual(mode, 0o600)
        # Save is atomic-ish (tmp file + 0600 + rename).
        self.assertFalse((self.tmp / "deployer_history.tmp").exists())

    def test_unknown_outcome_rejected(self):
        self.assertFalse(deployers.record_outcome("mintA", DEP1, "moon"))
        self.assertEqual(deployers.load_registry(), {})

    def test_empty_deployer_ignored(self):
        self.assertIsNone(deployers.record_launch(""))
        self.assertFalse(deployers.record_outcome("mintA", "", "hit_2x"))

    def test_outcome_without_prior_launch_keeps_rate_sane(self):
        deployers.record_outcome("mintA", DEP2, "hit_2x")
        entry = deployers.load_registry()[DEP2]
        self.assertLessEqual(deployers.hit_rate(entry), 1.0)


class HotDeployerRuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/deployer-test-%d" % int(time.time() * 1e6))
        self.tmp.mkdir(parents=True)
        self.reg = self.tmp / "deployer_history.json"
        self.p = patch.object(deployers, "_REGISTRY_FILE", self.reg)
        self.p.start()
        self.addCleanup(self.p.stop)
        # Hot: 4 launches, 2 hits = 50% >= 30%.
        for _ in range(4):
            deployers.record_launch(DEP1)
        deployers.record_outcome("m1", DEP1, "hit_2x")
        deployers.record_outcome("m2", DEP1, "hit_2x")
        # Cold: 2 launches (below min 3).
        for _ in range(2):
            deployers.record_launch(DEP2)
        deployers.record_outcome("m3", DEP2, "hit_2x")

    def test_hot_deployer_fasttrack_eligible(self):
        degen = _degen()
        state = {}
        res = deployers.evaluate_deployer_fasttrack(
            state, degen, "freshmint", DEP1, safety_passed=True)
        self.assertIsNotNone(res)
        self.assertEqual(res["deployer"], DEP1)
        self.assertEqual(res["launches_seen"], 4)
        self.assertEqual(res["hits_2x"], 2)
        self.assertEqual(res["hit_rate"], 0.5)
        self.assertIn("🏭", res["deployer_line"])
        self.assertIn("2/4", res["deployer_line"])

    def test_cold_deployer_neutral(self):
        degen = _degen()
        self.assertFalse(deployers.is_hot(DEP2, degen))
        res = deployers.evaluate_deployer_fasttrack(
            {}, degen, "freshmint", DEP2, safety_passed=True)
        self.assertIsNone(res)  # no boost, no penalty

    def test_unknown_deployer_neutral(self):
        degen = _degen()
        self.assertFalse(deployers.is_hot(DEP3, degen))
        res = deployers.evaluate_deployer_fasttrack(
            {}, degen, "freshmint", DEP3, safety_passed=True)
        self.assertIsNone(res)

    def test_already_carded_mint_not_fasttracked(self):
        degen = _degen()
        state = {}
        first = deployers.evaluate_deployer_fasttrack(
            state, degen, "mintX", DEP1, safety_passed=True)
        self.assertIsNotNone(first)
        second = deployers.evaluate_deployer_fasttrack(
            state, degen, "mintX", DEP1, safety_passed=True)
        self.assertIsNone(second)  # new-to-us mints only

    def test_tracking_disabled_neutral(self):
        degen = _degen(deployer_tracking_enabled=False)
        res = deployers.evaluate_deployer_fasttrack(
            {}, degen, "freshmint", DEP1, safety_passed=True)
        self.assertIsNone(res)

    def test_custom_knobs_respected(self):
        # min_launches=5 -> 4 launches is cold under this knob.
        degen = _degen(deployer_min_launches=5)
        self.assertFalse(deployers.is_hot(DEP1, degen))
        self.assertIsNone(deployers.evaluate_deployer_fasttrack(
            {}, degen, "freshmint", DEP1, safety_passed=True))
        # hit rate 25% under a 0.30 bar -> not hot.
        for _ in range(4):
            deployers.record_launch(DEP3)
        deployers.record_outcome("mx", DEP3, "hit_2x")  # 1/4 = 25%
        self.assertFalse(deployers.is_hot(DEP3, _degen()))

    def test_hard_fail_override_blocked(self):
        # Hot deployer + honeypot/can't-sell: the safety gate's verdict
        # wins — history NEVER overrides hard fails.
        degen = _degen()
        self.assertTrue(deployers.is_hot(DEP1, degen))
        res = deployers.evaluate_deployer_fasttrack(
            {}, degen, "honeypotmint", DEP1, safety_passed=False)
        self.assertIsNone(res)


class ResolutionCacheTest(unittest.TestCase):
    def test_pumpfun_creator_from_ctx_zero_rpc(self):
        ctx = {"extra": {"creator": DEP1}}
        with patch.object(deployers, "_rpc") as rpc:
            dep = deployers.resolve_deployer("mintX", "solana", ctx)
        self.assertEqual(dep, DEP1)
        rpc.assert_not_called()  # creator came from the source feed

    def test_cached_deployer_skips_repeat_resolution(self):
        ctx = {"extra": {"creator": DEP1}}
        state = {}
        calls = []

        def fake_resolve(mint, chain, ctx):
            calls.append(mint)
            return "DEPL%03d" % len(calls)

        degen = _degen()
        first = deployers.cached_deployer(state, "mintA", "solana", ctx,
                                         degen=degen)
        self.assertEqual(first, DEP1)
        second = deployers.cached_deployer(state, "mintA", "solana", ctx,
                                          degen=degen)
        self.assertEqual(second, DEP1)
        self.assertIn("mintA", state["deployer_cache"])
        # TTL expiry forces a re-resolve.
        state["deployer_cache"]["mintB"] = {"deployer": None,
                                            "ts": time.time() - 99999}
        with patch.object(deployers, "resolve_deployer", fake_resolve):
            got = deployers.cached_deployer(state, "mintB", "solana", ctx,
                                           degen=degen)
        self.assertEqual(got, "DEPL001")
        self.assertEqual(len(calls), 1)

    def test_rpc_fallback_first_signer(self):
        sigs = [{"signature": "sig_new"}, {"signature": "sig_old"}]
        tx = {"transaction": {"message": {"accountKeys": [
            {"pubkey": DEP2}, {"pubkey": "other"}]}}}
        with patch.object(deployers, "_rpc",
                           side_effect=[sigs, tx]) as rpc, \
             patch.object(deployers, "api_key", return_value="k"):
            dep = deployers.resolve_deployer("mintPool", "solana", {})
        self.assertEqual(dep, DEP2)
        rpc.assert_called()

    def test_rpc_fallback_aborts_when_too_much_history(self):
        sigs = [{"signature": "sig%d" % i} for i in range(100)]
        with patch.object(deployers, "_rpc", return_value=sigs):
            self.assertIsNone(deployers.resolve_deployer("mintPool",
                                                         "solana", {}))


class DeployersCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/deployer-test-%d" % int(time.time() * 1e6))
        self.tmp.mkdir(parents=True)
        self.reg = self.tmp / "deployer_history.json"
        self.p = patch.object(deployers, "_REGISTRY_FILE", self.reg)
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_empty_registry_renders_gracefully(self):
        text = commands_deployers.cmd_deployers(SimpleNamespace(), [])
        self.assertIn("No hot deployers yet", text)
        self.assertIn("neutral", text)

    def test_output_with_min_launches_filter(self):
        for _ in range(4):
            deployers.record_launch(DEP1)
        deployers.record_outcome("m1", DEP1, "hit_2x")
        deployers.record_outcome("m2", DEP1, "hit_2x")
        for _ in range(2):  # cold — below the default filter
            deployers.record_launch(DEP2)
        deployers.record_outcome("m3", DEP2, "hit_2x")
        text = commands_deployers.cmd_deployers(SimpleNamespace(), [])
        self.assertIn("2/4", text)
        self.assertIn("50%", text)
        self.assertNotIn(DEP2[:8], text)  # filtered: only 2 launches
        # Loosen the filter: the 2-launch deployer now shows.
        loose = commands_deployers.cmd_deployers(SimpleNamespace(), ["2"])
        self.assertIn(DEP2[:8], loose)


if __name__ == "__main__":
    unittest.main()
