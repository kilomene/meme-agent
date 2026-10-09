"""Outcome-driven feedback loop (idea #6): proposals, never auto-tuning.

Reads the REAL paper-outcome dataset (``backtest_dataset.jsonl``, written
by ``_backtest_record_paper_close`` — true entry/exit/peak prices, never
simulated) and proposes tuning adjustments for an allowlist of
NON-SAFETY knobs. Three learners:

* **ranking learner** — sniper threshold + narrative-heat boost (original #6)
* **entry learner** — which entry signals actually predicted runners
  (smart-money, fingerprints, velocity, buy pressure, deployer heat);
  proposes re-weighting the sniper score toward what worked
* **exit learner** — trailing/TP/hold/stop proposals from real exit
  reasons, giveback (peak vs realized), and dead-money analysis
* **context learner** — informational only (no knobs): which hours,
  sources, lanes, and narratives actually ran. Shown in ``/tune``.

Upgrades:
* proposals expire after 14 days (stale evidence)
* a knob can't be re-proposed within 7 days of being applied
* applied proposals are graded later: did the win rate actually improve?
  (``/tune`` shows helped / hurt / unclear)

Hard rules (user agreement 2026-09-28):
* Nothing is ever applied automatically. Proposals are stored, surfaced
  via the ``tuning.proposals`` event + ``/tune``, and only the user's
  explicit ``/tune apply <id>`` writes them to ``config_overrides.json``.
* ``SAFETY_NEVER_TUNE`` knobs can never be proposed AND are refused at
  apply time (defense in depth). The user's standing 1/100 safety bar,
  insider limits, bundle limits, and all hard-fail gates live here.
* A threshold proposal must preserve at least 80% of historical runners.
* Minimum evidence: ``feedback_min_outcomes`` (default 20) closed
  positions before any proposal is generated.

All state is JSON-serializable; no network, no secrets, stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from daemon.launch_scanner import winner_model
from daemon.launch_scanner import wavefront

logger = logging.getLogger("forex_agent.daemon.launch_scanner.feedback")

WAT = timezone(timedelta(hours=1))  # Africa/Lagos, no DST

# -- guardrails -----------------------------------------------------------
# Knobs that can NEVER be proposed and are REFUSED at apply time. The
# user's standing safety bar, insider/bundle gates, and every hard-fail
# gate live here. This list is intentionally broad: when in doubt, a
# knob belongs here, not in TUNABLE.
#
# NOTE: trail_trigger_pct / trail_drawdown_pct were parked here under
# the original conservative #6. The user explicitly asked for exit
# learning on 2026-09-28 — trailing is part of exits — so they moved to
# TUNABLE. Every change still needs an explicit /tune apply.
SAFETY_NEVER_TUNE = frozenset({
    "early_min_score",          # standing 1/100 quality bar (2026-09-27)
    "safety_gate",
    "safety_fail_closed",
    "bundle_max_cluster_pct",
    "insider_cluster_max_pct",
    "graph_insiders_max",
    "evm_sell_sim_enabled",
    "insider_watch_max",
    "instant_max_per_hour",     # spam caps are user-set, not learned
    "early_warn_max_per_hour",
    "daily_max_loss_sol",       # risk governor — never learned
    "max_positions",            # position sizing — never learned
    "entry_sol",                # position sizing — never learned
    "kill_switch_enabled",
})

# Knobs the analyzer is ALLOWED to propose. Everything else is out of
# scope. Ranking sharpness + exit strategy — never safety.
TUNABLE = frozenset({
    # ranking learner (original #6)
    "sniper_tier_threshold",
    "narrative_boost_max",
    # winner-likeness ranker (idea #5, user decision 2026-10-01):
    # minimum 0-100 score for a confirmed/lowcap runner to keep its
    # top-tier alert. Default 0 = OFF (score shown, nothing held).
    "winner_min_score",
    # second-wave measurement (idea #3, user decision 2026-10-01):
    # trigger knobs, proposed ONLY after >=10 settled waves prove an
    # edge (wavefront.analyze). Nothing auto-tunes.
    "second_wave_pullback_pct",
    "second_wave_releg_gain_pct",
    "second_wave_reentry_window_min",
    # entry learner: sniper signal weights
    "sniper_weight_smart",
    "sniper_weight_fp",
    "sniper_weight_velocity",
    "sniper_weight_pressure",
    "sniper_boost_deployer",
    # exit learner
    "take_profit_pct",
    "stop_loss_pct",
    "trail_trigger_pct",
    "trail_drawdown_pct",
    "max_hold_hours",
})

# Hard clamp per knob — apply_override clamps into these ranges rather
# than writing an absurd value.
KNOB_CLAMPS: Dict[str, Tuple[float, float]] = {
    "sniper_tier_threshold": (10.0, 95.0),
    "narrative_boost_max": (0.0, 20.0),
    "winner_min_score": (0.0, 100.0),
    "second_wave_pullback_pct": (20.0, 70.0),
    "second_wave_releg_gain_pct": (20.0, 150.0),
    "second_wave_reentry_window_min": (15.0, 180.0),
    "sniper_weight_smart": (0.0, 50.0),
    "sniper_weight_fp": (0.0, 50.0),
    "sniper_weight_velocity": (0.0, 50.0),
    "sniper_weight_pressure": (0.0, 50.0),
    "sniper_boost_deployer": (0.0, 20.0),
    "take_profit_pct": (50.0, 1000.0),
    "stop_loss_pct": (5.0, 60.0),
    "trail_trigger_pct": (10.0, 200.0),
    "trail_drawdown_pct": (5.0, 60.0),
    "max_hold_hours": (0.5, 48.0),
}

# Fallback defaults when the degen config lacks a knob (kept in sync
# with config/defaults.yaml).
KNOB_DEFAULTS: Dict[str, float] = {
    "sniper_tier_threshold": 50.0,
    "narrative_boost_max": 8.0,
    "winner_min_score": 0.0,
    "second_wave_pullback_pct": 40.0,
    "second_wave_releg_gain_pct": 50.0,
    "second_wave_reentry_window_min": 45.0,
    "sniper_weight_smart": 35.0,
    "sniper_weight_fp": 30.0,
    "sniper_weight_velocity": 20.0,
    "sniper_weight_pressure": 10.0,
    "sniper_boost_deployer": 5.0,
    "take_profit_pct": 200.0,
    "stop_loss_pct": 30.0,
    "trail_trigger_pct": 50.0,
    "trail_drawdown_pct": 30.0,
    "max_hold_hours": 4.0,
}

RUNNER_MULT = 2.0          # peak multiple that counts as a runner
RUNNER_KEEP_PCT = 0.80     # a proposal must keep >= this share of runners
HOT_HEAT = 1.3             # narrative_heat above this counts as "hot"
THRESHOLD_STEP = 5.0       # sniper threshold proposals move in 5-pt steps
BOOST_STEP = 4.0           # narrative_boost_max proposals move in 4-pt steps
BOOST_MIN, BOOST_MAX = 0.0, 20.0
WEIGHT_STEP = 5.0          # entry-learner weight proposals move in 5-pt steps
MAX_NEW_PER_SCAN = 5       # never flood the user with proposals
PROPOSAL_TTL_DAYS = 14     # open proposals expire (stale evidence)
APPLY_COOLDOWN_DAYS = 7    # one change per knob per week (no thrash)
FOLLOWUP_MIN_CLOSES = 10   # closes after apply before grading it
FOLLOWUP_MAX_DAYS = 30     # stop waiting for follow-up after this long


# -- paths -----------------------------------------------------------------
def default_proposals_path() -> Path:
    base = os.environ.get("FOREX_AGENT_HOME") or str(Path.home() / ".forex-agent")
    return Path(base) / "launch_scanner" / "tune_proposals.json"


def default_dataset_path(degen: Any) -> str:
    return os.path.expanduser(str(
        getattr(degen, "backtest_dataset_path",
                "~/.forex-agent/launch_scanner/backtest_dataset.jsonl")))


# -- dataset ----------------------------------------------------------------
def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _knob(degen: Any, name: str) -> float:
    return _f(getattr(degen, name, KNOB_DEFAULTS.get(name, 0.0)),
              KNOB_DEFAULTS.get(name, 0.0))


def load_outcomes(path: Any) -> List[Dict[str, Any]]:
    """Read real paper closes from the backtest dataset. Never raises."""
    out: List[Dict[str, Any]] = []
    try:
        with open(os.path.expanduser(str(path)), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("outcome_kind") == "paper":
                    out.append(rec)
    except OSError:
        pass
    return out


def _is_runner(rec: Dict[str, Any]) -> bool:
    return (not rec.get("rug")) and _f(rec.get("peak_mult")) >= RUNNER_MULT


def _stats(recs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Win-rate snapshot: runner rate + average realized multiple."""
    n = len(recs)
    if not n:
        return {"n": 0, "runner_rate": 0.0, "avg_realized": 0.0}
    realized = [_f(r.get("realized_mult"), 1.0) for r in recs]
    return {
        "n": n,
        "runner_rate": round(sum(1 for r in recs if _is_runner(r)) / n, 3),
        "avg_realized": round(sum(realized) / n, 3),
    }


