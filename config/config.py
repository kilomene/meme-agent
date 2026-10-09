"""
Config loader — everything sensitive comes from environment variables
or a 0600 secrets file. Never hardcode account numbers, passwords, or
API keys in source, and never log them (use AppConfig.redacted()).

Layering: config/defaults.yaml holds every non-secret default;
environment variables override the file. Original bridge env keys are
preserved so existing .env files keep working.

Secrets file: set FOREX_SECRETS_FILE=/path/to/secrets.env containing
KEY=VALUE lines. The file should be mode 0600 — a warning is logged
otherwise (but loading still proceeds; failing closed here would
brick headless installs that manage perms differently).
"""

import json
import logging
import os
import stat
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

logger = logging.getLogger("config")

CONFIG_DIR = Path(__file__).resolve().parent
DEFAULTS_PATH = CONFIG_DIR / "defaults.yaml"

# Keys that must never appear in logs / events / error strings.
SECRET_KEYS = frozenset({
    "WORKER_API_KEY",
    "NOTIFY_WEBHOOK_URL",
    "NEWS_CALENDAR_API_KEY",
})

# Field names masked by AppConfig.redacted(). Mirrors SECRET_KEYS above
# (mapped to dataclass field names) plus the webhook URL, which may
# embed a token and must never be printed raw.
REDACTED_FIELDS = frozenset({
    "password", "api_key", "webhook_url", "bot_token", "telegram_bot_token",
})


def _load_secrets_file() -> None:
    path = os.environ.get("FOREX_SECRETS_FILE", "")
    if not path:
        return
    p = Path(path)
    if not p.is_file():
        raise RuntimeError(f"FOREX_SECRETS_FILE points at missing file: {path}")
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        logger.warning(
            "Secrets file %s has permissions %o (expected 0600) — tighten it.",
            path, mode,
        )
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_secrets_file()


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it before running (see config/defaults.yaml)."
        )
    return val


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("true", "1", "yes", "y")


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _list(name: str, default: List[str]) -> List[str]:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return list(default)
    return [s.strip() for s in raw.split(",") if s.strip()]


def _load_yaml_defaults() -> Dict[str, Any]:
    if yaml is None or not DEFAULTS_PATH.is_file():
        return {}
    with open(DEFAULTS_PATH) as f:
        return yaml.safe_load(f) or {}


def _yaml_mtime() -> int:
    try:
        return DEFAULTS_PATH.stat().st_mtime_ns
    except OSError:
        return 0


_YAML = _load_yaml_defaults()
_YAML_MTIME = _yaml_mtime()


def _maybe_reload_yaml() -> None:
    """Re-read defaults.yaml if it changed on disk since last load.

    Daemons call load_config() every cycle inside long-lived processes, but
    the YAML was cached at import time — so watchlist edits (manual or via
    the Telegram /add command) previously required a daemon restart. The
    mtime check makes them take effect on the next cycle instead.
    """
    global _YAML, _YAML_MTIME
    mtime = _yaml_mtime()
    if mtime != _YAML_MTIME:
        _YAML = _load_yaml_defaults()
        _YAML_MTIME = mtime


def _y(*keys: str, default: Any = None) -> Any:
    _maybe_reload_yaml()
    node: Any = _YAML
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


def _overrides_path() -> "Path":
    """~/.forex-agent/config_overrides.json (honors FOREX_AGENT_HOME).

    Written ONLY by the user's explicit ``/tune apply`` (idea #6) —
    nothing automatic ever touches it.
    """
    base = os.environ.get("FOREX_AGENT_HOME") or str(Path.home() / ".forex-agent")
    return Path(base) / "config_overrides.json"


def _load_degen_overrides() -> Dict[str, Any]:
    """User-approved degen knob overrides. Missing/corrupt -> {}.

    Never raises; a broken file must never stop the bot from starting.
    """
    try:
        raw = _overrides_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    node = data.get("degen")
    return dict(node) if isinstance(node, dict) else {}


@dataclass
class MemecoinConfig:
    """Settings for broker.provider=memecoin (broker/memecoin/RUNTIME.md)."""
    # Filesystem home for the paper engine: paper_state.json, fills.jsonl,
    # idempotency.json, killswitch file. Default:
    # $FOREX_AGENT_HOME/data/memecoin.
    data_dir: str = field(default_factory=lambda: _str(
        "MEMECOIN_DATA_DIR", _y("memecoin", "data_dir", default="")))
    # Virtual starting balance (SOL) for a fresh paper state.
    paper_start_sol: float = field(default_factory=lambda: _float(
        "MEMECOIN_PAPER_START_SOL", _y("memecoin", "paper_start_sol",
                                       default=10.0)))
    # Live-mode arming (necessary, NOT sufficient — live execution is not
    # implemented and the adapter fails closed; see RUNTIME.md).
    live_armed: bool = field(default_factory=lambda: _bool(
        "MEMECOIN_LIVE_ARMED", _y("memecoin", "live_armed", default=False)))
    # Wallet PUBLIC key only. The wallet secret must never leave the
    # operator's custody and is never read by this repo.
    wallet_pubkey: str = field(default_factory=lambda: _str(
        "MEMECOIN_WALLET_PUBKEY", _y("memecoin", "wallet_pubkey", default="")))
    # Extra trade symbols: {SYMBOL: mint}. SOL is always registered.
    symbols: Dict[str, str] = field(default_factory=lambda: _memecoin_symbols())
    # Multi-chain: enabled chain names (registry names; default solana).
    # Env MEMECOIN_CHAINS overrides as a comma list.
    chains: List[str] = field(default_factory=lambda: [
        c.strip().lower() for c in
        _list("MEMECOIN_CHAINS", _y("memecoin", "chains", default=["solana"]))
        if c.strip()
    ] or ["solana"])
    # Runtime wallet-key pattern (broker/memecoin/WALLET_SECURITY.md):
    # where a future live wallet secret is read from at runtime.
    # none = no key (paper default); env = MEMECOIN_PRIVATE_KEY_<CHAIN>
    # (fallback MEMECOIN_PRIVATE_KEY); file = key_file (0600 required).
    wallet_key_source: str = field(default_factory=lambda: _str(
        "MEMECOIN_WALLET_KEY_SOURCE",
        _y("memecoin", "wallet", "key_source", default="none")).strip().lower())
    wallet_key_file: str = field(default_factory=lambda: _str(
        "MEMECOIN_WALLET_KEY_FILE",
        _y("memecoin", "wallet", "key_file", default="")))
    # Devnet execution (testing only): when true, the memecoin adapter
    # runs in devnet mode — real devnet SOL transfers per fill, via
    # broker/memecoin/execution/. Requires wallet_pubkey plus a key
    # from wallet_key_source. Mainnet is never reachable from this flag;
    # the live switch stays a separate deliberate gate (see RUNTIME.md).
    devnet: bool = field(default_factory=lambda: _bool(
        "MEMECOIN_DEVNET", _y("memecoin", "devnet", default=False)))
    # Devnet RPC endpoint override (must still be a devnet host — the
    # execution layer refuses anything else).
    devnet_rpc_url: str = field(default_factory=lambda: _str(
        "MEMECOIN_DEVNET_RPC_URL",
        _y("memecoin", "devnet_rpc_url", default="")))


def _memecoin_symbols() -> Dict[str, str]:
    raw = _y("memecoin", "symbols", default=None)
    out: Dict[str, str] = {}
    if isinstance(raw, dict):
        for sym, mint in raw.items():
            if sym and mint:
                out[str(sym).upper()] = str(mint)
    # MEMECOIN_SYMBOLS env: "BONK:<mint>,WIF:<mint>"
    for item in _list("MEMECOIN_SYMBOLS", []):
        if ":" in item:
            sym, _, mint = item.partition(":")
            if sym.strip() and mint.strip():
                out[sym.strip().upper()] = mint.strip()
    return out


@dataclass
class BrokerConfig:
    provider: str = field(default_factory=lambda: _str("BROKER_PROVIDER", _y("broker", "provider", default="memecoin")))
    memecoin: MemecoinConfig = field(default_factory=MemecoinConfig)


