"""Degen scoreboard — the honesty instrument.

Reads ``position.closed`` events with ``degen_*`` reasons from the event
queue and computes real performance: trades, win rate, total P&L,
average win/loss, profit factor, and a per-tier breakdown.

No exchange, no estimates — only closed paper positions. If the strategy
isn't profitable, this is where we see it first.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Dict, List

logger = logging.getLogger("forex_agent.daemon.launch_scanner.scoreboard")


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def load_degen_closes(db_path: str, limit: int = 2000) -> List[Dict[str, Any]]:
    """All closed degen paper positions, newest last."""
    closes: List[Dict[str, Any]] = []
    try:
        db = sqlite3.connect(db_path)
        try:
            rows = db.execute(
                "select ts, event from event_queue "
                "where event like '%\"event\": \"position.closed\"%' "
                "order by rowid desc limit ?", (limit,)).fetchall()
        finally:
            db.close()
    except Exception as exc:
        logger.warning("scoreboard read failed: %s", exc)
        return []
    for ts, blob in rows:
        try:
            e = json.loads(blob)
        except Exception:
            continue
        if str(e.get("reason") or "").startswith("degen_"):
            e["_ts"] = ts
            closes.append(e)
    closes.reverse()
    return closes


def _ts_seconds(value: Any) -> float:
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value)).timestamp()
    except Exception:
        return 0.0


def daily_pnl_sol(closes: List[Dict[str, Any]], day_start_ts: float) -> float:
    """Realized degen P&L since day_start_ts (unix seconds)."""
    return round(sum(_f(c.get("profit")) for c in closes
                     if _ts_seconds(c.get("_ts")) >= day_start_ts), 6)


def summarize(closes: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(closes)
    wins = [c for c in closes if _f(c.get("profit")) > 0]
    losses = [c for c in closes if _f(c.get("profit")) <= 0]
    gross_win = sum(_f(c.get("profit")) for c in wins)
    gross_loss = abs(sum(_f(c.get("profit")) for c in losses))
    by_tier: Dict[str, Dict[str, Any]] = {}
    for c in closes:
        tier = str(c.get("degen_tier") or "?")
        b = by_tier.setdefault(tier, {"trades": 0, "pnl": 0.0, "wins": 0})
        b["trades"] += 1
        b["pnl"] += _f(c.get("profit"))
        if _f(c.get("profit")) > 0:
            b["wins"] += 1
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / n, 1) if n else 0.0,
        "total_pnl_sol": round(sum(_f(c.get("profit")) for c in closes), 6),
        "avg_win_sol": round(gross_win / len(wins), 6) if wins else 0.0,
        "avg_loss_sol": round(gross_loss / len(losses), 6) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else 0.0,
        "best_sol": round(max((_f(c.get("profit")) for c in closes),
                              default=0.0), 6),
        "worst_sol": round(min((_f(c.get("profit")) for c in closes),
                               default=0.0), 6),
        "by_tier": {k: {"trades": v["trades"],
                        "pnl_sol": round(v["pnl"], 6),
                        "win_rate_pct": round(100.0 * v["wins"] / v["trades"], 1)}
                    for k, v in by_tier.items()},
    }


def format_card(summary: Dict[str, Any]) -> str:
    s = summary
    lines = [
        "📊 DEGEN SCOREBOARD (paper, closed positions)",
        "trades: %d · wins: %d · losses: %d · win rate: %.1f%%" % (
            s["trades"], s["wins"], s["losses"], s["win_rate_pct"]),
        "total P/L: %+.6f SOL · profit factor: %.2f" % (
            s["total_pnl_sol"], s["profit_factor"]),
        "avg win %+.6f / avg loss %+.6f SOL" % (
            s["avg_win_sol"], s["avg_loss_sol"]),
        "best %+.6f / worst %+.6f SOL" % (s["best_sol"], s["worst_sol"]),
    ]
    for tier, b in sorted(s["by_tier"].items()):
        lines.append("  %s: %d trades, %+.6f SOL, %.1f%% win" % (
            tier, b["trades"], b["pnl_sol"], b["win_rate_pct"]))
    if not s["trades"]:
        lines.append("(no closed degen positions yet)")
    return "\n".join(lines)