# -- proposal store -----------------------------------------------------------
def _load_store(path: Any = None) -> Dict[str, Any]:
    p = Path(str(path)) if path else default_proposals_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 2, "proposals": [], "insights": [],
                "insights_ts": 0.0}
    if not isinstance(data, dict):
        return {"version": 2, "proposals": [], "insights": [],
                "insights_ts": 0.0}
    items = data.get("proposals")
    data["proposals"] = [x for x in items if isinstance(x, dict)] \
        if items else []
    if not isinstance(data.get("insights"), list):
        data["insights"] = []
    return data


def _save_store(store: Dict[str, Any], path: Any = None) -> None:
    p = Path(str(path)) if path else default_proposals_path()
    store["version"] = 2
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(store), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    os.chmod(p, 0o600)


def load_proposals(path: Any = None) -> List[Dict[str, Any]]:
    return _load_store(path)["proposals"]


def save_proposals(proposals: List[Dict[str, Any]],
                   path: Any = None) -> None:
    store = _load_store(path)
    store["proposals"] = proposals
    _save_store(store, path)


def load_insights(path: Any = None) -> List[Dict[str, Any]]:
    return _load_store(path)["insights"]


def save_insights(insights: List[Dict[str, Any]], now: float,
                  path: Any = None) -> None:
    store = _load_store(path)
    store["insights"] = insights
    store["insights_ts"] = now
    _save_store(store, path)


def _proposal_id(knob: str, suggested: Any, day: str) -> str:
    digest = hashlib.sha256(
        ("%s|%s|%s" % (knob, suggested, day)).encode()).hexdigest()
    return "T-" + digest[:6].upper()


