"""AI narrative picker + narrative heat tracker.

Two halves, both read-only with respect to trading decisions:

* **narrative classification** — ``classify_narrative`` tags a coin by
  matching its symbol/name/description against the curated
  ``NARRATIVE_KEYWORDS`` taxonomy (dog, cat, ai, gaming, ...). Pure
  substring matching, stdlib only, never raises on weird input.

* **narrative heat** — ``NarrativeHeat`` learns which narratives are
  actually running hot from closed-runner outcomes. A tag whose coins
  turn into runners (peak multiple >= 2.0, not a rug) more often than
  the global average gets a heat multiplier above 1.0; a tag that
  underperforms gets one below 1.0. Multipliers stay in [0.5, 2.0].

DESIGN RULE (standing user rule): narrative is a SOFT SIGNAL ONLY.
This module never allows, blocks, holds, or drops anything. It
classifies and scores; the safety gates and judge pipeline in
``daemon/launch_scanner/__init__.py`` make every allow/block decision.
A hot narrative must never override a safety fail, and a cold
narrative must never block an otherwise qualified coin. Callers
should use ``heat()`` as a tie-breaker / sizing input, not a gate.

All state is bounded (5000 outcomes) and JSON-serializable; no
network, no secrets, stdlib only.
"""

from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

# -- taxonomy -----------------------------------------------------------
# Each tag: 6-15 lowercase keywords. Matching is substring-based, so
# keep keywords specific enough to avoid accidental hits.
NARRATIVE_KEYWORDS: Dict[str, List[str]] = {
    "dog": ["dog", "doge", "shiba", "inu", "puppy", "pup", "hound",
            "canine", "akita", "corgi", "bulldog", "pomeranian"],
    "cat": ["cat", "kitten", "kitty", "meow", "feline", "neko",
            "gato", "paws"],
    "frog": ["frog", "pepe", "toad", "ribbit", "tadpole",
             "bullfrog", "pond"],
    "ai": ["ai", "artificial", "gpt", "llm", "neural", "robot",
           "agent", "machine", "deep", "cortex", "synth", "cyber",
           "compute"],
    "gaming": ["game", "gaming", "gamer", "play", "quest", "arcade",
               "rpg", "esports", "metaverse", "pixel", "console",
               "speedrun"],
    "political": ["trump", "maga", "biden", "vote", "election",
                  "president", "senate", "congress", "democrat",
                  "republican", "politic", "freedom", "patriot"],
    "celebrity": ["elon", "musk", "celebrity", "celebr", "famous",
                  "influencer", "kardashian", "bieber", "taylor",
                  "ronaldo", "messi", "superstar"],
    "sports": ["sport", "football", "soccer", "basketball", "baseball",
               "tennis", "olympic", "champion", "league", "touchdown",
               "knockout", "derby"],
    "food": ["food", "pizza", "burger", "taco", "sushi", "ramen",
             "cookie", "cake", "chocolate", "coffee", "noodle",
             "snack"],
    "space": ["space", "mars", "lunar", "rocket", "orbit", "galaxy",
              "cosmos", "astronaut", "satellite", "nebula",
              "interstellar", "starship"],
    "degen": ["ape", "moon", "lambo", "hodl", "degen", "moonshot",
              "gem", "pump", "sendit", "wagmi", "diamond", "100x",
              "1000x"],
    "rwa": ["rwa", "realworld", "estate", "property", "treasury",
            "bond", "yield", "tokenized", "commodity", "gold",
            "silver", "reit"],
    "meme_classic": ["pepe", "doge", "shib", "bonk", "wif", "floki",
                     "brett", "popcat", "mog", "turbo", "neiro",
                     "ponke"],
}

OTHER = "other"

# -- heat tuning ----------------------------------------------------------
DAY_S = 86400.0
OUTCOME_TTL_S = 72 * 3600.0          # prune() drops outcomes older than this
MAX_OUTCOMES = 5000                  # hard bound; oldest evicted first
RUNNER_MULT = 2.0                    # peak multiple that counts as a runner
MIN_CLOSED_PER_TAG = 3               # min closed outcomes before a tag is trusted
HEAT_MIN = 0.5
HEAT_MAX = 2.0


def _s(value: Any) -> str:
    """Never-raising str()."""
    try:
        return str(value)
    except Exception:
        return ""


def classify_narrative(symbol: Any, name: Any,
                       description: Any = "") -> List[str]:
    """Tag a coin from its symbol/name/description.

    Case-insensitive substring match against ``NARRATIVE_KEYWORDS``.
    Returns the sorted unique matched tags, or ``["other"]`` when
    nothing matches. Never raises: ``None``/numbers/etc. are
    stringified first.
    """
    text = " ".join(_s(v) for v in (symbol, name, description)).lower()
    matched = [tag for tag, keywords in NARRATIVE_KEYWORDS.items()
               if any(kw in text for kw in keywords)]
    return sorted(set(matched)) or [OTHER]


