"""Strict runner confirmation engine.

Two-stage pipeline:

  1. CANDIDATE — loose detection (fresh launch, velocity spike,
     graduation, DexScreener boost). No alert, no trade. Just tracked.
  2. CONFIRMED — only when sustained, multi-poll evidence proves real
     demand. Everything else is dropped silently.

Confirmation requires ALL of:
  * momentum   — mcap up >= gain_mult from first sighting AND price
                 rising in >= rising_polls CONSECUTIVE polls
                 (sustained climb, not a one-poll wick)
  * volume     — 5-minute volume >= min_m5_volume_usd (real money moving)
  * liquidity  — >= min_liquidity_usd (tradable, harder to rug)
  * buy pressure — 5m buys >= min_buys_m5 and buys/sells >= min ratio
                 (demand, not distribution)

Rejection (silent drop):
  * mcap dumped > 60% from peak sighting (rugged / died)
  * no DexScreener pair after max_no_pair_polls (can't analyze it)
  * confirm_window_minutes passes without confirming (went nowhere)

Pure logic — no I/O, fully unit-testable. The daemon feeds it
DexScreener pair stats each cycle.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

CONFIRM = "confirm"
REJECT = "reject"
WAIT = "wait"


def new_candidate(key: str, mint: Optional[str], symbol: str, name: str,
                  chain: str, source: str, tier: str, url: str,
                  now: float) -> Dict[str, Any]:
    return {
        "key": key,
        "mint": mint,
        "symbol": symbol,
        "name": name,
        "chain": chain,
        "source": source,
        "tier": tier,
        "url": url,
        "first_ts": now,
        "first_mcap": 0.0,
        "peak_mcap": 0.0,
        "last_price": 0.0,
        "rising_streak": 0,
        "no_pair_polls": 0,
        "last_mcap": 0.0,
        "last_liq": 0.0,
        "last_vol_m5": 0.0,
    }


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class RunnerJudge:
    """Applies the confirmation rules to one candidate per cycle."""

    def __init__(self, cfg):
        self.gain_mult = float(getattr(cfg, "confirm_gain_mult", 1.8))
        self.rising_polls = int(getattr(cfg, "confirm_rising_polls", 3))
        self.min_vol_m5 = float(getattr(cfg, "confirm_min_m5_volume_usd", 3000))
        self.min_liq = float(getattr(cfg, "confirm_min_liquidity_usd", 12000))
        self.min_buys = int(getattr(cfg, "confirm_min_buys_m5", 5))
        self.buy_sell_ratio = float(getattr(cfg, "confirm_min_buy_sell_ratio", 1.5))
        self.window_s = float(getattr(cfg, "confirm_window_minutes", 30)) * 60
        self.max_no_pair = 10

    def update(self, cand: Dict[str, Any],
               stats: Optional[Dict[str, Any]], now: float) -> str:
        """Feed fresh DexScreener pair stats (or None). Returns
        CONFIRM / REJECT / WAIT."""
        if not stats:
            cand["no_pair_polls"] = int(cand.get("no_pair_polls", 0)) + 1
            if cand["no_pair_polls"] >= self.max_no_pair:
                return REJECT
            return self._age_check(cand, now)

        mcap = _f(stats.get("usd_market_cap"))
        price = _f(stats.get("price_usd"))
        liq = _f(stats.get("usd_liquidity"))
        vol_m5 = _f(stats.get("vol_m5"))
        buys = int(stats.get("buys_m5") or 0)
        sells = int(stats.get("sells_m5") or 0)

        if mcap <= 0 or price <= 0:
            return self._age_check(cand, now)

        if not cand.get("first_mcap"):
            cand["first_mcap"] = mcap
            cand["peak_mcap"] = mcap
            cand["last_price"] = price
            return WAIT

        # Peak tracking + death check.
        if mcap > _f(cand.get("peak_mcap")):
            cand["peak_mcap"] = mcap
        if mcap < _f(cand.get("peak_mcap")) * 0.4:
            return REJECT  # rugged / died — drop silently

        # Cadence guard: the daemon throttles pair-stats refreshes (~30s)
        # while the judge runs every cycle. A cached/duplicate observation
        # (identical price AND mcap) must be neutral — it must NOT reset
        # the rising streak, or real runners can never confirm.
        prev_price = _f(cand.get("last_price"))
        if (prev_price > 0 and price == prev_price
                and mcap == _f(cand.get("last_mcap"))):
            return self._age_check(cand, now)

        # Rising streak: strictly higher price than last poll (+1% to
        # ignore noise). Any genuinely flat/down poll resets the streak.
        if prev_price > 0 and price > prev_price * 1.01:
            cand["rising_streak"] = int(cand.get("rising_streak", 0)) + 1
        elif prev_price > 0:
            cand["rising_streak"] = 0
        # (first real observation after seeding just sets the baseline)
        cand["last_price"] = price
        cand["last_mcap"] = mcap
        cand["last_liq"] = liq
        cand["last_vol_m5"] = vol_m5

        gain = mcap / _f(cand.get("first_mcap"))
        buy_ok = buys >= self.min_buys and (
            sells == 0 or buys / max(sells, 1) >= self.buy_sell_ratio)

        if (gain >= self.gain_mult
                and int(cand.get("rising_streak", 0)) >= self.rising_polls
                and vol_m5 >= self.min_vol_m5
                and liq >= self.min_liq
                and buy_ok):
            return CONFIRM
        return self._age_check(cand, now)

    def _age_check(self, cand: Dict[str, Any], now: float) -> str:
        if now - _f(cand.get("first_ts")) > self.window_s:
            return REJECT
        return WAIT


def upside_band(mcap_usd: float) -> str:
    """Honest arithmetic framing for the card: what a 10x means from here."""
    if mcap_usd <= 0:
        return "n/a"
    if mcap_usd < 50_000:
        return "micro-cap — 10x = $%.0fk" % (mcap_usd * 10 / 1000)
    if mcap_usd < 300_000:
        return "small-cap — 10x = $%.1fM" % (mcap_usd * 10 / 1_000_000)
    return "mid-cap — 10x = $%.1fM" % (mcap_usd * 10 / 1_000_000)
