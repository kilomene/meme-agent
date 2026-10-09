"""Devnet-only Solana JSON-RPC client.

The client refuses to talk to anything that is not a Solana devnet
endpoint. This is a hard allowlist, not a suggestion: there is no
code path in this package that can send a transaction to mainnet.

HTTP via stdlib urllib only (repo convention — see
test_no_broker_sdk_import_anywhere_in_repo).
"""

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from broker import BROKER_UNAVAILABLE, CONFIG_INVALID, BrokerError

logger = logging.getLogger("broker.memecoin.execution.rpc")

DEFAULT_DEVNET_RPC = "https://api.devnet.solana.com"

# Hosts this client will ever talk to. api.devnet.solana.com is the
# public devnet endpoint; the wildcard covers provider devnet hosts.
_DEVNET_HOST_SUFFIXES = ("devnet.solana.com",)


def _ensure_devnet_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise BrokerError(CONFIG_INVALID,
                          "devnet RPC URL is empty — refusing to proceed")
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    if not host or not any(host == s or host.endswith("." + s)
                           for s in _DEVNET_HOST_SUFFIXES):
        raise BrokerError(
            CONFIG_INVALID,
            f"refusing non-devnet RPC host {host!r} — this client is "
            f"devnet-only by design")
    return url


def _post_json(url: str, payload: Dict[str, Any],
               timeout: float) -> Any:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "User-Agent": "forex-agent-memecoin-devnet/1.0",
                 "Connection": "close"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise BrokerError(BROKER_UNAVAILABLE,
                          f"devnet RPC HTTP {exc.code}",
                          detail={"reason": "rpc_http_error",
                                  "http_status": exc.code}) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise BrokerError(
            BROKER_UNAVAILABLE,
            f"devnet RPC unreachable ({type(exc).__name__}) — "
            f"check network egress to {urlparse(url).hostname}",
            detail={"reason": "rpc_unreachable"}) from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise BrokerError(BROKER_UNAVAILABLE,
                          "devnet RPC returned non-JSON response",
                          detail={"reason": "rpc_bad_response"}) from exc


class DevnetRPC:
    """Minimal JSON-RPC client pinned to Solana devnet."""

    def __init__(self, url: str = DEFAULT_DEVNET_RPC,
                 timeout: float = 30.0):
        self._url = _ensure_devnet_url(url)
        self._timeout = float(timeout)
        self._id = 0

    @property
    def url(self) -> str:
        return self._url

    def call(self, method: str, params: Optional[list] = None) -> Any:
        """Raw JSON-RPC call. Raises BROKER_UNAVAILABLE on transport or
        RPC-level errors (never leaks request bodies into the message)."""
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id,
                   "method": method, "params": params or []}
        body = _post_json(self._url, payload, self._timeout)
        if not isinstance(body, dict):
            raise BrokerError(BROKER_UNAVAILABLE,
                              "devnet RPC returned malformed response",
                              detail={"reason": "rpc_bad_response"})
        if body.get("error"):
            err = body["error"]
            raise BrokerError(BROKER_UNAVAILABLE,
                              "devnet RPC error: "
                              f"{str(err.get('message', err))[:160]}",
                              detail={"reason": "rpc_error"})
        return body.get("result")

    # -- convenience methods -------------------------------------------
    def get_health(self) -> str:
        return self.call("getHealth")

    def get_slot(self) -> int:
        return int(self.call("getSlot"))

    def get_latest_blockhash(self) -> Dict[str, Any]:
        res = self.call("getLatestBlockhash", [{"commitment": "confirmed"}])
        return res["value"]  # {"blockhash": ..., "lastValidBlockHeight": ...}

    def get_balance(self, pubkey: str) -> int:
        res = self.call("getBalance", [pubkey, {"commitment": "confirmed"}])
        return int(res["value"])  # lamports

    def send_transaction(self, b64_tx: str) -> str:
        res = self.call("sendTransaction",
                        [b64_tx, {"encoding": "base64",
                                 "preflightCommitment": "confirmed"}])
        return str(res)  # base58 signature

    def get_signature_statuses(self, signatures: List[str]) -> List[Any]:
        res = self.call("getSignatureStatuses",
                        [signatures, {"searchTransactionHistory": True}])
        return res["value"]

    def request_airdrop(self, pubkey: str,
                        lamports: int) -> str:
        return str(self.call("requestAirdrop", [pubkey, lamports]))
