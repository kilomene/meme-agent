"""launch_scanner — degen launch radar daemon.

Real-time new-coin detection across:
  * pump.fun newest launches (Solana)
  * DexScreener top boosts (solana / base / ethereum / robinhood)
  * Hyperliquid new spot listings

Qualification tiers: early (fresh + traction), velocity (mcap exploding),
graduated (pump.fun -> PumpSwap), boost (DexScreener-paid visibility),
listing (new CEX spot listing).

TWO-TIER PIPELINE:

  1. EARLY DETECT (``launch.early``) — the CA goes out to Telegram the
     moment a launch qualifies as a candidate, explicitly marked
     UNCONFIRMED. Speed with honesty: no confirmation evidence, no
     safety verdict, no paper entry at this stage.
  2. CONFIRMED (``launch.detected``) — daemon/launch_scanner/runner.py
     tracks each candidate across polls and only confirms when sustained
     multi-poll evidence proves real demand: mcap up >= gain_mult from
     first sighting, price rising in N consecutive polls, real 5-minute
     volume, adequate liquidity, and buy-side dominance. Everything else
     is dropped silently — the CONFIRMED card is the only one that can
     trigger a paper entry.

  LOW-CAP HUNTER (``launch.lowcap_runner``) — daemon/launch_scanner/lowcap.py
  watches coins at/below ``lowcap_mcap_ceiling_usd`` and drops the CA the
  moment the run from the sub-ceiling baseline lands inside
  [lowcap_min_run_pct, lowcap_max_run_pct] (default 70–100%) with the
  fast quality gate passed: catch the runner at low mcap, pre-viral.
  UNCONFIRMED and alert-only — never a paper entry.

On confirmation the daemon ALWAYS emits ``launch.detected`` (the Telegram
card carries the CA plus the confirmation evidence). For Solana coins with
``degen.auto_enter`` it also places a paper BUY through the memecoin paper
adapter — fixed size, idempotent per mint, kill-switch respected, max
concurrent positions capped. Base/ETH/Hyperliquid detections are
alert-only: the paper engine prices Solana only.

Degen exits are managed here (SL/TP touch, max hold) because new coins
have no H1 candles for the ATR-based exit manager.

MILESTONE TRACKING (``launch.milestone``) — daemon/launch_scanner/
milestones.py registers every carded coin at its card-time mcap and, on
a ~1-minute poll, emits a 🏆 card the first time it crosses 2x / 5x /
10x / 25x / 50x / 100x / 250x / 500x / 1000x from OUR card — then keeps
doubling past that, so however many x's the coin does, the run keeps
getting celebrated. Multiples are anchored to the card, not to the
bottom tick. Measurement only — never trades, never gates.

SLOW-COOKER LATE RUNNERS (``launch.late_runner``) — daemon/
launch_scanner/slowcooker.py reuses the milestone watch records: a
quality-passed coin (instant card OR digest-queued) that sits quiet
past the 45-min initial confirmation window and then 2x's from its
card-time anchor within 3h gets an ACTIONABLE card — full safety
re-gate (fail-closed) + paper position labeled ``late_runner`` with
the standard exit plan. Exactly one 2x card per coin: the slow cooker
commits before milestones.poll evaluates, marking the record so the
measurement-only 2x celebration stays silent (5x/10x+ still fire).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from daemon.common import (
    Daemon,
    get_home,
    get_store,
    get_daemon_state,
    load_config,
    main,
    save_daemon_state,
)

logger = logging.getLogger("forex_agent.daemon.launch_scanner")

from daemon.launch_scanner import backtest, bundle, deployers, earlywarn, feedback, fingerprints, insiders, latency, logstream, lowcap, milestones, momentum, narratives, recap, runner, safety, scoreboard, secondwave, sentiment, shadow, slowcooker, smartgrade, smartmoney, sniper, sources  # noqa: E402

# Once-per-day scheduled-card flags. Canonical definitions live in their
# modules (toppicks.SLOT_KEY, recap._STATE_KEY, feedback._REVIEW_STATE_KEY);
# the literals are repeated here so run_once can snapshot them without
# importing toppicks at module load (it is imported lazily below).
_DAILY_SLOT_KEYS = ("toppicks_last_slot", "recap_last_slot",
                    "review_last_slot")


def _testing() -> bool:
    """True when running under the unit-test suite.

    The backtest dataset recorders skip writes aimed at the LIVE dataset
    path in this mode, so tests never pollute the user's real data.
    Tests that deliberately exercise the writers pass an explicit tmp
    path — explicit paths always write, even under test.
    """
    return os.environ.get("FOREX_AGENT_TESTING") == "1"


def _live_dataset_path() -> str:
    return os.path.abspath(os.path.expanduser(
        "~/.forex-agent/launch_scanner/backtest_dataset.jsonl"))


def _skip_live_write(path: str) -> bool:
    """Whether a recorder write must be skipped (test -> live path)."""
    try:
        return _testing() and os.path.abspath(path) == _live_dataset_path()
    except Exception:
        return False


#: Suffix marking the runner leg of a two-tier paper position.
LEG_B_SUFFIX = "|B"


def count_base_positions(positions: Optional[Dict[str, Any]]) -> int:
    """Number of distinct paper positions, counting a two-tier A+B leg
    pair as ONE position (both legs share the same capital as the old
    single position, so exposure limits must not double-count)."""
    if not positions:
        return 0
    seen = set()
    for key in positions:
        base = key[:-len(LEG_B_SUFFIX)] if key.endswith(LEG_B_SUFFIX) \
            else key
        seen.add(base)
    return len(seen)


def _helius_rpc_url() -> Optional[str]:
    """Authenticated Helius RPC, preferred when the key file exists.

    The key lives at ``$FOREX_AGENT_HOME/helius/api_key`` (0600) — the
    same pattern as the Telegram bot token. Never logged: the failover
    logging redacts query strings.
    """
    try:
        key = (Path(get_home()) / "helius" / "api_key").read_text().strip()
    except Exception:
        return None
    if not key:
        return None
    return "https://mainnet.helius-rpc.com/?api-key=" + key


# Exit-policy version, recorded on every paper close. Bumped to "v2"
# when the Leg A profit-lock trail shipped (2026-10-02, user-approved):
# v1 closes ran the single-stage trail (arm at +50%, 30% trailback);
# v2 adds the Leg A early stage (arm at +20%, 15% trailback) so a
# fading runner banks its profit instead of round-tripping to the stop.
# The exit measurement compares v1 vs v2 honestly instead of mixing
# regimes. v1 = closes before 2026-10-02 ~23:45 WAT.
EXIT_POLICY_VERSION = "v2"


# Relative epsilon for threshold comparisons: 1.2/1.0 - 1.0 is
# 0.19999999999999996 in floating point, so an exact +20% peak would
# otherwise miss an exact +20% trigger by one ulp.
_EPS = 1e-9


def _gte(a: float, b: float) -> bool:
    return a >= b * (1.0 - _EPS)


def _lte(a: float, b: float) -> bool:
    return a <= b * (1.0 + _EPS)


def trail_exit_reason(degen: Any, leg: Optional[str],
                      entry_px: float, peak: float,
                      price: float) -> Optional[str]:
    """Two-stage trailing stop. Returns "trail_stop", "profit_lock",
    or None. Never raises.

    Leg A (the banker):
      stage 1 — peak up >= leg_a_trail_trigger_pct (default +20%):
                 exit on leg_a_trail_drawdown_pct (default 15%)
                 pullback from the peak. Peak +20% exits at ~+2%,
                 peak +40% at ~+19%: profit is banked, never donated.
      stage 2 — peak up >= trail_trigger_pct (default +50%): the
                 pre-existing 30% trailback, unchanged.
    Leg B (the moon bag): the pre-existing single stage only
      (arm +50%, 30% trailback) — it keeps full room to run.
    Positions without a leg tag are treated as Leg A.
    """
    try:
        entry_px = float(entry_px or 0)
        peak = float(peak or 0)
        price = float(price or 0)
        if entry_px <= 0 or peak <= 0 or price <= 0:
            return None
        gain = peak / entry_px - 1.0
        if gain < 0:
            return None
        leg_u = str(leg or "A").upper()
        t2 = float(getattr(degen, "trail_trigger_pct", 50.0) or 50.0)
        d2 = float(getattr(degen, "trail_drawdown_pct", 30.0) or 30.0)
        if leg_u != "B":
            t1 = float(getattr(degen, "leg_a_trail_trigger_pct", 20.0)
                       or 20.0)
            d1 = float(getattr(degen, "leg_a_trail_drawdown_pct", 15.0)
                       or 15.0)
            # Stage 2 first: once the runner is big, the looser
            # pre-existing trail governs (unchanged behavior).
            if _gte(gain, t2 / 100.0):
                if _lte(price, peak * (1.0 - d2 / 100.0)):
                    return "trail_stop"
            elif _gte(gain, t1 / 100.0):
                if _lte(price, peak * (1.0 - d1 / 100.0)):
                    return "profit_lock"
            return None
        if _gte(gain, t2 / 100.0) and _lte(
                price, peak * (1.0 - d2 / 100.0)):
            return "trail_stop"
        return None
    except Exception:
        return None


class LaunchScanner(Daemon):
    name = "launch_scanner"
    interval = 30.0

    def __init__(self, interval: Optional[float] = None):
        super().__init__(interval)

    # -- main loop ------------------------------------------------------
    def run_once(self) -> None:
        from agent import events as event_bus  # noqa: PLC0415

        config = load_config()
        degen = getattr(config, "degen", None)
        if degen is None or not getattr(degen, "enabled", True):
            return
        try:
            self.interval = float(
                getattr(degen, "poll_interval_seconds", 30) or 30)
        except (TypeError, ValueError):
            pass

        store = get_store()
        event_bus.configure(store=store)
        state = get_daemon_state(store, self.name) or {}
        state.setdefault("seen", {})
        state.setdefault("degen_positions", {})
        state.setdefault("hl_tokens", [])
        state.setdefault("cycle", 0)
        # Once-per-day card flags whose in-memory change must persist
        # immediately (see the write-through in the finally block below).
        # Canonical names: toppicks.SLOT_KEY, recap._STATE_KEY,
        # feedback._REVIEW_STATE_KEY.
        _slots_before = {k: state.get(k) for k in _DAILY_SLOT_KEYS}
        # Position open/close must persist immediately, like the
        # daily-slot flags (see the write-through in the finally block
        # below). run_once() reloads state from disk every cycle while
        # the full save is throttled to state_save_interval_seconds
        # (300s): a degen_positions key added or removed in a non-save
        # cycle was discarded on the next reload — the broker ticket
        # survived in paper_state.json, so the position could never
        # close (orphan), and closes popped in-memory were retried every
        # cycle forever (zombie). The measurement ledgers ride along so
        # the Oct-9 verdict is built on complete data, not survivors.
        _pos_before = set((state.get("degen_positions") or {}).keys())
        _ledger_before = {
            k: set((state.get(k) or {}).keys())
            for k in ("proof_checks", "footprint_signals")
        }

        try:
            self._ensure_logstream(config)
        except Exception:
            logger.exception("launch_scanner: logstream start failed")
        try:
            adapter = self._fresh_adapter()
        except Exception:
            logger.exception("launch_scanner: could not build adapter")
            return
        try:
            self._manage_exits(config, adapter, state)
            self._retry_pending(config, adapter, state)
            try:
                # Smart-money wallet poll + fast-track (#4a): poll_wallets
                # returns {mint: [wallet_addresses]} of fresh buys.
                base_poll = float(getattr(
                    config.degen, "smart_poll_seconds", 120.0) or 120.0)
                try:
                    n_wallets = len(smartmoney.load_registry() or {})
                except Exception:
                    n_wallets = 0
                # Helius credit guardrail: signature-fetch calls scale with
                # the wallet count (2 wallets @60s ≈ 117–129k credits/mo ≈
                # 13% of the 1M free tier; 10 wallets @60s ≈ 487–547k —
                # unsafe). Effective poll never goes faster than the
                # configured cadence and degrades 12s per tracked wallet,
                # so adding wallets slows the cadence instead of blowing
                # the credit budget (>5 wallets -> >60s effective).
                poll_seconds = max(base_poll, float(n_wallets) * 12.0)
                fresh = smartmoney.poll_wallets(state,
                                                poll_seconds=poll_seconds)
                if fresh:
                    self._smart_fasttrack(config, state, fresh)
            except Exception:
                logger.warning("smart-money poll failed", exc_info=True)
            try:
                self._drain_logstream(config, adapter, state)
            except Exception:
                logger.exception("logstream drain failed")
            self._scan(config, adapter, state)
            try:
                shadow.update(state, config.degen)
            except Exception:
                logger.exception("shadow ledger update failed")
            try:
                insiders.update(state, config.degen)
            except Exception:
                logger.exception("insider tracker update failed")
            if int(state.get("cycle", 0)) % 240 == 1:  # ~hourly at 15s
                self._log_scoreboard(store)
        except Exception:
            logger.exception("launch_scanner cycle failed")
        finally:
            now = time.time()
            try:
                self._prune_seen(state, degen, now)
            except Exception:
                logger.exception("launch_scanner: seen prune failed")
            try:
                interval = float(getattr(degen, "state_save_interval_seconds",
                                         300.0) or 300.0)
            except (TypeError, ValueError):
                interval = 300.0
            last_save = state.get("_state_saved_ts") or 0.0
            try:
                last_save = float(last_save)
            except (TypeError, ValueError):
                last_save = 0.0
            # Throttle full state dumps: the blob carries the (pruned) seen
            # map, so writing it every cycle is pure write amplification.
            if now - last_save >= interval:
                state["_state_saved_ts"] = now
                save_daemon_state(store, self.name, state)
            # Daily-slot write-through (2026-10-05 fix): the once-per-day
            # card flags in _DAILY_SLOT_KEYS are set in the in-memory state
            # during the cycle, but run_once() reloads state from disk at
            # the top of every cycle (~15s) while the save above is
            # throttled to every state_save_interval_seconds (300s
            # default). Without this write-through the next cycle sees the
            # stale on-disk state and re-fires the card — on 2026-10-05
            # that meant 12 copies each of the 08:00 WAT runners recap,
            # weekly review and Top Picks. The same staleness orphaned
            # paper positions (entry written in-memory, lost on reload,
            # broker ticket immortal) and resurrected closed ones every
            # cycle (zombie close retries) — so degen_positions key
            # changes and measurement-ledger additions get the same
            # immediate save. Peak-price-only mutations still wait for
            # the throttled save; only open/close membership is urgent.
            try:
                _slots_after = {k: state.get(k) for k in _DAILY_SLOT_KEYS}
                _pos_after = set((state.get("degen_positions") or {}).keys())
                _ledger_after = {
                    k: set((state.get(k) or {}).keys())
                    for k in ("proof_checks", "footprint_signals")
                }
            except Exception:
                _slots_after, _pos_after, _ledger_after = {}, set(), {}
            if (_slots_after != _slots_before
                    or _pos_after != _pos_before
                    or _ledger_after != _ledger_before):
                state["_state_saved_ts"] = now
                try:
                    save_daemon_state(store, self.name, state)
                except Exception:
                    logger.exception(
                        "launch_scanner: write-through save failed")

    @staticmethod
    def _prune_seen(state: Dict[str, Any], degen, now: float) -> int:
        """Drop stale dedup entries from ``state["seen"]``.

        Returns the number of entries removed. Entries idle longer than
        ``degen.seen_retention_hours`` are evicted (last activity = newest
        of ``first_ts`` and any mcap sample); ``degen.seen_max_entries``
        is a hard backstop keeping the most recently active entries.
        """
        seen = state.get("seen")
        if not isinstance(seen, dict) or not seen:
            return 0
        try:
            retention = float(getattr(degen, "seen_retention_hours", 48.0)
                              or 48.0)
        except (TypeError, ValueError):
            retention = 48.0
        try:
            max_entries = int(getattr(degen, "seen_max_entries", 100000)
                              or 100000)
        except (TypeError, ValueError):
            max_entries = 100000
        cutoff = now - retention * 3600.0

        def _last_active(entry) -> float:
            if not isinstance(entry, dict):
                return 0.0
            try:
                last = float(entry.get("first_ts") or 0.0)
            except (TypeError, ValueError):
                last = 0.0
            mcap = entry.get("mcap")
            if isinstance(mcap, (list, tuple)):
                for point in mcap:
                    try:
                        ts = float(point[0])
                    except (TypeError, ValueError, IndexError):
                        continue
                    if ts > last:
                        last = ts
            return last

        removed = 0
        for key in list(seen.keys()):
            entry = seen[key]
            last = _last_active(entry)
            if last <= 0 and isinstance(entry, dict):
                # No timestamp at all (bare alerted marker): stamp once so
                # it gets a full retention window, not instant eviction.
                entry["first_ts"] = now
                continue
            if last < cutoff:
                del seen[key]
                removed += 1
        if len(seen) > max_entries:
            ranked = sorted(seen.items(),
                            key=lambda kv: _last_active(kv[1]))
            for key, _ in ranked[:len(seen) - max_entries]:
                del seen[key]
                removed += 1
        if removed:
            logger.warning("launch_scanner: pruned %d stale seen entries "
                           "(%d remain)", removed, len(seen))
        return removed

    @staticmethod
    def _digest_has(state: Dict[str, Any], mint: str) -> bool:
        """True when the spam-guard digest queue already holds this mint.

        The smart-buy fast-track queues WITHOUT setting early_alerted
        (so the coin stays eligible for a full instant card later) — this
        guard keeps the two queue sites from stacking duplicate items for
        the same mint.
        """
        if not mint:
            return False
        return any(isinstance(d, dict) and d.get("mint") == mint
                   for d in (state.get("early_digest") or []))

    def _smart_fasttrack(self, config, state: Dict[str, Any],
                         fresh: Dict[str, List[str]]) -> None:
        """Smart-buy fast-track (#4a): a tracked wallet's FIRST buy of a
        brand-new mint skips the rising-poll wait — the instant safety
        gate (``_instant_verdict``) runs immediately, and on pass the
        trigger is ranked by the sniper tier: tier-1 stages a
        ``launch.early`` card for the end-of-cycle flush
        (highest-rank-first through the 4/hr instant cap, exactly like a
        normal instant card — never published directly). Sub-tier joins
        the SAME digest queue as normal instant cards (never dropped
        silently); ``early_alerted`` is NOT set on that path — the digest
        flush handles first-card latency and the tiered card.

        Cross-dedup: a mint evaluated here (pass OR hold) is recorded in
        ``state["smart_fasttracked"]`` by ``evaluate_fasttrack`` and
        never evaluated twice; a published card sets
        ``seen[mint]["early_alerted"]`` so the normal pipeline never
        re-cards it. The deployer trigger (``_deployer_card_line``)
        skips any mint in ``smart_fasttracked`` — one mint, one
        fast-track card, both orderings defended.
        """
        degen = config.degen
        now = time.time()
        # Wallet grading (#4b): ledger every fresh buy (deduped per
        # wallet+mint). Outcomes join via ``smartgrade.reconcile`` every
        # ~10 scan cycles (see _scan) — no explicit outcome hook needed.
        try:
            smartgrade.record_wallet_buys(fresh, degen)
        except Exception:
            logger.warning("wallet-grade record failed", exc_info=True)
        if not getattr(degen, "smart_fasttrack_enabled", True):
            return
        for mint, addrs in (fresh or {}).items():
            if not mint:
                continue
            labels = smartmoney.wallet_labels_for(state, mint) or [
                str(a)[:8] + "…" for a in (addrs or []) if a]
            if not labels:
                continue
            trig = smartmoney.evaluate_fasttrack(
                state, degen, mint, labels,
                is_new=lambda m: smartmoney.mint_is_new(state, m),
                verdict_fn=LaunchScanner._instant_verdict)
            if not trig:
                continue
            sm_line = ("⚡ fast-track: %s bought first seen"
                       % ", ".join(labels))
            # Sniper tier: a smart buy materially boosts the rank but
            # does NOT bypass the threshold — the computed score decides
            # the lane. Tier-1 stages for the end-of-cycle flush
            # (highest-rank-first through the standing 4/hour cap);
            # sub-tier joins the digest queue, cap-independent, keeping
            # the fast-track markers.
            entry = state["seen"].get(mint) or {}
            fc = {"mint": mint, "symbol": "", "name": "",
                  "chain": "solana", "source": "smart-fasttrack",
                  "tier": "t1", "url": ""}
            res = self._sniper_rank(config, state, fc, entry, {})
            reason_ft = trig.get("reason") or "smart-buy fast-track"
            report_ft = {"score": trig.get("safety_score")}
            extra = {"fasttrack": True, "smart_money": sm_line,
                     "smart_wallets": labels,
                     "safety_score": trig.get("safety_score")}
            if res["tier"] != "sniper":
                # Sub-tier smart buy: digest lane. NO early_alerted: the
                # digest flush handles first-card latency and the tiered
                # card, and the coin stays eligible for a full instant
                # card when the cap has room again.
                logger.warning(
                    "FASTTRACK STANDARD (sniper %.1f < %.1f) %s -> "
                    "digest queue", res["score"], res["threshold"], mint)
                self._enqueue_early_digest(
                    config, state, degen, fc, {}, 0.0, "t1", reason_ft,
                    report_ft, res, now, extra=extra)
                continue
            # Tier-1: create the seen entry (no KeyError, no re-card)
            # and set early_alerted NOW — the normal pipeline must never
            # re-card this mint (cross-dedup contract). The sub-tier
            # digest path above deliberately creates NO seen entry, so
            # the coin stays eligible for a full instant card later.
            seen_entry = state["seen"].setdefault(mint, {})
            seen_entry["first_ts"] = entry.get("first_ts", now)
            seen_entry.setdefault("alerted", False)
            seen_entry.setdefault("symbol", "")
            seen_entry.setdefault("chain", "solana")
            seen_entry.setdefault("source", "smart-fasttrack")
            seen_entry.setdefault("mcap", [])
            seen_entry["early_alerted"] = True
            self._sniper_stage(config, state, degen, fc, {}, seen_entry,
                               0.0, "t1", reason_ft, report_ft, res, now,
                               extra=extra)
            logger.warning("FASTTRACK STAGED %s bought by %s score=%.1f",
                           mint, ", ".join(labels), res["score"])

    def _deployer_card_line(self, config, state: Dict[str, Any],
                            mint: str, chain: str, c: Dict[str, Any],
                            allow: bool) -> Optional[Dict[str, Any]]:
        """Deployer tracking (#2): lazy, cached resolution + hot-deployer
        fast-track payload for the card line.

        Call ONLY for quality-passed coins (``_instant_verdict`` returned
        ``allow=True``), off the hot per-candidate scan loop. Resolves
        the deployer via ``deployers.cached_deployer`` (pump.fun creators
        resolve free from the source dict; pool launches cost 1–2 Helius
        calls, cached 6h) and records the launch — then, for a hot
        deployer on a new-to-us mint, returns the fast-track payload
        (``deployer_line`` = "🏭 deployer 4/10 hit 2x"). The safety
        verdict ran FIRST and is passed in as ``safety_passed`` — history
        never overrides hard fails.

        Cross-dedup: returns None when the smart-buy fast-track already
        evaluated this mint (``smart_fasttracked``). The
        ``early_alerted`` half of the dedup is enforced structurally —
        the only call sites sit inside ``if not entry.get("early_alerted")``
        gates, so a previously carded mint never reaches this trigger.
        """
        degen = config.degen
        try:
            if not getattr(degen, "deployer_tracking_enabled", True):
                return None
            if not mint or not allow:
                return None
            deployer = deployers.cached_deployer(
                state, mint, chain or "", c, degen)
            if deployer:
                try:
                    deployers.record_launch(deployer)
                except Exception:
                    logger.warning("deployer record_launch failed",
                                   exc_info=True)
            if mint in (state.get("smart_fasttracked") or {}):
                # Smart-buy fast-track already evaluated this mint (pass
                # or hold) — never a second fast-track card.
                return None
            return deployers.evaluate_deployer_fasttrack(
                state, degen, mint, deployer, safety_passed=allow)
        except Exception:
            logger.warning("deployer card line failed for %s", mint,
                           exc_info=True)
            return None

    @staticmethod
    def _log_scoreboard(store) -> None:
        try:
            db_path = str(getattr(store, "path", ""))
            closes = scoreboard.load_degen_closes(db_path)
            summary = scoreboard.summarize(closes)
            logger.warning("SCOREBOARD %s", scoreboard.format_card(summary)
                           .replace("\n", " | "))
        except Exception:
            logger.exception("scoreboard failed")

    @staticmethod
    def _fresh_adapter():
        # Fresh (uncached) adapter so newly added watchlist mints are
        # priced immediately — the cached adapter keeps its construction-
        # time symbol registry.
        from agent.tools import backend  # noqa: PLC0415
        return backend._build_adapter()

    # -- sub-second logstream detection ---------------------------------
    def _ensure_logstream(self, config) -> None:
        """Start the program-log subscriber thread once per daemon life."""
        if not getattr(config.degen, "logstream_enabled", True):
            return
        ls = getattr(self, "_logstream", None)
        if ls is None:
            urls = list(getattr(config.degen, "logstream_rpc_urls", None)
                        or logstream.HTTP_RPC_URLS)
            helius = _helius_rpc_url()
            if helius and helius not in urls:
                urls.insert(0, helius)  # authenticated endpoint wins
            ls = logstream.LogStream(http_rpcs=urls)
            self._logstream = ls
            ls.start()
            logger.warning("logstream: detection thread started")

    def _drain_logstream(self, config, adapter,
                         state: Dict[str, Any]) -> None:
        """Feed sub-second detections into the normal candidate pipeline."""
        ls = getattr(self, "_logstream", None)
        if ls is None:
            return
        for item in ls.drain():
            mint = item.get("mint")
            if not mint:
                continue
            c = {
                "mint": mint,
                "symbol": "", "name": "",
                "chain": "solana", "source": "pumpfun-log",
                "created_ts": item.get("ts") or time.time(),
                "usd_market_cap": 0.0,
                "extra": {"complete": False, "banned": False,
                          "signature": item.get("signature")},
                "url": "https://pump.fun/coin/%s" % mint,
            }
            try:
                self._consider(config, adapter, state, c)
            except Exception:
                logger.exception("logstream consider failed for %s", mint)

    # -- scanning -------------------------------------------------------
    def _scan(self, config, adapter, state: Dict[str, Any]) -> None:
        state["cycle"] = int(state.get("cycle", 0)) + 1
        cands = sources.pumpfun_new() + sources.dexscreener_boosts()
        if state["cycle"] % 20 == 1:  # ~every 5 min at 15s cadence
            cands += sources.hyperliquid_listings()
        if (state["cycle"] % 4 == 1  # ~every 1 min at 15s cadence
                and getattr(config.degen, "usdc_pools_enabled", True)):
            # USDC-quoted launches: Raydium/Meteora/Orca (Solana),
            # Aerodrome/Uniswap (Base), Uniswap (Ethereum). Same gates
            # and score bar as every other source.
            cands += sources.geckoterminal_usdc_pools()
        # Hyperliquid baseline guard: the listings feed returns the FULL
        # spot book, so an empty baseline would misread every existing
        # market as a brand-new listing (alert storm). Seed the whole
        # current book silently before any candidate is considered.
        hl_syms = {c.get("symbol") for c in cands
                   if c.get("source") == "hyperliquid-listing"
                   and c.get("symbol")}
        known = state_hl(state)
        if hl_syms and not known:
            for sym in sorted(hl_syms):
                known.append(sym)
            state["hl_seeded"] = True
            logger.warning("hyperliquid baseline seeded silently (%d "
                           "markets) — no alerts for the existing book",
                           len(known))
        for c in cands:
            try:
                self._consider(config, adapter, state, c)
            except Exception:
                logger.exception("consider failed for %s", c.get("mint"))
        try:
            # Sniper tier: emit staged tier-1 cards highest-rank-first
            # through the standing instant cap; cap-tripped cards join
            # the digest queue, never dropped.
            self._flush_sniper_pending(config, state)
        except Exception:
            logger.exception("sniper flush failed")
        try:
            self._momentum_scan(config, adapter, state)
        except Exception:
            logger.exception("momentum scan failed")
        # Hyperliquid baseline: after the first listing scan has run,
        # future unseen symbols are genuinely new listings.
        if not state.get("hl_seeded"):
            state["hl_seeded"] = True
        try:
            self._judge(config, adapter, state)
        except Exception:
            logger.exception("judge failed")
        try:
            self._flush_early_digest(config, state)
        except Exception:
            logger.exception("early digest flush failed")
        try:
            # Runners recap (user decision 2026-09-28): twice-daily card
            # with only the coins that actually ran. Informational only.
            recap.maybe_send(config, state, time.time())
        except Exception:
            logger.exception("runners recap failed")
        try:
            # Feedback loop (idea #6, user decision 2026-09-28): once per
            # day, analyze real paper outcomes and store tuning
            # PROPOSALS. Nothing is applied here — the user's explicit
            # /tune apply is the only path to a config change. A card
            # goes out only when there is something new to decide on.
            from agent import events as event_bus  # noqa: PLC0415
            # Second-wave measurement (idea #3, user decision
            # 2026-10-01): settle waves older than 24h against
            # milestone peaks BEFORE the learner runs, so today's
            # analysis sees the freshest settled evidence. The
            # learner only PROPOSES trigger tweaks after >=10
            # settled waves prove an edge.
            try:
                if getattr(config.degen, "wavefront_enabled", True):
                    from daemon.launch_scanner import wavefront  # noqa: PLC0415
                    n = wavefront.settle(
                        state, milestones.tracked(state) or {},
                        time.time())
                    if n:
                        logger.warning("WAVEFRONT settled %d waves", n)
            except Exception:
                logger.exception("wavefront settle failed")
            new_props = feedback.maybe_daily_scan(config, state,
                                                  time.time())
            if new_props:
                _insights = feedback.load_insights()[:3]
                event_bus.publish({
                    "event": "tuning.proposals",
                    "count": len(new_props),
                    "items": [
                        {"id": p.get("id"), "knob": p.get("knob"),
                         "current": p.get("current"),
                         "suggested": p.get("suggested"),
                         "rationale": p.get("rationale")}
                        for p in new_props
                    ],
                    "insights": [
                        {"kind": i.get("kind"), "label": i.get("label"),
                         "mood": i.get("mood"), "detail": i.get("detail")}
                        for i in _insights
                    ],
                    "note": ("Your approval needed — nothing changes "
                             "until you run /tune apply <id>."),
                })
        except Exception:
            logger.exception("feedback scan failed")
        try:
            # Smart-wallet auto-discovery (idea #2, user decision
            # 2026-10-01): once a day, mine early buyers of past 5x+
            # runners and store wallet proposals for review. Proposals
            # ONLY — /smartadd is the only path to the tracked
            # registry; nothing auto-adds. Throttled inside
            # run_discovery via state["wallet_discovery"].
            from daemon.launch_scanner import wallet_discovery  # noqa: PLC0415
            from agent import events as event_bus  # noqa: PLC0415
            disc_mult = float(
                getattr(config.degen, "discovery_runner_mult", 5.0) or 5.0)
            runners = []
            for m, r in (milestones.tracked(state) or {}).items():
                if not isinstance(r, dict):
                    continue
                peak = float(r.get("peak_mult") or 0)
                if peak < disc_mult:
                    continue
                runners.append({
                    "mint": m,
                    "symbol": r.get("symbol") or "",
                    "peak_mult": peak,
                    "first_seen_ts": float(r.get("card_ts") or 0),
                })
            disc = wallet_discovery.run_discovery(
                state, config.degen, runners, rpc=smartmoney._rpc)
            if disc.get("proposals"):
                event_bus.publish({
                    "event": "discovery.proposals",
                    "count": disc["proposals"],
                    "runners_mined": disc.get("runners_mined"),
                    "note": ("New smart-wallet candidates awaiting review — "
                             "see wallet_discovery_proposals.json; "
                             "/smartadd <wallet> to track. Nothing was "
                             "auto-added."),
                })
        except Exception:
            logger.exception("wallet discovery failed")
        try:
            # Weekly learner review (user decision 2026-09-28): one card
            # every Monday 08:00 WAT — new proposals, applied changes and
            # their grades, the week's win rate. Informational only;
            # /review shows the same card on demand.
            feedback.maybe_weekly_review(config, state, time.time())
        except Exception:
            logger.exception("weekly review failed")
        try:
            # Top Picks shortlist (user decision 2026-10-01): once a day
            # at toppicks_time (default 08:00 WAT), rank the last 24h of
            # confirmed runners by composite score and publish ONE card
            # with the top N — the shortlist to gamble from instead of
            # 200+ alerts. Ranking only: gates, bar, and caps untouched;
            # picks are alert-only, never auto-traded.
            from daemon.launch_scanner import toppicks  # noqa: PLC0415
            toppicks.maybe_daily_picks(
                config, state, time.time(),
                milestones=milestones.tracked(state))
        except Exception:
            logger.exception("top picks failed")
        try:
            # Wallet grading (#4b): join the buy ledger with milestone
            # outcomes every ~10 cycles — anchors fresh buys to card-time
            # mcaps and refreshes 2x hits / peak multiples. Cheap; the
            # /smartgrade command also reconciles on demand.
            if int(state.get("cycle", 0)) % 10 == 1:
                smartgrade.reconcile(state, config.degen)
        except Exception:
            logger.exception("wallet-grade reconcile failed")
        try:
            # --- slow-cooker late runners ------------------------------
            # Quality-passed coins (instant cards AND digest-queued ones)
            # that sat quiet past the 45-min initial confirmation window
            # and THEN 2x'd from their card-time anchor get an ACTIONABLE
            # card (safety re-gate + paper entry). This runs BEFORE
            # milestones.poll on purpose: a committed late runner marks
            # the milestone record so the measurement-only 2x celebration
            # stays silent — exactly one 2x card per coin. If the safety
            # re-check fails, nothing is marked and the milestone 2x
            # still fires as the consolation card.
            for leevt in slowcooker.poll(state, config.degen,
                                         time.time(),
                                         int(state.get("cycle", 0))):
                slowcooker.handle_late(self, config, adapter, state,
                                       leevt)
        except Exception:
            logger.exception("slow-cooker poll failed")
        try:
            # Milestone tracking: follow every carded coin; celebrate the
            # runs from OUR card. Throttled inside milestones.poll.
            from agent import events as event_bus  # noqa: PLC0415
            for mevt in milestones.poll(state, config.degen, time.time(),
                                        int(state.get("cycle", 0))):
                event_bus.publish(mevt)
        except Exception:
            logger.exception("milestone poll failed")
        try:
            # --- second-wave re-entry --------------------------------
            # Carded coins that pulled back from their peak multiple and
            # are re-accelerating get their own card + paper entry.
            # Throttled inside secondwave.poll; safety re-runs at
            # re-entry inside secondwave.handle_wave. Distinct 6h
            # per-coin cooldown, independent of the main alert lanes.
            for swevt in secondwave.poll(state, config.degen,
                                         time.time(),
                                         int(state.get("cycle", 0))):
                secondwave.handle_wave(self, config, adapter, state,
                                       swevt)
        except Exception:
            logger.exception("second-wave poll failed")
        try:
            # Sustained-velocity tier (idea #4, user decision
            # 2026-10-01): re-check confirmed runners seeded at alert
            # time. SUSTAINED earns one louder top-tier card;
            # FADING/DEAD stay silent (measurement only, never spam).
            self._sustained_poll(config, state, time.time())
        except Exception:
            logger.exception("sustained poll failed")

    # -- established-coin momentum scanner ------------------------------
    def _momentum_scan(self, config, adapter,
                       state: Dict[str, Any]) -> None:
        """Scan GeckoTerminal trending pools for established runners.

        This is the complement to launch hunting: it catches coins that
        are ALREADY running (the FWOG/NEET/ANTFUN-shaped moves the
        launch pipeline never sees because those coins are weeks old).

        Networks come from ``degen.momentum_networks`` (default
        solana + base + eth); every network's trending pools flow
        through the same qualifier and the same safety gate.

        Two-stage like launches, but compressed:
          1. First qualification -> momentum.alert card (no paper entry).
          2. Still qualifying on a LATER scan -> paper entry, so we
             never buy a one-snapshot top.

        The 6h cooldown suppresses duplicate alert CARDS only — the
        stage-2 confirmation pass re-evaluates tracked runners every
        scan with the cooldown bypassed, so entries can actually happen.

        Every stage passes the SAME instant quality gate as launches
        (known safety report, no hard fails, score >= early_min_score).
        Unknown / failing / low-score coins are held silently and never
        consume cooldown or paper. Never raises.
        """
        degen = config.degen
        if not getattr(degen, "momentum_enabled", True):
            return
        now = time.time()
        interval = float(getattr(degen, "momentum_scan_seconds", 300)
                         or 300)
        if now - float(state.get("momentum_last_scan") or 0) < interval:
            return
        state["momentum_last_scan"] = now
        networks = getattr(degen, "momentum_networks", None) or ["solana"]
        pools: List[Dict[str, Any]] = []
        for net in networks:
            net = str(net or "").strip().lower()
            if not net:
                continue
            try:
                pools.extend(sources.geckoterminal_trending_pools(net))
            except Exception:
                logger.warning("momentum scan: trending pools fetch failed"
                               " (%s)", net, exc_info=True)
        scanner = getattr(self, "_momentum_scanner", None)
        if scanner is None:
            scanner = momentum.MomentumScanner()
            self._momentum_scanner = scanner
        try:
            runners = scanner.scan(degen, pools, now)
        except Exception:
            logger.warning("momentum scan: qualify failed", exc_info=True)
            return
        from agent import events as event_bus  # noqa: PLC0415
        tracked = state.setdefault("momentum_seen", {})
        # Prune tracked runners unseen for 48h — same hygiene as the
        # fingerprint map; a runner that stopped running is forgotten.
        cutoff = now - 48 * 3600
        for m in [m for m, e in tracked.items()
                  if float((e or {}).get("last_ts") or 0) < cutoff]:
            del tracked[m]
        for r in runners or []:
            try:
                self._momentum_handle(config, adapter, state, tracked,
                                      scanner, r, now, event_bus)
            except Exception:
                logger.exception("momentum handle failed for %s",
                                 r.get("mint"))
        # Stage-2 confirmation pass: runners that alerted on an EARLIER
        # scan but haven't paper-entered are re-evaluated WITHOUT the
        # Telegram cooldown. The 6h cooldown suppresses duplicate CARDS
        # only — it must never block confirmation, or stage-2 entries
        # could never happen (the pool would be suppressed on every
        # later scan). Runs even when no fresh runners qualify. Runners
        # first seen on THIS scan are skipped: two-stage means never
        # buying a one-snapshot top.
        pools_by_mint: Dict[str, Dict[str, Any]] = {}
        for p in pools:
            m = p.get("mint")
            if m and m not in pools_by_mint:
                pools_by_mint[m] = p
        for mint, entry in list(tracked.items()):
            if not entry or entry.get("entered"):
                continue
            if now - float(entry.get("first_ts") or 0) < interval:
                continue  # alerted this scan — confirm on a LATER scan
            pool = pools_by_mint.get(mint)
            if pool is None:
                continue
            try:
                r2 = scanner.qualify(degen, pool, now, skip_cooldown=True)
            except Exception:
                continue
            if not r2:
                continue
            try:
                self._momentum_handle(config, adapter, state, tracked,
                                      scanner, r2, now, event_bus)
            except Exception:
                logger.exception("momentum confirm failed for %s", mint)

    @staticmethod
    @staticmethod
    def _early_warn_held(config, state: Dict[str, Any], door: str,
                         coin: Dict[str, Any],
                         report: Optional[Dict[str, Any]], mcap: float,
                         trigger: Optional[str],
                         extra: Optional[Dict[str, Any]] = None) -> None:
        """Shared early-warning-lane hook for a quality-held coin.

        `door` is "launch" or "momentum". `trigger` names why this held
        coin still deserves a warning (None = no warning). Starts insider
        tracking, then publishes one clearly-labeled UNCONFIRMED warning
        through earlywarn.maybe_warn — which enforces the score floor,
        the can't-exit exclusion, the per-coin cooldown and the hourly
        rate limit. Never raises; never trades; never touches the main
        alert stream or its cooldowns.
        """
        try:
            degen = getattr(config, "degen", None)
            if not trigger:
                return
            if not earlywarn.floor_ok(degen, report):
                return
            if earlywarn.cant_exit(report):
                logger.warning("WARN SKIP (can't exit) %s %s",
                               (coin or {}).get("symbol"),
                               (coin or {}).get("mint"))
                return
            mint = (coin or {}).get("mint") or ""
            chain = (coin or {}).get("chain") or ""
            insiders.watch(state, degen, mint, chain, report)
            payload_extra = dict(extra or {})
            line = insiders.trend_line(state, mint)
            if line:
                payload_extra["insider_line"] = line
            score = float((report or {}).get("score") or 0)
            earlywarn.maybe_warn(
                state, degen, door,
                (coin or {}).get("symbol") or "", mint, chain,
                mcap or 0.0, score,
                earlywarn.dangers(report), trigger, payload_extra)
        except Exception:
            logger.exception("early warning hook failed")

    @staticmethod
    def _record_skipped(state: Dict[str, Any], kind: str, symbol: str,
                        mint: str, chain: str, reason: str,
                        extra: Optional[Dict[str, Any]] = None) -> None:
        """Append a quality-held runner to the skipped-runners digest.

        `kind` is "momentum" or "launch". Repeats of the same coin merge
        into one record with a `hits` counter (a coin held on every scan
        must not flood the buffer). Bounded to the newest 200 distinct
        coins within a 7-day window, and persisted with daemon state so
        the /skipped command can show what the gates blocked and why.
        Never raises.
        """
        try:
            buf = state.setdefault("skipped_runners", [])
            now = time.time()
            rec: Dict[str, Any] = {
                "ts": now,
                "kind": kind,
                "symbol": symbol or "",
                "mint": mint or "",
                "chain": chain or "",
                "reason": str(reason or "")[:180],
                "hits": 1,
            }
            if extra:
                for k in ("chg_24h_pct", "usd_market_cap", "lane",
                          "tier", "safety_score"):
                    v = extra.get(k)
                    if v is not None:
                        rec[k] = v
            key = (kind, mint or symbol or "?")
            for i, old in enumerate(buf):
                if not isinstance(old, dict):
                    continue
                okey = (old.get("kind"),
                        old.get("mint") or old.get("symbol") or "?")
                if okey == key:
                    rec["hits"] = int(old.get("hits") or 1) + 1
                    rec["first_ts"] = old.get("first_ts") or old.get("ts")
                    buf[i] = rec
                    break
            else:
                rec["first_ts"] = now
                buf.append(rec)
            cutoff = now - 7 * 86400
            buf[:] = [r for r in buf
                      if isinstance(r, dict) and r.get("ts", 0) >= cutoff
                      ][-200:]
        except Exception:
            pass

    @staticmethod
    def _trade_plan(degen) -> Dict[str, Any]:
        """Standing trade plan attached to every signal card.

        Mirrors the degen paper engine's real exit knobs so the plan
        printed on a card is the plan the engine would enforce on entry.
        Read from config at publish time — never hardcoded twice.
        """
        try:
            return {
                "stop_pct": float(getattr(degen, "stop_loss_pct", 30.0) or 30.0),
                "tp_pct": float(getattr(degen, "take_profit_pct", 200.0) or 200.0),
                "max_hold_h": float(getattr(degen, "max_hold_hours", 4.0) or 4.0),
                "trail_trigger_pct": float(getattr(degen, "trail_trigger_pct", 50.0) or 50.0),
                "trail_drawdown_pct": float(getattr(degen, "trail_drawdown_pct", 30.0) or 30.0),
                # Leg A profit lock (v2 exits): bank profit early —
                # once the peak hits leg_a_trail_trigger_pct, a
                # leg_a_trail_drawdown_pct pullback exits ("profit_lock").
                "leg_a_trail_trigger_pct": float(
                    getattr(degen, "leg_a_trail_trigger_pct", 20.0) or 20.0),
                "leg_a_trail_drawdown_pct": float(
                    getattr(degen, "leg_a_trail_drawdown_pct", 15.0) or 15.0),
                # Two-tier legs: A = the exits above; B skips the fixed
                # TP and holds longer with the same stop/trail.
                "two_tier": bool(getattr(degen, "two_tier_enabled", True)),
                "tier_b_max_hold_h": float(
                    getattr(degen, "tier_b_max_hold_hours", 24.0) or 24.0),
            }
        except Exception:
            return {"stop_pct": 30.0, "tp_pct": 200.0, "max_hold_h": 4.0,
                    "trail_trigger_pct": 50.0, "trail_drawdown_pct": 30.0,
                    "leg_a_trail_trigger_pct": 20.0,
                    "leg_a_trail_drawdown_pct": 15.0,
                    "two_tier": True, "tier_b_max_hold_h": 24.0}

    def _momentum_handle(self, config, adapter, state,
                         tracked: Dict[str, Any],
                         scanner, r: Dict[str, Any], now: float,
                         event_bus) -> None:
        """Alert on first sighting; paper-enter on second consecutive
        qualification. Safety gate runs at BOTH stages."""
        degen = config.degen
        mint = r.get("mint")
        if not mint:
            return
        entry = tracked.get(mint)
        if entry is not None:
            entry["last_ts"] = now  # still running — don't prune
        # Intel layer: narrative tags + heat, sentiment (best effort).
        try:
            ntags = narratives.classify_narrative(
                r.get("symbol"), r.get("name"), "")
        except Exception:
            ntags = ["other"]
        try:
            heat = round(self._narr_heat_tracker().heat(ntags, now), 2)
        except Exception:
            heat = 1.0
        try:
            sc = self._sentiment_track(config, state, r, now)
        except Exception:
            sc = None
        # THE gate: known safety report, no hard fails,
        # score >= early_min_score. Unknown/failing/low coins are held
        # silently — they never alert, never enter, never burn cooldown.
        allow, reason, report = self._instant_verdict(degen, r)
        if not allow:
            logger.warning("MOMENTUM HELD (quality) %s %s: %s",
                           r.get("symbol"), mint, reason)
            self._record_skipped(
                state, "momentum", r.get("symbol"), mint,
                r.get("chain"), reason,
                {"chg_24h_pct": r.get("chg_24h_pct"),
                 "usd_market_cap": r.get("usd_market_cap"),
                 "lane": r.get("lane") or "established",
                 "safety_score": (report or {}).get("score")})
            # Shadow experiment: simulate what entering this blocked
            # runner would have done (measurement only, never trades).
            shadow.maybe_open(state, degen, "momentum", r.get("symbol"),
                              mint, r.get("chain"),
                              r.get("usd_market_cap"), reason)
            # Early-warning lane: this runner is held by the safety gate
            # but qualified on momentum — one UNCONFIRMED warning so the
            # user sees it start running instead of hearing about it late.
            self._early_warn_held(
                config, state, "momentum", r, report,
                r.get("usd_market_cap") or 0.0, "momentum_qualify",
                {"lane": r.get("lane") or "established",
                 "chg_24h_pct": r.get("chg_24h_pct"),
                 "url": r.get("url")})
            # Digest lane (user decision 2026-09-28): safety-unknown
            # momentum runners join the hourly digest UNVETTED — never
            # instant, never paper. Hard fails stay held silently.
            self._maybe_digest_unvetted(
                config, state, degen, r, entry,
                {"narratives": ntags, "narrative_heat": heat,
                 "sentiment": {"verdict": sc}},
                r.get("usd_market_cap") or 0.0,
                "momentum-%s" % (r.get("lane") or "established"),
                reason, report, now)
            return
        score = (report or {}).get("score")
        if entry is None:
            # Stage 1: first sighting -> alert card, no paper entry.
            if not self._early_allowed(degen, state, now):
                logger.warning("MOMENTUM HELD (spam window) %s %s",
                               r.get("symbol"), mint)
                return
            chg24 = r.get("chg_24h_pct") or 0
            chg1 = r.get("chg_1h_pct") or 0
            lane = r.get("lane") or "established"
            if lane == "lowcap":
                note = ("Lowcap runner +%.1f%%/24h (%+.1f%%/1h), "
                        "safety %s/100. UNCONFIRMED — watching for "
                        "paper entry." % (chg24, chg1, score))
            else:
                note = ("Established runner +%.1f%%/24h (%+.1f%%/1h), "
                        "safety %s/100. UNCONFIRMED — watching for "
                        "paper entry." % (chg24, chg1, score))
            evt = {
                "event": "momentum.alert",
                "confirmed": False,
                "plan": self._trade_plan(degen),
                "source": "gecko-trending",
                "lane": lane,
                "chain": r.get("chain") or "solana",
                "mint": mint,
                "symbol": r.get("symbol") or "",
                "name": r.get("name") or "",
                "url": r.get("url") or "",
                "chg_24h_pct": chg24,
                "chg_1h_pct": chg1,
                "vol_24h_usd": r.get("vol_24h_usd"),
                "liq_usd": r.get("liq_usd"),
                "usd_market_cap": r.get("usd_market_cap"),
                "safety_score": score,
                "narratives": ntags,
                "narrative_heat": heat,
                "sentiment": (sc or {}).get("verdict"),
                "note": note,
            }
            milestones.track_signal(state, degen, evt)
            # Entry timing (measurement only): momentum alerts fire on
            # first sighting, so first-seen == first-card -> ~0 latency.
            latency.record_first_seen(state, degen, mint, now,
                                      symbol=evt.get("symbol"))
            evt["latency_min"] = latency.record_first_card(
                state, degen, mint, now, symbol=evt.get("symbol"),
                source="momentum.alert")
            event_bus.publish(evt)
            scanner.mark_alerted(r.get("pool_address"), now)
            tracked[mint] = {"first_ts": now, "last_ts": now,
                             "entered": False,
                             "symbol": r.get("symbol") or ""}
            logger.warning("MOMENTUM ALERT [%s] %s %s +%.1f%%/24h safety=%s",
                           lane, r.get("symbol"), mint, chg24, score)
            return
        if entry.get("entered"):
            return
        # Stage 2: still running on a later scan -> paper entry.
        if not getattr(degen, "auto_enter", True):
            return
        if count_base_positions(state.get("degen_positions")) >= int(
                getattr(degen, "max_positions", 5) or 5):
            logger.warning("MOMENTUM HELD (max positions) %s %s",
                           r.get("symbol"), mint)
            return
        try:
            from daemon.telegram import watchlist  # noqa: PLC0415
            ok, msg = watchlist.add_to_watchlist(r.get("symbol"), mint)
            logger.warning("momentum watchlist %s: %s",
                           r.get("symbol"), msg)
        except Exception:
            logger.exception("momentum watchlist add failed for %s", mint)
        card = {
            "event": "momentum.alert",
            "tier": "momentum",
            "confirmed": True,
            "plan": self._trade_plan(degen),
            "source": "gecko-trending",
            "lane": r.get("lane") or "established",
            "chain": r.get("chain") or "solana",
            "mint": mint,
            "symbol": r.get("symbol") or "",
            "narratives": ntags,
            "narrative_heat": heat,
            "sentiment": (sc or {}).get("verdict"),
            "note": "",
        }
        entered = self._paper_enter(
            config, adapter, state, card, degen, mint,
            r.get("symbol") or "",
            intel={"sentiment": r.get("sentiment"),
                   "safety": {"score": score,
                              "fails": (report or {}).get("fails") or []}})
        if entered:
            entry["entered"] = True
            entry["entry_ts"] = now
            logger.warning("MOMENTUM ENTERED %s %s", r.get("symbol"),
                           mint)
        # A failed entry (no quote yet etc.) stays un-entered and retries
        # on the next qualifying scan — same as the launch pipeline.

    def _consider(self, config, adapter, state: Dict[str, Any],
                  c: Dict[str, Any]) -> None:
        degen = config.degen
        mint = c.get("mint")
        key = mint or "%s:%s" % (c.get("chain"), c.get("symbol"))
        now = time.time()
        seen = state["seen"]
        entry = seen.get(key)
        mcap = float(c.get("usd_market_cap") or 0)
        if entry is None:
            entry = {
                "first_ts": now,
                "mcap": [[now, mcap]],
                "alerted": False,
                "symbol": c.get("symbol") or "",
                "name": c.get("name") or "",
                "chain": c.get("chain") or "",
                "source": c.get("source") or "",
                "url": c.get("url") or "",
            }
            seen[key] = entry
            # Entry timing (measurement only): first observation of this
            # mint/pool — the latency baseline. Idempotent per mint.
            if mint:
                latency.record_first_seen(state, degen, mint, now,
                                          symbol=c.get("symbol"))
        else:
            hist = entry.setdefault("mcap", [])
            hist.append([now, mcap])
            cutoff = now - float(degen.velocity_window_minutes) * 60 - 120
            entry["mcap"] = [m for m in hist if m[0] >= cutoff][-40:]
            if c.get("symbol"):
                entry["symbol"] = c["symbol"]

        if entry["alerted"]:
            return
        # Already a live candidate? The judge owns it from here — skip
        # re-qualification. Re-qualifying every scan burns one DexScreener
        # HTTP call per boost candidate per scan (self-inflicted rate
        # limiting) and changes nothing: tier assignment only matters at
        # candidate creation.
        candidates = state.setdefault("candidates", {})
        if key in candidates:
            return
        tier = self._qualify(degen, c, entry, now, state)
        if not tier:
            return
        # Stage 1: register a SILENT candidate. The judge (stage 2) decides
        # whether this ever becomes an alerted runner.
        #
        # Tier 1 alert: the CA goes out IMMEDIATELY on detection, clearly
        # marked UNCONFIRMED — speed for the user, honesty about the risk.
        # Confirmation (tier 2) still requires the full multi-poll proof.
        # (key is not in candidates here — checked above before _qualify.)
        candidates[key] = runner.new_candidate(
            key, mint, c.get("symbol") or "", c.get("name") or "",
            c.get("chain") or "", c.get("source") or "", tier,
            c.get("url") or "", now)
        # AI narrative picker: classify once at candidate creation so
        # every card and the heat tracker carry the tags. Soft signal
        # only — never a gate, never a CA trigger.
        try:
            ntags = narratives.classify_narrative(
                c.get("symbol"), c.get("name"),
                (c.get("extra") or {}).get("description") or "")
        except Exception:
            ntags = ["other"]
        new_cand = candidates[key]
        new_cand["narratives"] = ntags
        if mint:
            state.setdefault("narrative_tags", {})[mint] = ntags
        try:
            new_cand["narrative_heat"] = round(
                self._narr_heat_tracker().heat(ntags, now), 2)
        except Exception:
            new_cand["narrative_heat"] = 1.0
        logger.warning("CANDIDATE [%s] %s %s", tier,
                       c.get("symbol"), mint)
        if not entry.get("early_alerted"):
            entry["early_alerted"] = True
            if tier == "listing" and not mint:
                # CEX listings carry no contract address — there is no
                # CA to drop instantly, so skip the tier-1 card. The
                # single confirmed listing card is the whole signal.
                logger.warning("EARLY SKIP (listing, no CA) %s",
                               c.get("symbol"))
            else:
                # Sniper tier ("monitor wide, alert narrow"): the safety
                # verdict runs FIRST and is unchanged — hard fails still
                # fail-closed, the 1/100 bar intact. Quality-passed
                # candidates are then ranked 0-100 by runner probability
                # (sniper.py): score >= threshold stages a tier-1
                # instant card, below threshold joins the digest queue.
                # Nothing is ever silently discarded.
                allow, reason, report = self._instant_verdict(degen, c)
                if not allow:
                    logger.warning(
                        "EARLY HELD (quality) %s %s tier=%s: %s",
                        c.get("symbol"), mint, tier, reason)
                    self._record_skipped(
                        state, "launch", c.get("symbol"), mint,
                        c.get("chain"), reason,
                        {"tier": tier, "usd_market_cap": mcap,
                         "safety_score": (report or {}).get("score")})
                    shadow.maybe_open(state, degen, "launch",
                                      c.get("symbol"), mint,
                                      c.get("chain"), mcap, reason)
                    # Early-warning lane: held by the gate — warn only if
                    # it's actually showing run signals (hot fingerprint
                    # or strong first-sight momentum).
                    fp_verdict = None
                    try:
                        _tr = getattr(self, "_fp_tracker", None)
                        _sc = _tr.score(mint) if _tr else None
                        fp_verdict = (_sc or {}).get("verdict")
                    except Exception:
                        fp_verdict = None
                    self._early_warn_held(
                        config, state, "launch", c, report, mcap,
                        earlywarn.launch_trigger(degen, c, fp_verdict),
                        {"tier": tier, "url": c.get("url")})
                    # Digest lane (user decision 2026-09-28): safety-unknown
                    # coins join the hourly digest UNVETTED — never instant,
                    # never paper. Hard fails stay held silently.
                    self._maybe_digest_unvetted(
                        config, state, degen, c, entry, new_cand, mcap,
                        tier, reason, report, now)
                else:
                    res = self._sniper_rank(config, state, c, entry,
                                            new_cand)
                    if res["tier"] == "sniper":
                        # Tier-1: stage for the end-of-cycle flush, which
                        # emits staged cards highest-rank-first through
                        # the standing 4/hour cap (cap-tripped cards join
                        # the digest queue — never dropped).
                        self._sniper_stage(config, state, degen, c,
                                           new_cand, entry, mcap, tier,
                                           reason, report, res, now)
                    else:
                        logger.warning(
                            "EARLY STANDARD (sniper %.1f < %.1f) %s %s "
                            "tier=%s -> digest queue",
                            res["score"], res["threshold"],
                            c.get("symbol"), mint, tier)
                        self._enqueue_early_digest(
                            config, state, degen, c, new_cand, mcap,
                            tier, reason, report, res, now)


    # -- instant-alert helpers ------------------------------------------
    @staticmethod
    def _safety_limits(degen) -> Dict[str, Any]:
        """Hard-gate tuning for the safety screen.

        RugCheck insider fields (Solana) and the EVM sell simulation
        read their limits from here. Missing attributes fall back to
        the strict defaults — a coin is never screened with a looser
        gate than intended.
        """
        def _num(name, default, cast):
            try:
                v = getattr(degen, name, default)
                return default if v is None else cast(v)
            except (TypeError, ValueError):
                return default

        return {
            "insider_cluster_max_pct":
                _num("insider_cluster_max_pct", 20.0, float),
            "graph_insiders_max":
                _num("graph_insiders_max", 5, int),
            "evm_sell_sim_enabled":
                bool(getattr(degen, "evm_sell_sim_enabled", True)),
            "evm_sell_sim_timeout_s":
                _num("evm_sell_sim_timeout_s", 3.0, float),
        }

    @staticmethod
    def _early_allowed(degen, state: Dict[str, Any], now: float) -> bool:
        """Spam guard: cap instant alerts per time window."""
        max_n = int(getattr(degen, "early_alert_max_per_window", 10) or 10)
        window = float(getattr(degen, "early_alert_window_seconds", 300)
                       or 300)
        w = state.get("early_window")
        if not w or now - float(w.get("start", 0)) >= window:
            state["early_window"] = {"start": now, "count": 1}
            return True
        if int(w.get("count", 0)) >= max_n:
            return False
        w["count"] = int(w.get("count", 0)) + 1
        return True

    # -- sniper tier: runner-probability ranking -----------------------
    def _sniper_rank(self, config, state: Dict[str, Any], c: Dict[str, Any],
                     entry: Dict[str, Any],
                     new_cand: Dict[str, Any]) -> Dict[str, Any]:
        """Rank one quality-passed candidate 0-100 (sniper.py).

        Never raises and never blocks: on any trouble it fails OPEN to
        the tier-1 lane (pre-sniper behavior) and logs — ranking is an
        optimization, not a gate. The safety verdict already ran.
        """
        degen = config.degen
        try:
            signals = sniper.collect_signals(self, state, degen,
                                             c.get("mint"), entry)
            # #5 narrative-heat boost: the candidate already carries its
            # heat multiplier (classified + heat-scored at candidate
            # build time). Feed it to the ranker as a soft signal.
            try:
                signals["narrative_heat"] = float(
                    new_cand.get("narrative_heat") or 1.0)
            except (TypeError, ValueError):
                signals["narrative_heat"] = 1.0
            return sniper.rank_candidate(degen, signals)
        except Exception:
            logger.exception("sniper rank failed for %s — instant lane",
                             c.get("mint"))
            return {"score": 100.0, "tier": "sniper",
                    "reasons": ["rank-error"], "threshold": 0.0,
                    "enabled": True}

    def _build_early_evt(self, config, state: Dict[str, Any], degen,
                         c: Dict[str, Any], new_cand: Dict[str, Any],
                         entry: Dict[str, Any], mcap: float, tier: str,
                         reason: str, report: Optional[Dict[str, Any]],
                         res: Dict[str, Any],
                         now: float,
                         extra: Optional[Dict[str, Any]] = None) -> tuple:
        """Build the tier-1 instant ``launch.early`` event (no publish).

        Publishing is deferred to ``_flush_sniper_pending`` so staged
        cards emit highest-rank-first. The deployer trigger is computed
        ONCE here and returned — the digest fallback must reuse it, never
        re-resolve (``record_launch`` is not idempotent).
        """
        mint = c.get("mint")
        note = ("Fast analysis passed (%s) — UNCONFIRMED, "
                "no confirmation yet. High risk." % reason)
        evt = {
            "event": "launch.early",
            "tier": tier,
            "confirmed": False,
            "plan": self._trade_plan(degen),
            "source": c.get("source"),
            "chain": c.get("chain") or "",
            "mint": mint,
            "symbol": c.get("symbol") or "",
            "name": c.get("name") or "",
            "url": c.get("url") or "",
            "usd_market_cap": mcap,
            "safety_score": ((report or {}).get("score")),
            # Sniper tier: the card shows its work — score + one-line
            # reason, so the user sees why it made the tier.
            "sniper": res.get("score"),
            "sniper_line": sniper.sniper_line(res),
            # Entry-signal snapshot for the #6 entry learner — which
            # signals picked this coin, recorded so closed trades can
            # be attributed back to them. Small dict of numbers.
            "sniper_signals": res.get("signals_snapshot"),
            # Intel fields live on the CANDIDATE (classified and
            # heat-scored above), not on the raw source object — read
            # them from new_cand so the card actually carries them.
            "narratives": new_cand.get("narratives") or ["other"],
            "narrative_heat": new_cand.get("narrative_heat") or 1.0,
            "sentiment": (new_cand.get("sentiment") or {}).get("verdict"),
            "age_minutes": round((now - entry["first_ts"]) / 60.0, 1),
            "note": note,
        }
        # Deployer fast-track (#2): hot-deployer line on the card. The
        # safety verdict (allow) ran FIRST above — history never
        # overrides hard fails. Cross-dedup: mints already evaluated by
        # the smart-buy fast-track get no second fast-track card.
        dtrig = self._deployer_card_line(
            config, state, mint, c.get("chain") or "", c, True)
        if dtrig:
            evt["deployer"] = dtrig["deployer"]
            evt["deployer_line"] = dtrig["deployer_line"]
        if extra:
            evt.update(extra)
        return evt, dtrig

    def _sniper_stage(self, config, state: Dict[str, Any], degen,
                      c: Dict[str, Any], new_cand: Dict[str, Any],
                      entry: Dict[str, Any], mcap: float, tier: str,
                      reason: str, report: Optional[Dict[str, Any]],
                      res: Dict[str, Any], now: float,
                      extra: Optional[Dict[str, Any]] = None) -> None:
        """Stage a tier-1 card for the end-of-cycle flush."""
        mint = c.get("mint")
        evt, dtrig = self._build_early_evt(
            config, state, degen, c, new_cand, entry, mcap, tier,
            reason, report, res, now, extra=extra)
        state.setdefault("sniper_pending", []).append({
            "score": float(res.get("score") or 0),
            "mint": mint,
            "evt": evt,
            "dtrig": dtrig,
            "digest_args": {
                "c": c, "new_cand": new_cand, "mcap": mcap, "tier": tier,
                "reason": reason, "report": report, "res": res,
                "extra": extra,
            },
        })
        logger.warning("SNIPER STAGED %s %s score=%.1f tier=%s",
                       c.get("symbol"), mint, res.get("score"), tier)

    def _enqueue_early_digest(self, config, state: Dict[str, Any], degen,
                              c: Dict[str, Any], new_cand: Dict[str, Any],
                              mcap: float, tier: str, reason: str,
                              report: Optional[Dict[str, Any]],
                              res: Dict[str, Any], now: float,
                              dtrig: Optional[Dict[str, Any]] = None,
                              extra: Optional[Dict[str, Any]] = None
                              ) -> None:
        """Queue a quality-passed coin into the early digest.

        The sub-tier lane AND the cap-tripped tier-1 fallback share this
        path — nothing queued is ever silently discarded. ``dtrig`` is
        the precomputed deployer trigger when the caller already ran it
        (staged tier-1 cards); otherwise it resolves here. ``extra``
        injects caller-specific item fields (e.g. fast-track markers).
        """
        mint = c.get("mint")
        # Deduped: a fast-track item for this mint may already be queued
        # (it doesn't set early_alerted).
        if self._digest_has(state, mint):
            logger.warning("EARLY DIGEST already queued for %s (%s) tier=%s",
                           c.get("symbol"), mint, tier)
            return
        if dtrig is None:
            # Deployer tracking (#2): resolve + record the launch for
            # quality-passed coins; a hot deployer earns its 🏭 line on
            # the digest item.
            dtrig = self._deployer_card_line(
                config, state, mint, c.get("chain") or "", c, True)
        item = {
            "mint": mint, "symbol": c.get("symbol") or "",
            "name": c.get("name") or "",
            "chain": c.get("chain") or "", "tier": tier,
            "source": c.get("source"),
            "url": c.get("url") or "",
            "reason": reason,
            "safety_score": ((report or {}).get("score")),
            "deployer_line": (dtrig or {}).get("deployer_line"),
            "narratives": (new_cand.get("narratives") or ["other"]),
            "narrative_heat": (new_cand.get("narrative_heat") or 1.0),
            "sentiment": (new_cand.get("sentiment") or {}).get("verdict"),
            "sniper_score": res.get("score"),
            "sniper_tier": res.get("tier"),
            "ts": now,
        }
        if extra:
            item.update(extra)
        state.setdefault("early_digest", []).append(item)
        milestones.track_signal(state, degen, {
            "event": "launch.early_digest",
            "mint": mint, "symbol": c.get("symbol") or "",
            "name": c.get("name") or "",
            "chain": c.get("chain") or "",
            "url": c.get("url") or "",
            "usd_market_cap": mcap,
            "safety_score": ((report or {}).get("score")),
        })
        logger.warning("EARLY DIGEST-QUEUED (quality-passed) %s %s "
                       "tier=%s", c.get("symbol"), mint, tier)

    def _flush_sniper_pending(self, config, state: Dict[str, Any]) -> None:
        """Emit staged tier-1 cards, highest rank first.

        Runs at the end of each scan cycle. Each staged card goes
        through the standing instant cap (``_early_allowed``); when the
        cap trips, the card joins the SAME digest queue as ever — never
        dropped. Cap is consumed at publish time, so the counter counts
        cards actually sent.
        """
        pending = state.pop("sniper_pending", None) or []
        if not pending:
            return
        from agent import events as event_bus  # noqa: PLC0415
        degen = config.degen
        now = time.time()
        # Stable sort: ties keep arrival order, strictly highest rank
        # wins the contested cap slots.
        pending.sort(key=lambda p: float(p.get("score") or 0),
                     reverse=True)
        for p in pending:
            evt = p.get("evt") or {}
            mint = p.get("mint")
            if self._early_allowed(degen, state, now):
                try:
                    milestones.track_signal(state, degen, evt)
                    # Entry timing (measurement only): first user-visible
                    # card — idempotent, the first card wins.
                    evt["latency_min"] = latency.record_first_card(
                        state, degen, mint, now, symbol=evt.get("symbol"),
                        source="launch.early")
                    event_bus.publish(evt)
                except Exception:
                    # Publish failed AFTER the cap slot was consumed: roll
                    # the slot back (no card was sent) and route the
                    # already safety-passed card to the digest queue —
                    # never silently lost, never double-sent.
                    logger.exception(
                        "SNIPER publish failed for %s -> digest queue",
                        mint)
                    w = state.get("early_window")
                    if w:
                        w["count"] = max(0, int(w.get("count", 1)) - 1)
                    d = p.get("digest_args") or {}
                    self._enqueue_early_digest(
                        config, state, degen, d.get("c"),
                        d.get("new_cand"), d.get("mcap"), d.get("tier"),
                        d.get("reason"), d.get("report"), d.get("res"),
                        now, dtrig=p.get("dtrig"),
                        extra=d.get("extra"))
                    continue
                logger.warning("SNIPER [early] %s %s score=%.1f tier=%s",
                               evt.get("symbol"), mint,
                               float(p.get("score") or 0),
                               evt.get("tier"))
            else:
                d = p.get("digest_args") or {}
                logger.warning(
                    "SNIPER HELD (cap) %s %s score=%.1f -> digest queue",
                    evt.get("symbol"), mint,
                    float(p.get("score") or 0))
                self._enqueue_early_digest(
                    config, state, degen, d.get("c"), d.get("new_cand"),
                    d.get("mcap"), d.get("tier"), d.get("reason"),
                    d.get("report"), d.get("res"), now,
                    dtrig=p.get("dtrig"), extra=d.get("extra"))

    @staticmethod
    def _instant_verdict(degen, c: Dict[str, Any]):
        """Fast quality gate for the instant CA.

        Returns (allow, reason, report). The CA drops instantly ONLY when
        fast analysis marks the coin a good candidate: a known safety
        report, no hard fails (incl. insider-cluster / serial-deployer /
        sell-simulation gates), and score >= early_min_score. Unknown or
        unanalyzed coins are held silently — they stay candidates and can
        still reach the confirmed stage. Never raw CA spam.

        Fail-closed throughout: if the flash safety screen is disabled,
        NO instant CA drops — an instant alert requires a live screen to
        have actually run (same rule as the low-cap hunter).
        """
        mint = c.get("mint")
        if not mint:
            return True, "no-mint", None
        if not getattr(degen, "early_safety_flash", True):
            return False, ("flash-disabled (safety screen off — instant CA "
                           "needs a live screen)"), None
        try:
            timeout = float(getattr(degen, "early_flash_timeout_s", 4.0)
                            or 4.0)
            report = safety.check_token(
                c.get("chain") or "", mint, timeout=timeout,
                limits=LaunchScanner._safety_limits(degen))
        except Exception as exc:
            logger.warning("flash safety crashed for %s: %s", mint, exc)
            return False, "safety-error", None
        if report.get("unknown"):
            return False, "safety-unknown", report
        if not report.get("passed"):
            fails = "; ".join((report.get("fails") or [])[:2])
            return False, "safety-fail: %s" % fails, report
        min_score = float(getattr(degen, "early_min_score", 1.0) or 0.0)
        try:
            score = float(report.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        if score < min_score:
            return False, "score %.0f < %.0f" % (score, min_score), report
        return True, "quality-pass score %.0f" % score, report

    def _maybe_digest_unvetted(self, config, state: Dict[str, Any], degen,
                               c: Dict[str, Any], entry: Optional[Dict],
                               new_cand: Dict[str, Any], mcap: float,
                               tier: str, reason: str,
                               report: Optional[Dict[str, Any]],
                               now: float) -> bool:
        """Route a safety-unknown hold into the early digest (user decision
        2026-09-28).

        ONLY the exact ``safety-unknown`` reason qualifies: the screen ran
        but produced no report. Hard fails (honeypot / can't-sell / high
        sell tax / insider clusters), safety errors, flash-disabled, and
        below-bar scores NEVER take this path — they stay held silently.

        The coin is ranked (best-effort, never raises) so the digest item
        carries its runner-probability score, but the tier is IGNORED:
        nothing here can stage an instant card or open a paper position.
        The digest is browse-only and the item carries a prominent
        UNVETTED flag instead of a score.

        Returns True when the coin was queued.
        """
        if reason != "safety-unknown":
            return False
        if not getattr(degen, "digest_include_unknown_safety", False):
            return False
        res = self._sniper_rank(config, state, c, entry, new_cand)
        self._enqueue_early_digest(
            config, state, degen, c, new_cand, mcap, tier, reason,
            report, res, now, extra={"unvetted": True})
        logger.warning("EARLY DIGEST-QUEUED (unvetted) %s %s tier=%s "
                       "sniper=%.1f", c.get("symbol"), c.get("mint"),
                       tier, float(res.get("score") or 0))
        return True

    @staticmethod
    def _unvetted_confirm_allowed(degen, report) -> bool:
        """Chart-confirmed + no safety report + knob on => alert with label.

        Hard fails (known bad: honeypot / can't-sell / high sell tax /
        insider clusters) NEVER take this path — only the unknown-report
        case does.
        """
        return bool((report or {}).get("unknown")
                    and getattr(degen, "confirm_unvetted_alerts", False))

    def _confirm_safety_ok(self, degen, cand):
        """Final safety gate before a confirmation drops a CA.

        Returns (ok, reason, report). A hard fail blocks. An UNKNOWN
        report is returned as not-ok; the caller then decides: with
        ``confirm_unvetted_alerts`` (user decision 2026-09-28) a
        chart-confirmed runner alerts anyway with a prominent UNVETTED
        label, otherwise it is held silently. A KNOWN report scoring
        below ``early_min_score`` never confirms.
        """
        report = safety.check_token(
            cand.get("chain") or "", cand.get("mint"),
            limits=LaunchScanner._safety_limits(degen))
        if report.get("unknown"):
            return False, "safety-unknown (no score)", report
        if not report.get("passed"):
            fails = "; ".join((report.get("fails") or [])[:2])
            return False, "safety-fail: %s" % fails, report
        min_score = float(getattr(degen, "early_min_score", 1.0) or 0.0)
        _sc = report.get("score")
        if (not report.get("unknown")
                and isinstance(_sc, (int, float))
                and _sc < min_score):
            return False, "score %.0f < %.0f" % (_sc, min_score), report
        return True, "ok", report

    @staticmethod
    def _digest_item(d: Dict[str, Any], state: Dict[str, Any]
                     ) -> Dict[str, Any]:
        """One digest queue entry -> the compact item dict on the card."""
        return {"mint": d.get("mint"), "symbol": d.get("symbol"),
                "chain": d.get("chain"), "tier": d.get("tier"),
                "url": d.get("url"), "reason": d.get("reason"),
                "safety_score": d.get("safety_score"),
                "unvetted": bool(d.get("unvetted")),
                "deployer_line": d.get("deployer_line"),
                "smart_money": d.get("smart_money"),
                "fasttrack": d.get("fasttrack"),
                "sniper_score": d.get("sniper_score"),
                "sniper_tier": d.get("sniper_tier"),
                "latency_min": latency.latency_for(state, d.get("mint")),
                "narratives": d.get("narratives") or ["other"],
                "narrative_heat": d.get("narrative_heat") or 1.0,
                "sentiment": d.get("sentiment")}

    def _flush_early_digest(self, config, state: Dict[str, Any]) -> None:
        """Publish one digest card for spam-guard-queued alerts, then reset."""
        degen = config.degen
        window = float(getattr(degen, "early_alert_window_seconds", 300)
                       or 300)
        w = state.get("early_window") or {}
        digest = state.get("early_digest") or []
        if not digest:
            return
        # Digest pause switch (user decision 2026-10-02): when disabled,
        # drop the queued card silently. Milestone tracking already ran
        # at enqueue time, so runners-recap coverage is preserved; only
        # the Telegram card is suppressed. Confirmed runners, warnings,
        # recaps, and Top Picks are untouched. Code default True keeps
        # historical behavior for callers without the knob; the live
        # config sets it false.
        if not getattr(degen, "early_digest_enabled", True):
            state["early_digest"] = []
            state["early_window"] = {"start": time.time(), "count": 0}
            logger.warning("EARLY DIGEST paused: %d queued alerts dropped",
                           len(digest))
            return
        if time.time() - float(w.get("start", 0)) < window:
            return
        from agent import events as event_bus  # noqa: PLC0415
        # Tiers (FIX A): the old single top-20-by-score cut silently
        # dropped most of the queue. Now two non-overlapping lists:
        #   items_safe  — top 15 by safety score (existing behavior).
        #   items_fresh — up to 15 more by MOST RECENTLY QUEUED. The
        #     queue-time ``ts`` (not the score) picks these: the freshest
        #     arrivals are exactly what the old score cut dropped first,
        #     so this tier is the coverage fix — nothing queued is
        #     invisible on the card, and /digestfull exposes the rest.
        by_score = sorted(
            digest, key=lambda d: float(d.get("safety_score") or 0),
            reverse=True)
        items_safe = by_score[:15]
        seen = set()
        for d in items_safe:
            seen.add(d.get("mint") or id(d))
        by_fresh = sorted(
            digest, key=lambda d: float(d.get("ts") or 0), reverse=True)
        items_fresh = [d for d in by_fresh
                       if (d.get("mint") or id(d)) not in seen][:15]
        # Entry timing (measurement only): the digest reaches the user NOW
        # — this is the first card for every queued coin that never had an
        # instant one. Idempotent: an earlier instant card keeps its time.
        flush_ts = time.time()
        for d in digest:
            if isinstance(d, dict) and d.get("mint"):
                latency.record_first_card(
                    state, degen, d.get("mint"), flush_ts,
                    symbol=d.get("symbol"), source="digest")
        n_unvetted = sum(1 for d in digest
                         if isinstance(d, dict) and d.get("unvetted"))
        if n_unvetted:
            note = ("spam guard: %d instant alerts collapsed into one "
                    "digest card; ⚠️ %d of them are UNVETTED — no safety "
                    "report exists, browse only, never vetted picks"
                    % (len(digest), n_unvetted))
        else:
            note = ("spam guard: %d instant alerts collapsed into one "
                    "digest card (all passed fast-analysis quality gate)"
                    % len(digest))
        event_bus.publish({
            "event": "launch.early_digest",
            "count": len(digest),
            "shown": len(items_safe) + len(items_fresh),
            "unvetted_count": n_unvetted,
            "plan": self._trade_plan(degen),
            "items_safe": [self._digest_item(d, state)
                           for d in items_safe],
            "items_fresh": [self._digest_item(d, state)
                            for d in items_fresh],
            "note": note,
        })
        state["early_digest"] = []
        state["early_window"] = {"start": time.time(), "count": 0}
        logger.warning("EARLY DIGEST sent: %d queued alerts (%d safe + "
                       "%d fresh shown)", len(digest), len(items_safe),
                       len(items_fresh))

    # -- low-cap runner hunter ----------------------------------------
    @staticmethod
    def _lowcap_verdict(degen, c):
        """Strict quality verdict for the low-cap hunter.

        Same choke point as the instant CA, but fail-closed: when the
        flash safety screen is disabled the CA must NOT drop — a low-cap
        runner alert requires a live safety screen to have actually run.
        """
        allow, reason, report = LaunchScanner._instant_verdict(degen, c)
        if reason.startswith("flash-disabled"):
            return (False,
                    "flash-disabled (lowcap hunter needs a live safety "
                    "screen)", report)
        return allow, reason, report

    def _lowcap_track(self, config, state: Dict[str, Any],
                      cand: Dict[str, Any],
                      stats: Optional[Dict[str, Any]], now: float,
                      fp_score: Optional[Dict[str, Any]] = None) -> None:
        """Feed one candidate's stats into the low-cap runner hunter.

        Fires at most one UNCONFIRMED ``launch.lowcap_runner`` alert per
        mint — only inside the 70–100% running band, at/below the low-mcap
        ceiling, with the quality gate passed. Alert-only; the confirmed
        pipeline below is untouched and remains the only paper-entry path.
        """
        from agent import events as event_bus  # noqa: PLC0415

        degen = config.degen
        if not getattr(degen, "lowcap_enabled", True):
            return
        mint = cand.get("mint")
        if not mint:
            return
        hunter = getattr(self, "_lowcap_hunter", None)
        if hunter is None or hunter.cfg is not degen:
            # Reuse one hunter per daemon life; rebuild only if the degen
            # config object was swapped (e.g. tests).
            hunter = lowcap.LowcapHunter(degen)
            self._lowcap_hunter = hunter
        tracked = state.setdefault("lowcap", {})
        hunter.prune(tracked, now)
        probe: Dict[str, Any] = {}
        event = hunter.update(
            tracked, mint=mint,
            symbol=cand.get("symbol") or "",
            name=cand.get("name") or "",
            chain=cand.get("chain") or "",
            source=cand.get("source") or "",
            url=cand.get("url") or "",
            stats=stats,
            rising_streak=int(cand.get("rising_streak") or 0),
            now=now,
            verdict_fn=lambda c: self._lowcap_verdict(degen, c),
            probe=probe,
            fp_score=fp_score,
        )
        self._lowcap_funnel(state, tracked, mint, probe, now)
        if not event:
            return
        # Intel layer rides on the card: narrative tags + heat, sentiment
        # verdict. Soft signals only — the quality gate already decided.
        event["narratives"] = cand.get("narratives") or ["other"]
        event["narrative_heat"] = cand.get("narrative_heat") or 1.0
        event["sentiment"] = (cand.get("sentiment") or {}).get("verdict")
        # Winner-likeness ranker (idea #5, user decision 2026-10-01):
        # same scoring as the confirmed lane — the score rides on the
        # card, and winner_min_score (default 0 = OFF) can hold
        # low-scoring lowcap runners once the user approves a threshold
        # via /tune. The safety verdict above is never affected.
        wscore, wfactors, wok = self._winner_score(
            config, state, cand, mint or "", label="lowcap")
        event["winner_score"] = wscore
        if wfactors:
            event["winner_factors"] = "; ".join(wfactors)
        if self._winner_hold_check(degen, state, mint or "",
                                   event.get("symbol"), wscore, wok,
                                   "lowcap", now):
            return
        # Holder-quality ranker (idea #1, user decision 2026-10-01):
        # same display-only scoring as the confirmed lane — the score
        # rides on the card, never gates the alert.
        hscore, hfactors = self._holder_score(
            state, cand, mint or "", event.get("run_pct"))
        event["holder_score"] = hscore
        if hfactors:
            event["holder_factors"] = "; ".join(hfactors)
        if self._early_allowed(degen, state, now):
            event["plan"] = self._trade_plan(degen)
            milestones.track_signal(state, degen, event)
            # Entry timing (measurement only): first user-visible card.
            event["latency_min"] = latency.record_first_card(
                state, degen, mint, now, symbol=event.get("symbol"),
                source="launch.lowcap_runner")
            event_bus.publish(event)
            logger.warning("LOWCAP RUNNER %s %s +%.0f%% mcap=$%.0f",
                           event["symbol"], mint, event["run_pct"],
                           event["usd_market_cap"])
        else:
            # Spam guard tripped — queue into the digest. The quality gate
            # already passed, so this item may enter the digest; anything
            # else never reaches a CA-bearing alert.
            state.setdefault("early_digest", []).append({
                "mint": mint, "symbol": event["symbol"],
                "name": event.get("name") or "",
                "chain": event.get("chain") or "", "tier": "lowcap",
                "source": event.get("source"),
                "url": event.get("url") or "",
                "reason": "lowcap run +%.0f%% @ mcap $%.0f" % (
                    event["run_pct"], event["usd_market_cap"]),
                "safety_score": event.get("safety_score"),
                "narratives": event.get("narratives") or ["other"],
                "narrative_heat": event.get("narrative_heat") or 1.0,
                "sentiment": event.get("sentiment"),
                "ts": now,
            })
            milestones.track_signal(state, degen, {
                "event": "launch.early_digest",
                "mint": mint, "symbol": event["symbol"],
                "name": event.get("name") or "",
                "chain": event.get("chain") or "",
                "url": event.get("url") or "",
                "usd_market_cap": event.get("usd_market_cap"),
                "safety_score": event.get("safety_score"),
            })
            logger.warning("LOWCAP DIGEST-QUEUED %s %s",
                           event["symbol"], mint)

    def _lowcap_funnel(self, state: Dict[str, Any],
                       tracked: Dict[str, Dict[str, Any]],
                       mint: str, probe: Dict[str, Any], now: float) -> None:
        """Aggregate the hunter's per-gate probe into a funnel snapshot.

        Keeps the last known gate per tracked mint (bounded by the
        hunter's own MAX_TRACKED pruning) plus lifetime counters, and
        logs a one-line funnel summary every ~15 minutes so the binding
        constraint is visible without guessing.
        """
        funnel = state.setdefault("lowcap_funnel", {})
        by_mint = funnel.setdefault("by_mint", {})
        if probe.get("fired"):
            funnel["fired"] = int(funnel.get("fired") or 0) + 1
            by_mint.pop(mint, None)
        elif probe.get("baselined"):
            funnel["baselined"] = int(funnel.get("baselined") or 0) + 1
            by_mint[mint] = "watching"
        elif probe.get("blocked_by"):
            by_mint[mint] = probe["blocked_by"]
        # Drop entries for mints the hunter no longer tracks.
        for m in list(by_mint):
            if m not in tracked:
                by_mint.pop(m, None)
        last = float(funnel.get("last_log_ts") or 0)
        if now - last < 900:
            return
        funnel["last_log_ts"] = now
        counts: Dict[str, int] = {}
        for reason in by_mint.values():
            counts[reason] = counts.get(reason, 0) + 1
        breakdown = " ".join("%s:%d" % kv for kv in sorted(counts.items()))
        hot_fp = sum(1 for e in (state.get("fingerprints") or {}).values()
                     if isinstance(e, dict) and e.get("verdict") == "HOT")
        logger.warning("LOWCAP FUNNEL tracked=%d baselined=%d fired=%d "
                       "hot_fp=%d %s%s",
                       len(tracked), funnel.get("baselined") or 0,
                       funnel.get("fired") or 0, hot_fp, breakdown,
                       latency.funnel_bits(state))

    def _fingerprint_track(self, config, state: Dict[str, Any],
                           cand: Dict[str, Any],
                           stats: Optional[Dict[str, Any]],
                           now: float) -> Optional[Dict[str, Any]]:
        """Feed one candidate's stats into the early-fingerprint tracker.

        Returns the composite fingerprint score dict (or None when too
        early / disabled). HOT fingerprints are logged once per mint.
        Holder counts refresh opportunistically: Solana mints already
        tracked by the low-cap hunter, at most fp_holder_refresh_s
        apart and fp_holder_per_cycle fresh HTTP calls per judge cycle.
        """
        degen = config.degen
        if not getattr(degen, "fp_enabled", True):
            return None
        mint = cand.get("mint")
        if not mint or not stats:
            return None
        tracker = getattr(self, "_fp_tracker", None)
        if tracker is None or tracker.cfg is not degen:
            tracker = fingerprints.FingerprintTracker(degen)
            self._fp_tracker = tracker
        tracker.prune(now)
        fstate = state.setdefault("fingerprints", {})
        # Prune entries for mints that left the pipeline — otherwise
        # this dict grows without bound as candidates churn. (Candidate
        # dict keys are usually the mint but can fall back to
        # chain:symbol, so compare against candidate mints, not keys.)
        keep = {c.get("mint") for c in (state.get("candidates") or {}).values()
                if isinstance(c, dict) and c.get("mint")}
        keep.update(state.get("lowcap") or {})
        for m in list(fstate):
            if m not in keep:
                fstate.pop(m, None)
        holders = self._fp_holder_refresh(degen, state, fstate, cand, now)
        tracker.observe(mint, stats, now, holders=holders)
        sc = tracker.score(mint)
        if not sc:
            return None
        entry = fstate.setdefault(mint, {})
        entry.update({"score": sc["composite"], "verdict": sc["verdict"],
                      "buy_accel": sc["buy_accel"],
                      "vol_accel": sc["vol_accel"],
                      "holder_growth_pct": sc["holder_growth_pct"],
                      "buy_pressure": sc["buy_pressure"], "ts": now})
        if sc["verdict"] == "HOT" and not entry.get("logged_hot"):
            entry["logged_hot"] = True
            hg = sc["holder_growth_pct"]
            logger.warning(
                "FINGERPRINT HOT %s %s score=%.0f buy_accel=%.1fx "
                "vol_accel=%.1fx holders=%s (%s) pressure=%.2f",
                cand.get("symbol"), mint, sc["composite"],
                sc["buy_accel"], sc["vol_accel"],
                entry.get("holders"),
                ("+%s%%" % hg) if hg is not None else "n/a",
                sc["buy_pressure"])
        return sc

    def _fp_holder_refresh(self, degen, state: Dict[str, Any],
                           fstate: Dict[str, Any],
                           cand: Dict[str, Any], now: float
                           ) -> Optional[int]:
        """Rate-limited RugCheck totalHolders for lowcap-tracked mints."""
        mint = cand.get("mint")
        if not mint or (cand.get("chain") or "") != "solana":
            return None
        if mint not in (state.get("lowcap") or {}):
            return None
        entry = fstate.setdefault(mint, {})
        refresh_s = float(getattr(degen, "fp_holder_refresh_s", 600.0))
        if now - float(entry.get("holder_ts") or 0) < refresh_s:
            return entry.get("holders")
        if int(getattr(self, "_fp_holder_budget", 0)) <= 0:
            return entry.get("holders")
        self._fp_holder_budget -= 1
        holders = fingerprints.fetch_total_holders(mint)
        entry["holder_ts"] = now
        if holders is not None:
            entry["holders"] = holders
        return entry.get("holders")

    # -- intel layer: sentiment + narratives + backtest recorder --------
    def _narr_heat_tracker(self):
        """Lazy narrative-heat tracker, loaded from disk once.

        ``NarrativeHeat.load`` is a classmethod that RETURNS the loaded
        tracker — the return value must be kept, otherwise saved heat
        is silently discarded and restarts begin empty.
        """
        tr = getattr(self, "_narr_heat", None)
        if tr is None:
            path = narratives.default_heat_path()
            try:
                tr = narratives.NarrativeHeat.load(path)
            except Exception:
                tr = narratives.NarrativeHeat()
            self._narr_heat = tr
            self._narr_heat_path = path
            self._narr_heat_saved = 0.0
        return tr

    def _narr_heat_save(self, now: float, force: bool = False) -> None:
        tr = getattr(self, "_narr_heat", None)
        if tr is None:
            return
        if force or now - float(getattr(self, "_narr_heat_saved", 0.0)) > 900:
            try:
                tr.save(self._narr_heat_path)
                self._narr_heat_saved = now
            except Exception:
                logger.warning("narrative heat save failed", exc_info=True)

    def _narrative_attach(self, config, state: Dict[str, Any],
                          cand: Dict[str, Any], now: float) -> Optional[float]:
        """Refresh the candidate's narrative heat multiplier.

        Soft signal only: it rides on the card and in logs, and the
        backtester measures whether it earns its keep. It never gates.
        """
        degen = config.degen
        if not getattr(degen, "narrative_enabled", True):
            cand.pop("narrative_heat", None)
            return None
        tags = cand.get("narratives") or ["other"]
        tr = self._narr_heat_tracker()
        try:
            tr.prune(now)
        except Exception:
            pass
        h = tr.heat(tags, now)
        cand["narrative_heat"] = round(h, 2)
        self._narr_heat_save(now)
        return h

    def _sentiment_track(self, config, state: Dict[str, Any],
                         cand: Dict[str, Any], now: float
                         ) -> Optional[Dict[str, Any]]:
        """Rate-limited social-sentiment observation for one candidate.

        Keyless DexScreener profile fetch, at most
        sentiment_per_cycle fresh HTTP calls per judge cycle and one
        refresh per mint every 10 minutes. Soft boost only — it scores,
        it never allows/blocks/drops.
        """
        degen = config.degen
        if not getattr(degen, "sentiment_enabled", True):
            return None
        mint = cand.get("mint")
        if not mint:
            return None
        chain = (cand.get("chain") or "solana").lower()
        tr = getattr(self, "_sent_tracker", None)
        if tr is None or getattr(tr, "cfg", None) is not degen:
            tr = sentiment.SentimentTracker(degen)
            self._sent_tracker = tr
        tr.prune(now)
        entry = state.setdefault("sentiment", {}).setdefault(mint, {})
        if now - float(entry.get("profile_ts") or 0) < 600:
            profile = entry.get("profile")
        else:
            if not hasattr(self, "_sent_budget"):
                self._sent_budget = int(
                    getattr(degen, "sentiment_per_cycle", 2))
            if int(self._sent_budget) <= 0:
                return entry.get("verdict_info")
            self._sent_budget -= 1
            try:
                profile = sentiment.fetch_token_profile(chain, mint)
            except Exception:
                logger.warning("sentiment profile fetch failed for %s",
                               mint)
                return entry.get("verdict_info")
            entry["profile_ts"] = now
            entry["profile"] = profile
        if profile is None:
            return entry.get("verdict_info")
        sc = tr.observe(mint, profile, now)
        if sc:
            entry["verdict_info"] = sc
            cand["sentiment"] = sc
        return sc

    def _backtest_record(self, config, state: Dict[str, Any],
                         cand: Dict[str, Any], verdict: str,
                         now: float) -> None:
        """Append a closed candidate to the backtest dataset (JSONL).

        Called on terminal verdicts (confirm/reject) so
        scripts/backtest can replay history through the entry/exit
        rules. Never raises; never affects live decisions.
        """
        degen = config.degen
        if not getattr(degen, "backtest_enabled", True):
            return
        try:
            path = os.path.expanduser(str(
                getattr(degen, "backtest_dataset_path",
                        "~/.forex-agent/launch_scanner/"
                        "backtest_dataset.jsonl")))
            if _skip_live_write(path):
                return
            mint = cand.get("mint") or ""
            snaps = []
            tr = getattr(self, "_fp_tracker", None)
            series = getattr(tr, "_series", {}).get(mint) if tr else None
            if series:
                for o in series:
                    snaps.append({
                        "t": o[0], "mcap_usd": o[1], "price_usd": o[2],
                        "vol5m_usd": o[3], "buys5m": int(o[4]),
                        "sells5m": int(o[5]), "liq_usd": o[6]})
            saf = cand.get("safety") or {}
            if verdict == "confirm":
                first = float(cand.get("first_mcap") or 0)
                last = float(cand.get("last_mcap") or 0)
                gain = last / first if first > 0 else 1.0
                outcome = {"peak_mult": round(gain, 3), "peak_t": now,
                           "rug": False, "end_t": now}
            else:
                outcome = {"peak_mult": 1.0, "peak_t": now,
                           "rug": False, "end_t": now}
            backtest.record_history(path, {
                "schema_version": backtest.HISTORY_SCHEMA_VERSION,
                # "judge": outcome synthesized from judge snapshots at
                # confirm/reject time. Real realized outcomes arrive via
                # _backtest_record_paper_close with outcome_kind="paper".
                "outcome_kind": "judge",
                "mint": mint, "symbol": cand.get("symbol") or "",
                "chain": cand.get("chain") or "",
                "source": cand.get("source") or "",
                "t0": cand.get("first_ts") or now,
                "snapshots": snaps,
                "safety": {"score": saf.get("score"), "passed": True,
                           "fails": saf.get("fails", []),
                           "unknown": saf.get("score") is None},
                "sentiment": cand.get("sentiment"),
                "narratives": cand.get("narratives") or ["other"],
                "outcome": outcome, "verdict": verdict,
            })
        except Exception:
            logger.warning("backtest record failed", exc_info=True)

    def _record_narrative_close(self, degen, state: Dict[str, Any],
                                mint: str, pnl: float) -> None:
        """Record a closed paper position into the narrative-heat
        tracker. Never raises; never affects live decisions."""
        try:
            ntags = (state.get("narrative_tags") or {}).get(mint) or [
                "other"]
            vol = float(getattr(degen, "entry_sol", 0) or 0)
            mult = 1.0 + (pnl / vol) if vol > 0 else 1.0
            ntr = self._narr_heat_tracker()
            ntr.record_outcome(ntags, max(0.0, mult), False, time.time())
            self._narr_heat_save(time.time(), force=True)
        except Exception:
            logger.warning("narrative heat record failed", exc_info=True)

    def _backtest_record_paper_close(self, config, state: Dict[str, Any],
                                     mint: str, symbol: str,
                                     pos: Dict[str, Any],
                                     entry_px: float, close_px: float,
                                     reason: str, pnl: float,
                                     now: float) -> None:
        """Append a REAL paper-position outcome to the backtest dataset.

        Unlike the judge-verdict snapshots (which synthesize outcomes),
        this records what actually happened: true entry/exit/peak prices
        in SOL, real timestamps, the exit reason, and a rug flag for
        near-total losses. ``outcome_kind="paper"`` tells the backtest
        engine to tally it as a realized result instead of re-simulating
        it. Never raises; never affects live decisions.
        """
        degen = config.degen
        if not getattr(degen, "backtest_enabled", True):
            return
        try:
            path = os.path.expanduser(str(
                getattr(degen, "backtest_dataset_path",
                        "~/.forex-agent/launch_scanner/"
                        "backtest_dataset.jsonl")))
            if _skip_live_write(path):
                return
            entry_px = float(entry_px or 0)
            close_px = float(close_px or 0)
            peak = float(pos.get("peak_price_sol") or entry_px or close_px)
            realized_mult = (close_px / entry_px) if entry_px > 0 else 1.0
            peak_mult = (peak / entry_px) if entry_px > 0 else 1.0
            rug = realized_mult <= 0.10  # lost 90%+ — liquidity-pull shape
            snaps: List[Dict[str, Any]] = []
            tr = getattr(self, "_fp_tracker", None)
            series = getattr(tr, "_series", {}).get(mint) if tr else None
            if series:
                for o in series:
                    snaps.append({
                        "t": o[0], "mcap_usd": o[1], "price_usd": o[2],
                        "vol5m_usd": o[3], "buys5m": int(o[4]),
                        "sells5m": int(o[5]), "liq_usd": o[6]})
            saf_score = pos.get("safety_score")
            rec = {
                "schema_version": backtest.HISTORY_SCHEMA_VERSION,
                "outcome_kind": "paper",
                "mint": mint, "symbol": symbol or "", "chain": "solana",
                "wave": int(pos.get("wave", 1) or 1),  # 1 = first wave,
                                                      # >=2 = second-wave re-entry
                "label": pos.get("label") or "launch",  # lane: launch /
                                                       # momentum / second_wave
                "leg": pos.get("leg") or "A",  # two-tier exit leg: A = classic
                                               # exits, B = runner (no fixed TP)
                "source": pos.get("source") or "",
                "t0": pos.get("entry_ts") or now,
                "t_entry": pos.get("entry_ts") or now,
                "t_close": now,
                "entry_price_sol": entry_px,
                "close_price_sol": close_px,
                "peak_price_sol": peak,
                "peak_mult": round(peak_mult, 4),
                "realized_mult": round(realized_mult, 4),
                "pnl_sol": round(float(pnl or 0), 6),
                "exit_reason": reason,
                # Exit-policy version (v1 = single-stage trail, v2 =
                # Leg A profit-lock added 2026-10-02): keeps the exit
                # measurement honest across the regime change.
                "exit_version": EXIT_POLICY_VERSION,
                "rug": rug,
                "outcome": {"peak_mult": round(peak_mult, 4),
                            "peak_t": now, "rug": rug, "end_t": now},
                "safety": {"score": saf_score,
                           "unknown": saf_score is None,
                           "fails": pos.get("safety_fails") or []},
                "narratives": (pos.get("narratives")
                               or (state.get("narrative_tags") or {}).get(
                                   mint) or ["other"]),
                "narrative_heat": pos.get("narrative_heat") or 1.0,
                # Sniper score at entry — the #6 feedback loop's raw
                # material for threshold analysis. None = unscored lane.
                "sniper_score": pos.get("sniper_score"),
                # Entry-signal breakdown + hold time — the #6 entry and
                # exit learners' raw material. None = unscored lane.
                "entry_signals": pos.get("entry_signals"),
                "hold_min": round(max(0.0, (now - float(
                    pos.get("entry_ts") or now)) / 60.0), 1),
                "sentiment": pos.get("sentiment"),
                "sentiment_verdict": pos.get("sentiment_verdict"),
                "snapshots": snaps,
                "verdict": "confirm",
            }
            backtest.record_history(path, rec)
        except Exception:
            logger.warning("paper backtest record failed", exc_info=True)

    # -- stage 2: strict runner confirmation ----------------------------
    def _judge(self, config, adapter, state: Dict[str, Any]) -> None:
        degen = config.degen
        candidates = state.get("candidates") or {}
        if not candidates:
            return
        judge = runner.RunnerJudge(degen)
        boost_wallets = int(getattr(degen, "smart_boost_wallets", 2))
        boost_polls = max(1, int(getattr(degen, "smart_rising_polls", 2)))
        now = time.time()
        # Per-cycle budget for fresh RugCheck holder lookups (the
        # fingerprint tracker decrements it; see _fp_holder_refresh).
        self._fp_holder_budget = int(
            getattr(degen, "fp_holder_per_cycle", 3))
        # Per-cycle budget for fresh DexScreener social-profile lookups
        # (the sentiment tracker decrements it; see _sentiment_track).
        self._sent_budget = int(
            getattr(degen, "sentiment_per_cycle", 2))
        batch = self._batch_refresh(candidates, now)
        for key in list(candidates):
            cand = candidates[key]
            # Smart-money accelerant: candidates that tracked wallets
            # bought need fewer rising polls to confirm.
            smart_buys = smartmoney.smart_buys_for(
                state, cand.get("mint") or "")
            cand["smart_buys"] = smart_buys
            j = judge
            if smart_buys >= boost_wallets:
                j = runner.RunnerJudge(degen)
                j.rising_polls = boost_polls
                logger.warning("SMART-MONEY boost %s: %d tracked wallets "
                               "bought — confirming on %d rising polls",
                               cand.get("symbol"), smart_buys, boost_polls)
            try:
                verdict, stats = self._verdict(j, cand, now, batch)
            except Exception:
                logger.exception("judge failed for %s", key)
                continue
            try:
                fp_sc = self._fingerprint_track(config, state, cand,
                                                stats, now)
            except Exception:
                logger.exception("fingerprint track failed for %s", key)
                fp_sc = None
            try:
                self._sentiment_track(config, state, cand, now)
            except Exception:
                logger.exception("sentiment track failed for %s", key)
            try:
                self._narrative_attach(config, state, cand, now)
            except Exception:
                logger.exception("narrative attach failed for %s", key)
            try:
                self._lowcap_track(config, state, cand, stats, now,
                                   fp_score=fp_sc)
            except Exception:
                logger.exception("lowcap track failed for %s", key)
            if verdict == runner.WAIT:
                continue
            candidates.pop(key, None)
            seen = state.setdefault("seen", {})
            seen.setdefault(key, {})["alerted"] = True
            if verdict == runner.REJECT:
                logger.warning("DROPPED (not a runner) %s %s",
                               cand.get("symbol"), cand.get("mint"))
                self._backtest_record(config, state, cand, "reject", now)
                continue
            # CONFIRMED — final safety gate before any alert or entry.
            tier = cand.get("tier") or "runner"
            if getattr(degen, "safety_gate", True) and cand.get("mint"):
                ok, reason, report = self._confirm_safety_ok(degen, cand)
                if not ok:
                    if self._unvetted_confirm_allowed(degen, report):
                        # Chart-confirmed but no safety report: the chart
                        # proof IS the analysis. Alert WITH a prominent
                        # UNVETTED label instead of silence. Hard fails
                        # (known honeypot / can't-sell / high sell tax /
                        # insider clusters) still drop below.
                        logger.warning(
                            "CONFIRMED UNVETTED %s %s — alerting without "
                            "safety report", cand.get("symbol"),
                            cand.get("mint"))
                        cand["safety"] = None
                        cand["unvetted_confirmed"] = True
                    else:
                        logger.warning("BLOCKED (%s) %s %s", reason,
                                       cand.get("symbol"), cand.get("mint"))
                        # Early-warning lane: this coin PROVED itself a
                        # runner (multi-poll confirmation) but failed the
                        # final safety gate — the strongest possible warning
                        # trigger. One UNCONFIRMED card, never a trade.
                        self._early_warn_held(
                            config, state, "launch", cand, report,
                            (stats or {}).get("usd_market_cap") or 0.0,
                            "confirmed_runner_blocked",
                            {"tier": cand.get("tier") or "runner",
                             "url": (stats or {}).get("url")
                                    or cand.get("url")})
                        continue  # rug risk — drop silently from main flow
                # Deep scan: only meaningful on a KNOWN report — the cheap
                # gate raised insider/concentration flags, so walk the
                # funding graph before letting it through. Unvetted coins
                # have no flags to follow up on.
                if (ok and (cand.get("chain") or "").lower() == "solana"
                        and any(
                            k in " ".join(
                                (report or {}).get("flags", [])).lower()
                            for k in ("insider", "top holder", "top 5"))):
                    deep = bundle.analyze(
                        cand["mint"],
                        max_cluster_pct=float(
                            getattr(degen, "bundle_max_cluster_pct", 30.0)))
                    if not deep.get("passed"):
                        logger.warning("BLOCKED (bundle) %s %s: %s",
                                       cand.get("symbol"), cand.get("mint"),
                                       "; ".join(deep.get("fails", [])))
                        continue  # bundled supply — drop silently
                    report["flags"].extend(deep.get("flags", []))
                    cand["bundle"] = deep
                if not cand.get("unvetted_confirmed"):
                    cand["safety"] = report
                # Unvetted confirmed coins keep safety=None (set above) so
                # the card renders the UNVETTED label, never a phantom score.
            # Full alert + paper entry path.
            cand["confirm_stats"] = stats or {}
            entry = seen.get(key) or {}
            entry.update({
                "symbol": cand.get("symbol") or entry.get("symbol") or "",
                "name": cand.get("name") or entry.get("name") or "",
                "chain": cand.get("chain") or entry.get("chain") or "",
                "source": cand.get("source") or entry.get("source") or "",
                "url": cand.get("url") or entry.get("url") or "",
            })
            fake_c = {
                "mint": cand.get("mint"),
                "symbol": cand.get("symbol"),
                "name": cand.get("name"),
                "chain": cand.get("chain"),
                "source": cand.get("source"),
                "url": (stats or {}).get("url") or cand.get("url"),
                "usd_market_cap": (stats or {}).get("usd_market_cap")
                                   or cand.get("last_mcap") or 0,
                "confirm_stats": stats or {},
                "confirm": {
                    "gain_mult": round(
                        (cand.get("last_mcap") or 0)
                        / max(cand.get("first_mcap") or 1, 1), 2),
                    "rising_polls": int(cand.get("rising_streak") or 0),
                    "vol_m5": round(cand.get("last_vol_m5") or 0),
                    "liquidity": round(cand.get("last_liq") or 0),
                    "buys_m5": (stats or {}).get("buys_m5", 0),
                    "sells_m5": (stats or {}).get("sells_m5", 0),
                },
                "safety": cand.get("safety"),
                "unvetted_confirmed": bool(cand.get("unvetted_confirmed")),
                "smart_buys": int(cand.get("smart_buys") or 0),
                # Intel layer: the judge refreshed these per cycle, so the
                # confirmed card carries the live tags/heat/sentiment.
                "narratives": cand.get("narratives") or ["other"],
                "narrative_heat": cand.get("narrative_heat") or 1.0,
                "sentiment": cand.get("sentiment"),
            }
            # Standing 4/hour instant cap also guards the confirmed lane:
            # cap-tripped runners stay visible via the digest, never lost.
            if not self._early_allowed(degen, state, now):
                logger.warning("CONFIRMED cap-tripped %s %s -> digest queue",
                               fake_c.get("symbol"), fake_c.get("mint"))
                self._enqueue_early_digest(
                    config, state, degen, fake_c, cand,
                    fake_c.get("usd_market_cap") or 0, tier,
                    "chart-confirmed (instant cap tripped)",
                    cand.get("safety"),
                    {"score": None, "tier": "confirmed", "threshold": 50.0},
                    now)
                self._backtest_record(config, state, cand, "confirm", now)
                continue
            self._on_launch(config, adapter, state, fake_c, entry, tier, key)
            self._backtest_record(config, state, cand, "confirm", now)

    @staticmethod
    def _batch_refresh(candidates: Dict[str, Any],
                       now: float) -> Dict[str, Dict[str, Any]]:
        """Refresh pair stats for all due candidates: one DexScreener
        batch call per chain (up to 30 mints each) instead of one call
        per candidate."""
        due: Dict[str, List[str]] = {}
        for cand in candidates.values():
            mint = cand.get("mint")
            if not mint:
                continue
            if now - float(cand.get("stats_ts", 0)) < 30:
                continue
            chain = (cand.get("chain") or "solana").lower()
            due.setdefault(chain, []).append(mint)
        out: Dict[str, Dict[str, Any]] = {}
        for chain, mints in due.items():
            try:
                out.update(sources.dexscreener_batch_stats(mints, chain))
            except Exception:
                logger.warning("batch stats failed for chain %s", chain)
        return out

    def _verdict(self, judge, cand: Dict[str, Any], now: float,
                 batch: Optional[Dict[str, Dict[str, Any]]] = None):
        """Return (verdict, stats). Hyperliquid spot listings have no mint
        to analyze — a fresh exchange listing is itself the confirmation."""
        mint = cand.get("mint")
        if not mint:
            if cand.get("source") == "hyperliquid-listing":
                return runner.CONFIRM, {}
            return runner.REJECT, None
        # Throttle pair-stats refreshes to ~every 30s per candidate.
        if now - float(cand.get("stats_ts", 0)) < 30:
            stats = cand.get("stats")
        else:
            stats = (batch or {}).get(mint)
            if stats is None:
                stats = sources.dexscreener_token_stats(mint)
            cand["stats"] = stats
            cand["stats_ts"] = now
        return judge.update(cand, stats, now), stats

    def _qualify(self, degen, c: Dict[str, Any],
                 entry: Dict[str, Any], now: float,
                 state: Optional[Dict[str, Any]] = None) -> Optional[str]:
        src = c.get("source")
        if src == "pumpfun":
            extra = c.get("extra") or {}
            if extra.get("banned"):
                return None
            mcap = float(c.get("usd_market_cap") or 0)
            if extra.get("complete"):
                # Graduated coins bypass the "fresh launch" age gate, but
                # they must still clear an absolute market-cap floor —
                # otherwise dust graduates become candidates.
                if mcap >= float(degen.min_usd_market_cap):
                    return "graduated"
                return None
            age_min = (now - entry["first_ts"]) / 60.0
            if age_min <= float(degen.max_age_minutes) and \
                    mcap >= float(degen.min_usd_market_cap):
                return "early"
            hist = entry.get("mcap") or []
            if len(hist) >= 2:
                lo = min(m[1] for m in hist)
                hi = max(m[1] for m in hist)
                if lo > 0 and hi / lo >= float(degen.velocity_multiplier) \
                        and mcap >= float(degen.min_usd_market_cap) / 2:
                    return "velocity"
            return None
        if src == "dexscreener-boost":
            mint = c.get("mint") or ""
            stats = sources.dexscreener_token_stats(mint)
            if not stats:
                return None
            entry["symbol"] = stats.get("symbol") or entry["symbol"]
            entry["name"] = stats.get("name") or entry["name"]
            entry["url"] = stats.get("url") or entry["url"]
            liq = float(stats.get("usd_liquidity") or 0)
            mc = float(stats.get("usd_market_cap") or 0)
            if liq >= 10000 or mc >= 50000:
                return "boost"
            return None
        if src == "pumpfun-log":
            # Sub-second program-log detection: market cap is unknown at this
            # point, so the instant alert is the whole point. Confirmation
            # still needs real pair data from the judge.
            return "logstream"
        if src == "gecko-usdc-pool":
            # USDC-quoted DEX launches (Raydium/Meteora/Orca on Solana,
            # Aerodrome/Uniswap on Base, Uniswap on Ethereum). Already
            # trading against real USDC liquidity, so they enter like
            # graduated launches: fresh pools get the "early" tier
            # (instant verdict), older first-seen pools are tracked for
            # the confirmed stage. Same safety gates either way.
            mcap = float(c.get("usd_market_cap") or 0)
            if mcap < float(degen.min_usd_market_cap):
                return None
            created = float(c.get("created_ts") or 0)
            if created > 0:
                age_min = (now - created) / 60.0
                if age_min <= float(degen.max_age_minutes):
                    return "early"
            return "graduated"
        if src == "hyperliquid-listing":
            sym = c.get("symbol") or ""
            if not sym:
                return None
            if state is None:
                return "listing"  # no baseline tracking available
            known = state_hl(state)
            # First sighting seeds the baseline silently — pre-existing
            # listings are not "new" and must not alert on startup.
            if not state.get("hl_seeded"):
                if sym not in known:
                    known.append(sym)
                return None
            if sym not in known:
                known.append(sym)
                return "listing"
            return None
        return None

    # -- launch handling ------------------------------------------------
    @staticmethod
    def _resolve_symbol(mint: Optional[str],
                        raw_symbol: str) -> str:
        """Unique, registry-safe symbol for a mint.

        Falls back to DGEN<mint-prefix> for unusable tickers, and suffixes
        (PIXEL2, ...) when a different mint already owns the ticker —
        duplicate tickers are the norm on pump.fun.
        """
        from daemon.telegram import watchlist  # noqa: PLC0415
        clean = watchlist.sanitize_symbol(raw_symbol)
        base = clean or ("DGEN" + "".join(
            ch for ch in (mint or "") if ch.isalnum())[:6].upper())
        try:
            _mon, registry = watchlist.current_watchlist()
        except Exception:
            return base
        if registry.get(base) in (None, mint):
            return base
        for n in range(2, 100):
            cand = ("%s%d" % (base, n))[:12]
            if registry.get(cand) in (None, mint):
                return cand
        return base

    def _winner_score(self, config, state: Dict[str, Any],
                      cand: Dict[str, Any], mint: str,
                      label: str = "launch"
                      ) -> Tuple[float, List[str], bool]:
        """Winner-likeness score (idea #5) for a live candidate.

        Trains (once per day, cached) on real paper-close records and
        scores the candidate 0-100 against past 10x+ runners. Returns
        ``(score, top_factor_lines, model_ready)``. Never raises; on any
        failure returns (0.0, [], False) — fail-neutral, never a gate by
        itself. The ``winner_min_score`` knob (default 0 = off) decides
        whether a low score holds the alert.
        """
        try:
            from daemon.launch_scanner import winner_model  # noqa: PLC0415
            now = time.time()
            model = getattr(self, "_winner_model", None)
            if model is None or now - float(
                    getattr(self, "_winner_model_ts", 0) or 0) > 86400:
                recs = feedback.load_outcomes(
                    feedback.default_dataset_path(config.degen))
                ins = {str((r or {}).get("mint") or ""): r
                       for r in (state.get("insider_outcomes") or [])
                       if isinstance(r, dict)}
                model = winner_model.train(recs, ins)
                self._winner_model = model
                self._winner_model_ts = now
            if int((model or {}).get("n_total") or 0) < 5:
                return 0.0, [], False
            fp = (state.get("fingerprints") or {}).get(mint) or {}
            snaps: List[Dict[str, Any]] = []
            tr = getattr(self, "_fp_tracker", None)
            series = getattr(tr, "_series", {}).get(mint) if tr else None
            if series:
                for o in series:
                    try:
                        snaps.append({"t": o[0], "mcap_usd": o[1]})
                    except (IndexError, TypeError):
                        continue
            synth = winner_model.synth_record_from_candidate(
                cand, fp=fp, snaps=snaps, label=label)
            score, contribs = winner_model.score(
                model, winner_model.features_from_record(synth))
            lines = ["%s=%s" % (f, b) for f, b, _lift in contribs[:2]]
            return score, lines, True
        except Exception:
            logger.exception("winner scoring failed for %s", mint)
            return 0.0, [], False

    @staticmethod
    def _winner_hold_check(degen, state: Dict[str, Any], mint: str,
                           symbol: str, score: float, ready: bool,
                           lane: str, now: float) -> bool:
        """True when the winner_min_score knob holds this runner.

        The knob defaults to 0 = OFF (score shown, nothing held); it
        only bites after the user approves a threshold via /tune. The
        hold is recorded in state["winner_held"] (bounded, newest 200)
        for measurement. Hard safety gates are never affected.
        """
        wmin = float(getattr(degen, "winner_min_score", 0.0) or 0.0)
        if not (ready and wmin > 0 and score < wmin):
            return False
        held = state.setdefault("winner_held", {})
        held[mint] = {"symbol": symbol, "score": score, "min": wmin,
                      "ts": now, "lane": lane}
        if len(held) > 200:
            for k in sorted(held, key=lambda k: held[k]["ts"])[
                    :len(held) - 200]:
                held.pop(k, None)
        logger.warning("WINNER-HELD (%s) %s %s score=%.0f < min=%.0f",
                       lane, symbol, mint, score, wmin)
        return True

    def _holder_score(self, state: Dict[str, Any], cand: Dict[str, Any],
                      mint: str,
                      mcap_growth_pct: Optional[float] = None
                      ) -> Tuple[float, List[str]]:
        """Holder-quality ranker (idea #1) for a live candidate.

        Scores 0-100 whether holder distribution is broadening with the
        price (healthy accumulation) or the mcap is sprinting while
        holders stall (distribution into exits). Display-only: the
        score rides on the card and NEVER gates an alert. Unknown data
        is fail-neutral (50.0, no factors). Never raises.
        """
        try:
            from daemon.launch_scanner import holder_quality  # noqa: PLC0415
            fp = (state.get("fingerprints") or {}).get(mint) or {}
            saf = cand.get("safety")
            inp = holder_quality.holder_inputs(
                fp, saf if isinstance(saf, dict) else None)
            return holder_quality.score(inp, mcap_growth_pct=mcap_growth_pct)
        except Exception:
            logger.exception("holder scoring failed for %s", mint)
            return 50.0, []

    def _on_launch(self, config, adapter, state: Dict[str, Any],
                   c: Dict[str, Any], entry: Dict[str, Any],
                   tier: str, key: str) -> None:
        from agent import events as event_bus  # noqa: PLC0415

        degen = config.degen
        mint = c.get("mint")
        chain = c.get("chain") or ""
        symbol = self._resolve_symbol(mint, entry.get("symbol") or c.get("symbol") or "")

        card = {
            "event": "launch.detected",
            "tier": tier,
            "confirmed": True,
            "plan": self._trade_plan(degen),
            "source": c.get("source"),
            "chain": chain,
            "mint": mint,
            "symbol": symbol,
            "name": entry.get("name") or c.get("name") or "",
            "url": entry.get("url") or c.get("url") or "",
            "usd_market_cap": float(c.get("usd_market_cap") or 0),
            "age_minutes": round((time.time() - entry["first_ts"]) / 60.0, 1),
            "entry": None,
            "note": "",
        }
        ev = c.get("confirm") or {}
        if ev:
            card["evidence"] = ev
            card["upside"] = runner.upside_band(card["usd_market_cap"])
        saf = c.get("safety")
        # Strict: the safety score is ALWAYS noted on a dropped coin.
        card["safety"] = (safety.verdict_line(saf) if saf
                          else "safety scan unavailable (no score)")
        card["safety_score"] = ((saf or {}).get("score")
                               if isinstance(saf, dict) else None)
        # Unvetted confirmed lane (user decision 2026-09-28): the chart
        # proof triggered this alert with no safety report — the renderer
        # must show the UNVETTED label, never imply a score exists.
        card["unvetted"] = bool(c.get("unvetted_confirmed"))
        card["narratives"] = c.get("narratives") or ["other"]
        card["narrative_heat"] = c.get("narrative_heat") or 1.0
        card["sentiment"] = (c.get("sentiment") or {}).get("verdict")
        smart_buys = int(c.get("smart_buys") or 0)
        if smart_buys:
            card["smart_money"] = ("%d tracked wallet%s bought early" % (
                smart_buys, "" if smart_buys == 1 else "s"))
        # Winner-likeness ranker (idea #5, user decision 2026-10-01):
        # every confirmed runner is scored 0-100 against the bot's own
        # 10x+ history and the score rides on the card. The
        # winner_min_score knob (default 0 = OFF) can hold low-scoring
        # runners out of the top tier — but only after the user
        # approves a threshold via /tune. Hard safety gates above are
        # never affected.
        wscore, wfactors, wok = self._winner_score(
            config, state, c, mint or "", label="launch")
        card["winner_score"] = wscore
        if wfactors:
            card["winner_factors"] = "; ".join(wfactors)
        if self._winner_hold_check(degen, state, mint or "", symbol,
                                   wscore, wok, "launch", time.time()):
            return
        # Holder-quality ranker (idea #1, user decision 2026-10-01):
        # scores whether holder distribution broadens with the price
        # or the mcap is sprinting while holders stall (distribution
        # into exits). Display-only — the score rides on the card and
        # never gates the alert. Unknown data scores neutral 50.
        try:
            _mg = float((ev or {}).get("gain_mult") or 0)
            mg = (_mg - 1.0) * 100.0 if _mg > 0 else None
        except (TypeError, ValueError):
            mg = None
        hscore, hfactors = self._holder_score(state, c, mint or "", mg)
        card["holder_score"] = hscore
        if hfactors:
            card["holder_factors"] = "; ".join(hfactors)
        # Top Picks shortlist (user decision 2026-10-01): snapshot every
        # signal at confirmed-card time so the daily ranking can pick
        # the best 5. Only alerted runners are recorded (the hold-check
        # above already returned for held ones). Never gates anything.
        try:
            from daemon.launch_scanner import toppicks  # noqa: PLC0415
            sres = self._sniper_rank(config, state, c, entry, c)
            fp = (state.get("fingerprints") or {}).get(mint or "") or {}
            toppicks.record_scores(state, mint or "", {
                "symbol": symbol, "chain": chain,
                "sniper": float(sres.get("score") or 0),
                "winner": float(wscore or 0),
                "holder": float(hscore or 0),
                "smart_buys": int(c.get("smart_buys") or 0),
                "fp": str(fp.get("verdict") or ""),
                "mcap": card["usd_market_cap"],
            }, now=time.time())
        except Exception:
            logger.exception("toppicks record failed for %s", mint)
        # Proof-of-demand checks (user decision 2026-10-02): measure the
        # 3 checks (LP lock, holder concentration, distribution) for
        # every scored runner. MEASUREMENT ONLY — the results are
        # recorded in state["proof_checks"] and never gate anything.
        # After ~1 week the ledger answers whether any check becomes a
        # real gate; nothing self-activates. Skipped under unit tests
        # (hermetic: no live HTTP from the suite).
        try:
            from daemon.launch_scanner import proof  # noqa: PLC0415
            if not _testing():
                proof.measure(state, degen, mint or "", chain,
                              now=time.time())
        except Exception:
            logger.exception("proof measure failed for %s", mint)
        # Footprint signals (user decision 2026-10-02): measure holder
        # velocity (crowd arriving?) and accumulation (volume spiking
        # while price flat?) for every scored runner, from the
        # fingerprint time series. MEASUREMENT ONLY — recorded in
        # state["footprint_signals"], never gates anything. Skipped
        # under unit tests (hermetic: no live state from the suite).
        try:
            from daemon.launch_scanner import footprints  # noqa: PLC0415
            if not _testing():
                _tr = getattr(self, "_fp_tracker", None)
                footprints.measure(
                    state,
                    getattr(_tr, "_series", {}) if _tr else {},
                    degen, mint or "", now=time.time())
        except Exception:
            logger.exception("footprints measure failed for %s", mint)

        entered = False
        if mint and chain == "solana" and getattr(degen, "auto_enter", True):
            # Register with the broker + H1 watchlist first (idempotent).
            try:
                from daemon.telegram import watchlist  # noqa: PLC0415
                ok, msg = watchlist.add_to_watchlist(symbol, mint)
                logger.warning("launch watchlist %s: %s", symbol, msg)
            except Exception:
                logger.exception("watchlist add failed for %s", symbol)
            if count_base_positions(state["degen_positions"]) >= int(degen.max_positions):
                card["note"] = "max degen positions reached — alert only"
            else:
                entered = self._paper_enter(
                    config, adapter, state, card, degen, mint, symbol,
                    intel={"sentiment": c.get("sentiment"),
                           "safety": c.get("safety")})
                if not entered and "no live quote" in (card.get("note") or ""):
                    # Quote not indexed yet — retry on later cycles while
                    # the launch is still fresh.
                    state.setdefault("pending_entry", {})[mint] = {
                        "symbol": symbol, "qualify_ts": time.time(),
                        "tier": tier,
                    }

        if not entered and not card["note"]:
            if chain != "solana":
                card["note"] = "alert only — paper engine prices Solana"
            elif not getattr(degen, "auto_enter", True):
                card["note"] = "auto_enter off — alert only"

        milestones.track_signal(state, degen, card)
        # Entry timing (measurement only): confirmed card — the first card
        # wins, so an earlier instant/digest card keeps its latency.
        card["latency_min"] = latency.record_first_card(
            state, degen, mint, symbol=symbol, source="launch.detected")
        event_bus.publish(card)
        logger.warning("LAUNCH [%s] %s %s mcap=$%.0f",
                       tier, symbol, mint, card["usd_market_cap"])
        # Sustained-velocity tier (idea #4, user decision 2026-10-01):
        # seed the delayed re-check. The first alert above is unchanged;
        # sustained_delay_min later a still-running coin earns ONE louder
        # top-tier card. Fading/dead coins stay silent (logged only).
        if mint and getattr(degen, "sustained_enabled", True):
            try:
                from daemon.launch_scanner import sustained  # noqa: PLC0415
                sustained.seed_watch(state, mint, symbol, now=time.time())
                rec = (state.get("sustained_watch") or {}).get(mint)
                if isinstance(rec, dict):
                    rec["chain"] = chain
                    rec["card_mcap"] = card["usd_market_cap"]
            except Exception:
                logger.exception("sustained seed failed for %s", mint)

    def _sustained_poll(self, config, state: Dict[str, Any], now: float
                        ) -> None:
        """Delayed re-check of confirmed runners (idea #4).

        For each coin seeded at alert time whose delay has elapsed:
        hold-vs-peak comes from the milestone record (anchor x
        peak_mult vs mcap_now — the true peak, not a short window);
        the recent trend comes from the fingerprint series. The coin is
        classified via sustained.classify and marked checked (fires
        once). SUSTAINED publishes one ``launch.sustained`` top-tier
        card; FADING/DEAD are logged only — never spam. Never raises.
        """
        from agent import events as event_bus  # noqa: PLC0415
        from daemon.launch_scanner import sustained  # noqa: PLC0415
        degen = config.degen
        if not getattr(degen, "sustained_enabled", True):
            return
        watch = state.get("sustained_watch")
        if not isinstance(watch, dict) or not watch:
            return
        peak_hold = float(
            getattr(degen, "sustained_peak_hold_pct", 70.0) or 70.0)
        max_dd = float(
            getattr(degen, "sustained_max_drawdown_pct", 30.0) or 30.0)
        lookback = float(
            getattr(degen, "sustained_lookback_min", 15.0) or 15.0)
        mw = milestones.tracked(state) or {}
        tr = getattr(self, "_fp_tracker", None)
        series_map = getattr(tr, "_series", {}) if tr else {}
        for mint in list(watch):
            rec = watch.get(mint)
            if not isinstance(rec, dict) or rec.get("checked"):
                continue
            if not sustained.due_for_check(state, mint, degen, now):
                continue
            mrec = mw.get(mint) or {}
            anchor = float(mrec.get("anchor") or rec.get("card_mcap") or 0)
            peak_mult = float(mrec.get("peak_mult") or 0)
            mcap_now = float(mrec.get("mcap_now") or 0)
            symbol = (rec.get("symbol") or mrec.get("symbol")
                      or (mint or "")[:8])
            if anchor <= 0 or peak_mult <= 0 or mcap_now <= 0:
                # No milestone data (e.g. milestones disabled) — mark
                # checked so it never retries; nothing to classify.
                sustained.mark_checked(state, mint)
                logger.warning("SUSTAINED-SKIP %s: no milestone data",
                               symbol)
                continue
            peak_mcap = anchor * peak_mult
            hold_pct = mcap_now / peak_mcap * 100.0
            drawdown_pct = (peak_mcap - mcap_now) / peak_mcap * 100.0
            # Recent trend from the fingerprint series (last ~6 polls,
            # obs tuples (ts, mcap, ...)); 0 when too short.
            trend_pct = 0.0
            try:
                obs = series_map.get(mint) or []
                pts = [(float(o[0]), float(o[1])) for o in obs
                       if len(o) > 1 and float(o[1]) > 0]
                if len(pts) >= 2:
                    cutoff = now - lookback * 60.0
                    base = next((m for t, m in pts if t >= cutoff),
                                pts[0][1])
                    if base > 0:
                        trend_pct = ((pts[-1][1] - base) / base * 100.0)
            except Exception:
                trend_pct = 0.0
            verdict, reason = sustained.classify(
                hold_pct, trend_pct, drawdown_pct, peak_hold, max_dd)
            sustained.mark_checked(state, mint)
            logger.warning("SUSTAINED-CHECK %s %s: %s — %s",
                           symbol, mint, verdict, reason)
            if verdict != "SUSTAINED":
                continue  # FADING/DEAD stay silent — measurement only
            mins = int((now - float(rec.get("confirm_ts") or now)) / 60.0)
            event_bus.publish({
                "event": "launch.sustained",
                "symbol": symbol,
                "mint": mint,
                "chain": rec.get("chain") or mrec.get("chain") or "solana",
                "name": mrec.get("name") or "",
                "url": mrec.get("url") or "",
                "usd_market_cap": mcap_now,
                "verdict": verdict,
                "hold_pct": round(hold_pct, 1),
                "trend_pct": round(trend_pct, 1),
                "drawdown_pct": round(drawdown_pct, 1),
                "peak_mult": round(peak_mult, 2),
                "minutes_since_card": round(
                    (now - float(rec.get("confirm_ts") or now)) / 60.0, 1),
                "plan": self._trade_plan(degen),
                "note": ("Still holding %.0f%% of its peak %d min after "
                         "the alert — top-tier runner." % (hold_pct, mins)),
            })

    @staticmethod
    def _leg_pos_dict(ticket: int, symbol: str, mint: str, wave: int,
                      tag: str, entry_price: float, tier: str, sl: float,
                      tp: Optional[float], volume_sol: float,
                      max_hold_hours: float, leg: Optional[str],
                      card: Dict[str, Any], intel: Dict[str, Any],
                      saf: Dict[str, Any],
                      deployer: Optional[str]) -> Dict[str, Any]:
        """One leg's position record. ``leg`` is "A"/"B" for two-tier
        entries, None for legacy single-leg positions. Leg B carries
        ``tp=None`` (no fixed take-profit) and the longer max hold."""
        return {
            "ticket": ticket,
            "symbol": symbol,
            "mint": mint,
            "wave": wave,
            "wave_number": wave,
            "label": tag,
            "leg": leg,
            "entry_price_sol": float(entry_price),
            "peak_price_sol": float(entry_price),
            "entry_ts": time.time(),
            "tier": tier,
            "sl": sl,
            "tp": tp,
            "volume_sol": float(volume_sol),
            "max_hold_hours": float(max_hold_hours),
            # Intel at entry — for the real-outcome backtest record.
            "source": card.get("source") or "",
            "narratives": card.get("narratives") or ["other"],
            "narrative_heat": card.get("narrative_heat") or 1.0,
            "sentiment": intel.get("sentiment"),
            "sentiment_verdict": card.get("sentiment"),
            # Sniper score at entry — feeds the #6 feedback loop's
            # threshold analysis. None for non-sniper lanes.
            "sniper_score": card.get("sniper"),
            # Entry-signal breakdown — feeds the #6 entry learner
            # (which signals actually predicted runners). None for
            # lanes that never ran the sniper ranker.
            "entry_signals": card.get("sniper_signals"),
            "safety_score": saf.get("score"),
            "safety_fails": saf.get("fails") or [],
            "deployer": deployer,
        }

    def _paper_enter(self, config, adapter, state: Dict[str, Any],
                     card: Dict[str, Any], degen, mint: str,
                     symbol: str,
                     intel: Optional[Dict[str, Any]] = None,
                     wave: int = 1, tag: Optional[str] = None) -> bool:
        """Paper-enter one confirmed runner.

        ``intel`` (full sentiment dict + raw safety dict) is stashed on
        the position record so real paper closes can be written to the
        backtest dataset with true entry/exit outcomes. The card only
        carries the verdict strings, per the event schema.

        ``wave`` labels the entry: 1 = first-wave entry (default, the
        original card), >= 2 = second-wave re-entry. Second-wave
        positions are keyed separately (``second_wave:<mint>``) so a
        re-entry can coexist with the first-wave position, and the
        journaled wave number lets Sunday's review grade first-wave
        vs second-wave expectancy separately.

        ``tag`` overrides the position label explicitly (e.g. the
        slow-cooker passes ``tag="late_runner"``); when omitted it is
        derived from ``wave`` exactly as before, so all existing
        callers are unaffected.
        """
        from broker import OrderRequest  # noqa: PLC0415
        from core.execution.broker_guard import execution_scope  # noqa: PLC0415

        price_fn = getattr(adapter, "_price_sol_quiet", None)
        entry_price = price_fn(symbol) if price_fn else None
        if not entry_price or entry_price <= 0:
            card["note"] = "no live quote yet — alert only, will retry"
            return False
        # Daily loss breaker: pause NEW entries when today's realized
        # degen P&L is worse than -daily_max_loss_sol. Alerts still fire;
        # only fresh entries stop. Fails open if the ledger can't be read.
        try:
            from datetime import datetime, timezone
            from storage.store import DEFAULT_DB_PATH
            day_start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp()
            closes = scoreboard.load_degen_closes(str(DEFAULT_DB_PATH))
            if scoreboard.daily_pnl_sol(closes, day_start) <= -float(
                    degen.daily_max_loss_sol):
                logger.warning("ENTRY PAUSED (daily loss breaker) %s %s",
                               symbol, mint)
                return False
        except Exception:
            logger.exception("daily loss breaker read failed; allowing entry")
        sl = entry_price * (1 - float(degen.stop_loss_pct) / 100.0)
        tp = entry_price * (1 + float(degen.take_profit_pct) / 100.0)
        w = max(1, int(wave or 1))
        tag = tag or ("launch" if w == 1 else "second_wave")
        comment = {"launch": "degen-launch",
                   "second_wave": "degen-second-wave"}.get(
            tag, "degen-" + str(tag).replace("_", "-"))
        # Two-tier exits: the entry is split into two half-size legs.
        # Leg A keeps the classic exits (stop / fixed TP / 4h hold /
        # trail); leg B (runner) skips the fixed TP and holds up to
        # tier_b_max_hold_hours with the same stop and trail. With
        # two_tier_enabled=false this is exactly one full-size leg.
        two_tier = bool(getattr(degen, "two_tier_enabled", True))
        full_vol = float(degen.entry_sol)
        leg_vol = full_vol / 2.0 if two_tier else full_vol
        req = OrderRequest(
            symbol=symbol, direction="BUY",
            volume=leg_vol,
            stop_loss=sl, take_profit=tp,
            signal_id="%s:%s" % (tag, mint),
            idempotency_key="%s:%s" % (tag, mint),
            comment=comment,
        )
        try:
            with execution_scope():
                result = adapter.submit_order(req)
        except Exception as exc:
            logger.warning("degen paper entry rejected for %s: %s", symbol, exc)
            card["note"] = "paper entry rejected: %s" % exc
            return False
        intel = intel or {}
        saf = intel.get("safety") or {}
        pos_key = mint if tag == "launch" else "%s:%s" % (tag, mint)
        # Deployer (#2): resolved lazily at the early-card stage — ride it
        # along on the position so paper closes can grade the deployer
        # registry without any RPC at close time.
        try:
            _pos_deployer = ((state.get("deployer_cache") or {}).get(
                mint) or {}).get("deployer")
        except Exception:
            _pos_deployer = None
        _leg_kw = dict(symbol=symbol, mint=mint, wave=w, tag=tag,
                       tier=card.get("tier") or "?", sl=sl, card=card,
                       intel=intel, saf=saf, deployer=_pos_deployer)
        state["degen_positions"][pos_key] = self._leg_pos_dict(
            ticket=result.ticket, entry_price=float(result.price),
            tp=tp, volume_sol=leg_vol,
            max_hold_hours=float(degen.max_hold_hours),
            leg="A" if two_tier else None, **_leg_kw)
        # Leg B is written to state right after leg A, so a crash between
        # the two can only strand leg A — which then behaves exactly like
        # the old single-position code. If leg B's submit fails, leg A
        # keeps running alone (graceful degradation, same as before).
        ticket_b = None
        if two_tier:
            try:
                with execution_scope():
                    result_b = adapter.submit_order(OrderRequest(
                        symbol=symbol, direction="BUY",
                        volume=leg_vol,
                        # The paper broker never enforces TP itself; the
                        # engine does via the position record below, where
                        # leg B carries tp=None (no fixed take-profit).
                        stop_loss=sl, take_profit=tp,
                        signal_id="%s:%s:B" % (tag, mint),
                        idempotency_key="%s:%s:B" % (tag, mint),
                        comment="%s-B" % comment,
                    ))
                state["degen_positions"][pos_key + LEG_B_SUFFIX] = \
                    self._leg_pos_dict(
                        ticket=result_b.ticket,
                        entry_price=float(result_b.price),
                        tp=None, volume_sol=leg_vol,
                        max_hold_hours=float(
                            getattr(degen, "tier_b_max_hold_hours", 24.0)
                            or 24.0),
                        leg="B", **_leg_kw)
                ticket_b = result_b.ticket
            except Exception as exc:
                logger.warning("degen leg-B entry failed for %s "
                               "(leg A live): %s", symbol, exc)
                card["note"] = ((card.get("note") or "")
                                + " runner leg failed — single leg active"
                                ).strip()
        card["entry"] = {
            "ticket": result.ticket,
            "ticket_b": ticket_b,
            "volume_sol": full_vol,
            "price_sol": float(result.price),
            "stop_loss_pct": float(degen.stop_loss_pct),
            "take_profit_pct": float(degen.take_profit_pct),
            "two_tier": two_tier,
        }
        return True

    # -- degen exits ----------------------------------------------------
    def _retry_pending(self, config, adapter,
                       state: Dict[str, Any]) -> None:
        """Retry paper entry for qualified launches whose quote wasn't
        indexed yet. Gives up after 30 min — no longer an early entry."""
        from agent import events as event_bus  # noqa: PLC0415

        degen = config.degen
        pending = state.get("pending_entry") or {}
        if not pending or not getattr(degen, "auto_enter", True):
            return
        now = time.time()
        for mint in list(pending):
            item = pending[mint]
            if now - float(item.get("qualify_ts", now)) > 1800:
                pending.pop(mint, None)
                continue
            if count_base_positions(state.get("degen_positions")) >= \
                    int(degen.max_positions):
                continue
            card = {"entry": None, "note": "", "tier": item.get("tier") or "?"}
            # Restore whatever intel we have: tags were saved at candidate
            # creation, sentiment verdicts refresh per judge cycle.
            _ntags = state.setdefault("narrative_tags", {})
            _sents = state.setdefault("sentiment", {})
            card["narratives"] = _ntags.get(mint) or ["other"]
            _sv = (_sents.get(mint) or {}).get("verdict_info") or {}
            card["sentiment"] = _sv.get("verdict")
            _rintel = {"sentiment": (_sents.get(mint) or {}).get(
                "verdict_info"),
                       "safety": None}
            if self._paper_enter(config, adapter, state, card, degen,
                                 mint, item["symbol"], intel=_rintel):
                pending.pop(mint, None)
                en = card["entry"] or {}
                event_bus.publish({
                    "event": "trade.executed",
                    "symbol": item["symbol"],
                    "ticket": en.get("ticket"),
                    "side": "BUY",
                    "volume": en.get("volume_sol"),
                    "price": en.get("price_sol"),
                    "signal_id": "launch:%s" % mint,
                })
                logger.warning("DEGEN delayed entry %s #%s",
                               item["symbol"], en.get("ticket"))

    def _grade_deployer_close(self, state: Dict[str, Any], degen,
                              mint: str, entry_px: float, close_px: float,
                              peak: float, pos: Dict[str, Any]) -> None:
        """Grade one closed paper position into the deployer registry.

        Outcome: ``"rug"`` when the realized multiple is <= 0.10
        (liquidity-pull shape — same cutoff as the backtest recorder),
        ``"hit_2x"`` when the peak multiple of entry reached 2.0, else
        ``"dead"``. Runs once per mint (guarded by
        ``state["deployer_outcome_recorded"]``) so the milestone-expiry
        grade never double-counts the same launch. Never raises.
        """
        try:
            if not getattr(degen, "deployer_tracking_enabled", True):
                return
            if not mint:
                return
            recorded = state.setdefault("deployer_outcome_recorded", {})
            if recorded.get(mint):
                return  # already graded (close or expiry) — no double count
            deployer = (pos or {}).get("deployer") or (
                (state.get("deployer_cache") or {}).get(mint) or {}
            ).get("deployer")
            if not deployer:
                return
            realized = (close_px / entry_px) if entry_px > 0 else 1.0
            peak_mult = (peak / entry_px) if entry_px > 0 else 1.0
            if realized <= 0.10:
                outcome = "rug"
            elif peak_mult >= 2.0:
                outcome = "hit_2x"
            else:
                outcome = "dead"
            if deployers.record_outcome(mint, deployer, outcome):
                recorded[mint] = {"ts": time.time(), "outcome": outcome}
                logger.warning("DEPLOYER graded %s -> %s (%s…)", mint,
                               outcome, str(deployer)[:8])
        except Exception:
            logger.warning("deployer close grade failed for %s", mint,
                           exc_info=True)

    def _manage_exits(self, config, adapter,
                      state: Dict[str, Any]) -> None:
        from agent import events as event_bus  # noqa: PLC0415
        from core.execution.broker_guard import execution_scope  # noqa: PLC0415

        degen = config.degen
        positions = state.get("degen_positions") or {}
        if not positions:
            return
        price_fn = getattr(adapter, "_price_sol_quiet", None)
        now = time.time()
        # Price pre-pass: a position with no quote is only a dead pool
        # (as opposed to a market-data outage) when other coins priced
        # fine in this same cycle — that distinction gates the dead-pool
        # time stop below.
        prices: Dict[str, Any] = {}
        for mint in list(positions):
            pos = positions[mint]
            symbol = pos.get("symbol", "")
            try:
                prices[mint] = price_fn(symbol) if price_fn else None
            except Exception:
                prices[mint] = None
        market_alive = any(p and p > 0 for p in prices.values())
        for mint in list(positions):
            pos = positions[mint]
            symbol = pos.get("symbol", "")
            # Position dict keys may carry a wave suffix
            # (``second_wave:<mint>``); downstream records key on the
            # real mint so second-wave closes don't fragment datasets.
            real_mint = pos.get("mint") or mint
            price = prices.get(mint)
            age_h = (now - float(pos.get("entry_ts", now))) / 3600.0
            max_hold_h = float(
                pos.get("max_hold_hours") or float(degen.max_hold_hours))
            dead_pool_close = False
            if not price or price <= 0:
                # Dead pool: the quote is gone. When the feed itself is
                # healthy (other coins priced this cycle) the pool is
                # specifically dead — honor the time stop at ~0 instead
                # of skipping the position forever.
                if market_alive and age_h >= max_hold_h:
                    dead_pool_close = True
                else:
                    continue
            entry_px = float(pos.get("entry_price_sol") or 0)
            peak = float(pos.get("peak_price_sol") or entry_px or 0)
            if price and price > peak:
                peak = price
                pos["peak_price_sol"] = peak
            reason = None
            if dead_pool_close:
                reason = "dead_pool"
            elif price <= float(pos.get("sl", 0)):
                reason = "stop_loss"
            elif pos.get("tp") is not None and price >= float(pos["tp"]):
                # Leg B (runner) carries tp=None: no fixed take-profit,
                # it exits on stop / trail / max hold only.
                reason = "take_profit"
            elif age_h >= max_hold_h:
                reason = "max_hold"
            else:
                # Two-stage trailing stop (trail_exit_reason): Leg A
                # banks profit early — once the peak hits
                # leg_a_trail_trigger_pct (+20%) a
                # leg_a_trail_drawdown_pct (15%) pullback locks the
                # profit in ("profit_lock") instead of round-tripping
                # to the stop. Past +50% the pre-existing 30% trail
                # governs ("trail_stop"), unchanged. Leg B keeps the
                # single pre-existing stage — the moon bag keeps full
                # room to run.
                reason = trail_exit_reason(
                    degen, pos.get("leg"), entry_px, peak, price)
            if not reason:
                continue
            ticket = pos.get("ticket")
            try:
                with execution_scope():
                    if dead_pool_close:
                        # No pool to sell into — confirmed worthless.
                        close_px = adapter.close_position_at(
                            ticket, 0.0, reason="dead_pool")
                    else:
                        close_px = adapter.close_position(ticket)
            except Exception as exc:
                logger.warning("degen close failed ticket %s: %s", ticket, exc)
                # Position may already be gone — drop it so we don't spin.
                if "no open paper position" in str(exc):
                    positions.pop(mint, None)
                continue
            entry_px = float(pos.get("entry_price_sol") or close_px)
            vol = float(pos.get("volume_sol") or degen.entry_sol)
            pnl = vol * (close_px / entry_px - 1) if entry_px > 0 else 0.0
            positions.pop(mint, None)
            # Score the smart wallets that bought this runner.
            try:
                smartmoney.record_outcome(
                    real_mint, pnl > 0,
                    list((state.get("smart_activity") or {}).get(
                        real_mint, [])))
            except Exception:
                logger.warning("smart-money scoring failed", exc_info=True)
            # Deployer grading (#2): "rug" when the realized multiple is
            # <= 0.10 (liquidity-pull shape — same cutoff as the backtest
            # recorder), "hit_2x" when the peak multiple reached 2.0, else
            # "dead". Once per mint (deployer_outcome_recorded) so the
            # milestone-expiry grade never double-counts the same launch.
            try:
                self._grade_deployer_close(state, degen, real_mint,
                                           entry_px, close_px, peak, pos)
            except Exception:
                logger.warning("deployer close grading failed",
                               exc_info=True)
            # Narrative heat: learn which narratives actually run from
            # closed paper positions (soft signal; never a gate).
            self._record_narrative_close(degen, state, real_mint, pnl)
            # Real-outcome backtest record: true entry/exit/peak, not a
            # synthesized judge outcome. This is what proves which
            # narratives and signals actually made money.
            self._backtest_record_paper_close(
                config, state, real_mint, symbol, pos, entry_px, close_px,
                reason, pnl, time.time())
            event_bus.publish({
                "event": "position.closed",
                "ticket": ticket,
                "symbol": symbol,
                "reason": "degen_%s" % reason,
                "degen_tier": pos.get("tier") or "?",
                "leg": pos.get("leg") or "A",
                "close_price": close_px,
                "profit": round(pnl, 6),
                "exit_version": EXIT_POLICY_VERSION,
            })
            logger.warning("DEGEN CLOSE %s #%s %s: %+.6f SOL",
                           symbol, ticket, reason, pnl)


def state_hl(state: Dict[str, Any]):
    return state.setdefault("hl_tokens", [])


def main_entry(argv=None):
    return main(lambda: LaunchScanner(), argv)


if __name__ == "__main__":
    sys.exit(main_entry(sys.argv[1:]))
