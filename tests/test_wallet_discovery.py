"""Hermetic tests for daemon/launch_scanner/wallet_discovery.py.

No network, no real HOME reads: the RPC layer is faked and
FOREX_AGENT_HOME points at a tmp dir.
"""

import os
import time

import pytest

from daemon.launch_scanner import wallet_discovery as wd


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _tx(owner_buys, block_time=1_700_000_000, mint="MINT1"):
    """Build a fake jsonParsed getTransaction result.

    owner_buys: {owner: (pre, post)} for the mint under test.
    """
    pre, post = [], []
    for owner, (a, b) in owner_buys.items():
        pre.append({"owner": owner, "mint": mint,
                    "uiTokenAmount": {"uiAmount": a}})
        post.append({"owner": owner, "mint": mint,
                     "uiTokenAmount": {"uiAmount": b}})
    return {"meta": {"preTokenBalances": pre,
                     "postTokenBalances": post}}


class FakeRPC:
    """Canned Helius: sigs newest-first with blockTime, txs by signature."""

    def __init__(self, sigs, txs):
        # sigs: {mint: [(signature, block_time)]}; txs: {mint: {sig: tx}}
        self.sigs = sigs
        self.txs = txs
        self.calls = []
        self._mint = None

    def __call__(self, method, params):
        self.calls.append(method)
        if method == "getSignaturesForAddress":
            self._mint = params[0]
            return [{"signature": s, "blockTime": bt}
                    for s, bt in self.sigs.get(params[0], [])]
        if method == "getTransaction":
            return self.txs.get(self._mint, {}).get(params[0], {})
        raise AssertionError("unexpected rpc %s" % method)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREX_AGENT_HOME", str(tmp_path))
    # module cached _HOME at import; proposals_path() reads env live
    return tmp_path


# ---------------------------------------------------------------------------
# Step 1: early buyers
# ---------------------------------------------------------------------------

def test_tx_buyers_extracts_balance_increases():
    tx = _tx({"alice": (0, 100.0), "bob": (50.0, 50.0),
              "carol": (200.0, 10.0)})
    buyers = wd._tx_buyers(tx, "MINT1")
    assert set(buyers) == {"alice"}
    assert buyers["alice"] == pytest.approx(100.0)


def test_tx_buyers_ignores_other_mints():
    tx = {"meta": {"preTokenBalances": [],
                   "postTokenBalances": [
                       {"owner": "dave", "mint": "OTHER",
                        "uiTokenAmount": {"uiAmount": 999.0}}]}}
    assert wd._tx_buyers(tx, "MINT1") == {}


def test_early_buyers_respects_window():
    t0 = 1_700_000_000.0
    sigs = [("sig_new", t0 + 7200),   # 2h after first-seen: too late
            ("sig_mid", t0 + 600),    # inside window
            ("sig_old", t0 - 300)]    # slightly before: allowed
    txs = {
        "sig_new": _tx({"late": (0, 50.0)}),
        "sig_mid": _tx({"mid": (0, 50.0)}),
        "sig_old": _tx({"early": (0, 50.0)}),
    }
    rpc = FakeRPC({"MINT1": sigs}, {"MINT1": txs})
    buyers = wd.early_buyers_for_mint(rpc, "MINT1", t0,
                                      early_window_s=3600.0,
                                      sleep_s=0)
    assert set(buyers) == {"mid", "early"}
    assert "late" not in buyers


def test_early_buyers_never_raises_on_rpc_failure():
    def boom(method, params):
        raise RuntimeError("down")
    assert wd.early_buyers_for_mint(boom, "MINT1", time.time(),
                                    sleep_s=0) == {}


# ---------------------------------------------------------------------------
# Step 2: scoring
# ---------------------------------------------------------------------------

def _runner_rpc():
    t0 = 1_700_000_000.0
    sigs = {"MINT1": [("s1", t0 + 100)], "MINT2": [("s2", t0 + 100)]}
    txs = {"MINT1": {"s1": _tx({"w_alice": (0, 10.0), "w_bob": (0, 5.0)})},
           "MINT2": {"s2": _tx({"w_alice": (0, 10.0), "w_bob": (0, 5.0)},
                               mint="MINT2")}}
    return FakeRPC(sigs, txs), t0


def test_score_wallets_counts_hits_and_smooths():
    rpc, t0 = _runner_rpc()
    runners = [
        {"mint": "MINT1", "symbol": "R1", "peak_mult": 12.0,
         "first_seen_ts": t0},
        {"mint": "MINT2", "symbol": "R2", "peak_mult": 8.0,
         "first_seen_ts": t0},
    ]
    scores = wd.score_wallets(runners, rpc, sleep_s=0)
    assert scores["w_alice"]["hits"] == 2
    assert scores["w_bob"]["hits"] == 2
    # Laplace smoothing: 2 hits -> (2+0.2)/(2+2) = 0.55
    assert scores["w_alice"]["hit_rate"] == pytest.approx(0.55)
    assert scores["w_alice"]["runners"] == ["R1", "R2"]


