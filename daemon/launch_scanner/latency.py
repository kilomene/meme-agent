"""Entry-timing measurement: detection latency, first sighting -> first card.

Measurement only — never trades, never gates, never touches the broker.
For each candidate coin we record:

  first_seen_ts — first observation of the mint/pool in the scanner
                  (``LaunchScanner._consider``, where ``state["seen"]``
                  gets its ``first_ts`` entry).
  first_card_ts — first emitted card that reaches the user: an instant
                  card (``launch.early``, ``launch.detected``,
                  ``launch.lowcap_runner``, ``momentum.alert``) OR a
                  digest item flushed by ``_flush_early_digest`` —
                  whichever first.
  detection_latency_min = (first_card_ts - first_seen_ts) / 60.0

Per-coin state follows the milestones tracker pattern: bounded and kept
in the daemon's own persisted state (``state["latency_coins"]``). A
rolling window of the last N carded coins (N =
``degen.latency_window_coins``, default 100) drives the median/p90 stats
surfaced in the funnel summary and the read-only ``/latency`` command.

Coins seen before this module deployed have no ``first_seen_ts``: their
latency is recorded as unknown (``None``) — never a crash, never a fake
zero. The journal path (``latency_min`` on the card event payloads, a
nullable optional field in the event schemas) carries the same value per
coin in the signal journal.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("forex_agent.daemon.launch_scanner.latency")

_COINS_KEY = "latency_coins"
_WINDOW_KEY = "latency_window"
_MAX_COINS = 500


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def enabled(degen: Any) -> bool:
    """Entry-timing tracker is ON unless explicitly disabled.

    Follows the codebase's getattr-with-default convention so no config
    schema change is required.
    """
    if degen is None:
        return True
    return bool(getattr(degen, "latency_enabled", True))


def window_size(degen: Any) -> int:
    """Rolling-window size for median/p90 (``degen.latency_window_coins``)."""
    try:
        n = int(getattr(degen, "latency_window_coins", 100) or 100)
    except (TypeError, ValueError):
        n = 100
    return max(1, n)


def _coins(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    coins = state.get(_COINS_KEY)
    if not isinstance(coins, dict):
        coins = {}
        state[_COINS_KEY] = coins
    return coins


def _bound_coins(coins: Dict[str, Dict[str, Any]]) -> None:
    """Bound per-coin records: oldest first-carded (or never-carded) drop."""
    if len(coins) <= _MAX_COINS:
        return
    ordered = sorted(coins.items(),
                     key=lambda kv: (_f(kv[1].get("first_card_ts"))
                                    or 1e18, _f(kv[1].get("ts"))))
    for old_mint, _ in ordered[:len(coins) - _MAX_COINS]:
        coins.pop(old_mint, None)


def record_first_seen(state: Dict[str, Any], degen: Any, mint: str,
                      now: Optional[float] = None,
                      symbol: Optional[str] = None) -> bool:
    """Record the first observation of a mint/pool. Idempotent per mint.

    Returns True when the coin is now tracked. A coin that already has a
    first-seen keeps it — the FIRST observation is the latency baseline.
    """
    try:
        if not enabled(degen) or not isinstance(state, dict):
            return False
        mint = str(mint or "").strip()
        if not mint:
            return False
        now = float(now) if now is not None else time.time()
        coins = _coins(state)
        rec = coins.get(mint)
        if rec is None:
            coins[mint] = {
                "symbol": str(symbol or "") or mint[:8],
                "first_seen_ts": now,
                "first_card_ts": None,
                "latency_min": None,
                "source": None,
                "ts": now,
            }
            _bound_coins(coins)
            return True
        if rec.get("first_seen_ts") is None and rec.get("first_card_ts") is None:
            # Observed before the card module recorded anything.
            rec["first_seen_ts"] = now
            if symbol and not rec.get("symbol"):
                rec["symbol"] = str(symbol)
        return True
    except Exception:
        logger.exception("latency record_first_seen failed for %s", mint)
        return False


def _push_window(state: Dict[str, Any], degen: Any, mint: str,
                 rec: Dict[str, Any], latency_min: float,
                 now: float) -> None:
    window = state.get(_WINDOW_KEY)
    if not isinstance(window, list):
        window = []
        state[_WINDOW_KEY] = window
    window.append({
        "mint": mint,
        "symbol": rec.get("symbol") or mint[:8],
        "latency_min": round(latency_min, 2),
        "source": rec.get("source") or "",
        "card_ts": now,
    })
    size = window_size(degen)
    if len(window) > size:
        del window[:len(window) - size]


def record_first_card(state: Dict[str, Any], degen: Any, mint: str,
                      now: Optional[float] = None,
                      symbol: Optional[str] = None,
                      source: Optional[str] = None,
                      assume_seen: bool = False) -> Optional[float]:
    """Record the first user-visible card for a mint; return its latency.

    Idempotent: the FIRST card wins — an instant card and a later digest
    flush for the same mint keep the instant card's time, and vice versa.
    Returns latency in minutes, or ``None`` when the coin was never
    recorded as first-seen (pre-deploy coins -> unknown, never a crash,
    never a fake zero). With ``assume_seen=True`` (alert-on-first-sight
    lanes such as momentum stage-1), a missing first-seen is treated as
    now -> ~0 latency.
    """
    try:
        if not enabled(degen) or not isinstance(state, dict):
            return None
        mint = str(mint or "").strip()
        if not mint:
            return None
        now = float(now) if now is not None else time.time()
        coins = _coins(state)
        rec = coins.get(mint)
        if rec is not None and rec.get("first_card_ts"):
            # Already carded — the first card keeps its latency.
            return rec.get("latency_min")
        if rec is None:
            rec = {
                "symbol": str(symbol or "") or mint[:8],
                "first_seen_ts": None,
                "first_card_ts": None,
                "latency_min": None,
                "source": None,
                "ts": now,
            }
            coins[mint] = rec
        if symbol and not rec.get("symbol"):
            rec["symbol"] = str(symbol)
        first_seen = rec.get("first_seen_ts")
        if first_seen is None and assume_seen:
            first_seen = now
            rec["first_seen_ts"] = now
        rec["first_card_ts"] = now
        if source:
            rec["source"] = str(source)
        if first_seen is not None:
            latency_min = max(0.0, (now - _f(first_seen)) / 60.0)
            rec["latency_min"] = latency_min
            _push_window(state, degen, mint, rec, latency_min, now)
        else:
            # Backfill: seen before this module deployed -> unknown.
            rec["latency_min"] = None
        _bound_coins(coins)
        return rec["latency_min"]
    except Exception:
        logger.exception("latency record_first_card failed for %s", mint)
        return None


def latency_for(state: Dict[str, Any], mint: Optional[str]) -> Optional[float]:
    """Current per-coin latency in minutes (None = unknown / never carded)."""
    try:
        if not isinstance(state, dict) or not mint:
            return None
        rec = (state.get(_COINS_KEY) or {}).get(str(mint))
        if not isinstance(rec, dict):
            return None
        return rec.get("latency_min")
    except Exception:
        return None


def _median(values: List[float]) -> Optional[float]:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return (float(s[mid - 1]) + float(s[mid])) / 2.0


def _p90(values: List[float]) -> Optional[float]:
    """Nearest-rank 90th percentile: smallest value >= 90% of observations."""
    s = sorted(values)
    n = len(s)
    if n == 0:
        return None
    k = max(1, math.ceil(0.9 * n))
    return float(s[k - 1])


def summary(state: Dict[str, Any],
            degen: Any = None) -> Dict[str, Any]:
    """Rolling-window stats: median + p90 latency + slowest examples.

    ``n`` counts carded coins with a KNOWN latency in the window; unknown
    (pre-deploy) cards are counted separately and never pollute the math.
    Never raises.
    """
    try:
        coins = state.get(_COINS_KEY) or {}
        if not isinstance(coins, dict):
            coins = {}
        window = state.get(_WINDOW_KEY) or []
        if not isinstance(window, list):
            window = []
        known = [w for w in window
                 if isinstance(w, dict) and w.get("latency_min") is not None]
        latencies = [_f(w["latency_min"]) for w in known]
        unknown = sum(1 for rec in coins.values()
                      if isinstance(rec, dict) and rec.get("first_card_ts")
                      and rec.get("latency_min") is None)
        slowest = sorted(known,
                         key=lambda w: _f(w.get("latency_min")),
                         reverse=True)[:3]
        out = {
            "n": len(latencies),
            "median_min": _median(latencies),
            "p90_min": _p90(latencies),
            "unknown": unknown,
            "window_coins": window_size(degen) if degen is not None else None,
            "slowest": [{
                "mint": w.get("mint") or "",
                "symbol": w.get("symbol") or "?",
                "latency_min": _f(w.get("latency_min")),
                "source": w.get("source") or "?",
            } for w in slowest],
        }
        return out
    except Exception:
        logger.exception("latency summary failed")
        return {"n": 0, "median_min": None, "p90_min": None, "unknown": 0,
                "window_coins": None, "slowest": []}


def funnel_bits(state: Dict[str, Any], degen: Any = None) -> str:
    """Short stat fragment for the funnel/summary log line ("" when empty)."""
    try:
        s = summary(state, degen)
        if not s["n"]:
            return ""
        return " lat_n=%d med=%.1fm p90=%.1fm" % (
            s["n"], s["median_min"], s["p90_min"])
    except Exception:
        return ""