@dataclass
class EmaRsiStrategyConfig:
    enabled: bool = True
    ema_fast: int = 20
    ema_slow: int = 50
    rsi_period: int = 14
    atr_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    atr_sl_multiplier: float = 1.5
    atr_tp_multiplier: float = 3.0


@dataclass
class TradingConfig:
    symbols: List[str] = field(default_factory=lambda: _list("SYMBOLS", _y("symbols", default=["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"])))
    timeframes: List[str] = field(default_factory=lambda: _list("TIMEFRAMES", _y("timeframes", default=["H1"])))
    # Back-compat: the original bridge had a single TIMEFRAME env.
    timeframe: str = field(default_factory=lambda: _str("TIMEFRAME", (_y("timeframes", default=["H1"]) or ["H1"])[0]))
    ema_rsi: EmaRsiStrategyConfig = field(default_factory=lambda: EmaRsiStrategyConfig(
        enabled=_bool("STRATEGY_EMA_RSI_ENABLED", _y("strategies", "ema_rsi", "enabled", default=True)),
        ema_fast=_int("EMA_FAST", _y("strategies", "ema_rsi", "ema_fast", default=20)),
        ema_slow=_int("EMA_SLOW", _y("strategies", "ema_rsi", "ema_slow", default=50)),
        rsi_period=_int("RSI_PERIOD", _y("strategies", "ema_rsi", "rsi_period", default=14)),
        atr_period=_int("ATR_PERIOD", _y("strategies", "ema_rsi", "atr_period", default=14)),
        rsi_overbought=_float("RSI_OVERBOUGHT", _y("strategies", "ema_rsi", "rsi_overbought", default=70.0)),
        rsi_oversold=_float("RSI_OVERSOLD", _y("strategies", "ema_rsi", "rsi_oversold", default=30.0)),
        atr_sl_multiplier=_float("ATR_SL_MULTIPLIER", _y("strategies", "ema_rsi", "atr_sl_multiplier", default=1.5)),
        atr_tp_multiplier=_float("ATR_TP_MULTIPLIER", _y("strategies", "ema_rsi", "atr_tp_multiplier", default=3.0)),
    ))


@dataclass
class RiskConfig:
    """Guardrails — fractions of EQUITY unless noted. These exist so a
    bug or a bad signal can't blow the account."""
    fixed_lot_size: float = field(default_factory=lambda: _float("FIXED_LOT_SIZE", _y("risk", "fixed_lot_size", default=0.01)))
    use_percent_risk_sizing: bool = field(default_factory=lambda: _bool("USE_PERCENT_RISK_SIZING", _y("risk", "use_percent_risk_sizing", default=False)))
    # Original bridge used percent-style env keys (1.0 = 1%); yaml uses
    # fractions (0.01 = 1%). Env wins when set.
    max_risk_per_trade: float = field(default_factory=lambda: (
        _float("MAX_RISK_PER_TRADE_PCT", 0) / 100.0
        if os.environ.get("MAX_RISK_PER_TRADE_PCT") else
        _float("MAX_RISK_PER_TRADE", _y("risk", "max_risk_per_trade", default=0.01))
    ))
    max_daily_loss: float = field(default_factory=lambda: (
        _float("MAX_DAILY_LOSS_PCT", 0) / 100.0
        if os.environ.get("MAX_DAILY_LOSS_PCT") else
        _float("MAX_DAILY_LOSS", _y("risk", "max_daily_loss", default=0.03))
    ))
    max_open_positions: int = field(default_factory=lambda: _int("MAX_OPEN_POSITIONS", _y("risk", "max_open_positions", default=3)))
    max_total_exposure_lots: float = field(default_factory=lambda: _float("MAX_TOTAL_EXPOSURE_LOTS", _y("risk", "max_total_exposure_lots", default=1.0)))
    max_consecutive_losses: int = field(default_factory=lambda: _int("MAX_CONSECUTIVE_LOSSES", _y("risk", "max_consecutive_losses", default=4)))
    max_correlated_positions: int = field(default_factory=lambda: _int("MAX_CORRELATED_POSITIONS", _y("risk", "max_correlated_positions", default=1)))
    require_stop_loss: bool = field(default_factory=lambda: _bool("REQUIRE_SL", _y("risk", "require_stop_loss", default=True)))
    min_account_equity: float = field(default_factory=lambda: _float("MIN_ACCOUNT_EQUITY", _y("risk", "min_account_equity", default=0.0)))
    max_spread_points: float = field(default_factory=lambda: _float("MAX_SPREAD_POINTS", _y("risk", "max_spread_points", default=0.0)))


@dataclass
class AutonomyConfig:
    # signals_only | assisted | autonomous
    mode: str = field(default_factory=lambda: _str("AUTONOMY_MODE", _y("autonomy", "mode", default="assisted")))


@dataclass
class WorkerConfig:
    enabled: bool = field(default_factory=lambda: _bool("WORKER_ENABLED", _y("worker", "enabled", default=False)))
    base_url: str = field(default_factory=lambda: _str("WORKER_BASE_URL", _y("worker", "url", default="")))
    api_key: str = field(default_factory=lambda: _str("WORKER_API_KEY", ""))
    poll_interval_seconds: int = field(default_factory=lambda: _int("POLL_INTERVAL_SECONDS", _y("worker", "poll_interval_seconds", default=30)))


@dataclass
class ExitManagerConfig:
    """Every action here can only reduce risk (tighten a stop, close an
    overdue trade) — never loosen a stop or add exposure."""
    enabled: bool = field(default_factory=lambda: _bool("EXIT_MANAGER_ENABLED", _y("exits", "enabled", default=True)))
    max_hold_hours: float = field(default_factory=lambda: _float("MAX_HOLD_HOURS", _y("exits", "max_hold_hours", default=48.0)))
    breakeven_trigger_atr: float = field(default_factory=lambda: _float("BREAKEVEN_TRIGGER_ATR", _y("exits", "breakeven_trigger_atr", default=1.0)))
    trailing_activation_atr: float = field(default_factory=lambda: _float("TRAILING_ACTIVATION_ATR", _y("exits", "trailing_activation_atr", default=2.0)))
    trailing_atr_multiplier: float = field(default_factory=lambda: _float("TRAILING_ATR_MULTIPLIER", _y("exits", "trailing_atr_multiplier", default=1.5)))
    check_interval_seconds: int = field(default_factory=lambda: _int("EXIT_CHECK_INTERVAL_SECONDS", _y("exits", "check_interval_seconds", default=60)))


