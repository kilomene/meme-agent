# SECURITY.md — forex-agent

**Date:** 2026-09-21 · Scope: the whole subsystem (A: Linux forex-agent
below) plus the Cloudflare Worker (B: cloud/sync layer).

> **Note (2026-09-22):** the previous broker provider and all of its
> credential handling were purged and replaced by the keyless memecoin
> provider. Private keys are never requested, received, or stored
> anywhere in this system.

**Dated security review:** `docs/SECURITY_REVIEW_2026-09-21.md` (2026-09-21,
phase 8). It found and fixed 6 issues around secret redaction, the
secrets contract, and installer file permissions; 16 regression tests
in `tests/test_security.py`. NOTE: the review predates the 2026-09-22
broker purge — the previous provider, gateway, and credential handling
it describes no longer exist. The broker layer is now the keyless
memecoin provider. See the dated report for full detail.

---

# Part A — the forex-agent subsystem (Linux)

## 1. Localhost-only API

- `scripts/local_api.py` binds **127.0.0.1 only** — never `0.0.0.0`.
  The loopback bind IS the access control; there is no auth on the API
  or the SSE stream, by design.
- **Never expose this port beyond the host.** Anyone who can reach it
  can read events, account/positions data, and submit trade requests
  (still gated by the execution gateway, §5–§7, but readable and
  submittable).
- `FOREX_API_PORT` (default 8765) only changes the port, never the
  bind address.
- The SSE stream holds one server thread per connection (stdlib
  `ThreadingHTTPServer`) — sized for a handful of local agent
  subscribers, not for many concurrent streams.

## 2. Secret handling and redaction

- Secrets live in **exactly two places**: environment variables, or
  `$FOREX_AGENT_HOME/secrets.env` at **mode 0600**. The installer
  creates it at 0600, never overwrites it on re-runs, never prints its
  contents; `config/config.py` logs a warning if the file is readable
  by group/other.
- `AppConfig.redacted()` is the **only printable form** of the config.
  `scripts/forex config --json` redacts secret fields; `scripts/forex
  notify --status` masks `webhook_url`; webhook channel logs carry the
  host only, never the full URL (the URL may embed a token).
- Event payloads carry market/trade/risk facts — **never credentials**.
  Nothing in the event journal, logs, or `install-result.json`
  contains a secret.
- The broker layer is **keyless**: the memecoin provider takes no
  credentials and no private keys are ever requested, received, or
  stored — only a wallet **public** address may be configured, and only
  for the fail-closed live path. There is no broker secret to leak,
  rotate, or steal.

## 3. Memecoin provider: keyless + fail-closed

- The memecoin provider (`broker/memecoin/`) imports no broker SDK
  and holds no credentials: market data comes from public
  DexScreener/GeckoTerminal endpoints over stdlib `urllib`. There is
  no authentication material to protect on the market-data path.
- Live mode is intentionally fail-closed: `MemecoinAdapter.connect()`
  in live mode always raises `BrokerError(BROKER_UNAVAILABLE)` — no
  code path in this tree can produce a real-money fill.
- Arming live requires `MEMECOIN_LIVE_ARMED=1` plus
  `MEMECOIN_WALLET_PUBKEY` — a wallet **public** address only. The
  config loader rejects missing/blank arming values with
  `CREDENTIALS_INVALID`; private-key-shaped values are never accepted
  anywhere.
- No broker SDK is imported anywhere in this tree (guard test:
  `tests/test_iface_tools.py::TestNoBrokerSdk`); everything outside
  `broker/` talks to `BrokerAdapter`.

## 4. Input validation

- The event bus (`agent/events/bus.py`) schema-validates every event
  before journaling; malformed events are rejected, not stored.
- Trade requests are validated in the gateway: symbol whitelist
  (`INVALID_SYMBOL`), direction, volume step clamp, stop-loss/take-profit
  presence and sanity, modify rules (stops may tighten only —
  direction-aware: BUY stop up, SELL stop down), close requires a known
  ticket.
- SSE/HTTP: unknown `resume_from` / `ack` / `since` ids → HTTP 400
  (`INVALID_ARGUMENT`), never silent misbehavior.
- Installer: empty or whitespace-only `MEMECOIN_LIVE_ARMED` /
  `MEMECOIN_WALLET_PUBKEY` values count as missing →
  `needs_credentials` with a `required` list, never a silent proceed.

## 5. File permissions

