"""Tests for the telegram signal + control bot daemon.

Run from the forex-agent root:
    python3 -m unittest tests.test_telegram_bot -v

Covers (all with fake transport — no network, no real token):
  * client: token never appears in raised/logged errors (redaction).
  * security: pairing from first /start; second chat rejected + audited;
    env allowlist honored; replay of an update_id processed once;
    alert dedup across restarts; confirm code single-use / expiry /
    chat-mismatch; rate limiting; live/mainnet requests refused.
  * commands: malformed trade/close rejected without mutation; unknown
    command; halt engages immediately (safe action); trade/close/resume
    require confirm and execute once; every mutation audited.
  * alerts: known event types format; unknown types skipped; dedup.
  * daemon: run_once consumes updates, advances the offset, persists
    pairing/offset/alert-dedup in daemon state; missing token = idle.
"""

import copy
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("FOREX_AGENT_HOME", "/tmp/forex-test-home-telegram")

from daemon.telegram import client as tg_client          # noqa: E402
from daemon.telegram import security as tg_security      # noqa: E402
from daemon.telegram import commands as tg_commands      # noqa: E402
from daemon.telegram import alerts as tg_alerts          # noqa: E402
from unittest.mock import patch                          # noqa: E402


CHAT, USER = 6883056760, 111222333
CHAT2, USER2 = 999888777, 444555666


# ----------------------------------------------------------------------
# fakes
# ----------------------------------------------------------------------
class FakeTelegramClient:
    """In-memory Bot API: scripted updates, recorded sends."""

    def __init__(self, token="fake-token"):
        self.token = token
        self.updates = []          # queued update dicts
        self.sent = []             # (chat_id, text, reply_markup)
        self.answered = []         # (callback_id, text)
        self._next_id = 1

    def queue_message(self, chat_id, user_id, text):
        uid = self._next_id
        self._next_id += 1
        self.updates.append({
            "update_id": uid,
            "message": {"message_id": uid, "chat": {"id": chat_id},
                        "from": {"id": user_id}, "text": text,
                        "date": int(time.time())},
        })
        return uid

    def queue_callback(self, chat_id, user_id, cb_id, data):
        uid = self._next_id
        self._next_id += 1
        self.updates.append({
            "update_id": uid,
            "callback_query": {"id": cb_id, "from": {"id": user_id},
                               "message": {"chat": {"id": chat_id}}, "data": data},
        })
        return uid

    # API surface used by the daemon
    def get_updates(self, offset=None, timeout=15, limit=100):
        out = [u for u in self.updates
               if offset is None or u["update_id"] >= offset][:limit]
        self.updates = [u for u in self.updates if u not in out]
        return out

    def send_message(self, chat_id, text, reply_markup=None, disable_notification=False):
        self.sent.append((chat_id, text, reply_markup))
        return {"message_id": len(self.sent)}

    def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        self.answered.append((callback_query_id, text))
        return True

    def get_me(self):
        return {"id": 1, "username": "testbot"}


class FakeTools:
    """Records mutations; returns canned reads."""

    def __init__(self):
        self.calls = []
        self.positions = [{"ticket": "101", "direction": "BUY", "volume": 0.1,
                           "symbol": "EURUSD", "open_price": 1.0850, "profit": 12.5}]

    def forex_get_health(self):
        return {"mode": "paper", "kill_switch": "clear", "checks": {"db": True}}

    def forex_get_signals(self, limit=20):
        return {"signals": [{"signal_id": "sig:EURUSD:H1:x", "direction": "BUY",
                             "symbol": "EURUSD", "timeframe": "H1"}]}

    def forex_get_signal(self, signal_id):
        return {"signal": {"signal_id": signal_id, "direction": "BUY"}}

    def forex_get_positions(self):
        return {"positions": list(self.positions)}

    def forex_get_account(self):
        return {"account": {"balance": 10000.0, "equity": 10012.5, "margin": 50.0}}

    def forex_get_performance(self, lookback_days=30):
        return {"trades": 3, "win_rate": 0.66}

    def forex_get_trade_history(self, symbol=None, limit=10):
        return {"trades": []}

    def forex_get_risk(self):
        return {"kill_switch_engaged": False, "kill_switch_source": None}

    def forex_request_trade(self, symbol, direction, volume, stop_loss,
                            take_profit=None, signal_id=None,
                            idempotency_key=None, request_id=None):
        self.calls.append(("trade", request_id, direction, symbol, volume))
        return {"ok": True, "request_id": request_id}

    def forex_close_position(self, ticket, request_id=None):
        self.calls.append(("close", request_id, ticket))
        return {"ok": True}

    def forex_kill_switch(self, engage=True, reason=None):
        self.calls.append(("kill_switch", engage, reason))
        return {"ok": True, "engaged": engage, "source": "test"}


def make_ctx(client, state=None):
    state = {} if state is None else state
    sec = tg_security.SecurityContext(state)
    published = []
    audit = []
    ctx = tg_commands.HandlerContext(
        send=lambda chat_id, text, markup: client.send_message(chat_id, text, reply_markup=markup),
        security=sec,
        tools=FakeTools(),
        publish=lambda e, sev=None: published.append((e, sev)),
        audit_log=audit,
    )
    return client, ctx, sec, published, audit


# ----------------------------------------------------------------------
# client redaction
# ----------------------------------------------------------------------
class ClientRedactionTests(unittest.TestCase):
    def test_redact_strips_token(self):
        token = "123:ABC-secret"
        self.assertNotIn(token, tg_client.redact("url bot%s/x" % token, token))
        self.assertEqual(tg_client.redact(None, token), None)

    def test_client_requires_token(self):
        with self.assertRaises(ValueError):
            tg_client.TelegramClient("")


