"""Sniper-tier runner-probability ranking ("monitor wide, alert narrow").

Scores every quality-passed candidate 0-100 from signals that already
exist and are live — fingerprint composite, tracked smart-wallet buys,
early velocity (rising polls), buy/sell pressure, and hot-deployer
history as a small boost. Pure logic: no I/O, no trading, no RPC calls
(the deployer check reads only the already-cached deployer, never
resolving — history must never slow the hot path).

Fail-safe throughout: a missing signal contributes 0, never a penalty
and never a boost. Unknown/unscored handling is unchanged — this module
only ever sees coins that ALREADY passed ``_instant_verdict``.

Weights and the tier threshold are ``degen.*`` config knobs so the
Sunday review can tune them on evidence without a code change. Safety
gates never self-tune and are untouched by this module.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import deployers
from . import smartmoney


def _num(value: Any, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def rank_candidate(degen: Any, signals: Dict[str, Any]) -> Dict[str, Any]:
    """Score a quality-passed candidate 0-100 for runner probability.

    ``signals`` keys (all optional; missing == 0 contribution):
      fp_composite  0-100 fingerprint composite (None when too early)
      fp_verdict    "HOT" / "WARMING" / "COLD" / None
      smart_buys    int — tracked wallets that bought this mint
      rising_polls  int — consecutive rising observations
      buy_pressure  0-1 buys/(buys+sells) (None when unknown)
      hot_deployer  bool — deployer already cached AND hot in registry
      narrative_heat  0.5-2.0 narrative heat multiplier (#5). Heat above
                      1.0 earns up to ``narrative_boost_max`` bonus points;
                      heat <= 1.0 is NEVER a penalty (soft signal only).

    Returns {"score", "tier" ("sniper"/"standard"), "reasons",
             "threshold", "enabled"}. Tier is "sniper" when enabled and
    score >= sniper_tier_threshold. Reasons are short human phrases for
    the card's one-line "why".
    """
    signals = signals or {}
    enabled = bool(getattr(degen, "sniper_enabled", True))
    threshold = _num(getattr(degen, "sniper_tier_threshold", 50.0), 50.0)
    w_fp = _num(getattr(degen, "sniper_weight_fp", 30.0), 30.0)
    w_smart = _num(getattr(degen, "sniper_weight_smart", 35.0), 35.0)
    w_vel = _num(getattr(degen, "sniper_weight_velocity", 20.0), 20.0)
    w_press = _num(getattr(degen, "sniper_weight_pressure", 10.0), 10.0)
    boost_dep = _num(getattr(degen, "sniper_boost_deployer", 5.0), 5.0)

    parts: List[float] = []
    reasons: List[str] = []

    # Fingerprint composite 0-100 -> weighted share. HOT/WARMING earn a
    # reason line; COLD still contributes its (small) score, no praise.
    fp = signals.get("fp_composite")
    if isinstance(fp, (int, float)) and fp > 0:
        parts.append(w_fp * _clamp01(float(fp) / 100.0))
        verdict = (signals.get("fp_verdict") or "").upper()
        if verdict == "HOT":
            reasons.append("HOT fingerprint")
        elif verdict == "WARMING":
            reasons.append("warming fingerprint")

    # Tracked smart-wallet buys: the strongest single edge we have.
    # 2+ wallets -> full weight; 1 wallet -> half.
    try:
        n_smart = int(signals.get("smart_buys") or 0)
    except (TypeError, ValueError):
        n_smart = 0
    if n_smart >= 2:
        parts.append(w_smart)
        reasons.append("%d smart buys" % n_smart)
    elif n_smart == 1:
        parts.append(w_smart * 0.5)
        reasons.append("1 smart buy")

    # Early velocity: consecutive rising polls. Needs at least 2 to
    # count as momentum; a single uptick is noise.
    try:
        r = int(signals.get("rising_polls") or 0)
    except (TypeError, ValueError):
        r = 0
    if r >= 3:
        parts.append(w_vel)
        reasons.append("rising momentum")
    elif r == 2:
        parts.append(w_vel * 0.65)
        reasons.append("rising momentum")
    elif r == 1:
        parts.append(w_vel * 0.3)

    # Buy/sell pressure 0-1 -> weighted share. Missing stays 0.
    p = signals.get("buy_pressure")
    if isinstance(p, (int, float)):
        parts.append(w_press * _clamp01(float(p)))

    # Hot deployer: small flat boost, never a penalty. Unknown/cold
    # deployers are neutral by construction (deployers.is_hot).
    if signals.get("hot_deployer"):
        parts.append(boost_dep)
        reasons.append("hot deployer")

    # Narrative heat boost (#5): a coin riding a hot narrative earns up
    # to ``narrative_boost_max`` extra points, scaled by how far above
    # neutral (1.0) its heat is. Heat <= 1.0 (cold/neutral/unknown)
    # contributes 0 — a cold narrative never blocks or penalizes an
    # otherwise qualified coin. Toggle: ``narrative_boost_enabled``.
    if bool(getattr(degen, "narrative_boost_enabled", True)):
        heat = signals.get("narrative_heat")
        if isinstance(heat, (int, float)) and float(heat) > 1.0:
            max_boost = _num(getattr(degen, "narrative_boost_max", 8.0),
                             8.0)
            if max_boost > 0:
                boost = min(max_boost,
                            max_boost * (float(heat) - 1.0))
                parts.append(boost)
                reasons.append("hot narrative x%.2f" % float(heat))

    score = round(min(100.0, sum(parts)), 1)
    tier = "sniper" if (enabled and score >= threshold) else "standard"
    # Compact signal snapshot — the #6 entry learner's raw material.
    # Recorded on paper positions at entry so closed trades can be
    # attributed back to the signals that picked them.
    snap = signals if isinstance(signals, dict) else {}
    snapshot = {
        "smart_buys": _num(snap.get("smart_buys"), 0.0),
        "fp": _num(snap.get("fp_composite"), 0.0),
        "fp_verdict": str(snap.get("fp_verdict") or ""),
        "rising_polls": _num(snap.get("rising_polls"), 0.0),
        "buy_pressure": _num(snap.get("buy_pressure"), 0.0),
        "hot_deployer": bool(snap.get("hot_deployer")),
        "heat": _num(snap.get("narrative_heat"), 1.0),
    }
    return {
        "score": score,
        "tier": tier,
        "reasons": reasons,
        "threshold": threshold,
        "enabled": enabled,
        "signals_snapshot": snapshot,
    }


def sniper_line(result: Optional[Dict[str, Any]]) -> str:
    """One-line "why this made the tier" for the instant card."""
    if not result:
        return ""
    why = " + ".join(result.get("reasons") or []) or "quality-passed"
    return "🎯 sniper %.1f — %s" % (float(result.get("score") or 0), why)


def collect_signals(owner: Any, state: Dict[str, Any], degen: Any,
                    mint: Optional[str],
                    entry: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Gather the live signals for one candidate. Never raises, never
    does I/O beyond cheap local reads: fingerprint tracker (in-memory),
    smart-money activity (in-memory), mcap history (in-memory), and the
    deployer cache (in-memory; ``deployers.is_hot`` reads the local
    registry file — no RPC, never a fresh resolution here).

    Every lookup is guarded: on any trouble the signal stays at its
    zero default, which can only lower the score, never inflate it.
    """
    signals: Dict[str, Any] = {
        "fp_composite": None,
        "fp_verdict": None,
        "smart_buys": 0,
        "rising_polls": 0,
        "buy_pressure": None,
        "hot_deployer": False,
    }
    # Fingerprint composite / verdict / pressure / rising streak.
    try:
        tracker = getattr(owner, "_fp_tracker", None)
        sc = tracker.score(mint) if (tracker and mint) else None
        if sc:
            signals["fp_composite"] = sc.get("composite")
            signals["fp_verdict"] = sc.get("verdict")
            signals["buy_pressure"] = sc.get("buy_pressure")
            try:
                signals["rising_polls"] = int(sc.get("rising_buys") or 0)
            except (TypeError, ValueError):
                pass
    except Exception:
        pass
    # Tracked smart-wallet buys (distinct wallets, recent activity).
    try:
        if mint:
            labels = smartmoney.wallet_labels_for(state, mint) or []
            signals["smart_buys"] = len(labels)
    except Exception:
        pass
    # Velocity from the seen-entry mcap history: trailing consecutive
    # rises. Takes the max with the fingerprint rising streak.
    try:
        hist = (entry or {}).get("mcap") or []
        rises = 0
        for i in range(len(hist) - 1, 0, -1):
            try:
                prev = float(hist[i - 1][1] or 0)
                cur = float(hist[i][1] or 0)
            except (TypeError, ValueError, IndexError):
                break
            if cur > prev > 0:
                rises += 1
            else:
                break
        signals["rising_polls"] = max(int(signals["rising_polls"] or 0),
                                      rises)
    except Exception:
        pass
    # Hot deployer: cache-only. A mint whose deployer was never resolved
    # (or resolved cold/unknown) earns no boost — neutral, never penalty.
    try:
        cache = state.get("deployer_cache") or {}
        cent = cache.get(mint) if isinstance(cache, dict) else None
        dep = (cent or {}).get("deployer") if isinstance(cent, dict) else None
        if dep and deployers.is_hot(dep, degen):
            signals["hot_deployer"] = True
    except Exception:
        pass
    return signals
