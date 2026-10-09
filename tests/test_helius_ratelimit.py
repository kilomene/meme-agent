"""Tests for the shared Helius rate-limit guard."""
import time
import urllib.error

from daemon.launch_scanner import helius_ratelimit as rl


def setup_function(_):
    rl.reset_for_tests()


def test_clear_by_default():
    assert rl.cooldown_remaining() == 0.0


def test_ratelimit_engages_cooldown():
    wait = rl.report_ratelimited()
    assert wait == 60.0
    assert rl.cooldown_remaining() > 50.0


def test_backoff_doubles_to_cap():
    waits = [rl.report_ratelimited() for _ in range(6)]
    assert waits == [60.0, 120.0, 240.0, 480.0, 600.0, 600.0]


def test_success_resets_backoff():
    rl.report_ratelimited()
    rl.report_ratelimited()
    rl.report_success()
    assert rl.report_ratelimited() == 60.0


def test_cooldown_expires():
    rl.report_ratelimited(now=time.time() - 61.0)
    assert rl.cooldown_remaining() == 0.0


def _fake_429(*a, **k):
    raise urllib.error.HTTPError("http://x", 429, "Too Many Requests",
                                 {}, None)


def _fake_ok(*a, **k):
    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"result": {"ok": true}}'
    return R()


def test_smartmoney_rpc_respects_cooldown(monkeypatch):
    import daemon.launch_scanner.smartmoney as sm
    monkeypatch.setattr(sm, "api_key", lambda: "k")
    rl.report_ratelimited()  # engage cooldown
    calls = []
    monkeypatch.setattr(sm.urllib.request, "urlopen",
                        lambda *a, **k: calls.append(1) or _fake_ok())
    assert sm._rpc("getSignaturesForAddress", []) is None
    assert calls == []  # no HTTP during cooldown


def test_smartmoney_rpc_429_engages_shared_cooldown(monkeypatch):
    import daemon.launch_scanner.smartmoney as sm
    monkeypatch.setattr(sm, "api_key", lambda: "k")
    monkeypatch.setattr(sm.urllib.request, "urlopen", _fake_429)
    assert sm._rpc("getSignaturesForAddress", []) is None
    assert rl.cooldown_remaining() > 50.0


def test_smartmoney_rpc_success_clears_backoff(monkeypatch):
    import daemon.launch_scanner.smartmoney as sm
    monkeypatch.setattr(sm, "api_key", lambda: "k")
    rl.report_ratelimited()  # 60s ladder
    rl.report_ratelimited()  # 120s ladder
    rl._cooldown_until = time.time() - 1  # let the cooldown lapse
    monkeypatch.setattr(sm.urllib.request, "urlopen",
                        lambda *a, **k: _fake_ok())
    assert sm._rpc("getSignaturesForAddress", []) == {"ok": True}
    assert rl.report_ratelimited() == 60.0  # ladder was reset by success


def test_deployer_rpc_shares_cooldown(monkeypatch):
    import daemon.launch_scanner.deployers as dp
    import daemon.launch_scanner.smartmoney as sm
    monkeypatch.setattr(dp, "api_key", lambda: "k")
    monkeypatch.setattr(sm, "api_key", lambda: "k")
    # 429 via smartmoney engages the cooldown deployers must respect
    monkeypatch.setattr(sm.urllib.request, "urlopen", _fake_429)
    assert sm._rpc("getSignaturesForAddress", []) is None
    calls = []
    monkeypatch.setattr(dp.urllib.request, "urlopen",
                        lambda *a, **k: calls.append(1) or _fake_ok())
    assert dp._rpc("getSignaturesForAddress", []) is None
    assert calls == []  # deployer skipped: shared cooldown
