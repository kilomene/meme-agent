"""Insider behavior tracker (v1): watch what insiders DO, not just the count.

The hard gate blocks on raw counts (graph insiders > 5, insider cluster
share > 20%). This module adds the missing dimension: it snapshots the
insider supply share / graph-insider count per watched mint and re-checks
periodically, so the warning card can say what the insiders are doing:

  "insiders 12% -> 9% (diluting)"      public buying in, healthy
  "insiders 8% -> 18% (accumulating)"  concentration rising, caution
  "insiders steady at 11%"

v1 is measurement + display only. The hard gate is NOT changed here —
any behavior-based gate change waits for real data from this tracker.
Cheap by design: re-uses the existing free safety check, throttled to a
few refreshes per cycle, capped watchlist, 24h pruning. Never raises.

Fast-start: a freshly watched mint refreshes every
``degen.insider_fast_minutes`` (default 5) for the first
``degen.insider_fast_window_minutes`` (default 60), so the warning card
carries a real insider trend while the coin is still hot. After the
window it falls back to ``degen.insider_refresh_minutes``.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("forex_agent.daemon.launch_scanner.insiders")

_CLUSTER_RE = re.compile(r"insider cluster holds\s+(\d+(?:\.\d+)?)\s*%", re.I)
_CLUSTER_FLAG_RE = re.compile(r"insider network holds\s+(\d+(?:\.\d+)?)\s*%", re.I)
_GRAPH_RE = re.compile(r"(\d+)\s+graph insiders", re.I)


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _cfg(degen: Any, name: str, default: Any) -> Any:
    return getattr(degen, name, default) if degen is not None else default


def parse_insider_signals(report: Optional[Dict[str, Any]]) -> Tuple[float, int]:
    """(cluster_pct, graph_insider_count) parsed from a safety report.

    Returns (0.0, 0) when nothing parseable — the caller treats that as
    "no insider signal", never as a fail.
    """
    cluster = 0.0
    graph_n = 0
    try:
        rep = report or {}
        texts = [str(x) for x in (rep.get("fails") or [])]
        texts += [str(x) for x in (rep.get("flags") or [])]
        for text in texts:
            m = _CLUSTER_RE.search(text) or _CLUSTER_FLAG_RE.search(text)
            if m:
                cluster = max(cluster, _f(m.group(1)))
            g = _GRAPH_RE.search(text)
            if g:
                graph_n = max(graph_n, int(_f(g.group(1))))
    except Exception:
        pass
    return cluster, graph_n


def has_insider_signal(report: Optional[Dict[str, Any]]) -> bool:
    cluster, graph_n = parse_insider_signals(report)
    return cluster > 0.0 or graph_n > 0


def watch(state: Dict[str, Any], degen: Any, mint: str, chain: str,
          report: Optional[Dict[str, Any]]) -> None:
    """Start tracking a mint's insider footprint (idempotent)."""
    try:
        if not mint or not bool(_cfg(degen, "insider_track_enabled", True)):
            return
        if not has_insider_signal(report):
            return
        cluster, graph_n = parse_insider_signals(report)
        table = state.setdefault("insider_watch", {})
        if not isinstance(table, dict):
            return
        if mint in table:
            return
        if len(table) >= int(_cfg(degen, "insider_watch_max", 40)):
            return
        now = time.time()
        table[mint] = {
            "chain": chain or "solana",
            "first_cluster_pct": cluster,
            "first_graph_n": graph_n,
            "first_ts": now,
            "last_cluster_pct": cluster,
            "last_graph_n": graph_n,
            "last_ts": now,
            "samples": 1,
        }
    except Exception:
        logger.exception("insiders watch failed")


def _prune(table: Dict[str, Any], now: float) -> None:
    try:
        stale = [m for m, w in table.items()
                 if now - _f((w or {}).get("last_ts")) > 24 * 3600.0]
        for m in stale:
            table.pop(m, None)
    except Exception:
        pass


def _interval_for(w: Dict[str, Any], degen: Any, now: float) -> float:
    """Per-mint snapshot interval in seconds. Never raises."""
    try:
        normal = _f(_cfg(degen, "insider_refresh_minutes", 15.0)) * 60.0
        fast_min = _f(_cfg(degen, "insider_fast_minutes", 5.0))
        window_min = _f(_cfg(degen, "insider_fast_window_minutes", 60.0))
        if fast_min > 0 and window_min > 0:
            first_ts = _f(w.get("first_ts")) or _f(w.get("last_ts"))
            if now - first_ts < window_min * 60.0:
                return max(60.0, fast_min * 60.0)  # no sub-minute storms
        return normal if normal > 0 else 15.0 * 60.0
    except Exception:
        return 15.0 * 60.0


def update(state: Dict[str, Any], degen: Any, max_refresh: int = 3) -> None:
    """Refresh a few watched mints' insider footprints. Never raises."""
    try:
        if not bool(_cfg(degen, "insider_track_enabled", True)):
            return
        table = state.get("insider_watch")
        if not isinstance(table, dict) or not table:
            return
        now = time.time()
        _prune(table, now)
        from daemon.launch_scanner import safety  # noqa: PLC0415
        done = 0
        for mint, w in list(table.items()):
            if done >= max_refresh:
                break
            w = w or {}
            if now - _f(w.get("last_ts")) < _interval_for(w, degen, now):
                continue
            try:
                rep = safety.check_token(
                    w.get("chain") or "solana", mint, timeout=6.0)
            except Exception:
                continue
            cluster, graph_n = parse_insider_signals(rep)
            w["last_cluster_pct"] = cluster
            w["last_graph_n"] = graph_n
            w["last_ts"] = now
            w["samples"] = int(w.get("samples") or 0) + 1
            done += 1
    except Exception:
        logger.exception("insiders update failed")


