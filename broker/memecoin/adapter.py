"""MemecoinAdapter — BrokerAdapter for Solana memecoin trading.

Self-contained paper trading engine: no external bot, no private keys,
no new dependencies. Two modes:

  * paper (default): virtual SOL balance, fills priced from public
    market data (DexScreener/GeckoTerminal via transport.py), every fill
    journaled to fills.jsonl, idempotency keys honored, adapter-level
    kill-switch file coupling (submit refuses while engaged).
  * live: intentionally fail-closed. connect() raises
    BrokerError(BROKER_UNAVAILABLE) — there is no execution path yet,
    so live can never place an order through this adapter.

Spot-only model: BUY spends SOL to open/increase a token position;
SELL reduces open long exposure (FIFO); naked SELL (no holdings) is
rejected with INVALID_ORDER — shorting is unsupported. `volume` is
denominated in SOL on both sides. Account currency is SOL.

State lives under data_dir (default: $FOREX_AGENT_HOME/data/memecoin):
  paper_state.json  virtual balance + open positions + ticket counter
  fills.jsonl       every paper fill, one JSON object per line
  idempotency.json  idempotency_key -> OrderResult (replay, never re-fill)
  killswitch        file presence = engaged; submit_order refuses while set

See RUNTIME.md for the full contract.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from broker import (
    MAGIC_NUMBER,
    AccountInfo,
    BrokerAdapter,
    BrokerError,
    BrokerHealth,
    Deal,
    Order,
    OrderRequest,
    OrderResult,
    Position,
    Quote,
    SymbolSpec,
    BROKER_NOT_CONNECTED,
    BROKER_UNAVAILABLE,
    CONFIG_INVALID,
    CREDENTIALS_INVALID,
    INVALID_ORDER,
    INVALID_SYMBOL,
    RISK_LIMIT_EXCEEDED,
)
from broker.memecoin.transport import MemecoinMarketData
from broker.memecoin.wallet import NullKeyProvider
from core.market import Candle

logger = logging.getLogger("broker.memecoin")

SOL_MINT = "So11111111111111111111111111111111111111112"
DEFAULT_PAPER_START_SOL = 10.0
_SOL_PRICE_TTL = 60.0  # seconds


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write_json_atomic(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


class MemecoinAdapter(BrokerAdapter):
    adapter_name = "memecoin"

    def __init__(self, data_dir: Optional[str] = None,
                 mode: str = "paper",
                 paper_start_sol: float = DEFAULT_PAPER_START_SOL,
                 live_armed: bool = False,
                 wallet_pubkey: str = "",
                 symbols: Optional[Dict[str, str]] = None,
                 market_data: Optional[MemecoinMarketData] = None,
                 chains: Optional[List[str]] = None,
                 key_provider=None,
                 devnet_rpc_url: str = ""):
        """
        data_dir: state directory (created if missing).
        mode: "paper" | "live". Live is fail-closed (see module docstring).
        paper_start_sol: virtual starting balance for a fresh paper state.
        live_armed / wallet_pubkey: required (but not sufficient) for live.
        symbols: {SYMBOL: mint}; SOL is always registered.
        market_data: injectable transport (tests).
        chains: enabled multi-chain names (registry names); default
            ["solana"]. "solana" is this adapter's native SOL-settled
            engine; every other enabled chain gets a USDC-settled
            ChainAdapter with per-chain state under <data_dir>/chains/.
        key_provider: WalletKeyProvider for the runtime key pattern
            (paper default: NullKeyProvider — no key needed).
        devnet_rpc_url: Solana devnet RPC endpoint for mode="devnet".
            The execution layer refuses non-devnet hosts regardless.
        """
        if mode not in ("paper", "devnet", "live"):
            raise BrokerError(CONFIG_INVALID,
                              f"memecoin mode must be paper|devnet|live, got {mode!r}")
        home = os.environ.get("FOREX_AGENT_HOME", os.path.expanduser("~/.forex-agent"))
        self._data_dir = Path(data_dir or os.path.join(home, "data", "memecoin"))
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._mode = mode
        self._paper_start_sol = float(paper_start_sol)
        self._live_armed = bool(live_armed)
        self._wallet_pubkey = (wallet_pubkey or "").strip()
        self._market = market_data or MemecoinMarketData()
        self._market.register("SOL", SOL_MINT)
        for sym, mint in (symbols or {}).items():
            self._market.register(sym, mint)
        self._enabled_chains = (
            ["solana"] if not chains
            else [str(c).strip().lower() for c in chains if str(c).strip()]
        ) or ["solana"]
        # Eager validation: unknown chain names fail fast, not at first trade.
        from broker.memecoin.chains.registry import chain_names  # noqa: PLC0415 (lazy)
        valid = set(chain_names())
        unknown = [c for c in self._enabled_chains if c not in valid]
        if unknown:
            raise BrokerError(
                CONFIG_INVALID,
                f"unknown memecoin chain(s): {unknown} — valid: "
                f"{sorted(valid)}")
        self._key_provider = key_provider or NullKeyProvider()
        self._chain_adapters: Dict[str, object] = {}
        self._connected = False
        # Devnet execution (mode="devnet" only). Built lazily on connect()
        # so paper mode never imports the signing stack.
        self._devnet_rpc_url = (devnet_rpc_url or "").strip()
        self._devnet = None  # DevnetExecutor once connected
        self._sol_price_cache: Optional[tuple] = None  # (ts, price_usd)
        self._load_state()

    # -- persisted paper state -------------------------------------------
    def _state_path(self) -> Path:
        return self._data_dir / "paper_state.json"

    def _load_state(self) -> None:
        state = _read_json(self._state_path(), None)
        if not isinstance(state, dict):
            state = {}
        self._balance_sol: float = float(
            state.get("balance_sol", self._paper_start_sol))
        self._positions: Dict[str, dict] = state.get("positions", {})
        self._next_ticket: int = int(state.get("next_ticket", 1))

    def _save_state(self) -> None:
        _write_json_atomic(self._state_path(), {
            "balance_sol": self._balance_sol,
            "positions": self._positions,
            "next_ticket": self._next_ticket,
            "updated_at": _utcnow().isoformat(),
        })

    def _fills_path(self) -> Path:
        return self._data_dir / "fills.jsonl"

    def _journal_fill(self, entry: dict) -> None:
        entry = dict(entry)
        entry.setdefault("ts", _utcnow().isoformat())
        entry.setdefault("mode", self._mode)
        with open(self._fills_path(), "a") as fh:
            fh.write(json.dumps(entry) + "\n")

    def _idempotency_path(self) -> Path:
        return self._data_dir / "idempotency.json"

    def _idempotent_get(self, key: str) -> Optional[dict]:
        store = _read_json(self._idempotency_path(), {})
        return store.get(key) if isinstance(store, dict) else None

    def _idempotent_put(self, key: str, result: dict) -> None:
        store = _read_json(self._idempotency_path(), {})
        if not isinstance(store, dict):
            store = {}
        store[key] = result
        _write_json_atomic(self._idempotency_path(), store)

    def _idempotent_recover_from_journal(self, key: str) -> Optional[OrderResult]:
        """Rebuild an OrderResult from the fill journal.

        Crash window: the fill is journaled (and state saved) BEFORE the
        idempotency store is written. If the process dies in between, a
        retry would otherwise fill a second time. The journal already
        records the idempotency key, so a retry can recover the original
        fill instead of duplicating it.
        """
        path = self._fills_path()
        if not path.exists():
            return None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in reversed(lines[-500:]):  # recent fills only
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("idempotency_key") != key:
                continue
            if entry.get("kind") not in ("BUY", "SELL"):
                # Auxiliary entries (e.g. DEVNET_EXEC linkage) carry no
                # fill data — skip them during recovery.
                continue
            return OrderResult(
                ticket=int(entry["ticket"]),
                symbol=str(entry["symbol"]),
                direction=str(entry.get("kind", "BUY")),
                volume=float(entry["volume_sol"]),
                price=float(entry["price_sol"]),
                retcode=0,
                message="paper fill (recovered from journal)",
                raw={"mode": "paper",
                     "tokens": float(entry.get("tokens", 0.0)),
                     "recovered": True},
            )
        return None

    def _kill_switch_engaged(self) -> bool:
        return (self._data_dir / "killswitch").exists()

    # -- pricing -----------------------------------------------------------
    def _sol_price_usd(self) -> float:
        now = time.monotonic()
        if self._sol_price_cache and now - self._sol_price_cache[0] < _SOL_PRICE_TTL:
            return self._sol_price_cache[1]
        q = self._market.get_quote("SOL")
        if not q or not q.get("price_usd"):
            raise BrokerError(BROKER_UNAVAILABLE,
                              "memecoin market data unreachable (SOL/USD quote "
                              "failed) — refusing to price paper fills",
                              detail={"reason": "market_data_unreachable"})
        price = float(q["price_usd"])
        self._sol_price_cache = (now, price)
        return price

    def _price_sol(self, symbol: str) -> float:
        """Token price in SOL, via USD legs. Raises BROKER_UNAVAILABLE
        when market data is unreachable — paper fills are never priced
        from stale or missing data."""
        q = self._market.get_quote(symbol)
        if not q or not q.get("price_usd"):
            raise BrokerError(
                BROKER_UNAVAILABLE,
                f"no market quote for {symbol!r} — paper fill refused",
                detail={"reason": "market_data_unreachable", "symbol": symbol})
        return float(q["price_usd"]) / self._sol_price_usd()

    def _price_sol_quiet(self, symbol: str) -> Optional[float]:
        try:
            return self._price_sol(symbol)
        except BrokerError:
            return None

    # -- lifecycle ---------------------------------------------------------
    def connect(self, creds: dict) -> None:
        if self._mode == "live":
            # Fail-closed: there is deliberately no execution path yet.
            # The armed/wallet checks run first so misconfiguration is
            # reported as misconfiguration, not as an outage.
            if not (self._live_armed and self._wallet_pubkey):
                raise BrokerError(
                    CREDENTIALS_INVALID,
                    "live memecoin mode requires MEMECOIN_LIVE_ARMED=true and "
                    "MEMECOIN_WALLET_PUBKEY set (public key only — the wallet "
                    "secret must never leave your custody)")
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "live memecoin execution is not implemented: this adapter "
                "is paper-only by design. See broker/memecoin/RUNTIME.md.",
                detail={"reason": "live_not_implemented"})
        if self._mode == "devnet":
            self._connect_devnet()
            return
        self._connected = True
        logger.info("MemecoinAdapter connected (paper mode, data_dir=%s)",
                    self._data_dir)

    def _connect_devnet(self) -> None:
        """Real devnet execution. Fails closed without a key, without a
        configured wallet pubkey, on pubkey mismatch, or when devnet is
        unreachable."""
        from broker.memecoin.execution.devnet_executor import (  # noqa: PLC0415 (lazy)
            DevnetExecutor)
        from broker.memecoin.execution.rpc import (  # noqa: PLC0415 (lazy)
            DEFAULT_DEVNET_RPC)
        if not self._wallet_pubkey:
            raise BrokerError(
                CREDENTIALS_INVALID,
                "devnet memecoin mode requires MEMECOIN_WALLET_PUBKEY set "
                "(public key only)")
        executor = DevnetExecutor(
            key_provider=self._key_provider,
            rpc_url=self._devnet_rpc_url or DEFAULT_DEVNET_RPC,
            expected_pubkey=self._wallet_pubkey)
        wallet = executor.connect()  # raises on any problem
        self._devnet = executor
        self._connected = True
        logger.info("MemecoinAdapter connected (devnet mode, wallet=%s)",
                    wallet)

    def disconnect(self) -> None:
        self._connected = False

    def _ensure(self) -> None:
        if not self._connected:
            raise BrokerError(BROKER_NOT_CONNECTED,
                              "Not connected — call connect() first.")

    # -- multi-chain plumbing --------------------------------------------
    @property
    def enabled_chains(self) -> List[str]:
        """Enabled multi-chain names (registry names)."""
        return list(self._enabled_chains)

    def chain_adapter(self, name: str):
        """Return the ChainAdapter for an enabled non-solana chain.

        "solana" is this adapter's native SOL-settled engine, so it has
        no ChainAdapter — use submit_order(req) without a chain for the
        Solana path. Raises CONFIG_INVALID for unknown/disabled chains
        (and for "solana", to avoid a confusing dual-book).
        """
        from broker.memecoin.chains import get_chain_adapter  # noqa: PLC0415 (lazy)
        key = (name or "").strip().lower()
        if key == "solana":
            raise BrokerError(
                CONFIG_INVALID,
                "'solana' is served by this adapter's native SOL-settled "
                "engine — call submit_order(req) without a chain for the "
                "Solana path")
        if key not in self._enabled_chains:
            raise BrokerError(
                CONFIG_INVALID,
                f"chain {name!r} is not enabled (enabled: "
                f"{self._enabled_chains})")
        if key not in self._chain_adapters:
            self._chain_adapters[key] = get_chain_adapter(
                key, data_dir=str(self._data_dir), mode=self._mode,
                key_provider=self._key_provider, armed=self._live_armed)
        return self._chain_adapters[key]

    def chain_status(self) -> dict:
        """Per-chain status fragments, keyed by chain name. Never raises."""
        self._ensure()
        out = {
            "solana": {
                "chain": "solana",
                "family": "solana",
                "mode": self._mode,
                "note": "native SOL-settled engine",
                "trading_available": (
                    self._mode == "paper" and not self._kill_switch_engaged()),
                "detail": {
                    "balance_sol": round(self._balance_sol, 9),
                    "open_positions": len(self._positions),
                },
            }
        }
        for name in self._enabled_chains:
            if name == "solana":
                continue
            try:
                out[name] = self.chain_adapter(
                    name).broker_status_fragment()
            except Exception as exc:  # defensive: status must never raise
                out[name] = {"chain": name,
                             "detail": {"probe_error": str(exc)[:200]}}
        return out

    # -- account -----------------------------------------------------------
    def _open_value_sol(self) -> float:
        """Current market value of open positions, in SOL."""
        total = 0.0
        for p in self._positions.values():
            px = self._price_sol_quiet(p["symbol"])
            if px is None:
                continue
            total += px * p["tokens"]
        return total

    def account_info(self) -> AccountInfo:
        self._ensure()
        # Equity = cash + current market value of open positions. (BUY
        # deducts the cost from cash, so adding only unrealized P/L would
        # double-count the cost basis.)
        equity = self._balance_sol + self._open_value_sol()
        return AccountInfo(
            balance=self._balance_sol,
            equity=equity,
            currency="SOL",
            margin=0.0,
            free_margin=self._balance_sol,
            leverage=1,
            login=0,
            server="memecoin-paper" if self._mode == "paper" else "memecoin-live",
        )

    # -- market data ---------------------------------------------------------
    def symbols(self, names: Optional[List[str]] = None) -> List[SymbolSpec]:
        self._ensure()
        known = self._market.known_symbols()
        if names is not None:
            unknown = [n for n in names if n.upper() not in known]
            if unknown:
                raise BrokerError(INVALID_SYMBOL,
                                  f"unknown memecoin symbol(s): {unknown}")
            known = [n.upper() for n in names]
        return [SymbolSpec(
            name=n,
            volume_min=0.001,      # SOL
            volume_max=1000.0,     # SOL
            volume_step=0.001,
            tick_value=1.0,        # account currency is SOL; 1 tick = 1 SOL per unit
            tick_size=1e-9,
            contract_size=1.0,
            digits=9,
            point=1e-9,
        ) for n in known]

    def candles(self, symbol: str, timeframe: str, count: int = 200) -> List[Candle]:
        self._ensure()
        return self._market.get_candles(symbol, timeframe, count)

    def quote(self, symbol: str) -> Optional[Quote]:
        self._ensure()
        px = self._price_sol_quiet(symbol)
        if px is None:
            return None
        now = _utcnow()
        # Spot quote: no spread on the paper book; spread checks see 0.
        return Quote(symbol=symbol.upper(), bid=px, ask=px, time=now)

    def token_stats(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Rich DexScreener token stats for signal cards.

        Best-effort: returns None when the upstream is unreachable.
        Never raises — signal emission must not fail because stats
        are unavailable.
        """
        try:
            return self._market.get_token_stats(symbol)
        except Exception:  # noqa: BLE001 - stats are best-effort
            return None

    # -- positions / orders ---------------------------------------------------
    def _as_position(self, ticket: int, p: dict) -> Position:
        px = self._price_sol_quiet(p["symbol"]) or p["entry_price_sol"]
        profit = (px - p["entry_price_sol"]) * p["tokens"] \
            if p["direction"] == "BUY" else 0.0
        return Position(
            ticket=ticket,
            position_id=ticket,
            symbol=p["symbol"],
            direction=p["direction"],
            volume=p["volume_sol"],
            price_open=p["entry_price_sol"],
            price_current=px,
            sl=p["sl"],
            tp=p["tp"],
            profit=profit,
            swap=0.0,
            magic=MAGIC_NUMBER,
            comment=p["comment"],
            time_open=datetime.fromisoformat(p["time_open"]),
        )

    def positions(self) -> List[Position]:
        self._ensure()
        return [self._as_position(int(t), p)
                for t, p in sorted(self._positions.items(), key=lambda kv: int(kv[0]))]

    def orders(self) -> List[Order]:
        self._ensure()
        return []  # paper book has no pending orders; fills are immediate

    # -- trading (paper) -------------------------------------------------------
    def _next(self) -> int:
        t = self._next_ticket
        self._next_ticket += 1
        return t

    def submit_order(self, req: OrderRequest,
                     chain: Optional[str] = None) -> OrderResult:
        self._ensure()
        target = (chain or "solana").strip().lower()
        if target != "solana":
            # Multi-chain route: the ChainAdapter owns validation,
            # pricing, journaling and the live fail-closed gate. req.volume
            # is interpreted as USDC on chain routes (documented in
            # RUNTIME.md); the native path below stays SOL-denominated.
            if req.direction not in ("BUY", "SELL"):
                raise BrokerError(INVALID_ORDER,
                                  f"direction must be BUY|SELL, got {req.direction!r}")
            if req.volume <= 0:
                raise BrokerError(INVALID_ORDER,
                                  f"volume must be positive, got {req.volume}")
            if req.stop_loss is None:
                raise BrokerError(INVALID_ORDER, "stop_loss is required")
            adapter = self.chain_adapter(target)
            fill = adapter.submit_swap(
                token=req.symbol, side=req.direction,
                amount_usdc=float(req.volume),
                idempotency_key=req.idempotency_key)
            return OrderResult(
                ticket=fill["ticket"], symbol=req.symbol.upper(),
                direction=req.direction, volume=float(req.volume),
                price=float(fill["price_usd"]), retcode=0,
                message=f"paper fill ({target})",
                raw={"mode": "paper", "chain": target, "fill": fill})
        if self._mode == "live":
            # Unreachable in practice (connect() refuses live), but fail
            # closed here too — defense in depth.
            raise BrokerError(BROKER_UNAVAILABLE,
                              "live memecoin execution is not implemented")
        if self._kill_switch_engaged():
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "memecoin kill-switch file is engaged — paper order refused",
                detail={"reason": "kill_switch_engaged"})
        if req.direction not in ("BUY", "SELL"):
            raise BrokerError(INVALID_ORDER,
                              f"direction must be BUY|SELL, got {req.direction!r}")
        if req.volume <= 0:
            raise BrokerError(INVALID_ORDER,
                              f"volume must be positive, got {req.volume}")
        if req.stop_loss is None:
            raise BrokerError(INVALID_ORDER, "stop_loss is required")
        symbol = req.symbol.upper()
        if not self._market.mint_for(symbol):
            raise BrokerError(INVALID_SYMBOL,
                              f"unknown memecoin symbol {req.symbol!r}")

        # Idempotency: a repeated key replays the original fill — the
        # position is never opened twice.
        if req.idempotency_key:
            prior = self._idempotent_get(req.idempotency_key)
            if prior is not None:
                logger.info("idempotent replay for key %s (ticket %s)",
                            req.idempotency_key, prior.get("ticket"))
                return OrderResult(**prior)
            # Crash window: fill journaled but idempotency store never
            # written (process died between the two writes). Recover from
            # the journal instead of filling a second time.
            recovered = self._idempotent_recover_from_journal(
                req.idempotency_key)
            if recovered is not None:
                logger.info("idempotent recovery from journal for key %s "
                            "(ticket %s)", req.idempotency_key,
                            recovered.ticket)
                self._idempotent_put(req.idempotency_key, {
                    "ticket": recovered.ticket, "symbol": recovered.symbol,
                    "direction": recovered.direction,
                    "volume": recovered.volume, "price": recovered.price,
                    "retcode": recovered.retcode,
                    "message": recovered.message, "raw": recovered.raw,
                })
                return recovered

        price_sol = self._price_sol(symbol)  # raises BROKER_UNAVAILABLE when blind

        if self._mode == "devnet":
            return self._devnet_fill(req, symbol, price_sol)

        if req.direction == "BUY":
            result = self._paper_buy(req, symbol, price_sol)
        else:
            result = self._paper_sell(req, symbol, price_sol)

        if req.idempotency_key:
            self._idempotent_put(req.idempotency_key, {
                "ticket": result.ticket, "symbol": result.symbol,
                "direction": result.direction, "volume": result.volume,
                "price": result.price, "retcode": result.retcode,
                "message": result.message, "raw": result.raw,
            })
        return result

    def _devnet_fill(self, req: OrderRequest, symbol: str,
                     price_sol: float) -> OrderResult:
        """Devnet mode fill: real on-chain SOL transfer + paper ledger.

        There is no memecoin DEX liquidity on devnet, so the SOL leg of
        the trade is executed as a real devnet SOL transfer (to self —
        proving sign -> send -> confirm on-chain), while token quantities
        are tracked in the paper ledger exactly like paper mode. Every
        journaled devnet fill carries its real devnet transaction
        signature.

        Ordering: the on-chain transfer happens BEFORE the ledger
        mutation, so a failed transfer never leaves a fill without a
        signature. The light pre-checks below mirror _paper_buy/_paper_sell
        validation for the same reason (they re-run inside those
        methods; duplication is deliberate).
        """
        if self._devnet is None:
            raise BrokerError(BROKER_UNAVAILABLE,
                              "devnet executor not connected")
        sol_amount = float(req.volume)
        if req.direction == "BUY":
            if sol_amount > self._balance_sol:
                raise BrokerError(
                    RISK_LIMIT_EXCEEDED,
                    f"insufficient virtual balance: need {sol_amount:.6f} "
                    f"SOL, have {self._balance_sol:.6f} SOL",
                    detail={"need_sol": sol_amount,
                            "balance_sol": self._balance_sol})
        else:
            tokens_to_sell = sol_amount / price_sol
            available = sum(
                p["tokens"] for p in self._positions.values()
                if p["symbol"] == symbol and p["direction"] == "BUY")
            if tokens_to_sell > available + 1e-12:
                raise BrokerError(
                    INVALID_ORDER,
                    f"SELL {symbol} rejected: trying to sell "
                    f"{tokens_to_sell:.6f} tokens but only {available:.6f} "
                    f"held")
        # Real devnet execution: self-transfer proves the pipeline.
        sig = self._devnet.transfer_sol(self._devnet.wallet_pubkey,
                                        sol_amount)
        if req.direction == "BUY":
            result = self._paper_buy(req, symbol, price_sol)
        else:
            result = self._paper_sell(req, symbol, price_sol)
        self._journal_fill({
            "ticket": result.ticket, "kind": "DEVNET_EXEC",
            "symbol": symbol, "direction": req.direction,
            "volume_sol": sol_amount, "devnet_sig": sig,
            "idempotency_key": req.idempotency_key,
        })
        result.message = f"devnet fill (sig {sig[:12]}…)"
        result.raw["mode"] = "devnet"
        result.raw["devnet_sig"] = sig
        # _paper_buy/_paper_sell wrote the idempotency record with
        # mode="paper" before we annotated it — overwrite with the
        # devnet-annotated result so replays return the signature, not
        # the stale paper record.
        self._idempotent_put(req.idempotency_key, {
            "ticket": result.ticket, "symbol": result.symbol,
            "direction": result.direction, "volume": result.volume,
            "price": result.price, "retcode": result.retcode,
            "message": result.message, "raw": result.raw,
        })
        logger.info("devnet %s %s: %.6f SOL on-chain sig=%s (ticket %s)",
                    req.direction, symbol, sol_amount, sig, result.ticket)
        return result

    def _paper_buy(self, req: OrderRequest, symbol: str,
                   price_sol: float) -> OrderResult:
        cost_sol = float(req.volume)
        if cost_sol > self._balance_sol:
            raise BrokerError(
                RISK_LIMIT_EXCEEDED,
                f"insufficient virtual balance: need {cost_sol:.6f} SOL, "
                f"have {self._balance_sol:.6f} SOL",
                detail={"need_sol": cost_sol,
                        "balance_sol": self._balance_sol})
        tokens = cost_sol / price_sol
        ticket = self._next()
        comment = req.order_comment()
        self._positions[str(ticket)] = {
            "symbol": symbol,
            "direction": "BUY",
            "volume_sol": cost_sol,
            "tokens": tokens,
            "entry_price_sol": price_sol,
            "sl": req.stop_loss,
            "tp": req.take_profit,
            "comment": comment,
            "signal_id": req.signal_id,
            "time_open": _utcnow().isoformat(),
        }
        self._balance_sol -= cost_sol
        self._save_state()
        self._journal_fill({
            "ticket": ticket, "kind": "BUY", "symbol": symbol,
            "volume_sol": cost_sol, "tokens": tokens,
            "price_sol": price_sol, "comment": comment,
            "signal_id": req.signal_id,
            "idempotency_key": req.idempotency_key,
            "balance_sol_after": self._balance_sol,
        })
        logger.info("paper BUY %s: %.6f SOL -> %.6f tokens @ %.9f SOL (ticket %s)",
                    symbol, cost_sol, tokens, price_sol, ticket)
        return OrderResult(ticket=ticket, symbol=symbol, direction="BUY",
                           volume=cost_sol, price=price_sol, retcode=0,
                           message="paper fill",
                           raw={"mode": "paper", "tokens": tokens})

    def _paper_sell(self, req: OrderRequest, symbol: str,
                    price_sol: float) -> OrderResult:
        """Spot reduce-only: SELL closes/reduces open BUY exposure FIFO.
        Naked SELL is rejected — shorting is unsupported."""
        sell_sol = float(req.volume)
        holdings = [(int(t), p) for t, p in self._positions.items()
                    if p["symbol"] == symbol and p["direction"] == "BUY"]
        if not holdings:
            raise BrokerError(
                INVALID_ORDER,
                f"SELL {symbol} rejected: no open long position to reduce "
                f"(spot market — shorting is unsupported)")
        holdings.sort(key=lambda tp: tp[0])  # FIFO
        tokens_to_sell = sell_sol / price_sol
        available = sum(p["tokens"] for _, p in holdings)
        if tokens_to_sell > available + 1e-12:
            raise BrokerError(
                INVALID_ORDER,
                f"SELL {symbol} rejected: trying to sell "
                f"{tokens_to_sell:.6f} tokens but only {available:.6f} held")
        remaining = tokens_to_sell
        realized = 0.0
        closed_tickets = []
        for ticket, p in holdings:
            if remaining <= 1e-12:
                break
            take = min(p["tokens"], remaining)
            realized += (price_sol - p["entry_price_sol"]) * take
            p["tokens"] -= take
            # volume_sol tracks remaining cost basis proportionally
            if p["tokens"] <= 1e-12:
                closed_tickets.append(ticket)
                del self._positions[str(ticket)]
            else:
                frac = p["tokens"] / (p["tokens"] + take)
                p["volume_sol"] *= frac
            remaining -= take
        proceeds_sol = tokens_to_sell * price_sol
        self._balance_sol += proceeds_sol
        # Allocate the ticket BEFORE persisting state: if the process dies
        # between the state write and the journal write, the ticket is
        # never reused on restart.
        ticket = self._next()
        self._save_state()
        comment = req.order_comment()
        self._journal_fill({
            "ticket": ticket, "kind": "SELL", "symbol": symbol,
            "volume_sol": proceeds_sol, "tokens": tokens_to_sell,
            "price_sol": price_sol, "realized_pnl_sol": realized,
            "closed_tickets": closed_tickets,
            "comment": comment, "signal_id": req.signal_id,
            "idempotency_key": req.idempotency_key,
            "balance_sol_after": self._balance_sol,
        })
        logger.info("paper SELL %s: %.6f tokens @ %.9f SOL, realized %+.9f SOL "
                    "(ticket %s)", symbol, tokens_to_sell, price_sol,
                    realized, ticket)
        return OrderResult(ticket=ticket, symbol=symbol, direction="SELL",
                           volume=proceeds_sol, price=price_sol, retcode=0,
                           message="paper fill",
                           raw={"mode": "paper", "tokens": tokens_to_sell,
                                "realized_pnl_sol": realized,
                                "closed_tickets": closed_tickets})

    def modify_order(self, ticket: int, sl: float, tp: float) -> None:
        self._ensure()
        if self._mode == "live":
            raise BrokerError(BROKER_UNAVAILABLE,
                              "live memecoin execution is not implemented")
        p = self._positions.get(str(ticket))
        if p is None:
            raise BrokerError(INVALID_ORDER,
                              f"no open paper position with ticket {ticket}")
        p["sl"], p["tp"] = sl, tp
        self._save_state()
        logger.info("paper modify ticket %s: SL=%.9f TP=%.9f", ticket, sl, tp)

    def close_position(self, ticket: int) -> float:
        """Market close of a paper position at the live quote. Returns the
        close price in SOL.

        Risk-reducing closes are NOT blocked by the kill-switch file —
        only new orders are. An emergency brake must never trap open
        positions (core KillSwitch.close_all() relies on this).
        """
        self._ensure()
        if self._mode == "live":
            raise BrokerError(BROKER_UNAVAILABLE,
                              "live memecoin execution is not implemented")
        p = self._positions.get(str(ticket))
        if p is None:
            raise BrokerError(INVALID_ORDER,
                              f"no open paper position with ticket {ticket}")
        # Price FIRST: if the quote fails the position stays untouched.
        price_sol = self._price_sol(p["symbol"])
        del self._positions[str(ticket)]
        proceeds = p["tokens"] * price_sol
        realized = (price_sol - p["entry_price_sol"]) * p["tokens"]
        self._balance_sol += proceeds
        self._save_state()
        self._journal_fill({
            "ticket": ticket, "kind": "CLOSE", "symbol": p["symbol"],
            "volume_sol": proceeds, "tokens": p["tokens"],
            "price_sol": price_sol, "realized_pnl_sol": realized,
            "balance_sol_after": self._balance_sol,
        })
        logger.info("paper CLOSE ticket %s (%s): %+.9f SOL realized",
                    ticket, p["symbol"], realized)
        return price_sol

    def close_position_at(self, ticket: int, price_sol: float,
                          reason: str = "") -> float:
        """Close a paper position at an explicit price. Returns the close
        price in SOL.

        Only for dead-pool accounting: the market quote is confirmed
        gone (no pool to sell into, feed healthy for other coins), so
        the position is worthless. Normal closes must use
        close_position() (live quote) — paper fills are never priced
        from stale or missing data.
        """
        self._ensure()
        if self._mode == "live":
            raise BrokerError(BROKER_UNAVAILABLE,
                              "live memecoin execution is not implemented")
        p = self._positions.get(str(ticket))
        if p is None:
            raise BrokerError(INVALID_ORDER,
                              f"no open paper position with ticket {ticket}")
        price_sol = float(price_sol)
        del self._positions[str(ticket)]
        proceeds = p["tokens"] * price_sol
        realized = (price_sol - p["entry_price_sol"]) * p["tokens"]
        self._balance_sol += proceeds
        self._save_state()
        self._journal_fill({
            "ticket": ticket, "kind": "CLOSE", "symbol": p["symbol"],
            "volume_sol": proceeds, "tokens": p["tokens"],
            "price_sol": price_sol, "realized_pnl_sol": realized,
            "balance_sol_after": self._balance_sol,
            "close_reason": reason or "explicit",
        })
        logger.info("paper CLOSE@%s ticket %s (%s): %+.9f SOL realized",
                    price_sol, ticket, p["symbol"], realized)
        return price_sol

    # -- history -----------------------------------------------------------
    def deal_history(self, from_: datetime, to: datetime,
                     position_id: Optional[int] = None) -> List[Deal]:
        self._ensure()
        deals: List[Deal] = []
        try:
            lines = self._fills_path().read_text().splitlines()
        except OSError:
            return []
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ts = e.get("ts", "")
            try:
                t = datetime.fromisoformat(ts)
            except ValueError:
                continue
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            lo, hi = from_, to
            if lo.tzinfo is None:
                lo = lo.replace(tzinfo=timezone.utc)
            if hi.tzinfo is None:
                hi = hi.replace(tzinfo=timezone.utc)
            if not (lo <= t <= hi):
                continue
            if position_id is not None and e.get("ticket") != position_id:
                continue
            kind = e.get("kind", "")
            deals.append(Deal(
                ticket=int(e.get("ticket", 0)),
                position_id=int(e.get("ticket", 0)),
                symbol=e.get("symbol", ""),
                direction="BUY" if kind in ("BUY",) else "SELL",
                entry="IN" if kind == "BUY" else "OUT",
                volume=float(e.get("volume_sol", 0.0)),
                price=float(e.get("price_sol", 0.0)),
                profit=float(e.get("realized_pnl_sol", 0.0)),
                commission=0.0,
                swap=0.0,
                magic=MAGIC_NUMBER,
                comment=e.get("comment", ""),
                time=t,
            ))
        return deals

    # -- health / status -----------------------------------------------------
    def health(self) -> BrokerHealth:
        if self._mode == "live":
            return BrokerHealth(
                connected=False, adapter=self.adapter_name,
                server_time=_utcnow(),
                message="live memecoin execution is not implemented "
                        "(paper-only adapter).")
        return BrokerHealth(
            connected=self._connected,
            adapter=self.adapter_name,
            server_time=_utcnow(),
            message="" if self._connected else "Not connected (paper).",
        )

    def _market_data_ok(self) -> bool:
        """Best-effort market-data probe with a short timeout. Never raises."""
        try:
            return self._market.get_quote("SOL") is not None
        except Exception:
            return False

    def broker_status(self) -> dict:
        """Structured status. Never raises. Field docs: RUNTIME.md."""
        try:
            return {"broker": self._status()}
        except Exception as exc:  # defensive: status must never raise
            return {"broker": {
                "provider": "memecoin", "mode": self._mode,
                "configured": True, "reachable": False, "connected": False,
                "account_available": False, "market_data_available": False,
                "trading_available": False,
                "detail": {"probe_error": str(exc)[:200]},
            }}

    def _status(self) -> dict:
        status = {
            "provider": "memecoin",
            "mode": self._mode,
            "configured": True,
            "reachable": False,
            "connected": False,
            "account_available": False,
            "market_data_available": False,
            "trading_available": False,
            "detail": {"chains": self._enabled_chains},
        }
        if self._mode == "live":
            status["detail"]["reason"] = (
                "live memecoin execution is not implemented — this adapter "
                "is paper-only by design (see broker/memecoin/RUNTIME.md)")
            return status
        # Paper engine is local: it always "answers".
        status["reachable"] = True
        status["connected"] = self._connected
        status["account_available"] = True
        try:
            bal = self._balance_sol
            status["detail"]["balance_sol"] = round(bal, 9)
            status["detail"]["open_positions"] = len(self._positions)
        except Exception:
            pass
        if self._market_data_ok():
            status["market_data_available"] = True
        else:
            status["detail"]["market_data"] = (
                "public market-data endpoints unreachable — paper fills "
                "will be refused until quotes return")
        if self._kill_switch_engaged():
            status["detail"]["kill_switch"] = "engaged (killswitch file present)"
        else:
            status["trading_available"] = True
        if not self._connected:
            status["detail"]["reason"] = "paper engine ready, not connected (call connect())"
        return status
