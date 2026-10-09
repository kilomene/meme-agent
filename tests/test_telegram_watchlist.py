"""Tests for the Telegram /add watchlist command and config hot-reload.

Run from the forex-agent root:
    python3 -m unittest tests.test_telegram_watchlist -v

Covers (no network — DexScreener resolution is stubbed):
  * watchlist.validate_mint: real mints accepted, garbage rejected.
  * watchlist.sanitize_symbol: normalization + bounds.
  * watchlist.add_to_watchlist: surgical insert into a temp defaults.yaml —
    both the memecoin.symbols registry and the top-level symbols list get
    the entry, comments/formatting survive, duplicates rejected, malformed
    configs fail closed.
  * commands /add: usage, invalid CA, explicit symbol, resolved symbol,
    already-watched, unauthorized chat; registered in _COMMANDS and in the
    mutating rate-limit bucket.
  * config hot-reload: editing defaults.yaml on disk is picked up by the
    next _y() call without reimport.
"""

import copy
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("FOREX_AGENT_HOME", "/tmp/forex-test-home-watchlist")

from daemon.telegram import commands as tg_commands      # noqa: E402
from daemon.telegram import security as tg_security      # noqa: E402
from daemon.telegram import watchlist as tg_watchlist    # noqa: E402
import config.config as app_config                      # noqa: E402


BONK_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
FAKE_MINT = "So11111111111111111111111111111111111111112"  # wSOL, valid 32-byte
NEW_MINT = "HzaCANKGkhNcQGb1VWL8f4tHJYrCmaXrPWk9eHR7GYYX"   # SATOSHINU

MINIMAL_DEFAULTS = """\
# test fixture — comments must survive surgical edits
memecoin:
  symbols:
    BONK: "%s"

symbols:
  - BONK
  - SOL

timeframes:
  - H1
""" % BONK_MINT

CHAT, USER = 6883056760, 111222333


class FakeClient:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return {"ok": True}


class FakeTools:
    pass


def make_ctx():
    client = FakeClient()
    sec = tg_security.SecurityContext({})
    published, audit = [], []
    ctx = tg_commands.HandlerContext(
        send=lambda c, t, m: client.send_message(c, t, reply_markup=m),
        security=sec, tools=FakeTools(),
        publish=lambda e, sev=None: published.append((e, sev)),
        audit_log=audit)
    return client, ctx, sec, published, audit


def paired_ctx():
    client, ctx, sec, published, audit = make_ctx()
    tg_commands.handle_message(ctx, CHAT, USER, 1, "/start")
    assert sec.is_paired()
    client.sent.clear()
    return client, ctx, sec, published, audit


def temp_defaults(text=MINIMAL_DEFAULTS):
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    return path


