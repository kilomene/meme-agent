"""Tests for proof-of-demand checks (measurement only).

Covers the three checks (LP lock, holder concentration, distribution),
the measure() ledger behavior (bounded, TTL, disabled, never-raises),
and the config knob declarations.
"""

import unittest
import unittest.mock
from types import SimpleNamespace

from daemon.launch_scanner import proof


def _degen(**kw):
    d = {"proof_enabled": True,
         "proof_holder_top10_max_pct": 40.0,
         "proof_holder_single_max_pct": 20.0,
         "proof_dump_sell_ratio": 2.0,
         "proof_insider_max_pct": 20.0,
         "proof_timeout_s": 8.0}
    d.update(kw)
    return SimpleNamespace(**d)


class LpCheckTests(unittest.TestCase):
    def test_lockers_dict_pass(self):
        r = proof.check_lp({"lockers": {"a": {}}, "markets": []})
        self.assertEqual(r["verdict"], "pass")

    def test_lockers_list_pass(self):
        r = proof.check_lp({"lockers": [{"a": 1}], "markets": []})
        self.assertEqual(r["verdict"], "pass")

    def test_market_lp_locked_pass(self):
        r = proof.check_lp({"lockers": {},
                            "markets": [{"marketType": "x",
                                         "lpLocked": True}]})
        self.assertEqual(r["verdict"], "pass")

    def test_market_explicit_unlocked_fail(self):
        r = proof.check_lp({"lockers": {},
                            "markets": [{"marketType": "x",
                                         "lpLocked": False}]})
        self.assertEqual(r["verdict"], "fail")

    def test_no_data_unknown_not_pass(self):
        # The www case: RugCheck carries no lock info. Must be
        # "unknown", never silently treated as pass.
        r = proof.check_lp({"lockers": {}, "markets": [
            {"marketType": "pump_fun_amm", "lpLocked": None}]})
        self.assertEqual(r["verdict"], "unknown")

    def test_none_report_unknown(self):
        self.assertEqual(proof.check_lp(None)["verdict"], "unknown")
        self.assertEqual(proof.check_lp("junk")["verdict"], "unknown")


class HolderCheckTests(unittest.TestCase):
    def _holders(self, pcts):
        return {"topHolders": [{"pct": p, "address": "a%d" % i}
                               for i, p in enumerate(pcts)]}

    def test_distributed_pass(self):
        r = proof.check_holders(self._holders([2.5, 1.8, 1.8, 1.7, 1.6,
                                                1.4, 1.4, 1.3, 1.2, 1.1]))
        self.assertEqual(r["verdict"], "pass")

    def test_top10_concentrated_fail(self):
        r = proof.check_holders(self._holders([8.0] * 10))
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("top 10", r["detail"])

    def test_single_whale_fail(self):
        r = proof.check_holders(self._holders([25.0, 1.0, 1.0]))
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("top wallet", r["detail"])

    def test_no_holders_unknown(self):
        self.assertEqual(proof.check_holders({})["verdict"], "unknown")
        self.assertEqual(proof.check_holders(None)["verdict"], "unknown")

    def test_custom_limits(self):
        r = proof.check_holders(self._holders([8.0] * 10),
                                top10_max_pct=90.0, single_max_pct=50.0)
        self.assertEqual(r["verdict"], "pass")


class DistributionCheckTests(unittest.TestCase):
    def _pair(self, buys, sells):
        return {"txns": {"h1": {"buys": buys, "sells": sells}}}

    def test_sells_dominating_fail(self):
        with unittest.mock.patch.object(
                proof, "_dex_pair", return_value=self._pair(100, 300)):
            r = proof.check_distribution("M", "solana")
        self.assertEqual(r["verdict"], "fail")

    def test_balanced_pass(self):
        with unittest.mock.patch.object(
                proof, "_dex_pair", return_value=self._pair(300, 200)):
            r = proof.check_distribution("M", "solana")
        self.assertEqual(r["verdict"], "pass")

    def test_thin_book_unknown(self):
        with unittest.mock.patch.object(
                proof, "_dex_pair", return_value=self._pair(3, 12)):
            r = proof.check_distribution("M", "solana")
        self.assertEqual(r["verdict"], "unknown")

    def test_no_pair_unknown(self):
        with unittest.mock.patch.object(proof, "_dex_pair",
                                        return_value=None):
            r = proof.check_distribution("M", "solana")
        self.assertEqual(r["verdict"], "unknown")

    def test_insider_cluster_fail_no_http(self):
        # Insider tripwire fires before any HTTP call.
        with unittest.mock.patch.object(
                proof, "_dex_pair",
                side_effect=AssertionError("must not fetch")):
            r = proof.check_distribution("M", "solana", insider_pct=35.0)
        self.assertEqual(r["verdict"], "fail")

    def test_zero_buys_fail(self):
        with unittest.mock.patch.object(
                proof, "_dex_pair", return_value=self._pair(0, 50)):
            r = proof.check_distribution("M", "solana")
        self.assertEqual(r["verdict"], "fail")


