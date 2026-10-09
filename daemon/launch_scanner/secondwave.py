"""Second-wave re-entry: catch the second leg of a carded runner.

After a coin's first card (tracked by ``milestones.py``), watch the
multiple of card-time mcap. When the coin pulls back >=
``second_wave_pullback_pct`` from its peak multiple AND then
re-accelerates >= ``second_wave_releg_gain_pct`` from the pullback
trough within ``second_wave_reentry_window_min`` minutes — with
rising-poll confirmation (a HOT fingerprint counts as one rising
poll) — the scanner emits a distinct ``launch.second_wave`` card and
opens a NEW paper position labeled ``second_wave`` (wave number
journaled) with the standard exit plan.

The per-coin peak multiple is owned by the milestone tracker
(``milestones.py`` records ``peak_mult`` on each watch record); this
module never duplicates that tracking — it only reads it.

Discipline, matching the main lanes:
- ONE second-wave card per coin per ``second_wave_cooldown_hours``
  (default 6h). This cooldown is fully INDEPENDENT of the main 6h
  duplicate-card cooldown: it neither reads nor writes any other
  lane's state.
- The full safety gate re-runs at re-entry (fail-closed, trial bar
  applies). A failing re-check holds SILENTLY: no card, no position,
  no cooldown burned.
- A trough is consumed on confirmation — one trough never fires
  two waves.
- Never raises; never touches live/mainnet (entries go through the
  same paper ``_paper_enter`` path as first-wave runners).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("forex_agent.daemon.launch_scanner.secondwave")

_WATCH_KEY = "second_wave"          # state key: per-mint phase machines
_STATS_KEY = "second_wave_stats"    # state key: cached batch stats
_STATS_TTL_S = 30.0                 # DexScreener batch refresh cadence
_EXPIRY_S = 24 * 3600               # same lifetime as milestone tracking
_RISE_NEEDED = 2                    # consecutive rising polls to confirm


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def enabled(degen: Any) -> bool:
    """Second-wave tracking is ON unless explicitly disabled.

    Follows the codebase's getattr-with-default convention so no config
    schema change is required; the flag survives the trial auto-revert
    untouched.
    """
    return bool(getattr(degen, "second_wave_enabled", True))


def _num(degen: Any, name: str, default: float) -> float:
    try:
        return float(getattr(degen, name, default) or default)
    except (TypeError, ValueError):
        return default


def knobs(degen: Any) -> Dict[str, float]:
    """Second-wave tuning knobs (config/defaults.yaml, degen section)."""
    return {
        "pullback_pct": _num(degen, "second_wave_pullback_pct", 40.0),
        "releg_gain_pct": _num(degen, "second_wave_releg_gain_pct", 50.0),
        "window_min": _num(degen, "second_wave_reentry_window_min", 45.0),
        "cooldown_h": _num(degen, "second_wave_cooldown_hours", 6.0),
    }


def _batch_stats(watch: Dict[str, Dict[str, Any]]
                 ) -> Dict[str, Dict[str, Any]]:
    """Current {mint: stats} via the milestone tracker's batch fetcher."""
    from . import milestones  # noqa: PLC0415 (lazy: package init order)
    return milestones._batch_stats(watch)


def _refresh_stats(state: Dict[str, Any],
                   watch: Dict[str, Dict[str, Any]],
                   now: float) -> Dict[str, Dict[str, Any]]:
    """Throttled batch refresh shared by all tracked mints this poll."""
    cached = state.get(_STATS_KEY)
    if (isinstance(cached, dict)
            and now - _f(cached.get("ts")) < _STATS_TTL_S
            and isinstance(cached.get("data"), dict)):
        return cached["data"]
    data: Dict[str, Dict[str, Any]] = {}
    try:
        data = _batch_stats(watch) or {}
    except Exception:
        logger.warning("second-wave stats refresh failed", exc_info=True)
    state[_STATS_KEY] = {"ts": now, "data": data}
    return data


def _fp_hot(state: Dict[str, Any], mint: str) -> bool:
    """HOT fingerprint counts as one rising poll (lowcap.py pattern).

    The fingerprint tracker runs in the judge pipeline; the verdict is
    read opportunistically from existing state — never computed here.
    """
    try:
        return (state.get("fingerprints") or {}).get(mint, {}).get(
            "verdict") == "HOT"
    except Exception:
        return False


