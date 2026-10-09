"""Command surface for the Telegram control bot.

Two classes of command:

* **Read-only** — health, signals, positions, account, performance,
  history, kill-switch state. Free to run for the paired chat.
* **Mutating (paper-only)** — trade, close, kill-switch release.
  These park a two-step confirmation; nothing executes without an
  explicit ``/confirm <code>`` from the paired chat.

There is deliberately NO command that can enable live/mainnet trading;
any such request is refused on sight.

Handlers are pure-ish: they take a :class:`HandlerContext` (client,
security, tool functions, bus publisher) so unit tests can inject fakes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .security import SecurityContext
from . import watchlist as _watchlist
from . import filters as _filters

logger = logging.getLogger("forex_agent.daemon.telegram")


# ----------------------------------------------------------------------
# context
# ----------------------------------------------------------------------
class HandlerContext:
    def __init__(self, send: Callable[[Any, str, Optional[Dict[str, Any]]], None],
                 security: SecurityContext,
                 tools: Any,
                 publish: Callable[[Dict[str, Any], Optional[str]], None],
                 audit_log: Optional[List[Dict[str, Any]]] = None,
                 delete_message: Optional[Callable[[Any, Any], None]] = None):
        self.send = send
        self.security = security
        self.tools = tools
        self.publish = publish
        self.audit_log = audit_log if audit_log is not None else []
        # best-effort message deleter (chat_id, message_id) -> None
        self.delete_message = delete_message
        # message_id of the update currently being handled (set per message)
        self.message_id: Any = None


# ----------------------------------------------------------------------
# formatting helpers
# ----------------------------------------------------------------------
def _fmt_money(value: Any) -> str:
    try:
        return "$%.2f" % float(value)
    except (TypeError, ValueError):
        return "n/a"


def _short_signal(sig: Dict[str, Any]) -> str:
    return "%s %s %s %s" % (sig.get("signal_id", "?"), sig.get("direction", "?"),
                            sig.get("symbol", "?"), sig.get("timeframe", ""))


def _ago(ts: Any) -> str:
    """Compact '3m' / '2h' / '45s' age string for a unix timestamp."""
    try:
        s = max(0, int(time.time() - float(ts)))
    except (TypeError, ValueError):
        return "?"
    if s < 90:
        return "%ds" % s
    m = s // 60
    if m < 90:
        return "%dm" % m
    return "%dh" % (m // 60)


def _fnum(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _fmt_minutes(value: Any) -> str:
    """Compact '45s' / '2.1m' duration string for a minutes float."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "?"
    if v < 1.0:
        return "%ds" % int(round(v * 60.0))
    return "%.1fm" % v


HELP_TEXT = (
    "Paper-trading control bot\n\n"
    "Read-only:\n"
    "/status — bot & daemon health\n"
    "/signals [n] — latest signals\n"
    "/signal <id> — signal detail\n"
    "/positions — open paper positions\n"
    "/account — balance / equity\n"
    "/perf [days] — performance\n"
    "/degenstats — degen runner P/L scoreboard\n"
    "/history [symbol] [n] — trade history\n"
    "/kill — kill-switch state\n\n"
    "Paper-only actions (2-step confirm):\n"
    "/trade BUY|SELL SYMBOL VOL SL TP\n"
    "   e.g. /trade BUY EURUSD 0.1 1.0800 1.0950\n"
    "/close <ticket>\n"
    "/closeall\n"
    "/resume — release kill switch (confirm)\n\n"
    "Watchlist:\n"
    "/add <CA> [SYMBOL] — watch a coin by contract address\n"
    "/smartadd <wallet> [label] — track a smart-money wallet\n"
    "/smartremove <wallet> — stop tracking a wallet\n"
    "/smartlist — tracked wallets & hit rates\n"
    "/smartgrade — smart-wallet PnL grades (trailing 30d)\n"
    "/deployers — top deployers by 2x hit rate\n"
    "/skipped — runners the gates blocked + why\n"
    "/shadow — how the blocked coins would have performed\n"
    "/insiders — insider trends + outcomes per direction\n"
    "/latency — detection latency: first sight → first card\n"
    "/heat — which narratives are running hot (learned, read-only)\n"
    "/tune — feedback-loop proposals: list / scan / apply / dismiss (you decide)\n"
    "/review — weekly learner review on demand (proposals, grades, win rate)\n"
    "🎚 personal filters (tighten-only, never loosen the gates):\n"
    "/minscore <0-100>|off — only signal cards at/above your score\n"
    "/lane <lowcap|established|both> — momentum lane filter\n"
    "/quiet <HH-HH>|off — hold signals, one digest after (Lagos time)\n"
    "/filters — show your current filters\n"
    "⚠️ warnings — the early-warning lane sends UNCONFIRMED cards itself\n"
    "(held coins showing run signals; never trades, you decide)\n"
    "/heliuskey <key> — wire in your Helius API key (checks it first)\n\n"
    "Immediate safety:\n"
    "/halt [reason] — engage kill switch now\n\n"
    "Live trading can never be enabled from here."
)


# ----------------------------------------------------------------------
# entry points
# ----------------------------------------------------------------------
def handle_message(ctx: HandlerContext, chat_id: Any, user_id: Any,
                   update_id: int, text: str, message_id: Any = None) -> None:
    """Route one incoming message through auth -> rate limit -> handler."""
    sec = ctx.security
    text = (text or "").strip()
    ctx.message_id = message_id

    # Empty / whitespace-only messages carry no command — drop before any
    # parsing (text.split()[0] would raise IndexError below).
    if not text:
        return

    # --- pairing gate -------------------------------------------------
    if not sec.is_paired():
        first = text.split()[0].lower()
        if first in ("/start", "start"):
            ok, msg = sec.try_pair(chat_id, user_id)
            ctx.send(chat_id, "✅ Paired. This chat is now the control channel.\n\n" + HELP_TEXT
                     if ok else "⛔ " + msg, None)
        else:
            audit = sec.note_unauthorized(chat_id, user_id)
            ctx.publish(audit, "WARNING")
        return

    if not sec.is_authorized(chat_id, user_id):
        ctx.publish(sec.note_unauthorized(chat_id, user_id), "WARNING")
        return

    if not text:
        return

    # --- hard refusal: live/mainnet ------------------------------------
    if SecurityContext.refuses_live(text):
        ctx.send(chat_id, "⛔ Refused. Telegram can never enable live or mainnet trading. "
                          "This bot is paper-only by construction.", None)
        _audit(ctx, chat_id, user_id, update_id, "live_refused", text, False)
        return

    parts = text.split()
    cmd = parts[0].lower().lstrip("/")
    args = parts[1:]

    mutating = cmd in ("trade", "close", "closeall", "resume", "confirm",
                       "halt", "add", "sethelius", "heliuskey")
    if not sec.check_rate(chat_id, mutating=mutating):
        ctx.send(chat_id, "⏳ Slow down — rate limit hit. Try again in a few seconds.", None)
        return

    handler = _COMMANDS.get(cmd)
    if handler is None:
        ctx.send(chat_id, "Unknown command. /help lists everything.", None)
        return
    try:
        handler(ctx, chat_id, user_id, update_id, args)
    except Exception as exc:  # never let a handler crash the loop
        logger.exception("command handler failed: %s", cmd)
        ctx.send(chat_id, "⚠️ Command failed internally. Nothing was changed.", None)


