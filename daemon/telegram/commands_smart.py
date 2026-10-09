"""Read-only smart-wallet PnL grading command.

This module only DEFINES the handler — it is NOT registered here.
The integration step registers it in the telegram daemon's command map
as ``"smartgrade"`` with a thin wrapper::

    def _cmd_smartgrade(ctx, chat_id, user_id, update_id, args):
        from daemon.telegram import commands_smart
        ctx.send(chat_id, commands_smart.cmd_smartgrade(ctx, args), None)

plus a help line ``/smartgrade — smart-wallet PnL grades (trailing 30d)``.

``cmd_smartgrade(ctx, args) -> str`` returns the reply text (never sends).
``build_grade_text`` is the pure, easily-testable builder underneath.
"""

from __future__ import annotations

import time
from typing import Any, Dict


def build_grade_text(registry: Dict[str, Dict[str, Any]],
                     launch_state: Dict[str, Any],
                     degen: Any = None) -> str:
    """Pure text builder: registry + launch_scanner state -> reply string."""
    from daemon.launch_scanner import smartgrade
    if not registry:
        return ("No smart wallets tracked yet.\n"
                "Add one: /smartadd <wallet-address> [label]")
    try:
        smartgrade.reconcile(launch_state, degen)
    except Exception:
        pass
    return smartgrade.format_grades(registry, degen)


def cmd_smartgrade(ctx, args) -> str:
    """``/smartgrade`` — per-wallet outcome grades, trailing 30d.

    Read-only: reconcile refreshes the grading ledger from the
    milestone tracker, but never adds/removes wallets or touches gates.
    Returns the message text; the caller sends it.
    """
    try:
        from daemon.launch_scanner import smartmoney
    except Exception as exc:
        return "⚠️ smart-money module unavailable: %s" % exc
    wallets = smartmoney.load_registry()
    try:
        from daemon.common import get_daemon_state, get_store
        launch_state = get_daemon_state(get_store(),
                                        "launch_scanner") or {}
    except Exception as exc:
        return "⚠️ couldn't read scanner state: %s" % exc
    _ = time.time()  # noqa: F841 (kept for a future quiet-hours check)
    _ = args
    return build_grade_text(wallets, launch_state)
