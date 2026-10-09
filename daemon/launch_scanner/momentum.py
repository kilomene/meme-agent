"""Established-coin momentum scanner.

The launch hunter watches NEW mints in their first minutes. This module
watches ESTABLISHED coins that are running right now — the FWOG/NEET/
ANTFUN-shaped moves the launch pipeline never sees because those coins
are weeks old.

Source: GeckoTerminal trending_pools (Solana) — pools people are
actually trading this hour. A pool qualifies when:

  * 24h price change inside [min_24h_pct, max_24h_pct] — the upper cap
    is the anti-top-buying guard: a coin already up 500%+ is someone
    else's exit, not our entry;
  * 1h change not deeply negative — the run isn't already dumping;
  * 24h volume and liquidity above floors — real market, not a
    wash-traded puddle;
  * not a stablecoin / SOL / wrapped leg.

Qualified pools still pass the SAME safety gate as every dropped CA
(known report, no hard fails, score >= early_min_score). Momentum is a
different hunting ground, not a weaker bar.

Two lanes, one scan: pools with a KNOWN market cap inside
``(momentum_lowcap_min_mcap_usd, momentum_lowcap_max_mcap_usd]``
(default $50K–$10M) are tagged lane "lowcap"; bigger (or unknown-mcap)
pools are lane "established". A known market cap at or below the floor
is the dust zone — excluded entirely, no alert and no lane. Both lanes
share every gate above and the same safety bar — the lane only changes
how the card is labeled, never what gets through.

Alerts fire as ``momentum.alert`` events (own Telegram card) on first
qualification. Paper entry happens only when the runner STILL qualifies
on a later scan (two-stage, like the launch pipeline's multi-poll proof)
— never on a one-snapshot top. Entries ride the normal paper pipeline,
so exits are managed and the weekly review can attribute outcomes to
this mode via ``source: gecko-trending``.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

# Legs that are never a "runner" no matter what the tape says.
_STABLE_SYMBOLS = {"USDC", "USDT", "SOL", "WSOL", "WETH", "ETH", "WBTC", "DAI"}


def _f(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f == f else default  # NaN -> default


class MomentumScanner:
    """Stateless qualifier + cooldown tracker for trending pools."""

    def __init__(self) -> None:
        # pool address -> last alert epoch
        self._last_alert: Dict[str, float] = {}

    def _cfg(self, degen: Any, name: str, default: float) -> float:
        try:
            return float(getattr(degen, name, default))
        except (TypeError, ValueError):
            return default

    def qualify(self, degen: Any, pool: Dict[str, Any],
                now: Optional[float] = None,
                skip_cooldown: bool = False) -> Optional[Dict[str, Any]]:
        """Return a runner dict, or None with the pool held silently.

        ``skip_cooldown`` bypasses the per-pool alert cooldown. It is
        used ONLY by the stage-2 confirmation pass: the 6h cooldown
        suppresses duplicate Telegram CARDS, never confirmation
        evaluation. Without the bypass, a pool marked at stage 1 would
        be suppressed on every later scan and stage-2 paper entries
        could never happen.
        """
        now = now if now is not None else time.time()
        addr = str(pool.get("pool_address") or "")
        symbol = str(pool.get("symbol") or "").upper()
        if not addr or symbol in _STABLE_SYMBOLS or not pool.get("mint"):
            return None
        chg24 = _f(pool.get("chg_24h_pct"))
        chg1 = _f(pool.get("chg_1h_pct"))
        vol24 = _f(pool.get("vol_24h_usd"))
        liq = _f(pool.get("liq_usd"))
        min24 = self._cfg(degen, "momentum_min_gain_24h", 20.0)
        max24 = self._cfg(degen, "momentum_max_gain_24h", 300.0)
        if chg24 < min24:
            return None
        if chg24 > max24:
            return None  # anti-top-buying: already mooned, skip
        max_dd = self._cfg(degen, "momentum_max_drawdown_1h", 15.0)
        if chg1 < -abs(max_dd):
            return None  # run already dumping
        if vol24 < self._cfg(degen, "momentum_min_vol_24h_usd", 100000.0):
            return None
        if liq < self._cfg(degen, "momentum_min_liq_usd", 50000.0):
            return None
        cooldown = self._cfg(degen, "momentum_cooldown_hours", 6.0) * 3600
        last = self._last_alert.get(addr)
        # The cooldown gates duplicate ALERT cards only. Confirmation
        # evaluation bypasses it via skip_cooldown (see docstring).
        if not skip_cooldown and last is not None and now - last < cooldown:
            return None
        mcap = _f(pool.get("usd_market_cap"))
        floor = self._cfg(degen, "momentum_lowcap_min_mcap_usd", 50000.0)
        ceiling = self._cfg(degen, "momentum_lowcap_max_mcap_usd",
                            10000000.0)
        # Two lanes, one scan: "lowcap" for known caps inside
        # (floor, ceiling], "established" for everything bigger (or
        # unknown mcap, which keeps the pre-lane behavior). A KNOWN cap
        # at/below the floor is the dust zone — excluded entirely, no
        # alert, no lane. Both lanes share every other gate and the
        # same safety bar.
        if 0 < mcap <= floor:
            return None
        lane = "lowcap" if 0 < mcap <= ceiling else "established"
        return {
            "pool_address": addr,
            "mint": pool.get("mint"),
            "symbol": pool.get("symbol") or "",
            "name": pool.get("name") or "",
            "chain": pool.get("chain") or "solana",
            "source": "gecko-trending",
            "lane": lane,
            "url": pool.get("url") or "",
            "chg_24h_pct": round(chg24, 2),
            "chg_1h_pct": round(chg1, 2),
            "vol_24h_usd": vol24,
            "liq_usd": liq,
            "usd_market_cap": mcap,
        }

    def scan(self, degen: Any, pools: List[Dict[str, Any]],
             now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Qualify a batch of trending pools. Never raises."""
        now = now if now is not None else time.time()
        out = []
        for pool in pools:
            try:
                r = self.qualify(degen, pool, now)
            except Exception:
                continue
            if r:
                out.append(r)
        return out

    def mark_alerted(self, pool_address: str,
                     now: Optional[float] = None) -> None:
        self._last_alert[str(pool_address)] = (
            now if now is not None else time.time())
        # prune entries older than 48h so the map can't grow forever
        cutoff = (now if now is not None else time.time()) - 48 * 3600
        for k in [k for k, v in self._last_alert.items() if v < cutoff]:
            del self._last_alert[k]
