# Sniper threshold calibration — 33-coin validated subset (2026-09-28)

## What was attempted
Reconstruct the sniper input features at card time for the 33 validated
trades (6 TP winners ≥3x: TOLY, INVESTUSA, BARRON, AROS, GROK2, HOOD;
27 duds ≤1.05x) and check whether any round threshold separates them.

## What was honestly recoverable (real data, no fabrication)

| Feature (weight) | Source | Recoverable? | Result on n=33 |
|---|---|---|---|
| buy_pressure (10) | backtest_dataset.jsonl first snapshot | YES — snapshots land within ~1 min of entry (card time) | winners 0.70–0.88, duds 0.61–0.89 — **complete overlap** |
| smart_buys (35) | smart_wallet_grades.json, buy_ts ≤ entry+1h | YES | **0 for all 33** — neither tracked wallet bought any of these coins near entry |
| rising_polls (20) | snapshot series | NO — 1 snapshot/coin, can't compute a streak | 0 (as live ranker treats missing) |
| fp_composite (30) | fingerprint tracker | NO — computed live, never persisted | None → 0 (as live) |
| hot_deployer (5) | deployer cache | NO — mint→deployer map lives only in daemon memory | False → 0 (as live) |

Notable: the worst dud (ANTHROPIC, 0.00x realized) had the HIGHEST
card-time buy pressure (0.89). Pressure alone anti-predicts here.

## Score distribution (default weights, missing signals = 0, as live)
- Range 6.1–8.9 across all 33. Winners scattered through the pack
  (AROS 8.8 ranks #2, GROK2 7.0 ranks #21 of 33).
- No threshold separates winners from duds: at 30/40/50/60 the result
  is identically 0/6 winners tier-1 and 0/27 duds tier-1.

## Conclusion
**This subset cannot validate the threshold.** 85 of the 100 points
(fingerprint 30 + smart-wallet 35 + velocity 20) come from signals that
were never persisted historically. The reconstructible 15 points show
no separation, and tuning weights/threshold to force separation on
this data would be overfitting to noise.

## Decision
Keep the round default **sniper_tier_threshold = 50.0**. Validate
FORWARD, not backward: the live ranker now scores every quality-passed
coin with all five signals; join sniper scores with realized outcomes
over the next closes and revisit the threshold with real data.

## Reproducibility
- Script: /tmp/calib_sniper.py (uses /tmp/calib_dataset.jsonl, a frozen
  copy of ~/.forex-agent/launch_scanner/backtest_dataset.jsonl —
  the live file is appended to by the running daemon and contains
  duplicate mint lines; richest-snapshot entry kept per mint)
- Per-coin rows: /tmp/calib_rows.json
- Mints: /tmp/calib_mints.json (matched from fills.jsonl by symbol+time)
