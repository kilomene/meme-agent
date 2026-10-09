"""Multi-chain memecoin tests: registry, chain adapters (paper engine),
wallet key providers, key hygiene, live fail-closed, and the
MemecoinAdapter chain plumbing.

No network in this file: every chain adapter is constructed with
injected `quotes` (dict), never the default public sources. Dummy hex
keys only — nothing real. Environment mutations are scoped per-test
via _EnvGuard so nothing leaks between tests.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker import (
    BROKER_UNAVAILABLE,
    CONFIG_INVALID,
    CREDENTIALS_INVALID,
    INVALID_ORDER,
    INVALID_SYMBOL,
    BrokerError,
    OrderRequest,
)
from broker.memecoin.adapter import MemecoinAdapter
from broker.memecoin.chains import (
    CHAIN_REGISTRY,
    chain_names,
    get_chain_adapter,
    get_chain_def,
)
from broker.memecoin.chains.evm import EvmChainAdapter
from broker.memecoin.chains.hyperliquid import HyperliquidAdapter
from broker.memecoin.chains.solana import SolanaChainAdapter
from broker.memecoin.transport import MemecoinMarketData
from broker.memecoin.wallet import (
    EnvKeyProvider,
    FileKeyProvider,
    NullKeyProvider,
    build_key_provider,
)

# Dummy key material for tests — never a real secret.
_DUMMY_KEY = "0x" + "ab" * 32
_DUMMY_KEY_2 = "0x" + "cd" * 32
_TOKEN = "0x" + "11" * 20  # valid EVM address shape


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


class _FakeMarket(MemecoinMarketData):
    """Deterministic USD-price double for the native Solana engine."""

    def __init__(self, usd_prices):
        super().__init__()
        self.usd = dict(usd_prices)

    def get_quote(self, symbol):
        if not self.mint_for(symbol):
            return None
        px = self.usd.get(symbol.upper())
        if px is None:
            return None
        return {"symbol": symbol.upper(), "price_usd": px}


def _evm(name, tmp, quotes=None, **kw):
    kw.setdefault("data_dir", os.path.join(tmp, "mc"))
    kw.setdefault("mode", "paper")
    kw.setdefault("quotes", {_TOKEN.lower(): 2.0} if quotes is None else quotes)
    return EvmChainAdapter(get_chain_def(name), **kw)


def _req(symbol, direction="BUY", volume=100.0, key=None):
    return OrderRequest(symbol=symbol, direction=direction, volume=volume,
                        stop_loss=0.5, take_profit=2.0,
                        signal_id="sig-mc", idempotency_key=key)


# ---------------------------------------------------------------------------
class TestRegistry(unittest.TestCase):
    def test_registry_has_20_plus_chains(self):
        self.assertGreaterEqual(len(CHAIN_REGISTRY), 20)
        required = {"ethereum", "base", "arbitrum", "optimism", "polygon",
                    "bnb_chain", "avalanche", "linea", "blast", "zksync",
                    "scroll", "mantle", "berachain", "sei", "monad",
                    "hyperevm", "solana", "hyperliquid", "sui", "aptos"}
        self.assertTrue(required <= set(chain_names()),
                        f"missing: {required - set(chain_names())}")

    def test_each_def_has_required_fields(self):
        for cdef in CHAIN_REGISTRY:
            self.assertTrue(cdef.name and cdef.name == cdef.name.lower())
            self.assertIn(cdef.family,
                          {"evm", "solana", "hyperliquid", "sui", "aptos"})
            self.assertTrue(cdef.native_currency)
            self.assertTrue(cdef.rpc_env_var.startswith("MEMECOIN_RPC_"))
            self.assertTrue(cdef.explorer_url.startswith("https://"))
            if cdef.family == "evm":
                self.assertIsNotNone(cdef.chain_id)
                self.assertEqual(cdef.adapter, "evm")

    def test_unknown_chain_rejected(self):
        with self.assertRaises(BrokerError) as ctx:
            get_chain_def("narnia")
        self.assertEqual(ctx.exception.code, CONFIG_INVALID)
        self.assertIn("base", str(ctx.exception))

    def test_unimplemented_family_rejected_clearly(self):
        tmp = tempfile.mkdtemp(prefix="forex_mc_")
        for name in ("sui", "aptos"):
            with self.assertRaises(BrokerError) as ctx:
                get_chain_adapter(name, data_dir=tmp)
            self.assertEqual(ctx.exception.code, CONFIG_INVALID)
            self.assertIn("no chain adapter", str(ctx.exception))

    def test_quote_assets_include_usdc_everywhere(self):
        tmp = tempfile.mkdtemp(prefix="forex_mc_")
        for name in ("base", "ethereum", "solana", "hyperliquid"):
            a = get_chain_adapter(name, data_dir=os.path.join(tmp, name),
                                  quotes={} if name != "solana" else None)
            self.assertIn("USDC", a.quote_assets)


# ---------------------------------------------------------------------------
class TestEvmPaperEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forex_mc_")

    def test_base_paper_buy_and_sell(self):
        a = _evm("base", self.tmp)
        self.assertEqual(a.chain_id, 8453)
        self.assertEqual(a.native_currency, "ETH")
        fill = a.submit_swap(_TOKEN, "BUY", 100.0)
        self.assertEqual(fill["ticket"], 1)
        self.assertEqual(fill["chain"], "base")
        self.assertAlmostEqual(fill["tokens"], 50.0)  # 100 USDC @ $2
        bal = a.get_balance()
        self.assertAlmostEqual(bal["USDC"], 900.0)
        self.assertEqual(bal["currency"], "ETH")
        sell = a.submit_swap(_TOKEN, "SELL", 40.0)
        self.assertAlmostEqual(sell["proceeds_usdc"], 40.0)
        self.assertAlmostEqual(sell["realized_pnl_usd"], 0.0)
        self.assertAlmostEqual(a.get_balance()["USDC"], 940.0)
        # Journal: one line per fill, per chain.
        lines = open(os.path.join(self.tmp, "mc", "chains", "base",
                                  "fills.jsonl")).read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["side"], "BUY")

    def test_ethereum_paper_buy(self):
        a = _evm("ethereum", self.tmp)
        fill = a.submit_swap(_TOKEN, "BUY", 50.0)
        self.assertEqual(fill["chain"], "ethereum")
        self.assertAlmostEqual(fill["tokens"], 25.0)

    def test_naked_sell_rejected(self):
        a = _evm("base", self.tmp)
        with self.assertRaises(BrokerError) as ctx:
            a.submit_swap(_TOKEN, "SELL", 10.0)
        self.assertEqual(ctx.exception.code, INVALID_ORDER)

    def test_invalid_token_rejected(self):
        a = _evm("base", self.tmp)
        with self.assertRaises(BrokerError) as ctx:
            a.submit_swap("not-an-address", "BUY", 10.0)
        self.assertEqual(ctx.exception.code, INVALID_SYMBOL)

    def test_no_quote_refuses_fill(self):
        a = _evm("base", self.tmp, quotes={})
        with self.assertRaises(BrokerError) as ctx:
            a.submit_swap(_TOKEN, "BUY", 10.0)
        self.assertEqual(ctx.exception.code, BROKER_UNAVAILABLE)

    def test_idempotent_replay(self):
        a = _evm("base", self.tmp)
        f1 = a.submit_swap(_TOKEN, "BUY", 100.0, idempotency_key="k-1")
        f2 = a.submit_swap(_TOKEN, "BUY", 100.0, idempotency_key="k-1")
        self.assertEqual(f1["ticket"], f2["ticket"])
        self.assertAlmostEqual(a.get_balance()["USDC"], 900.0)  # charged once
        lines = open(os.path.join(self.tmp, "mc", "chains", "base",
                                  "fills.jsonl")).read().splitlines()
        self.assertEqual(len(lines), 1)

    def test_kill_switch_blocks_chain_swaps(self):
        a = _evm("base", self.tmp)
        open(os.path.join(self.tmp, "mc", "killswitch"), "w").close()
        with self.assertRaises(BrokerError) as ctx:
            a.submit_swap(_TOKEN, "BUY", 10.0)
        self.assertEqual(ctx.exception.code, BROKER_UNAVAILABLE)
        self.assertFalse(a.broker_status_fragment()["trading_available"])

    def test_validate_address(self):
        a = _evm("base", self.tmp)
        self.assertTrue(a.validate_address(_TOKEN))
        self.assertFalse(a.validate_address("0x123"))
        self.assertFalse(a.validate_address("not-an-address"))
        self.assertFalse(a.validate_address(""))


# ---------------------------------------------------------------------------
class TestSolanaAndHyperliquid(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forex_mc_")

    def test_solana_chain_paper_swap(self):
        mint = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
        a = SolanaChainAdapter(
            None, data_dir=os.path.join(self.tmp, "mc"), mode="paper",
            quotes={mint: 0.000003})
        fill = a.submit_swap(mint, "BUY", 30.0)
        self.assertEqual(fill["chain"], "solana")
        self.assertAlmostEqual(fill["tokens"], 30.0 / 0.000003)
        self.assertAlmostEqual(a.get_balance()["USDC"], 970.0)
        self.assertTrue(a.validate_address(mint))
        self.assertFalse(a.validate_address("0x123"))

    def test_hyperliquid_paper_swap(self):
        a = HyperliquidAdapter(
            None, data_dir=os.path.join(self.tmp, "mc"), mode="paper",
            quotes={"HYPE": 25.0})
        self.assertEqual(a.native_currency, "HYPE")
        self.assertTrue(a.validate_token("HYPE"))
        self.assertTrue(a.validate_address("0x" + "ab" * 20))
        self.assertFalse(a.validate_token("not a coin!!"))
        fill = a.submit_swap("HYPE", "BUY", 100.0)
        self.assertEqual(fill["chain"], "hyperliquid")
        self.assertAlmostEqual(fill["tokens"], 4.0)
        q = a.get_quote("hype")  # case-insensitive
        self.assertAlmostEqual(q["price_usd"], 25.0)


# ---------------------------------------------------------------------------
class TestWalletProviders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forex_wallet_")

    def test_null_provider(self):
        self.assertIsNone(NullKeyProvider().get_key("base"))

    def test_env_provider_chain_specific_then_fallback(self):
        p = EnvKeyProvider()
        with _EnvGuard(remove=("MEMECOIN_PRIVATE_KEY_BASE",
                               "MEMECOIN_PRIVATE_KEY")):
            self.assertIsNone(p.get_key("base"))
        with _EnvGuard(MEMECOIN_PRIVATE_KEY=_DUMMY_KEY_2):
            self.assertEqual(p.get_key("base"), _DUMMY_KEY_2)
        with _EnvGuard(MEMECOIN_PRIVATE_KEY_BASE=_DUMMY_KEY,
                       MEMECOIN_PRIVATE_KEY=_DUMMY_KEY_2):
            self.assertEqual(p.get_key("base"), _DUMMY_KEY)
            self.assertEqual(p.get_key("ethereum"), _DUMMY_KEY_2)

    def test_env_provider_empty_counts_as_unset(self):
        with _EnvGuard(MEMECOIN_PRIVATE_KEY_BASE="   ",
                       MEMECOIN_PRIVATE_KEY=""):
            self.assertIsNone(EnvKeyProvider().get_key("base"))

    def _key_file(self, content, mode=0o600):
        path = os.path.join(self.tmp, "wallet.key")
        with open(path, "w") as fh:
            fh.write(content)
        os.chmod(path, mode)
        return path

    def test_file_provider_raw_key(self):
        path = self._key_file(_DUMMY_KEY + "\n")
        self.assertEqual(FileKeyProvider(path).get_key("base"), _DUMMY_KEY)

    def test_file_provider_key_value_format(self):
        path = self._key_file('MEMECOIN_PRIVATE_KEY="%s"\n' % _DUMMY_KEY)
        self.assertEqual(FileKeyProvider(path).get_key("base"), _DUMMY_KEY)

    def test_file_provider_refuses_group_readable(self):
        path = self._key_file(_DUMMY_KEY, mode=0o644)
        with self.assertRaises(BrokerError) as ctx:
            FileKeyProvider(path).get_key("base")
        self.assertEqual(ctx.exception.code, CREDENTIALS_INVALID)
        self.assertIn("0600", str(ctx.exception))

    def test_file_provider_refuses_world_readable(self):
        path = self._key_file(_DUMMY_KEY, mode=0o604)
        with self.assertRaises(BrokerError):
            FileKeyProvider(path).get_key("base")

    def test_file_provider_missing_file_returns_none(self):
        self.assertIsNone(
            FileKeyProvider(os.path.join(self.tmp, "nope.key")).get_key("base"))

    def test_build_key_provider(self):
        self.assertIsInstance(build_key_provider("none"), NullKeyProvider)
        self.assertIsInstance(build_key_provider("env"), EnvKeyProvider)
        path = self._key_file(_DUMMY_KEY)
        self.assertIsInstance(build_key_provider("file", path), FileKeyProvider)
        with self.assertRaises(BrokerError) as ctx:
            build_key_provider("file", "")
        self.assertEqual(ctx.exception.code, CONFIG_INVALID)
        with self.assertRaises(BrokerError):
            build_key_provider("bogus")


# ---------------------------------------------------------------------------
class TestKeyHygiene(unittest.TestCase):
    """Key material must never appear in logs, exceptions, or repr."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forex_wallet_")

    def test_repr_never_contains_key(self):
        with _EnvGuard(MEMECOIN_PRIVATE_KEY=_DUMMY_KEY):
            p = EnvKeyProvider()
            p.get_key("base")  # load it, then check repr
            self.assertNotIn(_DUMMY_KEY, repr(p))
        fp = FileKeyProvider(os.path.join(self.tmp, "k.key"))
        self.assertNotIn(_DUMMY_KEY, repr(fp))
        a = _evm("base", self.tmp)
        self.assertNotIn(_DUMMY_KEY, repr(a))

    def test_logs_never_contain_key(self):
        with _EnvGuard(MEMECOIN_PRIVATE_KEY_BASE=_DUMMY_KEY):
            provider = EnvKeyProvider()
            a = EvmChainAdapter(
                get_chain_def("base"), data_dir=os.path.join(self.tmp, "mc"),
                mode="live", key_provider=provider, armed=True,
                quotes={_TOKEN.lower(): 2.0})
            with self.assertLogs("broker.memecoin", level="INFO") as logs:
                # Paper swap with a key configured (key unused in paper).
                a_paper = EvmChainAdapter(
                    get_chain_def("base"),
                    data_dir=os.path.join(self.tmp, "mc2"), mode="paper",
                    key_provider=provider,
                    quotes={_TOKEN.lower(): 2.0})
                a_paper.submit_swap(_TOKEN, "BUY", 10.0)
                # Live refusal (armed + key, still fail-closed).
                with self.assertRaises(BrokerError):
                    a.submit_swap(_TOKEN, "BUY", 10.0)
        blob = "\n".join(logs.output)
        self.assertNotIn(_DUMMY_KEY, blob)

    def test_exceptions_never_contain_key(self):
        path = os.path.join(self.tmp, "wallet.key")
        with open(path, "w") as fh:
            fh.write(_DUMMY_KEY)
        os.chmod(path, 0o644)
        with _EnvGuard(MEMECOIN_PRIVATE_KEY=_DUMMY_KEY):
            with self.assertRaises(BrokerError) as ctx:
                FileKeyProvider(path).get_key("base")
        self.assertNotIn(_DUMMY_KEY, str(ctx.exception))
        self.assertNotIn(_DUMMY_KEY, repr(ctx.exception))


