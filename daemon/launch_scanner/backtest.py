"""Backtesting engine — replay recorded launch histories through entry/exit rules.

The live scanner records one JSON line per closed candidate via
``record_history()`` (see ``scripts/backtest``). ``BacktestEngine``
replays those histories through configurable entry and exit rules and
produces a compact performance report: win rate, average/median
multiple, profit factor, expectancy, max drawdown, plus per-source and
per-narrative attribution and per-gate block counts.

This module is deliberately standalone: pure logic, stdlib only, no
imports from the live scanner, no network, no secrets. The daemon's
safety gates (insider clusters, sell simulation, unknown-score
fail-closed) are enforced at record time; the backtester replays the
*recorded* safety verdicts and never re-scores anything.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
from typing import Any, Dict, List, Optional, Tuple

# Bump when the recorded-history record shape changes.
HISTORY_SCHEMA_VERSION = 1

_DEFAULT_ENTRY_RULES = {
    "min_safety_score": 50.0,
    "require_known_safety": True,
    "max_entry_mcap_usd": 500000.0,
}

_DEFAULT_EXIT_RULES = {
    # (multiple, fraction-of-original-position) sold on first touch.
    "tp_ladder": [(2.0, 0.5), (5.0, 0.25)],
    # From the peak, on the remainder, once peak reached >= 1.5x.
    "trailing_stop_pct": 30.0,
    "time_stop_h": 6.0,
}


def _f(value: Any) -> float:
    """Lenient float: None/garbage -> 0.0, never raises."""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(v) or math.isinf(v):
        return 0.0
    return v


def record_history(path: str, record: Dict[str, Any]) -> bool:
    """Append one history record as a JSON line. Crash-safe: never raises.

    Returns True on success, False on any I/O trouble.
    """
    try:
        p = os.path.expanduser(str(path))
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        return True
    except Exception:
        return False


class BacktestEngine:
    """Replay histories through entry/exit rules. Standalone, no scanner."""

    def __init__(self, entry_rules: Optional[Dict[str, Any]] = None,
                 exit_rules: Optional[Dict[str, Any]] = None):
        entry = dict(_DEFAULT_ENTRY_RULES)
        if entry_rules:
            entry.update(entry_rules)
        exitr = dict(_DEFAULT_EXIT_RULES)
        if exit_rules:
            exitr.update(exit_rules)
        self.min_safety_score = _f(entry.get("min_safety_score"))
        self.require_known_safety = bool(entry.get("require_known_safety"))
        self.max_entry_mcap = _f(entry.get("max_entry_mcap_usd"))
        # tp_ladder: list of (mult, fraction), sorted by mult ascending.
        ladder = []
        for rung in exitr.get("tp_ladder") or []:
            try:
                m, frac = rung
                m, frac = _f(m), _f(frac)
                if m > 1.0 and frac > 0:
                    ladder.append((m, min(frac, 1.0)))
            except (TypeError, ValueError):
                continue
        self.tp_ladder: List[Tuple[float, float]] = sorted(ladder)
        self.trailing_stop_pct = _f(exitr.get("trailing_stop_pct"))
        self.time_stop_h = _f(exitr.get("time_stop_h"))

    # -- entry ---------------------------------------------------------
    def _history_gate(self, hist: Dict[str, Any]) -> Tuple[bool, str]:
        """History-level gates. Returns (ok, block_reason)."""
        outcome = hist.get("outcome")
        if not isinstance(outcome, dict):
            outcome = {}
        if outcome.get("rug"):
            return False, "rug"
        safety = hist.get("safety")
        if not isinstance(safety, dict):
            safety = {}
        if self.require_known_safety and (
                safety.get("unknown") or safety.get("score") is None):
            return False, "safety-unknown"
        if _f(safety.get("score")) < self.min_safety_score:
            return False, "safety-score"
        return True, ""

    def _find_entry(self, snaps: List[Dict[str, Any]]) -> Optional[int]:
        """Index of the first snapshot satisfying entry rules, else None."""
        for i, snap in enumerate(snaps):
            if not isinstance(snap, dict):
                continue
            price = _f(snap.get("price_usd"))
            if price <= 0:
                continue
            if _f(snap.get("mcap_usd")) > self.max_entry_mcap:
                continue
            return i
        return None

    # -- exit simulation ------------------------------------------------
    def _simulate(self, snaps: List[Dict[str, Any]],
                  entry_idx: int) -> Dict[str, Any]:
        """Walk post-entry snapshots; TP ladder, trailing stop, time stop."""
        entry_snap = snaps[entry_idx]
        entry_price = _f(entry_snap.get("price_usd"))
        entry_t = _f(entry_snap.get("t"))
        deadline = entry_t + self.time_stop_h * 3600.0

        remainder = 1.0
        proceeds = 0.0
        rung_hit = [False] * len(self.tp_ladder)
        peak = 1.0
        max_dd = 0.0
        exit_reason = "final"
        last_mult = 1.0

        for snap in snaps[entry_idx + 1:]:
            if not isinstance(snap, dict):
                continue
            price = _f(snap.get("price_usd"))
            if price <= 0:
                continue
            t = _f(snap.get("t"))
            mult = price / entry_price
            last_mult = mult
            peak = max(peak, mult)
            max_dd = max(max_dd, peak - mult)

            # TP ladder rungs, first touch, executed at the rung price.
            for i, (rung_mult, frac) in enumerate(self.tp_ladder):
                if not rung_hit[i] and mult >= rung_mult:
                    sell = min(frac, remainder)
                    proceeds += sell * rung_mult
                    remainder -= sell
                    rung_hit[i] = True
            if remainder <= 1e-12:
                remainder = 0.0
                exit_reason = "tp-all"
                break

            # Trailing stop on the remainder (armed once peak >= 1.5x).
            if (self.trailing_stop_pct > 0 and peak >= 1.5
                    and mult <= peak * (1.0 - self.trailing_stop_pct / 100.0)):
                proceeds += remainder * mult
                remainder = 0.0
                exit_reason = "trailing"
                break

            # Time stop: sell the remainder at the deadline snapshot.
            if t >= deadline:
                proceeds += remainder * mult
                remainder = 0.0
                exit_reason = "time-stop"
                break

        if remainder > 0:
            proceeds += remainder * last_mult
            remainder = 0.0
        return {
            "proceeds_mult": proceeds,
            "pnl_mult": proceeds - 1.0,
            "peak_mult": peak,
            "max_drawdown_mult": max_dd,
            "exit_reason": exit_reason,
        }

    # -- realized paper tally ------------------------------------------
    @staticmethod
    def _tally_paper(paper: Dict[str, Any], hist: Dict[str, Any]) -> None:
        """Tally one realized paper outcome as reported (no simulation).

        Guards against malformed values; never raises.
        """
        mult = _f(hist.get("realized_mult"))
        if mult <= 0:
            mult = 1.0
        paper["trades"] += 1
        paper["mults"] = paper.get("mults") or []
        paper["mults"].append(mult)
        paper["total_pnl_sol"] = round(
            _f(paper.get("total_pnl_sol")) + _f(hist.get("pnl_sol")), 6)
        if hist.get("rug"):
            paper["rugs"] += 1
        if mult > 1.0:
            paper["wins"] += 1
        elif mult < 1.0:
            paper["losses"] += 1
        narrs = hist.get("narratives") or []
        if not isinstance(narrs, list):
            narrs = []
        for tag in narrs:
            tag = str(tag)
            if not tag:
                continue
            nar = paper["per_narrative"].setdefault(
                tag, {"trades": 0, "wins": 0})
            nar["trades"] += 1
            if mult > 1.0:
                nar["wins"] += 1
        reason = str(hist.get("exit_reason") or "unknown")
        er = paper["per_exit_reason"].setdefault(
            reason, {"trades": 0, "wins": 0})
        er["trades"] += 1
        if mult > 1.0:
            er["wins"] += 1

    # -- main entry ------------------------------------------------------
    def run(self, histories: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Replay histories. Returns the report dict (never raises)."""
        report: Dict[str, Any] = {
            "schema_version": HISTORY_SCHEMA_VERSION,
            "n_histories": 0,
            "n_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "avg_mult": 0.0,
            "median_mult": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_mult": 0.0,
            "expectancy_mult": 0.0,
            "per_source": {},
            "per_narrative": {},
            "per_gate_blocked": {},
            "skipped_malformed": 0,
            "trades": [],
            # Realized paper outcomes (outcome_kind="paper"): what the bot
            # actually did — never re-simulated, tallied as reported.
            "paper": {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "rugs": 0,
                "total_pnl_sol": 0.0,
                "win_rate": 0.0,
                "avg_mult": 0.0,
                "median_mult": 0.0,
                "expectancy_mult": 0.0,
                "per_narrative": {},
                "per_exit_reason": {},
            },
        }
        blocked = report["per_gate_blocked"]

        def _block(reason: str) -> None:
            blocked[reason] = blocked.get(reason, 0) + 1

        mults: List[float] = []
        gross_win = 0.0
        gross_loss = 0.0

        for hist in histories or []:
            report["n_histories"] += 1
            if not isinstance(hist, dict):
                report["skipped_malformed"] += 1
                continue
            if hist.get("outcome_kind") == "paper":
                # Real paper close — tally as reported, never re-simulate.
                self._tally_paper(report["paper"], hist)
                continue
            snaps = hist.get("snapshots")
            if not isinstance(snaps, list) or not snaps:
                _block("no-snapshots")
                continue
            ok, reason = self._history_gate(hist)
            if not ok:
                _block(reason)
                continue
            entry_idx = self._find_entry(snaps)
            if entry_idx is None:
                _block("entry-mcap")
                continue

            trade = self._simulate(snaps, entry_idx)
            pnl = trade["pnl_mult"]
            mult = trade["proceeds_mult"]
            report["n_trades"] += 1
            if pnl > 0:
                report["wins"] += 1
                gross_win += pnl
            elif pnl < 0:
                report["losses"] += 1
                gross_loss += -pnl
            mults.append(mult)
            report["max_drawdown_mult"] = max(
                report["max_drawdown_mult"], trade["max_drawdown_mult"])

            source = str(hist.get("source") or "unknown")
            src = report["per_source"].setdefault(
                source, {"trades": 0, "wins": 0})
            src["trades"] += 1
            if pnl > 0:
                src["wins"] += 1

            narrs = hist.get("narratives") or []
            if not isinstance(narrs, list):
                narrs = []
            for tag in narrs:
                tag = str(tag)
                if not tag:
                    continue
                nar = report["per_narrative"].setdefault(
                    tag, {"trades": 0, "wins": 0})
                nar["trades"] += 1
                if pnl > 0:
                    nar["wins"] += 1

            trade_rec = {
                "mint": hist.get("mint"),
                "symbol": hist.get("symbol"),
                "source": source,
                "narratives": [str(t) for t in narrs if str(t)],
                "proceeds_mult": round(mult, 4),
                "pnl_mult": round(pnl, 4),
                "exit_reason": trade["exit_reason"],
                "peak_mult": round(trade["peak_mult"], 4),
                "max_drawdown_mult": round(trade["max_drawdown_mult"], 4),
            }
            report["trades"].append(trade_rec)

        n = report["n_trades"]
        if n:
            report["win_rate"] = round(report["wins"] / n, 4)
            report["avg_mult"] = round(sum(mults) / n, 4)
            report["median_mult"] = round(statistics.median(mults), 4)
            report["expectancy_mult"] = round(report["avg_mult"] - 1.0, 4)
        if gross_loss > 0:
            report["profit_factor"] = round(gross_win / gross_loss, 4)
        elif gross_win > 0:
            report["profit_factor"] = float("inf")
        report["max_drawdown_mult"] = round(report["max_drawdown_mult"], 4)
        for bucket in (report["per_source"], report["per_narrative"]):
            for key, agg in bucket.items():
                t = agg["trades"]
                agg["win_rate"] = round(agg["wins"] / t, 4) if t else 0.0
        # Finalize realized paper stats.
        paper = report["paper"]
        mults = paper.pop("mults", None) or []
        pt = paper["trades"]
        if pt:
            paper["win_rate"] = round(paper["wins"] / pt, 4)
            paper["avg_mult"] = round(sum(mults) / len(mults), 4) if mults else 1.0
            paper["median_mult"] = round(statistics.median(mults), 4) if mults else 1.0
            paper["expectancy_mult"] = round(paper["avg_mult"] - 1.0, 4)
        for bucket in (paper["per_narrative"], paper["per_exit_reason"]):
            for key, agg in bucket.items():
                t = agg["trades"]
                agg["win_rate"] = round(agg["wins"] / t, 4) if t else 0.0
        return report


