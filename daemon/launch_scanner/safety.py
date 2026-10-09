"""Pre-entry safety gate: rug / honeypot screening.

Solana  -> RugCheck report API (free, no key):
            https://api.rugcheck.xyz/v1/tokens/{mint}/report
          mint/freeze authority, top-holder concentration, insider
          graphs, LP lock state, scored risk list.
          HARD GATES on the insider fields most bots ignore:
          ``insiderNetworks`` (funding-graph bundle clusters — combined
          share of supply), ``graphInsidersDetected`` (insider wallet
          count), ``creatorTokens`` (serial-deployer history).
Base/ETH -> GoPlus token_security (free, no key):
            https://api.gopluslabs.io/api/v1/token_security/{chain_id}
          honeypot flag, buy/sell tax, mintable, blacklist, PLUS a real
          buy->sell simulation via Honeypot.is (free, no key):
            https://api.honeypot.is/v2/IsHoneypot
          "can a buyer actually sell?" — the hole GoPlus's declared-tax
          flags don't cover.

check_token(chain, mint, timeout=..., limits=...) ->
    dict(passed, score, flags, fails, unknown):
  passed=False -> hard fail: do NOT enter, drop silently.
  unknown=True -> API unreachable; caller decides (fail-closed at both
                  the instant and confirmed alert gates).

Pure stdlib HTTP, short timeouts, never raises.
curl-first through the VM egress proxy (urllib intermittently fails there);
urllib is the fallback.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import urllib.request
from typing import Any, Dict, List

logger = logging.getLogger("forex_agent.daemon.launch_scanner.safety")

_RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
_GOPLUS_URL = ("https://api.gopluslabs.io/api/v1/token_security/"
               "{chain_id}?contract_addresses={mint}")
_HONEYPOT_URL = ("https://api.honeypot.is/v2/IsHoneypot"
                 "?chainID={chain_id}&address={mint}")
_CHAIN_IDS = {"base": "8453", "ethereum": "1"}

_TIMEOUT = 12

_UA = {"User-Agent": "forex-agent/1.0 (+paper-trading-bot)",
       "Accept": "application/json"}


def _curl_json(url: str, timeout: float) -> Any:
    """Fetch JSON via curl (no shell — arg list only). None on any failure.

    VM egress quirk: Python's urllib through the MITM proxy intermittently
    fails (IncompleteRead / connection reset) while curl succeeds
    consistently. The safety verdict gates every CA drop, so prefer curl
    when available; urllib below is the fallback. Never raises.
    """
    if not shutil.which("curl"):
        return None
    cmd = [shutil.which("curl"), "-sS", "--max-time", str(int(timeout))]
    for key, val in _UA.items():
        cmd += ["-H", "%s: %s" % (key, val)]
    cmd.append(url)
    try:
        out = subprocess.run(cmd, capture_output=True,
                             timeout=timeout + 5, check=False)
    except Exception:
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    try:
        return json.loads(out.stdout.decode("utf-8", "replace"))
    except ValueError:
        return None


def _get_json(url: str, timeout: float = _TIMEOUT) -> Any:
    data = _curl_json(url, timeout)
    if data is not None:
        return data
    # Connection: close — the egress path can truncate urllib keep-alive
    # reuse (-> IncompleteRead); close-per-request is more reliable.
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0",
                      "Accept": "application/json",
                      "Connection": "close"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        logger.warning("safety fetch failed %s: %s", url.split("?")[0], exc)
        return None


def _f(value: Any) -> float:
    try:
        return float(str(value).strip().rstrip("%") or 0)
    except (TypeError, ValueError):
        return 0.0


def _truthy(value: Any) -> bool:
    """Booleans, '1'/'0', 'true'/'false' -> bool. Never raises."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes")


def _insider_cluster_pct(networks: Any) -> float:
    """Combined % of supply held across RugCheck insiderNetworks.

    Defensive: the per-network field names may vary between report
    versions, so scan every entry for any pct-like numeric field.
    Returns 0.0 when nothing parseable is found — the caller then keeps
    a flag (never a fail) for the unparseable shape.
    """
    total = 0.0
    found = False
    try:
        entries = list(networks or [])
    except TypeError:
        return 0.0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for key, val in entry.items():
            kl = str(key).lower()
            if "pct" in kl or "percent" in kl:
                pct = _f(val)
                if 0.0 < pct <= 100.0:
                    total += pct
                    found = True
    return total if found else 0.0