class MintValidationTests(unittest.TestCase):
    def test_real_mints_valid(self):
        self.assertTrue(tg_watchlist.validate_mint(BONK_MINT))
        self.assertTrue(tg_watchlist.validate_mint(FAKE_MINT))
        self.assertTrue(tg_watchlist.validate_mint(NEW_MINT))

    def test_garbage_rejected(self):
        for bad in ("", "not-an-address", "0" * 44, "O" * 44,
                    "z" * 44,  # valid charset, decodes to > 32 bytes
                    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263 "):  # trailing space
            self.assertFalse(tg_watchlist.validate_mint(bad), bad)

    def test_wrong_byte_length_rejected(self):
        # 32 '1's decode to 32 zero bytes -> valid; 31 -> invalid
        self.assertTrue(tg_watchlist.validate_mint("1" * 32))
        self.assertFalse(tg_watchlist.validate_mint("1" * 31))

    def test_evm_addresses_valid(self):
        # 0x + 40 hex chars, either case.
        self.assertTrue(tg_watchlist.validate_mint(
            "0xb32e10022ffbedfe10bc818a1c7e67d9d87e0fa7"))
        self.assertTrue(tg_watchlist.validate_mint("0x" + "AB" * 20))
        self.assertTrue(tg_watchlist.validate_mint("0X" + "ab" * 20))

    def test_malformed_evm_rejected(self):
        for bad in ("0x123",                 # too short
                    "0x" + "ab" * 19,        # 38 hex chars
                    "0x" + "ab" * 21,        # 42 hex chars
                    "0x" + "zz" * 20,        # non-hex
                    "0xb32e10022ffbedfe10bc818a1c7e67d9d87e0fa7 "):  # trailing space
            self.assertFalse(tg_watchlist.validate_mint(bad), bad)


class SymbolSanitizeTests(unittest.TestCase):
    def test_normalizes(self):
        self.assertEqual(tg_watchlist.sanitize_symbol("satoshinu"), "SATOSHINU")
        self.assertEqual(tg_watchlist.sanitize_symbol("looong-cat!"), "LOOONGCAT")

    def test_rejects(self):
        self.assertIsNone(tg_watchlist.sanitize_symbol("a"))
        self.assertIsNone(tg_watchlist.sanitize_symbol(""))
        self.assertIsNone(tg_watchlist.sanitize_symbol("---"))

    def test_truncates(self):
        self.assertEqual(len(tg_watchlist.sanitize_symbol("a" * 30)), 12)


class AddToWatchlistTests(unittest.TestCase):
    def setUp(self):
        self.path = temp_defaults()
        self._orig = tg_watchlist._defaults_path
        tg_watchlist._defaults_path = lambda: self.path  # noqa: E731

    def tearDown(self):
        tg_watchlist._defaults_path = self._orig
        os.remove(self.path)

    def _read(self):
        with open(self.path) as fh:
            return fh.read()

    def test_add_updates_both_locations(self):
        ok, msg = tg_watchlist.add_to_watchlist("SATOSHINU", NEW_MINT)
        self.assertTrue(ok, msg)
        text = self._read()
        self.assertIn('    SATOSHINU: "%s"' % NEW_MINT, text)
        self.assertIn("  - SATOSHINU\n", text)
        # comments + formatting survive
        self.assertIn("# test fixture", text)
        mon, reg = tg_watchlist.current_watchlist()
        self.assertIn("SATOSHINU", mon)
        self.assertEqual(reg["SATOSHINU"], NEW_MINT)

    def test_duplicate_mint_rejected(self):
        ok, msg = tg_watchlist.add_to_watchlist("BONK2", BONK_MINT)
        self.assertFalse(ok)
        self.assertIn("already watched as BONK", msg)
        self.assertNotIn("BONK2", self._read())

    def test_duplicate_symbol_rejected(self):
        ok, msg = tg_watchlist.add_to_watchlist("BONK", NEW_MINT)
        self.assertFalse(ok)
        self.assertIn("already on the watchlist", msg)

    def test_invalid_mint_rejected(self):
        ok, _ = tg_watchlist.add_to_watchlist("NOPE", "garbage")
        self.assertFalse(ok)

    def test_bad_symbol_rejected(self):
        ok, _ = tg_watchlist.add_to_watchlist("x", NEW_MINT)
        self.assertFalse(ok)

    def test_malformed_config_fails_closed(self):
        with open(self.path, "w") as fh:
            fh.write("symbols: [not, a, mapping]\n")
        ok, msg = tg_watchlist.add_to_watchlist("SATOSHINU", NEW_MINT)
        self.assertFalse(ok)
        self.assertIn("manually", msg)

    def test_keys_before_symbols_do_not_break_insert(self):
        # Regression: real defaults.yaml has indent-2 keys (data_dir,
        # paper_start_sol, ...) BEFORE memecoin.symbols — the insert must
        # not treat them as the end of the memecoin section.
        text = ("memecoin:\n"
                "  data_dir: \"\"\n"
                "  paper_start_sol: 10.0\n"
                "  live_armed: false\n"
                "  symbols:\n"
                "    BONK: \"%s\"\n"
                "  chains: [\"solana\"]\n"
                "\nsymbols:\n"
                "  - BONK\n"
                "  - SOL\n") % BONK_MINT
        path = temp_defaults(text)
        orig = tg_watchlist._defaults_path
        tg_watchlist._defaults_path = lambda: path  # noqa: E731
        try:
            ok, msg = tg_watchlist.add_to_watchlist("SATOSHINU", NEW_MINT)
            self.assertTrue(ok, msg)
            with open(path) as fh:
                out = fh.read()
            self.assertIn('    SATOSHINU: "%s"' % NEW_MINT, out)
            self.assertIn("  - SATOSHINU\n", out)
        finally:
            tg_watchlist._defaults_path = orig
            os.remove(path)


class AddCommandTests(unittest.TestCase):
    def setUp(self):
        self.path = temp_defaults()
        self._orig = tg_watchlist._defaults_path
        tg_watchlist._defaults_path = lambda: self.path  # noqa: E731

    def tearDown(self):
        tg_watchlist._defaults_path = self._orig
        os.remove(self.path)

    def test_registered_and_rate_limited(self):
        self.assertIn("add", tg_commands._COMMANDS)
        self.assertIn("/add", tg_commands.HELP_TEXT)

    def test_usage_without_args(self):
        client, ctx, sec, published, audit = paired_ctx()
        tg_commands.handle_message(ctx, CHAT, USER, 2, "/add")
        self.assertTrue(any("Usage" in t for _, t, _ in client.sent))

    def test_invalid_ca_rejected(self):
        client, ctx, sec, published, audit = paired_ctx()
        tg_commands.handle_message(ctx, CHAT, USER, 2, "/add not-a-ca")
        self.assertTrue(any("doesn't look like a valid" in t for _, t, _ in client.sent))
        self.assertTrue(any(a["action"] == "add.rejected" for a in audit))

    def test_add_with_explicit_symbol(self):
        client, ctx, sec, published, audit = paired_ctx()
        with mock.patch.object(tg_watchlist, "resolve_token") as rt:
            tg_commands.handle_message(ctx, CHAT, USER, 2, "/add %s PEPE2" % NEW_MINT)
            rt.assert_not_called()  # explicit symbol skips DexScreener
        self.assertTrue(any("Watching PEPE2" in t for _, t, _ in client.sent))
        mon, reg = tg_watchlist.current_watchlist()
        self.assertIn("PEPE2", mon)
        self.assertEqual(reg["PEPE2"], NEW_MINT)
        self.assertTrue(any(a["action"] == "add" and a["ok"] for a in audit))

    def test_add_resolves_symbol_via_dexscreener(self):
        client, ctx, sec, published, audit = paired_ctx()
        with mock.patch.object(tg_watchlist, "resolve_token",
                               return_value={"symbol": "SATOSHINU", "name": "Satoshi Inu"}):
            tg_commands.handle_message(ctx, CHAT, USER, 2, "/add %s" % NEW_MINT)
        self.assertTrue(any("Watching SATOSHINU" in t for _, t, _ in client.sent))

    def test_add_unresolvable_without_symbol(self):
        client, ctx, sec, published, audit = paired_ctx()
        with mock.patch.object(tg_watchlist, "resolve_token", return_value=None):
            tg_commands.handle_message(ctx, CHAT, USER, 2, "/add %s" % NEW_MINT)
        self.assertTrue(any("Couldn't identify" in t for _, t, _ in client.sent))
        mon, _ = tg_watchlist.current_watchlist()
        self.assertNotIn("SATOSHINU", mon)  # nothing changed

    def test_add_duplicate_graceful(self):
        client, ctx, sec, published, audit = paired_ctx()
        tg_commands.handle_message(ctx, CHAT, USER, 2, "/add %s BONK" % NEW_MINT)
        self.assertTrue(any("already on the watchlist" in t for _, t, _ in client.sent))

    def test_unauthorized_chat_cannot_add(self):
        client, ctx, sec, published, audit = make_ctx()
        tg_commands.handle_message(ctx, CHAT, USER, 1, "/start")
        other_ctx_client = FakeClient()
        # stranger (never paired state) — fresh context, unpaired
        c2, ctx2, sec2, pub2, aud2 = make_ctx()
        tg_commands.handle_message(ctx2, 999, 888, 1, "/add %s X" % NEW_MINT)
        self.assertEqual(c2.sent, [])


class ConfigHotReloadTests(unittest.TestCase):
    def test_yaml_reload_on_mtime_change(self):
        path = temp_defaults()
        try:
            orig_path = app_config.DEFAULTS_PATH
            orig_yaml = app_config._YAML
            orig_mtime = app_config._YAML_MTIME
            app_config.DEFAULTS_PATH = Path(path)
            app_config._YAML_MTIME = 0  # force reload
            try:
                self.assertEqual(app_config._y("symbols"), ["BONK", "SOL"])
                # edit on disk; next _y() must see it without reimport
                with open(path) as fh:
                    text = fh.read()
                text = text.replace("  - SOL\n", "  - SOL\n  - NEWCOIN\n", 1)
                with open(path, "w") as fh:
                    fh.write(text)
                os.utime(path, (time.time() + 2, time.time() + 2))
                self.assertIn("NEWCOIN", app_config._y("symbols"))
            finally:
                app_config.DEFAULTS_PATH = orig_path
                app_config._YAML = orig_yaml
                app_config._YAML_MTIME = orig_mtime
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
