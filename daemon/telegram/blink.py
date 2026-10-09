"""Card pulse: a blinking live-status line on every single-mint card.

Telegram cannot animate text, so "blinking" is implemented as message
edits: every BLINK_POLL_S the daemon re-reads the coin's mcap and edits
the card's first line, alternating the status emoji between on (green/red)
and off (black). With the chat open the card visibly pulses. Edits are
silent — Telegram never re-notifies on edit.

State machine per tracked card (mcap in USD):
  LIVE (green): mcap >= card mcap                      -> pulse green
  DUMP (red):   mcap <= peak * (1 - DUMP_DRAWDOWN)      -> pulse red
  DEAD:         mcap < card mcap * DEAD_FRAC             -> final grey edit, drop
Entries also expire after BLINK_TTL_S (final grey edit, then drop) and
the registry is capped at BLINK_MAX cards (oldest evicted).

Only single-mint cards are tracked (digests, top-picks and recaps carry
many coins and are skipped). Price source is the keyless DexScreener
tokens/v1 API, shared 1s call gap with the rest of the repo.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("forex_agent.daemon.telegram.blink")

# -- tuning -----------------------------------------------------------------
BLINK_POLL_S = 60.0        # re-read mcap + toggle blink phase this often
BLINK_TTL_S = 4 * 3600.0   # stop pulsing cards older than this
BLINK_MAX = 20             # max concurrently pulsing cards (oldest evicted)
DUMP_DRAWDOWN = 0.30       # peak -> -30% counts as dumping/rug
DEAD_FRAC = 0.05           # below 5% of card mcap the coin is dead
_MIN_CALL_GAP_S = 1.0      # DexScreener politeness gap (module-wide)
_DEX_URL = "https://api.dexscreener.com/tokens/v1/{chain}/{mint}"
_UA = {"User-Agent": "forex-agent/1.0 (+paper-trading-bot)",
       "Connection": "close"}
_CHAIN_IDS = {"solana": "solana", "base": "base",
              "ethereum": "eth", "eth": "eth"}

# Single-mint card events whose first line gets the pulse.
BLINK_EVENTS = {
    "launch.early", "degen.warning", "launch.lowcap_runner",
    "launch.milestone", "launch.second_wave", "launch.late_runner",
    "momentum.alert", "launch.detected", "launch.sustained",
}

_last_call_ts = 0.0


# -- pure state machine (unit-tested, no I/O) --------------------------------
def classify(now_usd: float, card_usd: float, peak_usd: float,
             drawdown: float = DUMP_DRAWDOWN) -> str:
    """Return 'dead', 'dump' or 'live' for the current mcap."""
    if card_usd > 0 and now_usd < card_usd * DEAD_FRAC:
        return "dead"
    if peak_usd > 0 and now_usd <= peak_usd * (1.0 - drawdown):
        return "dump"
    return "live"


def _fmt_pct(pct: float) -> str:
    sign = "+" if pct >= 0 else "\u2212"  # + / minus sign
    return "%s%.0f%%" % (sign, abs(pct))


def render_status(state: str, phase_on: bool, pct: float,
                  m5: Optional[float] = None,
                  h1: Optional[float] = None) -> str:
    """First-line pulse text. phase_on toggles every poll for the blink."""
    body = _fmt_pct(pct)
    tf = ""
    if m5 is not None and h1 is not None:
        tf = " | 5m %s \u00b7 1h %s" % (_fmt_pct(m5), _fmt_pct(h1))
    if state == "live":
        return "%s LIVE %s%s" % ("\U0001F7E2" if phase_on else "\u26AB", body, tf)
    if state == "dump":
        return "%s DUMP %s%s" % ("\U0001F534" if phase_on else "\u26AB", body, tf)
    return "\u26AB ENDED %s%s" % (body, tf)


def _card_mcap_usd(event: Dict[str, Any]) -> float:
    for key in ("usd_market_cap", "mcap_usd", "card_mcap", "market_cap_usd"):
        try:
            v = float(event.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return 0.0


# -- price fetch --------------------------------------------------------------
def _parse_stats(data: Any) -> Tuple[float, Optional[float], Optional[float]]:
    """Best (mcap, m5%, h1%) across pairs. Pure function, unit-tested."""
    best_mcap, best_m5, best_h1 = 0.0, None, None
    items = data if isinstance(data, list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            v = float(item.get("marketCap") or item.get("fdv") or 0)
        except (TypeError, ValueError):
            continue
        if v <= best_mcap:
            continue
        best_mcap = v
        pc = item.get("priceChange") or {}
        try:
            best_m5 = float(pc.get("m5")) if pc.get("m5") is not None else None
            best_h1 = float(pc.get("h1")) if pc.get("h1") is not None else None
        except (TypeError, ValueError):
            best_m5, best_h1 = None, None
    return best_mcap, best_m5, best_h1


def fetch_stats(mint: str, chain: str = "solana",
                timeout: float = 15.0) -> Tuple[float, Optional[float], Optional[float]]:
    """(mcap_usd, m5_pct, h1_pct) via DexScreener (keyless).

    (0.0, None, None) on any failure — the caller keeps the last state.
    """
    global _last_call_ts
    gap = _MIN_CALL_GAP_S - (time.time() - _last_call_ts)
    if gap > 0:
        time.sleep(gap)
    cid = _CHAIN_IDS.get(str(chain or "").lower(), "solana")
    url = _DEX_URL.format(chain=cid, mint=mint)
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        logger.debug("blink price fetch failed for %s: %s", mint, exc)
        return 0.0, None, None
    finally:
        _last_call_ts = time.time()
    return _parse_stats(data)


def fetch_mcap_usd(mint: str, chain: str = "solana",
                   timeout: float = 15.0) -> float:
    """Backwards-compatible mcap-only wrapper."""
    mcap, _, _ = fetch_stats(mint, chain, timeout)
    return mcap


# -- registry -------------------------------------------------------------------
def _registry(state: Dict[str, Any]) -> Dict[str, Any]:
    reg = state.get("blink")
    if not isinstance(reg, dict):
        reg = {}
        state["blink"] = reg
    return reg


def initial_line(event: Dict[str, Any]) -> Optional[str]:
    """Status line to prepend before sending, or None if not blinkable."""
    if event.get("event") not in BLINK_EVENTS:
        return None
    if not event.get("mint") or _card_mcap_usd(event) <= 0:
        return None
    return render_status("live", True, 0.0)


def register(state: Dict[str, Any], event: Dict[str, Any], chat_id: Any,
             message_id: Any, full_text: str) -> bool:
    """Track a freshly sent card for pulsing. full_text must already start
    with the status line from initial_line(). Returns True if tracked."""
    if initial_line(event) is None or not message_id:
        return False
    key = str(event.get("event_id") or "%s:%s" % (event.get("event"),
                                                 event.get("mint")))
    reg = _registry(state)
    # Evict oldest when capped.
    while len(reg) >= BLINK_MAX:
        oldest = min(reg, key=lambda k: reg[k].get("ts_card", 0))
        del reg[oldest]
    card_usd = _card_mcap_usd(event)
    reg[key] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "mint": event.get("mint"),
        "chain": event.get("chain") or "solana",
        "symbol": str(event.get("symbol") or "?")[:16],
        "card_usd": card_usd,
        "peak_usd": card_usd,
        "phase": True,
        "lines": full_text.split("\n"),
        "ts_card": time.time(),
    }
    return True


def _drop(reg: Dict[str, Any], key: str, client, entry: Dict[str, Any],
          final: bool = True, final_mcap: Optional[float] = None) -> None:
    """Final grey edit, then forget the card. Best-effort: never raises."""
    if final:
        try:
            ref = final_mcap if final_mcap else entry["peak_usd"]
            pct = _pct(ref, entry["card_usd"])
            lines = [render_status("dead", False, pct)] + entry["lines"][1:]
            client.edit_message_text(entry["chat_id"], entry["message_id"],
                                     "\n".join(lines))
        except Exception as exc:
            logger.debug("blink final edit failed for %s: %s",
                         entry.get("symbol"), exc)
    reg.pop(key, None)


def _pct(now_usd: float, card_usd: float) -> float:
    if card_usd <= 0:
        return 0.0
    return (now_usd - card_usd) / card_usd * 100.0


def poll(state: Dict[str, Any], client, now: Optional[float] = None) -> Dict[str, int]:
    """One pulse cycle: re-read mcaps, toggle phases, edit cards.

    Returns counters; never raises (per-card failures are isolated).
    """
    now = now or time.time()
    reg = _registry(state)
    done = {"live": 0, "dump": 0, "ended": 0, "failed": 0}
    for key in list(reg.keys()):
        e = reg[key]
        try:
            # Expiry.
            if now - e.get("ts_card", now) > BLINK_TTL_S:
                _drop(reg, key, client, e, final=True)
                done["ended"] += 1
                continue
            mcap, m5, h1 = fetch_stats(e["mint"], e.get("chain"))
            if mcap <= 0:
                continue  # price failed: keep last state, retry next poll
            e["peak_usd"] = max(e.get("peak_usd", 0.0), mcap)
            st = classify(mcap, e["card_usd"], e["peak_usd"])
            if st == "dead":
                _drop(reg, key, client, e, final=True, final_mcap=mcap)
                done["ended"] += 1
                continue
            e["phase"] = not e.get("phase", True)
            pct = _pct(mcap, e["card_usd"])
            lines = [render_status(st, e["phase"], pct, m5, h1)] + e["lines"][1:]
            client.edit_message_text(e["chat_id"], e["message_id"],
                                     "\n".join(lines))
            e["lines"] = lines
            done[st] += 1
        except Exception as exc:
            # 429/403/deleted message: drop the card, stay alive.
            logger.debug("blink poll failed for %s: %s", e.get("symbol"), exc)
            reg.pop(key, None)
            done["failed"] += 1
    return done


def maybe_poll(state: Dict[str, Any], client) -> None:
    """Throttle wrapper: at most one poll per BLINK_POLL_S."""
    now = time.time()
    if not _registry(state):
        return
    last = float(state.get("blink_last_poll") or 0)
    if now - last < BLINK_POLL_S:
        return
    state["blink_last_poll"] = now
    try:
        done = poll(state, client, now=now)
    except Exception:
        logger.exception("blink poll crashed")
        return
    if any(done.values()):
        logger.info("blink poll: %s", done)
