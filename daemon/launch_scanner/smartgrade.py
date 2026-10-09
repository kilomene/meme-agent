"""Per-wallet PnL grading for tracked smart-money wallets.

Measurement only — grading NEVER auto-adds or removes wallets; the user
decides via /smartadd / /smartremove.

Pipeline (wired by the integration step, never by this module):

1. After each ``smartmoney.poll_wallets()`` call, the scanner passes the
   fresh ``{mint: [wallet_addresses]}`` activity to :func:`record_wallet_buys`.
   One buy record per (wallet, mint); repeat buys of the same mint within
   ``smart_grade_dedup_days`` (default 7) are NOT re-recorded.

2. Periodically (every ~10 scanner cycles is plenty) the scanner calls
   :func:`reconcile` with the launch_scanner daemon state. It anchors each
   unanchored buy to the milestone tracker's card-time mcap
   (``state["milestone_watch"][mint]["anchor"]`` — the same anchor the
   pending-entry fix backfills) and reads that record's ``hit`` list and
   ``peak_mult`` for outcomes.

Honesty notes:
- Multiples are PEAK multiples of OUR card-time anchor within the
  milestone tracker's 24h window — a measure of what the wallet's pick
  did at its best, NOT the wallet's realized PnL (we don't see their
  exits). The metric "reached 2x" means "crossed 2x of our card anchor
  within 24h".
- Buys whose mint was never carded stay unanchored and count toward
  "buys tracked" but are excluded from all multiple math.

Storage: ``~/.forex-agent/launch_scanner/smart_wallet_grades.json``
(0600), one file shared by both daemons like the wallet registry::

    {wallet: {"buys": [{"mint", "buy_ts", "buy_mcap", "anchor_mcap",
                        "hit_2x", "peak_mult", "last_check_ts"}],
               "last_active_ts": ...}}
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("forex_agent.daemon.launch_scanner.smartgrade")

_HOME = Path(os.environ.get("FOREX_AGENT_HOME", Path.home() / ".forex-agent"))
_GRADES_FILE = _HOME / "launch_scanner" / "smart_wallet_grades.json"

_DEFAULT_DEDUP_DAYS = 7.0
_DEFAULT_WINDOW_DAYS = 30.0
# Watch records expire after 24h; keep graded buys a little longer than
# the trailing window so the report's denominator is stable.
_PRUNE_AFTER_DAYS = 45.0


def _num(name: str, degen: Any, default: float) -> float:
    try:
        v = getattr(degen, name, default)
        return default if v is None else float(v)
    except (TypeError, ValueError):
        return default


def load_grades() -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(_GRADES_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_grades(grades: Dict[str, Dict[str, Any]]) -> bool:
    try:
        _GRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _GRADES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(grades))
        os.chmod(tmp, 0o600)
        tmp.replace(_GRADES_FILE)
        return True
    except OSError as exc:
        logger.warning("could not save smart wallet grades: %s", exc)
        return False


def _recent_buy(rec: Dict[str, Any], mint: str,
               cutoff: float) -> bool:
    for b in rec.get("buys") or []:
        if (b.get("mint") == mint
                and float(b.get("buy_ts") or 0) >= cutoff):
            return True
    return False


def record_wallet_buys(fresh: Dict[str, List[str]],
                       degen: Any = None) -> int:
    """Record first-seen wallet buys from one poll's fresh activity.

    ``fresh`` is ``{mint: [wallet_addresses]}`` as returned by
    ``smartmoney.poll_wallets()``. Returns the number of new buy records.
    Never raises.
    """
    try:
        if not fresh:
            return 0
        dedup_days = _num("smart_grade_dedup_days", degen,
                          _DEFAULT_DEDUP_DAYS)
        cutoff = time.time() - dedup_days * 86400.0
        grades = load_grades()
        now = time.time()
        added = 0
        for mint, wallets in (fresh or {}).items():
            if not mint:
                continue
            for wallet in wallets or []:
                if not wallet:
                    continue
                rec = grades.setdefault(
                    wallet, {"buys": [], "last_active_ts": 0.0})
                rec["last_active_ts"] = now
                if _recent_buy(rec, mint, cutoff):
                    continue  # same mint, already on the ledger
                rec["buys"].append({
                    "mint": mint,
                    "buy_ts": now,
                    "buy_mcap": None,
                    "anchor_mcap": None,
                    "hit_2x": False,
                    "peak_mult": 0.0,
                    "last_check_ts": 0.0,
                })
                added += 1
        if added:
            save_grades(grades)
        return added
    except Exception as exc:
        logger.warning("record_wallet_buys failed: %s", exc)
        return 0


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def reconcile(launch_state: Dict[str, Any], degen: Any = None) -> int:
    """Join buy ledger with milestone-tracker outcomes.

    Anchors unanchored buys to the card-time mcap and refreshes
    ``hit_2x`` / ``peak_mult`` from the watch record. Watch records
    expire after 24h — a buy whose watch is gone keeps its last recorded
    values (frozen at window end). Prunes buys older than
    ``_PRUNE_AFTER_DAYS``. Returns the number of buys updated. Never
    raises.
    """
    try:
        grades = load_grades()
        if not grades:
            return 0
        watch = (launch_state or {}).get("milestone_watch") or {}
        now = time.time()
        prune_before = now - _PRUNE_AFTER_DAYS * 86400.0
        updated = 0
        for wallet, rec in list(grades.items()):
            buys = rec.get("buys") or []
            kept: List[Dict[str, Any]] = []
            for b in buys:
                if float(b.get("buy_ts") or 0) < prune_before:
                    continue  # too old to matter; drop
                kept.append(b)
                mint = b.get("mint")
                wrec = watch.get(mint) if mint else None
                if not isinstance(wrec, dict):
                    continue  # never carded or watch expired; freeze
                anchor = _f(wrec.get("anchor"))
                if anchor > 0 and not b.get("anchor_mcap"):
                    b["anchor_mcap"] = anchor
                    b["buy_mcap"] = anchor
                    updated += 1
                hits = wrec.get("hit") or []
                hit_2x = any(_f(h) >= 2.0 for h in hits)
                peak = _f(wrec.get("peak_mult"))
                if hit_2x != bool(b.get("hit_2x")) or peak > _f(
                        b.get("peak_mult")):
                    b["hit_2x"] = hit_2x
                    b["peak_mult"] = max(peak, _f(b.get("peak_mult")))
                    b["last_check_ts"] = now
                    updated += 1
            rec["buys"] = kept
            if not kept and not float(rec.get("last_active_ts") or 0):
                grades.pop(wallet, None)
        if updated:
            save_grades(grades)
        return updated
    except Exception as exc:
        logger.warning("grade reconcile failed: %s", exc)
        return 0


def grade_wallet(wallet: str, registry: Dict[str, Dict[str, Any]],
                 degen: Any = None,
                 now: Optional[float] = None) -> Dict[str, Any]:
    """Grade one wallet over the trailing window.

    Returns a dict with: wallet, label, buys_tracked (all buys in
    window), buys_graded (anchored to a card mcap), n_2x, pct_2x (of
    graded buys), median_mult, best_mult, worst_mult (peak multiples of
    graded buys), last_active_ts. Unanchored buys never enter multiple
    math. Empty history → zeros, rendered gracefully by the formatter.
    """
    now = now if now is not None else time.time()
    window_days = _num("smart_grade_window_days", degen,
                      _DEFAULT_WINDOW_DAYS)
    cutoff = now - window_days * 86400.0
    info = registry.get(wallet) or {}
    grades = load_grades()
    rec = grades.get(wallet) or {}
    buys = [b for b in (rec.get("buys") or [])
            if float(b.get("buy_ts") or 0) >= cutoff]
    graded = [b for b in buys if _f(b.get("anchor_mcap")) > 0]
    mults = sorted(_f(b.get("peak_mult")) for b in graded)
    n_2x = sum(1 for b in graded if b.get("hit_2x"))
    return {
        "wallet": wallet,
        "label": info.get("label") or "",
        "short": str(wallet)[:10] + "…",
        "buys_tracked": len(buys),
        "buys_graded": len(graded),
        "n_2x": n_2x,
        "pct_2x": round(100.0 * n_2x / len(graded), 1) if graded else 0.0,
        "median_mult": round(statistics.median(mults), 2) if mults else 0.0,
        "best_mult": round(mults[-1], 2) if mults else 0.0,
        "worst_mult": round(mults[0], 2) if mults else 0.0,
        "last_active_ts": float(rec.get("last_active_ts") or 0),
    }


def _ago(ts: float, now: float) -> str:
    if not ts:
        return "never"
    mins = (now - ts) / 60.0
    if mins < 1:
        return "just now"
    if mins < 60:
        return "%dm ago" % int(mins)
    hrs = mins / 60.0
    if hrs < 24:
        return "%.1fh ago" % hrs
    return "%.1fd ago" % (hrs / 24.0)


def format_grades(registry: Dict[str, Dict[str, Any]],
                  degen: Any = None,
                  now: Optional[float] = None) -> str:
    """Clean table of per-wallet grades for the /smartgrade reply."""
    now = now if now is not None else time.time()
    window_days = _num("smart_grade_window_days", degen,
                      _DEFAULT_WINDOW_DAYS)
    lines = ["🧠 Smart-wallet grades (trailing %dd):" % int(window_days)]
    for wallet in sorted(registry):
        g = grade_wallet(wallet, registry, degen, now)
        name = g["label"] or g["short"]
        if not g["buys_tracked"]:
            lines.append("• %s — no buys in trailing %dd" % (
                name, int(window_days)))
            continue
        unanchored = g["buys_tracked"] - g["buys_graded"]
        stat = ("%d buys · %d graded · %d 2x (%0.0f%%) · "
                "med %.1fx · best %.1fx · worst %.1fx · active %s" % (
                    g["buys_tracked"], g["buys_graded"], g["n_2x"],
                    g["pct_2x"], g["median_mult"], g["best_mult"],
                    g["worst_mult"], _ago(g["last_active_ts"], now)))
        if unanchored:
            stat += " · %d uncarded" % unanchored
        lines.append("• %s — %s" % (name, stat))
    lines.append("")
    lines.append("Multiples = peak multiple of our card anchor within "
                 "24h (measurement only, not realized PnL). Grading never "
                 "adds/removes wallets — /smartadd / /smartremove decide.")
    return "\n".join(lines)
