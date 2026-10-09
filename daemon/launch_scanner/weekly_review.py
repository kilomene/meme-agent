"""Weekly paper-trading review.

Reads the backtest dataset (real paper closes + judge replays) and prints
a structured report for the weekly review: what actually happened, which
narratives / exit reasons / safety bands produced the results, and whether
the data is thick enough to tune anything yet.

Paper only, no network. Run via scripts/weekly-paper-review.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List

DATASET = os.path.expanduser(
    "~/.forex-agent/launch_scanner/backtest_dataset.jsonl")
WEEK_S = 7 * 24 * 3600
MIN_CLOSES_FOR_TUNING = 30
WIN_MULT = 1.5  # realized >= 1.5x counts as a win


def _load() -> List[Dict[str, Any]]:
    recs = []
    if not os.path.exists(DATASET):
        return recs
    with open(DATASET) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except Exception:
                continue
    return recs


def _week(recs, now):
    return [r for r in recs
            if (r.get("t_close") or r.get("t0") or 0) >= now - WEEK_S]


def _fmt_pct(n, d):
    return "n/a" if not d else "%.1f%%" % (100.0 * n / d)


def _safety_score(r: Dict[str, Any]):
    """Extract a numeric 0-100 safety score from a record.

    Records store ``safety`` as a dict like {'score': n, ...} (score may be
    None for unknown); older writers may have stored a bare number. Returns
    None when no numeric score is available.
    """
    v = r.get("safety")
    if isinstance(v, dict):
        v = v.get("score")
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    return None


def _paper_section(papers: List[Dict[str, Any]]) -> List[str]:
    out = ["== REAL PAPER CLOSES (last 7 days) =="]
    if not papers:
        out.append("No paper positions closed this week.")
        out.append("The bot opened no qualifying positions, or none closed yet.")
        return out
    n = len(papers)
    wins = [r for r in papers if (r.get("realized_mult") or 1.0) >= WIN_MULT]
    losses = [r for r in papers if (r.get("realized_mult") or 1.0) < 1.0]
    breakeven = n - len(wins) - len(losses)
    mults = [(r.get("realized_mult") or 1.0) for r in papers]
    avg_mult = sum(mults) / n
    pnl = sum(float(r.get("pnl_sol") or 0) for r in papers)
    rugs = sum(1 for r in papers if r.get("rug"))
    out.append(f"closes: {n} | wins(>=1.5x): {len(wins)} | "
               f"losses(<1x): {len(losses)} | flat: {breakeven}")
    out.append(f"win rate: {_fmt_pct(len(wins), n)} | avg realized: "
               f"{avg_mult:.3f}x | expectancy: {avg_mult - 1:+.3f}x")
    out.append(f"total realized P&L: {pnl:+.6f} SOL | rugs: {rugs}")
    by_exit = Counter(r.get("exit_reason") or "?" for r in papers)
    out.append("by exit: " + " | ".join(
        f"{k}: {v}" for k, v in by_exit.most_common()))
    by_narr = defaultdict(list)
    for r in papers:
        for tag in (r.get("narratives") or ["other"]):
            by_narr[tag].append(r.get("realized_mult") or 1.0)
    out.append("by narrative (trades, win rate, avg x):")
    for tag, ms in sorted(by_narr.items(),
                          key=lambda kv: sum(kv[1]) / len(kv[1]),
                          reverse=True):
        w = sum(1 for m in ms if m >= WIN_MULT)
        out.append(f"  {tag}: {len(ms)} trades, {_fmt_pct(w, len(ms))}, "
                   f"{sum(ms) / len(ms):.2f}x avg")
    w_saf = [_safety_score(r) for r in wins]
    w_saf = [s for s in w_saf if s is not None]
    l_saf = [_safety_score(r) for r in papers
             if (r.get("realized_mult") or 1.0) < 1.0]
    l_saf = [s for s in l_saf if s is not None]
    if w_saf:
        out.append(f"avg safety of winners: {sum(w_saf) / len(w_saf):.0f}/100 "
                   f"(n={len(w_saf)})")
    if l_saf:
        out.append(f"avg safety of losers: "
                   f"{sum(l_saf) / len(l_saf):.0f}/100 "
                   f"(n={len(l_saf)})")
    out.append("top closes:")
    for r in sorted(papers, key=lambda r: r.get("realized_mult") or 0,
                    reverse=True)[:5]:
        out.append(f"  {r.get('symbol')} x{r.get('realized_mult')} "
                   f"({r.get('exit_reason')}, pnl {r.get('pnl_sol'):+.4f} SOL)")
    out.append("worst closes:")
    for r in sorted(papers, key=lambda r: r.get("realized_mult") or 0)[:5]:
        out.append(f"  {r.get('symbol')} x{r.get('realized_mult')} "
                   f"({r.get('exit_reason')}, pnl {r.get('pnl_sol'):+.4f} SOL)")
    return out


def _judge_section(recs: List[Dict[str, Any]]) -> List[str]:
    out = ["== JUDGE REPLAYS (last 7 days, simulated) =="]
    judge = [r for r in recs if r.get("outcome_kind") == "judge"
             or "outcome_kind" not in r]
    if not judge:
        out.append("no replay histories recorded this week.")
        return out
    out.append(f"histories: {len(judge)} | "
               f"verdicts: {dict(Counter(r.get('verdict') for r in judge))}")
    return out


def _verdict_section(all_papers: List[Dict[str, Any]]) -> List[str]:
    out = ["== DATA SUFFICIENCY =="]
    n = len(all_papers)
    out.append(f"lifetime real closes: {n} "
               f"(tuning threshold: {MIN_CLOSES_FOR_TUNING})")
    if n < 10:
        out.append("VERDICT: too thin — no tuning yet. Keep collecting; "
                   "the gate stays as-is.")
    elif n < MIN_CLOSES_FOR_TUNING:
        out.append("VERDICT: building — directional reads only, "
                   "no threshold changes yet.")
    else:
        out.append("VERDICT: enough data to tune entry bar / narrative "
                   "weights with evidence.")
    return out


def main(argv: List[str]) -> int:
    now = time.time()
    recs = _load()
    week = _week(recs, now)
    papers_week = [r for r in week if r.get("outcome_kind") == "paper"]
    papers_all = [r for r in recs if r.get("outcome_kind") == "paper"]
    lines = []
    lines.append("WEEKLY PAPER REVIEW")
    lines.append(time.strftime("week ending %Y-%m-%d %H:%M WAT",
                               time.localtime(now)))
    lines.append("")
    lines.extend(_paper_section(papers_week))
    lines.append("")
    lines.extend(_judge_section(week))
    lines.append("")
    lines.extend(_verdict_section(papers_all))
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
