"""Launch-radar data sources: pump.fun, DexScreener boosts, Hyperliquid listings.

All functions are stdlib-only, never raise (return [] on any failure),
and normalize to dicts:

    {
        "mint": str | None,      # contract address (None for CEX listings)
        "symbol": str,           # display ticker (may be "")
        "name": str,
        "chain": str,            # solana | base | ethereum | robinhood | hyperliquid
        "source": str,           # pumpfun | dexscreener-boost | hyperliquid-listing
        "created_ts": float,     # epoch seconds (0 when unknown)
        "usd_market_cap": float, # 0 when unknown
        "extra": dict,           # source-specific fields
        "url": str,              # best link for the alert card
    }
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

logger = logging.getLogger("forex_agent.daemon.launch_scanner.sources")

_HTTP_TIMEOUT = 15
_UA = {"User-Agent": "forex-agent/1.0 (+paper-trading-bot)",
       "Connection": "close"}

# Bounded retry for flaky upstreams (DexScreener / Hyperliquid sometimes
# reset connections or truncate bodies). Never raises — returns None after
# the budget is spent so a bad poll can't stall the scanner cycle.
_RETRYABLE = (
    urllib.error.URLError,
    ConnectionError,
    TimeoutError,
)
try:
    import http.client as _http_client
    _RETRYABLE = _RETRYABLE + (
        _http_client.IncompleteRead,
        _http_client.RemoteDisconnected,
        _http_client.BadStatusLine,
    )
except Exception:  # pragma: no cover — stdlib always has http.client
    _http_client = None

_MAX_ATTEMPTS = 3
_BACKOFF_S = (1.0, 2.0)  # sleeps between attempts 1->2, 2->3

_PUMPFUN_URL = ("https://frontend-api-v3.pump.fun/coins?offset=0&limit=50"
                "&sort=created_timestamp&order=DESC&includeNsfw=false")
_DEX_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
_DEX_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
_DEX_BATCH_URL = "https://api.dexscreener.com/tokens/v1/{chain}/{mints}"
_HL_URL = "https://api.hyperliquid.xyz/info"
_GECKO_NEW_POOLS_URL = ("https://api.geckoterminal.com/api/v2/networks/"
                       "{network}/new_pools")

_BOOST_CHAINS = {"solana", "base", "ethereum", "robinhood"}

# GeckoTerminal network id -> our chain name, plus the USDC mint/address on
# each network. USDC-quoted pools are matched by address (the new_pools
# payload carries no token symbols in `included`), never by name.
# GeckoTerminal network id -> our canonical chain name. The safety
# gate keys EVM checks off "base"/"ethereum" (see safety._CHAIN_IDS),
# so "eth" must map to "ethereum" — a raw "eth" chain would come back
# safety-unknown and be held silently by the fail-closed gate.
_GECKO_NETWORK_TO_CHAIN = {"solana": "solana", "base": "base",
                           "eth": "ethereum"}

_GECKO_USDC_NETWORKS = (
    ("solana", "solana",
     "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
    ("base", "base",
     "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA4a966"),
    ("eth", "ethereum",
     "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
)


def _curl_bytes(url: str, data: Optional[bytes] = None,
                extra_headers: Optional[Dict[str, str]] = None
                ) -> Optional[bytes]:
    """Fetch raw body bytes via curl (no shell — arg list only).

    VM egress quirk: Python's urllib through the MITM proxy intermittently
    fails against DexScreener (IncompleteRead / connection reset — currently
    ~100% failure) while curl succeeds consistently (10/10). Prefer curl
    when available; the urllib loop below is the fallback. Returns None on
    any failure (never raises).
    """
    if not shutil.which("curl"):
        return None
    curl = shutil.which("curl")
    cmd = [curl, "-sS", "--max-time", str(_HTTP_TIMEOUT)]
    headers = dict(_UA)
    headers.pop("Connection", None)  # curl manages its own connections
    if extra_headers:
        headers.update(extra_headers)
    for key, val in headers.items():
        cmd += ["-H", "%s: %s" % (key, val)]
    if data is not None:
        cmd += ["-X", "POST", "--data-binary", "@-"]
    cmd.append(url)
    try:
        out = subprocess.run(cmd, input=data, capture_output=True,
                             timeout=_HTTP_TIMEOUT + 5, check=False)
    except Exception:
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    return out.stdout


def _get_json(url: str) -> Optional[Any]:
    raw = _curl_bytes(url)
    if raw is not None:
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            pass  # garbage payload — fall through to urllib
    last_exc: Optional[Exception] = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except _RETRYABLE as exc:
            last_exc = exc
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF_S[attempt])
        except Exception as exc:
            logger.warning("source fetch failed %s: %s", url.split("?")[0][:60], exc)
            return None
    logger.warning("source fetch failed after %d attempts %s: %s",
                   _MAX_ATTEMPTS, url.split("?")[0][:60], last_exc)
    return None


def _post_json(url: str, payload: Dict[str, Any]) -> Optional[Any]:
    raw = _curl_bytes(url, data=json.dumps(payload).encode(),
                      extra_headers={"Content-Type": "application/json"})
    if raw is not None:
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            pass  # garbage payload — fall through to urllib
    last_exc: Optional[Exception] = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            body = json.dumps(payload).encode()
            req = urllib.request.Request(url, data=body, headers=dict(
                _UA, **{"Content-Type": "application/json"}))
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except _RETRYABLE as exc:
            last_exc = exc
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF_S[attempt])
        except Exception as exc:
            logger.warning("source post failed %s: %s", url[:60], exc)
            return None
    logger.warning("source post failed after %d attempts %s: %s",
                   _MAX_ATTEMPTS, url[:60], last_exc)
    return None


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ----------------------------------------------------------------------
# pump.fun — newest launches on Solana
# ----------------------------------------------------------------------
def pumpfun_new() -> List[Dict[str, Any]]:
    data = _get_json(_PUMPFUN_URL)
    if not isinstance(data, list):
        return []
    out = []
    for c in data:
        if not isinstance(c, dict):
            continue
        mint = str(c.get("mint") or "").strip()
        if not mint:
            continue
        out.append({
            "mint": mint,
            "symbol": str(c.get("symbol") or "").strip(),
            "name": str(c.get("name") or "").strip(),
            "chain": "solana",
            "source": "pumpfun",
            "created_ts": _f(c.get("created_timestamp")) / 1000.0,
            "usd_market_cap": _f(c.get("usd_market_cap") or c.get("market_cap_usd")),
            "extra": {
                "complete": bool(c.get("complete")),
                "reply_count": int(c.get("reply_count") or 0),
                "banned": bool(c.get("is_banned")),
                "creator": str(c.get("creator") or ""),
            },
            "url": "https://pump.fun/coin/%s" % mint,
        })
    return out


# ----------------------------------------------------------------------
# DexScreener — top boosted tokens (multi-chain runner visibility)
# ----------------------------------------------------------------------
def dexscreener_boosts() -> List[Dict[str, Any]]:
    data = _get_json(_DEX_BOOSTS_URL)
    if not isinstance(data, list):
        return []
    out = []
    for b in data:
        if not isinstance(b, dict):
            continue
        chain = str(b.get("chainId") or "").lower()
        if chain not in _BOOST_CHAINS:
            continue
        addr = str(b.get("tokenAddress") or "").strip()
        if not addr:
            continue
        links = b.get("links") or {}
        url = str(b.get("url") or "")
        if not url:
            url = "https://dexscreener.com/%s/%s" % (chain, addr)
        out.append({
            "mint": addr,
            "symbol": "",
            "name": str(b.get("description") or "")[:60],
            "chain": chain,
            "source": "dexscreener-boost",
            "created_ts": 0.0,
            "usd_market_cap": 0.0,
            "extra": {"boost_amount": b.get("totalAmount")},
            "url": url,
        })
    return out


def _pair_to_stats(top: Dict[str, Any]) -> Dict[str, Any]:
    base = top.get("baseToken") or {}
    dex = str(top.get("dexId") or "")
    chain = str(top.get("chainId") or "")
    pair_addr = str(top.get("pairAddress") or "")
    txns_m5 = (top.get("txns") or {}).get("m5") or {}
    return {
        "symbol": str(base.get("symbol") or ""),
        "name": str(base.get("name") or ""),
        "usd_liquidity": _f((top.get("liquidity") or {}).get("usd")),
        "usd_market_cap": _f(top.get("marketCap") or top.get("fdv")),
        "price_usd": _f(top.get("priceUsd")),
        "vol_m5": _f((top.get("volume") or {}).get("m5")),
        "buys_m5": int(txns_m5.get("buys") or 0),
        "sells_m5": int(txns_m5.get("sells") or 0),
        "url": ("https://dexscreener.com/%s/%s" % (chain, pair_addr)
                if chain and pair_addr else ""),
        "dex": dex,
        "chain": chain,
    }


def dexscreener_batch_stats(mints: List[str],
                            chain: str = "solana") -> Dict[str, Dict[str, Any]]:
    """Batch pair stats: one HTTP call for up to 30 mints.

    GET /tokens/v1/{chain}/{m1,m2,...} — free, no key. Returns
    {mint: stats}; mints with no indexed pair are simply absent.
    Highest-liquidity pair wins per mint.
    """
    out: Dict[str, Dict[str, Any]] = {}
    addrs = [m for m in dict.fromkeys(mints) if m][:30]
    if not addrs:
        return out
    data = _get_json(_DEX_BATCH_URL.format(chain=chain,
                                           mints=",".join(addrs)))
    if not isinstance(data, list):
        return out
    best: Dict[str, Dict[str, Any]] = {}
    for pair in data:
        base = pair.get("baseToken") or {}
        addr = str(base.get("address") or "")
        if not addr:
            continue
        stats = _pair_to_stats(pair)
        prev = best.get(addr.lower())
        if prev is None or stats["usd_liquidity"] > prev["usd_liquidity"]:
            best[addr.lower()] = stats
    for m in addrs:
        hit = best.get(m.lower())
        if hit:
            out[m] = hit
    return out


def dexscreener_token_stats(mint: str) -> Optional[Dict[str, Any]]:
    """Resolve a mint -> {symbol, name, usd_liquidity, usd_market_cap, url}."""
    data = _get_json(_DEX_TOKEN_URL.format(mint=mint))
    pairs = (data or {}).get("pairs") or []
    if not pairs:
        return None
    pairs = sorted(pairs,
                   key=lambda p: _f((p.get("liquidity") or {}).get("usd")),
                   reverse=True)
    return _pair_to_stats(pairs[0])


# ----------------------------------------------------------------------
# GeckoTerminal — trending pools (established-coin momentum discovery)
# ----------------------------------------------------------------------
_GECKO_TRENDING_URL = ("https://api.geckoterminal.com/api/v2/networks/"
                       "{network}/trending_pools")


def geckoterminal_trending_pools(network: str = "solana"
                                 ) -> List[Dict[str, Any]]:
    """Pools people are actually trading right now (free endpoint).

    This is the established-coin momentum feed: unlike new_pools (birth
    hunting), trending_pools surfaces coins mid-run — the FWOG/NEET/
    ANTFUN-shaped moves. Each pool carries 1h/24h price change, 24h
    volume and liquidity so the momentum scanner can qualify runners
    and skip exhausted tops. Never raises.
    """
    out: List[Dict[str, Any]] = []
    try:
        data = _get_json(_GECKO_TRENDING_URL.format(network=network))
    except Exception:
        return out
    if not isinstance(data, dict):
        return out
    for pool in data.get("data") or []:
        if not isinstance(pool, dict):
            continue
        try:
            attrs = pool.get("attributes") or {}
            rels = pool.get("relationships") or {}
            base_id = ((rels.get("base_token") or {}).get("data")
                       or {}).get("id")
            mint = _gecko_token_addr(base_id)
            if not mint:
                continue
            name = str(attrs.get("name") or "")
            symbol = (name.split("/")[0].strip() if "/" in name else "")
            chg = attrs.get("price_change_percentage") or {}
            vol = attrs.get("volume_usd") or {}
            pool_addr = str(attrs.get("address") or "")
            out.append({
                "pool_address": pool_addr,
                "mint": mint,
                "symbol": symbol[:20],
                "name": name[:60],
                "chain": _GECKO_NETWORK_TO_CHAIN.get(network, network),
                "chg_1h_pct": _f(chg.get("h1")),
                "chg_24h_pct": _f(chg.get("h24")),
                "vol_24h_usd": _f(vol.get("h24")),
                "liq_usd": _f(attrs.get("reserve_in_usd")),
                "usd_market_cap": _f(attrs.get("market_cap_usd")
                                     or attrs.get("fdv_usd")),
                "url": (("https://www.geckoterminal.com/%s/pools/%s"
                         % (network, pool_addr)) if pool_addr else ""),
            })
        except Exception:
            continue
    return out


# ----------------------------------------------------------------------
# GeckoTerminal — newest pools quoted in USDC (organic launch discovery)
# ----------------------------------------------------------------------
def _gecko_token_addr(token_id: Any) -> str:
    """'solana_<mint>' / 'base_0x..' / 'eth_0x..' -> bare token address."""
    tid = str(token_id or "").strip()
    if "_" in tid:
        return tid.split("_", 1)[1].strip()
    return tid


def _gecko_created_ts(value: Any) -> float:
    try:
        text = str(value or "").strip().replace("Z", "+00:00")
        if not text:
            return 0.0
        import datetime as _dt
        return _dt.datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def geckoterminal_usdc_pools() -> List[Dict[str, Any]]:
    """Newest USDC-quoted pools per network (free GeckoTerminal endpoint).

    Covers the launch venues pump.fun never sees: Raydium/Meteora/Orca
    USDC pools on Solana, Aerodrome/Uniswap USDC pools on Base, Uniswap
    USDC pools on Ethereum. Candidates flow through the same safety
    gates and score bar as every other source.
    """
    out: List[Dict[str, Any]] = []
    for gecko_net, chain, usdc_addr in _GECKO_USDC_NETWORKS:
        data = _get_json(_GECKO_NEW_POOLS_URL.format(network=gecko_net))
        if not isinstance(data, dict):
            continue
        for pool in data.get("data") or []:
            if not isinstance(pool, dict):
                continue
            attrs = pool.get("attributes") or {}
            rels = pool.get("relationships") or {}
            quote_id = ((rels.get("quote_token") or {}).get("data")
                        or {}).get("id")
            if (_gecko_token_addr(quote_id).lower()
                    != usdc_addr.lower()):
                continue
            base_id = ((rels.get("base_token") or {}).get("data")
                       or {}).get("id")
            mint = _gecko_token_addr(base_id)
            if not mint:
                continue
            # Never alert on USDC itself (or a mislabeled quote leg).
            if mint.lower() == usdc_addr.lower():
                continue
            name = str(attrs.get("name") or "")
            symbol = name.split("/")[0].strip() if "/" in name else ""
            pool_addr = str(attrs.get("address") or "")
            out.append({
                "mint": mint,
                "symbol": symbol[:20],
                "name": name[:60],
                "chain": chain,
                "source": "gecko-usdc-pool",
                "created_ts": _gecko_created_ts(
                    attrs.get("pool_created_at")),
                "usd_market_cap": _f(attrs.get("market_cap_usd")
                                     or attrs.get("fdv_usd")),
                "extra": {
                    "pool_address": pool_addr,
                    "dex": ((rels.get("dex") or {}).get("data")
                            or {}).get("id"),
                },
                "url": ("https://www.geckoterminal.com/%s/pools/%s"
                        % (gecko_net, pool_addr)) if pool_addr else "",
            })
    return out
# ----------------------------------------------------------------------
# Hyperliquid — new spot listings
# ----------------------------------------------------------------------
def hyperliquid_listings() -> List[Dict[str, Any]]:
    data = _post_json(_HL_URL, {"type": "spotMeta"})
    tokens = (data or {}).get("tokens") or []
    out = []
    for t in tokens:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()
        if not name or name in ("USDC", "USDT"):
            continue
        out.append({
            "mint": None,
            "symbol": name,
            "name": name,
            "chain": "hyperliquid",
            "source": "hyperliquid-listing",
            "created_ts": 0.0,
            "usd_market_cap": 0.0,
            "extra": {},
            "url": "https://app.hyperliquid.xyz/trade/%s-SPOT" % name,
        })
    return out
