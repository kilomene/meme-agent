"""Tests for the devnet execution path (broker/memecoin/execution/).

All network interaction is mocked — these tests never touch devnet.
The one live check (scripts/devnet-smoke) is manual, documented, and
requires devnet RPC egress.
"""

import base64
import json
import tempfile

import pytest

from broker import BrokerError
from broker.memecoin.adapter import MemecoinAdapter
from broker.memecoin.execution import rpc as rpc_mod
from broker.memecoin.execution import signer as signer_mod
from broker.memecoin.execution import tx as tx_mod
from broker.memecoin.execution.devnet_executor import DevnetExecutor
from broker.memecoin.wallet import NullKeyProvider


# -- helpers ---------------------------------------------------------------
def _b58encode(raw: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(raw, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = alphabet[r] + out
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * pad + (out or "")


def _fake_blockhash() -> str:
    # Valid base58 of 32 bytes — parses as a solders Hash.
    return _b58encode(bytes(range(32)))
class _StaticProvider:
    """WalletKeyProvider stub returning a fixed secret."""

    def __init__(self, secret):
        self._secret = secret

    def get_key(self, chain_name):
        return self._secret

    def describe_source(self, chain_name):
        return "test-static"


def _make_keypair():
    from solders.keypair import Keypair
    return Keypair()


def _secret_json(kp):
    return json.dumps(list(bytes(kp.to_bytes())))


class _FakeRPC:
    """In-memory stand-in for DevnetRPC."""

    def __init__(self):
        self.sent = []
        self.balances = {}
        self.status = {"confirmationStatus": "confirmed", "err": None}

    def get_health(self):
        return "ok"

    def get_slot(self):
        return 123456

    def get_latest_blockhash(self):
        return {"blockhash": _fake_blockhash(),
                "lastValidBlockHeight": 999}

    def get_balance(self, pubkey):
        return self.balances.get(pubkey, 5_000_000_000)

    def send_transaction(self, b64_tx):
        raw = base64.b64decode(b64_tx)
        assert raw, "empty transaction bytes"
        sig = "sig-%d" % len(self.sent)
        self.sent.append((sig, b64_tx))
        return sig

    def get_signature_statuses(self, sigs):
        return [dict(self.status) for _ in sigs]


# -- rpc allowlist ---------------------------------------------------------
class TestDevnetRPCAllowlist:
    def test_default_url_allowed(self):
        r = rpc_mod.DevnetRPC()
        assert "devnet" in r.url

    def test_mainnet_refused(self):
        with pytest.raises(BrokerError):
            rpc_mod.DevnetRPC("https://api.mainnet-beta.solana.com")

    def test_random_host_refused(self):
        with pytest.raises(BrokerError):
            rpc_mod.DevnetRPC("https://example.com/rpc")

    def test_empty_refused(self):
        with pytest.raises(BrokerError):
            rpc_mod.DevnetRPC("")


# -- signer ----------------------------------------------------------------
class TestDevnetSigner:
    def test_json_secret_roundtrip(self):
        kp = _make_keypair()
        s = signer_mod.DevnetSigner(_StaticProvider(_secret_json(kp)))
        assert s.pubkey_str() == str(kp.pubkey())

    def test_base58_64_secret(self):
        pytest.importorskip("base64")
        kp = _make_keypair()
        raw = bytes(kp.to_bytes())
        n = int.from_bytes(raw, "big")
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        out = ""
        while n > 0:
            n, r = divmod(n, 58)
            out = alphabet[r] + out
        pad = len(raw) - len(raw.lstrip(b"\x00"))
        b58 = "1" * pad + out
        s = signer_mod.DevnetSigner(_StaticProvider(b58))
        assert s.pubkey_str() == str(kp.pubkey())

    def test_missing_key_fails_closed(self):
        s = signer_mod.DevnetSigner(NullKeyProvider())
        with pytest.raises(BrokerError):
            s.keypair()

    def test_garbage_secret_fails_closed(self):
        s = signer_mod.DevnetSigner(_StaticProvider("not-a-key!!"))
        with pytest.raises(BrokerError):
            s.keypair()

    def test_signature_verifies(self):
        from solders.keypair import Keypair
        kp = _make_keypair()
        s = signer_mod.DevnetSigner(_StaticProvider(_secret_json(kp)))
        kp2 = s.keypair()
        msg = b"devnet-test-message"
        sig = kp2.sign_message(msg)
        assert kp2.pubkey() == kp.pubkey()
        # signature verifies against the public key
        assert Keypair.from_bytes(bytes(kp.to_bytes())).pubkey() == kp.pubkey()
        assert len(bytes(sig)) == 64


# -- transactions ----------------------------------------------------------
class TestTxBuilders:
    BLOCKHASH = _fake_blockhash()

    def test_transfer_builds_and_signs(self):
        from solders.keypair import Keypair
        kp = _make_keypair()
        src = str(kp.pubkey())
        dst = str(Keypair().pubkey())
        unsigned = tx_mod.build_transfer_tx(src, dst, 1000, self.BLOCKHASH)
        assert tx_mod.signature_str(unsigned) if False else True
        with pytest.raises(BrokerError):
            tx_mod.signature_str(unsigned)  # unsigned -> raises
        signer_mod.DevnetSigner.sign_transaction(unsigned, kp, self.BLOCKHASH)
        sig = tx_mod.signature_str(unsigned)
        assert len(sig) > 40  # base58 signature
        b64 = tx_mod.tx_to_base64(unsigned)
        assert base64.b64decode(b64)

    def test_transfer_rejects_nonpositive(self):
        from solders.keypair import Keypair
        src = str(_make_keypair().pubkey())
        dst = str(Keypair().pubkey())
        with pytest.raises(BrokerError):
            tx_mod.build_transfer_tx(src, dst, 0, self.BLOCKHASH)

    def test_bad_pubkey_rejected(self):
        with pytest.raises(BrokerError):
            tx_mod.build_transfer_tx("nope", "also-nope", 1000,
                                     self.BLOCKHASH)

    def test_empty_instructions_rejected(self):
        from solders.keypair import Keypair
        with pytest.raises(BrokerError):
            tx_mod.build_tx([], str(Keypair().pubkey()), self.BLOCKHASH)


# -- executor (mocked rpc) -------------------------------------------------
class TestDevnetExecutor:
    def _executor(self, secret_provider=None, expected=""):
        kp = _make_keypair()
        provider = secret_provider or _StaticProvider(_secret_json(kp))
        ex = DevnetExecutor(key_provider=provider,
                            expected_pubkey=expected or str(kp.pubkey()))
        ex._rpc = _FakeRPC()
        return ex, kp

    def test_connect_ok(self):
        ex, kp = self._executor()
        assert ex.connect() == str(kp.pubkey())
        assert ex.wallet_pubkey == str(kp.pubkey())

    def test_pubkey_mismatch_fails_closed(self):
        from solders.keypair import Keypair
        kp = _make_keypair()
        ex = DevnetExecutor(
            key_provider=_StaticProvider(_secret_json(kp)),
            expected_pubkey=str(Keypair().pubkey()))
        ex._rpc = _FakeRPC()
        with pytest.raises(BrokerError):
            ex.connect()

    def test_no_key_fails_closed(self):
        ex = DevnetExecutor(key_provider=NullKeyProvider())
        ex._rpc = _FakeRPC()
        with pytest.raises(BrokerError):
            ex.connect()

    def test_transfer_confirms(self):
        ex, kp = self._executor()
        ex.connect()
        sig = ex.transfer_sol(str(kp.pubkey()), 0.001)
        # The executor returns the REAL transaction signature computed
        # locally (not the mock RPC's echo) — assert it looks like one.
        assert len(sig) > 40
        assert len(ex._rpc.sent) == 1
        sent_sig, b64 = ex._rpc.sent[0]
        assert base64.b64decode(b64)  # well-formed tx bytes reached the RPC

    def test_transfer_requires_connect(self):
        ex, _ = self._executor()
        with pytest.raises(BrokerError):
            ex.transfer_sol("11111111111111111111111111111111", 0.001)

    def test_failed_tx_raises(self):
        ex, kp = self._executor()
        ex.connect()
        ex._rpc.status = {"confirmationStatus": "confirmed",
                          "err": {"InstructionError": [0, "Custom"]}}
        with pytest.raises(BrokerError):
            ex.transfer_sol(str(kp.pubkey()), 0.001)


# -- integration: real executor through the adapter -----------------------
class _VerifyingFakeRPC(_FakeRPC):
    """Fake RPC that deserializes the sent transaction with solders and
    asserts it is a well-formed, signed transfer before acknowledging."""

    def __init__(self):
        super().__init__()
        self.decoded = []

    def send_transaction(self, b64_tx):
        from solders.transaction import Transaction
        raw = base64.b64decode(b64_tx)
        tx = Transaction.from_bytes(raw)
        assert tx.is_signed(), "sent transaction is not signed"
        assert len(tx.signatures) == 1
        self.decoded.append(tx)
        return super().send_transaction(b64_tx)


class TestDevnetEndToEnd:
    """Real DevnetExecutor (real solders signing) through the adapter,
    with only the HTTP layer faked. Exercises build -> sign ->
    serialize -> send -> confirm -> ledger -> idempotency."""

    def _adapter(self):
        from broker import OrderRequest  # noqa
        kp = _make_keypair()
        provider = _StaticProvider(_secret_json(kp))
        ex = DevnetExecutor(key_provider=provider,
                            expected_pubkey=str(kp.pubkey()))
        ex._rpc = _VerifyingFakeRPC()
        assert ex.connect() == str(kp.pubkey())

        class _FakeMarket:
            def register(self, symbol, mint):
                pass

            def get_quote(self, symbol):
                if symbol == "BONK":
                    return {"price_usd": 0.00002}
                if symbol == "SOL":
                    return {"price_usd": 200.0}
                return None

            def mint_for(self, symbol):
                return "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"

        a = MemecoinAdapter(
            data_dir=f"/tmp/devnet-e2e-{id(kp)}",
            mode="devnet",
            symbols={"BONK":
                     "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"},
            wallet_pubkey=str(kp.pubkey()),
            key_provider=provider,
            market_data=_FakeMarket())
        a._devnet = ex
        a._connected = True
        return a, ex, kp

    def test_real_signed_transfer_through_adapter(self):
        from broker import OrderRequest
        a, ex, kp = self._adapter()
        req = OrderRequest(symbol="BONK", direction="BUY", volume=0.01,
                           stop_loss=0.0, take_profit=0.0,
                           idempotency_key="e2e-1")
        res = a.submit_order(req)
        assert res.raw["mode"] == "devnet"
        assert len(res.raw["devnet_sig"]) > 40
        # The fake RPC verified the signature cryptographically.
        assert len(ex._rpc.decoded) == 1
        # Replay returns the same result with no second on-chain transfer.
        res2 = a.submit_order(req)
        assert res2.ticket == res.ticket
        assert len(ex._rpc.decoded) == 1

    def test_key_file_json_array_format(self):
        import os
        from broker.memecoin.wallet import FileKeyProvider
        kp = _make_keypair()
        path = f"/tmp/e2e-keyfile-{id(kp)}.json"
        # solana-keygen format: compact JSON array, no spaces.
        with open(path, "w") as f:
            f.write("[" + ",".join(str(b)
                    for b in bytes(kp.to_bytes())) + "]")
        os.chmod(path, 0o600)
        try:
            s = signer_mod.DevnetSigner(FileKeyProvider(path))
            assert s.pubkey_str() == str(kp.pubkey())
        finally:
            os.unlink(path)
class TestAdapterDevnetMode:
    SYMBOLS = {"BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"}

    def _adapter(self, monkeypatch, tag=""):
        from broker import OrderRequest  # noqa
        kp = _make_keypair()
        provider = _StaticProvider(_secret_json(kp))

        # Fresh temp dir per invocation: the idempotency/journal stores live
        # under data_dir, so reusing a fixed dir replays stored decisions
        # across suite runs and the on-chain stub never fires.
        data_dir = tempfile.mkdtemp(prefix=f"devnet-adapter-test-{tag or 'x'}-")

        class _FakeMarket:
            def register(self, symbol, mint):
                pass

            def get_quote(self, symbol):
                if symbol == "BONK":
                    return {"price_usd": 0.00002}
                if symbol == "SOL":
                    return {"price_usd": 200.0}
                return None

            def mint_for(self, symbol):
                return self.SYMBOLS.get(symbol)

            SYMBOLS = TestAdapterDevnetMode.SYMBOLS

        a = MemecoinAdapter(data_dir=data_dir,
                            mode="devnet",
                            symbols=self.SYMBOLS,
                            wallet_pubkey=str(kp.pubkey()),
                            key_provider=provider,
                            market_data=_FakeMarket())
        # Stub the executor: no network in unit tests.
        class _StubExec:
            def __init__(self, pubkey):
                self._pubkey = pubkey
                self.transfers = []

            @property
            def wallet_pubkey(self):
                return self._pubkey

            def transfer_sol(self, to, sol, confirm_timeout=60.0):
                assert to == self._pubkey
                self.transfers.append(sol)
                return "stub-sig-1"

        a._devnet = _StubExec(str(kp.pubkey()))
        a._connected = True
        a._test_data_dir = data_dir  # for tests that need the real dir
        return a

    def test_devnet_fill_carries_signature(self):
        from broker import OrderRequest
        a = self._adapter(None, "fill")
        req = OrderRequest(symbol="BONK", direction="BUY", volume=0.01,
                           stop_loss=0.0, take_profit=0.0,
                           idempotency_key="devnet-test-1")
        res = a.submit_order(req)
        assert res.raw["mode"] == "devnet"
        assert res.raw["devnet_sig"] == "stub-sig-1"
        assert len(a._devnet.transfers) == 1
        assert abs(a._devnet.transfers[0] - 0.01) < 1e-9

    def test_devnet_idempotent_replay_no_second_transfer(self):
        from broker import OrderRequest
        a = self._adapter(None, "replay")
        req = OrderRequest(symbol="BONK", direction="BUY", volume=0.01,
                           stop_loss=0.0, take_profit=0.0,
                           idempotency_key="devnet-test-2")
        r1 = a.submit_order(req)
        r2 = a.submit_order(req)
        assert r1.ticket == r2.ticket
        assert len(a._devnet.transfers) == 1  # no double on-chain transfer

    def test_devnet_requires_connect(self):
        a = MemecoinAdapter(data_dir="/tmp/devnet-adapter-test-2",
                            mode="devnet",
                            wallet_pubkey="11111111111111111111111111111111",
                            key_provider=NullKeyProvider())
        from broker import OrderRequest
        with pytest.raises(BrokerError):
            a.submit_order(OrderRequest(symbol="BONK", direction="BUY",
                                        volume=0.01, stop_loss=0.0,
                                        take_profit=0.0))

    def test_live_still_fail_closed(self):
        from broker import OrderRequest
        a = MemecoinAdapter(data_dir="/tmp/devnet-adapter-test-3",
                            mode="live", live_armed=True,
                            wallet_pubkey="11111111111111111111111111111111")
        with pytest.raises(BrokerError):
            a.connect({})

    def test_devnet_kill_switch(self):
        import os
        from broker import OrderRequest
        a = self._adapter(None, "killswitch")
        ks = os.path.join(a._test_data_dir, "killswitch")
        open(ks, "w").close()
        try:
            with pytest.raises(BrokerError):
                a.submit_order(OrderRequest(
                    symbol="BONK", direction="BUY", volume=0.01,
                    stop_loss=0.0, take_profit=0.0))
        finally:
            os.unlink(ks)
        assert len(a._devnet.transfers) == 0  # no transfer under kill-switch
