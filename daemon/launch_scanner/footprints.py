"""Footprint signals (measurement only, user decision 2026-10-02).

The track-vs-trade gap: the bot tracks ~393 coins but only trades a
handful, because at entry time winners and rugs look identical. Two
"footprint" signals try to read intent *before* the run, from the
fingerprint time series the scanner already keeps per mint:

  1. holder_velocity — new holders per minute. A real crowd arriving
     (50 new holders in 10 minutes) is the footprint of a run; a
     manufactured chart has buys but no crowd.
  2. accumulation — volume spiking while the price sits flat. Quiet
     loading by big buyers before the move.

MEASUREMENT ONLY. measure() reads the per-mint fingerprint series
(obs tuples: ts, mcap, price, vol_m5, buys_m5, sells_m5, liq,
holders|None) and records raw values plus provisional flags into
state["footprint_signals"] (bounded 500, 7-day TTL). NOTHING gates on
the results: entries, alerts, caps, and picks are untouched.

Coverage honesty: holder counts refresh only for lowcap-tracked Solana
mints (rate-limited RugCheck calls), so holder_velocity is often
"unknown" for confirmed runners — recorded distinctly, because the
week verdict must answer how often the signal even has data, not just
whether it worked when present.

After ~1 week the ledger answers, per signal: how often was data
present, and did flagged coins win more than unflagged ones? The user
decides whether any signal becomes a gate; nothing self-activates.

Never raises. No I/O — pure reads of the in-memory series.
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("footprints")

STATE_KEY = "footprint_signals"  # mint -> {ts, velocity, accumulation}
_MAX_ENTRIES = 500
_LEDGER_TTL_S = 7 * 24 * 3600

# obs tuple indexes in FingerprintTracker._series
_TS, _MCAP, _PRICE, _VOL_M5 = 0, 1, 2, 3
_HOLDERS = 7

_CROWD = "crowd"      # holders arriving fast (provisional threshold)
_QUIET = "quiet"      # holder data present, no crowd
_LOADING = "loading"  # volume spiking while price flat (provisional)
_NO = "no"            # series present, no accumulation shape
_UNKNOWN = "unknown"  # insufficient data — recorded, never faked


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN -> default


def _knob(degen: Any, name: str, default: Any) -> Any:
    try:
        v = getattr(degen, name, default)
        return default if v is None else v
    except Exception:
        return default


def _obs_list(series: Any) -> List[Tuple[float, ...]]:
    try:
        return [tuple(float(v) if v is not None else None for v in o)
                for o in (series or []) if o and len(o) > _HOLDERS]
    except Exception:
        return []


def holder_velocity(obs: List[Tuple[float, ...]],
                    crowd_per_min: float = 1.0,
                    min_window_min: float = 5.0) -> Dict[str, Any]:
    """New holders per minute over the observed window.

    crowd   — holders/min >= crowd_per_min (provisional threshold; the
              week verdict sets the real one).
    quiet   — holder data present but below the crowd threshold.
    unknown — fewer than 2 holder observations, or the observations
              span less than min_window_min (too short to mean
              anything). Never faked from thin data.
    Raw values (holders_first/last, elapsed_min, per_min, growth_pct)
    are always recorded so the week analysis can re-threshold freely.
    """
    try:
        pts = [(o[_TS], o[_HOLDERS]) for o in obs
               if o[_TS] is not None and o[_HOLDERS] is not None
               and o[_HOLDERS] > 0]
        rec: Dict[str, Any] = {"verdict": _UNKNOWN,
                               "holders_first": None,
                               "holders_last": None,
                               "elapsed_min": None,
                               "per_min": None,
                               "growth_pct": None}
        if len(pts) < 2:
            rec["detail"] = "only %d holder observation(s)" % len(pts)
            return rec
        t0, h0 = pts[0]
        t1, h1 = pts[-1]
        elapsed_min = (t1 - t0) / 60.0
        rec["holders_first"] = int(h0)
        rec["holders_last"] = int(h1)
        rec["elapsed_min"] = round(elapsed_min, 1)
        if elapsed_min < min_window_min:
            rec["detail"] = "window %.1f min < %.0f min minimum" % (
                elapsed_min, min_window_min)
            return rec
        per_min = (h1 - h0) / elapsed_min
        growth_pct = (h1 - h0) / h0 * 100.0 if h0 > 0 else 0.0
        rec["per_min"] = round(per_min, 2)
        rec["growth_pct"] = round(growth_pct, 1)
        if per_min >= crowd_per_min:
            rec["verdict"] = _CROWD
            rec["detail"] = "+%.1f holders/min over %.0f min" % (
                per_min, elapsed_min)
        else:
            rec["verdict"] = _QUIET
            rec["detail"] = "%+.1f holders/min over %.0f min" % (
                per_min, elapsed_min)
        return rec
    except Exception:
        logger.exception("footprints holder_velocity failed")
        return {"verdict": _UNKNOWN, "detail": "check error"}


def accumulation(obs: List[Tuple[float, ...]],
                 vol_accel_min: float = 2.0,
                 price_flat_pct: float = 10.0,
                 vol_floor: float = 1000.0) -> Dict[str, Any]:
    """Volume spiking while price sits flat — quiet loading?

    loading — latest 5m volume >= vol_accel_min x the earlier-poll mean
              (floored so tiny bases don't fake it) AND the price moved
              no more than price_flat_pct from first to last.
    no      — series long enough but the shape isn't there.
    unknown — fewer than 3 polls.
    """
    try:
        pts = [(o[_TS], o[_PRICE], o[_VOL_M5]) for o in obs
               if o[_TS] is not None]
        rec: Dict[str, Any] = {"verdict": _UNKNOWN,
                               "vol_accel": None,
                               "price_change_pct": None}
        if len(pts) < 3:
            rec["detail"] = "only %d poll(s)" % len(pts)
            return rec
        vols = [p[2] or 0.0 for p in pts]
        last_vol = vols[-1]
        base = sum(vols[:-1]) / max(1, len(vols) - 1)
        vol_accel = last_vol / max(vol_floor, base) if base >= 0 else 0.0
        p0, p1 = pts[0][1] or 0.0, pts[-1][1] or 0.0
        price_change_pct = ((p1 - p0) / p0 * 100.0) if p0 > 0 else None
        rec["vol_accel"] = round(min(vol_accel, 99.0), 2)
        rec["price_change_pct"] = (round(price_change_pct, 1)
                                   if price_change_pct is not None else None)
        flat = (price_change_pct is not None
                and abs(price_change_pct) <= price_flat_pct)
        if vol_accel >= vol_accel_min and flat:
            rec["verdict"] = _LOADING
            rec["detail"] = "vol %.1fx, price %+.1f%%" % (
                vol_accel, price_change_pct)
        else:
            rec["verdict"] = _NO
            rec["detail"] = "vol %.1fx, price %s" % (
                vol_accel,
                ("%+.1f%%" % price_change_pct)
                if price_change_pct is not None else "n/a")
        return rec
    except Exception:
        logger.exception("footprints accumulation failed")
        return {"verdict": _UNKNOWN, "detail": "check error"}


def measure(state: Dict[str, Any], series_map: Any, degen: Any,
            mint: str, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Measure the 2 footprint signals for one scored runner.

    Measurement only: records {ts, velocity, accumulation} into
    state["footprint_signals"][mint] (bounded 500, 7-day TTL). Returns
    the record, or None when disabled / no mint / no series. Never
    raises, never gates.
    """
    try:
        if not mint or not _knob(degen, "footprints_enabled", True):
            return None
        now = now if now is not None else time.time()
        series = None
        try:
            series = (series_map or {}).get(str(mint))
        except Exception:
            series = None
        obs = _obs_list(series)

        velocity = holder_velocity(
            obs,
            crowd_per_min=float(
                _knob(degen, "fp_velocity_crowd_per_min", 1.0) or 1.0),
            min_window_min=float(
                _knob(degen, "fp_velocity_min_window_min", 5.0) or 5.0),
        )
        accum = accumulation(
            obs,
            vol_accel_min=float(
                _knob(degen, "fp_accum_vol_accel", 2.0) or 2.0),
            price_flat_pct=float(
                _knob(degen, "fp_accum_price_flat_pct", 10.0) or 10.0),
        )

        rec = {"ts": now, "mint": str(mint),
               "velocity": velocity, "accumulation": accum}
        ledger = state.get(STATE_KEY)
        if not isinstance(ledger, dict):
            ledger = {}
            state[STATE_KEY] = ledger
        ledger[str(mint)] = rec
        # Prune stale, then bound by recency.
        cutoff = now - _LEDGER_TTL_S
        for m in [m for m, r in ledger.items()
                  if not isinstance(r, dict) or _f(r.get("ts")) < cutoff]:
            ledger.pop(m, None)
        if len(ledger) > _MAX_ENTRIES:
            for m in sorted(ledger, key=lambda k: _f(ledger[k].get("ts"))
                            )[:len(ledger) - _MAX_ENTRIES]:
                ledger.pop(m, None)
        return rec
    except Exception:
        logger.exception("footprints measure failed for %s", mint)
        return None


def summary(rec: Optional[Dict[str, Any]]) -> str:
    """One-line signal summary, e.g. 'VELOCITY:crowd ACCUM:loading'."""
    try:
        if not isinstance(rec, dict):
            return "no footprint data"
        return "VELOCITY:%s ACCUM:%s" % (
            (rec.get("velocity") or {}).get("verdict", "?"),
            (rec.get("accumulation") or {}).get("verdict", "?"),
        )
    except Exception:
        return "no footprint data"