# ----------------------------------------------------------------------
# security
# ----------------------------------------------------------------------
class SecurityTests(unittest.TestCase):
    def test_pairing_first_start(self):
        sec = tg_security.SecurityContext({})
        ok, _ = sec.try_pair(CHAT, USER)
        self.assertTrue(ok)
        self.assertTrue(sec.is_authorized(CHAT, USER))
        self.assertFalse(sec.is_authorized(CHAT2, USER2))

    def test_pairing_refuses_second_chat(self):
        sec = tg_security.SecurityContext({})
        sec.try_pair(CHAT, USER)
        ok, _ = sec.try_pair(CHAT2, USER2)
        self.assertFalse(ok)
        self.assertFalse(sec.is_authorized(CHAT2, USER2))

    def test_env_allowlist(self):
        os.environ["TELEGRAM_ALLOWED_CHAT_ID"] = str(CHAT)
        os.environ["TELEGRAM_ALLOWED_USER_ID"] = str(USER)
        try:
            sec = tg_security.SecurityContext({})
            ok, _ = sec.try_pair(CHAT2, USER2)
            self.assertFalse(ok)
            ok, _ = sec.try_pair(CHAT, USER)
            self.assertTrue(ok)
        finally:
            del os.environ["TELEGRAM_ALLOWED_CHAT_ID"]
            del os.environ["TELEGRAM_ALLOWED_USER_ID"]

    def test_replay_protection(self):
        sec = tg_security.SecurityContext({})
        self.assertFalse(sec.seen_update(7))
        sec.mark_update(7)
        self.assertTrue(sec.seen_update(7))

    def test_alert_dedup(self):
        sec = tg_security.SecurityContext({})
        sec.mark_alert("evt_1")
        self.assertTrue(sec.alert_seen("evt_1"))
        self.assertFalse(sec.alert_seen("evt_2"))

    def test_rate_limit(self):
        sec = tg_security.SecurityContext({})
        for _ in range(tg_security.READ_LIMIT):
            self.assertTrue(sec.check_rate(CHAT))
        self.assertFalse(sec.check_rate(CHAT))
        # mutating bucket is separate
        self.assertTrue(sec.check_rate(CHAT, mutating=True))

    def test_confirm_single_use_and_expiry(self):
        sec = tg_security.SecurityContext({})
        code = sec.new_confirm("trade", {"a": 1}, CHAT, USER, "req-1", now=1000.0)
        intent, status = sec.claim_confirm(code, CHAT, USER, now=1001.0)
        self.assertEqual(status, "ok")
        self.assertEqual(intent["request_id"], "req-1")
        # second claim fails
        _, status = sec.claim_confirm(code, CHAT, USER, now=1002.0)
        self.assertEqual(status, "unknown")

    def test_confirm_expiry_and_mismatch(self):
        sec = tg_security.SecurityContext({})
        code = sec.new_confirm("trade", {}, CHAT, USER, "req-1", now=1000.0)
        _, status = sec.claim_confirm(code, CHAT, USER, now=1000.0 + tg_security.CONFIRM_TTL_S + 1)
        self.assertEqual(status, "expired")
        code2 = sec.new_confirm("trade", {}, CHAT, USER, "req-2", now=1000.0)
        _, status = sec.claim_confirm(code2, CHAT2, USER2, now=1001.0)
        self.assertEqual(status, "mismatch")

    def test_request_id_stable(self):
        a = tg_security.SecurityContext.request_id(42, "trade", "x")
        b = tg_security.SecurityContext.request_id(42, "trade", "x")
        c = tg_security.SecurityContext.request_id(43, "trade", "x")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(a.startswith("tg-42-"))

    def test_live_refused(self):
        for text in ("/live", "go live now", "switch to mainnet", "/mode_live",
                     "enable real-money"):
            self.assertTrue(tg_security.SecurityContext.refuses_live(text), text)
        self.assertFalse(tg_security.SecurityContext.refuses_live("/trade BUY EURUSD 0.1 1 2"))


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------
class CommandTests(unittest.TestCase):
    def _paired(self):
        client = FakeTelegramClient()
        client, ctx, sec, published, audit = make_ctx(client)
        tg_commands.handle_message(ctx, CHAT, USER, 1, "/start")
        self.assertTrue(sec.is_paired())
        return client, ctx, sec, published, audit

    def test_unauthorized_chat_ignored_and_audited(self):
        client, ctx, sec, published, audit = make_ctx(FakeTelegramClient())
        tg_commands.handle_message(ctx, CHAT2, USER2, 1, "/status")
        self.assertEqual(client.sent, [])  # no reply to strangers
        self.assertTrue(any(e.get("event") == "telegram.unauthorized" for e, _ in published))

    def test_start_pairs_first_chat(self):
        client, ctx, sec, published, audit = make_ctx(FakeTelegramClient())
        tg_commands.handle_message(ctx, CHAT, USER, 1, "/start")
        self.assertTrue(sec.is_paired())
        self.assertTrue(any("Paired" in text for _, text, _ in client.sent))

    def test_second_chat_start_rejected(self):
        client, ctx, sec, published, audit = make_ctx(FakeTelegramClient())
        tg_commands.handle_message(ctx, CHAT, USER, 1, "/start")
        n = len(client.sent)
        tg_commands.handle_message(ctx, CHAT2, USER2, 2, "/start")
        self.assertEqual(len(client.sent), n)  # ignored, no reply

    def test_read_only_status(self):
        client, ctx, sec, published, audit = self._paired()
        client.sent.clear()
        tg_commands.handle_message(ctx, CHAT, USER, 2, "/status")
        self.assertTrue(any("Status" in text for _, text, _ in client.sent))

    def test_skipped_shows_blocked_runners(self):
        import daemon.common as common
        client, ctx, sec, published, audit = self._paired()
        client.sent.clear()
        state = {"skipped_runners": [
            {"ts": time.time() - 60, "kind": "momentum", "symbol": "SHART",
             "mint": "MINTX", "chain": "solana",
             "reason": "safety-fail: graph insiders detected: 7 (limit 5)",
             "chg_24h_pct": 460.0, "safety_score": 1, "hits": 3},
        ]}
        with patch.object(common, "get_store", return_value=object()), \
             patch.object(common, "get_daemon_state", return_value=state):
            tg_commands.handle_message(ctx, CHAT, USER, 3, "/skipped")
        texts = [t for _, t, _ in client.sent]
        self.assertTrue(any("SHART" in t for t in texts))
        self.assertTrue(any("insiders" in t for t in texts))
        self.assertTrue(any("460" in t for t in texts))
        self.assertTrue(any("(×3)" in t for t in texts))

    def test_skipped_empty_state(self):
        import daemon.common as common
        client, ctx, sec, published, audit = self._paired()
        client.sent.clear()
        with patch.object(common, "get_store", return_value=object()), \
             patch.object(common, "get_daemon_state", return_value={}):
            tg_commands.handle_message(ctx, CHAT, USER, 3, "/skipped")
        self.assertTrue(any("No skipped runners" in t
                            for _, t, _ in client.sent))

    def test_shadow_shows_ledger(self):
        import daemon.common as common
        client, ctx, sec, published, audit = self._paired()
        client.sent.clear()
        state = {
            "shadow_open": {
                "M1": {"symbol": "BRAIN", "kind": "launch",
                       "chain": "solana", "entry_mcap": 1000000.0,
                       "last_mcap": 3000000.0,
                       "opened_ts": time.time() - 3600,
                       "reason": "score 1 < 50"},
            },
            "shadow_closed": [
                {"symbol": "X", "outcome": "win", "pnl_pct": 210.0},
                {"symbol": "Y", "outcome": "loss", "pnl_pct": -30.0},
            ],
        }
        with patch.object(common, "get_store", return_value=object()), \
             patch.object(common, "get_daemon_state", return_value=state):
            tg_commands.handle_message(ctx, CHAT, USER, 3, "/shadow")
        texts = [t for _, t, _ in client.sent]
        self.assertTrue(any("BRAIN" in t for t in texts))
        self.assertTrue(any("Win rate" in t for t in texts))
        self.assertTrue(any("not real trades" in t for t in texts))

    def test_shadow_empty_state(self):
        import daemon.common as common
        client, ctx, sec, published, audit = self._paired()
        client.sent.clear()
        with patch.object(common, "get_store", return_value=object()), \
             patch.object(common, "get_daemon_state", return_value={}):
            tg_commands.handle_message(ctx, CHAT, USER, 3, "/shadow")
        self.assertTrue(any("Shadow ledger is empty" in t
                            for _, t, _ in client.sent))

    def test_insiders_shows_watchlist_and_outcomes(self):
        import daemon.common as common
        from daemon.launch_scanner import insiders
        client, ctx, sec, published, audit = self._paired()
        client.sent.clear()
        now = time.time()
        state = {
            "insider_watch": {
                "M1": {"symbol": "BRN", "chain": "solana",
                       "first_cluster_pct": 34.0, "last_cluster_pct": 20.0,
                       "first_graph_n": 8, "last_graph_n": 8,
                       "last_ts": now},
            },
            "insider_outcomes": [
                {"direction": "diluting", "outcome": "win", "pnl_pct": 210.0},
                {"direction": "diluting", "outcome": "loss", "pnl_pct": -30.0},
            ],
        }
        with patch.object(common, "get_store", return_value=object()), \
             patch.object(common, "get_daemon_state", return_value=state):
            tg_commands.handle_message(ctx, CHAT, USER, 3, "/insiders")
        texts = [t for _, t, _ in client.sent]
        self.assertTrue(any("BRN" in t for t in texts))
        self.assertTrue(any("diluting" in t for t in texts))
        self.assertTrue(any("50%" in t for t in texts))  # win rate
        self.assertTrue(any("measurement only" in t for t in texts))

    def test_insiders_empty_state(self):
        import daemon.common as common
        client, ctx, sec, published, audit = self._paired()
        client.sent.clear()
        with patch.object(common, "get_store", return_value=object()), \
             patch.object(common, "get_daemon_state", return_value={}):
            tg_commands.handle_message(ctx, CHAT, USER, 3, "/insiders")
        self.assertTrue(any("watching no coins" in t
                            for _, t, _ in client.sent))

    def test_unknown_command(self):
        client, ctx, sec, published, audit = self._paired()
        client.sent.clear()
        tg_commands.handle_message(ctx, CHAT, USER, 2, "/frobnicate")
        self.assertTrue(any("Unknown command" in text for _, text, _ in client.sent))

    def test_malformed_trade_rejected_no_mutation(self):
        client, ctx, sec, published, audit = self._paired()
        tools = ctx.tools
        for bad in ("/trade", "/trade BUY", "/trade BUY EURUSD 0.1",
                    "/trade HOLD EURUSD 0.1 1 2", "/trade BUY EURUSD x 1 2"):
            tg_commands.handle_message(ctx, CHAT, USER, 9, bad)
        self.assertEqual(tools.calls, [])

    def test_trade_confirm_flow_executes_once(self):
        client, ctx, sec, published, audit = self._paired()
        tools = ctx.tools
        tg_commands.handle_message(ctx, CHAT, USER, 10, "/trade BUY EURUSD 0.1 1.0800 1.0950")
        self.assertEqual(tools.calls, [])  # nothing executed yet
        code = next(iter(sec.state["pending_confirms"]))
        tg_commands.handle_message(ctx, CHAT, USER, 11, "/confirm %s" % code)
        self.assertEqual(len(tools.calls), 1)
        kind, req_id, direction, symbol, volume = tools.calls[0]
        self.assertEqual((kind, direction, symbol, volume), ("trade", "BUY", "EURUSD", 0.1))
        self.assertTrue(req_id.startswith("tg-10-"))
        # double confirm is dead
        tg_commands.handle_message(ctx, CHAT, USER, 12, "/confirm %s" % code)
        self.assertEqual(len(tools.calls), 1)

    def test_confirm_wrong_code(self):
        client, ctx, sec, published, audit = self._paired()
        tg_commands.handle_message(ctx, CHAT, USER, 10, "/close 101")
        tg_commands.handle_message(ctx, CHAT, USER, 11, "/confirm ZZZZZZ")
        self.assertEqual(ctx.tools.calls, [])
        self.assertTrue(any("Unknown confirm code" in text for _, text, _ in client.sent))

    def test_callback_confirm_button(self):
        client, ctx, sec, published, audit = self._paired()
        tg_commands.handle_message(ctx, CHAT, USER, 10, "/close 101")
        code = next(iter(sec.state["pending_confirms"]))
        tg_commands.handle_callback(ctx, CHAT, USER, 11, "cb1", "confirm:%s" % code,
                                    answer=lambda qid, text: client.answer_callback_query(qid, text=text))
        self.assertEqual(len(ctx.tools.calls), 1)
        self.assertTrue(any(qid == "cb1" for qid, _ in client.answered))

    def test_halt_is_immediate_safe_action(self):
        client, ctx, sec, published, audit = self._paired()
        tg_commands.handle_message(ctx, CHAT, USER, 20, "/halt testing")
        self.assertIn(("kill_switch", True, "testing"), ctx.tools.calls)
        self.assertTrue(any("ENGAGED" in text for _, text, _ in client.sent))

    def test_resume_requires_confirm(self):
        client, ctx, sec, published, audit = self._paired()
        tg_commands.handle_message(ctx, CHAT, USER, 21, "/resume")
        self.assertEqual(ctx.tools.calls, [])
        code = next(iter(sec.state["pending_confirms"]))
        tg_commands.handle_message(ctx, CHAT, USER, 22, "/confirm %s" % code)
        self.assertIn(("kill_switch", False, "released from Telegram"), ctx.tools.calls)

    def test_live_request_refused_and_audited(self):
        client, ctx, sec, published, audit = self._paired()
        tg_commands.handle_message(ctx, CHAT, USER, 30, "/live")
        self.assertTrue(any("never enable live" in text for _, text, _ in client.sent))
        self.assertTrue(any(a["action"] == "live_refused" for a in audit))

    def test_every_mutation_audited(self):
        client, ctx, sec, published, audit = self._paired()
        tg_commands.handle_message(ctx, CHAT, USER, 20, "/halt x")
        tg_commands.handle_message(ctx, CHAT, USER, 21, "/trade BUY EURUSD 0.1 1.08 1.09")
        actions = {a["action"] for a in audit}
        self.assertIn("halt", actions)
        self.assertIn("trade.proposed", actions)

    def test_closeall_confirms_with_ticket_list(self):
        client, ctx, sec, published, audit = self._paired()
        tg_commands.handle_message(ctx, CHAT, USER, 40, "/closeall")
        code = next(iter(sec.state["pending_confirms"]))
        tg_commands.handle_message(ctx, CHAT, USER, 41, "/confirm %s" % code)
        closes = [c for c in ctx.tools.calls if c[0] == "close"]
        self.assertEqual(len(closes), 1)


