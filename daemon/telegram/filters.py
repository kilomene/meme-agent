"""Personal alert filters — per-chat overlays on the global gates.

A filter can only TIGHTEN or reshape what this chat receives; it can
never weaken a global safety gate. The scanner already enforces the
standing gates (known safety report, no hard fails, min score) before
an event is ever published — events reaching the telegram daemon have
passed them. These filters sit on top:

* ``min_score`` — personal minimum safety score; events below it are
  skipped for this chat.
* ``lane`` — momentum lane preference: "lowcap" | "established" |
  "both" (default "both"). Only affects ``momentum.alert`` events.
* ``quiet`` — quiet hours {"start": h, "end": h} in Africa/Lagos
  (UTC+1, no DST). Signal cards during quiet hours are queued and
  delivered as ONE digest when quiet ends. Safety-critical events
  (kill switch, health, trades, positions) are never filtered or
  queued — see SIGNAL_EVENTS for the exact set.

Filters live in the telegram daemon's own persisted state under
``state["personal_filters"][<chat_id>]``. Command handlers mutate the
live state dict (``ctx.security.state``); the daemon's cycle-end save
persists it — handlers must NOT call ``save_daemon_state`` themselves,
or the cycle-end save would clobber their write.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

# Events these filters may touch. Everything else (kill_switch.*,
# health.*, trade.*, position.*, daemon.*, worker.*, market.*) always
# sends immediately.
SIGNAL_EVENTS = (
    "launch.early",
    "launch.detected",
    "launch.lowcap_runner",
    "launch.milestone",
    "launch.second_wave",
    "launch.late_runner",
    "momentum.alert",
    "degen.warning",
    "launch.early_digest",
)

LANES = ("lowcap", "established", "both")

QUIET_QUEUE_MAX = 50

_KIND_LABELS = {
    "launch.early": "⚡ early",
    "launch.detected": "🚀 confirmed",
    "launch.lowcap_runner": "🎯 lowcap",
    "launch.milestone": "🏆 milestone",
    "launch.second_wave": "🔁 second wave",
    "launch.late_runner": "🐢 late runner",
    "momentum.alert": "📈 momentum",
    "degen.warning": "⚠️ warning",
    "launch.early_digest": "🗞 digest",
}


# ----------------------------------------------------------------------
# storage
# ----------------------------------------------------------------------
def get_filters(state: Dict[str, Any], chat_id: Any) -> Dict[str, Any]:
    """Return this chat's filter dict, creating defaults if absent."""
    bucket = state.setdefault("personal_filters", {})
    flt = bucket.get(str(chat_id))
    if not isinstance(flt, dict):
        flt = {"min_score": None, "lane": "both", "quiet": None}
        bucket[str(chat_id)] = flt
    # Backfill keys for state written by older builds.
    flt.setdefault("min_score", None)
    flt.setdefault("lane", "both")
    flt.setdefault("quiet", None)
    return flt


# ----------------------------------------------------------------------
# quiet hours
# ----------------------------------------------------------------------
def wat_hour(now: Optional[float] = None) -> int:
    """Current hour in Africa/Lagos. WAT is UTC+1 with no DST, so plain
    gmtime arithmetic is exact and needs no tz database."""
    t = time.gmtime(now if now is not None else time.time())
    return (t.tm_hour + 1) % 24


def in_quiet(flt: Dict[str, Any], hour: Optional[int] = None) -> bool:
    """True if `hour` (default: now, WAT) falls inside the quiet range."""
    quiet = flt.get("quiet")
    if not isinstance(quiet, dict):
        return False
    try:
        start = int(quiet.get("start"))
        end = int(quiet.get("end"))
    except (TypeError, ValueError):
        return False
    h = wat_hour() if hour is None else int(hour) % 24
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end  # wraps midnight, e.g. 23-7


def parse_quiet_range(text: str) -> Optional[Dict[str, int]]:
    """Parse 'HH-HH' (0-23 each, start != end) or return None."""
    parts = (text or "").strip().split("-")
    if len(parts) != 2:
        return None
    try:
        start, end = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= start <= 23 and 0 <= end <= 23):
        return None
    if start == end:
        return None
    return {"start": start, "end": end}


