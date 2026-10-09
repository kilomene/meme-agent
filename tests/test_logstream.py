"""Tests for daemon/launch_scanner/logstream.py."""

import queue
import time
import unittest

from daemon.launch_scanner import logstream
from daemon.launch_scanner.logstream import LogStream, WSClient, WSError, extract_mint


def _parsed_tx(mint):
    return {
        "transaction": {
            "message": {
                "instructions": [{
                    "parsed": {"type": "initializeMint",
                               "info": {"mint": mint}},
                }],
            },
        },
        "meta": {"preTokenBalances": [], "postTokenBalances": [],
                 "innerInstructions": []},
    }


class ExtractMintTest(unittest.TestCase):
    def test_parsed_initialize_mint(self):
        self.assertEqual(extract_mint(_parsed_tx("NEWMINT1")), "NEWMINT1")

    def test_balance_diff_fallback(self):
        tx = {
            "transaction": {"message": {"instructions": []}},
            "meta": {
                "preTokenBalances": [
                    {"mint": logstream.WSOL_MINT, "owner": "x"},
                ],
                "postTokenBalances": [
                    {"mint": logstream.WSOL_MINT, "owner": "x"},
                    {"mint": "FALLBACKMINT", "owner": "y"},
                ],
                "innerInstructions": [],
            },
        }
        self.assertEqual(extract_mint(tx), "FALLBACKMINT")

    def test_garbage_returns_none(self):
        self.assertIsNone(extract_mint({}))
        self.assertIsNone(extract_mint({"meta": {}}))
        self.assertIsNone(extract_mint(None))


class LogStreamDegradationTest(unittest.TestCase):
    def test_http_rpc_method_not_shadowed_by_url_attr(self):
        # Regression: __init__ once stored the URL string in self._http_rpc,
        # shadowing the _http_rpc() method -> "'str' object is not callable".
        ls = LogStream()
        self.assertTrue(callable(ls._http_rpc))
        self.assertIsInstance(ls._http_rpc_url, str)

    def test_mint_for_signature_uses_rpc(self):
        ls = LogStream.__new__(LogStream)
        ls._http_rpc_url = "http://x/"
        ls._last_rpc_ts = 0.0
        ls._rpc_fail_count = 0
        ls._last_rpc_warn = 0.0
        ls._http_rpc = lambda method, params: _parsed_tx("RPCMINT")
        self.assertEqual(ls._mint_for_signature("sig"), "RPCMINT")
    def test_unreachable_ws_degrades_not_crashes(self):
        # Nothing listening on this port: the thread must fail, back off,
        # and leave the scanner on polling — never raise.
        ls = LogStream(ws_url="ws://127.0.0.1:1")
        ls.start()
        try:
            time.sleep(0.5)
            self.assertFalse(ls.healthy)
            self.assertEqual(ls.drain(), [])
        finally:
            ls.stop()

    def test_drain_returns_queued_items(self):
        ls = LogStream.__new__(LogStream)
        ls._q = queue.Queue()
        ls._q.put({"mint": "M1", "signature": "s1", "ts": 1.0})
        ls._q.put({"mint": "M2", "signature": "s2", "ts": 2.0})
        out = ls.drain()
        self.assertEqual([i["mint"] for i in out], ["M1", "M2"])
        self.assertEqual(ls.drain(), [])

    def test_failed_tx_notifications_ignored(self):
        ls = LogStream.__new__(LogStream)
        ls._q = queue.Queue()
        ls._seen_sigs = set()
        ls._mint_for_signature = lambda sig: (_ for _ in ()).throw(
            AssertionError("must not fetch failed txs"))
        msg = ('{"jsonrpc":"2.0","method":"logsNotification","params":'
               '{"result":{"value":{"signature":"bad1",'
               '"err":{"InstructionError":[0,"Custom"]},"logs":[]}},'
               '"subscription":1}}')
        ls._handle_message(msg)  # must not raise, must not queue
        self.assertEqual(ls.drain(), [])

    def test_duplicate_signatures_deduped(self):
        ls = LogStream.__new__(LogStream)
        ls._q = queue.Queue()
        ls._seen_sigs = set()
        ls._mint_for_signature = lambda sig: "DUPMINT"
        msg = ('{"jsonrpc":"2.0","method":"logsNotification","params":'
               '{"result":{"value":{"signature":"dup1","err":null,"logs":[]}},'
               '"subscription":1}}')
        ls._handle_message(msg)
        ls._handle_message(msg)
        self.assertEqual(len(ls.drain()), 1)


class WSClientTest(unittest.TestCase):
    def test_connect_refused_raises_wserror(self):
        ws = WSClient("ws://127.0.0.1:1", timeout=3)
        with self.assertRaises(Exception):
            ws.connect()

    def test_bad_scheme_rejected(self):
        ws = WSClient("http://example.com/", timeout=3)
        with self.assertRaises(WSError):
            ws.connect()


if __name__ == "__main__":
    unittest.main()