# ---------------------------------------------------------------------------
class TestLiveFailClosed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forex_mc_")

    def _live(self, **kw):
        kw.setdefault("data_dir", os.path.join(self.tmp, "mc"))
        kw.setdefault("mode", "live")
        kw.setdefault("quotes", {_TOKEN.lower(): 2.0})
        return EvmChainAdapter(get_chain_def("base"), **kw)

    def test_live_without_key_refused_as_credentials(self):
        a = self._live(key_provider=NullKeyProvider(), armed=False)
        with self.assertRaises(BrokerError) as ctx:
            a.submit_swap(_TOKEN, "BUY", 10.0)
        self.assertEqual(ctx.exception.code, CREDENTIALS_INVALID)

    def test_live_with_key_but_unarmed_refused(self):
        with _EnvGuard(MEMECOIN_PRIVATE_KEY=_DUMMY_KEY):
            a = self._live(key_provider=EnvKeyProvider(), armed=False)
            with self.assertRaises(BrokerError) as ctx:
                a.submit_swap(_TOKEN, "BUY", 10.0)
        self.assertEqual(ctx.exception.code, CREDENTIALS_INVALID)

    def test_live_with_key_and_armed_still_fail_closed(self):
        with _EnvGuard(MEMECOIN_PRIVATE_KEY=_DUMMY_KEY):
            a = self._live(key_provider=EnvKeyProvider(), armed=True)
            with self.assertRaises(BrokerError) as ctx:
                a.submit_swap(_TOKEN, "BUY", 10.0)
        self.assertEqual(ctx.exception.code, BROKER_UNAVAILABLE)
        self.assertEqual(ctx.exception.detail.get("reason"),
                         "live_not_implemented")

    def test_live_status_fragment_honest(self):
        a = self._live(key_provider=NullKeyProvider())
        frag = a.broker_status_fragment()
        self.assertEqual(frag["mode"], "live")
        self.assertFalse(frag["trading_available"])
        self.assertIn("paper-only", frag["detail"]["reason"])


