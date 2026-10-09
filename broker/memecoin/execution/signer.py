"""Signing for devnet execution via the runtime key pattern.

The secret is read TRANSIENTLY from the configured WalletKeyProvider
(broker/memecoin/wallet.py) and never stored on any long-lived
object, never logged, never appears in exceptions or repr.

Accepted secret formats (whatever the provider returns):
  * JSON array of 64 ints — the Solana CLI id.json format
  * base58 of the 64-byte secret key (seed || pubkey)
  * base58 of the 32-byte seed

Requires the ``solders`` package.
"""

import base64
import json
import logging
from typing import Optional

from broker import CREDENTIALS_INVALID, BrokerError
from broker.memecoin.wallet import WalletKeyProvider

logger = logging.getLogger("broker.memecoin.execution.signer")

try:
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    _SOLDERS = True
except ImportError:  # pragma: no cover - dependency check at runtime
    Keypair = None  # type: ignore
    Pubkey = None  # type: ignore
    _SOLDERS = False

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        try:
            digit = _B58_ALPHABET.index(ch)
        except ValueError as exc:
            raise BrokerError(CREDENTIALS_INVALID,
                              "wallet secret is not valid base58"
                              ) from exc
        n = n * 58 + digit
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


class DevnetSigner:
    """Builds a solders Keypair from the provider secret, on demand."""

    def __init__(self, key_provider: WalletKeyProvider,
                 chain: str = "solana"):
        if not _SOLDERS:
            raise BrokerError(
                CREDENTIALS_INVALID,
                "devnet signing needs the 'solders' package "
                "(pip install solders)")
        self._provider = key_provider
        self._chain = chain

    def _load_secret(self) -> bytes:
        secret = self._provider.get_key(self._chain)
        if not secret or not str(secret).strip():
            raise BrokerError(
                CREDENTIALS_INVALID,
                "no wallet secret available from "
                f"{self._provider.describe_source(self._chain)} — "
                "devnet execution needs a key")
        text = str(secret).strip()
        # JSON array format (Solana CLI id.json).
        if text.startswith("["):
            try:
                arr = json.loads(text)
            except ValueError as exc:
                raise BrokerError(CREDENTIALS_INVALID,
                                  "wallet secret JSON is malformed"
                                  ) from exc
            if (not isinstance(arr, list) or len(arr) != 64
                    or not all(isinstance(x, int) and 0 <= x < 256
                               for x in arr)):
                raise BrokerError(
                    CREDENTIALS_INVALID,
                    "wallet secret JSON must be an array of 64 byte values")
            return bytes(arr)
        raw = _b58decode(text)
        if len(raw) not in (32, 64):
            raise BrokerError(
                CREDENTIALS_INVALID,
                "wallet secret must decode to 32 (seed) or 64 (secret key) "
                f"bytes, got {len(raw)}")
        return raw

    def keypair(self):
        """Build the Keypair now; the secret bytes are dropped on return."""
        raw = self._load_secret()
        try:
            if len(raw) == 64:
                kp = Keypair.from_bytes(raw)
            else:
                kp = Keypair.from_seed(raw)
        except Exception as exc:
            raise BrokerError(CREDENTIALS_INVALID,
                              "wallet secret could not be parsed as a "
                              "Solana keypair") from exc
        return kp

    def pubkey_str(self) -> str:
        """The public key (safe to log)."""
        return str(self.keypair().pubkey())

    @staticmethod
    def sign_transaction(tx, keypair, blockhash) -> bytes:
        """Sign a solders Transaction in place; returns the signature."""
        from solders.hash import Hash
        tx.partial_sign([keypair], Hash.from_string(blockhash))
        sig = tx.signatures[0]
        return bytes(sig)

    def __repr__(self):  # never leak key material
        return (f"{type(self).__name__}(chain={self._chain!r}, "
                f"source={self._provider.describe_source(self._chain)!r})")