def _check_solana(mint: str, timeout: float = _TIMEOUT,
                  limits: Dict[str, Any] = None) -> Dict[str, Any]:
    flags: List[str] = []
    fails: List[str] = []
    limits = limits or {}
    insider_cluster_max_pct = limits.get("insider_cluster_max_pct", 20.0)
    try:
        insider_cluster_max_pct = float(insider_cluster_max_pct)
    except (TypeError, ValueError):
        insider_cluster_max_pct = 20.0
    graph_insiders_max = limits.get("graph_insiders_max", 5)
    try:
        graph_insiders_max = int(graph_insiders_max)
    except (TypeError, ValueError):
        graph_insiders_max = 5
    data = _get_json(_RUGCHECK_URL.format(mint=mint), timeout)
    if not data:
        return {"passed": True, "score": 0, "flags": [],
                "fails": [], "unknown": True}
    if data.get("rugged"):
        fails.append("already rugged")

    if data.get("mintAuthority"):
        fails.append("mint authority enabled (infinite mint risk)")
    if data.get("freezeAuthority"):
        flags.append("freeze authority enabled")

    for risk in data.get("risks") or []:
        name = str(risk.get("name") or "")
        level = str(risk.get("level") or "").lower()
        if not name:
            continue
        if level == "danger":
            fails.append("rugcheck danger: %s" % name[:60])
        elif name not in [f.split(": ", 1)[-1] for f in flags]:
            flags.append("risk: %s" % name[:60])

    # Holder concentration — raw top-holder percentages.
    holders = data.get("topHolders") or []
    if holders:
        top1 = _f(holders[0].get("pct"))
        top5 = sum(_f(h.get("pct")) for h in holders[:5])
        if top1 > 80:
            fails.append("top holder owns %.0f%%" % top1)
        elif top1 > 50:
            flags.append("top holder owns %.0f%%" % top1)
        if top5 > 90:
            fails.append("top 5 holders own %.0f%%" % top5)
        elif top5 > 70:
            flags.append("top 5 holders own %.0f%%" % top5)

    # Insider / bundle clustering — the #1 hole in a score-only screen.
    # Bundled launches split supply across fresh wallets so top-holder
    # concentration looks clean; RugCheck's insiderNetworks lists the
    # funding-graph clusters. Gate HARD on their combined share of
    # supply, and on the insider-wallet count.
    networks = data.get("insiderNetworks") or []
    cluster_pct = _insider_cluster_pct(networks)
    if networks and cluster_pct > insider_cluster_max_pct:
        fails.append("insider cluster holds %.0f%% (limit %.0f%%)" %
                     (cluster_pct, insider_cluster_max_pct))
    elif networks:
        if cluster_pct > 0.0:
            flags.append("insider network holds %.0f%%" % cluster_pct)
        else:
            # Non-empty cluster list but no parseable share — flag, never
            # fail: we cannot quantify what we cannot parse.
            flags.append("insider network detected")

    try:
        graph_insiders = int(data.get("graphInsidersDetected") or 0)
    except (TypeError, ValueError):
        graph_insiders = 0
    if graph_insiders > graph_insiders_max:
        fails.append("graph insiders detected: %d (limit %d)" %
                     (graph_insiders, graph_insiders_max))
    elif graph_insiders > 0 and not networks:
        flags.append("insider network detected")

    # Serial deployer check — creators behind many tokens are often
    # rug factories.
    creator_tokens = data.get("creatorTokens") or []
    if len(creator_tokens) > 40:
        fails.append("serial deployer (%d tokens)" % len(creator_tokens))
    elif len(creator_tokens) > 10:
        flags.append("deployer has %d tokens" % len(creator_tokens))

    score = int(data.get("score_normalised") or 0)
    return {"passed": not fails, "score": score, "flags": flags,
            "fails": fails, "unknown": False}


def _apply_sell_simulation(chain_id: str, mint: str,
                           fails: List[str], flags: List[str],
                           timeout: float) -> None:
    """Free Honeypot.is buy->sell simulation: can a buyer actually sell?

    GoPlus flags DECLARED taxes/owner powers; this simulates the real
    sell path — the hole those flags don't cover. Fail-closed on a
    detected honeypot, fail-OPEN on API trouble (the GoPlus screen still
    gated the coin — flag the outage instead of blocking). Never raises.
    """
    data = _get_json(_HONEYPOT_URL.format(chain_id=chain_id, mint=mint),
                     timeout)
    sim = (data or {}).get("honeypotResult") or {}
    if not sim:
        flags.append("sell simulation unavailable")
        return
    if _truthy(sim.get("isHoneypot")):
        fails.append("honeypot (sell simulation)")
        return
    if _truthy(sim.get("cannotSellAll")):
        fails.append("cannot sell all (sell simulation)")
    sell_tax = _f(sim.get("sellTax"))
    if sell_tax > 10:
        fails.append("sell tax %.0f%% (simulated)" % sell_tax)
    elif sell_tax > 5:
        flags.append("sell tax %.0f%% (simulated)" % sell_tax)


