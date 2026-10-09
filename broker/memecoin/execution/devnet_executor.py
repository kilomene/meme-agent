"""DevnetExecutor — the real (devnet-only) execution path.

Pipeline per action:

  1. fetch a recent blockhash        (DevnetRPC)
  2. build the unsigned transaction   (tx.py)
  3. sign with the operator's keypair (signer.py — secret stays transient)
  4. send + wait for confirmation     (DevnetRPC)

What it can do today:
  * get_balance_sol()      — devnet SOL balance of the wallet
  * transfer_sol(to, sol)  — real devnet SOL transfer, confirmed

What it deliberately cannot do:
  * anything on mainnet (the RPC client refuses non-devnet hosts)
  * token swaps (no memecoin DEX liquidity exists on devnet; the
    Jupiter client in jupiter.py is mainnet-API and is NOT wired here)

Every transfer is confirmed before returning: the method returns the
signature only after the network reports it confirmed (or raises
BROKER_UNAVAILABLE on timeout/failure).
"""

import logging
import time
from typing import Optional

from broker import BROKER_UNAVAILABLE, CREDENTIALS_INVALID, BrokerError
from broker.memecoin.execution import rpc as rpc_mod
from broker.memecoin.execution import signer as signer_mod
from broker.memecoin.execution import tx as tx_mod
from broker.memecoin.wallet import WalletKeyProvider

logger = logging.getLogger("broker.memecoin.execution.executor")

LAMPORTS_PER_SOL = 1_000_000_000


class DevnetExecutor:
    """Real devnet execution. Devnet only, by construction."""

    def __init__(self,
                 key_provider: WalletKeyProvider,
                 rpc_url: str = rpc_mod.DEFAULT_DEVNET_RPC,
                 chain: str = "solana",
                 expected_pubkey: str = ""):
        self._rpc = rpc_mod.DevnetRPC(rpc_url)  # raises on non-devnet URL
        self._signer = signer_mod.DevnetSigner(key_provider, chain)
        self._expected_pubkey = (expected_pubkey or "").strip()
        self._wallet_pubkey: Optional[str] = None

    @property
    def wallet_pubkey(self) -> Optional[str]:
        return self._wallet_pubkey

    def connect(self) -> str:
        """Health-check devnet, load the keypair, verify identity.

        Returns the wallet public key (base58). Fails closed when:
          * devnet is unreachable
          * no key is available from the provider
          * the key's pubkey != expected_pubkey (when configured)
        """
        health = self._rpc.get_health()
        if health != "ok":
            raise BrokerError(BROKER_UNAVAILABLE,
                              f"devnet RPC unhealthy: {health!r}")
        pubkey = self._signer.pubkey_str()
        if self._expected_pubkey and pubkey != self._expected_pubkey:
            raise BrokerError(
                CREDENTIALS_INVALID,
                "wallet key does not match MEMECOIN_WALLET_PUBKEY — "
                "refusing to sign with an unexpected key")
        self._wallet_pubkey = pubkey
        slot = self._rpc.get_slot()
        logger.info("DevnetExecutor connected: wallet=%s slot=%d",
                    pubkey, slot)
        return pubkey

    def get_balance_sol(self) -> float:
        self._require_connected()
        lamports = self._rpc.get_balance(self._wallet_pubkey)
        return lamports / LAMPORTS_PER_SOL

    def transfer_sol(self, to_pubkey: str, sol: float,
                     confirm_timeout: float = 60.0) -> str:
        """Send SOL on devnet and wait for confirmation.

        Returns the confirmed transaction signature (base58).
        """
        self._require_connected()
        if sol <= 0:
            raise BrokerError(BROKER_UNAVAILABLE,
                              f"transfer amount must be positive, got {sol}")
        lamports = int(round(sol * LAMPORTS_PER_SOL))
        if lamports <= 0:
            raise BrokerError(BROKER_UNAVAILABLE,
                              f"transfer amount {sol} SOL is below 1 lamport")

        bh = self._rpc.get_latest_blockhash()
        blockhash = bh["blockhash"]
        unsigned = tx_mod.build_transfer_tx(
            self._wallet_pubkey, to_pubkey, lamports, blockhash)
        kp = self._signer.keypair()
        try:
            signer_mod.DevnetSigner.sign_transaction(unsigned, kp, blockhash)
            sig = tx_mod.signature_str(unsigned)
            b64 = tx_mod.tx_to_base64(unsigned)
        finally:
            # Drop the keypair reference immediately; the secret bytes
            # live only inside this call frame.
            del kp
        sent = self._rpc.send_transaction(b64)
        if sent != sig:
            logger.warning("RPC echoed signature %s != local %s", sent, sig)
        self._await_confirmation(sig, confirm_timeout)
        logger.info("devnet transfer confirmed: %s -> %s %.9f SOL sig=%s",
                    self._wallet_pubkey, to_pubkey, sol, sig)
        return sig

    # -- internals -----------------------------------------------------
    def _require_connected(self) -> None:
        if not self._wallet_pubkey:
            raise BrokerError(BROKER_UNAVAILABLE,
                              "DevnetExecutor not connected — call "
                              "connect() first")

    def _await_confirmation(self, signature: str,
                            timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            statuses = self._rpc.get_signature_statuses([signature])
            st = statuses[0] if statuses else None
            if st:
                if st.get("err"):
                    raise BrokerError(
                        BROKER_UNAVAILABLE,
                        "devnet transaction failed on-chain: "
                        f"{str(st['err'])[:160]}",
                        detail={"reason": "tx_failed",
                                "signature": signature})
                conf = st.get("confirmationStatus")
                if conf in ("confirmed", "finalized"):
                    return
            time.sleep(2.0)
        raise BrokerError(BROKER_UNAVAILABLE,
                          f"devnet transaction {signature} not confirmed "
                          f"within {timeout:.0f}s",
                          detail={"reason": "confirm_timeout",
                                  "signature": signature})