@dataclass
class DegenConfig:
    """Degen launch radar: real-time new-coin detection + paper auto-entry.

    Scans pump.fun launches, DexScreener boosts (solana/base/ethereum/
    robinhood) and Hyperliquid spot listings. Qualified launches get a
    paper BUY (fixed size, idempotent per mint) and ALWAYS a Telegram card
    with the CA. Paper-only by construction — entries go through the same
    paper adapter and kill-switch as everything else.
    """
    enabled: bool = field(default_factory=lambda: _bool("DEGEN_ENABLED", _y("degen", "enabled", default=True)))
    poll_interval_seconds: int = field(default_factory=lambda: _int("DEGEN_POLL_SECONDS", _y("degen", "poll_interval_seconds", default=30)))
    entry_sol: float = field(default_factory=lambda: _float("DEGEN_ENTRY_SOL", _y("degen", "entry_sol", default=0.05)))
    max_positions: int = field(default_factory=lambda: _int("DEGEN_MAX_POSITIONS", _y("degen", "max_positions", default=5)))
    min_usd_market_cap: float = field(default_factory=lambda: _float("DEGEN_MIN_USD_MCAP", _y("degen", "min_usd_market_cap", default=10000.0)))
    max_age_minutes: float = field(default_factory=lambda: _float("DEGEN_MAX_AGE_MINUTES", _y("degen", "max_age_minutes", default=10.0)))
    velocity_multiplier: float = field(default_factory=lambda: _float("DEGEN_VELOCITY_MULT", _y("degen", "velocity_multiplier", default=3.0)))
    velocity_window_minutes: float = field(default_factory=lambda: _float("DEGEN_VELOCITY_WINDOW_MIN", _y("degen", "velocity_window_minutes", default=10.0)))
    # Dedup-memory hygiene: state["seen"] accumulates one entry per observed
    # mint forever. Without pruning it grows to ~100MB and every per-cycle
    # state dump becomes a giant write that stalls the SQLite backup API
    # (2026-09-29: 106MB dumps every ~90s). Entries idle longer than
    # seen_retention_hours are dropped (a coin re-running after that is
    # treated as a fresh listing); seen_max_entries is a hard backstop
    # keeping the most recently active entries.
    seen_retention_hours: float = field(default_factory=lambda: _float("DEGEN_SEEN_RETENTION_HOURS", _y("degen", "seen_retention_hours", default=48.0)))
    seen_max_entries: int = field(default_factory=lambda: _int("DEGEN_SEEN_MAX_ENTRIES", _y("degen", "seen_max_entries", default=100000)))
    # State dumps no longer happen every cycle: at most one full
    # daemon_state write per interval (crash recovery may lose up to one
    # interval of dedup watermarks — worst case a duplicate alert).
    state_save_interval_seconds: float = field(default_factory=lambda: _float("DEGEN_STATE_SAVE_INTERVAL_SEC", _y("degen", "state_save_interval_seconds", default=300.0)))
    stop_loss_pct: float = field(default_factory=lambda: _float("DEGEN_STOP_LOSS_PCT", _y("degen", "stop_loss_pct", default=30.0)))
    take_profit_pct: float = field(default_factory=lambda: _float("DEGEN_TAKE_PROFIT_PCT", _y("degen", "take_profit_pct", default=200.0)))
    max_hold_hours: float = field(default_factory=lambda: _float("DEGEN_MAX_HOLD_HOURS", _y("degen", "max_hold_hours", default=4.0)))
    # Two-tier exits: leg A uses the knobs above; leg B (runner half)
    # skips the fixed TP and holds up to tier_b_max_hold_hours.
    two_tier_enabled: bool = field(default_factory=lambda: _bool("DEGEN_TWO_TIER_ENABLED", _y("degen", "two_tier_enabled", default=True)))
    tier_b_max_hold_hours: float = field(default_factory=lambda: _float("DEGEN_TIER_B_MAX_HOLD_HOURS", _y("degen", "tier_b_max_hold_hours", default=24.0)))
    # Proof-of-demand checks (2026-10-02, measurement only): knobs for
    # the LP-lock / holder-concentration / distribution checks. Nothing
    # gates on them yet.
    proof_enabled: bool = field(default_factory=lambda: _bool("DEGEN_PROOF_ENABLED", _y("degen", "proof_enabled", default=True)))
    proof_holder_top10_max_pct: float = field(default_factory=lambda: _float("DEGEN_PROOF_HOLDER_TOP10_MAX_PCT", _y("degen", "proof_holder_top10_max_pct", default=40.0)))
    proof_holder_single_max_pct: float = field(default_factory=lambda: _float("DEGEN_PROOF_HOLDER_SINGLE_MAX_PCT", _y("degen", "proof_holder_single_max_pct", default=20.0)))
    proof_dump_sell_ratio: float = field(default_factory=lambda: _float("DEGEN_PROOF_DUMP_SELL_RATIO", _y("degen", "proof_dump_sell_ratio", default=2.0)))
    proof_insider_max_pct: float = field(default_factory=lambda: _float("DEGEN_PROOF_INSIDER_MAX_PCT", _y("degen", "proof_insider_max_pct", default=20.0)))
    proof_timeout_s: float = field(default_factory=lambda: _float("DEGEN_PROOF_TIMEOUT_S", _y("degen", "proof_timeout_s", default=8.0)))
    # Footprint signals (2026-10-02, measurement only): holder-velocity
    # and accumulation, recorded per scored runner. Nothing gates yet.
    footprints_enabled: bool = field(default_factory=lambda: _bool("DEGEN_FOOTPRINTS_ENABLED", _y("degen", "footprints_enabled", default=True)))
    fp_velocity_crowd_per_min: float = field(default_factory=lambda: _float("DEGEN_FP_VELOCITY_CROWD_PER_MIN", _y("degen", "fp_velocity_crowd_per_min", default=1.0)))
    fp_velocity_min_window_min: float = field(default_factory=lambda: _float("DEGEN_FP_VELOCITY_MIN_WINDOW_MIN", _y("degen", "fp_velocity_min_window_min", default=5.0)))
    fp_accum_vol_accel: float = field(default_factory=lambda: _float("DEGEN_FP_ACCUM_VOL_ACCEL", _y("degen", "fp_accum_vol_accel", default=2.0)))
    fp_accum_price_flat_pct: float = field(default_factory=lambda: _float("DEGEN_FP_ACCUM_PRICE_FLAT_PCT", _y("degen", "fp_accum_price_flat_pct", default=10.0)))
    auto_enter: bool = field(default_factory=lambda: _bool("DEGEN_AUTO_ENTER", _y("degen", "auto_enter", default=True)))
    # Strict runner confirmation (stage 2): a candidate only becomes an
    # alerted runner when ALL of these hold.
    confirm_gain_mult: float = field(default_factory=lambda: _float("DEGEN_CONFIRM_GAIN_MULT", _y("degen", "confirm_gain_mult", default=1.8)))
    confirm_rising_polls: int = field(default_factory=lambda: _int("DEGEN_CONFIRM_RISING_POLLS", _y("degen", "confirm_rising_polls", default=3)))
    confirm_min_m5_volume_usd: float = field(default_factory=lambda: _float("DEGEN_CONFIRM_MIN_M5_VOL", _y("degen", "confirm_min_m5_volume_usd", default=3000.0)))
    confirm_min_liquidity_usd: float = field(default_factory=lambda: _float("DEGEN_CONFIRM_MIN_LIQ", _y("degen", "confirm_min_liquidity_usd", default=12000.0)))
    confirm_min_buys_m5: int = field(default_factory=lambda: _int("DEGEN_CONFIRM_MIN_BUYS", _y("degen", "confirm_min_buys_m5", default=5)))
    confirm_min_buy_sell_ratio: float = field(default_factory=lambda: _float("DEGEN_CONFIRM_BUY_SELL_RATIO", _y("degen", "confirm_min_buy_sell_ratio", default=1.5)))
    confirm_window_minutes: float = field(default_factory=lambda: _float("DEGEN_CONFIRM_WINDOW_MIN", _y("degen", "confirm_window_minutes", default=30.0)))
    safety_gate: bool = field(default_factory=lambda: _bool("DEGEN_SAFETY_GATE", _y("degen", "safety_gate", default=True)))
    safety_fail_closed: bool = field(default_factory=lambda: _bool("DEGEN_SAFETY_FAIL_CLOSED", _y("degen", "safety_fail_closed", default=False)))
    trail_trigger_pct: float = field(default_factory=lambda: _float("DEGEN_TRAIL_TRIGGER_PCT", _y("degen", "trail_trigger_pct", default=50.0)))
    trail_drawdown_pct: float = field(default_factory=lambda: _float("DEGEN_TRAIL_DRAWDOWN_PCT", _y("degen", "trail_drawdown_pct", default=30.0)))
    # Leg A profit lock (v2 exits, 2026-10-02): bank profit early on the
    # classic half. Not in the feedback TUNABLE set — fixed policy.
    leg_a_trail_trigger_pct: float = field(default_factory=lambda: _float("DEGEN_LEG_A_TRAIL_TRIGGER_PCT", _y("degen", "leg_a_trail_trigger_pct", default=20.0)))
    leg_a_trail_drawdown_pct: float = field(default_factory=lambda: _float("DEGEN_LEG_A_TRAIL_DRAWDOWN_PCT", _y("degen", "leg_a_trail_drawdown_pct", default=15.0)))
    daily_max_loss_sol: float = field(default_factory=lambda: _float("DEGEN_DAILY_MAX_LOSS_SOL", _y("degen", "daily_max_loss_sol", default=0.15)))
    bundle_max_cluster_pct: float = field(default_factory=lambda: _float("DEGEN_BUNDLE_MAX_CLUSTER_PCT", _y("degen", "bundle_max_cluster_pct", default=30.0)))
    smart_poll_seconds: float = field(default_factory=lambda: _float("DEGEN_SMART_POLL_SECONDS", _y("degen", "smart_poll_seconds", default=180.0)))
    smart_boost_wallets: int = field(default_factory=lambda: _int("DEGEN_SMART_BOOST_WALLETS", _y("degen", "smart_boost_wallets", default=2)))
    smart_rising_polls: int = field(default_factory=lambda: _int("DEGEN_SMART_RISING_POLLS", _y("degen", "smart_rising_polls", default=2)))
    # Instant (tier-1) alert tuning ------------------------------------
    # Flash safety: run the cheap RugCheck/GoPlus screen with a short
    # timeout BEFORE the instant CA drops. Hard fails suppress the
    # instant alert (the coin stays a silent candidate); unknown/API
    # trouble fails CLOSED — no score, no CA (strict 1/100 rule).
    # Disabling the flash screen disables instant CAs entirely.
    early_safety_flash: bool = field(default_factory=lambda: _bool("DEGEN_EARLY_SAFETY_FLASH", _y("degen", "early_safety_flash", default=True)))
    early_flash_timeout_s: float = field(default_factory=lambda: _float("DEGEN_EARLY_FLASH_TIMEOUT_S", _y("degen", "early_flash_timeout_s", default=4.0)))
    # Instant-CA quality gate: the CA drops instantly ONLY when fast
    # analysis marks the coin a good candidate — known safety report,
    # no hard fails, score >= early_min_score. Unknown/unanalyzed coins
    # are held silently (they can still confirm later). No raw CA spam.
    early_min_score: float = field(default_factory=lambda: _float("DEGEN_EARLY_MIN_SCORE", _y("degen", "early_min_score", default=1.0)))
    # RugCheck insider gates (Solana): hard fail when funding-graph
    # insider clusters hold more than this share of supply, or when more
    # than graph_insiders_max insider wallets are detected. Bundled
    # launches split supply across fresh wallets so top-holder
    # concentration looks clean — this is the hole the gates close.
    insider_cluster_max_pct: float = field(default_factory=lambda: _float("DEGEN_INSIDER_CLUSTER_MAX_PCT", _y("degen", "insider_cluster_max_pct", default=20.0)))
    graph_insiders_max: int = field(default_factory=lambda: _int("DEGEN_GRAPH_INSERS_MAX", _y("degen", "graph_insiders_max", default=5)))
    # EVM sell simulation (Base/Ethereum): free Honeypot.is buy->sell
    # simulation layered on top of GoPlus — "can a buyer actually
    # sell?". Fail-closed on a detected honeypot; API trouble only adds
    # a flag (the GoPlus screen still gated the coin).
    evm_sell_sim_enabled: bool = field(default_factory=lambda: _bool("DEGEN_EVM_SELL_SIM", _y("degen", "evm_sell_sim_enabled", default=True)))
    evm_sell_sim_timeout_s: float = field(default_factory=lambda: _float("DEGEN_EVM_SELL_SIM_TIMEOUT_S", _y("degen", "evm_sell_sim_timeout_s", default=3.0)))
    # Spam guard: at most early_alert_max_per_window instant alerts per
    # early_alert_window_seconds; overflow is queued into one digest card.
    early_alert_max_per_window: int = field(default_factory=lambda: _int("DEGEN_EARLY_MAX_PER_WINDOW", _y("degen", "early_alert_max_per_window", default=10)))
    early_alert_window_seconds: float = field(default_factory=lambda: _float("DEGEN_EARLY_WINDOW_S", _y("degen", "early_alert_window_seconds", default=300.0)))
    # Low-cap runner hunter: catch running coins at LOW mcap before they
    # go viral. A CA drops only when the run from the sub-ceiling
    # baseline lands inside [lowcap_min_run_pct, lowcap_max_run_pct]
    # (default 70–100%) AND the fast quality gate passes (known safety
    # report, no hard fails, min score). Alert-only, UNCONFIRMED — never
    # a paper entry; the confirmed pipeline stays the only entry path.
    lowcap_enabled: bool = field(default_factory=lambda: _bool("DEGEN_LOWCAP_ENABLED", _y("degen", "lowcap_enabled", default=True)))
    lowcap_mcap_ceiling_usd: float = field(default_factory=lambda: _float("DEGEN_LOWCAP_MCAP_CEILING", _y("degen", "lowcap_mcap_ceiling_usd", default=500000.0)))
    lowcap_min_run_pct: float = field(default_factory=lambda: _float("DEGEN_LOWCAP_MIN_RUN_PCT", _y("degen", "lowcap_min_run_pct", default=70.0)))
    lowcap_max_run_pct: float = field(default_factory=lambda: _float("DEGEN_LOWCAP_MAX_RUN_PCT", _y("degen", "lowcap_max_run_pct", default=100.0)))
    lowcap_rising_polls: int = field(default_factory=lambda: _int("DEGEN_LOWCAP_RISING_POLLS", _y("degen", "lowcap_rising_polls", default=2)))
    lowcap_min_m5_volume_usd: float = field(default_factory=lambda: _float("DEGEN_LOWCAP_MIN_M5_VOL", _y("degen", "lowcap_min_m5_volume_usd", default=2000.0)))
    lowcap_min_liquidity_usd: float = field(default_factory=lambda: _float("DEGEN_LOWCAP_MIN_LIQ", _y("degen", "lowcap_min_liquidity_usd", default=8000.0)))
    lowcap_min_buys_m5: int = field(default_factory=lambda: _int("DEGEN_LOWCAP_MIN_BUYS", _y("degen", "lowcap_min_buys_m5", default=4)))
    lowcap_min_buy_sell_ratio: float = field(default_factory=lambda: _float("DEGEN_LOWCAP_BUY_SELL_RATIO", _y("degen", "lowcap_min_buy_sell_ratio", default=1.3)))
    lowcap_window_minutes: float = field(default_factory=lambda: _float("DEGEN_LOWCAP_WINDOW_MIN", _y("degen", "lowcap_window_minutes", default=30.0)))
    lowcap_fp_boost_enabled: bool = field(default_factory=lambda: _bool("DEGEN_LOWCAP_FP_BOOST", _y("degen", "lowcap_fp_boost_enabled", default=True)))
    # Early-fingerprint toolkit (fingerprints.py): per-mint buy/volume
    # acceleration + holder growth -> composite 0-100 HOT/WARMING/COLD.
    fp_enabled: bool = field(default_factory=lambda: _bool("DEGEN_FP_ENABLED", _y("degen", "fp_enabled", default=True)))
    fp_window_polls: int = field(default_factory=lambda: _int("DEGEN_FP_WINDOW_POLLS", _y("degen", "fp_window_polls", default=6)))
    fp_min_polls: int = field(default_factory=lambda: _int("DEGEN_FP_MIN_POLLS", _y("degen", "fp_min_polls", default=3)))
    fp_buy_floor: float = field(default_factory=lambda: _float("DEGEN_FP_BUY_FLOOR", _y("degen", "fp_buy_floor", default=3.0)))
    fp_vol_floor_usd: float = field(default_factory=lambda: _float("DEGEN_FP_VOL_FLOOR", _y("degen", "fp_vol_floor_usd", default=1000.0)))
    fp_hot_score: float = field(default_factory=lambda: _float("DEGEN_FP_HOT_SCORE", _y("degen", "fp_hot_score", default=70.0)))
    fp_warm_score: float = field(default_factory=lambda: _float("DEGEN_FP_WARM_SCORE", _y("degen", "fp_warm_score", default=45.0)))
    fp_holder_refresh_s: float = field(default_factory=lambda: _float("DEGEN_FP_HOLDER_REFRESH_S", _y("degen", "fp_holder_refresh_s", default=600.0)))
    fp_holder_per_cycle: int = field(default_factory=lambda: _int("DEGEN_FP_HOLDER_PER_CYCLE", _y("degen", "fp_holder_per_cycle", default=3)))
    # Sub-second detection: subscribe to Solana program logs for new
    # mints (pump.fun). Deploys a background thread; degrades gracefully
    # to polling when the websocket is unreachable.
    logstream_enabled: bool = field(default_factory=lambda: _bool("DEGEN_LOGSTREAM_ENABLED", _y("degen", "logstream_enabled", default=True)))
    # USDC-pair launch discovery: newest USDC-quoted pools via free
    # GeckoTerminal new_pools (Raydium/Meteora/Orca on Solana,
    # Aerodrome/Uniswap on Base, Uniswap on Ethereum). Polled ~1/min.
    usdc_pools_enabled: bool = field(default_factory=lambda: _bool("DEGEN_USDC_POOLS_ENABLED", _y("degen", "usdc_pools_enabled", default=True)))
    # Established-coin momentum scanner (momentum.py): GeckoTerminal
    # trending pools on the configured networks (default solana + base
    # + eth), qualified by 24h/1h move + volume + liquidity, then
    # through the SAME safety gate as launches (score >= early_min_score,
    # no hard fails, unknown = silent). Polled every momentum_scan_seconds.
    momentum_enabled: bool = field(default_factory=lambda: _bool("DEGEN_MOMENTUM_ENABLED", _y("degen", "momentum_enabled", default=True)))
    momentum_scan_seconds: int = field(default_factory=lambda: _int("DEGEN_MOMENTUM_SCAN_SECONDS", _y("degen", "momentum_scan_seconds", default=300)))
    momentum_networks: List[str] = field(default_factory=lambda: _list("DEGEN_MOMENTUM_NETWORKS", _y("degen", "momentum_networks", default=["solana", "base", "eth"])))
    momentum_min_gain_24h: float = field(default_factory=lambda: _float("DEGEN_MOMENTUM_MIN_GAIN_24H", _y("degen", "momentum_min_gain_24h", default=20.0)))
    momentum_max_gain_24h: float = field(default_factory=lambda: _float("DEGEN_MOMENTUM_MAX_GAIN_24H", _y("degen", "momentum_max_gain_24h", default=300.0)))
    momentum_max_drawdown_1h: float = field(default_factory=lambda: _float("DEGEN_MOMENTUM_MAX_DRAWDOWN_1H", _y("degen", "momentum_max_drawdown_1h", default=15.0)))
    momentum_min_vol_24h_usd: float = field(default_factory=lambda: _float("DEGEN_MOMENTUM_MIN_VOL_24H_USD", _y("degen", "momentum_min_vol_24h_usd", default=100000.0)))
    momentum_min_liq_usd: float = field(default_factory=lambda: _float("DEGEN_MOMENTUM_MIN_LIQ_USD", _y("degen", "momentum_min_liq_usd", default=50000.0)))
    momentum_cooldown_hours: float = field(default_factory=lambda: _float("DEGEN_MOMENTUM_COOLDOWN_HOURS", _y("degen", "momentum_cooldown_hours", default=6.0)))
    momentum_lowcap_max_mcap_usd: float = field(default_factory=lambda: _float("DEGEN_MOMENTUM_LOWCAP_MAX_MCAP_USD", _y("degen", "momentum_lowcap_max_mcap_usd", default=10000000.0)))
    momentum_lowcap_min_mcap_usd: float = field(default_factory=lambda: _float("DEGEN_MOMENTUM_LOWCAP_MIN_MCAP_USD", _y("degen", "momentum_lowcap_min_mcap_usd", default=50000.0)))
    # Shadow ledger: paper-simulate blocked runners (measurement only, never trades).
    shadow_enabled: bool = field(default_factory=lambda: _bool("DEGEN_SHADOW_ENABLED", _y("degen", "shadow_enabled", default=True)))
    shadow_max_open: int = field(default_factory=lambda: _int("DEGEN_SHADOW_MAX_OPEN", _y("degen", "shadow_max_open", default=20)))
    shadow_stop_pct: float = field(default_factory=lambda: _float("DEGEN_SHADOW_STOP_PCT", _y("degen", "shadow_stop_pct", default=30.0)))
    shadow_tp_pct: float = field(default_factory=lambda: _float("DEGEN_SHADOW_TP_PCT", _y("degen", "shadow_tp_pct", default=200.0)))
    shadow_max_hold_hours: float = field(default_factory=lambda: _float("DEGEN_SHADOW_MAX_HOLD_HOURS", _y("degen", "shadow_max_hold_hours", default=24.0)))
    shadow_pending_timeout_minutes: float = field(default_factory=lambda: _float("DEGEN_SHADOW_PENDING_TIMEOUT_MINUTES", _y("degen", "shadow_pending_timeout_minutes", default=60.0)))
    # Early-warning lane: UNCONFIRMED alerts for gate-held coins that are
    # still showing run signals. Main 1/100 stream untouched.
    warn_enabled: bool = field(default_factory=lambda: _bool("DEGEN_WARN_ENABLED", _y("degen", "warn_enabled", default=True)))
    warn_min_score: float = field(default_factory=lambda: _float("DEGEN_WARN_MIN_SCORE", _y("degen", "warn_min_score", default=1.0)))
    warn_max_per_hour: int = field(default_factory=lambda: _int("DEGEN_WARN_MAX_PER_HOUR", _y("degen", "warn_max_per_hour", default=4)))
    warn_cooldown_hours: float = field(default_factory=lambda: _float("DEGEN_WARN_COOLDOWN_HOURS", _y("degen", "warn_cooldown_hours", default=0.5)))
    warn_min_buys_m5: float = field(default_factory=lambda: _float("DEGEN_WARN_MIN_BUYS_M5", _y("degen", "warn_min_buys_m5", default=20.0)))
    warn_min_vol_m5_usd: float = field(default_factory=lambda: _float("DEGEN_WARN_MIN_VOL_M5_USD", _y("degen", "warn_min_vol_m5_usd", default=5000.0)))
    # Insider behavior tracker: snapshot + trend of insider supply share.
    insider_track_enabled: bool = field(default_factory=lambda: _bool("DEGEN_INSIDER_TRACK_ENABLED", _y("degen", "insider_track_enabled", default=True)))
    insider_watch_max: int = field(default_factory=lambda: _int("DEGEN_INSIDER_WATCH_MAX", _y("degen", "insider_watch_max", default=40)))
    insider_refresh_minutes: float = field(default_factory=lambda: _float("DEGEN_INSIDER_REFRESH_MINUTES", _y("degen", "insider_refresh_minutes", default=15.0)))
    insider_fast_minutes: float = field(default_factory=lambda: _float("DEGEN_INSIDER_FAST_MINUTES", _y("degen", "insider_fast_minutes", default=5.0)))
    insider_fast_window_minutes: float = field(default_factory=lambda: _float("DEGEN_INSIDER_FAST_WINDOW_MINUTES", _y("degen", "insider_fast_window_minutes", default=60.0)))
    insider_ledger_max: int = field(default_factory=lambda: _int("DEGEN_INSIDER_LEDGER_MAX", _y("degen", "insider_ledger_max", default=200)))
    # Failover RPC pool for the logstream's transaction fetches
    # (comma-separated env DEGEN_LOGSTREAM_RPC_URLS overrides).
    # Failover RPC pool for the logstream's transaction fetches
    # (comma-separated env DEGEN_LOGSTREAM_RPC_URLS overrides).
    # Mirrors daemon.launch_scanner.logstream.HTTP_RPC_URLS; kept inline
    # here to avoid a config -> daemon import cycle.
    logstream_rpc_urls: List[str] = field(default_factory=lambda: _list("DEGEN_LOGSTREAM_RPC_URLS", _y("degen", "logstream_rpc_urls", default=["https://api.mainnet-beta.solana.com", "https://solana.public-rpc.com", "https://rpc.ankr.com/solana"])))
    # Social sentiment tracker (sentiment.py): keyless DexScreener
    # profile velocity -> HOT/WARM/NEUTRAL. Soft boost only — this
    # module never allows, blocks, or drops a CA.
    sentiment_enabled: bool = field(default_factory=lambda: _bool("DEGEN_SENTIMENT_ENABLED", _y("degen", "sentiment_enabled", default=True)))
    sentiment_per_cycle: int = field(default_factory=lambda: _int("DEGEN_SENTIMENT_PER_CYCLE", _y("degen", "sentiment_per_cycle", default=2)))
    # AI narrative picker + heat tracker (narratives.py): classify each
    # coin's narrative and learn which narratives are running hot from
    # closed outcomes. Soft signal only — never a gate.
    narrative_enabled: bool = field(default_factory=lambda: _bool("DEGEN_NARRATIVE_ENABLED", _y("degen", "narrative_enabled", default=True)))
    # Backtesting engine + dataset recorder (backtest.py): every closed
    # candidate is appended to a JSONL dataset; scripts/backtest replays
    # it through the entry/exit rules to prove which gates earn their keep.
    backtest_enabled: bool = field(default_factory=lambda: _bool("DEGEN_BACKTEST_ENABLED", _y("degen", "backtest_enabled", default=True)))
    backtest_dataset_path: str = field(default_factory=lambda: _str("DEGEN_BACKTEST_DATASET_PATH", _y("degen", "backtest_dataset_path", default="~/.forex-agent/launch_scanner/backtest_dataset.jsonl")))
    # Sniper tier: runner-probability ranking of quality-passed coins
    # (sniper.py). The safety gate (score >= early_min_score, currently
    # 1/100) runs FIRST and is untouched — the ranker only orders the
    # survivors. Tier-1 (>= threshold) stages for the instant lane
    # (4/hour cap); sub-tier joins the digest queue. Missing signals
    # contribute 0 and can never boost.
    sniper_enabled: bool = field(default_factory=lambda: _bool("DEGEN_SNIPER_ENABLED", _y("degen", "sniper_enabled", default=True)))
    sniper_tier_threshold: float = field(default_factory=lambda: _float("DEGEN_SNIPER_TIER_THRESHOLD", _y("degen", "sniper_tier_threshold", default=50.0)))
    sniper_weight_fp: float = field(default_factory=lambda: _float("DEGEN_SNIPER_WEIGHT_FP", _y("degen", "sniper_weight_fp", default=30.0)))
    sniper_weight_smart: float = field(default_factory=lambda: _float("DEGEN_SNIPER_WEIGHT_SMART", _y("degen", "sniper_weight_smart", default=35.0)))
    sniper_weight_velocity: float = field(default_factory=lambda: _float("DEGEN_SNIPER_WEIGHT_VELOCITY", _y("degen", "sniper_weight_velocity", default=20.0)))
    sniper_weight_pressure: float = field(default_factory=lambda: _float("DEGEN_SNIPER_WEIGHT_PRESSURE", _y("degen", "sniper_weight_pressure", default=10.0)))
    sniper_boost_deployer: float = field(default_factory=lambda: _float("DEGEN_SNIPER_BOOST_DEPLOYER", _y("degen", "sniper_boost_deployer", default=5.0)))
    # --- narrative-heat boost, idea #5 (user decision 2026-09-28) --------
    # A coin riding a hot narrative (heat > 1.0) earns up to
    # narrative_boost_max extra sniper points, scaled by (heat - 1.0).
    # Heat <= 1.0 is never a penalty — soft signal only, never a gate.
    narrative_boost_enabled: bool = field(default_factory=lambda: _bool("DEGEN_NARRATIVE_BOOST_ENABLED", _y("degen", "narrative_boost_enabled", default=True)))
    narrative_boost_max: float = field(default_factory=lambda: _float("DEGEN_NARRATIVE_BOOST_MAX", _y("degen", "narrative_boost_max", default=8.0)))
    # --- feedback loop, idea #6 (user decision 2026-09-28) ---------------
    # Outcome-driven tuning proposals; manual approval only, safety
    # gates never self-tune (see daemon/launch_scanner/feedback.py).
    feedback_enabled: bool = field(default_factory=lambda: _bool("DEGEN_FEEDBACK_ENABLED", _y("degen", "feedback_enabled", default=True)))
    feedback_min_outcomes: int = field(default_factory=lambda: _int("DEGEN_FEEDBACK_MIN_OUTCOMES", _y("degen", "feedback_min_outcomes", default=20)))
    # --- confirmed-runner alert lane (user decision 2026-09-28) ------------
    # Chart-confirmed runners (multi-poll proof) drop the CA even when no
    # safety report exists — the chart proof IS the analysis. They carry a
    # prominent UNVETTED label instead of a score. Hard fails (honeypot /
    # can't-sell / high sell tax / insider clusters) still block where a
    # report exists. Unconfirmed coins stay silent as before.
    confirm_unvetted_alerts: bool = field(default_factory=lambda: _bool("DEGEN_CONFIRM_UNVETTED_ALERTS", _y("degen", "confirm_unvetted_alerts", default=True)))
    # --- digest unvetted (user decision 2026-09-28) -----------------------
    # Safety-unknown coins (the screen ran but produced NO report — NOT
    # known-bad) join the hourly digest with a prominent UNVETTED label.
    # Never instant alerts, never paper entries. Hard fails (honeypot /
    # can't-sell / high sell tax / insider clusters), safety errors, and
    # below-bar scores stay held silently. Deliberately NOT a learning
    # knob: only the user's explicit word changes this.
    digest_include_unknown_safety: bool = field(default_factory=lambda: _bool("DEGEN_DIGEST_INCLUDE_UNKNOWN_SAFETY", _y("degen", "digest_include_unknown_safety", default=True)))
    # Early-digest pause switch (user decision 2026-10-02). Default
    # False: the digest card is suppressed (queue dropped at flush).
    early_digest_enabled: bool = field(default_factory=lambda: _bool("DEGEN_EARLY_DIGEST_ENABLED", _y("degen", "early_digest_enabled", default=False)))
    milestone_max_tracked: int = field(default_factory=lambda: _int("DEGEN_MILESTONE_MAX_TRACKED", _y("degen", "milestone_max_tracked", default=1000)))
    # --- runners recap (user decision 2026-09-28) --------------------------
    # Twice-daily card listing only the coins that actually ran (>=
    # recap_min_mult from their card-time anchor) since the last recap.
    # Informational only: never trades, never gates. Times are WAT
    # (Africa/Lagos, UTC+1, no DST).
    recap_enabled: bool = field(default_factory=lambda: _bool("DEGEN_RECAP_ENABLED", _y("degen", "recap_enabled", default=True)))
    recap_times: List[str] = field(default_factory=lambda: _list("DEGEN_RECAP_TIMES", _y("degen", "recap_times", default=["08:00", "20:00"])))
    recap_min_mult: float = field(default_factory=lambda: _float("DEGEN_RECAP_MIN_MULT", _y("degen", "recap_min_mult", default=2.0)))
    recap_max_items: int = field(default_factory=lambda: _int("DEGEN_RECAP_MAX_ITEMS", _y("degen", "recap_max_items", default=20)))
    # Weekly learner review (user decision 2026-09-28): one card every
    # Monday 08:00 WAT — new proposals, applied changes + grades, the
    # week's win rate. Informational only. NOT a learning knob.
    review_enabled: bool = field(default_factory=lambda: _bool("DEGEN_REVIEW_ENABLED", _y("degen", "review_enabled", default=True)))
    review_weekday: int = field(default_factory=lambda: _int("DEGEN_REVIEW_WEEKDAY", _y("degen", "review_weekday", default=0)))
    review_time: str = field(default_factory=lambda: _str("DEGEN_REVIEW_TIME", _y("degen", "review_time", default="08:00")))
    # --- winner-likeness ranker, idea #5 (user decision 2026-10-01) --------
    # Every confirmed/lowcap runner is scored 0-100 against the bot's own
    # 10x+ history (the score rides on the card). OFF (0) until the user
    # approves a threshold via /tune. Hard safety gates are never
    # affected by this ranker.
    winner_min_score: float = field(default_factory=lambda: _float("DEGEN_WINNER_MIN_SCORE", _y("degen", "winner_min_score", default=0.0)))
    # --- sustained-velocity tier, idea #4 (user decision 2026-10-01) -------
    # Re-check each confirmed runner sustained_delay_min after its first
    # alert: SUSTAINED earns a louder top-tier card, FADING/DEAD stay
    # silent (logged only, never spam). The first alert is never delayed
    # or changed.
    sustained_enabled: bool = field(default_factory=lambda: _bool("DEGEN_SUSTAINED_ENABLED", _y("degen", "sustained_enabled", default=True)))
    sustained_delay_min: float = field(default_factory=lambda: _float("DEGEN_SUSTAINED_DELAY_MIN", _y("degen", "sustained_delay_min", default=30.0)))
    sustained_peak_hold_pct: float = field(default_factory=lambda: _float("DEGEN_SUSTAINED_PEAK_HOLD_PCT", _y("degen", "sustained_peak_hold_pct", default=70.0)))
    sustained_lookback_min: float = field(default_factory=lambda: _float("DEGEN_SUSTAINED_LOOKBACK_MIN", _y("degen", "sustained_lookback_min", default=15.0)))
    sustained_max_drawdown_pct: float = field(default_factory=lambda: _float("DEGEN_SUSTAINED_MAX_DRAWDOWN_PCT", _y("degen", "sustained_max_drawdown_pct", default=30.0)))
    # --- second-wave measurement, idea #3 (user decision 2026-10-01) ------
    # Records every published second-wave card and settles it 24h later;
    # the feedback learner can propose trigger tweaks only after >= 10
    # settled waves. Measurement only — entry behavior is unchanged.
    wavefront_enabled: bool = field(default_factory=lambda: _bool("DEGEN_WAVEFRONT_ENABLED", _y("degen", "wavefront_enabled", default=True)))
    # --- Top Picks shortlist (user decision 2026-10-01) -------------------
    # Once a day at toppicks_time (WAT), rank the last 24h of confirmed
    # runners by composite score and publish ONE card with the top
    # toppicks_count — the shortlist to gamble from. Ranking only: the
    # safety bar, hard gates, and caps are never affected; picks are
    # alert-only, never auto-traded.
    toppicks_enabled: bool = field(default_factory=lambda: _bool("DEGEN_TOPPICKS_ENABLED", _y("degen", "toppicks_enabled", default=True)))
    toppicks_count: int = field(default_factory=lambda: _int("DEGEN_TOPPICKS_COUNT", _y("degen", "toppicks_count", default=5)))
    toppicks_time: str = field(default_factory=lambda: _str("DEGEN_TOPPICKS_TIME", _y("degen", "toppicks_time", default="08:00")))


