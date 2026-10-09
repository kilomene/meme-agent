"""Hermetic tests for daemon/launch_scanner/winner_model.py.

Synthetic paper records only — never touches the real HOME dataset.
"""

import time

import pytest

from daemon.launch_scanner import winner_model as wm


# ---------------------------------------------------------------------------
# Synthetic records
# ---------------------------------------------------------------------------

def _rec(symbol, peak_mult, rug=False, smart_buys=0, fp="COLD",
         safety=1, sniper=40.0, lane="launch", insider_dir="unknown",
         cluster=0.0, narratives=("other",), heat=1.0, velocity=None):
    snaps = []
    if velocity is not None:
        t0 = 1_700_000_000.0
        snaps = [{"t": t0, "mcap_usd": 10_000.0},
                 {"t": t0 + 300, "mcap_usd": 10_000.0 * velocity}]
    return {
        "mint": "mint_" + symbol, "symbol": symbol,
        "peak_mult": peak_mult, "rug": rug,
        "safety": {"score": safety},
        "sniper_score": sniper,
        "entry_signals": {"smart_buys": smart_buys, "fp_verdict": fp,
                          "rising_polls": 2, "buy_pressure": 0.7,
                          "hot_deployer": False},
        "narrative_heat": heat, "narratives": list(narratives),
        "label": lane, "source": "pumpfun", "wave": 1,
        "snapshots": snaps,
        "_insider": {"direction": insider_dir,
                     "cluster_start": cluster},
    }


def _dataset():
    rows, insider = [], {}
    # xxx winners: smart money in early, HOT fingerprint, accumulating
    for i in range(6):
        r = _rec("WIN%d" % i, peak_mult=15.0 + i, smart_buys=2, fp="HOT",
                 safety=1, sniper=72.0, insider_dir="accumulating",
                 cluster=3.0, velocity=2.5)
        rows.append(r)
        insider[r["mint"]] = r.pop("_insider")
    # duds: no smart money, cold, diluting insiders
    for i in range(14):
        r = _rec("DUD%d" % i, peak_mult=1.2, smart_buys=0, fp="COLD",
                 safety=1, sniper=35.0, insider_dir="diluting",
                 cluster=12.0, velocity=1.1)
        rows.append(r)
        insider[r["mint"]] = r.pop("_insider")
    return rows, insider


# ---------------------------------------------------------------------------
# Features + labels
# ---------------------------------------------------------------------------

def test_is_xxx_labels_only_big_non_rugs():
    assert wm.is_xxx({"peak_mult": 12.0, "rug": False}) is True
    assert wm.is_xxx({"peak_mult": 12.0, "rug": True}) is False
    assert wm.is_xxx({"peak_mult": 3.0, "rug": False}) is False


def test_features_bin_correctly():
    rows, insider = _dataset()
    f = wm.features_from_record(rows[0], insider)
    assert f["smart_buys"] == "2+"
    assert f["fp_verdict"] == "HOT"
    assert f["safety_score"] == "0-1"
    assert f["sniper_score"] == "70+"
    assert f["insider_direction"] == "accumulating"
    assert f["early_velocity_15m"] == "1.5-3x"
    f2 = wm.features_from_record(rows[6], insider)
    assert f2["smart_buys"] == "0"
    assert f2["insider_direction"] == "diluting"


def test_features_missing_insider_is_unknown():
    f = wm.features_from_record(_rec("X", 1.0), {})
    assert f["insider_direction"] == "unknown"
    assert f["insider_cluster"] == "unknown"


# ---------------------------------------------------------------------------
# Train + score
# ---------------------------------------------------------------------------

def test_train_counts_and_json_safe():
    rows, insider = _dataset()
    import json
    model = wm.train(rows, insider)
    assert model["n_total"] == 20
    assert model["n_xxx"] == 6
    json.dumps(model)  # serializable
    assert model["features"]["smart_buys"]["2+"] == [6, 6]


def test_score_ranks_xxx_like_candidate_higher():
    rows, insider = _dataset()
    model = wm.train(rows, insider)
    xxx_like = wm.features_from_record(
        _rec("Q", 1.0, smart_buys=3, fp="HOT", sniper=80.0,
             insider_dir="accumulating", cluster=2.0, velocity=4.0),
        {"mint_Q": {"direction": "accumulating", "cluster_start": 2.0}})
    dud_like = wm.features_from_record(
        _rec("Z", 1.0, smart_buys=0, fp="COLD", sniper=30.0,
             insider_dir="diluting", cluster=15.0, velocity=1.0),
        {"mint_Z": {"direction": "diluting", "cluster_start": 15.0}})
    s_xxx, contrib_xxx = wm.score(model, xxx_like)
    s_dud, _c = wm.score(model, dud_like)
    assert s_xxx > s_dud
    assert s_xxx > 50.0 > s_dud
    # contributions explain the score
    feats = {c[0] for c in contrib_xxx}
    assert "smart_buys" in feats or "fp_verdict" in feats


def test_score_needs_minimum_data():
    s, c = wm.score({"n_xxx": 0, "n_total": 2, "features": {}}, {})
    assert s == 0.0 and c == []


def test_score_unseen_bin_backs_off_gracefully():
    rows, insider = _dataset()
    model = wm.train(rows, insider)
    s, _c = wm.score(model, {"smart_buys": "9", "nope": "x"})
    assert 0.0 <= s <= 100.0


# ---------------------------------------------------------------------------
# Threshold + proposals
# ---------------------------------------------------------------------------

def test_suggest_threshold_keeps_xxx_and_beats_baseline():
    rows, insider = _dataset()
    model = wm.train(rows, insider)
    sug = wm.suggest_threshold(model, rows, insider)
    assert sug is not None
    kx, tx = sug["xxx_kept"]
    assert kx / tx >= wm.RUNNER_KEEP_PCT
    assert sug["precision"] > sug["base_precision"]


def test_suggest_threshold_none_without_xxx():
    rows = [_rec("D%d" % i, 1.5) for i in range(12)]
    model = wm.train(rows, {})
    assert wm.suggest_threshold(model, rows, {}) is None


def test_analyze_winner_ranking_proposal_shape():
    rows, insider = _dataset()
    now = time.time()

    class Degen:
        pass

    props = wm.analyze_winner_ranking(Degen(), rows, None, now,
                                      "2026-10-01", min_n=10)
    assert len(props) == 1
    p = props[0]
    assert p["knob"] == wm.WINNER_KNOB == "winner_min_score"
    assert p["area"] == "ranking"
    assert p["status"] == "open"
    assert p["id"].startswith("T-")
    assert 0 < p["suggested"] <= 100
    assert "xxx" in p["rationale"]


def test_analyze_winner_ranking_empty_on_thin_data():
    rows = [_rec("D%d" % i, 1.5) for i in range(5)]

    class Degen:
        pass

    assert wm.analyze_winner_ranking(Degen(), rows, None, time.time(),
                                     "2026-10-01") == []