# ---------------------------------------------------------------------------
class TestAdapterChainPlumbing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forex_mc_")
        self.market = _FakeMarket({"SOL": 100.0, "BONK": 0.0001})
        self.market.register("BONK",
                             "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")

    def _adapter(self, **kw):
        kw.setdefault("data_dir", os.path.join(self.tmp, "memecoin"))
        kw.setdefault("mode", "paper")
        kw.setdefault("market_data", self.market)
        kw.setdefault("symbols", {"BONK": self.market.mint_for("BONK")})
        a = MemecoinAdapter(**kw)
        a.connect({})
        return a

    def _inject_base(self, a):
        """Pre-seed the base ChainAdapter with injected quotes (no network)."""
        base = EvmChainAdapter(
            get_chain_def("base"), data_dir=os.path.join(self.tmp, "memecoin"),
            mode="paper", quotes={_TOKEN.lower(): 2.0})
        a._chain_adapters["base"] = base
        return base

    def test_default_is_solana_only(self):
        a = self._adapter()
        self.assertEqual(a.enabled_chains, ["solana"])

    def test_unknown_chain_rejected_at_construction(self):
        with self.assertRaises(BrokerError) as ctx:
            self._adapter(chains=["solana", "narnia"])
        self.assertEqual(ctx.exception.code, CONFIG_INVALID)

    def test_chain_routed_order(self):
        a = self._adapter(chains=["solana", "base"])
        base = self._inject_base(a)
        res = a.submit_order(_req(_TOKEN, "BUY", 100.0), chain="base")
        self.assertEqual(res.message, "paper fill (base)")
        self.assertEqual(res.raw["chain"], "base")
        self.assertEqual(res.ticket, 1)
        # Volume is interpreted as USDC on chain routes.
        self.assertAlmostEqual(base.get_balance()["USDC"], 900.0)

    def test_chain_routed_sell(self):
        a = self._adapter(chains=["solana", "base"])
        self._inject_base(a)
        a.submit_order(_req(_TOKEN, "BUY", 100.0), chain="base")
        res = a.submit_order(_req(_TOKEN, "SELL", 40.0), chain="base")
        self.assertEqual(res.direction, "SELL")

    def test_unknown_chain_routing_rejected(self):
        a = self._adapter(chains=["solana", "base"])
        with self.assertRaises(BrokerError) as ctx:
            a.submit_order(_req(_TOKEN, "BUY", 10.0), chain="narnia")
        self.assertEqual(ctx.exception.code, CONFIG_INVALID)

    def test_disabled_chain_routing_rejected(self):
        a = self._adapter(chains=["solana"])  # base not enabled
        with self.assertRaises(BrokerError) as ctx:
            a.submit_order(_req(_TOKEN, "BUY", 10.0), chain="base")
        self.assertEqual(ctx.exception.code, CONFIG_INVALID)

    def test_solana_chain_name_uses_native_engine(self):
        a = self._adapter(chains=["solana", "base"])
        # Explicit chain="solana" takes the native SOL-settled path.
        res = a.submit_order(_req("BONK", "BUY", 0.05), chain="solana")
        self.assertEqual(res.message, "paper fill")
        # ...while chain_adapter("solana") refuses to avoid a dual book.
        with self.assertRaises(BrokerError) as ctx:
            a.chain_adapter("solana")
        self.assertEqual(ctx.exception.code, CONFIG_INVALID)

    def test_chain_status_never_raises(self):
        a = self._adapter(chains=["solana", "base", "hyperliquid"])
        self._inject_base(a)
        status = a.chain_status()
        self.assertIn("solana", status)
        self.assertIn("base", status)
        self.assertIn("hyperliquid", status)
        self.assertTrue(status["base"]["trading_available"])

    def test_broker_status_reports_chains(self):
        a = self._adapter(chains=["solana", "base"])
        detail = a.broker_status()["broker"]["detail"]
        self.assertEqual(detail["chains"], ["solana", "base"])