@dataclass
class PerformanceReviewConfig:
    enabled: bool = field(default_factory=lambda: _bool("PERFORMANCE_REVIEW_ENABLED", _y("performance", "enabled", default=True)))
    interval_hours: float = field(default_factory=lambda: _float("PERFORMANCE_REVIEW_INTERVAL_HOURS", _y("performance", "interval_hours", default=6.0)))
    lookback_days: int = field(default_factory=lambda: _int("PERFORMANCE_LOOKBACK_DAYS", _y("performance", "lookback_days", default=30)))


@dataclass
class KillSwitchConfig:
    check_interval_seconds: int = field(default_factory=lambda: _int("KILL_SWITCH_CHECK_INTERVAL_SECONDS", _y("kill_switch", "check_interval_seconds", default=10)))


@dataclass
class EventsConfig:
    enabled: bool = field(default_factory=lambda: _bool("EVENTS_ENABLED", _y("events", "enabled", default=True)))
    queue_max: int = field(default_factory=lambda: _int("EVENTS_QUEUE_MAX", _y("events", "queue_max", default=1000)))
    journal_retention_days: int = field(default_factory=lambda: _int(
        "EVENTS_JOURNAL_RETENTION_DAYS", _y("events", "journal_retention_days", default=30)))
    journal_max_events: int = field(default_factory=lambda: _int(
        "EVENTS_JOURNAL_MAX_EVENTS", _y("events", "journal_max_events", default=100000)))


