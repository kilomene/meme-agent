"""Winner-likeness ranker: P(xxx | entry features).

Trains a binned naive-Bayes model on REAL paper closes (the same
``backtest_dataset.jsonl`` the feedback loop reads) joined with insider
ledger rows by mint, and scores candidates 0-100 by resemblance to past
xxx runners (peak >= 10x, not rugged) versus duds.

Why naive Bayes with bins: it is explainable (every score carries its top
contributing factors for the card), needs no dependencies, trains on
dozens of closes instead of thousands, and degrades gracefully — unknown
features simply contribute nothing.

Entry-time features (all present on paper records today):
  safety_score, sniper_score, smart_buys, fp_verdict, rising_polls,
  buy_pressure, hot_deployer, narrative_heat, narrative tag, lane label,
  source, insider direction + starting cluster share, early 15m velocity
  (derived from fingerprint snapshots).

Outputs
-------
* :func:`train` -> serializable model dict (JSON-safe).
* :func:`score` -> ``(0-100 score, [(feature, bin, lift), ...])``.
* :func:`analyze_winner_ranking` -> proposal dicts in the feedback
  ``_mk`` shape suggesting a ``winner_min_score`` knob value
  (proposals-only, like the rest of the loop).

Wiring (on user go-ahead only) needs three one-line additions to
``feedback.py``: ``"winner_min_score"`` in ``TUNABLE``, plus entries in
``KNOB_CLAMPS`` (0-100) and ``KNOB_DEFAULTS`` (0 = off). Until then this
module only trains, scores, and proposes — it never changes live
decisions.

This module is NOT wired into the live daemon.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("forex_agent.daemon.launch_scanner.winner_model")

XXX_MULT = 10.0        # peak multiple that counts as "did xxx"
RUNNER_MULT = 2.0      # peak multiple that counts as a runner (context)
RUNNER_KEEP_PCT = 0.80  # a threshold proposal must keep >= this of xxx
WINNER_KNOB = "winner_min_score"
WINNER_KNOB_DEFAULT = 0.0   # 0 = ranker off until the user tunes it


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Feature extraction (entry-time only — never uses outcome fields)
# ---------------------------------------------------------------------------

def _bin_safety(score: Any) -> str:
    if score is None:
        return "unknown"
    s = _f(score, -1)
    if s <= 1:
        return "0-1"
    if s <= 20:
        return "2-20"
    if s <= 50:
        return "21-50"
    return "51-100"


def _bin_sniper(score: Any) -> str:
    if score is None:
        return "none"
    s = _f(score)
    if s < 30:
        return "<30"
    if s < 50:
        return "30-50"
    if s < 70:
        return "50-70"
    return "70+"


def _early_velocity_15m(rec: Dict[str, Any]) -> Optional[float]:
    """Mcap growth over the first ~15 minutes of fingerprint snapshots."""
    try:
        snaps = rec.get("snapshots") or []
        if not isinstance(snaps, list) or len(snaps) < 2:
            return None
        t0 = _f(snaps[0].get("t"))
        m0 = _f(snaps[0].get("mcap_usd"))
        if m0 <= 0:
            return None
        best = None
        for s in snaps[1:]:
            if _f(s.get("t")) - t0 > 900:
                break
            m = _f(s.get("mcap_usd"))
            if m > 0:
                best = m
        if best is None:
            return None
        return round(best / m0, 3)
    except Exception:
        return None


def _bin_velocity(v: Optional[float]) -> str:
    if v is None:
        return "unknown"
    if v < 1.5:
        return "<1.5x"
    if v < 3.0:
        return "1.5-3x"
    return "3x+"


def features_from_record(rec: Dict[str, Any],
                         insider_by_mint: Optional[Dict[str, Dict[str, Any]]] = None
                         ) -> Dict[str, str]:
    """Binned categorical entry features for one paper record."""
    feats: Dict[str, str] = {}
    try:
        saf = rec.get("safety") or {}
        feats["safety_score"] = _bin_safety(saf.get("score"))
        feats["sniper_score"] = _bin_sniper(rec.get("sniper_score"))
        sig = rec.get("entry_signals") or {}
        if not isinstance(sig, dict):
            sig = {}
        nb = int(_f(sig.get("smart_buys")))
        feats["smart_buys"] = "2+" if nb >= 2 else ("1" if nb == 1 else "0")
        feats["fp_verdict"] = str(sig.get("fp_verdict") or "none").upper()
        rp = int(_f(sig.get("rising_polls")))
        feats["rising_polls"] = "3+" if rp >= 3 else ("2" if rp == 2 else "0-1")
        bp = _f(sig.get("buy_pressure"), -1)
        feats["buy_pressure"] = ("unknown" if bp < 0 else
                                 "<0.4" if bp < 0.4 else
                                 "0.4-0.6" if bp < 0.6 else "0.6+")
        feats["hot_deployer"] = "yes" if sig.get("hot_deployer") else "no"
        nh = _f(rec.get("narrative_heat"), 1.0)
        feats["narrative_heat"] = ("hot" if nh > 1.3 else
                                  "warm" if nh > 1.0 else "cold")
        narrs = rec.get("narratives") or ["other"]
        feats["narrative"] = str(narrs[0]) if narrs else "other"
        feats["lane"] = str(rec.get("label") or "launch")
        feats["source"] = str(rec.get("source") or "unknown")
        feats["wave"] = "2+" if int(_f(rec.get("wave"), 1)) >= 2 else "1"
        feats["early_velocity_15m"] = _bin_velocity(_early_velocity_15m(rec))
        ins = (insider_by_mint or {}).get(str(rec.get("mint") or ""))
        if isinstance(ins, dict):
            feats["insider_direction"] = str(ins.get("direction")
                                             or "unknown")
            cs = _f(ins.get("cluster_start"), -1)
            feats["insider_cluster"] = ("unknown" if cs < 0 else
                                        "<5%" if cs < 5 else
                                        "5-20%" if cs <= 20 else ">20%")
        else:
            feats["insider_direction"] = "unknown"
            feats["insider_cluster"] = "unknown"
    except Exception as exc:
        logger.warning("feature extraction failed: %s", exc)
    return feats


def is_xxx(rec: Dict[str, Any]) -> bool:
    """The label we actually care about: did xxx, not a rug."""
    try:
        return (not rec.get("rug")) and _f(rec.get("peak_mult")) >= XXX_MULT
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Training: binned naive Bayes with Laplace smoothing
# ---------------------------------------------------------------------------

def train(rows: List[Dict[str, Any]],
          insider_by_mint: Optional[Dict[str, Dict[str, Any]]] = None
          ) -> Dict[str, Any]:
    """Train on paper records. Returns a JSON-serializable model::

        {"features": {feat: {bin: [xxx, total]}}, "n_xxx": int,
         "n_total": int, "trained_ts": float}
    """
    model: Dict[str, Any] = {"features": {}, "n_xxx": 0, "n_total": 0,
                             "trained_ts": time.time()}
    try:
        for rec in rows or []:
            if not isinstance(rec, dict):
                continue
            xxx = is_xxx(rec)
            feats = features_from_record(rec, insider_by_mint)
            model["n_total"] += 1
            if xxx:
                model["n_xxx"] += 1
            for feat, b in feats.items():
                bins = model["features"].setdefault(feat, {})
                cell = bins.setdefault(b, [0, 0])
                cell[1] += 1
                if xxx:
                    cell[0] += 1
    except Exception as exc:
        logger.warning("winner model train failed: %s", exc)
    return model


def _log_odds(p: float) -> float:
    p = min(0.999, max(0.001, p))
    return math.log(p / (1.0 - p))


def score(model: Dict[str, Any],
          features: Dict[str, str],
          ) -> Tuple[float, List[Tuple[str, str, float]]]:
    """Score binned features 0-100 by resemblance to past xxx.

    Returns ``(score, [(feature, bin, lift), ...])`` sorted by |lift|
    desc — the top entries explain the score on a future card. Unknown
    features contribute nothing (fail-neutral, never fail-closed: this is
    a ranker, not a gate).
    """
    contribs: List[Tuple[str, str, float]] = []
    try:
        n_xxx = int(model.get("n_xxx") or 0)
        n_total = int(model.get("n_total") or 0)
        if n_total < 5:
            return 0.0, []
        # Laplace-smoothed prior.
        prior = (n_xxx + 1.0) / (n_total + 2.0)
        total_lo = _log_odds(prior)
        feat_maps = model.get("features") or {}
        for feat, b in (features or {}).items():
            bins = feat_maps.get(feat)
            if not isinstance(bins, dict):
                continue
            # Unseen bin: back off to the feature's marginal xxx rate.
            cell = bins.get(b)
            if cell is None:
                tx = sum(c[0] for c in bins.values())
                tt = sum(c[1] for c in bins.values())
                p = (tx + 1.0) / (tt + 2.0)
            else:
                p = (cell[0] + 1.0) / (cell[1] + 2.0)
            lift = _log_odds(p) - _log_odds(prior)
            if abs(lift) > 0.05:
                contribs.append((feat, b, round(lift, 3)))
            total_lo += lift
        prob = 1.0 / (1.0 + math.exp(-total_lo))
        contribs.sort(key=lambda c: -abs(c[2]))
        return round(prob * 100.0, 1), contribs
    except Exception as exc:
        logger.warning("winner score failed: %s", exc)
        return 0.0, []


def score_record(model: Dict[str, Any], rec: Dict[str, Any],
                 insider_by_mint: Optional[Dict[str, Dict[str, Any]]] = None
                 ) -> Tuple[float, List[Tuple[str, str, float]]]:
    """Score a paper record (for backtesting the ranker itself)."""
    return score(model, features_from_record(rec, insider_by_mint))


# ---------------------------------------------------------------------------
# Threshold analysis -> proposals (proposals-only, like the feedback loop)
# ---------------------------------------------------------------------------

def _proposal_id(knob: str, suggested: Any, day: str) -> str:
    digest = hashlib.sha256(
        ("%s|%s|%s" % (knob, suggested, day)).encode()).hexdigest()
    return "T-" + digest[:6].upper()


def suggest_threshold(model: Dict[str, Any],
                      rows: List[Dict[str, Any]],
                      insider_by_mint: Optional[Dict[str, Dict[str, Any]]] = None,
                      keep_pct: float = RUNNER_KEEP_PCT
                      ) -> Optional[Dict[str, Any]]:
    """Find a winner_min_score threshold worth proposing.

    Picks the threshold that maximizes xxx precision while keeping >=
    ``keep_pct`` of past xxx. Returns None when no threshold beats the
    no-filter baseline on enough evidence.
    """
    try:
        scored = []
        for rec in rows or []:
            if not isinstance(rec, dict):
                continue
            s, _c = score_record(model, rec, insider_by_mint)
            scored.append((s, is_xxx(rec)))
        xxx_total = sum(1 for _s, x in scored if x)
        if xxx_total < 3 or len(scored) < 10:
            return None
        base_prec = xxx_total / len(scored)
        best = None
        for thr in (30.0, 40.0, 50.0, 60.0, 70.0):
            kept = [(s, x) for s, x in scored if s >= thr]
            if not kept:
                continue
            kept_xxx = sum(1 for _s, x in kept if x)
            if kept_xxx / xxx_total < keep_pct:
                continue
            prec = kept_xxx / len(kept)
            if prec > base_prec * 1.5 and (
                    best is None or prec > best["precision"]):
                best = {"threshold": thr, "precision": round(prec, 3),
                        "base_precision": round(base_prec, 3),
                        "xxx_kept": [kept_xxx, xxx_total],
                        "coins_kept": [len(kept), len(scored)]}
        return best
    except Exception as exc:
        logger.warning("threshold suggestion failed: %s", exc)
        return None


def analyze_winner_ranking(degen: Any,
                           recs: List[Dict[str, Any]],
                           insider_rows: Optional[List[Dict[str, Any]]],
                           now: float,
                           day: str,
                           min_n: int = 20) -> List[Dict[str, Any]]:
    """Propose a ``winner_min_score`` knob value from real outcomes.

    Proposal dicts match the feedback ``_mk`` shape (id/knob/area/current/
    suggested/rationale/evidence/created_ts/status). NOTE: storing via
    ``feedback.add_proposals`` drops knobs not in ``feedback.TUNABLE`` —
    wiring requires adding ``"winner_min_score"`` there plus clamp/default
    entries (three lines, on user go-ahead).
    """
    proposals: List[Dict[str, Any]] = []
    try:
        recs = [r for r in (recs or []) if isinstance(r, dict)]
        if len(recs) < max(10, min_n):
            return proposals
        insider_by_mint: Dict[str, Dict[str, Any]] = {}
        for row in insider_rows or []:
            if isinstance(row, dict) and row.get("mint"):
                insider_by_mint[str(row["mint"])] = row
        model = train(recs, insider_by_mint)
        if model["n_xxx"] < 3:
            return proposals  # no xxx in history yet — nothing to learn
        sug = suggest_threshold(model, recs, insider_by_mint)
        if not sug:
            return proposals
        try:
            current = float(getattr(degen, WINNER_KNOB,
                                    WINNER_KNOB_DEFAULT))
        except Exception:
            current = WINNER_KNOB_DEFAULT
        thr = float(sug["threshold"])
        if thr == current:
            return proposals
        kx, tx = sug["xxx_kept"]
        kc, tc = sug["coins_kept"]
        proposals.append({
            "id": _proposal_id(WINNER_KNOB, round(thr, 2), day),
            "knob": WINNER_KNOB,
            "area": "ranking",
            "current": round(current, 2),
            "suggested": round(thr, 2),
            "rationale": (
                "Coins scoring >= %.0f on winner-likeness (resemblance to "
                "past 10x+ runners at entry) did xxx %.1f%% of the time vs "
                "%.1f%% with no filter, while keeping %d/%d past xxx. "
                "Holding low-scoring confirmed runners out of the top tier "
                "cuts rugs without starving the xxx." % (
                    thr, sug["precision"] * 100,
                    sug["base_precision"] * 100, kx, tx)),
            "evidence": {"threshold": thr,
                         "precision": sug["precision"],
                         "base_precision": sug["base_precision"],
                         "xxx_kept": [kx, tx], "coins_kept": [kc, tc],
                         "outcomes": len(recs),
                         "note": "winner_min_score is tunable via /tune; "
                                 "default 0 = off"},
            "created_ts": now,
            "status": "open",
        })
    except Exception as exc:
        logger.warning("analyze_winner_ranking failed: %s", exc)
    return proposals


# ---------------------------------------------------------------------------
# Live-candidate adapter: score a runner AT CONFIRM TIME
# ---------------------------------------------------------------------------

def synth_record_from_candidate(cand: Optional[Dict[str, Any]],
                                fp: Optional[Dict[str, Any]] = None,
                                snaps: Optional[List[Dict[str, Any]]] = None,
                                label: str = "launch",
                                wave: int = 1) -> Dict[str, Any]:
    """Build a paper-record-shaped dict from a LIVE candidate so the
    trained model can score it at confirm time.

    ``cand`` is the judge candidate, ``fp`` the fingerprint entry from
    ``state["fingerprints"][mint]``, ``snaps`` fingerprint-series
    snapshots in record format. Missing fields stay absent — the model
    treats unknowns as neutral (fail-neutral ranker, never a gate).
    """
    cand = cand if isinstance(cand, dict) else {}
    fp = fp if isinstance(fp, dict) else {}
    saf = cand.get("safety")
    if not isinstance(saf, dict):
        saf = {}
    confirm = cand.get("confirm")
    if not isinstance(confirm, dict):
        confirm = {}
    sig = {
        "smart_buys": cand.get("smart_buys"),
        "fp_verdict": fp.get("verdict"),
        "rising_polls": confirm.get("rising_polls"),
        "buy_pressure": fp.get("buy_pressure"),
        "hot_deployer": cand.get("hot_deployer"),
    }
    return {
        "mint": str(cand.get("mint") or ""),
        "safety": {"score": saf.get("score")},
        "sniper_score": cand.get("sniper_score"),
        "entry_signals": sig,
        "narrative_heat": cand.get("narrative_heat"),
        "narratives": cand.get("narratives") or ["other"],
        "label": label,
        "source": cand.get("source") or "unknown",
        "wave": wave,
        "snapshots": snaps or [],
    }
