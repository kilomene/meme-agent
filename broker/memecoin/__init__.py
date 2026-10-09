"""Memecoin broker provider — multi-chain memecoin trading backend.

Self-contained: paper trading engine, JSONL fill journal, kill-switch
file coupling, idempotency store, and keyless public market-data
transports (stdlib urllib only — no new dependencies). Solana is the
native SOL-settled engine; additional chains are served by USDC-settled
ChainAdapters under broker/memecoin/chains/ (see RUNTIME.md).

Wallet secrets are never handled here beyond the runtime key pattern
(broker/memecoin/wallet.py): keys are read at runtime from an env var
or a 0600 file, used transiently, and never logged. Paper mode needs
no key. See WALLET_SECURITY.md.
"""

from broker.memecoin.adapter import MemecoinAdapter
from broker.memecoin.transport import MemecoinMarketData
from broker.memecoin.wallet import (
    EnvKeyProvider,
    FileKeyProvider,
    NullKeyProvider,
    WalletKeyProvider,
    build_key_provider,
)

__all__ = [
    "MemecoinAdapter",
    "MemecoinMarketData",
    "WalletKeyProvider",
    "NullKeyProvider",
    "EnvKeyProvider",
    "FileKeyProvider",
    "build_key_provider",
]
