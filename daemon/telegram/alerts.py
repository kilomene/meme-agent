"""Alert fan-out: journal events -> Telegram messages.

The daemon polls the durable event journal; every new event whose type has
a formatter below becomes a Telegram alert to the paired chat. Events are
deduplicated by their journal ``event_id`` (persisted across restarts).

Unknown event types are ignored — alerts must be intentional, never a
firehose of internal noise.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from daemon.telegram.signal_card import render_signal_card


def _fmt_money(value: Any) -> str:
    try:
        return "$%.2f" % float(value)
    except (TypeError, ValueError):
        return "n/a"


def _signal(e: Dict[str, Any]) -> str:
    return render_signal_card(e)


def _fmt_mcap(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if v >= 1_000_000:
        return "$%.2fM" % (v / 1_000_000)
    if v >= 1_000:
        return "$%.1fk" % (v / 1_000)
    return "$%.0f" % v


def _score_line(score) -> str:
    """Strict: the safety score is ALWAYS noted on a dropped coin — the
    line is never omitted; when no score exists it says so explicitly."""
    if score is not None:
        return "fast-analysis safety score: %s/100" % score
    return "safety score: unavailable (no safety report)"


def _winner_line(e: Dict[str, Any]) -> str:
    """Winner-likeness line (idea #5): 0-100 resemblance to the bot's
    own past 10x+ runners at entry, with the top explanatory factors.
    Omitted when the score was never computed."""
    try:
        s = e.get("winner_score")
        if s is None:
            return ""
        line = "🏆 winner-likeness %.0f/100" % float(s)
        factors = e.get("winner_factors")
        if factors:
            line += " (%s)" % factors
        return line
    except (TypeError, ValueError):
        return ""


def _holder_line(e: Dict[str, Any]) -> str:
    """Holder-quality line (idea #1): 0-100 whether holder distribution
    is broadening with the price or the mcap is sprinting while
    holders stall (distribution into exits). Omitted when the score
    was never computed."""
    try:
        s = e.get("holder_score")
        if s is None:
            return ""
        line = "👥 holder quality %.0f/100" % float(s)
        factors = e.get("holder_factors")
        if factors:
            line += " (%s)" % factors
        return line
    except (TypeError, ValueError):
        return ""


def _plan_line(e: Dict[str, Any]) -> Optional[str]:
    """Trade plan line for a signal card.

    Renders the standing plan attached at publish time (stop / TP /
    trailing / max hold) plus absolute mcap levels when the signal's
    market cap is known. Returns None when the event carries no plan —
    old journal events must never crash the formatter.
    """
    plan = e.get("plan")
    if not isinstance(plan, dict):
        return None
    try:
        stop = float(plan.get("stop_pct"))
        tp = float(plan.get("tp_pct"))
    except (TypeError, ValueError):
        return None
    hold = plan.get("max_hold_h")
    trail_t = plan.get("trail_trigger_pct")
    trail_d = plan.get("trail_drawdown_pct")
    line = "📋 Plan: stop -%g%% · TP +%g%%" % (stop, tp)
    if trail_t is not None:
        try:
            line += " · trail locks at +%g%% (exit -%g%% off peak)" % (
                float(trail_t), float(trail_d or 0))
        except (TypeError, ValueError):
            pass
    # Leg A profit lock (v2 exits): bank profit early on the classic
    # half; the runner half keeps the single looser trail.
    try:
        leg_a_t = plan.get("leg_a_trail_trigger_pct")
        if leg_a_t is not None:
            line += " · Leg A locks profit at +%g%% (-%g%% off peak)" % (
                float(leg_a_t),
                float(plan.get("leg_a_trail_drawdown_pct") or 0))
    except (TypeError, ValueError):
        pass
    try:
        if hold is not None:
            line += " · max hold %gh" % float(hold)
    except (TypeError, ValueError):
        pass
    # Two-tier exits: the runner half skips the fixed TP and holds
    # longer with the same stop/trail.
    try:
        if plan.get("two_tier"):
            line += ("\n   ↳ runner half: no fixed TP · same stop/trail"
                     " · max hold %gh" % float(plan.get("tier_b_max_hold_h")
                                               or 24.0))
    except (TypeError, ValueError):
        pass
    try:
        mcap = float(e.get("usd_market_cap") or 0)
    except (TypeError, ValueError):
        mcap = 0.0
    if mcap > 0:
        line += ("\n   ↳ @ mcap %s: stop %s · TP %s" % (
            _fmt_mcap(mcap),
            _fmt_mcap(mcap * (1 - stop / 100.0)),
            _fmt_mcap(mcap * (1 + tp / 100.0))))
    else:
        line += " · levels set at first price"
    return line


def _intel_line(e: Dict[str, Any]) -> str:
    """Narrative + sentiment intel, one compact block.

    Soft signals only: they describe the coin, they never decided the
    drop. Omitted entirely when there is nothing to show.
    """
    parts = []
    tags = [t for t in (e.get("narratives") or []) if t != "other"]
    if tags:
        heat = e.get("narrative_heat")
        suffix = (" · heat %.1fx" % heat) if isinstance(
            heat, (int, float)) else ""
        parts.append("🏷️ %s%s" % (", ".join(tags[:4]), suffix))
    s = e.get("sentiment")
    if s:
        parts.append("💬 sentiment: %s" % s)
    return "\n".join(parts)


def _launch_early(e: Dict[str, Any]) -> str:
    """Tier-1 degen card — instant CA for coins that passed fast analysis.

    The coin cleared the instant quality gate (known safety report, no
    hard fails, min score) — but full multi-poll confirmation is still
    running, and no paper entry happens here. Speed with honesty.
    """
    tier = str(e.get("tier", "?")).upper()
    lines = [
        "⚡ EARLY DETECT [%s] — %s" % (tier, e.get("symbol", "?") or "?"),
    ]
    name = e.get("name")
    if name:
        lines.append(str(name)[:60])
    lines.append("chain: %s · via %s" % (e.get("chain", "?"),
                                         e.get("source", "?")))
    if e.get("mint"):
        lines.append("CA: %s" % e["mint"])
    else:
        lines.append("no contract address (CEX listing)")
    lines.append("mcap %s · spotted %s min ago" % (
        _fmt_mcap(e.get("usd_market_cap")), e.get("age_minutes", "?")))
    lines.append("🛡 " + _score_line(e.get("safety_score")))
    plan = _plan_line(e)
    if plan:
        lines.append(plan)
    intel = _intel_line(e)
    if intel:
        lines.append(intel)
    # Smart-buy fast-track: wallet names on the instant card.
    sm = e.get("smart_money")
    if sm:
        lines.append("🧠 smart money: %s" % sm)
    # Deployer fast-track: the hot deployer's line. The worker's
    # deployer_line already carries its 🏭 prefix — render as-is.
    dl = e.get("deployer_line")
    if dl:
        lines.append(str(dl))
    # Sniper tier: the card shows its work — score + one-line reason.
    sl = e.get("sniper_line")
    if sl:
        lines.append(str(sl))
    lines.append("⚠️ UNCONFIRMED — full confirmation still "
                 "running. High risk, size accordingly.")
    if e.get("url"):
        lines.append(str(e["url"]))
    return "\n".join(lines)


def _momentum_alert(e: Dict[str, Any]) -> str:
    """Established-runner card — a coin already running that cleared the
    same fast-analysis quality gate as launches (known safety report, no
    hard fails, min score). Not a fresh launch: the move started hours
    ago, so the anti-top-buying guard already filtered exhausted pumps.
    Paper entry (if any) only happens on a second consecutive
    qualification — never on this first sighting.

    Two lanes share this card: "established" (bigger caps) and
    "lowcap" (at/below the lowcap mcap ceiling). The lane only changes
    the title and risk line — never the gates the coin had to pass.
    """
    lane = str(e.get("lane") or "established").lower()
    if lane == "lowcap":
        title = "🚀 LOWCAP MOMENTUM — %s" % (e.get("symbol", "?") or "?")
        risk = ("⚠️ Low-cap runner: wilder swings, higher rug risk. "
                "Paper only.")
    else:
        title = "🚀 MOMENTUM RUNNER — %s" % (e.get("symbol", "?") or "?")
        risk = ("⚠️ Established coin mid-run: momentum can reverse "
                "fast. Paper only.")
    lines = [title]
    name = e.get("name")
    if name:
        lines.append(str(name)[:60])
    lines.append("chain: %s · via %s" % (e.get("chain", "?"),
                                        e.get("source", "?")))
    if e.get("mint"):
        lines.append("CA: %s" % e["mint"])
    try:
        lines.append("+%.1f%% / 24h · %+.1f%% / 1h" % (
            float(e.get("chg_24h_pct") or 0),
            float(e.get("chg_1h_pct") or 0)))
    except (TypeError, ValueError):
        pass
    lines.append("vol 24h %s · liq %s · mcap %s" % (
        _fmt_mcap(e.get("vol_24h_usd")), _fmt_mcap(e.get("liq_usd")),
        _fmt_mcap(e.get("usd_market_cap"))))
    lines.append("🛡 " + _score_line(e.get("safety_score")))
    plan = _plan_line(e)
    if plan:
        lines.append(plan)
    intel = _intel_line(e)
    if intel:
        lines.append(intel)
    if e.get("entry"):
        lines.append("📥 paper entered @ %s SOL" % e["entry"].get(
            "price_sol", "?"))
    else:
        lines.append("👀 watching — paper entry only if still running "
                     "next scan.")
    lines.append(risk)
    if e.get("url"):
        lines.append(str(e["url"]))
    return "\n".join(lines)


def _digest_item_line(item: Dict[str, Any]) -> str:
    """One digest item -> one compact card line (shared by all tiers)."""
    sym = item.get("symbol") or "?"
    mint = item.get("mint") or ""
    tier = str(item.get("tier", "?")).upper()
    score = item.get("safety_score")
    if item.get("unvetted"):
        # Safety-unknown coins (user decision 2026-09-28): no report
        # exists — the label must say so, never a fake score.
        tag = " [⚠️ UNVETTED — no safety report]"
    else:
        tag = (" [score %s/100]" % score) if score is not None \
            else " [score n/a]"
    tags = [t for t in (item.get("narratives") or []) if t != "other"]
    tag_s = (" 🏷️%s" % ",".join(tags[:3])) if tags else ""
    sent = item.get("sentiment")
    sent_s = (" 💬%s" % sent) if sent else ""
    # Fast-track / deployer markers ride on digest items too — guarded,
    # renders nothing when absent.
    marks = ""
    if item.get("fasttrack") and item.get("smart_money"):
        marks += " ⚡fast-track"
    dl = item.get("deployer_line")
    if dl:
        # deployer_line already carries 🏭 ("🏭 deployer 4/10 hit 2x") —
        # compact it for the one-line digest item.
        marks += " 🏭%s" % str(dl).replace("🏭", "").strip()
    sn = item.get("sniper_score")
    if sn is not None:
        try:
            marks += " 🎯%.0f" % float(sn)
        except (TypeError, ValueError):
            pass
    if mint:
        return ("• %s [%s]%s%s%s%s\n  CA: %s"
                % (sym, tier, tag, tag_s, sent_s, marks, mint))
    return "• %s [%s]%s%s%s%s (no CA)" % (sym, tier, tag, tag_s, sent_s,
                                          marks)


def _launch_early_digest(e: Dict[str, Any]) -> str:
    """Spam-guard digest — instant alerts that arrived over the rate cap,
    collapsed into one card instead of blowing up the user's phone.

    Two tiers: 🛡 Highest safety (top 15 by safety score) then
    🎲 Fresh shots (up to 15 more, most recently queued). The footer is
    honest about coverage — "showing X of Y" with real numbers, never
    implying all queued coins were shown; the rest stays retrievable
    on demand via /digestfull.
    """
    count = e.get("count", 0)
    safe = e.get("items_safe")
    fresh = e.get("items_fresh")
    if safe is None and fresh is None:
        # Legacy pre-tier digest events still in the journal: render the
        # single items list exactly as before.
        return _launch_early_digest_legacy(e)
    lines = [
        "🗞 EARLY DIGEST — %d launches (spam guard)" % count,
    ]
    unvetted_n = int(e.get("unvetted_count") or 0)
    if unvetted_n:
        lines.append(
            "Bundled: rate-capped alerts + ⚠️ %d UNVETTED (no safety "
            "report — browse only, not vetted picks)." % unvetted_n)
    else:
        lines.append("These hit the instant-alert rate cap, so they're "
                     "bundled here.")
    plan = _plan_line(e)
    if plan:
        lines.append(plan)
    shown = 0
    if safe:
        lines.append("🛡 Highest safety:")
        for item in safe[:15]:
            lines.append(_digest_item_line(item))
        shown += len(safe[:15])
    if fresh:
        lines.append("🎲 Fresh shots:")
        for item in fresh[:15]:
            lines.append(_digest_item_line(item))
        shown += len(fresh[:15])
    # HONEST footer: real numbers from what is actually rendered —
    # never imply all queued coins were shown.
    lines.append("showing %d of %d" % (shown, count))
    lines.append("Full waiting list anytime before the next flush: "
                 "/digestfull")
    lines.append("⚠️ All UNCONFIRMED — confirmation still running.")
    return "\n".join(lines)


def _launch_early_digest_legacy(e: Dict[str, Any]) -> str:
    """Pre-tier digest rendering, kept for old journal events."""
    count = e.get("count", 0)
    lines = [
        "🗞 EARLY DIGEST — %d launches (spam guard)" % count,
        "These hit the instant-alert rate cap, so they're bundled here.",
    ]
    plan = _plan_line(e)
    if plan:
        lines.append(plan)
    for item in (e.get("items") or [])[:20]:
        lines.append(_digest_item_line(item))
    if count > 20:
        lines.append("…and %d more (see journal)" % (count - 20))
    lines.append("⚠️ All UNCONFIRMED — confirmation still running.")
    return "\n".join(lines)


def _launch_lowcap(e: Dict[str, Any]) -> str:
    """Low-cap runner card — 70–100% run at low mcap, quality-passed.

    UNCONFIRMED pre-viral window signal: the coin proved a strong run
    from a low base and cleared fast analysis, but full multi-poll
    confirmation is still running. No paper entry at this tier.
    """
    lines = [
        "🎯 LOWCAP RUNNER — %s" % (e.get("symbol", "?") or "?"),
    ]
    name = e.get("name")
    if name:
        lines.append(str(name)[:60])
    lines.append("chain: %s · via %s" % (e.get("chain", "?"),
                                         e.get("source", "?")))
    if e.get("mint"):
        lines.append("CA: %s" % e["mint"])
    try:
        run = float(e.get("run_pct") or 0)
    except (TypeError, ValueError):
        run = 0.0
    lines.append("run +%.0f%% · mcap %s (base %s) · spotted %s min ago" % (
        run, _fmt_mcap(e.get("usd_market_cap")),
        _fmt_mcap(e.get("base_mcap")), e.get("age_minutes", "?")))
    ev = e.get("evidence") or {}
    if ev:
        lines.append("📊 %d rising polls · 5m vol %s · liq %s · "
                     "buys %s / sells %s" % (
                         ev.get("rising_polls", "?"),
                         _fmt_mcap(ev.get("vol_m5")),
                         _fmt_mcap(ev.get("liquidity")),
                         ev.get("buys_m5", "?"), ev.get("sells_m5", "?")))
    fp = (ev.get("fingerprint") or {}) if isinstance(ev, dict) else {}
    if fp and fp.get("verdict"):
        hg = fp.get("holder_growth_pct")
        lines.append("📈 fingerprints: buys %.1fx · vol %.1fx · holders %s · "
                     "score %.0f (%s)%s" % (
                         fp.get("buy_accel") or 0, fp.get("vol_accel") or 0,
                         ("%+.1f%%" % hg) if hg is not None else "n/a",
                         fp.get("score") or 0, fp.get("verdict"),
                         " · counts as 1 rising poll"
                         if ev.get("fp_boost") else ""))
    lines.append("🛡 " + _score_line(e.get("safety_score")))
    wl = _winner_line(e)
    if wl:
        lines.append(wl)
    hl = _holder_line(e)
    if hl:
        lines.append(hl)
    plan = _plan_line(e)
    if plan:
        lines.append(plan)
    intel = _intel_line(e)
    if intel:
        lines.append(intel)
    lines.append("⚠️ UNCONFIRMED — pre-viral window signal, full "
                 "confirmation still running. High risk, size accordingly.")
    if e.get("url"):
        lines.append(str(e["url"]))
    return "\n".join(lines)


def _launch(e: Dict[str, Any]) -> str:
    """Degen runner card — only CONFIRMED runners land here, with the CA
    and the confirmation evidence, whether or not a paper entry fired."""
    tier = str(e.get("tier", "?")).upper()
    lines = [
        "🚀 CONFIRMED RUNNER [%s] — %s" % (tier, e.get("symbol", "?")),
    ]
    name = e.get("name")
    if name:
        lines.append(str(name)[:60])
    lines.append("chain: %s · via %s" % (e.get("chain", "?"),
                                         e.get("source", "?")))
    if e.get("mint"):
        lines.append("CA: %s" % e["mint"])
    lines.append("mcap %s · age %s min" % (
        _fmt_mcap(e.get("usd_market_cap")), e.get("age_minutes", "?")))
    ev = e.get("evidence") or {}
    if ev:
        lines.append("📊 x%s from sight · %d rising polls · 5m vol $%s · "
                     "liq $%s · buys %s / sells %s" % (
                         ev.get("gain_mult", "?"), ev.get("rising_polls", "?"),
                         _fmt_mcap(ev.get("vol_m5")), _fmt_mcap(ev.get("liquidity")),
                         ev.get("buys_m5", "?"), ev.get("sells_m5", "?")))
    upside = e.get("upside")
    if upside:
        lines.append("🎯 %s" % upside)
    saf = e.get("safety")
    # Strict: the safety score is ALWAYS noted — never silently omitted.
    lines.append("🛡 %s" % (saf or "safety scan unavailable (no score)"))
    if e.get("unvetted"):
        lines.append("⚠️ UNVETTED — no safety scan ran on this coin. The "
                     "chart confirmed the run; rug risk is UNKNOWN. "
                     "Trade small or skip.")
    plan = _plan_line(e)
    if plan:
        lines.append(plan)
    intel = _intel_line(e)
    if intel:
        lines.append(intel)
    sm = e.get("smart_money")
    if sm:
        lines.append("🧠 smart money: %s" % sm)
    wl = _winner_line(e)
    if wl:
        lines.append(wl)
    hl = _holder_line(e)
    if hl:
        lines.append(hl)
    entry = e.get("entry") or {}
    if entry:
        lines.append("✅ paper entry: %s SOL @ %.9g SOL (#%s)" % (
            entry.get("volume_sol", "?"), entry.get("price_sol", 0),
            entry.get("ticket", "?")))
        lines.append("SL -%s%% / TP +%s%%" % (
            entry.get("stop_loss_pct", "?"), entry.get("take_profit_pct", "?")))
    note = e.get("note")
    if note:
        lines.append("ℹ️ %s" % note)
    if e.get("url"):
        lines.append(str(e["url"]))
    return "\n".join(lines)


def _trade_executed(e: Dict[str, Any]) -> str:
    return ("✅ TRADE EXECUTED (paper)\n%s %s %s @ %s\nticket #%s  req %s" % (
        e.get("direction", "?"), e.get("volume", "?"), e.get("symbol", "?"),
        e.get("price", "?"), e.get("ticket", "?"), e.get("request_id", "?")))


def _trade_rejected(e: Dict[str, Any]) -> str:
    return ("🚫 TRADE REJECTED\n%s %s %s\nreason: %s" % (
        e.get("direction", "?"), e.get("volume", "?"), e.get("symbol", "?"),
        e.get("reason", e.get("code", "?"))))


def _position_closed(e: Dict[str, Any]) -> str:
    leg = e.get("leg")
    leg_str = " [leg %s]" % leg if leg in ("A", "B") else ""
    return ("📉 POSITION CLOSED%s\n#%s %s %s\nP/L %s  reason: %s" % (
        leg_str, e.get("ticket", "?"), e.get("direction", ""),
        e.get("symbol", ""), _fmt_money(e.get("profit")),
        e.get("reason", "?")))


def _position_external(e: Dict[str, Any]) -> str:
    return ("⚠️ POSITION CLOSED EXTERNALLY\n#%s %s %s\nP/L %s" % (
        e.get("ticket", "?"), e.get("direction", ""), e.get("symbol", ""),
        _fmt_money(e.get("profit"))))


def _risk_blocked(e: Dict[str, Any]) -> str:
    return "⚠️ RISK BLOCKED\n%s" % e.get("reason", "a trade was blocked by risk checks")


def _kill_activated(e: Dict[str, Any]) -> str:
    return "🛑 KILL SWITCH ENGAGED\nsource: %s  reason: %s" % (
        e.get("source", "?"), e.get("reason", "?"))


def _kill_cleared(e: Dict[str, Any]) -> str:
    return "🟢 KILL SWITCH RELEASED\nsource: %s" % e.get("source", "?")


def _daemon_stopped(e: Dict[str, Any]) -> str:
    return "🔴 DAEMON STOPPED: %s\nWatchdog will attempt recovery." % e.get("daemon", "?")


def _daemon_started(e: Dict[str, Any]) -> str:
    src = e.get("source") or ""
    if src in ("watchdog", "recovery"):
        return "🟢 DAEMON RECOVERED by watchdog: %s" % e.get("daemon", "?")
    return "ℹ️ Daemon started: %s" % e.get("daemon", "?")


def _health_degraded(e: Dict[str, Any]) -> str:
    return "⚠️ HEALTH DEGRADED\n%s" % e.get("detail", e.get("reason", "check failed"))


def _health_recovered(e: Dict[str, Any]) -> str:
    return "🟢 HEALTH RECOVERED"


def _broker_down(e: Dict[str, Any]) -> str:
    return "🔴 BROKER DISCONNECTED\n%s" % e.get("detail", "paper broker unreachable")


def _worker_down(e: Dict[str, Any]) -> str:
    return "🔴 WORKER UNAVAILABLE — cloud sync paused"


def _worker_back(e: Dict[str, Any]) -> str:
    return "🟢 WORKER RECONNECTED"


def _degen_warning(e: Dict[str, Any]) -> str:
    """Early-warning lane card — a coin the safety gate HELD that is
    still showing run signals.

    This is NOT a vetted pick: the card prints the real (low) score and
    the exact blocking dangers, plus the insider behavior line when
    tracked. No trade is attached, none will be. The user decides.
    """
    door = str(e.get("door", "?")).upper()
    lines = [
        "⚠️ EARLY WARNING — UNCONFIRMED [%s]" % door,
        "%s · %s" % (e.get("symbol", "?") or "?",
                     e.get("chain", "?") or "?"),
    ]
    if e.get("mint"):
        lines.append("CA: %s" % e["mint"])
    mcap = _fmt_mcap(e.get("usd_market_cap"))
    chg = e.get("chg_24h_pct")
    move = (" · %+g%% 24h" % chg) if isinstance(chg, (int, float)) else ""
    lines.append("mcap %s%s" % (mcap, move))
    score = e.get("safety_score")
    if score is not None:
        lines.append("🛡 score %s/100 — BELOW the safety bar" % score)
    else:
        lines.append("🛡 score unavailable")
    dangers = [str(d) for d in (e.get("dangers") or []) if str(d).strip()]
    if dangers:
        lines.append("⛔ Held because:")
        lines.extend("  · %s" % d[:90] for d in dangers[:4])
    insider_line = e.get("insider_line")
    if insider_line:
        lines.append(str(insider_line)[:120])
    trigger = e.get("trigger")
    if trigger:
        lines.append("signal: %s" % str(trigger).replace("_", " "))
    lines.append("⚠️ NOT VETTED — the gate held this coin. "
                 "High risk. You decide.")
    if e.get("url"):
        lines.append(str(e["url"]))
    return "\n".join(lines)


def _launch_milestone(e: Dict[str, Any]) -> str:
    """Runner milestone — a coin we carded just crossed a multiple.

    The multiple is anchored to OUR card (card-time mcap), so "10x"
    means 10x from when the user was alerted — never from an absolute
    bottom tick nobody could buy.
    """
    mult = e.get("multiple")
    try:
        mult_s = "%.1fx" % float(mult)
    except (TypeError, ValueError):
        mult_s = "?"
    lines = [
        "🏆 MILESTONE — %s just did %s from our call!" % (
            e.get("symbol", "?") or "?", mult_s),
    ]
    name = e.get("name")
    if name:
        lines.append(str(name)[:60])
    lines.append("chain: %s · called via %s" % (e.get("chain", "?"),
                                               e.get("source_event", "?")))
    if e.get("mint"):
        lines.append("CA: %s" % e["mint"])
    lines.append("called @ mcap %s → now %s" % (
        _fmt_mcap(e.get("card_mcap")), _fmt_mcap(e.get("mcap_now"))))
    mins = e.get("minutes_since_card")
    try:
        lines.append("⏱ %s min after the card" % (round(float(mins), 1)))
    except (TypeError, ValueError):
        pass
    lines.append("🛡 " + _score_line(e.get("safety_score")))
    lines.append("Not a new entry signal — the run already happened. "
                 "Chasing vertical candles is how gains evaporate.")
    if e.get("url"):
        lines.append(str(e["url"]))
    return "\n".join(lines)


def _launch_second_wave(e: Dict[str, Any]) -> str:
    """Second-wave re-entry card — a carded coin pulled back hard from
    its peak and is re-accelerating with rising-poll confirmation.

    The full safety gate re-ran at re-entry (score always shown); the
    standard trade plan prints with absolute mcap levels, like every
    other signal card. The wave number journals which re-entry this
    is (wave 1 was the original card).
    """
    wave = e.get("wave_number")
    try:
        wave_s = "wave #%d" % int(wave)
    except (TypeError, ValueError):
        wave_s = "re-entry"
    lines = [
        "🔁 SECOND WAVE — %s (%s)" % (e.get("symbol", "?") or "?", wave_s),
    ]
    name = e.get("name")
    if name:
        lines.append(str(name)[:60])
    lines.append("chain: %s · via %s" % (e.get("chain", "?"),
                                        e.get("source", "?") or "?"))
    if e.get("mint"):
        lines.append("CA: %s" % e["mint"])
    pb = e.get("pullback_pct")
    rg = e.get("relegain_pct")
    pm = e.get("peak_multiple")
    mn = e.get("multiple_now")
    try:
        lines.append("📉 pullback -%.0f%% from %.1fx peak → "
                     "🔄 +%.0f%% from trough (now %.1fx of card)" % (
                         float(pb), float(pm), float(rg), float(mn)))
    except (TypeError, ValueError):
        pass
    lines.append("mcap %s · %s min after the card" % (
        _fmt_mcap(e.get("usd_market_cap")), e.get("minutes_since_card",
                                                  "?")))
    # Strict: the re-check safety score is ALWAYS noted — never omitted.
    saf = e.get("safety")
    lines.append("🛡 %s" % (saf or "safety scan unavailable (no score)"))
    lines.append(_score_line(e.get("safety_score")))
    plan = _plan_line(e)
    if plan:
        lines.append(plan)
    entry = e.get("entry") or {}
    if entry:
        lines.append("✅ paper entry: %s SOL @ %.9g SOL (#%s) [second_wave]" % (
            entry.get("volume_sol", "?"), entry.get("price_sol", 0),
            entry.get("ticket", "?")))
        lines.append("SL -%s%% / TP +%s%%" % (
            entry.get("stop_loss_pct", "?"), entry.get("take_profit_pct", "?")))
    note = e.get("note")
    if note:
        lines.append("ℹ️ %s" % note)
    if e.get("url"):
        lines.append(str(e["url"]))
    return "\n".join(lines)


def _launch_late_runner(e: Dict[str, Any]) -> str:
    """Late-runner card — a quality-passed coin that sat quiet past the
    45-min initial confirmation window, then 2x'd from its card-time
    anchor (the e/acc shape: digest-queued at birth, quiet for hours,
    then running).

    ACTIONABLE, not a celebration: the full safety gate re-ran at
    trigger time (score always shown) and a paper position opened
    labeled late_runner with the standard exit plan. The
    measurement-only milestone 2x celebration is suppressed for this
    coin — this card IS its 2x signal.
    """
    lines = [
        "🐢 LATE RUNNER — %s" % (e.get("symbol", "?") or "?"),
    ]
    name = e.get("name")
    if name:
        lines.append(str(name)[:60])
    lines.append("chain: %s · via %s" % (e.get("chain", "?"),
                                        e.get("source", "?") or "?"))
    if e.get("mint"):
        lines.append("CA: %s" % e["mint"])
    try:
        lines.append("⏳ %.1fh after first card · 📈 +%.0f%% "
                     "(%.1fx of card mcap)" % (
                         float(e.get("hours_since_seen") or 0),
                         float(e.get("gain_pct") or 0),
                         float(e.get("multiple_now") or 0)))
    except (TypeError, ValueError):
        pass
    lines.append("mcap %s" % _fmt_mcap(e.get("usd_market_cap")))
    # Strict: the re-check safety score is ALWAYS noted — never omitted.
    saf = e.get("safety")
    lines.append("🛡 %s" % (saf or "safety scan unavailable (no score)"))
    lines.append(_score_line(e.get("safety_score")))
    plan = _plan_line(e)
    if plan:
        lines.append(plan)
    entry = e.get("entry") or {}
    if entry:
        lines.append("✅ paper entry: %s SOL @ %.9g SOL (#%s) [late_runner]"
                     % (entry.get("volume_sol", "?"),
                        entry.get("price_sol", 0),
                        entry.get("ticket", "?")))
        lines.append("SL -%s%% / TP +%s%%" % (
            entry.get("stop_loss_pct", "?"),
            entry.get("take_profit_pct", "?")))
    note = e.get("note")
    if note:
        lines.append("ℹ️ %s" % note)
    if e.get("url"):
        lines.append(str(e["url"]))
    return "\n".join(lines)


def _runners_recap(e: Dict[str, Any]) -> str:
    """Twice-daily recap — ONLY coins that actually ran. No maybes."""
    slot = e.get("slot") or ""
    lines = ["🏆 RUNNERS RECAP%s" % (" — " + slot if slot else "")]
    lines.append("%d coin%s hit 2x+ since the last recap" % (
        e.get("count", 0), "" if e.get("count", 0) == 1 else "s"))
    for it in (e.get("items") or [])[:20]:
        try:
            peak = float(it.get("peak_mult") or 0.0)
        except (TypeError, ValueError):
            peak = 0.0
        score = it.get("safety_score")
        if score is None:
            safe = "⚠️ unvetted"
        else:
            try:
                safe = "🛡 %d/100" % int(float(score))
            except (TypeError, ValueError):
                safe = "🛡 n/a"
        lines.append("• %s  x%.1f  (%s)  %s\n  CA: %s" % (
            it.get("symbol", "?"), peak, it.get("chain", "?"), safe,
            it.get("mint", "?")))
    overflow = e.get("overflow") or 0
    try:
        overflow = int(overflow)
    except (TypeError, ValueError):
        overflow = 0
    if overflow > 0:
        lines.append("…and %d more runner%s qualified — "
                     "they lead the next recap" % (
                         overflow, "" if overflow == 1 else "s"))
    note = e.get("note")
    if note:
        lines.append("ℹ️ %s" % note)
    return "\n".join(lines)


def _data_stale(e: Dict[str, Any]) -> str:
    return "⚠️ MARKET DATA STALE: %s %s" % (e.get("symbol", ""), e.get("timeframe", ""))


def _tuning_proposals(e: Dict[str, Any]) -> str:
    # Idea #6 feedback loop: the bot proposes, the user decides.
    # Nothing here changes anything — /tune apply <id> is the only
    # path to a config change.
    n = e.get("count", 0)
    lines = ["🎛 Tuning proposals (%d new)" % n,
             "The bot learned from real paper outcomes. Your call:"]
    for it in (e.get("items") or [])[:10]:
        lines.append("• %s: %s → %s\n  %s\n  id: %s" % (
            it.get("knob", "?"), it.get("current", "?"),
            it.get("suggested", "?"),
            (it.get("rationale") or "")[:220],
            it.get("id", "?")))
    insights = [i for i in (e.get("insights") or []) if isinstance(i, dict)]
    if insights:
        lines.append("📚 What the bot learned:")
        for i in insights[:3]:
            mark = "🔥" if i.get("mood") == "hot" else "🧊"
            lines.append("%s %s %s — %s" % (
                mark, i.get("kind", ""), i.get("label", ""),
                i.get("detail", "")))
    lines.append("Run /tune to review, /tune apply <id> to accept, "
                 "/tune dismiss <id> to drop.")
    note = e.get("note")
    if note:
        lines.append("ℹ️ %s" % note)
    return "\n".join(lines)


def _launch_sustained(e: Dict[str, Any]) -> str:
    """Top-tier card (idea #4) — a confirmed runner that is STILL
    holding its move ~30 min after the first alert. SUSTAINED verdict
    only; fading/dead coins never reach this card (logged only)."""
    lines = [
        "🔥 SUSTAINED RUNNER — %s" % (e.get("symbol", "?") or "?"),
    ]
    name = e.get("name")
    if name:
        lines.append(str(name)[:60])
    lines.append("chain: %s" % (e.get("chain", "?")))
    if e.get("mint"):
        lines.append("CA: %s" % e["mint"])
    lines.append("holding %.0f%% of peak (%.1fx) · trend %+.0f%% · "
                 "drawdown %.0f%%" % (
                     e.get("hold_pct") or 0, e.get("peak_mult") or 0,
                     e.get("trend_pct") or 0, e.get("drawdown_pct") or 0))
    lines.append("mcap %s · %s min after the first alert" % (
        _fmt_mcap(e.get("usd_market_cap")),
        e.get("minutes_since_card", "?")))
    plan = _plan_line(e)
    if plan:
        lines.append(plan)
    note = e.get("note")
    if note:
        lines.append("ℹ️ %s" % note)
    if e.get("url"):
        lines.append(str(e["url"]))
    return "\n".join(lines)


def _top_picks(e: Dict[str, Any]) -> str:
    """Daily Top Picks shortlist (user decision 2026-10-01) — the N
    confirmed runners with the best composite score (sniper / winner /
    holder / smart-money / fingerprint) from the last 24h. One card so
    the user gambles from a shortlist, not 200+ alerts. Yesterday's
    picks are graded on the card from the milestone peak tracker."""
    n = e.get("count", 0)
    lines = ["🎯 TOP %d PICKS — %s" % (n, e.get("date", "")),
             "Best composite scores of the last 24h confirmed runners."]
    for i, p in enumerate(e.get("picks") or [], 1):
        if not isinstance(p, dict):
            continue
        lines.append("%d. %s  score %.0f/100  (%s)\n   CA: %s" % (
            i, p.get("symbol", "?"), float(p.get("score") or 0),
            p.get("why", ""), p.get("mint", "?")))
    y = e.get("yesterday") or {}
    if isinstance(y, dict) and y.get("count"):
        lines.append("📊 Yesterday's picks (%s): %d/%d hit 2x+, best x%.1f"
                     % (y.get("date", "?"), y.get("hit_2x", 0),
                        y.get("count", 0), y.get("best_mult", 0)))
    note = e.get("note")
    if note:
        lines.append("ℹ️ %s" % note)
    return "\n".join(lines)


def _discovery_proposals(e: Dict[str, Any]) -> str:
    # Idea #2 wallet auto-discovery: new smart-wallet candidates await
    # review. Proposals only — /smartadd is the only path to the
    # tracked registry; nothing was auto-added.
    n = e.get("count", 0)
    lines = ["🔍 Smart-wallet candidates (%d new)" % n,
             "Mined from early buyers of past 5x+ runners."]
    mined = e.get("runners_mined")
    if mined:
        lines.append("Runners mined: %d" % mined)
    lines.append("Review wallet_discovery_proposals.json, then "
                 "/smartadd <wallet> to track. Nothing was auto-added.")
    note = e.get("note")
    if note:
        lines.append("ℹ️ %s" % note)
    return "\n".join(lines)


def _tuning_weekly_review(e: Dict[str, Any]) -> str:
    """Weekly learner review — what the bot proposed, what you applied,
    whether it helped, and how the week went. Informational only."""
    week = e.get("week") or ""
    lines = ["📊 WEEKLY LEARNER REVIEW — week of %s" % week]
    st = e.get("stats") or {}
    n = int(st.get("n") or 0)
    if n:
        lines.append("How the week went: %d closes · %.0f%% runners · "
                     "avg x%.2f (prev week: %.0f%%)" % (
                         n, 100.0 * float(st.get("runner_rate") or 0),
                         float(st.get("avg_realized") or 0),
                         100.0 * float(st.get("prev_runner_rate") or 0)))
    else:
        lines.append("No closes this week yet — the learner is waiting "
                     "on fresh paper outcomes.")
    new_p = [p for p in (e.get("new_proposals") or [])
             if isinstance(p, dict)]
    total_new = int(e.get("new_proposals_total") or len(new_p))
    if new_p:
        lines.append("🎛 New proposals (%d) — nothing changes until "
                     "you apply:" % total_new)
        for p in new_p:
            lines.append("• [%s] %s: %s → %s\n  id: %s" % (
                p.get("area", "?"), p.get("knob", "?"),
                p.get("current", "?"), p.get("suggested", "?"),
                p.get("id", "?")))
        if total_new > len(new_p):
            lines.append("…and %d more — /tune to see all"
                         % (total_new - len(new_p)))
    open_older = int(e.get("open_older") or 0)
    if open_older:
        lines.append("📚 %d older proposal%s still open — /tune to review"
                     % (open_older, "" if open_older == 1 else "s"))
    applied = [p for p in (e.get("applied") or []) if isinstance(p, dict)]
    if applied:
        lines.append("✅ Applied this week:")
        for p in applied:
            lines.append("• %s %s → %s — grade: %s" % (
                p.get("knob", "?"), p.get("current", "?"),
                p.get("suggested", "?"), p.get("followup", "pending")))
    insights = [i for i in (e.get("insights") or []) if isinstance(i, dict)]
    if insights:
        lines.append("💡 Learned:")
        for i in insights[:3]:
            mark = "🔥" if i.get("mood") == "hot" else "🧊"
            lines.append("%s %s %s — %s" % (
                mark, i.get("kind", ""), i.get("label", ""),
                i.get("detail", "")))
    note = e.get("note")
    if note:
        lines.append("ℹ️ %s" % note)
    return "\n".join(lines)


FORMATTERS: Dict[str, Callable[[Dict[str, Any]], str]] = {
    "signal.detected": _signal,
    "launch.early": _launch_early,
    "launch.early_digest": _launch_early_digest,
    "degen.warning": _degen_warning,
    "launch.lowcap_runner": _launch_lowcap,
    "launch.milestone": _launch_milestone,
    "launch.second_wave": _launch_second_wave,
    "launch.late_runner": _launch_late_runner,
    "momentum.alert": _momentum_alert,
    "launch.detected": _launch,
    "launch.runners_recap": _runners_recap,
    "tuning.proposals": _tuning_proposals,
    "tuning.weekly_review": _tuning_weekly_review,
    "discovery.proposals": _discovery_proposals,
    "launch.sustained": _launch_sustained,
    "launch.top_picks": _top_picks,
    "trade.executed": _trade_executed,
    "trade.rejected": _trade_rejected,
    "position.closed": _position_closed,
    "position.external_close": _position_external,
    "risk.blocked": _risk_blocked,
    "kill_switch.activated": _kill_activated,
    "kill_switch.cleared": _kill_cleared,
    "daemon.stopped": _daemon_stopped,
    "daemon.started": _daemon_started,
    "health.degraded": _health_degraded,
    "health.recovered": _health_recovered,
    "broker.disconnected": _broker_down,
    "worker.unavailable": _worker_down,
    "worker.reconnected": _worker_back,
    "market.data_stale": _data_stale,
}


def format_alert_markup(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Inline keyboard for an alert card, or None.

    Instant and confirmed runner cards get a one-tap "Copy CA" button
    (Bot API copy_text button) so the contract pastes straight into a
    wallet without selecting text."""
    if event.get("event") in ("launch.early", "launch.detected",
                               "launch.lowcap_runner", "momentum.alert",
                               "launch.milestone", "launch.second_wave",
                               "launch.late_runner", "launch.sustained"):
        mint = event.get("mint")
        if mint:
            return {"inline_keyboard": [[{
                "text": "📋 Copy CA",
                "copy_text": {"text": str(mint)},
            }]]}
    return None


def format_alert(event: Dict[str, Any]) -> Optional[str]:
    """Render a journal event as a Telegram message, or None to skip."""
    fmt = FORMATTERS.get(event.get("event", ""))
    if fmt is None:
        return None
    try:
        return fmt(event)
    except Exception:
        return None
