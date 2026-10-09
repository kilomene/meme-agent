# Memecoin provider — runtime contract

`broker/memecoin/` is the multi-chain memecoin broker backend. It is
**self-contained**: the paper trading engine, fill journal, idempotency
store and market-data transports all live in this package. There is no
external bot dependency, and private keys are only ever read at runtime
from the operator's own machine (see WALLET_SECURITY.md) — the wallet
secret must never leave the operator's custody.

## Modes

|                | paper (default) | devnet (testing) | live |
|----------------|-----------------|------------------|------|
| Selected by    | `mode: dry_run` (or `DRY_RUN=true`) | `MEMECOIN_DEVNET=true` (+ `MEMECOIN_DEVNET_RPC_URL`, `MEMECOIN_WALLET_PUBKEY`) | `mode: live` (`DRY_RUN=false` / `MODE=live`) |
| `connect()`    | always succeeds, no credentials | connects to **devnet only**; verifies expected pubkey | **always raises** `BROKER_UNAVAILABLE` — fail-closed by design |
| `submit_order` | virtual fill at the live public quote | real devnet SOL self-transfer + paper ledger; every fill carries its devnet tx signature | unreachable (connect refuses first); raises `BROKER_UNAVAILABLE` defensively |
| Balance        | virtual SOL, default 10.0 (`memecoin.paper_start_sol`) | real devnet SOL balance (read-only) + virtual ledger | n/a |

Live is intentionally non-functional: there is deliberately no execution
path (no transaction signing, no DEX wiring). Until a real, reviewed
execution path exists, live requests fail loudly instead of silently
doing nothing.

### Devnet mode (testing only)

`MEMECOIN_DEVNET=true` selects the devnet execution path — the only
mode that signs and sends real (devnet) transactions. Design:

* **Devnet-pinned RPC.** `DevnetRPC` refuses any host that is not
  `*.devnet.solana.com`. There is no code path to mainnet.
* **Expected-pubkey check.** `connect()` verifies the loaded keypair's
  pubkey matches `MEMECOIN_WALLET_PUBKEY`; mismatch fails closed.
* **Per-fill self-transfer.** Each order performs a real devnet SOL
  self-transfer equal to the order's SOL volume (proving sign → send →
  confirm), while token quantities are tracked in the paper ledger.
  Every fill carries its real devnet transaction signature.
* **No DEX swaps on devnet.** There is no memecoin DEX liquidity on
  devnet; the self-transfer proves the plumbing, not swap correctness.
* **Jupiter client is future-only.** `execution/jupiter.py` targets the
  mainnet Jupiter v6 API and is deliberately not wired into the devnet
  path — live/mainnet remains a separate deliberate gate.

Manual smoke test: `scripts/devnet-smoke` (requires devnet RPC egress
and a funded devnet keypair; the Hatch sandbox VM's egress proxy
blackholes `api.devnet.solana.com` specifically — verified 2026-09-22:
other hosts return 200, devnet RPC hangs and times out, no approval
pending — so run the smoke test from a machine with direct network
access, e.g. the future VPS).

### Live prerequisites (necessary, not sufficient)

1. `mode: live`
2. `MEMECOIN_LIVE_ARMED=true`
3. `MEMECOIN_WALLET_PUBKEY` set (public key **only**)
4. Kill-switch file absent

Missing 2–3 raises `CONFIG_INVALID` at `connect()`; with 2–4 satisfied,
`connect()` still raises `BROKER_UNAVAILABLE` (`live_not_implemented`).

## Paper trading model

* **Spot only.** `BUY` spends SOL to open a token position; `SELL`
  reduces open long exposure FIFO; a naked `SELL` (no holdings) is
  rejected with `INVALID_ORDER` — shorting is unsupported.
* **`volume` is denominated in SOL** on both sides (BUY: SOL to spend;
  SELL: SOL worth of tokens to sell).
* **Fills are priced from live public quotes** (DexScreener token price
  → SOL via the SOL/USD leg). If market data is unreachable, the fill is
  **refused** with `BROKER_UNAVAILABLE` — paper fills are never priced
  from stale or invented data.
* Every fill is appended to `fills.jsonl`; `deal_history()` reads back
  from that journal.
* `account_info()`: `balance` = virtual SOL (realized),
  `equity` = balance + unrealized P/L at live quotes, `currency` = "SOL".
* `orders()` is always empty — the paper book has no pending orders;
  fills are immediate.
* `modify_order()` adjusts SL/TP on a paper position (stop triggering is
  not simulated; the exit manager closes positions via `close_position`).
* `close_position()` sells the whole position at the live quote and
  returns the close price in SOL.

## Idempotency

`submit_order` with an `idempotency_key` stores the resulting
`OrderResult` in `idempotency.json`. A repeated key **replays the
original result** — the position is never opened twice and the balance
is never charged twice. Keys are scoped to the data dir.

## Kill-switch file

If `<data_dir>/killswitch` exists, `submit_order` raises
`BROKER_UNAVAILABLE` (`detail.reason = "kill_switch_engaged"`).
Risk-reducing `close_position` calls are **not** blocked — an emergency
brake must never trap open positions (core `KillSwitch.close_all()`
relies on this). This is defense-in-depth alongside the core kill switch
(`core.execution.kill_switch`, which the gateway checks first): an
operator can halt even paper trading by touching one file, no restart
needed. `broker_status()` reports it as `detail.kill_switch =
"engaged (killswitch file present)"` (key absent when clear) and drops
`trading_available` to false.

