"""Proof-of-demand checks (measurement only, user decision 2026-10-02).

The manufactured-runner failure mode: a small group paints the chart
(rising polls, volume, buys), the bot "confirms" the runner, then they
dump 99% into the momentum buyers. 2026-10-02: the Top Picks ranker
scored 5 coins highest on winner-likeness and ALL 5 rugged the same
day. Ranking "winner-likeness" cannot fix this — only refusing to buy
the trap can.

Three checks that target the trap directly:
  1. LP lock/burn verification — is the liquidity actually locked, or
     can it be pulled?
  2. Holder concentration — do a few wallets own the supply? One dump
     kills the trade.
  3. Distribution pressure — are insiders / sellers already selling
     into the run while the chart still looks good?

MEASUREMENT ONLY. measure() runs the checks for every scored confirmed
runner and records pass/fail/unknown per check into state["proof_checks"]
(bounded, TTL-pruned). NOTHING gates on the results: entries, alerts,
caps, and picks are untouched. After ~1 week the ledger answers the
two questions that decide whether any check becomes a real gate:
  - how many of the bot's past winners would have PASSED, and
  - how many rugs/dumps would have been BLOCKED.
The user decides; nothing self-activates.

Verdicts are three-valued on purpose: "unknown" (no data — e.g.
RugCheck often carries no LP-lock info) is recorded distinctly from
pass/fail, because the fail-open vs fail-closed design choice for a
future gate depends on exactly how often "unknown" happens.

Never raises. Cheap: per-mint TTL cache (default 10 min) so a coin
scored once costs one RugCheck + one DexScreener call.
"""

import logging
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("proof")

STATE_KEY = "proof_checks"   # mint -> {ts, lp, holders, distribution}
_MAX_ENTRIES = 500
_LEDGER_TTL_S = 7 * 24 * 3600

_CACHE: Dict[str, Tuple[float, Any]] = {}  # key -> (ts, payload)
_CACHE_TTL_S = 600.0

_RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
_DEX_URL = "https://api.dexscreener.com/tokens/v1/{chain}/{mint}"

_PASS = "pass"
_FAIL = "fail"
_UNKNOWN = "unknown"


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN -> default


def _knob(degen: Any, name: str, default: Any) -> Any:
    try:
        v = getattr(degen, name, default)
        return default if v is None else v
    except Exception:
        return default


def _cached(key: str, fetch, ttl: float) -> Any:
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        payload = fetch()
    except Exception:
        return hit[1] if hit else None
    _CACHE[key] = (now, payload)
    if len(_CACHE) > 2000:
        # drop oldest quarter; keeps the cache bounded on long uptimes
        for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:500]:
            _CACHE.pop(k, None)
    return payload


def _rugcheck_report(mint: str, timeout: float) -> Optional[Dict[str, Any]]:
    from . import safety  # noqa: PLC0415 (lazy: package init order)

    def _fetch():
        return safety._get_json(_RUGCHECK_URL.format(mint=mint), timeout)

    data = _cached("rug:" + mint, _fetch, _CACHE_TTL_S)
    return data if isinstance(data, dict) else None


def _dex_pair(mint: str, chain: str, timeout: float
              ) -> Optional[Dict[str, Any]]:
    """Raw highest-liquidity DexScreener pair for h1 txn distribution."""
    from . import safety  # noqa: PLC0415

    def _fetch():
        return safety._get_json(
            _DEX_URL.format(chain=chain or "solana", mint=mint), timeout)

    data = _cached("dex:" + mint, _fetch, _CACHE_TTL_S)
    if not isinstance(data, list) or not data:
        return None
    best = None
    best_liq = -1.0
    for p in data:
        if not isinstance(p, dict):
            continue
        liq = _f((p.get("liquidity") or {}).get("usd"))
        if liq > best_liq:
            best_liq = liq
            best = p
    return best


