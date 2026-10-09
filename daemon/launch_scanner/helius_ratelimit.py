"""Shared Helius rate-limit guard for the launch scanner.

Both ``smartmoney`` and ``deployers`` hit the same Helius RPC endpoint with
the same API key, so they share one rate-limit bucket — and one cooldown.
Without coordination, each module retries blindly on HTTP 429 and the
combined hammering keeps the bucket empty (2026-10-07: 42k+ 429s in the
log, smart-money signal degraded to a limp).

Protocol (fail-soft, never raises):
- Before any Helius call: ``cooldown_remaining()`` — >0 means skip the
  call and return None, exactly like a failed request today.
- After a call: ``report_ratelimited()`` on HTTP 429, ``report_success()``
  on any successful response. Anything else (timeouts, 5xx, no key)
  leaves the cooldown untouched — only 429 engages the brake.

Backoff: 60s, doubling to a 600s cap, reset on first success. All state is
module-level (the daemons are single-process); stdlib only so tests can
import it freely.
"""

from __future__ import annotations

import time

_BASE_BACKOFF_S = 60.0
_MAX_BACKOFF_S = 600.0

_cooldown_until = 0.0
_backoff_s = _BASE_BACKOFF_S


def cooldown_remaining(now: Optional[float] = None) -> float:
    """Seconds left on the shared cooldown, 0 when clear."""
    t = time.time() if now is None else now
    return max(0.0, _cooldown_until - t)


def report_ratelimited(now: Optional[float] = None) -> float:
    """Engage/extend the cooldown. Returns the cooldown seconds set."""
    global _cooldown_until, _backoff_s
    t = time.time() if now is None else now
    _cooldown_until = t + _backoff_s
    set_for = _backoff_s
    _backoff_s = min(_MAX_BACKOFF_S, _backoff_s * 2.0)
    return set_for


def report_success() -> None:
    """A call got through — reset the backoff ladder."""
    global _backoff_s
    _backoff_s = _BASE_BACKOFF_S


def reset_for_tests() -> None:
    """Test hook: clear cooldown and backoff state."""
    global _cooldown_until, _backoff_s
    _cooldown_until = 0.0
    _backoff_s = _BASE_BACKOFF_S
