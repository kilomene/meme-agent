"""Security tests (Phase 8): prove each fixed finding stays fixed.

Covers: secret redaction in CLI/config output, the SECRET_KEYS contract,
localhost-only API bind, event-poll limit clamping, and tool-argument
validation (symbol/direction/volume/ticket) at the registry/MCP/API
layers.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMP = tempfile.mkdtemp(prefix="forex_security_")

# Isolated storage for every test in this module.
os.environ["FOREX_AGENT_HOME"] = _TMP
os.environ["FOREX_AGENT_STORAGE"] = os.path.join(_TMP, "security.db")
os.environ.setdefault("BROKER_PROVIDER", "disconnected")


def _cli_env(**secrets):
    env = dict(os.environ)
    env["FOREX_AGENT_HOME"] = _TMP
    env["FOREX_AGENT_STORAGE"] = os.path.join(_TMP, "cli.db")
    env["BROKER_PROVIDER"] = "disconnected"
    env.update(secrets)
    return env


def _run_cli(*args, **secrets):
    cli = os.path.join(_ROOT, "scripts", "forex")
    proc = subprocess.run([sys.executable, cli, *args],
                          capture_output=True, text=True, cwd=_ROOT,
                          env=_cli_env(**secrets), timeout=60)
    try:
        payload = json.loads(proc.stdout) if proc.stdout.strip() else None
    except json.JSONDecodeError:
        payload = None
    return proc.returncode, payload


class _EnvGuard:
    """Save/restore os.environ keys around a test."""

    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, *exc):
        for key, old in self.saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        return False


class TestSecretRedaction(unittest.TestCase):
    def test_cli_config_redacts_webhook_url(self):
        # REGRESSION: forex config used to print NOTIFY_WEBHOOK_URL raw.
        _, payload = _run_cli(
            "config", "--json",
            NOTIFY_WEBHOOK_URL="https://hooks.example.com/x?token=WEBHOOKSECRET99")
        self.assertTrue(payload["ok"])
        blob = json.dumps(payload["config"])
        self.assertNotIn("WEBHOOKSECRET99", blob)
        self.assertEqual(payload["config"]["notifications"]["webhook_url"], "***")

    def test_cli_config_redacts_broker_credentials(self):
        # The memecoin provider has no broker credentials to redact; the
        # only secret left in the broker area is nothing. WORKER_API_KEY
        # must still be masked, and the wallet PUBLIC key stays visible.
        _, payload = _run_cli(
            "config", "--json",
            WORKER_API_KEY="WKSECRET88",
            MEMECOIN_WALLET_PUBKEY="WalletPubkeyNotSecret123")
        self.assertTrue(payload["ok"])
        blob = json.dumps(payload["config"])
        self.assertNotIn("WKSECRET88", blob)
        self.assertEqual(payload["config"]["worker"]["api_key"], "***")
        # This CLI env runs provider=disconnected; the wallet PUBLIC key is
        # public by definition and stays visible either way.
        self.assertEqual(payload["config"]["broker"]["provider"], "disconnected")
        self.assertIn("WalletPubkeyNotSecret123", blob)

    def test_redacted_masks_all_secret_fields(self):
        from config.config import load_config
        with _EnvGuard(WORKER_API_KEY="WKSECRET88",
                       MEMECOIN_WALLET_PUBKEY="WalletPubkeyNotSecret123",
                       NOTIFY_WEBHOOK_URL="https://h.example/x?token=TK9",
                       PROVIDER_OVERRIDE="irrelevant"):
            redacted = load_config().redacted()
        blob = json.dumps(redacted)
        for secret in ("WKSECRET88", "TK9"):
            self.assertNotIn(secret, blob)
        # The wallet public key is public by definition — never masked.
        self.assertIn("WalletPubkeyNotSecret123", blob)
        self.assertEqual(redacted["worker"]["api_key"], "***")
        self.assertEqual(redacted["notifications"]["webhook_url"], "***")

    def test_secret_keys_contract_covers_bearer_tokens(self):
        from config.config import SECRET_KEYS
        for key in ("WORKER_API_KEY", "NOTIFY_WEBHOOK_URL",
                    "NEWS_CALENDAR_API_KEY"):
            self.assertIn(key, SECRET_KEYS,
                          "%s must be in the never-log contract" % key)


class TestLocalApiHardening(unittest.TestCase):
    def test_binds_loopback_only(self):
        from scripts import local_api
        server = local_api.run(0)
        try:
            host, _port = server.server_address[:2]
            self.assertEqual(host, "127.0.0.1")
        finally:
            server.server_close()

    def test_events_limit_clamped(self):
        from agent.events import bus
        from scripts import local_api
        from storage import Store
        tmp = tempfile.mkdtemp(prefix="forex_sec_events_")
        store = Store(os.path.join(tmp, "events.db"))
        bus.reset_for_tests()
        try:
            bus.configure(store=store)
            for _ in range(3):
                bus.publish({"event": "signal.detected", "symbol": "EURUSD",
                             "timeframe": "M15", "direction": "BUY"})
            payload = local_api._events_payload({"limit": ["999999999"]})
            self.assertTrue(payload["ok"])
            self.assertLessEqual(payload["count"], 1000)
            latest = local_api._events_latest_payload({"limit": ["999999999"]})
            self.assertLessEqual(latest["count"], 1000)
        finally:
            bus.reset_for_tests()
            store.close()

    def test_events_bad_limit_rejected(self):
        from scripts import local_api
        with self.assertRaises(ValueError):
            local_api._clamp_limit({"limit": ["not-a-number"]})


class TestToolArgumentValidation(unittest.TestCase):
    def test_request_trade_rejects_empty_symbol(self):
        from agent.tools import registry
        result = registry.call("forex.request_trade", {
            "symbol": "", "direction": "BUY", "volume": 0.01,
            "stop_loss": 1.1000})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "INVALID_SYMBOL")

    def test_request_trade_rejects_bad_direction(self):
        from agent.tools import registry
        result = registry.call("forex.request_trade", {
            "symbol": "EURUSD", "direction": "HOLD", "volume": 0.01,
            "stop_loss": 1.1000})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "INVALID_DIRECTION")

    def test_request_trade_rejects_nonpositive_volume(self):
        from agent.tools import registry
        result = registry.call("forex.request_trade", {
            "symbol": "EURUSD", "direction": "BUY", "volume": -1,
            "stop_loss": 1.1000})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "INVALID_ARGUMENT")

    def test_close_position_rejects_malicious_ticket(self):
        from agent.tools import registry
        result = registry.call("forex.close_position",
                               {"ticket": "1; DROP TABLE journal"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "INVALID_TICKET")

    def test_mcp_surfaces_tool_validation_error(self):
        from agent.mcp import server
        response = server.handle_request({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "forex.request_trade",
                       "arguments": {"symbol": "EURUSD", "direction": "SIDEWAYS",
                                     "volume": 0.01, "stop_loss": 1.1}}})
        self.assertIn("result", response)
        self.assertTrue(response["result"]["isError"])
        content = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(content["error_code"], "INVALID_DIRECTION")

    def test_cli_shell_metachars_treated_as_data(self):
        # No shell anywhere between argv and the tool: metacharacters in
        # an argument must be inert data, never executed.
        marker = os.path.join(_TMP, "sec_pwned_marker")
        if os.path.exists(marker):
            os.remove(marker)
        _, payload = _run_cli("signal", "$(touch %s)" % marker, "--json")
        self.assertIsNotNone(payload)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload.get("found", True))
        self.assertFalse(os.path.exists(marker),
                         "shell metacharacters in CLI args were executed")


if __name__ == "__main__":
    unittest.main()
