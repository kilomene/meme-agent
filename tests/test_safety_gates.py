"""Safety-gate regression tests: RugCheck insider-network gates and the
EVM sell-simulation (Honeypot.is), plus the fail-closed bypass fixes.

The user's standing rule: every coin dropped to the bot must have a
KNOWN safety score >= 1/100 — no raw CA may leave the bot without a
live screen having run, and unknown/unscored coins are always silent.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from daemon.launch_scanner import LaunchScanner, safety


def _rug(**kw):
    d = {"rugged": False, "mintAuthority": None, "freezeAuthority": None,
         "risks": [], "score_normalised": 85, "topHolders": [],
         "graphInsidersDetected": 0, "insiderNetworks": [],
         "creatorTokens": []}
    d.update(kw)
    return d


def _goplus(**kw):
    info = {"is_honeypot": "0", "buy_tax": "1", "sell_tax": "1",
            "is_mintable": "0", "cannot_buy": "0",
            "cannot_sell_all": "0", "is_blacklisted": "0"}
    info.update(kw)
    return {"result": {"0xABC": info}}


def _sim(**kw):
    sim = {"isHoneypot": False, "cannotBuy": False,
           "cannotSellAll": False, "buyTax": 0, "sellTax": 0}
    sim.update(kw)
    return {"honeypotResult": sim}


def _route(goplus_data, sim_data, calls):
    """Route _get_json by URL; record every fetch URL."""
    def _fetch(url, timeout=None):
        calls.append(url)
        if "honeypot.is" in url:
            return sim_data
        return goplus_data
    return _fetch


class InsiderGatesTest(unittest.TestCase):
    """RugCheck insiderNetworks / graphInsidersDetected hard gates."""

    def _sol(self, payload, limits=None):
        with patch.object(safety, "_get_json", return_value=payload):
            return safety.check_token("solana", "MINT1", limits=limits)

    def test_cluster_share_above_limit_fails(self):
        rep = _rug(insiderNetworks=[{"pct": 15.0}, {"pct": 12.5}])
        r = self._sol(rep)
        self.assertFalse(r["passed"])
        self.assertTrue(any("insider cluster holds" in f
                            for f in r["fails"]))
        # 15 + 12.5 = 27.5 -> message rounds
        self.assertTrue(any("28%" in f or "27%" in f for f in r["fails"]))

    def test_cluster_share_below_limit_flags_only(self):
        rep = _rug(insiderNetworks=[{"pct": 8.0}])
        r = self._sol(rep)
        self.assertTrue(r["passed"])
        self.assertTrue(any("insider network holds" in f
                            for f in r["flags"]))

    def test_cluster_share_exact_limit_passes(self):
        rep = _rug(insiderNetworks=[{"pct": 20.0}])
        r = self._sol(rep)
        self.assertTrue(r["passed"])  # fail only when STRICTLY above

    def test_unparseable_network_shape_never_fails(self):
        rep = _rug(insiderNetworks=[{"wallets": ["a", "b"]}])
        r = self._sol(rep)
        self.assertTrue(r["passed"])
        self.assertTrue(any("insider network detected" in f
                            for f in r["flags"]))

    def test_graph_insiders_above_limit_fails(self):
        r = self._sol(_rug(graphInsidersDetected=6))
        self.assertFalse(r["passed"])
        self.assertTrue(any("graph insiders" in f for f in r["fails"]))

    def test_graph_insiders_within_limit_flags(self):
        r = self._sol(_rug(graphInsidersDetected=3))
        self.assertTrue(r["passed"])
        self.assertTrue(any("insider network detected" in f
                            for f in r["flags"]))

    def test_no_duplicate_insider_flag(self):
        rep = _rug(insiderNetworks=[{"pct": 5.0}],
                   graphInsidersDetected=2)
        r = self._sol(rep)
        self.assertTrue(r["passed"])
        insider_flags = [f for f in r["flags"] if "insider" in f]
        self.assertEqual(len(insider_flags), 1)

    def test_custom_limits_override(self):
        rep = _rug(insiderNetworks=[{"pct": 30.0}],
                   graphInsidersDetected=8)
        r = self._sol(rep, limits={"insider_cluster_max_pct": 40.0,
                                   "graph_insiders_max": 10})
        self.assertTrue(r["passed"])

    def test_malformed_limits_fall_back_to_defaults(self):
        rep = _rug(graphInsidersDetected=6)
        r = self._sol(rep, limits={"graph_insiders_max": "junk"})
        self.assertFalse(r["passed"])  # default 5 still applies

    def test_serial_deployer_still_fails(self):
        rep = _rug(creatorTokens=[{"mint": "x"}] * 50)
        r = self._sol(rep)
        self.assertFalse(r["passed"])
        self.assertTrue(any("serial deployer" in f for f in r["fails"]))


class SellSimulationTest(unittest.TestCase):
    """Honeypot.is buy->sell simulation on Base/Ethereum."""

    def _evm(self, goplus_data, sim_data, limits=None):
        calls = []
        with patch.object(safety, "_get_json",
                          side_effect=_route(goplus_data, sim_data, calls)):
            r = safety.check_token("base", "0xabc", limits=limits)
        return r, calls

    def test_simulated_honeypot_fails(self):
        r, calls = self._evm(_goplus(), _sim(isHoneypot=True))
        self.assertFalse(r["passed"])
        self.assertTrue(any("honeypot (sell simulation)" in f
                            for f in r["fails"]))
        self.assertEqual(len(calls), 2)  # goplus + simulation

    def test_simulated_cannot_sell_fails(self):
        r, _ = self._evm(_goplus(), _sim(cannotSellAll=True))
        self.assertFalse(r["passed"])
        self.assertTrue(any("cannot sell all (sell simulation)" in f
                            for f in r["fails"]))

    def test_simulated_sell_tax_fails_above_ten(self):
        r, _ = self._evm(_goplus(), _sim(sellTax=12))
        self.assertFalse(r["passed"])
        self.assertTrue(any("simulated" in f for f in r["fails"]))

    def test_simulated_sell_tax_flags_above_five(self):
        r, _ = self._evm(_goplus(), _sim(sellTax=7))
        self.assertTrue(r["passed"])
        self.assertTrue(any("simulated" in f for f in r["flags"]))

    def test_simulation_unavailable_is_flag_not_fail(self):
        r, _ = self._evm(_goplus(), None)  # API down
        self.assertTrue(r["passed"])
        self.assertTrue(any("sell simulation unavailable" in f
                            for f in r["flags"]))

    def test_clean_simulation_passes_with_full_score(self):
        r, _ = self._evm(_goplus(), _sim())
        self.assertTrue(r["passed"])
        self.assertEqual(r["score"], 100)

    def test_simulation_disabled_skips_second_call(self):
        r, calls = self._evm(_goplus(), _sim(isHoneypot=True),
                             limits={"evm_sell_sim_enabled": False})
        self.assertTrue(r["passed"])
        self.assertEqual(len(calls), 1)  # goplus only

    def test_goplus_fail_skips_simulation(self):
        r, calls = self._evm(_goplus(is_honeypot="1"),
                             _sim(isHoneypot=True))
        self.assertFalse(r["passed"])
        self.assertEqual(len(calls), 1)  # no wasted sim call
        self.assertIn("honeypot", r["fails"])

    def test_bool_and_string_sim_fields(self):
        # Honeypot.is may answer with bools or "1"/"0" strings.
        r, _ = self._evm(_goplus(), _sim(isHoneypot="1"))
        self.assertFalse(r["passed"])
        r, _ = self._evm(_goplus(), _sim(isHoneypot="false"))
        self.assertTrue(r["passed"])


class StrictBypassTest(unittest.TestCase):
    """Fail-closed: no screen, no score, no CA — at either gate."""

    def setUp(self):
        self.sc = LaunchScanner.__new__(LaunchScanner)

    def test_instant_flash_disabled_is_fail_closed(self):
        degen = SimpleNamespace(early_safety_flash=False)
        coin = {"mint": "MINT", "symbol": "X", "chain": "solana"}
        with patch.object(safety, "check_token") as ck:
            allow, reason, report = LaunchScanner._instant_verdict(
                degen, coin)
        self.assertFalse(allow)
        self.assertIn("flash-disabled", reason)
        self.assertIsNone(report)
        ck.assert_not_called()  # no screen ran -> no CA, period

    def test_confirm_unknown_always_blocks(self):
        for fail_closed in (False, True):
            degen = SimpleNamespace(early_min_score=50.0,
                                    safety_fail_closed=fail_closed)
            cand = {"mint": "MINT", "symbol": "X", "chain": "solana"}
            report = {"passed": True, "score": 0, "flags": [],
                      "fails": [], "unknown": True}
            with patch.object(safety, "check_token",
                               return_value=report):
                ok, reason, _ = self.sc._confirm_safety_ok(degen, cand)
            self.assertFalse(ok)
            self.assertIn("safety-unknown", reason)

    def test_confirm_known_low_score_still_blocks(self):
        degen = SimpleNamespace(early_min_score=50.0,
                                safety_fail_closed=False)
        cand = {"mint": "MINT", "symbol": "X", "chain": "solana"}
        report = {"passed": True, "score": 49, "flags": [],
                  "fails": [], "unknown": False}
        with patch.object(safety, "check_token", return_value=report):
            ok, reason, _ = self.sc._confirm_safety_ok(degen, cand)
        self.assertFalse(ok)
        self.assertIn("49 < 50", reason)

    def test_safety_limits_defaults(self):
        limits = LaunchScanner._safety_limits(SimpleNamespace())
        self.assertEqual(limits["insider_cluster_max_pct"], 20.0)
        self.assertEqual(limits["graph_insiders_max"], 5)
        self.assertTrue(limits["evm_sell_sim_enabled"])
        self.assertEqual(limits["evm_sell_sim_timeout_s"], 3.0)


class SafetyConfigTest(unittest.TestCase):
    """New knobs exist with the strict defaults."""

    def test_defaults(self):
        import yaml
        from config.config import DegenConfig
        with open("config/defaults.yaml") as fh:
            y = yaml.safe_load(fh)["degen"]
        self.assertEqual(y["insider_cluster_max_pct"], 20.0)
        self.assertEqual(y["graph_insiders_max"], 5)
        self.assertTrue(y["evm_sell_sim_enabled"])
        self.assertEqual(y["evm_sell_sim_timeout_s"], 3.0)
        cfg = DegenConfig()
        self.assertEqual(cfg.insider_cluster_max_pct, 20.0)
        self.assertEqual(cfg.graph_insiders_max, 5)
        self.assertTrue(cfg.evm_sell_sim_enabled)
        self.assertEqual(cfg.evm_sell_sim_timeout_s, 3.0)


if __name__ == "__main__":
    unittest.main()
