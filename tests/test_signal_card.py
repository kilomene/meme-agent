"""Tests for the rich memecoin signal card (daemon/telegram/signal_card.py),
the transport stats method, and the signal.detected schema extension."""

import unittest
from unittest.mock import patch

from daemon.telegram.signal_card import (
    _compact,
    _price,
    render_signal_card,
)


FULL_STATS = {
    "symbol": "BONK",
    "name": "Bonk",
    "mint": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "price_usd": 0.000003589,
    "fdv_usd": 318955271.0,
    "liquidity_usd": 344980.79,
    "volume_h24": 2449736.27,
    "volume_h1": 281164.37,
    "change_h1_pct": 0.81,
    "change_h24_pct": 6.39,
    "buys_h1": 1642,
    "sells_h1": 1254,
    "dex": "orca",
    "chain": "solana",
    "age_days": 1350,
    "chart_url": "https://dexscreener.com/solana/abc",
    "socials": [{"type": "twitter", "url": "https://x.com/bonk"}],
}

BASE_EVENT = {
    "event": "signal.detected",
    "signal_id": "sig:BONK:H1:2026-09-22T18:00:00Z",
    "symbol": "BONK",
    "timeframe": "H1",
    "direction": "BUY",
    "entry_price": 0.0000036,
    "stop_loss": 0.0000034,
    "take_profit": 0.0000040,
    "strategy": "ema_rsi",
    "trigger": "ema_bull_cross",
}


def _event(**over):
    e = dict(BASE_EVENT)
    e.update(over)
    return e


class CompactTests(unittest.TestCase):
    def test_compact(self):
        self.assertEqual(_compact(191000), "191.00K")
        self.assertEqual(_compact(2400000), "2.40M")
        self.assertEqual(_compact(999), "999")
        self.assertEqual(_compact(None), "n/a")
        self.assertEqual(_compact("junk"), "n/a")

    def test_price(self):
        self.assertEqual(_price(0.000003589), "$0.000003589")
        self.assertEqual(_price(200.5), "$200.5000")
        self.assertEqual(_price(None), "n/a")
        self.assertEqual(_price(0), "n/a")


class CardTests(unittest.TestCase):
    def test_full_card(self):
        card = render_signal_card(_event(token_stats=dict(FULL_STATS)))
        self.assertIn("Bonk", card)
        self.assertIn("$BONK", card)
        self.assertIn("🔼", card)  # BUY arrow
        self.assertIn("318.96M", card)  # FDV compact
        self.assertIn("$0.000003589", card)
        self.assertIn("solana @ orca", card)
        self.assertIn("1642", card)  # buys
        self.assertIn("1254", card)  # sells
        self.assertIn("https://dexscreener.com/solana/abc", card)
        self.assertIn("ema_rsi", card)
        self.assertIn("sig:BONK:H1:", card)
        self.assertIn(FULL_STATS["mint"], card)
        # holder rows must never be fabricated
        self.assertNotIn("👥", card)
        self.assertNotIn("🌱", card)

    def test_sell_arrow(self):
        card = render_signal_card(
            _event(direction="SELL", token_stats=dict(FULL_STATS)))
        self.assertIn("🔽", card)

    def test_fallback_without_stats(self):
        card = render_signal_card(_event())
        self.assertIn("📡 SIGNAL BUY", card)
        self.assertIn("sig:BONK:H1:", card)

    def test_fallback_with_non_dict_stats(self):
        card = render_signal_card(_event(token_stats="oops"))
        self.assertIn("📡 SIGNAL", card)

    def test_partial_stats_no_crash(self):
        card = render_signal_card(_event(token_stats={"symbol": "BONK"}))
        self.assertIn("$BONK", card)
        self.assertIn("n/a", card)

    def test_no_holder_data_invented(self):
        card = render_signal_card(_event(token_stats=dict(FULL_STATS)))
        self.assertNotIn("Total:", card)
        self.assertNotIn("Fresh", card)


class SchemaTests(unittest.TestCase):
    def test_token_stats_accepted_by_schema(self):
        from agent.events.bus import validate_event
        evt = _event(token_stats=dict(FULL_STATS))
        out = validate_event(evt)
        self.assertEqual(out["token_stats"]["symbol"], "BONK")

    def test_schema_still_rejects_unknown(self):
        from agent.events.bus import validate_event
        with self.assertRaises(ValueError):
            validate_event(_event(bogus_field=1))


class TransportStatsTests(unittest.TestCase):
    def _market(self):
        from broker.memecoin.transport import MemecoinMarketData
        return MemecoinMarketData(symbols={"BONK": "MINT1"})

    def _pair(self):
        return {
            "chainId": "solana",
            "dexId": "orca",
            "pairAddress": "PAIR1",
            "baseToken": {"name": "Bonk", "symbol": "BONK"},
            "priceUsd": "0.000003589",
            "fdv": 318955271,
            "liquidity": {"usd": 344980.79},
            "volume": {"h24": 2449736.27, "h1": 281164.37},
            "priceChange": {"h1": 0.81, "h24": 6.39},
            "txns": {"h1": {"buys": 1642, "sells": 1254}},
            "pairCreatedAt": 1671980424000,
            "url": "https://dexscreener.com/solana/abc",
            "info": {"socials": [{"type": "twitter",
                                  "url": "https://x.com/bonk"}]},
        }

    def test_get_token_stats_parses(self):
        m = self._market()
        payload = {"pairs": [self._pair()]}
        with patch("broker.memecoin.transport._http_get_json",
                   return_value=payload):
            stats = m.get_token_stats("BONK")
        self.assertIsNotNone(stats)
        self.assertEqual(stats["symbol"], "BONK")
        self.assertEqual(stats["name"], "Bonk")
        self.assertAlmostEqual(stats["price_usd"], 0.000003589)
        self.assertEqual(stats["fdv_usd"], 318955271)
        self.assertEqual(stats["buys_h1"], 1642)
        self.assertEqual(stats["sells_h1"], 1254)
        self.assertEqual(stats["dex"], "orca")
        self.assertEqual(stats["chart_url"],
                         "https://dexscreener.com/solana/abc")
        self.assertIsInstance(stats["age_days"], int)

    def test_get_token_stats_none_when_unreachable(self):
        m = self._market()
        with patch("broker.memecoin.transport._http_get_json",
                   return_value=None):
            self.assertIsNone(m.get_token_stats("BONK"))

    def test_get_token_stats_none_for_unknown_symbol(self):
        m = self._market()
        self.assertIsNone(m.get_token_stats("NOPE"))

    def test_get_token_stats_never_raises(self):
        m = self._market()
        with patch("broker.memecoin.transport._http_get_json",
                   side_effect=RuntimeError("boom")):
            self.assertIsNone(m.get_token_stats("BONK"))


if __name__ == "__main__":
    unittest.main()
