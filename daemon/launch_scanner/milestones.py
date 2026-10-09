"""Milestone tracking: celebrate the runners we actually called.

When a signal card goes out (launch.early, launch.detected,
launch.lowcap_runner, momentum.alert, or a spam-guard digest item), the
mint plus its card-time market cap are registered here. On a throttled
poll the tracker refreshes current mcaps via DexScreener batch stats and
emits a ``launch.milestone`` event the first time a tracked coin crosses
2x / 5x / 10x / 25x / 50x / 100x / 250x / 500x / 1000x from its card-time
mcap — and keeps doubling (2000x, 4000x, …) past that, so however many
x's the coin does, the run keeps getting celebrated.

Anchoring is honest: the multiple is measured from OUR card, not from
the absolute bottom tick — so "10x" means 10x from when the user was
alerted. Coins carded with an unknown (zero) mcap open as PENDING and
anchor on the first indexed price, mirroring the shadow ledger's
pending-entry semantics.

Dedupe with the slow-cooker (slowcooker.py): when a tracked coin
becomes a late runner, the slow cooker commits FIRST in the scan cycle
and marks the watch record ``late_runner_fired`` — this poll then
skips the 2x celebration for that mint (higher milestones 5x/10x+
still fire), so the user gets exactly one 2x card: the ACTIONABLE
late-runner card, not the measurement-only celebration.

Measurement only: milestones never trade, never gate, never touch the
broker, and each threshold fires exactly once per coin. Tracked coins
expire after 24h.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("forex_agent.daemon.launch_scanner.milestones")

_WATCH_KEY = "milestone_watch"
_MAX_TRACKED = 120
_EXPIRY_S = 24 * 3600
_POLL_EVERY_CYCLES = 4  # ~1 min at the scanner's 15s cadence

# Sources that mark a tracked coin as a CONFIRMED runner. A coin first
# carded unconfirmed (launch.early / launch.early_digest /
# launch.lowcap_runner) and later confirmed keeps its first anchor, but
# gains ``confirmed_runner`` on the record — the slow cooker only watches
# coins that never confirmed.
_CONFIRMED_SOURCES = ("launch.detected", "launch.second_wave")

_DEFAULT_THRESHOLDS = (2.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0,
                       1000.0)


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def enabled(degen: Any) -> bool:
    """Milestone tracking is ON unless explicitly disabled.

    Follows the codebase's getattr-with-default convention so no config
    schema change is required; the flag survives the trial auto-revert
    untouched.
    """
    return bool(getattr(degen, "milestones_enabled", True))


def thresholds(degen: Any) -> List[float]:
    raw = getattr(degen, "milestone_thresholds", None)
    out: List[float] = []
    if isinstance(raw, (list, tuple)):
        for t in raw:
            try:
                v = float(t)
            except (TypeError, ValueError):
                continue
            if v > 1:
                out.append(v)
    if not out:
        out = list(_DEFAULT_THRESHOLDS)
    return sorted(set(out))


def _min_mcap_now(degen: Any) -> float:
    """Floor on current mcap before a milestone may fire.

    Dust wobble (a $200 coin ticking to $1k) is not a milestone anyone
    can trade; the floor keeps the lane to moves with real size.
    """
    try:
        return float(getattr(degen, "milestone_min_mcap_now_usd", 5000.0))
    except (TypeError, ValueError):
        return 5000.0


def _watch(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    w = state.get(_WATCH_KEY)
    if not isinstance(w, dict):
        w = {}
        state[_WATCH_KEY] = w
    return w


def tracked(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The live milestone watch records: per-mint card-time anchor plus
    ``peak_mult`` (the coin's max multiple of its anchor, maintained in
    :func:`poll`). The second-wave re-entry module reads these — peak
    tracking lives here so no other module duplicates it."""
    return _watch(state)