def handle_callback(ctx: HandlerContext, chat_id: Any, user_id: Any,
                    update_id: int, callback_id: str, data: str,
                    answer: Callable[[str, Optional[str]], None]) -> None:
    """Inline-button callbacks: only confirm:<code> / cancel:<code>."""
    sec = ctx.security
    if not sec.is_paired() or not sec.is_authorized(chat_id, user_id):
        ctx.publish(sec.note_unauthorized(chat_id, user_id), "WARNING")
        answer(callback_id, "unauthorized")
        return
    if not sec.check_rate(chat_id, mutating=True):
        answer(callback_id, "rate limited")
        return
    if data.startswith("confirm:"):
        _do_confirm(ctx, chat_id, user_id, update_id, data[len("confirm:"):],
                    answer_cb=lambda t: answer(callback_id, t))
    elif data.startswith("cancel:"):
        intent, status = sec.claim_confirm(data[len("cancel:"):], chat_id, user_id)
        answer(callback_id, "cancelled" if status == "ok" else "already gone (%s)" % status)
    else:
        answer(callback_id, "unknown button")


# ----------------------------------------------------------------------
# audit
# ----------------------------------------------------------------------
def _audit(ctx: HandlerContext, chat_id: Any, user_id: Any, update_id: int,
           action: str, detail: str, ok: bool) -> None:
    entry = {"event": "telegram.command", "chat_id": chat_id, "user_id": user_id,
             "update_id": update_id, "action": action, "detail": detail[:200],
             "ok": ok}
    ctx.audit_log.append(entry)
    try:
        ctx.publish(entry, "NOTICE" if ok else "WARNING")
    except Exception:
        logger.exception("audit publish failed")


def _confirm_keyboard(code: str) -> Dict[str, Any]:
    return {"inline_keyboard": [[
        {"text": "✅ Confirm %s" % code, "callback_data": "confirm:%s" % code},
        {"text": "✖ Cancel", "callback_data": "cancel:%s" % code},
    ]]}


def _ask_confirm(ctx: HandlerContext, chat_id: Any, user_id: Any, update_id: int,
                 action: str, summary: str, payload: Dict[str, Any]) -> None:
    req_id = SecurityContext.request_id(update_id, action,
                                        str(sorted(payload.items())))
    code = ctx.security.new_confirm(action, payload, chat_id, user_id, req_id)
    ctx.send(chat_id,
             "⚠️ Confirm %s (paper only):\n%s\n\n"
             "Reply /confirm %s or tap below. Expires in 3 minutes." % (action, summary, code),
             _confirm_keyboard(code))
    _audit(ctx, chat_id, user_id, update_id, action + ".proposed", summary, True)


# ----------------------------------------------------------------------
# read-only commands
# ----------------------------------------------------------------------
def _cmd_start(ctx, chat_id, user_id, update_id, args):
    ok, msg = ctx.security.try_pair(chat_id, user_id)
    ctx.send(chat_id, ("✅ This chat is already the paired control channel." if not ok and ctx.security.is_paired()
                       else ("✅ Paired. " if ok else "⛔ " + msg) + "\n\n" + HELP_TEXT), None)


def _cmd_help(ctx, chat_id, user_id, update_id, args):
    ctx.send(chat_id, HELP_TEXT, None)


def _cmd_status(ctx, chat_id, user_id, update_id, args):
    try:
        health = ctx.tools.forex_get_health()
    except Exception as exc:
        ctx.send(chat_id, "⚠️ health read failed: %s" % exc, None)
        return
    lines = ["🟢 Status"]
    for key in ("mode", "kill_switch", "daemons", "queue", "storage"):
        if key in health:
            lines.append("%s: %s" % (key, health[key]))
    if "checks" in health and isinstance(health["checks"], dict):
        bad = [k for k, v in health["checks"].items() if not v]
        lines.append("checks failing: %s" % (", ".join(bad) if bad else "none"))
    ctx.send(chat_id, "\n".join(lines), None)


def _cmd_signals(ctx, chat_id, user_id, update_id, args):
    try:
        n = int(args[0]) if args else 5
    except ValueError:
        ctx.send(chat_id, "Usage: /signals [n]", None)
        return
    try:
        res = ctx.tools.forex_get_signals(limit=max(1, min(n, 20)))
    except Exception as exc:
        ctx.send(chat_id, "⚠️ signals read failed: %s" % exc, None)
        return
    sigs = res.get("signals", res) if isinstance(res, dict) else res
    if not sigs:
        ctx.send(chat_id, "No recent signals.", None)
        return
    ctx.send(chat_id, "📡 Latest signals:\n" + "\n".join(
        "• " + _short_signal(s) for s in sigs[:n]), None)


def _cmd_signal(ctx, chat_id, user_id, update_id, args):
    if not args:
        ctx.send(chat_id, "Usage: /signal <signal_id>", None)
        return
    try:
        res = ctx.tools.forex_get_signal(args[0])
    except Exception as exc:
        ctx.send(chat_id, "⚠️ signal read failed: %s" % exc, None)
        return
    sig = res.get("signal", res) if isinstance(res, dict) else res
    ctx.send(chat_id, "📡 Signal %s\n" % args[0] + "\n".join(
        "%s: %s" % (k, v) for k, v in sig.items()) if isinstance(sig, dict)
        else str(sig), None)


def _cmd_positions(ctx, chat_id, user_id, update_id, args):
    try:
        res = ctx.tools.forex_get_positions()
    except Exception as exc:
        ctx.send(chat_id, "⚠️ positions read failed: %s" % exc, None)
        return
    positions = res.get("positions", []) if isinstance(res, dict) else []
    if not positions:
        ctx.send(chat_id, "No open positions.", None)
        return
    lines = ["📊 Open positions (%d):" % len(positions)]
    for p in positions:
        lines.append("• #%s %s %s %s @ %s  P/L %s" % (
            p.get("ticket"), p.get("direction"), p.get("volume"),
            p.get("symbol"), p.get("open_price"), _fmt_money(p.get("profit"))))
    ctx.send(chat_id, "\n".join(lines), None)


def _cmd_account(ctx, chat_id, user_id, update_id, args):
    try:
        res = ctx.tools.forex_get_account()
    except Exception as exc:
        ctx.send(chat_id, "⚠️ account read failed: %s" % exc, None)
        return
    acct = res.get("account", res) if isinstance(res, dict) else {}
    ctx.send(chat_id, "💰 Account\nBalance: %s\nEquity: %s\nMargin: %s" % (
        _fmt_money(acct.get("balance")), _fmt_money(acct.get("equity")),
        _fmt_money(acct.get("margin"))), None)


def _cmd_perf(ctx, chat_id, user_id, update_id, args):
    try:
        days = int(args[0]) if args else 30
    except ValueError:
        ctx.send(chat_id, "Usage: /perf [days]", None)
        return
    try:
        res = ctx.tools.forex_get_performance(lookback_days=days)
    except Exception as exc:
        ctx.send(chat_id, "⚠️ performance read failed: %s" % exc, None)
        return
    ctx.send(chat_id, "📈 Performance (%dd): %s" % (days, res), None)


def _cmd_smartadd(ctx, chat_id, user_id, update_id, args):
    """Track a smart-money wallet.

    /smartadd <wallet> [label]

    The wallet joins the Helius watch set; when it buys a token we're
    tracking, that candidate confirms faster and gets a 🧠 line.
    """
    if not args:
        ctx.send(chat_id,
                 "Usage: /smartadd <wallet-address> [label]\n"
                 "e.g. /smartadd 7xKX...9abc sniper-1", None)
        return
    try:
        from daemon.launch_scanner import smartmoney
    except Exception as exc:
        ctx.send(chat_id, "⚠️ smart-money module unavailable: %s" % exc, None)
        return
    ok, msg = smartmoney.add_wallet(
        args[0], " ".join(args[1:]) if len(args) > 1 else "")
    extra = ""
    if ok and not smartmoney.api_key():
        extra = ("\n\n⚠️ Helius key not set yet — the wallet is saved but "
                 "polling stays idle until the key is wired in.")
    ctx.send(chat_id, ("✅ " if ok else "⛔ ") + msg + extra, None)
    _audit(ctx, chat_id, user_id, update_id, "smartadd",
           args[0][:16], ok)