@dataclass
class NotificationsConfig:
    enabled: bool = field(default_factory=lambda: _bool("NOTIFICATIONS_ENABLED", _y("notifications", "enabled", default=False)))
    poll_interval_seconds: float = field(default_factory=lambda: _float("NOTIFY_POLL_INTERVAL_SECONDS", _y("notifications", "poll_interval_seconds", default=5.0)))
    timeout_seconds: float = field(default_factory=lambda: _float("NOTIFY_TIMEOUT_SECONDS", _y("notifications", "timeout_seconds", default=5.0)))
    max_attempts: int = field(default_factory=lambda: _int("NOTIFY_MAX_ATTEMPTS", _y("notifications", "max_attempts", default=2)))
    backoff_seconds: float = field(default_factory=lambda: _float("NOTIFY_BACKOFF_SECONDS", _y("notifications", "backoff_seconds", default=1.0)))
    channel_agent: bool = field(default_factory=lambda: _bool("NOTIFY_CHANNEL_AGENT", _y("notifications", "channels", "agent", default=True)))
    channel_worker: bool = field(default_factory=lambda: _bool("NOTIFY_CHANNEL_WORKER", _y("notifications", "channels", "worker", default=True)))
    channel_fcm: bool = field(default_factory=lambda: _bool("NOTIFY_CHANNEL_FCM", _y("notifications", "channels", "fcm", default=False)))
    channel_webhook: bool = field(default_factory=lambda: _bool("NOTIFY_CHANNEL_WEBHOOK", _y("notifications", "channels", "webhook", default=False)))
    channel_telegram: bool = field(default_factory=lambda: _bool("NOTIFY_CHANNEL_TELEGRAM", _y("notifications", "channels", "telegram", default=False)))
    # Prefer the NOTIFY_WEBHOOK_URL env var — the URL may embed a token.
    webhook_url: str = field(default_factory=lambda: _str("NOTIFY_WEBHOOK_URL", _y("notifications", "webhook_url", default="")))
    # Bot token is a secret: TELEGRAM_BOT_TOKEN env var ONLY, never yaml.
    telegram_bot_token: str = field(default_factory=lambda: _str("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: _str("TELEGRAM_CHAT_ID", _y("notifications", "telegram_chat_id", default="")))
    fcm_project_id: str = field(default_factory=lambda: _str("FCM_PROJECT_ID", _y("notifications", "fcm_project_id", default="")))
    # Worker push endpoint path for FCM ("" = none defined yet; the FCM
    # channel then honestly reports unconfigured).
    fcm_push_path: str = field(default_factory=lambda: _str("NOTIFY_FCM_PUSH_PATH", _y("notifications", "fcm_push_path", default="")))
    routing: Dict[str, tuple] = field(default_factory=lambda: _notify_routing())


def _notify_routing() -> Dict[str, tuple]:
    """Severity -> channel-name routing, yaml-overridable.

    Defaults mirror agent.notifications.dispatcher.DEFAULT_ROUTING (lazy
    import keeps config.py free of the agent package at import time).
    """
    from agent.notifications.dispatcher import DEFAULT_ROUTING  # noqa: PLC0415
    routing = {str(k).upper(): tuple(v) for k, v in DEFAULT_ROUTING.items()}
    raw = _y("notifications", "routing", default=None)
    if isinstance(raw, dict):
        for sev, names in raw.items():
            if isinstance(names, (list, tuple)):
                routing[str(sev).upper()] = tuple(str(n) for n in names)
    return routing


@dataclass
class LoggingConfig:
    level: str = field(default_factory=lambda: _str("LOG_LEVEL", _y("logging", "level", default="INFO")))
    format: str = field(default_factory=lambda: _str("LOG_FORMAT", _y("logging", "format", default="json")))


@dataclass
class ChartConfig:
    symbols: List[str] = field(default_factory=list)  # empty = trading symbols; resolved in AppConfig
    timeframes: List[str] = field(default_factory=lambda: _list("CHART_TIMEFRAMES", _y("charts", "timeframes", default=["M15", "H1", "H4", "D1"])))
    candle_count: int = field(default_factory=lambda: _int("CHART_CANDLE_COUNT", _y("charts", "candle_count", default=150)))
    update_interval_seconds: int = field(default_factory=lambda: _int("CHART_UPDATE_INTERVAL_SECONDS", _y("charts", "update_interval_seconds", default=120)))
    ema_fast: int = field(default_factory=lambda: _int("CHART_EMA_FAST", 20))
    ema_slow: int = field(default_factory=lambda: _int("CHART_EMA_SLOW", 50))


def _resolve_mode() -> str:
    dry_run_env = os.environ.get("DRY_RUN")
    if dry_run_env is not None:
        return "dry_run" if dry_run_env.strip().lower() in ("true", "1", "yes") else "live"
    mode = _str("MODE", _y("mode", default="dry_run")).strip().lower()
    if mode not in ("dry_run", "live"):
        raise RuntimeError(
            f"CONFIG_INVALID: mode must be 'dry_run' or 'live', got {mode!r}."
        )
    return mode


@dataclass
class AppConfig:
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    autonomy: AutonomyConfig = field(default_factory=AutonomyConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    exit_manager: ExitManagerConfig = field(default_factory=ExitManagerConfig)
    degen: DegenConfig = field(default_factory=DegenConfig)
    performance_review: PerformanceReviewConfig = field(default_factory=PerformanceReviewConfig)
    kill_switch: KillSwitchConfig = field(default_factory=KillSwitchConfig)
    events: EventsConfig = field(default_factory=EventsConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    chart: ChartConfig = field(default_factory=ChartConfig)
    mode: str = field(default_factory=_resolve_mode)
    scan_interval_seconds: int = field(default_factory=lambda: _int("SCAN_INTERVAL_SECONDS", _y("scan_interval_seconds", default=60)))
    reconciliation_interval_seconds: int = field(default_factory=lambda: _int("RECONCILIATION_INTERVAL_SECONDS", _y("reconciliation_interval_seconds", default=300)))

    def __post_init__(self) -> None:
        if not self.chart.symbols:
            self.chart.symbols = list(self.trading.symbols)
        if self.autonomy.mode not in ("signals_only", "assisted", "autonomous"):
            raise RuntimeError(
                f"CONFIG_INVALID: autonomy.mode must be signals_only|assisted|autonomous, "
                f"got {self.autonomy.mode!r}."
            )
        if self.broker.provider not in ("memecoin", "disconnected"):
            raise RuntimeError(
                f"CONFIG_INVALID: broker.provider must be memecoin|disconnected, "
                f"got {self.broker.provider!r}."
            )
        # User-approved tuning overrides (idea #6, /tune apply). These sit
        # ON TOP of defaults.yaml + env: the user's explicit word beats
        # everything. Only #6-TUNABLE knobs are accepted (defense in
        # depth: even a hand-edited file can't sneak a safety knob in —
        # feedback.apply_override already refuses those at write time).
        # Each value is coerced to the existing field's type.
        try:
            _overrides = _load_degen_overrides()
        except Exception:
            _overrides = {}
        # TUNABLE allowlist loaded straight from feedback.py by file
        # path (not `from daemon.launch_scanner import ...` — that would
        # run the scanner package __init__ and risk a circular import
        # here). feedback.py is stdlib-only, so it loads standalone.
        _allowed = set()
        try:
            import importlib.util as _ilu
            _fb_path = os.path.join(
                os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))),
                "daemon", "launch_scanner", "feedback.py")
            _spec = _ilu.spec_from_file_location("_fb_tunable", _fb_path)
            _fb = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_fb)
            _allowed = set(_fb.TUNABLE)
        except Exception:
            _allowed = set()
        for _k, _v in _overrides.items():
            if _k not in _allowed:
                continue
            if not hasattr(self.degen, _k):
                continue
            _cur = getattr(self.degen, _k)
            try:
                if isinstance(_cur, bool):
                    if isinstance(_v, str):
                        _v = _v.strip().lower() in (
                            "1", "true", "yes", "y", "on")
                    else:
                        _v = bool(_v)
                elif isinstance(_cur, int) and not isinstance(_cur, bool):
                    _v = int(float(_v))
                elif isinstance(_cur, float):
                    _v = float(_v)
                elif isinstance(_cur, str):
                    _v = str(_v)
                setattr(self.degen, _k, _v)
            except (TypeError, ValueError):
                continue
        # Multi-chain memecoin validation.
        if self.broker.provider == "memecoin":
            from broker.memecoin.chains.registry import chain_names  # noqa: PLC0415 (lazy, stdlib-only)
            valid = set(chain_names())
            unknown = [c for c in self.broker.memecoin.chains if c not in valid]
            if unknown:
                raise RuntimeError(
                    f"CONFIG_INVALID: unknown memecoin chain(s): {unknown} — "
                    f"valid: {sorted(valid)}."
                )
            if self.broker.memecoin.wallet_key_source not in ("none", "env", "file"):
                raise RuntimeError(
                    f"CONFIG_INVALID: memecoin.wallet.key_source must be "
                    f"none|env|file, got "
                    f"{self.broker.memecoin.wallet_key_source!r}."
                )
            if (self.broker.memecoin.wallet_key_source == "file"
                    and not self.broker.memecoin.wallet_key_file):
                raise RuntimeError(
                    "CONFIG_INVALID: memecoin.wallet.key_source=file requires "
                    "memecoin.wallet.key_file (MEMECOIN_WALLET_KEY_FILE)."
                )
            # The wallet block only selects WHERE a future key is read
            # from at runtime. There is no flag here that enables live
            # trading (the provider is paper-only by design), so any
            # unknown key under memecoin.wallet is rejected rather than
            # silently accepted.
            raw_wallet = _y("memecoin", "wallet", default=None)
            if isinstance(raw_wallet, dict):
                bad = sorted(set(raw_wallet) - {"key_source", "key_file"})
                if bad:
                    raise RuntimeError(
                        f"CONFIG_INVALID: unknown memecoin.wallet key(s): "
                        f"{bad} — no live-enabling flags exist at this stage."
                    )
        if self.mode == "live" and self.broker.provider == "memecoin":
            missing = [k for k, v in (
                ("MEMECOIN_LIVE_ARMED", self.broker.memecoin.live_armed),
                ("MEMECOIN_WALLET_PUBKEY", self.broker.memecoin.wallet_pubkey),
            ) if not v]
            if missing:
                raise RuntimeError(
                    f"CREDENTIALS_INVALID: live mode requires memecoin arming; "
                    f"missing: {', '.join(missing)}. (Public key only — never "
                    f"share a wallet secret.)"
                )
        if self.worker.enabled and not self.worker.base_url:
            raise RuntimeError(
                "CONFIG_INVALID: worker.enabled=true requires WORKER_BASE_URL."
            )

    @property
    def dry_run(self) -> bool:
        return self.mode == "dry_run"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def redacted(self) -> Dict[str, Any]:
        """Config as a dict with every secret value masked — safe to log."""
        d = asdict(self)

        def _mask(obj: Any) -> Any:
            if isinstance(obj, dict):
                # Every secret field is masked (declared REDACTED_FIELDS),
                # including the webhook URL (may embed a token). The
                # memecoin wallet PUBLIC key is public by definition and
                # stays visible.
                return {k: ("***" if k in REDACTED_FIELDS else _mask(v))
                        for k, v in obj.items()}
            if isinstance(obj, list):
                return [_mask(v) for v in obj]
            return obj

        return _mask(d)


def load_config() -> AppConfig:
    """Build the effective config: defaults.yaml + environment overrides."""
    return AppConfig()