class MeasureTests(unittest.TestCase):
    def _mocked(self):
        p1 = unittest.mock.patch.object(
            proof, "_rugcheck_report",
            return_value={"lockers": {"a": {}},
                          "topHolders": [{"pct": 2.0, "address": "x"}],
                          "insiderNetworks": []})
        p2 = unittest.mock.patch.object(
            proof, "_dex_pair",
            return_value={"txns": {"h1": {"buys": 200, "sells": 100}}})
        return p1, p2

    def test_measure_records_all_three(self):
        p1, p2 = self._mocked()
        with p1, p2:
            rec = proof.measure({}, _degen(), "MINT1", "solana", now=1000.0)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["lp"]["verdict"], "pass")
        self.assertEqual(rec["holders"]["verdict"], "pass")
        self.assertEqual(rec["distribution"]["verdict"], "pass")

    def test_measure_writes_bounded_ledger(self):
        state = {}
        p1, p2 = self._mocked()
        with p1, p2:
            for i in range(505):
                proof.measure(state, _degen(), "M%d" % i, "solana",
                              now=1000.0 + i)
        ledger = state["proof_checks"]
        self.assertLessEqual(len(ledger), 500)
        # oldest evicted, newest kept
        self.assertNotIn("M0", ledger)
        self.assertIn("M504", ledger)

    def test_measure_disabled_is_noop(self):
        state = {}
        rec = proof.measure(state, _degen(proof_enabled=False),
                            "MINT1", "solana")
        self.assertIsNone(rec)
        self.assertNotIn("proof_checks", state)

    def test_measure_empty_mint_noop(self):
        self.assertIsNone(proof.measure({}, _degen(), "", "solana"))

    def test_measure_never_raises(self):
        # Fetch failures surface as None reports -> unknown verdicts,
        # still a record. A crash deeper inside -> None, never raises.
        with unittest.mock.patch.object(
                proof, "_rugcheck_report", return_value=None), \
             unittest.mock.patch.object(proof, "_dex_pair",
                                        return_value=None):
            rec = proof.measure({}, _degen(), "MINT1", "solana")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["lp"]["verdict"], "unknown")
        self.assertEqual(rec["holders"]["verdict"], "unknown")
        self.assertEqual(rec["distribution"]["verdict"], "unknown")
        with unittest.mock.patch.object(
                proof, "_dex_pair",
                side_effect=RuntimeError("boom")):
            # per-check try/except absorbs it -> unknown, still a record
            rec2 = proof.measure({}, _degen(), "MINT1", "solana")
        self.assertIsNotNone(rec2)
        self.assertEqual(rec2["distribution"]["verdict"], "unknown")

    def test_measure_evm_checks_unknown(self):
        with unittest.mock.patch.object(
                proof, "_dex_pair",
                return_value={"txns": {"h1": {"buys": 200,
                                              "sells": 100}}}):
            rec = proof.measure({}, _degen(), "0xABC", "base")
        self.assertEqual(rec["lp"]["verdict"], "unknown")
        self.assertEqual(rec["holders"]["verdict"], "unknown")
        self.assertEqual(rec["distribution"]["verdict"], "pass")

    def test_measure_prunes_stale(self):
        state = {"proof_checks": {
            "OLD": {"ts": 1.0, "lp": {}, "holders": {},
                    "distribution": {}}}}
        p1, p2 = self._mocked()
        with p1, p2:
            proof.measure(state, _degen(), "NEW", "solana",
                          now=8 * 24 * 3600.0)
        self.assertNotIn("OLD", state["proof_checks"])
        self.assertIn("NEW", state["proof_checks"])

    def test_summary_line(self):
        rec = {"lp": {"verdict": "unknown"},
               "holders": {"verdict": "pass"},
               "distribution": {"verdict": "fail"}}
        self.assertEqual(proof.summary(rec), "LP:? HOLDERS:PASS DIST:FAIL")
        self.assertEqual(proof.summary(None), "no proof data")


class ConfigKnobTests(unittest.TestCase):
    def test_degen_config_declares_proof_knobs(self):
        from config.config import DegenConfig
        import dataclasses
        names = {f.name for f in dataclasses.fields(DegenConfig)}
        for k in ("proof_enabled", "proof_holder_top10_max_pct",
                  "proof_holder_single_max_pct", "proof_dump_sell_ratio",
                  "proof_insider_max_pct", "proof_timeout_s"):
            self.assertIn(k, names)

    def test_yaml_declares_proof_knobs(self):
        import yaml
        with open("config/defaults.yaml") as fh:
            y = yaml.safe_load(fh)
        degen = y.get("degen") or {}
        for k in ("proof_enabled", "proof_holder_top10_max_pct",
                  "proof_holder_single_max_pct", "proof_dump_sell_ratio",
                  "proof_insider_max_pct", "proof_timeout_s"):
            self.assertIn(k, degen, "defaults.yaml missing degen.%s" % k)

    def test_knob_defaults_used_when_missing(self):
        # A degen namespace without proof_* attrs must not crash
        # measure() — getattr defaults apply (no repeat of the
        # two-tier getattr bug).
        with unittest.mock.patch.object(
                proof, "_rugcheck_report", return_value=None), \
             unittest.mock.patch.object(proof, "_dex_pair",
                                        return_value=None):
            rec = proof.measure({}, SimpleNamespace(), "M", "solana")
        self.assertIsNotNone(rec)


if __name__ == "__main__":
    unittest.main()
