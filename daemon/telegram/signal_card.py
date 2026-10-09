"""Rich memecoin signal cards for Telegram alerts.

Renders a ``signal.detected`` event as a sniper-style token card, using
the live ``token_stats`` payload the signal monitor attaches (price, FDV,
liquidity, volume, 1H buys/sells, pair age, chart link, socials).

Only real data is ever shown: holder distribution and fresh-wallet stats
are not available from our market source, so those rows are omitted
rather than fabricated. When ``token_stats`` is absent (stats fetch
failed), the card falls back to the plain signal format so the alert
still goes out.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def _compact(value: Any) -> str:
    """191000 -> '191K', 2.4e6 -> '2.40M'."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if v < 0:
        return "n/a"
    for bound, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= bound:
            return "%.2f%s" % (v / bound, suffix)
    return "%.0f" % v


def _price(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if v <= 0:
        return "n/a"
    if v >= 1:
        return "$%.4f" % v
    # Small prices: keep significant digits without trailing noise.
    text = "%.10f" % v
    text = text.rstrip("0").rstrip(".")
    return "$" + text


def _pct(value: Any) -> str:
    try:
        return "%+.1f%%" % float(value)
    except (TypeError, ValueError):
        return "n/a"


def _int(value: Any) -> str:
    try:
        return "%d" % int(value)
    except (TypeError, ValueError):
        return "?"


def _signal_plain(e: Dict[str, Any]) -> str:
    return ("📡 SIGNAL %s\n%s %s @ %s\nSL %s  TP %s\nstrategy: %s\nid: %s" % (
        e.get("direction", "?"), e.get("symbol", "?"), e.get("timeframe", ""),
        e.get("entry_price", "?"), e.get("stop_loss", "?"),
        e.get("take_profit", "?"),
        e.get("strategy", "?"), e.get("signal_id", "?")))


def render_signal_card(event: Dict[str, Any]) -> str:
    """Render a signal.detected event as a rich card (or plain fallback)."""
    stats = event.get("token_stats")
    if not isinstance(stats, dict):
        return _signal_plain(event)

    direction = str(event.get("direction", "")).upper()
    arrow = "🔼" if direction == "BUY" else "🔽" if direction == "SELL" else "➡️"
    symbol = stats.get("symbol") or event.get("symbol", "?")
    name = stats.get("name") or symbol

    age_days = stats.get("age_days")
    age = "%dd" % age_days if isinstance(age_days, int) and age_days >= 0 else "?"

    lines = [
        "🅿️ %s [%s] $%s %s" % (name, _compact(stats.get("fdv_usd")),
                               symbol, arrow),
        "🌐 %s @ %s" % (stats.get("chain") or "?", stats.get("dex") or "?"),
        "💰 USD: %s" % _price(stats.get("price_usd")),
        "💎 FDV: %s" % _compact(stats.get("fdv_usd")),
        "💦 Liq: %s" % _compact(stats.get("liquidity_usd")),
        "📊 Vol 24H: %s ⋅ Age: %s" % (_compact(stats.get("volume_h24")), age),
        "📈 1H: %s ⋅ %s 🅑 %s Ⓢ %s" % (
            _compact(stats.get("volume_h1")), _pct(stats.get("change_h1_pct")),
            _int(stats.get("buys_h1")), _int(stats.get("sells_h1"))),
    ]

    socials = stats.get("socials") or []
    if socials:
        kinds = " ⋅ ".join(
            str(s.get("type") or "link") for s in socials[:4]
            if isinstance(s, dict))
        lines.append("🧰 %s" % kinds)
    if stats.get("chart_url"):
        lines.append("💹 Chart: %s" % stats["chart_url"])

    lines += [
        "",
        "📡 %s %s %s @ %s" % (
            direction or "?", event.get("symbol", "?"),
            event.get("timeframe", ""), _price(event.get("entry_price"))),
        "SL %s  TP %s" % (_price(event.get("stop_loss")),
                          _price(event.get("take_profit"))),
        "strategy: %s" % (event.get("strategy", "?")),
    ]
    trigger = event.get("trigger")
    if trigger:
        lines.append("trigger: %s" % trigger)
    lines.append("🆔 %s" % event.get("signal_id", "?"))
    if stats.get("mint"):
        lines.append(stats["mint"])

    return "\n".join(lines)