def trend(state: Dict[str, Any], mint: str) -> Optional[Dict[str, Any]]:
    """Current insider trend for a mint, or None when not watched."""
    try:
        table = state.get("insider_watch") or {}
        w = table.get(mint)
        if not isinstance(w, dict):
            return None
        first = _f(w.get("first_cluster_pct"))
        last = _f(w.get("last_cluster_pct"))
        delta = last - first
        if abs(delta) < 1.0:
            direction = "steady"
        elif delta < 0:
            direction = "diluting"
        else:
            direction = "accumulating"
        return {
            "first_cluster_pct": round(first, 1),
            "last_cluster_pct": round(last, 1),
            "first_graph_n": int(w.get("first_graph_n") or 0),
            "last_graph_n": int(w.get("last_graph_n") or 0),
            "direction": direction,
            "samples": int(w.get("samples") or 0),
        }
    except Exception:
        return None


def trend_line(state: Dict[str, Any], mint: str) -> str:
    """One-line insider behavior summary for a Telegram card."""
    try:
        t = trend(state, mint)
        if not t:
            return ""
        first, last = t["first_cluster_pct"], t["last_cluster_pct"]
        d = t["direction"]
        if d == "steady":
            mood = "holding steady"
        elif d == "diluting":
            mood = "diluting — public buying in"
        else:
            mood = "accumulating — caution"
        line = "🧠 Insiders: %.0f%% → %.0f%% (%s)" % (first, last, mood)
        if t["last_graph_n"]:
            line += " · %d graph insiders" % t["last_graph_n"]
        return line
    except Exception:
        return ""


# ----------------------------------------------------------------------
# Phase 1: insider outcome ledger
# ----------------------------------------------------------------------
# Measurement only. When the shadow ledger closes a paper-simulated
# position on a coin we were insider-tracking, record which way the
# insider footprint moved (first -> last snapshot) next to the outcome.
# Over time this answers: do accumulating-insider coins actually beat
# diluting-insider ones? No gate reads this ledger; it is evidence for
# humans, not an input to trading decisions.

def _ledger_cap(degen) -> int:
    try:
        return max(1, int(getattr(degen, "insider_ledger_max", 200)))
    except Exception:
        return 200


def record_outcome(state: Dict[str, Any], degen: Any, mint: str,
                   outcome: Dict[str, Any]) -> bool:
    """Join a shadow close with the insider trend for that mint.

    `outcome` carries symbol/chain/outcome ("win"|"loss"|"timeout") and
    pnl_pct. Appends one row to state["insider_outcomes"]. Never raises.
    """
    try:
        watch = state.get("insider_watch")
        if not isinstance(watch, dict):
            return False
        entry = watch.get(mint)
        if not isinstance(entry, dict):
            return False  # not tracked (or pruned) — nothing to join
        t = trend(state, mint) or {}
        direction = t.get("direction") or "steady"
        ledger = state.setdefault("insider_outcomes", [])
        if not isinstance(ledger, list):
            state["insider_outcomes"] = ledger = []
        ledger.append({
            "mint": mint,
            "symbol": (outcome.get("symbol") if isinstance(outcome, dict)
                       else "") or entry.get("symbol") or "",
            "chain": (outcome.get("chain") if isinstance(outcome, dict)
                      else "") or entry.get("chain") or "",
            "direction": direction,
            "cluster_start": entry.get("first_cluster_pct", 0.0),
            "cluster_end": entry.get("last_cluster_pct", 0.0),
            "graph_start": entry.get("first_graph_n", 0),
            "graph_end": entry.get("last_graph_n", 0),
            "outcome": (outcome.get("outcome") if isinstance(outcome, dict)
                        else ""),
            "pnl_pct": round(float((outcome.get("pnl_pct") if isinstance(
                outcome, dict) else 0.0) or 0.0), 2),
            "ts": time.time(),
        })
        del ledger[:-_ledger_cap(degen)]
        return True
    except Exception:
        logger.warning("insider record_outcome failed", exc_info=True)
        return False


def outcome_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    """Aggregate the outcome ledger per insider direction. Never raises."""
    try:
        rows = [r for r in (state.get("insider_outcomes") or [])
                if isinstance(r, dict)]
        by_dir: Dict[str, Dict[str, Any]] = {}
        for d in ("accumulating", "diluting", "steady"):
            ds = [r for r in rows if r.get("direction") == d]
            pnls = [float(r.get("pnl_pct") or 0.0) for r in ds]
            wins = sum(1 for r in ds if r.get("outcome") == "win")
            by_dir[d] = {
                "n": len(ds),
                "wins": wins,
                "win_rate": round(wins / len(ds) * 100.0, 1) if ds else 0.0,
                "avg_pnl": round(sum(pnls) / len(pnls), 1) if pnls else 0.0,
            }
        all_pnls = [float(r.get("pnl_pct") or 0.0) for r in rows]
        return {
            "total": len(rows),
            "by_direction": by_dir,
            "avg_pnl": round(sum(all_pnls) / len(all_pnls), 1)
            if all_pnls else 0.0,
        }
    except Exception:
        return {"total": 0, "by_direction": {}, "avg_pnl": 0.0}