# ----------------------------------------------------------------------
# event gating
# ----------------------------------------------------------------------
def _event_score(event: Dict[str, Any]) -> Optional[float]:
    score = event.get("safety_score")
    try:
        return float(score) if score is not None else None
    except (TypeError, ValueError):
        return None


def event_passes(event: Dict[str, Any], flt: Dict[str, Any]) -> bool:
    """True if this signal event may be sent to the filtered chat.

    Never raises. For ``launch.early_digest`` the items list is filtered
    in place and the event fails when nothing remains.
    """
    try:
        ev = event.get("event")
        if ev == "momentum.alert":
            want = str(flt.get("lane") or "both").lower()
            if want in ("lowcap", "established"):
                lane = str(event.get("lane") or "established").lower()
                if lane != want:
                    return False
        ms = flt.get("min_score")
        if ms is not None:
            try:
                bar = float(ms)
            except (TypeError, ValueError):
                return True  # corrupt filter value: fail open, never block
            if ev == "launch.early_digest":
                # Tiered digests carry items_safe / items_fresh; legacy
                # journal events carry a single items list. Filter every
                # tier list in place. `count` stays the total queued —
                # the honest "showing X of Y" footer is computed from
                # the rendered lists, not from count.
                kept_total = 0
                for key in ("items_safe", "items_fresh"):
                    lst = event.get(key)
                    if not isinstance(lst, list):
                        continue
                    kept = [it for it in lst
                            if isinstance(it, dict)
                            and _score_of(it) is not None
                            and _score_of(it) >= bar]
                    kept_total += len(kept)
                    event[key] = kept
                if isinstance(event.get("items"), list):
                    kept = [it for it in event["items"]
                            if isinstance(it, dict)
                            and _score_of(it) is not None
                            and _score_of(it) >= bar]
                    kept_total += len(kept)
                    event["items"] = kept
                    event["count"] = len(kept)
                if not kept_total:
                    return False
                return True
            score = _event_score(event)
            if score is None or score < bar:
                return False
        return True
    except Exception:
        return True  # a filter must never kill an alert


def _score_of(item: Dict[str, Any]) -> Optional[float]:
    try:
        s = item.get("safety_score")
        return float(s) if s is not None else None
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# quiet queue
# ----------------------------------------------------------------------
def summarize_for_quiet(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Compact, send-safe summary of a queued signal event."""
    try:
        ev = str(event.get("event") or "")
        item: Dict[str, Any] = {
            "kind": _KIND_LABELS.get(ev, ev or "?"),
            "symbol": str(event.get("symbol") or "?")[:20],
            "mint": str(event.get("mint") or ""),
            "score": _event_score(event),
            "ts": event.get("ts") or "",
        }
        if ev == "launch.early_digest":
            item["count"] = int(event.get("count") or 0)
        return item
    except Exception:
        return None


def queue_quiet(state: Dict[str, Any], item: Dict[str, Any]) -> None:
    """Append to the quiet queue, bounded (oldest dropped first)."""
    q = state.setdefault("quiet_queue", [])
    q.append(item)
    if len(q) > QUIET_QUEUE_MAX:
        del q[: len(q) - QUIET_QUEUE_MAX]


def format_quiet_digest(items: List[Dict[str, Any]]) -> str:
    """One card summarizing everything held during quiet hours."""
    lines = ["🌙 QUIET-HOURS DIGEST — %d signal%s held"
             % (len(items), "" if len(items) == 1 else "s"),
             "Sent now that quiet hours ended. Nothing was traded."]
    for it in items:
        sym = it.get("symbol") or "?"
        kind = it.get("kind") or "?"
        score = it.get("score")
        tag = (" [score %s/100]" % score) if score is not None else " [score n/a]"
        count = it.get("count")
        extra = (" ×%d" % count) if count else ""
        lines.append("• %s %s%s%s" % (kind, sym, tag, extra))
        mint = it.get("mint")
        if mint:
            lines.append("  CA: %s" % mint)
    lines.append("⚠️ Verify before acting — these are UNCONFIRMED signals.")
    return "\n".join(lines)
