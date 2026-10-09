"""Watchlist management for the Telegram control bot.

Lets the paired user add a coin to the monitoring list by pasting its
contract address (``/add <CA>``). The coin's display symbol is resolved
via DexScreener; the user may also pass an explicit symbol
(``/add <CA> <SYMBOL>``).

Two config locations are updated together (both are required — see the
NOTE in config/defaults.yaml):

* ``memecoin.symbols`` — SYMBOL -> mint registry (broker side)
* top-level ``symbols`` — the list market_monitor actually polls

Edits are surgical text insertions so comments and formatting in
defaults.yaml survive. The config layer hot-reloads on mtime change, so
market_monitor picks the new coin up on its next cycle — no restart.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("forex_agent.daemon.telegram.watchlist")

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

_DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
_HTTP_TIMEOUT = 12

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,12}$")


def validate_mint(mint: str) -> bool:
    """True if `mint` is a plausible Solana address (base58, 32 bytes)
    or EVM address (0x + 40 hex chars)."""
    if not isinstance(mint, str):
        return False
    if mint.startswith(("0x", "0X")):
        h = mint[2:]
        return len(h) == 40 and all(
            c in "0123456789abcdefABCDEF" for c in h)
    if not 32 <= len(mint) <= 44:
        return False
    if any(c not in _B58_ALPHABET for c in mint):
        return False
    try:
        return len(_b58decode(mint)) == 32
    except Exception:
        return False


def _b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58_ALPHABET.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def sanitize_symbol(symbol: str) -> Optional[str]:
    """Uppercase alphanumeric token symbol, 2-12 chars. None if unusable."""
    if not symbol:
        return None
    clean = re.sub(r"[^A-Za-z0-9]", "", symbol).upper()[:12]
    return clean if _SYMBOL_RE.match(clean or "") else None


def resolve_token(mint: str) -> Optional[Dict[str, str]]:
    """Resolve a Solana mint via DexScreener -> {symbol, name} or None.

    Never raises; network trouble just yields None and the caller falls
    back to an explicit symbol or a clear error.
    """
    try:
        req = urllib.request.Request(
            _DEXSCREENER_TOKEN_URL.format(mint=mint),
            headers={"User-Agent": "forex-agent/1.0",
                     "Connection": "close"},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        logger.warning("dexscreener resolve failed for %s: %s", mint[:8], exc)
        return None
    pairs = (data or {}).get("pairs") or []
    sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
    pool = sol_pairs or pairs
    if not pool:
        return None
    # Prefer the most liquid pool — likeliest canonical listing.
    pool.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
              reverse=True)
    base = (pool[0].get("baseToken") or {})
    symbol = sanitize_symbol(str(base.get("symbol") or ""))
    name = str(base.get("name") or "").strip()[:40]
    if not symbol:
        return None
    return {"symbol": symbol, "name": name or symbol}


def _defaults_path() -> str:
    from config.config import DEFAULTS_PATH  # noqa: PLC0415
    return str(DEFAULTS_PATH)


def _read_defaults() -> Tuple[List[str], Dict[str, Any]]:
    """Return (lines, parsed yaml) of defaults.yaml."""
    import yaml  # noqa: PLC0415
    path = _defaults_path()
    with open(path) as fh:
        text = fh.read()
    return text.splitlines(keepends=True), (yaml.safe_load(text) or {})


def current_watchlist() -> Tuple[List[str], Dict[str, str]]:
    """(monitoring symbols, {SYMBOL: mint} registry) from defaults.yaml."""
    _, parsed = _read_defaults()
    monitoring = list(parsed.get("symbols") or [])
    registry = dict((parsed.get("memecoin") or {}).get("symbols") or {})
    return monitoring, registry


def add_to_watchlist(symbol: str, mint: str) -> Tuple[bool, str]:
    """Surgically add SYMBOL->mint to defaults.yaml. Returns (ok, message).

    Updates BOTH the memecoin.symbols registry and the top-level symbols
    monitoring list. Fails closed on any structural surprise.
    """
    symbol = (symbol or "").upper()
    if not _SYMBOL_RE.match(symbol):
        return False, "bad symbol %r" % symbol
    if not validate_mint(mint):
        return False, "that doesn't look like a valid Solana or EVM address"

    lines, parsed = _read_defaults()
    monitoring = list(parsed.get("symbols") or [])
    registry = dict((parsed.get("memecoin") or {}).get("symbols") or {})

    if mint in registry.values():
        other = next(s for s, m in registry.items() if m == mint)
        return False, "already watched as %s" % other
    if symbol in monitoring or symbol in registry:
        return False, "%s is already on the watchlist" % symbol

    out = list(lines)
    if not _insert_registry_entry(out, symbol, mint):
        return False, "config structure changed — add it manually and tell me"
    if not _insert_monitoring_entry(out, symbol):
        return False, "config structure changed — add it manually and tell me"

    # Atomic write, then verify the result still parses with our entries.
    path = _defaults_path()
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".defaults.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.writelines(out)
        import yaml  # noqa: PLC0415
        with open(tmp) as fh:
            check = yaml.safe_load(fh.read()) or {}
        check_mon = list(check.get("symbols") or [])
        check_reg = dict((check.get("memecoin") or {}).get("symbols") or {})
        if symbol not in check_mon or check_reg.get(symbol) != mint:
            raise ValueError("verification failed")
        os.replace(tmp, path)
    except Exception as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        logger.error("watchlist write failed: %s", exc)
        return False, "couldn't save the config — nothing was changed"
    return True, "ok"


def _insert_registry_entry(lines: List[str], symbol: str, mint: str) -> bool:
    """Insert `    SYMBOL: "mint"` as the last entry of memecoin.symbols."""
    in_memecoin = False
    in_symbols = False
    memecoin_indent = None
    last_entry = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_memecoin = stripped.startswith("memecoin:")
            in_symbols = False
            continue
        if not in_memecoin:
            continue
        if not in_symbols:
            if indent == 2 and stripped.startswith("symbols:"):
                in_symbols = True
            continue
        if indent == 4 and re.match(r"^[A-Za-z0-9_]+:\s*\"[^\"]*\"", stripped):
            last_entry = i
        elif indent <= 2:
            break
    if last_entry is None:
        return False
    lines.insert(last_entry + 1, '    %s: "%s"\n' % (symbol, mint))
    return True


def _insert_monitoring_entry(lines: List[str], symbol: str) -> bool:
    """Insert `  - SYMBOL` as the last entry of the top-level symbols list."""
    in_list = False
    last_entry = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if in_list and not stripped:
                continue
            if in_list and stripped.startswith("#"):
                continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            if stripped == "symbols:":
                in_list = True
                continue
            if in_list:
                break
            continue
        if in_list:
            if indent == 2 and stripped.startswith("- "):
                last_entry = i
            elif indent <= 2 and stripped:
                break
    if last_entry is None:
        return False
    lines.insert(last_entry + 1, "  - %s\n" % symbol)
    return True