def add_proposals(new: List[Dict[str, Any]],
                  path: Any = None) -> List[Dict[str, Any]]:
    """Append proposals, skipping dupes (same open knob+suggestion).

    Returns the proposals that were actually added.
    """
    store = _load_store(path)
    existing = store["proposals"]
    open_keys = {(p.get("knob"), str(p.get("suggested")))
                 for p in existing if p.get("status") == "open"}
    added: List[Dict[str, Any]] = []
    for p in new:
        if p.get("knob") in SAFETY_NEVER_TUNE:
            continue  # never even store a safety-knob proposal
        if p.get("knob") not in TUNABLE:
            continue
        key = (p.get("knob"), str(p.get("suggested")))
        if key in open_keys:
            continue
        existing.append(p)
        open_keys.add(key)
        added.append(p)
    # Bound the store: keep the newest 100.
    store["proposals"] = existing[-100:]
    _save_store(store, path)
    return added


def mark_proposal(proposal_id: str, status: str,
                  path: Any = None) -> Optional[Dict[str, Any]]:
    """Set a proposal's status ('applied' | 'dismissed' | 'expired').

    Never raises.
    """
    store = _load_store(path)
    hit = None
    for p in store["proposals"]:
        if p.get("id") == proposal_id and p.get("status") == "open":
            p["status"] = status
            p["decided_ts"] = time.time()
            hit = p
            break
    if hit:
        _save_store(store, path)
    return hit


def expire_stale(path: Any = None, now: Optional[float] = None) -> int:
    """Expire open proposals older than PROPOSAL_TTL_DAYS. Returns count."""
    now = now if now is not None else time.time()
    store = _load_store(path)
    n = 0
    for p in store["proposals"]:
        if p.get("status") == "open" and \
                now - float(p.get("created_ts") or 0) > \
                PROPOSAL_TTL_DAYS * 86400:
            p["status"] = "expired"
            p["decided_ts"] = now
            n += 1
    if n:
        _save_store(store, path)
    return n


def _knob_cooldown_ok(knob: str, existing: List[Dict[str, Any]],
                      now: float) -> bool:
    """True if the knob wasn't applied within APPLY_COOLDOWN_DAYS."""
    for p in existing:
        if p.get("knob") == knob and p.get("status") == "applied":
            if now - float(p.get("decided_ts") or 0) < \
                    APPLY_COOLDOWN_DAYS * 86400:
                return False
    return True