def synthetic_history(mint: str = "SYNTH1", symbol: str = "SYN",
                      chain: str = "solana", source: str = "synthetic",
                      seed: int = 1, n_snaps: int = 24,
                      start_price: float = 1e-6, drift: float = 0.02,
                      vol: float = 0.05, rug: bool = False,
                      narratives: Optional[List[str]] = None,
                      safety_score: float = 80.0) -> Dict[str, Any]:
    """Deterministic fake history for tests/demos (seeded random)."""
    rng = random.Random(seed)
    t0 = 1700000000.0
    snaps: List[Dict[str, Any]] = []
    price = max(_f(start_price), 1e-12)
    peak = 0.0
    peak_t = t0
    for i in range(max(1, int(n_snaps))):
        t = t0 + i * 60.0
        if i > 0:
            if rug and i > n_snaps // 2:
                price *= 0.05  # the rug: liquidity pulled, price collapses
            else:
                price *= max(0.0001, 1.0 + _f(drift)
                             + _f(vol) * rng.gauss(0.0, 1.0))
        price = max(price, 1e-12)
        snaps.append({
            "t": t,
            "price_usd": price,
            "mcap_usd": price * 1e6,  # small enough to pass the entry ceiling
            "vol5m_usd": price * 1e6 * 0.02 * rng.random(),
            "buys5m": rng.randint(0, 50),
            "sells5m": rng.randint(0, 40),
            "liq_usd": price * 1e6 * 0.1,
        })
        if price > peak:
            peak, peak_t = price, t
    score = _f(safety_score)
    return {
        "mint": mint,
        "symbol": symbol,
        "chain": chain,
        "source": source,
        "t0": t0,
        "snapshots": snaps,
        "safety": {"score": score, "passed": score >= 50.0,
                   "fails": [], "unknown": False},
        "sentiment": None,
        "narratives": list(narratives or []),
        "outcome": {
            "peak_mult": peak / max(_f(start_price), 1e-12),
            "peak_t": peak_t,
            "rug": bool(rug),
            "end_t": snaps[-1]["t"],
        },
    }


