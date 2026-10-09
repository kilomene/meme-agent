"""Sustained-velocity tier (idea #4).

The confirmed-runner alert fires on multi-poll chart proof — but a
proof can go stale minutes later. This module answers one question:
**is the run still alive?** A runner that is still holding most of its
peak N minutes after confirmation earns a second, louder tier
(``SUSTAINED RUNNER``); one that already faded stays at the normal
tier. Nothing here blocks the first alert — it only decides whether
the top tier fires.

Pure + dependency-free. All inputs are plain dicts / series lists of
``(ts, mcap)`` tuples. Never raises.

Wiring (on user go-ahead) would: on each ``launch.detected``, schedule
a re-check ``sustained_delay_min`` later; if ``assess`` still says
SUSTAINED, publish the top-tier card + paper entry. Knobs below are
read from degen with the documented defaults.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("sustained")

# Knob names (read from degen at wiring time; defaults here).
KNOB_DELAY_MIN = "sustained_delay_min"            # default 30.0
KNOB_PEAK_HOLD_PCT = "sustained_peak_hold_pct"    # default 70.0
KNOB_LOOKBACK_MIN = "sustained_lookback_min"      # default 15.0
KNOB_MAX_DD_PCT = "sustained_max_drawdown_pct"    # default 30.0

DEFAULTS = {
    KNOB_DELAY_MIN: 30.0,
    KNOB_PEAK_HOLD_PCT: 70.0,
    KNOB_LOOKBACK_MIN: 15.0,
    KNOB_MAX_DD_PCT: 30.0,
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def _num(degen: Any, name: str) -> float:
    try:
        return float(getattr(degen, name, DEFAULTS[name]))
    except Exception:
        return DEFAULTS[name]


def series_stats(series: List[Tuple[float, float]],
                 now: Optional[float] = None,
                 lookback_min: float = 15.0
                 ) -> Dict[str, Any]:
    """Peak / current / drawdown / short-trend over a mcap series.

    Returns ``{"peak", "last", "drawdown_pct", "trend_pct",
    "age_min"}`` where trend is the move over the lookback window.
    Empty series -> all zeros. Never raises.
    """
    out = {"peak": 0.0, "last": 0.0, "drawdown_pct": 0.0,
           "trend_pct": 0.0, "age_min": 0.0}
    try:
        pts = [(float(t), float(m)) for t, m in (series or [])
               if _f(m) > 0]
        if not pts:
            return out
        pts.sort(key=lambda p: p[0])
        now = now if now is not None else time.time()
        peak = max(m for _, m in pts)
        last = pts[-1][1]
        out["peak"] = peak
        out["last"] = last
        out["drawdown_pct"] = (round((peak - last) / peak * 100.0, 1)
                               if peak > 0 else 0.0)
        out["age_min"] = round((now - pts[0][0]) / 60.0, 1)
        cutoff = now - lookback_min * 60.0
        base = next((m for t, m in pts if t >= cutoff), pts[0][1])
        out["trend_pct"] = (round((last - base) / base * 100.0, 1)
                            if base > 0 else 0.0)
        return out
    except Exception as exc:
        logger.warning("series stats failed: %s", exc)
        return out


def classify(hold_pct: float, trend_pct: float, drawdown_pct: float,
             peak_hold_pct: float = 70.0,
             max_drawdown_pct: float = 30.0) -> Tuple[str, str]:
    """Verdict + reason from pre-computed stats.

    Pure function — the shared rule for both series-based ``assess``
    and snapshot-based checks. ``hold_pct`` is current-as-%-of-peak;
    ``trend_pct`` the short-window trend; ``drawdown_pct`` the peak
    drawdown. Never raises.
    """
    try:
        if hold_pct < 100.0 - max_drawdown_pct:
            return ("DEAD",
                    "drawdown %.0f%% exceeds %.0f%%" %
                    (drawdown_pct, max_drawdown_pct))
        if hold_pct >= peak_hold_pct and trend_pct >= -10.0:
            return ("SUSTAINED",
                    "holding %.0f%% of peak, trend %+.0f%%" %
                    (hold_pct, trend_pct))
        return ("FADING",
                "holding %.0f%% of peak, trend %+.0f%% — weakening" %
                (hold_pct, trend_pct))
    except Exception:
        return "DEAD", "classify error"


def assess(series: List[Tuple[float, float]],
           now: Optional[float] = None,
           peak_hold_pct: float = 70.0,
           lookback_min: float = 15.0,
           max_drawdown_pct: float = 30.0
           ) -> Tuple[str, Dict[str, Any]]:
    """Is the run still alive? Returns ``(verdict, evidence)``.

    Verdicts: ``SUSTAINED`` (still holding >= peak_hold_pct of peak and
    no worse than max_drawdown_pct with a non-collapsing short trend),
    ``FADING`` (alive but weakening), ``DEAD`` (rugged or fully retraced).
    Never raises.
    """
    try:
        st = series_stats(series, now, lookback_min)
        peak, last = st["peak"], st["last"]
        ev = dict(st)
        if peak <= 0 or last <= 0:
            ev["reason"] = "no price data"
            return "DEAD", ev
        hold_pct = last / peak * 100.0
        ev["hold_pct"] = round(hold_pct, 1)
        verdict, reason = classify(hold_pct, st["trend_pct"],
                                   st["drawdown_pct"], peak_hold_pct,
                                   max_drawdown_pct)
        ev["reason"] = reason
        return verdict, ev
    except Exception as exc:
        logger.warning("assess failed: %s", exc)
        return "DEAD", {"reason": "error"}


def due_for_check(state: Dict[str, Any], mint: str,
                  degen: Any, now: Optional[float] = None) -> bool:
    """True when a confirmed coin's sustained re-check is due.

    ``state["sustained_watch"][mint] = {"confirm_ts": ...}`` is seeded
    at wiring time when the confirmed alert fires; the re-check fires
    once, ``sustained_delay_min`` later. Pure state read; never raises.
    """
    try:
        now = now if now is not None else time.time()
        watch = state.get("sustained_watch") or {}
        rec = watch.get(mint)
        if not isinstance(rec, dict) or rec.get("checked"):
            return False
        delay = _num(degen, KNOB_DELAY_MIN) * 60.0
        return now - _f(rec.get("confirm_ts")) >= delay
    except Exception:
        return False


def seed_watch(state: Dict[str, Any], mint: str, symbol: str,
               now: Optional[float] = None,
               max_entries: int = 500) -> None:
    """Seed the sustained re-check watch at confirm time. Bounded."""
    try:
        now = now if now is not None else time.time()
        watch = state.setdefault("sustained_watch", {})
        if not isinstance(watch, dict):
            return
        watch[mint] = {"symbol": symbol, "confirm_ts": now,
                       "checked": False}
        if len(watch) > max_entries:
            for k in sorted(watch,
                            key=lambda k: _f(watch[k].get("confirm_ts")))[
                    :len(watch) - max_entries]:
                watch.pop(k, None)
    except Exception as exc:
        logger.warning("sustained seed failed: %s", exc)


def mark_checked(state: Dict[str, Any], mint: str) -> None:
    """Mark the re-check done (fires once per coin)."""
    try:
        rec = (state.get("sustained_watch") or {}).get(mint)
        if isinstance(rec, dict):
            rec["checked"] = True
    except Exception:
        pass


def check(state: Dict[str, Any], degen: Any, mint: str,
          series: List[Tuple[float, float]],
          now: Optional[float] = None) -> Dict[str, Any]:
    """Full re-check: verdict + evidence + watch bookkeeping.

    Returns ``{"verdict", "evidence", "due"}``. Marks the coin checked
    whenever the check ran. Never raises.
    """
    try:
        now = now if now is not None else time.time()
        if not due_for_check(state, mint, degen, now):
            return {"verdict": "WAIT", "evidence": {}, "due": False}
        verdict, ev = assess(
            series, now,
            peak_hold_pct=_num(degen, KNOB_PEAK_HOLD_PCT),
            lookback_min=_num(degen, KNOB_LOOKBACK_MIN),
            max_drawdown_pct=_num(degen, KNOB_MAX_DD_PCT))
        mark_checked(state, mint)
        return {"verdict": verdict, "evidence": ev, "due": True}
    except Exception as exc:
        logger.warning("sustained check failed: %s", exc)
        return {"verdict": "DEAD", "evidence": {"reason": "error"},
                "due": False}