## State directory

Default: `$FOREX_AGENT_HOME/data/memecoin` (override:
`memecoin.data_dir` / `MEMECOIN_DATA_DIR`).

| file             | contents |
|------------------|----------|
| `paper_state.json` | virtual balance, open positions, ticket counter (atomic rewrite) |
| `fills.jsonl`      | every paper fill, one JSON object per line (append-only) |
| `idempotency.json` | idempotency key → `OrderResult` |
| `killswitch`       | presence = engaged |

## Market data (transport.py)

Keyless public endpoints over stdlib `urllib` (no new dependencies):

* **Quotes:** DexScreener `GET /latest/dex/tokens/{mint}` → top Solana
  pair by liquidity → `priceUsd`, converted to SOL via the SOL/USD leg.
* **Candles:** GeckoTerminal
  `GET /api/v2/networks/solana/pools/{pool}/ohlcv/{minute|hour|day}`.
  Timeframes map M1/M5/M15 → minute, M30/H1/H4 → hour, D1 → day.
  The forming candle is dropped (closed-candle contract, same as live).

Any fetch failure degrades to `None` / `BROKER_UNAVAILABLE` with
`detail.reason = "market_data_unreachable"` — never a fake price.

## `broker_status()` fields

* `provider`: `"memecoin"`, `mode`: `"paper"` | `"live"`
* `configured`: a provider is selected (always true here)
* `reachable`: the paper engine answers (local — always true in paper)
* `connected`: `connect()` was called (paper)
* `account_available`: virtual account readable (paper: true)
* `market_data_available`: a live SOL quote probe succeeded
* `trading_available`: paper fills allowed (false when the kill-switch
  file is engaged, or in live mode)
* `detail`: `balance_sol`, `open_positions`, and human-readable reasons
  whenever a flag is degraded

## Symbols

`memecoin.symbols` maps `SYMBOL: mint` (e.g. `BONK:
DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263`). `SOL` is always
registered (`So11111...11112`). Unknown symbols raise `INVALID_SYMBOL`.

## Multi-chain

`memecoin.chains` (env `MEMECOIN_CHAINS`, default `["solana"]`) enables
chains from the registry (`broker/memecoin/chains/registry.py`, 20+
chains: ethereum, base, arbitrum, optimism, polygon, bnb_chain,
avalanche, linea, blast, zksync, scroll, mantle, berachain, sei, monad,
hyperevm, unichain, ink, soneium, solana, hyperliquid, sui, aptos).
Unknown names are rejected at config load; chains tracked without an
implemented adapter (sui, aptos) raise a clear "not implemented" error.

* **`solana`** is this adapter's native engine (SOL-settled, described
  above). `submit_order(req)` — or `chain="solana"` — takes the native
  path; `req.volume` is denominated in SOL.
* **Every other chain** is served by a `ChainAdapter`
  (`broker/memecoin/chains/`): a USDC-settled spot paper book with
  per-chain state under `<data_dir>/chains/<chain>/` (`paper_state.json`,
  `fills.jsonl`, `idempotency.json`). `submit_order(req, chain="base")`
  routes there; `req.volume` is interpreted as **USDC** on chain routes.
  BUY spends USDC at the public USD quote; SELL reduces holdings
  (no shorts); a missing quote refuses the fill (`BROKER_UNAVAILABLE`).
* EVM chains share one generic adapter (per-chain config: chain id,
  native currency, RPC env var); Solana reuses the DexScreener
  transport; Hyperliquid uses its public `allMids` endpoint for USD
  mids. All transports are stdlib-only with graceful degradation.
* The `<data_dir>/killswitch` file is global: it blocks swaps on every
  chain at once. `chain_status()` returns per-chain fragments;
  `broker_status()` reports the enabled chains in `detail.chains`.

Adding a chain is a one-line `ChainDef` in the registry — no code
changes.

## Wallet key pattern

Paper mode needs no key (`NullKeyProvider` default). For the future
live path, `memecoin.wallet` selects where a wallet secret is read from
**at runtime, on the operator's own machine**:

| `key_source` | behavior |
|---|---|
| `none` (default) | no key is read |
| `env` | `MEMECOIN_PRIVATE_KEY_<CHAIN>` (e.g. `MEMECOIN_PRIVATE_KEY_BASE`), fallback `MEMECOIN_PRIVATE_KEY` |
| `file` | `memecoin.wallet.key_file` (`MEMECOIN_WALLET_KEY_FILE`); the file must be a regular file owned by the operator with mode `0600` or stricter — anything group/world-readable is **refused** |

Enforced in code: key material never appears in logs, exceptions, or
`repr` (tested); the key is consulted transiently and never stored.
Live chain swaps require an armed key **and then still fail closed**
(`BROKER_UNAVAILABLE`, `live_not_implemented`) — the checks exist so
misconfiguration is reported honestly, but there is no execution path
at this stage. Operator guide: `broker/memecoin/WALLET_SECURITY.md`.