class NarrativeHeat:
    """Heat multipliers per narrative, learned from closed outcomes.

    ``record_outcome`` feeds closed-runner results in; ``heat`` answers
    how hot a set of tags is right now, purely from recorded data.
    """

    def __init__(self) -> None:
        # (ts, tags tuple, peak mult, rug bool) — oldest on the left.
        self._outcomes: Deque[Tuple[float, Tuple[str, ...], float, bool]] = \
            deque(maxlen=MAX_OUTCOMES)

    # -- recording ------------------------------------------------------
    @staticmethod
    def _norm_tags(tags: Any) -> Tuple[str, ...]:
        if tags is None:
            return (OTHER,)
        if isinstance(tags, str):
            tags = [tags]
        try:
            items = list(tags)
        except TypeError:
            items = [tags]
        seen = set()
        out: List[str] = []
        for t in items:
            s = _s(t).strip().lower()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return tuple(sorted(out)) or (OTHER,)

    @staticmethod
    def _f(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def record_outcome(self, tags: Any, mult: Any, rug: Any,
                       now: Any) -> None:
        """Record one closed outcome. ``mult`` = peak multiple vs entry."""
        self._outcomes.append((self._f(now), self._norm_tags(tags),
                               self._f(mult), bool(rug)))

    # -- heat ------------------------------------------------------------
    def _recent(self, now: float, window_s: float = DAY_S) -> list:
        cutoff = self._f(now) - window_s
        return [o for o in self._outcomes if o[0] >= cutoff]

    @staticmethod
    def _is_runner(outcome: Tuple[float, Tuple[str, ...], float, bool]) -> bool:
        _ts, _tags, mult, rug = outcome
        return (not rug) and mult >= RUNNER_MULT

    def heat(self, tags: Any, now: Any) -> float:
        """Heat multiplier in [0.5, 2.0] for a set of tags.

        Per tag (last 24h): runner_rate = runners / closed; a tag needs
        at least ``MIN_CLOSED_PER_TAG`` closed outcomes to be trusted,
        otherwise it contributes 1.0. tag_heat = 1.0 +
        clamp(tag_runner_rate - global_runner_rate, -0.5, +1.0).
        The result is the average of the tag heats; ``["other"]`` and
        unknown tags are always neutral at 1.0.
        """
        norm = self._norm_tags(tags)
        if norm == (OTHER,):
            return 1.0
        now_f = self._f(now)
        recent = self._recent(now_f)
        if not recent:
            return 1.0
        global_rate = (sum(1 for o in recent if self._is_runner(o))
                       / len(recent))
        heats: List[float] = []
        for tag in norm:
            tagged = [o for o in recent if tag in o[1]]
            if len(tagged) < MIN_CLOSED_PER_TAG:
                heats.append(1.0)
                continue
            tag_rate = (sum(1 for o in tagged if self._is_runner(o))
                        / len(tagged))
            delta = max(-0.5, min(1.0, tag_rate - global_rate))
            heats.append(1.0 + delta)
        avg = sum(heats) / len(heats)
        return round(max(HEAT_MIN, min(HEAT_MAX, avg)), 3)

    def leaderboard(self, now: Any) -> List[Dict[str, Any]]:
        """Per-tag heat table, hottest first (read-only).

        One row per taxonomy tag with at least ``MIN_CLOSED_PER_TAG``
        closed outcomes in the last 24h:
        ``{"tag", "heat", "closed", "runners", "runner_rate"}``.
        Tags without enough data are omitted — never guessed.
        Powers the /heat leaderboard (#5).
        """
        now_f = self._f(now)
        recent = self._recent(now_f)
        if not recent:
            return []
        global_rate = (sum(1 for o in recent if self._is_runner(o))
                       / len(recent))
        rows: List[Dict[str, Any]] = []
        for tag in sorted(NARRATIVE_KEYWORDS):
            tagged = [o for o in recent if tag in o[1]]
            if len(tagged) < MIN_CLOSED_PER_TAG:
                continue
            n_run = sum(1 for o in tagged if self._is_runner(o))
            tag_rate = n_run / len(tagged)
            delta = max(-0.5, min(1.0, tag_rate - global_rate))
            rows.append({
                "tag": tag,
                "heat": round(max(HEAT_MIN, min(HEAT_MAX, 1.0 + delta)), 3),
                "closed": len(tagged),
                "runners": n_run,
                "runner_rate": round(tag_rate, 3),
            })
        rows.sort(key=lambda r: (-r["heat"], -r["closed"], r["tag"]))
        return rows

    # -- hygiene -----------------------------------------------------------
    def prune(self, now: Any) -> None:
        """Drop outcomes older than 72h."""
        cutoff = self._f(now) - OUTCOME_TTL_S
        self._outcomes = deque(
            (o for o in self._outcomes if o[0] >= cutoff),
            maxlen=MAX_OUTCOMES)

    def __len__(self) -> int:
        return len(self._outcomes)

    # -- persistence ---------------------------------------------------------
    def to_state(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "outcomes": [[ts, list(tags), mult, rug]
                         for ts, tags, mult, rug in self._outcomes],
        }

    @classmethod
    def from_state(cls, state: Any) -> "NarrativeHeat":
        """Tolerant rebuild: malformed input yields an empty tracker."""
        inst = cls()
        if not isinstance(state, dict):
            return inst
        outcomes = state.get("outcomes")
        if not isinstance(outcomes, list):
            return inst
        for item in outcomes:
            try:
                ts, tags, mult, rug = item
            except (TypeError, ValueError):
                continue
            inst.record_outcome(tags, mult, rug, ts)
        return inst

    def save(self, path: Any) -> None:
        """Persist to JSON with 0600 permissions (atomic replace)."""
        p = Path(_s(path))
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(self.to_state()), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
        os.chmod(p, 0o600)  # belt-and-braces: umask-proof the final file

    @classmethod
    def load(cls, path: Any) -> "NarrativeHeat":
        """Load from JSON; missing/corrupt file yields an empty tracker."""
        try:
            raw = Path(_s(path)).read_text(encoding="utf-8")
        except OSError:
            return cls()
        try:
            data = json.loads(raw)
        except ValueError:
            return cls()
        return cls.from_state(data)


def default_heat_path() -> Path:
    """~/.forex-agent/launch_scanner/narrative_heat.json.

    Honors FOREX_AGENT_HOME when set (test/dev convention).
    """
    base = os.environ.get("FOREX_AGENT_HOME") or str(Path.home() / ".forex-agent")
    return Path(base) / "launch_scanner" / "narrative_heat.json"
