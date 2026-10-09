"""Slow-cooker late runners: catch the 2x that comes LATE.

A quality-passed coin (instant card OR digest-queued) that sits quiet
past the initial runner-confirmation window and THEN runs >=
``slow_cooker_trigger_pct`` (default 100% = 2x) from its card-time
anchor within ``slow_cooker_window_hours`` (default 3.0h) gets an
ACTIONABLE ``launch.late_runner`` card: the full safety gate re-runs
(fail-closed, trial bar applies) and a paper position opens labeled
``late_runner`` with the standard exit plan.

The observation pool is the milestone tracker's own watch records
(``state["milestone_watch"]``: anchor, card_ts, peak_mult, source) —
digest-queued coins ARE in the pool because the scanner calls
``milestones.track_signal`` at every digest-queue site. This module
never builds a parallel tracking pool; it is bounded by the milestone
watch's own bound (120 records, 24h expiry).

DEDUPE with milestones (deliberate, documented in both modules): the
milestone tracker fires a MEASUREMENT-ONLY 2x celebration card. The
late-runner card is ACTIONABLE (re-gated + paper entry). Exactly one
2x card per coin:
  * slowcooker commits FIRST in the scan cycle (before milestones.poll
    evaluates), marking the milestone record ``late_runner_fired`` —
    milestones.poll then SKIPS the 2x celebration for that mint
    (higher milestones 5x/10x+ still fire).
  * if the milestone 2x already celebrated in an EARLIER cycle
    (2.0 in the record's ``hit`` list), slowcooker skips the mint —
    the coin already got its 2x card.
  * if the safety re-check FAILS, slowcooker holds SILENTLY and burns
    nothing — the milestone 2x celebration still fires as the
    consolation card, and the coin may trigger again later.

Discipline, matching the other lanes:
- ONE late-runner card per mint (per-mint fired flag in
  ``state["slowcooker_fired"]``; pruned alongside the milestone watch).
- Full safety re-gate at trigger (fail-closed). A failing re-check
  holds SILENTLY: no card, no position, no fired flag burned.
- Late-runner cards go through the NORMAL instant spam guard
  (``_early_allowed``): they count toward the 4/hour cap. If the cap
  is hit they queue into the digest like everything else — no bypass.
- Never raises; never touches live/mainnet (entries go through the
  same paper ``_paper_enter`` path as the other lanes).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("forex_agent.daemon.launch_scanner.slowcooker")

_FIRED_KEY = "slowcooker_fired"    # state key: per-mint fired flags
_STATS_KEY = "slowcooker_stats"    # state key: cached batch stats
_STATS_TTL_S = 30.0                # DexScreener batch refresh cadence
_POLL_EVERY_CYCLES = 4             # same ~1 min cadence as milestones.poll

# The initial runner-confirmation window: a 2x inside this window is the
# main lanes' job (confirmed-runner pipeline), not the slow cooker's.
# A coin is only a LATE runner once its card is older than this.
_INITIAL_WINDOW_MIN = 45.0

# Only coins whose first card was NOT a confirmed runner qualify: an
# instant card, a digest item, or a lowcap-runner alert that never
# confirmed. Confirmed runners (launch.detected) and second waves
# already had their actionable moment.
_SOURCES = ("launch.early", "launch.early_digest", "launch.lowcap_runner")


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def enabled(degen: Any) -> bool:
    """Slow-cooker late-runner watch is ON unless explicitly disabled.

    Follows the codebase's getattr-with-default convention so no config
    schema change is required; the flag survives the trial auto-revert
    untouched.
    """
    return bool(getattr(degen, "slow_cooker_enabled", True))


def _num(degen: Any, name: str, default: float) -> float:
    try:
        return float(getattr(degen, name, default) or default)
    except (TypeError, ValueError):
        return default


def knobs(degen: Any) -> Dict[str, float]:
    """Slow-cooker tuning knobs (config/defaults.yaml, degen section)."""
    return {
        # Observation window past the card, in hours (default 3.0).
        "window_h": _num(degen, "slow_cooker_window_hours", 3.0),
        # Trigger as a multiple of the card-time anchor: 1 + pct/100
        # (default 100.0 -> 2.0x).
        "trigger_mult": 1.0 + _num(degen, "slow_cooker_trigger_pct",
                                   100.0) / 100.0,
    }


def _batch_stats(watch: Dict[str, Dict[str, Any]]
                 ) -> Dict[str, Dict[str, Any]]:
    """Current {mint: stats} via the milestone tracker's batch fetcher."""
    from . import milestones  # noqa: PLC0415 (lazy: package init order)
    return milestones._batch_stats(watch)