# ----------------------------------------------------------------------
# alerts
# ----------------------------------------------------------------------
class AlertTests(unittest.TestCase):
    def test_known_events_format(self):
        self.assertIn("SIGNAL", tg_alerts.format_alert(
            {"event": "signal.detected", "direction": "BUY", "symbol": "EURUSD",
             "timeframe": "H1", "entry_price": 1.08, "stop_loss": 1.07,
             "take_profit": 1.09, "strategy": "ema_rsi", "signal_id": "sig:1"}))
        self.assertIn("ENGAGED", tg_alerts.format_alert(
            {"event": "kill_switch.activated", "source": "risk"}))
        self.assertIn("STOPPED", tg_alerts.format_alert(
            {"event": "daemon.stopped", "daemon": "health_monitor"}))
        self.assertIn("REJECTED", tg_alerts.format_alert(
            {"event": "trade.rejected", "direction": "BUY", "volume": 0.1,
             "symbol": "EURUSD", "reason": "risk"}))

    def test_unknown_events_skipped(self):
        self.assertIsNone(tg_alerts.format_alert({"event": "market.candle_closed"}))
        self.assertIsNone(tg_alerts.format_alert({"event": "something.else"}))


# ----------------------------------------------------------------------
# daemon loop
# ----------------------------------------------------------------------
class DaemonLoopTests(unittest.TestCase):
    def _daemon(self, client, tools, token="tok"):
        from daemon.telegram import TelegramDaemon
        os.environ["TELEGRAM_BOT_TOKEN"] = token
        self.addCleanup(lambda: os.environ.pop("TELEGRAM_BOT_TOKEN", None))
        return TelegramDaemon(client_factory=lambda t: client, tools=tools)

    def test_run_once_pairs_and_advances_offset(self):
        from daemon.common import get_daemon_state, save_daemon_state  # noqa
        client, tools = FakeTelegramClient(), FakeTools()
        daemon = self._daemon(client, tools)
        state = {}
        sec_holder = {}

        # drive the daemon's internals with an injected store
        import daemon.telegram as tg_mod
        real_get_store, real_get_state, real_save = tg_mod.get_store, tg_mod.get_daemon_state, tg_mod.save_daemon_state
        real_bus_configure, real_bus_poll = tg_mod.event_bus.configure, tg_mod.event_bus.poll
        store = object()
        tg_mod.get_store = lambda: store
        tg_mod.get_daemon_state = lambda s, name: state
        tg_mod.save_daemon_state = lambda s, name, st: state.update(st)
        tg_mod.event_bus.configure = lambda store=None: None
        tg_mod.event_bus.poll = lambda since=None, limit=100: []
        try:
            client.queue_message(CHAT, USER, "/start")
            client.queue_message(CHAT, USER, "/status")
            daemon.run_once()
        finally:
            tg_mod.get_store, tg_mod.get_daemon_state = real_get_store, real_get_state
            tg_mod.save_daemon_state = real_save
            # NOTE: event_bus.configure/poll are module-global; they must be
            # restored too, otherwise later test modules inherit the stubs
            # (and their publishes land in the real production store).
            tg_mod.event_bus.configure = real_bus_configure
            tg_mod.event_bus.poll = real_bus_poll
        self.assertEqual(state.get("paired_chat_id"), CHAT)
        self.assertEqual(state.get("offset"), 3)  # both updates consumed
        self.assertTrue(any("Paired" in t for _, t, _ in client.sent))

    def test_run_once_replay_drops_redelivery(self):
        client, tools = FakeTelegramClient(), FakeTools()
        daemon = self._daemon(client, tools)
        state = {"paired_chat_id": CHAT, "paired_user_id": USER}
        import daemon.telegram as tg_mod
        real = (tg_mod.get_store, tg_mod.get_daemon_state, tg_mod.save_daemon_state,
                tg_mod.event_bus.configure, tg_mod.event_bus.poll)
        store = object()
        tg_mod.get_store = lambda: store
        tg_mod.get_daemon_state = lambda s, name: state
        tg_mod.save_daemon_state = lambda s, name, st: state.update(st)
        tg_mod.event_bus.configure = lambda store=None: None
        tg_mod.event_bus.poll = lambda since=None, limit=100: []
        try:
            uid = client.queue_message(CHAT, USER, "/status")
            daemon.run_once()
            n = len(client.sent)
            # redeliver the same update (simulating Telegram retry)
            client.updates.insert(0, {"update_id": uid, "message": {
                "message_id": uid, "chat": {"id": CHAT}, "from": {"id": USER},
                "text": "/status", "date": int(time.time())}})
            daemon.run_once()
        finally:
            (tg_mod.get_store, tg_mod.get_daemon_state, tg_mod.save_daemon_state,
             tg_mod.event_bus.configure, tg_mod.event_bus.poll) = real
        self.assertEqual(len(client.sent), n)  # redelivery produced no new reply

    def test_run_once_no_token_idles(self):
        from daemon.telegram import TelegramDaemon
        daemon = TelegramDaemon(tools=FakeTools())
        import daemon.telegram as tg_mod
        real = (tg_mod.get_store, tg_mod.get_daemon_state, tg_mod.save_daemon_state,
                tg_mod.event_bus.configure, tg_mod.load_token)
        state = {}
        tg_mod.get_store = lambda: object()
        tg_mod.get_daemon_state = lambda s, name: state
        tg_mod.save_daemon_state = lambda s, name, st: state.update(st)
        tg_mod.event_bus.configure = lambda store=None: None
        tg_mod.load_token = lambda: None
        try:
            daemon.run_once()  # must not raise
        finally:
            (tg_mod.get_store, tg_mod.get_daemon_state, tg_mod.save_daemon_state,
             tg_mod.event_bus.configure, tg_mod.load_token) = real
        self.assertEqual(state, {})

    def test_alert_dedup_across_restarts(self):
        client, tools = FakeTelegramClient(), FakeTools()
        daemon = self._daemon(client, tools)
        state = {"paired_chat_id": CHAT, "paired_user_id": USER,
                 "alerted_event_ids": ["evt_1"]}
        import daemon.telegram as tg_mod
        real = (tg_mod.get_store, tg_mod.get_daemon_state, tg_mod.save_daemon_state,
                tg_mod.event_bus.configure, tg_mod.event_bus.poll)
        tg_mod.get_store = lambda: object()
        tg_mod.get_daemon_state = lambda s, name: state
        tg_mod.save_daemon_state = lambda s, name, st: state.update(st)
        tg_mod.event_bus.configure = lambda store=None: None
        evt = {"event": "signal.detected", "event_id": "evt_1", "ts": "2026-09-22T18:00:00Z",
               "direction": "BUY", "symbol": "EURUSD", "timeframe": "H1",
               "signal_id": "sig:1"}
        evt2 = dict(evt, event_id="evt_2")
        tg_mod.event_bus.poll = lambda since=None, limit=100: [copy.deepcopy(evt), copy.deepcopy(evt2)]
        try:
            daemon.run_once()  # first pass: sends evt_2 only
            first = len(client.sent)
            self.assertEqual(first, 1)
            daemon.run_once()  # second pass: same events redelivered -> nothing new
        finally:
            (tg_mod.get_store, tg_mod.get_daemon_state, tg_mod.save_daemon_state,
             tg_mod.event_bus.configure, tg_mod.event_bus.poll) = real
        self.assertEqual(len(client.sent), first)
        self.assertIn("evt_2", state["alerted_event_ids"])

    # -- helpers for fan-out tests --------------------------------------
    def _patched_bus(self, tg_mod, state, events):
        real = (tg_mod.get_store, tg_mod.get_daemon_state, tg_mod.save_daemon_state,
                tg_mod.event_bus.configure, tg_mod.event_bus.poll)
        tg_mod.get_store = lambda: object()
        tg_mod.get_daemon_state = lambda s, name: state
        tg_mod.save_daemon_state = lambda s, name, st: state.update(st)
        tg_mod.event_bus.configure = lambda store=None: None
        tg_mod.event_bus.poll = lambda since=None, limit=100: [copy.deepcopy(e) for e in events]
        return real

    def _unpatch_bus(self, tg_mod, real):
        (tg_mod.get_store, tg_mod.get_daemon_state, tg_mod.save_daemon_state,
         tg_mod.event_bus.configure, tg_mod.event_bus.poll) = real

    @staticmethod
    def _alert_event(event_id, ts):
        return {"event": "signal.detected", "event_id": event_id, "ts": ts,
                "direction": "BUY", "symbol": "BONK", "timeframe": "H1",
                "signal_id": "sig:%s" % event_id}

    def test_empty_and_whitespace_messages_do_not_crash(self):
        from daemon.telegram import security as tg_sec
        from daemon.telegram.commands import HandlerContext, handle_message
        sent = []
        sec = tg_sec.SecurityContext({})
        ctx = HandlerContext(send=lambda c, t, m: sent.append((c, t)),
                             security=sec, tools=FakeTools(),
                             publish=lambda e, sev=None: None)
        # unpaired: empty / whitespace-only must not raise IndexError
        handle_message(ctx, CHAT, USER, 1, "")
        handle_message(ctx, CHAT, USER, 2, "   \n\t  ")
        self.assertFalse(sec.is_paired())
        # paired: same — routed to nothing, no crash, no reply
        sec.state["paired_chat_id"] = CHAT
        sec.state["paired_user_id"] = USER
        handle_message(ctx, CHAT, USER, 3, "")
        handle_message(ctx, CHAT, USER, 4, "   ")
        self.assertEqual(sent, [])

    def test_fanout_failed_send_not_marked_and_retried(self):
        from daemon.telegram.client import TelegramError
        import daemon.telegram as tg_mod

        class FlakyClient(FakeTelegramClient):
            def __init__(self):
                super().__init__()
                self.fail = True
            def send_message(self, chat_id, text, reply_markup=None,
                             disable_notification=False):
                if self.fail:
                    raise TelegramError("simulated send failure")
                return super().send_message(chat_id, text, reply_markup,
                                            disable_notification)

        client, tools = FlakyClient(), FakeTools()
        daemon = self._daemon(client, tools)
        state = {"paired_chat_id": CHAT, "paired_user_id": USER}
        events = [self._alert_event("evt_fail_1", "2026-09-22T19:00:00+00:00"),
                  self._alert_event("evt_fail_2", "2026-09-22T19:01:00+00:00")]
        real = self._patched_bus(tg_mod, state, events)
        try:
            daemon.run_once()  # both sends fail
        finally:
            self._unpatch_bus(tg_mod, real)
        # failed alerts are NOT marked delivered...
        self.assertNotIn("evt_fail_1", state.get("alerted_event_ids", []))
        # ...and last_event_ts does NOT advance past them (no skipping)
        self.assertIsNone(state.get("last_event_ts"))
        self.assertEqual(client.sent, [])

        # network recovers: the same events are retried and delivered once
        client.fail = False
        real = self._patched_bus(tg_mod, state, events)
        try:
            daemon.run_once()
        finally:
            self._unpatch_bus(tg_mod, real)
        self.assertEqual(len(client.sent), 2)
        self.assertIn("evt_fail_1", state["alerted_event_ids"])
        self.assertIn("evt_fail_2", state["alerted_event_ids"])
        self.assertEqual(state["last_event_ts"], "2026-09-22T19:01:00+00:00")

    def test_fanout_advances_only_past_delivered_prefix(self):
        from daemon.telegram.client import TelegramError
        import daemon.telegram as tg_mod

        class FailFirstClient(FakeTelegramClient):
            def __init__(self):
                super().__init__()
                self.calls = 0
            def send_message(self, chat_id, text, reply_markup=None,
                             disable_notification=False):
                self.calls += 1
                if self.calls == 1:
                    raise TelegramError("first send fails")
                return super().send_message(chat_id, text, reply_markup,
                                            disable_notification)

        client, tools = FailFirstClient(), FakeTools()
        daemon = self._daemon(client, tools)
        state = {"paired_chat_id": CHAT, "paired_user_id": USER}
        events = [self._alert_event("evt_p1", "2026-09-22T19:00:00+00:00"),
                  self._alert_event("evt_p2", "2026-09-22T19:01:00+00:00")]
        real = self._patched_bus(tg_mod, state, events)
        try:
            daemon.run_once()  # first fails -> stop; second NOT attempted
        finally:
            self._unpatch_bus(tg_mod, real)
        self.assertEqual(client.sent, [])  # nothing delivered
        self.assertNotIn("evt_p2", state.get("alerted_event_ids", []))
        self.assertIsNone(state.get("last_event_ts"))


