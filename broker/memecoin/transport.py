"""Keyless public market-data transport for the memecoin provider.

Sources (all free, no API keys; curl preferred, stdlib urllib fallback):
  * DexScreener — token -> top Solana pair lookup
    (https://api.dexscreener.com/latest/dex/tokens/{mint})
  * GeckoTerminal — OHLCV candles per pool
    (https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/{agg})

Graceful degradation: every method returns None / raises
BrokerError(BROKER_UNAVAILABLE) with an honest message when the network
or the upstream is unreachable. The adapter treats "no market data" as
a hard stop for order placement — paper fills are never invented from
stale or missing prices.
"""

import json
import logging
import shutil
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from broker import BROKER_UNAVAILABLE, BrokerError
from core.market import Candle

logger = logging.getLogger("broker.memecoin.transport")

_DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
_GECKO_OHLCV_URL = (
    "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/{agg}"
)
_HTTP_TIMEOUT = 10

# Repo timeframe -> GeckoTerminal aggregate bucket.
_TIMEFRAME_AGG = {
    "M1": "minute", "M5": "minute", "M15": "minute",
    "M30": "hour", "H1": "hour", "H4": "hour", "D1": "day",
}


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _http_get_json(url: str) -> Optional[Any]:
    """GET url, parse JSON. Returns None on any failure (never raises)."""
    # VM egress quirk (2026-09-22): Python's urllib through the MITM proxy
    # intermittently fails against these hosts (IncompleteRead / connection
    # reset) while curl succeeds consistently. Prefer curl when available,
    # fall back to urllib otherwise.
    if shutil.which("curl"):
        try:
            out = subprocess.run(
                ["curl", "-sS", "--max-time", str(_HTTP_TIMEOUT),
                 "-H", "Accept: application/json", url],
                capture_output=True, timeout=_HTTP_TIMEOUT + 5, check=False,
            )
            if out.returncode == 0 and out.stdout:
                try:
                    return json.loads(out.stdout.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    # Garbage 200 from curl: treat like a transport
                    # failure and fall through to urllib (never raise —
                    # contract is "None on any failure").
                    logger.warning("market data curl bad payload for %s: %s",
                                   url, exc)
            logger.warning("market data curl failed for %s: %s",
                           url, (out.stderr or b"")[:120].decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001 - fall through to urllib
            logger.warning("market data curl error for %s: %s", url, exc)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "forex-agent/memecoin-provider",
                          "Accept": "application/json",
                          "Connection": "close"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning("market data HTTP %s from %s", resp.status, url)
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError,
            ValueError, json.JSONDecodeError) as exc:
        logger.warning("market data fetch failed for %s: %s", url, exc)
        return None


class MemecoinMarketData:
    """Public market-data access for Solana tokens. Stateless and keyless.

    `symbols` maps a trade symbol (e.g. "SOL" or a mint address) to a
    token mint. Pool resolution is cached in-memory per process.
    """

    def __init__(self, symbols: Optional[Dict[str, str]] = None):
        # symbol -> mint
        self._symbols: Dict[str, str] = dict(symbols or {})
        self._pool_cache: Dict[str, Optional[str]] = {}

    # -- symbol registry -------------------------------------------------
    def register(self, symbol: str, mint: str) -> None:
        self._symbols[symbol.upper()] = mint

    def mint_for(self, symbol: str) -> Optional[str]:
        return self._symbols.get(symbol.upper())

    def known_symbols(self) -> List[str]:
        return sorted(self._symbols)

    # -- quotes -----------------------------------------------------------
    def _top_pair(self, mint: str) -> Optional[Dict[str, Any]]:
        """Top Solana pair for a mint from DexScreener (by liquidity)."""
        data = _http_get_json(_DEXSCREENER_TOKEN_URL.format(mint=mint))
        if not data:
            return None
        pairs = [p for p in (data.get("pairs") or [])
                 if str(p.get("chainId", "")).lower() == "solana"]
        if not pairs:
            return None
        pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0),
                   reverse=True)
        return pairs[0]

    def get_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Live price snapshot: {"price_usd", "price_sol", "pair", "ts"}.

        Returns None when the upstream is unreachable or the token has
        no Solana pair — the adapter turns that into BROKER_UNAVAILABLE.
        """
        mint = self.mint_for(symbol)
        if not mint:
            return None
        pair = self._top_pair(mint)
        if not pair:
            return None
        try:
            price_usd = float(pair.get("priceUsd") or 0.0)
        except (TypeError, ValueError):
            return None
        if price_usd <= 0:
            return None
        price_native = pair.get("priceNative")
        try:
            price_sol = float(price_native) if price_native else None
        except (TypeError, ValueError):
            price_sol = None
        return {
            "symbol": symbol.upper(),
            "mint": mint,
            "price_usd": price_usd,
            "price_sol": price_sol,
            "pair": pair.get("pairAddress"),
            "ts": datetime.now(timezone.utc),
        }

    def get_token_stats(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Rich token stats for signal cards: price, FDV, liquidity,
        volumes, price changes, buy/sell txn counts, dex/chain, pair age,
        chart URL and socials — straight from the top DexScreener pair.

        Returns None when the upstream is unreachable or the token has no
        Solana pair. Never raises; callers treat None as "stats unavailable"
        and fall back to the plain signal format.
        """
        mint = self.mint_for(symbol)
        if not mint:
            return None
        try:
            pair = self._top_pair(mint)
        except Exception:  # noqa: BLE001 - stats are best-effort
            logger.warning("token stats lookup failed for %s", symbol,
                           exc_info=True)
            return None
        if not pair:
            return None
        try:
            base = pair.get("baseToken") or {}
            liquidity = pair.get("liquidity") or {}
            volume = pair.get("volume") or {}
            price_change = pair.get("priceChange") or {}
            txns = pair.get("txns") or {}
            info = pair.get("info") or {}
            socials = info.get("socials") or []
            h1 = txns.get("h1") or {}
            age_days = None
            created = pair.get("pairCreatedAt")
            if created:
                try:
                    age_days = max(
                        0,
                        (datetime.now(timezone.utc)
                         - datetime.fromtimestamp(float(created) / 1000.0,
                                                  tz=timezone.utc)).days)
                except (TypeError, ValueError, OverflowError):
                    age_days = None
            return {
                "symbol": symbol.upper(),
                "name": base.get("name") or symbol.upper(),
                "mint": mint,
                "price_usd": _safe_float(pair.get("priceUsd")),
                "fdv_usd": _safe_float(pair.get("fdv")),
                "liquidity_usd": _safe_float(liquidity.get("usd")),
                "volume_h24": _safe_float(volume.get("h24")),
                "volume_h1": _safe_float(volume.get("h1")),
                "change_h1_pct": _safe_float(price_change.get("h1")),
                "change_h24_pct": _safe_float(price_change.get("h24")),
                "buys_h1": _safe_int(h1.get("buys")),
                "sells_h1": _safe_int(h1.get("sells")),
                "dex": pair.get("dexId"),
                "chain": pair.get("chainId"),
                "age_days": age_days,
                "chart_url": pair.get("url"),
                "socials": [
                    {"type": s.get("type"), "url": s.get("url")}
                    for s in socials if isinstance(s, dict) and s.get("url")
                ],
            }
        except Exception:  # noqa: BLE001 - stats are best-effort
            logger.warning("token stats parse failed for %s", symbol,
                           exc_info=True)
            return None

    # -- candles ----------------------------------------------------------
    def _pool_for(self, mint: str) -> Optional[str]:
        if mint not in self._pool_cache:
            pair = self._top_pair(mint)
            self._pool_cache[mint] = pair.get("pairAddress") if pair else None
        return self._pool_cache[mint]

    def get_candles(self, symbol: str, timeframe: str,
                    count: int = 200) -> List[Candle]:
        """CLOSED candles, oldest -> newest, via GeckoTerminal OHLCV.

        Raises BrokerError(BROKER_UNAVAILABLE) when the symbol is unknown,
        the timeframe is unsupported, or market data is unreachable —
        never returns invented candles.
        """
        mint = self.mint_for(symbol)
        if not mint:
            raise BrokerError(BROKER_UNAVAILABLE,
                              f"unknown memecoin symbol {symbol!r}: no mint "
                              f"registered (see memecoin.symbols)")
        agg = _TIMEFRAME_AGG.get(timeframe)
        if agg is None:
            raise BrokerError(BROKER_UNAVAILABLE,
                              f"unsupported timeframe {timeframe!r} for memecoin "
                              f"market data (have: {sorted(_TIMEFRAME_AGG)})")
        pool = self._pool_for(mint)
        if not pool:
            raise BrokerError(BROKER_UNAVAILABLE,
                              f"no Solana liquidity pool found for {symbol!r}")
        url = (_GECKO_OHLCV_URL.format(pool=pool, agg=agg)
               + f"?limit={min(max(count + 1, 2), 1000)}")
        data = _http_get_json(url)
        ohlcv = (data or {}).get("data", {}).get("attributes", {}).get("ohlcv_list")
        if not ohlcv:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                f"memecoin market data unreachable for {symbol} {timeframe} "
                f"(GeckoTerminal returned no candles)",
                detail={"reason": "market_data_unreachable"},
            )
        candles = []
        for row in ohlcv:
            # row: [unix_ts, open, high, low, close, volume]
            try:
                ts, o, h, l, c = int(row[0]), float(row[1]), float(row[2]), \
                    float(row[3]), float(row[4])
                vol = float(row[5]) if len(row) > 5 else 0.0
            except (TypeError, ValueError, IndexError):
                continue
            candles.append(Candle(
                time=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=o, high=h, low=l, close=c, volume=vol))
        candles.sort(key=lambda c: c.time)
        if not candles:
            raise BrokerError(BROKER_UNAVAILABLE,
                              f"no usable candles for {symbol} {timeframe}")
        # Drop the forming candle — same closed-candle contract as every
        # other adapter.
        return candles[:-1][-count:]
