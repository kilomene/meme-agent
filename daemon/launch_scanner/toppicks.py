"""Top Picks shortlist (user decision 2026-10-01).

The confirmed lane fires ~200+ alerts a day and only ~1% run 2x+ — no
human can choose from that. Once a day this module ranks the last 24h
of confirmed runners on every signal the bot already measures and
publishes ONE card with the top N (default 5): the shortlist to
actually consider gambling on.

Composite score (0-100), all inputs already computed at card time:
  sniper   30%  velocity + smart-money + buy pressure + fingerprints
  winner   25%  likeness to the bot's own past 10x+ runners
  holder   20%  accumulation vs distribution (holder growth vs mcap)
  smart    15%  tracked-wallet early buys (3+ buys = full marks)
  finger   10%  HOT=100 / WARMING=60 / COLD=20 / unknown=50

Measurement only, on top of everything: the 1/100 safety bar, hard
gates (honeypot / can't-sell / insider clusters), and the 4/hour cap
are untouched — this ranks alerted runners, it never admits new ones.
Picks are alert-only; the bot never auto-trades them. Yesterday's
picks are graded on the card (2x+ hit count, best multiple) from the
milestone peak tracker, so the shortlist earns or loses trust on
paper before anyone risks anything real. Never raises.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("toppicks")

WAT = timezone(timedelta(hours=1))

SCORE_KEY = "pick_scores"        # mint -> score snapshot at card time
LOG_KEY = "top_picks_log"        # list of past daily picks (for grading)
SLOT_KEY = "toppicks_last_slot"  # last WAT date a picks card was built

_MAX_SCORES = 500
_SCORE_TTL_S = 48 * 3600
_WINDOW_S = 24 * 3600

# (weight, label)
_WEIGHTS = (
    (0.30, "sniper"),
    (0.25, "winner"),
    (0.20, "holder"),
    (0.15, "smart"),
    (0.10, "finger"),
)

_FP_SCORE = {"HOT": 100.0, "WARMING": 60.0, "COLD": 20.0}


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN -> default


def record_scores(state: Dict[str, Any], mint: str,
                  snapshot: Dict[str, Any], now: Optional[float] = None
                  ) -> None:
    """Store one confirmed runner's card-time score snapshot.

    Called from _on_launch after the alert is published — only alerted
    runners are ever ranked. Bounded and TTL-pruned. Never raises.
    """
    try:
        if not mint or not isinstance(snapshot, dict):
            return
        now = now if now is not None else time.time()
        snap = dict(snapshot)
        snap["ts"] = now
        scores = state.get(SCORE_KEY)
        if not isinstance(scores, dict):
            scores = {}
            state[SCORE_KEY] = scores
        scores[str(mint)] = snap
        # Prune stale, then bound by recency.
        cutoff = now - _SCORE_TTL_S
        for m in [m for m, s in scores.items()
                  if not isinstance(s, dict)
                  or _f(s.get("ts")) < cutoff]:
            scores.pop(m, None)
        if len(scores) > _MAX_SCORES:
            for m in sorted(scores, key=lambda k: _f(scores[k].get("ts"))
                            )[:len(scores) - _MAX_SCORES]:
                scores.pop(m, None)
    except Exception:
        logger.exception("toppicks record failed for %s", mint)


def composite(rec: Dict[str, Any]) -> Tuple[float, List[str]]:
    """Weighted 0-100 composite + the top-2 contributing 'why' lines."""
    try:
        parts = {
            "sniper": _f(rec.get("sniper"), 50.0),
            "winner": _f(rec.get("winner"), 50.0),
            "holder": _f(rec.get("holder"), 50.0),
            "smart": min(100.0, _f(rec.get("smart_buys")) * 34.0),
            "finger": _FP_SCORE.get(
                str(rec.get("fp") or "").upper(), 50.0),
        }
        total = 0.0
        contribs = []
        for w, label in _WEIGHTS:
            c = w * parts[label]
            total += c
            contribs.append((c, label, parts[label]))
        contribs.sort(reverse=True)
        why = []
        for _, label, raw in contribs[:2]:
            if label == "smart":
                why.append("%d tracked wallet%s in" % (
                    int(_f(rec.get("smart_buys"))),
                    "" if int(_f(rec.get("smart_buys"))) == 1 else "s"))
            elif label == "finger":
                why.append("fingerprint %s" % (
                    str(rec.get("fp") or "unknown")))
            else:
                why.append("%s %d/100" % (label, int(round(raw))))
        return round(total, 1), why
    except Exception:
        logger.exception("toppicks composite failed")
        return 50.0, []


def build_picks(state: Dict[str, Any], n: int = 5,
                now: Optional[float] = None
                ) -> List[Dict[str, Any]]:
    """Rank the last 24h of scored runners, best first. Never raises."""
    try:
        now = now if now is not None else time.time()
        n = max(1, min(10, int(n or 5)))
        scores = state.get(SCORE_KEY)
        if not isinstance(scores, dict):
            return []
        cutoff = now - _WINDOW_S
        ranked = []
        for mint, rec in scores.items():
            if not isinstance(rec, dict):
                continue
            if _f(rec.get("ts")) < cutoff:
                continue
            score, why = composite(rec)
            ranked.append({
                "mint": mint,
                "symbol": str(rec.get("symbol") or mint[:8]),
                "chain": str(rec.get("chain") or "solana"),
                "score": score,
                "why": why,
                "anchor_mcap": _f(rec.get("mcap")),
                "card_ts": _f(rec.get("ts")),
            })
        ranked.sort(key=lambda r: -r["score"])
        return ranked[:n]
    except Exception:
        logger.exception("toppicks build failed")
        return []


def slot_due(degen: Any, now: float) -> Optional[str]:
    """Today's WAT picks slot (YYYY-MM-DD), or None if not due yet."""
    try:
        raw = str(getattr(degen, "toppicks_time", "08:00") or "08:00")
        hh, mm = raw.split(":")
        h, m = int(hh), int(mm)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            h, m = 8, 0
    except (TypeError, ValueError, AttributeError):
        h, m = 8, 0
    day = datetime.fromtimestamp(now, WAT)
    slot = day.replace(hour=h, minute=m, second=0, microsecond=0)
    if slot.timestamp() > now:
        return None
    return slot.strftime("%Y-%m-%d")


