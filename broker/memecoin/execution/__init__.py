"""Devnet execution for the memecoin provider (testing only).

This package implements a REAL Solana devnet transaction pipeline:

  rpc.py             Devnet-only JSON-RPC client (devnet URL allowlist)
  signer.py          Keypair loading + signing via WalletKeyProvider
  tx.py              Transaction builders (SOL transfer, generic)
  devnet_executor.py DevnetExecutor: balance, transfer, confirm
  jupiter.py         Jupiter quote/swap client (mainnet API; devnet swaps
                     are not supported by Jupiter — kept for the future
                     live path, never used by the devnet executor)

Design rules (see broker/memecoin/RUNTIME.md):

  * DEVNET ONLY. DevnetRPC refuses any URL that is not a devnet
    endpoint, and DevnetExecutor refuses to construct anything else.
    There is no mainnet code path here — the future live switch is a
    separate, deliberate build, never a flag flip on this module.
  * The wallet SECRET is never logged, never stored on a long-lived
    object, and only ever read transiently from the configured
    WalletKeyProvider (broker/memecoin/wallet.py).
  * Dependency: the ``solders`` package (pip install solders).

On devnet there is no memecoin DEX liquidity, so the devnet executor
moves the SOL leg of a trade as a real on-chain SOL transfer (to self
by default — proving sign -> send -> confirm). Token quantities stay
virtual. This is a pipeline test, not a trading strategy.
"""

from broker.memecoin.execution.devnet_executor import DevnetExecutor
from broker.memecoin.execution.rpc import (
    DEFAULT_DEVNET_RPC,
    DevnetRPC,
)
from broker.memecoin.execution.signer import DevnetSigner

__all__ = ["DEFAULT_DEVNET_RPC", "DevnetExecutor", "DevnetRPC", "DevnetSigner"]
