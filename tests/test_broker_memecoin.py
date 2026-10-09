"""Memecoin provider tests: paper engine, idempotency, kill switch,
state persistence, honest degradation, live fail-closed, and config
validation.

Market data is a deterministic in-process double (a MemecoinMarketData
subclass overriding only the network call) — no network in this file.
Environment mutations are scoped per-test via _EnvGuard so nothing
leaks between tests.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker import (
    BROKER_NOT_CONNECTED,
    BROKER_UNAVAILABLE,
    CREDENTIALS_INVALID,
    INVALID_ORDER,
    INVALID_SYMBOL,
    RISK_LIMIT_EXCEEDED,
    BrokerError,
    OrderRequest,
)
from broker.memecoin.adapter import SOL_MINT, MemecoinAdapter
from broker.memecoin.transport import _TIMEFRAME_AGG, MemecoinMarketData

_TEST_MINTS = {
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
}


class _EnvGuard:
    """Save/restore os.environ keys; remove=() deletes keys temporarily."""

    def __init__(self, remove=(), **values):
        self.values = values
        self.remove = tuple(remove)
        self.saved = {}

    def __enter__(self):
        for key in list(self.values) + list(self.remove):
            self.saved[key] = os.environ.get(key)
        for key in self.remove:
            os.environ.pop(key, None)
        os.environ.update(self.values)
        return self

    def __exit__(self, *exc):
        for key, old in self.saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        return False


class FakeMarket(MemecoinMarketData):
    """Deterministic market-data double: USD prices, no network."""

    def __init__(self, usd_prices=None, symbols=None):
        super().__init__(symbols)
        self.usd = dict(usd_prices or {"SOL": 100.0})
        self.fail = False
        self.quote_calls = 0

    def get_quote(self, symbol):
        self.quote_calls += 1
        if self.fail:
            return None
        if not self.mint_for(symbol):
            return None
        px = self.usd.get(symbol.upper())
        if px is None:
            return None
        return {"symbol": symbol.upper(), "mint": "fake-mint",
                "price_usd": px, "price_sol": None,
                "pair": None, "ts": None}

    def get_candles(self, symbol, timeframe, count):
        if self.fail:
            raise BrokerError(BROKER_UNAVAILABLE, "market data down")
        return []


def _req(symbol, direction, volume, key=None):
    return OrderRequest(symbol=symbol, direction=direction, volume=volume,
                        stop_loss=0.5, take_profit=2.0,
                        signal_id="sig-test", idempotency_key=key)


def _adapter(tmp, market=None, **kw):
    kw.setdefault("data_dir", os.path.join(tmp, "memecoin"))
    kw.setdefault("mode", "paper")
    kw.setdefault("market_data",
                  market if market is not None else FakeMarket())
    kw.setdefault("symbols", dict(_TEST_MINTS))
    return MemecoinAdapter(**kw)


class TestMemecoinPaperEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forex_memecoin_")
        # BONK @ $0.0001 with SOL @ $100  =>  1e-6 SOL per BONK.
        self.market = FakeMarket({"SOL": 100.0, "BONK": 0.0001,
                                  "WIF": 0.001})

    def test_paper_connect_needs_no_credentials(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        h = a.health()
        self.assertTrue(h.connected)
        self.assertEqual(h.adapter, "memecoin")

    def test_default_virtual_balance_ten_sol(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        acct = a.account_info()
        self.assertEqual(acct.balance, 10.0)
        self.assertEqual(acct.currency, "SOL")
        self.assertEqual(acct.equity, 10.0)

    def test_custom_start_balance(self):
        a = _adapter(self.tmp, self.market, paper_start_sol=25.0)
        a.connect({})
        self.assertEqual(a.account_info().balance, 25.0)

    def test_paper_buy_fills_and_journals(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        res = a.submit_order(_req("BONK", "BUY", 2.0, key="k-1"))
        self.assertEqual(res.price, 0.000_001)
        self.assertEqual(res.volume, 2.0)  # volume is SOL-denominated
        self.assertEqual(res.raw["tokens"], 2_000_000.0)
        self.assertEqual(a.account_info().balance, 8.0)
        pos = a.positions()
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0].symbol, "BONK")
        # JSONL fill journal has exactly one line.
        with open(os.path.join(a._data_dir, "fills.jsonl")) as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["ticket"], res.ticket)
        self.assertEqual(entry["symbol"], "BONK")
        self.assertEqual(entry["kind"], "BUY")
        self.assertEqual(entry["idempotency_key"], "k-1")

    def test_paper_sell_fifo_reduces_exposure(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        a.submit_order(_req("BONK", "BUY", 2.0, key="k-1"))
        res = a.submit_order(_req("BONK", "SELL", 0.5, key="k-2"))
        self.assertEqual(res.price, 0.000_001)
        # SELL volume is SOL-denominated: 0.5 SOL worth at 1e-6 -> 500k tokens.
        self.assertEqual(res.raw["tokens"], 500_000.0)
        pos = a.positions()
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0].volume, 1.5)  # remaining cost basis, SOL
        self.assertEqual(a.account_info().balance, 8.5)
        with open(os.path.join(a._data_dir, "fills.jsonl")) as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)

    def test_naked_sell_rejected(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        with self.assertRaises(BrokerError) as ctx:
            a.submit_order(_req("BONK", "SELL", 1.0))
        self.assertEqual(ctx.exception.code, INVALID_ORDER)
        self.assertEqual(a.account_info().balance, 10.0)

    def test_insufficient_balance_rejected(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        with self.assertRaises(BrokerError) as ctx:
            a.submit_order(_req("BONK", "BUY", 11.0))
        self.assertEqual(ctx.exception.code, RISK_LIMIT_EXCEEDED)

    def test_unknown_symbol_invalid(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        with self.assertRaises(BrokerError) as ctx:
            a.submit_order(_req("NOPE", "BUY", 1.0))
        self.assertEqual(ctx.exception.code, INVALID_SYMBOL)

    def test_stop_loss_required(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        bad = OrderRequest(symbol="BONK", direction="BUY", volume=1.0,
                           stop_loss=None, take_profit=2.0)
        with self.assertRaises(BrokerError) as ctx:
            a.submit_order(bad)
        self.assertEqual(ctx.exception.code, INVALID_ORDER)

    def test_sol_always_registered(self):
        a = _adapter(self.tmp, self.market, symbols={})
        a.connect({})
        self.assertIn("SOL", [s.name for s in a.symbols()])
        self.assertEqual(a._market.mint_for("SOL"), SOL_MINT)

    def test_orders_always_empty(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        a.submit_order(_req("BONK", "BUY", 1.0, key="k-1"))
        self.assertEqual(a.orders(), [])

    def test_equity_reflects_market_value(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        a.submit_order(_req("BONK", "BUY", 2.0, key="k-1"))
        self.market.usd["BONK"] = 0.0002  # price doubles in USD terms
        acct = a.account_info()
        self.assertEqual(acct.balance, 8.0)    # realized cash unchanged
        self.assertEqual(acct.equity, 12.0)    # 8 cash + 4 position value

    def test_modify_position_sl_tp(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        res = a.submit_order(_req("BONK", "BUY", 2.0, key="k-1"))
        self.assertIsNone(a.modify_order(res.ticket, 0.5, 2.0))
        pos = a.positions()[0]
        self.assertEqual(pos.sl, 0.5)
        self.assertEqual(pos.tp, 2.0)
        with self.assertRaises(BrokerError) as ctx:
            a.modify_order(999999, 1.0, 2.0)
        self.assertEqual(ctx.exception.code, INVALID_ORDER)

    def test_close_position_sells_all_at_live_quote(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        res = a.submit_order(_req("BONK", "BUY", 2.0, key="k-1"))
        self.market.usd["BONK"] = 0.0002
        close_px = a.close_position(res.ticket)
        self.assertEqual(close_px, 0.000_002)
        self.assertEqual(a.positions(), [])
        # 8 cash + 2M tokens * 2e-6 = 12 SOL realized.
        self.assertEqual(a.account_info().balance, 12.0)

    def test_close_position_quote_failure_keeps_position(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        res = a.submit_order(_req("BONK", "BUY", 2.0, key="k-1"))
        self.market.fail = True
        with self.assertRaises(BrokerError) as ctx:
            a.close_position(res.ticket)
        self.assertEqual(ctx.exception.code, BROKER_UNAVAILABLE)
        self.assertEqual(len(a.positions()), 1)  # position NOT lost

    def test_deal_history_reads_journal(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        a.submit_order(_req("BONK", "BUY", 1.0, key="k-1"))
        a.submit_order(_req("WIF", "BUY", 1.0, key="k-2"))
        deals = a.deal_history(datetime(2000, 1, 1, tzinfo=timezone.utc),
                               datetime(2100, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(len(deals), 2)
        self.assertEqual({d.symbol for d in deals}, {"BONK", "WIF"})

    def test_state_persists_across_restart(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        a.submit_order(_req("BONK", "BUY", 3.0, key="k-1"))
        b = _adapter(self.tmp, self.market)
        b.connect({})
        self.assertEqual(b.account_info().balance, 7.0)
        self.assertEqual(len(b.positions()), 1)
        self.assertEqual(b.positions()[0].symbol, "BONK")
        self.assertEqual(len(b.deal_history(
            datetime(2000, 1, 1, tzinfo=timezone.utc),
            datetime(2100, 1, 1, tzinfo=timezone.utc))), 1)

    def test_not_connected_reads_raise(self):
        a = _adapter(self.tmp, self.market)
        with self.assertRaises(BrokerError) as ctx:
            a.account_info()
        self.assertEqual(ctx.exception.code, BROKER_NOT_CONNECTED)


class TestMemecoinIdempotency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forex_memecoin_idem_")
        self.market = FakeMarket({"SOL": 100.0, "BONK": 0.0001})

    def test_replay_returns_original_result_no_duplicate_fill(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        r1 = a.submit_order(_req("BONK", "BUY", 2.0, key="dup-key"))
        r2 = a.submit_order(_req("BONK", "BUY", 2.0, key="dup-key"))
        self.assertEqual(r1.ticket, r2.ticket)
        self.assertEqual(r1.price, r2.price)
        self.assertEqual(a.account_info().balance, 8.0)  # charged once
        with open(os.path.join(a._data_dir, "fills.jsonl")) as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)  # one fill, not two
        self.assertEqual(len(a.positions()), 1)

    def test_replay_survives_restart(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        r1 = a.submit_order(_req("BONK", "BUY", 2.0, key="k-9"))
        b = _adapter(self.tmp, self.market)
        b.connect({})
        r2 = b.submit_order(_req("BONK", "BUY", 2.0, key="k-9"))
        self.assertEqual(r1.ticket, r2.ticket)
        self.assertEqual(b.account_info().balance, 8.0)
        with open(os.path.join(b._data_dir, "fills.jsonl")) as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)

    def test_crash_between_journal_and_idempotency_store_recovers(self):
        # Simulate a crash after the fill was journaled but before the
        # idempotency store was written: delete idempotency.json.
        a = _adapter(self.tmp, self.market)
        a.connect({})
        r1 = a.submit_order(_req("BONK", "BUY", 2.0, key="k-crash"))
        os.remove(os.path.join(a._data_dir, "idempotency.json"))
        b = _adapter(self.tmp, self.market)
        b.connect({})
        r2 = b.submit_order(_req("BONK", "BUY", 2.0, key="k-crash"))
        self.assertEqual(r1.ticket, r2.ticket)
        self.assertTrue(r2.raw.get("recovered"))
        self.assertEqual(b.account_info().balance, 8.0)  # not charged twice
        with open(os.path.join(b._data_dir, "fills.jsonl")) as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)

    def test_different_keys_fill_twice(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        a.submit_order(_req("BONK", "BUY", 1.0, key="k-a"))
        a.submit_order(_req("BONK", "BUY", 1.0, key="k-b"))
        self.assertEqual(len(a.positions()), 2)
        self.assertEqual(a.account_info().balance, 8.0)


class TestMemecoinKillSwitch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forex_memecoin_ks_")
        self.market = FakeMarket({"SOL": 100.0, "BONK": 0.0001})

    def test_kill_switch_file_blocks_submit(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        open(os.path.join(a._data_dir, "killswitch"), "w").write("halt")
        with self.assertRaises(BrokerError) as ctx:
            a.submit_order(_req("BONK", "BUY", 1.0))
        self.assertEqual(ctx.exception.code, BROKER_UNAVAILABLE)
        self.assertEqual(ctx.exception.detail.get("reason"),
                         "kill_switch_engaged")
        # Nothing was charged or journaled.
        self.assertEqual(a.account_info().balance, 10.0)
        self.assertFalse(os.path.exists(os.path.join(a._data_dir,
                                                     "fills.jsonl")))

    def test_kill_switch_allows_risk_reducing_close(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        res = a.submit_order(_req("BONK", "BUY", 2.0, key="k-1"))
        open(os.path.join(a._data_dir, "killswitch"), "w").write("halt")
        close_px = a.close_position(res.ticket)
        self.assertEqual(close_px, 0.000_001)
        self.assertEqual(a.positions(), [])

    def test_kill_switch_reported_in_status(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        open(os.path.join(a._data_dir, "killswitch"), "w").write("halt")
        st = a.broker_status()["broker"]
        self.assertFalse(st["trading_available"])
        self.assertIn("kill_switch", st["detail"])

    def test_no_kill_switch_allows_trading(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        st = a.broker_status()["broker"]
        self.assertTrue(st["trading_available"])
        self.assertNotIn("kill_switch", st["detail"])


class TestMemecoinHonestDegradation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forex_memecoin_deg_")
        self.market = FakeMarket({"SOL": 100.0, "BONK": 0.0001})

    def test_market_data_outage_refuses_fill(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        self.market.fail = True
        with self.assertRaises(BrokerError) as ctx:
            a.submit_order(_req("BONK", "BUY", 1.0))
        self.assertEqual(ctx.exception.code, BROKER_UNAVAILABLE)
        self.assertEqual(ctx.exception.detail.get("reason"),
                         "market_data_unreachable")
        # No invented price: balance untouched, nothing journaled.
        self.assertEqual(a.account_info().balance, 10.0)
        self.assertFalse(os.path.exists(os.path.join(a._data_dir,
                                                     "fills.jsonl")))

    def test_market_data_outage_degrades_status_honestly(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        self.market.fail = True
        st = a.broker_status()["broker"]
        self.assertFalse(st["market_data_available"])
        self.assertIn("market_data", st["detail"])
        # Account reads still work (local state).
        self.assertTrue(st["account_available"])

    def test_status_fields_shape(self):
        a = _adapter(self.tmp, self.market)
        a.connect({})
        st = a.broker_status()["broker"]
        for field in ("provider", "mode", "configured", "reachable",
                      "connected", "account_available",
                      "market_data_available", "trading_available", "detail"):
            self.assertIn(field, st, "missing broker_status field %r" % field)
        self.assertEqual(st["provider"], "memecoin")
        self.assertEqual(st["mode"], "paper")
        self.assertEqual(st["detail"]["balance_sol"], 10.0)


class TestMemecoinLiveFailClosed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forex_memecoin_live_")

    def test_live_connect_refuses_without_arming(self):
        a = _adapter(self.tmp, FakeMarket(), mode="live")
        with self.assertRaises(BrokerError) as ctx:
            a.connect({})
        self.assertEqual(ctx.exception.code, CREDENTIALS_INVALID)

    def test_live_connect_refuses_even_when_armed(self):
        a = _adapter(self.tmp, FakeMarket(), mode="live", live_armed=True,
                     wallet_pubkey="TestPubkey11111111111111111111111111111111")
        with self.assertRaises(BrokerError) as ctx:
            a.connect({})
        self.assertEqual(ctx.exception.code, BROKER_UNAVAILABLE)
        self.assertEqual(ctx.exception.detail.get("reason"),
                         "live_not_implemented")

    def test_live_submit_defensively_refused(self):
        a = _adapter(self.tmp, FakeMarket(), mode="live", live_armed=True,
                     wallet_pubkey="TestPubkey11111111111111111111111111111111")
        # connect() refuses live, so submit dies at _ensure() — either way
        # it is refused, never executed.
        with self.assertRaises(BrokerError) as ctx:
            a.submit_order(_req("SOL", "BUY", 1.0))
        self.assertIn(ctx.exception.code,
                      (BROKER_NOT_CONNECTED, BROKER_UNAVAILABLE))


class TestMemecoinTransport(unittest.TestCase):
    def test_timeframe_granularity_mapping(self):
        self.assertEqual(_TIMEFRAME_AGG["M1"], "minute")
        self.assertEqual(_TIMEFRAME_AGG["M15"], "minute")
        self.assertEqual(_TIMEFRAME_AGG["H1"], "hour")
        self.assertEqual(_TIMEFRAME_AGG["D1"], "day")
        self.assertNotIn("W1", _TIMEFRAME_AGG)

    def test_unknown_symbol_returns_none_without_network(self):
        md = MemecoinMarketData()
        self.assertIsNone(md.get_quote("NOPE"))

    def test_unknown_symbol_candles_raise(self):
        md = MemecoinMarketData()
        with self.assertRaises(BrokerError) as ctx:
            md.get_candles("NOPE", "M15", 10)
        self.assertEqual(ctx.exception.code, BROKER_UNAVAILABLE)

    def test_unsupported_timeframe_raises(self):
        md = MemecoinMarketData({"BONK": "mint-1"})
        with self.assertRaises(BrokerError) as ctx:
            md.get_candles("BONK", "W1", 10)
        self.assertEqual(ctx.exception.code, BROKER_UNAVAILABLE)

    def test_symbol_registry_case_insensitive(self):
        md = MemecoinMarketData({"BONK": "mint-1"})
        self.assertEqual(md.mint_for("bonk"), "mint-1")
        md.register("SOL", SOL_MINT)
        self.assertEqual(md.mint_for("sol"), SOL_MINT)
        self.assertIsNone(md.mint_for("NOPE"))
        self.assertIn("BONK", md.known_symbols())


class TestMemecoinConfig(unittest.TestCase):
    def test_default_provider_is_memecoin(self):
        with _EnvGuard(remove=("BROKER_PROVIDER",)):
            from config.config import AppConfig
            cfg = AppConfig()
            self.assertEqual(cfg.broker.provider, "memecoin")

    def test_unknown_provider_rejected(self):
        with _EnvGuard(BROKER_PROVIDER="bogus_provider"):
            from config.config import AppConfig
            with self.assertRaisesRegex(RuntimeError, "CONFIG_INVALID"):
                AppConfig()

    def test_disconnected_provider_accepted(self):
        with _EnvGuard(BROKER_PROVIDER="disconnected"):
            from config.config import AppConfig
            cfg = AppConfig()
            self.assertEqual(cfg.broker.provider, "disconnected")

    def test_live_mode_requires_arming(self):
        with _EnvGuard(BROKER_PROVIDER="memecoin", MODE="live",
                       remove=("DRY_RUN",)):
            from config.config import AppConfig
            with self.assertRaisesRegex(RuntimeError, "CREDENTIALS_INVALID"):
                AppConfig()

    def test_live_mode_armed_config_constructs(self):
        with _EnvGuard(BROKER_PROVIDER="memecoin", MODE="live",
                       MEMECOIN_LIVE_ARMED="true",
                       MEMECOIN_WALLET_PUBKEY="TestPubkey11111111111111111111111111111111",
                       remove=("DRY_RUN",)):
            from config.config import AppConfig
            cfg = AppConfig()
            self.assertTrue(cfg.broker.memecoin.live_armed)
            self.assertEqual(cfg.mode, "live")

    def test_paper_defaults(self):
        with _EnvGuard(remove=("BROKER_PROVIDER", "DRY_RUN", "MODE",
                               "MEMECOIN_DATA_DIR", "MEMECOIN_PAPER_START_SOL",
                               "MEMECOIN_LIVE_ARMED", "MEMECOIN_WALLET_PUBKEY")):
            from config.config import AppConfig
            cfg = AppConfig()
            self.assertEqual(cfg.mode, "dry_run")
            self.assertEqual(cfg.broker.memecoin.paper_start_sol, 10.0)
            self.assertFalse(cfg.broker.memecoin.live_armed)

    def test_no_private_key_fields_anywhere(self):
        import dataclasses
        import inspect

        from broker.memecoin.adapter import MemecoinAdapter
        from config.config import MemecoinConfig
        for f in dataclasses.fields(MemecoinConfig):
            for needle in ("private", "secret", "seed", "mnemonic"):
                self.assertNotIn(needle, f.name.lower(),
                                 "secret-looking config field %r" % f.name)
        # Adapter constructor takes no key material either.
        params = inspect.signature(MemecoinAdapter.__init__).parameters
        for name in params:
            for needle in ("private", "secret", "seed", "mnemonic"):
                self.assertNotIn(needle, name.lower(),
                                 "secret-looking adapter param %r" % name)


class TestBackendWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forex_memecoin_wire_")

    def tearDown(self):
        from agent.tools import backend
        backend.reset_adapter_cache()

    def test_backend_builds_memecoin_adapter_by_default(self):
        from agent.tools import backend
        with _EnvGuard(BROKER_PROVIDER="memecoin",
                       FOREX_AGENT_HOME=self.tmp,
                       MEMECOIN_DATA_DIR=os.path.join(self.tmp, "meme-data"),
                       remove=("DRY_RUN", "MODE")):
            backend.reset_adapter_cache()
            adapter = backend._build_adapter()
            self.assertIsInstance(adapter, MemecoinAdapter)
            self.assertTrue(adapter.health().connected)
            self.assertEqual(adapter.account_info().balance, 10.0)

    def test_backend_disconnected_provider(self):
        from agent.tools import backend
        from broker.disconnected import DisconnectedAdapter
        with _EnvGuard(BROKER_PROVIDER="disconnected",
                       FOREX_AGENT_HOME=self.tmp):
            backend.reset_adapter_cache()
            adapter = backend._build_adapter()
            self.assertIsInstance(adapter, DisconnectedAdapter)


if __name__ == "__main__":
    unittest.main()