def _cmd_smartremove(ctx, chat_id, user_id, update_id, args):
    """/smartremove <wallet> — stop tracking a wallet."""
    if not args:
        ctx.send(chat_id, "Usage: /smartremove <wallet-address>", None)
        return
    try:
        from daemon.launch_scanner import smartmoney
    except Exception as exc:
        ctx.send(chat_id, "⚠️ smart-money module unavailable: %s" % exc, None)
        return
    ok, msg = smartmoney.remove_wallet(args[0])
    ctx.send(chat_id, ("✅ " if ok else "⛔ ") + msg, None)
    _audit(ctx, chat_id, user_id, update_id, "smartremove",
           args[0][:16], ok)


def _helius_key_file() -> Path:
    home = Path(os.environ.get("FOREX_AGENT_HOME", Path.home() / ".forex-agent"))
    return home / "helius" / "api_key"


def _write_helius_key(key: str) -> None:
    """Atomically write the key with 0600 perms; never logs the key."""
    path = _helius_key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(key.strip() + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    # reset this process's cached key so verification + polling use it
    try:
        from daemon.launch_scanner import smartmoney
        smartmoney._key_cache = key.strip()
        smartmoney._key_checked = True
    except Exception:
        logger.warning("sethelius: smartmoney cache reset failed", exc_info=True)


def _verify_helius_key(key: str, timeout: float = 15.0) -> Tuple[bool, str]:
    """Cheap getSlot through the key. Error strings never carry the key."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": "getSlot"}).encode("utf-8")
    req = urllib.request.Request(
        "https://mainnet.helius-rpc.com/?api-key=" + urllib.parse.quote(key),
        data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Connection": "close"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        # Surface the status: 401 = bad/inactive key, 429 = throttled.
        return False, "HTTP %s from Helius" % exc.code
    except Exception as exc:  # never str(exc): it may embed the URL
        return False, "request failed (%s)" % type(exc).__name__
    if isinstance(payload, dict) and "result" in payload:
        return True, str(payload["result"])
    err = payload.get("error", {}) if isinstance(payload, dict) else {}
    msg = err.get("message", "unexpected response") if isinstance(err, dict) else "unexpected response"
    return False, str(msg)[:120]


def _cmd_sethelius(ctx, chat_id, user_id, update_id, args):
    """/sethelius <key> — wire in the Helius API key.

    The key goes straight into the daemon's 0600 key file, is verified
    with a cheap getSlot call, and the user's message is scrubbed.
    The key is never echoed, logged, or journaled anywhere.
    """
    if not args or not args[0].strip():
        ctx.send(chat_id,
                 "Usage: /sethelius <your-helius-api-key>\n"
                 "Paste only the key itself, nothing else.", None)
        return
    key = args[0].strip()
    if len(key) < 16 or any(ch.isspace() for ch in key):
        ctx.send(chat_id,
                 "⛔ That doesn't look like an API key — "
                 "paste only the key itself.", None)
        _audit(ctx, chat_id, user_id, update_id, "sethelius",
               "rejected: malformed", False)
        return
    try:
        _write_helius_key(key)
    except Exception as exc:
        logger.exception("sethelius: key file write failed")
        ctx.send(chat_id, "⚠️ Couldn't save the key: %s" % exc, None)
        _audit(ctx, chat_id, user_id, update_id, "sethelius",
               "write failed", False)
        return
    ok, detail = _verify_helius_key(key)
    if ok:
        ctx.send(chat_id,
                 "✅ Helius key saved and verified (chain slot %s).\n"
                 "Fast launch detection can now use it — just say the word "
                 "in the main chat and I'll restart the scanner to pick it up."
                 % detail, None)
    else:
        ctx.send(chat_id,
                 "⚠️ Key saved, but verification failed: %s\n"
                 "Check the key on helius.dev and run /sethelius again."
                 % detail, None)
    _audit(ctx, chat_id, user_id, update_id, "sethelius", "saved", ok)
    # scrub the command message so the key doesn't sit in chat history
    if ctx.delete_message and ctx.message_id:
        try:
            ctx.delete_message(chat_id, ctx.message_id)
        except Exception:
            logger.warning("sethelius: message scrub failed", exc_info=True)
            ctx.send(chat_id,
                     "ℹ️ I couldn't auto-delete your key message — "
                     "please delete it manually.", None)


# ----------------------------------------------------------------------
# /heliuskey — Helius key delivery, done properly this time
# ----------------------------------------------------------------------
# The old /sethelius saved whatever was pasted (it once stored a 46-char
# non-key) and only then discovered Helius rejected it. This route is
# stricter: the paste must LOOK like a Helius key (UUID shape) and must
# PASS a live Helius check BEFORE anything is written to the key file.
_HELIUS_KEY_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _diagnose_helius_paste(raw: str) -> str:
    """Tell the user exactly what is wrong with what they pasted."""
    s = (raw or "").strip()
    if not s:
        return "you sent nothing"
    low = s.lower()
    if low.startswith("http") or "helius" in low or "/" in s:
        return ("that looks like a link or page text, not a key — copy "
                "only the API key value itself from your Helius dashboard")
    if len(s) != 36:
        return ("that's %d characters long — a Helius API key is 36 "
                "characters like 123e4567-e89b-12d3-a456-426614174000. "
                "You may have pasted a project ID or a different secret"
                % len(s))
    return ("it isn't key-shaped — a Helius key looks like "
            "123e4567-e89b-12d3-a456-426614174000")


def _cmd_heliuskey(ctx, chat_id, user_id, update_id, args):
    """/heliuskey <key> — wire in the Helius API key, validated first.

    Unlike the old route: (1) the paste must match a Helius key's shape
    before anything happens; (2) the key is tested live against Helius
    BEFORE it is saved, so a bad paste can never land in the key file.
    The key is never echoed, logged, or journaled, and the user's
    message is scrubbed afterwards.
    """
    if not args or not args[0].strip():
        ctx.send(chat_id,
                 "🔑 To wire in your Helius key:\n\n"
                 "1. Sign up FREE at dashboard.helius.dev — the Free plan "
                 "costs $0 (no card, no funding needed) and is plenty for "
                 "this bot.\n"
                 "2. Create an API key there and copy just the key "
                 "(36 characters, like "
                 "123e4567-e89b-12d3-a456-426614174000).\n"
                 "3. Send: /heliuskey <paste-the-key-here>\n\n"
                 "I'll test the key with Helius before saving anything — "
                 "a wrong paste can't break anything.", None)
        return
    raw = args[0].strip()
    if not _HELIUS_KEY_RE.match(raw):
        ctx.send(chat_id,
                 "⛔ That doesn't look like a Helius API key — %s.\n"
                 "Nothing was saved. Copy the key itself from your Helius "
                 "dashboard and try again."
                 % _diagnose_helius_paste(raw), None)
        _audit(ctx, chat_id, user_id, update_id, "heliuskey",
               "rejected: malformed", False)
        return
    # Verify BEFORE saving: a rejected key must never reach the key file.
    ok, detail = _verify_helius_key(raw)
    if not ok:
        if "401" in detail:
            hint = ("Helius doesn't recognize it — make sure you copied "
                    "the API key itself (not the project ID), and that "
                    "the project is active on your dashboard.")
        elif "429" in detail:
            hint = "Helius throttled the check — wait a minute and retry."
        else:
            hint = "Helius check problem: %s. Check your connection " \
                   "and retry." % detail
        ctx.send(chat_id,
                 "⚠️ I tested that key with Helius and it didn't pass: %s\n"
                 "Nothing was saved — fix the key and run /heliuskey again."
                 % hint, None)
        _audit(ctx, chat_id, user_id, update_id, "heliuskey",
               "rejected: verification failed", False)
        return
    try:
        _write_helius_key(raw)
    except Exception as exc:
        logger.exception("heliuskey: key file write failed")
        ctx.send(chat_id, "⚠️ Key is valid but I couldn't save it: %s"
                 % exc, None)
        _audit(ctx, chat_id, user_id, update_id, "heliuskey",
               "write failed", False)
        return
    ctx.send(chat_id,
             "✅ Helius key tested and saved (chain slot %s).\n"
             "Smart-money tracking and fast launch detection can use it "
             "now — say the word in the main chat and I'll restart the "
             "scanner to pick it up." % detail, None)
    _audit(ctx, chat_id, user_id, update_id, "heliuskey", "saved", True)
    # scrub the command message so the key doesn't sit in chat history
    if ctx.delete_message and ctx.message_id:
        try:
            ctx.delete_message(chat_id, ctx.message_id)
        except Exception:
            logger.warning("heliuskey: message scrub failed", exc_info=True)
            ctx.send(chat_id,
                     "ℹ️ I couldn't auto-delete your key message — "
                     "please delete it manually.", None)


def _cmd_smartlist(ctx, chat_id, user_id, update_id, args):
    """/smartlist — tracked wallets and their hit rates."""
    try:
        from daemon.launch_scanner import smartmoney
    except Exception as exc:
        ctx.send(chat_id, "⚠️ smart-money module unavailable: %s" % exc, None)
        return
    wallets = smartmoney.load_registry()
    if not wallets:
        ctx.send(chat_id,
                 "No smart wallets tracked yet.\n"
                 "Add one: /smartadd <wallet-address> [label]", None)
        return
    lines = ["🧠 Smart wallets (%d):" % len(wallets)]
    for w, info in wallets.items():
        label = info.get("label") or ""
        picks = int(info.get("picks") or 0)
        score = smartmoney.wallet_score(info)
        stat = ("%d picks · %.0f%% win" % (picks, score)) if picks else "no scored picks yet"
        lines.append("• %s… %s — %s" % (w[:10], label, stat))
    if not smartmoney.api_key():
        lines.append("\n⚠️ Helius key not set — polling idle.")
    ctx.send(chat_id, "\n".join(lines), None)


def _cmd_smartgrade(ctx, chat_id, user_id, update_id, args):
    """/smartgrade — smart-wallet PnL grades (trailing 30d).

    Read-only: shows per-wallet 2x rates and peak multiples of our card
    anchor. Defined in daemon/telegram/commands_smart.py; registered
    here with a thin wrapper.
    """
    from daemon.telegram import commands_smart
    ctx.send(chat_id, commands_smart.cmd_smartgrade(ctx, args), None)


def _cmd_deployers(ctx, chat_id, user_id, update_id, args):
    """/deployers — top deployers by 2x hit rate.

    Read-only: the deployer-history leaderboard. Defined in
    daemon/telegram/commands_deployers.py; the adapter there matches
    this daemon's handler convention.
    """
    from daemon.telegram import commands_deployers
    commands_deployers.cmd_deployers_handler(
        ctx, chat_id, user_id, update_id, args)


def _cmd_skipped(ctx, chat_id, user_id, update_id, args):
    """/skipped — runners the gates blocked, with the reason.

    Read-only digest of the launch scanner's skipped-runners buffer:
    every coin held by the quality/safety gates, most significant
    first. Nothing here was alerted or traded.
    """
    try:
        from daemon.common import get_daemon_state, get_store
        state = get_daemon_state(get_store(), "launch_scanner") or {}
    except Exception as exc:
        ctx.send(chat_id, "⚠️ Couldn't read scanner state: %s" % exc, None)
        return
    skipped = [r for r in (state.get("skipped_runners") or [])
               if isinstance(r, dict)]
    if not skipped:
        ctx.send(chat_id,
                 "🧊 No skipped runners recorded yet.\n"
                 "Coins the safety/momentum gates hold will appear here "
                 "with the exact reason.", None)
        return

    # Most significant first: biggest 24h move, then most-held.
    top = sorted(
        skipped,
        key=lambda r: (float(r.get("chg_24h_pct") or 0),
                       int(r.get("hits") or 1)),
        reverse=True)[:12]
    lines = ["🧊 Skipped runners (%d tracked, 7d):" % len(skipped)]
    for r in top:
        sym = r.get("symbol") or "?"
        kind = r.get("kind") or "?"
        bits = []
        chg = r.get("chg_24h_pct")
        if chg:
            bits.append("+%g%%/24h" % float(chg))
        score = r.get("safety_score")
        if score is not None:
            bits.append("score %s" % score)
        tier = r.get("tier") or r.get("lane")
        if tier:
            bits.append(str(tier))
        hits = int(r.get("hits") or 1)
        head = "• %s [%s]%s" % (sym, kind,
                                (" " + " ".join(bits)) if bits else "")
        reason = str(r.get("reason") or "held")[:90]
        tail = "⛔ %s%s — %s ago" % (
            reason, (" (×%d)" % hits) if hits > 1 else "",
            _ago(float(r.get("ts") or 0)))
        lines.append(head + "\n  " + tail)
    text = "\n".join(lines)
    ctx.send(chat_id, text[:3900], None)


def _cmd_shadow(ctx, chat_id, user_id, update_id, args):
    """/shadow — how the blocked coins would have performed.

    Read-only report on the shadow ledger: every gate-blocked runner is
    paper-simulated from its hold-time market cap with stop-loss /
    take-profit / timeout exits. Measurement only — never real trades,
    never alerts.
    """
    try:
        from daemon.common import get_daemon_state, get_store
        from daemon.launch_scanner import shadow
        state = get_daemon_state(get_store(), "launch_scanner") or {}
    except Exception as exc:
        ctx.send(chat_id, "⚠️ Couldn't read scanner state: %s" % exc, None)
        return
    summ = shadow.summary(state)
    if not summ["open"] and not summ["closed"] and not summ.get("pending"):
        ctx.send(chat_id,
                 "🔬 Shadow ledger is empty.\n"
                 "Blocked runners will be paper-simulated here with "
                 "stop-loss / take-profit exits.", None)
        return
    lines = ["🔬 Shadow — what loosening the gates would have done:"]
    head = "Open %d · Closed %d · Win rate %.0f%% · Avg %+.1f%%" % (
        summ["open"], summ["closed"], summ["win_rate"], summ["avg_pnl"])
    if summ.get("pending"):
        head += " · %d awaiting first price ⏳" % summ["pending"]
    lines.append(head)
    open_book = state.get("shadow_open") or {}

    def _pnl(pos):
        entry = _fnum(pos.get("entry_mcap"))
        cur = _fnum(pos.get("last_mcap"))
        return (cur - entry) / entry * 100.0 if entry > 0 else 0.0

    if isinstance(open_book, dict) and open_book:
        lines.append("Open:")
        for _mint, pos in sorted(open_book.items(), key=lambda kv: _pnl(kv[1]),
                                reverse=True)[:8]:
            if not isinstance(pos, dict):
                continue
            if pos.get("pending"):
                lines.append("• %s [%s] ⏳ awaiting first price — %s open" % (
                    pos.get("symbol") or "?", pos.get("kind") or "?",
                    _ago(pos.get("opened_ts"))))
                continue
            lines.append("• %s [%s] %+.0f%% — %s open" % (
                pos.get("symbol") or "?", pos.get("kind") or "?",
                _pnl(pos), _ago(pos.get("opened_ts"))))
    closed = [c for c in (state.get("shadow_closed") or [])
              if isinstance(c, dict)]
    if closed:
        lines.append("Recent closed:")
        for c in closed[-6:][::-1]:
            mark = "✅" if c.get("outcome") == "win" else (
                "❌" if c.get("outcome") == "loss" else "⏱")
            lines.append("%s %s %+.0f%% (%s)" % (
                mark, c.get("symbol") or "?", _fnum(c.get("pnl_pct")),
                c.get("outcome") or "?"))
    lines.append("Paper simulation of blocked coins — not real trades.")
    ctx.send(chat_id, "\n".join(lines)[:3900], None)


def _cmd_insiders(ctx, chat_id, user_id, update_id, args):
    """/insiders — insider behavior tracker (Phase 1: measurement only).

    Read-only. Shows the coins currently being insider-tracked with their
    trend (diluting / accumulating / steady), plus the outcome ledger:
    how shadow-simulated closes performed per insider direction. This is
    evidence for humans — it never changes any gate.
    """
    try:
        from daemon.common import get_daemon_state, get_store
        from daemon.launch_scanner import insiders
        state = get_daemon_state(get_store(), "launch_scanner") or {}
    except Exception as exc:
        ctx.send(chat_id, "⚠️ Couldn't read scanner state: %s" % exc, None)
        return
    watch = state.get("insider_watch")
    if not isinstance(watch, dict):
        watch = {}
    summ = insiders.outcome_summary(state)
    if not watch and not summ.get("total"):
        ctx.send(chat_id,
                 "🧠 Insider tracker is watching no coins yet.\n"
                 "It tracks the insider footprint of held runners and "
                 "joins it with shadow outcomes — check back after some "
                 "warnings fire.", None)
        return
    lines = ["🧠 Insider tracker — measurement only, gates unchanged:"]
    if watch:
        lines.append("Watching %d:" % len(watch))
        shown = 0
        for mint, w in sorted(
                watch.items(),
                key=lambda kv: (kv[1].get("last_ts") or 0)
                if isinstance(kv[1], dict) else 0, reverse=True):
            if not isinstance(w, dict) or shown >= 10:
                continue
            t = insiders.trend(state, mint) or {}
            d = t.get("direction") or "steady"
            mark = "📈" if d == "accumulating" else (
                "📉" if d == "diluting" else "➖")
            lines.append("%s %s [%s]: %.0f%% → %.0f%% (%s)" % (
                mark, w.get("symbol") or "?", w.get("chain") or "?",
                w.get("first_cluster_pct") or 0.0,
                w.get("last_cluster_pct") or 0.0, d))
            shown += 1
    by_dir = summ.get("by_direction") or {}
    if summ.get("total"):
        lines.append("Outcomes vs insider direction "
                     "(%d shadow closes):" % summ["total"])
        for d in ("accumulating", "diluting", "steady"):
            s = by_dir.get(d) or {}
            if not s.get("n"):
                continue
            lines.append("• %s: %d closed · %.0f%% win · %+.1f%% avg" % (
                d, s["n"], s["win_rate"], s["avg_pnl"]))
        lines.append("Do accumulating-insider coins beat diluting ones? "
                     "That's the question this ledger answers.")
    ctx.send(chat_id, "\n".join(lines)[:3900], None)


def _cmd_latency(ctx, chat_id, user_id, update_id, args):
    """/latency — entry timing: first sighting → first card (measurement only).

    Read-only. Median + p90 detection latency over a rolling window of the
    last N carded coins, plus the 3 slowest recent examples with coin +
    latency. Never trades, never gates.
    """
    try:
        from daemon.common import get_daemon_state, get_store
        from daemon.launch_scanner import latency
        state = get_daemon_state(get_store(), "launch_scanner") or {}
    except Exception as exc:
        ctx.send(chat_id, "⚠️ Couldn't read scanner state: %s" % exc, None)
        return
    summ = latency.summary(state)
    if not summ["n"] and not summ["unknown"]:
        ctx.send(chat_id,
                 "⏱ No carded coins tracked yet.\n"
                 "Latency is measured from a coin's first sighting to its "
                 "first card (instant or digest).", None)
        return
    lines = ["⏱ Entry timing — first sighting → first card"]
    if summ["n"]:
        lines.append("last %d carded: median %s · p90 %s" % (
            summ["n"], _fmt_minutes(summ["median_min"]),
            _fmt_minutes(summ["p90_min"])))
    if summ["unknown"]:
        lines.append("%d carded with unknown first sighting "
                     "(seen before tracking started)" % summ["unknown"])
    if summ["slowest"]:
        lines.append("Slowest recent:")
        for s in summ["slowest"]:
            lines.append("• %s +%s (%s)" % (
                s.get("symbol") or "?", _fmt_minutes(s.get("latency_min")),
                s.get("source") or "?"))
    lines.append("Measurement only — never trades, never gates.")
    ctx.send(chat_id, "\n".join(lines)[:3900], None)


def _cmd_heat(ctx, chat_id, user_id, update_id, args):
    """/heat — narrative heat leaderboard (read-only).

    Which coin themes are actually running hot, learned from real
    closed paper outcomes in the last 24h. Hot narratives earn a
    sniper-score boost (idea #5); cold ones never penalize.
    """
    try:
        from daemon.launch_scanner import narratives
        tr = narratives.NarrativeHeat.load(narratives.default_heat_path())
        rows = tr.leaderboard(time.time())
    except Exception as exc:
        ctx.send(chat_id, "⚠️ Couldn't read narrative heat: %s" % exc, None)
        return
    if not rows:
        ctx.send(chat_id,
                 "🔥 No narrative has enough closed outcomes yet.\n"
                 "A theme needs 3+ closed paper positions in the last "
                 "24h before its heat is trusted.", None)
        return
    lines = ["🔥 Narrative heat — last 24h, from real outcomes"]
    for r in rows[:12]:
        heat = float(r.get("heat") or 1.0)
        icon = "🔥" if heat > 1.2 else ("🌡️" if heat > 1.0 else "🧊")
        lines.append("%s %s  x%.2f  (%d closed · %d runners · %.0f%% hit)"
                     % (icon, r.get("tag", "?"), heat,
                        r.get("closed", 0), r.get("runners", 0),
                        float(r.get("runner_rate") or 0) * 100))
    lines.append("Hot themes boost the sniper score (#5). "
                 "Cold themes never block a coin.")
    ctx.send(chat_id, "\n".join(lines)[:3900], None)


def _cmd_tune(ctx, chat_id, user_id, update_id, args):
    """/tune — the feedback loop's proposals (idea #6).

    /tune               list open proposals + what the bot learned
    /tune scan          run the outcome analysis now
    /tune apply <id>    accept a proposal (YOUR approval — the only
                        path to a config change)
    /tune dismiss <id>  drop a proposal

    The bot proposes; you decide. Three learners study real paper
    outcomes: entries (which signals predicted runners), exits
    (trailing / take-profit / hold / stop), and ranking. Safety knobs
    (your 1/100 bar, insider/bundle gates, hard fails) can never be
    proposed and are refused even if asked for. Applied proposals are
    graded later — /tune shows whether each one actually helped.
    """
    try:
        from daemon.launch_scanner import feedback
    except Exception as exc:
        ctx.send(chat_id, "\u26a0\ufe0f Feedback module unavailable: %s" % exc, None)
        return

    def _list() -> str:
        open_p = [p for p in feedback.load_proposals()
                  if p.get("status") == "open"]
        lines = []
        if not open_p:
            lines.append("\U0001f39b No open tuning proposals.\n"
                         "The bot analyzes real paper outcomes once a day; "
                         "new proposals arrive as cards. /tune scan runs it now.")
        else:
            lines.append("\U0001f39b Open tuning proposals — nothing changes "
                         "until you apply one:")
            for p in open_p:
                area = p.get("area") or "ranking"
                lines.append("\u2022 [%s] %s: %s \u2192 %s\n  %s\n  id: %s" % (
                    area, p.get("knob", "?"), p.get("current", "?"),
                    p.get("suggested", "?"),
                    (p.get("rationale") or "")[:240],
                    p.get("id", "?")))
            lines.append("Apply with /tune apply <id> \u00b7 drop with "
                         "/tune dismiss <id>.")
        ins = feedback.load_insights()[:5]
        if ins:
            lines.append("\U0001f4da What the bot learned (informational — "
                         "no changes):")
            for i in ins:
                mark = "\U0001f525" if i.get("mood") == "hot" else "\U0001f9ca"
                lines.append("%s %s %s — %s" % (
                    mark, i.get("kind", ""), i.get("label", ""),
                    i.get("detail", "")))
        hist = [p for p in feedback.load_proposals()
                if p.get("status") not in ("open",)]
        hist = hist[-5:]
        if hist:
            lines.append("\U0001f558 Recent decisions:")
            for p in reversed(hist):
                extra = ""
                if p.get("status") == "applied":
                    fu = p.get("followup") or "pending"
                    extra = " — did it help? %s" % fu
                lines.append("\u2022 %s %s \u2192 %s: %s%s" % (
                    p.get("knob", "?"), p.get("current", "?"),
                    p.get("suggested", "?"), p.get("status", "?"), extra))
        return "\n".join(lines)

    sub = (args[0].lower() if args else "")
    if sub == "scan":
        try:
            from config.config import load_config
            cfg = load_config()
            new, _insights = feedback.run_scan(cfg.degen, time.time())
        except Exception as exc:
            ctx.send(chat_id, "\u26a0\ufe0f Tune scan failed: %s" % exc, None)
            return
        if new:
            ctx.send(chat_id,
                     ("\U0001f39b Scan complete — %d new proposal(s):\n%s"
                      % (len(new), _list()))[:3900], None)
        else:
            ctx.send(chat_id,
                     "\U0001f39b Scan complete — no new proposals. %s"
                     % ("Not enough fresh outcomes yet."
                        if not _list().startswith("\U0001f39b Open") else
                        "Existing proposals unchanged."), None)
        return
    if sub in ("apply", "dismiss") and len(args) > 1:
        pid = str(args[1]).upper()
        open_p = [p for p in feedback.load_proposals()
                  if p.get("status") == "open"]
        hit = next((p for p in open_p
                    if str(p.get("id") or "").upper() == pid), None)
        if not hit:
            ctx.send(chat_id,
                     ("\u26a0\ufe0f No open proposal with id %s.\n%s"
                      % (args[1], _list()))[:3900], None)
            return
        knob = str(hit.get("knob") or "")
        if knob in feedback.SAFETY_NEVER_TUNE:
            ctx.send(chat_id,
                     "\U0001f6e1 Refused: %s is a safety knob and can never be "
                     "tuned." % knob, None)
            return
        if sub == "dismiss":
            feedback.mark_proposal(hit["id"], "dismissed")
            ctx.send(chat_id, "\U0001f5d1 Proposal %s dismissed." % hit["id"], None)
            return
        # apply — the user's explicit approval, the ONLY write path.
        # The override write must succeed before anything is marked.
        try:
            feedback.apply_override(knob, hit.get("suggested"))
        except ValueError as exc:
            ctx.send(chat_id, "\U0001f6e1 Refused: %s" % exc, None)
            return
        except Exception as exc:
            ctx.send(chat_id, "\u26a0\ufe0f Apply failed: %s" % exc, None)
            return
        # Snapshot today's win rate so the bot can later grade whether
        # this change actually helped.
        try:
            from config.config import load_config
            _cfg = load_config()
            _recs = feedback.load_outcomes(
                feedback.default_dataset_path(_cfg.degen))
            feedback.apply_with_baseline(hit["id"], _recs)
        except Exception:
            feedback.mark_proposal(hit["id"], "applied")
        ctx.send(chat_id,
                 "\u2705 Applied: %s %s \u2192 %s.\n"
                 "Takes effect on the next scan cycle (config reloads "
                 "every cycle, no restart needed). I'll grade whether it "
                 "helped once enough new trades close. Paper-only, as always."
                 % (knob, hit.get("current"), hit.get("suggested")), None)
        return
    ctx.send(chat_id, _list()[:3900], None)


def _cmd_review(ctx, chat_id, user_id, update_id, args):
    """/review — the weekly learner review, on demand.

    Same card the bot sends every Monday 08:00 WAT: new proposals,
    applied changes + grades, and the week's win rate. Informational
    only — /tune apply stays the only way to change anything.
    """
    try:
        from daemon.launch_scanner import feedback
        from daemon.telegram import alerts as tg_alerts
        from config.config import load_config
        cfg = load_config()
        event = feedback.build_weekly_review(cfg.degen, time.time())
        text = tg_alerts._tuning_weekly_review(event)
        ctx.send(chat_id, text[:3900], None)
    except Exception as exc:
        ctx.send(chat_id,
                 "\u26a0\ufe0f Review unavailable: %s" % exc, None)
# silently dropping the tail.
DIGESTFULL_MAX = 200


def _digestfull_newest(digest):
    """Newest-first digest entries, bounded to DIGESTFULL_MAX."""
    return sorted((d for d in digest if isinstance(d, dict)),
                  key=lambda d: float(d.get("ts") or 0),
                  reverse=True)[:DIGESTFULL_MAX]


def _cmd_digestfull(ctx, chat_id, user_id, update_id, args):
    """/digestfull — the full accumulating digest queue, newest first.

    Read-only. The digest card only shows two tiers (top-15 by safety
    score + 15 freshest); this command dumps the CURRENT accumulating
    queue — bounded to the newest 200, one line per coin — so nothing
    the spam guard queued is silently dropped. The queue resets on
    every flush, so this shows exactly what is waiting RIGHT NOW.
    """
    try:
        from daemon.common import get_daemon_state, get_store
        state = get_daemon_state(get_store(), "launch_scanner") or {}
    except Exception as exc:
        ctx.send(chat_id, "⚠️ Couldn't read scanner state: %s" % exc, None)
        return
    digest = [d for d in (state.get("early_digest") or [])
              if isinstance(d, dict)]
    if not digest:
        ctx.send(chat_id,
                 "🗞 The digest queue is empty right now — nothing is "
                 "waiting on the spam guard.\n"
                 "Quality-passed and unvetted coins queue here between "
                 "flushes; the digest card bundles them when the alert "
                 "window resets.", None)
        return
    now = time.time()
    newest = _digestfull_newest(digest)
    lines = ["🗞 Digest queue — %d waiting (newest first):" % len(digest)]
    for d in newest:
        sym = d.get("symbol") or "?"
        score = d.get("safety_score")
        if d.get("unvetted"):
            score_s = "[⚠️ UNVETTED]"
        else:
            score_s = ("[score %s/100]" % score) if score is not None \
                else "[score n/a]"
        ts = float(d.get("ts") or 0)
        age = ("+%dmin" % max(0, int((now - ts) / 60))) if ts > 0 \
            else "+?min"
        mint = d.get("mint") or "?"
        lines.append("• %s %s %s CA: %s" % (sym, score_s, age, mint))
    if len(digest) > DIGESTFULL_MAX:
        lines.append("…and %d older (queue order preserved)"
                     % (len(digest) - DIGESTFULL_MAX))
    # Chunk into Telegram-safe messages (same ~3900-char limit the
    # other commands use) so the whole newest-200 slice stays
    # retrievable instead of being silently truncated.
    chunks, cur, cur_len = [], [], 0
    for line in lines:
        if cur and cur_len + 1 + len(line) > 3900:
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += (1 if cur_len else 0) + len(line)
    if cur:
        chunks.append("\n".join(cur))
    for i, chunk in enumerate(chunks):
        if i:
            chunk = "🗞 (cont.)\n" + chunk
        ctx.send(chat_id, chunk, None)


def _cmd_degenstats(ctx, chat_id, user_id, update_id, args):
    try:
        from daemon.launch_scanner import scoreboard
        from storage.store import DEFAULT_DB_PATH
    except Exception as exc:
        ctx.send(chat_id, "⚠️ scoreboard unavailable: %s" % exc, None)
        return
    closes = scoreboard.load_degen_closes(str(DEFAULT_DB_PATH))
    summary = scoreboard.summarize(closes)
    ctx.send(chat_id, scoreboard.format_card(summary), None)


def _cmd_history(ctx, chat_id, user_id, update_id, args):
    symbol = args[0] if args else None
    try:
        n = int(args[1]) if len(args) > 1 else 10
    except ValueError:
        ctx.send(chat_id, "Usage: /history [symbol] [n]", None)
        return
    try:
        res = ctx.tools.forex_get_trade_history(symbol=symbol, limit=n)
    except Exception as exc:
        ctx.send(chat_id, "⚠️ history read failed: %s" % exc, None)
        return
    trades = res.get("trades", []) if isinstance(res, dict) else []
    if not trades:
        ctx.send(chat_id, "No trades in history.", None)
        return
    lines = ["🧾 Trade history:"]
    for t in trades[:n]:
        lines.append("• %s %s %s %s  P/L %s" % (
            t.get("close_time") or t.get("time"), t.get("direction"),
            t.get("volume"), t.get("symbol"), _fmt_money(t.get("profit"))))
    ctx.send(chat_id, "\n".join(lines), None)


def _cmd_kill(ctx, chat_id, user_id, update_id, args):
    try:
        res = ctx.tools.forex_get_risk()
    except Exception as exc:
        ctx.send(chat_id, "⚠️ risk read failed: %s" % exc, None)
        return
    engaged = res.get("kill_switch_engaged")
    ctx.send(chat_id, "🛑 Kill switch: %s%s" % (
        "ENGAGED" if engaged else ("clear" if engaged is False else "unknown"),
        " (source: %s)" % res.get("kill_switch_source") if res.get("kill_switch_source") else ""),
        None)


# ----------------------------------------------------------------------
# mutating commands (paper-only, 2-step confirm)
# ----------------------------------------------------------------------
def _cmd_trade(ctx, chat_id, user_id, update_id, args):
    """Usage: /trade BUY|SELL SYMBOL VOL SL TP"""
    if len(args) != 5:
        ctx.send(chat_id, "Usage: /trade BUY|SELL SYMBOL VOL SL TP\n"
                          "e.g. /trade BUY EURUSD 0.1 1.0800 1.0950", None)
        return
    direction, symbol = args[0].upper(), args[1].upper()
    if direction not in ("BUY", "SELL"):
        ctx.send(chat_id, "Direction must be BUY or SELL.", None)
        return
    try:
        volume, sl, tp = float(args[2]), float(args[3]), float(args[4])
    except ValueError:
        ctx.send(chat_id, "VOL, SL and TP must be numbers.", None)
        return
    if volume <= 0 or sl <= 0 or tp <= 0:
        ctx.send(chat_id, "VOL, SL and TP must be positive.", None)
        return
    summary = "%s %s vol=%s SL=%s TP=%s (paper)" % (direction, symbol, volume, sl, tp)
    _ask_confirm(ctx, chat_id, user_id, update_id, "trade", summary,
                 {"direction": direction, "symbol": symbol, "volume": volume,
                  "stop_loss": sl, "take_profit": tp})


def _cmd_close(ctx, chat_id, user_id, update_id, args):
    if len(args) != 1:
        ctx.send(chat_id, "Usage: /close <ticket>", None)
        return
    _ask_confirm(ctx, chat_id, user_id, update_id, "close",
                 "close position #%s (paper)" % args[0], {"ticket": args[0]})


def _cmd_closeall(ctx, chat_id, user_id, update_id, args):
    try:
        res = ctx.tools.forex_get_positions()
    except Exception as exc:
        ctx.send(chat_id, "⚠️ positions read failed: %s" % exc, None)
        return
    positions = res.get("positions", []) if isinstance(res, dict) else []
    if not positions:
        ctx.send(chat_id, "No open positions.", None)
        return
    tickets = [str(p.get("ticket")) for p in positions]
    _ask_confirm(ctx, chat_id, user_id, update_id, "closeall",
                 "close ALL %d open positions (paper): %s" % (len(tickets), ", ".join(tickets)),
                 {"tickets": tickets})


def _cmd_halt(ctx, chat_id, user_id, update_id, args):
    """Safe action: engage the kill switch immediately, no confirm needed."""
    reason = " ".join(args) if args else "manual halt from Telegram"
    try:
        res = ctx.tools.forex_kill_switch(engage=True, reason=reason)
    except Exception as exc:
        ctx.send(chat_id, "⚠️ kill-switch engage failed: %s" % exc, None)
        _audit(ctx, chat_id, user_id, update_id, "halt.failed", str(exc)[:200], False)
        return
    if res.get("ok"):
        ctx.send(chat_id, "🛑 Kill switch ENGAGED. New entries blocked; open positions are being swept closed.",
                 None)
        _audit(ctx, chat_id, user_id, update_id, "halt", reason, True)
    else:
        ctx.send(chat_id, "⚠️ Kill-switch engage failed: %s" % res, None)
        _audit(ctx, chat_id, user_id, update_id, "halt.failed", str(res)[:200], False)


def _cmd_resume(ctx, chat_id, user_id, update_id, args):
    _ask_confirm(ctx, chat_id, user_id, update_id, "resume",
                 "RELEASE the kill switch (re-enables paper entries)",
                 {"engage": False})


def _cmd_add(ctx, chat_id, user_id, update_id, args):
    """Add a coin to the signal watchlist by contract address.

    /add <CA>            — symbol resolved via DexScreener
    /add <CA> <SYMBOL>   — explicit symbol

    Monitoring-only (paper): the coin joins the H1 signal scan and you get
    a Telegram card if the strategy triggers. Never trades by itself.
    """
    if not args:
        ctx.send(chat_id,
                 "Usage: /add <contract-address> [SYMBOL]\n"
                 "Paste a contract address (Solana or EVM) and I'll start "
                 "watching it for signals.",
                 None)
        return
    mint = args[0].strip()
    if not _watchlist.validate_mint(mint):
        ctx.send(chat_id,
                 "⛔ That doesn't look like a valid contract address "
                 "(Solana or EVM). "
                 "Double-check the CA and try again.", None)
        _audit(ctx, chat_id, user_id, update_id, "add.rejected", mint[:24], False)
        return

    symbol = _watchlist.sanitize_symbol(args[1]) if len(args) > 1 else None
    if len(args) > 1 and not symbol:
        ctx.send(chat_id,
                 "⛔ Symbol must be 2-12 letters/numbers, e.g. /add %s PEPE2" % mint[:12],
                 None)
        return
    name = ""
    if not symbol:
        info = _watchlist.resolve_token(mint)
        if not info:
            ctx.send(chat_id,
                     "⚠️ Couldn't identify that token on DexScreener. If the CA is right, "
                     "pass a symbol explicitly: /add %s <SYMBOL>" % mint, None)
            _audit(ctx, chat_id, user_id, update_id, "add.unresolved", mint[:24], False)
            return
        symbol, name = info["symbol"], info["name"]

    ok, msg = _watchlist.add_to_watchlist(symbol, mint)
    if not ok:
        ctx.send(chat_id, "⚠️ Couldn't add it: %s" % msg, None)
        _audit(ctx, chat_id, user_id, update_id, "add.failed",
               "%s %s" % (symbol, msg), False)
        return
    label = "%s (%s)" % (symbol, name) if name and name != symbol else symbol
    ctx.send(chat_id,
             "✅ Watching %s now.\n"
             "CA: %s\n"
             "Monitoring starts within ~1 minute; first signal check on the next "
             "closed H1 candle. Paper mode — alerts only, no auto-trading."
             % (label, mint), None)
    _audit(ctx, chat_id, user_id, update_id, "add", "%s %s" % (symbol, mint[:16]), True)


def _cmd_confirm(ctx, chat_id, user_id, update_id, args):
    if not args:
        ctx.send(chat_id, "Usage: /confirm <code>", None)
        return
    _do_confirm(ctx, chat_id, user_id, update_id, args[0], answer_cb=None)


def _do_confirm(ctx: HandlerContext, chat_id: Any, user_id: Any, update_id: int,
                code: str, answer_cb: Optional[Callable[[str], None]]) -> None:
    intent, status = ctx.security.claim_confirm(code, chat_id, user_id)
    if intent is None:
        msg = {"unknown": "Unknown confirm code.",
               "mismatch": "Code does not belong to this chat.",
               "expired": "Code expired — run the command again."}[status]
        ctx.send(chat_id, "⛔ " + msg, None)
        if answer_cb:
            answer_cb(msg)
        _audit(ctx, chat_id, user_id, update_id, "confirm.rejected", "%s: %s" % (code, status), False)
        return
    action, payload, req_id = intent["action"], intent["payload"], intent["request_id"]
    try:
        if action == "trade":
            res = ctx.tools.forex_request_trade(
                symbol=payload["symbol"], direction=payload["direction"],
                volume=payload["volume"], stop_loss=payload["stop_loss"],
                take_profit=payload["take_profit"], request_id=req_id)
            ok = bool(res.get("ok"))
            msg = ("✅ Paper trade accepted: %s %s %s (req %s)" % (
                payload["direction"], payload["volume"], payload["symbol"], req_id)
                if ok else "🚫 Trade rejected: %s" % res)
        elif action == "close":
            res = ctx.tools.forex_close_position(payload["ticket"], request_id=req_id)
            ok = bool(res.get("ok"))
            msg = ("✅ Position #%s closed (req %s)" % (payload["ticket"], req_id)
                   if ok else "🚫 Close failed: %s" % res)
        elif action == "closeall":
            results = []
            for i, ticket in enumerate(payload["tickets"]):
                r = ctx.tools.forex_close_position(ticket, request_id="%s-%d" % (req_id, i))
                results.append((ticket, bool(r.get("ok"))))
            ok = all(x[1] for x in results)
            msg = "✅ Closed %d/%d positions." % (sum(x[1] for x in results), len(results)) \
                if ok else "⚠️ Partial close: %s" % results
        elif action == "resume":
            res = ctx.tools.forex_kill_switch(engage=False, reason="released from Telegram")
            ok = bool(res.get("ok")) and not res.get("engaged", True)
            msg = ("🟢 Kill switch RELEASED. Paper entries re-enabled."
                   if ok else "⚠️ Release failed: %s" % res)
        else:
            ok, msg = False, "unknown action"
    except Exception as exc:
        ok, msg = False, "execution failed: %s" % exc
    ctx.send(chat_id, msg, None)
    if answer_cb:
        answer_cb("done" if ok else "failed")
    _audit(ctx, chat_id, user_id, update_id, action + ".executed", "%s ok=%s" % (req_id, ok), ok)


# ----------------------------------------------------------------------
# personal alert filters (tighten-only overlays on the global gates)
# ----------------------------------------------------------------------
def _cmd_minscore(ctx, chat_id, user_id, update_id, args):
    flt = _filters.get_filters(ctx.security.state, chat_id)
    if not args or args[0].lower() == "off":
        flt["min_score"] = None
        ctx.send(chat_id,
                 "✅ Personal min score cleared — you get every signal "
                 "the bot's own gates pass.", None)
        return
    try:
        n = float(args[0])
    except (TypeError, ValueError):
        ctx.send(chat_id, "Usage: /minscore <0-100>  or  /minscore off", None)
        return
    if not 0 <= n <= 100:
        ctx.send(chat_id, "Score must be between 0 and 100.", None)
        return
    flt["min_score"] = n
    ctx.send(chat_id,
             "✅ Personal filter set: only signal cards scoring ≥ %g/100 "
             "will reach you.\n"
             "This only tightens the bot's own safety gates — it can "
             "never lower them." % n, None)


def _cmd_lane(ctx, chat_id, user_id, update_id, args):
    flt = _filters.get_filters(ctx.security.state, chat_id)
    if not args:
        ctx.send(chat_id,
                 "Your momentum lane filter: %s\n"
                 "Usage: /lane <lowcap|established|both>"
                 % (flt.get("lane") or "both"), None)
        return
    lane = args[0].lower()
    if lane not in ("lowcap", "established", "both"):
        ctx.send(chat_id, "Usage: /lane <lowcap|established|both>", None)
        return
    flt["lane"] = lane
    ctx.send(chat_id,
             "✅ Momentum lane filter: %s.\n"
             "Only affects 🚀 momentum cards; launch and warning cards "
             "are untouched." % lane, None)


def _cmd_quiet(ctx, chat_id, user_id, update_id, args):
    flt = _filters.get_filters(ctx.security.state, chat_id)
    if not args or args[0].lower() == "off":
        had = flt.get("quiet")
        flt["quiet"] = None
        note = (" Anything queued is delivered as one digest on the "
                "next cycle." if had else "")
        ctx.send(chat_id,
                 "✅ Quiet hours off — signals arrive instantly again." + note,
                 None)
        return
    rng = _filters.parse_quiet_range(args[0])
    if rng is None:
        ctx.send(chat_id,
                 "Usage: /quiet <start>-<end>  (hours 0-23, e.g. /quiet 23-7)"
                 "  or  /quiet off", None)
        return
    flt["quiet"] = rng
    ctx.send(chat_id,
             "✅ Quiet hours set: %d:00–%d:00 Lagos time.\n"
             "Signal cards will be held and delivered as ONE digest when "
             "quiet ends. Kill-switch, health and trade alerts always "
             "come through instantly." % (rng["start"], rng["end"]), None)


def _cmd_filters(ctx, chat_id, user_id, update_id, args):
    flt = _filters.get_filters(ctx.security.state, chat_id)
    ms = flt.get("min_score")
    quiet = flt.get("quiet")
    lines = ["🎚 Your personal alert filters"]
    lines.append("min score: %s" % (
        ("≥ %g/100" % ms) if ms is not None else "off (bot gates only)"))
    lines.append("momentum lane: %s" % (flt.get("lane") or "both"))
    if isinstance(quiet, dict):
        lines.append("quiet hours: %d:00–%d:00 Lagos" % (
            quiet.get("start"), quiet.get("end")))
        queued = ctx.security.state.get("quiet_queue") or []
        if queued:
            lines.append("queued right now: %d signal(s)" % len(queued))
    else:
        lines.append("quiet hours: off")
    lines.append("These only tighten the bot's gates — never loosen them.")
    ctx.send(chat_id, "\n".join(lines), None)


_COMMANDS: Dict[str, Callable] = {
    "start": _cmd_start,
    "help": _cmd_help,
    "status": _cmd_status,
    "health": _cmd_status,
    "signals": _cmd_signals,
    "signal": _cmd_signal,
    "positions": _cmd_positions,
    "account": _cmd_account,
    "perf": _cmd_perf,
    "degenstats": _cmd_degenstats,
    "history": _cmd_history,
    "kill": _cmd_kill,
    "trade": _cmd_trade,
    "close": _cmd_close,
    "closeall": _cmd_closeall,
    "halt": _cmd_halt,
    "resume": _cmd_resume,
    "confirm": _cmd_confirm,
    "add": _cmd_add,
    "smartadd": _cmd_smartadd,
    "smartremove": _cmd_smartremove,
    "smartlist": _cmd_smartlist,
    "smartgrade": _cmd_smartgrade,
    "deployers": _cmd_deployers,
    "skipped": _cmd_skipped,
    "shadow": _cmd_shadow,
    "insiders": _cmd_insiders,
    "latency": _cmd_latency,
    "heat": _cmd_heat,
    "tune": _cmd_tune,
    "review": _cmd_review,
    "digestfull": _cmd_digestfull,
    "sethelius": _cmd_sethelius,
    "heliuskey": _cmd_heliuskey,
    "minscore": _cmd_minscore,
    "lane": _cmd_lane,
    "quiet": _cmd_quiet,
    "filters": _cmd_filters,
}