def check_lp(report: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """LP lock/burn verification from a RugCheck report.

    pass    — a locker entry exists, or a market reports lpLocked true.
    fail    — a market explicitly reports lpLocked false (rare).
    unknown — RugCheck carries no lock info (common: e.g. pump AMM
              coins). Recorded distinctly; NOT treated as pass.
    """
    try:
        if not isinstance(report, dict):
            return {"verdict": _UNKNOWN, "detail": "no report"}
        lockers = report.get("lockers") or {}
        if isinstance(lockers, dict) and lockers:
            return {"verdict": _PASS,
                    "detail": "LP locked (%d locker%s)" % (
                        len(lockers), "" if len(lockers) == 1 else "s")}
        if isinstance(lockers, list) and lockers:
            return {"verdict": _PASS,
                    "detail": "LP locked (%d locker%s)" % (
                        len(lockers), "" if len(lockers) == 1 else "s")}
        saw_explicit_unlocked = False
        for m in report.get("markets") or []:
            if not isinstance(m, dict):
                continue
            locked = m.get("lpLocked")
            if locked is True or str(locked).lower() in ("1", "true"):
                return {"verdict": _PASS,
                        "detail": "LP locked (%s)" % str(
                            m.get("marketType") or "pool")[:24]}
            if locked is False or str(locked).lower() in ("0", "false"):
                saw_explicit_unlocked = True
        if saw_explicit_unlocked:
            return {"verdict": _FAIL, "detail": "LP explicitly unlocked"}
        return {"verdict": _UNKNOWN,
                "detail": "no LP lock data in report"}
    except Exception:
        logger.exception("proof check_lp failed")
        return {"verdict": _UNKNOWN, "detail": "check error"}


def check_holders(report: Optional[Dict[str, Any]],
                  top10_max_pct: float = 40.0,
                  single_max_pct: float = 20.0) -> Dict[str, str]:
    """Holder concentration from RugCheck topHolders.

    fail — top-10 wallets own more than top10_max_pct of supply, or the
           single biggest wallet owns more than single_max_pct.
    pass — holder data present and within limits.
    unknown — no holder data.
    """
    try:
        holders = (report or {}).get("topHolders") or []
        holders = [h for h in holders if isinstance(h, dict)]
        if not holders:
            return {"verdict": _UNKNOWN, "detail": "no holder data"}
        pcts = sorted((_f(h.get("pct")) for h in holders), reverse=True)
        top1 = pcts[0] if pcts else 0.0
        top10 = sum(pcts[:10])
        if top1 > single_max_pct:
            return {"verdict": _FAIL,
                    "detail": "top wallet owns %.1f%% (limit %.0f%%)" % (
                        top1, single_max_pct)}
        if top10 > top10_max_pct:
            return {"verdict": _FAIL,
                    "detail": "top 10 own %.1f%% (limit %.0f%%)" % (
                        top10, top10_max_pct)}
        return {"verdict": _PASS,
                "detail": "top10 %.1f%%, top1 %.1f%%" % (top10, top1)}
    except Exception:
        logger.exception("proof check_holders failed")
        return {"verdict": _UNKNOWN, "detail": "check error"}


def check_distribution(mint: str, chain: str,
                       insider_pct: float = 0.0,
                       insider_max_pct: float = 20.0,
                       sell_ratio_max: float = 2.0,
                       min_h1_txns: int = 20,
                       timeout: float = 8.0) -> Dict[str, str]:
    """Distribution pressure: is someone selling into the run?

    Two independent tripwires (either one fails the check):
      - DexScreener h1 sells >= sell_ratio_max * h1 buys (with a minimum
        txn floor so thin books don't false-positive), and
      - RugCheck insider-network combined supply share above
        insider_max_pct.
    pass/unknown follow the same data-availability rule as the others.
    """
    try:
        if insider_pct > insider_max_pct > 0:
            return {"verdict": _FAIL,
                    "detail": "insider cluster %.0f%% of supply" % insider_pct}
        pair = _dex_pair(mint, chain, timeout)
        txns = ((pair or {}).get("txns") or {}).get("h1") or {}
        buys = int(txns.get("buys") or 0)
        sells = int(txns.get("sells") or 0)
        if buys + sells < min_h1_txns:
            return {"verdict": _UNKNOWN,
                    "detail": "only %d h1 txns" % (buys + sells)}
        if buys <= 0:
            return {"verdict": _FAIL,
                    "detail": "%d h1 sells, no buys" % sells}
        ratio = sells / float(buys)
        if ratio >= sell_ratio_max:
            return {"verdict": _FAIL,
                    "detail": "h1 sells %.1fx buys (%d/%d)" % (
                        ratio, sells, buys)}
        return {"verdict": _PASS,
                "detail": "h1 sells %.1fx buys" % ratio}
    except Exception:
        logger.exception("proof check_distribution failed")
        return {"verdict": _UNKNOWN, "detail": "check error"}


def _insider_pct(report: Optional[Dict[str, Any]]) -> float:
    """Combined insider-network supply share (same parse as safety)."""
    try:
        from . import safety  # noqa: PLC0415
        return float(safety._insider_cluster_pct(
            (report or {}).get("insiderNetworks")))
    except Exception:
        return 0.0


def measure(state: Dict[str, Any], degen: Any, mint: str, chain: str,
            now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Run the 3 proof-of-demand checks for one scored runner.

    Measurement only: records {ts, lp, holders, distribution} into
    state["proof_checks"][mint] (bounded 500, 7-day TTL). Returns the
    record, or None when disabled / no mint. Never raises, never gates.
    """
    try:
        if not mint or not _knob(degen, "proof_enabled", True):
            return None
        now = now if now is not None else time.time()
        timeout = float(_knob(degen, "proof_timeout_s", 8.0) or 8.0)
        chain_l = (chain or "solana").lower()

        report = None
        if chain_l == "solana":
            report = _rugcheck_report(str(mint), timeout)

        if chain_l == "solana":
            lp = check_lp(report)
            holders = check_holders(
                report,
                top10_max_pct=float(
                    _knob(degen, "proof_holder_top10_max_pct", 40.0) or 40.0),
                single_max_pct=float(
                    _knob(degen, "proof_holder_single_max_pct", 20.0) or 20.0),
            )
        else:
            # EVM: RugCheck holder/LP shapes don't apply — record
            # unknown honestly rather than forcing a Solana parse.
            lp = {"verdict": _UNKNOWN, "detail": "solana-only check"}
            holders = {"verdict": _UNKNOWN, "detail": "solana-only check"}

        distribution = check_distribution(
            str(mint), chain_l,
            insider_pct=_insider_pct(report),
            insider_max_pct=float(
                _knob(degen, "proof_insider_max_pct", 20.0) or 20.0),
            sell_ratio_max=float(
                _knob(degen, "proof_dump_sell_ratio", 2.0) or 2.0),
            timeout=timeout,
        )

        rec = {"ts": now, "mint": str(mint), "chain": chain_l,
               "lp": lp, "holders": holders, "distribution": distribution}
        ledger = state.get(STATE_KEY)
        if not isinstance(ledger, dict):
            ledger = {}
            state[STATE_KEY] = ledger
        ledger[str(mint)] = rec
        # Prune stale, then bound by recency.
        cutoff = now - _LEDGER_TTL_S
        for m in [m for m, r in ledger.items()
                  if not isinstance(r, dict) or _f(r.get("ts")) < cutoff]:
            ledger.pop(m, None)
        if len(ledger) > _MAX_ENTRIES:
            for m in sorted(ledger, key=lambda k: _f(ledger[k].get("ts"))
                            )[:len(ledger) - _MAX_ENTRIES]:
                ledger.pop(m, None)
        return rec
    except Exception:
        logger.exception("proof measure failed for %s", mint)
        return None


def summary(rec: Optional[Dict[str, Any]]) -> str:
    """One-line verdict summary, e.g. 'LP:? HOLDERS:PASS DIST:FAIL'."""
    try:
        if not isinstance(rec, dict):
            return "no proof data"
        short = {_PASS: "PASS", _FAIL: "FAIL", _UNKNOWN: "?"}
        return "LP:%s HOLDERS:%s DIST:%s" % (
            short.get((rec.get("lp") or {}).get("verdict"), "?"),
            short.get((rec.get("holders") or {}).get("verdict"), "?"),
            short.get((rec.get("distribution") or {}).get("verdict"), "?"),
        )
    except Exception:
        return "no proof data"
