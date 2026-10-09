"""Unit tests for the card pulse (daemon/telegram/blink.py).

Hermetic: the DexScreener fetch is monkeypatched; no network, no HOME reads.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.telegram import blink


def _ev(**kw):
    e = {"event": "launch.detected", "event_id": "evt1", "mint": "MINT1",
         "chain": "solana", "symbol": "TEST", "usd_market_cap": 20000.0}
    e.update(kw)
    return e


# -- classify ---------------------------------------------------------------
def test_classify_live():
    assert blink.classify(25000.0, 20000.0, 25000.0) == "live"
    assert blink.classify(20000.0, 20000.0, 20000.0) == "live"


def test_classify_dump_at_30pct():
    # exactly -30% from peak -> dump (boundary inclusive)
    assert blink.classify(7000.0, 10000.0, 10000.0) == "dump"
    assert blink.classify(6999.0, 10000.0, 12000.0) == "dump"


def test_classify_dump_needs_peak_drawdown_not_card():
    # -20% from peak is still live even if below card
    assert blink.classify(8000.0, 10000.0, 10000.0) == "live"


def test_classify_dead():
    assert blink.classify(900.0, 20000.0, 20000.0) == "dead"
    assert blink.classify(0.0, 20000.0, 20000.0) == "dead"


# -- render_status ------------------------------------------------------------
def test_render_status_shapes():
    assert blink.render_status("live", True, 125.0) == "\U0001F7E2 LIVE +125%"
    assert blink.render_status("live", False, 125.0) == "\u26AB LIVE +125%"
    assert blink.render_status("dump", True, -84.0) == "\U0001F534 DUMP \u221284%"
    assert blink.render_status("dump", False, -84.0) == "\u26AB DUMP \u221284%"
    assert blink.render_status("dead", False, -96.0) == "\u26AB ENDED \u221296%"


def test_render_status_timeframes():
    line = blink.render_status("live", True, 125.0, m5=8.0, h1=-30.0)
    assert line == "\U0001F7E2 LIVE +125% | 5m +8% \u00b7 1h \u221230%"
    # missing timeframes -> no suffix
    assert blink.render_status("live", True, 10.0) == "\U0001F7E2 LIVE +10%"


def test_parse_stats():
    data = [
        {"marketCap": 1000, "priceChange": {"m5": 1.5, "h1": -2.0}},
        {"marketCap": 5000, "fdv": 5000,
         "priceChange": {"m5": 7.7, "h1": -30.45}},
        {"nope": True},
    ]
    mcap, m5, h1 = blink._parse_stats(data)
    assert mcap == 5000.0 and m5 == 7.7 and h1 == -30.45
    assert blink._parse_stats([]) == (0.0, None, None)
    assert blink._parse_stats(None) == (0.0, None, None)


# -- initial_line / register ----------------------------------------------------
def test_initial_line_blinkable():
    line = blink.initial_line(_ev())
    assert line is not None and line.startswith("\U0001F7E2 LIVE")


def test_initial_line_skips_non_blink_events():
    assert blink.initial_line(_ev(event="launch.early_digest")) is None
    assert blink.initial_line(_ev(event="launch.top_picks")) is None


def test_initial_line_skips_missing_mint_or_mcap():
    assert blink.initial_line(_ev(mint=None)) is None
    assert blink.initial_line(_ev(usd_market_cap=0)) is None


def test_initial_line_accepts_milestone_card_mcap_key():
    e = _ev(event="launch.milestone")
    del e["usd_market_cap"]
    e["card_mcap"] = 5000.0
    assert blink.initial_line(e) is not None


def test_register_and_cap_eviction():
    state = {}
    for i in range(blink.BLINK_MAX + 3):
        e = _ev(event_id="evt%d" % i, mint="MINT%d" % i)
        assert blink.register(state, e, 1, 100 + i, "line0\nbody")
    reg = state["blink"]
    assert len(reg) == blink.BLINK_MAX
    # oldest three evicted
    assert "evt0" not in reg and "evt2" not in reg
    assert "evt%d" % (blink.BLINK_MAX + 2) in reg


# -- poll -----------------------------------------------------------------------
class _FakeClient:
    def __init__(self):
        self.edits = []

    def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))
        return {"ok": True}


def _reg_with(state, key="evt1", card=20000.0, peak=20000.0, age_s=10.0):
    import time
    state["blink"] = {key: {
        "chat_id": 7, "message_id": 42, "mint": "MINT1", "chain": "solana",
        "symbol": "TEST", "card_usd": card, "peak_usd": peak, "phase": True,
        "lines": ["\U0001F7E2 LIVE +0%", "body line"],
        "ts_card": time.time() - age_s}}


def test_poll_live_toggles_phase_and_edits():
    import time
    state = {}
    _reg_with(state)
    blink.fetch_stats = lambda mint, chain="solana", timeout=15.0: (26000.0, 8.0, 40.0)
    try:
        client = _FakeClient()
        done = blink.poll(state, client, now=time.time())
    finally:
        del blink.fetch_stats
    assert done["live"] == 1
    assert len(client.edits) == 1
    chat, mid, text = client.edits[0]
    assert text.split("\n")[0] == "\u26AB LIVE +30% | 5m +8% \u00b7 1h +40%"  # phase toggled off
    assert state["blink"]["evt1"]["phase"] is False
    assert state["blink"]["evt1"]["peak_usd"] == 26000.0


def test_poll_dump_and_dead():
    import time
    blink.fetch_stats = lambda mint, chain="solana", timeout=15.0: (6000.0, -5.0, -70.0)
    try:
        state = {}
        _reg_with(state, key="a", card=20000.0, peak=20000.0)
        client = _FakeClient()
        done = blink.poll(state, client, now=time.time())
        assert done["dump"] == 1
        # phase toggled True->False, so off-phase renders black
        assert client.edits[0][2].split("\n")[0] == "\u26AB DUMP \u221270% | 5m \u22125% \u00b7 1h \u221270%"
        # second poll toggles back on -> red shows
        done = blink.poll(state, client, now=time.time())
        assert client.edits[1][2].split("\n")[0] == "\U0001F534 DUMP \u221270% | 5m \u22125% \u00b7 1h \u221270%"

        state = {}
        _reg_with(state, key="b", card=20000.0, peak=20000.0)
        blink.fetch_stats = lambda mint, chain="solana", timeout=15.0: (500.0, -50.0, -97.0)
        client = _FakeClient()
        done = blink.poll(state, client, now=time.time())
        assert done["ended"] == 1
        assert "b" not in state["blink"]  # dropped after final edit
        assert client.edits[0][2].split("\n")[0].startswith("\u26AB ENDED")
    finally:
        del blink.fetch_stats


def test_poll_expiry_drops():
    import time
    state = {}
    _reg_with(state, age_s=blink.BLINK_TTL_S + 1)
    client = _FakeClient()
    done = blink.poll(state, client, now=time.time())
    assert done["ended"] == 1
    assert state["blink"] == {}


def test_poll_price_failure_keeps_state():
    import time
    blink.fetch_stats = lambda mint, chain="solana", timeout=15.0: (0.0, None, None)
    try:
        state = {}
        _reg_with(state)
        client = _FakeClient()
        done = blink.poll(state, client, now=time.time())
        assert done == {"live": 0, "dump": 0, "ended": 0, "failed": 0}
        assert "evt1" in state["blink"]  # kept for next poll
        assert client.edits == []
    finally:
        del blink.fetch_stats
