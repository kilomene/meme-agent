"""Second-wave outcome ledger + trigger tuner (idea #3).

The second-wave lane is live but thinly measured: historically ~75 wave
signals fired with only a handful of paper closes, so "lean harder into
it" is NOT supported by the data. This module fixes the measurement
gap first:

1. ``record_wave`` snapshots every wave signal (pullback %, re-gain %,
   multiple at wave time).
2. ``settle`` joins settled waves (older than 24h) with the milestone
   watch's peak multiples -> forward multiple FROM the wave card.
3. ``analyze`` proposes trigger-knob adjustments from the settled
   ledger — proposals only, never applies.

Pure + dependency-free. Never raises. NOT wired: the daemon would call
``record_wave`` where ``launch.second_wave`` publishes and ``settle``
once a day with the milestone watch.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("wavefront")

_WATCH_KEY = "wavefront"          # state key: pending wave snapshots
_LEDGER_KEY = "wavefront_ledger"  # state key: settled wave outcomes
_MAX_PENDING = 500
_MAX_LEDGER = 2000
_SETTLE_AGE_S = 24 * 3600

# Trigger knobs this module can propose (mirrors secondwave.knobs()).
TUNABLE = ("second_wave_pullback_pct",
           "second_wave_releg_gain_pct",
           "second_wave_reentry_window_min")
KNOB_CLAMPS = {
    "second_wave_pullback_pct": (20.0, 70.0),
    "second_wave_releg_gain_pct": (20.0, 150.0),
    "second_wave_reentry_window_min": (15.0, 180.0),
}
KNOB_DEFAULTS = {
    "second_wave_pullback_pct": 40.0,
    "second_wave_releg_gain_pct": 50.0,
    "second_wave_reentry_window_min": 45.0,
}
_MIN_SETTLED = 10  # minimum settled waves before proposing anything


def _f(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def record_wave(state: Dict[str, Any], wave_event: Dict[str, Any],
                now: Optional[float] = None) -> bool:
    """Snapshot a published ``launch.second_wave`` card for later
    settling. Returns True when recorded. Bounded; never raises."""
    try:
        now = now if now is not None else time.time()
        ev = wave_event if isinstance(wave_event, dict) else {}
        mint = str(ev.get("mint") or "")
        if not mint:
            return False
        watch = state.setdefault(_WATCH_KEY, {})
        if not isinstance(watch, dict):
            return False
        watch[mint] = {
            "symbol": str(ev.get("symbol") or ""),
            "wave_ts": now,
            "wave_number": int(ev.get("wave_number") or 2),
            "pullback_pct": _f(ev.get("pullback_pct")),
            "relegain_pct": _f(ev.get("relegain_pct")),
            "peak_multiple": _f(ev.get("peak_multiple")),
            "multiple_now": _f(ev.get("multiple_now")),
            "safety_score": ev.get("safety_score"),
        }
        if len(watch) > _MAX_PENDING:
            for k in sorted(watch,
                            key=lambda k: _f(watch[k].get("wave_ts")))[
                    :len(watch) - _MAX_PENDING]:
                watch.pop(k, None)
        return True
    except Exception as exc:
        logger.warning("wavefront record failed: %s", exc)
        return False


def settle(state: Dict[str, Any],
           milestone_watch: Dict[str, Dict[str, Any]],
           now: Optional[float] = None) -> int:
    """Settle waves older than 24h against milestone peaks.

    forward_mult = milestone peak_mult / wave-time multiple_now, i.e.
    how much the coin ran AFTER the wave card. Settled rows move to
    ``state["wavefront_ledger"]`` (bounded, newest 2000). Returns the
    number settled. Never raises.
    """
    try:
        now = now if now is not None else time.time()
        watch = state.get(_WATCH_KEY)
        if not isinstance(watch, dict) or not watch:
            return 0
        mw = milestone_watch if isinstance(milestone_watch, dict) else {}
        ledger = state.setdefault(_LEDGER_KEY, [])
        if not isinstance(ledger, list):
            ledger = state[_LEDGER_KEY] = []
        settled = 0
        for mint in list(watch):
            rec = watch.get(mint)
            if not isinstance(rec, dict):
                watch.pop(mint, None)
                continue
            if now - _f(rec.get("wave_ts")) < _SETTLE_AGE_S:
                continue
            mrec = mw.get(mint) or {}
            peak = _f(mrec.get("peak_mult"))
            mnow = _f(rec.get("multiple_now"))
            fwd = round(peak / mnow, 3) if peak > 0 and mnow > 0 else 0.0
            ledger.append({
                "mint": mint, "symbol": rec.get("symbol") or "",
                "wave_ts": rec.get("wave_ts"),
                "wave_number": rec.get("wave_number"),
                "pullback_pct": rec.get("pullback_pct"),
                "relegain_pct": rec.get("relegain_pct"),
                "peak_multiple": rec.get("peak_multiple"),
                "multiple_now": mnow,
                "forward_mult": fwd,
                "hit_2x": fwd >= 2.0,
            })
            watch.pop(mint, None)
            settled += 1
        if len(ledger) > _MAX_LEDGER:
            del ledger[:len(ledger) - _MAX_LEDGER]
        return settled
    except Exception as exc:
        logger.warning("wavefront settle failed: %s", exc)
        return 0


def report(state: Dict[str, Any]) -> Dict[str, Any]:
    """Honest summary of the second-wave lane: signals, settled, hits.

    This is the number the user asked for — is the lane earning a
    bigger bet? Never raises.
    """
    try:
        ledger = state.get(_LEDGER_KEY) or []
        pending = state.get(_WATCH_KEY) or {}
        rows = [r for r in ledger if isinstance(r, dict)]
        n = len(rows)
        hits = sum(1 for r in rows if r.get("hit_2x"))
        fwds = sorted(_f(r.get("forward_mult")) for r in rows)
        med = fwds[n // 2] if n else 0.0
        return {
            "waves_settled": n,
            "waves_pending": len(pending) if isinstance(pending, dict)
            else 0,
            "hit_2x": hits,
            "hit_rate": round(hits / n, 3) if n else 0.0,
            "median_forward_mult": round(med, 2),
            "max_forward_mult": round(fwds[-1], 2) if n else 0.0,
            "verdict": ("unproven — %d settled waves, need %d" % (n, _MIN_SETTLED)
                        if n < _MIN_SETTLED else
                        ("earning a bigger bet" if hits / n >= 0.3
                         else "not earning a bigger bet")),
        }
    except Exception as exc:
        logger.warning("wavefront report failed: %s", exc)
        return {"verdict": "unknown"}


def analyze(degen: Any,
            rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Propose trigger-knob adjustments from settled waves.

    Compares the 2x-hit rate of waves in the top vs bottom half of each
    trigger dimension (pullback depth, re-gain strength). A knob is
    proposed only when one side clearly outperforms with >= 10 settled
    waves. Proposals only — same shape as feedback proposals, but this
    module never writes them anywhere; wiring decides the store.
    Never raises.
    """
    proposals: List[Dict[str, Any]] = []
    try:
        rows = [r for r in (rows or []) if isinstance(r, dict)]
        if len(rows) < _MIN_SETTLED:
            return proposals
        dims = (("pullback_pct", "second_wave_pullback_pct", -1),
                ("relegain_pct", "second_wave_releg_gain_pct", 1))
        for dim, knob, _direction in dims:
            vals = sorted(_f(r.get(dim)) for r in rows)
            if len(vals) < _MIN_SETTLED:
                continue
            cut = vals[len(vals) // 2]
            lo = [r for r in rows if _f(r.get(dim)) < cut]
            hi = [r for r in rows if _f(r.get(dim)) >= cut]
            if len(lo) < 4 or len(hi) < 4:
                continue
            hr_lo = sum(1 for r in lo if r.get("hit_2x")) / len(lo)
            hr_hi = sum(1 for r in hi if r.get("hit_2x")) / len(hi)
            # Pullback: deeper pullbacks (hi side) winning -> raise the
            # bar; shallow winning -> lower it. Re-gain: stronger
            # re-gain (hi side) winning -> raise the bar.
            if hr_hi - hr_lo >= 0.25:
                winner, cur = "deeper" if dim == "pullback_pct" \
                    else "stronger", None
            elif hr_lo - hr_hi >= 0.25:
                winner, cur = "shallower" if dim == "pullback_pct" \
                    else "weaker", None
            else:
                continue
            try:
                cur = float(getattr(degen, knob,
                                    KNOB_DEFAULTS[knob]))
            except Exception:
                cur = KNOB_DEFAULTS[knob]
            lo_c, hi_c = KNOB_CLAMPS[knob]
            step = (hi_c - lo_c) / 10.0
            if "deeper" in winner or "stronger" in winner:
                sug = min(hi_c, cur + step)
            else:
                sug = max(lo_c, cur - step)
            if abs(sug - cur) < 1e-9:
                continue
            proposals.append({
                "knob": knob,
                "area": "second_wave",
                "current": round(cur, 2),
                "suggested": round(sug, 2),
                "rationale": (
                    "%s %s waves hit 2x %.0f%% vs %.0f%% for %s ones "
                    "(%d settled). Nudging %s %.0f -> %.0f." % (
                        dim.replace("_pct", ""), winner,
                        max(hr_hi, hr_lo) * 100, min(hr_hi, hr_lo) * 100,
                        "shallow" if winner in ("deeper", "stronger")
                        else "extreme", len(rows), knob, cur, sug)),
                "evidence": {"dim": dim, "cut": round(cut, 1),
                             "hit_rate_hi": round(hr_hi, 3),
                             "hit_rate_lo": round(hr_lo, 3),
                             "settled": len(rows)},
                "status": "open",
            })
        return proposals
    except Exception as exc:
        logger.warning("wavefront analyze failed: %s", exc)
        return proposals