def apply_with_baseline(proposal_id: str, recs: List[Dict[str, Any]],
                        path: Any = None,
                        now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Mark applied AND snapshot the current win rate as baseline for
    later grading. Returns the proposal or None. Never raises."""
    now = now if now is not None else time.time()
    try:
        hit = mark_proposal(proposal_id, "applied", path)
        if hit is not None:
            hit["baseline"] = _stats(recs[-30:])
            hit["followup"] = "pending"
            store = _load_store(path)
            for p in store["proposals"]:
                if p.get("id") == proposal_id:
                    p.update(hit)
                    break
            _save_store(store, path)
        return hit
    except Exception:
        return None


def grade_applied(path: Any, recs: List[Dict[str, Any]],
                  now: float) -> int:
    """Grade applied proposals: did the win rate actually improve?

    Compares closes after the apply against the baseline snapshot.
    Sets followup to 'helped' | 'hurt' | 'unclear'. Returns graded count.
    Never raises.
    """
    try:
        store = _load_store(path)
        n = 0
        for p in store["proposals"]:
            if p.get("status") != "applied":
                continue
            if p.get("followup") not in (None, "pending"):
                continue
            decided = float(p.get("decided_ts") or 0)
            if now - decided > FOLLOWUP_MAX_DAYS * 86400:
                p["followup"] = "unclear"
                n += 1
                continue
            after = [r for r in recs
                     if float(r.get("t_close") or 0) > decided]
            if len(after) < FOLLOWUP_MIN_CLOSES:
                continue
            base = p.get("baseline") or {}
            if int(base.get("n") or 0) < 5:
                p["followup"] = "unclear"  # no baseline to compare
                n += 1
                continue
            cur = _stats(after)
            p["followup_stats"] = cur
            br = float(base.get("runner_rate") or 0)
            cr = cur["runner_rate"]
            if cr >= br + 0.10:
                p["followup"] = "helped"
            elif cr <= br - 0.10:
                p["followup"] = "hurt"
            else:
                p["followup"] = "unclear"
            n += 1
        if n:
            _save_store(store, path)
        return n
    except Exception:
        return 0


# -- analysis ------------------------------------------------------------------
def _bucket_rate(rows: List[Dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    return sum(1 for r in rows if _is_runner(r)) / len(rows)


def _mk(knob: str, current: float, suggested: float, area: str,
        rationale: str, evidence: Dict[str, Any], now: float,
        day: str) -> Dict[str, Any]:
    return {
        "id": _proposal_id(knob, round(suggested, 2), day),
        "knob": knob,
        "area": area,  # ranking | entries | exits
        "current": round(current, 2),
        "suggested": round(suggested, 2),
        "rationale": rationale,
        "evidence": evidence,
        "created_ts": now,
        "status": "open",
    }


def _min_n(degen: Any) -> int:
    return max(5, int(getattr(degen, "feedback_min_outcomes", 20) or 20))


def analyze_ranking(degen: Any, recs: List[Dict[str, Any]], now: float,
                    day: str, min_n: int) -> List[Dict[str, Any]]:
    """Original #6: sniper threshold + narrative-heat boost proposals."""
    proposals: List[Dict[str, Any]] = []
    # --- raise the sniper tier threshold --------------------------------
    scored = [r for r in recs
              if isinstance(r.get("sniper_score"), (int, float))]
    if len(scored) >= min_n:
        thr = _knob(degen, "sniper_tier_threshold")
        lo = [r for r in scored
              if thr <= _f(r["sniper_score"]) < thr + THRESHOLD_STEP]
        hi = [r for r in scored if _f(r["sniper_score"]) >= thr + THRESHOLD_STEP]
        if len(lo) >= 5 and hi:
            lo_rate, hi_rate = _bucket_rate(lo), _bucket_rate(hi)
            if hi_rate > 0 and lo_rate < 0.5 * hi_rate:
                runners = [r for r in scored if _is_runner(r)]
                kept = [r for r in runners
                        if _f(r["sniper_score"]) >= thr + THRESHOLD_STEP]
                keep_pct = len(kept) / len(runners) if runners else 0.0
                if runners and keep_pct >= RUNNER_KEEP_PCT:
                    proposals.append(_mk(
                        "sniper_tier_threshold", thr, thr + THRESHOLD_STEP,
                        "ranking",
                        "Coins scoring %.0f–%.0f ran %.1f%% of the time "
                        "vs %.1f%% for coins scoring %.0f+. Raising the "
                        "bar cuts the weak band while keeping %.0f%% of "
                        "past runners." % (
                            thr, thr + THRESHOLD_STEP, lo_rate * 100,
                            hi_rate * 100, thr + THRESHOLD_STEP,
                            keep_pct * 100),
                        {"band_low": [len(lo), round(lo_rate, 3)],
                         "band_high": [len(hi), round(hi_rate, 3)],
                         "runners_kept": [len(kept), len(runners)],
                         "outcomes": len(scored)},
                        now, day))
    # --- adjust the narrative-heat boost ---------------------------------
    heated = [r for r in recs
              if isinstance(r.get("narrative_heat"), (int, float))]
    if len(heated) >= min_n:
        hot = [r for r in heated if _f(r["narrative_heat"]) > HOT_HEAT]
        rest = [r for r in heated if _f(r["narrative_heat"]) <= HOT_HEAT]
        if len(hot) >= 5 and rest:
            hot_rate, rest_rate = _bucket_rate(hot), _bucket_rate(rest)
            cur = _knob(degen, "narrative_boost_max")
            suggested = None
            if rest_rate > 0 and hot_rate >= 1.5 * rest_rate:
                suggested = min(BOOST_MAX, cur + BOOST_STEP)
                why = ("Hot-narrative entries ran %.1f%% vs %.1f%% for the "
                       "rest — the heat boost is earning its keep; giving "
                       "it more weight." % (hot_rate * 100, rest_rate * 100))
            elif hot_rate < 0.7 * rest_rate:
                suggested = max(BOOST_MIN, cur - BOOST_STEP)
                why = ("Hot-narrative entries ran %.1f%% vs %.1f%% for the "
                       "rest — the heat boost is not pulling its weight; "
                       "shrinking it." % (hot_rate * 100, rest_rate * 100))
            if suggested is not None and suggested != cur:
                proposals.append(_mk(
                    "narrative_boost_max", cur, suggested, "ranking", why,
                    {"hot": [len(hot), round(hot_rate, 3)],
                     "rest": [len(rest), round(rest_rate, 3)],
                     "outcomes": len(heated)},
                    now, day))
    return proposals


# Entry learner: which signals actually predicted runners? Each component
# maps to its sniper weight knob.
ENTRY_COMPONENTS = [
    ("smart_buys", "sniper_weight_smart",
     lambda s: _f(s.get("smart_buys")) >= 2, "2+ smart-money buys"),
    ("fp", "sniper_weight_fp",
     lambda s: str(s.get("fp_verdict") or "").upper() == "HOT",
     "HOT fingerprint"),
    ("velocity", "sniper_weight_velocity",
     lambda s: _f(s.get("rising_polls")) >= 2, "2+ rising polls"),
    ("pressure", "sniper_weight_pressure",
     lambda s: _f(s.get("buy_pressure")) >= 0.6, "buy pressure ≥ 0.6"),
    ("deployer", "sniper_boost_deployer",
     lambda s: bool(s.get("hot_deployer")), "hot deployer"),
]


def analyze_entry(degen: Any, recs: List[Dict[str, Any]], now: float,
                  day: str, min_n: int) -> List[Dict[str, Any]]:
    """Entry learner: re-weight sniper signals toward what predicted
    runners. Max 2 proposals per scan (strongest first)."""
    sig = [r for r in recs if isinstance(r.get("entry_signals"), dict)]
    if len(sig) < min_n:
        return []
    scored: List[Tuple[float, Dict[str, Any]]] = []  # (strength, proposal)
    for _key, knob, test, label in ENTRY_COMPONENTS:
        has = [r for r in sig if test(r["entry_signals"])]
        lack = [r for r in sig if not test(r["entry_signals"])]
        if len(has) < 8 or not lack:
            continue
        hr, lr = _bucket_rate(has), _bucket_rate(lack)
        cur = _knob(degen, knob)
        lo_c, hi_c = KNOB_CLAMPS[knob]
        strong = (lr > 0 and hr >= 1.8 * lr) or (lr == 0 and hr >= 0.5)
        if strong and (hr - lr) >= 0.15 and cur < hi_c:
            sug = min(hi_c, cur + WEIGHT_STEP)
            scored.append((hr - lr, _mk(
                knob, cur, sug, "entries",
                "Coins with %s ran %.1f%% vs %.1f%% without — that "
                "signal predicts winners; weighting it heavier." % (
                    label, hr * 100, lr * 100),
                {"with": [len(has), round(hr, 3)],
                 "without": [len(lack), round(lr, 3)],
                 "outcomes": len(sig)},
                now, day)))
        elif lr > 0 and hr < 0.6 * lr and cur > lo_c:
            sug = max(lo_c, cur - WEIGHT_STEP)
            scored.append((lr - hr, _mk(
                knob, cur, sug, "entries",
                "Coins with %s ran %.1f%% vs %.1f%% without — that "
                "signal is noise; weighting it down." % (
                    label, hr * 100, lr * 100),
                {"with": [len(has), round(hr, 3)],
                 "without": [len(lack), round(lr, 3)],
                 "outcomes": len(sig)},
                now, day)))
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:2]]