def track_signal(state: Dict[str, Any], degen: Any,
                 event: Dict[str, Any]) -> bool:
    """Register a freshly published signal card for milestone tracking.

    Idempotent per mint: a coin carded twice (e.g. launch.early then
    launch.detected) keeps its FIRST card's anchor. When the second
    card is a confirmed runner (launch.detected / launch.second_wave),
    the record is upgraded with ``confirmed_runner`` so the slow cooker
    never treats a confirmed coin as carded-but-quiet. Returns True when
    the coin is now tracked.
    """
    try:
        if not enabled(degen):
            return False
        if not isinstance(event, dict):
            return False
        mint = str(event.get("mint") or "").strip()
        if not mint:
            return False
        watch = _watch(state)
        if mint in watch:
            # Confirmed-runner upgrade: the slow cooker only watches
            # unconfirmed first cards; a later confirmation retires the
            # record from its pool (the anchor and milestones stay).
            rec = watch[mint]
            if isinstance(rec, dict) and str(
                    event.get("event") or "") in _CONFIRMED_SOURCES:
                rec["confirmed_runner"] = True
            return True
        now = time.time()
        anchor = _f(event.get("usd_market_cap"))
        watch[mint] = {
            "symbol": str(event.get("symbol") or mint[:8]),
            "chain": str(event.get("chain") or "solana").lower(),
            "name": str(event.get("name") or ""),
            "url": str(event.get("url") or ""),
            "anchor": anchor if anchor > 0 else 0.0,
            "pending": anchor <= 0,
            "hit": [],
            "card_ts": now,
            "source": str(event.get("event") or "signal"),
            "safety_score": event.get("safety_score"),
        }
        # Bound the watch list: oldest cards drop first. The cap is a
        # config knob (default 1000 ≈ a full day of cards) so the
        # twice-daily runners recap sees every runner, not just the
        # most recent 120.
        max_tracked = int(getattr(degen, "milestone_max_tracked",
                                  _MAX_TRACKED) or _MAX_TRACKED)
        if len(watch) > max_tracked:
            oldest = sorted(watch.items(),
                            key=lambda kv: float(kv[1].get("card_ts") or 0))
            for old_mint, _ in oldest[:len(watch) - max_tracked]:
                watch.pop(old_mint, None)
        logger.warning("MILESTONE tracking %s %s anchor=$%.0f%s",
                       watch[mint]["symbol"], mint, anchor,
                       " (pending)" if anchor <= 0 else "")
        return True
    except Exception:
        logger.exception("milestone track failed for %s",
                         (event or {}).get("mint"))
        return False


def poll(state: Dict[str, Any], degen: Any, now: float,
         cycle: int) -> List[Dict[str, Any]]:
    """Refresh tracked mcaps; return milestone events to publish.

    Throttled to every ``_POLL_EVERY_CYCLES`` scan cycles (~1 min).
    Never raises.
    """
    out: List[Dict[str, Any]] = []
    try:
        if not enabled(degen):
            return out
        if cycle % _POLL_EVERY_CYCLES != 0:
            return out
        watch = _watch(state)
        if not watch:
            return out
        th = thresholds(degen)
        floor = _min_mcap_now(degen)
        # Expire stale entries first.
        for mint in list(watch):
            if now - float(watch[mint].get("card_ts") or 0) > _EXPIRY_S:
                _grade_deployer_outcome(state, degen, mint, watch[mint])
                watch.pop(mint, None)
        if not watch:
            return out
        stats = _batch_stats(watch)
        for mint, rec in list(watch.items()):
            hit = stats.get(mint)
            mcap = _f((hit or {}).get("usd_market_cap"))
            if mcap <= 0:
                continue  # not indexed yet; keep waiting
            if rec.get("pending"):
                # First indexed price becomes the earliest actionable
                # anchor — same semantics as the shadow pending fix.
                rec["anchor"] = mcap
                rec["pending"] = False
                logger.warning("MILESTONE anchor backfilled %s %s $%.0f",
                               rec.get("symbol"), mint, mcap)
            anchor = _f(rec.get("anchor"))
            if anchor <= 0:
                continue
            mult = mcap / anchor
            # Peak multiple of OUR card-time mcap, owned here. The
            # second-wave re-entry module reads ``peak_mult`` from the
            # watch record instead of tracking its own peak.
            try:
                rec["peak_mult"] = max(_f(rec.get("peak_mult")), mult)
            except Exception:
                rec["peak_mult"] = mult
            if mcap < floor:
                continue
            fired = rec.setdefault("hit", [])

            def _fire(t: float) -> None:
                fired.append(t)
                out.append(_milestone_event(rec, mint, t, mult, mcap,
                                           anchor, now))
                logger.warning("MILESTONE %s %s hit %.0fx "
                               "($%.0f -> $%.0f)", rec.get("symbol"),
                               mint, t, anchor, mcap)

            for t in th:
                if t == 2.0 and rec.get("late_runner_fired"):
                    # DEDUPE with the slow-cooker (slowcooker.py): the
                    # coin already got an ACTIONABLE late-runner card for
                    # this 2x (safety re-gated + paper entry), so the
                    # measurement-only celebration stays silent — the
                    # user gets exactly one 2x card. Higher milestones
                    # (5x/10x/...) still fire.
                    continue
                if mult >= t and t not in fired:
                    _fire(t)
            # Open-ended tail: past the static ladder the thresholds keep
            # doubling (2000x, 4000x, ...) so no moonshot outruns the
            # celebration. The 24h expiry bounds tracking, not a top.
            tail = max(th)
            while mult >= tail * 2:
                tail *= 2
                if tail not in fired:
                    _fire(tail)
        return out
    except Exception:
        logger.exception("milestone poll failed")
        return []