def test_score_wallets_excludes_deployers():
    rpc, t0 = _runner_rpc()
    runners = [{"mint": "MINT1", "symbol": "R1", "peak_mult": 12.0,
                "first_seen_ts": t0}]
    scores = wd.score_wallets(runners, rpc, deployers={"w_alice"},
                              sleep_s=0)
    assert "w_alice" not in scores
    assert "w_bob" in scores


# ---------------------------------------------------------------------------
# Step 3: proposals
# ---------------------------------------------------------------------------

def test_discovery_proposals_filters_and_sorts():
    scores = {
        "w1": {"hits": 3, "seen": 3, "hit_rate": 0.8, "runners": ["A"]},
        "w2": {"hits": 1, "seen": 1, "hit_rate": 0.9, "runners": ["B"]},
        "w3": {"hits": 2, "seen": 2, "hit_rate": 0.4, "runners": ["C"]},
        "w4": {"hits": 2, "seen": 2, "hit_rate": 0.7, "runners": ["D"]},
    }
    now = time.time()
    props = wd.discovery_proposals(scores, registry={"w4": {}},
                                   now=now, day="2026-10-01",
                                   min_hits=2, min_hit_rate=0.5)
    wallets = [p["wallet"] for p in props]
    assert wallets == ["w1"]          # w2 too few hits, w3 rate too low,
                                      # w4 already tracked
    p = props[0]
    assert p["kind"] == "smart_wallet_add"
    assert p["status"] == "open"
    assert p["id"].startswith("W-")


def test_save_and_load_proposals_roundtrip(home):
    now = time.time()
    props = [{"id": "W-ABC123", "kind": "smart_wallet_add",
              "wallet": "w_new", "status": "open", "created_ts": now}]
    added = wd.save_proposals(props)
    assert len(added) == 1
    # dupe wallet not added twice
    assert wd.save_proposals(props) == []
    loaded = wd.load_proposals()
    assert [p["wallet"] for p in loaded] == ["w_new"]
    # file is 0600
    assert (wd.proposals_path().stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Degen:
    discovery_enabled = True
    discovery_runner_mult = 5.0
    discovery_early_window_s = 3600.0
    discovery_min_hits = 1
    discovery_min_hit_rate = 0.3
    discovery_max_runners = 10
    discovery_max_new = 5
    discovery_max_sigs = 30
    discovery_interval_s = 3600.0


def test_run_discovery_end_to_end(home):
    rpc, t0 = _runner_rpc()
    state: dict = {}
    runners = [{"mint": "MINT1", "symbol": "R1", "peak_mult": 12.0,
                "first_seen_ts": t0}]
    out = wd.run_discovery(state, Degen(), runners, rpc=rpc,
                           registry={}, now=t0 + 5000)
    assert out["ran"] is True
    assert out["runners_mined"] == 1
    assert out["wallets_scored"] == 2
    assert out["proposals"] == 2
    # registry untouched — proposals only
    assert wd.load_proposals() and len(wd.load_proposals()) == 2
    # second run inside the throttle window does nothing
    out2 = wd.run_discovery(state, Degen(), runners, rpc=rpc,
                            registry={}, now=t0 + 5001)
    assert out2["ran"] is False
    assert out2["reason"] == "throttled"


def test_run_discovery_skips_below_bar_and_mined(home):
    rpc, _t0 = _runner_rpc()
    state = {wd._DISCOVERY_KEY: {wd._EVAL_KEY: {"MINT1": {}},
                                 "last_run_ts": 0}}
    out = wd.run_discovery(
        state, Degen(),
        [{"mint": "MINT1", "symbol": "R1", "peak_mult": 50.0,
          "first_seen_ts": 1},
         {"mint": "MINT9", "symbol": "R9", "peak_mult": 2.0,  # below bar
          "first_seen_ts": 1}],
        rpc=rpc, registry={}, now=time.time())
    assert out["ran"] is True
    assert out["runners_mined"] == 0
    assert out["reason"] == "no-fresh-runners"


def test_run_discovery_disabled_and_no_rpc(home):
    class Off:
        discovery_enabled = False
    out = wd.run_discovery({}, Off(), [], rpc=None)
    assert out["reason"] == "disabled"
    out = wd.run_discovery({}, Degen(), [], rpc=None)
    assert out["reason"] == "no-rpc"