class FanOutResilienceTests(unittest.TestCase):
    """Outbound alerts must still go out when the getUpdates long-poll
    fails — inbound fetching and outbound delivery are independent."""

    def _daemon(self, client, tools, token="tok"):
        from daemon.telegram import TelegramDaemon
        os.environ["TELEGRAM_BOT_TOKEN"] = token
        self.addCleanup(lambda: os.environ.pop("TELEGRAM_BOT_TOKEN", None))
        return TelegramDaemon(client_factory=lambda t: client, tools=tools)

    def test_fan_out_continues_when_get_updates_fails(self):
        client, tools = FakeTelegramClient(), FakeTools()
        daemon = self._daemon(client, tools)
        state = {"paired_chat_id": CHAT, "paired_user_id": USER}

        def boom(offset=None, timeout=15, limit=100):
            raise tg_client.TelegramError("long-poll timeout")
        client.get_updates = boom

        import daemon.telegram as tg_mod
        real = (tg_mod.get_store, tg_mod.get_daemon_state,
                tg_mod.save_daemon_state, tg_mod.event_bus.configure,
                tg_mod.event_bus.poll)
        store = object()
        tg_mod.get_store = lambda: store
        tg_mod.get_daemon_state = lambda s, name: state
        tg_mod.save_daemon_state = lambda s, name, st: state.update(st)
        tg_mod.event_bus.configure = lambda store=None: None
        tg_mod.event_bus.poll = lambda since=None, limit=100: [{
            "event": "launch.early", "event_id": "evt-early-1",
            "ts": "2026-09-23T09:00:00+00:00",
            "tier": "early", "symbol": "TKN", "name": "Token",
            "chain": "solana", "source": "pumpfun", "mint": "MINT1",
            "usd_market_cap": 15000, "age_minutes": 0.5, "url": "u"}]
        try:
            daemon.run_once()  # must not raise; alert still delivered
        finally:
            (tg_mod.get_store, tg_mod.get_daemon_state,
             tg_mod.save_daemon_state, tg_mod.event_bus.configure,
             tg_mod.event_bus.poll) = real
        self.assertTrue(
            any("EARLY DETECT" in t and "CA: MINT1" in t
                for _, t, _ in client.sent),
            "early alert was not fanned out during getUpdates failure")


