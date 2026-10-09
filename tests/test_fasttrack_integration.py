"""Integration tests: smart-buy fast-track + deployer tracking wiring.

Exercises the LaunchScanner wiring (not the workers' pure logic):
  * smart fast-track end-to-end through _smart_fasttrack (mocked verdict)
  * deployer fast-track enrichment through _deployer_card_line
  * cross-dedup: one mint + both triggers -> exactly one card
  * cap-tripped fast-track -> digest queue (never silent), no early_alerted
  * disabled flags -> no fast-track / no deployer trigger
  * hard fail -> blocked even when a wallet bought AND the deployer is hot

No network: the safety verdict is monkeypatched, the grades/deployer
registry files are tmp-patched, publish is captured.
"""

import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.events import bus as event_bus
from daemon.launch_scanner import (LaunchScanner, deployers, smartgrade,
                                   smartmoney)

W1 = "7xKXgT2aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890ab"
DEP1 = "DeplAAAA11111111111111111111111111111111111"


def _degen(**kw):
    base = dict(
        smart_fasttrack_enabled=True,
        smart_poll_seconds=60.0,
        smart_grade_dedup_days=7.0,
        smart_grade_window_days=30.0,
        deployer_tracking_enabled=True,
        deployer_hot_hit_rate=0.30,
        deployer_min_launches=3,
        deployer_resolve_ttl_s=21600.0,
        early_alert_max_per_window=4,
        early_alert_window_seconds=3600.0,
        milestones_enabled=True,
        entry_sol=0.1,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _config(degen):
    return SimpleNamespace(degen=degen)


def _pass(degen, candidate):
    return True, "quality-pass score 5", {"score": 5, "passed": True}


def _tier1_degen(**kw):
    """Sniper knobs so a single tracked-wallet buy scores tier-1.

    (1 smart buy -> 100% of the 100.0 smart weight = 50.0 >= threshold.)
    """
    kw.setdefault("sniper_weight_smart", 100.0)
    kw.setdefault("sniper_weight_fp", 0.0)
    kw.setdefault("sniper_weight_velocity", 0.0)
    kw.setdefault("sniper_weight_pressure", 0.0)
    kw.setdefault("sniper_boost_deployer", 0.0)
    return _degen(**kw)


def _hardfail(degen, candidate):
    return False, "safety-fail: honeypot", {"score": 1, "passed": False,
                                            "fails": ["honeypot"]}


class _Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/ft-integ-%d" % int(time.time() * 1e6))
        self.tmp.mkdir(parents=True)
        self._patches = [
            patch.object(smartgrade, "_GRADES_FILE",
                         self.tmp / "smart_wallet_grades.json"),
            patch.object(deployers, "_REGISTRY_FILE",
                         self.tmp / "deployer_history.json"),
            patch.object(smartmoney, "load_registry",
                         lambda: {W1: {"label": "sniper-1"}}),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.published = []
        self._pub = patch.object(
            event_bus, "publish",
            lambda evt, severity=None: self.published.append(evt)
            or {"ok": True})
        self._pub.start()
        self.addCleanup(self._pub.stop)
        # agent.events.publish is the name _smart_fasttrack imports;
        # patch the package attribute too.
        import agent.events as events_pkg
        self._pub2 = patch.object(
            events_pkg, "publish",
            lambda evt, severity=None: self.published.append(evt)
            or {"ok": True})
        self._pub2.start()
        self.addCleanup(self._pub2.stop)
        self._verdict = patch.object(
            LaunchScanner, "_instant_verdict",
            staticmethod(_pass))
        self._verdict.start()
        self.addCleanup(self._verdict.stop)
        self.scanner = LaunchScanner.__new__(LaunchScanner)

    def _fresh_state(self, mint, wallet=W1):
        return {"seen": {}, "smart_activity": {mint: [wallet]},
                "early_digest": []}


class SmartFastTrackWiringTest(_Harness):
    def test_end_to_end_stages_then_flush_publishes_one_card(self):
        mint = "MINTFASTTRACK11111111111111111111111111111"
        state = self._fresh_state(mint)
        degen = _tier1_degen()
        cfg = _config(degen)
        self.scanner._smart_fasttrack(cfg, state, {mint: [W1]})
        # Sniper tier: tier-1 stages, nothing publishes mid-cycle.
        self.assertEqual(len(self.published), 0)
        self.assertEqual(len(state.get("sniper_pending") or []), 1)
        self.scanner._flush_sniper_pending(cfg, state)
        self.assertEqual(len(self.published), 1)
        evt = self.published[0]
        self.assertEqual(evt["event"], "launch.early")
        self.assertTrue(evt["fasttrack"])
        self.assertEqual(evt["source"], "smart-fasttrack")
        self.assertIn("sniper-1", evt["smart_money"])
        self.assertEqual(evt["smart_wallets"], ["sniper-1"])
        self.assertEqual(evt["safety_score"], 5.0)
        self.assertIn("plan", evt)
        # The strict bus validator must accept the new fields.
        event_bus.validate_event(evt)
        # Idempotency + cross-dedup markers.
        self.assertIn(mint, state["smart_fasttracked"])
        self.assertTrue(state["seen"][mint]["early_alerted"])
        # Wallet grading ledger got the buy.
        grades = smartgrade.load_grades()
        self.assertTrue(any(
            b.get("mint") == mint
            for b in (grades.get(W1) or {}).get("buys", [])))

    def test_second_poll_does_not_refire(self):
        mint = "MINTREPEAT2222222222222222222222222222222"
        state = self._fresh_state(mint)
        degen = _tier1_degen()
        cfg = _config(degen)
        self.scanner._smart_fasttrack(cfg, state, {mint: [W1]})
        self.scanner._smart_fasttrack(cfg, state, {mint: [W1]})
        self.assertEqual(len(state.get("sniper_pending") or []), 1)
        self.scanner._flush_sniper_pending(cfg, state)
        self.assertEqual(len(self.published), 1)

    def test_sub_tier_fasttrack_goes_to_digest_with_cap_room(self):
        # A smart buy that does NOT reach the sniper threshold joins the
        # digest queue even when the instant cap has room — a smart buy
        # boosts the rank, it doesn't bypass the tier.
        mint = "MINTSUBTIER66666666666666666666666666666"
        state = self._fresh_state(mint)
        degen = _degen()  # default knobs: 1 buy = 17.5 < 50
        self.scanner._smart_fasttrack(_config(degen), state,
                                      {mint: [W1]})
        self.assertEqual(len(self.published), 0)
        self.assertEqual(state.get("sniper_pending") or [], [])
        digest = state["early_digest"]
        self.assertEqual(len(digest), 1)
        item = digest[0]
        self.assertEqual(item["mint"], mint)
        self.assertTrue(item["fasttrack"])
        self.assertIn("sniper-1", item["smart_money"])
        self.assertEqual(item["sniper_tier"], "standard")
        # NOT marked early_alerted on the digest path — the coin stays
        # eligible for a full instant card when the cap has room.
        self.assertNotIn(mint, state.get("seen", {}))
        # ... but fast-track will not re-evaluate it on the next poll.
        self.assertIn(mint, state["smart_fasttracked"])

    def test_cap_tripped_tier1_goes_to_digest_not_silent(self):
        mint = "MINTCAPTRIP33333333333333333333333333333"
        state = self._fresh_state(mint)
        now = time.time()
        state["early_window"] = {"start": now, "count": 4}  # cap full
        degen = _tier1_degen()
        cfg = _config(degen)
        self.scanner._smart_fasttrack(cfg, state, {mint: [W1]})
        self.assertEqual(len(self.published), 0)
        self.assertEqual(len(state.get("sniper_pending") or []), 1)
        # The flush honors the standing cap: no send, digest instead.
        self.scanner._flush_sniper_pending(cfg, state)
        self.assertEqual(len(self.published), 0)
        digest = state["early_digest"]
        self.assertEqual(len(digest), 1)
        item = digest[0]
        self.assertEqual(item["mint"], mint)
        self.assertTrue(item["fasttrack"])
        self.assertIn("sniper-1", item["smart_money"])
        self.assertEqual(item["sniper_tier"], "sniper")
        # The cap counter counts SENDS: staging + the failed flush must
        # not consume slots.
        self.assertEqual(state["early_window"]["count"], 4)
        # Tier-1 staging marks early_alerted (one mint, one instant
        # card); the digest flush still delivers the card.
        self.assertTrue(state["seen"][mint]["early_alerted"])
        # ... and fast-track will not re-evaluate it on the next poll.
        self.assertIn(mint, state["smart_fasttracked"])

    def test_disabled_flag_no_fasttrack(self):
        mint = "MINTDISABLED4444444444444444444444444444"
        state = self._fresh_state(mint)
        degen = _degen(smart_fasttrack_enabled=False)
        self.scanner._smart_fasttrack(_config(degen), state,
                                      {mint: [W1]})
        self.assertEqual(len(self.published), 0)
        self.assertEqual(state["early_digest"], [])
        # Grading still ledgers the buy even with fast-track off.
        grades = smartgrade.load_grades()
        self.assertTrue(any(
            b.get("mint") == mint
            for b in (grades.get(W1) or {}).get("buys", [])))

    def test_hard_fail_blocked_and_marked(self):
        mint = "MINTHARDFAIL555555555555555555555555555"
        state = self._fresh_state(mint)
        degen = _degen()
        # Layer the hard-fail verdict OVER the setUp pass-verdict; the
        # context manager restores it on exit (no double-stop).
        with patch.object(LaunchScanner, "_instant_verdict",
                          staticmethod(_hardfail)):
            self.scanner._smart_fasttrack(_config(degen), state,
                                          {mint: [W1]})
        self.assertEqual(len(self.published), 0)
        self.assertEqual(state["early_digest"], [])
        # Held mints are marked evaluated — never re-fired, but the
        # normal scan path stays eligible.
        self.assertIn(mint, state["smart_fasttracked"])
        self.assertNotIn(mint, state.get("seen", {}))


class DeployerCardLineTest(_Harness):
    def _hot_registry(self):
        return {DEP1: {"launches_seen": 10, "hits_2x": 4, "rugs": 0,
                       "dead": 6, "first_seen": 1.0, "last_seen": 2.0}}

    def test_hot_deployer_enriches_card(self):
        mint = "MINTDEPLOYER666666666666666666666666666"
        state = {"seen": {}}
        degen = _degen()
        with patch.object(deployers, "load_registry",
                          lambda: self._hot_registry()), \
             patch.object(deployers, "cached_deployer",
                          lambda *a, **k: DEP1), \
             patch.object(deployers, "record_launch",
                          lambda d: {"launches_seen": 11}):
            dtrig = self.scanner._deployer_card_line(
                _config(degen), state, mint, "solana",
                {"mint": mint, "extra": {"creator": DEP1}}, True)
        self.assertIsNotNone(dtrig)
        self.assertEqual(dtrig["deployer"], DEP1)
        self.assertEqual(dtrig["deployer_line"],
                         "🏭 deployer 4/10 hit 2x")
        # Event carrying the new fields passes strict validation.
        evt = {"event": "launch.early", "symbol": "TST", "mint": mint,
               "deployer": dtrig["deployer"],
               "deployer_line": dtrig["deployer_line"]}
        event_bus.validate_event(evt)

    def test_cold_deployer_no_trigger(self):
        mint = "MINTDEPLOYER777777777777777777777777777"
        state = {"seen": {}}
        degen = _degen()
        with patch.object(deployers, "load_registry", lambda: {}), \
             patch.object(deployers, "cached_deployer",
                          lambda *a, **k: DEP1), \
             patch.object(deployers, "record_launch",
                          lambda d: {"launches_seen": 1}):
            dtrig = self.scanner._deployer_card_line(
                _config(degen), state, mint, "solana",
                {"mint": mint, "extra": {}}, True)
        self.assertIsNone(dtrig)

    def test_disabled_tracking_no_trigger(self):
        mint = "MINTDEPLOYER888888888888888888888888888"
        state = {"seen": {}}
        degen = _degen(deployer_tracking_enabled=False)
        with patch.object(deployers, "cached_deployer") as cd:
            dtrig = self.scanner._deployer_card_line(
                _config(degen), state, mint, "solana",
                {"mint": mint, "extra": {}}, True)
        self.assertIsNone(dtrig)
        cd.assert_not_called()

    def test_safety_failed_no_trigger(self):
        mint = "MINTDEPLOYER999999999999999999999999999"
        state = {"seen": {}}
        degen = _degen()
        with patch.object(deployers, "cached_deployer",
                          lambda *a, **k: DEP1), \
             patch.object(deployers, "record_launch",
                          lambda d: {"launches_seen": 11}), \
             patch.object(deployers, "load_registry",
                          lambda: self._hot_registry()):
            dtrig = self.scanner._deployer_card_line(
                _config(degen), state, mint, "solana",
                {"mint": mint, "extra": {"creator": DEP1}}, False)
        # allow=False -> no resolution-triggered card line, and the
        # worker's evaluate is never reached with a pass.
        self.assertIsNone(dtrig)

    def test_smart_fasttracked_mint_skipped(self):
        mint = "MINTDEDUPAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        state = {"seen": {},
                 "smart_fasttracked": {mint: {"ts": time.time()}}}
        degen = _degen()
        with patch.object(deployers, "cached_deployer",
                          lambda *a, **k: DEP1), \
             patch.object(deployers, "record_launch",
                          lambda d: {"launches_seen": 11}), \
             patch.object(deployers, "load_registry",
                          lambda: self._hot_registry()):
            dtrig = self.scanner._deployer_card_line(
                _config(degen), state, mint, "solana",
                {"mint": mint, "extra": {"creator": DEP1}}, True)
        # Smart-buy fast-track already evaluated this mint — no second
        # fast-track card, even though the deployer is hot.
        self.assertIsNone(dtrig)


class CrossDedupTest(_Harness):
    def test_smart_first_then_deployer_one_card(self):
        mint = "MINTXDBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
        state = self._fresh_state(mint)
        degen = _tier1_degen()
        cfg = _config(degen)
        # 1) smart-buy fast-track fires -> staged -> flush publishes #1.
        self.scanner._smart_fasttrack(cfg, state, {mint: [W1]})
        self.assertEqual(len(self.published), 0)
        self.scanner._flush_sniper_pending(cfg, state)
        self.assertEqual(len(self.published), 1)
        # 2) deployer trigger for the same mint: smart_fasttracked
        # already contains it -> suppressed.
        with patch.object(deployers, "cached_deployer",
                          lambda *a, **k: DEP1), \
             patch.object(deployers, "record_launch",
                          lambda d: {"launches_seen": 11}), \
             patch.object(deployers, "load_registry",
                          lambda: {DEP1: {"launches_seen": 10,
                                          "hits_2x": 4, "rugs": 0,
                                          "dead": 6}}):
            dtrig = self.scanner._deployer_card_line(
                _config(degen), state, mint, "solana",
                {"mint": mint, "extra": {"creator": DEP1}}, True)
        self.assertIsNone(dtrig)
        self.assertEqual(len(self.published), 1)
        # 3) the normal pipeline's early_alerted gate also blocks a
        # second card for the same mint.
        self.assertTrue(state["seen"][mint]["early_alerted"])

    def test_normal_card_first_then_smart_no_refire(self):
        mint = "MINTXDCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
        # Simulate the normal path having carded this mint already.
        state = self._fresh_state(mint)
        state["seen"][mint] = {"early_alerted": True, "alerted": False,
                               "first_ts": time.time()}
        degen = _degen()
        before = len(self.published)
        self.scanner._smart_fasttrack(_config(degen), state,
                                      {mint: [W1]})
        # mint_is_new -> False (already in seen) -> no fire.
        self.assertEqual(len(self.published), before)

    def test_digest_dedup_no_double_queue(self):
        mint = "MINTXDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"
        state = self._fresh_state(mint)
        state["early_digest"] = [{"mint": mint, "symbol": "X",
                                  "fasttrack": True}]
        self.assertTrue(
            LaunchScanner._digest_has(state, mint))
        self.assertFalse(
            LaunchScanner._digest_has(state, "OTHER_MINT"))


class OutcomeWiringTest(_Harness):
    def test_grade_deployer_close_rug(self):
        state = {"deployer_cache": {"M1": {"deployer": DEP1, "ts": 1.0}}}
        degen = _degen()
        pos = {"deployer": DEP1}
        # realized 0.05x -> rug.
        self.scanner._grade_deployer_close(state, degen, "M1",
                                           entry_px=1.0, close_px=0.05,
                                           peak=1.2, pos=pos)
        reg = deployers.load_registry()
        self.assertEqual(reg[DEP1]["rugs"], 1)
        self.assertTrue(state["deployer_outcome_recorded"]["M1"])

    def test_grade_deployer_close_hit_2x_then_expiry_skips(self):
        state = {"deployer_cache": {"M2": {"deployer": DEP1, "ts": 1.0}}}
        degen = _degen()
        pos = {"deployer": DEP1}
        # peak 2.5x of entry, realized 1.5x -> hit_2x.
        self.scanner._grade_deployer_close(state, degen, "M2",
                                           entry_px=1.0, close_px=1.5,
                                           peak=2.5, pos=pos)
        reg = deployers.load_registry()
        self.assertEqual(reg[DEP1]["hits_2x"], 1)
        # Milestone expiry for the same mint must not double-count.
        from daemon.launch_scanner import milestones
        watch = {"M2": {"peak_mult": 3.0, "card_ts": time.time() - 90000}}
        state["milestone_watch"] = watch
        milestones._grade_deployer_outcome(state, degen, "M2",
                                           watch["M2"])
        reg = deployers.load_registry()
        self.assertEqual(reg[DEP1]["hits_2x"], 1)  # still 1

    def test_grade_deployer_close_dead(self):
        state = {"deployer_cache": {"M3": {"deployer": DEP1, "ts": 1.0}}}
        degen = _degen()
        pos = {}
        # realized 0.8x, peak 1.2x -> dead.
        self.scanner._grade_deployer_close(state, degen, "M3",
                                           entry_px=1.0, close_px=0.8,
                                           peak=1.2, pos=pos)
        reg = deployers.load_registry()
        self.assertEqual(reg[DEP1]["dead"], 1)

    def test_expiry_grades_hit_2x_without_close(self):
        from daemon.launch_scanner import milestones
        state = {"deployer_cache": {"M4": {"deployer": DEP1, "ts": 1.0}}}
        degen = _degen()
        rec = {"peak_mult": 5.0, "card_ts": time.time() - 90000}
        milestones._grade_deployer_outcome(state, degen, "M4", rec)
        reg = deployers.load_registry()
        self.assertEqual(reg[DEP1]["hits_2x"], 1)

    def test_expiry_grades_dead_without_close(self):
        from daemon.launch_scanner import milestones
        state = {"deployer_cache": {"M5": {"deployer": DEP1, "ts": 1.0}}}
        degen = _degen()
        rec = {"peak_mult": 1.1, "card_ts": time.time() - 90000}
        milestones._grade_deployer_outcome(state, degen, "M5", rec)
        reg = deployers.load_registry()
        self.assertEqual(reg[DEP1]["dead"], 1)


if __name__ == "__main__":
    unittest.main()
