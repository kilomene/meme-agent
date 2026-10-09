"""Solana chain adapter — reuses the existing keyless public market-data
transport (broker/memecoin/transport.py: DexScreener quotes) instead of
duplicating it.

Note: the top-level MemecoinAdapter already serves Solana through its
native SOL-settled engine; this ChainAdapter is the multi-chain
plumbing's USDC-settled Solana book (same quote source, per-chain
state under <data_dir>/chains/solana/). It is used when chain
adapters are addressed directly, and keeps the door open for
consolidating the engines later.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Union

from broker.memecoin.chains.base import ChainAdapter
from broker.memecoin.transport import MemecoinMarketData

logger = logging.getLogger("broker.memecoin.chains.solana")

# USDC mint on Solana (SPL). Registered so USDC balances can be quoted.
USDC_MINT_SOLANA = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

_BASE58_ALPHABET = set(
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]+$")

QuotesSource = Union[Dict[str, float], Callable[[str], Optional[float]]]


class SolanaChainAdapter(ChainAdapter):
    chain_name = "solana"
    chain_id = None
    family = "solana"
    native_currency = "SOL"

    def __init__(self, chain_def=None, *, data_dir: str, mode: str = "paper",
                 key_provider=None, armed: bool = False,
                 market_data: Optional[MemecoinMarketData] = None,
                 quotes: Optional[QuotesSource] = None,
                 start_native: float = 1.0, start_usdc: float = 1000.0):
        self._market = market_data or MemecoinMarketData()
        self._market.register("USDC", USDC_MINT_SOLANA)
        self._quotes = quotes
        super().__init__(chain_def, data_dir=data_dir, mode=mode,
                         key_provider=key_provider, armed=armed,
                         start_native=start_native, start_usdc=start_usdc)

    # -- identity ------------------------------------------------------
    def validate_address(self, addr: str) -> bool:
        if not addr or not (32 <= len(addr) <= 44):
            return False
        return bool(_BASE58_RE.match(addr)) and set(addr) <= _BASE58_ALPHABET

    # -- quotes ----------------------------------------------------------
    def _transport_price_usd(self, token: str) -> Optional[float]:
        """USD price via the shared DexScreener transport. Tokens may be
        given as mint addresses (registered on the fly) or as symbols
        already registered on the transport."""
        mint = token.strip()
        if self.validate_address(mint) and not self._market.mint_for(mint):
            # Register the mint under itself so the transport can quote it.
            self._market.register(mint, mint)
        q = self._market.get_quote(
            mint if self._market.mint_for(mint) else token)
        if not q or not q.get("price_usd"):
            return None
        try:
            price = float(q["price_usd"])
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None

    def get_quote(self, token: str) -> Optional[dict]:
        addr = token.strip()
        price: Optional[float] = None
        if isinstance(self._quotes, dict):
            price = self._quotes.get(addr)
        elif callable(self._quotes):
            try:
                price = self._quotes(addr)
            except Exception as exc:
                logger.warning("injected quote source failed for %s: %s",
                               addr, exc)
                price = None
        else:
            price = self._transport_price_usd(addr)
        if not price or price <= 0:
            return None
        return {"token": addr, "price_usd": float(price),
                "chain": self.chain_name,
                "ts": datetime.now(timezone.utc).isoformat()}