EXIT_REASONS = ("stop_loss", "take_profit", "max_hold", "trail_stop",
                "profit_lock", "dead_pool")


def analyze_exits(degen: Any, recs: List[Dict[str, Any]], now: float,
                  day: str, min_n: int) -> List[Dict[str, Any]]:
    """Exit learner: trailing / TP / hold / stop proposals from real
    exit reasons, giveback (peak vs realized), and dead-money analysis."""
    ex = [r for r in recs if r.get("exit_reason") in EXIT_REASONS]
    if len(ex) < min_n:
        return []
    out: List[Dict[str, Any]] = []

    def ev(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"n": len(rows), "runner_rate": round(_bucket_rate(rows), 3)}

    # --- trailing drawdown: giveback vs shakeout -------------------------
    # profit_lock exits are trail exits too (the Leg A early stage) —
    # they belong in the same giveback analysis.
    trail_all = [r for r in ex if r["exit_reason"] in
                 ("trail_stop", "profit_lock")
                 and _f(r.get("realized_mult")) >= 1.0]
    trail = [r for r in trail_all if _f(r.get("realized_mult")) >= 1.2]
    cur_dd = _knob(degen, "trail_drawdown_pct")
    if len(trail) >= 8:
        give = sum(_f(r.get("peak_mult")) - _f(r.get("realized_mult"))
                   for r in trail) / len(trail)
        if give >= 1.0 and cur_dd > KNOB_CLAMPS["trail_drawdown_pct"][0]:
            sug = max(KNOB_CLAMPS["trail_drawdown_pct"][0], cur_dd - 5.0)
            out.append(_mk(
                "trail_drawdown_pct", cur_dd, sug, "exits",
                "Profitable trail exits gave back %.1fx on average "
                "(peak vs exit). Tightening the trail locks more of "
                "each run in." % give,
                {"trail_exits": ev(trail),
                 "avg_giveback_x": round(give, 2)}, now, day))
    if len(trail_all) >= 8:
        shook = [r for r in trail_all
                 if _f(r.get("realized_mult")) < 1.25
                 and _f(r.get("peak_mult")) >= 2.0]
        if len(shook) >= 5 and len(shook) / len(trail_all) >= 0.3 \
                and cur_dd < KNOB_CLAMPS["trail_drawdown_pct"][1]:
            sug = min(KNOB_CLAMPS["trail_drawdown_pct"][1], cur_dd + 5.0)
            out.append(_mk(
                "trail_drawdown_pct", cur_dd, sug, "exits",
                "%d of %d trail exits (%.0f%%) were runners shaken out "
                "for under +25%% — the trail is too tight and cuts "
                "winners early." % (len(shook), len(trail_all),
                                    100.0 * len(shook) / len(trail_all)),
                {"trail_exits": ev(trail_all),
                 "shaken_out": [len(shook), len(trail_all)]}, now, day))

    # --- take profit: is the target a fantasy? ---------------------------
    tp_hits = [r for r in ex if r["exit_reason"] == "take_profit"]
    hit_rate = len(tp_hits) / len(ex)
    runners = [r for r in ex if _is_runner(r)]
    missed = [r for r in runners if r["exit_reason"] != "take_profit"]
    cur_tp = _knob(degen, "take_profit_pct")
    if hit_rate < 0.15 and len(missed) >= 8 \
            and cur_tp > KNOB_CLAMPS["take_profit_pct"][0]:
        med_peak = statistics.median(_f(r.get("peak_mult")) for r in missed)
        if med_peak < 1 + cur_tp / 100.0:
            sug = max(KNOB_CLAMPS["take_profit_pct"][0], cur_tp - 25.0)
            out.append(_mk(
                "take_profit_pct", cur_tp, sug, "exits",
                "Take-profit hit only %.1f%% of closes; runners typically "
                "peaked at %.1fx, below the +%.0f%% target. Lowering the "
                "target banks more winners instead of watching them fade."
                % (hit_rate * 100, med_peak, cur_tp),
                {"tp_hit_rate": round(hit_rate, 3),
                 "missed_runners": [len(missed), len(runners)],
                 "median_runner_peak": round(med_peak, 2)}, now, day))

    # --- max hold: dead money sitting the full hold ----------------------
    mh = [r for r in ex if r["exit_reason"] == "max_hold"]
    if len(mh) >= 8:
        flat = [r for r in mh
                if 0.85 <= _f(r.get("realized_mult")) <= 1.25]
        if len(flat) / len(mh) >= 0.5:
            cur_h = _knob(degen, "max_hold_hours")
            if cur_h > KNOB_CLAMPS["max_hold_hours"][0]:
                sug = max(KNOB_CLAMPS["max_hold_hours"][0], cur_h - 1.0)
                out.append(_mk(
                    "max_hold_hours", cur_h, sug, "exits",
                    "%.0f%% of max-hold exits (%d/%d) sat flat the whole "
                    "hold (±25%%) — dead money. A shorter hold recycles "
                    "capital into fresh coins faster." % (
                        100.0 * len(flat) / len(mh), len(flat), len(mh)),
                    {"max_hold_exits": ev(mh),
                     "flat": [len(flat), len(mh)]}, now, day))

    # --- wicked stops: winners round-tripped to the stop -----------------
    # If a trade peaked past the trail trigger but still exited at the
    # stop-loss, the trailing lock-in armed too late to save it.
    sl = [r for r in ex if r["exit_reason"] == "stop_loss"]
    trig = _knob(degen, "trail_trigger_pct")
    if len(sl) >= 8 and trig > KNOB_CLAMPS["trail_trigger_pct"][0]:
        wicked = [r for r in sl
                  if _f(r.get("peak_mult")) >= 1 + trig / 100.0]
        if len(wicked) >= 5 and len(wicked) / len(sl) >= 0.4:
            sug = max(KNOB_CLAMPS["trail_trigger_pct"][0], trig - 10.0)
            out.append(_mk(
                "trail_trigger_pct", trig, sug, "exits",
                "%d of %d stop-outs (%d%%) had peaked past +%.0f%% first — "
                "winners round-tripped to the stop because the trailing "
                "lock-in armed too late. Arming it earlier saves them."
                % (len(wicked), len(sl),
                   round(100.0 * len(wicked) / len(sl)), trig),
                {"stop_exits": ev(sl),
                 "wicked": [len(wicked), len(sl)]}, now, day))

    # --- stop-loss width: too tight (shakeouts) vs too loose (bleeds) ---
    # The stop is the highest-leverage exit knob — most closes end here.
    # Two failure modes, told apart by real evidence:
    #   too tight: stops fire fast on noise, cutting coins that showed life
    #     -> widen, give entries room to breathe.
    #   too loose: stops are slow bleeds on coins that never showed life
    #     -> tighten, bank a smaller loss per stopped trade.
    # The two proposals are mutually exclusive per scan (widen wins ties:
    # a shakeout destroys a winner, a bleed only costs basis points).
    cur_sl = _knob(degen, "stop_loss_pct")
    lo_sl, hi_sl = KNOB_CLAMPS["stop_loss_pct"]

    def _hold_min(r: Dict[str, Any]) -> float:
        hm = _f(r.get("hold_min"), -1.0)
        if hm >= 0:
            return hm
        t0, t1 = _f(r.get("t_entry"), 0.0), _f(r.get("t_close"), 0.0)
        if t1 > t0 > 0:
            return (t1 - t0) / 60.0
        return 0.0

    if len(sl) >= 8:
        sl_share = len(sl) / len(ex)
        fast = [r for r in sl if _hold_min(r) < 45.0]
        promising = [r for r in sl if _f(r.get("peak_mult")) >= 1.15]
        dead = [r for r in sl if _f(r.get("peak_mult")) < 1.05
                and _hold_min(r) >= 60.0]
        widen_ev = (sl_share >= 0.35
                    and (len(fast) / len(sl) >= 0.5
                         or (len(promising) >= 5
                             and len(promising) / len(sl) >= 0.3)))
        tighten_ev = (sl_share >= 0.35 and len(dead) >= 5
                      and len(dead) / len(sl) >= 0.4)
        if widen_ev and cur_sl < hi_sl:
            sug = min(hi_sl, cur_sl + 5.0)
            out.append(_mk(
                "stop_loss_pct", cur_sl, sug, "exits",
                "Stop-loss took %.0f%% of closes (%d/%d): %d died within "
                "45 minutes and %d had peaked +15%% first — the -%.0f%% "
                "stop is cutting on noise. Widening to -%.0f%% gives "
                "entries room to breathe."
                % (100.0 * sl_share, len(sl), len(ex), len(fast),
                   len(promising), cur_sl, sug),
                {"stop_exits": ev(sl),
                 "fast_stopouts": [len(fast), len(sl)],
                 "promising_stopouts": [len(promising), len(sl)]},
                now, day))
        elif tighten_ev and cur_sl > lo_sl:
            sug = max(lo_sl, cur_sl - 5.0)
            out.append(_mk(
                "stop_loss_pct", cur_sl, sug, "exits",
                "Stop-loss took %.0f%% of closes: %d of %d stop-outs "
                "never peaked +5%% and bled for 60+ minutes — slow "
                "bleeds on dead coins. Tightening to -%.0f%% banks a "
                "smaller loss per stopped trade."
                % (100.0 * sl_share, len(dead), len(sl), sug),
                {"stop_exits": ev(sl),
                 "dead_bleeds": [len(dead), len(sl)]}, now, day))
    return out


