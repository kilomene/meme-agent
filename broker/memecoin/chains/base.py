"""ChainAdapter — per-chain interface for the multi-chain memecoin provider.

Every supported chain gets one ChainAdapter. Concrete adapters share
the USDC-settled spot paper book (PaperBook): BUY spends virtual USDC,
SELL reduces token holdings (no shorts), every fill journaled per
chain, idempotency keys honored.

Paper/live contract (same as the top-level adapter):
  * paper: virtual balances, fills priced from public quotes only —
    a missing quote REFUSES the fill (BROKER_UNAVAILABLE), never
    invents a price.
  * live: fail-closed. submit_swap requires an armed wallet key AND
    then still raises BROKER_UNAVAILABLE — there is no execution path
    at this stage. The checks exist and are tested; live never executes.

Key hygiene: adapters never log, repr, or echo key material. The key
provider is consulted transiently (truthiness only) and the key is
never stored on the adapter.
"""

import abc
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from broker import (
    BROKER_UNAVAILABLE,
    CONFIG_INVALID,
    CREDENTIALS_INVALID,
    INVALID_ORDER,
    INVALID_SYMBOL,
    BrokerError,
)

logger = logging.getLogger("broker.memecoin.chains")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json_atomic(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class PaperBook:
    """USDC-settled spot paper book shared by all chain adapters.

    State (per chain, under <data_dir>/chains/<chain>/):
      paper_state.json  balances {native, USDC}, holdings, ticket counter
      fills.jsonl       every paper fill, one JSON object per line
      idempotency.json  idempotency_key -> fill dict (replay, never re-fill)

    Spot reduce-only: BUY spends USDC for tokens at the quoted USD price;
    SELL converts token holdings back to USDC (average-cost basis).
    Naked SELL is rejected — shorting is unsupported.
    """

    def __init__(self, state_dir: str, chain_name: str, native_currency: str,
                 start_native: float = 1.0, start_usdc: float = 1000.0):
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._chain = chain_name
        self._native = native_currency
        self._start_native = float(start_native)
        self._start_usdc = float(start_usdc)
        self._load()

    # -- state ---------------------------------------------------------
    def _load(self) -> None:
        state = _read_json(self._dir / "paper_state.json", None)
        if not isinstance(state, dict):
            state = {}
        bal = state.get("balances", {})
        self._balances: Dict[str, float] = {
            "native": float(bal.get("native", self._start_native)),
            "USDC": float(bal.get("USDC", self._start_usdc)),
        }
        self._holdings: Dict[str, dict] = state.get("holdings", {})
        self._next_ticket: int = int(state.get("next_ticket", 1))

    def _save(self) -> None:
        _write_json_atomic(self._dir / "paper_state.json", {
            "balances": self._balances,
            "holdings": self._holdings,
            "next_ticket": self._next_ticket,
            "updated_at": _utcnow_iso(),
        })

    def _journal(self, entry: dict) -> None:
        entry = dict(entry)
        entry.setdefault("ts", _utcnow_iso())
        entry.setdefault("mode", "paper")
        entry.setdefault("chain", self._chain)
        with open(self._dir / "fills.jsonl", "a",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def _idem_get(self, key: str) -> Optional[dict]:
        store = _read_json(self._dir / "idempotency.json", {})
        return store.get(key) if isinstance(store, dict) else None

    def _idem_put(self, key: str, fill: dict) -> None:
        store = _read_json(self._dir / "idempotency.json", {})
        if not isinstance(store, dict):
            store = {}
        store[key] = fill
        _write_json_atomic(self._dir / "idempotency.json", store)

    # -- reads ---------------------------------------------------------
    def balances(self) -> Dict[str, float]:
        return dict(self._balances)

    def holdings(self) -> Dict[str, dict]:
        return {t: dict(h) for t, h in self._holdings.items()}

    # -- trading -------------------------------------------------------
    def _next(self) -> int:
        t = self._next_ticket
        self._next_ticket += 1
        return t

    def buy(self, token: str, spend_usdc: float, price_usd: float,
            idempotency_key: Optional[str] = None) -> dict:
        token = token.upper() if not token.startswith("0x") else token
        if spend_usdc <= 0:
            raise BrokerError(INVALID_ORDER,
                              f"swap amount must be positive, got {spend_usdc}")
        if not price_usd or price_usd <= 0:
            raise BrokerError(BROKER_UNAVAILABLE,
                              f"no usable quote for {token} on {self._chain}",
                              detail={"reason": "market_data_unreachable"})
        if idempotency_key:
            prior = self._idem_get(idempotency_key)
            if prior is not None:
                logger.info("chain %s idempotent replay for key %s (ticket %s)",
                            self._chain, idempotency_key, prior.get("ticket"))
                return prior
        if spend_usdc > self._balances["USDC"]:
            raise BrokerError(
                INVALID_ORDER,
                f"insufficient virtual USDC on {self._chain}: need "
                f"{spend_usdc:.6f}, have {self._balances['USDC']:.6f}")
        tokens = spend_usdc / price_usd
        h = self._holdings.get(token, {"tokens": 0.0, "avg_entry_usd": 0.0})
        new_tokens = h["tokens"] + tokens
        h["avg_entry_usd"] = (
            (h["tokens"] * h["avg_entry_usd"] + tokens * price_usd) / new_tokens
            if new_tokens > 0 else 0.0)
        h["tokens"] = new_tokens
        self._holdings[token] = h
        self._balances["USDC"] -= spend_usdc
        ticket = self._next()
        self._save()
        fill = {
            "ticket": ticket, "chain": self._chain, "token": token,
            "side": "BUY", "spend_usdc": spend_usdc, "tokens": tokens,
            "price_usd": price_usd,
            "balance_usdc_after": self._balances["USDC"],
            "ts": _utcnow_iso(), "mode": "paper",
            "idempotency_key": idempotency_key,
        }
        self._journal(fill)
        if idempotency_key:
            self._idem_put(idempotency_key, fill)
        logger.info("paper BUY %s on %s: %.6f USDC -> %.6f tokens @ %.6f USD "
                    "(ticket %s)", token, self._chain, spend_usdc, tokens,
                    price_usd, ticket)
        return fill

    def sell(self, token: str, worth_usdc: float, price_usd: float,
             idempotency_key: Optional[str] = None) -> dict:
        token = token.upper() if not token.startswith("0x") else token
        if worth_usdc <= 0:
            raise BrokerError(INVALID_ORDER,
                              f"swap amount must be positive, got {worth_usdc}")
        if not price_usd or price_usd <= 0:
            raise BrokerError(BROKER_UNAVAILABLE,
                              f"no usable quote for {token} on {self._chain}",
                              detail={"reason": "market_data_unreachable"})
        if idempotency_key:
            prior = self._idem_get(idempotency_key)
            if prior is not None:
                logger.info("chain %s idempotent replay for key %s (ticket %s)",
                            self._chain, idempotency_key, prior.get("ticket"))
                return prior
        h = self._holdings.get(token)
        if not h or h["tokens"] <= 1e-12:
            raise BrokerError(
                INVALID_ORDER,
                f"SELL {token} on {self._chain} rejected: no holdings to "
                f"reduce (spot market — shorting is unsupported)")
        tokens_to_sell = worth_usdc / price_usd
        if tokens_to_sell > h["tokens"] + 1e-9:
            raise BrokerError(
                INVALID_ORDER,
                f"SELL {token} on {self._chain} rejected: trying to sell "
                f"{tokens_to_sell:.6f} tokens but only {h['tokens']:.6f} held")
        tokens_to_sell = min(tokens_to_sell, h["tokens"])
        realized = (price_usd - h["avg_entry_usd"]) * tokens_to_sell
        h["tokens"] -= tokens_to_sell
        if h["tokens"] <= 1e-12:
            del self._holdings[token]
        proceeds = tokens_to_sell * price_usd
        self._balances["USDC"] += proceeds
        ticket = self._next()
        self._save()
        fill = {
            "ticket": ticket, "chain": self._chain, "token": token,
            "side": "SELL", "proceeds_usdc": proceeds,
            "tokens": tokens_to_sell, "price_usd": price_usd,
            "realized_pnl_usd": realized,
            "balance_usdc_after": self._balances["USDC"],
            "ts": _utcnow_iso(), "mode": "paper",
            "idempotency_key": idempotency_key,
        }
        self._journal(fill)
        if idempotency_key:
            self._idem_put(idempotency_key, fill)
        logger.info("paper SELL %s on %s: %.6f tokens @ %.6f USD, realized "
                    "%+.6f USD (ticket %s)", token, self._chain,
                    tokens_to_sell, price_usd, realized, ticket)
        return fill


class ChainAdapter(abc.ABC):
    """One adapter per chain. Subclasses set the class attributes and
    implement address validation + quote sourcing; the paper book, live
    gating, kill-switch coupling and status shape are shared here."""

    chain_name: str = ""
    chain_id: Optional[int] = None
    family: str = ""
    native_currency: str = ""

    def __init__(self, chain_def, *, data_dir: str, mode: str = "paper",
                 key_provider=None, armed: bool = False,
                 start_native: float = 1.0, start_usdc: float = 1000.0):
        if mode not in ("paper", "live"):
            raise BrokerError(
                CONFIG_INVALID,
                f"chain adapter mode must be paper|live, got {mode!r}")
        self._def = chain_def
        self._data_dir = Path(data_dir)
        self._mode = mode
        self._key_provider = key_provider
        self._armed = bool(armed)
        self._book = PaperBook(
            str(self._data_dir / "chains" / self.chain_name),
            self.chain_name, self.native_currency,
            start_native=start_native, start_usdc=start_usdc)

    # -- identity ------------------------------------------------------
    @property
    def quote_assets(self) -> List[str]:
        """Assets swaps settle in / are quoted against. USDC everywhere."""
        return ["USDC", self.native_currency]

    @abc.abstractmethod
    def validate_address(self, addr: str) -> bool:
        """True if addr is a plausible wallet/token address on this chain."""

    def validate_token(self, token: str) -> bool:
        """True if token is a plausible trade identifier on this chain.
        Defaults to address validation; chains with symbol-based markets
        (e.g. Hyperliquid) override this."""
        return self.validate_address(token)

    @abc.abstractmethod
    def get_quote(self, token: str) -> Optional[dict]:
        """Live price snapshot: {"token", "price_usd", "ts"[, ...]}.
        Returns None when unreachable — the caller turns that into
        BROKER_UNAVAILABLE (paper fills are never priced blind)."""

    # -- reads ---------------------------------------------------------
    def get_balance(self) -> dict:
        bal = self._book.balances()
        return {"native": bal["native"], "USDC": bal["USDC"],
                "currency": self.native_currency}

    def holdings(self) -> Dict[str, dict]:
        return self._book.holdings()

    # -- trading -------------------------------------------------------
    def _kill_switch_engaged(self) -> bool:
        return (self._data_dir / "killswitch").exists()

    def _require_live_ready(self) -> None:
        """Live gate: armed key required — and then STILL fail closed.

        The key is consulted transiently (truthiness only), never stored,
        never logged. At this stage there is no execution path, so the
        second raise always fires; the first exists so misconfiguration
        is reported as misconfiguration."""
        provider = self._key_provider
        key = provider.get_key(self.chain_name) if provider else None
        source = (provider.describe_source(self.chain_name)
                  if provider else "none (no key provider)")
        if not (self._armed and key):
            logger.warning("live %s submit refused: not armed or no wallet "
                           "key (source: %s)", self.chain_name, source)
            raise BrokerError(
                CREDENTIALS_INVALID,
                f"live {self.chain_name} trading requires arming "
                f"(live_armed) and a wallet key (source: {source})")
        logger.warning("live %s submit refused: execution not implemented "
                       "(paper-only stage)", self.chain_name)
        raise BrokerError(
            BROKER_UNAVAILABLE,
            f"live {self.chain_name} execution is not implemented — "
            f"this provider is paper-only by design",
            detail={"reason": "live_not_implemented",
                    "chain": self.chain_name})

    def submit_swap(self, token: str, side: str,
                    amount_usdc: float,
                    idempotency_key: Optional[str] = None) -> dict:
        """Paper swap (USDC-settled). Live always raises (fail-closed).

        token: chain-native trade identifier (contract address on EVM,
          mint on Solana, coin symbol on Hyperliquid).
        side: BUY (spend USDC) | SELL (USDC worth of holdings to sell).
        amount_usdc: USDC to spend (BUY) or USDC worth to sell (SELL).
        Returns the journaled fill dict."""
        if self._mode == "live":
            self._require_live_ready()
        if self._kill_switch_engaged():
            raise BrokerError(
                BROKER_UNAVAILABLE,
                f"{self.chain_name} kill-switch file is engaged — paper "
                f"swap refused",
                detail={"reason": "kill_switch_engaged",
                        "chain": self.chain_name})
        side = (side or "").upper()
        if side not in ("BUY", "SELL"):
            raise BrokerError(INVALID_ORDER,
                              f"side must be BUY|SELL, got {side!r}")
        if not token or not self.validate_token(token):
            raise BrokerError(INVALID_SYMBOL,
                              f"invalid token identifier {token!r} for chain "
                              f"{self.chain_name!r}")
        quote = self.get_quote(token)
        if not quote or not quote.get("price_usd"):
            raise BrokerError(
                BROKER_UNAVAILABLE,
                f"no market quote for {token} on {self.chain_name} — "
                f"paper swap refused",
                detail={"reason": "market_data_unreachable",
                        "chain": self.chain_name})
        price = float(quote["price_usd"])
        if side == "BUY":
            return self._book.buy(token, float(amount_usdc), price,
                                  idempotency_key)
        return self._book.sell(token, float(amount_usdc), price,
                               idempotency_key)

    # -- status ----------------------------------------------------------
    def broker_status_fragment(self) -> dict:
        """Never raises. No key material, ever."""
        frag = {
            "chain": self.chain_name,
            "family": self.family,
            "chain_id": self.chain_id,
            "mode": self._mode,
            "native_currency": self.native_currency,
            "quote_assets": self.quote_assets,
            "trading_available": False,
            "detail": {},
        }
        try:
            bal = self._book.balances()
            frag["detail"]["balance_native"] = round(bal["native"], 9)
            frag["detail"]["balance_usdc"] = round(bal["USDC"], 6)
            frag["detail"]["open_tokens"] = len(self._book.holdings())
            if self._mode == "live":
                frag["detail"]["reason"] = (
                    "live execution is not implemented — paper-only stage")
            elif self._kill_switch_engaged():
                frag["detail"]["kill_switch"] = "engaged (killswitch file present)"
            else:
                frag["trading_available"] = True
        except Exception as exc:  # defensive: status must never raise
            frag["detail"]["probe_error"] = str(exc)[:200]
        return frag

    def __repr__(self) -> str:  # never leak key material
        return (f"{type(self).__name__}(chain={self.chain_name!r}, "
                f"mode={self._mode!r})")