def _update(rec: Dict[str, Any], mult: float, peak: float, now: float,
            kb: Dict[str, float], fp_hot: bool) -> bool:
    """Advance one coin's phase machine. Returns True on confirmation.

    Phases:
    - "up": waiting for a >= pullback_pct drop from the peak multiple.
    - "pullback": trough formed; waiting for >= releg_gain_pct
      re-acceleration from the trough within window_min, confirmed by
      _RISE_NEEDED consecutive rising polls (HOT fingerprint counts
      as one). The trough deepens while it falls (window restarts from
      the new low); a stale trough resets to "up" without firing.
    """
    phase = rec.get("phase") or "up"
    if phase == "up":
        rec["last_mult"] = mult
        rec["rise_streak"] = 0
        if peak > 0 and mult <= peak * (1 - kb["pullback_pct"] / 100.0):
            rec["phase"] = "pullback"
            rec["trough_mult"] = mult
            rec["trough_ts"] = now
            logger.warning(
                "SECOND WAVE pullback %s: -%.0f%% from %.1fx peak "
                "(now %.1fx) — watching for re-entry",
                rec.get("symbol"), 100.0 * (1 - mult / peak), peak, mult)
        return False
    # phase == "pullback"
    trough = _f(rec.get("trough_mult"))
    if trough <= 0:
        rec["trough_mult"] = mult
        rec["trough_ts"] = now
        rec["rise_streak"] = 0
        rec["last_mult"] = mult
        return False
    if mult < trough:
        # Trough deepens — restart the re-entry window from the new low.
        rec["trough_mult"] = mult
        rec["trough_ts"] = now
        rec["rise_streak"] = 0
        rec["last_mult"] = mult
        return False
    if now - _f(rec.get("trough_ts")) > kb["window_min"] * 60.0:
        # Stale trough: the re-acceleration window closed without a
        # confirmation — back to waiting for a fresh pullback.
        rec["phase"] = "up"
        rec["rise_streak"] = 0
        rec["last_mult"] = mult
        logger.warning("SECOND WAVE stale trough %s: re-acceleration "
                       "window expired without confirming", rec.get("symbol"))
        return False
    if mult > _f(rec.get("last_mult")):
        rec["rise_streak"] = int(rec.get("rise_streak") or 0) + 1
    else:
        rec["rise_streak"] = 0
    rec["last_mult"] = mult
    needed = _RISE_NEEDED - (1 if fp_hot else 0)
    gain = mult / trough - 1.0
    if gain >= kb["releg_gain_pct"] / 100.0 and rec["rise_streak"] >= needed:
        # CONFIRMED — consume the trough so one trough never fires
        # two waves. The confirming streak is journaled on the payload.
        rec["confirm_streak"] = rec["rise_streak"]
        rec["phase"] = "up"
        rec["rise_streak"] = 0
        return True
    return False


def _wave_payload(rec: Dict[str, Any], mrec: Dict[str, Any], mint: str,
                  mcap: float, anchor: float, mult: float, peak: float,
                  now: float, fp_hot: bool) -> Dict[str, Any]:
    trough = _f(rec.get("trough_mult")) or mult
    return {
        "mint": mint,
        "chain": str(mrec.get("chain") or "solana"),
        "symbol": str(rec.get("symbol") or mint[:8]),
        "name": str(rec.get("name") or ""),
        "url": str(rec.get("url") or ""),
        "mcap_now": mcap,
        "anchor": anchor,
        "peak_multiple": round(peak, 2),
        "multiple_now": round(mult, 2),
        "trough_multiple": round(trough, 2),
        "pullback_pct": round(100.0 * (1 - trough / peak), 1)
        if peak > 0 else 0.0,
        "relegain_pct": round(100.0 * (mult / trough - 1.0), 1)
        if trough > 0 else 0.0,
        "rise_streak": int(rec.get("confirm_streak")
                          or rec.get("rise_streak") or 0),
        "fp_hot": bool(fp_hot),
        # Wave 1 was the original card; a re-entry is wave 2, 3, ...
        "wave_number": int(rec.get("wave_number", 1) or 1) + 1,
        "minutes_since_card": round(
            (now - _f(mrec.get("card_ts") or now)) / 60.0, 1),
        "source": str(mrec.get("source") or "signal"),
    }


