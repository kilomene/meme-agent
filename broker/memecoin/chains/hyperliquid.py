"""Hyperliquid chain adapter skeleton.

Hyperliquid has its own API shape: EVM-style 0x addresses for wallets,
but spot markets are addressed by coin symbol (e.g. "HYPE", "PURR").
Paper quotes come from the public Hyperliquid info endpoint
(`POST /info {"type": "allMids"}` — mid prices in USD), stdlib urllib
only, graceful degradation. Tests inject `quotes` instead.

Paper settlement is in USDC at the coin's USD mid — which matches
Hyperliquid's own USDC-margined model. Live is fail-closed like every
other chain (armed key required, then still BROKER_UNAVAILABLE).
"""

import json
import logging
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Union

from broker.memecoin.chains.base import ChainAdapter

logger = logging.getLogger("broker.memecoin.chains.hyperliquid")

_HL_INFO_URL = "https://api.hyperliquid.xyz/info"
_HTTP_TIMEOUT = 10

_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_COIN_RE = re.compile(r"^[A-Z0-9]{1,12}$")

QuotesSource = Union[Dict[str, float], Callable[[str], Optional[float]]]


def _http_post_json(url: str, payload: dict):
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"User-Agent": "forex-agent/memecoin-chains",
                     "Content-Type": "application/json",
                     "Accept": "application/json",
                     "Connection": "close"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError,
            ValueError, json.JSONDecodeError) as exc:
        logger.warning("hyperliquid market data fetch failed: %s", exc)
        return None


class HyperliquidAdapter(ChainAdapter):
    chain_name = "hyperliquid"
    chain_id = None
    family = "hyperliquid"
    native_currency = "HYPE"

    def __init__(self, chain_def=None, *, data_dir: str, mode: str = "paper",
                 key_provider=None, armed: bool = False,
                 quotes: Optional[QuotesSource] = None,
                 start_native: float = 1.0, start_usdc: float = 1000.0):
        self._quotes = quotes
        super().__init__(chain_def, data_dir=data_dir, mode=mode,
                         key_provider=key_provider, armed=armed,
                         start_native=start_native, start_usdc=start_usdc)

    # -- identity ------------------------------------------------------
    def validate_address(self, addr: str) -> bool:
        """Hyperliquid wallets use EVM-style 0x addresses."""
        return bool(addr) and bool(_EVM_ADDRESS_RE.match(addr.strip()))

    def validate_token(self, token: str) -> bool:
        """Spot markets are addressed by coin symbol, e.g. HYPE, PURR."""
        return bool(token) and bool(_COIN_RE.match(token.strip().upper()))

    # -- quotes ----------------------------------------------------------
    def _public_mid_usd(self, coin: str) -> Optional[float]:
        """Coin USD mid from the public Hyperliquid info endpoint.
        Returns None on any failure (never raises)."""
        data = _http_post_json(_HL_INFO_URL, {"type": "allMids"})
        if not isinstance(data, dict):
            return None
        raw = data.get(coin.upper())
        try:
            price = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None

    def get_quote(self, token: str) -> Optional[dict]:
        coin = token.strip().upper()
        price: Optional[float] = None
        if isinstance(self._quotes, dict):
            price = self._quotes.get(coin)
        elif callable(self._quotes):
            try:
                price = self._quotes(coin)
            except Exception as exc:
                logger.warning("injected quote source failed for %s: %s",
                               coin, exc)
                price = None
        else:
            price = self._public_mid_usd(coin)
        if not price or price <= 0:
            return None
        return {"token": coin, "price_usd": float(price),
                "chain": self.chain_name,
                "ts": datetime.now(timezone.utc).isoformat()}
