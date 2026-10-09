"""Deployer tracking: which wallet launched which coin, and whether that
wallet's launches tend to run.

A "deployer" is the creator wallet behind a launch: for pump.fun mints it
is the launch transaction's creator; for direct pool launches
(pumpswap/meteora) it is the pool creator. Hot deployers — wallets with a
history of launches that ran — get their NEW coins fast-tracked to the
same instant-card path as the smart-buy fast-track.

Hard rule, stated once: HISTORY NEVER OVERRIDES HARD FAILS. The safety
gate (``_instant_verdict``, fail-closed, trial bar) runs FIRST regardless
of deployer heat — a hot deployer's honeypot is still blocked silently.
``evaluate_deployer_fasttrack`` takes ``safety_passed`` and returns None
when it is False, so the ordering is enforced in code, not just in docs.

Performance contract for the integrator (read this before wiring):

* Resolution is LAZY and CACHED. Never call ``resolve_deployer`` on the
  hot detection path. The intended call sites are:
  - quality-passed coins only (after ``_instant_verdict`` allowed them),
    resolved inline in the slow part of the cycle; or
  - a throttled background pass (one mint per cycle, e.g. the newest
    quality-passed coin without a cached deployer).
* Use ``cached_deployer(state, ...)`` — it reads
  ``state["deployer_cache"]`` first (mint -> {"deployer", "ts"}) and only
  resolves when the entry is missing or older than
  ``degen.deployer_resolve_ttl_s`` (default 6h). Negative results (None)
  are cached too, with the same TTL, so one bad mint never triggers
  repeated RPC calls.
* pump.fun mints resolve for FREE: ``sources.py`` already ships the
  creator in ``extra["creator"]`` — ``resolve_deployer`` reads it from
  ``ctx`` (the normalized launch source dict) and makes NO RPC call.
  Helius ``getSignaturesForAddress``/``getTransaction`` is only used for
  direct pool launches where no creator field exists.

Registry (shared file, 0600 — same convention as smartmoney.py)::

    ~/.forex-agent/launch_scanner/deployer_history.json

per deployer: ``{launches_seen, hits_2x, rugs, dead, first_seen, last_seen}``

Outcomes are graded independently in this module (``record_outcome``);
a shared grading helper covering wallets AND deployers would be cleaner
for a later cleanup — noted for the integrator (see INTEGRATION below).

Knobs (integrator adds to config/defaults.yaml, getattr-with-default):

* ``deployer_tracking_enabled`` (default True)
* ``deployer_hot_hit_rate`` (default 0.30)
* ``deployer_min_launches`` (default 3)
* ``deployer_resolve_ttl_s`` (default 21600 = 6h)
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from . import helius_ratelimit

logger = logging.getLogger("forex_agent.daemon.launch_scanner.deployers")

_HOME = Path(os.environ.get("FOREX_AGENT_HOME", Path.home() / ".forex-agent"))
_REGISTRY_FILE = _HOME / "launch_scanner" / "deployer_history.json"
_CACHE_KEY = "deployer_cache"
_SEEN_KEY = "deployer_fasttrack_seen"
_MAX_REGISTRY = 2000
_TIMEOUT = 20

_RPC_URL = "https://mainnet.helius-rpc.com/?api-key={key}"
_KEY_FILE = _HOME / "helius" / "api_key"
_key_cache: Optional[str] = None
_key_checked = False

# Outcomes accepted by record_outcome.
OUTCOMES = ("hit_2x", "rug", "dead")


# ----------------------------------------------------------------------
# Helius RPC (reused pattern from smartmoney.py; Solana only)
# ----------------------------------------------------------------------

def api_key() -> Optional[str]:
    """Helius key from the 0600 file or env. Cached; None when absent."""
    global _key_cache, _key_checked
    if _key_checked:
        return _key_cache
    _key_checked = True
    key = os.environ.get("HELIUS_API_KEY", "").strip()
    if not key:
        try:
            key = _KEY_FILE.read_text().strip()
        except OSError:
            key = ""
    _key_cache = key or None
    return _key_cache


def _rpc(method: str, params: List[Any]) -> Any:
    if not api_key():
        return None
    if helius_ratelimit.cooldown_remaining() > 0:
        return None  # shared Helius cooldown: fail-soft like any miss
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method,
         "params": params}).encode()
    req = urllib.request.Request(
        _RPC_URL.format(key=api_key()), data=payload,
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0",
                 "Connection": "close"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            result = json.loads(
                resp.read().decode("utf-8", "replace")).get("result")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            wait = helius_ratelimit.report_ratelimited()
            logger.warning("deployer rpc %s rate-limited (429); shared "
                           "cooldown %.0fs", method, wait)
        else:
            logger.warning("deployer rpc %s failed: HTTP %s",
                           method, exc.code)
        return None
    except Exception as exc:
        logger.warning("deployer rpc %s failed: %s", method, exc)
        return None
    helius_ratelimit.report_success()
    return result


# ----------------------------------------------------------------------
# Deployer resolution
# ----------------------------------------------------------------------

def _first_signer(tx: Dict[str, Any]) -> Optional[str]:
    """First signer of a transaction — the wallet that paid for (and in
    practice created) it. Works for pump.fun create txs and pool-create
    txs alike; the deployer always signs the launch transaction."""
    try:
        msg = (tx.get("transaction") or {}).get("message") or {}
        keys = msg.get("accountKeys") or []
        first = keys[0] if keys else None
        pub = (first or {}).get("pubkey") or first
        return str(pub) if pub else None
    except Exception:
        return None


def resolve_deployer(mint: str, chain: str,
                     ctx: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    """Resolve the deployer wallet for one mint.

    ``ctx`` is the normalized launch source dict (as produced by
    ``sources.py`` / the launch candidate) — pump.fun mints carry
    ``extra.creator`` and resolve with ZERO RPC calls. ``chain`` should be
    the canonical chain name (solana/base/ethereum).

    Non-solana chains and mints without any resolvable source return None.
    Never raises.
    """
    if not mint:
        return None
    extra = (ctx or {}).get("extra") or {}
    creator = str(extra.get("creator") or "").strip()
    if creator:
        # pump.fun launch — creator shipped free by the source feed.
        return creator
    if (chain or "").lower() != "solana":
        return None
    # Direct pool launch: find the mint's earliest transaction; its first
    # signer is the deployer. getSignaturesForAddress is newest-first; the
    # last entry is the oldest we can see.
    try:
        sigs = _rpc("getSignaturesForAddress", [mint, {"limit": 100}]) or []
        if not sigs:
            return None
        if len(sigs) >= 100:
            # Too much history to reach the launch reliably — bounded.
            return None
        oldest = (sigs[-1] or {}).get("signature")
        if not oldest:
            return None
        tx = _rpc("getTransaction",
                  [oldest, {"encoding": "jsonParsed",
                            "maxSupportedTransactionVersion": 0}]) or {}
        return _first_signer(tx)
    except Exception as exc:
        logger.warning("resolve_deployer failed for %s: %s", mint, exc)
        return None


def cached_deployer(state: Dict[str, Any], mint: str, chain: str,
                    ctx: Optional[Mapping[str, Any]] = None,
                    degen: Any = None) -> Optional[str]:
    """Lazy, cached deployer resolution. Reads ``state["deployer_cache"]``
    first; resolves only when missing or older than
    ``degen.deployer_resolve_ttl_s`` (default 6h). Negative results are
    cached too.

    The INTEGRATOR must only call this off the hot path: for
    quality-passed coins (after ``_instant_verdict`` allowed them) or in a
    throttled background pass (one mint per cycle). Never call this in the
    per-candidate scan loop.
    """
    ttl = float(getattr(degen, "deployer_resolve_ttl_s", 21600.0))
    cache = state.setdefault(_CACHE_KEY, {})
    now = time.time()
    entry = cache.get(mint)
    if isinstance(entry, dict) and now - float(entry.get("ts") or 0) < ttl:
        return entry.get("deployer")
    deployer = resolve_deployer(mint, chain, ctx)
    # Bound the cache: evict the oldest entries past 2000.
    if len(cache) > 2000:
        for key in sorted(cache, key=lambda k: float(
                (cache.get(k) or {}).get("ts") or 0))[
                :len(cache) - 2000]:
            cache.pop(key, None)
    cache[mint] = {"deployer": deployer, "ts": now}
    return deployer


# ----------------------------------------------------------------------
# Registry (shared JSON file, 0600 — same convention as smartmoney.py)
# ----------------------------------------------------------------------

def load_registry() -> Dict[str, Dict[str, Any]]:
    """Deployer history from the shared JSON file (both daemons read it)."""
    try:
        data = json.loads(_REGISTRY_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_registry(hist: Dict[str, Dict[str, Any]]) -> bool:
    try:
        _REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _REGISTRY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(hist))
        os.chmod(tmp, 0o600)
        tmp.replace(_REGISTRY_FILE)
        return True
    except OSError as exc:
        logger.warning("could not save deployer registry: %s", exc)
        return False


def _new_entry(now: float) -> Dict[str, Any]:
    return {"launches_seen": 0, "hits_2x": 0, "rugs": 0, "dead": 0,
            "first_seen": now, "last_seen": now}


def record_launch(deployer: str) -> Optional[Dict[str, Any]]:
    """Count one launch for a deployer. Returns the updated entry."""
    deployer = (deployer or "").strip()
    if not deployer:
        return None
    now = time.time()
    hist = load_registry()
    entry = hist.get(deployer) or _new_entry(now)
    entry["launches_seen"] = int(entry.get("launches_seen") or 0) + 1
    entry["last_seen"] = now
    hist[deployer] = entry
    if len(hist) > _MAX_REGISTRY:
        # Evict least-recently-seen, never hot deployers.
        for dep in sorted(
                hist,
                key=lambda d: float((hist.get(d) or {}).get("last_seen")
                                    or 0))[:len(hist) - _MAX_REGISTRY]:
            if not _entry_is_hot(hist.get(dep), 3, 0.30):
                hist.pop(dep, None)
                break
    save_registry(hist)
    return entry


def record_outcome(mint: str, deployer: str, outcome: str) -> bool:
    """Grade one closed launch: outcome in {"hit_2x","rug","dead"}.

    ``mint`` is recorded for audit only; grading keys off the deployer.
    Deployers seen here but never launched through ``record_launch`` are
    created with launches_seen=1 (first-seen assumption) so hit rates
    stay <= 1. Returns False on an unknown outcome (never raises).
    """
    if outcome not in OUTCOMES:
        logger.warning("record_outcome: unknown outcome %r for %s", outcome,
                       mint)
        return False
    deployer = (deployer or "").strip()
    if not deployer:
        return False
    now = time.time()
    hist = load_registry()
    entry = hist.get(deployer)
    if entry is None:
        entry = _new_entry(now)
        entry["launches_seen"] = 1
    else:
        entry["launches_seen"] = max(int(entry.get("launches_seen") or 0), 1)
    if outcome == "hit_2x":
        entry["hits_2x"] = int(entry.get("hits_2x") or 0) + 1
    elif outcome == "rug":
        entry["rugs"] = int(entry.get("rugs") or 0) + 1
    else:
        entry["dead"] = int(entry.get("dead") or 0) + 1
    entry["last_seen"] = now
    hist[deployer] = entry
    return save_registry(hist)


# ----------------------------------------------------------------------
# Hot-deployer rule + fast-track trigger
# ----------------------------------------------------------------------

def hit_rate(entry: Optional[Dict[str, Any]]) -> float:
    """2x-hit rate of a registry entry (0.0 when ungraded/too few)."""
    if not entry:
        return 0.0
    launches = int(entry.get("launches_seen") or 0)
    if launches <= 0:
        return 0.0
    return float(entry.get("hits_2x") or 0) / launches


def _entry_is_hot(entry: Optional[Dict[str, Any]], min_launches: int,
                  hot_hit_rate: float) -> bool:
    if not entry:
        return False  # unknown deployer: NEUTRAL, never hot
    if int(entry.get("launches_seen") or 0) < min_launches:
        return False  # cold deployer: NEUTRAL, never hot
    return hit_rate(entry) >= hot_hit_rate


def is_hot(deployer: Optional[str], degen: Any) -> bool:
    """True only for proven deployers: launches_seen >=
    ``degen.deployer_min_launches`` (default 3) AND hit rate >=
    ``degen.deployer_hot_hit_rate`` (default 0.30).

    Unknown, first-time, and cold (1-2 launch) deployers are NEUTRAL —
    no boost, no penalty, ever.
    """
    if not deployer:
        return False
    min_launches = max(1, int(getattr(degen, "deployer_min_launches", 3)))
    hot_hit_rate = float(getattr(degen, "deployer_hot_hit_rate", 0.30))
    return _entry_is_hot(load_registry().get(deployer), min_launches,
                         hot_hit_rate)


def evaluate_deployer_fasttrack(state: Dict[str, Any], degen: Any, mint: str,
                                deployer: Optional[str],
                                safety_passed: bool = True
                                ) -> Optional[Dict[str, Any]]:
    """Decide whether a coin gets the hot-deployer fast-track.

    ``safety_passed`` is the result of ``_instant_verdict`` — the
    integrator MUST pass it, and the gate MUST have run first. History
    never overrides hard fails: a False here (honeypot, can't-sell,
    insider cluster, score below bar) returns None, no fast-track.

    Hot deployer + new-to-us mint -> dict with the facts the card needs::

        {"deployer": ..., "launches_seen": n, "hits_2x": h,
         "hit_rate": r, "deployer_line": "🏭 deployer 4/10 hit 2x"}

    Unknown/cold deployers, disabled tracking, and already-carded mints
    return None (NEUTRAL — no boost, no penalty). Never raises.
    """
    try:
        if not getattr(degen, "deployer_tracking_enabled", True):
            return None
        if not safety_passed:
            # Hard-fail override blocked: the safety gate's verdict wins.
            return None
        if not deployer or not mint:
            return None
        seen = state.setdefault(_SEEN_KEY, [])
        if mint in seen:
            return None  # new-to-us mints only
        if not is_hot(deployer, degen):
            return None
        entry = load_registry().get(deployer) or {}
        launches = int(entry.get("launches_seen") or 0)
        hits = int(entry.get("hits_2x") or 0)
        rate = hit_rate(entry)
        seen.append(mint)
        if len(seen) > 2000:
            del seen[:len(seen) - 2000]
        return {
            "deployer": deployer,
            "launches_seen": launches,
            "hits_2x": hits,
            "hit_rate": round(rate, 3),
            "deployer_line": "🏭 deployer %d/%d hit 2x" % (hits, launches),
        }
    except Exception as exc:
        logger.warning("deployer fast-track eval failed: %s", exc)
        return None


def hot_deployers(degen: Any, limit: int = 12) -> List[Dict[str, Any]]:
    """Top deployers by hit rate, honoring the min-launches filter."""
    min_launches = max(1, int(getattr(degen, "deployer_min_launches", 3)))
    rows = []
    for dep, entry in load_registry().items():
        if int(entry.get("launches_seen") or 0) < min_launches:
            continue
        rows.append({"deployer": dep,
                     "launches_seen": int(entry.get("launches_seen") or 0),
                     "hits_2x": int(entry.get("hits_2x") or 0),
                     "rugs": int(entry.get("rugs") or 0),
                     "dead": int(entry.get("dead") or 0),
                     "hit_rate": round(hit_rate(entry), 3),
                     "last_seen": float(entry.get("last_seen") or 0)})
    rows.sort(key=lambda r: (r["hit_rate"], r["launches_seen"]),
              reverse=True)
    return rows[:limit]
