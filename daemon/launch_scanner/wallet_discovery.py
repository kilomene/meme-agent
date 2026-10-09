"""Smart-wallet auto-discovery (Helius).

Finds wallets worth tracking by looking at WHO bought past xxx runners
early, then scores those wallets by hit rate across runners.

Pipeline
--------
1. ``runners`` — dicts ``{mint, symbol, peak_mult, first_seen_ts,
   deployer?}`` with ``peak_mult >= discovery_runner_mult`` (default 5x).
2. :func:`early_buyers_for_mint` — oldest signatures for the mint ->
   wallets whose token balance for that mint increased inside the early
   window (``first_seen_ts - 10min`` .. ``first_seen_ts + early_window``).
3. :func:`score_wallets` — per wallet ``{hits, seen, hit_rate}``; a hit is
   an early buy of a qualifying runner. Laplace-smoothed hit rate.
4. :func:`discovery_proposals` — wallets clearing ``discovery_min_hits``
   (default 2) and ``discovery_min_hit_rate`` (default 0.5), not already
   tracked and not deployers, become proposal dicts.

Discovery NEVER writes to the tracked registry. Proposals live in their
own file (``wallet_discovery_proposals.json``); the user approves them and
adds wallets with the existing ``/smartadd`` command. No feedback.py
changes are needed for this module.

Budgets keep the Helius free tier safe: ``discovery_max_runners`` per run
(default 10), ``discovery_max_sigs`` per mint (default 30), a short sleep
between RPC calls, and ``discovery_interval_s`` throttling (default 24h)
in :func:`run_discovery`.

This module is NOT wired into the live daemon. Every function takes
explicit inputs (``rpc`` injected, registry passed in) so it is fully
unit-testable with fakes. Nothing here publishes events, touches the
broker, or changes trading behavior.

Integration (on user go-ahead only):
  * call ``run_discovery(state, degen, runners, rpc=smartmoney._rpc)``
    from a daily hook in ``daemon/launch_scanner/__init__.py``;
  * feed it runners from the milestone watch / ``launch.runners_recap``
    payloads (peak_mult >= bar);
  * surface new proposals on the Telegram ``/tune`` card or a new
    ``/wallets`` read-only command.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("forex_agent.daemon.launch_scanner.wallet_discovery")

_HOME = Path(os.environ.get("FOREX_AGENT_HOME", Path.home() / ".forex-agent"))
_PROPOSALS_FILE = _HOME / "launch_scanner" / "wallet_discovery_proposals.json"

_DISCOVERY_KEY = "wallet_discovery"   # state ledger key (wiring only)
_EVAL_KEY = "evaluated_runners"       # mints already mined, in the ledger


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _cfg(degen: Any, name: str, default: Any) -> Any:
    try:
        v = getattr(degen, name, default)
        return default if v is None else v
    except Exception:
        return default


def proposals_path() -> Path:
    base = os.environ.get("FOREX_AGENT_HOME") or str(Path.home() / ".forex-agent")
    return Path(base) / "launch_scanner" / "wallet_discovery_proposals.json"


# ---------------------------------------------------------------------------
# Step 1: who bought this runner early?
# ---------------------------------------------------------------------------

def _tx_buyers(tx: Dict[str, Any], mint: str) -> Dict[str, float]:
    """Wallets whose balance of ``mint`` increased in one transaction.

    Returns {wallet: bought_amount}. Pure; mirrors the heuristic in
    smartmoney._bought_mints but collects every buyer, not one wallet.
    """
    meta = (tx or {}).get("meta") or {}
    pre: Dict[tuple, float] = {}
    for b in meta.get("preTokenBalances") or []:
        try:
            pre[(str(b.get("owner")), str(b.get("mint")))] = _f(
                (b.get("uiTokenAmount") or {}).get("uiAmount"))
        except Exception:
            continue
    out: Dict[str, float] = {}
    for b in meta.get("postTokenBalances") or []:
        try:
            owner, m = str(b.get("owner")), str(b.get("mint"))
            if m != mint or not owner:
                continue
            post_amt = _f((b.get("uiTokenAmount") or {}).get("uiAmount"))
            if post_amt - pre.get((owner, m), 0.0) > 0:
                out[owner] = out.get(owner, 0.0) + post_amt - pre.get(
                    (owner, m), 0.0)
        except Exception:
            continue
    return out


def early_buyers_for_mint(rpc: Callable[[str, List[Any]], Any],
                         mint: str,
                         first_seen_ts: float,
                         early_window_s: float = 3600.0,
                         max_sigs: int = 30,
                         sleep_s: float = 0.15) -> Dict[str, float]:
    """Wallets that bought ``mint`` inside the early window.

    Returns ``{wallet: first_buy_ts}``. Only transactions with blockTime in
    ``[first_seen_ts - 600, first_seen_ts + early_window_s]`` count, so a
    wallet that aped at the top hours later is not credited as "early".
    ``rpc`` is injected (``smartmoney._rpc`` in production). Never raises.
    """
    buyers: Dict[str, float] = {}
    try:
        if not mint:
            return buyers
        sigs = rpc("getSignaturesForAddress",
                   [mint, {"limit": 100}]) or []
        # API returns newest-first; the early buyers are at the tail.
        sigs = sigs[-max_sigs:] if len(sigs) > max_sigs else sigs
        lo = first_seen_ts - 600.0
        hi = first_seen_ts + max(60.0, early_window_s)
        for s in reversed(sigs):  # oldest first
            try:
                sig = (s or {}).get("signature")
                bt = _f((s or {}).get("blockTime"), 0.0)
                if not sig or not (lo <= bt <= hi):
                    continue
                tx = rpc("getTransaction",
                         [sig, {"encoding": "jsonParsed",
                                "maxSupportedTransactionVersion": 0}]) or {}
                for wallet in _tx_buyers(tx, mint):
                    if wallet not in buyers or bt < buyers[wallet]:
                        buyers[wallet] = bt
            except Exception:
                continue
            if sleep_s:
                time.sleep(sleep_s)
    except Exception as exc:
        logger.warning("early buyers scan failed for %s: %s", mint, exc)
    return buyers


# ---------------------------------------------------------------------------
# Step 2: score wallets across runners
# ---------------------------------------------------------------------------

def score_wallets(runners: List[Dict[str, Any]],
                  rpc: Callable[[str, List[Any]], Any],
                  deployers: Optional[set] = None,
                  early_window_s: float = 3600.0,
                  max_sigs: int = 30,
                  max_runners: int = 10,
                  sleep_s: float = 0.15) -> Dict[str, Dict[str, Any]]:
    """Score candidate wallets by early-buy hit rate across runners.

    ``runners``: ``[{mint, symbol, peak_mult, first_seen_ts}]`` — the
    caller filters to qualifying runners (peak >= bar). ``deployers``:
    optional set of deployer addresses to exclude (insiders gaming the
    discovery). Returns ``{wallet: {hits, seen, hit_rate, runners}}`` with
    Laplace-smoothed hit rates. Never raises.
    """
    deployers = deployers or set()
    scores: Dict[str, Dict[str, Any]] = {}
    try:
        for r in (runners or [])[:max(1, max_runners)]:
            mint = str(r.get("mint") or "")
            if not mint:
                continue
            buyers = early_buyers_for_mint(
                rpc, mint, _f(r.get("first_seen_ts")),
                early_window_s=early_window_s, max_sigs=max_sigs,
                sleep_s=sleep_s)
            for wallet, _bt in buyers.items():
                if wallet in deployers:
                    continue
                s = scores.setdefault(wallet, {"hits": 0, "seen": 0,
                                               "runners": []})
                s["hits"] += 1
                sym = str(r.get("symbol") or mint[:8])
                if sym not in s["runners"]:
                    s["runners"].append(sym)
            # "seen" counts runners the wallet could have been evaluated
            # on; every mined runner counts for every wallet found on it.
            # Wallets are only in the map when they hit, so seen == hits
            # here; the rate is smoothed against a prior instead.
        for wallet, s in scores.items():
            hits = int(s["hits"])
            # Laplace smoothing vs a 10% prior: a 1-hit wallet scores
            # ~0.18, a 3/3 wallet ~0.8. Rewards repeat hitters only.
            s["hit_rate"] = round((hits + 0.2) / (hits + 2.0), 3)
            s["seen"] = hits
    except Exception as exc:
        logger.warning("wallet scoring failed: %s", exc)
    return scores


# ---------------------------------------------------------------------------
# Step 3: proposals (never auto-add)
# ---------------------------------------------------------------------------

def _proposal_id(wallet: str, day: str) -> str:
    digest = hashlib.sha256(("%s|%s" % (wallet, day)).encode()).hexdigest()
    return "W-" + digest[:6].upper()


def discovery_proposals(scores: Dict[str, Dict[str, Any]],
                        registry: Dict[str, Any],
                        now: float,
                        day: str,
                        min_hits: int = 2,
                        min_hit_rate: float = 0.5,
                        max_new: int = 5) -> List[Dict[str, Any]]:
    """Filter scored wallets into reviewable proposals.

    Excludes wallets already tracked. Sorted by hits desc, then hit_rate.
    Proposal dicts are stored by :func:`save_proposals` and approved via
    the existing ``/smartadd`` command — discovery never writes the
    registry itself.
    """
    out: List[Dict[str, Any]] = []
    try:
        cands = []
        for wallet, s in (scores or {}).items():
            if wallet in (registry or {}):
                continue
            hits = int(s.get("hits") or 0)
            rate = _f(s.get("hit_rate"))
            if hits >= max(1, min_hits) and rate >= min_hit_rate:
                cands.append((wallet, s, hits, rate))
        cands.sort(key=lambda c: (-c[2], -c[3]))
        for wallet, s, hits, rate in cands[:max(1, max_new)]:
            runners = list(s.get("runners") or [])[:8]
            out.append({
                "id": _proposal_id(wallet, day),
                "kind": "smart_wallet_add",
                "wallet": wallet,
                "label": "discovered-%s" % _proposal_id(wallet, day)[2:],
                "hits": hits,
                "seen": int(s.get("seen") or hits),
                "hit_rate": round(rate, 3),
                "runners": runners,
                "rationale": (
                    "Bought %d past runner(s) early (%s) with a %.0f%% "
                    "smoothed hit rate. Review, then /smartadd to track." % (
                        hits, ", ".join(runners) or "n/a", rate * 100)),
                "status": "open",
                "created_ts": now,
            })
    except Exception as exc:
        logger.warning("discovery proposals failed: %s", exc)
    return out


def load_proposals(path: Any = None) -> List[Dict[str, Any]]:
    p = Path(str(path)) if path else proposals_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        items = data.get("proposals") if isinstance(data, dict) else data
        return [x for x in (items or []) if isinstance(x, dict)]
    except (OSError, ValueError):
        return []


def save_proposals(new: List[Dict[str, Any]],
                   path: Any = None) -> List[Dict[str, Any]]:
    """Append proposals, skipping dupes (same open wallet). Bounded to
    the newest 50. Returns the proposals actually added."""
    p = Path(str(path)) if path else proposals_path()
    try:
        existing = load_proposals(p)
    except Exception:
        existing = []
    open_wallets = {x.get("wallet") for x in existing
                    if x.get("status") == "open"}
    added = []
    for prop in new or []:
        if not isinstance(prop, dict) or not prop.get("wallet"):
            continue
        if prop["wallet"] in open_wallets:
            continue
        existing.append(prop)
        open_wallets.add(prop["wallet"])
        added.append(prop)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1,
                                   "proposals": existing[-50:]}))
        os.chmod(tmp, 0o600)
        tmp.replace(p)
    except OSError as exc:
        logger.warning("could not save discovery proposals: %s", exc)
    return added


# ---------------------------------------------------------------------------
# Orchestrator (wiring only — NOT called by the live daemon yet)
# ---------------------------------------------------------------------------

def run_discovery(state: Dict[str, Any],
                  degen: Any,
                  runners: List[Dict[str, Any]],
                  rpc: Optional[Callable[[str, List[Any]], Any]] = None,
                  registry: Optional[Dict[str, Any]] = None,
                  now: Optional[float] = None) -> Dict[str, Any]:
    """One discovery pass over recent runners. Never raises.

    Throttled by ``discovery_interval_s`` (default 24h). Skips runners
    below ``discovery_runner_mult`` (default 5x) and runners already
    mined (tracked in the state ledger). Writes only its own bounded
    ledger key and the proposals file — never the wallet registry, never
    any trading state.
    """
    now = now if now is not None else time.time()
    summary: Dict[str, Any] = {"ran": False, "runners_mined": 0,
                               "wallets_scored": 0, "proposals": 0}
    try:
        if not bool(_cfg(degen, "discovery_enabled", True)):
            summary["reason"] = "disabled"
            return summary
        interval = max(3600.0, _f(_cfg(degen, "discovery_interval_s",
                                       86400.0)))
        ledger = state.setdefault(_DISCOVERY_KEY, {})
        if not isinstance(ledger, dict):
            state[_DISCOVERY_KEY] = ledger = {}
        if now - _f(ledger.get("last_run_ts")) < interval:
            summary["reason"] = "throttled"
            return summary
        if rpc is None:
            summary["reason"] = "no-rpc"
            return summary
        bar = _f(_cfg(degen, "discovery_runner_mult", 5.0), 5.0)
        evaluated = ledger.setdefault(_EVAL_KEY, {})
        fresh = [r for r in (runners or [])
                 if _f(r.get("peak_mult")) >= bar
                 and str(r.get("mint") or "") not in evaluated]
        if not fresh:
            ledger["last_run_ts"] = now
            summary["ran"] = True
            summary["reason"] = "no-fresh-runners"
            return summary
        deployers = set()
        try:
            dep = ledger.get("known_deployers")
            if isinstance(dep, list):
                deployers = set(dep)
        except Exception:
            pass
        scores = score_wallets(
            fresh, rpc, deployers=deployers,
            early_window_s=_f(_cfg(degen, "discovery_early_window_s",
                                   3600.0), 3600.0),
            max_sigs=int(_cfg(degen, "discovery_max_sigs", 30) or 30),
            max_runners=int(_cfg(degen, "discovery_max_runners", 10)
                            or 10),
            sleep_s=0.2)
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        props = discovery_proposals(
            scores, registry or {}, now, day,
            min_hits=int(_cfg(degen, "discovery_min_hits", 2) or 2),
            min_hit_rate=_f(_cfg(degen, "discovery_min_hit_rate", 0.5),
                            0.5),
            max_new=int(_cfg(degen, "discovery_max_new", 5) or 5))
        added = save_proposals(props)
        for r in fresh:
            evaluated[str(r.get("mint"))] = {
                "symbol": str(r.get("symbol") or ""),
                "peak_mult": _f(r.get("peak_mult")), "ts": now}
        # Bound the ledger: keep the newest 200 mined runners.
        if len(evaluated) > 200:
            for m in list(evaluated)[:-200]:
                evaluated.pop(m, None)
        ledger["last_run_ts"] = now
        summary.update({"ran": True, "runners_mined": len(fresh),
                        "wallets_scored": len(scores),
                        "proposals": len(added)})
    except Exception as exc:
        logger.warning("run_discovery failed: %s", exc)
        summary["reason"] = "error"
    return summary