def _grade_deployer_outcome(state: Dict[str, Any], degen: Any,
                            mint: str, rec: Dict[str, Any]) -> None:
    """Grade a 24h-expired watch record into the deployer registry.

    ``"hit_2x"`` when the coin's peak multiple of OUR card-time anchor
    reached 2.0, else ``"dead"``. Runs once per mint (guarded by
    ``state["deployer_outcome_recorded"]``) so a paper-close grade for
    the same launch never double-counts. Wallet grading needs no hook
    here: ``smartgrade.reconcile`` reads the watch record's ``hit`` /
    ``peak_mult`` directly every ~10 scan cycles. Never raises.
    """
    try:
        if not getattr(degen, "deployer_tracking_enabled", True):
            return
        if not mint or not isinstance(rec, dict):
            return
        recorded = state.setdefault("deployer_outcome_recorded", {})
        if recorded.get(mint):
            return  # already graded at the paper close — no double count
        deployer = ((state.get("deployer_cache") or {}).get(mint)
                    or {}).get("deployer")
        if not deployer:
            return
        outcome = "hit_2x" if _f(rec.get("peak_mult")) >= 2.0 else "dead"
        from daemon.launch_scanner import deployers  # local: avoid cycles
        if deployers.record_outcome(mint, deployer, outcome):
            recorded[mint] = {"ts": time.time(), "outcome": outcome}
            logger.warning("DEPLOYER graded %s -> %s (%s…)", mint,
                           outcome, str(deployer)[:8])
    except Exception:
        logger.warning("deployer expiry grade failed for %s", mint,
                       exc_info=True)


def _batch_stats(watch: Dict[str, Dict[str, Any]]
                 ) -> Dict[str, Dict[str, Any]]:
    """Current {mint: stats} via DexScreener batch endpoint, by chain."""
    try:
        from . import sources
    except Exception:
        return {}
    by_chain: Dict[str, List[str]] = {}
    for mint, rec in watch.items():
        chain = str(rec.get("chain") or "solana").lower()
        by_chain.setdefault(chain, []).append(mint)
    merged: Dict[str, Dict[str, Any]] = {}
    for chain, mints in by_chain.items():
        try:
            merged.update(sources.dexscreener_batch_stats(mints,
                                                          chain=chain))
        except Exception:
            logger.warning("milestone refresh failed for chain %s",
                           chain, exc_info=True)
    return merged


def _milestone_event(rec: Dict[str, Any], mint: str, threshold: float,
                     mult: float, mcap: float, anchor: float,
                     now: float) -> Dict[str, Any]:
    return {
        "event": "launch.milestone",
        "symbol": rec.get("symbol") or mint[:8],
        "mint": mint,
        "chain": rec.get("chain") or "solana",
        "name": rec.get("name") or "",
        "url": rec.get("url") or "",
        "threshold": threshold,
        "multiple": round(mult, 1),
        "card_mcap": round(anchor, 2),
        "mcap_now": round(mcap, 2),
        "safety_score": rec.get("safety_score"),
        "source_event": rec.get("source") or "signal",
        "minutes_since_card": round(
            (now - float(rec.get("card_ts") or now)) / 60.0, 1),
    }