def _refresh_stats(state: Dict[str, Any],
                   watch: Dict[str, Dict[str, Any]],
                   now: float) -> Dict[str, Dict[str, Any]]:
    """Throttled batch refresh (30s TTL, same pattern as secondwave.py).

    The milestone tracker's DexScreener batch fetcher is shared —
    slowcooker never builds its own price pipeline.
    """
    cached = state.get(_STATS_KEY)
    if (isinstance(cached, dict)
            and now - _f(cached.get("ts")) < _STATS_TTL_S
            and isinstance(cached.get("data"), dict)):
        return cached["data"]
    data: Dict[str, Dict[str, Any]] = {}
    try:
        data = _batch_stats(watch) or {}
    except Exception:
        logger.warning("slow-cooker stats refresh failed", exc_info=True)
    state[_STATS_KEY] = {"ts": now, "data": data}
    return data


def _late_payload(mrec: Dict[str, Any], mint: str, mcap: float,
                  anchor: float, mult: float, now: float
                  ) -> Dict[str, Any]:
    return {
        "mint": mint,
        "chain": str(mrec.get("chain") or "solana"),
        "symbol": str(mrec.get("symbol") or mint[:8]),
        "name": str(mrec.get("name") or ""),
        "url": str(mrec.get("url") or ""),
        "mcap_now": mcap,
        "anchor": anchor,
        "multiple_now": round(mult, 2),
        "gain_pct": round(100.0 * (mult - 1.0), 1),
        "hours_since_seen": round(
            (now - _f(mrec.get("card_ts") or now)) / 3600.0, 1),
        "source": str(mrec.get("source") or ""),
    }


def poll(state: Dict[str, Any], degen: Any, now: float,
         cycle: int) -> List[Dict[str, Any]]:
    """Find tracked coins that just became late runners.

    A tracked coin qualifies when ALL hold:
      * its first card was not a confirmed runner (source in
        ``_SOURCES`` — never launch.detected / launch.second_wave), and
        it was not confirmed later either (the milestone record gains
        ``confirmed_runner`` when a confirmation card lands),
      * the initial 45-min runner-confirmation window has closed,
      * it is still within ``slow_cooker_window_hours`` of its card,
      * its current multiple of the card-time anchor >= the trigger
        multiple (default 2.0),
      * it has not already fired (per-mint fired flag), and the
        milestone tracker has not already celebrated this 2x in an
        earlier cycle (2.0 in the record's ``hit`` list).
    Returns trigger payloads for :func:`handle_late`. Never raises.
    """
    out: List[Dict[str, Any]] = []
    try:
        if not enabled(degen):
            return out
        if cycle % _POLL_EVERY_CYCLES != 0:
            return out
        from . import milestones  # noqa: PLC0415 (lazy: package init order)
        kb = knobs(degen)
        watch = milestones.tracked(state)
        fired = state.setdefault(_FIRED_KEY, {})
        # Prune fired flags for coins that left milestone tracking (24h
        # expiry there) — the fired set stays bounded by the watch.
        for mint in list(fired):
            if mint not in watch:
                fired.pop(mint, None)
        if not watch:
            return out
        stats = _refresh_stats(state, watch, now)
        for mint, mrec in watch.items():
            if not isinstance(mrec, dict):
                continue
            if mrec.get("late_runner_fired"):
                continue
            if mint in fired:
                continue
            if str(mrec.get("source") or "") not in _SOURCES:
                continue
            if mrec.get("confirmed_runner"):
                continue  # confirmed later — not a quiet unconfirmed coin
            anchor = _f(mrec.get("anchor"))
            if anchor <= 0:
                continue  # pending: no actionable reference mcap yet
            age_s = now - _f(mrec.get("card_ts"))
            if age_s < _INITIAL_WINDOW_MIN * 60.0:
                continue  # initial confirmation window still open
            if age_s > kb["window_h"] * 3600.0:
                continue  # slow-cooker window closed
            if 2.0 in (mrec.get("hit") or []):
                continue  # milestone already celebrated this 2x
            mcap = _f((stats.get(mint) or {}).get("usd_market_cap"))
            if mcap <= 0:
                continue
            mult = mcap / anchor
            if mult < kb["trigger_mult"]:
                continue
            out.append(_late_payload(mrec, mint, mcap, anchor, mult,
                                     now))
            logger.warning("SLOW COOKER trigger %s %s: %.1fx of card "
                           "mcap after %.1fh (anchor $%.0f -> $%.0f)",
                           mrec.get("symbol"), mint, mult,
                           age_s / 3600.0, anchor, mcap)
        return out
    except Exception:
        logger.exception("slow-cooker poll failed")
        return []