def _check_evm(chain: str, mint: str, timeout: float = _TIMEOUT,
               limits: Dict[str, Any] = None) -> Dict[str, Any]:
    flags: List[str] = []
    fails: List[str] = []
    limits = limits or {}
    chain_id = _CHAIN_IDS.get(chain)
    if not chain_id:
        return {"passed": True, "score": 0, "flags": [],
                "fails": [], "unknown": True}
    data = _get_json(_GOPLUS_URL.format(chain_id=chain_id, mint=mint), timeout)
    result = (data or {}).get("result") or {}
    # GoPlus keys the result by address (any case).
    info = {}
    for key, val in result.items():
        if key.lower() == mint.lower():
            info = val or {}
            break
    if not info:
        return {"passed": True, "score": 0, "flags": [],
                "fails": [], "unknown": True}

    if str(info.get("is_honeypot")) == "1":
        fails.append("honeypot")
    if str(info.get("cannot_buy")) == "1":
        fails.append("cannot buy")
    if str(info.get("cannot_sell_all")) == "1":
        fails.append("cannot sell")
    buy_tax = _f(info.get("buy_tax"))
    sell_tax = _f(info.get("sell_tax"))
    if buy_tax > 10:
        fails.append("buy tax %.0f%%" % buy_tax)
    elif buy_tax > 5:
        flags.append("buy tax %.0f%%" % buy_tax)
    if sell_tax > 10:
        fails.append("sell tax %.0f%%" % sell_tax)
    elif sell_tax > 5:
        flags.append("sell tax %.0f%%" % sell_tax)
    if str(info.get("is_blacklisted")) == "1":
        flags.append("blacklist function present")
    if str(info.get("is_mintable")) == "1":
        flags.append("mintable")
    if str(info.get("is_proxy")) == "1":
        flags.append("proxy contract")
    if str(info.get("slippage_modifiable")) == "1":
        flags.append("slippage modifiable")

    # Real sell simulation (Honeypot.is, free): GoPlus already failed
    # above if the contract declares a honeypot, so skip the extra call
    # then; otherwise simulate the actual sell path.
    if not fails and limits.get("evm_sell_sim_enabled", True):
        try:
            sim_timeout = float(limits.get("evm_sell_sim_timeout_s", 3.0))
        except (TypeError, ValueError):
            sim_timeout = 3.0
        _apply_sell_simulation(chain_id, mint, fails, flags,
                               min(timeout, max(sim_timeout, 0.5)))

    score = 100 - 25 * len(fails) - 5 * len(flags)
    return {"passed": not fails, "score": max(score, 0), "flags": flags,
            "fails": fails, "unknown": False}


def check_token(chain: str, mint: str, timeout: float = _TIMEOUT,
                limits: Dict[str, Any] = None) -> Dict[str, Any]:
    """Screen one token. Never raises; returns a verdict dict.

    ``timeout`` bounds the HTTP wait — the instant-alert flash check
    passes a short timeout so a slow API can't stall detection.
    ``limits`` tunes the hard gates (insider cluster share, graph
    insider count, EVM sell simulation) — see ``daemon`` config.
    """
    chain = (chain or "").lower()
    if not mint:
        return {"passed": True, "score": 0, "flags": [],
                "fails": [], "unknown": True}
    try:
        if chain == "solana":
            return _check_solana(mint, timeout, limits)
        return _check_evm(chain, mint, timeout, limits)
    except Exception as exc:  # never let screening crash the loop
        logger.warning("safety check crashed for %s: %s", mint, exc)
        return {"passed": True, "score": 0, "flags": [],
                "fails": [], "unknown": True}


def verdict_line(report: Dict[str, Any]) -> str:
    """One-line summary for the Telegram card."""
    if report.get("unknown"):
        return "safety scan unavailable (no score)"
    if not report.get("passed"):
        return "BLOCKED: %s" % "; ".join(report["fails"][:2])
    bits = ["pass (score %d)" % report.get("score", 0)]
    if report.get("flags"):
        bits.append("; ".join(report["flags"][:2]))
    return " ".join(bits)