def grade_picks(log: List[Dict[str, Any]],
                milestone_watch: Dict[str, Dict[str, Any]],
                today: str) -> Dict[str, Any]:
    """Grade the most recent picks day before today via milestone peaks.

    Returns {date, count, hit_2x, best_mult}. Never raises.
    """
    out = {"date": "", "count": 0, "hit_2x": 0, "best_mult": 0.0}
    try:
        past = [e for e in (log or [])
                if isinstance(e, dict) and str(e.get("date") or "") < today]
        if not past:
            return out
        entry = past[-1]
        picks = entry.get("picks") or []
        out["date"] = str(entry.get("date") or "")
        out["count"] = len(picks)
        best = 0.0
        hits = 0
        for p in picks:
            if not isinstance(p, dict):
                continue
            rec = (milestone_watch or {}).get(str(p.get("mint") or ""))
            peak = _f((rec or {}).get("peak_mult"))
            if peak >= 2.0:
                hits += 1
            best = max(best, peak)
        out["hit_2x"] = hits
        out["best_mult"] = round(best, 1)
        return out
    except Exception:
        logger.exception("toppicks grade failed")
        return out


def maybe_daily_picks(config: Any, state: Dict[str, Any], now: float,
                      milestones: Optional[Dict[str, Dict[str, Any]]] = None
                      ) -> Optional[List[Dict[str, Any]]]:
    """Build + publish the daily Top Picks card when the slot is due.

    Once per WAT day at/after toppicks_time (default 08:00). Publishes
    ``launch.top_picks``; records the picks in the log for grading.
    Returns the picks, or None when not due / disabled / empty.
    Never raises.
    """
    try:
        degen = config.degen
        if not getattr(degen, "toppicks_enabled", True):
            return None
        slot = slot_due(degen, now)
        if slot is None or state.get(SLOT_KEY) == slot:
            return None
        n = int(getattr(degen, "toppicks_count", 5) or 5)
        picks = build_picks(state, n=n, now=now)
        state[SLOT_KEY] = slot  # mark even when empty: one card per day
        if not picks:
            logger.warning("TOP PICKS %s: no scored runners in window",
                           slot)
            return None
        log = state.get(LOG_KEY)
        if not isinstance(log, list):
            log = []
            state[LOG_KEY] = log
        log.append({"date": slot, "picks": [
            {"mint": p["mint"], "symbol": p["symbol"],
             "score": p["score"], "anchor_mcap": p["anchor_mcap"]}
            for p in picks]})
        if len(log) > 30:
            del log[:len(log) - 30]
        yesterday = grade_picks(log, milestones or {}, slot)
        from agent import events as event_bus  # noqa: PLC0415
        event_bus.publish({
            "event": "launch.top_picks",
            "date": slot,
            "count": len(picks),
            "picks": [
                {"mint": p["mint"], "symbol": p["symbol"],
                 "chain": p["chain"], "score": p["score"],
                 "why": "; ".join(p["why"]),
                 "anchor_mcap": p["anchor_mcap"]}
                for p in picks
            ],
            "yesterday": yesterday,
            "note": ("Top %d confirmed runners by composite score "
                     "(sniper/winner/holder/smart/fingerprint). "
                     "Paper-tracked — not financial advice." % len(picks)),
        })
        logger.warning("TOP PICKS %s: %d picks (best %.1f)", slot,
                       len(picks), picks[0]["score"] if picks else 0)
        return picks
    except Exception:
        logger.exception("top picks daily run failed")
        return None