def handle_late(scanner: Any, config: Any, adapter: Any,
                state: Dict[str, Any], late: Dict[str, Any]) -> bool:
    """Safety re-check, card, and paper entry for one late runner.

    - The full safety gate re-runs at trigger time (fail-closed, trial
      bar applies). A failing re-check holds SILENTLY: no card, no
      position, no fired flag burned — the coin may trigger again
      later, and the milestone 2x celebration still fires as the
      consolation card.
    - On a passing re-check the ``launch.late_runner`` card goes
      through the NORMAL instant spam guard (counts toward the 4/hour
      cap). If the cap is hit the signal queues into the digest like
      everything else — no bypass — and (like all digest items) opens
      no paper position.
    - The commit (instant card OR digest queue) marks the milestone
      record ``late_runner_fired`` so milestones.poll skips the 2x
      celebration, and sets the per-mint fired flag so the coin never
      fires twice.
    Returns True when an instant card was emitted. Never raises.
    """
    from agent import events as event_bus  # noqa: PLC0415
    from . import latency, milestones, safety  # noqa: PLC0415 (lazy)

    degen = config.degen
    mint = late.get("mint")
    chain = str(late.get("chain") or "solana")
    symbol = str(late.get("symbol") or (mint or "")[:8])
    now = time.time()
    try:
        if not mint:
            return False
        fired = state.setdefault(_FIRED_KEY, {})
        if mint in fired:
            return False
        # Full safety re-check at trigger — fail-closed. The trial
        # score bar (early_min_score) applies here exactly as it does
        # to every other actionable lane.
        ok, reason, report = scanner._confirm_safety_ok(
            degen, {"mint": mint, "chain": chain})
        if not ok:
            logger.warning("SLOW COOKER HELD (safety) %s %s: %s — "
                           "holding silently, no card, no position",
                           symbol, mint, reason)
            return False
        saf = report if isinstance(report, dict) else {}
        card: Dict[str, Any] = {
            "event": "launch.late_runner",
            "symbol": symbol,
            "mint": mint,
            "chain": chain,
            "name": str(late.get("name") or ""),
            "url": str(late.get("url") or ""),
            "usd_market_cap": float(late.get("mcap_now") or 0),
            "hours_since_seen": late.get("hours_since_seen"),
            "gain_pct": late.get("gain_pct"),
            "multiple_now": late.get("multiple_now"),
            # Strict: the re-check score is ALWAYS noted on the card.
            "safety": safety.verdict_line(saf),
            "safety_score": saf.get("score"),
            "plan": scanner._trade_plan(degen),
            "source": str(late.get("source") or ""),
            "confirmed": True,
            "entry": None,
            "note": "",
        }
        # Commit point: the milestone record is marked (dedupe — the
        # measurement-only 2x celebration stays silent) and the per-mint
        # fired flag is set, whether the card goes out instantly or
        # queues into the digest.
        watch = milestones.tracked(state)
        mrec = watch.get(mint)
        if isinstance(mrec, dict):
            mrec["late_runner_fired"] = True
        fired[mint] = now
        entered = False
        if scanner._early_allowed(degen, state, now):
            if chain == "solana" and getattr(degen, "auto_enter", True):
                from daemon.launch_scanner import count_base_positions  # noqa: PLC0415
                if count_base_positions(state.get("degen_positions")) >= int(
                        getattr(degen, "max_positions", 5) or 5):
                    card["note"] = "max degen positions reached — alert only"
                else:
                    entered = scanner._paper_enter(
                        config, adapter, state, card, degen, mint, symbol,
                        tag="late_runner", intel={"safety": saf})
            if not entered and not card.get("note"):
                if chain != "solana":
                    card["note"] = "alert only — paper engine prices Solana"
                elif not getattr(degen, "auto_enter", True):
                    card["note"] = "auto_enter off — alert only"
            # Entry timing (measurement only): the first card always
            # wins — this coin was carded long ago (instant or digest),
            # so this records nothing new, just the journal field.
            card["latency_min"] = latency.record_first_card(
                state, degen, mint, now, symbol=symbol,
                source="launch.late_runner")
            event_bus.publish(card)
            logger.warning("LATE RUNNER 🐢 %s %s: +%.0f%% (%.1fx of "
                           "card) %.1fh after first card%s",
                           symbol, mint,
                           float(late.get("gain_pct") or 0),
                           float(late.get("multiple_now") or 0),
                           float(late.get("hours_since_seen") or 0),
                           " [paper entry]" if entered else "")
            return True
        # Spam cap hit — queue into the digest like everything else.
        # Digest items never open paper positions.
        state.setdefault("early_digest", []).append({
            "mint": mint, "symbol": symbol,
            "name": str(late.get("name") or ""),
            "chain": chain, "tier": "late_runner",
            "source": str(late.get("source") or ""),
            "url": str(late.get("url") or ""),
            "reason": "late runner +%.0f%% (%.1fx of card) %.1fh after "
                      "first card" % (
                          float(late.get("gain_pct") or 0),
                          float(late.get("multiple_now") or 0),
                          float(late.get("hours_since_seen") or 0)),
            "safety_score": saf.get("score"),
            "narratives": ["other"],
            "narrative_heat": 1.0,
            "sentiment": None,
            "ts": now,
        })
        logger.warning("LATE RUNNER queued (spam cap) %s %s", symbol,
                       mint)
        return False
    except Exception:
        logger.exception("slow-cooker handle failed for %s", mint)
        return False
