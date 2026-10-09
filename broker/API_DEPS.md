# broker/ API_DEPS — cross-area imports owned by CORE-BUILDER

The broker package is the ONLY place that touches market/execution
backends. Providers: `memecoin` (default, `broker/memecoin/` —
self-contained paper engine + keyless public market data) and
`disconnected` (analysis-only). No third-party broker or exchange SDK
is imported anywhere in this repo — the memecoin provider uses stdlib
`urllib` only. Everything outside `broker/` sees only the
`BrokerAdapter` interface.

## The no-broker-SDK rule

No broker/exchange SDK is imported anywhere.
Verified by: `tests/test_iface_tools.py::TestNoBrokerSdk`, which
allow-lists every import in the repo (stdlib + first-party + known
non-broker dependencies).

## Imports INTO broker/

| Imported by | From | Names | Contract |
|---|---|---|---|
| `broker/memecoin/adapter.py` | `broker` (base) | `BrokerAdapter`, `AccountInfo`, `BrokerError`, `BrokerHealth`, `Deal`, `Order`, `OrderRequest`, `OrderResult`, `Position`, `Quote`, `SymbolSpec`, `MAGIC_NUMBER`, `extract_signal_id_from_comment`, error codes | Base dataclasses + interface defined in `broker/__init__.py` |
| `broker/memecoin/adapter.py` | `broker.memecoin.transport` | `MemecoinMarketData` | Keyless public market data (DexScreener quotes, GeckoTerminal candles); stdlib `urllib` only |
| `broker/memecoin/transport.py` | `broker` (base) | `BROKER_UNAVAILABLE`, `BrokerError` | Structured errors only |
| `broker/memecoin/transport.py` | `core.market` | `Candle` | `Candle(time, open, high, low, close, volume)` — candles returned oldest→newest, closed candles |
| `broker/disconnected.py` | `broker` (base) | `BrokerAdapter`, `BrokerError`, `BROKER_UNAVAILABLE` | Every operation raises `BrokerError(BROKER_UNAVAILABLE, ...)` |

`core.market` is imported by `broker/memecoin/transport.py` only so
candles return the same `Candle` type the rest of core consumes.
Direction of dependency: broker → core.market (types only, no logic).

## What broker provides to others

`broker/__init__.py` exports the full surface; consumers import from
`broker`, never from `broker.memecoin` directly (except
`agent/tools/backend._build_adapter()`, which constructs the adapter).

**Interface** — `BrokerAdapter` (abc):

| Method | Returns | Notes |
|---|---|---|
| `connect(creds: dict) -> None` | | memecoin paper: always succeeds, no credentials. memecoin live: always raises `BrokerError` (fail-closed). |
| `disconnect() -> None` | | |
| `account_info()` | `AccountInfo(balance, equity, currency)` | memecoin: balance = virtual SOL, equity = balance + open-position market value, currency `"SOL"` |
| `symbols(names=None)` | `list[SymbolSpec]` | `SymbolSpec`: name, volume_min/max/step, tick_value, tick_size, contract_size, digits, point. memecoin always registers `SOL`. |
| `candles(symbol, timeframe, count=200)` | `list[Candle]` | closed candles, oldest→newest (GeckoTerminal; raises `BROKER_UNAVAILABLE` when unreachable) |
| `positions()` | `list[Position]` | `Position`: ticket, position_id, symbol, direction, volume (SOL cost basis), price_open/current (SOL), sl, tp, profit, swap, magic, comment, time_open; `.is_own`, `.signal_id` |
| `orders()` | `list[Order]` | memecoin paper: always empty (fills are immediate) |
| `submit_order(req: OrderRequest)` | `OrderResult` | **broker-confirmed only** — returns after the fill is journaled; raises `BrokerError` on reject/transport error. Never a provisional fill. memecoin: `volume` is SOL-denominated; SELL is reduce-only (no shorting). |
| `modify_order(ticket, sl, tp)` | `None` | SL/TP modify; raises `BrokerError` |
| `close_position(ticket)` | `float` | close price in SOL; raises `BrokerError`. Not blocked by the kill-switch file (risk-reducing). |
| `deal_history(from_, to, position_id=None)` | `list[Deal]` | read back from the paper fill journal (`fills.jsonl`) |
| `health()` | `BrokerHealth(connected, adapter, server_time, message)` | |
| `quote(symbol)` | `Quote \| None` | `Quote`: symbol, bid, ask, time; `.spread`; None when unavailable. memecoin paper: bid == ask (no spread on the paper book). |
| `broker_status()` | `dict` | memecoin only: structured provider status — `provider/mode/configured/reachable/connected/account_available/market_data_available/trading_available/detail`. Never raises. Field docs: `broker/memecoin/RUNTIME.md`. |

**Errors** — `BrokerError(code, message, detail=None)` with codes:
`BROKER_UNAVAILABLE`, `BROKER_NOT_CONNECTED`, `INVALID_SYMBOL`,
`MARKET_CLOSED`, `RISK_LIMIT_EXCEEDED`, `DAILY_LOSS_LIMIT`,
`MAX_EXPOSURE`, `INVALID_ORDER`, `CONFIG_INVALID`,
`CREDENTIALS_INVALID`.

**Conventions** — `MAGIC_NUMBER = 20260817` tags every bot order;
`OrderRequest.order_comment()` yields `approved:<signal_id>`;
`is_own_position(magic)` / `Position.is_own` filter bot positions.

**Adapters** — `broker/memecoin/adapter.MemecoinAdapter` (paper:
self-contained virtual-SOL engine with JSON state, JSONL fill journal,
idempotency store, and kill-switch file coupling; live: fail-closed —
`connect()` always raises `BROKER_UNAVAILABLE`; see
`broker/memecoin/RUNTIME.md`),
`broker/disconnected.DisconnectedAdapter` (raises `BROKER_UNAVAILABLE`
for every operation; used when no broker is configured).