# ---------------------------------------------------------------------------
class TestMemecoinConfigValidation(unittest.TestCase):
    """Config-level validation for chains + wallet (needs yaml)."""

    def test_chains_and_wallet_fields_present(self):
        from config.config import MemecoinConfig
        with _EnvGuard(remove=("MEMECOIN_CHAINS", "MEMECOIN_WALLET_KEY_SOURCE",
                               "MEMECOIN_WALLET_KEY_FILE")):
            cfg = MemecoinConfig()
            self.assertEqual(cfg.chains, ["solana"])
            self.assertEqual(cfg.wallet_key_source, "none")
            self.assertEqual(cfg.wallet_key_file, "")

    def test_chains_env_override(self):
        from config.config import MemecoinConfig
        with _EnvGuard(MEMECOIN_CHAINS="solana, base ,hyperliquid"):
            cfg = MemecoinConfig()
            self.assertEqual(cfg.chains, ["solana", "base", "hyperliquid"])

    def test_wallet_env_override(self):
        from config.config import MemecoinConfig
        with _EnvGuard(MEMECOIN_WALLET_KEY_SOURCE="env"):
            self.assertEqual(MemecoinConfig().wallet_key_source, "env")

    def test_appconfig_rejects_unknown_chain(self):
        from config.config import load_config
        # Pin BROKER_PROVIDER: other test modules setdefault it to
        # "disconnected" at import time (pre-existing repo wart), which
        # would skip the memecoin validation block under test.
        with _EnvGuard(BROKER_PROVIDER="memecoin",
                       MEMECOIN_CHAINS="solana,narnia"):
            with self.assertRaises(RuntimeError) as ctx:
                load_config()
        self.assertIn("CONFIG_INVALID", str(ctx.exception))

    def test_appconfig_rejects_bad_key_source(self):
        from config.config import load_config
        with _EnvGuard(BROKER_PROVIDER="memecoin",
                       MEMECOIN_WALLET_KEY_SOURCE="yolo"):
            with self.assertRaises(RuntimeError) as ctx:
                load_config()
        self.assertIn("CONFIG_INVALID", str(ctx.exception))

    def test_appconfig_rejects_file_source_without_path(self):
        from config.config import load_config
        with _EnvGuard(BROKER_PROVIDER="memecoin",
                       MEMECOIN_WALLET_KEY_SOURCE="file",
                       remove=("MEMECOIN_WALLET_KEY_FILE",)):
            with self.assertRaises(RuntimeError) as ctx:
                load_config()
        self.assertIn("CONFIG_INVALID", str(ctx.exception))

    def test_appconfig_accepts_file_source_with_path(self):
        from config.config import load_config
        with _EnvGuard(BROKER_PROVIDER="memecoin",
                       MEMECOIN_WALLET_KEY_SOURCE="file",
                       MEMECOIN_WALLET_KEY_FILE="/tmp/k.key"):
            cfg = load_config()
            self.assertEqual(cfg.broker.memecoin.wallet_key_source, "file")


if __name__ == "__main__":
    unittest.main()
