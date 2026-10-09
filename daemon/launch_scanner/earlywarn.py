"""Early-warning lane: UNCONFIRMED alerts for blocked-but-moving coins.

The main alert stream stays strict (score >= 50, no hard fails). This lane
is separate and honest: when a coin the quality gate HOLDS is still
showing real run signals, the user gets one clearly-labeled warning card
so they can watch/decide early — instead of hearing about the run after
it's over.

Two doors:
  launch   — a held launch candidate with hot first-sight momentum or a
             HOT/WARMING fingerprint.
  momentum — a held momentum qualifier (it already passed the momentum
             gates: 20-300% 24h, volume/liquidity floors — that IS the
             run signal).

Rules:
  * Floor: known safety score >= degen.warn_min_score (default 1.0).
    Unknown/unscored coins stay silent — same as everywhere else.
  * Never warns on can't-exit coins (honeypot / cannot-sell / sell tax):
    alerting a coin you literally cannot exit helps nobody.
  * Other hard fails (insider clusters, concentration, mint authority…)
    are SHOWN on the card, not hidden — the exact danger is printed.
  * Per-coin cooldown (default 6h) + global rate limit (default 4/hour)
    so the lane can never become a firehose.
  * Publishes {"event": "degen.warning"} only. Never paper-trades,
    never touches the main cooldowns or stage-2 entries.

Every public function is fail-safe: never raises into the scanner loop.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("forex_agent.daemon.launch_scanner.earlywarn")

_CANT_EXIT_MARKERS = ("honeypot", "cannot sell", "can't sell", "sell tax")


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _cfg(degen: Any, name: str, default: Any) -> Any:
    return getattr(degen, name, default) if degen is not None else default


def enabled(degen: Any) -> bool:
    return bool(_cfg(degen, "warn_enabled", True))


def cant_exit(report: Optional[Dict[str, Any]]) -> bool:
    """True when the report says the coin can't be sold/exited."""
    try:
        fails = (report or {}).get("fails") or []
        for fail in fails:
            text = str(fail).lower()
            if any(m in text for m in _CANT_EXIT_MARKERS):
                return True
        return False
    except Exception:
        return False


def dangers(report: Optional[Dict[str, Any]]) -> List[str]:
    """Exact blocking reasons to print on the card (fails + flags)."""
    try:
        rep = report or {}
        out: List[str] = []
        for item in (rep.get("fails") or []) + (rep.get("flags") or []):
            text = str(item).strip()
            if text and text not in out:
                out.append(text)
        return out[:6]
    except Exception:
        return []


def floor_ok(degen: Any, report: Optional[Dict[str, Any]]) -> bool:
    """Known score at/above the warning floor (default 1.0)."""
    try:
        score = _f((report or {}).get("score"))
        # score 0/None = unknown or total failure -> silent
        if score <= 0:
            return False
        return score >= _f(_cfg(degen, "warn_min_score", 1.0))
    except Exception:
        return False


def launch_trigger(degen: Any, c: Dict[str, Any],
                   fp_verdict: Optional[str]) -> Optional[str]:
    """Why a held launch candidate deserves a warning, or None.

    fp_verdict: the fingerprint tracker's verdict for this mint
    ("HOT"/"WARMING"/"COLD"/None) when available.
    """
    try:
        if fp_verdict in ("HOT", "WARMING"):
            return "hot_fingerprint"
        buys = _f((c or {}).get("buys_m5"))
        vol = _f((c or {}).get("vol_m5"))
        if buys >= _f(_cfg(degen, "warn_min_buys_m5", 20.0)) and \
           vol >= _f(_cfg(degen, "warn_min_vol_m5_usd", 5000.0)):
            return "early_momentum"
        return None
    except Exception:
        return None


def _cooldown_ok(state: Dict[str, Any], mint: str, now: float,
                 hours: float) -> bool:
    try:
        cd = state.get("warn_cooldown") or {}
        last = _f(cd.get(mint))
        return (now - last) >= hours * 3600.0
    except Exception:
        return True


def _rate_ok(state: Dict[str, Any], now: float, max_per_hour: float) -> bool:
    try:
        stamps = [t for t in (state.get("warn_stamps") or [])
                  if now - _f(t) < 3600.0]
        state["warn_stamps"] = stamps
        return len(stamps) < max(1, int(max_per_hour))
    except Exception:
        return True


def maybe_warn(state: Dict[str, Any], degen: Any, door: str, symbol: str,
               mint: str, chain: str, mcap: float, score: float,
               danger_list: List[str], trigger: str,
               extra: Optional[Dict[str, Any]] = None) -> bool:
    """Publish one degen.warning event if all lane rules pass.

    Returns True when the warning was published. Never raises.
    """
    try:
        if not enabled(degen):
            return False
        if not mint or not trigger:
            return False
        now = time.time()
        cooldown_h = _f(_cfg(degen, "warn_cooldown_hours", 0.5))
        if not _cooldown_ok(state, mint, now, cooldown_h):
            return False
        if not _rate_ok(state, now, _f(_cfg(degen, "warn_max_per_hour", 4.0))):
            return False
        from agent import events as event_bus  # noqa: PLC0415
        payload: Dict[str, Any] = {
            "event": "degen.warning",
            "door": door,                      # "launch" | "momentum"
            "confirmed": False,
            "unconfirmed": True,
            "chain": chain or "",
            "mint": mint,
            "symbol": symbol or "",
            "usd_market_cap": _f(mcap),
            "safety_score": score,
            "dangers": list(danger_list or [])[:6],
            "trigger": trigger,
            "note": ("UNCONFIRMED early warning — held by the safety "
                     "gate, but showing run signals. Not vetted."),
        }
        for key in ("chg_24h_pct", "lane", "tier", "url", "name"):
            if extra and extra.get(key) is not None:
                payload[key] = extra[key]
        event_bus.publish(payload)
        cd = state.setdefault("warn_cooldown", {})
        if isinstance(cd, dict):
            cd[mint] = now
        stamps = state.setdefault("warn_stamps", [])
        if isinstance(stamps, list):
            stamps.append(now)
        logger.warning("WARN [%s] %s %s (%s) score=%s: %s",
                       door, symbol, mint, trigger, score,
                       "; ".join(danger_list or [])[:100])
        return True
    except Exception:
        logger.exception("earlywarn maybe_warn failed")
        return False