class CopyButtonTests(unittest.TestCase):
    """One-tap copy-CA button on runner cards; digest card formatting."""

    def test_copy_button_on_early_and_confirmed(self):
        from daemon.telegram import alerts as tg_alerts
        for evt in ("launch.early", "launch.detected"):
            mk = tg_alerts.format_alert_markup({"event": evt, "mint": "MINT9"})
            self.assertIsNotNone(mk)
            btn = mk["inline_keyboard"][0][0]
            self.assertEqual(btn["text"], "📋 Copy CA")
            self.assertEqual(btn["copy_text"]["text"], "MINT9")

    def test_no_button_without_mint_or_other_events(self):
        from daemon.telegram import alerts as tg_alerts
        self.assertIsNone(tg_alerts.format_alert_markup(
            {"event": "launch.early"}))  # no mint
        self.assertIsNone(tg_alerts.format_alert_markup(
            {"event": "signal.detected", "mint": "M"}))
        self.assertIsNone(tg_alerts.format_alert_markup(
            {"event": "launch.early_digest", "count": 3}))

    def test_digest_card_lists_cas(self):
        from daemon.telegram import alerts as tg_alerts
        e = {"event": "launch.early_digest", "count": 2, "items": [
            {"symbol": "A", "mint": "MINT_A", "tier": "early"},
            {"symbol": "B", "mint": "MINT_B", "tier": "velocity"}]}
        text = tg_alerts.format_alert(e)
        self.assertIn("EARLY DIGEST", text)
        self.assertIn("CA: MINT_A", text)
        self.assertIn("CA: MINT_B", text)
        self.assertIn("UNCONFIRMED", text)

    def test_fan_out_attaches_copy_button(self):
        client, tools = FakeTelegramClient(), FakeTools()
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        self.addCleanup(lambda: os.environ.pop("TELEGRAM_BOT_TOKEN", None))
        from daemon.telegram import TelegramDaemon
        daemon = TelegramDaemon(client_factory=lambda t: client, tools=tools)
        state = {"paired_chat_id": CHAT, "paired_user_id": USER}
        import daemon.telegram as tg_mod
        real = (tg_mod.get_store, tg_mod.get_daemon_state,
                tg_mod.save_daemon_state, tg_mod.event_bus.configure,
                tg_mod.event_bus.poll)
        store = object()
        tg_mod.get_store = lambda: store
        tg_mod.get_daemon_state = lambda s, name: state
        tg_mod.save_daemon_state = lambda s, name, st: state.update(st)
        tg_mod.event_bus.configure = lambda store=None: None
        tg_mod.event_bus.poll = lambda since=None, limit=100: [{
            "event": "launch.early", "event_id": "evt-copy-1",
            "ts": "2026-09-23T09:00:00+00:00",
            "tier": "early", "symbol": "TKN", "chain": "solana",
            "source": "pumpfun", "mint": "COPYMINT",
            "usd_market_cap": 15000, "age_minutes": 0.5, "url": "u"}]
        try:
            daemon.run_once()
        finally:
            (tg_mod.get_store, tg_mod.get_daemon_state,
             tg_mod.save_daemon_state, tg_mod.event_bus.configure,
             tg_mod.event_bus.poll) = real
        self.assertTrue(client.sent, "no alert sent")
        markup = client.sent[0][2]
        self.assertIsNotNone(markup)
        self.assertEqual(markup["inline_keyboard"][0][0]["copy_text"]["text"],
                         "COPYMINT")


