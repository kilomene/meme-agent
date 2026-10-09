# Universal-AGT Deployment

Deploys the meme-agent on [Universal-AGT](https://github.com/kilomene/Universal-AGT).

## Files

- `Dockerfile` — Python 3.12 slim, stdlib only (no pip dependencies)
- `agent.deploy.json` — Universal-AGT manifest (port 8765, healthcheck `/health`)
- `entrypoint.sh` — starts the 4 monitor daemons in background, API in foreground

## How it works

The container runs `scripts/local_api.py` with `FOREX_API_BIND=0.0.0.0`
so Universal-AGT's healthcheck can reach `GET /health`. The default bind
remains `127.0.0.1` for non-container use — the env var is opt-in.

The four daemons (`market_monitor`, `signal_monitor`, `position_monitor`,
`health_monitor`) run in the background; the API is the foreground process.

## Deploy

```bash
# From the repo root:
tar -czf meme-agent.tar.gz \
  --exclude='.git' \
  agent.deploy.json deploy/ scripts/ agent/ broker/ core/ \
  config/ daemon/ intelligence/ storage/ backtesting/

# Then follow the Universal-AGT deploy guide to upload and deploy.
```

## Known limitations

- **State is ephemeral.** Paper balance, positions, and fill journal live in
  `/data` inside the container. A redeploy wipes them. Universal-AGT doesn't
  have persistent volumes yet.
