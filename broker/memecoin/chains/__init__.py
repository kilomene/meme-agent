"""Multi-chain plumbing for the memecoin provider.

`get_chain_adapter(name, ...)` is the single entry point: it resolves a
chain name through the registry and builds the right adapter. Unknown
chain names are rejected with CONFIG_INVALID; chains tracked in the
registry with no adapter implemented yet (sui, aptos) raise a clear
"not implemented" error instead of being silently misrouted.
"""

from broker import CONFIG_INVALID, BrokerError
from broker.memecoin.chains.base import ChainAdapter, PaperBook
from broker.memecoin.chains.registry import (
    CHAIN_REGISTRY,
    ChainDef,
    chain_names,
    get_chain_def,
)
from broker.memecoin.chains.evm import EvmChainAdapter
from broker.memecoin.chains.solana import SolanaChainAdapter
from broker.memecoin.chains.hyperliquid import HyperliquidAdapter

_ADAPTER_CLASSES = {
    "evm": EvmChainAdapter,
    "solana": SolanaChainAdapter,
    "hyperliquid": HyperliquidAdapter,
}

__all__ = [
    "ChainAdapter",
    "PaperBook",
    "ChainDef",
    "CHAIN_REGISTRY",
    "chain_names",
    "get_chain_def",
    "get_chain_adapter",
    "EvmChainAdapter",
    "SolanaChainAdapter",
    "HyperliquidAdapter",
]


def get_chain_adapter(name: str, *, data_dir: str, mode: str = "paper",
                      key_provider=None, armed: bool = False,
                      **kwargs) -> ChainAdapter:
    """Build the ChainAdapter for a registry chain name.

    Raises:
      BrokerError(CONFIG_INVALID): unknown chain name, or the chain's
        family has no adapter implemented yet.
    """
    cdef = get_chain_def(name)  # CONFIG_INVALID on unknown names
    if not cdef.adapter:
        raise BrokerError(
            CONFIG_INVALID,
            f"no chain adapter implemented for family {cdef.family!r} "
            f"(chain {cdef.name!r}) — the chain is tracked in the registry "
            f"for future work, but cannot be used yet")
    cls = _ADAPTER_CLASSES.get(cdef.adapter)
    if cls is None:  # defensive: registry/adapter table out of sync
        raise BrokerError(
            CONFIG_INVALID,
            f"registry adapter {cdef.adapter!r} for chain {cdef.name!r} "
            f"is not wired up")
    return cls(cdef, data_dir=data_dir, mode=mode,
               key_provider=key_provider, armed=armed, **kwargs)