| Path | Permissions | Notes |
|---|---|---|
| `$FOREX_AGENT_HOME/secrets.env` | 0600 | never overwritten, never printed |
| `$FOREX_AGENT_HOME/storage/local.db` | default umask of the installing user; keep the agent home private | holds kill-switch latch, risk state, idempotency keys, audit log |
| `$FOREX_AGENT_HOME/data/memecoin/` | agent home, same ownership | paper engine state: balance JSON, fill journal, idempotency store, kill-switch file |
| `run/*.pid`, `log/*.log`, `run/notify.cursor` | agent home, same ownership | no secrets |

For `--system` installs the `forex` user owns `/var/lib/forex-agent`;
systemd unit `WorkingDirectory` must be readable only by that user.

## 6. Kill-switch supremacy

- The kill switch sits **below the agent layer**: no prompt, tool call,
  or API request can trade through it. When engaged, `request_trade`
  and `modify_position` are rejected; `close_position` is still allowed
  (closing reduces risk).
- Two latches reinforce each other: the subsystem latch in
  `storage/local.db` (checked by `health_monitor`, which engages
  fail-closed if the latch is unreadable) and the memecoin paper
  engine's kill-switch file (`$FOREX_AGENT_HOME/data/memecoin/killswitch`),
  which blocks new submissions at the adapter while still permitting
  risk-reducing closes — the switch can never trap a position.
- `KillSwitch.close_all()` flattens every open position through the
  risk-reducing path and is never blocked by the switch itself.
- A Worker (cloud) outage never disables local safety — the local
  latch is always authoritative.

## 7. Execution gateway: single choke point

- `core/execution/gateway.py` is the only path from "the agent wants a
  trade" to "an order leaves this machine": idempotency → kill switch
  (fail-closed) → symbol whitelist → risk engine → spread/tick/account/
  broker-grid checks → dry-run block → broker submit → confirmed fills
  only. Every decision is audit-logged and emits an event.
- `core/execution/broker_guard.py` wraps the adapter in a
  `GatewayOnlyAdapter` that raises
  `BrokerError(GATEWAY_BYPASS_ATTEMPTED)` on any write attempt made
  outside the gateway. Trading tools call the
  gateway, never the adapter.

## 8. Honest failure (what is never faked)

No fake market data, no fake fills, no fake predictions. Disconnected
or unreachable broker ⇒ `BROKER_UNAVAILABLE` with an explanatory
message. Economic calendar and ML prediction report `available: false`.
Live mode is intentionally fail-closed with `BROKER_UNAVAILABLE` —
no code path in this tree can produce a real-money fill — the docs say
so wherever it matters.

---

# Part B — Cloudflare Worker (cloud/sync layer)

Scope: the Worker only. Subsystem-side secret handling is covered in
Part A above.

## 1. Authentication: per-client credentials

The old single shared secret (`WORKER_API_KEY`, one Bearer token for the
bridge and the app alike) is **gone**. Every client now has its own
provisioned record in the `api_clients` D1 table:

| Column | Meaning |
|---|---|
| `client_id` | unique, e.g. `bridge`, `app`, `agent` |
| `secret_hash` | SHA-256 hex digest of the secret — the raw secret is NEVER stored |
| `label` | human note ("signal bridge client") |
| `revoked` | `1` = rejected |
| `created_at` / `last_used_at` | provisioning + last successful use (best-effort) |

Wire format (both accepted):

```
Authorization: Basic base64(client_id:secret)   (preferred)
Authorization: Bearer <client_id>:<secret>
```

Auth failures are indistinguishable (unknown/revoked/bad → same 401)
and throttled per source IP. Provisioning and rotation: `worker/README.md`
and `docs/SECURITY.md` §1 (Part B).

## 2. Rate limiting

Per-isolate in-code limiter (`worker/src/ratelimit.js`) plus a
recommended Cloudflare dashboard rule for a hard global cap. Signal
report routes are capped (≤30 req/min); the route is not idempotent —
dedupe client-side (same symbol+timeframe+candle_time) before sending.

## 3. Data handling

- Signals, executions, closes, and performance snapshots are stored in
  D1; the schema is append-only via numbered migrations
  (`worker/migrations/`), never destructive.
- The `broker_result` column on `signals` holds the broker's execution
  result JSON (renamed from the previous provider-specific name in
  migration 0003; the provider is broker-neutral).
- Push notifications go through the configured provider; notification
  payloads carry trade facts, never secrets.
