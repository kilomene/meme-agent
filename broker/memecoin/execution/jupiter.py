"""Jupiter swap API client (NOT used by the devnet executor).

Jupiter's public swap API serves mainnet. There is no devnet swap
liquidity for memecoins, so this client exists for the FUTURE live
path only — it is deliberately not wired into DevnetExecutor and not
reachable from adapter devnet mode.

Kept thin and honest: quote -> swap-transaction, both against the
public Jupiter v6 API. Any future live wiring must go through the
same deliberate gates as everything else (paper soak, devnet
validation, explicit per-chain arming, review).

HTTP via stdlib urllib only (repo convention).
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict

from broker import BROKER_UNAVAILABLE, BrokerError

logger = logging.getLogger("broker.memecoin.execution.jupiter")

DEFAULT_JUPITER_BASE = "https://quote-api.jup.ag"


def _get_json(url: str, params: Dict[str, str],
              timeout: float) -> Any:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{url}?{qs}",
        headers={"User-Agent": "forex-agent-memecoin-devnet/1.0",
                 "Connection": "close"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, TimeoutError,
            ValueError) as exc:
        raise BrokerError(BROKER_UNAVAILABLE,
                          f"jupiter quote failed ({type(exc).__name__})"
                          ) from exc


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
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, TimeoutError,
            ValueError) as exc:
        raise BrokerError(BROKER_UNAVAILABLE,
                          f"jupiter swap build failed ({type(exc).__name__})"
                          ) from exc


class JupiterClient:
    """Thin client for Jupiter v6 quote + swap endpoints."""

    def __init__(self, base_url: str = DEFAULT_JUPITER_BASE,
                 timeout: float = 30.0):
        self._base = (base_url or DEFAULT_JUPITER_BASE).rstrip("/")
        self._timeout = float(timeout)

    def get_quote(self, input_mint: str, output_mint: str,
                  amount_lamports: int,
                  slippage_bps: int = 100) -> Dict[str, Any]:
        """Best route quote for amount_lamports of input_mint."""
        if amount_lamports <= 0:
            raise BrokerError(BROKER_UNAVAILABLE,
                              "jupiter quote amount must be positive")
        return _get_json(
            f"{self._base}/v6/quote",
            {"inputMint": input_mint, "outputMint": output_mint,
             "amount": str(amount_lamports),
             "slippageBps": str(slippage_bps)},
            self._timeout)

    def get_swap_transaction(self, quote: Dict[str, Any],
                             user_pubkey: str) -> Dict[str, Any]:
        """Build the swap transaction for a quote. Returns the /v6/swap
        response (contains base64 'swapTransaction')."""
        return _post_json(
            f"{self._base}/v6/swap",
            {"quoteResponse": quote, "userPublicKey": user_pubkey,
             "wrapAndUnwrapSol": True},
            self._timeout)