def _load_dataset(path: str) -> List[Dict[str, Any]]:
    histories: List[Dict[str, Any]] = []
    with open(os.path.expanduser(path), "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                histories.append(json.loads(line))
            except Exception:
                histories.append(line)  # counted as malformed, not crashed on
    return histories


def _parse_tp(spec: str) -> List[Tuple[float, float]]:
    ladder: List[Tuple[float, float]] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        m, frac = part.split(":", 1)
        m_f, f_f = _f(m), _f(frac)
        if m_f > 1.0 and f_f > 0:
            ladder.append((m_f, min(f_f, 1.0)))
    return ladder


def _print_report(report: Dict[str, Any]) -> None:
    n, t = report["n_histories"], report["n_trades"]
    print("Backtest — %d histories, %d trades" % (n, t))
    if not t:
        print("no trades taken (all histories blocked or malformed)")
    else:
        print("win rate %.1f%% (%dW/%dL)  avg %.3fx  median %.3fx" % (
            report["win_rate"] * 100.0, report["wins"], report["losses"],
            report["avg_mult"], report["median_mult"]))
        pf = report["profit_factor"]
        pf_s = "inf" if pf == float("inf") else "%.2f" % pf
        print("expectancy %+.3fx  profit factor %s  max drawdown %.3fx" % (
            report["expectancy_mult"], pf_s, report["max_drawdown_mult"]))
    for label, bucket in (("source", report["per_source"]),
                          ("narrative", report["per_narrative"])):
        if bucket:
            bits = ["%s: %d trades %.0f%%" % (
                k, v["trades"], v["win_rate"] * 100.0)
                for k, v in sorted(bucket.items())]
            print("per %s — %s" % (label, " | ".join(bits)))
    if report["per_gate_blocked"]:
        bits = ["%s=%d" % kv for kv in sorted(
            report["per_gate_blocked"].items())]
        print("blocked — " + " ".join(bits))
    if report["skipped_malformed"]:
        print("skipped malformed: %d" % report["skipped_malformed"])
    paper = report.get("paper") or {}
    if paper.get("trades"):
        print("--- REAL PAPER OUTCOMES (%d closed positions) ---" % (
            paper["trades"],))
        print("win rate %.1f%% (%dW/%dL)  avg %.3fx  expectancy %+.3fx" % (
            paper["win_rate"] * 100.0, paper["wins"], paper["losses"],
            paper["avg_mult"], paper["expectancy_mult"]))
        print("realized P&L %+.6f SOL  rugs %d" % (
            paper["total_pnl_sol"], paper["rugs"]))
        for label, bucket in (("narrative", paper["per_narrative"]),
                              ("exit", paper["per_exit_reason"])):
            if bucket:
                bits = ["%s: %d trades %.0f%%" % (
                    k, v["trades"], v["win_rate"] * 100.0)
                    for k, v in sorted(bucket.items())]
                print("paper per %s — %s" % (label, " | ".join(bits)))
    else:
        print("no real paper closes recorded yet (paper positions close "
              "into this section)")


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry: replay a recorded dataset and print the report."""
    ap = argparse.ArgumentParser(
        prog="backtest",
        description="Replay recorded launch histories through entry/exit "
                    "rules (paper only, no network).")
    ap.add_argument("dataset", nargs="?",
                    default="~/.forex-agent/launch_scanner/backtest_dataset.jsonl",
                    help="JSONL history dataset path")
    ap.add_argument("--min-safety", type=float, default=None,
                    help="override entry min safety score (default 50.0)")
    ap.add_argument("--tp", default=None,
                    help='override TP ladder, e.g. "2:0.5,5:0.25"')
    ap.add_argument("--trailing", type=float, default=None,
                    help="override trailing stop pct (default 30)")
    ap.add_argument("--time-stop-h", type=float, default=None,
                    help="override time stop hours (default 6)")
    args = ap.parse_args(argv)

    entry_rules: Dict[str, Any] = {}
    if args.min_safety is not None:
        entry_rules["min_safety_score"] = args.min_safety
    exit_rules: Dict[str, Any] = {}
    if args.tp is not None:
        exit_rules["tp_ladder"] = _parse_tp(args.tp)
    if args.trailing is not None:
        exit_rules["trailing_stop_pct"] = args.trailing
    if args.time_stop_h is not None:
        exit_rules["time_stop_h"] = args.time_stop_h

    if not os.path.exists(os.path.expanduser(args.dataset)):
        print("no data yet — the recorder builds it from live paper flow")
        print("(expected at %s)" % os.path.expanduser(args.dataset))
        return 0
    histories = _load_dataset(args.dataset)
    engine = BacktestEngine(entry_rules=entry_rules, exit_rules=exit_rules)
    _print_report(engine.run(histories))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