# ----------------------------------------------------------------------
# /sethelius — Helius key wiring
# ----------------------------------------------------------------------
class SetHeliusTests(unittest.TestCase):
    KEY = "TESTKEY-abc123xyz789"

    def setUp(self):
        from daemon.launch_scanner import smartmoney
        self._sm = smartmoney
        self._old_cache = (smartmoney._key_cache, smartmoney._key_checked)
        self.tmp = tempfile.mkdtemp(prefix="helius-test-")
        self._old_home = os.environ.get("FOREX_AGENT_HOME")
        os.environ["FOREX_AGENT_HOME"] = self.tmp
        self._old_verify = tg_commands._verify_helius_key
        tg_commands._verify_helius_key = lambda key, timeout=15.0: (True, "424242")
        self.deleted = []

    def tearDown(self):
        tg_commands._verify_helius_key = self._old_verify
        self._sm._key_cache, self._sm._key_checked = self._old_cache
        if self._old_home is None:
            os.environ.pop("FOREX_AGENT_HOME", None)
        else:
            os.environ["FOREX_AGENT_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _paired_ctx(self):
        client = FakeTelegramClient()
        client, ctx, sec, published, audit = make_ctx(client)
        ctx.delete_message = lambda c, m: self.deleted.append((c, m))
        tg_commands.handle_message(ctx, CHAT, USER, 1, "/start")
        self.assertTrue(sec.is_paired())
        client.sent.clear()
        return client, ctx, sec, published, audit

    def _key_file(self):
        return os.path.join(self.tmp, "helius", "api_key")

    def test_sethelius_saves_verifies_and_scrubs(self):
        client, ctx, sec, published, audit = self._paired_ctx()
        tg_commands.handle_message(ctx, CHAT, USER, 2,
                                   "/sethelius " + self.KEY, message_id=77)
        # file written, locked down, exact content
        with open(self._key_file()) as fh:
            self.assertEqual(fh.read(), self.KEY + "\n")
        self.assertEqual(os.stat(self._key_file()).st_mode & 0o777, 0o600)
        # success reply, key never echoed
        self.assertTrue(any("verified" in t for _, t, _ in client.sent))
        for _, text, _ in client.sent:
            self.assertNotIn(self.KEY, text)
        # audit journal: action recorded, key absent everywhere
        self.assertTrue(any(e.get("action") == "sethelius" and e.get("ok")
                            for e in audit))
        for entry in audit:
            self.assertNotIn(self.KEY, str(entry))
        # user's message scrubbed
        self.assertEqual(self.deleted, [(CHAT, 77)])
        # this process's cached key reset so polling uses it
        self.assertEqual(self._sm._key_cache, self.KEY)

    def test_sethelius_malformed_rejected(self):
        client, ctx, sec, published, audit = self._paired_ctx()
        tg_commands.handle_message(ctx, CHAT, USER, 2, "/sethelius short",
                                   message_id=78)
        self.assertFalse(os.path.exists(self._key_file()))
        self.assertTrue(any("doesn't look like an API key" in t
                            for _, t, _ in client.sent))
        self.assertEqual(self.deleted, [])

    def test_sethelius_no_args_usage(self):
        client, ctx, sec, published, audit = self._paired_ctx()
        tg_commands.handle_message(ctx, CHAT, USER, 2, "/sethelius")
        self.assertFalse(os.path.exists(self._key_file()))
        self.assertTrue(any("Usage: /sethelius" in t for _, t, _ in client.sent))

    def test_sethelius_verify_failure_still_saves_warns(self):
        tg_commands._verify_helius_key = lambda key, timeout=15.0: (
            False, "401 Unauthorized")
        client, ctx, sec, published, audit = self._paired_ctx()
        tg_commands.handle_message(ctx, CHAT, USER, 2,
                                   "/sethelius " + self.KEY, message_id=79)
        with open(self._key_file()) as fh:
            self.assertEqual(fh.read(), self.KEY + "\n")
        self.assertTrue(any("verification failed" in t for _, t, _ in client.sent))
        for _, text, _ in client.sent:
            self.assertNotIn(self.KEY, text)

    def test_sethelius_unauthorized_chat_refused(self):
        client, ctx, sec, published, audit = make_ctx(FakeTelegramClient())
        ctx.delete_message = lambda c, m: self.deleted.append((c, m))
        tg_commands.handle_message(ctx, CHAT2, USER2, 1,
                                   "/sethelius " + self.KEY, message_id=80)
        self.assertEqual(client.sent, [])
        self.assertFalse(os.path.exists(self._key_file()))
        self.assertEqual(self.deleted, [])

    def test_sethelius_scrub_failure_warns_user(self):
        client, ctx, sec, published, audit = self._paired_ctx()

        def boom(chat_id, message_id):
            raise RuntimeError("cannot delete")

        ctx.delete_message = boom
        tg_commands.handle_message(ctx, CHAT, USER, 2,
                                   "/sethelius " + self.KEY, message_id=81)
        self.assertTrue(any("delete it manually" in t for _, t, _ in client.sent))


if __name__ == "__main__":
    unittest.main()


# ----------------------------------------------------------------------
# /heliuskey — validated Helius key delivery
# ----------------------------------------------------------------------
class HeliusKeyTests(unittest.TestCase):
    KEY = "123e4567-e89b-12d3-a456-426614174000"  # valid UUID shape
    BAD46 = "x" * 46  # the shape of the real failed paste

    def setUp(self):
        from daemon.launch_scanner import smartmoney
        self._sm = smartmoney
        self._old_cache = (smartmoney._key_cache, smartmoney._key_checked)
        self.tmp = tempfile.mkdtemp(prefix="heliuskey-test-")
        self._old_home = os.environ.get("FOREX_AGENT_HOME")
        os.environ["FOREX_AGENT_HOME"] = self.tmp
        self._old_verify = tg_commands._verify_helius_key
        self.verify_calls = []
        outer = self

        def fake_verify(key, timeout=15.0):
            outer.verify_calls.append(key)
            return True, "424242"

        tg_commands._verify_helius_key = fake_verify
        self.deleted = []

    def tearDown(self):
        tg_commands._verify_helius_key = self._old_verify
        self._sm._key_cache, self._sm._key_checked = self._old_cache
        if self._old_home is None:
            os.environ.pop("FOREX_AGENT_HOME", None)
        else:
            os.environ["FOREX_AGENT_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _paired_ctx(self):
        client = FakeTelegramClient()
        client, ctx, sec, published, audit = make_ctx(client)
        ctx.delete_message = lambda c, m: self.deleted.append((c, m))
        tg_commands.handle_message(ctx, CHAT, USER, 1, "/start")
        self.assertTrue(sec.is_paired())
        client.sent.clear()
        return client, ctx, sec, published, audit

    def _key_file(self):
        return os.path.join(self.tmp, "helius", "api_key")

    def test_no_args_shows_guide(self):
        client, ctx, sec, published, audit = self._paired_ctx()
        tg_commands.handle_message(ctx, CHAT, USER, 2, "/heliuskey")
        self.assertFalse(os.path.exists(self._key_file()))
        self.assertEqual(self.verify_calls, [])
        self.assertTrue(any("dashboard.helius.dev" in t
                            for _, t, _ in client.sent))
        self.assertTrue(any("Free plan" in t for _, t, _ in client.sent))

    def test_malformed_46char_rejected_with_diagnosis(self):
        client, ctx, sec, published, audit = self._paired_ctx()
        tg_commands.handle_message(ctx, CHAT, USER, 2,
                                   "/heliuskey " + self.BAD46, message_id=81)
        self.assertFalse(os.path.exists(self._key_file()))
        self.assertEqual(self.verify_calls, [])  # never even tested
        self.assertTrue(any("46 characters" in t for _, t, _ in client.sent))
        self.assertEqual(self.deleted, [])

    def test_url_paste_rejected(self):
        client, ctx, sec, published, audit = self._paired_ctx()
        tg_commands.handle_message(ctx, CHAT, USER, 2,
                                   "/heliuskey https://dashboard.helius.dev/x",
                                   message_id=82)
        self.assertFalse(os.path.exists(self._key_file()))
        self.assertTrue(any("not a key" in t for _, t, _ in client.sent))

    def test_verify_failure_saves_nothing(self):
        # KEY DIFFERENCE vs /sethelius: a rejected key never lands in the file.
        tg_commands._verify_helius_key = lambda key, timeout=15.0: (
            False, "HTTP 401 from Helius")
        client, ctx, sec, published, audit = self._paired_ctx()
        tg_commands.handle_message(ctx, CHAT, USER, 2,
                                   "/heliuskey " + self.KEY, message_id=83)
        self.assertFalse(os.path.exists(self._key_file()))
        self.assertTrue(any("doesn't recognize it" in t
                            for _, t, _ in client.sent))
        for _, text, _ in client.sent:
            self.assertNotIn(self.KEY, text)
        self.assertEqual(self.deleted, [])

    def test_success_saves_verifies_and_scrubs(self):
        client, ctx, sec, published, audit = self._paired_ctx()
        tg_commands.handle_message(ctx, CHAT, USER, 2,
                                   "/heliuskey " + self.KEY, message_id=84)
        with open(self._key_file()) as fh:
            self.assertEqual(fh.read(), self.KEY + "\n")
        self.assertEqual(os.stat(self._key_file()).st_mode & 0o777, 0o600)
        self.assertTrue(any("tested and saved" in t
                            for _, t, _ in client.sent))
        for _, text, _ in client.sent:
            self.assertNotIn(self.KEY, text)
        self.assertTrue(any(e.get("action") == "heliuskey" and e.get("ok")
                            for e in audit))
        for entry in audit:
            self.assertNotIn(self.KEY, str(entry))
        self.assertEqual(self.deleted, [(CHAT, 84)])
        self.assertEqual(self._sm._key_cache, self.KEY)
