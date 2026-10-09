#!/usr/bin/env python3
"""One-off: re-adopt orphaned paper-broker tickets into the launch
scanner's degen_positions so the exit manager can close them.

Root cause (fixed 2026-10-05 in daemon/launch_scanner/__init__.py):
run_once() reloaded daemon state from disk every cycle while the full
save was throttled to 300s, so position records written in non-save
cycles were lost on the next reload. Broker tickets in paper_state.json
survived — N positions the exit manager could never see or close.

What this does:
  * finds broker tickets not tracked in the scanner's degen_positions
  * rebuilds minimal position records (ticket/symbol/mint/entry/sl/tp/
    entry_ts/leg) shaped like _leg_pos_dict, keyed "reconciled:<mint>"
  * drops zombie scanner keys whose ticket is gone from the broker
  * saves via save_daemon_state; the scanner picks them up next cycle
    and closes them through the normal exit path (max_hold at the live
    quote, or dead_pool at ~0 when the quote is confirmed gone), with
    full close cards, fills, and backtest-dataset records.

Usage:
  reconcile_orphans.py [--apply] [--batch N]

  default: dry run — prints what would change, writes nothing.
  --apply: writes into the live daemon state journal.
  --batch N: adopt only the N oldest orphans (stagger to avoid a
             wall of close cards; e.g. three runs of --batch 10).

Deploy order: (1) the dead-pool + write-through code must be live
(i.e. restart the scanner AFTER the fix is deployed), (2) run this
with --apply, (3) re-run (dry) to confirm zero orphans remain.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

AGENT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, AGENT_ROOT)
os.environ.setdefault("FOREX_AGENT_HOME", os.path.expanduser("~/.forex-agent"))
HOME = os.path.expanduser("~/.forex-agent")
os.environ.setdefault("FOREX_AGENT_STORAGE",
                       os.path.join(HOME, "storage", "local.db"))

from storage import Store  # noqa: E402
from daemon.common import get_daemon_state, save_daemon_state  # noqa: E402

BROKER_STATE = os.path.join(HOME, "data", "memecoin", "paper_state.json")
TWO_TIER_LIVE_TS = datetime(2026, 10, 1, 22, 25,
                            tzinfo=timezone.utc).timestamp()


def _parse_mint(signal_id):
    # "launch:<mint>" or "launch:<mint>:B"
    parts = (signal_id or "").split(":")
    if len(parts) >= 2 and parts[0] == "launch":
        return parts[1]
    return ""


def _parse_ts(time_open):
    try:
        dt = datetime.fromisoformat(str(time_open))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return time.time()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--batch", type=int, default=0)
    args = ap.parse_args()

    broker = json.load(open(BROKER_STATE))
    bpos = broker.get("positions") or {}

    store = Store()
    state = get_daemon_state(store, "launch_scanner") or {}
    degen = state.get("degen_positions") or {}
    degen_tickets = {str(v.get("ticket")) for v in degen.values()
                     if isinstance(v, dict)}

    # Zombies: tracked by the scanner but gone from the broker.
    zombies = [k for k, v in degen.items()
               if isinstance(v, dict)
               and str(v.get("ticket")) not in {str(t) for t in bpos}]

    # Orphans: in the broker but invisible to the exit manager.
    orphans = [t for t in bpos if str(t) not in degen_tickets]
    orphans.sort(key=lambda t: _parse_ts(bpos[t].get("time_open")))
    if args.batch and args.batch > 0:
        orphans = orphans[:args.batch]

    # Sibling detection for two-tier A/B legs (comment "degen-launch-B").
    by_symbol_time = {}
    for t in bpos:
        p = bpos[t]
        key = (p.get("symbol"), str(p.get("time_open"))[:16])
        by_symbol_time.setdefault(key, []).append(t)

    adopted = []
    for t in orphans:
        p = bpos[t]
        comment = p.get("comment") or ""
        mint = _parse_mint(p.get("signal_id")) or "unknown"
        entry_ts = _parse_ts(p.get("time_open"))
        if comment.endswith("-B"):
            leg, mh = "B", 24.0
        elif entry_ts < TWO_TIER_LIVE_TS:
            leg, mh = None, 4.0  # legacy single-leg position
        else:
            leg, mh = "A", 4.0
        key = "reconciled:%s%s" % (mint, "|B" if leg == "B" else "")
        rec = {
            "ticket": int(t),
            "symbol": p.get("symbol") or "",
            "mint": mint,
            "wave": 1,
            "wave_number": 1,
            "label": "reconciled",   # lane tag: orphan re-adoption
            "leg": leg,
            "entry_price_sol": float(p.get("entry_price_sol") or 0),
            "peak_price_sol": float(p.get("entry_price_sol") or 0),
            "entry_ts": entry_ts,
            "tier": "?",
            "sl": p.get("sl"),
            "tp": p.get("tp"),
            "volume_sol": float(p.get("volume_sol") or 0),
            "max_hold_hours": mh,
            "source": "",
            "narratives": ["other"],
            "narrative_heat": 1.0,
            "sentiment": None,
            "sentiment_verdict": None,
            "sniper_score": None,
            "entry_signals": None,
            "safety_score": None,
            "safety_fails": [],
            "deployer": None,
        }
        adopted.append((key, t, p.get("symbol"), leg, rec))

    print("broker open positions : %d" % len(bpos))
    print("scanner tracked       : %d" % len(degen))
    print("zombie keys to drop   : %d %s" % (len(zombies), zombies[:10]))
    print("orphans to adopt      : %d" % len(adopted))
    for key, t, sym, leg, rec in adopted:
        age_h = (time.time() - rec["entry_ts"]) / 3600.0
        print("  ticket %-4s %-12s leg=%-4s age=%6.1fh key=%s"
              % (t, sym, leg, age_h, key))

    if not args.apply:
        print("\nDRY RUN — no state written. Re-run with --apply.")
        return 0

    for k in zombies:
        degen.pop(k, None)
    for key, t, sym, leg, rec in adopted:
        degen[key] = rec
    state["degen_positions"] = degen
    save_daemon_state(store, "launch_scanner", state)

    # Verify the write survived (a concurrent scanner cycle could have
    # clobbered it — re-run if so).
    check = get_daemon_state(store, "launch_scanner") or {}
    ckeys = set((check.get("degen_positions") or {}).keys())
    missing = [k for k, _, _, _, _ in adopted if k not in ckeys]
    if missing:
        print("WARNING: %d adopted keys missing after save "
              "(scanner cycle raced the write) — re-run." % len(missing))
        return 2
    print("\nAPPLIED and verified: %d orphans adopted, %d zombies dropped."
          % (len(adopted), len(zombies)))
    print("The scanner will close them on its next cycles "
          "(max_hold / dead_pool) with full cards + dataset records.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