def analyze_context(recs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Context learner — informational only. No knobs, no proposals:
    which hours, sources, lanes, and narratives actually ran."""
    if len(recs) < 10:
        return []
    insights: List[Dict[str, Any]] = []
    overall = _bucket_rate(recs)

    def _note(kind: str, label: str, rows: List[Dict[str, Any]]) -> None:
        n = len(rows)
        if n < 10:
            return
        rate = _bucket_rate(rows)
        if overall > 0 and rate >= 1.8 * overall:
            insights.append({
                "kind": kind, "label": label, "mood": "hot",
                "detail": "ran %.0f%% (%d/%d) vs %.0f%% overall" % (
                    rate * 100, sum(1 for r in rows if _is_runner(r)),
                    n, overall * 100),
                "n": n})
        elif rate <= 0.5 * overall:
            insights.append({
                "kind": kind, "label": label, "mood": "cold",
                "detail": "ran %.0f%% (%d/%d) vs %.0f%% overall" % (
                    rate * 100, sum(1 for r in rows if _is_runner(r)),
                    n, overall * 100),
                "n": n})

    # hour of day (WAT) in 6-hour blocks
    blocks: Dict[str, List[Dict[str, Any]]] = {}
    for r in recs:
        t = r.get("t_entry") or r.get("t0")
        if not t:
            continue
        try:
            h = datetime.fromtimestamp(float(t), WAT).hour
        except (TypeError, ValueError):
            continue
        blocks.setdefault("%02d–%02d WAT" % ((h // 6) * 6,
                                             ((h // 6) * 6 + 6) % 24),
                          []).append(r)
    for label in sorted(blocks):
        _note("hour", label, blocks[label])
    # source (pump.fun, geckoterminal, …)
    by_src: Dict[str, List[Dict[str, Any]]] = {}
    for r in recs:
        by_src.setdefault(str(r.get("source") or "unknown"), []).append(r)
    for label in sorted(by_src):
        _note("source", label, by_src[label])
    # lane (position label: launch / momentum / second_wave / …)
    by_lane: Dict[str, List[Dict[str, Any]]] = {}
    for r in recs:
        by_lane.setdefault(str(r.get("label") or "launch"), []).append(r)
    for label in sorted(by_lane):
        _note("lane", label, by_lane[label])
    # narrative tags
    by_tag: Dict[str, List[Dict[str, Any]]] = {}
    for r in recs:
        tags = r.get("narratives") or ["other"]
        for t in tags:
            by_tag.setdefault(str(t), []).append(r)
    for label in sorted(by_tag):
        _note("narrative", label, by_tag[label])
    # strongest first
    insights.sort(key=lambda i: -i["n"])
    return insights[:6]


def analyze(degen: Any, now: Optional[float] = None,
            existing: Optional[List[Dict[str, Any]]] = None,
            insider_rows: Optional[List[Dict[str, Any]]] = None,
            wave_rows: Optional[List[Dict[str, Any]]] = None
            ) -> List[Dict[str, Any]]:
    """Generate tuning proposals from real outcomes. Pure analysis —
    never applies anything, never touches live config.

    ``insider_rows`` is the insider-outcome ledger (state key
    ``insider_outcomes``) — lets the winner ranker use insider
    direction/cluster features. ``wave_rows`` is the settled
    second-wave ledger (state key ``wavefront_ledger``) — lets the
    second-wave measurement propose trigger tweaks only on proven
    evidence. Both optional.

    Returns a list of proposal dicts
    (possibly empty), capped at MAX_NEW_PER_SCAN. Knobs on
    apply-cooldown are skipped.
    """
    now = now if now is not None else time.time()
    day = datetime.fromtimestamp(now, WAT).date().isoformat()
    min_n = _min_n(degen)
    recs = load_outcomes(default_dataset_path(degen))
    if len(recs) < min_n:
        return []
    existing = existing if existing is not None else load_proposals()
    proposals: List[Dict[str, Any]] = []
    proposals += analyze_ranking(degen, recs, now, day, min_n)
    proposals += analyze_entry(degen, recs, now, day, min_n)
    proposals += analyze_exits(degen, recs, now, day, min_n)
    # Winner-likeness ranker (idea #5, user decision 2026-10-01):
    # proposes a winner_min_score threshold from real 10x+ outcomes.
    proposals += winner_model.analyze_winner_ranking(
        degen, recs, insider_rows, now, day, min_n)
    # Second-wave measurement (idea #3, user decision 2026-10-01):
    # proposes trigger tweaks ONLY after >=10 settled waves prove an
    # edge. Proposals only — /tune apply is the only write path.
    proposals += wavefront.analyze(degen, wave_rows or [])
    # guards: allowlist, cooldown, cap
    out: List[Dict[str, Any]] = []
    for p in proposals:
        if p["knob"] in SAFETY_NEVER_TUNE or p["knob"] not in TUNABLE:
            continue
        if not _knob_cooldown_ok(p["knob"], existing, now):
            continue
        out.append(p)
        if len(out) >= MAX_NEW_PER_SCAN:
            break
    return out


def run_scan(degen: Any, now: Optional[float] = None,
             path: Any = None,
             insider_rows: Optional[List[Dict[str, Any]]] = None,
             wave_rows: Optional[List[Dict[str, Any]]] = None
             ) -> Tuple[List[Dict[str, Any]],
                        List[Dict[str, Any]]]:
    """Full scan: expire stale, grade applied, analyze, store.

    Returns (newly added proposals, current insights). Never applies.
    """
    now = now if now is not None else time.time()
    expire_stale(path, now)
    recs = load_outcomes(default_dataset_path(degen))
    grade_applied(path, recs, now)
    existing = load_proposals(path)
    added = add_proposals(
        analyze(degen, now, existing, insider_rows, wave_rows), path)
    insights = analyze_context(recs)
    save_insights(insights, now, path)
    return added, insights


# -- user-approved overrides ------------------------------------------------------
def overrides_path() -> Path:
    """~/.forex-agent/config_overrides.json (honors FOREX_AGENT_HOME)."""
    base = os.environ.get("FOREX_AGENT_HOME") or str(Path.home() / ".forex-agent")
    return Path(base) / "config_overrides.json"


def apply_override(knob: str, value: Any,
                   path: Any = None) -> Dict[str, Any]:
    """Write one user-approved knob override. Returns the full overrides.

    The ONLY write path to config_overrides.json. Raises ValueError for
    safety-never-tune knobs (refused, never written) and for knobs
    outside TUNABLE. Values are clamped into KNOB_CLAMPS. File is 0600.
    """
    if knob in SAFETY_NEVER_TUNE:
        raise ValueError("safety knob %r can never be tuned" % (knob,))
    if knob not in TUNABLE:
        raise ValueError("knob %r is not tunable" % (knob,))
    lo, hi = KNOB_CLAMPS[knob]
    try:
        value = min(hi, max(lo, float(value)))
    except (TypeError, ValueError):
        raise ValueError("knob %r needs a number" % (knob,))
    # keep ints integral (thresholds/weights read as floats downstream,
    # but the file stays clean)
    p = Path(str(path)) if path else overrides_path()
    data: Dict[str, Any] = {}
    try:
        raw = p.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            data = parsed
    except (OSError, ValueError):
        data = {}
    node = data.get("degen")
    if not isinstance(node, dict):
        node = {}
        data["degen"] = node
    node[knob] = value
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    os.chmod(p, 0o600)
    return data


# -- daily scan hook ------------------------------------------------------------
def maybe_daily_scan(config: Any, state: Dict[str, Any],
                     now: float) -> List[Dict[str, Any]]:
    """Run the analysis once per WAT calendar day.

    Stores new proposals + insights; returns the proposals added
    (possibly empty). Never raises; never applies anything.
    """
    try:
        degen = config.degen
        if not bool(getattr(degen, "feedback_enabled", True)):
            return []
        day = datetime.fromtimestamp(now, WAT).date().isoformat()
        if state.get("feedback_last_scan_day") == day:
            return []
        state["feedback_last_scan_day"] = day
        new, _insights = run_scan(
            degen, now,
            insider_rows=state.get("insider_outcomes"),
            wave_rows=state.get("wavefront_ledger"))
        return new
    except Exception:
        return []


# -- weekly learner review (user decision 2026-09-28) -------------------------------
# One card per week: new proposals, applied changes + their grades, and
# the week's win rate vs the prior week. Informational only — the review
# never applies anything; /tune apply stays the only write path.

_REVIEW_STATE_KEY = "review_last_slot"


def _review_slot(degen: Any, now: float) -> Optional[str]:
    """This week's review slot key (WAT), or None if not due yet."""
    try:
        weekday = int(getattr(degen, "review_weekday", 0) or 0) % 7
        raw = str(getattr(degen, "review_time", "08:00") or "08:00")
        hh, mm = raw.split(":")
        h, m = int(hh), int(mm)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            h, m = 8, 0
    except (TypeError, ValueError, AttributeError):
        weekday, h, m = 0, 8, 0
    day = datetime.fromtimestamp(now, WAT)
    monday = day - timedelta(days=day.weekday())
    slot = monday.replace(hour=h, minute=m, second=0, microsecond=0)
    if slot.timestamp() > now:
        return None
    return slot.strftime("%Y-%m-%d")


def build_weekly_review(degen: Any, now: float,
                        path: Any = None) -> Dict[str, Any]:
    """Pure builder: the weekly review payload. Never raises, never
    applies anything. Safe to call on demand (the /review command)."""
    try:
        week_ago = now - 7 * 86400
        two_ago = now - 14 * 86400
        store = _load_store(path)
        props = store.get("proposals", [])

        def _ts(p: Dict[str, Any], key: str) -> float:
            try:
                return float(p.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0

        new_p = [p for p in props
                 if p.get("status") == "open" and _ts(p, "created_ts") >= week_ago]
        open_older = sum(1 for p in props
                         if p.get("status") == "open" and _ts(p, "created_ts") < week_ago)
        applied = [p for p in props
                   if p.get("status") == "applied" and _ts(p, "decided_ts") >= week_ago]

        recs = load_outcomes(default_dataset_path(degen))
        this_wk = [r for r in recs if _f(r.get("t_close"), 0) >= week_ago]
        prev_wk = [r for r in recs
                   if two_ago <= _f(r.get("t_close"), 0) < week_ago]
        stats = _stats(this_wk)
        prev = _stats(prev_wk)
        stats["prev_n"] = prev["n"]
        stats["prev_runner_rate"] = prev["runner_rate"]

        insights = store.get("insights", [])[:3]
        monday = (datetime.fromtimestamp(now, WAT)
                  - timedelta(days=datetime.fromtimestamp(now, WAT).weekday()))
        return {
            "event": "tuning.weekly_review",
            "week": monday.strftime("%Y-%m-%d"),
            "new_proposals": [
                {"id": p.get("id"), "knob": p.get("knob"),
                 "area": p.get("area"), "current": p.get("current"),
                 "suggested": p.get("suggested")}
                for p in new_p[:5]
            ],
            "new_proposals_total": len(new_p),
            "open_older": open_older,
            "applied": [
                {"knob": p.get("knob"), "current": p.get("current"),
                 "suggested": p.get("suggested"),
                 "followup": p.get("followup") or "pending"}
                for p in applied[:5]
            ],
            "stats": stats,
            "insights": [
                {"kind": i.get("kind"), "label": i.get("label"),
                 "mood": i.get("mood"), "detail": i.get("detail")}
                for i in insights
            ],
            "note": ("The bot proposes, you decide — "
                     "/tune to apply or dismiss."),
        }
    except Exception:
        return {"event": "tuning.weekly_review", "week": "",
                "new_proposals": [], "new_proposals_total": 0,
                "open_older": 0, "applied": [], "stats": _stats([]),
                "insights": [],
                "note": "Review unavailable — see /tune."}


def maybe_weekly_review(config: Any, state: Dict[str, Any],
                        now: float) -> Optional[Dict[str, Any]]:
    """Publish the weekly review card when its slot is due.

    The slot is marked sent ONLY after the publish succeeds — a failure
    leaves it unmarked so the next cycle retries.
    """
    try:
        degen = config.degen
        if not bool(getattr(degen, "review_enabled", True)):
            return None
        slot = _review_slot(degen, now)
        if not slot or state.get(_REVIEW_STATE_KEY) == slot:
            return None
        event = build_weekly_review(degen, now)
        from agent import events as event_bus  # noqa: PLC0415
        event_bus.publish(event)
        state[_REVIEW_STATE_KEY] = slot
        logger.warning("WEEKLY REVIEW [%s] %d new proposals, %d applied",
                       slot, event["new_proposals_total"],
                       len(event["applied"]))
        return event
    except Exception:
        logger.exception("weekly review failed")
        return None
