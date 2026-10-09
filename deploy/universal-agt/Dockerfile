FROM python:3.12-slim

# No pip dependencies needed — stdlib only
WORKDIR /app

# Copy the meme-project source (build context is the repo root)
COPY . /app/

# Agent home for state, journals, PID files
ENV FOREX_AGENT_HOME=/data
ENV FOREX_API_PORT=8765
ENV FOREX_API_BIND=0.0.0.0

RUN mkdir -p /data/run /data/data && \
    python3 -c "import sys; sys.path.insert(0, '/app'); import agent.tools.registry; print('registry OK')"

COPY deploy/universal-agt/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8765

CMD ["/entrypoint.sh"]
