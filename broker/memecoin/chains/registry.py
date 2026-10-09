"""Data-driven chain registry for the multi-chain memecoin provider.

Adding a new chain is a one-line ChainDef addition — no code changes.
Each entry declares:

  name            canonical lowercase name (used in config + env vars)
  chain_id        EVM chain id, or None for non-EVM L1s
  family          evm | solana | hyperliquid | sui | aptos
  native_currency gas / base asset ticker
  rpc_env_var     env var holding the chain's RPC URL (read at runtime)
  explorer_url    block explorer (display only)
  adapter         "evm" | "solana" | "hyperliquid" | None
                  None = tracked but no adapter implemented yet — the
                  factory raises a clear error instead of silently
                  misrouting.
  dexscreener_id  DexScreener chainId for public paper quotes, or None
                  (no public quote source -> paper fills are refused
                  until quotes are injected/configured).

Unknown chain names are rejected with CONFIG_INVALID listing the valid
names.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from broker import CONFIG_INVALID, BrokerError


@dataclass(frozen=True)
class ChainDef:
    name: str
    chain_id: Optional[int]
    family: str
    native_currency: str
    rpc_env_var: str
    explorer_url: str
    adapter: Optional[str]
    dexscreener_id: Optional[str] = None


CHAIN_REGISTRY: List[ChainDef] = [
    # -- EVM family ------------------------------------------------------
    ChainDef("ethereum", 1, "evm", "ETH", "MEMECOIN_RPC_ETHEREUM",
             "https://etherscan.io", "evm", "ethereum"),
    ChainDef("base", 8453, "evm", "ETH", "MEMECOIN_RPC_BASE",
             "https://basescan.org", "evm", "base"),
    ChainDef("arbitrum", 42161, "evm", "ETH", "MEMECOIN_RPC_ARBITRUM",
             "https://arbiscan.io", "evm", "arbitrum"),
    ChainDef("optimism", 10, "evm", "ETH", "MEMECOIN_RPC_OPTIMISM",
             "https://optimistic.etherscan.io", "evm", "optimism"),
    ChainDef("polygon", 137, "evm", "POL", "MEMECOIN_RPC_POLYGON",
             "https://polygonscan.com", "evm", "polygon"),
    ChainDef("bnb_chain", 56, "evm", "BNB", "MEMECOIN_RPC_BNB_CHAIN",
             "https://bscscan.com", "evm", "bsc"),
    ChainDef("avalanche", 43114, "evm", "AVAX", "MEMECOIN_RPC_AVALANCHE",
             "https://snowtrace.io", "evm", "avalanche"),
    ChainDef("linea", 59144, "evm", "ETH", "MEMECOIN_RPC_LINEA",
             "https://lineascan.build", "evm", "linea"),
    ChainDef("blast", 81457, "evm", "ETH", "MEMECOIN_RPC_BLAST",
             "https://blastscan.io", "evm", "blast"),
    ChainDef("zksync", 324, "evm", "ETH", "MEMECOIN_RPC_ZKSYNC",
             "https://explorer.zksync.io", "evm", "zksync"),
    ChainDef("scroll", 534352, "evm", "ETH", "MEMECOIN_RPC_SCROLL",
             "https://scrollscan.com", "evm", "scroll"),
    ChainDef("mantle", 5000, "evm", "MNT", "MEMECOIN_RPC_MANTLE",
             "https://explorer.mantle.xyz", "evm", "mantle"),
    ChainDef("berachain", 80094, "evm", "BERA", "MEMECOIN_RPC_BERACHAIN",
             "https://berascan.com", "evm", "berachain"),
    ChainDef("sei", 1329, "evm", "SEI", "MEMECOIN_RPC_SEI",
             "https://seitrace.com", "evm", "sei"),
    ChainDef("monad", 143, "evm", "MON", "MEMECOIN_RPC_MONAD",
             "https://monadscan.com", "evm", None),
    ChainDef("hyperevm", 999, "evm", "HYPE", "MEMECOIN_RPC_HYPEREVM",
             "https://explorer.hyperliquid.xyz", "evm", None),
    ChainDef("unichain", 130, "evm", "ETH", "MEMECOIN_RPC_UNICHAIN",
             "https://uniscan.xyz", "evm", None),
    ChainDef("ink", 57073, "evm", "ETH", "MEMECOIN_RPC_INK",
             "https://explorer.inkonchain.com", "evm", None),
    ChainDef("soneium", 1868, "evm", "ETH", "MEMECOIN_RPC_SONEIUM",
             "https://soneium.blockscout.com", "evm", None),
    # -- non-EVM families -------------------------------------------------
    ChainDef("solana", None, "solana", "SOL", "MEMECOIN_RPC_SOLANA",
             "https://solscan.io", "solana", None),
    ChainDef("hyperliquid", None, "hyperliquid", "HYPE",
             "MEMECOIN_RPC_HYPERLIQUID",
             "https://app.hyperliquid.xyz/explorer", "hyperliquid", None),
    # Tracked for future work: no adapter implemented yet.
    ChainDef("sui", None, "sui", "SUI", "MEMECOIN_RPC_SUI",
             "https://suiscan.xyz", None, None),
    ChainDef("aptos", None, "aptos", "APT", "MEMECOIN_RPC_APTOS",
             "https://aptoscan.com", None, None),
]

_CHAINS_BY_NAME: Dict[str, ChainDef] = {c.name: c for c in CHAIN_REGISTRY}


def chain_names() -> List[str]:
    """Canonical chain names, sorted."""
    return sorted(_CHAINS_BY_NAME)


def get_chain_def(name: str) -> ChainDef:
    """Look up a chain by name. Raises CONFIG_INVALID for unknown names."""
    key = (name or "").strip().lower()
    cdef = _CHAINS_BY_NAME.get(key)
    if cdef is None:
        raise BrokerError(
            CONFIG_INVALID,
            f"unknown memecoin chain {name!r} — valid chains: "
            f"{', '.join(chain_names())}")
    return cdef