def poll(state: Dict[str, Any], degen: Any, now: float,
         cycle: int) -> List[Dict[str, Any]]:
    """Advance second-wave tracking; return confirmed wave payloads.

    Reads the milestone tracker's per-coin peak multiple (owned there,
    never duplicated here). Never raises.
    """
    out: List[Dict[str, Any]] = []
    try:
        if not enabled(degen):
            return out
        from . import milestones  # noqa: PLC0415 (lazy: package init order)
        kb = knobs(degen)
        watch = milestones.tracked(state)
        sw = state.setdefault(_WATCH_KEY, {})
        # Prune coins that left milestone tracking (24h expiry there).
        for mint in list(sw):
            rec = sw[mint]
            if (mint not in watch
                    or now - _f(rec.get("first_ts", now)) > _EXPIRY_S):
                sw.pop(mint, None)
        if not watch:
            return out
        stats = _refresh_stats(state, watch, now)
        for mint, mrec in watch.items():
            if not isinstance(mrec, dict):
                continue
            anchor = _f(mrec.get("anchor"))
            if anchor <= 0:
                continue  # pending: no actionable reference mcap yet
            mcap = _f((stats.get(mint) or {}).get("usd_market_cap"))
            if mcap <= 0:
                continue
            mult = mcap / anchor
            # Peak multiple is owned by the milestone tracker.
            peak = _f(mrec.get("peak_mult")) or mult
            rec = sw.get(mint)
            if not isinstance(rec, dict):
                rec = {"symbol": str(mrec.get("symbol") or mint[:8]),
                       "chain": str(mrec.get("chain") or "solana"),
                       "name": str(mrec.get("name") or ""),
                       "url": str(mrec.get("url") or ""),
                       "phase": "up", "trough_mult": 0.0,
                       "trough_ts": 0.0, "last_mult": mult,
                       "rise_streak": 0, "wave_number": 1,
                       "last_card_ts": 0.0, "first_ts": now}
                sw[mint] = rec
            else:
                rec["symbol"] = str(mrec.get("symbol")
                                    or rec.get("symbol") or mint[:8])
                rec["chain"] = str(mrec.get("chain")
                                   or rec.get("chain") or "solana")
                rec["name"] = str(mrec.get("name") or rec.get("name")
                                  or "")
                rec["url"] = str(mrec.get("url") or rec.get("url") or "")
            hot = _fp_hot(state, mint)
            try:
                fired = _update(rec, mult, peak, now, kb, hot)
            except Exception:
                logger.exception("second-wave update failed for %s", mint)
                continue
            if fired:
                out.append(_wave_payload(rec, mrec, mint, mcap, anchor,
                                         mult, peak, now, hot))
        return out
    except Exception:
        logger.exception("second-wave poll failed")
        return []


