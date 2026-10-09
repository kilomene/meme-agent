#!/bin/bash
# Container entrypoint: daemons in background, API in foreground.
set -e

export FOREX_AGENT_HOME="${FOREX_AGENT_HOME:-/data}"
export FOREX_API_BIND="${FOREX_API_BIND:-0.0.0.0}"
export FOREX_API_PORT="${FOREX_API_PORT:-8765}"
mkdir -p "$FOREX_AGENT_HOME/run" "$FOREX_AGENT_HOME/data"

cd /app

# Start the monitor daemons in the background
for daemon in market_monitor signal_monitor position_monitor health_monitor; do
    echo "starting daemon: $daemon"
    python3 -m "daemon.${daemon}" >> "$FOREX_AGENT_HOME/run/${daemon}.log" 2>&1 &
done

# Start telegram notification daemon if configured
if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
    echo "starting daemon: telegram"
    python3 -m daemon.telegram >> "$FOREX_AGENT_HOME/run/telegram.log" 2>&1 &
fi

# API in foreground — this is the container's main process.
# Universal-AGT healthchecks GET /health on $FOREX_API_PORT.
echo "starting API server on ${FOREX_API_BIND}:${FOREX_API_PORT}"
exec python3 scripts/local_api.py --port "$FOREX_API_PORT"
