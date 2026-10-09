"""Generic EVM chain adapter — one class serves every EVM-family chain.

Per-chain behavior comes from the ChainDef (chain id, native currency,
DexScreener chain id). The RPC URL is read from the chain's env var at
runtime (reserved for the future live path; paper mode needs no RPC).

Paper quotes: DexScreener token lookup filtered to this chain
(stdlib urllib, graceful degradation — any failure yields None and the
paper swap is refused). Tests inject `quotes` instead (dict or callable)
so no network is ever touched.

Paper settlement is in USDC at the token's USD price; the virtual
native balance is tracked for future gas accounting.
"""

import json
import logging
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Union

from broker.memecoin.chains.base import ChainAdapter

logger = logging.getLogger("broker.memecoin.chains.evm")

_DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{addr}"
_HTTP_TIMEOUT = 10

_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

QuotesSource = Union[Dict[str, float], Callable[[str], Optional[float]]]


def _http_get_json(url: str):
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "forex-agent/memecoin-chains",
                          "Accept": "application/json",
                          "Connection": "close"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError,
            ValueError, json.JSONDecodeError) as exc:
        logger.warning("evm market data fetch failed for %s: %s", url, exc)
        return None


class EvmChainAdapter(ChainAdapter):
    """Paper trading on any EVM chain, driven by its ChainDef."""

    family = "evm"

    def __init__(self, chain_def, *, data_dir: str, mode: str = "paper",
                 key_provider=None, armed: bool = False,
                 quotes: Optional[QuotesSource] = None,
                 start_native: float = 1.0, start_usdc: float = 1000.0):
        self.chain_name = chain_def.name
        self.chain_id = chain_def.chain_id
        self.native_currency = chain_def.native_currency
        self._dexscreener_id = chain_def.dexscreener_id
        self._rpc_env_var = chain_def.rpc_env_var
        self._quotes = quotes
        super().__init__(chain_def, data_dir=data_dir, mode=mode,
                         key_provider=key_provider, armed=armed,
                         start_native=start_native, start_usdc=start_usdc)

    # -- identity ------------------------------------------------------
    def validate_address(self, addr: str) -> bool:
        return bool(addr) and bool(_EVM_ADDRESS_RE.match(addr.strip()))

    @property
    def rpc_url(self) -> Optional[str]:
        """Chain RPC URL from the environment (live path only)."""
        import os
        return os.environ.get(self._rpc_env_var) or None

    # -- quotes ----------------------------------------------------------
    def _dexscreener_price_usd(self, token: str) -> Optional[float]:
        """Token USD price via DexScreener, filtered to this chain.
        Returns None on any failure (never raises)."""
        if not self._dexscreener_id:
            return None
        data = _http_get_json(_DEXSCREENER_TOKEN_URL.format(addr=token))
        if not data:
            return None
        pairs = [p for p in (data.get("pairs") or [])
                 if str(p.get("chainId", "")).lower() == self._dexscreener_id]
        if not pairs:
            return None
        pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0),
                   reverse=True)
        try:
            price = float(pairs[0].get("priceUsd") or 0.0)
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None

    def get_quote(self, token: str) -> Optional[dict]:
        addr = token.strip()
        price: Optional[float] = None
        if isinstance(self._quotes, dict):
            price = self._quotes.get(addr.lower())
            if price is None:
                # allow checksummed-key dicts too
                price = self._quotes.get(addr)
        elif callable(self._quotes):
            try:
                price = self._quotes(addr)
            except Exception as exc:  # injected source misbehaved
                logger.warning("injected quote source failed for %s: %s",
                               addr, exc)
                price = None
        else:
            price = self._dexscreener_price_usd(addr)
        if not price or price <= 0:
            return None
        return {"token": addr, "price_usd": float(price),
                "chain": self.chain_name,
                "ts": datetime.now(timezone.utc).isoformat()}
