"""Social-sentiment tracker — websites / socials footprint per token.

Reads each token's public DexScreener profile (websites + socials lists)
and watches how the footprint grows between observations: a team that
adds a website, a Twitter, a Telegram over hours looks very different
from a mint whose profile never fills in.

DESIGN RULE (the user's standing rule): sentiment is a **soft boost only**.
This module scores; it never decides. There is no allow/block/drop logic
anywhere in here — nothing in this file can suppress or fire an alert.
The daemon may use a HOT verdict as one small positive input among many,
exactly like a fingerprint, and the hard safety gates always win.

``SentimentTracker`` keeps a per-mint time series of profile observations
(pure logic, no I/O). Profiles come from ``fetch_token_profile`` (one
small keyless DexScreener call per token, rate-limited).

``score()`` returns a dict with a 0-100 score, a new-socials-per-hour
velocity, signal strings and a verdict:

* HOT     — score >= 70
* WARM    — score >= 40
* NEUTRAL — everything else (including zero socials, always)

Scores are floored so tiny observation windows (< 300s) can't fake a
velocity signal, and capped so no profile can exceed 100.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

_DEX_TOKEN_URL = "https://api.dexscreener.com/tokens/v1/{chain}/{mint}"
_UA = {"User-Agent": "forex-agent/1.0 (+paper-trading-bot)",
       "Connection": "close"}

# Keyless public API — no secrets anywhere in this module.
_CHAIN_IDS = {"solana": "solana", "base": "base", "ethereum": "eth"}

# Min gap between HTTP calls (module level so every caller shares it).
_MIN_CALL_GAP_S = 1.0
_last_call_ts = 0.0

# Verdict thresholds.
HOT_SCORE = 70.0
WARM_SCORE = 40.0

# Observation window must span this long before velocity counts —
# otherwise two polls 10s apart with one new link would fake "hype".
MIN_WINDOW_S = 300.0

# Hard cap on tracked mints so daemon state stays small.
MAX_TRACKED = 2000

# Observations kept per mint (first-vs-latest is all scoring needs).
_MAX_OBS = 12

_TWITTER_TYPES = {"twitter", "x"}
_TELEGRAM_TYPES = {"telegram"}


def _socials_of(profile: Dict[str, Any]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    socials = profile.get("socials") or []
    if isinstance(socials, dict):
        socials = [socials]
    for s in socials:
        if not isinstance(s, dict):
            continue
        stype = str(s.get("type") or "").strip().lower()
        url = str(s.get("url") or "").strip()
        if stype:
            out.append((stype, url))
    return out


def _websites_of(profile: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    sites = profile.get("websites") or []
    if isinstance(sites, dict):
        sites = [sites]
    for w in sites:
        url = w.get("url") if isinstance(w, dict) else w
        url = str(url or "").strip()
        if url:
            out.append(url)
    return out


def fetch_token_profile(chain: str, mint: str,
                       timeout_s: float = 8.0) -> Optional[Dict[str, Any]]:
    """Keyless DexScreener token profile for one mint.

    Returns the first pair's ``info`` normalized to
    ``{"websites": [...], "socials": [{"type":, "url":}]}``,
    or None on any trouble (bad chain, HTTP error, parse error, no pairs).
    No secrets, no second calls (token-boosts deliberately skipped to keep
    this a single cheap call per mint). Respects a 1s minimum gap between
    calls across all callers.
    """
    global _last_call_ts
    chain_id = _CHAIN_IDS.get((chain or "").strip().lower())
    if not chain_id or not mint:
        return None
    wait = _MIN_CALL_GAP_S - (time.monotonic() - _last_call_ts)
    if wait > 0:
        time.sleep(wait)
    try:
        req = urllib.request.Request(
            _DEX_TOKEN_URL.format(chain=chain_id, mint=mint),
            headers=_UA,
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        pairs = data if isinstance(data, list) else [data]
        if not pairs or not isinstance(pairs[0], dict):
            return None
        info = pairs[0].get("info") or {}
        if not isinstance(info, dict):
            info = {}
        return {
            "websites": _websites_of(info),
            "socials": [{"type": t, "url": u}
                        for t, u in _socials_of(info)],
        }
    except Exception:
        return None
    finally:
        _last_call_ts = time.monotonic()


# obs tuple: (ts, has_website, socials tuple of (type, url))
_Obs = Tuple[float, bool, Tuple[Tuple[str, str], ...]]


class SentimentTracker:
    """Per-mint social-footprint time series + 0-100 sentiment score."""

    def __init__(self, cfg=None):
        self.cfg = cfg  # retained so owners can detect config swaps
        self.enabled = bool(getattr(cfg, "sentiment_enabled", True)
                            if cfg is not None else True)
        self._series: Dict[str, Deque[_Obs]] = {}

    # -- state hygiene ------------------------------------------------
    def _enforce_bound(self) -> None:
        if len(self._series) > MAX_TRACKED:
            ordered = sorted(self._series.items(),
                             key=lambda kv: kv[1][-1][0])
            for mint, _ in ordered[:len(self._series) - MAX_TRACKED]:
                self._series.pop(mint, None)

    def prune(self, now: float, max_age_s: float = 86400.0) -> None:
        """Drop mints with no observation inside max_age_s; enforce the
        MAX_TRACKED bound by evicting the oldest first."""
        now_f = float(now)
        for mint in list(self._series):
            q = self._series[mint]
            while q and now_f - q[0][0] > max_age_s:
                q.popleft()
            if not q:
                self._series.pop(mint, None)
        self._enforce_bound()

    # -- main entry ----------------------------------------------------
    def observe(self, mint: str, profile: Optional[Dict[str, Any]],
                now: float) -> Optional[Dict[str, Any]]:
        """Append one profile observation; return the sentiment score
        (needs >= 2 observations), or None when too early / disabled /
        profile missing. Pure score — never an allow/block decision."""
        if not self.enabled or not mint or not profile:
            return None
        socials = tuple(sorted(set(_socials_of(profile))))
        obs: _Obs = (float(now), bool(_websites_of(profile)), socials)
        q = self._series.get(mint)
        if q is None:
            q = self._series[mint] = deque(maxlen=_MAX_OBS)
        q.append(obs)
        self._enforce_bound()
        if len(q) < 2:
            return None
        return self.score(mint)

    def score(self, mint: str) -> Optional[Dict[str, Any]]:
        """Sentiment for a mint, or None when fewer than 2 observations."""
        q = self._series.get(mint)
        if not q or len(q) < 2:
            return None
        first, last = q[0], q[-1]
        dt_s = last[0] - first[0]

        first_types = {t for t, _ in first[2]}
        last_types = {t for t, _ in last[2]}
        first_set, last_set = set(first[2]), set(last[2])
        new_socials = len(last_set - first_set)
        distinct_types = len(last_types)

        signals: List[str] = []
        if last[1]:
            signals.append("has_website")
        if last_types & _TWITTER_TYPES:
            signals.append("has_twitter")
        if last_types & _TELEGRAM_TYPES:
            signals.append("has_telegram")
        if last[1] and not first[1]:
            signals.append("new_website")
        if (last_types & _TWITTER_TYPES) and not (first_types & _TWITTER_TYPES):
            signals.append("new_twitter")
        if (last_types & _TELEGRAM_TYPES) and not (first_types & _TELEGRAM_TYPES):
            signals.append("new_telegram")
        if len(last_set) > len(first_set):
            signals.append("social_count_up")

        # Scoring: base 20 for having any socials now; +15 per NEW social
        # between first and latest (capped at +45); +10 when the profile
        # covers 3+ distinct social types. Hard-capped at 100.
        has_any = bool(last_set)
        score = (20.0 if has_any else 0.0)
        score += min(45.0, 15.0 * new_socials)
        if distinct_types >= 3:
            score += 10.0
        score = max(0.0, min(100.0, score))

        # Velocity: new socials per hour. Floored — zero new socials means
        # zero velocity, and a window under MIN_WINDOW_S can't fake hype:
        # such mints stay NEUTRAL no matter the raw score.
        if new_socials <= 0 or dt_s <= 0:
            velocity = 0.0
        else:
            velocity = new_socials / (dt_s / 3600.0)

        window_ok = dt_s >= MIN_WINDOW_S
        if not has_any and new_socials == 0:
            verdict = "NEUTRAL"  # zero socials ever: can never be HOT
        elif not window_ok:
            verdict = "NEUTRAL"
        elif score >= HOT_SCORE:
            verdict = "HOT"
        elif score >= WARM_SCORE:
            verdict = "WARM"
        else:
            verdict = "NEUTRAL"

        return {
            "score": round(score, 1),
            "verdict": verdict,
            "velocity": round(velocity, 2),
            "signals": signals,
            "new_socials": new_socials,
            "distinct_types": distinct_types,
            "window_s": round(dt_s, 1),
            "polls": len(q),
        }
