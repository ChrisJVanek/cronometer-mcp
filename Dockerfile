# Hosted HTTP mode (ChatGPT, claude.ai). Set MCP_PUBLIC_URL, MCP_AUTH_PASSWORD and
# the Cronometer credentials as service variables, and mount a volume at /data so
# the Cronometer sessions and OAuth tokens survive redeploys.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev

ENV CRONOMETER_DATA_DIR=/data \
    XDG_CACHE_HOME=/data/cache \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0
EXPOSE 8430
CMD ["uv", "run", "--frozen", "--no-dev", "cronometer-mcp", "--transport", "http"]
