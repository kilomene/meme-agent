"""Telegram Bot API client for the paper-trading alert/control bot.

Design notes
------------
* stdlib-only (urllib), matching repo conventions elsewhere.
* The bot token is NEVER logged, raised in exceptions, or echoed into
  error bodies handed to callers. Every string that could contain the
  token passes through ``_redact`` before it reaches logs or messages.
* Transport failures raise :class:`TelegramError` and are caught by the
  daemon loop: Telegram going down must never crash the daemon, and this
  daemon going down must never affect the trading daemons.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger("forex_agent.daemon.telegram")

API_BASE = "https://api.telegram.org"

_REDACTED = "[REDACTED]"


class TelegramError(Exception):
    """A Telegram API or transport failure (token never included)."""


def redact(text: Any, token: Optional[str]) -> Any:
    """Strip every occurrence of the token from a string (best effort)."""
    if not token or not isinstance(text, str):
        return text
    return text.replace(token, _REDACTED)


class TelegramClient:
    """Minimal Bot API client: getMe / getUpdates / sendMessage / answerCallbackQuery."""

    def __init__(self, token: str, api_base: str = API_BASE, timeout: float = 30.0):
        if not token or not isinstance(token, str):
            raise ValueError("Telegram bot token is required")
        self._token = token
        self._base = api_base.rstrip("/")
        self._timeout = timeout

    # -- low level -----------------------------------------------------
    def _url(self, method: str) -> str:
        return "%s/bot%s/%s" % (self._base, self._token, method)

    def _request(self, method: str, params: Optional[Dict[str, Any]] = None,
                 use_get: bool = False) -> Dict[str, Any]:
        params = params or {}
        # Keep the token out of logs AND out of the URL query string.
        safe_params = {k: v for k, v in params.items()}
        try:
            if use_get:
                url = self._url(method) + "?" + urllib.parse.urlencode(safe_params)
                req = urllib.request.Request(
                    url, method="GET",
                    headers={"Connection": "close"})
            else:
                body = urllib.parse.urlencode(safe_params).encode("utf-8")
                req = urllib.request.Request(
                    self._url(method), data=body, method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded",
                             "Connection": "close"},
                )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", "replace")
            except Exception:
                raw = ""
            raise TelegramError(
                "Telegram API HTTP %s for %s: %s"
                % (exc.code, method, redact(raw[:500], self._token))
            ) from None
        except Exception as exc:
            raise TelegramError(
                "Telegram transport failed for %s: %s"
                % (method, redact(str(exc), self._token))
            ) from None
        try:
            payload = json.loads(raw)
        except ValueError:
            raise TelegramError(
                "Telegram API returned non-JSON for %s" % method) from None
        if not payload.get("ok"):
            desc = payload.get("description", "unknown error")
            raise TelegramError(
                "Telegram API error for %s: %s" % (method, redact(str(desc), self._token)))
        return payload.get("result")

    # -- bot API --------------------------------------------------------
    def get_me(self) -> Dict[str, Any]:
        """Authenticate the token; returns the bot user record."""
        return self._request("getMe", use_get=True)

    def get_updates(self, offset: Optional[int] = None,
                    timeout: int = 15, limit: int = 100) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"timeout": timeout, "limit": limit,
                                  "allowed_updates": json.dumps(["message", "callback_query"])}
        if offset is not None:
            params["offset"] = offset
        result = self._request("getUpdates", params)
        return result if isinstance(result, list) else []

    def send_message(self, chat_id: Any, text: str,
                     reply_markup: Optional[Dict[str, Any]] = None,
                     disable_notification: bool = False) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
            "disable_notification": disable_notification,
        }
        if reply_markup is not None:
            params["reply_markup"] = json.dumps(reply_markup)
        return self._request("sendMessage", params)

    def answer_callback_query(self, callback_query_id: str,
                              text: Optional[str] = None,
                              show_alert: bool = False) -> Dict[str, Any]:
        params: Dict[str, Any] = {"callback_query_id": callback_query_id,
                                  "show_alert": show_alert}
        if text:
            params["text"] = text
        return self._request("answerCallbackQuery", params)

    def delete_message(self, chat_id: Any, message_id: Any) -> Dict[str, Any]:
        """Best-effort delete (used to scrub secrets the user sent)."""
        return self._request("deleteMessage",
                             {"chat_id": chat_id, "message_id": message_id})

    def edit_message_text(self, chat_id: Any, message_id: Any,
                          text: str) -> Dict[str, Any]:
        """Edit a sent message's text (used by the card pulse). Edits are
        silent: Telegram never re-notifies on edit."""
        return self._request("editMessageText",
                             {"chat_id": chat_id, "message_id": message_id,
                              "text": text, "disable_web_page_preview": True})
