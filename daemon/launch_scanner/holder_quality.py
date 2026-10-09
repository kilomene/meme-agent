"""Holder-quality ranker (idea #1).

Ranks confirmed runners by whether HOLDERS are rising alongside the
market cap — a run carried by broadening distribution looks different
from one carried by a shrinking pool of wallets. This is a RANKER, not
a gate: it scores 0-100 with explanatory factors and never blocks
anything by itself. Wiring (on user go-ahead) would add a
``holder_quality_min`` knob consulted the same way as
``winner_min_score``.

Pure + dependency-free: all inputs are plain dicts. Never raises.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("holder_quality")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def _opt(value: Any) -> Optional[float]:
    """Optional float: None stays None (genuinely unknown).

    Unknown inputs must NEVER be coerced to 0.0 — a 0.0 holder growth
    would read as "holders stalled" and falsely flag distribution on
    any coin with mcap growth. None means "no data", fail-neutral.
    """
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


def holder_inputs(fp: Optional[Dict[str, Any]],
                  safety: Optional[Dict[str, Any]],
                  holder_count: Optional[float] = None) -> Dict[str, Any]:
    """Normalize the raw inputs into one dict.

    ``fp``: fingerprint entry from ``state["fingerprints"][mint]``
    (holder_growth_pct, buy_pressure). ``safety``: the safety report
    dict (may carry top-holder concentration fields). ``holder_count``:
    latest known holder count, when available.
    """
    fp = fp if isinstance(fp, dict) else {}
    safety = safety if isinstance(safety, dict) else {}
    return {
        # None (unknown) is preserved — see _opt. score() treats None
        # as no-data, never as zero.
        "holder_growth_pct": _opt(fp.get("holder_growth_pct")),
        "buy_pressure": _f(fp.get("buy_pressure"), -1.0),
        # Top-holder concentration: prefer an explicit share field when
        # the report carries one; otherwise unknown (-1.0).
        "top_share_pct": _f(
            safety.get("top_holder_share_pct",
                       safety.get("top10_share_pct", -1.0)), -1.0),
        "holder_count": _f(holder_count, -1.0),
    }


def score(inp: Dict[str, Any],
          mcap_growth_pct: Optional[float] = None
          ) -> Tuple[float, List[str]]:
    """Score holder quality 0-100.

    ``mcap_growth_pct`` is the mcap move over the same window the
    holder growth was measured on. The core signal is DIVERGENCE:
    holders growing while mcap grows = accumulation (healthy);
    mcap sprinting while holders stall = distribution into exits.

    Returns ``(score, [factor lines])``. Unknown inputs contribute
    nothing (fail-neutral). Never raises.
    """
    factors: List[str] = []
    try:
        inp = inp if isinstance(inp, dict) else {}
        hg = _opt(inp.get("holder_growth_pct"))  # None = unknown
        bp = _f(inp.get("buy_pressure"), -1.0)
        top = _f(inp.get("top_share_pct"), -1.0)
        hc = _f(inp.get("holder_count"), -1.0)

        total = 50.0  # neutral start; evidence moves it

        # 1. Holder growth itself (0..+25). Skipped when unknown.
        if hg is not None:
            if hg >= 50:
                total += 25
                factors.append("holders +%.0f%% (broadening)" % hg)
            elif hg >= 20:
                total += 15
                factors.append("holders +%.0f%%" % hg)
            elif hg >= 5:
                total += 7
            elif hg < -10:
                total -= 15
                factors.append("holders %.0f%% (shrinking)" % hg)

        # 2. Divergence: holder growth MINUS mcap growth over the same
        # window. Positive = the crowd is arriving with the price.
        # Needs BOTH sides known; unknown holder growth must never
        # read as "holders stalled" (false distribution flag).
        mg = _f(mcap_growth_pct, -1.0)
        if mg >= 0 and hg is not None:
            div = hg - mg
            if div >= 0:
                total += 15
                factors.append("holders keeping up with +%.0f%% mcap" % mg)
            elif div >= -30:
                total += 5
            else:
                total -= 20
                factors.append(
                    "mcap +%.0f%% but holders +%.0f%% (distribution)" %
                    (mg, hg))
        elif mg >= 0:
            factors.append("holder growth unknown — divergence skipped")
        else:
            factors.append("mcap window unknown — divergence skipped")

        # 3. Buy pressure corroboration (0..+10).
        if bp >= 0.6:
            total += 10
            factors.append("buy pressure %.2f" % bp)
        elif 0 <= bp < 0.4:
            total -= 8
            factors.append("sell pressure %.2f" % bp)

        # 4. Concentration: a single top holder owning most of supply
        # is the classic rug shape (-20).
        if top >= 0:
            if top >= 50:
                total -= 20
                factors.append("top holder %.0f%% of supply" % top)
            elif top >= 25:
                total -= 8
                factors.append("top holder %.0f%%" % top)
            elif top <= 10:
                total += 5
                factors.append("distributed top holders (%.0f%%)" % top)

        # 5. Absolute holder base: a real crowd is harder to rug.
        if hc >= 1000:
            total += 5
            factors.append("%.0f holders" % hc)
        elif 0 <= hc < 100:
            total -= 5
            factors.append("only %.0f holders" % hc)

        total = max(0.0, min(100.0, round(total, 1)))
        return total, factors
    except Exception as exc:
        logger.warning("holder quality score failed: %s", exc)
        return 50.0, []


def rank(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rank pre-scored candidate dicts by holder quality.

    Each candidate needs ``holder_inputs`` + ``mcap_growth_pct`` keys
    (or already-scored ``holder_score``). Returns the list sorted
    best-first with ``holder_score``/``holder_factors`` attached.
    Pure; never raises.
    """
    try:
        out = []
        for c in candidates or []:
            if not isinstance(c, dict):
                continue
            if c.get("holder_score") is None:
                s, f = score(c.get("holder_inputs") or {},
                             c.get("mcap_growth_pct"))
                c = dict(c, holder_score=s, holder_factors=f)
            out.append(c)
        out.sort(key=lambda c: _f(c.get("holder_score"), 0.0),
                 reverse=True)
        return out
    except Exception as exc:
        logger.warning("holder rank failed: %s", exc)
        return list(candidates or [])
