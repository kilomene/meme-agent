"""Shadow ledger: paper-simulate the coins the gates block.

For every quality-held runner (the same coins recorded in the
skipped-runners digest), the shadow ledger opens a simulated position at
the hold-time market cap and tracks it with stop-loss / take-profit /
timeout exits. It never touches real positions, never publishes alert
events, never calls the broker — it is a measurement instrument answering
one question: "what would loosening the gates have done?"

First-sight holds (e.g. logstream mints) arrive before any price feed
exists, so their entry market cap is unknown (0). Those open as PENDING
positions: the entry is backfilled from the first successful price
refresh, and pendings that never get a price are dropped after
degen.shadow_pending_timeout_minutes. This closes the blind spot where
the ledger silently ignored the freshest coins — the ones it most needs
to measure.

All state lives under state["shadow_open"] / state["shadow_closed"] and
is persisted with the daemon state. Every public function is fail-safe:
on any error it logs and returns, never raising into the scanner loop.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("forex_agent.daemon.launch_scanner.shadow")

_SHADOW_CLOSED_CAP = 300
_REFRESH_SECONDS = 120.0


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _cfg(degen: Any, name: str, default: Any) -> Any:
    return getattr(degen, name, default) if degen is not None else default


def enabled(degen: Any) -> bool:
    return bool(_cfg(degen, "shadow_enabled", True))


def maybe_open(state: Dict[str, Any], degen: Any, kind: str, symbol: str,
               mint: str, chain: str, entry_mcap_usd: float,
               reason: str) -> bool:
    """Open a shadow position for a just-blocked runner.

    Returns True when a position was opened. Guards: experiment enabled,
    known mint, no already-open shadow position for the mint, and the
    concurrent-position cap. A zero/unknown entry mcap opens a PENDING
    position whose entry is backfilled on the first price refresh —
    first-sight holds must be measured, not silently skipped.
    Never raises.
    """
    try:
        if not enabled(degen):
            return False
        if not mint:
            return False
        open_book = state.setdefault("shadow_open", {})
        if not isinstance(open_book, dict):
            state["shadow_open"] = open_book = {}
        if mint in open_book:
            return False
        max_open = int(_f(_cfg(degen, "shadow_max_open", 20)) or 20)
        if len(open_book) >= max_open:
            return False
        now = time.time()
        entry = _f(entry_mcap_usd)
        priced = entry > 0
        open_book[mint] = {
            "symbol": symbol or "",
            "kind": kind or "",
            "chain": chain or "",
            "entry_mcap": entry if priced else 0.0,
            "last_mcap": entry if priced else 0.0,
            "opened_ts": now,
            "last_ts": now,
            "reason": str(reason or "")[:120],
            # Pending until the first price refresh backfills the entry.
            "pending": not priced,
            "entry_ts": now if priced else 0.0,
        }
        return True
    except Exception:
        logger.exception("shadow maybe_open failed")
        return False


def _close(state: Dict[str, Any], degen: Any, mint: str,
           pos: Dict[str, Any], exit_mcap: float, outcome: str,
           now: float) -> None:
    closed = state.setdefault("shadow_closed", [])
    if not isinstance(closed, list):
        state["shadow_closed"] = closed = []
    entry = _f(pos.get("entry_mcap"))
    pnl = ((exit_mcap - entry) / entry * 100.0) if entry > 0 else 0.0
    closed.append({
        "symbol": pos.get("symbol") or "",
        "mint": mint,
        "chain": pos.get("chain") or "",
        "kind": pos.get("kind") or "",
        "entry_mcap": entry,
        "exit_mcap": exit_mcap,
        "pnl_pct": round(pnl, 2),
        "outcome": outcome,  # "win" | "loss" | "timeout"
        "opened_ts": pos.get("opened_ts"),
        "closed_ts": now,
        "reason": pos.get("reason") or "",
    })
    del closed[:-_SHADOW_CLOSED_CAP]
    # Phase 1 insider outcome ledger: join this close with the insider
    # trend for the mint (measurement only — never influences trading).
    try:
        from . import insiders as _insiders
        _insiders.record_outcome(state, degen, mint, {
            "symbol": pos.get("symbol") or "",
            "chain": pos.get("chain") or "",
            "outcome": outcome,
            "pnl_pct": round(pnl, 2),
        })
    except Exception:
        logger.warning("shadow->insider outcome join failed", exc_info=True)


def _refresh_mcaps(open_book: Dict[str, Dict[str, Any]]) -> None:
    """Refresh last_mcap for open positions via DexScreener batch stats.

    Grouped by chain, one HTTP call per chain (<=30 mints each). Mints
    with no indexed pair keep their last known mcap. Never raises.
    """
    try:
        from . import sources
    except Exception:
        return
    by_chain: Dict[str, List[str]] = {}
    for mint, pos in open_book.items():
        chain = (pos.get("chain") or "solana").lower()
        by_chain.setdefault(chain, []).append(mint)
    for chain, mints in by_chain.items():
        try:
            stats = sources.dexscreener_batch_stats(mints, chain=chain)
        except Exception:
            logger.warning("shadow refresh failed for chain %s", chain,
                           exc_info=True)
            continue
        for mint in mints:
            hit = stats.get(mint)
            if not hit:
                continue
            mcap = _f(hit.get("usd_market_cap"))
            if mcap > 0:
                open_book[mint]["last_mcap"] = mcap
                open_book[mint]["last_ts"] = time.time()


def update(state: Dict[str, Any], degen: Any,
           now: Optional[float] = None) -> Dict[str, int]:
    """Refresh open shadow positions; close on stop / TP / timeout.

    Throttled to one price refresh per _REFRESH_SECONDS. Returns a small
    dict of what happened (opens are done by maybe_open, not here).
    Never raises.
    """
    result = {"refreshed": 0, "closed_win": 0, "closed_loss": 0,
              "closed_timeout": 0, "backfilled": 0, "pending_dropped": 0}
    try:
        if not enabled(degen):
            return result
        open_book = state.get("shadow_open")
        if not isinstance(open_book, dict) or not open_book:
            return result
        now = now if now is not None else time.time()
        last = _f(state.get("shadow_last_refresh"))
        if now - last < _REFRESH_SECONDS:
            return result
        state["shadow_last_refresh"] = now

        _refresh_mcaps(open_book)
        result["refreshed"] = len(open_book)

        stop_pct = _f(_cfg(degen, "shadow_stop_pct", 30.0))
        tp_pct = _f(_cfg(degen, "shadow_tp_pct", 200.0))
        max_hold_s = _f(_cfg(degen, "shadow_max_hold_hours", 24.0)) * 3600.0
        pending_timeout_s = _f(_cfg(degen, "shadow_pending_timeout_minutes",
                                    60.0)) * 60.0

        for mint in list(open_book.keys()):
            pos = open_book[mint]
            if pos.get("pending"):
                # First known price becomes the entry: the earliest price
                # the bot could actually have acted on.
                cur = _f(pos.get("last_mcap"))
                if cur > 0:
                    pos["entry_mcap"] = cur
                    pos["pending"] = False
                    pos["entry_ts"] = time.time()
                    result["backfilled"] += 1
                    logger.warning("shadow backfilled entry %s %s mcap=%.0f",
                                   pos.get("symbol"), mint, cur)
                elif (pending_timeout_s > 0
                      and now - _f(pos.get("opened_ts")) >= pending_timeout_s):
                    # Never got a price: "no data", not a loss — drop it
                    # without recording a close.
                    del open_book[mint]
                    result["pending_dropped"] += 1
                    logger.warning("shadow dropped pending %s %s "
                                   "(no price in %.0fm)",
                                   pos.get("symbol"), mint,
                                   pending_timeout_s / 60.0)
                continue

            entry = _f(pos.get("entry_mcap"))
            cur = _f(pos.get("last_mcap"))
            if entry <= 0 or cur <= 0:
                continue
            pnl = (cur - entry) / entry * 100.0
            age = now - _f(pos.get("opened_ts"))
            if pnl <= -stop_pct:
                _close(state, degen, mint, pos, cur, "loss", now)
                del open_book[mint]
                result["closed_loss"] += 1
            elif pnl >= tp_pct:
                _close(state, degen, mint, pos, cur, "win", now)
                del open_book[mint]
                result["closed_win"] += 1
            elif max_hold_s > 0 and age >= max_hold_s:
                _close(state, degen, mint, pos, cur, "timeout", now)
                del open_book[mint]
                result["closed_timeout"] += 1
        return result
    except Exception:
        logger.exception("shadow update failed")
        return result


def summary(state: Dict[str, Any]) -> Dict[str, Any]:
    """Aggregate stats for the /shadow command. Never raises."""
    try:
        open_book = state.get("shadow_open") or {}
        if not isinstance(open_book, dict):
            open_book = {}
        priced = [p for p in open_book.values()
                  if isinstance(p, dict) and not p.get("pending")]
        pending = [p for p in open_book.values()
                   if isinstance(p, dict) and p.get("pending")]
        closed = [c for c in (state.get("shadow_closed") or [])
                  if isinstance(c, dict)]
        wins = [c for c in closed if c.get("outcome") == "win"]
        losses = [c for c in closed if c.get("outcome") == "loss"]
        pnls = [_f(c.get("pnl_pct")) for c in closed]
        return {
            "open": len(priced),
            "pending": len(pending),
            "closed": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "timeouts": len([c for c in closed
                             if c.get("outcome") == "timeout"]),
            "win_rate": (len(wins) / len(closed) * 100.0) if closed else 0.0,
            "avg_pnl": (sum(pnls) / len(pnls)) if pnls else 0.0,
            "best": max(pnls) if pnls else 0.0,
            "worst": min(pnls) if pnls else 0.0,
        }
    except Exception:
        return {"open": 0, "pending": 0, "closed": 0, "wins": 0,
                "losses": 0, "timeouts": 0, "win_rate": 0.0, "avg_pnl": 0.0,
                "best": 0.0, "worst": 0.0}
