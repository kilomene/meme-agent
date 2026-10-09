"""Runners recap: the day's PROVEN runners in one card, twice daily.

Reads the milestone watch (``milestones.tracked``): every tracked coin
whose ``peak_mult`` reached ``recap_min_mult`` (default 2.0x from its
card-time anchor) and that has not been recapped yet is collected, sorted
by peak multiple, and published as a single ``launch.runners_recap``
event. The Telegram renderer turns it into one 🏆 card.

Measurement/informational only: the recap never trades, never gates,
never opens positions. Each runner appears in exactly one recap
(``recap_sent`` is marked on the watch record when collected).

Times are WAT (Africa/Lagos = UTC+1, no DST — fixed offset is exact
year-round). Slots come from ``degen.recap_times`` (default
["08:00", "20:00"]). The slot key stored in state is the slot's
"YYYY-MM-DD HH:MM" so a daemon restart never re-sends a recap.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from daemon.launch_scanner import milestones

logger = logging.getLogger("forex_agent.daemon.launch_scanner.recap")

_STATE_KEY = "recap_last_slot"
# WAT = UTC+1, no daylight saving — fixed offset is correct year-round.
_WAT = timezone(timedelta(hours=1))


def enabled(degen: Any) -> bool:
    """The recap is ON unless explicitly disabled."""
    return bool(getattr(degen, "recap_enabled", True))


def _times(degen: Any) -> List[str]:
    raw = getattr(degen, "recap_times", None)
    out: List[str] = []
    if isinstance(raw, (list, tuple)):
        for t in raw:
            s = str(t or "").strip()
            try:
                hh, mm = s.split(":")
                h, m = int(hh), int(mm)
            except (ValueError, AttributeError):
                continue
            if 0 <= h <= 23 and 0 <= m <= 59:
                out.append("%02d:%02d" % (h, m))
    return sorted(set(out)) or ["08:00", "20:00"]


def _min_mult(degen: Any) -> float:
    try:
        return max(1.0, float(getattr(degen, "recap_min_mult", 2.0) or 2.0))
    except (TypeError, ValueError):
        return 2.0


def _max_items(degen: Any) -> int:
    try:
        return max(1, int(getattr(degen, "recap_max_items", 20) or 20))
    except (TypeError, ValueError):
        return 20


def _latest_slot(degen: Any, now: float) -> Optional[str]:
    """The most recent recap slot at or before ``now`` (WAT), as a key."""
    day = datetime.fromtimestamp(now, tz=_WAT)
    best: Optional[datetime] = None
    for t in _times(degen):
        h, m = int(t[:2]), int(t[3:5])
        slot = day.replace(hour=h, minute=m, second=0, microsecond=0)
        if slot.timestamp() <= now and (best is None or slot > best):
            best = slot
    return best.strftime("%Y-%m-%d %H:%M") if best else None


def due(state: Dict[str, Any], degen: Any, now: float) -> bool:
    """True when a recap slot has passed since the last sent recap."""
    try:
        if not enabled(degen):
            return False
        slot = _latest_slot(degen, now)
        if not slot:
            return False
        return state.get(_STATE_KEY) != slot
    except Exception:
        logger.exception("recap due check failed")
        return False


def collect(state: Dict[str, Any], degen: Any
            ) -> Tuple[List[Tuple[str, Dict[str, Any]]], int]:
    """Qualifying runners not yet recapped, highest peak first.

    Returns (shown, overflow): ``shown`` is capped at ``recap_max_items``;
    ``overflow`` is how many more qualified but didn't fit — they stay
    unmarked and lead the next recap, and the card says so honestly.

    Nothing is marked sent here: call :func:`mark_sent` only after the
    recap card has actually published, so a publish failure retries
    instead of silently dropping runners.
    """
    try:
        watch = milestones.tracked(state)
    except Exception:
        logger.exception("recap collect failed")
        return [], 0
    floor = _min_mult(degen)
    runners: List[Tuple[str, Dict[str, Any]]] = []
    for mint, rec in watch.items():
        if not isinstance(rec, dict) or rec.get("recap_sent"):
            continue
        try:
            peak = float(rec.get("peak_mult") or 0.0)
        except (TypeError, ValueError):
            peak = 0.0
        if peak >= floor:
            runners.append((mint, rec))
    runners.sort(key=lambda kv: float(kv[1].get("peak_mult") or 0.0),
                 reverse=True)
    cap = _max_items(degen)
    return runners[:cap], max(0, len(runners) - cap)


def mark_sent(runners: List[Tuple[str, Dict[str, Any]]]) -> None:
    """Mark collected runners as recapped. Call AFTER publish succeeds."""
    for _mint, rec in runners:
        try:
            rec["recap_sent"] = True
        except Exception:
            logger.exception("recap mark_sent failed")


def build_event(runners: List[Tuple[str, Dict[str, Any]]],
                slot: str, overflow: int = 0) -> Dict[str, Any]:
    """The ``launch.runners_recap`` event the Telegram renderer consumes."""
    items = []
    for mint, rec in runners:
        try:
            peak = float(rec.get("peak_mult") or 0.0)
        except (TypeError, ValueError):
            peak = 0.0
        items.append({
            "mint": mint,
            "symbol": str(rec.get("symbol") or mint[:8]),
            "chain": str(rec.get("chain") or "solana"),
            "peak_mult": round(peak, 2),
            "safety_score": rec.get("safety_score"),
            "unvetted": rec.get("safety_score") is None,
            "url": str(rec.get("url") or ""),
        })
    note = "only coins that actually ran — no maybes"
    return {"event": "launch.runners_recap", "slot": slot,
            "count": len(items), "items": items, "overflow": overflow,
            "note": note}


def maybe_send(config: Any, state: Dict[str, Any], now: float
               ) -> Optional[Dict[str, Any]]:
    """Publish the recap when a slot is due. Returns the event or None.

    The slot and the runners are marked sent ONLY after the publish
    succeeds — a publish failure leaves everything unmarked so the next
    cycle retries instead of silently dropping runners.
    """
    try:
        degen = config.degen
        if not due(state, degen, now):
            return None
        slot = _latest_slot(degen, now) or ""
        runners, overflow = collect(state, degen)
        if not runners:
            # Quiet slot: mark it sent so the cycle doesn't retry, but
            # don't publish an empty card — "monitor wide, alert narrow".
            state[_STATE_KEY] = slot
            logger.warning("RUNNERS RECAP [%s] no runners — card skipped",
                           slot)
            return None
        event = build_event(runners, slot, overflow)
        from agent import events as event_bus  # noqa: PLC0415
        event_bus.publish(event)
        mark_sent(runners)
        state[_STATE_KEY] = slot
        logger.warning("RUNNERS RECAP [%s] %d runners%s", slot,
                       event["count"],
                       " (+%d overflow)" % overflow if overflow else "")
        return event
    except Exception:
        logger.exception("recap send failed")
        return None
