"""Low-cap runner hunter — catch running coins at LOW mcap, pre-viral.

Tracks mints whose market cap sits under ``lowcap_mcap_ceiling_usd`` and
baselines each one at its first sub-ceiling sighting. When the run from
the baseline lands inside [lowcap_min_run_pct, lowcap_max_run_pct]
(default 70–100%) AND the fast quality gate passes (known safety report,
no hard fails, min score), it emits a ``launch.lowcap_runner`` event —
an UNCONFIRMED, alert-only tripwire with the CA.

Rules that keep the user's standing instructions:

* A CA drops ONLY inside the 70–100% running band. Below 70% the coin
  is still being watched; above 100% it has left the pre-viral window
  and the hunter stands down for that mint (the strict confirmed-runner
  pipeline in runner.py still applies independently).
* The coin must still be at/below the low-mcap ceiling when the band
  hits — no chasing coins that already left low-cap territory.
* Evidence may be boosted by HOT early fingerprints (see
  fingerprints.py): extreme buy/volume acceleration counts as one
  rising poll, so 1 real rising poll + HOT fingerprints clears the
  streak gate. The streak floor stays at 1; every other gate unchanged.
  Disable with ``lowcap_fp_boost_enabled: false``.
* The instant quality verdict is the single choke point: unknown,
  safety-failed, or low-score coins are held silently and can NEVER
  produce a CA-bearing alert from this module.
* Rugged/died coins (mcap < 40% of tracked peak) are dropped silently.
* One alert per mint, ever. Tracking expires after lowcap_window_minutes.

No paper entry happens here — runner.py's confirmed pipeline remains the
only path to a paper entry. Pure logic, no I/O: the daemon injects the
quality-verdict callable.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

# (allow, reason, report) — same shape as
# LaunchScanner._instant_verdict.
VerdictFn = Callable[[Dict[str, Any]], Tuple[bool, str, Optional[Dict[str, Any]]]]

# Hard cap on tracked mints so daemon state stays small.
MAX_TRACKED = 500


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class LowcapHunter:
    """Applies the low-cap runner rules to one mint per call."""

    def __init__(self, cfg):
        self.cfg = cfg  # retained so owners can detect config swaps
        self.enabled = bool(getattr(cfg, "lowcap_enabled", True))
        self.ceiling = float(
            getattr(cfg, "lowcap_mcap_ceiling_usd", 500000.0))
        self.min_run = float(getattr(cfg, "lowcap_min_run_pct", 70.0))
        self.max_run = float(getattr(cfg, "lowcap_max_run_pct", 100.0))
        self.rising_polls = int(getattr(cfg, "lowcap_rising_polls", 2))
        self.min_vol_m5 = float(
            getattr(cfg, "lowcap_min_m5_volume_usd", 2000.0))
        self.min_liq = float(
            getattr(cfg, "lowcap_min_liquidity_usd", 8000.0))
        self.min_buys = int(getattr(cfg, "lowcap_min_buys_m5", 4))
        self.buy_sell_ratio = float(
            getattr(cfg, "lowcap_min_buy_sell_ratio", 1.3))
        self.window_s = float(
            getattr(cfg, "lowcap_window_minutes", 30.0)) * 60
        # HOT-fingerprint boost: extreme buy/volume acceleration (see
        # fingerprints.py) counts as one rising poll of evidence, so a
        # coin with 1 real rising poll + HOT fingerprints clears the
        # streak gate instead of needing 2 flat rising polls.
        self.fp_boost = bool(getattr(cfg, "lowcap_fp_boost_enabled", True))

    # -- state hygiene ------------------------------------------------
    def prune(self, tracked: Dict[str, Dict[str, Any]], now: float) -> None:
        """Drop expired entries and cap the table at MAX_TRACKED."""
        now_f = float(now)
        for mint in list(tracked):
            if now_f - float(tracked[mint].get("base_ts", now_f)) > self.window_s:
                tracked.pop(mint, None)
        if len(tracked) > MAX_TRACKED:
            ordered = sorted(tracked.items(),
                             key=lambda kv: float(kv[1].get("base_ts", 0)))
            for mint, _ in ordered[:len(tracked) - MAX_TRACKED]:
                tracked.pop(mint, None)

    # -- main entry ----------------------------------------------------
    def update(self, tracked: Dict[str, Dict[str, Any]], *,
               mint: str, symbol: str, name: str, chain: str, source: str,
               url: str, stats: Optional[Dict[str, Any]],
               rising_streak: int, now: float,
               verdict_fn: VerdictFn,
               probe: Optional[Dict[str, Any]] = None,
               fp_score: Optional[Dict[str, Any]] = None,
               ) -> Optional[Dict[str, Any]]:
        """Feed fresh DexScreener pair stats for one mint.

        Returns the ``launch.lowcap_runner`` event dict when every gate
        passes, else None (coin stays tracked for later polls).

        When ``probe`` is given it is filled with ``baselined`` (bool),
        ``fired`` (bool) and ``blocked_by`` (gate name or None) so owners
        can see exactly which gate holds each tracked coin.
        """
        def _note(reason: Optional[str]) -> None:
            if probe is not None:
                probe["blocked_by"] = reason

        if probe is not None:
            probe["baselined"] = False
            probe["fired"] = False
            probe["blocked_by"] = None
        if not self.enabled or not mint or not stats:
            _note("no_stats" if mint else "no_mint")
            return None
        mcap = _f(stats.get("usd_market_cap"))
        if mcap <= 0:
            _note("no_mcap")
            return None
        now_f = float(now)

        t = tracked.get(mint)
        if t is None:
            # First sighting: only baseline coins that are actually
            # low-cap. Anything above the ceiling was never our prey.
            if mcap > self.ceiling:
                _note("above_ceiling")
                return None
            tracked[mint] = {"base": mcap, "peak": mcap,
                             "base_ts": now_f, "alerted": False}
            if probe is not None:
                probe["baselined"] = True
            return None
        if t.get("alerted"):
            _note("already_alerted")
            return None
        if now_f - float(t.get("base_ts", now_f)) > self.window_s:
            tracked.pop(mint, None)
            _note("expired")
            return None
        base = _f(t.get("base"))
        if base <= 0:
            tracked.pop(mint, None)
            _note("bad_base")
            return None

        # Peak tracking + death check: rugged / died — drop silently.
        peak = _f(t.get("peak")) or mcap
        if mcap > peak:
            t["peak"] = peak = mcap
        if mcap < peak * 0.4:
            tracked.pop(mint, None)
            _note("died")
            return None

        # Left low-cap territory: the hunter stands down for this mint.
        if mcap > self.ceiling:
            t["alerted"] = True
            _note("left_ceiling")
            return None

        run_pct = (mcap - base) / base * 100.0
        if run_pct < self.min_run:
            _note("waiting_run")
            return None  # not running hard enough yet — keep watching
        if run_pct > self.max_run:
            # Ran past the pre-viral window — too late for this hunter.
            t["alerted"] = True
            _note("past_window")
            return None

        # Demand evidence: a real climb, real money, buy-side pressure.
        # A HOT fingerprint (extreme buy/volume acceleration) counts as
        # one rising poll of evidence — never a free pass, the streak
        # floor stays at 1 and every other gate is unchanged.
        fp_hot = self.fp_boost and (fp_score or {}).get("verdict") == "HOT"
        need_streak = max(1, self.rising_polls - (1 if fp_hot else 0))
        if probe is not None:
            probe["fp_boost"] = bool(fp_hot)
        if int(rising_streak or 0) < need_streak:
            _note("streak")
            return None
        vol_m5 = _f(stats.get("vol_m5"))
        liq = _f(stats.get("usd_liquidity"))
        if vol_m5 < self.min_vol_m5 or liq < self.min_liq:
            _note("volume" if vol_m5 < self.min_vol_m5 else "liquidity")
            return None
        buys = int(stats.get("buys_m5") or 0)
        sells = int(stats.get("sells_m5") or 0)
        if not (buys >= self.min_buys and (
                sells == 0 or buys / max(sells, 1) >= self.buy_sell_ratio)):
            _note("buys")
            return None

        # Quality gate — the single choke point. Unknown, hard-failed,
        # or low-score coins are held silently and NEVER drop a CA.
        allow, reason, report = verdict_fn({"mint": mint, "chain": chain})
        if not allow:
            _note("safety")
            return None
        t["alerted"] = True
        if probe is not None:
            probe["fired"] = True
        return {
            "event": "launch.lowcap_runner",
            "tier": "lowcap",
            "confirmed": False,
            "source": source,
            "chain": chain,
            "mint": mint,
            "symbol": symbol or mint[:8],
            "name": name or "",
            "url": url or "",
            "usd_market_cap": round(mcap, 2),
            "base_mcap": round(base, 2),
            "run_pct": round(run_pct, 1),
            "age_minutes": round((now_f - float(t.get("base_ts", now_f))) / 60.0, 1),
            "safety_score": ((report or {}).get("score")),
            "evidence": {
                "rising_polls": int(rising_streak or 0),
                "vol_m5": round(vol_m5, 2),
                "liquidity": round(liq, 2),
                "buys_m5": buys,
                "sells_m5": sells,
                "fingerprint": ({
                    "score": (fp_score or {}).get("composite"),
                    "verdict": (fp_score or {}).get("verdict"),
                    "buy_accel": (fp_score or {}).get("buy_accel"),
                    "vol_accel": (fp_score or {}).get("vol_accel"),
                    "holder_growth_pct":
                        (fp_score or {}).get("holder_growth_pct"),
                } if fp_score else None),
                "fp_boost": bool(fp_hot),
            },
            "note": ("Low-cap runner: +%.0f%% from $%s base — UNCONFIRMED "
                     "pre-viral window signal (%s). High risk.%s"
                     % (run_pct, _fmt_mcap(base), reason,
                        _fp_note_line(fp_score))),
        }


def _fp_note_line(fp_score: Optional[Dict[str, Any]]) -> str:
    if not fp_score:
        return ""
    hg = fp_score.get("holder_growth_pct")
    return (" Fingerprints: buys %.1fx, vol %.1fx, holders %s "
            "(score %.0f %s)."
            % (fp_score.get("buy_accel") or 0,
               fp_score.get("vol_accel") or 0,
               ("%+.1f%%" % hg) if hg is not None else "n/a",
               fp_score.get("composite") or 0,
               fp_score.get("verdict") or "?"))


def _fmt_mcap(value: float) -> str:
    if value >= 1_000_000:
        return "%.2fM" % (value / 1_000_000)
    if value >= 1_000:
        return "%.1fk" % (value / 1_000)
    return "%.0f" % value
