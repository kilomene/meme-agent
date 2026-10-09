"""Security layer for the Telegram control bot.

This module holds every rule that keeps a chat interface to a trading bot
from becoming an attack surface:

* **Pairing / allowlist** — exactly one Telegram chat and one Telegram user
  may ever command the bot. Pairing happens from the first ``/start`` (the
  user proves possession of the chat); optional hard-coded allowlists via
  environment override it. Every other chat/user is ignored and audited.
* **Replay protection** — every processed Telegram ``update_id`` is recorded
  in durable daemon state; redelivered updates are dropped silently.
* **Alert deduplication** — every journal ``event_id`` fanned out as an
  alert is recorded; restarts never re-send an alert.
* **Two-step confirmation** — every mutating command (trade / close /
  kill-switch release) parks a pending intent with a short-lived single-use
  code; execution only happens on an explicit ``/confirm <code>`` (or the
  inline button) from the paired chat within the TTL.
* **Rate limiting** — per-chat token buckets keep floods and loops cheap.
* **Fail closed** — malformed, expired, or unauthorized commands are
  refused; nothing in this module can enable live/mainnet trading (no such
  command exists, and the words "live"/"mainnet" are refused explicitly).

All state lives in the daemon-state dict handed in at construction; the
daemon persists it after every cycle.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("forex_agent.daemon.telegram")

CONFIRM_TTL_S = 180            # pending confirm lives 3 minutes
CONFIRM_CODE_LEN = 6

# Rate limits (per paired chat)
READ_LIMIT = 15                # messages per window
READ_WINDOW_S = 60
MUTATE_LIMIT = 5               # mutating commands per window
MUTATE_WINDOW_S = 60

MAX_TRACKED = 500              # bounded replay/dedup history


def _bounded(seq, value):
    seq.append(value)
    if len(seq) > MAX_TRACKED:
        del seq[: len(seq) - MAX_TRACKED]
    return seq


class SecurityContext:
    """Authorization + replay + dedup + confirm state for one daemon."""

    def __init__(self, state: Dict[str, Any]):
        self.state = state
        self.state.setdefault("processed_update_ids", [])
        self.state.setdefault("alerted_event_ids", [])
        self.state.setdefault("pending_confirms", {})
        self.state.setdefault("rate_buckets", {})
        self.state.setdefault("unauthorized_hits", 0)

    # ------------------------------------------------------------------
    # pairing / allowlist
    # ------------------------------------------------------------------
    def is_paired(self) -> bool:
        return bool(self.state.get("paired_chat_id") and self.state.get("paired_user_id"))

    def _env_allowlist(self) -> Tuple[Optional[str], Optional[str]]:
        return (os.environ.get("TELEGRAM_ALLOWED_CHAT_ID"),
                os.environ.get("TELEGRAM_ALLOWED_USER_ID"))

    def try_pair(self, chat_id: Any, user_id: Any) -> Tuple[bool, str]:
        """Pair the first /start sender. Returns (ok, message)."""
        env_chat, env_user = self._env_allowlist()
        if env_chat and str(chat_id) != str(env_chat):
            return False, "pairing denied: chat not in the configured allowlist"
        if env_user and str(user_id) != str(env_user):
            return False, "pairing denied: user not in the configured allowlist"
        if self.is_paired():
            return False, "already paired"
        self.state["paired_chat_id"] = chat_id
        self.state["paired_user_id"] = user_id
        logger.warning("telegram paired to chat %s user %s", chat_id, user_id)
        return True, "paired"

    def is_authorized(self, chat_id: Any, user_id: Any) -> bool:
        if not self.is_paired():
            return False
        return (str(chat_id) == str(self.state.get("paired_chat_id"))
                and str(user_id) == str(self.state.get("paired_user_id")))

    def note_unauthorized(self, chat_id: Any, user_id: Any) -> Dict[str, Any]:
        """Record an unauthorized access attempt; returns an audit payload."""
        self.state["unauthorized_hits"] = int(self.state.get("unauthorized_hits", 0)) + 1
        return {
            "event": "telegram.unauthorized",
            "chat_id": chat_id,
            "user_id": user_id,
            "total_hits": self.state["unauthorized_hits"],
            "note": "ignored; only the paired chat may interact",
        }

    def re_pair(self, chat_id: Any, user_id: Any) -> Tuple[bool, str]:
        """Break an existing pairing and pair anew (explicit /repair)."""
        self.state.pop("paired_chat_id", None)
        self.state.pop("paired_user_id", None)
        self.state["pending_confirms"] = {}
        return self.try_pair(chat_id, user_id)

    # ------------------------------------------------------------------
    # replay protection
    # ------------------------------------------------------------------
    def seen_update(self, update_id: int) -> bool:
        return update_id in self.state["processed_update_ids"]

    def mark_update(self, update_id: int) -> None:
        _bounded(self.state["processed_update_ids"], update_id)

    # ------------------------------------------------------------------
    # alert dedup
    # ------------------------------------------------------------------
    def alert_seen(self, event_id: str) -> bool:
        return event_id in self.state["alerted_event_ids"]

    def mark_alert(self, event_id: str) -> None:
        _bounded(self.state["alerted_event_ids"], event_id)

    # ------------------------------------------------------------------
    # rate limiting
    # ------------------------------------------------------------------
    def check_rate(self, chat_id: Any, mutating: bool = False,
                   now: Optional[float] = None) -> bool:
        """True if the message may proceed, False if throttled."""
        now = now if now is not None else time.time()
        key = "%s:%s" % (chat_id, "mut" if mutating else "read")
        bucket = self.state["rate_buckets"].setdefault(key, [])
        window = MUTATE_WINDOW_S if mutating else READ_WINDOW_S
        limit = MUTATE_LIMIT if mutating else READ_LIMIT
        bucket[:] = [t for t in bucket if now - t < window]
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    # ------------------------------------------------------------------
    # two-step confirmation
    # ------------------------------------------------------------------
    def new_confirm(self, action: str, payload: Dict[str, Any],
                    chat_id: Any, user_id: Any, request_id: str,
                    now: Optional[float] = None) -> str:
        """Park a mutating intent; returns the single-use confirm code."""
        now = now if now is not None else time.time()
        self._sweep_confirms(now)
        code = secrets.token_hex(CONFIRM_CODE_LEN // 2).upper()
        while code in self.state["pending_confirms"]:
            code = secrets.token_hex(CONFIRM_CODE_LEN // 2).upper()
        self.state["pending_confirms"][code] = {
            "action": action,
            "payload": payload,
            "chat_id": chat_id,
            "user_id": user_id,
            "request_id": request_id,
            "expires_at": now + CONFIRM_TTL_S,
            "created_at": now,
        }
        return code

    def claim_confirm(self, code: str, chat_id: Any, user_id: Any,
                      now: Optional[float] = None) -> Tuple[Optional[Dict[str, Any]], str]:
        """Claim a pending intent. Single-use; returns (intent, status)."""
        now = now if now is not None else time.time()
        code = (code or "").strip().upper()
        pending = self.state["pending_confirms"]
        intent = pending.pop(code, None)
        if intent is None:
            return None, "unknown"
        if str(intent.get("chat_id")) != str(chat_id) or str(intent.get("user_id")) != str(user_id):
            return None, "mismatch"
        if now > float(intent.get("expires_at", 0)):
            return None, "expired"
        return intent, "ok"

    def _sweep_confirms(self, now: float) -> None:
        pending = self.state["pending_confirms"]
        for code in [c for c, i in pending.items()
                     if now > float(i.get("expires_at", 0))]:
            pending.pop(code, None)

    # ------------------------------------------------------------------
    # idempotent request identity
    # ------------------------------------------------------------------
    @staticmethod
    def request_id(update_id: int, command: str, args: str) -> str:
        """Stable idempotency key for one user command.

        Redelivering the same Telegram update yields the same key, so the
        execution gateway replays the original decision instead of acting
        twice.
        """
        digest = hashlib.sha256(("%s|%s" % (command, args)).encode()).hexdigest()[:16]
        return "tg-%s-%s" % (update_id, digest)

    # ------------------------------------------------------------------
    # hard refusals
    # ------------------------------------------------------------------
    LIVE_WORDS = ("live", "mainnet", "real money")

    @classmethod
    def refuses_live(cls, text: str) -> bool:
        lowered = (" " + (text or "").lower() + " ")
        for ch in ("/", "_", "-", "."):
            lowered = lowered.replace(ch, " ")
        return any(" %s " % w in lowered for w in cls.LIVE_WORDS)
