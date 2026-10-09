"""Transaction builders for devnet execution.

Requires the ``solders`` package. All builders produce UNSIGNED
solders Transactions; signing happens in signer.py so key material
stays in one place.
"""

import base64
import logging
from typing import List

from broker import BROKER_UNAVAILABLE, BrokerError

logger = logging.getLogger("broker.memecoin.execution.tx")

try:
    from solders.hash import Hash
    from solders.instruction import Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.transaction import Transaction
    _SOLDERS = True
except ImportError:  # pragma: no cover
    _SOLDERS = False


def _require_solders() -> None:
    if not _SOLDERS:
        raise BrokerError(BROKER_UNAVAILABLE,
                          "devnet execution needs the 'solders' package "
                          "(pip install solders)")


def pubkey(s: str):
    """Parse a base58 public key, with a clean error on garbage."""
    _require_solders()
    try:
        return Pubkey.from_string(s)
    except Exception as exc:
        raise BrokerError(BROKER_UNAVAILABLE,
                          f"invalid Solana public key {s!r}") from exc


def build_transfer_tx(from_pubkey: str, to_pubkey: str,
                      lamports: int, blockhash: str):
    """Unsigned SOL transfer transaction."""
    _require_solders()
    if lamports <= 0:
        raise BrokerError(BROKER_UNAVAILABLE,
                          f"transfer amount must be positive, got {lamports}")
    src = pubkey(from_pubkey)
    dst = pubkey(to_pubkey)
    try:
        blockhash_h = Hash.from_string(blockhash)
    except Exception as exc:
        raise BrokerError(BROKER_UNAVAILABLE,
                          "invalid blockhash for transfer") from exc
    ix = transfer(TransferParams(from_pubkey=src, to_pubkey=dst,
                                 lamports=int(lamports)))
    msg = Message.new_with_blockhash([ix], src, blockhash_h)
    return Transaction.new_unsigned(msg)


def build_tx(instructions: List, payer: str, blockhash: str):
    """Unsigned transaction from arbitrary instructions."""
    _require_solders()
    if not instructions:
        raise BrokerError(BROKER_UNAVAILABLE,
                          "cannot build a transaction with no instructions")
    payer_k = pubkey(payer)
    try:
        blockhash_h = Hash.from_string(blockhash)
    except Exception as exc:
        raise BrokerError(BROKER_UNAVAILABLE,
                          "invalid blockhash for transaction") from exc
    msg = Message.new_with_blockhash(instructions, payer_k, blockhash_h)
    return Transaction.new_unsigned(msg)


def tx_to_base64(tx) -> str:
    """Serialize a SIGNED transaction for sendTransaction."""
    _require_solders()
    return base64.b64encode(bytes(tx)).decode("ascii")


def signature_str(tx) -> str:
    """Base58 signature of a signed transaction (safe to log)."""
    _require_solders()
    if not tx.is_signed() or not tx.signatures:
        raise BrokerError(BROKER_UNAVAILABLE,
                          "transaction is not signed yet")
    return str(tx.signatures[0])