def handle_wave(scanner: Any, config: Any, adapter: Any,
                state: Dict[str, Any], sw: Dict[str, Any]) -> bool:
    """Safety re-check, card, and paper entry for one confirmed wave.

    - The per-coin second-wave cooldown (default 6h) is checked FIRST
      and is independent of the main duplicate-card cooldown.
    - The full safety gate re-runs at re-entry (fail-closed, trial
      bar applies). A failing re-check holds SILENTLY: no card, no
      position, and the cooldown is NOT burned.
    - On success the ``launch.second_wave`` card publishes with the
      standard trade plan, and a NEW paper position opens labeled
      ``second_wave`` with the wave number journaled.
    Returns True when a card was emitted. Never raises.
    """
    from agent import events as event_bus  # noqa: PLC0415
    from . import safety  # noqa: PLC0415 (lazy: package init order)

    degen = config.degen
    mint = sw.get("mint")
    chain = str(sw.get("chain") or "solana")
    symbol = str(sw.get("symbol") or (mint or "")[:8])
    kb = knobs(degen)
    now = time.time()
    try:
        if not mint:
            return False
        rec = (state.get(_WATCH_KEY) or {}).get(mint)
        # Cooldown: ONE second-wave card per coin per 6h. Independent
        # of (never reads, never writes) the main duplicate-card
        # cooldown.
        if (rec is not None and now - _f(rec.get("last_card_ts"))
                < kb["cooldown_h"] * 3600.0):
            logger.warning("SECOND WAVE cooldown %s %s: wave #%d "
                           "suppressed (%.1fh of %.1fh left)",
                           symbol, mint, sw.get("wave_number"),
                           (kb["cooldown_h"] * 3600.0
                            - (now - _f(rec.get("last_card_ts")))) / 3600.0,
                           kb["cooldown_h"])
            return False
        # Full safety re-check at re-entry — fail-closed. The trial
        # score bar (early_min_score) applies here exactly as it does
        # to first-wave confirmations.
        ok, reason, report = scanner._confirm_safety_ok(
            degen, {"mint": mint, "chain": chain})
        if not ok:
            logger.warning("SECOND WAVE HELD (safety) %s %s: %s — "
                           "holding silently, no card, no position",
                           symbol, mint, reason)
            return False
        wave_no = int(sw.get("wave_number") or 2)
        saf = report if isinstance(report, dict) else {}
        card: Dict[str, Any] = {
            "event": "launch.second_wave",
            "symbol": symbol,
            "mint": mint,
            "chain": chain,
            "name": str(sw.get("name") or ""),
            "url": str(sw.get("url") or ""),
            "usd_market_cap": float(sw.get("mcap_now") or 0),
            "wave_number": wave_no,
            "pullback_pct": sw.get("pullback_pct"),
            "relegain_pct": sw.get("relegain_pct"),
            "peak_multiple": sw.get("peak_multiple"),
            "multiple_now": sw.get("multiple_now"),
            # Strict: the re-check score is ALWAYS noted on the card.
            "safety": safety.verdict_line(saf),
            "safety_score": saf.get("score"),
            "plan": scanner._trade_plan(degen),
            "source": str(sw.get("source") or ""),
            "confirmed": True,
            "minutes_since_card": sw.get("minutes_since_card"),
            "entry": None,
            "note": "",
        }
        entered = False
        if chain == "solana" and getattr(degen, "auto_enter", True):
            from daemon.launch_scanner import count_base_positions  # noqa: PLC0415
            if count_base_positions(state.get("degen_positions")) >= int(
                    getattr(degen, "max_positions", 5) or 5):
                card["note"] = "max degen positions reached — alert only"
            else:
                entered = scanner._paper_enter(
                    config, adapter, state, card, degen, mint, symbol,
                    wave=wave_no, intel={"safety": saf})
                # A second wave is time-sensitive: if the quote isn't
                # indexed, it is alert-only — it does NOT join the
                # first-wave pending-entry retry queue.
        if not entered and not card.get("note"):
            if chain != "solana":
                card["note"] = "alert only — paper engine prices Solana"
            elif not getattr(degen, "auto_enter", True):
                card["note"] = "auto_enter off — alert only"
        event_bus.publish(card)
        # Second-wave measurement (idea #3, user decision 2026-10-01):
        # snapshot every published wave card so the 24h forward multiple
        # can settle into the measurement ledger. Records only — the
        # entry behavior above is unchanged.
        try:
            if getattr(degen, "wavefront_enabled", True):
                from daemon.launch_scanner import wavefront  # noqa: PLC0415
                wavefront.record_wave(state, card, now)
        except Exception:
            logger.exception("wavefront record failed for %s", mint)
        if rec is not None:
            rec["wave_number"] = wave_no
            rec["last_card_ts"] = now
        logger.warning("SECOND WAVE #%d %s %s: pullback -%.0f%% from "
                       "%.1fx peak -> +%.0f%% from trough (now %.1fx "
                       "of card)%s",
                       wave_no, symbol, mint,
                       float(sw.get("pullback_pct") or 0),
                       float(sw.get("peak_multiple") or 0),
                       float(sw.get("relegain_pct") or 0),
                       float(sw.get("multiple_now") or 0),
                       " [paper entry]" if entered else "")
        return True
    except Exception:
        logger.exception("second-wave handle failed for %s", mint)
        return False
